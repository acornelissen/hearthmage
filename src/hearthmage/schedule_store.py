"""Local persistence for weekly schedule definitions.

The Nexho hub does not reliably serve stored programs back over the LAN (an
`*OD` read returns only a programmed/not-programmed summary; the block detail is
cloud-delivered), so this app owns the schedule definitions. They are kept here
as JSON and written to the hub with `*P#`; the hub is a write target, not the
source of truth. See docs/nexho-protocol.md.

Shape on disk: ``{"<zone>": {"<day>": [[target, start, end], ...]}}`` where day
is 0..6 (Mon..Sun) and start/end are minutes of day. A zone key that is present
(even with no days) means this app owns that zone's whole week: days missing
from it are pushed to the hub as cleared.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from hearthmage.atomicio import write_json_atomic
from hearthmage.backups import rotate_backup
from hearthmage.schedule import Block, validate_blocks, validate_days

_ACTIVE_KEY = "__active__"  # reserved top-level key (zone ids are numeric strings)
_log = logging.getLogger("hearthmage.schedule")


def _zone_key(zone_id: str) -> str:
    """Zone ids are ASCII digit strings; anything else (including the reserved
    active key) is rejected so it can never collide with store internals."""
    key = str(zone_id)
    if not (key.isascii() and key.isdigit()):
        raise ValueError(f"invalid zone id: {zone_id!r}")
    return key


def _parse_zone(days: object) -> dict[str, list[list[int]]]:
    """Validate one zone's ``{"<day>": [[t, s, e], ...]}`` mapping."""
    if not isinstance(days, dict):
        raise ValueError("zone schedule is not an object")
    out: dict[str, list[list[int]]] = {}
    for day, raw in days.items():
        if not (str(day).isascii() and str(day).isdigit()):
            raise ValueError(f"invalid weekday: {day!r}")
        validate_days([int(day)])
        if not isinstance(raw, list) or not all(
            isinstance(b, list) and len(b) == 3 and all(type(v) is int for v in b) for b in raw
        ):
            raise ValueError(f"invalid blocks for day {day!r}")
        blocks = validate_blocks([Block(*b) for b in raw])
        out[str(int(day))] = [[b.target, b.start, b.end] for b in blocks]
    return out


def _parse_active(raw: object) -> dict[str, bool]:
    if not isinstance(raw, dict):
        raise ValueError("active map is not an object")
    out: dict[str, bool] = {}
    for zone, val in raw.items():
        if type(val) is not bool:
            raise ValueError(f"active flag for zone {zone!r} is not true/false")
        out[_zone_key(zone)] = val
    return out


def validate_data(data: object) -> dict:
    """Strictly validate a whole schedules document (e.g. from an imported
    backup); raise ValueError on anything that could not have been written here."""
    if not isinstance(data, dict):
        raise ValueError("schedules is not an object")
    clean: dict = {}
    for key, value in data.items():
        if key == _ACTIVE_KEY:
            clean[key] = _parse_active(value)
        else:
            clean[_zone_key(key)] = _parse_zone(value)
    return clean


def _clean_data(data: object) -> dict:
    """Lenient load: keep every valid zone/day/flag and drop (and log) the rest,
    so one bad entry in a hand-edited file does not lose the whole schedule."""
    if not isinstance(data, dict):
        return {}
    clean: dict = {}
    for key, value in data.items():
        if key == _ACTIVE_KEY:
            if isinstance(value, dict):
                active = {}
                for zone, flag in value.items():
                    try:
                        active.update(_parse_active({zone: flag}))
                    except ValueError as exc:
                        _log.warning("dropping schedule active flag %r: %s", zone, exc)
                clean[key] = active
            continue
        try:
            zone = _zone_key(key)
        except ValueError as exc:
            _log.warning("dropping schedule zone %r: %s", key, exc)
            continue
        if not isinstance(value, dict):
            _log.warning("dropping schedule zone %r: not an object", key)
            continue
        days: dict[str, list[list[int]]] = {}
        for day, raw in value.items():
            try:
                days.update(_parse_zone({day: raw}))
            except (ValueError, TypeError) as exc:
                _log.warning("dropping zone %s day %r: %s", key, day, exc)
        clean[zone] = days
    return clean


class ScheduleStore:
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
        except (OSError, ValueError):
            self._data = {}  # corrupt or unreadable: start empty rather than crash
            return
        self._data = _clean_data(loaded)

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

    def owns(self, zone_id: str) -> bool:
        """True once this app has written any schedule for the zone, even if every
        day has since been cleared: it then owns all seven days on the hub."""
        with self._lock:
            return str(zone_id) != _ACTIVE_KEY and str(zone_id) in self._data

    def zone_ids(self) -> list[str]:
        """Zones with an owned week or a recorded program state."""
        with self._lock:
            ids = {k for k in self._data if k != _ACTIVE_KEY}
            ids.update(self._data.get(_ACTIVE_KEY, {}))
            return sorted(ids, key=int)

    def get_active(self, zone_id: str) -> bool | None:
        """Whether this zone's program is running, as last set from this app
        (the hub does not report it over the LAN). None if never set."""
        with self._lock:
            val = self._data.get(_ACTIVE_KEY, {}).get(str(zone_id))
            return bool(val) if val is not None else None

    # ---- writes ----------------------------------------------------------

    def set_pattern(self, zone_id: str, blocks: list[Block], days: list[int]) -> None:
        """Store ``blocks`` as the schedule for every day in ``days``."""
        key = _zone_key(zone_id)
        days = validate_days([int(d) for d in days])
        serialised = [[b.target, b.start, b.end] for b in validate_blocks(blocks)]
        with self._lock:
            zone = self._data.setdefault(key, {})
            for day in days:
                zone[str(day)] = [list(b) for b in serialised]
            self._save()

    def clear_day(self, zone_id: str, day: int) -> None:
        key = _zone_key(zone_id)
        (day,) = validate_days([int(day)])
        with self._lock:
            # Keep the (possibly empty) zone: the app still owns its week.
            self._data.setdefault(key, {}).pop(str(day), None)
            self._save()

    def set_active(self, zone_id: str, active: bool) -> None:
        key = _zone_key(zone_id)
        with self._lock:
            self._data.setdefault(_ACTIVE_KEY, {})[key] = bool(active)
            self._save()
