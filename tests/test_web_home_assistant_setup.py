"""Tests for the /ha/ wizard after its migration to the canonical look.

Two things this guards:

1. Each of the three states (none / partial / connected) renders canonical
   design-system bytes (links /assets/app.css, carries the shared .app-header,
   embeds the CSRF meta tag) and delivers its behaviour as an ES module -- no
   inline <script> beyond the typed #ha-page-data JSON island.
2. The migration was presentation-only: the server-rendered POST flow
   (/discover, /save, /disconnect, /credentials-for-copy, /reset), the CSRF
   checks, the restart-on-save, and the public module surface (render fn,
   make_server, main) are unchanged.

Network (httpx) and subprocess (systemctl) are mocked, mirroring the other
hardware-free web tests.
"""
from __future__ import annotations

import http
import json
from email.message import Message
from io import BytesIO
from typing import Any

from jasper.web import home_assistant_setup as ha


# ---------------------------------------------------------------------------
# Render-level assertions (call the render fns directly with a fixed token).
# ---------------------------------------------------------------------------

CSRF = "x" * 43  # passes _common._is_valid_token (32..128 url-safe chars)


def _render(state: dict[str, str], flash: str = "") -> str:
    return ha._render_index(state, CSRF, status_msg=flash).decode()


def _state_none() -> dict[str, str]:
    return {}


def _state_partial() -> dict[str, str]:
    return {ha.ENV_URL: "http://homeassistant.local:8123"}


def _state_connected() -> dict[str, str]:
    return {
        ha.ENV_URL: "http://homeassistant.local:8123",
        ha.ENV_TOKEN: "eyJ0eXAiabcdefghijklmnopqrstuvwxyz0123456789",
        ha.ENV_AGENT_ID: "",
    }


def test_state_machine_routing():
    assert ha._state_machine(_state_none()) == "none"
    assert ha._state_machine(_state_partial()) == "partial"
    assert ha._state_machine(_state_connected()) == "connected"


def test_all_states_are_canonical_documents():
    for state in (_state_none(), _state_partial(), _state_connected()):
        out = _render(state)
        assert out.startswith("<!doctype html>")
        assert "/assets/app.css?v=" in out
        assert "/assets/home-assistant/home-assistant.css?v=" in out
        # legacy chrome must be gone
        assert "PAGE_STYLE" not in out
        assert "nav-back" not in out


def test_all_states_have_shared_app_header():
    for state in (_state_none(), _state_partial(), _state_connected()):
        out = _render(state)
        assert 'class="app-header"' in out
        assert '<h1 class="app-header__title">Home Assistant</h1>' in out
        assert '<use href="#icon-back">' in out


def test_all_states_embed_csrf_meta():
    for state in (_state_none(), _state_partial(), _state_connected()):
        out = _render(state)
        assert 'meta name="jts-csrf"' in out
        assert f'content="{CSRF}"' in out


def test_all_states_load_es_module_and_have_no_behaviour_script():
    for state in (_state_none(), _state_partial(), _state_connected()):
        out = _render(state)
        assert '<script type="module" src="/assets/home-assistant/js/main.js">' in out
        # No inline behaviour script, no legacy inline dialog helper.
        assert "jtsConfirmSubmit" not in out
        assert "addEventListener" not in out
        assert "fetch(" not in out
        # The only permitted inline script is the typed data island.
        assert out.count("<script") == out.count(
            '<script type="application/json"'
        ) + out.count('<script type="module"')


def test_state_none_uses_canonical_field_vocabulary():
    out = _render(_state_none())
    assert 'class="field"' in out
    assert 'class="form-actions"' in out
    assert 'class="btn btn--primary"' in out
    assert 'id="discover-btn"' in out
    assert 'action="./save"' in out


def test_state_none_recent_urls_are_escaped_data_attrs():
    state = {ha.ENV_RECENT_URLS: json.dumps(["http://ha.local:8123"])}
    out = _render(state)
    assert 'class="btn btn--ghost recent-link"' in out
    assert 'data-url="http://ha.local:8123"' in out
    # Never interpolated into inline JS.
    assert "onclick" not in out


def test_state_partial_has_token_form_and_csrf_field():
    out = _render(_state_partial())
    assert 'action="./save"' in out
    assert 'name="token"' in out
    assert 'name="csrf_token"' in out  # _common.CSRF_FORM_FIELD
    assert 'href="./reset"' in out


