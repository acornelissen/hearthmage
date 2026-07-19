"""Find the Nexho hub on the local network.

The hub is the one device that answers the Nexho LAN protocol: a single
``OPS1/`` datagram to UDP 6653 comes back as ``OPOK,OPS1,...`` carrying the IDU
serial. Discovery sweeps the local /24, sending each address exactly one packet,
so the hub only ever receives one probe (no flooding). See docs/nexho-protocol.md.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

_DEFAULT_PORT = 6653
_PROBE = b"OPS1/"


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
    """The 253 other host addresses in ``ip``'s /24 (our own address skipped)."""
    if not ip:
        return []
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return []
    prefix = ".".join(parts[:3])
    return [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != ip]


def _udp_probe(ip: str, port: int, timeout: float, attempts: int = 2) -> str | None:
    # Retry a couple of times so a momentarily-busy hub (it services one
    # conversation at a time) still answers. Only the hub ever replies, so the
    # retries add no load to other hosts.
    for _ in range(attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(_PROBE, (ip, port))
            data, _ = sock.recvfrom(1024)
            return data.rstrip(b"\x00").decode("ascii", errors="replace")
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
    workers: int = 64,
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


def discover_local(timeout: float = 0.6) -> list[dict]:
    """Convenience: sweep this machine's /24 for hubs."""
    return discover_hubs(subnet_hosts(local_ip()), timeout=timeout)
