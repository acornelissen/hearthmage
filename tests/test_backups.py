from pathlib import Path
import json

import pytest

from hearthmage.backups import (
    build_bundle,
    list_backups,
    restore_bundle,
    rotate_backup,
)


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_rotate_backup_snapshots_the_file(tmp_path):
    src = tmp_path / "schedules.json"
    _write(src, {"1": {"0": [[20, 0, 600]]}})
    backup = rotate_backup(str(src))
    assert backup is not None
    saved = json.loads(open(backup, encoding="utf-8").read())
    assert saved == {"1": {"0": [[20, 0, 600]]}}
    # backups live in a sibling backups/ directory, not next to the original
    assert "backups" in backup


def test_rotate_backup_noop_when_source_missing(tmp_path):
    assert rotate_backup(str(tmp_path / "nope.json")) is None


def test_rotate_backup_prunes_to_keep_newest(tmp_path):
    src = tmp_path / "config.json"
    for i in range(5):
        _write(src, {"n": i})
        rotate_backup(str(src), keep=3)
    kept = list_backups(str(src))
    assert len(kept) == 3  # only the newest three survive


def test_list_backups_is_newest_first(tmp_path):
    src = tmp_path / "config.json"
    _write(src, {"n": 0})
    first = rotate_backup(str(src))
    _write(src, {"n": 1})
    second = rotate_backup(str(src))
    order = list_backups(str(src))
    assert order[0] == second and order[1] == first


def test_build_bundle_captures_both_files(tmp_path):
    cfg = tmp_path / "config.json"
    sched = tmp_path / "schedules.json"
    _write(cfg, {"hub_ip": "10.0.0.20"})
    _write(sched, {"1": {"0": [[20, 0, 600]]}})
    bundle = build_bundle(str(cfg), str(sched))
    assert bundle["config"] == {"hub_ip": "10.0.0.20"}
    assert bundle["schedules"] == {"1": {"0": [[20, 0, 600]]}}
    assert bundle["hearthmage_backup"] >= 1


def test_build_bundle_tolerates_missing_files(tmp_path):
    bundle = build_bundle(str(tmp_path / "config.json"), str(tmp_path / "schedules.json"))
    assert bundle["config"] is None
    assert bundle["schedules"] is None


def test_restore_bundle_writes_both_files(tmp_path):
    cfg = tmp_path / "config.json"
    sched = tmp_path / "schedules.json"
    restore_bundle(
        {"hearthmage_backup": 1, "config": {"hub_ip": "10.0.0.99"}, "schedules": {"2": {}}},
        str(cfg),
        str(sched),
    )
    assert json.loads(cfg.read_text())["hub_ip"] == "10.0.0.99"
    assert json.loads(sched.read_text()) == {"2": {}}


def test_restore_bundle_backs_up_existing_before_overwrite(tmp_path):
    cfg = tmp_path / "config.json"
    sched = tmp_path / "schedules.json"
    _write(cfg, {"hub_ip": "old"})
    _write(sched, {"1": {}})
    restore_bundle(
        {"hearthmage_backup": 1, "config": {"hub_ip": "10.0.0.2"}, "schedules": {"9": {}}},
        str(cfg),
        str(sched),
    )
    assert json.loads(cfg.read_text())["hub_ip"] == "10.0.0.2"
    # the pre-restore config is recoverable from a backup
    assert list_backups(str(cfg))  # at least one snapshot of the old file


def test_restore_bundle_rejects_foreign_payload(tmp_path):
    with pytest.raises(ValueError):
        restore_bundle({"not": "a bundle"}, str(tmp_path / "c.json"), str(tmp_path / "s.json"))


def test_restore_bundle_skips_absent_sections(tmp_path):
    cfg = tmp_path / "config.json"
    sched = tmp_path / "schedules.json"
    _write(sched, {"1": {}})
    restore_bundle({"hearthmage_backup": 1, "config": {"hub_ip": "10.0.0.2"}}, str(cfg), str(sched))
    assert cfg.exists()  # config written
    assert json.loads(sched.read_text()) == {"1": {}}  # schedules untouched (absent in bundle)


