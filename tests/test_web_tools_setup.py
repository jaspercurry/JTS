"""Handler-level tests for the /tools/ catalog wizard.

The static web-convention/design-system gates (test_web_wizard_conventions,
test_web_json_island, test_web_design_system) already cover the page's shape;
these drive the real Handler through synthetic requests to pin behaviour:

  * GET /            -> canonical document (app.css link, .app-header, CSRF meta)
  * GET / host guard -> 403 on a DNS-rebinding Host, 404 on an unknown route
  * GET /catalog.json -> catalog metadata + fresh disabled-set OVERLAID
                        (+ pending flag); unavailable on missing
  * POST /toggle     -> route + CSRF guards, name validation, file write —
                        STAGES only, NO restart (no-op guard, needs_setup reject)
  * POST /apply      -> restarts voice once; honest no-restart on no-provider /
                        bonded; rate-limited under the reboot ladder

Subprocess (systemctl, via restart_voice_daemon) and the filesystem paths are
mocked / pointed at tmp_path, mirroring the other hardware-free web tests.
"""
from __future__ import annotations

import http
import json
import threading
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from jasper.tool_prompt_overrides import read_prompt_overrides
from jasper.tool_state import (
    ToolState,
    read_disabled_tools,
    read_tool_state,
    write_disabled_tools,
    write_tool_state,
)
from jasper.web import tools_setup


CSRF = "x" * 43  # passes _common._is_valid_token (32..128 url-safe chars)


@pytest.fixture(autouse=True)
def _reset_apply_floor():
    """The Apply rate-limit's in-memory fail-closed floor is a module global;
    reset it so per-test apply timing is independent of test order."""
    tools_setup._LAST_APPLY[0] = 0.0
    yield
    tools_setup._LAST_APPLY[0] = 0.0


def _handler_cls(catalog_path: str, state_path: str):
    return tools_setup._make_handler(
        {"catalog_path": catalog_path, "state_path": state_path},
    )


def _write_catalog(path, tools):
    path.write_text(json.dumps({"schema_version": 1, "tools": tools}))


def _write(path, payload):
    path.write_text(json.dumps(payload))


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


def _post(handler_cls, route, payload, *, token=CSRF, cookie_token=CSRF):
    body = json.dumps(payload).encode()
    headers = {}
    if token is not None:
        headers["X-CSRF-Token"] = token
    if cookie_token is not None:
        headers["Cookie"] = "jts_csrf=" + cookie_token
    return _make_request(
        handler_cls, route, method="POST", body=body, headers=headers,
    )


def _post_toggle(handler_cls, payload, **kw):
    return _post(handler_cls, "/toggle", payload, **kw)


def _post_toggle_pack(handler_cls, payload, **kw):
    return _post(handler_cls, "/toggle-pack", payload, **kw)


def _post_prompt(handler_cls, payload, **kw):
    return _post(handler_cls, "/prompt", payload, **kw)


def _post_prompt_reset(handler_cls, payload, **kw):
    return _post(handler_cls, "/prompt-reset", payload, **kw)


def _post_apply(handler_cls, payload=None, **kw):
    return _post(handler_cls, "/apply", payload or {}, **kw)


# A realistic catalog entry — toggleability is derived from `status`, so the
# fixtures need it (a status-less entry is treated as not-configured).
def _tool(name, status="active", **extra):
    return {"name": name, "status": status, "labels": [], **extra}


def _pack(pack_id="spotify", status="active", **extra):
    return {
        "id": pack_id,
        "title": "Spotify",
        "summary": "Spotify tools",
        "category": "Music",
        "tool_names": ["spotify_play"],
        "status": status,
        "tool_count": 1,
        **extra,
    }


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
    assert 'href="/tools/guide/"' in out
    assert 'target="_blank" rel="noopener"' in out
    assert '<script type="module" src="/assets/tools/js/main.js">' in out


