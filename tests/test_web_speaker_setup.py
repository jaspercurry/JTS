# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /speaker/ wizard after its migration to the canonical look.

1. The page renders canonical design-system bytes (links /assets/app.css,
   carries the shared .app-header, embeds the CSRF meta tag) and delivers its
   behaviour as an ES module -- no inline <script>.
2. The migration was presentation-only: the server-rendered POST /save flow
   (validate -> duplicate-check -> write -> restart) and the public module
   surface (render fn, make_server, main) are unchanged.
"""
from __future__ import annotations

import http
import types

from jasper.speaker_name import DEFAULT_SPEAKER_NAME, SpeakerNameError
from jasper.web import speaker_setup

from ._web_test_helpers import FakeHandler


def _render(
    current_name: str = "Kitchen",
    current_room: str = "",
    flash: str = "",
    hostname: str = "jts.local",
) -> str:
    return speaker_setup._index_html(
        current_name=current_name,
        current_room=current_room,
        hostname=hostname,
        csrf_token="tok-abcdefghijklmnopqrstuvwx",
        status_msg=flash,
    ).decode()


def test_speaker_page_is_canonical_document():
    out = _render()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    assert "max-width: 620px" not in out


def test_speaker_page_has_shared_app_header():
    out = _render()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Speaker name</h1>' in out
    assert '<use href="#icon-back">' in out


def test_speaker_page_embeds_csrf_meta():
    out = _render()
    assert 'meta name="jts-csrf"' in out
    assert 'content="tok-abcdefghijklmnopqrstuvwx"' in out


def test_speaker_form_preserves_csrf_field_and_save_action():
    out = _render()
    assert 'action="./save"' in out
    assert 'method="post"' in out
    assert 'type="hidden"' in out


def test_speaker_form_uses_canonical_field_vocabulary():
    out = _render()
    assert 'class="field"' in out
    assert 'type="text" name="name"' in out
    assert 'class="form-actions"' in out
    assert 'class="btn btn--primary"' in out


def test_speaker_page_loads_es_module_not_inline_script():
    out = _render()
    assert '<script type="module" src="/assets/speaker/js/main.js">' in out
    before_module = out.split('<script type="module"')[0]
    assert "jtsConfirm(" not in before_module
    assert "addEventListener" not in before_module


def test_speaker_page_passes_default_via_data_attr_not_inline_js():
    out = _render()
    assert 'data-default="' + DEFAULT_SPEAKER_NAME + '"' in out


def test_speaker_current_name_is_escaped_into_value():
    out = _render(current_name='Brittany "B"')
    assert "Brittany" in out
    assert "&quot;B&quot;" in out


def test_speaker_hint_shows_configured_hostname_not_hardcoded():
    # Regression: the "The address stays X" hint must reflect this
    # speaker's actual JASPER_HOSTNAME, not a baked-in "jts.local".
    out = _render(hostname="jts2.local")
    assert "The address stays <code>jts2.local</code>" in out
    assert "<code>jts.local</code>" not in out


def test_speaker_hint_escapes_hostname():
    out = _render(hostname='evil"<script>')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_speaker_blank_flash_renders_no_banner():
    assert 'class="banner' not in _render(flash="")


def test_speaker_saved_flash_renders_ok_banner():
    out = _render(flash='Saved. Speaker renamed to "Kitchen". Services restarting.')
    assert "banner--ok" in out


def _handler_cls():
    return speaker_setup._make_handler({"state_path": "/tmp/does-not-matter.env"})


def test_public_surface_is_stable():
    assert callable(speaker_setup.make_server)
    assert callable(speaker_setup.main)
    assert callable(speaker_setup._index_html)


def test_get_root_renders_canonical_page(monkeypatch):
    monkeypatch.setattr(
        speaker_setup, "read_state",
        lambda path: types.SimpleNamespace(name="Kitchen", room=""),
    )
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


def test_post_save_validation_error_redirects_with_flash(monkeypatch):
    token = "z" * 64

    def boom(_name):
        raise SpeakerNameError("Name too long")

    monkeypatch.setattr(speaker_setup, "validate_name", boom)
    handler = _handler_cls()
    # csrf_token is the form field (_common.CSRF_FORM_FIELD); jts_csrf is the
    # double-submit cookie. They must carry the same token to pass guard_mutating_request.
    body = ("csrf_token=" + token + "&name=waytoolong").encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert h.header_values("Location") == ["./"]


def test_post_save_applies_rename_and_restarts(monkeypatch):
    token = "y" * 64
    calls = {"apply": [], "write": []}

    monkeypatch.setattr(speaker_setup, "validate_name", lambda n: n.strip())
    monkeypatch.setattr(speaker_setup, "validate_room", lambda r: r.strip())
    monkeypatch.setattr(
        speaker_setup, "read_state",
        lambda path: types.SimpleNamespace(name="OldName", room=""),
    )
    monkeypatch.setattr(speaker_setup, "_find_conflicts", lambda name: [])
    monkeypatch.setattr(
        speaker_setup, "write_state",
        lambda name, room, path, mode=0o644: (
            calls["write"].append((name, room)) or name
        ),
    )
    monkeypatch.setattr(
        speaker_setup, "_apply_name",
        lambda name: calls["apply"].append(name),
    )

    handler = _handler_cls()
    # csrf_token = form field (CSRF_FORM_FIELD); jts_csrf = double-submit cookie.
    body = ("csrf_token=" + token + "&name=NewName&room=Kitchen").encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler.do_POST(h)

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert calls["write"] == [("NewName", "Kitchen")]
    assert calls["apply"] == ["NewName"]


def test_post_save_rejects_bad_csrf(monkeypatch):
    monkeypatch.setattr(
        speaker_setup, "read_state",
        lambda path: types.SimpleNamespace(name="OldName"),
    )
    handler = _handler_cls()
    # Form-field token (csrf_token) deliberately differs from the cookie token,
    # so the double-submit compare fails -> 403, no rename.
    body = b"csrf_token=" + b"a" * 64 + b"&name=NewName"
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + "b" * 64)
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
