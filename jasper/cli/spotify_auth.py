# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One-time Spotify OAuth bootstrap for headless Pi (CLI flavour).

Most users go through the web wizard at http://jts.local/spotify/. This
CLI is the fallback for ops scenarios where there's no browser-on-the-Pi
path: SSH-only setups, scripted reinstalls, etc.

Flow: PKCE Authorization Code with manual paste-back.

Run once after install: `jasper-spotify-auth`. Prints the Spotify
authorize URL; the user opens it on any device, grants permissions,
gets redirected to http://127.0.0.1:8888/callback?code=... (which fails
to load — that's expected; the user's browser isn't on the Pi). They
copy the full URL from the address bar and paste it back here. spotipy
parses the code out, exchanges it for tokens via PKCE (no client
secret needed), and caches the refresh token at SPOTIFY_CACHE_PATH.
After that the daemon refreshes silently forever.
"""
from __future__ import annotations

import sys

from spotipy.oauth2 import SpotifyPKCE

from ..config import Config
from ..spotify_router import SPOTIFY_SCOPE


def main() -> None:
    cfg = Config.from_env()
    if not cfg.spotify_enabled:
        print(
            "SPOTIFY_CLIENT_ID must be set in /etc/jasper/jasper.env "
            "before running this command. (PKCE — no client secret needed.)",
            file=sys.stderr,
        )
        sys.exit(2)

    if not cfg.spotify_redirect_uri.startswith("http://127.0.0.1"):
        print(
            "WARNING: this CLI uses the manual paste-back flow, which "
            "expects a loopback IP redirect URI. Spotify rejects "
            "`localhost` and any LAN IP / .local hostname; only "
            "`http://127.0.0.1:PORT/...` is accepted as the loopback "
            f"exception. Current SPOTIFY_REDIRECT_URI: {cfg.spotify_redirect_uri}",
            file=sys.stderr,
        )

    auth = SpotifyPKCE(
        client_id=cfg.spotify_client_id,
        redirect_uri=cfg.spotify_redirect_uri,
        scope=SPOTIFY_SCOPE,
        cache_path=cfg.spotify_cache_path,
        open_browser=False,
    )

    print()
    print("Spotify OAuth (one-time, headless flow, PKCE)")
    print("=" * 60)
    print()
    print("1. Open this URL on your phone or laptop:")
    print()
    print(f"   {auth.get_authorize_url()}")
    print()
    print("2. Sign in and grant access.")
    print("3. Your browser will be redirected to a URL that fails to load:")
    print(f"     {cfg.spotify_redirect_uri}?code=AQ...&state=...")
    print("   That failure is expected — copy the FULL URL from the address")
    print("   bar (must include `?code=...`).")
    print()
    print("4. Paste the full redirect URL here and hit Enter:")
    print()

    pasted = input("Redirect URL: ").strip()
    code = auth.parse_response_code(pasted)
    if not code or code == pasted:
        print("Could not parse a `code` parameter from that URL.", file=sys.stderr)
        sys.exit(1)

    auth.get_access_token(code)
    print()
    print(f"Refresh token cached at {cfg.spotify_cache_path}.")
    print("The voice daemon will refresh access silently from here on.")


if __name__ == "__main__":
    main()