def test_build_bundle_redacts_secrets(tmp_path):
    cfg = tmp_path / "config.json"
    _write(cfg, {
        "hub_ip": "10.0.0.9",
        "secret_key": "supersecret",
        "mqtt": {"host": "10.0.0.5", "password": "brokerpass", "username": "u"},
    })
    bundle = build_bundle(str(cfg), str(tmp_path / "schedules.json"))
    dumped = json.dumps(bundle)
    assert "supersecret" not in dumped
    assert "brokerpass" not in dumped
    # non-secret content survives
    assert bundle["config"]["hub_ip"] == "10.0.0.9"
    assert bundle["config"]["mqtt"]["host"] == "10.0.0.5"
    # and the source file itself is untouched
    assert json.loads(cfg.read_text())["mqtt"]["password"] == "brokerpass"


def test_restore_bundle_keeps_stored_secrets_when_absent(tmp_path):
    cfg = tmp_path / "config.json"
    _write(cfg, {
        "hub_ip": "10.0.0.9",
        "secret_key": "supersecret",
        "mqtt": {"host": "10.0.0.5", "password": "brokerpass"},
    })
    bundle = build_bundle(str(cfg), str(tmp_path / "schedules.json"))  # redacted
    restore_bundle(bundle, str(cfg), str(tmp_path / "schedules.json"))
    restored = json.loads(cfg.read_text())
    assert restored["secret_key"] == "supersecret"
    assert restored["mqtt"]["password"] == "brokerpass"


@pytest.mark.parametrize(
    "imported",
    [
        {"host": "10.0.0.66", "port": 1883, "username": "u"},
        {"host": "10.0.0.5", "port": 8883, "username": "u"},
        {"host": "10.0.0.5", "port": 1883, "username": "someone"},
        {"host": "10.0.0.5", "port": 1883},
    ],
)
def test_restore_bundle_drops_stored_password_for_a_different_broker(tmp_path, imported):
    # An imported file must not redirect the stored password to another broker.
    cfg = tmp_path / "config.json"
    _write(cfg, {"mqtt": {"host": "10.0.0.5", "port": 1883, "username": "u", "password": "p"}})
    restore_bundle(
        {"hearthmage_backup": 1, "config": {"mqtt": imported}},
        str(cfg), str(tmp_path / "schedules.json"),
    )
    restored = json.loads(cfg.read_text())["mqtt"]
    assert "password" not in restored
    assert restored["host"] == imported["host"]


def test_restore_bundle_keeps_password_for_the_same_broker_written_differently(tmp_path):
    cfg = tmp_path / "config.json"
    _write(cfg, {"mqtt": {"host": "10.0.0.5", "username": "u", "password": "p"}})
    restore_bundle(
        {"hearthmage_backup": 1,
         "config": {"mqtt": {"host": " 10.0.0.5 ", "port": "1883", "username": "u "}}},
        str(cfg), str(tmp_path / "schedules.json"),
    )
    assert json.loads(cfg.read_text())["mqtt"]["password"] == "p"


def test_restore_bundle_does_not_inject_mqtt_when_bundle_has_none(tmp_path):
    cfg = tmp_path / "config.json"
    _write(cfg, {"hub_ip": "10.0.0.9", "mqtt": {"host": "10.0.0.5", "password": "p"}})
    restore_bundle(
        {"hearthmage_backup": 1, "config": {"hub_ip": "10.0.0.8"}},
        str(cfg), str(tmp_path / "schedules.json"),
    )
    assert "mqtt" not in json.loads(cfg.read_text())


def test_restore_bundle_rejects_invalid_schedules_without_writing(tmp_path):
    cfg = tmp_path / "config.json"
    sched = tmp_path / "schedules.json"
    _write(cfg, {"hub_ip": "old"})
    _write(sched, {"1": {}})
    for bad in (
        {"1": {"0": [[200, 420, 540]]}},  # out-of-range target
        {"__active__": {"0": [[20, 0, 60]]}},  # junk in the reserved key
        {"1": {"0": [[20, "a", 5]]}},
    ):
        with pytest.raises(ValueError):
            restore_bundle(
                {"hearthmage_backup": 1, "config": {"hub_ip": "10.0.0.2"}, "schedules": bad},
                str(cfg),
                str(sched),
            )
    assert json.loads(cfg.read_text())["hub_ip"] == "old"  # nothing half-restored
    assert json.loads(sched.read_text()) == {"1": {}}


