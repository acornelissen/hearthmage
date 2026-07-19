from hearthmage.energy_store import EnergyStore


def test_unknown_zone_is_none(tmp_path):
    store = EnergyStore(str(tmp_path / "energy.json"))
    assert store.get_zone("1") is None
    assert store.get_all() == {}


def test_set_and_get_zone(tmp_path):
    store = EnergyStore(str(tmp_path / "energy.json"))
    store.set_zone("1", 1000, [12, 40], [300, 280], "2026-07-19T10:00:00+00:00")
    got = store.get_zone("1")
    assert got["rated_watts"] == 1000
    assert got["daily"] == [12, 40]
    assert got["monthly"] == [300, 280]
    assert got["fetched_at"] == "2026-07-19T10:00:00+00:00"


def test_persists_across_reload(tmp_path):
    path = str(tmp_path / "energy.json")
    EnergyStore(path).set_zone("2", 800, [5], [60], "2026-07-19T09:00:00+00:00")
    assert EnergyStore(path).get_zone("2")["rated_watts"] == 800


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "energy.json"
    path.write_text("{ not json")
    store = EnergyStore(str(path))
    assert store.get_all() == {}  # falls back to empty, does not crash
