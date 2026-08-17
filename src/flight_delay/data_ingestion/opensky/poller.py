from flight_delay.common.config import Settings
from opensky_api import OpenSkyApi, FlightData
from pydantic import BaseModel, Field

class PollingDetails(BaseModel):
    settings: Settings
    airport: str = Field(
        ...,
        min_length=3,
        max_length=4,
        description="IATA (3-letter) or ICAO (4-letter) airport code",
    )
    epoch_start: float = Field(..., ge=0, description="Start timestamp in seconds")
    epoch_end: float = Field(..., ge=0, description="End timestamp in seconds")

# Interval must be withing same day
def poll_airport_departure_once(client: OpenSkyApi, details: PollingDetails) -> list[FlightData]:
    raw_vector = client.get_departures_by_airport(details.airport,
                                                  details.epoch_start,
                                                  details.epoch_end)
    return raw_vector

def poll_airport_arrival_once(client: OpenSkyApi, details: PollingDetails) -> list[FlightData]:
    raw_vector = client.get_arrivals_by_airport(details.airport,
                                                details.epoch_start,
                                                details.epoch_end)
    return raw_vector
