"""Which addresses count as the home LAN.

The app talks to two things on the network: the hub (UDP) and, optionally, an
MQTT broker (TCP). Both must stay on the LAN, so every place that accepts one
of those addresses (the settings forms, a backup import, the environment)
checks it here. Discovery uses the same RFC 1918 check before it sweeps.

- The hub must be an IPv4 literal in 10/8, 172.16/12 or 192.168/16.
- The broker may also be loopback (a broker on the same box) or a LAN name:
  single-label (no trailing dot) or ending in .local/.lan/.home.arpa/.internal
  (a trailing dot is fine there, ``broker.lan.``, and is kept). Names are not
  resolved here: DNS can change later, and a request must not wait on it.

100.64.0.0/10 (Tailscale and carrier NAT) is not the home LAN for either.
"""

from __future__ import annotations

import ipaddress

# Explicit RFC 1918 ranges rather than ipaddress.is_private, which also accepts
# documentation ranges (192.0.2/24, 203.0.113/24, ...) and others.
LAN_NETS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_.")


class AddressRejected(ValueError):
    """A hub or broker address (or port) the app will not use. ``field`` names
    the form field it came from, so the page can mark that field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def host_name(raw: str) -> str | None:
    """A DNS name lowercased with one trailing dot dropped, or None if it is
    not a plain name (empty label, stray character, too long)."""
    name = raw.lower()
    if name.endswith("."):
        name = name[:-1]
    if not name or len(name) > 253 or not set(name) <= _NAME_CHARS:
        return None
    if any(label == "" for label in name.split(".")):
        return None
    return name


def is_lan_ipv4(value) -> bool:
    """Whether ``value`` is an RFC 1918 IPv4 address written the usual way.
    ``IPv4Address`` refuses the shorthand forms (``10.1``, ``0x0a000001``,
    leading zeros) that the socket layer would quietly accept."""
    if not isinstance(value, str):
        return False
    try:
        addr = ipaddress.IPv4Address(value.strip())
    except ValueError:
        return False
    return any(addr in net for net in LAN_NETS)


def is_lan_broker_host(value) -> bool:
    """Whether ``value`` is an acceptable MQTT broker host: an RFC 1918 IPv4
    address, loopback, or a LAN name (see the module docstring)."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        addr = None
    if addr is not None:
        return addr.is_loopback or is_lan_ipv4(text)
    name = host_name(text)
    if name is None:
        return False
    if text.endswith(".") and "." not in name:
        # "ai." is an absolute name: it skips the search domain and asks a TLD.
        return False
    last = name.rsplit(".", 1)[-1]
    if last.isdigit() or last.startswith("0x"):
        # The resolver reads "134744072" or "0x8080808" as an IPv4 address.
        return False
    return "." not in name or name.endswith(LAN_SUFFIXES)
