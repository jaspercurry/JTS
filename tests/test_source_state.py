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


def _mock_busctl_router(responses: dict[bytes, tuple[bytes, int]]):
    """Like _mock_subprocess but routes by which busctl property is
    requested. Lets a single test cover both PlaybackStatus and
    Metadata calls with different responses each.

    `responses` maps a marker (e.g. b"PlaybackStatus" or b"Metadata")
    that appears in the busctl args to (stdout, returncode). If no
    marker matches, the mock returns empty stdout / returncode 0.
    """
    async def fake(*args, **kwargs):
        args_blob = b" ".join(
            a.encode() if isinstance(a, str) else a for a in args
        )
        for marker, (out, rc) in responses.items():
            if marker in args_blob:
                proc = MagicMock()
                proc.communicate = AsyncMock(return_value=(out, b""))
                proc.returncode = rc
                return proc
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
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
# airplay_playing — busctl Get PlaybackStatus AND Metadata on shairport
#
# Contract since 2026-05-22: BOTH PlaybackStatus=="Playing" AND a non-
# empty xesam:title in Metadata are required. Phantom AirPlay SETUPs
# from idle Apple devices set PlaybackStatus=Playing but carry empty
# Metadata; genuine sessions populate xesam:title with the sender's
# track title. Off-switch: JASPER_AIRPLAY_METADATA_GATE=disabled.
# ----------------------------------------------------------------------

# Sample busctl payloads. The Metadata one is a real shape captured
# from a populated session — the regex in source_state.py matches the
# `"xesam:title" s "..."` substring.
_METADATA_GENUINE = (
    b'v a{sv} 5 "mpris:trackid" o "/org/.../A" '
    b'"xesam:title" s "Some Track" '
    b'"xesam:album" s "Some Album" '
    b'"xesam:artist" as 1 "Some Artist" '
    b'"mpris:length" x 164610000\n'
)
_METADATA_PHANTOM = b"v a{sv} 0\n"


@pytest.mark.asyncio
async def test_airplay_playing_genuine_session_passes_both_checks():
    """Genuine AirPlay: PlaybackStatus=Playing AND metadata has title."""
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_busctl_router({
            b"PlaybackStatus": (b'v s "Playing"\n', 0),
            b"Metadata":       (_METADATA_GENUINE, 0),
        }),
    ):
        assert await source_state.airplay_playing() is True


@pytest.mark.asyncio
async def test_airplay_playing_phantom_session_returns_false():
    """Phantom SETUP from macOS: Playing reported but Metadata empty.
    Without the metadata gate, this case caused volume-routing flap
    every ~30 s during Spotify Connect playback."""
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_busctl_router({
            b"PlaybackStatus": (b'v s "Playing"\n', 0),
            b"Metadata":       (_METADATA_PHANTOM, 0),
        }),
    ):
        assert await source_state.airplay_playing() is False


@pytest.mark.asyncio
async def test_airplay_playing_returns_false_when_paused():
    """Paused short-circuits before the metadata probe — no point
    checking the title when PlaybackStatus alone disqualifies the
    session. (Also a perf nicety: one busctl call instead of two.)"""
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_busctl_router({
            b"PlaybackStatus": (b'v s "Paused"\n', 0),
        }),
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


@pytest.mark.asyncio
async def test_airplay_playing_off_switch_reverts_to_playbackstatus_only(
    monkeypatch,
):
    """JASPER_AIRPLAY_METADATA_GATE=disabled is the escape hatch for
    field conditions where xesam:title genuinely empties during real
    audio. With it set, behaviour matches the pre-2026-05-22 contract:
    PlaybackStatus alone determines the answer."""
    monkeypatch.setenv("JASPER_AIRPLAY_METADATA_GATE", "disabled")
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_busctl_router({
            b"PlaybackStatus": (b'v s "Playing"\n', 0),
            # Metadata would say phantom, but the gate is disabled so
            # we shouldn't even call it. Test passes either way since
            # we return early after seeing Playing.
            b"Metadata":       (_METADATA_PHANTOM, 0),
        }),
    ):
        assert await source_state.airplay_playing() is True


@pytest.mark.asyncio
async def test_airplay_playing_metadata_call_failure_treated_as_phantom():
    """If the Metadata busctl call fails (DBus glitch, timeout) while
    PlaybackStatus is Playing, we fail closed — treat as phantom rather
    than risk a 30 s -25 dB duck. Off-switch is the escape if this is
    too aggressive in practice."""
    async def router(*args, **kwargs):
        args_blob = b" ".join(
            a.encode() if isinstance(a, str) else a for a in args
        )
        if b"PlaybackStatus" in args_blob:
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b'v s "Playing"\n', b""))
            proc.returncode = 0
            return proc
        # Metadata call fails
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", side_effect=router):
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
