# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /system/ dashboard server (jasper.web.system_setup).

The page itself is mostly client-side JS so server-side tests focus
on the routes' wiring + the JSON proxy. We don't try to test the
sparkline rendering — that's browser territory.
"""
from __future__ import annotations

import json
import http.cookiejar
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from jasper.web import system_setup


_NODE = shutil.which("node")
_NAV_HARNESS = Path(__file__).resolve().parent / "js" / "system_status_navigation_test.mjs"
_AUDIO_HARNESS = Path(__file__).resolve().parent / "js" / "system_audio_sections_test.mjs"
_LATENCY_HARNESS = Path(__file__).resolve().parent / "js" / "system_latency_control_test.mjs"
_OPTIONAL_FEATURES_HARNESS = (
    Path(__file__).resolve().parent / "js" / "system_optional_features_test.mjs"
)
_TRANSPORT_PARK_HARNESS = (
    Path(__file__).resolve().parent / "js" / "system_transport_park_test.mjs"
)
_DIAGNOSTICS_RENDER_HARNESS = (
    Path(__file__).resolve().parent / "js" / "system_diagnostics_render_test.mjs"
)
_MAIN_JS = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "assets" / "system-status" / "js" / "main.js"
)
_AUDIO_SECTIONS_JS = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "assets" / "system-status" / "js" / "audio-sections.js"
)
_OPTIONAL_FEATURES_JS = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "assets" / "system-status" / "js"
    / "optional-features-card.js"
)


def _http_get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_status_navigation_runtime_contract() -> None:
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_NAV_HARNESS), str(_MAIN_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ok": True}


def test_audio_sections_runtime_contract() -> None:
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_AUDIO_HARNESS), str(_AUDIO_SECTIONS_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ok": True}


def test_optional_features_runtime_contract() -> None:
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_OPTIONAL_FEATURES_HARNESS), str(_OPTIONAL_FEATURES_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ok": True}


def test_transport_park_card_runtime_contract() -> None:
    """The park card's model, its body, and where its section lands.

    Takes views.js too: the card's PLACEMENT (beside the audio alert, above
    the vitals grid) is behaviour, and pinning it by building the panel beats
    grepping the source for an argument list.
    """
    if _NODE is None:
        pytest.skip("node not on PATH")
    js_dir = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "assets" / "system-status" / "js"
    )
    proc = subprocess.run(
        [
            _NODE,
            str(_TRANSPORT_PARK_HARNESS),
            str(js_dir / "sections.js"),
            str(js_dir / "views.js"),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ok": True}


def _csrf_post(
    url: str,
    *,
    data: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """POST after acquiring the dashboard's CSRF cookie and page token."""

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
    request_headers = {"X-CSRF-Token": token, **(headers or {})}
    req = urllib.request.Request(
        url, data=data, method="POST", headers=request_headers,
    )
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _http_post(url: str) -> tuple[int, bytes]:
    return _csrf_post(url)


def _http_post_json(url: str, payload: dict[str, Any]) -> tuple[int, bytes]:
    return _csrf_post(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )


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
            "audio_health": {
                "schema_version": 1,
                "sampled_at": 1_750_000_000.0,
                "overall": {
                    "status": "ok",
                    "headline": "Audio is ready",
                    "detail": "The shared output path is healthy.",
                    "active_source": None,
                    "since": None,
                },
                "signal_path": {
                    "status": "ok",
                    "headline": "Output path ready",
                    "detail": "Fan-in, processing, and final output are available.",
                },
                "latency": {
                    "applicable": False,
                    "source_id": None,
                    "kind": "none",
                    "status": "idle",
                    "headline": "Not applicable",
                    "detail": "No low-latency route is active.",
                },
                "sources": [
                    {
                        "id": "airplay", "label": "AirPlay", "state": "idle",
                        "status": "idle", "headline": "Ready for a sender",
                        "detail": "Receiver timing is healthy.",
                        "timing": {
                            "kind": "sync", "status": "ok",
                            "headline": "Sync timing healthy", "detail": "",
                        },
                    },
                ],
                "issues": [],
                "technical": {"airplay": {"status": "ok"}},
            },
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
            "voice_provider": "gemini",
            "usb_gadget_forensics": {"enabled": False, "running": False,
                                       "ram_cap_bytes": 524288},
        },
        "/system/diagnostics": {
            "fails": 0, "warns": 0, "results": [
                {"name": "env_file", "status": "ok", "detail": "/etc/jasper/jasper.env present"},
            ],
        },
        "/aec/enhanced-aec": {
            "schema_version": 1,
            "feature": "enhanced_aec",
            "state": "not_installed",
            "requested": False,
            "installed": False,
            "current": False,
            "summary": "Standard echo cancellation is active.",
            "detail": "",
            "last_error": "",
            "desired_fingerprint": "abc123",
            "installed_fingerprint": "",
            "engine": "v1",
            "action": {
                "enabled": True,
                "label": "Install enhancement",
                "reason": "",
            },
        },
        "/aec/enhanced-aec/install": {
            "schema_version": 1,
            "feature": "enhanced_aec",
            "state": "installing",
            "requested": True,
            "installed": False,
            "current": False,
            "summary": "Installing in the background.",
            "detail": "",
            "last_error": "",
            "desired_fingerprint": "abc123",
            "installed_fingerprint": "",
            "engine": "v1",
            "action": {
                "enabled": False,
                "label": "Installing…",
                "reason": "installation_in_progress",
            },
        },
        "/system/restart/voice": {"ok": True, "action": "restart-voice"},
        "/system/restart/audio": {"ok": True, "action": "restart-audio"},
        "/system/audio-quality": {"ok": True, "action": "audio-quality"},
        "/usb-forensics": {"enabled": True, "running": False,
                             "ram_cap_bytes": 524288},
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
            # Record any forwarded control token so a test can assert the
            # wizard relays the browser's X-JTS-Token.
            token = self.headers.get("X-JTS-Token")
            if token is not None:
                received.append(("X-JTS-Token", token))
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
    assert 'data-view="system"' in text
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


