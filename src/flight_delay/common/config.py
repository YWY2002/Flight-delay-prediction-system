from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repo root, derived from this file's location:
#   src/flight_delay/common/config.py -> parents[3] == project root
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Floor on the OpenSky poll interval. This is a DOMAIN constraint, not a
# preference: the free tier has a daily credit budget, and a tight loop burns it
# in minutes (plan §14). Encoding it here means an unsafe value fails at startup
# instead of silently exhausting the quota at 3am.
MIN_OPENSKY_POLL_SECONDS = 60.0


class Settings(BaseSettings):
    """Typed, validated application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Namespace every variable: FDP_DATA_DIR, not DATA_DIR. Prevents
        # collisions with unrelated vars already in the shell environment.
        env_prefix="FDP_",
        # Ignore unknown FDP_* vars rather than crashing, so a stale entry in
        # someone's .env doesn't block the whole app.
        extra="ignore",
        # Immutable after construction
        frozen=True,
    )

    # ---- Scope -------------------------------------------------------------
    # `NoDecode` is load-bearing. Without it, pydantic-settings classifies a
    # tuple-typed field as "complex" and runs json.loads() on the raw env value
    # inside the environment source -- which happens BEFORE any field_validator.
    # `FDP_AIRPORTS=KJFK,KEWR` would then die in the JSON decoder and never reach
    # the comma-splitting validator below. NoDecode passes the raw string
    # through, leaving parsing to us.
    airports: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("WSSS",),
        description="Active airports. Each must exist in the airport reference file.",
    )
    airports_file: Path = Field(
        default=PROJECT_ROOT / "config" / "airports.toml",
        description="Path to the airport reference data.",
    )
    bbox_radius_nm: float = Field(
        default=60.0,
        gt=0.0,
        le=250.0,
        description="Half-width of the OpenSky query box around each airport, nautical miles.",
    )

    # ---- Poll intervals ----------------------------------------------------
    opensky_poll_seconds: float = Field(
        default=90.0,
        ge=MIN_OPENSKY_POLL_SECONDS,
        description="Interval between OpenSky /states/all polls per bounding box.",
    )
    metar_poll_seconds: float = Field(
        default=600.0,
        gt=0.0,
        description="METARs publish hourly, plus off-cycle SPECIs; 10 min catches SPECIs.",
    )
    taf_poll_seconds: float = Field(
        default=3600.0,
        gt=0.0,
        description="TAFs are issued every 6 h; hourly polling is ample.",
    )
    faa_poll_seconds: float = Field(
        default=300.0,
        gt=0.0,
        description="FAA NAS status: ground stops / GDPs change on a minutes timescale.",
    )

    # ---- HTTP --------------------------------------------------------------
    opensky_base_url: str = Field(
        default="https://opensky-network.org/api",
        description="OpenSky REST API root.",
    )
    weather_base_url: str = Field(
        default="https://aviationweather.gov/api/data",
        description="NOAA aviationweather.gov data API root (METAR, TAF).",
    )
    faa_base_url: str = Field(
        default="https://nasstatus.faa.gov",
        description="FAA NAS status root (ground stops, GDPs).",
    )
    aircraft_database_url: str = Field(
        default=(
            "https://opensky-network.org/datasets/metadata/aircraft-database-complete-2026-01.csv"
        ),
        description=(
            "OpenSky aircraft metadata CSV snapshot. Versioned by month upstream, "
            "so this needs bumping when refreshing."
        ),
    )
    opensky_token_url: str = Field(
        default=(
            "https://auth.opensky-network.org/auth/realms/opensky-network"
            "/protocol/openid-connect/token"
        ),
        description="OAuth2 client-credentials token endpoint (Keycloak).",
    )
    http_timeout_seconds: float = Field(
        default=10.0,
        gt=0.0,
        description=(
            "Timeout for every outbound HTTP call. Explicit on purpose: a request "
            "with no timeout can hang forever and silently wedge the poller, which "
            "looks like 'the data stopped' rather than like an error."
        ),
    )

    # ---- Credit budget -----------------------------------------------------
    opensky_daily_credits: int = Field(
        default=4000,
        gt=0,
        description=(
            "Daily OpenSky credit allowance. Roughly 400 anonymous, 4000 registered, "
            "8000 for ADS-B feeders. Set this to your actual tier: too high and the "
            "budget gate never fires, too low and we throttle ourselves needlessly."
        ),
    )

    # ---- Retry / backoff ---------------------------------------------------
    opensky_max_retry_attempts: int = Field(
        default=4,
        ge=1,
        description=(
            "Total attempts including the first. Bounded so a persistent outage "
            "surfaces as a failed poll cycle rather than a task wedged forever."
        ),
    )
    opensky_initial_backoff_seconds: float = Field(
        default=1.0,
        gt=0.0,
        description="First retry delay; doubles with jitter thereafter.",
    )
    opensky_max_backoff_seconds: float = Field(
        default=60.0,
        gt=0.0,
        description="Ceiling on any single backoff, including a server Retry-After.",
    )

    # ---- Storage -----------------------------------------------------------
    data_dir: Path = Field(
        default=PROJECT_ROOT / "data",
        description="Root of the local data lake. Gitignored.",
    )

    # ---- Logging -----------------------------------------------------------
    log_level: str = Field(default="INFO", description="Root log level.")
    log_json: bool = Field(
        default=False,
        description=(
            "JSON logs for containers and log shippers; human-readable console "
            "output for local development. Content is identical either way."
        ),
    )

    # ---- Secrets -----------------------------------------------------------
    # Optional so the app imports, tests run, and CI passes without credentials.
    # Code that actually needs them calls require_opensky_credentials(), which
    # fails loudly at the point of use. See the note on that method.
    opensky_client_id: str | None = Field(
        default=None,
        description="OpenSky OAuth2 client id. Set via FDP_OPENSKY_CLIENT_ID.",
    )
    opensky_client_secret: SecretStr | None = Field(
        default=None,
        description="OpenSky OAuth2 client secret. Set via FDP_OPENSKY_CLIENT_SECRET.",
    )

    @field_validator("airports", mode="before")
    @classmethod
    def _parse_airport_list(cls, value: object) -> object:
        """Accept `FDP_AIRPORTS=KJFK,KEWR` instead of demanding JSON.

        pydantic-settings parses collection-typed fields as JSON by default,
        which would force `FDP_AIRPORTS='["KJFK","KEWR"]'` in the .env file.
        Comma-separated is what anyone would type by hand, so accept that and
        normalise case here at the boundary.
        """
        if isinstance(value, str):
            return tuple(part.strip().upper() for part in value.split(",") if part.strip())
        return value

    @field_validator("airports")
    @classmethod
    def _require_at_least_one_airport(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("At least one airport must be configured (FDP_AIRPORTS).")
        return value

    # ---- Derived paths -----------------------------------------------------
    # Medallion layout: bronze = raw as received, silver = cleaned and typed,
    # gold = modelling-ready features. Derived from data_dir so relocating the
    # lake is a single env var, and the three layers can never disagree.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def bronze_dir(self) -> Path:
        return self.data_dir / "bronze"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def silver_dir(self) -> Path:
        return self.data_dir / "silver"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gold_dir(self) -> Path:
        return self.data_dir / "gold"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reference_dir(self) -> Path:
        """Slowly-changing lookup tables, replaced wholesale rather than
        appended. Separate from bronze, which is an append-only observation log
        whose partitions are never rewritten."""
        return self.data_dir / "reference"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def aircraft_reference_path(self) -> Path:
        return self.reference_dir / "aircraft.parquet"

    # ---- Secret access -----------------------------------------------------
    def require_opensky_credentials(self) -> tuple[str, str]:
        """Return (client_id, client_secret), or raise with a fixable message.

        Deliberately not a required field. "Fail fast at startup" is the right
        instinct, but scoped to what a process actually uses: the training job
        and the API server have no business refusing to boot because an
        ingestion credential is absent. So the check moves to the point of use,
        where the error can name exactly what to set.
        """
        if self.opensky_client_id is None or self.opensky_client_secret is None:
            raise RuntimeError(
                "OpenSky credentials are not configured. Set FDP_OPENSKY_CLIENT_ID and "
                "FDP_OPENSKY_CLIENT_SECRET in your .env (see .env.example). Create a "
                "client at https://opensky-network.org/ under Account -> API Client."
            )
        return self.opensky_client_id, self.opensky_client_secret.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so config is parsed once and every module observes identical values.
    The trade-off is hidden global state: any test that manipulates the
    environment must call `get_settings.cache_clear()` first, or it will see a
    value cached by an earlier test. Tests that need specific values should
    construct `Settings(...)` directly instead -- explicit beats ambient.
    """
    return Settings()
