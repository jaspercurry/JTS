# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from jasper.tools.spotify import make_spotify_tools


class FakeRenderer:
    def __init__(self, renderers=None, currentsong=None) -> None:
        self._renderers = renderers or {}
        self._currentsong = currentsong or {}

    async def active_renderers(self) -> dict:
        return self._renderers

    async def get_currentsong(self) -> dict:
        return self._currentsong


class FakeSpotify:
    """Spotify stand-in.

    `search_results`, when a dict, maps `type` ("artist"/"track"/"album"/
    "playlist") to a single-item top-level result, e.g.
        {"artist": ("spotify:artist:abc", "Sufjan Stevens")}
    `library` is a list of (uri, name) tuples returned by
    current_user_playlists.

    For backwards compatibility, `search_results` also accepts a raw
    Spotify-shaped dict (`{"artists": {"items": [...]}}`) returned for
    every call regardless of type."""

    def __init__(
        self,
        *,
        playback=None,
        devices=None,
        search_results=None,
        library=None,
    ) -> None:
        self._playback = playback
        self._devices = devices or {"devices": []}
        self._search_results = search_results or {}
        self._library = library or []
        self.start_playback = MagicMock()
        self.add_to_queue = MagicMock()
        self.last_search_q: str | None = None
        self.last_search_type: str | None = None

    def current_playback(self):
        return self._playback

    def devices(self):
        return self._devices

    def search(self, q, type, limit):
        self.last_search_q = q
        self.last_search_type = type
        if isinstance(self._search_results, dict) and self._search_results and (
            "artists" in self._search_results
            or "tracks" in self._search_results
            or "albums" in self._search_results
            or "playlists" in self._search_results
        ):
            # Legacy raw shape — returned for every call.
            return self._search_results
        # Type-keyed shape.
        hit = self._search_results.get(type) if isinstance(self._search_results, dict) else None
        if hit is None:
            return {f"{type}s": {"items": []}}
        uri, name = hit
        # Mirror Spotify's real response: both `id` and `uri` are
        # populated. Derive id from the trailing segment of the URI
        # (e.g. spotify:artist:rks → rks).
        item_id = uri.rsplit(":", 1)[-1] if uri else ""
        return {f"{type}s": {"items": [{"uri": uri, "id": item_id, "name": name}]}}

    def current_user_playlists(self, limit=50):
        return {
            "items": [{"uri": uri, "name": name} for uri, name in self._library]
        }

    def artist_albums(self, artist_id, include_groups=None, limit=20):
        """Return a page of releases preconfigured via `with_releases`.

        Mirrors the real Spotify endpoint's hard cap of 10 per page —
        passing limit > 10 RAISES, matching the live API's HTTP 400
        "Invalid limit" response. This is the regression pin for the
        2026-05-22 bug where the tool passed limit=50 (spotipy's
        signature accepted it, but the live API rejected it).
        """
        if limit is None or limit > 10:
            raise ValueError(
                f"FakeSpotify: limit={limit!r} exceeds Spotify's documented "
                f"max=10 for /artists/{{id}}/albums — the live API returns "
                f"HTTP 400 'Invalid limit' here. Use limit<=10 and paginate."
            )
        self.last_artist_albums_id = artist_id
        self.last_artist_albums_include_groups = include_groups
        self.last_artist_albums_limit = limit
        releases = list(getattr(self, "_releases", []))
        page = releases[:limit]
        next_url = "fake://next" if len(releases) > limit else None
        self._remaining_releases = releases[limit:]
        return {"items": page, "next": next_url}

    def next(self, response):
        """Pagination follower used by spotipy. Returns the next slice
        of the configured releases until exhausted."""
        remaining = list(getattr(self, "_remaining_releases", []) or [])
        if not remaining:
            return {"items": [], "next": None}
        limit = getattr(self, "last_artist_albums_limit", 10)
        page = remaining[:limit]
        self._remaining_releases = remaining[limit:]
        next_url = "fake://next" if self._remaining_releases else None
        return {"items": page, "next": next_url}

    def with_releases(self, releases: list) -> "FakeSpotify":
        """Configure the items returned by artist_albums.

        Each entry is a dict in Spotify's shape — minimally:
            {"uri": "spotify:album:abc",
             "name": "X",
             "album_type": "single",        # 'album' | 'single'
             "release_date": "2026-05-20",
             "release_date_precision": "day"}
        """
        self._releases = releases
        return self

    def shuffle(self, state, device_id=None):
        self.last_shuffle_state = state

    def playlist(self, pid, fields=None, market=None, additional_types=None):
        tracks = getattr(self, "_playlist_tracks", {}).get(pid, [])
        items = [
            {"added_at": added, "track": {"uri": uri, "name": uri.rsplit(":", 1)[-1]}}
            for uri, added in tracks
        ]
        return {"tracks": {"items": items, "next": None}}

    def playlist_items(self, pid, fields=None, limit=100, offset=0, additional_types=None):
        # `_playlist_tracks` map: {playlist_id_or_uri_suffix: [(uri, added_at), ...]}
        tracks = getattr(self, "_playlist_tracks", {}).get(pid, [])
        items = [
            {"added_at": added, "track": {"uri": uri, "name": uri.rsplit(":", 1)[-1]}}
            for uri, added in tracks
        ]
        page = items[offset:offset + limit]
        return {
            "items": page,
            "next": None if offset + limit >= len(items) else "next-page",
        }

    def with_playlist_tracks(self, playlist_id: str, tracks: list):
        """Configure the tracks returned by playlist_items for this id."""
        if not hasattr(self, "_playlist_tracks"):
            self._playlist_tracks = {}
        self._playlist_tracks[playlist_id] = tracks
        return self


