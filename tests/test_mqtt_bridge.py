import json

import pytest

from hearthmage.mqtt_bridge import MqttBridge


class _Handler:
    def __init__(self):
        self.calls = []

    def set_temperature(self, zone_id, temperature):
        self.calls.append((zone_id, temperature))

    def turn_on(self, zone_id):
        self.calls.append((zone_id, "on"))


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


def test_mode_heat_command_turns_the_zone_back_on():
    # heatapp-local-9cg: a zone switched off from Home Assistant must be able
    # to come back on; the client picks the target (its last one above off).
    handler = _Handler()
    bridge, _ = _bridge(handler)
    bridge.on_message("hearthmage/3/mode/set", "heat")
    bridge.on_message("hearthmage/4/mode/set", " HEAT ")
    bridge.on_message("hearthmage/5/mode/set", "cool")  # not a mode we offer
    assert handler.calls == [("3", "on"), ("4", "on")]


def test_retained_commands_are_ignored():
    handler = _Handler()
    bridge, _ = _bridge(handler)
    bridge.handle_message("hearthmage/1/temperature/set", "25", retain=True)
    bridge.handle_message("hearthmage/1/mode/set", "off", retain=True)
    assert handler.calls == []


def test_live_commands_still_apply_through_handle_message():
    handler = _Handler()
    bridge, _ = _bridge(handler)
    bridge.handle_message("hearthmage/1/temperature/set", "25", retain=False)
    assert handler.calls == [("1", 25.0)]


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


def test_publish_state_off_when_setpoint_is_the_off_temperature():
    bridge, published = _bridge()
    bridge.publish_state("2", current=18.0, setpoint=1.0)  # Off is sent as 1 degree
    state = json.loads(published[-1][1])
    assert state["mode"] == "off"


def test_commands_work_when_the_base_topic_has_slashes():
    # "home/hearthmage" is an ordinary MQTT layout; state already published
    # under it, but every command was dropped by a fixed segment count.
    class Handler:
        def __init__(self):
            self.calls = []

        def set_temperature(self, zone, value):
            self.calls.append((zone, value))

    handler = Handler()
    bridge = MqttBridge(
        publish=lambda *a, **k: None, handler=handler, base_topic="home/hearthmage", node_id="hub"
    )
    bridge.on_message("home/hearthmage/1/temperature/set", "20.5")
    bridge.on_message("home/hearthmage/3/mode/set", "off")
    # Near misses must still be ignored.
    bridge.on_message("home/hearthmage2/1/temperature/set", "30")
    bridge.on_message("home/1/temperature/set", "30")
    bridge.on_message("home/hearthmage/1/extra/temperature/set", "30")
    assert handler.calls == [("1", 20.5), ("3", 0.0)]


class _FakeMessageInfo:
    def __init__(self, client):
        self._client = client

    def wait_for_publish(self, timeout=None):
        self._client.events.append(("wait", timeout))
        if self._client.publish_wait_error:
            raise self._client.publish_wait_error


class _FakePahoClient:
    """Just enough of paho 2.x: connect_async stores the broker in _host, and
    reconnect() runs on_pre_connect and then "opens a socket" to _host, the way
    paho's Client.reconnect does before every connect and reconnect."""

    def __init__(self, *args, **kwargs):
        self.client_id = kwargs.get("client_id")
        self._host = ""
        self._port = 1883
        self.on_pre_connect = None
        self.suppress_exceptions = True  # paho's default is False; connect() must pin it
        self.opened = []  # (host, port) of every socket "opened"
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.connected = False
        self.events = []  # publish/wait/disconnect, in order
        self.publish_wait_error = None
        self.published = []
        self.subscribed = []

    def username_pw_set(self, *a):
        pass

    def will_set(self, *a, **k):
        pass

    def connect_async(self, host, port=1883):
        self._host = host
        self._port = port

    def loop_start(self):
        self.loop_started = True

    def reconnect_delay_set(self, *a):
        pass

    def loop_stop(self):
        self.loop_stopped = True

    def publish(self, topic, payload=None, retain=False, **k):
        self.published.append((topic, payload, retain))
        self.events.append(("publish", topic, payload))
        return _FakeMessageInfo(self)

    def is_connected(self):
        return self.connected

    def subscribe(self, topic, *a, **k):
        self.subscribed.append(topic)

    def disconnect(self):
        self.disconnected = True
        self.events.append(("disconnect",))

    def reconnect(self):
        if self.on_pre_connect:
            self.on_pre_connect(self, None)  # paho re-raises (suppress_exceptions False)
        self.opened.append((self._host, self._port))