def test_get_tool_detail_renders_canonical_page(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("get_weather")])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/tool/get_weather/",
    )
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert "/assets/tools/tools.css?v=" in out
    assert 'class="app-header"' in out
    assert 'href="/tools/"' in out
    assert 'id="tool-detail-data"' in out
    assert '"pack_id": "tool:get_weather"' in out
    assert '<script type="module" src="/assets/tools/js/detail.js">' in out


def test_get_tool_detail_json_island_escapes_tool_name(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("get_weather")])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/tool/%3C%2Fscript%3E",
    )
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert '{"pack_id": "tool:\\u003C/script\\u003E"}' in out
    assert '{"pack_id": "tool:</script>"}' not in out
    assert "\\u003C/script\\u003E" in out


def test_get_pack_detail_renders_canonical_page(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("spotify_play")])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/pack/spotify/",
    )
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert 'id="tool-detail-data"' in out
    assert '"pack_id": "spotify"' in out
    assert '<script type="module" src="/assets/tools/js/detail.js">' in out


def test_get_tool_authoring_guide_renders(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/guide/",
    )
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "Tool authoring guide" in out
    assert "/assets/app.css?v=" in out
    assert "/assets/tools/tools.css?v=" in out
    assert 'class="app-header"' in out
    assert 'meta name="jts-csrf"' in out
    assert "<script" not in out
    assert "CapabilityPack" in out
    assert "ToolDefinition" in out
    assert "ToolExecutor" in out
    assert "llm_description" in out
    assert "untrusted_output=True" in out
    assert "consequential=True" in out
    assert "no marketplace" in out
    assert "untrusted-code" in out


def test_get_tool_authoring_guide_uses_read_guard(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/guide", headers={"Host": "evil.example"},
    )
    h.do_GET()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
    assert b"host_not_allowed" in h.wfile.getvalue()


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


def test_get_unknown_tool_route_404s_before_host_guard(tmp_path):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [])
    h = _make_request(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        "/tool/too/deep", headers={"Host": "evil.example"},
    )
    h.do_GET()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_pack_card_hover_uses_whole_card_surface_not_title_color():
    css = Path("deploy/assets/tools/tools.css").read_text()
    assert ".tool-pack-card[data-pack-href]:hover {" in css
    assert "var(--surface-hover)" in css
    assert ":hover .tool-pack-card__title" not in css


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


def test_post_toggle_needs_setup_tool_is_rejected(tmp_path, monkeypatch):
    """A needs_setup tool has no live control; a crafted POST for it must be
    rejected (don't widen the disabled-set / restart surface to unconfigured
    tools)."""
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("home_assistant", status="needs_setup")])
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        {"name": "home_assistant", "enabled": False},
    )
    h.do_POST()
    assert h.status == 400
    assert json.loads(h.wfile.getvalue().decode())["error"] == "tool not configured"


