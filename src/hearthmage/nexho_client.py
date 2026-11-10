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
from hearthmage.schedule import Block, build_program_command, parse_program_day

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
_WRITE_TIMEOUT = 5.0  # the vendor app waits 5 s; a radio attempt takes about that long
# The vendor app waits up to 45 s for a weekly-program write; we wait less so a
# dead radiator cannot hold the wire for minutes, but well past one radio try.
_PROGRAM_WRITE_TIMEOUT = 15.0
_PROGRAM_WRITE_RETRIES = 2
# A radiator can miss an RF window and the hub then answers ER,1 at once; the
# same command a few seconds later usually succeeds (live 2026-11-01). The
# vendor app resends on ER too. Used for program reads/writes and on/off.
_ER_ATTEMPTS = 4
_ER_GAP = 3.0
_POLL_INTERVAL = 15.0
# Unreachable zones back off exponentially so they stop monopolising the hub;
# capped so a radiator that wakes is still noticed within a couple of minutes.
_BACKOFF_MAX = 120.0
# The hub is considered stale (worth a UI banner / unhealthy /healthz) if it has
# not answered a zone-list within this many poll intervals.
_STALE_INTERVALS = 4

# A radiator dozes off over RF for a few minutes at a time, which is normal and
# not worth a message. Silent for longer than this, the UI says so next to the
# last reading (it may be switched off at the wall).
_SILENT_AFTER = 600.0
# Heater counts (OPS3) are re-read this often even when the zone list has not
# changed: a zone deleted in the vendor app keeps its OPS2 slot and only shows
# up as zero heaters. The hub answers OPS3 itself, with no radio involved.
_HEATERS_REFRESH = 600.0

