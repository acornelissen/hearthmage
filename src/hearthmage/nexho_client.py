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
from hearthmage.energy import DAILY_BUCKETS, MONTHLY_BUCKETS_MAX, parse_energy
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
# A query that reaches the hub straight after its last reply is answered with
# that reply's value: four settings reads sent with no pause came back shifted
# by one, and the poll that followed got a mixed reply (live 2026-11-11). With a
# 50 ms pause, 24 rounds out of 24 were right; this leaves double that.
_HUB_QUIET_GAP = 0.1
# A radiator setting is believed only when two reads in a row agree, so a reply
# that belongs to another query cannot reach the settings form or a write.
_ELEMENT_READS = 3
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
# A setpoint the hub does not acknowledge (radiator asleep or switched off) is
# sent again after this long, doubling up to the cap, until it is acknowledged
# or replaced by a newer target. A radiator that starts answering polls again
# after a silence gets it at once.
_SETPOINT_RETRY_FIRST = 30.0
_SETPOINT_RETRY_MAX = 900.0
# ...but not for ever: a radiator switched off at the wall must not be sent an
# old target when it is powered on again days later. This long after the latest
# request the setpoint is dropped and the card says it was not delivered.
_SETPOINT_GIVE_UP_AFTER = 2 * 3600.0
# Heater counts (OPS3) are re-read this often even when the zone list has not
# changed: a zone deleted in the vendor app keeps its OPS2 slot and only shows
# up as zero heaters. The hub answers OPS3 itself, with no radio involved.
_HEATERS_REFRESH = 600.0

_MAX_PLAUSIBLE_ROOM = 60.0  # vendor app: a room reading above this is re-read
_MAX_PLAUSIBLE_TARGET = 35.0  # ...and a setpoint above this
# Heaters per zone accepted from OPS3. The protocol doc gives no maximum (live:
# one per zone); 16 is a generous cap (inferred). A sensor-offset save writes
# once per heater, so an unbounded count would be that many writes.
_MAX_HEATERS = 16
# Fields in an *?F status reply, the last one the firmware string (``v...``).
# Live on all four radiators 2026-11-01; other firmware may differ (inferred).
_STATUS_F_FIELDS = 21
# A zone whose status replies keep being rejected for their shape is logged at
# most this often, so it does not look like a radiator that is simply asleep.
_REJECTED_STATUS_LOG_EVERY = 600.0
_MIN_TEMP = 5.0
_MAX_TEMP = 30.0
# "Off" is sent as a 1 degree setpoint: *T0 did not reliably turn radiators off.
_OFF_TEMP = 1.0


class _Withdrawn(Exception):
    """A command stopped before it went out again, because it is no longer wanted."""


class ClientStopped(HearthError):
    """The client was stopped (the app swapped it for a new one), so it sends
    nothing more. Not a hub failure: the same request can go to the new client."""


class _Wire:
    """One hub's wire, shared by every client of that hub address. The app
    swaps clients (a hub settings save, a backup import) while the old one may
    be mid-exchange; sharing the lock, the write priority and the time of the
    last reply keeps one conversation with the hub at a time across the swap,
    quiet gap included. It also passes setpoints a stopped client was still
    sending on to the hub's next client."""

    def __init__(self) -> None:
        self.lock = threading.Lock()  # held for a whole exchange
        self.cond = threading.Condition()  # guards writes_pending
        self.writes_pending = 0
        self.replied_at: float | None = None  # guarded by lock
        self.clients = threading.Lock()  # guards owner and handed_over
        self.owner: NexhoLocalClient | None = None  # the started, not stopped, client
        # zone -> (target, requested_at, unconfirmed) from a stopped client
        self.handed_over: dict[int, tuple[float, float, bool]] = {}

    # Lock order: lock (an exchange) -> clients -> a client's _lock. Nothing
    # here takes ``lock``; a client never takes ``clients`` under its _lock.

    def take_over(self, client: NexhoLocalClient) -> None:
        """``client`` starts: it is the owner now, and gets what was handed over."""
        with self.clients:
            self.owner = client
            waiting, self.handed_over = self.handed_over, {}
            self._park(client._adopt_setpoints(waiting))

    def hand_over(self, client: NexhoLocalClient, waiting: dict[int, tuple[float, float, bool]]) -> None:
        """``client`` has stopped: its waiting setpoints go to the running
        client, or wait for the next one to start. An owner that is itself
        stopping takes none (they would die with it): they wait too."""
        with self.clients:
            # An ack that landed after stop()'s snapshot found nothing here to
            # drop (_Wire.confirmed): drop it now. Any later ack still waits
            # for ``clients`` and drops what is parked or adopted below.
            with client._lock:
                waiting = {
                    zone: entry for zone, entry in waiting.items()
                    if client._acked.get(zone) != entry[:2]
                }
            if self.owner is client:
                self.owner = None
            if self.owner is not None:
                waiting = self.owner._adopt_setpoints(waiting)  # what it could not take
            self._park(waiting)

    def confirmed(self, zone: int, target: float, requested_at: float | None) -> None:
        """A stopped client's setpoint was acknowledged after it handed it
        over. Called with ``lock`` held, so before anyone can send it again:
        it is dropped from the parked ones, or settled in the owner."""
        with self.clients:
            held = self.handed_over.get(zone)
            if held is not None and held[:2] == (target, requested_at):
                del self.handed_over[zone]
            if self.owner is not None:
                self.owner._settle_carried(zone, target, requested_at)

    def _park(self, waiting: dict[int, tuple[float, float, bool]]) -> None:
        """Keep setpoints for the next client; called with ``clients`` held."""
        for zone, entry in waiting.items():
            held = self.handed_over.get(zone)
            if held is None or held[1] < entry[1]:  # the later request wins
                self.handed_over[zone] = entry


