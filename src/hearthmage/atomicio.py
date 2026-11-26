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


def write_json_atomic(path: str, data: Any, indent: int | None = 2) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
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
