"""METAR and TAF: fetch for every active airport, land them in bronze.

Both reports come from the same client and the same station list, so they share
a module; they are separate poll functions because their cadences differ by an
order of magnitude (METAR ~10 min, TAF ~60 min).

Scheduling lives in `ingest/poller.py`, which runs these alongside the OpenSky
and FAA sources on their own cadences.
"""

from __future__ import annotations

import time
import uuid

from flight_delay.common.logging_config import get_logger
from flight_delay.ingest.context import SourceContext
from flight_delay.ingest.weather.bronze import (
    METAR_SCHEMA,
    METAR_SOURCE,
    TAF_SCHEMA,
    TAF_SOURCE,
    metar_to_row,
    taf_to_row,
)
from flight_delay.ingest.weather.client import WeatherClient

logger = get_logger(__name__)


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