class FakeAccountClient:
    def __init__(self, name: str, sp, playlists=None) -> None:
        self.account = MagicMock()
        self.account.name = name
        # Real dict so `dict(ac.account.playlists)` round-trips. Without
        # this, MagicMock auto-creates a child mock and dict() barfs.
        self.account.playlists = playlists if playlists is not None else {}
        self.sp = sp


class FakeRouter:
    def __init__(
        self, transport_match=None, active_account=None,
        empty_reason: str = "no_accounts",
        rebuild_clients=None,
        revoked_names=None,
    ) -> None:
        self._transport_match = transport_match
        self._active_account = active_account
        self.clients = {"jasper": active_account or transport_match} if (
            active_account or transport_match
        ) else {}
        self._empty_reason = empty_reason
        # When set, refresh_if_empty() drops these into self.clients
        # so the test can simulate "wizard re-link landed mid-call".
        self._rebuild_clients = rebuild_clients
        self._revoked_names = list(revoked_names or [])
        self.refresh_calls = 0

    async def resolve_for_transport(self, client_name: str, title: str):
        return self._transport_match

    async def active(self, *, airplay_active: bool):
        return self._active_account

    async def refresh_if_empty(self) -> bool:
        self.refresh_calls += 1
        if self.clients:
            return True
        if self._rebuild_clients:
            self.clients = dict(self._rebuild_clients)
            # If a rebuild populated clients, also expose them via
            # active() so the next call resolves cleanly.
            if not self._active_account:
                self._active_account = next(iter(self.clients.values()))
            return True
        return False

    def empty_reason(self) -> str:
        return "" if self.clients else self._empty_reason

    def revoked_account_names(self) -> list:
        return list(self._revoked_names)


def _by_name(tools):
    return {f.__name__: f for f in tools}


# ============================================================
# AirPlay-carrying-Spotify short-circuit
# ============================================================


def test_play_airplay_short_circuits_to_sender_device():
    """When AirPlay is active and the title-match identifies the sender's
    account, start_playback targets that account's currently-playing
    Spotify Connect device (the sender's phone) — NOT the JTS librespot
    endpoint. No renderer-stop. This is the bug fix for the 'no spotify
    target device available' error during AirPlay-Spotify play-by-name."""
    sp = FakeSpotify(
        playback={
            "is_playing": True,
            "device": {"id": "iphone-spotify-id", "name": "iPhone"},
            "item": {"name": "Hey Jude", "artists": [{"name": "The Beatles"}]},
        },
        # Note: the JTS librespot endpoint is NOT in this account's
        # devices list, which is the realistic scenario that broke
        # before the fix.
        devices={"devices": [{"id": "iphone-spotify-id", "name": "iPhone"}]},
        search_results={"artist": ("spotify:artist:xyz", "Kanye West")},
    )
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched)
    renderer = FakeRenderer(
        renderers={"aplactive": True},
        currentsong={"file": "Airplay Active"},
    )

    with patch(
        "jasper.spotify_router.airplay_client_name",
        new=lambda: _coro_return("Jasper's iPhone"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=lambda: _coro_return({"title": "Hey Jude", "artist": "The Beatles"}),
    ):
        tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
        result = asyncio.run(tools["spotify_play"](query="Kanye West", kind="artist"))

    assert result.get("ok") is True
    assert result.get("account") == "jasper"
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs["device_id"] == "iphone-spotify-id"
    assert kwargs.get("context_uri") == "spotify:artist:xyz"


def test_play_airplay_short_circuit_falls_through_when_no_device_id():
    """Defensive: if Spotify reports current_playback with no device.id
    (rare, mid-handoff), fall through to resolve_target rather than
    crashing."""
    sp = FakeSpotify(
        playback={"is_playing": True, "device": {}, "item": {"name": "X"}},
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"track": ("spotify:track:abc", "Hey Jude")},
    )
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched, active_account=matched)
    renderer = FakeRenderer(
        renderers={"aplactive": True},
        currentsong={"file": "Airplay Active"},
    )

    with patch(
        "jasper.spotify_router.airplay_client_name",
        new=lambda: _coro_return("Jasper's iPhone"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=lambda: _coro_return({"title": "Hey Jude", "artist": "The Beatles"}),
    ), patch(
        "jasper.tools.spotify.resolve_target",
        new=lambda *a, **k: _coro_return(_FakeResolution("renderer-id", [])),
    ):
        tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
        result = asyncio.run(tools["spotify_play"](query="Hey Jude", kind="track"))

    assert result.get("ok") is True
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs["device_id"] == "renderer-id"


def test_play_no_airplay_uses_resolve_target():
    """When AirPlay isn't active, the short-circuit doesn't fire and we
    go through resolve_target as before."""
    sp = FakeSpotify(
        playback=None,
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"track": ("spotify:track:abc", "Hey Jude")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Hey Jude", kind="track"))

    assert result.get("ok") is True
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs["device_id"] == "renderer-id"
    assert kwargs.get("uris") == ["spotify:track:abc"]


# ============================================================
# Field-qualifier search (artist/album)
# ============================================================


def test_artist_search_uses_field_qualifier():
    """Artist searches use `artist:"X"` so STRFKR doesn't outrank Matt and
    Kim just because STRFKR has a song titled 'Matt & Kim'."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"artist": ("spotify:artist:matt-and-kim-real", "Matt and Kim")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Matt and Kim", kind="artist"))

    assert result.get("ok") is True
    assert result.get("playing") == "Matt and Kim"
    assert sp.last_search_q == 'artist:"Matt and Kim"'


def test_album_search_uses_field_qualifier():
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"album": ("spotify:album:abc", "Some Album")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    asyncio.run(tools["spotify_play"](query="Grace", kind="album"))
    assert sp.last_search_q == 'album:"Grace"'


def test_track_search_passes_query_through_unqualified():
    """Track queries stay unqualified so 'Daylight by Matt and Kim' still
    works — a `track:` filter would zero-result that."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"track": ("spotify:track:abc", "Daylight")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    asyncio.run(tools["spotify_play"](query="Daylight by Matt and Kim", kind="track"))
    assert sp.last_search_q == "Daylight by Matt and Kim"


def test_track_kind_rejects_unrelated_result():
    """Defense-in-depth relevance gate: a misrouted recency query ("new song
    by X" that should have gone to spotify_play_latest_by_artist) lands on the
    track path and Spotify returns an unrelated track. The WRatio gate refuses
    rather than playing the wrong song. Regression for the 2026-05-23
    "play the new song by Rainbow Kitten Surprise → Headshots" misroute."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"track": ("spotify:track:rando", "Headshots (4r da Locals)")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="new song by Rainbow Kitten Surprise", kind="track")
    )

    assert result.get("ok") is not True
    sp.start_playback.assert_not_called()


def test_track_kind_accepts_relevant_result():
    """The gate is conservative: a genuine track request still plays. The query
    carries extra words ("by ...") but WRatio's partial match keeps a real hit
    well above the loose track threshold."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"track": ("spotify:track:abc", "Clocks")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Clocks by Coldplay", kind="track"))

    assert result.get("ok") is True
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("uris") == ["spotify:track:abc"]


