# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The volume seam filled: what the adapter owes that the owner does not.

The owner's own arbitration is pinned by ``tests/test_volume_owner.py`` and is
not re-asserted here. What this file pins is the three places the handle-free
seam and the handle-carrying owner could disagree: a raised acquire must leave
nothing to give back, a second release must not hand a stale handle back, and a
preempted claim must prove as ``None`` rather than as its declared level.

The fader double is local rather than imported from the owner's suite — a
fixture library that reaches into a test file is the edge ``tests/engine_twin``
was written to avoid, and this one is fifteen lines.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_BASELINE
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.volume_claim import (
    MeasurementVolumeClaim,
    OwnerVolumeDoor,
)
from jasper.volume_owner import ClaimKind, VolumeClaimRefused, VolumeOwner

from tests.engine_twin import FakeSeams, open_session

HOUSEHOLD_DB = -21.5
MEASUREMENT_DB = -12.5
COMMISSIONING_DB = -6.0


class _Fader:
    """A fader that remembers its writes and can refuse one."""

    def __init__(self, db: float | None = -30.0) -> None:
        self.db = db
        self.writes: list[float] = []
        self.accept = True

    async def set(self, db: float) -> bool:
        self.writes.append(float(db))
        if not self.accept:
            return False
        self.db = float(db)
        return True

    async def get(self) -> float | None:
        return self.db


async def _owner(fader: _Fader) -> VolumeOwner:
    owner = VolumeOwner(
        set_fader_db=lambda db: fader.set(db),
        get_fader_db=lambda: fader.get(),
    )
    assert await owner.declare_household_level_db(HOUSEHOLD_DB) is True
    return owner


async def test_a_release_after_an_acquire_that_raised_gives_nothing_back():
    """The seam calls release on the failure path unconditionally.

    The owner is fail-closed: an unconfirmable write raises and leaves no claim
    held, so there is nothing for the adapter to hand back. Without its own
    nothing-held guard the adapter would take a ``None`` handle to the owner
    and fail on the way in — turning a contracted no-op into the second
    exception on an unwind that already has one.
    """
    fader = _Fader()
    owner = await _owner(fader)
    fader.accept = False
    claim = MeasurementVolumeClaim(owner)

    with pytest.raises(VolumeClaimRefused):
        await claim.acquire(MEASUREMENT_DB)

    await claim.release()

    assert owner.declared_level_db() == HOUSEHOLD_DB
    assert await claim.prove() is None


async def test_a_second_release_is_a_no_op_and_hands_back_no_stale_handle():
    fader = _Fader()
    owner = await _owner(fader)
    claim = MeasurementVolumeClaim(owner)
    await claim.acquire(MEASUREMENT_DB)

    await claim.release()
    fader.writes.clear()
    await claim.release()

    assert fader.writes == [], "the second release wrote the fader again"
    assert owner.declared_level_db() == HOUSEHOLD_DB
    assert fader.db == HOUSEHOLD_DB


async def test_a_preempted_claim_proves_as_none_rather_than_its_own_level():
    """Rank 1 is shared and rank 2 outranks it, mid-walk.

    Commissioning (rank 2) takes the fader over a live session claim (rank 1).
    The session's level is no longer in effect, so ``prove`` must refuse rather
    than report the level this claim was acquired at — which is the number a
    record would otherwise be stamped with while the speaker played at another.
    """
    fader = _Fader()
    owner = await _owner(fader)
    claim = MeasurementVolumeClaim(owner)
    await claim.acquire(MEASUREMENT_DB)
    assert await claim.prove() == MEASUREMENT_DB

    await owner.acquire_level(ClaimKind.COMMISSIONING, COMMISSIONING_DB)

    assert fader.db == COMMISSIONING_DB
    assert await claim.prove() is None


async def test_a_claim_proves_its_level_while_it_is_the_one_in_effect():
    """Anti-vacuity for the refusals above: proving must be reachable."""
    fader = _Fader()
    owner = await _owner(fader)
    claim = MeasurementVolumeClaim(owner)

    assert await claim.prove() is None, "nothing is proven before an acquire"
    await claim.acquire(MEASUREMENT_DB)

    assert await claim.prove() == MEASUREMENT_DB
    assert fader.db == MEASUREMENT_DB


async def test_a_real_session_drives_the_adapter_through_the_seam():
    """The seam is satisfied by SHAPE, so the proof is the call working.

    ``mypy`` cannot check that here — it reads ``jasper/`` and nothing has
    assigned this into an ``EngineSeams`` slot until the wave that wires the
    session to a front end. So the conformance evidence is a whole session
    lifetime driven over the real owner: opened, one stimulus proven and
    banked, closed, and the fader back where the household left it.
    """
    fader = _Fader()
    owner = await _owner(fader)
    fakes = FakeSeams().replace(volume=MeasurementVolumeClaim(owner))

    async with open_session(fakes, measurement_level_db=MEASUREMENT_DB) as (
        session, _,
    ):
        assert fader.db == MEASUREMENT_DB, "open() took the claim"
        outcome = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert [r["level_db"] for r in fakes.banked] == [MEASUREMENT_DB]
    assert outcome.record_ids != ()
    assert fader.db == HOUSEHOLD_DB, "close() gave the fader back"


