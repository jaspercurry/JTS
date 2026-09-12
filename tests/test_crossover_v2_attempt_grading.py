# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Production attempt identity, durable write ordering and advisory decisions."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.web import correction_crossover_v2 as host
from jasper.active_speaker.crossover_v2.durable_state import (
    MAX_ATTEMPT_HISTORY, AttemptIntegrity,
)

from tests.crossover_v2_fixtures import (
    SESSION,
    FakeSeams,
    _run_phase,
    _verify_only_conductor, _verify_analysis,
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


def test_store_write_precedes_journey_history():
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
    assert observed[0]["attempt_id"] == "candidate-a"
    assert observed[0]["decision_at_write"] is None
    assert observed[0]["history_at_write"] == ()
    assert c.last_attempt_decision != prior
    assert [item.attempt_id for item in c.attempt_history] == ["candidate-a"]


def test_comparison_advice_does_not_limit_further_human_started_experiments():
    history = ()
    cap = MAX_ATTEMPT_HISTORY
    for attempt in range(cap + 2):
        c = _verify_only_conductor(
            FakeSeams(), tuning_attempt_id=f"candidate-{attempt}", attempt_history=history,
        )
        assert _run_phase(c, 1, 1)["accepted"] is True
        assert c.last_attempt_decision is None
        history = c.attempt_history
    assert len(history) == cap
    assert history[-1].attempt_id == f"candidate-{cap + 1}"



def test_an_accepted_but_incomparable_record_is_not_banked_into_history(monkeypatch):
    real_from_verify = flow.attempt_record_from_verify

    def _incomparable_record(*args, **kwargs):
        record = real_from_verify(*args, **kwargs)
        return replace(
            record,
            integrity=AttemptIntegrity(comparable=False, reasons=("legacy_shape",)),
        )

    monkeypatch.setattr(flow, "attempt_record_from_verify", _incomparable_record)
    c = _verify_only_conductor(FakeSeams(), tuning_attempt_id="candidate-a")
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert c.attempt_history == ()


def test_failed_verify_grade_is_durable_advice_without_a_retake(tmp_path):
    fakes = FakeSeams()
    fakes.verify = lambda program: _verify_analysis(program, max_db=3.0)
    conductor = _verify_only_conductor(fakes, tuning_attempt_id="failed-grade")
    verdict = _run_phase(conductor, 1, 1)
    assert verdict["accepted"] is True
    assert verdict["next"] == "accept"
    assert conductor.current_phase == "done"
    assert conductor.last_attempt_decision is None
    path = tmp_path / "state.json"
    host.set_state_path_for_tests(path)
    try:
        host.persist_conductor_state(conductor, failure_code=None)
        state = host.load_v2_state()
    finally:
        host.set_state_path_for_tests(None)
    assert state["verify"]["outcome"] == "fail"
    assert state["verify"]["claims"]["integration"]["status"] == "fail"
    assert state["attempts_loop"]["last_decision"] is None
    assert state["attempts_loop"]["history"][-1]["grade_db"] == 3.0
    grade = host._post_apply_grade(state)
    assert grade["state"] == host.GRADE_FAILED
    assert grade["verify_outcome"] == "fail"
