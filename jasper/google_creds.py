"""Google OAuth credential storage + lookup for the per-household-
member Calendar + Gmail voice tools.

Mirrors `jasper.accounts` (Spotify multi-user registry) and
`jasper.spotify_router.build_clients` (lazy per-account API client
construction). One file because Google's surface is smaller than
Spotify's and a separate router buys nothing.

State layout on disk (WS1 Phase 4a — moved out of the shared
/var/lib/jasper StateDirectory into the group-`jasper-secrets` dir so
only jasper-voice + jasper-web can read the refresh tokens):

    /var/lib/jasper-secrets/google/
        accounts.json              — registry index
        tokens/<name>.json         — per-account refresh token

`accounts.json` shape:

    {
      "version": 1,
      "default": "jasper",
      "accounts": [
        {
          "name": "jasper",
          "token_path": "/var/lib/jasper-secrets/google/tokens/jasper.json",
          "email": "jasper@gmail.com",
          "display_name": "Jasper Curry"
        },
        ...
      ]
    }

`tokens/<name>.json` shape — a subset of google-auth's
``Credentials.to_authorized_user_info()``. The CLIENT_ID/SECRET fields
that the official format expects are stripped before saving (they
live in `/var/lib/jasper-secrets/google_credentials.env` and embedding
them in every per-user file would duplicate secrets across N copies):

    {
      "refresh_token": "1//0g...",
      "token_uri": "https://oauth2.googleapis.com/token",
      "scopes": ["https://www.googleapis.com/auth/calendar.readonly", ...]
    }

Access tokens (short-lived, ~1 h) are NOT persisted — google-auth
refreshes on every load. Same shape as spotipy's cache, just shorter
because we don't keep the access token between voice commands.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# WS1 Phase 4a — the Google token tree lives in the group-`jasper-secrets`
# dir (readable only by jasper-voice + jasper-web), NOT under the shared
# /var/lib/jasper StateDirectory. Overridable via JASPER_GOOGLE_ACCOUNTS_PATH
# (config.google_accounts_path). The install-time migration moves an
# existing tree here and rewrites the absolute token_path entries baked
# into accounts.json. See docs/HANDOFF-privilege-separation.md "Phase 4".
DEFAULT_REGISTRY_PATH = "/var/lib/jasper-secrets/google/accounts.json"
DEFAULT_TOKEN_DIR = "/var/lib/jasper-secrets/google/tokens"

# Read-only v1 scopes. The OIDC triplet (openid/email/profile) is used
# during the OAuth dance to fetch the user's email + display name so
# the wizard can show "Jasper Curry (jasper@gmail.com)" rather than a
# bare label. Costs nothing extra at Google's end. Calendar/Gmail are
# the two read scopes the v1 tool surface needs.
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


# ----------------------------------------------------------------------
# Registry — one entry per household member.
# ----------------------------------------------------------------------


@dataclass
class GoogleAccount:
    name: str
    token_path: str = ""
    email: str = ""
    display_name: str = ""


@dataclass
class GoogleRegistry:
    accounts: list[GoogleAccount]
    default_name: str
    path: str

    def __init__(
        self,
        accounts: list[GoogleAccount] | None = None,
        default_name: str = "",
        path: str = DEFAULT_REGISTRY_PATH,
    ) -> None:
        self.accounts = accounts if accounts is not None else []
        self.default_name = default_name
        self.path = path

    @classmethod
    def load(cls, path: str = DEFAULT_REGISTRY_PATH) -> "GoogleRegistry":
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls(path=path)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "google accounts registry %s unreadable (%s); starting empty",
                path, e,
            )
            return cls(path=path)
        accounts: list[GoogleAccount] = []
        for a in data.get("accounts", []):
            accounts.append(GoogleAccount(
                name=a["name"],
                token_path=a.get("token_path", ""),
                email=a.get("email", ""),
                display_name=a.get("display_name", ""),
            ))
        return cls(
            accounts=accounts,
            default_name=data.get("default", ""),
            path=path,
        )

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), mode=0o750, exist_ok=True)
        payload = {
            "version": 1,
            "default": self.default_name,
            "accounts": [asdict(a) for a in self.accounts],
        }
        tmp = self.path + ".tmp"
        # 0o640 group read — accounts.json holds the linked members' Gmail
        # addresses (PII-adjacent). WS1 Phase 4a: the file lives in the
        # setgid `jasper-secrets` dir, so a tempfile created here inherits
        # group `jasper-secrets`; 0o640 lets jasper-voice read a token
        # jasper-web's OAuth flow wrote (and vice versa) while keeping it off
        # the broad `jasper` group and away from every other daemon. No world
        # read. Token files use the same mode (save_token below).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, name: str) -> GoogleAccount | None:
        for a in self.accounts:
            if a.name == name:
                return a
        return None

    def default(self) -> GoogleAccount | None:
        if self.default_name:
            d = self.get(self.default_name)
            if d is not None:
                return d
        return self.accounts[0] if self.accounts else None

    def add_or_update(
        self,
        account: GoogleAccount,
        *,
        make_default: bool = False,
    ) -> None:
        existing = self.get(account.name)
        if existing is not None:
            if account.token_path:
                existing.token_path = account.token_path
            if account.email:
                existing.email = account.email
            if account.display_name:
                existing.display_name = account.display_name
        else:
            if not account.token_path:
                account.token_path = default_token_path_for(account.name)
            self.accounts.append(account)
        if make_default or not self.default_name:
            self.default_name = account.name

    def remove(self, name: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.name != name]
        if self.default_name == name:
            self.default_name = self.accounts[0].name if self.accounts else ""
        return len(self.accounts) < before


def default_token_path_for(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return os.path.join(DEFAULT_TOKEN_DIR, f"{safe}.json")


# ----------------------------------------------------------------------
# Token I/O — strip-and-restore the client_id/secret around persistence.
# ----------------------------------------------------------------------


def save_token(token_path: str, *, refresh_token: str, scopes: list[str] | None = None,
               token_uri: str = GOOGLE_TOKEN_URI) -> None:
    """Persist a token JSON at mode 0600. Refuses to write a payload
    without a refresh_token — that's the only field that's actually
    durable, and a file without it is useless to load_credentials."""
    if not refresh_token:
        raise ValueError("refusing to save token without a refresh_token")
    payload = {
        "refresh_token": refresh_token,
        "token_uri": token_uri or GOOGLE_TOKEN_URI,
        "scopes": list(scopes) if scopes else list(GOOGLE_SCOPES),
    }
    os.makedirs(os.path.dirname(token_path), mode=0o750, exist_ok=True)
    tmp = token_path + ".tmp"
    # 0o640 group read — the token dir is setgid `jasper-secrets` (WS1
    # Phase 4a), so this tempfile inherits that group; group read lets
    # jasper-voice load a token jasper-web's OAuth wrote, with no access for
    # any other daemon. No world read. See GoogleRegistry.save.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, token_path)


def load_credentials(
    account: GoogleAccount,
    *,
    client_id: str,
    client_secret: str,
):
    """Reconstruct ``google.oauth2.credentials.Credentials`` from the
    stored refresh token plus the env-file's client_id/secret, then
    refresh to get a fresh access token. Returns None on any failure
    (missing file, malformed JSON, network error, revoked token) so
    callers can treat the account as silently disabled and surface a
    re-link instruction to the user.

    The access token is short-lived (~1 h), so we refresh on every
    call rather than caching across voice commands. One HTTP per
    voice command is cheap compared to the Google API call that
    follows it; the alternative (caching with TTL) would just be a
    second copy of state to keep coherent."""
    if not account.token_path:
        return None
    try:
        with open(account.token_path) as f:
            raw = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        logger.info(
            "google account %s token unreadable at %s: %s",
            account.name, account.token_path, e,
        )
        return None
    refresh_token = (raw or {}).get("refresh_token", "")
    if not refresh_token:
        logger.warning(
            "google account %s token file %s lacks refresh_token",
            account.name, account.token_path,
        )
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        logger.warning("google-auth not installed; google tools unavailable")
        return None
    info = {
        "refresh_token": refresh_token,
        "token_uri": raw.get("token_uri") or GOOGLE_TOKEN_URI,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": raw.get("scopes") or list(GOOGLE_SCOPES),
    }
    try:
        creds = Credentials.from_authorized_user_info(info, scopes=info["scopes"])
        if not creds.valid:
            creds.refresh(Request())
        return creds
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "google account %s: token refresh failed (%s); "
            "user may need to re-link at the wizard",
            account.name, e,
        )
        return None


def valid_access_token(
    account: GoogleAccount,
    *,
    client_id: str,
    client_secret: str,
) -> str | None:
    """Return a freshly-refreshed access token for this account, or
    None if anything went wrong. Used by jasper-doctor and ad-hoc
    probes; tools usually want the full Credentials object so they
    can hand it straight to googleapiclient."""
    creds = load_credentials(
        account, client_id=client_id, client_secret=client_secret,
    )
    if creds is None:
        return None
    token = getattr(creds, "token", None)
    return token if isinstance(token, str) and token else None


# ----------------------------------------------------------------------
# Tool-side accessor.
# ----------------------------------------------------------------------


def _default_service_factory(api_name: str, version: str, credentials):
    """googleapiclient.discovery.build, with the discovery cache
    suppressed so we don't print the "file_cache is only supported with
    oauth2client<4.0.0" startup warning. The discovery JSON is small
    enough that the in-process LRU is plenty."""
    from googleapiclient.discovery import build
    return build(
        api_name, version, credentials=credentials, cache_discovery=False,
    )


@dataclass
class GoogleClients:
    """Lookup + service-build surface for the per-account tools.

    Tools call `resolve_account(name)` to convert the model's `account`
    arg ("brittany" or "" for default) into a canonical name, then
    `build_calendar(name)` / `build_gmail(name)` for the API resource.
    Tests inject a `service_factory` to avoid hitting Google."""
    registry: GoogleRegistry
    client_id: str
    client_secret: str
    service_factory: Callable[..., Any] = field(default=_default_service_factory)

    def list_account_names(self) -> list[str]:
        return [a.name for a in self.registry.accounts]

    def default_account_name(self) -> str | None:
        d = self.registry.default()
        return d.name if d is not None else None

    def resolve_account(self, name: str) -> str | None:
        """Convert the model's `account` arg to a canonical name. Empty
        string → use the default account. Unknown name → None (the
        caller surfaces "no account named X — try Y or Z")."""
        if not name:
            return self.default_account_name()
        match = self.registry.get(name)
        return match.name if match is not None else None

    def credentials(self, name: str):
        a = self.registry.get(name)
        if a is None:
            return None
        return load_credentials(
            a, client_id=self.client_id, client_secret=self.client_secret,
        )

    def build_calendar(self, name: str):
        creds = self.credentials(name)
        if creds is None:
            return None
        return self.service_factory("calendar", "v3", creds)

    def build_gmail(self, name: str):
        creds = self.credentials(name)
        if creds is None:
            return None
        return self.service_factory("gmail", "v1", creds)


def build_google_clients(
    cfg,
    *,
    registry: GoogleRegistry | None = None,
    service_factory: Callable[..., Any] | None = None,
) -> GoogleClients | None:
    """Build a `GoogleClients` from a `Config`, or return None if
    Google is not configured at the env level (no CLIENT_ID/SECRET).

    The voice daemon calls this in `_build_registry`; tests construct
    `GoogleClients(...)` directly with a fake registry + factory."""
    if not cfg.google_enabled:
        return None
    reg = registry if registry is not None else GoogleRegistry.load(cfg.google_accounts_path)
    factory = service_factory or _default_service_factory
    return GoogleClients(
        registry=reg,
        client_id=cfg.google_client_id,
        client_secret=cfg.google_client_secret,
        service_factory=factory,
    )
