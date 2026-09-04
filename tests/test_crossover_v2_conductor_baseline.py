# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: fixture premise, live-attempts loop, happy path, predicted-ripple disclosure (G1)."""

from __future__ import annotations

import dataclasses
import logging
import types
import numpy as np
import pytest
from dataclasses import replace
from typing import Any
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import (
    capture_plan,
    planning,
)
from jasper.active_speaker.crossover_v2.contracts import REFERENCE_MARK_DESIGN_AXIS
from jasper.active_speaker.crossover_v2.round_evidence import (
    MEASURED_BENEFIT_MARGIN_DB,
    measured_response_from_analysis,
)
from jasper.active_speaker.attempts_loop import (
    PROVENANCE_REALIZED,
    REASON_ATTEMPT_NOT_COMPARABLE,
    REASON_BASELINE_ESTABLISHED,
    REASON_GRADED_BINS_SHRANK,
    REASON_IMPROVEMENT_ABOVE_FLOOR,
    STOP_EVIDENCE,
    AttemptIntegrity,
    AttemptRecord,
    decide_next,
)
from jasper.active_speaker.crossover_v2_flow import (
    ATTEMPT_REASON_NO_FLOOR,
    ALIGNMENT_CONFIDENCE_TRUST_FLOOR,
    GAIN_CAP_BACKOFF_DB,
    MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB,
    CrossoverV2FlowError,
    alignment_delay_search_bounds_us,
    alignment_to_candidate_fields,
    back_off_gain,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_REVIEW,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.branch_chain import crossover_response_complex
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    INTEGRITY_CHECK_SWEEP_HEARD,
    INTEGRITY_CHECK_SWEEP_SCHEDULE,
    INTEGRITY_FAIL,
    INTEGRITY_NOT_EVALUATED,
    CaptureIntegrity,
    IntegrityCheck,
)
from jasper.active_speaker.flat_spec import (
    evaluate_flat_spec,
    spec_convergence_residual,
)
from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
)
from tests.test_active_speaker_profile import _two_way_preset
from tests.crossover_v2_fixtures import (
    CAPS,
    FakeSeams,
    SESSION,
    _DIAG_LOGGER,
    _ENTRY_BASELINE_RESIDUAL_DB,
    _POST_APPLY_RESIDUAL_DB,
    _alignment,
    _attempt_floor,
    _capture,
    _conductor,
    _eligible_measure_analysis,
    _measure_analysis,
    _preset,
    _run_phase,
    _verify_analysis,
    _verify_only_conductor,
)


# --- the shared fixture's own premise ------------------------------------------


def test_the_fixture_entry_baseline_is_measurably_worse_than_the_post_apply_one():
    """``_fixture_entry_baseline``'s whole reason to exist, made falsifiable.

    Every conductor this file builds grades its #2291 round against that
    baseline, and the grading is only honest if the "before" really is the worse
    measurement — by more than the claim margin. Nothing else in the file would
    notice if it stopped being true: ``_in_room_summed_db`` changing, the
    reducer's grid changing, or the sign of ``spec_convergence_residual``
    flipping would all turn every round into a measured REGRESSION, which the
    adoption table restores on, and the failures would surface far from here as
    refusals about rollback anchors.

    It also pins the two decimals the fixture's comment quotes, so those are
    checked numbers rather than remembered ones.
    """
    fakes = FakeSeams()
    conductor = _conductor(fakes)
    baseline = conductor.measure_entry_baseline
    assert baseline is not None

    post = measured_response_from_analysis(
        _verify_analysis(conductor.program_for_phase(PHASE_VERIFY)),
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
    )
    # Comparable by construction, or the benefit verdict is about the fixture
    # rather than about the speaker.
    assert baseline.program_id == post.program_id
    assert baseline.reference_mark == post.reference_mark
    assert baseline.curve.hz == post.curve.hz

    def residual_db(hz, db, excluded) -> float:
        report = evaluate_flat_spec(
            np.asarray(hz, dtype=np.float64),
            np.asarray(db, dtype=np.float64),
            np.asarray(excluded, dtype=bool),
        )
        convergence = spec_convergence_residual(report)
        assert convergence.evaluable and convergence.rms_db is not None
        return float(convergence.rms_db)

    before_db = residual_db(baseline.curve.hz, baseline.curve.db, baseline.excluded)
    after_db = residual_db(post.curve.hz, post.curve.db, post.excluded)
    assert before_db == pytest.approx(_ENTRY_BASELINE_RESIDUAL_DB, abs=0.001)
    assert after_db == pytest.approx(_POST_APPLY_RESIDUAL_DB, abs=0.001)
    assert (before_db - after_db) > MEASURED_BENEFIT_MARGIN_DB


# --- live attempts loop -------------------------------------------------------


def test_accepted_apply_verify_writes_model_error_exactly_once():
    written: list[dict[str, Any]] = []
    fakes = FakeSeams()

    def record(**observation: Any) -> bool:
        written.append(dict(observation))
        return True

    c = _verify_only_conductor(
        fakes,
        seams=replace(fakes.seams(), record_model_error=record),
        tuning_attempt_id="candidate-a",
        speaker_id="speaker-a",
    )
    first = _run_phase(c, 1, 1)
    repeated = _run_phase(c, 1, 2)

    assert first["accepted"] is True
    assert repeated["accepted"] is True
    assert len(written) == 1
    assert written[0] == {
        "speaker_id": "speaker-a",
        "attempt_id": "candidate-a",
        "metric": flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        "predicted_db": 0.0,
        "realized_db": 0.9,
        "context": {
            "session_id": SESSION,
            "provenance": PROVENANCE_REALIZED,
        },
    }
    assert [item.attempt_id for item in c.attempt_history] == ["candidate-a"]
    assert c.last_attempt_decision["reason"] == REASON_BASELINE_ESTABLISHED


def test_store_write_is_idempotent_across_a_crash_before_journey_persist(tmp_path):
    """A rebuilt conductor may lack history even though the store write won."""
    from jasper.active_speaker.model_error_store import (
        load_state,
        record_model_error,
    )

    path = tmp_path / "model-error.json"

    def record(**observation: Any) -> bool:
        record_model_error(path=path, **observation)
        return True

    first_fakes = FakeSeams()
    first = _verify_only_conductor(
        first_fakes,
        seams=replace(first_fakes.seams(), record_model_error=record),
        tuning_attempt_id="candidate-a",
        speaker_id="speaker-a",
    )
    assert _run_phase(first, 1, 1)["accepted"] is True
    assert len(load_state(path)["model_error"]) == 1

    # Simulate a crash before the host persisted ``first.attempt_history``:
    # rebuild with no history but the same applied-candidate identity.
    recovered_fakes = FakeSeams()
    recovered = _verify_only_conductor(
        recovered_fakes,
        seams=replace(recovered_fakes.seams(), record_model_error=record),
        tuning_attempt_id="candidate-a",
        speaker_id="speaker-a",
    )
    assert _run_phase(recovered, 1, 1)["accepted"] is True

    records = load_state(path)["model_error"]
    assert [item["attempt_id"] for item in records] == ["candidate-a"]
    assert [item.attempt_id for item in recovered.attempt_history] == ["candidate-a"]


