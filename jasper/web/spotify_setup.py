"""Tiny self-contained HTTP service for adding household Spotify
accounts. Public surface: https://jts.local/spotify/ — visible there
because nginx reverse-proxies /spotify/ → http://127.0.0.1:8765/.

Stack: stdlib http.server (no extra deps). One thread per request,
which is fine for a setup page that gets a handful of hits in its
lifetime. spotipy is the only third-party dep, already used by the
voice daemon.

Three states, single index page renders the appropriate one:
  1. No CLIENT_ID/SECRET → guided setup wizard (paste creds, link to
     Spotify Developer Dashboard for grabbing them).
  2. Creds set, no accounts → redirect-URI instructions (copy button +
     deep link to this app's settings page) + add-account form.
  3. Creds set, accounts exist → existing management UI.

Routes (paths the app sees AFTER nginx strips the /spotify/ prefix):
  GET  /                    state-aware setup/management UI
  POST /setup-credentials   save CLIENT_ID/SECRET, restart jasper-voice
  POST /reset-credentials   clear creds (back to state 1)
  POST /start               begin OAuth for `name` (303 redirect to Spotify)
  GET  /callback            OAuth callback — exchanges code, writes cache,
                            redirects back to /
  POST /remove              remove an account by name
  POST /default             change the default account
  GET  /playlist-preview    live-fetch a playlist's name from Spotify
                            (json {uri, name} or {error}); used by the
                            paste-URL field's debounced lookup
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

# Persisted CLIENT_ID/SECRET. Separate from /etc/jasper/jasper.env so the
# web service can write to it without needing /etc to be RW (systemd's
# ProtectSystem=full keeps /etc read-only). Both jasper-web and
# jasper-voice source this file via an optional EnvironmentFile so a
# restart picks up the values written here.
CREDS_FILE = "/var/lib/jasper/spotify_credentials.env"

# Spotify Developer App ID format: 32 lowercase hex characters. Used
# both for input validation and to build the deep link to the app's
# settings page.
_CLIENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _read_creds_file(path: str = CREDS_FILE) -> dict[str, str]:
    return read_env_file(path)


def _write_creds_file(client_id: str, client_secret: str, path: str = CREDS_FILE) -> None:
    write_env_file(path, {
        "SPOTIFY_CLIENT_ID": client_id,
        "SPOTIFY_CLIENT_SECRET": client_secret,
    })


def _delete_creds_file(path: str = CREDS_FILE) -> None:
    delete_env_file(path)


def _restart_voice_daemon() -> None:
    restart_voice_daemon()


# ============================================================
# HTML rendering
# ============================================================


# Page-style additions specific to the Spotify wizard. Shared base CSS
# (body, h1, buttons, card styles) lives in _common.PAGE_STYLE; the
# `<style>` block on each page concatenates the two. Anything used by
# multiple wizards belongs upstream in _common.
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
"""


def _wrap_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    """Spotify wizard page wrapper — uses the shared `wrap_page` for
    structure, then swaps in the style block extended with Spotify-
    specific selectors (.pl-section, etc.)."""
    page = wrap_page(title, body, status_msg=status_msg).decode()
    return page.replace(
        f"<style>{PAGE_STYLE}</style>",
        f"<style>{_SPOTIFY_PAGE_STYLE}</style>",
    ).encode()


def _setup_wizard_html(*, status_msg: str = "") -> bytes:
    """State 1: no CLIENT_ID/SECRET configured. Walk the user through
    creating a Spotify Developer App and pasting the credentials."""
    body = """
<p class="sub">Connect this speaker to Spotify. Takes about two minutes.</p>

<h2>Step 1: Create a Spotify Developer App</h2>
<p>If you don't already have one, create a new app on Spotify's developer dashboard. Any name and description is fine — this is just an identity for your speaker.</p>
<p><a class="btn" href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">Open Spotify Developer Dashboard ↗</a></p>
<p class="hint">After clicking <strong>Create app</strong>, you'll land on the app's overview page. The Client ID is shown immediately. Click <strong>View client secret</strong> to reveal the secret. Copy both.</p>

<h2>Step 2: Paste the credentials here</h2>
<form method="post" action="setup-credentials">
  <label for="client_id">Client ID</label>
  <input id="client_id" name="client_id" type="text" required
         autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
         placeholder="32 hex characters, e.g. 1a2b3c…">

  <label for="client_secret">Client Secret</label>
  <input id="client_secret" name="client_secret" type="password" required
         autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
         placeholder="32 hex characters">
  <small>Stored locally on the speaker only. Never sent anywhere except Spotify.</small>

  <p style="margin-top:1.4em">
    <button type="submit">Save credentials →</button>
  </p>
</form>
"""
    return _wrap_page("Set up Spotify on this speaker", body, status_msg=status_msg)


