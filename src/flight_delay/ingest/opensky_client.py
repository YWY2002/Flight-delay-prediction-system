"""OpenSky `/states/all` client: fetch live aircraft state vectors for a bbox.

The awkward part of this API is its wire format. Each aircraft comes back as a
bare JSON array with no field names:

    ["3c6444", "DLH9LF  ", "Germany", 1458564120, 1458564120, 6.15, 50.19,
     9639.3, false, 232.88, 98.26, 4.55, null, 9547.86, "1000", false, 0]

Position 6 means latitude only because everyone agrees it does. If OpenSky ever
inserts a field, every downstream number silently shifts one slot and the
pipeline keeps running while producing garbage. Nothing crashes. Models train on
nonsense.

So the array is converted to named fields exactly once, here, at the boundary,
and validated hard enough that a shape change fails loudly instead of quietly
(plan task 1.12). After this module, no code indexes into a positional array.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from flight_delay.common.airports import BoundingBox
from flight_delay.common.config import Settings
from flight_delay.ingest.opensky_auth import (
    OpenSkyAuth,
    OpenSkyTokenProvider,
    token_provider_from_settings,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://opensky-network.org/api"

# The documented order of the /states/all state-vector array.
#
# This tuple IS the contract. It is the only place the positional layout is
# written down, and `_STATE_FIELDS.index(...)` is never used anywhere: fields
# are addressed by name after parsing.
#
# Names carry units on purpose (`_m`, `_ms`, `_deg`). OpenSky reports SI units
# (metres, metres/second) while aviation thresholds in Phase 3 are written in
# feet and knots. A bare `altitude` invites someone to compare metres against a
# 1,500 ft threshold and get a plausible-looking wrong answer. Unit-suffixed
# names make that mistake visible at the call site.
_STATE_FIELDS: tuple[str, ...] = (
    "icao24",  # 0  transponder address, lowercase hex
    "callsign",  # 1  padded to 8 chars, may be blank
    "origin_country",  # 2
    "time_position",  # 3  epoch s, null if no position ever received
    "last_contact",  # 4  epoch s, always present
    "longitude",  # 5  degrees, null if unknown
    "latitude",  # 6  degrees, null if unknown
    "baro_altitude_m",  # 7  metres, barometric
    "on_ground",  # 8
    "velocity_ms",  # 9  metres/second over ground
    "true_track_deg",  # 10 degrees clockwise from true north
    "vertical_rate_ms",  # 11 metres/second, positive = climbing
    "sensors",  # 12 receiver ids, usually null
    "geo_altitude_m",  # 13 metres, GNSS
    "squawk",  # 14 transponder code
    "spi",  # 15 special purpose indicator
    "position_source",  # 16 0=ADS-B 1=ASTERIX 2=MLAT 3=FLARM
)

# Header OpenSky uses to report the remaining daily credit budget.
_CREDITS_HEADER = "X-Rate-Limit-Remaining"


class OpenSkyApiError(RuntimeError):
    """A `/states/all` call failed."""


class OpenSkyRateLimitError(OpenSkyApiError):
    """The daily credit budget or a rate limit was exhausted (HTTP 429).

    Its own type because the correct response differs from other failures: back
    off hard and stop polling for a while, rather than retrying promptly. Task
    1.3 branches on this.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _epoch_to_utc(value: object) -> object:
    """Convert Unix epoch seconds to a timezone-aware UTC datetime.

    Aware, never naive. A naive datetime silently adopts whatever timezone the
    reader assumes, and this project correlates aircraft positions with METAR
    observations and BTS schedules across timezones. An off-by-one-hour join is
    the kind of bug that produces a model which looks fine and is wrong.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    return value


EpochSeconds = Annotated[datetime, BeforeValidator(_epoch_to_utc)]


class StateVector(BaseModel):
    """One aircraft's state at one instant, parsed from the positional array."""

    model_config = ConfigDict(frozen=True)

    # Six lowercase hex digits. This pattern is load-bearing as a contract
    # check: if OpenSky ever inserts a field, position 0 becomes a callsign or
    # a country name, and this rejects it immediately. A permissive `str` here
    # would let the whole shifted record through.
    icao24: str = Field(pattern=r"^[0-9a-f]{6}$")

    callsign: str | None = None
    origin_country: str = ""

    time_position: EpochSeconds | None = None
    last_contact: EpochSeconds

    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)

    baro_altitude_m: float | None = None
    geo_altitude_m: float | None = None
    on_ground: bool = False

    velocity_ms: float | None = Field(default=None, ge=0.0)
    true_track_deg: float | None = Field(default=None, ge=0.0, le=360.0)
    # No bounds: negative is descending, positive is climbing.
    vertical_rate_ms: float | None = None

    sensors: tuple[int, ...] | None = None
    squawk: str | None = None
    spi: bool = False
    # Left as a plain int rather than an enum. This is the bronze layer and we
    # do not control the vocabulary; a new source value should not break
    # ingestion of an otherwise perfectly good record.
    position_source: int = 0

    @model_validator(mode="before")
    @classmethod
    def _from_positional_array(cls, value: Any) -> Any:
        """Map OpenSky's unnamed array onto named fields."""
        if isinstance(value, dict):
            return value  # already named (tests, round-trips)

        if not isinstance(value, Sequence) or isinstance(value, str | bytes):
            raise ValueError(
                f"Expected a state-vector array, got {type(value).__name__}. "
                "The OpenSky response shape may have changed."
            )

        # Too short is unambiguously broken. Longer is tolerated: OpenSky
        # appends optional fields (a `category` at index 17 when extended=1 is
        # requested), and rejecting that would break ingestion over a field we
        # do not even read. Insertions cannot be distinguished from appends
        # here, which is exactly why the per-field validators above matter.
        if len(value) < len(_STATE_FIELDS):
            raise ValueError(
                f"Expected at least {len(_STATE_FIELDS)} elements in a state vector, "
                f"got {len(value)}. The OpenSky array layout may have changed."
            )

        return dict(zip(_STATE_FIELDS, value, strict=False))

    @field_validator("callsign", mode="before")
    @classmethod
    def _clean_callsign(cls, value: object) -> object:
        """Strip OpenSky's fixed-width padding; blank becomes None.

        Callsigns arrive right-padded to 8 characters ("DLH9LF  "). Left as-is,
        every downstream group-by would treat "DLH9LF" and "DLH9LF  " as
        different flights.
        """
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class StatesResponse(BaseModel):
    """A parsed `/states/all` response."""

    model_config = ConfigDict(frozen=True)

    time: EpochSeconds
    states: tuple[StateVector, ...] = ()

    # Read from a response header, not the body. Task 1.3 needs it for credit
    # budgeting; capturing it here avoids re-plumbing the call path later.
    credits_remaining: int | None = None

    @field_validator("states", mode="before")
    @classmethod
    def _null_states_is_empty(cls, value: object) -> object:
        """OpenSky sends `"states": null`, not `[]`, when the box is empty.

        A real occurrence, not a hypothetical: a tight bbox at 3am has no
        traffic. Without this, every quiet poll would raise.
        """
        return () if value is None else value


