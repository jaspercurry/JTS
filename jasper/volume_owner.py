# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one owner of CamillaDSP's main fader — four ranked claim kinds.

``docs/REFACTOR-TUNING-2026-08.md`` §3 wave 5 collapses **18
production-reachable fader writers**, nine of which can interleave inside a
single crossover-v2 measurement session with nothing arbitrating between them,
into one owner exposing four claim kinds: **household · transient-duck ·
session-measurement · commissioning**. This module is that owner. Wave 5b/5c
route the writers into it; wave 6 wires
:class:`~jasper.active_speaker.crossover_v2.session_seams.VolumeClaim` — the
engine's session-measurement face — onto it.

**What "ranked" means, exactly.** Three of the four kinds declare a LEVEL: an
absolute dB the fader should read. They are totally ordered — household <
session-measurement < commissioning — and the highest-ranked claim currently
held is *the level in effect*. The fourth kind, the transient duck, declares no
level at all: it is an ATTENUATION that composes below whichever level is in
effect. That asymmetry is the design, not an omission — a duck that could win
the level question would be a level claim with a confusing name.

A lower-ranked level claim held under a higher-ranked one is **recorded and not
written**. That is the whole point: a household volume change during a
measurement session no longer moves the fader out from under the stimulus, and
it is not lost either — it is what the fader lands on when the measurement
claim releases.

**One declared level.** Five overlapping notions collapse here:
``listening_level``, ``measurement_volume_db``, ``locked_main_volume_db``,
``SolvedLevel.main_volume_db`` and ``fader_db``. A claim's ``level_db`` is the
only one left. (``SolvedLevel.main_volume_db`` is a proposal that no writer ever
wrote; it becomes an argument to :meth:`VolumeOwner.acquire_level`, never a
second seat of truth.)

**One confirm tolerance, and it is not minted here.**
:data:`~jasper.active_speaker.volume_latch.READBACK_TOLERANCE_DB` via
:func:`~jasper.active_speaker.volume_latch.fader_matches` is the repo's one
*"do these two fader dB values agree?"* test. This module consumes it. Wave 5
collapses the two independent ``0.05`` literals and the ``1e-6`` onto it.

**The 0 dB ceiling is NOT this module's.** ``devices.volume_limit`` stays
``0.0`` and ``jasper.camilla._coerce_main_volume_db`` clamps every positive
write; the owner sits BEHIND that door as its only caller, never as its
exception. Nothing here re-implements or relaxes it — a fourth clamp owner
would make the rule harder to read, not safer. The owner refuses only
*non-finite* numbers, which is arithmetic integrity (a NaN would poison the
``min`` below), not a safety clamp.

**The release algebra is ADR-0004's**, including the part that is easy to get
backwards: a duck gives back its own attenuation and nothing else
(``min(reference, current + depth)``), while a *level* claim's release restores
the next level outright. Clamping a level release against the live fader would
strand the speaker at the level being given up. The reference is read
synchronously at release time, from live claim state — ADR-0004 constraint 3,
which a design that resolved references eagerly would reintroduce as a defect
under a new name.

**Every settle reads first.** Arbitration re-derives the whole target on every
claim change, so a write that lands where the fader already sits has to cost
nothing — CamillaDSP ramps each one over 400 ms. Reading first also makes the
write a TRIPWIRE: a target that reads back wrong is disclosed where it happens.
That pair is what lets wave 5e delete the 1 Hz drift reconciler rather than
replace it, and it is the same shape
:func:`~jasper.active_speaker.volume_latch.hold_fader_at` already uses.

**In-memory, per process.** Durable volume-safety state belongs to the claim
holders that own it (wave 5d merges the three schemas). Cross-daemon ordering
stays with the leases that already provide it. This owner arbitrates the
writers inside one process, which is where all nine session collisions live.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from .active_speaker.volume_latch import READBACK_TOLERANCE_DB, fader_matches
from .log_event import log_event

logger = logging.getLogger(__name__)

__all__ = [
    "ClaimKind",
    "VolumeClaimConflict",
    "VolumeClaimHandle",
    "VolumeClaimRefused",
    "VolumeOwner",
    "duck_release_target_db",
]

SetFaderDb = Callable[[float], Awaitable[Any]]
GetFaderDb = Callable[[], Awaitable[Any]]

#: Errors a fader read/write is allowed to fail with. Named rather than blind,
#: exactly as ``volume_latch`` names them: the controller already closes its
#: own surface and callers wrap ``CamillaUnavailable`` before it reaches here.
_FADER_IO_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


