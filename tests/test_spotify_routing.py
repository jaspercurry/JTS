# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.spotify_routing import _match_track, _normalise, _find_librespot_id


def _spotify(title: str, artist: str, is_playing: bool = True) -> dict:
    return {
        "is_playing": is_playing,
        "device": {"id": "phone-id"},
        "item": {"name": title, "artists": [{"name": artist}]},
    }


def test_normalise_strips_articles_and_punctuation():
    assert _normalise("The Beatles") == _normalise("Beatles")
    assert _normalise("Hey Jude") == _normalise("HEY JUDE!")
    assert _normalise("Float On") == _normalise("Float  On")
    assert _normalise("") == ""
    assert _normalise("A Hard Day's Night") == _normalise("Hard Day s Night")


def test_match_track_exact_match():
    airplay = {"title": "Hey Jude", "artist": "The Beatles"}
    spotify = _spotify("Hey Jude", "The Beatles")
    assert _match_track(airplay, spotify) is True


def test_match_track_normalisation_title_only():
    """Title comparison is case/punctuation-insensitive after normalisation.
    Artist disagreement is OK now — title-only matching."""
    airplay = {"title": "HEY JUDE!", "artist": "Some Cover Band"}
    spotify = _spotify("Hey Jude", "The Beatles")
    assert _match_track(airplay, spotify) is True


def test_match_track_remaster_suffix_still_matches():
    """Real-world: AirPlay reports the album-version title with no suffix,
    Spotify reports the same track from a remastered release. Title-only
    matching tolerates this; both-must-match would have false-negatived."""
    airplay = {"title": "Hey Jude", "artist": "The Beatles"}
    spotify = _spotify("Hey Jude", "The Beatles - Remastered 2015")
    assert _match_track(airplay, spotify) is True


def test_match_track_different_song_returns_false():
    airplay = {"title": "Float On", "artist": "Modest Mouse"}
    spotify = _spotify("Hey Jude", "The Beatles")
    assert _match_track(airplay, spotify) is False


def test_match_track_spotify_paused_returns_false():
    """Spotify session exists but is_playing=False should not match — the
    user might be AirPlaying something else and have a stale paused
    Spotify session with the same song title coincidentally."""
    airplay = {"title": "Hey Jude", "artist": "The Beatles"}
    spotify = _spotify("Hey Jude", "The Beatles", is_playing=False)
    assert _match_track(airplay, spotify) is False


def test_match_track_missing_metadata_returns_false():
    """AirPlay sources without metadata (e.g. some YouTube setups) shouldn't
    falsely match against any random Spotify session."""
    airplay = {}
    spotify = _spotify("Hey Jude", "The Beatles")
    assert _match_track(airplay, spotify) is False


def test_match_track_spotify_no_playback_returns_false():
    airplay = {"title": "Hey Jude", "artist": "The Beatles"}
    assert _match_track(airplay, None) is False


def test_match_track_handles_capital_field_keys():
    """Renderer metadata fields can vary in casing across sources."""
    airplay = {"Title": "Hey Jude"}
    spotify = _spotify("Hey Jude", "The Beatles")
    assert _match_track(airplay, spotify) is True


def test_find_librespot_id_substring_match():
    devices = [
        {"id": "laptop", "name": "Jasper's MacBook"},
        {"id": "renderer", "name": "JTS jasper"},
        {"id": "phone", "name": "iPhone"},
    ]
    assert _find_librespot_id(devices, "JTS") == "renderer"
    assert _find_librespot_id(devices, "JTS") == "renderer"
    assert _find_librespot_id(devices, "jasper") == "laptop"  # first match wins
    assert _find_librespot_id(devices, "kitchen") is None


def test_find_librespot_id_empty_list():
    assert _find_librespot_id([], "renderer") is None


# --- resolve_target branches ---


import pytest

from jasper.spotify_routing import resolve_target


class _FakeSp:
    def __init__(self, playback, devices):
        self._playback = playback
        self._devices = devices

    def current_playback(self):
        return self._playback

    def devices(self):
        return self._devices


class _FakeRenderer:
    def __init__(self, renderers, song):
        self._renderers = renderers
        self._song = song

    async def active_renderers(self):
        return self._renderers

    async def get_currentsong(self):
        return self._song


def _devices(*names_and_ids):
    return {"devices": [{"id": i, "name": n} for n, i in names_and_ids]}


@pytest.mark.asyncio
async def test_resolve_airplay_carrying_spotify_targets_phone():
    sp = _FakeSp(
        playback={
            "is_playing": True,
            "device": {"id": "phone-id"},
            "item": {"name": "Hey Jude", "artists": [{"name": "The Beatles"}]},
        },
        devices=_devices(("iPhone", "phone-id"), ("JTS jasper", "renderer-id")),
    )
    renderer = _FakeRenderer(
        renderers={"aplactive": True},
        song={"title": "Hey Jude", "artist": "The Beatles"},
    )
    r = await resolve_target(sp, renderer, "JTS")
    assert r.device_id == "phone-id"
    assert r.stop_renderers == []
    assert "metadata match" in r.reason


@pytest.mark.asyncio
async def test_resolve_airplay_non_spotify_stops_airplay_targets_librespot():
    sp = _FakeSp(
        playback={"is_playing": False},  # no Spotify playing
        devices=_devices(("JTS jasper", "renderer-id")),
    )
    renderer = _FakeRenderer(
        renderers={"aplactive": True},
        song={"title": "Some Apple Music Track", "artist": "Some Artist"},
    )
    r = await resolve_target(sp, renderer, "JTS")
    assert r.device_id == "renderer-id"
    assert r.stop_renderers == ["airplay"]


@pytest.mark.asyncio
async def test_resolve_bluetooth_stops_bluetooth_targets_librespot():
    sp = _FakeSp(playback=None, devices=_devices(("JTS jasper", "renderer-id")))
    renderer = _FakeRenderer(renderers={"btactive": True}, song={})
    r = await resolve_target(sp, renderer, "JTS")
    assert r.device_id == "renderer-id"
    assert r.stop_renderers == ["bluetooth"]


@pytest.mark.asyncio
async def test_resolve_idle_targets_librespot_no_stop():
    sp = _FakeSp(playback=None, devices=_devices(("JTS jasper", "renderer-id")))
    renderer = _FakeRenderer(renderers={}, song={"state": "stop"})
    r = await resolve_target(sp, renderer, "JTS")
    assert r.device_id == "renderer-id"
    assert r.stop_renderers == []


@pytest.mark.asyncio
async def test_resolve_librespot_active_no_stop():
    """If librespot is already playing on the Pi, target it without
    touching anything else."""
    sp = _FakeSp(
        playback={
            "is_playing": True,
            "device": {"id": "renderer-id"},
            "item": {"name": "X", "artists": [{"name": "Y"}]},
        },
        devices=_devices(("JTS jasper", "renderer-id")),
    )
    renderer = _FakeRenderer(
        renderers={"spotactive": True},
        song={"state": "play", "file": "spotify:track:..."},
    )
    r = await resolve_target(sp, renderer, "JTS")
    assert r.device_id == "renderer-id"
    assert r.stop_renderers == []


@pytest.mark.asyncio
async def test_resolve_no_librespot_visible_returns_none_id():
    """If the Pi's librespot isn't in the devices list, fall back to None
    so the caller can return an actionable error to the user."""
    sp = _FakeSp(playback=None, devices=_devices(("iPhone", "phone-id")))
    renderer = _FakeRenderer(renderers={}, song={})
    r = await resolve_target(sp, renderer, "JTS")
    assert r.device_id is None
    assert r.stop_renderers == []
