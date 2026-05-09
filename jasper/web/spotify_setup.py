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
  POST /setup-credentials   save CLIENT_ID + OAUTH_MODE, restart jasper-voice
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
from ..spotify_router import SPOTIFY_SCOPE
from ..spotify_uri import parse_playlist_uri, playlist_id_from_uri
from ._common import (
    PAGE_STYLE,
    delete_env_file,
    read_env_file,
    read_form,
    restart_voice_daemon,
    wrap_page,
    write_env_file,
)

logger = logging.getLogger(__name__)

# Persisted CLIENT_ID + OAUTH_MODE. Separate from /etc/jasper/jasper.env
# so the web service can write to it without /etc being RW (systemd's
# ProtectSystem=full keeps /etc read-only). Both jasper-web and
# jasper-voice source this file via an optional EnvironmentFile so a
# restart picks up the values written here.
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
    restart_voice_daemon()


# ============================================================
# HTML rendering
# ============================================================


_SPOTIFY_PAGE_STYLE = PAGE_STYLE + """
  ul.accounts { list-style: none; padding: 0; }
  ul.accounts li { background: #f4f4f4; padding: 0.6em 0.8em;
                    border-radius: 6px; margin-bottom: 0.4em;
                    display: flex; align-items: center; gap: 0.6em; }
  ul.accounts li .name { font-weight: 600; flex: 1; }
  ul.accounts li .badge { background: #4a8; color: white; padding: 0.1em 0.5em;
                            border-radius: 4px; font-size: 0.8em; }
  .account-actions { display: flex; gap: 0.5em; margin: 0.7em 0 0.6em; }
  .account-actions form { margin: 0; }
  .account-actions button { padding: 0.35em 0.8em; font-size: 0.9em; }

  /* Playlist list inside an expanded account */
  .pl-section h3 { font-size: 0.95em; margin: 0.8em 0 0.3em; color: #444; }
  ul.pl-list { list-style: none; padding: 0; margin: 0.3em 0 0.6em; }
  ul.pl-list li {
    display: flex; align-items: center; gap: 0.5em;
    background: #fff; border: 1px solid #e6e6e6; border-radius: 5px;
    padding: 0.5em 0.6em 0.5em 0.7em; margin-bottom: 0.3em;
    font-size: 0.95em;
  }
  ul.pl-list li .pl-name { font-weight: 600; flex: 1;
                            overflow: hidden; text-overflow: ellipsis;
                            white-space: nowrap; }
  ul.pl-list li form { margin: 0; }
  ul.pl-list li .pl-x {
    background: transparent; color: #888; border: 0;
    width: 28px; height: 28px; padding: 0;
    border-radius: 50%; cursor: pointer;
    font-size: 1.1em; line-height: 1;
    display: inline-flex; align-items: center; justify-content: center;
  }
  ul.pl-list li .pl-x:hover { background: #fee; color: #d44; filter: none; }
  .pl-empty { color: #888; font-size: 0.9em; font-style: italic;
              margin: 0.4em 0 0.6em; }
  .add-playlist-btn {
    background: transparent; color: #1db954; border: 1px solid #1db954;
    padding: 0.4em 0.85em; font-size: 0.9em; font-weight: 600;
  }
  .add-playlist-btn:hover { background: #f0fff4; filter: none; }
  /* Initial display:none beats display:flex on toggle reveal — using
     [hidden]+!important is the simpler fix than restructuring. */
  form.pl-add { display: flex; align-items: center; gap: 0.5em;
                margin-top: 0.5em; flex-wrap: wrap; }
  form.pl-add[hidden] { display: none !important; }
  form.pl-add .pl-input {
    flex: 1 1 240px; min-width: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.85em; padding: 0.45em 0.6em;
  }
  form.pl-add .pl-submit { padding: 0.45em 0.9em; font-size: 0.9em; }
  .pl-preview { font-size: 0.88em; color: #666;
                flex-basis: 100%; min-height: 1.2em; margin-top: 0.1em; }
  .pl-preview.success { color: #1db954; font-weight: 600; }
  .pl-preview.error   { color: #b33; }

  /* Mode picker (bounce / manual) on the credentials wizard */
  .mode-picker { display: flex; flex-direction: column; gap: 0.6em;
                 margin: 0.4em 0 0.4em; }
  .mode-picker label {
    display: block; font-weight: normal; padding: 0.7em 0.9em;
    background: #f4f4f4; border: 1px solid #ddd; border-radius: 6px;
    cursor: pointer; margin: 0;
  }
  .mode-picker label.selected { background: #f0fff4; border-color: #1db954; }
  .mode-picker input[type=radio] { margin-right: 0.5em; }
  .mode-picker .mode-title { font-weight: 600; }
  .mode-picker .mode-sub { color: #666; font-size: 0.92em;
                           margin-top: 0.25em; display: block; }

  /* Manual-mode pre-warn page (after /start) */
  .prewarn { background: #fff8e6; border: 1px solid #e0c577;
             border-radius: 6px; padding: 0.8em 1em; margin: 1em 0; }
  .prewarn h3 { margin-top: 0; color: #6b4f00; }
  .prewarn ol { padding-left: 1.4em; margin: 0.6em 0; }
  .prewarn ol > li { margin-bottom: 0.5em; }
  textarea.paste {
    width: 100%; min-height: 5em; padding: 0.5em; border: 1px solid #bbb;
    border-radius: 4px; font-size: 0.9em; box-sizing: border-box;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: #fff;
  }
"""