def test_changed_recovery_verify_cannot_split_store_and_journey_truth(
    tmp_path, caplog,
):
    """A recovery conflict cannot reuse the previous candidate's verdict."""
    from jasper.active_speaker.model_error_store import (
        ModelErrorConflictError,
        load_state,
        record_model_error,
    )
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )
    from jasper.web import correction_crossover_v2 as v2host

    path = tmp_path / "model-error.json"
    state_path = tmp_path / "v2-state.json"
    history = (
        AttemptRecord(
            attempt_id="candidate-base",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
            sitting_id=SESSION,
            integrity=AttemptIntegrity(comparable=True),
            grade_db=1.4,
            n_graded_bins=120,
        ),
        AttemptRecord(
            attempt_id="candidate-previous",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
            sitting_id=SESSION,
            integrity=AttemptIntegrity(comparable=True),
            grade_db=1.0,
            n_graded_bins=120,
        ),
    )
    prior_decision = decide_next(history, _attempt_floor()).to_dict()
    assert prior_decision["reason"] == REASON_IMPROVEMENT_ABOVE_FLOOR
    assert prior_decision["basis_attempt_ids"] == [
        "candidate-base", "candidate-previous",
    ]

    # The store write won, then the process died before the new journey fact.
    record_model_error(
        speaker_id="speaker-a",
        attempt_id="candidate-current",
        metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        predicted_db=0.0,
        realized_db=0.9,
        path=path,
    )

    def record(**observation: Any) -> bool:
        try:
            record_model_error(path=path, **observation)
        except ModelErrorConflictError:
            return False
        return True

    recovered_fakes = FakeSeams()
    recovered_fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.7, n_graded_bins=80,
    )
    recovered = _verify_only_conductor(
        recovered_fakes,
        seams=replace(recovered_fakes.seams(), record_model_error=record),
        attempt_history=history,
        last_attempt_decision=prior_decision,
        tuning_attempt_id="candidate-current",
        speaker_id="speaker-a",
    )
    with caplog.at_level(logging.WARNING):
        assert _run_phase(recovered, 1, 1)["accepted"] is True

    records = load_state(path)["model_error"]
    assert len(records) == 1
    assert records[0]["realized_db"] == pytest.approx(0.9)
    assert recovered.attempt_history == history
    assert recovered.last_attempt_decision is None
    assert (
        "event=correction.crossover_v2_model_error_identity_conflict"
        in caplog.text
    )
    assert "correction.crossover_v2_model_error_write_failed" not in caplog.text

    # The host persists the conductor snapshot verbatim. The household surface
    # must see no attempt sentence—not the hydrated previous candidate's 0.4 dB
    # claim dressed up as the current result.
    v2host.set_state_path_for_tests(state_path)
    try:
        v2host.persist_conductor_state(recovered, failure_code=None)
        persisted = v2host.load_v2_state()
    finally:
        v2host.set_state_path_for_tests(None)
    assert persisted["attempts_loop"]["last_decision"] is None
    assert [
        item["attempt_id"] for item in persisted["attempts_loop"]["history"]
    ] == ["candidate-base", "candidate-previous"]
    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "done",
            "verify": persisted["verify"],
            "candidate": persisted["candidate"],
            "attempts_loop": persisted["attempts_loop"],
        },
    })
    assert "tracked its prediction" not in envelope["verdict_text"]


def test_model_error_store_failure_warns_without_blocking_verify(caplog):
    def fail_write(**_observation: Any) -> None:
        raise OSError("synthetic full disk")

    fakes = FakeSeams()
    c = _verify_only_conductor(
        fakes,
        seams=replace(fakes.seams(), record_model_error=fail_write),
        tuning_attempt_id="candidate-a",
        speaker_id="speaker-a",
    )
    with caplog.at_level(logging.WARNING):
        verdict = _run_phase(c, 1, 1)

    assert verdict["accepted"] is True
    assert c.current_phase == PHASE_DONE
    assert [item.attempt_id for item in c.attempt_history] == ["candidate-a"]
    assert "event=correction.crossover_v2_model_error_write_failed" in caplog.text


def test_unexpected_store_failure_cannot_double_bank_on_a_retry(caplog):
    """#2386: an out-of-family seam raise must not let a retry re-fire the write.

    The rung that stops a second durable write is the attempt landing in
    ``attempt_history``, which ``_grade_verify_attempt`` appends AFTER the seam
    call. Before the fix a raise outside the named family skipped that append,
    so a retry of the SAME applied candidate was assessed as a new attempt and
    asked the seam a second time — measured on the shipped code as two writes
    for two runs of one attempt. Both halves are asserted here: the write fires
    once, and the attempt is banked, which is the mechanism that makes it once.
    """
    calls: list[dict[str, Any]] = []

    def unexpected_write(**observation: Any) -> bool:
        calls.append(dict(observation))
        # Deliberately outside (OSError, RuntimeError, TypeError, ValueError,
        # OverflowError). MemoryError is the shape a 1 GB Pi can actually
        # produce; the property is about the interface, not this class.
        raise MemoryError("synthetic out-of-family store failure")

    fakes = FakeSeams()
    c = _verify_only_conductor(
        fakes,
        seams=replace(fakes.seams(), record_model_error=unexpected_write),
        tuning_attempt_id="candidate-a",
        speaker_id="speaker-a",
    )
    # The raise is TOLERATED rather than allowed to end the test, because the
    # original bug is a COUNT: a test that dies on the first propagating run
    # never reaches the second write and so never observes the double-bank it
    # claims to pin. Pre-fix this loop collects two calls and two propagated
    # exceptions; post-fix, one call and none.
    #
    # caplog at WARNING, not ERROR, so the named-family event WOULD be captured
    # if it fired — otherwise "not filed under the other arm" is vacuous.
    verdicts: list[dict] = []
    propagated: list[str] = []
    with caplog.at_level(logging.WARNING):
        for run in (1, 2):
            try:
                verdicts.append(_run_phase(c, 1, run))
            except MemoryError:
                propagated.append("MemoryError")

    # The property, asserted first so a regression reddens on the count itself.
    assert len(calls) == 1  # was 2
    assert propagated == []  # was ["MemoryError", "MemoryError"]
    # The mechanism that produces it.
    assert [item.attempt_id for item in c.attempt_history] == ["candidate-a"]
    # The forensics failure did not reverse the VERIFY the gate accepted.
    assert [verdict["accepted"] for verdict in verdicts] == [True, True]
    assert c.current_phase == PHASE_DONE
    assert (
        "event=correction.crossover_v2_model_error_write_unexpected"
        in caplog.text
    )
    assert (
        "event=correction.crossover_v2_model_error_write_failed"
        not in caplog.text
    )


def test_base_exception_from_the_store_seam_still_propagates():
    """The broad catch is ``Exception``, deliberately not ``BaseException``.

    A ``KeyboardInterrupt`` must not be swallowed by a forensics guard, and
    containing it would buy no retry-safety anyway: nothing this method appends
    is persisted by this method, so a dying process has no retry to protect.
    """
    def interrupted_write(**_observation: Any) -> bool:
        raise KeyboardInterrupt("operator stopped the run")

    fakes = FakeSeams()
    c = _verify_only_conductor(
        fakes,
        seams=replace(fakes.seams(), record_model_error=interrupted_write),
        tuning_attempt_id="candidate-a",
        speaker_id="speaker-a",
    )
    with pytest.raises(KeyboardInterrupt):
        _run_phase(c, 1, 1)


