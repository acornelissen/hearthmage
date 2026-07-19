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
    Settings(path, env={}).set_mqtt("10.0.0.7", 1883, "u", "keep", "")
    assert Settings(path, env={}).mqtt_config()["password"] == "sekret"


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
