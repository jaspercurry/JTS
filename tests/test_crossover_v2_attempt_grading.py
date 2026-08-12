# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The seam between the conductor and ``crossover_v2.attempt_grading`` (5b-ii).

The claims this slice makes that nothing else asserted. Every one of them is a
property of the SPLIT — what each ladder is allowed to know, and what the
conductor must still do itself — so the wiring half is written against the
production entry point (``_consume_verify`` via ``_run_phase``) rather than
against the pure functions, which is where a mistake would actually show up.

**The durable write is the reason this slice is its own review.** Banking
prediction error into ``model_error_store`` cannot be retried, undone, or
double-counted, so the properties below pin more than "the decision is the
same": they pin that the write still happens exactly once, still happens at the
same point in the sequence, and still cannot be reached by an answer nobody
wired. The extraction deliberately left the seam call, its guard, its ``except``
arm and both of its log lines on the conductor; these tests are what makes that
claim checkable rather than merely stated.

The kind-vocabulary trio is the discipline ``spatial.SCREEN_KINDS``,
``coordinator.REFUSAL_KINDS`` and ``admission.SETTLE_KINDS`` already keep,
applied to the two decisions this slice introduced: declared set, every member
handled, and an unrecognised member loud and CONSERVATIVE rather than quietly
admitted — which here means "never reaches the write" and "never claims an
improvement".
"""

import ast
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import attempt_grading

from tests.test_crossover_v2_conductor import (
    FakeSeams,
    _run_phase,
    _verify_only_conductor,
)

IDENTITY_UNMAPPED_EVENT = "crossover_v2_attempt_identity_kind_unmapped"
GRADE_UNMAPPED_EVENT = "crossover_v2_attempt_grade_kind_unmapped"

#: A kind no released ladder returns — the shape of the future defect.
FUTURE_KIND = "a_kind_from_the_future"


def _never_asked() -> str | None:
    raise AssertionError("the candidate was reached with a tuning id in hand")


# --- the identity ladder ------------------------------------------------------


def test_the_applied_candidates_identity_is_taken_most_specific_first():
    """Tuning attempt id, then the built candidate, then a session fallback.

    The order is the ruling: on the stage that grades a round the first is the
    only one populated, and two captures of one applied candidate must land on
    one id or the dedup rung cannot see a repeat.
    """
    assert attempt_grading.assess_attempt_identity(
        tuning_attempt_id="candidate-a",
        candidate_fingerprint=_never_asked,
        session_id="S",
        capture_attempt=2,
        recorded_ids=(),
    ) == attempt_grading.AttemptIdentity(attempt_grading.ATTEMPT_NEW, "candidate-a")

    assert attempt_grading.assess_attempt_identity(
        tuning_attempt_id="",
        candidate_fingerprint=lambda: "fingerprint-b",
        session_id="S",
        capture_attempt=2,
        recorded_ids=(),
    ).attempt_id == "fingerprint-b"

    # No tuning id and no candidate at all: unique per capture, so two captures
    # of an unidentified proposal are never mistaken for a repeat of one.
    assert attempt_grading.assess_attempt_identity(
        tuning_attempt_id=None,
        candidate_fingerprint=lambda: None,
        session_id="S",
        capture_attempt=2,
        recorded_ids=(),
    ).attempt_id == "S:2"
    # An empty fingerprint is as absent as no candidate — the shipped
    # ``str(... or "")`` collapses both, and falling back is what keeps an
    # unidentifiable capture out of another attempt's identity.
    assert attempt_grading.assess_attempt_identity(
        tuning_attempt_id=None,
        candidate_fingerprint=lambda: "",
        session_id="S",
        capture_attempt=3,
        recorded_ids=(),
    ).attempt_id == "S:3"


def test_the_ladder_does_not_reach_the_candidate_while_a_tuning_id_exists():
    """``candidate_fingerprint`` is a port for call count, and this is the count.

    The shipped flow reads the candidate object only when no tuning attempt id
    is in hand, so the LADDER must not ask above that rung.

    This is only half the promise, and the half a unit test can see: a call site
    that resolved the callable eagerly would satisfy this contract and still
    reach into the host object on every graded capture.
    ``test_the_flow_passes_the_fingerprint_lazily_not_as_a_value`` is the other
    half. Both exist because a mutation of the flow's call site survived this
    test — the scope of a guard has to match the scope of its claim.
    """
    asked: list[int] = []

    def fingerprint() -> str | None:
        asked.append(1)
        return "fingerprint-b"

    attempt_grading.assess_attempt_identity(
        tuning_attempt_id="candidate-a",
        candidate_fingerprint=fingerprint,
        session_id="S",
        capture_attempt=1,
        recorded_ids=(),
    )
    assert asked == []

    attempt_grading.assess_attempt_identity(
        tuning_attempt_id=None,
        candidate_fingerprint=fingerprint,
        session_id="S",
        capture_attempt=1,
        recorded_ids=(),
    )
    assert asked == [1]


class _RecordingCandidate:
    """A candidate whose fingerprint read is observable."""

    def __init__(self) -> None:
        self.reads = 0

    @property
    def fingerprint(self) -> str:
        self.reads += 1
        return "fingerprint-b"


def test_the_flow_passes_the_fingerprint_lazily_not_as_a_value():
    """The call-count port asserted at the WIRING, not just in the ladder.

    The sibling above pins that the ladder does not ask above the tuning-id
    rung. It cannot see the other half: ``candidate_fingerprint=(lambda _v=<the
    expression>: _v)`` is still a callable, still satisfies the ladder, and
    still reads the host object on every graded capture — a read the shipped
    flow does not make. Only a test driven through the conductor tells those
    apart, and a flow-side mutation surviving the unit test is how this one came
    to exist.
    """
    with_id = _RecordingCandidate()
    c = _verify_only_conductor(FakeSeams(), tuning_attempt_id="candidate-a")
    c._candidate = with_id
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert with_id.reads == 0
    assert [item.attempt_id for item in c.attempt_history] == ["candidate-a"]

    # Non-vacuity: with no tuning id the same wiring DOES reach the candidate,
    # so a zero above is the port working rather than the fixture never looking.
    without_id = _RecordingCandidate()
    c2 = _verify_only_conductor(FakeSeams(), tuning_attempt_id="")
    c2._candidate = without_id
    assert _run_phase(c2, 1, 1)["accepted"] is True
    assert without_id.reads > 0
    assert [item.attempt_id for item in c2.attempt_history] == ["fingerprint-b"]


def test_a_candidate_already_in_history_is_not_a_new_attempt():
    """The one rung between a replayed capture and a second durable write."""
    assert attempt_grading.assess_attempt_identity(
        tuning_attempt_id="candidate-a",
        candidate_fingerprint=_never_asked,
        session_id="S",
        capture_attempt=1,
        recorded_ids=("candidate-zero", "candidate-a"),
    ) == attempt_grading.AttemptIdentity(
        attempt_grading.ATTEMPT_ALREADY_RECORDED, "candidate-a",
    )


def test_the_history_scan_stops_at_the_first_match():
    """The docstring promises the shipped ``any(...)`` short-circuit survives."""
    consumed: list[str] = []

    def ids():
        for item in ("candidate-a", "candidate-b", "candidate-c"):
            consumed.append(item)
            yield item

    assert attempt_grading.assess_attempt_identity(
        tuning_attempt_id="candidate-a",
        candidate_fingerprint=_never_asked,
        session_id="S",
        capture_attempt=1,
        recorded_ids=ids(),
    ).kind == attempt_grading.ATTEMPT_ALREADY_RECORDED
    assert consumed == ["candidate-a"]


# --- the grading ladder -------------------------------------------------------


def test_evidence_refusal_outranks_grading_preconditions():
    """The ORDER is the ruling, and only the both-true case can show it.

    A capture that failed its integrity checks on a speaker with no adopted
    floor must be answered as a capture problem (#2033), not as a configuration
    one. Swapping the two rungs passes every single-condition case and fails
    only here.
    """
    assert attempt_grading.grade_attempt_outcome(
        comparable=False, floor_present=False,
    ) == attempt_grading.GRADE_NOT_COMPARABLE
    assert attempt_grading.grade_attempt_outcome(
        comparable=False, floor_present=True,
    ) == attempt_grading.GRADE_NOT_COMPARABLE
    assert attempt_grading.grade_attempt_outcome(
        comparable=True, floor_present=False,
    ) == attempt_grading.GRADE_NO_FLOOR
    assert attempt_grading.grade_attempt_outcome(
        comparable=True, floor_present=True,
    ) == attempt_grading.GRADE_DECIDE_NEXT


# --- the declared vocabularies ------------------------------------------------


def test_the_declared_kinds_are_the_ones_the_ladders_can_return():
    """A declaration can go stale, so drive both directions out of the ladders.

    Every kind the two ladders actually produce is generated here, so a kind
    that stops being reachable — or one returned but never declared — shows up
    as a set difference rather than as an answer nobody wired an arm for.
    """
    identities = {
        attempt_grading.assess_attempt_identity(
            tuning_attempt_id="candidate-a",
            candidate_fingerprint=_never_asked,
            session_id="S",
            capture_attempt=1,
            recorded_ids=recorded,
        ).kind
        for recorded in ((), ("candidate-a",))
    }
    assert identities == set(attempt_grading.IDENTITY_KINDS)

    grades = {
        attempt_grading.grade_attempt_outcome(
            comparable=comparable, floor_present=floor_present,
        )
        for comparable in (True, False)
        for floor_present in (True, False)
    }
    assert grades == set(attempt_grading.GRADE_KINDS)


def test_the_ladders_module_imports_neither_the_flow_nor_the_loop_kernel():
    """The docstring's dependency claim, asserted rather than stated.

    The package-wide guard in ``test_crossover_v2_journey`` covers the flow and
    ``jasper.web``. This adds the claim specific to this module: it answers with
    kinds, so it needs no ``attempts_loop`` import — and a future edit that
    reaches for ``LoopDecision`` here would move household-visible payload
    construction out of the one place that owns it.
    """
    module = (
        Path(flow.__file__).resolve().parent / "crossover_v2" / "attempt_grading.py"
    )
    tree = ast.parse(module.read_text(), filename=str(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not [name for name in imported if "attempts_loop" in name]
    assert not [name for name in imported if "crossover_v2_flow" in name]
    assert not [name for name in imported if name.startswith("jasper.web")]


# --- the wiring, and the durable write ----------------------------------------


def _seen(caplog, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if event in r.getMessage()]


def test_every_declared_identity_kind_is_handled(caplog):
    """The catch-all must not answer for a kind somebody did wire.

    The assertion is the JOURNAL, not the outcome: an unmapped kind also skips,
    so "nothing was written" would pass for exactly the case this exists to
    catch. Its sibling below proves the event does fire for an unwired kind, so
    this guard cannot be vacuous.
    """
    with caplog.at_level(logging.ERROR):
        for kind in sorted(attempt_grading.IDENTITY_KINDS):
            fakes = FakeSeams()
            c = _verify_only_conductor(
                fakes,
                seams=replace(fakes.seams(), record_model_error=lambda **_: True),
                tuning_attempt_id="candidate-a",
            )
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    flow._grading, "assess_attempt_identity",
                    lambda kind=kind, **_: attempt_grading.AttemptIdentity(
                        kind, "candidate-a",
                    ),
                )
                _run_phase(c, 1, 1)

    assert _seen(caplog, IDENTITY_UNMAPPED_EVENT) == []


def test_every_declared_grade_kind_is_handled(caplog):
    """Same discipline on the rung behind the write."""
    with caplog.at_level(logging.ERROR):
        for kind in sorted(attempt_grading.GRADE_KINDS):
            fakes = FakeSeams()
            c = _verify_only_conductor(fakes, tuning_attempt_id="candidate-a")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    flow._grading, "grade_attempt_outcome", lambda kind=kind, **_: kind,
                )
                _run_phase(c, 1, 1)

    assert _seen(caplog, GRADE_UNMAPPED_EVENT) == []


def test_an_unrecognised_identity_kind_never_reaches_the_durable_write(caplog):
    """The DIRECTION is the assertion, and here it is the permissive one.

    Falling through an unwired identity kind reaches ``record_model_error``,
    which would bank a second durable observation of one candidate identity —
    an irreversible double-write. So the fallback skips, and the test asserts
    the store was never called rather than merely that a log line appeared.
    """
    written: list[dict[str, Any]] = []
    fakes = FakeSeams()
    c = _verify_only_conductor(
        fakes,
        seams=replace(
            fakes.seams(),
            record_model_error=lambda **obs: written.append(dict(obs)) or True,
        ),
        tuning_attempt_id="candidate-a",
    )

    with caplog.at_level(logging.ERROR), pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow._grading, "assess_attempt_identity",
            lambda **_: attempt_grading.AttemptIdentity(FUTURE_KIND, "candidate-a"),
        )
        verdict = _run_phase(c, 1, 1)

    assert written == []
    assert [r.levelname for r in _seen(caplog, IDENTITY_UNMAPPED_EVENT)] == ["ERROR"]
    # It skipped rather than half-graded: no ledger growth, no projection.
    assert c.attempt_history == ()
    assert c.last_attempt_decision is None
    # And it did not cost the capture its verdict — the write is forensics.
    assert verdict["accepted"] is True


def test_an_unrecognised_grade_kind_refuses_rather_than_claiming_improvement(caplog):
    """The other ladder's dangerous direction: an unearned improvement claim.

    ``decide_next`` is the arm that makes a claim ABOUT THE SPEAKER. Degrading
    toward it would put "improved above floor" in front of a household on an
    answer nobody wired, and on a speaker with no adopted floor it would not
    even get that far. The evidence refusal is the honest degradation.
    """
    fakes = FakeSeams()
    c = _verify_only_conductor(fakes, tuning_attempt_id="candidate-a")

    with caplog.at_level(logging.ERROR), pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow._grading, "grade_attempt_outcome", lambda **_: FUTURE_KIND)
        _run_phase(c, 1, 1)

    assert [r.levelname for r in _seen(caplog, GRADE_UNMAPPED_EVENT)] == ["ERROR"]
    decision = c.last_attempt_decision
    assert decision["decision"] == flow.STOP_EVIDENCE
    assert decision["reason"] == flow.REASON_ATTEMPT_NOT_COMPARABLE
    # It did NOT degrade in the permissive direction.
    assert decision["improved"] is None


def test_the_durable_write_still_happens_between_the_record_and_the_decision():
    """The write's SEQUENCE POINT, not just its count.

    The extraction brackets ``record_model_error`` with two ladders. This pins
    that it still fires after the record exists and before the loop decision is
    projected onto the conductor — the window the shipped comment calls
    "claim the durable observation identity before banking the journey
    projection". A rewrite that gathered the decision first and wrote afterwards
    would keep every count and payload assertion green and still break the
    crash-recovery ordering this method was written for.
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