def test_glitched_verify_reaches_loop_as_stop_evidence():
    integrity = CaptureIntegrity(checks=(
        IntegrityCheck(INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_FAIL),
        IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE,
            INTEGRITY_NOT_EVALUATED,
            "sweep was not heard",
        ),
    ))
    fakes = FakeSeams()
    fakes.verify = lambda program: _verify_analysis(program, integrity=integrity)
    # No adopted floor is the production default. Evidence refusal must still
    # outrank that absent grading precondition (#2033).
    c = _conductor(
        fakes,
        index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        tuning_attempt_id="candidate-glitched",
    )

    verdict = _run_phase(c, 1, 1)

    assert verdict["accepted"] is False
    assert c.attempt_history == ()
    decision = c.last_attempt_decision
    assert decision["decision"] == STOP_EVIDENCE
    assert decision["reason"] == REASON_ATTEMPT_NOT_COMPARABLE
    assert decision["notes"] == [
        INTEGRITY_CHECK_SWEEP_HEARD,
        INTEGRITY_CHECK_SWEEP_SCHEDULE,
    ]


def test_no_floor_records_ungraded_and_a_floor_never_outranks_evidence():
    """The two grading-arm combinations nothing else drives (#2033 order).

    Row 1: a comparable capture on a speaker with no adopted floor is
    recorded UNGRADED — the no-floor status, not a refusal and not a claim.
    Row 2: a non-comparable capture on a speaker that HAS a floor still
    answers as a capture problem: the floor's presence must not promote
    grading past the evidence refusal. A mutation fusing the two conditions
    (refuse only when non-comparable AND floorless) survives every other
    case in the suite and fails only on this pair.
    """
    fakes = FakeSeams()
    c = _conductor(
        fakes,
        index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        tuning_attempt_id="candidate-a",
    )
    assert _run_phase(c, 1, 1)["accepted"] is True
    decision = c.last_attempt_decision
    assert decision["reason"] == ATTEMPT_REASON_NO_FLOOR
    assert decision["decision"] is None
    assert decision["improved"] is None
    assert decision["floor"] is None
    assert decision["basis_attempt_ids"] == ["candidate-a"]
    # Ungraded is recorded, not dropped: the attempt still enters history.
    assert [item.attempt_id for item in c.attempt_history] == ["candidate-a"]

    integrity = CaptureIntegrity(checks=(
        IntegrityCheck(INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_FAIL),
    ))
    floored_fakes = FakeSeams()
    floored_fakes.verify = lambda program: _verify_analysis(
        program, integrity=integrity,
    )
    floored = _verify_only_conductor(
        floored_fakes, tuning_attempt_id="candidate-b",
    )
    assert _run_phase(floored, 1, 1)["accepted"] is False
    decision = floored.last_attempt_decision
    assert decision["decision"] == STOP_EVIDENCE
    assert decision["reason"] == REASON_ATTEMPT_NOT_COMPARABLE
    assert floored.attempt_history == ()


def test_live_seam_refuses_improvement_when_verify_denominator_shrinks():
    history = (
        AttemptRecord(
            attempt_id="candidate-previous",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
            sitting_id=SESSION,
            integrity=AttemptIntegrity(comparable=True),
            grade_db=1.0,
            n_graded_bins=400,
        ),
    )
    fakes = FakeSeams()
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.6, n_graded_bins=200,
    )
    c = _verify_only_conductor(
        fakes,
        attempt_history=history,
        tuning_attempt_id="candidate-latest",
    )

    verdict = _run_phase(c, 1, 1)

    assert verdict["accepted"] is True
    decision = c.last_attempt_decision
    assert decision["decision"] == STOP_EVIDENCE
    assert decision["reason"] == REASON_GRADED_BINS_SHRANK
    assert decision["basis_attempt_ids"] == [
        "candidate-previous", "candidate-latest",
    ]


def test_live_seam_preserves_immediate_predecessor_basis():
    history = (
        AttemptRecord(
            attempt_id="candidate-early",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
            sitting_id=SESSION,
            integrity=AttemptIntegrity(comparable=True),
            grade_db=9.0,
        ),
        AttemptRecord(
            attempt_id="candidate-previous",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
            sitting_id=SESSION,
            integrity=AttemptIntegrity(comparable=True),
            grade_db=1.0,
        ),
    )
    fakes = FakeSeams()
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.6)
    c = _verify_only_conductor(
        fakes,
        attempt_history=history,
        tuning_attempt_id="candidate-latest",
    )

    verdict = _run_phase(c, 1, 1)

    assert verdict["accepted"] is True
    decision = c.last_attempt_decision
    assert decision["reason"] == REASON_IMPROVEMENT_ABOVE_FLOOR
    assert decision["basis_attempt_ids"] == [
        "candidate-previous", "candidate-latest",
    ]
    assert decision["improvement_db"] == pytest.approx(0.4)


def test_live_seam_refuses_a_claim_against_a_previous_measurement_journey():
    """Issue #2081, at the seam the household actually reaches.

    "Start over" preserves ``attempts_loop`` on purpose, so the second tune's
    VERIFY is graded against the first tune's — but the microphone was put
    down, re-placed and re-aimed in between, and the claim floor was measured
    with it bolted down (``captures/repeat-floor-20260731``). The predecessor
    below carries the session that measured it; this VERIFY carries its own.
    Before the fix the conductor answered ``improvement_above_floor`` with
    ``improvement_db 0.4`` and no record of the gap; the only thing that
    differs from the test above is which sitting the predecessor names.
    """
    history = (
        AttemptRecord(
            attempt_id="candidate-previous",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
            # A DIFFERENT capture session — the first tune's, not this one's.
            sitting_id="first_tune_verify_session",
            integrity=AttemptIntegrity(comparable=True),
            grade_db=1.0,
        ),
    )
    fakes = FakeSeams()
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.6)
    c = _verify_only_conductor(
        fakes,
        attempt_history=history,
        tuning_attempt_id="candidate-latest",
    )

    verdict = _run_phase(c, 1, 1)

    # The VERIFY itself still passes — this is a claim refusal, not a capture
    # rejection, and the household's speaker is not told its measurement failed.
    assert verdict["accepted"] is True
    decision = c.last_attempt_decision
    assert decision["decision"] == STOP_EVIDENCE
    assert decision["reason"] == "sitting_mismatch"
    assert decision["basis_attempt_ids"] == [
        "candidate-previous", "candidate-latest",
    ]
    # No unlicensed number survives to a renderer.
    assert decision["improvement_db"] is None
    assert decision["magnitude_db"] is None
    # The attempt is still BANKED — refusing the claim must not cost the
    # household the record, or the next tune has no predecessor either.
    assert [item.attempt_id for item in c.attempt_history] == [
        "candidate-previous", "candidate-latest",
    ]


def test_live_seam_stamps_this_session_as_the_new_attempts_sitting():
    """The stamp is the session that captured the sweep, not a constant."""
    fakes = FakeSeams()
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.6)
    c = _verify_only_conductor(fakes, tuning_attempt_id="candidate-latest")

    assert _run_phase(c, 1, 1)["accepted"] is True

    banked = c.attempt_history[-1]
    assert banked.attempt_id == "candidate-latest"
    assert banked.sitting_id == c.session_id
    assert banked.sitting_id  # never the empty "unrecorded" value


