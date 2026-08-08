"""NOAA aviationweather.gov client: METAR observations and TAF forecasts.

    https://aviationweather.gov/api/data/metar?ids=KJFK,KEWR&format=json
    https://aviationweather.gov/api/data/taf?ids=KJFK,KEWR&format=json

No API key, no auth. Both endpoints take a comma-separated station list, so all
airports come back in one request rather than one request each.

Two fields in this API are genuinely messy and get explicit handling below:
`visib` arrives as a string like "10+" or "1 1/2" as often as a number, and
`wdir` can be the string "VRB" when the wind is variable. Both are normalised at
the boundary so nothing downstream has to know.

The raw report text (`rawOb` / `rawTAF`) is kept alongside the parsed fields.
METAR text is the canonical, authoritative form of the observation; keeping it
means anything we failed to parse today can be recovered later without having
lost the data.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from flight_delay.common.config import Settings
from flight_delay.common.logging_config import get_logger
from flight_delay.common.timeutil import EpochSeconds
from flight_delay.ingest.errors import IngestError
from flight_delay.ingest.http import raise_for_status

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://aviationweather.gov/api/data"

# Cloud covers that constitute a ceiling. A ceiling is the lowest broken or
# overcast layer; scattered and few do not count.
_CEILING_COVERS = frozenset({"BKN", "OVC", "OVX", "VV"})

# "1 1/2" (mixed), "1/2" (fraction), "10+" or "10" (plain).
_MIXED_FRACTION = re.compile(r"^(\d+)\s+(\d+)/(\d+)$")
_FRACTION = re.compile(r"^(\d+)/(\d+)$")


class WeatherApiError(IngestError):
    """A METAR or TAF request failed."""


class CloudLayer(BaseModel):
    """One reported cloud layer."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    cover: str | None = None
    base_ft: int | None = Field(default=None, alias="base")


def _parse_visibility(value: object) -> object:
    """Normalise the visibility field to statute miles as a float.

    NOAA sends this as a number sometimes and a string others: "10+" means "ten
    or more", and fractions like "1/2" or "1 1/2" appear in low visibility,
    which is exactly the weather this project cares about. Left as a raw string
    it would be unusable as a feature; parsed loosely it would silently become
    null on the worst-weather days, which is the worst possible bias to
    introduce into a delay model.
    """
    if value is None or isinstance(value, int | float):
        return value
    if not isinstance(value, str):
        return value

    text = value.strip().rstrip("+").replace("SM", "").strip()
    if not text:
        return None

    if mixed := _MIXED_FRACTION.match(text):
        whole, num, den = (int(g) for g in mixed.groups())
        return whole + num / den if den else None
    if fraction := _FRACTION.match(text):
        num, den = (int(g) for g in fraction.groups())
        return num / den if den else None

    try:
        return float(text)
    except ValueError:
        logger.warning("weather.unparseable_visibility", value=value)
        return None


