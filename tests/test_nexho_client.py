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


def _holiday_client(reply: str = "OK"):
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
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


def _recording_client(keypad_raw: str = "OK,0"):
    sent: list[str] = []
    client = NexhoLocalClient("10.0.0.99")

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
    client._ask = lambda cmd, *a, **k: replies["D#" if cmd.startswith("D#") else cmd].pop(0)  # type: ignore[method-assign]
    client.set_day_pattern("1", [Block(20, 0, 60)], [0])  # does not raise
    assert client.read_day_pattern("1", 0) == []


def test_program_write_gives_up_after_repeated_er(monkeypatch):
    import hearthmage.nexho_client as nc
    from hearthmage.schedule import Block

    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    calls: list[str] = []
    client = NexhoLocalClient("10.0.0.99")
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
    client._ask = lambda cmd, retries, timeout, priority=False: seen.append(timeout) or "OK"  # type: ignore[method-assign]
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
