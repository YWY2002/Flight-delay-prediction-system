"""Tests for the OpenSky /states/all client.

The bulk of these are contract tests. Parsing an unnamed positional array is the
single most fragile point in the whole ingestion path: a field inserted upstream
shifts every value by one slot, nothing raises, and the pipeline keeps producing
confidently wrong numbers. These tests exist to turn that silent corruption into
a loud failure.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from flight_delay.common.airports import Airport, BoundingBox
from flight_delay.common.config import Settings
from flight_delay.ingest.opensky_client import (
    OpenSkyApiError,
    OpenSkyClient,
    OpenSkyRateLimitError,
    StatesResponse,
    StateVector,
    client_from_settings,
)

BBOX = BoundingBox(lamin=39.64, lamax=41.64, lomin=-75.10, lomax=-72.46)

# A realistic state vector, in OpenSky's documented positional order.
# Note the padded callsign: that padding is real, not a typo.
RAW_STATE: list[Any] = [
    "3c6444",  # 0  icao24
    "DLH9LF  ",  # 1  callsign (right-padded to 8)
    "Germany",  # 2  origin_country
    1458564120,  # 3  time_position
    1458564120,  # 4  last_contact
    6.1546,  # 5  longitude
    50.1964,  # 6  latitude
    9639.3,  # 7  baro_altitude_m
    False,  # 8  on_ground
    232.88,  # 9  velocity_ms
    98.26,  # 10 true_track_deg
    4.55,  # 11 vertical_rate_ms
    None,  # 12 sensors
    9547.86,  # 13 geo_altitude_m
    "1000",  # 14 squawk
    False,  # 15 spi
    0,  # 16 position_source
]


def states_payload(*states: list[Any], time: int = 1458564121) -> dict[str, Any]:
    return {"time": time, "states": list(states)}


Handler = Callable[[httpx.Request], httpx.Response]


def make_client(
    handler: Handler,
    *,
    seen: list[httpx.Request] | None = None,
) -> OpenSkyClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return OpenSkyClient(
        httpx.Client(transport=httpx.MockTransport(wrapped)),
        base_url="https://api.example.test",
    )


def ok(payload: dict[str, Any], **headers: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, headers=headers)

    return handler


# ---- Parsing the positional array -----------------------------------------


def test_parses_every_field_by_position() -> None:
    """Pins the full index-to-name mapping. If OpenSky reorders anything, this
    is the test that says so."""
    sv = StateVector.model_validate(RAW_STATE)

    assert sv.icao24 == "3c6444"
    assert sv.callsign == "DLH9LF"
    assert sv.origin_country == "Germany"
    # epoch 1458564120 == 2016-03-21T12:42:00Z
    assert sv.last_contact == datetime(2016, 3, 21, 12, 42, tzinfo=UTC)
    assert sv.longitude == pytest.approx(6.1546)
    assert sv.latitude == pytest.approx(50.1964)
    assert sv.baro_altitude_m == pytest.approx(9639.3)
    assert sv.on_ground is False
    assert sv.velocity_ms == pytest.approx(232.88)
    assert sv.true_track_deg == pytest.approx(98.26)
    assert sv.vertical_rate_ms == pytest.approx(4.55)
    assert sv.sensors is None
    assert sv.geo_altitude_m == pytest.approx(9547.86)
    assert sv.squawk == "1000"
    assert sv.spi is False
    assert sv.position_source == 0


def test_timestamps_are_timezone_aware_utc() -> None:
    """Naive datetimes silently adopt the reader's assumed timezone, and this
    project joins aircraft data against METAR and BTS across timezones."""
    sv = StateVector.model_validate(RAW_STATE)
    assert sv.last_contact.tzinfo is not None
    assert sv.last_contact.utcoffset() == UTC.utcoffset(None)


def test_callsign_padding_is_stripped() -> None:
    """Without stripping, 'DLH9LF' and 'DLH9LF  ' group as different flights."""
    assert StateVector.model_validate(RAW_STATE).callsign == "DLH9LF"


def test_blank_callsign_becomes_none() -> None:
    raw = list(RAW_STATE)
    raw[1] = "        "
    assert StateVector.model_validate(raw).callsign is None


def test_null_position_is_tolerated() -> None:
    """Aircraft with no position fix are still real records. Bronze keeps them;
    filtering is the silver layer's job."""
    raw = list(RAW_STATE)
    raw[5] = None  # longitude
    raw[6] = None  # latitude

    sv = StateVector.model_validate(raw)

    assert sv.longitude is None
    assert sv.latitude is None
    assert sv.icao24 == "3c6444"


def test_extra_trailing_field_is_tolerated() -> None:
    """OpenSky appends a `category` at index 17 when extended=1. Rejecting an
    appended field we do not even read would break ingestion for no reason."""
    sv = StateVector.model_validate([*RAW_STATE, 3])
    assert sv.icao24 == "3c6444"


# ---- Contract: shape changes must fail loudly ------------------------------


def test_short_array_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 17 elements"):
        StateVector.model_validate(RAW_STATE[:10])


def test_inserted_field_is_caught_by_the_icao24_pattern() -> None:
    """The scenario this whole module is defensive about: a field inserted at
    the front shifts every value one slot right. Without a strict icao24
    pattern, latitude would silently become altitude and the pipeline would
    carry on producing garbage."""
    shifted = ["EXTRA", *RAW_STATE]

    with pytest.raises(ValidationError):
        StateVector.model_validate(shifted)


