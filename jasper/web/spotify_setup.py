"""Tiny self-contained HTTP service for adding household Spotify
accounts. Public surface: http://jts.local/spotify/ — visible there
because nginx reverse-proxies /spotify/ → http://127.0.0.1:8765/.

Auth flow: Spotify Authorization Code with PKCE. The Client Secret
is not used (PKCE was designed for clients that can't keep secrets,
which a phone-OAuth-into-a-Pi flow definitely is). The user pastes
only the Client ID into the wizard.

Spotify post-2025 redirect-URI rules require HTTPS for any non-loopback
host. Two supported modes side-step that:

  - bounce (default) — Spotify redirects the phone to a static page on
    GitHub Pages (`https://jaspercurry.github.io/spotify-oauth-callback/
    ?host=<JASPER_HOSTNAME>`), which immediately bounces the browser to
    `http://<JASPER_HOSTNAME>/spotify/oauth-callback?code=…&state=…`
    over plain HTTP. No cert needed on the speaker. The bounce page is
    a separate public repo (`jaspercurry/spotify-oauth-callback`); it's
    a 100-line static file with no analytics or third-party scripts —
    inert by design.

  - manual — Spotify redirects to `http://127.0.0.1:8888/callback`
    (the loopback exception Spotify still allows). The user's phone
    can't actually reach 127.0.0.1 (that's the phone, not the speaker),
    so Safari shows "cannot connect" — the user copies the URL from
    the address bar and pastes it back into the speaker's setup page,
    which extracts the code and exchanges it. Zero external dependency,
    slightly worse UX. The wizard pre-warns the user about the
    "cannot connect" page before kicking off the flow.

State CSRF protection: each /start generates a random nonce, stored in
an in-memory pending-flows map (10-min TTL) keyed to the account name.
The nonce is sent as Spotify's `state` parameter. On callback, the
nonce is looked up to recover the account name; unknown or expired
nonces are rejected. PKCE's verifier itself lives in the per-account
spotipy cache file between /start and /oauth-callback.

Three states, single index page renders the appropriate one:
  1. No CLIENT_ID → wizard prompts for ID and OAuth mode (bounce/manual).
  2. Creds set, no accounts → redirect-URI instructions for the chosen
     mode + add-account form.
  3. Creds set, accounts exist → existing management UI.

Routes (paths the app sees AFTER nginx strips the /spotify/ prefix):
  GET  /                    state-aware setup/management UI
  POST /setup-credentials   save CLIENT_ID + OAUTH_MODE, restart Spotify consumers
  POST /reset-credentials   clear creds (back to state 1)
  POST /start               begin OAuth for `name` — bounce mode
                            303s to Spotify; manual mode renders the
                            pre-warn-and-paste page
  GET  /oauth-callback      bounce-mode lands here via the GH Pages
                            redirect; also accessible directly if a user
                            chooses the "open URL on same-Wi-Fi device"
                            fallback path
  POST /paste-callback      manual-mode primary path; user pastes the
                            URL Safari showed on the connection-refused
                            page, server parses and exchanges
  POST /remove              remove an account by name
  POST /default             change the default account
  GET  /playlist-preview    live-fetch a playlist's name from Spotify
  POST /playlist-add        normalise URL→URI, fetch name, store on
                            account, restart jasper-voice
  POST /playlist-remove     drop a URI from an account
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import re
import secrets
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..accounts import (
    Account,
    Registry,
    default_cache_path_for,
    DEFAULT_REGISTRY_PATH,
)
from ..spotify_router import (
    ACCOUNT_NEEDS_OAUTH,
    ACCOUNT_OK,
    ACCOUNT_REVOKED,
    SPOTIFY_SCOPE,
    AccountStatus,
    BuildResult,
    build_clients,
)
from ..spotify_uri import parse_playlist_uri, playlist_id_from_uri
from ._common import (
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    delete_env_file,
    read_env_file,
    read_form,
    reject_csrf,
    restart_systemd_units,
    send_html_response,
    send_see_other,
    guard_mutating_request,
    write_env_file,
)

# Page-specific stylesheet served static from /assets/. Shared primitives
# (.page, .info-card, .deflist, .badge, .field/.form-actions/.form-hint,
# .banner, .btn--*, .section__title, .eyebrow) come from /assets/app.css;
# only the bounce/manual mode picker, the copy row, the per-account cards +
# token-health badges, the inline playlist editor, and the manual-mode
# pre-warn callout live in spotify.css.
SPOTIFY_PAGE_CSS_HREF = "/assets/spotify/spotify.css"

logger = logging.getLogger(__name__)

# Persisted CLIENT_ID + OAUTH_MODE. Separate from /etc/jasper/jasper.env
# so the web service can write to it without /etc being RW (systemd's
# ProtectSystem=full keeps /etc read-only). jasper-web reads it in
# process; jasper-voice, jasper-control, and jasper-mux source it via
# optional EnvironmentFile so a restart picks up the values written here.
CREDS_FILE = "/var/lib/jasper/spotify_credentials.env"

# Spotify Developer App ID format: 32 lowercase hex characters.
_CLIENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# OAuth flow modes. `bounce` redirects through the static GitHub Pages
# page; `manual` uses the loopback IP and a paste-the-URL fallback.
OAUTH_MODES = ("bounce", "manual")

# Default redirect URIs per mode. The bounce URL points at a static
# page hosted on GitHub Pages from the standalone
# `jaspercurry/spotify-oauth-callback` repo; the `?host=` query param
# tells the page which mDNS hostname to forward back to, so a single
# hosted page works for any speaker hostname.
DEFAULT_BOUNCE_REDIRECT_URI_BASE = (
    "https://jaspercurry.github.io/spotify-oauth-callback/"
)
DEFAULT_MANUAL_REDIRECT_URI = "http://127.0.0.1:8888/callback"


def _default_bounce_redirect_uri(hostname: str) -> str:
    """Build the canonical bounce-mode redirect URI for the given
    hostname. Forks may override the entire URL via
    JASPER_SPOTIFY_BOUNCE_REDIRECT_URI; this is just the default."""
    return f"{DEFAULT_BOUNCE_REDIRECT_URI_BASE}?host={hostname}"


def _redirect_uri_for_mode(mode: str, cfg: dict[str, Any]) -> str:
    """Resolve the redirect URI string for the given mode, honouring
    the cfg-level overrides (which come from env vars at startup)."""
    if mode == "manual":
        return cfg.get("manual_redirect_uri") or DEFAULT_MANUAL_REDIRECT_URI
    return (
        cfg.get("bounce_redirect_uri")
        or _default_bounce_redirect_uri(cfg.get("hostname") or "jts.local")
    )


# In-memory pending-flow store:
#   {nonce: (account_name, code_verifier, code_challenge, created_monotonic)}.
#
# State (the value Spotify echoes back) is the random nonce; we look
# it up on the callback to recover the account this flow belongs to
# AND both halves of the PKCE handshake parameters that were generated
# when the authorize URL was built.
#
# Why persist both: spotipy's `SpotifyPKCE.get_access_token` regenerates
# verifier+challenge if EITHER is None — the guard reads
# `if self.code_verifier is None or self.code_challenge is None`, then
# calls `get_pkce_handshake_parameters()` which clobbers both. Setting
# only `code_verifier` on a fresh instance (leaving `code_challenge`
# at its `__init__` default of None) silently triggers that path,
# regenerates a new verifier, and Spotify rejects the exchange with
# `invalid_grant: code_verifier was incorrect`. So we capture both at
# /start and restore both at /oauth-callback. The challenge value
# isn't used in the exchange POST itself (only `code_verifier` is sent),
# but assigning it suppresses the regeneration check.
#
# 10-minute TTL matches Spotify's auth-code lifetime; expired entries
# are pruned lazily on each /start. Per-process is fine — jasper-web
# runs as one systemd unit, and the wizard isn't load-balanced.
_PENDING_FLOWS: dict[str, tuple[str, str, str, float]] = {}
_FLOW_TTL_SEC = 600.0


def _gc_pending(now: float | None = None) -> None:
    if now is None:
        now = time.monotonic()
    expired = [
        k for k, (_, _, _, t) in _PENDING_FLOWS.items() if now - t > _FLOW_TTL_SEC
    ]
    for k in expired:
        _PENDING_FLOWS.pop(k, None)


def _new_nonce() -> str:
    return secrets.token_urlsafe(16)


def _read_creds_file(path: str = CREDS_FILE) -> dict[str, str]:
    return read_env_file(path)


def _write_creds_file(client_id: str, mode: str, path: str = CREDS_FILE) -> None:
    write_env_file(path, {
        "SPOTIFY_CLIENT_ID": client_id,
        "SPOTIFY_OAUTH_MODE": mode,
    })


def _delete_creds_file(path: str = CREDS_FILE) -> None:
    delete_env_file(path)


def _restart_voice_daemon() -> None:
    restart_systemd_units("jasper-voice")


def _restart_spotify_consumers() -> None:
    restart_systemd_units("jasper-voice", "jasper-control", "jasper-mux")


# ============================================================
# HTML rendering
# ============================================================


# Disambiguation note shown at the top of every Spotify wizard page.
# The /spotify/ wizard was historically the only "Spotify setup" surface
# even though it only handles the Web API account side; users would
# land here looking to make basic Spotify Connect (phone-side
# "pick JTS in the app") work and feel lost. This note steers the
# basic case to /sources/ and frames this wizard as advanced. Rendered
# as a canonical .info-card so it shares the design system's accent
# treatment instead of carrying inline styles.
_DISAMBIGUATION_BANNER = """
<div class="info-card advanced-note">
  <p><strong>Heads up:</strong> basic Spotify Connect (picking JTS from
  your Spotify app's device picker) needs no setup &mdash; it's already on.
  Turn it on or off on the <a href="/sources/">Sources page</a>. This
  wizard is for the <em>advanced</em> case: voice cold-start
  (&ldquo;Hey Jarvis, play Hamilton&rdquo;) and multi-account routing,
  which need per-account OAuth.</p>
</div>
"""


def _wrap_page(
    title: str, body: str, *, csrf_token: str = "", status_msg: str = "",
) -> bytes:
    """Wrap a page body in the canonical document shell.

    Single chokepoint for all five /spotify/ page states (setup wizard,
    redirect-URI page, manual pre-warn, management, plus their shared
    chrome): emits the .app-header back bar, the flash banner, the
    disambiguation note, the body inside <main class="page">, and the
    page's ES module. The CSRF <meta> + cache-busted app.css/spotify.css
    links come from canonical_page(). Page-specific CSS lives in the
    static /assets/spotify/spotify.css (page_css_href), never inline."""
    full = (
        canonical_header(title)
        + '<main class="page">'
        + canonical_banner(status_msg)
        + _DISAMBIGUATION_BANNER
        + body
        + "</main>"
        + '<script type="module" src="/assets/spotify/js/main.js"></script>'
    )
    return canonical_page(
        title, full,
        csrf_token=csrf_token,
        page_css_href=SPOTIFY_PAGE_CSS_HREF,
    )


def _mode_picker_html(*, selected: str = "bounce") -> str:
    """The bounce-vs-manual radio group. Used on the initial credentials
    page and on the management page's settings panel."""
    bounce_checked = ' checked' if selected == 'bounce' else ''
    bounce_class = ' selected' if selected == 'bounce' else ''
    manual_checked = ' checked' if selected == 'manual' else ''
    manual_class = ' selected' if selected == 'manual' else ''
    return f"""
<div class="mode-picker">
  <label class="mode-option{bounce_class}">
    <input type="radio" name="mode" value="bounce"{bounce_checked}>
    <span class="mode-title">Bounce (recommended)</span>
    <span class="mode-sub">
      Spotify redirects through a static page on GitHub Pages, which
      bounces back to your speaker. Smoothest UX. The bounce page is
      checked in next to this code, hosted free on GitHub.
    </span>
  </label>
  <label class="mode-option{manual_class}">
    <input type="radio" name="mode" value="manual"{manual_checked}>
    <span class="mode-title">Manual paste</span>
    <span class="mode-sub">
      No external infrastructure at all. After you approve on Spotify,
      your phone shows "cannot connect" &mdash; that's expected; you copy
      the URL and paste it back into this page.
    </span>
  </label>
</div>
"""


def _setup_wizard_html(csrf_token: str = "", *, status_msg: str = "") -> bytes:
    """State 1: no CLIENT_ID configured. Walk the user through creating
    a Spotify Developer App and pasting the credentials."""
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    body = f"""
<p class="form-hint">Connect this speaker to Spotify. Takes about two minutes.</p>

<h2>Step 1: Create a Spotify Developer App</h2>
<p>If you don't already have one, create a new app on Spotify's developer
   dashboard. Any name and description is fine &mdash; this is just an identity
   for your speaker.</p>
<p><a class="btn btn--default" href="https://developer.spotify.com/dashboard"
       target="_blank" rel="noopener">Open Spotify Developer Dashboard &#8599;</a></p>
<p class="form-hint">After clicking <strong>Create app</strong>, you'll land
   on the app's overview page. The Client ID is shown immediately. You
   do <em>not</em> need the Client Secret &mdash; this speaker uses the PKCE
   flow, which is designed for clients that can't keep secrets.</p>

<h2>Step 2: Pick how Spotify should send you back here</h2>
<p>Spotify requires HTTPS for redirect URIs, but this speaker only runs
   plain HTTP on your home network. Pick one:</p>
{_mode_picker_html(selected="bounce")}

<form method="post" action="setup-credentials" id="creds-form">
  {csrf}
  <input type="hidden" name="mode" id="mode-input" value="bounce">

  <h2>Step 3: Paste the Client ID</h2>
  <div class="field">
    <label for="client_id">Client ID</label>
    <input id="client_id" name="client_id" type="text" required
           autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
           placeholder="32 hex characters, e.g. 1a2b3c&hellip;">
    <p class="form-hint">Stored locally on the speaker. The Client Secret is not needed.</p>
  </div>

  <div class="form-actions">
    <button type="submit" class="btn btn--primary">Save and continue &rarr;</button>
  </div>
</form>
"""
    return _wrap_page(
        "Set up Spotify on this speaker", body,
        csrf_token=csrf_token, status_msg=status_msg,
    )


def _redirect_uri_section_html(redirect_uri: str, client_id: str, mode: str) -> str:
    """The 'copy this URL, paste it into your Spotify app's redirect URIs'
    block. Reused on the post-credential page and the management page."""
    dashboard_link = f"https://developer.spotify.com/dashboard/{html.escape(client_id)}"
    redirect_safe = html.escape(redirect_uri)
    mode_note = (
        "This is the static redirect page hosted on GitHub Pages. "
        "Spotify accepts it because it's HTTPS; the page bounces "
        "you back to your speaker over plain HTTP."
        if mode == "bounce" else
        "This is a loopback IP address — Spotify allows it as a "
        "literal exception. Your phone can't actually reach 127.0.0.1, "
        "which is the point: the OAuth flow lands on a connection-refused "
        "page and you copy the URL back here."
    )
    return f"""
<h2>Add this redirect URL to your Spotify app</h2>
<p>Spotify needs to know where to send users back after they sign in.
   Paste this URL into your Developer App's <strong>Redirect URIs</strong> list.</p>

<div class="copy-row">
  <input id="redirect-uri" type="text" readonly value="{redirect_safe}"
         data-select-on-click>
  <button type="button" class="btn btn--default"
          data-copy-target="redirect-uri">Copy</button>
  <span class="copy-feedback">Copied!</span>
</div>
<p class="form-hint">{mode_note}</p>

<ol class="steps">
  <li>Click <strong>Open this app's settings &#8599;</strong> below &mdash; opens in a new tab.</li>
  <li>On Spotify's page, click <strong>Settings</strong> (or <strong>Edit</strong>),
      scroll to <strong>Redirect URIs</strong>, paste the URL above, click
      <strong>Add</strong>, then <strong>Save</strong> at the bottom.</li>
  <li>Come back here and add your account below.</li>
</ol>

<p><a class="btn btn--default" href="{dashboard_link}" target="_blank" rel="noopener">Open this app's settings &#8599;</a></p>
"""


def _add_account_form_html(csrf_token: str = "") -> str:
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    return f"""
<h2>Add an account</h2>
<form method="post" action="start">
  {csrf}
  <div class="field">
    <label for="name">Your name (label only)</label>
    <input id="name" name="name" type="text" required pattern="[a-zA-Z0-9_-]+"
           placeholder="brittany" autocapitalize="off" autocorrect="off">
    <p class="form-hint">Lowercase, no spaces. Just an internal label &mdash; pick anything.</p>
  </div>

  <div class="form-actions">
    <button type="submit" class="btn btn--primary">Continue with Spotify &rarr;</button>
  </div>
  <p class="form-hint">You'll be sent to Spotify to log in once. The token stays on this speaker.</p>
</form>
"""


def _redirect_uri_page_html(
    redirect_uri: str, client_id: str, mode: str,
    csrf_token: str = "", *, status_msg: str = "",
) -> bytes:
    """State 2: creds saved, no accounts yet. Show the redirect-URI
    setup steps prominently, then the add-account form."""
    masked = client_id[:4] + "…" + client_id[-4:] if len(client_id) > 8 else "configured"
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    body = f"""
<p class="form-hint">Credentials saved (Client ID:
   <span class="credchip">{html.escape(masked)}</span>,
   mode: <strong>{html.escape(mode)}</strong>).
   Two steps left.</p>

{_redirect_uri_section_html(redirect_uri, client_id, mode)}

{_add_account_form_html(csrf_token)}

<form method="post" action="reset-credentials"
      data-confirm="Clear the saved Client ID? You'll need to paste it again."
      data-confirm-danger="1">
  {csrf}
  <div class="form-actions">
    <button type="submit" class="btn btn--danger">Reset Spotify credentials</button>
  </div>
</form>
"""
    return _wrap_page(
        "Almost there — connect Spotify", body,
        csrf_token=csrf_token, status_msg=status_msg,
    )


def _manual_paste_form_html(csrf_token: str = "", *, hint: str | None = None) -> str:
    """The textarea-and-submit form for pasting a callback URL. Lives on
    the manual-mode pre-warn page (primary path) and on the index as a
    fallback for bounce mode (when the auto-redirect couldn't reach
    the speaker, e.g. user is on cellular)."""
    extra = f'<p class="form-hint">{html.escape(hint)}</p>' if hint else ""
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    return f"""
<form method="post" action="paste-callback">
  {csrf}
  <div class="field">
    <label for="pasted">Paste the redirect URL Spotify sent you to</label>
    <textarea id="pasted" name="pasted" class="paste" required
              autocomplete="off" autocapitalize="off" autocorrect="off"
              spellcheck="false"
              placeholder="http://127.0.0.1:8888/callback?code=AQAAA&hellip;&amp;state=&hellip;"></textarea>
    {extra}
  </div>
  <div class="form-actions">
    <button type="submit" class="btn btn--primary">Finish connecting &rarr;</button>
  </div>
</form>
"""


def _manual_prewarn_page_html(
    authorize_url: str, account_name: str,
    csrf_token: str = "", *, status_msg: str = "",
) -> bytes:
    """Manual-mode: after /start, render this page instead of redirecting
    to Spotify. Pre-frames the "cannot connect" page so it doesn't look
    like a failure, then offers the paste field."""
    auth_safe = html.escape(authorize_url)
    body = f"""
<p class="form-hint">Adding account <strong>{html.escape(account_name)}</strong>
   in manual mode.</p>

<div class="prewarn">
  <h3>What's going to happen</h3>
  <ol>
    <li>Tap <strong>Open Spotify Authorization</strong> below.</li>
    <li>Approve on Spotify.</li>
    <li>Your phone will try to load <code>http://127.0.0.1:8888/&hellip;</code>
        and show <strong>"cannot connect"</strong>.
        <strong>That's expected.</strong></li>
    <li>Copy the URL from your browser's address bar &mdash; the whole thing,
        starting with <code>http://127.0.0.1:8888/callback?code=&hellip;</code>.</li>
    <li>Come back to this page and paste it below.</li>
  </ol>
</div>

<p><a class="btn btn--primary" href="{auth_safe}" target="_blank" rel="noopener">Open Spotify Authorization &#8599;</a></p>

{_manual_paste_form_html(csrf_token,
    hint=("Paste the entire URL — the speaker will pick out the code "
          "and state automatically.")
)}

<p><a href=".">&larr; Cancel and go back</a></p>
"""
    return _wrap_page(
        f"Connecting {account_name} on Spotify — manual mode",
        body, csrf_token=csrf_token, status_msg=status_msg,
    )


def _account_playlists_section_html(account: Account, csrf_token: str = "") -> str:
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    rows = []
    for uri, name in account.playlists.items():
        # The playlist name is untrusted (it comes from Spotify); it rides in
        # the escaped data-confirm attribute, never interpolated into JS, so
        # the shared dialog can't be markup-injected.
        confirm_msg = html.escape(f"Remove {name}?", quote=True)
        rows.append(f"""
          <li title="{html.escape(uri)}">
            <span class="pl-name">{html.escape(name)}</span>
            <form method="post" action="playlist-remove"
                  data-confirm="{confirm_msg}" data-confirm-danger="1">
              {csrf}
              <input type="hidden" name="account" value="{html.escape(account.name)}">
              <input type="hidden" name="uri" value="{html.escape(uri)}">
              <button class="btn btn--ghost" type="submit"
                      aria-label="Remove playlist">&times;</button>
            </form>
          </li>""")
    list_html = (
        f"<ul class='pl-list'>{''.join(rows)}</ul>" if rows
        else "<p class='pl-empty'>No custom playlists yet.</p>"
    )
    acct = html.escape(account.name)
    return f"""
<div class="pl-section" data-account="{acct}">
  <h3>Custom playlists</h3>
  {list_html}
  <button type="button" class="btn btn--ghost add-playlist-btn"
          data-target="pl-add-{acct}">+ Add Spotify playlist</button>
  <form method="post" action="playlist-add" class="pl-add" id="pl-add-{acct}" hidden>
    {csrf}
    <input type="hidden" name="account" value="{acct}">
    <input type="text" class="pl-input" name="url_or_uri"
           placeholder="https://open.spotify.com/playlist/&hellip; or spotify:playlist:&hellip;"
           autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
    <button type="submit" class="btn btn--primary pl-submit" disabled>Add</button>
    <span class="pl-preview"></span>
  </form>
</div>"""


def _health_badge_html(status: AccountStatus | None) -> str:
    """Per-account badge: green check when the cached token still
    refreshes, red warning when Spotify signed the account out (re-link
    required), grey when we couldn't probe at all. Renders nothing when
    status is None — defensive: keeps the page usable if the probe
    pipeline is uninitialised. Unicode glyphs are deliberate UI
    elements (consistent with the wizard's ✓ playlist preview marker),
    not decorative emoji — they let the user scan a long account list
    by colour + shape at a glance."""
    if status is None:
        return ""
    if status.state == ACCOUNT_OK:
        return (
            '<span class="health-badge health-ok"'
            ' title="Token is valid">✓ linked</span>'
        )
    if status.state == ACCOUNT_REVOKED:
        return (
            '<span class="health-badge health-revoked"'
            ' title="Spotify signed this account out. '
            'Click Re-link below.">⚠ signed out</span>'
        )
    if status.state == ACCOUNT_NEEDS_OAUTH:
        return (
            '<span class="health-badge health-warn"'
            ' title="No cached token — re-OAuth needed">'
            '○ not linked</span>'
        )
    detail = html.escape(status.detail or "unknown")
    return (
        f'<span class="health-badge health-warn" title="{detail}">'
        '? status unknown</span>'
    )


def _relink_notice_html(status: AccountStatus | None, name: str, csrf_token: str = "") -> str:
    """Banner + Re-link button inside the revoked card. The Re-link
    button POSTs to /start with the account name pre-filled; the OAuth
    callback overwrites the existing cache file at the same path.

    `name` is escaped here for defense in depth — callers already
    escape (see _account_card_html) and the registry constrains names
    to `[a-zA-Z0-9_-]+`, but escaping at the render site keeps the
    function safe if a future caller passes an unescaped name."""
    if status is None or status.state != ACCOUNT_REVOKED:
        return ""
    safe_name = html.escape(name)
    return f"""
<div class="relink-notice">
  <p><strong>Spotify signed {safe_name} out of this speaker.</strong>
     Voice commands targeting this account won't work until you re-link.
     This usually happens after a password change, signing out of all
     devices on Spotify, or a long stretch without using the app.</p>
  <form method="post" action="start">
    {csrf_field_html(csrf_token) if csrf_token else ''}
    <input type="hidden" name="name" value="{safe_name}">
    <button class="btn btn--primary" type="submit">Re-link {safe_name}</button>
  </form>
</div>"""


def _account_card_html(
    account: Account, *, is_default: bool, is_open: bool,
    status: AccountStatus | None = None,
    csrf_token: str = "",
) -> str:
    pl_count = len(account.playlists)
    if pl_count == 0:
        count_label = "no custom playlists"
    elif pl_count == 1:
        count_label = "1 custom playlist"
    else:
        count_label = f"{pl_count} custom playlists"
    name = html.escape(account.name)
    badges = []
    if is_default:
        badges.append(
            '<span class="badge" style="--tone: var(--status-ok)">default</span>'
        )
    health = _health_badge_html(status)
    if health:
        badges.append(health)
    badge_html = " ".join(badges)
    # Auto-open revoked cards: the user should see the re-link CTA
    # without having to click into each account.
    auto_open = is_open or (status is not None and status.state == ACCOUNT_REVOKED)
    open_attr = " open" if auto_open else ""
    set_default_btn = (
        '<button class="btn btn--default" type="submit" disabled>Default</button>'
        if is_default else
        '<button class="btn btn--default" type="submit">Set default</button>'
    )
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    # The account name is registry-constrained to [a-zA-Z0-9_-]+, but it still
    # rides in the escaped data-confirm attribute (never interpolated into JS)
    # so the shared dialog stays injection-safe regardless of caller.
    remove_confirm = html.escape(f"Remove {account.name}?", quote=True)
    return f"""
<details class="account"{open_attr}>
  <summary>
    <span class="acct-name">{name}</span>
    {badge_html}
    <span class="pl-count">{count_label}</span>
  </summary>
  <div class="account-body">
    {_relink_notice_html(status, name, csrf_token)}
    <div class="account-actions">
      <form method="post" action="default">
        {csrf}
        <input type="hidden" name="name" value="{name}">
        {set_default_btn}
      </form>
      <form method="post" action="remove"
            data-confirm="{remove_confirm}" data-confirm-danger="1">
        {csrf}
        <input type="hidden" name="name" value="{name}">
        <button class="btn btn--danger" type="submit">Remove account</button>
      </form>
    </div>
    {_account_playlists_section_html(account, csrf_token)}
  </div>
</details>"""


def _claim_speaker_section_html() -> str:
    """One-time setup notice for cold-start voice playback.

    Linking an account here gives the voice daemon a Web API client
    for that user, but it does NOT log librespot in as that user.
    librespot is a separate process with its own Spotify Connect
    auth — until someone has authenticated it (either by tapping
    JTS in their Spotify app once, or via the OAuth claim script
    below), the speaker is invisible to ANY account's `sp.devices()`
    list and `spotify_play` cold-starts fail with a "device not
    linked" message.

    Spotify's OAuth flow for librespot redirects to a hardcoded
    `http://127.0.0.1:8091/login`, so the auth has to land at the
    Pi via SSH tunnel — we can't drive it from this browser-side
    page. The script wraps that, plus the start/stop dance.
    """
    return """
<details class="info-card">
  <summary><strong>Cold-start voice commands (one-time setup)</strong></summary>
  <div>
    <p>Voice <code>"play X"</code> from silence (no AirPlay session)
       needs the Pi's Spotify Connect (librespot) to be logged in to
       a Spotify account. Linking an account above only sets up the
       <em>Web API</em> client &mdash; librespot is a separate process and
       needs its own one-time sign-in.</p>
    <p>Two ways to do that:</p>
    <ol class="steps">
      <li>Open Spotify on any device on this Wi-Fi, tap the device
          picker, and select <strong>JTS</strong> once. The credential
          is then cached locally and survives restarts.</li>
      <li>Run the OAuth claim script from your laptop &mdash; no phone needed:
          <code>bash scripts/claim-librespot.sh</code>.
          It SSH-tunnels librespot's OAuth callback port, opens Spotify
          auth in your browser, and writes credentials to
          <code>/var/cache/librespot</code>.</li>
    </ol>
    <p class="form-hint">librespot can only be logged in as one user at a
       time. Whichever person last claimed it is the account voice
       cold-starts will play through. Other household members can
       still use Spotify Connect from their phone normally &mdash; that's
       a separate code path that doesn't depend on this state.</p>
  </div>
</details>"""


def _management_html(
    registry: Registry, redirect_uri: str, client_id: str, mode: str,
    csrf_token: str = "", *, status_msg: str = "",
    health_result: BuildResult | None = None,
) -> bytes:
    """State 3: at least one account is OAuthed."""
    cards = []
    for a in registry.accounts:
        is_default = a.name == registry.default_name
        is_open = is_default or bool(a.playlists)
        status = (
            _status_by_name(health_result, a.name)
            if health_result is not None else None
        )
        cards.append(_account_card_html(
            a, is_default=is_default, is_open=is_open, status=status,
            csrf_token=csrf_token,
        ))

    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    body = f"""
<p class="form-hint">Each household member links their own Spotify account once.
   The speaker identifies the active listener by cross-referencing the
   AirPlay-pushed track title with each account's currently-playing
   Spotify track &mdash; no per-device setup needed.</p>

<h2>Accounts</h2>
<p class="form-hint">Click an account to manage it. Custom playlists
   let you reach Spotify-owned algorithmic playlists (Discover Weekly,
   Daily Mix, Release Radar, Daylist) by voice &mdash; they're hidden from
   Spotify's search API, so paste the share link from Spotify desktop's
   right-click &rarr; Share &rarr; Copy link.</p>
{''.join(cards)}

{_add_account_form_html(csrf_token)}

{_claim_speaker_section_html()}

<details class="info-card">
  <summary><strong>Spotify app settings (redirect URI, OAuth mode, reset credentials)</strong></summary>
  <div>
    <p>Currently using <strong>{html.escape(mode)}</strong> mode.
       To switch, reset credentials and choose the other mode when re-pasting
       your Client ID.</p>
    {_redirect_uri_section_html(redirect_uri, client_id, mode)}

    <h3>If a phone can't reach the speaker after authorizing</h3>
    <p>This happens on cellular or a different Wi-Fi. Paste the URL from
       the GitHub Pages bounce-page fallback (or from the address bar in
       manual mode) here:</p>
    {_manual_paste_form_html(csrf_token)}

    <form method="post" action="reset-credentials"
          data-confirm="Clear the saved Client ID? Existing OAuthed accounts will keep working until their tokens expire."
          data-confirm-danger="1">
      {csrf}
      <div class="form-actions">
        <button type="submit" class="btn btn--danger">Reset Spotify credentials</button>
      </div>
    </form>
  </div>
</details>
"""
    return _wrap_page(
        "Spotify accounts on this speaker", body,
        csrf_token=csrf_token, status_msg=status_msg,
    )


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return read_form(handler)


_SHARE_PAGE_TIMEOUT_SEC = 5.0
_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]+)"')