def test_artist_search_strips_stray_quotes():
    """Defensive: a stray double-quote breaks the field syntax. Strip them."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"artist": ("spotify:artist:abc", "X")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    asyncio.run(tools["spotify_play"](query='Foo "Bar"', kind="artist"))
    assert sp.last_search_q == 'artist:"Foo Bar"'


# ============================================================
# kind="auto" — unified resolution across artist/track/album/library
# ============================================================


def test_auto_picks_artist_on_bare_name():
    """'play Sufjan Stevens' with kind=auto: artist exact-name match
    beats whatever Spotify's track-relevance returns (which previously
    surfaced Taylor Swift via popularity ranking on weak track queries)."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={
            "artist": ("spotify:artist:sufjan", "Sufjan Stevens"),
            "track": ("spotify:track:tswift", "Cardigan"),  # nonsense top track hit
            "album": ("spotify:album:carrie", "Carrie & Lowell"),
        },
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Sufjan Stevens"))

    assert result.get("ok") is True
    assert result.get("kind") == "artist"
    assert result.get("playing") == "Sufjan Stevens"
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:artist:sufjan"


def test_auto_tiebreak_artist_beats_track_on_close_score():
    """Both artist and track score 100 ('exact match'). Tiebreak prefers
    artist for bare-name queries."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={
            "artist": ("spotify:artist:matt-and-kim", "Matt and Kim"),
            "track": ("spotify:track:strfkr-song", "Matt and Kim"),  # STRFKR's homonym song
        },
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Matt and Kim"))

    assert result.get("kind") == "artist"
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:artist:matt-and-kim"


def test_auto_picks_track_when_no_artist_with_that_name():
    """'play All of the Lights' — no artist by that name, track wins."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={
            "track": ("spotify:track:lights", "All of the Lights"),
            # No artist or album with that exact name.
        },
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="All of the Lights"))

    assert result.get("kind") == "track"
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("uris") == ["spotify:track:lights"]


def test_auto_picks_user_playlist_on_fuzzy_match():
    """'play Jasperine Jams' (mistranscribed 'Jaspany Jams'): no artist/
    track/album hits, but the user's saved playlist 'Jaspany Jams' fuzzy-
    matches at score ~77, above the 75 threshold for kind=auto."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={},  # empty Spotify-search results
        library=[("spotify:playlist:jaspany", "Jaspany Jams")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Jasperine Jams"))

    assert result.get("ok") is True
    assert result.get("kind") == "playlist"
    assert result.get("playing") == "Jaspany Jams"
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:playlist:jaspany"


def test_auto_returns_clarification_when_nothing_matches():
    """Garbage query: no candidate above threshold → clarification error."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={
            "artist": ("spotify:artist:rando", "Some Random Artist"),
            "track": ("spotify:track:rando", "Random Song"),
        },
        library=[("spotify:playlist:p1", "Workout Mix")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="qwerty asdf"))

    assert "error" in result
    assert "didn't understand" in result["error"]
    assert "artist" in result["error"]
    assert "playlist" in result["error"]
    sp.start_playback.assert_not_called()


# ============================================================
# kind="playlist" — fuzzy library match with looser threshold
# ============================================================


