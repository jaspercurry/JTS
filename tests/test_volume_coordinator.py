# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.volume_coordinator.

Covers:
- mapping helpers round-trip
- set/adjust/mute/unmute on each source
- camilla-as-master for idle/AirPlay; push-mode for Spotify/BT
- echo prevention: own-write within window is ignored on observe
- observe out-of-window changes update listening_level + persist
- initialize() applies regression and DOES NOT bump last_used_at
- subsequent set_listening_level DOES bump last_used_at
- camilla restart-blip: best_effort=True calls survive unavailability
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from jasper import bluealsa_probe
from jasper import volume_coordinator as vc_mod
from jasper.volume_coordinator import (
    AIRPLAY_DB_MAX,
    AIRPLAY_DB_MIN,
    BT_VOLUME_MAX,
    ECHO_WINDOW_SEC,
    Source,
    VolumeCoordinator,
    airplay_db_to_listening_level,
    bt_volume_to_listening_level,
    listening_level_to_airplay_db,
    listening_level_to_bt_volume,
    listening_level_to_spotify_percent,
    spotify_percent_to_listening_level,
)
from jasper.volume_diagnostics import read_diagnostics
from jasper.volume_persistence import VolumePersistence, percent_to_db


@pytest.fixture(autouse=True)
def _reset_bluealsa_probe_state():
    bluealsa_probe._reset_for_tests()
    yield
    bluealsa_probe._reset_for_tests()


# ---------- mapping helpers -------------------------------------------------


@pytest.mark.parametrize("level", [0, 25, 50, 75, 100])
def test_airplay_round_trip(level):
    db = listening_level_to_airplay_db(level)
    assert AIRPLAY_DB_MIN <= db <= AIRPLAY_DB_MAX
    assert airplay_db_to_listening_level(db) == level


@pytest.mark.parametrize("level", [0, 50, 100])
def test_spotify_round_trip(level):
    pct = listening_level_to_spotify_percent(level)
    assert spotify_percent_to_listening_level(pct) == level


@pytest.mark.parametrize("level", [0, 25, 50, 75, 100])
def test_bt_round_trip(level):
    vol = listening_level_to_bt_volume(level)
    assert 0 <= vol <= BT_VOLUME_MAX
    # ±1pp slack for the percent↔127 conversion at non-multiples
    assert abs(bt_volume_to_listening_level(vol) - level) <= 1


def test_clamping_below_zero_and_above_100():
    assert listening_level_to_airplay_db(-10) == AIRPLAY_DB_MIN
    assert listening_level_to_airplay_db(150) == AIRPLAY_DB_MAX
    assert listening_level_to_bt_volume(-10) == 0
    assert listening_level_to_bt_volume(150) == BT_VOLUME_MAX


# ---------- coordinator dispatch -------------------------------------------


class _FakeCamilla:
    def __init__(self, db: float = 0.0) -> None:
        self._db = db
        self.muted = False
        self.set_calls: list[float] = []
        self.mute_calls: list[bool] = []
        self.events: list[tuple[str, float | bool]] = []
        self.get_calls: int = 0
        # When True, every best_effort call is a no-op (writes return
        # False, reads return None) to simulate a camilla restart blip.
        # Non-best_effort calls raise CamillaUnavailable.
        self.unavailable = False

    async def get_volume_db(self, *, best_effort: bool = False) -> float | None:
        self.get_calls += 1
        if self.unavailable:
            if best_effort:
                return None
            from jasper.camilla import CamillaUnavailable
            raise CamillaUnavailable("test fake offline")
        return self._db

    async def get_volume_and_mute(
        self, *, best_effort: bool = False,
    ) -> tuple[float, bool] | None:
        self.get_calls += 1
        if self.unavailable:
            if best_effort:
                return None
            from jasper.camilla import CamillaUnavailable
            raise CamillaUnavailable("test fake offline")
        return self._db, self.muted

    async def set_volume_db(
        self, db: float, *, best_effort: bool = False,
    ) -> bool:
        if self.unavailable:
            if best_effort:
                return False
            from jasper.camilla import CamillaUnavailable
            raise CamillaUnavailable("test fake offline")
        self._db = db
        self.set_calls.append(db)
        self.events.append(("volume", db))
        return True

    async def set_main_mute(
        self, muted: bool, *, best_effort: bool = False,
    ) -> bool:
        if self.unavailable:
            if best_effort:
                return False
            from jasper.camilla import CamillaUnavailable
            raise CamillaUnavailable("test fake offline")
        self.muted = bool(muted)
        self.mute_calls.append(bool(muted))
        self.events.append(("mute", bool(muted)))
        return True


class _FakeBackend:
    def __init__(
        self,
        active: dict[str, bool] | None = None,
        selected: str | None = None,
    ) -> None:
        self._active = active or {}
        self._selected = selected

    async def active_renderers(self) -> dict[str, bool]:
        return dict(self._active)

    async def selected_source(self) -> str | None:
        return self._selected


class _RecordingCoordinator(VolumeCoordinator):
    """Subclass that records source-side dispatch calls without
    actually invoking subprocess busctl / HTTP. Replaces `_set_*`
    methods with capture lists. Mirrors production semantics:
    idle/AirPlay use camilla; Spotify/BT are push-mode."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.airplay_writes: list[int] = []
        self.spotify_writes: list[int] = []
        self.bt_writes: list[int] = []
        self.camilla_writes: list[int] = []

    async def _set_airplay(self, level: int) -> bool:
        self.airplay_writes.append(level)
        return await self._set_camilla(level)

    async def _set_spotify(self, level: int) -> bool:
        self.spotify_writes.append(level)
        self._stamp_outbound(Source.SPOTIFY, level)
        return True

    async def _set_bluetooth(self, level: int) -> bool:
        self.bt_writes.append(level)
        self._stamp_outbound(Source.BLUETOOTH, level)
        return True

    async def _set_camilla(self, level: int) -> bool:
        ok = await super()._set_camilla(level)
        self.camilla_writes.append(level)
        return ok


def _coord(
    tmp_path,
    *,
    active: dict[str, bool] | None = None,
    selected: str | None = None,
    db: float = 0.0,
):
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=db)
    backend = _FakeBackend(active=active, selected=selected)
    coord = _RecordingCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend,
        spotify_router=None,  # tests bypass _set_spotify dispatch
        handoff_settle_sec=0.0,
    )
    return coord, cam, persistence


async def test_aclose_is_safe_without_owned_observer_tasks(tmp_path):
    coord, _, _ = _coord(tmp_path, active={})

    await coord.aclose()


# ---------- outbound dispatch ----------------------------------------------


async def test_set_volume_idle_writes_camilla(tmp_path):
    coord, cam, _ = _coord(tmp_path, active={})
    await coord.set_listening_level(70)
    assert coord.camilla_writes == [70]
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(70))
    assert cam.mute_calls[-1] is False


async def test_set_volume_zero_hard_mutes_camilla_master(tmp_path):
    """0% content volume is a real mute, not just the dB curve bottom."""
    coord, cam, persistence = _coord(tmp_path, active={})

    await coord.set_listening_level(0)

    assert coord.camilla_writes == [0]
    assert cam.mute_calls[-1] is True
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 0
    assert record.main_volume_db == pytest.approx(percent_to_db(0))


async def test_set_volume_nonzero_clears_mute_after_volume_write(tmp_path):
    """Unmute order is volume first, then main_mute=false, so there is
    no full-scale transient while returning from a 0% content mute."""
    coord, cam, _ = _coord(tmp_path, active={}, db=-50.0)
    cam.muted = True

    await coord.set_listening_level(75)

    assert cam.events[-2:] == [
        ("volume", pytest.approx(percent_to_db(75))),
        ("mute", False),
    ]


async def test_set_volume_airplay_active_routes_to_camilla(tmp_path):
    """AirPlay is camilla-as-master: dial/voice/HTTP changes must be
    audible even though modern AirPlay 2 sender slider reflection via
    shairport-sync is unavailable."""
    coord, cam, _ = _coord(
        tmp_path, active={"aplactive": True}, db=0.0,
    )
    await coord.set_listening_level(50)
    assert coord.airplay_writes == [50]
    assert coord.camilla_writes == [50]


async def test_manual_selected_source_overrides_raw_renderer_probe(tmp_path):
    """Source selection gates what the speaker actually passes, so
    volume dispatch follows mux's manual selection over raw activity."""
    coord, _, _ = _coord(
        tmp_path,
        active={"aplactive": True},
        selected="spotify",
        db=0.0,
    )

    await coord.set_listening_level(55)

    assert coord.spotify_writes == [55]
    assert coord.airplay_writes == []


