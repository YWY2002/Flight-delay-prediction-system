"""Bronze writer tests, centred on the schema drift that took the poller down.

The two crashes these reproduce are not hypothetical. Both came out of a live
run against WSSS on 2026-08-25, one per weather source, within two polls of
each other.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pyarrow.parquet as pq
import pytest

from flight_delay.data_ingestion.writer import (
    METAR_SCHEMA,
    METAR_SOURCE,
    TAF_SCHEMA,
    TAF_SOURCE,
    BronzeWriter,
    weather_records_to_rows,
)

DAY = datetime(2026, 8, 25, 5, 30, tzinfo=UTC)


def metar(**overrides: Any) -> dict[str, Any]:
    """A METAR record shaped like the real payload, trimmed to what matters."""
    return {
        "icaoId": "WSSS",
        "obsTime": 1787637600,
        "temp": 27,
        "dewp": 24,
        "wdir": 150,
        "wspd": 8,
        "visib": 5.59,
        "rawOb": "METAR WSSS 250500Z 15008KT 9000 FEW015 27/24 Q1010",
        "clouds": [{"cover": "FEW", "base": 1500}],
        "fltCat": "VFR",
    } | overrides


def taf(*visibilities: Any) -> dict[str, Any]:
    """A TAF bulletin whose forecast periods carry the given visibilities."""
    return {
        "icaoId": "WSSS",
        "issueTime": "2026-08-25T05:00:00.000Z",
        "validTimeFrom": 1787637600,
        "rawTAF": "TAF WSSS 250500Z 2506/2612 15008KT 9999 FEW015",
        "fcsts": [
            {
                "timeFrom": 1787637600 + index * 3600,
                "wdir": 150,
                "wspd": 8,
                "visib": value,
                "clouds": [{"cover": "FEW", "base": 1500, "type": None}],
                "icgTurb": [],
                "temp": [],
            }
            for index, value in enumerate(visibilities)
        ],
    }


def test_metar_visibility_flips_type_between_polls(tmp_path):
    """The original ArrowTypeError: double on disk, string in the new batch.

    Poll one lands while WSSS is hazy, so visib is 5.59. Visibility then clears
    and the API starts sending '10+'. Before the schema this raised "Field
    visib has incompatible types: double vs string" and every subsequent poll
    that day died the same way.
    """
    writer = BronzeWriter(root=tmp_path)
    for value in (5.59, "10+"):
        writer.write(
            METAR_SOURCE,
            weather_records_to_rows([metar(visib=value)], METAR_SCHEMA),
            DAY,
            schema=METAR_SCHEMA,
        )

    table = pq.read_table(writer.file_path(METAR_SOURCE, DAY))
    assert table.column("visib").to_pylist() == ["5.59", "10+"]


def test_taf_mixes_visibility_types_inside_one_bulletin(tmp_path):
    """The original ArrowInvalid: one batch, two types, no file involved.

    "Could not convert '6+' with type str: tried to convert to double" fired in
    `from_pylist`, so this one failed before the writer touched the disk. A
    single bulletin holding both shapes is ordinary, not a malformed response.
    """
    writer = BronzeWriter(root=tmp_path)
    result = writer.write(
        TAF_SOURCE,
        weather_records_to_rows([taf(6.21, "6+")], TAF_SCHEMA),
        DAY,
        schema=TAF_SCHEMA,
    )

    assert result.rows == 1
    table = pq.read_table(writer.file_path(TAF_SOURCE, DAY))
    assert [f["visib"] for f in table.column("fcsts").to_pylist()[0]] == ["6.21", "6+"]


def test_variable_wind_direction_survives(tmp_path):
    """`wdir` is the same trap as visib: an integer until the wind goes VRB."""
    writer = BronzeWriter(root=tmp_path)
    for value in (150, "VRB"):
        writer.write(
            METAR_SOURCE,
            weather_records_to_rows([metar(wdir=value)], METAR_SCHEMA),
            DAY,
            schema=METAR_SCHEMA,
        )

    table = pq.read_table(writer.file_path(METAR_SOURCE, DAY))
    assert table.column("wdir").to_pylist() == ["150", "VRB"]


def test_existing_inferred_file_is_conformed(tmp_path):
    """A day file written before the schema existed must not block the next write.

    This is the state the running container is actually in: visib already
    double on disk. The first schema-carrying write has to repair that file
    rather than fail against it, otherwise the fix only takes effect at midnight.
    """
    writer = BronzeWriter(root=tmp_path)
    writer.write(METAR_SOURCE, [metar()], DAY)  # no schema: infers visib as double
    assert pq.read_table(writer.file_path(METAR_SOURCE, DAY)).schema.field("visib").type == "double"

    writer.write(
        METAR_SOURCE,
        weather_records_to_rows([metar(visib="10+")], METAR_SCHEMA),
        DAY,
        schema=METAR_SCHEMA,
    )

    table = pq.read_table(writer.file_path(METAR_SOURCE, DAY))
    assert table.num_rows == 2
    assert table.schema.field("visib").type == "string"
    assert table.column("visib").to_pylist() == ["5.59", "10+"]


def test_existing_taf_file_conforms_through_an_uncastable_column(tmp_path):
    """The inferred TAF file stores icgTurb as list<null>, and Arrow cannot cast that.

    `list -> utf8` has no kernel, so conforming an existing TAF day file cannot
    lean on `cast` alone. Real failure, hit replaying the 2026-08-25 backup.
    """
    writer = BronzeWriter(root=tmp_path)
    writer.write(TAF_SOURCE, [taf("6+")], DAY)  # no schema: icgTurb infers as list<null>
    writer.write(
        TAF_SOURCE,
        weather_records_to_rows([taf(6.21)], TAF_SCHEMA),
        DAY,
        schema=TAF_SCHEMA,
    )

    table = pq.read_table(writer.file_path(TAF_SOURCE, DAY))
    assert table.num_rows == 2
    assert [row[0]["icgTurb"] for row in table.column("fcsts").to_pylist()] == ["[]", "[]"]
    assert [row[0]["visib"] for row in table.column("fcsts").to_pylist()] == ["6+", "6.21"]


def test_integral_floats_render_without_a_trailing_zero():
    """The coerced path and Arrow's cast path have to agree on 10.0.

    They meet in the same column whenever an older file is conformed, and
    "10" beside "10.0" is the kind of split that only shows up as a silver
    join quietly missing rows.
    """
    [row] = weather_records_to_rows([metar(visib=10.0)], METAR_SCHEMA)
    assert row["visib"] == "10"


def test_unpinned_forecast_arrays_round_trip_as_json():
    """icgTurb/temp are stored as JSON text until their real shape is known."""
    entry = {"validTimeFrom": 1787637600, "intensity": "MOD"}
    [row] = weather_records_to_rows([taf("6+") | {"fcsts": None}], TAF_SCHEMA)
    assert row["fcsts"] is None

    record = taf("6+")
    record["fcsts"][0]["icgTurb"] = [entry]
    [row] = weather_records_to_rows([record], TAF_SCHEMA)
    assert json.loads(row["fcsts"][0]["icgTurb"]) == [entry]


def test_unknown_api_field_is_reported_not_swallowed(capsys):
    """A field the API adds later is dropped by from_pylist without a word.

    Read off stdout rather than caplog: structlog renders here, so the stdlib
    capture fixture sees nothing.
    """
    weather_records_to_rows([metar(newFieldFromAwc=1)], METAR_SCHEMA)
    assert "newFieldFromAwc" in capsys.readouterr().out


def test_naive_partition_time_is_rejected(tmp_path):
    """SGT is UTC+8, so a naive value files everything after 16:00 under tomorrow."""
    with pytest.raises(ValueError, match="timezone aware"):
        BronzeWriter(root=tmp_path).write(METAR_SOURCE, [metar()], datetime(2026, 8, 25, 5, 30))
