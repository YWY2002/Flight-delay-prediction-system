"""Single-shot METAR and TAF queries against aviationweather.gov.

    https://aviationweather.gov/api/data/metar?ids=WSSS&format=json
    https://aviationweather.gov/api/data/taf?ids=WSSS&format=json

No API key and no auth. Both endpoints take a comma-separated station list, so
N stations cost one round trip rather than N.

Records are returned exactly as received, for the bronze layer. `format=json`
rather than `format=raw` on purpose: the JSON payload *contains* the raw report
text (`rawOb` for METAR, `rawTAF` for TAF) alongside the decoded fields, so it
is a strict superset. The raw text is the canonical, authoritative form of an
observation; keeping it means anything we fail to parse today stays recoverable
later. Typed models belong in silver, not here.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from flight_delay.common.config import Settings
from flight_delay.common.logging_config import get_logger
from flight_delay.data_ingestion.weather.errors import (
    WeatherRequestFailed,
    WeatherResponseInvalid,
    WeatherUnreachable,
)

logger = get_logger(__name__)

# The key both endpoints use for the station identifier.
_STATION_KEY = "icaoId"

# Station identifiers are exactly four uppercase letters, matching the pattern
# `Airport.metar_station` already enforces on the reference data.
_STATION_PATTERN = r"^[A-Z]{4}$"


class WeatherPollingDetails(BaseModel):
    """One METAR-or-TAF query, validated before it can reach the wire."""

    settings: Settings
    stations: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description="Station identifiers, e.g. ('WSSS',). Sent as one request.",
    )

    @field_validator("stations", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> object:
        """Accept a bare string or any iterable, and normalise case.

        Deduplicated because the API answers a repeated id once, which would
        otherwise make the missing-station check below report a false positive.
        Order is preserved so logs read in the order the caller asked.
        """
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list | tuple):
            return value

        seen: dict[str, None] = {}
        for item in value:
            if isinstance(item, str) and (cleaned := item.strip().upper()):
                seen[cleaned] = None
        return tuple(seen)

    @field_validator("stations")
    @classmethod
    def _check_station_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject malformed ids here rather than letting them vanish upstream.

        This is not cosmetic. A station the API does not recognise is not an
        error there: a request for a bad id alone returns 204, and a request
        mixing good and bad ids returns 200 carrying only the good ones. Either
        way the typo is silently swallowed, so it has to be caught locally.
        """
        bad = [s for s in value if not re.match(_STATION_PATTERN, s)]
        if bad:
            raise ValueError(
                f"Malformed station id(s): {', '.join(bad)}. Expected four "
                f"uppercase letters, e.g. WSSS."
            )
        return value


def _poll_weather_once(
    http: httpx.Client,
    details: WeatherPollingDetails,
    endpoint: str,
) -> list[dict[str, Any]]:
    """Run one query, converting every non-answer into a typed failure.

    Returns:
        The records as received, one per station that had something to report.
        An empty list is a legitimate answer and is returned, not raised.

    Raises:
        WeatherUnreachable: the request produced no HTTP response.
        WeatherRequestFailed: the API answered with an error status.
        WeatherResponseInvalid: a success status carrying an unusable body.
    """
    url = f"{details.settings.weather_base_url.rstrip('/')}/{endpoint}"
    params = {"ids": ",".join(details.stations), "format": "json"}
    where = f"{endpoint} {','.join(details.stations)}"

    try:
        response = http.get(url, params=params)
    except httpx.HTTPError as exc:
        raise WeatherUnreachable(f"aviationweather.gov {where} never completed: {exc}") from exc

    if response.status_code >= 400:
        raise WeatherRequestFailed(
            f"aviationweather.gov {where} failed: HTTP {response.status_code}",
            status=response.status_code,
        )

    # 204 No Content when nothing at all matched -- an unknown id, or a station
    # that currently has no report. The body is genuinely empty, so calling
    # .json() on it raises; this is the branch that stops a poll loop dying on
    # a quiet station rather than on a real fault.
    if response.status_code == 204 or not response.content:
        records: list[dict[str, Any]] = []
    else:
        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherResponseInvalid(
                f"aviationweather.gov {where} returned HTTP {response.status_code} "
                f"with a non-JSON body: {exc}"
            ) from exc

        # Both endpoints return a list today, including for a single station.
        # A bare object is tolerated rather than trusted, since the shape is
        # undocumented and costs one branch to absorb.
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise WeatherResponseInvalid(
                f"aviationweather.gov {where} returned {type(payload).__name__}, "
                f"expected a list. The API contract may have changed."
            )
        records = [item for item in payload if isinstance(item, dict)]

    # The silent-failure guard. A request for WSSS,ZZZZ answers 200 with only
    # WSSS, so a typo or a decommissioned station shows up as quietly thinner
    # data rather than as an error. Named explicitly here so it is visible the
    # first time it happens instead of at model-training time.
    returned = {str(record.get(_STATION_KEY, "")).upper() for record in records}
    missing = [station for station in details.stations if station not in returned]

    if missing:
        logger.warning(
            "weather.poll.missing_stations",
            endpoint=endpoint,
            requested=list(details.stations),
            missing=missing,
            returned=len(records),
            http_status=response.status_code,
        )
    else:
        logger.debug(
            "weather.poll.completed",
            endpoint=endpoint,
            requested=list(details.stations),
            records=len(records),
        )

    return records


def poll_metar_once(http: httpx.Client, details: WeatherPollingDetails) -> list[dict[str, Any]]:
    """The latest METAR for each requested station.

    METARs are issued hourly, with off-cycle SPECIs when conditions change
    sharply, which is exactly the weather that drives delays. Polling faster
    than hourly is about catching those SPECIs promptly, not about the routine
    reports; repeated calls between issues return the same observation, so
    dedupe downstream on (station, `obsTime`).
    """
    return _poll_weather_once(http, details, "metar")


def poll_taf_once(http: httpx.Client, details: WeatherPollingDetails) -> list[dict[str, Any]]:
    """The current TAF for each requested station.

    TAFs are issued every six hours and forecast 24 to 30 hours ahead, so this
    is slow-moving data. Each record carries its forecast periods under
    `fcsts`, and the whole bulletin is superseded rather than amended in place:
    keep successive issues rather than overwriting, or the forecast that was
    actually visible at prediction time becomes unrecoverable.
    """
    return _poll_weather_once(http, details, "taf")


def weather_http_client(settings: Settings) -> httpx.Client:
    """An httpx client carrying the configured timeout.

    Explicit because httpx defaults to a 5s timeout while `http_timeout_seconds`
    is the value this project actually reasons about, and a client built without
    it would quietly ignore the setting.
    """
    return httpx.Client(timeout=settings.http_timeout_seconds)
