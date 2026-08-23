from flight_delay.common.config import Settings, get_settings
from opensky_api import OpenSkyApi, _count_utc_dates, FlightData, TokenManager
from flight_delay.data_ingestion.opensky.poller import (
    poll_airport_departure_once,
    poll_airport_arrival_once,
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


    client = OpenSkyApi(token_manager=TokenManager(client_id, client_secret))
    # print(poll_airport_departure_once(settings, client, "WSSS", 1786759200, 1786766400))
    # print(client.get_states())
    start = utc_to_epoch(get_today_midnight_utc())
    end = utc_to_epoch(get_today_2am_utc())
    details = PollingDetails(settings=settings,
                             airport="WSSS",
                             epoch_start=1787234400,
                             epoch_end=1787238000)

    departure_data = poll_airport_arrival_once(client, details)
    print(departure_data)
    print(len(departure_data))
    # print(client.get_departures_by_airport("WSSS", 1787234400, 1787238000))
    # print(client.get_states())

if __name__ == "__main__":
    main()
# ['76d107', 1786765784, 'WSSS', 1786767879, None, 'TGW226  ', 11895, 991, 0, 0, 19, 0]
