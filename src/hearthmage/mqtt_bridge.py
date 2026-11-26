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
import secrets
import socket
import string
import threading
from typing import Callable

from hearthmage.domain import MAX_TEMP, MIN_TEMP, TEMP_STEP
from hearthmage.lan import is_ip_literal, is_lan_broker_address

_log = logging.getLogger("hearthmage.mqtt")

_OFF_TEMP = 1  # the client sends Off as a 1 degree setpoint
_LOOKUP_POLL = 0.1  # seconds between stop checks while a broker lookup runs
_MAX_LOOKUPS = 4  # broker lookups waiting on DNS at once; past this, refuse
_RECONNECT_MIN_DELAY = 1  # paho's reconnect backoff, seconds (its defaults)
_RECONNECT_MAX_DELAY = 120
_SUPERVISE_EVERY = 1.0  # seconds between checks that paho's network thread is alive
_OFFLINE_WAIT = 1.0  # most seconds a stop waits for "offline" to go out
# Client ids are "hm" + node_id + this: never empty (paho answers a rejected
# empty id by reconnecting from inside its packet handler), and fixed for the
# life of the process so a restart takes over the broker session.
_CLIENT_SUFFIX = secrets.token_hex(4)
_ID_CHARS = frozenset(string.ascii_letters + string.digits)
_MAX_CLIENT_ID = 23  # the length every MQTT 3.1.1 broker must accept


def client_id(node_id: str) -> str:
    """A client id any broker accepts: at most 23 characters of [0-9A-Za-z]
    (MQTT 3.1.1, 3.1.3.1), "hm" + the node id's letters and digits, cut so the
    per-process suffix fits."""
    node = "".join(c for c in node_id if c in _ID_CHARS)
    room = _MAX_CLIENT_ID - len("hm") - len(_CLIENT_SUFFIX)
    return "hm" + node[:room] + _CLIENT_SUFFIX


class BrokerRefused(OSError):
    """No LAN address to open the broker connection on. An OSError, so paho
    counts the attempt as a failed connect and retries with its usual backoff
    (any other exception type would end paho's network thread)."""


def vetted_broker_address(host: str, port: int, stopping: threading.Event) -> str:
    """The address to open the broker connection on, or BrokerRefused.

    A name is resolved now, so a DNS or DHCP change is followed on every
    reconnect, and it is used only if every address it resolves to is RFC 1918
    IPv4 or loopback: one public A or AAAA record refuses the lot. An IP literal
    skips the lookup; it passed the same check at save time, but is checked
    again here."""
    if is_ip_literal(host):
        addresses = [host]
    else:
        addresses = _lookup(host, port, stopping)
    if not addresses or not all(is_lan_broker_address(a) for a in addresses):
        _log.warning(
            "Not connecting to MQTT broker %r: it resolves to %r, which is not all "
            "on the local network. Will retry.", host, addresses,
        )
        raise BrokerRefused(f"MQTT broker {host!r} is not on the local network")
    return addresses[0]


