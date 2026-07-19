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
