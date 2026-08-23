from flight_delay.common.config import Settings, get_settings
from opensky_api import OpenSkyApi, _count_utc_dates, FlightData, TokenManager
from flight_delay.data_ingestion.opensky.client import TrackedOpenSkyApi
from flight_delay.common.airports import load_airports
from flight_delay.data_ingestion.opensky.poller import (
    poll_airport_departure_once,
    poll_airport_arrival_once,
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

    bbox = load_airports(settings.airports_file)["WSSS"].bounding_box(settings.bbox_radius_nm)
    snapshot = poll_states_once(client, bbox)
    print(snapshot)

if __name__ == "__main__":
    main()
# ['76d107', 1786765784, 'WSSS', 1786767879, None, 'TGW226  ', 11895, 991, 0, 0, 19, 0]
