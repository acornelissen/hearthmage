"""Local persistence for weekly schedule definitions.

The Nexho hub does not reliably serve stored programs back over the LAN (an
`*OD` read returns only a programmed/not-programmed summary; the block detail is
cloud-delivered), so this app owns the schedule definitions. They are kept here
as JSON and written to the hub with `*P#`; the hub is a write target, not the
source of truth. See docs/nexho-protocol.md.

Shape on disk: ``{"<zone>": {"<day>": [[target, start, end], ...]}}`` where day
is 0..6 (Mon..Sun) and start/end are minutes of day.
"""

from __future__ import annotations

import json
import os
import threading

from hearthmage.atomicio import write_json_atomic
from hearthmage.backups import rotate_backup
from hearthmage.schedule import MAX_BLOCKS, Block

_DAY_MINUTES = 24 * 60
_ACTIVE_KEY = "__active__"  # reserved top-level key (zone ids are numeric strings)


class ScheduleStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, list[list[int]]]] = {}
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
            self._data = {}  # corrupt or unreadable: start empty rather than crash

    def _save(self) -> None:
        write_json_atomic(self._path, self._data)
        try:
            rotate_backup(self._path)  # keep a timestamped copy of every change
        except OSError:
            pass  # a failed backup must not fail the save

    # ---- reads -----------------------------------------------------------

    def get_day(self, zone_id: str, day: int) -> list[Block]:
        with self._lock:
            raw = self._data.get(str(zone_id), {}).get(str(day), [])
            return [Block(int(t), int(s), int(e)) for t, s, e in raw]

    def get_zone(self, zone_id: str) -> dict[int, list[Block]]:
        if str(zone_id) == _ACTIVE_KEY:
            return {}  # the reserved active-state key is not a zone
        with self._lock:
            days = self._data.get(str(zone_id), {})
            return {
                int(day): [Block(int(t), int(s), int(e)) for t, s, e in blocks]
                for day, blocks in days.items()
            }

    def get_active(self, zone_id: str) -> bool | None:
        """Whether this zone's program is running, as last set from this app
        (the hub does not report it over the LAN). None if never set."""
        with self._lock:
            val = self._data.get(_ACTIVE_KEY, {}).get(str(zone_id))
            return bool(val) if val is not None else None

    # ---- writes ----------------------------------------------------------

    def set_pattern(self, zone_id: str, blocks: list[Block], days: list[int]) -> None:
        """Store ``blocks`` as the schedule for every day in ``days``."""
        if not days:
            raise ValueError("a schedule pattern needs at least one day")
        _validate(blocks)
        serialised = [[b.target, b.start, b.end] for b in blocks]
        with self._lock:
            zone = self._data.setdefault(str(zone_id), {})
            for day in days:
                zone[str(int(day))] = [list(b) for b in serialised]
            self._save()

    def clear_day(self, zone_id: str, day: int) -> None:
        with self._lock:
            zone = self._data.get(str(zone_id))
            if zone is not None:
                zone.pop(str(int(day)), None)
                self._save()

    def set_active(self, zone_id: str, active: bool) -> None:
        with self._lock:
            self._data.setdefault(_ACTIVE_KEY, {})[str(zone_id)] = bool(active)
            self._save()


def _validate(blocks: list[Block]) -> None:
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"a day can hold at most {MAX_BLOCKS} blocks")
    for b in blocks:
        if not (0 <= b.start < b.end <= _DAY_MINUTES):
            raise ValueError(f"invalid block times: start={b.start} end={b.end}")
