from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time

from hearthmage.atomicio import write_json_atomic
from hearthmage.domain import HearthError, Room, Scene
from hearthmage.energy import parse_energy
from hearthmage.schedule import Block, build_program_command, parse_day_schedule

_log = logging.getLogger("hearthmage.nexho")

# The hub replies with a single UDP datagram terminated by a NUL byte, which
# the official app strips (getLength()-1). Commands are ASCII, '/'-terminated.
_NULL = b"\x00"
_DEFAULT_PORT = 6653

# The hub is single-threaded: while it attempts RF to a radiator it cannot
# answer anything else. An unreachable radiator makes it return ER after ~5s, so
# the read timeout must be long enough to capture that ER in ONE attempt - a
# shorter timeout just fires repeated blocking RF attempts and starves writes.
_READ_RETRIES = 2
_READ_TIMEOUT = 6.0
_WRITE_RETRIES = 4
_WRITE_TIMEOUT = 3.0
_POLL_INTERVAL = 15.0
# Unreachable zones back off exponentially so they stop monopolising the hub;
# capped so a radiator that wakes is still noticed within a couple of minutes.
_BACKOFF_MAX = 120.0
# The hub is considered stale (worth a UI banner / unhealthy /healthz) if it has
# not answered a zone-list within this many poll intervals.
_STALE_INTERVALS = 4

_MIN_TEMP = 5.0
_MAX_TEMP = 30.0


