# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.busctl import BusctlResult, run_busctl
from jasper.tools.transport import (
    _airplay_remote_available,
    _mpris_call,
    _detect_source,
    make_transport_dispatcher,
    make_transport_tools,
)
from jasper.tools.spotify import no_account_msg
from tests._spotify_tool_fakes import FakeAccountClient, FakeRenderer, FakeRouter, FakeSpotify

_TWO_DEVICES = {
    "devices": [
        {"id": "dev1", "name": "iPhone", "is_active": True},
        {"id": "dev2", "name": "Pi", "is_active": False},
    ]
}


def _by_name(tools):
    return {f.__name__: f for f in tools}


# --- _detect_source ---


@pytest.mark.parametrize(
    ("renderer_kwargs", "expected"),
    [
        pytest.param({"renderers": {"aplactive": True}}, "airplay", id="airplay"),
        pytest.param({"renderers": {"spotactive": True}}, "spotify", id="spotify"),
        pytest.param({"renderers": {"btactive": True}}, "bluetooth", id="bluetooth"),
        pytest.param(
            {"renderers": {}}, "none", id="returns_none_when_no_renderer_active",
        ),
        pytest.param(
            {
                "renderers": {"aplactive": True, "spotactive": True},
                "selected_source": None,
            },
            "airplay",
            id="airplay_wins_over_others",
        ),
        pytest.param(
            {
                "renderers": {"aplactive": True, "spotactive": True},
                "selected_source": "spotify",
            },
            "spotify",
            id="prefers_mux_selected_source",
        ),
        pytest.param(
            {"renderers": {"usbsinkactive": True}}, "usbsink", id="supports_usbsink",
        ),
        pytest.param(
            {"renderers": {}, "selected_source": "usbsink"}, "usbsink",
            id="prefers_mux_usbsink_winner",
        ),
        pytest.param(
            {
                "renderers": {"btactive": True},
                "selected_source_error": OSError("mux socket unavailable"),
            },
            "bluetooth",
            id="falls_back_when_mux_unavailable",
        ),
    ],
)
def test_detect_source(renderer_kwargs, expected):
    renderer = FakeRenderer(**renderer_kwargs)
    assert asyncio.run(_detect_source(renderer)) == expected


@pytest.mark.parametrize("source", ["spotify", "airplay"])
@pytest.mark.parametrize(
    ("tool_name", "method", "playback"),
    [
        ("next_track", "next_track", None),
        ("previous_track", "previous_track", None),
        ("pause", "pause_playback", None),
        ("resume", "start_playback", None),
        ("toggle", "pause_playback", {"is_playing": True}),
        ("toggle", "start_playback", {"is_playing": False}),
        ("toggle", "start_playback", None),
        ("toggle", "start_playback", OSError("unavailable")),
    ],
)
def test_spotify_transport_commands(source, tool_name, method, playback):
    renderer = FakeRenderer(selected_source=source)
    sp = FakeSpotify(devices=_TWO_DEVICES)
    sp.current_playback = MagicMock(
        return_value=playback,
        side_effect=playback if isinstance(playback, Exception) else None,
    )
    account = FakeAccountClient("jasper", sp)
    router = FakeRouter(
        transport_match=account if source == "airplay" else None,
        active_account=account if source == "spotify" else None,
    )
    tools = _by_name(make_transport_tools(renderer, router))
    dispatch = make_transport_dispatcher(renderer, router)
    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Jasper's Mac"),
    ), patch(
        "jasper.tools.transport.airplay_now_playing",
        new=AsyncMock(return_value={"title": "Hey Jude"}),
    ), patch(
        "jasper.tools.transport._mpris_call", new=AsyncMock(),
    ) as mpris:
        result = asyncio.run(
            dispatch("toggle") if tool_name == "toggle" else tools[tool_name]()
        )
    for name in ("next_track", "previous_track", "pause_playback", "start_playback"):
        if name == method:
            getattr(sp, name).assert_called_once_with(device_id="dev1")
        else:
            getattr(sp, name).assert_not_called()
    assert sp.current_playback.call_count == (tool_name == "toggle")
    mpris.assert_not_awaited()
    assert result == {
        "ok": True,
        "source": "airplay+spotify" if source == "airplay" else "spotify",
        "account": "jasper",
    }


