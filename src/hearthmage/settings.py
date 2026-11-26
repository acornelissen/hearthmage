from __future__ import annotations

import json
import os
import secrets
import threading
from typing import Mapping

from hearthmage.atomicio import write_json_atomic
from hearthmage.backups import rotate_backup

_DEFAULT_HUB_PORT = 6653
_DEFAULT_CONFIG = os.path.expanduser("~/.config/hearthmage/config.json")


def _env_lookup(env: Mapping[str, str], suffix: str, default: str | None = None) -> str | None:
    """Read a ``HEARTHMAGE_<suffix>`` environment variable."""
    value = env.get(f"HEARTHMAGE_{suffix}")
    return value if value is not None else default


def config_path(env: Mapping[str, str] = os.environ) -> str:
    return _env_lookup(env, "CONFIG_FILE", _DEFAULT_CONFIG)


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def parse_port(raw) -> int | None:
    """A port number from the config file, the environment or a form, or None
    if it is unusable. json accepts Infinity and NaN, which int() cannot take."""
    try:
        port = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return port if 1 <= port <= 65535 else None


def mqtt_broker(host, port, username) -> tuple:
    """The broker a stored password belongs to, as (host, port, username),
    normalised so blank, padded and string values compare equal. A password
    is only ever sent to the broker it was entered for."""
    host = host.strip() if isinstance(host, str) else ""
    username = username.strip() if isinstance(username, str) else ""
    return host, parse_port(port) or 1883, username


class MqttPasswordReentry(ValueError):
    """Keeping the stored broker password would send it to a different broker."""


def _parse_zone_names(raw: str | None) -> dict[str, str]:
    names: dict[str, str] = {}
    for pair in (raw or "").split(","):
        zid, sep, name = pair.partition(":")
        if sep and zid.strip() and name.strip():
            names[zid.strip()] = name.strip()
    return names


