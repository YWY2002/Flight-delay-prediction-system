"""Ingestion entry point: the composition root.

This is the only module that knows about both ends of the pipeline. Pollers know
how to fetch, the writer knows how to persist, and the schedulers know when to
act; none of them import each other. The sinks below are what join them, and
they live here because a sink needs a `BronzeWriter` *instance*, which cannot
exist until settings are loaded and a root directory is chosen. That is a
runtime decision, so it belongs at the entry point rather than in a module that
merely defines the class.
"""

from __future__ import annotations

import signal
from datetime import UTC, datetime
from threading import Event
from types import FrameType
from typing import Any

from opensky_api import OpenSkyStates, TokenManager

from flight_delay.common.config import Settings, get_settings
from flight_delay.common.logging_config import configure_logging, get_logger
from flight_delay.common.timeutil import utc_now
from flight_delay.data_ingestion.opensky.client import TrackedOpenSkyApi
from flight_delay.data_ingestion.opensky.scheduler import build_states_task, wsss_bounding_box
from flight_delay.data_ingestion.scheduling import ScheduledTask, run_scheduler
from flight_delay.data_ingestion.weather.poller import WeatherPollingDetails, weather_http_client
from flight_delay.data_ingestion.weather.scheduler import build_metar_task, build_taf_task
from flight_delay.data_ingestion.writer import (
    METAR_SCHEMA,
    METAR_SOURCE,
    OPENSKY_SOURCE,
    STATE_VECTOR_SCHEMA,
    TAF_SCHEMA,
    TAF_SOURCE,
    BronzeWriter,
    state_vectors_to_rows,
    weather_records_to_rows,
)

logger = get_logger(__name__)


def build_tasks(settings: Settings, writer: BronzeWriter) -> list[ScheduledTask]:
    """Wire pollers to the writer and return the tasks to schedule.

    The sinks are closures over `writer` rather than free functions taking one,
    because the scheduler calls them with exactly one argument: the payload. A
    sink that also expects `settings` will type-check fine and then raise
    TypeError on the first tick, which is a poor place to find out.
    """

    def on_snapshot(snapshot: OpenSkyStates) -> None:
        writer.write(
            OPENSKY_SOURCE,
            state_vectors_to_rows(snapshot),
            datetime.fromtimestamp(snapshot.time, UTC),
            schema=STATE_VECTOR_SCHEMA,
        )

    # Both weather sinks pass a schema for the same reason OpenSky does: without
    # one, `visib` infers as double while conditions are poor and as string once
    # they clear ("10+"), and the append fails partway through the day.
    def on_metar(records: list[dict[str, Any]]) -> None:
        writer.write(
            METAR_SOURCE,
            weather_records_to_rows(records, METAR_SCHEMA),
            utc_now(),
            schema=METAR_SCHEMA,
        )

    def on_taf(records: list[dict[str, Any]]) -> None:
        writer.write(
            TAF_SOURCE,
            weather_records_to_rows(records, TAF_SCHEMA),
            utc_now(),
            schema=TAF_SCHEMA,
        )

    client = TrackedOpenSkyApi(token_manager=TokenManager(*settings.require_opensky_credentials()))
    http = weather_http_client(settings)
    stations = WeatherPollingDetails(settings=settings, stations=settings.airports)

    return [
        build_states_task(client, wsss_bounding_box(settings), on_snapshot),
        build_metar_task(http, stations, on_metar),
        build_taf_task(http, stations, on_taf),
    ]


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    writer = BronzeWriter(settings.bronze_dir)
    tasks = build_tasks(settings, writer)

    # One `run_scheduler` for all three, NOT one call per source. Each
    # `run_*_scheduler` helper blocks forever, so calling them in sequence would
    # run OpenSky and never reach the weather tasks. Sharing one loop is also
    # what lets METAR and TAF land on the same wake-up at 18:01Z.
    stop = Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("ingest.shutdown.requested", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("ingest.starting", bronze_dir=str(settings.bronze_dir), airports=settings.airports)
    run_scheduler(tasks, stop=stop)
    logger.info("ingest.stopped")


if __name__ == "__main__":
    main()
