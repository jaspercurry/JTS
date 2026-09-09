# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Page-shell markup for the canonical design system; no state reads.

`pair_banner_html` stays in `_common` because it reads multiroom state.
"""
from __future__ import annotations

import html
import json
import urllib.parse
from typing import Any

from ..env_load import parse_env_file
from ..install_profile import BUILD_MANIFEST_FILE
from ._common import control_token_meta_html, csrf_meta_html


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


def _asset_version() -> str:
    """Current cache-busting token for canonical design assets.

    nginx serves /assets/ with `immutable, max-age=1y`, so the linked URL
    must change when the stylesheet does. We key it on the deployed build
    SHA (written to /var/lib/jasper/build.txt by install.sh) — a new
    deploy is exactly when app.css can change. Fail-soft: a missing or
    unreadable file yields "dev", a still-valid (un-busted) URL.

    Read on each HTML render rather than caching for the process lifetime.
    A wizard can be socket-activated during the install window before the
    verified manifest is written; that long-lived process must notice the
    final atomic manifest replacement. This is one tiny local read per page
    navigation, never part of a wizard's polling/data path."""
    sha = parse_env_file(str(BUILD_MANIFEST_FILE)).get("JASPER_GIT_SHA", "")
    return sha if sha and sha != "unknown" else "dev"


