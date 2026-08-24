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


class BronzeWriter:
    """One Parquet file per source per UTC day, appended in place.

    Layout under `root` (normally `settings.bronze_dir`, so `data/bronze/...`;
    pass `settings.data_dir` instead if you want `data/opensky/...` flat):

        <root>/opensky/2026-08/opensky_24082026.parquet
        <root>/metar/2026-08/metar_24082026.parquet
        <root>/taf/2026-08/taf_24082026.parquet

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
        return self._root / source / f"{partition_time.strftime('%Y-%m')}"

    def file_path(self, source: str, partition_time: datetime) -> Path:
        """The day file for `source`, e.g. `opensky/2026-08/opensky_24082026.parquet`.
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
            # `permissive` lets a column that was all-null yesterday take a real
            # type today, and tolerates a field appearing or vanishing upstream.
            # With an explicit schema neither happens, but METAR and TAF have no
            # schema yet, and a strict concat there would fail on ordinary data.
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