def test_snapshot_is_private_like_the_file_it_copies(tmp_path):
    # config.json holds the session secret and the MQTT password, so a snapshot
    # must not be readable by other local users.
    import os
    import stat

    from hearthmage.atomicio import write_json_atomic
    from hearthmage.backups import rotate_backup

    src = tmp_path / "config.json"
    write_json_atomic(str(src), {"secret_key": "s3cret"})
    snap = rotate_backup(str(src))
    assert snap is not None
    assert stat.S_IMODE(os.stat(snap).st_mode) & 0o077 == 0
    assert Path(snap).read_bytes() == src.read_bytes()


def test_snapshot_leaves_no_partial_file_behind(tmp_path, monkeypatch):
    import os

    import hearthmage.backups as backups
    from hearthmage.atomicio import write_json_atomic

    src = tmp_path / "config.json"
    write_json_atomic(str(src), {"a": 1})

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backups.os, "replace", boom)
    try:
        backups.rotate_backup(str(src))
    except OSError:
        pass
    assert os.listdir(tmp_path / "backups") == []


def test_older_snapshots_of_a_private_file_are_tightened(tmp_path):
    # Snapshots written before the fix are world-readable copies of a 0600
    # file. They are fixed the next time a snapshot is taken, not left until
    # ten more saves have pushed them out.
    import os
    import stat

    from hearthmage.atomicio import write_json_atomic
    from hearthmage.backups import list_backups, rotate_backup

    src = tmp_path / "config.json"
    write_json_atomic(str(src), {"secret_key": "s3cret"})
    old = tmp_path / "backups" / "config.json.20260101T000000_000000.bak.json"
    old.parent.mkdir()
    old.write_text("{}")
    os.chmod(old, 0o644)
    rotate_backup(str(src))
    for snap in list_backups(str(src)):
        assert stat.S_IMODE(os.stat(snap).st_mode) & 0o077 == 0, snap


def test_snapshots_of_a_shared_file_are_left_as_they_are(tmp_path):
    # If the file itself is readable by others (a share that manages access its
    # own way), that is the owner's choice; do not lock its old snapshots.
    import os
    import stat

    from hearthmage.backups import rotate_backup

    src = tmp_path / "schedules.json"
    src.write_text("{}")
    os.chmod(src, 0o644)
    old = tmp_path / "backups" / "schedules.json.20260101T000000_000000.bak.json"
    old.parent.mkdir()
    old.write_text("{}")
    os.chmod(old, 0o644)
    rotate_backup(str(src))
    assert stat.S_IMODE(os.stat(old).st_mode) == 0o644


@pytest.mark.parametrize(
    "config",
    [
        {"mqtt": {"host": "10.0.0.5", "port": float("inf")}},
        {"mqtt": {"host": "10.0.0.5", "port": float("nan")}},
        {"mqtt": {"host": "10.0.0.5", "port": 70000}},
        {"mqtt": {"host": "10.0.0.5", "port": "x"}},
        {"hub_ip": "10.0.0.20", "hub_port": float("inf")},
        {"hub_ip": "10.0.0.20", "hub_port": 0},
    ],
)
def test_restore_bundle_rejects_an_unusable_port_without_writing(tmp_path, config):
    # json accepts Infinity; int() of it raised OverflowError on every page.
    cfg = tmp_path / "config.json"
    _write(cfg, {"hub_ip": "old"})
    with pytest.raises(ValueError):
        restore_bundle({"hearthmage_backup": 1, "config": config},
                       str(cfg), str(tmp_path / "schedules.json"))
    assert json.loads(cfg.read_text()) == {"hub_ip": "old"}


def test_restore_bundle_accepts_usual_port_forms(tmp_path):
    cfg = tmp_path / "config.json"
    for config in ({"hub_port": 6653, "mqtt": {"host": "h", "port": "1883"}},
                   {"mqtt": {"host": "h"}}, {"mqtt": {"host": "h", "port": None}}):
        restore_bundle({"hearthmage_backup": 1, "config": config},
                       str(cfg), str(tmp_path / "schedules.json"))


