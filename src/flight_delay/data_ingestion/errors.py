"""Failure types shared by every ingestion source.

Each source (OpenSky, aviationweather.gov, the FAA feed) has its own failure
modes and its own exception module. What they share is the question a poll loop
actually asks: did this fail, and is waiting going to help? That question is
answered here so a caller polling several sources can write one `except
IngestError` and one retry policy rather than one per source.
"""

from __future__ import annotations

# Statuses where repeating the identical request can plausibly succeed. 429 is a
# budget refilling and 5xx is the far end having a bad time; both clear up on
# their own. 408/425 are transport hiccups the server chose to report as a
# status. Everything else (400 malformed, 401 bad token, 403 not permitted, 404
# no such resource) is a defect in the request that will fail the same way on
# every attempt, and retrying it just spends quota to learn nothing.
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class IngestError(RuntimeError):
    """Base class for every failure raised by an ingestion poller.

    Attributes:
        retryable: Whether repeating the identical request could succeed.
            Drives backoff decisions in the poll loop, so a 429 costs a wait
            while a 400 costs a bug report rather than four wasted attempts.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def status_is_retryable(status: int | None) -> bool:
    """Classify an HTTP status, treating an unknown one as retryable.

    Unobservable statuses are optimistic on purpose: retrying a fatal error
    wastes a little quota, while giving up on a transient one loses a window of
    data that often cannot be re-fetched once it ages out.
    """
    return status is None or status in RETRYABLE_HTTP_STATUSES
