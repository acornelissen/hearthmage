import json

import pytest
from fastapi.testclient import TestClient

from hearthmage.app import app, get_client
from hearthmage.domain import HearthError, Room
from hearthmage.fake_client import FakeHearthClient
from hearthmage.schedule import Block


@pytest.fixture()
def client() -> TestClient:
    fake = FakeHearthClient(
        [Room("1", "Living Room", 19.5, 21.0, is_off=False, min_temp=5.0, max_temp=30.0)]
    )
    app.dependency_overrides[get_client] = lambda: fake
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_index_lists_rooms(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Living Room" in resp.text
    assert "21" in resp.text  # target in the readout / input


def test_zone_status_returns_readout(client: TestClient):
    resp = client.get("/rooms/1/status")
    assert resp.status_code == 200
    assert 'id="readout-1"' in resp.text
    assert "Target 21" in resp.text


def test_zone_status_unknown_zone_does_not_crash(client: TestClient):
    resp = client.get("/rooms/99/status")
    assert resp.status_code == 200
    assert 'id="readout-99"' in resp.text


def test_set_temperature_returns_updated_readout(client: TestClient):
    resp = client.post("/rooms/1/temperature", data={"temperature": "22"})
    assert resp.status_code == 200
    assert 'id="readout-1"' in resp.text
    assert "Target 22" in resp.text


def test_set_temperature_off_shows_off(client: TestClient):
    resp = client.post("/rooms/1/temperature", data={"temperature": "0"})
    assert resp.status_code == 200
    assert "Off" in resp.text
    assert "Target" not in resp.text  # off, so no target line


def test_set_temperature_half_degree_shows_one_decimal(client: TestClient):
    resp = client.post("/rooms/1/temperature", data={"temperature": "20.5"})
    assert resp.status_code == 200
    assert "Target 20.5" in resp.text


def test_readout_carries_the_hub_target_for_the_target_box(client: TestClient):
    # The page script copies this into the target box so the box follows the hub.
    resp = client.get("/rooms/1/status")
    assert 'data-target="21.0"' in resp.text


def test_readout_target_is_empty_when_the_zone_is_off(client: TestClient):
    resp = client.post("/rooms/1/temperature", data={"temperature": "0"})
    assert 'data-target=""' in resp.text


def test_readout_has_no_target_when_the_zone_is_unknown(client: TestClient):
    # No hub value to show, so the target box must be left alone.
    resp = client.get("/rooms/99/status")
    assert "data-target" not in resp.text


def test_target_box_reads_off_when_the_zone_is_off(client: TestClient):
    client.post("/rooms/1/temperature", data={"temperature": "0"})
    resp = client.get("/")
    box = resp.text.split('class="setpoint-input"')[1].split(">")[0]
    assert 'placeholder="Off"' in box
    assert 'value=""' in box


def test_off_button_follows_the_hub_like_the_target_box(client: TestClient):
    # The Off button is rendered disabled when the zone is off. It sits outside
    # the polled readout, so the page script must keep it in step, or a zone
    # that was off at page load could never be turned off again without a reload.
    client.post("/rooms/1/temperature", data={"temperature": "0"})
    page = client.get("/").text
    button = page.split('class="off"')[1].split(">")[0]
    assert "disabled" in button  # server-rendered state when off
    script = page.split("Keep each target box and Off button")[1].split("</script>")[0]
    assert "'.setpoint-off button'" in script
    assert "off.disabled = hub === ''" in script


@pytest.mark.parametrize("silent_for, text", [
    (2700.0, "over 45 min"),
    (3 * 3600.0, "over 3 h"),
])
def test_readout_says_when_the_radiator_has_stopped_answering(silent_for, text):
    fake = FakeHearthClient([
        Room("1", "Lounge", 15.8, 10.0, is_off=False, min_temp=5.0, max_temp=30.0,
             silent_for=silent_for)
    ])
    app.dependency_overrides[get_client] = lambda: fake
    try:
        page = TestClient(app).get("/rooms/1/status").text
    finally:
        app.dependency_overrides.clear()
    assert "hasn&rsquo;t answered for " + text in page
    assert "Showing the last reading" in page
    assert "No answer" in page  # the state chip must not claim Idle/Heating
    assert "Idle" not in page and "At target" not in page


def test_readout_says_when_a_target_is_not_confirmed_yet():
    fake = FakeHearthClient([
        Room("1", "Lounge", 15.8, 21.0, is_off=False, min_temp=5.0, max_temp=30.0,
             unconfirmed=True)
    ])
    app.dependency_overrides[get_client] = lambda: fake
    try:
        page = TestClient(app).get("/rooms/1/status").text
    finally:
        app.dependency_overrides.clear()
    assert "Target 21.0" in page and 'data-target="21.0"' in page  # the wanted target stays
    assert "hasn&rsquo;t confirmed" in page and "Still trying" in page
    assert "Not confirmed" in page  # the chip must not claim Heating
    assert "Heating" not in page


def test_readout_has_no_silence_note_for_a_radiator_that_answers(client: TestClient):
    page = client.get("/rooms/1/status").text
    assert "answered" not in page and "No answer" not in page


def test_setpoint_request_is_logged_with_value_and_client(client: TestClient, caplog):
    with caplog.at_level("INFO", logger="hearthmage.setpoint"):
        client.post("/rooms/1/temperature", data={"temperature": "22"})
    messages = [r.getMessage() for r in caplog.records if r.name == "hearthmage.setpoint"]
    assert messages == ["zone '1' target 22.0 requested by testclient"]


def test_set_temperature_error_shows_message(client: TestClient):
    resp = client.post("/rooms/99/temperature", data={"temperature": "22"})
    assert resp.status_code == 200
    assert 'role="alert"' in resp.text


def test_index_survives_unreachable_hub():
    class BoomClient:
        def list_rooms(self):
            raise HearthError("hub unreachable")

        def set_temperature(self, room_id, temperature):  # pragma: no cover
            raise HearthError("hub unreachable")

        def set_scene_membership(self, room_id, scene, member):  # pragma: no cover
            raise HearthError("hub unreachable")

    app.dependency_overrides[get_client] = lambda: BoomClient()
    try:
        resp = TestClient(app).get("/")
        assert resp.status_code == 200  # graceful, not a 500
        assert 'role="alert"' in resp.text
    finally:
        app.dependency_overrides.clear()


def _unconfigured(tmp_path, monkeypatch):
    import hearthmage.app as appmod
    from hearthmage.settings import Settings

    monkeypatch.setattr(appmod, "settings", Settings(str(tmp_path / "c.json"), env={}))
    monkeypatch.setattr(appmod, "_client", None)
    monkeypatch.setattr(appmod, "_client_hub", None)
    return appmod


def test_setup_redirect_when_unconfigured(tmp_path, monkeypatch):
    _unconfigured(tmp_path, monkeypatch)
    resp = TestClient(app).get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"


def test_settings_page_renders_setup_form(tmp_path, monkeypatch):
    _unconfigured(tmp_path, monkeypatch)
    resp = TestClient(app).get("/settings")
    assert resp.status_code == 200
    assert "Hub IP" in resp.text


def test_discover_lists_found_hubs(tmp_path, monkeypatch):
    _unconfigured(tmp_path, monkeypatch)
    import hearthmage.discovery as disc

    monkeypatch.setattr(disc, "discover_local", lambda *a, **k: [{"ip": "10.0.0.20", "idu": 15792}])
    resp = TestClient(app).get("/settings/discover")
    assert resp.status_code == 200
    assert "10.0.0.20" in resp.text
    assert "15792" in resp.text
    assert "Use this hub" in resp.text


def test_discover_handles_no_hub_found(tmp_path, monkeypatch):
    _unconfigured(tmp_path, monkeypatch)
    import hearthmage.discovery as disc

    monkeypatch.setattr(disc, "discover_local", lambda *a, **k: [])
    resp = TestClient(app).get("/settings/discover")
    assert resp.status_code == 200
    assert "No hub found" in resp.text


def test_save_hub_persists_and_redirects_home(tmp_path, monkeypatch):
    appmod = _unconfigured(tmp_path, monkeypatch)
    resp = TestClient(app).post(
        "/settings/hub",
        data={"hub_ip": "10.0.0.20", "hub_port": "6653"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert appmod.settings.hub_ip == "10.0.0.20"


# ---- auth -----------------------------------------------------------------


def _with_auth(monkeypatch, tmp_path, password="swordfish"):
    import hearthmage.app as appmod
    from hearthmage.settings import Settings

    s = Settings(str(tmp_path / "c.json"), env={"HEARTHMAGE_PASSWORD": password,
                                                "HEARTHMAGE_SECRET_KEY": "testkey"})
    monkeypatch.setattr(appmod, "settings", s)
    return appmod


def test_auth_off_by_default_allows_access(client: TestClient):
    # The default module settings have no password, so the app stays open.
    assert client.get("/").status_code == 200


def test_protected_get_redirects_to_login(tmp_path, monkeypatch):
    _with_auth(monkeypatch, tmp_path)
    resp = TestClient(app).get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_sets_session_and_grants_access(tmp_path, monkeypatch):
    appmod = _with_auth(monkeypatch, tmp_path)
    appmod.app.dependency_overrides[get_client] = lambda: FakeHearthClient(
        [Room("1", "Living Room", 19.5, 21.0, is_off=False, min_temp=5.0, max_temp=30.0)]
    )
    try:
        c = TestClient(app)
        bad = c.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert bad.status_code == 401
        ok = c.post("/login", data={"password": "swordfish"}, follow_redirects=False)
        assert ok.status_code == 303
        # the session cookie now lets us in
        assert c.get("/").status_code == 200
    finally:
        appmod.app.dependency_overrides.clear()


def test_mutating_post_requires_session(tmp_path, monkeypatch):
    _with_auth(monkeypatch, tmp_path)
    resp = TestClient(app).post(
        "/rooms/1/temperature", data={"temperature": "20"}, follow_redirects=False
    )
    assert resp.status_code == 403  # no session -> forbidden, not a redirect


def test_cross_origin_post_is_rejected(tmp_path, monkeypatch):
    appmod = _with_auth(monkeypatch, tmp_path)
    c = TestClient(app)
    c.post("/login", data={"password": "swordfish"})  # obtain a session
    resp = c.post(
        "/rooms/1/temperature",
        data={"temperature": "20"},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert resp.status_code == 403  # CSRF: origin mismatch
    _ = appmod


def test_cross_origin_post_is_rejected_without_a_password(client: TestClient):
    # No password keeps the app open on the LAN, but a web page on another
    # site must not be able to drive it through a visitor's browser.
    resp = client.post(
        "/rooms/1/temperature",
        data={"temperature": "30"},
        headers={"origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert "Target 21" in client.get("/rooms/1/status").text  # nothing was changed


def test_same_origin_and_headerless_posts_still_work_without_a_password(client: TestClient):
    same = client.post(
        "/rooms/1/temperature",
        data={"temperature": "22"},
        headers={"origin": "http://testserver"},
    )
    assert same.status_code == 200
    # curl and scripts send no Origin or Referer; they keep working.
    assert client.post("/rooms/1/temperature", data={"temperature": "23"}).status_code == 200


def test_cross_origin_login_post_is_rejected(tmp_path, monkeypatch):
    _with_auth(monkeypatch, tmp_path)
    resp = TestClient(app).post(
        "/login",
        data={"password": "swordfish"},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_non_ascii_password_can_sign_in(tmp_path, monkeypatch):
    _with_auth(monkeypatch, tmp_path, password="geheim-p\u00e4ssword")
    c = TestClient(app)
    wrong = c.post("/login", data={"password": "geheim-password"}, follow_redirects=False)
    assert wrong.status_code == 401
    ok = c.post("/login", data={"password": "geheim-p\u00e4ssword"}, follow_redirects=False)
    assert ok.status_code == 303


def test_non_ascii_guess_is_a_plain_wrong_password(tmp_path, monkeypatch):
    _with_auth(monkeypatch, tmp_path)
    resp = TestClient(app).post("/login", data={"password": "sw\u00f6rdfish"}, follow_redirects=False)
    assert resp.status_code == 401


def test_changing_the_password_signs_everyone_out(tmp_path, monkeypatch):
    _with_auth(monkeypatch, tmp_path)
    c = TestClient(app)
    c.post("/login", data={"password": "swordfish"})
    # Signed in: no hub is configured here, so the home page sends us to setup.
    assert c.get("/", follow_redirects=False).headers["location"] == "/settings"
    # The owner changes the password; the secret key stays the same.
    _with_auth(monkeypatch, tmp_path, password="new-password")
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    denied = c.post("/rooms/1/temperature", data={"temperature": "20"}, follow_redirects=False)
    assert denied.status_code == 403


def test_healthz_stays_public_under_auth(tmp_path, monkeypatch):
    _with_auth(monkeypatch, tmp_path)
    # unconfigured hub -> 503, but crucially not a redirect to /login
    assert TestClient(app).get("/healthz").status_code in (200, 503)


# ---- PWA ------------------------------------------------------------------


def test_manifest_is_served(client: TestClient):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert "manifest" in resp.headers["content-type"]
    data = resp.json()
    assert data["start_url"] == "/"
    assert data["display"] == "standalone"
    assert data["icons"]  # at least one icon


def test_service_worker_is_served_at_root(client: TestClient):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "caches" in resp.text  # it's a real service worker


def test_service_worker_cache_name_follows_static_files(monkeypatch):
    """A stylesheet change must change sw.js, or installed clients keep the old CSS."""
    import hearthmage.app as appmod

    before = appmod._service_worker_source()
    monkeypatch.setattr(appmod, "_STATIC_SHELL_FILES", ())  # stands in for changed assets
    assert appmod._service_worker_source() != before


def test_service_worker_fetches_pages_from_the_network_first(client: TestClient):
    text = client.get("/sw.js").text
    assert "'/'" not in text.split("const STATIC")[1].split(";")[0]  # home page not cache-first
    assert "fetch(req).catch(() => caches.match('/'))" in text  # offline fallback kept


def test_service_worker_offline_fallback_is_for_page_loads_only(client: TestClient):
    """A failed htmx poll must fail, not come back as the cached home page: htmx
    would swap the whole page into a readout, and every copy would poll again."""
    text = client.get("/sw.js").text
    handler = text.split("addEventListener('fetch'")[1]
    assert "} else if (req.mode === 'navigate') {" in handler
    fallback = handler.split("req.mode === 'navigate'")[1]
    assert "caches.match('/')" in fallback  # the fallback sits inside the navigate branch...
    assert "caches.match('/')" not in handler.split("req.mode === 'navigate'")[0]  # ...and nowhere else
    assert "} else {" not in handler  # no catch-all branch for other requests


def test_index_links_manifest_and_registers_sw(client: TestClient):
    resp = client.get("/")
    assert 'rel="manifest"' in resp.text
    assert "serviceWorker" in resp.text


# ---- history -------------------------------------------------------------


def _with_history(tmp_path, monkeypatch):
    import hearthmage.app as appmod
    from hearthmage.history import HistoryStore

    monkeypatch.setattr(appmod, "history", HistoryStore(str(tmp_path / "history.db")))
    return appmod


def test_history_page_renders_sparkline(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_history(tmp_path, monkeypatch)
    for i in range(5):
        appmod.history.record_temp("1", 19.0 + i * 0.2, 21.0, ts=100.0 + i)
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "Living Room" in resp.text
    assert "<polyline" in resp.text  # a sparkline was drawn
    assert "5 readings" in resp.text


def test_history_page_without_data_shows_placeholder(client: TestClient, tmp_path, monkeypatch):
    _with_history(tmp_path, monkeypatch)
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "Not enough readings yet" in resp.text


def test_record_reading_hook_writes_to_history(tmp_path, monkeypatch):
    appmod = _with_history(tmp_path, monkeypatch)
    appmod._record_reading(1, 20.4, 21.0)
    assert len(appmod.history.temp_series("1")) == 1


def _with_waste(tmp_path, monkeypatch, hub_client=None):
    from hearthmage.waste import WasteDetector

    appmod = _with_history(tmp_path, monkeypatch)
    monkeypatch.setattr(appmod, "waste", WasteDetector())
    monkeypatch.setattr(appmod, "_client", hub_client or FakeHearthClient(
        [Room("1", "Living Room", 13.0, 5.0, is_off=False, min_temp=5.0, max_temp=30.0)]))
    return appmod


def _join_status_captures() -> None:
    import threading

    for t in threading.enumerate():
        if t.name == "status-capture":
            t.join(timeout=5)


_DAY = 1_793_000_000.0  # a fixed timestamp (late October 2026)


def _fast_rise(appmod, zone="1", start=_DAY):
    for minute, temp in [(0, 13.0), (5, 13.0), (10, 13.3), (15, 13.6), (20, 14.0)]:
        appmod._check_waste(zone, start + minute * 60, temp, 5.0)


def test_heating_with_a_low_target_is_recorded_with_radiator_status(tmp_path, monkeypatch):
    appmod = _with_waste(tmp_path, monkeypatch)
    _fast_rise(appmod)
    _join_status_captures()
    [event] = appmod.history.waste_events("1")
    assert (event["start_temp"], event["peak_temp"], event["setpoint"]) == (13.0, 14.0, 5.0)
    snapshot = appmod.history.status_for_event(event["id"])
    assert snapshot["kind"] == "heating" and snapshot["raw"].startswith("OK,")
    assert appmod.history.latest_status("1", "rest") is not None  # baseline before it


def test_waste_check_never_breaks_the_reading_hook(tmp_path, monkeypatch):
    appmod = _with_waste(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(appmod.waste, "feed", boom)
    appmod._record_reading(1, 20.4, 21.0)  # does not raise
    assert len(appmod.history.temp_series("1")) == 1


def test_status_capture_failure_is_tolerated(tmp_path, monkeypatch):
    class Asleep(FakeHearthClient):
        def read_status_raw(self, room_id):
            raise HearthError("asleep")

    appmod = _with_waste(tmp_path, monkeypatch, Asleep(
        [Room("1", "Living Room", 13.0, 5.0, is_off=False, min_temp=5.0, max_temp=30.0)]))
    _fast_rise(appmod)
    _join_status_captures()
    assert len(appmod.history.waste_events("1")) == 1  # the event is kept anyway


def test_history_page_lists_waste_events_and_status_changes(client, tmp_path, monkeypatch):
    appmod = _with_waste(tmp_path, monkeypatch)
    now = appmod.datetime.now(appmod.timezone.utc).timestamp()
    appmod.history.record_status("1", now - 7200, "rest",
                                 "OK,13,0,5,21,19,17,6,11,69,0,0,0,0,1,0,44,244,0,33,v200110")
    event_id = appmod.history.start_waste_event("1", now - 3600, 13.3, 5.0, now - 2640, 14.5)
    appmod.history.update_waste_event(event_id, now - 2640, 14.5, open_=False)
    appmod.history.record_status("1", now - 3500, "heating",
                                 "OK,13,5,5,3,13,40,6,11,72,0,1,0,0,1,0,44,244,0,33,v200110",
                                 event_id=event_id)
    page = client.get("/history").text
    assert "Heating nobody asked for" in page
    assert "16 min" in page and "+1.2" in page and "target 5" in page
    assert "Field 9" in page and "69" in page and "72" in page  # changed while heating
    assert "Clock" not in page  # the clock always moves; not listed


def test_history_page_says_when_nothing_was_wasted(client, tmp_path, monkeypatch):
    _with_waste(tmp_path, monkeypatch)
    assert "No heating without being asked in the last 30 days" in client.get("/history").text


def test_home_page_flags_recent_waste(client, tmp_path, monkeypatch):
    appmod = _with_waste(tmp_path, monkeypatch)
    assert "heated without being asked" not in client.get("/").text
    now = appmod.datetime.now(appmod.timezone.utc).timestamp()
    appmod.history.start_waste_event("1", now - 600, 13.0, 5.0, now - 60, 13.8)
    page = client.get("/").text
    assert "Living Room heated without being asked" in page
    assert 'href="/history#waste-1"' in page


# ---- per-radiator config -------------------------------------------------


def test_config_page_shows_current_settings(client: TestClient):
    resp = client.get("/zones/1/config")
    assert resp.status_code == 200
    assert "Living Room" in resp.text
    assert "Lock the keypad" in resp.text
    assert "Anti-ice temperature" in resp.text
    assert "Sensor offset" in resp.text
    assert "offset calibration" not in resp.text  # 1#20 is not an offset


class _ConfigSpy(FakeHearthClient):
    """Fake that records what the settings form asked to write."""

    def __init__(self, config: dict) -> None:
        super().__init__(
            [Room("1", "Living Room", 19.5, 21.0, is_off=False, min_temp=5.0, max_temp=30.0)]
        )
        self._config["1"] = dict(config)
        self.writes: list[dict] = []

    def set_config(self, room_id, **kwargs) -> None:
        self.writes.append(kwargs)
        super().set_config(room_id, **kwargs)


def _config_client(config: dict):
    spy = _ConfigSpy(config)
    app.dependency_overrides[get_client] = lambda: spy
    return spy, TestClient(app)


_READ = {"keypad_lock": False, "window_sensor": True, "frost_temp": 5, "sensor_offset": 0.7}
# What the page posts back when nothing is touched: each value plus what was read.
_UNTOUCHED = {
    "window_sensor": "1", "frost_temp": "5", "sensor_offset": "0.7",
    "orig_keypad_lock": "0", "orig_window_sensor": "1",
    "orig_frost_temp": "5", "orig_sensor_offset": "0.7",
}


def test_save_config_writes_only_what_changed(client: TestClient):
    # Behaviour change (heatapp-local-xns): the form used to send all four
    # settings on every save; it now sends only the ones the user changed.
    spy, c = _config_client(_READ)
    resp = c.post(
        "/zones/1/config", data={**_UNTOUCHED, "keypad_lock": "1", "frost_temp": "8"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/zones/1/config?saved=1"
    assert spy.writes == [{"keypad_lock": True, "frost_temp": 8}]
    assert spy.read_config("1") == {**_READ, "keypad_lock": True, "frost_temp": 8}


def test_save_config_with_no_change_sends_nothing(client: TestClient):
    spy, c = _config_client(_READ)
    resp = c.post("/zones/1/config", data=_UNTOUCHED, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/zones/1/config?unchanged=1"
    assert spy.writes == []
    assert "Nothing changed" in c.get("/zones/1/config?unchanged=1").text


def test_config_page_marks_an_unread_setting_and_never_writes_it(client: TestClient):
    # Window, anti-ice and offset could not be read. They must show as unknown,
    # not as off/7/0, and a save of the keypad lock must leave them alone.
    spy, c = _config_client(
        {"keypad_lock": False, "window_sensor": None, "frost_temp": None, "sensor_offset": None}
    )
    page = c.get("/zones/1/config").text
    assert page.count("couldn&rsquo;t be read") == 3
    for name in ("window_sensor", "frost_temp", "sensor_offset"):
        field = page.split(f'name="{name}"')[1].split(">")[0]
        assert "disabled" in field
    assert 'name="frost_temp"' in page and 'value="7"' not in page
    # A crafted or stale post that still carries values for the unread settings:
    resp = c.post(
        "/zones/1/config",
        data={"keypad_lock": "1", "frost_temp": "7", "sensor_offset": "0",
              "orig_keypad_lock": "0", "orig_window_sensor": "",
              "orig_frost_temp": "", "orig_sensor_offset": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert spy.writes == [{"keypad_lock": True}]


@pytest.mark.parametrize("data", [
    {"frost_temp": "99", "sensor_offset": "0"},
    {"frost_temp": "7", "sensor_offset": "12"},
    {"frost_temp": "x", "sensor_offset": "0"},
    {"frost_temp": "7", "sensor_offset": "nan"},
])
def test_save_config_rejects_bad_values(client: TestClient, data):
    resp = client.post("/zones/1/config", data=data, follow_redirects=False)
    assert resp.status_code == 400
    fake = app.dependency_overrides[get_client]()
    assert fake.read_config("1")["frost_temp"] == 7  # nothing written


# ---- energy --------------------------------------------------------------


def _with_energy(tmp_path, monkeypatch):
    import hearthmage.app as appmod
    from hearthmage.energy_store import EnergyStore
    from hearthmage.settings import Settings

    monkeypatch.setattr(appmod, "energy", EnergyStore(str(tmp_path / "energy.json")))
    monkeypatch.setattr(appmod, "settings", Settings(str(tmp_path / "c.json"), env={}))
    return appmod


def test_energy_page_renders_cached_zone(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_energy(tmp_path, monkeypatch)
    appmod.energy.set_zone("1", 1000, [10, 5, 0, 0, 0, 0, 0, 0], [30], "2026-07-19T10:00:00+00:00")
    resp = client.get("/energy")
    assert resp.status_code == 200
    assert "Living Room" in resp.text
    assert "1.0" in resp.text  # today's 10 counters * 0.1h * 1kW = 1.0 kWh
    assert "Whole home" in resp.text


def test_energy_page_without_reading_shows_placeholder(client: TestClient, tmp_path, monkeypatch):
    _with_energy(tmp_path, monkeypatch)
    resp = client.get("/energy")
    assert resp.status_code == 200
    assert "No energy reading yet" in resp.text


def test_save_price_persists_and_redirects(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_energy(tmp_path, monkeypatch)
    resp = client.post("/settings/price", data={"price_per_kwh": "0.29"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/energy"
    assert appmod.settings.price_per_kwh == 0.29


# ---- health / observability ---------------------------------------------


def test_healthz_ok_with_fake_client(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["configured"] is True


def test_healthz_unconfigured_returns_503(tmp_path, monkeypatch):
    _unconfigured(tmp_path, monkeypatch)
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["configured"] is False


def test_healthz_503_when_hub_stale():
    class StaleClient:
        def list_rooms(self):
            return []

        def health(self):
            return {"poller_alive": True, "hub_stale": True, "zones": {}}

    app.dependency_overrides[get_client] = lambda: StaleClient()
    try:
        resp = TestClient(app).get("/healthz")
        assert resp.status_code == 503
        assert resp.json()["ok"] is False
    finally:
        app.dependency_overrides.clear()


def test_index_shows_stale_banner():
    class StaleClient:
        def list_rooms(self):
            return []

        def hub_stale(self):
            return True

    app.dependency_overrides[get_client] = lambda: StaleClient()
    try:
        resp = TestClient(app).get("/")
        assert resp.status_code == 200
        assert "hasn" in resp.text and "hub" in resp.text  # stale banner text present
    finally:
        app.dependency_overrides.clear()


# ---- backup / restore ---------------------------------------------------


def _configured(tmp_path, monkeypatch):
    import hearthmage.app as appmod
    from hearthmage.schedule import Block
    from hearthmage.schedule_store import ScheduleStore
    from hearthmage.settings import Settings

    s = Settings(str(tmp_path / "config.json"), env={})
    s.set_hub("10.0.0.20", 6653)
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_pattern("1", [Block(20, 0, 600)], [0])
    monkeypatch.setattr(appmod, "settings", s)
    monkeypatch.setattr(appmod, "schedules", store)
    monkeypatch.setattr(appmod, "_client", None)
    monkeypatch.setattr(appmod, "_client_hub", None)
    return appmod


def test_export_returns_bundle(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)
    resp = TestClient(app).get("/settings/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert "attachment" in resp.headers["content-disposition"]
    bundle = resp.json()
    assert bundle["config"]["hub_ip"] == "10.0.0.20"
    assert bundle["schedules"]["1"]["0"] == [[20, 0, 600]]


def test_import_restores_and_reloads(tmp_path, monkeypatch):
    appmod = _configured(tmp_path, monkeypatch)
    bundle = {
        "hearthmage_backup": 1,
        "config": {"hub_ip": "10.9.9.9", "hub_port": 6653},
        "schedules": {"2": {"3": [[18, 60, 120]]}},
    }
    resp = TestClient(app).post(
        "/settings/import",
        files={"backup": ("hearth-backup.json", json.dumps(bundle), "application/json")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
    assert appmod.settings.hub_ip == "10.9.9.9"  # live store reloaded
    assert appmod.schedules.get_day("2", 3)[0].target == 18


def test_import_rejects_foreign_file(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)
    resp = TestClient(app).post(
        "/settings/import",
        files={"backup": ("junk.json", '{"not":"a bundle"}', "application/json")},
    )
    assert resp.status_code == 400
    assert "not a valid HearthMage backup" in resp.text


# ---- schedule -----------------------------------------------------------


def _with_store(tmp_path, monkeypatch):
    import hearthmage.app as appmod
    from hearthmage.schedule_store import ScheduleStore
    from hearthmage.sync_store import SyncStore

    monkeypatch.setattr(appmod, "schedules", ScheduleStore(str(tmp_path / "sched.json")))
    monkeypatch.setattr(appmod, "sync", SyncStore(str(tmp_path / "sync.json")))
    monkeypatch.setattr(appmod, "_sync_backoff", {}, raising=False)  # no waits carried between tests
    return appmod


def test_schedule_page_renders(client: TestClient, tmp_path, monkeypatch):
    _with_store(tmp_path, monkeypatch)
    resp = client.get("/zones/1/schedule")
    assert resp.status_code == 200
    assert "Living Room" in resp.text
    assert "Monday" in resp.text and "Sunday" in resp.text


def test_toggle_program_records_active_state(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    client.post("/zones/1/program", data={"active": "1"}, follow_redirects=False)
    assert appmod.schedules.get_active("1") is True
    client.post("/zones/1/program", data={"active": "0"}, follow_redirects=False)
    assert appmod.schedules.get_active("1") is False


def test_save_schedule_stores_and_redirects(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    resp = client.post(
        "/zones/1/schedule",
        data={
            "days": ["0", "2"],
            "block_target": ["20", "16"],
            "block_start": ["07:00", "09:00"],
            "block_end": ["09:00", "22:00"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/zones/1/schedule"
    day0 = appmod.schedules.get_day("1", 0)
    assert [(b.target, b.start, b.end) for b in day0] == [(20, 420, 540), (16, 540, 1320)]
    assert appmod.schedules.get_day("1", 2) == day0  # applied to both selected days


def test_clear_day_removes_from_store(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [3])
    resp = client.post("/zones/1/schedule/clear", data={"day": "3"}, follow_redirects=False)
    assert resp.status_code == 303
    assert appmod.schedules.get_day("1", 3) == []


def test_activate_controls_only_with_saved_schedule(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    resp = client.get("/zones/1/schedule")
    assert "Deactivate" not in resp.text  # nothing to activate yet
    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0])
    resp2 = client.get("/zones/1/schedule")
    assert "Activate" in resp2.text and "Deactivate" in resp2.text


def test_source_of_truth_note_only_without_saved_schedule(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    assert "source of truth" in client.get("/zones/1/schedule").text  # empty: note shown
    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0])
    assert "source of truth" not in client.get("/zones/1/schedule").text  # set: note hidden


def test_retry_resyncs_failed_zone_from_stored_schedule(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    # A zone was left failed with a stored schedule and program state.
    appmod.schedules.set_pattern("1", [Block(20, 420, 540)], [0, 1])
    appmod.schedules.set_active("1", True)
    appmod.sync.set("1", "failed")

    fake = FakeHearthClient(
        [Room("1", "Living Room", 19.0, 20.0, is_off=False, min_temp=5.0, max_temp=30.0)]
    )
    appmod._retry_failed_syncs(fake)

    assert appmod.sync.get("1") == "synced"  # retry cleared the failure
    assert fake.applied  # the stored pattern was re-pushed
    assert fake.program_active["1"] is True  # program state re-applied


def _wait_for_sync() -> None:
    """Join the background schedule-sync workers a route started."""
    import threading

    for t in threading.enumerate():
        if t.name == "schedule-sync":
            t.join(timeout=5)


def _room_fake() -> FakeHearthClient:
    return FakeHearthClient(
        [Room("1", "Living Room", 19.0, 20.0, is_off=False, min_temp=5.0, max_temp=30.0)]
    )


def test_save_schedule_rejects_malformed_rows_with_an_error(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    resp = client.post(
        "/zones/1/schedule",
        data={
            "days": ["1"],
            "block_target": ["20", "18"],
            "block_start": ["07:00", "10:00"],
            "block_end": ["09:00", "08:00"],  # second row ends before it starts
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "Ends before it starts" in resp.text
    assert 'aria-invalid="true"' in resp.text
    assert 'value="10:00"' in resp.text  # what the user typed is kept
    assert appmod.schedules.get_zone("1") == {}  # nothing saved


@pytest.mark.parametrize(
    "data, message",
    [
        ({"block_target": "20", "block_start": "07:00", "block_end": "09:00"}, "Choose at least one day"),
        ({"days": "0"}, "Add at least one time frame"),
        ({"days": "0", "block_target": "200", "block_start": "07:00", "block_end": "08:00"}, "whole number from 5 to 30"),
        ({"days": "0", "block_target": "nan", "block_start": "07:00", "block_end": "08:00"}, "whole number from 5 to 30"),
        ({"days": "0", "block_target": "inf", "block_start": "07:00", "block_end": "08:00"}, "whole number from 5 to 30"),
        ({"days": "0", "block_target": "20.5", "block_start": "07:00", "block_end": "08:00"}, "whole number from 5 to 30"),
        ({"days": "9", "block_target": "20", "block_start": "07:00", "block_end": "08:00"}, "Unknown day"),
        ({"days": "²", "block_target": "20", "block_start": "07:00", "block_end": "08:00"}, "Unknown day"),
        ({"days": "0", "block_target": "20", "block_start": "07:00", "block_end": ""}, "Fill in the temperature"),
        ({"days": "0", "block_target": "20", "block_start": "7am", "block_end": "08:00"}, "HH:MM"),
        ({"days": "0", "block_target": ["20", "18"], "block_start": ["07:00", "08:00"],
          "block_end": ["09:00", "10:00"]}, "Overlaps another time frame"),
    ],
)
def test_save_schedule_validates_input(client, tmp_path, monkeypatch, data, message):
    appmod = _with_store(tmp_path, monkeypatch)
    resp = client.post("/zones/1/schedule", data=data, follow_redirects=False)
    assert resp.status_code == 400
    assert message in resp.text
    assert appmod.schedules.get_zone("1") == {}


def test_save_schedule_accepts_a_frame_ending_at_midnight(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    resp = client.post(
        "/zones/1/schedule",
        data={"days": "0", "block_target": ["20", "21"], "block_start": ["07:00", "18:00"],
              "block_end": ["09:00", "00:00"]},
        follow_redirects=False,
    )
    _wait_for_sync()
    assert resp.status_code == 303
    assert appmod.schedules.get_day("1", 0) == [Block(20, 420, 540), Block(21, 1080, 1440)]
    assert "18:00&ndash;24:00" in client.get("/zones/1/schedule").text


def test_schedule_routes_reject_non_numeric_zone(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    form = {"days": "0", "block_target": "20", "block_start": "07:00", "block_end": "08:00"}
    assert client.post("/zones/__active__/schedule", data=form).status_code == 404
    assert client.post("/zones/abc/program", data={"active": "1"}).status_code == 404
    assert client.get("/zones/x/schedule").status_code == 404
    assert appmod.schedules.get_active("0") is None


def test_clear_rejects_unknown_day(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    resp = client.post("/zones/1/schedule/clear", data={"day": "-1"}, follow_redirects=False)
    assert resp.status_code == 400
    assert appmod.sync.get("1") is None  # no push started, nothing stuck on pending


def test_edit_link_preloads_the_days_frames(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 420, 540), Block(21, 1020, 1440)], [2])
    page = client.get("/zones/1/schedule")
    assert 'href="/zones/1/schedule?edit=2#editor"' in page.text
    edit = client.get("/zones/1/schedule?edit=2").text
    assert "Edit Wednesday" in edit
    assert 'value="17:00"' in edit and 'value="00:00"' in edit  # 24:00 shown as 00:00
    assert 'name="days" value="2" checked' in edit


def test_editing_a_day_then_saving_keeps_every_frame(client, tmp_path, monkeypatch):
    """The reported bug: adding a second slot to a day must not lose the first."""
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 420, 540)], [0])
    client.post(
        "/zones/1/schedule",
        data={"days": "0", "editing": "1", "block_target": ["20", "21"],
              "block_start": ["07:00", "17:00"], "block_end": ["09:00", "22:00"]},
    )
    _wait_for_sync()
    assert appmod.schedules.get_day("1", 0) == [Block(20, 420, 540), Block(21, 1020, 1320)]


def test_resync_pushes_the_whole_week_with_cleared_days(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 420, 540)], [0, 1])
    appmod.schedules.clear_day("1", 1)
    fake = _room_fake()
    assert appmod._resync_zone(fake, "1") is True
    sent = {d: blocks for _, blocks, days in fake.applied for d in days}
    assert sorted(sent) == list(range(7))  # every day written
    assert sent[0] == [Block(20, 420, 540)]
    assert sent[1] == []  # the cleared day goes out as cleared


def test_resync_of_a_fully_cleared_zone_clears_the_hub(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 420, 540)], [0])
    appmod.schedules.clear_day("1", 0)
    fake = _room_fake()
    appmod._resync_zone(fake, "1")
    assert fake.applied == [("1", [], list(range(7)))]


def test_resync_leaves_unowned_zone_programs_alone(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    appmod.schedules.set_active("1", False)  # program state only, no schedule set here
    fake = _room_fake()
    appmod._resync_zone(fake, "1")
    assert fake.applied == []  # a vendor-app program is not wiped
    assert fake.program_active["1"] is False


def test_sync_status_reflects_every_day_not_the_last_push(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    class MondayAsleep:
        def set_day_pattern(self, zone, blocks, days):
            if 0 in days:
                raise HearthError("asleep")

    appmod.schedules.set_pattern("1", [Block(20, 0, 60)], [0])
    appmod._request_sync(MondayAsleep(), "1")
    _wait_for_sync()
    appmod.schedules.set_pattern("1", [Block(18, 0, 60)], [5])  # Saturday alone would succeed
    appmod._request_sync(MondayAsleep(), "1")
    _wait_for_sync()
    assert appmod.sync.get("1") == "failed"  # Monday still has not reached the hub


def test_sync_crash_is_recorded_as_failed(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    class Broken:
        def set_day_pattern(self, *a):
            raise RuntimeError("bug")

    appmod.schedules.set_pattern("1", [Block(20, 0, 60)], [0])
    appmod._request_sync(Broken(), "1")
    _wait_for_sync()
    assert appmod.sync.get("1") == "failed"
    assert "1" not in appmod._sync_running


def test_change_during_a_push_is_pushed_again(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 0, 60)], [0])
    pushed: list = []

    class EditsMidPush:
        def set_day_pattern(self, zone, blocks, days):
            pushed.append((list(blocks), list(days)))
            if len(pushed) == 1:  # the user saves again while the first push runs
                appmod.schedules.set_pattern("1", [Block(22, 0, 60)], [0])
                appmod._request_sync(self, "1")

    appmod._request_sync(EditsMidPush(), "1")
    _wait_for_sync()
    final_monday = [b for b, d in pushed if 0 in d][-1]
    assert final_monday == [Block(22, 0, 60)]  # the latest state landed last
    assert appmod.sync.get("1") == "synced"


def test_toggle_program_pushes_program_state(client, tmp_path, monkeypatch):
    _with_store(tmp_path, monkeypatch)
    fake = app.dependency_overrides[get_client]()
    client.post("/zones/1/program", data={"active": "1"}, follow_redirects=False)
    _wait_for_sync()
    assert fake.program_active["1"] is True
    client.post("/zones/1/program", data={"active": "0"}, follow_redirects=False)
    _wait_for_sync()
    assert fake.program_active["1"] is False


def test_retry_loop_follows_the_current_client(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 0, 60)], [0])
    appmod.sync.set("1", "failed")
    current = _room_fake()
    monkeypatch.setattr(appmod, "get_client", lambda: current)

    def one_cycle(_seconds):
        if current.applied:
            raise SystemExit  # stop the endless loop after one retry
    monkeypatch.setattr(appmod._retry_wake, "wait", one_cycle)
    with pytest.raises(SystemExit):
        appmod._retry_loop()
    assert current.applied and appmod.sync.get("1") == "synced"


def test_import_backup_queues_restored_schedules_for_the_hub(client, tmp_path, monkeypatch):
    import io

    import hearthmage.app as appmod
    from hearthmage.settings import Settings
    from hearthmage.sync_store import SyncStore

    monkeypatch.setattr(appmod, "settings", Settings(str(tmp_path / "c.json"), env={}))
    monkeypatch.setattr(appmod, "sync", SyncStore(str(tmp_path / "sync.json")))
    bundle = {"hearthmage_backup": 1, "schedules": {"1": {"0": [[20, 420, 540]]}}}
    resp = client.post(
        "/settings/import",
        files={"backup": ("b.json", io.BytesIO(json.dumps(bundle).encode()), "application/json")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert appmod.sync.get("1") == "pending"  # queued, without building a hub client here
    assert appmod._retry_wake.is_set()  # retry loop woken to push it now
    fake = _room_fake()
    appmod._retry_failed_syncs(fake)  # what the woken retry loop does
    assert ("1", [Block(20, 420, 540)], [0]) in fake.applied
    assert appmod.sync.get("1") == "synced"


def test_import_backup_rejects_invalid_schedules(client, tmp_path, monkeypatch):
    import io

    import hearthmage.app as appmod
    from hearthmage.settings import Settings

    monkeypatch.setattr(appmod, "settings", Settings(str(tmp_path / "c.json"), env={}))
    bundle = {"hearthmage_backup": 1, "schedules": {"1": {"0": [[200, 420, 540]]}}}
    resp = client.post(
        "/settings/import",
        files={"backup": ("b.json", io.BytesIO(json.dumps(bundle).encode()), "application/json")},
    )
    assert resp.status_code == 400


def test_expired_holiday_is_not_shown(client, tmp_path, monkeypatch):
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    appmod.holidays.set("1", until="2020-01-01T08:00", arrival_temp=20.0, prev_setpoint=None)
    assert "Cancel holiday" not in client.get("/zones/1/schedule").text
    assert appmod.holidays.get("1") is None


def test_old_format_holiday_record_is_dropped(client, tmp_path, monkeypatch):
    # Records from before 2026-11-01 were sent to the hub with the wrong fields.
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    appmod.holidays._data["1"] = {"day": 5, "month": 11, "temp": 7.0, "prev_setpoint": None}
    assert "Cancel holiday" not in client.get("/zones/1/schedule").text
    assert appmod.holidays.get("1") is None

def test_push_is_verified_by_reading_the_hub_back(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)

    class Forgetful(FakeHearthClient):
        """Acks every write but the hub still holds nothing for Monday."""

        def read_day_pattern(self, room_id, day):
            return [] if day == 0 else super().read_day_pattern(room_id, day)

    appmod.schedules.set_pattern("1", [Block(20, 420, 540)], [0])
    assert appmod._resync_zone(_room_fake(), "1") is True  # read-back matches
    forgetful = Forgetful([Room("1", "Living Room", 19.0, 20.0, False, 5.0, 30.0)])
    assert appmod._resync_zone(forgetful, "1") is False  # acked, but not on the hub


def test_push_verification_fails_when_a_day_cannot_be_read(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)

    class Asleep(FakeHearthClient):
        def read_day_pattern(self, room_id, day):
            raise HearthError("asleep")

    appmod.schedules.set_pattern("1", [Block(20, 420, 540)], [0])
    assert appmod._resync_zone(Asleep([Room("1", "L", 19.0, 20.0, False, 5.0, 30.0)]), "1") is False


def test_load_from_radiator_previews_without_saving(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    fake = app.dependency_overrides[get_client]()
    fake.programs[("1", 2)] = [Block(19, 360, 480), Block(21, 1080, 1440)]
    resp = client.post("/zones/1/schedule/load")
    assert resp.status_code == 200
    assert "On the radiator" in resp.text
    assert "18:00&ndash;24:00" in resp.text
    assert 'name="day2" value="19,360,480;21,1080,1440"' in resp.text
    assert appmod.schedules.get_zone("1") == {}  # nothing replaced yet


def test_load_from_radiator_reports_an_unreadable_day(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    fake = app.dependency_overrides[get_client]()

    def asleep_on_wednesday(room_id, day):
        if day == 2:
            raise HearthError("Zone 1 day 2: no answer")
        return []

    monkeypatch.setattr(fake, "read_day_pattern", asleep_on_wednesday)
    resp = client.post("/zones/1/schedule/load")
    assert "Couldn&#39;t read Wednesday" in resp.text
    assert "Replace saved schedule" not in resp.text


def test_load_from_radiator_says_when_it_already_matches(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    fake = app.dependency_overrides[get_client]()
    appmod.schedules.set_pattern("1", [Block(20, 420, 540)], [0])
    fake.programs[("1", 0)] = [Block(20, 420, 540)]
    resp = client.post("/zones/1/schedule/load")
    assert "already matches" in resp.text
    assert "Replace saved schedule" not in resp.text


def test_apply_loaded_week_replaces_and_syncs(client, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0, 1])
    resp = client.post(
        "/zones/1/schedule/load/apply",
        data={"day2": "19,360,480;21,1080,1440", **{f"day{d}": "" for d in (0, 1, 3, 4, 5, 6)}},
        follow_redirects=False,
    )
    _wait_for_sync()
    assert resp.status_code == 303
    assert appmod.schedules.get_zone("1") == {2: [Block(19, 360, 480), Block(21, 1080, 1440)]}
    assert appmod.sync.get("1") == "synced"


@pytest.mark.parametrize("bad", ["19,360", "99,360,480", "19,480,360", "x", "19,0,600;20,300,700"])
def test_apply_loaded_week_rejects_tampered_input(client, tmp_path, monkeypatch, bad):
    appmod = _with_store(tmp_path, monkeypatch)
    resp = client.post("/zones/1/schedule/load/apply", data={"day0": bad})
    assert resp.status_code == 400
    assert appmod.schedules.get_zone("1") == {}


def test_settings_cloud_toggle(client, tmp_path, monkeypatch):
    fake = app.dependency_overrides[get_client]()
    fake.cloud_reporting = True
    part = client.get("/settings/cloud")
    assert "is sending data to the manufacturer" in part.text
    resp = client.post("/settings/cloud", data={"enabled": "0"}, headers={"HX-Request": "true"})
    assert fake.cloud_reporting is False
    assert "is not sending data" in resp.text
    resp = client.post("/settings/cloud", data={"enabled": "1"}, follow_redirects=False)
    assert fake.cloud_reporting is True
    assert resp.status_code == 303  # no-JS fallback returns to the settings page


def test_settings_cloud_reports_an_unreachable_hub(client, monkeypatch):
    fake = app.dependency_overrides[get_client]()

    def unreachable():
        raise HearthError("No response from hub")

    monkeypatch.setattr(fake, "read_cloud_reporting", unreachable)
    assert "Couldn&#39;t read this setting from the hub" in client.get("/settings/cloud").text


def _with_holiday(tmp_path, monkeypatch):
    import hearthmage.app as appmod
    from hearthmage.holiday_store import HolidayStore

    monkeypatch.setattr(appmod, "holidays", HolidayStore(str(tmp_path / "holiday.json")))
    return appmod


def _in_days(n: int) -> str:
    from datetime import date, timedelta

    return (date.today() + timedelta(days=n)).isoformat()


def test_start_holiday_sends_days_from_today(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    resp = client.post(
        "/zones/1/holiday",
        data={"until": _in_days(4), "until_time": "17:30", "arrival": "temp", "arrival_temp": "20"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/zones/1/schedule"
    fake = app.dependency_overrides[get_client]()
    assert fake.holiday["1"] == {"active": True, "days": 4, "hour": 17, "minute": 30,
                                 "arrival_temp": 20.0}
    stored = appmod.holidays.get("1")
    assert stored["until"] == f"{_in_days(4)}T17:30"
    assert stored["arrival_temp"] == 20.0
    assert stored["prev_setpoint"] == 21.0  # the room's target before the hold


def test_start_holiday_talks_to_the_hub_off_the_event_loop(client, tmp_path, monkeypatch):
    # A hub write can block for half a minute when the radiator is asleep. On the
    # event loop that would freeze every other request, /healthz included.
    import asyncio

    _with_holiday(tmp_path, monkeypatch)
    fake = app.dependency_overrides[get_client]()
    seen = {}

    def set_holiday(room_id, **kwargs):
        try:
            asyncio.get_running_loop()
            seen["on_event_loop"] = True
        except RuntimeError:
            seen["on_event_loop"] = False

    monkeypatch.setattr(fake, "set_holiday", set_holiday)
    resp = client.post(
        "/zones/1/holiday",
        data={"until": _in_days(4), "until_time": "17:30", "arrival": "temp", "arrival_temp": "20"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert seen == {"on_event_loop": False}


def test_start_holiday_can_resume_the_schedule(client: TestClient, tmp_path, monkeypatch):
    _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    client.post("/zones/1/holiday",
                data={"until": _in_days(2), "until_time": "08:00", "arrival": "program"})
    fake = app.dependency_overrides[get_client]()
    assert fake.holiday["1"]["arrival_temp"] is None


@pytest.mark.parametrize("data, message", [
    ({"until": _in_days(-1), "until_time": "08:00", "arrival": "program"}, "in the future"),
    ({"until": _in_days(300), "until_time": "08:00", "arrival": "program"}, "240 days"),
    ({"until": "not-a-date", "until_time": "08:00", "arrival": "program"}, "date"),
    ({"until": _in_days(2), "until_time": "8am", "arrival": "program"}, "HH:MM"),
    ({"until": _in_days(2), "until_time": "08:00", "arrival": "temp", "arrival_temp": "99"}, "5 to 30"),
])
def test_start_holiday_rejects_bad_input(client, tmp_path, monkeypatch, data, message):
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    resp = client.post("/zones/1/holiday", data=data, follow_redirects=False)
    assert resp.status_code == 400
    assert message in resp.text
    assert "1" not in app.dependency_overrides[get_client]().holiday  # nothing sent
    assert appmod.holidays.get("1") is None


def test_cancel_holiday_clears_and_restores_setpoint(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    appmod.holidays.set("1", until=f"{_in_days(3)}T08:00", arrival_temp=20.0, prev_setpoint=21.0)
    fake = app.dependency_overrides[get_client]()
    fake.set_temperature("1", 7)
    resp = client.post("/zones/1/holiday/cancel", follow_redirects=False)
    assert resp.status_code == 303
    assert appmod.holidays.get("1") is None
    assert fake.holiday["1"]["active"] is False  # hub hold cleared
    room = next(r for r in fake.list_rooms() if r.id == "1")
    assert room.target_temp == 21.0  # setpoint restored


def test_cancel_holiday_keeps_the_hold_when_the_hub_does_not_answer(client, tmp_path, monkeypatch):
    # The radiator is asleep: the hub still holds the zone, so the app must keep
    # its record (and the setpoint to restore) and let the user try again.
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    appmod.holidays.set("1", until=f"{_in_days(3)}T08:00", arrival_temp=20.0, prev_setpoint=21.0)
    fake = app.dependency_overrides[get_client]()
    fake.set_temperature("1", 7)

    def asleep(room_id):
        raise HearthError("Holiday clear failed for zone 1: 'ER,1'")

    monkeypatch.setattr(fake, "clear_holiday", asleep)
    resp = client.post("/zones/1/holiday/cancel", follow_redirects=False)
    assert resp.status_code == 502
    assert "didn&#39;t cancel the holiday" in resp.text or "didn't cancel the holiday" in resp.text
    assert "Cancel holiday" in resp.text  # still offered, so it can be retried
    assert appmod.holidays.get("1")["prev_setpoint"] == 21.0
    room = next(r for r in fake.list_rooms() if r.id == "1")
    assert room.target_temp == 7.0  # no restore while the hold is still on


def test_cancel_holiday_leaves_a_running_program_alone(client, tmp_path, monkeypatch):
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    appmod.schedules.set_pattern("1", [Block(20, 420, 540)], [0])
    appmod.schedules.set_active("1", True)
    appmod.holidays.set("1", until=f"{_in_days(3)}T08:00", arrival_temp=None, prev_setpoint=21.0)
    fake = app.dependency_overrides[get_client]()
    fake.set_temperature("1", 7)
    client.post("/zones/1/holiday/cancel")
    room = next(r for r in fake.list_rooms() if r.id == "1")
    assert room.target_temp == 7.0  # not forced to a manual target over the program


def test_schedule_page_shows_active_holiday(client: TestClient, tmp_path, monkeypatch):
    from datetime import date, timedelta

    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    until = date.today() + timedelta(days=3)
    appmod.holidays.set("1", until=f"{until.isoformat()}T17:30", arrival_temp=20.0,
                        prev_setpoint=21.0)
    page = client.get("/zones/1/schedule").text
    assert "Cancel holiday" in page
    assert f"{until:%a %d %b}, 17:30" in page
    assert "20&deg;C" in page
    appmod.holidays.set("1", until=f"{until.isoformat()}T17:30", arrival_temp=None,
                        prev_setpoint=None)
    assert "weekly schedule" in client.get("/zones/1/schedule").text


def test_retry_keeps_failed_when_still_unreachable(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0])
    appmod.sync.set("1", "failed")

    class Boom:
        def set_day_pattern(self, *a):
            raise HearthError("still asleep")

    appmod._retry_failed_syncs(Boom())
    assert appmod.sync.get("1") == "failed"  # stays failed for the next retry


class _AsleepZone:
    """A hub whose radiator never answers a schedule write; counts the attempts."""

    def __init__(self) -> None:
        self.day_writes = 0
        self.toggles = 0

    def set_day_pattern(self, *a):
        self.day_writes += 1
        raise HearthError("still asleep")

    def set_program_active(self, *a):
        self.toggles += 1
        raise HearthError("still asleep")


def test_resync_stops_sending_days_at_the_first_write_the_hub_does_not_take(tmp_path, monkeypatch):
    # Two day-groups: once one write fails the radiator is asleep, so the other
    # would only be another doomed RF attempt.
    appmod = _with_store(tmp_path, monkeypatch)
    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0])
    appmod.schedules.set_pattern("1", [Block(18, 0, 600)], [1])
    appmod.schedules.set_active("1", True)
    hub = _AsleepZone()
    assert appmod._resync_zone(hub, "1") is False
    assert hub.day_writes == 1


def test_program_switch_is_still_sent_when_the_week_will_not_go_through(tmp_path, monkeypatch):
    # Switching the weekly program off is how a user stops unwanted heating. It
    # is one short command and must not wait for a week write that keeps failing.
    appmod = _with_store(tmp_path, monkeypatch)
    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0])
    appmod.schedules.set_active("1", False)

    class WeekFails(FakeHearthClient):
        def set_day_pattern(self, *a):
            raise HearthError("program write timed out")

    hub = WeekFails([Room("1", "L", 19.0, 20.0, False, 5.0, 30.0)])
    assert appmod._resync_zone(hub, "1") is False  # still not synced...
    assert hub.program_active["1"] is False  # ...but the switch-off reached the hub


def test_a_change_or_a_new_hub_clears_the_retry_wait(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(appmod, "_monotonic", lambda: clock["now"])
    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0])
    appmod.sync.set("1", "failed")
    asleep = _AsleepZone()
    for _ in range(4):  # fail until the wait is several minutes long
        appmod._retry_failed_syncs(asleep)
        clock["now"] += appmod._RETRY_MAX_WAIT
    assert appmod._sync_backoff["1"][0] == 4
    # The hub address is corrected: retries must not sit out the old wait.
    appmod._drop_client()
    assert appmod._sync_backoff == {}
    fake = _room_fake()
    appmod._retry_failed_syncs(fake)
    assert appmod.sync.get("1") == "synced"
    # And a user's own change starts the count afresh rather than adding to it.
    appmod._sync_backoff["1"] = (5, clock["now"] + 9999)
    appmod._request_sync(fake, "1")
    assert "1" not in appmod._sync_backoff or appmod._sync_backoff["1"][0] <= 1


def test_retry_backs_off_from_a_zone_that_keeps_failing(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(appmod, "_monotonic", lambda: clock["now"])
    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0])
    appmod.sync.set("1", "failed")
    hub = _AsleepZone()

    appmod._retry_failed_syncs(hub)  # first retry: tried, fails
    assert hub.day_writes == 1
    clock["now"] += 59
    appmod._retry_failed_syncs(hub)  # too soon
    assert hub.day_writes == 1
    clock["now"] += 2  # 61 s after the first failure
    appmod._retry_failed_syncs(hub)
    assert hub.day_writes == 2
    clock["now"] += 61  # the wait has doubled to 120 s, so this is still too soon
    appmod._retry_failed_syncs(hub)
    assert hub.day_writes == 2
    clock["now"] += 60
    appmod._retry_failed_syncs(hub)
    assert hub.day_writes == 3
    clock["now"] += 100 * 3600  # however long it has failed, the wait is capped
    appmod._retry_failed_syncs(hub)
    clock["now"] += appmod._RETRY_MAX_WAIT + 1
    appmod._retry_failed_syncs(hub)
    assert hub.day_writes == 5


def test_a_newly_queued_or_recovered_zone_is_pushed_without_waiting(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(appmod, "_monotonic", lambda: clock["now"])
    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [0])
    appmod.sync.set("1", "failed")
    appmod._retry_failed_syncs(_AsleepZone())  # now backing off
    # A backup import (or a new change) queues the zone as pending: push at once.
    appmod.sync.set("1", "pending")
    fake = _room_fake()
    appmod._retry_failed_syncs(fake)
    assert appmod.sync.get("1") == "synced"
    # ...and the success wipes the backoff, so a later failure starts from 60 s.
    assert "1" not in appmod._sync_backoff


# ---- MQTT settings --------------------------------------------------------


def _mqtt_env(tmp_path, monkeypatch, env=None):
    import hearthmage.app as appmod
    from hearthmage.settings import Settings

    monkeypatch.setattr(appmod, "settings", Settings(str(tmp_path / "c.json"), env=env or {}))
    monkeypatch.setattr(appmod, "_client", None)
    monkeypatch.setattr(appmod, "_client_hub", None)
    restarts: list[bool] = []
    monkeypatch.setattr(appmod, "_restart_mqtt", lambda: restarts.append(True))
    return appmod, restarts


def test_settings_page_shows_mqtt_section_without_password(tmp_path, monkeypatch):
    appmod, _ = _mqtt_env(tmp_path, monkeypatch)
    appmod.settings.set_mqtt("10.0.0.5", 1883, "hearth", "set", "sekret")
    resp = TestClient(app).get("/settings")
    assert resp.status_code == 200
    assert "Home Assistant (MQTT)" in resp.text
    assert "10.0.0.5" in resp.text
    assert "sekret" not in resp.text  # the stored password is never rendered
    assert "leave blank to keep" in resp.text  # placeholder signals one is set


def test_settings_page_warns_when_ui_is_open(tmp_path, monkeypatch):
    _mqtt_env(tmp_path, monkeypatch)
    resp = TestClient(app).get("/settings")
    assert "anyone on" in resp.text  # open-UI warning


def test_settings_page_no_warning_with_auth(tmp_path, monkeypatch):
    _mqtt_env(
        tmp_path, monkeypatch,
        env={"HEARTHMAGE_PASSWORD": "swordfish", "HEARTHMAGE_SECRET_KEY": "testkey"},
    )
    c = TestClient(app)
    c.post("/login", data={"password": "swordfish"})
    resp = c.get("/settings")
    assert resp.status_code == 200
    assert "anyone on" not in resp.text


def test_save_mqtt_persists_and_restarts_bridge(tmp_path, monkeypatch):
    appmod, restarts = _mqtt_env(tmp_path, monkeypatch)
    resp = TestClient(app).post(
        "/settings/mqtt",
        data={"host": "10.0.0.5", "port": "1883", "username": "hearth",
              "password": "sekret", "base_topic": "home/heat"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
    cfg = appmod.settings.mqtt_config()
    assert cfg["host"] == "10.0.0.5"
    assert cfg["password"] == "sekret"
    assert cfg["base_topic"] == "home/heat"
    assert restarts  # the bridge was restarted with the new settings


def test_save_mqtt_blank_password_keeps_stored(tmp_path, monkeypatch):
    appmod, _ = _mqtt_env(tmp_path, monkeypatch)
    appmod.settings.set_mqtt("10.0.0.5", 1883, "hearth", "set", "sekret")
    TestClient(app).post(
        "/settings/mqtt",
        data={"host": "10.0.0.5", "port": "1883", "username": "hearth",
              "password": "", "base_topic": "hearthmage"},
        follow_redirects=False,
    )
    assert appmod.settings.mqtt_config()["password"] == "sekret"


def test_save_mqtt_clear_password(tmp_path, monkeypatch):
    appmod, _ = _mqtt_env(tmp_path, monkeypatch)
    appmod.settings.set_mqtt("10.0.0.5", 1883, "hearth", "set", "sekret")
    TestClient(app).post(
        "/settings/mqtt",
        data={"host": "10.0.0.5", "port": "1883", "username": "hearth",
              "password": "", "clear_password": "1", "base_topic": "hearthmage"},
        follow_redirects=False,
    )
    assert appmod.settings.mqtt_config()["password"] is None


def test_disable_mqtt_clears_and_restarts(tmp_path, monkeypatch):
    appmod, restarts = _mqtt_env(tmp_path, monkeypatch)
    appmod.settings.set_mqtt("10.0.0.5", 1883, "hearth", "set", "sekret")
    resp = TestClient(app).post("/settings/mqtt/disable", follow_redirects=False)
    assert resp.status_code == 303
    assert appmod.settings.mqtt_enabled is False
    assert restarts


def test_export_redacts_mqtt_password_and_secret_key(tmp_path, monkeypatch):
    appmod, _ = _mqtt_env(tmp_path, monkeypatch)
    appmod.settings.set_mqtt("10.0.0.5", 1883, "hearth", "set", "brokerpass")
    appmod.settings.secret_key()  # force one to be generated and stored
    resp = TestClient(app).get("/settings/export")
    assert resp.status_code == 200
    assert "brokerpass" not in resp.text
    assert "secret_key" not in resp.text


def test_import_round_trip_preserves_stored_password(tmp_path, monkeypatch):
    appmod, _ = _mqtt_env(tmp_path, monkeypatch)
    appmod.settings.set_mqtt("10.0.0.5", 1883, "hearth", "set", "brokerpass")
    c = TestClient(app)
    exported = c.get("/settings/export").content
    resp = c.post(
        "/settings/import",
        files={"backup": ("b.json", exported, "application/json")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from hearthmage.settings import Settings
    restored = Settings(str(tmp_path / "c.json"), env={})
    assert restored.mqtt_config()["password"] == "brokerpass"


def test_save_mqtt_blank_host_saves_nothing(tmp_path, monkeypatch):
    appmod, restarts = _mqtt_env(tmp_path, monkeypatch)
    resp = TestClient(app).post(
        "/settings/mqtt",
        data={"host": "   ", "port": "1883", "username": "", "password": "",
              "base_topic": "hearthmage"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert appmod.settings.mqtt_config() is None
    assert not restarts


def test_save_mqtt_bad_port_falls_back(tmp_path, monkeypatch):
    appmod, _ = _mqtt_env(tmp_path, monkeypatch)
    for bad in ("nope", "0", "70000"):
        TestClient(app).post(
            "/settings/mqtt",
            data={"host": "10.0.0.5", "port": bad, "username": "", "password": "",
                  "base_topic": "hearthmage"},
            follow_redirects=False,
        )
        assert appmod.settings.mqtt_config()["port"] == 1883


def test_save_mqtt_strips_host(tmp_path, monkeypatch):
    appmod, _ = _mqtt_env(tmp_path, monkeypatch)
    TestClient(app).post(
        "/settings/mqtt",
        data={"host": "  10.0.0.5  ", "port": "1883", "username": "", "password": "",
              "base_topic": "hearthmage"},
        follow_redirects=False,
    )
    assert appmod.settings.mqtt_config()["host"] == "10.0.0.5"


def test_restart_mqtt_stops_old_client_and_is_serialised(tmp_path, monkeypatch):
    import threading
    import time

    import hearthmage.app as appmod
    from hearthmage.mqtt_bridge import MqttBridge
    from hearthmage.settings import Settings

    monkeypatch.setattr(appmod, "settings", Settings(str(tmp_path / "c.json"), env={}))
    appmod.settings.set_mqtt("10.0.0.5", 1883, "", "keep", "")
    monkeypatch.setattr(appmod, "_client", None)
    monkeypatch.setattr(appmod, "_mqtt_client", None)
    monkeypatch.setattr(appmod, "mqtt_bridge", None)

    created = []

    class FakeClient:
        def __init__(self):
            self.stopped = False

        def loop_stop(self):
            self.stopped = True

        def disconnect(self):
            pass

    def fake_connect(self, host, port, username, password):
        time.sleep(0.01)  # widen the race window
        client = FakeClient()
        created.append(client)
        return client

    monkeypatch.setattr(MqttBridge, "connect", fake_connect)

    threads = [threading.Thread(target=appmod._restart_mqtt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 2
    live = [c for c in created if not c.stopped]
    assert len(live) == 1  # exactly one client survives; no leak
    assert appmod._mqtt_client is live[0]
    appmod._restart_mqtt()  # a later restart can still stop the survivor
    assert live[0].stopped
    appmod._mqtt_client.loop_stop()
    monkeypatch.setattr(appmod, "_mqtt_client", None)
    monkeypatch.setattr(appmod, "mqtt_bridge", None)


def test_settings_page_lazy_loads_the_cloud_section(client, tmp_path, monkeypatch):
    import hearthmage.app as appmod
    from hearthmage.settings import Settings

    monkeypatch.setattr(appmod, "settings", Settings(str(tmp_path / "c.json"), env={}))
    monkeypatch.setattr(appmod, "get_client", lambda: _room_fake())  # the page lists rooms
    assert 'hx-get="/settings/cloud"' not in client.get("/settings").text  # no hub yet
    appmod.settings.set_hub("10.0.0.5", 6653)
    assert 'hx-get="/settings/cloud"' in client.get("/settings").text