def _wrap_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    page = wrap_page(title, body, status_msg=status_msg).decode()
    return page.replace(
        f"<style>{PAGE_STYLE}</style>",
        f"<style>{_SPOTIFY_PAGE_STYLE}</style>",
    ).encode()


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
      your phone shows "cannot connect" — that's expected; you copy
      the URL and paste it back into this page.
    </span>
  </label>
</div>
<script>
// Highlight the selected radio's label so the picked card is obvious.
document.querySelectorAll('.mode-picker input[type=radio]').forEach(function(r) {{
  r.addEventListener('change', function() {{
    document.querySelectorAll('.mode-picker label').forEach(function(l) {{
      l.classList.remove('selected');
    }});
    r.parentElement.classList.add('selected');
  }});
}});
</script>
"""


def _setup_wizard_html(*, status_msg: str = "") -> bytes:
    """State 1: no CLIENT_ID configured. Walk the user through creating
    a Spotify Developer App and pasting the credentials."""
    body = f"""
<p class="sub">Connect this speaker to Spotify. Takes about two minutes.</p>

<h2>Step 1: Create a Spotify Developer App</h2>
<p>If you don't already have one, create a new app on Spotify's developer
   dashboard. Any name and description is fine — this is just an identity
   for your speaker.</p>
<p><a class="btn" href="https://developer.spotify.com/dashboard"
       target="_blank" rel="noopener">Open Spotify Developer Dashboard ↗</a></p>
<p class="hint">After clicking <strong>Create app</strong>, you'll land
   on the app's overview page. The Client ID is shown immediately. You
   do <em>not</em> need the Client Secret — this speaker uses the PKCE
   flow, which is designed for clients that can't keep secrets.</p>

<h2>Step 2: Pick how Spotify should send you back here</h2>
<p>Spotify requires HTTPS for redirect URIs, but this speaker only runs
   plain HTTP on your home network. Pick one:</p>
{_mode_picker_html(selected="bounce")}

<form method="post" action="setup-credentials" id="creds-form">
  <input type="hidden" name="mode" id="mode-input" value="bounce">

  <h2>Step 3: Paste the Client ID</h2>
  <label for="client_id">Client ID</label>
  <input id="client_id" name="client_id" type="text" required
         autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
         placeholder="32 hex characters, e.g. 1a2b3c…">
  <small>Stored locally on the speaker. The Client Secret is not needed.</small>

  <p style="margin-top:1.4em">
    <button type="submit">Save and continue →</button>
  </p>
</form>