@pytest.mark.parametrize("source", ["airplay", "bluetooth"])
@pytest.mark.parametrize(
    ("tool_name", "method"),
    [
        ("next_track", "Next"),
        ("previous_track", "Previous"),
        ("pause", "Pause"),
        ("resume", "Play"),
        ("toggle", "PlayPause"),
    ],
)
def test_native_transport_commands(source, tool_name, method):
    renderer = FakeRenderer(selected_source=source)
    router = FakeRouter(transport_match=None)
    tools = _by_name(make_transport_tools(renderer, router))
    dispatch = make_transport_dispatcher(renderer, router)
    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Some Mac"),
    ), patch(
        "jasper.tools.transport.airplay_now_playing",
        new=AsyncMock(return_value={"title": "Apple Music Track"}),
    ), patch(
        "jasper.tools.transport.run_busctl",
        new=AsyncMock(side_effect=[BusctlResult(0, b"b true\n", b""), BusctlResult(0, b"", b"")]),
    ) as busctl, patch(
        "jasper.tools.transport._bluetooth_call", new=AsyncMock(),
    ) as bluetooth:
        result = asyncio.run(
            dispatch("toggle") if tool_name == "toggle" else tools[tool_name]()
        )
    if source == "airplay":
        assert [call.args for call in busctl.await_args_list] == [
            ("get-property", "org.gnome.ShairportSync", "/org/gnome/ShairportSync",
             "org.gnome.ShairportSync.RemoteControl", "Available"),
            ("call", "org.mpris.MediaPlayer2.ShairportSync", "/org/mpris/MediaPlayer2",
             "org.mpris.MediaPlayer2.Player", method),
        ]
        assert all(call.kwargs["timeout"] == 2.0 for call in busctl.await_args_list)
        bluetooth.assert_not_awaited()
    else:
        bluetooth.assert_awaited_once_with(method)
        busctl.assert_not_awaited()
    assert result == {"ok": True, "source": source}