@pytest.fixture
def fake_paho(monkeypatch):
    import sys
    import types

    mqtt = types.ModuleType("paho.mqtt.client")
    mqtt.Client = _FakePahoClient
    mqtt.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho.mqtt = paho_mqtt
    paho_mqtt.client = mqtt
    monkeypatch.setitem(sys.modules, "paho", paho)
    monkeypatch.setitem(sys.modules, "paho.mqtt", paho_mqtt)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", mqtt)
    created = []

    class Recorded(_FakePahoClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    mqtt.Client = Recorded
    yield mqtt
    for client in created:
        client.loop_stop()  # ends the bridge's supervisor thread


@pytest.fixture
def dns(monkeypatch):
    """Stub socket.getaddrinfo. ``dns.answers`` maps a name to its addresses (or
    an exception to raise); an unknown name fails the test. Never real DNS."""
    import socket

    class Dns:
        def __init__(self):
            self.answers = {}
            self.asked = []

    stub = Dns()

    def getaddrinfo(host, port, *args, **kwargs):
        stub.asked.append((host, port))
        answer = stub.answers[host]
        if isinstance(answer, BaseException):
            raise answer
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "",
             (a, port, 0, 0) if ":" in a else (a, port))
            for a in answer
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return stub


def test_connect_installs_a_callback_that_drops_retained_messages(fake_paho):
    import types

    handler = _Handler()
    bridge, _ = _bridge(handler)
    client = bridge.connect("broker", 1883, None, None)

    def msg(retain):
        return types.SimpleNamespace(
            topic="hearthmage/1/temperature/set", payload=b"25", retain=retain
        )

    client.on_message(client, None, msg(retain=True))
    assert handler.calls == []
    client.on_message(client, None, msg(retain=False))
    assert handler.calls == [("1", 25.0)]


def test_connect_does_not_resolve_on_the_calling_thread(fake_paho, dns):
    # connect() runs on a request thread under the app's locks: no DNS there.
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, None, None)
    assert dns.asked == []
    assert client.loop_started is True
    assert client.suppress_exceptions is False  # a refusal must abort paho's connect


def test_a_lan_name_connects_to_the_address_it_resolves_to(fake_paho, dns):
    dns.answers["mqtt.local"] = ["192.168.1.10"]
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1884, "user", "secret")
    client.reconnect()
    assert dns.asked == [("mqtt.local", 1884)]
    assert client.opened == [("192.168.1.10", 1884)]


def test_every_reconnect_resolves_again_and_follows_a_new_address(fake_paho, dns):
    dns.answers["mqtt.local"] = ["192.168.1.10"]
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, None, None)
    client.reconnect()
    dns.answers["mqtt.local"] = ["192.168.1.20"]  # DHCP moved the broker
    client.reconnect()
    assert client.opened == [("192.168.1.10", 1883), ("192.168.1.20", 1883)]
    assert len(dns.asked) == 2


