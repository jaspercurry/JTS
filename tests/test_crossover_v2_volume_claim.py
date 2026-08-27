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
from jasper.active_speaker.crossover_v2.volume_claim import MeasurementVolumeClaim
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
