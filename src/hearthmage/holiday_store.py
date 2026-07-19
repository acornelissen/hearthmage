"""Local record of holiday holds.

The hub holds a zone at a frost temperature until a date and auto-resumes, but it
does not hand back a friendly end date, and cancelling a hold leaves the zone at
the frost temperature rather than the setpoint it had before. So we keep, per
zone, the intended end date (for display) and the setpoint to restore when the
user cancels early. It is a convenience record, not a source of truth.

Shape on disk: ``{"<zone>": {"day": int, "month": int, "temp": float,
"prev_setpoint": float | null}}``.
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

    def set(self, zone_id: str, day: int, month: int, temp: float, prev_setpoint) -> None:
        with self._lock:
            self._data[str(zone_id)] = {
                "day": int(day),
                "month": int(month),
                "temp": float(temp),
                "prev_setpoint": prev_setpoint,
            }
            write_json_atomic(self._path, self._data)

    def clear(self, zone_id: str) -> None:
        with self._lock:
            if str(zone_id) in self._data:
                del self._data[str(zone_id)]
                write_json_atomic(self._path, self._data)
