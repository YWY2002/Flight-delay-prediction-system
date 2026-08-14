"""Daily API credit budgeting for OpenSky.

OpenSky meters usage in credits that refill once per day (roughly 400 anonymous,
4,000 registered, 8,000 if you feed ADS-B back). Exhausting the budget does not
merely fail one call: it takes ingestion down for the rest of the day, and this
project's whole schedule depends on accumulating an unbroken history.

So the budget is a PROACTIVE gate. It refuses a request we cannot afford rather
than discovering the problem from a 429. Retrying is reactive and belongs in
`retry.py`; conflating the two produces a client that retries its way through a
budget it has already spent.

Note the clock here is the WALL clock, not the monotonic clock used for token
expiry in `opensky_auth.py`. The two answer different questions. Token expiry is
"how much time has passed", which must be immune to NTP adjustments. Budget
reset is "has the calendar date changed in UTC", which is inherently a wall-clock
question and cannot be answered by a monotonic counter at all.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import ClassVar

from flight_delay.common.logging_config import get_logger

logger = get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CreditBudgetExhausted(RuntimeError):
    """The daily credit budget is spent, so the request was never attempted.

    Explicitly NOT retryable. Retrying would be worse than useless: the budget
    does not refill for hours, and every attempt burns more of tomorrow's
    allowance if the day happens to roll over mid-loop. The caller should skip
    this poll cycle and try again later.
    """

    retryable: ClassVar[bool] = False


@dataclass(frozen=True)
class BudgetSnapshot:
    """A point-in-time view, for logging and (in Phase 8) metrics."""

    day: date
    limit: int
    used: int
    remaining: int


class CreditBudget:
    """Tracks daily API credit consumption and refuses to overspend.

    Two sources of truth are reconciled:

    - **Local counting.** Every attempt increments the counter. Always
      available, but it only knows about requests this process made, so it
      undercounts when several processes share one account, or after a restart.
    - **The server's `X-Rate-Limit-Remaining` header.** Authoritative, but only
      arrives with a response, so it cannot gate the first request.

    Local counting gates; the server header corrects. See `reconcile`.
    """

    def __init__(
        self,
        daily_limit: int,
        *,
        clock: Callable[[], datetime] = _utc_now,
        low_water_fraction: float = 0.1,
    ) -> None:
        """
        Args:
            daily_limit: Credits available per UTC day.
            clock: Injected so tests can cross a day boundary without waiting
                until midnight.
            low_water_fraction: Log a warning below this fraction remaining.
                An early signal that the poll interval or bbox size needs
                revisiting, well before ingestion actually stops.
        """
        if daily_limit <= 0:
            raise ValueError(f"daily_limit must be positive, got {daily_limit}")

        self._daily_limit = daily_limit
        self._clock = clock
        self._low_water = max(1, int(daily_limit * low_water_fraction))

        self._lock = threading.Lock()
        self._day = clock().date()
        self._used = 0
        self._warned_low = False

    # ---- internals ---------------------------------------------------------

    def _roll_day_if_needed(self) -> None:
        """Reset the counter when the UTC date changes. Caller holds the lock."""
        today = self._clock().date()
        if today != self._day:
            logger.info(
                "credits.day_rolled",
                day=str(today),
                previous_day=str(self._day),
                previous_used=self._used,
                limit=self._daily_limit,
            )
            self._day = today
            self._used = 0
            self._warned_low = False

    def _warn_if_low(self) -> None:
        """Caller holds the lock."""
        remaining = self._daily_limit - self._used
        if remaining <= self._low_water and not self._warned_low:
            # Latched so a busy poller does not emit this every 90 seconds for
            # the rest of the day.
            self._warned_low = True
            logger.warning(
                "credits.running_low",
                remaining=remaining,
                limit=self._daily_limit,
                day=str(self._day),
                hint=("Consider raising FDP_OPENSKY_POLL_SECONDS or shrinking FDP_BBOX_RADIUS_NM."),
            )

    # ---- public API --------------------------------------------------------

    @property
    def daily_limit(self) -> int:
        return self._daily_limit

    def remaining(self) -> int:
        with self._lock:
            self._roll_day_if_needed()
            return max(0, self._daily_limit - self._used)

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            self._roll_day_if_needed()
            return BudgetSnapshot(
                day=self._day,
                limit=self._daily_limit,
                used=self._used,
                remaining=max(0, self._daily_limit - self._used),
            )

    def consume(self, credits: int = 1) -> None:
        """Reserve `credits` for a request about to be made.

        Called once per HTTP ATTEMPT, retries included. A retried request costs
        the same as a fresh one, so charging only the logical call would let a
        retry storm silently overspend the budget it is supposed to protect.

        Raises:
            CreditBudgetExhausted: if the request would exceed the budget. The
                request is not attempted.
        """
        if credits <= 0:
            raise ValueError(f"credits must be positive, got {credits}")

        with self._lock:
            self._roll_day_if_needed()

            if self._used + credits > self._daily_limit:
                raise CreditBudgetExhausted(
                    f"OpenSky daily credit budget exhausted: {self._used}/{self._daily_limit} "
                    f"used on {self._day}. The budget refills at UTC midnight. Raise "
                    "FDP_OPENSKY_POLL_SECONDS, shrink FDP_BBOX_RADIUS_NM, or reduce "
                    "FDP_AIRPORTS."
                )

            self._used += credits
            self._warn_if_low()

    def reconcile(self, server_remaining: int) -> None:
        """Correct local accounting from the server's authoritative header.

        Local counting drifts for reasons that are entirely normal: the process
        restarted, another poller shares the account, or a request was billed
        differently than assumed. The server knows the truth, so its number wins
        whenever we have it.
        """
        if server_remaining < 0:
            return

        with self._lock:
            self._roll_day_if_needed()

            if server_remaining > self._daily_limit:
                # Configured limit understates reality (for example the account
                # was upgraded to the 8,000-credit ADS-B feeder tier). Harmless
                # but worth surfacing, since it means we are throttling
                # ourselves unnecessarily.
                logger.warning(
                    "credits.limit_understated",
                    server_remaining=server_remaining,
                    configured_limit=self._daily_limit,
                    hint="Consider raising FDP_OPENSKY_DAILY_CREDITS.",
                )

            self._used = max(0, self._daily_limit - server_remaining)
            self._warn_if_low()
