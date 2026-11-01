"""Time-series history for room temperature and daily energy.

The poller already reads each zone's temperature every few seconds and throws it
away; this keeps it. A tiny SQLite database records temperature readings and
per-day energy totals so the app can draw trend sparklines and, for energy,
retain history past the hub's own short window (~8 days / ~6 months). It also
keeps "heating nobody asked for" events (see waste.py) and the raw radiator
status (``*?F``) captured during them and at rest, for diagnosis.

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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS waste_events ("
                "id INTEGER PRIMARY KEY, zone TEXT NOT NULL, start_ts REAL NOT NULL, "
                "start_temp REAL, setpoint REAL, peak_ts REAL, peak_temp REAL, "
                "open INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_waste_zone_ts ON waste_events(zone, start_ts)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS status_snapshots ("
                "id INTEGER PRIMARY KEY, zone TEXT NOT NULL, ts REAL NOT NULL, "
                "kind TEXT NOT NULL, raw TEXT NOT NULL, event_id INTEGER)"
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

    # ---- heating nobody asked for -----------------------------------------

    def start_waste_event(
        self, zone: str, start_ts: float, start_temp: float, setpoint: float,
        peak_ts: float, peak_temp: float,
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO waste_events (zone, start_ts, start_temp, setpoint, peak_ts, peak_temp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(zone), start_ts, start_temp, setpoint, peak_ts, peak_temp),
            )
            return int(cur.lastrowid)

    def update_waste_event(self, event_id: int, peak_ts: float, peak_temp: float, open_: bool) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE waste_events SET peak_ts = ?, peak_temp = ?, open = ? WHERE id = ?",
                (peak_ts, peak_temp, int(open_), int(event_id)),
            )

    def close_open_waste_events(self) -> None:
        """Mark every event closed (after a restart nothing tracks them any more)."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE waste_events SET open = 0 WHERE open = 1")

    def waste_events(self, zone: str, since_ts: float = 0.0, limit: int = 50) -> list[dict]:
        """A zone's events, newest first."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM waste_events WHERE zone = ? AND start_ts >= ? "
                "ORDER BY start_ts DESC LIMIT ?",
                (str(zone), since_ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def zones_with_waste(self, since_ts: float) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT zone FROM waste_events WHERE start_ts >= ? ORDER BY zone",
                (since_ts,),
            ).fetchall()
        return [r["zone"] for r in rows]

    def record_status(
        self, zone: str, ts: float, kind: str, raw: str, event_id: int | None = None
    ) -> None:
        """Store a raw ``*?F`` reply; ``kind`` is "rest" or "heating"."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO status_snapshots (zone, ts, kind, raw, event_id) VALUES (?, ?, ?, ?, ?)",
                (str(zone), ts, kind, raw, event_id),
            )

    def latest_status(self, zone: str, kind: str) -> dict | None:
        return self._one(
            "SELECT * FROM status_snapshots WHERE zone = ? AND kind = ? ORDER BY ts DESC LIMIT 1",
            (str(zone), kind),
        )

    def rest_status_before(self, zone: str, ts: float) -> dict | None:
        """The resting snapshot taken closest before ``ts`` (to compare an event with)."""
        return self._one(
            "SELECT * FROM status_snapshots WHERE zone = ? AND kind = 'rest' AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (str(zone), ts),
        )

    def status_for_event(self, event_id: int) -> dict | None:
        return self._one(
            "SELECT * FROM status_snapshots WHERE event_id = ? ORDER BY ts LIMIT 1", (int(event_id),)
        )

    def _one(self, sql: str, params: tuple) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
