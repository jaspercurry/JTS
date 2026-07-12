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
import logging
import subprocess
from email.message import Message
from io import BytesIO
from pathlib import Path

import pytest

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


def test_connect_new_worst_path_matches_declared_timeout_ceiling(monkeypatch):
    """Drive the real serialized fail path without sleeping: current-profile
    reads, profile lookup, visible + hidden attempts, cleanup, and rollback."""
    timeouts: list[int] = []

    def fake_run(cmd, *, timeout=10, log_argv=True):
        timeouts.append(timeout)
        if cmd[-3:] == ["connection", "show", "--active"]:
            return _completed(cmd, stdout="Home:uuid:wifi:wlan0\n")
        return _completed(cmd, returncode=1, stderr="failed")

    def fake_secret(cmd, *, timeout=10):
        timeouts.append(timeout)
        return _completed(
            cmd,
            returncode=4,
            stderr="Error: No network with SSID 'MissingNet' found.",
        )

    monkeypatch.setattr(wifi_setup, "_run_nmcli", fake_run)
    monkeypatch.setattr(wifi_setup, "_run_nmcli_secret", fake_secret)

    ok, _ = wifi_setup.connect_new("MissingNet", "secretpw")

    assert ok is False
    assert timeouts == [5, 5, 5, 5, 5, 45, 45, 10, 30]
    assert sum(timeouts) == wifi_setup.CONNECT_NEW_TIMEOUT_CEILING


def test_wifi_ui_copy_matches_three_minute_proxy_contract() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/wifi/js/main.js"
    ).read_text()
    assert "90s" not in source
    assert source.count("up to 3 minutes including rollback") == 2
    assert "full switch and recovery attempt can take " in source
    assert "up to 3 minutes" in source


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


def test_nmcli_timeout_log_scrubs_psk_from_secret_argv(monkeypatch, caplog):
    psk = "timeout-secret-psk"
    cmd = [
        "nmcli",
        "device",
        "wifi",
        "connect",
        "HomeNet",
        "password",
        psk,
    ]

    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(wifi_setup.subprocess, "run", time_out)
    caplog.set_level(logging.INFO, logger=wifi_setup.logger.name)

    proc = wifi_setup._run_nmcli_secret(cmd, timeout=1)

    assert proc.returncode == 124
    assert psk not in caplog.text
    assert "password ***" in caplog.text


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

    captured = {"status": None, "responses": [], "headers": []}
    # Override the network-touching surface of BaseHTTPRequestHandler so the
    # real helper methods (_send_json etc.) run without a socket.
    def capture_response(status, *args, **kwargs):
        captured["status"] = int(status)
        captured["responses"].append(int(status))

    h.send_response = capture_response
    h.send_response_only = h.send_response
    h.send_header = lambda name, value: captured["headers"].append((name, value))
    h.end_headers = lambda: None
    h.send_error = lambda status, *a, **k: captured.__setitem__("status", int(status))
    h.log_message = lambda *a, **k: None
    return h, captured


class _TrackingReader(BytesIO):
    def __init__(self, initial_bytes: bytes, *, fail: bool = False) -> None:
        super().__init__(initial_bytes)
        self.fail = fail
        self.read_calls: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if self.fail:
            raise OSError("request body read failed")
        return super().read(size)


class _FailingWriter:
    def __init__(self) -> None:
        self.write_calls: list[bytes] = []

    def write(self, body: bytes) -> None:
        self.write_calls.append(body)
        raise BrokenPipeError("client disconnected during response body")


def _valid_post(path: str, body: bytes):
    token = "v" * 64
    return _make_request(
        path,
        body=body,
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )


def _event_records(caplog, event: str):
    logfmt_prefix = f"event={event}"
    return [
        record
        for record in caplog.records
        if record.getMessage().startswith(logfmt_prefix)
        or record.getMessage().startswith(f'{{"event": "{event}"')
    ]


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


