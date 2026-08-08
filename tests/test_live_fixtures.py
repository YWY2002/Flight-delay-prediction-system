"""Tests against REAL responses captured from the live APIs.

These are different in kind from the other test modules, and the distinction is
the whole point:

    A mock proves we correctly handle the shape we BELIEVE the API returns.
    A recorded fixture proves the belief itself was correct.

Everything else in this suite is the first kind. That is why 252 passing tests
still hid a real bug: the FAA closure section sends `<Start>` and `<Reopen>`,
while the hand-written sample XML used `<Start_Time>` and `<End_Time>`. Airport,
reason, and event type all parsed, so nothing looked broken; the timestamps just
came back None. Silent, and invisible to a mock built from the same wrong
assumption as the code.

Fixtures were captured on 2026-08-08 with:

    curl "https://aviationweather.gov/api/data/metar?ids=KJFK&format=json"
    curl "https://nasstatus.faa.gov/api/airport-status-information"

Re-capture them when an upstream contract is suspected to have changed. They are
snapshots of one moment, not a live check: a green run here means "we still
parse what the API sent that day", not "the API still sends this".
"""

from __future__ import annotations

import json

from flight_delay.ingest.faa_client import parse_status_xml
from flight_delay.ingest.weather_client import Metar
from tests.samples import LIVE_FAA_XML, LIVE_METAR_JSON

# ---- METAR -----------------------------------------------------------------


def test_live_metar_parses() -> None:
    payload = json.loads(LIVE_METAR_JSON)
    assert isinstance(payload, list)

    metar = Metar.model_validate(payload[0])

    assert metar.station == "KJFK"
    assert metar.obs_time.tzinfo is not None
    assert metar.raw_text is not None and metar.raw_text.startswith("METAR KJFK")


def test_live_metar_visibility_string_is_handled() -> None:
    """The live response really does send `"10+"` as a string rather than a
    number. This is the case the parser exists for, confirmed not assumed."""
    payload = json.loads(LIVE_METAR_JSON)[0]

    assert payload["visib"] == "10+"
    assert Metar.model_validate(payload).visibility_sm == 10.0


def test_live_metar_omits_absent_optional_fields() -> None:
    """No gust and no significant weather means the keys are ABSENT, not null.
    Optional fields with defaults handle that; required ones would not."""
    payload = json.loads(LIVE_METAR_JSON)[0]

    assert "wgst" not in payload
    assert "wxString" not in payload

    metar = Metar.model_validate(payload)
    assert metar.wind_gust_kt is None
    assert metar.wx_string is None


def test_live_metar_flight_category_is_captured() -> None:
    """`fltCat` is returned by the API and named in plan section 1. It was being
    dropped by `extra="ignore"` until the live response was inspected."""
    payload = json.loads(LIVE_METAR_JSON)[0]

    assert payload["fltCat"] == "VFR"
    assert Metar.model_validate(payload).flight_category == "VFR"


def test_live_metar_clouds_parse() -> None:
    metar = Metar.model_validate(json.loads(LIVE_METAR_JSON)[0])

    assert [c.cover for c in metar.clouds] == ["FEW", "SCT"]
    # FEW and SCT are not a ceiling, so a clear-ish day reports none.
    assert metar.ceiling_ft is None


# ---- FAA -------------------------------------------------------------------


def test_live_faa_xml_parses() -> None:
    events = parse_status_xml(LIVE_FAA_XML)

    assert events, "the captured document contained active closures"
    assert all(len(e.airport) == 3 for e in events)
    assert {e.event_type for e in events} == {"closure"}


def test_live_faa_closure_timestamps_are_extracted() -> None:
    """THE REGRESSION TEST. Before the fix these were both None because the code
    looked for `<Start_Time>` and `<End_Time>` while the feed sends `<Start>`
    and `<Reopen>`. Nothing raised; the data was just quietly incomplete."""
    events = parse_status_xml(LIVE_FAA_XML)

    with_times = [e for e in events if e.start_text and e.end_text]
    assert with_times, "no closure carried both a start and a reopen time"

    sample = with_times[0]
    assert "UTC" in sample.start_text  # type: ignore[operator]
    assert "UTC" in sample.end_text  # type: ignore[operator]


def test_live_faa_reason_is_the_raw_notam() -> None:
    """Reasons are NOTAM text, not tidy prose. Anything downstream that wants
    structure has to parse it, and should not assume a clean phrase."""
    events = parse_status_xml(LIVE_FAA_XML)
    assert any((e.reason or "").startswith("!") for e in events)


def test_live_faa_repeated_section_names_are_all_read() -> None:
    """The live document contains the same <Name> more than once, as separate
    <Delay_type> blocks. Keying sections into a dict would silently keep only
    the last one."""
    assert LIVE_FAA_XML.count("<Name>Airport Closures</Name>") > 1
    assert len(parse_status_xml(LIVE_FAA_XML)) > 2
