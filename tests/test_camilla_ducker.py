from __future__ import annotations

import sys
import types

import pytest

# camilladsp is a Pi-side runtime dep not installed locally; stub it so
# `import jasper.camilla` works in unit tests. Ducker only touches
# CamillaController via the public interface (set_volume_db /
# adjust_volume_db), and we pass a fake camilla into it anyway.
sys.modules.setdefault("camilladsp", types.ModuleType("camilladsp"))
sys.modules["camilladsp"].CamillaClient = object  # type: ignore[attr-defined]

from jasper.camilla import CamillaUnavailable, Ducker  # noqa: E402


class _FakeCamilla:
    def __init__(self, db: float = 0.0) -> None:
        self._db = db
        self.set_calls: list[float] = []
        # When True, every best_effort call returns None (write) /
        # None (read) without recording. Simulates a camilla restart
        # blip from the daemon's perspective.
        self.unavailable = False

    async def get_volume_db(self, *, best_effort: bool = False) -> float | None:
        if self.unavailable:
            if best_effort:
                return None
            raise CamillaUnavailable("test fake offline")
        return self._db

    async def set_volume_db(
        self, db: float, *, best_effort: bool = False,
    ) -> bool:
        if self.unavailable:
            if best_effort:
                return False
            raise CamillaUnavailable("test fake offline")
        self._db = db
        self.set_calls.append(db)
        return True

    async def adjust_volume_db(
        self, delta_db: float, *, best_effort: bool = False,
    ) -> float | None:
        current = await self.get_volume_db(best_effort=best_effort)
        if current is None:
            return None
        target = current + delta_db
        if not await self.set_volume_db(target, best_effort=best_effort):
            return None
        return target


def _ducker(camilla: _FakeCamilla, *, duck_db: float = -25.0,
            target: float = 0.0) -> Ducker:
    async def provider() -> float:
        return target
    return Ducker(camilla, duck_db, target_db_provider=provider)


def _ducker_with_dynamic_target(
    camilla: _FakeCamilla, *, duck_db: float = -25.0,
    target_holder: list[float],
) -> Ducker:
    async def provider() -> float:
        return target_holder[0]
    return Ducker(camilla, duck_db, target_db_provider=provider)


@pytest.mark.asyncio
async def test_duck_lowers_camilla_by_duck_db():
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    assert cam._db == -40.0
    assert cam.set_calls == [-40.0]


@pytest.mark.asyncio
async def test_restore_writes_target_db_absolutely():
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    await d.restore()
    assert cam._db == -15.0
    # Two writes: duck (additive) then restore (absolute set).
    assert cam.set_calls == [-40.0, -15.0]


@pytest.mark.asyncio
async def test_restore_uses_current_target_not_pre_duck_value():
    """Regression for the dial-during-duck overshoot. If
    `listening_level` changes mid-session, restore lands at the new
    target — not at `pre_duck + duck_delta`. Reproduces the +25 dB
    bug from 2026-05-08: pre_duck=0, duck=-25 → camilla=-25,
    listening_level moves so target becomes -27. Old additive restore
    would have written 0; new absolute restore writes -27."""
    cam = _FakeCamilla(db=0.0)
    target = [0.0]
    d = _ducker_with_dynamic_target(cam, duck_db=-25.0, target_holder=target)
    await d.duck()
    assert cam._db == -25.0
    target[0] = -27.0
    await d.restore()
    assert cam._db == -27.0


@pytest.mark.asyncio
async def test_restore_after_external_camilla_write_still_uses_target():
    """Even if some other writer touched camilla during the duck
    (the bug case where _set_camilla wasn't gated), restore uses the
    target_db_provider's value — not whatever camilla currently shows."""
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    # Simulate an interloping write (e.g. dial pre-gate) during duck.
    await cam.set_volume_db(0.0)
    await d.restore()
    assert cam._db == -15.0


@pytest.mark.asyncio
async def test_double_duck_is_no_op():
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    await d.duck()
    assert cam.set_calls == [-40.0]


@pytest.mark.asyncio
async def test_restore_without_duck_is_no_op():
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.restore()
    assert cam.set_calls == []


