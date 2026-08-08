"""Time helpers shared across ingestion sources.

Every timestamp in this system is timezone-aware UTC, never naive. Aircraft
positions get joined against METAR observations, FAA advisories, and BTS
schedules across timezones; a naive datetime silently adopts whatever timezone
the reader assumes, and an off-by-one-hour join yields a model that looks fine
and is wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BeforeValidator


def epoch_to_utc(value: object) -> object:
    """Convert Unix epoch seconds to an aware UTC datetime, passing through
    anything that is not a number."""
    # `bool` is an `int` subclass in Python; excluding it stops a stray True
    # from silently becoming 1970-01-01T00:00:01Z.
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    return value


EpochSeconds = Annotated[datetime, BeforeValidator(epoch_to_utc)]


def utc_now() -> datetime:
    return datetime.now(UTC)
