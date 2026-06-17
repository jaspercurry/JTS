"""One-time Google OAuth bootstrap CLI fallback for headless setup.

The web wizard at http://jts.local/google is the supported path. This
CLI exists as a fallback for headless / scripted installs (mirroring
``jasper-spotify-auth``) — paste the auth URL into a browser on
another device, sign in, paste the redirected URL back here.

Run: ``jasper-google-auth <name>`` where ``<name>`` is the household-
member label (``jasper``, ``brittany``). Idempotent — re-running
overwrites the previous refresh token for that label.
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse

from .. import env_load
from ..config import Config
from ..google_creds import (
    GOOGLE_SCOPES,
    GoogleAccount,
    GoogleRegistry,
    default_token_path_for,
    save_token,
)


_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def main() -> None:
    env_load.load_env_files()
    parser = argparse.ArgumentParser(
        prog="jasper-google-auth",
        description=(
            "Headless Google OAuth bootstrap. Prefer the web wizard at "
            "http://jts.local/google when a browser is reachable on "
            "the LAN — this CLI exists for scripted installs and "
            "remote-shell setup."
        ),
    )
    parser.add_argument(
        "name",
        help=(
            "Household-member label for this account (lowercase, no spaces)."
            " Used by voice ('what's on Brittany's calendar')."
        ),
    )
    parser.add_argument(
        "--make-default", action="store_true",
        help=(
            "Mark this account as the default — the one used when "
            "voice queries don't name a person."
        ),
    )
    args = parser.parse_args()

    if not _NAME_RE.fullmatch(args.name):
        print(
            "name must be letters/digits/_/- only.",
            file=sys.stderr,
        )
        sys.exit(2)

    cfg = Config.from_env()
    if not cfg.google_enabled:
        print(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set (in "
            "/etc/jasper/jasper.env or /var/lib/jasper-secrets/google_credentials.env)"
            " before running this command. Use the web wizard at "
            f"{cfg.google_setup_url} to paste them, or set them by hand.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        print(
            "google-auth-oauthlib is not installed. Run "
            "`pip install google-auth-oauthlib` (or re-run install.sh).",
            file=sys.stderr,
        )
        sys.exit(1)

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": cfg.google_client_id,
                "client_secret": cfg.google_client_secret,
                "auth_uri": _AUTH_URI,
                "token_uri": _TOKEN_URI,
                "redirect_uris": [cfg.google_redirect_uri],
            }
        },
        scopes=GOOGLE_SCOPES,
        state=args.name,
    )
    flow.redirect_uri = cfg.google_redirect_uri
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    print()
    print(f"Google OAuth (headless flow for account {args.name!r})")
    print("=" * 60)
    print()
    print("1. Open this URL on your phone or laptop:")
    print()
    print(f"   {auth_url}")
    print()
    print("2. Sign in and grant access (Calendar + Gmail read-only).")
    print("3. Your browser will be redirected to a URL that fails to load:")
    print(f"     {cfg.google_redirect_uri}?code=...&state={args.name}")
    print("   That failure is expected — copy the FULL URL from the address")
    print("   bar (must include `?code=...`).")
    print()
    print("4. Paste the full redirect URL here and hit Enter:")
    print()

    pasted = input("Redirect URL: ").strip()
    code = _extract_code(pasted)
    if not code:
        print(
            "Could not parse a `code` parameter from that URL.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        flow.fetch_token(code=code)
    except Exception as e:  # noqa: BLE001
        print(f"Token exchange failed: {e}", file=sys.stderr)
        sys.exit(1)

    creds = flow.credentials
    if not creds.refresh_token:
        print(
            "Google did not return a refresh token. Try again, or revoke "
            "this app at https://myaccount.google.com/permissions and "
            "re-link.",
            file=sys.stderr,
        )
        sys.exit(1)

    registry = GoogleRegistry.load(cfg.google_accounts_path)
    token_path = default_token_path_for(args.name)
    account = GoogleAccount(name=args.name, token_path=token_path)
    registry.add_or_update(account, make_default=args.make_default)
    registry.save()
    save_token(
        token_path,
        refresh_token=creds.refresh_token,
        scopes=list(creds.scopes or GOOGLE_SCOPES),
        token_uri=creds.token_uri or _TOKEN_URI,
    )
    print()
    print(f"Refresh token saved at {token_path}.")
    print(
        f"Account {args.name!r} added to registry "
        f"({cfg.google_accounts_path})."
    )
    if args.make_default or registry.default_name == args.name:
        print(f"Default account: {args.name!r}")
    print(
        "The voice daemon will refresh access silently from here on. "
        "Restart with `sudo systemctl restart jasper-voice` to pick "
        "up the new account."
    )


def _extract_code(pasted: str) -> str:
    """Pull the `code` parameter out of a pasted URL or accept a bare
    code if the user just pasted the value alone. The state value is
    available if we ever need to verify it, but the CLI already knows
    which account it's authorising — `args.name` was used to build
    the auth URL — so we don't double-check it here."""
    if not pasted:
        return ""
    # Bare code (no URL): no `?` and no `&code=`
    if "?" not in pasted and "code=" not in pasted:
        return pasted
    parsed = urllib.parse.urlparse(pasted)
    qs = urllib.parse.parse_qs(parsed.query)
    code = qs.get("code", [""])[0]
    return code


if __name__ == "__main__":
    main()
