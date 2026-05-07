from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from jasper.tools.spotify import make_spotify_tools


class FakeMoode:
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
        return {f"{type}s": {"items": [{"uri": uri, "name": name}]}}

    def current_user_playlists(self, limit=50):
        return {
            "items": [{"uri": uri, "name": name} for uri, name in self._library]
        }

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
    def __init__(self, transport_match=None, active_account=None) -> None:
        self._transport_match = transport_match
        self._active_account = active_account
        self.clients = {"jasper": active_account or transport_match} if (
            active_account or transport_match
        ) else {}

    async def resolve_for_transport(self, client_name: str, title: str):
        return self._transport_match

    async def active(self, *, airplay_active: bool):
        return self._active_account


def _by_name(tools):
    return {f.__name__: f for f in tools}


# ============================================================
# AirPlay-carrying-Spotify short-circuit
# ============================================================


def test_play_airplay_short_circuits_to_sender_device():
    """When AirPlay is active and the title-match identifies the sender's
    account, start_playback targets that account's currently-playing
    Spotify Connect device (the sender's phone) — NOT moOde's librespot.
    No renderer-stop. This is the bug fix for the 'no spotify target
    device available' error during AirPlay-Spotify play-by-name."""
    sp = FakeSpotify(
        playback={
            "is_playing": True,
            "device": {"id": "iphone-spotify-id", "name": "iPhone"},
            "item": {"name": "Hey Jude", "artists": [{"name": "The Beatles"}]},
        },
        # Note: moOde's librespot is NOT in this account's devices list,
        # which is the realistic scenario that broke before the fix.
        devices={"devices": [{"id": "iphone-spotify-id", "name": "iPhone"}]},
        search_results={"artist": ("spotify:artist:xyz", "Kanye West")},
    )
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched)
    moode = FakeMoode(
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
        tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={"track": ("spotify:track:abc", "Hey Jude")},
    )
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched, active_account=matched)
    moode = FakeMoode(
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
        new=lambda *a, **k: _coro_return(_FakeResolution("moode-id", [])),
    ):
        tools = _by_name(make_spotify_tools(router, moode, "moode"))
        result = asyncio.run(tools["spotify_play"](query="Hey Jude", kind="track"))

    assert result.get("ok") is True
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs["device_id"] == "moode-id"


