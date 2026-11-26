from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from functools import lru_cache
from ipaddress import IPv6Address, ip_address, ip_network
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from hearthmage.atomicio import remove_stale_temp_files
from hearthmage.auth import (
    IDLE_SECONDS,
    LoginThrottle,
    Session,
    SessionStore,
    describe_device,
    password_fingerprint,
    throttle_key,
)
from hearthmage.backups import build_bundle, restore_bundle, tighten_data_files
from hearthmage.domain import HearthClient, HearthError
from hearthmage.energy import cost, daily_kwh
from hearthmage.energy_store import EnergyStore
from hearthmage.fake_client import FakeHearthClient
from hearthmage.history import HistoryStore
from hearthmage.lan import LAN_SUFFIXES, AddressRejected, host_name, is_ip_literal
from hearthmage.holiday_store import HolidayStore
from hearthmage.schedule import (
    DAY_MINUTES,
    MAX_BLOCKS,
    MAX_TARGET,
    MIN_TARGET,
    WEEKDAY_NAMES,
    Block,
)
from hearthmage.schedule_store import ScheduleStore
from hearthmage.settings import (
    InvalidSetting,
    MqttPasswordReentry,
    Settings,
    config_path,
    parse_port,
)
from hearthmage.sync_store import SyncStore
from hearthmage.waste import WasteDetector, status_diff

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _pct(value) -> float:
    """A bar size as a percentage clamped to 0..100; non-numbers count as 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return min(100.0, max(0.0, number))


templates.env.globals["pct"] = _pct

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Parse the host settings now so a typo is logged at startup, not at the
    # first request (the parsers cache, so it is logged once).
    _parse_allowed_hosts(settings.allowed_hosts)
    _parse_trusted_proxies(settings.trusted_proxies)
    # A share's inherited ACL can have left the app's files readable by others
    # (the Synology NAS did); every write makes its file private again, and
    # this covers the ones that are rarely written. history.py does its own.
    tighten_data_files([
        settings.config_file, settings.schedules_path, settings.sync_path, settings.holiday_path,
        settings.sessions_path, settings.cache_path, settings.energy_path,
    ])
    # A stop mid-write leaves a temp file behind (one from config.json holds
    # the secrets); every store lives next to the config file.
    data_dir = os.path.dirname(settings.config_file) or "."
    removed = sum(remove_stale_temp_files(d) for d in (data_dir, os.path.join(data_dir, "backups")))
    if removed:
        logging.getLogger("hearthmage.app").info("removed %d leftover temp file(s)", removed)
    if settings.auth_enabled:
        _sessions()  # load now: seeds the throttle's known addresses
    yield


# The generated API docs are switched off: nothing needs them on a LAN appliance.
app = FastAPI(title="Heating", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

settings = Settings(config_path())
schedules = ScheduleStore(settings.schedules_path)
energy = EnergyStore(settings.energy_path)
history = HistoryStore(settings.history_path)
holidays = HolidayStore(settings.holiday_path)
waste = WasteDetector()
history.close_open_waste_events()  # a restart forgets what was being tracked
_waste_log = logging.getLogger("hearthmage.waste")
_setpoint_log = logging.getLogger("hearthmage.setpoint")  # who asked for which target

_TEMP_RETAIN_DAYS = 30  # keep a month of temperature readings


mqtt_bridge = None  # set when the optional MQTT bridge is enabled
_mqtt_client = None  # the paho client behind the bridge, kept so a restart can stop it
# Serialises bridge restarts (routes + get_client). Deliberately NOT _client_lock:
# get_client calls _restart_mqtt while holding _client_lock, which is not reentrant.
# Lock order is always _client_lock -> _mqtt_lock, never the reverse.
_mqtt_lock = threading.Lock()


def _record_reading(zone: int, current, setpoint) -> None:
    """History hook for the poller: log a fresh reading, prune, and mirror to MQTT."""
    now = datetime.now(timezone.utc).timestamp()
    history.record_temp(str(zone), current, setpoint, ts=now)
    history.prune_old(now, _TEMP_RETAIN_DAYS * 86400)
    try:
        _check_waste(str(zone), now, current, setpoint)
    except Exception:  # noqa: BLE001 - waste detection must not disturb polling
        _waste_log.exception("waste check failed for zone %s", zone)
    if mqtt_bridge is not None:
        try:
            mqtt_bridge.publish_state(str(zone), current, setpoint)
        except Exception:  # noqa: BLE001 - MQTT must not disturb polling
            pass


def _check_waste(zone: str, ts: float, current, setpoint) -> None:
    """Feed a reading to the waste detector; store events and capture the
    radiator's status when one starts (and now and then at rest, to compare)."""
    change = waste.feed(zone, ts, current, setpoint)
    if change is None:
        if waste.baseline_due(zone, ts, current, setpoint):
            _capture_status(zone, "rest")
        return
    kind, event = change
    if kind == "start":
        event.id = history.start_waste_event(
            zone, event.start_ts, event.start_temp, event.setpoint, event.peak_ts, event.peak_temp
        )
        _waste_log.warning(
            "zone %s heating with target %s: %.1f -> %.1f C", zone, event.setpoint,
            event.start_temp, event.peak_temp,
        )
        _capture_status(zone, "heating", event.id)
    elif event.id is not None:
        history.update_waste_event(event.id, event.peak_ts, event.peak_temp, open_=kind == "update")


def _capture_status(zone: str, kind: str, event_id: int | None = None) -> None:
    """Read the radiator's raw status (``*?F``) in the background and keep it."""
    read = getattr(_client, "read_status_raw", None)
    if read is None:
        return

    def capture() -> None:
        try:
            raw = read(zone)
        except HearthError as exc:
            _waste_log.info("zone %s %s status capture failed: %s", zone, kind, exc)
            return
        history.record_status(zone, datetime.now(timezone.utc).timestamp(), kind, raw, event_id)

    threading.Thread(target=capture, name="status-capture", daemon=True).start()


class _MqttCommandHandler:
    """Routes MQTT commands back through the app's single serialised client, so
    the hub keeps exactly one owner."""

    def set_temperature(self, zone_id: str, temperature: float) -> None:
        _setpoint_log.info("zone %r target %s requested by mqtt", zone_id, temperature)
        try:
            get_client().set_temperature(zone_id, temperature)
        except (HearthError, NotConfigured):
            pass

    def turn_on(self, zone_id: str) -> None:
        _setpoint_log.info("zone %r turned on requested by mqtt", zone_id)
        try:
            turn_on = getattr(get_client(), "turn_on", None)
            if turn_on is not None:
                turn_on(zone_id)
        except (HearthError, NotConfigured):
            pass


def _restart_mqtt() -> None:
    """(Re)start the MQTT bridge from current settings: stop any running paho
    client first, then connect a new one - or none, if MQTT is now disabled.
    Serialised by _mqtt_lock so concurrent saves cannot leak a client. The
    connect itself is non-blocking (connect_async), so a dead broker cannot
    stall the request. Failures are logged, not fatal."""
    with _mqtt_lock:
        _restart_mqtt_locked()


def _restart_mqtt_locked() -> None:
    global mqtt_bridge, _mqtt_client
    import logging

    log = logging.getLogger("hearthmage.mqtt")
    if _mqtt_client is not None:
        try:
            _mqtt_client.disconnect()  # paho's order: the loop sends DISCONNECT and exits
            _mqtt_client.loop_stop()
        except Exception:  # noqa: BLE001 - a wedged old client must not block the new one
            log.exception("Stopping the old MQTT client failed")
        _mqtt_client = None
        mqtt_bridge = None
    config = settings.mqtt_config()
    if not config:
        return
    from hearthmage.mqtt_bridge import MqttBridge

    bridge = MqttBridge(
        publish=lambda *a, **k: None,  # replaced by the real client on connect()
        handler=_MqttCommandHandler(),
        base_topic=config["base_topic"],
        node_id=config["node_id"],
    )
    try:
        _mqtt_client = bridge.connect(
            config["host"], config["port"], config["username"], config["password"]
        )
        # Use the already-built client directly: get_client() would re-enter
        # _client_lock when this runs during the client build.
        rooms = _safe_rooms(_client)[0] if _client is not None else []
        bridge.publish_discovery([(r.id, r.name) for r in rooms])
        mqtt_bridge = bridge
    except Exception:  # noqa: BLE001 - a broken broker must not break the app
        log.exception("MQTT bridge failed to start")

_client: HearthClient | None = None
_client_hub: tuple[str, int] | None = None  # (ip, port) the current client was built for
_client_lock = threading.Lock()  # guards the lazy client build (routes run in a threadpool)
sync = SyncStore(settings.sync_path)  # persisted zone -> "pending" | "synced" | "failed"
_retry_started = False


_SESSION_COOKIE = "hearth_session"
# Paths reachable without a session even when auth is on (login, assets, health).
_PUBLIC_PATHS = frozenset({"/login", "/logout", "/manifest.webmanifest", "/sw.js",
                           "/healthz", "/favicon.ico"})
_HSTS = "max-age=31536000"

# Process-wide; tests swap both. The store follows settings.sessions_path.
_login_throttle = LoginThrottle()
_session_store: SessionStore | None = None
_session_store_lock = threading.Lock()


def _sessions() -> SessionStore:
    """The session store. Loading it (at startup or first use) also marks the
    addresses of its sessions as known to the throttle, so after a restart or
    a password change a flood of failures elsewhere cannot keep the owner out."""
    global _session_store
    with _session_store_lock:
        if _session_store is None or _session_store.path != settings.sessions_path:
            _session_store = SessionStore(settings.sessions_path)
            _login_throttle.remember(throttle_key(a) for a in _session_store.addresses())
        return _session_store


def _fingerprint() -> str:
    """Ties sessions to the current password and secret key: change either
    and every session opened before counts as signed out."""
    return password_fingerprint(settings.auth_password or "", settings.secret_key())


