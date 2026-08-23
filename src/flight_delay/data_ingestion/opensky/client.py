"""An OpenSky client that does not throw away the HTTP status code."""

from __future__ import annotations

from typing import Any

from opensky_api import OpenSkyApi
from requests import Response


class TrackedOpenSkyApi(OpenSkyApi):  # type: ignore[misc]  # untyped base, see pyproject
    """``OpenSkyApi`` that remembers the status of the response it just handled.

    Every call in the upstream client funnels through ``_get_json``, which maps
    a 404 to ``[]``, anything else non-200 to ``None``, and drops the response
    object on the floor. That is what makes an exhausted credit budget (429),
    an out-of-range window (403) and a float-typed timestamp (400) all arrive
    at the caller as the same bare ``None``.

    Reimplementing the HTTP layer to recover three bytes of information is not
    worth it, so instead we hang a ``requests`` response hook on the session
    the client already owns. The hook fires on every response, which means
    :attr:`last_status` always describes the call that just returned. Token
    refreshes do not disturb it: ``TokenManager`` posts to the auth server with
    a module-level ``requests.post``, not through this session.

    Not thread-safe, since ``last_status`` is a single slot. Give each thread
    its own client if you ever poll airports concurrently.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_status: int | None = None
        # Counts responses actually received. `last_status` alone cannot tell
        # "the server refused" from "we never sent anything", because
        # `get_states` can bail out at its own client-side rate limiter before
        # issuing a request -- leaving `last_status` holding the *previous*
        # call's code. Snapshot this before a call and compare after to know
        # whether the wire was touched at all.
        self.responses_seen: int = 0
        # `_session` is private to the parent, but subclassing is the intended
        # way to reach it, and the alternative is vendoring `_get_json`.
        self._session.hooks["response"].append(self._record_status)

    def _record_status(self, response: Response, **_: Any) -> None:
        self.last_status = response.status_code
        self.responses_seen += 1
