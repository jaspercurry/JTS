# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /spotify/ wizard after its migration to the canonical look.

1. Each of the wizard's page states (setup, redirect-URI, manual pre-warn,
   management) renders canonical design-system bytes: it links /assets/app.css
   and the per-page /assets/spotify/spotify.css, carries the shared .app-header,
   embeds the CSRF meta tag, and loads its behaviour as an ES module with no
   inline <script> and no inline on*-handlers.
2. The migration was presentation-only: every <form> keeps its csrf_token
   hidden field, the OAuth + multi-account routes still resolve, and the public
   module surface (render fns, make_server, main) is unchanged. Network / disk
   side effects (spotipy, Registry, systemctl) are mocked like the other web
   tests.
"""
from __future__ import annotations

import http
import types
from email.message import Message
from io import BytesIO


from jasper.web import spotify_setup


# ---------------------------------------------------------------------------
# Render-layer tests (call the page builders directly).
# ---------------------------------------------------------------------------

CSRF = "tok-abcdefghijklmnopqrstuvwx"


def test_setup_wizard_is_canonical_document():
    out = spotify_setup._setup_wizard_html(CSRF).decode()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    assert "/assets/spotify/spotify.css?v=" in out
    # Legacy 620px body wrapper is gone.
    assert "max-width: 620px" not in out


def test_setup_wizard_has_shared_app_header():
    out = spotify_setup._setup_wizard_html(CSRF).decode()
    assert 'class="app-header"' in out
    assert '<use href="#icon-back">' in out


def test_setup_wizard_embeds_csrf_meta_and_field():
    out = spotify_setup._setup_wizard_html(CSRF).decode()
    assert 'meta name="jts-csrf"' in out
    assert f'content="{CSRF}"' in out
    # The form still carries the hidden csrf_token field.
    assert 'name="csrf_token"' in out


def test_setup_wizard_uses_canonical_field_vocabulary():
    out = spotify_setup._setup_wizard_html(CSRF).decode()
    assert 'class="field"' in out
    assert 'class="form-actions"' in out
    assert 'class="btn btn--primary"' in out
    assert 'action="setup-credentials"' in out


def test_setup_wizard_loads_es_module_not_inline_script():
    out = spotify_setup._setup_wizard_html(CSRF).decode()
    assert '<script type="module" src="/assets/spotify/js/main.js">' in out
    # No inline <script> body and no inline event handlers survive.
    before_module = out.split('<script type="module"')[0]
    assert "addEventListener" not in before_module
    assert "copyRedirect" not in before_module
    assert "onclick=" not in out
    assert "onsubmit=" not in out


def test_setup_wizard_shows_disambiguation_note_as_info_card():
    out = spotify_setup._setup_wizard_html(CSRF).decode()
    assert "advanced-note" in out
    assert 'href="/sources/"' in out


def test_management_page_links_to_spotify_tool_pack():
    registry = spotify_setup.Registry(
        accounts=[spotify_setup.Account(name="jasper")],
        default_name="jasper",
    )
    out = spotify_setup._management_html(
        registry,
        "https://example.test/cb?host=jts.local",
        "0123456789abcdef0123456789abcdef",
        "bounce",
        CSRF,
    ).decode()
    assert 'href="/tools/pack/spotify/"' in out
    assert "Manage Spotify tool prompts" in out


def test_redirect_uri_page_renders_copy_row_without_inline_js():
    out = spotify_setup._redirect_uri_page_html(
        "https://example.test/cb?host=jts.local",
        "0123456789abcdef0123456789abcdef",
        "bounce",
        CSRF,
    ).decode()
    assert out.startswith("<!doctype html>")
    assert 'class="app-header"' in out
    # Copy button drives the clipboard via data-* + delegated handler.
    assert 'data-copy-target="redirect-uri"' in out
    assert "onclick=" not in out
    # The redirect URI itself is escaped into the readonly input.
    assert "https://example.test/cb?host=jts.local" in out


def test_redirect_uri_page_reset_form_uses_data_confirm():
    out = spotify_setup._redirect_uri_page_html(
        "https://example.test/cb",
        "0123456789abcdef0123456789abcdef",
        "bounce",
        CSRF,
    ).decode()
    assert 'action="reset-credentials"' in out
    assert 'data-confirm="' in out
    assert 'data-confirm-danger="1"' in out
    assert "jtsConfirmSubmit" not in out


def test_manual_prewarn_page_is_canonical():
    out = spotify_setup._manual_prewarn_page_html(
        "https://accounts.spotify.com/authorize?x=1",
        "brittany",
        CSRF,
    ).decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out
    assert "prewarn" in out
    assert 'name="csrf_token"' in out
    assert "https://accounts.spotify.com/authorize?x=1" in out


def test_mode_picker_carries_no_inline_script():
    out = spotify_setup._mode_picker_html(selected="manual")
    assert "mode-picker" in out
    assert "<script" not in out
    assert 'value="manual"' in out


def test_blank_flash_renders_no_banner():
    out = spotify_setup._setup_wizard_html(CSRF, status_msg="").decode()
    assert 'class="banner' not in out


def test_error_flash_renders_danger_banner():
    out = spotify_setup._setup_wizard_html(
        CSRF, status_msg="Auth exchange failed: nope",
    ).decode()
    assert "banner--danger" in out


# ---------------------------------------------------------------------------
# Account-card render tests (token-health + escaping + confirm).
# ---------------------------------------------------------------------------

def _account(name="brittany", playlists=None):
    return types.SimpleNamespace(name=name, playlists=playlists or {})


def test_account_card_uses_canonical_classes_and_data_confirm():
    out = spotify_setup._account_card_html(
        _account(), is_default=True, is_open=True, status=None, csrf_token=CSRF,
    )
    assert "<details" in out and 'class="account"' in out
    assert 'class="btn btn--default"' in out
    assert 'class="btn btn--danger"' in out
    # Remove is guarded by the shared dialog via data-confirm, not inline JS.
    assert 'data-confirm="' in out
    assert "onsubmit=" not in out
    # The default account gets a tone-themed badge.
    assert "badge" in out


def test_account_card_escapes_untrusted_name_in_confirm():
    out = spotify_setup._account_card_html(
        _account(name="ab<c"), is_default=False, is_open=False,
        status=None, csrf_token=CSRF,
    )
    # The raw "<" must not appear unescaped inside the data-confirm attribute.
    assert "Remove ab<c?" not in out
    assert "ab&lt;c" in out


def test_health_badge_tone_classes():
    from jasper.spotify_router import (
        ACCOUNT_OK,
        ACCOUNT_REVOKED,
        ACCOUNT_NEEDS_OAUTH,
    )
    ok = spotify_setup._health_badge_html(types.SimpleNamespace(state=ACCOUNT_OK, detail=""))
    revoked = spotify_setup._health_badge_html(
        types.SimpleNamespace(state=ACCOUNT_REVOKED, detail="")
    )
    needs = spotify_setup._health_badge_html(
        types.SimpleNamespace(state=ACCOUNT_NEEDS_OAUTH, detail="")
    )
    assert "health-ok" in ok
    assert "health-revoked" in revoked
    assert "health-warn" in needs
    # None status renders nothing (defensive).
    assert spotify_setup._health_badge_html(None) == ""


def test_playlist_section_add_form_has_no_inline_js():
    out = spotify_setup._account_playlists_section_html(
        _account(playlists={"spotify:playlist:1": "My Mix"}), csrf_token=CSRF,
    )
    assert 'class="pl-add"' in out
    assert 'class="btn btn--primary pl-submit"' in out
    assert "onsubmit=" not in out
    assert "onclick=" not in out
    # Remove-playlist form is dialog-guarded with the escaped name.
    assert 'data-confirm="' in out


# ---------------------------------------------------------------------------
# Handler / routing tests (drive do_GET/do_POST on a real handler instance).
# ---------------------------------------------------------------------------
#
# After the canonical migration the Handler's do_GET/do_POST delegate to
# instance helpers it defines on itself (_render_index, _render_redirect_uri_page,
# _send_see_other, _send_json, ...). So -- mirroring the proven harness in
# tests/test_web_wifi_setup.py -- we instantiate the *real* Handler class via
# __new__ (skipping BaseHTTPRequestHandler.__init__'s socket plumbing), bolt the
# synthetic request I/O onto it, and stub only the network-touching surface so
# those real helper methods run without a socket.


class _Request:
    """A real Handler instance wired to a synthetic request.

    Wraps ``handler_cls.__new__(handler_cls)`` so ``self`` is a genuine Handler:
    the migrated ``_render_*`` / ``_send_*`` helpers exist and execute. Captures
    the response status into ``.status`` and emitted headers into ``.sent_headers``
    (exposed via ``header_values``); the body is read off ``.wfile`` as before.
    """

    def __init__(self, handler_cls, path: str, body: bytes = b"",
                 cookies: str = "") -> None:
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

        self.status = None
        self.sent_headers = []
        self.wfile = h.wfile

        # Override the socket-touching surface of BaseHTTPRequestHandler so the
        # real helper methods run without a real connection.
        h.send_response = self._record_status
        h.send_response_only = self._record_status
        h.send_header = lambda name, value: self.sent_headers.append((name, value))
        h.end_headers = lambda: None
        h.send_error = self._record_status
        h.address_string = lambda: "127.0.0.1"
        h.log_message = lambda *a, **k: None
        self._handler = h

    def _record_status(self, status, *a, **k):
        self.status = int(status)

    def do_GET(self):
        self._handler.do_GET()

    def do_POST(self):
        self._handler.do_POST()

    def header_values(self, name):
        return [v for n, v in self.sent_headers if n.lower() == name.lower()]


def _handler_cls(client_id="", mode="bounce", registry_path="/tmp/no.json"):
    return spotify_setup._make_handler({
        "client_id": client_id,
        "mode": mode,
        "bounce_redirect_uri": "https://example.test/cb?host=jts.local",
        "manual_redirect_uri": "http://127.0.0.1:8888/callback",
        "registry_path": registry_path,
    })


def test_public_surface_is_stable():
    assert callable(spotify_setup.make_server)
    assert callable(spotify_setup.main)
    assert callable(spotify_setup._make_handler)
    assert callable(spotify_setup._setup_wizard_html)
    assert callable(spotify_setup._management_html)


def test_get_root_unconfigured_renders_setup_wizard():
    h = _Request(_handler_cls(client_id=""), "/")
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out
    # Unconfigured state shows the create-app wizard.
    assert "Create a Spotify Developer App" in out


def test_get_root_with_tools_return_uses_tool_pack_back_link():
    h = _Request(
        _handler_cls(client_id=""),
        "/?return_to=%2Ftools%2Fpack%2Fspotify%2F",
    )
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert 'href="/tools/pack/spotify/"' in out


def test_get_root_rejects_off_origin_return_link():
    h = _Request(
        _handler_cls(client_id=""),
        "/?return_to=%2F%2Fevil.test%2F",
    )
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert 'href="/"' in out
    assert "evil.test" not in out


def test_get_root_configured_no_accounts_renders_redirect_page(monkeypatch):
    # No accounts -> redirect-URI page. Registry.load is mocked to empty.
    monkeypatch.setattr(
        spotify_setup.Registry, "load",
        classmethod(lambda cls, path: types.SimpleNamespace(
            accounts=[], default_name="",
        )),
    )
    h = _Request(_handler_cls(client_id="0123456789abcdef0123456789abcdef"), "/")
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "Add this redirect URL to your Spotify app" in out
    assert 'class="app-header"' in out


def test_get_unknown_path_404s():
    h = _Request(_handler_cls(), "/nope")
    h.do_GET()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_unknown_route_404s():
    h = _Request(_handler_cls(), "/nope", body=b"")
    h.do_POST()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_setup_credentials_rejects_bad_csrf():
    # Form-field token differs from the cookie token -> 403, no write.
    body = b"csrf_token=" + b"a" * 64 + b"&client_id=x&mode=bounce"
    h = _Request(_handler_cls(), "/setup-credentials", body=body,
                 cookies="jts_csrf=" + "b" * 64)
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)


def test_post_setup_credentials_saves_and_restarts(monkeypatch):
    token = "y" * 64
    calls = {"write": [], "restart": []}
    monkeypatch.setattr(
        spotify_setup, "_write_creds_file",
        lambda cid, mode: calls["write"].append((cid, mode)),
    )
    monkeypatch.setattr(
        spotify_setup, "_restart_spotify_consumers",
        lambda: calls["restart"].append(True),
    )
    monkeypatch.setattr(spotify_setup, "_invalidate_health_cache", lambda: None)

    cid = "0123456789abcdef0123456789abcdef"
    body = ("csrf_token=" + token + "&client_id=" + cid + "&mode=bounce").encode()
    h = _Request(_handler_cls(), "/setup-credentials", body=body,
                 cookies="jts_csrf=" + token)
    h.do_POST()

    # 303 redirect home with a flash; creds written + consumers restarted.
    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert calls["write"] == [(cid, "bounce")]
    assert calls["restart"] == [True]


def test_post_setup_credentials_rejects_malformed_client_id(monkeypatch):
    token = "y" * 64
    wrote = []
    monkeypatch.setattr(
        spotify_setup, "_write_creds_file",
        lambda cid, mode: wrote.append((cid, mode)),
    )
    # Too short / not 32 hex -> redirect with error flash, no write.
    body = ("csrf_token=" + token + "&client_id=nothex&mode=bounce").encode()
    h = _Request(_handler_cls(), "/setup-credentials", body=body,
                 cookies="jts_csrf=" + token)
    h.do_POST()
    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert wrote == []


def test_playlist_preview_is_json_and_needs_no_csrf(monkeypatch):
    # Read-only AJAX endpoint: returns JSON, no CSRF required.
    monkeypatch.setattr(spotify_setup, "parse_playlist_uri", lambda raw: "")
    h = _Request(_handler_cls(client_id="0123456789abcdef0123456789abcdef"),
                 "/playlist-preview?account=x&url=notaurl")
    h.do_GET()
    assert h.status == 200
    ctype = h.header_values("Content-Type")
    assert any("application/json" in c for c in ctype)
    body = h.wfile.getvalue().decode()
    assert "error" in body
