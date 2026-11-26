"""Find the Nexho hub on the local network.

The hub is the one device that answers the Nexho LAN protocol: a single
``OPS1/`` datagram to UDP 6653 comes back as ``OPOK,OPS1,...`` carrying the IDU
serial. Discovery sweeps the local /24, sending each address exactly one packet,
so the hub only ever receives one probe (no flooding). See docs/nexho-protocol.md.

The sweep is LAN-only: it runs only when this machine's address is RFC 1918
private. A public address, loopback, link-local or the 100.64.0.0/10 CGNAT range
(Tailscale and carrier NAT; not the home LAN) is never swept. Concurrent callers
share one sweep, and a reply counts only if it comes from the probed IP and port.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from hearthmage.lan import is_lan_ipv4

_DEFAULT_PORT = 6653
_PROBE = b"OPS1/"
_WORKERS = 64
_ATTEMPTS = 2
_WAIT_SLACK = 1.0


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
    """This machine's address on the LAN (no packet is actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


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


def discover_hubs(
    hosts: list[str],
    port: int = _DEFAULT_PORT,
    timeout: float = 0.6,
    workers: int = _WORKERS,
    probe: Callable[[str], str | None] | None = None,
) -> list[dict]:
    """Probe ``hosts`` in parallel; return [{ip, idu}] for those that are hubs."""
    do_probe = probe or (lambda ip: _udp_probe(ip, port, timeout))
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


def discover_local(timeout: float = 0.6) -> list[dict]:
    """Sweep this machine's /24 for hubs.

    Single flight: while a sweep runs, other callers wait for it and share its
    result instead of starting threads of their own (a waiter gives up with []
    after the worst-case sweep time). Nothing is cached. Returns [] without sending anything when the local
    address is not a private LAN address.
    """
    global _inflight
    ip = local_ip()
    hosts = subnet_hosts(ip)
    if not hosts:
        return []
    with _sweep_lock:
        sweep = _inflight
        leader = sweep is None
        if leader:
            sweep = _inflight = _Sweep()
    if leader:
        try:
            sweep.result = discover_hubs(hosts, timeout=timeout)
        except BaseException as exc:
            sweep.error = exc
        finally:
            with _sweep_lock:
                _inflight = None
            sweep.done.set()
    else:
        # Worst case is every round of workers timing out on every attempt.
        rounds = -(-len(hosts) // _WORKERS)
        if not sweep.done.wait(rounds * _ATTEMPTS * timeout + _WAIT_SLACK):
            return []  # leader is stuck; the UI falls back to manual entry
    if sweep.error:
        raise sweep.error
    return [dict(h) for h in sweep.result]
