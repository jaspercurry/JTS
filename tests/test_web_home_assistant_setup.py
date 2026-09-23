# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Home Assistant wizard rendering, request guards, and state transitions."""
from __future__ import annotations

import http
import json
import shutil
import subprocess
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

from jasper.web import home_assistant_setup as ha
from jasper.web._common import RestartOutcome
from tests._web_test_helpers import assert_canonical_page, make_real_handler


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
        assert_canonical_page(out)
        assert "/assets/home-assistant/home-assistant.css?v=" in out
        # legacy chrome must be gone
        assert "PAGE" "_STYLE" not in out
        assert "nav-back" not in out


def test_all_states_have_shared_app_header():
    for state in (_state_none(), _state_partial(), _state_connected()):
        out = _render(state)
        assert_canonical_page(out)
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


def test_state_connected_links_to_home_assistant_tool_pack():
    out = _render(_state_connected())
    assert 'href="/assistant/tools/pack/home-assistant/"' in out
    assert "Manage Home Assistant tool" in out


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
    # json.dumps does NOT escape angle brackets, so without an escape the
    # value would close the application/json island at HTML-parse time and
    # inject markup (reachable stored XSS). The island is built by the
    # shared `json_island()` helper, which JSON-unicode-escapes `<` / `>` / `&`.
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
    assert "\\u003C/script\\u003E" in out
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


def _make_request(
    path: str,
    body: bytes = b"",
    cookies: str = "",
    headers: dict[str, str] | None = None,
) -> Any:
    headers = dict(headers or {})
    if cookies:
        headers["Cookie"] = cookies
    handler, _ = make_real_handler(
        _handler_cls(), path, body=body, headers=headers,
    )
    return handler


def test_get_root_renders_canonical_page(monkeypatch):
    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    h = _make_request("/")
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert_canonical_page(out)


def test_get_root_with_tools_return_uses_tool_pack_back_link(monkeypatch):
    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    h = _make_request("/?return_to=%2Fassistant%2Ftools%2Fpack%2Fhome-assistant%2F")
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert 'href="/assistant/tools/pack/home-assistant/"' in out


def test_get_root_rejects_off_origin_return_link(monkeypatch):
    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    h = _make_request("/?return_to=%2F%2Fevil.test%2F")
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert 'href="/assistant/"' in out
    assert "evil.test" not in out


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


