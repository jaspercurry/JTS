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
    actually invoking subprocess busctl / HTTP. Replaces the four
    `_set_*` methods with capture lists."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.airplay_writes: list[int] = []
        self.spotify_writes: list[int] = []
        self.bt_writes: list[int] = []
        self.camilla_writes: list[int] = []

    async def _set_airplay(self, level: int) -> None:
        self.airplay_writes.append(level)
        self._stamp_outbound(Source.AIRPLAY, level)

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
    assert coord.airplay_writes == []
    # camilla received -15 dB (70% on -50..0 scale)
    assert cam.set_calls and abs(cam.set_calls[-1] - (-15.0)) < 0.01


async def test_set_volume_airplay_active_routes_to_airplay(tmp_path):
    coord, cam, _ = _coord(
        tmp_path, active={"aplactive": True}, db=-25.0,
    )
    await coord.set_listening_level(50)
    assert coord.airplay_writes == [50]
    assert coord.camilla_writes == []
    # Camilla NOT touched in source-active dispatch — that's the
    # transition handler's job, gated on voice-session state to
    # avoid fighting the ducker's additive math.
    assert cam.set_calls == []


async def test_set_volume_spotify_active_routes_to_spotify(tmp_path):
    coord, cam, _ = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0,
    )
    await coord.set_listening_level(40)
    assert coord.spotify_writes == [40]
    assert coord.airplay_writes == []
    assert cam.set_calls == []


async def test_set_volume_bluetooth_active_routes_to_bt(tmp_path):
    coord, cam, _ = _coord(
        tmp_path, active={"btactive": True}, db=-25.0,
    )
    await coord.set_listening_level(60)
    assert coord.bt_writes == [60]
    assert coord.airplay_writes == []
    assert cam.set_calls == []


async def test_idle_to_source_transition_pins_camilla(tmp_path):
    """idle→active transition pins camilla to 0 dB and pushes
    listening_level to the new source."""
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True}, db=-25.0)
    await coord.set_listening_level(50)
    # No camilla writes from set in source-active mode.
    assert cam.set_calls == []
    # Now simulate a transition from idle:
    await coord.apply_active_source_transition(Source.IDLE, Source.AIRPLAY)
    # Camilla should be pinned to 0 dB
    assert cam.set_calls == [0.0]
    # And listening_level pushed to the new source again
    assert coord.airplay_writes[-1] == 50


async def test_source_to_idle_transition_restores_camilla(tmp_path):
    """active→idle transition hands camilla back to listening_level percent."""
    coord, cam, _ = _coord(tmp_path, active={}, db=0.0)
    await coord.set_listening_level(60)
    # idle path: camilla wrote -20 dB (60% on -50..0)
    assert cam.set_calls and abs(cam.set_calls[-1] - (-20.0)) < 0.01
    # Simulate transition from airplay back to idle
    await coord.apply_active_source_transition(Source.AIRPLAY, Source.IDLE)
    # Camilla should now be at -20 dB again (60%)
    assert abs(cam.set_calls[-1] - (-20.0)) < 0.01


async def test_transition_suppressed_during_voice_session(tmp_path):
    """note_voice_session(True) gates apply_active_source_transition
    so the ducker's additive math isn't corrupted by absolute writes."""
    coord, cam, _ = _coord(tmp_path, active={}, db=0.0)
    coord.note_voice_session(True)
    initial_calls = list(cam.set_calls)
    await coord.apply_active_source_transition(Source.IDLE, Source.AIRPLAY)
    # No new camilla writes
    assert cam.set_calls == initial_calls
    coord.note_voice_session(False)
    await coord.apply_active_source_transition(Source.IDLE, Source.AIRPLAY)
    assert 0.0 in cam.set_calls


async def test_airplay_priority_over_spotify_over_bt(tmp_path):
    """When multiple sources report active (transition window),
    coordinator picks airplay > spotify > bt."""
    coord, _, _ = _coord(
        tmp_path,
        active={"aplactive": True, "spotactive": True, "btactive": True},
    )
    await coord.set_listening_level(50)
    assert coord.airplay_writes == [50]
    assert coord.spotify_writes == []
    assert coord.bt_writes == []


