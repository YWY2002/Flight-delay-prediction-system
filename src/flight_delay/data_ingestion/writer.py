import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from opensky_api import OpenSkyStates

from flight_delay.common.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# OpenSky state vector schema
# ---------------------------------------------------------------------------
#
# Field ORDER matches `opensky_api.StateVector.keys`, which is the order OpenSky
# packs the wire array in. Keeping it means this file can be diffed against the
# raw response element by element; sorting it alphabetically would look tidier
# and make that check impossible.
#
# Field metadata carries units and enum meanings. Parquet keeps it, so the file
# stays self-describing: "altitude in metres, not feet" is exactly the fact that
# goes missing between bronze and a model six months later, and a reader that
# never saw this module can recover it with `schema.field(name).metadata`.

# Documented in the StateVector docstring. Exported because silver needs to
# decode these, and the mapping should live with the schema rather than being
# retyped at the call site.
POSITION_SOURCES: dict[int, str] = {
    0: "ADS-B",
    1: "ASTERIX",
    2: "MLAT",
    3: "FLARM",
}

AIRCRAFT_CATEGORIES: dict[int, str] = {
    0: "No information at all",
    1: "No ADS-B Emitter Category Information",
    2: "Light (< 15500 lbs)",
    3: "Small (15500 to 75000 lbs)",
    4: "Large (75000 to 300000 lbs)",
    5: "High Vortex Large (aircraft such as B-757)",
    6: "Heavy (> 300000 lbs)",
    7: "High Performance (> 5g acceleration and 400 kts)",
    8: "Rotorcraft",
    9: "Glider / sailplane",
    10: "Lighter-than-air",
    11: "Parachutist / Skydiver",
    12: "Ultralight / hang-glider / paraglider",
    13: "Reserved",
    14: "Unmanned Aerial Vehicle",
    15: "Space / Trans-atmospheric vehicle",
    16: "Surface Vehicle - Emergency Vehicle",
    17: "Surface Vehicle - Service Vehicle",
    18: "Point Obstacle (includes tethered balloons)",
    19: "Cluster Obstacle",
    20: "Line Obstacle",
}


