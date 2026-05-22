from __future__ import annotations

import asyncio
import logging

from rapidfuzz import fuzz

from . import tool
from ..spotify_routing import resolve_target, stop_renderers

logger = logging.getLogger(__name__)


# Confidence thresholds for spotify_play resolution. Tuned empirically
# against rapidfuzz.fuzz.WRatio:
#   "matt and kim" vs "strfkr"            → 24   (well below)
#   "sufjan stevens" vs "taylor swift"    → 31   (well below)
#   "jasperine jams" vs "jaspany jams"    → 77   (above, mishear-tolerant)
#   exact match (any)                      → 100
_PLAY_THRESHOLD = 75
_PLAYLIST_THRESHOLD = 55   # very loose: voice-to-text on user-coined
                           # playlist names is brutal ("Jaspany Jams" →
                           # "Jazz Knee Jams"). User said the word
                           # "playlist", so we know the intent and the
                           # candidate pool is their personal library —
                           # false-positive risk is low.
_PLAYLIST_BEST_BY_FAR_GAP = 15   # if the top library match scores at
                                  # least this many points above #2 AND
                                  # clears the floor below, take it even
                                  # at sub-threshold absolute score —
                                  # there's a clear winner.
_PLAYLIST_BEST_BY_FAR_FLOOR = 35  # absolute floor that the best-by-far
                                  # rule must still clear. Stops a single-
                                  # playlist library from matching every
                                  # unrelated query (top.score=20, gap=20
                                  # — clear winner of nothing).
_TIEBREAK_GAP = 5          # within this score gap, fall back to the
                           # preference order rather than picking by score.

# Order matters — first listed wins on close-score tiebreak.
_TIEBREAK_ORDER = ("artist", "playlist", "track", "album")

_NOT_UNDERSTOOD = (
    "Sorry, I didn't understand. Please try again — did you mean an "
    "artist, a song, or a playlist?"
)


def _format_name_list(names: "list[str]") -> str:
    """English list join for spoken output: ['jasper'] → 'jasper';
    ['jasper', 'brittany'] → 'jasper and brittany';
    ['a', 'b', 'c'] → 'a, b, and c'. Lowercased — voice tool error
    messages are lowercase by convention (see other tool errors)."""
    lowered = [n.lower() for n in names]
    if not lowered:
        return ""
    if len(lowered) == 1:
        return lowered[0]
    if len(lowered) == 2:
        return f"{lowered[0]} and {lowered[1]}"
    return ", ".join(lowered[:-1]) + f", and {lowered[-1]}"


def _playlist_score(query: str, name: str) -> int:
    """Fuzzy score (0-100) for a playlist name against the user's spoken
    query.

    We use plain Levenshtein-based `fuzz.ratio` rather than `WRatio`:

    - `WRatio` includes `partial_ratio`, which fires ~83% on
      'discover weekly' vs 'covers' because 'covers' looks like a
      substring of 'discover'. False positive.
    - `token_set_ratio` / `token_sort_ratio` look for word-level
      overlap, which misses character-level voice-to-text errors:
      'jazz bunny jams' vs 'jaspany jamz' scores only 44 there
      despite being a phonetically reasonable mishear.
    - `fuzz.ratio` (raw Levenshtein, normalised) splits the difference:
      'discover weekly' vs 'covers' = 47 (correctly below threshold),
      'jazz bunny jams' vs 'jaspany jamz' = 59 (correctly above).
    """
    return int(fuzz.ratio(query.lower(), name.lower()))


