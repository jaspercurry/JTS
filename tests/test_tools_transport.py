from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.tools.transport import _detect_source, make_transport_tools


class FakeMoode:
    def __init__(self, renderers=None, currentsong=None) -> None:
        self._renderers = renderers or {}
        self._currentsong = currentsong or {}
        self.next_track = AsyncMock()
        self.previous_track = AsyncMock()
        self.pause = AsyncMock()
        self.play = AsyncMock()

    async def active_renderers(self) -> dict:
        return self._renderers

    async def get_currentsong(self) -> dict:
        return self._currentsong


class FakeSpotify:
    def __init__(self, active_id="dev1") -> None:
        self._active_id = active_id
        self.next_track = MagicMock()
        self.previous_track = MagicMock()
        self.pause_playback = MagicMock()
        self.start_playback = MagicMock()

    def devices(self):
        return {
            "devices": [
                {"id": "dev1", "name": "iPhone", "is_active": True},
                {"id": "dev2", "name": "Pi", "is_active": False},
            ]
        }

    def current_playback(self):
        return None


def _by_name(tools):
    return {f.__name__: f for f in tools}


def test_detect_source_airplay():
    moode = FakeMoode(renderers={"aplactive": True})
    assert asyncio.run(_detect_source(moode)) == "airplay"


def test_detect_source_spotify():
    moode = FakeMoode(renderers={"spotactive": True})
    assert asyncio.run(_detect_source(moode)) == "spotify"


def test_detect_source_bluetooth():
    moode = FakeMoode(renderers={"btactive": True})
    assert asyncio.run(_detect_source(moode)) == "bluetooth"


def test_detect_source_falls_back_to_mpd():
    moode = FakeMoode(renderers={})
    assert asyncio.run(_detect_source(moode)) == "mpd"


def test_detect_source_airplay_wins_over_others():
    moode = FakeMoode(renderers={"aplactive": True, "spotactive": True})
    assert asyncio.run(_detect_source(moode)) == "airplay"


def test_dispatch_airplay_calls_mpris_when_remote_available():
    moode = FakeMoode(renderers={"aplactive": True})
    sp = FakeSpotify()
    tools = _by_name(make_transport_tools(moode, sp))

    with patch(
        "jasper.tools.transport._airplay_remote_available",
        new=AsyncMock(return_value=True),
    ), patch("jasper.tools.transport._mpris_call", new=AsyncMock()) as mpris:
        result = asyncio.run(tools["next_track"]())
    mpris.assert_awaited_once_with("Next")
    assert result == {"ok": True, "source": "airplay"}


def test_dispatch_airplay_pause_maps_to_mpris_pause():
    moode = FakeMoode(renderers={"aplactive": True})
    tools = _by_name(make_transport_tools(moode, FakeSpotify()))
    with patch(
        "jasper.tools.transport._airplay_remote_available",
        new=AsyncMock(return_value=True),
    ), patch("jasper.tools.transport._mpris_call", new=AsyncMock()) as mpris:
        asyncio.run(tools["pause"]())
    mpris.assert_awaited_once_with("Pause")


def test_dispatch_airplay_remote_unavailable_returns_error():
    moode = FakeMoode(renderers={"aplactive": True})
    tools = _by_name(make_transport_tools(moode, FakeSpotify()))
    with patch(
        "jasper.tools.transport._airplay_remote_available",
        new=AsyncMock(return_value=False),
    ), patch("jasper.tools.transport._mpris_call", new=AsyncMock()) as mpris:
        result = asyncio.run(tools["next_track"]())
    mpris.assert_not_awaited()
    assert "error" in result
    assert "remote control" in result["error"].lower()


def test_dispatch_spotify_targets_active_device():
    moode = FakeMoode(renderers={"spotactive": True})
    sp = FakeSpotify(active_id="dev1")
    tools = _by_name(make_transport_tools(moode, sp))
    result = asyncio.run(tools["next_track"]())
    sp.next_track.assert_called_once_with(device_id="dev1")
    assert result == {"ok": True, "source": "spotify"}


def test_dispatch_mpd_uses_moode_methods():
    moode = FakeMoode(renderers={})
    tools = _by_name(make_transport_tools(moode, FakeSpotify()))
    asyncio.run(tools["pause"]())
    moode.pause.assert_awaited_once()


def test_dispatch_bluetooth_returns_unsupported_error():
    moode = FakeMoode(renderers={"btactive": True})
    tools = _by_name(make_transport_tools(moode, FakeSpotify()))
    result = asyncio.run(tools["pause"]())
    assert "error" in result
    assert "bluetooth" in result["error"].lower()


def test_resume_aliases_play_action():
    moode = FakeMoode(renderers={"aplactive": True})
    tools = _by_name(make_transport_tools(moode, FakeSpotify()))
    with patch("jasper.tools.transport._mpris_call", new=AsyncMock()) as mpris:
        asyncio.run(tools["resume"]())
    mpris.assert_awaited_once_with("Play")


def test_get_now_playing_routes_to_airplay_mpris():
    moode = FakeMoode(renderers={"aplactive": True})
    tools = _by_name(make_transport_tools(moode, FakeSpotify()))
    with patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "T", "artist": "A", "album": "B"}),
    ):
        result = asyncio.run(tools["get_now_playing"]())
    assert result == {"title": "T", "artist": "A", "album": "B", "source": "airplay"}


def test_get_now_playing_routes_to_mpd_when_no_renderer():
    moode = FakeMoode(
        renderers={},
        currentsong={"title": "Local Song", "artist": "Local Artist", "album": "X"},
    )
    tools = _by_name(make_transport_tools(moode, FakeSpotify()))
    result = asyncio.run(tools["get_now_playing"]())
    assert result["title"] == "Local Song"
    assert result["source"] == "mpd"


def test_dispatch_failures_return_error_dict():
    moode = FakeMoode(renderers={"aplactive": True})
    tools = _by_name(make_transport_tools(moode, FakeSpotify()))
    with patch(
        "jasper.tools.transport._mpris_call",
        new=AsyncMock(side_effect=RuntimeError("dbus down")),
    ):
        result = asyncio.run(tools["pause"]())
    assert "error" in result
