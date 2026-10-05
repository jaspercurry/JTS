# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Who may start one more capture, and what it costs.

This module DECIDES and does not act — the session owns every irreversible
half, so a pure gate asked the same question twice answers the same way. No
household vocabulary lives here: a refusal leaves as a kind plus an opaque
reason code and :mod:`.refusal_copy` renders the sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jasper.platform.json_fields import finite_float

from .refusal_copy import TakeCharge, TakeVerdict

__all__ = [
    "DECISION_KINDS",
    "LEVEL_RETAKES",
    "MAX_EXTRA_ATTEMPTS_PER_POSITION",
    "AttemptOverspendError",
    "BeginDecision",
    "SlotAttempts",
    "assess_begin",
]


#: A placement's takes after its free first, of every charge but a replay (ADR-0422).
MAX_EXTRA_ATTEMPTS_PER_POSITION = 2
#: The verdicts that retake at the level they name.
LEVEL_RETAKES = frozenset({"retake_louder", "retake_quieter"})


class AttemptOverspendError(RuntimeError):
    """A slot was charged an extra attempt it did not have.

    Module-local: a pure ledger has no business knowing the flow's
    ``CrossoverV2FlowError``, and the session translates at the one call site.
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
    charge: TakeCharge = "operator"
    #: The last take's fault and reading, when it was refused for a retake at its own level.
    refusal: tuple[str, float] | None = None
    #: A take repeated that refusal, so no retake here can change the answer (ADR-0428).
    repeated: bool = False

    @property
    def extras_used(self) -> int:
        return self.by_household

    def left(self) -> int:
        """Extra takes left: the placement's cap less every charged take (ADR-0422)."""
        return 0 if self.repeated else max(0, MAX_EXTRA_ATTEMPTS_PER_POSITION - self.by_household - self.by_speaker)

    def can_retry(self, charge: TakeCharge = "operator") -> bool:
        return charge == "replay" or self.left() > 0

    def can_admit(self, charge: TakeCharge) -> bool:
        return not self.admitted or self.can_retry(charge)

    def admit(self) -> None:
        """Count one admitted take: a slot's first is free, and each later one spends its charge."""
        if self.admitted:
            self.spend(self.charge)
        self.admitted += 1

    def spend(self, charge: TakeCharge) -> None:
        if not self.can_retry(charge):
            raise AttemptOverspendError("slot has no attempts left for this initiator")
        if charge == "speaker":
            self.by_speaker += 1
        elif charge != "replay":
            self.by_household += 1

    def note(self, verdict: TakeVerdict) -> None:
        """Keep a take's refusal for a retake at its own level: its fault, and its level
        reading or else its peak. One that repeats the last, its reading within
        ``SAME_POSE_DRIFT_DB``, spends the placement. A replay leaves the last as it is (ADR-0428)."""
        from .capture_dispatch import SAME_POSE_DRIFT_DB  # lazy: it loads scipy, and the arm-walk CLI imports this module
        if verdict.charge == "replay":
            return
        reading = finite_float(verdict.evidence.get("level_db_spl", verdict.evidence.get("peak_dbfs")))
        last, self.refusal = self.refusal, (
            (verdict.fault, reading) if verdict.fault and reading is not None and verdict.next not in LEVEL_RETAKES
            else None)
        if last and self.refusal and last[0] == self.refusal[0] and abs(last[1] - self.refusal[1]) <= SAME_POSE_DRIFT_DB:
            self.repeated = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "left": self.left(),
            "by_speaker": self.by_speaker,
            "by_household": self.by_household,
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
    retry_charge: TakeCharge = "operator",
) -> BeginDecision:
    """Admit (or refuse) one phone ``begin_capture`` (§5.7)."""
    if ledger is None or not ledger.admitted:
        return BeginDecision(ADMIT)
    if not ledger.can_retry(retry_charge):
        return BeginDecision(REFUSE_EXTRAS_SPENT, code=default_code)
    return BeginDecision(ADMIT)
