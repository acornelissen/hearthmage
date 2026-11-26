"""Find the Nexho hub on the local network.

The hub is the one device that answers the Nexho LAN protocol: a single
``OPS1/`` datagram to UDP 6653 comes back as ``OPOK,OPS1,...`` carrying the IDU
serial. Discovery sweeps the local /24, sending each address exactly one packet,
so the hub only ever receives one probe (no flooding). See docs/nexho-protocol.md.

The sweep is LAN-only. It covers the /24 of each RFC 1918 address on a network
interface that is up and is not loopback, a point-to-point link, a VPN or
tunnel device, or a container or VM bridge (by name), at most four /24s, all in
one sweep. A public address, loopback, link-local or the 100.64.0.0/10 CGNAT
range (Tailscale and carrier NAT; not the home LAN) is never swept. Where the
interfaces cannot be listed, it falls back to the address the routing table
would use, which cannot tell a VPN apart. Concurrent callers share one sweep,
and a reply counts only if it comes from the probed IP and port.

A probe to a hub this process talks to goes through that hub's wire (see
nexho_client): the hub answers a query sent straight after its last reply with
that reply, so a probe must not land in the middle of the poller's exchange.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from hearthmage.lan import is_lan_ipv4
from hearthmage.nexho_client import WireBusy, exchange_on_wire

try:
    import fcntl
except ImportError:  # not on Windows; discovery then falls back to local_ip()
    fcntl = None

_DEFAULT_PORT = 6653
_PROBE = b"OPS1/"
_WORKERS = 64
_ATTEMPTS = 2
_WAIT_SLACK = 1.0
_MAX_SUBNETS = 4  # /24s per sweep, so a box with many networks is not a flood
# How long a probe waits for a hub's wire while the poller is talking to it.
# If it stays busy, the hub is listed (without its IDU) when it answered the
# running client's zone list within _RECENT_REPLY seconds, else left out.
_WIRE_WAIT = 2.0
_RECENT_REPLY = 60.0

# The ifreq ioctls that read one interface's IPv4 address, netmask and flags
# (SIOCGIFADDR, SIOCGIFNETMASK, SIOCGIFFLAGS). The request numbers differ by
# OS, but the parts read here sit at the same offsets on both: the name in
# bytes 0-15, the flags (a short) at 16, a sockaddr_in's address at 20-23.
_PLATFORM = sys.platform
_IFREQ_IOCTLS = {
    "linux": (0x8915, 0x891B, 0x8913),
    "darwin": (0xC0206921, 0xC0206925, 0xC0206911),
}
_IFREQ_SIZE = 40  # Linux's struct ifreq; macOS's is 32
_IFF_UP = 0x1
_IFF_LOOPBACK = 0x8
_IFF_POINTOPOINT = 0x10
# Interfaces that are never the home LAN, by name. VPN and tunnel devices:
# macOS utun/ipsec/ppp/gif/stf; Linux tun/tap/wg (WireGuard, Mullvad's
# wg0-mullvad), tailscale, ZeroTier zt*, NordLynx, ProtonVPN, Cisco cscotun,
# GlobalProtect gpd, GRE/IPIP/SIT/VTI. Container and VM bridges: Docker
# docker0/veth* (its user networks are br- and 12 hex digits, matched exactly so
# OpenWrt's br-lan still counts), libvirt virbr*, LXD, Podman/CNI, macOS
# vmnet/vmenet. On Linux, sysfs also catches tun/tap VPNs of any name.
_NOT_LAN_PREFIXES = (
    "utun", "tun", "tap", "wg", "tailscale", "ppp", "ipsec", "gif", "stf", "zt",
    "nordlynx", "proton", "pvpn", "gpd", "vti", "ip_vti", "gre", "erspan", "ipip",
    "sit", "docker", "veth", "virbr", "vmnet", "vmenet", "lxdbr", "podman", "cni",
)
_NOT_LAN_PARTS = ("tun", "vpn")  # cscotun0, nordtun, fctvpn, ...
_DOCKER_BRIDGE = re.compile(r"br-[0-9a-f]{12}")
# macOS bridgeN is the Thunderbolt Bridge, Internet Sharing or a VM network.
# On Linux a bridge named bridge0 can be the LAN, so this is macOS only.
_DARWIN_NOT_LAN_PREFIXES = ("bridge",)
_ARPHRD_ETHER = "1"  # /sys/class/net/<if>/type of Ethernet and Wi-Fi


def parse_ops1_idu(resp: str) -> int | None:
    """Extract the IDU serial from an ``OPS1`` reply, or None if not a hub.

    Reply ``OPOK,OPS1,24,192,0,0,0,38,40,176,61,0`` -> fields [9],[10],[11] are
    lo,mid,hi of the IDU: ``lo + mid*256 + hi*65536`` (= 15792 in this example).
    """
    if not resp.startswith("OPOK,OPS1"):
        return None
    fields = resp.split(",")
    if len(fields) < 12:
        return None
    try:
        return int(fields[9]) + int(fields[10]) * 256 + int(fields[11]) * 65536
    except ValueError:
        return None


def local_ip() -> str | None:
    """The address the routing table would send a LAN packet from (no packet
    is actually sent). Behind a VPN that routes 10/8 this is the VPN's address,
    so it is only the fallback for when interfaces cannot be listed."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def interface_addresses() -> list[tuple[str, str, str, int]] | None:
    """(name, address, netmask, flags) of each interface with an IPv4 address,
    in interface order, or None where the interfaces cannot be listed (another
    OS, no fcntl, or not even loopback answered). One primary address per
    interface. The stdlib has no portable call for this, and the container
    image has neither ``ip`` nor ``ifconfig``, so it asks the kernel directly."""
    codes = _IFREQ_IOCTLS.get(_PLATFORM)
    if codes is None or fcntl is None:
        return None
    try:
        names = socket.if_nameindex()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    found = []
    try:
        for _index, name in names:
            raw = os.fsencode(name)
            if len(raw) > 15:
                continue  # not a kernel interface name (IFNAMSIZ is 16)
            req = struct.pack(f"{_IFREQ_SIZE}s", raw)
            try:
                addr = fcntl.ioctl(sock.fileno(), codes[0], req)
                mask = fcntl.ioctl(sock.fileno(), codes[1], req)
                flags = fcntl.ioctl(sock.fileno(), codes[2], req)
            except OSError:
                continue  # no IPv4 address, or gone since the listing
            found.append((
                name,
                socket.inet_ntoa(addr[20:24]),
                socket.inet_ntoa(mask[20:24]),
                struct.unpack_from("=H", flags, 16)[0],
            ))
    finally:
        sock.close()
    return found or None


