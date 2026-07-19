"""Durable JSON writes.

Writing straight over a file (``open(path, "w")`` then ``json.dump``) can leave a
truncated file if the process dies or the machine loses power mid-write - and the
loaders here fall back to "empty" on a parse error, so a user's schedules or hub
config would silently vanish. Write to a temp file in the same directory, fsync
it, then atomically ``os.replace`` it onto the target instead.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def write_json_atomic(path: str, data: Any, indent: int | None = 2) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
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