_wires: dict[tuple[str, int], _Wire] = {}
_wires_lock = threading.Lock()


def _wire_for(addr: tuple[str, int]) -> _Wire:
    with _wires_lock:
        wire = _wires.get(addr)
        if wire is None:
            wire = _wires[addr] = _Wire()
        return wire


def _settle(wire: _Wire) -> None:
    """Wait out the quiet gap after the hub's last reply (see
    ``_HUB_QUIET_GAP``), whichever client it replied to. Called with
    ``wire.lock`` held."""
    replied_at = wire.replied_at
    if replied_at is None:
        return
    wait = replied_at + _HUB_QUIET_GAP - time.monotonic()
    if wait > 0:
        time.sleep(wait)


class WireBusy(Exception):
    """The hub's wire stayed taken for the whole wait. ``zone_list_at`` is when
    the hub last answered the running client's zone list (monotonic), or None
    if no client is running or it never has."""

    def __init__(self, zone_list_at: float | None) -> None:
        super().__init__("the hub's wire is busy")
        self.zone_list_at = zone_list_at


def _busy(wire: _Wire) -> WireBusy:
    with wire.clients:
        owner = wire.owner
    # A float read: a hint, no client lock needed.
    return WireBusy(owner._last_ops2_ok if owner is not None else None)