async def test_set_volume_spotify_active_routes_to_spotify(tmp_path):
    coord, cam, _ = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0,
    )
    await coord.set_listening_level(40)
    assert coord.spotify_writes == [40]
    assert cam.set_calls == []  # Spotify is push-mode; camilla untouched


async def test_push_mode_zero_sets_final_mute_after_source_push(tmp_path):
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=0.0,
    )

    await coord.set_listening_level(0)

    assert coord.spotify_writes == [0]
    assert cam.mute_calls[-1] is True
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    record = persistence.load()
    assert record is not None
    assert record.main_volume_db == pytest.approx(percent_to_db(0))


async def test_push_mode_nonzero_clears_stale_final_mute(tmp_path):
    coord, cam, _ = _coord(
        tmp_path, active={"spotactive": True}, db=-50.0,
    )
    cam.muted = True

    await coord.set_listening_level(75)

    assert coord.spotify_writes == [75]
    assert cam.events[-2:] == [
        ("volume", pytest.approx(0.0)),
        ("mute", False),
    ]


async def test_set_volume_spotify_failure_updates_camilla_guard(tmp_path):
    """If the active push source cannot accept volume, normal user
    volume changes still keep the audible path guarded by Camilla."""
    coord, cam, _ = _coord(
        tmp_path, active={"spotactive": True}, db=0.0,
    )

    async def fail_spotify(_level: int) -> bool:
        return False

    coord._set_spotify = fail_spotify

    await coord.set_listening_level(25)

    assert cam.set_calls[-1] == pytest.approx(percent_to_db(25))


async def test_set_volume_spotify_failure_records_guard_diagnostics(
    tmp_path, monkeypatch,
):
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    coord, _, _ = _coord(
        tmp_path, active={"spotactive": True}, db=0.0,
    )

    async def fail_spotify(_level: int) -> bool:
        return False

    coord._set_spotify = fail_spotify

    await coord.set_listening_level(25)

    diag = read_diagnostics(str(diag_path))
    assert diag["push_guard"]["active"] is True
    assert diag["push_guard"]["source"] == "spotify"
    assert diag["push_guard"]["level"] == 25
    assert diag["push_guard"]["guard_db"] == pytest.approx(
        round(percent_to_db(25), 2)
    )
    assert diag["push_guard"]["reason"] == "push_write_failed"


async def test_set_volume_bluetooth_active_routes_to_bt(tmp_path):
    coord, cam, _ = _coord(
        tmp_path, active={"btactive": True}, db=-25.0,
    )
    await coord.set_listening_level(60)
    assert coord.bt_writes == [60]
    assert cam.set_calls == []  # BT is push-mode; camilla untouched


async def test_idle_to_push_source_transition_pins_camilla(tmp_path):
    """idle→push-mode-source transition pins camilla to 0 dB and
    pushes listening_level to the new source's slider."""
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True}, db=-25.0)
    await coord.set_listening_level(50)
    # Push-mode dispatch: spotify_writes captures, camilla untouched.
    assert cam.set_calls == []
    # Now simulate a transition from idle:
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)
    # Camilla should be pinned to 0 dB.
    assert cam.set_calls == [0.0]
    # And listening_level pushed to the new source again.
    assert coord.spotify_writes[-1] == 50


async def test_push_source_to_idle_transition_restores_camilla(tmp_path):
    """push-mode-source → idle transition hands camilla back to
    listening_level percent. Uses SPOTIFY (push) → IDLE."""
    coord, cam, _ = _coord(tmp_path, active={}, db=0.0)
    await coord.set_listening_level(60)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(60))
    # Simulate transition from spotify back to idle
    await coord.apply_active_source_transition(Source.SPOTIFY, Source.IDLE)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(60))


async def test_transition_suppressed_during_voice_session(tmp_path):
    """note_voice_session(True) gates apply_active_source_transition
    so the ducker's additive math isn't corrupted by absolute writes.
    Uses SPOTIFY (push) so the transition would otherwise write camilla."""
    coord, cam, _ = _coord(tmp_path, active={}, db=0.0)
    coord.note_voice_session(True)
    initial_calls = list(cam.set_calls)
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)
    # No new camilla writes — gated.
    assert cam.set_calls == initial_calls
    coord.note_voice_session(False)
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)
    # Now the transition fires: idle → spotify
    # (push-mode) pushes the source and leaves Camilla already unmuted
    # at its 0 dB carrier.
    assert coord.spotify_writes[-1] == coord.get_listening_level()
    assert cam.muted is False


async def test_airplay_priority_over_spotify_over_bt(tmp_path):
    """When multiple sources report active (transition window),
    coordinator picks airplay > spotify > bt."""
    coord, _, _ = _coord(
        tmp_path,
        active={"aplactive": True, "spotactive": True, "btactive": True},
    )
    await coord.set_listening_level(50)
    # AirPlay won the priority chain → AirPlay path fired.
    assert coord.airplay_writes == [50]
    assert coord.camilla_writes == [50]
    assert coord.spotify_writes == []
    assert coord.bt_writes == []


