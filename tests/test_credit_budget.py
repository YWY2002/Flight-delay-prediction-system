"""Tests for daily API credit budgeting.

The clock is injected, so crossing a UTC midnight takes microseconds instead of
waiting until midnight.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from structlog.testing import capture_logs

from flight_delay.ingest.opensky.credit_budget import CreditBudget, CreditBudgetExhausted


class FakeClock:
    """A controllable wall clock.

    Wall clock, not monotonic: budget reset is "has the UTC calendar date
    changed", which a monotonic counter cannot answer. Deliberately the opposite
    choice from token expiry in opensky_auth.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def make_budget(limit: int = 100, clock: FakeClock | None = None) -> CreditBudget:
    return CreditBudget(limit, clock=clock or FakeClock())


# ---- Basic accounting ------------------------------------------------------


def test_starts_with_the_full_allowance() -> None:
    assert make_budget(100).remaining() == 100


def test_consume_decrements() -> None:
    budget = make_budget(100)
    budget.consume(1)
    budget.consume(4)
    assert budget.remaining() == 95


def test_snapshot_reports_the_current_state() -> None:
    clock = FakeClock()
    budget = make_budget(100, clock)
    budget.consume(30)

    snap = budget.snapshot()

    assert snap.limit == 100
    assert snap.used == 30
    assert snap.remaining == 70
    assert snap.day == clock.now.date()


def test_rejects_a_nonpositive_limit() -> None:
    with pytest.raises(ValueError, match="daily_limit must be positive"):
        CreditBudget(0)


def test_rejects_nonpositive_consumption() -> None:
    with pytest.raises(ValueError, match="credits must be positive"):
        make_budget().consume(0)


# ---- The gate --------------------------------------------------------------


def test_exhausting_the_budget_raises_before_any_request() -> None:
    """The whole point: refuse the call rather than discover a 429."""
    budget = make_budget(3)
    for _ in range(3):
        budget.consume(1)

    with pytest.raises(CreditBudgetExhausted, match="daily credit budget exhausted"):
        budget.consume(1)


def test_exhaustion_message_names_the_knobs_to_turn() -> None:
    budget = make_budget(1)
    budget.consume(1)

    with pytest.raises(CreditBudgetExhausted) as err:
        budget.consume(1)

    assert "FDP_OPENSKY_POLL_SECONDS" in str(err.value)
    assert "FDP_BBOX_RADIUS_NM" in str(err.value)


def test_exhaustion_is_not_retryable() -> None:
    """Retrying cannot conjure credits, and the budget will not refill for
    hours. This flag is what stops the retry policy from looping on it."""
    assert CreditBudgetExhausted.retryable is False


def test_a_rejected_consume_does_not_charge() -> None:
    budget = make_budget(5)
    budget.consume(5)

    with pytest.raises(CreditBudgetExhausted):
        budget.consume(3)

    assert budget.remaining() == 0
    assert budget.snapshot().used == 5


def test_partial_room_still_rejects_an_oversized_request() -> None:
    budget = make_budget(10)
    budget.consume(8)

    with pytest.raises(CreditBudgetExhausted):
        budget.consume(3)


# ---- Day rollover ----------------------------------------------------------


def test_budget_resets_at_utc_midnight() -> None:
    clock = FakeClock(datetime(2026, 8, 7, 23, 59, tzinfo=UTC))
    budget = make_budget(10, clock)
    budget.consume(10)
    assert budget.remaining() == 0

    clock.advance(minutes=2)  # crosses into 2026-08-08 UTC

    assert budget.remaining() == 10


def test_no_reset_within_the_same_day() -> None:
    clock = FakeClock(datetime(2026, 8, 7, 1, 0, tzinfo=UTC))
    budget = make_budget(10, clock)
    budget.consume(4)

    clock.advance(hours=20)  # still 2026-08-07

    assert budget.remaining() == 6


