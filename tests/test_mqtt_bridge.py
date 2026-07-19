import json

from hearthmage.mqtt_bridge import MqttBridge


class _Handler:
    def __init__(self):
        self.calls = []

    def set_temperature(self, zone_id, temperature):
        self.calls.append((zone_id, temperature))


def _bridge(handler=None):
    published = []
    bridge = MqttBridge(
        publish=lambda topic, payload, retain=False: published.append((topic, payload, retain)),
        handler=handler or _Handler(),
        base_topic="hearthmage",
        node_id="hub1",
    )
    return bridge, published


def test_discovery_declares_a_climate_entity():
    bridge, published = _bridge()
    bridge.publish_discovery([("1", "Living Room")])
    topic, payload, retain = published[0]
    assert topic == "homeassistant/climate/hub1/1/config"
    assert retain is True
    cfg = json.loads(payload)
    assert cfg["unique_id"] == "hearthmage_hub1_1"
    assert cfg["modes"] == ["off", "heat"]
    assert cfg["temperature_command_topic"] == "hearthmage/1/temperature/set"
    assert cfg["mode_command_topic"] == "hearthmage/1/mode/set"
    assert cfg["min_temp"] == 5 and cfg["max_temp"] == 30 and cfg["temp_step"] == 0.5


def test_publish_state_reports_temp_and_mode():
    bridge, published = _bridge()
    bridge.publish_state("1", current=20.4, setpoint=21.0)
    topic, payload, retain = published[-1]
    assert topic == "hearthmage/1/state"
    state = json.loads(payload)
    assert state["current_temperature"] == 20.4
    assert state["temperature"] == 21.0
    assert state["mode"] == "heat"


def test_publish_state_off_when_setpoint_zero():
    bridge, published = _bridge()
    bridge.publish_state("2", current=18.0, setpoint=0.0)
    state = json.loads(published[-1][1])
    assert state["mode"] == "off"


def test_temperature_command_calls_handler():
    handler = _Handler()
    bridge, _ = _bridge(handler)
    bridge.on_message("hearthmage/1/temperature/set", "20.5")
    assert handler.calls == [("1", 20.5)]


def test_mode_off_command_sets_zero():
    handler = _Handler()
    bridge, _ = _bridge(handler)
    bridge.on_message("hearthmage/3/mode/set", "off")
    assert handler.calls == [("3", 0.0)]


def test_mode_heat_command_is_ignored_without_temperature():
    handler = _Handler()
    bridge, _ = _bridge(handler)
    bridge.on_message("hearthmage/3/mode/set", "heat")
    assert handler.calls == []  # temperature command drives the value, not mode


def test_unknown_or_malformed_topic_is_ignored():
    handler = _Handler()
    bridge, _ = _bridge(handler)
    bridge.on_message("hearthmage/1/temperature/set", "not-a-number")
    bridge.on_message("some/other/topic", "20")
    assert handler.calls == []


def test_command_topics_lists_subscriptions():
    bridge, _ = _bridge()
    assert bridge.command_topic_filter() == "hearthmage/+/+/set"


def test_publish_discovery_remembers_zones():
    bridge, published = _bridge()
    bridge.publish_discovery([("1", "Living Room"), ("2", "Study")])
    assert bridge.has_zones() is True
    published.clear()
    # a broker reconnect must re-announce the same zones
    bridge.republish_discovery()
    assert [t for t, _, _ in published] == [
        "homeassistant/climate/hub1/1/config",
        "homeassistant/climate/hub1/2/config",
    ]


def test_republish_discovery_noop_without_zones():
    bridge, published = _bridge()
    assert bridge.has_zones() is False
    bridge.republish_discovery()
    assert published == []
