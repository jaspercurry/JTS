"""Tests for jasper.renderer.RendererClient.

Mocks at the I/O boundary: tmp_path-backed librespot state file
(which the --onevent hook would write), and asyncio.create_subprocess_exec
for busctl / bluealsa-cli.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.renderer import (
    RendererClient,
    _parse_mpris_metadata,
)


# ----------------------------------------------------------------------
# RendererClient.active_renderers — mocks each underlying source
# ----------------------------------------------------------------------

@pytest.fixture
def renderer(tmp_path):
    # Per-test state file path. Tests write fixture JSON into it
    # (or leave it absent) to control what source_state.spotify_playing
    # observes via active_renderers.
    return RendererClient(
        librespot_state_path=str(tmp_path / "librespot.state.json"),
    )


def _write_librespot_state(renderer, payload):
    """Helper to write a librespot state file the renderer will read."""
    from pathlib import Path
    Path(renderer._librespot_state_path).write_text(json.dumps(payload))


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


@pytest.mark.asyncio
async def test_active_renderers_all_inactive(renderer):
    # No librespot state file present, busctl empty for AirPlay,
    # bluealsa-cli has no PCM.
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await renderer.active_renderers()
    assert result == {
        "aplactive": False,
        "btactive": False,
        "spotactive": False,
    }


@pytest.mark.asyncio
async def test_active_renderers_spotify_playing(renderer):
    _write_librespot_state(renderer, {
        "playing": True, "paused": False, "stopped": False,
        "uri": "spotify:track:X",
    })
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await renderer.active_renderers()
    assert result["spotactive"] is True
    assert result["aplactive"] is False
    assert result["btactive"] is False


@pytest.mark.asyncio
async def test_active_renderers_bluetooth_playing(renderer):
    fake_pcm = b"/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source\n"
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=fake_pcm),
    ):
        result = await renderer.active_renderers()
    assert result["btactive"] is True
    assert result["spotactive"] is False


@pytest.mark.asyncio
async def test_active_renderers_resilient_to_missing_state_file(renderer):
    """If librespot state file is absent (daemon not started yet, or
    session never connected), the spotify probe returns False rather
    than raising — same fail-soft contract as the busctl/bluealsa
    probes. (Direct probe-level coverage lives in test_source_state.py;
    here we just pin the integration behaviour through active_renderers.)"""
    # No state file written → librespot_state.is_playing returns False
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await renderer.active_renderers()
    assert result["spotactive"] is False


# ----------------------------------------------------------------------
# RendererClient.get_currentsong — cascade by active source
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_currentsong_spotify_returns_uri(renderer):
    """librespot's --onevent only gives us URI/track_id in the state
    file — title/artist resolution requires a Spotify Web API call,
    which voice tools handle via spotify_router. The renderer just
    surfaces the URI so transport routing knows the source identity."""
    _write_librespot_state(renderer, {
        "playing": True, "paused": False, "stopped": False,
        "uri": "spotify:track:6IiSsjuKiOIbOCSv10SqPn",
    })
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        song = await renderer.get_currentsong()
    assert song["uri"] == "spotify:track:6IiSsjuKiOIbOCSv10SqPn"


@pytest.mark.asyncio
async def test_currentsong_returns_empty_when_no_source(renderer):
    """When no Spotify, AirPlay, or BT is active, currentsong returns
    {} — the three real renderers are the only sources we introspect."""
    # No librespot state file → no spotify; subprocess mock → no AirPlay
    # PlaybackStatus, no BT a2dpsnk.
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        song = await renderer.get_currentsong()
    assert song == {}


# ----------------------------------------------------------------------
# pause_airplay — MPRIS Pause on shairport-sync
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_airplay_calls_mpris_pause(renderer):
    """Verify pause_airplay() invokes busctl with the Pause method on
    shairport-sync's MPRIS interface. We capture args by wrapping
    create_subprocess_exec rather than replacing it with `new=`."""
    captured_args: list[tuple] = []
    fake = _mock_subprocess(returncode=0)

    async def capturing(*args, **kwargs):
        captured_args.append(args)
        return await fake(*args, **kwargs)

    with patch("asyncio.create_subprocess_exec", side_effect=capturing):
        await renderer.pause_airplay()

    assert captured_args, "create_subprocess_exec was not called"
    args = captured_args[0]
    assert "busctl" in args[0]
    assert "Pause" in args
    assert "org.mpris.MediaPlayer2.ShairportSync" in args


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

@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
        # First call: bluealsa-cli list-pcms (BT not active)
        # Second call: busctl Get PlaybackStatus (returns "Playing")
        # Third call: busctl Get Metadata (returns the sample)
        proc = MagicMock()
        proc.returncode = 0
        if "bluealsa-cli" in args:
            proc.communicate = AsyncMock(return_value=(b"", b""))
        elif "PlaybackStatus" in args:
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
