# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Who may start one more capture, and what it costs (#2291 Phase 5a-vi).

The meter in front of a capture: one prompted position gets its planned
capture plus at most :data:`MAX_EXTRA_ATTEMPTS_PER_POSITION` extras, pooled
across everyone who can ask (bounded-retry ruling #2086, recorded in
docs/historical/crossover-measurement-v2-campaign-record.md). This module
DECIDES and does not act — the session owns every irreversible half, so a
pure gate asked the same question twice answers the same way. No household
vocabulary lives here: a refusal leaves as a kind plus an opaque reason code
and :mod:`.refusal_copy` renders the sentence. Registry projections and the
apply-failure probe arrive as stated arguments, the probe as a callable so
it is invoked only on the hold branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Container

__all__ = [
    "ATTEMPT_INITIATOR_HOUSEHOLD",
    "ATTEMPT_INITIATOR_SPEAKER",
    "DECISION_KINDS",
    "MAX_EXTRA_ATTEMPTS_PER_POSITION",
    "SETTLE_BELOW_POSITION_FLOOR",
    "SETTLE_CONDITION_NOT_RETRIABLE",
    "SETTLE_GROUP_CLOSE_REQUIRED",
    "SETTLE_GROUP_KINDS",
    "SETTLE_KEPT_EARLIER_TAKE",
    "SETTLE_KINDS",
    "SETTLE_PHASE_CANNOT_PROCEED",
    "SETTLE_POSITION_UNRESOLVED",
    "SETTLE_RETRY_REMAINS",
    "SETTLE_SLOT_KINDS",
    "AttemptOverspendError",
    "BeginDecision",
    "SlotAttempts",
    "assess_begin",
    "extra_initiator",
    "extras_spent_message",
    "pilot_heard_for",
    "reflection_measured_for",
    "settle_group_position",
    "settle_spent_slot",
    "spent_slot_outcome",
]


# Bounded-retry ruling #2086: one prompted position gets its PLANNED capture
# plus at most this many EXTRA attempts, POOLED across everyone who can ask
# — the household's "Try again" and voluntary retakes, and the session's own
# geometry retakes. Deliberately NOT derived from ``ReasonSpec.retry_budget``:
# the bound belongs to the position and the household's patience, not to
# whichever condition happened to fire last.
MAX_EXTRA_ATTEMPTS_PER_POSITION = 3

# Who asked for one extra attempt. Pooled against the single bound above, but
# recorded separately so the count the household reads is truthful about who
# spent what. Observed at the REJECTION that kept the plan alive, never at
# the relay's ``retake`` flag: a geometry rung rejects a good capture to hold
# the runner on the same index, so it travels with ``retake=false``.
ATTEMPT_INITIATOR_HOUSEHOLD = "household"
ATTEMPT_INITIATOR_SPEAKER = "speaker"


class AttemptOverspendError(RuntimeError):
    """A slot was charged an extra attempt it did not have.

    Module-local for the reason :class:`~.programs.NoProgramForPhaseError` is:
    a pure ledger has no business knowing the flow's ``CrossoverV2FlowError``,
    and the session translates at the one call site.
    """


#: :attr:`BeginDecision.kind` — admit this begin (``spends_extra`` says whether
#: it costs one of the position's extras, and ``initiator`` who is charged).
ADMIT = "admit"
#: Hold the begin: VERIFY is soft-held until an apply is observed.
DEFER_AWAITING_APPLY = "defer_awaiting_apply"
#: Refuse: the auto-apply hit a TERMINAL failure, named by ``code``.
REFUSE_APPLY_FAILED = "refuse_apply_failed"
#: Refuse: the slot's last rejection was a condition another take cannot clear.
REFUSE_NON_RETRIABLE = "refuse_non_retriable"
#: Refuse: the slot's extras are gone (the backstop — see :func:`assess_begin`).
REFUSE_EXTRAS_SPENT = "refuse_extras_spent"

#: Every kind :func:`assess_begin` can return. Declared so the flow's handling
#: can be VERIFIED rather than trusted — the discipline
#: :data:`.spatial.SCREEN_KINDS` and :data:`.coordinator.REFUSAL_KINDS`
#: already keep. The unhandled direction is REFUSE: an extra take costs the
#: household a try it may need, while a refusal costs it a retry it can make
#: again.
DECISION_KINDS = frozenset({
    ADMIT,
    DEFER_AWAITING_APPLY,
    REFUSE_APPLY_FAILED,
    REFUSE_NON_RETRIABLE,
    REFUSE_EXTRAS_SPENT,
})


