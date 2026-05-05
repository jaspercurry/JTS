"""Tiny self-contained HTTP service for adding household Spotify
accounts. Public surface: http://jasper.local/spotify/ — visible there
because nginx reverse-proxies /spotify/ → http://127.0.0.1:8765/.

Stack: stdlib http.server (no extra deps). One thread per request,
which is fine for a setup page that gets a handful of hits in its
lifetime. spotipy is the only third-party dep, already used by the
voice daemon.

Routes (paths the app sees AFTER nginx strips the /spotify/ prefix):
  GET  /                  list of accounts + form to add a new one
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
import sys
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


def _html(title: str, body: str, *, status_msg: str = "") -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         max-width: 560px; margin: 2em auto; padding: 0 1em; color: #222; }}
  h1 {{ margin-bottom: 0.25em; }} h2 {{ margin-top: 2em; }}
  .sub {{ color: #666; margin-top: 0; }}
  .msg {{ background: #e8f4ff; border: 1px solid #abd; padding: 0.6em 0.8em;
          border-radius: 6px; margin: 1em 0; }}
  .err {{ background: #ffe8e8; border-color: #d99; }}
  ul.accounts {{ list-style: none; padding: 0; }}
  ul.accounts li {{ background: #f4f4f4; padding: 0.6em 0.8em;
                    border-radius: 6px; margin-bottom: 0.4em;
                    display: flex; align-items: center; gap: 0.6em; }}
  ul.accounts li .name {{ font-weight: 600; flex: 1; }}
  ul.accounts li .pat {{ color: #666; font-size: 0.9em; }}
  ul.accounts li .badge {{ background: #4a8; color: white; padding: 0.1em 0.5em;
                            border-radius: 4px; font-size: 0.8em; }}
  form {{ margin-top: 1em; }}
  label {{ display: block; margin: 0.6em 0 0.2em; font-weight: 600; }}
  input[type=text] {{ width: 100%; padding: 0.5em; border: 1px solid #bbb;
                      border-radius: 4px; font-size: 1em; box-sizing: border-box; }}
  small {{ color: #666; }}
  button {{ background: #1db954; color: white; border: 0; padding: 0.6em 1.2em;
           border-radius: 4px; font-size: 1em; cursor: pointer; }}
  button.secondary {{ background: #888; }}
  button.danger {{ background: #d44; }}
  button:hover {{ filter: brightness(1.1); }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
{f'<p class="msg">{html.escape(status_msg)}</p>' if status_msg else ''}
{body}
</body>
</html>""".encode()


def _index_html(registry: Registry, *, status_msg: str = "") -> bytes:
    accounts_html = ""
    if registry.accounts:
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
    else:
        accounts_html = "<p class='sub'>No accounts yet — add the first household member's Spotify below.</p>"

    body = f"""
<p class="sub">Each household member links their own Spotify account once. The speaker identifies the active listener by cross-referencing the AirPlay-pushed track title with each account&apos;s currently-playing Spotify track — no per-device setup needed.</p>

<h2>Accounts</h2>
{accounts_html}

<h2>Add an account</h2>
<form method="post" action="start">
  <label for="name">Your name</label>
  <input id="name" name="name" type="text" required pattern="[a-zA-Z0-9_-]+"
         placeholder="brittany" autocapitalize="off" autocorrect="off">
  <small>Lowercase, no spaces. This is just an internal label — pick anything.</small>

  <p style="margin-top:1em">
    <button type="submit">Continue with Spotify →</button>
  </p>
  <small>You&apos;ll be sent to Spotify to log in once. The token stays on this speaker.</small>
</form>
"""
    return _html("Spotify accounts on this speaker", body, status_msg=status_msg)


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length).decode("utf-8") if length else ""
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}


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

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)

            if path == "/":
                registry = Registry.load(cfg["registry_path"])
                self._send_html(_index_html(
                    registry, status_msg=qs.get("msg", [""])[0],
                ))
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
                try:
                    self._exchange_code(state, code)
                except Exception as e:  # noqa: BLE001
                    logger.exception("oauth exchange failed")
                    self._redirect(f"./?msg=Auth+exchange+failed:+{urllib.parse.quote(str(e))}")
                    return
                self._redirect(f"./?msg=Linked+{urllib.parse.quote(state)}+successfully")
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            form = _read_form(self)

            if path == "/start":
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
                return

            if path == "/remove":
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
                    self._redirect(f"./?msg=Removed+{urllib.parse.quote(name)}")
                else:
                    self._redirect("./?msg=Account+not+found")
                return

            if path == "/default":
                name = form.get("name", "")
                registry = Registry.load(cfg["registry_path"])
                if registry.get(name) is not None:
                    registry.default_name = name
                    registry.save()
                    self._redirect(f"./?msg=Default+set+to+{urllib.parse.quote(name)}")
                else:
                    self._redirect("./?msg=Account+not+found")
                return

            self.send_error(HTTPStatus.NOT_FOUND)

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
            # Writes the token to cache_path as a side-effect.
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
        default=os.environ.get("SPOTIFY_REDIRECT_URI", "https://jasper.local/spotify/callback"),
    )
    args = parser.parse_args(argv)

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        sys.stderr.write("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set in the environment\n")
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": args.redirect_uri,
        "registry_path": args.registry,
    }
    server = ThreadingHTTPServer((args.host, args.port), _make_handler(cfg))
    logger.info(
        "jasper-web listening on http://%s:%d (redirect_uri=%s)",
        args.host, args.port, args.redirect_uri,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