def test_dispatch_airplay_no_match_no_dacp_returns_error():
    renderer = FakeRenderer(renderers={"aplactive": True})
    router = FakeRouter(transport_match=None)
    tools = _by_name(make_transport_tools(renderer, router))

    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Some Mac"),
    ), patch(
        "jasper.tools.transport.airplay_now_playing",
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


@pytest.mark.parametrize("router", [
    None,
    FakeRouter(empty_reason="no_accounts"),
    FakeRouter(empty_reason="revoked", revoked_names=["jasper", "brittany"]),
], ids=["no-router", "no-accounts", "revoked"])
def test_dispatch_spotify_without_an_account_speaks_the_spotify_tools_answer(
    monkeypatch, router,
):
    monkeypatch.setattr("jasper.tools.transport.resolve_hostname", lambda: "jts.local")
    renderer = FakeRenderer(renderers={"spotactive": True})
    tools = _by_name(make_transport_tools(renderer, router))
    result = asyncio.run(tools["pause"]())
    assert result == {"error": no_account_msg(router, "jts.local/spotify")}


def test_dispatch_spotify_lazy_rebuild_recovers():
    """The wizard re-link landed mid-call: voice command issued while
    router.clients is empty triggers refresh_if_empty, which now finds
    a usable client. Transport routes to the rebuilt account.

    This is the "no daemon restart required after re-link" promise
    applied to the transport tool path."""
    renderer = FakeRenderer(renderers={"spotactive": True})
    sp = FakeSpotify(devices=_TWO_DEVICES)
    rebuilt = FakeAccountClient("jasper", sp)
    router = FakeRouter(
        active_account=None,
        empty_reason="revoked",
        rebuild_clients={"jasper": rebuilt},
    )
    tools = _by_name(make_transport_tools(renderer, router))
    result = asyncio.run(tools["pause"]())
    assert result.get("ok") is True
    assert result.get("source") == "spotify"
    assert router.refresh_calls == 1


def test_dispatch_no_source_returns_nothing_playing_error():
    renderer = FakeRenderer(renderers={})
    tools = _by_name(make_transport_tools(renderer, None))
    result = asyncio.run(tools["pause"]())
    assert "error" in result
    assert "nothing is playing" in result["error"].lower()
    assert result["source"] == "none"


def test_dispatch_failures_return_error_dict():
    renderer = FakeRenderer(renderers={"aplactive": True})
    sp = FakeSpotify(devices=_TWO_DEVICES)
    sp.next_track = MagicMock(side_effect=RuntimeError("network down"))
    matched = FakeAccountClient("jasper", sp)
    router = FakeRouter(transport_match=matched)
    tools = _by_name(make_transport_tools(renderer, router))
    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Jasper's Mac"),
    ), patch(
        "jasper.tools.transport.airplay_now_playing",
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


def test_dispatch_bluetooth_avrcp_failure_returns_error():
    renderer = FakeRenderer(renderers={"btactive": True})
    dispatch = make_transport_dispatcher(renderer, None)
    with patch(
        "jasper.tools.transport._bluetooth_call",
        new=AsyncMock(side_effect=RuntimeError("no player")),
    ):
        result = asyncio.run(dispatch("pause"))
    assert "error" in result
    assert "no player" in result["error"]


def test_dispatch_usbsink_returns_host_owned_error():
    renderer = FakeRenderer(renderers={"usbsinkactive": True})
    dispatch = make_transport_dispatcher(renderer, None)
    result = asyncio.run(dispatch("pause"))
    assert result["source"] == "usbsink"
    assert "host computer" in result["error"]


# --- get_now_playing ---


def test_get_now_playing_routes_to_airplay_mpris_when_no_match():
    renderer = FakeRenderer(renderers={"aplactive": True})
    router = FakeRouter(transport_match=None)
    tools = _by_name(make_transport_tools(renderer, router))
    with patch(
        "jasper.tools.transport.airplay_client_name",
        new=AsyncMock(return_value="Some Mac"),
    ), patch(
        "jasper.tools.transport.airplay_now_playing",
        new=AsyncMock(return_value={"title": "T", "artist": "A", "album": "B"}),
    ):
        result = asyncio.run(tools["get_now_playing"]())
    assert result == {"title": "T", "artist": "A", "album": "B", "source": "airplay"}


def test_get_now_playing_returns_empty_when_no_source():
    renderer = FakeRenderer(renderers={})
    tools = _by_name(make_transport_tools(renderer, None))
    result = asyncio.run(tools["get_now_playing"]())
    assert result == {"title": "", "artist": "", "album": "", "source": "none"}


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [(b"b true\n", 0, True), (b"b false\n", 0, False), (b"b true\n", 1, False)],
)
async def test_airplay_remote_available_from_busctl(monkeypatch, stdout, returncode, expected):
    monkeypatch.setattr(
        "jasper.tools.transport.run_busctl",
        AsyncMock(return_value=BusctlResult(returncode, stdout, b"")),
    )
    assert await _airplay_remote_available() is expected


@pytest.mark.parametrize("result", [None, BusctlResult(1, b"", b"denied")])
async def test_transport_busctl_failures(monkeypatch, result):
    monkeypatch.setattr("jasper.tools.transport.run_busctl", AsyncMock(return_value=result))
    assert await _airplay_remote_available() is False
    with pytest.raises(RuntimeError):
        await _mpris_call("Play")


async def test_busctl_timeout_kills_and_reaps_child(monkeypatch):
    process = MagicMock(returncode=None)
    process.communicate = AsyncMock(side_effect=asyncio.Event().wait)
    process.wait = AsyncMock(return_value=-9)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await run_busctl("call", "org.example", timeout=0.001) is None
    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()