def test_the_banked_sitting_survives_the_durable_state_round_trip():
    """A stamp the persistence layer drops is a stamp that never fired.

    The whole #2081 hazard lives across a restart — the predecessor is read
    back out of the state file "Start over" preserved — so the field has to
    make the round trip, not just exist in memory.
    """
    record = AttemptRecord(
        attempt_id="candidate-a",
        metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        provenance=PROVENANCE_REALIZED,
        sitting_id="the_session_that_measured_it",
        integrity=AttemptIntegrity(comparable=True),
        grade_db=1.0,
    )
    assert record.to_dict()["sitting_id"] == "the_session_that_measured_it"

    restored = flow.attempt_history_from_state(
        {"attempts_loop": {"history": [record.to_dict()]}}
    )
    assert [item.sitting_id for item in restored] == [
        "the_session_that_measured_it",
    ]


def test_a_pre_2081_persisted_row_restores_as_unrecorded_not_as_a_match():
    """Every shipped speaker's history looks like this on the upgrade deploy.

    Two such rows must not compare equal as one sitting — the restore has to
    hand the kernel the value it refuses on, which is what makes the upgrade
    stop claiming rather than claim something it cannot support.
    """
    legacy_row = {
        "attempt_id": "candidate-old",
        "metric": flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        "provenance": PROVENANCE_REALIZED,
        "integrity": {"comparable": True, "reasons": []},
        "grade_db": 4.0,
    }
    restored = flow.attempt_history_from_state(
        {"attempts_loop": {"history": [legacy_row]}}
    )
    assert len(restored) == 1
    assert restored[0].sitting_id == ""


# --- happy path -----------------------------------------------------------------


def test_happy_path_walks_check_measure_apply_verify():
    fakes = FakeSeams()
    c = _conductor(fakes)
    assert c.current_phase == PHASE_CHECK

    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True
    assert fakes.played[0][0] == PHASE_CHECK
    assert len(fakes.published_checks) == 1
    assert c.current_phase == PHASE_MEASURE

    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert verdict["candidate_fingerprint"]
    # Two-stage commission D1 (PR-T3): the candidate is a PROPOSAL. Nothing
    # in this payload tells anything to apply it — the ``auto_apply: True``
    # literal that used to sit here is gone, and its absence is the pin.
    assert "auto_apply" not in verdict
    assert fakes.played[1][0] == PHASE_MEASURE
    assert len(fakes.published_candidates) == 1
    candidate = fakes.published_candidates[0]
    assert candidate.fingerprint == verdict["candidate_fingerprint"]
    # positive delay_us ⇒ tweeter earlier ⇒ tweeter delayed (W4 sign contract).
    assert candidate.alignment.delay_role == "tweeter"
    assert candidate.alignment.delay_us == pytest.approx(150.0)
    # MEASURE accepted but not applied ⇒ the host's own auto-apply is in
    # flight (machine-paced seconds, never a human control page).
    assert c.current_phase == PHASE_APPLYING

    # VERIFY is soft-held until the auto-apply completes (§5.2 auto-arm) —
    # the mechanism is unchanged; only the release trigger moved from a
    # human tap to the host's own auto-apply.
    with pytest.raises(CaptureBeginDeferred) as excinfo:
        c.authorize_begin(3, 3)
    assert excinfo.value.code == "awaiting_apply"

    # The host's auto-apply background thread finished successfully — this
    # is what jasper.web.correction_crossover_v2.handle_v2_apply's
    # observe_apply_success ultimately flips, read here through the seam.
    # (current_phase reads the conductor's own in-memory ``applied`` flag,
    # which only updates once authorize_begin actually re-checks the seam —
    # so it stays "applying" here until the VERIFY begin below observes it.)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.applied is True
    assert fakes.played[2][0] == PHASE_VERIFY
    assert c.verify_outcome == "pass"
    assert c.current_phase == PHASE_DONE


def test_a_capture_on_a_phase_without_a_consumer_is_refused_loudly():
    """A capture index mapped to a control-page phase is a wiring defect.

    The dispatch table refuses it as a typed error. The chains it replaced
    fell back to grading such a capture as post-apply VERIFY against empty
    priors — banking it durably as a tuning attempt, silently.
    """
    fakes = FakeSeams()
    c = _conductor(fakes, index_phase_map={1: PHASE_REVIEW})
    with pytest.raises(CrossoverV2FlowError):
        c.consume_capture(1, 1, _capture())
    # Refused before any analysis, banking, or verify grading ran.
    assert fakes.analyzed == []
    assert c.attempt_history == ()


def test_apply_gate_seam_releases_deferred_verify():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    with pytest.raises(CaptureBeginDeferred):
        c.authorize_begin(3, 3)
    # The apply-complete observation arrives through the seam (the host's
    # own auto-apply thread finishing — never a human tap).
    fakes.apply_done = True
    c.authorize_begin(3, 3)  # no longer deferred
    assert c.applied is True


