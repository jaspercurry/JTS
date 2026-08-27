# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The session's fader claim, filled — twice, for one wave only.

**Two implementations, and exactly one is ever bound.**
:class:`MeasurementVolumeClaim` is the destination: a real ranked claim on
:class:`~jasper.volume_owner.VolumeOwner`. :class:`PlanHeldVolumeClaim` is the
interim, bound while ``SessionVolumePlan`` still owns the fader through its own
door — it takes no claim and proves what the plan holds. W5-c swaps the binding
and deletes the plan's doors in one PR, so no commit boundary has two writers
on this fader. The rest of this docstring describes the destination.

:class:`~.session_seams.VolumeClaim` is handle-**free**: ``acquire`` / ``prove``
/ ``release`` pass nothing between them, because a session holds ONE claim for
its whole lifetime and has no second one to name.
:class:`~jasper.volume_owner.VolumeOwner` is handle-**carrying**: it arbitrates
four claim kinds across a whole process, so every call says which claim it
means. This module is that difference and nothing else — one
:class:`~jasper.volume_owner.VolumeClaimHandle` held between three calls.

**Not the first production fader claim; the fifth consumer of one that already
ships.** ``acquire_level`` has four production callers outside the owner today
— level-match and autolevel in :mod:`jasper.web.correction_setup`, the
commissioning floor tone in :mod:`jasper.web.sound_setup`, and
:mod:`jasper.web.balance_volume_guard` — and three of them already take
``SESSION_MEASUREMENT``. Nothing here clamps, arbitrates or writes the fader:
the owner does all three, and it sits behind ``camilla.py``'s door as its only
caller rather than as its exception.