@pytest.mark.parametrize(
    "addresses",
    [
        ["8.8.8.8"],
        ["192.168.1.5", "8.8.8.8"],  # mixed answers fail closed
        ["192.168.1.5", "2a00:1450:4001::1"],  # a public AAAA beside a private A
        ["fd00::1"],  # IPv6: only ::1 is acceptable
        ["::ffff:192.168.1.5"],
        ["100.64.0.1"],
        ["192.168.1.5", "fe80::1%en0"],
        [],
    ],
)
def test_a_name_resolving_off_the_lan_is_refused(fake_paho, dns, caplog, addresses):
    import logging

    dns.answers["mqtt.lan"] = addresses
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.lan", 1883, "user", "secret")
    with caplog.at_level(logging.WARNING, logger="hearthmage.mqtt"):
        with pytest.raises(OSError):  # an OSError: paho backs off and retries
            client.reconnect()
    assert client.opened == []  # no socket, so the credentials never leave
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert repr("mqtt.lan") in warnings[0].getMessage()
    assert repr(addresses) in warnings[0].getMessage()


def test_a_refused_name_is_retried_and_connects_once_it_is_on_the_lan(fake_paho, dns):
    dns.answers["mqtt.lan"] = ["8.8.8.8"]
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.lan", 1883, None, None)
    with pytest.raises(OSError):
        client.reconnect()
    dns.answers["mqtt.lan"] = ["10.0.0.7"]
    client.reconnect()
    assert client.opened == [("10.0.0.7", 1883)]


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["::1"], ["::1", "127.0.0.1"]])
def test_a_name_resolving_to_loopback_is_accepted(fake_paho, dns, addresses):
    dns.answers["localhost"] = addresses
    bridge, _ = _bridge()
    client = bridge.connect("localhost", 1883, None, None)
    client.reconnect()
    assert client.opened == [(addresses[0], 1883)]


@pytest.mark.parametrize(
    "error",
    [
        __import__("socket").gaierror(8, "nodename nor servname provided"),
        UnicodeError("label too long"),  # the idna codec on a 64+ char label
    ],
)
def test_a_failed_lookup_is_a_retryable_refusal(fake_paho, dns, caplog, error):
    import logging

    dns.answers["mqtt.local"] = error
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, None, None)
    with caplog.at_level(logging.WARNING, logger="hearthmage.mqtt"):
        with pytest.raises(OSError):  # never another type: that would end paho's loop
            client.reconnect()
    assert client.opened == []
    assert any(repr("mqtt.local") in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("host", ["192.168.1.30", "127.0.0.1", "::1"])
def test_an_ip_literal_skips_the_lookup(fake_paho, dns, host):
    bridge, _ = _bridge()
    client = bridge.connect(host, 1883, None, None)
    client.reconnect()
    assert dns.asked == []
    assert client.opened == [(host, 1883)]


@pytest.mark.parametrize("host", ["8.8.8.8", "100.64.0.1", "fd00::1", "2a00:1450:4001::1"])
def test_an_ip_literal_off_the_lan_is_still_refused(fake_paho, dns, caplog, host):
    import logging

    bridge, _ = _bridge()
    client = bridge.connect(host, 1883, "user", "secret")
    with caplog.at_level(logging.WARNING, logger="hearthmage.mqtt"):
        with pytest.raises(OSError):
            client.reconnect()
    assert dns.asked == []
    assert client.opened == []
    assert any(repr(host) in r.getMessage() for r in caplog.records)


def test_loop_stop_ends_a_slow_lookup_promptly(fake_paho, monkeypatch):
    import socket
    import threading
    import time

    release = threading.Event()

    def slow_getaddrinfo(*args, **kwargs):
        release.wait(5)  # a DNS server that does not answer
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 1883))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, None, None)
    outcome = []

    def network_thread():  # stands in for paho's loop thread
        try:
            client.reconnect()
            outcome.append("connected")
        except OSError:
            outcome.append("refused")

    worker = threading.Thread(target=network_thread)
    worker.start()
    time.sleep(0.05)
    started = time.monotonic()
    client.loop_stop()
    worker.join(2)
    assert not worker.is_alive()
    assert time.monotonic() - started < 1.0
    assert outcome == ["refused"]
    assert client.opened == []  # a stopped bridge never connects
    assert client.loop_stopped is True  # paho's own loop_stop still ran
    release.set()  # the DNS answer arrives late; the helper thread then ends
    for t in threading.enumerate():
        if t.name == "mqtt-resolve":
            t.join(2)
            assert not t.is_alive()


