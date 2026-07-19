from hearthmage.sync_store import SyncStore


def test_unknown_zone_is_none(tmp_path):
    store = SyncStore(str(tmp_path / "sync.json"))
    assert store.get("1") is None


def test_set_and_get_persists(tmp_path):
    path = str(tmp_path / "sync.json")
    SyncStore(path).set("1", "failed")
    assert SyncStore(path).get("1") == "failed"  # survives reload


def test_failed_zones_lists_only_failed(tmp_path):
    store = SyncStore(str(tmp_path / "sync.json"))
    store.set("1", "failed")
    store.set("2", "synced")
    store.set("3", "failed")
    assert sorted(store.failed_zones()) == ["1", "3"]


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "sync.json"
    path.write_text("{ not json")
    store = SyncStore(str(path))
    assert store.get("1") is None  # falls back to empty, does not crash
