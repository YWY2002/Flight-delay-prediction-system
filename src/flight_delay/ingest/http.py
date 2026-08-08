"""Shared HTTP response handling for ingestion clients.

The status-to-error mapping is identical across sources, and getting it subtly
different per client is how one source ends up retrying its 403s while another
gives up on its 503s.
"""

from __future__ import annotations

import httpx

from flight_delay.ingest.errors import IngestError, RateLimitError, TransientIngestError


def parse_retry_after(headers: httpx.Headers) -> float | None:
    """Read `Retry-After` as seconds, if present and numeric.

    The header may also be an HTTP date; that form is not handled here, and the
    retry policy falls back to its own backoff schedule when this returns None.
    """
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def raise_for_status(
    response: httpx.Response,
    *,
    source: str,
    credentials_hint: str | None = None,
) -> None:
    """Map an HTTP status onto the ingestion error taxonomy.

    The split that matters is retryable versus not:
      429  -> RateLimitError, retryable on the server's terms
      5xx  -> TransientIngestError, their problem and usually temporary
      401/403, other 4xx -> IngestError, deterministic and needs a human
    """
    status = response.status_code
    if status < 400:
        return

    if status == 429:
        raise RateLimitError(
            f"{source} rate limited the request (HTTP 429).",
            retry_after_seconds=parse_retry_after(response.headers),
        )

    if status >= 500:
        raise TransientIngestError(f"{source} returned HTTP {status}.")

    if status in (401, 403):
        message = f"{source} rejected the request (HTTP {status})."
        if credentials_hint:
            message = f"{message} {credentials_hint}"
        raise IngestError(message)

    # Remaining 4xx are our fault and deterministic: a bad path, a malformed
    # query. Retrying reproduces them exactly.
    raise IngestError(f"{source} returned HTTP {status}.")
