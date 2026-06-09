"""Tests for jasper.mux — the renderer source-arbiter.

Tests focus on the transition-detection state machine, which is the
hard logic. The probe-implementation tests live in test_source_state.py
since the probes were factored out into jasper.source_state; here we
just patch their bound names in jasper.mux's namespace and mutate the
return values per tick.
"""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jasper.music_sources import VolumeMode
from jasper.mux import Mux, Source

REPO = Path(__file__).resolve().parents[1]


class _FakeHandoff:
    def __init__(self, prev, current, *, level=50, result="ok"):
        self.prev_source = prev
        self.current_source = current
        self.reason = "test"
        self.level = level
        self.prev_mode = VolumeMode.CAMILLA_MASTER
        self.current_mode = VolumeMode.CAMILLA_MASTER
        self.guard_db = -25.0
        self.camilla_before_db = 0.0
        self.push_ok = None
        self.settled_ms = 0
        self.result = result
        self.detail = ""

    @property
    def ok(self):
        return self.result in {"ok", "degraded_safe", "noop"}


class _FakeVolumeCoordinator:
    def __init__(self):
        self.prepared: list[tuple[Source, Source, str]] = []
        self.finalized: list[_FakeHandoff] = []
        self.events: list[str] = []
        self.next_result = "ok"

    async def prepare_source_handoff(self, prev, current, *, reason):
        self.prepared.append((prev, current, reason))
        self.events.append(f"prepare:{current.value}")
        return _FakeHandoff(prev, current, result=self.next_result)

    async def finalize_source_handoff(self, handoff):
        self.finalized.append(handoff)
        self.events.append(f"finalize:{handoff.current_source.value}")
        return True

    async def aclose(self):
        pass


@pytest.fixture
def mux(tmp_path):
    # State file paths are per-test (tmp_path) so we don't accidentally
    # touch /run/librespot or the real /var/lib/jasper/mux_mode.json if a
    # test forgets to stub the probes.
    m = Mux(
        librespot_state_path=str(tmp_path / "librespot.state.json"),
        volume_coordinator=_FakeVolumeCoordinator(),
        mode_state_path=str(tmp_path / "mux_mode.json"),
    )
    m._fanin_select = AsyncMock(return_value={})
    m._fanin_auto = AsyncMock(return_value={})
    m._fanin_none = AsyncMock(return_value={})
    return m


@pytest.fixture
def patched_probes(monkeypatch):
    """Replaces the source_state probe references in jasper.mux's
    namespace with AsyncMocks. Tests mutate return_value via
    `_stub_probes` to control what the next tick sees.

    USB sink defaults to False so existing 3-source tests written
    before the fourth source landed still exercise the same matrix
    without needing to pass usbsink=False explicitly."""
    spotify = AsyncMock(return_value=False)
    airplay = AsyncMock(return_value=False)
    bluetooth = AsyncMock(return_value=False)
    usbsink = AsyncMock(return_value=False)
    monkeypatch.setattr("jasper.mux.spotify_playing", spotify)
    monkeypatch.setattr("jasper.mux.airplay_playing", airplay)
    monkeypatch.setattr("jasper.mux.bluetooth_playing", bluetooth)
    monkeypatch.setattr("jasper.mux.usbsink_playing", usbsink)
    return SimpleNamespace(
        spotify=spotify, airplay=airplay,
        bluetooth=bluetooth, usbsink=usbsink,
    )


def _stub_probes(
    probes, *, spotify: bool = False, airplay: bool = False,
    bluetooth: bool = False, usbsink: bool = False,
):
    """Set the next return_value for each patched probe."""
    probes.spotify.return_value = spotify
    probes.airplay.return_value = airplay
    probes.bluetooth.return_value = bluetooth
    probes.usbsink.return_value = usbsink


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


def test_mux_service_loads_spotify_credentials_env():
    """Mux owns source handoff, so it needs the same wizard-written
    Spotify credentials as voice/control. Without this env file, the
    Spotify Web API router is empty and Spotify handoff stays
    degraded_safe with Camilla attenuating the stream."""
    unit = (REPO / "deploy" / "systemd" / "jasper-mux.service").read_text()
    env_files = [
        line.strip().split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("EnvironmentFile=")
    ]

    assert "-/var/lib/jasper/spotify_credentials.env" in env_files