async def test_adjust_volume(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(50)
    await coord.adjust_listening_level(15)
    assert coord.airplay_writes == [50, 65]


async def test_adjust_clamps_to_0_and_100(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(95)
    await coord.adjust_listening_level(20)
    assert coord.airplay_writes[-1] == 100
    await coord.adjust_listening_level(-200)
    assert coord.airplay_writes[-1] == 0


async def test_mute_then_unmute(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(70)
    saved = await coord.mute()
    assert saved == 70
    assert coord.airplay_writes[-1] == 0  # silence
    assert coord.is_muted()
    restored = await coord.unmute()
    assert restored == 70
    assert coord.airplay_writes[-1] == 70
    assert not coord.is_muted()


async def test_unmute_without_prior_mute_uses_fallback(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"aplactive": True})
    restored = await coord.unmute(fallback_level=50)
    assert restored == 50


# ---------- echo prevention ------------------------------------------------


async def test_observe_within_echo_window_ignored(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(60)
    # Immediate observation echoing back the same value → ignored.
    db = listening_level_to_airplay_db(60)
    await coord.observe_source_volume(Source.AIRPLAY, db)
    # listening_level unchanged; no extra airplay writes.
    assert coord.get_listening_level() == 60
    assert coord.airplay_writes == [60]


async def test_observe_outside_echo_window_updates_level(tmp_path, monkeypatch):
    coord, _, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(60)
    # Fast-forward time past the echo window without sleeping.
    fake_now = time.monotonic() + ECHO_WINDOW_SEC + 1.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    db = listening_level_to_airplay_db(40)
    await coord.observe_source_volume(Source.AIRPLAY, db)
    assert coord.get_listening_level() == 40
    # Observation should NOT trigger an outbound dispatch (no echo).
    assert coord.airplay_writes == [60]


async def test_observe_different_value_within_window_still_updates(tmp_path):
    """If we wrote 60% and observe 30% within the window, the value
    differs enough to be a real user-side change — don't ignore."""
    coord, _, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(60)
    db = listening_level_to_airplay_db(30)
    await coord.observe_source_volume(Source.AIRPLAY, db)
    assert coord.get_listening_level() == 30


async def test_observe_persists_listening_level(tmp_path, monkeypatch):
    coord, _, persistence = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(60)
    fake_now = time.monotonic() + ECHO_WINDOW_SEC + 1.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    db = listening_level_to_airplay_db(40)
    await coord.observe_source_volume(Source.AIRPLAY, db)
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


# ---------- DACP-aware AirPlay dispatch -------------------------------------
# When AirPlay 2 senders (iOS 17+) connect, shairport's DACP back-channel
# is typically unavailable. SetAirplayVolume succeeds silently in that
# case, so the bug we're guarding against is "log says we set it; nothing
# actually happened." _set_airplay must skip the dispatch and log the
# truth. We don't fall back to attenuating Camilla — that'd double-
# attenuate against the iPhone slider (already in the chain) and the
# cap-lift transient when shairport's high_volume_threshold expires
# would pop the volume up without warning.


async def test_set_airplay_skips_dispatch_when_dacp_unavailable(tmp_path, monkeypatch):
    """Real _set_airplay path: DACP false → no busctl call, level not
    stamped (so a subsequent observer reading isn't mistaken for our
    own write)."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    import jasper.volume_coordinator as vc_mod
    busctl_calls: list = []

    async def fake_dacp() -> bool:
        return False

    async def fake_call(*args, **kwargs):
        busctl_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(vc_mod, "_airplay_dacp_available", fake_dacp)
    monkeypatch.setattr(vc_mod, "_busctl_call_method", fake_call)

    await coord._set_airplay(75)

    assert busctl_calls == [], "no busctl call should fire when DACP is false"
    # Outbound stamp should not have been recorded — otherwise the
    # observer's echo guard would suppress the next genuine iPhone-
    # side change.
    assert Source.AIRPLAY not in coord._last_outbound


async def test_set_airplay_dispatches_when_dacp_available(tmp_path, monkeypatch):
    """Real _set_airplay path: DACP true → busctl SetAirplayVolume
    fires with the right interface + signature. Stamps outbound so
    the echo guard catches the next observer reading."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})
    coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
        spotify_router=None,
    )
    import jasper.volume_coordinator as vc_mod
    busctl_calls: list = []

    async def fake_dacp() -> bool:
        return True

    async def fake_call(bus_name, object_path, interface, method, signature, value, **kwargs):
        busctl_calls.append({
            "bus_name": bus_name, "object_path": object_path,
            "interface": interface, "method": method,
            "signature": signature, "value": value,
        })
        return True

    monkeypatch.setattr(vc_mod, "_airplay_dacp_available", fake_dacp)
    monkeypatch.setattr(vc_mod, "_busctl_call_method", fake_call)

    await coord._set_airplay(75)

    assert len(busctl_calls) == 1
    call = busctl_calls[0]
    assert call["bus_name"] == "org.gnome.ShairportSync"
    assert call["interface"] == "org.gnome.ShairportSync.RemoteControl"
    assert call["method"] == "SetAirplayVolume"
    assert call["signature"] == "d"
    # 75% should map to roughly -7.5 dB on the airplay -30..0 scale.
    db = float(call["value"])
    assert -10 < db < -5
    assert Source.AIRPLAY in coord._last_outbound
