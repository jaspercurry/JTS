# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A previous session's bank, read back — the "before" a candidate check needs.

Wave 2's first decision, which :mod:`.session_seams` deferred to it in as many
words: *"rebuilding a session over a previous bank is wave 2's first decision …
what ships here is the read door an offline ``analyze`` needs; who constructs
the session around an existing bank, and how it re-learns what that bank
already discloses, is designed there."*

**The shape, in one sentence: a prior bank is ``save``'s inverse.**
:meth:`~.session.TuningSession.save` writes five keys — session id, declared
level, graph fingerprint, banked record ids, disclosures — and this reads
exactly those five back, plus the one fact derived from them that a
candidate-check needs: **which banked record is the "before".**

**Why a session cannot just be re-opened.** A tuning round crosses a process
boundary and an irreversible act: stage 1 measures the entry baseline
immediately before the household applies, the apply/rollback transaction runs
(§3's one *"not a target. Ever."*), and stage 2 measures again and grades. The
two sides are two sessions on two graphs, and the second one's honest
relationship to the first is *"I read its bank"* — never *"I am it."* That is
why :class:`~.session.TuningSession` refuses to re-open and why this is a
separate, read-only type rather than a constructor flag.

**Read-only, and that is the whole safety property.** Nothing here writes, and
the bank it reads is not this session's to bank into. The defect this replaces
is the opposite shape: today the only durable copy of the entry-baseline curve
lives in ``/var/lib/jasper/active_speaker_crossover_v2_state.json``, whose
``verify_priors`` dict is *rebuilt from the conductor on every persist*, so a
stage-2 write destroys the "before" unless the host remembers to seed it back
in (fragment ``02``'s duplication #2).

**Measurement management is NOT here** (§0's non-goals). Listing sessions,
browsing them, deleting them, and deciding what a bank's retention is are
explicitly future scope. This finds one prior bank by the id its ``save``
returned, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from .contracts import MEASURE_KIND_BASELINE
from .measure_spec import CapabilityStub, stub_for_code

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session_seams import RecordStore

__all__ = ["PriorBank"]


@dataclass(frozen=True)
class PriorBank:
    """One previous session's banked evidence, and the store that holds it.

    Constructed by :meth:`read` and never by hand: the fields are what a
    ``save`` wrote, so minting one from literals would assert a bank exists
    that does not.

    ``baseline_record_id`` is the LAST record of kind
    :data:`~.contracts.MEASURE_KIND_BASELINE` in the bank, and ``""`` when the
    bank holds none. Last rather than first, because the entry baseline's whole
    justification is *"immediately before apply"* — every capture that followed
    it would be room, microphone and household drift landing inside the
    before→after bracket — so the baseline nearest the end of the session is
    the one nearest the act being graded.

    ``measurement_level_db`` and ``graph_fingerprint`` are carried because a
    before→after claim is only as good as the drive level and the graph
    matching on both sides — ruling S8's *"same drive voltage, nothing touched
    between measurements"* and ``entry_baseline_record``'s own *"a before→after
    claim is only as good as those three matching on both sides."* This type
    reports them; comparing them is ``analyze``'s.
    """

    store: "RecordStore"
    state_id: str
    session_id: str
    measurement_level_db: float | None
    graph_fingerprint: str
    record_ids: tuple[str, ...]
    baseline_record_id: str
    disclosures: tuple[CapabilityStub, ...]

    @classmethod
    def read(cls, store: "RecordStore", state_id: str) -> "PriorBank | None":
        """The bank one ``save`` left behind, or ``None`` when there is none.

        ``None`` rather than a raise, for the reason
        :meth:`~.session_seams.RecordStore.read` gives: a missing prior is a
        fact a session discloses, not an exception that strands a household
        whose only remaining move is the round being refused (ruling S10). A
        state id that no longer resolves, and a state written by a build before
        this key shipped, are the same answer.

        The baseline is resolved here rather than on demand so that one
        constructed bank is one consistent reading. It scans from the END
        backwards and stops at the first baseline-kind record, which is one
        store read in the ordinary case.
        """
        state = store.read_state(state_id)
        if state is None:
            return None
        record_ids = _texts(state.get("record_ids"))
        return cls(
            store=store,
            state_id=state_id,
            session_id=str(state.get("session_id") or ""),
            measurement_level_db=_number(state.get("measurement_level_db")),
            graph_fingerprint=str(state.get("graph_fingerprint") or ""),
            record_ids=record_ids,
            baseline_record_id=_last_of_kind(
                store, record_ids, MEASURE_KIND_BASELINE,
            ),
            disclosures=_disclosures(state.get("disclosures")),
        )

    def baseline(self) -> Mapping[str, Any] | None:
        """The banked "before" record, or ``None`` when the bank holds none.

        The read half of the verify seam's middle argument —
        ``(applied_candidate, entry_baseline, capture) → verdict``.
        """
        if not self.baseline_record_id:
            return None
        return self.store.read(self.baseline_record_id)


def _last_of_kind(
    store: "RecordStore", record_ids: tuple[str, ...], kind: str,
) -> str:
    for record_id in reversed(record_ids):
        record = store.read(record_id)
        if record is not None and record.get("kind") == kind:
            return record_id
    return ""


def _texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _disclosures(value: Any) -> tuple[CapabilityStub, ...]:
    """Re-render what the bank disclosed, dropping anything unrecognisable.

    A code this build has no stub for is a hole a LATER build named and this
    one cannot describe; rendering it as an unnamed stub would put a sentence
    in front of a person that says nothing. Dropping it keeps every disclosure
    this session reports one the reader can act on, and the bank still carries
    the raw code for whoever wrote it.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    stubs: list[CapabilityStub] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        stub = stub_for_code(
            str(entry.get("code") or ""), captured=bool(entry.get("captured")),
        )
        if stub is not None:
            stubs.append(stub)
    return tuple(stubs)
