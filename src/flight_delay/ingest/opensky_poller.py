"""OpenSky polling loop: fetch state vectors per airport, land them in bronze.

One structured log line per poll (plan task 1.10) carrying counts, latency, and
remaining credits, so ingestion health is answerable from the logs alone before
Prometheus exists in Phase 8.

The loop here is deliberately minimal. Prefect takes over scheduling in Phase 6;
this exists so Phase 1 has a runnable entry point and the bronze layer can be
exercised for real.
"""

from __future__ import annotations

import signal
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import pyarrow as pa

from flight_delay.common.airports import Airport, load_airports, resolve_active_airports
from flight_delay.common.config import Settings, get_settings
from flight_delay.common.logging_config import configure_logging, get_logger
from flight_delay.ingest.bronze import BronzeWriter, payload_hash
from flight_delay.ingest.credit_budget import CreditBudgetExhausted
from flight_delay.ingest.opensky_client import (
    OpenSkyApiError,
    OpenSkyClient,
    StatesResponse,
    StateVector,
    client_from_settings,
)

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


def run_poller(
    settings: Settings | None = None,
    *,
    stop_event: threading.Event | None = None,
    max_cycles: int | None = None,
    interval_seconds: float | None = None,
) -> None:
    """Poll on a fixed cadence until stopped.

    Args:
        stop_event: Set to request shutdown. Waited on rather than slept
            through, so Ctrl-C is honoured immediately instead of after the
            remaining interval.
        max_cycles: Stop after this many cycles. For tests and smoke runs.
        interval_seconds: Override the configured cadence. An explicit argument
            rather than config, so the 60 s credit-protection floor enforced in
            `Settings` still applies to every real run while tests can drive
            many cycles instantly.
    """
    settings = settings or get_settings()
    stop = stop_event or threading.Event()
    interval = interval_seconds if interval_seconds is not None else settings.opensky_poll_seconds

    reference = load_airports(settings.airports_file)
    airports = resolve_active_airports(settings.airports, reference)
    writer = BronzeWriter(settings.bronze_dir)

    logger.info(
        "poller.starting",
        airports=[a.icao for a in airports],
        interval_seconds=interval,
        bbox_radius_nm=settings.bbox_radius_nm,
        daily_credits=settings.opensky_daily_credits,
        bronze_dir=str(settings.bronze_dir),
    )

    cycles = 0
    with client_from_settings(settings) as client:
        while not stop.is_set():
            cycle_started = time.monotonic()
            poll_all_once(
                client,
                airports,
                writer,
                radius_nm=settings.bbox_radius_nm,
            )
            cycles += 1

            if max_cycles is not None and cycles >= max_cycles:
                break

            # Subtract the work from the interval so cadence stays constant
            # rather than drifting by however long each cycle took. A slow cycle
            # shortens the wait; it never pushes the next poll later and later.
            elapsed = time.monotonic() - cycle_started
            remaining = max(0.0, interval - elapsed)
            if remaining == 0.0 and interval > 0:
                logger.warning(
                    "poller.cycle_overran",
                    elapsed_seconds=round(elapsed, 3),
                    interval_seconds=interval,
                )
            stop.wait(remaining)

    logger.info("poller.stopped", cycles=cycles)


def main() -> None:
    """CLI entry point: `uv run flight-delay-ingest`."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    stop = threading.Event()

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        # Set a flag rather than raising: the current write finishes and the
        # loop exits cleanly, instead of tearing down mid-Parquet-write.
        logger.info("poller.shutdown_requested", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    run_poller(settings, stop_event=stop)


if __name__ == "__main__":
    main()
