"""Shared helpers for the JTS web setup pages.

Every wizard under `jasper/web/` (Spotify, voice, transit, wake, …)
shares the look, the systemd-style env-file atomics, the
`systemctl restart jasper-voice` shell-out, and the request-response
plumbing for navigation hygiene (flash cookies, CSRF tokens, no-store
caching). What's NOT shared: per-wizard route handlers, page layouts,
form bodies.

## Conventions for new wizards

Every wizard's request handler should look like this:

    def do_GET(self):
        if path == "/":
            ctx = begin_request(self)
            send_html_response(self, render_page(
                ctx["csrf_token"], status_msg=ctx["flash"],
            ))

    def do_POST(self):
        # Route-check before CSRF-check: unknown paths return 404
        # without revealing the CSRF state.
        if path not in ("/save", "/clear", …):
            self.send_error(HTTPStatus.NOT_FOUND); return
        form = read_form(self)
        if not verify_csrf(self, form):
            reject_csrf(self); return
        # ... handle ...
        send_see_other(self, "./", flash="Saved.")

Every `<form method="post">` includes `{csrf_field_html(csrf_token)}`
inside it. Every page that uses fetch() for state changes includes
`{csrf_meta_html(csrf_token)}` in the <body> and the JS sends the
token as `X-CSRF-Token` on POST.

DO NOT:
* redirect to `./?msg=Saved…` — that pollutes browser history. Use
  `send_see_other(self, "./", flash="Saved.")` instead.
* roll your own `_redirect` or `_send_html` — call `send_see_other`
  / `send_html_response` directly. They emit `Cache-Control: no-store`,
  the CSRF cookie, and the flash-clear cookie consistently.
* skip the CSRF check on a form-bodied POST because "it's LAN-only".
  A cross-origin attacker page can still trigger a same-site POST via
  `<form action="http://jts.local/...">`. SameSite=Strict on the CSRF
  cookie + the double-submit check is what stops it.

JSON-bodied POSTs (Content-Type: application/json) are CORS-preflighted
by browsers, which already blocks cross-origin attackers — wifi /
bluetooth / dial / correction skip CSRF on those endpoints because of
this. If a wizard adds a form-bodied POST, it MUST add the CSRF check.

See `tests/test_web_common.py` for the helpers' behavior contracts.
"""
from __future__ import annotations

import html
import http
import logging
import os
import secrets
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cookie + header constants.
# ---------------------------------------------------------------------------

# Flash messages (PRG status text) live in a short-lived cookie instead of a
# `?msg=…` query param on the redirect target. The query-param pattern
# poisoned browser history: the post-save URL `/voice/?msg=Saved` was a
# distinct entry from `/voice/`, so clicking Back went to "the same page
# without the message" rather than the previous page in the wizard flow.
# Cookies disappear on the next render; URLs stay clean and shareable.
FLASH_COOKIE_NAME = "jts_flash"

# Double-submit CSRF token. Lives in a cookie set on any wizard GET and
# echoed back as a hidden form field on every POST. Server compares the
# two with `secrets.compare_digest`. Defends against a cross-origin
# attacker getting the user's browser to POST to a write endpoint —
# SameSite=Strict prevents the cookie from accompanying the cross-origin
# request, so the token check fails and we 403.
CSRF_COOKIE_NAME = "jts_csrf"
CSRF_FORM_FIELD = "csrf_token"
_CSRF_TOKEN_BYTES = 32  # 32 bytes → 43 base64-url-safe chars

# Attribute name we stash request context on (flash text, csrf token,
# csrf-cookie-needs-setting flag). Stashed on the handler instance so
# the per-request begin_request → send_html_response flow can share state
# without re-parsing cookies twice.
_CTX_ATTR = "_jts_request_ctx"


