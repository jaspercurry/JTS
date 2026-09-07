# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.camilla import CamillaUnavailable, CueDuck, Ducker
from jasper.volume_owner import VolumeOwner


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


async def _owner(camilla: _FakeCamilla, household_db: float) -> VolumeOwner:
    """The one fader owner, standing at the household level.

    Every duck holder in a process shares this instance — in production it is
    ``VolumeCoordinator.volume_owner``. Seeding it is not test scaffolding: a
    duck is an attenuation BELOW the level in effect, so a duck over no
    declared level has nothing to subtract from.
    """
    owner = VolumeOwner(
        set_fader_db=lambda db: camilla.set_volume_db(db, best_effort=True),
        get_fader_db=lambda: camilla.get_volume_db(best_effort=True),
    )
    await owner.declare_household_level_db(household_db)
    return owner


async def _ducker(camilla: _FakeCamilla, *, duck_db: float = -25.0,
                  target: float = 0.0) -> Ducker:
    async def provider() -> float:
        return target
    owner = await _owner(camilla, target)
    return Ducker(owner, duck_db, target_db_provider=provider)


async def _ducker_with_dynamic_target(
    camilla: _FakeCamilla, *, duck_db: float = -25.0,
    target_holder: list[float],
) -> Ducker:
    async def provider() -> float:
        return target_holder[0]
    owner = await _owner(camilla, target_holder[0])
    return Ducker(owner, duck_db, target_db_provider=provider)


async def test_duck_lowers_camilla_by_duck_db():
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    assert cam._db == -40.0
    assert cam.set_calls == [-40.0]


async def test_restore_writes_target_db_absolutely():
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    await d.restore()
    assert cam._db == -15.0
    # Two writes: duck (additive) then restore (absolute set).
    assert cam.set_calls == [-40.0, -15.0]


async def test_restore_uses_current_target_not_pre_duck_value():
    """Regression for the remote-during-duck overshoot. If
    `listening_level` changes mid-session, restore lands at the new
    target — not at `pre_duck + duck_delta`. Reproduces the +25 dB
    bug from 2026-05-08: pre_duck=0, duck=-25 → camilla=-25,
    listening_level moves so target becomes -27. Old additive restore
    would have written 0; new absolute restore writes -27."""
    cam = _FakeCamilla(db=0.0)
    target = [0.0]
    d = await _ducker_with_dynamic_target(cam, duck_db=-25.0, target_holder=target)
    await d.duck()
    assert cam._db == -25.0
    target[0] = -27.0
    await d.restore()
    assert cam._db == -27.0


async def test_restore_after_external_camilla_write_still_uses_target():
    """Even if some other writer touched camilla during the duck
    (the bug case where _set_camilla wasn't gated), restore uses the
    target_db_provider's value — not whatever camilla currently shows."""
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    # Simulate an interloping write (e.g. remote pre-gate) during duck.
    await cam.set_volume_db(0.0)
    await d.restore()
    assert cam._db == -15.0


async def test_double_duck_is_no_op():
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    await d.duck()
    assert cam.set_calls == [-40.0]


async def test_restore_without_duck_is_no_op():
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.restore()
    assert cam.set_calls == []


async def test_is_ducked_property_tracks_duck_state():
    """`is_ducked` is the public signal that jasper-control's
    VolumeCoordinator consults (via UDS session_status) to decide
    whether to defer a remote/web-slider camilla write. Must reflect
    the actual ducker state, not just _ducked's last assignment."""
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)
    assert d.is_ducked is False
    await d.duck()
    assert d.is_ducked is True
    await d.restore()
    assert d.is_ducked is False


async def test_camilla_ducker_declares_exclusive_volume_ownership():
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)
    assert d.locks_camilla_volume is True


async def test_is_ducked_stays_false_when_duck_skipped_camilla_down():
    """Camilla restart blip during the duck attempt — the write
    didn't land, so we mustn't claim we're ducked (jasper-control
    would defer remote writes against a phantom duck, freezing the
    knob until camilla recovers and the next duck() actually fires)."""
    cam = _FakeCamilla(db=0.0)
    cam.unavailable = True
    d = await _ducker(cam, duck_db=-25.0, target=0.0)
    await d.duck()
    assert d.is_ducked is False


# ---------- camilla unavailable / restart-blip handling --------------------


async def test_duck_when_camilla_unreachable_does_not_raise():
    """A camilla restart blip during a wake event must not propagate
    into the voice loop. duck() returns silently."""
    cam = _FakeCamilla(db=0.0)
    cam.unavailable = True
    d = await _ducker(cam, duck_db=-25.0, target=0.0)
    await d.duck()
    assert cam.set_calls == []


async def test_duck_when_camilla_unreachable_does_not_latch_ducked():
    """If duck() couldn't actually write, _ducked must stay False so the
    next duck() retries when camilla recovers, and restore() short-
    circuits cleanly. Regression guard for the silent-ducked-state bug:
    if we latched, restore() would attempt a write, succeed once camilla
    is back, and pin the volume to a stale target."""
    cam = _FakeCamilla(db=0.0)
    cam.unavailable = True
    d = await _ducker(cam, duck_db=-25.0, target=0.0)
    await d.duck()
    # restore should be a no-op — never wrote during duck.
    await d.restore()
    assert cam.set_calls == []


async def test_camilla_recovers_voice_resumes_ducking():
    """After a camilla outage during which duck() was a no-op, when
    camilla comes back the next duck()/restore() cycle works normally.
    This is the "Restart=always brought camilla back; voice keeps
    ducking on subsequent wakes" path."""
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)

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


