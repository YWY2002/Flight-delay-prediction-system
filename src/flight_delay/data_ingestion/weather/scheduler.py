"""Scheduled polling of METAR observations and TAF forecasts.

Two tasks on two grids, both offset one minute past the publication boundary:

    METAR   every 30 min   :01 and :31 past the hour
    TAF     every  6 h     00:01, 06:01, 12:01, 18:01Z

The offset is the point of scheduling these on the wall clock at all. Both
products are published on fixed boundaries, and the bulletin does not appear on
aviationweather.gov at the instant it is stamped: it has to reach NOAA first.
Asking at exactly 18:00:00Z reliably returns the *previous* cycle, so every
poll would be one cycle stale while looking perfectly healthy. A minute of
slack costs a minute of latency and removes that whole class of silent
staleness. Widen it if you see the old cycle in the logs; a minute is the
smallest buffer that works, not a measured safety margin.

WSSS issues METAR on the half hour (`METAR WSSS 231000Z`, `230530Z`), which the
30 minute grid matches. Stations that report hourly simply return the same
observation twice, so dedupe downstream on (station, obsTime) rather than
assuming one row per poll.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import Any

import httpx

from flight_delay.common.logging_config import get_logger
from flight_delay.data_ingestion.scheduling import ScheduledTask, run_scheduler
from flight_delay.data_ingestion.weather.poller import (
    WeatherPollingDetails,
    poll_metar_once,
    poll_taf_once,
)

logger = get_logger(__name__)

METAR_INTERVAL_SECONDS = 1800.0  # 30 minutes
TAF_INTERVAL_SECONDS = 21600.0  # 6 hours

# One minute past the boundary, for the reason in the module docstring. Shared
# by both grids so they coincide exactly at 00:01/06:01/12:01/18:01Z and the
# scheduler serves them on a single wake-up.
PUBLICATION_BUFFER_SECONDS = 60.0

# A sink for raw records: whatever the endpoint returned, untouched.
RecordSink = Callable[[list[dict[str, Any]]], None]


def build_metar_task(
    http: httpx.Client,
    details: WeatherPollingDetails,
    on_records: RecordSink,
    *,
    interval_seconds: float = METAR_INTERVAL_SECONDS,
    offset_seconds: float = PUBLICATION_BUFFER_SECONDS,
) -> ScheduledTask:
    """A `ScheduledTask` fetching the latest METAR for every station in `details`."""

    def run() -> int:
        records = poll_metar_once(http, details)
        on_records(records)
        return len(records)

    return ScheduledTask(
        name="weather.metar",
        interval_seconds=interval_seconds,
        run=run,
        offset_seconds=offset_seconds,
    )


def build_taf_task(
    http: httpx.Client,
    details: WeatherPollingDetails,
    on_records: RecordSink,
    *,
    interval_seconds: float = TAF_INTERVAL_SECONDS,
    offset_seconds: float = PUBLICATION_BUFFER_SECONDS,
) -> ScheduledTask:
    """A `ScheduledTask` fetching the current TAF for every station in `details`."""

    def run() -> int:
        records = poll_taf_once(http, details)
        on_records(records)
        return len(records)

    return ScheduledTask(
        name="weather.taf",
        interval_seconds=interval_seconds,
        run=run,
        offset_seconds=offset_seconds,
    )


def run_weather_scheduler(
    http: httpx.Client,
    details: WeatherPollingDetails,
    on_metar: RecordSink,
    on_taf: RecordSink,
    *,
    metar_interval_seconds: float = METAR_INTERVAL_SECONDS,
    taf_interval_seconds: float = TAF_INTERVAL_SECONDS,
    buffer_seconds: float = PUBLICATION_BUFFER_SECONDS,
    max_ticks: int | None = None,
    stop: Event | None = None,
) -> None:
    """Poll METAR every 30 minutes and TAF every 6 hours until stopped.

    Separate sinks because the two are different records with different
    lifetimes. A METAR is a point observation and accumulates. A TAF is a
    bulletin that supersedes its predecessor wholesale rather than being amended
    in place, so keep successive issues instead of overwriting: otherwise the
    forecast that was actually visible at prediction time is unrecoverable, and
    a model trained on it silently learns from a forecast nobody could have had.

    Both tasks land on the same wake-up when their grids coincide, at
    00:01/06:01/12:01/18:01Z.
    """
    run_scheduler(
        [
            build_metar_task(
                http,
                details,
                on_metar,
                interval_seconds=metar_interval_seconds,
                offset_seconds=buffer_seconds,
            ),
            build_taf_task(
                http,
                details,
                on_taf,
                interval_seconds=taf_interval_seconds,
                offset_seconds=buffer_seconds,
            ),
        ],
        max_ticks=max_ticks,
        stop=stop,
    )
