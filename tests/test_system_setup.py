"""Tests for the /system/ dashboard server (jasper.web.system_setup).

The page itself is mostly client-side JS so server-side tests focus
on the routes' wiring + the JSON proxy. We don't try to test the
sparkline rendering — that's browser territory.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from jasper.web import system_setup


def _http_get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _http_post(url: str) -> tuple[int, bytes]:
    """POST with CSRF round-trip. Mints the cookie via GET /, reads the
    csrf token from the rendered <meta name=jts-csrf>, sends both on
    the actual POST as X-CSRF-Token."""
    import http.cookiejar
    import re
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
    )
    page = opener.open(base + "/", timeout=5).read().decode()
    m = re.search(
        r'<meta\s+name="jts-csrf"\s+content="([^"]+)"', page,
    )
    token = m.group(1) if m else ""
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"X-CSRF-Token": token},
    )
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def upstream_control():
    """Stand up a fake jasper-control on a random port. Stores
    every request path it sees so tests can assert on routing."""
    received: list[tuple[str, str]] = []  # (method, path)
    responses: dict[str, dict] = {
        "/system/snapshot": {
            "build": {"JASPER_GIT_SHA": "abc1234"},
            "metrics": {"current": {"mem_total_mb": 2048}},
            "cloud": {"available": False, "reason": "no usage.db yet"},
            "voice_provider": "gemini",
        },
        "/system/diagnostics": {
            "fails": 0, "warns": 0, "results": [
                {"name": "env_file", "status": "ok", "detail": "/etc/jasper/jasper.env present"},
            ],
        },
        "/system/restart/voice": {"ok": True, "action": "restart-voice"},
        "/system/restart/audio": {"ok": True, "action": "restart-audio"},
        "/system/reboot": {"ok": True, "action": "reboot"},
        "/system/poweroff": {"ok": True, "action": "poweroff"},
    }

    class _UpHandler(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw) -> None:
            pass

        def _reply(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            received.append(("GET", self.path))
            if self.path in responses:
                self._reply(responses[self.path])
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            received.append(("POST", self.path))
            if self.path in responses:
                self._reply(responses[self.path])
            else:
                self.send_error(404)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _UpHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield base, received, responses
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


@pytest.fixture
def dashboard_server(upstream_control):
    """Stand up jasper-system-web pointing at the fake control."""
    base, received, responses = upstream_control
    handler = system_setup._make_handler(control_base=base)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    web_base = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield web_base, received, responses
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_root_serves_html_with_polling_script(dashboard_server) -> None:
    base, _received, _ = dashboard_server
    status, body = _http_get(f"{base}/")
    assert status == 200
    text = body.decode("utf-8")
    assert "<!doctype html>" in text
    assert "id=\"spark-memory\"" in text  # sparkline target present
    assert "data.json" in text  # polling URL referenced from JS
    assert "Restart voice" in text  # action button present


def test_data_json_proxies_snapshot(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, body = _http_get(f"{base}/data.json")
    assert status == 200
    payload = json.loads(body)
    assert payload["build"]["JASPER_GIT_SHA"] == "abc1234"
    assert payload["voice_provider"] == "gemini"
    assert ("GET", "/system/snapshot") in received


def test_diagnostics_json_proxies_doctor(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, body = _http_get(f"{base}/diagnostics.json")
    assert status == 200
    payload = json.loads(body)
    assert payload["fails"] == 0
    assert ("GET", "/system/diagnostics") in received


def test_post_restart_voice_proxies(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, body = _http_post(f"{base}/restart/voice")
    assert status == 200
    payload = json.loads(body)
    assert payload["action"] == "restart-voice"
    assert ("POST", "/system/restart/voice") in received


def test_post_restart_audio_proxies(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, _ = _http_post(f"{base}/restart/audio")
    assert status == 200
    assert ("POST", "/system/restart/audio") in received


def test_post_reboot_proxies(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, _ = _http_post(f"{base}/reboot")
    assert status == 200
    assert ("POST", "/system/reboot") in received


def test_post_poweroff_proxies(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, body = _http_post(f"{base}/poweroff")
    assert status == 200
    payload = json.loads(body)
    assert payload["action"] == "poweroff"
    assert ("POST", "/system/poweroff") in received


def test_poweroff_requires_csrf(dashboard_server) -> None:
    """Power off is destructive (no auto-recovery — user must
    physically re-plug). Same CSRF gate as the other action endpoints."""
    base, received, _ = dashboard_server
    # Plain POST with no X-CSRF-Token header should be rejected.
    req = urllib.request.Request(f"{base}/poweroff", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 403
    # And the upstream control was NOT contacted.
    assert ("POST", "/system/poweroff") not in received


def test_root_includes_poweroff_button(dashboard_server) -> None:
    """The dashboard HTML carries a Power off button styled as a
    danger action, sitting alongside Reboot."""
    base, _, _ = dashboard_server
    status, body = _http_get(f"{base}/")
    assert status == 200
    text = body.decode("utf-8")
    assert 'id="btn-poweroff"' in text
    # Double-confirm copy is load-bearing UX — the second prompt is
    # what discourages mis-click on the most destructive action on
    # the dashboard. Keep it in the test so a future "tidy the JS"
    # PR doesn't silently drop it.
    assert "physically re-plug power" in text
    assert "absolutely sure" in text


def test_unknown_route_404(dashboard_server) -> None:
    base, _, _ = dashboard_server
    status, _ = _http_get(f"{base}/nope")
    assert status == 404


def test_aec_card_moved_to_wake(dashboard_server) -> None:
    """The Wake detection card moved to /wake/. /system/ must no
    longer serve the routes that backed it — /aec.json, /aec/toggle,
    /aec/leg, /aec/threshold all 404 here, and the HTML must not
    reference the old DOM ids the card's JS bound to."""
    base, received, _ = dashboard_server
    for route in ("/aec.json",):
        status, _ = _http_get(f"{base}{route}")
        assert status == 404, f"{route} should be gone from /system/"
    for route in ("/aec/toggle", "/aec/leg", "/aec/threshold"):
        status, _ = _http_post(f"{base}{route}")
        assert status == 404, f"{route} should be gone from /system/"
    # And jasper-control never saw an /aec call from /system/.
    assert not [r for r in received if "/aec" in r[1]]
    # The HTML stopped referencing the card-specific DOM ids.
    _, body = _http_get(f"{base}/")
    text = body.decode()
    for stale in (
        "btn-aec-toggle", "leg-raw", "leg-dtln",
        "wake-threshold", "aec-card", "leg-table",
    ):
        assert stale not in text, f"{stale} should be gone from /system/ HTML"


def test_data_json_502_when_control_down() -> None:
    """If jasper-control is unreachable, /data.json returns 502 with
    a useful error body rather than crashing the dashboard. Lets the
    browser-side `catch` show a meaningful 'Disconnected' message."""
    # Point at a port nothing is listening on.
    handler = system_setup._make_handler(control_base="http://127.0.0.1:1")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        status, body = _http_get(f"{base}/data.json")
        assert status == 502
        payload = json.loads(body)
        assert "error" in payload
        assert "jasper-control unreachable" in payload["error"]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)
