"""Detect heating that nobody asked for (energy waste).

The Lounge radiator was seen warming the room at +4.1 C/h with a 5 C target,
the same rate as a deliberate heat to 18 C. This watches every fresh reading
and flags a zone whose room temperature climbs fast while its target sits well
below the room temperature.

Rules (tuned against 30 days of real history, see heatapp-local-y49):

- the room rose at least ``MIN_RISE`` within ``WINDOW`` (0.5 C in 15 min, i.e.
  2 C/h); sunny-afternoon rises run about 1 C/h and stay below it;
- the climb is sustained: its low point is at least ``MIN_SPAN`` back, so a
  sudden jump (0.5 C in a minute or two, seen in real data: warm air or sensor
  noise, far faster than a radiator heats a room) does not count;
- the target was at least ``TARGET_MARGIN`` below the room for every reading
  in that window, so heat that was just asked for (or the tail of it right
  after the target was lowered) does not count;
- an event ends when the room has not set a new peak for ``QUIET_END``, or
  when heat is asked for again.

Pure logic: the caller stores events and reads the radiator's status.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

MIN_RISE = 0.5  # C
WINDOW = 15 * 60  # seconds
MIN_SPAN = 10 * 60  # seconds: the climb's low point must be at least this old
TARGET_MARGIN = 1.0  # C below the room
QUIET_END = 15 * 60  # seconds without a new peak
BASELINE_EVERY = 6 * 3600  # seconds between resting status snapshots


@dataclass
class WasteEvent:
    zone: str
    start_ts: float
    start_temp: float
    peak_ts: float
    peak_temp: float
    setpoint: float
    id: int | None = None  # set by the caller once stored

    @property
    def end_ts(self) -> float:
        return self.peak_ts


class WasteDetector:
    def __init__(self) -> None:
        self._recent: dict[str, deque[tuple[float, float, float]]] = {}
        self._open: dict[str, WasteEvent] = {}
        self._last_baseline: dict[str, float] = {}

    def feed(
        self, zone: str, ts: float, current: float | None, setpoint: float | None
    ) -> tuple[str, WasteEvent] | None:
        """Take one reading. Returns ("start" | "update" | "end", event) when an
        event begins, moves or finishes, else None."""
        if current is None or setpoint is None:
            return None
        recent = self._recent.setdefault(zone, deque())
        recent.append((ts, current, setpoint))
        while recent and recent[0][0] < ts - WINDOW:
            recent.popleft()

        event = self._open.get(zone)
        if event is not None:
            if setpoint + TARGET_MARGIN > current or ts - event.peak_ts > QUIET_END:
                del self._open[zone]
                return "end", event
            if current > event.peak_temp:
                event.peak_temp, event.peak_ts = current, ts
                return "update", event
            return None

        if any(sp + TARGET_MARGIN > cur for _, cur, sp in recent):
            return None  # heat was asked for at some point in the window
        low_ts, low_temp, _ = min(recent, key=lambda r: (r[1], -r[0]))  # latest lowest
        if current - low_temp < MIN_RISE or ts - low_ts < MIN_SPAN:
            return None
        event = WasteEvent(zone, low_ts, low_temp, ts, current, setpoint)
        self._open[zone] = event
        return "start", event

    def baseline_due(
        self, zone: str, ts: float, current: float | None, setpoint: float | None
    ) -> bool:
        """True (and noted as taken) when it is time for a resting status
        snapshot: no event open, no heat asked for, none taken recently."""
        if zone in self._open or current is None or setpoint is None:
            return False
        if setpoint + TARGET_MARGIN > current:
            return False
        last = self._last_baseline.get(zone)
        if last is not None and ts - last < BASELINE_EVERY:
            return False
        self._last_baseline[zone] = ts
        return True


# ``*?F`` reply fields (index after splitting on commas; [0] is "OK"). Mapped
# live 2026-11-01; anything unlisted is not understood yet.
STATUS_LABELS = {
    1: "Room temperature (whole)",
    2: "Room temperature (tenths)",
    3: "Target",
    4: "Clock hour",
    5: "Clock minute",
    6: "Clock second",
    7: "Weekday",
}
_EXPECTED_TO_MOVE = {1, 2, 4, 5, 6}  # room temperature and the clock always change


def status_diff(rest_raw: str | None, heating_raw: str | None) -> list[dict]:
    """Fields that differ between a resting and a heating ``*?F`` reply,
    leaving out room temperature and the clock. Empty if either is unusable."""
    if not (rest_raw and heating_raw and rest_raw.startswith("OK,") and heating_raw.startswith("OK,")):
        return []
    rest, heat = rest_raw.split(","), heating_raw.split(",")
    rows = []
    for i in range(1, min(len(rest), len(heat))):
        if i in _EXPECTED_TO_MOVE or rest[i] == heat[i]:
            continue
        rows.append({"index": i, "label": STATUS_LABELS.get(i, f"Field {i}"),
                     "rest": rest[i], "heating": heat[i]})
    return rows
