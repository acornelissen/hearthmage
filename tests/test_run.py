import runpy
from pathlib import Path

import uvicorn

RUN_PY = Path(__file__).resolve().parent.parent / "run.py"


def test_run_py_turns_off_uvicorn_proxy_headers(monkeypatch):
    # uvicorn's own proxy handling trusts X-Forwarded-For/-Proto from 127.0.0.1
    # and rewrites the peer and scheme before the app's trusted-proxy check.
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    runpy.run_path(str(RUN_PY), run_name="__main__")
    assert len(calls) == 1
    assert calls[0]["proxy_headers"] is False