def _redirect_uri_section_html(redirect_uri: str, client_id: str) -> str:
    """The 'copy this URL, paste it into your Spotify app's redirect URIs'
    block. Shared between the post-credential page and the management
    page (so account-add is always reachable even after the user has
    moved past initial setup)."""
    dashboard_link = f"https://developer.spotify.com/dashboard/{html.escape(client_id)}"
    redirect_safe = html.escape(redirect_uri)
    return f"""
<h2>Add this redirect URL to your Spotify app</h2>
<p>Spotify needs to know where to send users back after they sign in. Paste this URL into your Developer App's <strong>Redirect URIs</strong> list.</p>

<div class="copy-row">
  <input id="redirect-uri" type="text" readonly value="{redirect_safe}"
         onclick="this.select();">
  <button type="button" onclick="copyRedirect()">Copy</button>
  <span id="copy-feedback" class="copy-feedback">Copied!</span>
</div>

<ol class="steps">
  <li>Click <strong>Open this app's settings ↗</strong> below — opens in a new tab.</li>
  <li>On Spotify's page, click <strong>Settings</strong> (or <strong>Edit</strong>), scroll to <strong>Redirect URIs</strong>, paste the URL above, click <strong>Add</strong>, then <strong>Save</strong> at the bottom.</li>
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


def _redirect_uri_page_html(redirect_uri: str, client_id: str, *, status_msg: str = "") -> bytes:
    """State 2: creds saved, no accounts yet. Show the redirect-URI
    setup steps prominently, then the add-account form."""
    masked = client_id[:4] + "…" + client_id[-4:] if len(client_id) > 8 else "configured"
    body = f"""
<p class="sub">Credentials saved (Client ID: <span class="credbox" style="display:inline-block; padding:0.05em 0.4em">{html.escape(masked)}</span>). Two steps left.</p>

{_redirect_uri_section_html(redirect_uri, client_id)}

{_add_account_form_html()}

<form method="post" action="reset-credentials" style="margin-top:3em"
      onsubmit="return confirm('Clear the saved Client ID and Secret? You\\'ll need to paste them again.');">
  <button type="submit" class="danger">Reset Spotify credentials</button>
</form>
"""
    return _wrap_page("Almost there — connect Spotify", body, status_msg=status_msg)


def _account_playlists_section_html(account: Account) -> str:
    """Per-account playlist subsection. Renders the existing rows + a
    hidden 'add' form. The page-level JS wires up live name preview as
    the user pastes, and the toggle for the add form."""
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
    """One <details> card per account: collapsed by default, expanded
    if it's the registry default (so the user always sees their primary
    state on landing). Action buttons live INSIDE the expanded body so
    they're not perpetual visual distractions on the index."""
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
  // On submit (or server-side fetch fails), page redirects with status
  // message — same pattern as the rest of this UI.
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


