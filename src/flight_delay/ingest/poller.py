"""Multi-source ingestion loop.

Each source has its own natural cadence, driven by how fast the underlying data
actually changes:

    OpenSky   every ~90 s   aircraft move continuously
    FAA       every 5 min   ground stops change on a minutes timescale
    METAR     every 10 min  published hourly, plus off-cycle SPECIs
    TAF       every 60 min  issued every 6 hours

Polling every source at the fastest cadence would burn OpenSky credits for
nothing and hammer NOAA for data that has not changed. So sources are scheduled
independently: each tracks its own next-due time, and the loop wakes on the
shortest outstanding interval.

Prefect replaces this in Phase 6. It exists so Phase 1 is runnable end to end.
"""

from __future__ import annotations

import signal
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import FrameType

from flight_delay.common.airports import Airport, load_airports, resolve_active_airports
from flight_delay.common.config import Settings, get_settings
from flight_delay.common.logging_config import configure_logging, get_logger
from flight_delay.common.timeutil import utc_now
from flight_delay.ingest import faa_client, opensky_client, weather_client
from flight_delay.ingest.bronze import BronzeWriter
from flight_delay.ingest.errors import IngestError
from flight_delay.ingest.faa_bronze import FAA_SCHEMA, FAA_SOURCE, faa_event_to_row
from flight_delay.ingest.faa_client import FaaClient, faa_events_for
from flight_delay.ingest.opensky_poller import poll_all_once
from flight_delay.ingest.weather_bronze import (
    METAR_SCHEMA,
    METAR_SOURCE,
    TAF_SCHEMA,
    TAF_SOURCE,
    metar_to_row,
    taf_to_row,
)
from flight_delay.ingest.weather_client import WeatherClient

logger = get_logger(__name__)


@dataclass
class ScheduledSource:
    """One source and when it is next due.

    Due times are tracked on the MONOTONIC clock: this is "has enough time
    elapsed", not "what time is it", so an NTP correction must not make a source
    look overdue by an hour or suppress it for one.
    """

    name: str
    interval_seconds: float
    poll: Callable[[], None]
    next_due: float = 0.0
    failures: int = 0
    polls: int = 0

    def is_due(self, now: float) -> bool:
        return now >= self.next_due

    def schedule_next(self, now: float) -> None:
        self.next_due = now + self.interval_seconds


@dataclass
class SourceContext:
    """Everything the per-source poll functions need."""

    airports: Sequence[Airport]
    writer: BronzeWriter
    settings: Settings
    clock: Callable[[], datetime] = utc_now
    results: dict[str, int] = field(default_factory=dict)