def test_a_stopped_bridge_does_not_reconnect(fake_paho, dns):
    dns.answers["mqtt.local"] = ["192.168.1.10"]
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, None, None)
    client.loop_stop()
    with pytest.raises(OSError):
        client.reconnect()
    assert client.opened == []


def test_connect_refuses_a_paho_that_no_longer_keeps_the_host_in_host(fake_paho, dns):
    # The vetted address is written to Client._host. If a paho upgrade moves it,
    # writing _host would do nothing and paho would connect to the raw name.
    class MovedHost(fake_paho.Client):
        def connect_async(self, host, port=1883):
            self._broker = host

    fake_paho.Client = MovedHost
    bridge, _ = _bridge()
    with pytest.raises(RuntimeError):
        bridge.connect("mqtt.local", 1883, None, None)


@pytest.fixture
def real_paho(monkeypatch):
    """The installed paho, with the network thread and socket creation stubbed.
    Catches a paho upgrade that renames Client._host or stops calling
    on_pre_connect before it opens the socket."""
    import socket

    mqtt = pytest.importorskip("paho.mqtt.client")
    monkeypatch.setattr(mqtt.Client, "loop_start", lambda self: None)  # reconnect() runs here
    opened = []

    def create_connection(address, *args, **kwargs):
        opened.append(address)
        raise ConnectionRefusedError("stub: no socket in tests")

    monkeypatch.setattr(socket, "create_connection", create_connection)
    return opened


def test_real_paho_opens_the_socket_to_the_vetted_address(real_paho, dns):
    dns.answers["mqtt.local"] = ["192.168.1.10"]
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, "user", "secret")
    with pytest.raises(OSError):  # the stubbed socket refuses; paho would retry
        client.reconnect()
    dns.answers["mqtt.local"] = ["192.168.1.20"]
    with pytest.raises(OSError):
        client.reconnect()
    assert real_paho == [("192.168.1.10", 1883), ("192.168.1.20", 1883)]
    client.loop_stop()


def test_real_paho_opens_nothing_for_a_name_off_the_lan(real_paho, dns):
    dns.answers["mqtt.lan"] = ["192.168.1.5", "8.8.8.8"]
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.lan", 1883, "user", "secret")
    with pytest.raises(OSError):
        client.reconnect()
    assert dns.asked == [("mqtt.lan", 1883)]  # the hook ran
    assert real_paho == []
    client.loop_stop()



@pytest.mark.parametrize(
    "node_id, prefix",
    [
        ("hub1", "hmhub1"),
        ("home/hub-1_x", "hmhomehub1x"),  # only [0-9A-Za-z] survive
        ("a-very-long-node-identifier", "hmaverylongnode"),  # cut so the suffix fits
        ("düsseldorf", "hmdsseldorf"),  # non-ASCII letters are dropped too
        ("---", "hm"),
    ],
)
def test_the_client_id_is_spec_safe_and_names_the_node(fake_paho, node_id, prefix):
    # Never empty: paho answers a rejected empty id by re-sending CONNECT from
    # inside its packet handler. MQTT 3.1.1 brokers must accept 1-23 chars of
    # [0-9A-Za-z]; anything else may be refused with rc=2.
    import re

    bridge = MqttBridge(publish=lambda *a, **k: None, handler=_Handler(), node_id=node_id)
    first = bridge.connect("10.0.0.5", 1883, None, None)
    second = bridge.connect("10.0.0.5", 1883, None, None)
    assert re.fullmatch(r"[0-9A-Za-z]{1,23}", first.client_id)
    assert first.client_id.startswith(prefix)
    assert re.fullmatch(r"[0-9a-f]{8}", first.client_id[len(prefix):])
    assert second.client_id == first.client_id  # stable for this process


