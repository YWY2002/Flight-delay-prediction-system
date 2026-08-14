"""Sample API payloads shared across test modules.

These live in one place so a change to what we believe an upstream response
looks like updates every test at once. Cross-importing between test modules
would work but makes the dependency direction between tests unclear.
"""

from __future__ import annotations

import pathlib
from typing import Any

from flight_delay.common.airports import Airport

# ---- Airports --------------------------------------------------------------

KJFK = Airport(
    icao="KJFK",
    name="John F. Kennedy International",
    lat=40.6398,
    lon=-73.7789,
    metar_station="KJFK",
    faa_code="JFK",
)
KEWR = Airport(
    icao="KEWR",
    name="Newark Liberty International",
    lat=40.6925,
    lon=-74.1687,
    metar_station="KEWR",
    faa_code="EWR",
)

# ---- OpenSky ---------------------------------------------------------------

# One state vector in OpenSky's documented positional order.
# The padded callsign is real, not a typo.
RAW_STATE: list[Any] = [
    "3c6444", "DLH9LF  ", "Germany", 1458564120, 1458564120, -73.78, 40.64,
    9639.3, False, 232.88, 98.26, 4.55, None, 9547.86, "1000", False, 0,
]  # fmt: skip

# ---- Weather ---------------------------------------------------------------

METAR_JSON: dict[str, Any] = {
    "icaoId": "KJFK",
    "obsTime": 1704110400,
    "rawOb": "KJFK 011200Z 27010G18KT 10SM -RA BKN020 OVC035 05/M02 A2993",
    "temp": 5.0,
    "dewp": -2.0,
    "wdir": 270,
    "wspd": 10,
    "wgst": 18,
    "visib": "10+",
    "altim": 1013.2,
    "wxString": "-RA",
    "clouds": [{"cover": "BKN", "base": 2000}, {"cover": "OVC", "base": 3500}],
}

TAF_JSON: dict[str, Any] = {
    "icaoId": "KJFK",
    "validTimeFrom": 1704110400,
    "validTimeTo": 1704196800,
    "rawTAF": "KJFK 011120Z 0112/0218 27010KT P6SM BKN020",
    "fcsts": [
        {
            "timeFrom": 1704110400,
            "timeTo": 1704153600,
            "wdir": 270,
            "wspd": 10,
            "visib": "6+",
            "clouds": [{"cover": "BKN", "base": 2000}],
        }
    ],
}

# ---- FAA -------------------------------------------------------------------

# Closure section element names (`Start`, `Reopen`) are VERIFIED against the
# live feed on 2026-08-08; see tests/fixtures/faa_status_live_2026-08-08.xml.
# The ground-stop and GDP sections were absent that day (no programs active),
# so their shape below still follows the published schema and remains
# UNVERIFIED. Treat those two blocks as an assumption, not a fact.
STATUS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AIRPORT_STATUS_INFORMATION>
  <Update_Time>Sat Aug 8 09:02:20 2026 GMT</Update_Time>
  <Delay_type>
    <Name>Ground Stop Programs</Name>
    <Ground_Stop_List>
      <Program>
        <ARPT>EWR</ARPT>
        <Reason>weather / thunderstorms</Reason>
        <End_Time>18:00</End_Time>
      </Program>
    </Ground_Stop_List>
  </Delay_type>
  <Delay_type>
    <Name>Ground Delay Programs</Name>
    <Ground_Delay_List>
      <Ground_Delay>
        <ARPT>ORD</ARPT>
        <Reason>weather / low ceilings</Reason>
        <Avg>45 minutes</Avg>
      </Ground_Delay>
    </Ground_Delay_List>
  </Delay_type>
  <Delay_type>
    <Name>Airport Closures</Name>
    <Airport_Closure_List>
      <Airport>
        <ARPT>JFK</ARPT>
        <Reason>!JFK 08/001 JFK AD AP CLSD 2608080200-2608080500</Reason>
        <Start>Aug 08 at 02:00 UTC.</Start>
        <Reopen>Aug 08 at 05:00 UTC.</Reopen>
      </Airport>
    </Airport_Closure_List>
  </Delay_type>
</AIRPORT_STATUS_INFORMATION>
"""

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# Real responses captured from the live APIs on 2026-08-08. These are the
# "recorded fixtures" of plan task 1.11: a mock proves we handle the shape we
# BELIEVE in, while these prove the belief itself.
LIVE_FAA_XML = (FIXTURES / "faa_status_live_2026-08-08.xml").read_text(encoding="utf-8")
LIVE_METAR_JSON = (FIXTURES / "metar_kjfk_live_2026-08-08.json").read_text(encoding="utf-8")

EMPTY_STATUS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AIRPORT_STATUS_INFORMATION>
</AIRPORT_STATUS_INFORMATION>
"""