def _fetch_playlist_name_via_share_page(playlist_id: str) -> str | None:
    """Last-resort name lookup: scrape `og:title` from the public
    open.spotify.com share page. Used when the Web API returns 404 for
    algorithmic personalised playlists."""
    import urllib.request
    url = f"https://open.spotify.com/playlist/{playlist_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_SHARE_PAGE_TIMEOUT_SEC) as r:
            body = r.read(50_000).decode("utf-8", errors="replace")
    except (urllib.request.URLError, OSError, TimeoutError) as e:
        logger.info("share-page fetch failed for %s: %s", playlist_id, e)
        return None
    m = _OG_TITLE_RE.search(body)
    if not m:
        return None
    name = m.group(1).strip()
    return name or None


def _resolve_playlist_name(sp, uri: str) -> str | None:
    pid = playlist_id_from_uri(uri)
    if not pid:
        return None
    try:
        info = sp.playlist(pid, fields="name")
    except Exception as e:  # noqa: BLE001
        http_status = getattr(e, "http_status", None)
        if http_status == 404:
            logger.info(
                "Web API 404 for %s — algorithmic playlist suspected, "
                "falling back to share-page scrape", uri,
            )
            return _fetch_playlist_name_via_share_page(pid)
        raise
    name = ((info or {}).get("name") or "").strip()
    return name or None


