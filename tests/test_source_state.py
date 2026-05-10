"""Tests for jasper.source_state — the three async probes that report
which renderer is currently producing audio.

The probes wrap I/O (a librespot state file, busctl, bluealsa-cli);
mock at that boundary. Both jasper.renderer.RendererClient.active_renderers
and jasper.mux's tick loop depend on these returning False on transport
error rather than raising — every test here exercises that contract too.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper import source_state


def _mock_subprocess(stdout: bytes = b"", returncode: int = 0):
    """Build an asyncio.create_subprocess_exec replacement that returns
    a mock proc with .communicate() pre-canned."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode

    async def fake(*args, **kwargs):
        return proc

    return fake


# ----------------------------------------------------------------------
# spotify_playing — reads the librespot --onevent state file
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spotify_playing_missing_state_file(tmp_path):
    """librespot writes the state file lazily (first --onevent fires
    after the first session). Before that, the probe must report
    not-playing rather than raising."""
    path = tmp_path / "librespot.state.json"
    assert await source_state.spotify_playing(str(path)) is False


@pytest.mark.asyncio
async def test_spotify_playing_state_file_says_playing(tmp_path):
    path = tmp_path / "librespot.state.json"
    path.write_text(json.dumps(
        {"playing": True, "paused": False, "stopped": False},
    ))
    assert await source_state.spotify_playing(str(path)) is True


@pytest.mark.asyncio
async def test_spotify_playing_state_file_says_paused(tmp_path):
    """A paused session is not active. is_playing's contract is "playing,
    not paused, not stopped" — make sure paused state surfaces correctly."""
    path = tmp_path / "librespot.state.json"
    path.write_text(json.dumps(
        {"playing": False, "paused": True, "stopped": False},
    ))
    assert await source_state.spotify_playing(str(path)) is False


# ----------------------------------------------------------------------
# airplay_playing — busctl Get PlaybackStatus on shairport-sync MPRIS
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_airplay_playing_returns_true_when_busctl_says_playing():
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b'v s "Playing"\n'),
    ):
        assert await source_state.airplay_playing() is True


@pytest.mark.asyncio
async def test_airplay_playing_returns_false_when_paused():
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b'v s "Paused"\n'),
    ):
        assert await source_state.airplay_playing() is False


@pytest.mark.asyncio
async def test_airplay_playing_handles_busctl_missing():
    """If busctl isn't on PATH (unlikely on Trixie but possible on a
    stripped image), the probe must return False rather than raise."""
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("busctl"),
    ):
        assert await source_state.airplay_playing() is False


@pytest.mark.asyncio
async def test_airplay_playing_handles_busctl_nonzero_returncode():
    """busctl returns non-zero when the bus name doesn't exist
    (e.g. shairport-sync isn't running). Treat as not-playing."""
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b"", returncode=1),
    ):
        assert await source_state.airplay_playing() is False


# ----------------------------------------------------------------------
# bluetooth_playing — bluealsa-cli list-pcms parsing
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bluetooth_playing_detects_a2dpsnk_source():
    fake_pcm = b"/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source\n"
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=fake_pcm),
    ):
        assert await source_state.bluetooth_playing() is True


@pytest.mark.asyncio
async def test_bluetooth_playing_returns_false_on_empty_output():
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        assert await source_state.bluetooth_playing() is False


@pytest.mark.asyncio
async def test_bluetooth_playing_handles_timeout():
    """bluealsa-cli hanging (DBus daemon stuck) should time out cleanly
    and return False — the mux's 1 Hz tick can't tolerate a probe that
    blocks longer than 2 s."""
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=asyncio.TimeoutError(),
    ):
        assert await source_state.bluetooth_playing() is False


@pytest.mark.asyncio
async def test_bluetooth_playing_handles_bluealsa_cli_missing():
    """bluealsa-cli isn't part of base Trixie; if the user ran the
    install script with no Bluetooth path, it may genuinely be missing."""
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("bluealsa-cli"),
    ):
        assert await source_state.bluetooth_playing() is False