async def test_adjust_volume(tmp_path):
    """Push-mode adjust path: each set/adjust pushes a fresh value
    to the source's slider."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(50)
    await coord.adjust_listening_level(15)
    assert coord.spotify_writes == [50, 65]


async def test_adjust_clamps_to_0_and_100(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(95)
    await coord.adjust_listening_level(20)
    assert coord.spotify_writes[-1] == 100
    await coord.adjust_listening_level(-200)
    assert coord.spotify_writes[-1] == 0


async def test_mute_then_unmute(tmp_path):
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(70)
    saved = await coord.mute()
    assert saved == 70
    assert coord.spotify_writes[-1] == 0  # silence
    assert cam.mute_calls[-1] is True
    assert coord.is_muted()
    restored = await coord.unmute()
    assert restored == 70
    assert coord.spotify_writes[-1] == 70
    assert cam.mute_calls[-1] is False
    assert not coord.is_muted()


async def test_unmute_without_prior_mute_uses_fallback(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    restored = await coord.unmute(fallback_level=50)
    assert restored == 50


# ---------- echo prevention ------------------------------------------------


# Echo-prevention tests use SPOTIFY as a representative push-mode source.


async def test_observe_within_echo_window_ignored(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    # Immediate observation echoing back the same value → ignored.
    await coord.observe_source_volume(Source.SPOTIFY, 60)
    # listening_level unchanged; no extra source writes.
    assert coord.get_listening_level() == 60
    assert coord.spotify_writes == [60]


async def test_observe_outside_echo_window_updates_level(tmp_path, monkeypatch):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    # Fast-forward time past the echo window without sleeping.
    fake_now = time.monotonic() + ECHO_WINDOW_SEC + 1.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    await coord.observe_source_volume(Source.SPOTIFY, 40)
    assert coord.get_listening_level() == 40
    # Observation should NOT trigger an outbound dispatch (no echo).
    assert coord.spotify_writes == [60]


async def test_observe_different_value_within_window_is_ignored(tmp_path):
    """A poll can briefly see stale source state right after our write,
    especially during source handoff; ignore the whole echo window."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    await coord.observe_source_volume(Source.SPOTIFY, 30)
    assert coord.get_listening_level() == 60


async def test_observe_persists_listening_level(tmp_path, monkeypatch):
    coord, _, persistence = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    fake_now = time.monotonic() + ECHO_WINDOW_SEC + 1.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    await coord.observe_source_volume(Source.SPOTIFY, 40)
    rec = persistence.load()
    assert rec is not None
    assert rec.listening_level == 40


async def test_observe_spotify_user_change_clears_degraded_guard(tmp_path):
    """A real source-side Spotify slider move proves the source volume
    surface is carrying user intent. Clear any degraded-safe Camilla
    guard so the path returns to normal push-mode loudness."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0,
    )
    coord._level = 50
    persistence.save_listening_level(50)
    await coord._set_camilla_db(
        -25.0,
        context="test_degraded_guard",
        persist=True,
    )

    await coord.observe_source_volume(Source.SPOTIFY, 100)

    assert coord.get_listening_level() == 100
    assert cam.set_calls[-1] == pytest.approx(0.0)
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 100
    assert record.main_volume_db == pytest.approx(0.0)


async def test_observe_spotify_same_level_clears_degraded_guard(tmp_path):
    """A source observation at the canonical level is also proof that
    the push-mode source already carries listening_level."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0,
    )
    coord._level = 100
    persistence.save_listening_level(100)
    await coord._set_camilla_db(
        -25.0,
        context="test_degraded_guard",
        persist=True,
    )

    await coord.observe_source_volume(Source.SPOTIFY, 100)

    assert coord.get_listening_level() == 100
    assert cam.set_calls[-1] == pytest.approx(0.0)
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 100
    assert record.main_volume_db == pytest.approx(0.0)


async def test_observe_spotify_clear_deferred_during_duck_keeps_guard(
    tmp_path, monkeypatch,
):
    """A push confirmation during an active duck is not a real carrier
    clear. Keep the guard persisted so the observer can retry later."""
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-13.0,
    )
    coord._level = 90
    persistence.save_listening_level(90)
    persistence.save_now(-13.0)

    async def probe():
        return True

    coord._duck_active_probe = probe

    await coord.observe_source_volume(Source.SPOTIFY, 90)

    assert cam.set_calls == []
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 90
    assert record.main_volume_db == pytest.approx(-13.0)
    diag = read_diagnostics(str(diag_path))
    assert diag["last_clear_event"]["ok"] is False
    assert diag["last_clear_event"]["reason"] == "clear_deferred_duck_active"
    assert diag.get("push_guard", {}).get("active") is not False


async def test_observe_spotify_repairs_live_guard_after_false_clear(
    tmp_path,
):
    """Recover from the legacy split-brain: persistence claimed the push
    guard was clear, but live Camilla was still attenuating the path."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-13.0,
    )
    coord._level = 90
    persistence.save_listening_level(90)
    persistence.save_now(0.0)

    async def probe():
        return False

    coord._duck_active_probe = probe

    await coord.observe_source_volume(Source.SPOTIFY, 90)

    assert cam.set_calls[-1] == pytest.approx(0.0)
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 90
    assert record.main_volume_db == pytest.approx(0.0)


async def test_successful_push_dispatch_clears_degraded_guard(
    tmp_path, monkeypatch,
):
    """If a later outbound push succeeds, Camilla should stop carrying
    the degraded fallback attenuation."""
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0,
    )
    coord._level = 50
    persistence.save_listening_level(50)
    await coord._set_camilla_db(
        -25.0,
        context="test_degraded_guard",
        persist=True,
    )

    await coord.set_listening_level(50)

    assert coord.spotify_writes == [50]
    assert cam.set_calls[-1] == pytest.approx(0.0)
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 50
    assert record.main_volume_db == pytest.approx(0.0)
    diag = read_diagnostics(str(diag_path))
    assert diag["push_guard"]["active"] is False
    assert diag["last_clear_event"]["source"] == "spotify"
    assert diag["last_clear_event"]["previous_db"] == pytest.approx(-25.0)
    assert diag["last_clear_event"]["reason"] == "push_confirmed"


async def test_observe_respects_recent_cross_process_write(tmp_path):
    """Hardware knobs hit jasper-control, which has a separate
    coordinator and no shared outbound stamp. A stale observer poll
    should not undo the freshly persisted knob level."""
    coord, _, persistence = _coord(tmp_path, active={"spotactive": True})
    coord._level = 70
    persistence.save_listening_level(70)

    # Simulate jasper-control in another process handling a knob twist.
    persistence.save_listening_level(80)

    assert coord._is_recent_cross_process_write(70)
    await coord.observe_source_volume(Source.SPOTIFY, 70)

    assert coord.get_listening_level() == 80
    rec = persistence.load()
    assert rec is not None and rec.listening_level == 80


# ---------- initialize / boot regression ----------------------------------


async def test_initialize_first_boot_uses_default(tmp_path):
    coord, _, persistence = _coord(tmp_path, active={})
    target, reason = await coord.initialize(first_boot_default_pct=42)
    assert target == 42
    assert "first-boot" in reason
    # Persistence has the new level.
    rec = persistence.load()
    assert rec is not None and rec.listening_level == 42


async def test_initialize_does_not_bump_last_used_at(tmp_path):
    """Boot-time restore must NOT update last_used_at — otherwise
    every restart resets the idle-reset clock and yesterday's
    bedtime 90% never gets clamped."""
    coord, _, persistence = _coord(tmp_path, active={})
    # Pre-seed an old record with last_used_at days ago.
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    persistence._current_main_volume_db = -25.0
    persistence._current_listening_level = 90
    persistence._current_last_used_at = old_ts
    persistence._write_full()

    await coord.initialize(
        stale_after_sec=60.0,
        safe_low_pct=20, safe_high_pct=70,
        first_boot_default_pct=50,
    )
    rec = persistence.load()
    assert rec is not None
    # Old last_used_at preserved (within 1 second tolerance for round-trip).
    assert rec.last_used_at is not None
    assert abs((rec.last_used_at - old_ts).total_seconds()) < 1.0


async def test_user_change_bumps_last_used_at(tmp_path):
    coord, _, persistence = _coord(tmp_path, active={})
    await coord.set_listening_level(45)
    rec = persistence.load()
    assert rec is not None
    assert rec.last_used_at is not None
    # Should be very recent (within last 5 seconds).
    age = (datetime.now(timezone.utc) - rec.last_used_at).total_seconds()
    assert 0 <= age < 5


# ---------- AirPlay camilla-master dispatch --------------------------------


async def test_set_airplay_delegates_to_camilla_without_subprocess(
    tmp_path,
    monkeypatch,
):
    """Real _set_airplay path: use CamillaDSP as the reliable audible
    AirPlay volume surface, not shairport-sync DACP/DBus."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    async def fail_spawn(*args, **kwargs):
        raise AssertionError("AirPlay should not spawn a control subprocess")

    monkeypatch.setattr(vc_mod.asyncio, "create_subprocess_exec", fail_spawn)

    await coord._set_airplay(75)

    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(75))
    assert Source.AIRPLAY not in coord._last_outbound


