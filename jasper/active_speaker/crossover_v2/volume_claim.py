# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The session's fader claim, and ``SessionVolumePlan``'s door onto the same
``VolumeOwner``. Adapts the handle-free ``session_seams.VolumeClaim`` seam
onto the handle-carrying owner: one ``VolumeClaimHandle`` held between three
calls. ``SESSION_MEASUREMENT`` ranks 1 (household 0, commissioning 2); a
stale rank-1 taker sharing this process makes ``acquire_level`` raise
``VolumeClaimConflict``. Nothing here clamps: ``devices.volume_limit`` stays
``0.0``; ``camilla._coerce_main_volume_db`` clamps every positive write.
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

    Construct with the process's owner
    (:func:`~jasper.volume_owner.volume_owner`), never a freshly minted one: a
    second owner over one fader defeats the arbitration.
    """

    def __init__(self, owner: VolumeOwner) -> None:
        self._owner = owner
        self._handle: VolumeClaimHandle | None = None

    async def acquire(self, level_db: float) -> None:
        """Ensure this session's ONE claim is established at ``level_db``.

        Idempotent for the same level — two callers (the plan's
        :class:`OwnerVolumeDoor` and :meth:`~.session.TuningSession.open`) ask
        for one claim — and raises
        :class:`~jasper.volume_owner.VolumeClaimRefused` on a re-acquire at a
        different level. Moving a held claim is
        :meth:`~jasper.volume_owner.VolumeOwner.relevel`, not this verb. The
        handle is stored only once ``acquire_level`` returns one, so a raised
        acquire leaves nothing held and :meth:`release` is then a no-op.
        """
        held = self._handle
        if held is None:
            self._handle = await self._owner.acquire_level(
                ClaimKind.SESSION_MEASUREMENT, level_db,
            )
            return
        if fader_matches(held.level_db, level_db):
            return
        raise VolumeClaimRefused(
            "this session already holds its measurement claim at "
            f"{held.level_db} dB, not the requested {level_db} dB"
        )

    async def prove(self) -> float | None:
        """The fader reading, but only when it agrees with the declared level.

        Delegated whole to the owner, which already answers ``None`` for every
        way a level can fail to be in effect. ``None`` before an acquire.
        """
        if self._handle is None:
            return None
        return await self._owner.prove(self._handle)

    async def release(self) -> None:
        """Give the claim back. Idempotent, and a no-op against nothing held.

        Lands the fader on the standing household level; the plan's drain runs
        after this and re-asserts its own durable snapshot on top.
        """
        if self._handle is None:
            return
        await self._owner.release(self._handle)
        self._handle = None


class OwnerVolumeDoor:
    """``SessionVolumePlan``'s door onto the owner — the plan stops writing.

    Implements :class:`~jasper.active_speaker.session_volume_plan.VolumeDoor`
    over the owner the session claims through: the plan's ladder chooses the
    level, the owner makes every write. Restore ordering is load-bearing — the
    session releases first (``TuningSession.close``), the plan declares its
    durable snapshot second, and the snapshot wins because it is last.
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

        Becomes ``original_main_volume_db``, the number every drain restores
        toward. A declared-level read would compare a number against itself and
        pass by construction (#2925).
        """
        return await read_fader_db(self._read_fader)

    async def establish_measurement_level_db(self, level_db: float) -> bool:
        """Take the session's claim at ``level_db``; confirmed?

        ``True`` is a fader that read back at the declared level. Any refusal is
        ``False``, never a raise. A door constructed without a claim (the
        restore-only drains) always answers ``False``.
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
            # ``reason=`` separates a conflict from an unconfirmed write —
            # opposite fixes. ``holder=`` is the owner's message, which names
            # the claim kind only; the owner keeps no holder identity.
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

        ``declare_household_level_db`` answers ``True`` both for a written fader
        and for a level merely RECORDED behind a higher-ranked claim; the
        discriminator between them is
        :meth:`~jasper.volume_owner.VolumeOwner.declared_level_db`, and the
        recorded case is ``DEFERRED`` rather than ``FAILED`` so the drain does
        not fall to its −60 dB emergency rung. ``LANDED`` is judged against the
        owner's ``target_db``, not the bare level, because a transient duck
        moves the fader without moving the declared level.
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
