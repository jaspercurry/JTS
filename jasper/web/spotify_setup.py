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
  GET  /                  state-aware setup/management UI
  POST /setup-credentials save CLIENT_ID/SECRET, restart jasper-voice
  POST /reset-credentials clear creds (back to state 1)
  POST /start             begin OAuth for `name` (303 redirect to Spotify)
  GET  /callback          OAuth callback — exchanges code, writes cache,
                          redirects back to /
  POST /remove            remove an account by name
  POST /default           change the default account
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import re
import subprocess
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
    """Parse a systemd-style EnvironmentFile (KEY=VALUE per line, no
    quoting). Returns {} if the file is missing or unreadable."""
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


def _write_creds_file(client_id: str, client_secret: str, path: str = CREDS_FILE) -> None:
    """Atomically write the credentials file with mode 0600. The
    secret never lives at world-readable mode on disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"SPOTIFY_CLIENT_ID={client_id}\n")
            f.write(f"SPOTIFY_CLIENT_SECRET={client_secret}\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _delete_creds_file(path: str = CREDS_FILE) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not delete %s: %s", path, e)


def _restart_voice_daemon() -> None:
    """Best-effort restart of jasper-voice so it picks up the new
    CLIENT_ID/SECRET (or new accounts) on its next boot. Logs but
    does not raise — the user can always restart by hand if this
    fails."""
    try:
        subprocess.run(
            ["systemctl", "restart", "jasper-voice"],
            check=False, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("jasper-voice restart failed: %s", e)


# ============================================================
# HTML rendering
# ============================================================


_PAGE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         max-width: 620px; margin: 2em auto; padding: 0 1em; color: #222; }
  h1 { margin-bottom: 0.25em; } h2 { margin-top: 2em; }
  .sub { color: #666; margin-top: 0; }
  .msg { background: #e8f4ff; border: 1px solid #abd; padding: 0.6em 0.8em;
          border-radius: 6px; margin: 1em 0; }
  .err { background: #ffe8e8; border-color: #d99; }
  ol.steps { padding-left: 1.4em; }
  ol.steps > li { margin-bottom: 1em; }
  ul.accounts { list-style: none; padding: 0; }
  ul.accounts li { background: #f4f4f4; padding: 0.6em 0.8em;
                    border-radius: 6px; margin-bottom: 0.4em;
                    display: flex; align-items: center; gap: 0.6em; }
  ul.accounts li .name { font-weight: 600; flex: 1; }
  ul.accounts li .badge { background: #4a8; color: white; padding: 0.1em 0.5em;
                            border-radius: 4px; font-size: 0.8em; }
  form { margin-top: 1em; }
  label { display: block; margin: 0.6em 0 0.2em; font-weight: 600; }
  input[type=text], input[type=password] {
    width: 100%; padding: 0.5em; border: 1px solid #bbb;
    border-radius: 4px; font-size: 1em; box-sizing: border-box;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
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
"""


def _wrap_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    msg_class = "msg err" if "error" in status_msg.lower() or "fail" in status_msg.lower() else "msg"
    msg_html = f'<p class="{msg_class}">{html.escape(status_msg)}</p>' if status_msg else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
{msg_html}
{body}
</body>
</html>""".encode()


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


def _management_html(registry: Registry, redirect_uri: str, client_id: str, *, status_msg: str = "") -> bytes:
    """State 3: at least one account is OAuthed. Show the existing
    management UI. The redirect-URI block stays accessible (collapsed
    in <details>) so the user can copy it again if they need to add
    the URL to a different app or re-add a removed one."""
    items = []
    for a in registry.accounts:
        is_default = a.name == registry.default_name
        items.append(f"""
          <li>
            <span class="name">{html.escape(a.name)}</span>
            {'<span class="badge">default</span>' if is_default else ''}
            <form method="post" action="default" style="margin:0">
              <input type="hidden" name="name" value="{html.escape(a.name)}">
              <button class="secondary" type="submit" {'disabled' if is_default else ''}>Set default</button>
            </form>
            <form method="post" action="remove" style="margin:0"
                  onsubmit="return confirm('Remove {html.escape(a.name)}?');">
              <input type="hidden" name="name" value="{html.escape(a.name)}">
              <button class="danger" type="submit">Remove</button>
            </form>
          </li>""")
    accounts_html = f"<ul class='accounts'>{''.join(items)}</ul>"

    body = f"""
<p class="sub">Each household member links their own Spotify account once. The speaker identifies the active listener by cross-referencing the AirPlay-pushed track title with each account's currently-playing Spotify track — no per-device setup needed.</p>

<h2>Accounts</h2>
{accounts_html}

{_add_account_form_html()}

<details style="margin-top:2.4em">
  <summary>Spotify app settings (redirect URI, reset credentials)</summary>
  {_redirect_uri_section_html(redirect_uri, client_id)}
  <form method="post" action="reset-credentials" style="margin-top:2em"
        onsubmit="return confirm('Clear the saved Client ID and Secret? Existing OAuthed accounts will keep working until their tokens expire.');">
    <button type="submit" class="danger">Reset Spotify credentials</button>
  </form>
</details>
"""
    return _wrap_page("Spotify accounts on this speaker", body, status_msg=status_msg)


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length).decode("utf-8") if length else ""
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}


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

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)

            if path == "/":
                self._render_index(status_msg=qs.get("msg", [""])[0])
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