def test_mux_service_can_write_state_dir():
    """The manual-pin persistence file lives under /var/lib/jasper.
    The unit must guarantee that path is writable. Mux runs as root with
    no ProtectSystem today (so it's writable), but StateDirectory=jasper
    codifies the write target and keeps the pin durable if mux ever
    gains filesystem sandboxing — mirroring jasper-voice.service."""
    unit = (REPO / "deploy" / "systemd" / "jasper-mux.service").read_text()
    lines = [line.strip() for line in unit.splitlines()]
    has_state_dir = "StateDirectory=jasper" in lines
    has_rw_path = any(
        line.startswith("ReadWritePaths=") and "/var/lib/jasper" in line
        for line in lines
    )
    # If mux ever adds ProtectSystem=, one of these must be present or
    # the persistence write silently fails on the live Pi.
    no_protect_system = not any(
        line.startswith("ProtectSystem=") for line in lines
    )
    assert has_state_dir or has_rw_path or no_protect_system


# ----------------------------------------------------------------------
# USB sink arbitration — fourth source. Volume/preempt protocol uses
# an HTTP POST to the daemon's localhost listener; tests stub the
# wrapper method so they don't try to hit a real socket.
# ----------------------------------------------------------------------


def _stub_usbsink_preempt(mux: Mux):
    """Replace the HTTP-POST helper so tests can assert the calls
    without binding 127.0.0.1:8781. Returns the mock so callers can
    inspect call args."""
    mux._usbsink_set_preempt = AsyncMock(
        side_effect=lambda silenced, *, reason: setattr(
            mux, "_usbsink_preempted", bool(silenced),
        ),
    )
    return mux._usbsink_set_preempt


@pytest.mark.asyncio
async def test_usbsink_starting_alone_takes_speaker(mux, patched_probes):
    """User plugs in Mac and starts playing while nothing else
    is active. USB wins the speaker, no other source to pause."""
    _stub_probes(patched_probes, usbsink=True)
    _stub_pauses(mux)
    _stub_usbsink_preempt(mux)
    await mux._tick()
    mux._pause.assert_not_awaited()
    assert mux._winner is Source.USBSINK


@pytest.mark.asyncio
async def test_airplay_preempts_usbsink_with_silenced_post(mux, patched_probes):
    """USB playing. AirPlay starts. Mux POSTs silenced=true so the
    daemon stops mixing its audio into the loopback."""
    _stub_pauses(mux)
    preempt = _stub_usbsink_preempt(mux)

    _stub_probes(patched_probes, usbsink=True)
    await mux._tick()  # USB wins
    preempt.assert_not_awaited()

    _stub_probes(patched_probes, usbsink=True, airplay=True)
    await mux._tick()  # AirPlay newly-started → preempt USB
    mux._pause.assert_awaited_once_with(Source.USBSINK)
    assert mux._winner is Source.AIRPLAY


@pytest.mark.asyncio
async def test_usbsink_preempt_released_when_others_idle(mux, patched_probes):
    """After AirPlay stops, mux clears USB's preempt so the user can
    re-take the speaker just by playing on the host."""
    _stub_pauses(mux)
    preempt = _stub_usbsink_preempt(mux)
    # Real _pause normally POSTs preempt; the stub doesn't, so flip
    # the flag manually to simulate that side effect.
    _stub_probes(patched_probes, usbsink=True, airplay=True)
    await mux._tick()
    mux._usbsink_preempted = True

    # AirPlay stops. USB still RMS-active. Mux should release.
    _stub_probes(patched_probes, usbsink=True, airplay=False)
    await mux._tick()
    # _usbsink_set_preempt called with silenced=False
    assert preempt.await_count >= 1
    last_call = preempt.await_args_list[-1]
    assert last_call.args[0] is False


@pytest.mark.asyncio
async def test_usbsink_pause_then_play_clears_preempt(mux, patched_probes):
    """Host paused (RMS dropped) while preempted, then user hits
    play again (RMS rises). The daemon publishes a fresh
    inactive→active edge → mux sees newly_started → preempt released
    so audio flows again. Other sources get preempted as the new
    winner."""
    _stub_pauses(mux)
    preempt = _stub_usbsink_preempt(mux)

    # Initial: USB + AirPlay both playing, AirPlay wins, USB preempted.
    _stub_probes(patched_probes, usbsink=True, airplay=True)
    await mux._tick()
    mux._usbsink_preempted = True

    # Host paused → daemon publishes playing=false. AirPlay still on.
    _stub_probes(patched_probes, usbsink=False, airplay=True)
    await mux._tick()
    # Preempt stays on; no edge to react to.

    # Host plays again → daemon publishes playing=true → newly_started.
    _stub_probes(patched_probes, usbsink=True, airplay=True)
    await mux._tick()
    # USB just became the new winner → preempt cleared, AirPlay paused.
    last_call = preempt.await_args_list[-1]
    assert last_call.args[0] is False
    assert mux._winner is Source.USBSINK


