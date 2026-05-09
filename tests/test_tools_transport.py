from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from jasper.tools.transport import (
    _detect_source,
    make_transport_dispatcher,
    make_transport_tools,
)


class FakeRenderer:
    def __init__(self, renderers=None, currentsong=None) -> None:
        self._renderers = renderers or {}
        self._currentsong = currentsong or {}
        self.pause_airplay = AsyncMock()

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


class FakeAccountClient:
    """Stand-in for an AccountClient — same .account.name and .sp attrs."""
    def __init__(self, name: str, sp) -> None:
        self.account = MagicMock()
        self.account.name = name
        self.sp = sp


class FakeRouter:
    def __init__(self, transport_match=None, active_account=None) -> None:
        self._transport_match = transport_match
        self._active_account = active_account
        self.clients = {}

    async def resolve_for_transport(self, client_name: str, title: str):
        return self._transport_match

    async def active(self, *, airplay_active: bool):
        return self._active_account


def _by_name(tools):
    return {f.__name__: f for f in tools}


# --- _detect_source ---


def test_detect_source_airplay():
    renderer = FakeRenderer(renderers={"aplactive": True})
    assert asyncio.run(_detect_source(renderer)) == "airplay"


def test_detect_source_spotify():
    renderer = FakeRenderer(renderers={"spotactive": True})
    assert asyncio.run(_detect_source(renderer)) == "spotify"


def test_detect_source_bluetooth():
    renderer = FakeRenderer(renderers={"btactive": True})
    assert asyncio.run(_detect_source(renderer)) == "bluetooth"


def test_detect_source_returns_none_when_no_renderer_active():
    renderer = FakeRenderer(renderers={})
    assert asyncio.run(_detect_source(renderer)) == "none"


def test_detect_source_airplay_wins_over_others():
    renderer = FakeRenderer(renderers={"aplactive": True, "spotactive": True})
    assert asyncio.run(_detect_source(renderer)) == "airplay"


# --- AirPlay dispatch: title-match path ---


def test_dispatch_airplay_title_match_routes_to_account():
    renderer = FakeRenderer(renderers={"aplactive": True})
    sp = FakeSpotify()
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched)
    tools = _by_name(make_transport_tools(renderer, router))

    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Jasper's Mac Studio"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "Hey Jude", "artist": "X", "album": "Y"}),
    ):
        result = asyncio.run(tools["next_track"]())

    sp.next_track.assert_called_once_with(device_id="dev1")
    assert result == {"ok": True, "source": "airplay+spotify", "account": "jasper"}


def test_dispatch_airplay_pause_routes_to_account():
    renderer = FakeRenderer(renderers={"aplactive": True})
    sp = FakeSpotify()
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched)
    tools = _by_name(make_transport_tools(renderer, router))

    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Jasper's iPhone"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "Hey Jude"}),
    ):
        asyncio.run(tools["pause"]())
    sp.pause_playback.assert_called_once_with(device_id="dev1")


# --- AirPlay dispatch: no title match → DACP fallback ---


def test_dispatch_airplay_no_match_falls_back_to_dacp_when_available():
    renderer = FakeRenderer(renderers={"aplactive": True})
    router = FakeRouter(transport_match=None)
    tools = _by_name(make_transport_tools(renderer, router))

    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Some Mac"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "Apple Music Track"}),
    ), patch(
        "jasper.tools.transport._airplay_remote_available",
        new=AsyncMock(return_value=True),
    ), patch(
        "jasper.tools.transport._mpris_call", new=AsyncMock(),
    ) as mpris:
        result = asyncio.run(tools["next_track"]())
    mpris.assert_awaited_once_with("Next")
    assert result == {"ok": True, "source": "airplay"}


def test_dispatch_airplay_no_match_no_dacp_returns_error():
    renderer = FakeRenderer(renderers={"aplactive": True})
    router = FakeRouter(transport_match=None)
    tools = _by_name(make_transport_tools(renderer, router))

    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Some Mac"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "Mystery Track"}),
    ), patch(
        "jasper.tools.transport._airplay_remote_available",
        new=AsyncMock(return_value=False),
    ), patch(
        "jasper.tools.transport._mpris_call", new=AsyncMock(),
    ) as mpris:
        result = asyncio.run(tools["next_track"]())
    mpris.assert_not_awaited()
    assert "error" in result
    assert "spotify" in result["error"].lower()


def test_dispatch_airplay_no_router_falls_back_to_dacp():
    renderer = FakeRenderer(renderers={"aplactive": True})
    tools = _by_name(make_transport_tools(renderer, None))

    with patch(
        "jasper.tools.transport._airplay_remote_available",
        new=AsyncMock(return_value=True),
    ), patch(
        "jasper.tools.transport._mpris_call", new=AsyncMock(),
    ) as mpris:
        result = asyncio.run(tools["next_track"]())
    mpris.assert_awaited_once_with("Next")
    assert result == {"ok": True, "source": "airplay"}


# --- Other source dispatches ---


def test_dispatch_spotify_targets_active_device():
    renderer = FakeRenderer(renderers={"spotactive": True})
    sp = FakeSpotify(active_id="dev1")
    active = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=active)
    tools = _by_name(make_transport_tools(renderer, router))

    result = asyncio.run(tools["next_track"]())
    sp.next_track.assert_called_once_with(device_id="dev1")
    assert result == {"ok": True, "source": "spotify", "account": "jasper"}


