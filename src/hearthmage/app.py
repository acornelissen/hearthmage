from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import threading
from datetime import date, datetime, timezone
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
from fastapi.templating import Jinja2Templates

from hearthmage.backups import build_bundle, restore_bundle
from hearthmage.domain import HearthClient, HearthError
from hearthmage.energy import cost, daily_kwh
from hearthmage.energy_store import EnergyStore
from hearthmage.fake_client import FakeHearthClient
from hearthmage.history import HistoryStore
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
from hearthmage.settings import Settings, config_path
from hearthmage.sync_store import SyncStore

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Heating")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

settings = Settings(config_path())
schedules = ScheduleStore(settings.schedules_path)
energy = EnergyStore(settings.energy_path)
history = HistoryStore(settings.history_path)
holidays = HolidayStore(settings.holiday_path)

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
    history.prune_temp(before_ts=now - _TEMP_RETAIN_DAYS * 86400)
    if mqtt_bridge is not None:
        try:
            mqtt_bridge.publish_state(str(zone), current, setpoint)
        except Exception:  # noqa: BLE001 - MQTT must not disturb polling
            pass


class _MqttCommandHandler:
    """Routes MQTT commands back through the app's single serialised client, so
    the hub keeps exactly one owner."""

    def set_temperature(self, zone_id: str, temperature: float) -> None:
        try:
            get_client().set_temperature(zone_id, temperature)
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
            _mqtt_client.loop_stop()
            _mqtt_client.disconnect()
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


def _session_token() -> str:
    """The signed value a valid session cookie must hold (proves the password)."""
    return hmac.new(settings.secret_key().encode(), b"authenticated:v1", hashlib.sha256).hexdigest()


def _is_authenticated(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get(_SESSION_COOKIE, ""), _session_token())


def _same_origin(request: Request) -> bool:
    """CSRF guard: a mutating request's Origin/Referer must match this host.
    Absent headers fall through to the SameSite=Strict session cookie."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    return urlparse(origin).netloc == request.url.netloc


@app.middleware("http")
async def _security(request: Request, call_next):
    """Optional gate: when a password is set, require a session for the UI and a
    same-origin check on mutating requests. No password => fully open (as before)."""
    if not settings.auth_enabled:
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _same_origin(request):
            return PlainTextResponse("CSRF check failed", status_code=403)
        if not _is_authenticated(request):
            return PlainTextResponse("Forbidden", status_code=403)
        return await call_next(request)
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


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
    _stop_client(old)


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
    return templates.TemplateResponse(
        request, "index.html", {"rooms": rooms, "error": error, "hub_stale": hub_stale}
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
# from the network (the cached home page is only the offline fallback). The app
# is LAN-only, so this is about install ergonomics and fast loads, not full
# offline use. The cache name carries a hash of the static files, so changing
# any of them changes sw.js and browsers drop the old cache.
_STATIC_SHELL_FILES = ("styles.css", "htmx.min.js", "icon.svg")
_SERVICE_WORKER = """
const CACHE = 'hearth-%(version)s';
const STATIC = ['/static/styles.css', '/static/htmx.min.js', '/static/icon.svg',
                '/manifest.webmanifest'];
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
  } else {
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
def healthz(client: HearthClient = Depends(get_client)) -> JSONResponse:
    """Liveness/health for monitoring: poller alive and hub answering recently.
    An unconfigured app is reported as 503 by the NotConfigured handler above."""
    report = getattr(client, "health", None)
    if report is None:  # a client without health data is assumed fine
        return JSONResponse({"configured": True, "ok": True})
    data = report()
    ok = bool(data.get("poller_alive", True)) and not data.get("hub_stale", False)
    return JSONResponse({"configured": True, "ok": ok, **data}, status_code=200 if ok else 503)


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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": False})


@app.post("/login")
def login(request: Request, password: str = Form(...)) -> Response:
    expected = settings.auth_password
    if expected and hmac.compare_digest(password, expected):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            _SESSION_COOKIE, _session_token(), httponly=True, samesite="strict", path="/"
        )
        return resp
    return templates.TemplateResponse(request, "login.html", {"error": True}, status_code=401)


