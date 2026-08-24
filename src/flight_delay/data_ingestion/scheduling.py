"""Wall-clock aligned tick scheduling, shared by the per-source schedulers.

Sources here fire on calendar boundaries rather than after an elapsed delay:
METAR at :01 and :31 past the hour, TAF at 00:01/06:01/12:01/18:01Z. That is a
deliberate difference from a plain `sleep(interval)` loop, and it buys two
things. Ticks land at the same instants on every run and after every restart,
so bronze partitions line up and a gap is visible as a missing tick rather than
as a shifted one. And drift cannot accumulate: a poll that takes four seconds
does not push every later poll four seconds late.

The cost is the clock it must use. Elapsed-time scheduling belongs on
`time.monotonic`, which no NTP correction can move. "Fire at 18:01Z" is a
question about what time it *is*, so it has to read the wall clock, and a
backwards NTP step can therefore replay a tick or a forwards one skip it. That
is the right trade when the whole point is to sit just after a publication
boundary, but it is a trade, not a free lunch.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event

from flight_delay.common.logging_config import get_logger
from flight_delay.data_ingestion.errors import IngestError

logger = get_logger(__name__)


def next_tick(now: float, interval_seconds: float, offset_seconds: float = 0.0) -> float:
    """The first aligned instant strictly after `now`, as a Unix timestamp.

    Ticks sit at `k * interval + offset` for integer k, measured from the Unix
    epoch. Because the epoch begins at midnight UTC and every interval used here
    divides a day evenly, that grid lands on clean UTC boundaries: a 1800s
    interval with a 60s offset gives :01 and :31 past each hour.

    Strictly after, never equal, so a task that finishes inside the same second
    it started cannot immediately re-fire the tick it just served.
    """
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")

    elapsed = now - offset_seconds
    return offset_seconds + (math.floor(elapsed / interval_seconds) + 1) * interval_seconds


@dataclass(frozen=True)
class ScheduledTask:
    """One thing to run, and how often.

    Attributes:
        name: Identifies the task in logs.
        interval_seconds: Spacing of the tick grid.
        run: Performs one poll. Returns the record count, for logging only.
        offset_seconds: Shifts the grid later. Used to sit just past a
            publication boundary rather than exactly on it.
    """

    name: str
    interval_seconds: float
    run: Callable[[], int]
    offset_seconds: float = 0.0


def _epoch_now() -> float:
    return time.time()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_scheduler(
    tasks: Sequence[ScheduledTask],
    *,
    max_ticks: int | None = None,
    stop: Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = _epoch_now,
) -> None:
    """Run `tasks` on their tick grids until stopped.

    Tasks sharing a tick instant all run on the same wake-up, in the order
    given.

    A task whose poll overruns its own interval does not accumulate a backlog:
    the next tick is computed from the time the poll *finished*, so a slow cycle
    skips the grid points it missed instead of firing them back to back.

    Args:
        max_ticks: Stop after this many wake-ups. `None` runs forever; tests
            pass a small number.
        stop: Set it to end the loop, including from another thread. Checked
            before each wake-up and used for the wait itself, so shutdown does
            not have to sit through a six-hour TAF sleep.
        sleep: Injected so tests need not spend real time. Ignored when `stop`
            is given, since the event supplies an interruptible wait.
        clock: Injected so tests can drive the grid directly.

    Raises:
        IngestError: re-raised when the failure is not retryable. See
            `_run_task` for why this stops the loop rather than continuing.
    """
    if not tasks:
        raise ValueError("run_scheduler needs at least one task.")

    now = clock()
    due = {
        task.name: next_tick(now, task.interval_seconds, task.offset_seconds) for task in tasks
    }
    for task in tasks:
        logger.info(
            "scheduler.task.registered",
            task=task.name,
            interval_s=task.interval_seconds,
            offset_s=task.offset_seconds,
            first_due=_iso(due[task.name]),
        )

    consecutive_failures = dict.fromkeys(due, 0)
    ticks = 0

    while max_ticks is None or ticks < max_ticks:
        if stop is not None and stop.is_set():
            logger.info("scheduler.stopped", reason="stop event set", ticks=ticks)
            return

        wake_at = min(due.values())
        if _wait(wake_at - clock(), stop, sleep):
            logger.info("scheduler.stopped", reason="stop event set during wait", ticks=ticks)
            return

        now = clock()
        for task in tasks:
            if due[task.name] > now:
                continue
            _run_task(task, consecutive_failures)
            # Recomputed from the clock *after* the poll, so an overrun skips
            # missed grid points rather than queueing them up.
            due[task.name] = next_tick(clock(), task.interval_seconds, task.offset_seconds)

        ticks += 1


def _wait(delay: float, stop: Event | None, sleep: Callable[[float], None]) -> bool:
    """Wait `delay` seconds. Returns True if the loop should stop."""
    if delay <= 0:
        return False
    if stop is not None:
        return stop.wait(delay)
    sleep(delay)
    return False


def _run_task(task: ScheduledTask, consecutive_failures: dict[str, int]) -> None:
    """Run one poll, deciding what survives and what stops the loop.

    A retryable failure (429, 5xx, a dropped connection) is logged and skipped:
    the next tick will try again, and a scheduler that dies because NOAA
    returned a 502 costs far more than one missed cycle.

    A non-retryable one (400, 401, 403, a malformed request) is re-raised. It
    cannot fix itself, so continuing would leave the process alive and
    collecting nothing, which is the failure mode this codebase treats as worse
    than a crash: silence looks identical to quiet weather. Same reasoning for
    an unexpected exception, which is a bug rather than an outage. Note this
    differs from the multi-source loop in `ingestion_example`, which swallows
    everything precisely because one bad source there must not silence three
    good ones; with a single source per scheduler that argument does not apply.
    """
    try:
        records = task.run()
    except IngestError as exc:
        if not exc.retryable:
            logger.error("scheduler.task.fatal", task=task.name, error=str(exc))
            raise
        consecutive_failures[task.name] += 1
        logger.warning(
            "scheduler.task.failed",
            task=task.name,
            error=str(exc),
            consecutive_failures=consecutive_failures[task.name],
        )
        return
    except Exception:
        logger.exception("scheduler.task.crashed", task=task.name)
        raise

    consecutive_failures[task.name] = 0
    logger.info("scheduler.task.polled", task=task.name, records=records)