def test_scalar_instead_of_array_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Expected a state-vector array"):
        StateVector.model_validate("3c6444")


def test_out_of_range_latitude_is_rejected() -> None:
    """A second line of defence against silent index drift."""
    raw = list(RAW_STATE)
    raw[6] = 1234.5
    with pytest.raises(ValidationError):
        StateVector.model_validate(raw)


def test_state_vector_is_immutable() -> None:
    sv = StateVector.model_validate(RAW_STATE)
    with pytest.raises(ValidationError):
        sv.latitude = 0.0


# ---- Response envelope -----------------------------------------------------


def test_null_states_becomes_empty_tuple() -> None:
    """A tight bbox at 3am genuinely returns `"states": null`. Every quiet poll
    would raise without this."""
    response = StatesResponse.model_validate({"time": 1458564121, "states": None})
    assert response.states == ()


def test_parses_multiple_states() -> None:
    second = list(RAW_STATE)
    second[0] = "abc123"
    client = make_client(ok(states_payload(RAW_STATE, second)))

    result = client.get_states(BBOX)

    assert [s.icao24 for s in result.states] == ["3c6444", "abc123"]


# ---- Request construction --------------------------------------------------


def test_sends_bbox_as_opensky_query_params() -> None:
    """The bbox fields were named lamin/lamax/lomin/lomax back in airports.py
    precisely so this is a direct dump with no renaming step that could
    transpose a latitude and a longitude."""
    seen: list[httpx.Request] = []
    client = make_client(ok(states_payload()), seen=seen)

    client.get_states(BBOX)

    params = seen[0].url.params
    assert seen[0].url.path == "/states/all"
    assert float(params["lamin"]) == pytest.approx(39.64)
    assert float(params["lamax"]) == pytest.approx(41.64)
    assert float(params["lomin"]) == pytest.approx(-75.10)
    assert float(params["lomax"]) == pytest.approx(-72.46)


def test_bbox_from_a_real_airport_round_trips() -> None:
    """End to end from reference data: airport -> bbox -> query params."""
    kjfk = Airport(
        icao="KJFK",
        name="JFK",
        lat=40.6398,
        lon=-73.7789,
        metar_station="KJFK",
        faa_code="JFK",
    )
    seen: list[httpx.Request] = []
    client = make_client(ok(states_payload()), seen=seen)

    client.get_states(kjfk.bounding_box(radius_nm=60.0))

    params = seen[0].url.params
    assert float(params["lamin"]) < kjfk.lat < float(params["lamax"])
    assert float(params["lomin"]) < kjfk.lon < float(params["lomax"])


# ---- Credits and failure modes --------------------------------------------


def test_credits_remaining_is_captured_from_the_header() -> None:
    client = make_client(ok(states_payload(RAW_STATE), **{"X-Rate-Limit-Remaining": "3994"}))
    assert client.get_states(BBOX).credits_remaining == 3994


def test_missing_credits_header_is_not_an_error() -> None:
    client = make_client(ok(states_payload(RAW_STATE)))
    assert client.get_states(BBOX).credits_remaining is None


def test_unparseable_credits_header_does_not_fail_the_response() -> None:
    """A junk budget header is not worth discarding a perfectly good payload."""
    client = make_client(ok(states_payload(RAW_STATE), **{"X-Rate-Limit-Remaining": "lots"}))
    assert client.get_states(BBOX).credits_remaining is None


def test_rate_limit_raises_its_own_type() -> None:
    """Distinct type because the correct reaction differs: back off hard rather
    than retry promptly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={}, headers={"Retry-After": "120"})

    with pytest.raises(OpenSkyRateLimitError) as err:
        make_client(handler).get_states(BBOX)

    assert err.value.retry_after_seconds == 120.0
    assert "FDP_OPENSKY_POLL_SECONDS" in str(err.value)


def test_rate_limit_error_is_an_api_error() -> None:
    """Callers that do not care about the distinction can catch the base type."""
    assert issubclass(OpenSkyRateLimitError, OpenSkyApiError)


@pytest.mark.parametrize("status", [401, 403])
def test_persistent_auth_failure_names_the_credentials(status: int) -> None:
    """The auth layer already retried once with a fresh token, so reaching here
    means the credentials themselves are wrong, not merely expired."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    with pytest.raises(OpenSkyApiError, match="FDP_OPENSKY_CLIENT_ID"):
        make_client(handler).get_states(BBOX)


def test_server_error_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    with pytest.raises(OpenSkyApiError, match="503"):
        make_client(handler).get_states(BBOX)


def test_transport_failure_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(OpenSkyApiError, match="Could not reach"):
        make_client(handler).get_states(BBOX)


def test_malformed_body_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(OpenSkyApiError, match="did not match the expected shape"):
        make_client(handler).get_states(BBOX)


# ---- Wiring ----------------------------------------------------------------


def test_client_from_settings_requires_credentials() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="FDP_OPENSKY_CLIENT_ID"):
        client_from_settings(settings)


def test_client_from_settings_builds_an_authenticated_client() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        opensky_client_id="id-1",
        opensky_client_secret=SecretStr("secret-1"),
        opensky_base_url="https://api.example.test",
    )
    with client_from_settings(settings) as client:
        assert isinstance(client, OpenSkyClient)


def test_client_does_not_close_a_borrowed_http_client() -> None:
    borrowed = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with OpenSkyClient(borrowed):
        pass
    assert not borrowed.is_closed