def test_post_scan_preserves_body_agnostic_policy_for_invalid_json(monkeypatch):
    calls = []
    monkeypatch.setattr(
        wifi_setup,
        "scan_networks_report",
        lambda: calls.append("scan") or {
            "networks": [],
            "scan": {"ok": True, "degraded": False},
        },
    )
    h, captured = _valid_post("/scan", b"{")

    h.do_POST()

    assert captured["status"] == 200
    assert calls == ["scan"]


@pytest.mark.parametrize(("mode", "ok"), [("new", True), ("new", False),
                                            ("saved", True), ("saved", False)])
def test_post_connect_emits_one_redacted_action_event(
    monkeypatch,
    caplog,
    mode,
    ok,
):
    psk = "connect-action-secret"
    backend_message = "backend response detail must not be logged"
    caplog.set_level(logging.INFO, logger=wifi_setup.logger.name)

    if mode == "new":
        ssid = "Home\nGuest"
        body = json.dumps(
            {"ssid": ssid, "password": psk, "hidden": True},
        ).encode()

        def fake_connect_new(got_ssid, got_password, *, hidden=False):
            assert (got_ssid, got_password, hidden) == (ssid, psk, True)
            return ok, backend_message

        monkeypatch.setattr(wifi_setup, "connect_new", fake_connect_new)
    else:
        profile = "Saved Home"
        body = json.dumps({"name": profile}).encode()

        def fake_connect_saved(got_profile):
            assert got_profile == profile
            return ok, backend_message

        monkeypatch.setattr(wifi_setup, "connect_saved", fake_connect_saved)

    h, captured = _valid_post("/connect", body)
    h.do_POST()

    assert captured["status"] == (200 if ok else 502)
    assert json.loads(h.wfile.getvalue())["message"] == backend_message
    records = _event_records(caplog, "wifi.connect")
    assert len(records) == 1
    record = records[0]
    assert record.levelno == (logging.INFO if ok else logging.WARNING)
    if mode == "new":
        assert record.getMessage() == (
            "event=wifi.connect mode=new "
            f'ssid="Home\\nGuest" ok={str(ok).lower()} client=127.0.0.1'
        )
    else:
        assert record.getMessage() == (
            "event=wifi.connect mode=saved "
            f'profile="Saved Home" ok={str(ok).lower()} client=127.0.0.1'
        )
    assert record.getMessage().splitlines() == [record.getMessage()]
    assert psk not in caplog.text
    assert backend_message not in caplog.text


@pytest.mark.parametrize("ok", [True, False])
def test_post_forget_emits_one_action_event(monkeypatch, caplog, ok):
    backend_message = "forget backend response detail"
    monkeypatch.setattr(
        wifi_setup,
        "forget",
        lambda name: (ok, backend_message),
    )
    caplog.set_level(logging.INFO, logger=wifi_setup.logger.name)

    h, captured = _valid_post("/forget", b'{"name":"Guest profile"}')
    h.do_POST()

    assert captured["status"] == (200 if ok else 502)
    records = _event_records(caplog, "wifi.forget")
    assert len(records) == 1
    assert records[0].getMessage() == (
        f'event=wifi.forget profile="Guest profile" ok={str(ok).lower()} '
        "client=127.0.0.1"
    )
    assert records[0].levelno == (logging.INFO if ok else logging.WARNING)
    assert backend_message not in caplog.text


