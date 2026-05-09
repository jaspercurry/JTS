"""Google OAuth setup wizard at /google/.

Multi-account, household-aware — mirrors `jasper.web.spotify_setup` but
simpler (no playlist management, no transport-routing logic; just paste
CLIENT_ID/SECRET, OAuth each member, manage defaults).

Three states, single index page renders the appropriate one:
  1. No CLIENT_ID/SECRET → guided setup wizard (paste creds, link to
     Google Cloud Console).
  2. Creds set, no accounts → redirect-URI instructions + add-account
     form.
  3. Creds set, accounts exist → management UI (list, default, remove,
     add more).

Routes (paths the app sees AFTER nginx strips the /google/ prefix):
  GET  /                    state-aware setup/management UI
  POST /setup-credentials   save CLIENT_ID/SECRET, restart jasper-voice
  POST /reset-credentials   clear creds (back to state 1)
  POST /start               begin OAuth for `name` (303 redirect to
                            Google's authorize endpoint)
  GET  /callback            OAuth callback — exchange code, write
                            token JSON, hit OIDC userinfo for email +
                            display name, redirect back to /
  POST /remove              remove an account by name
  POST /default             change the default account
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..google_creds import (
    GOOGLE_SCOPES,
    GoogleAccount,
    GoogleRegistry,
    default_token_path_for,
    save_token,
)
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


# Persisted CLIENT_ID/SECRET. Same shape as spotify_credentials.env so
# the systemd unit picks both up via `EnvironmentFile=`. Mode 0600 by
# write_env_file's default.
CREDS_FILE = "/var/lib/jasper/google_credentials.env"

# Google OAuth client IDs end in `.apps.googleusercontent.com`.
# Loose pattern — Google has changed the leading numeric chunk's
# length over time, and a strict regex would reject perfectly-valid
# IDs the next time the format shifts.
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$")

_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"


# ----------------------------------------------------------------------
# Persistence helpers — env file + token writes.
# ----------------------------------------------------------------------


def _read_creds_file(path: str = CREDS_FILE) -> dict[str, str]:
    return read_env_file(path)


def _write_creds_file(client_id: str, client_secret: str, *, path: str = CREDS_FILE) -> None:
    write_env_file(path, {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
    })


def _delete_creds_file(path: str = CREDS_FILE) -> None:
    delete_env_file(path)


def _restart_voice_daemon() -> None:
    restart_voice_daemon()


# ----------------------------------------------------------------------
# HTML rendering.
# ----------------------------------------------------------------------


_GOOGLE_PAGE_STYLE = PAGE_STYLE + """
  /* Account list — same shape as Spotify's account cards but
     simpler (no inline playlist management). */
  ul.accounts { list-style: none; padding: 0; }
  ul.accounts li {
    background: #f4f4f4; padding: 0.7em 0.9em;
    border-radius: 6px; margin-bottom: 0.5em;
    display: flex; align-items: center; gap: 0.6em;
    flex-wrap: wrap;
  }
  ul.accounts li .name { font-weight: 600; }
  ul.accounts li .email {
    color: #666; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.9em;
  }
  ul.accounts li .badge {
    background: #4a8; color: white; padding: 0.1em 0.5em;
    border-radius: 4px; font-size: 0.8em;
  }
  ul.accounts li .actions {
    margin-left: auto; display: flex; gap: 0.4em;
  }
  ul.accounts li .actions form { margin: 0; }
  ul.accounts li .actions button {
    padding: 0.3em 0.7em; font-size: 0.85em;
  }
"""


def _wrap_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    page = wrap_page(title, body, status_msg=status_msg).decode()
    return page.replace(
        f"<style>{PAGE_STYLE}</style>",
        f"<style>{_GOOGLE_PAGE_STYLE}</style>",
    ).encode()


def _setup_wizard_html(*, status_msg: str = "") -> bytes:
    """State 1: no CLIENT_ID/SECRET. Walk through creating a Google
    Cloud Console OAuth client + pasting credentials."""
    body = """
<p class="sub">Connect this speaker to Google Calendar and Gmail. Takes about three minutes.</p>

<h2>Step 1: Create a Google Cloud OAuth client</h2>
<p>If you don't already have one, create a new OAuth 2.0 Client ID for a <strong>Web application</strong> in the Google Cloud Console. Any project name and app name is fine — this is just an identity for your speaker.</p>
<p><a class="btn" href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">Open Google Cloud Console ↗</a></p>
<p class="hint">After creating the client you'll see the Client ID and Client Secret. You can come back to this page anytime to paste them.</p>

<h2>Step 2: Enable the Calendar and Gmail APIs</h2>
<p>The OAuth client also needs the relevant APIs turned on for your project.</p>
<ul>
  <li><a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" target="_blank" rel="noopener">Enable Google Calendar API ↗</a></li>
  <li><a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener">Enable Gmail API ↗</a></li>
