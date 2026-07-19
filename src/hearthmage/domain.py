from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class Scene(str, Enum):
    STANDBY = "Standby"
    BOOST = "Boost"
    HOLIDAY = "Holiday"
    LEAVE = "Leave"
    PARTY = "Party"


PRESETS = (Scene.BOOST, Scene.HOLIDAY, Scene.LEAVE, Scene.PARTY)


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    current_temp: float | None
    target_temp: float | None
    is_off: bool
    min_temp: float
    max_temp: float
    status: str = "ok"  # "ok" | "loading" | "unreachable"
    error: str | None = None  # transient per-zone error (e.g. a write that failed)


class HearthError(Exception):
    """Raised when a heater operation cannot be completed."""


@runtime_checkable
class HearthClient(Protocol):
    def list_rooms(self) -> list[Room]: ...

    def set_temperature(self, room_id: str, temperature: float) -> None: ...

    def set_scene_membership(self, room_id: str, scene: Scene, member: bool) -> None: ...