def _same_secret(given: str, expected: str) -> bool:
    """Constant-time string compare. ``hmac.compare_digest`` raises on non-ASCII
    str, so compare the UTF-8 bytes instead."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _current_session(request: Request) -> Session | None:
    token = request.cookies.get(_SESSION_COOKIE, "")
    if not token or not settings.auth_enabled:
        return None
    return _sessions().lookup(token, _fingerprint())


def _is_authenticated(request: Request) -> bool:
    return _current_session(request) is not None


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    # Secure only over https: on the plain-http LAN default the browser would
    # drop a Secure cookie and nobody could sign in.
    response.set_cookie(
        _SESSION_COOKIE, token, max_age=IDLE_SECONDS, httponly=True, samesite="strict",
        path="/", secure=_request_scheme(request) == "https",
    )


_security_log = logging.getLogger("hearthmage.security")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _split_host(raw: str) -> tuple[str, int | None] | None:
    """Parse a Host-style ``host[:port]`` strictly: an IPv4 address or name, or
    a bracketed IPv6 address, then an optional port. Returns (host, port) with
    IPv6 unbracketed, or None for anything else (userinfo, spaces, a path,
    an unbracketed IPv6 address, an empty or out-of-range port)."""
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0 or "%" in raw[:end]:  # no zone ids
            return None
        try:
            host = str(IPv6Address(raw[1:end]))
        except ValueError:
            return None
        rest = raw[end + 1:]
        if rest and not rest.startswith(":"):
            return None
        port_text = rest[1:] if rest else None
    else:
        if raw.count(":") > 1:
            return None
        name, sep, port_text = raw.partition(":")
        if not sep:
            port_text = None
        try:
            host = str(ip_address(name))
        except ValueError:
            host = host_name(name)
            if host is None:
                return None
    if port_text is None:
        return host, None
    port = _parse_port(port_text)
    return (host, port) if port is not None else None


def _parse_port(text: str) -> int | None:
    """A port of 1-5 ASCII digits in 1..65535, or None."""
    if not (text.isascii() and text.isdigit() and len(text) <= 5):
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


@lru_cache(maxsize=8)
def _parse_allowed_hosts(raw: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """HEARTHMAGE_ALLOWED_HOSTS -> (exact names, wildcard suffixes). A ``*.``
    entry matches one extra label and needs at least two labels after it, so
    ``*`` or ``*.com`` cannot open the app to every name. Bad entries are
    logged and skipped."""
    exact: set[str] = set()
    wildcards: list[str] = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        wildcard = entry.startswith("*.")
        name = host_name(entry[2:] if wildcard else entry)
        if name is None or (wildcard and "." not in name):
            _security_log.warning("Ignoring HEARTHMAGE_ALLOWED_HOSTS entry %r", entry)
            continue
        if wildcard:
            wildcards.append("." + name)
        else:
            exact.add(name)
    return frozenset(exact), tuple(wildcards)


@lru_cache(maxsize=8)
def _parse_trusted_proxies(raw: str) -> tuple:
    """HEARTHMAGE_TRUSTED_PROXIES -> networks. Each entry is an IP or a CIDR
    with no host bits set; bad entries are logged and skipped."""
    networks = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        try:
            networks.append(ip_network(entry))
        except ValueError:
            _security_log.warning("Ignoring HEARTHMAGE_TRUSTED_PROXIES entry %r", entry)
    return tuple(networks)


def _host_allowed(host: str) -> bool:
    """Names a DNS-rebinding page cannot hold: IP literals, localhost,
    single-label and LAN-suffix names, plus the owner's own list."""
    try:
        ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or "." not in host or host.endswith(LAN_SUFFIXES):
        return True
    exact, wildcards = _parse_allowed_hosts(settings.allowed_hosts)
    if host in exact:
        return True
    return any(
        host.endswith(suffix) and "." not in host[: -len(suffix)] and len(host) > len(suffix)
        for suffix in wildcards
    )


