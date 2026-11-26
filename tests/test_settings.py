import pytest

from hearthmage.settings import Settings


def test_env_seeds_when_no_file(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={"HEARTHMAGE_BASE_IP": "10.0.0.20"})
    assert s.hub_ip == "10.0.0.20"
    assert s.hub_port == 6653
    assert s.is_configured()


def test_not_configured_without_ip(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={})
    assert s.hub_ip is None
    assert not s.is_configured()


def test_fake_mode_counts_as_configured(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={"HEARTHMAGE_FAKE": "1"})
    assert s.is_configured()


def test_set_hub_persists_across_reload(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_hub("10.0.0.50", 6653)
    reloaded = Settings(path, env={})
    assert reloaded.hub_ip == "10.0.0.50"
    assert reloaded.hub_port == 6653
    assert reloaded.is_configured()


def test_saved_file_overrides_env(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_hub("10.0.0.99")
    s = Settings(path, env={"HEARTHMAGE_BASE_IP": "10.0.0.1"})
    assert s.hub_ip == "10.0.0.99"  # explicit UI value wins over env default


def test_zone_names_merge_env_and_file(tmp_path):
    path = str(tmp_path / "c.json")
    s = Settings(path, env={"HEARTHMAGE_ZONE_NAMES": "1:Lounge,2:Bedroom"})
    assert s.zone_names == {"1": "Lounge", "2": "Bedroom"}
    s.set_zone_name("2", "Master Bedroom")
    s.set_zone_name("3", "Kitchen")
    reloaded = Settings(path, env={"HEARTHMAGE_ZONE_NAMES": "1:Lounge,2:Bedroom"})
    assert reloaded.zone_names == {"1": "Lounge", "2": "Master Bedroom", "3": "Kitchen"}


def test_blank_name_clears_it(tmp_path):
    path = str(tmp_path / "c.json")
    s = Settings(path, env={})
    s.set_zone_name("1", "Lounge")
    s.set_zone_name("1", "  ")
    assert "1" not in Settings(path, env={}).zone_names


def test_price_defaults_to_zero_and_persists(tmp_path):
    path = str(tmp_path / "c.json")
    assert Settings(path, env={}).price_per_kwh == 0.0
    Settings(path, env={}).set_price(0.28)
    assert Settings(path, env={}).price_per_kwh == 0.28


def test_price_seeded_from_env(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={"HEARTHMAGE_PRICE_PER_KWH": "0.31"})
    assert s.price_per_kwh == 0.31


def test_bad_price_falls_back_to_zero(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={"HEARTHMAGE_PRICE_PER_KWH": "free"})
    assert s.price_per_kwh == 0.0


def test_auth_disabled_without_password(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={})
    assert s.auth_enabled is False
    assert s.auth_password is None


def test_auth_enabled_with_password(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={"HEARTHMAGE_PASSWORD": "swordfish"})
    assert s.auth_enabled is True
    assert s.auth_password == "swordfish"


def test_secret_key_is_generated_and_persisted(tmp_path):
    path = str(tmp_path / "c.json")
    key = Settings(path, env={}).secret_key()
    assert len(key) >= 32
    assert Settings(path, env={}).secret_key() == key  # stable across reloads


def test_mqtt_disabled_without_host(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={})
    assert s.mqtt_enabled is False
    assert s.mqtt_config() is None


def test_mqtt_config_from_env(tmp_path):
    s = Settings(
        str(tmp_path / "c.json"),
        env={
            "HEARTHMAGE_MQTT_HOST": "10.0.0.5",
            "HEARTHMAGE_MQTT_PORT": "8883",
            "HEARTHMAGE_MQTT_USERNAME": "hearth",
            "HEARTHMAGE_MQTT_BASE_TOPIC": "home/heat",
        },
    )
    assert s.mqtt_enabled is True
    cfg = s.mqtt_config()
    assert cfg["host"] == "10.0.0.5"
    assert cfg["port"] == 8883
    assert cfg["username"] == "hearth"
    assert cfg["password"] is None
    assert cfg["base_topic"] == "home/heat"


def test_mqtt_file_overrides_env(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt(
        "10.0.0.6", 8883, "filer", "set", "sekret", base_topic="attic/heat"
    )
    s = Settings(
        path,
        env={
            "HEARTHMAGE_MQTT_HOST": "10.0.0.5",
            "HEARTHMAGE_MQTT_PORT": "1883",
            "HEARTHMAGE_MQTT_USERNAME": "enver",
            "HEARTHMAGE_MQTT_PASSWORD": "envpass",
            "HEARTHMAGE_MQTT_BASE_TOPIC": "home/heat",
        },
    )
    cfg = s.mqtt_config()
    assert cfg["host"] == "10.0.0.6"
    assert cfg["port"] == 8883
    assert cfg["username"] == "filer"
    assert cfg["password"] == "sekret"
    assert cfg["base_topic"] == "attic/heat"
    assert cfg["node_id"] == "hub"  # node_id stays env-only


def test_mqtt_enabled_from_file(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "", "keep", "")
    assert Settings(path, env={}).mqtt_enabled is True


def test_mqtt_blank_password_keeps_stored(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "u", "set", "sekret")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "u", "keep", "", base_topic="attic")
    cfg = Settings(path, env={}).mqtt_config()
    assert cfg["password"] == "sekret"
    assert cfg["base_topic"] == "attic"


@pytest.mark.parametrize(
    "host, port, username",
    [("10.0.0.7", 1883, "u"), ("10.0.0.6", 8883, "u"), ("10.0.0.6", 1883, "other")],
)
def test_keeping_the_password_for_a_different_broker_is_refused(tmp_path, host, port, username):
    # The stored password must never be sent in a plaintext CONNECT to a broker
    # it was not entered for: a changed host, port or username needs re-entry.
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "u", "set", "sekret")
    s = Settings(path, env={})
    assert s.mqtt_password_needs_reentry(host, port, username) is True
    with pytest.raises(ValueError):
        s.set_mqtt(host, port, username, "keep", "")
    cfg = Settings(path, env={}).mqtt_config()
    assert (cfg["host"], cfg["port"], cfg["username"], cfg["password"]) == (
        "10.0.0.6", 1883, "u", "sekret"
    )


def test_same_broker_after_trimming_needs_no_reentry(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "u", "set", "sekret")
    assert Settings(path, env={}).mqtt_password_needs_reentry(" 10.0.0.6 ", "1883", " u ") is False


def test_a_different_broker_with_a_new_or_cleared_password_is_saved(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "u", "set", "sekret")
    Settings(path, env={}).set_mqtt("10.0.0.7", 1883, "u", "set", "fresh")
    assert Settings(path, env={}).mqtt_config()["password"] == "fresh"
    Settings(path, env={}).set_mqtt("10.0.0.8", 1883, "u", "clear", "")
    cfg = Settings(path, env={}).mqtt_config()
    assert (cfg["host"], cfg["password"]) == ("10.0.0.8", None)


def test_a_different_broker_with_no_stored_password_needs_no_reentry(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "", "keep", "")
    s = Settings(path, env={})
    assert s.mqtt_password_needs_reentry("10.0.0.7", 1884, "new") is False
    s.set_mqtt("10.0.0.7", 1884, "new", "keep", "")
    assert Settings(path, env={}).mqtt_config()["host"] == "10.0.0.7"


def test_stored_password_without_a_host_is_not_kept_for_a_new_host(tmp_path):
    path = tmp_path / "c.json"
    import json as _json
    path.write_text(_json.dumps({"mqtt": {"password": "sekret"}}))
    s = Settings(str(path), env={})
    with pytest.raises(ValueError):
        s.set_mqtt("10.0.0.7", 1883, "", "keep", "")


def test_mqtt_clear_action_removes_password(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "u", "set", "sekret")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "u", "clear", "")
    s = Settings(path, env={})
    assert s.mqtt_config()["password"] is None
    assert s.mqtt_password_set is False