def test_state_partial_https_shows_self_signed_checkbox():
    https = {ha.ENV_URL: "https://ha.example.com:8123"}
    out = _render(https)
    assert 'name="accept_self_signed"' in out
    assert 'name="accept_self_signed_present"' in out
    # Plain http hides it.
    assert 'name="accept_self_signed"' not in _render(_state_partial())


def test_state_connected_status_card_masks_token():
    out = _render(_state_connected())
    assert 'class="deflist"' in out
    # The full token must never appear; only the masked prefix…suffix.
    assert "eyJ0eXAiabcdefghijklmnopqrstuvwxyz0123456789" not in out
    assert ha.mask_secret("eyJ0eXAiabcdefghijklmnopqrstuvwxyz0123456789") in out


def test_state_connected_disconnect_uses_data_confirm():
    out = _render(_state_connected())
    assert 'action="./disconnect"' in out
    assert "data-confirm=" in out
    assert 'data-confirm-danger="1"' in out


def test_state_connected_page_data_island_carries_prompt_not_inline_js():
    out = _render(_state_connected())
    assert 'id="ha-page-data"' in out
    # The voice-pack prompt rides in the JSON island, not in executable JS.
    assert "JTS smart speaker" in out
    assert "json.dumps(VOICE_PACK_PROMPT)" not in out


def test_state_connected_page_data_island_escapes_script_breakout():
    # agent_id is a free-form POST field, not validated against the agent
    # dropdown, so an operator/attacker can stash a script-closing tag in it.
    # json.dumps does NOT escape forward slashes, so without the </-sequence
    # guard the value would close the application/json island at HTML-parse
    # time and inject markup (reachable stored XSS). Mirror the sibling
    # wake_corpus_setup.py guard: the </ must be escaped to <\/.
    payload = "</script><img src=x onerror=alert(1)>"
    state = {
        **_state_connected(),
        ha.ENV_AGENT_ID: payload,
    }
    out = _render(state)
    # The raw breakout must NOT survive into the rendered island.
    assert payload not in out
    assert "</script><img" not in out
    # The escaped form is what lands in the JSON island.
    assert "<\\/script>" in out
    # And the island still round-trips: pull the JSON text back out and parse
    # it, confirming currentAgent decodes to the original attacker payload
    # (the JS does the same JSON.parse at load time).
    marker = '<script type="application/json" id="ha-page-data">'
    start = out.index(marker) + len(marker)
    end = out.index("</script>", start)
    parsed = json.loads(out[start:end])
    assert parsed["currentAgent"] == payload


def test_connected_flash_renders_banner():
    out = _render(_state_connected(), flash="Disconnected. The speaker is restarting.")
    assert 'class="banner' in out


def test_blank_flash_renders_no_banner():
    assert 'class="banner' not in _render(_state_connected(), flash="")


# ---------------------------------------------------------------------------
# Handler-level assertions (drive do_GET/do_POST through a fake handler).
# ---------------------------------------------------------------------------


def _handler_cls():
    return ha._make_handler({"state_path": "/tmp/ha-does-not-matter.env"})


def _make_request(path: str, body: bytes = b"", cookies: str = "") -> Any:
    """Build a *real* /ha/ Handler instance wired to a synthetic request.

    Mirrors tests/test_web_wifi_setup.py's `_make_request`. The Handler
    defines its response helpers (_send_html / _send_json) as instance
    methods, so we instantiate the real class (via __new__, to skip
    BaseHTTPRequestHandler.__init__'s socket plumbing) and bolt the request
    I/O onto it. We then override only the network-touching surface of
    BaseHTTPRequestHandler so the real helper methods run without a socket.

    Status + emitted headers are captured back onto the instance as
    ``.status`` / ``.sent_headers`` (with a ``header_values(name)`` reader),
    matching the attribute surface the handler tests assert against.
    """
    handler_cls = _handler_cls()
    h = handler_cls.__new__(handler_cls)
    h.path = path
    h.headers = Message()
    h.headers["Content-Length"] = str(len(body))
    h.headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookies:
        h.headers["Cookie"] = cookies
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
    h.log_message = lambda *a, **k: None
    h.header_values = lambda name: [
        v for n, v in h.sent_headers if n.lower() == name.lower()
    ]
    return h


def test_public_surface_is_stable():
    assert callable(ha.make_server)
    assert callable(ha.main)
    assert callable(ha._render_index)


