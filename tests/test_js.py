"""Behavioural checks for the two pieces of browser JavaScript the app ships.

There is no JS build or test runner here. Each check is a small Node script in
``tests/js`` that loads the app's real code (the service worker source, the
inline page script) and runs it against stub browser objects. They are skipped
when ``node`` is not installed, so the Python suite still runs anywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

import hearthmage.app as appmod

JS_DIR = Path(__file__).parent / "js"
INDEX_HTML = Path(appmod.__file__).parent / "templates" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(script: str, target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(JS_DIR / script), str(target)], capture_output=True, text=True, timeout=30
    )


def test_service_worker_only_falls_back_for_page_loads(tmp_path):
    # Answering a failed htmx poll with the cached home page makes the page
    # nest itself until the tab hangs (it took a browser down on 2026-11-10).
    source = tmp_path / "sw.js"
    source.write_text(appmod._service_worker_source(), encoding="utf-8")
    result = _run("service_worker_check.js", source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout and result.stdout.count("PASS") == 7


def test_target_box_and_off_button_follow_the_hub():
    # The target box and Off button sit outside the polled readout; the page
    # script keeps them showing the hub's state without clobbering an edit.
    result = _run("target_box_check.js", INDEX_HTML)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout and result.stdout.count("PASS") == 14
