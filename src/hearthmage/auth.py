"""Sign-in sessions and the login throttle (used only when a password is set).

Sessions: one per signed-in device. The browser holds a random token; the
store keeps only its SHA-256, so a copy of the file signs nobody in. Each
session also carries a fingerprint of the password it was opened with: a
changed password (or secret key) no longer matches, and every session from
before the change counts as signed out. A session not used for 90 days
expires. ``last_seen`` is written at most every few minutes, not on every
request.

Shape on disk: ``{"version": 1, "sessions": {"<sha256 of token>": {"handle":
str, "fingerprint": str, "created": float, "last_seen": float, "user_agent":
str, "address": str}}}``. ``handle`` is a separate random id that names a
session in the devices list; it cannot be turned back into the token.

Throttle: in memory, so a restart forgets it. Per client address, five
failures are free and then each try waits 1 s, doubling to 15 min. Across all
addresses, more than 20 failures without a 10-minute pause slows every sign-in
the same way, so many addresses (or a proxy that shows one) cannot brute
force. A success clears only its own address and exempts it from the
global wait for 90 days, as do the addresses of sessions in the store. A
try refused during a wait changes nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import IPv6Address, ip_address, ip_network

from hearthmage.atomicio import write_json_atomic

IDLE_SECONDS = 90 * 86400  # a session unused this long is signed out
TOUCH_SECONDS = 300  # write last_seen at most this often
MAX_SESSIONS = 100
_UA_MAX = 200
_TOKEN_MAX = 128  # token_urlsafe(32) is 43 characters
_CLOCK_SKEW = 86400  # a last_seen further ahead than this counts as expired

FREE_FAILURES = 5
GLOBAL_FREE_FAILURES = 21  # "more than 20"
BASE_WAIT = 1.0
MAX_WAIT = 900.0
GLOBAL_QUIET = 600.0  # a 10-minute pause ends a streak across addresses
ADDRESS_QUIET = 86400.0  # an address is forgotten after a quiet day
MAX_TRACKED = 4096
KNOWN_SECONDS = IDLE_SECONDS  # a successful sign-in exempts its address this long
MAX_KNOWN = 256  # known addresses kept (about one per device)

_log = logging.getLogger("hearthmage.security")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@lru_cache(maxsize=4)
def password_fingerprint(password: str, key: str) -> str:
    """Which password (and secret key) a session was opened with. scrypt, so
    a leaked sessions file plus config is slow to brute force; cached, so it
    costs once per process. Never called with user input."""
    return hashlib.scrypt(
        password.encode("utf-8"), salt=key.encode("utf-8"), n=2**14, r=8, p=1, dklen=32
    ).hex()


@dataclass(frozen=True)
class Session:
    handle: str
    created: float
    last_seen: float
    user_agent: str
    address: str
    renewed: bool = False  # last_seen was just written: re-send the cookie


def _valid_record(record) -> bool:
    if not isinstance(record, dict):
        return False
    strings = ("handle", "fingerprint", "user_agent", "address")
    numbers = ("created", "last_seen")
    return all(isinstance(record.get(k), str) for k in strings) and all(
        isinstance(record.get(k), (int, float)) and not isinstance(record.get(k), bool)
        for k in numbers
    )


class SessionStore:
    """Server-side sessions in a JSON file next to the config. Thread-safe."""

    def __init__(
        self, path: str, clock=time.time, max_sessions: int = MAX_SESSIONS
    ) -> None:
        self._path = path
        self._clock = clock
        self._max = max_sessions
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    @property
    def path(self) -> str:
        return self._path

    def now(self) -> float:
        """The store's clock, so "last seen" is shown on the same time base."""
        return self._clock()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            return
        sessions = loaded.get("sessions") if isinstance(loaded, dict) else None
        if isinstance(sessions, dict):
            self._data = {k: v for k, v in sessions.items() if _valid_record(v)}

    def _commit(self, data: dict[str, dict]) -> None:
        """Write ``data`` and only then make it current, so a failed save
        leaves memory and disk in step. Callers hold ``_lock`` and never
        change ``self._data`` or its records in place.
        write_json_atomic creates the file 0600."""
        write_json_atomic(self._path, {"version": 1, "sessions": data})
        self._data = data

    def _commit_quietly(self, data: dict[str, dict]) -> bool:
        """Housekeeping (renewal, pruning) that must not fail a request."""
        try:
            self._commit(data)
            return True
        except OSError:
            _log.warning("could not save sessions to %r", self._path, exc_info=True)
            return False

    def _expired(self, record: dict, now: float) -> bool:
        # A last_seen more than a day ahead means the clock was wrong when it
        # was written; without this it would never expire.
        idle = now - record["last_seen"]
        return idle > IDLE_SECONDS or idle < -_CLOCK_SKEW

    def _live(self, now: float, fingerprint: str | None = None) -> dict[str, dict]:
        """A copy without expired sessions (and, given a fingerprint, without
        ones from another password)."""
        return {
            key: record for key, record in self._data.items()
            if not self._expired(record, now)
            and (fingerprint is None or _same(record["fingerprint"], fingerprint))
        }

    @staticmethod
    def _session(record: dict, renewed: bool = False) -> Session:
        return Session(
            handle=record["handle"],
            created=record["created"],
            last_seen=record["last_seen"],
            user_agent=record["user_agent"],
            address=record["address"],
            renewed=renewed,
        )

    def create(self, fingerprint: str, user_agent: str, address: str) -> str:
        """Open a session and return the token for the cookie."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            data = self._live(now)
            while len(data) >= self._max:
                del data[min(data, key=lambda k: data[k]["last_seen"])]
            data[hash_token(token)] = {
                "handle": secrets.token_urlsafe(12),
                "fingerprint": fingerprint,
                "created": now,
                "last_seen": now,
                "user_agent": (user_agent or "")[:_UA_MAX],
                "address": (address or "")[:64],
            }
            self._commit(data)
        return token

    def lookup(self, token: str, fingerprint: str) -> Session | None:
        """The live session for this token, or None. Renews it on use."""
        if not token or len(token) > _TOKEN_MAX:
            return None
        key = hash_token(token)
        now = self._clock()
        with self._lock:
            record = self._data.get(key)
            if record is None:
                return None
            if self._expired(record, now) or not _same(record["fingerprint"], fingerprint):
                data = dict(self._data)
                del data[key]
                self._commit_quietly(data)  # signed out either way
                return None
            if now - record["last_seen"] < TOUCH_SECONDS:
                return self._session(record)
            renewed = {**record, "last_seen": now}
            if not self._commit_quietly({**self._data, key: renewed}):
                return self._session(record)  # still signed in, renewal retried later
            return self._session(renewed, renewed=True)

    def revoke(self, token: str) -> None:
        if not token or len(token) > _TOKEN_MAX:
            return
        key = hash_token(token)
        with self._lock:
            if key in self._data:
                self._commit({k: v for k, v in self._data.items() if k != key})

    def revoke_handle(self, handle: str, fingerprint: str) -> bool:
        """Sign out the device listed under ``handle``."""
        with self._lock:
            for key, record in self._data.items():
                if _same(record["handle"], handle) and _same(record["fingerprint"], fingerprint):
                    self._commit({k: v for k, v in self._data.items() if k != key})
                    return True
        return False

    def revoke_others(self, token: str) -> int:
        """Sign out every device but the one holding ``token``."""
        keep = hash_token(token) if token and len(token) <= _TOKEN_MAX else None
        with self._lock:
            data = {k: v for k, v in self._data.items() if k == keep}
            gone = len(self._data) - len(data)
            if gone:
                self._commit(data)
            return gone

    def addresses(self) -> list[str]:
        """Client addresses of unexpired sessions, whatever password they
        were opened with: devices the owner has used to sign in."""
        now = self._clock()
        with self._lock:
            return [r["address"] for r in self._data.values() if not self._expired(r, now)]

    def list(self, fingerprint: str) -> list[Session]:
        """Live sessions for the current password, most recently seen first."""
        now = self._clock()
        with self._lock:
            data = self._live(now, fingerprint)
            if len(data) != len(self._data):
                self._commit_quietly(data)
            records = sorted(data.values(), key=lambda r: r["last_seen"], reverse=True)
            return [self._session(r) for r in records]


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---- device names ---------------------------------------------------------------

_BROWSERS = (  # order matters: Edge and Opera say Chrome, Chrome says Safari
    ("Edg/", "Edge"), ("EdgA/", "Edge"), ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"), ("SamsungBrowser/", "Samsung Internet"),
    ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"),
    ("Chrome/", "Chrome"), ("CriOS/", "Chrome"), ("Chromium/", "Chromium"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (  # iOS says "like Mac OS X", Android says Linux
    ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
    ("CrOS", "ChromeOS"), ("Windows", "Windows"), ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"), ("Linux", "Linux"),
)


def describe_device(user_agent: str) -> str:
    """A short name like "Firefox on macOS" for the devices list."""
    agent = user_agent or ""
    browser = next((name for mark, name in _BROWSERS if mark in agent), None)
    if browser is None:
        return "Unknown browser"
    system = next((name for mark, name in _SYSTEMS if mark in agent), None)
    return f"{browser} on {system}" if system else browser


# ---- login throttle -------------------------------------------------------------


def throttle_key(address: str) -> str:
    """What failures are counted against: the full IP address (IPv4-mapped
    IPv6 as IPv4). Rotating addresses, even within one IPv6 /64, is what the
    global cap is for."""
    try:
        ip = ip_address(address)
    except ValueError:
        return address[:64]
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _delay(failures: int, free: int) -> float:
    if failures < free:
        return 0.0
    return min(BASE_WAIT * 2 ** min(failures - free, 20), MAX_WAIT)


class _Streak:
    __slots__ = ("failures", "last")

    def __init__(self) -> None:
        self.failures = 0
        self.last = 0.0

    def wait(self, now: float, free: int) -> float:
        return max(0.0, self.last + _delay(self.failures, free) - now)

    def over(self, now: float, free: int, quiet: float) -> bool:
        """True once ``quiet`` seconds pass with no failure after the wait."""
        return now >= self.last + _delay(self.failures, free) + quiet

    def fail(self, now: float) -> None:
        self.failures += 1
        self.last = now


class LoginThrottle:
    """Per-address and global backoff for failed sign-ins. Thread-safe.

    An address that signed in within the last 90 days skips the global wait
    (its own per-address wait still applies), so a flood of failures from
    other addresses cannot keep the owner out."""

    def __init__(
        self, clock=time.monotonic, max_tracked: int = MAX_TRACKED, max_known: int = MAX_KNOWN
    ) -> None:
        self._clock = clock
        self._max = max_tracked
        self._max_known = max_known
        self._lock = threading.Lock()
        self._addresses: OrderedDict[str, _Streak] = OrderedDict()
        self._known: OrderedDict[str, float] = OrderedDict()  # key -> last success
        self._global = _Streak()

    def _address(self, key: str, now: float) -> _Streak | None:
        streak = self._addresses.get(key)
        if streak is not None and streak.over(now, FREE_FAILURES, ADDRESS_QUIET):
            del self._addresses[key]
            return None
        return streak

    def _global_streak(self, now: float) -> _Streak:
        if self._global.over(now, GLOBAL_FREE_FAILURES, GLOBAL_QUIET):
            self._global = _Streak()
        return self._global

    def _is_known(self, key: str, now: float) -> bool:
        since = self._known.get(key)
        if since is None:
            return False
        if now - since > KNOWN_SECONDS:
            del self._known[key]
            return False
        return True

    def _wait(self, key: str, now: float) -> float:
        streak = self._address(key, now)
        mine = streak.wait(now, FREE_FAILURES) if streak is not None else 0.0
        if self._is_known(key, now):
            return mine
        return max(mine, self._global_streak(now).wait(now, GLOBAL_FREE_FAILURES))

    def _count(self, key: str, now: float) -> None:
        """One more failure on ``key``'s own streak."""
        streak = self._address(key, now)
        if streak is None:
            streak = self._addresses[key] = _Streak()
        self._addresses.move_to_end(key)
        while len(self._addresses) > self._max:
            self._addresses.popitem(last=False)
        streak.fail(now)

    def wait(self, key: str) -> float:
        """Seconds before ``key`` may try again (0 = now). A try refused for
        this wait changes nothing, so it ends on the doubling schedule, at
        most 15 minutes after the last checked failure, however often
        someone sharing the key retries."""
        with self._lock:
            return self._wait(key, self._clock())

    def failure(self, key: str) -> float:
        """Count a wrong password; returns the wait now in force for ``key``."""
        with self._lock:
            now = self._clock()
            self._count(key, now)
            overall = self._global_streak(now)
            overall.fail(now)
            if overall.failures == GLOBAL_FREE_FAILURES:
                _log.warning(
                    "%d failed sign-ins without a %d-minute pause: slowing every sign-in",
                    overall.failures, int(GLOBAL_QUIET // 60),
                )
            return self._wait(key, now)

    def success(self, key: str) -> None:
        """Clear ``key``'s failures and remember it as a known address."""
        with self._lock:
            self._addresses.pop(key, None)
            self._know(key, self._clock())

    def remember(self, keys) -> None:
        """Mark addresses as known (the app passes those of live sessions,
        so a restart or a password change does not forget the owner)."""
        with self._lock:
            now = self._clock()
            for key in keys:
                self._know(key, now)

    def _know(self, key: str, now: float) -> None:
        self._known[key] = now
        self._known.move_to_end(key)
        while len(self._known) > self._max_known:
            self._known.popitem(last=False)

    def tracked(self) -> int:
        with self._lock:
            return len(self._addresses)
