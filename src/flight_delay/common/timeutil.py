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
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")  # UTC+8, no DST

def epoch_to_utc(value: object) -> object:
    """Convert Unix epoch seconds to an aware UTC datetime, passing through
    anything that is not a number."""
    # `bool` is an `int` subclass in Python; excluding it stops a stray True
    # from silently becoming 1970-01-01T00:00:01Z.
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    return value

def utc_to_epoch(utc_time: datetime) -> float:
    return utc_time.timestamp()

EpochSeconds = Annotated[datetime, BeforeValidator(epoch_to_utc)]


def utc_now() -> datetime:
    return datetime.now(UTC)

def get_today_midnight_sgt() -> datetime:
    now_sgt = datetime.now(SGT)
    return now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)

def get_today_midnight_utc() -> datetime:
    return utc_now().replace(hour=0, minute=0, second=0, microsecond=0)

def get_today_2am_utc() -> datetime:
        return utc_now().replace(hour=2, minute=0, second=0, microsecond=0)

def get_today_6am_utc() -> datetime:
        return utc_now().replace(hour=6, minute=0, second=0, microsecond=0)

def get_today_12pm_utc() -> datetime:
        return utc_now().replace(hour=12, minute=0, second=0, microsecond=0)

def get_today_6pm_utc() -> datetime:
        return utc_now().replace(hour=18, minute=0, second=0, microsecond=0)