def test_mqtt_password_set_flag(tmp_path):
    path = str(tmp_path / "c.json")
    s = Settings(path, env={})
    assert s.mqtt_password_set is False
    s.set_mqtt("10.0.0.6", 1883, "u", "set", "sekret")
    assert Settings(path, env={}).mqtt_password_set is True


def test_mqtt_password_set_from_env(tmp_path):
    s = Settings(str(tmp_path / "c.json"), env={"HEARTHMAGE_MQTT_PASSWORD": "envpass"})
    assert s.mqtt_password_set is True


def test_clear_mqtt_disables_bridge(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "u", "set", "sekret")
    Settings(path, env={}).clear_mqtt()
    s = Settings(path, env={})
    assert s.mqtt_enabled is False
    assert s.mqtt_config() is None


_ENV_BROKER = {
    "HEARTHMAGE_MQTT_HOST": "10.0.0.5",
    "HEARTHMAGE_MQTT_USERNAME": "hearth",
    "HEARTHMAGE_MQTT_PASSWORD": "envpass",
}


def test_first_save_with_blank_password_keeps_the_env_password(tmp_path):
    # The form says "(set) leave blank to keep". With the broker configured by
    # environment variables, the first save from the UI must not drop the
    # password, or the bridge reconnects unauthenticated and is refused.
    path = str(tmp_path / "c.json")
    Settings(path, env=_ENV_BROKER).set_mqtt("10.0.0.5", 1883, "hearth", "keep", "", base_topic="home")
    cfg = Settings(path, env=_ENV_BROKER).mqtt_config()
    assert cfg["password"] == "envpass"
    assert cfg["base_topic"] == "home"