def test_apply_failed_seam_refuses_the_deferred_verify_hold():
    """Owner ruling (2026-07-20): a TERMINAL auto-apply failure must not
    strand the phone on the deferred hold toward a dishonest capture_timeout —
    authorize_begin refuses outright with the real reason."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_failed_code = "apply_failed"
    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(3, 3)
    assert excinfo.value.code == "apply_failed"
    assert c.last_failure_code == "apply_failed"
    assert c.applied is False


def test_an_implausible_delay_never_renders_mic_placement_advice():
    """The copy separation the confidence demotion required (#2085's shape).

    Both rungs shared ``low_alignment_confidence`` until the burn-down, so the
    ONE sentence behind it — "Place the microphone about 1 m in front of the
    speaker at tweeter height" — was rendered for a confidently-WRONG delay
    too. That is the #2085 pathology exactly: a household whose microphone was
    never the problem, told to move it. Demoting the confidence rung without
    splitting the kinds would have left this rejection holding that sentence as
    its only voice.

    Pinned on the CONTENT, not on the code, because the defect was what the
    household read. The physics sentence must name the delay and must not
    instruct a mic move; and no live registry row may carry the retired code.
    """
    spec = REASON_REGISTRY["delay_implausible"]
    household = f"{spec.message} {spec.banner}".lower()
    assert "delay" in household
    for mic_advice in ("place the microphone", "tweeter height", "1 m in front"):
        assert mic_advice not in household, mic_advice
    # The retired code is gone from the registry, so nothing can route back to
    # the shared sentence.
    assert "low_alignment_confidence" not in REASON_REGISTRY


def test_low_alignment_confidence_accepts_and_banks_a_reservation():
    """The nanny burn-down, at the trust floor.

    It REFUSED here and spent a retry until then. §4 names its exact
    category as excluded — "confidence heuristics ... is provenance, not a
    gate" — and the one live bench datum undercut it: two captures at ~0.677
    confidence, one accepted and one refused 58 s apart, so confidence was
    never the discriminator the reused reason code claimed it was.

    Transformed rather than deleted, exactly as the ripple gate below was: the
    threshold and its exclusive comparator are still pinned, and only the
    consequence of crossing it changed."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program,
        alignment=_alignment(confidence=ALIGNMENT_CONFIDENCE_TRUST_FLOOR - 0.1),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    # No reason code at all — the structural difference from the refusal this
    # replaces, and the same shape the ripple disclosure asserts below.
    assert not verdict.get("code")
    # The candidate the refusal used to prevent now exists and is published.
    assert fakes.published_candidates
    assert c.candidate is not None
    # The measured value rides WITH the floor it was judged against, so a later
    # constant change cannot retro-caption a banked reservation.
    reservation = c.measure_alignment_reservation
    assert reservation is not None
    assert reservation["confidence"] == pytest.approx(
        ALIGNMENT_CONFIDENCE_TRUST_FLOOR - 0.1
    )
    assert reservation["trust_floor"] == ALIGNMENT_CONFIDENCE_TRUST_FLOOR


def test_alignment_confidence_at_the_trust_floor_banks_nothing():
    """The floor is still an exclusive lower bound (`<`, not `<=`).

    Exactly-at-floor is trusted, so it reserves nothing — the boundary the
    refusal used to be pinned at, kept on the disclosure."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program,
        alignment=_alignment(confidence=ALIGNMENT_CONFIDENCE_TRUST_FLOOR),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.measure_alignment_reservation is None


def test_uncalibrated_measure_accepts_and_banks_a_reservation():
    """Audit gauntlet 5a, at the conductor: disclose, never block.

    Same shape as the alignment-confidence reservation above — the capture
    is ACCEPTED and carries an honest reservation instead of refusing, and
    the fact is read off ``analysis.mic_calibrated`` alone, never guessed
    from ``mic_tier`` (a resolved-but-unrecognized-model mic ALSO reports the
    conservative "phone" tier while genuinely being calibrated — see
    ``tests/test_correction_crossover_v2_endpoints.py``'s bare-curve case)."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program, mic_calibrated=False)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert not verdict.get("code")
    assert fakes.published_candidates
    assert c.candidate is not None
    assert c.measure_calibration_reservation is True


def test_a_calibrated_measure_banks_no_calibration_reservation():
    """The converse — the disclosure's own "clean measurement" counterpart."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program, mic_calibrated=True)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.measure_calibration_reservation is None


def test_no_alignment_estimate_skips_the_confidence_gate():
    """A trims-only candidate (no alignment estimate at all) is never
    confidence-gated — same condition the former nudge used."""
    from dataclasses import replace

    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverAlignment,
    )

    fakes = FakeSeams()

    def _measure_no_alignment(program):
        return replace(_measure_analysis(program), alignment=None)

    fakes.measure = _measure_no_alignment
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert fakes.published_candidates[0].alignment == MeasuredCrossoverAlignment()


def test_implausible_delay_rejects_measure_even_at_high_confidence():
    """Fix 3: a confidently-WRONG delay (high GCC confidence at the wrong
    lag — a real hardware failure mode, not a hypothetical one) must still
    be rejected when its magnitude falls outside the preset's declared
    ``delay_range_ms`` search bound (``_two_way_preset``'s [0.05, 0.30] ms =
    [50, 300] us) rather than auto-applying a physically implausible
    correction. A delay inside that declared bound is unaffected.

    **It has its own code since the confidence rung was demoted.** The two
    shared ``low_alignment_confidence``, so this physics rejection rendered a
    sentence about mic placement — the #2085 pathology, aimed at a household
    whose microphone was never the problem. A physics fact and a prior are
    different answers and now say different things."""
    fakes = FakeSeams()
    # High confidence (clears ALIGNMENT_CONFIDENCE_TRUST_FLOOR) but a
    # magnitude (631 us) more than double the declared 300 us upper bound —
    # mirrors the confidently-implausible -631 us hardware failure.
    fakes.measure = lambda program: _measure_analysis(
        program, alignment=_alignment(delay_us=-631.0, confidence=0.9),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is False
    assert verdict["code"] == "delay_implausible"
    assert not fakes.published_candidates
    assert c.candidate is None
    assert c.current_phase == PHASE_MEASURE

    # A delay inside the declared bound (same high confidence) is accepted.
    fakes2 = FakeSeams()
    fakes2.measure = lambda program: _measure_analysis(
        program, alignment=_alignment(delay_us=-200.0, confidence=0.9),
    )
    c2 = _conductor(fakes2)
    _run_phase(c2, 1, 1)
    verdict2 = _run_phase(c2, 2, 2)
    assert verdict2["accepted"] is True


# --- measurement-honesty disclosure G1: predicted-ripple reservation --------------
#
# These four tests pinned the OPPOSITE behaviour until the owner's 2026-08-03
# ruling (#2087): crossing the threshold refused the capture and reused
# ``low_alignment_confidence``. They are transformed rather than deleted, so
# every boundary the old gate was pinned at is still pinned — the threshold,
# its exclusive ``>``, and the trims-only skip all survive; only the
# consequence of crossing it changed from a refusal to a disclosure.


def test_predicted_ripple_over_threshold_accepts_and_banks_a_reservation():
    """Owner ruling #2087: a candidate whose OWN predicted ripple is worse
    than the calibration corpus — mirrors the 2026-07-22 corrupted-phone-chain
    hardware evidence (27.316 dB at a confidence that cleared
    ALIGNMENT_CONFIDENCE_TRUST_FLOOR) — now PROCEEDS carrying an honest
    reservation instead of refusing.

    The refusal this replaces told a household with a correctly placed
    microphone to move it (#2085) and killed the session on the attempt meter
    (#2086). What the capture measured is unchanged; what the household is
    told about it is the whole change."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=27.316,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    # No reason code at all — an accepted verdict carries none, which is the
    # structural difference from the refusal this replaces.
    assert not verdict.get("code")
    # The measured value rides WITH the threshold it was judged against, so a
    # later constant change cannot retro-caption a banked reservation.
    assert c.measure_ripple_reservation == {
        "predicted_ripple_db": 27.316,
        "threshold_db": MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB,
    }


def test_predicted_ripple_disclosure_emits_its_own_event(caplog):
    """The disclosure has a stable ``event=`` line of its own, at WARNING.

    ``guard=`` on the per-capture diag is one field on a line that fires for
    every capture; this is the line an operator counts or alerts on."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=15.244,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert "event=correction.crossover_v2_ripple_disclosed" in caplog.text
    assert "predicted_ripple_db=15.244" in caplog.text
    assert "threshold_db=15.0" in caplog.text
    assert any(
        record.levelno == logging.WARNING
        and "crossover_v2_ripple_disclosed" in record.getMessage()
        for record in caplog.records
    )


def test_predicted_ripple_well_under_threshold_banks_nothing():
    """A representative value from the 2026-07-22 clean-corpus worst case
    passes with NO reservation — the threshold sits well above it, and a clean
    capture must say nothing rather than reassure. See
    ``MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB``'s comment for the corpus
    composition AND range; neither is restated here per issue #2015 (the
    range drifted the same way the count once did)."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=9.0,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.measure_ripple_reservation is None


def test_predicted_ripple_threshold_boundary_exact_is_silent_just_above_discloses():
    """The threshold is an exclusive upper bound (``>``, not ``>=``) — exactly
    at it banks nothing, matching this file's other boundary comparators
    (e.g. test_alignment_confidence_at_the_trust_floor_is_trusted). Both sides
    accept now; the boundary decides whether anything is DISCLOSED."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert c.measure_ripple_reservation is None

    fakes2 = FakeSeams()
    fakes2.measure = lambda program: _measure_analysis(
        program,
        predicted_ripple_db=MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB + 0.01,
    )
    c2 = _conductor(fakes2)
    _run_phase(c2, 1, 1)
    verdict2 = _run_phase(c2, 2, 2)
    assert verdict2["accepted"] is True
    assert c2.measure_ripple_reservation is not None


def test_predicted_ripple_disclosure_skips_when_no_alignment():
    """A trims-only candidate (no alignment estimate at all) banks no ripple
    reservation — the same skip condition the confidence floor and Fix 3 use
    (see test_no_alignment_estimate_skips_the_confidence_gate), kept through
    the conversion because a reservation about a candidate built without an
    alignment estimate would describe something else."""
    from dataclasses import replace

    fakes = FakeSeams()
    fakes.measure = lambda program: replace(
        _measure_analysis(program, predicted_ripple_db=27.316), alignment=None,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.measure_ripple_reservation is None


def test_predicted_ripple_reservation_clears_when_a_retake_is_clean():
    """A re-measured MEASURE that comes back clean CLEARS the reservation.

    The reservation describes the ACCEPTED capture, so it must not outlive the
    capture it was about — the same reset-at-the-top-of-``_measure_verdict``
    lifecycle ``_last_measure_guard`` has. Pinned because the failure mode is
    silent: a stale reservation would caption a clean measurement with a
    caveat about a capture the household already replaced."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=27.316,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    assert c.measure_ripple_reservation is not None

    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=9.0,
    )
    c._rearm_measure_after_transient()
    _run_phase(c, 2, 2)
    assert c.measure_ripple_reservation is None


def test_measure_priors_thread_declared_delay_magnitudes_without_applied_target():
    """T2 threads declared magnitudes even before a target is applied.

    The reference preset declares [50, 300] us; Fix 3's 100 us margin makes
    [0, 400] us. ``delay_target_driver`` may legitimately be absent on a fresh
    preset; the drift-corrected physical peak gap later orients the signed
    lobe, so that must not disable T2.
    """
    c = _conductor(FakeSeams())
    expected = (0.0, 400.0)
    assert alignment_delay_search_bounds_us(_preset()) == expected
    assert c._measure_priors().alignment_delay_bounds_us == expected

    raw = _two_way_preset()
    raw["crossover_regions"][0]["delay_target_driver"] = None
    fresh = ActiveSpeakerPreset.from_mapping(raw)
    assert alignment_delay_search_bounds_us(fresh) == expected


def _applied_profile(*, woofer_delay_ms, tweeter_delay_ms, in_snapshot=True):
    """An applied Layer-A record carrying one per-role delay pair."""
    corrections = {
        "woofer": {"gain_db": -3.0, "delay_ms": woofer_delay_ms, "inverted": False},
        "tweeter": {"gain_db": 0.0, "delay_ms": tweeter_delay_ms, "inverted": False},
    }
    profile = {"status": "applied", "corrections": corrections}
    if in_snapshot:
        profile["recomposition_snapshot"] = {"corrections": corrections}
    return profile


@pytest.mark.parametrize(
    ("woofer_ms", "tweeter_ms", "expected_us"),
    [
        # Positive ⇒ the tweeter is the delayed role, matching
        # `alignment_to_candidate_fields`'s own fold in the other direction.
        (0.0, 0.0596, 59.6),
        (0.211, 0.0, -211.0),
        (0.0, 0.0, 0.0),
    ],
    ids=["tweeter_delayed", "woofer_delayed", "no_delay_applied"],
)
def test_applied_profile_delay_reads_back_in_the_analysis_sign_frame(
    woofer_ms, tweeter_ms, expected_us,
):
    """#2617's carry-forward value, and the sign contract it shares.

    The applied profile stores a non-negative magnitude per role; the analysis
    speaks one signed ``(D_woofer - D_tweeter)``. This reader is the inverse of
    ``alignment_to_candidate_fields``, so a round trip through both must be the
    identity — that is what stops the two halves of one convention drifting.
    """
    got = planning.applied_profile_delay_us(
        _applied_profile(woofer_delay_ms=woofer_ms, tweeter_delay_ms=tweeter_ms),
        woofer_role="woofer", tweeter_role="tweeter",
    )
    assert got == pytest.approx(expected_us)

    # The round trip, against the forward fold this is the inverse of.
    analysis = types.SimpleNamespace(
        alignment=types.SimpleNamespace(
            delay_us=got, status=ALIGNMENT_OK, polarity="normal",
        ),
    )
    magnitude, role, _polarity = alignment_to_candidate_fields(
        analysis, roles=("woofer", "tweeter"),
    )
    assert magnitude == pytest.approx(abs(expected_us))
    assert role == ("tweeter" if expected_us >= 0.0 else "woofer")


def test_a_mirror_only_profile_still_yields_the_delay_it_plays():
    """S-SF1: the era rule is the OWNER's, not a second stricter one.

    A profile whose corrections live only in the top-level mirror — the older
    on-disk era — is still a speaker with a delay in its graph. This reader
    once traversed ``recomposition_snapshot`` itself and returned ``None``
    here, which committed no delay on a speaker that plays one and labelled it
    "the design asks for none". It now consumes
    ``baseline_profile.profile_driver_corrections``, so it inherits the mirror
    fallback ``profile_linearization`` and ``commanded.profile_graph_summation``
    already read through, and a future era reaches all three at once.
    """
    mirror_only = _applied_profile(
        woofer_delay_ms=0.0, tweeter_delay_ms=0.0596, in_snapshot=False,
    )
    assert "recomposition_snapshot" not in mirror_only
    assert planning.applied_profile_delay_us(
        mirror_only, woofer_role="woofer", tweeter_role="tweeter",
    ) == pytest.approx(59.6)


def test_the_snapshot_wins_when_a_profile_carries_both_copies():
    """Authoritative, not merely present — the owner's preference, inherited.

    A profile written by the current era carries both copies; the snapshot is
    what ``recompose_applied_baseline_yaml`` re-emits from, so it is the delay
    the speaker plays. Pinned with the two copies DISAGREEING, which is the
    only shape in which the preference is observable.
    """
    profile = _applied_profile(woofer_delay_ms=0.0, tweeter_delay_ms=0.0596)
    profile["corrections"] = {
        "woofer": {"gain_db": -3.0, "delay_ms": 0.5, "inverted": False},
        "tweeter": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
    }
    assert planning.applied_profile_delay_us(
        profile, woofer_role="woofer", tweeter_role="tweeter",
    ) == pytest.approx(59.6)


@pytest.mark.parametrize(
    ("profile", "why"),
    [
        (None, "nothing has been commissioned"),
        ({"status": "applied"}, "no corrections in either copy"),
        (
            {"recomposition_snapshot": {"corrections": {
                "tweeter": {"gain_db": 0.0, "delay_ms": 0.0596, "inverted": False},
            }}},
            "a role the corrections never mention is unreadable, not zero",
        ),
        (
            {"recomposition_snapshot": {"corrections": {
                "woofer": {"delay_ms": float("nan")},
                "tweeter": {"delay_ms": 0.0596},
            }}},
            "a non-finite delay is not a delay",
        ),
        (
            {"recomposition_snapshot": {"corrections": {
                "woofer": {"delay_ms": True}, "tweeter": {"delay_ms": 0.0},
            }}},
            "a JSON boolean is not a numeric delay",
        ),
    ],
    ids=["absent", "no_corrections", "missing_role", "non_finite", "boolean"],
)
def test_an_unreadable_applied_delay_is_none_and_never_a_guessed_zero(profile, why):
    """``None`` and ``0.0`` are different facts and must stay different.

    ``0.0`` is "this speaker plays no relative delay"; ``None`` is "nobody can
    say what it plays". The low-SNR refusal commits the same number either way
    and records a DIFFERENT objective, so collapsing them here would make a
    persisted candidate claim the design asks for no delay on a speaker nobody
    could read.

    A role present without ``delay_ms`` is deliberately NOT in this list: the
    profile records a magnitude only on whichever role is delayed, so its
    absence there is a statement of zero — the same reading
    ``commanded.profile_graph_summation`` takes of the same field.
    """
    assert planning.applied_profile_delay_us(
        profile, woofer_role="woofer", tweeter_role="tweeter",
    ) is None, why


def test_a_role_named_without_a_delay_reads_as_zero_not_unreadable():
    """The counterpart of the row above, stated rather than implied."""
    assert planning.applied_profile_delay_us(
        {"recomposition_snapshot": {"corrections": {
            "woofer": {"gain_db": -3.0, "inverted": False},
            "tweeter": {"gain_db": 0.0, "delay_ms": 0.0596, "inverted": False},
        }}},
        woofer_role="woofer", tweeter_role="tweeter",
    ) == pytest.approx(59.6)


def test_measure_priors_carry_the_applied_alignment_and_no_other_phase_does(
    monkeypatch,
):
    """The seam: the session reads Layer-A state, the analysis is handed it.

    Contract #4 — the analysis is a pure function of (program, WAV, priors) —
    is what makes this a prior rather than a read from inside the analyzer.
    And MEASURE is the only phase that commits an alignment, so it is the only
    phase told what the speaker already plays: handing it to VERIFY would put
    the current answer inside the comparison meant to be independent of it.
    """
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: _applied_profile(
            woofer_delay_ms=0.0, tweeter_delay_ms=0.0596,
        ),
    )
    c = _conductor(FakeSeams())

    applied = c._measure_priors().applied_alignment
    assert applied is not None and applied.delay_us == pytest.approx(59.6)
    for factory in (
        c._check_priors, c._verify_priors, c._cloud_priors,
        c._lateral_priors, c._entry_baseline_priors,
    ):
        assert factory().applied_alignment is None, factory.__name__


@pytest.mark.parametrize(
    ("loader", "expected_present", "why"),
    [
        (lambda *a, **k: None, False, "nothing applied ⇒ the design's own answer"),
        (
            lambda *a, **k: {"status": "applied", "recomposition_snapshot": {}},
            True,
            "a graph IS applied and its record does not say what it plays",
        ),
    ],
    ids=["nothing_applied", "applied_but_unreadable"],
)
def test_the_session_separates_nothing_applied_from_unreadably_applied(
    monkeypatch, loader, expected_present, why,
):
    """S-SF1's disclosure: the two cases commit the same delay, not the same claim.

    Both end at "commit no delay", and the selector gives them different
    objectives — but only if the seam preserves the distinction on the way in.
    ``None`` says the design's answer stands; an ``AppliedAlignment`` with no
    delay says something is playing that nobody could read.
    """
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        loader,
    )
    applied = _conductor(FakeSeams())._measure_priors().applied_alignment

    assert (applied is not None) is expected_present, why
    if applied is not None:
        assert applied.delay_us is None


