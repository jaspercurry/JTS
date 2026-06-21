# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /voice/ wizard after its migration to the canonical look.

1. The page renders canonical design-system bytes (links /assets/app.css and
   the page-specific /assets/voice/voice.css, carries the shared .app-header,
   embeds the CSRF meta tag) and delivers its behaviour as an ES module --
   no inline <script>.
2. The migration was presentation-only: the server-rendered POST flows
   (/save, /clear-credentials, /refresh-models, /pricing, /pricing-import),
   their CSRF + flash plumbing, and the public module surface
   (_index_html / make_server / main) are unchanged.

The pure-function and the full POST-flow coverage lives in the existing
tests/test_voice_setup.py (driven through a real ThreadingHTTPServer); this
file focuses on the canonical-shell migration and a couple of handler-wiring
smoke checks.
"""
from __future__ import annotations

import http
import threading
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from io import BytesIO

from jasper.voice.catalog import PROVIDERS
from jasper.web import _common, voice_setup


def _render(state: dict | None = None, flash: str = "") -> str:
    return voice_setup._index_html(
        state or {},
        "tok-abcdefghijklmnopqrstuvwx",
        status_msg=flash,
    ).decode()


# --- canonical-shell render assertions -------------------------------------


def test_voice_page_is_canonical_document():
    out = _render()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    # The legacy bespoke wrapper is gone.
    assert "max-width: 620px" not in out
    assert "nav-back" not in out


def test_voice_page_links_page_css():
    out = _render()
    assert "/assets/voice/voice.css?v=" in out


def test_voice_page_has_shared_app_header():
    out = _render()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Voice provider</h1>' in out
    assert '<use href="#icon-back">' in out


def test_voice_page_embeds_csrf_meta():
    out = _render()
    assert 'meta name="jts-csrf"' in out
    assert 'content="tok-abcdefghijklmnopqrstuvwx"' in out


def test_voice_save_form_preserves_csrf_field_and_action():
    out = _render()
    assert 'action="save"' in out
    assert 'id="save-form"' in out
    assert 'name="csrf_token"' in out


def test_voice_page_uses_canonical_field_vocabulary():
    out = _render()
    assert 'class="field"' in out
    assert 'class="form-actions"' in out
    assert 'class="btn btn--primary"' in out
    assert 'class="info-card provider-card"' in out


def test_voice_page_renders_all_provider_cards_and_radios():
    out = _render()
    for p in PROVIDERS:
        assert p.label in out
        # one active-provider radio per provider
        assert f'name="active" value="{p.id}"' in out


def test_voice_page_has_save_and_test_and_first_time_key_metadata():
    out = _render()
    assert "Save and Test" in out
    assert 'formaction="save-test"' in out
    for p in PROVIDERS:
        assert f'data-provider-radio="{p.id}"' in out
        assert f'data-provider-key="{p.id}"' in out
        assert f'data-provider-radio-row="{p.id}"' in out


def test_voice_page_loads_es_module_not_inline_script():
    out = _render()
    assert '<script type="module" src="/assets/voice/js/main.js">' in out
    before_module = out.split('<script type="module"')[0]
    # No inline confirm/clipboard JS leaked into the document body.
    assert "jtsConfirmSubmit" not in before_module
    assert "navigator.clipboard" not in before_module
    assert "onclick=" not in before_module
    assert "onsubmit=" not in before_module


def test_voice_clear_key_uses_data_confirm_not_inline_js():
    # A configured provider renders a clear-key form; the confirm rides in a
    # data-* attribute consumed by the ES module, never inline JS.
    out = _render(state={"GEMINI_API_KEY": "AIzaTESTKEY", "JASPER_VOICE_PROVIDER": "gemini"})
    assert 'action="clear-credentials"' in out
    assert "data-confirm=" in out
    assert 'data-confirm-danger="1"' in out


def test_voice_blank_flash_renders_no_banner():
    assert 'class="banner' not in _render(flash="")


def test_voice_flash_is_routed_through_canonical_banner():
    # The page's only job is to route the flash through the shared
    # canonical_banner() (presentation parity). The exact severity classing
    # is canonical_banner's contract, covered by test_web_common.py — so we
    # assert the page embeds *exactly* what the shared helper produces for
    # the same message, for the success/failure/cleared flashes the save
    # handlers actually write back. (Asserting equality with the helper,
    # rather than a hard-coded tone class, keeps this test correct if the
    # shared classing is ever retuned.)
    for flash in (
        "Saved. Voice daemon restarting on Google Gemini.",
        "Could not refresh OpenAI models: connection failed",
        "Cleared Gemini Live credentials.",
    ):
        assert _common.canonical_banner(flash) in _render(flash=flash)


# --- handler wiring smoke checks (presentation-preserving behaviour) -------
#
# GET render + the early-return POST branches (unknown route 404, bad-CSRF
# 403) are driven through a minimal fake handler. The full save/clear/refresh
# flows reach `self._handle_*` instance methods on the real Handler, so those
# are exercised end-to-end through an actual ThreadingHTTPServer below
# (mirroring tests/test_voice_setup.py).


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for the early-return branches."""

    def __init__(self, path: str, body: bytes = b"", cookies: str = "") -> None:
        self.path = path
        self.headers = Message()
        self.headers["Content-Length"] = str(len(body))
        self.headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookies:
            self.headers["Cookie"] = cookies
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.status = None
        self.sent_headers = []
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = int(status)

    def send_response_only(self, status):
        self.status = int(status)

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def send_error(self, status, *a, **k):
        self.status = int(status)

    def address_string(self):
        return "127.0.0.1"

    def log_message(self, *a, **k):
        pass

    def header_values(self, name):
        return [v for n, v in self.sent_headers if n.lower() == name.lower()]