def test_first_save_for_a_different_broker_does_not_carry_the_env_password(tmp_path):
    path = str(tmp_path / "c.json")
    s = Settings(path, env=_ENV_BROKER)
    assert s.mqtt_password_needs_reentry("10.0.0.99", 1883, "hearth") is True
    with pytest.raises(ValueError):
        s.set_mqtt("10.0.0.99", 1883, "hearth", "keep", "")
    assert s.mqtt_config()["host"] == "10.0.0.5"  # nothing changed in memory
    assert Settings(path, env={}).mqtt_config() is None  # and nothing was written


def test_clearing_the_password_is_final_even_with_an_env_password(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env=_ENV_BROKER).set_mqtt("10.0.0.5", 1883, "hearth", "clear", "")
    s = Settings(path, env=_ENV_BROKER)
    assert s.mqtt_config()["password"] is None
    # ...and a later blank-password save keeps it cleared rather than re-reading env.
    s.set_mqtt("10.0.0.5", 1883, "hearth", "keep", "")
    assert Settings(path, env=_ENV_BROKER).mqtt_config()["password"] is None


def test_disable_stays_disabled_when_env_configures_a_broker(tmp_path):
    # Disable used to drop the stored object, which handed control straight
    # back to the environment: the bridge reconnected while the UI said stopped.
    path = str(tmp_path / "c.json")
    Settings(path, env=_ENV_BROKER).clear_mqtt()
    s = Settings(path, env=_ENV_BROKER)
    assert s.mqtt_enabled is False
    assert s.mqtt_config() is None
    assert s.mqtt_password_set is False
    # Saving the form again turns it back on.
    s.set_mqtt("10.0.0.6", 1883, "", "keep", "")
    assert Settings(path, env=_ENV_BROKER).mqtt_config()["host"] == "10.0.0.6"


def test_mqtt_bad_port_falls_back(tmp_path):
    s = Settings(
        str(tmp_path / "c.json"),
        env={"HEARTHMAGE_MQTT_HOST": "10.0.0.5", "HEARTHMAGE_MQTT_PORT": "nope"},
    )
    assert s.mqtt_config()["port"] == 1883


def test_bad_port_env_falls_back(tmp_path):
    s = Settings(
        str(tmp_path / "c.json"),
        env={"HEARTHMAGE_BASE_IP": "10.0.0.20", "HEARTHMAGE_HUB_PORT": "nope"},
    )
    assert s.hub_port == 6653


def test_stored_mqtt_object_is_authoritative(tmp_path):
    """Once an mqtt object exists in the file, env vars are ignored entirely:
    a UI 'clear password' must stick even with HEARTHMAGE_MQTT_* exported."""
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.6", 1883, "", "clear", "")
    s = Settings(
        path,
        env={
            "HEARTHMAGE_MQTT_PASSWORD": "envpass",
            "HEARTHMAGE_MQTT_USERNAME": "enver",
            "HEARTHMAGE_MQTT_PORT": "9999",
            "HEARTHMAGE_MQTT_BASE_TOPIC": "env/topic",
        },
    )
    cfg = s.mqtt_config()
    assert cfg["password"] is None
    assert cfg["username"] is None
    assert cfg["port"] == 1883
    assert cfg["base_topic"] == "hearthmage"
    assert s.mqtt_password_set is False


def test_stored_mqtt_without_host_disables_despite_env_host(tmp_path):
    path = str(tmp_path / "c.json")
    import json as _json
    (tmp_path / "c.json").write_text(_json.dumps({"mqtt": {"port": 1884}}))
    s = Settings(path, env={"HEARTHMAGE_MQTT_HOST": "10.0.0.5"})
    assert s.mqtt_enabled is False
    assert s.mqtt_config() is None


