"""FAA NAS status: ground stops, ground delay programs, and closures.

    client.py  XML fetch and parsing into FaaEvent
    bronze.py  FaaEvent -> bronze row mapping
    poller.py  one nationwide fetch, filtered to our airports

Same split as `weather` and `opensky`: HTTP, storage shape, and one cycle of
work each change for different reasons.
"""