async def test_observe_airplay_is_ignored(tmp_path):
    """AirPlay sender slider is upstream trim, not canonical JTS
    volume, while AirPlay is camilla-as-master."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"aplactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70)

    db = listening_level_to_airplay_db(30)
    await coord.observe_source_volume(Source.AIRPLAY, db)

    assert coord.get_listening_level() == 70
    rec = persistence.load()
    assert rec is not None and rec.listening_level == 70


async def test_observe_inactive_source_is_ignored(tmp_path):
    """Stale readings from a non-current renderer must not steal the
    canonical level from the active source."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70)

    await coord.observe_source_volume(
        Source.AIRPLAY, listening_level_to_airplay_db(30),
    )

    assert coord.get_listening_level() == 70


# ---------- transitions across idle / active-renderer boundary -------------


async def test_transition_airplay_to_spotify_clears_camilla_and_pushes_spotify(tmp_path):
    """AirPlay is camilla-master; Spotify is push-mode. Switching to
    Spotify clears residual Camilla attenuation and pushes the same
    listening_level to Spotify."""
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(60)
    assert coord.airplay_writes == [60]
    assert coord.camilla_writes == [60]
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(60))
    # Now the user switches to Spotify Connect. Active flips to spot.
    coord._backend = _FakeBackend(active={"spotactive": True})
    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)
    assert cam.set_calls[-1] == 0.0
    assert coord.spotify_writes == [60]


async def test_transition_spotify_to_airplay_restores_camilla(tmp_path):
    """Spotify is push-mode; AirPlay is camilla-master. Switching to
    AirPlay restores Camilla to the remembered listening_level."""
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(50)
    assert coord.spotify_writes == [50]
    assert cam.set_calls == []
    coord._backend = _FakeBackend(active={"aplactive": True})
    await coord.apply_active_source_transition(Source.SPOTIFY, Source.AIRPLAY)
    assert coord.airplay_writes == []
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(50))


async def test_handoff_spotify_to_airplay_guards_camilla_before_gate(tmp_path):
    """Push-mode → camilla-master handoff lowers Camilla before mux
    exposes the AirPlay lane."""
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True}, db=0.0)
    await coord.set_listening_level(50)

    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert handoff.ok
    assert handoff.guard_db == pytest.approx(percent_to_db(50))
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(50))


async def test_handoff_catches_lower_level_during_guard_settle(tmp_path):
    """If the user lowers volume while Camilla is settling, handoff
    catches down before mux opens the target lane."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=0.0,
    )
    await coord.set_listening_level(50)
    original_set_camilla_db = coord._set_camilla_db
    lowered = False

    async def set_and_lower_once(db, *, context, persist):
        nonlocal lowered
        ok = await original_set_camilla_db(
            db, context=context, persist=persist,
        )
        if context == "source_handoff_guard" and not lowered:
            persistence.save_listening_level(20, mark_user_change=True)
            lowered = True
        return ok

    coord._set_camilla_db = set_and_lower_once

    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert handoff.ok
    assert handoff.level == 20
    assert handoff.guard_db == pytest.approx(percent_to_db(20))
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(20))


async def test_handoff_airplay_to_spotify_pushes_before_finalize(tmp_path):
    """Camilla-master → push-mode handoff pushes the source volume
    before mux opens the source, then finalize pins Camilla to 0 dB."""
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True}, db=-25.0)
    await coord.set_listening_level(60)
    coord.spotify_writes.clear()

    handoff = await coord.prepare_source_handoff(
        Source.AIRPLAY, Source.SPOTIFY, reason="manual",
    )

    assert handoff.ok
    assert handoff.push_ok is True
    assert coord.spotify_writes == [60]
    await coord.finalize_source_handoff(handoff)
    assert cam.set_calls[-1] == pytest.approx(0.0)


async def test_handoff_push_failure_keeps_camilla_guarded(tmp_path):
    """If a push-mode source cannot accept volume, handoff degrades
    safe by keeping downstream Camilla at the canonical guard."""
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True}, db=0.0)
    await coord.set_listening_level(40)

    async def fail_spotify(_level: int) -> bool:
        return False

    coord._set_spotify = fail_spotify
    handoff = await coord.prepare_source_handoff(
        Source.AIRPLAY, Source.SPOTIFY, reason="manual",
    )

    assert handoff.result == "degraded_safe"
    assert handoff.push_ok is False
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))
    await coord.finalize_source_handoff(handoff)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))


async def test_observer_transition_push_failure_preserves_guard(tmp_path):
    """The observer backstop must not undo mux's degraded-safe guard.

    If Spotify/Bluetooth cannot accept a source-side volume write,
    Camilla remains the fallback safety carrier instead of being
    cleared to 0 dB on the next active-source observer tick.
    """
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True}, db=0.0)
    await coord.set_listening_level(40)

    async def fail_spotify(_level: int) -> bool:
        return False

    coord._set_spotify = fail_spotify
    coord._backend = _FakeBackend(active={"spotactive": True})

    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert cam.set_calls
    assert 0.0 not in cam.set_calls
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))


async def test_handoff_ducked_camilla_master_waits_until_guard_safe(tmp_path):
    """During a voice duck, a camilla-master target is only safe if the
    current ducked Camilla level is already below the target guard.
    The target is still persisted so Ducker.restore lands safe."""
    coord, cam, persistence = _coord(
        tmp_path,
        active={"spotactive": True},
        selected="airplay",
        db=-25.0,
    )
    coord._level = 20  # target guard is percent_to_db(20); duck is too loud
    coord._persistence.save_listening_level(20, mark_user_change=True)
    persistence.save_now(0.0)

    async def duck_active():
        return True

    coord._duck_active_probe = duck_active

    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert not handoff.ok
    assert handoff.detail == "camilla_guard_failed"
    assert cam.set_calls == []
    rec = persistence.load()
    assert rec is not None
    assert rec.main_volume_db == pytest.approx(round(percent_to_db(20), 2))


async def test_handoff_ducked_safe_guard_reports_restore_target(tmp_path):
    """If the duck has already made Camilla quiet enough, prepare may
    succeed and Ducker.restore still targets the selected source level."""
    coord, _, persistence = _coord(
        tmp_path,
        active={"spotactive": True},
        selected="airplay",
        db=-45.0,
    )
    coord._level = 20
    persistence.save_listening_level(20, mark_user_change=True)

    async def duck_active():
        return True

    coord._duck_active_probe = duck_active

    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert handoff.ok
    assert await coord.get_camilla_target_db() == pytest.approx(percent_to_db(20))


async def test_ducker_restore_preserves_degraded_push_guard(tmp_path):
    """Push-mode normally restores Camilla to 0 dB, but a degraded
    handoff guard is intentional safety state and must survive restore."""
    coord, cam, _ = _coord(
        tmp_path,
        active={"spotactive": True},
        selected="spotify",
        db=0.0,
    )
    await coord.set_listening_level(35)
    guard_db = -32.5
    await coord._set_camilla_db(
        guard_db,
        context="test_degraded_guard",
        persist=True,
    )

    assert await coord.get_camilla_target_db() == pytest.approx(guard_db)

    await coord._set_camilla_db(
        0.0,
        context="test_normal_push",
        persist=True,
    )
    assert await coord.get_camilla_target_db() == pytest.approx(0.0)


async def test_transition_idle_to_airplay_keeps_camilla_level(tmp_path):
    """Idle and AirPlay both use camilla, so no handoff write or
    sender push is needed."""
    coord, cam, _ = _coord(tmp_path, active={})
    await coord.set_listening_level(40)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(40))
    coord._backend = _FakeBackend(active={"aplactive": True})
    await coord.apply_active_source_transition(Source.IDLE, Source.AIRPLAY)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))
    assert coord.airplay_writes == []
    assert coord.spotify_writes == []


async def test_transition_spotify_to_bluetooth_pushes_to_new_source(tmp_path):
    """Both sides push-mode. The new source's slider needs to be pushed;
    Camilla's carrier is already safe for a nonzero level."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(55)
    assert coord.spotify_writes == [55]
    coord._backend = _FakeBackend(active={"btactive": True})
    await coord.apply_active_source_transition(Source.SPOTIFY, Source.BLUETOOTH)
    # BT got the level pushed.
    assert coord.bt_writes == [55]


