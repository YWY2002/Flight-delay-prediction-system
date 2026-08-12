"""Bronze schema and row mapping for FAA NAS status events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pyarrow as pa

from flight_delay.ingest.bronze import payload_hash
from flight_delay.ingest.faa.client import FaaEvent

FAA_SOURCE = "faa_status"

FAA_SCHEMA = pa.schema(
    [
        ("poll_id", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("payload_hash", pa.string()),
        ("airport", pa.string()),
        ("event_type", pa.string()),
        ("reason", pa.string()),
        ("start_text", pa.string()),
        ("end_text", pa.string()),
        ("avg_delay_text", pa.string()),
        ("raw_xml", pa.string()),
    ]
)


def faa_event_to_row(event: FaaEvent, *, poll_id: str, ingested_at: datetime) -> dict[str, Any]:
    """Flatten an FAA event into a bronze row.

    Polled every 5 minutes while a ground stop can last hours, so the same
    program is seen dozens of times. `observed_at` is excluded from the hash
    along with `ingested_at`, so those repeats collapse to one event in silver
    and the duration can be recovered from first-seen and last-seen ingestion
    times.
    """
    identity = event.model_dump(mode="json", exclude={"observed_at"})
    return {
        "poll_id": poll_id,
        "ingested_at": ingested_at,
        "payload_hash": payload_hash(identity),
        "airport": event.airport,
        "event_type": event.event_type,
        "reason": event.reason,
        "start_text": event.start_text,
        "end_text": event.end_text,
        "avg_delay_text": event.avg_delay_text,
        "raw_xml": event.raw_xml,
    }
