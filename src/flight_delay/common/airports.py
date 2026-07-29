"""Airport reference data: typed models, loader, and bounding-box derivation.

Loads `config/airports.toml` into validated `Airport` objects. Parsing happens
once, at the edge; everything downstream receives typed objects instead of
re-reading dicts and re-checking whether a key exists.
"""

from __future__ import annotations

import tomllib
from math import cos, radians
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# ICAO identifiers are exactly four uppercase letters (KJFK, EGLL, RJTT).
_ICAO_PATTERN = r"^[A-Z]{4}$"

# One degree of latitude is ~60 nautical miles, everywhere on the globe.
# (That is the historical *definition* of the nautical mile, not a coincidence.)
_NM_PER_DEGREE_LAT = 60.0

# Floor on cos(latitude) when converting nm -> degrees of longitude. Guards the
# division from exploding as cos() -> 0 at the poles. 0.01 corresponds to ~89.4
# degrees latitude; no airport we care about is anywhere near it.
_MIN_COS_LAT = 0.01


class BoundingBox(BaseModel):
    """A lat/lon rectangle, named to match OpenSky's query parameters.

    Field names are deliberately OpenSky's (`lamin`/`lamax`/`lomin`/`lomax`) so
    the call site is a direct unpack with no error-prone renaming step.
    """

    model_config = ConfigDict(frozen=True)

    lamin: float = Field(ge=-90.0, le=90.0, description="Minimum latitude (south edge).")
    lamax: float = Field(ge=-90.0, le=90.0, description="Maximum latitude (north edge).")
    lomin: float = Field(ge=-180.0, le=180.0, description="Minimum longitude (west edge).")
    lomax: float = Field(ge=-180.0, le=180.0, description="Maximum longitude (east edge).")


class Airport(BaseModel):
    """Static facts about one airport, loaded from `config/airports.toml`.

    Frozen: reference data must not be mutated at runtime. A bug that quietly
    moves an airport is far easier to prevent than to diagnose.
    """

    model_config = ConfigDict(frozen=True)

    icao: str = Field(pattern=_ICAO_PATTERN)
    name: str = Field(min_length=1)
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    metar_station: str = Field(pattern=_ICAO_PATTERN)

    def bounding_box(self, radius_nm: float) -> BoundingBox:
        """Derive a square-ish bbox extending `radius_nm` around the field.

        Derived rather than stored: if the box were config, it could drift out of
        sync with `lat`/`lon` and silently stop containing its own airport --
        which looks like "the airport got quiet", not like a bug.

        The longitude span is widened by 1/cos(lat) because meridians converge
        toward the poles: at KJFK's ~40.6 degrees N, a degree of longitude is only
        ~46 nm, so covering the same distance east-west takes ~1.3x more degrees
        than north-south.

        Known limitation: no antimeridian wraparound. An airport within
        `radius_nm` of +/-180 degrees longitude would clamp instead of splitting
        into two boxes. Every airport in scope is in the US, so this is an
        accepted simplification rather than an oversight -- revisit if the
        project ever covers Fiji or the Aleutians.
        """
        if radius_nm <= 0:
            raise ValueError(f"radius_nm must be positive, got {radius_nm}")

        lat_delta = radius_nm / _NM_PER_DEGREE_LAT
        cos_lat = max(cos(radians(self.lat)), _MIN_COS_LAT)
        lon_delta = radius_nm / (_NM_PER_DEGREE_LAT * cos_lat)

        return BoundingBox(
            lamin=max(self.lat - lat_delta, -90.0),
            lamax=min(self.lat + lat_delta, 90.0),
            lomin=max(self.lon - lon_delta, -180.0),
            lomax=min(self.lon + lon_delta, 180.0),
        )


def load_airports(path: Path) -> dict[str, Airport]:
    """Load and validate the airport reference file, keyed by ICAO.

    Raises FileNotFoundError if missing, ValueError on duplicate ICAO ids, and
    pydantic.ValidationError on a malformed entry. All three are startup-time
    failures on purpose: a typo in reference data should stop the process, not
    produce an airport that silently never matches anything.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Airport reference file not found: {path}")

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("airports", [])
    if not entries:
        raise ValueError(f"No [[airports]] entries found in {path}")

    airports: dict[str, Airport] = {}
    for entry in entries:
        airport = Airport.model_validate(entry)
        # Explicit duplicate check: building the dict via comprehension would
        # silently keep the last entry and drop the earlier one.
        if airport.icao in airports:
            raise ValueError(f"Duplicate airport '{airport.icao}' in {path}")
        airports[airport.icao] = airport

    return airports


def resolve_active_airports(
    active_icaos: tuple[str, ...],
    reference: dict[str, Airport],
) -> tuple[Airport, ...]:
    """Map the configured active ICAO codes onto loaded reference entries.

    This is the join between the two config layers: `.env` says *which* airports
    to run, `airports.toml` says *what they are*. An active code with no
    reference entry raises immediately rather than being skipped -- a typo in
    FDP_AIRPORTS should be loud, since the quiet failure mode is a pipeline that
    runs clean while collecting nothing.
    """
    unknown = [icao for icao in active_icaos if icao not in reference]
    if unknown:
        known = ", ".join(sorted(reference))
        raise ValueError(f"Unknown airport(s) in config: {', '.join(unknown)}. Known: {known}")

    return tuple(reference[icao] for icao in active_icaos)
