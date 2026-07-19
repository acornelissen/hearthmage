import pytest

from hearthmage.domain import HearthError
from hearthmage.nexho_client import (
    NexhoLocalClient,
    _decode_setpoint,
    _encode_setpoint,
    _parse_status,
)


def make_client(responses: dict[str, str], names=None) -> NexhoLocalClient:
    """A client whose UDP layer is replaced by a canned-response lookup."""
    client = NexhoLocalClient("10.0.0.99", zone_names=names or {})

    def fake_ask(cmd: str, retries: int, timeout: float, priority: bool = False) -> str:
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
    client._ask = lambda cmd, r, t, priority=False: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    client.set_temperature("2", 0)  # Off
    room = next(r for r in client.list_rooms() if r.id == "2")
    assert room.is_off is True
    _queue_write(client, 2, 0.0)
    client._drain_writes(2)
    assert sent[-1] == "D#2#1#0#0*T0/"  # off sends *T0


def test_set_temperature_half_degree_rounds_to_nearest_half():
    client = make_client({"D#": "OK"})
    client.set_temperature("3", 20.6)
    room = next(r for r in client.list_rooms() if r.id == "3")
    assert room.target_temp == 20.5  # nearest 0.5, not integer
    assert room.is_off is False


def test_send_once_encodes_half_degree_on_the_wire():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, r, t, priority=False: sent.append(cmd) or "OK"  # type: ignore[method-assign]
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


def test_list_rooms_cold_start_seeds_loading():
    client = make_client({"OPS2/": "OPOK,OPS2,1,2"})
    rooms = client.list_rooms()  # no poll has run yet
    assert [r.id for r in rooms] == ["1", "2"]
    assert all(r.status == "loading" for r in rooms)


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
    client._ask = lambda cmd, retries, timeout, priority=False: sent.append(cmd) or "OK"  # type: ignore[method-assign]
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


def test_drain_keeps_optimistic_value_on_failure():
    client = make_client({"D#": "ER,1"})
    with client._lock:
        client._state[2] = client._room(2, 19.0, 20.0, "ok")  # optimistic target 20
        client._last[2] = (19.0, 18.0)  # last confirmed target was 18
    _queue_write(client, 2, 20)
    client._drain_writes(2)
    assert client._pending[2] == 20.0  # unconfirmed: poll keeps showing the target
    assert 2 not in client._writers
    room = next(r for r in client.list_rooms() if r.id == "2")
    assert room.error is None  # no scary error for an unconfirmed write
    assert room.target_temp == 20.0  # keeps the value the user set


def test_rapid_writes_coalesce_to_latest_target():
    """A newer tap arriving mid-send supersedes the in-flight one: the radiator
    receives the latest value last, and only that value is confirmed."""
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")

    def fake_ask(cmd, retries, timeout, priority=False):
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
            "R#1#1#0#0*UDEL/": "OK",
            "R#1#1#0#0*?UD/": "OK,1000,12,40",
            "R#1#1#0#0*?UM/": "OK,1000,300",
        }
    )
    client._on_energy = lambda zone, data: seen.append((zone, data))
    client._energy_interval = 3600.0
    client._next_energy = 0.0  # due now
    client._poll_once()  # temperature sweep + one energy read
    assert seen == [(1, {"rated_watts": 1000, "daily": [12, 40], "monthly": [300]})]


def test_energy_poll_step_skips_when_no_callback():
    client = make_client({"OPS2/": "OPOK,OPS2,1", "R#1#1#0#0*?T/": "OK,20,00,007"})
    client._next_energy = 0.0
    client._poll_once()  # must not attempt energy reads without a callback
    assert client._energy_queue == []


def test_set_holiday_sends_rh_with_encoded_temp():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, r, t, priority=False: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    client.set_holiday("1", day=5, month=11, temp=7.0)
    assert sent == ["D#1#1#0#0*RH#5#11#0#7/"]


def test_set_holiday_half_degree_temp_encoded():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, r, t, priority=False: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    client.set_holiday("2", day=1, month=1, temp=7.5)
    assert sent == ["D#2#1#0#0*RH#1#1#0#135/"]  # 7.5 -> 135 (whole/half encoding)


def test_clear_holiday_sends_off_sentinel():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, r, t, priority=False: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    client.clear_holiday("1")
    assert sent == ["D#1#1#0#0*RH#0#0#0#251/"]


def test_set_holiday_raises_when_not_acked():
    client = make_client({"D#1#1#0#0*RH": "ER,1"})
    with pytest.raises(HearthError):
        client.set_holiday("1", day=5, month=11, temp=7.0)


def test_read_holiday_active_and_inactive():
    active = make_client({"R#1#1#0#0*?RH/": "OK,RH,17,1,7,10,53,32,6,0,5,11,0,7,6,1,0,0"})
    assert active.read_holiday("1") == {"active": True, "day": 5, "month": 11, "temp": 7.0}

    off = make_client({"R#2#1#0#0*?RH/": "OK,RH,17,1,13,10,53,28,6,0,253,0,0,253,6,1"})
    assert off.read_holiday("2") == {"active": False, "day": None, "month": None, "temp": None}


def test_read_config_decodes_all_three():
    client = make_client(
        {
            "R#1#1#0#0*?E#0#8/": "OK,1",  # keypad locked
            "R#1#1#0#0*?E#0#6/": "OK,0",  # window sensor off
            "R#1#1#0#0*?E#1#20/": "OK,7",  # offset 7
        }
    )
    cfg = client.read_config("1")
    assert cfg == {"keypad_lock": True, "window_sensor": False, "offset": 7}


def test_read_config_none_when_asleep():
    client = make_client({"R#3#1#0#0*?E#0#8/": "ER,1"})
    assert client.read_config("3") is None


def test_set_config_writes_only_given_fields():
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
    client._ask = lambda cmd, r, t, priority=False: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    client.set_config("2", keypad_lock=True, offset=9)
    assert "D#2#1#0#0*SEP#0#8#1/" in sent  # keypad lock on
    assert "D#2#1#0#0*SEP#1#20#9/" in sent  # offset 9
    assert not any("0#6" in c for c in sent)  # window sensor not touched


def test_set_config_rejects_out_of_range_offset():
    client = NexhoLocalClient("10.0.0.99")
    with pytest.raises(HearthError):
        client.set_config("2", offset=99)


def test_set_config_raises_when_write_not_acked():
    client = make_client({"D#2#1#0#0*SEP#0#6#1/": "ER,1"})
    with pytest.raises(HearthError):
        client.set_config("2", window_sensor=True)


def test_read_energy_probes_then_reads():
    client = make_client(
        {
            "R#1#1#0#0*UDEL/": "OK",
            "R#1#1#0#0*?UD/": "OK,1000,12,40,35,50,0,22,18,7",
            "R#1#1#0#0*?UM/": "OK,1000,300,280,260",
        }
    )
    data = client.read_energy("1")
    assert data["rated_watts"] == 1000
    assert data["daily"] == [12, 40, 35, 50, 0, 22, 18, 7]
    assert data["monthly"] == [300, 280, 260]


def test_read_energy_none_when_probe_fails():
    client = make_client({"R#3#1#0#0*UDEL/": "ER,1"})
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
