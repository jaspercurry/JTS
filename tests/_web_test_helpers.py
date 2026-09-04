# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for web wizard tests.

Server-teardown timing note, because this is the module the affected tests
already import: `tests/conftest.py` rebinds the DEFAULT `poll_interval` of
`socketserver.BaseServer.serve_forever` from the stdlib's 0.5 s to 0.01 s.
Nothing at the `threading.Thread(target=server.serve_forever)` call sites
hints at that, so if you are ever puzzled by how fast (or, after a revert,
how slow) a `server.shutdown()` returns, that shim is the thread to pull.
Production is unaffected — `tests/` is not in the distribution.

The wizards now require a CSRF token on every mutating POST
(double-submit cookie pattern: token in `jts_csrf` cookie plus either a
matching `csrf_token` form field or `X-CSRF-Token` header). These
helpers handle the GET-then-POST handshake so each test can stay focused
on what it's actually verifying.

``FakeHandler`` provides the smaller in-process style used by wizard tests
that call a handler class's ``do_GET`` / ``do_POST`` methods directly.
"""
from __future__ import annotations

import hmac
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.message import Message
from io import BytesIO
from typing import Any


CSRF_COOKIE_NAME = "jts_csrf"
CSRF_FORM_FIELD = "csrf_token"


def patch_measurement_window(monkeypatch: Any, calls: dict) -> None:
    """Record ``coordinator.measurement_window`` into ``calls["window_events"]``
    as ``"open"``/``"close"``. ``calls["window_mode"]``: ``"fail"`` refuses
    entry, ``"fail_exit"`` fails the restore.
    """
    from jasper import measurement_window as coordinator

    @asynccontextmanager
    async def window(**kwargs: Any) -> AsyncIterator[None]:
        calls["window_events"].append("open")
        if calls.get("window_mode") == "fail":
            raise RuntimeError("window refused")
        try:
            yield
        finally:
            calls["window_events"].append("close")
            if calls.get("window_mode") == "fail_exit":
                raise RuntimeError("window restore failed")

    monkeypatch.setattr(coordinator, "measurement_window", window)


class FakeHandler:
    """Socketless ``BaseHTTPRequestHandler`` surface for wizard unit tests.

    ``body=None`` deliberately omits Content-Length and Content-Type.  That
    preserves the body-less request shape used by the rooms wizard; passing
    bytes (including ``b""``) models a form request and installs both headers.
    Response headers remain an ordered list so duplicate headers can be
    asserted without collapsing them through ``Message``.
    """

    def __init__(
        self,
        path: str,
        body: bytes | None = b"",
        cookies: str = "",
    ) -> None:
        self.path = path
        self.headers = Message()
        payload = b"" if body is None else body
        if body is not None:
            self.headers["Content-Length"] = str(len(body))
            self.headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookies:
            self.headers["Cookie"] = cookies
        self.rfile = BytesIO(payload)
        self.wfile = BytesIO()
        self.status: int | None = None
        self.sent_headers: list[tuple[str, str]] = []
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status: int) -> None:
        self.status = int(status)

    def send_response_only(self, status: int) -> None:
        self.status = int(status)

    def send_header(self, name: str, value: str) -> None:
        self.sent_headers.append((name, value))

    def end_headers(self) -> None:
        pass

    def send_error(self, status: int, *args: object, **kwargs: object) -> None:
        self.status = int(status)

    def address_string(self) -> str:
        return "127.0.0.1"

    def log_message(self, *args: object, **kwargs: object) -> None:
        pass

    def header_values(self, name: str) -> list[str]:
        return [
            value
            for header, value in self.sent_headers
            if header.lower() == name.lower()
        ]


def make_real_handler(
    handler_cls,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    content_type: str | None = "application/x-www-form-urlencoded",
    content_length: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Instantiate a real wizard handler without its socket constructor."""

    handler = handler_cls.__new__(handler_cls)
    handler.path = path
    handler.headers = Message()
    handler.headers["Content-Length"] = (
        str(len(body)) if content_length is None else content_length
    )
    if content_type is not None:
        handler.headers["Content-Type"] = content_type
    for name, value in (headers or {}).items():
        handler.headers[name] = value
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.client_address = ("127.0.0.1", 0)

    captured: dict[str, Any] = {"status": None, "responses": [], "headers": []}

    def capture_response(status: int, *args: object, **kwargs: object) -> None:
        captured["status"] = int(status)
        captured["responses"].append(int(status))
        handler.status = int(status)

    def capture_error(status: int, *args: object, **kwargs: object) -> None:
        captured["status"] = int(status)
        handler.status = int(status)

    handler.status = None
    handler.sent_headers = captured["headers"]
    handler.send_response = capture_response
    handler.send_response_only = capture_response
    handler.send_header = lambda name, value: captured["headers"].append((name, value))
    handler.end_headers = lambda: None
    handler.send_error = capture_error
    handler.address_string = lambda: "127.0.0.1"
    handler.log_message = lambda *args, **kwargs: None
    handler.header_values = lambda name: [
        value
        for header, value in handler.sent_headers
        if header.lower() == name.lower()
    ]
    return handler, captured


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Block redirect following so callers can assert on the 303 status.

    Used as a build_opener handler to surface the redirect response as an
    HTTPError(303) instead of transparently chasing it (which would hit
    GET / and we'd lose the test signal)."""

    def http_error_303(self, req, fp, code, msg, headers):  # noqa: ARG002
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def make_csrf_session(base_url: str, page_path: str = "/") -> dict:
    """Hit `page_path` on `base_url` to mint a CSRF cookie, return the
    pieces needed for a subsequent POST.

    Returns a dict with:
      jar:   the CookieJar that received the Set-Cookie (pass back into
             post_with_csrf so the cookie travels on the POST)
      token: the CSRF token value (urldecoded if needed) to include in
             the form's csrf_token field
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
    )
    opener.open(base_url + page_path).read()
    token = ""
    for cookie in jar:
        if cookie.name == CSRF_COOKIE_NAME:
            token = cookie.value or ""
            break
    if not token:
        raise AssertionError(
            f"Wizard at {base_url}{page_path} did not set a {CSRF_COOKIE_NAME} "
            f"cookie — begin_request() / send_html_response() not wired in?"
        )
    return {"jar": jar, "token": token}


def post_with_csrf(
    base_url: str,
    path: str,
    form: dict,
    *,
    session: dict | None = None,
    expect_status: int = 303,
):
    """POST a form to `path` with the CSRF cookie + token already in
    place. If `session` is omitted we mint one via a GET to `path` first.

    Asserts the response status matches `expect_status` (303 by default
    — the wizards reply 303 SEE_OTHER on successful save). Returns the
    cookie jar so the caller can chain follow-up requests."""
    if session is None:
        # Default: GET the same path's "container" page to mint the
        # token. Strip trailing /save (or similar) → land on /.
        page = path.rsplit("/", 1)[0] + "/"
        session = make_csrf_session(base_url, page_path=page)
    payload = dict(form)
    payload[CSRF_FORM_FIELD] = session["token"]
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        base_url + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPCookieProcessor(session["jar"]),
    )
    try:
        resp = opener.open(req)
        assert resp.status == expect_status, (
            f"POST {path} got {resp.status}, wanted {expect_status}"
        )
        return session["jar"]
    except urllib.error.HTTPError as e:
        assert e.code == expect_status, (
            f"POST {path} got HTTP {e.code}, wanted {expect_status}: "
            f"{e.read()[:200]!r}"
        )
        return session["jar"]


def request_with_csrf(
    base_url: str,
    path: str,
    data: bytes,
    *,
    content_type: str,
    session: dict | None = None,
    expect_status: int = 200,
):
    """POST arbitrary bytes with the CSRF cookie + X-CSRF-Token header.

    Useful for JSON endpoints and non-form uploads such as audio/wav.
    Returns the urllib response object for 2xx statuses, or the HTTPError
    object when `expect_status` is an error code."""
    if session is None:
        session = make_csrf_session(base_url, page_path="/")
    req = urllib.request.Request(
        base_url + path,
        data=data,
        method="POST",
        headers={
            "Content-Type": content_type,
            "X-CSRF-Token": session["token"],
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(session["jar"]),
    )
    try:
        resp = opener.open(req)
        assert resp.status == expect_status, (
            f"POST {path} got {resp.status}, wanted {expect_status}"
        )
        return resp
    except urllib.error.HTTPError as e:
        assert e.code == expect_status, (
            f"POST {path} got HTTP {e.code}, wanted {expect_status}: "
            f"{e.read()[:200]!r}"
        )
        return e


def json_post_with_csrf(
    base_url: str,
    path: str,
    payload: dict,
    *,
    session: dict | None = None,
    expect_status: int = 200,
):
    """POST a JSON body with the CSRF cookie + X-CSRF-Token header."""
    return request_with_csrf(
        base_url,
        path,
        json.dumps(payload).encode(),
        content_type="application/json",
        session=session,
        expect_status=expect_status,
    )


def assert_verify_uses_constant_time_compare(monkeypatch, tmp_path, module, file_attr, secret):
    """``module.verify`` must compare through ``hmac.compare_digest``, never ``==``."""
    path = tmp_path / "secret"
    path.write_text(secret)
    monkeypatch.setattr(module, file_attr, str(path))
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest
    monkeypatch.setattr(
        hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b)
    )
    assert module.verify("wrong") is False
    assert calls == [("wrong", secret)]
