"""Optional MQTT bridge exposing each zone as a Home Assistant climate entity.

This lets HomeKit, geofencing, and automations reach the heaters through Home
Assistant while THIS app stays the single owner of the hub.

HARD RULE: the hub services one conversation at a time (an RF attempt blocks it
for several seconds), so nothing else may talk to the hub directly. Home
Assistant and every other integration must go through this app, over MQTT (or
the web UI / REST) - never straight to the hub. This bridge is the only place
that both publishes hub state and accepts commands, and it funnels every command
back through the same serialised client the web UI uses.

The payload builders and command router here are pure and unit-tested; the paho
MQTT client is imported lazily in :meth:`connect`, so this module has no hard
dependency on paho unless the bridge is actually switched on.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

_log = logging.getLogger("hearthmage.mqtt")

_MIN_TEMP = 5
_MAX_TEMP = 30


class MqttBridge:
    def __init__(
        self,
        publish: Callable[..., None],
        handler,
        base_topic: str = "hearthmage",
        node_id: str = "hub",
    ) -> None:
        self._publish = publish
        self._handler = handler
        self._base = base_topic.rstrip("/")
        self._node = node_id
        self._zones: list[tuple[str, str]] = []  # last-announced zones, for reconnects

    # ---- topics ----------------------------------------------------------

    def _state_topic(self, zone: str) -> str:
        return f"{self._base}/{zone}/state"

    def availability_topic(self) -> str:
        return f"{self._base}/status"

    def command_topic_filter(self) -> str:
        """Single wildcard subscription covering every zone's command topics."""
        return f"{self._base}/+/+/set"

    # ---- outbound: discovery + state ------------------------------------

    def _discovery(self, zone: str, name: str) -> dict:
        state = self._state_topic(zone)
        return {
            "name": name,
            "unique_id": f"hearthmage_{self._node}_{zone}",
            "modes": ["off", "heat"],
            "min_temp": _MIN_TEMP,
            "max_temp": _MAX_TEMP,
            "temp_step": 0.5,
            "temperature_unit": "C",
            "current_temperature_topic": state,
            "current_temperature_template": "{{ value_json.current_temperature }}",
            "temperature_state_topic": state,
            "temperature_state_template": "{{ value_json.temperature }}",
            "temperature_command_topic": f"{self._base}/{zone}/temperature/set",
            "mode_state_topic": state,
            "mode_state_template": "{{ value_json.mode }}",
            "mode_command_topic": f"{self._base}/{zone}/mode/set",
            "availability_topic": self.availability_topic(),
            "device": {
                "identifiers": [f"hearthmage_{self._node}"],
                "name": "HearthMage",
                "manufacturer": "Farho Nexho NT (via HearthMage)",
            },
        }

    def publish_discovery(self, zones: list[tuple[str, str]]) -> None:
        """Announce each (zone_id, name) as an HA climate entity (retained).
        The list is remembered so every broker (re)connect can re-announce it."""
        self._zones = list(zones)
        for zone, name in zones:
            topic = f"homeassistant/climate/{self._node}/{zone}/config"
            self._publish(topic, json.dumps(self._discovery(zone, name)), retain=True)

    def has_zones(self) -> bool:
        """Whether discovery has ever been published with a non-empty zone list."""
        return bool(self._zones)

    def republish_discovery(self) -> None:
        """Re-announce the last-known zones; a no-op until they are known."""
        if self._zones:
            self.publish_discovery(self._zones)

    def publish_state(self, zone: str, current, setpoint) -> None:
        payload = {
            "current_temperature": current,
            "temperature": setpoint,
            "mode": "off" if (setpoint is None or setpoint <= 0) else "heat",
        }
        self._publish(self._state_topic(str(zone)), json.dumps(payload), retain=True)

    def publish_availability(self, online: bool) -> None:
        self._publish(self.availability_topic(), "online" if online else "offline", retain=True)

    # ---- inbound: commands ----------------------------------------------

    def on_message(self, topic: str, payload: str) -> None:
        """Route an inbound command topic to the hub client. Everything flows
        back through the app's serialised client, never straight to the hub."""
        parts = topic.split("/")
        # <base>/<zone>/<field>/set
        if len(parts) != 4 or parts[0] != self._base or parts[3] != "set":
            return
        zone, field = parts[1], parts[2]
        if field == "temperature":
            try:
                value = float(payload)
            except (TypeError, ValueError):
                return
            self._handler.set_temperature(zone, value)
        elif field == "mode":
            if payload.strip().lower() == "off":
                self._handler.set_temperature(zone, 0.0)
            # "heat" is driven by the temperature command; nothing to do here.

    # ---- network (lazy paho; not covered by unit tests) -----------------

    def connect(self, host: str, port: int, username: str | None, password: str | None):
        """Connect to the broker and start the network loop. Returns the client."""
        import paho.mqtt.client as mqtt  # imported here so paho is optional

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if username:
            client.username_pw_set(username, password or "")
        client.will_set(self.availability_topic(), "offline", retain=True)

        def _on_connect(cli, _userdata, _flags, _reason_code, _properties):
            cli.subscribe(self.command_topic_filter())
            self.publish_availability(True)
            self.republish_discovery()  # HA must re-learn the zones on every connect

        def _on_message(_cli, _userdata, msg):
            try:
                self.on_message(msg.topic, msg.payload.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001 - a bad command must not kill the loop
                _log.exception("MQTT command handling failed for %s", msg.topic)

        client.on_connect = _on_connect
        client.on_message = _on_message
        # Rebind publish to the real client now that we have one.
        self._publish = lambda topic, payload, retain=False: client.publish(
            topic, payload, retain=retain
        )
        # connect_async + loop_start: the TCP connect (and retries) happen on
        # paho's daemon network thread, so a dead broker never blocks a request.
        client.connect_async(host, port)
        client.loop_start()
        _log.info("MQTT bridge connecting to %s:%s", host, port)
        return client
