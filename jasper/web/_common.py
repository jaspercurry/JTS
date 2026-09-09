# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for the JTS web setup pages.

Every wizard under `jasper/web/` (Spotify, voice, transit, wake, …)
shares the `systemctl restart jasper-voice` shell-out and the
request-response plumbing for navigation hygiene (flash cookies, CSRF
tokens, no-store caching); the page shell itself lives in `chrome.py`.
What's NOT shared: per-wizard route handlers, page layouts, form bodies.

## Conventions for new wizards

A wizard is two route tables of bare `handler_fn(handler)` callables and a
dispatcher that hands them to the shared seam — nothing else:

    _GET_ROUTES = {"/": _get_index, "/state": _get_state}
    _POST_ROUTES = {"/save": _post_save, "/clear": _post_clear}

    def do_GET(self):
        dispatch_get(self, _GET_ROUTES)

    def do_POST(self):
        dispatch_post(self, _POST_ROUTES, guard="header")

The tables live wherever their bodies reach the wizard's state: inside
`_make_handler`'s closure when they close over `cfg` or the `Handler` class,
at module level when they close over nothing. The pins drive real handler
instances, so the two read the same.

Unknown paths 404 before any guard, never revealing CSRF state. `route_path`
makes `/save`, `/save/` and `/save?x=1` one key; a prefix family such as
`/layer/<name>` passes `resolve=`, a `path -> callable or None` hook that may
only inspect the path string — no I/O, no state lookup — because it runs
ahead of every guard on a request that has proved nothing. Wear
`@resolve_samples({"/layer/raw": _post_layer})` on that hook: the generic
route pins drive one sample path per prefix family exactly as they drive a
table key, so a family that names no sample is pinned by nothing. A family
shaped `prefix + <param> + suffix` is `prefix_route("/pair/", "/stream",
_get_pair_stream)` rather than a hand-rolled hook, and several of them
compose with `first_match(...)`.
`guard="header"` runs `guard_mutating_request` in the dispatcher, ahead of
any body read, and those POST bodies wear `@json_body`. A wizard whose
guard varies per route passes `guard="per-body"` and each body declares its
own — `@form_guarded` (token in the form body), `@header_guarded` (token in
the header), `@read_guarded` (read-only probe: no token, cross-site
navigations refused). A GET that changes state is a table entry wrapped in
`read_guarded(...)`: under the dispatcher's permissive read guard that
composes to the strict one, refusing the cross-site navigation a plain GET
route allows.

Every `<form method="post">` includes `{csrf_field_html(csrf_token)}`
inside it. Every page that uses fetch() for state changes includes
`{csrf_meta_html(csrf_token)}` in the document and imports
`deploy/assets/shared/js/http.js`, then uses `jsonHeaders()` or
`csrfHeaders({...})` on state-changing POSTs.

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
by browsers, which blocks simple cross-origin form attacks, but new
mutating fetch() endpoints should still send the shared `X-CSRF-Token`
header. That keeps every write path under one obvious rule. Read-only
probe endpoints may skip CSRF when they don't reveal secrets or mutate
speaker state. If a wizard adds a form-bodied POST, it MUST add the
CSRF check.