def test_an_unreadable_applied_profile_never_fails_a_measure_analysis(monkeypatch):
    """A structurally-wrong state file reads as "nothing applied", not a crash.

    The consumer's fail-safe is "commit no delay", which is a worse tune and a
    working speaker; raising here would lose the whole capture over a fact one
    refusal path consults.
    """
    def _raise(*_a, **_k):
        raise ValueError("hand-edited state file")

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        _raise,
    )
    c = _conductor(FakeSeams())
    assert c._measure_priors().applied_alignment is None


def test_measure_priors_compose_configured_path_from_ssots_and_freeze_input():
    raw = _two_way_preset()
    raw["crossover_regions"][0]["upper_polarity"] = "inverted"
    preset = ActiveSpeakerPreset.from_mapping(raw)
    woofer = flow.CrossoverSection(6000.0, 4, False)
    tweeter = flow.CrossoverSection(300.0, 4, True)
    supplied = {"woofer": [woofer], "tweeter": [tweeter]}
    c = _conductor(
        FakeSeams(), source_preset=preset,
        measurement_protection_sections_by_role=supplied,
    )
    supplied["woofer"].clear()
    supplied["tweeter"] = [woofer]
    priors = c._measure_priors()
    # The measurement kernel may not import this package, so priors carry an
    # evaluated `freqs -> complex response` rather than CrossoverSections. The
    # transfer must still come from the sections the conductor copied at
    # construction, NOT from the caller's list mutated above.
    freqs = np.array([100.0, 1000.0, 8000.0])
    assert priors.measurement_protection_response_by_role.keys() == {
        "woofer", "tweeter",
    }
    for role, section in (("woofer", woofer), ("tweeter", tweeter)):
        np.testing.assert_allclose(
            priors.measurement_protection_response_by_role[role](freqs),
            crossover_response_complex(freqs, (section,)),
        )
    for role, sections in flow.sections_by_role(preset.crossover_regions).items():
        np.testing.assert_allclose(
            priors.configured_crossover_response_by_role[role](freqs),
            crossover_response_complex(freqs, sections),
        )
    assert priors.configured_polarity_sign_by_role == {"woofer": 1, "tweeter": -1}
    # Pins the WIRING of §4.2's candidate-required mask: every role's band must
    # be derived, and must cover the overlap band it is unioned with (a None
    # derivation silently returns the policy to the whole driven band).
    overlap = overlap_band_hz(priors.crossover_fc_hz)
    required = priors.candidate_required_band_hz_by_role
    assert required is not None and required.keys() == {"woofer", "tweeter"}
    for role, (lo, hi) in required.items():
        assert lo <= overlap[0] and hi >= overlap[1], role
    legacy = _conductor(FakeSeams())._measure_priors()
    assert legacy.measurement_protection_response_by_role is None
    assert legacy.configured_crossover_response_by_role is None
    assert legacy.configured_polarity_sign_by_role is None
    assert legacy.candidate_required_band_hz_by_role is None