def test_concurrent_lookups_of_one_name_share_one_dns_query(fake_paho, monkeypatch):
    # Each restart while DNS hangs used to leave one more resolver thread behind.
    import socket
    import threading
    import time

    release = threading.Event()
    asked = []

    def slow_getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]

    from hearthmage import mqtt_bridge

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    monkeypatch.setattr(mqtt_bridge, "_LOOKUP_POLL", 0.01)
    bridge, _ = _bridge()
    workers = []
    for _ in range(20):
        client = bridge.connect("mqtt.local", 1883, None, None)
        worker = threading.Thread(target=lambda c=client: pytest.raises(OSError, c.reconnect))
        worker.start()
        workers.append((client, worker))
        time.sleep(0.01)
        client.loop_stop()
        worker.join(2)
    assert len(asked) == 1
    assert sum(t.name == "mqtt-resolve" for t in threading.enumerate()) == 1
    release.set()
    for t in threading.enumerate():
        if t.name == "mqtt-resolve":
            t.join(2)
    # The next attempt asks DNS again, so an address change is still followed.
    client = bridge.connect("mqtt.local", 1883, None, None)
    client.reconnect()
    assert len(asked) == 2


def test_lookups_of_many_names_are_capped(fake_paho, monkeypatch):
    import socket
    import threading

    from hearthmage import mqtt_bridge

    release = threading.Event()
    asked = []

    def slow_getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        release.wait(5)
        raise socket.gaierror(8, "stub")

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    monkeypatch.setattr(mqtt_bridge, "_LOOKUP_POLL", 0.01)
    bridge, _ = _bridge()
    stopped = threading.Event()
    stopped.set()  # each attempt gives up at once; its lookup stays in flight
    names = [f"broker{i}.local" for i in range(mqtt_bridge._MAX_LOOKUPS + 5)]
    for name in names:
        with pytest.raises(OSError):
            mqtt_bridge._lookup(name, 1883, stopped)
    release.set()
    for t in threading.enumerate():
        if t.name == "mqtt-resolve":
            t.join(2)
    assert len(asked) == mqtt_bridge._MAX_LOOKUPS


@pytest.fixture
def paho_broker(monkeypatch):
    """Real paho on its real network thread, against a scripted broker on a
    socketpair. ``replies`` is the CONNACK return code for each connection in
    turn (the last one repeats). No TCP: create_connection and paho's wake-up
    socketpair are both stubbed."""
    import socket
    import threading
    import types

    mqtt = pytest.importorskip("paho.mqtt.client")
    from hearthmage import mqtt_bridge

    monkeypatch.setattr(mqtt, "_socketpair_compat", socket.socketpair)
    monkeypatch.setattr(mqtt_bridge, "_RECONNECT_MIN_DELAY", 0.05)
    monkeypatch.setattr(mqtt_bridge, "_RECONNECT_MAX_DELAY", 0.1)
    monkeypatch.setattr(mqtt_bridge, "_SUPERVISE_EVERY", 0.05)
    state = types.SimpleNamespace(
        replies=[0], opened=[], connects=[], after=[], deaths=[], sockets=[]
    )

    def create_connection(address, *args, **kwargs):
        rc = state.replies[min(len(state.opened), len(state.replies) - 1)]
        state.opened.append(address)
        ours, theirs = socket.socketpair()
        state.sockets += [ours, theirs]

        def broker():
            try:
                state.connects.append(theirs.recv(4096))
                theirs.sendall(bytes([0x20, 0x02, 0x00, rc]))
                while chunk := theirs.recv(4096):  # until the client closes it
                    state.after.append(chunk)
            except OSError:
                pass

        threading.Thread(target=broker, daemon=True).start()
        return ours

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(
        threading, "excepthook",
        lambda args: state.deaths.append((args.thread.name, args.exc_type.__name__)),
    )
    yield state
    for sock in state.sockets:
        sock.close()


