"""Tests for the METAR/TAF client.

Heavily weighted toward the two messy fields, `visib` and `wdir`, because those
are where silent data loss would happen and where the loss would be *biased*:
both misbehave precisely in bad weather, which is exactly the condition a delay
model needs to see.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from flight_delay.ingest.errors import IngestError, RateLimitError
from flight_delay.ingest.weather_client import (
    Metar,
    Taf,
    WeatherApiError,
    WeatherClient,
)
from tests.samples import METAR_JSON, TAF_JSON


def make_client(handler: Any) -> WeatherClient:
    return WeatherClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://wx.example.test/api/data",
    )


def json_handler(payload: Any, status: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


# ---- METAR parsing ---------------------------------------------------------


def test_parses_a_full_metar() -> None:
    metar = Metar.model_validate(METAR_JSON)

    assert metar.station == "KJFK"
    assert metar.obs_time == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert metar.temp_c == pytest.approx(5.0)
    assert metar.dewpoint_c == pytest.approx(-2.0)
    assert metar.wind_dir_deg == 270
    assert metar.wind_speed_kt == pytest.approx(10)
    assert metar.wind_gust_kt == pytest.approx(18)
    assert metar.wx_string == "-RA"
    assert metar.raw_text is not None


def test_obs_time_is_timezone_aware() -> None:
    assert Metar.model_validate(METAR_JSON).obs_time.tzinfo is not None


def test_raw_report_text_is_preserved() -> None:
    """METAR text is the authoritative form. Keeping it means anything we failed
    to parse today can be recovered later without re-collecting."""
    assert "27010G18KT" in (Metar.model_validate(METAR_JSON).raw_text or "")


# ---- Visibility: the messy field -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10+", 10.0),
        ("10", 10.0),
        (10, 10.0),
        (3.5, 3.5),
        ("6+", 6.0),
        ("1/2", 0.5),
        ("1 1/2", 1.5),
        ("1/4", 0.25),
        ("10SM", 10.0),
        (None, None),
    ],
)
def test_visibility_forms_all_parse(raw: object, expected: float | None) -> None:
    """NOAA sends this as a number sometimes and a string others. Fractions
    appear in LOW visibility, which is exactly the weather that matters: parsing
    them loosely would null out the worst-weather rows and bias the model."""
    metar = Metar.model_validate({**METAR_JSON, "visib": raw})
    if expected is None:
        assert metar.visibility_sm is None
    else:
        assert metar.visibility_sm == pytest.approx(expected)


def test_unparseable_visibility_becomes_none_not_an_error() -> None:
    """One odd field must not discard an otherwise good observation."""
    metar = Metar.model_validate({**METAR_JSON, "visib": "unknown"})
    assert metar.visibility_sm is None
    assert metar.station == "KJFK"


# ---- Wind direction: the other messy field ---------------------------------


def test_variable_wind_direction_is_not_an_error() -> None:
    """`wdir` is "VRB" when the wind is variable. That is a real, meaningful
    observation near an airport, so it must not reject the record."""
    metar = Metar.model_validate({**METAR_JSON, "wdir": "VRB"})
    assert metar.wind_dir_deg is None
    assert metar.wind_speed_kt == pytest.approx(10)


def test_variable_wind_is_distinguished_from_not_reported() -> None:
    variable = Metar.model_validate(
        {**METAR_JSON, "wdir": "VRB", "rawOb": "KJFK 011200Z VRB04KT 10SM"}
    )
    missing = Metar.model_validate({**METAR_JSON, "wdir": None, "rawOb": "KJFK 011200Z"})

    assert variable.wind_variable is True
    assert missing.wind_variable is False


# ---- Clouds and ceiling ----------------------------------------------------


def test_cloud_layers_are_parsed() -> None:
    clouds = Metar.model_validate(METAR_JSON).clouds
    assert [c.cover for c in clouds] == ["BKN", "OVC"]
    assert [c.base_ft for c in clouds] == [2000, 3500]


def test_ceiling_is_the_lowest_broken_or_overcast_layer() -> None:
    assert Metar.model_validate(METAR_JSON).ceiling_ft == 2000


def test_scattered_layers_are_not_a_ceiling() -> None:
    """A ceiling is BKN or worse. Counting SCT would understate how good the
    conditions actually are."""
    payload = {**METAR_JSON, "clouds": [{"cover": "SCT", "base": 1200}]}
    assert Metar.model_validate(payload).ceiling_ft is None


def test_clear_sky_has_no_ceiling() -> None:
    assert Metar.model_validate({**METAR_JSON, "clouds": []}).ceiling_ft is None


def test_null_clouds_becomes_empty() -> None:
    assert Metar.model_validate({**METAR_JSON, "clouds": None}).clouds == ()


# ---- Contract --------------------------------------------------------------


def test_unknown_fields_are_ignored() -> None:
    """NOAA returns many fields we do not use; a new one must not break
    ingestion."""
    metar = Metar.model_validate({**METAR_JSON, "someNewField": 42})
    assert metar.station == "KJFK"


def test_missing_station_is_rejected() -> None:
    """The join key. A record without it cannot be attributed to an airport."""
    payload = {k: v for k, v in METAR_JSON.items() if k != "icaoId"}
    with pytest.raises(Exception, match="icaoId"):
        Metar.model_validate(payload)


# ---- TAF -------------------------------------------------------------------


def test_parses_a_taf_with_periods() -> None:
    taf = Taf.model_validate(TAF_JSON)

    assert taf.station == "KJFK"
    assert taf.valid_from == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert len(taf.periods) == 1
    assert taf.periods[0].wind_speed_kt == pytest.approx(10)
    assert taf.periods[0].visibility_sm == pytest.approx(6.0)


def test_taf_with_no_periods_is_allowed() -> None:
    assert Taf.model_validate({**TAF_JSON, "fcsts": None}).periods == ()


# ---- HTTP behaviour --------------------------------------------------------


def test_requests_all_stations_in_one_call() -> None:
    """N airports cost one round trip, not N."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[METAR_JSON])

    make_client(handler).get_metar(["KJFK", "KEWR", "KORD"])

    assert len(seen) == 1
    assert seen[0].url.params["ids"] == "KJFK,KEWR,KORD"
    assert seen[0].url.params["format"] == "json"
    assert seen[0].url.path.endswith("/metar")


