"""Tests for jasper.mux — the renderer source-arbiter.

Tests focus on the transition-detection state machine, which is the
hard logic. The probe-implementation tests live in test_source_state.py
since the probes were factored out into jasper.source_state; here we
just patch their bound names in jasper.mux's namespace and mutate the
return values per tick.
"""
from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jasper.mux import Mux, Source


@pytest.fixture
def mux(tmp_path):
    # State file path is per-test (tmp_path) so we don't accidentally
    # touch /run/librespot if a test forgets to stub the probes.
    m = Mux(librespot_state_path=str(tmp_path / "librespot.state.json"))
    return m


@pytest.fixture
def patched_probes(monkeypatch):
    """Replaces the source_state probe references in jasper.mux's
    namespace with AsyncMocks. Tests mutate return_value via
    `_stub_probes` to control what the next tick sees."""
    spotify = AsyncMock(return_value=False)
    airplay = AsyncMock(return_value=False)
    bluetooth = AsyncMock(return_value=False)
    monkeypatch.setattr("jasper.mux.spotify_playing", spotify)
    monkeypatch.setattr("jasper.mux.airplay_playing", airplay)
    monkeypatch.setattr("jasper.mux.bluetooth_playing", bluetooth)
    return SimpleNamespace(
        spotify=spotify, airplay=airplay, bluetooth=bluetooth,
    )


def _stub_probes(
    probes, *, spotify: bool, airplay: bool, bluetooth: bool,
):
    """Set the next return_value for each patched probe."""
    probes.spotify.return_value = spotify
    probes.airplay.return_value = airplay
    probes.bluetooth.return_value = bluetooth


def _stub_pauses(mux: Mux):
    """Replace the pause action with a capturing AsyncMock."""
    mux._pause = AsyncMock()


