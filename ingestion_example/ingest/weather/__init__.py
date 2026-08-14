"""Aviation Weather Center: METAR observations and TAF forecasts.

    client.py  HTTP + parsing for both report types
    bronze.py  report -> bronze row mapping
    poller.py  one METAR poll and one TAF poll, on demand

Split so the client stays about HTTP, the bronze module about storage shape,
and the poller about one cycle of work; they change for different reasons.
"""