@pytest.mark.parametrize(("enabled", "ok"), [(True, True), (False, False)])
def test_post_radio_emits_one_action_event(
    monkeypatch,
    caplog,
    enabled,
    ok,
):
    backend_message = "radio backend response detail"
    calls = []

    def fake_set_radio(on):
        calls.append(on)
        return ok, backend_message

    monkeypatch.setattr(wifi_setup, "set_radio", fake_set_radio)
    caplog.set_level(logging.INFO, logger=wifi_setup.logger.name)
    body = json.dumps({"on": enabled}).encode()

    h, captured = _valid_post("/radio", body)
    h.do_POST()

    assert captured["status"] == (200 if ok else 502)
    assert calls == [enabled]
    records = _event_records(caplog, "wifi.radio")
    assert len(records) == 1
    assert records[0].getMessage() == (
        f"event=wifi.radio enabled={str(enabled).lower()} "
        f"ok={str(ok).lower()} client=127.0.0.1"
    )
    assert records[0].levelno == (logging.INFO if ok else logging.WARNING)
    assert backend_message not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("password psk-shaped-runtime-detail"),
        ConnectionResetError("password psk-shaped-reset-detail"),
    ],
)
def test_post_action_backend_exception_is_structured_and_generic(
    monkeypatch,
    caplog,
    error,
):
    private_message = str(error)

    def fail_connect(_name):
        raise error

    monkeypatch.setattr(wifi_setup, "connect_saved", fail_connect)
    caplog.set_level(logging.ERROR, logger=wifi_setup.logger.name)

    h, captured = _valid_post("/connect", b'{"name":"Saved Home"}')
    h.do_POST()

    assert captured["status"] == 502
    assert json.loads(h.wfile.getvalue()) == {
        "ok": False,
        "message": "Wi-Fi action failed",
    }
    records = _event_records(caplog, "wifi.post_dispatch_failed")
    assert len(records) == 1
    assert records[0].getMessage() == (
        "event=wifi.post_dispatch_failed action=connect "
        f"error={type(error).__name__} "
        "ok=false client=127.0.0.1"
    )
    assert records[0].exc_info is None
    assert _event_records(caplog, "wifi.connect") == []
    assert captured["responses"] == [502]
    assert private_message not in caplog.text
    assert "Traceback" not in caplog.text
    assert private_message not in h.wfile.getvalue().decode()


@pytest.mark.parametrize(
    ("body", "content_length", "read_fails", "expected_reads"),
    [
        (b"", 0, False, []),
        (b"{", 1, False, [1]),
        (b"\xff", 1, False, [1]),
        (b"{}", 2, False, [2]),
        (b"{}", 3, False, [3]),
        (b"[]", 2, False, [2]),
        (b"null", 4, False, [4]),
        (b'{"on":"false"}', 14, False, [14]),
        (b'{"on":true}', -1, False, []),
        (b'{"on":true}', 100_001, False, []),
        (b'{"on":true}', "not-a-number", False, []),
        (b'{"on":true}', 11, True, [11]),
    ],
)
def test_post_radio_rejects_invalid_bodies_without_mutation(
    monkeypatch,
    body,
    content_length,
    read_fails,
    expected_reads,
):
    calls = []
    monkeypatch.setattr(wifi_setup, "set_radio", lambda on: calls.append(on))
    h, captured = _valid_post("/radio", body)
    h.headers.replace_header("Content-Length", str(content_length))
    reader = _TrackingReader(body, fail=read_fails)
    h.rfile = reader

    h.do_POST()

    assert captured["status"] == 400
    assert json.loads(h.wfile.getvalue()) == {
        "ok": False,
        "message": "on must be a boolean",
    }
    assert calls == []
    assert reader.read_calls == expected_reads


