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
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .accounts import Account, Registry, build_cache_handler
from .spotify_routing import _normalise as _normalise_title

logger = logging.getLogger(__name__)

SPOTIFY_SCOPE = (
    "user-modify-playback-state user-read-playback-state "
    "user-read-currently-playing user-read-private "
    "playlist-read-private playlist-read-collaborative"
)

# Per-request timeout (seconds) for the spotipy HTTP client. spotipy's
# default is no timeout, so a hung Spotify API socket would block the
# calling thread indefinitely. Bounded here so callers (e.g. mux's pause
# loop) can't be suspended forever on one stuck account.
_SPOTIPY_REQUESTS_TIMEOUT_SEC = 4.0

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

# Minimum gap between Router.refresh_if_empty attempts when the previous
# attempt produced no usable clients. Protects Spotify's /api/token from
# being hammered on a persistently-revoked account (the user has to
# re-link via the wizard for recovery; retrying every voice command
# can't help). Tuned for "every voice command shouldn't trigger a network
# refresh, but the user shouldn't wait long after re-linking either."
# Tests monkey-patch this module attribute when they need deterministic
# timing.
_REFRESH_MIN_INTERVAL_SEC = 30.0


def _now() -> float:
    """Wrapped `time.monotonic` so tests can mock the refresh-cooldown
    clock without affecting asyncio's event-loop clock (which also calls
    `time.monotonic`)."""
    return time.monotonic()


@dataclass
class AccountClient:
    account: Account
    sp: Any  # spotipy.Spotify, but kept loose for testability


# State values surfaced per account from build_clients. The wizard and
# the voice tool both read these to pick the right user-facing message.
# Keep this in sync with `jasper.web.spotify_setup.AccountHealth`.
ACCOUNT_OK = "ok"                  # cache present, refresh succeeded
ACCOUNT_NEEDS_OAUTH = "needs_oauth"  # no cache file — never OAuthed
ACCOUNT_REVOKED = "revoked"        # cache present, refresh got invalid_grant
ACCOUNT_ERROR = "error"            # other failure (network, etc.)


@dataclass
class AccountStatus:
    name: str
    state: str  # one of ACCOUNT_OK / _NEEDS_OAUTH / _REVOKED / _ERROR
    detail: str = ""


@dataclass
class BuildResult:
    clients: dict[str, AccountClient]
    statuses: list[AccountStatus]
    # The registry's default_name at the moment of the build. Mirrored
    # here so a rebuild_fn caller (Router.refresh_if_empty) can also
    # update Router.default_name when the wizard changes which account
    # is default — without a second Registry.load.
    default_name: str = ""


def _classify_oauth_error(exc: BaseException) -> tuple[str, str]:
    """Map a spotipy/refresh exception to (state, detail). Picks
    'revoked' for any error whose `error` attr is 'invalid_grant', or
    whose text mentions invalid_grant / revoked — that's the Spotify
    response for revoked/expired/superseded refresh tokens.

    Inspects `.error` first (the structured attribute spotipy's
    SpotifyOauthError carries; future-proofs against upstream changes
    to the str() format), then falls back to substring search on the
    rendered text (catches plain-Exception cases used in tests + any
    historical paths where the structured attr is missing)."""
    error_attr = getattr(exc, "error", None)
    if isinstance(error_attr, str) and error_attr.lower() == "invalid_grant":
        return ACCOUNT_REVOKED, "refresh token revoked — re-link required"
    msg = str(exc)
    low = msg.lower()
    if "invalid_grant" in low or "revoked" in low:
        return ACCOUNT_REVOKED, "refresh token revoked — re-link required"
    return ACCOUNT_ERROR, msg[:200]


