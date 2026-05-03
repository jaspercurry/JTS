"""One-time Spotify OAuth bootstrap for headless Pi.

Run once after install: `jasper-spotify-auth`. Prints the Spotify authorize
URL; the user opens it on their phone, grants permissions, gets redirected
to http://127.0.0.1:8765/callback?code=... (which fails to load — that's
fine), copies the full URL from the phone's address bar, pastes it back
here. spotipy parses the code out, exchanges it for tokens, and caches the
refresh token at SPOTIFY_CACHE_PATH. After that the daemon refreshes
silently forever (until the user revokes access).
"""
from __future__ import annotations

import sys

from spotipy.oauth2 import SpotifyOAuth

from ..config import Config
from ..tools.spotify import SPOTIFY_SCOPE


def main() -> None:
    cfg = Config.from_env()
    if not cfg.spotify_enabled:
        print(
            "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set in "
            "/etc/jasper/jasper.env before running this command.",
            file=sys.stderr,
        )
        sys.exit(2)

    if not cfg.spotify_redirect_uri.startswith("http://127.0.0.1"):
        print(
            "WARNING: Spotify (since 2025-04-09) only accepts loopback "
            "redirect URIs as `http://127.0.0.1:PORT/...` — `localhost` is "
            "rejected. Current SPOTIFY_REDIRECT_URI: "
            f"{cfg.spotify_redirect_uri}",
            file=sys.stderr,
        )

    auth = SpotifyOAuth(
        client_id=cfg.spotify_client_id,
        client_secret=cfg.spotify_client_secret,
        redirect_uri=cfg.spotify_redirect_uri,
        scope=SPOTIFY_SCOPE,
        cache_path=cfg.spotify_cache_path,
        open_browser=False,
    )

    print()
    print("Spotify OAuth (one-time, headless flow)")
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

    auth.get_access_token(code, as_dict=False)
    print()
    print(f"Refresh token cached at {cfg.spotify_cache_path}.")
    print("The voice daemon will refresh access silently from here on.")


if __name__ == "__main__":
    main()
