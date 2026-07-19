import json

import pytest
from fastapi.testclient import TestClient

from hearthmage.app import app, get_client
from hearthmage.domain import HearthError, Room
from hearthmage.fake_client import FakeHearthClient


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


# ---- per-radiator config -------------------------------------------------


def test_config_page_shows_current_settings(client: TestClient):
    resp = client.get("/zones/1/config")
    assert resp.status_code == 200
    assert "Living Room" in resp.text
    assert "Lock the keypad" in resp.text
    assert "Temperature offset" in resp.text


def test_save_config_writes_and_redirects(client: TestClient):
    resp = client.post(
        "/zones/1/config",
        data={"keypad_lock": "1", "offset": "9"},  # window_sensor omitted -> off
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/zones/1/config?saved=1"
    # confirm the fake client recorded the change
    from hearthmage.app import get_client as _gc

    fake = app.dependency_overrides[_gc]()
    cfg = fake.read_config("1")
    assert cfg["keypad_lock"] is True
    assert cfg["window_sensor"] is False
    assert cfg["offset"] == 9


def test_save_config_rejects_bad_offset(client: TestClient):
    resp = client.post("/zones/1/config", data={"offset": "99"}, follow_redirects=False)
    assert resp.status_code == 502
    assert "out of range" in resp.text


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


def test_save_schedule_skips_malformed_rows(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    client.post(
        "/zones/1/schedule",
        data={
            "days": ["1"],
            "block_target": ["20", "18"],
            "block_start": ["07:00", "10:00"],
            "block_end": ["09:00", "08:00"],  # second row ends before it starts
        },
        follow_redirects=False,
    )
    day1 = appmod.schedules.get_day("1", 1)
    assert [(b.target, b.start, b.end) for b in day1] == [(20, 420, 540)]


def test_clear_day_removes_from_store(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    appmod.schedules.set_pattern("1", [Block(20, 0, 600)], [3])
    resp = client.post("/zones/1/schedule/clear", data={"day": "3"}, follow_redirects=False)
    assert resp.status_code == 303
    assert appmod.schedules.get_day("1", 3) == []


def test_push_schedule_syncs_to_client(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    fake = FakeHearthClient(
        [Room("1", "Living Room", 19.0, 20.0, is_off=False, min_temp=5.0, max_temp=30.0)]
    )
    appmod._push_schedule(fake, "1", [Block(20, 420, 540)], [0, 1])
    assert fake.applied == [("1", [Block(20, 420, 540)], [0, 1])]
    assert appmod.sync.get("1") == "synced"


def test_toggle_program_activate_and_deactivate(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    fake = FakeHearthClient(
        [Room("1", "Living Room", 19.0, 20.0, is_off=False, min_temp=5.0, max_temp=30.0)]
    )
    appmod.app.dependency_overrides[get_client] = lambda: fake
    try:
        resp = TestClient(app).post("/zones/1/program", data={"active": "1"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/zones/1/schedule"
        # the background push runs synchronously enough via the helper; verify directly
        appmod._push_program(fake, "1", False)
        assert fake.program_active["1"] is False
        appmod._push_program(fake, "1", True)
        assert fake.program_active["1"] is True
    finally:
        appmod.app.dependency_overrides.clear()


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


def test_push_program_marks_failed_on_error(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)

    class Boom:
        def set_program_active(self, *a):
            raise HearthError("asleep")

    appmod._push_program(Boom(), "1", True)
    assert appmod.sync.get("1") == "failed"


def test_push_schedule_marks_failed_on_error(tmp_path, monkeypatch):
    appmod = _with_store(tmp_path, monkeypatch)
    from hearthmage.schedule import Block

    class Boom:
        def set_day_pattern(self, *a):
            raise HearthError("asleep")

    appmod._push_schedule(Boom(), "1", [Block(20, 0, 600)], [0])
    assert appmod.sync.get("1") == "failed"


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


def _with_holiday(tmp_path, monkeypatch):
    import hearthmage.app as appmod
    from hearthmage.holiday_store import HolidayStore

    monkeypatch.setattr(appmod, "holidays", HolidayStore(str(tmp_path / "holiday.json")))
    return appmod


def test_start_holiday_sets_hub_and_store(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)  # schedule page reads sync/schedules too
    resp = client.post(
        "/zones/1/holiday",
        data={"until": "2026-11-05", "temp": "7"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/zones/1/schedule"
    fake = app.dependency_overrides[get_client]()
    assert fake.holiday["1"] == {"active": True, "day": 5, "month": 11, "temp": 7.0}
    stored = appmod.holidays.get("1")
    assert stored["day"] == 5 and stored["month"] == 11
    assert stored["prev_setpoint"] == 21.0  # the room's target before the hold


def test_cancel_holiday_clears_and_restores_setpoint(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_holiday(tmp_path, monkeypatch)
    appmod.holidays.set("1", day=5, month=11, temp=7.0, prev_setpoint=21.0)
    resp = client.post("/zones/1/holiday/cancel", follow_redirects=False)
    assert resp.status_code == 303
    assert appmod.holidays.get("1") is None
    fake = app.dependency_overrides[get_client]()
    assert fake.holiday["1"]["active"] is False  # hub hold cleared
    room = next(r for r in fake.list_rooms() if r.id == "1")
    assert room.target_temp == 21.0  # setpoint restored


def test_schedule_page_shows_active_holiday(client: TestClient, tmp_path, monkeypatch):
    appmod = _with_holiday(tmp_path, monkeypatch)
    _with_store(tmp_path, monkeypatch)
    appmod.holidays.set("1", day=5, month=11, temp=7.0, prev_setpoint=21.0)
    resp = client.get("/zones/1/schedule")
    assert resp.status_code == 200
    assert "Cancel holiday" in resp.text
    assert "05/11" in resp.text


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