def test_playlist_kind_uses_library_with_loose_threshold():
    """'play my Jasperine Jams playlist' (kind=playlist): library fuzzy
    match should clear the looser playlist threshold (~55) even when the
    auto threshold (~75) would have been borderline."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[
            ("spotify:playlist:jaspany", "Jaspany Jams"),
            ("spotify:playlist:other", "Workout Mix"),
        ],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Jasperine Jams", kind="playlist"))

    assert result.get("ok") is True
    assert result.get("kind") == "playlist"
    assert result.get("playing") == "Jaspany Jams"
    assert "Jaspany Jams" in result.get("confirm", "")
    assert "Now playing" in result.get("confirm", "")


def test_playlist_play_uses_context_uri_in_native_order():
    """Playlist playback hands off to Spotify via context_uri so the
    playlist plays in its stored order. Shuffle is explicitly disabled.
    Spotify's API has no sort/order parameter — there's no honest way
    to play 'newest first' without rolling our own queue, which we
    deliberately don't do."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[("spotify:playlist:jaspany", "Jaspany Jamz")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Jaspany Jamz", kind="playlist")
    )

    assert result.get("ok") is True
    assert result.get("shuffle") is False
    assert sp.last_shuffle_state is False
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:playlist:jaspany"
    assert kwargs.get("uris") is None
    assert "Now playing" in result.get("confirm", "")
    assert "newest" not in result.get("confirm", "")


def test_playlist_play_with_shuffle_enables_shuffle_state():
    """`shuffle=True` flips Spotify's shuffle state on; playback still
    goes through context_uri (Spotify handles randomisation server-side)."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[("spotify:playlist:jaspany", "Jaspany Jamz")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Jaspany Jamz", kind="playlist", shuffle=True)
    )

    assert result.get("ok") is True
    assert result.get("shuffle") is True
    assert sp.last_shuffle_state is True
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:playlist:jaspany"
    assert kwargs.get("uris") is None
    assert "Shuffling" in result.get("confirm", "")


def test_track_play_returns_confirm_field():
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"track": ("spotify:track:abc", "Hey Jude")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Hey Jude", kind="track"))
    assert "Hey Jude" in result.get("confirm", "")


def test_playlist_kind_best_by_far_picks_top_below_threshold():
    """Voice-to-text mishears badly: 'Jaspany Jams' → 'Jazz Knee Jams'.
    Top library match scores below the absolute threshold but is well
    clear of the runner-up — take it as the clear winner."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[
            ("spotify:playlist:jaspany", "Jaspany Jams"),
            ("spotify:playlist:cardio", "Cardio Mix"),
            ("spotify:playlist:reading", "Reading Music"),
        ],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Jazz Knee Jams", kind="playlist")
    )

    # 'Jaspany Jams' should win against 'Cardio Mix' / 'Reading Music' —
    # those are completely unrelated, so the gap between top and #2 is
    # much larger than _PLAYLIST_BEST_BY_FAR_GAP even at low absolute score.
    assert result.get("ok") is True
    assert result.get("playing") == "Jaspany Jams"


def test_playlist_kind_best_by_far_does_not_fire_when_close():
    """Two playlists named similarly to the (mistranscribed) query: top
    score is borderline AND close to #2 — return clarification rather
    than guess. Prevents 'Jaspany Jams' from accidentally beating
    'Jaspeny Jams' when scores are within a few points."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[
            ("spotify:playlist:a", "Jasper's A Jams"),
            ("spotify:playlist:b", "Jasper's B Jams"),
            ("spotify:playlist:c", "Jasper's C Jams"),
        ],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="random gibberish xyz", kind="playlist")
    )

    # All three playlists score similarly low against unrelated query —
    # no clear winner → clarification.
    assert "error" in result
    assert "didn't understand" in result["error"]


def test_playlist_kind_does_not_fall_back_to_public_search():
    """If the user said 'playlist' but their library doesn't match, we do
    NOT fall back to public Spotify playlists — public results are noisy
    (e.g. 'Jaspany Jams' fuzzy-matches strangers' 'Jaslene's Jams' / 'Jazzy
    Jams') and the user almost always meant a personal playlist."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"playlist": ("spotify:playlist:public", "Today's Top Hits")},
        library=[],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Today's Top Hits", kind="playlist")
    )

    assert "error" in result
    assert "didn't understand" in result["error"]
    sp.start_playback.assert_not_called()


def test_playlist_kind_returns_clarification_on_no_match():
    """Library miss + public miss → clarification error."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={},
        library=[("spotify:playlist:other", "Workout Mix")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Some Nonexistent Playlist", kind="playlist")
    )

    assert "error" in result
    assert "didn't understand" in result["error"]


# ============================================================
# Configured (web-UI-pinned) playlist URIs
#
# These cover the per-account `playlists: dict[str, str]` map populated
# via the web UI. The motivating case: Spotify hides algorithmic
# personalised playlists (Discover Weekly, Daily Mix N, Release Radar)
# from `current_user_playlists` AND from owner-filtered catalog search.
# The user pins their personal URIs by hand so name lookup hits them.
# ============================================================


def test_configured_playlist_resolves_when_library_misses():
    """User has manually pinned 'Discover Weekly' for their account.
    Library search returns nothing useful. The configured URI wins."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[("spotify:playlist:other", "Workout Mix")],
    )
    active = FakeAccountClient(
        "jasper", sp,
        playlists={"spotify:playlist:dw_jasper": "Discover Weekly"},
    )
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Discover Weekly", kind="playlist")
    )

    assert result.get("ok") is True
    assert result.get("playing") == "Discover Weekly"
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:playlist:dw_jasper"