def _from_trusted_proxy(request: Request) -> bool:
    """True if the direct peer is in HEARTHMAGE_TRUSTED_PROXIES."""
    networks = _parse_trusted_proxies(settings.trusted_proxies)
    if not networks or request.client is None:
        return False
    try:
        peer = ip_address(request.client.host)
    except ValueError:
        return False
    if isinstance(peer, IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    return any(peer.version == net.version and peer in net for net in networks)


def _forwarded(request: Request, name: str) -> str | None:
    """The value the nearest proxy put in a forwarded header (the last one in
    a comma list), or None if there is none."""
    values = ",".join(request.headers.getlist(name))
    last = values.split(",")[-1].strip()
    return last or None


def _request_scheme(request: Request) -> str:
    """``http`` or ``https`` as the browser sees it: X-Forwarded-Proto from a
    trusted proxy, else the scheme of the connection itself."""
    if _from_trusted_proxy(request):
        proto = (_forwarded(request, "x-forwarded-proto") or "").lower()
        if proto in _DEFAULT_PORTS:
            return proto
    # From the scope, not request.url: that rebuilds the URL from the Host
    # header and raises on a malformed one (this runs for every response).
    return request.scope.get("scheme", "http")


def _client_address(request: Request) -> str:
    """The client's IP: from a trusted proxy, the address it forwarded for
    (the last X-Forwarded-For entry, if it is a valid IP); else the peer."""
    peer = request.client.host if request.client else ""
    if _from_trusted_proxy(request):
        try:
            return str(ip_address(_forwarded(request, "x-forwarded-for") or ""))
        except ValueError:
            pass
    return peer or "unknown"


def _direct_host(request: Request) -> tuple[str, int | None] | None:
    """(host, port) from the single Host header, or None if it is missing,
    repeated or malformed."""
    hosts = request.headers.getlist("host")
    return _split_host(hosts[0]) if len(hosts) == 1 else None


def _request_host(request: Request) -> tuple[str, int | None] | None:
    """(host, port) as the browser sees it: from a trusted proxy,
    X-Forwarded-Host overrides the Host header, and X-Forwarded-Port the port
    unless X-Forwarded-Host already names one; else the Host header. None if
    the header used is malformed."""
    target = _direct_host(request)
    if target is None or not _from_trusted_proxy(request):
        return target
    forwarded = _forwarded(request, "x-forwarded-host")
    if forwarded is not None:
        target = _split_host(forwarded)
        if target is None:
            return None
        if target[1] is not None:
            # A port here is the public one (proxy published as 8443:443);
            # X-Forwarded-Port would then be the proxy's own 443.
            return target
    port = _parse_port(_forwarded(request, "x-forwarded-port") or "")
    return (target[0], port) if port is not None else target


def _same_origin(request: Request, host: str, port: int | None) -> bool:
    """CSRF guard: a mutating request's Origin/Referer must have this request's
    scheme, host and port. Browsers always send Origin on a cross-site POST, so
    a request with neither header is a script or curl on the LAN, which the
    open default allows."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    origin_scheme = parsed.scheme.lower()
    origin_host = _split_host(parsed.netloc)
    if origin_scheme not in _DEFAULT_PORTS or origin_host is None:
        return False
    scheme = _request_scheme(request)
    return (
        origin_scheme == scheme
        and origin_host[0] == host
        and (origin_host[1] or _DEFAULT_PORTS[origin_scheme]) == (port or _DEFAULT_PORTS[scheme])
    )


_UPLOAD_PATH = "/settings/import"
_MAX_IMPORT_BYTES = 1024 * 1024  # a real export is a few KB
_MAX_IMPORT_BODY = _MAX_IMPORT_BYTES + 64 * 1024  # plus multipart framing
_MAX_FORM_BODY = 64 * 1024  # the largest real form is a few KB
_IMPORT_TOO_LARGE = "That file is too large to be a HearthMage backup."


class _UploadCap:
    """Pure ASGI guard on request body size. Starlette parses and spools a whole
    body before a route runs, so a route cannot bound it. Every POST is capped
    at 64 KiB, the backup upload at 1 MiB plus multipart framing. A Content-Length
    over the cap is refused up front; the body bytes are also counted as they
    arrive, for a chunked or understated length. When the count trips, the app is
    fed a disconnect, anything it then tries to send is dropped (FastAPI would
    answer with a JSON 400) and this answers 413 itself, unless the app had
    already started a response. Added before ``_security`` so the host, origin
    and session checks run first, and the security headers (added after, hence
    outside) still reach the 413."""

    def __init__(self, app) -> None:
        self.app = app

    async def _refuse(self, scope, send) -> None:
        async def no_body():
            return {"type": "http.disconnect"}

        if scope["path"] == _UPLOAD_PATH:
            # The settings page with the error, so the user sees what happened.
            request = Request(scope, no_body)
            response = await run_in_threadpool(
                _settings_response, request, {"import_error": _IMPORT_TOO_LARGE}, 413
            )
        else:
            response = PlainTextResponse("That request is too large.", status_code=413)
        await response(scope, no_body, send)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        limit = _MAX_IMPORT_BODY if scope["path"] == _UPLOAD_PATH else _MAX_FORM_BODY
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    too_big = int(value) > limit
                except ValueError:
                    too_big = True
                if too_big:
                    await self._refuse(scope, send)
                    return
        received = 0
        tripped = False
        started = False

        async def counted():
            nonlocal received, tripped
            if tripped:
                return {"type": "http.disconnect"}
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                tripped = True
                return {"type": "http.disconnect"}
            return message

        async def tracked(message):
            nonlocal started
            if tripped:
                return  # the app's answer to a body it never fully got
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except Exception:
            if not tripped:
                raise
        if tripped and not started:
            await self._refuse(scope, send)


app.add_middleware(_UploadCap)


@app.middleware("http")
async def _security(request: Request, call_next):
    """Every request must name a host this app answers to, on every path: an
    IP literal, localhost, a single-label name, a .local/.lan/.home.arpa/
    .internal name, or one listed in HEARTHMAGE_ALLOWED_HOSTS. That stops DNS
    rebinding, where a hostile page re-points its own name at this app's LAN
    address. Behind a proxy listed in HEARTHMAGE_TRUSTED_PROXIES, the
    X-Forwarded-Host/-Proto/-Port it sends stand in for host, scheme and port,
    and both the Host header and the forwarded host must pass; from anyone
    else those headers are ignored.

    Every mutating request must also come from this site (CSRF guard), password
    or not: its Origin must match the scheme, host and port above. Without it
    any web page a LAN user opens could drive the heating or repoint the hub and
    MQTT broker with a plain cross-site form post. When a password is set, the
    UI also needs a session. No password => open to anyone on the LAN."""
    # The Host header must pass even behind a trusted proxy (which keeps Host
    # and sets X-Forwarded-Host itself): a rebinding page reaching the app
    # through that hop sends its own name in Host and could pick any
    # X-Forwarded-Host it likes.
    direct = _direct_host(request)
    target = _request_host(request)
    if direct is None or target is None or not (
        _host_allowed(direct[0]) and _host_allowed(target[0])
    ):
        return PlainTextResponse("Unknown host", status_code=400)
    mutating = request.method in ("POST", "PUT", "PATCH", "DELETE")
    if mutating and not _same_origin(request, *target):
        return PlainTextResponse("CSRF check failed", status_code=403)
    if not settings.auth_enabled:
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    # In the threadpool: the lookup may write the sessions file.
    session = await run_in_threadpool(_current_session, request)
    if session is None:
        if mutating:
            return PlainTextResponse("Forbidden", status_code=403)
        if request.headers.get("HX-Request") == "true":  # a poll: move the tab, not the partial
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)
    response = await call_next(request)
    if session.renewed:  # keep the cookie's 90 days in step with the server's
        _set_session_cookie(response, request, request.cookies[_SESSION_COOKIE])
    return response


_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


# Registered after _security so it wraps it: refusals and redirects get the
# headers too. The templates carry no inline script or style (a test enforces it).
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        # Answer here, not in Starlette's outer handler, so a 500 carries the headers.
        logging.getLogger("hearthmage.app").exception("unhandled error on %s %s", request.method, request.url.path)
        response = PlainTextResponse("Internal Server Error", status_code=500)
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    # HSTS only over https: on plain http (the LAN default) a browser that
    # saw it would refuse that host name over http from then on.
    if _request_scheme(request) == "https":
        response.headers["Strict-Transport-Security"] = _HSTS
    return response


class NotConfigured(Exception):
    """Raised when no hub is configured yet; handled by redirecting to setup."""


@app.exception_handler(NotConfigured)
async def _handle_not_configured(request: Request, exc: NotConfigured) -> Response:
    # /healthz is for monitors, not humans: report unconfigured as unhealthy JSON
    # rather than bouncing to the setup page.
    if request.url.path == "/healthz":
        return JSONResponse({"configured": False, "ok": False}, status_code=503)
    return RedirectResponse("/settings", status_code=303)


def get_client() -> HearthClient:
    global _client, _client_hub
    if settings.use_fake:
        return FakeHearthClient()
    if not settings.hub_ip:
        raise NotConfigured()
    hub = (settings.hub_ip, settings.hub_port)
    with _client_lock:  # two concurrent first requests must not each build a poller
        if _client is None or _client_hub != hub:
            from hearthmage.nexho_client import NexhoLocalClient

            _stop_client(_client)  # hub changed: the old poller must not keep running

            client = NexhoLocalClient(
                settings.hub_ip,
                settings.hub_port,
                settings.zone_names,
                cache_file=settings.cache_path,
                on_reading=_record_reading,
                on_energy=_record_energy,
                on_zones=_announce_zones,
                is_held=_zone_held,
            )
            client.start()  # begin background polling
            _client = client
            _client_hub = hub
            _start_retry_loop()
            if mqtt_bridge is None and settings.mqtt_enabled:
                _restart_mqtt()  # startup: nothing running yet, so this just starts it
            elif mqtt_bridge is not None and not mqtt_bridge.has_zones():
                # The bridge started before any hub client existed (e.g. via a
                # settings save), so discovery went out empty. Announce now.
                with _mqtt_lock:
                    if mqtt_bridge is not None and not mqtt_bridge.has_zones():
                        rooms, _err = _safe_rooms(client)
                        mqtt_bridge.publish_discovery([(r.id, r.name) for r in rooms])
        return _client


def _start_retry_loop() -> None:
    """Start the background schedule-sync retry thread once."""
    global _retry_started
    if _retry_started:
        return
    _retry_started = True
    threading.Thread(target=_retry_loop, name="sync-retry", daemon=True).start()


def _stop_client(client: HearthClient | None) -> None:
    stop = getattr(client, "stop", None)
    if stop is not None:
        stop()


def _drop_client() -> None:
    """Forget the hub client (stopping its poller); the next request rebuilds it."""
    global _client, _client_hub
    with _client_lock:
        old, _client, _client_hub = _client, None, None
        _stop_client(old)  # under the lock: no new client starts while it runs
    with _sync_lock:
        _sync_backoff.clear()  # waits earned against the old hub mean nothing now


def _announce_zones(zones: list[tuple[str, str]]) -> None:
    """The poller's hook for a new or changed zone list: tell Home Assistant.
    On a first run nothing is known when the client is built."""
    with _mqtt_lock:
        if mqtt_bridge is not None:
            mqtt_bridge.publish_discovery(zones)


def _safe_rooms(client: HearthClient) -> tuple[list, str | None]:
    try:
        return client.list_rooms(), None
    except HearthError as exc:
        return [], f"Cannot reach the hub: {exc}"


def _find_room(client: HearthClient, room_id: str):
    for room in client.list_rooms():
        if room.id == room_id:
            return room
    return None


def _readout(request: Request, room_id: str, room, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_readout.html", {"room_id": room_id, "room": room, "error": error}
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, client: HearthClient = Depends(get_client)) -> HTMLResponse:
    rooms, error = _safe_rooms(client)
    hub_stale = getattr(client, "hub_stale", lambda: False)()
    day_ago = datetime.now(timezone.utc).timestamp() - 86400
    wasting = set(history.zones_with_waste(since_ts=day_ago))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rooms": rooms,
            "error": error,
            "hub_stale": hub_stale,
            "connecting": getattr(client, "connecting", lambda: False)(),
            "wasting": [r for r in rooms if r.id in wasting],
        },
    )


_MANIFEST = {
    "name": "HearthMage",
    "short_name": "HearthMage",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#faf4ec",
    "theme_color": "#faf4ec",
    "icons": [
        {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml",
         "purpose": "any maskable"}
    ],
}

# Minimal service worker: static assets are served cache-first, pages always
# from the network (the cached home page is only the offline fallback, and only
# for page loads: a failed htmx poll must fail, or htmx swaps a whole home page
# into a readout and each copy starts polling too). The app
# is LAN-only, so this is about install ergonomics and fast loads, not full
# offline use. The cache name carries a hash of the static files, so changing
# any of them changes sw.js and browsers drop the old cache.
_STATIC_SHELL_FILES = ("styles.css", "htmx.min.js", "icon.svg", "app.js", "target-box.js",
                       "schedule.js")
_SERVICE_WORKER = """
const CACHE = 'hearth-%(version)s';
const STATIC = ['/static/styles.css', '/static/htmx.min.js', '/static/icon.svg',
                '/static/app.js', '/static/target-box.js',
                '/static/schedule.js', '/manifest.webmanifest'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC.concat(['/'])))
    .then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) =>
    Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
  ).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (STATIC.includes(url.pathname)) {
    e.respondWith(caches.match(req).then((r) => r || fetch(req)));
  } else if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/')));
  }
});
"""


def _service_worker_source() -> str:
    digest = hashlib.sha256(json.dumps(_MANIFEST, sort_keys=True).encode())
    for name in _STATIC_SHELL_FILES:
        digest.update((BASE_DIR / "static" / name).read_bytes())
    return _SERVICE_WORKER % {"version": digest.hexdigest()[:12]}


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(_MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> Response:
    # Served from the root so its scope covers the whole app.
    return Response(_service_worker_source(), media_type="text/javascript")


@app.get("/healthz")
def healthz(request: Request, client: HearthClient = Depends(get_client)) -> JSONResponse:
    """Liveness/health for monitoring: poller alive and hub answering recently.
    An unconfigured app is reported as 503 by the NotConfigured handler above.
    With a password set, callers without a session get only configured/ok."""
    report = getattr(client, "health", None)
    if report is None:  # a client without health data is assumed fine
        return JSONResponse({"configured": True, "ok": True})
    data = report()
    ok = bool(data.get("poller_alive", True)) and not data.get("hub_stale", False)
    if settings.auth_enabled and not _is_authenticated(request):
        data = {}  # a public probe learns only up or down, not zones or timings
    return JSONResponse({"configured": True, "ok": ok, **data}, status_code=200 if ok else 503)


@app.get("/rooms", response_class=HTMLResponse)
def rooms_ready(
    request: Request, stale: str = "", client: HearthClient = Depends(get_client)
) -> Response:
    """Polled while the hub's first zone list is awaited. ``stale`` is what the
    page last showed ("1" = the hub-quiet banner). 204 (nothing to swap) while
    nothing changed; otherwise the banner and room list, out of band."""
    rooms, error = _safe_rooms(client)
    connecting = getattr(client, "connecting", lambda: False)()
    hub_stale = getattr(client, "hub_stale", lambda: False)()
    if connecting and not rooms and not error and stale == ("1" if hub_stale else "0"):
        return Response(status_code=204)
    return templates.TemplateResponse(
        request, "_rooms_ready.html",
        {"rooms": rooms, "error": error, "connecting": connecting, "hub_stale": hub_stale},
    )


@app.get("/rooms/{room_id}/status", response_class=HTMLResponse)
def zone_status(
    request: Request, room_id: str, client: HearthClient = Depends(get_client)
) -> HTMLResponse:
    try:
        return _readout(request, room_id, _find_room(client, room_id))
    except HearthError as exc:
        return _readout(request, room_id, None, error=str(exc))


@app.post("/rooms/{room_id}/temperature", response_class=HTMLResponse)
def set_temperature(
    request: Request,
    room_id: str,
    temperature: float = Form(...),
    client: HearthClient = Depends(get_client),
) -> HTMLResponse:
    # Record what was asked and from where, so a target nobody remembers
    # setting can be traced. %r keeps a crafted zone id on one log line.
    _setpoint_log.info(
        "zone %r target %s requested by %s",
        room_id, temperature, request.client.host if request.client else "unknown",
    )
    _check_zone(room_id)
    error: str | None = None
    try:
        client.set_temperature(room_id, temperature)
    except HearthError as exc:
        error = str(exc)
    try:
        room = _find_room(client, room_id)
    except HearthError:
        room = None
    return _readout(request, room_id, room, error=error)


# ---- auth ---------------------------------------------------------------


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def _wait_text(seconds: float) -> str:
    """A wait as people say it: whole seconds under a minute, else minutes."""
    whole = math.ceil(seconds)
    if whole < 60:
        return _plural(whole, "second")
    return _plural(math.ceil(whole / 60), "minute")


def _login_response(
    request: Request, status_code: int = 200, error: bool = False, wait: float = 0.0
) -> HTMLResponse:
    context = {"error": error, "retry_in": _wait_text(wait) if wait > 0 else None,
               "throttled": status_code == 429}
    resp = templates.TemplateResponse(request, "login.html", context, status_code=status_code)
    if status_code == 429:
        resp.headers["Retry-After"] = str(math.ceil(wait))
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return _login_response(request)


@app.post("/login")
def login(request: Request, password: str = Form(...)) -> Response:
    expected = settings.auth_password
    if not expected:  # no password set: nothing to sign in to, nothing to throttle
        return _login_response(request, 401, error=True)
    address = _client_address(request)
    key = throttle_key(address)
    store = _sessions()  # loads it, which marks known addresses for the throttle
    wait = _login_throttle.wait(key)
    if wait > 0:  # not checked and not counted
        return _login_response(request, 429, wait=wait)
    if not _same_secret(password, expected):
        wait = _login_throttle.failure(key)
        _security_log.warning("failed sign-in from %r", address)
        return _login_response(request, 401, error=True, wait=wait)
    _login_throttle.success(key)
    store.revoke(request.cookies.get(_SESSION_COOKIE, ""))  # this device's old session
    token = store.create(_fingerprint(), request.headers.get("user-agent", ""), address)
    _security_log.info("signed in from %r", address)
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, token)
    return resp


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    if settings.auth_enabled:
        _sessions().revoke(request.cookies.get(_SESSION_COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(
        _SESSION_COOKIE, path="/", httponly=True, samesite="strict",
        secure=_request_scheme(request) == "https",
    )
    return resp


@app.post("/settings/sessions/sign-out")
def sign_out_device(device: str = Form(...)) -> RedirectResponse:
    """Sign out one other device from the devices list (session required by
    the middleware, same-origin checked like every POST)."""
    if settings.auth_enabled:
        _sessions().revoke_handle(device, _fingerprint())
    return RedirectResponse("/settings?signed_out=one#devices", status_code=303)


@app.post("/settings/sessions/sign-out-others")
def sign_out_other_devices(request: Request) -> RedirectResponse:
    if settings.auth_enabled:
        _sessions().revoke_others(request.cookies.get(_SESSION_COOKIE, ""))
    return RedirectResponse("/settings?signed_out=others#devices", status_code=303)


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return _plural(int(seconds // 60), "minute") + " ago"
    if seconds < 2 * 86400:
        return _plural(int(seconds // 3600), "hour") + " ago"
    return _plural(int(seconds // 86400), "day") + " ago"


def _devices_view(request: Request) -> list[dict]:
    """Signed-in devices for the settings page, this one first."""
    if not settings.auth_enabled:
        return []
    current = _current_session(request)
    store = _sessions()
    now = store.now()
    devices = []
    for s in store.list(_fingerprint()):
        created = datetime.fromtimestamp(s.created)
        devices.append({
            "handle": s.handle,
            "label": describe_device(s.user_agent),
            "address": s.address or "unknown address",
            "current": current is not None and s.handle == current.handle,
            "last_seen": "Now" if current is not None and s.handle == current.handle
            else _ago(max(0.0, now - s.last_seen)),
            "last_seen_iso": datetime.fromtimestamp(s.last_seen, timezone.utc).isoformat(),
            "created": f"{created.day} {created:%b %Y}",
            "created_iso": created.date().isoformat(),
        })
    devices.sort(key=lambda d: not d["current"])  # stable: then most recent first
    return devices


# ---- settings / setup ---------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return _settings_response(request)


def _off_lan_warning(address: str) -> str:
    """The standing warning for a stored hub address off the LAN. It names the
    address, so it still reads right after the user types a different one."""
    return (
        f"The saved address {address} is not on your local network. HearthMage "
        "still uses it, but will not save it again: enter an address on your "
        "home network."
    )


_mqtt_default_topic = "hearthmage"


def _off_lan_broker_warning(address: str) -> str:
    """The broker's version: the bridge connects to a name only while it
    resolves to a local address, and never to an IP address off the LAN (see
    mqtt_bridge.vetted_broker_address)."""
    if is_ip_literal(address):
        use = "does not connect to it"
    else:
        use = "connects to it only while it resolves to a local address"
    return (
        f"The saved address {address} is not on your local network. HearthMage "
        f"{use}, and will not save it again: enter an address on your home network."
    )


def _settings_response(
    request: Request, form: dict | None = None, status_code: int = 200
) -> HTMLResponse:
    """The settings page. ``form`` re-shows a rejected hub or MQTT form: the
    values the user typed (never the password) plus the field's error
    (``hub_ip_error``, ``hub_port_error``, ``mqtt_host_error``, ``mqtt_error``).
    A stored address off the LAN gets a standing warning on its field."""
    form = form or {}
    rooms: list = []
    if settings.is_configured():
        try:
            rooms, _ = _safe_rooms(get_client())
        except NotConfigured:
            rooms = []
    listed = {f"name_{room.id}" for room in rooms}
    other_name_errors = [m for k, m in form.get("name_errors", {}).items() if k not in listed]
    rejected_topic = settings.mqtt_base_topic_rejected
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "hub_ip": settings.hub_ip,
            "hub_port": settings.hub_port,
            "rooms": rooms,
            "auth_enabled": settings.auth_enabled,
            "devices": _devices_view(request),
            "signed_out": request.query_params.get("signed_out"),
            "hub_ready": settings.is_configured(),
            "hub_ip_error": None if settings.hub_ip_on_lan else _off_lan_warning(settings.hub_ip),
            **_mqtt_view(),
            "mqtt_base_topic_error": None if rejected_topic is None else (
                f"The saved base topic {rejected_topic} is not valid, so HearthMage uses "
                f"{_mqtt_default_topic}. Enter a valid base topic."
            ),
            "name_other_errors": other_name_errors,
            "mqtt_host_error": (
                None if settings.mqtt_host_on_lan
                else _off_lan_broker_warning(settings.mqtt_config()["host"])
            ),
            **form,
        },
        status_code=status_code,
    )


def _mqtt_view() -> dict:
    """Template context for the MQTT section. Deliberately excludes the stored
    password: only the fact that one is set ever reaches a template."""
    config = settings.mqtt_config() or {}
    return {
        "mqtt_enabled": settings.mqtt_enabled,
        "mqtt_host": config.get("host"),
        "mqtt_port": config.get("port", 1883),
        "mqtt_username": config.get("username"),
        "mqtt_base_topic": config.get("base_topic", "hearthmage"),
        "mqtt_password_set": settings.mqtt_password_set,
    }


@app.post("/settings/mqtt")
def save_mqtt(
    request: Request,
    host: str = Form(...),
    port: str = Form("1883"),  # str, then parsed: a stray value must not 422
    username: str = Form(""),
    password: str = Form(""),
    clear_password: str = Form(None),
    base_topic: str = Form("hearthmage"),
) -> Response:
    host = host.strip()
    if not host:
        # Nothing to save; disabling is an explicit action (/settings/mqtt/disable).
        return RedirectResponse("/settings", status_code=303)
    port_num = parse_port(port) or 1883  # same fall-back-to-default style as hub_port
    # Blank password input keeps the stored one; the checkbox clears it.
    action = "clear" if clear_password is not None else ("set" if password else "keep")
    try:
        settings.set_mqtt(host, port_num, username, action, password, base_topic=base_topic)
    except AddressRejected as exc:
        # Nothing was saved: the bridge must not connect off the LAN.
        return _settings_response(
            request,
            {
                "mqtt_host": host,
                "mqtt_port": port_num,
                "mqtt_username": username.strip(),
                "mqtt_base_topic": base_topic,
                "mqtt_host_error": str(exc),
                "focus": "mqtt_host",
            },
            status_code=400,
        )
    except InvalidSetting as exc:
        return _settings_response(
            request,
            {
                "mqtt_host": host,
                "mqtt_port": port_num,
                "mqtt_username": username.strip(),
                "mqtt_base_topic": base_topic,
                "mqtt_base_topic_error": str(exc),
                "focus": "mqtt_base_topic",
            },
            status_code=400,
        )
    except MqttPasswordReentry:
        # The stored password would go, in plaintext, to a broker it was not
        # entered for. Nothing was saved; ask for it again.
        return _settings_response(
            request,
            {
                "mqtt_host": host,
                "mqtt_port": port_num,
                "mqtt_username": username.strip(),
                "mqtt_base_topic": base_topic,
                "mqtt_error": "The broker host, port or username changed. Enter the "
                "broker password again, or tick “Remove the stored broker "
                "password” to connect without one.",
            },
            status_code=400,
        )
    _restart_mqtt()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/mqtt/disable")
def disable_mqtt() -> RedirectResponse:
    settings.clear_mqtt()
    _restart_mqtt()  # with no config left this just stops the bridge
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/discover", response_class=HTMLResponse)
def discover_hub(request: Request) -> HTMLResponse:
    from hearthmage.discovery import discover_local

    # The configured hub's probe waits its turn on the hub's wire (see discovery).
    hub = (settings.hub_ip, settings.hub_port) if settings.hub_ip else None
    try:
        hubs = discover_local(hub=hub)
    except OSError:
        hubs = []  # no usable network interface; fall back to manual entry
    return templates.TemplateResponse(request, "_discover.html", {"hubs": hubs})


@app.post("/settings/hub")
def save_hub(
    request: Request,
    hub_ip: str = Form(...),
    hub_port: str = Form("6653"),  # str, then parsed: a stray value must not 422
) -> Response:
    try:
        settings.set_hub(hub_ip, hub_port)
    except AddressRejected as exc:
        # Nothing was saved; the hub client keeps talking to the old address.
        field = "hub_port" if exc.field == "hub_port" else "hub_ip"
        form = {"hub_ip": hub_ip.strip(), "hub_port": hub_port.strip(), "focus": field}
        # A port error leaves hub_ip_error alone: a standing warning about the
        # stored hub stays on the page.
        form[f"{field}_error"] = str(exc)
        return _settings_response(request, form, status_code=400)
    _drop_client()  # force a rebuild against the new hub
    return RedirectResponse("/", status_code=303)


@app.post("/settings/names")
async def save_names(request: Request) -> Response:
    form = await request.form()
    names: dict[str, str] = {}
    for key, value in form.items():
        zone_id = key[len("name_"):]
        if key.startswith("name_") and zone_id.isascii() and zone_id.isdigit():
            names[zone_id] = str(value)  # any other name_* field is not ours; keys are checked below
    try:
        settings.set_zone_names(names)  # one save, one backup snapshot
    except InvalidSetting as exc:
        # Nothing was saved. Show what was typed and mark the field.
        return await run_in_threadpool(
            _settings_response,
            request,
            {"name_values": names, "name_errors": {exc.field: str(exc)}, "focus": exc.field},
            400,
        )
    set_names = getattr(_client, "set_names", None)
    if set_names is not None:
        set_names(settings.zone_names)
    return RedirectResponse("/settings", status_code=303)


# ---- manufacturer cloud reporting ----------------------------------------


def _cloud_view(request: Request, client: HearthClient, error: str | None = None) -> HTMLResponse:
    read = getattr(client, "read_cloud_reporting", None)
    state: bool | None = None
    if read is None:
        error = error or "This hub connection can't read this setting."
    else:
        try:
            state = read()
        except HearthError:
            error = error or "Couldn't read this setting from the hub. Try again in a minute."
    return templates.TemplateResponse(request, "_cloud.html", {"cloud_on": state, "error": error})


@app.get("/settings/cloud", response_class=HTMLResponse)
def cloud_reporting(request: Request, client: HearthClient = Depends(get_client)) -> HTMLResponse:
    return _cloud_view(request, client)


@app.post("/settings/cloud")
def set_cloud_reporting(
    request: Request, enabled: int = Form(...), client: HearthClient = Depends(get_client)
) -> Response:
    setter = getattr(client, "set_cloud_reporting", None)
    error = None
    if setter is None:
        error = "This hub connection can't change this setting."
    else:
        try:
            setter(bool(enabled))
        except HearthError:
            error = "The hub didn't confirm the change. Try again."
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse("/settings", status_code=303)  # no-JS form post
    return _cloud_view(request, client, error)


# ---- backup / restore ---------------------------------------------------


@app.get("/settings/export")
def export_backup() -> Response:
    """Download config + schedules as one JSON bundle (the only copy of the
    schedule detail, so users can keep it somewhere safe)."""
    bundle = build_bundle(settings.config_file, settings.schedules_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    body = json.dumps(bundle, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="hearth-backup-{stamp}.json"'},
    )


_IMPORT_CHUNK = 64 * 1024


async def _read_capped(upload: UploadFile, limit: int) -> bytes | None:
    """The upload's bytes, or None if it is longer than ``limit``. Reads in
    chunks and never more than ``limit + 1`` bytes in total."""
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = await upload.read(min(_IMPORT_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    return None if len(data) > limit else data


def _restore_upload(raw: bytes) -> str | None:
    """Restore the bundle and reload the live stores; the error text, or None.
    Holds both stores' locks across restore and reload, and reloads the same
    objects in place: a save already running on either one finishes first and a
    later one writes the restored data, so nothing overwrites the import.
    Blocking file work: call it from a worker thread."""
    with settings._lock, schedules._lock:
        try:
            bundle = json.loads(raw.decode("utf-8"))
            restore_bundle(bundle, settings.config_file, settings.schedules_path)
        except AddressRejected:
            return ("That backup was not restored: its hub or broker address is not "
                    "on your local network.")
        except InvalidSetting as exc:
            return f"That backup was not restored: {exc}"
        except (ValueError, UnicodeDecodeError):
            return "That file is not a valid HearthMage backup."
        settings.reload()
        schedules.reload()
    return None


@app.post("/settings/import")
async def import_backup(request: Request, backup: UploadFile = File(...)) -> Response:
    """Restore a previously exported bundle, then reload the live stores."""
    raw = await _read_capped(backup, _MAX_IMPORT_BYTES)
    if raw is None:
        error = _IMPORT_TOO_LARGE
    else:
        error = await run_in_threadpool(_restore_upload, raw)
    if error:
        return await run_in_threadpool(_settings_response, request, {"import_error": error}, 400)
    # Drop the client so the next request rebuilds it against the (possibly new) hub.
    _drop_client()
    # The restored schedules are now the source of truth: queue every zone for
    # the retry loop to push (it resolves the rebuilt client itself).
    for zone in schedules.zone_ids():
        sync.set(zone, "pending")
    _retry_wake.set()
    return RedirectResponse("/settings", status_code=303)


# ---- history ------------------------------------------------------------


def _sparkline_points(values: list[float], width: int = 320, height: int = 64, pad: int = 4) -> str:
    """SVG polyline points for a value series, normalised to the viewbox."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad) * i / (n - 1)
        y = pad + (height - 2 * pad) * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


