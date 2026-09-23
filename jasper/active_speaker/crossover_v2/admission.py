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
from typing import Any, Container

from .refusal_copy import TakeCharge

__all__ = [
    "ATTEMPT_INITIATOR_HOUSEHOLD",
    "ATTEMPT_INITIATOR_SPEAKER",
    "DECISION_KINDS",
    "MAX_EXTRA_ATTEMPTS_PER_POSITION",
    "MAX_AUTOMATIC_RETAKES_PER_POSITION",
    "AttemptOverspendError",
    "BeginDecision",
    "SlotAttempts",
    "assess_begin",
    "extras_spent_message",
    "pilot_heard_for",
    "reflection_measured_for",
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
#: can be VERIFIED rather than trusted. The unhandled direction is REFUSE: an
#: extra take costs the household a try it may need, while a refusal costs it a
#: retry it can make again.
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
    retry_charge: TakeCharge = "operator",
) -> BeginDecision:
    """Admit (or refuse) one phone ``begin_capture`` (§5.7).

    The ``code`` on :data:`REFUSE_EXTRAS_SPENT` is the condition actually
    observed at this slot, never a generic exhaustion code that would erase
    what went wrong.
    """
    if ledger is None or not ledger.admitted:
        return BeginDecision(ADMIT)
    # The ``is not None`` half narrows the type and changes no answer: the flow
    # passes a ``frozenset[str]``, in which ``None`` is never a member.
    if last_reason is not None and last_reason in non_retriable:
        # Not exhaustion — a condition another take cannot clear, whose own copy
        # already names the one action that helps.
        return BeginDecision(REFUSE_NON_RETRIABLE, code=last_reason)
    if not ledger.can_retry(retry_charge):
        return BeginDecision(REFUSE_EXTRAS_SPENT, code=last_reason or default_code)
    return BeginDecision(
        ADMIT,
        spends_extra=True,
        initiator=ATTEMPT_INITIATOR_SPEAKER if ledger.charge == "speaker" else ATTEMPT_INITIATOR_HOUSEHOLD,
    )