<script>
// Mirror the picked radio into the hidden form field so the POST
// carries the chosen mode without needing the radios to live inside
// the same form (they don't — the picker is a sibling block).
document.querySelectorAll('.mode-picker input[type=radio]').forEach(function(r) {{
  r.addEventListener('change', function() {{
    document.getElementById('mode-input').value = r.value;
  }});
}});
</script>
"""
    return _wrap_page("Set up Spotify on this speaker", body, status_msg=status_msg)


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
         onclick="this.select();">
  <button type="button" onclick="copyRedirect()">Copy</button>
  <span id="copy-feedback" class="copy-feedback">Copied!</span>
</div>
<p class="hint">{mode_note}</p>

<ol class="steps">
  <li>Click <strong>Open this app's settings ↗</strong> below — opens in a new tab.</li>
  <li>On Spotify's page, click <strong>Settings</strong> (or <strong>Edit</strong>),
      scroll to <strong>Redirect URIs</strong>, paste the URL above, click
      <strong>Add</strong>, then <strong>Save</strong> at the bottom.</li>
  <li>Come back here and add your account below.</li>
</ol>

<p><a class="btn secondary" href="{dashboard_link}" target="_blank" rel="noopener">Open this app's settings ↗</a></p>

<script>
async function copyRedirect() {{
  const input = document.getElementById('redirect-uri');
  const fb = document.getElementById('copy-feedback');
  try {{
    await navigator.clipboard.writeText(input.value);
  }} catch (e) {{
    input.select();
    document.execCommand('copy');
  }}
  fb.classList.add('shown');
  setTimeout(() => fb.classList.remove('shown'), 1800);
}}
</script>
"""


def _add_account_form_html() -> str:
    return """
<h2>Add an account</h2>
<form method="post" action="start">
  <label for="name">Your name (label only)</label>
  <input id="name" name="name" type="text" required pattern="[a-zA-Z0-9_-]+"
         placeholder="brittany" autocapitalize="off" autocorrect="off">
  <small>Lowercase, no spaces. Just an internal label — pick anything.</small>

  <p style="margin-top:1em">
    <button type="submit">Continue with Spotify →</button>
  </p>
  <small>You'll be sent to Spotify to log in once. The token stays on this speaker.</small>
</form>
"""


def _redirect_uri_page_html(
    redirect_uri: str, client_id: str, mode: str, *, status_msg: str = "",
) -> bytes:
    """State 2: creds saved, no accounts yet. Show the redirect-URI
    setup steps prominently, then the add-account form."""
    masked = client_id[:4] + "…" + client_id[-4:] if len(client_id) > 8 else "configured"
    body = f"""
<p class="sub">Credentials saved (Client ID:
   <span class="credbox" style="display:inline-block; padding:0.05em 0.4em">{html.escape(masked)}</span>,
   mode: <strong>{html.escape(mode)}</strong>).
   Two steps left.</p>

{_redirect_uri_section_html(redirect_uri, client_id, mode)}

{_add_account_form_html()}

<form method="post" action="reset-credentials" style="margin-top:3em"
      onsubmit="return confirm('Clear the saved Client ID? You\\'ll need to paste it again.');">
  <button type="submit" class="danger">Reset Spotify credentials</button>
</form>
"""
    return _wrap_page("Almost there — connect Spotify", body, status_msg=status_msg)


def _manual_paste_form_html(*, hint: str | None = None) -> str:
    """The textarea-and-submit form for pasting a callback URL. Lives on
    the manual-mode pre-warn page (primary path) and on the index as a
    fallback for bounce mode (when the auto-redirect couldn't reach
    the speaker, e.g. user is on cellular)."""
    extra = f'<p class="hint">{html.escape(hint)}</p>' if hint else ""
    return f"""
<form method="post" action="paste-callback">
  <label for="pasted">Paste the redirect URL Spotify sent you to</label>
  <textarea id="pasted" name="pasted" class="paste" required
            autocomplete="off" autocapitalize="off" autocorrect="off"
            spellcheck="false"
            placeholder="http://127.0.0.1:8888/callback?code=AQAAA…&state=…"></textarea>
  {extra}
  <p style="margin-top:0.8em">
    <button type="submit">Finish connecting →</button>
  </p>
</form>
"""


