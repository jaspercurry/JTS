"""Unit tests for jasper.volume_coordinator.

Covers:
- mapping helpers round-trip
- set/adjust/mute/unmute on each source
- camilla pinned at 0 dB during source-active operation
- echo prevention: own-write within window is ignored on observe
- observe out-of-window changes update listening_level + persist
- initialize() applies regression and DOES NOT bump last_used_at
- subsequent set_listening_level DOES bump last_used_at
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import pytest

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
from jasper.volume_persistence import VolumePersistence


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
        self.set_calls: list[float] = []
        self.get_calls: int = 0

    async def get_volume_db(self) -> float:
        self.get_calls += 1
        return self._db

    async def set_volume_db(self, db: float) -> None:
        self._db = db
        self.set_calls.append(db)


class _FakeBackend:
    def __init__(self, active: dict[str, bool] | None = None) -> None:
        self._active = active or {}

    async def active_renderers(self) -> dict[str, bool]:
        return dict(self._active)


class _RecordingCoordinator(VolumeCoordinator):
    """Subclass that records source-side dispatch calls without
    actually invoking subprocess busctl / HTTP. Replaces `_set_*`
    methods with capture lists. Mirrors production semantics:
    AIRPLAY is always camilla-as-master, so _set_airplay falls
    through to _set_camilla; only SPOTIFY and BLUETOOTH are
    push-mode."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.spotify_writes: list[int] = []
        self.bt_writes: list[int] = []
        self.camilla_writes: list[int] = []

    async def _set_spotify(self, level: int) -> None:
        self.spotify_writes.append(level)
        self._stamp_outbound(Source.SPOTIFY, level)

    async def _set_bluetooth(self, level: int) -> None:
        self.bt_writes.append(level)
        self._stamp_outbound(Source.BLUETOOTH, level)

    async def _set_camilla(self, level: int) -> None:
        from jasper.volume_persistence import percent_to_db
        db = percent_to_db(level)
        await self._camilla.set_volume_db(db)
        self._persistence.save_now(db)
        self.camilla_writes.append(level)


def _coord(tmp_path, *, active: dict[str, bool] | None = None, db: float = 0.0):
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=db)
    backend = _FakeBackend(active=active)
    coord = _RecordingCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend,
        spotify_router=None,  # tests bypass _set_spotify dispatch
    )
    return coord, cam, persistence


# ---------- outbound dispatch ----------------------------------------------


async def test_set_volume_idle_writes_camilla(tmp_path):
    coord, cam, _ = _coord(tmp_path, active={})
    await coord.set_listening_level(70)
    assert coord.camilla_writes == [70]
    # camilla received -15 dB (70% on -50..0 scale)
    assert cam.set_calls and abs(cam.set_calls[-1] - (-15.0)) < 0.01


async def test_set_volume_airplay_active_routes_to_camilla(tmp_path):
    """AirPlay is camilla-as-master in this codebase. The dial controls
    audio via camilla.main_volume rather than pushing to the AirPlay
    sender's slider (which Apple silently no-ops on AirPlay 2)."""
    coord, cam, _ = _coord(
        tmp_path, active={"aplactive": True}, db=0.0,
    )
    await coord.set_listening_level(50)
    # listening_level → camilla, not the AirPlay sender.
    assert coord.camilla_writes == [50]
    assert coord.spotify_writes == []
    # Camilla received -25 dB (50% on -50..0 scale).
    assert cam.set_calls and abs(cam.set_calls[-1] - (-25.0)) < 0.01


async def test_set_volume_spotify_active_routes_to_spotify(tmp_path):
    coord, cam, _ = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0,
    )
    await coord.set_listening_level(40)
    assert coord.spotify_writes == [40]
    assert cam.set_calls == []  # Spotify is push-mode; camilla untouched