# --------------------------------------------------------------------------- #
# the session's ONE claim, and the plan's door onto the same owner
# --------------------------------------------------------------------------- #


def _owner_over(fader: _Fader) -> VolumeOwner:
    return VolumeOwner(set_fader_db=fader.set, get_fader_db=fader.get)


async def test_a_second_acquire_at_the_same_level_is_the_claim_it_already_holds():
    """Two callers, one session, one claim — and no conflict with itself.

    The plan's door establishes through this adapter and
    ``TuningSession.open`` takes the session's slot through it. Sending the
    second ask to the owner would raise ``VolumeClaimConflict`` against this
    session's OWN claim, which is not the collision the same-kind rule exists
    to catch.
    """
    fader = _Fader(HOUSEHOLD_DB)
    claim = MeasurementVolumeClaim(_owner_over(fader))

    await claim.acquire(MEASUREMENT_DB)
    await claim.acquire(MEASUREMENT_DB)

    assert fader.db == MEASUREMENT_DB
    assert await claim.prove() == MEASUREMENT_DB


async def test_a_second_acquire_at_a_DIFFERENT_level_is_refused():
    """One declared level per session. A quiet re-level would hide the defect.

    Moving a held claim is ``VolumeOwner.relevel`` and is deliberately not this
    seam's verb — five overlapping notions of "the level" is what the
    one-declared-level rule exists to delete.
    """
    fader = _Fader(HOUSEHOLD_DB)
    claim = MeasurementVolumeClaim(_owner_over(fader))
    await claim.acquire(MEASUREMENT_DB)

    with pytest.raises(VolumeClaimRefused):
        await claim.acquire(MEASUREMENT_DB - 6.0)

    assert fader.db == MEASUREMENT_DB, "the refused re-level moved nothing"


async def test_another_holders_same_kind_claim_still_conflicts():
    """The rule stays sharp for the holders it is actually about.

    Level-match, autolevel and the balance guard share this process and this
    kind. A stale one must refuse the session with a NAMED reason, not merge.
    """
    fader = _Fader(HOUSEHOLD_DB)
    owner = _owner_over(fader)
    await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, -9.0)
    claim = MeasurementVolumeClaim(owner)

    with pytest.raises(VolumeClaimRefused) as caught:
        await claim.acquire(MEASUREMENT_DB)

    assert "session_measurement" in str(caught.value)


async def test_the_door_names_the_holder_rather_than_only_failing(caplog):
    """F2's honest surfacing: a reasoned refusal, never a bare failure.

    "CamillaDSP would not confirm" and "another wizard never gave the fader
    back" have opposite fixes, so the disclosure has to tell them apart.
    """
    import logging

    fader = _Fader(HOUSEHOLD_DB)
    owner = _owner_over(fader)
    await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, -9.0)
    door = OwnerVolumeDoor(
        owner, read_fader=fader.get, claim=MeasurementVolumeClaim(owner),
    )

    with caplog.at_level(logging.ERROR):
        established = await door.establish_measurement_level_db(MEASUREMENT_DB)

    assert established is False, "a refused claim is not an established level"
    assert "session_volume_claim_refused" in caplog.text
    assert "session_measurement" in caplog.text, "the holder is named"


async def test_a_door_with_no_claim_cannot_establish_and_says_so():
    """The three out-of-runner drains bind the restore leg alone."""
    fader = _Fader(HOUSEHOLD_DB)
    door = OwnerVolumeDoor(_owner_over(fader), read_fader=fader.get)

    assert await door.establish_measurement_level_db(MEASUREMENT_DB) is False
    assert fader.db == HOUSEHOLD_DB


async def test_the_doors_restore_refuses_a_declaration_the_fader_did_not_take():
    """THE C3 RULING, at the door: declare_ok AND the fader actually landed.

    ``declare_household_level_db`` answers ``True`` for a legitimate deferral
    to a higher-ranked claim — the level is recorded and the fader is not
    written. Passing that through would make the plan report a restore that
    never happened and clear its durable intent over a speaker still at
    measurement level.
    """
    fader = _Fader(HOUSEHOLD_DB)
    owner = _owner_over(fader)
    await owner.declare_household_level_db(HOUSEHOLD_DB)
    # A commissioning claim outranks the household level and holds the fader.
    await owner.acquire_level(ClaimKind.COMMISSIONING, MEASUREMENT_DB)
    door = OwnerVolumeDoor(owner, read_fader=fader.get)

    declared_alone = await owner.declare_household_level_db(HOUSEHOLD_DB)
    restored = await door.restore_household_level_db(HOUSEHOLD_DB)

    assert declared_alone is True, "the owner defers, and says its intent stands"
    assert fader.db == MEASUREMENT_DB, "the deferral wrote nothing"
    assert restored is False, "the DOOR answers for the fader, not the intent"
