"""What a per-source poll function needs to do its job.

Shared, flat, and deliberately not in `poller.py`: the source pollers under
`weather/` and `faa/` take a `SourceContext`, and `poller.py` imports those
pollers to schedule them. Leaving this type next to the scheduler would make
that a circular import.

It carries no scheduling state. When a source runs is `ScheduledSource`'s
business, and that stays with the scheduler.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from flight_delay.common.airports import Airport
from flight_delay.common.config import Settings
from flight_delay.common.timeutil import utc_now
from flight_delay.ingest.bronze import BronzeWriter


@dataclass
class SourceContext:
    """Everything the per-source poll functions need."""

    airports: Sequence[Airport]
    writer: BronzeWriter
    settings: Settings
    clock: Callable[[], datetime] = utc_now
    results: dict[str, int] = field(default_factory=dict)
