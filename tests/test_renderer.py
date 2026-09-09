# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.renderer.RendererClient.

Mocks at the I/O boundary: tmp_path-backed librespot state file
(which the --onevent hook would write), asyncio.create_subprocess_exec for
busctl, and the BlueZ A2DP probe.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.renderer import (
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
        "jasper.renderer.usbsink_streaming",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "jasper.source_state.a2dp_sink_playing",
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
        patch("jasper.renderer.usbsink_streaming", new=AsyncMock(return_value=True)),
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
    cancellation-only chain (_tick -> _active_source -> here); an
    unbounded connect would make that loop immortal (#2003)."""
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
        "jasper.source_state.a2dp_sink_playing",
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
# RendererClient.get_currentsong — cascade by active source
# ----------------------------------------------------------------------

async def test_currentsong_spotify_returns_uri(renderer):
    """librespot's --onevent only gives us URI/track_id in the state
    file — title/artist resolution requires a Spotify Web API call,
    which voice tools handle via spotify_router. The renderer just
    surfaces the URI so transport routing knows the source identity."""
    write_librespot_state(
        renderer._librespot_state_path,
        playing=True, paused=False, stopped=False,
        uri="spotify:track:6IiSsjuKiOIbOCSv10SqPn",
    )
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        song = await renderer.get_currentsong()
    assert song["uri"] == "spotify:track:6IiSsjuKiOIbOCSv10SqPn"


async def test_currentsong_returns_empty_when_no_source(renderer):
    """When no Spotify, AirPlay, or BT is active, currentsong returns
    {} — the three real renderers are the only sources we introspect."""
    # No librespot state file → no spotify; subprocess mock → no AirPlay
    # PlaybackStatus; no BlueZ bus → no BT.
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        song = await renderer.get_currentsong()
    assert song == {}


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


async def test_currentsong_airplay_returns_metadata(renderer):
    """When AirPlay is the active source and shairport-sync's MPRIS
    has metadata, currentsong should populate title/album/artist
    from the parsed busctl output."""
    # No librespot state file → spotactive False; aplactive=True via MPRIS

    sample_mpris = (
        'v a{sv} 4 "mpris:trackid" o "/foo" '
        '"xesam:title" s "Bohemian Rhapsody" '
        '"xesam:album" s "A Night at the Opera" '
        '"xesam:artist" as 1 "Queen"'
    )

    async def fake_subproc(*args, **kwargs):
        # First call: busctl Get PlaybackStatus (returns "Playing")
        # Second call: busctl Get Metadata (returns the sample)
        proc = MagicMock()
        proc.returncode = 0
        if "PlaybackStatus" in args:
            proc.communicate = AsyncMock(return_value=(b'v s "Playing"\n', b""))
        elif "Metadata" in args:
            proc.communicate = AsyncMock(
                return_value=(sample_mpris.encode(), b""),
            )
        else:
            proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subproc):
        song = await renderer.get_currentsong()

    assert song["title"] == "Bohemian Rhapsody"
    assert song["album"] == "A Night at the Opera"
    assert song["artist"] == "Queen"
