"""Failure types raised by the OpenSky pollers.

The upstream ``opensky_api`` client collapses every outcome into two return
values: ``None`` for any non-200 response and ``[]`` for a 404. A caller
therefore cannot tell "out of credits", "not authorised for this window",
"malformed request" and "no flights in this window" apart, even though only
the last one is a legitimate answer and only some of the others are worth
retrying.

These exceptions restore that distinction. Everything here derives from
:class:`OpenSkyPollError`, so a caller that does not care about the detail can
catch one type and mark the poll cycle failed.
"""

from __future__ import annotations

from flight_delay.data_ingestion.errors import IngestError, status_is_retryable


class OpenSkyPollError(IngestError):
    """Base class for every failure the OpenSky pollers raise.

    Carries `retryable` from :class:`IngestError`, so a loop polling OpenSky
    alongside the weather sources can catch one type and share one retry policy.
    """


class OpenSkyUnreachable(OpenSkyPollError):
    """The request never produced an HTTP response.

    Connection reset, DNS failure, TLS error, or the client-side timeout. Always
    retryable: nothing about the request itself is known to be wrong.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class OpenSkyThrottled(OpenSkyPollError):
    """The client's own rate limiter refused to send the request.

    Not a server 429: nothing left the process, no credits were spent, and
    waiting is the only cure. `opensky_api` applies this to `get_states` alone
    (5s between calls authenticated, 10s anonymous) and keys it on the *method*,
    so several bounding boxes polled back to back through one client contend
    with each other.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class OpenSkyRequestFailed(OpenSkyPollError):
    """The API answered, but with something other than data.

    Attributes:
        status: The HTTP status, when observable. It is ``None`` for a plain
            ``OpenSkyApi``, which discards the response object; pass a
            :class:`~flight_delay.data_ingestion.opensky.client.TrackedOpenSkyApi`
            to get the real code.
    """

    def __init__(self, message: str, *, status: int | None) -> None:
        super().__init__(message, retryable=status_is_retryable(status))
        self.status = status
