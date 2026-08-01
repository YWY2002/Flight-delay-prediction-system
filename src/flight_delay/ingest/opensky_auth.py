"""OAuth2 client-credentials authentication for the OpenSky Network API.

OpenSky issues short-lived bearer tokens (~30 min) from a Keycloak endpoint. A
poller running for days therefore needs a token that refreshes itself, and it
needs to do so *before* expiry rather than after a failed request.

Two pieces:
    OpenSkyTokenProvider - fetches, caches, and refreshes the bearer token.
    OpenSkyAuth          - an httpx.Auth that stamps the header on each request
                           and retries once if the server rejects the token.

Nothing here knows about flights. Keeping auth separate from the API client
means the token logic can be tested exhaustively with a fake clock and a mock
transport, with no live network access anywhere in the suite.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from types import TracebackType

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from flight_delay.common.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)

# Refresh this many seconds BEFORE the token actually expires.
#
# Refreshing reactively (wait for a 401, then re-auth) costs one failed request
# on every expiry and adds latency at exactly the wrong moment. A margin also
# absorbs clock skew between us and the auth server, plus the flight time of a
# request that is issued just before expiry and arrives just after.
DEFAULT_REFRESH_SKEW_SECONDS = 60.0


class OpenSkyAuthError(RuntimeError):
    """A bearer token could not be obtained.

    Deliberately distinct from httpx's exceptions so callers can tell "your
    credentials are wrong" (never retry, a human must fix it) apart from "the
    network hiccuped" (retry with backoff, task 1.3).
    """


class _TokenResponse(BaseModel):
    """The subset of the OAuth2 token response we rely on.

    Validated with pydantic rather than raw dict access so a change in the
    provider's response shape fails loudly at the boundary, with a readable
    error, instead of surfacing as a `KeyError` or a `None` token three layers
    away. This is the same contract-test principle as plan task 1.12.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str = Field(min_length=1)
    expires_in: int = Field(gt=0, description="Token lifetime in seconds.")
    token_type: str = "Bearer"


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    # A deadline on the MONOTONIC clock, not a wall-clock timestamp. See the
    # note on `time_source` in OpenSkyTokenProvider for why that matters.
    expires_at: float


