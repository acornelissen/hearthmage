import pytest

from hearthmage.domain import Room, Scene, HearthError
from hearthmage.fake_client import FakeHearthClient


def make_client() -> FakeHearthClient:
    return FakeHearthClient(
        [
            Room("1", "Living Room", 19.5, 21.0, is_off=False, min_temp=5.0, max_temp=30.0),
            Room("2", "Bedroom", 17.0, 18.0, is_off=False, min_temp=5.0, max_temp=30.0),
        ]
    )


def test_list_rooms_returns_seeded_rooms():
    client = make_client()
    rooms = client.list_rooms()
    assert [r.name for r in rooms] == ["Living Room", "Bedroom"]


def test_set_temperature_updates_target():
    client = make_client()
    client.set_temperature("1", 22.5)
    room = next(r for r in client.list_rooms() if r.id == "1")
    assert room.target_temp == 22.5


def test_set_temperature_unknown_room_raises():
    client = make_client()
    with pytest.raises(HearthError):
        client.set_temperature("99", 20.0)


def test_standby_membership_marks_room_off():
    client = make_client()
    client.set_scene_membership("1", Scene.STANDBY, True)
    room = next(r for r in client.list_rooms() if r.id == "1")
    assert room.is_off is True
    client.set_scene_membership("1", Scene.STANDBY, False)
    room = next(r for r in client.list_rooms() if r.id == "1")
    assert room.is_off is False


def test_empty_list_yields_no_rooms():
    client = FakeHearthClient([])
    assert client.list_rooms() == []


def test_preset_scene_is_noop_on_fake():
    client = FakeHearthClient(
        [Room("1", "Living Room", 19.5, 21.0, is_off=False, min_temp=5.0, max_temp=30.0)]
    )
    client.set_scene_membership("1", Scene.BOOST, True)
    room = client.list_rooms()[0]
    assert room.is_off is False
