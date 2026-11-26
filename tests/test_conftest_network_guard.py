import socket

import pytest


def test_udp_sendto_to_lan_address_is_refused():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(RuntimeError, match="10.0.0.99"):
            s.sendto(b"x", ("10.0.0.99", 6653))
    finally:
        s.close()


def test_tcp_connect_to_external_address_is_refused():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="192.0.2.1"):
            s.connect(("192.0.2.1", 80))
    finally:
        s.close()


def test_create_connection_and_dns_are_refused():
    with pytest.raises(RuntimeError, match="192.0.2.1"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    with pytest.raises(RuntimeError, match="example.com"):
        socket.getaddrinfo("example.com", 80)


def test_loopback_is_allowed():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tx.sendto(b"hi", rx.getsockname())
        assert rx.recvfrom(10)[0] == b"hi"
        assert socket.getaddrinfo("localhost", 80)
    finally:
        tx.close()
        rx.close()
