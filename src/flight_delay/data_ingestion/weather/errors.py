"""Failure types raised by the aviationweather.gov pollers."""

from __future__ import annotations

from flight_delay.data_ingestion.errors import IngestError, status_is_retryable


class WeatherPollError(IngestError):
    """Base class for every failure the METAR and TAF pollers raise."""


class WeatherUnreachable(WeatherPollError):
    """The request never produced an HTTP response.

    Connection reset, DNS failure, TLS error, or the client-side timeout. Always
    retryable: nothing about the request itself is known to be wrong.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class WeatherRequestFailed(WeatherPollError):
    """The API answered with an error status.

    Attributes:
        status: The HTTP status returned.
    """

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message, retryable=status_is_retryable(status))
        self.status = status


class WeatherResponseInvalid(WeatherPollError):
    """A success status carrying a body we cannot use.

    Distinct from :class:`WeatherRequestFailed` because the status looked fine:
    the failure is in the payload. Treated as retryable, since in practice this
    is an upstream maintenance or error page served with a 200 rather than a
    permanent contract change. If it persists, the contract really did move and
    the parsing here needs revisiting.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)
