# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.audio_control.renderer.RendererClient.

Mocks at the I/O boundary: tmp_path-backed librespot state file
(which the --onevent hook would write), asyncio.create_subprocess_exec for
busctl, and the BlueZ A2DP probe.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.audio_control.renderer import (
    RendererClient,
    _parse_mpris_metadata,
)

from tests._librespot_state import write_librespot_state


# ----------------------------------------------------------------------
# RendererClient.active_renderers — mocks each underlying source
# ----------------------------------------------------------------------

@pytest.fixture
def renderer(tmp_path, monkeypatch):
    # Per-test state file path. Tests write fixture state into it
    # (or leave it absent) to control what source_state.spotify_playing
    # observes via active_renderers.
    monkeypatch.setattr(
        "jasper.audio_control.renderer.usbsink_streaming",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "jasper.playback_state.source_state.a2dp_sink_playing",
        AsyncMock(return_value=False),
    )
    return RendererClient(
        librespot_state_path=str(tmp_path / "librespot.state.env"),
    )


def _mock_subprocess(stdout: bytes = b"", returncode: int = 0):
    """Build an asyncio.create_subprocess_exec replacement that returns
    a mock proc with .communicate() / .wait() pre-canned."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    async def fake(*args, **kwargs):
        return proc
    return fake


async def test_active_renderers_all_inactive(renderer):
    # No librespot state file present, busctl empty for AirPlay, no A2DP
    # transport.
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await renderer.active_renderers()
    assert result == {
        "aplactive": False,
        "btactive": False,
        "spotactive": False,
        # The fixture pins fan-in USB activity inactive.
        "usbsinkactive": False,
    }


async def test_active_renderers_reports_fanin_usb_activity(renderer):
    with (
        patch("asyncio.create_subprocess_exec", new=_mock_subprocess(stdout=b"")),
        patch("jasper.audio_control.renderer.usbsink_streaming", new=AsyncMock(return_value=True)),
    ):
        result = await renderer.active_renderers()

    assert result["usbsinkactive"] is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # Mux's own effective answer is the only field read: a manual pin,
        # an auto winner still playing, and mux saying nothing is audible.
        (b'{"mode":"manual","selected_source":"bluetooth",'
         b'"winner":"bluetooth","active_source":"bluetooth"}\n', "bluetooth"),
        (b'{"mode":"auto","selected_source":null,"winner":"airplay",'
         b'"active_source":"airplay"}\n', "airplay"),
        # A stale winner that stopped playing: mux reports idle, and the
        # caller must not be handed the winner behind mux's back.
        (b'{"mode":"auto","selected_source":null,"winner":"airplay",'
         b'"active_source":"idle"}\n', "idle"),
        # Fail-soft: an older STATUS without the field.
        (b'{"mode":"auto","selected_source":null,"winner":"airplay"}\n', None),
    ],
)
async def test_selected_source_reads_mux_effective_source(
    renderer, status, expected,
):
    reader = MagicMock()
    reader.readline = AsyncMock(return_value=status)
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()

    with patch(
        "asyncio.open_unix_connection",
        new=AsyncMock(return_value=(reader, writer)),
    ):
        assert await renderer.selected_source() == expected


async def test_selected_source_times_out_on_stalled_connect(renderer):
    """A wedged mux listener must not hang the connect past its 1s bound.

    VolumeObserver polls selected_source() every tick through a
    cancellation-only chain (_tick -> active_source -> audible_source ->
    here); an unbounded connect would make that loop immortal (#2003)."""
    async def _hang(*_a, **_kw):
        await asyncio.Event().wait()

    with patch("asyncio.open_unix_connection", new=_hang):
        loop = asyncio.get_running_loop()
        start = loop.time()
        result = await asyncio.wait_for(renderer.selected_source(), timeout=10.0)
        elapsed = loop.time() - start
    assert result is None
    assert elapsed < 3.0, (
        f"selected_source() took {elapsed:.1f}s against a stalled listener "
        "-- its connect must raise within its own 1.0s asyncio.timeout "
        "bound, not the test's outer safety net"
    )


async def test_active_renderers_spotify_playing(renderer):
    write_librespot_state(
        renderer._librespot_state_path,
        playing=True, paused=False, stopped=False, uri="spotify:track:X",
    )
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await renderer.active_renderers()
    assert result["spotactive"] is True
    assert result["aplactive"] is False
    assert result["btactive"] is False


async def test_active_renderers_bluetooth_playing(renderer, monkeypatch):
    monkeypatch.setattr(
        "jasper.playback_state.source_state.a2dp_sink_playing",
        AsyncMock(return_value=True),
    )
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await renderer.active_renderers()
    assert result["btactive"] is True
    assert result["spotactive"] is False


async def test_active_renderers_resilient_to_missing_state_file(renderer):
    """If librespot state file is absent (daemon not started yet, or
    session never connected), the spotify probe returns False rather
    than raising — same fail-soft contract as the busctl and BlueZ
    probes. (Direct probe-level coverage lives in test_source_state.py;
    here we just pin the integration behaviour through active_renderers.)"""
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await renderer.active_renderers()
    assert result["spotactive"] is False


# ----------------------------------------------------------------------
# RendererClient.get_currentsong — the audible source's track
# ----------------------------------------------------------------------

_SPOTIFY_URI = "spotify:track:6IiSsjuKiOIbOCSv10SqPn"
_AIRPLAY_TAGS = {
    "title": "Bohemian Rhapsody", "album": "A Night at the Opera", "artist": "Queen",
}


async def _shairport_playing(*args, **kwargs):
    """busctl against shairport-sync: Playing, with the tags above."""
    if "PlaybackStatus" in args:
        out = b'v s "Playing"\n'
    elif "Metadata" in args:
        out = (
            b'v a{sv} 4 "mpris:trackid" o "/foo" '
            b'"xesam:title" s "Bohemian Rhapsody" '
            b'"xesam:album" s "A Night at the Opera" '
            b'"xesam:artist" as 1 "Queen"'
        )
    else:
        out = b""
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(out, b""))
    proc.wait = AsyncMock(return_value=0)
    return proc