_WASTE_DAYS = 30  # how far back the history page lists waste events


def _waste_view(zone: str) -> dict:
    """A zone's recent heating-nobody-asked-for events, for the history page."""
    since = datetime.now(timezone.utc).timestamp() - _WASTE_DAYS * 86400
    events = []
    for e in history.waste_events(zone, since_ts=since, limit=20):
        start = datetime.fromtimestamp(e["start_ts"])
        end = datetime.fromtimestamp(e["peak_ts"])
        events.append({
            "when": f"{start:%a %d %b, %H:%M}–{end:%H:%M}",
            "minutes": round((e["peak_ts"] - e["start_ts"]) / 60),
            "rise": e["peak_temp"] - e["start_temp"],
            "setpoint": e["setpoint"],
            "ongoing": bool(e["open"]),
        })
    changes, captured = [], None
    for e in history.waste_events(zone, since_ts=since, limit=20):
        heating = history.status_for_event(e["id"])
        if heating:  # the most recent event that has a status capture
            rest = history.rest_status_before(zone, heating["ts"])
            changes = status_diff(rest["raw"] if rest else None, heating["raw"])
            captured = {"heating": heating["raw"], "rest": rest["raw"] if rest else None}
            break
    return {
        "events": events,
        "total_minutes": sum(ev["minutes"] for ev in events),
        "changes": changes,
        "captured": captured,
    }


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, client: HearthClient = Depends(get_client)) -> HTMLResponse:
    rooms, error = _safe_rooms(client)
    zones = []
    for room in rooms:
        series = history.temp_series(room.id, limit=1000)
        currents = [s["current"] for s in series if s["current"] is not None]
        zones.append(
            {
                "id": room.id,
                "name": room.name,
                "points": _sparkline_points(currents),
                "count": len(currents),
                "latest": currents[-1] if currents else None,
                "low": min(currents) if currents else None,
                "high": max(currents) if currents else None,
                "waste": _waste_view(room.id),
            }
        )
    return templates.TemplateResponse(
        request, "history.html", {"zones": zones, "error": error}
    )


