# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Who may start one more capture, and what it costs.

This module DECIDES and does not act — the session owns every irreversible
half, so a pure gate asked the same question twice answers the same way. No
household vocabulary lives here: a refusal leaves as a kind plus an opaque
reason code and :mod:`.refusal_copy` renders the sentence. Bounded-retry
ruling #2086, recorded in
docs/historical/crossover-measurement-v2-campaign-record.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Container

from .refusal_copy import TakeCharge

__all__ = [
    "ATTEMPT_INITIATOR_HOUSEHOLD",
    "ATTEMPT_INITIATOR_SPEAKER",
    "DECISION_KINDS",
    "MAX_EXTRA_ATTEMPTS_PER_POSITION",
    "MAX_AUTOMATIC_RETAKES_PER_POSITION",
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
    "extras_spent_message",
    "pilot_heard_for",
    "reflection_measured_for",
    "settle_group_position",
    "settle_spent_slot",
    "spent_slot_outcome",
]


MAX_EXTRA_ATTEMPTS_PER_POSITION = 3
# Six extra takes per pose bound USB-fault work; planned configs/repeats spend none.
MAX_AUTOMATIC_RETAKES_PER_POSITION = 6

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
    REFUSE_NON_RETRIABLE,
    REFUSE_EXTRAS_SPENT,
})


@dataclass
class SlotAttempts:
    admitted: int = 0
    by_household: int = 0
    by_speaker: int = 0
    charge: TakeCharge = "operator"
    retries_per_pose: int = MAX_EXTRA_ATTEMPTS_PER_POSITION

    @property
    def extras_used(self) -> int:
        return self.by_household

    @property
    def extras_left(self) -> int:
        return max(0, min(self.retries_per_pose - self.by_household, self.automatic_left))

    @property
    def automatic_left(self) -> int:
        return max(0, MAX_AUTOMATIC_RETAKES_PER_POSITION - self.by_household - self.by_speaker)

    def can_retry(self, charge: TakeCharge = "operator") -> bool:
        return (self.automatic_left if charge == "speaker" else self.extras_left) > 0

    def spend(self, charge: TakeCharge) -> None:
        if not self.can_retry(charge):
            raise AttemptOverspendError("slot has no attempts left for this initiator")
        if charge == "speaker":
            self.by_speaker += 1
        else:
            self.by_household += 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.retries_per_pose,
            "left": self.extras_left,
            "by_speaker": self.by_speaker,
            "by_household": self.by_household,
            "automatic_left": self.automatic_left,
            "automatic_allowed": MAX_AUTOMATIC_RETAKES_PER_POSITION,
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


def extras_spent_message(
    ledger: SlotAttempts, *, diagnosis: str, outcome: str,
) -> str:
    """The household sentence for a position whose extras are gone.

    Deliberately does NOT reuse the full registry ``message``: retriable rows
    end by inviting an action the flow will no longer grant.
    """
    used = ledger.by_household + ledger.by_speaker
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
    ledger: SlotAttempts | None,
    last_reason: str | None,
    non_retriable: Container[str],
    default_code: str,
) -> BeginDecision:
    """Admit (or refuse) one phone ``begin_capture`` (§5.7).

    Neither closing condition normally arrives here — both are settled at the
    REJECTION that closed the slot (#2086 item 3, ADR-0227). :data:`REFUSE_EXTRAS_SPENT`
    and :data:`REFUSE_NON_RETRIABLE` are the backstops for a begin that reaches
    a settled slot anyway, and the ``code`` on the former is the condition
    actually observed at this slot, never a generic exhaustion code that would
    erase what went wrong.
    """
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
    if not ledger.can_retry():
        return BeginDecision(REFUSE_EXTRAS_SPENT, code=last_reason or default_code)
    return BeginDecision(
        ADMIT,
        spends_extra=True,
        initiator=ATTEMPT_INITIATOR_SPEAKER if ledger.charge == "speaker" else ATTEMPT_INITIATOR_HOUSEHOLD,
    )


# The other half of the bounded-retry ruling — see ADR-0227 (#2086 item 3).
# :func:`assess_begin` answers "may one more capture start"; this answers
# "this take was rejected — is there an honest next take, or is this the
# outcome". Two conditions close a slot: the meter running out, and a
# rejection naming a condition no further take can clear.

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
    if ledger is None or ledger.can_retry():
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