def _read_sysfs(name: str, attribute: str) -> str | None:
    """/sys/class/net/<name>/<attribute>, or None if it cannot be read."""
    if "/" in name or name in (".", ".."):
        return None
    try:
        with open(f"/sys/class/net/{name}/{attribute}", encoding="ascii") as handle:
            return handle.read(64)
    except (OSError, ValueError):
        return None


def _is_linux_tunnel(name: str) -> bool:
    """Whether sysfs shows ``name`` as a tun/tap device or not Ethernet (a tap
    VPN such as OpenVPN, n2n or Hamachi is Ethernet, but has tun_flags). A
    file that cannot be read says nothing; the name rules still apply."""
    if _read_sysfs(name, "tun_flags") is not None:
        return True
    kind = _read_sysfs(name, "type")
    return kind is not None and kind.strip() != _ARPHRD_ETHER


def _is_lan_interface(name: str, flags: int) -> bool:
    if not flags & _IFF_UP or flags & (_IFF_LOOPBACK | _IFF_POINTOPOINT):
        return False
    lower = name.lower()
    if lower.startswith(_NOT_LAN_PREFIXES) or _DOCKER_BRIDGE.fullmatch(lower):
        return False
    if any(part in lower for part in _NOT_LAN_PARTS):
        return False
    if _PLATFORM == "darwin" and lower.startswith(_DARWIN_NOT_LAN_PREFIXES):
        return False
    return not (_PLATFORM == "linux" and _is_linux_tunnel(name))


def lan_addresses() -> list[tuple[str, str]]:
    """(address, netmask) of this machine on the home LAN, in interface order:
    RFC 1918 addresses on interfaces that are up and are not loopback,
    point-to-point, a tunnel or a container/VM bridge (see _NOT_LAN_PREFIXES;
    on Linux also sysfs). An empty list
    when only those exist. Where interfaces cannot be listed, the routing
    table's choice (local_ip) as a /24, if it is a LAN address."""
    interfaces = interface_addresses()
    if interfaces is None:
        ip = local_ip()
        return [(ip, "255.255.255.0")] if is_lan_ipv4(ip) else []
    return [
        (ip, mask)
        for name, ip, mask, flags in interfaces
        if _is_lan_interface(name, flags) and is_lan_ipv4(ip)
    ]


def sweep_hosts(addresses: list[tuple[str, str]]) -> list[str]:
    """The addresses to probe for ``addresses`` (from lan_addresses): the /24
    around each, or the whole subnet when it is smaller than a /24, each host
    once, this machine's own addresses left out, at most _MAX_SUBNETS
    subnets."""
    own = {ip for ip, _mask in addresses}
    hosts: list[str] = []
    seen: set[str] = set()
    subnets = 0
    for ip, mask in addresses:
        if subnets == _MAX_SUBNETS:
            break
        if not is_lan_ipv4(ip):
            continue
        try:
            net = ipaddress.IPv4Interface(f"{ip.strip()}/{mask}").network
        except ValueError:
            net = None  # an odd netmask: take the /24
        if net is not None and net.prefixlen > 24:
            candidates = [str(h) for h in net.hosts()]
        else:
            candidates = subnet_hosts(ip)
        fresh = [h for h in candidates if h not in own and h not in seen]
        if not fresh:
            continue  # a /24 already covered, or a /32 with no neighbours
        subnets += 1
        seen.update(fresh)
        hosts.extend(fresh)
    return hosts


