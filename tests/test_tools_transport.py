# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.bluetooth.avrcp import bluetooth_avrcp_call, bluetooth_player_path
from jasper.tools.transport import (
    _detect_source,
    make_transport_dispatcher,
    make_transport_tools,
)
from tests._spotify_tool_fakes import FakeAccountClient, FakeRenderer
from tests._spotify_tool_fakes import FakeRouter as _SharedFakeRouter

FakeRouter = partial(_SharedFakeRouter, populate_clients=False)


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


def test_dispatch_spotify_revoked_returns_signed_out_message_with_name():
    """When source=spotify and every account is revoked, transport must
    say "signed <name> out" (action-oriented + names the affected
    account) — not "no account configured" (different action)."""
    renderer = FakeRenderer(renderers={"spotactive": True})
    # No active account; empty_reason indicates revoked; name the
    # household member so the LLM can speak it.
    router = FakeRouter(
        active_account=None, empty_reason="revoked",
        revoked_names=["jasper"],
    )
    tools = _by_name(make_transport_tools(renderer, router))
    result = asyncio.run(tools["pause"]())
    assert "error" in result
    assert "signed jasper out" in result["error"]
    assert "re-link" in result["error"]
    # The message must include the speaker hostname so the LLM can read
    # it aloud and the user knows where to go.
    assert "/spotify" in result["error"]


def test_dispatch_spotify_revoked_multi_account_lists_all_names():
    """Two-household scenario via transport tool. Both members' tokens
    revoked; transport names both so the user knows the full re-link
    scope."""
    renderer = FakeRenderer(renderers={"spotactive": True})
    router = FakeRouter(
        active_account=None, empty_reason="revoked",
        revoked_names=["jasper", "brittany"],
    )
    tools = _by_name(make_transport_tools(renderer, router))
    result = asyncio.run(tools["pause"]())
    assert "jasper and brittany" in result["error"]


def test_dispatch_spotify_no_account_returns_old_message():
    """When source=spotify and no accounts are even registered (not
    revoked, just never set up), transport keeps the older message —
    no behavior change for that path."""
    renderer = FakeRenderer(renderers={"spotactive": True})
    router = FakeRouter(active_account=None, empty_reason="no_accounts")
    tools = _by_name(make_transport_tools(renderer, router))
    result = asyncio.run(tools["pause"]())
    assert "error" in result
    assert "no spotify account configured" in result["error"]


def test_dispatch_spotify_lazy_rebuild_recovers():
    """The wizard re-link landed mid-call: voice command issued while
    router.clients is empty triggers refresh_if_empty, which now finds
    a usable client. Transport routes to the rebuilt account.

    This is the "no daemon restart required after re-link" promise
    applied to the transport tool path."""
    renderer = FakeRenderer(renderers={"spotactive": True})
    sp = FakeSpotify(active_id="dev1")
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


def test_bluetooth_player_path_prefers_active_a2dp_device():
    with patch(
        "jasper.bluetooth.avrcp.bluetooth_active_device_path",
        new=AsyncMock(return_value="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"),
    ), patch(
        "jasper.bluetooth.avrcp.bluetooth_player_paths",
        new=AsyncMock(return_value=[
            "/org/bluez/hci0/dev_11_22_33_44_55_66/player0",
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/player0",
        ]),
    ):
        assert asyncio.run(bluetooth_player_path()) == (
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/player0"
        )


def test_bluetooth_player_path_falls_back_to_first_player():
    with patch(
        "jasper.bluetooth.avrcp.bluetooth_active_device_path",
        new=AsyncMock(return_value=None),
    ), patch(
        "jasper.bluetooth.avrcp.bluetooth_player_paths",
        new=AsyncMock(return_value=[
            "/org/bluez/hci0/dev_11_22_33_44_55_66/player0",
        ]),
    ):
        assert asyncio.run(bluetooth_player_path()) == (
            "/org/bluez/hci0/dev_11_22_33_44_55_66/player0"
        )


def test_dispatch_bluetooth_routes_to_bluez_avrcp():
    renderer = FakeRenderer(renderers={"btactive": True})
    tools = _by_name(make_transport_tools(renderer, None))
    with patch(
        "jasper.tools.transport._bluetooth_call",
        new=AsyncMock(),
    ) as bt_call:
        result = asyncio.run(tools["next_track"]())
    bt_call.assert_awaited_once_with("Next")
    assert result == {"ok": True, "source": "bluetooth"}


def test_bluetooth_playpause_uses_status_to_call_pause_when_playing():
    captured = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **_kwargs):
        captured.append(args)
        return _Proc()

    with patch(
        "jasper.bluetooth.avrcp.bluetooth_player_path",
        new=AsyncMock(return_value="/org/bluez/hci0/dev_AA/player0"),
    ), patch(
        "jasper.bluetooth.avrcp.bluetooth_player_status",
        new=AsyncMock(return_value="playing"),
    ), patch(
        "jasper.busctl.asyncio.create_subprocess_exec",
        new=fake_exec,
    ):
        asyncio.run(bluetooth_avrcp_call("PlayPause"))

    assert captured
    assert captured[0][-1] == "Pause"


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


def test_toggle_bluetooth_routes_to_bluez_playpause():
    renderer = FakeRenderer(renderers={"btactive": True})
    dispatch = make_transport_dispatcher(renderer, None)
    with patch(
        "jasper.tools.transport._bluetooth_call",
        new=AsyncMock(),
    ) as bt_call:
        result = asyncio.run(dispatch("toggle"))
    bt_call.assert_awaited_once_with("PlayPause")
    assert result == {"ok": True, "source": "bluetooth"}


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
