"""FAA NAS status: ground stops, ground delay programs, and closures.

    client.py  XML fetch and parsing into FaaEvent
    bronze.py  FaaEvent -> bronze row mapping

Same split as `weather`: HTTP and parsing on one side, storage shape on the
other.
"""