def build_clients(
    registry: Registry,
    *,
    client_id: str,
    redirect_uri: str,
) -> BuildResult:
    """Build a spotipy.Spotify client per registered account. Returns
    a BuildResult with the usable clients dict, a per-account status
    list (so callers can distinguish "never set up" from "token revoked"
    and surface the right message to the user), and the registry's
    default_name (mirrored so Router.refresh_if_empty can update its
    default_name on a rebuild without a separate Registry.load).

    Uses PKCE (no client secret). Refresh tokens issued via the legacy
    Code+Secret flow will fail to refresh under PKCE — those accounts
    need to re-link via the web wizard at http://jts.local/spotify/.
    """
    # spotipy is imported lazily so the module is importable in test
    # environments without the spotipy wheel installed.
    import spotipy
    from spotipy.oauth2 import SpotifyPKCE

    clients: dict[str, AccountClient] = {}
    statuses: list[AccountStatus] = []
    for account in registry.accounts:
        cache_path = account.cache_path
        if cache_path and not os.path.exists(cache_path):
            logger.warning(
                "account %s has no cached token at %s — skipping (needs web setup)",
                account.name, cache_path,
            )
            statuses.append(AccountStatus(
                name=account.name,
                state=ACCOUNT_NEEDS_OAUTH,
                detail="no cached token — not yet OAuthed",
            ))
            continue
        try:
            auth = SpotifyPKCE(
                client_id=client_id,
                redirect_uri=redirect_uri,
                scope=SPOTIFY_SCOPE,
                # cache_handler (not cache_path) so a refresh-on-read rewrites
                # the token cache group-`jasper`-readable (0640) — the non-root
                # jasper-control reader needs it; see accounts.build_cache_handler.
                cache_handler=build_cache_handler(cache_path),
                open_browser=False,
            )
            # Trigger a token-cache read to surface OAuth issues at
            # startup rather than on first voice command. validate_token
            # auto-refreshes if expired; that's the path where a revoked
            # refresh_token raises SpotifyOauthError(invalid_grant).
            token = auth.get_cached_token()
            if not token:
                logger.warning(
                    "account %s cache present but token unusable — needs re-link",
                    account.name,
                )
                statuses.append(AccountStatus(
                    name=account.name,
                    state=ACCOUNT_NEEDS_OAUTH,
                    detail="cache unreadable or empty",
                ))
                continue
            sp = spotipy.Spotify(
                auth_manager=auth,
                requests_timeout=_SPOTIPY_REQUESTS_TIMEOUT_SEC,
            )
            clients[account.name] = AccountClient(account=account, sp=sp)
            statuses.append(AccountStatus(name=account.name, state=ACCOUNT_OK))
        except Exception as e:  # noqa: BLE001
            state, detail = _classify_oauth_error(e)
            logger.warning(
                "account %s: build failed (%s); marking %s",
                account.name, e, state,
            )
            statuses.append(AccountStatus(
                name=account.name, state=state, detail=detail,
            ))
    return BuildResult(
        clients=clients, statuses=statuses,
        default_name=registry.default_name,
    )


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
    """Picks the active AccountClient for the current voice command.

    Holds per-account `statuses` (so callers can ask "is this empty
    because we never set up, or because every token is revoked?") and
    optionally a `rebuild_fn` — when set, `refresh_if_empty()` reruns
    the build and atomically replaces `clients` if any account now
    succeeds. This lets a re-link via the wizard recover without a
    daemon restart: the next voice command's per-call check picks up
    the new cache file.
    """
    clients: dict[str, AccountClient]
    default_name: str
    statuses: list[AccountStatus] = field(default_factory=list)
    rebuild_fn: Callable[[], BuildResult] | None = field(default=None, repr=False)
    # None means "never attempted" — distinguishes the first call (which
    # must always proceed) from a recent retry (which we rate-limit at
    # _REFRESH_MIN_INTERVAL_SEC). Initializing to 0.0 was a bug:
    # time.monotonic() near process start is < the interval, so the very
    # first refresh would be silently throttled.
    _last_refresh_attempt: float | None = field(
        default=None, init=False, repr=False,
    )
    _cache: _CachedDecision | None = field(default=None, init=False, repr=False)

    async def refresh_if_empty(self) -> bool:
        """If `clients` is empty and a `rebuild_fn` is set, rerun the
        build and replace `clients` + `statuses` if it succeeds.
        Rate-limited to once per `_REFRESH_MIN_INTERVAL_SEC` so we don't
        hammer Spotify's token endpoint on a persistently-revoked token
        (the user has to re-link via the wizard; retrying won't help).

        Returns True iff `clients` is non-empty after the attempt.

        Failure-mode notes:
        - If `rebuild_fn` raises (e.g. transient network error reading
          the registry, or Spotify's auth endpoint 5xx'ing) we log,
          return False, and do NOT advance the rate-limit timestamp —
          the next voice command should retry immediately rather than
          wait out the cooldown. The cooldown exists to throttle
          PERSISTENT failures (revoked tokens), not transient ones.
        - If `rebuild_fn` runs and produces a result with no clients
          (e.g. all accounts revoked) we record the timestamp so we
          don't pound /api/token on every subsequent voice command.
        """
        if self.clients:
            return True
        if self.rebuild_fn is None:
            return False
        now = _now()
        if (
            self._last_refresh_attempt is not None
            and now - self._last_refresh_attempt < _REFRESH_MIN_INTERVAL_SEC
        ):
            return bool(self.clients)
        try:
            result = await asyncio.to_thread(self.rebuild_fn)
        except Exception as e:  # noqa: BLE001
            # Don't update _last_refresh_attempt — a transient failure
            # shouldn't lock us out of retrying for the cooldown window.
            logger.warning("router: rebuild_fn raised: %s", e)
            return False
        self._last_refresh_attempt = now
        # Replace statuses + default_name so the tool layer sees the
        # latest per-account reason and the wizard-current default.
        # (The wizard may still show a stale "ok" for up to its own
        # cache TTL — that's fine, the wizard owns its cache.)
        self.statuses = result.statuses
        if result.default_name:
            self.default_name = result.default_name
        if result.clients:
            self.clients = result.clients
            logger.info(
                "router: lazy rebuild succeeded with %d client(s): %s",
                len(result.clients), sorted(result.clients.keys()),
            )
            return True
        logger.info(
            "router: lazy rebuild produced no clients (statuses=%s)",
            [(s.name, s.state) for s in result.statuses],
        )
        return False

    def empty_reason(self) -> str:
        """Classify why `clients` is empty. Returns one of:
          "revoked"     — at least one account has a revoked token
          "needs_oauth" — accounts registered but none OAuthed
          "no_accounts" — registry empty
          ""            — clients is non-empty (no failure)
        Used by the voice tool to pick the right user-facing message."""
        if self.clients:
            return ""
        if not self.statuses:
            return "no_accounts"
        if any(s.state == ACCOUNT_REVOKED for s in self.statuses):
            return "revoked"
        return "needs_oauth"

    def revoked_account_names(self) -> list[str]:
        """Names of accounts whose tokens were revoked at last build.
        Empty list when there are none (or when statuses isn't
        populated). Voice tool reads this to name the affected
        accounts in the spoken error — "spotify signed jasper out"
        beats "your spotify session expired" because the user knows
        which household member's account to re-link."""
        return [s.name for s in self.statuses if s.state == ACCOUNT_REVOKED]

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
