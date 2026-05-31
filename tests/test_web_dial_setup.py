"""Hardware-free tests for the /dial/ rotary-dial onboarding wizard.

Mirrors the structure of the other ``tests/test_web_*_setup.py`` suites:
render-surface assertions (canonical design system bytes) plus handler
behaviour with the pyserial enumeration and the jasper-dial-onboard
subprocess mocked. No USB device, no ESP32, no network is touched.
"""
from __future__ import annotations

import io
import re
from typing import Any
from unittest import mock

import pytest

from jasper.web import dial_setup


# ---------------------------------------------------------------------------
# Firmware status fixtures — the three states the setup page renders.
# ---------------------------------------------------------------------------

_FW_READY: dict[str, Any] = {
    "present": True,
    "path": "/opt/jasper/firmware/dial/jasper-dial.bin",
    "size_bytes": 712 * 1024,
    "mtime_iso": "2026-05-30 10:00 UTC",
    "source_newer": False,
    "source_mtime_iso": "2026-05-29 09:00 UTC",
}
_FW_SOURCE_NEWER: dict[str, Any] = {**_FW_READY, "source_newer": True}
_FW_MISSING: dict[str, Any] = {
    "present": False,
    "path": "/opt/jasper/firmware/dial/jasper-dial.bin",
    "size_bytes": None,
    "mtime_iso": None,
    "source_newer": False,
    "source_mtime_iso": None,
}

_TOKEN = "tok-abcdefghijklmnopqrstuvwxyz012345"


# ---------------------------------------------------------------------------
# Render surface — canonical design system.
# ---------------------------------------------------------------------------


def test_landing_uses_canonical_design_system() -> None:
    html = dial_setup._landing_html(_TOKEN)
    assert html.lower().startswith(b"<!doctype html>")
    assert b"/assets/app.css" in html
    assert b"app-header" in html
    assert b'name="jts-csrf"' in html
    # The token value is interpolated into the meta tag, not the literal
    # variable name.
    assert _TOKEN.encode() in html
    assert b"csrf_token" not in html


def test_landing_has_continue_link_and_no_js() -> None:
    html = dial_setup._landing_html(_TOKEN)
    assert b'href="setup"' in html
    # The landing page is static — it must not pull in the setup module.
    assert b"/assets/dial/js/main.js" not in html


def test_setup_uses_canonical_design_system() -> None:
    html = dial_setup._setup_html(
        ssid="HomeNet", firmware=_FW_READY, csrf_token=_TOKEN,
    )
    assert html.lower().startswith(b"<!doctype html>")
    assert b"/assets/app.css" in html
    assert b"app-header" in html
    assert b'name="jts-csrf"' in html
    assert _TOKEN.encode() in html


def test_setup_loads_es_module_and_page_css() -> None:
    html = dial_setup._setup_html(
        ssid="HomeNet", firmware=_FW_READY, csrf_token=_TOKEN,
    )
    assert b'<script type="module" src="/assets/dial/js/main.js">' in html
    assert b"/assets/dial/dial.css" in html
    # The behaviour lives in the module — no inline <script> with logic.
    assert b"function provision" not in html
    assert b"setInterval" not in html


def test_setup_has_scan_and_result_mount_points() -> None:
    html = dial_setup._setup_html(
        ssid="HomeNet", firmware=_FW_READY, csrf_token=_TOKEN,
    )
    assert b'id="status"' in html
    assert b'id="devices"' in html
    assert b'id="result"' in html


def test_setup_shows_ssid_and_placeholder() -> None:
    with_ssid = dial_setup._setup_html(
        ssid="MyWiFi", firmware=_FW_READY, csrf_token=_TOKEN,
    )
    assert b"MyWiFi" in with_ssid
    without_ssid = dial_setup._setup_html(
        ssid="", firmware=_FW_READY, csrf_token=_TOKEN,
    )
    assert b"check Pi WiFi" in without_ssid


# ---------------------------------------------------------------------------
# Firmware banner state machine (ready / source-newer / not-staged).
# ---------------------------------------------------------------------------


def test_firmware_banner_ready() -> None:
    out = dial_setup._firmware_banner_html(_FW_READY)
    assert 'class="fw-banner ok"' in out
    assert "Firmware ready to flash" in out
    assert "712 KB" in out
    assert "jasper-dial.bin" in out


def test_firmware_banner_source_newer() -> None:
    out = dial_setup._firmware_banner_html(_FW_SOURCE_NEWER)
    assert 'class="fw-banner warn"' in out
    assert "source is newer" in out
    assert "build.sh" in out


def test_firmware_banner_not_staged() -> None:
    out = dial_setup._firmware_banner_html(_FW_MISSING)
    assert 'class="fw-banner warn"' in out
    assert "No firmware staged" in out
    assert "build.sh" in out