# Per-account token-health probe results, cached briefly so a page
# refresh doesn't slam Spotify's auth endpoint. build_clients hits
# /api/token for each account; without the cache, every render of the
# /spotify page would be one HTTP round-trip per registered account,
# which adds up under any reasonable rate of UI activity.
_HEALTH_CACHE_TTL_SEC = 60.0
_health_cache: dict[str, Any] = {"at": 0.0, "result": None}


def _invalidate_health_cache() -> None:
    """Drop the cached probe result so the next render re-checks. Call
    after any mutation that could change a token's validity: a fresh
    OAuth callback, manual paste-callback, account removal, or a
    credentials reset."""
    _health_cache["at"] = 0.0
    _health_cache["result"] = None


def _probe_all_health(cfg: dict[str, Any]) -> BuildResult:
    """Per-account token-health probe with a short TTL cache. Returns
    a `BuildResult` whose `statuses` list has one entry per registered
    account. The wizard reads this to render an "ok / revoked /
    not-yet-OAuthed" badge per card."""
    if not cfg.get("client_id"):
        return BuildResult(clients={}, statuses=[])
    now = time.monotonic()
    cached = _health_cache.get("result")
    if cached is not None and (now - _health_cache["at"]) < _HEALTH_CACHE_TTL_SEC:
        return cached
    try:
        registry = Registry.load(cfg["registry_path"])
        result = build_clients(
            registry,
            client_id=cfg["client_id"],
            redirect_uri=_redirect_uri_for_mode(cfg["mode"], cfg),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("health probe build_clients failed: %s", e)
        result = BuildResult(clients={}, statuses=[])
    _health_cache["at"] = now
    _health_cache["result"] = result
    return result


def _status_by_name(result: BuildResult, name: str) -> AccountStatus | None:
    for s in result.statuses:
        if s.name == name:
            return s
    return None


def _spotify_client_for_account(cfg: dict[str, Any], account_name: str):
    """Build a one-shot spotipy client for an account, using its OAuth
    cache. Returns None if creds are unset, the account is unknown, or
    the cached token is missing/unusable."""
    if not cfg.get("client_id"):
        return None
    registry = Registry.load(cfg["registry_path"])
    account = registry.get(account_name)
    if account is None or not account.cache_path:
        return None
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyPKCE
    except ImportError:
        logger.warning("spotipy not installed; playlist preview unavailable")
        return None
    try:
        auth = SpotifyPKCE(
            client_id=cfg["client_id"],
            redirect_uri=_redirect_uri_for_mode(cfg["mode"], cfg),
            scope=SPOTIFY_SCOPE,
            cache_path=account.cache_path,
            open_browser=False,
        )
        if not auth.get_cached_token():
            return None
        return spotipy.Spotify(auth_manager=auth)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not build spotify client for %s: %s", account_name, e)
        return None


def _parse_callback_url(pasted: str) -> tuple[str, str] | None:
    """Pull `code` and `state` out of an arbitrary pasted string. Accepts
    a full URL, just the query-string fragment, or just `code=…&state=…`.
    Returns None if either parameter is missing."""
    s = pasted.strip()
    if not s:
        return None
    # Strip a leading `?` so a bare query-string fragment parses too.
    if s.startswith("?"):
        s = s[1:]
    # If it looks like a URL, take the query-string portion. Otherwise
    # treat the whole thing as a query string.
    if "://" in s:
        parsed = urllib.parse.urlparse(s)
        qs = parsed.query
    else:
        qs = s
    parts = urllib.parse.parse_qs(qs)
    code = (parts.get("code") or [""])[0]
    state = (parts.get("state") or [""])[0]
    if not code or not state:
        return None
    return code, state


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Returns a request handler class closed over the config dict."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _redirect(self, location: str) -> None:
            # Compat shim: if `location` carries a `?msg=…` status
            # message (the wizard's pre-flash-cookie redirect pattern),
            # hoist it into a flash cookie and redirect to the clean
            # URL. The browser ends up at e.g. `./` with the message in
            # a cookie that the next GET renders — no `?msg=` query
            # param polluting browser history, no migrating ~30
            # callsites. New code SHOULD call send_see_other(...,
            # flash=...) directly.
            parsed = urllib.parse.urlparse(location)
            if parsed.query:
                qs = urllib.parse.parse_qs(
                    parsed.query, keep_blank_values=True,
                )
                msgs = qs.pop("msg", None)
                flash = (msgs[0] if msgs else "").strip()
                if flash:
                    clean_query = urllib.parse.urlencode(qs, doseq=True)
                    clean = urllib.parse.urlunparse(
                        parsed._replace(query=clean_query),
                    )
                    send_see_other(self, clean, flash=flash)
                    return
            send_see_other(self, location)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict, *, status: int = 200) -> None:
            import json as _json
            body = _json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)

            if path == "/":
                ctx = begin_request(self)
                self._render_index(
                    ctx["csrf_token"], status_msg=ctx["flash"],
                )
                return

            if path == "/playlist-preview":
                # Read-only AJAX endpoint — no CSRF, no flash.
                self._handle_playlist_preview(qs)
                return

            if path == "/oauth-callback":
                # OAuth callback from Spotify (or the bounce page) —
                # protected by the OAuth `state` nonce, not by CSRF.
                self._handle_oauth_callback_get(qs)
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            # State-changing POST routes — all require CSRF.
            CSRF_POST_ROUTES = (
                "/setup-credentials", "/reset-credentials", "/start",
                "/paste-callback", "/remove", "/default",
                "/playlist-add", "/playlist-remove",
            )
            if path not in CSRF_POST_ROUTES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = _read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return

            if path == "/setup-credentials":
                self._handle_setup_credentials(form)
                return

            if path == "/reset-credentials":
                self._handle_reset_credentials()
                return

            if path == "/start":
                self._handle_start(form)
                return

            if path == "/paste-callback":
                self._handle_paste_callback(form)
                return

            if path == "/remove":
                self._handle_remove(form)
                return

            if path == "/default":
                self._handle_default(form)
                return

            if path == "/playlist-add":
                self._handle_playlist_add(form)
                return

            if path == "/playlist-remove":
                self._handle_playlist_remove(form)
                return

        # --- route bodies ---

        def _render_index(self, csrf_token: str = "", *, status_msg: str = "") -> None:
            if not cfg["client_id"]:
                self._send_html(_setup_wizard_html(
                    csrf_token, status_msg=status_msg,
                ))
                return
            registry = Registry.load(cfg["registry_path"])
            redirect_uri = _redirect_uri_for_mode(cfg["mode"], cfg)
            if not registry.accounts:
                self._send_html(_redirect_uri_page_html(
                    redirect_uri, cfg["client_id"], cfg["mode"], csrf_token,
                    status_msg=status_msg,
                ))
                return
            health = _probe_all_health(cfg)
            self._send_html(_management_html(
                registry, redirect_uri, cfg["client_id"], cfg["mode"],
                csrf_token, status_msg=status_msg,
                health_result=health,
            ))

        def _handle_setup_credentials(self, form: dict[str, str]) -> None:
            client_id = form.get("client_id", "").strip()
            mode = form.get("mode", "").strip() or "bounce"
            if mode not in OAUTH_MODES:
                self._redirect("./?msg=Invalid+OAuth+mode.")
                return
            if not client_id:
                self._redirect("./?msg=Client+ID+is+required.")
                return
            if not _CLIENT_ID_RE.fullmatch(client_id):
                self._redirect(
                    "./?msg=Client+ID+should+be+32+lowercase+hex+characters."
                    "+Double-check+the+value+from+Spotify."
                )
                return
            try:
                _write_creds_file(client_id, mode)
            except OSError as e:
                logger.exception("could not write credentials file")
                self._redirect(f"./?msg=Could+not+save+credentials:+{urllib.parse.quote(str(e))}")
                return
            cfg["client_id"] = client_id
            cfg["mode"] = mode
            # client_id change invalidates every cached token-health
            # verdict (verdicts are computed against the old client_id).
            _invalidate_health_cache()
            _restart_spotify_consumers()
            self._redirect(
                "./?msg=Credentials+saved.+Now+add+the+redirect+URL+to+your+Spotify+app."
            )

        def _handle_reset_credentials(self) -> None:
            _delete_creds_file()
            cfg["client_id"] = ""
            cfg["mode"] = "bounce"
            _invalidate_health_cache()
            _restart_spotify_consumers()
            self._redirect("./?msg=Credentials+cleared.")

        def _handle_start(self, form: dict[str, str]) -> None:
            if not cfg["client_id"]:
                self._redirect("./?msg=Set+up+Spotify+credentials+first.")
                return
            name = form.get("name", "").strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                self._redirect("./?msg=Invalid+name+(letters/digits/_-+only)")
                return

            registry = Registry.load(cfg["registry_path"])
            cache_path = default_cache_path_for(name)
            registry.add_or_update(Account(name=name, cache_path=cache_path))
            registry.save()

            # Generate a CSRF nonce; we'll use it as Spotify's `state`
            # and look it up on the callback to recover the account
            # name AND the PKCE verifier.
            _gc_pending()
            nonce = _new_nonce()

            from spotipy.oauth2 import SpotifyPKCE
            redirect_uri = _redirect_uri_for_mode(cfg["mode"], cfg)
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            auth = SpotifyPKCE(
                client_id=cfg["client_id"],
                redirect_uri=redirect_uri,
                scope=SPOTIFY_SCOPE,
                cache_path=cache_path,
                state=nonce,
                open_browser=False,
            )
            # `get_authorize_url()` lazily generates verifier+challenge
            # on the SpotifyPKCE instance. The challenge goes into the
            # URL we redirect to; both have to come back with us to
            # /oauth-callback, where we'll restore them on a fresh
            # SpotifyPKCE instance before the token exchange. spotipy's
            # CacheFileHandler doesn't persist either value, so we
            # stash both in _PENDING_FLOWS keyed by the nonce. See the
            # `_PENDING_FLOWS` docstring for why both are required.
            authorize_url = auth.get_authorize_url()
            _PENDING_FLOWS[nonce] = (
                name, auth.code_verifier, auth.code_challenge, time.monotonic(),
            )

            if cfg["mode"] == "manual":
                # Don't bounce the browser to Spotify yet — the user
                # needs the pre-warn first or they'll see "cannot
                # connect" at the end and assume something broke.
                self._send_html(_manual_prewarn_page_html(
                    authorize_url, name,
                ))
                return

            # bounce mode: standard 303 to Spotify.
            self._redirect(authorize_url)

        def _handle_oauth_callback_get(self, qs: dict[str, list[str]]) -> None:
            """Bounce mode: GH Pages redirected the browser here with
            ?code=…&state=… in the query string. Validate state, exchange
            code, redirect home with a status message."""
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [""])[0]
            err = (qs.get("error") or [""])[0]
            if err:
                self._redirect(
                    f"./?msg=Spotify+returned+error:+{urllib.parse.quote(err)}"
                )
                return
            if not (code and state):
                self._redirect("./?msg=Missing+code+or+state+from+Spotify")
                return
            self._exchange_and_finish(code, state)

        def _handle_paste_callback(self, form: dict[str, str]) -> None:
            """Manual mode primary path, and bounce-mode-fallback path:
            the user pasted a URL or query-string fragment containing
            code+state. Parse and exchange."""
            pasted = form.get("pasted", "").strip()
            if not pasted:
                self._redirect("./?msg=Paste+the+full+URL+including+code+and+state.")
                return
            parsed = _parse_callback_url(pasted)
            if parsed is None:
                self._redirect(
                    "./?msg=Could+not+find+code+and+state+in+the+pasted+URL."
                    "+Make+sure+you+copied+the+whole+thing."
                )
                return
            code, state = parsed
            self._exchange_and_finish(code, state)

        def _exchange_and_finish(self, code: str, state: str) -> None:
            if not cfg["client_id"]:
                self._redirect("./?msg=Credentials+were+cleared+mid-flow.+Start+over.")
                return
            _gc_pending()
            entry = _PENDING_FLOWS.pop(state, None)
            if entry is None:
                self._redirect(
                    "./?msg=That+authorization+expired+or+wasn't+started+from+this+speaker."
                    "+Start+over."
                )
                return
            account_name, verifier, challenge, _created = entry
            try:
                self._exchange_code(account_name, code, verifier, challenge)
            except Exception as e:  # noqa: BLE001
                logger.exception("oauth exchange failed")
                self._redirect(
                    f"./?msg=Auth+exchange+failed:+{urllib.parse.quote(str(e))}"
                )
                return
            _invalidate_health_cache()
            _restart_spotify_consumers()
            self._redirect(
                f"./?msg=Linked+{urllib.parse.quote(account_name)}+successfully"
            )

        def _handle_remove(self, form: dict[str, str]) -> None:
            name = form.get("name", "")
            registry = Registry.load(cfg["registry_path"])
            cache_path = ""
            a = registry.get(name)
            if a is not None:
                cache_path = a.cache_path
            if registry.remove(name):
                registry.save()
                if cache_path and os.path.isfile(cache_path):
                    try:
                        os.unlink(cache_path)
                    except OSError:
                        pass
                _invalidate_health_cache()
                _restart_spotify_consumers()
                self._redirect(f"./?msg=Removed+{urllib.parse.quote(name)}")
            else:
                self._redirect("./?msg=Account+not+found")

        def _handle_default(self, form: dict[str, str]) -> None:
            name = form.get("name", "")
            registry = Registry.load(cfg["registry_path"])
            if registry.get(name) is not None:
                registry.default_name = name
                registry.save()
                _restart_spotify_consumers()
                self._redirect(f"./?msg=Default+set+to+{urllib.parse.quote(name)}")
            else:
                self._redirect("./?msg=Account+not+found")

        # --- playlist config ---

        def _handle_playlist_preview(self, qs: dict[str, list[str]]) -> None:
            account_name = (qs.get("account") or [""])[0]
            raw = (qs.get("url") or [""])[0]
            uri = parse_playlist_uri(raw)
            if not uri:
                self._send_json({"error": "Not a Spotify playlist URL or URI."})
                return
            sp = _spotify_client_for_account(cfg, account_name)
            if sp is None:
                self._send_json({"error": f"Account '{account_name}' not found or not signed in."})
                return
            try:
                name = _resolve_playlist_name(sp, uri)
            except Exception as e:  # noqa: BLE001
                logger.info("playlist preview failed for %s: %s", uri, e)
                self._send_json({"error": "Spotify couldn't find that playlist."})
                return
            if not name:
                self._send_json({"error": "Couldn't find that playlist's name."})
                return
            self._send_json({"uri": uri, "name": name})

        def _handle_playlist_add(self, form: dict[str, str]) -> None:
            account_name = form.get("account", "").strip()
            raw = form.get("url_or_uri", "").strip()
            uri = parse_playlist_uri(raw)
            if not uri:
                self._redirect("./?msg=Not+a+Spotify+playlist+URL+or+URI")
                return
            sp = _spotify_client_for_account(cfg, account_name)
            if sp is None:
                self._redirect(f"./?msg=Account+{urllib.parse.quote(account_name)}+not+found+or+not+signed+in")
                return
            try:
                name = _resolve_playlist_name(sp, uri)
            except Exception as e:  # noqa: BLE001
                logger.info("playlist add lookup failed for %s: %s", uri, e)
                self._redirect("./?msg=Spotify+couldn%27t+find+that+playlist")
                return
            if not name:
                self._redirect("./?msg=Couldn%27t+find+that+playlist%27s+name")
                return
            registry = Registry.load(cfg["registry_path"])
            if not registry.add_playlist(account_name, uri, name):
                self._redirect("./?msg=Account+not+found")
                return
            registry.save()
            _restart_voice_daemon()
            self._redirect(
                f"./?msg=Added+{urllib.parse.quote(name)}+to+{urllib.parse.quote(account_name)}"
            )

        def _handle_playlist_remove(self, form: dict[str, str]) -> None:
            account_name = form.get("account", "").strip()
            uri = form.get("uri", "").strip()
            if not (account_name and uri):
                self._redirect("./?msg=Missing+account+or+uri")
                return
            registry = Registry.load(cfg["registry_path"])
            if registry.remove_playlist(account_name, uri):
                registry.save()
                _restart_voice_daemon()
                self._redirect(f"./?msg=Removed+playlist+from+{urllib.parse.quote(account_name)}")
            else:
                self._redirect("./?msg=Playlist+not+found")

        def _exchange_code(
            self, account_name: str, code: str,
            verifier: str, challenge: str,
        ) -> None:
            registry = Registry.load(cfg["registry_path"])
            account = registry.get(account_name)
            if account is None:
                raise RuntimeError(f"unknown account: {account_name}")
            from spotipy.oauth2 import SpotifyPKCE
            os.makedirs(os.path.dirname(account.cache_path), exist_ok=True)
            auth = SpotifyPKCE(
                client_id=cfg["client_id"],
                redirect_uri=_redirect_uri_for_mode(cfg["mode"], cfg),
                scope=SPOTIFY_SCOPE,
                cache_path=account.cache_path,
                open_browser=False,
            )
            # Restore BOTH verifier and challenge before exchange.
            # spotipy's `get_access_token` regenerates both via
            # `get_pkce_handshake_parameters()` whenever either is None,
            # so setting just `code_verifier` is silently ignored — the
            # regenerated verifier doesn't match the challenge Spotify
            # already saw, and we get `invalid_grant`. The challenge
            # itself isn't sent on the exchange POST; assigning it just
            # suppresses the regeneration guard.
            auth.code_verifier = verifier
            auth.code_challenge = challenge
            auth.get_access_token(code, check_cache=False)

    return Handler