def test_configured_playlist_beats_same_named_library_entry():
    """If both the library and the configured map have a 'Discover Weekly'
    (e.g. a user-coined copy + the real pinned URI), the configured one
    wins on ties via stable sort. The user paid the cost of configuring
    it; that's a vote of confidence."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[("spotify:playlist:user_copy", "Discover Weekly")],
    )
    active = FakeAccountClient(
        "jasper", sp,
        playlists={"spotify:playlist:real_dw": "Discover Weekly"},
    )
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Discover Weekly", kind="playlist")
    )

    assert result.get("ok") is True
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:playlist:real_dw"


def test_configured_playlist_picks_up_voice_to_text_mishears():
    """'Disco verbally' for 'Discover Weekly' — fuzzy match against the
    configured name should still catch it via the loose playlist
    threshold + best-by-far rule. This is the same tolerance the library
    path enjoys; configured entries shouldn't be stricter."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[],
    )
    active = FakeAccountClient(
        "jasper", sp,
        playlists={
            "spotify:playlist:dw": "Discover Weekly",
            "spotify:playlist:rr": "Release Radar",
        },
    )
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Discover Weekend", kind="playlist")
    )

    assert result.get("ok") is True
    assert result.get("playing") == "Discover Weekly"


def test_configured_playlist_works_in_auto_kind():
    """User says 'play Discover Weekly' without the word 'playlist'.
    Auto path fans out artist + track + album + library; the
    configured map flows in via the library function. With no
    artist/track/album hits, the playlist match should win on score."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[],
        search_results={},  # no artist/track/album hits for 'Discover Weekly'
    )
    active = FakeAccountClient(
        "jasper", sp,
        playlists={"spotify:playlist:dw": "Discover Weekly"},
    )
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(tools["spotify_play"](query="Discover Weekly"))  # auto

    assert result.get("ok") is True
    assert result.get("kind") == "playlist"
    assert result.get("playing") == "Discover Weekly"


def test_no_configured_playlist_does_not_break_existing_paths():
    """Defensive: an account with no configured playlists (the common
    case) behaves exactly like before — library lookup only."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        library=[("spotify:playlist:jaspany", "Jaspany Jams")],
    )
    active = FakeAccountClient("jasper", sp)  # no playlists kwarg → default {}
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play"](query="Jaspany Jams", kind="playlist")
    )

    assert result.get("ok") is True
    assert result.get("playing") == "Jaspany Jams"


# ============================================================
# Edge cases
# ============================================================


def test_no_clients_still_registers_tools_with_setup_error():
    """When no Spotify accounts are configured, the tools must still
    register (so Gemini can call them) and short-circuit to a spoken
    "go OAuth at <setup_url>" error. Previously returned []; that made
    Gemini fall silent on 'play X' because it had no relevant tool to
    offer."""
    router = FakeRouter()  # empty_reason defaults to "no_accounts"
    renderer = FakeRenderer()
    tools = _by_name(make_spotify_tools(
        router, renderer, "JTS", setup_url="https://jts.local/spotify",
    ))
    assert set(tools.keys()) == {
        "spotify_play", "spotify_play_latest_by_artist", "spotify_queue",
    }
    play_result = asyncio.run(tools["spotify_play"](query="Ariana Grande"))
    assert "no spotify account is configured" in play_result["error"]
    assert "https://jts.local/spotify" in play_result["error"]
    assert "set one up" in play_result["error"]
    queue_result = asyncio.run(tools["spotify_queue"](query="Anti-Hero"))
    assert "no spotify account is configured" in queue_result["error"]
    assert "https://jts.local/spotify" in queue_result["error"]


def test_no_clients_no_setup_url_omits_url_phrase():
    """Defensive: if setup_url isn't configured (shouldn't happen in
    practice — config.py provides a default — but tests shouldn't
    crash on the empty case), the error message stays sane."""
    router = FakeRouter()
    renderer = FakeRenderer()
    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    play_result = asyncio.run(tools["spotify_play"](query="X"))
    assert play_result["error"] == "no spotify account is configured."


def test_revoked_token_returns_signed_out_message_with_account_name():
    """When the router's empty_reason is 'revoked' (Spotify-server-side
    invalidation, surfaced via build_clients failing with invalid_grant),
    the user-facing error should:
      - distinguish "signed out" from "never set up" (different action)
      - name the affected account so a household with multiple accounts
        knows whose to re-link
      - include the re-link URL"""
    router = FakeRouter(empty_reason="revoked", revoked_names=["jasper"])
    renderer = FakeRenderer()
    tools = _by_name(make_spotify_tools(
        router, renderer, "JTS", setup_url="https://jts.local/spotify",
    ))
    play_result = asyncio.run(tools["spotify_play"](query="X"))
    err = play_result["error"]
    assert "signed jasper out" in err, f"got: {err}"
    assert "re-link" in err
    assert "https://jts.local/spotify" in err
    # Explicitly NOT the "set one up" / "configure" phrasing.
    assert "set one up" not in err
    assert "no spotify account" not in err


def test_revoked_multi_account_names_all_revoked():
    """Two-household scenario: both household members' tokens revoked
    at once (e.g. both reset their password in the same week). The
    voice message should name BOTH so the user knows the scope of
    re-linking required."""
    router = FakeRouter(
        empty_reason="revoked",
        revoked_names=["jasper", "brittany"],
    )
    renderer = FakeRenderer()
    tools = _by_name(make_spotify_tools(
        router, renderer, "JTS", setup_url="https://jts.local/spotify",
    ))
    play_result = asyncio.run(tools["spotify_play"](query="X"))
    err = play_result["error"]
    assert "jasper and brittany" in err, f"got: {err}"
    assert "https://jts.local/spotify" in err