def test_firmware_banner_escapes_path() -> None:
    hostile = {**_FW_READY, "path": "/tmp/<script>x</script>"}
    out = dial_setup._firmware_banner_html(hostile)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ---------------------------------------------------------------------------
# Handler routing + behaviour. Drive the stdlib handler with stubbed I/O
# streams (the documented BaseHTTPRequestHandler test pattern) so no socket
# server runs; mock the pyserial scan and the onboard subprocess.
# ---------------------------------------------------------------------------


def _drive(method: str, path: str, *, body: bytes = b"", headers: str = "") -> tuple[int, bytes]:
    """Run one request through the handler and return (status, raw response).

    Builds a raw HTTP request, feeds it to a Handler whose socket I/O is
    replaced with BytesIO, and parses the status line out of the response.
    """
    handler_cls = dial_setup._make_handler()
    extra = headers
    if body:
        extra += f"Content-Length: {len(body)}\r\n"
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"{extra}"
        f"\r\n"
    ).encode() + body

    handler = handler_cls.__new__(handler_cls)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 0)
    handler.request_version = "HTTP/1.1"
    # Parse the request line/headers the way BaseHTTPRequestHandler does,
    # then dispatch the verb method directly.
    handler.raw_requestline = handler.rfile.readline()
    handler.parse_request()
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    resp = handler.wfile.getvalue()
    status_match = re.match(rb"HTTP/1\.\d (\d{3})", resp)
    status = int(status_match.group(1)) if status_match else 0
    return status, resp


def test_make_handler_returns_class() -> None:
    handler_cls = dial_setup._make_handler()
    assert handler_cls is not None
    assert hasattr(handler_cls, "do_GET")
    assert hasattr(handler_cls, "do_POST")


def test_get_landing_route() -> None:
    status, resp = _drive("GET", "/")
    assert status == 200
    assert b"/assets/app.css" in resp
    assert b"app-header" in resp


def test_get_setup_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # /setup reads the Pi SSID and firmware status; keep both local.
    monkeypatch.setattr(dial_setup, "_read_pi_ssid", lambda: "HomeNet")
    monkeypatch.setattr(dial_setup, "_read_firmware_status", lambda: _FW_READY)
    status, resp = _drive("GET", "/setup")
    assert status == 200
    assert b"/assets/dial/js/main.js" in resp
    assert b"fw-banner ok" in resp
    assert b"HomeNet" in resp


def test_get_scan_route_returns_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = [{
        "port": "/dev/ttyACM0", "vid": "0x303a", "pid": "0x1001",
        "serial": "ABC123", "description": "USB JTAG/serial",
    }]
    monkeypatch.setattr(dial_setup, "_list_esp32_s3_ports", lambda: fake)
    status, resp = _drive("GET", "/scan")
    assert status == 200
    assert b"/dev/ttyACM0" in resp
    assert b'"devices"' in resp


def test_get_unknown_route_404() -> None:
    status, _ = _drive("GET", "/nope")
    assert status == 404


def test_post_onboard_rejects_missing_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    # No CSRF cookie/header → reject before doing anything.
    monkeypatch.setattr(dial_setup, "verify_csrf", lambda h: False)
    called = mock.Mock()
    monkeypatch.setattr(dial_setup, "_run_onboard", called)
    status, _ = _drive(
        "POST", "/onboard", body=b'{"port":"/dev/ttyACM0"}',
        headers="Content-Type: application/json\r\n",
    )
    assert status == 403
    called.assert_not_called()


def test_post_onboard_rejects_bad_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dial_setup, "verify_csrf", lambda h: True)
    called = mock.Mock()
    monkeypatch.setattr(dial_setup, "_run_onboard", called)
    status, resp = _drive(
        "POST", "/onboard", body=b'{"port":"not-a-dev-path"}',
        headers="Content-Type: application/json\r\n",
    )
    assert status == 400
    assert b"invalid port" in resp
    called.assert_not_called()


def test_post_onboard_rejects_unplugged_port(monkeypatch: pytest.MonkeyPatch) -> None:
    # Valid-looking /dev path, but not in the currently-plugged set — the
    # crafted-POST guard must reject before running esptool.
    monkeypatch.setattr(dial_setup, "verify_csrf", lambda h: True)
    monkeypatch.setattr(dial_setup, "_list_esp32_s3_ports", lambda: [])
    called = mock.Mock()
    monkeypatch.setattr(dial_setup, "_run_onboard", called)
    status, resp = _drive(
        "POST", "/onboard", body=b'{"port":"/dev/ttyACM9"}',
        headers="Content-Type: application/json\r\n",
    )
    assert status == 400
    assert b"not a recognized ESP32-S3 device" in resp
    called.assert_not_called()


