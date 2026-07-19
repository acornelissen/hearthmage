"""Time-series history for room temperature and daily energy.

The poller already reads each zone's temperature every few seconds and throws it
away; this keeps it. A tiny SQLite database (two tables) records temperature
readings and per-day energy totals so the app can draw trend sparklines and, for
energy, retain history past the hub's own short window (~8 days / ~6 months).

It is derived data, not a source of truth: deleting the database just loses the
history. Writes come from the poller thread and the energy refresh thread, so
every operation opens its own short-lived connection under a lock.
"""

from __future__ import annotations

import os
import sqlite3
import threading


class HistoryStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS temp_readings ("
                "zone TEXT NOT NULL, ts REAL NOT NULL, current REAL, setpoint REAL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_temp_zone_ts ON temp_readings(zone, ts)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS energy_daily ("
                "zone TEXT NOT NULL, day TEXT NOT NULL, kwh REAL, "
                "PRIMARY KEY (zone, day))"
            )

    # ---- temperature -----------------------------------------------------

    def record_temp(self, zone: str, current: float | None, setpoint: float | None, ts: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO temp_readings (zone, ts, current, setpoint) VALUES (?, ?, ?, ?)",
                (str(zone), float(ts), current, setpoint),
            )

    def temp_series(self, zone: str, limit: int = 2000) -> list[dict]:
        """Temperature readings for a zone, oldest first (up to the newest ``limit``)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, current, setpoint FROM temp_readings WHERE zone = ? "
                "ORDER BY ts DESC LIMIT ?",
                (str(zone), limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_temp(self, before_ts: float) -> None:
        """Delete temperature rows older than ``before_ts`` (retention control)."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM temp_readings WHERE ts < ?", (float(before_ts),))

    # ---- energy ----------------------------------------------------------

    def record_energy_day(self, zone: str, day: str, kwh: float) -> None:
        """Record (or update) a zone's energy total for one calendar day."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO energy_daily (zone, day, kwh) VALUES (?, ?, ?) "
                "ON CONFLICT(zone, day) DO UPDATE SET kwh = excluded.kwh",
                (str(zone), str(day), float(kwh)),
            )

    def energy_series(self, zone: str, limit: int = 400) -> list[dict]:
        """Per-day energy for a zone, oldest first (up to the newest ``limit`` days)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT day, kwh FROM energy_daily WHERE zone = ? ORDER BY day DESC LIMIT ?",
                (str(zone), limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
