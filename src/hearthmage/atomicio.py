"""Durable JSON writes.

Writing straight over a file (``open(path, "w")`` then ``json.dump``) can leave a
truncated file if the process dies or the machine loses power mid-write - and the
loaders here fall back to "empty" on a parse error, so a user's schedules or hub
config would silently vanish. Write to a temp file in the same directory, fsync
it, then atomically ``os.replace`` it onto the target instead.

Every file is owner-only. ``mkstemp`` asks for 0600, but a share's inherited
ACL can override the mode a file is created with (the Synology NAS made them
0777, 2026-11-26); an explicit chmod afterwards does stick, so it is set again.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from typing import Any


def make_private_fd(fd: int) -> None:
    """Owner-only on an open file. Best effort: a filesystem that refuses
    chmod is not an error (the write still has to happen)."""
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass


def make_private(path: str, mode: int = 0o600) -> None:
    """Tighten an existing file (or, with ``mode=0o700``, a folder) that others
    can reach. Missing paths and a filesystem that refuses chmod are ignored."""
    try:
        if stat.S_IMODE(os.stat(path).st_mode) != mode:
            os.chmod(path, mode)
    except OSError:
        pass


_TEMP_PREFIX = ".tmp-"
_TEMP_SUFFIX = ".json"


def remove_stale_temp_files(directory: str, older_than: float = 60.0) -> int:
    """Delete temp files that writes here left behind when the process was
    stopped mid-write: regular files named ``.tmp-*.json`` (this module's own
    pattern, used for snapshots too) last changed over ``older_than`` seconds
    ago, so a write in progress is never touched. Symlinks and every other
    file are left alone. Returns how many were removed."""
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    cutoff = time.time() - older_than
    removed = 0
    for name in names:
        if not (name.startswith(_TEMP_PREFIX) and name.endswith(_TEMP_SUFFIX)):
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed


def write_json_atomic(path: str, data: Any, indent: int | None = 2) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
    try:
        make_private_fd(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
