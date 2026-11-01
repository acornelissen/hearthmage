from hearthmage.waste import WasteDetector

MIN = 60


def _feed(det, readings, zone="1"):
    """Feed (minute, current, setpoint) readings; return the non-None changes."""
    out = []
    for minute, current, setpoint in readings:
        change = det.feed(zone, minute * MIN, current, setpoint)
        if change is not None:
            out.append(change)
    return out


def test_fast_rise_with_a_low_target_starts_an_event():
    det = WasteDetector()
    changes = _feed(det, [(0, 13.0, 5.0), (5, 13.0, 5.0), (10, 13.3, 5.0), (15, 13.6, 5.0)])
    kind, event = changes[0]
    assert kind == "start"
    assert event.zone == "1"
    assert event.start_ts == 5 * MIN  # the last reading before the climb
    assert event.start_temp == 13.0
    assert event.peak_temp == 13.6
    assert event.setpoint == 5.0


def test_slow_rise_is_not_an_event():
    # ~1 C/h, the shape of a sunny afternoon: never 0.5 C within 15 minutes.
    det = WasteDetector()
    readings = [(m, 13.0 + m / 60, 5.0) for m in range(0, 120, 5)]
    assert _feed(det, readings) == []


def test_rise_while_heat_was_asked_for_is_not_an_event():
    det = WasteDetector()
    assert _feed(det, [(0, 13.0, 18.0), (5, 13.4, 18.0), (10, 13.8, 18.0)]) == []


def test_rise_right_after_lowering_the_target_is_not_an_event():
    # The target dropped mid-window; the radiator was still asked for heat just before.
    det = WasteDetector()
    assert _feed(det, [(0, 15.0, 18.0), (5, 15.3, 5.0), (10, 15.6, 5.0)]) == []


def test_event_updates_then_ends_when_the_climb_stops():
    det = WasteDetector()
    readings = [(0, 13.0, 5.0), (5, 13.3, 5.0), (10, 13.6, 5.0), (15, 14.0, 5.0)]
    readings += [(m, 13.9, 5.0) for m in range(20, 40, 5)]  # flat after the peak at 15
    changes = _feed(det, readings)
    kinds = [k for k, _ in changes]
    assert kinds[0] == "start"
    assert kinds[-1] == "end"
    assert "start" not in kinds[1:]
    ended = changes[-1][1]
    assert ended.end_ts == 15 * MIN  # the peak, not when we noticed it stopped
    assert ended.peak_temp == 14.0


def test_event_ends_when_heat_is_asked_for():
    det = WasteDetector()
    changes = _feed(det, [(0, 13.0, 5.0), (5, 13.3, 5.0), (10, 13.6, 5.0), (12, 13.8, 20.0)])
    assert [k for k, _ in changes][-1] == "end"


def test_readings_without_a_temperature_or_target_are_ignored():
    det = WasteDetector()
    assert _feed(det, [(0, None, 5.0), (5, 13.0, None), (10, None, None)]) == []


def test_zones_are_independent():
    det = WasteDetector()
    _feed(det, [(0, 13.0, 5.0), (5, 13.3, 5.0), (10, 13.6, 5.0)], zone="1")
    assert _feed(det, [(0, 13.0, 5.0), (5, 13.0, 5.0)], zone="2") == []


def test_baseline_is_due_when_resting_then_waits():
    det = WasteDetector()
    det.feed("1", 0, 13.0, 5.0)
    assert det.baseline_due("1", 0, 13.0, 5.0) is True
    assert det.baseline_due("1", 60, 13.0, 5.0) is False  # just taken
    assert det.baseline_due("1", 7 * 3600, 13.0, 5.0) is True  # hours later


def test_no_baseline_while_heating_or_during_an_event():
    det = WasteDetector()
    assert det.baseline_due("1", 0, 13.0, 18.0) is False  # heat is asked for
    _feed(det, [(0, 13.0, 5.0), (5, 13.3, 5.0), (10, 13.6, 5.0)])
    assert det.baseline_due("1", 11 * MIN, 13.6, 5.0) is False  # event open


def test_a_sudden_jump_is_not_an_event():
    # Live zone 2: +0.5 C in 1-3 minutes, far faster than any radiator heats a
    # room (deliberate heating peaks around +4.5 C/h). Warm air or noise.
    det = WasteDetector()
    readings = [(0, 13.4, 5.0), (1, 13.9, 5.0), (3, 13.9, 5.0), (6, 13.5, 5.0), (12, 13.5, 5.0)]
    assert _feed(det, readings) == []


def test_status_diff_skips_temperature_and_clock():
    from hearthmage.waste import status_diff

    rest = "OK,13,0,5,21,19,17,6,11,69,0,0,0,0,1,0,44,244,0,33,v200110"
    heat = "OK,14,5,5,3,13,40,2,11,72,0,1,0,0,1,0,44,244,0,33,v200110"
    rows = status_diff(rest, heat)
    assert [(r["index"], r["rest"], r["heating"]) for r in rows] == [
        (7, "6", "2"),  # weekday: different day, still listed (it is not the clock time)
        (9, "69", "72"),
        (11, "0", "1"),
    ]
    assert rows[0]["label"] == "Weekday"
    assert rows[1]["label"] == "Field 9"


def test_status_diff_of_identical_or_unusable_replies():
    from hearthmage.waste import status_diff

    same = "OK,13,0,5,21,19,17,6,11,69"
    assert status_diff(same, same) == []
    assert status_diff(None, same) == []
    assert status_diff("ER,1", same) == []