def exchange_on_wire(addr: tuple[str, int], exchange, wait: float, create: bool = False):
    """Run ``exchange()`` (one query to the hub at ``addr``, returning its
    reply or None) as a read on that hub's wire: after any pending write, with
    the wire held, the quiet gap waited out first and its clock restarted
    after, so neither side's next query gets the other's reply. The clock
    restarts after a timeout too: the hub may still answer late. (``_ask`` does
    not do that yet; see bead heatapp-local-41n.) For use from outside a client
    (discovery).

    With no wire for ``addr`` (no client in this process has talked to it) it
    just runs, unless ``create``: then the wire is made first, so a client
    built for that hub meanwhile waits for this exchange. Raises WireBusy if
    the wire is not free within ``wait`` seconds."""
    with _wires_lock:
        wire = _wires.get(addr)
    if wire is None:
        if not create:
            return exchange()
        wire = _wire_for(addr)
    deadline = time.monotonic() + wait
    with wire.cond:  # reads yield to writes, as in _wire_acquire
        while wire.writes_pending > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _busy(wire)
            wire.cond.wait(remaining)
    if not wire.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise _busy(wire)
    try:
        _settle(wire)
        return exchange()
    finally:
        wire.replied_at = time.monotonic()  # answered or not (see above)
        wire.lock.release()


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
        on_zones=None,
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
        # Optional callback([(zone id, name), ...]) fired by the poller when the
        # accepted zone list first appears or changes (MQTT discovery).
        self._on_zones = on_zones
        self._announced: list[int] | None = None  # the zone list last announced
        self._next_energy = 0.0  # monotonic; 0 = due on the first poll cycle
        self._energy_queue: list[int] = []
        self._lock = threading.Lock()
        self._state: dict[int, Room] = {}  # zone -> latest Room served to the UI
        self._last: dict[int, tuple[float | None, float | None]] = {}  # last good temps
        self._pending: dict[int, float] = {}  # zone -> target while a write is in flight
        self._desired: dict[int, int] = {}  # zone -> latest requested target (writes coalesce)
        self._writers: set[int] = set()  # zones with a live single-flight write worker
        self._write_seq: dict[int, int] = {}  # zone -> bumped on every requested write
        self._unconfirmed: set[int] = set()  # zones whose target the hub has not acked yet
        self._requested_at: dict[int, float] = {}  # zone -> when its latest target was asked for
        self._gave_up: dict[int, float] = {}  # zone -> target dropped after _SETPOINT_GIVE_UP_AFTER
        self._write_kick: set[int] = set()  # zones whose waiting retry should go now
        # zone -> (target, requested_at) the hub last acked, noted with the wire
        # held so stop() never hands over a setpoint that has already landed.
        self._acked: dict[int, tuple[float, float | None]] = {}
        # zone -> when a newer command dropped its waiting setpoint (see
        # cancel_waiting_setpoint); a carried-over older setpoint is not sent.
        self._cancelled_at: dict[int, float] = {}
        # Wakes write workers that are waiting to retry (new target, kick, stop).
        self._write_wake = threading.Condition()
        self._zones: list[int] | None = None  # the accepted zone list
        self._zones_seen: list[int] | None = None  # the hub's previous answer, accepted or not
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
        # I/O is serialised through the hub's wire, shared with any other
        # client of the same hub (see _Wire). Writes take priority - reads yield
        # while any write is pending so a user setpoint never waits behind the
        # poller (at most behind the single read already in flight).
        self._wire = _wire_for(self._addr)
        # Per-zone poll scheduling (monotonic clock). Unreachable zones back off.
        self._next_poll: dict[int, float] = {}
        self._backoff: dict[int, float] = {}
        # Replies rejected for their shape, logged so they can be captured
        # without flooding the log: key -> last reply logged (once per distinct
        # reply), and zone -> when a rejected status reply was last logged.
        self._rejected_logged: dict[tuple, str] = {}
        self._status_rejected_at: dict[int, float] = {}
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
        wire = self._wire
        if priority:
            with wire.cond:
                wire.writes_pending += 1
        else:
            with wire.cond:
                while wire.writes_pending > 0:
                    wire.cond.wait()
        wire.lock.acquire()

    def _wire_release(self, priority: bool) -> None:
        wire = self._wire
        wire.lock.release()
        if priority:
            with wire.cond:
                wire.writes_pending -= 1
                if wire.writes_pending == 0:
                    wire.cond.notify_all()

    def _ask(
        self, cmd: str, retries: int, timeout: float, priority: bool = False, still_wanted=None,
        on_reply=None,
    ) -> str:
        """Send ``cmd`` and return the hub's reply. ``still_wanted``, if given, is
        asked with the wire held before every send; once it says no, nothing more
        goes out and _Withdrawn is raised. Checking with the wire held matters: a
        command that withdraws this one and then takes the wire is never followed
        by it. ``on_reply``, if given, is called with the reply while the wire
        is still held. A stopped client sends nothing (ClientStopped): its
        successor may be talking to the hub."""
        self._wire_acquire(priority)
        try:
            last_exc: Exception | None = None
            for _ in range(retries):
                sock = None
                self._let_hub_settle()
                if self._stop.is_set():
                    raise ClientStopped("The hub connection was restarted. Try again.")
                if still_wanted is not None and not still_wanted():  # last check before sending
                    raise _Withdrawn(cmd)
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
                    self._wire.replied_at = time.monotonic()
                    reply = data.rstrip(_NULL).decode("ascii", errors="replace")
                    _log.debug("hub %s -> %s (%.0fms)", cmd, reply, (time.monotonic() - started) * 1000)
                    if on_reply is not None:
                        on_reply(reply)
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

    def _let_hub_settle(self) -> None:
        """Wait out the quiet gap after the hub's last reply (see
        ``_HUB_QUIET_GAP``), whichever client it replied to. Called with the
        wire held."""
        _settle(self._wire)

    def _ask_until_answered(
        self, cmd: str, ok_prefix: str, priority: bool,
        retries: int | None = None, timeout: float | None = None, still_wanted=None,
        on_reply=None,
    ) -> str:
        """Send ``cmd``, resending after a short gap while the hub answers ER
        (radiator missed the RF window). The wire is released between attempts
        so other traffic is not blocked. Returns the last reply. See ``_ask``
        for ``still_wanted`` and ``on_reply``."""
        default = (_WRITE_RETRIES, _WRITE_TIMEOUT) if priority else (_READ_RETRIES, _READ_TIMEOUT)
        retries = retries if retries is not None else default[0]
        timeout = timeout if timeout is not None else default[1]
        resp = ""
        for attempt in range(_ER_ATTEMPTS):
            resp = self._ask(
                cmd, retries, timeout, priority=priority, still_wanted=still_wanted, on_reply=on_reply
            )
            if resp.startswith(ok_prefix) or not resp.startswith("ER"):
                return resp
            if still_wanted is not None and not still_wanted():
                raise _Withdrawn(cmd)  # nothing more to send, so no gap to wait out
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
        visible = [z for z in ids if self._heaters.get(z, 1) > 0]
        # A changed list is only believed once the hub has said it twice in a
        # row: a hub that is rebooting can answer with a short or empty list,
        # and acting on one such answer would hide zones and throw away their
        # readings and any setpoint waiting to be sent. The very first answer
        # is taken as it is (there is nothing to compare it with).
        with self._lock:
            confirmed = visible == self._zones_seen
            self._zones_seen = visible
            if self._zones is None or confirmed:
                self._zones = visible
            accepted = self._zones
        if visible != accepted:
            self._heaters_read_at = 0.0  # disputed: read the heater counts again next poll
        if confirmed:
            self._forget_zones_except(visible)
        return accepted

    def _forget_zones_except(self, ids: list[int]) -> None:
        """The hub has confirmed its zones: drop everything held for any other
        zone. Without this a zone that was deleted, or that came from the
        readings cache of another hub, stays on screen as "ok" with its last
        reading for ever. Never called on a hub failure or a single answer."""
        keep = set(ids)
        with self._lock:
            for table in (self._state, self._last, self._pending, self._desired,
                          self._next_poll, self._backoff, self._last_fresh, self._write_seq,
                          self._requested_at, self._gave_up, self._status_rejected_at,
                          self._acked, self._cancelled_at):
                for zone in [z for z in table if z not in keep]:
                    del table[zone]
            for key in [k for k in self._rejected_logged if isinstance(k[0], int) and k[0] not in keep]:
                del self._rejected_logged[key]  # a zone's energy rejections
            self._unconfirmed &= keep
            self._write_kick &= keep
        with self._write_wake:
            self._write_wake.notify_all()  # a worker retrying for a dropped zone ends

    def _read_heater_counts(self, ids: list[int]) -> None:
        """Read OPS3/ (heaters per zone, in OPS2 order). On failure, or a reply
        with a count that is not 0.._MAX_HEATERS, the counts stay unknown and
        writes fall back to one heater; nothing is hidden."""
        try:
            resp = self._ask("OPS3/", _READ_RETRIES, _READ_TIMEOUT)
        except HearthError:
            return
        if not resp.startswith("OPOK,OPS3,"):
            return
        # Only the listed zones' counts: like OPS2, the reply may carry slots
        # after them. An empty field is unknown for that zone (one heater).
        tokens = resp.split(",")[2:][: len(ids)]
        counts = [None if tok == "" else _digits(tok) for tok in tokens]
        if any(tok != "" and (c is None or c > _MAX_HEATERS) for tok, c in zip(tokens, counts)):
            self._log_rejected(("OPS3",), "heater counts (OPS3)", resp)
            return
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
            error=error or self._undelivered_message(zone),
            silent_for=self._silent_for(zone),
            unconfirmed=zone in self._unconfirmed,
        )

    def _undelivered_message(self, zone: int) -> str | None:
        """What to tell the user about a setpoint that was dropped after going
        unacknowledged for too long, or None. Shown until the radiator answers
        again or a new target is set."""
        target = self._gave_up.get(zone)
        if target is None:
            return None
        what = "Switching off" if target <= _OFF_TEMP else f"The change to {target:.1f} °C"
        hours = round(_SETPOINT_GIVE_UP_AFTER / 3600)
        waited = f"for {hours} hours" if hours >= 1 else "in time"
        return (
            f"{what} was not delivered: the radiator did not answer {waited}. "
            "Set it again once it is back."
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
        if current is None and resp.startswith(("OK,", "OPOK,")):
            self._log_rejected_status(zone, resp)
        return current, target, current is not None or target is not None

    def _log_rejected_status(self, zone: int, resp: str) -> None:
        """The hub answered, but not with a status reply: say so (unlike a
        radiator asleep over RF), at most every _REJECTED_STATUS_LOG_EVERY."""
        now = time.monotonic()
        last = self._status_rejected_at.get(zone)
        if last is not None and now - last < _REJECTED_STATUS_LOG_EVERY:
            return
        self._status_rejected_at[zone] = now
        _log.warning("zone %s status reply of an unexpected shape ignored: %r", zone, resp)

    def _log_rejected(self, key: tuple, what: str, resp: str) -> None:
        """Log a reply rejected for its shape, once per distinct reply per key."""
        if self._rejected_logged.get(key) == resp:
            return
        self._rejected_logged[key] = resp
        _log.warning("%s reply of an unexpected shape ignored: %r", what, resp)

    # ---- background poller ----------------------------------------------

    def start(self) -> None:
        if self._poller_started:
            return
        self._poller_started = True
        self._started_at = time.monotonic()
        self._wire.take_over(self)  # setpoints a stopped client of this hub left
        self._thread = threading.Thread(target=self._poll_loop, name="nexho-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop this client for good (the app swapped to a new one). Nothing
        more goes on the wire: an exchange already on the air finishes, the
        poller ends before its next zone and write workers end. Setpoints still
        waiting for the hub go to this hub's next client, which sends them."""
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()  # under _lock: set_temperature and _note_ack check it there
            now = time.monotonic()
            waiting = {}
            for zone, target in self._desired.items():
                asked = self._requested_at.get(zone, now)
                if self._acked.get(zone) == (target, asked):
                    continue  # acked; the worker just has not recorded it yet
                waiting[zone] = (target, asked, zone in self._unconfirmed)
        with self._write_wake:
            self._write_wake.notify_all()  # end any write worker waiting to retry
        self._wire.hand_over(self, waiting)

    def stopped(self) -> bool:
        """True once stop() has run: this client sends nothing more."""
        return self._stop.is_set()

    def _adopt_setpoints(
        self, waiting: dict[int, tuple[float, float, bool]]
    ) -> dict[int, tuple[float, float, bool]]:
        """Send setpoints a stopped client of this hub had not got acknowledged.
        The swap is not the user's doing, so their target is kept, with its
        give-up clock and its "Not confirmed" mark. One past the give-up time,
        or older than this client's own request or cancel for the zone, is
        dropped. Returns those not taken because this client has stopped too."""
        now = time.monotonic()
        refused = {}
        for zone, entry in waiting.items():
            target, requested_at, unconfirmed = entry
            if now - requested_at >= _SETPOINT_GIVE_UP_AFTER:
                continue
            with self._lock:
                if self._stop.is_set():
                    refused[zone] = entry
                    continue
                newer = max(self._requested_at.get(zone, -math.inf),
                            self._cancelled_at.get(zone, -math.inf))
                if newer >= requested_at:
                    continue
                if unconfirmed:
                    self._unconfirmed.add(zone)
                start = self._want_setpoint(zone, target, requested_at)
            self._run_writer(zone, start)
            _log.info("zone %s setpoint %s carried over from the previous hub client", zone, target)
        return refused

    def _settle_carried(self, zone: int, target: float, requested_at: float | None) -> None:
        """The previous client's ack for a setpoint this one carried over came
        in: it has landed, so this client has nothing to send for it."""
        with self._lock:
            if self._desired.get(zone) != target or self._requested_at.get(zone) != requested_at:
                return  # replaced since: the newer target still goes out
            self._record_ack(zone, target)
        with self._write_wake:
            self._write_wake.notify_all()  # the worker sees nothing left and ends

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 - a bad poll must not kill the poller
                if self._stop.is_set():
                    break  # stopped mid-poll: its next query was refused
                _log.exception("poll cycle failed; poller continues")
            try:
                wait = self._sleep_until_due()
            except Exception:  # noqa: BLE001 - nor may working out the next one
                _log.exception("poll scheduling failed; poller continues")
                wait = self._poll_interval
            self._stop.wait(wait)

    def _sleep_until_due(self) -> float:
        """Sleep until the soonest zone is due, so the hub sits idle in between
        and a user write meets a free hub. Capped at the poll interval."""
        with self._lock:
            soonest = min(self._next_poll.values(), default=None)
        if soonest is None:
            return 1.0
        return max(1.0, min(self._poll_interval, soonest - time.monotonic()))

    def _poll_once(self) -> None:
        zones = self._zone_ids()
        now = time.monotonic()
        announce = None
        with self._lock:
            for zone in zones:
                self._state.setdefault(zone, self._room(zone, None, None, "loading"))
            if self._on_zones is not None and zones != self._announced:
                self._announced = list(zones)
                announce = [(str(z), self._state[z].name) for z in zones]
        if announce is not None and not self._stop.is_set():
            try:
                self._on_zones(announce)  # after seeding, so list_rooms agrees
            except Exception:  # noqa: BLE001 - announcing must not break polling
                _log.exception("on_zones hook failed")
        # Poll sequentially - the hub serialises anyway, and concurrent reads
        # only pile contention onto it. Zones that are backing off are skipped so
        # an asleep radiator does not keep blocking the hub every cycle.
        for zone in zones:
            if self._stop.is_set():
                return  # swapped out: the next client has the hub now
            with self._lock:
                due = self._next_poll.get(zone, 0.0)
            if due > now:
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
        with self._lock:
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
            was_silent = self._backoff.get(zone, 0.0) > self._poll_interval  # backing off
        current, target, fresh = self._read_zone_raw(zone)
        if fresh:
            _log.debug("zone %s fresh: current=%s target=%s", zone, current, target)
        else:
            _log.debug("zone %s did not answer (RF asleep)", zone)
        reported = target  # the target handed to the reading hook, see below
        settled_by_read = False
        with self._lock:
            if fresh:
                self._last_fresh[zone] = time.monotonic()
                self._gave_up.pop(zone, None)  # it is back: the card shows the truth again
            pending = self._pending.get(zone)
            undisturbed = (
                not writing_before
                and zone not in self._writers
                and self._write_seq.get(zone, 0) == seq_before
            )
            if fresh and pending is not None and target is not None and target == pending:
                # The radiator already holds the target that was asked for: the
                # write landed even if its ack was lost. That confirms it, and
                # the worker (if any) has nothing left to send.
                self._last[zone] = (current, target)
                self._pending.pop(zone, None)
                self._unconfirmed.discard(zone)
                self._write_kick.discard(zone)
                if self._desired.get(zone) == pending:
                    self._desired.pop(zone, None)
                self._state[zone] = self._room(zone, current, target, "ok")
                settled_by_read = True
            elif fresh and undisturbed:
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
                    if zone not in self._unconfirmed:
                        reported = pending  # first send still in flight: about to land
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
                reported = kept
            else:
                last = self._last.get(zone)
                if last is not None:
                    self._state[zone] = self._room(zone, last[0], last[1], "ok")
                else:
                    self._state[zone] = self._room(zone, None, None, "unreachable")
        if settled_by_read:
            with self._write_wake:
                self._write_wake.notify_all()  # a worker waiting to retry can stop
        elif fresh and was_silent:
            self._kick_write_retry(zone)
        if fresh and self._on_reading is not None:
            # The hook feeds history, the waste detector and MQTT state. It runs
            # after the read is reconciled so it is told the target the radiator
            # holds: the raw one normally (and while a setpoint is unconfirmed),
            # but the newer one when a write overlapped this read.
            try:
                self._on_reading(zone, current, reported)
            except Exception:  # noqa: BLE001 - history logging must not break polling
                _log.exception("on_reading hook failed for zone %s", zone)
        return fresh

    # ---- HearthClient interface (non-blocking; serves the cache) --------

    def list_rooms(self) -> list[Room]:
        """The rooms as the poller last left them. Never talks to the hub: page
        requests and htmx polls call this. On a cold start with no readings
        cache it is empty until the poller's first zone list (``connecting``)."""
        with self._lock:
            return [self._state[zone] for zone in sorted(self._state)]

    def connecting(self) -> bool:
        """True until the hub has answered a zone list, so an empty room list
        means "not known yet" rather than "the hub has no rooms"."""
        with self._lock:
            return self._zones is None

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
                    "unconfirmed": z in self._unconfirmed,
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

    def cancel_waiting_setpoint(self, room_id: str) -> None:
        """Drop a setpoint that is still waiting for the radiator, because a
        newer command for the zone (a holiday, the weekly program switched on)
        is about to be sent. Otherwise the old setpoint would be re-sent after
        the new command and override it, e.g. heat the room for a whole absence.
        This holds for a first send still in flight too: the hub may be resending
        it on ER, and _send_once sends nothing more once it is dropped here. A
        datagram already on the air is answered before the newer command goes out
        (one conversation on the wire at a time)."""
        zone = _zone_int(room_id)
        with self._lock:
            self._cancelled_at[zone] = time.monotonic()  # nor one carried over later
            if zone not in self._desired:
                return  # no setpoint waiting to be sent or re-sent
            target = self._desired.pop(zone, None)
            self._pending.pop(zone, None)
            self._unconfirmed.discard(zone)
            self._write_kick.discard(zone)
            last = self._last.get(zone)
            prev = self._state.get(zone)
            current = prev.current_temp if prev else (last[0] if last else None)
            self._state[zone] = self._room(zone, current, last[1] if last else None, "ok")
        _log.info("zone %s unconfirmed setpoint %s dropped: replaced by a newer command", zone, target)
        with self._write_wake:
            self._write_wake.notify_all()  # its worker sees nothing left to send and ends

    def _kick_write_retry(self, zone: int) -> None:
        """The radiator has just come back after not answering polls, so it is
        awake now: if a setpoint is waiting to be retried for it, send it now
        rather than later. Only called on that silent-to-answering change; a
        radiator that answers every poll but refuses the setpoint is left to
        the retry backoff, or it would get a burst of writes on every poll."""
        with self._lock:
            if zone not in self._unconfirmed:
                return
            self._write_kick.add(zone)
        with self._write_wake:
            self._write_wake.notify_all()

    def set_temperature(self, room_id: str, temperature: float) -> None:
        """Optimistic write: reflect the new setpoint instantly, send in the
        background. Writes to a zone are coalesced through a single-flight worker
        so rapid taps converge on the last requested target instead of racing -
        at most one datagram is in flight per zone, and only the final value is
        confirmed. ``_last`` (the confirmed cache) is updated only on ack. A
        target the hub does not acknowledge is shown as not confirmed and
        re-sent until it is, or until a newer target replaces it.
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
            if self._stop.is_set():  # swapped out: refuse rather than lose it
                raise HearthError("The hub connection was restarted. Set the target again.")
            start = self._want_setpoint(zone, target, time.monotonic())  # give-up clock starts
        self._run_writer(zone, start)

    def _want_setpoint(self, zone: int, target: float, requested_at: float) -> bool:
        """Make ``target`` the zone's wanted setpoint and show it. Called with
        _lock held. True if no worker is running and the caller must start one."""
        prev = self._state.get(zone)
        current = prev.current_temp if prev else None
        self._gave_up.pop(zone, None)  # a new request replaces an undelivered one
        self._requested_at[zone] = requested_at  # the give-up clock
        self._pending[zone] = target  # poll must not revert this
        self._desired[zone] = target  # latest wins
        self._write_seq[zone] = self._write_seq.get(zone, 0) + 1  # see _poll_zone
        self._state[zone] = self._room(zone, current, target, "ok")
        if zone in self._writers:
            # The running worker will pick up the new desired target...
            self._write_kick.add(zone)  # ...now, even if it is the same target again
            return False
        self._writers.add(zone)
        return True

    def _run_writer(self, zone: int, start: bool) -> None:
        """Start the zone's write worker, or wake the running one."""
        if not start:
            with self._write_wake:
                self._write_wake.notify_all()  # it may be waiting to retry the old one
            return
        threading.Thread(
            target=self._drain_writes, args=(zone,), name="nexho-write", daemon=True
        ).start()

    def _drain_writes(self, zone: int) -> None:
        """Single-flight write worker: send the desired target, and if a newer
        one arrived while sending, send that instead - repeat until the hub has
        acknowledged the latest one. A target the hub does not acknowledge
        (radiator asleep or switched off) is kept, marked not confirmed, and
        sent again after a growing wait. It is never dropped silently: after
        _SETPOINT_GIVE_UP_AFTER it is dropped with a message on the card."""
        failures = 0
        # True while this worker is the zone's registered writer. Once it has
        # discarded itself (under _lock) a new worker may register, and the
        # finally below must not unregister that one.
        registered = True
        try:
            while not self._stop.is_set():
                with self._lock:
                    target = self._desired.get(zone)
                    if target is None:
                        self._writers.discard(zone)  # atomic with the empty check
                        registered = False
                        return
                    self._write_kick.discard(zone)
                ok = self._send_once(zone, target)
                if self._stop.is_set():
                    return  # swapped out: the hub's next client has this setpoint
                with self._lock:
                    if ok is None or self._desired.get(zone) != target:
                        # Superseded by a newer tap, or withdrawn and then asked
                        # for again: a new request, so loop to send the latest.
                        failures = 0
                        continue
                    prev = self._state.get(zone)
                    current = prev.current_temp if prev else None
                    if ok:
                        self._record_ack(zone, target)
                        self._writers.discard(zone)
                        registered = False
                        return
                    # No ack. Give up if it has been too long since the user
                    # asked: the card goes back to the last confirmed target and
                    # says the change was not delivered.
                    left = _SETPOINT_GIVE_UP_AFTER - (
                        time.monotonic() - self._requested_at.get(zone, time.monotonic())
                    )
                    if left <= 0:
                        self._desired.pop(zone, None)
                        self._pending.pop(zone, None)
                        self._unconfirmed.discard(zone)
                        self._write_kick.discard(zone)
                        self._gave_up[zone] = float(target)
                        last = self._last.get(zone)
                        self._state[zone] = self._room(
                            zone, last[0] if last else current, last[1] if last else None, "ok"
                        )
                        self._writers.discard(zone)
                        registered = False
                        _log.warning(
                            "zone %s setpoint %s given up: not acknowledged for %.0f min",
                            zone, target, _SETPOINT_GIVE_UP_AFTER / 60,
                        )
                        return
                    # Otherwise stay the zone's writer so the poller keeps the
                    # user's target (see _poll_zone), and say it is not confirmed.
                    failures += 1
                    self._unconfirmed.add(zone)
                    self._state[zone] = self._room(zone, current, float(target), "ok")
                wait = min(_setpoint_retry_wait(failures), left)  # one last try at the limit
                _log.info("zone %s setpoint %s not confirmed; retrying in %.0fs", zone, target, wait)
                self._wait_to_retry(zone, target, wait)
        finally:
            # Safety net: never leave the zone marked as having a live worker if
            # the loop raised unexpectedly, or no future write to it would ever
            # be sent (set_temperature would assume a worker is running).
            with self._lock:
                if registered:
                    self._writers.discard(zone)

    def _record_ack(self, zone: int, target: float) -> None:
        """The hub acknowledged ``target``: it is the confirmed one now and
        nothing is left to send. Called with _lock held."""
        prev = self._state.get(zone)
        current = prev.current_temp if prev else None
        self._desired.pop(zone, None)
        self._unconfirmed.discard(zone)
        self._write_kick.discard(zone)
        self._last[zone] = (current, float(target))
        self._pending.pop(zone, None)  # confirmed - release poll guard
        self._state[zone] = self._room(zone, current, float(target), "ok")

    def _note_ack(self, zone: int, target: float, reply: str) -> None:
        """Called with the wire held when a setpoint reply comes in. An ack is
        noted at once, so a stop() that follows does not hand the setpoint on;
        one that arrives after stop() tells the wire, so the next client does
        not send it again (see _Wire.confirmed)."""
        if reply != "OK":
            return
        with self._lock:
            if self._desired.get(zone) != target:
                return  # replaced while on the air: the newer one still goes out
            asked = self._requested_at.get(zone)
            self._acked[zone] = (target, asked)
            stopped = self._stop.is_set()
        if stopped:
            self._wire.confirmed(zone, target, asked)

    def _wait_to_retry(self, zone: int, target: float, wait: float) -> None:
        """Sleep before re-sending ``target``, but wake at once if a newer
        target arrives, the radiator answers a poll, the zone is dropped, or the
        client is stopped."""

        def wake() -> bool:
            return (
                self._stop.is_set()
                or self._desired.get(zone) != target
                or zone in self._write_kick
            )

        with self._write_wake:
            self._write_wake.wait_for(wake, timeout=wait)

    def _send_once(self, zone: int, target: float) -> bool | None:
        """Send one setpoint command; True if the hub acknowledged. ``target`` is
        the display temperature (_OFF_TEMP = off); it is encoded to the wire value here.
        Resending the same target is idempotent - the radiator ends at that value.
        Nothing more is sent once ``target`` is no longer the zone's desired one
        (a newer target, or cancel_waiting_setpoint); that returns None, which
        is not a refusal by the hub."""
        wire = _encode_setpoint(target)

        def still_wanted() -> bool:
            with self._lock:
                return self._desired.get(zone) == target

        try:
            resp = self._ask_until_answered(
                f"D#{zone}#{self._modules(zone)}#0#0*T{wire}/", "OK", priority=True,
                still_wanted=still_wanted, on_reply=lambda reply: self._note_ack(zone, target, reply),
            )
            acked = resp == "OK"  # the vendor treats "OK,<...>" as a command error
            if acked:
                _log.info("zone %s setpoint %s acknowledged", zone, target)
            else:
                _log.warning("zone %s setpoint %s not acknowledged: %s", zone, target, resp)
            return acked
        except _Withdrawn:
            _log.info("zone %s setpoint %s not sent again: replaced", zone, target)
            return None
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
        parts = resp.split(",")  # exactly OK,<byte>: anything else is another reply
        if len(parts) != 2 or parts[0] != "OK":
            return None
        value = _digits(parts[1])
        return value if value is not None and value <= 255 else None

    def _read_element_agreed(self, zone: int, group: int, index: int) -> int | None:
        """Read one setting until two reads in a row agree; None if the radiator
        does not answer or the answers keep differing. The reply carries nothing
        that ties it to its query, so agreement is the only check there is."""
        previous = self._read_element(zone, group, index)
        for _ in range(_ELEMENT_READS - 1):
            if previous is None:
                return None
            value = self._read_element(zone, group, index)
            if value == previous:
                return value
            previous = value
        _log.warning("zone %s setting %s#%s: reads disagree, treating as unread", zone, group, index)
        return None

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
        if self._read_element(zone, *self._EL_KEYPAD) is None:
            return None  # first read failed: radiator asleep, don't bother more
        keypad = self._read_element_agreed(zone, *self._EL_KEYPAD)
        window = self._read_element_agreed(zone, *self._EL_WINDOW)
        frost = self._read_element_agreed(zone, *self._EL_FROST)
        offset = self._read_element_agreed(zone, *self._EL_OFFSET)
        return {
            "keypad_lock": bool(keypad & 1) if keypad is not None else None,
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
            current = self._read_element_agreed(zone, *self._EL_KEYPAD)
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
        self.cancel_waiting_setpoint(room_id)
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
        monthly_watts, monthly = parse_energy(um)
        # A payload that does not fit is logged so it can be captured: the
        # framing is inferred and no radiator has answered these reads yet.
        if watts is None or len(daily) != DAILY_BUCKETS:
            if ud.startswith(("OK,", "OPOK,")):
                self._log_rejected((zone, "UD"), f"zone {zone} daily energy (*?UD)", ud)
            return None
        if monthly_watts != watts or len(monthly) > MONTHLY_BUCKETS_MAX:
            if um.startswith(("OK,", "OPOK,")):
                self._log_rejected((zone, "UM"), f"zone {zone} monthly energy (*?UM)", um)
            monthly = []  # not this radiator's monthly reply (or none)
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
        fields = resp.split(",")
        if fields[0] != "OK" or len(fields) != _STATUS_F_FIELDS or not fields[-1].startswith("v"):
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
        if parts[0] != "OPOK" or len(parts) != 2 or parts[1] not in ("0", "1"):
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


def _setpoint_retry_wait(failures: int) -> float:
    """Seconds to wait before re-sending a setpoint after ``failures`` tries in
    a row went unacknowledged: 30 s, doubling, capped at 15 minutes."""
    return min(_SETPOINT_RETRY_FIRST * 2 ** (failures - 1), _SETPOINT_RETRY_MAX)


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
    radiator was unreachable over RF, so both values are unknown. So is any
    reply not of exactly that shape (three plain numbers, tenths 0..9): the
    hub can answer with the reply meant for another query.
    """
    if not resp.startswith(("OK,", "OPOK,")):
        return None, None
    parts = resp.split(",")
    numbers = [_digits(p) for p in parts[1:]]
    if len(numbers) != 3 or None in numbers or numbers[1] > 9:
        return None, None  # logged by the caller, which knows the zone
    whole, tenths, setpoint = numbers
    current = whole + tenths / 10.0
    target = _decode_setpoint(setpoint)
    # The vendor app re-reads such replies; treat them as no reading at all.
    if current > _MAX_PLAUSIBLE_ROOM or target > _MAX_PLAUSIBLE_TARGET:
        return None, None
    return current, target


def _digits(value: str) -> int | None:
    """``value`` as a number if it is plain ASCII digits (no sign, no spaces)."""
    return int(value) if value.isascii() and value.isdigit() else None


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
