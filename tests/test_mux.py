"""Tests for jasper.mux — the renderer source-arbiter.

Tests focus on the transition-detection state machine, which is the
hard logic. The HTTP and busctl probes are mocked at their I/O
boundary the same way test_renderer.py mocks them.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.mux import Mux, Source


@pytest.fixture
def mux(tmp_path):
    # State file path is per-test (tmp_path) so individual probe tests
    # can write fixtures into it without colliding with /run/librespot.
    m = Mux(librespot_state_path=str(tmp_path / "librespot.state.json"))
    return m


def _stub_probes(mux: Mux, *, spotify: bool, airplay: bool, bluetooth: bool):
    """Replace the three async probe methods with fixed-value stubs."""
    mux._spotify_playing = AsyncMock(return_value=spotify)
    mux._airplay_playing = AsyncMock(return_value=airplay)
    mux._bluetooth_playing = AsyncMock(return_value=bluetooth)


def _stub_pauses(mux: Mux):
    """Replace the pause action with a capturing AsyncMock."""
    mux._pause = AsyncMock()


@pytest.mark.asyncio
async def test_no_transitions_no_pause_calls(mux):
    _stub_probes(mux, spotify=False, airplay=False, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()
    mux._pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_source_starting_has_nothing_to_preempt(mux):
    _stub_probes(mux, spotify=True, airplay=False, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()
    # Spotify just started, no other source was playing → no pauses.
    mux._pause.assert_not_awaited()
    assert mux._winner is Source.SPOTIFY


@pytest.mark.asyncio
async def test_new_source_preempts_current(mux):
    # First tick: Spotify is playing
    _stub_probes(mux, spotify=True, airplay=False, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()
    mux._pause.assert_not_awaited()

    # Second tick: AirPlay starts, Spotify still playing — AirPlay wins
    _stub_probes(mux, spotify=True, airplay=True, bluetooth=False)
    await mux._tick()
    mux._pause.assert_awaited_once_with(Source.SPOTIFY)
    assert mux._winner is Source.AIRPLAY


@pytest.mark.asyncio
async def test_continued_play_does_not_re_pause(mux):
    """Once preempted, the older source going back to not-playing
    should not trigger any further action."""
    _stub_probes(mux, spotify=True, airplay=False, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()  # Spotify wins

    _stub_probes(mux, spotify=True, airplay=True, bluetooth=False)
    await mux._tick()  # AirPlay starts, Spotify gets paused
    assert mux._pause.await_count == 1

    # AirPlay still playing, Spotify now stopped (it got paused)
    _stub_probes(mux, spotify=False, airplay=True, bluetooth=False)
    await mux._tick()
    # Should be no new pause calls — Spotify is already not playing.
    assert mux._pause.await_count == 1


@pytest.mark.asyncio
async def test_three_way_preemption(mux):
    """BT playing → Spotify starts → AirPlay starts. Final winner
    is AirPlay, with Spotify getting preempted along the way and
    BT preempted by both."""
    _stub_pauses(mux)

    _stub_probes(mux, spotify=False, airplay=False, bluetooth=True)
    await mux._tick()  # BT first; nothing to pause
    mux._pause.assert_not_awaited()

    _stub_probes(mux, spotify=True, airplay=False, bluetooth=True)
    await mux._tick()  # Spotify starts; should preempt BT
    mux._pause.assert_awaited_with(Source.BLUETOOTH)
    assert mux._winner is Source.SPOTIFY

    # In reality BT preempt is a no-op so BT keeps showing playing.
    # We model that by leaving bluetooth=True in the next tick.
    _stub_probes(mux, spotify=True, airplay=True, bluetooth=True)
    await mux._tick()  # AirPlay starts; should preempt BOTH others
    # Two new pause calls — Spotify and BT.
    pause_targets = [c.args[0] for c in mux._pause.await_args_list]
    assert Source.SPOTIFY in pause_targets
    assert Source.BLUETOOTH in pause_targets
    assert mux._winner is Source.AIRPLAY


@pytest.mark.asyncio
async def test_simultaneous_start_picks_one_deterministically(mux):
    """If multiple sources transition in the same tick (e.g. user
    starts Spotify and AirPlay within 1s), the mux picks one as
    the winner and pauses the other. Determinism comes from Source
    enum order — we just verify there's no crash + a winner is set."""
    _stub_probes(mux, spotify=True, airplay=True, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()
    # One pause call (winner has none); the loser pauses.
    assert mux._pause.await_count == 1
    assert mux._winner is not None


@pytest.mark.asyncio
async def test_spotify_probe_handles_missing_state_file(mux):
    """librespot state file isn't written until the first --onevent
    fires. Before that, _spotify_playing must return False rather
    than raising."""
    # mux fixture's path doesn't exist yet → returns False
    assert await mux._spotify_playing() is False


@pytest.mark.asyncio
async def test_spotify_probe_reads_state_file_playing(mux, tmp_path):
    import json
    path = tmp_path / "librespot.state.json"
    path.write_text(json.dumps({"playing": True, "paused": False, "stopped": False}))
    mux._librespot_state_path = str(path)
    assert await mux._spotify_playing() is True


@pytest.mark.asyncio
async def test_spotify_probe_state_file_paused(mux, tmp_path):
    """Paused/stopped sessions should report not-playing."""
    import json
    path = tmp_path / "librespot.state.json"
    path.write_text(json.dumps({"playing": False, "paused": True, "stopped": False}))
    mux._librespot_state_path = str(path)
    assert await mux._spotify_playing() is False


@pytest.mark.asyncio
async def test_airplay_probe_handles_busctl_missing(mux):
    """If busctl isn't on PATH (unlikely on Trixie but possible on
    a stripped image), _airplay_playing returns False without
    raising."""
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("busctl"),
    ):
        assert await mux._airplay_playing() is False


@pytest.mark.asyncio
async def test_bluetooth_probe_handles_timeout(mux):
    """bluealsa-cli list-pcms hanging (DBus daemon stuck) should
    timeout cleanly and return False."""
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=asyncio.TimeoutError(),
    ):
        assert await mux._bluetooth_playing() is False


@pytest.mark.asyncio
async def test_pause_is_resilient_to_action_failures(mux):
    """If _pause throws, _tick should not crash — the polling loop's
    try/except catches any exception. We test that here at the _pause
    boundary specifically: a failing pause shouldn't hang the
    mux's state-update logic."""
    _stub_probes(mux, spotify=True, airplay=False, bluetooth=False)
    await mux._tick()
    _stub_probes(mux, spotify=True, airplay=True, bluetooth=False)
    mux._pause = AsyncMock(side_effect=RuntimeError("pause API down"))
    # Tick should propagate the exception (run() handles it at the
    # outer level, so per-tick can be fragile).
    with pytest.raises(RuntimeError):
        await mux._tick()
    # State still updated despite the pause failure.
    assert mux._state.playing[Source.SPOTIFY] is True
    assert mux._state.playing[Source.AIRPLAY] is True
