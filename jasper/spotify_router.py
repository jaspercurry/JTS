"""Routes Spotify commands to the right account.

Decides which household member's spotipy client should handle a given
voice command. Three signals, in priority order:

  1. AirPlay is active and the ClientName matches one of the
     configured accounts. This is the strongest signal — the audio
     coming through the speaker right now belongs to that person, so
     transport / play commands should target their account.

  2. AirPlay isn't active (or doesn't match). Check whose Spotify
     Web API session reports is_playing=true. Catches the case where
     somebody is Spotify-Connect'd to the Pi via librespot under their
     own account.

  3. Fall back to the registry's default account. This is the cold-
     start case — nobody is currently active, so we use the
     configured "speaker owner" for "play X" voice commands.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from .accounts import Account, Registry

logger = logging.getLogger(__name__)

SPOTIFY_SCOPE = (
    "user-modify-playback-state user-read-playback-state "
    "user-read-currently-playing user-read-private"
)


@dataclass
class AccountClient:
    account: Account
    sp: Any  # spotipy.Spotify, but kept loose for testability


def build_clients(
    registry: Registry,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, AccountClient]:
    """Build a spotipy.Spotify client per registered account. Skips any
    account whose cache file is missing or unreadable — they need to
    complete OAuth via the web flow before they show up in the dict."""
    # spotipy is imported lazily so the module is importable in test
    # environments without the spotipy wheel installed.
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    clients: dict[str, AccountClient] = {}
    for account in registry.accounts:
        try:
            auth = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope=SPOTIFY_SCOPE,
                cache_path=account.cache_path,
                open_browser=False,
            )
            # Trigger a token-cache read to surface OAuth issues at
            # startup rather than on first voice command.
            token = auth.get_cached_token()
            if not token:
                logger.warning(
                    "account %s has no cached token at %s — skipping (needs web setup)",
                    account.name, account.cache_path,
                )
                continue
            sp = spotipy.Spotify(auth_manager=auth)
            clients[account.name] = AccountClient(account=account, sp=sp)
        except Exception as e:  # noqa: BLE001
            logger.warning("account %s: build failed (%s); skipping", account.name, e)
    return clients


# DBus probe for shairport-sync's currently-connected ClientName.
# Returns "" when nothing is connected (or the call fails — we treat
# unreadable as "no AirPlay"). Cached call, but kept very short — the
# resolver runs at most once per voice command, not per audio frame.
_GNOME_DEST = "org.gnome.ShairportSync"
_GNOME_PATH = "/org/gnome/ShairportSync"
_GNOME_RC_IFACE = "org.gnome.ShairportSync.RemoteControl"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_CLIENT_NAME_RE = re.compile(r'string\s+"((?:[^"\\]|\\.)*)"')


async def airplay_client_name() -> str:
    """Read shairport's currently-connected sender name. Returns ''
    when AirPlay is not active or the property is empty."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "dbus-send", "--system", "--print-reply",
            f"--dest={_GNOME_DEST}",
            _GNOME_PATH,
            f"{_PROPS_IFACE}.Get",
            f"string:{_GNOME_RC_IFACE}",
            "string:ClientName",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError):
        return ""
    if proc.returncode != 0:
        return ""
    text = stdout.decode(errors="replace")
    # Expected output:
    #     method return ...
    #        variant       string "Jasper's iPhone"
    # The variant-string is wrapped in dbus-send's text-formatting; we
    # extract the inner string with the regex above. There's only one
    # string in the reply for a string-typed property.
    m = _CLIENT_NAME_RE.search(text)
    return m.group(1) if m else ""


@dataclass
class Router:
    """Picks the active AccountClient for the current voice command."""
    clients: dict[str, AccountClient]
    default_name: str

    async def resolve_airplay(self) -> AccountClient | None:
        """Return the AccountClient whose ClientName patterns match
        the current AirPlay sender, or None if AirPlay isn't active
        or the sender doesn't match any configured account.

        Used directly by the transport dispatcher when AirPlay is
        the active source — it's a stronger signal than the broader
        active() resolution because it confirms the audio coming
        through the speaker right now belongs to that specific
        account."""
        client_name = await airplay_client_name()
        if not client_name:
            return None
        for ac in self.clients.values():
            if ac.account.matches_client_name(client_name):
                logger.info(
                    "router: airplay sender %r matched account %s",
                    client_name, ac.account.name,
                )
                return ac
        logger.info(
            "router: airplay sender %r matched no configured account",
            client_name,
        )
        return None

    async def active(self, *, airplay_active: bool) -> AccountClient | None:
        """Resolve the AccountClient for the current moment.

        airplay_active: passed in by callers who already know moOde's
        renderer state, so we don't make redundant SQLite reads.
        """
        if not self.clients:
            return None

        # 1) AirPlay sender match wins.
        if airplay_active:
            matched = await self.resolve_airplay()
            if matched is not None:
                return matched

        # 2) Otherwise, whichever account's Spotify is_playing.
        playing = await asyncio.gather(
            *(self._is_playing(ac) for ac in self.clients.values()),
            return_exceptions=True,
        )
        for ac, is_playing in zip(self.clients.values(), playing):
            if is_playing is True:
                logger.info(
                    "router: account %s reports is_playing=true",
                    ac.account.name,
                )
                return ac

        # 3) Default account.
        if self.default_name in self.clients:
            logger.info("router: falling back to default account %s", self.default_name)
            return self.clients[self.default_name]
        first = next(iter(self.clients.values()))
        logger.info(
            "router: no default configured; falling back to first account %s",
            first.account.name,
        )
        return first

    @staticmethod
    async def _is_playing(ac: AccountClient) -> bool:
        try:
            playback = await asyncio.to_thread(ac.sp.current_playback)
        except Exception:  # noqa: BLE001
            return False
        return bool(playback and playback.get("is_playing"))
