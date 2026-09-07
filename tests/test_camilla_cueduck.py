# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.camilla import CamillaUnavailable, CueDuck
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


# A transient-duck claim gives its depth back against the level already in
# effect, and the reference is the household claim rather than whatever the
# fader reads — so these tests seed the owner exactly as the coordinator
# does. `tests/conftest.py`'s autouse `_isolate_canonical_target_provider`
# nulls the process-wide provider, so the seeded claim is the only reference
# in play here.


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
