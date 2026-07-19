"""Persistent schedule-sync state.

A schedule or program push to the hub can fail (the radiator is asleep on RF).
That state used to live in an in-memory dict, so a failed push was forgotten on
restart and never retried. This persists the per-zone status
(``pending`` | ``synced`` | ``failed``) so the app can show it after a restart
and a background task can re-push the ``failed`` zones from the schedule store
(the source of truth) once the radiator is reachable again.
"""

from __future__ import annotations

import json
import os
import threading

from hearthmage.atomicio import write_json_atomic


class SyncStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self._data = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, ValueError):
            self._data = {}  # corrupt: start empty rather than crash

    def get(self, zone_id: str) -> str | None:
        with self._lock:
            return self._data.get(str(zone_id))

    def set(self, zone_id: str, status: str) -> None:
        with self._lock:
            self._data[str(zone_id)] = status
            write_json_atomic(self._path, self._data)

    def failed_zones(self) -> list[str]:
        with self._lock:
            return [z for z, status in self._data.items() if status == "failed"]
