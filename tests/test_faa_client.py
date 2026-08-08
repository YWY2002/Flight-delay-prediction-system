"""Tests for the FAA NAS status client.

The XML shape here is inferred from the published ATCSCC schema and has not been
verified against the live endpoint, so these tests pin what we believe the
contract to be. If reality differs, they are what will say so.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from flight_delay.ingest.errors import IngestError
from flight_delay.ingest.faa_client import (
    FaaApiError,
    FaaClient,
    faa_events_for,
    parse_status_xml,
)
from tests.samples import EMPTY_STATUS_XML, STATUS_XML

WHEN = datetime(2026, 8, 7, 14, 0, tzinfo=UTC)


def make_client(handler: Any) -> FaaClient:
    return FaaClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://faa.example.test",
    )


def xml_handler(body: str, status: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return handler


# ---- Parsing ---------------------------------------------------------------


def test_parses_all_delay_types() -> None:
    events = parse_status_xml(STATUS_XML, observed_at=WHEN)

    assert {(e.airport, e.event_type) for e in events} == {
        ("EWR", "ground_stop"),
        ("ORD", "ground_delay"),
        ("JFK", "closure"),
    }


def test_event_fields_are_extracted() -> None:
    events = parse_status_xml(STATUS_XML, observed_at=WHEN)
    stop = next(e for e in events if e.airport == "EWR")

    assert stop.reason == "weather / thunderstorms"
    assert stop.end_text == "18:00"
    assert stop.observed_at == WHEN


def test_delay_type_names_are_normalised() -> None:
    """Downstream code should never have to match on FAA prose like
    "Ground Stop Programs", which is free to be reworded upstream."""
    events = parse_status_xml(STATUS_XML, observed_at=WHEN)

    assert {e.event_type for e in events} == {"ground_stop", "ground_delay", "closure"}
    assert all(e.event_type.islower() and " " not in e.event_type for e in events)


def test_raw_xml_fragment_is_preserved() -> None:
    """The endpoint's contract is only loosely documented, so keeping the source
    fragment means anything the parser misses can be recovered from disk rather
    than re-collected."""
    stop = next(e for e in parse_status_xml(STATUS_XML) if e.airport == "EWR")
    assert stop.raw_xml is not None
    assert "EWR" in stop.raw_xml


def test_quiet_period_returns_no_events() -> None:
    """A clear day is the common case and must not look like a failure."""
    assert parse_status_xml(EMPTY_STATUS_XML) == []


def test_unknown_delay_section_is_skipped_not_fatal() -> None:
    """A new FAA section is informational. The rest of the document is still
    perfectly usable, and losing it would be a self-inflicted outage."""
    xml = STATUS_XML.replace("Airport Closures", "Some Brand New Program")

    events = parse_status_xml(xml)

    assert {e.airport for e in events} == {"EWR", "ORD"}


def test_event_without_an_airport_is_skipped() -> None:
    xml = STATUS_XML.replace("<ARPT>EWR</ARPT>", "")
    assert "EWR" not in {e.airport for e in parse_status_xml(xml)}


def test_list_wrappers_are_not_mistaken_for_records() -> None:
    """The closure section wraps records in <Airport_Closure_List>, whose
    children are <Airport> elements. Matching loosely would count the wrapper
    itself as a record."""
    events = parse_status_xml(STATUS_XML, observed_at=WHEN)
    assert len(events) == 3


def test_malformed_xml_fails_loudly() -> None:
    with pytest.raises(FaaApiError, match="unparseable XML"):
        parse_status_xml("<AIRPORT_STATUS_INFORMATION><unclosed>")


def test_airport_codes_are_uppercased() -> None:
    xml = STATUS_XML.replace("<ARPT>EWR</ARPT>", "<ARPT>ewr</ARPT>")
    assert "EWR" in {e.airport for e in parse_status_xml(xml)}


# ---- Filtering -------------------------------------------------------------


def test_filters_to_tracked_airports() -> None:
    """The endpoint is nationwide with no filter, so filtering happens here."""
    events = parse_status_xml(STATUS_XML, observed_at=WHEN)

    matched = faa_events_for(events, {"EWR", "JFK"})

    assert {e.airport for e in matched} == {"EWR", "JFK"}


def test_filtering_uses_faa_codes_not_icao() -> None:
    """KEWR would match nothing. This is why airports.toml carries faa_code."""
    events = parse_status_xml(STATUS_XML, observed_at=WHEN)
    assert faa_events_for(events, {"KEWR"}) == []


# ---- HTTP ------------------------------------------------------------------


def test_fetches_the_status_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=STATUS_XML)

    events = make_client(handler).get_status()

    assert seen[0].url.path == "/api/airport-status-information"
    assert len(events) == 3


def test_server_error_is_retryable() -> None:
    with pytest.raises(IngestError) as err:
        make_client(xml_handler("", status=502)).get_status()
    assert err.value.retryable is True


def test_transport_failure_raises_faa_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(FaaApiError, match="Could not reach"):
        make_client(handler).get_status()
