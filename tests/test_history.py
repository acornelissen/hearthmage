from hearthmage.history import HistoryStore


def test_temp_series_round_trips(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    store.record_temp("1", 20.4, 21.0, ts=100.0)
    store.record_temp("1", 20.6, 21.0, ts=200.0)
    store.record_temp("2", 18.0, 19.0, ts=150.0)
    series = store.temp_series("1")
    assert [(r["ts"], r["current"], r["setpoint"]) for r in series] == [
        (100.0, 20.4, 21.0),
        (200.0, 20.6, 21.0),
    ]
    assert len(store.temp_series("2")) == 1


def test_temp_series_empty_for_unknown_zone(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    assert store.temp_series("9") == []


def test_energy_day_is_upserted(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    store.record_energy_day("1", "2026-07-18", 3.2)
    store.record_energy_day("1", "2026-07-19", 4.0)
    store.record_energy_day("1", "2026-07-19", 4.5)  # same day updates, not duplicates
    series = store.energy_series("1")
    assert [(r["day"], r["kwh"]) for r in series] == [
        ("2026-07-18", 3.2),
        ("2026-07-19", 4.5),
    ]


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "history.db")
    HistoryStore(path).record_temp("1", 20.0, 21.0, ts=100.0)
    assert len(HistoryStore(path).temp_series("1")) == 1


def test_waste_event_lifecycle(tmp_path):
    store = HistoryStore(str(tmp_path / "h.db"))
    event_id = store.start_waste_event("1", start_ts=100.0, start_temp=13.0, setpoint=5.0,
                                       peak_ts=700.0, peak_temp=13.6)
    store.update_waste_event(event_id, peak_ts=900.0, peak_temp=14.0, open_=True)
    [event] = store.waste_events("1")
    assert event["start_ts"] == 100.0 and event["peak_ts"] == 900.0
    assert event["peak_temp"] == 14.0 and event["open"] == 1
    store.update_waste_event(event_id, peak_ts=900.0, peak_temp=14.0, open_=False)
    assert store.waste_events("1")[0]["open"] == 0
    assert store.waste_events("2") == []


def test_waste_events_newest_first_and_since(tmp_path):
    store = HistoryStore(str(tmp_path / "h.db"))
    for start in (100.0, 5000.0, 9000.0):
        store.start_waste_event("1", start, 13.0, 5.0, start + 600, 13.6)
    assert [e["start_ts"] for e in store.waste_events("1")] == [9000.0, 5000.0, 100.0]
    assert [e["start_ts"] for e in store.waste_events("1", since_ts=4000.0)] == [9000.0, 5000.0]
    assert store.zones_with_waste(since_ts=8000.0) == ["1"]


def test_close_open_waste_events(tmp_path):
    """After a restart nothing is tracking an open event any more."""
    path = str(tmp_path / "h.db")
    HistoryStore(path).start_waste_event("1", 100.0, 13.0, 5.0, 700.0, 13.6)
    reopened = HistoryStore(path)
    reopened.close_open_waste_events()
    assert reopened.waste_events("1")[0]["open"] == 0


def test_status_snapshots(tmp_path):
    store = HistoryStore(str(tmp_path / "h.db"))
    assert store.latest_status("1", "rest") is None
    store.record_status("1", 10.0, "rest", "OK,13,0,5")
    store.record_status("1", 20.0, "rest", "OK,13,1,5")
    event_id = store.start_waste_event("1", 100.0, 13.0, 5.0, 700.0, 13.6)
    store.record_status("1", 110.0, "heating", "OK,13,5,5", event_id=event_id)
    assert store.latest_status("1", "rest")["raw"] == "OK,13,1,5"
    assert store.status_for_event(event_id)["raw"] == "OK,13,5,5"
    assert store.rest_status_before("1", 15.0)["raw"] == "OK,13,0,5"


def test_db_file_is_owner_only(tmp_path):
    import stat

    path = tmp_path / "history.db"
    HistoryStore(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_world_readable_db_is_tightened(tmp_path):
    import os
    import stat

    path = tmp_path / "history.db"
    HistoryStore(str(path))
    os.chmod(path, 0o644)
    HistoryStore(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_chmod_refused_is_not_an_error(tmp_path, monkeypatch):
    import os

    def refuse(*a, **k):
        raise PermissionError("no")

    monkeypatch.setattr(os, "chmod", refuse)
    HistoryStore(str(tmp_path / "history.db"))  # must not raise


def test_ts_index_exists(tmp_path):
    import sqlite3

    path = tmp_path / "history.db"
    HistoryStore(str(path))
    names = {r[1] for r in sqlite3.connect(path).execute("PRAGMA index_list(temp_readings)")}
    assert "ix_temp_ts" in names


def _fill(store):
    store.record_temp("1", 20.0, 21.0, ts=100.0)
    store.record_temp("1", 20.0, 21.0, ts=900.0)
    old_closed = store.start_waste_event("1", 100.0, 18.0, 17.0, 110.0, 19.0)
    store.update_waste_event(old_closed, 110.0, 19.0, open_=False)
    old_open = store.start_waste_event("1", 120.0, 18.0, 17.0, 120.0, 18.0)
    new_ev = store.start_waste_event("1", 800.0, 18.0, 17.0, 810.0, 19.0)
    store.update_waste_event(new_ev, 810.0, 19.0, open_=False)  # kept for being recent
    store.record_status("1", 101.0, "heating", "a", old_closed)
    store.record_status("1", 121.0, "heating", "b", old_open)
    store.record_status("1", 150.0, "rest", "c")  # old, no event
    store.record_status("1", 50.0, "heating", "d", new_ev)  # old ts, live event
    store.record_status("1", 850.0, "rest", "e")
    store.record_status("1", 40.0, "rest", "f")  # baseline before the old-ts heating snapshot
    return old_closed, old_open, new_ev


def test_prune_old_covers_all_but_energy(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    old_closed, old_open, new_ev = _fill(store)
    store.record_energy_day("1", "2020-01-01", 3.0)
    assert store.prune_old(now=1000.0, retain_seconds=500.0) is True  # cutoff 500
    assert [r["ts"] for r in store.temp_series("1")] == [900.0]
    ids = {e["id"] for e in store.waste_events("1")}
    assert ids == {old_open, new_ev}  # open event and in-window event survive
    raws = {store.status_for_event(i)["raw"] for i in ids}
    assert raws == {"b", "d"}
    assert store.latest_status("1", "rest")["raw"] == "e"
    assert store.status_for_event(old_closed) is None
    assert store.rest_status_before("1", 50.0)["raw"] == "f"  # baseline kept
    assert store.rest_status_before("1", 149.0)["raw"] == "f"
    assert len(store.energy_series("1")) == 1


def test_prune_old_runs_at_most_hourly(tmp_path, monkeypatch):
    from hearthmage import history as hist

    clock = {"t": 0.0}
    monkeypatch.setattr(hist, "_monotonic", lambda: clock["t"])
    store = HistoryStore(str(tmp_path / "history.db"))
    store.record_temp("1", 20.0, 21.0, ts=100.0)
    assert store.prune_old(now=10000.0, retain_seconds=500.0) is True
    store.record_temp("1", 20.0, 21.0, ts=200.0)
    clock["t"] = 3599.0
    assert store.prune_old(now=10000.0, retain_seconds=500.0) is False
    assert len(store.temp_series("1")) == 1  # 200.0 not yet pruned
    clock["t"] = 3600.0
    assert store.prune_old(now=10000.0, retain_seconds=500.0) is True
    assert store.temp_series("1") == []


def test_prune_gate_ignores_wall_clock_jumps_back(tmp_path, monkeypatch):
    from hearthmage import history as hist

    clock = {"t": 0.0}
    monkeypatch.setattr(hist, "_monotonic", lambda: clock["t"])
    store = HistoryStore(str(tmp_path / "history.db"))
    assert store.prune_old(now=1_000_000.0, retain_seconds=500.0) is True
    clock["t"] = 3600.0  # an hour later by the monotonic clock, wall clock went back
    assert store.prune_old(now=10.0, retain_seconds=5.0) is True


def test_restrict_runs_before_schema_work(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(HistoryStore, "_restrict_files", lambda self: order.append("restrict"))
    monkeypatch.setattr(HistoryStore, "_init_db", lambda self: order.append("init"))
    HistoryStore(str(tmp_path / "history.db"))
    assert order[0] == "restrict"


def test_prune_old_baseline_is_per_zone(tmp_path):
    import sqlite3

    path = tmp_path / "history.db"
    store = HistoryStore(str(path))
    store.record_status("1", 40.0, "rest", "z1base")
    store.record_status("1", 20.0, "rest", "z1older")  # not the newest before the heating one
    store.record_status("2", 45.0, "rest", "z2newer")  # another zone's rest must not count
    store.record_status("2", 30.0, "rest", "z2old")
    ev = store.start_waste_event("1", 600.0, 18.0, 17.0, 610.0, 19.0)
    store.update_waste_event(ev, 610.0, 19.0, open_=False)
    store.record_status("1", 50.0, "heating", "h1", ev)
    live = store.start_waste_event("2", 10.0, 18.0, 17.0, 10.0, 18.0)  # open, old
    store.record_status("2", 35.0, "heating", "h2", live)
    store.record_status("1", 60.0, "heating", "hnull", None)  # old, no event: goes
    store.prune_old(now=1000.0, retain_seconds=500.0)
    left = {r[0] for r in sqlite3.connect(path).execute("SELECT raw FROM status_snapshots")}
    assert left == {"h1", "h2", "z1base", "z2old"}


def test_status_snapshots_index_exists(tmp_path):
    import sqlite3

    path = tmp_path / "history.db"
    HistoryStore(str(path))
    names = {r[1] for r in sqlite3.connect(path).execute("PRAGMA index_list(status_snapshots)")}
    assert "ix_status_zone_kind_ts" in names
