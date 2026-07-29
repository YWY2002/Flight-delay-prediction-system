"""Tests for airport reference data loading and bounding-box geometry."""

from __future__ import annotations

from math import cos, radians
from pathlib import Path

import pytest
from pydantic import ValidationError

from flight_delay.common.airports import (
    Airport,
    load_airports,
    resolve_active_airports,
)
from flight_delay.common.config import PROJECT_ROOT

KJFK = Airport(
    icao="KJFK",
    name="John F. Kennedy International",
    lat=40.6398,
    lon=-73.7789,
    metar_station="KJFK",
)


# ---- Model validation ------------------------------------------------------


def test_rejects_malformed_icao() -> None:
    """A lowercase or wrong-length id must fail loudly, not be coerced."""
    with pytest.raises(ValidationError):
        Airport(icao="kjfk", name="x", lat=0.0, lon=0.0, metar_station="KJFK")


def test_rejects_out_of_range_latitude() -> None:
    with pytest.raises(ValidationError):
        Airport(icao="KJFK", name="x", lat=91.0, lon=0.0, metar_station="KJFK")


def test_airport_is_immutable() -> None:
    """Reference data must not be mutable at runtime."""
    with pytest.raises(ValidationError):
        KJFK.lat = 0.0


# ---- Bounding box geometry -------------------------------------------------


def test_bbox_contains_its_own_airport() -> None:
    """The defining property: the box must always contain the field itself."""
    bbox = KJFK.bounding_box(radius_nm=60.0)
    assert bbox.lamin < KJFK.lat < bbox.lamax
    assert bbox.lomin < KJFK.lon < bbox.lomax


def test_bbox_latitude_span_uses_60nm_per_degree() -> None:
    """60 nm of latitude is exactly 1 degree, by definition of the nautical mile."""
    bbox = KJFK.bounding_box(radius_nm=60.0)
    assert bbox.lamax - bbox.lamin == pytest.approx(2.0)


def test_bbox_longitude_span_widens_with_latitude() -> None:
    """Meridians converge, so a degree of longitude covers less ground than a
    degree of latitude away from the equator. The box must compensate."""
    bbox = KJFK.bounding_box(radius_nm=60.0)
    lat_span = bbox.lamax - bbox.lamin
    lon_span = bbox.lomax - bbox.lomin

    assert lon_span > lat_span
    expected = 2.0 / cos(radians(KJFK.lat))
    assert lon_span == pytest.approx(expected, rel=1e-6)


def test_bbox_rejects_nonpositive_radius() -> None:
    with pytest.raises(ValueError, match="radius_nm must be positive"):
        KJFK.bounding_box(radius_nm=0.0)


def test_bbox_clamps_near_the_pole() -> None:
    """Latitude must never exceed +/-90 even with an absurd radius."""
    svalbard = Airport(icao="ENSB", name="Svalbard", lat=78.246, lon=15.4656, metar_station="ENSB")
    bbox = svalbard.bounding_box(radius_nm=1000.0)
    assert bbox.lamax <= 90.0
    assert bbox.lomin >= -180.0
    assert bbox.lomax <= 180.0


# ---- Loader ----------------------------------------------------------------


def test_loads_committed_reference_file() -> None:
    """Guards the real config/airports.toml against typos. This test fails the
    build if someone commits a malformed entry."""
    airports = load_airports(PROJECT_ROOT / "config" / "airports.toml")

    assert {"KJFK", "KEWR", "KORD"} <= set(airports)
    assert airports["KJFK"].name.startswith("John F. Kennedy")


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_airports(tmp_path / "nope.toml")


def test_duplicate_icao_raises(tmp_path: Path) -> None:
    """A dict comprehension would silently drop the earlier entry; we don't."""
    path = tmp_path / "airports.toml"
    path.write_text(
        """
[[airports]]
icao = "KJFK"
name = "First"
lat = 40.6398
lon = -73.7789
metar_station = "KJFK"

[[airports]]
icao = "KJFK"
name = "Duplicate"
lat = 40.0
lon = -73.0
metar_station = "KJFK"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate airport 'KJFK'"):
        load_airports(path)


def test_empty_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "airports.toml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="No \\[\\[airports\\]\\] entries"):
        load_airports(path)


# ---- Joining the two config layers ----------------------------------------


def test_resolve_active_airports_preserves_order() -> None:
    reference = {"KJFK": KJFK, "KEWR": KJFK.model_copy(update={"icao": "KEWR"})}
    resolved = resolve_active_airports(("KEWR", "KJFK"), reference)
    assert [a.icao for a in resolved] == ["KEWR", "KJFK"]


def test_resolve_unknown_airport_raises() -> None:
    """A typo in FDP_AIRPORTS must stop the process. Skipping it silently would
    yield a pipeline that runs green while collecting nothing."""
    with pytest.raises(ValueError, match="Unknown airport"):
        resolve_active_airports(("KXXX",), {"KJFK": KJFK})