@pytest.mark.asyncio
async def test_no_transitions_no_pause_calls(mux, patched_probes):
    _stub_probes(patched_probes, spotify=False, airplay=False, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()
    mux._pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_source_starting_has_nothing_to_preempt(mux, patched_probes):
    _stub_probes(patched_probes, spotify=True, airplay=False, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()
    # Spotify just started, no other source was playing → no pauses.
    mux._pause.assert_not_awaited()
    assert mux._winner is Source.SPOTIFY


@pytest.mark.asyncio
async def test_new_source_preempts_current(mux, patched_probes):
    # First tick: Spotify is playing
    _stub_probes(patched_probes, spotify=True, airplay=False, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()
    mux._pause.assert_not_awaited()

    # Second tick: AirPlay starts, Spotify still playing — AirPlay wins
    _stub_probes(patched_probes, spotify=True, airplay=True, bluetooth=False)
    await mux._tick()
    mux._pause.assert_awaited_once_with(Source.SPOTIFY)
    assert mux._winner is Source.AIRPLAY


@pytest.mark.asyncio
async def test_continued_play_does_not_re_pause(mux, patched_probes):
    """Once preempted, the older source going back to not-playing
    should not trigger any further action."""
    _stub_probes(patched_probes, spotify=True, airplay=False, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()  # Spotify wins

    _stub_probes(patched_probes, spotify=True, airplay=True, bluetooth=False)
    await mux._tick()  # AirPlay starts, Spotify gets paused
    assert mux._pause.await_count == 1

    # AirPlay still playing, Spotify now stopped (it got paused)
    _stub_probes(patched_probes, spotify=False, airplay=True, bluetooth=False)
    await mux._tick()
    # Should be no new pause calls — Spotify is already not playing.
    assert mux._pause.await_count == 1


@pytest.mark.asyncio
async def test_three_way_preemption(mux, patched_probes):
    """BT playing → Spotify starts → AirPlay starts. Final winner
    is AirPlay, with Spotify getting preempted along the way and
    BT preempted by both."""
    _stub_pauses(mux)

    _stub_probes(patched_probes, spotify=False, airplay=False, bluetooth=True)
    await mux._tick()  # BT first; nothing to pause
    mux._pause.assert_not_awaited()

    _stub_probes(patched_probes, spotify=True, airplay=False, bluetooth=True)
    await mux._tick()  # Spotify starts; should preempt BT
    mux._pause.assert_awaited_with(Source.BLUETOOTH)
    assert mux._winner is Source.SPOTIFY

    # In reality BT preempt is a no-op so BT keeps showing playing.
    # We model that by leaving bluetooth=True in the next tick.
    _stub_probes(patched_probes, spotify=True, airplay=True, bluetooth=True)
    await mux._tick()  # AirPlay starts; should preempt BOTH others
    # Two new pause calls — Spotify and BT.
    pause_targets = [c.args[0] for c in mux._pause.await_args_list]
    assert Source.SPOTIFY in pause_targets
    assert Source.BLUETOOTH in pause_targets
    assert mux._winner is Source.AIRPLAY


@pytest.mark.asyncio
async def test_simultaneous_start_picks_one_deterministically(mux, patched_probes):
    """If multiple sources transition in the same tick (e.g. user
    starts Spotify and AirPlay within 1s), the mux picks one as
    the winner and pauses the other. Determinism comes from Source
    enum order — we just verify there's no crash + a winner is set."""
    _stub_probes(patched_probes, spotify=True, airplay=True, bluetooth=False)
    _stub_pauses(mux)
    await mux._tick()
    # One pause call (winner has none); the loser pauses.
    assert mux._pause.await_count == 1
    assert mux._winner is not None


@pytest.mark.asyncio
async def test_pause_is_resilient_to_action_failures(mux, patched_probes):
    """If _pause throws, _tick should not crash — the polling loop's
    try/except catches any exception. We test that here at the _pause
    boundary specifically: a failing pause shouldn't hang the
    mux's state-update logic."""
    _stub_probes(patched_probes, spotify=True, airplay=False, bluetooth=False)
    await mux._tick()
    _stub_probes(patched_probes, spotify=True, airplay=True, bluetooth=False)
    mux._pause = AsyncMock(side_effect=RuntimeError("pause API down"))
    # Tick should propagate the exception (run() handles it at the
    # outer level, so per-tick can be fragile).
    with pytest.raises(RuntimeError):
        await mux._tick()
    # State still updated despite the pause failure.
    assert mux._state.playing[Source.SPOTIFY] is True
    assert mux._state.playing[Source.AIRPLAY] is True


# --- Regression test for the BuildResult return-shape change ---


def test_ensure_spotify_router_consumes_build_result_correctly(tmp_path, monkeypatch):
    """build_clients used to return a bare `dict[str, AccountClient]`.
    PR #162 changed it to `BuildResult(clients=..., statuses=...,
    default_name=...)`. Three callers (mux.py + control/server.py x2)
    silently broke because they did `clients = build_clients(...)`
    then `Router(clients=clients, ...)` — passing a dataclass where a
    dict was expected.

    This test pins the correct shape consumption for mux.py: given a
    fake build_clients that returns a BuildResult, _ensure_spotify_router
    must produce a Router whose `clients` field is the dict, not the
    BuildResult itself."""
    from unittest.mock import patch, MagicMock
    from jasper.mux import Mux
    from jasper.spotify_router import (
        ACCOUNT_OK, AccountClient, AccountStatus, BuildResult, Router,
    )
    from jasper.accounts import Account

    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "a" * 32)
    monkeypatch.setenv(
        "JASPER_SPOTIFY_ACCOUNTS_PATH", str(tmp_path / "accounts.json"),
    )
    (tmp_path / "accounts.json").write_text(
        '{"accounts": [{"name": "jasper", "cache_path": "/nope"}], '
        '"default": "jasper"}'
    )

    fake_client = AccountClient(
        account=Account(name="jasper", cache_path="/nope"),
        sp=MagicMock(),
    )

    def fake_build_clients(_registry, *, client_id, redirect_uri):
        return BuildResult(
            clients={"jasper": fake_client},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
            default_name="jasper",
        )

    mux = Mux.__new__(Mux)  # bypass full __init__
    mux._spotify_router = None
    mux._spotify_router_built = False

    with patch("jasper.spotify_router.build_clients", side_effect=fake_build_clients):
        router = mux._ensure_spotify_router()

    assert isinstance(router, Router)
    assert router.clients == {"jasper": fake_client}, (
        "router.clients must be the dict from BuildResult.clients, "
        "not the BuildResult itself"
    )
    assert isinstance(router.clients, dict)
    assert router.statuses[0].state == ACCOUNT_OK