def test_taf_hits_the_taf_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[TAF_JSON])

    make_client(handler).get_taf(["KJFK"])
    assert seen[0].url.path.endswith("/taf")


def test_empty_station_list_makes_no_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    assert make_client(handler).get_metar([]) == []
    assert not called


def test_bare_object_response_is_wrapped() -> None:
    """A single-station query can return an object rather than a list."""
    assert len(make_client(json_handler(METAR_JSON)).get_metar(["KJFK"])) == 1


def test_empty_response_is_not_an_error() -> None:
    assert make_client(json_handler([])).get_metar(["KJFK"]) == []


def test_server_error_is_retryable() -> None:
    with pytest.raises(IngestError) as err:
        make_client(json_handler({}, status=503)).get_metar(["KJFK"])
    assert err.value.retryable is True


def test_rate_limit_raises_rate_limit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={}, headers={"Retry-After": "30"})

    with pytest.raises(RateLimitError) as err:
        make_client(handler).get_metar(["KJFK"])
    assert err.value.retry_after_seconds == 30.0


def test_client_error_is_not_retryable() -> None:
    with pytest.raises(IngestError) as err:
        make_client(json_handler({}, status=404)).get_metar(["KJFK"])
    assert err.value.retryable is False


def test_transport_failure_raises_weather_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(WeatherApiError, match="Could not reach"):
        make_client(handler).get_metar(["KJFK"])


def test_non_list_payload_fails_loudly() -> None:
    with pytest.raises(WeatherApiError, match="Expected a list"):
        make_client(json_handler("nonsense")).get_metar(["KJFK"])