def test_an_uncomposed_protected_neutral_capture_is_refused_at_the_seam():
    """The fitter's branch-input invariant, pinned where it actually runs.

    Pinned at ``_build_measure_candidate``, NOT ``_fit_linearization``: the
    2026-08-05 panel (correctness B1 / hearing-safety SF2) showed the guard
    living inside the fit was swallowed three lines later by
    ``_build_candidate``'s SF2 degrade handler, which catches ``ValueError``,
    and the session committed a reviewable, Apply-able trims-only candidate. A
    direct call to the private method cannot see that. It also has to refuse
    the trims-only path: the emitter runs with region polarity OFF here and
    §4.2 restores ``sign_c`` offline, so trim/delay/polarity would be solved in
    a different convention from the applied graph.
    """
    from jasper.audio_measurement.program import build_measure_program
    protection = {"woofer": [flow.CrossoverSection(6000.0, 4, False)],
                  "tweeter": [flow.CrossoverSection(300.0, 4, True)]}
    program = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0},
        [RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
         RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0))],
    )
    c = _conductor(FakeSeams(), measurement_protection_sections_by_role=protection)
    c._measure_program = program
    analysis = _eligible_measure_analysis(program)
    assert analysis.configured_path_composed is False

    # THE SEAM: no candidate is built, so none can be committed or applied.
    with pytest.raises(ValueError, match="reached the fitter uncomposed"):
        c._build_measure_candidate(analysis, None)
    assert c.candidate is None

    # …and it does NOT fire once the composition ran, nor on a legacy conductor
    # (whose emitter puts the shoulders into the audio itself).
    composed = dataclasses.replace(analysis, configured_path_composed=True)
    assert c._build_candidate(composed) is not None
    legacy = _conductor(FakeSeams())
    legacy._measure_program = program
    assert legacy._build_candidate(analysis) is not None


