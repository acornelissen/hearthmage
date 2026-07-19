"""Last-known energy readings, cached to disk.

Radiators are often asleep on RF, so an energy read frequently fails. This keeps
the most recent successful reading per zone (rated wattage plus the raw daily and
monthly counters, and when it was fetched) so the energy page always has
something to show and survives restarts. It is a cache, not a source of truth:
losing it just means the next successful read repopulates it.

Shape on disk: ``{"<zone>": {"rated_watts": int, "daily": [int...],
"monthly": [int...], "fetched_at": "<iso8601>"}}``.
"""

from __future__ import annotations

import json
import os
import threading

from hearthmage.atomicio import write_json_atomic


class EnergyStore:
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
            self._data = {}  # corrupt cache: start empty rather than crash

    def get_all(self) -> dict[str, dict]:
        with self._lock:
            return {z: dict(v) for z, v in self._data.items()}

    def get_zone(self, zone_id: str) -> dict | None:
        with self._lock:
            value = self._data.get(str(zone_id))
            return dict(value) if value is not None else None

    def set_zone(
        self,
        zone_id: str,
        rated_watts: int,
        daily: list[int],
        monthly: list[int],
        fetched_at: str,
    ) -> None:
        with self._lock:
            self._data[str(zone_id)] = {
                "rated_watts": int(rated_watts),
                "daily": [int(c) for c in daily],
                "monthly": [int(c) for c in monthly],
                "fetched_at": fetched_at,
            }
            write_json_atomic(self._path, self._data)
