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
        self.programs: dict[tuple[str, int], list] = {}  # (room_id, day) -> blocks held "on the hub"
        self.cloud_reporting = False
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
        target = 1.0 if off else round(temperature * 2) / 2  # Off is a 1 degree setpoint
        self._rooms[room_id] = replace(room, target_temp=target, is_off=off)

    def set_scene_membership(self, room_id: str, scene: Scene, member: bool) -> None:
        room = self._get(room_id)
        if scene is Scene.STANDBY:
            self._rooms[room_id] = replace(room, is_off=member)

    def set_day_pattern(self, room_id: str, blocks, days) -> None:
        self._get(room_id)  # validate the zone exists
        self.applied.append((room_id, list(blocks), list(days)))
        for day in days:
            self.programs[(room_id, day)] = sorted(blocks, key=lambda b: b.start)

    def read_day_pattern(self, room_id: str, day: int) -> list:
        self._get(room_id)
        return list(self.programs.get((room_id, day), []))

    def read_status_raw(self, room_id: str) -> str:
        room = self._get(room_id)
        whole = int(room.current_temp or 0)
        tenths = int(round(((room.current_temp or 0) - whole) * 10))
        return f"OK,{whole},{tenths},{int(room.target_temp or 0)},12,0,0,0,11,50,0,0,0,0,1,0,44,244,0,30,v000000"

    def read_cloud_reporting(self) -> bool:
        return self.cloud_reporting

    def set_cloud_reporting(self, enabled: bool) -> None:
        self.cloud_reporting = bool(enabled)

    def set_program_active(self, room_id: str, active: bool) -> None:
        self._get(room_id)  # validate the zone exists
        self.program_active[room_id] = bool(active)

    def read_config(self, room_id: str) -> dict | None:
        self._get(room_id)
        return self._config.setdefault(room_id, dict(_DEFAULT_CONFIG))

    def set_config(
        self, room_id, keypad_lock=None, window_sensor=None, frost_temp=None, sensor_offset=None
    ) -> None:
        self._get(room_id)
        if frost_temp is not None and not (0 <= frost_temp <= 17):
            raise HearthError(f"Anti-ice temperature {frost_temp} out of range 0..17")
        if sensor_offset is not None and not abs(sensor_offset) <= 9.9:
            raise HearthError(f"Sensor offset {sensor_offset} out of range +/-9.9")
        cfg = self._config.setdefault(room_id, dict(_DEFAULT_CONFIG))
        if keypad_lock is not None:
            cfg["keypad_lock"] = bool(keypad_lock)
        if window_sensor is not None:
            cfg["window_sensor"] = bool(window_sensor)
        if frost_temp is not None:
            cfg["frost_temp"] = int(frost_temp)
        if sensor_offset is not None:
            cfg["sensor_offset"] = round(float(sensor_offset), 1)

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


_DEFAULT_CONFIG = {"keypad_lock": False, "window_sensor": False, "frost_temp": 7, "sensor_offset": 0.0}


def _default_rooms() -> list[Room]:
    return [
        Room("1", "Living Room", 19.5, 21.0, is_off=False, min_temp=5.0, max_temp=30.0),
        Room("2", "Bedroom", 17.0, 18.0, is_off=False, min_temp=5.0, max_temp=30.0),
    ]