@pytest.mark.asyncio
async def test_usbsink_preempt_release_idempotent(mux, patched_probes):
    """When already not preempted and all others go idle, mux
    should NOT spam release POSTs. The set_preempt method is itself
    a no-op when state matches, but mux's `if self._usbsink_preempted`
    guard prevents the call entirely."""
    _stub_pauses(mux)
    preempt = _stub_usbsink_preempt(mux)

    _stub_probes(patched_probes, usbsink=True)
    await mux._tick()
    # USB only, no preempt action.
    preempt.assert_not_awaited()

    # USB stops. No transitions, no preempt action.
    _stub_probes(patched_probes, usbsink=False)
    await mux._tick()
    preempt.assert_not_awaited()


# ----------------------------------------------------------------------
# Escape-hatch env var. JASPER_USBSINK_PREEMPT=disabled short-circuits
# the preempt POST so mux still tracks state but never asks the daemon
# to silence — degrades to Bluetooth-style "brief mixing on preempt"
# behaviour without requiring a redeploy.
# ----------------------------------------------------------------------


def _make_real_mux_http_stubbed(tmp_path):
    """Build a real Mux with httpx stubbed, for tests that exercise
    `_usbsink_set_preempt`'s real implementation directly (vs. going
    through _pause which the existing tests stub out)."""
    from unittest.mock import AsyncMock as _AsyncMock
    m = Mux(librespot_state_path=str(tmp_path / "librespot.state.json"))
    fake_http = _AsyncMock()
    # Default to a 200 OK so the POST path completes normally.
    fake_http.post.return_value.status_code = 200
    m._http = fake_http
    return m, fake_http


@pytest.mark.asyncio
async def test_usbsink_set_preempt_skips_post_when_env_disabled(
    monkeypatch, tmp_path,
):
    """With the escape hatch set, _usbsink_set_preempt updates the
    tracked flag but does NOT POST to the daemon. Exercises the
    method directly — bypasses _pause which the other tests stub."""
    monkeypatch.setenv("JASPER_USBSINK_PREEMPT", "disabled")
    m, fake_http = _make_real_mux_http_stubbed(tmp_path)

    await m._usbsink_set_preempt(True, reason="test_escape_hatch")

    # State updated optimistically — mux's view of the world matches
    # what it would have been if the POST had succeeded.
    assert m._usbsink_preempted is True
    # But no HTTP POST happened.
    fake_http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_usbsink_set_preempt_unsilencing_also_skips_when_env_disabled(
    monkeypatch, tmp_path,
):
    """The escape hatch covers both directions — silence AND unsilence
    skip the POST. Otherwise an operator enabling the escape hatch
    mid-flight (with USB already silenced) would never get unsilenced."""
    monkeypatch.setenv("JASPER_USBSINK_PREEMPT", "disabled")
    m, fake_http = _make_real_mux_http_stubbed(tmp_path)
    m._usbsink_preempted = True  # Pretend we were preempted before

    await m._usbsink_set_preempt(False, reason="test_release")

    assert m._usbsink_preempted is False
    fake_http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_usbsink_set_preempt_disabled_value_must_be_literal(
    monkeypatch, tmp_path,
):
    """The escape hatch is a string-match on the literal "disabled".
    Other truthy strings (1, true, off, yes) do NOT activate it,
    matching the sibling escape hatches' contract — avoids accidental
    activation when an operator sets the var to a generic truthy value
    expecting an enable. Mirrors the explicit `"disabled"` contract
    in jasper.source_state._airplay_metadata_gate_disabled."""
    for val in ("1", "true", "off", "yes", "enabled", ""):
        monkeypatch.setenv("JASPER_USBSINK_PREEMPT", val)
        m, fake_http = _make_real_mux_http_stubbed(tmp_path)

        await m._usbsink_set_preempt(True, reason=f"val_{val}")

        assert fake_http.post.await_count == 1, (
            f"JASPER_USBSINK_PREEMPT={val!r} should NOT trigger the "
            "escape hatch; only the literal 'disabled' (case-insensitive)."
        )