# Curated inline icon sprite for the redesigned pages AND the static landing
# page, which substitutes it at install time (jasper.web.landing). Reference
# one with `<svg class="ico"><use href="#icon-NAME"></use></svg>`. Add a symbol
# here when a page needs a new glyph — keep it a shared set, not per-page.
# Symbols carry geometry only (lucide-style, 24×24, no `stroke-width`): the
# wrapper owns the weight — `.ico` sets 2, a settings row's `.row-icon svg`
# sets 1.9 — and a symbol-level attribute would outrank both.
CANONICAL_ICON_SPRITE = """\
<svg class="sr-only" aria-hidden="true" focusable="false">
  <symbol id="icon-back" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="m15 18-6-6 6-6"></path>
  </symbol>
  <symbol id="icon-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="m9 18 6-6-6-6"></path>
  </symbol>
  <symbol id="icon-sound" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 14h4l5 5V5L8 10H4z"></path>
    <path d="M17 9a5 5 0 0 1 0 6"></path>
    <path d="M19.5 6.5a8.5 8.5 0 0 1 0 11"></path>
  </symbol>
  <symbol id="icon-sliders" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 6h16"></path><path d="M4 12h16"></path><path d="M4 18h16"></path>
    <circle cx="9" cy="6" r="2"></circle><circle cx="15" cy="12" r="2"></circle>
    <circle cx="11" cy="18" r="2"></circle>
  </symbol>
  <symbol id="icon-wave" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M3 12c2.2-4 4.5-4 6.8 0s4.5 4 6.7 0 3.7-4 4.5-2.2"></path>
    <path d="M3 17c2.2-4 4.5-4 6.8 0s4.5 4 6.7 0 3.7-4 4.5-2.2"></path>
  </symbol>
  <symbol id="icon-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M5 12h14"></path><path d="M12 5v14"></path>
  </symbol>
  <symbol id="icon-trash" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M3 6h18"></path>
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path>
    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
    <line x1="10" x2="10" y1="11" y2="17"></line>
    <line x1="14" x2="14" y1="11" y2="17"></line>
  </symbol>
  <symbol id="icon-pencil" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"></path>
    <path d="m15 5 4 4"></path>
  </symbol>
  <symbol id="icon-spark" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.582a.5.5 0 0 1 0 .962L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"></path>
  </symbol>
  <symbol id="icon-shuffle" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"></path>
    <path d="m18 2 4 4-4 4"></path>
    <path d="M2 6h1.9c1.5 0 2.9.9 3.6 2.2"></path>
    <path d="M22 18h-5.9c-1.3 0-2.6-.7-3.3-1.8l-.5-.8"></path>
    <path d="m18 14 4 4-4 4"></path>
  </symbol>
  <symbol id="icon-airplay" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M5 17H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-1"></path>
    <path d="m12 15 5 6H7l5-6z"></path>
  </symbol>
  <symbol id="icon-bluetooth" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="m7 7 10 10-5 5V2l5 5L7 17"></path>
  </symbol>
  <symbol id="icon-music" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <circle cx="8" cy="18" r="4"></circle><path d="M12 18V2l7 4"></path>
  </symbol>
  <symbol id="icon-usb" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <circle cx="10" cy="7" r="1"></circle><circle cx="4" cy="20" r="1"></circle>
    <path d="M4.7 19.3 19 5"></path><path d="m21 3-3 1 2 2 1-3Z"></path>
    <path d="M9.26 7.68 5 12l2 5"></path><path d="m10 14 5 2 3.5-3.5"></path>
    <path d="m18 12 1-1 1 1-1 1Z"></path>
  </symbol>
  <symbol id="icon-source" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 6h16"></path><path d="M4 12h16"></path><path d="M4 18h16"></path>
    <path d="M8 6v12"></path><path d="M16 6v12"></path>
  </symbol>
  <symbol id="icon-voice" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z"></path>
    <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><path d="M12 19v3"></path>
  </symbol>
  <symbol id="icon-chat" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-5.5A8 8 0 1 1 21 12z"></path>
    <path d="M8 10h8"></path><path d="M8 14h5"></path>
  </symbol>
  <symbol id="icon-wake" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 12h2"></path><path d="M18 12h2"></path>
    <path d="M7 7l1.4 1.4"></path><path d="M15.6 15.6 17 17"></path>
    <path d="M17 7l-1.4 1.4"></path><path d="M8.4 15.6 7 17"></path>
    <circle cx="12" cy="12" r="3"></circle>
  </symbol>
  <symbol id="icon-tools" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"></path>
  </symbol>
  <symbol id="icon-weather" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M7 17h10a4 4 0 0 0 0-8 5.5 5.5 0 0 0-10.6 1.5A3.5 3.5 0 0 0 7 17z"></path>
    <path d="M5 5l1.2 1.2"></path><path d="M12 3v2"></path><path d="M19 5l-1.2 1.2"></path>
  </symbol>
  <symbol id="icon-transit" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <rect x="5" y="4" width="14" height="13" rx="2"></rect>
    <path d="M8 8h8"></path><path d="M8 13h.01"></path><path d="M16 13h.01"></path>
    <path d="M8 21l2-4"></path><path d="M16 21l-2-4"></path>
  </symbol>
  <symbol id="icon-calendar" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <rect x="4" y="5" width="16" height="15" rx="2"></rect>
    <path d="M8 3v4"></path><path d="M16 3v4"></path><path d="M4 10h16"></path>
    <path d="M8 14h3"></path><path d="M13 14h3"></path><path d="M8 17h3"></path>
  </symbol>
  <symbol id="icon-home" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M3 11 12 4l9 7"></path><path d="M5 10v10h14V10"></path>
    <path d="M10 20v-6h4v6"></path>
  </symbol>
  <symbol id="icon-wifi" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 20h.01"></path><path d="M2 8.82a15 15 0 0 1 20 0"></path>
    <path d="M5 12.859a10 10 0 0 1 14 0"></path><path d="M8.5 16.429a5 5 0 0 1 7 0"></path>
  </symbol>
  <symbol id="icon-peers" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <circle cx="7" cy="12" r="3"></circle><circle cx="17" cy="7" r="3"></circle>
    <circle cx="17" cy="17" r="3"></circle><path d="M9.5 10.5 14.5 8.5"></path>
    <path d="M9.5 13.5 14.5 15.5"></path>
  </symbol>
  <symbol id="icon-system" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <rect x="4" y="5" width="16" height="11" rx="2"></rect>
    <path d="M8 20h8"></path><path d="M12 16v4"></path>
  </symbol>
  <symbol id="icon-tag" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"></path>
    <circle cx="7.5" cy="7.5" r=".5" fill="currentColor"></circle>
  </symbol>
  <symbol id="icon-software" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="M8 7h8"></path><path d="M8 12h8"></path><path d="M8 17h5"></path>
    <rect x="5" y="3" width="14" height="18" rx="2"></rect>
  </symbol>
  <symbol id="icon-dev" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-linecap="round" stroke-linejoin="round">
    <path d="m8 9-4 3 4 3"></path><path d="m16 9 4 3-4 3"></path><path d="m14 5-4 14"></path>
  </symbol>
</svg>"""


