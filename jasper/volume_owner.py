# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one owner of CamillaDSP's main fader — four ranked claim kinds.

Every fader writer in a process goes through this owner, as one of four claim
kinds: **household · transient-duck · session-measurement · commissioning**.

**What "ranked" means.** Three kinds declare a LEVEL — an absolute dB the fader
should read — totally ordered household < session-measurement < commissioning,
and the highest-ranked claim held is *the level in effect*. The transient duck
declares no level: it is an ATTENUATION composing below whichever level is in
effect. A lower-ranked level claim held under a higher-ranked one is **recorded
and not written**, and is what the fader lands on when the higher claim
releases.

**One declared level.** A claim's ``level_db`` is the only seat of truth: any
level a caller derives arrives as an argument to
:meth:`VolumeOwner.acquire_level`.

**One confirm tolerance, and it is not minted here.**
:data:`~jasper.active_speaker.volume_latch.READBACK_TOLERANCE_DB` via
:func:`~jasper.active_speaker.volume_latch.fader_matches` is the repo's one
*"do these two fader dB values agree?"* test. This module consumes it.

**The 0 dB ceiling is NOT this module's.** ``devices.volume_limit`` stays
``0.0`` and ``jasper.camilla._coerce_main_volume_db`` clamps every positive
write; the owner sits BEHIND that door as its only caller, never as its
exception. It refuses only *non-finite* numbers, which is arithmetic integrity
(a NaN would poison the ``min`` below), not a safety clamp.

**The release algebra is ADR-0004's.** The one thing not stated there, because
it only arises once claims are ranked: a *level* claim's release restores the
next level OUTRIGHT, while only a duck gives back its own attenuation.

**Every settle reads first**, which keeps arbitration from churning: re-deriving
the whole target on every claim change would otherwise repeat writes CamillaDSP
ramps over 400 ms.

**Doors are injected and must not raise.** The setter and getter are the
holder's to bind, and the contract is
:data:`~jasper.active_speaker.volume_latch.FADER_IO_ERRORS`'s: report failure,
never raise a transport error. A claim's ledger entry unwinds on ANY escape, so
a holder that breaks the contract loses its claim rather than stranding one.

**In-memory, per process.** Durable volume-safety state belongs to the claim
holders that own it, and cross-daemon ordering to the leases that already
provide it.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from .active_speaker.volume_latch import (
    FADER_IO_ERRORS,
    READBACK_TOLERANCE_DB,
    fader_matches,
    read_fader_db,
    set_and_confirm_volume,
)
from .json_fields import finite_float
from .log_event import log_event

logger = logging.getLogger(__name__)

__all__ = [
    "ClaimKind",
    "VolumeClaimConflict",
    "VolumeClaimHandle",
    "VolumeClaimRefused",
    "VolumeOwner",
    "duck_release_target_db",
    "install_volume_owner",
    "volume_owner",
]

SetFaderDb = Callable[[float], Awaitable[Any]]
GetFaderDb = Callable[[], Awaitable[Any]]