def _manual_prewarn_page_html(
    authorize_url: str, account_name: str, *, status_msg: str = "",
) -> bytes:
    """Manual-mode: after /start, render this page instead of redirecting
    to Spotify. Pre-frames the "cannot connect" page so it doesn't look
    like a failure, then offers the paste field."""
    auth_safe = html.escape(authorize_url)
    body = f"""
<p class="sub">Adding account <strong>{html.escape(account_name)}</strong>
   in manual mode.</p>

<div class="prewarn">
  <h3>What's going to happen</h3>
  <ol>
    <li>Tap <strong>Open Spotify Authorization</strong> below.</li>
    <li>Approve on Spotify.</li>
    <li>Your phone will try to load <code>http://127.0.0.1:8888/…</code>
        and show <strong>"cannot connect"</strong>.
        <strong>That's expected.</strong></li>
    <li>Copy the URL from your browser's address bar — the whole thing,
        starting with <code>http://127.0.0.1:8888/callback?code=…</code>.</li>
    <li>Come back to this page and paste it below.</li>
  </ol>
</div>

<p><a class="btn" href="{auth_safe}" target="_blank" rel="noopener">Open Spotify Authorization ↗</a></p>

{_manual_paste_form_html(
    hint=("Paste the entire URL — the speaker will pick out the code "
          "and state automatically.")
)}

<p style="margin-top:2em">
  <a href=".">← Cancel and go back</a>
</p>
"""
    return _wrap_page(
        f"Connecting {account_name} on Spotify — manual mode",
        body, status_msg=status_msg,
    )


