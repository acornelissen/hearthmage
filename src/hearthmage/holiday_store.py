"""Local record of holiday holds.

The hub holds a zone until the occupants are back (a date and time) and then
switches to an arrival temperature or back to the weekly program. Its read-back
gives only days left, so we keep, per zone, the intended end (for display) and
the setpoint to restore if the user cancels early. A convenience record, not a
source of truth.

Shape on disk: ``{"<zone>": {"until": "YYYY-MM-DDTHH:MM", "arrival_temp":
float | null, "prev_setpoint": float | null}}``. ``until`` is local time;
``arrival_temp`` null means "resume the weekly program". Records written
before 2026-11-01 used day/month keys (sent to the hub with the wrong fields)
and are dropped by the app.
"""

from __future__ import annotations

import json
import os
import threading

from hearthmage.atomicio import write_json_atomic


class HolidayStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, ValueError):
            self._data = {}

    def get(self, zone_id: str) -> dict | None:
        with self._lock:
            value = self._data.get(str(zone_id))
            return dict(value) if value is not None else None

    def set(self, zone_id: str, until: str, arrival_temp: float | None, prev_setpoint) -> None:
        with self._lock:
            self._data[str(zone_id)] = {
                "until": str(until),
                "arrival_temp": float(arrival_temp) if arrival_temp is not None else None,
                "prev_setpoint": prev_setpoint,
            }
            write_json_atomic(self._path, self._data)

    def clear(self, zone_id: str) -> None:
        with self._lock:
            if str(zone_id) in self._data:
                del self._data[str(zone_id)]
                write_json_atomic(self._path, self._data)
