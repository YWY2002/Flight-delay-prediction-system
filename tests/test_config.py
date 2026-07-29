"""Tests for deployment settings.

Every test constructs `Settings(_env_file=None, ...)` explicitly. That argument
matters: without it, pydantic-settings would read the developer's local `.env`,
making results depend on an untracked file. Such a test passes on the machine
that wrote it and fails in CI (or worse, the reverse). Config tests must be
hermetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from flight_delay.common.airports import load_airports, resolve_active_airports
from flight_delay.common.config import MIN_OPENSKY_POLL_SECONDS, Settings


def make_settings(**overrides: Any) -> Settings:
    """Build Settings ignoring any ambient .env file.

    `**overrides: Any` rather than `object`: pydantic accepts pre-validation
    input types (e.g. a comma-separated str for the `airports` tuple), which the
    statically synthesised __init__ signature does not describe.

    The ignore is for `_env_file`, a real pydantic-settings runtime parameter
    that mypy cannot see because the model's __init__ signature is synthesised
    from declared fields.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# ---- Defaults --------------------------------------------------------------


def test_defaults_are_usable_without_any_env() -> None:
    """A fresh clone with no .env must still construct valid settings."""
    settings = make_settings()

    assert settings.airports == ("KJFK", "KEWR", "KORD")
    assert settings.bbox_radius_nm == 60.0
    assert settings.opensky_poll_seconds >= MIN_OPENSKY_POLL_SECONDS
    assert settings.opensky_client_id is None


def test_derived_layer_dirs_hang_off_data_dir() -> None:
    """Relocating the lake is one env var; the layers cannot disagree."""
    settings = make_settings(data_dir=Path("/lake"))

    assert settings.bronze_dir == Path("/lake/bronze")
    assert settings.silver_dir == Path("/lake/silver")
    assert settings.gold_dir == Path("/lake/gold")


# ---- Parsing / validation --------------------------------------------------


def test_airports_parse_from_comma_separated_string() -> None:
    """`FDP_AIRPORTS=kjfk, kewr` is what a human types; accept and normalise it."""
    settings = make_settings(airports="kjfk, kewr ")
    assert settings.airports == ("KJFK", "KEWR")


def test_empty_airport_list_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(airports="")


def test_poll_interval_below_credit_floor_is_rejected() -> None:
    """The OpenSky credit budget is a hard domain constraint. An unsafe value
    must fail at startup, not drain the daily quota unnoticed."""
    with pytest.raises(ValidationError):
        make_settings(opensky_poll_seconds=MIN_OPENSKY_POLL_SECONDS - 1)


def test_poll_interval_at_the_floor_is_allowed() -> None:
    settings = make_settings(opensky_poll_seconds=MIN_OPENSKY_POLL_SECONDS)
    assert settings.opensky_poll_seconds == MIN_OPENSKY_POLL_SECONDS


def test_negative_bbox_radius_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(bbox_radius_nm=-1.0)


def test_settings_are_immutable() -> None:
    settings = make_settings()
    with pytest.raises(ValidationError):
        settings.bbox_radius_nm = 10.0


# ---- Environment plumbing --------------------------------------------------


def test_env_vars_use_the_fdp_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDP_AIRPORTS", "KORD")
    monkeypatch.setenv("FDP_BBOX_RADIUS_NM", "25")

    settings = make_settings()

    assert settings.airports == ("KORD",)
    assert settings.bbox_radius_nm == 25.0


def test_unprefixed_env_var_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare DATA_DIR belonging to some other tool must not leak into ours."""
    monkeypatch.setenv("DATA_DIR", "/somebody/elses/path")
    assert make_settings().data_dir != Path("/somebody/elses/path")


# ---- Secrets ---------------------------------------------------------------


def test_missing_credentials_raise_an_actionable_error() -> None:
    with pytest.raises(RuntimeError, match="FDP_OPENSKY_CLIENT_ID"):
        make_settings().require_opensky_credentials()


def test_credentials_returned_when_configured() -> None:
    settings = make_settings(opensky_client_id="id-123", opensky_client_secret="shh")
    assert settings.require_opensky_credentials() == ("id-123", "shh")


def test_secret_is_masked_in_repr() -> None:
    """SecretStr keeps credentials out of logs and tracebacks -- the most common
    way secrets escape is an exception dump, not a git commit."""
    settings = make_settings(opensky_client_id="id-123", opensky_client_secret="hunter2")
    assert "hunter2" not in repr(settings)


# ---- The two config layers together ---------------------------------------


def test_default_airports_all_exist_in_reference_file() -> None:
    """Cross-check between the layers: every airport in the shipped defaults must
    resolve against the committed reference data. Catches the case where someone
    edits one file and forgets the other."""
    settings = make_settings()
    reference = load_airports(settings.airports_file)

    resolved = resolve_active_airports(settings.airports, reference)

    assert len(resolved) == len(settings.airports)
    for airport in resolved:
        assert airport.bounding_box(settings.bbox_radius_nm).lamin < airport.lat