def test_the_tier_chooser_quotes_the_stage_1_the_session_actually_runs():
    """#2098's pattern: one producer owns the capture-count fact.

    `prepare_v2_session` runs stage 1 with `STAGE1_INCLUDES_CLOUD_MEASURE`, and
    before this the chooser still read `shape.measure_capture_target` — the
    cloud-inclusive 10 (Full) / 5 (Express) — plus cloud-inclusive minutes. The
    household was told it was starting a ten-capture walk that the session then
    did not take. Both surfaces now derive from the same flag.
    """
    info = flow.tier_display_info()
    assert flow.STAGE1_INCLUDES_CLOUD_MEASURE is False
    # DERIVED from the surviving stage-1 flag rather than hardcoded, so the
    # chooser is pinned to whatever stage 1 actually runs and this test moves
    # with a flag flip instead of going stale — which it has done twice now,
    # for R17's lateral flip on and the 2026-08-18 pause back off, before the
    # walk was retired outright. No stage-1 plan builds a lateral group any
    # more, so that term is gone rather than held at a flag-derived 0; only
    # #2291's entry baseline is still flag-driven, and it's on, so this is 3.
    expected_stage1 = 2 + (1 if flow.STAGE1_INCLUDES_ENTRY_BASELINE else 0)
    # The tiers genuinely no longer differ in stage 1 — so the numbers must not
    # imply that they do. (The lateral walk would not change that: it is the
    # ANCHOR's own robustness sample, not a spatial cloud, so it is the same
    # poses at either tier.)
    assert info["full"]["stage1_captures"] == expected_stage1
    assert info["express"]["stage1_captures"] == expected_stage1
    # Stage 2 is where they still differ, and the chooser copy says so.
    # 6 since the 2026-08-24 geometry ruling put the design axis into the
    # post-apply pose set (``CLOUD_VERIFY_POSE_PROMPTS``): VERIFY's anchor plus
    # five prompted poses.
    assert info["full"]["stage2_captures"] == 6
    assert info["express"]["stage2_captures"] == 1
    for tier, detail in info.items():
        assert detail["capture_target"] == (
            detail["stage1_captures"] + detail["stage2_captures"]
        ), tier
        # Honest minutes: a real duration, bounded by the module's OWN
        # per-entry wall-clock ceiling for the captures this build plans, so
        # the bound moves with the plan instead of going stale. It was a flat
        # ``<= 10`` written for a two-capture stage 1; R17's walk makes Full's
        # honest quote 12 min (6 before the flip), which that bound would have
        # failed for being TRUE. The sharp anti-cloud guards are the two
        # assertions above — the flag itself and the flag-derived stage-1
        # count; this one only checks the promise tracks the plan rather than
        # being a hand-written figure.
        assert 0 < detail["estimated_minutes"] <= (
            detail["capture_target"] * flow.WALL_CLOCK_CEILING_PER_ENTRY_S / 60.0
        ), tier

    # The degraded fallback answers with the SAME numbers, so a failure in the
    # memoized build cannot quietly restore the cloud-inclusive figures.
    with pytest.MonkeyPatch.context() as mp:
        # The memo lives with ``tier_display_info`` in ``crossover_v2.capture_plan``;
        # patching the flow's re-export would rebind a name nothing reads.
        mp.setattr(
            capture_plan, "_tier_display_info_cached",
            lambda: (_ for _ in ()).throw(ValueError("forced")),
        )
        degraded = flow.tier_display_info()
    for tier, detail in degraded.items():
        assert detail["stage1_captures"] == info[tier]["stage1_captures"], tier
        assert detail["capture_target"] == info[tier]["capture_target"], tier


def test_measure_program_gains_back_off_from_caps():
    """W2 gate: the solver backs off ≥0.01 dB from exact per-driver caps."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    program = c.program_for_phase(PHASE_MEASURE)
    sweep_t = program.segment("sweep_t")
    # tweeter cap −65, session −20 ⇒ ceiling −45 − backoff.
    assert sweep_t.gain_db == pytest.approx(-45.0 - GAIN_CAP_BACKOFF_DB)
    assert sweep_t.effective_peak_dbfs <= CAPS["tweeter"] - GAIN_CAP_BACKOFF_DB + 1e-9
    # Woofer's solved gain is far under its cap and passes through unchanged.
    assert program.segment("sweep_w").gain_db == pytest.approx(-11.0)
    # MEASURE opens with the pilot pair riding the woofer's solved level.
    pilot_hi = program.segment("pilot_woofer_hi")
    assert pilot_hi.gain_db == pytest.approx(-11.0)
    assert program.segment("pilot_woofer_lo").gain_db == pytest.approx(-21.0)


def test_back_off_gain_at_cap():
    assert back_off_gain(-45.0, -20.0, -65.0) == pytest.approx(-45.01)
    assert back_off_gain(-50.0, -20.0, -65.0) == pytest.approx(-50.0)


def test_conductor_threads_geometry_and_result_to_analyze():
    """The declared driver spacing + prescribed 1 m mic distance reach the
    analyze seam (so the §3.2 parallax correction is live, not dead config),
    and the WHOLE CaptureResult crosses it (the production binding resolves
    the mic calibration from result.setup/device)."""
    from jasper.audio_measurement.program_analysis import MeasurementGeometry

    fakes = FakeSeams()
    c = _conductor(fakes)  # driver_spacing_m=0.15
    result = _capture()
    c.authorize_begin(1, 1)
    c.on_armed()
    c.consume_capture(1, 1, result)
    assert len(fakes.analyzed) == 1
    phase, _prog_phase, seen_result, _priors, geometry = fakes.analyzed[0]
    assert phase == PHASE_CHECK
    assert seen_result is result  # the CaptureResult itself, not just bytes
    assert isinstance(geometry, MeasurementGeometry)
    assert geometry.driver_spacing_m == pytest.approx(0.15)
    # This literal is the only tripwire for
    # ``crossover_v2_flow.MEASUREMENT_DISTANCE_M``, which nothing in the tree
    # imports — importing it here would make the assertion pass at any value.
    assert geometry.mic_distance_m == pytest.approx(1.0)
    assert geometry.parallax_us() > 0.0


