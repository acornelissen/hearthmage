from hearthmage.energy import (
    COUNTER_HOURS,
    cost,
    counter_to_kwh,
    daily_kwh,
    parse_energy,
)


def test_parse_energy_splits_watts_and_counters():
    watts, counters = parse_energy("OK,1000,12,40,35,50,0,22,18,7")
    assert watts == 1000
    assert counters == [12, 40, 35, 50, 0, 22, 18, 7]


def test_parse_energy_unreachable_returns_none():
    assert parse_energy("ER,1") == (None, [])
    assert parse_energy("(timeout)") == (None, [])


def test_parse_energy_stops_at_non_numeric_tail():
    watts, counters = parse_energy("OK,900,5,6,,")
    assert watts == 900
    assert counters == [5, 6]  # trailing empty fields dropped


def test_counter_to_kwh_uses_six_minute_buckets():
    # counter counts 6-minute on-intervals: kWh = counter * (6/60) * (W/1000)
    assert counter_to_kwh(10, 1000) == 1.0  # 10 * 0.1h * 1kW
    assert counter_to_kwh(0, 1500) == 0.0
    assert COUNTER_HOURS == 6 / 60


def test_daily_kwh_maps_each_counter():
    assert daily_kwh(1000, [10, 5, 0]) == [1.0, 0.5, 0.0]
    assert daily_kwh(None, [10]) == [0.0]  # unknown wattage -> zero, not a crash


def test_cost_multiplies_by_price():
    assert cost(2.5, 0.30) == 0.75
    assert cost(0.0, 0.30) == 0.0
