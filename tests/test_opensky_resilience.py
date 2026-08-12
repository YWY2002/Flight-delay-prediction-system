"""Integration tests: budget and retry working together inside the client.

The unit tests in test_credit_budget.py and test_retry.py check each mechanism
alone. These check the interactions, which is where the real bugs are: does a
retry charge the budget, does an exhausted budget skip the network entirely,
does a non-retryable failure escape immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from flight_delay.common.airports import BoundingBox
from flight_delay.ingest.opensky.client import (
    OpenSkyApiError,
    OpenSkyClient,
    OpenSkyRateLimitError,
    OpenSkyServerError,
    OpenSkyTransportError,
)
from flight_delay.ingest.opensky.credit_budget import CreditBudget, CreditBudgetExhausted
from flight_delay.ingest.retry import build_retrying

BBOX = BoundingBox(lamin=39.64, lamax=41.64, lomin=-75.10, lomax=-72.46)

RAW_STATE: list[Any] = [
    "3c6444", "DLH9LF  ", "Germany", 1458564120, 1458564120, 6.1546, 50.1964,
    9639.3, False, 232.88, 98.26, 4.55, None, 9547.86, "1000", False, 0,
]  # fmt: skip

PAYLOAD = {"time": 1458564121, "states": [RAW_STATE]}


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class ScriptedApi:
    """Replays a scripted sequence of responses and counts real requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.request_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        if self._responses:
            response = self._responses.pop(0)
        else:
            response = httpx.Response(200, json=PAYLOAD)
        if isinstance(response, BaseException):  # pragma: no cover - defensive
            raise response
        return response


def build_client(
    api: ScriptedApi,
    *,
    budget: CreditBudget | None = None,
    max_attempts: int = 4,
    sleeper: SleepRecorder | None = None,
) -> OpenSkyClient:
    return OpenSkyClient(
        httpx.Client(transport=httpx.MockTransport(api)),
        base_url="https://api.example.test",
        budget=budget,
        retrying=build_retrying(
            max_attempts=max_attempts,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=10.0,
            sleep=sleeper or SleepRecorder(),
        ),
    )


def ok(**headers: str) -> httpx.Response:
    return httpx.Response(200, json=PAYLOAD, headers=headers)


# ---- Retry inside the client ----------------------------------------------


def test_recovers_from_a_transient_server_error() -> None:
    api = ScriptedApi(httpx.Response(503, json={}), ok())
    client = build_client(api)

    result = client.get_states(BBOX)

    assert len(result.states) == 1
    assert api.request_count == 2


def test_recovers_from_a_rate_limit() -> None:
    api = ScriptedApi(httpx.Response(429, json={}, headers={"Retry-After": "5"}), ok())
    sleeper = SleepRecorder()
    client = build_client(api, sleeper=sleeper)

    client.get_states(BBOX)

    assert api.request_count == 2
    assert sleeper.delays == [5.0]


def test_gives_up_and_reraises_the_original_error() -> None:
    api = ScriptedApi(*[httpx.Response(503, json={}) for _ in range(5)])
    client = build_client(api, max_attempts=3)

    with pytest.raises(OpenSkyServerError, match="503"):
        client.get_states(BBOX)

    assert api.request_count == 3


def test_auth_failure_is_not_retried() -> None:
    """403 after the auth layer already refreshed means the credentials are
    wrong. Hammering them is how access gets suspended."""
    api = ScriptedApi(*[httpx.Response(403, json={}) for _ in range(5)])
    client = build_client(api)

    with pytest.raises(OpenSkyApiError, match="FDP_OPENSKY_CLIENT_ID"):
        client.get_states(BBOX)

    assert api.request_count == 1


def test_malformed_response_is_not_retried() -> None:
    """The same request would return the same malformed body. This needs a
    human, not another attempt."""
    api = ScriptedApi(*[httpx.Response(200, json={"nope": 1}) for _ in range(5)])
    client = build_client(api)

    with pytest.raises(OpenSkyApiError, match="did not match the expected shape"):
        client.get_states(BBOX)

    assert api.request_count == 1


