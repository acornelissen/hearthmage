from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


# Setpoint range and step in degrees C. Weekly-schedule block targets use the
# same range (schedule.MIN_TARGET/MAX_TARGET).
MIN_TEMP = 5
MAX_TEMP = 30
TEMP_STEP = 0.5


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
    # Seconds since the radiator last answered, once that is long enough to be
    # worth telling the user (the reading shown is then the last one it gave).
    silent_for: float | None = None
    # The target shown was asked for but the radiator has not acknowledged it
    # yet (asleep or switched off); the client keeps re-sending it.
    unconfirmed: bool = False


class HearthError(Exception):
    """Raised when a heater operation cannot be completed."""


@runtime_checkable
class HearthClient(Protocol):
    def list_rooms(self) -> list[Room]: ...

    def set_temperature(self, room_id: str, temperature: float) -> None: ...
