from hearthmage.holiday_store import HolidayStore


def test_unknown_zone_is_none(tmp_path):
    store = HolidayStore(str(tmp_path / "holiday.json"))
    assert store.get("1") is None


def test_set_get_and_persist(tmp_path):
    path = str(tmp_path / "holiday.json")
    HolidayStore(path).set("1", day=5, month=11, temp=7.0, prev_setpoint=21.0)
    got = HolidayStore(path).get("1")
    assert got == {"day": 5, "month": 11, "temp": 7.0, "prev_setpoint": 21.0}


def test_clear_removes_zone(tmp_path):
    store = HolidayStore(str(tmp_path / "holiday.json"))
    store.set("1", day=1, month=1, temp=7.0, prev_setpoint=None)
    store.clear("1")
    assert store.get("1") is None


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "holiday.json"
    path.write_text("{ not json")
    assert HolidayStore(str(path)).get("1") is None