def _enum_doc(mapping: dict[int, str]) -> str:
    return ", ".join(f"{code} = {label}" for code, label in mapping.items())


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one write. Returned rather than logged so callers can report
    it in their own structured log line and, later, as a metric."""

    path: Path | None
    rows: int
    bytes_written: int


STATE_VECTOR_SCHEMA = pa.schema(
    [
        # Note where this bites. `Table.from_pylist` and even `validate(full=True)`
        # accept a null here without complaint; `pq.write_table` is what raises
        # ArrowInvalid. So the guard fires at persist time and fails the whole
        # batch, not the offending row. Filter nulls before writing if losing a
        # good batch to one bad row is the worse outcome.
        pa.field(
            "icao24",
            pa.string(),
            nullable=False,
            metadata={"doc": "ICAO24 transponder address, lower-case hex."},
        ),
        # Fixed 8 characters, so it arrives space-padded ('TGW226  '). Stored
        # exactly as received; stripping is a silver-layer concern.
        pa.field(
            "callsign",
            pa.string(),
            metadata={"doc": "Callsign, 8 chars, space-padded. Null if none received."},
        ),
        pa.field(
            "origin_country",
            pa.string(),
            metadata={"doc": "Inferred from the ICAO24 address."},
        ),
        # Raw epoch SECONDS as int64, not an Arrow timestamp. Both obvious
        # alternatives are worse, and both fail quietly:
        #
        #   timestamp('s')  - Parquet has no second-resolution timestamp, so the
        #                     writer silently stores ms. The schema read back is
        #                     not the schema written, every `schema.equals()`
        #                     check fails on a perfectly good file, and the
        #                     constant in this module lies about what is on disk.
        #   timestamp('ms') - round-trips exactly, but Arrow reads a bare int as
        #                     "units of the column's unit". OpenSky sends
        #                     seconds, so 1787479200 lands as 1970-01-21. Wrong
        #                     by 1000x, no error, and it looks plausible enough
        #                     in a spot check to survive review.
        #
        # int64 is also what bronze is supposed to hold: the value exactly as
        # received, with interpretation deferred. Decode in silver with
        # `flight_delay.common.timeutil.epoch_to_utc`, or `to_timestamp()` in
        # DuckDB and Spark.
        pa.field(
            "time_position",
            pa.int64(),
            metadata={
                "unit": "Unix epoch seconds, UTC",
                "doc": "Last position report. Null if none within 15s before.",
            },
        ),
        pa.field(
            "last_contact",
            pa.int64(),
            metadata={
                "unit": "Unix epoch seconds, UTC",
                "doc": "Last message received from this transponder.",
            },
        ),
        pa.field(
            "longitude",
            pa.float64(),
            metadata={"unit": "degrees", "doc": "WGS-84 ellipsoidal coordinates."},
        ),
        pa.field(
            "latitude",
            pa.float64(),
            metadata={"unit": "degrees", "doc": "WGS-84 ellipsoidal coordinates."},
        ),
        pa.field(
            "baro_altitude",
            pa.float64(),
            metadata={"unit": "m", "doc": "Barometric altitude."},
        ),
        pa.field(
            "on_ground",
            pa.bool_(),
            metadata={"doc": "True if sending ADS-B surface position reports."},
        ),
        pa.field(
            "velocity",
            pa.float64(),
            metadata={"unit": "m/s", "doc": "Speed over ground."},
        ),
        pa.field(
            "true_track",
            pa.float64(),
            metadata={"unit": "degrees", "doc": "Clockwise from north; 0 is north."},
        ),
        pa.field(
            "vertical_rate",
            pa.float64(),
            metadata={"unit": "m/s", "doc": "Positive climbing, negative descending."},
        ),
        # int32 rather than int64: OpenSky sensor serials sit well inside its
        # range, and this column is null unless the request filtered by sensor.
        pa.field(
            "sensors",
            pa.list_(pa.int32()),
            metadata={"doc": "Serials of receivers that saw this vehicle. Usually null."},
        ),
        pa.field(
            "geo_altitude",
            pa.float64(),
            metadata={"unit": "m", "doc": "Geometric altitude."},
        ),
        pa.field(
            "squawk",
            pa.string(),
            metadata={"doc": "Transponder code. Four octal digits, kept as text."},
        ),
        pa.field(
            "spi",
            pa.bool_(),
            metadata={"doc": "Special purpose indicator."},
        ),
        # int8 holds every documented code with room to spare, and narrows the
        # column at rest without capping future additions (int8 reaches 127).
        pa.field(
            "position_source",
            pa.int8(),
            metadata={"doc": _enum_doc(POSITION_SOURCES)},
        ),
        pa.field(
            "category",
            pa.int8(),
            metadata={"doc": _enum_doc(AIRCRAFT_CATEGORIES)},
        ),
        # Appended last, deliberately. Every field above is index-aligned with
        # the wire array; this one is not on the vector at all. It comes from
        # the enclosing OpenSkyStates, which stamps the whole snapshot. Putting
        # it first would read more naturally but shift all 18 wire indices by
        # one and cost the element-by-element diff this order exists for.
        #
        # int64 epoch seconds, matching time_position and last_contact. See the
        # note there for why this is not an Arrow timestamp.
        pa.field(
            "snapshot_time",
            pa.int64(),
            nullable=False,
            metadata={
                "unit": "Unix epoch seconds, UTC",
                "doc": (
                    "OpenSkyStates.time: the instant this whole snapshot describes. "
                    "Every vector in it is valid for [time - 1, time]."
                ),
            },
        ),
    ],
    metadata={
        "source": "opensky /states/all",
        "layer": "bronze",
        "field_order": (
            "fields 0-17 match opensky_api.StateVector.keys, i.e. the wire array order; "
            "snapshot_time is appended and comes from the enclosing OpenSkyStates"
        ),
        "units": "SI as received: metres, m/s, degrees. No unit conversion in bronze.",
    },
)


# Source names. Each doubles as the subdirectory under the bronze root and as
# the filename prefix, so there is exactly one string to get right per source.
OPENSKY_SOURCE = "opensky"
METAR_SOURCE = "metar"
TAF_SOURCE = "taf"


def state_vectors_to_rows(snapshot: OpenSkyStates) -> list[dict[str, Any]]:
    """Flatten a snapshot into rows shaped for STATE_VECTOR_SCHEMA.

    Reads by field name off the schema rather than by array index. OpenSky can
    return a vector shorter than the full 18 elements, and the library builds
    its attributes with `zip`, so the trailing fields are simply absent rather
    than None; `getattr(..., None)` turns that into a null instead of an
    AttributeError that would drop the whole poll.

    `snapshot_time` is stamped onto every row from the enclosing snapshot, since
    it lives on OpenSkyStates rather than on the individual vectors.
    """
    # Read once, outside the loop: it is the same value for every row, and it is
    # what makes the rows of one poll distinguishable from the next poll's.
    snapshot_time = getattr(snapshot, "time", None)
    names = [field.name for field in STATE_VECTOR_SCHEMA if field.name != "snapshot_time"]
    return [
        {name: getattr(vector, name, None) for name in names} | {"snapshot_time": snapshot_time}
        for vector in snapshot.states
    ]


# ---------------------------------------------------------------------------
# METAR and TAF schemas
# ---------------------------------------------------------------------------
#
# Same job as STATE_VECTOR_SCHEMA: pin the types once so a column cannot change
# type between polls. Without a schema both sources crash inside a day, and the
# two ways they crash are worth naming separately because they fail at
# different points and only one of them leaves the day file readable.
#
#   1. Across polls. `visib` is a number when visibility is restricted (5.59)
#      and the string "10+" when it sits at or above the reporting ceiling. A
#      hazy morning writes the day file with visib as double; the first poll
#      after it clears infers string, and the append fails with "Field visib
#      has incompatible types: double vs string".
#   2. Within one poll. A TAF carries several forecast periods under `fcsts`,
#      each with its own visib. One period at 6.21 and the next at "6+" and the
#      batch cannot be built at all: `from_pylist` raises before the existing
#      file is even opened.
#
# Every field the API is known to send in more than one shape is typed `string`
# here, and `weather_records_to_rows` coerces values on the way in. That
# direction is deliberate. "10+" means "10 or greater", so forcing it to 10.0
# invents a precision the observation does not have and quietly conflates a
# capped reading with an exact one. Bronze keeps the sighting as sent; silver
# parses it into a value plus an at-or-above flag.
#
# `wdir` is the same story with "VRB" for variable wind. It has not fired yet
# only because Changi has not sent a variable-direction report since polling
# started, which is luck rather than a difference in kind.

# One cloud layer. `base` is null for CLR/SKC, and `type` (CB, TCU) appears on
# TAF forecast layers but not on METAR observations. Both endpoints share the
# struct so a reader does not need two code paths for one concept.
_CLOUD_LAYER = pa.struct(
    [
        pa.field("cover", pa.string()),
        pa.field("base", pa.int64()),
        pa.field("type", pa.string()),
    ]
)

METAR_SCHEMA = pa.schema(
    [
        pa.field(
            "icaoId",
            pa.string(),
            nullable=False,
            metadata={"doc": "Reporting station, four uppercase letters."},
        ),
        pa.field("receiptTime", pa.string(), metadata={"doc": "When AWC ingested the report."}),
        # Epoch seconds as int64, for the reasons spelled out on STATE_VECTOR_SCHEMA.
        pa.field(
            "obsTime",
            pa.int64(),
            metadata={"doc": "Observation time, epoch SECONDS. Dedupe on (icaoId, obsTime)."},
        ),
        pa.field("reportTime", pa.string(), metadata={"doc": "Nominal hour of the report."}),
        # Whole degrees from most stations, decimals from some. Typed double so
        # the two do not fight; an integer 26 arrives as 26.0.
        pa.field("temp", pa.float64(), metadata={"units": "degrees Celsius"}),
        pa.field("dewp", pa.float64(), metadata={"units": "degrees Celsius"}),
        pa.field(
            "wdir",
            pa.string(),
            metadata={"doc": "Degrees true as text, or 'VRB' when variable.", "units": "degrees"},
        ),
        pa.field("wspd", pa.int64(), metadata={"units": "knots"}),
        pa.field("wgst", pa.int64(), metadata={"doc": "Null when not gusting.", "units": "knots"}),
        pa.field(
            "visib",
            pa.string(),
            metadata={
                "doc": "Statute miles as text. '10+' means 10 or greater, not exactly 10.",
                "units": "statute miles",
            },
        ),
        pa.field("altim", pa.float64(), metadata={"units": "hectopascals"}),
        pa.field("slp", pa.float64(), metadata={"doc": "Sea level pressure.", "units": "hPa"}),
        pa.field("qcField", pa.int64(), metadata={"doc": "AWC quality-control bitmask."}),
        pa.field("wxString", pa.string(), metadata={"doc": "Present weather, e.g. 'TSRA'."}),
        pa.field("presTend", pa.float64(), metadata={"units": "hPa over 3 hours"}),
        pa.field("maxT", pa.float64(), metadata={"units": "degrees Celsius"}),
        pa.field("minT", pa.float64(), metadata={"units": "degrees Celsius"}),
        pa.field("maxT24", pa.float64(), metadata={"units": "degrees Celsius"}),
        pa.field("minT24", pa.float64(), metadata={"units": "degrees Celsius"}),
        pa.field("precip", pa.float64(), metadata={"units": "inches"}),
        pa.field("pcp3hr", pa.float64(), metadata={"units": "inches"}),
        pa.field("pcp6hr", pa.float64(), metadata={"units": "inches"}),
        pa.field("pcp24hr", pa.float64(), metadata={"units": "inches"}),
        pa.field("snow", pa.float64(), metadata={"units": "inches"}),
        pa.field("vertVis", pa.int64(), metadata={"doc": "Vertical visibility.", "units": "feet"}),
        pa.field("metarType", pa.string(), metadata={"doc": "METAR or SPECI."}),
        # The authoritative form. Every decoded field above is AWC's reading of
        # this line, so anything they decode wrongly stays recoverable here.
        pa.field("rawOb", pa.string(), metadata={"doc": "Raw observation text."}),
        pa.field("mostRecent", pa.int64(), metadata={"doc": "1 if latest for the station."}),
        pa.field("lat", pa.float64(), metadata={"units": "degrees"}),
        pa.field("lon", pa.float64(), metadata={"units": "degrees"}),
        pa.field("elev", pa.int64(), metadata={"units": "metres"}),
        pa.field("prior", pa.int64()),
        pa.field("name", pa.string(), metadata={"doc": "Station name."}),
        pa.field("clouds", pa.list_(_CLOUD_LAYER), metadata={"units": "base in feet AGL"}),
        pa.field("cover", pa.string(), metadata={"doc": "Worst layer, e.g. 'BKN'."}),
        pa.field("fltCat", pa.string(), metadata={"doc": "VFR, MVFR, IFR or LIFR."}),
    ],
    metadata={
        "source": "https://aviationweather.gov/api/data/metar?format=json",
        "note": (
            "visib and wdir are strings on purpose: the API sends '10+' and 'VRB' "
            "in the same field as numbers. See weather_records_to_rows."
        ),
    },
)

# One forecast period inside a TAF. This is where the within-batch crash lives:
# a single bulletin routinely mixes a numeric visib in one period with '6+' in
# the next, so pinning it here is what makes that bulletin writable at all.
_TAF_FORECAST = pa.struct(
    [
        pa.field("timeFrom", pa.int64()),
        pa.field("timeTo", pa.int64()),
        pa.field("timeBec", pa.int64()),
        pa.field("fcstChange", pa.string()),
        pa.field("probability", pa.int64()),
        pa.field("wdir", pa.string()),
        pa.field("wspd", pa.int64()),
        pa.field("wgst", pa.int64()),
        pa.field("wshearHgt", pa.int64()),
        pa.field("wshearDir", pa.string()),
        pa.field("wshearSpd", pa.int64()),
        pa.field("visib", pa.string()),
        pa.field("altim", pa.float64()),
        pa.field("vertVis", pa.int64()),
        pa.field("wxString", pa.string()),
        pa.field("notDecoded", pa.string()),
        pa.field("clouds", pa.list_(_CLOUD_LAYER)),
        # Icing/turbulence groups and forecast temperatures. Typed as JSON text
        # rather than as structs because every TAF seen so far has them empty,
        # so their element shape would be a guess. A guessed struct is the worse
        # failure: the first bulletin carrying icing either crashes the write
        # or, if the guess is merely incomplete, drops the keys that did not
        # match and looks like it worked. `weather_records_to_rows` runs
        # whatever arrives through json.dumps, which cannot drift. Promote them
        # to real structs once a populated example exists on disk.
        pa.field("icgTurb", pa.string(), metadata={"doc": "JSON array, shape not yet pinned."}),
        pa.field("temp", pa.string(), metadata={"doc": "JSON array, shape not yet pinned."}),
    ]
)

TAF_SCHEMA = pa.schema(
    [
        pa.field("icaoId", pa.string(), nullable=False),
        pa.field("dbPopTime", pa.string()),
        pa.field("bulletinTime", pa.string()),
        pa.field(
            "issueTime",
            pa.string(),
            metadata={"doc": "Issue time. With icaoId, the natural dedupe key."},
        ),
        pa.field("validTimeFrom", pa.int64(), metadata={"units": "epoch seconds"}),
        pa.field("validTimeTo", pa.int64(), metadata={"units": "epoch seconds"}),
        pa.field("rawTAF", pa.string(), metadata={"doc": "Raw bulletin text."}),
        pa.field("mostRecent", pa.int64()),
        pa.field("remarks", pa.string()),
        pa.field("lat", pa.float64(), metadata={"units": "degrees"}),
        pa.field("lon", pa.float64(), metadata={"units": "degrees"}),
        pa.field("elev", pa.int64(), metadata={"units": "metres"}),
        pa.field("prior", pa.int64()),
        pa.field("name", pa.string()),
        pa.field(
            "fcsts",
            pa.list_(_TAF_FORECAST),
            metadata={"doc": "Forecast periods in order. One row per bulletin, not per period."},
        ),
    ],
    metadata={
        "source": "https://aviationweather.gov/api/data/taf?format=json",
        "note": (
            "A bulletin is superseded, never amended in place. Keep successive issues; "
            "overwriting loses the forecast that was visible at prediction time."
        ),
    },
)

# Looked up by source name so a caller that already has the source string does
# not have to keep its own mapping in step with this module.
WEATHER_SCHEMAS: dict[str, pa.Schema] = {
    METAR_SOURCE: METAR_SCHEMA,
    TAF_SOURCE: TAF_SCHEMA,
}


def _conform(table: pa.Table, schema: pa.Schema, *, source: str) -> pa.Table:
    """Rewrite `table` to exactly `schema`, casting columns and filling gaps.

    Used on the day file already on disk, which may have been written before
    the schema existed or before it last changed. Column by column rather than
    `Table.cast`, which requires the field sets to match and so cannot handle a
    schema that has since gained a field.

    A column present on disk but absent from the schema is dropped, which is
    the one lossy move here, so it is logged rather than assumed harmless.
    """
    columns: list[Any] = []
    unknown: set[str] = set()
    for field in schema:
        if field.name not in table.schema.names:
            columns.append(pa.nulls(table.num_rows, type=field.type))
            continue

        column = table.column(field.name)
        if column.type.equals(field.type):
            columns.append(column)
            continue

        try:
            columns.append(column.cast(field.type))
        except (pa.ArrowNotImplementedError, pa.ArrowInvalid):
            # Arrow has no kernel for some conversions this schema needs, the
            # nested arrays stored as JSON text being the one that bites: it
            # refuses list -> utf8 outright. Rebuild the column through the
            # same Python coercion new records take, which knows how to render
            # a container as JSON and how to walk into structs to do it.
            values = column.to_pylist()
            columns.append(
                pa.array(
                    [_coerce(value, field.type, unknown, field.name) for value in values],
                    type=field.type,
                )
            )

    if unknown:
        logger.warning("bronze.conform.dropped_fields", source=source, fields=sorted(unknown))

    if dropped := [name for name in table.schema.names if name not in schema.names]:
        logger.warning(
            "bronze.conform.dropped_columns",
            source=source,
            columns=dropped,
            rows=table.num_rows,
            hint="Present in the existing day file, absent from the schema. Add them to keep.",
        )
    return pa.Table.from_arrays(columns, schema=schema)


def _stringify(value: Any) -> str:
    """Render `value` for a field the schema types as string.

    Deliberately matches what Arrow's own double-to-string cast produces, so a
    row coerced here and an older row cast by `_conform` come out identical:
    10.0 renders as "10", not "10.0". Containers become JSON rather than Python
    repr, which is what makes the icgTurb/temp escape hatch parseable later.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple | dict):
        return json.dumps(value, default=str)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _coerce(value: Any, dtype: pa.DataType, unknown: set[str], path: str) -> Any:
    """Reshape one value to `dtype`, recursing into lists and structs.

    Keys the schema does not mention are collected into `unknown` rather than
    dropped in silence. `from_pylist` discards them without a word, which is how
    a newly added upstream field goes missing for months before anyone looks.
    """
    if value is None:
        return None
    if pa.types.is_string(dtype):
        return _stringify(value)
    if pa.types.is_list(dtype) or pa.types.is_large_list(dtype):
        if not isinstance(value, list | tuple):
            return value
        return [_coerce(item, dtype.value_type, unknown, path) for item in value]
    if pa.types.is_struct(dtype):
        if not isinstance(value, Mapping):
            return value
        known = {field.name for field in dtype}
        unknown.update(f"{path}.{key}" for key in value if key not in known)
        return {
            field.name: _coerce(value.get(field.name), field.type, unknown, f"{path}.{field.name}")
            for field in dtype
        }
    return value