async def _user_library_ranked(
    sp, query: str, configured: "dict[str, str] | None" = None,
) -> "list[tuple[str, str, int]]":
    """Return playlists ranked by fuzzy match against `query`. Pool =
    `configured` (per-account map of uri→name set via the web UI) +
    everything from `sp.current_user_playlists`. Configured entries
    appear first on score ties (stable sort), so a manually-pinned
    Discover Weekly URI wins over a same-named library entry.

    Each entry is (uri, name, score), sorted high-to-low. Empty list
    when both pools are empty / unreachable."""
    ranked: list[tuple[str, str, int]] = []
    seen_uris: set[str] = set()

    # Configured first — for stable-sort tiebreak in the user's favour.
    if configured:
        for uri, name in configured.items():
            if not (uri and name):
                continue
            ranked.append((uri, name, _playlist_score(query, name)))
            seen_uris.add(uri)

    try:
        playlists = await asyncio.to_thread(sp.current_user_playlists, limit=50)
    except Exception as e:  # noqa: BLE001
        logger.warning("user playlists fetch failed: %s", e)
        playlists = None
    items = ((playlists or {}).get("items") or []) if playlists else []
    for p in items:
        if not p:
            continue
        name = p.get("name") or ""
        uri = p.get("uri") or ""
        if not (name and uri) or uri in seen_uris:
            continue
        ranked.append((uri, name, _playlist_score(query, name)))
        seen_uris.add(uri)

    ranked.sort(key=lambda r: -r[2])
    return ranked