@pytest.mark.parametrize(
    ("mux_answer", "expected"),
    [
        # Spotify and AirPlay both report playing; mux's answer decides
        # (ADR-0150), even when it names neither.
        ("airplay", _AIRPLAY_TAGS),
        # librespot's state file carries only the URI; tags need the Web API.
        ("spotify", {"title": "", "album": "", "artist": "", "uri": _SPOTIFY_URI}),
        ("bluetooth", {}),
        ("idle", {}),
        # Mux unreachable: the probes' order puts AirPlay first.
        (OSError("no mux socket"), _AIRPLAY_TAGS),
    ],
    ids=["airplay", "spotify", "bluetooth", "idle", "mux_unreachable"],
)
async def test_currentsong_follows_the_audible_source(
    renderer, mux_answer, expected,
):
    write_librespot_state(
        renderer._librespot_state_path,
        playing=True, paused=False, stopped=False, uri=_SPOTIFY_URI,
    )
    mux = AsyncMock(
        return_value={"active_source": mux_answer},
        side_effect=mux_answer if isinstance(mux_answer, Exception) else None,
    )
    with (
        patch("jasper.audio_control.renderer.mux_socket_command", new=mux),
        patch("asyncio.create_subprocess_exec", side_effect=_shairport_playing),
    ):
        assert await renderer.get_currentsong() == expected


# ----------------------------------------------------------------------
# MPRIS metadata parser — tested with the actual busctl output we
# captured from shairport-sync during the migration.
# ----------------------------------------------------------------------

def test_parse_mpris_metadata_real_shairport_output():
    sample = (
        'v a{sv} 5 "mpris:trackid" o "/org/gnome/ShairportSync/2BDA81CACBA82DDD" '
        '"xesam:title" s "PROSTITUTE" '
        '"xesam:album" s "PROSTITUTE" '
        '"xesam:artist" as 1 "Labrinth" '
        '"mpris:length" x 164610000'
    )
    parsed = _parse_mpris_metadata(sample)
    assert parsed["xesam:title"] == "PROSTITUTE"
    assert parsed["xesam:album"] == "PROSTITUTE"
    assert parsed["xesam:artist"] == ["Labrinth"]


def test_parse_mpris_metadata_multiple_artists():
    sample = '"xesam:artist" as 2 "Daft Punk" "Pharrell Williams"'
    parsed = _parse_mpris_metadata(sample)
    assert parsed["xesam:artist"] == ["Daft Punk", "Pharrell Williams"]


def test_parse_mpris_metadata_empty_input():
    assert _parse_mpris_metadata("") == {}
    assert _parse_mpris_metadata("v s \"random\"") == {}


# ----------------------------------------------------------------------
# Edge cases — make sure failure modes don't crash the cascade
# ----------------------------------------------------------------------

async def test_active_renderers_when_busctl_missing(renderer):
    """If busctl can't be found (FileNotFoundError), the airplay probe
    must return False rather than propagating — same fail-soft contract
    as the other probes. Probe-level coverage in test_source_state.py;
    here we verify active_renderers stays consistent end-to-end."""
    # No librespot state file → spotify inactive
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("busctl not found"),
    ):
        result = await renderer.active_renderers()
    # All probes return False on FileNotFoundError; nothing crashes.
    assert result["aplactive"] is False
    assert result["btactive"] is False
