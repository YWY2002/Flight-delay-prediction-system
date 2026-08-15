from flight_delay.common.config import Settings, get_settings
from opensky_api import OpenSkyApi
from flight_delay.data_ingestion.opensky.poller import poll_airport_departure_once
from flight_delay.common.timeutil import utc_now, utc_to_epoch
import time

def main() -> None:
    settings = get_settings()

    client = OpenSkyApi()
    print(poll_airport_departure_once(settings, client, "WSSS", 1786759200, 1786766400))
    # print(client.get_states())
    

if __name__ == "__main__":
    main()
['76d107', 1786765784, 'WSSS', 1786767879, None, 'TGW226  ', 11895, 991, 0, 0, 19, 0]
