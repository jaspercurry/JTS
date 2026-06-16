"""Handler-level tests for the /tools/ catalog wizard.

The static web-convention/design-system gates (test_web_wizard_conventions,
test_web_json_island, test_web_design_system) already cover the page's shape;
these drive the real Handler through synthetic requests to pin behaviour:

  * GET /            -> canonical document (app.css link, .app-header, CSRF meta)
  * GET / host guard -> 403 on a DNS-rebinding Host, 404 on an unknown route
  * GET /catalog.json -> read-through of the /run file; unavailable on missing
  * POST /toggle     -> route + CSRF guards, name validation, file write,
                        voice restart

Subprocess (systemctl, via restart_voice_daemon) and the filesystem paths are
mocked / pointed at tmp_path, mirroring the other hardware-free web tests.
"""
from __future__ import annotations

import http
import json
from email.message import Message
from io import BytesIO
from typing import Any

from jasper.web import tools_setup


CSRF = "x" * 43  # passes _common._is_valid_token (32..128 url-safe chars)


def _handler_cls(catalog_path: str, state_path: str):
    return tools_setup._make_handler(
        {"catalog_path": catalog_path, "state_path": state_path},
    )


def _write_catalog(path, tools):
    path.write_text(json.dumps({"schema_version": 1, "tools": tools}))


def _make_request(
    handler_cls,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> Any:
    h = handler_cls.__new__(handler_cls)
    h.path = path
    h.headers = Message()
    h.headers["Content-Length"] = str(len(body))
    if method == "POST":
        h.headers["Content-Type"] = "application/json"
    merged = {"Host": "jts.local", **(headers or {})}
    for key, value in merged.items():
        h.headers[key] = value
    h.rfile = BytesIO(body)
    h.wfile = BytesIO()
    h.client_address = ("127.0.0.1", 0)

    h.status = None
    h.sent_headers = []
    h.send_response = lambda status, *a, **k: setattr(h, "status", int(status))
    h.send_response_only = h.send_response
    h.send_header = lambda name, value: h.sent_headers.append((name, value))
    h.end_headers = lambda: None
    h.send_error = lambda status, *a, **k: setattr(h, "status", int(status))
    h.address_string = lambda: "127.0.0.1"
    h.log_message = lambda *a, **k: None
    return h


def _post_toggle(handler_cls, payload, *, token=CSRF, cookie_token=CSRF):
    body = json.dumps(payload).encode()
    headers = {}
    if token is not None:
        headers["X-CSRF-Token"] = token
    if cookie_token is not None:
        headers["Cookie"] = "jts_csrf=" + cookie_token
    return _make_request(
        handler_cls, "/toggle", method="POST", body=body, headers=headers,
    )


# --- GET / -----------------------------------------------------------------

def test_get_root_renders_canonical_page(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")), "/",
    )
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert "/assets/tools/tools.css?v=" in out
    assert 'class="app-header"' in out
    assert 'meta name="jts-csrf"' in out
    assert '<script type="module" src="/assets/tools/js/main.js">' in out


def test_get_root_rejects_dns_rebinding_host(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/", headers={"Host": "evil.example"},
    )
    h.do_GET()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
    assert b"host_not_allowed" in h.wfile.getvalue()


def test_get_unknown_route_404s(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/not-a-route", headers={"Host": "evil.example"},
    )
    h.do_GET()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


# --- GET /catalog.json -----------------------------------------------------

def test_get_catalog_returns_file_contents(tmp_path):
    cat = tmp_path / "tools.json"
    tools = [{"name": "get_weather", "status": "active", "labels": []}]
    _write_catalog(cat, tools)
    h = _make_request(_handler_cls(str(cat), str(tmp_path / "state.env")), "/catalog.json")
    h.do_GET()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["tools"] == tools


def test_get_catalog_missing_file_is_unavailable(tmp_path):
    h = _make_request(
        _handler_cls(str(tmp_path / "absent.json"), str(tmp_path / "state.env")),
        "/catalog.json",
    )
    h.do_GET()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["unavailable"] is True
    assert payload["tools"] == []


# --- POST /toggle ----------------------------------------------------------

def test_post_toggle_unknown_route_404s(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [{"name": "get_weather"}])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/nope", method="POST", body=b"{}",
    )
    h.do_POST()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_toggle_missing_csrf_is_403(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [{"name": "get_weather"}])
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        {"name": "get_weather", "enabled": False},
        token=None, cookie_token=None,
    )
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)


def test_post_toggle_unknown_tool_is_400(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [{"name": "get_weather"}])
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        {"name": "totally_made_up", "enabled": False},
    )
    h.do_POST()
    assert h.status == 400
    assert json.loads(h.wfile.getvalue().decode())["error"] == "unknown tool"


def test_post_toggle_bad_body_is_400(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [{"name": "get_weather"}])
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    # enabled must be a bool, not a string.
    h = _post_toggle(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        {"name": "get_weather", "enabled": "yes"},
    )
    h.do_POST()
    assert h.status == 400


def test_post_toggle_disable_writes_state_and_restarts(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write_catalog(cat, [{"name": "get_weather"}, {"name": "spotify_play"}])
    restarted = {"n": 0}
    monkeypatch.setattr(
        tools_setup, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )
    h = _post_toggle(
        _handler_cls(str(cat), str(state)),
        {"name": "spotify_play", "enabled": False},
    )
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload == {"ok": True, "name": "spotify_play", "enabled": False}
    assert restarted["n"] == 1
    assert tools_setup.read_disabled_tools(str(state)) == frozenset({"spotify_play"})


def test_post_toggle_enable_removes_from_disabled_set(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write_catalog(cat, [{"name": "get_weather"}, {"name": "spotify_play"}])
    tools_setup.write_disabled_tools(str(state), {"spotify_play", "get_weather"})
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle(
        _handler_cls(str(cat), str(state)),
        {"name": "spotify_play", "enabled": True},
    )
    h.do_POST()
    assert h.status == 200
    assert tools_setup.read_disabled_tools(str(state)) == frozenset({"get_weather"})


def test_public_surface_is_stable():
    assert callable(tools_setup.make_server)
    assert callable(tools_setup.main)
    assert callable(tools_setup._make_handler)
