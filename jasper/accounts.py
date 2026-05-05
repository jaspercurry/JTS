"""Multi-user Spotify account registry.

The speaker is a household device. More than one person may want to
issue voice commands and have those commands hit the right Spotify
account. Spotify's auth model is per-account — there is no shared
family token. So we maintain one OAuth refresh token per household
member and route commands to the right one by cross-referencing the
AirPlay-pushed track title against each account's currently-playing
Spotify track (see `jasper.spotify_router`).

State layout on disk:

    /var/lib/jasper/spotify/
        accounts.json              — registry index (this file)
        caches/<name>.json         — spotipy OAuth cache (one per user)

`accounts.json` shape:

    {
      "version": 1,
      "default": "jasper",
      "accounts": [
        {
          "name": "jasper",
          "cache_path": "/var/lib/jasper/spotify/caches/jasper.json"
        },
        ...
      ]
    }

Naming intentionally generic. "jasper" is the speaker project codename;
account names here are whatever each household member calls themselves.
A second household using this code might have accounts named "alice"
and "bob" — no code change needed.

Older registry files may carry a `client_name_patterns` field. It's
ignored — the title-match resolver supersedes the pattern model — but
left in JSON files in place so out-of-band tooling that wrote it
doesn't have to be updated immediately.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = "/var/lib/jasper/spotify/accounts.json"
DEFAULT_CACHE_DIR = "/var/lib/jasper/spotify/caches"

# Legacy single-account cache from the pre-multi-user era. Migrated
# into the registry as the default account on first startup if found.
LEGACY_CACHE_PATH = "/var/lib/jasper/.spotify-cache"


@dataclass
class Account:
    name: str
    cache_path: str = ""


@dataclass
class Registry:
    accounts: list[Account]
    default_name: str
    path: str

    def __init__(
        self,
        accounts: list[Account] | None = None,
        default_name: str = "",
        path: str = DEFAULT_REGISTRY_PATH,
    ) -> None:
        self.accounts = accounts if accounts is not None else []
        self.default_name = default_name
        self.path = path

    @classmethod
    def load(cls, path: str = DEFAULT_REGISTRY_PATH) -> "Registry":
        """Load from disk, or return an empty registry if the file is
        missing. Empty is a valid state — it means no accounts have been
        configured yet (e.g., fresh install before anyone has run the
        web setup)."""
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls(path=path)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("accounts registry %s unreadable (%s); starting empty", path, e)
            return cls(path=path)
        accounts = [
            Account(name=a["name"], cache_path=a.get("cache_path", ""))
            for a in data.get("accounts", [])
        ]
        return cls(accounts=accounts, default_name=data.get("default", ""), path=path)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {
            "version": 1,
            "default": self.default_name,
            "accounts": [asdict(a) for a in self.accounts],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, name: str) -> Account | None:
        for a in self.accounts:
            if a.name == name:
                return a
        return None

    def default(self) -> Account | None:
        if self.default_name:
            d = self.get(self.default_name)
            if d is not None:
                return d
        return self.accounts[0] if self.accounts else None

    def add_or_update(self, account: Account, *, make_default: bool = False) -> None:
        existing = self.get(account.name)
        if existing is not None:
            if account.cache_path:
                existing.cache_path = account.cache_path
        else:
            if not account.cache_path:
                account.cache_path = default_cache_path_for(account.name)
            self.accounts.append(account)
        if make_default or not self.default_name:
            self.default_name = account.name

    def remove(self, name: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.name != name]
        if self.default_name == name:
            self.default_name = self.accounts[0].name if self.accounts else ""
        return len(self.accounts) < before


def default_cache_path_for(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return os.path.join(DEFAULT_CACHE_DIR, f"{safe}.json")


def maybe_migrate_legacy(
    registry: Registry,
    legacy_cache: str = LEGACY_CACHE_PATH,
    default_name: str = "default",
) -> bool:
    """If the legacy single-account OAuth cache exists and the registry
    is empty, wrap that cache as the default account so existing
    single-user installs don't have to re-authenticate. Returns True
    if a migration was performed."""
    if registry.accounts:
        return False
    if not os.path.isfile(legacy_cache):
        return False
    new_cache = default_cache_path_for(default_name)
    os.makedirs(os.path.dirname(new_cache), exist_ok=True)
    try:
        Path(new_cache).write_bytes(Path(legacy_cache).read_bytes())
    except OSError as e:
        logger.warning("legacy cache migration failed: %s", e)
        return False
    registry.add_or_update(
        Account(name=default_name, cache_path=new_cache),
        make_default=True,
    )
    registry.save()
    logger.info(
        "migrated legacy spotify cache %s → account %s (%s)",
        legacy_cache, default_name, new_cache,
    )
    return True