class Metar(BaseModel):
    """One METAR observation.

    `extra="ignore"` on purpose: NOAA returns many fields we do not use, and a
    new one appearing upstream must not break ingestion. The contract we DO care
    about is enforced by the required fields below.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    station: str = Field(alias="icaoId")
    obs_time: EpochSeconds = Field(alias="obsTime")
    raw_text: str | None = Field(default=None, alias="rawOb")

    temp_c: float | None = Field(default=None, alias="temp")
    dewpoint_c: float | None = Field(default=None, alias="dewp")
    wind_dir_deg: int | None = Field(default=None, alias="wdir")
    wind_variable: bool = False
    wind_speed_kt: float | None = Field(default=None, alias="wspd")
    wind_gust_kt: float | None = Field(default=None, alias="wgst")
    visibility_sm: float | None = Field(default=None, alias="visib")
    altimeter_hpa: float | None = Field(default=None, alias="altim")
    wx_string: str | None = Field(default=None, alias="wxString")
    # VFR / MVFR / IFR / LIFR. The API derives this from visibility and ceiling
    # together, which is exactly the combination that drives arrival rates, so
    # it is worth taking rather than recomputing. Plan section 1 lists it as a
    # required field; it was silently dropped by `extra="ignore"` until the live
    # response was actually inspected.
    flight_category: str | None = Field(default=None, alias="fltCat")
    clouds: tuple[CloudLayer, ...] = ()

    _normalise_visibility = field_validator("visibility_sm", mode="before")(_parse_visibility)

    @field_validator("wind_dir_deg", mode="before")
    @classmethod
    def _handle_variable_wind(cls, value: object) -> object:
        """`wdir` is "VRB" when the wind direction is variable.

        Coerced to None rather than rejected: a variable wind is a real, valid
        observation (and a meaningful one near an airport), so dropping the whole
        record over it would lose good data. The `wind_variable` flag preserves
        the distinction between "variable" and "not reported".
        """
        if isinstance(value, str) and value.strip().upper() == "VRB":
            return None
        return value

    @field_validator("clouds", mode="before")
    @classmethod
    def _null_clouds_is_empty(cls, value: object) -> object:
        return () if value is None else value

    def model_post_init(self, _context: object) -> None:
        if self.wind_dir_deg is None and self.raw_text and "VRB" in self.raw_text:
            object.__setattr__(self, "wind_variable", True)

    @property
    def ceiling_ft(self) -> int | None:
        """Lowest broken/overcast layer, or None if the sky is clear enough.

        Derived rather than stored: bronze keeps the cloud layers as reported,
        and anything computed from them belongs to whoever consumes it. Storing
        a derived value in bronze would mean a change to this definition
        invalidates historical rows we can no longer recompute.
        """
        bases = [
            layer.base_ft
            for layer in self.clouds
            if layer.cover in _CEILING_COVERS and layer.base_ft is not None
        ]
        return min(bases) if bases else None


class TafPeriod(BaseModel):
    """One forecast period within a TAF."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    time_from: EpochSeconds | None = Field(default=None, alias="timeFrom")
    time_to: EpochSeconds | None = Field(default=None, alias="timeTo")
    change_indicator: str | None = Field(default=None, alias="fcstChange")
    wind_dir_deg: int | None = Field(default=None, alias="wdir")
    wind_speed_kt: float | None = Field(default=None, alias="wspd")
    wind_gust_kt: float | None = Field(default=None, alias="wgst")
    visibility_sm: float | None = Field(default=None, alias="visib")
    wx_string: str | None = Field(default=None, alias="wxString")
    clouds: tuple[CloudLayer, ...] = ()

    _normalise_visibility = field_validator("visibility_sm", mode="before")(_parse_visibility)

    @field_validator("wind_dir_deg", mode="before")
    @classmethod
    def _handle_variable_wind(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().upper() == "VRB":
            return None
        return value

    @field_validator("clouds", mode="before")
    @classmethod
    def _null_clouds_is_empty(cls, value: object) -> object:
        return () if value is None else value


class Taf(BaseModel):
    """One TAF forecast, with its periods."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    station: str = Field(alias="icaoId")
    valid_from: EpochSeconds | None = Field(default=None, alias="validTimeFrom")
    valid_to: EpochSeconds | None = Field(default=None, alias="validTimeTo")
    raw_text: str | None = Field(default=None, alias="rawTAF")
    periods: tuple[TafPeriod, ...] = Field(default=(), alias="fcsts")

    @field_validator("periods", mode="before")
    @classmethod
    def _null_periods_is_empty(cls, value: object) -> object:
        return () if value is None else value


class WeatherClient:
    """Client for the aviationweather.gov data API."""

    def __init__(
        self,
        http_client: httpx.Client,
        *,
        base_url: str = DEFAULT_BASE_URL,
        owns_client: bool = False,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._owns_client = owns_client

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> WeatherClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_metar(self, station_ids: list[str]) -> list[Metar]:
        """Fetch the latest METAR for each station.

        All stations in one request. The endpoint accepts a comma-separated
        list, so N airports cost one round trip rather than N.
        """
        return [Metar.model_validate(item) for item in self._fetch("metar", station_ids)]

    def get_taf(self, station_ids: list[str]) -> list[Taf]:
        """Fetch the current TAF for each station."""
        return [Taf.model_validate(item) for item in self._fetch("taf", station_ids)]

    def _fetch(self, endpoint: str, station_ids: list[str]) -> list[Any]:
        if not station_ids:
            return []

        url = f"{self._base_url}/{endpoint}"
        params = {"ids": ",".join(station_ids), "format": "json"}

        try:
            response = self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise WeatherApiError(f"Could not reach aviationweather.gov: {exc}") from exc

        raise_for_status(response, source="aviationweather.gov")

        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherApiError(
                f"aviationweather.gov returned a non-JSON {endpoint} response: {exc}"
            ) from exc

        # A single-station query can return a bare object rather than a list.
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise WeatherApiError(
                f"Expected a list from the {endpoint} endpoint, got "
                f"{type(payload).__name__}. The API contract may have changed."
            )

        logger.debug("weather.fetched", endpoint=endpoint, records=len(payload))
        return payload


def client_from_settings(settings: Settings) -> WeatherClient:
    return WeatherClient(
        httpx.Client(timeout=settings.http_timeout_seconds),
        base_url=settings.weather_base_url,
        owns_client=True,
    )