def _wait_for(condition, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _bridge_threads():
    import threading

    return [t.name for t in threading.enumerate()
            if t.name.startswith(("paho-mqtt", "mqtt-supervisor", "mqtt-resolve"))]


def test_real_paho_recovers_from_identifier_rejected_and_a_refused_retry(paho_broker, dns):
    import socket

    paho_broker.replies = [2, 0]  # CONNACK rc=2 identifier rejected, then accepted
    answers = iter([["192.168.1.10"], socket.gaierror(8, "stub: DNS down"), ["192.168.1.11"]])
    dns.answers = type("Answers", (), {"__getitem__": lambda self, name: next(answers, ["192.168.1.11"])})()
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, "user", "secret")
    try:
        assert _wait_for(client.is_connected)
        assert paho_broker.opened[0] == ("192.168.1.10", 1883)
        assert ("192.168.1.11", 1883) in paho_broker.opened
        assert b"hmhub1" in paho_broker.connects[0]  # never an empty id
        assert paho_broker.deaths == []
    finally:
        client.loop_stop()
        client.disconnect()
    assert _wait_for(lambda: _bridge_threads() == [], timeout=2)


def test_real_paho_thread_is_restarted_after_a_refusal_inside_connack(paho_broker, dns):
    # CONNACK rc=1 makes paho downgrade to MQTT 3.1 and call reconnect() from
    # its packet handler, where a refusal escapes and ends paho's thread.
    import socket

    paho_broker.replies = [1, 0]
    answers = iter([["192.168.1.10"], socket.gaierror(8, "stub: DNS down"), ["192.168.1.11"]])
    dns.answers = type("Answers", (), {"__getitem__": lambda self, name: next(answers, ["192.168.1.11"])})()
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, "user", "secret")
    try:
        assert _wait_for(client.is_connected)
        assert [name for name, _ in paho_broker.deaths] and \
            {exc for _, exc in paho_broker.deaths} == {"BrokerRefused"}  # the path was hit
        assert ("192.168.1.11", 1883) in paho_broker.opened
    finally:
        client.loop_stop()
        client.disconnect()
    assert _wait_for(lambda: _bridge_threads() == [], timeout=2)


def test_real_paho_stop_is_prompt_and_leaves_no_threads(paho_broker, dns):
    import time

    dns.answers["mqtt.local"] = ["8.8.8.8"]  # refused on every attempt
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, None, None)
    time.sleep(0.3)
    started = time.monotonic()
    client.loop_stop()
    client.disconnect()
    assert time.monotonic() - started < 1.0
    assert paho_broker.opened == []
    assert _wait_for(lambda: _bridge_threads() == [], timeout=2)



def test_a_refused_connack_subscribes_to_nothing_and_stays_offline(fake_paho, caplog):
    import logging

    bridge, _ = _bridge()
    bridge.publish_discovery([("1", "Living Room")])
    client = bridge.connect("10.0.0.5", 1883, "user", "secret")
    refused = type("Reason", (), {"is_failure": True, "__str__": lambda self: "Not authorized"})()
    with caplog.at_level(logging.WARNING, logger="hearthmage.mqtt"):
        client.on_connect(client, None, {}, refused, None)
    assert client.subscribed == []
    assert client.published == []  # no "online", no discovery
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Not authorized" in warning and "10.0.0.5" in warning
    assert "user" not in warning and "secret" not in warning


def test_an_accepted_connack_subscribes_and_goes_online(fake_paho):
    bridge, _ = _bridge()
    bridge.publish_discovery([("1", "Living Room")])
    client = bridge.connect("10.0.0.5", 1883, None, None)
    accepted = type("Reason", (), {"is_failure": False})()
    client.on_connect(client, None, {}, accepted, None)
    assert client.subscribed == ["hearthmage/+/+/set"]
    assert ("hearthmage/status", "online", True) in client.published
    assert any(t.startswith("homeassistant/climate/") for t, _, _ in client.published)


def test_real_paho_not_authorized_sends_no_subscribe_or_online(paho_broker, dns, caplog):
    import logging

    paho_broker.replies = [5]  # CONNACK rc=5 not authorized, every time
    bridge, _ = _bridge()
    with caplog.at_level(logging.WARNING, logger="hearthmage.mqtt"):
        client = bridge.connect("192.168.1.10", 1883, "user", "secret")
        try:
            assert _wait_for(lambda: any("Not authorized" in r.getMessage() for r in caplog.records))
        finally:
            client.disconnect()
            client.loop_stop()
    sent = b"".join(paho_broker.after)
    assert b"hearthmage/+/+/set" not in sent  # no SUBSCRIBE
    assert b"online" not in sent