def test_transport_error_is_retried() -> None:
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=PAYLOAD)

    client = OpenSkyClient(
        httpx.Client(transport=httpx.MockTransport(flaky)),
        base_url="https://api.example.test",
        retrying=build_retrying(max_attempts=4, sleep=SleepRecorder()),
    )

    assert len(client.get_states(BBOX).states) == 1
    assert calls == 3


def test_transport_error_type_is_retryable() -> None:
    assert OpenSkyTransportError.retryable is True
    assert OpenSkyServerError.retryable is True
    assert OpenSkyRateLimitError.retryable is True
    assert OpenSkyApiError.retryable is False


# ---- Budget inside the client ---------------------------------------------


def test_each_call_consumes_one_credit() -> None:
    budget = CreditBudget(10, clock=FakeClock())
    api = ScriptedApi()
    client = build_client(api, budget=budget)

    client.get_states(BBOX)
    client.get_states(BBOX)

    assert budget.snapshot().used == 2


def test_exhausted_budget_skips_the_network_entirely() -> None:
    """The gate is proactive: no request is made at all, so an exhausted budget
    costs nothing further."""
    budget = CreditBudget(1, clock=FakeClock())
    api = ScriptedApi()
    client = build_client(api, budget=budget)

    client.get_states(BBOX)
    assert api.request_count == 1

    with pytest.raises(CreditBudgetExhausted):
        client.get_states(BBOX)

    assert api.request_count == 1  # unchanged: nothing was sent


def test_budget_exhaustion_is_not_retried() -> None:
    """Retrying cannot conjure credits. Looping here would waste the retry
    budget and delay the caller for nothing."""
    budget = CreditBudget(1, clock=FakeClock())
    api = ScriptedApi()
    sleeper = SleepRecorder()
    client = build_client(api, budget=budget, sleeper=sleeper)

    client.get_states(BBOX)
    with pytest.raises(CreditBudgetExhausted):
        client.get_states(BBOX)

    assert sleeper.delays == []


def test_retries_are_charged_to_the_budget() -> None:
    """The important interaction. A retried request costs the same as a fresh
    one, so charging only the logical call would let a retry storm spend several
    times its share of the budget that exists to prevent exactly that."""
    budget = CreditBudget(10, clock=FakeClock())
    api = ScriptedApi(
        httpx.Response(503, json={}),
        httpx.Response(503, json={}),
        ok(),
    )
    client = build_client(api, budget=budget)

    client.get_states(BBOX)

    assert api.request_count == 3
    assert budget.snapshot().used == 3


def test_retry_stops_when_the_budget_runs_out_mid_storm() -> None:
    """Budget beats retry: the gate fires inside the loop, so a failing endpoint
    cannot be retried past the daily allowance."""
    budget = CreditBudget(2, clock=FakeClock())
    api = ScriptedApi(*[httpx.Response(503, json={}) for _ in range(10)])
    client = build_client(api, budget=budget, max_attempts=10)

    with pytest.raises(CreditBudgetExhausted):
        client.get_states(BBOX)

    assert api.request_count == 2


def test_server_header_reconciles_the_budget() -> None:
    """Local counting drifts; the server's number wins."""
    budget = CreditBudget(4000, clock=FakeClock())
    api = ScriptedApi(ok(**{"X-Rate-Limit-Remaining": "1500"}))
    client = build_client(api, budget=budget)

    client.get_states(BBOX)

    assert budget.remaining() == 1500


def test_reconciliation_can_trigger_exhaustion_on_the_next_call() -> None:
    """A shared account draining faster than we counted must stop us, not
    surprise us with a 429."""
    budget = CreditBudget(4000, clock=FakeClock())
    api = ScriptedApi(ok(**{"X-Rate-Limit-Remaining": "0"}))
    client = build_client(api, budget=budget)

    client.get_states(BBOX)

    with pytest.raises(CreditBudgetExhausted):
        client.get_states(BBOX)


def test_client_without_a_budget_is_unmetered() -> None:
    """Right for tests and mock transports, never for the live API."""
    api = ScriptedApi()
    client = build_client(api, budget=None)

    for _ in range(5):
        client.get_states(BBOX)

    assert api.request_count == 5
    assert client.budget is None