# ---- per-radiator config ------------------------------------------------


def _config_page(
    request: Request, zone_id: str, client: HearthClient, config: dict | None,
    saved: bool = False, error: str | None = None, status_code: int = 200,
    orig: dict | None = None, unchanged: bool = False,
) -> HTMLResponse:
    """``config`` is what the form shows; ``orig`` is what was read from the
    radiator (None per setting = that read failed). The form posts ``orig`` back
    so a save sends only what the user changed."""
    room = _find_room(client, zone_id)
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "zone_id": zone_id,
            "zone_name": room.name if room else f"Zone {zone_id}",
            "config": config,
            "orig": orig if orig is not None else config,
            "unchanged": unchanged,
            "frost_min": 0,
            "frost_max": 17,
            "offset_limit": 9.9,
            "saved": saved,
            "error": error,
        },
        status_code=status_code,
    )


@app.get("/zones/{zone_id}/config", response_class=HTMLResponse)
def zone_config_page(
    request: Request, zone_id: str, client: HearthClient = Depends(get_client),
    saved: int = 0, unchanged: int = 0,
) -> HTMLResponse:
    _check_zone(zone_id)
    read = getattr(client, "read_config", None)
    try:
        config = read(zone_id) if read else None
    except HearthError:
        config = None
    return _config_page(
        request, zone_id, client, config, saved=bool(saved), unchanged=bool(unchanged)
    )


def _parse_config_form(
    frost_temp: str | None, sensor_offset: str | None
) -> tuple[int | None, float | None]:
    """Validate the submitted config values; raises ValueError with a message
    for the user. A value that was not submitted (its input was disabled because
    the setting could not be read) stays None."""
    frost: int | None = None
    offset: float | None = None
    if frost_temp is not None:
        try:
            frost = int(frost_temp)
        except ValueError:
            raise ValueError("Anti-ice temperature must be a whole number of degrees.") from None
        if not 0 <= frost <= 17:
            raise ValueError("Anti-ice temperature must be from 0 to 17 °C.")
    if sensor_offset is not None:
        try:
            offset = float(sensor_offset)
        except ValueError:
            raise ValueError("Sensor offset must be a number, e.g. -0.5.") from None
        if not (math.isfinite(offset) and abs(offset) <= 9.9):
            raise ValueError("Sensor offset must be from -9.9 to +9.9 °C.")
        offset = round(offset, 1)
    return frost, offset