def _management_html(registry: Registry, redirect_uri: str, client_id: str, *, status_msg: str = "") -> bytes:
    """State 3: at least one account is OAuthed. Each account renders as
    an expand/collapse <details> card. The default account starts open
    so the user lands on their primary state without an extra click.
    The redirect-URI block stays accessible (collapsed in <details>) so
    the user can copy it again if they need to add the URL to a
    different app or re-add a removed one."""
    cards = []
    for a in registry.accounts:
        is_default = a.name == registry.default_name
        # Open the default by default, plus any account that has
        # playlists configured (so the user can see them at a glance).
        is_open = is_default or bool(a.playlists)
        cards.append(_account_card_html(a, is_default=is_default, is_open=is_open))

    body = f"""
<p class="sub">Each household member links their own Spotify account once. The speaker identifies the active listener by cross-referencing the AirPlay-pushed track title with each account's currently-playing Spotify track — no per-device setup needed.</p>

<h2>Accounts</h2>
<p class="accounts-help">Click an account to manage it. Custom playlists let you reach Spotify-owned algorithmic playlists (Discover Weekly, Daily Mix, Release Radar, Daylist) by voice — they're hidden from Spotify's search API, so paste the share link from Spotify desktop's right-click → Share → Copy link.</p>
{''.join(cards)}

{_add_account_form_html()}

<details style="margin-top:2.4em">
  <summary>Spotify app settings (redirect URI, reset credentials)</summary>
  {_redirect_uri_section_html(redirect_uri, client_id)}
  <form method="post" action="reset-credentials" style="margin-top:2em"
        onsubmit="return confirm('Clear the saved Client ID and Secret? Existing OAuthed accounts will keep working until their tokens expire.');">
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
    algorithmic personalised playlists (Discover Weekly, Release Radar,
    Daily Mix N, Daylist) — those URIs still PLAY via context_uri but
    Spotify's `/v1/playlists/{id}` endpoint refuses to enumerate them
    as of late 2024. The public HTML page renders fine and embeds the
    canonical name in OpenGraph metadata.

    Fragile by nature (HTML scrape) — only used as a fallback. Returns
    None on any error so the caller can surface a clean message."""
    import urllib.request
    url = f"https://open.spotify.com/playlist/{playlist_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_SHARE_PAGE_TIMEOUT_SEC) as r:
            # 50KB cap is plenty for the <head>; the page is multi-MB
            # of inlined React noise we don't need.
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
    """Try the Web API; fall back to the share-page scrape if Spotify
    returns 404. Other Spotify errors (auth, rate-limit, network) are
    re-raised so the caller can surface a real problem rather than
    silently scrape past it."""
    pid = playlist_id_from_uri(uri)
    if not pid:
        return None
    try:
        info = sp.playlist(pid, fields="name")
    except Exception as e:  # noqa: BLE001
        # spotipy raises spotipy.exceptions.SpotifyException; we don't
        # import it eagerly because the web module already keeps spotipy
        # imports lazy for testability.
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
    the cached token is missing/unusable. Used by the playlist preview
    + add endpoints, which need to call `sp.playlist()` server-side to
    fetch the canonical name from a pasted URL."""
    if not (cfg.get("client_id") and cfg.get("client_secret")):
        return None
    registry = Registry.load(cfg["registry_path"])
    account = registry.get(account_name)
    if account is None or not account.cache_path:
        return None
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError:
        logger.warning("spotipy not installed; playlist preview unavailable")
        return None
    try:
        auth = SpotifyOAuth(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            redirect_uri=cfg["redirect_uri"],
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


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Returns a request handler class closed over the config dict.

    `cfg` is mutated when the user submits credentials so the running
    process can serve the OAuth flow without needing a restart."""

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

            if path == "/callback":
                code = qs.get("code", [""])[0]
                state = qs.get("state", [""])[0]  # account name
                err = qs.get("error", [""])[0]
                if err:
                    self._redirect(f"./?msg=Spotify+returned+error:+{urllib.parse.quote(err)}")
                    return
                if not (code and state):
                    self._redirect("./?msg=Missing+code+or+state+from+Spotify")
                    return
                if not (cfg["client_id"] and cfg["client_secret"]):
                    self._redirect("./?msg=Credentials+were+cleared+mid-flow.+Start+over.")
                    return
                try:
                    self._exchange_code(state, code)
                except Exception as e:  # noqa: BLE001
                    logger.exception("oauth exchange failed")
                    self._redirect(f"./?msg=Auth+exchange+failed:+{urllib.parse.quote(str(e))}")
                    return
                _restart_voice_daemon()
                self._redirect(f"./?msg=Linked+{urllib.parse.quote(state)}+successfully")
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
            has_creds = bool(cfg["client_id"] and cfg["client_secret"])
            if not has_creds:
                self._send_html(_setup_wizard_html(status_msg=status_msg))
                return
            registry = Registry.load(cfg["registry_path"])
            if not registry.accounts:
                self._send_html(_redirect_uri_page_html(
                    cfg["redirect_uri"], cfg["client_id"], status_msg=status_msg,
                ))
                return
            self._send_html(_management_html(
                registry, cfg["redirect_uri"], cfg["client_id"],
                status_msg=status_msg,
            ))

        def _handle_setup_credentials(self, form: dict[str, str]) -> None:
            client_id = form.get("client_id", "").strip()
            client_secret = form.get("client_secret", "").strip()
            if not (client_id and client_secret):
                self._redirect("./?msg=Both+Client+ID+and+Client+Secret+are+required.")
                return
            if not _CLIENT_ID_RE.fullmatch(client_id):
                self._redirect("./?msg=Client+ID+should+be+32+lowercase+hex+characters.+Double-check+the+value+from+Spotify.")
                return
            try:
                _write_creds_file(client_id, client_secret)
            except OSError as e:
                logger.exception("could not write credentials file")
                self._redirect(f"./?msg=Could+not+save+credentials:+{urllib.parse.quote(str(e))}")
                return
            cfg["client_id"] = client_id
            cfg["client_secret"] = client_secret
            _restart_voice_daemon()
            self._redirect("./?msg=Credentials+saved.+Now+add+the+redirect+URL+to+your+Spotify+app.")

        def _handle_reset_credentials(self) -> None:
            _delete_creds_file()
            cfg["client_id"] = ""
            cfg["client_secret"] = ""
            _restart_voice_daemon()
            self._redirect("./?msg=Credentials+cleared.")

        def _handle_start(self, form: dict[str, str]) -> None:
            if not (cfg["client_id"] and cfg["client_secret"]):
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
            from spotipy.oauth2 import SpotifyOAuth
            auth = SpotifyOAuth(
                client_id=cfg["client_id"],
                client_secret=cfg["client_secret"],
                redirect_uri=cfg["redirect_uri"],
                scope=SPOTIFY_SCOPE,
                cache_path=cache_path,
                state=name,
                open_browser=False,
            )
            self._redirect(auth.get_authorize_url())

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
            """Live-preview a pasted Spotify URL/URI. Returns
            `{"uri": ..., "name": ...}` on success or `{"error": ...}`.
            Pure read; does not mutate the registry."""
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

        def _exchange_code(self, account_name: str, code: str) -> None:
            registry = Registry.load(cfg["registry_path"])
            account = registry.get(account_name)
            if account is None:
                raise RuntimeError(f"unknown account: {account_name}")
            from spotipy.oauth2 import SpotifyOAuth
            os.makedirs(os.path.dirname(account.cache_path), exist_ok=True)
            auth = SpotifyOAuth(
                client_id=cfg["client_id"],
                client_secret=cfg["client_secret"],
                redirect_uri=cfg["redirect_uri"],
                scope=SPOTIFY_SCOPE,
                cache_path=account.cache_path,
                state=account_name,
                open_browser=False,
            )
            auth.get_access_token(code, as_dict=False, check_cache=False)

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
    parser.add_argument(
        "--redirect-uri",
        default=os.environ.get("SPOTIFY_REDIRECT_URI", "https://jts.local/spotify/callback"),
    )
    args = parser.parse_args(argv)

    # Credentials may come from systemd's EnvironmentFile (for installs
    # provisioned the old way) OR from the wizard-written file (the
    # supported path on a fresh install). Prefer env over file when both
    # are set so manual /etc/jasper/jasper.env edits still win.
    creds_from_file = _read_creds_file()
    client_id = (
        os.environ.get("SPOTIFY_CLIENT_ID", "")
        or creds_from_file.get("SPOTIFY_CLIENT_ID", "")
    )
    client_secret = (
        os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        or creds_from_file.get("SPOTIFY_CLIENT_SECRET", "")
    )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": args.redirect_uri,
        "registry_path": args.registry,
    }
    state = "configured" if (client_id and client_secret) else "needs-setup"
    server = ThreadingHTTPServer((args.host, args.port), _make_handler(cfg))
    logger.info(
        "jasper-web listening on http://%s:%d (state=%s, redirect_uri=%s)",
        args.host, args.port, state, args.redirect_uri,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