# Page CSS. Same look as the Spotify wizard so the speaker presents one
# coherent settings UI instead of a stack of mismatched tools. Spotify-
# green primary button (#1db954) is intentional even on non-Spotify
# pages — the user knows that "the JTS settings green" means "save /
# proceed" by the time they see the second wizard.
PAGE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         max-width: 620px; margin: 2em auto; padding: 0 1em; color: #222; }
  h1 { margin-bottom: 0.25em; } h2 { margin-top: 2em; }
  .sub { color: #666; margin-top: 0; }
  .msg { background: #e8f4ff; border: 1px solid #abd; padding: 0.6em 0.8em;
          border-radius: 6px; margin: 1em 0; }
  .msg.ok { background: #e6f9ec; border-color: #1db954; color: #14542a; }
  .msg.ok::before { content: "✓ "; font-weight: 700; }
  .err { background: #ffe8e8; border-color: #d99; }
  ol.steps { padding-left: 1.4em; }
  ol.steps > li { margin-bottom: 1em; }
  form { margin-top: 1em; }
  label { display: block; margin: 0.6em 0 0.2em; font-weight: 600; }
  input[type=text], input[type=password], select {
    width: 100%; padding: 0.5em; border: 1px solid #bbb;
    border-radius: 4px; font-size: 1em; box-sizing: border-box;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: #fff;
  }
  small { color: #666; }
  .hint { color: #666; font-size: 0.92em; }
  button, a.btn {
    background: #1db954; color: white; border: 0;
    padding: 0.6em 1.2em; border-radius: 4px; font-size: 1em;
    cursor: pointer; text-decoration: none; display: inline-block;
  }
  a.btn.secondary, button.secondary { background: #4a4a4a; }
  button.danger { background: #d44; }
  button:hover, a.btn:hover { filter: brightness(1.1); }
  button:disabled { background: #b8b8b8; cursor: not-allowed; filter: none; }
  .copy-row { display: flex; gap: 0.5em; align-items: stretch; margin: 0.6em 0; }
  .copy-row input { flex: 1; }
  .copy-row button { padding: 0 1em; }
  .copy-feedback { color: #1db954; font-weight: 600; margin-left: 0.4em;
                   visibility: hidden; transition: opacity 0.2s; }
  .copy-feedback.shown { visibility: visible; }
  .credbox {
    background: #fafafa; border: 1px solid #ddd; padding: 0.4em 0.8em;
    border-radius: 6px; margin: 0.6em 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.92em; color: #555; word-break: break-all;
  }

  /* Account = expand/collapse card. Used by both /spotify (account
     management) and /voice (per-provider config). Common shape, same
     CSS, lives here once. */
  .accounts-help { color: #666; font-size: 0.92em; margin: 0 0 0.8em; }
  details.account { background: #f4f4f4; border-radius: 6px;
                     margin-bottom: 0.5em; overflow: hidden; }
  details.account > summary {
    list-style: none; cursor: pointer; padding: 0.7em 0.9em;
    display: flex; align-items: center; gap: 0.6em;
    user-select: none; -webkit-user-select: none;
  }
  details.account > summary::-webkit-details-marker { display: none; }
  details.account > summary::before {
    content: "▸"; color: #888; font-size: 0.9em;
    transition: transform 0.15s ease; display: inline-block; width: 0.9em;
  }
  details.account[open] > summary::before { transform: rotate(90deg); }
  details.account > summary:hover { background: #ececec; }
  details.account > summary .name { font-weight: 600; flex: 1; }
  details.account > summary .badge {
    background: #4a8; color: white; padding: 0.1em 0.5em;
    border-radius: 4px; font-size: 0.8em;
  }
  details.account > summary .badge.muted {
    background: #aaa;
  }
  details.account > summary .pl-count {
    color: #888; font-size: 0.88em; font-variant-numeric: tabular-nums;
  }
  details.account .account-body {
    padding: 0 0.9em 0.9em; border-top: 1px solid #e6e6e6;
  }

  /* Shared back-to-home nav link (NAV_BACK_HTML below). Sits above
     the page <h1> so every wizard has the same one-click escape
     back to http://jts.local/. Pages that define their own style
     block (dial, bluetooth, correction) re-import NAV_BACK_CSS so
     this rule travels with the link wherever it goes.*/
  .nav-back {
    display: inline-block; color: #666; text-decoration: none;
    font-size: 0.92em; margin-bottom: 0.6em;
  }
  .nav-back:hover { color: #222; }

  /* ---- Top-level disclosures (Connection details, Setup guide,
     OAuth client settings, etc.) shared across wizards.
     Browser-default <summary> is plain text + a tiny native
     triangle — easy to miss as clickable. These rules give the
     summary a card-style hover affordance and a right-aligned
     caret that rotates on open. Targets `.disclosure` only so
     other <details> patterns (account cards, contextual hints,
     log expanders) keep their own styling. */
  details.disclosure { margin-top: 1.4em; }
  details.disclosure > summary {
    list-style: none;
    cursor: pointer;
    user-select: none; -webkit-user-select: none;
    padding: 0.85em 2.4em 0.85em 1em;
    background: #f4f4f4;
    border: 1px solid #e6e6e6;
    border-radius: 8px;
    font-weight: 600;
    color: #222;
    position: relative;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  details.disclosure > summary:hover {
    background: #f0fff4;
    border-color: #1db954;
  }
  details.disclosure[open] > summary {
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    border-bottom-color: transparent;
  }
  details.disclosure > summary::-webkit-details-marker { display: none; }
  details.disclosure > summary::after {
    content: "▸";
    position: absolute;
    right: 1em; top: 50%;
    transform: translateY(-50%);
    color: #888;
    transition: transform 0.15s ease, color 0.15s ease;
  }
  details.disclosure > summary:hover::after,
  details.disclosure[open] > summary::after {
    color: #1db954;
  }
  details.disclosure[open] > summary::after {
    transform: translateY(-50%) rotate(90deg);
  }
  details.disclosure > .disclosure-body {
    padding: 0.6em 1em 1em;
    border: 1px solid #e6e6e6;
    border-top: none;
    border-bottom-left-radius: 8px;
    border-bottom-right-radius: 8px;
    background: #fff;
  }
"""


# Single source of truth for the home-link. Imported by every setup
# page so the markup stays identical even though each page renders
# its own HTML wrapper.
#
# Behaviour: unconditional <a href="/"> to the dashboard. This link
# is semantically an *Up* affordance (return to the speaker's home),
# not a Back affordance (reverse-chronological browser history).
# Android codifies this distinction explicitly; modern web design
# (GitHub, GitLab, Reddit, Material) follows it: chrome links reflect
# information hierarchy, the browser's Back button reflects history.
#
# The prior version did `history.back()` when `history.length > 1`,
# which broke in two ways:
#   1. After a save → ?msg=… redirect, history was
#      [/, /voice/, /voice/?msg=Saved…]; clicking Back went to
#      /voice/ without the message → the user perceived "back did
#      nothing." (Bigger fix: flash cookies replace ?msg=, so the
#      ghost entry no longer exists.)
#   2. `history.length` counts the whole tab's session history,
#      including cross-origin entries. Deep-link entries from
#      a phone-launcher / email / Slack had length > 1, so the JS
#      fired history.back() and exited JTS entirely.
# Pages with a different natural parent (e.g. Spotify / Google live
# under /integrations) can override the label per-wizard.
NAV_BACK_HTML = '<a class="nav-back" href="/">← Home</a>'

# CSS for `.nav-back` is included in `PAGE_STYLE` above. Re-exported
# here as a string fragment for pages that build their own style
# block instead of using `PAGE_STYLE` (dial / bluetooth / correction).
NAV_BACK_CSS = """
  .nav-back {
    display: inline-block; color: #666; text-decoration: none;
    font-size: 0.92em; margin-bottom: 0.6em;
  }
  .nav-back:hover { color: #222; }
"""


def wrap_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    """Wrap a body fragment into a complete HTML5 document with the
    shared style and an optional status banner.

    `status_msg` is rendered with an `err` class when it contains
    "error" or "fail" (case-insensitive), and with an `ok` class
    (green with a leading ✓) when it starts with "Saved" or "Cleared"
    — the two success vocabularies the wizards write back from save
    handlers. Anything else gets neutral info-blue styling."""
    lowered = status_msg.lower()
    if "error" in lowered or "fail" in lowered:
        msg_class = "msg err"
    elif lowered.startswith(("saved", "cleared")):
        msg_class = "msg ok"
    else:
        msg_class = "msg"
    msg_html = (
        f'<p class="{msg_class}">{html.escape(status_msg)}</p>'
        if status_msg else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
{NAV_BACK_HTML}
<h1>{html.escape(title)}</h1>
{msg_html}
{body}
</body>
</html>""".encode()


def read_env_file(path: str) -> dict[str, str]:
    """Parse a systemd-style EnvironmentFile (KEY=VALUE per line, no
    quoting). Returns {} if the file is missing or unreadable.

    Same shape used by `/var/lib/jasper/spotify_credentials.env` and
    `/var/lib/jasper/voice_provider.env` — both are sourced into
    jasper-voice's environment via systemd's `EnvironmentFile=`."""
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not read %s: %s", path, e)
    return out


def write_env_file(path: str, values: dict[str, str], *, mode: int = 0o600) -> None:
    """Atomically write a systemd EnvironmentFile-shaped key=value file
    with the given mode (default 0o600 — these files contain API keys
    and OAuth secrets).

    Atomicity matters: a half-written env file at restart time could
    leave jasper-voice with a partial config and a real-world impact
    (silent failure cue, lost session). The temp-file + rename pattern
    here gives the kernel an all-or-nothing swap."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            for key, val in values.items():
                # We write KEY=VALUE without quoting, matching systemd's
                # EnvironmentFile parsing: leading/trailing whitespace
                # in the value is stripped, but no escaping is applied
                # to embedded characters. API keys are alphanumeric
                # with `-`/`_`/`.` so this is safe; an explicit guard
                # keeps that contract honest if someone passes a value
                # with newlines or `=`.
                if "\n" in val or "\r" in val:
                    raise ValueError(f"env value for {key} contains newline")
                f.write(f"{key}={val}\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def delete_env_file(path: str) -> None:
    """Best-effort delete; missing-file is fine."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not delete %s: %s", path, e)


def restart_voice_daemon() -> None:
    """Best-effort restart of jasper-voice so it picks up new
    credentials / new provider / wake model on its next boot.

    `--no-block` is important. `Type=notify` units make `systemctl
    restart` block until the daemon emits READY=1, which for
    jasper-voice means model load + cue regen + reconnect to the
    LLM provider — often 8–12 s on a Pi. Without --no-block the
    web wizard's save handler hangs that long before returning the
    303 redirect, the browser shows a spinner, the user thinks
    nothing happened and click Save again (then again) — observed
    on PR #117 when switching wake models via the /wake/ UI.

    With --no-block, systemctl queues the restart and returns in
    a few ms. The browser gets the success banner immediately.
    The actual restart still happens; if it fails, the user finds
    out when wake doesn't fire (or via /system/) rather than via a
    web error — same failure mode we already had, since the
    previous `check=False, timeout=10` was swallowing errors too.

    The fallback timeout of 5 s is for systemctl's own argument-
    parsing / dbus-roundtrip overhead, NOT the restart itself —
    --no-block means systemctl shouldn't sit there waiting on the
    unit. If we hit 5 s here, something is wedged (dbus dead, etc.)
    and the bigger problem will surface elsewhere."""
    try:
        subprocess.run(
            ["systemctl", "restart", "--no-block", "jasper-voice"],
            check=False, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("jasper-voice restart failed: %s", e)


def read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    """Parse a urlencoded form body off a stdlib BaseHTTPRequestHandler
    request into a single-value dict. Empty values are preserved (so
    we can detect "user pasted nothing" vs "field absent")."""
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length).decode("utf-8") if length else ""
    return {
        k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()
    }


# ---------------------------------------------------------------------------
# Cookie parsing.
# ---------------------------------------------------------------------------

# Hand-rolled cookie parsing rather than http.cookies.SimpleCookie — that
# class trips on dashes inside cookie values and is overkill for two named
# cookies. parsed lazily per-request.
def _read_request_cookies(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    raw = handler.headers.get("Cookie") or ""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


# ---------------------------------------------------------------------------
# Flash cookie (PRG status messages).
# ---------------------------------------------------------------------------


def _format_set_cookie(
    name: str, value: str, *, max_age: int, http_only: bool = True,
) -> str:
    """Render a Set-Cookie header value. SameSite=Strict everywhere; Lax
    isn't quite enough — a cross-origin POST to /save would still send
    Lax cookies on top-level navigations, which a malicious page can
    arrange via `<form target="_top">`."""
    parts = [
        f"{name}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "SameSite=Strict",
    ]
    if http_only:
        parts.append("HttpOnly")
    return "; ".join(parts)


def read_flash(handler: BaseHTTPRequestHandler) -> str:
    """Read the flash cookie's text (urldecoded) off the request. Empty
    string if not set. Caller is responsible for clearing the cookie on
    the response — `send_html_response()` does this automatically when
    `flash` is non-empty in the request context."""
    cookies = _read_request_cookies(handler)
    raw = cookies.get(FLASH_COOKIE_NAME, "")
    if not raw:
        return ""
    try:
        return urllib.parse.unquote(raw)
    except (UnicodeDecodeError, ValueError):
        return ""


def _flash_set_cookie_header(message: str) -> str:
    """A Set-Cookie value that establishes the flash. 15 s Max-Age covers
    any reasonable POST → 303 → GET round-trip, including a slow LTE
    phone, without lingering long enough to appear on a later visit."""
    encoded = urllib.parse.quote(message, safe="")
    return _format_set_cookie(FLASH_COOKIE_NAME, encoded, max_age=15)


def _flash_clear_cookie_header() -> str:
    """A Set-Cookie value that clears the flash on the same response that
    renders it. Belt-and-suspenders with the 15 s expiry — guarantees the
    message doesn't linger across an unrelated subsequent GET, even if
    the browser clock is off."""
    return _format_set_cookie(FLASH_COOKIE_NAME, "", max_age=0)


# ---------------------------------------------------------------------------
# CSRF (double-submit cookie pattern).
# ---------------------------------------------------------------------------


def _is_valid_token(value: str) -> bool:
    # base64-url-safe alphabet only; correct length window. Strict to
    # reject anything weird in the cookie (avoids time-leakage on
    # compare_digest with weird inputs).
    if not 32 <= len(value) <= 128:
        return False
    return all(
        c.isalnum() or c in "-_" for c in value
    )


def _csrf_set_cookie_header(token: str) -> str:
    """30-day Max-Age. Long-lived because the user might leave a wizard
    tab open for hours between fetch and save; we don't want the CSRF
    check to start failing because the cookie expired mid-session."""
    return _format_set_cookie(
        CSRF_COOKIE_NAME, token,
        max_age=30 * 24 * 3600, http_only=False,
    )


def _read_or_mint_csrf(
    handler: BaseHTTPRequestHandler,
) -> tuple[str, bool]:
    """Return (token, minted_new). `minted_new=True` means the caller must
    arrange to send the Set-Cookie header on the response."""
    cookies = _read_request_cookies(handler)
    existing = cookies.get(CSRF_COOKIE_NAME, "")
    if _is_valid_token(existing):
        return existing, False
    return secrets.token_urlsafe(_CSRF_TOKEN_BYTES), True


def verify_csrf(
    handler: BaseHTTPRequestHandler, form: dict[str, str] | None = None,
) -> bool:
    """Return True iff the request carries a CSRF token that matches the
    cookie. `secrets.compare_digest` for constant-time comparison.

    Accepts the token via either:
      * `form[CSRF_FORM_FIELD]` — the form-rendered case
      * `X-CSRF-Token` request header — for JS-driven POSTs (fetch() with
        empty body, JSON bodies, etc.) where embedding a hidden input is
        awkward. JS reads the token from a `<meta name="jts-csrf">` tag
        the page renders and sends it as a header.

    Use at the top of every state-changing POST handler. Pair with
    `csrf_field_html()` on form-render sites and `csrf_meta_html()` on
    pages whose JS calls fetch."""
    cookies = _read_request_cookies(handler)
    cookie_token = cookies.get(CSRF_COOKIE_NAME, "")
    candidates: list[str] = []
    if form is not None:
        v = (form.get(CSRF_FORM_FIELD) or "").strip()
        if v:
            candidates.append(v)
    header_token = (handler.headers.get("X-CSRF-Token") or "").strip()
    if header_token:
        candidates.append(header_token)
    if not _is_valid_token(cookie_token):
        return False
    for token in candidates:
        if _is_valid_token(token) and secrets.compare_digest(cookie_token, token):
            return True
    return False


def csrf_field_html(token: str) -> str:
    """Hidden <input> markup to include inside every <form method=post>.
    The token comes from `begin_request()` / the request context."""
    return (
        f'<input type="hidden" name="{CSRF_FORM_FIELD}" '
        f'value="{html.escape(token)}">'
    )


def csrf_meta_html(token: str) -> str:
    """<meta> tag for pages whose JS calls fetch(). The script reads
    `document.querySelector('meta[name=jts-csrf]').content` and sends
    it as the `X-CSRF-Token` header on every state-changing POST."""
    return f'<meta name="jts-csrf" content="{html.escape(token)}">'


def reject_csrf(handler: BaseHTTPRequestHandler) -> None:
    """Send a 403 with a tiny HTML body explaining the failure. The
    wizards' POST handlers should call this and return on csrf-verify
    failure. We don't redirect because that would mask a real attack as
    "the page just glitched, try again." 403 is honest."""
    body = (
        b"<!doctype html><meta charset=utf-8>"
        b"<title>Session expired</title>"
        b"<h1>Session expired</h1>"
        b"<p>This form was submitted with a stale or missing session "
        b"token. Reload the page and try again.</p>"
        b'<p><a href=".">Reload</a></p>'
    )
    handler.send_response(http.HTTPStatus.FORBIDDEN)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------------------------
# Per-request context + unified response helpers.
# ---------------------------------------------------------------------------


def begin_request(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """Read flash + CSRF cookies once per request; stash on the handler.

    Call at the top of every GET handler that renders a form (or just
    every GET, no harm in extras). The returned dict has:
      `flash`        — text to render as the page status banner (or "")
      `csrf_token`   — value to feed `csrf_field_html(...)` from form code
      `_csrf_mint`   — internal; tells send_html_response to set the
                       CSRF cookie
    """
    flash = read_flash(handler)
    csrf, minted = _read_or_mint_csrf(handler)
    ctx: dict[str, Any] = {
        "flash": flash,
        "csrf_token": csrf,
        "_csrf_mint": minted,
        "_flash_set": bool(flash),
    }
    setattr(handler, _CTX_ATTR, ctx)
    return ctx


def _request_ctx(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    return getattr(handler, _CTX_ATTR, {}) or {}


def send_html_response(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    *,
    status: int = 200,
) -> None:
    """Send an HTML response with the JTS conventions baked in:
      * `Cache-Control: no-store` so back-navigation never resurrects a
        stale form snapshot (wizards render runtime state; staleness
        leads to "I clicked Save but it kept the old value" reports).
      * Sets the CSRF cookie if `begin_request()` minted a new one.
      * Clears the flash cookie if a flash was read this request, so the
        next render doesn't keep showing the success banner.
    The handler's old per-wizard `_send_html` should delegate here."""
    ctx = _request_ctx(handler)
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    if ctx.get("_csrf_mint"):
        handler.send_header(
            "Set-Cookie", _csrf_set_cookie_header(ctx["csrf_token"]),
        )
    if ctx.get("_flash_set"):
        handler.send_header("Set-Cookie", _flash_clear_cookie_header())
    handler.end_headers()
    handler.wfile.write(body)


def send_see_other(
    handler: BaseHTTPRequestHandler,
    location: str,
    *,
    flash: str = "",
) -> None:
    """Send a 303 SEE_OTHER redirect. Optionally sets the flash cookie so
    the GET target renders a status banner without a `?msg=...` query
    param polluting browser history.

    Replaces every wizard's per-class `_redirect(...)` plus the prior
    `_redirect(f'./?msg={urllib.parse.quote(msg)}')` pattern."""
    handler.send_response(http.HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    if flash:
        handler.send_header("Set-Cookie", _flash_set_cookie_header(flash))
    handler.end_headers()


def mask_secret(value: str) -> str:
    """Render a secret as `prefix…suffix` for display.

    Always shows enough of the prefix that the user can verify they
    pasted the right key family (sk-… for OpenAI, AIzaSy… for Google,
    xai-… for xAI), but hides the bulk so a screenshot of the page
    doesn't leak the secret. Empty input returns an empty string so
    the caller can render a "(not set)" placeholder."""
    if not value:
        return ""
    if len(value) <= 8:
        return "…" * len(value)
    return f"{value[:4]}…{value[-4:]}"