def _parse_config_orig(keypad: str, window: str, frost: str, offset: str) -> dict:
    """The values the page read from the radiator, posted back as hidden fields.
    Anything missing or unparseable is None: unknown, so never written."""

    def flag(value: str) -> bool | None:
        return {"1": True, "0": False}.get(value)

    def number(value: str, cast):
        try:
            parsed = cast(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None

    return {
        "keypad_lock": flag(keypad),
        "window_sensor": flag(window),
        "frost_temp": number(frost, int),
        "sensor_offset": number(offset, float),
    }


@app.post("/zones/{zone_id}/config")
def save_zone_config(
    request: Request,
    zone_id: str,
    frost_temp: str = Form(None),
    sensor_offset: str = Form(None),
    keypad_lock: str = Form(None),
    window_sensor: str = Form(None),
    orig_keypad_lock: str = Form(""),
    orig_window_sensor: str = Form(""),
    orig_frost_temp: str = Form(""),
    orig_sensor_offset: str = Form(""),
    client: HearthClient = Depends(get_client),
) -> Response:
    """Send the radiator only the settings the user changed. A setting the page
    could not read (no ``orig`` value) is never written: its current value is
    unknown, so anything sent would be a guess overwriting the real one."""
    _check_zone(zone_id)
    orig = _parse_config_orig(
        orig_keypad_lock, orig_window_sensor, orig_frost_temp, orig_sensor_offset
    )
    submitted = {
        "keypad_lock": keypad_lock is not None,
        "window_sensor": window_sensor is not None,
        "frost_temp": frost_temp,
        "sensor_offset": sensor_offset,
    }
    # What to show again if the save fails: unread settings stay unread.
    shown = {key: (value if orig[key] is not None else None) for key, value in submitted.items()}
    try:
        frost, offset = _parse_config_form(frost_temp, sensor_offset)
    except ValueError as exc:
        return _config_page(
            request, zone_id, client, shown, error=str(exc), status_code=400, orig=orig
        )
    wanted = {**submitted, "frost_temp": frost, "sensor_offset": offset}
    changes = {
        key: value
        for key, value in wanted.items()
        if value is not None and orig[key] is not None and value != orig[key]
    }
    apply = getattr(client, "set_config", None)
    if apply is None or not changes:
        return RedirectResponse(f"/zones/{zone_id}/config?unchanged=1", status_code=303)
    try:
        apply(zone_id, **changes)
    except HearthError as exc:
        return _config_page(
            request, zone_id, client, shown, error=str(exc), status_code=502, orig=orig
        )
    return RedirectResponse(f"/zones/{zone_id}/config?saved=1", status_code=303)


# ---- energy -------------------------------------------------------------


def _record_energy(zone, data: dict) -> None:
    """Store one zone's energy reading in the cache and the history DB. Shared by
    the hourly poller sweep and the on-demand /energy refresh."""
    room_id = str(zone)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    energy.set_zone(room_id, data["rated_watts"], data["daily"], data["monthly"], stamp)
    # Record today's total to the history DB so it survives past the hub's own
    # short retention window.
    days = daily_kwh(data["rated_watts"], data["daily"])
    if days:
        today = datetime.now(timezone.utc).date().isoformat()
        history.record_energy_day(room_id, today, days[0])


def _refresh_energy(client: HearthClient, room_ids: list[str]) -> None:
    """Read each zone's energy counters into the cache (background thread)."""
    read = getattr(client, "read_energy", None)
    if read is None:
        return
    for room_id in room_ids:
        try:
            data = read(room_id)
        except HearthError:
            data = None
        if data:  # asleep radiators return None; keep the last-known reading
            _record_energy(room_id, data)


_ENERGY_REFRESH_EVERY = 300.0  # an /energy visit refreshes at most this often
_energy_refresh = {"running": False, "started_at": None}  # guarded by the lock below
_energy_refresh_lock = threading.Lock()


def _start_energy_refresh(client: HearthClient, room_ids: list[str]) -> None:
    """Start a background energy refresh, unless one is running or one started
    in the last _ENERGY_REFRESH_EVERY: each one queues radio reads on the hub."""
    with _energy_refresh_lock:
        last = _energy_refresh["started_at"]
        if _energy_refresh["running"] or (
            last is not None and _monotonic() - last < _ENERGY_REFRESH_EVERY
        ):
            return
        _energy_refresh.update(running=True, started_at=_monotonic())

    def run() -> None:
        try:
            _refresh_energy(client, room_ids)
        finally:
            with _energy_refresh_lock:
                _energy_refresh["running"] = False

    try:
        threading.Thread(target=run, name="energy-refresh", daemon=True).start()
    except BaseException:
        with _energy_refresh_lock:
            _energy_refresh["running"] = False
        raise


def _zone_energy_view(room, price: float) -> dict:
    """Cached energy for one zone, decoded to kWh and cost."""
    cached = energy.get_zone(room.id)
    if not cached:
        return {"id": room.id, "name": room.name, "has_data": False}
    watts = cached.get("rated_watts")
    days = daily_kwh(watts, cached.get("daily", []))
    months = daily_kwh(watts, cached.get("monthly", []))
    today = days[0] if days else 0.0
    week = sum(days)
    return {
        "id": room.id,
        "name": room.name,
        "has_data": True,
        "rated_watts": watts,
        "days": days,
        "months": months,
        "today_kwh": today,
        "week_kwh": week,
        "today_cost": cost(today, price),
        "week_cost": cost(week, price),
        "fetched_at": cached.get("fetched_at"),
    }


@app.get("/energy", response_class=HTMLResponse)
def energy_page(request: Request, client: HearthClient = Depends(get_client)) -> HTMLResponse:
    rooms, error = _safe_rooms(client)
    return _energy_response(request, client, rooms, error)


def _energy_response(
    request: Request,
    client: HearthClient | None,
    rooms: list,
    error: str | None,
    form: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The energy page. ``form`` re-shows a rejected price form (``price_input``,
    ``price_error``). Without a ``client`` no refresh is started."""
    price = settings.price_per_kwh
    zones = [_zone_energy_view(r, price) for r in rooms]
    home_today = sum(z["today_kwh"] for z in zones if z["has_data"])
    home_week = sum(z["week_kwh"] for z in zones if z["has_data"])
    # Kick off a background refresh so the next visit reflects awake radiators.
    if rooms and client is not None:
        _start_energy_refresh(client, [r.id for r in rooms])
    return templates.TemplateResponse(
        request,
        "energy.html",
        {
            "zones": zones,
            "reporting": sum(1 for z in zones if z["has_data"]),
            "error": error,
            "price": price,
            "home_today_kwh": home_today,
            "home_week_kwh": home_week,
            "home_today_cost": cost(home_today, price),
            "home_week_cost": cost(home_week, price),
            **(form or {}),
        },
        status_code=status_code,
    )


@app.post("/settings/price")
def save_price(request: Request, price_per_kwh: str = Form("")) -> Response:
    # str, then parsed: a stray value must not 422 (nan and inf parse as floats)
    try:
        settings.set_price(price_per_kwh.strip())
    except InvalidSetting as exc:
        try:
            rooms, error = _safe_rooms(get_client())
        except NotConfigured:
            rooms, error = [], None
        form = {"price_input": price_per_kwh, "price_error": str(exc)}
        return _energy_response(request, None, rooms, error, form, status_code=400)
    return RedirectResponse("/energy", status_code=303)


# ---- weekly schedule ----------------------------------------------------

_sched_log = logging.getLogger("hearthmage.schedule")

# Every schedule change re-sends the zone's whole week and program state from
# the store (the source of truth), through at most one worker per zone. So
# pushes cannot land out of order, and the final status always describes a full
# push of the latest state: a later success can never hide an earlier failure.
_sync_lock = threading.Lock()
_sync_gen: dict[str, int] = {}  # zone -> bumped on every change
_sync_running: set[str] = set()  # zones with a live sync worker
_retry_wake = threading.Event()  # set to run the retry loop now instead of in 60s
# A zone that keeps failing (radiator asleep or unplugged) is retried less and
# less often: every failed push is several priority RF attempts that hold back
# polling and queue other zones' setpoints behind writes that cannot land.
_RETRY_FIRST_WAIT = 60.0
_RETRY_MAX_WAIT = 1800.0
_sync_backoff: dict[str, tuple[int, float]] = {}  # zone -> (failures in a row, retry not before)
_monotonic = time.monotonic  # indirection so tests can move the clock
# A program switch that reads back wrong is read once more after this pause,
# in case the radiator applies it a moment after its ack (untested).
_PROGRAM_VERIFY_PAUSE = 2.0
# How often synced zones' program state is read back from the hub, to catch a
# radiator whose program changed behind the app's back.
_DRIFT_CHECK_EVERY = 3600.0
_last_drift_check: float | None = None  # monotonic; None: check on the next cycle
# A zone whose program keeps reading back wrong is corrected at most this many
# times a day; after that the app stops fighting it (logged) until the user
# changes the zone's schedule. Every push can send *OFF, which moves setpoints.
_PROGRAM_CORRECTIONS_PER_DAY = 3
_program_corrections: dict[str, list[float]] = {}  # zone -> monotonic times; under _sync_lock
# Zones whose last push hit that cap: they settle as "disagrees", not "synced",
# so the schedule page can say so. Under _sync_lock.
_program_disagrees: set[str] = set()


def _program_corrections_left(zone: str) -> bool:
    """Whether the zone is still under its daily correction cap. Caller holds
    ``_sync_lock``."""
    recent = [t for t in _program_corrections.get(zone, []) if _monotonic() - t < 86400]
    _program_corrections[zone] = recent
    return len(recent) < _PROGRAM_CORRECTIONS_PER_DAY


def _note_program_correction(zone: str) -> bool:
    """Count one correction; False if the cap was already reached (then the
    app gives up on the zone's program state for now)."""
    with _sync_lock:
        if not _program_corrections_left(zone):
            return False
        _program_corrections[zone].append(_monotonic())
        return True


def _note_sync_result(zone: str, ok: bool) -> None:
    """Record a settled push for the retry backoff. Caller holds ``_sync_lock``."""
    if ok:
        _sync_backoff.pop(zone, None)
        return
    failures = _sync_backoff.get(zone, (0, 0.0))[0] + 1
    wait = min(_RETRY_FIRST_WAIT * 2 ** (failures - 1), _RETRY_MAX_WAIT)
    _sync_backoff[zone] = (failures, _monotonic() + wait)


def _resync_zone(client: HearthClient, zone: str) -> bool:
    """Push a zone's stored week and program state to the hub. Returns True if
    the hub accepted everything. Days missing from an owned week are sent as
    cleared, so a cleared day can never survive on the hub."""
    ok = True
    apply = getattr(client, "set_day_pattern", None)
    if apply is not None and schedules.owns(zone):
        stored = schedules.get_zone(zone)
        # Group days that share an identical block list into one write each.
        groups: dict[tuple, list[int]] = {}
        for day in range(7):
            key = tuple((b.target, b.start, b.end) for b in stored.get(day, []))
            groups.setdefault(key, []).append(day)
        for key, days in groups.items():
            try:
                apply(zone, [Block(*t) for t in key], days)
            except HearthError as exc:
                _sched_log.warning("zone %s schedule push for days %s failed: %s", zone, days, exc)
                # The radiator is not taking this write, so the remaining days
                # would most likely fail the same way; the whole week is re-sent
                # on the next attempt anyway. The program switch below is still
                # sent: it is one short command, and switching the program off
                # is how a user stops unwanted heating.
                ok = False
                break
        if ok:
            ok = _verify_week(client, zone, stored)
    toggle = getattr(client, "set_program_active", None)
    active = schedules.get_active(zone)
    if toggle is not None and active is not None:
        # *OFF can move the setpoint (live 2026-11-26: on every zone it was
        # sent to), so note the zone's target first and put it back after;
        # not during a holiday hold, whose level is not the user's target.
        held = holidays.get(zone) is not None
        before = None if (active or held) else _target_of(client, zone)
        with _sync_lock:
            _program_disagrees.discard(zone)  # this push decides it afresh
        try:
            toggle(zone, active)
        except HearthError as exc:
            _sched_log.warning("zone %s program on/off push failed: %s", zone, exc)
            ok = False
        else:
            if before is not None:
                _restore_target(client, zone, before)
            # What a holiday hold does to the program state is not known, so
            # it is not checked while one is recorded.
            if ok and not held:
                ok = _verify_program(client, zone, active)
    return ok


def _target_of(client: HearthClient, zone: str) -> float | None:
    """The zone's current target as the client knows it (0 for off), or None."""
    try:
        room = _find_room(client, zone)
    except HearthError:
        return None
    if room is None or room.target_temp is None:
        return None
    return 0.0 if room.is_off else room.target_temp


def _restore_target(client: HearthClient, zone: str, before: float) -> None:
    """Re-send the target a zone had before *OFF. Skipped if the target the
    client shows has changed meanwhile: that is a newer command (the user's),
    which must not be overwritten."""
    if _target_of(client, zone) != before:
        _sched_log.info("zone %s target changed during the program push; not restoring it", zone)
        return
    try:
        client.set_temperature(zone, before)
    except HearthError as exc:
        _sched_log.warning("zone %s target restore after *OFF failed: %s", zone, exc)


def _verify_program(client: HearthClient, zone: str, active: bool) -> bool:
    """Read the program state back from the hub (``*?F``), so "synced" means
    the radiator runs its program (or not), not just that it acked."""
    read = getattr(client, "read_program_active", None)
    if read is None:
        return True  # a client that cannot read back: the ack is all we have
    try:
        held = read(zone)
        if held is not None and held != active:
            time.sleep(_PROGRAM_VERIFY_PAUSE)
            held = read(zone)
    except HearthError as exc:
        _sched_log.warning("zone %s program state read-back failed: %s", zone, exc)
        return False
    if held is None:
        _sched_log.info("zone %s program state reads as an unknown value; trusting the ack", zone)
        return True
    if held == active:
        return True
    if not _note_program_correction(zone):
        _sched_log.warning(
            "zone %s program still reads %s after %d corrections today; giving up until the "
            "schedule is changed", zone, "on" if held else "off", _PROGRAM_CORRECTIONS_PER_DAY,
        )
        with _sync_lock:
            _program_disagrees.add(zone)
        return True
    _sched_log.warning("zone %s program read-back mismatch: hub has it %s", zone, "on" if held else "off")
    return False


def _check_program_drift(client: HearthClient) -> None:
    """At most every _DRIFT_CHECK_EVERY seconds, read each synced zone's
    program state from the hub and queue a push for any that no longer
    matches the app (live 2026-11-26 the hub had every program on while the
    app had them all off). A zone shown as "disagrees" (corrections given up
    for the day) goes back to "synced" once the hub agrees, and is corrected
    again once the daily cap allows. Zones being pushed, not settled, or with
    a holiday hold recorded are left alone; a zone that cannot be read is
    tried next time."""
    global _last_drift_check
    read = getattr(client, "read_program_active", None)
    if read is None:
        return
    now = _monotonic()
    if _last_drift_check is not None and now - _last_drift_check < _DRIFT_CHECK_EVERY:
        return
    _last_drift_check = now
    for zone in schedules.zone_ids():
        active = schedules.get_active(zone)
        if active is None:
            continue
        with _sync_lock:
            status = sync.get(zone)
            if zone in _sync_running or status not in ("synced", "disagrees"):
                continue
            if status == "synced" and not _program_corrections_left(zone):
                continue  # capped for today: nothing to do even if it drifted
            gen = _sync_gen.get(zone, 0)
        if holidays.get(zone) is not None:
            continue
        try:
            held = read(zone)
        except HearthError as exc:
            _sched_log.info("zone %s program drift check skipped: %s", zone, exc)
            continue
        if held is None:
            continue
        with _sync_lock:
            # A push that ran while we read (the user's change) has settled it.
            if (_sync_gen.get(zone, 0) != gen or zone in _sync_running
                    or sync.get(zone) != status or schedules.get_active(zone) != active):
                continue
            if held == active:
                if status == "disagrees":  # the radiator has come round
                    _program_disagrees.discard(zone)
                    sync.set(zone, "synced")
                continue
            if not _program_corrections_left(zone):
                continue
            _program_corrections[zone].append(_monotonic())
            sync.set(zone, "pending")  # the retry loop pushes pending zones at once
        _sched_log.warning(
            "zone %s program is %s at the hub but %s here; pushing it again",
            zone, "on" if held else "off", "on" if active else "off",
        )


def _verify_week(client: HearthClient, zone: str, stored: dict[int, list[Block]]) -> bool:
    """Read every day back from the hub (``*?P``) and compare with what was
    pushed, so "synced" means the hub holds it, not just that it acked."""
    read = getattr(client, "read_day_pattern", None)
    if read is None:
        return True  # a client that cannot read back: the ack is all we have
    for day in range(7):
        try:
            held = read(zone, day)
        except HearthError as exc:
            _sched_log.warning("zone %s day %s read-back failed: %s", zone, day, exc)
            return False
        if held != stored.get(day, []):
            _sched_log.warning("zone %s day %s read-back mismatch: hub has %s", zone, day, held)
            return False
    return True


def _sync_until_settled(client: HearthClient, zone: str) -> None:
    """Sync worker body; the caller has already added ``zone`` to _sync_running.
    Pushes again if the schedule changed mid-push, then records the result."""
    try:
        while True:
            with _sync_lock:
                gen = _sync_gen.get(zone, 0)
            ok = _resync_zone(client, zone)
            if not ok and getattr(client, "stopped", lambda: False)():
                # Swapped for a new client mid-push (hub save, backup import):
                # not a hub failure, so push again through the current one.
                try:
                    current = get_client()
                except NotConfigured:
                    current = client
                if current is not client:
                    client = current
                    continue
            with _sync_lock:
                if _sync_gen.get(zone, 0) == gen:  # nothing changed while pushing
                    _sync_running.discard(zone)
                    _note_sync_result(zone, ok)
                    if not ok:
                        sync.set(zone, "failed")
                    else:
                        sync.set(zone, "disagrees" if zone in _program_disagrees else "synced")
                    return
    except Exception:  # noqa: BLE001 - never leave the zone stuck on "pending"
        _sched_log.exception("zone %s schedule sync crashed", zone)
        with _sync_lock:
            _sync_running.discard(zone)
            try:
                sync.set(zone, "failed")
            except OSError:
                pass


def _request_sync(client: HearthClient, zone: str) -> None:
    """Mark a zone pending and push it in the background (or let the running
    worker pick the change up)."""
    with _sync_lock:
        _sync_gen[zone] = _sync_gen.get(zone, 0) + 1
        _sync_backoff.pop(zone, None)  # a fresh change starts the retry wait afresh
        _program_corrections.pop(zone, None)  # and the program correction count
        sync.set(zone, "pending")
        if zone in _sync_running:
            return
        _sync_running.add(zone)
    threading.Thread(
        target=_sync_until_settled, args=(client, zone), name="schedule-sync", daemon=True
    ).start()


def _retry_failed_syncs(client: HearthClient) -> None:
    """Push every zone not yet confirmed on the hub (failed, or queued as
    pending), skipping any a worker is already pushing and any failed zone
    still inside its backoff wait. A pending zone was queued on purpose (a
    change or a backup import), so it is pushed without waiting."""
    for zone in sync.unsettled_zones():
        with _sync_lock:
            if zone in _sync_running:
                continue
            if sync.get(zone) == "failed" and _monotonic() < _sync_backoff.get(zone, (0, 0.0))[1]:
                continue
            _sync_running.add(zone)
        _sync_until_settled(client, zone)


def _retry_loop() -> None:
    while True:
        _retry_wake.wait(60)
        _retry_wake.clear()
        try:
            # Resolve the client each cycle so retries follow a hub change.
            client = get_client()
            _retry_failed_syncs(client)
            _check_program_drift(client)  # a drifted zone is pushed on the next cycle
        except NotConfigured:
            pass
        except Exception:  # noqa: BLE001 - a bad retry cycle must not kill the loop
            _sched_log.exception("schedule retry cycle failed")


def _check_zone(zone_id: str) -> None:
    """Zone ids are numeric; anything else is not a zone (and must never reach
    the store, where non-numeric keys are reserved)."""
    if not (zone_id.isascii() and zone_id.isdigit()):
        raise HTTPException(status_code=404)


def _hhmm_to_minutes(value: str, is_end: bool = False) -> int | None:
    """Parse ``HH:MM`` to minutes of day. An end of 00:00 (or 24:00) means
    midnight at the end of the day, 1440."""
    hh, sep, mm = value.strip().partition(":")
    if not (sep and hh.isascii() and hh.isdigit() and mm.isascii() and mm.isdigit()):
        return None
    h, m = int(hh), int(mm)
    if h == 24 and m == 0 and is_end:
        return DAY_MINUTES
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    minutes = h * 60 + m
    return DAY_MINUTES if (is_end and minutes == 0) else minutes


def _minutes_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_target(value: str) -> int | None:
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    target = int(number)
    return target if MIN_TARGET <= target <= MAX_TARGET else None


def _parse_schedule_form(form) -> tuple[list[dict], list[int], list[Block], list[str]]:
    """Read the editor form. Returns (rows to re-render, days, blocks, general
    errors); a row with a problem carries its own ``error`` message. Fully
    blank rows are ignored; nothing else is dropped silently."""
    errors: list[str] = []
    rows: list[dict] = []
    for target, start, end in zip(
        form.getlist("block_target"), form.getlist("block_start"), form.getlist("block_end")
    ):
        row = {"target": str(target).strip(), "start": str(start).strip(),
               "end": str(end).strip(), "error": None}
        if not (row["target"] or row["start"] or row["end"]):
            continue
        rows.append(row)
        if not (row["target"] and row["start"] and row["end"]):
            row["error"] = "Fill in the temperature, start and end."
            continue
        t = _parse_target(row["target"])
        smin = _hhmm_to_minutes(row["start"])
        emin = _hhmm_to_minutes(row["end"], is_end=True)
        if t is None:
            row["error"] = f"Temperature must be a whole number from {MIN_TARGET} to {MAX_TARGET}."
        elif smin is None or emin is None:
            row["error"] = "Enter times as HH:MM."
        elif emin <= smin:
            row["error"] = ("Ends before it starts. To heat overnight, end this day at 00:00 "
                            "and add a frame from 00:00 on the next day.")
        else:
            row["block"] = Block(t, smin, emin)
    valid = sorted((r for r in rows if "block" in r), key=lambda r: r["block"].start)
    for prev, nxt in zip(valid, valid[1:]):
        if nxt["block"].start < prev["block"].end:
            nxt["error"] = prev["error"] = "Overlaps another time frame."
    if len(rows) > MAX_BLOCKS:
        errors.append(f"A day can hold at most {MAX_BLOCKS} time frames.")
    raw_days = [str(d) for d in form.getlist("days")]
    days = [int(d) for d in raw_days if d.isascii() and d.isdigit() and int(d) <= 6]
    if len(days) != len(raw_days):
        errors.append("Unknown day selected.")
    if not raw_days:
        errors.append("Choose at least one day.")
    if not rows:
        errors.append("Add at least one time frame. To remove a day's schedule, use Clear on that day.")
    blocks = [r["block"] for r in rows if "block" in r and not r["error"]]
    if any(r["error"] for r in rows):
        errors.insert(0, "Some time frames need fixing.")
    return rows, sorted(set(days)), blocks, errors


def _schedule_view(zone: str):
    """Per-day rows (with HH:MM strings) for rendering the stored schedule."""
    return _week_rows(schedules.get_zone(zone))


def _week_rows(stored: dict[int, list[Block]]) -> list[dict]:
    """Per-day rows (with HH:MM strings) for rendering a week of blocks."""
    rows = []
    for day, label in enumerate(WEEKDAY_NAMES):
        blocks = stored.get(day, [])
        rows.append(
            {
                "day": day,
                "label": label,
                "blocks": [
                    {
                        "target": b.target,
                        "start": _minutes_to_hhmm(b.start),
                        "end": _minutes_to_hhmm(b.end),  # 1440 shows as 24:00
                    }
                    for b in blocks
                ],
            }
        )
    return rows


def _editor_for_day(zone: str, day: int) -> dict:
    """Editor state preloaded with one day's stored time frames."""
    rows = [
        {"target": str(b.target), "start": _minutes_to_hhmm(b.start),
         "end": _minutes_to_hhmm(b.end % DAY_MINUTES), "error": None}  # 24:00 -> 00:00 input
        for b in schedules.get_day(zone, day)
    ]
    return {
        "title": f"Edit {WEEKDAY_NAMES[day]}",
        "editing": True,
        "rows": rows or [{"target": "20", "start": "07:00", "end": "22:00", "error": None}],
        "days": [day],
        "errors": [],
    }


_NEW_EDITOR = {
    "title": "New pattern",
    "editing": False,
    "rows": [{"target": "20", "start": "07:00", "end": "22:00", "error": None}],
    "days": [],
    "errors": [],
}


def _render_schedule(
    request: Request,
    zone_id: str,
    client: HearthClient,
    editor: dict,
    status_code: int = 200,
    loaded: dict | None = None,
    holiday_error: str | None = None,
) -> HTMLResponse:
    room = _find_room(client, zone_id)
    name = room.name if room else f"Zone {zone_id}"
    days = _schedule_view(zone_id)
    return templates.TemplateResponse(
        request,
        "schedule.html",
        {
            "zone_id": zone_id,
            "zone_name": name,
            "days": days,
            "has_schedule": any(row["blocks"] for row in days),
            "active": schedules.get_active(zone_id),
            "weekdays": list(enumerate(WEEKDAY_NAMES)),
            "sync": sync.get(zone_id),
            "holiday": _current_holiday(zone_id),
            "holiday_error": holiday_error,
            "editor": editor,
            "loaded": loaded,
            "can_load": hasattr(client, "read_day_pattern"),
            "max_blocks": MAX_BLOCKS,
            "min_target": MIN_TARGET,
            "max_target": MAX_TARGET,
        },
        status_code=status_code,
    )


@app.get("/zones/{zone_id}/schedule", response_class=HTMLResponse)
def schedule_page(
    request: Request, zone_id: str, edit: str = "", client: HearthClient = Depends(get_client)
) -> HTMLResponse:
    _check_zone(zone_id)
    if edit.isascii() and edit.isdigit() and int(edit) <= 6:
        editor = _editor_for_day(zone_id, int(edit))
    else:
        editor = _NEW_EDITOR
    return _render_schedule(request, zone_id, client, editor)


@app.post("/zones/{zone_id}/schedule")
async def save_schedule(
    request: Request, zone_id: str, client: HearthClient = Depends(get_client)
) -> Response:
    _check_zone(zone_id)
    form = await request.form()
    rows, days, blocks, errors = _parse_schedule_form(form)
    if errors:
        editor = {
            "title": (
                (f"Edit {WEEKDAY_NAMES[days[0]]}" if len(days) == 1 else "Edit pattern")
                if form.get("editing") == "1" else "New pattern"
            ),
            "editing": form.get("editing") == "1",
            "rows": rows[:MAX_BLOCKS] or _NEW_EDITOR["rows"],
            "days": days,
            "errors": errors,
        }
        return _render_schedule(request, zone_id, client, editor, status_code=400)
    schedules.set_pattern(zone_id, blocks, days)
    _request_sync(client, zone_id)
    return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)


@app.post("/zones/{zone_id}/schedule/clear")
def clear_schedule_day(
    zone_id: str, day: int = Form(...), client: HearthClient = Depends(get_client)
) -> Response:
    _check_zone(zone_id)
    if not 0 <= day <= 6:
        return PlainTextResponse("Unknown day", status_code=400)
    schedules.clear_day(zone_id, day)
    _request_sync(client, zone_id)
    return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)


@app.post("/zones/{zone_id}/program")
def toggle_program(
    zone_id: str, active: int = Form(...), client: HearthClient = Depends(get_client)
) -> RedirectResponse:
    _check_zone(zone_id)
    schedules.set_active(zone_id, bool(active))  # our app is the source of truth for this
    if active:
        # The program now decides the target. A manual setpoint still waiting
        # for a sleeping radiator must not be sent after it and override it.
        cancel = getattr(client, "cancel_waiting_setpoint", None)
        if cancel is not None:
            cancel(zone_id)
    _request_sync(client, zone_id)
    return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)