@pytest.mark.parametrize("route", ["/discover", "/ready", "/verify"])
@pytest.mark.parametrize(
    "fetch_metadata",
    [
        {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"},
        {
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        },
    ],
    ids=["fetch", "auto-submitting-form"],
)
def test_read_only_post_routes_reject_cross_site_callers(
    route, fetch_metadata, monkeypatch,
):
    # These read-only POST routes carry no CSRF token, so `@read_guarded` is
    # the whole guard. It must refuse a cross-site top-level navigation as
    # well as a cross-site fetch: the permissive navigation default exists
    # for links into a GET page, and letting it through here would let an
    # attacker page's auto-submitting form run the probe.
    called = {"discover": 0, "ready": 0, "verify": 0}
    monkeypatch.setattr(ha, "discover_sync", lambda *a, **k: called.__setitem__("discover", 1) or [])
    monkeypatch.setattr(ha, "ready_sync", lambda *a, **k: called.__setitem__("ready", 1) or {})
    monkeypatch.setattr(ha, "verify_sync", lambda *a, **k: called.__setitem__("verify", 1) or {})
    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    h = _make_request(
        route, body=b"", headers={"Host": "jts.local", **fetch_metadata},
    )
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
    assert called == {"discover": 0, "ready": 0, "verify": 0}


@pytest.mark.parametrize(
    "route,fake,payload_key",
    [
        ("/discover", "discover_sync", "instances"),
        ("/ready", "ready_sync", None),
        ("/verify", "verify_sync", None),
    ],
)
def test_read_only_post_routes_run_when_guard_allows(route, fake, payload_key, monkeypatch):
    monkeypatch.setattr(ha, fake, lambda *a, **k: {"ok": True} if payload_key is None else [])
    monkeypatch.setattr(ha, "read_env_file", lambda path: {})
    h = _make_request(
        route, body=b"",
        headers={"Host": "jts.local", "Sec-Fetch-Site": "same-origin"},
    )
    h.do_POST()
    assert h.status == 200


@pytest.mark.parametrize("branch", ["url-only", "changed-url", "rejected", "connected", "reused"])
@pytest.mark.parametrize("recent", [[], ["http://old:8123", "http://ha.local:8123", "http://third:8123"]])
@pytest.mark.parametrize("verify_ssl", [True, False])
@pytest.mark.parametrize("write_fails", [False, True])
def test_post_save_persistence(branch, recent, verify_ssl, write_fails, tmp_path, monkeypatch, caplog):
    path = tmp_path / "home_assistant.env"
    url = "http://homeassistant.local:8123" if branch == "url-only" else "http://ha.local:8123"
    llat = "eyJ0eXAi" + "z" * 180
    existing = {ha.ENV_URL: url, ha.ENV_AGENT_ID: "old-agent", ha.ENV_VERIFY_SSL: "0"}
    if branch in ("changed-url", "reused"):
        existing[ha.ENV_TOKEN] = llat
    if branch == "changed-url":
        existing[ha.ENV_URL] = "http://old:8123"
    if recent:
        existing[ha.ENV_RECENT_URLS] = json.dumps(recent)
    ha.write_env_file(path, existing, mode=ha.SECRET_ENV_MODE, owner=ha.HA_ENV_OWNER)
    before = path.read_bytes()
    writer = Mock(wraps=ha.write_env_file, side_effect=OSError("disk unavailable") if write_fails else None)
    verifier = Mock(return_value={"ok": branch != "rejected", "instance_name": "Home", "version": "2026.5"})
    restart = Mock(return_value=RestartOutcome.RAN)
    monkeypatch.setattr(ha, "write_env_file", writer)
    monkeypatch.setattr(ha, "verify_sync", verifier)
    monkeypatch.setattr(ha, "restart_voice_daemon", restart)
    caplog.set_level("INFO", logger=ha.__name__)
    form = {
        "csrf_token": CSRF, "url": url.removeprefix("http://"), "agent_id": "",
        "token": llat if branch in ("rejected", "connected") else "",
        "accept_self_signed_present": "1", "accept_self_signed": "" if verify_ssl else "on",
    }
    h, _ = make_real_handler(
        ha._make_handler({"state_path": str(path)}), "/save",
        body=urlencode(form).encode(), headers={"Cookie": "jts_csrf=" + CSRF},
    )
    h.do_POST()

    connected = branch in ("connected", "reused")
    expected = {ha.ENV_URL: url}
    if connected:
        expected.update({ha.ENV_TOKEN: llat, ha.ENV_AGENT_ID: "", ha.ENV_RECENT_URLS: json.dumps(
            [url] + [u for u in recent if u != url][:2],
        )})
    if not verify_ssl and branch in ("rejected", "connected", "reused"):
        expected[ha.ENV_VERIFY_SSL] = "0"
    if recent and not connected:
        expected[ha.ENV_RECENT_URLS] = json.dumps(recent)
    writer.assert_called_once_with(str(path), expected, mode=ha.SECRET_ENV_MODE, owner=ha.HA_ENV_OWNER)
    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert restart.call_count == int(connected and not write_fails)
    assert h.header_values("Location") == ["./?restarting=1" if connected and not write_fails else "./"]
    if branch in ("url-only", "changed-url"):
        verifier.assert_not_called()
    else:
        verifier.assert_called_once_with(url, llat, verify_ssl=verify_ssl)
    if write_fails:
        assert path.read_bytes() == before
    else:
        assert list(ha.read_env_file(str(path)).items()) == list(expected.items())
    assert path.stat().st_mode & 0o777 == 0o640
    assert llat not in h.wfile.getvalue().decode() + str(h.sent_headers) + caplog.text


def test_post_disconnect_clears_and_restarts(monkeypatch):
    token = "d" * 64
    deleted = {"n": 0}
    restarted = {"n": 0}
    monkeypatch.setattr(ha, "read_env_file", lambda path: _state_connected())
    monkeypatch.setattr(ha, "delete_env_file", lambda path: deleted.__setitem__("n", deleted["n"] + 1))
    monkeypatch.setattr(
        ha, "write_env_file", lambda path, values, mode=0o600, **kwargs: None,
    )
    monkeypatch.setattr(
        ha, "restart_voice_daemon",
        lambda: restarted.__setitem__("n", restarted["n"] + 1) or RestartOutcome.RAN,
    )

    body = b"csrf_token=" + token.encode()
    h = _make_request("/disconnect", body=body, cookies="jts_csrf=" + token)
    h.do_POST()

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert restarted["n"] == 1


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


def test_confirm_copy_and_ha_credentials_via_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    result = subprocess.run(
        [node, "tests/js/confirm_forms_copy_test.mjs"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