async def test_restore_when_camilla_drops_mid_session_clears_latch():
    """duck() succeeded, then camilla went down before restore() — the
    claim is still given back so a future duck() doesn't see a stale
    latch. Otherwise a flaky camilla connection could leave the daemon
    thinking it had ducked permanently.

    The second duck does not STACK on the first, and that is the fix the
    owner brings: the fader never came back up, so one duck over the
    household level is already exactly where it should be. The relative
    give-back this replaced wrote -65 — the same 25 dB taken twice, leaving
    the next voice turn 25 dB quieter than it asked for until a restore
    happened to succeed.
    """
    cam = _FakeCamilla(db=-15.0)
    d = await _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    assert cam.set_calls == [-40.0]
    cam.unavailable = True
    await d.restore()  # write fails best-effort, but the claim is given back
    assert d.is_ducked is False
    cam.unavailable = False
    # New duck cycle works (would short-circuit if the latch was stuck).
    await d.duck()
    assert d.is_ducked is True
    assert cam._db == -40.0
    assert cam.set_calls == [-40.0]


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
    d = await _ducker(cam, duck_db=-25.0, target=0.0)

    cue_calls: list[str] = []

    async def fake_cue_play(slug: str) -> None:
        cue_calls.append(slug)

    # Mirror voice_daemon.WakeLoop._play_cue's structure.
    slug = "cant_connect"
    try:
        try:
            await d.duck()
        except Exception:  # noqa: BLE001
            pass
        await fake_cue_play(slug)
    finally:
        await d.restore()

    assert cue_calls == ["cant_connect"]
    # Camilla was never written — duck silently no-op'd, restore
    # short-circuited (nothing was latched).
    assert cam.set_calls == []


# --- CueDuck -----------------------------------------------------
# A transient-duck claim for brief cue playback. It differs from Ducker in
# exactly one way now: Ducker re-declares the household level from its
# provider as part of releasing, because the level it restores to can be
# written by ANOTHER daemon while it holds. A cue is short and passive, so it
# gives its depth back against the level already in effect.
#
# These docstrings used to say the release replays a pre-duck SNAPSHOT and
# reads no target at all. That stopped being true in production the day
# `daemon_main` started registering a canonical target provider — from then on
# `_duck_release_target_db` released to `min(canonical, current + depth)`, and
# only `tests/conftest.py`'s autouse `_isolate_canonical_target_provider`,
# which nulls that provider, kept the old story alive HERE. The owner makes
# the seam explicit instead of ambient: the reference is the household claim,
# and these tests seed it exactly as the coordinator does.


async def test_cueduck_gives_back_its_own_depth():
    """Core contract: enter takes the depth, exit hands that depth back."""
    cam = _FakeCamilla(db=-14.0)  # user's listening level
    async with CueDuck(await _owner(cam, cam._db), duck_db=-10.0):
        assert cam._db == -24.0  # ducked
    assert cam._db == -14.0      # the level in effect, back


async def test_cueduck_release_ignores_an_interloping_write():
    """Regression guard for the bug that motivated CueDuck: if any
    other writer touches camilla during the duck window, that value
    must not become the release target.

    The reference is the household level the owner holds, not whatever the
    fader happens to read — and `min(reference, current + depth)` bounds the
    give-back by what this holder actually took, so an interloper can only
    make the result quieter, never louder."""
    cam = _FakeCamilla(db=0.0)
    async with CueDuck(await _owner(cam, cam._db), duck_db=-25.0):
        # Simulate the volume_coordinator's source-aware logic
        # writing a different camilla target mid-cue (1 Hz poll
        # observed an AirPlay slider drag, listening_level
        # reconciliation, etc.).
        await cam.set_volume_db(-14.0)
        assert cam._db == -14.0
    # The household claim wins — music returns to the level in effect.
    assert cam._db == 0.0


async def test_cueduck_drops_by_duck_db():
    """The perceived attenuation is identical between the long-turn and
    brief-cue paths — same audible level drop, one claim kind."""
    cam = _FakeCamilla(db=-6.0)
    async with CueDuck(await _owner(cam, cam._db), duck_db=-25.0):
        assert cam._db == -31.0


async def test_cueduck_restores_even_if_speak_raises():
    """The cue body running inside `async with` may raise (network
    blip, TTS empty response after retries, etc.). `__aexit__` must
    still release the claim — otherwise music stays ducked."""
    cam = _FakeCamilla(db=-10.0)
    with pytest.raises(RuntimeError, match="boom"):
        async with CueDuck(await _owner(cam, cam._db), duck_db=-25.0):
            assert cam._db == -35.0  # ducked
            raise RuntimeError("boom")
    assert cam._db == -10.0


async def test_cueduck_skips_duck_when_camilla_unavailable():
    """Camilla restarting at cue time → the attenuation cannot be
    established, so the claim is REFUSED rather than held. `__aenter__`
    writes nothing and `__aexit__` has nothing to give back. Music plays
    unducked over the cue rather than crashing the daemon."""
    cam = _FakeCamilla(db=-10.0)
    cam.unavailable = True
    async with CueDuck(await _owner(cam, cam._db), duck_db=-25.0):
        pass
    assert cam.set_calls == []


async def test_cueduck_writes_no_unnecessary_volume_writes():
    """Sanity: a CueDuck round-trip writes exactly two values to
    camilla (the ducked target, then the level in effect). No spurious
    intermediate writes."""
    cam = _FakeCamilla(db=-7.5)
    async with CueDuck(await _owner(cam, cam._db), duck_db=-25.0):
        pass
    assert cam.set_calls == [-32.5, -7.5]
