# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The session's fader claim, and the plan's door onto the same owner.

**One authority over this fader, and W5-c1 is where it becomes true.**
:class:`MeasurementVolumeClaim` is the session's ranked hold;
:class:`OwnerVolumeDoor` is :class:`~jasper.active_speaker.session_volume_plan.
SessionVolumePlan`'s door onto the same :class:`~jasper.volume_owner.VolumeOwner`.
The plan keeps its durable half — the snapshot, the write-before-first-mutation,
the wall-clock ceiling and the unresolved latch — and gives up the writing.

:class:`~.session_seams.VolumeClaim` is handle-**free**: ``acquire`` / ``prove``
/ ``release`` pass nothing between them, because a session holds ONE claim for
its whole lifetime and has no second one to name.
:class:`~jasper.volume_owner.VolumeOwner` is handle-**carrying**: it arbitrates
four claim kinds across a whole process, so every call says which claim it
means. This module is that difference and nothing else — one
:class:`~jasper.volume_owner.VolumeClaimHandle` held between three calls.

**Rank 1 is SHARED, and same-kind claims CONFLICT — those are two different
facts.** ``SESSION_MEASUREMENT`` ranks 1, between household (0) and
commissioning (2). Three other production takers hold that same kind — the
level-match and autolevel ramps in :mod:`jasper.web.correction_setup` and
:mod:`jasper.web.balance_volume_guard` — and they share this process, so a
stale one makes :meth:`VolumeOwner.acquire_level` raise
:class:`~jasper.volume_owner.VolumeClaimConflict` here: *two seats for one
truth* is refused, not merged. What ``prove`` guards is the other axis —
CROSS-rank preemption, a commissioning claim taking the fader *between two
positions of a single walk* — which is why a proof is contracted once per
stimulus rather than once per spec. A proof taken once per spec would stamp an
unverified level into every record after that moment; taken once per stimulus
it refuses exactly the capture that lost the fader, and nothing else.

