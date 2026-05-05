"""Routes Spotify commands to the right account.

Decides which household member's spotipy client should handle a given
voice command. There are two distinct resolution paths because the
signals available are different:

  - **Transport commands** (next/prev/pause/resume) — use
    `resolve_for_transport(client_name, mpris_title)`. AirPlay is
    pushing a track right now; we cross-reference each account's
    Spotify `current_playback.item.name` against shairport's
    MPRIS `xesam:title` and route to whoever's playing the same
    song. This is robust to device renames, multi-device users,
    and "the person playing isn't the speaker owner" — none of
    which the old ClientName-pattern model handled well.

  - **Cold-start commands** (`spotify_play "X"`) — use `active()`.
    No track is in flight to cross-reference, so we fall back to
    is_playing across configured accounts, then to the default
    account. This is the "speaker owner says 'play Beyoncé' from
    silence" case.

The session cache on `resolve_for_transport` means repeated
transport commands during the same AirPlay session and same track
hit a dict lookup, not the Spotify Web API. Re-resolution is forced
on track change (mpris_title differs), sender change (client_name
differs), or 1h TTL.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .accounts import Account, Registry
from .spotify_routing import _normalise as _normalise_title

logger = logging.getLogger(__name__)

SPOTIFY_SCOPE = (
    "user-modify-playback-state user-read-playback-state "
    "user-read-currently-playing user-read-private"
)

# Re-resolve a cached AirPlay-session→account decision after this many
# seconds even if nothing else changed. Belt-and-suspenders against
# stale tokens or a Spotify state we missed.
_CACHE_TTL_SEC = 3600.0

# Retry budget for title-match resolution. Spotify's `current_playback`
# is eventually-consistent: a session that's actively playing can briefly
# return None mid-playback, especially right after a track change or a
# device handoff. We retry a small number of times when the observed
# state looks "transiently empty" (all accounts returned None or raised),
# but skip retries when at least one account returned real data — a real
# data row that doesn't match isn't a blip, it's a non-Spotify sender,
# and retrying just adds latency before falling through to DACP.
_RETRY_BACKOFF_SEC = (0.20, 0.40)  # 2 retries, ~600ms total worst case


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
    m = _CLIENT_NAME_RE.search(text)
    return m.group(1) if m else ""


@dataclass
class _CachedDecision:
    client_name: str
    mpris_title_norm: str
    account_name: str
    cached_at: float


@dataclass
class Router:
    """Picks the active AccountClient for the current voice command."""
    clients: dict[str, AccountClient]
    default_name: str
    _cache: _CachedDecision | None = field(default=None, init=False, repr=False)

    async def resolve_for_transport(
        self, client_name: str, mpris_title: str
    ) -> AccountClient | None:
        """Cross-reference AirPlay's current track title against each
        account's Spotify `current_playback.item.name` and return the
        matching account. Returns None if no account is playing the
        same title — caller should fall back (DACP for non-Spotify
        senders, or surface an error).

        Cache key: `(client_name, normalized_mpris_title)`. Re-resolves
        on sender change, track change, or 1h TTL.

        Retries: if the first attempt returns no match AND every account
        either returned None or raised (i.e. nothing usable came back),
        retry up to `len(_RETRY_BACKOFF_SEC)` times to absorb Spotify
        Web API blips. A no-match where at least one account returned
        real data is a genuine non-match (probably a non-Spotify sender)
        and skips retries to avoid stalling the DACP fallback path.
        """
        if not self.clients:
            return None
        if not (client_name and mpris_title):
            # No identity signal to match on. Caller falls back.
            return None

        title_norm = _normalise_title(mpris_title)

        cached = self._cache
        if (
            cached is not None
            and cached.client_name == client_name
            and cached.mpris_title_norm == title_norm
            and (time.monotonic() - cached.cached_at) < _CACHE_TTL_SEC
            and cached.account_name in self.clients
        ):
            logger.debug(
                "router: cache hit — sender=%r title=%r → account=%s",
                client_name, mpris_title, cached.account_name,
            )
            return self.clients[cached.account_name]

        chosen: AccountClient | None = None
        for attempt in range(len(_RETRY_BACKOFF_SEC) + 1):
            chosen, retry_advised = await self._probe_and_match(
                client_name, mpris_title, title_norm,
            )
            if chosen is not None or not retry_advised:
                break
            if attempt < len(_RETRY_BACKOFF_SEC):
                backoff = _RETRY_BACKOFF_SEC[attempt]
                logger.info(
                    "router: title-match empty for sender=%r title=%r — "
                    "retrying in %dms (attempt %d/%d)",
                    client_name, mpris_title,
                    int(backoff * 1000), attempt + 1, len(_RETRY_BACKOFF_SEC),
                )
                await asyncio.sleep(backoff)

        if chosen is not None:
            self._cache = _CachedDecision(
                client_name=client_name,
                mpris_title_norm=title_norm,
                account_name=chosen.account.name,
                cached_at=time.monotonic(),
            )
        return chosen

    async def _probe_and_match(
        self, client_name: str, mpris_title: str, title_norm: str,
    ) -> tuple[AccountClient | None, bool]:
        """One round of `current_playback` polling + title comparison.
        Returns `(chosen, retry_advised)`. retry_advised is True only
        when the observed state looks transient (all accounts returned
        None or raised) — a stable no-match (some accounts had data,
        none matched) returns False so the caller can fall through to
        DACP without paying retry latency."""
        t0 = time.monotonic()
        playbacks = await asyncio.gather(
            *(self._current_playback(ac) for ac in self.clients.values()),
            return_exceptions=True,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        title_matches: list[AccountClient] = []
        playing_matches: list[AccountClient] = []
        n_data = 0
        n_none = 0
        n_error = 0
        for ac, pb in zip(self.clients.values(), playbacks):
            if isinstance(pb, Exception):
                n_error += 1
                logger.warning(
                    "router: account %s current_playback raised: %s",
                    ac.account.name, pb,
                )
                continue
            if pb is None:
                n_none += 1
                logger.debug(
                    "router: account %s current_playback=None (no recent session)",
                    ac.account.name,
                )
                continue
            n_data += 1
            item = pb.get("item") or {}
            sp_title = item.get("name", "")
            is_playing = bool(pb.get("is_playing"))
            matches = bool(sp_title) and _normalise_title(sp_title) == title_norm
            logger.debug(
                "router: account %s playback title=%r is_playing=%s match=%s",
                ac.account.name, sp_title, is_playing, matches,
            )
            if matches:
                title_matches.append(ac)
                if is_playing:
                    playing_matches.append(ac)

        chosen: AccountClient | None
        if not title_matches:
            chosen = None
        elif len(title_matches) == 1:
            chosen = title_matches[0]
        elif len(playing_matches) == 1:
            # Multiple accounts queued the same track; the one actively
            # playing is the AirPlay sender.
            chosen = playing_matches[0]
        elif self.default_name in {ac.account.name for ac in title_matches}:
            chosen = self.clients[self.default_name]
        else:
            chosen = title_matches[0]

        if chosen is not None:
            logger.info(
                "router: sender=%r title=%r → account=%s "
                "(probed %d accounts in %dms, %d title-match, %d playing)",
                client_name, mpris_title, chosen.account.name,
                len(self.clients), elapsed_ms,
                len(title_matches), len(playing_matches),
            )
        else:
            logger.info(
                "router: sender=%r title=%r → no match "
                "(probed %d accounts in %dms: %d data, %d none, %d error)",
                client_name, mpris_title,
                len(self.clients), elapsed_ms, n_data, n_none, n_error,
            )

        # Retry only when the API state looks transient — i.e. nothing
        # usable came back at all. Real data with no title match is a
        # non-Spotify sender; retrying won't help, fall through to DACP.
        retry_advised = chosen is None and n_data == 0 and (n_none > 0 or n_error > 0)
        return chosen, retry_advised

    def invalidate_cache(self) -> None:
        """Drop the cached AirPlay→account decision. Call when the
        AirPlay session ends so the next resolution starts fresh."""
        self._cache = None

    async def active(self, *, airplay_active: bool) -> AccountClient | None:
        """Resolve an account for cold-start commands like
        `spotify_play "Beyoncé"` — i.e. when there's no current track
        title to match against. Picks the first is_playing account, or
        falls back to the default.

        Note: for transport (next/prev/pause/resume) on an active
        AirPlay session, use `resolve_for_transport()` instead — it
        does the title cross-reference. This method is intentionally
        coarser; it's only used when no AirPlay-side title is
        available.
        """
        if not self.clients:
            return None

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
    async def _current_playback(ac: AccountClient) -> dict | None:
        try:
            return await asyncio.to_thread(ac.sp.current_playback)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    async def _is_playing(ac: AccountClient) -> bool:
        playback = await Router._current_playback(ac)
        return bool(playback and playback.get("is_playing"))