class _Lookup:
    """One DNS query in flight; every attempt that needs the same name waits on it."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.addresses: list[str] = []
        self.error: Exception | None = None


_lookups: dict[tuple[str, int], _Lookup] = {}  # in flight only; guarded by _lookups_lock
_lookups_lock = threading.Lock()


def _lookup(name: str, port: int, stopping: threading.Event) -> list[str]:
    """Every address ``name`` resolves to. The query runs on a helper thread
    so that ``stopping`` (set by loop_stop) ends the wait at once instead of
    holding a restart until a slow DNS server gives up. Attempts for a name
    already being resolved share that query, and at most _MAX_LOOKUPS run at
    once, so restarts against a hung DNS server cannot pile up threads."""
    key = (name, port)
    with _lookups_lock:
        lookup = _lookups.get(key)
        if lookup is None:
            if len(_lookups) >= _MAX_LOOKUPS:
                _log.warning("Not resolving MQTT broker %r: %d earlier lookups are still "
                             "waiting on DNS. Will retry.", name, len(_lookups))
                raise BrokerRefused("too many MQTT broker lookups in flight")
            lookup = _lookups[key] = _Lookup()
            threading.Thread(
                target=_resolve, args=(key, lookup), name="mqtt-resolve", daemon=True
            ).start()
    while not lookup.done.wait(_LOOKUP_POLL):
        if stopping.is_set():
            raise BrokerRefused("MQTT bridge stopped while resolving the broker")
    if lookup.error is not None:
        _log.warning("Cannot resolve MQTT broker %r: %s. Will retry.", name, lookup.error)
        raise BrokerRefused(f"cannot resolve MQTT broker {name!r}")
    return lookup.addresses


def _resolve(key: tuple[str, int], lookup: _Lookup) -> None:
    name, port = key
    try:
        infos = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
        lookup.addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    except Exception as exc:  # noqa: BLE001 - gaierror, or UnicodeError for a long label
        lookup.error = exc
    finally:
        with _lookups_lock:
            del _lookups[key]  # the next attempt asks DNS afresh
        lookup.done.set()


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
            "min_temp": MIN_TEMP,
            "max_temp": MAX_TEMP,
            "temp_step": TEMP_STEP,
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
            "mode": "off" if (setpoint is None or setpoint <= _OFF_TEMP) else "heat",
        }
        self._publish(self._state_topic(str(zone)), json.dumps(payload), retain=True)

    def publish_availability(self, online: bool) -> None:
        self._publish(self.availability_topic(), "online" if online else "offline", retain=True)

    # ---- inbound: commands ----------------------------------------------

    def on_message(self, topic: str, payload: str) -> None:
        """Route an inbound command topic to the hub client. Everything flows
        back through the app's serialised client, never straight to the hub."""
        # <base>/<zone>/<field>/set, where <base> may itself contain slashes.
        prefix = self._base + "/"
        if not topic.startswith(prefix):
            return
        parts = topic[len(prefix):].split("/")
        if len(parts) != 3 or parts[2] != "set":
            return
        zone, field = parts[0], parts[1]
        if field == "temperature":
            try:
                value = float(payload)
            except (TypeError, ValueError):
                return
            self._handler.set_temperature(zone, value)
        elif field == "mode":
            mode = payload.strip().lower()
            if mode == "off":
                self._handler.set_temperature(zone, 0.0)
            elif mode == "heat":
                # Back on to the zone's last target above off; the client
                # checks the zone is off and knows the target.
                self._handler.turn_on(zone)

    def handle_message(self, topic: str, payload: str, retain: bool) -> None:
        """Entry point for broker messages. A retained command is stale (the
        broker replays it on every reconnect) and must not override later
        changes, so it is dropped."""
        if retain:
            _log.info("Ignoring retained MQTT command on %s", topic)
            return
        self.on_message(topic, payload)

    # ---- network (lazy paho; tests fake it) ------------------------------

    def connect(self, host: str, port: int, username: str | None, password: str | None):
        """Connect to the broker and start the network loop. Returns the client.

        Nothing is resolved here (this runs on a request thread). Before every
        connect and reconnect, paho's network thread calls ``on_pre_connect``,
        which resolves and vets ``host`` and points the client at the vetted IP,
        so the socket that carries the credentials only ever opens to a LAN
        address."""
        import paho.mqtt.client as mqtt  # imported here so paho is optional

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id(self._node)
        )
        client.reconnect_delay_set(_RECONNECT_MIN_DELAY, _RECONNECT_MAX_DELAY)
        # paho re-raises an on_pre_connect error only while this is False (its
        # default). Pinned so a refusal always aborts the connect.
        client.suppress_exceptions = False
        if username:
            client.username_pw_set(username, password or "")
        client.will_set(self.availability_topic(), "offline", retain=True)

        def _on_connect(cli, _userdata, _flags, reason_code, _properties):
            # paho calls this for a refused CONNACK too (bad password, ...).
            if reason_code.is_failure:
                _log.warning("MQTT broker %r refused the connection: %s. Will retry.",
                             host, reason_code)
                return
            cli.subscribe(self.command_topic_filter())
            self.publish_availability(True)
            self.republish_discovery()  # HA must re-learn the zones on every connect

        def _on_message(_cli, _userdata, msg):
            try:
                self.handle_message(
                    msg.topic,
                    msg.payload.decode("utf-8", errors="replace"),
                    bool(msg.retain),
                )
            except Exception:  # noqa: BLE001 - a bad command must not kill the loop
                _log.exception("MQTT command handling failed for %s", msg.topic)

        stopping = threading.Event()

        def _on_pre_connect(cli, _userdata):
            if stopping.is_set():
                raise BrokerRefused("MQTT bridge stopped")
            # The public ``host`` setter refuses mid-connect; paho opens the
            # socket to ``_host`` right after this returns.
            cli._host = vetted_broker_address(host, port, stopping)

        stop_lock = threading.Lock()  # no loop_start once ``stopping`` is set

        def _supervise():
            # paho also reconnects from inside its packet handler (CONNACK
            # asking for MQTT 3.1), outside its retry loop; a refusal or TCP
            # error there ends paho's thread. loop_start() is a no-op while
            # that thread is alive and starts a fresh one once it has died.
            while not stopping.wait(_SUPERVISE_EVERY):
                with stop_lock:
                    if stopping.is_set():
                        return
                    if client.loop_start() == 0:  # MQTT_ERR_SUCCESS: it had died
                        _log.warning("MQTT network thread for %r stopped; restarted it", host)

        def _stop():
            with stop_lock:
                stopping.set()  # a lookup in progress gives up at once

        supervisor = threading.Thread(target=_supervise, name="mqtt-supervisor", daemon=True)
        paho_disconnect = client.disconnect
        paho_loop_stop = client.loop_stop

        def _disconnect(*args, **kwargs):
            # Ends this client for good (a restart builds a new one): without
            # the stop, the supervisor would see paho's thread end and restart it.
            _stop()
            if client.is_connected():
                # A clean DISCONNECT suppresses the will, so say "offline"
                # ourselves (retained, like the will), waiting a moment at most.
                try:
                    client.publish(self.availability_topic(), "offline", retain=True) \
                        .wait_for_publish(timeout=_OFFLINE_WAIT)
                except (RuntimeError, ValueError):  # not sent; disconnect anyway
                    _log.warning("Could not publish MQTT offline status before disconnecting")
            return paho_disconnect(*args, **kwargs)

        def _loop_stop():
            _stop()
            if supervisor.is_alive() and supervisor is not threading.current_thread():
                supervisor.join()
            return paho_loop_stop()

        client.on_connect = _on_connect
        client.on_message = _on_message
        client.on_pre_connect = _on_pre_connect
        client.loop_stop = _loop_stop
        client.disconnect = _disconnect
        # Rebind publish to the real client now that we have one.
        self._publish = lambda topic, payload, retain=False: client.publish(
            topic, payload, retain=retain
        )
        # connect_async + loop_start: the lookup, TCP connect and retries happen
        # on paho's daemon network thread, so a dead broker never blocks a request.
        client.connect_async(host, port)
        if getattr(client, "_host", None) != host:
            # A paho that keeps the host elsewhere would ignore the vetted IP
            # and connect to the raw name: refuse to start instead.
            raise RuntimeError("paho-mqtt no longer keeps the broker in Client._host")
        client.loop_start()
        supervisor.start()
        _log.info("MQTT bridge connecting to %s:%s", host, port)
        return client