def _handler_cls(tmp_path):
    return voice_setup._make_handler({
        "state_path": str(tmp_path / "voice.env"),
        # WS1 Phase 4a — the split-out keys file (mirrors make_server's cfg);
        # point it at the tempdir so handlers never touch /var/lib/jasper-secrets.
        "keys_path": str(tmp_path / "voice_keys.env"),
        "discovery_cache_path": str(tmp_path / "discovery.json"),
        "discovery_http_client": None,
        "pricing_path": str(tmp_path / "pricing.json"),
        "assistant_loudness_profile_path": str(tmp_path / "loudness.json"),
        "loudness_seed_fn": voice_setup.ensure_seed_profile,
    })


def test_public_surface_is_stable():
    assert callable(voice_setup.make_server)
    assert callable(voice_setup.main)
    assert callable(voice_setup._index_html)
    assert callable(voice_setup._make_handler)


def test_get_root_renders_canonical_page(tmp_path):
    handler = _handler_cls(tmp_path)
    h = _FakeHandler("/")
    handler.do_GET(h)
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out
    for p in PROVIDERS:
        assert p.label in out


def test_post_unknown_route_404s(tmp_path):
    handler = _handler_cls(tmp_path)
    h = _FakeHandler("/nope", body=b"")
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_save_rejects_bad_csrf(tmp_path):
    """A POST whose form token doesn't match the cookie token is rejected
    before any save logic runs. Driven through a real ThreadingHTTPServer
    (the bad-CSRF branch returns straight from reject_csrf, so the
    no-redirect opener sees the 403)."""
    state_path = tmp_path / "voice_provider.env"
    server = voice_setup.make_server(
        ("127.0.0.1", 0),
        state_path=str(state_path),
        discovery_cache_path=str(tmp_path / "discovery.json"),
        pricing_path=str(tmp_path / "pricing.json"),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = (
            "csrf_token=" + "a" * 64 + "&active=gemini&gemini_key=AIzaTESTKEY"
        ).encode()
        req = urllib.request.Request(
            base + "/save", data=body, method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": "jts_csrf=" + "b" * 64,
            },
        )

        class _NoRedirect(urllib.request.HTTPErrorProcessor):
            def http_response(self, request, response):
                return response
            https_response = http_response

        op = urllib.request.build_opener(_NoRedirect())
        try:
            status = op.open(req).status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == int(http.HTTPStatus.FORBIDDEN)
        assert not state_path.exists()
    finally:
        server.shutdown()
        server.server_close()


# NOTE: the full save / clear / refresh / pricing POST flows (write +
# restart, server-side "no key, no activate" guard, flash text) are exercised
# end-to-end against a real ThreadingHTTPServer in tests/test_voice_setup.py;
# those still apply unchanged after the presentation-only migration, so they
# are not duplicated here.
