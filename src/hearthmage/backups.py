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
from datetime import datetime, timezone
from typing import Any

from hearthmage.atomicio import write_json_atomic

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
    base = os.path.basename(source_path)
    dest = os.path.join(directory, f"{base}.{_timestamp()}{_SUFFIX}")
    shutil.copyfile(source_path, dest)
    _prune(source_path, keep)
    return dest


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
    if not isinstance(bundle, dict) or "hearthmage_backup" not in bundle:
        raise ValueError("not a HearthMage backup bundle")
    for key, path in (("config", config_path), ("schedules", schedules_path)):
        if key not in bundle:
            continue  # not part of this bundle: do not touch the file
        section = bundle[key]
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ValueError(f"backup section {key!r} is not an object")
        if key == "config":
            section = _restore_secrets(section, _read_json(path))
        rotate_backup(path)  # preserve whatever we are about to overwrite
        write_json_atomic(path, section)


def _restore_secrets(incoming: dict, existing: Any) -> dict:
    """Exports are redacted, so an imported config missing ``secret_key`` or
    ``mqtt.password`` keeps whatever the local file already stores. An import
    that explicitly carries those values wins; one with no ``mqtt`` object at
    all disables MQTT and does not resurrect the old password."""
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
    ):
        mqtt_in = dict(mqtt_in)
        mqtt_in["password"] = mqtt_old["password"]
        incoming["mqtt"] = mqtt_in
    return incoming
