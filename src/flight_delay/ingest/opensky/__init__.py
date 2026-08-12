"""OpenSky Network: live aircraft state vectors, plus the aircraft reference DB.

    auth.py               OAuth2 client-credentials token provider + httpx.Auth
    client.py             /states/all client and the state-vector schema
    credit_budget.py      the daily credit gate OpenSky meters us against
    poller.py             one poll cycle across every configured bounding box
    aircraft_metadata.py  static icao24 reference table (not a poller)

Nothing here is imported by the other sources: the credit budget and the token
dance are OpenSky's own concepts, not shared ingestion machinery. What IS shared
lives one level up in `bronze.py`, `errors.py`, `http.py`, and `retry.py`.
"""