async def test_set_camilla_deferred_during_voice_session(tmp_path):
    """During a voice session the Ducker owns camilla; coordinator
    writes are deferred. Regression for the dial-during-duck overshoot:
    the dial path goes set_listening_level → _dispatch → _set_camilla,
    and was unconditionally writing camilla mid-duck. Now it returns
    early on voice_session_active. listening_level still updates so
    Ducker.restore lands at the user's intended level."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-25.0)  # already ducked
    backend = _FakeBackend(active={})  # idle (camilla carries level)
    # Real coordinator, not the recording subclass — we want the
    # production _set_camilla path with its gate.
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord.note_voice_session(True)
    await coord.set_listening_level(46)
    # Camilla untouched — Ducker still owns it.
    assert cam.set_calls == []
    # listening_level updated and persisted so Ducker.restore can
    # read the right target on session end.
    assert coord.get_listening_level() == 46
    record = persistence.load()
    assert record is not None and record.listening_level == 46

    # Out of voice session, the same call writes camilla normally.
    coord.note_voice_session(False)
    await coord.set_listening_level(50)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(50))


# ---- cross-daemon duck-active probe -------------------------------------
#
# jasper-control builds a fresh VolumeCoordinator per HTTP request, so the
# `_voice_session_active` flag above is always False even when jasper-voice
# has a session in flight. Those coordinators receive a `duck_active_probe`
# callable that asks jasper-voice over UDS whether the Ducker is currently
# engaged. The probe is the authoritative signal — no inference. Probe-true
# defers (same effect as the flag); probe-false writes camilla; probe-None
# (UDS unreachable, voice wedged, malformed response) fails open so the dial
# never silently stops working.
#
# Replaces the prior dB-comparison heuristic that conflated "user spinning
# fast" with "duck active" (a fast 3-detent dial spin = +6 dB request,
# above the old 5 dB threshold, used to defer spuriously and poison
# listening_level — see docs/HANDOFF-volume.md "Cross-daemon defer signal").


async def test_set_camilla_deferred_when_probe_returns_true(tmp_path):
    """Per-request coordinator with a probe that signals duck-active.
    Camilla write is deferred, listening_level still persists so
    Ducker.restore lands at user intent on session end. Regression
    for the original PR #299 bug: dial twist during TTS would
    clobber the Ducker and music became audibly louder mid-utterance."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-40.0)  # already ducked
    backend = _FakeBackend(active={})

    async def probe():
        return True

    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None, duck_active_probe=probe,
    )
    await coord.set_listening_level(70)
    # Camilla NOT touched — defer fired.
    assert cam.set_calls == []
    # listening_level still persisted so Ducker.restore lands at -15.
    assert coord.get_listening_level() == 70
    record = persistence.load()
    assert record is not None and record.listening_level == 70


async def test_set_camilla_writes_when_probe_returns_false(tmp_path):
    """Probe says no duck → write camilla. No more spurious defers
    on legitimate user input."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-40.0)
    backend = _FakeBackend(active={})

    async def probe():
        return False

    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None, duck_active_probe=probe,
    )
    await coord.set_listening_level(70)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(70))


async def test_set_camilla_writes_when_probe_returns_none(tmp_path):
    """Probe returning None (UDS unreachable, voice daemon wedged,
    response malformed) → write camilla anyway. Fail-open is the
    correct default for a home appliance: better to occasionally
    un-duck music for a moment than to leave the user with a dead
    dial because of an inter-daemon problem."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-40.0)
    backend = _FakeBackend(active={})

    async def probe():
        return None

    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None, duck_active_probe=probe,
    )
    await coord.set_listening_level(70)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(70))


async def test_set_camilla_writes_when_probe_raises(tmp_path, caplog):
    """Probe is *expected* to convert errors to None internally, but
    if it raises anyway the coordinator must still fail-open (write
    camilla) and warn so the bug surfaces in logs without breaking
    volume control."""
    import logging
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})

    async def probe():
        raise RuntimeError("simulated probe bug")

    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None, duck_active_probe=probe,
    )
    caplog.set_level(logging.WARNING, logger="jasper.volume_coordinator")
    await coord.set_listening_level(60)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(60))
    assert any(
        "duck_active_probe raised" in r.message for r in caplog.records
    )


async def test_set_camilla_writes_when_no_probe_configured(tmp_path):
    """jasper-voice's own coordinator never sets a probe — it has
    the in-process `_voice_session_active` flag instead. With both
    signals off, camilla writes proceed. (When the flag goes on,
    the earlier test `test_set_camilla_deferred_during_voice_session`
    covers the defer.)"""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,  # no duck_active_probe
    )
    await coord.set_listening_level(70)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(70))


