"""Backups and export/import for the two irreplaceable files.

``schedules.json`` is the only copy of schedule block detail (the hub returns
only a day-level summary over the LAN), and ``config.json`` holds the hub
address and zone names. Losing either means re-entering everything by hand, so:

- every write to those files also drops a timestamped snapshot under a sibling
  ``backups/`` directory (kept to the newest few), and
- the settings page can export both as one JSON bundle and import it back.

Snapshots are plain copies of the file's post-write contents. Restore writes
through ``write_json_atomic`` and snapshots whatever it is about to overwrite
first, so an import can itself be undone.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any

from hearthmage.atomicio import make_private, make_private_fd, write_json_atomic
from hearthmage.lan import AddressRejected, is_lan_broker_host, is_lan_ipv4

BUNDLE_VERSION = 1
_DEFAULT_KEEP = 10
_SUFFIX = ".bak.json"


def _backups_dir(source_path: str) -> str:
    return os.path.join(os.path.dirname(source_path) or ".", "backups")


def _timestamp() -> str:
    # Microseconds keep two snapshots in the same second from colliding, and the
    # fixed width means lexical order equals chronological order.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")


def rotate_backup(source_path: str, keep: int = _DEFAULT_KEEP) -> str | None:
    """Snapshot ``source_path`` into its ``backups/`` dir; prune to newest ``keep``.

    Returns the snapshot path, or None if the source does not exist. Best-effort
    by contract of the callers, but surfaces OSErrors to a caller that cares.
    """
    if not os.path.exists(source_path):
        return None
    directory = _backups_dir(source_path)
    os.makedirs(directory, exist_ok=True)
    make_private(directory, 0o700)
    base = os.path.basename(source_path)
    dest = os.path.join(directory, f"{base}.{_timestamp()}{_SUFFIX}")
    _copy_private(source_path, dest)
    _prune(source_path, keep)
    _tighten_older_snapshots(source_path)
    return dest


def _tighten_older_snapshots(source_path: str) -> None:
    """Make the file's remaining snapshots owner-only: ones written before
    snapshots were private, or on a share whose inherited ACL opened them up.
    The app's files are always private (owner decision 2026-11-26), whatever
    the share does. Best-effort: a share that refuses chmod is not an error."""
    for snapshot in list_backups(source_path):
        make_private(snapshot)


def tighten_data_files(paths: list[str]) -> None:
    """Startup pass: make each of the app's own data files, their snapshots and
    the backups folder owner-only. Only these named files are touched, never
    anything else that shares the folder."""
    for path in paths:
        make_private(path)
        _tighten_older_snapshots(path)
        if os.path.isdir(_backups_dir(path)):
            make_private(_backups_dir(path), 0o700)


def _copy_private(source_path: str, dest: str) -> None:
    """Copy bytes to ``dest`` atomically, readable by the owner only.

    The config file holds the session secret and the MQTT password and is
    itself created 0600, so a snapshot must not be more readable than that.
    ``mkstemp`` creates the temp file 0600 (set again explicitly, as a share's
    ACL can override it); ``os.replace`` then makes the snapshot appear whole
    or not at all.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".tmp-", suffix=_SUFFIX)
    try:
        make_private_fd(fd)
        with os.fdopen(fd, "wb") as out, open(source_path, "rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_backups(source_path: str) -> list[str]:
    """Snapshot paths for ``source_path``, newest first."""
    base = os.path.basename(source_path)
    pattern = os.path.join(_backups_dir(source_path), f"{base}.*{_SUFFIX}")
    return sorted(glob.glob(pattern), reverse=True)


def _prune(source_path: str, keep: int) -> None:
    for stale in list_backups(source_path)[max(keep, 0):]:
        try:
            os.remove(stale)
        except OSError:
            pass  # pruning is housekeeping; a stuck file is not worth failing on


def _read_json(path: str) -> Any:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _redact_config(config: Any) -> Any:
    """Strip secrets from a config object bound for an export bundle: the
    session ``secret_key`` and the MQTT broker password never leave the box."""
    if not isinstance(config, dict):
        return config
    config = dict(config)
    config.pop("secret_key", None)
    mqtt = config.get("mqtt")
    if isinstance(mqtt, dict):
        mqtt = dict(mqtt)
        mqtt.pop("password", None)
        config["mqtt"] = mqtt
    return config


def build_bundle(config_path: str, schedules_path: str) -> dict:
    """A single portable snapshot of config + schedules for download.

    Secrets (session key, MQTT broker password) are redacted; ``restore_bundle``
    carries the locally stored values forward, so a round trip loses nothing."""
    return {
        "hearthmage_backup": BUNDLE_VERSION,
        "config": _redact_config(_read_json(config_path)),
        "schedules": _read_json(schedules_path),
    }


def restore_bundle(bundle: Any, config_path: str, schedules_path: str) -> None:
    """Write the bundle's config/schedules back, snapshotting existing files first.

    Sections absent from the bundle are left alone. Raises ValueError if the
    payload is not a recognised backup bundle.
    """
    # Imported here: schedule_store imports this module for rotate_backup.
    from hearthmage.schedule_store import validate_data

    if not isinstance(bundle, dict) or "hearthmage_backup" not in bundle:
        raise ValueError("not a HearthMage backup bundle")
    # Validate every section before writing any, so a bad file changes nothing.
    writes: list[tuple[str, dict]] = []
    for key, path in (("config", config_path), ("schedules", schedules_path)):
        if key not in bundle:
            continue  # not part of this bundle: do not touch the file
        section = bundle[key]
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ValueError(f"backup section {key!r} is not an object")
        if key == "config":
            _check_ports(section)  # an import is untrusted input
            _check_addresses(section)
            section = _clean_values(section)
            section = _restore_secrets(section, _read_json(path))
        else:
            section = validate_data(section)  # an import is untrusted input
        writes.append((path, section))
    for path, section in writes:
        rotate_backup(path)  # preserve whatever we are about to overwrite
        write_json_atomic(path, section)


def _check_ports(config: dict) -> None:
    """Refuse a config whose hub or broker port is not a usable port number.
    json accepts Infinity, which used to break every page that read it."""
    # Imported here: settings imports this module for rotate_backup.
    from hearthmage.settings import parse_port

    mqtt = config.get("mqtt")
    ports = [config.get("hub_port"), mqtt.get("port") if isinstance(mqtt, dict) else None]
    for raw in ports:
        if raw is not None and parse_port(raw) is None:
            raise ValueError("backup has an unusable port number")


def _clean_values(config: dict) -> dict:
    """Refuse a config with a bad MQTT base topic, zone name or price, by the
    same rules as the settings forms, and return a copy with the zone names
    stripped and their keys normalised. Raises InvalidSetting (a ValueError)."""
    # Imported here: settings imports this module for rotate_backup.
    from hearthmage.settings import (
        InvalidSetting,
        check_price,
        clean_zone_names,
        valid_base_topic,
    )

    config = dict(config)
    mqtt = config.get("mqtt")
    topic = mqtt.get("base_topic") if isinstance(mqtt, dict) else None
    if topic is not None and not valid_base_topic(topic):
        raise InvalidSetting("base_topic", "The backup's MQTT base topic is not valid.")
    names = config.get("zone_names")
    if names is not None:
        if not isinstance(names, dict):
            raise ValueError("backup zone names are not an object")
        config["zone_names"] = {zid: n for zid, n in clean_zone_names(names).items() if n}
    if config.get("price_per_kwh") is not None:
        config["price_per_kwh"] = check_price(config["price_per_kwh"])
    return config


def _check_addresses(config: dict) -> None:
    """Refuse a config that points the hub or the MQTT broker off the LAN, by
    the same rules as the settings forms. A blank or missing address is fine."""
    mqtt = config.get("mqtt")
    checks = (
        ("hub_ip", config.get("hub_ip"), is_lan_ipv4),
        ("host", mqtt.get("host") if isinstance(mqtt, dict) else None, is_lan_broker_host),
    )
    for field, value, on_lan in checks:
        blank = value is None or (isinstance(value, str) and not value.strip())
        if not blank and not on_lan(value):
            raise AddressRejected(field, "backup has an address off the local network")


def _restore_secrets(incoming: dict, existing: Any) -> dict:
    """Exports are redacted, so an imported config missing ``secret_key`` or
    ``mqtt.password`` keeps whatever the local file already stores. An import
    that explicitly carries those values wins; one with no ``mqtt`` object at
    all disables MQTT and does not resurrect the old password. The stored
    password is carried only to the same broker (host, port and username):
    an import must not redirect it to another host."""
    # Imported here: settings imports this module for rotate_backup.
    from hearthmage.settings import mqtt_broker

    if not isinstance(existing, dict):
        return incoming
    incoming = dict(incoming)
    if "secret_key" not in incoming and existing.get("secret_key"):
        incoming["secret_key"] = existing["secret_key"]
    mqtt_in, mqtt_old = incoming.get("mqtt"), existing.get("mqtt")
    if (
        isinstance(mqtt_in, dict)
        and "password" not in mqtt_in
        and isinstance(mqtt_old, dict)
        and mqtt_old.get("password")
        and mqtt_broker(mqtt_in.get("host"), mqtt_in.get("port"), mqtt_in.get("username"))
        == mqtt_broker(mqtt_old.get("host"), mqtt_old.get("port"), mqtt_old.get("username"))
    ):
        mqtt_in = dict(mqtt_in)
        mqtt_in["password"] = mqtt_old["password"]
        incoming["mqtt"] = mqtt_in
    return incoming
