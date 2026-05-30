"""Tests for the /system/ dashboard server (jasper.web.system_setup).

The page itself is mostly client-side JS so server-side tests focus
on the routes' wiring + the JSON proxy. We don't try to test the
sparkline rendering — that's browser territory.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


def _http_post_json(url: str, payload: dict[str, Any]) -> tuple[int, bytes]:
    """JSON POST with the same CSRF round-trip as _http_post."""
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
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": token,
        },
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
            "metrics": {
                "current": {"mem_total_mb": 2048},
                "services": [
                    {
                        "name": "jasper-outputd",
                        "unit": "jasper-outputd.service",
                        "group": "Audio",
                        "cpu_pct": 0.2,
                        "memory_mb": 11.2,
                    },
                ],
            },
            "airplay_health": {"status": "ok", "reason": "clean"},
            "audio_quality": {
                "converter": "samplerate_medium",
                "active_converter": "samplerate_medium",
                "label": "Medium",
                "summary": "Lower CPU, still clean.",
                "options": [],
            },
            "outputd": {
                "backend": "alsa",
                "content": {
                    "buffer_frames": 4096,
                    "xrun_count": 0,
                    "last_xrun_age_ms": None,
                    "xrun_rate_per_hour": 0.0,
                    "empty_periods": 0,
                    "eagain_count": 0,
                },
                "dac": {
                    "buffer_frames": 3072,
                    "xrun_count": 0,
                    "last_xrun_age_ms": None,
                    "xrun_rate_per_hour": 0.0,
                },
                "mix": {"last_period_clipped_samples": 0},
                "tts": {
                    "pending_frames": 0,
                    "over_budget": False,
                    "over_budget_ms": 0,
                    "dropped_commands": 0,
                    "dropped_audio_frames": 0,
                },
            },
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
        "/system/audio-quality": {"ok": True, "action": "audio-quality"},
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
                payload = dict(responses[self.path])
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length else b""
                if raw:
                    payload["received_body"] = json.loads(raw.decode())
                self._reply(payload)
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


def test_root_serves_canonical_shell(dashboard_server) -> None:
    """The page is the canonical design-system shell: the shared app.css
    link, a CSRF meta tag, the icon sprite, and the ES module entry. All
    behaviour now lives in /assets/system-status/js/ (asserted against the
    module files below), so the rendered HTML must NOT inline the old
    script or its DOM ids."""
    base, _received, _ = dashboard_server
    status, body = _http_get(f"{base}/")
    assert status == 200
    text = body.decode("utf-8")
    # Canonical shell from canonical_page().
    assert "<!doctype html>" in text
    assert "/assets/app.css?v=" in text  # shared stylesheet, cache-busted
    assert 'name="jts-csrf"' in text  # CSRF token for the module's POSTs
    assert 'id="icon-back"' in text  # shared inline sprite
    assert '<div id="app"' in text  # mount point
    assert "Loading the dashboard" in text  # boot placeholder (visible if modules fail to load)
    assert '<script type="module" src="/assets/system-status/js/main.js">' in text
    # Page CSS is a linked static file now (lintable + cacheable), not inlined.
    assert "/assets/system-status/system.css?v=" in text
    assert "<style>" not in text
    # The behaviour moved out of the HTML — no inline script, no old ids.
    assert "function render" not in text
    assert "data-converter" not in text
    assert 'id="spark-memory"' not in text
    assert 'id="airplay-card"' not in text
    assert "serviceMemoryMb" not in text


def test_data_json_proxies_snapshot(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, body = _http_get(f"{base}/data.json")
    assert status == 200
    payload = json.loads(body)
    assert payload["build"]["JASPER_GIT_SHA"] == "abc1234"
    assert payload["voice_provider"] == "gemini"
    assert payload["airplay_health"]["status"] == "ok"
    assert payload["outputd"]["backend"] == "alsa"
    assert payload["audio_quality"]["converter"] == "samplerate_medium"
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


def test_post_audio_quality_proxies_json_body(dashboard_server) -> None:
    base, received, responses = dashboard_server
    responses["/system/audio-quality"] = {
        "ok": True,
        "action": "audio-quality",
        "audio_quality": {"converter": "samplerate_best"},
    }
    status, body = _http_post_json(
        f"{base}/audio-quality",
        {"converter": "samplerate_best"},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["audio_quality"]["converter"] == "samplerate_best"
    assert payload["received_body"] == {"converter": "samplerate_best"}
    assert ("POST", "/system/audio-quality") in received


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


_MODULE_DIR = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "system-status" / "js"
)


# The /system/ UI is a layered set of static ES modules. These guards scan
# the combined module text so they survive refactors that move a string from
# one module to another (only the layout, not the behaviour, should change).
_EXPECTED_MODULES = (
    "dom", "format", "charts", "components", "sections",
    "views", "api", "actions", "main",
)


def _system_js() -> str:
    return "\n".join(
        (_MODULE_DIR / f"{name}.js").read_text() for name in _EXPECTED_MODULES
    )


def test_static_modules_present() -> None:
    """The /system/ UI ships as static ES modules (served + revalidated by
    nginx, copied by install.sh). Every layer must exist in the repo."""
    for name in _EXPECTED_MODULES:
        assert (_MODULE_DIR / f"{name}.js").is_file(), f"missing module {name}.js"


def test_modules_preserve_destructive_confirms_and_csrf() -> None:
    """The double-confirm on reboot + power off is load-bearing UX — the
    second prompt discourages a mis-click on the most destructive actions.
    Guard the copy + the CSRF-via-meta wiring so a future "tidy the JS" PR
    can't silently drop either."""
    js = _system_js()
    assert "physically re-plug power" in js
    assert "absolutely sure" in js
    assert "Wake-word will be unavailable" in js  # voice-restart warning
    # CSRF token is read from the meta tag, never baked into the cached module.
    assert "meta[name=jts-csrf]" in js
    assert "X-CSRF-Token" in js
    # Post-action feedback: reboot/power-off surface a "page will be
    # unreachable" note rather than relying on the button label alone.
    assert "unreachable for" in js


def test_modules_wire_the_proxy_endpoints() -> None:
    """The modules must POST to the same action paths the handler proxies and
    poll the same read endpoints."""
    js = _system_js()
    for path in ("restart/voice", "restart/audio", "reboot", "poweroff",
                 "audio-quality", "data.json", "diagnostics.json"):
        assert path in js, f"system modules no longer reference {path}"


def test_modules_preserve_metric_logic() -> None:
    """Spot-check that the formatting/threshold port survived: the system-total
    breakdown, throttle wording, the MPRIS fallback, the audio-conversion
    options, and the cgroup warning."""
    js = _system_js()
    assert "System total · shown / unshown / free" in js
    assert "throttling now" in js
    assert "MPRIS playing" in js
    assert "samplerate_medium" in js
    assert "samplerate_best" in js
    assert "cgroup_enable=memory" in js
    assert "tts dropped" in js


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
