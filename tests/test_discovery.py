from hearthmage.discovery import discover_hubs, parse_ops1_idu, subnet_hosts


def test_parse_ops1_idu_matches_sticker():
    # Real reply from the hub (IDU 15792 on the sticker).
    assert parse_ops1_idu("OPOK,OPS1,24,192,0,0,0,38,40,176,61,0") == 15792


def test_parse_ops1_idu_rejects_non_ops1():
    assert parse_ops1_idu("OPOK,OPS2,1,2,3,4") is None
    assert parse_ops1_idu("ER,1") is None
    assert parse_ops1_idu("OPOK,OPS1,24") is None  # too short


def test_subnet_hosts_covers_the_24():
    hosts = subnet_hosts("10.0.0.38")
    assert hosts[0] == "10.0.0.1"
    assert hosts[-1] == "10.0.0.254"
    assert "10.0.0.38" not in hosts  # skip our own address
    assert len(hosts) == 253


def test_subnet_hosts_invalid_ip_returns_empty():
    assert subnet_hosts(None) == []
    assert subnet_hosts("not-an-ip") == []


def test_discover_hubs_finds_the_responder():
    def fake_probe(ip):
        return "OPOK,OPS1,24,192,0,0,0,38,40,176,61,0" if ip == "10.0.0.20" else None

    found = discover_hubs(["10.0.0.19", "10.0.0.20", "10.0.0.21"], probe=fake_probe)
    assert found == [{"ip": "10.0.0.20", "idu": 15792}]


def test_discover_hubs_ignores_non_nexho_replies():
    def fake_probe(ip):
        return "some other udp service" if ip == "10.0.0.5" else None

    assert discover_hubs(["10.0.0.5", "10.0.0.6"], probe=fake_probe) == []


# --- hardening: private-only sweep, single flight, sender check ---

import socket
import threading
import time

import pytest

import hearthmage.discovery as disc

HUB_REPLY = b"OPOK,OPS1,24,192,0,0,0,38,40,176,61,0"


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",  # public
        "203.0.113.7",  # public (documentation range, still not private-LAN use)
        "100.64.0.5",  # Tailscale / CGNAT: not the home LAN
        "100.127.255.254",  # top of 100.64.0.0/10
        "127.0.0.1",  # loopback
        "169.254.1.2",  # link-local
        "::1",
        "2001:db8::1",
    ],
)
def test_subnet_hosts_refuses_non_lan_addresses(ip):
    assert subnet_hosts(ip) == []


@pytest.mark.parametrize("ip", ["10.1.2.3", "172.16.5.9", "172.31.0.2", "192.168.1.40"])
def test_subnet_hosts_accepts_rfc1918(ip):
    assert len(subnet_hosts(ip)) == 253


def test_discover_local_does_not_probe_a_public_address(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "interface_addresses", lambda: None)  # fallback path
    monkeypatch.setattr(disc, "local_ip", lambda: "8.8.8.8")
    monkeypatch.setattr(disc, "_udp_probe", lambda *a, **k: pytest.fail("probed"))
    assert disc.discover_local() == []


def test_discover_local_does_not_probe_a_tailscale_address(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "interface_addresses", lambda: None)  # fallback path
    monkeypatch.setattr(disc, "local_ip", lambda: "100.101.102.103")
    monkeypatch.setattr(disc, "_udp_probe", lambda *a, **k: pytest.fail("probed"))
    assert disc.discover_local() == []