def poll_metar_once(
    client: WeatherClient, context: SourceContext, *, poll_id: str | None = None
) -> int:
    """Fetch and store the latest METAR for every active airport."""
    poll_id = poll_id or uuid.uuid4().hex
    stations = [airport.metar_station for airport in context.airports]
    started = time.monotonic()

    observations = client.get_metar(stations)
    ingested_at = context.clock()
    rows = [metar_to_row(m, poll_id=poll_id, ingested_at=ingested_at) for m in observations]
    result = context.writer.write(METAR_SOURCE, rows, METAR_SCHEMA, partition_time=ingested_at)

    logger.info(
        "poll.completed",
        source=METAR_SOURCE,
        poll_id=poll_id,
        stations=len(stations),
        observations=len(observations),
        rows_written=result.rows,
        bytes_written=result.bytes_written,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    return result.rows


def poll_taf_once(
    client: WeatherClient, context: SourceContext, *, poll_id: str | None = None
) -> int:
    """Fetch and store the current TAF for every active airport."""
    poll_id = poll_id or uuid.uuid4().hex
    stations = [airport.metar_station for airport in context.airports]
    started = time.monotonic()

    forecasts = client.get_taf(stations)
    ingested_at = context.clock()
    rows = [taf_to_row(t, poll_id=poll_id, ingested_at=ingested_at) for t in forecasts]
    result = context.writer.write(TAF_SOURCE, rows, TAF_SCHEMA, partition_time=ingested_at)

    logger.info(
        "poll.completed",
        source=TAF_SOURCE,
        poll_id=poll_id,
        stations=len(stations),
        forecasts=len(forecasts),
        rows_written=result.rows,
        bytes_written=result.bytes_written,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    return result.rows


def poll_faa_once(client: FaaClient, context: SourceContext, *, poll_id: str | None = None) -> int:
    """Fetch nationwide FAA status and store the events for our airports.

    The endpoint has no airport filter, so everything is fetched and filtered
    here. `nationwide` is logged alongside `matched` because the ratio is a
    useful sanity check: matched consistently zero while nationwide is large
    suggests the faa_code mapping is wrong, not that the skies are calm.
    """
    poll_id = poll_id or uuid.uuid4().hex
    started = time.monotonic()

    all_events = client.get_status()
    codes = {airport.faa_code for airport in context.airports}
    events = faa_events_for(all_events, codes)

    ingested_at = context.clock()
    rows = [faa_event_to_row(e, poll_id=poll_id, ingested_at=ingested_at) for e in events]
    result = context.writer.write(FAA_SOURCE, rows, FAA_SCHEMA, partition_time=ingested_at)

    logger.info(
        "poll.completed",
        source=FAA_SOURCE,
        poll_id=poll_id,
        nationwide=len(all_events),
        matched=len(events),
        rows_written=result.rows,
        bytes_written=result.bytes_written,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    return result.rows


def _guard(name: str, poll: Callable[[], int], source: ScheduledSource) -> Callable[[], None]:
    """Wrap a poll so one source's failure cannot take down the loop.

    Ingestion history is on the critical path for training data, so a poller
    that dies because NOAA returned a 502 costs far more than a skipped cycle.
    Unexpected exceptions are caught too, not just IngestError: an unforeseen
    bug in one parser must not stop the other three sources from collecting.
    """

    def run() -> None:
        source.polls += 1
        try:
            poll()
        except IngestError as exc:
            source.failures += 1
            logger.error("poll.failed", source=name, error=str(exc), error_type=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - deliberate: keep the loop alive
            source.failures += 1
            logger.exception(
                "poll.unexpected_error",
                source=name,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    return run


def build_sources(
    context: SourceContext,
    *,
    opensky: opensky_client.OpenSkyClient,
    weather: WeatherClient,
    faa: FaaClient,
) -> list[ScheduledSource]:
    """Assemble the schedule from configured intervals."""
    settings = context.settings
    sources: list[ScheduledSource] = []

    def add(name: str, interval: float, poll: Callable[[], int]) -> None:
        source = ScheduledSource(name=name, interval_seconds=interval, poll=lambda: None)
        source.poll = _guard(name, poll, source)
        sources.append(source)

    add(
        "opensky_states",
        settings.opensky_poll_seconds,
        lambda: sum(
            r.rows_written
            for r in poll_all_once(
                opensky,
                context.airports,
                context.writer,
                radius_nm=settings.bbox_radius_nm,
                clock=context.clock,
            )
        ),
    )
    add(METAR_SOURCE, settings.metar_poll_seconds, lambda: poll_metar_once(weather, context))
    add(TAF_SOURCE, settings.taf_poll_seconds, lambda: poll_taf_once(weather, context))
    add(FAA_SOURCE, settings.faa_poll_seconds, lambda: poll_faa_once(faa, context))
    return sources


def run_scheduler(
    sources: list[ScheduledSource],
    *,
    stop: threading.Event,
    max_ticks: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Run due sources until stopped. Returns the number of ticks executed.

    Every source is polled once at startup rather than waiting out its first
    interval, so a restart produces data immediately instead of a silent hour
    while the TAF timer runs down.
    """
    ticks = 0
    while not stop.is_set():
        now = monotonic()
        for source in sources:
            if source.is_due(now):
                source.poll()
                source.schedule_next(monotonic())

        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break

        # Sleep only until the earliest next due time, so a fast source is not
        # delayed by a slow one's interval.
        sleep_for = max(0.0, min(s.next_due for s in sources) - monotonic())
        stop.wait(sleep_for)

    return ticks


def run_poller(
    settings: Settings | None = None,
    *,
    stop_event: threading.Event | None = None,
    max_ticks: int | None = None,
) -> None:
    """Wire up every source and run the loop."""
    settings = settings or get_settings()
    stop = stop_event or threading.Event()

    reference = load_airports(settings.airports_file)
    airports = resolve_active_airports(settings.airports, reference)
    context = SourceContext(
        airports=airports, writer=BronzeWriter(settings.bronze_dir), settings=settings
    )

    logger.info(
        "poller.starting",
        airports=[a.icao for a in airports],
        bronze_dir=str(settings.bronze_dir),
        intervals={
            "opensky_states": settings.opensky_poll_seconds,
            METAR_SOURCE: settings.metar_poll_seconds,
            TAF_SOURCE: settings.taf_poll_seconds,
            FAA_SOURCE: settings.faa_poll_seconds,
        },
    )

    with (
        opensky_client.client_from_settings(settings) as opensky,
        weather_client.client_from_settings(settings) as weather,
        faa_client.client_from_settings(settings) as faa,
    ):
        sources = build_sources(context, opensky=opensky, weather=weather, faa=faa)
        ticks = run_scheduler(sources, stop=stop, max_ticks=max_ticks)

    logger.info(
        "poller.stopped",
        ticks=ticks,
        polls={s.name: s.polls for s in sources},
        failures={s.name: s.failures for s in sources},
    )


def main() -> None:
    """CLI entry point: `uv run flight-delay-ingest`."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    stop = threading.Event()

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        # Set a flag rather than raising, so an in-flight Parquet write finishes
        # and the loop exits between polls instead of mid-write.
        logger.info("poller.shutdown_requested", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    run_poller(settings, stop_event=stop)


if __name__ == "__main__":
    main()