**Nothing here clamps or arbitrates.** ``devices.volume_limit`` stays ``0.0``
and ``jasper.camilla._coerce_main_volume_db`` clamps every positive write; the
owner sits behind that door as its only caller, never as its exception.
"""

from __future__ import annotations

import logging

from ...log_event import log_event
from ...volume_owner import (
    ClaimKind,
    VolumeClaimHandle,
    VolumeClaimRefused,
    VolumeOwner,
)
from ..session_volume_plan import RestoreOutcome
from ..volume_latch import GetMainVolumeDb, fader_matches, read_fader_db

logger = logging.getLogger(__name__)

__all__ = ["MeasurementVolumeClaim", "OwnerVolumeDoor"]


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

    @property
    def level_db(self) -> float | None:
        """The level this session's claim is held at, or ``None`` if unheld."""
        return None if self._handle is None else self._handle.level_db

    async def acquire(self, level_db: float) -> None:
        """Ensure this session's ONE claim is established at ``level_db``.

        **Idempotent for the SAME level, and that is the seam's own contract
        rather than a fast path around the owner.** Two callers reach this
        adapter for one session: the plan's :class:`OwnerVolumeDoor`, whose
        ``establish`` leg is where the fader is actually written, and
        :meth:`~.session.TuningSession.open`, which takes the session's volume
        slot. They are asking for the same single claim — *"a session holds ONE
        claim for its whole lifetime"* is this class's own premise — so the
        second ask is answered from the handle already held rather than sent to
        the owner as a second claim. Sending it would raise
        :class:`~jasper.volume_owner.VolumeClaimConflict` against this
        session's OWN claim, which is the one collision the owner's same-kind
        rule is not there to catch.

        The rule still bites where it means something: a DIFFERENT holder's
        ``SESSION_MEASUREMENT`` claim raises from the owner untouched, and a
        re-acquire at a DIFFERENT level raises here — that is the *five
        overlapping notions of "the level"* defect the one-declared-level rule
        exists to delete, and a claim that quietly re-levelled would hide it.
        Moving a held claim is :meth:`~jasper.volume_owner.VolumeOwner.relevel`
        and is deliberately not this seam's verb.

        The handle is stored only once ``acquire_level`` RETURNS one. The owner
        is fail-closed — an unconfirmable write raises and leaves no claim held
        — so a raised acquire leaves this adapter holding nothing, and the
        session's unconditional :meth:`release` on that path is a no-op rather
        than a give-back of something never taken.
        """
        held = self._handle
        if held is not None:
            if fader_matches(held.level_db, level_db):
                return
            raise VolumeClaimRefused(
                "this session already holds its measurement claim at "
                f"{held.level_db} dB, not the requested {level_db} dB"
            )
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

        Lands the fader on the standing household level. The plan's drain runs
        AFTER this and re-asserts its own durable snapshot on top, which is the
        ordering that makes one authority out of two definitions of *"where the
        fader belongs"* — see :class:`OwnerVolumeDoor`.
        """
        if self._handle is None:
            return
        await self._owner.release(self._handle)
        self._handle = None


class OwnerVolumeDoor:
    """``SessionVolumePlan``'s door onto the owner — the plan stops writing.

    Implements :class:`~jasper.active_speaker.session_volume_plan.VolumeDoor`
    over the same :class:`~jasper.volume_owner.VolumeOwner` the session claims
    through, so the plan's ladder still CHOOSES the level while the owner makes
    every write.

    **Why the two restores no longer disagree.** The owner's ``release`` lands
    the fader on the current next-ranked level; the plan's drain lands on the
    snapshot it took before its first mutation. Those are two definitions of
    *"where the fader belongs"*, and the interim seam this replaces existed
    because binding both left no ordering between them. There is one now, and
    it is the order the hooks already ran in: the session releases first
    (``TuningSession.close``), the plan's ladder declares its durable snapshot
    second, and the snapshot wins because it is last. Redundant when they agree
    — the owner's settle reads before it writes, so an agreeing pair costs no
    second write at all.
    """

    def __init__(
        self,
        owner: VolumeOwner,
        *,
        read_fader: GetMainVolumeDb,
        claim: MeasurementVolumeClaim | None = None,
    ) -> None:
        self._owner = owner
        self._read_fader = read_fader
        self._claim = claim

    async def read_household_level_db(self) -> float | None:
        """The PHYSICAL fader, deliberately — never the owner's declared level.

        This reading becomes ``original_main_volume_db``, the number every
        drain restores toward and the one the walked-away guarantee is written
        against. Answering with what the owner DECLARES would snapshot the
        intent instead of the state, and the crash this write exists to survive
        is precisely the one where those two disagree — a fader some other
        writer moved, or an owner whose last settle could not be confirmed.

        It is also the #2925 lens: a declared-level read compares a number
        against itself and passes by construction. That is how a whole
        overnight campaign of sweeps ran 8.712 dB below the confirmed volume
        with every check green.
        """
        return await read_fader_db(self._read_fader)

    async def establish_measurement_level_db(self, level_db: float) -> bool:
        """Take the session's claim at ``level_db``; confirmed?

        The claim IS the establishing authority: ``acquire_level`` sets and
        confirms fail-closed, so a ``True`` here is a fader that read back at
        the declared level. A refusal — an unconfirmable write, or a
        ``VolumeClaimConflict`` from another holder's same-kind claim — is
        ``False``, and the plan's caller turns that into its own non-OPENED
        answer rather than a raise.

        A door with no claim cannot establish anything, and says so instead of
        pretending: the three out-of-runner drains bind this door for their
        restore leg alone, and none of them opens a session.
        """
        if self._claim is None:
            log_event(
                logger,
                "correction.session_volume_establish_without_claim",
                level=logging.ERROR,
                level_db=f"{float(level_db):.2f}",
            )
            return False
        try:
            await self._claim.acquire(level_db)
        except VolumeClaimRefused as exc:
            # SPLIT THE TWO FAILURES, which is what a support read needs.
            # ``reason=`` separates a conflict — some other rank-1 taker in
            # this process, the level-match ramp, autolevel or the balance
            # guard, still holding its claim — from a write CamillaDSP would
            # not confirm. Those have opposite fixes. ``holder=`` carries the
            # owner's own message, which names the claim KIND rather than
            # which of the three takers it is; the owner keeps no
            # holder identity to report, and inventing one here would be a
            # second ledger.
            log_event(
                logger,
                "correction.session_volume_claim_refused",
                level=logging.ERROR,
                level_db=f"{float(level_db):.2f}",
                reason=type(exc).__name__,
                holder=str(exc),
            )
            return False
        return True

    async def restore_household_level_db(self, level_db: float) -> RestoreOutcome:
        """Declare the household level, then say what actually became of it.

        **Three answers, because the owner genuinely has three.**
        ``declare_household_level_db`` returns whether the OWNER's intent is in
        effect, and it answers ``True`` in two very different situations: the
        fader was written, or a higher-ranked claim holds the fader and the
        level was merely RECORDED. Collapsing those into one boolean is what
        broke the ladder — a deferral read as a failed write sends
        ``_drain_restore`` to its emergency rung, which declares −60 dB and
        leaves the FLOOR standing as the owner's household level once the
        claim releases.

        So this door distinguishes them, and the discriminator is the owner's
        own synchronous reader rather than a guess:
        :meth:`~jasper.volume_owner.VolumeOwner.declared_level_db` is the
        highest-ranked LEVEL claim held, so a value that is not the level just
        declared means something outranks this declaration and the fader is not
        this door's to move. That is DEFERRED, and the drain stops on it: the
        level is recorded, and the owner lands it when the claim above it
        releases.

        **Landed is judged against the owner's TARGET, not the bare level.** A
        transient duck is an attenuation over the level in effect, not a
        different level — it leaves ``declared_level_db`` alone and moves the
        fader. Comparing against the bare level would call a ducked speaker a
        failed write and send the ladder to its floor, which is the same defect
        arriving through a different door.
        """
        if not await self._owner.declare_household_level_db(level_db):
            return RestoreOutcome.FAILED
        in_effect = self._owner.declared_level_db()
        if in_effect is None or not fader_matches(in_effect, level_db):
            return RestoreOutcome.DEFERRED
        target = self._owner.target_db()
        reading = await self.read_household_level_db()
        if target is not None and fader_matches(reading, target):
            return RestoreOutcome.LANDED
        return RestoreOutcome.FAILED
