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
import time

_PRUNE_EVERY = 3600.0  # seconds between retention sweeps
_monotonic = time.monotonic  # indirection so tests can move the clock


class HistoryStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        self._last_prune: float | None = None
        self._create_private()
        self._restrict_files()  # before SQLite builds an index and a journal
        self._init_db()
        self._restrict_files()  # the file may have just been created by SQLite

    def _create_private(self) -> None:
        """Create the file owner-only before SQLite does (it would use 0644)."""
        try:
            os.close(os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600))
        except OSError:
            pass  # SQLite will report a real problem when it connects

    def _restrict_files(self) -> None:
        """Best effort: owner-only on the database and any sidecar files."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.chmod(self._path + suffix, 0o600)
            except OSError:
                pass  # missing sidecar, or a filesystem that refuses chmod

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
            conn.execute("CREATE INDEX IF NOT EXISTS ix_temp_ts ON temp_readings(ts)")
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_status_zone_kind_ts "
                "ON status_snapshots(zone, kind, ts)"
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

    def prune_old(self, now: float, retain_seconds: float) -> bool:
        """Apply retention to temperature, waste events and status snapshots.

        Runs at most once an hour (monotonic time; ``now`` only sets the cutoff); returns whether it ran. Open waste
        events, and snapshots of events still inside the window, are kept. Daily
        energy is long-term history and is never pruned.
        """
        with self._lock:
            tick = _monotonic()  # not the wall clock: it can jump back
            if self._last_prune is not None and tick - self._last_prune < _PRUNE_EVERY:
                return False
            self._last_prune = tick
        cutoff = float(now) - float(retain_seconds)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM temp_readings WHERE ts < ?", (cutoff,))
            # Keep: snapshots of open or in-window events, plus the newest resting
            # snapshot at or before each kept heating one (the event's baseline).
            live = "(SELECT id FROM waste_events WHERE open = 1 OR start_ts >= :c)"
            conn.execute(
                "DELETE FROM status_snapshots WHERE ts < :c "
                f"AND (event_id IS NULL OR event_id NOT IN {live}) "
                # One indexed lookup per kept heating snapshot. NULLs are filtered
                # out: a NULL inside NOT IN would make the delete remove nothing.
                "AND id NOT IN (SELECT baseline FROM (SELECT (SELECT r.id FROM status_snapshots r "
                "WHERE r.zone = h.zone AND r.kind = 'rest' AND r.ts <= h.ts "
                "ORDER BY r.ts DESC LIMIT 1) AS baseline "
                "FROM status_snapshots h WHERE h.kind = 'heating' "
                f"AND (h.ts >= :c OR h.event_id IN {live})) WHERE baseline IS NOT NULL)",
                {"c": cutoff},
            )
            conn.execute("DELETE FROM waste_events WHERE open = 0 AND start_ts < ?", (cutoff,))
        return True

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
