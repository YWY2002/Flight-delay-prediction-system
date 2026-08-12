"""Aviation Weather Center: METAR observations and TAF forecasts.

    client.py  HTTP + parsing for both report types
    bronze.py  report -> bronze row mapping

Split so the client stays about HTTP and the bronze module about storage shape;
they change for different reasons.
"""