@dataclass
class SlotAttempts:
    """One prompted position's attempt ledger (#2086).

    ``admitted`` counts every attempt the session let start; the first is the
    PLANNED capture and is free, and each one after it spends an extra against
    :data:`MAX_EXTRA_ATTEMPTS_PER_POSITION`, attributed to whoever asked. An
    ACCEPTED capture consumes no budget of its own, so a position measured
    cleanly on the first take still has its full three extras. Mutable on
    purpose: this is per-session state the session advances.
    """

    admitted: int = 0
    by_household: int = 0
    by_speaker: int = 0

    @property
    def extras_used(self) -> int:
        return self.by_household + self.by_speaker

    @property
    def extras_left(self) -> int:
        return max(0, MAX_EXTRA_ATTEMPTS_PER_POSITION - self.extras_used)

    def spend(self, initiator: str) -> None:
        """Charge one extra attempt to ``initiator``.

        Callers gate on :attr:`extras_left` first; an unchecked overspend raises
        rather than silently capping.
        """
        if self.extras_left <= 0:
            raise AttemptOverspendError(
                "slot has no extra attempts left "
                f"({self.extras_used}/{MAX_EXTRA_ATTEMPTS_PER_POSITION})"
            )
        if initiator == ATTEMPT_INITIATOR_SPEAKER:
            self.by_speaker += 1
        else:
            self.by_household += 1

    def to_payload(self) -> dict[str, Any]:
        """The honest count, as the phone renders it.

        Numbers only — the page composes the eyebrow, because the §2.1 screen
        grammar makes the counter the page's slot. ``by_speaker`` is what makes
        the count truthful about who spent what.
        """
        return {
            "used": self.extras_used,
            "allowed": MAX_EXTRA_ATTEMPTS_PER_POSITION,
            "left": self.extras_left,
            "by_speaker": self.by_speaker,
            "by_household": self.by_household,
        }


@dataclass(frozen=True)
class BeginDecision:
    """What :func:`assess_begin` concluded about one ``begin_capture``.

    ``code`` is an opaque reason token on every refusal. ``spends_extra`` and
    ``initiator`` are meaningful only on :data:`ADMIT`, and the session
    performs the charge.
    """

    kind: str
    code: str = ""
    spends_extra: bool = False
    initiator: str = ""


def extra_initiator(last_reason: str | None, *, geometry_locked_code: str) -> str:
    """Who is asking for the extra attempt about to be admitted.

    Read off the rejection that kept the plan alive, the only place the
    distinction is visible: a geometry rung is the session demanding a wider
    take of an otherwise fine capture, and it travels the ordinary begin path
    with ``retake=false`` (rejecting is the only lever that holds a
    fixed-length plan on the same index). Everything else is the household
    choosing to spend one. ``geometry_locked_code`` is stated rather than
    imported: the reason codes are the flow's.
    """
    return (
        ATTEMPT_INITIATOR_SPEAKER
        if last_reason == geometry_locked_code
        else ATTEMPT_INITIATOR_HOUSEHOLD
    )


def extras_spent_message(
    ledger: SlotAttempts, *, diagnosis: str, outcome: str,
) -> str:
    """The household sentence for a position whose extras are gone.

    Deliberately does NOT reuse the full registry ``message``: retriable rows
    end by inviting an action the flow will no longer grant.
    """
    used = ledger.extras_used
    tries = "try" if used == 1 else "tries"
    count = (
        f"JTS measured this spot {ledger.admitted} times — the planned one "
        f"plus {used} extra {tries} — and still could not get a clean read."
    )
    return " ".join(part for part in (diagnosis, count, outcome) if part)


def spent_slot_outcome(
    *,
    is_group: bool,
    index: int,
    unresolved: Container[int],
    retained: Container[int],
) -> str:
    """The state after an exhausted slot, derived from session state.

    The three facts arrive stated; the session reads them off
    ``_group_unresolved`` and ``_retained_group_indexes``, which remain its own.
    """
    if is_group:
        if index in unresolved:
            return "This position was left out and the group continued."
        if index in retained:
            return (
                "JTS kept the earlier measurement for this position and "
                "the group continued."
            )
        return (
            "The measurement cannot continue because too few positions "
            "produced a clean read."
        )
    return "The measurement cannot continue because this step needs a clean read."


def pilot_heard_for(
    code: str | None, paired: tuple[str, bool | None, bool | None] | None,
) -> bool | None:
    """The pilot evidence recorded WITH ``code``, else ``None`` (#2085).

    ``paired`` is the ``(code, pilot_heard, reflection_measured)`` triple the
    session holds for the position being described. The code is re-checked
    because the failure being described is not always the one last consumed —
    the flow's ``_refuse`` can name a code the capture loop never produced, and
    a replayed begin can address an older slot — and attaching one capture's
    evidence to another's code would put a confident, wrong sentence in front
    of a household.
    """
    if code is None or paired is None or paired[0] != code:
        return None
    return paired[1]