def test_dispatch_no_source_returns_nothing_playing_error():
    renderer = FakeRenderer(renderers={})
    tools = _by_name(make_transport_tools(renderer, None))
    result = asyncio.run(tools["pause"]())
    assert "error" in result
    assert "nothing is playing" in result["error"].lower()
    assert result["source"] == "none"


def test_dispatch_bluetooth_returns_unsupported_error():
    renderer = FakeRenderer(renderers={"btactive": True})
    tools = _by_name(make_transport_tools(renderer, None))
    result = asyncio.run(tools["pause"]())
    assert "error" in result
    assert "bluetooth" in result["error"].lower()


def test_resume_aliases_play_action():
    renderer = FakeRenderer(renderers={"aplactive": True})
    sp = FakeSpotify()
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched)
    tools = _by_name(make_transport_tools(renderer, router))

    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Jasper's Mac"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "Hey Jude"}),
    ):
        asyncio.run(tools["resume"]())
    sp.start_playback.assert_called_once_with(device_id="dev1")


def test_dispatch_failures_return_error_dict():
    renderer = FakeRenderer(renderers={"aplactive": True})
    sp = FakeSpotify()
    sp.next_track = MagicMock(side_effect=RuntimeError("network down"))
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched)
    tools = _by_name(make_transport_tools(renderer, router))
    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Jasper's Mac"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "Hey Jude"}),
    ):
        result = asyncio.run(tools["next_track"]())
    assert "error" in result


# --- toggle action ---


def test_toggle_no_source_returns_error():
    renderer = FakeRenderer(renderers={})
    dispatch = make_transport_dispatcher(renderer, None)
    result = asyncio.run(dispatch("toggle"))
    assert "error" in result
    assert result["source"] == "none"


def test_toggle_spotify_pauses_when_playing():
    renderer = FakeRenderer(renderers={"spotactive": True})
    sp = FakeSpotify()
    sp.current_playback = MagicMock(return_value={"is_playing": True})
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=matched)
    dispatch = make_transport_dispatcher(renderer, router)
    result = asyncio.run(dispatch("toggle"))
    sp.pause_playback.assert_called_once_with(device_id="dev1")
    sp.start_playback.assert_not_called()
    assert result["ok"] is True
    assert result["source"] == "spotify"


def test_toggle_spotify_resumes_when_paused():
    renderer = FakeRenderer(renderers={"spotactive": True})
    sp = FakeSpotify()
    sp.current_playback = MagicMock(return_value={"is_playing": False})
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(active_account=matched)
    dispatch = make_transport_dispatcher(renderer, router)
    result = asyncio.run(dispatch("toggle"))
    sp.start_playback.assert_called_once_with(device_id="dev1")
    sp.pause_playback.assert_not_called()
    assert result["ok"] is True


def test_toggle_airplay_with_spotify_match_routes_to_account():
    renderer = FakeRenderer(renderers={"aplactive": True})
    sp = FakeSpotify()
    sp.current_playback = MagicMock(return_value={"is_playing": True})
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched)
    dispatch = make_transport_dispatcher(renderer, router)
    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Jasper's Mac"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "Hey Jude"}),
    ):
        result = asyncio.run(dispatch("toggle"))
    sp.pause_playback.assert_called_once_with(device_id="dev1")
    assert result["source"] == "airplay+spotify"


def test_toggle_airplay_no_match_uses_mpris_playpause():
    """Non-Spotify AirPlay senders → MPRIS PlayPause is the native
    single-call toggle. Beats query+dispatch for browser tabs / Apple
    Music / podcast apps that don't expose is-playing introspection."""
    renderer = FakeRenderer(renderers={"aplactive": True})
    router = FakeRouter(transport_match=None)
    dispatch = make_transport_dispatcher(renderer, router)
    mpris_call = AsyncMock()
    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Some Mac"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "Hey Jude"}),
    ), patch(
        "jasper.tools.transport._airplay_remote_available",
        new=AsyncMock(return_value=True),
    ), patch(
        "jasper.tools.transport._mpris_call",
        new=mpris_call,
    ):
        result = asyncio.run(dispatch("toggle"))
    mpris_call.assert_awaited_once_with("PlayPause")
    assert result == {"ok": True, "source": "airplay"}


def test_toggle_bluetooth_returns_unsupported_error():
    renderer = FakeRenderer(renderers={"btactive": True})
    dispatch = make_transport_dispatcher(renderer, None)
    result = asyncio.run(dispatch("toggle"))
    assert "error" in result


# --- get_now_playing ---


def test_get_now_playing_routes_to_airplay_mpris_when_no_match():
    renderer = FakeRenderer(renderers={"aplactive": True})
    router = FakeRouter(transport_match=None)
    tools = _by_name(make_transport_tools(renderer, router))
    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Some Mac"),
    ), patch(
        "jasper.tools.transport._mpris_now_playing",
        new=AsyncMock(return_value={"title": "T", "artist": "A", "album": "B"}),
    ):
        result = asyncio.run(tools["get_now_playing"]())
    assert result == {"title": "T", "artist": "A", "album": "B", "source": "airplay"}


def test_get_now_playing_returns_empty_when_no_source():
    renderer = FakeRenderer(renderers={})
    tools = _by_name(make_transport_tools(renderer, None))
    result = asyncio.run(tools["get_now_playing"]())
    assert result == {"title": "", "artist": "", "album": "", "source": "none"}
