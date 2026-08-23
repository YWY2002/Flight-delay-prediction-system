"""Single-shot OpenSky airport queries.

One function per endpoint, each doing exactly one request. Scheduling, retry
and persistence belong to the caller; what lives here is the boundary where an
untyped HTTP answer becomes either flight rows or a typed failure.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from opensky_api import (
    FlightData,
    OpenSkyApi,
    OpenSkyStates,
    _count_utc_dates,
)
from pydantic import BaseModel, Field, model_validator

# `opensky_api` is built on `requests`, so transport failures surface as
# `requests` exceptions escaping the client untouched.
from requests import RequestException

from flight_delay.common.airports import BoundingBox
from flight_delay.common.config import Settings
from flight_delay.common.logging_config import get_logger
from flight_delay.common.timeutil import utc_now
from flight_delay.data_ingestion.opensky.client import TrackedOpenSkyApi
from flight_delay.data_ingestion.opensky.errors import (
    OpenSkyRequestFailed,
    OpenSkyThrottled,
    OpenSkyUnreachable,
)

logger = get_logger(__name__)

_ISO = "%Y-%m-%dT%H:%M:%SZ"

# How long after a window closes OpenSky needs before an empty answer for it is
# credible rather than premature. These are not documented upstream; they are
# measured, and they only ever pick a log level, never control flow.
#
# Measured 2026-08-22/23 against WSSS, EGLL and KJFK. OpenSky attributes a
# departure airport as soon as a track starts, so `flights/departure` answers
# within minutes, but the count keeps climbing for hours: a live window
# returned roughly a fifth of the rows the same window held a day later. An
# arrival airport can only be attributed once a track is *closed out*, and that
# runs as a batch. At 08-22 13:45Z the whole of 08-21 was complete, yet at
# 08-23 05:02Z the whole of 08-22 was still empty, so a given day lands at some
# hour during the following day. That puts the arrival worst case just under 48h.
_DEPARTURE_SETTLE_HOURS = 24.0
_ARRIVAL_SETTLE_HOURS = 48.0

# Both upstream methods share this shape: (airport, begin, end) -> rows or None.
_FetchFlights = Callable[[str, int, int], list[FlightData] | None]


class PollingDetails(BaseModel):
    """One airport-and-window query, validated before it can reach the wire."""

    settings: Settings
    airport: str = Field(
        ...,
        min_length=3,
        max_length=4,
        description="IATA (3-letter) or ICAO (4-letter) airport code",
    )
    epoch_start: int = Field(..., ge=0, description="Window start, Unix epoch seconds.")
    epoch_end: int = Field(..., ge=0, description="Window end, Unix epoch seconds.")

    @model_validator(mode="after")
    def _check_window(self) -> PollingDetails:
        """Reject windows the API would reject, at construction rather than at
        call time, so the traceback points at whoever built the bad window."""
        if self.epoch_start >= self.epoch_end:
            raise ValueError(
                f"epoch_end must be greater than epoch_start "
                f"(got start={self.epoch_start}, end={self.epoch_end})."
            )
        crossed = _count_utc_dates(self.epoch_start, self.epoch_end)
        if crossed > 1:
            raise ValueError(
                f"The airport endpoints accept a window crossing at most one UTC "
                f"calendar day boundary; {self.window_utc} crosses {crossed}. Note "
                f"this counts calendar dates, not elapsed hours: 23:00 to 01:00 "
                f"crosses one boundary and is fine, 23:00 to 01:00 two nights "
                f"later is not."
            )
        return self

    @property
    def window_utc(self) -> str:
        """The window as a human-readable UTC interval, for logs and errors."""
        start = datetime.fromtimestamp(self.epoch_start, UTC).strftime(_ISO)
        end = datetime.fromtimestamp(self.epoch_end, UTC).strftime(_ISO)
        return f"{start}/{end}"

    @property
    def window_age_hours(self) -> float:
        """Hours since the window closed. Negative if it has not closed yet."""
        return (utc_now().timestamp() - self.epoch_end) / 3600.0


def _poll_once(
    client: OpenSkyApi,
    details: PollingDetails,
    fetch: _FetchFlights,
    endpoint: str,
    settle_hours: float,
) -> list[FlightData]:
    """Run one airport query, converting every non-answer into a typed failure.

    Args:
        settle_hours: How old the window must be before an empty result is
            logged as ordinary rather than suspect. See the module constants.

    Returns:
        The flights OpenSky attributed to this airport and window. An empty
        list is a legitimate answer and is returned, not raised.

    Raises:
        OpenSkyUnreachable: the request produced no HTTP response.
        OpenSkyRequestFailed: a response arrived, but carried no data.
    """
    where = f"{endpoint} {details.airport} [{details.window_utc}]"

    try:
        flights = fetch(details.airport, details.epoch_start, details.epoch_end)
    except RequestException as exc:
        raise OpenSkyUnreachable(f"OpenSky {where} never completed: {exc}") from exc

    if flights is None:
        # `_get_json` returns None for every non-200 that is not a 404. Recover
        # the status if the caller handed us a client that kept it, so the
        # exception can say whether waiting will help.
        status = client.last_status if isinstance(client, TrackedOpenSkyApi) else None
        detail = (
            f"HTTP {status}"
            if status is not None
            else "status unavailable (pass a TrackedOpenSkyApi to capture it)"
        )
        raise OpenSkyRequestFailed(f"OpenSky {where} returned no data: {detail}", status=status)

    if not flights:
        # A 404 ("nothing found") and a genuinely quiet window are
        # indistinguishable over the wire, so this is a log line and not an
        # exception. Windows too recent to have settled are raised to WARNING,
        # because for those, empty is far more likely premature than true.
        age = details.window_age_hours
        settled = age >= settle_hours
        emit = logger.info if settled else logger.warning
        emit(
            "opensky.poll.empty",
            endpoint=endpoint,
            airport=details.airport,
            window=details.window_utc,
            window_age_hours=round(age, 1),
            settled=settled,
        )
    else:
        logger.debug(
            "opensky.poll.completed",
            endpoint=endpoint,
            airport=details.airport,
            window=details.window_utc,
            flights=len(flights),
        )

    return flights


def poll_airport_departure_once(client: OpenSkyApi, details: PollingDetails) -> list[FlightData]:
    """Flights that departed `details.airport` within the window.

    Close to live, because a departure airport is attributed as soon as a track
    starts. Not *complete* live, though: a window inside the current UTC day
    returns a partial count that keeps growing for hours, so re-poll it a day
    later when you need the settled figure.
    """
    return _poll_once(
        client,
        details,
        client.get_departures_by_airport,
        "flights/departure",
        _DEPARTURE_SETTLE_HOURS,
    )


def poll_airport_arrival_once(client: OpenSkyApi, details: PollingDetails) -> list[FlightData]:
    """Flights that arrived at `details.airport` within the window.

    Unlike departures this is not usable live at all. An arrival airport is
    attributed only once a track is closed out, and that batch settles at some
    hour during the *following* UTC day: a window 27h old was still returning
    nothing while one 39h old was complete. Run this as a backfill over whole
    past days, late enough that D-1 has landed, and read an empty answer for
    anything recent as "not derived yet" rather than "no traffic".
    """
    return _poll_once(
        client,
        details,
        client.get_arrivals_by_airport,
        "flights/arrival",
        _ARRIVAL_SETTLE_HOURS,
    )


def poll_states_once(client: OpenSkyApi, bbox: BoundingBox) -> OpenSkyStates:
    """Every aircraft state vector currently inside `bbox`.

    The live half of ingestion. Where the flights endpoints lag by hours to
    days, this reports what is airborne right now, and it is the only source of
    the altitude/vertical-rate traces that approach-anomaly detection needs.

    Returns:
        The snapshot, whose `time` field stamps the validity of every vector in
        it -- carry that, not the wall clock, as the observation timestamp. An
        empty `states` list is normal rather than an error: a 60 nm box at 3am
        often holds nothing.

    Raises:
        OpenSkyThrottled: the client's own limiter refused to send.
        OpenSkyUnreachable: the request produced no HTTP response.
        OpenSkyRequestFailed: a response arrived, but carried no data.
    """
    # `get_states` wants a positional 4-tuple, not the keyword arguments the
    # BoundingBox field names mirror, and the order is both latitudes first:
    # (lamin, lamax, lomin, lomax), NOT (lamin, lomin, lamax, lomax). Building
    # it here keeps `common.airports` free of any opensky_api coupling.
    bbox_tuple = (bbox.lamin, bbox.lamax, bbox.lomin, bbox.lomax)
    where = (
        f"states/all lat[{bbox.lamin:.4f},{bbox.lamax:.4f}] "
        f"lon[{bbox.lomin:.4f},{bbox.lomax:.4f}]"
    )

    seen_before = client.responses_seen if isinstance(client, TrackedOpenSkyApi) else None

    try:
        states = client.get_states(bbox=bbox_tuple)
    except RequestException as exc:
        raise OpenSkyUnreachable(f"OpenSky {where} never completed: {exc}") from exc

    if states is None:
        # Unlike the flights endpoints, `get_states` guards itself with a
        # client-side limiter and returns None *before sending anything* when
        # it trips. `last_status` would still hold the previous call's code, so
        # the response counter is what separates the two cases.
        if seen_before is not None and client.responses_seen == seen_before:
            raise OpenSkyThrottled(
                f"OpenSky {where} was blocked by the client's own rate limiter; "
                f"no request was sent. get_states permits one call every 5s "
                f"authenticated (10s anonymous), counted per method rather than "
                f"per bounding box -- polling several airports back to back "
                f"through a single client will trip it."
            )
        status = client.last_status if isinstance(client, TrackedOpenSkyApi) else None
        detail = (
            f"HTTP {status}"
            if status is not None
            else "status unavailable (pass a TrackedOpenSkyApi to capture it)"
        )
        raise OpenSkyRequestFailed(f"OpenSky {where} returned no data: {detail}", status=status)

    # `time` is the instant the whole snapshot describes; every vector in it is
    # valid for [time - 1, time]. Logging its lag makes a stalled feed visible
    # as growing staleness rather than as a silently repeating aircraft count.
    snapshot_age = round(utc_now().timestamp() - states.time, 1)

    if not states.states:
        logger.info("opensky.states.empty", bbox=where, snapshot_age_s=snapshot_age)
    else:
        logger.debug(
            "opensky.states.completed",
            bbox=where,
            aircraft=len(states.states),
            snapshot_age_s=snapshot_age,
        )

    return states
