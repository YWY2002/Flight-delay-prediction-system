"""Tests for the aircraft reference table.

The download is a thin wrapper; the parsing and writing are what carry risk, so
those are what is tested.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from flight_delay.ingest.opensky.aircraft_metadata import (
    AIRCRAFT_SCHEMA,
    AircraftDatabaseError,
    aircraft_age,
    parse_aircraft_csv,
    write_aircraft_reference,
)

CSV = """icao24,registration,manufacturername,model,typecode,operator,operatoricao,built
3c6444,D-AIBL,Airbus,A319 111,A319,Lufthansa,DLH,2011
a1b2c3,N123UA,Boeing,737-824,B738,United Airlines,UAL,1998
abc999,N456JB,Airbus,A320 232,A320,JetBlue,JBU,2005
"""


def test_parses_all_columns() -> None:
    rows = parse_aircraft_csv(CSV)

    assert len(rows) == 3
    first = rows[0]
    assert first["icao24"] == "3c6444"
    assert first["registration"] == "D-AIBL"
    assert first["manufacturer"] == "Airbus"
    assert first["typecode"] == "A319"
    assert first["operator_icao"] == "DLH"
    assert first["built"] == 2011


def test_icao24_is_lowercased() -> None:
    """OpenSky state vectors report icao24 in lowercase hex. The join key must
    match on both sides or every lookup silently misses."""
    rows = parse_aircraft_csv(CSV.replace("3c6444", "3C6444"))
    assert rows[0]["icao24"] == "3c6444"


def test_rows_without_icao24_are_dropped() -> None:
    """The join key. A row that cannot be joined to anything is dead weight."""
    csv = CSV + ",N999XX,Cessna,172,C172,,,\n"
    assert len(parse_aircraft_csv(csv)) == 3


def test_empty_values_become_none() -> None:
    csv = "icao24,registration,model,typecode,operator,built\nabc123,,,,,\n"
    row = parse_aircraft_csv(csv)[0]

    assert row["registration"] is None
    assert row["model"] is None
    assert row["built"] is None


def test_column_aliases_are_accepted() -> None:
    """Upstream header names have changed between releases; several aliases are
    accepted per field so a rename does not break ingestion."""
    csv = "icao24,reg,icaoAircraftType,manufacturer,built\nabc123,N1,B738,Boeing,2001\n"
    row = parse_aircraft_csv(csv)[0]

    assert row["registration"] == "N1"
    assert row["typecode"] == "B738"
    assert row["manufacturer"] == "Boeing"


def test_missing_icao24_column_fails_loudly() -> None:
    with pytest.raises(AircraftDatabaseError, match="no icao24 column"):
        parse_aircraft_csv("registration,model\nN1,737\n")


def test_no_header_fails_loudly() -> None:
    with pytest.raises(AircraftDatabaseError, match="no header row"):
        parse_aircraft_csv("")


# ---- Year of manufacture ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2011", 2011), ("2011-05-14", 2011), ("", None), ("n/a", None), ("0003", None)],
)
def test_built_year_parsing(raw: str, expected: int | None) -> None:
    """Implausible years are dropped rather than stored: an aircraft "built" in
    year 3 would produce an age of two thousand and skew every fleet-age
    feature computed from it."""
    csv = f"icao24,built\nabc123,{raw}\n"
    assert parse_aircraft_csv(csv)[0]["built"] == expected


def test_age_is_derived_not_stored() -> None:
    """Deviation from plan 1.7 on purpose. A stored age is wrong the moment the
    year turns; a reference table silently stale every January is worse than one
    that requires a subtraction."""
    assert "age" not in AIRCRAFT_SCHEMA.names
    assert "built" in AIRCRAFT_SCHEMA.names

    assert aircraft_age(2011, as_of_year=2026) == 15
    assert aircraft_age(2011, as_of_year=2030) == 19


def test_age_of_unknown_build_year_is_none() -> None:
    assert aircraft_age(None, as_of_year=2026) is None


def test_negative_age_is_rejected() -> None:
    """A future build year means bad data, not a negative-age aircraft."""
    assert aircraft_age(2030, as_of_year=2026) is None


# ---- Writing ---------------------------------------------------------------


def test_writes_a_readable_reference_table(tmp_path: Path) -> None:
    path = write_aircraft_reference(parse_aircraft_csv(CSV), tmp_path / "aircraft.parquet")

    table = pq.read_table(path)

    assert table.num_rows == 3
    assert table.schema.field("built").type == AIRCRAFT_SCHEMA.field("built").type
    assert {r["icao24"] for r in table.to_pylist()} == {"3c6444", "a1b2c3", "abc999"}


def test_write_replaces_rather_than_appends(tmp_path: Path) -> None:
    """A lookup table, not a log. Monthly refreshes replace it wholesale, which
    is exactly why it lives outside bronze."""
    destination = tmp_path / "aircraft.parquet"
    write_aircraft_reference(parse_aircraft_csv(CSV), destination)

    smaller = "icao24,built\nabc123,2001\n"
    write_aircraft_reference(parse_aircraft_csv(smaller), destination)

    assert pq.read_table(destination).num_rows == 1


def test_refuses_to_write_an_empty_table(tmp_path: Path) -> None:
    """Better to keep yesterday's good snapshot than replace it with nothing
    because the download returned an error page."""
    with pytest.raises(AircraftDatabaseError, match="empty"):
        write_aircraft_reference([], tmp_path / "aircraft.parquet")


def test_no_temp_file_remains_after_writing(tmp_path: Path) -> None:
    destination = tmp_path / "aircraft.parquet"
    write_aircraft_reference(parse_aircraft_csv(CSV), destination)

    assert list(tmp_path.glob("*.tmp")) == []
