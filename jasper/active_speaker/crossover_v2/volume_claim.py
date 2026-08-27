# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The session's fader claim, filled — the handle the seam does not carry.

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

from ...volume_owner import ClaimKind, VolumeClaimHandle, VolumeOwner

__all__ = ["MeasurementVolumeClaim"]


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
