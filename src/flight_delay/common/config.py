"""Central deployment configuration, loaded from environment / `.env`.

Split of responsibilities:
    config.py     - settings that VARY BY ENVIRONMENT (paths, intervals, secrets)
    airports.toml - domain facts that do NOT vary (coordinates, station ids)

Precedence, highest first: explicit kwargs -> environment variables -> `.env`
file -> field defaults. So CI and Docker override with real env vars, while a
local `.env` keeps day-to-day development frictionless.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repo root, derived from this file's location:
#   src/flight_delay/common/config.py -> parents[3] == project root
# Fine because we always run from a checkout or a container with the source
# mounted. If this package were ever pip-installed into site-packages, these
# defaults would point somewhere meaningless -- at which point set FDP_DATA_DIR
# and FDP_AIRPORTS_FILE explicitly instead of relying on the defaults.
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
        # Immutable after construction: config read at step 40 of a pipeline is
        # guaranteed identical to config read at step 1.
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
        default=("KJFK", "KEWR", "KORD"),
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

    # ---- Storage -----------------------------------------------------------
    data_dir: Path = Field(
        default=PROJECT_ROOT / "data",
        description="Root of the local data lake. Gitignored.",
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