def test_post_onboard_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dial_setup, "verify_csrf", lambda h: True)
    monkeypatch.setattr(
        dial_setup, "_list_esp32_s3_ports",
        lambda: [{"port": "/dev/ttyACM0", "vid": "0x303a",
                  "pid": "0x1001", "serial": "", "description": ""}],
    )
    run = mock.Mock(return_value={
        "ok": True, "message": "Dial provisioned and online.", "log": "...",
    })
    monkeypatch.setattr(dial_setup, "_run_onboard", run)
    status, resp = _drive(
        "POST", "/onboard", body=b'{"port":"/dev/ttyACM0","force_flash":false}',
        headers="Content-Type: application/json\r\n",
    )
    assert status == 200
    assert b"provisioned and online" in resp
    run.assert_called_once_with("/dev/ttyACM0", force_flash=False)


def test_post_onboard_failure_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dial_setup, "verify_csrf", lambda h: True)
    monkeypatch.setattr(
        dial_setup, "_list_esp32_s3_ports",
        lambda: [{"port": "/dev/ttyACM0", "vid": "0x303a",
                  "pid": "0x1001", "serial": "", "description": ""}],
    )
    run = mock.Mock(return_value={
        "ok": False, "error": "jasper-dial-onboard exit code 1", "log": "boom",
    })
    monkeypatch.setattr(dial_setup, "_run_onboard", run)
    status, resp = _drive(
        "POST", "/onboard", body=b'{"port":"/dev/ttyACM0","force_flash":true}',
        headers="Content-Type: application/json\r\n",
    )
    assert status == 502
    run.assert_called_once_with("/dev/ttyACM0", force_flash=True)


def test_post_unknown_route_404(monkeypatch: pytest.MonkeyPatch) -> None:
    status, _ = _drive("POST", "/nope")
    assert status == 404


# ---------------------------------------------------------------------------
# Onboard subprocess wrapper — fail-soft branches (no real esptool).
# ---------------------------------------------------------------------------


def test_run_onboard_smart_uses_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return mock.Mock(returncode=0, stdout="provisioned", stderr="")

    monkeypatch.setattr(dial_setup.subprocess, "run", fake_run)
    result = dial_setup._run_onboard("/dev/ttyACM0", force_flash=False)
    assert result["ok"] is True
    assert "--auto" in captured["cmd"]
    assert "--flash" not in captured["cmd"]


def test_run_onboard_force_uses_flash(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return mock.Mock(returncode=0, stdout="flashed", stderr="")

    monkeypatch.setattr(dial_setup.subprocess, "run", fake_run)
    result = dial_setup._run_onboard("/dev/ttyACM0", force_flash=True)
    assert result["ok"] is True
    assert "--flash" in captured["cmd"]


def test_run_onboard_short_circuit_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kw):
        return mock.Mock(
            returncode=0, stdout="auto mode short-circuit: already online", stderr="",
        )

    monkeypatch.setattr(dial_setup.subprocess, "run", fake_run)
    result = dial_setup._run_onboard("/dev/ttyACM0", force_flash=False)
    assert result["ok"] is True
    assert "already online" in result["message"]


def test_run_onboard_timeout_is_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kw):
        raise dial_setup.subprocess.TimeoutExpired(cmd=cmd, timeout=180)

    monkeypatch.setattr(dial_setup.subprocess, "run", fake_run)
    result = dial_setup._run_onboard("/dev/ttyACM0", force_flash=False)
    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_run_onboard_oserror_is_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kw):
        raise OSError("no such binary")

    monkeypatch.setattr(dial_setup.subprocess, "run", fake_run)
    result = dial_setup._run_onboard("/dev/ttyACM0", force_flash=False)
    assert result["ok"] is False
    assert "could not run" in result["error"]


# ---------------------------------------------------------------------------
# pyserial enumeration — fail-soft when pyserial is absent.
# ---------------------------------------------------------------------------


def test_list_ports_soft_without_pyserial(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kw):
        if name.startswith("serial"):
            raise ImportError("no pyserial")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert dial_setup._list_esp32_s3_ports() == []


# ---------------------------------------------------------------------------
# Public surface — names other code / the runner depend on.
# ---------------------------------------------------------------------------


def test_module_exposes_public_surface() -> None:
    assert hasattr(dial_setup, "_landing_html")
    assert hasattr(dial_setup, "_setup_html")
    assert hasattr(dial_setup, "_make_handler")
    assert hasattr(dial_setup, "main")
    assert hasattr(dial_setup, "_run_onboard")
    assert hasattr(dial_setup, "_list_esp32_s3_ports")
    assert hasattr(dial_setup, "_read_firmware_status")