def test_post_toggle_disable_stages_without_restart(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write_catalog(cat, [_tool("get_weather"), _tool("spotify_play")])
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
    assert payload == {
        "ok": True, "name": "spotify_play", "enabled": False, "pending": True,
    }
    # A toggle NEVER restarts — that's Apply's job.
    assert restarted["n"] == 0
    assert read_disabled_tools(str(state)) == frozenset({"spotify_play"})


def test_post_toggle_enable_removes_from_disabled_set(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write_catalog(cat, [_tool("get_weather"), _tool("spotify_play")])
    write_disabled_tools(str(state), {"spotify_play", "get_weather"})
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle(
        _handler_cls(str(cat), str(state)),
        {"name": "spotify_play", "enabled": True},
    )
    h.do_POST()
    assert h.status == 200
    assert read_disabled_tools(str(state)) == frozenset({"get_weather"})


def test_post_toggle_pack_stages_pack_without_restart(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write(cat, {
        "schema_version": 2,
        "tools": [_tool(
            "spotify_play",
            pack={"id": "spotify", "title": "Spotify", "summary": ""},
        )],
        "packs": [_pack("spotify")],
    })
    restarted = {"n": 0}
    monkeypatch.setattr(
        tools_setup, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )
    h = _post_toggle_pack(
        _handler_cls(str(cat), str(state)),
        {"id": "spotify", "enabled": False},
    )
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["ok"] is True
    assert payload["id"] == "spotify"
    assert payload["pending"] is True
    assert restarted["n"] == 0
    assert read_tool_state(str(state)).disabled_packs == {"spotify"}


def test_post_toggle_singleton_pack_writes_child_tool_state(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write(cat, {
        "schema_version": 2,
        "tools": [_tool("standalone_tool", category="Utilities")],
    })
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle_pack(
        _handler_cls(str(cat), str(state)),
        {"id": "tool:standalone_tool", "enabled": False},
    )
    h.do_POST()
    assert h.status == 200
    saved = read_tool_state(str(state))
    assert saved.disabled_tools == {"standalone_tool"}
    assert saved.disabled_packs == frozenset()


def test_post_toggle_pack_needs_setup_records_setup_intent(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write(cat, {
        "schema_version": 2,
        "tools": [_tool(
            "home_assistant",
            status="needs_setup",
            setup_url="/ha/",
            requires_setup=True,
            pack={
                "id": "home-assistant",
                "title": "Home Assistant",
                "summary": "",
                "setup_url": "/ha/",
            },
        )],
    })
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle_pack(
        _handler_cls(str(cat), str(state)),
        {"id": "home-assistant", "enabled": True},
    )
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload == {
        "ok": True,
        "id": "home-assistant",
        "enabled": True,
        "pending": False,
        "setup_required": True,
    }
    assert read_tool_state(str(state)).setup_enabled_packs == {"home-assistant"}


def test_post_toggle_pack_needs_setup_off_clears_setup_intent(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write(cat, {
        "schema_version": 2,
        "tools": [_tool(
            "home_assistant",
            status="needs_setup",
            setup_url="/ha/",
            requires_setup=True,
            pack={
                "id": "home-assistant",
                "title": "Home Assistant",
                "summary": "",
                "setup_url": "/ha/",
            },
        )],
    })
    write_tool_state(
        str(state),
        ToolState(setup_enabled_packs=frozenset({"home-assistant"})),
    )
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle_pack(
        _handler_cls(str(cat), str(state)),
        {"id": "home-assistant", "enabled": False},
    )
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["pending"] is False
    assert payload["setup_required"] is True
    assert read_tool_state(str(state)).setup_enabled_packs == frozenset()


def test_post_tool_toggle_rejects_pack_disabled_tool(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write(cat, {
        "schema_version": 2,
        "tools": [_tool(
            "spotify_play",
            pack={"id": "spotify", "title": "Spotify", "summary": ""},
        )],
        "packs": [_pack("spotify")],
    })
    write_tool_state(
        str(state),
        ToolState(disabled_packs=frozenset({"spotify"})),
    )
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_toggle(
        _handler_cls(str(cat), str(state)),
        {"name": "spotify_play", "enabled": True},
    )
    h.do_POST()
    assert h.status == 400
    assert json.loads(h.wfile.getvalue().decode())["error"] == "pack disabled"


def test_post_toggle_no_op_does_not_rewrite(tmp_path, monkeypatch):
    """Re-disabling an already-disabled tool changes nothing — no rewrite,
    no churn — but still returns ok+pending so the UI stays consistent."""
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write_catalog(cat, [_tool("spotify_play")])
    write_disabled_tools(str(state), {"spotify_play"})
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    calls = {"n": 0}
    real_write = write_tool_state
    monkeypatch.setattr(
        tools_setup, "write_tool_state",
        lambda p, s: (calls.__setitem__("n", calls["n"] + 1), real_write(p, s)),
    )
    h = _post_toggle(
        _handler_cls(str(cat), str(state)),
        {"name": "spotify_play", "enabled": False},  # already disabled
    )
    h.do_POST()
    assert h.status == 200
    assert calls["n"] == 0  # no-op: never rewrote the file
    assert read_disabled_tools(str(state)) == frozenset({"spotify_play"})


def test_post_prompt_override_and_reset_stage_without_restart(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    prompts = tmp_path / "prompts.json"
    _write(cat, {
        "schema_version": 2,
        "tools": [_tool(
            "get_weather",
            description="Default prompt",
            default_description="Default prompt",
            pack={"id": "weather", "title": "Weather", "summary": ""},
        )],
        "packs": [_pack("weather")],
    })
    restarted = {"n": 0}
    monkeypatch.setattr(
        tools_setup, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )
    handler = tools_setup._make_handler({
        "catalog_path": str(cat),
        "state_path": str(state),
        "prompt_overrides_path": str(prompts),
    })
    h = _post_prompt(handler, {"name": "get_weather", "prompt": "Custom prompt"})
    h.do_POST()
    assert h.status == 200
    assert read_prompt_overrides(str(prompts)) == {
        "get_weather": "Custom prompt",
    }
    assert restarted["n"] == 0
    assert json.loads(h.wfile.getvalue().decode())["pending"] is True

    h = _post_prompt_reset(handler, {"name": "get_weather"})
    h.do_POST()
    assert h.status == 200
    assert read_prompt_overrides(str(prompts)) == {}


# --- POST /apply -----------------------------------------------------------

def test_post_apply_restarts_once(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("spotify_play")])
    restarted = {"n": 0}
    monkeypatch.setattr(
        tools_setup, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )
    monkeypatch.setattr(tools_setup, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(tools_setup, "bonded_follower_active", lambda: False)
    h = _post_apply(_handler_cls(str(cat), str(tmp_path / "state.env")))
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["restarted"] is True
    assert restarted["n"] == 1


def test_post_apply_no_provider_does_not_restart(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("spotify_play")])
    restarted = {"n": 0}
    monkeypatch.setattr(
        tools_setup, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )
    monkeypatch.setattr(tools_setup, "read_active_provider", lambda: "")
    monkeypatch.setattr(tools_setup, "bonded_follower_active", lambda: False)
    h = _post_apply(_handler_cls(str(cat), str(tmp_path / "state.env")))
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["restarted"] is False
    assert payload["reason"] == "no_provider"
    assert restarted["n"] == 0


def test_post_apply_bonded_follower_does_not_restart(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("spotify_play")])
    restarted = {"n": 0}
    monkeypatch.setattr(
        tools_setup, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )
    monkeypatch.setattr(tools_setup, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(tools_setup, "bonded_follower_active", lambda: True)
    h = _post_apply(_handler_cls(str(cat), str(tmp_path / "state.env")))
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["restarted"] is False
    assert payload["reason"] == "bonded"
    assert restarted["n"] == 0


def test_post_apply_is_rate_limited(tmp_path, monkeypatch):
    """A second Apply inside the min-interval is throttled — no restart — so
    Apply-spam can't feed jasper-voice's StartLimitAction=reboot ladder."""
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write_catalog(cat, [_tool("spotify_play")])
    restarted = {"n": 0}
    monkeypatch.setattr(
        tools_setup, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )
    monkeypatch.setattr(tools_setup, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(tools_setup, "bonded_follower_active", lambda: False)
    hc = _handler_cls(str(cat), str(state))

    h1 = _post_apply(hc)
    h1.do_POST()
    assert json.loads(h1.wfile.getvalue().decode())["restarted"] is True

    h2 = _post_apply(hc)
    h2.do_POST()
    payload = json.loads(h2.wfile.getvalue().decode())
    assert payload["restarted"] is False
    assert payload["reason"] == "throttled"
    assert payload["retry_after"] >= 1
    assert restarted["n"] == 1  # only the first one restarted


def test_post_apply_throttle_survives_ts_write_failure(tmp_path, monkeypatch):
    """If the apply timestamp can't be persisted (RO rootfs / disk full), the
    in-memory fail-closed floor must still throttle — a write failure must not
    open the reboot-ladder DoS."""
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("spotify_play")])
    restarted = {"n": 0}
    monkeypatch.setattr(
        tools_setup, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )
    monkeypatch.setattr(tools_setup, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(tools_setup, "bonded_follower_active", lambda: False)
    # An unwritable ts path: the parent dir doesn't exist, so every write
    # fails (and every read returns 0.0). Only the in-memory floor can throttle.
    bad_ts = str(tmp_path / "nonexistent-dir" / "apply.ts")
    hc = tools_setup._make_handler({
        "catalog_path": str(cat), "state_path": str(tmp_path / "s.env"),
        "apply_ts_path": bad_ts,
    })

    h1 = _post_apply(hc)
    h1.do_POST()
    assert json.loads(h1.wfile.getvalue().decode())["restarted"] is True

    h2 = _post_apply(hc)
    h2.do_POST()
    assert json.loads(h2.wfile.getvalue().decode())["restarted"] is False
    assert restarted["n"] == 1  # floor held despite the unwritable ts file


def test_post_apply_ignores_non_finite_ts(tmp_path, monkeypatch):
    """A nan/inf timestamp must be treated as 0.0 (Apply proceeds) — not
    silently fail open (nan) or block Apply forever (inf)."""
    cat = tmp_path / "tools.json"
    state = tmp_path / "s.env"
    ts = tmp_path / "apply.ts"
    _write_catalog(cat, [_tool("spotify_play")])
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    monkeypatch.setattr(tools_setup, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(tools_setup, "bonded_follower_active", lambda: False)
    for bogus in ("inf", "nan", "-inf"):
        ts.write_text(bogus)
        tools_setup._LAST_APPLY[0] = 0.0  # isolate from the floor
        h = _post_apply(tools_setup._make_handler({
            "catalog_path": str(cat), "state_path": str(state),
            "apply_ts_path": str(ts),
        }))
        h.do_POST()
        payload = json.loads(h.wfile.getvalue().decode())
        assert payload["restarted"] is True, f"{bogus} ts blocked Apply"


def test_concurrent_toggles_do_not_lose_updates(tmp_path, monkeypatch):
    """The _STATE_LOCK must serialize the read-modify-write of tool_state.env
    so N concurrent toggles of distinct tools all land (no last-writer-wins
    lost updates). Pins the docstring safety claim."""
    cat = tmp_path / "tools.json"
    state = tmp_path / "s.env"
    names = [f"tool_{i}" for i in range(12)]
    _write_catalog(cat, [_tool(n) for n in names])
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    hc = _handler_cls(str(cat), str(state))

    barrier = threading.Barrier(len(names))

    def disable(name):
        barrier.wait()  # maximize overlap on the RMW
        h = _post_toggle(hc, {"name": name, "enabled": False})
        h.do_POST()

    threads = [threading.Thread(target=disable, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert read_disabled_tools(str(state)) == frozenset(names)


def test_post_apply_missing_csrf_is_403(tmp_path, monkeypatch):
    cat = tmp_path / "tools.json"
    _write_catalog(cat, [_tool("spotify_play")])
    monkeypatch.setattr(tools_setup, "restart_voice_daemon", lambda: None)
    h = _post_apply(
        _handler_cls(str(cat), str(tmp_path / "state.env")),
        token=None, cookie_token=None,
    )
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)


# --- GET /catalog.json overlay ---------------------------------------------

def test_catalog_overlays_fresh_disabled_set(tmp_path):
    """The wizard re-derives on/off from the fresh disabled-set — so a toggle
    converges in the UI without waiting on a voice restart to rewrite the
    catalog. A tool voice baked as 'active' reads 'off' once it's disabled."""
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write_catalog(cat, [_tool("get_weather", status="active")])
    write_disabled_tools(str(state), {"get_weather"})
    h = _make_request(_handler_cls(str(cat), str(state)), "/catalog.json")
    h.do_GET()
    payload = json.loads(h.wfile.getvalue().decode())
    by_name = {t["name"]: t for t in payload["tools"]}
    assert by_name["get_weather"]["status"] == "off"
    assert payload["pending"] is True


def test_public_surface_is_stable():
    assert callable(tools_setup.make_server)
    assert callable(tools_setup.main)
    assert callable(tools_setup._make_handler)
