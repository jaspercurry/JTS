# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
            if not guard_read_request(self):
                return
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
        if not guard_mutating_request(self, form):
            reject_csrf(self); return
        # ... handle ...
        send_see_other(self, "./", flash="Saved.")

Every `<form method="post">` includes `{csrf_field_html(csrf_token)}`
inside it. Every page that uses fetch() for state changes includes
`{csrf_meta_html(csrf_token)}` in the document and
`{csrf_fetch_helpers_js()}` in its script, then uses `jsonHeaders()`
or `csrfHeaders({...})` on state-changing POSTs.

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

import html
import http
import json
import logging
import os
import re
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any

from ..atomic_io import atomic_write_text
from ..control import client as control
from ..control import control_token
from ..control.restart_broker import manage_units
from ..http_security import management_read_allowed, mutating_request_allowed
from ..log_event import log_event
from ..voice.provider_state import read_active_provider

logger = logging.getLogger(__name__)

_LOCAL_WEB_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,253}$")
_IPV4_HOST_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

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


def toggle_html(
    input_id: str, *, checked: bool = False, disabled: bool = False,
) -> str:
    """Render the checkbox markup for the canonical toggle control.

    `input_id` is the DOM id; pages bind to it via
    `document.getElementById(input_id).addEventListener('change', ...)`.
    Initial `checked` / `disabled` set the first-paint state — server-
    rendered HTML is hydrated by a /state poll so the actual value
    converges to truth within a poll cycle anyway. The `.toggle` classes
    are styled by `/assets/app.css` on canonical pages."""
    attrs = [f'id="{html.escape(input_id)}"', 'type="checkbox"']
    if checked:
        attrs.append("checked")
    if disabled:
        attrs.append("disabled")
    return (
        f'<label class="toggle">'
        f'<input {" ".join(attrs)}>'
        f'<span class="track"></span>'
        f'</label>'
    )


# ---------------------------------------------------------------------------
# Canonical design system (the redesigned look).
# ---------------------------------------------------------------------------
#
# The management landing page (deploy/index.html) and the redesigned
# wizards share one stylesheet — /assets/app.css — served static by nginx
# and browser-cached. `canonical_page()` emits the document shell
# (head + stylesheet link + CSRF meta + the shared icon sprite) so a
# wizard authors only its body. Page-specific CSS rides in `page_css`;
# shared primitives live in app.css. This is the seam every migrated
# wizard reuses.

_asset_version_cache: str | None = None


def _asset_version() -> str:
    """Cache-busting token for /assets/app.css.

    nginx serves /assets/ with `immutable, max-age=1y`, so the linked URL
    must change when the stylesheet does. We key it on the deployed build
    SHA (written to /var/lib/jasper/build.txt by install.sh) — a new
    deploy is exactly when app.css can change. Fail-soft: a missing or
    unreadable file yields "dev", a still-valid (un-busted) URL. Read
    once per process; the socket-activated web server is short-lived and
    a deploy restarts it, so the cache can't go stale in practice."""
    global _asset_version_cache
    if _asset_version_cache is not None:
        return _asset_version_cache
    version = "dev"
    try:
        with open("/var/lib/jasper/build.txt") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("JASPER_GIT_SHA="):
                    sha = line.split("=", 1)[1].strip()
                    if sha and sha != "unknown":
                        version = sha
                    break
    except OSError:
        pass
    _asset_version_cache = version
    return version


# Curated inline icon sprite for the redesigned pages. Symbols mirror the
# landing page's set (lucide-style, 24×24, stroked). Reference one with
# `<svg class="ico"><use href="#icon-NAME"></use></svg>`. Add a symbol
# here when a page needs a new glyph — keep it a shared set, not per-page.
CANONICAL_ICON_SPRITE = """\
<svg class="sr-only" aria-hidden="true" focusable="false">
  <symbol id="icon-back" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="m15 18-6-6 6-6"></path>
  </symbol>
  <symbol id="icon-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="m9 18 6-6-6-6"></path>
  </symbol>
  <symbol id="icon-sound" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 14h4l5 5V5L8 10H4z"></path>
    <path d="M17 9a5 5 0 0 1 0 6"></path>
    <path d="M19.5 6.5a8.5 8.5 0 0 1 0 11"></path>
  </symbol>
  <symbol id="icon-sliders" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 6h16"></path><path d="M4 12h16"></path><path d="M4 18h16"></path>
    <circle cx="9" cy="6" r="2"></circle><circle cx="15" cy="12" r="2"></circle>
    <circle cx="11" cy="18" r="2"></circle>
  </symbol>
  <symbol id="icon-wave" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M3 12c2.2-4 4.5-4 6.8 0s4.5 4 6.7 0 3.7-4 4.5-2.2"></path>
    <path d="M3 17c2.2-4 4.5-4 6.8 0s4.5 4 6.7 0 3.7-4 4.5-2.2"></path>
  </symbol>
  <symbol id="icon-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M5 12h14"></path><path d="M12 5v14"></path>
  </symbol>
  <symbol id="icon-trash" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M3 6h18"></path>
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path>
    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
    <line x1="10" x2="10" y1="11" y2="17"></line>
    <line x1="14" x2="14" y1="11" y2="17"></line>
  </symbol>
  <symbol id="icon-pencil" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"></path>
    <path d="m15 5 4 4"></path>
  </symbol>
  <symbol id="icon-spark" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.582a.5.5 0 0 1 0 .962L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"></path>
  </symbol>
</svg>"""


