import pytest

from hearthmage.domain import HearthError
from hearthmage.nexho_client import (
    NexhoLocalClient,
    _decode_setpoint,
    _encode_setpoint,
    _parse_status,
)


@pytest.fixture(autouse=True)
def _no_resend_gap(monkeypatch):
    """Resends after ER wait a few seconds on a real hub; not in tests."""
    monkeypatch.setattr("hearthmage.nexho_client._ER_GAP", 0.0)
    monkeypatch.setattr("hearthmage.nexho_client._HUB_QUIET_GAP", 0.0)
    monkeypatch.setattr("hearthmage.nexho_client._SETPOINT_MIN_GAP", 0.0)  # its tests set it


@pytest.fixture(autouse=True)
def _fresh_hub_wires(monkeypatch):
    """Clients of one hub address share its wire; each test starts with none."""
    monkeypatch.setattr("hearthmage.nexho_client._wires", {}, raising=False)


class _NoNetwork:
    """Stands in for socket.socket: a test that reaches the real UDP layer
    without its own fake fails instead of sending to a LAN address."""

    def __init__(self, *a, **k):
        raise AssertionError("a test reached the real network; use _fake_hub or stub _ask")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    monkeypatch.setattr("hearthmage.nexho_client.socket.socket", _NoNetwork)


def make_client(responses: dict[str, str], names=None) -> NexhoLocalClient:
    """A client whose UDP layer is replaced by a canned-response lookup. Zones
    1..16 stand in as if from a readings cache until the hub lists its own."""
    client = NexhoLocalClient("10.0.0.99", zone_names=names or {})
    client._cached_zones = list(range(1, 17))

    def fake_ask(cmd: str, retries: int, timeout: float, priority: bool = False, **_) -> str:
        for prefix, resp in responses.items():
            if cmd.startswith(prefix):
                return resp
        raise HearthError(f"no canned response for {cmd!r}")

    client._ask = fake_ask  # type: ignore[method-assign]
    return client


def test_parse_status_temp_and_setpoint():
    assert _parse_status("OK,20,04,007") == (20.4, 7.0)
    assert _parse_status("OPOK,18,00,015") == (18.0, 15.0)
    assert _parse_status("OK,21,05,012") == (21.5, 12.0)


def test_parse_status_unreachable_returns_none():
    assert _parse_status("ER,1") == (None, None)
    assert _parse_status("ER") == (None, None)


def test_encode_setpoint_whole_half_and_off():
    assert _encode_setpoint(20.0) == 20  # whole degrees pass through
    assert _encode_setpoint(20.5) == 148  # half degree: temp + 127.5
    assert _encode_setpoint(5.5) == 133
    assert _encode_setpoint(0.0) == 0  # off
    assert _encode_setpoint(-3) == 0  # anything <= 0 is off


def test_decode_setpoint_inverts_encode():
    assert _decode_setpoint(20) == 20.0
    assert _decode_setpoint(148) == 20.5
    assert _decode_setpoint(133) == 5.5
    assert _decode_setpoint(0) == 0.0  # off reads back as 0


def test_parse_status_decodes_half_degree_setpoint():
    assert _parse_status("OK,20,04,148") == (20.4, 20.5)  # setpoint 148 -> 20.5
    assert _parse_status("OK,19,00,000") == (19.0, 0.0)  # setpoint 0 -> off


def test_set_temperature_off_marks_zone_off():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._cached_zones = list(range(1, 17))  # as if from a readings cache
    client._ask = lambda cmd, r, t, priority=False, **_: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    client.set_temperature("2", 0)  # Off
    room = next(r for r in client.list_rooms() if r.id == "2")
    assert room.is_off is True
    assert room.target_temp == 1.0
    _queue_write(client, 2, 1.0)
    client._drain_writes(2)
    assert sent[-1] == "D#2#1#0#0*T1/"  # off sends a 1 degree setpoint, not *T0


def test_set_temperature_half_degree_rounds_to_nearest_half():
    client = make_client({"D#": "OK"})
    client.set_temperature("3", 20.6)
    room = next(r for r in client.list_rooms() if r.id == "3")
    assert room.target_temp == 20.5  # nearest 0.5, not integer
    assert room.is_off is False


def test_send_once_encodes_half_degree_on_the_wire():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, r, t, priority=False, **_: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    assert client._send_once(3, 20.5) is True
    assert sent == ["D#3#1#0#0*T148/"]


def test_poll_zone_parses_and_names():
    client = make_client({"R#2#": "OK,20,04,007"}, names={"2": "Bedroom"})
    client._poll_zone(2)
    room = client._state[2]
    assert room.name == "Bedroom"
    assert room.current_temp == 20.4
    assert room.target_temp == 7.0
    assert room.status == "ok"


def test_poll_zone_unreachable_status():
    client = make_client({"R#3#": "ER,1"})
    client._poll_zone(3)
    room = client._state[3]
    assert room.status == "unreachable"
    assert room.current_temp is None
    assert room.target_temp is None


def test_poll_zone_falls_back_to_last_known():
    client = make_client({"R#2#": "OK,20,04,007"})
    client._poll_zone(2)  # success caches the reading
    client._ask = lambda cmd, retries, timeout, priority=False: "ER,1"  # type: ignore[method-assign]
    client._poll_zone(2)
    room = client._state[2]
    assert room.status == "ok"  # shows last known instead of blanking
    assert room.current_temp == 20.4
    assert room.target_temp == 7.0


def test_poll_zone_keeps_pending_target_while_unreachable():
    client = make_client({"R#2#": "ER,1"})
    with client._lock:
        client._last[2] = (19.0, 18.0)
        client._pending[2] = 22.0  # user just set 22, radiator asleep
    client._poll_zone(2)
    room = client._state[2]
    assert room.target_temp == 22.0  # poll must not revert the pending target
    assert room.current_temp == 19.0  # last-known room temp


def test_poll_zone_fresh_read_clears_pending():
    client = make_client({"R#2#": "OK,20,04,022"})
    with client._lock:
        client._pending[2] = 22.0
    client._poll_zone(2)  # radiator answers - ground truth reconciles
    assert 2 not in client._pending
    assert client._state[2].target_temp == 22.0


def test_poll_then_list_rooms_serves_cache():
    client = make_client(
        {"OPS2/": "OPOK,OPS2,1,2", "R#1#": "OK,20,04,007", "R#2#": "ER,1"},
        names={"1": "Lounge"},
    )
    client._poll_once()
    rooms = client.list_rooms()
    assert [r.id for r in rooms] == ["1", "2"]
    assert rooms[0].name == "Lounge"
    assert rooms[0].current_temp == 20.4
    assert rooms[0].status == "ok"
    assert rooms[1].status == "unreachable"


def test_the_first_zone_list_seeds_loading_rooms():
    client = make_client({"OPS2/": "OPOK,OPS2,1,2"})
    seen: list[list[str]] = []

    def read(zone):
        if not seen:
            seen.append([r.status for r in client.list_rooms()])
        return None, None, False

    client._read_zone_raw = read  # type: ignore[method-assign]
    client._poll_once()
    assert seen == [["loading", "loading"]]  # shown while the first reads run


def test_list_rooms_never_talks_to_the_hub():
    # heatapp-local-88h. Cold start, no readings cache, hub down: list_rooms
    # runs on page requests and htmx polls, so it must not wait on the wire
    # (up to 12 s each). It serves what the poller knows, here nothing yet.
    asked: list[str] = []

    def down(cmd, *a, **k):
        asked.append(cmd)
        raise HearthError("hub down")

    client = NexhoLocalClient("10.0.0.99")
    client._ask = down  # type: ignore[method-assign]
    assert client.list_rooms() == []
    assert asked == []
    assert client.connecting() is True  # the UI says so instead of "no rooms"


def test_connecting_ends_with_the_first_zone_list():
    client = make_client({"OPS2/": "OPOK,OPS2,1,2", "R#": "ER,1"})
    client._poll_once()
    assert client.connecting() is False
    assert [r.id for r in client.list_rooms()] == ["1", "2"]


def test_zone_list_cached_and_resilient():
    client = make_client({"OPS2/": "OPOK,OPS2,1,2,0,9"})
    assert client._zone_ids() == [1, 2]

    def boom(cmd, retries, timeout):
        raise HearthError("hub down")

    client._ask = boom  # type: ignore[method-assign]
    assert client._zone_ids() == [1, 2]  # falls back to cached list


def test_set_temperature_optimistic_update_is_instant():
    client = make_client({"D#": "OK"})
    client.set_temperature("3", 21.0)  # background send returns OK
    room = next(r for r in client.list_rooms() if r.id == "3")
    assert room.target_temp == 21.0  # reflected immediately, before the send
    assert room.status == "ok"


def test_set_temperature_rounds_to_nearest_half_degree():
    client = make_client({"D#": "OK"})
    client.set_temperature("3", 20.7)
    room = next(r for r in client.list_rooms() if r.id == "3")
    assert room.target_temp == 20.5


def _queue_write(client, zone: int, target: int) -> None:
    """Seed a zone as if set_temperature had run, without spawning the thread."""
    with client._lock:
        client._pending[zone] = float(target)
        client._desired[zone] = target
        client._writers.add(zone)


def test_send_once_sends_the_command():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, retries, timeout, priority=False, **_: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    assert client._send_once(3, 21) is True
    assert sent == ["D#3#1#0#0*T21/"]


def test_drain_confirms_on_ok():
    client = make_client({"D#": "OK"})
    with client._lock:
        client._state[2] = client._room(2, 19.0, None, "ok")
    _queue_write(client, 2, 21)
    client._drain_writes(2)
    assert client._last[2] == (19.0, 21.0)
    assert 2 not in client._pending  # confirmed write releases the poll guard
    assert 2 not in client._writers  # worker cleaned up
    room = next(r for r in client.list_rooms() if r.id == "2")
    assert room.target_temp == 21.0
    assert room.error is None


def test_rapid_writes_coalesce_to_latest_target():
    """A newer tap arriving mid-send supersedes the in-flight one: the radiator
    receives the latest value last, and only that value is confirmed."""
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")

    def fake_ask(cmd, retries, timeout, priority=False, **_):
        sent.append(cmd)
        if cmd == "D#2#1#0#0*T20/":  # user bumps to 22 while 20 is in flight
            with client._lock:
                client._desired[2] = 22
        return "OK"

    client._ask = fake_ask  # type: ignore[method-assign]
    _queue_write(client, 2, 20)
    client._drain_writes(2)
    assert sent == ["D#2#1#0#0*T20/", "D#2#1#0#0*T22/"]  # latest sent last
    assert client._last[2][1] == 22.0  # only the final value is confirmed
    assert 2 not in client._pending
    assert 2 not in client._desired
    assert 2 not in client._writers


def test_concurrent_set_does_not_spawn_second_worker():
    client = make_client({"D#": "OK"})
    with client._lock:
        client._writers.add(2)  # a worker is already draining zone 2
    client.set_temperature("2", 22.0)
    with client._lock:
        assert client._desired[2] == 22  # request recorded for the running worker
        assert client._writers == {2}  # no second worker started


def test_invalid_zone_id_raises():
    client = NexhoLocalClient("10.0.0.99")
    with pytest.raises(HearthError):
        client.set_temperature("not-a-zone", 20)


def test_read_timeout_captures_hub_error_response():
    # The hub takes ~5s to return ER for an asleep zone; a shorter read timeout
    # fires repeated blocking RF attempts instead of capturing the ER once.
    from hearthmage import nexho_client

    assert nexho_client._READ_TIMEOUT >= 6.0


def test_unreachable_zone_backs_off_and_resets_on_fresh_read():
    client = make_client({"OPS2/": "OPOK,OPS2,1", "R#1#": "ER,1"})
    client._poll_once()  # zone 1 unreachable
    first = client._next_poll[1] - client._backoff.get(1, 0)  # ~ now baseline
    backoff1 = client._backoff[1]
    client._next_poll[1] = 0.0  # make it due again
    client._poll_once()  # unreachable again -> backoff doubles
    assert client._backoff[1] == pytest.approx(backoff1 * 2)

    # A fresh reading resets the backoff to the normal interval.
    client._ask = lambda cmd, retries, timeout, priority=False: "OK,20,00,007"  # type: ignore[method-assign]
    client._next_poll[1] = 0.0
    client._poll_once()
    assert client._backoff[1] == client._poll_interval
    _ = first


def test_poll_once_skips_zones_not_yet_due():
    calls: list[str] = []

    client = make_client({"OPS2/": "OPOK,OPS2,1", "R#1#": "OK,20,00,007"})
    real_read = client._read_zone_raw

    def counting_read(zone):
        calls.append(f"R{zone}")
        return real_read(zone)

    client._read_zone_raw = counting_read  # type: ignore[method-assign]
    client._poll_once()  # zone 1 read once, next_poll pushed into the future
    assert calls == ["R1"]
    client._poll_once()  # not due yet -> skipped, hub left free
    assert calls == ["R1"]


def test_poll_zone_keeps_pending_while_write_in_flight():
    # A read that samples the OLD setpoint (18) must not revert the target a
    # write is mid-flight to (22), or the user's change would be lost on no-ack.
    client = make_client({"R#2#": "OK,19,00,018"})
    with client._lock:
        client._pending[2] = 22.0
        client._writers.add(2)  # a write to 22 is in flight
        client._last[2] = (19.0, 18.0)
    client._poll_zone(2)
    room = client._state[2]
    assert room.target_temp == 22.0  # not reverted to the read's 18
    assert 2 in client._pending  # guard preserved for the write to reconcile
    assert room.current_temp == 19.0  # room temp still refreshed from the read


def test_drain_writes_releases_worker_on_unexpected_error():
    client = make_client({})
    with client._lock:
        client._desired[2] = 20
        client._writers.add(2)

    def boom(zone, target):
        raise RuntimeError("fd exhausted")

    client._send_once = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        client._drain_writes(2)
    assert 2 not in client._writers  # not left wedged -> future writes can spawn


def test_ask_converts_socket_creation_error_to_hearth_error(monkeypatch):
    client = NexhoLocalClient("10.0.0.99")

    def boom(*a, **k):
        raise OSError("too many open files")

    monkeypatch.setattr("hearthmage.nexho_client.socket.socket", boom)
    with pytest.raises(HearthError):  # not a raw OSError escaping to the caller
        client._ask("OPS2/", 2, 0.1)