class NexhoLocalClient:
    """Local UDP adapter for the Farho "Nexho NT" hub, backed by a poller.

    The hub speaks an unauthenticated plaintext UDP protocol on port 6653
    (reverse-engineered from the Android app). Because reaching a radiator over
    RF is slow and intermittent, a background thread polls the hub and keeps a
    per-zone cache; ``list_rooms`` serves that cache instantly so page loads
    never block. Zones start as ``loading`` and hydrate as the poller reads them.
    """

    def __init__(
        self,
        hub_ip: str,
        port: int = _DEFAULT_PORT,
        zone_names: dict[str, str] | None = None,
        poll_interval: float = _POLL_INTERVAL,
        cache_file: str | None = None,
        on_reading=None,
        on_energy=None,
        energy_interval: float = 3600.0,
    ) -> None:
        self._addr = (hub_ip, port)
        self._names = zone_names or {}
        self._poll_interval = poll_interval
        self._cache_file = cache_file
        # Optional callback(zone:int, current, setpoint) fired on each fresh read,
        # used to log history. Best-effort: a failure here must not disturb polling.
        self._on_reading = on_reading
        # Optional callback(zone:int, energy_dict) driven by an hourly energy sweep
        # that reads one zone per poll tick so it never stalls temperature polling.
        self._on_energy = on_energy
        self._energy_interval = energy_interval
        self._next_energy = 0.0  # monotonic; 0 = due on the first poll cycle
        self._energy_queue: list[int] = []
        self._lock = threading.Lock()
        self._state: dict[int, Room] = {}  # zone -> latest Room served to the UI
        self._last: dict[int, tuple[float | None, float | None]] = {}  # last good temps
        self._pending: dict[int, float] = {}  # zone -> target while a write is in flight
        self._desired: dict[int, int] = {}  # zone -> latest requested target (writes coalesce)
        self._writers: set[int] = set()  # zones with a live single-flight write worker
        self._zones: list[int] | None = None  # cached zone list (it is fixed)
        self._poller_started = False
        # Observability: monotonic timestamps for health reporting.
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_ops2_ok: float | None = None  # last successful zone-list
        self._last_fresh: dict[int, float] = {}  # zone -> last time it answered
        self._stale_after = _STALE_INTERVALS * poll_interval
        # Wire arbitration: the hub handles one conversation at a time, so all
        # I/O is serialised through _io_lock. Writes take priority - reads yield
        # while any write is pending so a user setpoint never waits behind the
        # poller (at most behind the single read already in flight).
        self._io_lock = threading.Lock()
        self._io_cond = threading.Condition()
        self._writes_pending = 0
        # Per-zone poll scheduling (monotonic clock). Unreachable zones back off.
        self._next_poll: dict[int, float] = {}
        self._backoff: dict[int, float] = {}
        self._load_cache()  # show last-known readings immediately on start

    def _load_cache(self) -> None:
        if not self._cache_file or not os.path.exists(self._cache_file):
            return
        try:
            with open(self._cache_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            _log.warning("readings cache read failed (%s): %s", self._cache_file, exc)
            return
        for zid, pair in (data or {}).items():
            try:
                zone = int(zid)
                current, target = pair[0], pair[1]
            except (TypeError, ValueError, IndexError):
                continue
            self._last[zone] = (current, target)
            self._state[zone] = self._room(zone, current, target, "ok")

    def _save_cache(self) -> None:
        if not self._cache_file:
            return
        with self._lock:
            data = {str(z): [c, t] for z, (c, t) in self._last.items()}
        try:
            write_json_atomic(self._cache_file, data, indent=None)
        except OSError as exc:
            _log.warning("readings cache write failed (%s): %s", self._cache_file, exc)

    def set_names(self, names: dict[str, str]) -> None:
        """Update zone display names and refresh the cached rooms in place."""
        with self._lock:
            self._names = dict(names)
            for zone, room in list(self._state.items()):
                self._state[zone] = self._room(
                    zone, room.current_temp, room.target_temp, room.status, room.error
                )

    # ---- transport -------------------------------------------------------

    def _wire_acquire(self, priority: bool) -> None:
        """Take the wire. Writes (priority) preempt reads: a read waits while any
        write is pending, so a setpoint never queues behind the poller."""
        if priority:
            with self._io_cond:
                self._writes_pending += 1
        else:
            with self._io_cond:
                while self._writes_pending > 0:
                    self._io_cond.wait()
        self._io_lock.acquire()

    def _wire_release(self, priority: bool) -> None:
        self._io_lock.release()
        if priority:
            with self._io_cond:
                self._writes_pending -= 1
                if self._writes_pending == 0:
                    self._io_cond.notify_all()

    def _ask(self, cmd: str, retries: int, timeout: float, priority: bool = False) -> str:
        self._wire_acquire(priority)
        try:
            last_exc: Exception | None = None
            for _ in range(retries):
                sock = None
                started = time.monotonic()
                try:
                    # Socket creation is inside the try so an OSError here (e.g. fd
                    # exhaustion) becomes a HearthError rather than escaping raw
                    # and killing the caller (a write worker or the poller).
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.settimeout(timeout)
                    sock.sendto(cmd.encode("ascii"), self._addr)
                    data, _ = sock.recvfrom(1024)
                    reply = data.rstrip(_NULL).decode("ascii", errors="replace")
                    _log.debug("hub %s -> %s (%.0fms)", cmd, reply, (time.monotonic() - started) * 1000)
                    return reply
                except socket.timeout:
                    _log.debug("hub %s timed out after %.1fs", cmd, timeout)
                    continue
                except OSError as exc:  # noqa: BLE001 - surface transport failures
                    last_exc = exc
                    break
                finally:
                    if sock is not None:
                        sock.close()
            detail = f": {last_exc}" if last_exc else ""
            _log.warning("no response from hub for %s%s", cmd, detail)
            raise HearthError(f"No response from hub for {cmd!r}{detail}")
        finally:
            self._wire_release(priority)

    def _zone_ids(self) -> list[int]:
        try:
            resp = self._ask("OPS2/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            if self._zones is not None:
                return self._zones
            raise
        if not resp.startswith("OPOK,OPS2"):
            if self._zones is not None:
                return self._zones
            raise HearthError(f"Unexpected zone list response: {resp!r}")
        ids: list[int] = []
        for tok in resp.split(",")[2:]:
            tok = tok.strip()
            if tok in ("", "0"):  # a 0 field marks the end of used slots
                break
            if tok.isdigit():
                ids.append(int(tok))
        self._zones = ids
        self._last_ops2_ok = time.monotonic()  # hub answered: health clock resets
        return ids

    def _room(
        self,
        zone: int,
        current: float | None,
        target: float | None,
        status: str,
        error: str | None = None,
    ) -> Room:
        return Room(
            id=str(zone),
            name=self._names.get(str(zone), f"Zone {zone}"),
            current_temp=current,
            target_temp=target,
            is_off=(target == 0.0),  # a 0 setpoint means the heater is off
            min_temp=_MIN_TEMP,
            max_temp=_MAX_TEMP,
            status=status,
            error=error,
        )

    def _read_zone_raw(self, zone: int) -> tuple[float | None, float | None, bool]:
        """Query the radiator; return (current, target, fresh) where ``fresh``
        means the radiator actually answered this read."""
        try:
            resp = self._ask(f"R#{zone}#1#0#0*?T/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            resp = "ER"  # radiator did not answer over RF
        current, target = _parse_status(resp)
        return current, target, current is not None or target is not None

    # ---- background poller ----------------------------------------------

    def start(self) -> None:
        if self._poller_started:
            return
        self._poller_started = True
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._poll_loop, name="nexho-poller", daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        while True:
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 - a bad poll must not kill the poller
                _log.exception("poll cycle failed; poller continues")
            time.sleep(self._sleep_until_due())

    def _sleep_until_due(self) -> float:
        """Sleep until the soonest zone is due, so the hub sits idle in between
        and a user write meets a free hub. Capped at the poll interval."""
        if not self._next_poll:
            return 1.0
        soonest = min(self._next_poll.values())
        return max(1.0, min(self._poll_interval, soonest - time.monotonic()))

    def _poll_once(self) -> None:
        zones = self._zone_ids()
        now = time.monotonic()
        with self._lock:
            for zone in zones:
                self._state.setdefault(zone, self._room(zone, None, None, "loading"))
        # Poll sequentially - the hub serialises anyway, and concurrent reads
        # only pile contention onto it. Zones that are backing off are skipped so
        # an asleep radiator does not keep blocking the hub every cycle.
        for zone in zones:
            if self._next_poll.get(zone, 0.0) > now:
                continue
            fresh = self._poll_zone(zone)
            self._schedule_next(zone, fresh)
        self._save_cache()
        self._poll_energy_step(zones, now)

    def _poll_energy_step(self, zones: list[int], now: float) -> None:
        """Read at most one zone's energy per cycle when the hourly sweep is due,
        so energy reads never stall the temperature poller."""
        if self._on_energy is None:
            return
        if not self._energy_queue and now >= self._next_energy:
            self._energy_queue = list(zones)  # start a new sweep
            self._next_energy = now + self._energy_interval
        if not self._energy_queue:
            return
        zone = self._energy_queue.pop(0)
        data = self.read_energy(str(zone))
        if data is not None:
            try:
                self._on_energy(zone, data)
            except Exception:  # noqa: BLE001 - energy logging must not break polling
                _log.exception("on_energy hook failed for zone %s", zone)

    def _schedule_next(self, zone: int, fresh: bool) -> None:
        if fresh:
            self._backoff[zone] = self._poll_interval  # reachable: steady cadence
        else:
            prev = self._backoff.get(zone, self._poll_interval)
            self._backoff[zone] = min(prev * 2, _BACKOFF_MAX)  # unreachable: back off
        self._next_poll[zone] = time.monotonic() + self._backoff[zone]

    def _poll_zone(self, zone: int) -> bool:
        """Read one zone into the cache; return True if the radiator answered."""
        current, target, fresh = self._read_zone_raw(zone)
        if fresh:
            self._last_fresh[zone] = time.monotonic()
            _log.debug("zone %s fresh: current=%s target=%s", zone, current, target)
            if self._on_reading is not None:
                try:
                    self._on_reading(zone, current, target)
                except Exception:  # noqa: BLE001 - history logging must not break polling
                    _log.exception("on_reading hook failed for zone %s", zone)
        else:
            _log.debug("zone %s did not answer (RF asleep)", zone)
        with self._lock:
            pending = self._pending.get(zone)
            if fresh and zone not in self._writers:
                # No write in flight: the radiator's answer is ground truth and
                # reconciles any now-settled pending write. (A write worker,
                # while active, may have this read sampling the OLD setpoint, so
                # we must not treat a fresh read as truth until it finishes.)
                self._last[zone] = (current, target)
                self._pending.pop(zone, None)
                self._state[zone] = self._room(zone, current, target, "ok")
            elif pending is not None:
                # A write is in flight (this read may predate it) or the radiator
                # is unreachable: keep the user's target. Refresh the room
                # temperature from a fresh read, but never touch the target.
                if fresh:
                    prev = self._last.get(zone)
                    self._last[zone] = (current, prev[1] if prev else None)
                last = self._last.get(zone)
                self._state[zone] = self._room(zone, last[0] if last else None, pending, "ok")
            elif fresh:
                # Fresh read, a write is in flight but there is no pending target
                # to protect: take the reading.
                self._last[zone] = (current, target)
                self._state[zone] = self._room(zone, current, target, "ok")
            else:
                last = self._last.get(zone)
                if last is not None:
                    self._state[zone] = self._room(zone, last[0], last[1], "ok")
                else:
                    self._state[zone] = self._room(zone, None, None, "unreachable")
        return fresh

    # ---- HearthClient interface (non-blocking; serves the cache) --------

    def list_rooms(self) -> list[Room]:
        with self._lock:
            if self._state:
                return [self._state[zone] for zone in sorted(self._state)]
        # Cold start (poller has not filled anything yet): seed loading
        # placeholders so the page renders instantly.
        zones = self._zone_ids()
        with self._lock:
            for zone in zones:
                self._state.setdefault(zone, self._room(zone, None, None, "loading"))
            return [self._state[zone] for zone in sorted(self._state)]

    def hub_stale(self) -> bool:
        """True if the hub has not answered a zone-list recently enough to trust
        (drives the UI banner). False during the initial startup grace period."""
        now = time.monotonic()
        if self._last_ops2_ok is not None:
            return (now - self._last_ops2_ok) > self._stale_after
        if self._started_at is None:
            return False  # not started yet: nothing to be stale about
        return (now - self._started_at) > self._stale_after  # started but never answered

    def health(self) -> dict:
        """A snapshot for /healthz: poller liveness, hub recency, per-zone state."""
        now = time.monotonic()
        with self._lock:
            zones = {
                str(z): {
                    "status": self._state[z].status,
                    "last_fresh_seconds_ago": (
                        round(now - self._last_fresh[z], 1) if z in self._last_fresh else None
                    ),
                }
                for z in sorted(self._state)
            }
        hub_ago = round(now - self._last_ops2_ok, 1) if self._last_ops2_ok is not None else None
        return {
            "poller_alive": bool(self._thread and self._thread.is_alive()),
            "hub_last_contacted_seconds_ago": hub_ago,
            "hub_stale": self.hub_stale(),
            "zones": zones,
        }

    def set_temperature(self, room_id: str, temperature: float) -> None:
        """Optimistic write: reflect the new setpoint instantly, send in the
        background. Writes to a zone are coalesced through a single-flight worker
        so rapid taps converge on the last requested target instead of racing -
        at most one datagram is in flight per zone, and only the final value is
        confirmed. ``_last`` (the confirmed cache) is updated only on ack.
        """
        zone = _zone_int(room_id)
        if not math.isfinite(temperature):
            raise HearthError(f"Invalid temperature: {temperature!r}")
        # A non-positive request means OFF (setpoint 0). Otherwise clamp to the
        # supported range and snap to the nearest half degree before it becomes a
        # wire command - the min/max are otherwise only used for display, so a
        # crafted request could send an out-of-range setpoint to the radiator.
        if temperature <= 0:
            target = 0.0
        else:
            temperature = max(_MIN_TEMP, min(_MAX_TEMP, temperature))
            target = round(temperature * 2) / 2
        with self._lock:
            prev = self._state.get(zone)
            current = prev.current_temp if prev else None
            self._pending[zone] = target  # poll must not revert this
            self._desired[zone] = target  # latest wins
            self._state[zone] = self._room(zone, current, target, "ok")
            if zone in self._writers:
                return  # the running worker will pick up the new desired target
            self._writers.add(zone)
        threading.Thread(
            target=self._drain_writes, args=(zone,), name="nexho-write", daemon=True
        ).start()

    def _drain_writes(self, zone: int) -> None:
        """Single-flight write worker: send the desired target, and if a newer
        one arrived while sending, send that instead - repeat until settled."""
        try:
            while True:
                with self._lock:
                    target = self._desired.get(zone)
                    if target is None:
                        self._writers.discard(zone)  # atomic with the empty check
                        return
                ok = self._send_once(zone, target)
                with self._lock:
                    if self._desired.get(zone) != target:
                        continue  # superseded by a newer tap; loop to send the latest
                    self._desired.pop(zone, None)
                    if ok:
                        prev = self._state.get(zone)
                        current = prev.current_temp if prev else None
                        self._last[zone] = (current, float(target))
                        self._pending.pop(zone, None)  # confirmed - release poll guard
                        self._state[zone] = self._room(zone, current, float(target), "ok")
                    # On no-ack (radiator asleep on RF) keep the optimistic value and
                    # leave it pending; the poller reconciles from the next read.
                    self._writers.discard(zone)
                    return
        finally:
            # Safety net: never leave the zone marked as having a live worker if
            # the loop raised unexpectedly, or no future write to it would ever
            # be sent (set_temperature would assume a worker is running).
            with self._lock:
                self._writers.discard(zone)

    def _send_once(self, zone: int, target: float) -> bool:
        """Send one setpoint command; True if the hub acknowledged. ``target`` is
        the display temperature (0.0 = off); it is encoded to the wire value here.
        Resending the same target is idempotent - the radiator ends at that value."""
        wire = _encode_setpoint(target)
        try:
            resp = self._ask(
                f"D#{zone}#1#0#0*T{wire}/", _WRITE_RETRIES, _WRITE_TIMEOUT, priority=True
            )
            acked = resp.startswith("OK")
            if acked:
                _log.info("zone %s setpoint %s acknowledged", zone, target)
            else:
                _log.warning("zone %s setpoint %s not acknowledged: %s", zone, target, resp)
            return acked
        except HearthError:
            _log.warning("zone %s setpoint %s: no response from hub", zone, target)
            return False

    # ---- per-radiator element config (*?E read / *SEP write) -------------
    # Element parameters are addressed by (group, index). Confirmed live:
    # reads return ``OK,<value>``; writes ``D#<z>#1#0#0*SEP#<g>#<i>#<v>/`` ack OK
    # and persist (no separate commit packet needed).
    _EL_KEYPAD = (0, 8)  # bit0 = keypad locked
    _EL_WINDOW = (0, 6)  # 1 = open-window detection on
    _EL_OFFSET = (1, 20)  # temperature-offset calibration, 0..17
    _OFFSET_MIN = 0
    _OFFSET_MAX = 17

    def _read_element(self, zone: int, group: int, index: int) -> int | None:
        try:
            resp = self._ask(f"R#{zone}#1#0#0*?E#{group}#{index}/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            return None
        if not resp.startswith("OK,"):
            return None
        return _as_int(resp.split(",")[1]) if "," in resp else None

    def _write_element(self, zone: int, group: int, index: int, value: int) -> None:
        resp = self._ask(
            f"D#{zone}#1#0#0*SEP#{group}#{index}#{value}/",
            _WRITE_RETRIES,
            _WRITE_TIMEOUT,
            priority=True,
        )
        if not resp.startswith("OK"):
            raise HearthError(f"Config write failed for zone {zone} ({group}#{index}): {resp!r}")

    def read_config(self, room_id: str) -> dict | None:
        """Read keypad-lock, window-sensor and offset for a zone; None if asleep."""
        zone = _zone_int(room_id)
        keypad = self._read_element(zone, *self._EL_KEYPAD)
        if keypad is None:  # first read failed: radiator asleep, don't bother more
            return None
        window = self._read_element(zone, *self._EL_WINDOW)
        offset = self._read_element(zone, *self._EL_OFFSET)
        return {
            "keypad_lock": bool(keypad & 1),
            "window_sensor": bool(window),
            "offset": offset,
        }

    def set_config(
        self,
        room_id: str,
        keypad_lock: bool | None = None,
        window_sensor: bool | None = None,
        offset: int | None = None,
    ) -> None:
        """Write only the provided per-radiator settings. Validates the offset."""
        zone = _zone_int(room_id)
        if offset is not None and not (self._OFFSET_MIN <= offset <= self._OFFSET_MAX):
            raise HearthError(
                f"Offset {offset} out of range {self._OFFSET_MIN}..{self._OFFSET_MAX}"
            )
        if keypad_lock is not None:
            self._write_element(zone, *self._EL_KEYPAD, int(bool(keypad_lock)))
        if window_sensor is not None:
            self._write_element(zone, *self._EL_WINDOW, int(bool(window_sensor)))
        if offset is not None:
            self._write_element(zone, *self._EL_OFFSET, int(offset))

    # ---- holiday hold (*RH) ----------------------------------------------
    # Confirmed live: D#<z>#1#0#0*RH#<day>#<month>#0#<tempEncoded>/ holds the zone
    # at the frost temp until the date; *RH#0#0#0#251 clears it (reads back 253).
    # The read *?RH returns OK,RH,<...>, with day/month/temp at fixed offsets.
    _RH_OFF_SENTINELS = (0, 251, 253)

    def set_holiday(self, room_id: str, day: int, month: int, temp: float) -> None:
        """Hold a zone at ``temp`` until ``day``/``month`` (hub-side, auto-resumes)."""
        zone = _zone_int(room_id)
        resp = self._ask(
            f"D#{zone}#1#0#0*RH#{int(day)}#{int(month)}#0#{_encode_setpoint(temp)}/",
            _WRITE_RETRIES,
            _WRITE_TIMEOUT,
            priority=True,
        )
        if not resp.startswith("OK"):
            raise HearthError(f"Holiday set failed for zone {zone}: {resp!r}")

    def clear_holiday(self, room_id: str) -> None:
        """Cancel a zone's holiday hold (the off sentinel 251)."""
        zone = _zone_int(room_id)
        resp = self._ask(
            f"D#{zone}#1#0#0*RH#0#0#0#251/", _WRITE_RETRIES, _WRITE_TIMEOUT, priority=True
        )
        if not resp.startswith("OK"):
            raise HearthError(f"Holiday clear failed for zone {zone}: {resp!r}")

    def read_holiday(self, room_id: str) -> dict | None:
        """Read a zone's holiday hold; None if the radiator did not answer.

        The reply is ``OK,RH,<fields>`` with day at index 10, month at 11 and the
        (encoded) hold temperature at 13; an off hold reads those as sentinels."""
        zone = _zone_int(room_id)
        try:
            resp = self._ask(f"R#{zone}#1#0#0*?RH/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            return None
        parts = resp.split(",")
        if not resp.startswith("OK,RH,") or len(parts) < 14:
            return None
        temp_field = _as_int(parts[13])
        if temp_field is None or temp_field in self._RH_OFF_SENTINELS:
            return {"active": False, "day": None, "month": None, "temp": None}
        return {
            "active": True,
            "day": _as_int(parts[10]),
            "month": _as_int(parts[11]),
            "temp": _decode_setpoint(temp_field),
        }

    def read_energy(self, room_id: str) -> dict | None:
        """Read a zone's consumption counters, or None if the radiator is asleep.

        Pre-probes with the cheap ``*UDEL`` before the heavier ``*?UD``/``*?UM``
        reads, so an asleep zone is skipped without three slow RF attempts.
        Returns ``{rated_watts, daily, monthly}`` with raw counters (decode with
        hearthmage.energy)."""
        zone = _zone_int(room_id)
        try:
            probe = self._ask(f"R#{zone}#1#0#0*UDEL/", _READ_RETRIES, _READ_TIMEOUT)
            if not probe.startswith("OK"):
                return None
            ud = self._ask(f"R#{zone}#1#0#0*?UD/", _READ_RETRIES, _READ_TIMEOUT)
            um = self._ask(f"R#{zone}#1#0#0*?UM/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            return None
        watts, daily = parse_energy(ud)
        _, monthly = parse_energy(um)
        if watts is None:
            _log.debug("zone %s energy read returned no data: %s", zone, ud)
            return None
        return {"rated_watts": watts, "daily": daily, "monthly": monthly}

    def set_scene_membership(self, room_id: str, scene: Scene, member: bool) -> None:
        # Nexho climate zones expose no on/off or scene-activate opcode on the
        # LAN protocol; the web UI is setpoint-only. Present for the interface.
        raise HearthError("On/off and presets are not supported by the Nexho local protocol")

    # ---- weekly schedule (native hub programs) --------------------------

    def get_day_schedule(self, room_id: str, day: int) -> list[Block]:
        """Read one weekday's schedule blocks for a zone (via ``*OD``)."""
        zone = _zone_int(room_id)
        resp = self._ask(f"R#{zone}#1#0#0*OD{day}/", _READ_RETRIES, _READ_TIMEOUT)
        return parse_day_schedule(resp)

    def set_day_pattern(self, room_id: str, blocks: list[Block], days: list[int]) -> None:
        """Write a block pattern to every weekday in ``days`` (via ``*P``)."""
        zone = _zone_int(room_id)
        resp = self._ask(
            build_program_command(zone, blocks, days),
            _WRITE_RETRIES,
            _WRITE_TIMEOUT,
            priority=True,
        )
        if not resp.startswith("OK"):
            raise HearthError(f"Schedule write failed for zone {zone}: {resp!r}")

    def set_program_active(self, room_id: str, active: bool) -> None:
        """Turn a zone's weekly program on (``*ON``) or off (``*OFF``).

        Activated, the hub runs the stored schedule for the zone; deactivated,
        the zone holds its manual setpoint. Confirmed against the hub.
        """
        zone = _zone_int(room_id)
        op = "ON" if active else "OFF"
        resp = self._ask(f"D#{zone}#1#0#0*{op}/", _WRITE_RETRIES, _WRITE_TIMEOUT, priority=True)
        if not resp.startswith("OK"):
            raise HearthError(f"Program {op} failed for zone {zone}: {resp!r}")


def _encode_setpoint(temp: float | None) -> int:
    """Display temperature -> the value the hub expects after ``*T``.

    Whole degrees pass through (``20`` -> ``20``); half degrees are offset by
    127.5 (``20.5`` -> ``148``); anything <= 0 (or None) is OFF, sent as ``0``.
    """
    if temp is None or temp <= 0:
        return 0
    half = round(temp * 2) / 2
    if half == int(half):
        return int(half)
    return int(half + 127.5)


def _decode_setpoint(value: int) -> float:
    """Inverse of :func:`_encode_setpoint`. ``0`` (off) reads back as ``0.0``."""
    if value >= 128:
        return value - 127.5
    return float(value)


def _parse_status(resp: str) -> tuple[float | None, float | None]:
    """Parse an ``R#`` reply into (current_temp, target_temp).

    Success looks like ``OK,<tempWhole>,<tempTenths>,<setpoint>`` e.g.
    ``OK,20,04,007`` -> room 20.4 deg, setpoint 7 deg. A setpoint >=128 is a
    half-degree (``148`` -> 20.5) and ``0`` is OFF (target 0.0). ``ER`` means the
    radiator was unreachable over RF, so both values are unknown.
    """
    if not resp.startswith(("OK,", "OPOK,")):
        return None, None
    parts = resp.split(",")
    current: float | None = None
    target: float | None = None
    whole = _as_int(parts[1]) if len(parts) > 1 else None
    tenths = _as_int(parts[2]) if len(parts) > 2 else None
    if whole is not None:
        current = whole + (tenths or 0) / 10.0
    setpoint = _as_int(parts[3]) if len(parts) > 3 else None
    if setpoint is not None:
        target = _decode_setpoint(setpoint)
    return current, target


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _zone_int(room_id: str) -> int:
    try:
        return int(room_id)
    except (TypeError, ValueError) as exc:
        raise HearthError(f"Invalid zone id: {room_id!r}") from exc