def test_revoked_no_names_falls_back_to_generic_account_phrasing():
    """Edge case: revoked reason but statuses didn't surface names
    (shouldn't happen in production but the code path exists). The
    message should still be coherent, not crash or say 'signed  out'."""
    router = FakeRouter(empty_reason="revoked", revoked_names=[])
    renderer = FakeRenderer()
    tools = _by_name(make_spotify_tools(
        router, renderer, "JTS", setup_url="https://jts.local/spotify",
    ))
    play_result = asyncio.run(tools["spotify_play"](query="X"))
    err = play_result["error"]
    assert "your spotify account" in err
    assert "signed" in err
    assert "https://jts.local/spotify" in err


def test_format_name_list_pluralization():
    """English-list join used for voice output. Pin the spoken shape:
    one → bare name; two → 'a and b'; three+ → Oxford comma."""
    from jasper.tools.spotify import _format_name_list
    assert _format_name_list([]) == ""
    assert _format_name_list(["Jasper"]) == "jasper"  # lowercased
    assert _format_name_list(["jasper", "brittany"]) == "jasper and brittany"
    assert _format_name_list(["a", "b", "c"]) == "a, b, and c"
    assert _format_name_list(["a", "b", "c", "d"]) == "a, b, c, and d"


def test_revoked_then_relinked_recovers_without_daemon_restart():
    """End-to-end pin for the recovery path. Uses a REAL `Router` (not
    the FakeRouter) so we test the actual `refresh_if_empty` logic, not
    the test fake's mock of it.

    Scenario: startup build_clients returned nothing (all revoked).
    Between voice command 1 and voice command 2, the user re-links via
    the wizard — modeled here by switching what `rebuild_fn` returns.
    Voice command 2 must pick up the new client without a daemon
    restart.

    A bug in real Router.refresh_if_empty (mutation order, wrong
    rate-limit comparison, statuses-not-propagated) would fail this
    test where the prior FakeRouter version would pass."""
    from jasper.spotify_router import (
        ACCOUNT_OK, ACCOUNT_REVOKED, AccountClient, AccountStatus,
        BuildResult, Router,
    )
    from jasper.accounts import Account

    sp = FakeSpotify(
        devices={"devices": [{"id": "jts-device", "name": "JTS", "is_active": True}]},
        playback={"is_playing": True, "device": {"id": "jts-device"}, "item": {"name": "X"}},
        search_results={"artist": ("spotify:artist:abc", "Beyonce")},
    )

    # rebuild_fn returns "all revoked" first, then "one healthy client"
    # the second time (simulating the wizard re-link landing between
    # calls).
    healthy_ac = AccountClient(account=Account(name="jasper"), sp=sp)
    rebuild_returns = iter([
        BuildResult(
            clients={},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_REVOKED)],
            default_name="jasper",
        ),
        BuildResult(
            clients={"jasper": healthy_ac},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
            default_name="jasper",
        ),
    ])

    def rebuild():
        return next(rebuild_returns)

    # Initial state: startup build returned nothing (we'll prime the
    # router by hand to match the daemon's actual startup behavior).
    router = Router(
        clients={},
        default_name="jasper",
        statuses=[AccountStatus(name="jasper", state=ACCOUNT_REVOKED)],
        rebuild_fn=rebuild,
    )

    renderer = FakeRenderer()
    tools = _by_name(make_spotify_tools(
        router, renderer, "JTS", setup_url="https://jts.local/spotify",
    ))

    # Voice command 1: rebuild_fn returns "still revoked" → tool surfaces
    # the signed-out message naming the account.
    first = asyncio.run(tools["spotify_play"](query="Beyonce"))
    assert "signed jasper out" in first.get("error", "")
    # Voice command 2 must NOT be throttled by the rate-limit — patch
    # _now to advance past the cooldown so this is deterministic.
    with patch("jasper.spotify_router._now",
               side_effect=[1000.0, 1000.0 + 31.0]):
        # First _now() was already consumed by call 1; reset by patching
        # is fine since we're not relying on absolute timestamps.
        # Actually: easier to just bypass via the public api — reset the
        # internal field to None so the next call retries.
        router._last_refresh_attempt = None
        with patch("jasper.tools.spotify.resolve_target") as resolve_mock:
            resolve_mock.return_value = MagicMock(
                device_id="jts-device", stop_renderers=[],
            )
            second = asyncio.run(tools["spotify_play"](query="Beyonce"))
    assert second.get("ok") is True, (
        f"after rebuild succeeded, spotify_play should resolve; got: {second}"
    )
    assert router.clients == {"jasper": healthy_ac}, (
        "real Router.refresh_if_empty should have replaced clients atomically"
    )
    assert router.statuses[0].state == ACCOUNT_OK