def subnet_hosts(ip: str | None) -> list[str]:
    """The 253 other host addresses in ``ip``'s /24 (our own address skipped).

    Empty unless ``ip`` is a private LAN IPv4 address (see module docstring).
    """
    # 100.64.0.0/10 (Tailscale/CGNAT), loopback and link-local are not RFC 1918.
    if not is_lan_ipv4(ip):
        return []
    ip = str(ipaddress.IPv4Address(ip.strip()))  # build from the canonical form only
    prefix = ".".join(ip.split(".")[:3])
    return [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != ip]


def _udp_probe(ip: str, port: int, timeout: float, attempts: int = _ATTEMPTS) -> str | None:
    # Retry a couple of times so a momentarily-busy hub (it services one
    # conversation at a time) still answers. Only the hub ever replies, so the
    # retries add no load to other hosts.
    for _ in range(attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        try:
            sock.sendto(_PROBE, (ip, port))
            while True:
                data, addr = sock.recvfrom(1024)
                if addr == (ip, port):
                    return data.rstrip(b"\x00").decode("ascii", errors="replace")
                # Someone else answered (or spoofed); keep waiting for the host
                # we probed until the timeout runs out.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                sock.settimeout(remaining)
        except socket.timeout:
            continue
        except OSError:
            return None
        finally:
            sock.close()
    return None


# Stands in for a reply from a hub whose wire stayed busy but whose client is
# healthy: it is listed, without an IDU (parse_ops1_idu gives None).
_BUSY_HUB_REPLY = "OPOK,OPS1"


def _probe_host(ip: str, port: int, timeout: float, hub: tuple[str, int] | None) -> str | None:
    """Probe one host. A hub that a client of this process talks to (or the
    configured ``hub``) is probed on its wire, between the poller's exchanges."""
    try:
        return exchange_on_wire(
            (ip, port), lambda: _udp_probe(ip, port, timeout), _WIRE_WAIT, create=(ip, port) == hub
        )
    except WireBusy as busy:
        if busy.zone_list_at is not None and time.monotonic() - busy.zone_list_at <= _RECENT_REPLY:
            return _BUSY_HUB_REPLY
        return None


def discover_hubs(
    hosts: list[str],
    port: int = _DEFAULT_PORT,
    timeout: float = 0.6,
    workers: int = _WORKERS,
    probe: Callable[[str], str | None] | None = None,
    hub: tuple[str, int] | None = None,
) -> list[dict]:
    """Probe ``hosts`` in parallel; return [{ip, idu}] for those that are hubs.
    ``hub`` is the configured hub's (ip, port), probed on its wire."""
    do_probe = probe or (lambda ip: _probe_host(ip, port, timeout, hub))
    found: list[dict] = []
    if not hosts:
        return found
    with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as pool:
        for ip, resp in zip(hosts, pool.map(do_probe, hosts)):
            idu = parse_ops1_idu(resp) if resp else None
            if resp and resp.startswith("OPOK,OPS1"):
                found.append({"ip": ip, "idu": idu})
    return found


class _Sweep:
    """One sweep in progress; late callers wait on ``done`` and share it."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: list[dict] = []
        self.error: BaseException | None = None


_sweep_lock = threading.Lock()
_inflight: _Sweep | None = None


def _reset_sweep_state() -> None:
    global _inflight
    with _sweep_lock:
        _inflight = None


def discover_local(timeout: float = 0.6, hub: tuple[str, int] | None = None) -> list[dict]:
    """Sweep this machine's LAN /24s (see lan_addresses) for hubs. ``hub`` is
    the configured hub's (ip, port), if any (see discover_hubs).

    Single flight: while a sweep runs, other callers wait for it and share its
    result instead of starting threads of their own (a waiter gives up with []
    after the worst-case sweep time). Nothing is cached. Returns [] without
    sending anything when this machine has no private LAN address.
    """
    global _inflight
    hosts = sweep_hosts(lan_addresses())
    if not hosts:
        return []
    with _sweep_lock:
        sweep = _inflight
        leader = sweep is None
        if leader:
            sweep = _inflight = _Sweep()
    if leader:
        try:
            sweep.result = discover_hubs(hosts, timeout=timeout, hub=hub)
        except BaseException as exc:
            sweep.error = exc
        finally:
            with _sweep_lock:
                _inflight = None
            sweep.done.set()
    else:
        # Worst case is every round of workers timing out on every attempt,
        # plus one probe waiting out a busy hub wire.
        rounds = -(-len(hosts) // _WORKERS)
        if not sweep.done.wait(rounds * _ATTEMPTS * timeout + _WIRE_WAIT + _WAIT_SLACK):
            return []  # leader is stuck; the UI falls back to manual entry
    if sweep.error:
        raise sweep.error
    return [dict(h) for h in sweep.result]