async def _spotify_owned_playlist_match(
    sp, query: str
) -> "tuple[str, str, int] | None":
    """Search Spotify's public catalog for playlists whose owner is
    Spotify itself, fuzzy-match against `query`. Catches personalized
    auto-generated playlists (Discover Weekly, Release Radar, Daily Mix
    N, Repeat Rewind) and curated featured playlists, while filtering
    out the noise of random user playlists like 'Jaslene's Jams' that
    happen to share keywords with the query.

    Returns (uri, name, score) for the best Spotify-owned match, or
    None if no Spotify-owned result clears the playlist threshold."""
    try:
        results = await asyncio.to_thread(
            sp.search, q=query, type="playlist", limit=10
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("public playlist search failed: %s", e)
        return None
    items = ((results or {}).get("playlists") or {}).get("items") or []
    if not items:
        logger.info(
            "spotify_play: spotify-owned fallback for query=%r → "
            "search returned 0 playlists",
            query,
        )
        return None
    candidates: list[tuple[str, str, int, str]] = []  # (uri, name, score, owner_id)
    for p in items:
        if not p:
            continue
        owner_id = ((p.get("owner") or {}).get("id") or "").lower()
        name = p.get("name") or ""
        uri = p.get("uri") or ""
        if not (name and uri):
            continue
        score = _playlist_score(query, name)
        candidates.append((uri, name, score, owner_id))
    logger.info(
        "spotify_play: spotify-owned fallback for query=%r → "
        "search top 5: %s",
        query, [(name, owner, score) for _, name, score, owner in candidates[:5]],
    )
    best: tuple[str, str, int] | None = None
    for uri, name, score, owner_id in candidates:
        if owner_id != "spotify":
            continue
        if best is None or score > best[2]:
            best = (uri, name, score)
    return best


async def _user_library_match(
    sp, query: str, configured: "dict[str, str] | None" = None,
) -> "tuple[str, str, int] | None":
    """Top fuzzy match from the user's library + configured playlists,
    or None if empty/unreachable. Thin wrapper for the auto-path."""
    ranked = await _user_library_ranked(sp, query, configured=configured)
    return ranked[0] if ranked else None


async def _resolve_query(
    sp, query: str, kind: str,
    configured_playlists: "dict[str, str] | None" = None,
) -> "tuple[str, str, str] | None":
    """Resolve a 'play X' query to (uri, resolved_kind, display_name).

    Strategy:
      - kind="playlist"  → fuzzy-match user library (loose threshold,
                            tolerant of voice-to-text mishears); fall
                            back to public playlist search.
      - kind in {artist, album} → field-qualified Spotify search so the
                            query matches the entity NAME, not the
                            contents of its discography.
      - kind="track"     → unqualified Spotify search (preserves
                            "X by Y" phrasing).
      - kind="auto"      → fan out artist + track + album + library
                            scans, score with rapidfuzz, gate on
                            confidence threshold, tiebreak by
                            preference order.

    Returns None when no candidate clears the confidence bar — caller
    should surface a clarification error to the user.
    """
    safe_q = query.replace(chr(34), "")

    if kind == "playlist":
        # Defensive query normalization: strip a trailing "playlist" /
        # "playlists" before fuzzy-matching. The tool docstring tells
        # the model to set `kind="playlist"` and pass just the playlist
        # NAME (e.g. user says "play my Covers playlist" → query="Covers"),
        # but in practice the model sometimes leaves the word "playlist"
        # in the query string. That breaks fuzz.ratio: e.g. against the
        # library [Covers, untitled playlist], query="covers playlist"
        # scores ('untitled playlist', 62) > ('Covers', 57) because the
        # shared word "playlist" tips the Levenshtein balance toward
        # OTHER names that happen to contain "playlist". After this
        # strip, query="covers playlist" → "covers" → ('Covers', 100).
        # We only strip TRAILING occurrences so playlist names that
        # actually start with "Playlist" (e.g. "Playlist Best of 2026")
        # match correctly.
        playlist_q = query.strip()
        lower = playlist_q.lower()
        for suffix in (" playlists", " playlist"):
            if lower.endswith(suffix):
                playlist_q = playlist_q[: -len(suffix)].rstrip()
                break

        # User said the word "playlist". First try THEIR pool — the
        # account's configured-via-web-UI playlists merged with their
        # Spotify library. Configured wins ties (stable sort).
        # Fall back to Spotify-owned catalog search (Discover Weekly,
        # Release Radar, Daily Mix N) — but per the 2026 API, that
        # endpoint no longer returns the real Spotify-owned versions
        # for newly-issued credentials, so the configured map is the
        # only reliable path for those. Kept for the rare account that
        # still has access.
        # We do NOT fall back to general public playlist search —
        # 'Jaspany Jams' would otherwise fuzzy-match strangers' 'Jaslene's
        # Jams', which is exactly what we don't want.
        ranked = await _user_library_ranked(sp, playlist_q, configured=configured_playlists)
        if ranked:
            logger.info(
                "spotify_play: library candidates for query=%r → %s",
                query, [(name, score) for _, name, score in ranked[:5]],
            )
            top = ranked[0]
            if top[2] >= _PLAYLIST_THRESHOLD:
                return top[0], "playlist", top[1]
            # Best-by-far: take the top match even at low absolute score
            # if it's well clear of #2. Single-playlist libraries naturally
            # hit this; so do users with 3-10 distinctively-named playlists.
            runner_up_score = ranked[1][2] if len(ranked) > 1 else 0
            if (
                top[2] >= _PLAYLIST_BEST_BY_FAR_FLOOR
                and top[2] - runner_up_score >= _PLAYLIST_BEST_BY_FAR_GAP
            ):
                logger.info(
                    "spotify_play: picking %r at score=%d via best-by-far "
                    "(gap to #2 = %d)",
                    top[1], top[2], top[2] - runner_up_score,
                )
                return top[0], "playlist", top[1]

        # Library miss: try Spotify-owned playlists. Discover Weekly /
        # Release Radar / Daily Mix N are owned by 'spotify' and are
        # personalized to the listener when fetched with a user token.
        # Use the normalized query here too — searching Spotify's
        # catalog for "covers playlist" matches differently than
        # "covers".
        spotify_owned = await _spotify_owned_playlist_match(sp, playlist_q)
        if spotify_owned and spotify_owned[2] >= _PLAYLIST_THRESHOLD:
            logger.info(
                "spotify_play: spotify-owned playlist hit %r (score=%d)",
                spotify_owned[1], spotify_owned[2],
            )
            return spotify_owned[0], "playlist", spotify_owned[1]
        return None

    if kind in ("artist", "album"):
        q = f'{kind}:"{safe_q}"'
        try:
            results = await asyncio.to_thread(sp.search, q=q, type=kind, limit=1)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s search failed: %s", kind, e)
            return None
        items = ((results or {}).get(f"{kind}s") or {}).get("items") or []
        if not items or not items[0]:
            return None
        return items[0]["uri"], kind, items[0].get("name") or query

    if kind == "track":
        try:
            results = await asyncio.to_thread(
                sp.search, q=query, type="track", limit=1
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("track search failed: %s", e)
            return None
        items = ((results or {}).get("tracks") or {}).get("items") or []
        if not items or not items[0]:
            return None
        return items[0]["uri"], "track", items[0].get("name") or query

    # kind == "auto" or anything unrecognized — unified resolution.
    artist_q = f'artist:"{safe_q}"'
    album_q = f'album:"{safe_q}"'

    async def _safe_search(q: str, type_: str):
        try:
            return await asyncio.to_thread(sp.search, q=q, type=type_, limit=1)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s search failed: %s", type_, e)
            return None

    artist_res, track_res, album_res, lib_match = await asyncio.gather(
        _safe_search(artist_q, "artist"),
        _safe_search(query, "track"),
        _safe_search(album_q, "album"),
        _user_library_match(sp, query, configured=configured_playlists),
    )

    q_lower = query.lower()
    candidates: list[tuple[str, str, str, int]] = []  # (uri, kind, name, score)

    def _add_top(results, type_):
        if results is None:
            return
        items = ((results or {}).get(f"{type_}s") or {}).get("items") or []
        if not items or not items[0]:
            return
        name = items[0].get("name") or ""
        uri = items[0].get("uri") or ""
        if not uri:
            return
        score = int(fuzz.WRatio(q_lower, name.lower()))
        candidates.append((uri, type_, name, score))

    _add_top(artist_res, "artist")
    _add_top(track_res, "track")
    _add_top(album_res, "album")
    if lib_match is not None:
        candidates.append((lib_match[0], "playlist", lib_match[1], lib_match[2]))

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c[3])
    top_score = candidates[0][3]
    if top_score < _PLAY_THRESHOLD:
        logger.info(
            "spotify_play: no candidate above threshold %d for %r — top=%s",
            _PLAY_THRESHOLD, query,
            [(c[1], c[2], c[3]) for c in candidates[:4]],
        )
        return None

    close = [c for c in candidates if c[3] >= top_score - _TIEBREAK_GAP]
    close.sort(key=lambda c: _TIEBREAK_ORDER.index(c[1]))
    pick = close[0]
    logger.info(
        "spotify_play: resolved %r → %s %r (score=%d, considered=%s)",
        query, pick[1], pick[2], pick[3],
        [(c[1], c[3]) for c in candidates],
    )
    return pick[0], pick[1], pick[2]


def make_spotify_tools(router, renderer, librespot_name: str, setup_url: str = ""):
    """Multi-account-aware Spotify tools.

    `router` is a `jasper.spotify_router.Router`. When AirPlay is
    streaming a track right now, the same title cross-reference
    transport uses picks whose account this is — so "play Beyoncé"
    while a guest is AirPlaying lands on the guest's account, not
    the speaker owner's. When no AirPlay session is in flight (cold
    start), we fall back to whichever account is currently is_playing,
    then to the registry's default.

    `setup_url` is woven into the no-account-configured error so a
    fresh install can answer "play X" requests with a spoken pointer
    to the OAuth setup page. The tools always register (so the LLM
    can call them); when no usable client is available, each call
    short-circuits to a user-facing error that's specific to WHY —
    "needs setup" vs "session expired" — so the user knows whether to
    OAuth fresh or just re-link an existing account.

    The client-availability check happens per call (not at factory
    time): `router.refresh_if_empty()` re-runs `build_clients` if the
    router is currently empty, so re-linking via the web wizard
    recovers the daemon without a manual restart."""
    from ..spotify_router import airplay_client_name
    from .transport import _mpris_now_playing

    def _no_account_msg() -> str:
        """Pick the right user-facing message based on why the router is
        empty. Spoken verbatim by the LLM, so the phrasing is tuned for
        speech: short, no jargon, names the affected account(s) so a
        multi-household speaker tells the user *which* account to re-link
        rather than a generic "your spotify session"."""
        reason = router.empty_reason() if router is not None else "no_accounts"
        if reason == "revoked":
            names = router.revoked_account_names() if router is not None else []
            who = _format_name_list(names) if names else "your spotify account"
            base = f"spotify signed {who} out."
            if setup_url:
                base += f" tell the user to re-link at {setup_url}."
            return base
        base = "no spotify account is configured."
        if setup_url:
            base += f" tell the user to visit {setup_url} to set one up."
        return base

    async def _ensure_clients() -> bool:
        """Per-call client availability check. Returns True iff the
        router has at least one usable client after attempting a
        lazy rebuild. Cheap when clients are already loaded; rate-
        limited inside Router.refresh_if_empty for the empty path."""
        if router is None:
            return False
        if router.clients:
            return True
        return await router.refresh_if_empty()

    async def _resolve_for_play() -> "tuple[object, str | None, list[str], str, dict[str, str]] | None":
        if not await _ensure_clients():
            return None
        """Pick the active account and decide where to start_playback.
        Returns (sp, device_id, stop_renderers, account_name,
        configured_playlists) or None if no account / device combination
        can be reached. `configured_playlists` is a uri→name map populated
        via the web UI for that account (typically empty).

        AirPlay-carrying-Spotify short-circuit: if the title-match already
        identified the AirPlay sender's account, target that account's
        currently-playing Spotify Connect device (the sender's phone)
        directly. start_playback to that device just changes the track
        riding the existing AirPlay stream — no need to stop anything,
        and no dependency on whether the on-Pi librespot endpoint is
        visible to that account. resolve_target's heuristics (which
        re-derive the AirPlay→Spotify match from the renderer's
        currentsong) only run for cold-start cases."""
        renderers = await renderer.active_renderers()
        airplay_active = bool(renderers.get("aplactive"))
        if airplay_active:
            client_name = await airplay_client_name()
            try:
                metadata = await _mpris_now_playing()
                title = metadata.get("title", "")
            except (RuntimeError, asyncio.TimeoutError, FileNotFoundError):
                title = ""
            if client_name and title:
                ac = await router.resolve_for_transport(client_name, title)
                if ac is not None:
                    playback = await asyncio.to_thread(ac.sp.current_playback)
                    device_id = (playback or {}).get("device", {}).get("id")
                    if device_id:
                        return (
                            ac.sp, device_id, [], ac.account.name,
                            dict(getattr(ac.account, "playlists", {}) or {}),
                        )
                    logger.info(
                        "spotify_play: title-match account=%s but current_playback "
                        "has no device_id; falling through to resolve_target",
                        ac.account.name,
                    )
        ac = await router.active(airplay_active=airplay_active)
        if ac is None:
            return None
        resolution = await resolve_target(ac.sp, renderer, librespot_name)
        return (
            ac.sp, resolution.device_id, resolution.stop_renderers, ac.account.name,
            dict(getattr(ac.account, "playlists", {}) or {}),
        )

    @tool()
    async def spotify_play(
        query: str, kind: str = "auto", shuffle: bool = False
    ) -> dict:
        """Search Spotify and start playback for the active account.

        Set `kind` from EXPLICIT user phrasing only — when in doubt, leave
        it as "auto" and the server picks the best match across artists,
        tracks, albums, and the user's saved playlists.

          - "auto" (default): bare "play X" with no qualifier — e.g.
            "play Sufjan Stevens", "play All of the Lights", "play Jaspany Jams".
          - "artist": user explicitly said "the artist X", "songs by X",
            "music by X".
          - "track": user explicitly said "the song X", "the track X",
            or "X by Y" (where X is a song title and Y is the artist).
          - "album": user explicitly said "the album X" or "X album".
          - "playlist": user explicitly said the word "playlist" — e.g.
            "play X playlist", "the playlist X", "my X playlist". Pass
            ONLY the playlist name X as `query` — do NOT include the
            word "playlist" itself ("Covers", not "Covers playlist").
            Including "playlist" in the query biases fuzzy matching
            toward any other playlist whose name happens to contain
            that word. The server will fuzzy-match X against the user's
            saved playlists, tolerant of voice-to-text mishears.

        Set `shuffle=true` when the user explicitly asks for shuffled
        playback — "shuffle X", "play X shuffled", "play X on shuffle".
        Default is `shuffle=false`: playlists play in their stored
        order, artists play their top tracks, albums play in album
        order. Shuffle currently only meaningfully changes playlist
        playback; for artists/albums/tracks the flag is accepted but
        has no effect.

        On success the response includes a `confirm` field — speak that
        sentence verbatim to the user so they hear which artist / song /
        playlist was actually selected. This is especially important for
        playlists, where voice-to-text mishears coined names ("Jaspany
        Jamz" → "Jazz Knee Jams") and the user needs to know whether
        the right thing is now playing.

        Returns an error asking the user to specify when nothing matches
        confidently. The user must re-issue the wake word + command — the
        mic does not stay open for follow-ups."""
        resolved = await _resolve_for_play()
        if resolved is None:
            return {"error": _no_account_msg()}
        sp, device_id, stops, account_name, configured_playlists = resolved
        if not device_id:
            # The speaker's librespot is running and advertising via mDNS
            # but isn't in this account's Spotify Web API device list.
            # That happens when no one has tapped the speaker in their
            # Spotify app since the last librespot restart — pure-zeroconf
            # devices are invisible to the Web API until they've been
            # claimed at least once. The system prompt instructs the model
            # to read the `error` field verbatim, so the message below IS
            # the user-facing fix instruction.
            return {
                "error": (
                    f"Spotify Connect on the speaker isn't linked to your "
                    f"account yet. Open Spotify, tap the device picker, "
                    f"and select {librespot_name} once. Then try again."
                ),
            }

        pick = await _resolve_query(
            sp, query, kind, configured_playlists=configured_playlists,
        )
        if pick is None:
            return {"error": _NOT_UNDERSTOOD}
        uri, resolved_kind, name = pick

        if stops:
            await stop_renderers(renderer, stops)
        if resolved_kind == "track":
            await asyncio.to_thread(
                sp.start_playback, device_id=device_id, uris=[uri]
            )
        elif resolved_kind == "playlist":
            # Standard Spotify playback: set shuffle state, then start
            # the playlist via its context_uri. Spotify Web API has no
            # sort/order parameter; the playlist plays in its native
            # stored order (or shuffled, when shuffle=True). "Newest
            # first" is not an API capability — see commit history.
            try:
                await asyncio.to_thread(
                    sp.shuffle, state=shuffle, device_id=device_id
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("could not set shuffle=%s: %s", shuffle, e)
            await asyncio.to_thread(
                sp.start_playback, device_id=device_id, context_uri=uri,
            )
        else:
            # artist / album: context_uri-only; Spotify picks reasonable
            # ordering (top tracks for artist, track 1 for album).
            await asyncio.to_thread(
                sp.start_playback, device_id=device_id, context_uri=uri
            )

        # User-facing confirmation. The tool description tells the model
        # to speak the `confirm` field verbatim, so playlist matches in
        # particular get an unambiguous "Now playing X" — important
        # because the resolver may pick a name that's spelled or
        # pronounced differently than the user expected (e.g. "Jaspany
        # Jamz" with a Z).
        if resolved_kind == "playlist":
            confirm = (
                f"Shuffling your {name} playlist."
                if shuffle else
                f"Now playing your {name} playlist."
            )
        else:
            confirm = {
                "track": f"Playing {name}.",
                "artist": f"Playing top tracks for {name}.",
                "album": f"Playing the album {name}.",
            }.get(resolved_kind, f"Playing {name}.")

        return {
            "ok": True,
            "playing": name,
            "kind": resolved_kind,
            "account": account_name,
            "shuffle": bool(shuffle),
            "confirm": confirm,
        }

    @tool()
    async def spotify_play_latest_by_artist(artist: str) -> dict:
        """Find a named artist's most recent release (single or album)
        and start playback.

        Use this when the user asks for "the new", "the newest", "the
        latest", or "their newest" song / single / album / release by a
        named artist — e.g. "play the new Rainbow Kitten Surprise song",
        "play the latest Taylor Swift", "play Beyoncé's newest album".

        For any other "play X" request — naming a specific song, artist,
        album, or playlist without a recency qualifier — call
        `spotify_play` instead. `spotify_play` does catalog search, which
        has no temporal grounding; "new"/"latest"/"newest" in its query
        is a non-load-bearing word and the wrong track comes back.

        Returns the `confirm` field — speak it verbatim so the user
        hears which release was actually selected. On error, speak the
        `error` field verbatim.
        """
        resolved = await _resolve_for_play()
        if resolved is None:
            return {"error": _no_account_msg()}
        sp, device_id, stops, account_name, _ = resolved
        if not device_id:
            # Same root cause as spotify_play — see comment there.
            return {
                "error": (
                    f"Spotify Connect on the speaker isn't linked to your "
                    f"account yet. Open Spotify, tap the device picker, "
                    f"and select {librespot_name} once. Then try again."
                ),
            }

        safe_artist = artist.replace(chr(34), "")
        try:
            artist_res = await asyncio.to_thread(
                sp.search, q=f'artist:"{safe_artist}"', type="artist", limit=1
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("artist search failed: %s", e)
            return {"error": _NOT_UNDERSTOOD}
        items = ((artist_res or {}).get("artists") or {}).get("items") or []
        if not items or not items[0]:
            return {"error": _NOT_UNDERSTOOD}
        artist_id = items[0].get("id")
        artist_name = items[0].get("name") or artist
        if not artist_id:
            return {"error": _NOT_UNDERSTOOD}

        try:
            albums_res = await asyncio.to_thread(
                sp.artist_albums,
                artist_id,
                include_groups="single,album",
                limit=50,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("artist_albums failed for %s: %s", artist_name, e)
            return {"error": _NOT_UNDERSTOOD}
        releases = ((albums_res or {}).get("items") or [])
        if not releases:
            return {
                "error": f"couldn't find any releases for {artist_name}."
            }

        # Sort by release_date descending. Spotify returns dates at one
        # of three precisions: "day" (2026-05-15), "month" (2026-05), or
        # "year" (2026). Pad less-specific values to the START of their
        # period so a year-only "2026" can't accidentally outrank a more
        # specific newer release like "2026-05-15" via lexicographic
        # comparison ("2026" sorts BEFORE "2026-01-01" in plain string
        # compare because shorter-prefix wins on equal prefix).
        def _sort_key(r: dict) -> str:
            date_str = r.get("release_date") or "0000-01-01"
            precision = r.get("release_date_precision") or "day"
            if precision == "year":
                return f"{date_str}-01-01"
            if precision == "month":
                return f"{date_str}-01"
            return date_str

        releases.sort(key=_sort_key, reverse=True)
        newest = releases[0]
        uri = newest.get("uri")
        name = newest.get("name") or artist_name
        # album_type ∈ {"album", "single"} (compilation excluded via
        # include_groups). Used to phrase the spoken confirmation
        # naturally — "Playing X, the newest single from Y".
        album_type = newest.get("album_type") or "release"
        if not uri:
            return {"error": _NOT_UNDERSTOOD}

        if stops:
            await stop_renderers(renderer, stops)
        await asyncio.to_thread(
            sp.start_playback, device_id=device_id, context_uri=uri
        )

        confirm = f"Playing {name}, the newest {album_type} from {artist_name}."
        return {
            "ok": True,
            "playing": name,
            "artist": artist_name,
            "kind": album_type,
            "release_date": newest.get("release_date"),
            "account": account_name,
            "confirm": confirm,
        }

    @tool()
    async def spotify_queue(query: str) -> dict:
        """Search Spotify for a track and add it to the playback queue."""
        resolved = await _resolve_for_play()
        if resolved is None:
            return {"error": _no_account_msg()}
        sp, device_id, _, account_name, _ = resolved
        if not device_id:
            # Same root cause as spotify_play — see comment there.
            return {
                "error": (
                    f"Spotify Connect on the speaker isn't linked to your "
                    f"account yet. Open Spotify, tap the device picker, "
                    f"and select {librespot_name} once. Then try again."
                ),
            }
        results = await asyncio.to_thread(sp.search, q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return {"error": f"no track found for: {query}"}
        await asyncio.to_thread(
            sp.add_to_queue, items[0]["uri"], device_id=device_id
        )
        return {
            "ok": True,
            "queued": items[0].get("name", query),
            "account": account_name,
        }

    return [spotify_play, spotify_play_latest_by_artist, spotify_queue]