def test_set_temperature_clamps_out_of_range():
    client = make_client({"D#": "OK"})
    client.set_temperature("3", 999)
    room = next(r for r in client.list_rooms() if r.id == "3")
    assert room.target_temp == 30.0  # clamped to _MAX_TEMP, not sent raw


def test_set_temperature_rejects_non_finite():
    client = make_client({"D#": "OK"})
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(HearthError):
            client.set_temperature("3", bad)


def test_writes_preempt_reads_on_the_wire():
    import threading

    client = NexhoLocalClient("10.0.0.99")
    events: list[str] = []

    # A write holds the wire; a reader must wait until the write releases it.
    client._wire_acquire(priority=True)

    reader_done = threading.Event()

    def reader():
        client._wire_acquire(priority=False)
        events.append("read-acquired")
        client._wire_release(priority=False)
        reader_done.set()

    t = threading.Thread(target=reader)
    t.start()
    # Give the reader a moment; it must NOT have acquired the wire yet.
    assert not reader_done.wait(0.2)
    assert events == []

    events.append("write-releasing")
    client._wire_release(priority=True)
    assert reader_done.wait(1.0)  # reader proceeds once the write is done
    assert events == ["write-releasing", "read-acquired"]
    t.join()


def test_energy_poll_step_invokes_callback_when_due():
    seen = []
    client = make_client(
        {
            "OPS2/": "OPOK,OPS2,1",
            "R#1#1#0#0*?T/": "OK,20,00,007",
            "R#1#1#0#0*?UD/": "OK,1000,12,40,0,0,0,0,0,0",  # eight daily buckets
            "R#1#1#0#0*?UM/": "OK,1000,300",
        }
    )
    client._on_energy = lambda zone, data: seen.append((zone, data))
    client._energy_interval = 3600.0
    client._next_energy = 0.0  # due now
    client._poll_once()  # temperature sweep + one energy read
    assert seen == [(1, {"rated_watts": 1000, "daily": [12, 40, 0, 0, 0, 0, 0, 0], "monthly": [300]})]


def test_energy_poll_step_skips_when_no_callback():
    client = make_client({"OPS2/": "OPOK,OPS2,1", "R#1#1#0#0*?T/": "OK,20,00,007"})
    client._next_energy = 0.0
    client._poll_once()  # must not attempt energy reads without a callback
    assert client._energy_queue == []


def _holiday_client(reply: str = "OK"):
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._cached_zones = list(range(1, 17))  # as if from a readings cache
    client._ask = lambda cmd, *a, **k: sent.append(cmd) or reply  # type: ignore[method-assign]
    return client, sent


def test_set_holiday_sends_days_hour_minute_and_arrival_temp():
    # Vendor (AdvConfigHoliday): *RH#<days from today>#<hour>#<minute>#<arrival temp>
    client, sent = _holiday_client()
    client.set_holiday("1", days=4, hour=17, minute=30, arrival_temp=20.0)
    assert sent == ["D#1#1#0#0*RH#4#17#30#20/"]


def test_set_holiday_half_degree_and_resume_program():
    client, sent = _holiday_client()
    client.set_holiday("2", days=1, hour=8, minute=0, arrival_temp=20.5)
    client.set_holiday("2", days=1, hour=8, minute=0, arrival_temp=None)
    assert sent == ["D#2#1#0#0*RH#1#8#0#148/", "D#2#1#0#0*RH#1#8#0#251/"]  # 251 = resume program


@pytest.mark.parametrize("kwargs", [
    {"days": 241, "hour": 8, "minute": 0, "arrival_temp": 20.0},
    {"days": -1, "hour": 8, "minute": 0, "arrival_temp": 20.0},
    {"days": 3, "hour": 24, "minute": 0, "arrival_temp": 20.0},
    {"days": 3, "hour": 8, "minute": 60, "arrival_temp": 20.0},
    {"days": 3, "hour": 8, "minute": 0, "arrival_temp": 3.0},
])
def test_set_holiday_rejects_bad_values(kwargs):
    client, sent = _holiday_client()
    with pytest.raises(HearthError):
        client.set_holiday("1", **kwargs)
    assert sent == []


def test_clear_holiday_sends_the_vendor_clear():
    client, sent = _holiday_client()
    client.clear_holiday("1")
    assert sent == ["D#1#1#0#0*RH#0#0#0#251/"]


def test_set_holiday_needs_a_bare_ok(monkeypatch):
    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    for reply in ("ER,1", "OK,1"):
        client, _ = _holiday_client(reply)
        with pytest.raises(HearthError):
            client.set_holiday("1", days=4, hour=17, minute=30, arrival_temp=20.0)


def test_read_holiday_follows_the_vendor_fields():
    # [10] days left (<= 240 = a hold is set), [11] hour, [12] minute, [13] arrival temp
    live_none = "OK,RH,14,5,5,22,1,47,6,11,253,0,0,253,6,1,0,0,0,0,0,0,0,0,0"  # 2026-11-01
    client = make_client({"R#1#1#0#0*?RH/": live_none})
    assert client.read_holiday("1") == {"active": False}
    held = make_client({"R#1#1#0#0*?RH/": "OK,RH,14,5,5,22,1,47,6,11,4,17,30,148,6,1,0"})
    assert held.read_holiday("1") == {"active": True, "days_left": 4, "hour": 17, "minute": 30,
                                      "arrival_temp": 20.5}
    program = make_client({"R#1#1#0#0*?RH/": "OK,RH,14,5,5,22,1,47,6,11,0,9,0,251,6,1,0"})
    assert program.read_holiday("1")["arrival_temp"] is None  # resume the weekly program


def test_read_config_decodes_every_setting():
    client = make_client(
        {
            "R#1#1#0#0*?E#0#8/": "OK,129",  # keypad locked (bit0), bit7 set
            "R#1#1#0#0*?E#0#6/": "OK,0",  # window sensor off
            "R#1#1#0#0*?E#1#20/": "OK,7",  # anti-ice 7 C (live default)
            "R#1#1#0#0*?E#0#9/": "OK,251",  # sensor offset: signed tenths, -0.5 C
        }
    )
    cfg = client.read_config("1")
    assert cfg == {"keypad_lock": True, "window_sensor": False, "frost_temp": 7, "sensor_offset": -0.5}


def test_read_config_reports_an_unread_setting_as_unknown():
    # The keypad read works but the radiator then misses its RF window. A failed
    # read must not look like "off" or a default, or saving would write it back.
    client = make_client(
        {
            "R#1#1#0#0*?E#0#8/": "OK,1",
            "R#1#1#0#0*?E#0#6/": "ER,1",
            "R#1#1#0#0*?E#1#20/": "ER,1",
            "R#1#1#0#0*?E#0#9/": "ER,1",
        }
    )
    assert client.read_config("1") == {
        "keypad_lock": True, "window_sensor": None, "frost_temp": None, "sensor_offset": None,
    }


def test_read_config_none_when_asleep():
    client = make_client({"R#3#1#0#0*?E#0#8/": "ER,1"})
    assert client.read_config("3") is None


def _lagging_hub(truth: dict[str, int], stale: int = 5):
    """A hub that answers each settings read with the value of the query before
    it, as the real one does when a query follows its last reply too closely
    (live 2026-11-11). ``stale`` is what its buffer holds at the start."""
    client = NexhoLocalClient("10.0.0.99")
    client._cached_zones = list(range(1, 17))  # as if from a readings cache
    sent: list[str] = []
    held = {"value": stale}

    def ask(cmd, *a, **k):
        sent.append(cmd)
        if "*?E#" in cmd:
            reply = f"OK,{held['value']}"
            held["value"] = truth[cmd.split("*?E#")[1].rstrip("/")]
            return reply
        return "OK"

    client._ask = ask  # type: ignore[method-assign]
    return client, sent


def test_read_config_is_not_fooled_by_a_reply_one_query_behind():
    # Live 2026-11-11: the page showed anti-ice 0 and offset 0.7 for radiators
    # whose real values were 7 and 0, because each answer was the one before.
    client, _ = _lagging_hub({"0#8": 0, "0#6": 0, "1#20": 7, "0#9": 0})
    assert client.read_config("1") == {
        "keypad_lock": False, "window_sensor": False, "frost_temp": 7, "sensor_offset": 0.0,
    }


def test_read_config_reports_a_setting_whose_reads_disagree_as_unknown():
    client = NexhoLocalClient("10.0.0.99")
    client._cached_zones = list(range(1, 17))  # as if from a readings cache
    frost = iter([3, 9, 4, 8, 2, 6])

    def ask(cmd, *a, **k):
        return f"OK,{next(frost)}" if "*?E#1#20" in cmd else "OK,0"

    client._ask = ask  # type: ignore[method-assign]
    assert client.read_config("1") == {
        "keypad_lock": False, "window_sensor": False, "frost_temp": None, "sensor_offset": 0.0,
    }


def test_keypad_write_is_based_on_an_agreed_read():
    # The keypad byte is read, changed and written back; a reply one query
    # behind must not get its bit 7 written to the radiator.
    client, sent = _lagging_hub({"0#8": 128}, stale=0)
    client.set_config("2", keypad_lock=True)
    assert "D#2#1#0#0*SEP#0#8#129/" in sent


