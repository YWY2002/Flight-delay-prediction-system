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


class OpenSkyPollError(RuntimeError):
    """Base class for every failure the OpenSky pollers raise.

    Attributes:
        retryable: Whether repeating the identical request could succeed.
            Drives backoff decisions in the poll loop, so that a 429 costs a
            wait and a 400 costs a bug report rather than four wasted credits.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class OpenSkyUnreachable(OpenSkyPollError):
    """The request never produced an HTTP response.

    Connection reset, DNS failure, TLS error, or the client-side timeout. Always
    retryable: nothing about the request itself is known to be wrong.
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

    # 429 is the credit budget refilling and 5xx is OpenSky's problem, not
    # ours; both clear up on their own. 408/425 are transport hiccups the
    # server chose to report as a status. Everything else (400 malformed, 401
    # bad token, 403 window not permitted for this account) is a defect in the
    # request that will fail identically on every retry.
    _RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(self, message: str, *, status: int | None) -> None:
        # An unobservable status is treated as retryable on purpose: retrying a
        # fatal error wastes a few credits, while giving up on a transient one
        # loses a window of flight data that can never be re-polled once it
        # ages out of the free tier.
        super().__init__(
            message,
            retryable=status is None or status in self._RETRYABLE_STATUSES,
        )
        self.status = status