def test_no_device_id_returns_device_error_before_search():
    """When no device is reachable, fail-fast with the device error
    rather than burning a Spotify search call."""
    sp = FakeSpotify(
        devices={"devices": []},
        search_results={"artist": ("spotify:artist:abc", "X")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    with patch(
        "jasper.tools.spotify.resolve_target",
        new=lambda *a, **k: _coro_return(_FakeResolution(None, [])),
    ):
        tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
        result = asyncio.run(tools["spotify_play"](query="X", kind="artist"))

    assert "error" in result
    # User-facing fix instruction: tap the device name in the Spotify
    # app to claim librespot. Device name comes from the librespot_name
    # arg passed to make_spotify_tools (here: "JTS").
    assert "Spotify Connect on the speaker isn't linked" in result["error"]
    assert "JTS" in result["error"]
    assert sp.last_search_q is None


# ============================================================
# spotify_queue — hermetic dispatch and error contracts
# ============================================================


def test_queue_adds_first_search_result_to_resolved_device():
    sp = FakeSpotify(
        search_results={"track": ("spotify:track:daylight", "Daylight")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    with patch(
        "jasper.tools.spotify.resolve_target",
        new=lambda *a, **k: _coro_return(_FakeResolution("renderer-id", [])),
    ):
        tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
        result = asyncio.run(tools["spotify_queue"](query="Daylight"))

    assert result == {"ok": True, "queued": "Daylight", "account": "jasper"}
    sp.add_to_queue.assert_called_once_with(
        "spotify:track:daylight", device_id="renderer-id",
    )


def test_queue_returns_no_track_error_without_dispatch():
    sp = FakeSpotify(search_results={})
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    with patch(
        "jasper.tools.spotify.resolve_target",
        new=lambda *a, **k: _coro_return(_FakeResolution("renderer-id", [])),
    ):
        tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
        result = asyncio.run(tools["spotify_queue"](query="Missing Song"))

    assert result == {"error": "no track found for: Missing Song"}
    sp.add_to_queue.assert_not_called()


def test_queue_returns_device_error_before_search():
    sp = FakeSpotify(
        search_results={"track": ("spotify:track:daylight", "Daylight")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    with patch(
        "jasper.tools.spotify.resolve_target",
        new=lambda *a, **k: _coro_return(_FakeResolution(None, [])),
    ):
        tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
        result = asyncio.run(tools["spotify_queue"](query="Daylight"))

    assert "Spotify Connect on the speaker isn't linked" in result["error"]
    assert "JTS" in result["error"]
    assert sp.last_search_q is None
    sp.add_to_queue.assert_not_called()


# ============================================================
# helpers
# ============================================================
# spotify_play_latest_by_artist — "play the new X" by named artist
# ============================================================


def test_latest_picks_most_recent_release_across_singles_and_albums():
    """User asks for the new Rainbow Kitten Surprise song. Catalog has
    a 2024 album and a 2026-05-21 single; the single wins because it's
    most recent. Tool starts playback with the single's URI."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={
            "artist": ("spotify:artist:rks", "Rainbow Kitten Surprise"),
        },
    ).with_releases([
        {
            "uri": "spotify:album:old-lp",
            "name": "Love Hate Music Box",
            "album_type": "album",
            "release_date": "2024-09-13",
            "release_date_precision": "day",
        },
        {
            "uri": "spotify:album:new-single",
            "name": "Rainbow Kitten Surprise New Single",
            "album_type": "single",
            "release_date": "2026-05-21",
            "release_date_precision": "day",
        },
    ])
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play_latest_by_artist"](artist="Rainbow Kitten Surprise"),
    )

    assert result.get("ok") is True
    assert result.get("artist") == "Rainbow Kitten Surprise"
    assert result.get("playing") == "Rainbow Kitten Surprise New Single"
    assert result.get("kind") == "single"
    assert result.get("release_date") == "2026-05-21"

    # Field-qualified artist search so STRFKR's track titled "Rainbow
    # Kitten Surprise" can't outrank the actual band.
    assert sp.last_search_q == 'artist:"Rainbow Kitten Surprise"'
    # Singles + albums only — appears_on / compilation should be
    # excluded (a feature on another artist's album is not "their new
    # release").
    assert sp.last_artist_albums_include_groups == "single,album"

    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:album:new-single"
    assert kwargs.get("device_id") == "renderer-id"

    confirm = result.get("confirm", "")
    assert "Rainbow Kitten Surprise New Single" in confirm
    assert "newest single" in confirm
    assert "Rainbow Kitten Surprise" in confirm


def test_latest_returns_album_phrase_when_newest_release_is_album():
    """Album wins on date → confirm uses 'newest album' phrasing,
    `kind=album`."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"artist": ("spotify:artist:abc", "Taylor Swift")},
    ).with_releases([
        {
            "uri": "spotify:album:newest-lp",
            "name": "Midnights",
            "album_type": "album",
            "release_date": "2026-03-01",
            "release_date_precision": "day",
        },
        {
            "uri": "spotify:album:older-single",
            "name": "Some Single",
            "album_type": "single",
            "release_date": "2025-12-01",
            "release_date_precision": "day",
        },
    ])
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play_latest_by_artist"](artist="Taylor Swift"),
    )

    assert result.get("ok") is True
    assert result.get("kind") == "album"
    assert "newest album" in result.get("confirm", "")


def test_latest_handles_mixed_release_date_precisions():
    """Pad year-only/month-only release_dates to the START of their
    period. A year-only '2026' must NOT outrank a day-precision
    '2026-04-15' — without padding, lexicographic '2026' < '2026-04-15'
    flips the sort and the year-only entry would win wrongly."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"artist": ("spotify:artist:abc", "Old Band")},
    ).with_releases([
        {
            "uri": "spotify:album:year-only",
            "name": "Year Only Release",
            "album_type": "album",
            "release_date": "2026",
            "release_date_precision": "year",
        },
        {
            "uri": "spotify:album:day-precise",
            "name": "Day Precise Single",
            "album_type": "single",
            "release_date": "2026-04-15",
            "release_date_precision": "day",
        },
    ])
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play_latest_by_artist"](artist="Old Band"),
    )

    assert result.get("ok") is True
    assert result.get("playing") == "Day Precise Single"


def test_latest_returns_clarification_when_artist_not_found():
    """No artist matches → return the standard 'didn't understand'
    clarification error. The model speaks this verbatim."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={},  # artist search returns no items
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play_latest_by_artist"](artist="Nonexistent Band"),
    )

    assert "error" in result
    assert "didn't understand" in result["error"]
    sp.start_playback.assert_not_called()


def test_latest_returns_error_when_artist_has_no_releases():
    """Artist resolves but has no singles/albums (e.g. catalog has only
    appears_on/compilation, both excluded). Tell the user, don't crash
    and don't fall back to something unrelated."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"artist": ("spotify:artist:silent", "Silent Artist")},
    ).with_releases([])
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play_latest_by_artist"](artist="Silent Artist"),
    )

    assert "error" in result
    assert "Silent Artist" in result["error"]
    sp.start_playback.assert_not_called()


def test_latest_no_device_returns_device_error_before_search():
    """Same fail-fast contract as spotify_play — if no device is
    reachable, surface the device-linking instruction rather than
    burning the artist-search API call."""
    sp = FakeSpotify(
        devices={"devices": []},
        search_results={"artist": ("spotify:artist:abc", "X")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    with patch(
        "jasper.tools.spotify.resolve_target",
        new=lambda *a, **k: _coro_return(_FakeResolution(None, [])),
    ):
        tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
        result = asyncio.run(
            tools["spotify_play_latest_by_artist"](artist="X"),
        )

    assert "error" in result
    assert "Spotify Connect on the speaker isn't linked" in result["error"]
    assert "JTS" in result["error"]
    assert sp.last_search_q is None


def test_latest_paginates_and_picks_newest_across_pages():
    """The Spotify API caps `limit` at 10/page AND does not document a
    sort order. Both constraints mean we MUST page through everything
    and sort client-side — picking the newest from the first 10 items
    in arbitrary order is wrong.

    This scenario stages 15 releases (two pages of 10) with the newest
    deliberately on the second page. If the tool stopped after one
    page, or didn't sort, it would pick an older release.
    """
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"artist": ("spotify:artist:prolific", "Prolific Artist")},
    ).with_releases([
        # 14 older releases, then the newest at the end of the list
        # (will land on page 2 since the fake pages in stored order).
        {"uri": f"spotify:album:old-{i}", "name": f"Old {i}",
         "album_type": "album", "release_date": f"2022-01-{i:02d}",
         "release_date_precision": "day"}
        for i in range(1, 15)
    ] + [
        {"uri": "spotify:album:newest", "name": "Brand New Drop",
         "album_type": "single", "release_date": "2026-05-21",
         "release_date_precision": "day"},
    ])
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    result = asyncio.run(
        tools["spotify_play_latest_by_artist"](artist="Prolific Artist"),
    )

    assert result.get("ok") is True, result
    assert result.get("playing") == "Brand New Drop"
    assert result.get("release_date") == "2026-05-21"
    # And limit=10 was honored — the fake raises if the tool passes
    # anything higher, so reaching this assertion proves it.
    assert sp.last_artist_albums_limit == 10


def test_latest_limit_must_not_exceed_api_max_of_10():
    """Regression pin for 2026-05-22 bug. The tool MUST request
    limit<=10; the fake's artist_albums raises if not, mirroring the
    live API's HTTP 400 'Invalid limit'. If a future refactor
    silently raises the limit, this test catches it."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "renderer-id", "name": "JTS jasper"}]},
        search_results={"artist": ("spotify:artist:abc", "X")},
    ).with_releases([
        {"uri": "spotify:album:a", "name": "A",
         "album_type": "single", "release_date": "2026-05-01",
         "release_date_precision": "day"},
    ])
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    renderer = FakeRenderer(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, renderer, "JTS"))
    # If the tool passed limit > 10, the fake's artist_albums would
    # raise ValueError, the tool would catch it as a generic Exception
    # and return the _NOT_UNDERSTOOD error — i.e. test would observe
    # error instead of ok=True.
    result = asyncio.run(
        tools["spotify_play_latest_by_artist"](artist="X"),
    )
    assert result.get("ok") is True, (
        f"tool didn't reach playback — likely passed limit>10. "
        f"Result: {result!r}"
    )


def test_latest_no_clients_returns_setup_error():
    """No accounts configured → speak the setup-URL error, don't crash."""
    router = FakeRouter()
    renderer = FakeRenderer()
    tools = _by_name(make_spotify_tools(
        router, renderer, "JTS", setup_url="https://jts.local/spotify",
    ))
    assert "spotify_play_latest_by_artist" in tools
    result = asyncio.run(
        tools["spotify_play_latest_by_artist"](artist="Beyoncé"),
    )
    assert "no spotify account is configured" in result["error"]
    assert "https://jts.local/spotify" in result["error"]


# ============================================================


class _FakeResolution:
    def __init__(self, device_id, stops):
        self.device_id = device_id
        self.stop_renderers = stops
        self.reason = ""


async def _coro_return(value):
    return value
