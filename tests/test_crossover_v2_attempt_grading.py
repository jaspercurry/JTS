# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The conductor's tuning-attempt grading (#2291 5b-ii), driven through
``_consume_verify`` via ``_run_phase`` — the production entry point.

What this slice pins that nothing else asserts: which identity a graded
capture lands on (and that the candidate object is read only when no tuning
attempt id is in hand), and that the irreversible ``model_error_store`` write
still fires between the record's construction and the loop decision.
``tests/test_crossover_v2_conductor.py`` owns the neighbouring properties:
the already-recorded dedup, the write's exactly-once guard across store
failures, and the evidence-refusal-outranks-no-floor ruling.
"""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from tests.crossover_v2_fixtures import (
    SESSION,
    FakeSeams,
    _run_phase,
    _verify_only_conductor,
)


class _RecordingCandidate:
    """A candidate whose fingerprint read is observable."""

    def __init__(self) -> None:
        self.reads = 0

    @property
    def fingerprint(self) -> str:
        self.reads += 1
        return "fingerprint-b"


def test_the_flow_reads_the_candidate_only_when_no_tuning_id_is_in_hand():
    """The applied candidate's identity is taken most specific first.

    On the stage that grades a round the tuning attempt id is the only rung
    populated, and a capture that shares an applied candidate must share its
    id or the already-recorded dedup cannot see a repeat. While an id is in
    hand the candidate object is not read at all — the count below is that
    claim, asserted at the wiring where an eagerly-resolved value would show
    up as a read.
    """
    with_id = _RecordingCandidate()
    c = _verify_only_conductor(FakeSeams(), tuning_attempt_id="candidate-a")
    c._candidate = with_id
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert with_id.reads == 0
    assert [item.attempt_id for item in c.attempt_history] == ["candidate-a"]

    # Non-vacuity: with no tuning id the same wiring DOES reach the candidate,
    # so a zero above is the rung working rather than the fixture never
    # looking.
    without_id = _RecordingCandidate()
    c2 = _verify_only_conductor(FakeSeams(), tuning_attempt_id="")
    c2._candidate = without_id
    assert _run_phase(c2, 1, 1)["accepted"] is True
    assert without_id.reads > 0
    assert [item.attempt_id for item in c2.attempt_history] == ["fingerprint-b"]


def test_an_unidentifiable_attempt_gets_a_session_scoped_id():
    """No tuning id and no candidate to ask: unique per capture, so two
    captures of an unidentified proposal are never mistaken for a repeat of
    one. An empty fingerprint is as absent as no candidate — falling back is
    what keeps an unidentifiable capture out of another attempt's identity."""
    c = _verify_only_conductor(FakeSeams(), tuning_attempt_id="")
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert [item.attempt_id for item in c.attempt_history] == [f"{SESSION}:1"]

    c2 = _verify_only_conductor(FakeSeams(), tuning_attempt_id="")
    c2._candidate = SimpleNamespace(fingerprint="")
    assert _run_phase(c2, 1, 1)["accepted"] is True
    assert [item.attempt_id for item in c2.attempt_history] == [f"{SESSION}:1"]


def test_the_durable_write_still_happens_between_the_record_and_the_decision():
    """The write's SEQUENCE POINT, not just its count.

    ``record_model_error`` fires after the record exists and before the loop
    decision is projected onto the conductor — the window the shipped comment
    calls "claim the durable observation identity before banking the journey
    projection". A rewrite that gathered the decision first and wrote
    afterwards would keep every count and payload assertion green and still
    break the crash-recovery ordering this method was written for.
    """
    prior = {"decision": None, "reason": "seeded-prior"}
    observed: list[dict[str, Any]] = []
    fakes = FakeSeams()
    c = _verify_only_conductor(
        fakes,
        seams=replace(fakes.seams(), record_model_error=lambda **obs: (
            observed.append({
                "attempt_id": obs["attempt_id"],
                "decision_at_write": c.last_attempt_decision,
                "history_at_write": tuple(
                    item.attempt_id for item in c.attempt_history
                ),
            }) or True
        )),
        last_attempt_decision=prior,
        tuning_attempt_id="candidate-a",
    )

    assert _run_phase(c, 1, 1)["accepted"] is True

    assert len(observed) == 1
    # The record was already built: its id is what the store was asked about.
    assert observed[0]["attempt_id"] == "candidate-a"
    # ...and neither the projection nor the ledger had moved yet.
    assert observed[0]["decision_at_write"] == prior
    assert observed[0]["history_at_write"] == ()
    # Both moved afterwards, so the ordering above is a real window.
    assert c.last_attempt_decision != prior
    assert [item.attempt_id for item in c.attempt_history] == ["candidate-a"]
