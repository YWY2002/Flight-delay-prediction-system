import os
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from opensky_api import OpenSkyStates
from pyspark.sql import SparkSession

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
    ],
    metadata={
        "source": "opensky /states/all",
        "layer": "bronze",
        "field_order": "matches opensky_api.StateVector.keys, i.e. the wire array order",
        "units": "SI as received: metres, m/s, degrees. No unit conversion in bronze.",
    },
)


class BronzeWriter:
    def __init__(self, root: Path) -> None:
        """
        Args:
            root: Bronze root, normally `settings.bronze_dir`.
            compression: zstd gives noticeably better ratios than snappy on this
                kind of repetitive telemetry, at decompression speed that is
                still far faster than the disk. DuckDB and pyarrow both read it
                without configuration.
        """
        self._root = root

    def partition_dir(self, source: str, partition_time: datetime) -> Path:
        return (
                self._root
                / source
                / f"{partition_time.strftime('%Y-%m')}"
                )



def get_working_dir():
    return os.getcwd()


def create_bronze_writer_session():
    spark = SparkSession().builder.appName('Bronze Writer').getOrCreate()
    return spark

def state_vector_writer(client: SparkSession, payload: OpenSkyStates):
    raise NotImplementedError