**Rank 1 is SHARED, which is why ``prove`` is contracted per stimulus.**
``SESSION_MEASUREMENT`` ranks 1, between household (0) and commissioning (2),
and three production takers already hold that same kind — so two rank-1 claims
can be live in one process at once, and a commissioning claim can preempt this
one *between two positions of a single walk*. A proof taken once per spec would
stamp an unverified level into every record after that moment; taken once per
stimulus it refuses exactly the capture that lost the fader, and nothing else.
"""

from __future__ import annotations

from typing import Any, Callable

from ...volume_owner import ClaimKind, VolumeClaimHandle, VolumeOwner
from ..session_volume_plan import SessionVolumePlanError
from ..volume_latch import fader_matches

__all__ = ["MeasurementVolumeClaim", "PlanHeldVolumeClaim"]


class MeasurementVolumeClaim:
    """One session-measurement claim, taken and given back through the owner.

    Construct with the process's owner — :func:`~jasper.volume_owner.volume_owner`
    at the wiring site, never minted here: a second owner over one fader is the
    arbitration failure the owner exists to delete.

    One field of state, and it is the whole adapter.
    """

    def __init__(self, owner: VolumeOwner) -> None:
        self._owner = owner
        self._handle: VolumeClaimHandle | None = None

    async def acquire(self, level_db: float) -> None:
        """Take the claim at the declared level.

        The handle is stored only once ``acquire_level`` RETURNS one. The
        owner is fail-closed — an unconfirmable write raises and leaves no
        claim held — so a raised acquire leaves this adapter holding nothing,
        and the session's unconditional :meth:`release` on that path is a
        no-op rather than a give-back of something never taken.
        """
        self._handle = await self._owner.acquire_level(
            ClaimKind.SESSION_MEASUREMENT, level_db,
        )

    async def prove(self) -> float | None:
        """The fader reading, but only when it agrees with the declared level.

        Delegated whole: the owner's ``prove`` already answers ``None`` for
        every way a level can fail to be in effect — released, preempted,
        ducked over, unreadable, or disagreeing — and re-deciding any of that
        here would be a second arbiter. ``None`` before an acquire for the
        same reason: nothing is in effect, so nothing is proven.
        """
        if self._handle is None:
            return None
        return await self._owner.prove(self._handle)

    async def release(self) -> None:
        """Give the claim back. Idempotent, and a no-op against nothing held.

        The nothing-held guard is this adapter's own, not a restatement of the
        owner's: the owner tolerates a stale handle by ignoring it, but an
        adapter that kept one would go on claiming to hold what it gave back,
        and the next :meth:`prove` would ask about a claim that is gone.
        """
        if self._handle is None:
            return
        await self._owner.release(self._handle)
        self._handle = None


class PlanHeldVolumeClaim:
    """The INTERIM claim: ``SessionVolumePlan`` holds the fader, this proves it.

    **REMOVAL CONDITION, and this is not a fallback.** W5-c swaps this for
    :class:`MeasurementVolumeClaim` in the SAME PR that deletes
    ``SessionVolumePlan``'s fader doors (``_session_volume_io._set`` and its
    consuming call sites). The two implementations are never both bound: the
    seam lands now, the AUTHORITY moves when the old door dies. That is the
    strangler proper — at no commit boundary do two writers hold this fader.

    **Why the real claim is not bound yet, and it is not a coexistence
    annoyance.** The two authorities restore to different definitions. The
    owner's ``release`` lands the fader on the CURRENT next-ranked level — the
    household claim as it stands at release time. The plan's drain restores the
    ORIGINAL snapshot it took before its first mutation, through the
    exact→emergency ladder, and latches when neither confirms. A household
    level that moved during the session therefore makes the two restores
    disagree about where the fader belongs, with no defined ordering between
    them. Binding both would put that divergence on the speaker for a whole
    wave.

    **What this does NOT do.** It takes no claim, writes no fader, and restores
    nothing. Everything it asserts, it read.
    """

    def __init__(
        self, plan: Any, read_fader_db: Callable[[], Any],
    ) -> None:
        self._plan = plan
        self._read_fader_db = read_fader_db

    async def acquire(self, level_db: float) -> None:
        """Verify the plan already holds this session open at this level.

        Two readable facts, checked rather than trusted. ``assert_ready`` is
        the plan's own open/confirmed/inside-the-ceiling assertion — the same
        one ``play_program`` acquires before every stimulus — and it raises
        when the level is not actually in effect, which is the fail-closed
        answer this seam's caller already handles.

        The declared level must also be the one this session was constructed
        for. A session measuring at a level the plan never opened is the *five
        overlapping notions of "the level"* defect the one-declared-level rule
        exists to delete, and it is cheaper to refuse here than to discover it
        in a banked record.
        """
        self._plan.assert_ready()
        declared = self._plan.measurement_volume_db
        if declared is None or not fader_matches(declared, level_db):
            raise SessionVolumePlanError(
                "the session volume plan is open at "
                f"{declared!r} dB, not the declared {level_db} dB"
            )

    async def prove(self) -> float | None:
        """The fader reading, but only when it agrees with the declared level.

        A REAL check and deliberately not a pass-through: the 8.712 dB honesty
        gate has to hold through this window too, or ``measure`` banks records
        stamped with a level the speaker was not playing at. The comparison is
        :func:`~jasper.active_speaker.volume_latch.fader_matches` at the repo's
        one confirm tolerance, against the level the PLAN declares — which is
        the authority this wave — and the session then re-checks the answer
        against its own declared level. Two gates, one number.

        ``None`` for every way the level can fail to be in effect here: the
        plan holds none, the fader could not be read, or the two disagree.
        """
        declared = self._plan.measurement_volume_db
        if declared is None:
            return None
        reading = await self._read_fader_db()
        if reading is None or not fader_matches(reading, declared):
            return None
        return float(reading)

    async def release(self) -> None:
        """A DISCLOSED no-op: the plan's drain owns restore this wave.

        Not an oversight and not a silent one. ``_volume_hooks``'s ``_close``
        and ``_abandon`` arms already run the plan's exact→emergency ladder
        with its durable latch on every path out of the session, and a second
        give-back here would be the double-restore this whole option exists to
        avoid — two authorities landing the fader on two different definitions
        of "where it belongs".

        Idempotent and safe against nothing-held by construction: it holds
        nothing.
        """
        return None
