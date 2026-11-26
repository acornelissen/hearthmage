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
    monkeypatch.setattr(disc, "local_ip", lambda: "8.8.8.8")
    monkeypatch.setattr(disc, "_udp_probe", lambda *a, **k: pytest.fail("probed"))
    assert disc.discover_local() == []


def test_discover_local_does_not_probe_a_tailscale_address(monkeypatch):
    disc._reset_sweep_state()
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
    monkeypatch.setattr(disc, "local_ip", lambda: "10.0.0.5")
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
    monkeypatch.setattr(disc, "local_ip", lambda: "10.0.0.5")
    calls = []
    monkeypatch.setattr(disc, "discover_hubs", lambda hosts, **kw: calls.append(1) or [])
    disc.discover_local()
    disc.discover_local()
    assert len(calls) == 2


def test_waiters_receive_the_leaders_exception(monkeypatch):
    disc._reset_sweep_state()
    monkeypatch.setattr(disc, "local_ip", lambda: "10.0.0.5")
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
    monkeypatch.setattr(disc, "local_ip", lambda: "10.0.0.5")
    monkeypatch.setattr(disc, "_WAIT_SLACK", 0.0)
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
    monkeypatch.setattr(disc, "local_ip", lambda: "10.0.0.5")
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