def test_consume_succeeds_again_after_rollover() -> None:
    clock = FakeClock(datetime(2026, 8, 7, 23, 0, tzinfo=UTC))
    budget = make_budget(2, clock)
    budget.consume(2)
    with pytest.raises(CreditBudgetExhausted):
        budget.consume(1)

    clock.advance(hours=2)

    budget.consume(1)
    assert budget.remaining() == 1


# ---- Reconciliation with the server ---------------------------------------


def test_server_header_overrides_local_counting() -> None:
    """Local counting drifts across restarts and across processes sharing an
    account. The server knows the truth."""
    budget = make_budget(1000)
    budget.consume(5)

    budget.reconcile(server_remaining=200)

    assert budget.remaining() == 200


def test_reconcile_can_reveal_more_usage_than_we_counted() -> None:
    """Another poller on the same account, or a restart that lost the counter."""
    budget = make_budget(1000)
    budget.consume(1)

    budget.reconcile(server_remaining=10)

    assert budget.remaining() == 10
    with pytest.raises(CreditBudgetExhausted):
        budget.consume(11)


def test_reconcile_above_the_configured_limit_is_clamped() -> None:
    """Happens when the account is on a higher tier than configured. Should not
    produce negative usage."""
    budget = make_budget(100)
    budget.consume(50)

    budget.reconcile(server_remaining=8000)

    assert budget.snapshot().used == 0
    assert budget.remaining() == 100


def test_reconcile_ignores_a_negative_value() -> None:
    budget = make_budget(100)
    budget.consume(10)
    budget.reconcile(server_remaining=-1)
    assert budget.remaining() == 90


def test_reconcile_respects_day_rollover() -> None:
    clock = FakeClock(datetime(2026, 8, 7, 23, 59, tzinfo=UTC))
    budget = make_budget(100, clock)
    budget.reconcile(server_remaining=3)
    assert budget.remaining() == 3

    clock.advance(minutes=2)

    assert budget.remaining() == 100


# ---- Low-water warning -----------------------------------------------------


def test_low_water_warning_fires_once() -> None:
    """Latched on purpose: a poller running every 90 s would otherwise emit this
    hundreds of times a day and train everyone to ignore it."""
    budget = CreditBudget(100, clock=FakeClock(), low_water_fraction=0.1)
    budget.consume(89)

    with capture_logs() as logs:
        budget.consume(1)  # 10 remaining, at the threshold
        budget.consume(1)
        budget.consume(1)

    warnings = [e for e in logs if e["event"] == "credits.running_low"]
    assert len(warnings) == 1


def test_low_water_warning_carries_structured_fields() -> None:
    """The payoff of structured logging: the numbers are fields, so "when did
    credits start dropping" is a filter rather than a regex against prose."""
    budget = CreditBudget(100, clock=FakeClock(), low_water_fraction=0.1)
    budget.consume(89)

    with capture_logs() as logs:
        budget.consume(1)

    warning = next(e for e in logs if e["event"] == "credits.running_low")
    assert warning["remaining"] == 10
    assert warning["limit"] == 100
    assert warning["log_level"] == "warning"


def test_low_water_warning_resets_after_rollover() -> None:
    clock = FakeClock(datetime(2026, 8, 7, 23, 0, tzinfo=UTC))
    budget = CreditBudget(10, clock=clock, low_water_fraction=0.5)
    budget.consume(6)

    clock.advance(hours=2)

    with capture_logs() as logs:
        budget.consume(6)

    assert any(e["event"] == "credits.running_low" for e in logs)


def test_day_rollover_is_logged() -> None:
    clock = FakeClock(datetime(2026, 8, 7, 23, 59, tzinfo=UTC))
    budget = CreditBudget(100, clock=clock)
    budget.consume(40)

    clock.advance(minutes=2)

    with capture_logs() as logs:
        budget.remaining()

    rolled = next(e for e in logs if e["event"] == "credits.day_rolled")
    assert rolled["previous_used"] == 40
    assert rolled["day"] == "2026-08-08"