def test_get_root_renders_canonical_page(monkeypatch):
    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    h = _make_request("/")
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out


def test_post_discover_returns_instances_json_no_csrf(monkeypatch):
    # /discover is a read-only network probe — no CSRF required.
    monkeypatch.setattr(
        ha, "discover_sync",
        lambda timeout: [{"url": "http://ha.local:8123", "location_name": "Home"}],
    )
    h = _make_request("/discover", body=b"")
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["instances"][0]["url"] == "http://ha.local:8123"


def test_post_unknown_route_404s():
    h = _make_request("/nope", body=b"")
    h.do_POST()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_save_url_only_advances_to_partial(monkeypatch):
    token = "a" * 64
    written: dict[str, dict[str, str]] = {}

    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    monkeypatch.setattr(
        ha, "write_env_file",
        lambda path, values, mode=0o600: written.update({"v": values}),
    )
    restarted = {"n": 0}
    monkeypatch.setattr(ha, "restart_voice_daemon", lambda: restarted.__setitem__("n", restarted["n"] + 1))

    body = (
        "csrf_token=" + token
        + "&url=homeassistant.local:8123&token=&agent_id="
    ).encode()
    h = _make_request("/save", body=body, cookies="jts_csrf=" + token)
    h.do_POST()

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    # URL persisted, token not yet -> next render is state 2. No restart yet.
    assert written["v"][ha.ENV_URL] == "http://homeassistant.local:8123"
    assert restarted["n"] == 0


def test_post_save_with_token_verifies_and_restarts(monkeypatch):
    token = "b" * 64
    written: dict[str, dict[str, str]] = {}

    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    monkeypatch.setattr(
        ha, "verify_sync",
        lambda url, tok, verify_ssl=True: {
            "ok": True, "instance_name": "Home", "version": "2026.5",
        },
    )
    monkeypatch.setattr(
        ha, "write_env_file",
        lambda path, values, mode=0o600: written.update({"v": values}),
    )
    restarted = {"n": 0}
    monkeypatch.setattr(
        ha, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )

    llat = "eyJ0eXAi" + "z" * 180
    body = (
        "csrf_token=" + token
        + "&url=http://ha.local:8123&token=" + llat + "&agent_id="
    ).encode()
    h = _make_request("/save", body=body, cookies="jts_csrf=" + token)
    h.do_POST()

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert written["v"][ha.ENV_TOKEN] == llat
    assert restarted["n"] == 1
    # Lands on the restart-poll URL.
    assert h.header_values("Location") == ["./?restarting=1"]


def test_post_save_rejects_bad_csrf(monkeypatch):
    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    body = b"csrf_token=" + b"a" * 64 + b"&url=http://ha.local:8123"
    h = _make_request("/save", body=body, cookies="jts_csrf=" + "c" * 64)
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)


def test_post_disconnect_clears_and_restarts(monkeypatch):
    token = "d" * 64
    deleted = {"n": 0}
    restarted = {"n": 0}
    monkeypatch.setattr(ha, "read_env_file", lambda path: _state_connected())
    monkeypatch.setattr(ha, "delete_env_file", lambda path: deleted.__setitem__("n", deleted["n"] + 1))
    monkeypatch.setattr(
        ha, "write_env_file", lambda path, values, mode=0o600: None,
    )
    monkeypatch.setattr(
        ha, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1),
    )

    body = b"csrf_token=" + token.encode()
    h = _make_request("/disconnect", body=body, cookies="jts_csrf=" + token)
    h.do_POST()

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert restarted["n"] == 1


def test_credentials_for_copy_requires_csrf(monkeypatch):
    # No / mismatched CSRF -> 403, and the credentials are never read.
    monkeypatch.setattr(ha, "read_env_file", lambda path: _state_connected())
    h = _make_request("/credentials-for-copy", body=b"", cookies="jts_csrf=" + "e" * 64)
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)


def test_credentials_for_copy_returns_creds_with_csrf(monkeypatch):
    token = "f" * 64
    monkeypatch.setattr(ha, "read_env_file", lambda path: _state_connected())
    # Header-form CSRF (the JS sends X-CSRF-Token).
    h = _make_request("/credentials-for-copy", body=b"", cookies="jts_csrf=" + token)
    h.headers["X-CSRF-Token"] = token
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["url"] == "http://homeassistant.local:8123"
    assert payload["token"].startswith("eyJ0eXAi")