# ---- concurrency: one lock over every read and write of the stored data ------


class _RecordingLock:
    """Wraps the real lock and logs each check/write with the lock depth, plus
    every full release, so a test can see whether a step ran under the lock."""

    def __init__(self, inner):
        self.inner, self.depth, self.log = inner, 0, []

    def __enter__(self):
        self.inner.acquire()
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        if self.depth == 0:
            self.log.append("released")
        self.inner.release()


def _record(s, monkeypatch):
    import hearthmage.settings as settings_mod

    lock = s._lock = _RecordingLock(s._lock)
    check = s.mqtt_password_needs_reentry

    def recorded_check(*args):
        lock.log.append(("check", lock.depth))
        return check(*args)

    s.mqtt_password_needs_reentry = recorded_check
    write = settings_mod.write_json_atomic

    def recorded_write(*args):
        lock.log.append(("write", lock.depth))
        return write(*args)

    monkeypatch.setattr(settings_mod, "write_json_atomic", recorded_write)
    return lock


def test_set_mqtt_checks_and_writes_under_one_hold_of_the_lock(tmp_path, monkeypatch):
    # Two saves interleaving between the re-entry check and the field writes
    # used to leave an attacker's host with the owner's new password.
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.5", 1883, "", "keep", "")
    s = Settings(path, env={})
    lock = _record(s, monkeypatch)
    s.set_mqtt("10.0.0.5", 1883, "", "keep", "", base_topic="t")
    assert lock.log == [("check", 1), ("write", 1), "released"]


def test_set_mqtt_with_a_new_password_writes_under_the_lock(tmp_path, monkeypatch):
    s = Settings(str(tmp_path / "c.json"), env={})
    lock = _record(s, monkeypatch)
    s.set_mqtt("10.0.0.9", 1883, "", "set", "S")
    assert lock.log == [("write", 1), "released"]


def test_set_mqtt_replaces_the_mqtt_object_in_one_step(tmp_path):
    # A reader holding the old object must never see the new host with the
    # old password: the stored dict is replaced, not edited field by field.
    path = str(tmp_path / "c.json")
    s = Settings(path, env={})
    s.set_mqtt("10.0.0.5", 1883, "u", "set", "old")
    before = s._data["mqtt"]
    s.set_mqtt("10.0.0.9", 1883, "u", "set", "new")
    assert before == {"host": "10.0.0.5", "port": 1883, "username": "u",
                      "base_topic": "hearthmage", "password": "old"}
    assert s._data["mqtt"]["host"] == "10.0.0.9"


def test_readers_and_other_writers_take_the_lock(tmp_path, monkeypatch):
    s = Settings(str(tmp_path / "c.json"), env={})
    lock = _record(s, monkeypatch)
    calls = [
        lambda: s.mqtt_config(), lambda: s.mqtt_password_set, lambda: s.hub_ip,
        lambda: s.hub_port, lambda: s.zone_names, lambda: s.price_per_kwh,
        lambda: s.secret_key(), lambda: s.set_hub("10.0.0.20"), lambda: s.set_price(0.3),
        lambda: s.set_zone_name("1", "Hall"), lambda: s.clear_mqtt(),
    ]
    for call in calls:
        lock.log.clear()
        call()
        assert "released" in lock.log, call
        assert all(depth >= 1 for _, depth in (e for e in lock.log if e != "released"))


# ---- ports that json accepts but int() cannot take -----------------------------


def test_non_finite_or_out_of_range_ports_fall_back_when_read(tmp_path):
    path = tmp_path / "c.json"
    for bad in ("Infinity", "-Infinity", "NaN", "70000", "0", '"x"'):
        path.write_text(
            '{"hub_ip": "10.0.0.20", "hub_port": %s, "mqtt": {"host": "10.0.0.5", '
            '"port": %s, "password": "p"}}' % (bad, bad)
        )
        s = Settings(str(path), env={})
        assert s.hub_port == 6653, bad
        assert s.mqtt_config()["port"] == 1883, bad
        assert s.mqtt_password_needs_reentry("10.0.0.5", 1883, "") is False, bad


def test_set_mqtt_with_a_non_finite_port_stores_the_default(tmp_path):
    path = str(tmp_path / "c.json")
    Settings(path, env={}).set_mqtt("10.0.0.5", float("inf"), "", "keep", "")
    assert Settings(path, env={}).mqtt_config()["port"] == 1883
