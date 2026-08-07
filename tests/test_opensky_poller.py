"""Tests for the OpenSky polling loop and its bronze output.

End to end within the process: a mock HTTP transport, a real bronze writer
against tmp_path, and real Parquet files read back. The only thing faked is the
network.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest

from flight_delay.common.airports import Airport
from flight_delay.common.config import Settings
from flight_delay.ingest import opensky_poller
from flight_delay.ingest.bronze import BronzeWriter
from flight_delay.ingest.credit_budget import CreditBudget
from flight_delay.ingest.opensky_client import OpenSkyClient, StateVector
from flight_delay.ingest.opensky_poller import (
    SOURCE,
    STATE_VECTOR_SCHEMA,
    poll_airport_once,
    poll_all_once,
    run_poller,
    state_vector_to_row,
)


def make_settings(**overrides: Any) -> Settings:
    """Build Settings ignoring any ambient .env file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


KJFK = Airport(icao="KJFK", name="JFK", lat=40.6398, lon=-73.7789, metar_station="KJFK")
KEWR = Airport(icao="KEWR", name="Newark", lat=40.6925, lon=-74.1687, metar_station="KEWR")

RAW_STATE: list[Any] = [
    "3c6444", "DLH9LF  ", "Germany", 1458564120, 1458564120, -73.78, 40.64,
    9639.3, False, 232.88, 98.26, 4.55, None, 9547.86, "1000", False, 0,
]  # fmt: skip

WHEN = datetime(2026, 8, 7, 14, 32, 10, tzinfo=UTC)


def payload(*states: list[Any]) -> dict[str, Any]:
    return {"time": 1458564121, "states": list(states)}


def make_client(
    handler: Any,
    *,
    budget: CreditBudget | None = None,
) -> OpenSkyClient:
    return OpenSkyClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://api.example.test",
        budget=budget,
    )


def ok_handler(*states: list[Any], credits: str | None = None) -> Any:
    headers = {"X-Rate-Limit-Remaining": credits} if credits else {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload(*states), headers=headers)

    return handler


# ---- Row construction ------------------------------------------------------


def test_row_carries_all_metadata_columns() -> None:
    state = StateVector.model_validate(RAW_STATE)

    result = state_vector_to_row(
        state,
        poll_id="poll-1",
        airport="KJFK",
        ingested_at=WHEN,
        response_time=WHEN,
    )

    assert result["poll_id"] == "poll-1"
    assert result["airport"] == "KJFK"
    assert result["ingested_at"] == WHEN
    assert isinstance(result["payload_hash"], str)
    assert result["icao24"] == "3c6444"


def test_row_keys_match_the_schema_exactly() -> None:
    """Guards against a field added to StateVector without a matching column,
    which would fail at write time rather than here."""
    state = StateVector.model_validate(RAW_STATE)
    result = state_vector_to_row(
        state, poll_id="p", airport="KJFK", ingested_at=WHEN, response_time=WHEN
    )

    assert set(result) == set(STATE_VECTOR_SCHEMA.names)


def test_hash_excludes_ingestion_time() -> None:
    """Critical: ingested_at differs on every write by construction. Including
    it would make every record unique and silently defeat dedup entirely."""
    state = StateVector.model_validate(RAW_STATE)

    first = state_vector_to_row(
        state, poll_id="p1", airport="KJFK", ingested_at=WHEN, response_time=WHEN
    )
    later = state_vector_to_row(
        state,
        poll_id="p2",
        airport="KJFK",
        ingested_at=WHEN.replace(hour=20),
        response_time=WHEN,
    )

    assert first["payload_hash"] == later["payload_hash"]