def test_ask_leaves_the_hub_a_quiet_gap_between_exchanges(monkeypatch):
    """A query sent straight after the hub's last reply is answered with that
    reply's value (live 2026-11-11), so every exchange waits out a short gap."""
    import hearthmage.nexho_client as nc

    clock = {"now": 100.0}
    events: list[tuple] = []

    class FakeSocket:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            pass

        def send(self, data):
            events.append(("send", clock["now"]))

        def recv(self, n):
            clock["now"] += 0.04  # the hub takes about 40 ms to answer
            return b"OK,7\x00"

        def close(self):
            pass

    def fake_sleep(seconds):
        events.append(("sleep", round(seconds, 3)))
        clock["now"] += seconds

    monkeypatch.setattr(nc, "_HUB_QUIET_GAP", 0.1)
    monkeypatch.setattr(nc.socket, "socket", FakeSocket)
    monkeypatch.setattr(nc.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(nc.time, "sleep", fake_sleep)
    client = NexhoLocalClient("10.0.0.99")
    client._ask("R#1#1#0#0*?E#0#6/", 1, 0.1)  # first exchange: nothing to wait for
    client._ask("R#1#1#0#0*?E#1#20/", 1, 0.1)  # straight after: waits the gap
    clock["now"] += 5.0
    client._ask("R#1#1#0#0*?T/", 1, 0.1)  # the hub has been quiet: no wait
    sends = [t for kind, t in events if kind == "send"]
    assert [e for e in events if e[0] == "sleep"] == [("sleep", 0.1)]
    assert sends[1] - (sends[0] + 0.04) == pytest.approx(0.1)


def test_a_retry_after_a_timeout_waits_the_quiet_gap(monkeypatch):
    """A reply we never read still poisons the next query: live 2026-11-26, a
    query sent within 50 ms of an abandoned one got the abandoned one's value
    in 10 of 18 tries, and 0 of 18 from 100 ms on. A late reply can land just
    as our timeout fires, so the retry after a timeout waits the gap too."""
    import hearthmage.nexho_client as nc

    clock = {"now": 100.0}
    events: list[tuple] = []
    answers = iter([None, b"OK,7\x00"])  # first try times out, retry answers

    class FakeSocket:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            self.timeout = t

        def connect(self, addr):
            pass

        def send(self, data):
            events.append(("send", clock["now"]))

        def recv(self, n):
            answer = next(answers)
            if answer is None:
                clock["now"] += self.timeout
                raise nc.socket.timeout()
            clock["now"] += 0.04
            return answer

        def close(self):
            pass

    def fake_sleep(seconds):
        events.append(("sleep", round(seconds, 3)))
        clock["now"] += seconds

    monkeypatch.setattr(nc, "_HUB_QUIET_GAP", 0.1)
    monkeypatch.setattr(nc.socket, "socket", FakeSocket)
    monkeypatch.setattr(nc.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(nc.time, "sleep", fake_sleep)
    client = NexhoLocalClient("10.0.0.99")
    assert client._ask("R#1#1#0#0*?E#1#20/", 2, 6.0) == "OK,7"
    sends = [t for kind, t in events if kind == "send"]
    assert sends[1] - (sends[0] + 6.0) == pytest.approx(0.1)


def _recording_client(keypad_raw: str = "OK,0"):
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._cached_zones = list(range(1, 17))  # as if from a readings cache

    def ask(cmd, *a, **k):
        sent.append(cmd)
        return keypad_raw if "*?E#0#8" in cmd else "OK"

    client._ask = ask  # type: ignore[method-assign]
    return client, sent


def test_set_config_writes_frost_and_offset_like_the_vendor():
    client, sent = _recording_client()
    client.set_config("2", frost_temp=9, sensor_offset=-1.5)
    assert "D#2#1#0#0*SEP#1#20#9/" in sent  # anti-ice 9 C
    assert "R#2#1#0#0*SEP#0#9#241/" in sent  # -1.5 C -> -15 & 255; vendor uses R# here
    assert not any("0#6" in c or "0#8" in c for c in sent)  # untouched settings


def test_keypad_write_keeps_bit_7():
    client, sent = _recording_client(keypad_raw="OK,129")
    client.set_config("2", keypad_lock=False)
    assert "D#2#1#0#0*SEP#0#8#128/" in sent
    client, sent = _recording_client(keypad_raw="OK,0")
    client.set_config("2", keypad_lock=True)
    assert "D#2#1#0#0*SEP#0#8#1/" in sent


@pytest.mark.parametrize("kwargs", [{"frost_temp": 18}, {"frost_temp": -1},
                                    {"sensor_offset": 10.0}, {"sensor_offset": -9.95}])
def test_set_config_rejects_out_of_range(kwargs):
    client, sent = _recording_client()
    with pytest.raises(HearthError):
        client.set_config("2", **kwargs)
    assert not any("SEP" in c for c in sent)


def test_set_config_raises_when_write_not_acked():
    client = make_client({"D#2#1#0#0*SEP#0#6#1/": "ER,1"})
    with pytest.raises(HearthError):
        client.set_config("2", window_sensor=True)


def test_set_config_requires_a_bare_ok():
    # The vendor treats "OK,<...>" as a command error for writes.
    client = make_client({"D#2#1#0#0*SEP#0#6#1/": "OK,1"})
    with pytest.raises(HearthError):
        client.set_config("2", window_sensor=True)

def test_read_energy_reads_counters_and_never_resets_them():
    """*UDEL is the vendor app's consumption RESET (behind a "confirm reset"
    dialog), not a probe: reading energy must never send it."""
    sent: list[str] = []
    client = make_client(
        {
            "R#1#1#0#0*?UD/": "OK,1000,12,40,35,50,0,22,18,7",
            "R#1#1#0#0*?UM/": "OK,1000,300,280,260",
        }
    )
    real = client._ask
    client._ask = lambda cmd, *a, **k: (sent.append(cmd), real(cmd, *a, **k))[1]  # type: ignore[method-assign]
    data = client.read_energy("1")
    assert data["rated_watts"] == 1000
    assert data["daily"] == [12, 40, 35, 50, 0, 22, 18, 7]
    assert data["monthly"] == [300, 280, 260]
    assert not any("UDEL" in cmd for cmd in sent)


def test_read_energy_none_when_unreadable():
    client = make_client({"R#3#1#0#0*?UD/": "ER,1", "R#3#1#0#0*?UM/": "ER,1"})
    assert client.read_energy("3") is None


def test_hub_stale_false_before_start():
    client = NexhoLocalClient("10.0.0.99")
    assert client.hub_stale() is False  # nothing started yet, nothing to be stale about


def test_hub_stale_after_successful_zone_list_is_false():
    client = make_client({"OPS2/": "OPOK,OPS2,1,2"})
    client._zone_ids()  # records last_ops2_ok
    assert client.hub_stale() is False


def test_hub_stale_true_when_contact_is_old():
    import time as _time

    client = make_client({"OPS2/": "OPOK,OPS2,1"})
    client._zone_ids()
    # Backdate the last contact well past the stale window.
    client._last_ops2_ok = _time.monotonic() - (client._stale_after + 10)
    assert client.hub_stale() is True


def test_health_reports_zones_and_poller():
    client = make_client({"OPS2/": "OPOK,OPS2,1,2", "R#1#": "OK,20,04,007", "R#2#": "ER,1"})
    client._poll_once()
    report = client.health()
    assert report["poller_alive"] is False  # start() not called in this test
    assert report["hub_last_contacted_seconds_ago"] is not None
    assert report["zones"]["1"]["status"] == "ok"
    assert report["zones"]["1"]["last_fresh_seconds_ago"] is not None
    assert report["zones"]["2"]["status"] == "unreachable"
    assert report["zones"]["2"]["last_fresh_seconds_ago"] is None  # never answered


def test_cache_persists_last_known_across_restarts(tmp_path):
    path = str(tmp_path / "readings.json")
    first = NexhoLocalClient("10.0.0.99", cache_file=path)
    with first._lock:
        first._last[2] = (20.4, 7.0)
    first._save_cache()

    # A fresh client (simulating a restart) shows the last-known reading at once.
    second = NexhoLocalClient("10.0.0.99", cache_file=path)
    room = second._state[2]
    assert room.current_temp == 20.4
    assert room.target_temp == 7.0
    assert room.status == "ok"


def test_set_day_pattern_rejects_invalid_pattern_as_hearth_error():
    from hearthmage.schedule import Block

    sent: list[str] = []
    client = make_client({"D#": "OK"})
    real = client._ask
    client._ask = lambda cmd, *a, **k: (sent.append(cmd), real(cmd, *a, **k))[1]  # type: ignore[method-assign]
    with pytest.raises(HearthError):
        client.set_day_pattern("1", [Block(200, 0, 60)], [0])
    with pytest.raises(HearthError):
        client.set_day_pattern("1", [], [-1])
    assert sent == []  # nothing invalid reaches the wire


def test_stop_ends_the_poller():
    import time

    client = NexhoLocalClient("10.0.0.99", poll_interval=0.01)
    client._poll_once = lambda: None  # type: ignore[method-assign]
    client.start()
    client.stop()
    client._thread.join(timeout=3)
    assert not client._thread.is_alive()


def test_read_day_pattern_uses_the_program_read():
    from hearthmage.schedule import Block

    blocks = "20,7,0,9,0," + ",".join(["5,24,60,24,60"] * 5)
    client = make_client({"R#2#1#0#0*?P3/": f"OK,P3,11,{blocks}"})
    assert client.read_day_pattern("2", 3) == [Block(20, 420, 540)]


def test_read_day_pattern_unreadable_is_hearth_error(monkeypatch):
    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc.time, "sleep", lambda s: None)  # skip the ER retry gaps
    client = make_client({"R#2#1#0#0*?P3/": "ER,1"})
    with pytest.raises(HearthError):
        client.read_day_pattern("2", 3)
    with pytest.raises(HearthError):
        client.read_day_pattern("2", 9)  # not a weekday


def test_cloud_reporting_read_and_write():
    sent: list[str] = []
    client = make_client({"OPMR3898/": "OPOK,1", "OPMW3898,": "OPOK"})
    real = client._ask
    client._ask = lambda cmd, *a, **k: (sent.append(cmd), real(cmd, *a, **k))[1]  # type: ignore[method-assign]
    assert client.read_cloud_reporting() is True
    client.set_cloud_reporting(False)
    client.set_cloud_reporting(True)
    assert sent[1:] == ["OPMW3898,0/", "OPMW3898,1/"]


def test_cloud_reporting_errors_are_hearth_errors():
    client = make_client({"OPMR3898/": "ER", "OPMW3898,": "ER"})
    with pytest.raises(HearthError):
        client.read_cloud_reporting()
    with pytest.raises(HearthError):
        client.set_cloud_reporting(False)


def test_program_commands_retry_a_transient_er(monkeypatch):
    """Live 2026-11-01: a *P write got ER,1 and the same write 4s later got OK."""
    import hearthmage.nexho_client as nc
    from hearthmage.schedule import Block

    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    replies = {"D#": ["ER,1", "OK"], "R#1#1#0#0*?P0/": ["ER,1", "OK,P0,11," + ",".join(["5,24,60,24,60"] * 6)]}
    client = NexhoLocalClient("10.0.0.99")
    client._cached_zones = list(range(1, 17))  # as if from a readings cache
    client._ask = lambda cmd, *a, **k: replies["D#" if cmd.startswith("D#") else cmd].pop(0)  # type: ignore[method-assign]
    client.set_day_pattern("1", [Block(20, 0, 60)], [0])  # does not raise
    assert client.read_day_pattern("1", 0) == []


def test_program_write_gives_up_after_repeated_er(monkeypatch):
    import hearthmage.nexho_client as nc
    from hearthmage.schedule import Block

    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    calls: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._cached_zones = list(range(1, 17))  # as if from a readings cache
    client._ask = lambda cmd, *a, **k: (calls.append(cmd), "ER,1")[1]  # type: ignore[method-assign]
    with pytest.raises(HearthError):
        client.set_program_active("1", True)
    assert 1 < len(calls) <= 5  # retried, but bounded


def test_read_status_raw_returns_the_f_reply(monkeypatch):
    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    live = "OK,14,9,5,21,19,17,6,11,69,0,0,0,0,1,0,44,244,0,33,v200110"  # 2026-11-01
    assert make_client({"R#1#1#0#0*?F/": live}).read_status_raw("1") == live
    with pytest.raises(HearthError):
        make_client({"R#1#1#0#0*?F/": "ER,1"}).read_status_raw("1")


def test_ask_only_listens_to_the_hub(monkeypatch):
    """The socket is connected to the hub, so the OS drops datagrams from any
    other address (a spoofed reply on the LAN cannot answer for the hub)."""
    calls: list[tuple] = []

    class FakeSocket:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            calls.append(("connect", addr))

        def send(self, data):
            calls.append(("send", data))

        def recv(self, n):
            return b"OK\x00"

        def close(self):
            pass

    monkeypatch.setattr("hearthmage.nexho_client.socket.socket", FakeSocket)
    client = NexhoLocalClient("10.0.0.99", port=6653)
    assert client._ask("D#1#1#0#0*T20/", 1, 0.1) == "OK"
    assert calls == [("connect", ("10.0.0.99", 6653)), ("send", b"D#1#1#0#0*T20/")]


def test_setpoint_write_needs_a_bare_ok_and_resends_on_er(monkeypatch):
    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    client = NexhoLocalClient("10.0.0.99")
    _queue_write(client, 1, 20.0)  # only a target still wanted is resent
    client._ask = lambda cmd, *a, **k: "OK,1"  # type: ignore[method-assign]
    assert client._send_once(1, 20.0) is False  # vendor: OK,<...> is a command error
    replies = ["ER,1", "OK"]
    client._ask = lambda cmd, *a, **k: replies.pop(0)  # type: ignore[method-assign]
    assert client._send_once(1, 20.0) is True  # resent after the transient ER


def test_program_write_waits_longer_and_needs_a_bare_ok():
    from hearthmage.nexho_client import _PROGRAM_WRITE_TIMEOUT
    from hearthmage.schedule import Block

    seen: list[float] = []
    client = NexhoLocalClient("10.0.0.99")
    client._cached_zones = list(range(1, 17))  # as if from a readings cache
    client._ask = lambda cmd, retries, timeout, priority=False, **_: seen.append(timeout) or "OK"  # type: ignore[method-assign]
    client.set_day_pattern("1", [Block(20, 0, 60)], [0])
    assert seen == [_PROGRAM_WRITE_TIMEOUT] and _PROGRAM_WRITE_TIMEOUT >= 10
    client._ask = lambda *a, **k: "OK,1"  # type: ignore[method-assign]
    with pytest.raises(HearthError):
        client.set_day_pattern("1", [Block(20, 0, 60)], [0])
    with pytest.raises(HearthError):
        client.set_program_active("1", True)


def test_impossible_readings_are_not_trusted():
    # The vendor re-reads when the room reads above 60 C or the target above 35 C.
    assert _parse_status("OK,85,0,020") == (None, None)
    assert _parse_status("OK,20,0,040") == (None, None)
    assert _parse_status("OK,20,4,148") == (20.4, 20.5)  # half degree still fine


# ---- heaters per zone (OPS3) ------------------------------------------------
# The vendor reads OPS3/ (OPOK,OPS3,<count>,... in OPS2 order), sends each zone's
# heater count in every D# write and hides zones with 0 heaters. Live: 1,1,1,1.


def _multi_heater_client():
    sent: list[str] = []
    replies = {"OPS2/": "OPOK,OPS2,1,2,3", "OPS3/": "OPOK,OPS3,1,0,2"}
    client = NexhoLocalClient("10.0.0.99")

    def ask(cmd, *a, **k):
        sent.append(cmd)
        return replies.get(cmd, "OK")

    client._ask = ask  # type: ignore[method-assign]
    client._zone_ids()
    sent.clear()
    return client, sent


def test_zones_with_no_heaters_are_hidden():
    client, _ = _multi_heater_client()
    assert client._zone_ids() == [1, 3]
    client._poll_once()  # the poller puts the rooms on screen
    assert [r.id for r in client.list_rooms()] == ["1", "3"]


def test_writes_carry_the_zone_heater_count():
    from hearthmage.schedule import Block

    client, sent = _multi_heater_client()
    client._send_once(3, 20.0)
    client.set_day_pattern("3", [Block(20, 0, 60)], [0])
    client.set_program_active("3", True)
    client.set_holiday("3", days=2, hour=8, minute=0, arrival_temp=None)
    client.clear_holiday("3")
    client.set_config("3", window_sensor=True, frost_temp=7)
    writes = [c for c in sent if c.startswith("D#")]
    assert writes and all(c.startswith("D#3#2#0#0*") for c in writes), writes


def test_sensor_offset_is_written_to_each_heater():
    client, sent = _multi_heater_client()
    client.set_config("3", sensor_offset=0.5)
    assert [c for c in sent if "SEP#0#9" in c] == ["R#3#1#0#0*SEP#0#9#5/", "R#3#2#0#0*SEP#0#9#5/"]


def test_unknown_heater_counts_fall_back_to_one():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")

    def ask(cmd, *a, **k):
        sent.append(cmd)
        if cmd == "OPS2/":
            return "OPOK,OPS2,1,2"
        if cmd == "OPS3/":
            raise HearthError("no answer")
        return "OK"

    client._ask = ask  # type: ignore[method-assign]
    assert client._zone_ids() == [1, 2]  # nothing hidden when counts are unknown
    client._send_once(2, 20.0)
    assert "D#2#1#0#0*T20/" in sent


def test_heater_counts_are_read_once_not_every_poll():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, *a, **k: (sent.append(cmd), {"OPS2/": "OPOK,OPS2,1,2", "OPS3/": "OPOK,OPS3,1,1"}.get(cmd, "OK"))[1]  # type: ignore[method-assign]
    for _ in range(3):
        client._zone_ids()
    assert sent.count("OPS3/") == 1


# ---- zones follow the hub's own list (heatapp-local-iqr) -----------------


def test_a_zone_the_hub_no_longer_lists_is_forgotten(tmp_path):
    # Zone 3 is in the readings cache from an earlier run (or another hub). The
    # hub now lists only 1 and 2, so zone 3 must not stay on screen as "ok".
    import json

    path = tmp_path / "readings.json"
    path.write_text(json.dumps({"1": [19.0, 20.0], "2": [18.0, 17.0], "3": [15.8, 1.0]}))
    client = NexhoLocalClient("10.0.0.99", cache_file=str(path))
    assert [r.id for r in client.list_rooms()] == ["1", "2", "3"]  # from the cache
    client._ask = lambda cmd, *a, **k: {  # type: ignore[method-assign]
        "OPS2/": "OPOK,OPS2,1,2,0", "OPS3/": "OPOK,OPS3,1,1",
    }.get(cmd, "OK,19,00,020")
    client._poll_once()
    assert [r.id for r in client.list_rooms()] == ["1", "2", "3"]  # one answer is not enough
    client._poll_once()
    assert [r.id for r in client.list_rooms()] == ["1", "2"]  # the hub said it twice
    assert sorted(json.loads(path.read_text())) == ["1", "2"]  # and not reloaded next start


def test_one_odd_zone_list_does_not_wipe_anything(tmp_path):
    # A hub that is rebooting can answer with an empty or short list once. That
    # must not throw away readings or a setpoint that is waiting to be sent.
    answers = ["OPOK,OPS2,1,2", "OPOK,OPS2,1,2", "OPOK,OPS2,0", "OPOK,OPS2,1,2"]
    client = NexhoLocalClient("10.0.0.99")

    def ask(cmd, *a, **k):
        if cmd == "OPS2/":
            return answers.pop(0)
        return {"OPS3/": "OPOK,OPS3,1,1"}.get(cmd, "OK,19,00,020")

    client._ask = ask  # type: ignore[method-assign]
    client._poll_once()
    client._poll_once()
    with client._lock:
        client._pending[2] = 22.0
        client._desired[2] = 22.0
    client._poll_once()  # the odd one
    assert [r.id for r in client.list_rooms()] == ["1", "2"]
    assert client._pending == {2: 22.0} and client._desired == {2: 22.0}
    client._poll_once()
    assert [r.id for r in client.list_rooms()] == ["1", "2"]


def test_a_zero_heater_count_is_checked_again_before_the_zone_is_dropped():
    # OPS3 is normally re-read every ten minutes. When it makes a zone vanish it
    # is read again on the very next poll, so one bad answer cannot hide a zone
    # for ten minutes or drop its state.
    heaters = ["OPOK,OPS3,1,1", "OPOK,OPS3,1,0", "OPOK,OPS3,1,1"]
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")

    def ask(cmd, *a, **k):
        sent.append(cmd)
        if cmd == "OPS3/":
            return heaters.pop(0) if len(heaters) > 1 else heaters[0]
        return {"OPS2/": "OPOK,OPS2,1,2"}.get(cmd, "OK,19,00,020")

    client._ask = ask  # type: ignore[method-assign]
    client._poll_once()
    client._poll_once()
    assert sent.count("OPS3/") == 1
    client._heaters_read_at = 0.0  # the ten-minute refresh comes due and reads a bad 0
    client._poll_once()
    assert client._zones == [1, 2]  # one answer does not hide the zone...
    assert 2 in client._state
    client._poll_once()  # ...and the count is read again straight away: a blip
    assert sent.count("OPS3/") == 3
    assert client._zones == [1, 2]
    assert [r.id for r in client.list_rooms()] == ["1", "2"]


def test_zones_are_kept_when_the_hub_does_not_answer(tmp_path):
    import json

    path = tmp_path / "readings.json"
    path.write_text(json.dumps({"1": [19.0, 20.0], "2": [18.0, 17.0]}))
    client = NexhoLocalClient("10.0.0.99", cache_file=str(path))

    def down(cmd, *a, **k):
        raise HearthError("hub down")

    client._ask = down  # type: ignore[method-assign]
    with pytest.raises(HearthError):
        client._poll_once()
    assert [r.id for r in client.list_rooms()] == ["1", "2"]  # last-known view stays


def test_a_zone_deleted_in_the_vendor_app_is_noticed_without_a_restart(monkeypatch):
    # Deleting a zone leaves its OPS2 slot in place and only drops its heater
    # count to 0, so the heater counts have to be re-read now and then.
    import hearthmage.nexho_client as mod

    clock = {"now": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    heaters = {"reply": "OPOK,OPS3,1,1,1"}
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")

    def ask(cmd, *a, **k):
        sent.append(cmd)
        return {"OPS2/": "OPOK,OPS2,1,2,3", "OPS3/": heaters["reply"]}.get(cmd, "OK")

    client._ask = ask  # type: ignore[method-assign]
    assert client._zone_ids() == [1, 2, 3]
    heaters["reply"] = "OPOK,OPS3,1,1,0"  # zone 3 deleted in the vendor app
    clock["now"] += 30
    assert client._zone_ids() == [1, 2, 3]  # not re-read on every poll
    assert sent.count("OPS3/") == 1
    clock["now"] += mod._HEATERS_REFRESH
    assert client._zone_ids() == [1, 2, 3]  # seen once: checked again before acting
    assert client._zone_ids() == [1, 2]  # the hub said it twice


def test_a_zone_the_hub_does_not_have_is_refused_before_anything_is_sent():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")

    def ask(cmd, *a, **k):
        sent.append(cmd)
        return {"OPS2/": "OPOK,OPS2,1,2", "OPS3/": "OPOK,OPS3,1,1"}.get(cmd, "OK")

    client._ask = ask  # type: ignore[method-assign]
    client._poll_once()
    sent.clear()
    with pytest.raises(HearthError, match="Unknown zone"):
        client.set_temperature("99", 30)
    with pytest.raises(HearthError, match="Unknown zone"):
        client.set_config("99", frost_temp=17)
    with pytest.raises(HearthError, match="Unknown zone"):
        client.set_holiday("99", days=1, hour=8, minute=0, arrival_temp=None)
    assert sent == []  # nothing reached the wire
    assert [r.id for r in client.list_rooms()] == ["1", "2"]  # and no phantom card


def test_a_radiator_that_stops_answering_is_marked_silent(monkeypatch):
    # Powered off at its switch: the hub keeps the zone, the reads fail, and the
    # last reading must not go on looking live.
    import hearthmage.nexho_client as mod

    clock = {"now": 5000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    replies = {"R#1": "OK,15,08,010"}
    client = make_client(replies)
    client._poll_zone(1)
    assert client.list_rooms()[0].silent_for is None  # it just answered
    replies["R#1"] = "ER,1"
    clock["now"] += 300
    client._poll_zone(1)
    room = client.list_rooms()[0]
    assert (room.current_temp, room.silent_for) == (15.8, None)  # a short nap is normal
    clock["now"] += 2400
    client._poll_zone(1)
    room = client.list_rooms()[0]
    assert room.current_temp == 15.8  # last reading still shown...
    assert room.silent_for == 2700  # ...with how long it has been silent
    replies["R#1"] = "OK,14,00,010"
    client._poll_zone(1)
    assert client.list_rooms()[0].silent_for is None  # back again


def test_a_cached_zone_that_never_answers_after_a_restart_is_marked_silent(tmp_path, monkeypatch):
    import json

    import hearthmage.nexho_client as mod

    clock = {"now": 100.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    path = tmp_path / "readings.json"
    path.write_text(json.dumps({"1": [15.8, 1.0]}))
    client = NexhoLocalClient("10.0.0.99", cache_file=str(path))
    client._ask = lambda cmd, *a, **k: "ER,1"  # type: ignore[method-assign]
    client._started_at = clock["now"]  # what start() records
    clock["now"] += 900
    client._poll_zone(1)
    room = client.list_rooms()[0]
    assert (room.current_temp, room.status, room.silent_for) == (15.8, "ok", 900)


# ---- a stale read must not undo a confirmed setpoint (heatapp-local-6fs) --


def _wait_for_writer(client, zone):
    import time as _time

    deadline = _time.time() + 2
    while _time.time() < deadline:
        with client._lock:
            if zone not in client._writers:
                return
        _time.sleep(0.005)
    raise AssertionError("write worker did not finish")


def test_a_read_taken_before_a_write_cannot_undo_the_confirmed_target():
    # The radiator answers the poll with the old target (20). Before the poller
    # applies that answer the user sets 22 and the hub acknowledges it. The old
    # 20 must not be written over the confirmed 22.
    client = make_client({"D#": "OK"})
    with client._lock:
        client._last[1] = (19.0, 20.0)
        client._state[1] = client._room(1, 19.0, 20.0, "ok")

    def read_then_user_writes(zone):
        client.set_temperature("1", 22.0)
        _wait_for_writer(client, 1)
        return 19.2, 20.0, True

    client._read_zone_raw = read_then_user_writes  # type: ignore[method-assign]
    client._poll_zone(1)
    room = client.list_rooms()[0]
    assert room.target_temp == 22.0
    assert room.current_temp == 19.2  # the room temperature is still taken from the read
    assert client._last[1] == (19.2, 22.0)


def test_a_read_that_overlaps_a_finishing_write_cannot_undo_it():
    # Here the write was already in flight when the read started and was
    # acknowledged before the read was applied.
    client = make_client({})
    with client._lock:
        client._last[1] = (19.0, 20.0)
        client._pending[1] = 22.0
        client._writers.add(1)

    def read_while_write_lands(zone):
        with client._lock:  # what the write worker does on an ack
            client._last[1] = (19.0, 22.0)
            client._pending.pop(1, None)
            client._writers.discard(1)
        return 19.2, 20.0, True

    client._read_zone_raw = read_while_write_lands  # type: ignore[method-assign]
    client._poll_zone(1)
    assert client.list_rooms()[0].target_temp == 22.0
    assert client._last[1] == (19.2, 22.0)


def test_an_undisturbed_read_is_still_the_truth():
    # No write anywhere near the read: a change made on the radiator keypad
    # must still show up.
    client = make_client({"R#1": "OK,19,02,016"})
    with client._lock:
        client._last[1] = (19.0, 20.0)
    client._poll_zone(1)
    assert client.list_rooms()[0].target_temp == 16.0


# ---- an unacknowledged setpoint is retried, and shown as such (sdm) ------


def _scripted_sends(client, script):
    """Replace the wire send with a script: target -> list of results, used in
    order (the last one repeats). Returns the list of targets sent."""
    sent: list[float] = []
    results = {target: list(outcomes) for target, outcomes in script.items()}

    def send_once(zone, target):
        sent.append(target)
        outcomes = results[target]
        return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]

    client._send_once = send_once  # type: ignore[method-assign]
    return sent


def _wait_until(check, what):
    import time as _time

    deadline = _time.time() + 2
    while _time.time() < deadline:
        if check():
            return
        _time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _zone_ready(client, zone=2, current=19.0, target=18.0):
    """Seed a zone as if loaded from the readings cache."""
    with client._lock:
        client._last[zone] = (current, target)
        client._state[zone] = client._room(zone, current, target, "ok")
        client._cached_zones = sorted(set(client._cached_zones or []) | {zone})


def test_unacknowledged_setpoint_is_retried_until_the_hub_takes_it(monkeypatch):
    monkeypatch.setattr("hearthmage.nexho_client._SETPOINT_RETRY_FIRST", 0.01)
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    sent = _scripted_sends(client, {21.0: [False, False, True]})
    client.set_temperature("2", 21.0)
    _wait_for_writer(client, 2)
    assert sent == [21.0, 21.0, 21.0]
    room = next(r for r in client.list_rooms() if r.id == "2")
    assert (room.target_temp, room.unconfirmed) == (21.0, False)
    assert client._last[2] == (19.0, 21.0)
    assert 2 not in client._pending


def test_unacknowledged_setpoint_is_shown_as_not_confirmed_and_survives_polls():
    client = NexhoLocalClient("10.0.0.99")  # default retry wait: long enough to hold here
    _zone_ready(client)
    _scripted_sends(client, {21.0: [False]})
    client.set_temperature("2", 21.0)
    _wait_until(lambda: client.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    # The radiator wakes just long enough to answer a poll with its old target.
    client._read_zone_raw = lambda zone: (19.4, 18.0, True)  # type: ignore[method-assign]
    client._kick_write_retry = lambda zone: None  # type: ignore[method-assign]  # hold the retry for this check
    client._poll_zone(2)
    room = client.list_rooms()[0]
    assert (room.current_temp, room.target_temp, room.unconfirmed) == (19.4, 21.0, True)
    assert client._last[2][1] == 18.0  # nothing unconfirmed leaks into the confirmed cache
    assert client.health()["zones"]["2"]["unconfirmed"] is True
    client.stop()
    _wait_for_writer(client, 2)  # the retry worker ends with the client


def test_a_newer_target_replaces_one_that_is_still_being_retried():
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    sent = _scripted_sends(client, {21.0: [False], 23.0: [True]})
    client.set_temperature("2", 21.0)
    _wait_until(lambda: client.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    client.set_temperature("2", 23.0)  # must not wait out the retry delay
    _wait_for_writer(client, 2)
    assert sent == [21.0, 23.0]
    room = client.list_rooms()[0]
    assert (room.target_temp, room.unconfirmed) == (23.0, False)


def test_a_radiator_that_answers_a_poll_gets_the_waiting_setpoint_at_once():
    # It is awake right now, so this is the moment to resend, not minutes later.
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    sent = _scripted_sends(client, {21.0: [False, True]})
    client.set_temperature("2", 21.0)
    _wait_until(lambda: client.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    client._backoff[2] = 60.0  # it had stopped answering polls (backing off)
    client._read_zone_raw = lambda zone: (19.4, 18.0, True)  # type: ignore[method-assign]
    client._poll_zone(2)
    _wait_for_writer(client, 2)
    assert sent == [21.0, 21.0]
    assert client.list_rooms()[0].unconfirmed is False
    assert client._last[2][1] == 21.0


def test_a_radiator_that_answers_polls_but_refuses_the_setpoint_is_not_hammered():
    # It never went silent, so its poll answers are no reason to resend early:
    # that would be a burst of priority writes on every 15 s poll.
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    sent = _scripted_sends(client, {21.0: [False]})
    client.set_temperature("2", 21.0)
    _wait_until(lambda: client.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    client._read_zone_raw = lambda zone: (19.4, 18.0, True)  # type: ignore[method-assign]
    for _ in range(3):
        client._poll_zone(2)
    import time as _time

    _time.sleep(0.05)
    assert sent == [21.0]  # still waiting out the retry delay
    client.stop()
    _wait_for_writer(client, 2)


def test_asking_for_the_same_target_again_retries_it_now():
    # Pressing Set target a second time must not sit out the retry delay.
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    sent = _scripted_sends(client, {21.0: [False, True]})
    client.set_temperature("2", 21.0)
    _wait_until(lambda: client.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    client.set_temperature("2", 21.0)
    _wait_for_writer(client, 2)
    assert sent == [21.0, 21.0]
    assert client.list_rooms()[0].unconfirmed is False
    assert client._write_kick == set()  # nothing left over to cut a later wait short


def test_setpoint_retry_wait_doubles_and_is_capped():
    from hearthmage.nexho_client import (
        _SETPOINT_RETRY_FIRST,
        _SETPOINT_RETRY_MAX,
        _setpoint_retry_wait,
    )

    assert _setpoint_retry_wait(1) == _SETPOINT_RETRY_FIRST
    assert _setpoint_retry_wait(2) == _SETPOINT_RETRY_FIRST * 2
    assert _setpoint_retry_wait(3) == _SETPOINT_RETRY_FIRST * 4
    assert _setpoint_retry_wait(50) == _SETPOINT_RETRY_MAX


# ---- review follow-ups to the setpoint retry (heatapp-local-sdm) ----------


def test_a_poll_that_already_shows_the_wanted_target_confirms_it():
    # The setpoint reached the radiator but the hub's OK was lost. The next
    # read proves it, so stop saying "not confirmed" and stop re-sending.
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    sent = _scripted_sends(client, {21.0: [False]})
    client.set_temperature("2", 21.0)
    _wait_until(lambda: client.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    client._read_zone_raw = lambda zone: (19.4, 21.0, True)  # type: ignore[method-assign]
    client._poll_zone(2)
    _wait_for_writer(client, 2)
    room = client.list_rooms()[0]
    assert (room.target_temp, room.unconfirmed) == (21.0, False)
    assert client._last[2] == (19.4, 21.0)
    assert sent == [21.0]  # nothing was re-sent


@pytest.mark.parametrize("newer_command", ["holiday", "program_on"])
def test_a_newer_mode_command_cancels_a_setpoint_that_is_still_waiting(newer_command):
    # 22 C is waiting for a sleeping radiator. The user then starts a holiday
    # (or switches the weekly program on). The old 22 must not be sent after it.
    client = make_client({"D#": "OK"})
    _zone_ready(client)
    sent = _scripted_sends(client, {22.0: [False]})
    client.set_temperature("2", 22.0)
    _wait_until(lambda: client.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    if newer_command == "holiday":
        client.set_holiday("2", days=7, hour=18, minute=0, arrival_temp=None)
    else:
        client.cancel_waiting_setpoint("2")  # what the app does when the program is switched on
    _wait_for_writer(client, 2)  # the retry worker has gone
    room = client.list_rooms()[0]
    assert room.unconfirmed is False
    assert room.target_temp == 18.0  # back to the last confirmed target
    assert 2 not in client._pending and 2 not in client._desired
    assert sent == [22.0]  # the stale 22 was never sent again


def _fake_hub(monkeypatch, reply):
    """Replace the UDP socket with a hub that answers each command with
    ``reply(cmd)``, so the real _ask and its wire lock run. Returns the list of
    commands sent."""
    import hearthmage.nexho_client as nc

    sent: list[str] = []

    class FakeSocket:
        def __init__(self, *a):
            self.cmd = ""

        def settimeout(self, t):
            pass

        def connect(self, addr):
            pass

        def send(self, data):
            self.cmd = data.decode("ascii")
            sent.append(self.cmd)

        def recv(self, n):
            return reply(self.cmd).encode("ascii") + b"\x00"

        def close(self):
            pass

    monkeypatch.setattr(nc.socket, "socket", FakeSocket)
    return sent


@pytest.mark.parametrize("newer_command", ["holiday", "program_on"])
def test_a_newer_mode_command_during_the_first_send_stops_the_old_setpoint(
    monkeypatch, newer_command
):
    # heatapp-local-1y2. 22 C goes out to a sleeping radiator; while the hub is
    # still working on it the user starts a holiday (or switches the program
    # on). The hub answers ER, so the first send is not done yet. The old 22
    # must not go out again after the newer command, nor be retried later.
    import threading

    first_on_air = threading.Event()
    hub_answers = threading.Event()

    def reply(cmd):
        if "*T" not in cmd:
            return "OK"
        if not first_on_air.is_set():
            first_on_air.set()
            hub_answers.wait(2)  # the hub is still trying the radiator
        return "ER"  # the radiator missed the RF window

    sent = _fake_hub(monkeypatch, reply)
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    client.set_temperature("2", 22.0)
    assert first_on_air.wait(2)
    if newer_command == "holiday":
        def newer():
            client.set_holiday("2", days=7, hour=18, minute=0, arrival_temp=None)
    else:
        def newer():  # what toggle_program does: cancel, then the program goes out
            client.cancel_waiting_setpoint("2")
            client.set_program_active("2", True)
    other = threading.Thread(target=newer)
    other.start()
    try:
        _wait_until(lambda: client._wire.writes_pending == 2, "the newer command to queue for the wire")
    finally:
        hub_answers.set()  # never leave a thread blocked past the fake hub's life
        other.join(2)
    try:
        _wait_for_writer(client, 2)  # nothing is left to retry
    finally:
        client.stop()
    newer_at = next(i for i, cmd in enumerate(sent) if "*RH" in cmd or "*ON" in cmd)
    assert [cmd for cmd in sent[newer_at:] if "*T" in cmd] == []
    room = client.list_rooms()[0]
    assert (room.target_temp, room.unconfirmed) == (18.0, False)
    assert 2 not in client._pending and 2 not in client._desired


def test_a_cancel_while_the_hub_settles_sends_nothing(monkeypatch):
    # The newer command comes in during the quiet gap before the send: the
    # old 22 must not go out at all.
    sent = _fake_hub(monkeypatch, lambda cmd: "OK")
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    _queue_write(client, 2, 22.0)
    client._let_hub_settle = lambda: client.cancel_waiting_setpoint("2")  # type: ignore[method-assign]
    client._send_once(2, 22.0)
    assert sent == []


def test_a_withdrawn_setpoint_does_not_wait_out_the_er_gap(monkeypatch):
    # Nothing more will be sent, so there is nothing to wait for; a newer tap
    # must not sit behind a pointless 3 s pause.
    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc, "_ER_GAP", 3.0)
    slept: list[float] = []
    monkeypatch.setattr(nc.time, "sleep", slept.append)
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    _queue_write(client, 2, 22.0)

    def reply(cmd):
        client.cancel_waiting_setpoint("2")  # a holiday arrives while the hub tries
        return "ER"

    sent = _fake_hub(monkeypatch, reply)
    client._send_once(2, 22.0)
    assert len(sent) == 1
    assert 3.0 not in slept


def test_a_withdrawn_send_is_not_counted_as_a_failed_one(monkeypatch):
    # 22 is cancelled mid-send, then the user asks for 22 again before the
    # worker looks. That is a new request: no "not confirmed" mark and no
    # longer backoff for a send that the hub never refused.
    import hearthmage.nexho_client as nc

    def t_sends():
        return len([cmd for cmd in sent if "*T" in cmd])

    waits: list[tuple[int, int]] = []  # (failures, *T sends so far) per retry wait
    monkeypatch.setattr(
        nc, "_setpoint_retry_wait", lambda failures: waits.append((failures, t_sends())) or 1000.0
    )
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)

    def reply(cmd):
        if t_sends() == 1:
            client.cancel_waiting_setpoint("2")  # a holiday arrives while the hub tries
        return "ER"

    sent = _fake_hub(monkeypatch, reply)
    real_send = client._send_once
    tapped: list[int] = []

    def send_then_tap_again(zone, target):
        result = real_send(zone, target)
        if not tapped:
            tapped.append(1)
            client.set_temperature("2", 22.0)  # before the worker takes the lock again
        return result

    client._send_once = send_then_tap_again  # type: ignore[method-assign]
    client.set_temperature("2", 22.0)
    try:
        _wait_until(lambda: waits, "the first retry wait")
    finally:
        client.stop()
        _wait_for_writer(client, 2)
    # The first failure counted is the re-sent 22 the hub answered ER to.
    assert waits[0] == (1, 1 + nc._ER_ATTEMPTS)


def test_a_finished_worker_does_not_unregister_the_next_one():
    # Worker A has its ack and has left the zone. Before its cleanup runs, a
    # new target starts worker B. A's cleanup must not unregister B, or the
    # poller would think the zone idle and drop B's pending target.
    import threading

    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    _scripted_sends(client, {22.0: [True], 23.0: [False]})
    _queue_write(client, 2, 22.0)
    real_lock = client._lock
    main = threading.current_thread()
    tapped: list[int] = []

    class LockThatLetsATapIn:
        def __enter__(self):
            real_lock.acquire()

        def __exit__(self, *exc):
            real_lock.release()
            if not tapped and threading.current_thread() is main and 2 not in client._writers:
                tapped.append(1)
                client.set_temperature("2", 23.0)  # worker B starts

    client._lock = LockThatLetsATapIn()  # type: ignore[assignment]
    client._drain_writes(2)  # worker A: 22 is acknowledged
    try:
        assert tapped
        assert 2 in client._writers  # B is still the zone's writer
        client._read_zone_raw = lambda zone: (19.4, 18.0, True)  # type: ignore[method-assign]
        client._poll_zone(2)
        assert client._pending.get(2) == 23.0  # the poller kept B's target
    finally:
        client.stop()
        _wait_for_writer(client, 2)


def test_history_and_mqtt_are_not_told_a_target_from_before_a_write():
    # A read that overlapped a confirmed write carries the old target. The
    # reading hook (history, waste detector, MQTT state) must get the new one.
    seen: list[tuple] = []
    client = make_client({"D#": "OK"})
    client._on_reading = lambda zone, current, target: seen.append((zone, current, target))
    _zone_ready(client, zone=1, current=19.0, target=19.0)

    def read_then_user_writes(zone):
        client.set_temperature("1", 21.0)
        _wait_for_writer(client, 1)
        return 19.2, 19.0, True

    client._read_zone_raw = read_then_user_writes  # type: ignore[method-assign]
    client._poll_zone(1)
    assert seen == [(1, 19.2, 21.0)]


def test_the_reading_hook_reports_what_the_radiator_holds_while_unconfirmed():
    seen: list[tuple] = []
    client = NexhoLocalClient("10.0.0.99")
    client._on_reading = lambda zone, current, target: seen.append((zone, current, target))
    _zone_ready(client)
    _scripted_sends(client, {21.0: [False]})
    client.set_temperature("2", 21.0)
    _wait_until(lambda: client.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    client._read_zone_raw = lambda zone: (19.4, 18.0, True)  # type: ignore[method-assign]
    client._poll_zone(2)
    assert seen == [(2, 19.4, 18.0)]  # history records the truth, not the wish
    client.stop()
    _wait_for_writer(client, 2)


# ---- the retry gives up after two hours (owner's decision 2026-11-11) -----


def test_an_unacknowledged_setpoint_is_given_up_after_the_limit(monkeypatch):
    # A radiator switched off at the wall must not be sent yesterday's target
    # when it is powered on again.
    monkeypatch.setattr("hearthmage.nexho_client._SETPOINT_RETRY_FIRST", 0.01)
    monkeypatch.setattr("hearthmage.nexho_client._SETPOINT_GIVE_UP_AFTER", 0.08)
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)  # confirmed target 18
    sent = _scripted_sends(client, {21.0: [False]})
    client.set_temperature("2", 21.0)
    _wait_for_writer(client, 2)  # the worker ends by itself
    assert len(sent) >= 2  # it did retry first
    room = client.list_rooms()[0]
    assert (room.target_temp, room.unconfirmed) == (18.0, False)  # back to what the radiator holds
    assert "21.0" in room.error
    assert "not delivered" in room.error
    assert 2 not in client._pending and 2 not in client._desired
    assert client._last[2] == (19.0, 18.0)
    # The message stays through polls the radiator does not answer...
    client._read_zone_raw = lambda zone: (None, None, False)  # type: ignore[method-assign]
    client._poll_zone(2)
    assert "not delivered" in client.list_rooms()[0].error
    # ...and goes once it answers again (the card then shows the truth).
    client._read_zone_raw = lambda zone: (19.5, 18.0, True)  # type: ignore[method-assign]
    client._poll_zone(2)
    assert client.list_rooms()[0].error is None


def test_giving_up_on_off_says_so(monkeypatch):
    monkeypatch.setattr("hearthmage.nexho_client._SETPOINT_RETRY_FIRST", 0.01)
    monkeypatch.setattr("hearthmage.nexho_client._SETPOINT_GIVE_UP_AFTER", 0.05)
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    _scripted_sends(client, {1.0: [False]})
    client.set_temperature("2", 0)
    _wait_for_writer(client, 2)
    assert "Switching off" in client.list_rooms()[0].error


def test_a_new_request_restarts_the_give_up_clock_and_clears_the_message(monkeypatch):
    monkeypatch.setattr("hearthmage.nexho_client._SETPOINT_RETRY_FIRST", 0.01)
    monkeypatch.setattr("hearthmage.nexho_client._SETPOINT_GIVE_UP_AFTER", 0.05)
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    _scripted_sends(client, {21.0: [False], 22.0: [True]})
    client.set_temperature("2", 21.0)
    _wait_for_writer(client, 2)
    first_request = client._requested_at[2]
    assert client.list_rooms()[0].error
    client.set_temperature("2", 22.0)
    assert client._requested_at[2] > first_request
    _wait_for_writer(client, 2)
    room = client.list_rooms()[0]
    assert (room.target_temp, room.error) == (22.0, None)


def test_the_real_give_up_limit_is_two_hours():
    from hearthmage.nexho_client import _SETPOINT_GIVE_UP_AFTER

    assert _SETPOINT_GIVE_UP_AFTER == 2 * 3600


# ---- a reply of the wrong shape is no answer (heatapp-local-q2t, -5fw) -----
# A reply carries nothing that ties it to its query, and the vendor app on the
# LAN can still shift replies by one. Each read accepts only its own shape.

_LIVE_F = "OK,14,9,5,21,19,17,6,11,69,0,0,0,0,1,0,44,244,0,33,v200110"
_EMPTY_DAY = ",".join(["5,24,60,24,60"] * 6)


@pytest.mark.parametrize("reply", [
    "OPOK,OPS3,1,1,1,1",  # heater counts: would read as target 1 (Off)
    "OPOK,OPS2,1,2,3,4,0",  # zone list: would read as target 2
    "OK,10",  # an *?E setting: would read as room 10 with no target
    "OK,20,04",  # too short
    "OK,20,04,007,1",  # too long
    "OK,20,12,007",  # tenths above 9
    "OK,20,-4,007",
    "OK,20,04,abc",
    "OK, 20,04,007",
    "OK,,04,007",
    _LIVE_F,  # an *?F status
    f"OK,P0,11,{_EMPTY_DAY}",  # a day program
    "OK,1000,12,40",  # energy counters (room 1000 C)
])
def test_status_replies_of_another_shape_are_no_reading(reply):
    assert _parse_status(reply) == (None, None)


def test_a_shifted_status_reply_is_not_a_fresh_reading():
    client = make_client({"OPS2/": "OPOK,OPS2,2", "R#2#": "OPOK,OPS3,1,1,1,1"})
    _zone_ready(client)
    client._poll_once()  # same path as a radiator that did not answer
    room = client.list_rooms()[0]
    assert (room.current_temp, room.target_temp, room.is_off) == (19.0, 18.0, False)
    assert 2 not in client._last_fresh
    assert client._backoff[2] > client._poll_interval  # it backs off like a silent one


@pytest.mark.parametrize("reply", ["OK,20,04,007", "OK,256", "OK,-1", "OK,", "OK,7,0", "OPOK,7"])
def test_a_setting_read_accepts_only_ok_and_one_byte(reply):
    assert make_client({"R#1#1#0#0*?E#0#6/": reply})._read_element(1, 0, 6) is None


def test_a_setting_read_still_takes_a_byte():
    assert make_client({"R#1#1#0#0*?E#0#9/": "OK,251"})._read_element(1, 0, 9) == 251


def test_a_longer_genuine_day_program_is_still_read():
    # The OK,P<day> prefix already rules out every other query's reply. A
    # longer reply from other firmware must not break sync or Load from radiator.
    client = make_client({"R#2#1#0#0*?P3/": f"OK,P3,11,{_EMPTY_DAY},5"})
    assert client.read_day_pattern("2", 3) == []


@pytest.mark.parametrize("reply", ["OK,20,04,007", "OK,10", _LIVE_F.rsplit(",", 1)[0]])
def test_a_status_dump_must_look_like_one(monkeypatch, reply):
    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    with pytest.raises(HearthError):
        make_client({"R#1#1#0#0*?F/": reply}).read_status_raw("1")


_UD = "OK,1000,12,40,35,50,0,22,18,7"


@pytest.mark.parametrize("ud", [
    "OK,20,04,007",  # a shifted *?T status
    "OK,10",  # a shifted *?E setting
    _LIVE_F,  # a shifted *?F status
    "OK,1000,12,40",  # not the eight daily buckets
    "OK,1000,12,40,35,50,0,22,18,x",
])
def test_energy_needs_eight_daily_counters(ud):
    client = make_client({"R#1#1#0#0*?UD/": ud, "R#1#1#0#0*?UM/": "OK,1000,300"})
    assert client.read_energy("1") is None


@pytest.mark.parametrize("um", ["OK,20,04,007", _UD, "OK,999,300"])
def test_monthly_counters_from_another_reply_are_dropped(um):
    # Monthly buckets must carry the same rated watts as the daily read, and
    # there are at most seven; a copy of the daily reply has eight.
    client = make_client({"R#1#1#0#0*?UD/": _UD, "R#1#1#0#0*?UM/": um})
    data = client.read_energy("1")
    assert data["daily"] == [12, 40, 35, 50, 0, 22, 18, 7]
    assert data["monthly"] == []


def test_cloud_reporting_reply_must_be_exactly_one_flag():
    with pytest.raises(HearthError):
        make_client({"OPMR3898/": "OPOK,1,0"}).read_cloud_reporting()


@pytest.mark.parametrize("ops3", ["OPOK,OPS3,100000,1", "OPOK,OPS3,17,1", "OPOK,OPS3,1,x", "OPOK,OPS3,-1,1"])
def test_an_implausible_heater_count_reply_is_not_used(ops3):
    # A sensor-offset save writes once per heater: 100000 would be 100000
    # writes on a request thread. The reply is treated as unread (one heater).
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")

    def ask(cmd, *a, **k):
        sent.append(cmd)
        return {"OPS2/": "OPOK,OPS2,1,2", "OPS3/": ops3}.get(cmd, "OK")

    client._ask = ask  # type: ignore[method-assign]
    assert client._zone_ids() == [1, 2]  # nothing hidden on a bad reply
    client.set_config("1", sensor_offset=0.5)
    assert [c for c in sent if "SEP#0#9" in c] == ["R#1#1#0#0*SEP#0#9#5/"]


def test_sixteen_heaters_is_still_a_count():
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, *a, **k: {"OPS2/": "OPOK,OPS2,1", "OPS3/": "OPOK,OPS3,16"}.get(cmd, "OK")  # type: ignore[method-assign]
    client._zone_ids()
    assert client._modules(1) == 16


# ---- strict, but never stricter than the evidence, and never silent ------


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "hearthmage.nexho" and r.levelname == "WARNING"]


def test_heater_counts_past_the_zone_list_are_padding(caplog):
    # OPS2 ends its list with a 0 and may carry slots after it; OPS3 can be
    # padded the same way. Only the counts for listed zones are checked.
    client = make_client({"OPS2/": "OPOK,OPS2,1,2,3,4,0,9", "OPS3/": "OPOK,OPS3,1,0,1,1,255,255"})
    with caplog.at_level("WARNING", logger="hearthmage.nexho"):
        assert client._zone_ids() == [1, 3, 4]  # zone 2 (no heaters) stays hidden
    assert _warnings(caplog) == []


def test_a_rejected_heater_count_reply_is_logged_once(caplog):
    client = make_client({"OPS2/": "OPOK,OPS2,1,2", "OPS3/": "OPOK,OPS3,100000,1"})
    with caplog.at_level("WARNING", logger="hearthmage.nexho"):
        for _ in range(3):
            client._zone_ids()
    assert len(_warnings(caplog)) == 1
    assert "OPOK,OPS3,100000,1" in _warnings(caplog)[0]


def test_a_rejected_status_reply_is_logged_per_zone_at_most_every_ten_minutes(monkeypatch, caplog):
    import hearthmage.nexho_client as nc

    clock = {"now": 1000.0}
    monkeypatch.setattr(nc.time, "monotonic", lambda: clock["now"])
    replies = {"R#2#": "OPOK,OPS3,1,1,1,1", "R#3#": "ER,1"}
    client = make_client(replies)
    with caplog.at_level("WARNING", logger="hearthmage.nexho"):
        client._read_zone_raw(2)
        client._read_zone_raw(2)  # same zone soon after: not again
        client._read_zone_raw(3)  # an ER is a sleeping radiator, not a bad reply
        assert len(_warnings(caplog)) == 1
        assert "zone 2" in _warnings(caplog)[0] and "OPOK,OPS3,1,1,1,1" in _warnings(caplog)[0]
        clock["now"] += 601
        client._read_zone_raw(2)
    assert len(_warnings(caplog)) == 2


def test_a_rejected_energy_payload_is_logged_for_capture(caplog):
    # The protocol doc asks for exactly this payload if a radiator ever answers.
    client = make_client({"R#1#1#0#0*?UD/": "OK,1000,12,40", "R#1#1#0#0*?UM/": "ER,1"})
    with caplog.at_level("WARNING", logger="hearthmage.nexho"):
        assert client.read_energy("1") is None
        assert client.read_energy("1") is None  # the same payload again: logged once
    assert len(_warnings(caplog)) == 1
    assert "OK,1000,12,40" in _warnings(caplog)[0]


def test_a_rejected_monthly_payload_is_logged_and_er_is_not(caplog):
    ud = "OK,1000,12,40,35,50,0,22,18,7"
    client = make_client({"R#1#1#0#0*?UD/": ud, "R#1#1#0#0*?UM/": "OK,999,300"})
    with caplog.at_level("WARNING", logger="hearthmage.nexho"):
        client.read_energy("1")
        quiet = make_client({"R#1#1#0#0*?UD/": "ER,1", "R#1#1#0#0*?UM/": "ER,1"})
        quiet.read_energy("1")
    assert len(_warnings(caplog)) == 1
    assert "OK,999,300" in _warnings(caplog)[0]


def test_a_forgotten_zone_takes_its_rejection_log_state_with_it():
    client = make_client({"R#2#": "OK,10", "R#1#1#0#0*?UD/": "OK,1", "R#1#1#0#0*?UM/": "ER,1"})
    client._read_zone_raw(2)
    client.read_energy("1")
    client._rejected_logged[("OPS3",)] = "OPOK,OPS3,x"
    client._forget_zones_except([3])
    assert client._status_rejected_at == {}
    assert client._rejected_logged == {("OPS3",): "OPOK,OPS3,x"}  # not a zone's


def test_an_empty_heater_count_is_unknown_for_that_zone_only(caplog):
    client = make_client({"OPS2/": "OPOK,OPS2,1,2,3", "OPS3/": "OPOK,OPS3,2,,0"})
    with caplog.at_level("WARNING", logger="hearthmage.nexho"):
        assert client._zone_ids() == [1, 2]  # zone 2 unknown stays visible, 3 hidden
    assert (client._modules(1), client._modules(2)) == (2, 1)  # unknown: one heater
    assert _warnings(caplog) == []


# ---- the poller survives anything (heatapp-local-97w) ---------------------


def test_the_poller_survives_a_failure_while_working_out_its_sleep():
    import threading

    client = NexhoLocalClient("10.0.0.99", poll_interval=0.01)
    second_poll = threading.Event()
    polls: list[int] = []

    def poll_once():
        polls.append(1)
        if len(polls) == 2:
            second_poll.set()

    sleeps: list[int] = []

    def sleep_until_due():
        sleeps.append(1)
        if len(sleeps) == 1:
            raise RuntimeError("dictionary changed size during iteration")
        return 0.0

    client._poll_once = poll_once  # type: ignore[method-assign]
    client._sleep_until_due = sleep_until_due  # type: ignore[method-assign]
    client.start()
    try:
        assert second_poll.wait(2), "the poller died"
    finally:
        client.stop()
        client._thread.join(2)


# ---- one conversation with a hub across a client swap (heatapp-local-6vg) --


def test_a_stopped_poller_sends_nothing_after_the_read_on_the_air(monkeypatch):
    # The app swaps clients (settings save, backup import) while the old
    # poller is reading zone 1. Zones 2 and 3 must not be read after that.
    client = NexhoLocalClient("10.0.0.99")

    def reply(cmd):
        if cmd == "OPS2/":
            return "OPOK,OPS2,1,2,3"
        if cmd.startswith("R#1#"):
            client.stop()
        return "ER,1"

    sent = _fake_hub(monkeypatch, reply)
    client._poll_once()
    assert [cmd for cmd in sent if cmd.startswith("R#")] == ["R#1#1#0#0*?T/"]


def test_a_stopped_client_puts_nothing_on_the_wire(monkeypatch):
    sent = _fake_hub(monkeypatch, lambda cmd: "OK")
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    client.stop()
    with pytest.raises(HearthError):
        client.set_config("2", window_sensor=True)  # a request thread still holding it
    with pytest.raises(HearthError):
        client.set_temperature("2", 22.0)  # refused, not silently lost
    assert sent == []


def test_a_new_client_leaves_the_quiet_gap_after_the_old_ones_reply(monkeypatch):
    # The hub answers a query sent straight after its last reply with that
    # reply's value, whichever client sent it.
    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc, "_HUB_QUIET_GAP", 0.1)
    monkeypatch.setattr(nc.time, "monotonic", lambda: 100.0)
    slept: list[float] = []
    monkeypatch.setattr(nc.time, "sleep", slept.append)
    _fake_hub(monkeypatch, lambda cmd: "OPOK")
    old = NexhoLocalClient("10.0.0.99")
    old._ask("OPS2/", 1, 1.0)
    old.stop()
    NexhoLocalClient("10.0.0.99")._ask("OPS2/", 1, 1.0)
    assert slept == [pytest.approx(0.1)]


def test_a_new_client_waits_for_the_old_ones_exchange_to_finish(monkeypatch):
    import threading

    on_air = threading.Event()
    answer = threading.Event()

    def reply(cmd):
        if cmd == "OPS2/":
            on_air.set()
            answer.wait(2)  # the hub is still busy with the old client's query
        return "OPOK"

    sent = _fake_hub(monkeypatch, reply)
    old = NexhoLocalClient("10.0.0.99")
    new = NexhoLocalClient("10.0.0.99")
    first = threading.Thread(target=lambda: old._ask("OPS2/", 1, 1.0))
    second = threading.Thread(target=lambda: new._ask("OPMR3898/", 1, 1.0))
    first.start()
    try:
        assert on_air.wait(2)
        old.stop()
        second.start()
        second.join(0.2)  # time enough to send, if it could
        early = list(sent)
    finally:
        answer.set()
        first.join(2)
        if second.is_alive() or second.ident is not None:
            second.join(2)
    assert early == ["OPS2/"]
    assert sent == ["OPS2/", "OPMR3898/"]


def test_another_hub_has_its_own_wire():
    assert NexhoLocalClient("10.0.0.99")._wire is NexhoLocalClient("10.0.0.99")._wire
    assert NexhoLocalClient("10.0.0.99")._wire is not NexhoLocalClient("10.0.0.98")._wire


def _swap_while_waiting(new_ip="10.0.0.99"):
    """An old client whose 22 C the radiator has not acknowledged is stopped
    (the app swapped clients); a new one starts. Returns (old, new, new_sent)."""
    old = NexhoLocalClient("10.0.0.99")
    _zone_ready(old)
    old_sent = _scripted_sends(old, {22.0: [False]})
    old.set_temperature("2", 22.0)
    _wait_until(lambda: old.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    old.stop()
    _wait_for_writer(old, 2)
    assert old_sent == [22.0]  # the old worker sent nothing more
    new = NexhoLocalClient(new_ip)
    new._poll_once = lambda: None  # type: ignore[method-assign]
    _zone_ready(new)
    new_sent = _scripted_sends(new, {22.0: [False]})
    return old, new, new_sent


def test_a_setpoint_waiting_in_the_old_client_is_carried_to_the_new_one():
    old, new, new_sent = _swap_while_waiting()
    new.start()
    try:
        _wait_until(lambda: new_sent, "the new client to send it")
        room = new.list_rooms()[0]
        assert (room.target_temp, room.unconfirmed) == (22.0, True)
        assert new._pending[2] == 22.0  # the poller keeps it
        assert new._requested_at[2] == old._requested_at[2]  # the give-up clock runs on
    finally:
        new.stop()
        new._thread.join(2)
        _wait_for_writer(new, 2)
    assert new_sent == [22.0]


def test_a_setpoint_is_not_carried_to_another_hub():
    _old, new, new_sent = _swap_while_waiting(new_ip="10.0.0.98")
    new.start()
    new.stop()
    new._thread.join(2)
    assert new_sent == [] and 2 not in new._desired


# ---- review of the swap (2026-11-26): acks, overlapping stops, cancels ----


def _idle_client(ip="10.0.0.99", **kw):
    client = NexhoLocalClient(ip, **kw)
    client._poll_once = lambda: None  # type: ignore[method-assign]
    _zone_ready(client)
    return client


def test_a_setpoint_acked_after_stop_is_not_sent_again(monkeypatch):
    # The old worker's D# is on the air when the app stops the client; the
    # hub acks it. The new client must not send it a second time.
    import threading

    on_air, answer = threading.Event(), threading.Event()

    def reply(cmd):
        if cmd.startswith("D#2") and not on_air.is_set():
            on_air.set()
            answer.wait(2)
        return "OK"

    sent = _fake_hub(monkeypatch, reply)
    old = _idle_client()
    old.set_temperature("2", 22.0)
    assert on_air.wait(2)
    old.stop()
    new = _idle_client()
    new.start()
    answer.set()
    _wait_for_writer(old, 2)
    _wait_for_writer(new, 2)
    new.stop()
    assert [c for c in sent if c.startswith("D#2")] == ["D#2#1#0#0*T22/"]
    room = new.list_rooms()[0]
    assert (room.target_temp, room.unconfirmed, new._last[2][1]) == (22.0, False, 22.0)


def test_an_acked_setpoint_parked_on_the_wire_is_not_replayed(monkeypatch):
    # Hub A -> hub B -> hub A: the old A client's setpoint was acked after it
    # stopped, while nobody owned A's wire. The next A client must not send it.
    import threading

    on_air, answer = threading.Event(), threading.Event()

    def reply(cmd):
        if cmd.startswith("D#2") and not on_air.is_set():
            on_air.set()
            answer.wait(2)
        return "OK"

    sent = _fake_hub(monkeypatch, reply)
    old = _idle_client()
    old.set_temperature("2", 22.0)
    assert on_air.wait(2)
    old.stop()  # nobody owns the wire: the setpoint is parked
    answer.set()
    _wait_for_writer(old, 2)
    again = _idle_client()
    again.start()
    again.stop()
    assert [c for c in sent if c.startswith("D#2")] == ["D#2#1#0#0*T22/"]
    assert 2 not in again._desired


def test_a_setpoint_acked_just_before_stop_is_not_carried(monkeypatch):
    # The ack is in, but the worker has not recorded it yet when stop() runs.
    sent = _fake_hub(monkeypatch, lambda cmd: "OK")
    old = _idle_client()
    real_send = old._send_once

    def send_then_swap(zone, target):
        result = real_send(zone, target)
        old.stop()
        return result

    old._send_once = send_then_swap  # type: ignore[method-assign]
    old.set_temperature("2", 22.0)
    _wait_for_writer(old, 2)
    new = _idle_client()
    new.start()
    new.stop()
    assert [c for c in sent if c.startswith("D#2")] == ["D#2#1#0#0*T22/"]
    assert 2 not in new._desired


def test_a_setpoint_is_kept_when_two_stops_overlap(monkeypatch):
    # c1 is stopped while the owner c2 is itself stopping (its snapshot taken,
    # its hand-over not yet). c1's setpoint must reach c3.
    import threading

    import hearthmage.nexho_client as nc

    c1 = NexhoLocalClient("10.0.0.99")
    _zone_ready(c1)
    _scripted_sends(c1, {22.0: [False]})
    c1.set_temperature("2", 22.0)
    _wait_until(lambda: c1.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    c2 = _idle_client()
    c2.start()
    real_hand_over = nc._Wire.hand_over
    in_c2, go = threading.Event(), threading.Event()

    def gated(self, client, waiting):
        if client is c2:
            in_c2.set()
            go.wait(2)
        return real_hand_over(self, client, waiting)

    monkeypatch.setattr(nc._Wire, "hand_over", gated)
    stopping = threading.Thread(target=c2.stop)
    stopping.start()
    assert in_c2.wait(2)
    c1.stop()
    go.set()
    stopping.join(2)
    _wait_for_writer(c1, 2)
    c3 = _idle_client()
    c3_sent = _scripted_sends(c3, {22.0: [True]})
    c3.start()
    try:
        _wait_until(lambda: c3_sent, "c3 to send the carried setpoint")
    finally:
        c3.stop()
        _wait_for_writer(c3, 2)
    assert c3_sent == [22.0]


def test_a_cancel_on_the_new_client_beats_the_old_clients_hand_over():
    # The program is switched on through the new client (cancel_waiting_setpoint),
    # then the old client is stopped: its stale 22 must not be sent after that.
    old = NexhoLocalClient("10.0.0.99")
    _zone_ready(old)
    _scripted_sends(old, {22.0: [False]})
    old.set_temperature("2", 22.0)
    _wait_until(lambda: old.list_rooms()[0].unconfirmed, "the not-confirmed mark")
    new = _idle_client()
    new_sent = _scripted_sends(new, {22.0: [True]})
    new.start()
    new.cancel_waiting_setpoint("2")
    old.stop()
    _wait_for_writer(old, 2)
    new.stop()
    assert new_sent == [] and 2 not in new._desired


def test_the_poller_announces_the_zone_list_when_it_appears_or_changes():
    announced: list[list[tuple[str, str]]] = []
    replies = {"OPS2/": "OPOK,OPS2,1,2", "R#": "ER,1"}
    client = make_client(replies, names={"1": "Lounge"})
    client._on_zones = announced.append
    client._poll_once()
    client._poll_once()  # same list: nothing new to announce
    assert announced == [[("1", "Lounge"), ("2", "Zone 2")]]
    replies["OPS2/"] = "OPOK,OPS2,1"
    client._poll_once()
    client._poll_once()  # a changed list is believed the second time
    assert announced[-1] == [("1", "Lounge")] and len(announced) == 2


def test_a_stopped_client_raises_client_stopped(monkeypatch):
    from hearthmage.nexho_client import ClientStopped

    _fake_hub(monkeypatch, lambda cmd: "OK")
    client = NexhoLocalClient("10.0.0.99")
    client.stop()
    assert client.stopped() is True
    with pytest.raises(ClientStopped):
        client._ask("OPS2/", 1, 1.0)


def test_an_ack_between_the_stop_snapshot_and_the_hand_over_is_not_resent(monkeypatch):
    # stop() has taken its snapshot (22 still unacked) but not yet handed it
    # over when the ack comes in. The next client must not send 22 again.
    import threading

    import hearthmage.nexho_client as nc

    on_air, answer = threading.Event(), threading.Event()

    def reply(cmd):
        if cmd.startswith("D#2") and not on_air.is_set():
            on_air.set()
            answer.wait(2)
        return "OK"

    sent = _fake_hub(monkeypatch, reply)
    old = _idle_client()
    old.start()  # owner of the wire
    real_hand_over = nc._Wire.hand_over
    handing, go = threading.Event(), threading.Event()

    def gated(self, client, waiting):
        handing.set()
        go.wait(2)
        return real_hand_over(self, client, waiting)

    monkeypatch.setattr(nc._Wire, "hand_over", gated)
    stopping = threading.Thread(target=old.stop)
    try:
        old.set_temperature("2", 22.0)
        assert on_air.wait(2)
        stopping.start()
        assert handing.wait(2)
        answer.set()  # the ack lands now
        _wait_for_writer(old, 2)
    finally:
        answer.set()
        go.set()
        if stopping.is_alive():
            stopping.join(2)
        old.stop()
    monkeypatch.setattr(nc._Wire, "hand_over", real_hand_over)
    new = _idle_client()
    new.start()
    try:
        _wait_for_writer(new, 2)
    finally:
        new.stop()
    assert [c for c in sent if c.startswith("D#2")] == ["D#2#1#0#0*T22/"]
    assert 2 not in new._desired


# ---- MQTT mode=heat: back on to the last on-target (9cg) -----------------


def _acked_client():
    """An idle client whose sends are acknowledged at once, run in line."""
    client = _idle_client()
    sent = _scripted_sends(client, {t / 2: [True] for t in range(2, 61)})
    return client, sent


def test_turn_on_restores_the_last_target_set_before_off():
    client, sent = _acked_client()
    client.set_temperature("2", 21.5)
    _wait_for_writer(client, 2)
    client.set_temperature("2", 0)  # off from the web UI
    _wait_for_writer(client, 2)
    assert client.turn_on("2") is True
    _wait_for_writer(client, 2)
    assert sent == [21.5, 1.0, 21.5]
    assert client.list_rooms()[0].target_temp == 21.5


def test_turn_on_uses_the_last_fresh_reading_above_off():
    client = make_client({"R#2#": "OK,19,00,019"})
    client._poll_zone(2)  # the radiator holds 19 (set on its keypad, say)
    sent = _scripted_sends(client, {1.0: [True], 19.0: [True]})
    client.set_temperature("2", 0)
    _wait_for_writer(client, 2)
    client._ask = lambda cmd, *a, **k: "OK,19,00,000"  # type: ignore[method-assign]
    client._poll_zone(2)  # a fresh read of off does not replace it
    assert client.turn_on("2") is True
    _wait_for_writer(client, 2)
    assert sent == [1.0, 19.0]


def test_turn_on_falls_back_to_the_default_when_nothing_is_known():
    client, sent = _acked_client()
    with client._lock:
        client._state[2] = client._room(2, 19.0, 0.0, "ok")  # off, never seen on
    assert client.turn_on("2") is True
    _wait_for_writer(client, 2)
    assert sent == [20.0]


def test_turn_on_does_nothing_to_a_zone_that_is_on():
    # A holiday hold at 7 C is not off; nor is a target just set in the web UI.
    client, sent = _acked_client()
    with client._lock:
        client._state[2] = client._room(2, 15.0, 7.0, "ok")
    assert client.turn_on("2") is False
    with client._lock:
        client._state[2] = client._room(2, 15.0, None, "loading")  # not known to be off
    assert client.turn_on("2") is False
    assert sent == [] and 2 not in client._desired


def test_turn_on_stores_only_validated_clamped_targets():
    client, sent = _acked_client()
    client.set_temperature("2", 40.0)  # clamped to 30
    _wait_for_writer(client, 2)
    with pytest.raises(HearthError):
        client.set_temperature("2", float("nan"))
    client.set_temperature("2", 0)
    _wait_for_writer(client, 2)
    client.turn_on("2")
    _wait_for_writer(client, 2)
    assert sent == [30.0, 1.0, 30.0]


def test_turn_on_refuses_an_unknown_zone_and_grows_nothing():
    client = make_client({"OPS2/": "OPOK,OPS2,1,2", "OPS3/": "OPOK,OPS3,1,1"})
    client._zone_ids()
    before = {name: dict(value) if isinstance(value, dict) else set(value)
              for name, value in vars(client).items() if isinstance(value, (dict, set))}
    for bogus in ("99", "abc", "-1", ""):
        with pytest.raises(HearthError):
            client.turn_on(bogus)
    after = {name: dict(value) if isinstance(value, dict) else set(value)
             for name, value in vars(client).items() if isinstance(value, (dict, set))}
    assert after == before


def test_turn_on_does_not_grow_maps_before_the_hub_has_answered():
    client = NexhoLocalClient("10.0.0.99")  # no zone list, no readings cache
    with pytest.raises(HearthError, match="Still connecting"):
        client.turn_on("42")
    assert 42 not in client._state and 42 not in client._desired and 42 not in client._last_on


def test_turn_on_checks_and_writes_under_one_lock_hold():
    # A web-UI target that comes in while turn_on is deciding must land after
    # turn_on's write, so it is the one kept, never overwritten by it. The
    # lock below hands over to the waiting web write every time turn_on lets
    # go of it, so a gap between the check and the write would let it in.
    import threading

    client, sent = _acked_client()
    with client._lock:
        client._state[2] = client._room(2, 19.0, 1.0, "ok")
        client._last_on[2] = 21.0
    me = threading.current_thread()
    web_done = threading.Event()

    class YieldingLock:
        def __init__(self):
            self.inner = threading.Lock()
            self.armed = False

        def __enter__(self):
            self.inner.acquire()
            return self

        def __exit__(self, *exc):
            self.inner.release()
            if self.armed and threading.current_thread() is me:
                self.armed = False
                web_done.wait(2)  # the web write gets the lock now
            return False

    lock = YieldingLock()
    client._lock = lock  # type: ignore[assignment]

    class Probe(dict):
        def get(self, key, default=None):  # turn_on's check, lock held
            threading.Thread(
                target=lambda: (client.set_temperature("2", 23.0), web_done.set())
            ).start()
            lock.armed = True
            return super().get(key, default)

    client._last_on = Probe(client._last_on)
    assert client.turn_on("2") is True
    assert web_done.wait(2)
    _wait_for_writer(client, 2)
    assert client.list_rooms()[0].target_temp == 23.0
    assert sent[-1] == 23.0


def test_the_last_on_target_is_carried_to_the_hubs_next_client():
    old, _ = _acked_client()
    old.start()
    old.set_temperature("2", 22.5)
    _wait_for_writer(old, 2)
    old.stop()
    new, sent = _acked_client()
    with new._lock:
        new._state[2] = new._room(2, 19.0, 1.0, "ok")
    new.start()
    try:
        assert new.turn_on("2") is True
        _wait_for_writer(new, 2)
    finally:
        new.stop()
    assert sent == [22.5]


def test_the_last_on_target_reaches_a_client_that_started_first():
    old, _ = _acked_client()
    old.start()
    old.set_temperature("2", 22.5)
    _wait_for_writer(old, 2)
    new, sent = _acked_client()
    new.start()  # the new owner is running before the old one stops
    old.stop()
    with new._lock:
        new._state[2] = new._room(2, 19.0, 1.0, "ok")
    try:
        new.turn_on("2")
        _wait_for_writer(new, 2)
    finally:
        new.stop()
    assert sent == [22.5]


def test_a_forgotten_zone_takes_its_last_on_target_with_it():
    client = make_client({"OPS2/": "OPOK,OPS2,1", "OPS3/": "OPOK,OPS3,1"})
    with client._lock:
        client._last_on[2] = 21.0
    client._wire.note_sent(2, 1.0)
    client._zone_ids()
    client._zone_ids()  # said twice: believed
    assert 2 not in client._last_on and client._wire.last_sent(2) is None


# ---- rate limits on hub traffic from web requests (1pj.6) ----------------


def _gap_clock(monkeypatch, client, gap=1.0, clock=None):
    """A fake monotonic clock; waiting out the send gap moves it on."""
    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc, "_SETPOINT_MIN_GAP", gap)
    clock = clock if clock is not None else {"now": 100.0}
    monkeypatch.setattr(nc.time, "monotonic", lambda: clock["now"])
    waits: list[float] = []

    def wait(seconds):
        waits.append(round(seconds, 6))
        clock["now"] += seconds

    client._wait_send_gap = wait  # type: ignore[method-assign]
    return clock, waits


def test_consecutive_setpoint_sends_to_a_zone_are_a_minimum_gap_apart(monkeypatch):
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    clock, waits = _gap_clock(monkeypatch, client)
    sends: list[tuple[float, float]] = []

    def send_once(zone, target):
        sends.append((round(clock["now"], 6), target))
        clock["now"] += 0.2  # the exchange itself
        client._wire.note_sent(zone, clock["now"])  # a datagram went out
        if len(sends) < 3:  # a loop of POSTs: a newer target while each is on the air
            with client._lock:
                client._want_setpoint(zone, target + 1, clock["now"])
        return True

    client._send_once = send_once  # type: ignore[method-assign]
    _queue_write(client, 2, 21.0)
    client._drain_writes(2)
    assert sends == [(100.0, 21.0), (101.2, 22.0), (102.4, 23.0)]
    assert waits == [1.0, 1.0]


def test_the_send_gap_holds_across_workers(monkeypatch):
    # Each POST is acked before the next: every worker ends, the next starts.
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    clock, waits = _gap_clock(monkeypatch, client)
    sends: list[float] = []

    def send_once(zone, target):
        sends.append(round(clock["now"], 6))
        client._wire.note_sent(zone, clock["now"])
        return True

    client._send_once = send_once  # type: ignore[method-assign]
    _queue_write(client, 2, 21.0)
    client._drain_writes(2)
    clock["now"] += 0.3
    _queue_write(client, 2, 22.0)
    client._drain_writes(2)
    assert sends == [100.0, 101.0] and waits == [0.7]


def test_the_first_send_after_a_quiet_spell_is_not_delayed(monkeypatch):
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    clock, waits = _gap_clock(monkeypatch, client)
    client._send_once = lambda zone, target: client._wire.note_sent(zone, clock["now"]) or True  # type: ignore[method-assign]
    _queue_write(client, 2, 21.0)
    client._drain_writes(2)
    clock["now"] += 1.0
    _queue_write(client, 2, 22.0)
    client._drain_writes(2)
    _queue_write(client, 3, 22.0)  # another zone has its own gap
    client._drain_writes(3)
    assert waits == []


def test_a_cancel_during_the_send_gap_sends_nothing(monkeypatch):
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    clock, _ = _gap_clock(monkeypatch, client)
    sends: list[float] = []
    client._send_once = lambda zone, target: (  # type: ignore[method-assign]
        sends.append(target) or client._wire.note_sent(zone, clock["now"]) or True
    )
    _queue_write(client, 2, 21.0)
    client._drain_writes(2)
    client._wait_send_gap = lambda seconds: client.cancel_waiting_setpoint("2")  # type: ignore[method-assign]
    _queue_write(client, 2, 22.0)
    client._drain_writes(2)
    assert sends == [21.0] and 2 not in client._writers


def _blocking_reads(client, reply):
    """Replace _ask: the first command blocks until released. Returns
    (sent, on_air, release)."""
    import threading

    sent: list[str] = []
    on_air, release = threading.Event(), threading.Event()

    def ask(cmd, *a, **k):
        sent.append(cmd)
        if not on_air.is_set():
            on_air.set()
            assert release.wait(2)
        return reply(cmd)

    client._ask = ask  # type: ignore[method-assign]
    return sent, on_air, release


_CONFIG_REPLIES = {"*?E#0#8/": "OK,1", "*?E#0#6/": "OK,0", "*?E#1#20/": "OK,7", "*?E#0#9/": "OK,0"}


@pytest.mark.parametrize("method, reply, kind", [
    ("read_config", lambda cmd: next(v for k, v in _CONFIG_REPLIES.items() if cmd.endswith(k)), "config"),
    ("read_holiday", lambda cmd: "OK,RH,14,5,5,22,1,47,6,11,4,17,30,148,6,1,0", "holiday"),
    ("read_energy", lambda cmd: "OK,1000,12,40,35,50,0,22,18,7", "energy"),
])
def test_a_second_slow_read_of_a_zone_shares_the_one_running(method, reply, kind):
    import threading

    client = _idle_client()
    sent, on_air, release = _blocking_reads(client, reply)
    results: dict[str, object] = {}
    first = threading.Thread(target=lambda: results.__setitem__("first", getattr(client, method)("2")))
    first.start()
    assert on_air.wait(2)
    second = threading.Thread(target=lambda: results.__setitem__("second", getattr(client, method)("2")))
    second.start()
    _wait_until(lambda: client._reads[(kind, 2)].waiters == 1, "the second caller to wait")
    on_air_count = len(sent)
    release.set()
    first.join(2)
    second.join(2)
    assert results["first"] is not None and results["second"] == results["first"]
    assert results["second"] is not results["first"]  # a copy: one caller cannot change the other's
    one_read = len(sent)
    assert on_air_count == 1
    getattr(client, method)("2")  # once it is done, the next call reads again
    assert len(sent) == 2 * one_read
    assert client._reads == {}


def test_a_shared_read_waits_only_so_long(monkeypatch):
    import threading

    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc, "_SHARED_READ_WAIT", 0.01)
    client = _idle_client()
    sent, on_air, release = _blocking_reads(client, lambda cmd: "ER,1")
    first = threading.Thread(target=lambda: client.read_config("2"))
    first.start()
    try:
        assert on_air.wait(2)
        assert client.read_config("2") is None  # gave up waiting; sent nothing itself
        assert sent == ["R#2#1#0#0*?E#0#8/"]
    finally:
        release.set()
        first.join(2)


def test_a_shared_read_failure_reaches_the_waiting_caller_too():
    import threading

    client = _idle_client()
    on_air, release = threading.Event(), threading.Event()

    def failing_read(zone):
        on_air.set()
        assert release.wait(2)
        raise HearthError("radio failed")

    errors: list[str] = []

    def call():
        try:
            client._read_once("config", 2, lambda: failing_read(2))
        except HearthError as exc:
            errors.append(str(exc))

    first, second = threading.Thread(target=call), threading.Thread(target=call)
    first.start()
    assert on_air.wait(2)
    second.start()
    _wait_until(lambda: client._reads[("config", 2)].waiters == 1, "the second caller to wait")
    release.set()
    first.join(2)
    second.join(2)
    assert errors == ["radio failed", "radio failed"]


@pytest.mark.parametrize("write, method, reply", [
    (lambda c: c.set_config("2", frost_temp=10), "read_config",
     lambda cmd: "OK" if "*SEP" in cmd else next(v for k, v in _CONFIG_REPLIES.items() if cmd.endswith(k))),
    (lambda c: c.set_holiday("2", days=3, hour=9, minute=0, arrival_temp=None), "read_holiday",
     lambda cmd: "OK" if "*RH#" in cmd else "OK,RH,14,5,5,22,1,47,6,11,3,9,0,251,6,1,0"),
    (lambda c: c.clear_holiday("2"), "read_holiday",
     lambda cmd: "OK" if "*RH#" in cmd else "OK,RH,14,5,5,22,1,47,6,11,253,0,0,253,6,1,0"),
])
def test_a_read_after_a_write_does_not_share_one_from_before_it(monkeypatch, write, method, reply):
    # The page shown after a save must read the radiator again, not take the
    # answer of a read that was already running before the write went out.
    import threading

    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc, "_SHARED_READ_WAIT", 0.01)
    client = _idle_client()
    sent, on_air, release = _blocking_reads(client, reply)
    before = threading.Thread(target=lambda: getattr(client, method)("2"))
    before.start()
    try:
        assert on_air.wait(2)
        write(client)
        assert getattr(client, method)("2") is not None  # its own read, not a wait
    finally:
        release.set()
        before.join(2)
    assert client._reads == {}


# ---- review 2026-11-26: zone ids, connecting, waiters, gap on the wire ----


@pytest.mark.parametrize("bogus", ["-5", "0", "+7", "1_000", "\u0663", " 1", "1 ", "256", "", "1.0"])
def test_a_zone_id_is_a_plain_number_from_1_to_255(bogus):
    from hearthmage.nexho_client import _zone_int

    with pytest.raises(HearthError, match="Invalid zone id"):
        _zone_int(bogus)


def test_zone_ids_at_the_ends_of_the_range_are_accepted():
    from hearthmage.nexho_client import _zone_int

    assert (_zone_int("1"), _zone_int("255")) == (1, 255)


def test_nothing_is_sent_before_the_hub_has_listed_its_zones():
    # A setpoint for "-5" (or any number) before the hub's first answer went on
    # the wire and left a phantom zone that then stood in for the zone list.
    import threading

    client = NexhoLocalClient("10.0.0.99")  # no readings cache
    sent: list[str] = []
    client._ask = lambda cmd, *a, **k: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    threads = threading.active_count()
    for zone in ("-5", "1"):
        with pytest.raises(HearthError):
            client.set_temperature(zone, 20)
    with pytest.raises(HearthError, match="Still connecting"):
        client.set_temperature("1", 20)
    with pytest.raises(HearthError, match="Still connecting"):
        client.read_config("1")
    assert sent == [] and client._state == {} and client._desired == {}
    assert threading.active_count() == threads


def test_the_hubs_first_zone_list_is_used_at_once():
    client = make_client({"OPS2/": "OPOK,OPS2,1,2", "OPS3/": "OPOK,OPS3,1,1", "D#": "OK"})
    client._cached_zones = None  # a cold start: no readings cache
    with pytest.raises(HearthError, match="Still connecting"):
        client.set_temperature("2", 20)
    client._zone_ids()  # one answer is enough the first time
    client.set_temperature("2", 20)
    with pytest.raises(HearthError, match="Unknown zone"):
        client.set_temperature("3", 20)


def test_zones_from_the_readings_cache_stand_in_until_the_hub_answers(tmp_path):
    cache = tmp_path / "readings.json"
    cache.write_text('{"1": [19.0, 21.0], "-5": [1, 2], "x": [1, 2]}')
    client = NexhoLocalClient("10.0.0.99", cache_file=str(cache))
    client._send_once = lambda zone, target: True  # type: ignore[method-assign]
    client.set_temperature("1", 20)
    with pytest.raises(HearthError, match="Unknown zone"):
        client.set_temperature("2", 20)  # a write never makes a zone known
    assert sorted(client._state) == [1]


def test_a_shared_read_takes_only_so_many_waiters(monkeypatch):
    # Each waiter holds a request thread; past the cap a caller gets None at
    # once (the page shows that the settings could not be read).
    import threading

    import hearthmage.nexho_client as nc

    monkeypatch.setattr(nc, "_SHARED_READ_WAIT", 30.0)
    client = _idle_client()
    sent, on_air, release = _blocking_reads(
        client, lambda cmd: next(v for k, v in _CONFIG_REPLIES.items() if cmd.endswith(k))
    )
    threads = [threading.Thread(target=lambda: client.read_config("2")) for _ in range(5)]
    threads[0].start()
    try:
        assert on_air.wait(2)
        for thread in threads[1:]:
            thread.start()
        _wait_until(lambda: client._reads[("config", 2)].waiters == nc._SHARED_READ_MAX_WAITERS,
                    "the waiters to queue")
        got: list[object] = []
        extra = threading.Thread(target=lambda: got.append(client.read_config("2")))
        extra.start()
        extra.join(1)
        assert not extra.is_alive() and got == [None]
        assert len(sent) == 1  # it put nothing on the wire
    finally:
        release.set()
        for thread in threads:
            thread.join(2)


def test_the_send_gap_holds_across_a_client_swap(monkeypatch):
    old = NexhoLocalClient("10.0.0.99")
    _zone_ready(old)
    clock, _ = _gap_clock(monkeypatch, old)
    old._send_once = lambda zone, target: old._wire.note_sent(zone, clock["now"]) or True  # type: ignore[method-assign]
    _queue_write(old, 2, 21.0)
    old._drain_writes(2)
    old.stop()
    new = NexhoLocalClient("10.0.0.99")
    _zone_ready(new)
    _, waits = _gap_clock(monkeypatch, new, clock=clock)
    new._send_once = lambda zone, target: True  # type: ignore[method-assign]
    clock["now"] += 0.3
    _queue_write(new, 2, 22.0)
    new._drain_writes(2)
    assert waits == [0.7]


def test_the_send_time_is_noted_only_when_a_datagram_went_out(monkeypatch):
    import hearthmage.nexho_client as nc

    clock = {"now": 100.0}
    monkeypatch.setattr(nc.time, "monotonic", lambda: clock["now"])
    sent = _fake_hub(monkeypatch, lambda cmd: "OK")
    client = NexhoLocalClient("10.0.0.99")
    _zone_ready(client)
    _queue_write(client, 2, 22.0)
    settle = client._let_hub_settle
    client._let_hub_settle = lambda: client.cancel_waiting_setpoint("2")  # type: ignore[method-assign]
    assert client._send_once(2, 22.0) is None  # withdrawn before it went out
    assert sent == [] and client._wire.last_sent(2) is None
    client._let_hub_settle = settle  # type: ignore[method-assign]
    _queue_write(client, 2, 23.0)
    clock["now"] = 105.0
    assert client._send_once(2, 23.0) is True
    assert client._wire.last_sent(2) == 105.0


def test_a_reading_during_a_recorded_holiday_hold_is_not_the_last_on_target():
    # Owner decision 2026-11-26: the 7 C hold level must not become the target
    # that mode=heat restores; keypad changes outside a hold still count.
    held = {"on": False}
    client = make_client({"R#2#": "OK,15,00,007"})
    client._is_held = lambda zone: held["on"] and zone == 2
    _zone_ready(client)
    sent = _scripted_sends(client, {21.0: [True], 1.0: [True]})
    client.set_temperature("2", 21.0)
    _wait_for_writer(client, 2)
    held["on"] = True  # the app has a holiday recorded for zone 2
    client._poll_zone(2)
    client.set_temperature("2", 0)
    _wait_for_writer(client, 2)
    assert client.turn_on("2") is True
    _wait_for_writer(client, 2)
    assert sent == [21.0, 1.0, 21.0]


def test_the_held_hook_is_a_constructor_option_and_a_failing_one_skips_the_note():
    def broken(zone):
        raise RuntimeError("store unreadable")

    client = NexhoLocalClient("10.0.0.99", is_held=broken)
    _zone_ready(client)
    client._read_zone_raw = lambda zone: (15.0, 7.0, True)  # type: ignore[method-assign]
    client._poll_zone(2)  # must not raise
    assert 2 not in client._last_on


def test_a_zone_list_id_out_of_range_is_dropped_and_logged_once(caplog):
    client = make_client({"OPS2/": "OPOK,OPS2,1,300,٣,2", "OPS3/": "OPOK,OPS3,1,1"})
    client._cached_zones = None
    assert client._zone_ids() == [1, 2]
    assert client._zone_ids() == [1, 2]
    rejected = [r for r in _warnings(caplog) if "zone list" in r]
    assert len(rejected) == 1 and "300" in rejected[0]
    client.set_temperature("2", 20)  # the real zones still work
