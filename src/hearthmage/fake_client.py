from __future__ import annotations

from dataclasses import replace

from hearthmage.domain import HearthError, Room, Scene


class FakeHearthClient:
    """In-memory client for tests and offline UI work."""

    def __init__(self, rooms: list[Room] | None = None) -> None:
        self._rooms: dict[str, Room] = {}
        self._config: dict[str, dict] = {}  # room_id -> per-radiator element config
        self.holiday: dict[str, dict] = {}  # room_id -> holiday hold state
        self.applied: list = []  # (room_id, blocks, days) recorded by set_day_pattern
        self.program_active: dict[str, bool] = {}  # room_id -> last set_program_active
        for room in (_default_rooms() if rooms is None else rooms):
            self._rooms[room.id] = room

    def list_rooms(self) -> list[Room]:
        return list(self._rooms.values())

    def _get(self, room_id: str) -> Room:
        try:
            return self._rooms[room_id]
        except KeyError as exc:
            raise HearthError(f"Unknown room: {room_id}") from exc

    def set_temperature(self, room_id: str, temperature: float) -> None:
        room = self._get(room_id)
        off = temperature <= 0
        target = 0.0 if off else round(temperature * 2) / 2
        self._rooms[room_id] = replace(room, target_temp=target, is_off=off)

    def set_scene_membership(self, room_id: str, scene: Scene, member: bool) -> None:
        room = self._get(room_id)
        if scene is Scene.STANDBY:
            self._rooms[room_id] = replace(room, is_off=member)

    def set_day_pattern(self, room_id: str, blocks, days) -> None:
        self._get(room_id)  # validate the zone exists
        self.applied.append((room_id, list(blocks), list(days)))

    def set_program_active(self, room_id: str, active: bool) -> None:
        self._get(room_id)  # validate the zone exists
        self.program_active[room_id] = bool(active)

    def read_config(self, room_id: str) -> dict | None:
        self._get(room_id)
        return self._config.setdefault(
            room_id, {"keypad_lock": False, "window_sensor": False, "offset": 7}
        )

    def set_config(self, room_id, keypad_lock=None, window_sensor=None, offset=None) -> None:
        self._get(room_id)
        if offset is not None and not (0 <= offset <= 17):
            raise HearthError(f"Offset {offset} out of range 0..17")
        cfg = self._config.setdefault(
            room_id, {"keypad_lock": False, "window_sensor": False, "offset": 7}
        )
        if keypad_lock is not None:
            cfg["keypad_lock"] = bool(keypad_lock)
        if window_sensor is not None:
            cfg["window_sensor"] = bool(window_sensor)
        if offset is not None:
            cfg["offset"] = int(offset)

    def set_holiday(self, room_id: str, day: int, month: int, temp: float) -> None:
        self._get(room_id)
        self.holiday[room_id] = {"active": True, "day": int(day), "month": int(month),
                                 "temp": float(temp)}

    def clear_holiday(self, room_id: str) -> None:
        self._get(room_id)
        self.holiday[room_id] = {"active": False, "day": None, "month": None, "temp": None}

    def read_holiday(self, room_id: str) -> dict | None:
        self._get(room_id)
        return self.holiday.get(
            room_id, {"active": False, "day": None, "month": None, "temp": None}
        )

    def read_energy(self, room_id: str) -> dict | None:
        self._get(room_id)  # validate the zone exists
        return {
            "rated_watts": 1000,
            "daily": [12, 40, 35, 50, 0, 22, 18, 7],  # today first, then 7 days
            "monthly": [300, 280, 260, 240, 210, 190, 170],
        }

    def hub_stale(self) -> bool:
        return False  # the fake hub is always "reachable"

    def health(self) -> dict:
        return {
            "poller_alive": True,
            "hub_last_contacted_seconds_ago": 0.0,
            "hub_stale": False,
            "zones": {
                r.id: {"status": r.status, "last_fresh_seconds_ago": 0.0}
                for r in self._rooms.values()
            },
        }


def _default_rooms() -> list[Room]:
    return [
        Room("1", "Living Room", 19.5, 21.0, is_off=False, min_temp=5.0, max_temp=30.0),
        Room("2", "Bedroom", 17.0, 18.0, is_off=False, min_temp=5.0, max_temp=30.0),
    ]