async def test_set_volume_bluetooth_active_routes_to_bt(tmp_path):
    coord, cam, _ = _coord(
        tmp_path, active={"btactive": True}, db=-25.0,
    )
    await coord.set_listening_level(60)
    assert coord.bt_writes == [60]
    assert cam.set_calls == []  # BT is push-mode; camilla untouched


async def test_idle_to_push_source_transition_pins_camilla(tmp_path):
    """idle→push-mode-source transition pins camilla to 0 dB and
    pushes listening_level to the new source's slider. Uses SPOTIFY
    because AIRPLAY is camilla-as-master both sides (idle ↔ AP)."""
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
    # idle path: camilla wrote -20 dB (60% on -50..0)
    assert cam.set_calls and abs(cam.set_calls[-1] - (-20.0)) < 0.01
    # Simulate transition from spotify back to idle
    await coord.apply_active_source_transition(Source.SPOTIFY, Source.IDLE)
    # Camilla should now be at -20 dB again (60%)
    assert abs(cam.set_calls[-1] - (-20.0)) < 0.01


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
    # Now the transition fires: idle (camilla-as-master) → spotify
    # (push-mode) pins camilla at 0 dB.
    assert 0.0 in cam.set_calls


async def test_airplay_priority_over_spotify_over_bt(tmp_path):
    """When multiple sources report active (transition window),
    coordinator picks airplay > spotify > bt. AirPlay → camilla
    (camilla-as-master)."""
    coord, _, _ = _coord(
        tmp_path,
        active={"aplactive": True, "spotactive": True, "btactive": True},
    )
    await coord.set_listening_level(50)
    # AirPlay won the priority chain → camilla path fired.
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
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(70)
    saved = await coord.mute()
    assert saved == 70
    assert coord.spotify_writes[-1] == 0  # silence
    assert coord.is_muted()
    restored = await coord.unmute()
    assert restored == 70
    assert coord.spotify_writes[-1] == 70
    assert not coord.is_muted()


async def test_unmute_without_prior_mute_uses_fallback(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    restored = await coord.unmute(fallback_level=50)
    assert restored == 50


# ---------- echo prevention ------------------------------------------------


# Echo-prevention tests use SPOTIFY because AirPlay is unconditionally
# camilla-as-master in this codebase (observer always skips it). Spotify
# is the canonical push-mode source where echo prevention matters.


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


async def test_observe_different_value_within_window_still_updates(tmp_path):
    """If we wrote 60% and observe 30% within the window, the value
    differs enough to be a real user-side change — don't ignore."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    await coord.observe_source_volume(Source.SPOTIFY, 30)
    assert coord.get_listening_level() == 30


async def test_observe_persists_listening_level(tmp_path, monkeypatch):
    coord, _, persistence = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    fake_now = time.monotonic() + ECHO_WINDOW_SEC + 1.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    await coord.observe_source_volume(Source.SPOTIFY, 40)
    rec = persistence.load()
    assert rec is not None
    assert rec.listening_level == 40


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


# ---------- AirPlay dispatch is always camilla-as-master ------------------
# Empirically, Apple's AirPlay 2 receivers (iOS 17+ and macOS Sequoia)
# accept shairport's SetAirplayVolume DBus call but silently no-op the
# sender slider. We always use camilla — the dial works regardless.


async def test_set_airplay_uses_camilla_unconditionally(tmp_path):
    """Real _set_airplay path: always falls through to _set_camilla,
    no busctl SetAirplayVolume call ever fires, no AIRPLAY outbound
    stamp (we didn't write to AirPlay's slider)."""
    from jasper.volume_persistence import percent_to_db
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )

    await coord._set_airplay(75)

    # Camilla received percent_to_db(75) — the always-camilla path.
    assert cam.set_calls and abs(cam.set_calls[-1] - percent_to_db(75)) < 0.01
    # No AIRPLAY outbound stamp — we didn't write to AirPlay's slider.
    assert Source.AIRPLAY not in coord._last_outbound


async def test_observe_airplay_always_skipped(tmp_path):
    """observe_source_volume(AIRPLAY) is unconditionally a no-op:
    the sender's slider isn't the user's master volume in this model."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"aplactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    coord._level = 70
    persistence.save_listening_level(70)

    # Even with a strongly-different reading, observer must skip.
    db = listening_level_to_airplay_db(30)
    await coord.observe_source_volume(Source.AIRPLAY, db)

    assert coord.get_listening_level() == 70
    rec = persistence.load()
    assert rec is not None and rec.listening_level == 70


# ---------- transitions across camilla-as-master / push-mode boundary -----


async def test_transition_airplay_to_spotify_clears_camilla(tmp_path):
    """Dial drove camilla as master while AirPlay was active; user
    starts Spotify Connect; camilla must reset to 0 dB and Spotify's
    slider takes over carrying the level."""
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True})
    # User dials level to 60% during AirPlay. Camilla gets the
    # attenuation since AirPlay is camilla-as-master.
    await coord.set_listening_level(60)
    assert coord.camilla_writes == [60]
    # Now the user switches to Spotify Connect. Active flips to spot.
    coord._backend = _FakeBackend(active={"spotactive": True})
    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)
    # Camilla reset to 0 dB — no residual attenuation to double-stack
    # against Spotify's own slider.
    assert 0.0 in cam.set_calls
    assert cam.set_calls[-1] == 0.0
    # Spotify got pushed the listening_level so its slider carries the
    # user's intent.
    assert coord.spotify_writes == [60]