def test_audio_route_serves_same_shell_with_audio_view_marker(dashboard_server) -> None:
    base, _received, _ = dashboard_server
    status, body = _http_get(f"{base}/audio/")
    assert status == 200
    text = body.decode("utf-8")
    assert "<title>Status</title>" in text
    assert 'id="app" data-view="audio"' in text
    assert '<script type="module" src="/assets/system-status/js/main.js">' in text
    assert "/assets/system-status/system.css?v=" in text


def test_render_page_fails_closed_to_known_view_marker() -> None:
    text = system_setup._render_page(view='audio" onmouseover="bad').decode()
    assert 'data-view="system"' in text
    assert "onmouseover" not in text


def test_data_json_proxies_snapshot(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, body = _http_get(f"{base}/data.json")
    assert status == 200
    payload = json.loads(body)
    assert payload["build"]["JASPER_GIT_SHA"] == "abc1234"
    assert payload["voice_provider"] == "gemini"
    assert payload["audio_health"]["overall"]["headline"] == "Audio is ready"
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


def test_enhanced_aec_status_proxies_versioned_contract(
    dashboard_server,
) -> None:
    base, received, _ = dashboard_server
    status, body = _http_get(
        f"{base}/optional-features/enhanced-aec",
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    assert payload["feature"] == "enhanced_aec"
    assert payload["state"] == "not_installed"
    assert payload["action"]["label"] == "Install enhancement"
    assert ("GET", "/aec/enhanced-aec") in received


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


def test_post_usb_latency_proxies_json_body(dashboard_server) -> None:
    base, received, responses = dashboard_server
    responses["/system/usb-latency"] = {
        "ok": True,
        "action": "usb-latency",
        "mode": "medium",
    }
    status, body = _http_post_json(
        f"{base}/usb-latency",
        {"mode": "medium"},
    )
    assert status == 200
    assert json.loads(body)["received_body"] == {"mode": "medium"}
    assert ("POST", "/system/usb-latency") in received


def test_post_usb_forensics_proxies_json_body(dashboard_server) -> None:
    base, received, _ = dashboard_server
    status, body = _http_post_json(
        f"{base}/usb-forensics", {"action": "set_enabled", "enabled": True},
    )
    assert status == 200
    assert json.loads(body)["received_body"] == {
        "action": "set_enabled", "enabled": True,
    }
    assert ("POST", "/usb-forensics") in received


def test_post_enhanced_aec_install_proxies_json_body(
    dashboard_server,
) -> None:
    base, received, _ = dashboard_server
    status, body = _http_post_json(
        f"{base}/optional-features/enhanced-aec/install", {},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    assert payload["state"] == "installing"
    assert payload["received_body"] == {}
    assert ("POST", "/aec/enhanced-aec/install") in received


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


def _http_post_with_token(url: str, token: str) -> tuple[int, bytes]:
    """_http_post + an X-JTS-Token header (the browser-supplied control
    token the wizard must forward to jasper-control)."""
    return _csrf_post(url, headers={"X-JTS-Token": token})


def test_reboot_forwards_control_token_to_upstream(dashboard_server) -> None:
    """The /system/ wizard proxies server-side, so a browser-supplied
    X-JTS-Token must be forwarded to jasper-control or the control-token gate
    would 403 the dashboard."""
    base, received, _ = dashboard_server
    status, _ = _http_post_with_token(f"{base}/reboot", "household-secret")
    assert status == 200
    assert ("POST", "/system/reboot") in received
    assert ("X-JTS-Token", "household-secret") in received


def test_reboot_without_token_forwards_no_token(dashboard_server) -> None:
    """Default-off: no X-JTS-Token on the browser request -> the wizard
    forwards none (no header injected from disk)."""
    base, received, _ = dashboard_server
    status, _ = _http_post(f"{base}/reboot")
    assert status == 200
    assert ("POST", "/system/reboot") in received
    assert not any(k == "X-JTS-Token" for k, _ in received)


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


_ASSETS_DIR = Path(__file__).resolve().parent.parent / "deploy" / "assets"
_MODULE_DIR = _ASSETS_DIR / "system-status" / "js"

_SHARED_HTTP_JS = _ASSETS_DIR / "shared" / "js" / "http.js"
_SHARED_DOM_JS = _ASSETS_DIR / "shared" / "js" / "dom.js"


# The /system/ UI is a layered set of static ES modules. These guards scan
# the combined module text so they survive refactors that move a string from
# one module to another (only the layout, not the behaviour, should change).
# The text-node DOM builder (h()/svg()) is no longer a per-page module — it
# was promoted to the shared /assets/shared/js/dom.js owner — so it is folded
# in via _system_js() below rather than listed here.
_EXPECTED_MODULES = (
    "format", "charts", "components", "sections", "audio-sections", "audio-view",
    "views", "usb-forensics-card", "optional-features-card", "actions",
    "main",
)


def _system_js() -> str:
    parts = [(_MODULE_DIR / f"{name}.js").read_text() for name in _EXPECTED_MODULES]
    parts.append(_SHARED_HTTP_JS.read_text())
    parts.append(_SHARED_DOM_JS.read_text())
    return "\n".join(parts)


def test_static_modules_present() -> None:
    """The /system/ UI ships as static ES modules (served + revalidated by
    nginx, copied by install.sh). Every layer must exist in the repo."""
    for name in _EXPECTED_MODULES:
        assert (_MODULE_DIR / f"{name}.js").is_file(), f"missing module {name}.js"


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_usb_latency_control_reports_recovery_and_apply_failure() -> None:
    subprocess.run(
        [
            _NODE,
            str(_LATENCY_HARNESS),
            str(_MODULE_DIR / "sections.js"),
            str(_MODULE_DIR / "actions.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_diagnostics_render_runtime_contract() -> None:
    """SYS-2: the diagnostics table leads with its verdict and sorts rows
    fail, warn, skipped, ok."""
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_DIAGNOSTICS_RENDER_HARNESS), str(_MODULE_DIR / "actions.js")],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ok": True}


def test_system_view_surfaces_a_speaker_that_cannot_play() -> None:
    """A parked speaker must be visible on the page a household opens first.

    #2381: ``/state.audio_health`` carried the sentence but the System view
    rendered no audio surface at all, so a structurally-silent box looked
    exactly like an idle healthy one. The alert card is built hidden, keys off the backend's
    ``overall.status`` rather than any wording of its own, and sits above the
    vitals grid because a silent speaker outranks every metric below it.
    """
    views = (_MODULE_DIR / "views.js").read_text()
    audio_sections = (_MODULE_DIR / "audio-sections.js").read_text()

    assert re.search(
        r'import \{[^}]*\boutputAlert, outputAlertBody\b[^}]*\} '
        r'from "\./audio-sections\.js";',
        views,
    )
    assert 'const audioAlert = titledCard("Audio");' in views
    assert "audioAlert.section.hidden = true;" in views
    # First card in the panel: after the live pill, ahead of the vitals grid.
    assert "live.el, audioAlert.section," in views
    assert "outputAlert(snap.audio_health)" in views
    assert "refs.audioAlertSection.hidden = !alert;" in views
    # The alert comes from the audio-health sampler, not the metrics sampler,
    # so it must not sit behind the metrics warm-up gate — a box that cannot
    # play should not wait on a sparkline to say so.
    assert views.index("outputAlert(snap.audio_health)") < views.index(
        "if (hasMetrics) {"
    ), "the audio alert must render before/outside the metrics warm-up gate"

    # One writer for the parked sentence (jasper/control/audio_signal_path.py's
    # parked_signal). The browser decides whether to show it, never what it
    # says — a copy here is a drift site, not a convenience.
    from jasper.control.audio_signal_path import PARKED_HEADLINE

    js = _system_js()
    assert PARKED_HEADLINE not in js
    assert 'const OUTPUT_ALERT_STATUS = "issue";' in audio_sections


def test_unknown_route_404(dashboard_server) -> None:
    base, _, _ = dashboard_server
    status, _ = _http_get(f"{base}/nope")
    assert status == 404
    status, _ = _http_get(f"{base}/audio/nope")
    assert status == 404


def test_aec_card_moved_to_wake(dashboard_server) -> None:
    """The Wake detection card moved to /assistant/wake/. /system/ must no
    longer serve the routes that backed it — /aec.json,
    /aec/leg, /aec/threshold all 404 here, and the HTML must not
    reference the old DOM ids the card's JS bound to."""
    base, received, _ = dashboard_server
    for route in ("/aec.json",):
        status, _ = _http_get(f"{base}{route}")
        assert status == 404, f"{route} should be gone from /system/"
    for route in ("/aec/leg", "/aec/threshold"):
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
