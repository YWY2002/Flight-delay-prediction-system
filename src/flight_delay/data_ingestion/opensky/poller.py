from flight_delay.common.config import Settings
from opensky_api import OpenSkyApi, FlightData

#TODO State

def poll_airport_departure_once(settings: Settings, client: OpenSkyApi, airport: str, epoch_start: float, epoch_end: float) -> list[FlightData]:
    raw_vector = client.get_departures_by_airport(airport, epoch_start, epoch_end)
    return raw_vector
