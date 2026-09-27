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
from typing import Any, Literal

from .refusal_copy import TakeCharge

__all__ = [
    "DECISION_KINDS",
    "MAX_EXTRA_ATTEMPTS_PER_POSITION",
    "MAX_AUTOMATIC_RETAKES_PER_POSITION",
    "AttemptOverspendError",
    "BeginDecision",
    "SlotAttempts",
    "assess_begin",
]


MAX_EXTRA_ATTEMPTS_PER_POSITION = 3
# Six extra takes per pose bound USB-fault work; planned configs/repeats spend none.
MAX_AUTOMATIC_RETAKES_PER_POSITION = 6

#: The ledger's own free charge, for a take the executor admits without spending a retry (#5722).
SlotCharge = TakeCharge | Literal["replay"]


class AttemptOverspendError(RuntimeError):
    """A slot was charged an extra attempt it did not have.

    Module-local for the reason :class:`~.programs.NoProgramForPhaseError` is:
    a pure ledger has no business knowing the flow's ``CrossoverV2FlowError``,
    and the session translates at the one call site.
    """


#: :attr:`BeginDecision.kind` — admit this begin; the ledger charges it (:meth:`SlotAttempts.admit`).
ADMIT = "admit"
#: Refuse: the slot's extras are gone (the backstop — see :func:`assess_begin`).
REFUSE_EXTRAS_SPENT = "refuse_extras_spent"

#: Every kind :func:`assess_begin` can return. Declared so the flow's handling
#: can be VERIFIED rather than trusted. The unhandled direction is REFUSE: an
#: extra take costs the household a try it may need, while a refusal costs it a
#: retry it can make again.
DECISION_KINDS = frozenset({
    ADMIT,
    REFUSE_EXTRAS_SPENT,
})


@dataclass
class SlotAttempts:
    admitted: int = 0
    by_household: int = 0
    by_speaker: int = 0
    charge: SlotCharge = "operator"
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

    def can_retry(self, charge: SlotCharge = "operator") -> bool:
        return charge == "replay" or (self.automatic_left if charge == "speaker" else self.extras_left) > 0

    def can_admit(self, charge: SlotCharge) -> bool:
        return not self.admitted or self.can_retry(charge)

    def admit(self) -> None:
        """Count one admitted take: a slot's first is free, and each later one spends its charge."""
        if self.admitted:
            self.spend(self.charge)
        self.admitted += 1

    def spend(self, charge: SlotCharge) -> None:
        if not self.can_retry(charge):
            raise AttemptOverspendError("slot has no attempts left for this initiator")
        if charge == "speaker":
            self.by_speaker += 1
        elif charge != "replay":
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

    ``code`` is an opaque reason token on every refusal.
    """

    kind: str
    code: str = ""


def assess_begin(
    *,
    ledger: SlotAttempts | None,
    default_code: str,
    retry_charge: SlotCharge = "operator",
) -> BeginDecision:
    """Admit (or refuse) one phone ``begin_capture`` (§5.7)."""
    if ledger is None or not ledger.admitted:
        return BeginDecision(ADMIT)
    if not ledger.can_retry(retry_charge):
        return BeginDecision(REFUSE_EXTRAS_SPENT, code=default_code)
    return BeginDecision(ADMIT)