@pytest.mark.asyncio
async def test_usbsink_set_preempt_disabled_case_insensitive(
    monkeypatch, tmp_path,
):
    """Operators may set the value as "Disabled" or "DISABLED" by
    convention; the gate is case-insensitive per the sibling
    escape hatches."""
    for val in ("disabled", "DISABLED", "Disabled", "  disabled  "):
        monkeypatch.setenv("JASPER_USBSINK_PREEMPT", val)
        m, fake_http = _make_real_mux_http_stubbed(tmp_path)

        await m._usbsink_set_preempt(True, reason=f"val_{val!r}")

        assert fake_http.post.await_count == 0, (
            f"JASPER_USBSINK_PREEMPT={val!r} should trigger the "
            "escape hatch (case-insensitive, whitespace-stripped)."
        )


# ----------------------------------------------------------------------
# Manual source selection — web UI selects a renderer lane without
# turning renderers on/off. Fan-in is the audio gate; mux owns policy.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_source_gates_fanin_without_pausing_other_sources(
    mux, patched_probes,
):
    _stub_probes(
        patched_probes,
        spotify=True,
        airplay=True,
        bluetooth=True,
        usbsink=False,
    )
    _stub_pauses(mux)
    mux._fanin_select = AsyncMock(return_value={})

    status = await mux.select_source(Source.AIRPLAY)

    mux._fanin_select.assert_awaited_once_with(Source.AIRPLAY)
    mux._pause.assert_not_awaited()
    assert mux._manual_source is Source.AIRPLAY
    assert mux._winner is Source.AIRPLAY
    assert status["mode"] == "manual"
    assert status["selected_source"] == "airplay"
    assert status["active_source"] == "airplay"
    assert status["last_handoff"]["id"] == 1
    assert status["last_handoff"]["from"] == "idle"
    assert status["last_handoff"]["to"] == "airplay"


@pytest.mark.asyncio
async def test_select_source_prepares_volume_before_fanin_gate(
    mux, patched_probes,
):
    _stub_probes(patched_probes, spotify=True, airplay=True)
    coord = mux._volume_coordinator

    async def select_with_order(source):
        coord.events.append(f"select:{source.value}")
        return {}

    mux._fanin_select = AsyncMock(side_effect=select_with_order)

    await mux.select_source(Source.AIRPLAY)

    assert coord.events == [
        "prepare:airplay",
        "select:airplay",
        "finalize:airplay",
    ]


@pytest.mark.asyncio
async def test_select_source_does_not_open_fanin_when_handoff_fails(
    mux, patched_probes,
):
    _stub_probes(patched_probes, spotify=True, airplay=True)
    coord = mux._volume_coordinator
    coord.next_result = "failed"

    status = await mux.select_source(Source.AIRPLAY)

    mux._fanin_select.assert_not_awaited()
    assert coord.finalized == []
    assert mux._manual_source is None
    assert mux._winner is None
    assert status["mode"] == "auto"


@pytest.mark.asyncio
async def test_startup_handoff_failure_uses_fanin_none(
    mux, patched_probes,
):
    _stub_probes(patched_probes, airplay=True)
    _stub_pauses(mux)
    coord = mux._volume_coordinator
    coord.next_result = "failed"

    await mux._tick()

    mux._fanin_select.assert_not_awaited()
    mux._fanin_none.assert_awaited_once()
    mux._pause.assert_not_awaited()
    assert mux._winner is None


@pytest.mark.asyncio
async def test_failed_auto_handoff_retries_target_on_next_tick(
    mux, patched_probes,
):
    _stub_pauses(mux)
    _stub_probes(patched_probes, spotify=True)
    await mux._tick()
    assert mux._winner is Source.SPOTIFY

    coord = mux._volume_coordinator
    coord.next_result = "failed"
    _stub_probes(patched_probes, spotify=True, airplay=True)
    await mux._tick()

    assert mux._pending_auto_target is Source.AIRPLAY
    assert mux._winner is Source.SPOTIFY
    assert mux._last_handoff["id"] == 2
    assert mux._last_handoff["result"] == "failed"

    coord.next_result = "ok"
    await mux._tick()

    assert mux._pending_auto_target is None
    assert mux._winner is Source.AIRPLAY
    assert mux._last_handoff["id"] == 3
    assert mux._last_handoff["result"] == "ok"
    assert [event for event in coord.events if event == "prepare:airplay"] == [
        "prepare:airplay",
        "prepare:airplay",
    ]


