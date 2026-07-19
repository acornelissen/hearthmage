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
