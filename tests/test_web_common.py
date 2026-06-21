# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.web._common helpers.

The infrastructure tested here (flash cookies, CSRF tokens, the unified
HTML response helper, the 303 redirect helper) is exercised through fake
handlers rather than real http.server instances — keeps the suite fast
and lets us assert on exactly the header bytes that go on the wire.
"""
from __future__ import annotations

import http
import os
import threading
from email.message import Message
from io import BytesIO

import pytest

from jasper.web import _common


class _FakeHandler:
    """Minimal stand-in for a BaseHTTPRequestHandler. Captures the
    response line, headers, and body so tests can assert on the exact
    wire bytes that would have been written."""

    def __init__(self, cookies: str = "") -> None:
        self.headers = Message()
        if cookies:
            self.headers["Cookie"] = cookies
        self._status: int | None = None
        self._headers: list[tuple[str, str]] = []
        self.wfile = BytesIO()
        self._ended = False

    def send_response(self, status: int) -> None:
        assert self._status is None, "double send_response"
        self._status = status

    def send_header(self, name: str, value: str) -> None:
        assert self._status is not None, "header before status"
        assert not self._ended, "header after end_headers"
        self._headers.append((name, value))

    def end_headers(self) -> None:
        assert self._status is not None
        self._ended = True

    # --- assertions used in tests ---

    def header_values(self, name: str) -> list[str]:
        return [v for n, v in self._headers if n.lower() == name.lower()]

    def set_cookies(self) -> list[str]:
        return self.header_values("Set-Cookie")


# ----------------------------------------------------------------------
# Cookie parsing
# ----------------------------------------------------------------------


def test_read_request_cookies_handles_no_header():
    h = _FakeHandler()
    assert _common._read_request_cookies(h) == {}


def test_read_request_cookies_parses_one():
    h = _FakeHandler(cookies="jts_flash=hello")
    assert _common._read_request_cookies(h) == {"jts_flash": "hello"}


def test_read_request_cookies_parses_multiple_with_whitespace():
    h = _FakeHandler(cookies="jts_flash=hi; jts_csrf=abc")
    parsed = _common._read_request_cookies(h)
    assert parsed["jts_flash"] == "hi"
    assert parsed["jts_csrf"] == "abc"


def test_read_request_cookies_skips_malformed_entry():
    # Empty name entries (trailing semicolon, double semicolon) are
    # dropped — common artifact of cookie editors. The valid pair must
    # still come through.
    h = _FakeHandler(cookies="; jts_flash=x;;")
    assert _common._read_request_cookies(h) == {"jts_flash": "x"}


# ----------------------------------------------------------------------
# Flash cookie
# ----------------------------------------------------------------------


def test_read_flash_returns_empty_when_unset():
    h = _FakeHandler()
    assert _common.read_flash(h) == ""


def test_read_flash_urldecodes_value():
    # Server sets the flash via send_see_other(flash="Saved.+Voice...")
    # which url-encodes the message. read_flash reverses that.
    encoded = "Saved.%20Voice%20daemon%20restarting."
    h = _FakeHandler(cookies=f"jts_flash={encoded}")
    assert _common.read_flash(h) == "Saved. Voice daemon restarting."


def test_read_flash_tolerates_malformed_encoding():
    # Truncated %-escape — must not raise, must not feed garbage into
    # the page template.
    h = _FakeHandler(cookies="jts_flash=bad%E0")
    out = _common.read_flash(h)
    assert isinstance(out, str)  # tolerated, not raised


def test_send_see_other_sets_flash_cookie_with_correct_shape():
    h = _FakeHandler()
    _common.send_see_other(h, "./", flash="Saved.")
    assert h._status == http.HTTPStatus.SEE_OTHER
    locations = h.header_values("Location")
    assert locations == ["./"]
    cookies = h.set_cookies()
    assert len(cookies) == 1
    cookie = cookies[0]
    # Required attributes for the flash cookie's contract:
    assert cookie.startswith("jts_flash=Saved.")
    assert "Path=/" in cookie
    assert "Max-Age=15" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


def test_send_see_other_omits_set_cookie_when_no_flash():
    h = _FakeHandler()
    _common.send_see_other(h, "./")
    assert h.set_cookies() == []


def test_send_see_other_emits_cache_control_no_store():
    # Without no-store, browsers may cache the 303 and back-navigation
    # can replay a stale state.
    h = _FakeHandler()
    _common.send_see_other(h, "./", flash="ok")
    assert h.header_values("Cache-Control") == ["no-store"]


def test_send_see_other_urlencodes_special_characters_in_flash():
    h = _FakeHandler()
    _common.send_see_other(h, "./", flash="Saved 100%; ready.")
    cookie = h.set_cookies()[0]
    # `;` is a cookie separator — must be encoded. So must spaces.
    assert "Saved%20100%25%3B%20ready." in cookie


# ----------------------------------------------------------------------
# CSRF: cookie minting + verification
# ----------------------------------------------------------------------


def test_begin_request_mints_csrf_when_cookie_absent():
    h = _FakeHandler()
    ctx = _common.begin_request(h)
    assert _common._is_valid_token(ctx["csrf_token"])
    assert ctx["_csrf_mint"] is True


def test_begin_request_reuses_valid_existing_csrf_cookie():
    existing = "abc-DEF_123" + "x" * 24  # 35 chars, valid alphabet
    h = _FakeHandler(cookies=f"jts_csrf={existing}")
    ctx = _common.begin_request(h)
    assert ctx["csrf_token"] == existing
    assert ctx["_csrf_mint"] is False


def test_begin_request_replaces_invalid_existing_csrf_cookie():
    # 8 chars — too short, doesn't pass _is_valid_token; must mint new.
    h = _FakeHandler(cookies="jts_csrf=abc")
    ctx = _common.begin_request(h)
    assert ctx["csrf_token"] != "abc"
    assert ctx["_csrf_mint"] is True


def test_begin_request_loads_flash_into_context():
    h = _FakeHandler(cookies="jts_flash=Saved.")
    ctx = _common.begin_request(h)
    assert ctx["flash"] == "Saved."
    assert ctx["_flash_set"] is True


def test_guard_mutating_request_returns_true_for_matching_pair():
    token = "a" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token}")
    assert _common.guard_mutating_request(h, {_common.CSRF_FORM_FIELD: token}) is True


def test_guard_mutating_request_returns_false_for_mismatch():
    token_a = "a" * 64
    token_b = "b" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token_a}")
    assert _common.guard_mutating_request(h, {_common.CSRF_FORM_FIELD: token_b}) is False


def test_guard_mutating_request_returns_false_when_cookie_missing():
    h = _FakeHandler()
    assert _common.guard_mutating_request(h, {_common.CSRF_FORM_FIELD: "a" * 64}) is False


def test_guard_mutating_request_returns_false_when_form_field_missing():
    token = "a" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token}")
    assert _common.guard_mutating_request(h, {}) is False


def test_guard_mutating_request_returns_false_when_both_invalid():
    # Two short strings happen to compare_digest-equal but they shouldn't
    # pass our shape gate.
    h = _FakeHandler(cookies="jts_csrf=abc")
    assert _common.guard_mutating_request(h, {_common.CSRF_FORM_FIELD: "abc"}) is False


def test_guard_mutating_request_accepts_token_via_x_csrf_token_header():
    """For JS-driven POSTs (fetch with no body / JSON body) where a
    hidden form field is awkward, the token can ride on the
    X-CSRF-Token header. Same constant-time compare against the
    cookie."""
    token = "z" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token}")
    h.headers["X-CSRF-Token"] = token
    assert _common.guard_mutating_request(h) is True
    assert _common.guard_mutating_request(h, {}) is True


def test_guard_mutating_request_rejects_mismatched_header_token():
    cookie_token = "a" * 64
    bad_header_token = "b" * 64
    h = _FakeHandler(cookies=f"jts_csrf={cookie_token}")
    h.headers["X-CSRF-Token"] = bad_header_token
    assert _common.guard_mutating_request(h) is False


def test_csrf_meta_html_escapes_token():
    out = _common.csrf_meta_html('"><script>x</script>')
    assert "<script>" not in out
    assert "&quot;" in out


def test_csrf_fetch_helpers_js_defines_shared_header_helpers():
    out = _common.csrf_fetch_helpers_js()

    assert "function csrfHeaders(headers)" in out
    assert "function jsonHeaders()" in out
    assert "X-CSRF-Token" in out
    assert "'Content-Type': 'application/json'" in out


def test_csrf_field_html_escapes_token_value():
    # Defense-in-depth — secrets.token_urlsafe never produces HTML-active
    # chars, but if the cookie was poisoned by a man-in-the-middle the
    # rendered value must not break out of the input.
    bad = 'x" onclick="alert(1)'
    out = _common.csrf_field_html(bad)
    assert "&quot;" in out
    assert "alert(1)" in out  # the text survives, just escaped
    assert '" onclick="' not in out  # but the attribute injection doesn't


def test_reject_csrf_sends_403():
    h = _FakeHandler()
    _common.reject_csrf(h)
    assert h._status == http.HTTPStatus.FORBIDDEN
    assert h.header_values("Cache-Control") == ["no-store"]


# ----------------------------------------------------------------------
# Unified HTML response
# ----------------------------------------------------------------------


def test_send_html_response_emits_no_store():
    h = _FakeHandler()
    _common.begin_request(h)
    _common.send_html_response(h, b"<html></html>")
    assert h.header_values("Cache-Control") == ["no-store"]


def test_send_html_response_sets_csrf_cookie_on_first_render():
    h = _FakeHandler()
    _common.begin_request(h)  # mints new token → marks for set-cookie
    _common.send_html_response(h, b"x")
    cookies = h.set_cookies()
    assert len(cookies) == 1
    assert cookies[0].startswith("jts_csrf=")
    assert "SameSite=Strict" in cookies[0]
    assert "Max-Age=2592000" in cookies[0]  # 30 days


def test_send_html_response_does_not_resend_csrf_on_subsequent_render():
    token = "a" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token}")
    _common.begin_request(h)
    _common.send_html_response(h, b"x")
    assert h.set_cookies() == []  # nothing to set — cookie already there


def test_send_html_response_clears_flash_cookie_when_one_was_read():
    h = _FakeHandler(cookies="jts_flash=Saved.")
    _common.begin_request(h)
    _common.send_html_response(h, b"x")
    cookies = h.set_cookies()
    clear_cookies = [c for c in cookies if c.startswith("jts_flash=")]
    assert len(clear_cookies) == 1
    assert "Max-Age=0" in clear_cookies[0]


def test_send_html_response_works_without_begin_request_context():
    # Defensive: if a wizard hasn't migrated yet and calls
    # send_html_response without begin_request first, it should still
    # produce a valid response (just no CSRF cookie, no flash clear).
    h = _FakeHandler()
    _common.send_html_response(h, b"x", status=200)
    assert h._status == 200
    assert h.header_values("Cache-Control") == ["no-store"]
    assert h.set_cookies() == []


# ----------------------------------------------------------------------
# systemd restarts
# ----------------------------------------------------------------------


def test_restart_systemd_units_routes_through_broker_no_block(monkeypatch):
    # WS1 Phase 3: restart_systemd_units delegates to jasper-control's restart
    # broker (manage_units) instead of shelling out to systemctl directly.
    calls = []

    def fake_manage(*units, **kwargs):
        calls.append((units, kwargs))
        return {"ok": True}

    monkeypatch.setattr(_common, "manage_units", fake_manage)

    _common.restart_systemd_units(
        "jasper-voice", "jasper-control", "jasper-mux",
    )

    assert len(calls) == 1
    units, kwargs = calls[0]
    assert units == ("jasper-voice", "jasper-control", "jasper-mux")
    assert kwargs["verb"] == "restart"
    assert kwargs["no_block"] is True


def test_restart_voice_daemon_parks_when_provider_unset(monkeypatch):
    calls = []

    monkeypatch.setattr(_common, "read_active_provider", lambda: "")
    monkeypatch.setattr(
        _common, "manage_units",
        lambda *units, **kwargs: calls.append((units, kwargs)) or {"ok": True},
    )

    _common.restart_voice_daemon()

    assert calls == []


def test_restart_voice_daemon_restarts_when_provider_set(monkeypatch):
    calls = []

    monkeypatch.setattr(_common, "read_active_provider", lambda: "openai")
    monkeypatch.setattr(_common, "bonded_follower_active", lambda: False)
    monkeypatch.setattr(
        _common, "manage_units",
        lambda *units, **kwargs: calls.append((units, kwargs.get("verb")))
        or {"ok": True},
    )

    _common.restart_voice_daemon()

    # WS1 Phase 3b-2: ONLY the runtime restart via the broker — no `systemctl
    # enable`. jasper-voice is enabled at install and the root
    # jasper-aec-reconcile owns its boot-enable; the non-root jasper-control is
    # deliberately not granted polkit manage-unit-files (which can't be
    # unit-scoped and would re-open restart-of-any-unit).
    assert calls == [
        (("jasper-voice",), "restart"),
    ]


# ----------------------------------------------------------------------
# Canonical design system (canonical_page)
# ----------------------------------------------------------------------


def test_canonical_page_links_shared_stylesheet_with_cache_bust():
    out = _common.canonical_page("Sound", "<main></main>").decode()
    assert out.startswith("<!doctype html>")
    # Links the shared stylesheet, cache-busted by a version token.
    assert 'rel="stylesheet"' in out
    assert "/assets/app.css?v=" in out
    # Does NOT inline obsolete per-page wrapper CSS.
    assert "max-width: 620px" not in out


def test_canonical_page_includes_shared_icon_sprite():
    out = _common.canonical_page("Sound", "<main></main>").decode()
    assert 'id="icon-sound"' in out
    assert 'id="icon-chevron"' in out


def test_canonical_page_embeds_csrf_meta_only_when_token_given():
    with_token = _common.canonical_page(
        "S", "<main></main>", csrf_token="abc",
    ).decode()
    assert 'meta name="jts-csrf"' in with_token
    assert 'content="abc"' in with_token

    without = _common.canonical_page("S", "<main></main>").decode()
    assert "jts-csrf" not in without


def test_canonical_page_escapes_title():
    out = _common.canonical_page("<script>x</script>", "<main></main>").decode()
    assert "<title><script>" not in out
    assert "&lt;script&gt;" in out


def test_canonical_page_includes_page_specific_css():
    out = _common.canonical_page(
        "S", "<main></main>", page_css=".eq-graph{height:200px}",
    ).decode()
    assert "<style>.eq-graph{height:200px}</style>" in out


def test_canonical_page_omits_style_block_when_no_page_css():
    out = _common.canonical_page("S", "<main></main>").decode()
    assert "<style>" not in out


def test_canonical_page_links_page_stylesheet_with_cache_bust():
    # The preferred form: a real static .css file, cache-busted like app.css.
    out = _common.canonical_page(
        "S", "<main></main>", page_css_href="/assets/system-status/system.css",
    ).decode()
    assert 'rel="stylesheet" href="/assets/system-status/system.css?v=' in out
    assert "<style>" not in out  # linked, not inlined


# ----------------------------------------------------------------------
# Canonical header (the shared .app-header top bar)
# ----------------------------------------------------------------------


def test_canonical_header_renders_app_header_with_back_and_title():
    out = _common.canonical_header("Speaker name")
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Speaker name</h1>' in out
    # Back affordance: an icon-button linking home, drawn from the shared sprite.
    assert 'class="icon-button"' in out
    assert 'href="/"' in out
    assert 'aria-label="Home"' in out
    assert '<use href="#icon-back">' in out
    # Default right slot is an empty placeholder so the 3-col grid stays balanced.
    assert out.count("<span></span>") == 1


def test_canonical_header_escapes_title_and_back_attrs():
    out = _common.canonical_header(
        "<script>x</script>", back_href='"/evil', back_label='<b>L</b>',
    )
    assert "<script>" not in out
    assert "&lt;script&gt;x&lt;/script&gt;" in out
    # Attribute-injection via back_href / back_label must be neutralised.
    assert '"/evil' not in out
    assert "&quot;/evil" in out
    assert "&lt;b&gt;L&lt;/b&gt;" in out


def test_canonical_header_honours_custom_back_target():
    out = _common.canonical_header("Sound", back_href="/sound/", back_label="Back")
    assert 'href="/sound/"' in out
    assert 'aria-label="Back"' in out


def test_canonical_header_places_right_html_in_right_slot():
    out = _common.canonical_header(
        "T", right_html='<button class="btn">Edit</button>',
    )
    assert '<button class="btn">Edit</button>' in out
    # The supplied right_html replaces the empty placeholder.
    assert "<span></span>" not in out


def test_safe_back_href_accepts_local_paths_with_query():
    assert _common.safe_back_href("/tools/pack/spotify/?q=1") == (
        "/tools/pack/spotify/?q=1"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "tools/pack/spotify/",
        "//evil.test/path",
        "https://evil.test/path",
        "/\\evil.test/path",
        "/tools/\npack",
    ],
)
def test_safe_back_href_rejects_non_local_or_obfuscated_values(raw):
    assert _common.safe_back_href(raw, default="/fallback/") == "/fallback/"


# ----------------------------------------------------------------------
# Canonical banner (the shared .banner flash)
# ----------------------------------------------------------------------


def test_canonical_banner_blank_renders_nothing():
    assert _common.canonical_banner("") == ""
    assert _common.canonical_banner("   ") == ""


def test_canonical_banner_ok_for_saved_and_cleared():
    for msg in ("Saved. Speaker renamed.", "Cleared the cache."):
        out = _common.canonical_banner(msg)
        assert 'class="banner banner--ok"' in out
        assert 'role="status"' in out


def test_canonical_banner_danger_for_error_or_fail():
    for msg in ("Could not save: disk error", "That request failed"):
        assert 'class="banner banner--danger"' in _common.canonical_banner(msg)


def test_canonical_banner_info_for_neutral_message():
    out = _common.canonical_banner("Name unchanged.")
    assert 'class="banner banner--info"' in out


def test_canonical_banner_classing_matches_flash_contract():
    # A flash string lands in the expected severity bucket on canonical pages.
    cases = {
        "Saved.": "ok",
        "Cleared.": "ok",
        "Could not save: error": "danger",
        "Connection failed": "danger",
        "Name unchanged.": "info",
    }
    for msg, expected in cases.items():
        assert f"banner--{expected}" in _common.canonical_banner(msg)


def test_canonical_banner_escapes_message():
    out = _common.canonical_banner("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ----------------------------------------------------------------------
# Token shape validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    ("", False),               # empty
    ("short", False),          # too short
    ("a" * 31, False),         # 31 < 32
    ("a" * 32, True),          # min length
    ("a" * 128, True),         # max length
    ("a" * 129, False),        # over max
    ("abc.def", False),        # `.` not in alphabet
    ("abc def", False),        # whitespace
    ("a" * 32 + "-_", True),   # 34 chars, alphabet ok, length ok
    ("a" * 32 + "%!", False),  # 34 chars, alphabet invalid
    ("-_" * 16, True),         # all-alphabet
])
def test_is_valid_token_shape(value, expected):
    assert _common._is_valid_token(value) is expected


# --- atomic writers: unique temp (concurrent-writer safety) --------------


def test_write_env_file_uses_unique_temp_per_write(tmp_path, monkeypatch):
    # The ThreadingHTTPServer race: two writers of the same file must NOT
    # share one temp path (a fixed `<path>.tmp` lets them interleave + promote
    # a byte-mixed file). Capture what os.replace promotes; two writes -> two
    # distinct temps, neither the old fixed name.
    seen: list[str] = []
    real_replace = os.replace

    def capture(src, dst):
        seen.append(src)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", capture)
    p = str(tmp_path / "x.env")
    _common.write_env_file(p, {"A": "1"})
    _common.write_env_file(p, {"A": "2"})
    assert len(seen) == 2
    assert seen[0] != seen[1], "two writes shared a temp path -> race"
    assert all(s != p + ".tmp" for s in seen), "still using the fixed .tmp path"
    assert (tmp_path / "x.env").read_text() == "A=2\n"


def test_atomic_write_concurrent_writers_never_corrupt(tmp_path):
    # Stress: many threads writing the SAME file concurrently. The final file
    # must always be exactly ONE complete write — never byte-mixed — and no
    # temp files leak. This is the regression guard for the shared-.tmp race.
    p = str(tmp_path / "race.env")
    values = [f"value_{i}_" + "x" * 200 for i in range(8)]
    errors: list[Exception] = []

    def writer(v):
        try:
            for _ in range(50):
                _common.write_env_file(p, {"V": v})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(v,)) for v in values]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    content = (tmp_path / "race.env").read_text()
    assert content in {f"V={v}\n" for v in values}, f"corrupted/mixed write: {content!r}"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_atomic_write_cleans_up_temp_on_write_error(tmp_path):
    # A mid-write error (newline in a value) must leave no temp and no target.
    p = str(tmp_path / "bad.env")
    with pytest.raises(ValueError):
        _common.write_env_file(p, {"A": "has\nnewline"})
    assert not (tmp_path / "bad.env").exists()
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_restart_voice_daemon_skips_while_parked(monkeypatch):
    """A wizard save on a bonded follower must not boot 240 MB of parked
    models — config persists; the un-park path restarts voice on unbond."""
    import jasper.web._common as common

    monkeypatch.setattr(common, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(common, "bonded_follower_active", lambda: True)
    calls = []
    monkeypatch.setattr(common, "restart_systemd_units", lambda *u: calls.append(("restart", u)))
    common.restart_voice_daemon()
    assert calls == []


def test_restart_voice_daemon_runs_when_solo(monkeypatch):
    import jasper.web._common as common

    monkeypatch.setattr(common, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(common, "bonded_follower_active", lambda: False)
    calls = []
    monkeypatch.setattr(common, "restart_systemd_units", lambda *u: calls.append(("restart", u)))
    common.restart_voice_daemon()
    # WS1 Phase 3b-2: only the runtime restart — no `systemctl enable` (the root
    # jasper-aec-reconcile owns voice's boot-enable; the non-root jasper-control
    # is not granted polkit manage-unit-files).
    assert ("restart", ("jasper-voice",)) in calls


def test_pair_banner_html_renders_only_when_bonded(monkeypatch):
    import jasper.web._common as common

    monkeypatch.setattr(common, "bonded_follower_active", lambda: False)
    assert common.pair_banner_html() == ""
    monkeypatch.setattr(common, "bonded_follower_active", lambda: True)
    html = common.pair_banner_html()
    assert "stereo pair" in html and "/rooms/" in html


def test_local_web_host_prefers_mdns_and_rejects_raw_ips():
    import jasper.web._common as common

    assert common.local_web_host("jts3") == "jts3.local"
    assert common.local_web_host("jts3.local.") == "jts3.local"
    assert common.local_web_host("192.168.1.23") == ""
    assert common.local_web_host("bad/host") == ""


def test_pair_banner_renders_on_each_wizard_page(monkeypatch):
    """The §7.5 interface contract, pinned for the two f-string pages
    (voice, wake): banner present while a bonded follower, absent when
    solo, and never a literal brace leak. sound (string concatenation)
    and correction (header +=) use mechanisms that cannot brace-leak;
    their renders need heavy state objects, so they are covered by the
    live deploy check rather than forced through fixtures here."""
    import jasper.web._common as common

    def render_all():
        out = {}
        import jasper.web.voice_setup as vs
        out["voice"] = vs._index_html({}, "tok")
        import jasper.web.wake_setup as ws
        out["wake"] = ws._index_html({}, "tok")
        return out

    monkeypatch.setattr(common, "bonded_follower_active", lambda: True)
    for name, page in render_all().items():
        body = page.decode() if isinstance(page, bytes) else page
        assert "stereo pair" in body, name
        assert "{pair_banner_html" not in body, name

    monkeypatch.setattr(common, "bonded_follower_active", lambda: False)
    for name, page in render_all().items():
        body = page.decode() if isinstance(page, bytes) else page
        assert "stereo pair" not in body, name