def _build_cfg(
    *,
    registry_path: str,
    bounce_redirect_uri: str,
    manual_redirect_uri: str,
) -> dict[str, Any]:
    """Resolve cfg from env + on-disk creds; shared by main() and
    make_server() so direct CLI invocation and the jasper-web
    multi-wizard process see identical state."""
    creds_from_file = _read_creds_file()
    client_id = (
        os.environ.get("SPOTIFY_CLIENT_ID", "")
        or creds_from_file.get("SPOTIFY_CLIENT_ID", "")
    )
    mode = (
        os.environ.get("SPOTIFY_OAUTH_MODE", "")
        or creds_from_file.get("SPOTIFY_OAUTH_MODE", "")
        or "bounce"
    )
    if mode not in OAUTH_MODES:
        logger.warning("unknown SPOTIFY_OAUTH_MODE=%r; defaulting to bounce", mode)
        mode = "bounce"
    return {
        "client_id": client_id,
        "mode": mode,
        "bounce_redirect_uri": bounce_redirect_uri,
        "manual_redirect_uri": manual_redirect_uri,
        "registry_path": registry_path,
    }


def make_server(
    target,
    *,
    registry_path: str = DEFAULT_REGISTRY_PATH,
    bounce_redirect_uri: str | None = None,
    manual_redirect_uri: str = DEFAULT_MANUAL_REDIRECT_URI,
    hostname: str = "jts.local",
) -> ThreadingHTTPServer:
    """Build a configured server. `target` is either a pre-bound
    socket.socket (systemd handoff), an (host, port) tuple, or an
    int port (legacy 127.0.0.1 bind). Mirrors voice_setup.make_server
    so jasper.web.__main__ can drive both uniformly."""
    from . import _systemd
    cfg = _build_cfg(
        registry_path=registry_path,
        bounce_redirect_uri=(
            bounce_redirect_uri or _default_bounce_redirect_uri(hostname)
        ),
        manual_redirect_uri=manual_redirect_uri,
    )
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-web",
        description="Spotify-account setup web server for the Jasper smart speaker",
    )
    parser.add_argument("--host", default=os.environ.get("JASPER_SPOTIFY_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JASPER_SPOTIFY_WEB_PORT", "8765")))
    parser.add_argument(
        "--registry",
        default=os.environ.get("JASPER_SPOTIFY_ACCOUNTS_PATH", DEFAULT_REGISTRY_PATH),
    )
    hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
    parser.add_argument(
        "--bounce-redirect-uri",
        default=os.environ.get(
            "JASPER_SPOTIFY_BOUNCE_REDIRECT_URI",
            _default_bounce_redirect_uri(hostname),
        ),
        help="HTTPS redirect URI for bounce mode. Defaults to the "
             "canonical hosted page with `?host=${JASPER_HOSTNAME}`.",
    )
    parser.add_argument(
        "--manual-redirect-uri",
        default=os.environ.get(
            "JASPER_SPOTIFY_MANUAL_REDIRECT_URI",
            DEFAULT_MANUAL_REDIRECT_URI,
        ),
        help="Loopback redirect URI for manual mode.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Standalone CLI path — bind directly. The jasper-web multi-wizard
    # process calls make_server() with a target picked per-port from
    # systemd's handed-off sockets.
    server = make_server(
        (args.host, args.port),
        registry_path=args.registry,
        bounce_redirect_uri=args.bounce_redirect_uri,
        manual_redirect_uri=args.manual_redirect_uri,
        hostname=hostname,
    )
    logger.info(
        "jasper-web listening on http://%s:%d",
        args.host, args.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