class OpenSkyTokenProvider:
    """Fetches and caches an OpenSky bearer token, refreshing it before expiry.

    Usage:
        with OpenSkyTokenProvider(client_id, client_secret) as provider:
            token = provider.get_token()
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        token_url: str = DEFAULT_TOKEN_URL,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
        refresh_skew_seconds: float = DEFAULT_REFRESH_SKEW_SECONDS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            http_client: Optional client to use for the token request. It must
                NOT have `OpenSkyAuth` attached: authenticating the token
                request with a token we do not have yet recurses infinitely.
                Left as None, the provider creates and owns a bare client.
            time_source: Injected clock. Defaults to `time.monotonic` because
                expiry is an ELAPSED DURATION, not a point in wall-clock time.
                `time.time()` jumps whenever NTP corrects the system clock; a
                backward jump would make an expired token look valid and 401
                every subsequent request. Injecting it also lets tests advance
                time instantly instead of sleeping.
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._refresh_skew_seconds = refresh_skew_seconds
        self._time_source = time_source

        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_seconds)

        self._cached: _CachedToken | None = None
        # Serialises refreshes. Without it, N poller threads waking to an
        # expired token would each fire their own token request (a thundering
        # herd) and burn credits N times over for one token.
        self._lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying client, but only if we created it.

        Closing a caller-supplied client would be a surprising side effect on an
        object we do not own.
        """
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenSkyTokenProvider:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ---- public API --------------------------------------------------------

    def get_token(self) -> str:
        """Return a valid bearer token, fetching or refreshing if needed."""
        with self._lock:
            cached = self._cached
            if cached is not None and self._time_source() < cached.expires_at:
                return cached.access_token

            fresh = self._fetch_token()
            self._cached = fresh
            return fresh.access_token

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-authenticates.

        Needed because expiry is not the only way a token dies: the server can
        revoke it early, which we only discover from a 401.
        """
        with self._lock:
            self._cached = None

    # ---- internals ---------------------------------------------------------

    def _fetch_token(self) -> _CachedToken:
        # Read the clock BEFORE the request. Using the post-response time would
        # count the round trip as part of the token's remaining life, silently
        # overestimating how long it stays valid.
        requested_at = self._time_source()

        try:
            response = self._client.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            # Transport-level failure (DNS, TLS, timeout). Transient, so this is
            # the case worth retrying in task 1.3.
            raise OpenSkyAuthError(f"Could not reach the OpenSky token endpoint: {exc}") from exc

        # 4xx here means the credentials themselves are bad. Retrying cannot
        # help, so say exactly which variables to fix rather than emitting a
        # bare status code.
        if response.status_code in (400, 401, 403):
            raise OpenSkyAuthError(
                f"OpenSky rejected the client credentials (HTTP {response.status_code}). "
                "Check FDP_OPENSKY_CLIENT_ID and FDP_OPENSKY_CLIENT_SECRET in your .env. "
                "Create or reset a client at https://opensky-network.org under "
                "Account -> API Client."
            )
        if response.status_code >= 400:
            raise OpenSkyAuthError(f"OpenSky token endpoint returned HTTP {response.status_code}.")

        try:
            payload = _TokenResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise OpenSkyAuthError(
                "OpenSky token response did not match the expected OAuth2 shape. "
                f"The provider may have changed its contract. Details: {exc}"
            ) from exc

        if payload.token_type.lower() != "bearer":
            raise OpenSkyAuthError(
                f"Expected a bearer token, got token_type={payload.token_type!r}."
            )

        # Clamp the skew to half the lifetime. If the server ever issued a very
        # short-lived token (say 30 s), a fixed 60 s margin would place the
        # deadline in the past, so every single call would refresh: a credit-
        # burning hot loop, and precisely the failure the margin exists to avoid.
        effective_skew = min(self._refresh_skew_seconds, payload.expires_in / 2)
        expires_at = requested_at + payload.expires_in - effective_skew

        # Log the lifetime, never the token. Credentials escape through logs and
        # tracebacks far more often than through commits.
        logger.info(
            "Obtained OpenSky bearer token (expires_in=%ss, refreshing %.0fs early)",
            payload.expires_in,
            effective_skew,
        )
        return _CachedToken(access_token=payload.access_token, expires_at=expires_at)


def token_provider_from_settings(
    settings: Settings,
    *,
    http_client: httpx.Client | None = None,
) -> OpenSkyTokenProvider:
    """Build a provider from application settings.

    The single place that turns configuration into a live auth object, so the
    credential check happens once, here, with an actionable error message
    (`require_opensky_credentials`) rather than as a `None` propagating into an
    Authorization header.

    `http_client` is exposed so tests (and any future integration harness) can
    supply a mock transport without reaching into the provider's internals.
    """
    client_id, client_secret = settings.require_opensky_credentials()
    return OpenSkyTokenProvider(
        client_id=client_id,
        client_secret=client_secret,
        token_url=settings.opensky_token_url,
        timeout_seconds=settings.http_timeout_seconds,
        http_client=http_client,
    )


class OpenSkyAuth(httpx.Auth):
    """Attaches the bearer token to outgoing requests, refreshing on rejection.

    Implemented as an `httpx.Auth` so authentication is a property of the
    client, not something every call site has to remember. Forgetting a header
    is a silent 401; forgetting to construct a client is a loud error.
    """

    def __init__(self, provider: OpenSkyTokenProvider) -> None:
        self._provider = provider

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._provider.get_token()}"
        response = yield request

        # Proactive refresh handles ordinary expiry. This handles the rest:
        # server-side revocation, or a token invalidated by a credential reset.
        # Exactly one retry, because the generator yields at most twice: a loop
        # here against genuinely bad credentials would hammer the auth endpoint.
        if response.status_code == 401:
            logger.info("OpenSky returned 401; invalidating token and retrying once")
            self._provider.invalidate()
            request.headers["Authorization"] = f"Bearer {self._provider.get_token()}"
            yield request