class _FakeSock:
    """UDP socket stub that delivers queued (data, addr) replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, n):
        if not self.replies:
            raise socket.timeout()
        return self.replies.pop(0)

    def close(self):
        pass


def _patch_socket(monkeypatch, replies):
    fake = _FakeSock(replies)
    monkeypatch.setattr(disc.socket, "socket", lambda *a, **k: fake)
    return fake


def test_udp_probe_accepts_reply_from_probed_ip_and_port(monkeypatch):
    _patch_socket(monkeypatch, [(HUB_REPLY, ("10.0.0.20", 6653))])
    assert disc._udp_probe("10.0.0.20", 6653, 0.1, attempts=1).startswith("OPOK,OPS1")


def test_udp_probe_ignores_reply_from_another_ip(monkeypatch):
    _patch_socket(monkeypatch, [(HUB_REPLY, ("10.0.0.99", 6653))])
    assert disc._udp_probe("10.0.0.20", 6653, 0.1, attempts=1) is None


def test_udp_probe_ignores_reply_from_another_port(monkeypatch):
    _patch_socket(monkeypatch, [(HUB_REPLY, ("10.0.0.20", 4444))])
    assert disc._udp_probe("10.0.0.20", 6653, 0.1, attempts=1) is None


def test_udp_probe_skips_a_spoofed_reply_and_takes_the_real_one(monkeypatch):
    _patch_socket(
        monkeypatch,
        [(b"OPOK,OPS1,fake", ("10.0.0.99", 6653)), (HUB_REPLY, ("10.0.0.20", 6653))],
    )
    assert disc._udp_probe("10.0.0.20", 6653, 0.1, attempts=1) == HUB_REPLY.decode()


def test_concurrent_discover_local_shares_one_sweep(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "lan_addresses", lambda: [("10.0.0.5", "255.255.255.0")])
    calls = []
    gate = threading.Event()

    def fake_sweep(hosts, **kw):
        calls.append(1)
        gate.wait(2)
        return [{"ip": "10.0.0.20", "idu": 1}]

    monkeypatch.setattr(disc, "discover_hubs", fake_sweep)
    results = []
    threads = [threading.Thread(target=lambda: results.append(disc.discover_local())) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)  # let every caller reach the single-flight gate
    gate.set()
    for t in threads:
        t.join(3)
    assert len(calls) == 1
    assert results == [[{"ip": "10.0.0.20", "idu": 1}]] * 5


def test_each_scan_after_the_last_finished_sweeps_again(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "lan_addresses", lambda: [("10.0.0.5", "255.255.255.0")])
    calls = []
    monkeypatch.setattr(disc, "discover_hubs", lambda hosts, **kw: calls.append(1) or [])
    disc.discover_local()
    disc.discover_local()
    assert len(calls) == 2


def test_waiters_receive_the_leaders_exception(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "lan_addresses", lambda: [("10.0.0.5", "255.255.255.0")])
    gate = threading.Event()
    calls = []

    def failing_sweep(hosts, **kw):
        calls.append(1)
        gate.wait(2)
        raise OSError("boom")

    monkeypatch.setattr(disc, "discover_hubs", failing_sweep)
    outcomes = []

    def call():
        try:
            disc.discover_local()
            outcomes.append("ok")
        except OSError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(3)
    assert len(calls) == 1
    assert outcomes == ["boom"] * 4


def test_waiter_gives_up_with_empty_result_if_leader_is_stuck(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "lan_addresses", lambda: [("10.0.0.5", "255.255.255.0")])
    monkeypatch.setattr(disc, "_WAIT_SLACK", 0.0)
    monkeypatch.setattr(disc, "_WIRE_WAIT", 0.0)  # the bound also allows one busy-wire wait
    release = threading.Event()
    monkeypatch.setattr(disc, "discover_hubs", lambda hosts, **kw: release.wait(5) and [])
    leader = threading.Thread(target=lambda: disc.discover_local(timeout=0.01))
    leader.start()
    time.sleep(0.1)
    start = time.monotonic()
    assert disc.discover_local(timeout=0.01) == []  # bound: 4 rounds * 2 * 0.01
    assert time.monotonic() - start < 1
    release.set()
    leader.join(3)


def test_failed_sweep_does_not_wedge_the_next_one(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "lan_addresses", lambda: [("10.0.0.5", "255.255.255.0")])
    outcomes = [OSError("boom"), [{"ip": "10.0.0.20", "idu": 1}]]

    def fake_sweep(hosts, **kw):
        o = outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o

    monkeypatch.setattr(disc, "discover_hubs", fake_sweep)
    with pytest.raises(OSError):
        disc.discover_local()
    assert disc.discover_local() == [{"ip": "10.0.0.20", "idu": 1}]


def test_subnet_hosts_builds_addresses_from_the_canonical_ip():
    # A padded address must not produce " 10.0.0.1", which sendto would hand
    # to the resolver as a name.
    hosts = subnet_hosts(" 10.0.0.5 ")
    assert hosts[0] == "10.0.0.1"
    assert all(h == h.strip() for h in hosts)
    assert "10.0.0.5" not in hosts
    assert len(hosts) == 253


# --- which networks to sweep: LAN interfaces only, never a VPN or bridge ---

import struct

_LINUX_CODES = {0x8915: "addr", 0x891B: "mask", 0x8913: "flags"}
_DARWIN_CODES = {0xC0206921: "addr", 0xC0206925: "mask", 0xC0206911: "flags"}

# A MacBook on Wi-Fi with a WireGuard VPN (ProtonVPN, 10.2.0.2 on utun4) and a
# VM network (bridge100), as `ifconfig` shows them. Names with no IPv4 address
# (gif0, stf0, utun0..3, awdl0) answer the address ioctl with EADDRNOTAVAIL.
MACOS = [
    ("lo0", "127.0.0.1", "255.0.0.0", 0x8049),  # UP,LOOPBACK,RUNNING,MULTICAST
    ("gif0", None, None, 0x8010),
    ("stf0", None, None, 0x0000),
    ("en0", "192.168.1.23", "255.255.255.0", 0x8863),  # UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST
    ("utun0", None, None, 0x8051),
    ("awdl0", None, None, 0x8943),
    ("utun4", "10.2.0.2", "255.255.255.255", 0x8051),  # UP,POINTOPOINT,RUNNING,MULTICAST
    ("bridge100", "192.168.139.3", "255.255.254.0", 0x8A63),
]

# A Synology NAS seen from a host-network container, as `ip -o -4 addr` shows
# it: the LAN on eth0, Docker bridges, a WireGuard split tunnel routing 10/8,
# Tailscale, an OpenVPN tun and a tap.
LINUX = [
    ("lo", "127.0.0.1", "255.0.0.0", 0x0049),  # UP,LOOPBACK,RUNNING
    ("eth0", "192.168.1.20", "255.255.255.0", 0x1043),  # UP,BROADCAST,RUNNING,MULTICAST
    ("eth1", None, None, 0x1002),
    ("docker0", "172.17.0.1", "255.255.0.0", 0x1003),
    ("br-3f2a9c1d7e10", "172.18.0.1", "255.255.0.0", 0x1043),
    ("veth1a2b3c4", None, None, 0x1043),
    ("wg0", "10.64.0.2", "255.0.0.0", 0x0091),  # UP,POINTOPOINT,NOARP
    ("tailscale0", "100.101.102.103", "255.255.255.255", 0x10D1),
    ("tun0", "10.8.0.6", "255.255.255.0", 0x1051),
    ("tap0", "10.9.0.2", "255.255.255.0", 0x1043),
]
# /sys/class/net/<if>/type (ARPHRD_*) and tun_flags (present on tun and tap)
# for the LINUX interfaces, as the kernel shows them.
LINUX_SYSFS = {
    ("lo", "type"): "772\n",
    ("eth0", "type"): "1\n",
    ("docker0", "type"): "1\n",
    ("br-3f2a9c1d7e10", "type"): "1\n",
    ("wg0", "type"): "65534\n",
    ("tailscale0", "type"): "65534\n",
    ("tailscale0", "tun_flags"): "0x1002\n",
    ("tun0", "type"): "65534\n",
    ("tun0", "tun_flags"): "0x1001\n",
    ("tap0", "type"): "1\n",
    ("tap0", "tun_flags"): "0x1002\n",
}


def _use_interfaces(monkeypatch, table, platform, sysfs=None):
    """Answer if_nameindex and the ifreq ioctls from ``table`` the way the
    ``platform`` kernel lays out struct ifreq, and /sys/class/net reads from
    ``sysfs`` ({(name, attribute): text}; anything else is missing)."""
    codes = _LINUX_CODES if platform == "linux" else _DARWIN_CODES
    rows = {name: row for name, *row in table}

    def sockaddr(ip):
        packed = socket.inet_aton(ip)
        if platform == "linux":
            return struct.pack("=HH4s8x", socket.AF_INET, 0, packed)
        return struct.pack("=BBH4s8x", 16, socket.AF_INET, 0, packed)

    class FakeFcntl:
        @staticmethod
        def ioctl(fd, request, arg):
            name = bytes(arg[:16]).rstrip(b"\x00").decode()
            what = codes[request]
            ip, mask, flags = rows[name]
            head = bytes(arg[:16])
            if what == "flags":
                return head + struct.pack("=H", flags) + bytes(len(arg) - 18)
            value = ip if what == "addr" else mask
            if value is None:
                raise OSError(49, "Can't assign requested address")
            return head + sockaddr(value) + bytes(len(arg) - 32)

    monkeypatch.setattr(disc, "fcntl", FakeFcntl)
    monkeypatch.setattr(disc, "_PLATFORM", platform)
    monkeypatch.setattr(
        disc.socket, "if_nameindex", lambda: [(i + 1, name) for i, (name, *_r) in enumerate(table)]
    )
    monkeypatch.setattr(disc, "local_ip", lambda: pytest.fail("fell back to the routing table"))
    files = sysfs or {}
    monkeypatch.setattr(disc, "_read_sysfs", lambda name, attr: files.get((name, attr)))


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_interface_addresses_reads_ifreq_on_both_platforms(monkeypatch, platform):
    _use_interfaces(monkeypatch, LINUX, platform)
    found = disc.interface_addresses()
    assert ("eth0", "192.168.1.20", "255.255.255.0", 0x1043) in found
    assert ("wg0", "10.64.0.2", "255.0.0.0", 0x0091) in found
    assert all(name != "eth1" for name, *_ in found)  # no IPv4 address


def test_lan_addresses_macos_skips_vpn_and_vm_bridge(monkeypatch):
    _use_interfaces(monkeypatch, MACOS, "darwin")
    assert disc.lan_addresses() == [("192.168.1.23", "255.255.255.0")]


def test_lan_addresses_linux_skips_tunnels_and_docker(monkeypatch):
    _use_interfaces(monkeypatch, LINUX, "linux", LINUX_SYSFS)
    assert disc.lan_addresses() == [("192.168.1.20", "255.255.255.0")]


def test_vpn_routing_10_8_does_not_get_its_subnet_swept(monkeypatch):
    # The routing-table trick would answer 10.64.0.2 (wg0 routes 10/8 here).
    disc._reset_sweep_state()
    _use_interfaces(monkeypatch, LINUX, "linux", LINUX_SYSFS)
    swept = []
    monkeypatch.setattr(disc, "discover_hubs", lambda hosts, **kw: swept.extend(hosts) or [])
    disc.discover_local()
    assert len(swept) == 253
    assert all(h.startswith("192.168.1.") for h in swept)


@pytest.mark.parametrize("name", ["office", "edge0", "ham0", "tinc0"])
def test_linux_tap_vpn_with_a_custom_name_is_skipped(monkeypatch, name):
    # OpenVPN tap, n2n, Hamachi, tinc: ethernet framing, not point-to-point,
    # but a tun/tap device, which sysfs shows by tun_flags.
    table = [("lo", "127.0.0.1", "255.0.0.0", 0x49), (name, "10.7.0.2", "255.255.255.0", 0x1043)]
    sysfs = {(name, "type"): "1\n", (name, "tun_flags"): "0x1002\n"}
    _use_interfaces(monkeypatch, table, "linux", sysfs)
    assert disc.lan_addresses() == []


def test_linux_interface_that_is_not_ethernet_is_skipped(monkeypatch):
    table = [("lo", "127.0.0.1", "255.0.0.0", 0x49), ("lan0", "10.7.0.2", "255.255.255.0", 0x1043)]
    _use_interfaces(monkeypatch, table, "linux", {("lan0", "type"): "65534\n"})
    assert disc.lan_addresses() == []


def test_linux_missing_sysfs_falls_through_to_the_other_rules(monkeypatch):
    table = [("lo", "127.0.0.1", "255.0.0.0", 0x49), ("eth0", "192.168.1.20", "255.255.255.0", 0x1043)]
    _use_interfaces(monkeypatch, table, "linux", {})
    assert disc.lan_addresses() == [("192.168.1.20", "255.255.255.0")]


def test_sysfs_is_not_consulted_on_macos(monkeypatch):
    _use_interfaces(monkeypatch, MACOS, "darwin")
    monkeypatch.setattr(disc, "_read_sysfs", lambda *a: pytest.fail("read sysfs on macOS"))
    assert disc.lan_addresses() == [("192.168.1.23", "255.255.255.0")]


@pytest.mark.parametrize("name", ["br-lan", "bridge0", "br0", "br-guest"])
def test_real_linux_lan_bridges_are_swept(monkeypatch, name):
    table = [("lo", "127.0.0.1", "255.0.0.0", 0x49), (name, "192.168.1.1", "255.255.255.0", 0x1043)]
    _use_interfaces(monkeypatch, table, "linux", {(name, "type"): "1\n"})
    assert disc.lan_addresses() == [("192.168.1.1", "255.255.255.0")]


def test_only_dockers_own_bridge_names_are_skipped(monkeypatch):
    table = [
        ("lo", "127.0.0.1", "255.0.0.0", 0x49),
        ("br-3f2a9c1d7e10", "172.18.0.1", "255.255.0.0", 0x1043),
        ("br-3f2a9c1d7e1", "172.19.0.1", "255.255.0.0", 0x1043),  # 11 hex: not Docker's
    ]
    _use_interfaces(monkeypatch, table, "linux")
    assert disc.lan_addresses() == [("172.19.0.1", "255.255.0.0")]


def test_point_to_point_link_is_skipped_whatever_its_name(monkeypatch):
    table = [("lo0", "127.0.0.1", "255.0.0.0", 0x8049), ("en7", "10.20.0.2", "255.255.255.0", 0x8051)]
    _use_interfaces(monkeypatch, table, "darwin")
    assert disc.lan_addresses() == []


def test_interface_that_is_down_is_skipped(monkeypatch):
    table = [("lo", "127.0.0.1", "255.0.0.0", 0x49), ("eth0", "192.168.1.20", "255.255.255.0", 0x1002)]
    _use_interfaces(monkeypatch, table, "linux")
    assert disc.lan_addresses() == []


@pytest.mark.parametrize(
    "name", ["cscotun0", "gpd0", "nordlynx", "zt3jnwghuq", "ipsec0", "ppp0", "virbr0", "vmenet0"]
)
def test_other_vpn_and_vm_interfaces_are_skipped(monkeypatch, name):
    table = [("lo", "127.0.0.1", "255.0.0.0", 0x49), (name, "10.7.0.2", "255.255.255.0", 0x1043)]
    _use_interfaces(monkeypatch, table, "linux")
    assert disc.lan_addresses() == []


def test_only_tunnels_means_no_sweep_and_no_fallback(monkeypatch):
    disc._reset_sweep_state()
    table = [("lo", "127.0.0.1", "255.0.0.0", 0x49), ("wg0", "10.64.0.2", "255.0.0.0", 0x91)]
    _use_interfaces(monkeypatch, table, "linux")
    monkeypatch.setattr(disc, "_udp_probe", lambda *a, **k: pytest.fail("probed"))
    assert disc.discover_local() == []


def test_falls_back_to_the_routing_table_when_interfaces_cannot_be_listed(monkeypatch):
    def no_listing():
        raise OSError("not supported")

    monkeypatch.setattr(disc.socket, "if_nameindex", no_listing)
    monkeypatch.setattr(disc, "local_ip", lambda: "192.168.1.20")
    assert disc.interface_addresses() is None
    assert disc.lan_addresses() == [("192.168.1.20", "255.255.255.0")]


def test_falls_back_on_a_platform_without_known_ioctls(monkeypatch):
    monkeypatch.setattr(disc, "_PLATFORM", "win32")
    monkeypatch.setattr(disc, "local_ip", lambda: "10.0.0.5")
    assert disc.lan_addresses() == [("10.0.0.5", "255.255.255.0")]


def test_fallback_still_refuses_a_non_lan_address(monkeypatch):
    monkeypatch.setattr(disc, "interface_addresses", lambda: None)
    monkeypatch.setattr(disc, "interface_addresses", lambda: None)  # fallback path
    monkeypatch.setattr(disc, "local_ip", lambda: "100.101.102.103")
    assert disc.lan_addresses() == []


def test_sweep_hosts_covers_each_lan_24_once():
    hosts = disc.sweep_hosts(
        [("192.168.1.20", "255.255.255.0"), ("10.0.5.4", "255.255.255.0"), ("192.168.1.30", "255.255.255.0")]
    )
    assert len(hosts) == len(set(hosts))
    assert {h.rsplit(".", 1)[0] for h in hosts} == {"192.168.1", "10.0.5"}
    assert "192.168.1.20" not in hosts and "192.168.1.30" not in hosts and "10.0.5.4" not in hosts
    assert len(hosts) == 252 + 253


def test_sweep_hosts_takes_the_24_of_a_wider_lan():
    hosts = disc.sweep_hosts([("172.16.4.9", "255.255.0.0")])
    assert hosts[0] == "172.16.4.1" and hosts[-1] == "172.16.4.254"
    assert len(hosts) == 253


def test_sweep_hosts_stays_inside_a_narrower_lan():
    hosts = disc.sweep_hosts([("10.0.0.5", "255.255.255.240")])
    assert hosts == [f"10.0.0.{i}" for i in range(1, 15) if i != 5]


def test_sweep_hosts_sweeps_at_most_four_24s():
    addresses = [(f"10.0.{i}.2", "255.255.255.0") for i in range(6)]
    hosts = disc.sweep_hosts(addresses)
    assert {h.rsplit(".", 1)[0] for h in hosts} == {"10.0.0", "10.0.1", "10.0.2", "10.0.3"}


def test_sweep_hosts_refuses_non_lan_addresses():
    assert disc.sweep_hosts([("8.8.8.8", "255.255.255.0"), ("100.64.0.5", "255.192.0.0")]) == []


def test_several_lans_are_one_sweep_with_merged_results(monkeypatch):
    disc._reset_sweep_state()
    table = [
        ("lo", "127.0.0.1", "255.0.0.0", 0x49),
        ("eth0", "192.168.1.20", "255.255.255.0", 0x1043),
        ("eth1", "10.0.5.4", "255.255.255.0", 0x1043),
    ]
    _use_interfaces(monkeypatch, table, "linux")
    hub = HUB_REPLY.decode()
    monkeypatch.setattr(
        disc, "_udp_probe", lambda ip, *a, **k: hub if ip in ("192.168.1.7", "10.0.5.9") else None
    )
    found = disc.discover_local(timeout=0.01)
    assert found == [{"ip": "192.168.1.7", "idu": 15792}, {"ip": "10.0.5.9", "idu": 15792}]


# --- a probe to a hub this process is talking to goes through its wire ---

import hearthmage.nexho_client as nexho


@pytest.fixture()
def wires(monkeypatch):
    registry = {}
    monkeypatch.setattr(nexho, "_wires", registry)
    return registry


def _recording_probe(monkeypatch, addr, reply=HUB_REPLY.decode()):
    seen = []

    def probe(ip, port, timeout, *a, **k):
        wire = nexho._wires.get((ip, port))
        seen.append({"at": time.monotonic(), "wire_held": bool(wire and wire.lock.locked())})
        return reply

    monkeypatch.setattr(disc, "_udp_probe", probe)
    return seen


def test_probe_to_a_hub_waits_for_its_wire(monkeypatch, wires):
    wire = nexho._wire_for(("10.0.0.20", 6653))
    seen = _recording_probe(monkeypatch, ("10.0.0.20", 6653))
    wire.lock.acquire()  # the poller is mid-conversation
    out = []
    t = threading.Thread(target=lambda: out.append(disc.discover_hubs(["10.0.0.20"])))
    t.start()
    time.sleep(0.2)
    assert seen == []  # nothing went out while the poller held the wire
    wire.replied_at = time.monotonic()
    released = time.monotonic()
    wire.lock.release()
    t.join(3)
    assert out == [[{"ip": "10.0.0.20", "idu": 15792}]]
    assert seen[0]["wire_held"]
    assert seen[0]["at"] - released >= nexho._HUB_QUIET_GAP * 0.9  # quiet gap honoured
    assert wire.replied_at >= seen[0]["at"]  # the poller's next query waits out the gap too


def test_probe_to_a_hub_yields_to_a_pending_write(monkeypatch, wires):
    wire = nexho._wire_for(("10.0.0.20", 6653))
    seen = _recording_probe(monkeypatch, ("10.0.0.20", 6653))
    with wire.cond:
        wire.writes_pending = 1
    t = threading.Thread(target=lambda: disc.discover_hubs(["10.0.0.20"]))
    t.start()
    time.sleep(0.2)
    assert seen == []
    with wire.cond:
        wire.writes_pending = 0
        wire.cond.notify_all()
    t.join(3)
    assert len(seen) == 1


def test_busy_wire_of_a_hub_that_answered_lately_lists_it_without_probing(monkeypatch, wires):
    from types import SimpleNamespace

    monkeypatch.setattr(disc, "_WIRE_WAIT", 0.05)
    wire = nexho._wire_for(("10.0.0.20", 6653))
    wire.owner = SimpleNamespace(_last_ops2_ok=time.monotonic() - 5)  # a healthy client
    monkeypatch.setattr(disc, "_udp_probe", lambda *a, **k: pytest.fail("probed a busy hub"))
    wire.lock.acquire()
    try:
        assert disc.discover_hubs(["10.0.0.20"]) == [{"ip": "10.0.0.20", "idu": None}]
    finally:
        wire.lock.release()


@pytest.mark.parametrize("last_zone_list", ["long ago", "never", "no client"])
def test_busy_wire_of_a_silent_hub_lists_nothing(monkeypatch, wires, last_zone_list):
    from types import SimpleNamespace

    monkeypatch.setattr(disc, "_WIRE_WAIT", 0.05)
    wire = nexho._wire_for(("10.0.0.20", 6653))
    # The quiet-gap clock is no evidence of a hub: a timed-out probe sets it too.
    wire.replied_at = time.monotonic()
    if last_zone_list == "long ago":
        wire.owner = SimpleNamespace(_last_ops2_ok=time.monotonic() - 3600)
    elif last_zone_list == "never":
        wire.owner = SimpleNamespace(_last_ops2_ok=None)
    monkeypatch.setattr(disc, "_udp_probe", lambda *a, **k: pytest.fail("probed a busy hub"))
    wire.lock.acquire()
    try:
        assert disc.discover_hubs(["10.0.0.20"]) == []
    finally:
        wire.lock.release()


def test_timed_out_probe_still_restarts_the_quiet_gap(monkeypatch, wires):
    # The hub may answer late; the poller's next query must not meet that echo.
    wire = nexho._wire_for(("10.0.0.20", 6653))
    seen = _recording_probe(monkeypatch, ("10.0.0.20", 6653), reply=None)
    assert disc.discover_hubs(["10.0.0.20"]) == []
    assert wire.replied_at is not None and wire.replied_at >= seen[0]["at"]


def test_address_with_no_wire_is_probed_directly_and_gets_none(monkeypatch, wires):
    seen = _recording_probe(monkeypatch, ("10.0.0.21", 6653))
    assert disc.discover_hubs(["10.0.0.21"]) == [{"ip": "10.0.0.21", "idu": 15792}]
    assert seen[0]["wire_held"] is False
    assert wires == {}  # 253 probes must not leave 253 wires behind


def test_configured_hub_is_probed_on_its_wire_even_before_a_client_exists(monkeypatch, wires):
    # A client built for the hub during the probe must wait for it.
    seen = _recording_probe(monkeypatch, ("10.0.0.20", 6653))
    disc.discover_hubs(["10.0.0.19", "10.0.0.20"], hub=("10.0.0.20", 6653))
    assert sorted(s["wire_held"] for s in seen) == [False, True]
    assert set(wires) == {("10.0.0.20", 6653)}


def test_discover_local_passes_the_configured_hub_on(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "lan_addresses", lambda: [("10.0.0.5", "255.255.255.0")])
    calls = []
    monkeypatch.setattr(disc, "discover_hubs", lambda hosts, **kw: calls.append(kw) or [])
    disc.discover_local(hub=("10.0.0.20", 6653))
    assert calls[0]["hub"] == ("10.0.0.20", 6653)
