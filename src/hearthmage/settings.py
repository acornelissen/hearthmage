from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import threading
import unicodedata
from typing import Mapping

from hearthmage.atomicio import write_json_atomic
from hearthmage.backups import rotate_backup
from hearthmage.lan import AddressRejected, is_lan_broker_host, is_lan_ipv4

_DEFAULT_HUB_PORT = 6653
_DEFAULT_CONFIG = os.path.expanduser("~/.config/hearthmage/config.json")
_log = logging.getLogger("hearthmage.settings")
_HUB_IP_RULE = (
    "Enter the hub’s IPv4 address on your home network: "
    "10.x.x.x, 172.16–31.x.x or 192.168.x.x."
)
_PORT_RULE = "Enter a port from 1 to 65535."
_BROKER_RULE = (
    "Enter a broker on your home network: a 10.x, 172.16–31.x or 192.168.x "
    "address, localhost, or a local name such as mqtt.local or homeassistant."
)
_DEFAULT_BASE_TOPIC = "hearthmage"
_DEFAULT_NODE_ID = "hub"
_MAX_TOPIC = 64
_MAX_NODE_ID = 32
_MAX_ZONE_NAME = 40
# Segments of [A-Za-z0-9_-] joined by single slashes: no leading or trailing
# slash, no empty segment, and no MQTT wildcard. \Z, not $, so "a\n" fails.
_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
_NODE_ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_ZONE_KEY_RE = re.compile(r"[0-9]{1,3}\Z", re.ASCII)
# Cc controls, Cs surrogates (a lone one cannot be encoded as UTF-8), Co private
# use, Cn unassigned, Zl/Zp line and paragraph separators. Other Cf (ZWJ for
# emoji) stay; the bidi overrides and isolates do not.
_BAD_NAME_CATEGORIES = {"Cc", "Cs", "Co", "Cn", "Zl", "Zp"}
_BIDI_CONTROLS = (
    set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C}
)
_MAX_PRICE = 100.0
_TOPIC_RULE = (
    f"Use up to {_MAX_TOPIC} letters, digits, dashes, underscores and slashes, "
    "with no slash at the start or end and none doubled."
)
_PRICE_RULE = f"Enter a price from 0 to {_MAX_PRICE:g}."