def test_real_paho_disconnect_then_loop_stop_is_prompt_when_connected(paho_broker, dns):
    import time

    bridge, _ = _bridge()
    client = bridge.connect("192.168.1.10", 1883, None, None)
    assert _wait_for(client.is_connected)
    started = time.monotonic()
    client.disconnect()
    client.loop_stop()
    assert time.monotonic() - started < 0.5
    # A clean DISCONNECT reached the broker (its reader thread may lag a moment).
    assert _wait_for(lambda: b"\xe0\x00" in b"".join(paho_broker.after), timeout=2)
    assert _wait_for(lambda: _bridge_threads() == [], timeout=2)


def test_disconnect_while_a_lookup_hangs_is_prompt_and_restarts_nothing(fake_paho, monkeypatch):
    # disconnect() comes first now; the supervisor must not see paho's thread
    # end and start a new one, and a lookup in progress must give up.
    import socket
    import threading
    import time

    release = threading.Event()

    def slow_getaddrinfo(*args, **kwargs):
        release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 1883))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    bridge, _ = _bridge()
    client = bridge.connect("mqtt.local", 1883, None, None)
    outcome = []

    def network_thread():
        try:
            client.reconnect()
            outcome.append("connected")
        except OSError:
            outcome.append("refused")

    worker = threading.Thread(target=network_thread)
    worker.start()
    time.sleep(0.05)
    started = time.monotonic()
    client.disconnect()
    worker.join(2)
    assert time.monotonic() - started < 1.0
    assert outcome == ["refused"] and client.opened == []
    assert client.disconnected is True  # paho's own disconnect still ran
    with pytest.raises(OSError):
        client.reconnect()  # a disconnected bridge client stays down
    client.loop_stop()
    release.set()



def test_a_deliberate_stop_publishes_offline_before_disconnecting(fake_paho):
    bridge, _ = _bridge()
    client = bridge.connect("10.0.0.5", 1883, None, None)
    client.connected = True
    client.disconnect()
    client.loop_stop()
    publish, wait, disconnect = client.events
    assert publish == ("publish", "hearthmage/status", "offline")
    assert ("hearthmage/status", "offline", True) in client.published  # retained, like the will
    assert wait[0] == "wait" and 0 < wait[1] <= 1.0  # bounded: never blocks a restart long
    assert disconnect == ("disconnect",)


def test_a_stop_while_not_connected_publishes_nothing(fake_paho):
    bridge, _ = _bridge()
    client = bridge.connect("10.0.0.5", 1883, None, None)
    client.disconnect()
    assert client.events == [("disconnect",)]


@pytest.mark.parametrize("error", [RuntimeError("publish failed"), ValueError("queue full")])
def test_a_failed_offline_publish_still_disconnects(fake_paho, error):
    bridge, _ = _bridge()
    client = bridge.connect("10.0.0.5", 1883, None, None)
    client.connected = True
    client.publish_wait_error = error
    client.disconnect()
    assert client.events[-1] == ("disconnect",)


def test_real_paho_broker_gets_offline_before_disconnect(paho_broker, dns):
    bridge, _ = _bridge()
    client = bridge.connect("192.168.1.10", 1883, None, None)
    assert _wait_for(client.is_connected)
    client.disconnect()
    client.loop_stop()
    assert _wait_for(lambda: b"\xe0\x00" in b"".join(paho_broker.after), timeout=2)
    sent = b"".join(paho_broker.after)
    offline = bytes([0x31, 2 + len("hearthmage/status") + len("offline"), 0x00,
                     len("hearthmage/status")]) + b"hearthmage/status" + b"offline"
    assert offline in sent  # PUBLISH, qos 0, retain
    assert sent.index(offline) < sent.index(b"\xe0\x00")  # before DISCONNECT
