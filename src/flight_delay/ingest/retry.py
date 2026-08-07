"""Retry policy for transient ingestion failures.

Built on tenacity, with three properties that matter more than the retry loop
itself:

1. **Only transient failures retry.** The policy reads a `retryable` flag off
   the exception. Retrying bad credentials in a loop is how API access gets
   suspended, and retrying a malformed-response error just produces the same
   malformed response more slowly.
2. **`Retry-After` beats our own backoff.** When the server states how long to
   wait, guessing something shorter earns another 429.
3. **Jitter, always.** Several pollers that fail together and back off on an
   identical schedule will retry together too, re-synchronising into the
   thundering herd the backoff was meant to break up.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from flight_delay.common.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 60.0


def is_retryable(exc: BaseException) -> bool:
    """Whether an exception is worth retrying.

    The flag lives on the exception class, set at the point where the error is
    raised and the cause is actually known. The alternative, a tuple of
    retryable types maintained here, drifts out of sync with the raise sites and
    fails silently in both directions.

    Anything without the attribute is treated as non-retryable: unknown failures
    should surface, not be quietly repeated.
    """
    return bool(getattr(exc, "retryable", False))


def _retry_after_seconds(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after_seconds", None)
    if isinstance(value, int | float) and value >= 0:
        return float(value)
    return None


def build_retrying(
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Retrying:
    """Build the retry controller.

    Args:
        max_attempts: Total attempts including the first. Capped rather than
            unbounded so a persistent outage surfaces as a failed poll cycle
            instead of a task wedged forever, invisible to the scheduler.
        sleep: Injected so tests can assert on the backoff schedule without
            actually waiting. A test that really sleeps through exponential
            backoff is a test nobody runs.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")

    exponential = wait_exponential_jitter(
        initial=initial_backoff_seconds,
        max=max_backoff_seconds,
        jitter=initial_backoff_seconds,
    )

    def wait(retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None

        if exc is not None:
            server_hint = _retry_after_seconds(exc)
            if server_hint is not None:
                # Honour the server, but bounded. An absurd Retry-After would
                # otherwise park the poller for hours; better to fail this cycle
                # and let the scheduler come back on its own cadence.
                delay = min(server_hint, max_backoff_seconds)
                logger.info(
                    "retry.honouring_retry_after",
                    delay_seconds=round(delay, 2),
                    next_attempt=retry_state.attempt_number + 1,
                    server_hint_seconds=server_hint,
                )
                return delay

        delay = float(exponential(retry_state))
        logger.info(
            "retry.backing_off",
            error_type=type(exc).__name__ if exc is not None else "unknown",
            delay_seconds=round(delay, 2),
            next_attempt=retry_state.attempt_number + 1,
        )
        return delay

    return Retrying(
        retry=retry_if_exception(is_retryable),
        stop=stop_after_attempt(max_attempts),
        wait=wait,
        sleep=sleep,
        # Re-raise the ORIGINAL exception rather than tenacity's RetryError
        # wrapper. Callers catch OpenSkyRateLimitError and CreditBudgetExhausted
        # by type; burying those inside a wrapper would break every handler and
        # make tracebacks harder to read.
        reraise=True,
    )


def call_with_retry[T](retrying: Retrying, fn: Callable[[], T]) -> T:
    """Invoke `fn` under `retrying`, preserving its return type.

    A thin wrapper, but it keeps the generic plumbing in one place so call sites
    read as ordinary typed calls.
    """
    return retrying(fn)