def _read_week(client: HearthClient, zone: str) -> tuple[dict[int, list[Block]] | None, str | None]:
    """Read all seven days from the hub; (week, None) or (None, error message)."""
    read = getattr(client, "read_day_pattern", None)
    if read is None:
        return None, "This hub connection can't read schedules back."
    week: dict[int, list[Block]] = {}
    for day in range(7):
        try:
            week[day] = read(zone, day)
        except HearthError as exc:
            return None, (f"Couldn't read {WEEKDAY_NAMES[day]} from the radiator ({exc}). "
                          "If it is asleep, try again in a minute.")
    return week, None


def _encode_day(blocks: list[Block]) -> str:
    return ";".join(f"{b.target},{b.start},{b.end}" for b in blocks)


def _decode_day(value: str) -> list[Block]:
    """Inverse of _encode_day; raises ValueError on anything malformed (the
    values come back from the browser, so they are validated again on save)."""
    if not value.strip():
        return []
    blocks = []
    for part in value.split(";"):
        nums = part.split(",")
        if len(nums) != 3 or not all(n.isascii() and n.isdigit() for n in nums):
            raise ValueError(f"malformed block: {part!r}")
        blocks.append(Block(*(int(n) for n in nums)))
    return blocks


@app.post("/zones/{zone_id}/schedule/load", response_class=HTMLResponse)
def load_from_radiator(
    request: Request, zone_id: str, client: HearthClient = Depends(get_client)
) -> HTMLResponse:
    """Preview the week the radiator holds. Nothing is saved until the user
    confirms with /schedule/load/apply."""
    _check_zone(zone_id)
    week, error = _read_week(client, zone_id)
    loaded: dict = {"error": error}
    if week is not None:
        saved = {d: schedules.get_day(zone_id, d) for d in range(7)}
        loaded.update(
            rows=_week_rows(week),
            encoded={d: _encode_day(week[d]) for d in range(7)},
            matches=week == saved,
            empty=not any(week.values()),
        )
    return _render_schedule(request, zone_id, client, _NEW_EDITOR, loaded=loaded)


