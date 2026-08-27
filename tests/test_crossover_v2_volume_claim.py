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
from jasper.active_speaker.crossover_v2.session import UNPROVEN_LEVEL
from jasper.active_speaker.crossover_v2.volume_claim import (
    MeasurementVolumeClaim,
    PlanHeldVolumeClaim,
)
from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
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
# the INTERIM claim — the plan holds the fader, this proves it
#
# Bound only while `SessionVolumePlan` still owns the fader. What matters is
# that the honesty gate stays REAL through that window: a pass-through prove()
# would let `measure` bank records stamped with a level the speaker was not
# playing at, and every other assertion here would stay green while it did.
# --------------------------------------------------------------------------- #


class _Plan:
    """The fader authority this wave: open at a declared level, or not."""

    def __init__(
        self, declared: float | None = MEASUREMENT_DB, *, ready: bool = True,
    ) -> None:
        self.measurement_volume_db = declared
        self._ready = ready
        self.asserted = 0

    def assert_ready(self, now: float | None = None) -> None:
        self.asserted += 1
        if not self._ready:
            raise SessionVolumePlanError("no measurement volume is open")


def _plan_held(plan: _Plan, fader_db: float | None) -> PlanHeldVolumeClaim:
    async def _read() -> float | None:
        return fader_db

    return PlanHeldVolumeClaim(plan, _read)


async def test_the_interim_claim_proves_a_fader_that_agrees_with_the_plan():
    claim = _plan_held(_Plan(), MEASUREMENT_DB)

    await claim.acquire(MEASUREMENT_DB)

    assert await claim.prove() == MEASUREMENT_DB


async def test_a_fader_moved_out_from_under_the_plan_does_not_prove():
    """Condition 2's bar: the honesty gate is real, not a pass-through.

    Something moved the fader while the plan still believes it holds the
    session's level. `prove` must refuse, because a reading that disagrees is
    the 8.712 dB shape — a number stamped into a record while the speaker
    played at another.
    """
    claim = _plan_held(_Plan(), MEASUREMENT_DB + 8.712)

    await claim.acquire(MEASUREMENT_DB)

    assert await claim.prove() is None


async def test_a_moved_fader_refuses_the_bank_and_not_the_walk():
    """The refusal reaches `measure` as MS-14's, end to end.

    Driving the real session over the real interim claim: the stimulus still
    plays, the walk still continues, and what the drift costs is the CLAIM.
    """
    claim = _plan_held(_Plan(), MEASUREMENT_DB + 8.712)
    fakes = FakeSeams().replace(volume=claim)

    async with open_session(fakes, measurement_level_db=MEASUREMENT_DB) as (
        session, _,
    ):
        outcome = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert fakes.play.calls, "the stimulus must still have played"
    assert outcome.record_ids == ()
    assert fakes.banked == []
    assert outcome.stimuli[0].incident == UNPROVEN_LEVEL


async def test_an_unreadable_fader_does_not_prove():
    claim = _plan_held(_Plan(), None)

    await claim.acquire(MEASUREMENT_DB)

    assert await claim.prove() is None


async def test_a_plan_holding_nothing_proves_nothing():
    claim = _plan_held(_Plan(declared=None), MEASUREMENT_DB)

    assert await claim.prove() is None


async def test_an_acquire_against_a_plan_that_is_not_open_fails_closed():
    """`assert_ready` is a readable fact, so it is checked rather than trusted."""
    plan = _Plan(ready=False)
    claim = _plan_held(plan, MEASUREMENT_DB)

    with pytest.raises(SessionVolumePlanError):
        await claim.acquire(MEASUREMENT_DB)

    assert plan.asserted == 1


async def test_a_session_measuring_at_a_level_the_plan_never_opened_is_refused():
    """The one-declared-level rule, checked at the seam it enters.

    A session whose declared level is not the plan's would stamp its own
    number onto captures the plan set a different level for — the five
    overlapping notions of "the level" this rule deletes.
    """
    claim = _plan_held(_Plan(declared=MEASUREMENT_DB), MEASUREMENT_DB)

    with pytest.raises(SessionVolumePlanError):
        await claim.acquire(MEASUREMENT_DB - 6.0)


async def test_the_interim_release_restores_nothing_and_says_so():
    """Condition 3: a disclosed no-op, and it must not double-restore.

    The plan's drain owns restore this wave. The double-give-back this avoids
    is why the real claim is not bound yet, so a release that touched the
    fader here would reintroduce exactly what the option exists to prevent.
    """
    fader_reads = 0

    async def _read() -> float | None:
        nonlocal fader_reads
        fader_reads += 1
        return MEASUREMENT_DB

    plan = _Plan()
    claim = PlanHeldVolumeClaim(plan, _read)
    await claim.acquire(MEASUREMENT_DB)

    await claim.release()
    await claim.release()

    assert fader_reads == 0, "release must not even READ the fader"
    assert plan.asserted == 1, "release must not re-assert the plan"
