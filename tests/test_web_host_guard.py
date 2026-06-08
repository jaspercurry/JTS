"""Host/Origin guard on the shared wizard mutating chokepoint.

The nginx-fronted setup wizards under `jasper/web/` write WiFi PSKs, HA
tokens, and API keys and can trigger reboots. They all funnel mutating
(POST) requests through `guard_mutating_request()` in
`jasper/web/_common.py`. This module asserts that `guard_mutating_request`
applies the same DNS-rebinding / cross-site Host/Origin allowlist that the
control daemon (`jasper/control/server.py:_guard_mutating_request`)
already enforces — so a hostile Host/Origin is rejected BEFORE any state
change, while a legitimate LAN client (configured hostname, `.local`, raw
RFC1918 IP) keeps working. Lockout safety: it must accept exactly the
hosts the control daemon accepts. Style mirrors
tests/test_http_security.py.
"""
from __future__ import annotations

from email.message import Message
from io import BytesIO

from jasper.web import _common


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in carrying request headers.

    Mirrors tests/test_web_common.py's fake, with a `path` so the
    guard's structured-log line has something to render."""

    def __init__(self, *, cookies: str = "", path: str = "/save", **headers: str):
        self.headers = Message()
        if cookies:
            self.headers["Cookie"] = cookies
        for key, value in headers.items():
            self.headers[key.replace("_", "-")] = value
        self.path = path
        self.wfile = BytesIO()


_GOOD_TOKEN = "g" * 64


def _csrf_handler(**headers: str) -> _FakeHandler:
    """A handler that already passes the CSRF double-submit check, so any
    guard_mutating_request failure is attributable to the Host/Origin guard."""
    return _FakeHandler(cookies=f"jts_csrf={_GOOD_TOKEN}", **headers)


# --- guard_mutating_host (the helper, in isolation) ----------------------


def test_guard_allows_configured_hostname(monkeypatch):
    monkeypatch.setenv("JASPER_HOSTNAME", "jts.local")
    assert _common.guard_mutating_host(_FakeHandler(Host="jts.local")) is True


def test_guard_allows_dot_local(monkeypatch):
    # The configured hostname's bare + `.local` forms are both allowed,
    # matching is_allowed_management_host (and the control daemon).
    monkeypatch.setenv("JASPER_HOSTNAME", "speaker.local")
    assert _common.guard_mutating_host(_FakeHandler(Host="speaker.local")) is True
    assert _common.guard_mutating_host(_FakeHandler(Host="speaker")) is True


def test_guard_allows_raw_rfc1918_ip():
    assert _common.guard_mutating_host(_FakeHandler(Host="192.168.1.42:8773")) is True
    assert _common.guard_mutating_host(_FakeHandler(Host="10.0.0.5")) is True
    assert _common.guard_mutating_host(_FakeHandler(Host="172.16.3.9")) is True


def test_guard_allows_loopback_and_missing_host():
    assert _common.guard_mutating_host(_FakeHandler(Host="127.0.0.1")) is True
    # Non-browser clients (curl, dial) may omit Host entirely.
    assert _common.guard_mutating_host(_FakeHandler()) is True


def test_guard_rejects_public_host():
    assert _common.guard_mutating_host(_FakeHandler(Host="evil.example")) is False


def test_guard_rejects_cross_site_origin():
    h = _FakeHandler(Host="jts.local", Origin="http://evil.example")
    assert _common.guard_mutating_host(h) is False


def test_guard_rejects_cross_site_fetch_metadata():
    h = _FakeHandler(Host="jts.local")
    h.headers["Sec-Fetch-Site"] = "cross-site"
    assert _common.guard_mutating_host(h) is False


# --- guard_mutating_request composes the guard (the chokepoint every
#     wizard uses) ---


def test_guard_mutating_request_rejects_disallowed_host_even_with_valid_token():
    # Token is perfectly valid; the request must still be refused because
    # the Host is a DNS-rebinding shape. This is the security regression.
    h = _csrf_handler(Host="attacker.example")
    h.headers["X-CSRF-Token"] = _GOOD_TOKEN
    assert _common.guard_mutating_request(h) is False


def test_guard_mutating_request_rejects_cross_origin_even_with_valid_token():
    h = _csrf_handler(Host="jts.local", Origin="http://attacker.example")
    h.headers["X-CSRF-Token"] = _GOOD_TOKEN
    assert _common.guard_mutating_request(h) is False


def test_guard_mutating_request_accepts_legit_lan_hostname(monkeypatch):
    monkeypatch.setenv("JASPER_HOSTNAME", "jts.local")
    h = _csrf_handler(Host="jts.local", Origin="http://jts.local")
    h.headers["X-CSRF-Token"] = _GOOD_TOKEN
    assert _common.guard_mutating_request(h) is True


def test_guard_mutating_request_accepts_legit_dot_local(monkeypatch):
    monkeypatch.setenv("JASPER_HOSTNAME", "speaker.local")
    h = _csrf_handler(Host="speaker.local")
    h.headers["X-CSRF-Token"] = _GOOD_TOKEN
    assert _common.guard_mutating_request(h) is True


def test_guard_mutating_request_accepts_legit_raw_rfc1918_ip():
    # A household reaching the wizard by raw LAN IP (e.g. mDNS down) must
    # not be locked out of their own setup pages.
    h = _csrf_handler(Host="192.168.1.42", Origin="http://192.168.1.42")
    h.headers["X-CSRF-Token"] = _GOOD_TOKEN
    assert _common.guard_mutating_request(h) is True


def test_guard_mutating_request_still_rejects_bad_token_on_allowed_host():
    # Host guard passes, but the CSRF check must still fire.
    h = _csrf_handler(Host="jts.local")
    h.headers["X-CSRF-Token"] = "b" * 64
    assert _common.guard_mutating_request(h) is False