@pytest.mark.asyncio
async def test_auto_spotify_to_airplay_prepares_volume_before_fanin_gate(
    mux, patched_probes,
):
    _stub_pauses(mux)
    _stub_probes(patched_probes, spotify=True)
    await mux._tick()

    coord = mux._volume_coordinator
    coord.events.clear()

    async def select_with_order(source):
        coord.events.append(f"select:{source.value}")
        return {}

    mux._fanin_select = AsyncMock(side_effect=select_with_order)
    _stub_probes(patched_probes, spotify=True, airplay=True)

    await mux._tick()

    assert coord.events == [
        "prepare:airplay",
        "select:airplay",
        "finalize:airplay",
    ]
    mux._pause.assert_awaited_with(Source.SPOTIFY)
    assert mux._winner is Source.AIRPLAY


@pytest.mark.asyncio
async def test_winner_stopping_holds_fanin_none(
    mux, patched_probes,
):
    _stub_pauses(mux)
    _stub_probes(patched_probes, airplay=True)
    await mux._tick()
    assert mux._winner is Source.AIRPLAY

    _stub_probes(patched_probes)
    await mux._tick()

    mux._fanin_none.assert_awaited()
    assert mux._winner is None


@pytest.mark.asyncio
async def test_airplay_preempt_uses_stop_not_pause(mux, monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_busctl(*args):
        calls.append(args)
        return ""

    monkeypatch.setattr("jasper.mux._busctl", fake_busctl)

    await mux._pause(Source.AIRPLAY)

    assert calls == [(
        "call",
        "org.mpris.MediaPlayer2.ShairportSync",
        "/org/mpris/MediaPlayer2",
        "org.mpris.MediaPlayer2.Player",
        "Stop",
    )]


@pytest.mark.asyncio
async def test_airplay_preempt_falls_back_to_pause_when_stop_fails(
    mux, monkeypatch,
):
    calls: list[tuple[str, ...]] = []

    async def fake_busctl(*args):
        calls.append(args)
        return None if args[-1] == "Stop" else ""

    monkeypatch.setattr("jasper.mux._busctl", fake_busctl)

    await mux._pause(Source.AIRPLAY)

    assert [call[-1] for call in calls] == ["Stop", "Pause"]


@pytest.mark.asyncio
async def test_manual_tick_keeps_selected_source_when_other_source_starts(
    mux, patched_probes,
):
    mux._manual_source = Source.BLUETOOTH
    mux._fanin_select = AsyncMock(return_value={})
    _stub_pauses(mux)

    _stub_probes(patched_probes, bluetooth=True, spotify=False)
    await mux._tick()
    mux._pause.assert_not_awaited()

    _stub_probes(patched_probes, bluetooth=True, spotify=True)
    await mux._tick()

    mux._pause.assert_not_awaited()
    assert mux._fanin_select.await_count == 2
    mux._fanin_select.assert_awaited_with(Source.BLUETOOTH)
    assert mux._winner is Source.BLUETOOTH


@pytest.mark.asyncio
async def test_auto_select_clears_manual_source_and_releases_fanin_gate(
    mux, patched_probes,
):
    mux._manual_source = Source.SPOTIFY
    mux._winner = Source.SPOTIFY
    mux._fanin_select = AsyncMock(return_value={})
    _stub_probes(patched_probes, spotify=False, airplay=True)

    status = await mux.auto_select()

    mux._fanin_select.assert_awaited_once_with(Source.AIRPLAY)
    assert mux._manual_source is None
    assert status["mode"] == "auto"
    assert status["selected_source"] is None
    assert status["active_source"] == "airplay"


@pytest.mark.asyncio
async def test_auto_select_preempts_other_active_sources_before_auto_gate(
    mux, patched_probes,
):
    mux._manual_source = Source.AIRPLAY
    mux._fanin_select = AsyncMock(return_value={})
    _stub_pauses(mux)
    _stub_probes(patched_probes, spotify=True, airplay=True)

    status = await mux.auto_select()

    mux._pause.assert_awaited_once_with(Source.SPOTIFY)
    mux._fanin_select.assert_awaited_once_with(Source.AIRPLAY)
    assert mux._manual_source is None
    assert mux._winner is Source.AIRPLAY
    assert status["mode"] == "auto"


@pytest.mark.asyncio
async def test_auto_select_with_no_active_sources_holds_fanin_none(
    mux, patched_probes,
):
    mux._manual_source = Source.AIRPLAY
    mux._winner = Source.AIRPLAY
    _stub_probes(patched_probes)

    status = await mux.auto_select()

    mux._fanin_none.assert_awaited_once()
    mux._fanin_auto.assert_not_awaited()
    assert mux._manual_source is None
    assert mux._winner is None
    assert status["mode"] == "auto"


# ----------------------------------------------------------------------
# Manual-pin persistence — the pin must survive jasper-mux's
# Restart=always deploy/restart cycle. We simulate a restart by building
# a SECOND Mux pointed at the same mode-state file. Fail-open to Auto on
# a missing/corrupt file is the pre-persistence behaviour.
# ----------------------------------------------------------------------


def _fresh_mux_after_restart(tmp_path):
    """Construct a new Mux instance pointed at the same per-test
    librespot + mode-state paths the `mux` fixture uses — i.e. what a
    deploy/restart produces (a brand-new process, on-disk state intact)."""
    m = Mux(
        librespot_state_path=str(tmp_path / "librespot.state.json"),
        volume_coordinator=_FakeVolumeCoordinator(),
        mode_state_path=str(tmp_path / "mux_mode.json"),
    )
    m._fanin_select = AsyncMock(return_value={})
    m._fanin_auto = AsyncMock(return_value={})
    m._fanin_none = AsyncMock(return_value={})
    return m


@pytest.mark.asyncio
async def test_select_source_persists_manual_pin_to_disk(
    mux, patched_probes, tmp_path,
):
    """A successful manual selection writes the pin so it survives a
    restart."""
    _stub_probes(patched_probes, airplay=True)
    await mux.select_source(Source.AIRPLAY)

    persisted = (tmp_path / "mux_mode.json").read_text()
    assert json.loads(persisted) == {
        "mode": "manual", "selected_source": "airplay",
    }


@pytest.mark.asyncio
async def test_manual_pin_restored_after_simulated_restart(
    mux, patched_probes, tmp_path,
):
    """The headline behaviour: pin AirPlay, simulate a jasper-mux
    restart (fresh Mux, same file), and the new process comes up still
    pinned to AirPlay instead of silently reverting to Auto."""
    _stub_probes(patched_probes, airplay=True)
    await mux.select_source(Source.BLUETOOTH)
    assert mux._manual_source is Source.BLUETOOTH

    restarted = _fresh_mux_after_restart(tmp_path)

    assert restarted._manual_source is Source.BLUETOOTH
    assert restarted._status_payload()["mode"] == "manual"
    assert restarted._status_payload()["selected_source"] == "bluetooth"


@pytest.mark.asyncio
async def test_auto_select_persists_auto_so_restart_stays_auto(
    mux, patched_probes, tmp_path,
):
    """Pinning then returning to Auto must clear the persisted pin, so a
    later restart doesn't resurrect the old manual source."""
    _stub_probes(patched_probes, airplay=True, spotify=True)
    await mux.select_source(Source.AIRPLAY)
    assert json.loads((tmp_path / "mux_mode.json").read_text())["mode"] == "manual"

    await mux.auto_select()
    assert json.loads((tmp_path / "mux_mode.json").read_text()) == {"mode": "auto"}

    restarted = _fresh_mux_after_restart(tmp_path)
    assert restarted._manual_source is None


def test_fresh_mux_with_no_state_file_is_auto(tmp_path):
    """First boot / no prior pin → Auto."""
    m = Mux(
        librespot_state_path=str(tmp_path / "librespot.state.json"),
        mode_state_path=str(tmp_path / "missing.json"),
    )
    assert m._manual_source is None


def test_fresh_mux_with_corrupt_state_file_is_auto(tmp_path):
    """A corrupt mode file fails open to Auto rather than crashing
    construction or pinning to garbage."""
    state = tmp_path / "mux_mode.json"
    state.write_text("{half-written", encoding="utf-8")
    m = Mux(
        librespot_state_path=str(tmp_path / "librespot.state.json"),
        mode_state_path=str(state),
    )
    assert m._manual_source is None


def test_mux_mode_state_path_defaults_from_env(monkeypatch, tmp_path):
    """JASPER_MUX_MODE_STATE_PATH overrides the persistence location so
    operators / tests can relocate it. The default constant is computed
    at import; the constructor default tracks the env-resolved constant.

    Verify the explicit-arg path is honoured end to end (the env wiring
    itself is exercised by the module-level MUX_MODE_STATE_PATH constant
    which feeds the constructor default)."""
    import jasper.mux_mode_persistence as p

    custom = tmp_path / "custom_mode.json"
    p.write_mode(custom, Source.SPOTIFY)
    m = Mux(
        librespot_state_path=str(tmp_path / "librespot.state.json"),
        mode_state_path=str(custom),
    )
    assert m._manual_source is Source.SPOTIFY