def test_play_no_airplay_uses_resolve_target():
    """When AirPlay isn't active, the short-circuit doesn't fire and we
    go through resolve_target as before."""
    sp = FakeSpotify(
        playback=None,
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={"track": ("spotify:track:abc", "Hey Jude")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    result = asyncio.run(tools["spotify_play"](query="Hey Jude", kind="track"))

    assert result.get("ok") is True
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs["device_id"] == "moode-id"
    assert kwargs.get("uris") == ["spotify:track:abc"]


# ============================================================
# Field-qualifier search (artist/album)
# ============================================================


def test_artist_search_uses_field_qualifier():
    """Artist searches use `artist:"X"` so STRFKR doesn't outrank Matt and
    Kim just because STRFKR has a song titled 'Matt & Kim'."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={"artist": ("spotify:artist:matt-and-kim-real", "Matt and Kim")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    result = asyncio.run(tools["spotify_play"](query="Matt and Kim", kind="artist"))

    assert result.get("ok") is True
    assert result.get("playing") == "Matt and Kim"
    assert sp.last_search_q == 'artist:"Matt and Kim"'


def test_album_search_uses_field_qualifier():
    sp = FakeSpotify(
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={"album": ("spotify:album:abc", "Some Album")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    asyncio.run(tools["spotify_play"](query="Grace", kind="album"))
    assert sp.last_search_q == 'album:"Grace"'


def test_track_search_passes_query_through_unqualified():
    """Track queries stay unqualified so 'Daylight by Matt and Kim' still
    works — a `track:` filter would zero-result that."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={"track": ("spotify:track:abc", "Daylight")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    asyncio.run(tools["spotify_play"](query="Daylight by Matt and Kim", kind="track"))
    assert sp.last_search_q == "Daylight by Matt and Kim"


def test_artist_search_strips_stray_quotes():
    """Defensive: a stray double-quote breaks the field syntax. Strip them."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={"artist": ("spotify:artist:abc", "X")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={
            "artist": ("spotify:artist:sufjan", "Sufjan Stevens"),
            "track": ("spotify:track:tswift", "Cardigan"),  # nonsense top track hit
            "album": ("spotify:album:carrie", "Carrie & Lowell"),
        },
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={
            "artist": ("spotify:artist:matt-and-kim", "Matt and Kim"),
            "track": ("spotify:track:strfkr-song", "Matt and Kim"),  # STRFKR's homonym song
        },
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    result = asyncio.run(tools["spotify_play"](query="Matt and Kim"))

    assert result.get("kind") == "artist"
    sp.start_playback.assert_called_once()
    _, kwargs = sp.start_playback.call_args
    assert kwargs.get("context_uri") == "spotify:artist:matt-and-kim"


def test_auto_picks_track_when_no_artist_with_that_name():
    """'play All of the Lights' — no artist by that name, track wins."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={
            "track": ("spotify:track:lights", "All of the Lights"),
            # No artist or album with that exact name.
        },
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={},  # empty Spotify-search results
        library=[("spotify:playlist:jaspany", "Jaspany Jams")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={
            "artist": ("spotify:artist:rando", "Some Random Artist"),
            "track": ("spotify:track:rando", "Random Song"),
        },
        library=[("spotify:playlist:p1", "Workout Mix")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[
            ("spotify:playlist:jaspany", "Jaspany Jams"),
            ("spotify:playlist:other", "Workout Mix"),
        ],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[("spotify:playlist:jaspany", "Jaspany Jamz")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[("spotify:playlist:jaspany", "Jaspany Jamz")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={"track": ("spotify:track:abc", "Hey Jude")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    result = asyncio.run(tools["spotify_play"](query="Hey Jude", kind="track"))
    assert "Hey Jude" in result.get("confirm", "")


def test_playlist_kind_best_by_far_picks_top_below_threshold():
    """Voice-to-text mishears badly: 'Jaspany Jams' → 'Jazz Knee Jams'.
    Top library match scores below the absolute threshold but is well
    clear of the runner-up — take it as the clear winner."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[
            ("spotify:playlist:jaspany", "Jaspany Jams"),
            ("spotify:playlist:cardio", "Cardio Mix"),
            ("spotify:playlist:reading", "Reading Music"),
        ],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[
            ("spotify:playlist:a", "Jasper's A Jams"),
            ("spotify:playlist:b", "Jasper's B Jams"),
            ("spotify:playlist:c", "Jasper's C Jams"),
        ],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={"playlist": ("spotify:playlist:public", "Today's Top Hits")},
        library=[],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    result = asyncio.run(
        tools["spotify_play"](query="Today's Top Hits", kind="playlist")
    )

    assert "error" in result
    assert "didn't understand" in result["error"]
    sp.start_playback.assert_not_called()


def test_playlist_kind_returns_clarification_on_no_match():
    """Library miss + public miss → clarification error."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        search_results={},
        library=[("spotify:playlist:other", "Workout Mix")],
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[("spotify:playlist:other", "Workout Mix")],
    )
    active = FakeAccountClient(
        "jasper", sp,
        playlists={"spotify:playlist:dw_jasper": "Discover Weekly"},
    )
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[("spotify:playlist:user_copy", "Discover Weekly")],
    )
    active = FakeAccountClient(
        "jasper", sp,
        playlists={"spotify:playlist:real_dw": "Discover Weekly"},
    )
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
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
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[],
        search_results={},  # no artist/track/album hits for 'Discover Weekly'
    )
    active = FakeAccountClient(
        "jasper", sp,
        playlists={"spotify:playlist:dw": "Discover Weekly"},
    )
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    result = asyncio.run(tools["spotify_play"](query="Discover Weekly"))  # auto

    assert result.get("ok") is True
    assert result.get("kind") == "playlist"
    assert result.get("playing") == "Discover Weekly"


def test_no_configured_playlist_does_not_break_existing_paths():
    """Defensive: an account with no configured playlists (the common
    case) behaves exactly like before — library lookup only."""
    sp = FakeSpotify(
        devices={"devices": [{"id": "moode-id", "name": "Moode jasper"}]},
        library=[("spotify:playlist:jaspany", "Jaspany Jams")],
    )
    active = FakeAccountClient("jasper", sp)  # no playlists kwarg → default {}
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    tools = _by_name(make_spotify_tools(router, moode, "moode"))
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
    router = FakeRouter()
    moode = FakeMoode()
    tools = _by_name(make_spotify_tools(
        router, moode, "moode", setup_url="https://jts.local/spotify",
    ))
    assert set(tools.keys()) == {"spotify_play", "spotify_queue"}
    play_result = asyncio.run(tools["spotify_play"](query="Ariana Grande"))
    assert "no spotify account configured" in play_result["error"]
    assert "https://jts.local/spotify" in play_result["error"]
    queue_result = asyncio.run(tools["spotify_queue"](query="Anti-Hero"))
    assert "no spotify account configured" in queue_result["error"]
    assert "https://jts.local/spotify" in queue_result["error"]


def test_no_clients_no_setup_url_omits_url_phrase():
    """Defensive: if setup_url isn't configured (shouldn't happen in
    practice — config.py provides a default — but tests shouldn't
    crash on the empty case), the error message stays sane."""
    router = FakeRouter()
    moode = FakeMoode()
    tools = _by_name(make_spotify_tools(router, moode, "moode"))
    play_result = asyncio.run(tools["spotify_play"](query="X"))
    assert play_result["error"] == "no spotify account configured."


def test_no_device_id_returns_device_error_before_search():
    """When no device is reachable, fail-fast with the device error
    rather than burning a Spotify search call."""
    sp = FakeSpotify(
        devices={"devices": []},
        search_results={"artist": ("spotify:artist:abc", "X")},
    )
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    moode = FakeMoode(renderers={}, currentsong={})

    with patch(
        "jasper.tools.spotify.resolve_target",
        new=lambda *a, **k: _coro_return(_FakeResolution(None, [])),
    ):
        tools = _by_name(make_spotify_tools(router, moode, "moode"))
        result = asyncio.run(tools["spotify_play"](query="X", kind="artist"))

    assert "error" in result
    assert "no spotify target device available" in result["error"]
    assert sp.last_search_q is None


# ============================================================
# helpers
# ============================================================


class _FakeResolution:
    def __init__(self, device_id, stops):
        self.device_id = device_id
        self.stop_renderers = stops
        self.reason = ""


async def _coro_return(value):
    return value