async def test_set_camilla_fast_spin_regression(tmp_path):
    """Regression for the dial-fast-spin desync bug observed 2026-05-25.

    Reproduction: per-request coordinator, no active duck. User spins
    the dial fast enough that one POST batches 3 detents (+12% / +6 dB).
    Under the old dB-comparison heuristic, this triggered an
    `inferred_duck` defer because target_db - current_db = +6 > 5,
    even though there was no actual session. listening_level was
    persisted while main_volume stayed put — every subsequent dial
    twist read the inflated listening_level and kept deferring
    (cascade), trapping the user with a knob that did nothing until
    they spun all the way down.

    After the fix: probe returns False (no session) → camilla gets
    written. No defer. No cascade. The dial spin lands."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    # Match the production log: camilla at -18 dB (64%), in sync with
    # listening_level=64%.
    cam = _FakeCamilla(db=-18.0)
    backend = _FakeBackend(active={})

    async def probe():
        return False  # No session active — the actual bug scenario

    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None, duck_active_probe=probe,
    )
    # Seed in-memory level to 64% so the +12% adjust lands at 76%
    # (matching the production log's first deferred event).
    coord._level = 64
    persistence.save_listening_level(64, mark_user_change=True)

    # Fast spin: 3 detents batched -> +12% adjust -> 76%.
    await coord.adjust_listening_level(12)
    # Old behavior: cam.set_calls would be empty (defer fired) and
    # listening_level would be 76 while main_volume_db stayed -18.
    # New behavior: camilla written to the curve target; level in sync.
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(76)), (
        "fast spin must land on camilla when no duck is active"
    )
    assert coord.get_listening_level() == 76

    # And no cascade: subsequent small twists keep tracking 1:1.
    cam.db = percent_to_db(76)  # simulate camilla acknowledging the last write
    await coord.adjust_listening_level(4)  # one detent up → 80%
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(80))
    assert coord.get_listening_level() == 80


async def test_set_camilla_defer_logs_session_signaled_event(tmp_path, caplog):
    """The probe-driven defer emits `reason=session_signaled` so it's
    distinguishable in logs from the in-process flag path and from any
    future defer reasons."""
    import logging
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-40.0)
    backend = _FakeBackend(active={})

    async def probe():
        return True

    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None, duck_active_probe=probe,
    )
    caplog.set_level(logging.INFO, logger="jasper.volume_coordinator")
    await coord.set_listening_level(70)
    deferral_events = [
        r for r in caplog.records
        if "event=volume.deferred" in r.message
        and "reason=session_signaled" in r.message
    ]
    assert len(deferral_events) == 1
    msg = deferral_events[0].message
    assert "level=70%" in msg
    assert f"target_db={percent_to_db(70):.1f}" in msg


# ---- maybe_reconcile_camilla (self-healing backstop, "Option E") -------
#
# The reconciler is jasper-voice's belt-to-Option-A's-suspenders. It
# runs at 1 Hz inside VolumeObserver._tick and converges main_volume_db
# back toward percent_to_db(listening_level) when they've drifted —
# catching any future writer or transient that creates a desync.
# Gated heavily so it never fights a Ducker (in-session) or a CueDuck
# (deep drift) or a push-mode source (camilla pinned at 0 dB).


async def test_reconcile_noop_within_dead_band(tmp_path):
    """Drift smaller than RECONCILE_DRIFT_DB is camilla's normal jitter
    or sub-percentile rounding — no-op."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=percent_to_db(70) - 0.3)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70, mark_user_change=True)
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls == []


async def test_reconcile_converges_when_camilla_below_expected(tmp_path):
    """The Option-A bug shape: listening_level=76%
    but main_volume stuck at -18 dB (a 6 dB drift in the
    reconciler's catch band). Reconciler writes -12 dB."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-18.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 76
    persistence.save_listening_level(76, mark_user_change=True)
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(76))


async def test_reconcile_converges_when_camilla_above_expected(tmp_path):
    """Symmetric direction: main_volume is louder than listening_level
    implies (some writer raised it without going through the coordinator).
    Reconciler pulls it down."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-8.0)  # too loud for 70%
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70, mark_user_change=True)
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(70))


async def test_reconcile_corrects_deep_loud_drift(tmp_path):
    """Deep quiet drift can be a cue duck; deep loud drift is unsafe
    and should always be pulled back to the canonical level."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)  # 50 dB louder than expected for 0%
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 0
    persistence.save_listening_level(0, mark_user_change=True)
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True


async def test_reconcile_repairs_zero_percent_mute_drift(tmp_path):
    """At 0%, matching dB is not enough; main_mute must also be true."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-50.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 0
    persistence.save_listening_level(0, mark_user_change=True)
    persistence.save_now(-50.0)

    await coord.maybe_reconcile_camilla()

    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True


async def test_reconcile_preserves_toggle_mute_restore_level(tmp_path):
    """Toggle mute persists the restore level separately from audible 0%.

    The voice daemon's 1 Hz reconciler must treat `pre_mute_level` as the
    active mute intent. Otherwise it sees listening_level=59%, expects
    main_mute=false, and immediately undoes a remote mute button press.
    """
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=percent_to_db(0))
    cam.muted = True
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    persistence.save_listening_level(59, mark_user_change=True)
    persistence.save_now(percent_to_db(0))
    persistence.save_pre_mute_level(59)

    await coord.maybe_reconcile_camilla()

    assert cam.set_calls == []
    assert cam.mute_calls == []


async def test_reconcile_noop_when_drift_looks_like_duck(tmp_path):
    """CueDuck plays proactive cues with `_voice_session_active=False`.
    During a CueDuck, main_volume is JASPER_DUCK_DB below expected
    (default -25 dB, well past RECONCILE_DUCK_SKIP_DB=10). Reconciler
    must skip — un-ducking the cue mid-playback would defeat the cue."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=percent_to_db(70) - 25.0)  # cue-ducked
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70, mark_user_change=True)
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls == []


async def test_reconcile_noop_during_voice_session(tmp_path):
    """Voice session is active → the Ducker owns camilla.
    Reconciler must defer to it absolutely; the flag is the strongest
    gate."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=percent_to_db(70) - 15.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70, mark_user_change=True)
    coord.note_voice_session(True)
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls == []


async def test_reconcile_noop_during_measurement(tmp_path):
    """The 1 Hz resilience writer must not fight correction's ramp lease."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-3.15)
    coord = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=_FakeBackend(active={}),
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70, mark_user_change=True)
    await coord.note_measurement_active(True)

    await coord.maybe_reconcile_camilla()

    assert cam.set_calls == []


async def test_reconcile_in_flight_stops_when_measurement_begins(tmp_path):
    """MEASURE_PAUSE may race a tick already awaiting Camilla readback."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-3.15)
    coord = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=_FakeBackend(active={}),
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70, mark_user_change=True)
    read_started = asyncio.Event()
    release_read = asyncio.Event()

    async def blocked_read():
        read_started.set()
        await release_read.wait()
        return -3.15, False

    coord._read_camilla_volume_and_mute = blocked_read
    reconcile = asyncio.create_task(coord.maybe_reconcile_camilla())
    await read_started.wait()
    await coord.note_measurement_active(True)
    release_read.set()
    await reconcile

    assert cam.set_calls == []


async def test_measurement_pause_waits_for_in_flight_reconcile_write(tmp_path):
    """Pause acknowledges only after an older Camilla write has finished."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-3.15)
    coord = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=_FakeBackend(active={}),
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70, mark_user_change=True)
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    original_set = cam.set_volume_db

    async def blocked_set(db, *, best_effort=False):
        write_started.set()
        await release_write.wait()
        return await original_set(db, best_effort=best_effort)

    cam.set_volume_db = blocked_set
    reconcile = asyncio.create_task(coord.maybe_reconcile_camilla())
    await write_started.wait()
    pause = asyncio.create_task(coord.note_measurement_active(True))
    await asyncio.sleep(0)
    assert not pause.done()

    release_write.set()
    await reconcile
    await pause
    writes_at_acquire = len(cam.set_calls)
    await coord.maybe_reconcile_camilla()

    assert writes_at_acquire == 1
    assert len(cam.set_calls) == writes_at_acquire


async def test_reconcile_noop_when_push_mode_source_active(tmp_path):
    """Spotify Connect is active → camilla pinned at 0 dB by design,
    listening_level lives on the Spotify slider. Reconciler must not
    write camilla here — would compound with the source's own
    attenuator."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 60  # would imply -20 dB if camilla carried it
    persistence.save_listening_level(60, mark_user_change=True)
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls == []


