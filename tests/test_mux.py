# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.mux — the renderer source-arbiter.

Tests focus on the transition-detection state machine, which is the
hard logic. The probe-implementation tests live in test_source_state.py
since the probes were factored out into jasper.source_state; here we
just patch their bound names in jasper.mux's namespace and mutate the
return values per tick.
"""
from __future__ import annotations
import json
import logging
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
    monkeypatch.setattr("jasper.mux.read_usbsink_state", lambda *a, **k: None)
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
    """If _pause throws, _tick should not crash. Post-handoff preemption
    goes through _pause_best_effort (audit C5), so a failing pause is
    logged and swallowed inside the tick rather than aborting the
    remaining per-source pauses."""
    _stub_probes(patched_probes, spotify=True, airplay=False, bluetooth=False)
    await mux._tick()
    _stub_probes(patched_probes, spotify=True, airplay=True, bluetooth=False)
    mux._pause = AsyncMock(side_effect=RuntimeError("pause API down"))
    await mux._tick()  # must not raise
    mux._pause.assert_awaited_once_with(Source.SPOTIFY)
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

    assert "-/var/lib/jasper-intsecrets/spotify_credentials.env" in env_files


def test_mux_service_can_write_state_dir():
    """The manual-pin persistence file (mux_mode.json) + shared
    speaker_volume.json live under /var/lib/jasper; the unit must keep that path
    writable under ProtectSystem=strict. Since S2, mux does NOT declare
    StateDirectory=jasper (jasper-voice is the sole owner, to kill the owner-flip
    re-chown race), so the write guarantee is now explicitly
    ReadWritePaths=/var/lib/jasper. Pin both halves: mux must not co-own the
    StateDirectory, and it must list /var/lib/jasper in ReadWritePaths."""
    unit = (REPO / "deploy" / "systemd" / "jasper-mux.service").read_text()
    lines = [line.strip() for line in unit.splitlines()]
    assert "StateDirectory=jasper" not in lines, (
        "mux must NOT declare StateDirectory=jasper (S2: jasper-voice is the "
        "single owner; co-ownership caused the /var/lib/jasper owner-flip race)."
    )
    has_rw_path = any(
        line.startswith("ReadWritePaths=") and "/var/lib/jasper" in line
        for line in lines
    )
    has_protect_system = any(
        line.startswith("ProtectSystem=") for line in lines
    )
    # Under ProtectSystem=strict (which mux runs), the explicit ReadWritePaths
    # entry is the ONLY thing keeping /var/lib/jasper writable.
    assert has_protect_system, "mux is expected to run ProtectSystem=strict"
    assert has_rw_path, (
        "mux must list /var/lib/jasper in ReadWritePaths so the source pin + "
        "speaker_volume.json stay writable under ProtectSystem=strict (S2)."
    )


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
# USB combo box arbitration. In combo mode jasper-usbsink is in standby
# (bridge `playing:false` forever) while fan-in DIRECT-captures the gadget.
# mux detects USB liveness from the direct lane's host-input counter advancing
# across ticks.
# ----------------------------------------------------------------------


def _make_combo_box(mux: Mux, monkeypatch, frames_seq):
    monkeypatch.setattr(
        "jasper.mux.read_usbsink_state",
        lambda *a, **k: {
            "standby": True,
            "playing": False,
            "host_connected": True,
            "rms_dbfs": -120.0,
        },
    )
    frames = list(frames_seq)
    idx = {"i": 0}

    async def _fanin():
        value = frames[min(idx["i"], len(frames) - 1)]
        idx["i"] += 1
        if value is None:
            return None
        return {
            "inputs": [
                {"label": "spotify", "source": "lane", "frames_read": 5},
                {
                    "label": "usbsink",
                    "source": "direct",
                    # The captured broken shape: direct lane-level frames_read
                    # can stay frozen while resampler.input_frames advances.
                    "frames_read": 0,
                    "resampler": {"input_frames": value},
                },
            ],
        }

    mux._fanin_status_best_effort = _fanin
    return mux


@pytest.mark.asyncio
async def test_combo_usb_streaming_takes_speaker_in_auto(
    mux, patched_probes, monkeypatch,
):
    _stub_pauses(mux)
    _stub_usbsink_preempt(mux)
    _stub_probes(patched_probes, usbsink=False)
    _make_combo_box(mux, monkeypatch, [0, 48_000, 96_000])

    await mux._tick()
    assert mux._winner is None

    await mux._tick()
    assert mux._winner is Source.USBSINK
    mux._pause.assert_not_awaited()
    payload = mux._status_payload()
    assert payload["active_source"] == "usbsink"
    assert payload["winner"] == "usbsink"
    assert payload["sources"]["usbsink"]["playing"] is True
    assert payload["usbsink"]["combo"] is True

    await mux._tick()
    assert mux._winner is Source.USBSINK


@pytest.mark.asyncio
async def test_combo_usb_idle_frames_never_win(mux, patched_probes, monkeypatch):
    _stub_pauses(mux)
    _stub_usbsink_preempt(mux)
    _stub_probes(patched_probes, usbsink=False)
    _make_combo_box(mux, monkeypatch, [0, 0, 0, 0])

    for _ in range(4):
        await mux._tick()
    assert mux._winner is None
    assert mux._status_payload()["active_source"] == "idle"


@pytest.mark.asyncio
async def test_combo_usb_preempted_by_newly_started_source(
    mux, patched_probes, monkeypatch,
):
    _stub_pauses(mux)
    _stub_usbsink_preempt(mux)
    _stub_probes(patched_probes, usbsink=False, airplay=False)
    _make_combo_box(mux, monkeypatch, [0, 48_000, 96_000, 144_000])

    await mux._tick()
    await mux._tick()
    assert mux._winner is Source.USBSINK

    _stub_probes(patched_probes, usbsink=False, airplay=True)
    await mux._tick()
    assert mux._winner is Source.AIRPLAY
    mux._pause.assert_any_await(Source.USBSINK)


@pytest.mark.asyncio
async def test_combo_usb_survives_single_fanin_status_miss(
    mux, patched_probes, monkeypatch,
):
    _stub_pauses(mux)
    _stub_usbsink_preempt(mux)
    _stub_probes(patched_probes, usbsink=False)
    _make_combo_box(mux, monkeypatch, [0, 48_000, None, 96_000])

    await mux._tick()
    await mux._tick()
    assert mux._winner is Source.USBSINK
    await mux._tick()
    assert mux._winner is Source.USBSINK
    await mux._tick()
    assert mux._winner is Source.USBSINK


@pytest.mark.asyncio
async def test_solo_box_never_takes_combo_path(mux, patched_probes, monkeypatch):
    _stub_pauses(mux)
    _stub_usbsink_preempt(mux)
    monkeypatch.setattr(
        "jasper.mux.read_usbsink_state",
        lambda *a, **k: {"playing": True, "host_connected": True},
    )
    fanin_probe = AsyncMock(return_value=None)
    mux._fanin_status_best_effort = fanin_probe
    _stub_probes(patched_probes, usbsink=True)

    await mux._tick()
    assert mux._winner is Source.USBSINK
    assert mux._usbsink_combo_box is False
    assert mux._status_payload()["usbsink"]["combo"] is False
    fanin_probe.assert_not_awaited()


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
# Combo-box preempt transport. On a combo/direct box (JASPER_FANIN_USB_DIRECT)
# jasper-fanin captures the gadget directly and the jasper-usbsink bridge is in
# standby, so the :8781 preempt POST silences nothing. mux instead MUTE/UNMUTEs
# the fan-in usbsink lane at its mix stage. Solo boxes keep the :8781 path.
# ----------------------------------------------------------------------


def _make_combo_mux_transports_stubbed(tmp_path):
    """Real Mux flagged as a combo box, with BOTH preempt transports stubbed so
    `_usbsink_set_preempt` / the reassertion run for real but touch no socket or
    HTTP. Returns (mux, fanin_lane_mute_mock, fake_http)."""
    m = Mux(librespot_state_path=str(tmp_path / "librespot.state.json"))
    m._usbsink_combo_box = True
    fanin_mute = AsyncMock(return_value={})
    m._fanin_lane_mute = fanin_mute
    fake_http = AsyncMock()
    fake_http.post.return_value.status_code = 200
    m._http = fake_http
    return m, fanin_mute, fake_http


@pytest.mark.asyncio
async def test_combo_preempt_mutes_fanin_lane_not_8781(tmp_path):
    """Combo box: silencing USB is a fan-in lane MUTE, never a :8781 POST."""
    m, fanin_mute, fake_http = _make_combo_mux_transports_stubbed(tmp_path)
    await m._usbsink_set_preempt(True, reason="preempted_by_winner")
    fanin_mute.assert_awaited_once_with("usbsink", True)
    fake_http.post.assert_not_awaited()
    assert m._usbsink_preempted is True


@pytest.mark.asyncio
async def test_combo_release_unmutes_fanin_lane(tmp_path):
    """Combo release mirrors the existing release: UNMUTE the fan-in lane so a
    fresh host pause-then-play can retake the speaker."""
    m, fanin_mute, fake_http = _make_combo_mux_transports_stubbed(tmp_path)
    m._usbsink_preempted = True
    await m._usbsink_set_preempt(False, reason="all_others_idle")
    fanin_mute.assert_awaited_once_with("usbsink", False)
    fake_http.post.assert_not_awaited()
    assert m._usbsink_preempted is False


@pytest.mark.asyncio
async def test_solo_box_preempt_still_posts_8781(tmp_path):
    """Solo box (bridge not in standby): the :8781 POST path is unchanged and
    the fan-in lane mute is NOT used. Its deletion is a follow-up PR."""
    m, fake_http = _make_real_mux_http_stubbed(tmp_path)
    m._usbsink_combo_box = False
    fanin_mute = AsyncMock(return_value={})
    m._fanin_lane_mute = fanin_mute
    await m._usbsink_set_preempt(True, reason="preempted_by_winner")
    fake_http.post.assert_awaited_once()
    fanin_mute.assert_not_awaited()
    assert m._usbsink_preempted is True


@pytest.mark.asyncio
async def test_combo_escape_hatch_never_mutes(monkeypatch, tmp_path):
    """JASPER_USBSINK_PREEMPT=disabled degrades the combo path to graceful mix
    identically to the :8781 path: mux tracks state but issues no mute."""
    monkeypatch.setenv("JASPER_USBSINK_PREEMPT", "disabled")
    m, fanin_mute, fake_http = _make_combo_mux_transports_stubbed(tmp_path)
    await m._usbsink_set_preempt(True, reason="preempted_by_winner")
    assert m._usbsink_preempted is True  # tracked optimistically
    fanin_mute.assert_not_awaited()
    fake_http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_combo_mute_failure_is_bounded_and_retried(tmp_path, caplog):
    """A failed fan-in mute degrades exactly like a failed :8781 POST: WARN,
    graceful mixing, tracked flag NOT advanced so the next tick re-attempts
    (1 Hz, no retry storm, no silent failure)."""
    m, fanin_mute, _ = _make_combo_mux_transports_stubbed(tmp_path)
    fanin_mute.side_effect = RuntimeError("fanin socket gone")
    with caplog.at_level(logging.WARNING):
        await m._usbsink_set_preempt(True, reason="preempted_by_winner")
    assert m._usbsink_preempted is False  # not advanced → will retry
    assert "fanin lane mute failed" in caplog.text
    # State guard did NOT latch, so a subsequent tick tries again and succeeds.
    fanin_mute.side_effect = None
    await m._usbsink_set_preempt(True, reason="preempted_by_winner")
    assert m._usbsink_preempted is True


@pytest.mark.asyncio
async def test_reassert_mute_reissues_while_combo_preempted(tmp_path):
    """fan-in does not persist the mute (restarts unmuted), so mux reasserts it
    each tick while preempted — the next tick re-mutes a restarted fan-in."""
    m, fanin_mute, _ = _make_combo_mux_transports_stubbed(tmp_path)
    m._usbsink_preempted = True
    await m._reassert_usbsink_preempt_mute()
    fanin_mute.assert_awaited_once_with("usbsink", True)


@pytest.mark.asyncio
async def test_reassert_mute_noops_off_the_combo_preempt_path(
    monkeypatch, tmp_path,
):
    """Reassertion is a no-op when USB isn't preempted, on a solo box (the
    :8781 bridge persists its own state), and under the escape hatch."""
    # Not preempted.
    m, fanin_mute, _ = _make_combo_mux_transports_stubbed(tmp_path)
    m._usbsink_preempted = False
    await m._reassert_usbsink_preempt_mute()
    fanin_mute.assert_not_awaited()

    # Solo box, even while preempted.
    m2, fanin_mute2, _ = _make_combo_mux_transports_stubbed(tmp_path)
    m2._usbsink_combo_box = False
    m2._usbsink_preempted = True
    await m2._reassert_usbsink_preempt_mute()
    fanin_mute2.assert_not_awaited()

    # Escape hatch active.
    monkeypatch.setenv("JASPER_USBSINK_PREEMPT", "disabled")
    m3, fanin_mute3, _ = _make_combo_mux_transports_stubbed(tmp_path)
    m3._usbsink_preempted = True
    await m3._reassert_usbsink_preempt_mute()
    fanin_mute3.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_combo_preempt_reaches_fanin_mute(
    mux, patched_probes, monkeypatch,
):
    """End-to-end through _tick: on a combo box, AirPlay preempting a playing
    USB source drives a fan-in lane MUTE (not a :8781 POST) via the real _pause
    path — proving the wiring, not just the transport method in isolation."""
    fanin_mute = AsyncMock(return_value={})
    mux._fanin_lane_mute = fanin_mute
    fake_post = AsyncMock(return_value=SimpleNamespace(status_code=200))
    mux._http.post = fake_post
    _stub_probes(patched_probes, usbsink=False, airplay=False)
    _make_combo_box(mux, monkeypatch, [0, 48_000, 96_000, 144_000])

    await mux._tick()  # baseline frames
    await mux._tick()  # USB advances → wins
    assert mux._winner is Source.USBSINK

    _stub_probes(patched_probes, usbsink=False, airplay=True)
    await mux._tick()  # AirPlay wins → USB preempted via fan-in mute
    assert mux._winner is Source.AIRPLAY
    fanin_mute.assert_any_await("usbsink", True)
    fake_post.assert_not_awaited()
    assert mux._usbsink_preempted is True


@pytest.mark.asyncio
async def test_tick_combo_muted_host_stays_playing_for_liveness(
    mux, patched_probes, monkeypatch,
):
    """The telemetry-decoupling invariant at the mux level: while USB is
    preempted (fan-in lane muted), the direct lane still reports advancing
    frames, so mux keeps seeing the host as "playing". If mute zeroed the
    telemetry, mux would see USB "stop", release, and flap."""
    mux._fanin_lane_mute = AsyncMock(return_value={})
    mux._http.post = AsyncMock(return_value=SimpleNamespace(status_code=200))
    _stub_probes(patched_probes, usbsink=False, airplay=False)
    # Frames keep advancing across every tick — a streaming (even if muted) host.
    _make_combo_box(mux, monkeypatch, [0, 48_000, 96_000, 144_000, 192_000])

    await mux._tick()
    await mux._tick()
    assert mux._winner is Source.USBSINK

    _stub_probes(patched_probes, usbsink=False, airplay=True)
    await mux._tick()  # AirPlay wins, USB muted
    assert mux._usbsink_preempted is True
    # USB frames still advance under the mute → mux still reads it as playing.
    assert mux._status_payload()["sources"]["usbsink"]["playing"] is True


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
async def test_test_fanin_label_overrides_manual_reassert_without_persisting(
    mux, patched_probes,
):
    mux._manual_source = Source.AIRPLAY
    mux._winner = Source.AIRPLAY
    mux._fanin_select_label = AsyncMock(return_value={})
    _stub_probes(patched_probes, airplay=False)

    status = await mux.select_test_fanin_label("correction")
    await mux._tick()

    assert status["mode"] == "manual"
    assert status["selected_source"] == "airplay"
    assert status["test_source"] == "correction"
    assert status["active_source"] == "correction"
    mux._fanin_select_label.assert_awaited_with("correction")
    assert mux._manual_source is Source.AIRPLAY


@pytest.mark.asyncio
async def test_test_fanin_release_restores_manual_source(mux):
    mux._manual_source = Source.AIRPLAY
    mux._winner = Source.AIRPLAY
    mux._test_fanin_label = "correction"
    mux._fanin_select = AsyncMock(return_value={})
    mux._fanin_none = AsyncMock(return_value={})

    status = await mux.release_test_fanin_label()

    mux._fanin_select.assert_awaited_once_with(Source.AIRPLAY)
    mux._fanin_none.assert_not_awaited()
    assert status["test_source"] is None
    assert status["selected_source"] == "airplay"
    assert status["active_source"] == "airplay"


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
async def test_bluetooth_preempt_uses_avrcp_pause(mux, monkeypatch):
    calls: list[str] = []

    async def fake_avrcp(method: str) -> None:
        calls.append(method)

    monkeypatch.setattr("jasper.mux.bluetooth_avrcp_call", fake_avrcp)

    await mux._pause(Source.BLUETOOTH)

    assert calls == ["Pause"]


@pytest.mark.asyncio
async def test_bluetooth_preempt_avrcp_failure_is_best_effort(
    mux, monkeypatch, caplog,
):
    async def fake_avrcp(method: str) -> None:
        raise RuntimeError("no player")

    monkeypatch.setattr("jasper.mux.bluetooth_avrcp_call", fake_avrcp)

    with caplog.at_level(logging.WARNING, logger="jasper.mux"):
        await mux._pause(Source.BLUETOOTH)

    assert "event=bluetooth.preempt_pause_failed" in caplog.records[-1].message
    assert "phone_side_pause_required" in caplog.records[-1].message


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


# ---------------------------------------------------------------------------
# Audit C5 — _tick hygiene: preempt-release locking, best-effort pause
# fan-out, and removal of the never-implemented DEBOUNCE_TICKS policy.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usbsink_preempt_release_runs_inside_transition_lock(
    mux, patched_probes,
):
    """The new-transition USB preempt release must hold _transition_lock —
    otherwise a concurrent manual select_source can interleave between
    the release and the handoff (the pre-fix shape)."""
    held_during_release: list[bool] = []

    async def recording_release(silenced, *, reason):
        held_during_release.append(mux._transition_lock.locked())
        mux._usbsink_preempted = silenced

    mux._usbsink_set_preempt = recording_release
    mux._usbsink_preempted = True
    _stub_probes(patched_probes, usbsink=True)
    _stub_pauses(mux)
    await mux._tick()
    assert mux._winner is Source.USBSINK
    # First call is the new_transition release; it must be under the lock.
    assert held_during_release and held_during_release[0] is True


@pytest.mark.asyncio
async def test_one_pause_failure_does_not_abort_pausing_the_rest(
    mux, patched_probes,
):
    """Post-handoff preemption pauses every other active source. One
    renderer's pause raising (Spotify Web API down, busctl missing)
    must not skip the remaining sources or blow up the tick."""
    _stub_probes(patched_probes, spotify=True, bluetooth=True)
    _stub_pauses(mux)
    await mux._tick()  # establish a winner with two sources up
    mux._pause.reset_mock()

    _stub_probes(patched_probes, spotify=True, bluetooth=True, airplay=True)
    mux._pause = AsyncMock(side_effect=[RuntimeError("web api down"), None])
    await mux._tick()  # AirPlay wins; both others get pause attempts
    assert mux._winner is Source.AIRPLAY
    pause_targets = {c.args[0] for c in mux._pause.await_args_list}
    assert pause_targets == {Source.SPOTIFY, Source.BLUETOOTH}


def test_debounce_ticks_constant_removed():
    """DEBOUNCE_TICKS documented an anti-flap hold that was never
    implemented (dead since the file's first commit). The constant was
    deleted rather than activated — see the commit message rationale.
    This guards against the comment/constant reappearing without an
    actual implementation + tests."""
    assert not hasattr(Mux, "DEBOUNCE_TICKS")
