"""Tests for the multi-source scheduler and the per-source poll functions."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest

from flight_delay.common.airports import Airport  # noqa: F401
from flight_delay.common.config import Settings
from flight_delay.ingest.bronze import BronzeWriter
from flight_delay.ingest.errors import TransientIngestError
from flight_delay.ingest.faa.bronze import FAA_SOURCE
from flight_delay.ingest.faa.client import FaaClient
from flight_delay.ingest.poller import (
    ScheduledSource,
    SourceContext,
    poll_faa_once,
    poll_metar_once,
    poll_taf_once,
    run_scheduler,
)
from flight_delay.ingest.weather.bronze import METAR_SOURCE, TAF_SOURCE
from flight_delay.ingest.weather.client import WeatherClient
from tests.samples import KEWR, KJFK, METAR_JSON, STATUS_XML, TAF_JSON

WHEN = datetime(2026, 8, 7, 14, 30, tzinfo=UTC)


def make_context(tmp_path: Path, *airports: Airport) -> SourceContext:
    return SourceContext(
        airports=list(airports) or [KJFK],
        writer=BronzeWriter(tmp_path),
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        clock=lambda: WHEN,
    )


def weather_client(payload: Any) -> WeatherClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return WeatherClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://wx.example.test/api/data",
    )


def faa_client(body: str = STATUS_XML) -> FaaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    return FaaClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://faa.example.test",
    )


# ---- METAR -----------------------------------------------------------------


def test_metar_poll_writes_bronze(tmp_path: Path) -> None:
    context = make_context(tmp_path)

    rows = poll_metar_once(weather_client([METAR_JSON]), context)

    assert rows == 1
    files = list((tmp_path / METAR_SOURCE).rglob("*.parquet"))
    assert len(files) == 1

    record = pq.read_table(files[0]).to_pylist()[0]
    assert record["station"] == "KJFK"
    assert record["visibility_sm"] == pytest.approx(10.0)
    assert record["ingested_at"] == WHEN
    assert [c["cover"] for c in record["clouds"]] == ["BKN", "OVC"]


def test_metar_ceiling_is_not_stored(tmp_path: Path) -> None:
    """Derived from `clouds`, so it belongs to whoever consumes it. A derived
    value in bronze cannot be recomputed if its definition changes."""
    context = make_context(tmp_path)
    poll_metar_once(weather_client([METAR_JSON]), context)

    table = pq.read_table(next((tmp_path / METAR_SOURCE).rglob("*.parquet")))
    assert "ceiling_ft" not in table.schema.names


def test_repeated_metar_polls_share_a_hash(tmp_path: Path) -> None:
    """METARs publish hourly but are polled every 10 minutes to catch SPECIs, so
    the same observation returns five or six times. Those repeats must collapse
    in silver."""
    context = make_context(tmp_path)
    client = weather_client([METAR_JSON])

    for _ in range(3):
        poll_metar_once(client, context)

    hashes = {
        r["payload_hash"]
        for f in (tmp_path / METAR_SOURCE).rglob("*.parquet")
        for r in pq.read_table(f).to_pylist()
    }
    assert len(hashes) == 1


def test_metar_requests_every_active_station(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[METAR_JSON])

    client = WeatherClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://wx.example.test/api/data",
    )
    poll_metar_once(client, make_context(tmp_path, KJFK, KEWR))

    assert seen[0].url.params["ids"] == "KJFK,KEWR"


# ---- TAF -------------------------------------------------------------------


def test_taf_poll_writes_nested_periods(tmp_path: Path) -> None:
    context = make_context(tmp_path)

    poll_taf_once(weather_client([TAF_JSON]), context)

    record = pq.read_table(next((tmp_path / TAF_SOURCE).rglob("*.parquet"))).to_pylist()[0]

    assert record["station"] == "KJFK"
    assert len(record["periods"]) == 1
    assert record["periods"][0]["wind_speed_kt"] == pytest.approx(10.0)
    assert record["raw_text"] is not None


# ---- FAA -------------------------------------------------------------------


def test_faa_poll_stores_only_tracked_airports(tmp_path: Path) -> None:
    """The endpoint is nationwide. KJFK and KEWR are tracked; ORD is not."""
    context = make_context(tmp_path, KJFK, KEWR)

    poll_faa_once(faa_client(), context)

    rows = pq.read_table(next((tmp_path / FAA_SOURCE).rglob("*.parquet"))).to_pylist()
    assert {r["airport"] for r in rows} == {"JFK", "EWR"}


def test_faa_event_types_are_normalised(tmp_path: Path) -> None:
    context = make_context(tmp_path, KJFK, KEWR)
    poll_faa_once(faa_client(), context)

    rows = pq.read_table(next((tmp_path / FAA_SOURCE).rglob("*.parquet"))).to_pylist()
    assert {r["event_type"] for r in rows} == {"ground_stop", "closure"}


def test_quiet_faa_period_writes_no_file(tmp_path: Path) -> None:
    """A clear day is normal and must not litter the lake with empty files."""
    empty = "<AIRPORT_STATUS_INFORMATION></AIRPORT_STATUS_INFORMATION>"
    context = make_context(tmp_path, KJFK)

    assert poll_faa_once(faa_client(empty), context) == 0
    assert not (tmp_path / FAA_SOURCE).exists()


def test_repeated_faa_polls_share_a_hash(tmp_path: Path) -> None:
    """A ground stop lasts hours but is polled every 5 minutes, so the same
    program is seen dozens of times. Duration is recoverable from first-seen and
    last-seen ingestion times rather than from dozens of duplicate rows."""
    context = make_context(tmp_path, KJFK, KEWR)
    client = faa_client()

    for _ in range(3):
        poll_faa_once(client, context)

    hashes = {
        r["payload_hash"]
        for f in (tmp_path / FAA_SOURCE).rglob("*.parquet")
        for r in pq.read_table(f).to_pylist()
    }
    assert len(hashes) == 2  # one ground stop, one closure


# ---- Scheduler -------------------------------------------------------------


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def counting_source(name: str, interval: float, counter: dict[str, int]) -> ScheduledSource:
    def poll() -> None:
        counter[name] = counter.get(name, 0) + 1

    return ScheduledSource(name=name, interval_seconds=interval, poll=poll)


def test_every_source_polls_immediately_on_start() -> None:
    """A restart must produce data at once, not sit silent for an hour while the
    TAF timer runs down."""
    counter: dict[str, int] = {}
    sources = [
        counting_source("fast", 90.0, counter),
        counting_source("slow", 3600.0, counter),
    ]
    stop = threading.Event()

    run_scheduler(sources, stop=stop, max_ticks=1, monotonic=FakeMonotonic())

    assert counter == {"fast": 1, "slow": 1}


def test_fast_source_is_not_held_back_by_a_slow_one() -> None:
    """The reason sources are scheduled independently: polling everything at the
    slowest cadence loses data, polling at the fastest burns credits."""
    counter: dict[str, int] = {}
    clock = FakeMonotonic()
    sources = [
        counting_source("fast", 10.0, counter),
        counting_source("slow", 1000.0, counter),
    ]
    stop = threading.Event()

    for _ in range(5):
        run_scheduler(sources, stop=stop, max_ticks=1, monotonic=clock)
        clock.advance(10.0)

    assert counter["fast"] == 5
    assert counter["slow"] == 1


def test_scheduler_stops_when_the_event_is_set() -> None:
    counter: dict[str, int] = {}
    sources = [counting_source("s", 10.0, counter)]
    stop = threading.Event()
    stop.set()

    assert run_scheduler(sources, stop=stop, monotonic=FakeMonotonic()) == 0
    assert counter == {}


def test_one_failing_source_does_not_stop_the_others(tmp_path: Path) -> None:
    """Ingestion history is on the critical path for training data, so a poller
    that dies because NOAA returned 502 costs far more than a skipped cycle."""
    from flight_delay.ingest.poller import _guard

    counter: dict[str, int] = {}
    good = counting_source("good", 10.0, counter)

    bad = ScheduledSource(name="bad", interval_seconds=10.0, poll=lambda: None)

    def explode() -> int:
        raise TransientIngestError("upstream is down")

    bad.poll = _guard("bad", explode, bad)

    run_scheduler([bad, good], stop=threading.Event(), max_ticks=1, monotonic=FakeMonotonic())

    assert counter == {"good": 1}
    assert bad.failures == 1


def test_unexpected_exceptions_are_also_contained() -> None:
    """Not just IngestError: an unforeseen bug in one parser must not stop the
    other three sources from collecting."""
    from flight_delay.ingest.poller import _guard

    source = ScheduledSource(name="bad", interval_seconds=10.0, poll=lambda: None)

    def explode() -> int:
        raise ZeroDivisionError("oops")

    source.poll = _guard("bad", explode, source)

    run_scheduler([source], stop=threading.Event(), max_ticks=1, monotonic=FakeMonotonic())

    assert source.failures == 1
    assert source.polls == 1
