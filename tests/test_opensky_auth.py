"""Tests for OpenSky OAuth2 client-credentials auth.

No live network and no sleeping. Two injection points make that possible:

    httpx.MockTransport - answers requests in-process, and records what was
                          actually sent so we can assert on the request itself.
    FakeClock           - advances time instantly, so a 30-minute token expiry
                          is tested in microseconds.

A test suite that sleeps to test expiry is a suite nobody runs.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from flight_delay.common.config import Settings
from flight_delay.ingest.opensky_auth import (
    OpenSkyAuth,
    OpenSkyAuthError,
    OpenSkyTokenProvider,
    token_provider_from_settings,
)

TOKEN_URL = "https://auth.example.test/token"
API_URL = "https://api.example.test/states/all"


class FakeClock:
    """A monotonic clock under test control."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TokenEndpoint:
    """A stand-in OpenSky token endpoint that records the requests it receives."""

    def __init__(
        self,
        *,
        expires_in: int = 1800,
        status_code: int = 200,
        body: dict[str, object] | None = None,
    ) -> None:
        self.expires_in = expires_in
        self.status_code = status_code
        self.body = body
        self.requests: list[httpx.Request] = []
        self.call_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.call_count += 1

        if self.body is not None:
            return httpx.Response(self.status_code, json=self.body)
        if self.status_code >= 400:
            return httpx.Response(self.status_code, json={"error": "nope"})

        return httpx.Response(
            200,
            json={
                "access_token": f"token-{self.call_count}",
                "expires_in": self.expires_in,
                "token_type": "Bearer",
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def make_provider(
    endpoint: TokenEndpoint,
    clock: FakeClock,
    *,
    refresh_skew_seconds: float = 60.0,
) -> OpenSkyTokenProvider:
    return OpenSkyTokenProvider(
        client_id="test-id",
        client_secret="test-secret",
        token_url=TOKEN_URL,
        http_client=endpoint.client(),
        refresh_skew_seconds=refresh_skew_seconds,
        time_source=clock,
    )


# ---- The token request itself ---------------------------------------------


def test_sends_client_credentials_grant() -> None:
    """Contract test: assert the exact form body OAuth2 requires."""
    endpoint = TokenEndpoint()
    provider = make_provider(endpoint, FakeClock())

    provider.get_token()

    sent = endpoint.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == TOKEN_URL
    body = dict(pair.split("=", 1) for pair in sent.content.decode().split("&"))
    assert body["grant_type"] == "client_credentials"
    assert body["client_id"] == "test-id"
    assert body["client_secret"] == "test-secret"


def test_returns_the_access_token() -> None:
    provider = make_provider(TokenEndpoint(), FakeClock())
    assert provider.get_token() == "token-1"


# ---- Caching and refresh ---------------------------------------------------


def test_token_is_cached_across_calls() -> None:
    """Re-authenticating on every call would waste credits and add latency."""
    endpoint = TokenEndpoint(expires_in=1800)
    provider = make_provider(endpoint, FakeClock())

    tokens = {provider.get_token() for _ in range(5)}

    assert tokens == {"token-1"}
    assert endpoint.call_count == 1


def test_token_refreshes_after_expiry() -> None:
    endpoint = TokenEndpoint(expires_in=1800)
    clock = FakeClock()
    provider = make_provider(endpoint, clock)

    assert provider.get_token() == "token-1"
    clock.advance(1800)
    assert provider.get_token() == "token-2"
    assert endpoint.call_count == 2


def test_refresh_happens_early_by_the_skew_margin() -> None:
    """The whole point of the margin: refresh BEFORE expiry, so no request is
    ever sent with a token that dies in flight."""
    endpoint = TokenEndpoint(expires_in=1800)
    clock = FakeClock()
    provider = make_provider(endpoint, clock, refresh_skew_seconds=60.0)

    provider.get_token()

    # 1739 s in: still inside the margin, so the cached token is reused.
    clock.advance(1739)
    assert provider.get_token() == "token-1"

    # 1741 s in: past (1800 - 60), so it refreshes despite not having expired.
    clock.advance(2)
    assert provider.get_token() == "token-2"


def test_skew_is_clamped_to_half_the_lifetime() -> None:
    """A 60 s margin on a 30 s token would put the deadline in the past and make
    every single call refresh: a credit-burning hot loop."""
    endpoint = TokenEndpoint(expires_in=30)
    clock = FakeClock()
    provider = make_provider(endpoint, clock, refresh_skew_seconds=60.0)

    provider.get_token()
    clock.advance(1)
    provider.get_token()

    # Skew clamped to 15 s, so the token stays valid for the first 15 s.
    assert endpoint.call_count == 1


def test_invalidate_forces_reauthentication() -> None:
    endpoint = TokenEndpoint()
    provider = make_provider(endpoint, FakeClock())

    assert provider.get_token() == "token-1"
    provider.invalidate()
    assert provider.get_token() == "token-2"


# ---- Failure modes ---------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
def test_bad_credentials_name_the_variables_to_fix(status: int) -> None:
    """Retrying cannot fix wrong credentials, so the error must tell a human
    exactly what to change."""
    provider = make_provider(TokenEndpoint(status_code=status), FakeClock())

    with pytest.raises(OpenSkyAuthError) as err:
        provider.get_token()

    assert "FDP_OPENSKY_CLIENT_ID" in str(err.value)
    assert "FDP_OPENSKY_CLIENT_SECRET" in str(err.value)


def test_server_error_raises_auth_error() -> None:
    provider = make_provider(TokenEndpoint(status_code=503), FakeClock())
    with pytest.raises(OpenSkyAuthError, match="503"):
        provider.get_token()


def test_transport_failure_raises_auth_error() -> None:
    """DNS/TLS/timeout failures must surface as our own typed error, so task 1.3
    can retry these while never retrying bad credentials."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    provider = OpenSkyTokenProvider(
        client_id="test-id",
        client_secret="test-secret",
        token_url=TOKEN_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(explode)),
        time_source=FakeClock(),
    )

    with pytest.raises(OpenSkyAuthError, match="Could not reach"):
        provider.get_token()


@pytest.mark.parametrize(
    "body",
    [
        {"expires_in": 1800},  # no access_token
        {"access_token": "", "expires_in": 1800},  # empty token
        {"access_token": "abc"},  # no expires_in
        {"access_token": "abc", "expires_in": 0},  # nonsensical lifetime
    ],
)
def test_malformed_response_fails_loudly(body: dict[str, object]) -> None:
    """Plan task 1.12: a provider contract change must fail at the boundary with
    a readable error, not surface as a None token three layers away."""
    provider = make_provider(TokenEndpoint(body=body), FakeClock())

    with pytest.raises(OpenSkyAuthError, match="did not match the expected"):
        provider.get_token()


def test_non_bearer_token_type_rejected() -> None:
    body = {"access_token": "abc", "expires_in": 1800, "token_type": "mac"}
    provider = make_provider(TokenEndpoint(body=body), FakeClock())

    with pytest.raises(OpenSkyAuthError, match="bearer"):
        provider.get_token()


def test_secret_never_appears_in_error_messages() -> None:
    """Credentials escape through logs and tracebacks far more often than
    through commits."""
    provider = make_provider(TokenEndpoint(status_code=401), FakeClock())

    with pytest.raises(OpenSkyAuthError) as err:
        provider.get_token()

    assert "test-secret" not in str(err.value)


# ---- httpx.Auth integration ------------------------------------------------


def test_auth_sets_the_bearer_header() -> None:
    endpoint = TokenEndpoint()
    provider = make_provider(endpoint, FakeClock())
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"states": []})

    with httpx.Client(transport=httpx.MockTransport(api), auth=OpenSkyAuth(provider)) as client:
        client.get(API_URL)

    assert seen[0].headers["Authorization"] == "Bearer token-1"


def test_auth_retries_once_on_401_with_a_fresh_token() -> None:
    """Expiry is not the only way a token dies: the server can revoke it early,
    and a 401 is the only signal we get."""
    endpoint = TokenEndpoint()
    provider = make_provider(endpoint, FakeClock())
    seen: list[str] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        # Reject the first token, accept anything after it.
        if len(seen) == 1:
            return httpx.Response(401, json={"error": "revoked"})
        return httpx.Response(200, json={"states": []})

    with httpx.Client(transport=httpx.MockTransport(api), auth=OpenSkyAuth(provider)) as client:
        response = client.get(API_URL)

    assert response.status_code == 200
    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert endpoint.call_count == 2


def test_auth_does_not_loop_on_persistent_401() -> None:
    """A retry loop against genuinely dead credentials would hammer the auth
    endpoint and drain the credit budget. Exactly one retry, then give up."""
    endpoint = TokenEndpoint()
    provider = make_provider(endpoint, FakeClock())
    attempts = 0

    def api(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": "nope"})

    with httpx.Client(transport=httpx.MockTransport(api), auth=OpenSkyAuth(provider)) as client:
        response = client.get(API_URL)

    assert response.status_code == 401
    assert attempts == 2


# ---- Wiring to Settings ----------------------------------------------------


def test_factory_raises_when_credentials_are_absent() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="FDP_OPENSKY_CLIENT_ID"):
        token_provider_from_settings(settings)


def test_factory_builds_a_provider_from_settings() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        opensky_client_id="id-1",
        opensky_client_secret=SecretStr("secret-1"),
    )
    with token_provider_from_settings(settings) as provider:
        assert isinstance(provider, OpenSkyTokenProvider)


def test_factory_unwraps_the_secret_for_the_token_request() -> None:
    """SecretStr must be unwrapped exactly once, at the point of use. Sending
    the literal string 'SecretStr(...)' as a credential would fail with a
    confusing 401 rather than an obvious bug."""
    endpoint = TokenEndpoint()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        opensky_client_id="id-1",
        opensky_client_secret=SecretStr("secret-1"),
        opensky_token_url=TOKEN_URL,
    )
    provider = token_provider_from_settings(settings, http_client=endpoint.client())

    provider.get_token()

    body = dict(pair.split("=", 1) for pair in endpoint.requests[0].content.decode().split("&"))
    assert body["client_secret"] == "secret-1"


def test_provider_closes_only_clients_it_owns() -> None:
    """Closing a caller-supplied client would be a surprising side effect on an
    object the provider does not own."""
    endpoint = TokenEndpoint()
    borrowed = endpoint.client()

    with make_provider(endpoint, FakeClock()):
        pass  # provider used a client we passed in

    assert not borrowed.is_closed

    owned = OpenSkyTokenProvider(client_id="a", client_secret="b")
    owned.close()