See `tests/test_web_common.py` for the helpers' behavior contracts.
"""
from __future__ import annotations

import functools
import html
import http
import json
import logging
import os
import re
import secrets
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping
from contextlib import suppress
from http.server import BaseHTTPRequestHandler
from typing import Any, Literal

from ..atomic_io import atomic_write_text
from ..platform import control_client as control
from ..control import control_token
from ..control.restart_broker import manage_units
# Re-exported: google_setup.py still imports both from this module. Drop
# this line once it imports jasper.env_file directly.
from ..env_file import read_env_file, write_env_file  # noqa: F401
from ..identity.identity_state import management_read_allowed, mutating_request_allowed
from ..local_sources.markers import local_sources_allowed
from ..log_event import log_event
from ..multiroom.config import LOCAL_SOURCES_PARK_REASON_BONDED_FOLLOWER
from ..secret_redaction import redact_secrets
from ..voice.provider_state import read_active_provider

logger = logging.getLogger(__name__)

_LOCAL_WEB_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,253}$")
_IPV4_HOST_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_API_KEY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-.~]+$")

# ---------------------------------------------------------------------------
# Cookie + header constants.
# ---------------------------------------------------------------------------

# Flash messages (PRG status text) live in a short-lived cookie instead of a
# `?msg=…` query param on the redirect target. The query-param pattern
# poisoned browser history: the post-save URL `/assistant/voice/?msg=Saved` was a
# distinct entry from `/assistant/voice/`, so clicking Back went to "the same page
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


def value_for_env(
    state: dict[str, str],
    env_var: str,
    default: str = "",
) -> str:
    """Resolve wizard state using the same precedence as its daemon."""
    value = state.get(env_var, "").strip()
    if value:
        return value
    return os.environ.get(env_var, "") or default


# 0o640 group-readable mode for wizard-written secret/config env files (vs
# the 0o600 default), so the daemons that need a file can read it off disk.
# WHICH group depends on WHERE the file lives:
#   - Files under /var/lib/jasper (the shared StateDirectory) land group
#     `jasper` via systemd's recursive StateDirectory chown — voice_provider.env
#     (now keyless), control_token, etc.
#   - WS1 Phase 4a moved the high-value {jasper-voice, jasper-web}-only
#     secrets into the setgid /var/lib/jasper-secrets dir, so a file written
#     there inherits group `jasper-secrets` instead: voice_keys.env (the LLM
#     API keys split out of voice_provider.env) and google_credentials.env.
#   - WS1 Phase 4b moved integration secrets into the setgid
#     /var/lib/jasper-intsecrets dir, so Spotify/HA files inherit group
#     `jasper-intsecrets`.
#     The mode is the same 0o640; only the inherited group differs, which is
#     what narrows those secrets away from jasper-mux/-control/-input.
# Files only one daemon reads keep the 0o600 default.
SECRET_ENV_MODE = 0o640


def write_json_file(path: str, obj, *, mode: int = 0o644) -> None:
    """Atomically write ``obj`` as pretty JSON via the canonical
    ``jasper.atomic_io.atomic_write_text`` (unique-temp + ``os.replace``), so a
    reader (the voice daemon) never sees a half-written file. Default mode
    0644 — JSON config like pricing rates carries no secrets, unlike env
    files."""
    atomic_write_text(
        path, json.dumps(obj, indent=2, sort_keys=True) + "\n", mode=mode,
    )


def restart_systemd_units(*units: str) -> None:
    """Best-effort non-blocking restart for wizard-owned config changes.

    `--no-block` is important. `Type=notify` units make `systemctl
    restart` block until the daemon emits READY=1, which for
    jasper-voice means model load + cue regen + reconnect to the
    LLM provider — often 8–12 s on a Pi. Without --no-block the
    web wizard's save handler hangs that long before returning the
    303 redirect, the browser shows a spinner, the user thinks
    nothing happened and click Save again (then again) — observed
    on PR #117 when switching wake models via the /assistant/wake/ UI.

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
    and the bigger problem will surface elsewhere.

    WS1 Phase 3: this no longer shells out to systemctl directly — it asks
    jasper-control's restart broker to do it (manage_units), so jasper-web
    needs no privilege of its own once dropped to a non-root service user.
    manage_units is best-effort and never raises (same contract as before);
    while jasper-web is still root it falls back to a direct systemctl if the
    broker is unreachable."""
    if not units:
        return
    manage_units(
        *units, verb="restart", reason="wizard config change",
        no_block=True, timeout=5.0,
    )


def bonded_follower_park_reason() -> str:
    """Bounded presentation reason local sources (and Bluetooth) are parked,
    or "" when not parked.

    One of two values: ``"bonded_follower"`` (this speaker is an active
    bonded follower — the pair-member case UI copy already names) or
    ``"role_transition_in_progress"`` (any other reconciler-reported reason,
    collapsed so callers never have to branch on — or leak to a client —
    the reconciler's own internal `blocked_reason` vocabulary).

    A presentation mapping over the reconciler-facing verdict
    (:func:`jasper.local_sources.markers.local_sources_allowed`), which
    honours a prior reconciler deny when the grouping config cannot be read.
    """
    allowed, reason = local_sources_allowed()
    if allowed:
        return ""
    if reason == LOCAL_SOURCES_PARK_REASON_BONDED_FOLLOWER:
        return reason
    return "role_transition_in_progress"


def bonded_follower_active() -> bool:
    """True when a requested follower role actually remains effective.

    The grouping reconciler may refuse an unsafe bond and land solo without
    erasing the household's request.  Its fingerprinted status distinguishes
    that safe fallback from an active follower; missing/stale status keeps a
    requested follower parked until reconciliation proves otherwise.
    """
    return bool(bonded_follower_park_reason())


def bonded_follower_leader_addr() -> str:
    """Return the effective follower's leader address, if readable."""
    try:
        from ..multiroom.config import load_config
        from ..multiroom.effective_role import effective_follower_leader_addr

        return effective_follower_leader_addr(load_config()) or ""
    except Exception:  # noqa: BLE001 — fail-open
        return ""


def local_web_host(value: str) -> str:
    """Return a canonical .local host for speaker web links.

    Speaker-to-speaker state may carry raw hostnames or addresses. UI links
    should prefer mDNS names and must not expose raw IP links from pair state.
    """
    host = str(value or "").strip().rstrip(".")
    if not host or not _LOCAL_WEB_HOST_RE.match(host) or _IPV4_HOST_RE.match(host):
        return ""
    return host if host.endswith(".local") else f"{host}.local"


def bonded_follower_leader_web_url(path: str = "/") -> str:
    """Return the pair leader's web URL for UI hints, or empty if unknown."""
    host = local_web_host(bonded_follower_leader_addr())
    if not host:
        return ""
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"http://{host}{clean_path}"


def pair_banner_html() -> str:
    """A notice for wizard pages whose subject is parked/delegated while
    this speaker is a bonded follower. Empty string when not bonded —
    callers can interpolate unconditionally. Static text only (no
    untrusted values)."""
    if not bonded_follower_active():
        return ""
    return (
        '<div class="info-card info-card--accent" role="note">'
        "This speaker is part of a stereo pair. The assistant, sources, "
        "and leader-owned sound shaping run on the pair leader while paired "
        '(<a href="/sound/pair/">manage the pair</a>).</div>'
    )


def restart_voice_daemon() -> None:
    """Best-effort restart of jasper-voice so it picks up new
    credentials / new provider / wake model on its next boot.

    Two skip gates, both states where a restart would be WRONG:
    provider unset (voice refuses to start anyway), and parked as a
    bonded follower — the dumb-follower profile keeps voice disabled
    while paired, and a wizard save must not boot 240 MB of models
    that jasper-aec-reconcile would re-park; the saved config applies
    on unbond (the un-park path restarts voice with fresh env)."""
    if not read_active_provider():
        logger.info("not starting jasper-voice: JASPER_VOICE_PROVIDER is unset")
        return
    if bonded_follower_active():
        logger.info(
            "not restarting jasper-voice: parked (bonded follower) — "
            "saved config applies on unbond",
        )
        return
    # No explicit `systemctl enable` here. jasper-voice is enabled at install,
    # and the root jasper-aec-reconcile (Tier B) is the authoritative owner of
    # voice's enable/disable (it disables on bonded-follower park and re-enables
    # on unpark). The web side only needs the runtime restart. (WS1 Phase 3b-2:
    # the non-root jasper-control is deliberately NOT granted polkit
    # manage-unit-files — it can't be unit-scoped and `systemctl restart`
    # consults it, which would re-open restart-of-any-unit; see
    # deploy/polkit/49-jasper-control.rules.)
    restart_systemd_units("jasper-voice")


def terminate_process(
    proc: subprocess.Popen[Any] | None,
    *,
    timeout: float = 0.75,
) -> None:
    """Best-effort bounded shutdown of a subprocess a wizard spawned.

    SIGTERM, then SIGKILL after ``timeout`` seconds, then give up: a page
    that cannot reap its own audible helper must still answer the request.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass
    except (OSError, ProcessLookupError):
        pass


def close_awaitable(awaitable: Any) -> None:
    """Release a coroutine no runner took ownership of, so the interpreter
    does not warn about one that was never awaited."""
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


def terminate_async_process(proc: Any) -> None:
    """SIGTERM an ``asyncio.subprocess.Process`` a wizard spawned on the
    background loop. A child that has already exited is not an error.

    Reaping needs that loop, so it is not done here: a caller that must know
    the child is gone awaits ``proc.wait()`` through its own runner.
    """
    if proc is None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()


def reset_session_locked(
    state: dict[str, Any],
    fields: dict[str, Any],
    *,
    proc_key: str,
    error: str = "",
) -> None:
    """Return a measurement flow's session ``state`` to idle, clearing the
    child held under ``proc_key``; call under the flow's own lock, with
    ``fields`` carrying that flow's schema delta. Every step is
    non-blocking — a reap would need the background loop, which deadlocks
    against a playback watcher waiting on the caller's lock.
    """
    holder = state.get(proc_key)
    if holder:
        terminate_async_process(holder.get("proc"))
    release = state.get("release_window")
    state.update({
        "phase": "idle",
        "error": error,
        "members": None,
        "session_token": int(state.get("session_token", 0)) + 1,
        "release_window": None,
        proc_key: None,
        **fields,
    })
    if release is not None:
        release()


# Upper bound on a wizard form body. Every wizard POST here is a small
# urlencoded form (a handful of short fields); the largest realistic body
# is a pasted token or SSID list, far under this. nginx caps uploads at 1m
# in production, but that's a proxy mitigation, not a code guard — a direct
# hit on the socket-activated wizard (no nginx) must still be bounded so a
# bogus Content-Length can't make the handler allocate an unbounded read.
MAX_FORM_BODY_BYTES = 1024 * 1024


class JsonBodyError(ValueError):
    """Structured validation failure from :func:`read_json_object`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def read_json_object(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """Read one bounded UTF-8 JSON object from a stdlib request handler.

    Missing and zero Content-Length represent an empty object. Validation
    failures are policy-free structured errors; callers own response text and
    status. Stream ``OSError`` exceptions remain distinct for callers whose
    established error contract treats transport failures differently.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    raw_length = handler.headers.get("Content-Length") or "0"
    try:
        length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise JsonBodyError(
            "invalid_content_length",
            "Content-Length must be an integer",
        ) from exc
    if length < 0:
        raise JsonBodyError(
            "negative_content_length",
            "Content-Length must not be negative",
        )
    if length > max_bytes:
        raise JsonBodyError(
            "body_too_large",
            f"JSON body exceeds {max_bytes} bytes",
        )
    if length == 0:
        return {}

    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise JsonBodyError(
            "incomplete_body",
            f"incomplete JSON body: expected {length} bytes, received {len(raw)}",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonBodyError("invalid_utf8", "JSON body must be valid UTF-8") from exc
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise JsonBodyError("invalid_json", "invalid JSON body") from exc
    if not isinstance(parsed, dict):
        raise JsonBodyError("non_object", "JSON body must be an object")
    return parsed


_JSON_BODY_ERRORS = {
    "invalid_content_length": "invalid Content-Length",
    "negative_content_length": "invalid body length",
    "body_too_large": "invalid body length",
    "non_object": "body must be a JSON object",
    "incomplete_body": "incomplete body",
}


def read_json_body(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Wrap :func:`read_json_object` as `(parsed, error)`; exactly one is None."""
    try:
        return read_json_object(handler, max_bytes=max_bytes), None
    except JsonBodyError as exc:
        fallback = (
            f"invalid JSON body: {exc.__cause__}"
            if exc.__cause__ else "invalid JSON body"
        )
        return None, _JSON_BODY_ERRORS.get(exc.code, fallback)


# An OAuth provider returns the single-use authorization code as `?code=…` on
# the callback request line, which the stdlib hands to `log_message` verbatim.
# The query string carries nothing an operator reading the journal needs, so
# it is dropped from the record rather than left to the journal redactor to
# recognise (non-negotiable 3).
_QUERY_STRING_RE = re.compile(r"\?\S*")


def access_log_line(fmt: str, *args: Any) -> str:
    """The stdlib access-log line with its query string dropped. A wizard
    whose routes carry a secret in the query string (the OAuth callbacks)
    renders its `log_message` through this."""
    return _QUERY_STRING_RE.sub("", fmt % args)


def route_path(request_path: str) -> str:
    """Normalise a request line into the key a wizard route table uses:
    query string dropped, trailing slashes trimmed, "" mapped to "/".
    Every wizard dispatcher looks its route up by this, so `/save`,
    `/save/` and `/save?x=1` are one route. Lenient by design: `;params`
    and an absolute-form request line are normalised away before lookup,
    so those reach the guarded route body rather than a 404 — every guard
    still runs."""
    return urllib.parse.urlparse(request_path).path.rstrip("/") or "/"


def read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    """Parse a urlencoded form body off a stdlib BaseHTTPRequestHandler
    request into a single-value dict. Empty values are preserved (so
    we can detect "user pasted nothing" vs "field absent").

    Returns {} on a missing/non-numeric Content-Length or a body larger
    than MAX_FORM_BODY_BYTES — callers then see an empty form, which the
    CSRF/validation guards reject cleanly, rather than the handler
    crashing on a bad header or over-reading a hostile body."""
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except (TypeError, ValueError):
        return {}
    if length <= 0 or length > MAX_FORM_BODY_BYTES:
        return {}
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
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
    # reject anything weird before compare_digest, which raises TypeError
    # on non-ASCII str — and str.isalnum() alone is Unicode-aware, so the
    # isascii() check is load-bearing, not redundant.
    if not 32 <= len(value) <= 128 or not value.isascii():
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


def guard_mutating_host(handler: BaseHTTPRequestHandler) -> bool:
    """Return True iff a state-changing request's Host/Origin is allowed.

    Mirrors jasper-control's `_guard_mutating_request` (server.py): a
    browser DNS-rebinding / cross-site shape can reach the nginx-fronted
    wizards exactly as it can reach the control daemon, so the wizards
    must apply the same allowlist before mutating WiFi PSKs, HA tokens,
    API keys, or triggering reboots. Reuses
    `jasper.identity.identity_state.mutating_request_allowed` — the same allowlist
    the control daemon already runs in production (configured hostname,
    `.local`, RFC1918/ULA/loopback IPs, missing Host for non-browser
    clients). Used by the shared mutating request guard so every wizard
    inherits it at its single mutating chokepoint without per-page edits.

    Some form-rendered handlers parse the small POST form before calling
    `guard_mutating_request(handler, form)` so they can pass the parsed
    token. The load-bearing ordering invariant is: route-check unknown
    POST paths first, then call this host guard before any mutation or
    token compare.

    Returns False (so the caller rejects with 403) on a disallowed
    Host/Origin and logs one structured `event=http.reject` line."""
    ok, reason = mutating_request_allowed(handler.headers)
    if not ok:
        log_event(
            logger,
            "http.reject",
            reason=reason,
            host=repr(handler.headers.get("Host")),
            origin=repr(handler.headers.get("Origin")),
            path=getattr(handler, "path", "?"),
            level=logging.WARNING,
        )
    return ok


def _header_value(handler: BaseHTTPRequestHandler, name: str) -> str:
    return (handler.headers.get(name) or "").strip().lower()


def _is_top_level_navigation(handler: BaseHTTPRequestHandler) -> bool:
    """True for browser document navigations, false for subresource/fetch reads."""
    mode = _header_value(handler, "Sec-Fetch-Mode")
    dest = _header_value(handler, "Sec-Fetch-Dest")
    return mode == "navigate" and dest in ("", "document")


def guard_read_request(
    handler: BaseHTTPRequestHandler,
    *,
    allow_cross_site_navigation: bool = True,
) -> bool:
    """Return True iff a read request's Host / Fetch Metadata is allowed.

    Mirrors jasper-control's read guard for nginx-fronted wizards. This closes
    DNS-rebinding reads of setup pages and JSON polling endpoints without
    adding authentication to the trusted-LAN model. Call after a GET route is
    recognized, but before rendering or returning data, so unknown paths still
    return 404 without revealing guard state.

    Host validation still runs first. Cross-site browser fetch/subresource
    reads fail closed, but top-level document navigations are allowed by
    default so OAuth redirect-follow requests and ordinary links into the
    management UI do not dead-end on a 403. State-changing GET routes should
    pass ``allow_cross_site_navigation=False`` or, preferably, become POSTs.
    """
    ok, reason = management_read_allowed(handler.headers)
    if ok:
        return True
    if (
        allow_cross_site_navigation
        and reason == "cross_site_request"
        and _is_top_level_navigation(handler)
    ):
        return True
    log_event(
        logger,
        "http.reject",
        reason=reason,
        host=repr(handler.headers.get("Host")),
        sec_fetch_site=repr(handler.headers.get("Sec-Fetch-Site")),
        path=getattr(handler, "path", "?"),
        level=logging.WARNING,
    )
    body = (
        b"<!doctype html><meta charset=utf-8>"
        b"<title>Forbidden</title>"
        b"<h1>Forbidden</h1>"
        b"<p>This JTS management page is only available from the "
        b"speaker's trusted LAN hostname or address.</p>"
        + f"<p><code>{html.escape(reason)}</code></p>".encode("utf-8")
    )
    handler.send_response(http.HTTPStatus.FORBIDDEN)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)
    return False


def _csrf_token_valid(
    handler: BaseHTTPRequestHandler, form: dict[str, str] | None = None,
) -> bool:
    """Return True iff the request carries a CSRF token matching the
    double-submit cookie. `secrets.compare_digest` for constant-time
    comparison. Pure token check — no Host/Origin guarding here (that's
    `guard_mutating_host`'s single job; `guard_mutating_request` composes
    the two).

    Accepts the token via either:
      * `form[CSRF_FORM_FIELD]` — the form-rendered case
      * `X-CSRF-Token` request header — for JS-driven POSTs (fetch() with
        empty body, JSON bodies, etc.) where embedding a hidden input is
        awkward. JS reads the token from a `<meta name="jts-csrf">` tag
        the page renders and sends it as a header."""
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


def guard_mutating_request(
    handler: BaseHTTPRequestHandler, form: dict[str, str] | None = None,
) -> bool:
    """Return True iff a state-changing request is allowed to proceed:
    its Host/Origin passes the management allowlist AND it carries a CSRF
    token that matches the cookie.

    This is the single mutating chokepoint every wizard's POST handler
    calls. It composes two single-responsibility checks — keeping the
    name honest about doing both — rather than burying the host guard
    inside a "csrf" function:
      * `guard_mutating_host(handler)` — the DNS-rebinding / cross-site
        Host/Origin allowlist. It runs before token comparison inside
        this function, but some form handlers parse the small request
        body before this call so they can pass `form`. That is acceptable
        only because route checks happen first and mutation happens after
        this function returns True.
      * `_csrf_token_valid(handler, form)` — the double-submit token
        compare.
    Both must pass; a failure of either returns False, and the wizard's
    POST handler turns that into a 403 via `reject_csrf`.

    Use at the top of every state-changing POST handler. Pair with
    `csrf_field_html()` on form-render sites and `csrf_meta_html()` on
    pages whose JS calls fetch."""
    return guard_mutating_host(handler) and _csrf_token_valid(handler, form)


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


def control_token_meta_html() -> str:
    """<meta> tag carrying the WS1 control token, or "" when none exists yet.

    The invisible-token delivery (Phase 2): the page is only served behind the
    management-host / Fetch-Metadata read guard, so a same-origin dashboard sees
    the token in `meta[name=jts-control-token]` and rides it on the destructive
    POSTs (via http.js) with zero household friction. A cross-site fetch can't
    read it; a determined LAN device that fetches the page can — by design this
    is defense-in-depth on the annoyance-class routes, not a hard boundary.
    Emits nothing when the gate is off
    (no token file), so non-control pages stay byte-identical until the token
    exists."""
    token = control_token.current_token()
    if not token:
        return ""
    return f'<meta name="jts-control-token" content="{html.escape(token)}">'


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


# Each wrapper below stamps `csrf_mode` on the callable it returns, so a
# route table declares per route which axis guards it. tests/
# test_web_wizard_conventions.py pins each mode's behaviour off that marker.
def form_guarded(
    fn: Callable[[BaseHTTPRequestHandler, dict[str, str]], None],
) -> Callable[[BaseHTTPRequestHandler], None]:
    """Wrap a form route body as the bare `handler_fn(handler)` a wizard
    route table holds. A form wizard's CSRF token rides in the body, so the
    read has to happen before the guard; doing it here means no route body
    can be written that mutates without one."""
    @functools.wraps(fn)
    def route(handler: BaseHTTPRequestHandler) -> None:
        form = read_form(handler)
        if not guard_mutating_request(handler, form):
            reject_csrf(handler)
            return
        fn(handler, form)
    setattr(route, "csrf_mode", "form")
    return route


def header_guarded(
    fn: Callable[[BaseHTTPRequestHandler], None],
) -> Callable[[BaseHTTPRequestHandler], None]:
    """`form_guarded`'s sibling for a route whose CSRF token rides in the
    X-CSRF-Token header: the guard runs before any body read, so a rejected
    POST leaves the request body unconsumed. Wizards whose POST guard varies
    per route declare it here rather than in their dispatcher."""
    @functools.wraps(fn)
    def route(handler: BaseHTTPRequestHandler) -> None:
        if not guard_mutating_request(handler):
            reject_csrf(handler)
            return
        fn(handler)
    setattr(route, "csrf_mode", "header")
    return route


def read_guarded(
    fn: Callable[[BaseHTTPRequestHandler], None],
) -> Callable[[BaseHTTPRequestHandler], None]:
    """The siblings' third axis, for a POST that only reads: no CSRF token,
    but the read guard runs with cross-site top-level navigations refused,
    so a cross-site auto-submitting form cannot reach the body. The
    permissive default exists for links and OAuth redirect-follows into a
    GET page; a POST has neither."""
    @functools.wraps(fn)
    def route(handler: BaseHTTPRequestHandler) -> None:
        if not guard_read_request(handler, allow_cross_site_navigation=False):
            return
        fn(handler)
    setattr(route, "csrf_mode", "read")
    return route


def json_body(fn: Callable[[Any, dict[str, Any]], None]) -> Callable[[Any], None]:
    """Wrap a JSON route body as the bare `handler_fn(handler)` a wizard
    route table holds. The wizard's own `_read_json()` returns the parsed
    object, or None once it has already answered the client itself; a wizard
    may instead coerce a bad body to {} (wifi_setup)."""
    @functools.wraps(fn)
    def route(handler: Any) -> None:
        body = handler._read_json()
        if body is None:
            return
        fn(handler, body)
    route.reads_json_body = True  # type: ignore[attr-defined]
    return route


# The handler is `Any`: a table typed against `BaseHTTPRequestHandler` fails
# contravariance against each wizard's own narrower `_Handler`.
RouteFn = Callable[[Any], None]
RouteTable = Mapping[str, RouteFn]
Resolver = Callable[[str], RouteFn | None]
Runner = Callable[[Any, RouteFn, str], None]


def resolve_samples(samples: RouteTable) -> Callable[[Resolver], Resolver]:
    """Name one concrete path per prefix family a `resolve=` hook answers.

    `@resolve_samples({"/layer/raw": _post_layer})` stamps the mapping on the
    hook the way `csrf_mode` rides on a guard wrapper: dispatch ignores it,
    and the generic route pins read it so a `/layer/<name>` family is covered
    by the same 403 / 404 / malformed-body pins an exact table key gets.
    """
    def mark(hook: Resolver) -> Resolver:
        hook.resolve_samples = dict(samples)  # type: ignore[attr-defined]
        return hook
    return mark


def prefix_route(prefix: str, suffix: str, fn: RouteFn) -> Resolver:
    """One prefix family as data: `/pair/<mac>/stream` is
    `prefix_route("/pair/", "/stream", _get_pair_stream)`. The body
    re-derives its own path parameter and rejects a malformed one, which is
    where a bad id belongs — the hook itself only inspects the string."""
    def resolve(path: str) -> RouteFn | None:
        return fn if path.startswith(prefix) and path.endswith(suffix) else None
    return resolve


def first_match(*resolvers: Resolver) -> Resolver:
    """The first of several families to claim the path, else None."""
    def resolve(path: str) -> RouteFn | None:
        return next((r for r in (h(path) for h in resolvers) if r is not None), None)
    return resolve


def _route_for(
    handler: Any, table: RouteTable, resolve: Resolver | None,
) -> tuple[RouteFn | None, str]:
    """Table lookup then the prefix-family hook; sends the 404 itself.
    Returns the route (None once it has answered) and the normalised path
    it routed on, so a caller needing the path does not parse it twice.

    `resolve` runs before every guard, on a request that has proved
    nothing, so it may only inspect the path string — no I/O, no state
    lookup.
    """
    path = route_path(handler.path)
    route = table.get(path)
    if route is None and resolve is not None:
        route = resolve(path)
    if route is None:
        handler.send_error(http.HTTPStatus.NOT_FOUND)
    return route, path


def dispatch_get(
    handler: Any, table: RouteTable, *, resolve: Resolver | None = None,
) -> None:
    """Route a wizard GET. Unknown paths 404 before the read guard runs."""
    route, _ = _route_for(handler, table, resolve)
    if route is not None and guard_read_request(handler):
        route(handler)


def dispatch_post(
    handler: Any,
    table: RouteTable,
    *,
    guard: Literal["header", "per-body"] = "header",
    resolve: Resolver | None = None,
    run: Runner | None = None,
) -> None:
    """Route a wizard POST. Unknown paths 404 before any guard, never
    revealing CSRF state. `guard="header"` runs the mutating chokepoint
    here, ahead of any body read; `guard="per-body"` guards nothing — each
    route body wears `@form_guarded` / `@header_guarded` / `@read_guarded`.

    `run=` is the guarded-call hook for a dispatcher that owns a policy no
    route body can (correction_setup blocks content DSP on a bonded
    follower and nets a whole path family's exceptions): it is handed
    `(handler, route, path)` after the header guard and calls the route
    itself. It pairs with `guard="header"` only — under `guard="per-body"`
    there is no dispatcher guard for it to run behind, so the pairing is
    refused rather than silently unguarded."""
    if run is not None and guard != "header":
        raise ValueError('run= requires guard="header"')
    route, path = _route_for(handler, table, resolve)
    if route is None:
        return
    if guard == "header" and not guard_mutating_request(handler):
        reject_csrf(handler)
        return
    if run is not None:
        run(handler, route, path)
        return
    route(handler)


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


# Cap on the provider/OS text a failure flash quotes: the whole message is
# percent-quoted into one Set-Cookie header value, so keep it far short of any
# header-size limit.
_FLASH_DETAIL_CAP = 220


def flash_error(
    handler: BaseHTTPRequestHandler,
    prefix: str,
    exc: BaseException | str,
) -> None:
    """Redirect to `./` with a failure banner built from `exc`.

    A provider token-endpoint rejection, an `OSError`, or a raw
    provider-echoed query value (e.g. an OAuth callback's `error=`) can
    quote the very credential it was handed, so the text is scrubbed and
    bounded before it reaches the flash cookie and the rendered banner."""
    detail = redact_secrets(str(exc))[:_FLASH_DETAIL_CAP]
    send_see_other(handler, "./", flash=f"{prefix}: {detail}")


def send_rejected_form(
    handler: BaseHTTPRequestHandler,
    render: Callable[..., bytes],
    *,
    flash: str,
) -> None:
    """Answer a rejected form POST by re-rendering the page.

    The `send_see_other(flash=…)` sibling for a validation failure:
    nothing was stored, so there is nothing to redirect to, and the 303
    would throw away everything the user typed (docs/web-ia.md §4).
    `render` is the page's own render function bound to the submitted
    values (`functools.partial`); it is called with the request's CSRF
    token and the rejection text, which the page shows through
    `canonical_banner(status_msg)`.

    Never bind a key, PSK, or token into `render` — leave that field
    blank and say in `flash` that it needs re-entering."""
    ctx = begin_request(handler)
    send_html_response(
        handler,
        render(csrf_token=ctx["csrf_token"], status_msg=flash),
        status=http.HTTPStatus.UNPROCESSABLE_ENTITY,
    )


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


def api_key_token_is_valid(value: str) -> bool:
    """Whether an API key uses the wizards' conservative token alphabet."""

    return bool(_API_KEY_TOKEN_RE.fullmatch(value))


# ---------------------------------------------------------------------------
# jasper-control HTTP proxy helpers.
# ---------------------------------------------------------------------------
#
# Several wizards (today: /system, /wake) forward a handful of read +
# write endpoints to the jasper-control daemon on 127.0.0.1:8780. These
# are thin wrappers over jasper.platform.control_client (the one owner of the base
# URL / transport / error model); they keep the `(status, body)` tuple +
# unreachable-to-502 contract the wizard callers depend on.

DEFAULT_CONTROL_BASE = control.DEFAULT_BASE_URL


def proxy_get(
    path: str,
    *,
    control_base: str = DEFAULT_CONTROL_BASE,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Proxy a GET to jasper-control. Returns `(status, body)`. On
    transport failure, returns `(502, {"error": "..."} JSON)` so the
    caller can write it straight through to its own JSON client without
    branching on transport errors vs HTTP errors. A non-2xx upstream
    status is forwarded verbatim as `(status, body)`. `headers` forwards
    extra request headers (e.g. a browser-supplied X-JTS-Token)."""
    try:
        r = control.get(
            path, base_url=control_base, timeout=timeout, headers=headers,
        )
        return r.status, r.body
    except control.ControlError as e:
        return 502, json.dumps(
            {"error": f"jasper-control unreachable: {e}"},
        ).encode()


def proxy_post(
    path: str,
    *,
    control_base: str = DEFAULT_CONTROL_BASE,
    timeout: float = 5.0,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Proxy a POST to jasper-control. `body` defaults to empty (for
    parameterless action endpoints); pass JSON bytes for endpoints that
    take parameters. Same `(status, body)` contract as `proxy_get`.
    `headers` forwards extra request headers — the system wizard passes a
    browser-supplied X-JTS-Token through so the control-token gate
    sees it (the wizard proxies server-side, so the header can't ride the
    original browser fetch)."""
    try:
        r = control.post(
            path, data=(body or b""), base_url=control_base, timeout=timeout,
            headers=headers,
        )
        return r.status, r.body
    except control.ControlError as e:
        return 502, json.dumps(
            {"error": f"jasper-control unreachable: {e}"},
        ).encode()


def forward_control_token_headers(
    handler: BaseHTTPRequestHandler,
) -> dict[str, str] | None:
    """Extract a browser-supplied ``X-JTS-Token`` to forward to control.

    A wizard proxies the high-impact control mutations server-side, so the
    browser's ``X-JTS-Token`` (the control-token gate) would be lost
    unless the wizard explicitly forwards it. Returns ``{"X-JTS-Token": …}``
    when the header is present, else ``None``. The wizard never injects the token
    from disk — it only
    relays what the operator's browser sent — so the gate stays real (the
    secret lives in the browser, not auto-supplied on the Pi)."""
    token = handler.headers.get("X-JTS-Token")
    if token:
        return {"X-JTS-Token": token}
    return None


def send_proxy_json(
    handler: BaseHTTPRequestHandler, body: bytes, *, status: int = 200,
) -> None:
    """Write a proxied JSON body back to the client. Sends the right
    Content-Type / Content-Length / Cache-Control headers so the
    browser-side fetch() sees a well-formed JSON response even when
    the upstream is down (and we're forwarding a 502 from proxy_get
    / proxy_post)."""
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def send_json_response(
    handler: BaseHTTPRequestHandler,
    payload: Any,
    *,
    status: int = 200,
) -> None:
    """Serialize a local payload and send the canonical JSON response."""
    body = json.dumps(payload).encode("utf-8")
    send_proxy_json(handler, body, status=status)


def send_route_failure(
    send_json: Callable[..., None],
    exc: BaseException,
    *,
    logger: logging.Logger,
    event: str,
    **fields: Any,
) -> None:
    """Log a JSON route's failure as a structured error event, then answer
    the canonical ``{"error": …}`` body with 502.

    ``send_json`` is the handler's own JSON writer (``self._send_json``)
    rather than ``send_json_response``, so whatever bookkeeping a wizard
    wraps around the write still runs — the sound wizard records that a
    response has started so its dispatch-level catch-all never writes a
    second body. ``fields`` forwards extra structured fields to
    ``log_event``; the caller keeps its own ``except`` clause, so which
    exception types reach here stays a per-route decision."""
    log_event(
        logger,
        event,
        level=logging.ERROR,
        exc_info=True,
        result="error",
        **fields,
    )
    send_json({"error": str(exc)}, status=502)