async def test_transition_spotify_to_airplay_hands_camilla_back(tmp_path):
    """Push source → camilla-as-master: camilla must take over carrying
    listening_level so the dial keeps doing real work."""
    from jasper.volume_persistence import percent_to_db
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(50)
    assert coord.spotify_writes == [50]
    # Switch to AirPlay. Camilla must take over (camilla-as-master).
    coord._backend = _FakeBackend(active={"aplactive": True})
    await coord.apply_active_source_transition(Source.SPOTIFY, Source.AIRPLAY)
    # Camilla now at percent_to_db(50) — dial controls loudness via
    # camilla while AirPlay carries the audio.
    assert any(
        abs(c - percent_to_db(50)) < 0.01 for c in cam.set_calls
    ), f"expected camilla → {percent_to_db(50)} dB, got {cam.set_calls}"


async def test_transition_idle_to_airplay_no_camilla_change(tmp_path):
    """Both sides camilla-as-master (idle ↔ AirPlay). Camilla already
    carries listening_level; transition shouldn't write."""
    coord, cam, _ = _coord(tmp_path, active={})
    await coord.set_listening_level(40)
    calls_before = list(cam.set_calls)
    coord._backend = _FakeBackend(active={"aplactive": True})
    await coord.apply_active_source_transition(Source.IDLE, Source.AIRPLAY)
    # No additional camilla writes — both modes are camilla-as-master.
    assert cam.set_calls == calls_before
    assert coord.spotify_writes == []


async def test_transition_spotify_to_bluetooth_pushes_to_new_source(tmp_path):
    """Both sides push-mode. Camilla already at 0 dB; new source's
    slider needs to be pushed listening_level."""
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
    backend = _FakeBackend(active={})  # idle (camilla-as-master)
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
    assert cam.set_calls and abs(cam.set_calls[-1] - (-25.0)) < 0.01


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
    # 70% on the -50..0 scale = -15 dB.
    assert abs(target - (-15.0)) < 0.01


async def test_get_camilla_target_db_airplay_returns_listening_level_db(tmp_path):
    """AirPlay is camilla-as-master (Apple silently no-ops the sender
    slider) so the target is percent_to_db(listening_level), same as
    idle."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"aplactive": True})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    await coord.set_listening_level(40)
    target = await coord.get_camilla_target_db()
    assert abs(target - (-30.0)) < 0.01  # 40% → -30 dB


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
