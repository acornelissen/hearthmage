import pytest

from hearthmage.schedule import Block
from hearthmage.schedule_store import ScheduleStore


def test_unknown_zone_is_empty(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    assert store.get_zone("1") == {}
    assert store.get_day("1", 0) == []


def test_set_pattern_applies_blocks_to_each_day(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    blocks = [Block(20, 7 * 60, 9 * 60), Block(16, 9 * 60, 22 * 60)]
    store.set_pattern("1", blocks, [0, 2, 4])  # Mon, Wed, Fri
    for day in (0, 2, 4):
        assert store.get_day("1", day) == blocks
    assert store.get_day("1", 1) == []  # untouched day stays empty
    assert sorted(store.get_zone("1")) == [0, 2, 4]


def test_set_pattern_overwrites_a_day(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_pattern("1", [Block(18, 0, 600)], [1])
    store.set_pattern("1", [Block(21, 300, 900)], [1])
    assert store.get_day("1", 1) == [Block(21, 300, 900)]


def test_set_pattern_requires_days(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    with pytest.raises(ValueError):
        store.set_pattern("1", [Block(20, 0, 600)], [])


def test_clear_day_removes_it(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_pattern("1", [Block(20, 0, 600)], [0, 1])
    store.clear_day("1", 0)
    assert store.get_day("1", 0) == []
    assert store.get_day("1", 1) == [Block(20, 0, 600)]  # other day survives


def test_persists_across_reload(tmp_path):
    path = str(tmp_path / "schedules.json")
    first = ScheduleStore(path)
    first.set_pattern("2", [Block(19, 6 * 60, 8 * 60)], [5, 6])  # weekend
    reloaded = ScheduleStore(path)
    assert reloaded.get_day("2", 5) == [Block(19, 6 * 60, 8 * 60)]
    assert reloaded.get_day("2", 6) == [Block(19, 6 * 60, 8 * 60)]


def test_rejects_invalid_block_times(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    with pytest.raises(ValueError):
        store.set_pattern("1", [Block(20, 600, 600)], [0])  # start == end
    with pytest.raises(ValueError):
        store.set_pattern("1", [Block(20, 600, 300)], [0])  # end before start


def test_active_flag_persists_and_does_not_leak_as_a_zone(tmp_path):
    path = str(tmp_path / "schedules.json")
    store = ScheduleStore(path)
    assert store.get_active("1") is None  # unset
    store.set_pattern("1", [Block(20, 0, 600)], [0])
    store.set_active("1", True)
    assert store.get_active("1") is True
    # the reserved active key must not surface as schedule data for a zone
    assert store.get_zone("1") == {0: [Block(20, 0, 600)]}
    assert store.get_zone("__active__") == {}
    # survives reload
    reloaded = ScheduleStore(path)
    assert reloaded.get_active("1") is True
    reloaded.set_active("1", False)
    assert ScheduleStore(path).get_active("1") is False


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text("{ not json")
    store = ScheduleStore(str(path))
    assert store.get_zone("1") == {}  # falls back to empty, does not crash