def _account_playlists_section_html(account: Account) -> str:
    rows = []
    for uri, name in account.playlists.items():
        rows.append(f"""
          <li title="{html.escape(uri)}">
            <span class="pl-name">{html.escape(name)}</span>
            <form method="post" action="playlist-remove"
                  onsubmit="return confirm('Remove {html.escape(name)}?');">
              <input type="hidden" name="account" value="{html.escape(account.name)}">
              <input type="hidden" name="uri" value="{html.escape(uri)}">
              <button class="pl-x" type="submit" aria-label="Remove playlist">×</button>
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
  <button type="button" class="add-playlist-btn secondary"
          data-target="pl-add-{acct}">+ Add Spotify playlist</button>
  <form method="post" action="playlist-add" class="pl-add" id="pl-add-{acct}" hidden>
    <input type="hidden" name="account" value="{acct}">
    <input type="text" class="pl-input" name="url_or_uri"
           placeholder="https://open.spotify.com/playlist/… or spotify:playlist:…"
           autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
    <button type="submit" class="pl-submit" disabled>Add</button>
    <span class="pl-preview"></span>
  </form>
</div>"""


def _account_card_html(account: Account, *, is_default: bool, is_open: bool) -> str:
    pl_count = len(account.playlists)
    if pl_count == 0:
        count_label = "no custom playlists"
    elif pl_count == 1:
        count_label = "1 custom playlist"
    else:
        count_label = f"{pl_count} custom playlists"
    name = html.escape(account.name)
    badge = '<span class="badge">default</span>' if is_default else ""
    open_attr = " open" if is_open else ""
    set_default_btn = (
        '<button class="secondary" type="submit" disabled>Default</button>'
        if is_default else
        '<button class="secondary" type="submit">Set default</button>'
    )
    return f"""
<details class="account"{open_attr}>
  <summary>
    <span class="name">{name}</span>
    {badge}
    <span class="pl-count">{count_label}</span>
  </summary>
  <div class="account-body">
    <div class="account-actions">
      <form method="post" action="default">
        <input type="hidden" name="name" value="{name}">
        {set_default_btn}
      </form>
      <form method="post" action="remove"
            onsubmit="return confirm('Remove {name}?');">
        <input type="hidden" name="name" value="{name}">
        <button class="danger" type="submit">Remove account</button>
      </form>
    </div>
    {_account_playlists_section_html(account)}
  </div>
</details>"""


_PLAYLIST_JS = r"""
<script>
(function() {
  // Live preview: as the user pastes a URL, debounce and ask the server
  // for the playlist name. Submit button enables only when preview hits.
  document.querySelectorAll('form.pl-add').forEach(function(form) {
    var section = form.parentElement;
    var acct = section.dataset.account;
    var input = form.querySelector('.pl-input');
    var preview = form.querySelector('.pl-preview');
    var submit = form.querySelector('.pl-submit');
    var timer = null;
    var seq = 0;
    function reset() {
      submit.disabled = true;
      preview.textContent = '';
      preview.className = 'pl-preview';
    }
    input.addEventListener('input', function() {
      clearTimeout(timer);
      reset();
      var value = input.value.trim();
      if (!value) return;
      timer = setTimeout(function() {
        var mySeq = ++seq;
        preview.textContent = 'Looking up…';
        var u = new URL('playlist-preview', window.location.href);
        u.searchParams.set('account', acct);
        u.searchParams.set('url', value);
        fetch(u).then(function(r) { return r.json(); }).then(function(j) {
          if (mySeq !== seq) return;
          if (j.error) {
            preview.textContent = j.error;
            preview.classList.add('error');
            return;
          }
          preview.textContent = '✓ ' + j.name;
          preview.classList.add('success');
          submit.disabled = false;
        }).catch(function() {
          if (mySeq !== seq) return;
          preview.textContent = "Couldn't reach speaker.";
          preview.classList.add('error');
        });
      }, 350);
    });
  });
  document.querySelectorAll('.add-playlist-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var target = document.getElementById(btn.dataset.target);
      target.hidden = false;
      btn.style.display = 'none';
      target.querySelector('.pl-input').focus();
    });
  });
})();
</script>
"""


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
<details style="margin-top:2.4em">
  <summary>Cold-start voice commands (one-time setup)</summary>
  <div class="claim-section">
    <p>Voice <code>"play X"</code> from silence (no AirPlay session)
       needs the Pi's Spotify Connect (librespot) to be logged in to
       a Spotify account. Linking an account above only sets up the
       <em>Web API</em> client — librespot is a separate process and
       needs its own one-time sign-in.</p>
    <p>Two ways to do that:</p>
    <ol>
      <li>Open Spotify on any device on this Wi-Fi, tap the device
          picker, and select <strong>JTS</strong> once. The credential
          is then cached locally and survives restarts.</li>
      <li>Run the OAuth claim script from your laptop — no phone needed:
          <pre>bash scripts/claim-librespot.sh</pre>
          It SSH-tunnels librespot's OAuth callback port, opens Spotify
          auth in your browser, and writes credentials to
          <code>/var/cache/librespot</code>.</li>
    </ol>
    <p class="sub">librespot can only be logged in as one user at a
       time. Whichever person last claimed it is the account voice
       cold-starts will play through. Other household members can
       still use Spotify Connect from their phone normally — that's
       a separate code path that doesn't depend on this state.</p>
  </div>
</details>"""


def _management_html(
    registry: Registry, redirect_uri: str, client_id: str, mode: str,
    *, status_msg: str = "",
) -> bytes:
    """State 3: at least one account is OAuthed."""
    cards = []
    for a in registry.accounts:
        is_default = a.name == registry.default_name
        is_open = is_default or bool(a.playlists)
        cards.append(_account_card_html(a, is_default=is_default, is_open=is_open))

    body = f"""
<p class="sub">Each household member links their own Spotify account once.
   The speaker identifies the active listener by cross-referencing the
   AirPlay-pushed track title with each account's currently-playing
   Spotify track — no per-device setup needed.</p>

<h2>Accounts</h2>
<p class="accounts-help">Click an account to manage it. Custom playlists
   let you reach Spotify-owned algorithmic playlists (Discover Weekly,
   Daily Mix, Release Radar, Daylist) by voice — they're hidden from
   Spotify's search API, so paste the share link from Spotify desktop's
   right-click → Share → Copy link.</p>
{''.join(cards)}

{_add_account_form_html()}

{_claim_speaker_section_html()}

<details style="margin-top:2.4em">
  <summary>Spotify app settings (redirect URI, OAuth mode, reset credentials)</summary>
  <p style="margin-top:1em">Currently using <strong>{html.escape(mode)}</strong> mode.
     To switch, reset credentials and choose the other mode when re-pasting
     your Client ID.</p>
  {_redirect_uri_section_html(redirect_uri, client_id, mode)}

  <h3 style="margin-top:1.6em">If a phone can't reach the speaker after authorizing</h3>
  <p>This happens on cellular or a different Wi-Fi. Paste the URL from
     the GitHub Pages bounce-page fallback (or from the address bar in
     manual mode) here:</p>
  {_manual_paste_form_html()}

  <form method="post" action="reset-credentials" style="margin-top:2em"
        onsubmit="return confirm('Clear the saved Client ID? Existing OAuthed accounts will keep working until their tokens expire.');">
    <button type="submit" class="danger">Reset Spotify credentials</button>
  </form>
</details>

{_PLAYLIST_JS}
"""
    return _wrap_page("Spotify accounts on this speaker", body, status_msg=status_msg)


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
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict, *, status: int = 200) -> None:
            import json as _json
            body = _json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)

            if path == "/":
                self._render_index(status_msg=qs.get("msg", [""])[0])
                return

            if path == "/playlist-preview":
                self._handle_playlist_preview(qs)
                return

            if path == "/oauth-callback":
                self._handle_oauth_callback_get(qs)
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            form = _read_form(self)

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

            self.send_error(HTTPStatus.NOT_FOUND)

        # --- route bodies ---

        def _render_index(self, *, status_msg: str = "") -> None:
            if not cfg["client_id"]:
                self._send_html(_setup_wizard_html(status_msg=status_msg))
                return
            registry = Registry.load(cfg["registry_path"])
            redirect_uri = _redirect_uri_for_mode(cfg["mode"], cfg)
            if not registry.accounts:
                self._send_html(_redirect_uri_page_html(
                    redirect_uri, cfg["client_id"], cfg["mode"],
                    status_msg=status_msg,
                ))
                return
            self._send_html(_management_html(
                registry, redirect_uri, cfg["client_id"], cfg["mode"],
                status_msg=status_msg,
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
            _restart_voice_daemon()
            self._redirect(
                "./?msg=Credentials+saved.+Now+add+the+redirect+URL+to+your+Spotify+app."
            )

        def _handle_reset_credentials(self) -> None:
            _delete_creds_file()
            cfg["client_id"] = ""
            cfg["mode"] = "bounce"
            _restart_voice_daemon()
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
            _restart_voice_daemon()
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
                _restart_voice_daemon()
                self._redirect(f"./?msg=Removed+{urllib.parse.quote(name)}")
            else:
                self._redirect("./?msg=Account+not+found")

        def _handle_default(self, form: dict[str, str]) -> None:
            name = form.get("name", "")
            registry = Registry.load(cfg["registry_path"])
            if registry.get(name) is not None:
                registry.default_name = name
                registry.save()
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

    # Credentials may come from systemd's EnvironmentFile (legacy installs)
    # OR from the wizard-written file (the supported path on a fresh
    # install). Prefer env over file when both are set so manual
    # /etc/jasper/jasper.env edits still win.
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

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg: dict[str, Any] = {
        "client_id": client_id,
        "mode": mode,
        "bounce_redirect_uri": args.bounce_redirect_uri,
        "manual_redirect_uri": args.manual_redirect_uri,
        "registry_path": args.registry,
    }
    state = "configured" if client_id else "needs-setup"
    server = ThreadingHTTPServer((args.host, args.port), _make_handler(cfg))
    logger.info(
        "jasper-web listening on http://%s:%d (state=%s, mode=%s)",
        args.host, args.port, state, mode,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