async def test_reconcile_noop_when_camilla_unreachable(tmp_path):
    """Best-effort read returning None (camilla restart blip) → skip
    silently. The next tick retries when camilla recovers."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    cam.unavailable = True
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70, mark_user_change=True)
    # Should not raise.
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls == []


async def test_reconcile_emits_structured_event(tmp_path, caplog):
    """The reconciler's write logs `event=volume.reconciled` with
    enough context (level, current_db, expected_db, drift_db) that
    a debugger can answer 'who caused the drift' from journalctl
    alone."""
    import logging
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-18.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 76
    persistence.save_listening_level(76, mark_user_change=True)
    caplog.set_level(logging.INFO, logger="jasper.volume_coordinator")
    await coord.maybe_reconcile_camilla()
    events = [
        r for r in caplog.records
        if "event=volume.reconciled" in r.message
    ]
    assert len(events) == 1
    msg = events[0].message
    assert "level=76%" in msg
    assert "current_db=-18.00" in msg
    assert f"expected_db={percent_to_db(76):.2f}" in msg
    assert f"drift_db={percent_to_db(76) - (-18.0):+.2f}" in msg


async def test_reconcile_no_loop_when_already_converged(tmp_path):
    """After one reconcile fires and camilla is at expected, the next
    tick must be a no-op (no write loop). Idempotence regression."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-18.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 76
    persistence.save_listening_level(76, mark_user_change=True)
    await coord.maybe_reconcile_camilla()
    first_write_count = len(cam.set_calls)
    assert first_write_count == 1
    # Subsequent tick — camilla is now at -12 (set by reconciler);
    # no further drift to correct.
    await coord.maybe_reconcile_camilla()
    assert len(cam.set_calls) == first_write_count


async def test_get_camilla_target_db_idle_returns_listening_level_db(tmp_path):
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    await coord.set_listening_level(70)
    target = await coord.get_camilla_target_db()
    assert target == pytest.approx(percent_to_db(70))


async def test_get_camilla_target_db_airplay_returns_listening_level_db(tmp_path):
    """AirPlay is camilla-master, so ducker restore targets the JTS
    listening level."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"aplactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    await coord.set_listening_level(40)
    target = await coord.get_camilla_target_db()
    assert target == pytest.approx(percent_to_db(40))


async def test_get_camilla_target_db_push_mode_returns_zero(tmp_path):
    """In push mode (Spotify, BT) camilla is pinned at 0 dB; the
    source's own slider carries listening_level."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    target = await coord.get_camilla_target_db()
    assert target == 0.0


async def test_get_camilla_target_db_push_mode_zero_returns_mute_floor(tmp_path):
    """Ducker.restore must not unmask a push-mode 0% content mute."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-50.0)
    backend = _FakeBackend(active={"spotactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 0
    persistence.save_listening_level(0)

    target = await coord.get_camilla_target_db()

    assert target == pytest.approx(percent_to_db(0))


async def test_get_camilla_target_db_refreshes_from_disk(tmp_path):
    """Cross-process staleness guard for the duck-restore path. The
    control daemon (dial / HTTP) writes listening_level to disk on
    every twist; voice-daemon's in-memory `_level` only auto-refreshes
    on its own set/adjust/mute/transition calls. Without a refresh
    here, Ducker.restore() at the end of a wake reads the stale
    `_level` and writes camilla to the wrong dB — observed as a 56 dB
    jump (camilla -56 dB → 0 dB) at duck-off after a dial-spin to
    100% landed between voice-daemon operations.

    Mirrors test_transition_refreshes_from_disk but for the
    get_camilla_target_db code path that the Ducker actually uses."""
    coord, _, persistence = _coord(tmp_path, active={"aplactive": True})
    # voice-daemon coordinator's in-memory state: 38%.
    coord._level = 38
    persistence.save_listening_level(38)
    # Control daemon (different process) writes 80% to disk.
    persistence.save_listening_level(80)
    # Ducker.restore() reads this target after a failed turn. With the
    # refresh, we use 80%; without it we'd use the stale 38%, and once
    # the dial-truth eventually catches up to the
    # coordinator (e.g. via an unrelated source-state transition), the
    # NEXT duck-restore would jump camilla loudly to satisfy 100%.
    target = await coord.get_camilla_target_db()
    assert target == pytest.approx(percent_to_db(80))
    assert coord.get_listening_level() == 80


async def test_transition_refreshes_from_disk(tmp_path):
    """Cross-process staleness guard. The control daemon (dial / HTTP)
    writes listening_level to disk on every twist. voice_daemon's
    in-memory `_level` only auto-refreshes on its own set/adjust/mute
    calls, not on observer-triggered transitions."""
    coord, _, persistence = _coord(tmp_path, active={"aplactive": True})
    # voice-daemon coordinator's in-memory state: 50%.
    coord._level = 50
    persistence.save_listening_level(50)
    # Control daemon (different process) writes 80% to disk.
    persistence.save_listening_level(80)
    # Now an active-source transition fires. With the refresh, we
    # use 80%; without it we'd use the stale 50%.
    coord._backend = _FakeBackend(active={"spotactive": True})
    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)
    # Spotify was pushed the disk-truth 80%, not the in-memory 50%.
    assert coord.spotify_writes == [80]
    assert coord.get_listening_level() == 80


# ---------- camilla restart-blip survival ---------------------------------


async def test_volume_coordinator_proceeds_when_camilla_unreachable(tmp_path):
    """Regression: a dial twist arriving during a 2 s camilla restart
    blip (Restart=always brings camilla back) must not throw.
    listening_level is updated in memory and on disk; the camilla
    write itself is skipped silently and the next set_listening_level
    re-applies once camilla is back.

    The user's intent (target percent) is preserved end-to-end so the
    next operation lands at the right level."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})  # idle
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )

    # Camilla goes down (mid-restart).
    cam.unavailable = True

    # Dial twist lands during the blip. Must not raise.
    new_level = await coord.set_listening_level(70)
    assert new_level == 70

    # In-memory level updated; persistence reflects the user's intent.
    assert coord.get_listening_level() == 70
    assert persistence.load().listening_level == 70

    # Camilla itself was never written — best_effort=True silently
    # dropped the write because the fake was unavailable.
    assert cam.set_calls == []

    # Camilla recovers. The next set lands.
    cam.unavailable = False
    await coord.set_listening_level(40)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(40))
    assert coord.get_listening_level() == 40


# ---------- USB sink (camilla-master, host-slider observed inbound) --------


async def test_set_volume_usbsink_active_routes_to_camilla(tmp_path):
    """USB sink behaves like AirPlay for outbound: dial/voice writes
    land on CamillaDSP. The gadget mixer is NOT written back to (the
    host's slider is observed-only)."""
    coord, cam, _ = _coord(
        tmp_path, active={"usbsinkactive": True}, db=0.0,
    )
    await coord.set_listening_level(60)
    assert coord.camilla_writes == [60]
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(60))
    # No spotify/BT path triggered.
    assert coord.spotify_writes == []
    assert coord.bt_writes == []


