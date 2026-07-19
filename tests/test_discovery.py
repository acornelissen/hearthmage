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