def reflection_measured_for(
    code: str | None, paired: tuple[str, bool | None, bool | None] | None,
) -> bool | None:
    """The gate discriminator recorded with ``code`` at this position."""
    if code is None or paired is None or paired[0] != code:
        return None
    return paired[2]


def assess_begin(
    *,
    verify_hold: bool,
    apply_failure_code: Callable[[], str],
    ledger: SlotAttempts | None,
    last_reason: str | None,
    non_retriable: Container[str],
    default_code: str,
    geometry_locked_code: str,
) -> BeginDecision:
    """Admit (or defer / refuse) one phone ``begin_capture`` (§5.7).

    ``verify_hold`` is the session's "this is VERIFY and no apply has been
    observed" — VERIFY is soft-held until one is. No shipped session reaches
    that hold since the two-stage split (work order D10): stage 1 has no VERIFY
    index and stage 2's session is constructed ``applied=True``, so no new
    design may depend on it. A TERMINAL auto-apply failure refuses outright
    rather than holding toward a dishonest capture_timeout.

    Neither closing condition normally arrives here (#2086): both are settled
    at the REJECTION that closed the slot, so a household is never handed a
    "try again" screen whose button is about to end the session.
    :data:`REFUSE_EXTRAS_SPENT` and :data:`REFUSE_NON_RETRIABLE` are the
    backstops for a begin that reaches a settled slot anyway, and the ``code``
    on the former is the condition actually observed at this slot, never a
    generic exhaustion code that would erase what went wrong.
    """
    if verify_hold:
        failure_code = apply_failure_code()
        if failure_code:
            return BeginDecision(REFUSE_APPLY_FAILED, code=failure_code)
        return BeginDecision(DEFER_AWAITING_APPLY)
    # ONE pooled meter per slot: the planned capture, then at most
    # MAX_EXTRA_ATTEMPTS_PER_POSITION extras, whoever asks for them. The first
    # attempt of any slot is always admitted and always free, and nothing is
    # charged before the answer is ADMIT, so the hold above leaves no ledger
    # entry behind. Both halves state this function's PRECONDITION: "no attempts
    # yet" is expressible as no ledger at all or as a ledger with
    # ``admitted == 0``, and both must mean a free first attempt.
    if ledger is None or not ledger.admitted:
        return BeginDecision(ADMIT)
    # The ``is not None`` half narrows the type and changes no answer: the flow
    # passes a ``frozenset[str]``, in which ``None`` is never a member.
    if last_reason is not None and last_reason in non_retriable:
        # Not exhaustion — a condition another take cannot clear, whose own copy
        # already names the one action that helps. Reaching this means a begin
        # outran the terminal verdict :data:`SETTLE_CONDITION_NOT_RETRIABLE`,
        # which names the same code, so the two accounts agree.
        return BeginDecision(REFUSE_NON_RETRIABLE, code=last_reason)
    if ledger.extras_left <= 0:
        return BeginDecision(REFUSE_EXTRAS_SPENT, code=last_reason or default_code)
    return BeginDecision(
        ADMIT,
        spends_extra=True,
        initiator=extra_initiator(
            last_reason, geometry_locked_code=geometry_locked_code
        ),
    )


# The other half of the bounded-retry ruling (#2086 item 3).
# :func:`assess_begin` answers "may one more capture start"; this answers
# "this take was rejected — is there an honest next take, or is this the
# outcome". The answer belongs at the verdict rather than at the next
# begin, so a household is never shown a retry screen whose button only
# leads to a pre-play refusal. TWO conditions close a slot and the begin
# gate refuses on both, so both settle here or the same lie returns by a
# second door: the meter running out, and a rejection naming a condition
# no further take can clear.

#: The slot still has extras. Nothing settles; the household retries as before.
SETTLE_RETRY_REMAINS = "retry_remains"
#: This rejection named a condition another take cannot clear, so the tries the
#: meter still shows are tries the begin gate would refuse. Outranks every rung
#: below — see :func:`settle_spent_slot`.
SETTLE_CONDITION_NOT_RETRIABLE = "condition_not_retriable"
#: A single-capture phase with nothing left to spend: this take is the last word.
SETTLE_PHASE_CANNOT_PROCEED = "phase_cannot_proceed"
#: A position group — the outcome needs the group's own lock-guarded facts, so
#: the ladder continues in :func:`settle_group_position` under the caller's
#: close lock. Not an outcome: the one kind that names a rung rather than an end.
SETTLE_GROUP_CLOSE_REQUIRED = "group_close_required"
#: An earlier take of this position is still standing — nothing was lost.
SETTLE_KEPT_EARLIER_TAKE = "kept_earlier_take"
#: Too few curves in hand and too few positions left to reach the floor.
SETTLE_BELOW_POSITION_FLOOR = "below_position_floor"
#: Drop this position, record the observed condition against it, and advance.
SETTLE_POSITION_UNRESOLVED = "position_unresolved"

