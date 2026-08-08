"""Bronze schemas and row mapping for METAR and TAF.

Kept separate from `weather_client.py` so the client stays about HTTP and
parsing, while storage concerns (schema, metadata columns, hashing) live here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pyarrow as pa

from flight_delay.ingest.bronze import payload_hash
from flight_delay.ingest.weather_client import Metar, Taf

METAR_SOURCE = "metar"
TAF_SOURCE = "taf"

_CLOUD_STRUCT = pa.list_(pa.struct([("cover", pa.string()), ("base_ft", pa.int32())]))

METAR_SCHEMA = pa.schema(
    [
        ("poll_id", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("payload_hash", pa.string()),
        ("station", pa.string()),
        ("obs_time", pa.timestamp("us", tz="UTC")),
        ("raw_text", pa.string()),
        ("temp_c", pa.float64()),
        ("dewpoint_c", pa.float64()),
        ("wind_dir_deg", pa.int32()),
        ("wind_variable", pa.bool_()),
        ("wind_speed_kt", pa.float64()),
        ("wind_gust_kt", pa.float64()),
        ("visibility_sm", pa.float64()),
        ("altimeter_hpa", pa.float64()),
        ("wx_string", pa.string()),
        ("flight_category", pa.string()),
        ("clouds", _CLOUD_STRUCT),
    ]
)

TAF_SCHEMA = pa.schema(
    [
        ("poll_id", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("payload_hash", pa.string()),
        ("station", pa.string()),
        ("valid_from", pa.timestamp("us", tz="UTC")),
        ("valid_to", pa.timestamp("us", tz="UTC")),
        ("raw_text", pa.string()),
        (
            "periods",
            pa.list_(
                pa.struct(
                    [
                        ("time_from", pa.timestamp("us", tz="UTC")),
                        ("time_to", pa.timestamp("us", tz="UTC")),
                        ("change_indicator", pa.string()),
                        ("wind_dir_deg", pa.int32()),
                        ("wind_speed_kt", pa.float64()),
                        ("wind_gust_kt", pa.float64()),
                        ("visibility_sm", pa.float64()),
                        ("wx_string", pa.string()),
                        ("clouds", _CLOUD_STRUCT),
                    ]
                )
            ),
        ),
    ]
)


def metar_to_row(metar: Metar, *, poll_id: str, ingested_at: datetime) -> dict[str, Any]:
    """Flatten a METAR into a bronze row.

    METARs publish hourly but are polled every 10 minutes to catch off-cycle
    SPECIs, so the same observation is returned five or six times in a row. The
    hash covers the observation only, making those repeats collapse in silver.
    `ceiling_ft` is deliberately not stored: it is derived from `clouds`, and a
    derived value in bronze cannot be recomputed if its definition changes.
    """
    observation = metar.model_dump(mode="json")
    return {
        "poll_id": poll_id,
        "ingested_at": ingested_at,
        "payload_hash": payload_hash(observation),
        **metar.model_dump(),
        "clouds": [{"cover": layer.cover, "base_ft": layer.base_ft} for layer in metar.clouds],
    }


def taf_to_row(taf: Taf, *, poll_id: str, ingested_at: datetime) -> dict[str, Any]:
    """Flatten a TAF and its forecast periods into a bronze row."""
    forecast = taf.model_dump(mode="json")
    return {
        "poll_id": poll_id,
        "ingested_at": ingested_at,
        "payload_hash": payload_hash(forecast),
        "station": taf.station,
        "valid_from": taf.valid_from,
        "valid_to": taf.valid_to,
        "raw_text": taf.raw_text,
        "periods": [
            {
                "time_from": period.time_from,
                "time_to": period.time_to,
                "change_indicator": period.change_indicator,
                "wind_dir_deg": period.wind_dir_deg,
                "wind_speed_kt": period.wind_speed_kt,
                "wind_gust_kt": period.wind_gust_kt,
                "visibility_sm": period.visibility_sm,
                "wx_string": period.wx_string,
                "clouds": [
                    {"cover": layer.cover, "base_ft": layer.base_ft} for layer in period.clouds
                ],
            }
            for period in taf.periods
        ],
    }
