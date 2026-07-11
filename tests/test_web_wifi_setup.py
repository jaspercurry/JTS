# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /wifi/ wizard after its migration to the canonical look.

1. The page renders canonical design-system bytes (links /assets/app.css and
   the per-page /assets/wifi/wifi.css, carries the shared .app-header, embeds
   the CSRF meta tag) and delivers its behaviour as an ES module -- no inline
   <script>.
2. The migration was presentation-only: the live JSON endpoints
   (/state, /scan, /connect, /forget, /radio), the connect rollback / lockout
   logic, and the public module surface (render fn, make_server, main) are
   unchanged. nmcli is mocked so nothing shells out.
"""
from __future__ import annotations

import http
import json
import subprocess
from email.message import Message
from io import BytesIO

from jasper.web import wifi_setup


def _render(csrf_token: str = "tok-abcdefghijklmnopqrstuvwx") -> str:
    return wifi_setup._landing_html(csrf_token).decode()


# --------------------------------------------------------------------------
# Canonical render
# --------------------------------------------------------------------------


def test_wifi_page_is_canonical_document():
    out = _render()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    # The old hand-rolled shell and inline page styling are gone.
    assert "max-width: 720px" not in out
    assert "TOGGLE" "_CSS" not in out
    assert "#1db954" not in out


def test_wifi_page_links_page_css():
    assert "/assets/wifi/wifi.css?v=" in _render()


def test_wifi_page_has_shared_app_header():
    out = _render()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Wi-Fi</h1>' in out
    assert '<use href="#icon-back">' in out


def test_wifi_page_embeds_csrf_meta():
    out = _render()
    assert 'meta name="jts-csrf"' in out
    assert 'content="tok-abcdefghijklmnopqrstuvwx"' in out


def test_wifi_page_loads_es_module_not_inline_script():
    out = _render()
    assert '<script type="module" src="/assets/wifi/js/main.js">' in out
    before_module = out.split('<script type="module"')[0]
    # All behaviour (incl. the radio-kill confirm) lives in the module now.
    assert "jtsConfirm(" not in before_module
    assert "addEventListener" not in before_module
    assert "function rescan" not in before_module


def test_wifi_page_keeps_runtime_element_ids():
    # The ES module keys off these ids; the static shell must still ship them.
    out = _render()
    for el_id in (
        'id="current"', 'id="scan-btn"', 'id="scan-health"', 'id="avail-list"',
        'id="manual-ssid"', 'id="manual-password"', 'id="manual-hidden"',
        'id="manual-result"', 'id="manual-connect-btn"',
        'id="saved-list"', 'id="saved-count"',
    ):
        assert el_id in out, el_id


def test_wifi_page_uses_data_actions_not_inline_onclick():
    out = _render()
    assert "onclick=" not in out
    assert 'data-action="rescan"' in out
    assert 'data-action="submit-manual"' in out
    assert 'data-action="toggle-manual-pw"' in out


def test_wifi_page_has_no_server_form():
    # /wifi/ is a fetch/JSON page; there is no server-rendered <form> (every
    # mutation is an X-CSRF-Token POST from the module). So there's correctly
    # no hidden csrf_token field either.
    out = _render()
    assert "<form" not in out


# --------------------------------------------------------------------------
# Backend behaviour preserved (nmcli mocked)
# --------------------------------------------------------------------------


def test_public_surface_is_stable():
    assert callable(wifi_setup._landing_html)
    assert callable(wifi_setup.make_server)
    assert callable(wifi_setup.main)
    assert callable(wifi_setup.gather_state)
    assert callable(wifi_setup.connect_new)
    assert callable(wifi_setup.connect_saved)
    assert callable(wifi_setup.forget)
    assert callable(wifi_setup.set_radio)
    assert callable(wifi_setup.scan_networks_report)


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_gather_state_shape(monkeypatch):
    # No real nmcli: every probe returns a clean "wifi adapter present, radio
    # on, no ethernet, not connected, no saved" world.
    def fake_run(cmd, *, timeout=10, log_argv=True):
        fields = cmd[cmd.index("-f") + 1] if "-f" in cmd else ""
        if fields == "TYPE":
            return _completed(cmd, stdout="wifi\n")
        if fields == "TYPE,STATE":
            return _completed(cmd, stdout="wifi:connected\n")  # no ethernet row
        if cmd[:3] == ["nmcli", "radio", "wifi"]:
            return _completed(cmd, stdout="enabled\n")
        return _completed(cmd, stdout="")

    monkeypatch.setattr(wifi_setup, "_run_nmcli", fake_run)
    st = wifi_setup.gather_state()
    assert set(st) == {
        "adapterPresent", "radioOn", "hasEthernet",
        "lockoutRisk", "current", "saved",
    }
    assert st["adapterPresent"] is True
    assert st["radioOn"] is True
    assert st["hasEthernet"] is False
    assert st["lockoutRisk"] == "high"


def test_connect_new_rolls_back_on_failure(monkeypatch):
    """The lockout-critical path: a failed connect must (a) delete the broken
    new profile and (b) bring the previously-active profile back up."""
    calls = []

    monkeypatch.setattr(
        wifi_setup, "_current_wifi",
        lambda: {"profileName": "HomeNet", "ssid": "HomeNet"},
    )
    monkeypatch.setattr(wifi_setup, "_profile_exists", lambda name: False)
    monkeypatch.setattr(wifi_setup, "_stash_after_connect", lambda *a, **k: None)

    def fake_secret(cmd, *, timeout=10):
        calls.append(("secret", list(cmd)))
        # connect attempt fails with a non-SSID-lookup error
        return _completed(cmd, returncode=4, stderr="Error: Connection activation failed.")

    def fake_run(cmd, *, timeout=10, log_argv=True):
        calls.append(("run", list(cmd)))
        return _completed(cmd, returncode=0, stdout="")

    monkeypatch.setattr(wifi_setup, "_run_nmcli_secret", fake_secret)
    monkeypatch.setattr(wifi_setup, "_run_nmcli", fake_run)

    ok, msg = wifi_setup.connect_new("BadNet", "secretpw")
    assert ok is False
    # broken profile deleted (didn't exist before) ...
    assert any(c[1][:4] == ["nmcli", "connection", "delete", "BadNet"]
               for c in calls if c[0] == "run")
    # ... and the previous profile brought back up.
    assert any("connection" in c[1] and "up" in c[1] and "HomeNet" in c[1]
               for c in calls if c[0] == "run")
    assert "HomeNet" in msg


def test_readable_nmcli_error_scrubs_echoed_psk():
    # nmcli can echo the submitted password back in error text; it must
    # never survive into the string that is logged AND returned to the
    # browser. Both scrub patterns: literal PSK and `password <arg>`.
    psk = "hunter2secret"
    proc = _completed(
        ["nmcli"], returncode=4,
        stderr=f"Error: 802-11-wireless-security.psk: '{psk}' invalid; password {psk}",
    )
    msg = wifi_setup._readable_nmcli_error(proc, psk)
    assert psk not in msg
    assert "***" in msg


def test_readable_nmcli_error_scrubs_password_token_without_literal():
    # Even if we don't have the literal PSK, `password <arg>` echo is masked.
    proc = _completed(
        ["nmcli"], returncode=4, stderr="Error: password abc123def not accepted",
    )
    msg = wifi_setup._readable_nmcli_error(proc, None)
    assert "abc123def" not in msg
    assert "password ***" in msg


def test_connect_new_scrubs_psk_from_returned_message(monkeypatch):
    psk = "TopSecretWifiPass"
    monkeypatch.setattr(
        wifi_setup, "_current_wifi",
        lambda: {"profileName": "HomeNet", "ssid": "HomeNet"},
    )
    monkeypatch.setattr(wifi_setup, "_profile_exists", lambda name: False)
    monkeypatch.setattr(wifi_setup, "_stash_after_connect", lambda *a, **k: None)

    def fake_secret(cmd, *, timeout=10):
        # nmcli echoes the PSK back in its failure text.
        return _completed(
            cmd, returncode=4,
            stderr=f"Error: secrets were required but not provided: password {psk}",
        )

    monkeypatch.setattr(wifi_setup, "_run_nmcli_secret", fake_secret)
    monkeypatch.setattr(wifi_setup, "_run_nmcli", lambda *a, **k: _completed(["nmcli"]))

    ok, msg = wifi_setup.connect_new("MyNet", psk)
    assert ok is False
    assert psk not in msg


def test_set_radio_passes_on_off(monkeypatch):
    seen = []

    def fake_run(cmd, *, timeout=10, log_argv=True):
        seen.append(list(cmd))
        return _completed(cmd, returncode=0)

    monkeypatch.setattr(wifi_setup, "_run_nmcli", fake_run)
    ok, _ = wifi_setup.set_radio(False)
    assert ok is True
    assert ["nmcli", "radio", "wifi", "off"] == seen[-1]


# --------------------------------------------------------------------------
# Handler routes (no real server / nmcli)
# --------------------------------------------------------------------------


def _make_request(path: str, body: bytes = b"", cookies: str = "",
                  csrf_header: str = ""):
    """Build a real wifi Handler instance wired to a synthetic request.

    The Handler defines its response helpers (_send / _send_json / _read_json)
    as instance methods, so we instantiate the *real* class (via __new__, to
    skip BaseHTTPRequestHandler.__init__'s socket plumbing) and bolt the request
    I/O onto it. Returns (handler, captured) where ``captured`` exposes
    ``.status`` and ``.body`` after do_GET/do_POST runs."""
    handler_cls = wifi_setup._make_handler()
    h = handler_cls.__new__(handler_cls)
    h.path = path
    h.headers = Message()
    h.headers["Content-Length"] = str(len(body))
    h.headers["Content-Type"] = "application/json"
    if cookies:
        h.headers["Cookie"] = cookies
    if csrf_header:
        h.headers["X-CSRF-Token"] = csrf_header
    h.rfile = BytesIO(body)
    h.wfile = BytesIO()
    h.client_address = ("127.0.0.1", 0)

    captured = {"status": None, "headers": []}
    # Override the network-touching surface of BaseHTTPRequestHandler so the
    # real helper methods (_send_json etc.) run without a socket.
    h.send_response = lambda status, *a, **k: captured.__setitem__("status", int(status))
    h.send_response_only = h.send_response
    h.send_header = lambda name, value: captured["headers"].append((name, value))
    h.end_headers = lambda: None
    h.send_error = lambda status, *a, **k: captured.__setitem__("status", int(status))
    h.log_message = lambda *a, **k: None
    return h, captured


def test_get_root_renders_canonical_page():
    h, cap = _make_request("/")
    h.do_GET()
    assert cap["status"] == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out
    assert '<script type="module" src="/assets/wifi/js/main.js">' in out


def test_get_state_returns_json(monkeypatch):
    monkeypatch.setattr(
        wifi_setup, "gather_state",
        lambda: {"adapterPresent": True, "radioOn": False, "hasEthernet": False,
                 "lockoutRisk": "high", "current": None, "saved": []},
    )
    h, cap = _make_request("/state")
    h.do_GET()
    assert cap["status"] == 200
    assert json.loads(h.wfile.getvalue().decode())["lockoutRisk"] == "high"


def test_post_unknown_route_404s():
    h, cap = _make_request("/nope", body=b"{}")
    h.do_POST()
    assert cap["status"] == int(http.HTTPStatus.NOT_FOUND)


def test_post_scan_rejects_bad_csrf():
    # No cookie / no header -> guard_mutating_request fails -> 403, and the scan never runs.
    h, cap = _make_request("/scan", body=b"{}")
    h.do_POST()
    assert cap["status"] == int(http.HTTPStatus.FORBIDDEN)


def test_post_scan_runs_with_valid_csrf(monkeypatch):
    token = "t" * 64
    monkeypatch.setattr(
        wifi_setup, "scan_networks_report",
        lambda: {"networks": [], "scan": {"ok": True, "degraded": False}},
    )
    h, cap = _make_request("/scan", body=b"{}", cookies="jts_csrf=" + token,
                           csrf_header=token)
    h.do_POST()
    assert cap["status"] == 200
    assert json.loads(h.wfile.getvalue().decode())["scan"]["ok"] is True
