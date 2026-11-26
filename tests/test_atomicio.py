import json
import os

from hearthmage.atomicio import write_json_atomic


def test_writes_and_creates_directory(tmp_path):
    path = tmp_path / "sub" / "data.json"
    write_json_atomic(str(path), {"a": 1, "b": [2, 3]})
    assert json.loads(path.read_text()) == {"a": 1, "b": [2, 3]}


def test_replaces_existing_without_leaving_temp_files(tmp_path):
    path = tmp_path / "data.json"
    write_json_atomic(str(path), {"v": 1})
    write_json_atomic(str(path), {"v": 2})
    assert json.loads(path.read_text()) == {"v": 2}
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")]
    assert leftovers == []


def test_failed_serialisation_leaves_target_intact(tmp_path):
    path = tmp_path / "data.json"
    write_json_atomic(str(path), {"ok": True})
    try:
        write_json_atomic(str(path), {"bad": object()})  # not JSON-serialisable
    except TypeError:
        pass
    assert json.loads(path.read_text()) == {"ok": True}  # original survives
    assert [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")] == []


def _share_makes_new_files_readable(monkeypatch):
    """Emulate a share whose inherited ACL overrides the mode a file is
    created with (seen on the Synology NAS 2026-11-26: mkstemp's 0600 files
    showed up 0777); an explicit chmod afterwards does stick."""
    import tempfile

    real = tempfile.mkstemp

    def mkstemp(*a, **k):
        fd, path = real(*a, **k)
        os.chmod(path, 0o777)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)


def test_written_file_is_private_even_on_a_share_that_opens_it_up(tmp_path, monkeypatch):
    import stat

    _share_makes_new_files_readable(monkeypatch)
    path = tmp_path / "config.json"
    write_json_atomic(str(path), {"secret_key": "x"})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_share_that_refuses_chmod_still_gets_the_write(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise PermissionError("not supported")

    monkeypatch.setattr(os, "fchmod", refuse)
    path = tmp_path / "data.json"
    write_json_atomic(str(path), {"v": 1})
    assert json.loads(path.read_text()) == {"v": 1}


def test_make_private_tightens_an_existing_file_and_skips_missing_ones(tmp_path):
    import stat

    from hearthmage.atomicio import make_private

    path = tmp_path / "sync.json"
    path.write_text("{}")
    os.chmod(path, 0o777)
    make_private(str(path))
    make_private(str(tmp_path / "absent.json"))  # no error
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
