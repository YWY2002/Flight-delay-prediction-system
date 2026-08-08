"""Shared error taxonomy for ingestion.

One flag, `retryable`, read by the retry policy in `retry.py`. It lives on the
exception class, set where the error is raised and the cause is actually known,
rather than in a list of types maintained elsewhere that drifts out of sync and
fails silently in both directions.

Default is False: a new error type is non-retryable until someone deliberately
opts it in. That is the safe direction to be wrong in, because the cost of not
retrying a transient failure is one lost poll cycle, while the cost of retrying
a permanent one is hammering an API until access is suspended.
"""

from __future__ import annotations

from typing import ClassVar


class IngestError(RuntimeError):
    """Base for every ingestion failure. Not retryable."""

    retryable: ClassVar[bool] = False


class TransientIngestError(IngestError):
    """Worth retrying: timeouts, connection resets, 5xx, rate limits."""

    retryable: ClassVar[bool] = True


class RateLimitError(TransientIngestError):
    """Rate limited. Retryable, but on the server's terms.

    `retry_after_seconds` carries the `Retry-After` header when present; the
    retry policy prefers it over its own backoff, since guessing something
    shorter than the server asked for just earns another rejection.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
