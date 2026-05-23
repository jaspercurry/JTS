"""Helpers for end-to-end web wizard tests.

The wizards now require a CSRF token on every POST (double-submit cookie
pattern: token in `jts_csrf` cookie + matching `csrf_token` form field).
These helpers handle the GET-then-POST handshake so each test can stay
focused on what it's actually verifying.
"""
from __future__ import annotations

import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request


CSRF_COOKIE_NAME = "jts_csrf"
CSRF_FORM_FIELD = "csrf_token"


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
            token = cookie.value
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