class InvalidSetting(ValueError):
    """A value the app will not store. ``field`` names the form field it came
    from, so the page can mark that field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def valid_base_topic(value) -> bool:
    return (isinstance(value, str) and len(value) <= _MAX_TOPIC
            and _TOPIC_RE.match(value) is not None)


def valid_node_id(value) -> bool:
    return (isinstance(value, str) and len(value) <= _MAX_NODE_ID
            and _NODE_ID_RE.match(value) is not None)


def _bad_name_char(ch: str) -> bool:
    return unicodedata.category(ch) in _BAD_NAME_CATEGORIES or ord(ch) in _BIDI_CONTROLS


def check_zone_key(zone_id) -> str:
    """The zone id as a plain number string ("007" -> "7"), or InvalidSetting."""
    if not isinstance(zone_id, str) or _ZONE_KEY_RE.match(zone_id) is None:
        raise InvalidSetting(f"name_{zone_id}", f"Zone {zone_id!s:.10} is not a zone number (1 to 999).")
    if int(zone_id) < 1:
        raise InvalidSetting(f"name_{zone_id}", f"Zone {zone_id} is not a zone number (1 to 999).")
    return str(int(zone_id))


def check_zone_name(zone_id, name) -> str:
    """The stripped name, or InvalidSetting. A blank name is fine (it clears)."""
    field = f"name_{zone_id}"
    if not isinstance(name, str):
        raise InvalidSetting(field, f"Zone {zone_id}'s name must be text.")
    name = name.strip()
    if len(name) > _MAX_ZONE_NAME:
        raise InvalidSetting(field, f"Zone {zone_id}'s name is longer than {_MAX_ZONE_NAME} characters.")
    if any(_bad_name_char(ch) for ch in name):
        raise InvalidSetting(field, f"Zone {zone_id}'s name has a control or unusable character.")
    return name


def clean_zone_names(names: Mapping) -> dict[str, str]:
    """Every name stripped and every key normalised, or InvalidSetting for the
    first bad one. Blank names stay (they clear a name)."""
    cleaned: dict[str, str] = {}
    for zid, name in names.items():
        key = check_zone_key(zid)
        if key in cleaned:
            raise InvalidSetting(f"name_{zid}", f"Zone {key} appears twice.")
        cleaned[key] = check_zone_name(zid, name)
    return cleaned


def check_price(value) -> float:
    """The price as a float, or InvalidSetting if it is not finite and >= 0."""
    try:
        price = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError, OverflowError):
        price = math.nan
    if not math.isfinite(price) or price < 0 or price > _MAX_PRICE:
        raise InvalidSetting("price_per_kwh", _PRICE_RULE)
    return price + 0.0  # -0.0 becomes 0.0


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
        self._load()
        self._env_names = self._load_env_names()
        self._warn_about_addresses()

    def _load(self) -> None:
        data: dict = {}
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        with self._lock:
            self._data = data

    def reload(self) -> None:
        """Re-read the file into this same object, under its lock. Used after a
        backup restore, so a save racing the restore cannot write old data back."""
        with self._lock:
            self._load()
        self._warn_about_addresses()

    def _load_env_names(self) -> dict[str, str]:
        """HEARTHMAGE_ZONE_NAMES under the same rules as the form; an entry that
        fails is dropped with a warning."""
        names: dict[str, str] = {}
        for zid, name in _parse_zone_names(_env_lookup(self._env, "ZONE_NAMES")).items():
            try:
                names.update(clean_zone_names({zid: name}))
            except InvalidSetting as exc:
                _log.warning("Ignoring HEARTHMAGE_ZONE_NAMES entry %r: %s", zid, exc)
        return {zid: name for zid, name in names.items() if name}

    def _warn_about_addresses(self) -> None:
        """Log, once at load, any hub or broker address off the LAN. A bad
        environment value is ignored. A stored one (saved before this check
        existed) is still used so the app keeps working; the settings page
        shows a warning next to it."""
        for suffix, on_lan in (("BASE_IP", is_lan_ipv4), ("MQTT_HOST", is_lan_broker_host)):
            value = self._clean(_env_lookup(self._env, suffix))
            if value and not on_lan(value):
                _log.warning("Ignoring HEARTHMAGE_%s=%r: not an address on the local network",
                             suffix, value)
        for suffix, valid, default in (
            ("MQTT_BASE_TOPIC", valid_base_topic, _DEFAULT_BASE_TOPIC),
            ("MQTT_NODE_ID", valid_node_id, _DEFAULT_NODE_ID),
        ):
            value = _env_lookup(self._env, suffix)
            if value is not None and value.strip() and not valid(value.strip()):
                _log.warning("Ignoring HEARTHMAGE_%s=%r: using %r instead. Allowed: letters, "
                             "digits, dashes, underscores%s.", suffix, value, default,
                             " and single slashes" if suffix == "MQTT_BASE_TOPIC" else "")
        rejected = self.mqtt_base_topic_rejected
        if rejected is not None:
            _log.warning("Stored MQTT base topic %r is not valid; using %r instead. Enter a "
                         "valid base topic in Settings.", rejected, _DEFAULT_BASE_TOPIC)
        if not self.hub_ip_on_lan:
            _log.warning("Stored hub address %r is not a private LAN IPv4 address; still "
                         "using it. Enter the hub's LAN address in Settings.", self.hub_ip)
        if not self.mqtt_host_on_lan:
            _log.warning("Stored MQTT broker %r is not on the local network; still using "
                         "it. Enter a LAN broker in Settings.", self.mqtt_config()["host"])

    # --- read ---------------------------------------------------------

    @property
    def hub_ip(self) -> str | None:
        with self._lock:
            value = self._data.get("hub_ip")
        if not value:
            value = _env_lookup(self._env, "BASE_IP")
            if not is_lan_ipv4(value):
                value = None  # logged at load
        value = value.strip() if isinstance(value, str) else None
        return value or None

    @property
    def hub_ip_on_lan(self) -> bool:
        """False only when the hub address in use is off the LAN."""
        hub_ip = self.hub_ip
        return hub_ip is None or is_lan_ipv4(hub_ip)

    @property
    def mqtt_host_on_lan(self) -> bool:
        """False only when the broker in use is off the LAN."""
        config = self.mqtt_config()
        return config is None or is_lan_broker_host(config["host"])

    @property
    def hub_port(self) -> int:
        with self._lock:
            raw = self._data.get("hub_port")
        raw = raw or _env_lookup(self._env, "HUB_PORT") or _DEFAULT_HUB_PORT
        return parse_port(raw) or _DEFAULT_HUB_PORT

    @property
    def zone_names(self) -> dict[str, str]:
        names = dict(self._env_names)
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
    def sessions_path(self) -> str:
        """Where signed-in device sessions are kept (next to the config)."""
        directory = os.path.dirname(self._path) or "."
        return os.path.join(directory, "sessions.json")

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
            if base_topic is not None and not valid_base_topic(base_topic):
                base_topic = None  # logged at load, shown on the settings page
        else:
            host = self._clean(_env_lookup(self._env, "MQTT_HOST"))
            if not is_lan_broker_host(host):
                host = None  # logged at load
            raw_port = _env_lookup(self._env, "MQTT_PORT")
            username = self._clean(_env_lookup(self._env, "MQTT_USERNAME"))
            password = _env_lookup(self._env, "MQTT_PASSWORD") or None
            base_topic = self._clean(_env_lookup(self._env, "MQTT_BASE_TOPIC"))
            if base_topic is not None and not valid_base_topic(base_topic):
                base_topic = None  # logged at load
        if not host:
            return None
        return {
            "host": host,
            "port": parse_port(raw_port) or 1883,
            "username": username,
            "password": password,
            "base_topic": base_topic or _DEFAULT_BASE_TOPIC,
            "node_id": self._env_node_id(),
        }

    @property
    def mqtt_base_topic_rejected(self) -> str | None:
        """The stored base topic when it fails the rule (saved before the rule
        existed, or hand-edited); the bridge uses the default meanwhile."""
        stored = self._mqtt_stored()
        topic = self._clean(stored.get("base_topic")) if stored else None
        return topic if topic is not None and not valid_base_topic(topic) else None

    def _env_node_id(self) -> str:
        value = _env_lookup(self._env, "MQTT_NODE_ID", _DEFAULT_NODE_ID).strip()
        return value if valid_node_id(value) else _DEFAULT_NODE_ID

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
        """Key for the password fingerprint that ties sessions to the current
        password (changing either signs everyone out); generated and persisted once."""
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

    def set_hub(self, hub_ip: str, hub_port=None) -> None:
        """Save the hub address. ``hub_port`` may be form text; None keeps the
        stored port. Raises AddressRejected, before anything changes, for an
        address that is not a private LAN IPv4 address or an unusable port."""
        if not is_lan_ipv4(hub_ip):
            raise AddressRejected("hub_ip", _HUB_IP_RULE)
        port = None
        if hub_port is not None:
            port = parse_port(hub_port)
            if port is None:
                raise AddressRejected("hub_port", _PORT_RULE)
        with self._lock:
            self._data["hub_ip"] = hub_ip.strip()
            if port is not None:
                self._data["hub_port"] = port
            self._save()

    def set_price(self, price_per_kwh: float) -> None:
        price = check_price(price_per_kwh)  # InvalidSetting before anything changes
        with self._lock:
            self._data["price_per_kwh"] = price
            self._save()

    def set_zone_name(self, zone_id: str, name: str) -> None:
        self.set_zone_names({str(zone_id): name or ""})

    def set_zone_names(self, names: Mapping[str, str]) -> None:
        """Apply several zone names with one save and one backup snapshot. A
        blank name clears back to the default label. Raises InvalidSetting,
        before anything changes, for a non-numeric zone id or a bad name."""
        checked = clean_zone_names(names)
        with self._lock:
            stored = self._data.setdefault("zone_names", {})
            for zid, name in checked.items():
                if name:
                    stored[zid] = name
                else:
                    stored.pop(zid, None)
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
        or ``"clear"`` (remove it). A host off the LAN raises AddressRejected.
        ``"keep"`` for a different host, port or
        username raises MqttPasswordReentry before anything changes: the
        password has to be entered again for the new broker. The check, the
        change and the save happen under one hold of the lock, and the new
        object replaces the old one in a single assignment."""
        if not is_lan_broker_host(host):
            raise AddressRejected("host", _BROKER_RULE)
        base_topic = (base_topic or "").strip() or _DEFAULT_BASE_TOPIC
        if not valid_base_topic(base_topic):
            raise InvalidSetting("base_topic", _TOPIC_RULE)
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
                "base_topic": base_topic,
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
