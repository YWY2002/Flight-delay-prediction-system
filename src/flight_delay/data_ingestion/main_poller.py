from flight_delay.common.config import Settings, get_settings
from opensky_api import OpenSkyApi, _count_utc_dates, FlightData, TokenManager
from flight_delay.data_ingestion.opensky.client import TrackedOpenSkyApi
from flight_delay.data_ingestion.opensky.scheduler import wsss_bounding_box, run_states_scheduler
from flight_delay.data_ingestion.weather.poller import poll_taf_once, poll_metar_once, weather_http_client, WeatherPollingDetails
from flight_delay.data_ingestion.weather.scheduler import run_weather_scheduler
from flight_delay.common.airports import load_airports
from flight_delay.data_ingestion.opensky.poller import (
    poll_states_once,
    PollingDetails
)
from flight_delay.common.timeutil import (
    utc_now, 
    utc_to_epoch, 
    get_today_midnight_sgt, 
    get_today_midnight_utc, 
    get_today_2am_utc,
    get_today_6am_utc,
    get_today_12pm_utc,
    get_today_6pm_utc)
import time

def main() -> None:
    settings = get_settings()
    client_id, client_secret = settings.require_opensky_credentials()
    client = TrackedOpenSkyApi(token_manager=TokenManager(client_id, client_secret))
    bbox = wsss_bounding_box(settings)

    # print(poll_states_once(client, bbox))
    # http = weather_http_client(settings)
    # details = WeatherPollingDetails(settings=settings, stations=("WSSS",))
    # metars = poll_metar_once(http, details)
    # tafs   = poll_taf_once(http, details)
    # run_states_scheduler(client, bbox, BronzeWriterfunction)



if __name__ == "__main__":
    main()
# ['76d107', 1786765784, 'WSSS', 1786767879, None, 'TGW226  ', 11895, 991, 0, 0, 19, 0]
