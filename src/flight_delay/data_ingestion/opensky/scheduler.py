"""Scheduled polling of OpenSky live state vectors.

One task, on a three minute grid: fetch every aircraft inside a bounding box
and hand the snapshot to a sink. This is the live half of ingestion. The
flights endpoints cannot serve it -- arrivals for a given UTC day do not appear
until some hour during the next one -- so `/states/all` over the airport box is
the only source of what is actually in the air right now.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Event

from opensky_api import OpenSkyApi, OpenSkyStates

from flight_delay.common.airports import BoundingBox, load_airports
from flight_delay.common.config import Settings
from flight_delay.common.logging_config import get_logger
from flight_delay.data_ingestion.opensky.poller import poll_states_once
from flight_delay.data_ingestion.scheduling import ScheduledTask, run_scheduler

logger = get_logger(__name__)

# an aircraft crosses a 60 nm box in roughly ten minutes, so it is sampled three or four
# times on the way in. The floor is not the API but `MIN_OPENSKY_POLL_SECONDS`
# in Settings, and `get_states` separately refuses more than one call every 5s.
STATES_INTERVAL_SECONDS = 180.0

# No offset. Unlike METAR and TAF, a state vector has no publication boundary to
# sit behind -- the snapshot is whatever OpenSky holds at the moment we ask. The
# grid is still aligned so ticks land on :00, :03, :06 and so on, which keeps
# bronze partitioning predictable and spacing exactly even.
STATES_OFFSET_SECONDS = 0.0


def build_states_task(
    client: OpenSkyApi,
    bbox: BoundingBox,
    on_snapshot: Callable[[OpenSkyStates], None],
    *,
    interval_seconds: float = STATES_INTERVAL_SECONDS,
    name: str = "opensky.states",
) -> ScheduledTask:
    """A `ScheduledTask` wrapping one bounding box.

    Exposed separately from `run_states_scheduler` so several boxes, or this
    box alongside the weather tasks, can be driven by a single `run_scheduler`
    call later. Note that `get_states` rate-limits per *method* rather than per
    box, so two boxes on the same tick through one client will make the second
    raise `OpenSkyThrottled`: give each box its own client if you go that way.
    """

    def run() -> int:
        snapshot = poll_states_once(client, bbox)
        on_snapshot(snapshot)
        return len(snapshot.states)

    return ScheduledTask(
        name=name,
        interval_seconds=interval_seconds,
        run=run,
        offset_seconds=STATES_OFFSET_SECONDS,
    )


def run_states_scheduler(
    client: OpenSkyApi,
    bbox: BoundingBox,
    on_snapshot: Callable[[OpenSkyStates], None],
    *,
    interval_seconds: float = STATES_INTERVAL_SECONDS,
    max_ticks: int | None = None,
    stop: Event | None = None,
) -> None:
    """Poll `bbox` every three minutes until stopped.

    `on_snapshot` receives the whole `OpenSkyStates`, not just the vectors,
    because its `time` field stamps the validity of every vector in it. Record
    that as the observation timestamp rather than the wall clock at receipt:
    the snapshot is typically already a few seconds old when it arrives.
    """
    run_scheduler(
        [build_states_task(client, bbox, on_snapshot, interval_seconds=interval_seconds)],
        max_ticks=max_ticks,
        stop=stop,
    )


def wsss_bounding_box(settings: Settings) -> BoundingBox:
    """The configured box around Singapore Changi.

    Derived from the reference data rather than hardcoded, so the box cannot
    drift from the airport it is supposed to contain, and so `FDP_BBOX_RADIUS_NM`
    keeps working.
    """
    return load_airports(settings.airports_file)["WSSS"].bounding_box(settings.bbox_radius_nm)