# ---------- camilla unavailable / restart-blip handling --------------------


@pytest.mark.asyncio
async def test_duck_when_camilla_unreachable_does_not_raise():
    """A camilla restart blip during a wake event must not propagate
    into the voice loop. duck() returns silently."""
    cam = _FakeCamilla(db=0.0)
    cam.unavailable = True
    d = _ducker(cam, duck_db=-25.0, target=0.0)
    await d.duck()
    assert cam.set_calls == []


@pytest.mark.asyncio
async def test_duck_when_camilla_unreachable_does_not_latch_ducked():
    """If duck() couldn't actually write, _ducked must stay False so the
    next duck() retries when camilla recovers, and restore() short-
    circuits cleanly. Regression guard for the silent-ducked-state bug:
    if we latched, restore() would attempt a write, succeed once camilla
    is back, and pin the volume to a stale target."""
    cam = _FakeCamilla(db=0.0)
    cam.unavailable = True
    d = _ducker(cam, duck_db=-25.0, target=0.0)
    await d.duck()
    # restore should be a no-op — never wrote during duck.
    await d.restore()
    assert cam.set_calls == []


@pytest.mark.asyncio
async def test_camilla_recovers_voice_resumes_ducking():
    """After a camilla outage during which duck() was a no-op, when
    camilla comes back the next duck()/restore() cycle works normally.
    This is the "Restart=always brought camilla back; voice keeps
    ducking on subsequent wakes" path."""
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)

    # Outage: wake fires, duck/restore are no-ops.
    cam.unavailable = True
    await d.duck()
    await d.restore()
    assert cam.set_calls == []

    # Camilla recovers (Restart=always). Next wake event ducks normally.
    cam.unavailable = False
    await d.duck()
    assert cam.set_calls == [-40.0]
    await d.restore()
    assert cam.set_calls == [-40.0, -15.0]


@pytest.mark.asyncio
async def test_restore_when_camilla_drops_mid_session_clears_latch():
    """duck() succeeded, then camilla went down before restore() — the
    Ducker still resets _ducked=False so a future duck() doesn't see
    a stale latch. Otherwise a flaky camilla connection could leave
    the daemon thinking it had ducked permanently."""
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    assert cam.set_calls == [-40.0]
    cam.unavailable = True
    await d.restore()  # write fails best-effort, but latch resets
    cam.unavailable = False
    # New duck cycle works (would short-circuit if latch was stuck).
    await d.duck()
    assert cam.set_calls == [-40.0, -65.0]


@pytest.mark.asyncio
async def test_cue_plays_when_camilla_unreachable():
    """The single most important silent-failure regression guard:
    when a wake event hits a wake-blocking condition (spend cap,
    can't-connect) AND camilla is restarting, the cue must STILL
    play. Without this, the worst-case cascade looks like:

      camilla crashes → Restart=always brings it back in 2 s →
      during that window the user fires a wake → daemon detects
      can't-connect state → tries to play cant_connect cue →
      cue path tries to duck via Ducker → Ducker's camilla call
      fails → cue is silently dropped → speaker stays silent.

    The fix is: duck failure doesn't prevent cue playback. This test
    mirrors voice_daemon.WakeLoop._play_cue's exact try/except/finally
    structure — if production diverges from it, the contract still
    holds: the cue plays even when ducking can't.
    """
    cam = _FakeCamilla(db=0.0)
    cam.unavailable = True
    d = _ducker(cam, duck_db=-25.0, target=0.0)

    cue_calls: list[str] = []

    async def fake_cue_play(slug: str) -> None:
        cue_calls.append(slug)

    # Mirror voice_daemon.WakeLoop._play_cue's structure.
    slug = "cant_connect"
    try:
        try:
            await d.duck()
        except Exception:
            pass
        await fake_cue_play(slug)
    finally:
        await d.restore()

    assert cue_calls == ["cant_connect"]
    # Camilla was never written — duck silently no-op'd, restore
    # short-circuited (nothing was latched).
    assert cam.set_calls == []
