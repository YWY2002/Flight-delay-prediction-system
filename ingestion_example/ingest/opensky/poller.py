"""OpenSky state vectors: fetch per airport, land them in bronze.

One structured log line per poll (plan task 1.10) carrying counts, latency, and
remaining credits, so ingestion health is answerable from the logs alone before
Prometheus exists in Phase 8.

Scheduling lives in `poller.py`, which runs this alongside the weather and FAA
sources on their own cadences.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa

from opensky_api import OpenSkyApi

from flight_delay.common.airports import Airport
from flight_delay.common.logging_config import get_logger
from flight_delay.ingest.bronze import BronzeWriter, payload_hash
from flight_delay.ingest.opensky.client import (
    OpenSkyApiError,
    OpenSkyClient,
    StatesResponse,
    StateVector,
)
from flight_delay.ingest.opensky.credit_budget import CreditBudgetExhausted

logger = get_logger(__name__)

SOURCE = "opensky_states"

# Explicit schema, pinned. Never inferred: a batch where every `sensors` value
# is null would infer a different type than one with values, and the two files
# would fail to union at query time, long after the cause is forgettable.
#
# Column order groups ingestion metadata first, then the observation itself.
STATE_VECTOR_SCHEMA = pa.schema(
    [
        # -- ingestion metadata (task 1.9) --
        ("poll_id", pa.string()),
        ("airport", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("payload_hash", pa.string()),
        ("response_time", pa.timestamp("us", tz="UTC")),
        # -- the observation --
        ("icao24", pa.string()),
        ("callsign", pa.string()),
        ("origin_country", pa.string()),
        ("time_position", pa.timestamp("us", tz="UTC")),
        ("last_contact", pa.timestamp("us", tz="UTC")),
        ("longitude", pa.float64()),
        ("latitude", pa.float64()),
        ("baro_altitude_m", pa.float64()),
        ("geo_altitude_m", pa.float64()),
        ("on_ground", pa.bool_()),
        ("velocity_ms", pa.float64()),
        ("true_track_deg", pa.float64()),
        ("vertical_rate_ms", pa.float64()),
        ("sensors", pa.list_(pa.int64())),
        ("squawk", pa.string()),
        ("spi", pa.bool_()),
        ("position_source", pa.int32()),
    ]
)


@dataclass(frozen=True)
class PollResult:
    """Outcome of polling one airport once."""

    poll_id: str
    airport: str
    aircraft: int
    rows_written: int
    duration_seconds: float
    credits_remaining: int | None
    path: Path | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def state_vector_to_row(
    state: StateVector,
    *,
    poll_id: str,
    airport: str,
    ingested_at: datetime,
    response_time: datetime,
) -> dict[str, object]:
    """Flatten one state vector into a bronze row with its metadata.

    The hash covers the observation only. `ingested_at` is excluded because it
    differs on every write and would make each record unique; `airport` is
    excluded because the KJFK and KEWR boxes overlap heavily, and the same
    aircraft returned by both polls is one observation seen twice, not two
    different facts. Both columns are still stored, just outside the identity.
    """
    observation = state.model_dump(mode="json")
    return {
        "poll_id": poll_id,
        "airport": airport,
        "ingested_at": ingested_at,
        "payload_hash": payload_hash(observation),
        "response_time": response_time,
        **state.model_dump(),
    }


def poll_airport_once(
    client: OpenSkyClient,
    airport: Airport,
    writer: BronzeWriter,
    *,
    radius_nm: float,
    poll_id: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PollResult:
    """Poll one airport and write the result to bronze.

    Never raises for expected failures. A poller that dies because one airport
    returned 503 is worse than one that skips a cycle: the other airports would
    lose their data too, and the accumulating history is the whole point.
    """
    poll_id = poll_id or uuid.uuid4().hex
    started = time.monotonic()
    bbox = airport.bounding_box(radius_nm)

    log = logger.bind(poll_id=poll_id, airport=airport.icao, source=SOURCE)

    try:
        response: StatesResponse = client.get_states(bbox)
    except CreditBudgetExhausted as exc:
        # Expected and self-correcting at UTC midnight. Warning, not error:
        # nothing is broken, we are simply out of allowance.
        duration = time.monotonic() - started
        log.warning(
            "poll.skipped_no_credits",
            duration_seconds=round(duration, 3),
            reason=str(exc),
        )
        return PollResult(
            poll_id=poll_id,
            airport=airport.icao,
            aircraft=0,
            rows_written=0,
            duration_seconds=duration,
            credits_remaining=0,
            path=None,
            error="credit_budget_exhausted",
        )
    except OpenSkyApiError as exc:
        # Already retried where retrying made sense. Reaching here means the
        # cycle is lost; the next one will try again.
        duration = time.monotonic() - started
        log.error(
            "poll.failed",
            duration_seconds=round(duration, 3),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return PollResult(
            poll_id=poll_id,
            airport=airport.icao,
            aircraft=0,
            rows_written=0,
            duration_seconds=duration,
            credits_remaining=None,
            path=None,
            error=type(exc).__name__,
        )

    ingested_at = clock()
    rows = [
        state_vector_to_row(
            state,
            poll_id=poll_id,
            airport=airport.icao,
            ingested_at=ingested_at,
            response_time=response.time,
        )
        for state in response.states
    ]

    result = writer.write(SOURCE, rows, STATE_VECTOR_SCHEMA, partition_time=ingested_at)
    duration = time.monotonic() - started

    # The one line per poll that task 1.10 asks for. Every value is a field, not
    # prose, so "median latency for KJFK yesterday" is a filter rather than a
    # regex against a message that someone will eventually reword.
    log.info(
        "poll.completed",
        aircraft=len(response.states),
        rows_written=result.rows,
        bytes_written=result.bytes_written,
        duration_seconds=round(duration, 3),
        credits_remaining=response.credits_remaining,
        path=str(result.path) if result.path else None,
    )

    return PollResult(
        poll_id=poll_id,
        airport=airport.icao,
        aircraft=len(response.states),
        rows_written=result.rows,
        duration_seconds=duration,
        credits_remaining=response.credits_remaining,
        path=result.path,
    )


def poll_all_once(
    client: OpenSkyClient,
    airports: Sequence[Airport],
    writer: BronzeWriter,
    *,
    radius_nm: float,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[PollResult]:
    """Poll every active airport once, sharing one poll id.

    One id across the cycle so the resulting rows can be correlated back to a
    single sweep, in the logs and in the data.
    """
    poll_id = uuid.uuid4().hex
    return [
        poll_airport_once(
            client,
            airport,
            writer,
            radius_nm=radius_nm,
            poll_id=poll_id,
            clock=clock,
        )
        for airport in airports
    ]
