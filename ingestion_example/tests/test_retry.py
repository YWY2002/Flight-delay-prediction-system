"""Tests for the retry policy.

`sleep` is injected everywhere, so the backoff schedule is asserted by
inspecting recorded durations rather than by actually waiting. A test that
genuinely sleeps through exponential backoff takes a minute and gets skipped.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from flight_delay.ingest.retry import build_retrying, call_with_retry, is_retryable


class Transient(RuntimeError):
    retryable: ClassVar[bool] = True


class Permanent(RuntimeError):
    retryable: ClassVar[bool] = False


class WithRetryAfter(RuntimeError):
    retryable: ClassVar[bool] = True

    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry after {seconds}")
        self.retry_after_seconds = seconds


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# ---- Classification --------------------------------------------------------


def test_flagged_exceptions_are_retryable() -> None:
    assert is_retryable(Transient())
    assert not is_retryable(Permanent())


def test_unflagged_exceptions_are_not_retryable() -> None:
    """Unknown failures should surface, not be quietly repeated."""
    assert not is_retryable(ValueError("who knows"))


# ---- Retry behaviour -------------------------------------------------------


def test_succeeds_without_sleeping_when_nothing_fails() -> None:
    sleeper = SleepRecorder()
    retrying = build_retrying(max_attempts=4, sleep=sleeper)

    assert call_with_retry(retrying, lambda: "ok") == "ok"
    assert sleeper.delays == []


def test_retries_a_transient_failure_then_succeeds() -> None:
    sleeper = SleepRecorder()
    retrying = build_retrying(max_attempts=4, sleep=sleeper)
    attempts = 0

    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise Transient("boom")
        return "ok"

    assert call_with_retry(retrying, flaky) == "ok"
    assert attempts == 3
    assert len(sleeper.delays) == 2


def test_permanent_failure_is_not_retried() -> None:
    """Retrying bad credentials in a loop is how API access gets suspended."""
    sleeper = SleepRecorder()
    retrying = build_retrying(max_attempts=4, sleep=sleeper)
    attempts = 0

    def always_permanent() -> str:
        nonlocal attempts
        attempts += 1
        raise Permanent("credentials are wrong")

    with pytest.raises(Permanent):
        call_with_retry(retrying, always_permanent)

    assert attempts == 1
    assert sleeper.delays == []


def test_gives_up_after_max_attempts() -> None:
    """Bounded so a persistent outage surfaces as a failed cycle rather than a
    task wedged forever and invisible to the scheduler."""
    sleeper = SleepRecorder()
    retrying = build_retrying(max_attempts=3, sleep=sleeper)
    attempts = 0

    def always_fails() -> str:
        nonlocal attempts
        attempts += 1
        raise Transient("still broken")

    with pytest.raises(Transient):
        call_with_retry(retrying, always_fails)

    assert attempts == 3
    assert len(sleeper.delays) == 2


def test_original_exception_propagates_not_a_wrapper() -> None:
    """reraise=True. Callers catch OpenSkyRateLimitError and
    CreditBudgetExhausted by type; a RetryError wrapper would break every
    handler."""
    retrying = build_retrying(max_attempts=2, sleep=SleepRecorder())

    with pytest.raises(Transient, match="the real message"):
        call_with_retry(retrying, lambda: (_ for _ in ()).throw(Transient("the real message")))


def test_max_attempts_of_one_means_no_retry() -> None:
    sleeper = SleepRecorder()
    retrying = build_retrying(max_attempts=1, sleep=sleeper)

    with pytest.raises(Transient):
        call_with_retry(retrying, lambda: (_ for _ in ()).throw(Transient("x")))

    assert sleeper.delays == []


def test_rejects_invalid_max_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts must be at least 1"):
        build_retrying(max_attempts=0)


# ---- Backoff schedule ------------------------------------------------------


def test_backoff_grows_and_stays_within_the_ceiling() -> None:
    sleeper = SleepRecorder()
    retrying = build_retrying(
        max_attempts=6,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=10.0,
        sleep=sleeper,
    )

    with pytest.raises(Transient):
        call_with_retry(retrying, lambda: (_ for _ in ()).throw(Transient("x")))

    assert len(sleeper.delays) == 5
    assert all(0 < d <= 10.0 for d in sleeper.delays)
    # Growth, not a flat schedule. Compared loosely because of jitter.
    assert sleeper.delays[-1] > sleeper.delays[0]


def test_backoff_is_jittered() -> None:
    """Without jitter, pollers that fail together back off on identical
    schedules and retry together, re-forming the thundering herd the backoff
    exists to break up."""
    observed: set[float] = set()

    for _ in range(8):
        sleeper = SleepRecorder()
        retrying = build_retrying(
            max_attempts=3,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=30.0,
            sleep=sleeper,
        )
        with pytest.raises(Transient):
            call_with_retry(retrying, lambda: (_ for _ in ()).throw(Transient("x")))
        observed.update(sleeper.delays)

    assert len(observed) > 1


# ---- Retry-After -----------------------------------------------------------


def test_server_retry_after_overrides_our_backoff() -> None:
    """Guessing something shorter than the server asked for earns another 429."""
    sleeper = SleepRecorder()
    retrying = build_retrying(
        max_attempts=2,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=300.0,
        sleep=sleeper,
    )

    with pytest.raises(WithRetryAfter):
        call_with_retry(retrying, lambda: (_ for _ in ()).throw(WithRetryAfter(45.0)))

    assert sleeper.delays == [45.0]


def test_retry_after_is_capped_by_the_ceiling() -> None:
    """An absurd Retry-After would otherwise park the poller for hours. Better
    to fail this cycle and let the scheduler return on its own cadence."""
    sleeper = SleepRecorder()
    retrying = build_retrying(max_attempts=2, max_backoff_seconds=60.0, sleep=sleeper)

    with pytest.raises(WithRetryAfter):
        call_with_retry(retrying, lambda: (_ for _ in ()).throw(WithRetryAfter(86_400.0)))

    assert sleeper.delays == [60.0]


def test_missing_retry_after_falls_back_to_exponential() -> None:
    sleeper = SleepRecorder()
    retrying = build_retrying(
        max_attempts=2,
        initial_backoff_seconds=2.0,
        max_backoff_seconds=30.0,
        sleep=sleeper,
    )

    with pytest.raises(Transient):
        call_with_retry(retrying, lambda: (_ for _ in ()).throw(Transient("no hint")))

    assert len(sleeper.delays) == 1
    assert 0 < sleeper.delays[0] <= 30.0