#: A release that waited longer than this (seconds) for the owner's lock is
#: disclosed: a duck release runs inside a shielded ``finally`` and a stranded
#: duck is a silent speaker.
#: **Removal condition:** delete when no owner operation can hold the lock
#: across a fader round-trip — today an acquire can, bounded by
#: ``CamillaController``'s 5 s attempt budget and its one retry.
RELEASE_WAIT_DISCLOSE_S = 1.0


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
    disclosure, but a handle carries no authority of its own.
    """

    kind: ClaimKind
    token: int
    level_db: float | None = None
    depth_db: float | None = None


def duck_release_target_db(
    *, reference_db: float, current_db: float | None, depth_db: float,
) -> float:
    """Where a releasing DUCK lands the fader — ADR-0004's algebra.

    ``min(reference, current + depth)``. Both halves are load-bearing; the ADR
    records why, and the two opposite failure modes each one closes.

    An unreadable fader falls back to the reference: the level that should be
    in effect is still known, and it is never louder than the relative
    give-back would have been.
    """
    if current_db is None:
        return float(reference_db)
    return min(float(reference_db), float(current_db) + abs(float(depth_db)))


def _finite(value: Any, what: str) -> float:
    """A finite dB number, or a refusal. A ``bool`` is not a level: read as
    ``1.0`` it would be a POSITIVE level the 0 dB ceiling can never carry.
    """
    number = finite_float(value)
    if number is None:
        raise VolumeClaimRefused(f"{what} must be a finite number, got {value!r}")
    return number


def _fmt_db(value: float | None) -> str:
    """A dB field for a log line, EMPTY when there is no number.

    The empty string is the discriminator :meth:`VolumeOwner._disclose` names;
    an absent number must never render as one.
    """
    return "" if value is None else f"{value:.6f}"


class VolumeOwner:
    """Every fader write in this process, arbitrated by rank.

    Constructed with the write door and the read door as injected coroutines —
    in production ``CamillaController.set_volume_db`` and ``.get_volume_db``
    bound with ``best_effort=True``, so every write passes
    ``_coerce_main_volume_db``'s clamp and no transport error escapes into the
    arbitration. Injection rather than a controller import keeps the owner out
    of ``jasper.camilla``'s import graph.

    There is no tolerance knob: ``READBACK_TOLERANCE_DB`` is the repo's one
    confirm tolerance.
    """

    def __init__(
        self,
        *,
        set_fader_db: SetFaderDb,
        get_fader_db: GetFaderDb,
    ) -> None:
        self._set_fader_db = set_fader_db
        self._get_fader_db = get_fader_db
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

    def holds_kind(self, kind: ClaimKind) -> bool:
        """Is any claim of this kind held, by anyone?

        :meth:`holds` answers for a handle its caller already has;
        this answers for a process that has no handle and must not be given
        one. Its caller is the measurement-pause release, which needs to know
        whether a session still owns the fader without being handed authority
        over that session's claim.

        SYNCHRONOUS and non-blocking, like the readers above it: its caller
        cannot await, because the measurement-pause release runs on the REQUEST
        thread and bridges into the loop only via ``run_async``.

        That thread is why this snapshots and the readers above it do not: it is
        asked while the loop thread may be taking or releasing a claim, and
        iterating the live mapping across that raises "dictionary changed size
        during iteration". ``tuple()`` of the view is one atomic C-level copy.
        """
        return any(
            claim.kind is kind for claim in tuple(self._claims.values())
        )

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
            return await self._take(
                VolumeClaimHandle(
                    kind=kind, token=next(self._tokens), level_db=target,
                ),
                kind=kind.value,
                refusal=(
                    f"could not establish {target:.6f} dB for {kind.value}"
                ),
            )

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
            previous = self.declared_level_db()
            handle = self._replace_household_claim(target)
            if await self._establish(handle, context="declare:household"):
                return True
            # The prior household claim is already gone: a declaration
            # replaces it, never stacks. So on an unconfirmable write the owner
            # names a level the fader does not carry and the next release lands
            # on the NEW one. Disclosed rather than rolled back — restoring a
            # level nobody asked for would be the quieter mistake, not the
            # smaller one.
            log_event(
                logger,
                "volume.household_declare_unconfirmed",
                level=logging.WARNING,
                declared_db=f"{target:.6f}",
                previous_db=_fmt_db(previous),
            )
            return False

    async def relevel(
        self, handle: VolumeClaimHandle, level_db: float,
    ) -> VolumeClaimHandle:
        """Replace a held level in one settle and return its new handle.

        An incomplete move retains the original handle without another fader
        write: releasing to household volume could raise a live tone. The
        physical level may have moved before confirmation failed.
        """
        target = _finite(level_db, "level_db")
        async with self._lock:
            if self._claims.get(handle.token) != handle:
                raise VolumeClaimRefused("that claim is no longer held")
            if handle.level_db is None:
                raise VolumeClaimRefused("a duck declares a depth, not a level")
            del self._claims[handle.token]
            moved = VolumeClaimHandle(
                kind=handle.kind, token=next(self._tokens), level_db=target,
            )
            established = False
            try:
                established = await self._establish(
                    moved, context=f"relevel:{handle.kind.value}",
                )
                if not established:
                    raise VolumeClaimRefused(
                        f"could not establish {target:.6f} dB for "
                        f"{handle.kind.value}; the claim is held at "
                        f"{handle.level_db:.6f} dB"
                    )
                return moved
            finally:
                if not established:
                    self._claims.pop(moved.token, None)
                    self._claims[handle.token] = handle
                    log_event(
                        logger, "volume.relevel_unconfirmed", level=logging.ERROR,
                        kind=handle.kind.value, requested_db=f"{target:.6f}",
                        held_db=f"{handle.level_db:.6f}",
                    )

    async def acquire_duck(self, depth_db: float) -> VolumeClaimHandle:
        """Take ``depth_db`` of transient attenuation off the level in effect.

        Ducks STACK: two holders that overlap take both depths, and each gives
        back only its own. A duck over no declared level writes nothing — there
        is no level to attenuate — and that is a held claim, not a refusal.

        Fail-closed like :meth:`acquire_level`: an attenuation that could not
        be established raises and leaves no claim held, so a holder never
        believes it is ducking a speaker it did not move — a holder that
        latched on a skipped write would restore a level nothing ducked.
        """
        depth = abs(_finite(depth_db, "depth_db"))
        async with self._lock:
            return await self._take(
                VolumeClaimHandle(
                    kind=ClaimKind.TRANSIENT_DUCK,
                    token=next(self._tokens),
                    depth_db=depth,
                ),
                kind=ClaimKind.TRANSIENT_DUCK.value,
                refusal=f"could not establish {depth:.6f} dB of attenuation",
            )

    async def _establish(
        self, handle: VolumeClaimHandle, *, context: str,
    ) -> bool:
        """Seat ``handle`` and settle the fader on the new arbitration.

        The one claim-taking skeleton behind all four verbs. Caller holds the
        lock.

        ``True`` does not mean *"wrote something"*: a level claim outranked by a
        higher one is in effect the moment it is RECORDED, and recording cannot
        fail, so such a claim is never refused for a write it never wanted.

        The rank short-circuit is a LEVEL claim's alone: a duck declares no
        level and always composes.

        On failure the ledger KEEPS ``handle`` — the callers give it back two
        different ways, and :meth:`relevel`'s is not an unwind.
        """
        self._claims[handle.token] = handle
        is_level = handle.level_db is not None
        if is_level and self._top_level_claim() is not handle:
            return True
        return await self._apply(context=context)

    async def _take(
        self, handle: VolumeClaimHandle, *, kind: str, refusal: str,
    ) -> VolumeClaimHandle:
        """Establish a claim fail-CLOSED: what cannot be established is not held.

        The ``taken`` flag guards a ``finally``, so :meth:`_unwind` runs on
        EVERY exit that is not a completed take — the refusal raised here, a
        cancellation, or a raise from the injected door.
        """
        taken = False
        try:
            taken = await self._establish(handle, context=f"acquire:{kind}")
            if not taken:
                raise VolumeClaimRefused(refusal)
            return handle
        finally:
            if not taken:
                await self._unwind(handle, context=f"acquire_failed:{kind}")

    async def _unwind(
        self, handle: VolumeClaimHandle, *, context: str,
    ) -> None:
        """Take a half-taken claim out of the ledger and hand the fader back.

        Reached from a ``finally`` guarded by a success flag, so it runs on
        EVERY exit an acquire did not complete — a refusal, a cancellation, or a
        raise from the injected door. That last one this owner cannot prevent:
        ``CamillaUnavailable`` is not in
        :data:`~jasper.active_speaker.volume_latch.FADER_IO_ERRORS`, and naming
        it would mean importing ``jasper.camilla``, which imports this module.
        The pop is synchronous and cannot fail, so the ledger is correct before
        anything is awaited.

        The fader hand-back is best-effort: if it is cancelled or the door raises
        again, the ledger is already right and the next claim settles. It must
        never mask the exception being unwound.
        """
        self._claims.pop(handle.token, None)
        try:
            await self._apply(context=context)
        except FADER_IO_ERRORS:
            log_event(
                logger,
                "volume.claim_unwind_incomplete",
                level=logging.WARNING,
                context=context,
                kind=handle.kind.value,
            )

    async def release(
        self,
        handle: VolumeClaimHandle,
        *,
        household_level_db: float | None = None,
    ) -> None:
        """Give a claim back. Idempotent, and a no-op against nothing held.

        Called on every path out of a holder's lifetime, including after an
        acquire that raised and again if a first release raised — the same
        contract ``session_seams.VolumeClaim.release`` states.

        ``household_level_db`` re-declares the standing level as PART of this
        release, inside the same lock and before the single settle. A holder
        whose reference can move while it holds — the voice duck reads the
        coordinator's target fresh, because another daemon may have written it
        — must land the new level in one write. Declaring first and releasing
        second costs two, and the intermediate one is audible: a duck of 25 dB
        over a level that moved by 2 dB would dip a further 25 dB before coming
        back up. ``None`` leaves the standing level exactly as it was.
        """
        waited_from = time.monotonic()
        async with self._lock:
            waited_s = time.monotonic() - waited_from
            if waited_s > RELEASE_WAIT_DISCLOSE_S:
                # A release waits for the lock rather than taking a fast path
                # around it: a lock-free own-depth give-back would be a second
                # writer. The cost is disclosed rather than hidden, because a
                # duck release runs inside a shielded `finally` and a stranded
                # duck is a silent speaker.
                log_event(
                    logger,
                    "volume.claim_release_waited",
                    level=logging.WARNING,
                    kind=handle.kind.value,
                    waited_s=f"{waited_s:.3f}",
                )
            if self._claims.get(handle.token) != handle:
                return
            # Validated BEFORE the ledger changes: a refusal here must leave
            # the caller still holding its claim. Validating after the `del`
            # strands a ducked speaker — the holder believes it released, and
            # its own restore has no claim left to give the attenuation back.
            # After the held-check, though, so an idempotent no-op release
            # stays a no-op rather than starting to raise.
            household = (
                None if household_level_db is None
                else _finite(household_level_db, "household_level_db")
            )
            del self._claims[handle.token]
            if household is not None:
                self._replace_household_claim(household)
            reference = self.declared_level_db()
            if reference is None:
                await self._disclose_release_without_level(handle)
                return
            settled = reference - self.duck_depth_db()
            if handle.kind is ClaimKind.TRANSIENT_DUCK:
                settled = duck_release_target_db(
                    reference_db=settled,
                    current_db=await self._read(),
                    depth_db=handle.depth_db or 0.0,
                )
            # This read and :meth:`_settle`'s are NOT one question asked
            # twice: they are separated by a round-trip and the fader is shared
            # across daemons, so another writer can land a value while the first
            # read is in flight.
            await self._settle(
                settled, context=f"release:{handle.kind.value}",
            )

    # ---- proving a level is in effect -------------------------------------

    async def prove(self, handle: VolumeClaimHandle) -> float | None:
        """The fader reading, but only when it AGREES with this claim's level.

        ``None`` means *not proven*, which refuses to BANK a capture — never to
        play the stimulus and never to try again. Returning a drifted reading
        instead would hand a caller a number to stamp into a record while the
        speaker played at a different one.

        ``None`` for every way a level can fail to be in effect: the claim was
        released, a higher-ranked claim preempted it, a duck is down over it,
        the fader could not be read, or the reading disagrees. Never gated on a
        diagnostics flag (ADR-0009) — the excitation-safety ledger admitted the
        program against the declared level, so this is that ledger's own
        integrity rather than samplable forensics.

        **The read is UNCONDITIONAL**, taken before any verdict is decided, so
        ``observed_db`` is a real observation on every line: short-circuiting a
        refusal ahead of the read would report "the fader could not be read" for
        a case where it was never asked.

        **Under the owner's lock, for the whole body.** The read is an await, and
        without the lock a duck acquired while it was in flight would land
        between the reading and the verdict, which would then pass a PRE-duck
        number that agrees with the level while the speaker plays ducked.
        """
        async with self._lock:
            expected = handle.level_db
            observed = await self._read()
            if expected is None or self._claims.get(handle.token) != handle:
                result = "unheld"
            elif self._top_level_claim() is not handle:
                result = "preempted"
            elif not fader_matches(
                observed, expected, tolerance_db=READBACK_TOLERANCE_DB,
            ):
                result = "refused"
            else:
                self._disclose(handle, result="held", observed=observed)
                return observed
            self._disclose(handle, result=result, observed=observed)
            return None

    # ---- internals --------------------------------------------------------

    def _replace_household_claim(self, level_db: float) -> VolumeClaimHandle:
        """Seat the standing level. Replaces, never stacks — it is one fact.

        Caller holds the lock, and settles afterwards: this only moves the
        claim, so a release can re-declare and settle in one write.
        """
        for token, claim in list(self._claims.items()):
            if claim.kind is ClaimKind.HOUSEHOLD:
                del self._claims[token]
        handle = VolumeClaimHandle(
            kind=ClaimKind.HOUSEHOLD,
            token=next(self._tokens),
            level_db=level_db,
        )
        self._claims[handle.token] = handle
        return handle

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

        READ, then delegate to
        :func:`~jasper.active_speaker.volume_latch.set_and_confirm_volume`. The
        pre-read earns two things:

        **Arbitration is not churn.** Every claim change re-derives the whole
        target, so a household level re-declared under a held duck, or a release
        landing where the fader already sits, would otherwise repeat a write
        CamillaDSP ramps over 400 ms.

        **Drift is repaired, not patrolled for.** The pre-read puts back a fader
        that drifted off a level nobody re-declared, so skipping a redundant
        write never means skipping the check — which is why no periodic
        reconciler is needed.
        """
        target = float(target_db)
        if fader_matches(
            await self._read(), target, tolerance_db=READBACK_TOLERANCE_DB,
        ):
            return True
        confirmed = await set_and_confirm_volume(
            target,
            self._set_fader_db,
            self._get_fader_db,
            tolerance_db=READBACK_TOLERANCE_DB,
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
        return await read_fader_db(self._get_fader_db)

    async def _disclose_release_without_level(
        self, handle: VolumeClaimHandle,
    ) -> None:
        """A claim came back and nothing declares where the fader belongs.

        The owner writes nothing — with no level claim it has no target — but
        states the reading and the depth just given up, so a fader parked far
        from anything does not go unnoticed.
        """
        observed = await self._read()
        log_event(
            logger,
            "volume.claim_released_without_level",
            level=logging.WARNING,
            kind=handle.kind.value,
            observed_db=_fmt_db(observed),
            depth_db=_fmt_db(handle.depth_db),
        )

    def _disclose(
        self,
        handle: VolumeClaimHandle,
        *,
        result: str,
        observed: float | None,
    ) -> None:
        """One vocabulary, one question, discriminated by ``result=``.

        The positive ``result=held`` line is what makes the negative ones
        readable as evidence: absence of a refusal is otherwise
        indistinguishable from a proof that never ran. ``observed_db`` is EMPTY
        only when the fader could not be read.
        """
        expected = handle.level_db
        log_event(
            logger,
            "volume.claim_proof",
            level=logging.INFO if result == "held" else logging.WARNING,
            result=result,
            kind=handle.kind.value,
            expected_db=_fmt_db(expected),
            observed_db=_fmt_db(observed),
            tolerance_db=f"{READBACK_TOLERANCE_DB:.6f}",
        )


# ---------------------------------------------------------------------------
# The process registration, for the processes that have nowhere to inject from
# ---------------------------------------------------------------------------

_process_owner: VolumeOwner | None = None


def install_volume_owner(owner: VolumeOwner | None) -> None:
    """Register this process's fader owner. ``None`` clears it.

    **Two ways a holder reaches the owner.** A process that builds a long-lived
    ``VolumeCoordinator`` hands that coordinator's ``volume_owner`` straight to
    its holders. A process that builds no such coordinator has nothing to inject
    from — the ``/sound/`` floor-tone audition, the crossover level lease and
    the measurement volume guard run inside socket-activated wizards whose
    request handlers are reached from a router. This is the same split
    ``jasper.camilla.set_canonical_target_db_provider`` draws, for the same
    processes.

    ``tests/conftest.py``'s ``_isolate_process_volume_owner`` puts a
    registration back between tests.

    **The registration is THE owner for its process, not one of several.** A
    process that registers must not also construct a second owner, and a process
    that injects registers the same instance it injects, so
    :func:`volume_owner` never answers ``None`` where an owner exists.
    """
    global _process_owner
    _process_owner = owner


def volume_owner() -> VolumeOwner | None:
    """This process's fader owner, or ``None`` where none was registered.

    ``None`` is an honest answer, not a hole to plug: constructing an owner on
    the spot would make it the second one over the fader.
    """
    return _process_owner