def test_hash_excludes_the_observing_airport() -> None:
    """The KJFK and KEWR boxes overlap heavily (airports ~18 nm apart, boxes
    reach 60 nm), so the same aircraft is genuinely returned by both polls.
    Those rows describe one observation and must hash identically so silver can
    collapse them."""
    state = StateVector.model_validate(RAW_STATE)

    from_jfk = state_vector_to_row(
        state, poll_id="p", airport="KJFK", ingested_at=WHEN, response_time=WHEN
    )
    from_ewr = state_vector_to_row(
        state, poll_id="p", airport="KEWR", ingested_at=WHEN, response_time=WHEN
    )

    assert from_jfk["payload_hash"] == from_ewr["payload_hash"]
    assert from_jfk["airport"] != from_ewr["airport"]


def test_hash_differs_for_a_different_observation() -> None:
    state = StateVector.model_validate(RAW_STATE)
    moved_raw = list(RAW_STATE)
    moved_raw[7] = 5000.0  # different altitude
    moved = StateVector.model_validate(moved_raw)

    a = state_vector_to_row(
        state, poll_id="p", airport="KJFK", ingested_at=WHEN, response_time=WHEN
    )
    b = state_vector_to_row(
        moved, poll_id="p", airport="KJFK", ingested_at=WHEN, response_time=WHEN
    )

    assert a["payload_hash"] != b["payload_hash"]


# ---- Polling one airport ---------------------------------------------------


def test_poll_writes_rows_to_bronze(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler(RAW_STATE))

    result = poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)

    assert result.ok
    assert result.aircraft == 1
    assert result.rows_written == 1
    assert result.path is not None and result.path.exists()


def test_written_file_is_readable_and_correct(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler(RAW_STATE))

    result = poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)

    assert result.path is not None
    rows = pq.read_table(result.path).to_pylist()

    assert len(rows) == 1
    assert rows[0]["icao24"] == "3c6444"
    assert rows[0]["callsign"] == "DLH9LF"
    assert rows[0]["airport"] == "KJFK"
    assert rows[0]["ingested_at"] == WHEN


def test_poll_lands_in_the_ingestion_time_partition(tmp_path: Path) -> None:
    """Partitioned by when we learned it, not when it happened. The observation
    is from 2016; the partition is today."""
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler(RAW_STATE))

    result = poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)

    assert result.path is not None
    assert "date=2026-08-07" in str(result.path)
    assert "hour=14" in str(result.path)


def test_empty_poll_writes_no_file(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler())

    result = poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)

    assert result.ok
    assert result.aircraft == 0
    assert result.path is None


def test_credits_remaining_is_reported(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler(RAW_STATE, credits="3991"))

    result = poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)

    assert result.credits_remaining == 3991


# ---- Failure isolation -----------------------------------------------------


def test_api_failure_does_not_raise(tmp_path: Path) -> None:
    """A poller that dies because one airport returned 503 is worse than one
    that skips a cycle: the other airports lose their data too."""
    writer = BronzeWriter(tmp_path)

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    result = poll_airport_once(
        make_client(failing), KJFK, writer, radius_nm=60.0, clock=lambda: WHEN
    )

    assert not result.ok
    assert result.error == "OpenSkyServerError"
    assert result.rows_written == 0


def test_credit_exhaustion_is_reported_not_raised(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    budget = CreditBudget(1, clock=lambda: WHEN)
    client = make_client(ok_handler(RAW_STATE), budget=budget)

    poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)
    second = poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)

    assert not second.ok
    assert second.error == "credit_budget_exhausted"


def test_one_failing_airport_does_not_stop_the_others(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    calls = 0

    def first_call_fails(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=payload(RAW_STATE))

    results = poll_all_once(
        make_client(first_call_fails),
        [KJFK, KEWR],
        writer,
        radius_nm=60.0,
        clock=lambda: WHEN,
    )

    assert len(results) == 2
    assert [r.ok for r in results] == [False, True]
    assert results[1].rows_written == 1


# ---- Cycle-level behaviour -------------------------------------------------


def test_all_airports_share_one_poll_id(tmp_path: Path) -> None:
    """One id per sweep, so rows and log lines can be correlated back to it."""
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler(RAW_STATE))

    results = poll_all_once(client, [KJFK, KEWR], writer, radius_nm=60.0, clock=lambda: WHEN)

    assert len({r.poll_id for r in results}) == 1