class OpenSkyClient:
    """Thin, typed client for the OpenSky REST API."""

    def __init__(
        self,
        http_client: httpx.Client,
        *,
        base_url: str = DEFAULT_BASE_URL,
        owns_client: bool = False,
        token_provider: OpenSkyTokenProvider | None = None,
    ) -> None:
        """
        Args:
            http_client: Client with `OpenSkyAuth` attached.
            owns_client: Whether close() should close it. False when the caller
                supplied the client, since closing someone else's client is a
                surprising side effect.
            token_provider: Closed alongside the client when we own it.
        """
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._owns_client = owns_client
        self._token_provider = token_provider

    # ---- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._http.close()
            if self._token_provider is not None:
                self._token_provider.close()

    def __enter__(self) -> OpenSkyClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- API ---------------------------------------------------------------

    def get_states(self, bbox: BoundingBox) -> StatesResponse:
        """Fetch all aircraft state vectors inside `bbox`.

        The bbox field names were chosen in `airports.py` to match OpenSky's
        query parameters exactly, so this is a direct dump with no renaming step
        that could transpose a latitude and a longitude.
        """
        url = f"{self._base_url}/states/all"
        params = bbox.model_dump()

        try:
            response = self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise OpenSkyApiError(f"Could not reach the OpenSky API: {exc}") from exc

        self._raise_for_status(response)

        try:
            payload = StatesResponse.model_validate_json(response.content)
        except ValueError as exc:
            raise OpenSkyApiError(
                f"OpenSky /states/all response did not match the expected shape: {exc}"
            ) from exc

        credits_remaining = _parse_credits(response.headers)
        logger.info(
            "opensky states fetched: bbox=(%.3f,%.3f,%.3f,%.3f) aircraft=%d credits_remaining=%s",
            bbox.lamin,
            bbox.lamax,
            bbox.lomin,
            bbox.lomax,
            len(payload.states),
            credits_remaining,
        )
        return payload.model_copy(update={"credits_remaining": credits_remaining})

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise OpenSkyRateLimitError(
                "OpenSky rate limit or daily credit budget exhausted (HTTP 429). "
                "Raise FDP_OPENSKY_POLL_SECONDS, shrink FDP_BBOX_RADIUS_NM, or "
                "reduce FDP_AIRPORTS.",
                retry_after_seconds=_parse_retry_after(response.headers),
            )

        # The auth layer already retried once on a 401 with a fresh token. Still
        # failing means the credentials themselves are the problem, not expiry.
        if response.status_code in (401, 403):
            raise OpenSkyApiError(
                f"OpenSky rejected the request (HTTP {response.status_code}) even after "
                "refreshing the token. Check FDP_OPENSKY_CLIENT_ID and "
                "FDP_OPENSKY_CLIENT_SECRET."
            )

        if response.status_code >= 400:
            raise OpenSkyApiError(f"OpenSky API returned HTTP {response.status_code}.")


def _parse_credits(headers: httpx.Headers) -> int | None:
    raw = headers.get(_CREDITS_HEADER)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        # An unparseable budget header is not worth failing a good response for.
        logger.warning("Could not parse %s header: %r", _CREDITS_HEADER, raw)
        return None


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # Retry-After may also be an HTTP date; task 1.3 falls back to its own
        # backoff schedule when this is absent.
        return None


def client_from_settings(settings: Settings) -> OpenSkyClient:
    """Build a fully authenticated client from application settings.

    Note the two separate httpx clients. The token provider gets its own,
    deliberately WITHOUT auth attached: authenticating the token request with a
    token we do not have yet would recurse forever.
    """
    provider = token_provider_from_settings(settings)
    http_client = httpx.Client(
        auth=OpenSkyAuth(provider),
        timeout=settings.http_timeout_seconds,
    )
    return OpenSkyClient(
        http_client,
        base_url=settings.opensky_base_url,
        owns_client=True,
        token_provider=provider,
    )
