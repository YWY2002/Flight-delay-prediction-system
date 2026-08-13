"""FAA NAS status: fetch nationwide, keep the events for our airports.

Scheduling lives in `ingest/poller.py`, which runs this alongside the OpenSky
and weather sources on their own cadences.
"""

from __future__ import annotations

import time
import uuid

from flight_delay.common.logging_config import get_logger
from flight_delay.ingest.context import SourceContext
from flight_delay.ingest.faa.bronze import FAA_SCHEMA, FAA_SOURCE, faa_event_to_row
from flight_delay.ingest.faa.client import FaaClient, faa_events_for

logger = get_logger(__name__)


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
