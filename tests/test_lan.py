import pytest

from hearthmage.lan import is_lan_broker_address, is_lan_broker_host, is_lan_ipv4


@pytest.mark.parametrize(
    "ip", ["10.0.0.1", "10.255.255.254", "172.16.0.1", "172.31.255.254", "192.168.1.20", " 10.0.0.20 "]
)
def test_rfc1918_ipv4_is_lan(ip):
    assert is_lan_ipv4(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8", "172.15.255.255", "172.32.0.1", "192.169.0.1",
        "100.64.0.1", "100.127.255.254",  # Tailscale / CGNAT is not the home LAN
        "127.0.0.1", "169.254.1.1", "0.0.0.0", "255.255.255.255", "192.0.2.1",
        "010.0.0.1", "167772161", "0x0a000001", "10.1",  # shorthand forms inet_aton takes
        "fd00::1", "::ffff:10.0.0.1", "::1",
        "hub.local", "hub", "", "   ", "10.0.0.5:6653", None, 167772161,
    ],
)
def test_anything_else_is_not_a_lan_ipv4(ip):
    assert is_lan_ipv4(ip) is False


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.5", "172.20.1.1", "192.168.1.2",
        "127.0.0.1", "127.0.1.1", "::1", "localhost",  # a broker on the same box
        "broker", "mosquitto", "homeassistant", "mqtt.local", "ha.lan", "nas.home.arpa",
        "mqtt.internal", "MQTT.Local", " mqtt.local ", "core-mosquitto", "nas.lan.",
    ],
)
def test_lan_broker_hosts_are_accepted(host):
    assert is_lan_broker_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8", "100.64.0.1", "169.254.0.5", "0.0.0.0",
        "2001:db8::1", "2a00:1450:4001::1", "fd00::1", "fe80::1", "::ffff:8.8.8.8", "[::1]",
        "mqtt.example.com", "evil.com.", "local.evil.com", "example.local.com", "8.8.8.8.",
        # A single label with a trailing dot is an absolute name: "ai." is a TLD apex.
        "ai.", "broker.", "localhost.",
        # Single labels the resolver reads as an IPv4 address (134744072 = 8.8.8.8).
        "134744072", "0x8080808", "0X08080808", "010",
        "", "  ", "a b", "mqtt.local/x", "user@mqtt.local", "10.0.0.5:1883", "mqtt..local",
        None, 42,
    ],
)
def test_other_broker_hosts_are_refused(host):
    assert is_lan_broker_host(host) is False


@pytest.mark.parametrize("addr", ["10.0.0.5", "172.20.1.1", "192.168.1.2", "127.0.0.1", "127.0.1.1", "::1"])
def test_lan_broker_addresses_are_accepted(addr):
    assert is_lan_broker_address(addr) is True


@pytest.mark.parametrize(
    "addr",
    [
        "8.8.8.8", "100.64.0.1", "169.254.0.5", "0.0.0.0", "192.0.2.1",
        # IPv6: only ::1 is the local box; ULA, link-local and mapped forms are not.
        "fd00::1", "fe80::1", "fe80::1%en0", "2a00:1450:4001::1", "::ffff:192.168.1.5",
        "::ffff:127.0.0.1", "::",
        # A resolved address is a literal; a name never counts.
        "localhost", "mqtt.local", "10.1", "167772161", "", None,
    ],
)
def test_other_broker_addresses_are_refused(addr):
    assert is_lan_broker_address(addr) is False


@pytest.mark.parametrize("value", ["10.0.0.5", " 8.8.8.8 ", "::1", "fe80::1%en0"])
def test_ip_literals_are_recognised(value):
    from hearthmage.lan import is_ip_literal

    assert is_ip_literal(value) is True


@pytest.mark.parametrize("value", ["mqtt.local", "10.1", "167772161", "", None, 42])
def test_names_are_not_ip_literals(value):
    from hearthmage.lan import is_ip_literal

    assert is_ip_literal(value) is False