async def test_observe_usbsink_updates_listening_level_when_active(tmp_path):
    """Host slider moves while USB is the active source — listening
    level follows and CamillaDSP, the USB carrier, is updated."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"usbsinkactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 80
    persistence.save_listening_level(80)

    # Mac slider drops to 45%. Volume bridge POSTs that through.
    await coord.observe_source_volume(Source.USBSINK, 45)
    assert coord.get_listening_level() == 45
    assert persistence.load().listening_level == 45
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(45))
    assert cam.mute_calls[-1] is False


async def test_observe_usbsink_updates_when_selected_even_if_probe_idle(
    tmp_path,
):
    """Mux selection is the speaker gate; USB host volume should follow
    it even when the raw usbsink RMS activity probe is currently quiet."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-50.0)
    backend = _FakeBackend(active={}, selected="usbsink")
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 0
    persistence.save_listening_level(0)
    persistence.save_now(-50.0)
    cam.muted = True

    await coord.observe_source_volume(Source.USBSINK, 50)

    assert coord.get_listening_level() == 50
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 50
    assert record.main_volume_db == pytest.approx(round(percent_to_db(50), 2))
    assert cam.events[-2:] == [
        ("volume", pytest.approx(percent_to_db(50))),
        ("mute", False),
    ]


async def test_observe_usbsink_unmute_restores_camilla_carrier(tmp_path):
    """Wispr/macOS unmute restores the host slider value; because USB is
    camilla-master, that observation must also raise Camilla back."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-50.0)
    backend = _FakeBackend(active={"usbsinkactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 0
    persistence.save_listening_level(0)
    persistence.save_now(-50.0)
    cam.muted = True

    await coord.observe_source_volume(Source.USBSINK, 75)

    assert coord.get_listening_level() == 75
    record = persistence.load()
    assert record.listening_level == 75
    assert record.main_volume_db == pytest.approx(round(percent_to_db(75), 2))
    assert cam.events[-2:] == [
        ("volume", pytest.approx(percent_to_db(75))),
        ("mute", False),
    ]


async def test_observe_usbsink_unmute_defers_during_duck(tmp_path):
    """A USB host unmute records intent but does not clobber active ducking."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-50.0)
    backend = _FakeBackend(active={"usbsinkactive": True})

    async def probe():
        return True

    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None, duck_active_probe=probe,
    )
    coord._level = 0
    persistence.save_listening_level(0)
    persistence.save_now(-50.0)
    cam.muted = True

    await coord.observe_source_volume(Source.USBSINK, 75)

    assert coord.get_listening_level() == 75
    record = persistence.load()
    assert record.listening_level == 75
    assert record.main_volume_db == pytest.approx(percent_to_db(0))
    assert cam.set_calls == []
    assert cam.mute_calls[-1] is False


async def test_observe_usbsink_same_level_repairs_camilla_drift(tmp_path):
    """If JTS already remembers the host level, a fresh USB observation
    still repairs Camilla drift instead of returning early."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=-20.0)
    backend = _FakeBackend(active={"usbsinkactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 0
    persistence.save_listening_level(0)
    persistence.save_now(-20.0)

    await coord.observe_source_volume(Source.USBSINK, 0)

    assert coord.get_listening_level() == 0
    assert persistence.load().main_volume_db == pytest.approx(percent_to_db(0))
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True


async def test_observe_usbsink_same_level_repairs_mute_drift(tmp_path):
    """Even if dB is already at the 0% floor, unmuted Camilla is not
    converged: 0% must assert main_mute."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=percent_to_db(0))
    backend = _FakeBackend(active={"usbsinkactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 0
    persistence.save_listening_level(0)
    persistence.save_now(percent_to_db(0))

    await coord.observe_source_volume(Source.USBSINK, 0)

    assert coord.get_listening_level() == 0
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True


async def test_observe_usbsink_when_inactive_is_ignored(tmp_path):
    """Host slider chatter while AirPlay is playing should not steal
    JTS volume from AirPlay."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"aplactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70)

    await coord.observe_source_volume(Source.USBSINK, 20)
    assert coord.get_listening_level() == 70


async def test_observe_usbsink_clamps_out_of_range(tmp_path):
    """Defensive: percent outside [0, 100] gets clamped before storage."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"usbsinkactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 50
    persistence.save_listening_level(50)

    await coord.observe_source_volume(Source.USBSINK, 150)
    assert coord.get_listening_level() == 100

    await coord.observe_source_volume(Source.USBSINK, -20)
    assert coord.get_listening_level() == 0


async def test_usbsink_priority_below_airplay(tmp_path):
    """When AirPlay and USB both report active (transition window),
    AirPlay wins. This matches mux's first-source-defined-wins behavior
    and matches user expectations that a phone-controlled AirPlay
    session shouldn't be silently overridden by a Mac plugged into the
    USB port."""
    coord, _, _ = _coord(
        tmp_path,
        active={"aplactive": True, "usbsinkactive": True},
    )
    await coord.set_listening_level(55)
    # AirPlay path fired (which also writes camilla); USB-specific
    # branch did not.
    assert coord.airplay_writes == [55]


async def test_usbsink_is_camilla_master(tmp_path):
    """The _camilla_carries_level predicate determines whether camilla
    keeps the user's perceived level or is pinned at 0 dB. USB sink
    must be camilla-master to track listening_level through
    speaker output."""
    coord, _, _ = _coord(tmp_path, active={"usbsinkactive": True})
    assert await coord._camilla_carries_level(Source.USBSINK) is True
    # Inverse check — spotify is still push-mode.
    assert await coord._camilla_carries_level(Source.SPOTIFY) is False


# ---------- bluealsa transport-path probe goes through shared backoff -------
#
# _bluez_alsa_active_transport_path runs in jasper-control on every BT
# volume set from the dial/web. It must reuse jasper.bluealsa_probe so a
# D-Bus permission denial backs off process-wide instead of hammering the
# system bus once per volume set. These tests fail if the helper reverts
# to its own raw `bluealsa-cli list-pcms` subprocess.


async def test_bluez_transport_path_parses_pcm_line(monkeypatch):
    line = (
        b"/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source PCM ...\n"
    )

    class _Proc:
        returncode = 0

        async def communicate(self):
            return line, b""

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await vc_mod._bluez_alsa_active_transport_path() == (
        "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source"
    )


async def test_bluez_transport_path_returns_none_when_no_transport(monkeypatch):
    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await vc_mod._bluez_alsa_active_transport_path() is None


async def test_bluez_transport_path_suppresses_after_cli_failure(monkeypatch):
    """A D-Bus rejection (rc!=0) must trip the shared backoff so the
    second probe is short-circuited and does NOT spawn a subprocess.
    Only true if the helper routes through bluealsa_probe.list_pcms."""
    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"permission denied"

    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await vc_mod._bluez_alsa_active_transport_path() is None
    assert await vc_mod._bluez_alsa_active_transport_path() is None
    assert calls["n"] == 1


async def test_bluez_transport_path_shares_backoff_with_other_probes(monkeypatch):
    """The backoff is process-wide: a failure recorded by any
    bluealsa_probe consumer suppresses this helper's next probe without
    spawning. Pins the 'shared module', not a per-caller, contract."""
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("should not spawn while suppressed")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    # Pre-trip the shared backoff as if another consumer just failed.
    bluealsa_probe.note_probe_failure("rc=1", vc_mod.logger)

    assert await vc_mod._bluez_alsa_active_transport_path() is None
    assert calls["n"] == 0