@pytest.mark.parametrize(
    ("path", "body", "event"),
    [
        ("/connect", b'{"name":"Saved Home"}', "wifi.connect"),
        ("/forget", b'{"name":"Guest profile"}', "wifi.forget"),
        ("/radio", b'{"on":true}', "wifi.radio"),
    ],
)
@pytest.mark.parametrize(
    "failure_point",
    ["send_response", "header_flush", "body_write"],
)
def test_post_response_disconnect_keeps_single_primary_action_event(
    monkeypatch,
    caplog,
    path,
    body,
    event,
    failure_point,
):
    backend_calls = []
    monkeypatch.setattr(
        wifi_setup,
        "connect_saved",
        lambda name: (backend_calls.append(("connect", name)) or (True, "ok")),
    )
    monkeypatch.setattr(
        wifi_setup,
        "forget",
        lambda name: (backend_calls.append(("forget", name)) or (True, "ok")),
    )
    monkeypatch.setattr(
        wifi_setup,
        "set_radio",
        lambda enabled: (
            backend_calls.append(("radio", enabled)) or (True, "ok")
        ),
    )
    caplog.set_level(logging.INFO, logger=wifi_setup.logger.name)
    h, captured = _valid_post(path, body)
    response_attempts = captured["responses"]
    if failure_point == "send_response":
        response_attempts = []

        def fail_response(status, *args, **kwargs):
            response_attempts.append(int(status))
            raise BrokenPipeError("client disconnected before response headers")

        h.send_response = fail_response
        h.send_response_only = fail_response
    elif failure_point == "header_flush":
        def fail_end_headers():
            raise BrokenPipeError("client disconnected during header flush")

        h.end_headers = fail_end_headers
    else:
        h.wfile = _FailingWriter()

    with pytest.raises(BrokenPipeError):
        h.do_POST()

    assert len(backend_calls) == 1
    assert response_attempts == [200]
    assert len(_event_records(caplog, event)) == 1
    assert _event_records(caplog, "wifi.post_dispatch_failed") == []


def test_post_response_commit_guard_resets_for_keepalive(monkeypatch, caplog):
    backend_calls = []

    def fake_set_radio(enabled):
        backend_calls.append(enabled)
        return True, "ok"

    monkeypatch.setattr(wifi_setup, "set_radio", fake_set_radio)
    caplog.set_level(logging.INFO, logger=wifi_setup.logger.name)
    h, captured = _valid_post("/radio", b'{"on":true}')

    h.do_POST()
    second_body = b'{"on":false}'
    h.headers.replace_header("Content-Length", str(len(second_body)))
    h.rfile = BytesIO(second_body)
    h.wfile = BytesIO()
    h.do_POST()

    assert backend_calls == [True, False]
    assert captured["responses"] == [200, 200]
    assert len(_event_records(caplog, "wifi.radio")) == 2
    assert _event_records(caplog, "wifi.post_dispatch_failed") == []


def test_post_unknown_route_precedes_csrf_and_body_read(monkeypatch):
    def fail_guard(_handler):
        raise AssertionError("unknown routes must not reach CSRF")

    monkeypatch.setattr(wifi_setup, "guard_mutating_request", fail_guard)
    h, captured = _make_request("/unknown", body=b'{"on":true}')
    reader = _TrackingReader(b'{"on":true}', fail=True)
    h.rfile = reader

    h.do_POST()

    assert captured["status"] == int(http.HTTPStatus.NOT_FOUND)
    assert reader.read_calls == []


def test_post_csrf_rejection_precedes_body_read():
    h, captured = _make_request("/radio", body=b'{"on":true}')
    reader = _TrackingReader(b'{"on":true}', fail=True)
    h.rfile = reader

    h.do_POST()

    assert captured["status"] == int(http.HTTPStatus.FORBIDDEN)
    assert reader.read_calls == []


def test_post_connect_event_preserves_json_field_semantics(
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("JASPER_LOG_JSON", "1")
    monkeypatch.setattr(
        wifi_setup,
        "connect_saved",
        lambda name: (True, "connected"),
    )
    caplog.set_level(logging.INFO, logger=wifi_setup.logger.name)

    h, captured = _valid_post("/connect", b'{"name":"Saved Home"}')
    h.do_POST()

    assert captured["status"] == 200
    records = _event_records(caplog, "wifi.connect")
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload == {
        "event": "wifi.connect",
        "mode": "saved",
        "profile": "Saved Home",
        "ok": True,
        "client": "127.0.0.1",
    }
