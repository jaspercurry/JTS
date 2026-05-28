"""Tests for jasper.web._common helpers.

The infrastructure tested here (flash cookies, CSRF tokens, the unified
HTML response helper, the 303 redirect helper) is exercised through fake
handlers rather than real http.server instances — keeps the suite fast
and lets us assert on exactly the header bytes that go on the wire.
"""
from __future__ import annotations

import http
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


def test_verify_csrf_returns_true_for_matching_pair():
    token = "a" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token}")
    assert _common.verify_csrf(h, {_common.CSRF_FORM_FIELD: token}) is True


def test_verify_csrf_returns_false_for_mismatch():
    token_a = "a" * 64
    token_b = "b" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token_a}")
    assert _common.verify_csrf(h, {_common.CSRF_FORM_FIELD: token_b}) is False


def test_verify_csrf_returns_false_when_cookie_missing():
    h = _FakeHandler()
    assert _common.verify_csrf(h, {_common.CSRF_FORM_FIELD: "a" * 64}) is False


def test_verify_csrf_returns_false_when_form_field_missing():
    token = "a" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token}")
    assert _common.verify_csrf(h, {}) is False


def test_verify_csrf_returns_false_when_both_invalid():
    # Two short strings happen to compare_digest-equal but they shouldn't
    # pass our shape gate.
    h = _FakeHandler(cookies="jts_csrf=abc")
    assert _common.verify_csrf(h, {_common.CSRF_FORM_FIELD: "abc"}) is False


def test_verify_csrf_accepts_token_via_x_csrf_token_header():
    """For JS-driven POSTs (fetch with no body / JSON body) where a
    hidden form field is awkward, the token can ride on the
    X-CSRF-Token header. Same constant-time compare against the
    cookie."""
    token = "z" * 64
    h = _FakeHandler(cookies=f"jts_csrf={token}")
    h.headers["X-CSRF-Token"] = token
    assert _common.verify_csrf(h) is True
    assert _common.verify_csrf(h, {}) is True


def test_verify_csrf_rejects_mismatched_header_token():
    cookie_token = "a" * 64
    bad_header_token = "b" * 64
    h = _FakeHandler(cookies=f"jts_csrf={cookie_token}")
    h.headers["X-CSRF-Token"] = bad_header_token
    assert _common.verify_csrf(h) is False


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


def test_shared_toggle_css_respects_reduced_motion():
    assert "@media (prefers-reduced-motion: reduce)" in _common.TOGGLE_CSS
    assert ".toggle .track" in _common.TOGGLE_CSS
    assert "transition: none" in _common.TOGGLE_CSS


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


def test_restart_systemd_units_restarts_multiple_units_no_block(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

    monkeypatch.setattr(_common.subprocess, "run", fake_run)

    _common.restart_systemd_units(
        "jasper-voice", "jasper-control", "jasper-mux",
    )

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [
        "systemctl",
        "restart",
        "--no-block",
        "jasper-voice",
        "jasper-control",
        "jasper-mux",
    ]
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 5


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
