from opensky_api import OpenSkyApi

def main():
    api = OpenSkyApi()
    airportArrivals = api.get_states()
    print(airportArrivals)

if __name__ == "__main__":
    main()
