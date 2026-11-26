import pytest


@pytest.fixture(autouse=True)
def _fresh_auth_state(monkeypatch):
    """The login throttle and the session store are process-wide; give every
    test its own so failed sign-ins in one test cannot slow the next."""
    import hearthmage.app as appmod
    from hearthmage.auth import LoginThrottle

    monkeypatch.setattr(appmod, "_login_throttle", LoginThrottle())
    monkeypatch.setattr(appmod, "_session_store", None)


def _is_local_address(address):
    """True for a Unix socket path or a loopback IP (127.0.0.0/8, ::1)."""
    import ipaddress

    if isinstance(address, (bytes, bytearray, str)):
        return True  # AF_UNIX path
    host = address[0] if isinstance(address, tuple) and address else None
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail loudly if a test would leave the machine: only loopback and Unix
    sockets are allowed, and no DNS lookups for real host names."""
    import socket

    def guard_method(name):
        orig = getattr(socket.socket, name)

        def guarded(self, *args, **kwargs):
            if name == "sendmsg":
                address = args[3] if len(args) > 3 else kwargs.get("address")
            elif name == "sendto":
                address = args[-1] if args else None
            else:
                address = args[0] if args else None
            if address is not None and not _is_local_address(address):
                raise RuntimeError(
                    f"test tried a real network call: socket.{name} to {address!r}"
                )
            return orig(self, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)

    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        guard_method(name)

    orig_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host is not None and host != "" and not _is_local_address((host,)):
            raise RuntimeError(f"test tried a real DNS lookup: {host!r}")
        return orig_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)

    orig_create = socket.create_connection

    def guarded_create_connection(address, *args, **kwargs):
        if not _is_local_address(address):
            raise RuntimeError(
                f"test tried a real network call: socket.create_connection to {address!r}"
            )
        return orig_create(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
