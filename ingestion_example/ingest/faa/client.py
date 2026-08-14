"""FAA NAS Status client: ground stops, ground delay programs, closures.

    https://nasstatus.faa.gov/api/airport-status-information

This endpoint returns XML, not JSON, and its shape is only loosely documented.
Two consequences shape this module:

- **The raw payload is preserved.** Every parsed event carries the source XML
  fragment. If the parser turns out to miss something once real traffic is
  observed, the information is still on disk and can be re-extracted rather
  than re-collected. For a source whose contract we cannot fully verify in
  advance, that is cheap insurance.
- **Parsing is defensive, not strict.** Unlike the OpenSky positional array,
  where a shifted index silently corrupts numbers and must fail loudly, missing
  fields here are expected: the FAA emits different element sets per delay type.
  An unrecognised section is logged and skipped rather than raising.

XML is parsed with `defusedxml`. Standard-library `ElementTree` is documented as
vulnerable to entity-expansion attacks, and this is externally controlled input.

**Not yet verified against the live endpoint.** Element names below follow the
published ATCSCC schema; the contract test exists to fail loudly if reality
differs.
"""

from __future__ import annotations

from datetime import datetime
from xml.etree.ElementTree import Element  # noqa: S405 - types only, parsing uses defusedxml

import httpx
from defusedxml.ElementTree import fromstring
from pydantic import BaseModel, ConfigDict, Field

from flight_delay.common.config import Settings
from flight_delay.common.logging_config import get_logger
from flight_delay.common.timeutil import utc_now
from flight_delay.ingest.errors import IngestError
from flight_delay.ingest.http import raise_for_status

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://nasstatus.faa.gov"

# Section name in the XML -> the event type we record. Normalised here so
# downstream code never has to match on FAA prose.
_DELAY_SECTIONS: dict[str, str] = {
    "Ground Stop Programs": "ground_stop",
    "Ground Delay Programs": "ground_delay",
    "Airport Closures": "closure",
    "Airspace Flow Programs": "airspace_flow",
    "Arrival/Departure Delays": "arrival_departure_delay",
    "Deicing": "deicing",
}


class FaaApiError(IngestError):
    """An FAA NAS status request failed."""


class FaaEvent(BaseModel):
    """One active delay program or closure at one airport."""

    model_config = ConfigDict(frozen=True)

    airport: str = Field(min_length=2, description="FAA 3-letter code, e.g. EWR.")
    event_type: str
    reason: str | None = None
    start_text: str | None = None
    end_text: str | None = None
    avg_delay_text: str | None = None
    # The source fragment, kept verbatim. See the module docstring.
    raw_xml: str | None = None
    observed_at: datetime


def _text(element: Element, *names: str) -> str | None:
    """First non-empty text among the named child elements.

    Several names are accepted per field because the FAA uses different element
    names across delay types for the same concept.
    """
    for name in names:
        child = element.find(name)
        if child is not None and child.text and child.text.strip():
            return child.text.strip()
    return None


def _fragment(element: Element) -> str:
    from xml.etree.ElementTree import tostring  # noqa: S405 - serialising, not parsing

    return tostring(element, encoding="unicode").strip()


def parse_status_xml(xml_text: str, *, observed_at: datetime | None = None) -> list[FaaEvent]:
    """Parse the NAS status document into events.

    Returns an empty list when nothing is active, which is the common case on a
    clear day and must not be mistaken for a failure.
    """
    observed_at = observed_at or utc_now()

    try:
        root = fromstring(xml_text)
    except Exception as exc:
        raise FaaApiError(f"FAA NAS status returned unparseable XML: {exc}") from exc

    events: list[FaaEvent] = []

    for delay_type in root.iter("Delay_type"):
        name = _text(delay_type, "Name")
        event_type = _DELAY_SECTIONS.get(name or "")
        if event_type is None:
            # A new section is informational, not fatal: the rest of the
            # document is still perfectly usable.
            logger.debug("faa.unknown_delay_section", section=name)
            continue

        for airport_element in _iter_airport_elements(delay_type):
            airport = _text(airport_element, "ARPT", "Arpt")
            if not airport:
                logger.debug("faa.event_without_airport", section=name)
                continue

            events.append(
                FaaEvent(
                    airport=airport.upper(),
                    event_type=event_type,
                    reason=_text(airport_element, "Reason"),
                    # `Start` and `Reopen` are what the live feed actually
                    # sends for closures, confirmed against nasstatus.faa.gov
                    # on 2026-08-08 (see tests/fixtures/). The `_Time` variants
                    # were an assumption that produced silently-null timestamps:
                    # airport and reason parsed fine, so nothing looked broken.
                    # The other spellings are kept as fallbacks because the
                    # ground-stop and GDP sections were absent that day and
                    # remain unverified.
                    start_text=_text(airport_element, "Start", "Start_Time", "Begin_Time"),
                    end_text=_text(airport_element, "Reopen", "End_Time", "Stop_Time", "End"),
                    avg_delay_text=_text(airport_element, "Avg", "Avg_Delay", "Arrival_Departure"),
                    raw_xml=_fragment(airport_element),
                    observed_at=observed_at,
                )
            )

    return events


def _iter_airport_elements(delay_type: Element) -> list[Element]:
    """Elements that describe one airport's program.

    The FAA nests these under per-type list wrappers (`Ground_Stop_List`,
    `Airport_Closure_List`, ...), so rather than enumerating every wrapper name
    we take any element carrying an `ARPT` child.

    Only `ARPT` counts, not `Airport`: the closure section wraps its records in
    an `<Airport_Closure_List>` whose children are `<Airport>` elements, so
    matching on `Airport` would treat the wrapper itself as a record and emit a
    spurious skip for every closure.
    """
    return [
        element
        for element in delay_type.iter()
        if element is not delay_type
        and any(element.find(tag) is not None for tag in ("ARPT", "Arpt"))
    ]


class FaaClient:
    """Client for the FAA NAS status endpoint."""

    def __init__(
        self,
        http_client: httpx.Client,
        *,
        base_url: str = DEFAULT_BASE_URL,
        owns_client: bool = False,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._owns_client = owns_client

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> FaaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_status(self) -> list[FaaEvent]:
        """Fetch all currently active delay programs, nationwide.

        The endpoint has no airport filter, so this returns everything and
        callers filter. That is fine and even useful: a ground stop at ORD is
        relevant context for EWR, which is the cascade behaviour this project
        exists to model.
        """
        url = f"{self._base_url}/api/airport-status-information"

        try:
            response = self._http.get(url, headers={"Accept": "application/xml"})
        except httpx.HTTPError as exc:
            raise FaaApiError(f"Could not reach FAA NAS status: {exc}") from exc

        raise_for_status(response, source="FAA NAS status")

        events = parse_status_xml(response.text)
        logger.debug("faa.fetched", events=len(events))
        return events


def faa_events_for(events: list[FaaEvent], faa_codes: set[str]) -> list[FaaEvent]:
    """Filter nationwide events down to the airports we track."""
    return [event for event in events if event.airport in faa_codes]


def client_from_settings(settings: Settings) -> FaaClient:
    return FaaClient(
        httpx.Client(timeout=settings.http_timeout_seconds),
        base_url=settings.faa_base_url,
        owns_client=True,
    )