@app.post("/zones/{zone_id}/schedule/load/apply")
async def apply_loaded_week(
    request: Request, zone_id: str, client: HearthClient = Depends(get_client)
) -> Response:
    _check_zone(zone_id)
    form = await request.form()
    try:
        week = {day: _decode_day(str(form.get(f"day{day}", ""))) for day in range(7)}
        schedules.replace_week(zone_id, week)
    except ValueError:
        return PlainTextResponse(
            "That schedule could not be read. Load it from the radiator again.", status_code=400
        )
    _request_sync(client, zone_id)
    return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)


@app.get("/zones/{zone_id}/schedule/sync", response_class=HTMLResponse)
def schedule_sync_status(request: Request, zone_id: str) -> HTMLResponse:
    _check_zone(zone_id)
    return templates.TemplateResponse(
        request,
        "_sync_status.html",
        {"zone_id": zone_id, "sync": sync.get(zone_id), "active": schedules.get_active(zone_id)},
    )


# ---- holiday hold -------------------------------------------------------

_HOLIDAY_MAX_DAYS = 240  # the hub's limit (*RH days field)


def _current_holiday(zone_id: str) -> dict | None:
    """The zone's holiday hold for display, or None. Records whose end has
    passed (the hub resumes on its own) or that predate the corrected hub
    encoding (no "until") are dropped."""
    record = holidays.get(zone_id)
    if not record:
        return None
    try:
        until = datetime.fromisoformat(record["until"])
    except (KeyError, TypeError, ValueError):
        holidays.clear_if(zone_id, record)  # not one saved since the read
        return None
    if until <= datetime.now():
        holidays.clear_if(zone_id, record)
        return None
    return {
        "until_label": f"{until:%a %d %b}, {until:%H:%M}",
        "arrival_temp": record.get("arrival_temp"),
    }


def _zone_held(zone: int) -> bool:
    """For the hub client: a hold level read from the radiator is not the
    target that MQTT "heat" restores."""
    return _current_holiday(str(zone)) is not None


def _parse_holiday_form(form) -> tuple[datetime, float | None]:
    """Validate the holiday form; raises ValueError with a message for the user."""
    try:
        end_date = date.fromisoformat(str(form.get("until", "")))
    except ValueError:
        raise ValueError("Choose the date you're back.") from None
    minutes = _hhmm_to_minutes(str(form.get("until_time") or "12:00"))
    if minutes is None:
        raise ValueError("Enter the time you're back as HH:MM.")
    until = datetime(end_date.year, end_date.month, end_date.day, minutes // 60, minutes % 60)
    if until <= datetime.now():
        raise ValueError("The return date and time must be in the future.")
    if (until.date() - date.today()).days > _HOLIDAY_MAX_DAYS:
        raise ValueError(f"The hub can hold a zone for at most {_HOLIDAY_MAX_DAYS} days.")
    if form.get("arrival") == "program":
        return until, None
    arrival = _parse_target(str(form.get("arrival_temp", "")))
    if arrival is None:
        raise ValueError(f"The temperature for your return must be {MIN_TARGET} to {MAX_TARGET} °C.")
    return until, float(arrival)


@app.post("/zones/{zone_id}/holiday")
def start_holiday(
    request: Request,
    zone_id: str,
    until: str = Form(""),
    until_time: str = Form(""),
    arrival: str = Form(""),
    arrival_temp: str = Form(""),
    client: HearthClient = Depends(get_client),
) -> Response:
    # A plain def on purpose: the hub write below can block for a long time when
    # the radiator is asleep, and must run in the threadpool, not on the event loop.
    _check_zone(zone_id)
    form = {"until": until, "until_time": until_time, "arrival": arrival,
            "arrival_temp": arrival_temp}
    try:
        until, arrival = _parse_holiday_form(form)
    except ValueError as exc:
        return _render_schedule(
            request, zone_id, client, _NEW_EDITOR, status_code=400, holiday_error=str(exc)
        )
    # Remember the setpoint to restore if the hold is cancelled early. Read it
    # first: set_holiday drops a setpoint still waiting for the radiator, and the
    # user's last request is what to restore, confirmed or not.
    room = _find_room(client, zone_id)
    prev = room.target_temp if (room and not room.is_off) else None
    prev_active = schedules.get_active(zone_id)  # put back on cancel
    running = holidays.get(zone_id) if _current_holiday(zone_id) else None
    if running:  # a hold already runs: the room shows its level, keep the original
        prev = running["prev_setpoint"]
        prev_active = running.get("prev_active")
    setter = getattr(client, "set_holiday", None)
    if setter is not None:
        try:
            setter(zone_id, days=(until.date() - date.today()).days, hour=until.hour,
                   minute=until.minute, arrival_temp=arrival)
        except HearthError as exc:
            return _render_schedule(
                request, zone_id, client, _NEW_EDITOR, status_code=502,
                holiday_error=f"The hub didn't accept the holiday: {exc}",
            )
    holidays.set(zone_id, until=f"{until:%Y-%m-%dT%H:%M}", arrival_temp=arrival, prev_setpoint=prev,
                 prev_active=prev_active)
    if arrival is None and prev_active is not None:
        # "Go back to the weekly schedule": the hub resumes the program when
        # the hold ends, so the app records it as on too, or its program check
        # would switch it off again afterwards (owner decision 2026-11-26). A
        # zone whose program the app has never set stays unmanaged.
        schedules.set_active(zone_id, True)
    elif running and running.get("arrival_temp") is None and prev_active is not None:
        # The hold that switched the program on is replaced by one that ends
        # at a temperature: put the flag back. No push during the hold; the
        # program check corrects the hub once it is over.
        schedules.set_active(zone_id, prev_active)
    return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)


@app.post("/zones/{zone_id}/holiday/cancel")
def cancel_holiday(
    request: Request, zone_id: str, client: HearthClient = Depends(get_client)
) -> Response:
    _check_zone(zone_id)
    flag_changed = False
    clearer = getattr(client, "clear_holiday", None)
    if clearer is not None:
        try:
            clearer(zone_id)
        except HearthError as exc:
            # The hub still holds the zone. Keep the record (and the setpoint to
            # restore) so the page keeps showing the hold and offers Cancel again.
            return _render_schedule(
                request, zone_id, client, _NEW_EDITOR, status_code=502,
                holiday_error=f"The hub didn't cancel the holiday: {exc}. Try again shortly.",
            )
        # Cancelling undoes the holiday: the program on/off goes back to what it
        # was (starting one with "back to the weekly schedule" switched it on),
        # and as cancelling was seen live to leave the zone at its holiday
        # level, the target it had is restored unless a program drives it.
        record = holidays.get(zone_id)
        prev = record.get("prev_setpoint") if record else None
        # Only a hold that switched the program on ("back to the weekly
        # schedule") puts it back; a change the user made meanwhile stays.
        prev_active = record.get("prev_active") if record and record.get("arrival_temp") is None else None
        flag_changed = prev_active is not None and schedules.get_active(zone_id) != prev_active
        if flag_changed:
            schedules.set_active(zone_id, prev_active)
        if prev is not None and schedules.get_active(zone_id) is not True:
            try:
                client.set_temperature(zone_id, prev)
            except HearthError:
                pass  # the hold is cleared; the target can be set by hand
    holidays.clear(zone_id)
    if flag_changed:
        _request_sync(client, zone_id)  # the hub's clear resumes the program: push ours
    return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)