_MAX_PLAUSIBLE_ROOM = 60.0  # vendor app: a room reading above this is re-read
_MAX_PLAUSIBLE_TARGET = 35.0  # ...and a setpoint above this
_MIN_TEMP = 5.0
_MAX_TEMP = 30.0
# "Off" is sent as a 1 degree setpoint: *T0 did not reliably turn radiators off.
_OFF_TEMP = 1.0


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
        self._write_seq: dict[int, int] = {}  # zone -> bumped on every requested write
        self._zones: list[int] | None = None  # cached zone list (it is fixed)
        # Heaters (modules) per zone from OPS3/, read when the zone list changes.
        # Every D# write carries the count, as the vendor app does; unknown = 1.
        self._heaters: dict[int, int] = {}
        self._heaters_for: list[int] | None = None  # the OPS2 ids they were read for
        self._heaters_read_at = 0.0  # monotonic time of the last good OPS3 read
        self._poller_started = False
        self._stop = threading.Event()  # set by stop(): the poller exits
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
                    # Connected: the OS then drops datagrams from any address
                    # but the hub's, so another LAN host cannot answer for it.
                    sock.connect(self._addr)
                    sock.send(cmd.encode("ascii"))
                    data = sock.recv(1024)
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

    def _ask_until_answered(
        self, cmd: str, ok_prefix: str, priority: bool,
        retries: int | None = None, timeout: float | None = None,
    ) -> str:
        """Send ``cmd``, resending after a short gap while the hub answers ER
        (radiator missed the RF window). The wire is released between attempts
        so other traffic is not blocked. Returns the last reply."""
        default = (_WRITE_RETRIES, _WRITE_TIMEOUT) if priority else (_READ_RETRIES, _READ_TIMEOUT)
        retries = retries if retries is not None else default[0]
        timeout = timeout if timeout is not None else default[1]
        resp = ""
        for attempt in range(_ER_ATTEMPTS):
            resp = self._ask(cmd, retries, timeout, priority=priority)
            if resp.startswith(ok_prefix) or not resp.startswith("ER"):
                return resp
            if attempt + 1 < _ER_ATTEMPTS:
                _log.debug("hub %s -> %s; retrying in %.0fs", cmd, resp, _ER_GAP)
                time.sleep(_ER_GAP)
        return resp

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
        now = time.monotonic()
        self._last_ops2_ok = now  # hub answered: health clock resets
        if ids != self._heaters_for or now - self._heaters_read_at >= _HEATERS_REFRESH:
            self._read_heater_counts(ids)
        # A zone with no heaters is a deleted slot; the vendor app hides it too.
        ids = [z for z in ids if self._heaters.get(z, 1) > 0]
        self._forget_zones_except(ids)
        return ids

    def _forget_zones_except(self, ids: list[int]) -> None:
        """The hub has just told us its zones: drop everything held for any
        other zone. Without this a zone that was deleted, or that came from the
        readings cache of another hub, stays on screen as "ok" with its last
        reading for ever. Only called on a real answer, never on a hub failure."""
        keep = set(ids)
        with self._lock:
            self._zones = ids
            for table in (self._state, self._last, self._pending, self._desired,
                          self._next_poll, self._backoff, self._last_fresh):
                for zone in [z for z in table if z not in keep]:
                    del table[zone]

    def _read_heater_counts(self, ids: list[int]) -> None:
        """Read OPS3/ (heaters per zone, in OPS2 order). On failure the counts
        stay unknown and writes fall back to one heater; nothing is hidden."""
        try:
            resp = self._ask("OPS3/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            return
        if not resp.startswith("OPOK,OPS3,"):
            return
        counts = [_as_int(tok) for tok in resp.split(",")[2:]]
        with self._lock:
            self._heaters = {z: c for z, c in zip(ids, counts) if c is not None}
            self._heaters_for = ids
            self._heaters_read_at = time.monotonic()

    def _require_zone(self, room_id: str) -> int:
        """Zone number for a request, refused if the hub has no such zone. The
        id comes from a URL or an MQTT topic, so without this any number would
        be put on the wire and leave a phantom card behind. Before the hub has
        answered once, the zones restored from the readings cache stand in; with
        neither there is nothing to check against yet."""
        zone = _zone_int(room_id)
        with self._lock:
            known = self._zones if self._zones is not None else (list(self._state) or None)
        if known is not None and zone not in known:
            raise HearthError(f"Unknown zone: {room_id}")
        return zone

    def _modules(self, zone: int) -> int:
        """Heaters in a zone, for the second field of D# writes (default 1)."""
        count = self._heaters.get(zone, 1)
        return count if count > 0 else 1

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
            # 0 (*T0) or our 1 degree "off" setpoint both mean the heater is off
            is_off=(target is not None and target <= _OFF_TEMP),
            min_temp=_MIN_TEMP,
            max_temp=_MAX_TEMP,
            status=status,
            error=error,
            silent_for=self._silent_for(zone),
        )

    def _silent_for(self, zone: int) -> float | None:
        """How long the radiator has gone without answering, or None if that is
        not long enough to mention. A zone restored from the readings cache that
        has not answered since start is counted from when polling started."""
        since = self._last_fresh.get(zone, self._started_at)
        if since is None:
            return None
        silent = time.monotonic() - since
        return silent if silent >= _SILENT_AFTER else None

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

    def stop(self) -> None:
        """Stop the poller (e.g. the app switched to a different hub). A poll
        already talking to the hub finishes first; nothing new starts."""
        self._stop.set()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 - a bad poll must not kill the poller
                _log.exception("poll cycle failed; poller continues")
            self._stop.wait(self._sleep_until_due())

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
        # Note the write activity before the read goes out. If a write is in
        # flight now, or one is requested before this read is applied, the read
        # may have sampled the radiator before that write landed, so its target
        # is not ground truth (the room temperature still is).
        with self._lock:
            seq_before = self._write_seq.get(zone, 0)
            writing_before = zone in self._writers
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
            undisturbed = (
                not writing_before
                and zone not in self._writers
                and self._write_seq.get(zone, 0) == seq_before
            )
            if fresh and undisturbed:
                # No write anywhere near this read: the radiator's answer is
                # ground truth and reconciles any now-settled pending write.
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
                # A write overlapped this read and has already been confirmed
                # (nothing pending): keep the confirmed target, which is newer
                # than what this read may have sampled, and take the room
                # temperature. The next undisturbed read settles any difference.
                prev = self._last.get(zone)
                kept = prev[1] if prev is not None else target
                self._last[zone] = (current, kept)
                self._state[zone] = self._room(zone, current, kept, "ok")
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
        zone = self._require_zone(room_id)
        if not math.isfinite(temperature):
            raise HearthError(f"Invalid temperature: {temperature!r}")
        # A non-positive request means OFF (sent as _OFF_TEMP). Otherwise clamp to
        # the supported range and snap to the nearest half degree before it becomes
        # a wire command - the min/max are otherwise only used for display, so a
        # crafted request could send an out-of-range setpoint to the radiator.
        if temperature <= 0:
            target = _OFF_TEMP
        else:
            temperature = max(_MIN_TEMP, min(_MAX_TEMP, temperature))
            target = round(temperature * 2) / 2
        with self._lock:
            prev = self._state.get(zone)
            current = prev.current_temp if prev else None
            self._pending[zone] = target  # poll must not revert this
            self._desired[zone] = target  # latest wins
            self._write_seq[zone] = self._write_seq.get(zone, 0) + 1  # see _poll_zone
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
        the display temperature (_OFF_TEMP = off); it is encoded to the wire value here.
        Resending the same target is idempotent - the radiator ends at that value."""
        wire = _encode_setpoint(target)
        try:
            resp = self._ask_until_answered(f"D#{zone}#{self._modules(zone)}#0#0*T{wire}/", "OK", priority=True)
            acked = resp == "OK"  # the vendor treats "OK,<...>" as a command error
            if acked:
                _log.info("zone %s setpoint %s acknowledged", zone, target)
            else:
                _log.warning("zone %s setpoint %s not acknowledged: %s", zone, target, resp)
            return acked
        except HearthError:
            _log.warning("zone %s setpoint %s: no response from hub", zone, target)
            return False

    # ---- per-radiator element config (*?E read / *SEP write) -------------
    # Element parameters are addressed by (group, index): read with
    # ``R#<z>#1#0#0*?E#<g>#<i>/`` -> ``OK,<value>``, written with ``*SEP`` and
    # acked with a bare ``OK`` (no commit packet). Meanings and write forms
    # follow the decompiled vendor app (HeatApp 4.1.1); see the protocol doc.
    _EL_KEYPAD = (0, 8)  # bit0 = keypad locked; bit7 is kept on write (vendor does)
    _EL_WINDOW = (0, 6)  # 1 = open-window detection on
    _EL_FROST = (1, 20)  # anti-ice (frost protection) temperature, 0..17 C
    _EL_OFFSET = (0, 9)  # sensor offset, signed tenths of a degree, -9.9..+9.9 C
    FROST_MIN = 0
    FROST_MAX = 17
    OFFSET_LIMIT = 9.9

    def _read_element(self, zone: int, group: int, index: int) -> int | None:
        try:
            resp = self._ask(f"R#{zone}#1#0#0*?E#{group}#{index}/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            return None
        if not resp.startswith("OK,"):
            return None
        return _as_int(resp.split(",")[1]) if "," in resp else None

    def _write_element(
        self, zone: int, group: int, index: int, value: int, op: str = "D", module: int | None = None
    ) -> None:
        module = module if module is not None else self._modules(zone)
        resp = self._ask(
            f"{op}#{zone}#{module}#0#0*SEP#{group}#{index}#{value}/",
            _WRITE_RETRIES,
            _WRITE_TIMEOUT,
            priority=True,
        )
        if resp != "OK":
            raise HearthError(f"Config write failed for zone {zone} ({group}#{index}): {resp!r}")

    def read_config(self, room_id: str) -> dict | None:
        """Read keypad lock, window sensor, anti-ice temperature and sensor
        offset for a zone; None if the radiator is asleep. A setting whose own
        read failed is None (unknown), never a default: the settings form must
        not be able to write a guess back to the radiator."""
        zone = self._require_zone(room_id)
        keypad = self._read_element(zone, *self._EL_KEYPAD)
        if keypad is None:  # first read failed: radiator asleep, don't bother more
            return None
        window = self._read_element(zone, *self._EL_WINDOW)
        frost = self._read_element(zone, *self._EL_FROST)
        offset = self._read_element(zone, *self._EL_OFFSET)
        return {
            "keypad_lock": bool(keypad & 1),
            "window_sensor": bool(window) if window is not None else None,
            "frost_temp": frost,
            "sensor_offset": _decode_offset(offset) if offset is not None else None,
        }

    def set_config(
        self,
        room_id: str,
        keypad_lock: bool | None = None,
        window_sensor: bool | None = None,
        frost_temp: int | None = None,
        sensor_offset: float | None = None,
    ) -> None:
        """Write only the provided per-radiator settings, validated first."""
        zone = self._require_zone(room_id)
        if frost_temp is not None and not (self.FROST_MIN <= frost_temp <= self.FROST_MAX):
            raise HearthError(
                f"Anti-ice temperature {frost_temp} out of range {self.FROST_MIN}..{self.FROST_MAX}"
            )
        if sensor_offset is not None and not (
            math.isfinite(sensor_offset) and abs(sensor_offset) <= self.OFFSET_LIMIT + 1e-9
        ):
            raise HearthError(f"Sensor offset {sensor_offset} out of range +/-{self.OFFSET_LIMIT}")
        if keypad_lock is not None:
            current = self._read_element(zone, *self._EL_KEYPAD)
            if current is None:
                raise HearthError(f"Zone {zone}: could not read the keypad setting to update it")
            self._write_element(zone, *self._EL_KEYPAD, (current & 0x80) | int(bool(keypad_lock)))
        if window_sensor is not None:
            self._write_element(zone, *self._EL_WINDOW, int(bool(window_sensor)))
        if frost_temp is not None:
            self._write_element(zone, *self._EL_FROST, int(frost_temp))
        if sensor_offset is not None:
            # The vendor sends this one with R#, to each heater by its index.
            for module in range(1, self._modules(zone) + 1):
                self._write_element(
                    zone, *self._EL_OFFSET, round(sensor_offset * 10) & 0xFF, op="R", module=module
                )

    # ---- holiday hold (*RH) ----------------------------------------------
    # From the decompiled vendor app (AdvConfigHoliday, a0/j.java):
    #   D#<z>#<mod>#0#0*RH#<days from today>#<hour>#<minute>#<arrival temp>/
    # sets the date and time the occupants are back and the temperature to
    # switch to then (251 = resume the weekly program); *RH#0#0#0#251 clears it.
    # The read *?RH returns OK,RH,... with [10] days left (<= 240 means a hold is
    # set, 253 none), [11] hour, [12] minute, [13] arrival temp (251 = program).
    _RH_RESUME_PROGRAM = 251
    _RH_MAX_DAYS = 240

    def set_holiday(
        self, room_id: str, days: int, hour: int, minute: int, arrival_temp: float | None
    ) -> None:
        """Away until ``days`` from today at ``hour``:``minute``; then switch to
        ``arrival_temp``, or back to the weekly program if it is None."""
        zone = self._require_zone(room_id)
        if not (0 <= days <= self._RH_MAX_DAYS and 0 <= hour <= 23 and 0 <= minute <= 59):
            raise HearthError(f"Invalid holiday end: {days} days, {hour}:{minute:02d}")
        if arrival_temp is None:
            arrival = self._RH_RESUME_PROGRAM
        elif _MIN_TEMP <= arrival_temp <= _MAX_TEMP:
            arrival = _encode_setpoint(arrival_temp)
        else:
            raise HearthError(f"Arrival temperature {arrival_temp} out of range")
        resp = self._ask_until_answered(
            f"D#{zone}#{self._modules(zone)}#0#0*RH#{int(days)}#{int(hour)}#{int(minute)}#{arrival}/", "OK", priority=True
        )
        if resp != "OK":
            raise HearthError(f"Holiday set failed for zone {zone}: {resp!r}")

    def clear_holiday(self, room_id: str) -> None:
        """Cancel a zone's holiday hold (the vendor's clear: *RH#0#0#0#251)."""
        zone = self._require_zone(room_id)
        resp = self._ask_until_answered(
            f"D#{zone}#{self._modules(zone)}#0#0*RH#0#0#0#{self._RH_RESUME_PROGRAM}/", "OK", priority=True
        )
        if resp != "OK":
            raise HearthError(f"Holiday clear failed for zone {zone}: {resp!r}")

    def read_holiday(self, room_id: str) -> dict | None:
        """Read a zone's holiday hold; None if the radiator did not answer."""
        zone = self._require_zone(room_id)
        try:
            resp = self._ask(f"R#{zone}#1#0#0*?RH/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            return None
        parts = resp.split(",")
        if not resp.startswith("OK,RH,") or len(parts) < 14:
            return None
        days, hour, minute, arrival = (_as_int(p) for p in parts[10:14])
        if days is None or days > self._RH_MAX_DAYS:
            return {"active": False}
        return {
            "active": True,
            "days_left": days,
            "hour": hour,
            "minute": minute,
            "arrival_temp": None if arrival == self._RH_RESUME_PROGRAM or arrival is None
            else _decode_setpoint(arrival),
        }

    def read_energy(self, room_id: str) -> dict | None:
        """Read a zone's consumption counters, or None if the radiator is asleep.
        Returns ``{rated_watts, daily, monthly}`` with raw counters (decode with
        hearthmage.energy).

        Never send ``*UDEL``: in the vendor app it is the consumption RESET,
        behind a "confirm reset" dialog (it was once used here as a probe)."""
        zone = self._require_zone(room_id)
        try:
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

    def read_day_pattern(self, room_id: str, day: int) -> list[Block]:
        """Read one weekday's blocks back from the hub (``*?P<day>``, confirmed
        live). Raises HearthError if the radiator does not answer or holds
        something this app cannot represent."""
        zone = self._require_zone(room_id)
        if not 0 <= int(day) <= 6:
            raise HearthError(f"Invalid weekday: {day!r}")
        resp = self._ask_until_answered(f"R#{zone}#1#0#0*?P{int(day)}/", "OK,", priority=False)
        try:
            return parse_program_day(resp, int(day))
        except ValueError as exc:
            raise HearthError(f"Zone {zone} day {day}: {exc}") from exc

    def read_status_raw(self, room_id: str) -> str:
        """The radiator's full status reply (``*?F``), unparsed: room temperature,
        setpoint, its clock and weekday, firmware, and fields not yet mapped.
        Stored verbatim when diagnosing heating nobody asked for."""
        zone = self._require_zone(room_id)
        resp = self._ask_until_answered(f"R#{zone}#1#0#0*?F/", "OK,", priority=False)
        if not resp.startswith("OK,"):
            raise HearthError(f"Zone {zone} status read failed: {resp!r}")
        return resp

    def set_day_pattern(self, room_id: str, blocks: list[Block], days: list[int]) -> None:
        """Write a block pattern to every weekday in ``days`` (via ``*P``)."""
        zone = self._require_zone(room_id)
        try:
            cmd = build_program_command(zone, blocks, days, modules=self._modules(zone))
        except ValueError as exc:
            raise HearthError(f"Invalid schedule for zone {zone}: {exc}") from exc
        resp = self._ask_until_answered(
            cmd, "OK", priority=True,
            retries=_PROGRAM_WRITE_RETRIES, timeout=_PROGRAM_WRITE_TIMEOUT,
        )
        if resp != "OK":
            raise HearthError(f"Schedule write failed for zone {zone}: {resp!r}")

    def set_program_active(self, room_id: str, active: bool) -> None:
        """Turn a zone's weekly program on (``*ON``) or off (``*OFF``).

        Activated, the hub runs the stored schedule for the zone; deactivated,
        the zone holds its manual setpoint. Confirmed against the hub.
        """
        zone = self._require_zone(room_id)
        op = "ON" if active else "OFF"
        resp = self._ask_until_answered(f"D#{zone}#{self._modules(zone)}#0#0*{op}/", "OK", priority=True)
        if resp != "OK":
            raise HearthError(f"Program {op} failed for zone {zone}: {resp!r}")

    # ---- hub cloud reporting (memory address 3898) ------------------------
    # OPMR<addr>/ -> OPOK,<value>; OPMW<addr>,<value>/ -> OPOK. Confirmed live.

    def read_cloud_reporting(self) -> bool:
        """Whether the hub sends data to the vendor's external server."""
        resp = self._ask(f"OPMR{_CLOUD_REPORTING_ADDR}/", _READ_RETRIES, _READ_TIMEOUT)
        parts = resp.split(",")
        if parts[0] != "OPOK" or len(parts) < 2 or parts[1] not in ("0", "1"):
            raise HearthError(f"Unexpected cloud reporting reply: {resp!r}")
        return parts[1] == "1"

    def set_cloud_reporting(self, enabled: bool) -> None:
        """Turn the hub's reporting to the vendor's external server on or off."""
        value = 1 if enabled else 0
        resp = self._ask(
            f"OPMW{_CLOUD_REPORTING_ADDR},{value}/", _WRITE_RETRIES, _WRITE_TIMEOUT, priority=True
        )
        if resp != "OPOK":
            raise HearthError(f"Cloud reporting change failed: {resp!r}")


_CLOUD_REPORTING_ADDR = 3898  # hub memory: "send data to external server" (vendor app)


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


def _decode_offset(value: int) -> float:
    """Sensor offset element (0#9): a signed byte in tenths of a degree."""
    signed = value - 256 if value > 127 else value
    return signed / 10


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
    # The vendor app re-reads such replies; treat them as no reading at all.
    if (current is not None and current > _MAX_PLAUSIBLE_ROOM) or (
        target is not None and target > _MAX_PLAUSIBLE_TARGET
    ):
        _log.debug("implausible status reply ignored: %r", resp)
        return None, None
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