def canonical_page(
    title: str,
    body: str,
    *,
    csrf_token: str = "",
    page_css: str = "",
    page_css_href: str = "",
) -> bytes:
    """Wrap a body fragment in a full HTML document on the canonical
    design system (the redesigned management look).

    Shared tokens, fonts, and component primitives live in the static
    stylesheet /assets/app.css (one source of truth for every page); this
    helper emits the document shell so a wizard authors only its body markup:

      * doctype + head with the cache-busted app.css <link>,
      * the CSRF meta tag (when `csrf_token` is given, for fetch POSTs),
      * an optional per-page stylesheet for components that aren't shared:
        a cache-busted <link> (`page_css_href` — the preferred form: a real,
        lintable static .css file served from /assets/) or an inline <style>
        (`page_css`),
      * the shared inline icon sprite,
      * the caller's `body` (which supplies its own <header>/<main>/
        <script>).

    Returns bytes; send via `send_html_response()`."""
    version = html.escape(_asset_version())
    csrf = csrf_meta_html(csrf_token) if csrf_token else ""
    ctl_token = control_token_meta_html()
    page_link = (
        f'<link rel="stylesheet" href="{html.escape(page_css_href)}?v={version}">'
        if page_css_href else ""
    )
    style = f"<style>{page_css}</style>" if page_css else ""
    head_extra = "\n".join(
        part for part in (csrf, ctl_token, page_link, style) if part
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="/assets/app.css?v={version}">
{head_extra}
</head>
<body>
{CANONICAL_ICON_SPRITE}
{body}
</body>
</html>""".encode()


def canonical_header(
    title: str,
    *,
    back_href: str = "/",
    back_label: str = "Home",
    right_html: str = "",
) -> str:
    """The canonical sticky top bar (`.app-header`) for a migrated wizard.

    Single source of truth for the sub-page chrome: a round back button on
    the left (links ``back_href``, labelled ``back_label`` for screen
    readers, drawn from the shared ``#icon-back`` sprite symbol), the page
    title centred, and an optional ``right_html`` slot on the right (an
    action button, a badge, …). The 3-column grid in ``.app-header__row``
    keeps the title optically centred, so the right slot defaults to an
    empty ``<span>`` placeholder rather than collapsing the grid.

    ``title`` / ``back_href`` / ``back_label`` are escaped; ``right_html``
    is caller-trusted markup (it's the caller's job to escape any untrusted
    strings it interpolates, exactly as with ``canonical_page``'s body)."""
    right = right_html or "<span></span>"
    return (
        '<header class="app-header"><div class="app-header__row">'
        f'<a class="icon-button" href="{html.escape(back_href, quote=True)}" '
        f'aria-label="{html.escape(back_label, quote=True)}">'
        '<svg class="ico" aria-hidden="true"><use href="#icon-back"></use></svg>'
        '</a>'
        f'<h1 class="app-header__title">{html.escape(title)}</h1>'
        f'{right}'
        '</div></header>'
    )


def safe_back_href(raw: str | None, *, default: str = "/") -> str:
    """Return a local absolute path suitable for a header back link.

    `return_to` query params are user-controlled, so keep only same-site
    absolute paths like `/tools/pack/spotify/`. Reject protocol-relative
    URLs, schemes, backslashes, and control-character tricks before the value
    reaches `canonical_header()`.
    """
    if not raw:
        return default
    value = raw.strip()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        return default
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return default
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(("", "", path, parsed.query, ""))


def canonical_banner(message: str) -> str:
    """A canonical flash banner (`.banner`) for a migrated wizard.

    A flash string written by the shared ``send_see_other(flash=...)`` maps
    to a stable status, danger, or info severity. An empty / blank message
    renders nothing (returns ``""``) so the caller can unconditionally drop
    ``canonical_banner(flash)`` into the body:

      * contains "error" or "fail" (case-insensitive) → ``banner--danger``
      * starts with "saved" / "cleared" → ``banner--ok``
      * otherwise → ``banner--info``
    """
    if not message or not message.strip():
        return ""
    lowered = message.lower()
    if "error" in lowered or "fail" in lowered:
        tone = "banner--danger"
    elif lowered.startswith(("saved", "cleared")):
        tone = "banner--ok"
    else:
        tone = "banner--info"
    return (
        f'<div class="banner {tone}" role="status">'
        f'{html.escape(message)}</div>'
    )


# Translation applied to the serialized JSON of a data island. `<`, `>`,
# and `&` can only appear inside JSON string values, never in JSON
# structure, so a whole-text translate is safe. This is the same approach
# as Django's `json_script` filter: escaping `<` kills both `</script>`
# early-close breakouts and `<!--` script-data parser-state tricks.
_JSON_ISLAND_ESCAPES = {
    ord("<"): "\\u003C",
    ord(">"): "\\u003E",
    ord("&"): "\\u0026",
}


def json_island(element_id: str, payload: Any) -> str:
    """Serialize ``payload`` into an inert JSON data island.

    The returned element has this shape:

        <script type="application/json" id="...">...</script>

    This is the shared way a wizard hands Python-built page data to its
    ES module. The module reads it back with::

        JSON.parse(document.getElementById("...").textContent)

    Why a helper: an inline ``<script>``'s content ends at the first
    ``</script`` regardless of the ``type`` attribute, so untrusted
    strings serialized into an island could close it early and inject
    markup unless serialization guards ``<``. Centralizing the dumps and
    escape here makes that guard hard to forget; a conventions test
    asserts no page hand-rolls an ``application/json`` island.

    ``element_id`` is developer-supplied by convention, but it is
    attribute-escaped anyway, matching Django's ``json_script``. That
    keeps a future dynamic id from breaking out of the attribute.
    """
    body = json.dumps(payload).translate(_JSON_ISLAND_ESCAPES)
    safe_id = html.escape(element_id, quote=True)
    return (
        f'<script type="application/json" id="{safe_id}">{body}</script>'
    )


def read_env_file(path: str) -> dict[str, str]:
    """Parse a systemd-style EnvironmentFile (KEY=VALUE per line, no
    quoting). Returns {} if the file is missing or unreadable.

    Same shape used by `/var/lib/jasper-intsecrets/spotify_credentials.env` and
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
# Files only one daemon reads keep the 0o600 default. See
# docs/HANDOFF-privilege-separation.md "Phase 4".
SECRET_ENV_MODE = 0o640


def write_env_file(path: str, values: dict[str, str], *, mode: int = 0o600) -> None:
    """Atomically write a systemd EnvironmentFile-shaped key=value file
    with the given mode (default 0o600 — these files contain API keys
    and OAuth secrets; pass ``SECRET_ENV_MODE`` for the ones a non-root
    jasper-control reads off disk — see that constant).

    Atomicity matters: a half-written env file at restart time could
    leave jasper-voice with a partial config and a real-world impact
    (silent failure cue, lost session). Routes through the canonical
    ``jasper.atomic_io.atomic_write_text`` (unique-temp-file + ``os.replace``),
    which prevents torn reads by concurrent readers but does NOT protect
    against a lost-update race between two writers that each read the old
    file, change different keys, and then publish whole-file replacements —
    the ``ThreadingHTTPServer`` runs ``/save``/``/cities``/``/clear`` on
    separate threads against the same file with no lock. Callers that need
    cross-writer read-modify-write safety should use
    ``jasper.atomic_io.locked_update_env_file`` instead."""
    lines: list[str] = []
    for key, val in values.items():
        # KEY=VALUE without quoting, matching systemd's EnvironmentFile
        # parsing. API keys are alphanumeric with `-`/`_`/`.` so this is
        # safe; the guard keeps the contract honest if someone passes a
        # value with a newline (which would split into a bogus second line).
        if "\n" in val or "\r" in val:
            raise ValueError(f"env value for {key} contains newline")
        lines.append(f"{key}={val}\n")
    atomic_write_text(path, "".join(lines), mode=mode)


def delete_env_file(path: str) -> None:
    """Best-effort delete; missing-file is fine."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not delete %s: %s", path, e)


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


def bonded_follower_active() -> bool:
    """True when this speaker is an ACTIVE bonded multiroom FOLLOWER —
    the dumb-follower profile parks voice/AEC/renderers while paired.
    The ONE shared predicate (multiroom.config.is_bonded_follower),
    fail-open to False so a broken read never blocks a solo wizard."""
    try:
        from ..multiroom.config import is_bonded_follower, load_config

        return is_bonded_follower(load_config())
    except Exception:  # noqa: BLE001 — fail-open
        return False


def bonded_follower_leader_addr() -> str:
    """Return the bonded follower's configured leader address, if readable."""
    try:
        from ..multiroom.config import follower_leader_addr, load_config

        return follower_leader_addr(load_config()) or ""
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
        '(<a href="/rooms/">manage the pair</a>).</div>'
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
    # deploy/polkit/49-jasper-control.rules and docs/HANDOFF-privilege-separation.md.)
    restart_systemd_units("jasper-voice")


# Upper bound on a wizard form body. Every wizard POST here is a small
# urlencoded form (a handful of short fields); the largest realistic body
# is a pasted token or SSID list, far under this. nginx caps uploads at 1m
# in production, but that's a proxy mitigation, not a code guard — a direct
# hit on the socket-activated wizard (no nginx) must still be bounded so a
# bogus Content-Length can't make the handler allocate an unbounded read.
MAX_FORM_BODY_BYTES = 1024 * 1024


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


def guard_mutating_host(handler: BaseHTTPRequestHandler) -> bool:
    """Return True iff a state-changing request's Host/Origin is allowed.

    Mirrors jasper-control's `_guard_mutating_request` (server.py): a
    browser DNS-rebinding / cross-site shape can reach the nginx-fronted
    wizards exactly as it can reach the control daemon, so the wizards
    must apply the same allowlist before mutating WiFi PSKs, HA tokens,
    API keys, or triggering reboots. Reuses
    `jasper.http_security.mutating_request_allowed` — the same allowlist
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
    is defense-in-depth on the annoyance-class routes, not a hard boundary (see
    docs/HANDOFF-privilege-separation.md). Emits nothing when the gate is off
    (no token file), so non-control pages stay byte-identical until the token
    exists."""
    token = control_token.current_token()
    if not token:
        return ""
    return f'<meta name="jts-control-token" content="{html.escape(token)}">'


def csrf_fetch_helpers_js() -> str:
    """JavaScript helpers for fetch()-driven wizard POSTs.

    Pages render `csrf_meta_html()` once, include this snippet in their
    script, and use:

      * `jsonHeaders()` for JSON-bodied mutating POSTs.
      * `csrfHeaders({...})` when the POST has a non-JSON content type
        such as `audio/wav`.

    The helpers tolerate a missing meta tag so static render tests can
    call page renderers without minting a token."""
    return """
function csrfHeaders(headers) {
  var out = headers || {};
  var tokenEl = document.querySelector('meta[name=jts-csrf]');
  var token = tokenEl ? tokenEl.content : '';
  if (token) out['X-CSRF-Token'] = token;
  return out;
}
function jsonHeaders() {
  return csrfHeaders({'Content-Type': 'application/json'});
}
""".strip()


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


def redirect_with_legacy_msg(
    handler: BaseHTTPRequestHandler,
    location: str,
) -> None:
    """Redirect while translating a legacy ``?msg=...`` to a flash cookie.

    Google and Spotify still have older call sites that encode their status
    message in the redirect target.  Keep their compatibility behavior in one
    place while new code calls ``send_see_other(..., flash=...)`` directly.
    """
    parsed = urllib.parse.urlparse(location)
    if parsed.query:
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        messages = query.pop("msg", None)
        flash = (messages[0] if messages else "").strip()
        if flash:
            clean_query = urllib.parse.urlencode(query, doseq=True)
            clean_location = urllib.parse.urlunparse(
                parsed._replace(query=clean_query),
            )
            send_see_other(handler, clean_location, flash=flash)
            return
    send_see_other(handler, location)


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


# ---------------------------------------------------------------------------
# jasper-control HTTP proxy helpers.
# ---------------------------------------------------------------------------
#
# Several wizards (today: /system, /wake) forward a handful of read +
# write endpoints to the jasper-control daemon on 127.0.0.1:8780. These
# are thin wrappers over jasper.control.client (the one owner of the base
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
