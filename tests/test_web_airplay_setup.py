# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /airplay/ wizard after its migration to the canonical look.

1. The page renders canonical design-system bytes (links /assets/app.css and its
   page stylesheet, carries the shared .app-header, embeds the CSRF meta tag).
   This page has no client behaviour, so it ships NO ES module and NO <script>.
2. The migration was presentation-only: the server-rendered POST /save flow
   (validate mode -> write env file -> restart shairport-sync) and the public
   module surface (render fn, make_server, main) are unchanged.
"""
from __future__ import annotations

import http

from jasper.web import airplay_setup

from ._web_test_helpers import FakeHandler


def _render(mode: str = "synced", flash: str = "") -> str:
    return airplay_setup._index_html(
        mode,
        "tok-abcdefghijklmnopqrstuvwx",
        status_msg=flash,
    ).decode()


def test_airplay_page_is_canonical_document():
    out = _render()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    assert "/assets/airplay/airplay.css?v=" in out
    # Old shell markers must be gone.
    assert "max-width: 620px" not in out
    assert 'class="sub"' not in out


def test_airplay_page_has_shared_app_header():
    out = _render()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">AirPlay sync mode</h1>' in out
    assert '<use href="#icon-back">' in out


def test_airplay_page_embeds_csrf_meta():
    out = _render()
    assert 'meta name="jts-csrf"' in out
    assert 'content="tok-abcdefghijklmnopqrstuvwx"' in out


def test_airplay_form_preserves_csrf_field_and_save_action():
    out = _render()
    assert 'action="./save"' in out
    assert 'method="post"' in out
    assert 'type="hidden"' in out
    assert 'name="csrf_token"' in out


def test_airplay_form_uses_canonical_button_vocabulary():
    out = _render()
    assert 'class="form-actions"' in out
    assert 'class="btn btn--primary"' in out


def test_airplay_radio_group_preserved():
    # Behaviour: _apply_save reads form["mode"]; both radio values must render.
    out = _render()
    assert 'name="mode" value="synced"' in out
    assert 'name="mode" value="free-running"' in out


def test_airplay_checked_reflects_current_mode():
    synced = _render(mode="synced")
    assert 'name="mode" value="synced" checked' in synced
    assert 'name="mode" value="free-running" >' in synced

    free = _render(mode="free-running")
    assert 'name="mode" value="free-running" checked' in free
    assert 'name="mode" value="synced" >' in free


def test_airplay_page_has_no_client_script():
    # This page has no JS: no ES module, no inline <script>. That is correct
    # minimalism, mirroring the "no module for a no-JS page" rule.
    out = _render()
    assert "<script" not in out


def test_airplay_blank_flash_renders_no_banner():
    assert 'class="banner' not in _render(flash="")


def test_airplay_saved_flash_renders_ok_banner():
    out = _render(flash="Saved. AirPlay now in synced mode (shairport-sync restarted).")
    assert "banner--ok" in out


def test_airplay_flash_is_escaped():
    out = _render(flash="Unknown mode '<x>'.")
    assert "&lt;x&gt;" in out
    assert "<x>" not in out


# --- Behaviour: drive do_GET / do_POST through a fake handler, like the other
#     web-wizard tests. Network / subprocess side effects are monkeypatched. ---

def _handler_cls():
    return airplay_setup._make_handler({"state_path": "/tmp/does-not-matter.env"})


def test_public_surface_is_stable():
    assert callable(airplay_setup.make_server)
    assert callable(airplay_setup.main)
    assert callable(airplay_setup._index_html)
    assert callable(airplay_setup._current_mode)


def test_get_root_renders_canonical_page(monkeypatch):
    monkeypatch.setattr(airplay_setup, "_current_mode", lambda path: "synced")
    handler = _handler_cls()
    h = FakeHandler("/")
    handler.do_GET(h)
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out


def test_post_unknown_route_404s():
    handler = _handler_cls()
    h = FakeHandler("/nope", body=b"")
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_save_writes_mode_and_restarts(monkeypatch):
    token = "y" * 64
    calls = {"write": [], "restart": 0}

    def fake_write(path, data, mode=0o644):
        calls["write"].append((path, dict(data), mode))

    monkeypatch.setattr(airplay_setup, "write_env_file", fake_write)
    monkeypatch.setattr(
        airplay_setup, "_restart_shairport",
        lambda: calls.__setitem__("restart", calls["restart"] + 1),
    )

    handler = _handler_cls()
    # csrf_token = form field (CSRF_FORM_FIELD); jts_csrf = double-submit cookie.
    body = ("csrf_token=" + token + "&mode=free-running").encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler.do_POST(h)

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert h.header_values("Location") == ["./"]
    assert calls["restart"] == 1
    # free-running -> JASPER_AIRPLAY_FREE_RUNNING=yes
    assert calls["write"] and calls["write"][0][1] == {
        airplay_setup.ENV_VAR: "yes"
    }


def test_post_save_synced_writes_no(monkeypatch):
    token = "y" * 64
    calls = {"write": []}
    monkeypatch.setattr(
        airplay_setup, "write_env_file",
        lambda path, data, mode=0o644: calls["write"].append(dict(data)),
    )
    monkeypatch.setattr(airplay_setup, "_restart_shairport", lambda: None)

    handler = _handler_cls()
    body = ("csrf_token=" + token + "&mode=synced").encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler.do_POST(h)

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert calls["write"] == [{airplay_setup.ENV_VAR: "no"}]


def test_post_save_invalid_mode_redirects_without_write(monkeypatch):
    token = "y" * 64
    wrote = []
    monkeypatch.setattr(
        airplay_setup, "write_env_file",
        lambda *a, **k: wrote.append(True),
    )
    monkeypatch.setattr(airplay_setup, "_restart_shairport", lambda: wrote.append("restart"))

    handler = _handler_cls()
    body = ("csrf_token=" + token + "&mode=bogus").encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler.do_POST(h)

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert h.header_values("Location") == ["./"]
    assert wrote == []  # invalid mode never touches disk or restarts


def test_post_save_rejects_bad_csrf(monkeypatch):
    wrote = []
    monkeypatch.setattr(
        airplay_setup, "write_env_file", lambda *a, **k: wrote.append(True),
    )
    handler = _handler_cls()
    # Form-field token deliberately differs from the cookie -> 403, no write.
    body = b"csrf_token=" + b"a" * 64 + b"&mode=synced"
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + "b" * 64)
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
    assert wrote == []
