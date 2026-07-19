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
        {"hearthmage_backup": 1, "config": {"hub_ip": "new"}, "schedules": {"9": {}}},
        str(cfg),
        str(sched),
    )
    assert json.loads(cfg.read_text())["hub_ip"] == "new"
    # the pre-restore config is recoverable from a backup
    assert list_backups(str(cfg))  # at least one snapshot of the old file


def test_restore_bundle_rejects_foreign_payload(tmp_path):
    with pytest.raises(ValueError):
        restore_bundle({"not": "a bundle"}, str(tmp_path / "c.json"), str(tmp_path / "s.json"))


def test_restore_bundle_skips_absent_sections(tmp_path):
    cfg = tmp_path / "config.json"
    sched = tmp_path / "schedules.json"
    _write(sched, {"1": {}})
    restore_bundle({"hearthmage_backup": 1, "config": {"hub_ip": "x"}}, str(cfg), str(sched))
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


def test_restore_bundle_does_not_inject_mqtt_when_bundle_has_none(tmp_path):
    cfg = tmp_path / "config.json"
    _write(cfg, {"hub_ip": "10.0.0.9", "mqtt": {"host": "10.0.0.5", "password": "p"}})
    restore_bundle(
        {"hearthmage_backup": 1, "config": {"hub_ip": "10.0.0.8"}},
        str(cfg), str(tmp_path / "schedules.json"),
    )
    assert "mqtt" not in json.loads(cfg.read_text())
