# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared renderer, account, and router doubles for Spotify tools."""

from __future__ import annotations

from unittest.mock import MagicMock


class FakeRenderer:
    def __init__(
        self,
        renderers=None,
        currentsong=None,
        selected_source=None,
        selected_source_error=None,
    ) -> None:
        self._renderers = renderers or {}
        self._currentsong = currentsong or {}
        self._selected_source = selected_source
        self._selected_source_error = selected_source_error

    async def active_renderers(self) -> dict:
        return self._renderers

    async def get_currentsong(self) -> dict:
        return self._currentsong

    async def selected_source(self):
        if self._selected_source_error is not None:
            raise self._selected_source_error
        return self._selected_source


class FakeAccountClient:
    def __init__(self, name: str, sp, playlists=None) -> None:
        self.account = MagicMock()
        self.account.name = name
        self.account.playlists = playlists if playlists is not None else {}
        self.sp = sp


class FakeRouter:
    def __init__(
        self,
        transport_match=None,
        active_account=None,
        empty_reason: str = "no_accounts",
        rebuild_clients=None,
        revoked_names=None,
        *,
        populate_clients: bool = True,
    ) -> None:
        self._transport_match = transport_match
        self._active_account = active_account
        self.clients = (
            {"jasper": active_account or transport_match}
            if populate_clients and (active_account or transport_match)
            else {}
        )
        self._empty_reason = empty_reason
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
            if not self._active_account:
                self._active_account = next(iter(self.clients.values()))
            return True
        return False

    def empty_reason(self) -> str:
        return "" if self.clients else self._empty_reason

    def revoked_account_names(self) -> list:
        return list(self._revoked_names)


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
        self.next_track = MagicMock()
        self.previous_track = MagicMock()
        self.pause_playback = MagicMock()
        self.last_search_q: str | None = None

    def current_playback(self):
        return self._playback

    def devices(self):
        return self._devices

    def search(self, q, type, limit):
        self.last_search_q = q
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
