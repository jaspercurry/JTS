# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice setup shell and HTTP routing."""
from __future__ import annotations

import http
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import pytest

from jasper import atomic_io, env_file
from jasper.voice.catalog import PROVIDERS
from jasper.web import chrome, voice_setup

from ._web_test_helpers import assert_canonical_page, make_real_handler


def _render(state: dict | None = None, flash: str = "") -> str:
    return voice_setup._index_html(
        state or {},
        "tok-abcdefghijklmnopqrstuvwx",
        status_msg=flash,
        selected="gemini",
    ).decode()


# --- canonical-shell render assertions -------------------------------------


def test_voice_page_is_canonical_document():
    out = _render()
    assert_canonical_page(out)
    # The legacy bespoke wrapper is gone.
    assert "max-width: 620px" not in out
    assert "nav-back" not in out


def test_voice_page_links_page_css():
    out = _render()
    assert "/assets/voice/voice.css?v=" in out


def test_voice_page_has_shared_app_header():
    out = _render()
    assert_canonical_page(out)
    assert '<h1 class="app-header__title">Voice</h1>' in out
    assert '<use href="#icon-back">' in out
    assert 'href="/assistant/" aria-label="Assistant"' in out


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
    assert 'class="form-actions ' in out
    assert 'class="btn btn--primary"' in out


def test_voice_page_offers_every_provider_before_a_key_is_saved():
    out = _render()
    for provider in PROVIDERS:
        assert f'<option value="{provider.id}"' in out
    assert 'name="gemini_key"' in out
    assert 'formaction="save-test"' in out


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
        assert chrome.canonical_banner(flash) in _render(flash=flash)


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


def test_get_root_renders_canonical_page(tmp_path):
    h, _ = make_real_handler(_handler_cls(tmp_path), "/")
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert_canonical_page(out)
    for p in PROVIDERS:
        assert p.label in out


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.id)
@pytest.mark.parametrize("page", ["/", "/costs"])
def test_choose_provider_is_read_only_and_disclosures_start_closed(tmp_path, monkeypatch, provider, page):
    state_path = tmp_path / "voice.env"
    state = {"JASPER_VOICE_PROVIDER": "gemini", "JASPER_GEMINI_MODEL": "custom-live"}
    atomic_io.write_env_file(str(state_path), state)
    calls = []
    monkeypatch.setattr(voice_setup, "restart_voice_daemon", lambda: calls.append("restart"))
    monkeypatch.setattr(voice_setup, "refresh_provider_cache", lambda *a, **k: calls.append("refresh"))
    h, _ = make_real_handler(_handler_cls(tmp_path), f"{page}?provider={provider.id}")
    h.do_GET()
    assert h.status == 200
    assert env_file.read_env_file(str(state_path)) == state
    assert not (tmp_path / "voice_keys.env").exists()
    assert calls == []

    class FormParser(HTMLParser):
        forms = 0
        disclosures = 0
        disclosure_depth = 0
        keys = []

        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            if tag == "form":
                assert self.forms == 0
                self.forms += 1
            if tag == "details":
                assert "open" not in attrs
                assert self.disclosure_depth == 0
                self.disclosure_depth += 1
                self.disclosures += 1
            if tag == "input" and attrs.get("type") == "password":
                assert attrs.get("value", "") == ""
                self.keys.append(attrs["name"])

        def handle_endtag(self, tag):
            if tag == "form":
                self.forms -= 1
            if tag == "details":
                self.disclosure_depth -= 1

    parser = FormParser()
    parser.feed(h.wfile.getvalue().decode())
    assert parser.forms == 0
    assert parser.disclosures > 0
    assert parser.keys == ([f"{provider.id}_key"] if page == "/" else [])


def test_post_unknown_route_404s(tmp_path):
    h, _ = make_real_handler(_handler_cls(tmp_path), "/nope", body=b"")
    h.do_POST()
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