#: What :func:`settle_spent_slot` can answer — the rungs decided before the
#: caller takes its close lock.
SETTLE_SLOT_KINDS = frozenset({
    SETTLE_RETRY_REMAINS,
    SETTLE_CONDITION_NOT_RETRIABLE,
    SETTLE_PHASE_CANNOT_PROCEED,
    SETTLE_GROUP_CLOSE_REQUIRED,
})

#: What :func:`settle_group_position` can answer — the three outcomes only a
#: position group can produce, decided under that lock.
SETTLE_GROUP_KINDS = frozenset({
    SETTLE_KEPT_EARLIER_TAKE,
    SETTLE_BELOW_POSITION_FLOOR,
    SETTLE_POSITION_UNRESOLVED,
})

#: Every kind the settle ladder can answer with. The split is the LOCK
#: boundary, not a second decision, but the two halves partition it and the
#: partition is load-bearing: a group kind arriving from the first half is
#: as much a wiring defect as an undeclared one.
SETTLE_KINDS = SETTLE_SLOT_KINDS | SETTLE_GROUP_KINDS


def settle_spent_slot(
    *,
    ledger: SlotAttempts | None,
    is_group: Callable[[], bool],
    code: str | None = None,
    non_retriable: Container[str] = frozenset(),
) -> str:
    """Does this rejection settle the position, and can it settle alone?

    **The first rung is the CONDITION, not the meter** — the same precedence
    :func:`assess_begin` keeps. A rejection whose code is non-retriable is
    settled however many extras the slot still has, because the next begin
    would refuse it, and leaving it retryable puts a "Try again" button in
    front of a household with "3 left" printed beside it.

    Then the meter. A slot with extras left (or no meter yet) is not settled
    at all. Once they are gone a single-capture phase is decided right here,
    while a position group's outcome depends on facts that are only true while
    its close lock is held, so it answers :data:`SETTLE_GROUP_CLOSE_REQUIRED`
    and the caller continues into :func:`settle_group_position` under that
    lock. Splitting the ladder there is the lock boundary and nothing else.

    ``code`` and ``non_retriable`` are stated by the flow — the reason codes
    are its — and default to "nothing observed" so the meter rungs can still be
    asked in isolation. ``is_group`` is a callable for call count: it reaches
    the journey plan, and the shipped flow does not ask it while a slot still
    has retries to offer.
    """
    if code is not None and code in non_retriable:
        return SETTLE_CONDITION_NOT_RETRIABLE
    if ledger is None or ledger.extras_left > 0:
        return SETTLE_RETRY_REMAINS
    return (
        SETTLE_GROUP_CLOSE_REQUIRED if is_group()
        else SETTLE_PHASE_CANNOT_PROCEED
    )


def settle_group_position(
    *,
    index: int,
    retained: Collection[int],
    floor: int,
    unwalked_count: Callable[[], int],
) -> str:
    """The outcome for a spent position of a group — the ladder's last three rungs.

    In order, and the order is the point:

    1. **An earlier take is still standing.** A rejection never replaces a
       retained curve, so nothing was lost. Asked FIRST, because a position
       with a curve in hand must never be counted as a loss against the floor.
    2. **The group can no longer reach its floor.** Curves in hand PLUS the
       positions the household has not walked yet — never the count so far,
       which would make the answer depend on walk order.
    3. **Otherwise, drop it and carry on.** Below the declared plan length the
       claim is degraded and disclosed, not refused.

    ``retained`` is a collection because both rungs read it, for membership and
    for size. ``floor`` is a plain value: a total function of the phase alone,
    so resolving it eagerly can neither raise nor be observed.
    ``unwalked_count`` is a callable for the same call-count reason
    ``is_group`` is, rung 2 being asked only of a position rung 1 did not
    answer for. Called with the caller's close lock held.
    """
    if index in retained:
        return SETTLE_KEPT_EARLIER_TAKE
    if len(retained) + unwalked_count() < floor:
        return SETTLE_BELOW_POSITION_FLOOR
    return SETTLE_POSITION_UNRESOLVED
