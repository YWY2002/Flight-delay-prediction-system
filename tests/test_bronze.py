"""Tests for the bronze Parquet writer.

Bronze is the layer that cannot be regenerated, so these focus on the properties
that protect it: partitioning is correct, writes are atomic, schemas are pinned,
and the content hash means what we claim it means.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from flight_delay.ingest.bronze import BronzeWriter, payload_hash

SCHEMA = pa.schema(
    [
        ("icao24", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("payload_hash", pa.string()),
        ("altitude_m", pa.float64()),
        ("sensors", pa.list_(pa.int64())),
    ]
)

WHEN = datetime(2026, 8, 7, 14, 32, 10, tzinfo=UTC)


def row(icao24: str = "3c6444", altitude: float | None = 9000.0) -> dict[str, Any]:
    return {
        "icao24": icao24,
        "ingested_at": WHEN,
        "payload_hash": payload_hash({"icao24": icao24, "altitude_m": altitude}),
        "altitude_m": altitude,
        "sensors": None,
    }


# ---- Partitioning ----------------------------------------------------------


def test_partition_path_is_hive_style(tmp_path: Path) -> None:
    """Hive-style so DuckDB can read the tree with hive_partitioning=true and
    recover date and hour as real columns."""
    writer = BronzeWriter(tmp_path)

    path = writer.partition_dir("opensky_states", WHEN)

    assert path == tmp_path / "opensky_states" / "date=2026-08-07" / "hour=14"


def test_partition_uses_hour_zero_padding(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    early = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)
    assert writer.partition_dir("s", early).name == "hour=03"


def test_write_creates_the_partition_directory(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)

    result = writer.write("opensky_states", [row()], SCHEMA, partition_time=WHEN)

    assert result.path is not None
    assert result.path.parent == writer.partition_dir("opensky_states", WHEN)
    assert result.path.exists()


def test_different_hours_land_in_different_partitions(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)

    first = writer.write("s", [row()], SCHEMA, partition_time=WHEN)
    later = writer.write("s", [row()], SCHEMA, partition_time=WHEN.replace(hour=15))

    assert first.path is not None and later.path is not None
    assert first.path.parent != later.path.parent


# ---- Append-only behaviour -------------------------------------------------


def test_repeated_writes_never_overwrite(tmp_path: Path) -> None:
    """Append-only is the core guarantee. Two writes in the same hour must
    produce two files, not one file overwritten."""
    writer = BronzeWriter(tmp_path)

    paths = {writer.write("s", [row()], SCHEMA, partition_time=WHEN).path for _ in range(20)}

    assert len(paths) == 20
    assert all(p is not None and p.exists() for p in paths)


def test_filenames_sort_chronologically(tmp_path: Path) -> None:
    """The timestamp prefix means a plain directory listing is in write order."""
    writer = BronzeWriter(tmp_path)

    early = writer.write("s", [row()], SCHEMA, partition_time=WHEN)
    late = writer.write("s", [row()], SCHEMA, partition_time=WHEN.replace(minute=59))

    assert early.path is not None and late.path is not None
    assert early.path.name < late.path.name


# ---- Empty batches ---------------------------------------------------------


def test_empty_batch_writes_nothing(tmp_path: Path) -> None:
    """A quiet bbox at 3am is normal. A zero-row file per cycle would litter the
    lake with thousands of useless files and slow every later scan."""
    writer = BronzeWriter(tmp_path)

    result = writer.write("s", [], SCHEMA, partition_time=WHEN)

    assert result.path is None
    assert result.rows == 0
    assert not (tmp_path / "s").exists()


# ---- Atomicity -------------------------------------------------------------


def test_no_temp_files_remain_after_a_successful_write(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)

    writer.write("s", [row()], SCHEMA, partition_time=WHEN)

    leftovers = list(writer.partition_dir("s", WHEN).glob(".*tmp"))
    assert leftovers == []


def test_failed_write_leaves_no_partial_file(tmp_path: Path) -> None:
    """A crash mid-write must not leave a truncated Parquet file behind: every
    later read of that partition would fail. This is what makes 'kill and
    restart loses at most one poll' actually true."""
    writer = BronzeWriter(tmp_path)
    bad_row = {**row(), "altitude_m": "not a number"}

    with pytest.raises(pa.ArrowInvalid):
        writer.write("s", [bad_row], SCHEMA, partition_time=WHEN)

    directory = writer.partition_dir("s", WHEN)
    if directory.exists():
        assert list(directory.iterdir()) == []


# ---- Schema enforcement ----------------------------------------------------


def test_schema_is_preserved_on_read(tmp_path: Path) -> None:
    """Pinned types, so files written weeks apart union cleanly."""
    writer = BronzeWriter(tmp_path)
    result = writer.write("s", [row()], SCHEMA, partition_time=WHEN)

    assert result.path is not None
    table = pq.read_table(result.path)

    assert table.schema.field("altitude_m").type == pa.float64()
    assert table.schema.field("ingested_at").type == pa.timestamp("us", tz="UTC")
    assert table.schema.field("sensors").type == pa.list_(pa.int64())


def test_all_null_column_keeps_its_declared_type(tmp_path: Path) -> None:
    """The exact drift the explicit schema exists to prevent. Inference on a
    batch of all-null values yields a null-typed column, which will not union
    with a later batch that has real values."""
    writer = BronzeWriter(tmp_path)
    result = writer.write("s", [row(altitude=None)], SCHEMA, partition_time=WHEN)

    assert result.path is not None
    assert pq.read_table(result.path).schema.field("altitude_m").type == pa.float64()


def test_round_trip_preserves_values(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)
    result = writer.write("s", [row("abc123", 1234.5)], SCHEMA, partition_time=WHEN)

    assert result.path is not None
    table = pq.read_table(result.path).to_pylist()

    assert table[0]["icao24"] == "abc123"
    assert table[0]["altitude_m"] == pytest.approx(1234.5)
    assert table[0]["ingested_at"] == WHEN


def test_result_reports_rows_and_size(tmp_path: Path) -> None:
    writer = BronzeWriter(tmp_path)

    result = writer.write("s", [row(), row("abc123")], SCHEMA, partition_time=WHEN)

    assert result.rows == 2
    assert result.bytes_written > 0


# ---- Content hashing (task 1.9) --------------------------------------------


def test_hash_is_deterministic() -> None:
    """Must be stable across processes and runs, or dedup silently stops
    working after a restart."""
    assert payload_hash({"a": 1, "b": "x"}) == payload_hash({"a": 1, "b": "x"})


def test_hash_ignores_key_order() -> None:
    """Canonical JSON: the digest depends on content, not dict ordering."""
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})


def test_hash_changes_with_content() -> None:
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_hash_distinguishes_null_from_missing() -> None:
    assert payload_hash({"a": None}) != payload_hash({})


def test_hash_handles_nested_and_datetime_values() -> None:
    payload = {"ts": datetime(2026, 8, 7, tzinfo=UTC), "nested": {"x": [1, 2]}}
    assert payload_hash(payload) == payload_hash(payload)


def test_identical_observations_hash_identically() -> None:
    """The property dedup depends on: the same aircraft state seen twice, for
    instance from the overlapping KJFK and KEWR boxes, is one observation."""
    observation = {"icao24": "3c6444", "last_contact": 1458564120, "lat": 40.6}

    assert payload_hash(observation) == payload_hash(dict(observation))