def weather_records_to_rows(
    records: Sequence[Mapping[str, Any]], schema: pa.Schema
) -> list[dict[str, Any]]:
    """Shape raw METAR/TAF records to `schema` before they reach pyarrow.

    Pinning the schema alone is not enough. Arrow will cast a column that has
    already been built, but it rejects a bare float handed to a string field
    during `from_pylist`, and a batch whose visib values are genuinely mixed
    cannot be inferred in the first place. So the coercion has to happen on the
    Python side, value by value, before any table exists.
    """
    unknown: set[str] = set()
    rows = []
    for record in records:
        unknown.update(key for key in record if key not in schema.names)
        rows.append(
            {
                field.name: _coerce(record.get(field.name), field.type, unknown, field.name)
                for field in schema
            }
        )

    # A field the API added since this schema was written is real data being
    # discarded on every poll. Loud enough to notice, not loud enough to stop
    # ingestion over.
    if unknown:
        logger.warning(
            "bronze.weather.unknown_fields",
            fields=sorted(unknown),
            hint="Present in the API response, absent from the schema, so not persisted.",
        )
    return rows


class BronzeWriter:
    """One Parquet file per source per UTC day, appended in place.

    Layout under `root` (normally `settings.bronze_dir`, so `data/bronze/...`;
    pass `settings.data_dir` instead if you want `data/opensky/...`):

        <root>/opensky/year=2026/month=08/day=24/opensky_24082026.parquet
        <root>/metar/year=2026/month=08/day=24/metar_24082026.parquet
        <root>/taf/year=2026/month=08/day=24/taf_24082026.parquet

    The `key=value` directories are Hive partitioning, which Spark, pyarrow,
    DuckDB and Polars all discover natively -- this is not a Glue or Athena
    convention. It buys two things: `year`, `month` and `day` become real
    queryable columns that are stored in no file, and a query for one day lists
    directories rather than opening every file in the month.

    `source` sits ABOVE the partition columns, so the three sources are three
    independent datasets that merely share a partition scheme. Point readers at
    `<root>/opensky`, never at `<root>`: a reader given the bronze root does not
    error, it infers a schema from whichever file it meets first (alphabetically
    METAR) and silently returns every OpenSky row as nulls under METAR's columns.

    Parquet files are immutable, so "append" here means read, concatenate and
    rewrite. That is quadratic in writes per day, which is worth stating plainly
    before it surprises anyone: at the scheduled cadences it costs nothing.
    OpenSky at 480 polls a day and ~25 aircraft a poll ends the day around
    12,000 rows and a couple of MB, so the last rewrite of the day reads and
    writes a couple of MB, and the whole day moves well under a gigabyte of I/O.
    METAR (48 writes) and TAF (4) are noise beside it. It stops being free if
    you add many airports or drop the OpenSky interval much below a minute, at
    which point the fix is one file per poll plus a nightly compaction, not a
    faster rewrite.
    """

    def __init__(self, root: Path, *, compression: str = "zstd") -> None:
        """
        Args:
            root: Bronze root, normally `settings.bronze_dir`.
            compression: zstd gives noticeably better ratios than snappy on this
                kind of repetitive telemetry, at decompression speed that is
                still far faster than the disk. DuckDB and pyarrow both read it
                without configuration.
        """
        self._root = root
        self._compression = compression

    def partition_dir(self, source: str, partition_time: datetime) -> Path:
        return (
            self._root
            / source
            / f"year={partition_time:%Y}"
            / f"month={partition_time:%m}"
            / f"day={partition_time:%d}"
        )

    def file_path(self, source: str, partition_time: datetime) -> Path:
        """The day file for `source`.

        e.g. `opensky/year=2026/month=08/day=24/opensky_24082026.parquet`.

        The `ddmmyyyy` in the filename is redundant now that the path pins the
        date, and on its own it would not sort chronologically. Both are fine:
        each directory holds exactly one file, so nothing sorts against it, and
        a name that reads as a date is worth keeping for anyone browsing the
        bucket or handed a single file out of context. Query engines ignore
        filenames entirely and read the date from the directories.
        """
        return self.partition_dir(source, partition_time) / (
            f"{source}_{partition_time.strftime('%d%m%Y')}.parquet"
        )

    def write(
        self,
        source: str,
        records: Sequence[Mapping[str, Any]],
        partition_time: datetime,
        *,
        schema: pa.Schema | None = None,
    ) -> WriteResult:
        """Append `records` to the day file for `source`.

        Args:
            source: One of OPENSKY_SOURCE / METAR_SOURCE / TAF_SOURCE.
            records: Raw rows, as plain mappings.
            partition_time: Which day this batch belongs to. Must be timezone
                aware. Take it from the data itself (the snapshot time, the
                observation time) and not from the wall clock at write time, or
                a batch polled at 00:00:30Z lands in the wrong day whenever a
                retry pushes the write past midnight.
            schema: Strongly recommended, and required in practice for OpenSky.
                Without it pyarrow infers types per batch, and a column where
                every aircraft in this poll happened to report nothing infers as
                `null` rather than as its real type. The next poll infers
                `double`, the two schemas no longer match, and the append fails.
                Passing STATE_VECTOR_SCHEMA pins the types once.

        Returns:
            `rows` counts the records appended by THIS call; `bytes_written` is
            the size of the whole resulting file, which includes everything
            written earlier the same day.
        """
        if not records:
            logger.debug("bronze.write.empty", source=source)
            return WriteResult(path=None, rows=0, bytes_written=0)

        if partition_time.tzinfo is None:
            raise ValueError(
                f"partition_time must be timezone aware, got naive {partition_time!r}. "
                f"A naive value silently adopts the machine's local zone, which in "
                f"SGT (UTC+8) files everything after 16:00 UTC under tomorrow's date "
                f"and quietly splits every day's data across two files."
            )

        partition_time = partition_time.astimezone(UTC)
        out_dir = self.file_path(source, partition_time)
        incoming = pa.Table.from_pylist(list(records), schema=schema)

        if out_dir.exists():
            existing = pq.read_table(out_dir)
            if schema is not None:
                # The file on disk may predate this schema, or predate a change
                # to it. Conform it rather than trusting it: a day file written
                # by type inference has visib as double on a hazy morning, and
                # no amount of care on the incoming side makes that concat work.
                # Doing it here means the first write after a schema change
                # repairs the day in place instead of failing until midnight.
                existing = _conform(existing, schema, source=source)
            # `permissive` lets a column that was all-null yesterday take a real
            # type today, and tolerates a field appearing or vanishing upstream.
            # It is redundant once both sides are conformed, and kept for the
            # schema-less path, where a strict concat fails on ordinary data.
            table = pa.concat_tables([existing, incoming], promote_options="permissive")
        else:
            table = incoming

        out_dir.parent.mkdir(parents=True, exist_ok=True)

        # Write beside the target, then rename over it. `os.replace` is atomic
        # within a filesystem, so a crash or a schema rejection mid-write leaves
        # the previous file untouched rather than truncated. Without this, one
        # bad batch late in the day destroys every earlier poll in that file.
        tmp = out_dir.with_name(f"{out_dir.name}.tmp")
        try:
            pq.write_table(table, tmp, compression=self._compression)
            os.replace(tmp, out_dir)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        size = out_dir.stat().st_size
        logger.debug(
            "bronze.write.completed",
            source=source,
            path=str(out_dir),
            rows_appended=len(records),
            rows_total=table.num_rows,
            bytes=size,
        )
        return WriteResult(path=out_dir, rows=len(records), bytes_written=size)