@app.post("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


# ---- settings / setup ---------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    rooms: list = []
    if settings.is_configured():
        try:
            rooms, _ = _safe_rooms(get_client())
        except NotConfigured:
            rooms = []
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "hub_ip": settings.hub_ip,
            "hub_port": settings.hub_port,
            "rooms": rooms,
            "auth_enabled": settings.auth_enabled,
            "hub_ready": settings.is_configured(),
            **_mqtt_view(),
        },
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
    host: str = Form(...),
    port: str = Form("1883"),  # str, then parsed: a stray value must not 422
    username: str = Form(""),
    password: str = Form(""),
    clear_password: str = Form(None),
    base_topic: str = Form("hearthmage"),
) -> RedirectResponse:
    host = host.strip()
    if not host:
        # Nothing to save; disabling is an explicit action (/settings/mqtt/disable).
        return RedirectResponse("/settings", status_code=303)
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        port_num = 1883
    if not 1 <= port_num <= 65535:
        port_num = 1883  # same fall-back-to-default style as hub_port
    # Blank password input keeps the stored one; the checkbox clears it.
    action = "clear" if clear_password is not None else ("set" if password else "keep")
    settings.set_mqtt(host, port_num, username, action, password, base_topic=base_topic)
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

    try:
        hubs = discover_local()
    except OSError:
        hubs = []  # no usable network interface; fall back to manual entry
    return templates.TemplateResponse(request, "_discover.html", {"hubs": hubs})


@app.post("/settings/hub")
def save_hub(hub_ip: str = Form(...), hub_port: int = Form(6653)) -> RedirectResponse:
    settings.set_hub(hub_ip, hub_port)
    _drop_client()  # force a rebuild against the new hub
    return RedirectResponse("/", status_code=303)


@app.post("/settings/names")
async def save_names(request: Request) -> RedirectResponse:
    form = await request.form()
    for key, value in form.items():
        if key.startswith("name_"):
            settings.set_zone_name(key[len("name_"):], str(value))
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


@app.post("/settings/import")
async def import_backup(request: Request, backup: UploadFile = File(...)) -> Response:
    """Restore a previously exported bundle, then reload the live stores."""
    global settings, schedules
    raw = await backup.read()
    try:
        bundle = json.loads(raw.decode("utf-8"))
        restore_bundle(bundle, settings.config_file, settings.schedules_path)
    except (ValueError, UnicodeDecodeError):
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "hub_ip": settings.hub_ip,
                "hub_port": settings.hub_port,
                "rooms": [],
                "import_error": "That file is not a valid HearthMage backup.",
            },
            status_code=400,
        )
    # Rebuild the in-memory views from the restored files and drop the client so
    # the next request rebuilds it against the (possibly new) hub. Reload from the
    # same path we wrote, not the default, so a relocated config file is honoured.
    settings = Settings(settings.config_file)
    schedules = ScheduleStore(settings.schedules_path)
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
            }
        )
    return templates.TemplateResponse(
        request, "history.html", {"zones": zones, "error": error}
    )


# ---- per-radiator config ------------------------------------------------


@app.get("/zones/{zone_id}/config", response_class=HTMLResponse)
def zone_config_page(
    request: Request, zone_id: str, client: HearthClient = Depends(get_client), saved: int = 0
) -> HTMLResponse:
    room = _find_room(client, zone_id)
    name = room.name if room else f"Zone {zone_id}"
    read = getattr(client, "read_config", None)
    try:
        config = read(zone_id) if read else None
    except HearthError:
        config = None
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "zone_id": zone_id,
            "zone_name": name,
            "config": config,
            "offset_max": 17,
            "saved": bool(saved),
        },
    )