class ClaimKind(Enum):
    """The four things that may claim the main fader."""

    HOUSEHOLD = "household"
    TRANSIENT_DUCK = "transient_duck"
    SESSION_MEASUREMENT = "session_measurement"
    COMMISSIONING = "commissioning"


#: The total order over LEVEL claims. The transient duck is deliberately absent
#: — it declares an attenuation, not a level, and never answers "what level is
#: in effect right now".
_LEVEL_RANK: dict[ClaimKind, int] = {
    ClaimKind.HOUSEHOLD: 0,
    ClaimKind.SESSION_MEASUREMENT: 1,
    ClaimKind.COMMISSIONING: 2,
}


class VolumeClaimRefused(RuntimeError):
    """A claim could not be established, so the caller does not hold one."""


class VolumeClaimConflict(VolumeClaimRefused):
    """A second level claim of the same kind — two seats for one truth."""


@dataclass(frozen=True)
class VolumeClaimHandle:
    """What a holder gets back, and hands to :meth:`VolumeOwner.release`.

    Opaque by intent: a holder may read its own ``kind`` and ``level_db`` for
    disclosure, but the arbitration is the owner's and a handle carries no
    authority of its own.
    """

    kind: ClaimKind
    token: int
    level_db: float | None = None
    depth_db: float | None = None


def duck_release_target_db(
    *, reference_db: float, current_db: float | None, depth_db: float,
) -> float:
    """Where a releasing DUCK lands the fader — ADR-0004's algebra.

    ``min(reference, current + depth)``: give back this holder's own
    attenuation and nothing else, and never end above the level that should be
    in effect. Both halves are load-bearing and their failure modes are
    opposite — replaying an entry snapshot strands the fader when holders
    interleave, and a bare relative give-back clamps to 0 dB (loud) when the
    level changes inside the window.

    An unreadable fader falls back to the reference: the level that should be
    in effect is still known, and it is never louder than the relative
    give-back would have been.
    """
    if current_db is None:
        return float(reference_db)
    return min(float(reference_db), float(current_db) + abs(float(depth_db)))


def _finite(value: Any, what: str) -> float:
    if isinstance(value, bool):
        raise VolumeClaimRefused(f"{what} must be numeric, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VolumeClaimRefused(
            f"{what} must be numeric, got {value!r}"
        ) from exc
    if not math.isfinite(number):
        raise VolumeClaimRefused(f"{what} must be finite, got {value!r}")
    return number


