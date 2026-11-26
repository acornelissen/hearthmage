"""Security headers, the CSP's template invariants, and the disabled API docs."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import hearthmage.app as appmod
from hearthmage.app import app, get_client
from hearthmage.domain import Room
from hearthmage.fake_client import FakeHearthClient

BASE = Path(appmod.__file__).parent
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)


@pytest.fixture()
def client() -> TestClient:
    fake = FakeHearthClient(
        [Room("1", "Living Room", 19.5, 21.0, is_off=False, min_temp=5.0, max_temp=30.0)]
    )
    app.dependency_overrides[get_client] = lambda: fake
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/", "/rooms/1/status", "/static/styles.css", "/sw.js", "/healthz", "/nope"])
def test_every_response_carries_the_security_headers(client, path):
    resp = client.get(path)
    assert resp.headers["content-security-policy"] == CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    pp = resp.headers["permissions-policy"]
    for feature in ("camera", "microphone", "geolocation"):
        assert f"{feature}=()" in pp


def test_csrf_rejection_also_carries_headers(client):
    resp = client.post("/settings/hub", data={"hub_ip": "10.0.0.1"}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_disabled(client, path):
    assert client.get(path).status_code == 404


def test_templates_have_nothing_the_csp_blocks():
    for tpl in (BASE / "templates").glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\ssrc=)", text), f"inline script in {tpl.name}"
        assert not re.search(r"<style\b", text), f"inline style block in {tpl.name}"
        assert not re.search(r"\sstyle=", text), f"style attribute in {tpl.name}"
        assert not re.search(r"\son[a-z]+=", text), f"inline event handler in {tpl.name}"
        assert "javascript:" not in text, f"javascript: URL in {tpl.name}"
        assert "hx-on" not in text, f"hx-on handler in {tpl.name}"


@pytest.mark.parametrize("page", ["index", "schedule", "energy", "history", "settings", "config"])
def test_pages_that_load_htmx_turn_off_its_inline_style_and_eval(page):
    text = (BASE / "templates" / f"{page}.html").read_text(encoding="utf-8")
    assert '"includeIndicatorStyles":false' in text
    assert '"allowEval":false' in text


def test_every_script_a_template_loads_exists():
    for tpl in (BASE / "templates").glob("*.html"):
        for src in re.findall(r'<script[^>]*\ssrc="/static/([^"]+)"', tpl.read_text(encoding="utf-8")):
            assert (BASE / "static" / src).is_file(), f"{tpl.name} loads missing {src}"


def test_page_scripts_are_in_the_service_worker_shell():
    shell = set(appmod._STATIC_SHELL_FILES)
    assert {"app.js", "target-box.js", "schedule.js"} <= shell
    sw = appmod._service_worker_source()
    for name in ("app.js", "target-box.js", "schedule.js"):
        assert f"/static/{name}" in sw


# --- bars are server-rendered SVG (presentation attributes, no JS, no style="") ---


@pytest.mark.parametrize(
    "raw, want",
    [(58.04, 58.0), (150, 100.0), (-20, 0.0), (0, 0.0), (None, 0.0), (float("nan"), 0.0),
     (float("inf"), 100.0), (float("-inf"), 0.0), ("abc", 0.0), ("42.5", 42.5)],
)
def test_pct_clamps_and_ignores_non_numbers(raw, want):
    assert appmod._pct(raw) == pytest.approx(want, abs=0.05)


def test_readout_draws_the_track_as_svg(client):
    html = client.get("/rooms/1/status").text
    assert "data-bar" not in html and "style=" not in html
    assert '<svg class="track-svg"' in html
    assert re.search(r'<rect class="track-fill" [^>]*width="\d+\.\d%"', html)
    assert 'class="track-target"' in html


def test_bar_scripts_are_gone():
    assert not (BASE / "static" / "bars.js").exists()
    assert "bars.js" not in appmod._service_worker_source()
    for tpl in (BASE / "templates").glob("*.html"):
        assert "bars.js" not in tpl.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ["index", "energy"])
def test_pages_with_bars_define_the_heat_gradient_once(page):
    text = (BASE / "templates" / f"{page}.html").read_text(encoding="utf-8")
    assert text.count('_heat_defs.html') == 1
    assert 'id="heat-grad"' in (BASE / "templates" / "_heat_defs.html").read_text(encoding="utf-8")


def test_a_crashing_route_still_gets_security_headers_and_is_logged(caplog):
    @app.get("/_boom_for_test")
    def boom():
        raise RuntimeError("kaboom")

    try:
        resp = TestClient(app, raise_server_exceptions=False).get("/_boom_for_test")
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/_boom_for_test"]
    assert resp.status_code == 500
    assert resp.text == "Internal Server Error"
    assert resp.headers["content-security-policy"] == CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert any("kaboom" in (r.exc_text or "") for r in caplog.records)