class Settings:
    """Persistent, env-seeded app settings (hub address + zone names).

    A JSON file is the source of truth once written; environment variables
    provide initial defaults so the app can also be configured headless. The UI
    edits the file via ``set_hub`` / ``set_zone_name``.

    Routes run on a thread pool, so every read and write of ``_data`` holds
    ``_lock`` (reentrant: readers call each other). A write holds it across
    its checks, the change and the save.
    """

    def __init__(self, path: str, env: Mapping[str, str] = os.environ) -> None:
        self._path = path
        self._env = env
        self._data: dict = {}
        self._lock = threading.RLock()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    self._data = loaded
            except (OSError, ValueError):
                self._data = {}

    # --- read ---------------------------------------------------------

    @property
    def hub_ip(self) -> str | None:
        with self._lock:
            value = self._data.get("hub_ip") or _env_lookup(self._env, "BASE_IP")
        value = value.strip() if isinstance(value, str) else None
        return value or None

    @property
    def hub_port(self) -> int:
        with self._lock:
            raw = self._data.get("hub_port")
        raw = raw or _env_lookup(self._env, "HUB_PORT") or _DEFAULT_HUB_PORT
        return parse_port(raw) or _DEFAULT_HUB_PORT

    @property
    def zone_names(self) -> dict[str, str]:
        names = _parse_zone_names(_env_lookup(self._env, "ZONE_NAMES"))
        with self._lock:
            stored = self._data.get("zone_names")
            if isinstance(stored, dict):
                names.update({str(k): str(v) for k, v in stored.items()})
        return names

    @property
    def use_fake(self) -> bool:
        return _truthy(_env_lookup(self._env, "FAKE"))

    @property
    def bind_host(self) -> str:
        return _env_lookup(self._env, "BIND", "0.0.0.0")

    @property
    def port(self) -> int:
        try:
            return int(_env_lookup(self._env, "PORT", "8080"))
        except ValueError:
            return 8080

    @property
    def config_file(self) -> str:
        """Path to the JSON config file this instance reads and writes."""
        return self._path

    @property
    def cache_path(self) -> str:
        """Where last-known readings are persisted (next to the config file)."""
        directory = os.path.dirname(self._path) or "."
        return os.path.join(directory, "readings.json")

    @property
    def schedules_path(self) -> str:
        """Where weekly schedule definitions are persisted (next to the config)."""
        directory = os.path.dirname(self._path) or "."
        return os.path.join(directory, "schedules.json")

    @property
    def energy_path(self) -> str:
        """Where last-known energy readings are cached (next to the config)."""
        directory = os.path.dirname(self._path) or "."
        return os.path.join(directory, "energy.json")

    @property
    def history_path(self) -> str:
        """SQLite time-series history database (next to the config)."""
        directory = os.path.dirname(self._path) or "."
        return os.path.join(directory, "history.db")

    @property
    def sync_path(self) -> str:
        """Where per-zone schedule-sync status is persisted (next to the config)."""
        directory = os.path.dirname(self._path) or "."
        return os.path.join(directory, "sync.json")

    @property
    def holiday_path(self) -> str:
        """Where holiday-hold records are persisted (next to the config)."""
        directory = os.path.dirname(self._path) or "."
        return os.path.join(directory, "holiday.json")

    @property
    def price_per_kwh(self) -> float:
        """Electricity unit price for cost estimates (0.0 if unset)."""
        with self._lock:
            raw = self._data.get("price_per_kwh")
        if raw is None:
            raw = _env_lookup(self._env, "PRICE_PER_KWH")
        try:
            return max(0.0, float(raw)) if raw is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _mqtt_stored(self) -> dict | None:
        """The stored mqtt object, or None if the file has none. Writers replace
        the object whole, so a caller holding it sees one consistent save."""
        with self._lock:
            stored = self._data.get("mqtt")
        return stored if isinstance(stored, dict) else None

    @staticmethod
    def _clean(value) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def mqtt_enabled(self) -> bool:
        return self.mqtt_config() is not None

    @property
    def mqtt_password_set(self) -> bool:
        """Whether a broker password is configured. The password value itself
        is never handed to routes or templates."""
        with self._lock:
            stored = self._mqtt_stored()
            if stored is not None:
                return bool(stored.get("password"))
            return bool(_env_lookup(self._env, "MQTT_PASSWORD"))

    def mqtt_password_needs_reentry(self, host, port, username) -> bool:
        """Whether keeping the current password for this host, port and
        username would send it to a different broker than it was entered for.
        The current broker comes from the same source as ``mqtt_config``."""
        with self._lock:
            if not self.mqtt_password_set:
                return False  # nothing stored, nothing to leak
            stored = self._mqtt_stored()
            if stored is not None:
                current = mqtt_broker(stored.get("host"), stored.get("port"), stored.get("username"))
            else:
                current = mqtt_broker(
                    _env_lookup(self._env, "MQTT_HOST"),
                    _env_lookup(self._env, "MQTT_PORT"),
                    _env_lookup(self._env, "MQTT_USERNAME"),
                )
            return mqtt_broker(host, port, username) != current

    def mqtt_config(self) -> dict | None:
        """MQTT broker settings, or None if not enabled.

        Once the file holds an ``mqtt`` object it is authoritative wholesale:
        every key is read from it alone (missing keys mean their defaults), so
        clearing a value in the UI is final even with ``HEARTHMAGE_MQTT_*``
        exported. Env vars apply only when no ``mqtt`` object is stored.
        ``node_id`` stays env-only."""
        stored = self._mqtt_stored()  # one object, replaced whole by writers
        if stored is not None:
            host = self._clean(stored.get("host"))
            raw_port = stored.get("port")
            username = self._clean(stored.get("username"))
            password = stored.get("password") or None
            base_topic = self._clean(stored.get("base_topic"))
        else:
            host = self._clean(_env_lookup(self._env, "MQTT_HOST"))
            raw_port = _env_lookup(self._env, "MQTT_PORT")
            username = self._clean(_env_lookup(self._env, "MQTT_USERNAME"))
            password = _env_lookup(self._env, "MQTT_PASSWORD") or None
            base_topic = self._clean(_env_lookup(self._env, "MQTT_BASE_TOPIC"))
        if not host:
            return None
        return {
            "host": host,
            "port": parse_port(raw_port) or 1883,
            "username": username,
            "password": password,
            "base_topic": base_topic or "hearthmage",
            "node_id": _env_lookup(self._env, "MQTT_NODE_ID", "hub"),
        }

    @property
    def auth_password(self) -> str | None:
        """Shared secret that gates the UI, or None (open) if unset. Env only."""
        value = _env_lookup(self._env, "PASSWORD")
        return value or None

    @property
    def allowed_hosts(self) -> str:
        """Extra host names the app answers to, comma-separated. Env only."""
        return _env_lookup(self._env, "ALLOWED_HOSTS", "")

    @property
    def trusted_proxies(self) -> str:
        """Proxy IPs/CIDRs whose X-Forwarded-Host/-Proto count. Env only."""
        return _env_lookup(self._env, "TRUSTED_PROXIES", "")

    @property
    def auth_enabled(self) -> bool:
        return self.auth_password is not None

    def secret_key(self) -> str:
        """Key used to sign the session cookie; generated and persisted once."""
        with self._lock:
            key = _env_lookup(self._env, "SECRET_KEY") or self._data.get("secret_key")
            if not key:
                key = secrets.token_hex(32)
                self._data["secret_key"] = key
                self._save()
            return key

    def is_configured(self) -> bool:
        return bool(self.hub_ip) or self.use_fake

    # --- write --------------------------------------------------------

    def set_hub(self, hub_ip: str, hub_port: int | None = None) -> None:
        with self._lock:
            self._data["hub_ip"] = hub_ip.strip()
            if hub_port is not None:
                self._data["hub_port"] = int(hub_port)
            self._save()

    def set_price(self, price_per_kwh: float) -> None:
        with self._lock:
            self._data["price_per_kwh"] = max(0.0, float(price_per_kwh))
            self._save()

    def set_zone_name(self, zone_id: str, name: str) -> None:
        zone_id = str(zone_id)
        name = (name or "").strip()
        with self._lock:
            names = self._data.setdefault("zone_names", {})
            if name:
                names[zone_id] = name
            else:
                names.pop(zone_id, None)  # blank clears back to the default label
            self._save()

    def set_mqtt(
        self,
        host: str,
        port: int,
        username: str,
        password_action: str,
        password: str,
        base_topic: str = "hearthmage",
    ) -> None:
        """Save MQTT broker settings. ``password_action`` is ``"set"`` (store the
        given password), ``"keep"`` (leave the stored one alone; blank UI input)
        or ``"clear"`` (remove it). ``"keep"`` for a different host, port or
        username raises MqttPasswordReentry before anything changes: the
        password has to be entered again for the new broker. The check, the
        change and the save happen under one hold of the lock, and the new
        object replaces the old one in a single assignment."""
        with self._lock:
            if password_action == "keep" and self.mqtt_password_needs_reentry(host, port, username):
                raise MqttPasswordReentry("enter the broker password again for a different broker")
            stored = self._mqtt_stored()
            if stored is not None:
                kept_password = stored.get("password")
            else:
                # First save from the UI: the environment was in charge until
                # now, so "keep" has to mean keep the environment's password.
                # From here on the stored object is authoritative.
                kept_password = _env_lookup(self._env, "MQTT_PASSWORD")
            mqtt = {
                "host": host.strip(),
                "port": parse_port(port) or 1883,
                "username": (username or "").strip(),
                "base_topic": (base_topic or "").strip() or "hearthmage",
            }
            if password_action == "set":
                mqtt["password"] = password
            elif password_action == "keep" and kept_password:
                mqtt["password"] = kept_password
            # "clear": the new object simply has no password.
            self._data["mqtt"] = mqtt
            self._save()

    def clear_mqtt(self) -> None:
        """Disable the bridge and forget the stored broker settings (password
        too). An empty object is kept on purpose: it stays authoritative, so
        ``HEARTHMAGE_MQTT_*`` in the environment cannot switch the bridge back
        on behind a UI that says it is stopped."""
        with self._lock:
            self._data["mqtt"] = {}
            self._save()

    def _save(self) -> None:
        """Write ``_data`` out. Callers hold ``_lock``."""
        write_json_atomic(self._path, self._data)
        try:
            rotate_backup(self._path)  # keep a timestamped copy of every change
        except OSError:
            pass  # a failed backup must not fail the save