@app.post("/zones/{zone_id}/config")
def save_zone_config(
    request: Request,
    zone_id: str,
    offset: int = Form(...),
    keypad_lock: str = Form(None),
    window_sensor: str = Form(None),
    client: HearthClient = Depends(get_client),
) -> Response:
    apply = getattr(client, "set_config", None)
    if apply is None:
        return RedirectResponse(f"/zones/{zone_id}/config", status_code=303)
    try:
        apply(
            zone_id,
            keypad_lock=keypad_lock is not None,
            window_sensor=window_sensor is not None,
            offset=offset,
        )
    except HearthError as exc:
        room = _find_room(client, zone_id)
        return templates.TemplateResponse(
            request,
            "config.html",
            {
                "zone_id": zone_id,
                "zone_name": room.name if room else f"Zone {zone_id}",
                "config": {
                    "keypad_lock": keypad_lock is not None,
                    "window_sensor": window_sensor is not None,
                    "offset": offset,
                },
                "offset_max": 17,
                "error": str(exc),
            },
            status_code=502,
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
    price = settings.price_per_kwh
    zones = [_zone_energy_view(r, price) for r in rooms]
    home_today = sum(z["today_kwh"] for z in zones if z["has_data"])
    home_week = sum(z["week_kwh"] for z in zones if z["has_data"])
    # Kick off a background refresh so the next visit reflects awake radiators.
    if rooms:
        threading.Thread(
            target=_refresh_energy,
            args=(client, [r.id for r in rooms]),
            name="energy-refresh",
            daemon=True,
        ).start()
    return templates.TemplateResponse(
        request,
        "energy.html",
        {
            "zones": zones,
            "error": error,
            "price": price,
            "home_today_kwh": home_today,
            "home_week_kwh": home_week,
            "home_today_cost": cost(home_today, price),
            "home_week_cost": cost(home_week, price),
        },
    )


@app.post("/settings/price")
def save_price(price_per_kwh: float = Form(...)) -> RedirectResponse:
    settings.set_price(price_per_kwh)
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
                ok = False
        if ok:
            ok = _verify_week(client, zone, stored)
    toggle = getattr(client, "set_program_active", None)
    active = schedules.get_active(zone)
    if toggle is not None and active is not None:
        try:
            toggle(zone, active)
        except HearthError as exc:
            _sched_log.warning("zone %s program on/off push failed: %s", zone, exc)
            ok = False
    return ok


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
            with _sync_lock:
                if _sync_gen.get(zone, 0) == gen:  # nothing changed while pushing
                    _sync_running.discard(zone)
                    sync.set(zone, "synced" if ok else "failed")
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
        sync.set(zone, "pending")
        if zone in _sync_running:
            return
        _sync_running.add(zone)
    threading.Thread(
        target=_sync_until_settled, args=(client, zone), name="schedule-sync", daemon=True
    ).start()


def _retry_failed_syncs(client: HearthClient) -> None:
    """Push every zone not yet confirmed on the hub (failed, or queued as
    pending), skipping any a worker is already pushing."""
    for zone in sync.unsettled_zones():
        with _sync_lock:
            if zone in _sync_running:
                continue
            _sync_running.add(zone)
        _sync_until_settled(client, zone)


def _retry_loop() -> None:
    while True:
        _retry_wake.wait(60)
        _retry_wake.clear()
        try:
            # Resolve the client each cycle so retries follow a hub change.
            _retry_failed_syncs(get_client())
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
        {"zone_id": zone_id, "sync": sync.get(zone_id)},
    )


# ---- holiday hold -------------------------------------------------------


def _current_holiday(zone_id: str) -> dict | None:
    """The zone's holiday record, or None once its end date has passed (the hub
    resumes on its own then, so the record would only mislead)."""
    record = holidays.get(zone_id)
    if record and record.get("year"):
        try:
            ended = date(record["year"], record["month"], record["day"]) < date.today()
        except (TypeError, ValueError):
            ended = True
        if ended:
            holidays.clear(zone_id)
            return None
    return record


@app.post("/zones/{zone_id}/holiday")
def start_holiday(
    zone_id: str,
    until: str = Form(...),
    temp: float = Form(7.0),
    client: HearthClient = Depends(get_client),
) -> Response:
    _check_zone(zone_id)
    setter = getattr(client, "set_holiday", None)
    try:
        until_date = date.fromisoformat(until)
    except ValueError:
        return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)
    year, month, day = until_date.year, until_date.month, until_date.day
    if setter is not None:
        try:
            setter(zone_id, day=day, month=month, temp=temp)
        except HearthError:
            return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)
    # Remember the setpoint to restore if the hold is cancelled early.
    room = _find_room(client, zone_id)
    prev = room.target_temp if (room and not room.is_off) else None
    holidays.set(zone_id, day=day, month=month, temp=temp, prev_setpoint=prev, year=year)
    return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)


@app.post("/zones/{zone_id}/holiday/cancel")
def cancel_holiday(zone_id: str, client: HearthClient = Depends(get_client)) -> Response:
    _check_zone(zone_id)
    clearer = getattr(client, "clear_holiday", None)
    if clearer is not None:
        try:
            clearer(zone_id)
            # Cancelling leaves the zone at the frost temp; restore what it held.
            record = holidays.get(zone_id)
            prev = record.get("prev_setpoint") if record else None
            if prev is not None:
                client.set_temperature(zone_id, prev)
        except HearthError:
            pass
    holidays.clear(zone_id)
    return RedirectResponse(f"/zones/{zone_id}/schedule", status_code=303)