def test_poll_ids_differ_between_cycles(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler(RAW_STATE))

    first = poll_all_once(client, [KJFK], writer, radius_nm=60.0, clock=lambda: WHEN)
    second = poll_all_once(client, [KJFK], writer, radius_nm=60.0, clock=lambda: WHEN)

    assert first[0].poll_id != second[0].poll_id


def test_repeated_cycles_append_rather_than_overwrite(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler(RAW_STATE))

    for _ in range(5):
        poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)

    files = list((tmp_path / SOURCE).rglob("*.parquet"))
    assert len(files) == 5

    total = sum(pq.read_table(f).num_rows for f in files)
    assert total == 5


def test_duplicate_observations_across_cycles_share_a_hash(tmp_path: Path) -> None:
    """The dedup signal silver will rely on: an aircraft whose state has not
    changed between polls produces identical hashes."""
    writer = BronzeWriter(tmp_path)
    client = make_client(ok_handler(RAW_STATE))

    for _ in range(3):
        poll_airport_once(client, KJFK, writer, radius_nm=60.0, clock=lambda: WHEN)

    hashes = {
        r["payload_hash"]
        for f in (tmp_path / SOURCE).rglob("*.parquet")
        for r in pq.read_table(f).to_pylist()
    }
    assert len(hashes) == 1


# ---- The loop --------------------------------------------------------------


def patch_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(
        opensky_poller,
        "client_from_settings",
        lambda settings: make_client(handler),
    )


def test_run_poller_stops_after_max_cycles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`interval_seconds=0` keeps this instant. The 60 s credit floor still
    applies to real runs; it lives in Settings, which this bypasses on purpose
    rather than weakening."""
    patch_client(monkeypatch, ok_handler(RAW_STATE))
    settings = make_settings(airports="KJFK", data_dir=tmp_path)

    run_poller(settings, max_cycles=3, interval_seconds=0.0)

    files = list((tmp_path / "bronze" / SOURCE).rglob("*.parquet"))
    assert len(files) == 3


def test_run_poller_writes_under_the_configured_bronze_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, ok_handler(RAW_STATE))
    settings = make_settings(airports="KJFK", data_dir=tmp_path)

    run_poller(settings, max_cycles=1, interval_seconds=0.0)

    assert (tmp_path / "bronze" / SOURCE).exists()


def test_run_poller_polls_every_active_airport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, ok_handler(RAW_STATE))
    settings = make_settings(airports="KJFK,KEWR", data_dir=tmp_path)

    run_poller(settings, max_cycles=1, interval_seconds=0.0)

    airports = {
        r["airport"]
        for f in (tmp_path / "bronze" / SOURCE).rglob("*.parquet")
        for r in pq.read_table(f).to_pylist()
    }
    assert airports == {"KJFK", "KEWR"}


def test_run_poller_survives_a_failing_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop must keep running through outages. Ingestion history is on the
    critical path for training data, so a dead poller is expensive."""

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    patch_client(monkeypatch, failing)
    settings = make_settings(airports="KJFK", data_dir=tmp_path)

    run_poller(settings, max_cycles=2, interval_seconds=0.0)

    assert not (tmp_path / "bronze" / SOURCE).exists()


def test_run_poller_honours_a_preset_stop_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown is checked before work starts, so SIGTERM during startup does
    not still cost a poll."""
    patch_client(monkeypatch, ok_handler(RAW_STATE))
    settings = make_settings(airports="KJFK", data_dir=tmp_path)

    stop = threading.Event()
    stop.set()

    run_poller(settings, stop_event=stop, interval_seconds=0.0)

    assert not (tmp_path / "bronze").exists()

    assert not (tmp_path / "bronze").exists()
