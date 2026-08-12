"""OpenSky aircraft database: a static reference table keyed by icao24.

Unlike every other source here, this is not a poller. The database is a large
CSV snapshot that changes slowly, so it is downloaded on demand and written once
as a reference Parquet, then refreshed monthly.

It lands in `data/reference/`, not in bronze. Bronze is an append-only log of
observations, each true at a moment in time; this is a slowly-changing lookup
table that gets replaced wholesale. Mixing the two would mean either a bronze
partition that gets rewritten (breaking the append-only guarantee) or dozens of
near-identical snapshots to deduplicate at query time.

    uv run flight-delay-aircraft-db

**Deviation from plan task 1.7:** the plan says to derive and store
`aircraft_age = current_year - built`. This stores `built` and leaves age to be
computed where it is used. A stored age is wrong the moment the year turns, and
a reference table that is silently stale every January is worse than one that
requires a subtraction.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from flight_delay.common.config import Settings
from flight_delay.common.logging_config import get_logger
from flight_delay.ingest.errors import IngestError
from flight_delay.ingest.http import raise_for_status

logger = get_logger(__name__)

DEFAULT_DATABASE_URL = (
    "https://opensky-network.org/datasets/metadata/aircraft-database-complete-2026-01.csv"
)

AIRCRAFT_SCHEMA = pa.schema(
    [
        ("icao24", pa.string()),
        ("registration", pa.string()),
        ("typecode", pa.string()),
        ("model", pa.string()),
        ("manufacturer", pa.string()),
        ("operator", pa.string()),
        ("operator_icao", pa.string()),
        # Year of manufacture. Age is derived at use, never stored: see the
        # module docstring.
        ("built", pa.int32()),
    ]
)

# Source column -> our column. The upstream CSV carries far more columns than we
# need, and its header names have changed between releases, so several aliases
# are accepted per field.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "icao24": ("icao24",),
    "registration": ("registration", "reg"),
    "typecode": ("typecode", "icaoAircraftType", "icaoaircrafttype"),
    "model": ("model",),
    "manufacturer": ("manufacturername", "manufacturerName", "manufacturer"),
    "operator": ("operator",),
    "operator_icao": ("operatoricao", "operatorIcao"),
    "built": ("built", "firstflightdate", "firstFlightDate"),
}


class AircraftDatabaseError(IngestError):
    """The aircraft database could not be downloaded or parsed."""


def _clean(value: str | None) -> str | None:
    """Empty strings become None so nulls are consistent across the table."""
    if value is None:
        return None
    text = value.strip().strip("'\"").strip()
    return text or None


def _parse_year(value: str | None) -> int | None:
    """Extract a plausible year of manufacture.

    The column holds a bare year in some releases and a full date in others.
    Values outside a sane range are dropped rather than stored: an aircraft
    built in year 3 would quietly produce an age of two thousand and skew every
    fleet-age feature computed from it.
    """
    text = _clean(value)
    if not text:
        return None
    candidate = text[:4]
    try:
        year = int(candidate)
    except ValueError:
        return None
    if 1900 <= year <= 2100:
        return year
    return None


def _resolve_columns(fieldnames: Iterable[str]) -> dict[str, str]:
    """Map our column names onto whichever aliases this CSV release uses."""
    available = {name.strip().lower(): name for name in fieldnames if name}
    resolved: dict[str, str] = {}
    for target, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            actual = available.get(alias.lower())
            if actual is not None:
                resolved[target] = actual
                break
    return resolved


def parse_aircraft_csv(text: str) -> list[dict[str, Any]]:
    """Parse the aircraft database CSV into reference rows.

    Rows without a usable `icao24` are dropped: that column is the join key, and
    a row that cannot be joined to anything is dead weight. The count of dropped
    rows is logged, because a sudden jump means the upstream format changed.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise AircraftDatabaseError("Aircraft database CSV has no header row.")

    columns = _resolve_columns(reader.fieldnames)
    if "icao24" not in columns:
        raise AircraftDatabaseError(
            "Aircraft database CSV has no icao24 column; found "
            f"{sorted(reader.fieldnames)}. The upstream format may have changed."
        )

    rows: list[dict[str, Any]] = []
    dropped = 0

    for record in reader:
        rows_or_none = _row_from_record(record, columns)
        if rows_or_none is None:
            dropped += 1
            continue
        rows.append(rows_or_none)

    logger.info("aircraft_db.parsed", rows=len(rows), dropped=dropped)
    return rows


def _row_from_record(
    record: Mapping[str, str | None], columns: Mapping[str, str]
) -> dict[str, Any] | None:
    icao24 = _clean(record.get(columns["icao24"]))
    if not icao24:
        return None

    def value_for(target: str) -> str | None:
        source = columns.get(target)
        return _clean(record.get(source)) if source else None

    return {
        "icao24": icao24.lower(),
        "registration": value_for("registration"),
        "typecode": value_for("typecode"),
        "model": value_for("model"),
        "manufacturer": value_for("manufacturer"),
        "operator": value_for("operator"),
        "operator_icao": value_for("operator_icao"),
        "built": _parse_year(value_for("built")),
    }


def aircraft_age(built: int | None, *, as_of_year: int) -> int | None:
    """Age in years, computed at the point of use.

    Kept here beside the table it applies to, so the definition lives in one
    place even though the value is never stored.
    """
    if built is None:
        return None
    age = as_of_year - built
    return age if age >= 0 else None


def download_aircraft_database(url: str, *, timeout_seconds: float = 300.0) -> str:
    """Fetch the CSV. Deliberately thin so the parsing above stays testable.

    The timeout is generous because this file is large; the default 10 s used
    elsewhere would fail every time.
    """
    logger.info("aircraft_db.downloading", url=url)
    try:
        response = httpx.get(url, timeout=timeout_seconds, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise AircraftDatabaseError(f"Could not download the aircraft database: {exc}") from exc

    raise_for_status(response, source="OpenSky aircraft database")
    return response.text


def write_aircraft_reference(rows: list[dict[str, Any]], destination: Path) -> Path:
    """Write the reference Parquet, replacing any previous snapshot.

    Replacement rather than append: this is a lookup table, not a log. The write
    goes through a temp file and an atomic rename so a failure partway leaves the
    previous, still-valid snapshot in place rather than a truncated file.
    """
    if not rows:
        raise AircraftDatabaseError("Refusing to write an empty aircraft reference table.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=AIRCRAFT_SCHEMA)

    temp_path = destination.with_suffix(".parquet.tmp")
    try:
        pq.write_table(table, temp_path, compression="zstd")
        temp_path.replace(destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    logger.info(
        "aircraft_db.written",
        path=str(destination),
        rows=table.num_rows,
        bytes=destination.stat().st_size,
    )
    return destination


def refresh_aircraft_reference(settings: Settings) -> Path:
    """Download, parse, and write the reference table."""
    text = download_aircraft_database(settings.aircraft_database_url)
    rows = parse_aircraft_csv(text)
    return write_aircraft_reference(rows, settings.aircraft_reference_path)


def iter_rows(table: pa.Table) -> Iterator[dict[str, Any]]:
    """Convenience for reading the reference table back."""
    yield from table.to_pylist()


def main() -> None:
    """CLI entry point: `uv run flight-delay-aircraft-db`."""
    from flight_delay.common.config import get_settings
    from flight_delay.common.logging_config import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    refresh_aircraft_reference(settings)


if __name__ == "__main__":
    main()