</ul>

<h2>Step 3: Paste the credentials here</h2>
<form method="post" action="setup-credentials">
  <label for="client_id">Client ID</label>
  <input id="client_id" name="client_id" type="text" required
         autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
         placeholder="123456789012-abc….apps.googleusercontent.com">

  <label for="client_secret">Client Secret</label>
  <input id="client_secret" name="client_secret" type="password" required
         autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
         placeholder="GOCSPX-…">
  <small>Stored locally on the speaker only. Never sent anywhere except Google.</small>

  <p style="margin-top:1.4em">
    <button type="submit">Save credentials →</button>
  </p>
</form>
"""
    return _wrap_page(
        "Set up Google on this speaker", body, status_msg=status_msg,
    )


def _redirect_uri_section_html(redirect_uri: str) -> str:
    """The 'add this redirect URL to your OAuth client' block.
    Shared between state 2 and state 3 (so it's always reachable)."""
    redirect_safe = html.escape(redirect_uri)
    return f"""
<h2>Add this redirect URL to your OAuth client</h2>
<p>Google needs to know where to send the user back after sign-in. Paste this URL into your OAuth client's <strong>Authorized redirect URIs</strong> list.</p>

<div class="copy-row">
  <input id="redirect-uri" type="text" readonly value="{redirect_safe}"
         onclick="this.select();">
  <button type="button" onclick="copyRedirect()">Copy</button>
  <span id="copy-feedback" class="copy-feedback">Copied!</span>
</div>

<ol class="steps">
  <li>Open <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">Credentials in the Google Cloud Console ↗</a></li>
  <li>Click your OAuth 2.0 Client ID (the Web application one).</li>
  <li>Under <strong>Authorized redirect URIs</strong>, click <strong>Add URI</strong>, paste the URL above, then <strong>Save</strong>.</li>
</ol>

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
<h2>Add a Google account</h2>
<form method="post" action="start">
  <label for="name">Your name (label only)</label>
  <input id="name" name="name" type="text" required pattern="[a-zA-Z0-9_-]+"
         placeholder="brittany" autocapitalize="off" autocorrect="off">
  <small>Lowercase, no spaces. Used by voice ('what's on Brittany's calendar') and shown in the list below.</small>

  <p style="margin-top:1em">
    <button type="submit">Continue with Google →</button>
  </p>
  <small>You'll be sent to Google to sign in once. The refresh token stays on this speaker — read access only (Calendar + Gmail).</small>
</form>
"""


def _redirect_uri_page_html(redirect_uri: str, client_id: str, *, status_msg: str = "") -> bytes:
    masked = (
        client_id[:8] + "…" + client_id[-30:]
        if len(client_id) > 38 else "configured"
    )
    body = f"""
<p class="sub">Credentials saved (Client ID: <span class="credbox" style="display:inline-block; padding:0.05em 0.4em">{html.escape(masked)}</span>). Two steps left.</p>

{_redirect_uri_section_html(redirect_uri)}

{_add_account_form_html()}

<form method="post" action="reset-credentials" style="margin-top:3em"
      onsubmit="return confirm('Clear the saved Client ID and Secret? You\\'ll need to paste them again.');">
  <button type="submit" class="danger">Reset Google credentials</button>
</form>
"""
    return _wrap_page(
        "Almost there — connect Google", body, status_msg=status_msg,
    )


def _account_li_html(account: GoogleAccount, *, is_default: bool) -> str:
    name = html.escape(account.name)
    email = html.escape(account.email or "(unknown email)")
    badge = '<span class="badge">default</span>' if is_default else ""
    set_default = (
        '<button class="secondary" type="submit" disabled>Default</button>'
        if is_default
        else '<button class="secondary" type="submit">Set default</button>'
    )
    return f"""
<li>
  <span class="name">{name}</span>
  <span class="email">{email}</span>
  {badge}
  <span class="actions">
    <form method="post" action="default">
      <input type="hidden" name="name" value="{name}">
      {set_default}
    </form>
    <form method="post" action="remove"
          onsubmit="return confirm('Remove {name}? The refresh token will be deleted from this speaker.');">
      <input type="hidden" name="name" value="{name}">
      <button class="danger" type="submit">Remove</button>
    </form>
  </span>
</li>"""


def _management_html(
    registry: GoogleRegistry, redirect_uri: str, *, status_msg: str = "",
) -> bytes:
    items = [
        _account_li_html(a, is_default=(a.name == registry.default_name))
        for a in registry.accounts
    ]
    body = f"""
<p class="sub">Each household member links their Google account once. The voice loop reads Calendar + Gmail data per-account on demand — say "what's on Brittany's calendar" or "any new emails for Jasper" to disambiguate; bare requests use the default account.</p>

<h2>Linked accounts</h2>
<ul class="accounts">
{''.join(items)}
</ul>

{_add_account_form_html()}

<details style="margin-top:2.4em">
  <summary>OAuth client settings (redirect URI, reset credentials)</summary>
  {_redirect_uri_section_html(redirect_uri)}
  <form method="post" action="reset-credentials" style="margin-top:2em"
        onsubmit="return confirm('Clear the saved Client ID and Secret? Existing OAuthed accounts will keep working until their refresh tokens are revoked.');">
    <button type="submit" class="danger">Reset Google credentials</button>
  </form>
</details>
"""
    return _wrap_page(
        "Google accounts on this speaker", body, status_msg=status_msg,
    )


# ----------------------------------------------------------------------
# OAuth helpers.
# ----------------------------------------------------------------------


def _build_flow(cfg: dict[str, Any], *, state: str | None = None):
    """Construct a google_auth_oauthlib Flow with our Web-application
    client config. Imported lazily so the module is importable in
    unit tests without the google-auth-oauthlib wheel installed."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "auth_uri": _AUTH_URI,
                "token_uri": _TOKEN_URI,
                "redirect_uris": [cfg["redirect_uri"]],
            }
        },
        scopes=GOOGLE_SCOPES,
        state=state,
    )
    flow.redirect_uri = cfg["redirect_uri"]
    return flow


def _fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Hit Google's OIDC userinfo endpoint with the freshly-issued
    access token. Returns ``{}`` on any error — the wizard falls back
    to "(unknown email)" rather than failing the whole link."""
    if not access_token:
        return {}
    req = urllib.request.Request(
        _USERINFO_URI,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("userinfo fetch failed: %s", e)
        return {}


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


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
                    self._redirect(
                        f"./?msg=Google+returned+error:+{urllib.parse.quote(err)}"
                    )
                    return
                if not (code and state):
                    self._redirect("./?msg=Missing+code+or+state+from+Google")
                    return
                if not (cfg["client_id"] and cfg["client_secret"]):
                    self._redirect(
                        "./?msg=Credentials+were+cleared+mid-flow.+Start+over."
                    )
                    return
                try:
                    self._exchange_code(state, code)
                except Exception as e:  # noqa: BLE001
                    logger.exception("oauth exchange failed")
                    self._redirect(
                        f"./?msg=Auth+exchange+failed:+{urllib.parse.quote(str(e))}"
                    )
                    return
                _restart_voice_daemon()
                self._redirect(
                    f"./?msg=Linked+{urllib.parse.quote(state)}+successfully"
                )
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            form = read_form(self)

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
            registry = GoogleRegistry.load(cfg["registry_path"])
            if not registry.accounts:
                self._send_html(_redirect_uri_page_html(
                    cfg["redirect_uri"], cfg["client_id"],
                    status_msg=status_msg,
                ))
                return
            self._send_html(_management_html(
                registry, cfg["redirect_uri"], status_msg=status_msg,
            ))

        def _handle_setup_credentials(self, form: dict[str, str]) -> None:
            client_id = form.get("client_id", "").strip()
            client_secret = form.get("client_secret", "").strip()
            if not (client_id and client_secret):
                self._redirect(
                    "./?msg=Both+Client+ID+and+Client+Secret+are+required."
                )
                return
            if not _CLIENT_ID_RE.fullmatch(client_id):
                self._redirect(
                    "./?msg=Client+ID+should+end+in+.apps.googleusercontent.com"
                    "+-+double-check+the+value+from+Google+Cloud+Console."
                )
                return
            try:
                _write_creds_file(client_id, client_secret)
            except OSError as e:
                logger.exception("could not write credentials file")
                self._redirect(
                    f"./?msg=Could+not+save+credentials:+{urllib.parse.quote(str(e))}"
                )
                return
            cfg["client_id"] = client_id
            cfg["client_secret"] = client_secret
            _restart_voice_daemon()
            self._redirect(
                "./?msg=Credentials+saved.+Now+add+the+redirect+URL+to+your+"
                "OAuth+client."
            )

        def _handle_reset_credentials(self) -> None:
            _delete_creds_file()
            cfg["client_id"] = ""
            cfg["client_secret"] = ""
            _restart_voice_daemon()
            self._redirect("./?msg=Credentials+cleared.")

        def _handle_start(self, form: dict[str, str]) -> None:
            if not (cfg["client_id"] and cfg["client_secret"]):
                self._redirect("./?msg=Set+up+Google+credentials+first.")
                return
            name = form.get("name", "").strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                self._redirect(
                    "./?msg=Invalid+name+(letters/digits/_-+only)"
                )
                return
            registry = GoogleRegistry.load(cfg["registry_path"])
            token_path = default_token_path_for(name)
            registry.add_or_update(GoogleAccount(name=name, token_path=token_path))
            registry.save()
            try:
                flow = _build_flow(cfg, state=name)
                # `prompt='consent'` forces Google to issue a refresh
                # token even if the user has already consented — the
                # one we get on first consent is the only one we'll
                # ever see otherwise, and a re-link would silently
                # fail.
                auth_url, _state = flow.authorization_url(
                    access_type="offline",
                    prompt="consent",
                    include_granted_scopes="true",
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("authorize-url build failed")
                self._redirect(
                    f"./?msg=Could+not+start+OAuth:+{urllib.parse.quote(str(e))}"
                )
                return
            self._redirect(auth_url)

        def _handle_remove(self, form: dict[str, str]) -> None:
            name = form.get("name", "")
            registry = GoogleRegistry.load(cfg["registry_path"])
            token_path = ""
            a = registry.get(name)
            if a is not None:
                token_path = a.token_path
            if registry.remove(name):
                registry.save()
                if token_path and os.path.isfile(token_path):
                    try:
                        os.unlink(token_path)
                    except OSError:
                        pass
                _restart_voice_daemon()
                self._redirect(f"./?msg=Removed+{urllib.parse.quote(name)}")
            else:
                self._redirect("./?msg=Account+not+found")

        def _handle_default(self, form: dict[str, str]) -> None:
            name = form.get("name", "")
            registry = GoogleRegistry.load(cfg["registry_path"])
            if registry.get(name) is not None:
                registry.default_name = name
                registry.save()
                self._redirect(
                    f"./?msg=Default+set+to+{urllib.parse.quote(name)}"
                )
            else:
                self._redirect("./?msg=Account+not+found")

        def _exchange_code(self, account_name: str, code: str) -> None:
            registry = GoogleRegistry.load(cfg["registry_path"])
            account = registry.get(account_name)
            if account is None:
                raise RuntimeError(f"unknown account: {account_name}")
            flow = _build_flow(cfg, state=account_name)
            flow.fetch_token(code=code)
            creds = flow.credentials
            if not creds.refresh_token:
                # `prompt='consent'` should always produce one. If we
                # get here Google probably rate-limited the consent
                # screen for this user — the user can retry.
                raise RuntimeError(
                    "Google did not return a refresh token. Try again "
                    "in a moment, or revoke this app at "
                    "myaccount.google.com/permissions and re-link."
                )
            save_token(
                account.token_path,
                refresh_token=creds.refresh_token,
                scopes=list(creds.scopes or GOOGLE_SCOPES),
                token_uri=creds.token_uri or _TOKEN_URI,
            )
            # Best-effort identity lookup so the management page can
            # show "jasper@gmail.com" next to the label. Failure here
            # is non-fatal — the account still works for tools.
            info = _fetch_userinfo(creds.token or "")
            email = (info.get("email") or "").strip()
            display = (info.get("name") or "").strip()
            if email or display:
                account.email = email
                account.display_name = display
                registry.save()

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(
    host: str,
    port: int,
    *,
    registry_path: str = "/var/lib/jasper/google/accounts.json",
    redirect_uri: str = "https://jts.local/google/callback",
) -> ThreadingHTTPServer:
    """Build a configured ThreadingHTTPServer. Used by both the
    standalone CLI entry point AND by jasper.web.__main__ to colocate
    this server with the Spotify + voice wizards inside one process."""
    creds_from_file = _read_creds_file()
    client_id = (
        os.environ.get("GOOGLE_CLIENT_ID", "")
        or creds_from_file.get("GOOGLE_CLIENT_ID", "")
    )
    client_secret = (
        os.environ.get("GOOGLE_CLIENT_SECRET", "")
        or creds_from_file.get("GOOGLE_CLIENT_SECRET", "")
    )
    cfg = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "registry_path": registry_path,
    }
    return ThreadingHTTPServer((host, port), _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-google-web",
        description=(
            "Google Calendar + Gmail OAuth setup web server "
            "for the Jasper smart speaker"
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_GOOGLE_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_GOOGLE_WEB_PORT", "8768")),
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get(
            "JASPER_GOOGLE_ACCOUNTS_PATH",
            "/var/lib/jasper/google/accounts.json",
        ),
    )
    parser.add_argument(
        "--redirect-uri",
        default=os.environ.get(
            "GOOGLE_REDIRECT_URI",
            "https://jts.local/google/callback",
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(
        args.host, args.port,
        registry_path=args.registry,
        redirect_uri=args.redirect_uri,
    )
    logger.info(
        "jasper-google-web listening on http://%s:%d (state=%s, redirect_uri=%s)",
        args.host, args.port, args.registry, args.redirect_uri,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