def canonical_page(
    title: str,
    body: str,
    *,
    csrf_token: str = "",
    page_css: str = "",
    page_css_href: str = "",
    app_css_version: str = "",
    control_token_meta: bool = True,
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

    An install-time render passes `app_css_version` (the build SHA the run
    installs — /var/lib/jasper/build.txt still holds the PRIOR one while
    install.sh runs) and `control_token_meta=False`: a page written to disk
    for nginx has no privileged POST to ride the token, so it must not bake
    the secret into a world-readable file.

    Returns bytes; send via `send_html_response()`."""
    version = html.escape(app_css_version or _asset_version())
    csrf = csrf_meta_html(csrf_token) if csrf_token else ""
    ctl_token = control_token_meta_html() if control_token_meta else ""
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
    back_id: str = "",
    tabs_html: str = "",
    tabs_id: str = "",
) -> str:
    """The canonical sticky top bar (`.app-header`) for a migrated wizard.

    Single source of truth for the sub-page chrome: a round back button on
    the left (links ``back_href``, labelled ``back_label`` for screen
    readers, drawn from the shared ``#icon-back`` sprite symbol), the page
    title centred, and an optional ``right_html`` slot on the right (an
    action button, a badge, …). The 3-column grid in ``.app-header__row``
    keeps the title optically centred, so the right slot defaults to an
    empty ``<span>`` placeholder rather than collapsing the grid.

    ``back_id``, ``tabs_html`` and ``tabs_id`` are opt-in, empty by default:
    the sole consumer today is `sound_setup.py`'s EQ editor, whose JS binds
    the back button by id and renders a segmented view strip that must stay
    inside the sticky `.app-header` (`.app-header__tabs`, styled in
    `app.css`) to keep scrolling with it. ``tabs_id`` lands on that wrapper,
    so a page that hides the strip hides the wrapper's border with it.

    ``title`` / ``back_href`` / ``back_label`` are escaped; ``right_html``
    and ``tabs_html`` are caller-trusted markup (it's the caller's job to
    escape any untrusted strings it interpolates, exactly as with
    ``canonical_page``'s body)."""
    right = right_html or "<span></span>"
    back_id_attr = f' id="{html.escape(back_id, quote=True)}"' if back_id else ""
    tabs_id_attr = f' id="{html.escape(tabs_id, quote=True)}"' if tabs_id else ""
    tabs = (
        f'<div class="app-header__tabs"{tabs_id_attr}>{tabs_html}</div>'
        if tabs_html
        else ""
    )
    return (
        '<header class="app-header"><div class="app-header__row">'
        f'<a class="icon-button"{back_id_attr} '
        f'href="{html.escape(back_href, quote=True)}" '
        f'aria-label="{html.escape(back_label, quote=True)}">'
        '<svg class="ico" aria-hidden="true"><use href="#icon-back"></use></svg>'
        '</a>'
        f'<h1 class="app-header__title">{html.escape(title)}</h1>'
        f'{right}'
        f'</div>{tabs}</header>'
    )


def safe_back_href(raw: str | None, *, default: str = "/") -> str:
    """Return a local absolute path suitable for a header back link.

    `return_to` query params are user-controlled, so keep only same-site
    absolute paths like `/assistant/tools/pack/spotify/`. Reject protocol-relative
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
