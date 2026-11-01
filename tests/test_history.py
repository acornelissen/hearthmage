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


def test_prune_temp_drops_old_rows(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    store.record_temp("1", 20.0, 21.0, ts=100.0)
    store.record_temp("1", 20.5, 21.0, ts=500.0)
    store.prune_temp(before_ts=300.0)
    series = store.temp_series("1")
    assert [r["ts"] for r in series] == [500.0]  # the old row is gone


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