@pytest.mark.parametrize(
    "config",
    [
        {"hub_ip": "8.8.8.8"},
        {"hub_ip": "100.64.0.7"},  # Tailscale is not the home LAN
        {"hub_ip": "nexho.local"},  # the hub is an IPv4 literal, not a name
        {"hub_ip": 167772161},
        {"hub_ip": ["10.0.0.2"]},
        {"mqtt": {"host": "mqtt.example.com"}},
        {"mqtt": {"host": "2001:db8::1"}},
        {"mqtt": {"host": "134744072"}},
        {"mqtt": {"host": {"a": 1}}},
        {"hub_ip": "10.0.0.2", "mqtt": {"host": "100.64.0.9"}},
    ],
)
def test_restore_bundle_rejects_an_address_off_the_lan_without_writing(tmp_path, config):
    # An import is untrusted input: it must not point the hub's UDP or the
    # broker's TCP connection outside the LAN.
    from hearthmage.lan import AddressRejected

    cfg = tmp_path / "config.json"
    sched = tmp_path / "schedules.json"
    _write(cfg, {"hub_ip": "10.0.0.1"})
    with pytest.raises(AddressRejected):
        restore_bundle({"hearthmage_backup": 1, "config": config, "schedules": {"9": {}}},
                       str(cfg), str(sched))
    assert json.loads(cfg.read_text()) == {"hub_ip": "10.0.0.1"}
    assert not sched.exists()  # the whole import is refused, schedules too


def test_restore_bundle_accepts_lan_addresses_and_unset_ones(tmp_path):
    cfg = tmp_path / "config.json"
    for config in (
        {"hub_ip": "192.168.1.20", "mqtt": {"host": "mqtt.local"}},
        {"hub_ip": "172.16.0.9", "mqtt": {"host": "localhost"}},
        {"hub_ip": "", "mqtt": {"host": ""}},
        {"hub_ip": None, "mqtt": {}},
    ):
        restore_bundle({"hearthmage_backup": 1, "config": config},
                       str(cfg), str(tmp_path / "schedules.json"))
        assert json.loads(cfg.read_text()) == config


@pytest.mark.parametrize(
    "config",
    [
        {"mqtt": {"host": "10.0.0.5", "base_topic": "a/#"}},
        {"mqtt": {"host": "10.0.0.5", "base_topic": "/a"}},
        {"mqtt": {"host": "10.0.0.5", "base_topic": 5}},
        {"zone_names": {"1": "x" * 41}},
        {"zone_names": {"1": "bad\x00"}},
        {"zone_names": {"abc": "Kitchen"}},
        {"zone_names": {"1": 5}},
        {"zone_names": ["Kitchen"]},
        {"price_per_kwh": -1},
        {"price_per_kwh": float("inf")},
        {"price_per_kwh": float("nan")},
        {"price_per_kwh": "abc"},
        {"price_per_kwh": True},
    ],
)
def test_restore_rejects_bad_config_values_and_writes_nothing(tmp_path, config):
    cfg, sched = tmp_path / "config.json", tmp_path / "schedules.json"
    _write(cfg, {"hub_ip": "10.0.0.20"})
    bundle = {"hearthmage_backup": 1, "config": config, "schedules": {"1": {"0": [[20, 0, 600]]}}}
    with pytest.raises(ValueError):
        restore_bundle(bundle, str(cfg), str(sched))
    assert json.loads(cfg.read_text()) == {"hub_ip": "10.0.0.20"}
    assert not sched.exists()


def test_restore_accepts_good_topic_names_and_price(tmp_path):
    cfg, sched = tmp_path / "config.json", tmp_path / "schedules.json"
    config = {"mqtt": {"host": "10.0.0.5", "base_topic": "home/heat"},
              "zone_names": {"1": "Kitchen"}, "price_per_kwh": 0.3}
    restore_bundle({"hearthmage_backup": 1, "config": config}, str(cfg), str(sched))
    assert json.loads(cfg.read_text())["zone_names"] == {"1": "Kitchen"}


def test_restore_writes_the_validated_stripped_names(tmp_path):
    cfg, sched = tmp_path / "config.json", tmp_path / "schedules.json"
    config = {"zone_names": {"01": "  Kitchen  ", "2": "   "}}
    restore_bundle({"hearthmage_backup": 1, "config": config}, str(cfg), str(sched))
    assert json.loads(cfg.read_text())["zone_names"] == {"1": "Kitchen"}


def test_restore_error_names_the_failing_zone(tmp_path):
    config = {"zone_names": {"3": "x" * 41}}
    with pytest.raises(ValueError, match="Zone 3"):
        restore_bundle({"hearthmage_backup": 1, "config": config},
                       str(tmp_path / "c.json"), str(tmp_path / "s.json"))


def test_restore_rejects_a_lone_surrogate_name(tmp_path):
    config = {"zone_names": {"1": "\ud800"}}
    with pytest.raises(ValueError):
        restore_bundle({"hearthmage_backup": 1, "config": config},
                       str(tmp_path / "c.json"), str(tmp_path / "s.json"))