class VolumeOwner:
    """Every fader write in this process, arbitrated by rank.

    Constructed with the write door and the read door as injected coroutines —
    in production ``CamillaController.set_volume_db`` and ``.get_volume_db``,
    so every write this owner makes passes ``_coerce_main_volume_db``'s clamp.
    Injection rather than a controller import keeps the owner free of
    ``jasper.camilla``'s import graph and makes a test double a peer of
    production rather than a mock of it.
    """

    def __init__(
        self,
        *,
        set_fader_db: SetFaderDb,
        get_fader_db: GetFaderDb,
        tolerance_db: float = READBACK_TOLERANCE_DB,
    ) -> None:
        self._set_fader_db = set_fader_db
        self._get_fader_db = get_fader_db
        self._tolerance_db = float(tolerance_db)
        self._claims: dict[int, VolumeClaimHandle] = {}
        self._tokens = itertools.count(1)
        self._lock = asyncio.Lock()

    # ---- the synchronous readers (ADR-0004 constraint 3) -----------------

    def declared_level_db(self) -> float | None:
        """The level in effect: the highest-ranked level claim held.

        SYNCHRONOUS and non-blocking by contract. It is asked at release time,
        from inside a shielded ``finally``, where awaiting could strand a
        ducked — silent — speaker. ``None`` means no level claim is held at
        all, which is a fall-through and not an error.
        """
        top = self._top_level_claim()
        return None if top is None else top.level_db

    def duck_depth_db(self) -> float:
        """Total attenuation the held ducks are asking for, in dB."""
        return sum(
            abs(float(claim.depth_db or 0.0))
            for claim in self._claims.values()
            if claim.kind is ClaimKind.TRANSIENT_DUCK
        )

    def target_db(self) -> float | None:
        """What the fader should read right now: level in effect, ducked."""
        level = self.declared_level_db()
        return None if level is None else level - self.duck_depth_db()

    def holds(self, handle: VolumeClaimHandle) -> bool:
        """Is this claim still held? False once released or never taken."""
        return self._claims.get(handle.token) == handle

    # ---- taking and giving back claims -----------------------------------

    async def acquire_level(
        self, kind: ClaimKind, level_db: float,
    ) -> VolumeClaimHandle:
        """Take a LEVEL claim at ``level_db``, set the fader, and confirm it.

        Fail-closed: an unconfirmable write raises
        :class:`VolumeClaimRefused` and leaves no claim held, because a level
        that could not be established is not a level anything may be admitted
        against. A claim that outranks what is currently in effect moves the
        fader; one that does not is recorded and writes nothing.

        ``ClaimKind.HOUSEHOLD`` is not taken here — see
        :meth:`declare_household_level_db`, which is the standing claim.
        """
        if kind is ClaimKind.TRANSIENT_DUCK:
            raise VolumeClaimRefused("a duck declares a depth, not a level")
        if kind is ClaimKind.HOUSEHOLD:
            raise VolumeClaimRefused(
                "the household level is declared, not acquired"
            )
        target = _finite(level_db, "level_db")
        async with self._lock:
            if any(claim.kind is kind for claim in self._claims.values()):
                raise VolumeClaimConflict(
                    f"a {kind.value} level claim is already held"
                )
            handle = VolumeClaimHandle(
                kind=kind, token=next(self._tokens), level_db=target,
            )
            self._claims[handle.token] = handle
            if self._top_level_claim() is not handle:
                return handle
            if not await self._apply(context=f"acquire:{kind.value}"):
                # Give the claim back BEFORE settling, so the fader returns to
                # the level that was in effect rather than being stranded on a
                # target this claim could not establish. The caller holds
                # nothing, so its own release cannot do this for it.
                del self._claims[handle.token]
                await self._apply(context=f"acquire_failed:{kind.value}")
                raise VolumeClaimRefused(
                    f"could not establish {target:.6f} dB for {kind.value}"
                )
            return handle

    async def declare_household_level_db(self, level_db: float) -> bool:
        """The standing claim: what the speaker plays at when nothing else has it.

        Idempotent in shape — declaring replaces the household level rather
        than stacking a second one, because "the household level" is one fact.
        Writes the fader only when household is the level in effect; under a
        measurement or commissioning claim the new level is recorded and
        becomes what that claim's release lands on.

        Returns whether the owner's intent is in effect: a write that
        confirmed, or a legitimate deferral to a higher-ranked claim.
        ``False`` means the fader could not be established at the household
        level — the declaration still stands, and the next arbitration writes
        it again.
        """
        target = _finite(level_db, "level_db")
        async with self._lock:
            for token, claim in list(self._claims.items()):
                if claim.kind is ClaimKind.HOUSEHOLD:
                    del self._claims[token]
            handle = VolumeClaimHandle(
                kind=ClaimKind.HOUSEHOLD,
                token=next(self._tokens),
                level_db=target,
            )
            self._claims[handle.token] = handle
            if self._top_level_claim() is not handle:
                return True
            return await self._apply(context="declare:household")

    async def acquire_duck(self, depth_db: float) -> VolumeClaimHandle:
        """Take ``depth_db`` of transient attenuation off the level in effect.

        Ducks STACK: two holders that overlap take both depths, and each gives
        back only its own. A duck over no declared level writes nothing — there
        is no level to attenuate — and the claim is still held, so its release
        is still safe.
        """
        depth = abs(_finite(depth_db, "depth_db"))
        async with self._lock:
            handle = VolumeClaimHandle(
                kind=ClaimKind.TRANSIENT_DUCK,
                token=next(self._tokens),
                depth_db=depth,
            )
            self._claims[handle.token] = handle
            await self._apply(context="acquire:transient_duck")
            return handle

    async def release(self, handle: VolumeClaimHandle) -> None:
        """Give a claim back. Idempotent, and a no-op against nothing held.

        Called on every path out of a holder's lifetime, including after an
        acquire that raised and again if a first release raised — the same
        contract ``session_seams.VolumeClaim.release`` states.
        """
        async with self._lock:
            if self._claims.get(handle.token) != handle:
                return
            del self._claims[handle.token]
            reference = self.declared_level_db()
            if reference is None:
                return
            settled = reference - self.duck_depth_db()
            if handle.kind is ClaimKind.TRANSIENT_DUCK:
                settled = duck_release_target_db(
                    reference_db=settled,
                    current_db=await self._read(),
                    depth_db=handle.depth_db or 0.0,
                )
            await self._settle(
                settled, context=f"release:{handle.kind.value}",
            )

    # ---- MS-14 ------------------------------------------------------------

    async def prove(self, handle: VolumeClaimHandle) -> float | None:
        """The fader reading, but only when it AGREES with this claim's level.

        MS-14, and the shape ruling S10 preserves: ``None`` means *not proven*,
        which refuses to BANK a capture — never to play the stimulus and never
        to try again. Returning a drifted reading instead would hand a caller a
        number to stamp into a record while the speaker played at a different
        one, which is the 8.712 dB shape #2925 recorded.

        ``None`` for every way a level can fail to be in effect: the claim was
        released, a higher-ranked claim preempted it, a duck is down over it,
        the fader could not be read, or the reading disagrees. Never gated on a
        diagnostics flag (ADR-0009) — the excitation-safety ledger admitted the
        program against the declared level, so this is that ledger's own
        integrity rather than forensics that may be sampled.

        **The read is UNCONDITIONAL**, taken before any of those verdicts is
        decided. That is what keeps ``observed_db`` a real observation on every
        line: short-circuiting a refusal ahead of the read would report "the
        fader could not be read" for cases where it was never asked, stating an
        observation JTS never made (#2085).
        """
        expected = handle.level_db
        observed = await self._read()
        if expected is None or not self.holds(handle):
            result = "unheld"
        elif self._top_level_claim() is not handle:
            result = "preempted"
        elif not fader_matches(
            observed, expected, tolerance_db=self._tolerance_db,
        ):
            result = "refused"
        else:
            self._disclose(handle, result="held", observed=observed)
            return observed
        self._disclose(handle, result=result, observed=observed)
        return None

    # ---- internals --------------------------------------------------------

    def _top_level_claim(self) -> VolumeClaimHandle | None:
        ranked = [
            claim
            for claim in self._claims.values()
            if claim.kind in _LEVEL_RANK
        ]
        if not ranked:
            return None
        return max(ranked, key=lambda c: (_LEVEL_RANK[c.kind], c.token))

    async def _apply(self, *, context: str) -> bool:
        """Settle the fader on the arbitrated target. No level writes nothing."""
        target = self.target_db()
        if target is None:
            return True
        return await self._settle(target, context=context)

    async def _settle(self, target_db: float, *, context: str) -> bool:
        """Put the fader on ``target_db`` and prove it, writing only if needed.

        READ, then write only on disagreement, then prove through a further
        independent read — the same shape
        :func:`~jasper.active_speaker.volume_latch.hold_fader_at` already uses,
        and for the same two reasons.

        **The pre-read is why arbitration is not churn.** Every claim change
        re-derives the whole target, so a household level re-declared under a
        held duck, or a release that lands where the fader already sits, would
        otherwise repeat a write CamillaDSP ramps over 400 ms. Reading first
        makes those free and keeps the audible behaviour identical to the
        single write each writer performs today.

        **And it is why the write is a tripwire.** A target that reads back
        wrong is disclosed at the moment it happens, which is what lets wave 5e
        DELETE the 1 Hz drift reconciler rather than replace it: with one
        owner, drift is an alarm at the write boundary instead of something a
        cross-process patrol silently corrects a second later. The pre-read
        repairs a fader that drifted off a level nobody re-declared, so
        skipping the write never means skipping the check.
        """
        target = float(target_db)
        if fader_matches(
            await self._read(), target, tolerance_db=self._tolerance_db,
        ):
            return True
        try:
            applied = await self._set_fader_db(target)
        except _FADER_IO_ERRORS:
            applied = False
        confirmed = applied is not False and fader_matches(
            await self._read(), target, tolerance_db=self._tolerance_db,
        )
        if not confirmed:
            log_event(
                logger,
                "volume.claim_write_unconfirmed",
                level=logging.WARNING,
                context=context,
                target_db=f"{target:.6f}",
            )
        return confirmed

    async def _read(self) -> float | None:
        """One live read, normalized to "a usable number, or nothing"."""
        try:
            value = await self._get_fader_db()
        except _FADER_IO_ERRORS:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(float(value)) else None

    def _disclose(
        self,
        handle: VolumeClaimHandle,
        *,
        result: str,
        observed: float | None,
    ) -> None:
        """One vocabulary, one question, discriminated by ``result=``.

        The positive ``result=held`` line is the half that makes the negative
        ones readable as evidence: absence of a refusal is otherwise
        indistinguishable from a proof that never ran (#2198), and the whole
        point of this seam is that a support read can tell the two apart.
        ``observed_db`` is EMPTY only when the fader could not be read — the
        one clean discriminator, and it is a real reading rather than an
        inference because :meth:`prove`'s read is unconditional (#2085).
        """
        expected = handle.level_db
        log_event(
            logger,
            "volume.claim_proof",
            level=logging.INFO if result == "held" else logging.WARNING,
            result=result,
            kind=handle.kind.value,
            expected_db="" if expected is None else f"{expected:.6f}",
            observed_db="" if observed is None else f"{observed:.6f}",
            tolerance_db=f"{self._tolerance_db:.6f}",
        )
