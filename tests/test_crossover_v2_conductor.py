# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a conductor orchestration: the CHECK→MEASURE→APPLYING(auto)→VERIFY walk.

Fake-seam state walk per docs/historical/crossover-measurement-productization-design.md
§5/§6 W5a: the happy path, each §5.10 failure template, the deferred-VERIFY
release on apply, session-death volume abandon, the needs_recovery gate (W2
ruling), resume-skips-accepted-phases, and new-session-invalidates-evidence.
All seams (playback, analysis, publish, apply gate/failure) are injected
fakes — no relay, no DSP, no audio.

Owner ruling (2026-07-20): the conductor no longer waits for a human tap to
observe apply — ``fakes.apply_done = True`` / ``fakes.apply_failed_code``
simulate the HOST's own auto-apply (fired from a trusted MEASURE accept)
completing or failing, read through the ``apply_complete``/``apply_failed``
seams exactly as the real host wires them
(jasper.web.correction_crossover_v2_wired.build_v2_wired_run_and_consume). The
conductor itself never performs the apply — see
test_correction_crossover_v2_endpoints.py for the host-level auto-apply
trigger + background-thread wiring.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import re
import types
from dataclasses import replace
from typing import Any

import numpy as np
import pytest
import yaml

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import capture_plan
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_VERIFY,
    REFERENCE_MARK_DESIGN_AXIS,
)
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2 import accountability
from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2 import planning
from jasper.active_speaker.crossover_v2.intervention import (
    compose_sigma_db as _compose_sigma_db,
)
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
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    DELTA_PROBE_VERDICTS,
    VERDICT_FRAME_MISMATCH,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED,
    VERDICT_MODEL_ERROR,
    VERDICT_SAFETY_ONLY,
    VERDICT_UNAVAILABLE,
)
from jasper.active_speaker.crossover_v2.attempt_grading import (
    ATTEMPT_REASON_NO_FLOOR,
    PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
    PHASE_REVIEW,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    DELTA_PROBE_REASON_BY_VERDICT,
    REASON_CORRECTION_MODEL_ERROR,
    REASON_CORRECTION_ROLLBACK_FAILED,
    REASON_CORRECTION_UNSAFE_RESULT,
    REASON_CLOUD_GEOMETRY_LOCKED,
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    REASON_RELAY_TIMEOUT,
    TRANSIENT_AUTO_RETRY_CODES,
    locate_failed_diagnosis,
)
from jasper.active_speaker.crossover_v2_flow import (
    ALIGNMENT_CONFIDENCE_TRUST_FLOOR,
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_COUNTDOWN_S,
    AUTO_ADVANCE_ON_APPLY,
    AUTO_ADVANCE_TAP,
    CAPTURE_ENTRY_MARGIN_MS,
    CAPTURE_PLAN_MAX_ATTEMPTS,
    CLOUD_CLOSE_AWAITING_CONFIRM,
    CLOUD_GEOMETRY_RETRY_PROMPTS,
    CLOUD_POSITION_PROMPTS,
    CLOUD_RETAKE_ALLOWANCE,
    CLOUD_WALK_SHAPE_TAIL,
    CLOUD_WALK_SHAPE_TAIL_POST_APPLY,
    DEFAULT_CLOUD_MEASURE_POSITIONS,
    DEFAULT_CLOUD_VERIFY_POSITIONS,
    GAIN_CAP_BACKOFF_DB,
    GEOMETRY_RETRY_OFFSET_CM,
    GEOMETRY_RETRY_POSITIONS,
    LINEARIZATION_MIN_PAIRED_OCCURRENCES,
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    MAX_CLOUD_MEASURE_POSITIONS,
    MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB,
    MIN_CLOUD_MEASURE_POSITIONS,
    MIN_CLOUD_OFFSET_CM,
    MIN_CLOUD_VERIFY_POSITIONS,
    POSITION_ROLE_ONAX,
    POSITION_ROLES,
    PILOT_LEVEL_DELTA_DB,
    PILOT_SNR_UNUSABLE_DB,
    CLAIM_FAIL,
    CLAIM_NOT_EVALUATED,
    CLAIM_NO_PER_BRANCH_CAPTURE,
    CLAIM_PASS,
    verify_absolute_tolerance_db,
    REVERIFY_NO_REWALK_HEADLINE,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    TIER_EXPRESS,
    WIDE_OFFSET_MIN_CM,
    TIER_FULL,
    TIER_REMOTE,
    VERIFY_ANCHOR_HOLD_MESSAGE,
    CrossoverV2Session,
    CrossoverV2FlowError,
    V2PlanShape,
    _analysis_json,
    _program_duration_ms,
    _worst_pilot_snr_db,
    alignment_delay_search_bounds_us,
    alignment_to_candidate_fields,
    _min_positions_for_two_wide_offsets,
    _pose,
    back_off_gain,
    build_v2_capture_plan,
    build_v2_cloud_index_phase_map,
    build_v2_session_spec,
    build_v2_verify_capture_plan,
    build_v2_verify_session_spec,
    cloud_capture_target,
    cloud_plan_max_attempts,
    cloud_geometry_retry_reach_cm,
    cloud_walk_reach_cm,
    cloud_walk_shape,
    courtesy_prelude_for_phase,
    express_cloud_measure_positions,
    format_position_distance,
    resolve_plan_shape,
    spec_report_for_predicted_sum,
    session_wall_clock_ceiling_s,
    tier_display_info,
)
from jasper.active_speaker.branch_chain import crossover_response_complex
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement import gating
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import KIND_COURTESY_TONE, RoleBand
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_OK,
    CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB,
    CHANNEL_MAP_MIN_ISOLATION_DB,
    INTEGRITY_CHECK_SWEEP_HEARD,
    INTEGRITY_CHECK_SWEEP_SCHEDULE,
    INTEGRITY_FAIL,
    INTEGRITY_NOT_EVALUATED,
    AlignmentEstimate,
    CaptureIntegrity,
    CrossoverCandidate,
    DriftEstimate,
    GainPlan,
    IntegrityCheck,
    ProgramAnalysis,
    SegmentLocation,
    predicted_branch_sum,
    realized_branch_level_match,
    solve_branch_trims,
    summed_model_residual_delay_us,
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
            # A DIFFERENT relay session — the first tune's, not this one's.
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
    strand the phone on the deferred hold toward a dishonest relay_timeout —
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


# --- §5.10 failure templates ------------------------------------------------------


def test_clipped_measure_is_transient_auto_retry_with_quieter_program():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    gain_before = c.program_for_phase(PHASE_MEASURE).segment("sweep_w").gain_db

    fakes.measure = lambda program: _measure_analysis(program, clipped=True)
    verdict = _run_phase(c, 2, 2)
    assert verdict == {
        "accepted": False,
        "code": "clipped",
        "template": "silent_auto_retry",
        "reason": REASON_REGISTRY["clipped"].banner,
        "banner": REASON_REGISTRY["clipped"].banner,
        "auto_retry": True,
        # See the same key in
        # `test_low_alignment_confidence_rejects_measure_before_building_candidate`
        # — the pilot evidence rides every rejection (#2085), not only the
        # codes whose copy currently branches on it.
        "pilot_heard": None,
        # The honest per-position count rides EVERY verdict (#2086 item 2).
        # This rejection was the slot's PLANNED capture, so nothing is spent
        # yet and all three extras are still on offer.
        "attempts": {
            "used": 0, "allowed": 3, "left": 3,
            "by_speaker": 0, "by_household": 0,
        },
    }
    # The automatic retry is gain-adjusted: 3 dB quieter. This literal is the
    # only tripwire for ``crossover_v2_flow.CLIP_RETRY_BACKOFF_DB``, which
    # nothing in the tree imports — importing it here would make the assertion
    # pass at any value.
    gain_after = c.program_for_phase(PHASE_MEASURE).segment("sweep_w").gain_db
    assert gain_after == pytest.approx(gain_before - 3.0)
    # Retry (same index, next attempt) succeeds.
    fakes.measure = _measure_analysis
    assert _run_phase(c, 2, 3)["accepted"] is True


def test_glitch_reuses_drift_baselines_disagree():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(program, glitch=True)
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "drift_baselines_disagree"
    assert verdict["template"] == "silent_auto_retry"
    assert verdict["auto_retry"] is True


# --- measurement-honesty gate G2: sweep schedule-integrity (xrun detector) ------


def test_sweep_schedule_fires_on_large_residual_even_with_good_confidence():
    """Measurement-honesty gate G2 (2026-07-22 — the xrun detector): a
    uniform whole-capture schedule shift the repeat-pair drift check above
    is structurally blind to. Mirrors the 2026-07-22 ``event=outputd.xrun``
    hardware evidence's -25...-28 ms shift, isolating the RESIDUAL half of
    the gate: good confidence (0.8, clears SWEEP_LOCATE_CONFIDENCE_FLOOR)
    does not save a badly-shifted sweep. Routed identically to the
    pre-existing glitch branch above — same silent auto-retry, same reused
    drift_baselines_disagree code (§5.2's capture-glitch reuse convention);
    the diag ``guard`` field is what tells them apart in telemetry."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc("sweep_w", confidence=0.8,
                 residual_samples=-25e-3 * program.sample_rate_hz),
            _loc("sweep_t", confidence=0.8),
            _loc("sweep_w_rep", confidence=0.8),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "drift_baselines_disagree"
    assert verdict["template"] == "silent_auto_retry"
    assert verdict["auto_retry"] is True
    # The automatic retry recomposed the MEASURE program (§5.10 t1, mirrors
    # test_clipped_measure_is_transient_auto_retry_with_quieter_program) and
    # left the conductor in a working state — a clean re-capture succeeds.
    fakes.measure = _measure_analysis
    assert _run_phase(c, 2, 3)["accepted"] is True


def test_weakly_located_sweep_reads_too_quiet_not_glitched():
    """D3 (#1838): the CONFIDENCE half of G2 is a LEVEL verdict, not a glitch.

    Mirrors the 2026-07-22 xrun evidence's 0.07-0.12 per-segment confidence
    with a negligible residual, so only the confidence floor is exercised.
    0.12 clears LOCATE_MIN_CONFIDENCE (0.1) but is under
    SWEEP_LOCATE_CONFIDENCE_FLOOR (0.3).

    Until #1838 this returned `drift_baselines_disagree` + a silent auto
    retry — the household was told its capture had glitched, and the flow
    re-ran the same level. A sweep the locator can barely find was not
    spliced; it was too quiet to hear, and re-running it at the same level
    cannot succeed. `locate_failed` says so and does not auto-retry.

    WHICH sentence it says is no longer fixed: since #2085 the copy is chosen
    from this capture's own pilot evidence, because "too quiet to hear" is an
    inference the pilot can refute. This scenario's analysis carries no pilot
    verdict, so it renders the unknown-evidence copy; the two established
    branches are pinned in `test_crossover_v2_honest_capture_copy.py`.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc("sweep_w", confidence=0.12, residual_samples=1.0),
            _loc("sweep_t", confidence=0.12),
            _loc("sweep_w_rep", confidence=0.12),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "locate_failed"
    # Positive assertion: the household is asked to fix the level and retry,
    # not silently re-run at the same one. (`!= "silent_auto_retry"` would
    # also pass if the template were renamed or dropped.)
    assert verdict["template"] == "fix_and_retry"
    assert not verdict.get("auto_retry")


def test_buried_measure_capture_reads_too_quiet_not_glitched():
    """D3 (#1838), the whole field shape at once: session
    cap_-Us10xORVNlFa_dgi-sP7g's MEASURE played 33 dB below flat, so its
    pilots sank under their SNR floor, its sweeps located at 0.03, the
    mis-located sweeps produced a 1018-sample residual, and the residual
    tripped `glitch_detected` on noise.

    Every one of those is downstream of one cause: nobody could hear the
    capture. With the glitch branch second in the ladder the household was
    told "capture glitched", the flow silently re-armed the same unwinnable
    level, and the session burned 120 s of dead air into a CaptureTimeout.
    The verdict has to name the level.

    The pilots are given real confidence on purpose: they WERE located that
    evening (the SNR guard read 11.22 dB against a 12.38 dB floor, which it
    could only do on a located pair), and they are what let the capture past
    the first `_stimulus_locate_ok` gate.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        pilot_snr_ok=False,
        glitch=True,
        sweep_locations=(
            _loc("pilot_woofer_lo", kind="pilot", confidence=0.5),
            _loc("pilot_woofer_hi", kind="pilot", confidence=0.6),
            _loc("sweep_w", confidence=0.0298, residual_samples=1018.0),
            _loc("sweep_t", confidence=0.0298, residual_samples=1018.0),
            _loc("sweep_w_rep", confidence=0.0298, residual_samples=1018.0),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "pilot_level_collapse"
    assert not verdict.get("auto_retry")

    # And with the pilots healthy, the same buried sweeps still read as a
    # level problem — the weak-locate gate, not the glitch branch.
    fakes.measure = lambda program: _measure_analysis(
        program,
        glitch=True,
        sweep_locations=(
            _loc("pilot_woofer_lo", kind="pilot", confidence=0.5),
            _loc("sweep_w", confidence=0.15, residual_samples=1018.0),
            _loc("sweep_t", confidence=0.15, residual_samples=1018.0),
            _loc("sweep_w_rep", confidence=0.15, residual_samples=1018.0),
        ),
    )
    assert _run_phase(c, 2, 3)["code"] == "locate_failed"


def test_sweep_schedule_clean_capture_passes():
    """The default fixture (well inside both thresholds) is unaffected —
    the happy path already exercises this; pins it explicitly."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


def test_sweep_schedule_boundary_exact_values_pass():
    """Both thresholds are exclusive bounds (``>``/``<``) — exactly-at the
    ceiling/floor passes."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc(
                "sweep_w", confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR,
                residual_samples=(
                    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * 1e-3 * program.sample_rate_hz
                ),
            ),
            _loc("sweep_t", confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR),
            _loc("sweep_w_rep", confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


def test_sweep_schedule_ignores_pilot_segments():
    """Sweeps-only filter (mirrors ``_estimate_drift``'s own pilot exclusion
    in program_analysis.py): a catastrophically bad PILOT location does not
    fire G2 — only ``KIND_SWEEP`` locations are judged."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc("pilot_woofer_hi", "pilot", confidence=0.01,
                 residual_samples=-1_000_000.0),
            _loc("sweep_w", confidence=0.9),
            _loc("sweep_t", confidence=0.9),
            _loc("sweep_w_rep", confidence=0.9),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


def test_stimulus_locate_floor_is_per_role_not_per_capture():
    """D8 (#1838): one clearly-located driver must not clear the gate for a
    driver nobody heard.

    `_stimulus_locate_ok` was `max(confidences) >= LOCATE_MIN_CONFIDENCE`
    across every stimulus segment in the capture — on a two-driver program
    that is effectively no floor at all: a confidently-located woofer let a
    silent tweeter through to be analysed as if it had been measured.

    Per ROLE, not per SEGMENT: a two-level pilot pair's quiet side locates
    more coarsely by design, so the rule is "every role had at least one
    stimulus we could find", not "every segment was easy to find".
    """
    from jasper.active_speaker.crossover_v2_flow import _stimulus_locate_ok

    def _analysis(locations):
        return types.SimpleNamespace(locations=locations)

    def _role_loc(segment_id, role, confidence, kind="sweep"):
        return SegmentLocation(
            segment_id=segment_id, kind=kind, role=role,
            scheduled_start=0, located_start=0, residual_samples=0.0,
            confidence=confidence, peak_dbfs=-12.0, clipped=False,
        )

    # The hole this closes: woofer loud and clear, tweeter inaudible.
    assert not _stimulus_locate_ok(_analysis((
        _role_loc("sweep_w", "woofer", 0.9),
        _role_loc("sweep_t", "tweeter", 0.02),
    )))
    # Both heard: passes.
    assert _stimulus_locate_ok(_analysis((
        _role_loc("sweep_w", "woofer", 0.9),
        _role_loc("sweep_t", "tweeter", 0.4),
    )))
    # A role's weak quiet pilot does NOT sink a role that also has a
    # confidently-located segment.
    assert _stimulus_locate_ok(_analysis((
        _role_loc("pilot_woofer_lo", "woofer", 0.05, kind="pilot"),
        _role_loc("sweep_w", "woofer", 0.9),
        _role_loc("sweep_t", "tweeter", 0.4),
    )))
    # Nothing located at all is still a failure.
    assert not _stimulus_locate_ok(_analysis(()))


def test_locate_failed_and_budget_exhaustion():
    """The planned capture plus THREE extra tries, then the honest end.

    Transformed from a per-code budget (this reason's ``retry_budget`` of 1 gave
    two attempts total) to the owner's pooled per-position bound (#2086). CHECK
    is a single-capture phase: there are no other positions to proceed with, so
    exhaustion ends the session — but the refusal names the spent tries, and
    the code it attributes is the condition actually observed, never a generic
    exhaustion code.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, locate_confidence=0.01)
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "locate_failed"
    assert verdict["template"] == "fix_and_retry"
    # The planned capture spent nothing; three extras are on offer, and the
    # count the phone renders says so.
    assert verdict["attempts"] == {
        "used": 0, "allowed": 3, "left": 3, "by_speaker": 0, "by_household": 0,
    }
    for extra in (1, 2, 3):
        verdict = _run_phase(c, 1, 1 + extra)
        assert verdict["code"] == "locate_failed"
        assert verdict["attempts"]["used"] == extra
        assert verdict["attempts"]["left"] == 3 - extra
        # The household asked for every one of them — nothing was system-forced.
        assert verdict["attempts"]["by_household"] == extra
        assert verdict["attempts"]["by_speaker"] == 0

    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, 5)
    assert excinfo.value.code == "locate_failed"
    # The copy states the count and the outcome. It must NOT invite another try:
    # that is the exact sentence the ruling forbids in front of a refusal.
    message = excinfo.value.user_message
    assert "4 times" in message and "3 extra tries" in message
    assert message.startswith(locate_failed_diagnosis(verdict["pilot_heard"]))
    assert "cannot continue" in message.lower()
    assert "try again" not in message.lower()


def test_check_agc_and_snr_and_channel_map_verdicts():
    # linearity=False with ambient looking clean (snr_floor_ok defaults True)
    # ⇒ the phone's own AGC is the honest cause.
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, linearity=False)
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1)["code"] == "agc_behavioral_fail"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, snr_floor_ok=False)
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1)["code"] == "snr_floor"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, channel_map=False)
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "channel_map_mismatch"
    assert verdict["template"] == "hard_stop"
    # Hard stop: budget 0 ⇒ the very next begin is refused.
    with pytest.raises(CaptureBeginRefused):
        c.authorize_begin(1, 2)


def test_check_low_pilot_snr_routes_to_snr_floor_not_agc():
    """Band-relative ambient-compensated linearity fix (2026-07-20): when the
    quiet pilot's own in-band SNR is too low to trust the ambient-subtracted
    estimate, ``program_analysis`` forces ``linearity_ok`` True (never a false
    linearity FAILURE) and flags ``pilot_snr_ok=False`` instead. The conductor
    must route that on its own — before ever reaching the linearity branch —
    to the honest room/positioning reason, never blaming the phone's AGC."""
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, pilot_snr_ok=False)
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "snr_floor"
    assert verdict["template"] == "fix_and_retry"


def test_check_with_no_ambient_evidence_refuses_before_publishing_check_json():
    """Issue #1818's degraded path, pinned where it is ENFORCED.

    A capture whose ambient window survived below
    ``AMBIENT_MIN_USABLE_FRACTION`` yields an EMPTY band report, and
    ``_snr_floor_ok`` reads an empty report as ``False`` (pinned one module
    below by
    ``test_audio_measurement_program_analysis.py::test_check_ambient_below_the_usable_fraction_degrades_to_disclosed_no_evidence``).
    This is the other half of that coupling: the conductor must refuse such a
    CHECK with ``snr_floor`` **and must not publish check.json** — a refused
    CHECK that still published would hand MEASURE a gain plan and an ambient
    report the session never actually measured.

    The publish seam is a RAISING stub rather than a recording one on purpose.
    Asserting an empty ``published_checks`` list would pass for the wrong
    reason if the refusal were ever moved BELOW the publish and the list were
    cleared; a stub that raises fails loudly at the moment of the call, and
    names why in the failure text.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, snr_floor_ok=False)
    c = _conductor(fakes)

    def _must_not_publish(plan, ambient):
        raise AssertionError(
            "check.json was published for a CHECK the conductor refuses: "
            "the snr_floor gate must sit ABOVE publish_check"
        )

    c._seams = dataclasses.replace(c._seams, publish_check=_must_not_publish)

    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "snr_floor"
    assert fakes.published_checks == []


def test_check_linearity_fail_blames_the_room_when_ambient_is_elevated():
    """W6.12: agc_behavioral_fail's copy blames the phone's mic, but hardware
    round 4 proved a distinct honest cause with the identical symptom (the
    captured pilot-pair delta drifting from the programmed delta) — a loud
    ambient burst during the pilot pair, with the phone's AGC verifiably off.
    When the SAME capture's ambient bands ALSO fail the CHECK gain solve's own
    SNR-floor verdict (computed unconditionally, independent of linearity),
    the room — not the phone — is named."""
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(
        program, linearity=False, snr_floor_ok=False,
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "noisy_room_linearity"
    assert verdict["template"] == "fix_and_retry"


def test_measure_low_pilot_snr_routes_to_level_collapse_not_agc():
    """Issue #1810 at MEASURE.

    The guard existed on ``PilotObservation`` all along, but MEASURE programs
    carried no ambient window, so ``pilot_snr_ok`` could only ever be True
    there and this branch was unreachable. Now that the composer gives them a
    pre-pilot window, a capture whose pilots never cleared the room floor gets
    a verdict about the room and the level — never about the phone.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(program, pilot_snr_ok=False)
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "pilot_level_collapse"
    assert verdict["template"] == "fix_and_retry"


def test_measure_low_pilot_snr_wins_over_the_linearity_branch():
    """Ordering is the whole fix. ``_pilot_observations`` forces
    ``linearity_ok`` True under the SNR floor, but a caller that checked
    linearity FIRST would still route a hand-built analysis carrying both
    flags to the mic accusation — and, more importantly, the ordering is what
    a future analysis change must not be free to invert."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program, linearity=False, pilot_snr_ok=False,
    )
    assert _run_phase(c, 2, 2)["code"] == "pilot_level_collapse"


def test_verify_low_pilot_snr_routes_to_level_collapse_not_agc():
    """Issue #1810 at VERIFY — the JTS3 session of 2026-07-28.

    A freshly-applied correction dropped the pilot band 14-18 dB, the quiet
    pilot landed ~5 dB over the room floor, the noise compressed the captured
    two-pilot delta from 10 dB to 6 dB, and the household was told "your
    phone's microphone changed its own levels" while the only direct
    recording-chain evidence (``pilot_transfer_step_db``) was null.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(program, pilot_snr_ok=False)
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "pilot_level_collapse"
    # Post-apply, the envelope promotes any failure to the verify_fail screen
    # (W6.7 ruling 3) so the household keeps its Undo — the REASON's own
    # template stays fix_and_retry, which is what applies pre-apply.
    assert REASON_REGISTRY["pilot_level_collapse"].template == "fix_and_retry"


def test_verify_low_pilot_snr_does_not_seed_the_g3_transfer_baseline():
    """A collapsed pilot pair cannot establish the G3 reference either.

    ``_verify_verdict`` refuses on SNR BEFORE the transfer block, so a
    low-SNR first attempt leaves no baseline behind — otherwise the next,
    good attempt would be compared against a level measured out of noise and
    could fail ``verify_level_shift`` on the strength of it. This is also the
    bound that keeps ambient subtraction out of G3's error budget (see
    ``_pilot_transfer_by_role``'s docstring).
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_snr_ok=False, pilot_hi_dbfs=-45.0,
    )
    assert _run_phase(c, 3, 3)["code"] == "pilot_level_collapse"
    assert c._verify_pilot_baseline is None
    # The good re-verify then establishes the baseline itself and passes.
    fakes.verify = lambda program: _verify_analysis(program, pilot_hi_dbfs=-20.0)
    assert _run_phase(c, 3, 4)["accepted"] is True


def test_cloud_position_low_pilot_snr_routes_to_level_collapse_not_agc():
    """The same ordering on a prompted cloud position — the phase that walks
    the mic, and so the one most likely to meet a genuinely quiet spot."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.verify = lambda program: _verify_analysis(program, pilot_snr_ok=False)
    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[0], 3)
    assert verdict["code"] == "pilot_level_collapse"


@pytest.mark.parametrize("snrs,expected", [
    # The row the review caught: one pilot buried (-inf, "never exceeded the
    # ambient"), one clean. Dropping -inf as non-finite logged the CLEAN
    # pilot's 20.0 dB beside pilot_snr_ok=False — a diag row contradicting
    # itself, and the same "verdict beside absent evidence" shape #1810 is
    # about. The buried pilot must win the min().
    (( -math.inf, 20.0), PILOT_SNR_UNUSABLE_DB),
    # +inf is NOT a measurement ("no ambient window to validate against"), so
    # it is excluded rather than floored — the real number is reported.
    ((math.inf, 20.0), 20.0),
    # Every pilot +inf (a legacy program with no window at all): no number to
    # report, and None must not be confused with a measured floor.
    ((math.inf, math.inf), None),
    # Both buried.
    ((-math.inf, -math.inf), PILOT_SNR_UNUSABLE_DB),
    # Ordinary case: the worst real number.
    ((30.0, 11.5), 11.5),
])
def test_worst_pilot_snr_db_handles_both_infinities(snrs, expected):
    """The diag field must never contradict the verdict logged beside it."""
    analysis = _snr_analysis(
        *(_snr_pilot(f"r{i}", snr) for i, snr in enumerate(snrs))
    )
    assert _worst_pilot_snr_db(analysis) == expected


def test_worst_pilot_snr_db_is_none_without_pilots():
    assert _worst_pilot_snr_db(_snr_analysis()) is None


def test_pilot_level_collapse_copy_never_accuses_the_phone():
    """Issue #1810's actual complaint, pinned as copy.

    The household's previous experience of this failure was being told to go
    re-allow a microphone that had done nothing wrong. The new reason names
    the two real causes and two real actions; the definite mic accusation is
    reserved for ``verify_level_shift``, which has the cross-attempt transfer
    step to back it.
    """
    spec = REASON_REGISTRY["pilot_level_collapse"]
    assert spec.retry_budget == 1
    text = spec.message.lower()
    assert "phone's microphone" not in text
    assert "re-allow" not in text
    assert "too loud" in text and "too quiet" in text
    # The one code still allowed to state the mic as the cause is the one
    # holding the evidence for it.
    assert "microphone" in REASON_REGISTRY["verify_level_shift"].message.lower()


def test_agc_behavioral_fail_copy_states_the_observation_not_the_cause():
    """Issue #1810 amendment. ``agc_behavioral_fail`` fires on a captured
    two-pilot delta that did not match the programmed one — which the phone's
    input chain OR the speaker's own output compression can produce. The copy
    may describe that observation and prescribe the one useful action; it may
    not assert the phone as the cause, because this code never observes it."""
    message = REASON_REGISTRY["agc_behavioral_fail"].message
    assert "your phone's microphone changed" not in message.lower()
    assert "test tones" in message.lower()


def test_delay_exceeds_search_window_verdict():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        alignment=_alignment(status=ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "delay_exceeds_search_window"
    assert verdict["template"] == "fix_and_retry"


def test_verify_out_of_tolerance_and_inconclusive():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    # Out of tolerance: |measured − predicted| > 1.5 dB.
    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "verify_out_of_tolerance"
    assert verdict["template"] == "verify_fail"
    assert c.verify_outcome == "fail"

    # Gate-comparability: VERIFY's own gate shorter than MEASURE's ⇒
    # "inconclusive — re-verify", not fail (§5.2).
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0)
    verdict = _run_phase(c, 3, 4)
    assert verdict["code"] == "verify_inconclusive"
    assert c.verify_outcome == "inconclusive"

    # A comparable-gate clean re-verify passes (budget 2 admits it).
    fakes.verify = _verify_analysis
    verdict = _run_phase(c, 3, 5)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"



# --- alignment sign contract -----------------------------------------------------


def test_alignment_to_candidate_fields_sign_contract():
    def analysis_with(delay_us, status=ALIGNMENT_OK, polarity="normal"):
        class _A:
            alignment = _alignment(delay_us=delay_us, status=status, polarity=polarity)
        return _A()

    # positive ⇒ tweeter earlier ⇒ tweeter delayed.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0), roles=("woofer", "tweeter"),
    )
    assert (delay, role, polarity) == (150.0, "tweeter", "keep")
    # negative ⇒ woofer delayed, magnitude non-negative.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(-90.0), roles=("woofer", "tweeter"),
    )
    assert (delay, role, polarity) == (90.0, "woofer", "keep")
    # inverted polarity maps to the W4 "invert" vocabulary.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0, polarity="inverted"),
        roles=("woofer", "tweeter"),
    )
    assert polarity == "invert"
    # An edge-clamped estimate is not applied: trims-only candidate.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0, status=ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW),
        roles=("woofer", "tweeter"),
    )
    assert (delay, role, polarity) == (None, None, None)


# --- phase persistence + session binding (§5.6) -----------------------------------


def test_resume_within_session_skips_accepted_phases():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    snap = c.snapshot()
    assert snap.accepted_phases == (PHASE_CHECK,)

    resumed = CrossoverV2Session.hydrate(
        snap,
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
    )
    assert resumed.current_phase == PHASE_MEASURE
    # The MEASURE program was recomposed from the persisted gain plan.
    program = resumed.program_for_phase(PHASE_MEASURE)
    assert program.segment("sweep_w").gain_db == pytest.approx(-11.0)


def test_new_session_invalidates_check_and_measure_evidence():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    snap = c.snapshot()
    assert PHASE_MEASURE in snap.accepted_phases

    fresh = CrossoverV2Session.hydrate(
        snap,
        session_id="cap_other_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
    )
    assert fresh.accepted_phases == frozenset()
    assert fresh.current_phase == PHASE_CHECK


# --- position-group choreography (flat-linearization PR-3b) ------------------
#
# State-walk tests over the group lifecycle, driven through the fake seams. The
# cloud positions play the VERIFY-shaped summed program, so FakeSeams' analyze
# dispatch (keyed on the PROGRAM's phase) returns `_verify_analysis` for them
# with no new factory — the same reason `program_analysis` needed no new
# dispatch branch.


def test_cloud_measure_group_closes_only_after_its_last_position():
    """One PHASE spans many indexes: accepting position 3 of 8 must not read as
    "the pre-apply cloud is done" — the phase closes on its LAST index."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    assert c.current_phase == PHASE_CLOUD_MEASURE

    for index in CLOUD_MEASURE_INDEXES[:-1]:
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is True
        assert verdict["position_id"]
        assert PHASE_CLOUD_MEASURE not in c.accepted_phases
        assert c.current_phase == PHASE_CLOUD_MEASURE
        assert "group_complete" not in verdict

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert verdict["accepted"] is True
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    assert verdict["geometry"]["locked"] is False
    assert PHASE_CLOUD_MEASURE in c.accepted_phases
    # Every position is retained, in capture order, under a stable id.
    assert c.group_positions(PHASE_CLOUD_MEASURE) == tuple(
        f"{PHASE_CLOUD_MEASURE}_{i:02d}" for i in CLOUD_MEASURE_INDEXES
    )
    # The group closed, so its verdict is readable; the group that has not
    # started reports None (never "geometry was fine").
    assert c.group_geometry(PHASE_CLOUD_MEASURE) is not None
    assert c.group_geometry(PHASE_CLOUD_VERIFY) is None
    # …but the FIT has NOT run yet (flow-simplification §2.6): the geometry
    # close is a per-capture verdict, the fit waits for the household's
    # confirmation past the final position, so that position stays retakeable.
    assert verdict["awaiting_confirm"] is True
    assert c.candidate is None
    assert c.cloud_measure_group_awaiting_confirm() is True
    # The household's explicit confirmation is what builds the candidate — it
    # used to ride the next entry's begin, which a measure-only plan does not
    # have (two-stage work order D1).
    assert _confirm_cloud(c)["candidate_fingerprint"]
    assert c.candidate is not None
    assert c.cloud_measure_group_awaiting_confirm() is False
    # …and the measuring SESSION is over: it has no VERIFY entry to hold for,
    # and nothing was applied. What comes next is the review interlude on
    # jts.local, which the wizard's own phase resolution owns (a measure-only
    # `session_phases` with `applied` false resolves to `review`, never
    # `done` — see tests/test_correction_crossover_v2_endpoints.py).
    assert c.current_phase == PHASE_DONE
    assert PHASE_VERIFY not in c.session_phases


# --- the timing move + the cloud→fit wiring (flat-linearization PR-6b) -------
#
# Owner decision (2026-07-27): the fit, the candidate build, and the auto-apply
# trigger move from MEASURE's accept to the CLOUD_MEASURE group close, so the
# fit consumes the cloud's honesty verdict instead of preceding it by eight
# captures. These walk the REAL conductor for both halves of that: WHEN the
# candidate appears, and WHAT reaches the envelope when it does.


def test_the_candidate_is_built_at_the_cloud_group_close_not_at_measure():
    """The timing move, at the conductor's own surface.

    MEASURE still ACCEPTS — every trust gate it owns is unchanged and still
    fires there — but it no longer produces a candidate, a fingerprint, or the
    ``auto_apply`` flag. All three appear once the pre-apply cloud is walked
    AND confirmed, eight captures later, which is the first moment the fit has
    a cloud verdict to consume.

    Flow-simplification §2.6 moved the trigger one tap further: the final
    position's ACCEPTANCE closes the geometry and stashes the combine, and the
    household's confirmation past it is what fits. So the candidate appears on
    the confirm, not on that last verdict.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)

    measure_verdict = _run_phase(c, 1, 1) and _run_phase(c, 2, 2)
    assert measure_verdict["accepted"] is True
    assert measure_verdict["measurement_phase"] == PHASE_MEASURE
    assert "candidate_fingerprint" not in measure_verdict
    assert "auto_apply" not in measure_verdict
    assert c.candidate is None
    assert fakes.published_candidates == []

    attempt = 3
    for index in CLOUD_MEASURE_INDEXES[:-1]:
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert "auto_apply" not in verdict
        assert c.candidate is None, index

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert verdict["accepted"] is True
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    # Walked, not yet confirmed: no fit, no publish, nothing applied — so a
    # household that stops here leaves the speaker untouched.
    assert "auto_apply" not in verdict
    assert c.candidate is None
    assert fakes.published_candidates == []

    confirmed = _confirm_cloud(c)
    assert confirmed["candidate_fingerprint"] == c.candidate.fingerprint
    # …and it carries no apply trigger (D1).
    assert "auto_apply" not in confirmed
    assert len(fakes.published_candidates) == 1
    # A second confirm is a no-op — the fit fires exactly once per session.
    assert c.confirm_cloud_measure_group() is None
    assert len(fakes.published_candidates) == 1
    # And the measuring session ends here — nothing applied, nothing held.
    assert c.current_phase == PHASE_DONE


# --- the eager fit (owner UX direction, 2026-07-30) ------------------------------


def test_a_speculative_candidate_does_not_release_the_held_set():
    """**The load-bearing pin of the eager-fit rider.**

    Both seams that resolve the held set carried a comment warning that
    ``cloud_measure_group_awaiting_confirm`` answered "has the household
    confirmed?" with ``self._candidate is None`` — which is also the group
    close's fire-once guard. An eagerly-built candidate would therefore have
    flipped the predicate to False and un-held the runner's set, shutting the
    voluntary-retake window in the same instant it opened, silently, at the one
    moment the design exists to keep it open.

    So: fit early, and the window must not move. The predicate now reads
    ``_group_confirmed``, and an eager build parks somewhere nothing else
    looks.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_accept(c)

    assert c.cloud_measure_group_awaiting_confirm() is True
    assert c.run_speculative_group_close() is True

    # THE PIN: a candidate now exists, fitted and gated, and the household's
    # window is exactly as open as it was a line ago.
    assert c.cloud_measure_group_awaiting_confirm() is True
    # …because none of the three things that make a candidate real happened.
    assert c.candidate is None
    assert fakes.published_candidates == []
    # And the speaker page still says what is TRUE — the household has
    # something to do and it is on their phone. The eager fit is deliberately
    # invisible: "running" is reserved for work the household has asked for,
    # and a retake would otherwise have to walk that state backwards.
    assert c.cloud_close_state == CLOUD_CLOSE_AWAITING_CONFIRM

    # Only the household's own confirmation moves any of it.
    assert _confirm_cloud(c)["candidate_fingerprint"]
    assert c.cloud_measure_group_awaiting_confirm() is False
    assert len(fakes.published_candidates) == 1


def test_the_confirm_commits_the_eager_fit_rather_than_refitting():
    """The payoff: the household's Continue costs a COMMIT, not a fit.

    The whole point of the rider — the fit is the slowest thing in the session
    (a measured 2.7-6 s combine plus the fit itself, worse on a Pi 5) and it
    used to start only once the household had walked back to a browser and
    tapped. Here it has already run, so the tap publishes a finished candidate
    and the review screen is up immediately.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_accept(c)
    builds = _count_builds(c)

    assert c.run_speculative_group_close() is True
    assert len(builds) == 1
    banked = c._speculative_close.candidate
    # Idempotent: the host fires the trigger on every accept that leaves a
    # walked, unconfirmed cloud, and a retake makes that more than once. A
    # second eager fit while one is already banked must be a no-op, not a
    # second fit racing the first for the bank.
    assert c.run_speculative_group_close() is False
    assert len(builds) == 1

    confirmed = _confirm_cloud(c)

    # No second fit — the confirm consumed the banked build…
    assert len(builds) == 1
    # …and it is the SAME candidate, not merely an equal one: the eager fit
    # buys latency, never a different product.
    assert c.candidate is banked
    assert confirmed["candidate_fingerprint"] == banked.fingerprint
    assert fakes.published_candidates == [banked]
    # The bank is spent, so a re-delivered signal still cannot fit twice.
    assert c._speculative_close is None
    assert c.confirm_cloud_measure_group() is None
    assert len(builds) == 1


def test_a_retake_discards_the_eager_fit_and_the_confirm_refits_the_new_cloud():
    """The retake contract, preserved through the rider (owner requirement).

    A voluntary retake of the final position (§2.6) means the cloud CHANGED,
    so anything fitted from the old one is answering a question nobody asked
    any more. The discard is atomic with the re-stash of the combine, which is
    what lets the confirm trust a bank without a generation counter to check
    it against — and it is what keeps T3's data contract true: the fit consumes
    exactly the accepted cloud as of the close.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    attempt = _walk_measure_cloud_to_accept(c)
    builds = _count_builds(c)

    assert c.run_speculative_group_close() is True
    stale = c._speculative_close.candidate
    assert len(builds) == 1

    # The household redoes the final spot rather than continuing.
    retake = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert retake["accepted"] is True
    # Still held, still theirs to end — a retake is not a confirmation.
    assert retake["awaiting_confirm"] is True
    assert c.cloud_measure_group_awaiting_confirm() is True
    # THE DISCARD: the stale build is gone, dropped in the same locked region
    # that re-stashed the new combine.
    assert c._speculative_close is None

    confirmed = _confirm_cloud(c)

    # The confirm REFITTED — it did not smuggle the pre-retake build through.
    assert len(builds) == 2
    assert c.candidate is not stale
    assert confirmed["candidate_fingerprint"] == c.candidate.fingerprint
    assert fakes.published_candidates == [c.candidate]


def test_an_eager_fit_failure_surfaces_on_the_confirm_not_before():
    """A speculative failure must not corrupt the confirm flow.

    The household has not asked for this computation yet and may still retake,
    which would moot it entirely — so a failure here renders NOTHING. The bank
    stays empty, the held window stays open, and the confirm refits and raises
    the identical error from the identical place it always did, where the host
    maps it to a real terminal screen.

    The cost is one wasted fit on a session that is already ending; the
    alternative — re-raising a stored exception across a thread boundary —
    buys seconds on a terminal path in exchange for a second failure route.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_accept(c)

    def _boom(_analysis, _cloud):
        raise RuntimeError("synthetic fit failure")

    c._build_candidate = _boom

    assert c.run_speculative_group_close() is False

    # NOTHING moved: no candidate, no publish, no failure screen, and the
    # household's retake window is exactly as open as before.
    assert c._speculative_close is None
    assert c.candidate is None
    assert fakes.published_candidates == []
    assert c.cloud_measure_group_awaiting_confirm() is True
    assert c.cloud_close_state == CLOUD_CLOSE_AWAITING_CONFIRM

    # It surfaces where it always did — on the household's own confirmation.
    with pytest.raises(RuntimeError, match="synthetic fit failure"):
        c.confirm_cloud_measure_group()

    # **THE DISCRIMINATOR for the decoupling itself**, and the only assertion
    # in the suite that can tell the two predicates apart. A close that RAISED
    # leaves ``_candidate`` unset — that is T3's retryability contract, still
    # intact below — so the pre-rider predicate (``self._candidate is None``)
    # would report this set as still awaiting confirmation and re-hold a
    # runner whose household already tapped Continue. Only a predicate that
    # asks "has the household confirmed?" gets it right: the window shuts on
    # the TAP, not on whether the fit behind it succeeded.
    assert c.cloud_measure_group_awaiting_confirm() is False
    assert c.candidate is None


def test_only_the_pre_apply_group_close_fires_a_candidate_across_a_whole_session():
    """The `phase == PHASE_CLOUD_MEASURE` guard in ``_close_cloud_group`` is
    load-bearing and gets its own pin: ``_close_cloud_group`` is shared by BOTH
    position groups, so without it the POST-apply cloud's close would build a
    second candidate — over an already-applied speaker, on evidence gathered
    through the correction it would be re-deriving.

    Re-derived for the two-stage world (work order D1/D2): the journey is two
    SESSIONS now, so this walks both — stage 1's ten captures plus its explicit
    confirmation, then stage 2's six against a fresh applied conductor — and
    asserts exactly one candidate across the pair, built by the confirmation
    and never by any capture verdict. The old single-conductor version could
    not express this at all once the index spaces split.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    stage1 = _cloud_conductor(fakes)

    attempt = 1
    for index in sorted(CLOUD_MAP):
        verdict = _run_phase(stage1, index, attempt)
        attempt += 1
        assert verdict["accepted"] is True, index
        assert "auto_apply" not in verdict, index
        assert stage1.candidate is None, index
    assert _confirm_cloud(stage1)["candidate_fingerprint"]
    assert len(fakes.published_candidates) == 1

    # STAGE 2, on its own conductor: applied, its own index space, its own
    # post-apply group. It must close that group and publish NOTHING.
    fakes.apply_done = True
    stage2 = _conductor(
        fakes,
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    attempt = 1
    for index in sorted(STAGE2_MAP):
        verdict = _run_phase(stage2, index, attempt)
        attempt += 1
        assert verdict["accepted"] is True, index
        assert "auto_apply" not in verdict, index

    assert len(fakes.published_candidates) == 1
    assert stage2.candidate is None
    # The post-apply group DID close — this is not a vacuous pass.
    assert PHASE_CLOUD_VERIFY in stage2.accepted_phases
    assert stage2.group_geometry(PHASE_CLOUD_VERIFY) is not None
    assert stage2.current_phase == PHASE_DONE


def test_a_session_with_no_cloud_group_still_builds_the_candidate_at_measure():
    """The pre-cloud 3-entry shape has nothing to wait for, so it must behave
    EXACTLY as it did before the timing move — same accept, same payload keys,
    same auto-apply timing. The rule is "the fit runs at the last capture
    before the apply", and for this shape that capture is MEASURE."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)  # the default {1: check, 2: measure, 3: verify}
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert verdict["candidate_fingerprint"] == c.candidate.fingerprint
    assert len(fakes.published_candidates) == 1
    # No cloud exists, so no cloud evidence can ride the candidate — the
    # pre-move shape, byte for byte.
    assert c.candidate.exclusion_evidence == {}
    assert c.current_phase == PHASE_APPLYING


def test_the_clouds_honesty_verdict_reaches_the_fit_envelope():
    """THE wiring acceptance (plan PR-6, interpretation call (A)): the merged
    honesty mask a closed cloud produced actually binds the correction
    envelope, on the live path.

    A position-invariant comb cloud identifies real nulls; those intervals must
    (a) reach ``compose_envelope``'s ``spatial_exclusion_limit`` term, visible
    in the persisted fit's own per-octave reason summary, (b) cost the fit ALL
    correction depth inside them — zero gain spent where EQ cannot help — and
    (c) ride the candidate as the exclusion reason of record, with the τ/r
    registry that justifies them.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    fakes.verify = _comb_cloud_analysis_factory()
    c = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(c)
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict

    pipeline = c.group_cloud_result(PHASE_CLOUD_MEASURE)
    assert pipeline["available"] is True
    registry = pipeline["null_registry"]
    assert registry["classification"] == "position_invariant"
    assert registry["nulls"], "the fixture must identify nulls to prove anything"
    intervals = [tuple(band) for band in pipeline["merged_excluded_bands_hz"]]
    assert intervals

    # (a) the term bound at least one octave of the driver that reaches these
    # frequencies — the fit's OWN persisted account of why.
    reasons = {
        reason
        for fit in c.candidate.linearization.values()
        for reason in fit["reason_summary"].values()
    }
    assert "envelope_limited_by_spatial_exclusion" in reasons

    # (b) no correction is placed inside an identified null. NOTE: on THIS
    # fixture this assertion does not discriminate — it holds in the severed
    # case too, because every filter the fit places lands over an octave and a
    # half below the lowest null the cloud identifies, so there was never a
    # filter up there to remove (PR-6a's own corpus acceptance records the same
    # shape — the exclusion punches holes rather than moving filters).
    #
    # Stated as a SEPARATION and not as two frequency ranges on purpose: the
    # TOP of the fit's range tracks the shared fixture's bump (the 150 Hz floor
    # is the woofer RoleBand's own edge and does not move), so the literal that
    # used to sit here ("150-1485 Hz") went stale the moment R10a moved that
    # bump to +3 dB at 2400 Hz. Re-derived at that revision on 2026-08-02, the
    # fit tops out near 2.4 kHz and the nulls start above 7 kHz — a margin of
    # ~1.6 octaves, i.e. the conclusion holds with room to spare rather than
    # by a hair. If a later fixture change narrows that, this note is the
    # thing to re-measure; the endpoints themselves are not the claim.
    #
    # It is kept as a standing invariant, not as this test's proof; (a) and
    # (c) plus the sibling severing test are what carry that.
    for fit in c.candidate.linearization.values():
        for biquad in fit["filters"]:
            for lo, hi in intervals:
                assert not (lo <= float(biquad["freq"]) <= hi), (biquad, (lo, hi))

    # (c) the reason of record rides the candidate — the same intervals, the
    # same registry, the cloud's own N.
    evidence = c.candidate.exclusion_evidence
    assert [tuple(b) for b in evidence["excluded_bands_hz"]] == intervals
    assert evidence["null_registry"]["nulls"] == registry["nulls"]
    assert evidence["n_positions"] == len(c.group_positions(PHASE_CLOUD_MEASURE))
    assert evidence["phase"] == PHASE_CLOUD_MEASURE
    assert [band["center_hz"] for band in evidence["band_spread"]]

    # (d) the ROOM layer's half of the same payload (issue #1787, plan RC1).
    # The validity floor and the gated spec curve previously existed only in
    # the retention-prunable session bundle, so once a bundle aged out the room
    # layer could not tell where this speaker's gated measurement stops being
    # trustworthy nor what its gated response is. Both are copied verbatim from
    # this group's own pipeline result — the same source cloud_measure.json
    # reads — so the two copies cannot disagree.
    assert evidence["validity_floor_hz"] == pipeline["validity_floor_hz"]
    assert evidence["gated_spec_curve"]["freqs_hz"] == pipeline["curve"]["freqs_hz"]
    assert (
        evidence["gated_spec_curve"]["magnitude_db"]
        == pipeline["curve"]["magnitude_db"]
    )
    assert evidence["gated_spec_curve"]["freqs_hz"], "the curve must be non-empty"


def test_severing_the_cloud_wiring_changes_the_fit(monkeypatch):
    """The "delete the input, the test must fail" half of the acceptance.

    Same cloud, same MEASURE analysis — but with ``_cloud_fit_evidence``
    severed the fit never learns what the cloud found, the exclusion term is
    absent from every reason summary, the fit's own permitted band is wider,
    and the candidate carries no reason of record. If a future edit quietly
    stopped threading the cloud into ``compose_envelope``, THIS is the state
    the passing test above would collapse into.

    **The emitted correction now differs too** (PR-L5). Until L5 the biquads
    and trims were IDENTICAL wired and severed on this fixture — the cut-only
    fit placed every filter over an octave and a half below the lowest null the
    cloud identifies (the sibling test's note (b) carries the measured
    separation and the reason it is not written here as two ranges), so the
    exclusion had no filter to move and only narrowed the permitted band. L5
    makes the cloud load-bearing on the FILTERS: boost permission is gated on
    the cloud verdict having reached the envelope, because without it
    ``allowed_depth_db`` is not zeroed in the registry's nulls and a lift could
    be designed into one. So the wired run emits a boost the severed run does
    not, and severing now costs the correction a filter rather than only a
    disclosure.
    """
    def _run(sever: bool):
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        fakes.verify = _comb_cloud_analysis_factory()
        c = _cloud_conductor(fakes)
        if sever:
            monkeypatch.setattr(c, "_cloud_fit_evidence", lambda combined: None)
        _walk_measure_cloud_to_close(c)
        return c.candidate

    wired = _run(sever=False)
    severed = _run(sever=True)

    wired_reasons = {
        reason for fit in wired.linearization.values()
        for reason in fit["reason_summary"].values()
    }
    severed_reasons = {
        reason for fit in severed.linearization.values()
        for reason in fit["reason_summary"].values()
    }
    assert "envelope_limited_by_spatial_exclusion" in wired_reasons
    assert "envelope_limited_by_spatial_exclusion" not in severed_reasons
    assert wired.exclusion_evidence and severed.exclusion_evidence == {}
    # The FIT differs, not only its disclosure: the cloud's exclusion narrows
    # the band the fit was permitted to work in. This is the assertion that
    # would fail if the wiring were reduced to a reporting-only change.
    wired_band = wired.linearization["tweeter"]["fit_band_hz"]
    severed_band = severed.linearization["tweeter"]["fit_band_hz"]
    assert wired_band != severed_band, (wired_band, severed_band)
    # PR-L5: and the emitted CORRECTION differs — the wired run was granted the
    # lift vocabulary, the severed run was not, so only the wired one can carry
    # a boost. This is the strongest form of "delete the input, the test must
    # fail": severing the cloud now costs a filter, not just a reason string.
    wired_boosts = [
        f for fit in wired.linearization.values() for f in fit["filters"]
        if f["gain"] > 0.0
    ]
    severed_boosts = [
        f for fit in severed.linearization.values() for f in fit["filters"]
        if f["gain"] > 0.0
    ]
    assert wired_boosts, "the wired run should have been granted boost"
    assert severed_boosts == [], severed_boosts
    # …and the cut-only skeleton underneath is still the same fit: severing
    # withholds the lift, it does not re-plan the correction.
    for role in sorted(wired.linearization):
        wired_cuts = [
            f for f in wired.linearization[role]["filters"] if f["gain"] <= 0.0
        ]
        severed_cuts = [
            f for f in severed.linearization[role]["filters"] if f["gain"] <= 0.0
        ]
        assert wired_cuts == severed_cuts, role


def test_abandoning_the_walk_before_the_group_closes_leaves_the_speaker_untouched():
    """The fail-safe direction of the timing move, stated as a property.

    An operator who walks away part-way through the prompted cloud never
    reaches the group close, so no candidate is built, no ``auto_apply`` is
    ever returned, and nothing is handed to the apply transaction — the
    speaker is exactly as it was. This is STRICTLY safer than the pre-move
    flow, where the apply fired at MEASURE and abandoning the walk left a
    household with a corrected speaker that was never verified.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    # Every prompted position except the last — then the operator stops.
    for index in CLOUD_MEASURE_INDEXES[:-1]:
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is True
        assert "auto_apply" not in verdict

    assert c.candidate is None
    assert fakes.published_candidates == []
    assert PHASE_CLOUD_MEASURE not in c.accepted_phases
    # Nothing to apply, so the flow is still IN the cloud — never APPLYING.
    assert c.current_phase == PHASE_CLOUD_MEASURE


def test_a_group_close_with_no_retained_measure_analysis_fails_honestly():
    """The one state that could reach the group close without a fit input:
    a conductor carrying ``accepted_phases`` from a snapshot but none of the
    MEASURE analysis behind them — the same-session ``hydrate`` branch.

    **Production cannot construct it.** ``prepare_v2_session`` hydrates
    against a freshly MINTED relay session id, so ``snapshot.session_id ==
    session_id`` is never true there and hydrate always takes the
    fresh-start-at-CHECK branch (§5.6). This pins what happens if that ever
    stops being true: an honest raise (the host maps it to
    ``internal_error``, a real terminal screen) rather than a silent confirm
    with no ``auto_apply``, which would leave VERIFY's ``on_apply`` hold
    waiting on an apply that can never come.

    Since flow-simplification §2.6 the raise lands on the CONFIRM rather than
    on the final position's capture — the fit moved, the honesty did not.
    """
    fakes = FakeSeams()
    c = _cloud_conductor(fakes, accepted_phases=(PHASE_CHECK, PHASE_MEASURE))
    assert c.current_phase == PHASE_CLOUD_MEASURE

    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], 1)
    assert _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)["accepted"] is True
    with pytest.raises(CrossoverV2FlowError, match="no retained MEASURE analysis"):
        c.confirm_cloud_measure_group()


def test_a_candidate_build_failure_leaves_the_group_journalled_but_unaccepted(caplog):
    """N1: the exact forensic state a candidate-build raise leaves behind.

    ``_close_cloud_group``'s wrap protects the diagnostic PIPELINE, not the
    candidate build — the build is the session's product and is allowed to
    fail. Since flow-simplification §2.6 split the two, the split is visible
    here: the CAPTURES all succeeded and the group is genuinely accepted, and
    it is the household's CONFIRM — the fit — that raises. The host maps that
    to ``internal_error``; nothing durable claims a candidate.

    Pinned so nobody later reads the wrap's "the accept is already decided"
    comment as a promise it does not make.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)

    def _boom(_candidate):
        raise RuntimeError("synthetic publish-seam failure")

    c = _cloud_conductor(fakes)
    c._seams = replace(c._seams, publish_candidate=_boom)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)

    assert _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)["accepted"] is True
    with pytest.raises(RuntimeError, match="synthetic publish-seam failure"):
        c.confirm_cloud_measure_group()

    assert "event=correction.crossover_v2_cloud_group_complete" in caplog.text
    assert c.group_geometry(PHASE_CLOUD_MEASURE) is not None
    # The WALK completed and is recorded as such; only the fit failed.
    assert PHASE_CLOUD_MEASURE in c.accepted_phases
    # No half-published candidate is left readable on the conductor either:
    # the seam raised before it could be handed anywhere, and the fingerprint
    # never reached a verdict payload.
    assert fakes.published_candidates == []


def test_a_failed_cloud_pipeline_fits_without_cloud_terms_and_says_so(
    monkeypatch, caplog,
):
    """Honest degradation, named at the site: a group whose honesty pipeline
    never became available hands the fit NO cloud evidence — not the screen's
    intervals alone.

    That all-or-nothing rule is the wiring contract (issue #1742 item 4): the
    screen structurally cannot see a position-invariant null, so a screen-only
    mask would exclude the interference the cloud CAN see while silently
    correcting the interference it cannot. The session still produces a
    candidate and still auto-applies — a diagnostic failure is not a
    measurement failure — and the fallback is logged rather than silent.
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    monkeypatch.setattr(
        flow, "assemble_cloud_group_result",
        lambda *a, **k: {"available": False, "reason": "pipeline_failed"},
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    fakes.verify = _comb_cloud_analysis_factory()
    c = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(c)

    assert verdict["accepted"] is True
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.candidate is not None
    assert c.candidate.exclusion_evidence == {}
    assert "event=correction.crossover_v2_fit_without_cloud" in caplog.text
    assert "reason=pipeline_failed" in caplog.text


def test_a_cloud_pipeline_exception_never_costs_the_group_its_accept(monkeypatch):
    """S4 review finding (2026-07-26): the honest-instrument pipeline is
    diagnostic/disclosure machinery layered on TOP of an ALREADY-DECIDED
    accept — a bug in ``assemble_cloud_group_result`` (or the
    ``publish_cloud`` seam) must never flip that decision.
    ``_close_cloud_group``'s own wrap around ``_run_cloud_pipeline`` is the
    structural guarantee; this proves it holds even for a raise OUTSIDE
    ``assemble_cloud_group_result``'s own try/except (a genuinely unexpected
    pipeline bug, not the bounded family it already handles internally).
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic pipeline bug")

    monkeypatch.setattr(flow, "assemble_cloud_group_result", _boom)

    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)

    assert verdict["accepted"] is True
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    assert PHASE_CLOUD_MEASURE in c.accepted_phases
    # The geometry verdict (PR-3b's own field, decided BEFORE the pipeline
    # ever runs) is unaffected either way.
    assert c.group_geometry(PHASE_CLOUD_MEASURE) is not None
    # The pipeline result is honestly None ("never successfully ran"), not a
    # fabricated availability of any kind.
    assert c.group_cloud_result(PHASE_CLOUD_MEASURE) is None


def test_an_unnamed_exception_family_still_propagates_through_the_outer_wrap(
    monkeypatch,
):
    """N1 review finding (2026-07-27): ``_close_cloud_group``'s own comment
    used to claim its outer wrap around ``_run_cloud_pipeline`` made the
    "pipeline exception cannot cost the accept" invariant "structurally true
    rather than merely usually true" — unconditionally. It is not: the wrap
    only catches the same six named types
    (OSError, RuntimeError, TypeError, ValueError, IndexError, AttributeError)
    ``assemble_cloud_group_result``'s own docstring discloses.
    ``test_a_cloud_pipeline_exception_never_costs_the_group_its_accept``
    (immediately above) proves a NAMED family (``RuntimeError``) is caught;
    this proves the complementary residual — a ``KeyError``, outside that
    family, is NOT caught here either and propagates straight through
    ``_close_cloud_group``, costing the group its accept (no ``PhaseVerdict``
    is ever returned; the whole ``consume_capture`` call raises).
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    def _boom(*_a, **_k):
        raise KeyError("synthetic unnamed-family pipeline bug")

    monkeypatch.setattr(flow, "assemble_cloud_group_result", _boom)

    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)

    with pytest.raises(KeyError):
        _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)


def test_close_cloud_group_calls_the_combiner_exactly_once(monkeypatch):
    """S3 review finding, 2026-07-26 (timing sanity). The round-1 draft of
    this wiring called :func:`combine_cloud_positions` TWICE per group close
    — once for the retry-gating verdict via the old ``cloud_geometry_verdict``
    seam, once more from the honest-instrument pipeline. The two calls were
    byte-for-byte identical, but measured at 5.6-6.2 s per call on a laptop
    (interpreter-bound ``smooth_fractional_octave``), so the second call was
    pure operator wait with no evidentiary value — the fix (``_close_cloud_
    group`` combines once and both consumers read the same ``combined``
    object) is what this test pins.

    Wraps the REAL combiner (unlike ``_lock`` below, which stubs out
    ``_geometry_verdict_from_combined`` entirely) so the call COUNT is the
    only thing under test; the wrapped function still returns the genuine
    combined result, so the rest of the group-close path (geometry verdict,
    pipeline result) runs exactly as it would in production.
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    calls: list[int] = []
    real_combine = flow.combine_cloud_positions

    def _counting_combine(positions):
        calls.append(len(positions))
        return real_combine(positions)

    monkeypatch.setattr(flow, "combine_cloud_positions", _counting_combine)

    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)

    assert verdict["accepted"] is True
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    # The group-end combine ran exactly ONCE for this close — not once for
    # the retry gate and again for the pipeline.
    assert len(calls) == 1
    assert calls[0] == len(CLOUD_MEASURE_INDEXES)


def test_cloud_position_retry_budget_is_per_position_not_per_group():
    """Eight prompted positions are eight independent captures. Collapsing them
    onto the phase's cumulative counter would let retakes early in a group
    refuse a later position that has not failed at all."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)

    first, second = CLOUD_MEASURE_INDEXES[0], CLOUD_MEASURE_INDEXES[1]
    fakes.verify = lambda program: _verify_analysis(program, locate_confidence=0.0)
    verdict = _run_phase(c, first, attempt)
    attempt += 1
    assert verdict["accepted"] is False
    fakes.verify = _verify_analysis
    _run_phase(c, first, attempt)  # the retake at the SAME index is admitted
    attempt += 1
    # ... and the NEXT position starts with a clean budget, not the previous
    # position's spent one.
    c.authorize_begin(second, attempt)
    assert c.armed_capture == (second, attempt)


def test_cloud_position_qc_rejects_a_capture_with_no_usable_summed_response():
    """Per-position work is light — but not absent: a position that yielded no
    curve is not evidence, so it is retaken rather than combined."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    index = CLOUD_MEASURE_INDEXES[0]

    fakes.verify = lambda program: replace(
        _verify_analysis(program), summed_response=None,
    )
    verdict = _run_phase(c, index, attempt)
    assert verdict["accepted"] is False
    assert c.group_positions(PHASE_CLOUD_MEASURE) == ()


def test_geometry_locked_group_asks_for_wider_retakes_then_proceeds(monkeypatch):
    """`geometry.locked` is the one actionable thing the geometry instrument can
    say ("spread the mic further"), so the group asks — twice at most, then
    proceeds with the verdict disclosed. Unbounded retrying against a
    source-fixed defect would never terminate, because no mic move decorrelates
    a null that does not move."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    prompts = []
    for _ in range(GEOMETRY_RETRY_POSITIONS):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        assert verdict["accepted"] is False
        assert verdict["code"] == REASON_CLOUD_GEOMETRY_LOCKED
        prompts.append(verdict["prompt"])
        assert PHASE_CLOUD_MEASURE not in c.accepted_phases
        # The too-close take leaves the cloud — that is what a RETAKE is, the
        # only lever the fixed-length runner offers. Not a claim that dropping
        # beats appending: that claim was withdrawn in review (appending fills
        # the null further), so this asserts the mechanism, not a merit.
        assert last not in {
            int(pid.rsplit("_", 1)[1])
            for pid in c.group_positions(PHASE_CLOUD_MEASURE)
        }
    # Two rungs, so the second ask is a different instruction, not a repeat.
    assert prompts == list(CLOUD_GEOMETRY_RETRY_PROMPTS[:GEOMETRY_RETRY_POSITIONS])
    assert len(set(prompts)) == len(prompts)

    # Bounded: the third take is ACCEPTED even though geometry is still locked,
    # with the verdict disclosed rather than the household stuck.
    verdict = _run_phase(c, last, attempt)
    assert verdict["accepted"] is True
    assert verdict["geometry"]["locked"] is True
    assert c.group_geometry(PHASE_CLOUD_MEASURE)["locked"] is True
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_two_geometry_asks_leave_one_household_retry_in_the_pooled_budget(
    monkeypatch,
):
    """Two speaker asks spend two pooled extras; the third remains household.

    There is no geometry discount and no separate quality-failure budget.
    The planned close asks for the first wider take; that rejection asks for
    the second. Those two conductor-initiated extras leave exactly one of the
    position's three pooled extras for the household after an ordinary locate
    miss.
    """
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    # Two geometry retakes — good captures, wider spots.
    for _ in range(GEOMETRY_RETRY_POSITIONS):
        assert _run_phase(c, last, attempt)["code"] == REASON_CLOUD_GEOMETRY_LOCKED
        attempt += 1

    # Now ONE ordinary failure at that same position. It lands on the second
    # speaker-booked extra and asks for the sole remaining household extra.
    monkeypatch.undo()
    fakes.verify = lambda program: _verify_analysis(program, locate_confidence=0.0)
    verdict = _run_phase(c, last, attempt)
    attempt += 1
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_LOCATE_FAILED
    assert verdict["attempts"] == {
        "used": 2,
        "allowed": flow.MAX_EXTRA_ATTEMPTS_PER_POSITION,
        "left": 1,
        "by_speaker": 2,
        "by_household": 0,
    }

    # ...and the final pooled extra is the household's retry.
    fakes.verify = _verify_analysis
    verdict = _run_phase(c, last, attempt)
    assert verdict["accepted"] is True
    assert verdict["attempts"]["by_speaker"] == 2
    assert verdict["attempts"]["by_household"] == 1
    assert verdict["attempts"]["left"] == 0
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_a_spent_cloud_position_is_attributed_and_the_group_continues(
    monkeypatch, caplog,
):
    """The pooled bound is finite, and hitting it does NOT kill the session.

    Transformed from ``test_the_geometry_discount_is_capped_and_still_refuses_a_runaway``,
    which pinned the behaviour the owner ruled against (#2086): the slot's
    budget "still bites" with a terminal ``CaptureBeginRefused`` raised BEFORE
    any audio plays, while the phone's screen still said "try again". The bound
    is still finite — that half of the old test is what this keeps — but the
    fourth failure now settles the position instead of the session.
    """
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    attempt = _walk(c, (1, 2), 1)
    index = CLOUD_MEASURE_INDEXES[0]

    # A non-terminal position (not the group's last) can only fail on quality.
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, pilot_snr_ok=True,
    )
    # The planned capture plus two extras: still ordinary retries.
    for extra in (0, 1, 2):
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is False
        assert verdict["attempts"]["used"] == extra

    # The last extra. FINITE — the flow stops asking — and honest: the position
    # is marked unresolved carrying the observed condition, and the group
    # advances rather than the session dying at the microphone.
    verdict = _run_phase(c, index, attempt)
    attempt += 1
    assert verdict["accepted"] is True
    assert verdict["unresolved"] == {
        "index": index,
        "code": REASON_LOCATE_FAILED,
        "diagnosis": locate_failed_diagnosis(True),
    }
    assert verdict["attempts"]["left"] == 0
    spent = [
        record.getMessage() for record in caplog.records
        if "crossover_v2_position_attempts_spent" in record.getMessage()
    ]
    assert len(spent) == 1
    assert f'diagnosis="{locate_failed_diagnosis(True)}"' in spent[0]
    assert "pilot_heard=true" in spent[0]
    assert "observed=locate_failed" in spent[0]
    # Nothing was retained for it — an unresolved position is not evidence.
    assert index not in {
        int(pid.rsplit("_", 1)[1])
        for pid in c.group_positions(PHASE_CLOUD_MEASURE)
    }
    # …and the NEXT prompted position is admitted normally, with its own budget.
    second = CLOUD_MEASURE_INDEXES[1]
    c.authorize_begin(second, attempt)
    assert c.armed_capture == (second, attempt)


# ===========================================================================
# The bounded-retry ruling (owner, 2026-08-03, issue #2086). One prompted
# position gets the planned capture plus THREE extra attempts, pooled across
# everyone who can ask for one; exhaustion attributes and degrades rather than
# killing the session with copy that says "try again".
# ===========================================================================


def test_every_retriable_reason_has_one_structured_diagnosis_source():
    """Exhaustive negative guard for the count-only regression.

    Every retriable registry row must carry a diagnosis, and its historical
    retryable message/banner must be composed from that same value. Adding a
    new retriable code as a bare literal fails here before exhaustion can ship
    generic count-only copy for it.
    """
    retriable = {
        code: spec for code, spec in REASON_REGISTRY.items()
        if spec.retry_budget > 0
    }
    assert retriable
    for code, spec in retriable.items():
        assert spec.retry_copy is not None, code
        assert (spec.message or spec.banner) == spec.retry_copy.message, code
        assert flow.reason_diagnosis(code, spec), code


@pytest.mark.parametrize(
    ("analysis_kwargs", "expected_code"),
    [
        ({"linearity": False}, refusal_copy.REASON_AGC_BEHAVIORAL_FAIL),
        ({"pilot_snr_ok": False}, refusal_copy.REASON_SNR_FLOOR),
    ],
)
def test_non_special_reasons_keep_their_diagnosis_on_the_final_extra(
    analysis_kwargs, expected_code,
):
    """Representative literal reasons terminate with X, never count alone."""
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, **analysis_kwargs)
    c = _conductor(fakes)

    for attempt in range(1, flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 2):
        verdict = _run_phase(c, 1, attempt)

    diagnosis = flow.reason_diagnosis(
        expected_code, REASON_REGISTRY[expected_code]
    )
    assert verdict["code"] == expected_code
    assert verdict["terminal"] is True
    assert verdict["reason"].startswith(diagnosis)
    assert "try again" not in verdict["reason"].lower()
    assert "cannot continue" in verdict["reason"].lower()


def test_verify_inconclusive_keeps_its_measured_reflection_at_exhaustion():
    """#2095 evidence and #2097 terminal action stay on the same capture."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(
        program,
        max_db=0.5,
        gate_ms=5.0,
        floor_source=gating.FLOOR_MEASURED,
    )

    for attempt in range(3, 3 + flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 1):
        verdict = _run_phase(c, 3, attempt)

    diagnosis = refusal_copy.verify_inconclusive_diagnosis(True)
    assert verdict["terminal"] is True
    assert verdict["reflection_measured"] is True
    assert verdict["reason"].startswith(diagnosis)
    assert "try again" not in verdict["reason"].lower()


def test_the_extra_try_bound_is_pooled_across_initiators(monkeypatch):
    """Ruling item 1 + 4, replayed on the shape that killed the 2026-08-03
    verify: at position index 6 the flow spent locate_failed, two geometry
    rungs, then locate_failed again — five attempts at one spot, because each
    reason code held its own budget and the geometry discount forgave two more.
    The sixth begin was refused pre-play.

    One pooled meter now covers all of it. The bound is shared (a geometry rung
    spends an extra like anything else), and the accounting is not (it is
    booked to the speaker, because the speaker is who asked)."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    # The planned capture, then the wider retake the speaker asks for.
    for _ in range(GEOMETRY_RETRY_POSITIONS):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        assert verdict["code"] == REASON_CLOUD_GEOMETRY_LOCKED
    assert verdict["attempts"]["by_speaker"] == 1
    assert verdict["attempts"]["by_household"] == 0

    # The geometry ladder is spent; ordinary quality failures follow.
    monkeypatch.undo()
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, pilot_snr_ok=True,
    )
    verdict = _run_phase(c, last, attempt)
    attempt += 1
    assert verdict["code"] == REASON_LOCATE_FAILED
    # The take the speaker asked for is still the speaker's ask.
    assert verdict["attempts"] == {
        "used": 2, "allowed": 3, "left": 1, "by_speaker": 2, "by_household": 0,
    }

    # The household's own try is the third and last extra.
    verdict = _run_phase(c, last, attempt)
    attempt += 1
    assert verdict["attempts"] == {
        "used": 3, "allowed": 3, "left": 0, "by_speaker": 2, "by_household": 1,
    }
    # FINITE and honest: the position carries the condition actually observed,
    # and the group closes with what it has instead of the session dying.
    assert verdict["accepted"] is True
    assert verdict["unresolved"] == {
        "index": last,
        "code": REASON_LOCATE_FAILED,
        "diagnosis": locate_failed_diagnosis(True),
    }
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_an_accepted_capture_leaves_the_positions_extras_intact():
    """Ruling item 4. A position measured cleanly on its planned take has spent
    nothing, so a household that chooses to redo it gets the full three tries.

    This is the compounding defect from #2086: acceptance popped the reason but
    left the cumulative counter standing, so ONE voluntary retake of a healthy
    position landed in a meter with zero headroom and the next begin killed the
    session. Here the retakes all fail and the session survives — the earlier
    take was never lost, which is what makes giving up on it safe."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    index = CLOUD_MEASURE_INDEXES[0]

    verdict = _run_phase(c, index, attempt)
    attempt += 1
    assert verdict["accepted"] is True
    assert verdict["attempts"]["left"] == 3, "an accepted take consumes no extra"

    fakes.verify = lambda program: _verify_analysis(program, locate_confidence=0.0)
    for extra in (1, 2):
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is False
        assert verdict["attempts"]["by_household"] == extra

    # The third failed retake settles the slot — and because the ORIGINAL take
    # is still retained, nothing is unresolved: the earlier measurement stands.
    verdict = _run_phase(c, index, attempt)
    attempt += 1
    assert verdict["accepted"] is True
    assert verdict["kept_earlier_take"] is True
    assert "unresolved" not in verdict
    assert index in {
        int(pid.rsplit("_", 1)[1])
        for pid in c.group_positions(PHASE_CLOUD_MEASURE)
    }


def test_a_group_that_cannot_reach_the_floor_ends_honestly_not_with_retry_copy():
    """Ruling item 3's second half. When the phase genuinely cannot proceed the
    session does end — but the copy names the tries that were spent, never an
    action the flow will refuse. The pre-play refusal whose screen said "measure
    again" is the exact shape the owner ruled out."""
    fakes = FakeSeams()
    fakes.apply_done = True
    # A one-position verify group: giving its only position up would leave zero
    # curves, which is below MIN_RESOLVED_CLOUD_POSITIONS with nothing left to
    # walk, so this is the honest-terminal branch.
    c = _conductor(
        fakes,
        index_phase_map=SHORT_VERIFY_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    index = SHORT_VERIFY_CLOUD_INDEXES[0]
    attempt = _walk(c, (1,), 1)

    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, pilot_snr_ok=True,
    )
    for _ in range(flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 1):
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is False
    assert verdict["attempts"]["left"] == 0
    # The final capture itself is terminal — no retry screen/button survives
    # until a doomed next begin — and the group did NOT close: there is no
    # cloud to close with.
    assert verdict["terminal"] is True
    assert verdict["terminal_outcome"] == "below_position_floor"
    assert verdict["reason"].startswith(locate_failed_diagnosis(True))
    assert "try again" not in verdict["reason"].lower()
    assert "too few positions" in verdict["reason"].lower()
    assert PHASE_CLOUD_VERIFY not in c.accepted_phases

    # Defensive replay backstop remains diagnosis-identical.
    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(index, attempt)
    assert excinfo.value.code == REASON_LOCATE_FAILED, "attribute the observation"
    assert "3 extra tries" in excinfo.value.user_message
    assert excinfo.value.user_message.startswith(
        locate_failed_diagnosis(True)
    )
    assert "too few positions" in excinfo.value.user_message.lower()


def test_a_spent_final_slot_terminalizes_its_close_time_refusal():
    """The cloud-close hard stop replaces, rather than hides behind, X.

    The last verify-cloud position spends its pooled extras on locate misses.
    The group can still close without that spot, but its delta probe then
    refuses with ``correction_model_error``. That closing finding is the final
    truth: publish its exact code/copy as terminal on THIS capture, never the
    earlier locate diagnosis plus a retry the ledger cannot admit.
    """
    fakes = FakeSeams()
    fakes.apply_done = True
    c = _conductor(
        fakes,
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    # Isolate the close seam under test from delta-probe AND round-grading
    # arithmetic; the real classifier's mapping/copy is independently
    # exhaustive below. Injected at ``_grade_round_once`` rather than at the
    # probe's own seam because the fifth-principle routing deleted that seam:
    # a close-time refusal is now the ROUND's answer, and this is where it
    # enters the close.
    c._grade_round_once = (  # type: ignore[method-assign]
        lambda verdict: (
            flow.PhaseVerdict(False, REASON_CORRECTION_MODEL_ERROR)
            if c.current_phase == PHASE_CLOUD_VERIFY
            else verdict
        )
    )

    attempt = _walk(c, (VERIFY_INDEX, *CLOUD_VERIFY_INDEXES[:-1]), 1)
    last = CLOUD_VERIFY_INDEXES[-1]
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, pilot_snr_ok=True,
    )
    for _ in range(flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 1):
        verdict = _run_phase(c, last, attempt)
        attempt += 1

    closing_copy = REASON_REGISTRY[REASON_CORRECTION_MODEL_ERROR].message
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_MODEL_ERROR
    assert verdict["reason"] == closing_copy
    assert verdict["terminal"] is True
    assert verdict["terminal_outcome"] == "phase_cannot_proceed"
    assert verdict["attempts"]["left"] == 0
    assert "unresolved" not in verdict
    assert "could hear the speaker" not in verdict["reason"]
    assert "previous sound has been put back" in verdict["reason"]


def test_no_exhaustion_refusal_ever_carries_a_reasons_try_again_copy():
    """The ruling's hard prohibition, pinned over the WHOLE registry rather than
    one code: a refusal reached by spending a position's extras must never
    publish the reason's own action sentence, because every retriable one of
    those ends by inviting a retry the flow will not grant.

    Mutation-checked: reverting ``authorize_begin``'s exhaustion arm to the old
    ``raise CaptureBeginRefused(spec.code, spec.message or spec.banner)`` fails
    this. The message is taken from a REAL refusal rather than from the
    formatter, because a test that only inspects the formatter passes happily
    while the refusal publishes something else entirely."""
    retriable = [
        code for code in REASON_REGISTRY
        if code not in flow.NON_RETRIABLE_CODES
    ]
    assert retriable, "fixture sanity: the registry has retriable codes"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, locate_confidence=0.01)
    c = _conductor(fakes)
    for attempt in range(1, flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 2):
        assert _run_phase(c, 1, attempt)["accepted"] is False
    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 2)
    published = excinfo.value.user_message

    assert "try again" not in published.lower()
    assert "measure again" not in published.lower()
    for code in retriable:
        spec = REASON_REGISTRY[code]
        assert published != (spec.message or spec.banner), (
            f"{code}: an exhaustion refusal must not republish retry copy"
        )


def test_thin_evidence_lock_is_disclosed_not_retried(monkeypatch):
    """``thin_evidence`` marks a verdict resting on the bare minimum usable echo
    estimates — a cliff, not a gradient (GeometryLock's own docstring). Spending
    two more prompted positions on that basis buys a verdict the instrument
    already qualifies, so a thin lock is accepted and disclosed."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    _lock(monkeypatch, thin=True)

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert verdict["accepted"] is True
    assert verdict["geometry"]["locked"] is True
    assert verdict["geometry"]["thin_evidence"] is True
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_the_three_unprompted_phases_each_bank_a_take_of_their_own():
    """CHECK, MEASURE and VERIFY produce a banked take, like every other phase.

    Before this they produced none at all: their arms were the three the
    dispatch handed no ``index``, no ``attempt`` and no ``result``, so there
    was no identity to bank one under and no bytes to bank. Offline analyze
    could see a session's positions and its baseline and simply not its
    CHECK, its MEASURE or its VERIFY.

    What is banked is the CAPTURE — the digest, the identity, and the complex
    responses it measured — because that is what makes an offline replay of
    these phases possible at all. Their VERDICTS are not duplicated into the
    take: those live where each phase already puts them, and are rewritten
    inside a round that a take outlives. The curves are pinned separately, by
    ``test_every_banked_kind_carries_the_phase_its_analysis_measured``.
    """
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), bank_take=bank_into(retained)),
        index_phase_map=CLOUD_MAP,
    )
    _walk(c, (1, 2), 1)

    # VERIFY is stage 2's, so it takes the stage-2 shape to reach.
    verify_retained: list = []
    verify_fakes = FakeSeams()
    stage2 = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            verify_fakes.seams(), bank_take=bank_into(verify_retained),
        ),
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    _run_phase(stage2, VERIFY_INDEX, 1)

    # The LIST, not a phase-keyed dict: one take per accepted capture, and a
    # dict would quietly collapse a double-bank into the single entry this pin
    # was looking for — the real store would not catch it either, because two
    # banks of one record are byte-identical and therefore idempotent.
    all_banked = retained + verify_retained
    assert [meta["phase"] for meta in all_banked] == [
        PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY,
    ]
    banked = {meta["phase"]: meta for meta in all_banked}
    # Every one carries the identity a replay resolves it by, and the digest
    # that verifies the bytes it finds.
    for phase in (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY):
        take = banked[phase]
        assert take["session_id"] == SESSION
        assert take["index"] > 0
        assert take["attempt"] > 0
        assert take["wav_sha256"]
        assert take["captured_at"]


def _phase_probe(analysis):
    """The same analysis with a genuinely COMPLEX transfer function.

    The shipped fixtures build ``complex_tf`` as ``10 ** (mag / 20)`` cast to
    complex — real and positive, so every phase is exactly 0.0 and a banked
    ``phase_deg`` of all zeros would pass a round-trip that never carried
    phase at all. This winds a ramp onto the same magnitudes so the assertion
    has something to be wrong about.
    """
    import numpy as np

    def _wind(response):
        size = np.asarray(response.freqs_hz).size
        return replace(
            response,
            complex_tf=np.abs(response.complex_tf) * np.exp(
                1j * np.linspace(-9.0, 9.0, size)
            ),
        )

    return replace(
        analysis,
        driver_responses=tuple(_wind(r) for r in analysis.driver_responses),
        summed_response=(
            _wind(analysis.summed_response)
            if analysis.summed_response is not None else None
        ),
    )


def _walk_to_banked_take(phase: str) -> tuple[dict, object]:
    """Walk a real session to ``phase`` and return its banked take + analysis.

    The analysis comes back beside the take because the assertion is an
    agreement between them: what the capture measured, and what the record
    says it measured.
    """
    seen: dict = {}

    def _probe(factory, key):
        def make(program, **kw):
            seen[key] = _phase_probe(factory(program, **kw))
            return seen[key]
        return make

    fakes = FakeSeams()
    fakes.measure = _probe(_measure_analysis, PHASE_MEASURE)
    # Every cloud position and VERIFY play the same verify-shaped summed
    # sweep, so one factory serves both and the last one analysed is the take
    # this walk is about.
    fakes.verify = _probe(_verify_analysis, "summed")
    retained: list = []
    if phase == PHASE_VERIFY:
        c = _conductor(
            fakes,
            seams=replace(fakes.seams(), bank_take=bank_into(retained)),
            index_phase_map=STAGE2_MAP,
            accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
            applied=True,
        )
        _run_phase(c, VERIFY_INDEX, 1)
    else:
        c = CrossoverV2Session(
            session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
            fc_hz=FC_HZ, driver_caps_dbfs=CAPS,
            session_volume_db=SESSION_VOLUME_DB,
            seams=replace(fakes.seams(), bank_take=bank_into(retained)),
            index_phase_map=CLOUD_MAP,
        )
        attempt = _walk(c, (1, 2), 1)
        if phase == PHASE_CLOUD_MEASURE:
            _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    take = next(meta for meta in retained if meta["phase"] == phase)
    return take, seen[PHASE_MEASURE if phase == PHASE_MEASURE else "summed"]


@pytest.mark.parametrize(
    "phase,expected_roles",
    [
        (PHASE_MEASURE, ["woofer", "tweeter"]),
        (PHASE_VERIFY, ["summed"]),
        (PHASE_CLOUD_MEASURE, ["summed"]),
    ],
)
def test_every_banked_kind_carries_the_phase_its_analysis_measured(
    phase, expected_roles,
):
    """Ruling S3's other half — the acceptance row's *"``DriverResponse``
    banked"*, for the kinds that were not a walk pose.

    One kind banked phase and two did not: a pose carried ``curves``, the entry
    baseline carried magnitude alone, and a cloud seat and an unprompted-phase
    take carried no curve at all. Every one of those analyses computed the
    complex response in-process and dropped it, so a re-analysis re-derived
    phase from the WAVs and the forward model could never run from the bank.

    Asserted as a RECONSTRUCTION, not as key presence: the banked pair IS the
    transfer function, so the test rebuilds it and compares against the
    analysis's own values at the bins the record names.

    Parametrized by CARRY, deliberately: dropping one hop must red one row
    rather than the file. Two of the three hops are here — ``PHASE_MEASURE``
    and ``PHASE_VERIFY`` share ``_bank_phase_capture``'s single carry (one hop
    under two programs, so both rows go red together, which is what one hop
    breaking means), and ``PHASE_CLOUD_MEASURE`` is ``_retain_cloud_position``'s.
    The third, ``_retain_entry_baseline``'s, is pinned beside that phase's own
    retention tests in ``tests/test_crossover_v2_entry_baseline.py``.
    """
    import numpy as np

    take, analysis = _walk_to_banked_take(phase)
    sources = {
        r.role: r for r in (
            analysis.driver_responses
            or ((analysis.summed_response,) if analysis.summed_response else ())
        )
    }

    assert [curve["role"] for curve in take["curves"]] == expected_roles
    for curve in take["curves"]:
        source = sources[curve["role"]]
        rebuilt = 10.0 ** (np.asarray(curve["magnitude_db"]) / 20.0) * np.exp(
            1j * np.radians(np.asarray(curve["phase_deg"]))
        )
        # By the record's OWN account of which bins it sampled, not by
        # re-deriving the sampler here — that is the claim ``freqs_hz`` makes.
        at = {float(hz): i for i, hz in enumerate(source.freqs_hz)}
        sampled = [at[hz] for hz in curve["freqs_hz"]]
        assert np.allclose(rebuilt, np.asarray(source.complex_tf)[sampled])
        # Not vacuous: the fixture's wound phase really is non-zero.
        assert np.any(np.abs(np.asarray(curve["phase_deg"])) > 1.0)


def test_a_check_take_banks_no_curve_because_check_measures_none():
    """CHECK solves gains off pilots and computes no transfer function at all.

    The honest empty list, not an omission and not a claimed clean curve —
    ``_analyze_check`` returns a ``ProgramAnalysis`` with neither
    ``driver_responses`` nor ``summed_response``, so there is nothing for the
    carry to serialize and the record says exactly that.
    """
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), bank_take=bank_into(retained)),
        index_phase_map=CLOUD_MAP,
    )
    _walk(c, (1, 2), 1)

    banked = {meta["phase"]: meta for meta in retained}
    assert banked[PHASE_CHECK]["curves"] == []
    assert banked[PHASE_MEASURE]["curves"] != []


def test_a_verify_take_banks_the_kind_its_own_round_can_derive():
    """VERIFY classifies; it does not bank an unresolved kind it could resolve.

    ``take_kind`` needs two named fingerprints: the graph this capture went
    through, and the round's pre-apply comparand. By VERIFY the session holds
    both — the entry baseline stage 1 took is what a post-apply re-measure is
    post-apply OF — so leaving the comparand unstated would bank ``""`` for a
    take whose kind the round already knows. CHECK and MEASURE genuinely
    cannot: CHECK is kindless by design, and MEASURE's comparand is minted
    after it banks.

    The two fingerprints must also DIFFER, which is what makes this a verify
    rather than a baseline — the same graph on both sides is the round that
    changed nothing.
    """
    retained: list = []
    fakes = FakeSeams()
    c = _conductor(
        fakes,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained),
            entry_graph_fingerprint=lambda: "fp-after-the-apply",
        ),
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    # The fixture's stage-2 baseline, whose graph is "fixture_entry_graph" —
    # named, and not the post-apply one above.
    assert c.measure_entry_baseline is not None

    _run_phase(c, VERIFY_INDEX, 1)

    banked = [m for m in retained if m["phase"] == PHASE_VERIFY]
    assert len(banked) == 1
    assert banked[0]["measure_kind"] == MEASURE_KIND_VERIFY


def test_an_unprompted_take_is_named_the_way_the_entry_baseline_named_its_own():
    """One take-id convention across the four phases that prompt no spot.

    The entry baseline hit this first — a retained capture with no table row —
    and answered it by minting the position id from the phase and the index, so
    that once ``take_id_for`` qualifies it by attempt the position id IS the
    take id. A second convention here would mean a reader had to know which
    phase wrote a take before it could parse its name.
    """
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), bank_take=bank_into(retained)),
        index_phase_map=CLOUD_MAP,
    )
    _walk(c, (1, 2), 1)

    named = {meta["phase"]: meta for meta in retained}
    assert named[PHASE_CHECK]["take_id"] == f"{PHASE_CHECK}_01_a01"
    assert named[PHASE_MEASURE]["take_id"] == f"{PHASE_MEASURE}_02_a02"
    # The coincidence the entry baseline records: no prompted spot of its own,
    # so the position id and the take id are one string.
    for take in named.values():
        assert take["position_id"] == take["take_id"]


def _refuse_check(fakes):
    fakes.check = lambda program: _check_analysis(program, linearity=False)


def _refuse_measure(fakes):
    fakes.measure = lambda program: _measure_analysis(program, linearity=False)


def _refuse_verify(fakes):
    fakes.verify = lambda program: _verify_analysis(program, linearity=False)


@pytest.mark.parametrize(
    "phase", [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
)
def test_a_refused_capture_of_an_unprompted_phase_banks_nothing(phase):
    """Accepted-only, the rule every other retained kind already follows.

    A refused capture is evidence about the room or the phone, not about the
    speaker, and the journal is where that is recorded. Banking one would put a
    take in the bundle that the round never graded and offline analyze would
    have to learn to skip.

    Parametrized over all three because the rule is one rule and the arms are
    three call sites: pinning it on CHECK alone left deleting the guard from
    the MEASURE or the VERIFY arm invisible to the whole suite.
    """
    # Resolved here rather than in the parametrize: ``VERIFY_INDEX`` is
    # imported below this point in the module, so a decorator that named it
    # would not collect.
    refuse, warmup, index, stage_2 = {
        PHASE_CHECK: (_refuse_check, (), 1, False),
        PHASE_MEASURE: (_refuse_measure, (1,), 2, False),
        PHASE_VERIFY: (_refuse_verify, (), VERIFY_INDEX, True),
    }[phase]

    retained: list = []
    fakes = FakeSeams()
    refuse(fakes)
    kwargs = (
        {"index_phase_map": STAGE2_MAP,
         "accepted_phases": (PHASE_CHECK, PHASE_MEASURE), "applied": True}
        if stage_2 else {"index_phase_map": CLOUD_MAP}
    )
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), bank_take=bank_into(retained)),
        **kwargs,
    )
    for warm in warmup:
        _run_phase(c, warm, 1)

    verdict = _run_phase(c, index, 1)

    assert verdict["accepted"] is False
    assert [m for m in retained if m["phase"] == phase] == []
    # ...and the warm-up captures that WERE accepted still banked, so this is
    # reading a refusal rather than a seam that never fired.
    assert len(retained) == len(warmup)


def test_the_bank_seam_gets_every_accepted_position_with_its_prompt():
    """The forensic record the choreography owes: the prompt is the only durable
    statement of WHERE a curve was measured."""
    retained: list = []
    fakes = FakeSeams()
    seams = replace(
        fakes.seams(),
        bank_take=bank_into(retained, phase=PHASE_CLOUD_MEASURE),
    )
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=seams, index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)

    assert [meta["position_id"] for meta in retained] == [
        f"{PHASE_CLOUD_MEASURE}_{i:02d}" for i in CLOUD_MEASURE_INDEXES
    ]
    prompts = [meta["prompt"] for meta in retained]
    assert prompts == [p.text for p in CLOUD_POSITION_PROMPTS[: len(retained)]]
    assert sum(1 for meta in retained if meta["wide"]) >= 2
    # Each position's NAMED QUESTION rides its record, from the same table row
    # the prompt came from (attribution-stage plan §5 promotion-queue item 1).
    # The prompt string cannot be parsed back into a role, so the label is the
    # only way the attribution stage sees a labelled sample rather than an
    # anonymous member of an average — and it has to be the row's, not a guess.
    roles = [meta["role"] for meta in retained]
    assert roles == [p.role for p in CLOUD_POSITION_PROMPTS[: len(retained)]]
    # …and the shipped walk really does sample all three questions, which is
    # the point of labelling them at all: a walk that only ever produced one
    # role would be the same average with extra words.
    assert set(roles) == set(POSITION_ROLES)
    for meta in retained:
        assert meta["phase"] == PHASE_CLOUD_MEASURE
        assert meta["session_id"] == SESSION
        assert meta["captured_at"] > 0


def test_a_verify_pose_banks_its_angle_axis_and_distance_as_fields():
    """(T1-6) WHERE the microphone was, as numbers rather than as English.

    The defect this closes, measured against the banked artifacts of the
    2026-08 new-horn campaign: a ``cloud_verify`` position record carried no
    geometry field at all. Its only statement of place was the household
    ``prompt`` sentence — un-checkable, un-diffable, and the thing a reader
    interpreted as a mic being carried sideways when the rig had rotated.

    The owner's ruling names the three: angle, axis, distance. ``position_deg``
    deliberately spells the word ``lateral_pose_record`` already uses, so there
    is ONE vocabulary for "what bearing was this taken at" rather than two.
    """
    retained: list = []
    fakes = FakeSeams()
    fakes.apply_done = True
    c = _conductor(
        fakes,
        # The SAME ``fakes`` the conductor runs on, with one seam wrapped —
        # a second FakeSeams() here would silently drop ``apply_done``.
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained, phase=PHASE_CLOUD_VERIFY),
        ),
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    _walk(c, (VERIFY_INDEX, *CLOUD_VERIFY_INDEXES), 1)

    assert [m["position_id"] for m in retained] == [
        f"{PHASE_CLOUD_VERIFY}_{i:02d}" for i in CLOUD_VERIFY_INDEXES
    ]
    # The shipped pose set, read back off the records rather than off the
    # table: the design axis first, then the four sides.
    assert [m["position_deg"] for m in retained] == [
        flow.position_angle_deg(p) for p in flow.CLOUD_VERIFY_POSE_PROMPTS
    ] == [0, -7, 7, -22, 22]
    assert {m["position_axis"] for m in retained} == {"horizontal"}
    assert {m["mark_distance_m"] for m in retained} == {flow.MARK_DISTANCE_M}
    # The prompt stays — it is the human instruction — but it is no longer the
    # only place the geometry lives.
    assert [m["prompt"] for m in retained] == [
        p.text for p in flow.CLOUD_VERIFY_POSE_PROMPTS
    ]
    assert all(m["prompt"] for m in retained)


def test_a_vertical_seat_states_its_elevation_and_still_banks_no_bearing():
    """A raised pose commands NO bearing, and 0 would read as the design axis.

    Where it WAS raised to is ``vertical_deg``, derived from the row's own
    ``offset_cm`` against the mark distance exactly as a lateral row's bearing
    is, and signed by the row's own ABOVE/BELOW word — so the two 40 cm rows
    stop being byte-identical records.

    ``position_angle_deg`` still refuses a vertical row outright, and that
    refusal is deliberately kept: it aims an external POSITIONER, and no
    positioner can raise the microphone. This derivation runs on the retention
    path instead, where a raise would fail a capture the household already
    gave, so it states the axis and leaves the angle ``None``.
    """
    vertical = [
        p for p in CLOUD_POSITION_PROMPTS if p.role == flow.POSITION_ROLE_XOVR
    ]
    geometries = [flow.position_geometry(p) for p in vertical]

    assert {g.axis for g in geometries} == {"vertical"}
    assert {g.degrees for g in geometries} == {None}
    assert {g.mark_distance_m for g in geometries} == {flow.MARK_DISTANCE_M}
    assert [g.vertical_deg for g in geometries] == [7, -7, 22, -22]
    for prompt in vertical:
        with pytest.raises(CrossoverV2FlowError):
            flow.position_angle_deg(prompt)


def test_the_compound_retake_rung_states_the_rise_it_asks_for():
    """The one shipped pose that moves BOTH ways states both, or it lies.

    Rung 2 asks for 75 cm sideways AND 30 cm up. Its two displacements differ,
    so a single ``offset_cm`` cannot carry them — and a record that defaulted
    its elevation to 0 would claim mark height for a microphone the household
    was told to raise, and would pair that take against a mark-height baseline.
    """
    rung_2 = flow.CloudPositionPrompt(
        flow.CLOUD_GEOMETRY_RETRY_PROMPTS[1],
        offset_cm=flow.GEOMETRY_RETRY_OFFSET_CM,
        role=flow.POSITION_ROLE_OFFAX,
        vertical_sign=1,
        vertical_offset_cm=flow.CLOUD_GEOMETRY_RETRY_RISE_CM[1],
    )
    geometry = flow.position_geometry(rung_2)

    assert flow.CLOUD_GEOMETRY_RETRY_RISE_CM[1] > 0
    assert geometry.vertical_deg == 17
    # Its lateral distance is the wider one and is NOT what the rise came from.
    assert flow.GEOMETRY_RETRY_OFFSET_CM != flow.CLOUD_GEOMETRY_RETRY_RISE_CM[1]
    # Rung 1 is at mark height and says so.
    assert flow.CLOUD_GEOMETRY_RETRY_RISE_CM[0] == 0.0


def test_a_raised_seat_joins_no_bearing_set_the_walk_already_had():
    """The mixed walk's horizontal aggregates do not notice the raised seats.

    The shipped cloud table is already mixed — seven lateral rows and four
    raised ones. Banking an elevation must not move what the horizontal-only
    consumers see, and the mechanism that guarantees it is ``position_deg``
    staying ``None`` on a raised seat: every pooled bearing set in the tree
    (``evidence_packet._angle_deg_block`` is the one a reader sees) is built by
    filtering for an ``int`` bearing, so a raised seat is excluded there and
    included, AS LABELLED, everywhere a seat is listed.
    """
    geometries = [flow.position_geometry(p) for p in CLOUD_POSITION_PROMPTS]
    bearings = [g.degrees for g in geometries if isinstance(g.degrees, int)]

    assert bearings == [-7, 7, -22, 22, -14, 14, -31]
    assert [
        flow.position_angle_deg(p) for p in CLOUD_POSITION_PROMPTS
        if p.role != flow.POSITION_ROLE_XOVR
    ] == bearings
    # Every lateral seat is at mark height, so the new field says nothing new
    # about any of them — which is why an old bundle missing it reads as 0.
    assert {
        g.vertical_deg for g in geometries if g.axis == "horizontal"
    } == {0}


def test_a_retake_records_the_prompt_it_was_actually_given(monkeypatch):
    """B3: the sidecar's prompt is the only durable statement of WHERE a curve
    was measured. A geometry retake follows a wider-spot rung, not the position
    table's entry — recording the table entry would name a spot the operator
    was explicitly told to abandon."""
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained),
        ),
        index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    _run_phase(c, last, attempt)          # original take, then geometry-rejected
    attempt += 1
    _run_phase(c, last, attempt)          # first wider retake, rejected again
    attempt += 1
    monkeypatch.undo()
    _run_phase(c, last, attempt)          # second wider retake, accepted

    takes = [m for m in retained if m["index"] == last]
    assert len(takes) == 3
    # The original followed the table; both retakes followed their own rung, in
    # order, and are marked wide — the rungs ask for GEOMETRY_RETRY_OFFSET_CM,
    # past the wide class by design, and `wide` is computed from that distance
    # rather than hand-set (the body-part register this comment used to name
    # was withdrawn by #1805's 2026-07-28 ruling).
    assert takes[0]["prompt"] == CLOUD_POSITION_PROMPTS[
        len(CLOUD_MEASURE_INDEXES) - 1
    ].text
    assert takes[1]["prompt"] == CLOUD_GEOMETRY_RETRY_PROMPTS[0]
    assert takes[2]["prompt"] == CLOUD_GEOMETRY_RETRY_PROMPTS[1]
    assert takes[1]["wide"] is True and takes[2]["wide"] is True
    # Each take carries its own attempt — what disambiguates their artifacts.
    assert len({m["attempt"] for m in takes}) == 3
    # Only the LAST is in the cloud.
    surviving = c.group_position_takes(PHASE_CLOUD_MEASURE)
    assert [t["attempt"] for t in surviving if t["index"] == last] == [
        takes[2]["attempt"]
    ]


def test_group_combine_failure_degrades_to_an_unknown_verdict(monkeypatch):
    """A group's captures are already-accepted evidence; a combiner failure must
    not retroactively fail them."""
    def explode(_captures, **_kw):
        raise ValueError("malformed grid")

    # ``cloud_geometry_verdict`` imports the combiner lazily from its own
    # module, so patch it there rather than on the conductor's namespace.
    monkeypatch.setattr(
        "jasper.audio_measurement.spatial_combine.combine_positions", explode
    )
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert verdict["accepted"] is True
    assert verdict["geometry"] == {
        "locked": False, "reason": "combine_failed",
        "n_positions": len(CLOUD_MEASURE_INDEXES),
    }


def test_cloud_session_phases_and_resume_within_the_same_session():
    """§5.6 unchanged: a cloud group interrupted mid-way resumes only within the
    SAME relay session. The session's own phase list rides the snapshot so a
    reader can tell a cloud session from a verify-only re-arm."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    # A STAGE-1 session's phases (work order D1): CHECK, MEASURE, the
    # pre-apply cloud — and deliberately no VERIFY, because the post-apply
    # sweep is stage 2's own session. This tuple is exactly what the wizard's
    # ``_phase_from_state`` reads to resolve the review interlude.
    assert c.session_phases == (
        PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    snap = c.snapshot()
    assert PHASE_CLOUD_MEASURE in snap.accepted_phases
    assert snap.session_phases == c.session_phases

    resumed = CrossoverV2Session.hydrate(
        snap, session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(), index_phase_map=CLOUD_MAP,
    )
    assert PHASE_CLOUD_MEASURE in resumed.accepted_phases
    # Every phase this session runs is accepted; the journey continues in the
    # browser, not in another capture.
    assert resumed.current_phase == PHASE_DONE


def test_a_new_relay_session_invalidates_the_whole_cloud():
    """Mic position is unverifiable across sessions, so a fresh session restarts
    at CHECK — the cloud is evidence like any other phase, never an exception."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)

    fresh = CrossoverV2Session.hydrate(
        c.snapshot(), session_id="cap_a_different_session",
        source_preset=_preset(), roles_bands=_roles(), fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(), index_phase_map=CLOUD_MAP,
    )
    assert fresh.accepted_phases == frozenset()
    assert fresh.current_phase == PHASE_CHECK
    assert fresh.group_positions(PHASE_CLOUD_MEASURE) == ()
    assert fresh.group_geometry(PHASE_CLOUD_MEASURE) is None


def test_verify_only_rearm_session_never_waits_on_a_cloud_it_has_no_captures_for():
    """A conductor walks the phases ITS map addresses. The re-verify re-arm maps
    one index to VERIFY, so it must reach DONE rather than sitting pending on a
    position group that has no entry in its plan."""
    fakes = FakeSeams()
    c = _conductor(
        fakes, index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE),
        applied=True,
    )
    assert c.session_phases == (PHASE_VERIFY,)
    assert c.current_phase == PHASE_VERIFY
    _run_phase(c, 1, 1)
    assert c.current_phase == PHASE_DONE


def test_cloud_positions_play_the_summed_program_and_get_no_tracking_prior():
    """A cloud position is OFF the design axis by construction, so measured-vs-
    predicted divergence there is the spatial variation the cloud exists to
    sample — not a tracking error. Withholding ``predicted_sum`` means no
    tracking claim can be made from a capture that cannot support one."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    _run_phase(c, CLOUD_MEASURE_INDEXES[0], attempt)

    played_phase, played_program = fakes.played[-1]
    assert played_phase == PHASE_CLOUD_MEASURE
    # The conductor's phase and the PROGRAM's phase are different vocabularies:
    # the program is the VERIFY-shaped summed sweep, which is exactly why
    # `analyze_program_capture` needed no new dispatch branch.
    assert played_program.phase == PHASE_VERIFY
    analyzed_phase, prog_phase, _result, priors, _geometry = fakes.analyzed[-1]
    # Issue #1855: the analyze seam must receive the FLOW's phase
    # (cloud_measure), not the program's own phase (verify) — a retention
    # seam that read ``program.phase`` instead mislabeled every cloud
    # position as "verify" because the program is byte-identical to VERIFY's.
    assert analyzed_phase == PHASE_CLOUD_MEASURE
    assert prog_phase == PHASE_VERIFY
    assert priors.predicted_sum is None
    assert priors.crossover_fc_hz == FC_HZ


def test_summed_sweep_phases_share_one_program_object():
    """The byte-safety invariant issue #1976's fix depends on, pinned
    directly (adversarial-gate SF2, PR #2028): ``program_for_phase`` must hand
    the phases that share a program the SAME object, not merely an equal one.
    Each object is composed once in ``__init__`` (see the "Programs" block) and
    returned unchanged — nothing upstream of this test caught a divergence
    here: mutating ``program_for_phase`` to hand cloud phases a
    freshly-composed (value-equal, object-distinct) program left the wider
    suite green, because everything else asserts on program CONTENT
    (segments, gains, ``.phase``), never object identity. If this ever goes
    false, `jasper/web/correction_crossover_v2.py`'s
    ``bind_production_play._play`` writes a ``summed_program.wav`` that is
    NOT what a genuine capture of that phase actually played.

    Since the 2026-08-18 prelude trim there are TWO such objects rather than
    one: the compared pair (VERIFY and the entry baseline, whose ``program_id``
    equality is #2291's before→after check and the delta probe's anchor check)
    and the position groups' unannounced twin. The identity requirement is the
    same for each; what it is not is a claim that all four are one program.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    assert c.program_for_phase(PHASE_CLOUD_MEASURE) is c.program_for_phase(
        PHASE_CLOUD_VERIFY
    )
    assert c.program_for_phase(PHASE_ENTRY_BASELINE) is c.program_for_phase(
        PHASE_VERIFY
    )
    assert c.program_for_phase(PHASE_CLOUD_VERIFY) is not c.program_for_phase(
        PHASE_VERIFY
    )


# --- capture plan (auto-advance policy, §5.2/§5.7) ---------------------------------


def test_capture_plan_entries_carry_auto_advance_policy():
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    assert plan.schema_version == 2
    # RE-DERIVED for the two-stage split (work order D1/D2). The shipped
    # STAGE-1 plan is CHECK + MEASURE + N-1 prompted pre-apply positions:
    # 1 + 1 + 8 = 10 at the Full tier's DEFAULT_CLOUD_MEASURE_POSITIONS = 9.
    # It carries no VERIFY and no post-apply group — those are stage 2's plan,
    # pinned in test_the_stage_2_plan_walks_the_tiers_own_verify_shape.
    # ``cloud_capture_target()`` still names the WHOLE journey (10 + 6),
    # which is what the tier chooser promises. Stage 2's 6 is VERIFY's anchor
    # plus the five poses of ``CLOUD_VERIFY_POSE_PROMPTS`` (2026-08-24 ruling).
    assert plan.capture_target == 10
    assert cloud_capture_target() == 16
    kinds = [entry.kind_label for entry in plan.entries]
    assert kinds == (
        ["check", "measure"]
        + ["cloud_measure"] * (DEFAULT_CLOUD_MEASURE_POSITIONS - 1)
    )
    assert [entry.index for entry in plan.entries] == list(range(10))
    check, measure = plan.entries[0], plan.entries[1]
    # CHECK and MEASURE each take a tap. Every prompted cloud position needs
    # its own tap, because the operator has to physically move the mic
    # between them.
    assert check.screen["auto_advance"] == AUTO_ADVANCE_TAP
    # MEASURE used to auto-advance behind a 5 s cancelable countdown (same
    # spot, no movement needed). Issue #1823: it is also the session's longest
    # capture and the one that can be its loudest, and rolling into it unasked
    # read as the speaker taking a liberty — so it takes a tap, behind copy
    # that says what is coming. The countdown vocabulary is retained for a
    # future same-spot transition; it is simply unused by this entry, so the
    # countdown-only keys are gone with it.
    assert measure.screen["auto_advance"] == AUTO_ADVANCE_TAP
    assert "countdown_s" not in measure.screen
    assert "cancelable" not in measure.screen
    # HEDGED on purpose. #1825/#1829 solve each driver's MEASURE level to the
    # SNR the fit needs in its own band, so a quiet room gets a quiet MEASURE —
    # "louder" flat would be a promise the speaker no longer keeps.
    assert "can be the loudest" in measure.screen["body"]
    assert "louder —" not in measure.screen["body"]
    # The vocabulary itself survives the flip — the page still implements the
    # policy and a future same-spot transition can earn it back — but no
    # SHIPPED entry uses it today. Pinned so "unused, delete it" and "silently
    # reinstated on MEASURE" are both visible changes.
    assert AUTO_ADVANCE_COUNTDOWN_S > 0
    assert all(
        entry.screen.get("auto_advance") != AUTO_ADVANCE_COUNTDOWN
        for entry in plan.entries
    )
    for entry in plan.entries:
        if entry.kind_label.startswith("cloud_"):
            assert entry.screen["auto_advance"] == AUTO_ADVANCE_TAP
            # The redesign's grammar (§2.1): the INSTRUCTION is the title, the
            # supporting clause is the body and may legitimately be empty.
            assert entry.screen["title"]
            assert "body" in entry.screen
    # No entry of a STAGE-1 plan arms on an apply — there is no apply in this
    # session to arm on (work order D1/D10).
    assert all(
        entry.screen.get("auto_advance") != AUTO_ADVANCE_ON_APPLY
        for entry in plan.entries
    )
    # …and the END screen is stage 2's, not stage 1's: nothing here may claim
    # the speaker is tuned. (The generic page fallback a stage-1 plan therefore
    # falls back to is PR-T4's; see the work order's D7 list.)
    assert all("done_title" not in entry.screen for entry in plan.entries)
    # Durations are per-entry (heterogeneous) and positive.
    assert all(entry.duration_ms > 0 for entry in plan.entries)
    assert len({entry.duration_ms for entry in plan.entries}) > 1


def test_capture_plan_index_phase_map_matches_the_emitted_entries():
    """The prompt an entry carries and the phase the conductor runs for that
    index come from the same builder — a drift here would prompt "move left"
    while the conductor analysed a VERIFY."""
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    index_phase = build_v2_cloud_index_phase_map()
    assert len(index_phase) == plan.capture_target
    kind_for_phase = {
        PHASE_CHECK: "check",
        PHASE_MEASURE: "measure",
        PHASE_CLOUD_MEASURE: "cloud_measure",
        PHASE_VERIFY: "verify",
        PHASE_CLOUD_VERIFY: "cloud_verify",
    }
    for entry in plan.entries:
        # Entry indexes are 0-based; the relay's own index space is 1-based.
        assert entry.kind_label == kind_for_phase[index_phase[entry.index + 1]]


# --- commission tiers + the retake/confirm contract (flow-simplification) ----


def test_express_is_a_derived_shape_not_a_loosened_floor():
    """§1.2: express is a distinct NAMED plan, validated on its own terms.

    Its N comes from the prompt table (both wide offsets, no more), its M is 1
    (no post-apply group at all), and the FULL tier's validated floor
    ``MIN_CLOUD_MEASURE_POSITIONS`` does not move to accommodate it — the same
    counts are still refused when asked for as a full-tier configuration.
    """
    express = resolve_plan_shape(TIER_EXPRESS)
    assert express == V2PlanShape(
        tier=TIER_EXPRESS,
        cloud_measure_positions=express_cloud_measure_positions(),
        cloud_verify_positions=1,
    )
    assert (express.capture_target, express.max_attempts) == (7, 14)
    assert express.has_cloud_verify_group is False
    # The full tier is unchanged, and would REFUSE express's own counts.
    full = resolve_plan_shape()
    assert full.tier == TIER_FULL
    assert (full.capture_target, full.max_attempts) == (16, 23)
    assert full.has_cloud_verify_group is True
    with pytest.raises(CrossoverV2FlowError):
        resolve_plan_shape(
            TIER_FULL,
            cloud_measure_positions=express.cloud_measure_positions,
            cloud_verify_positions=1,
        )
    # Express is a fixed shape, so an explicit count that disagrees is refused
    # rather than quietly honoured.
    with pytest.raises(CrossoverV2FlowError):
        resolve_plan_shape(TIER_EXPRESS, cloud_measure_positions=6)


def test_an_unknown_tier_is_refused_and_an_absent_one_means_full():
    """Allowlist, not a guess: absence is the non-breaking default, an
    unrecognised id is a caller asking for an instrument this build does not
    have and must fail loudly rather than measure something else."""
    assert resolve_plan_shape(None).tier == TIER_FULL
    assert resolve_plan_shape("").tier == TIER_FULL
    assert resolve_plan_shape("  EXPRESS  ").tier == TIER_EXPRESS
    for bogus in ("quick", "Full measurement", "expres", "0"):
        with pytest.raises(CrossoverV2FlowError):
            resolve_plan_shape(bogus)


def test_one_resolved_shape_feeds_both_the_spec_and_the_index_phase_map():
    """The desync hazard this value exists to close: the emitted plan and the
    conductor's index→phase map must be derived from the SAME shape, not from
    two functions that happen to share defaults."""
    shape = resolve_plan_shape(TIER_EXPRESS)
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24, plan_shape=shape,
    )
    index_phase = build_v2_cloud_index_phase_map(plan_shape=shape)
    plan = spec.capture_plan
    # Stage 1's own target since the split — the whole-journey
    # ``shape.capture_target`` spans two sessions and no plan emits it.
    assert plan.capture_target == len(index_phase) == shape.measure_capture_target
    assert sorted(index_phase) == [e.index + 1 for e in plan.entries]
    # Handing over two sources of truth at once is refused outright.
    with pytest.raises(CrossoverV2FlowError):
        build_v2_cloud_index_phase_map(plan_shape=shape, cloud_measure_positions=9)


def test_the_post_apply_pose_set_is_a_parameter_with_a_runbook_default():
    """(T1-5) The runbook is a SUGGESTION: the walk takes the set it is given.

    Two halves, and either alone would be a half-fix. The DEFAULT is the
    owner's ratified set — the design axis and the four sides — so a household
    that states nothing gets it. And a caller that states a set gets THAT one,
    down to the prompt copy, because "measure the result at these angles" was
    not a question anyone could ask while the walk re-sliced a fixed table.

    One resolver behind both, so the plan the phone is handed and the session
    that walks it cannot read different tables.
    """
    assert [
        flow.position_angle_deg(p) for p in flow.CLOUD_VERIFY_POSE_PROMPTS
    ] == [0, -7, 7, -22, 22]
    assert flow.verify_pose_table() is flow.CLOUD_VERIFY_POSE_PROMPTS
    assert flow.verify_pose_table(None) is flow.CLOUD_VERIFY_POSE_PROMPTS

    # A caller-supplied set: the same two at-mark-and-one-side poses, and
    # nothing else. Chosen from the shipped table so the assertion is about the
    # SEAM rather than about a hand-built prompt's copy.
    chosen = flow.CLOUD_VERIFY_POSE_PROMPTS[:2]
    assert flow.verify_pose_table(chosen) == chosen

    shape = replace(resolve_plan_shape(), cloud_verify_positions=len(chosen) + 1)
    plan = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=shape, verify_prompts=chosen,
    )
    assert [
        e.screen["title"] for e in plan.entries if e.kind_label == "cloud_verify"
    ] == [p.headline for p in chosen]
    # …and the shape and the set have to agree in BOTH directions.
    #
    # Short table: the walk would prompt fewer spots than the session believes.
    with pytest.raises(CrossoverV2FlowError, match="pose set"):
        build_v2_verify_capture_plan(
            FC_HZ,
            plan_shape=replace(
                resolve_plan_shape(), cloud_verify_positions=len(chosen) + 2,
            ),
            verify_prompts=chosen,
        )
    # LONG table: the quiet one. The poses past ``M - 1`` never reach an entry,
    # so the walk is silently truncated to a prefix — while the orientation
    # sentence is quoted off the WHOLE table and promises a reach the walk does
    # not have. Pinned with a 60 cm sixth pose against the shipped 40 cm walk:
    # unguarded, the plan builds and the consent screen says 70 cm.
    long_table = flow.CLOUD_VERIFY_POSE_PROMPTS + (
        next(p for p in CLOUD_POSITION_PROMPTS if p.offset_cm == 60.0),
    )
    assert flow.cloud_walk_reach_cm_of(long_table) > flow.cloud_walk_reach_cm_of(
        flow.CLOUD_VERIFY_POSE_PROMPTS
    ), "the extra pose must widen the quoted reach, or this pins nothing"
    with pytest.raises(CrossoverV2FlowError, match="pose set"):
        build_v2_verify_capture_plan(
            FC_HZ, plan_shape=resolve_plan_shape(), verify_prompts=long_table,
        )
    # EXPRESS is the shape a bare ``!=`` would break: M = 1 emits no
    # cloud-verify entry at all, so its empty index list must not be measured
    # against the 5-row default. It is correct by construction and builds.
    express = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(TIER_EXPRESS),
    )
    assert express.capture_target == 1
    assert [e.kind_label for e in express.entries] == ["verify"]


def test_the_stage_2_plan_walks_the_tiers_own_verify_shape():
    """Work order D2, owner-confirmed 2026-07-29 — and the re-derivation of
    ``test_an_express_plan_emits_no_cloud_verify_and_ends_on_verify``, whose
    subject (the ``M = 1`` done-screen placement rule) moved out of stage 1's
    builder and into stage 2's along with the post-apply group itself.

    Full's stage 2 is the multi-position spatial walk; Express's is the single
    anchor at the mark. The phone's END screen rides the LAST entry either way
    (``renderPlanAllDone`` reads the final wire index), and Express's copy
    claims LESS because it verified less (§1.3).
    """
    from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS

    full = build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape())
    assert full.capture_target == DEFAULT_CLOUD_VERIFY_POSITIONS == 6
    assert [e.kind_label for e in full.entries] == (
        ["verify"] + ["cloud_verify"] * (DEFAULT_CLOUD_VERIFY_POSITIONS - 1)
    )
    assert [e.index for e in full.entries] == list(range(6))
    # The walk's prompted poses ARE the resolved pose set, in its own order —
    # the 2026-08-24 ruling's design-axis member first, then the four sides.
    assert [
        e.screen["title"] for e in full.entries if e.kind_label == "cloud_verify"
    ] == [p.headline for p in flow.CLOUD_VERIFY_POSE_PROMPTS]
    assert full.entries[-1].screen["done_title"] == "Your speaker is tuned"
    assert "Run a Full measurement" not in full.entries[-1].screen["done_body"]
    # Stage 1's own plan claims nothing about the result any more.
    assert all(
        "done_title" not in e.screen
        for e in build_v2_capture_plan(_roles(), FC_HZ).entries
    )

    express = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(TIER_EXPRESS),
    )
    assert express.capture_target == 1
    assert [e.kind_label for e in express.entries] == ["verify"]
    last = express.entries[-1]
    assert last.screen["done_title"] == "Your speaker is tuned"
    assert "Run a Full measurement" in last.screen["done_body"]
    # The B2-corrected phrase, not the withdrawn one. This line used to pin
    # `"verified-everywhere" in done_body` — an assertion actively holding the
    # overclaim that PR #1780's review had already ruled out on jts.local, so
    # the phone contradicted the wizard on one journey. Pin the shipped wording
    # instead, and pin the withdrawn one OUT so it cannot come back.
    assert (
        "the result checked at several spots around the mark"
        in last.screen["done_body"]
    )
    assert "verified-everywhere" not in last.screen["done_body"]

    # RE-DERIVED budgets. Stage 2 draws its own, from its own target:
    # Full 6 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE, Express 1 + …
    assert full.max_attempts == (
        6 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE
    ) <= MAX_CAPTURE_PLAN_ATTEMPTS
    assert express.max_attempts == (
        1 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE
    ) <= MAX_CAPTURE_PLAN_ATTEMPTS
    # …and its own walked-away ceiling: 1800 + (6-3)*120 / the plain baseline.
    assert session_wall_clock_ceiling_s(full) == 2160.0
    assert session_wall_clock_ceiling_s(express) == 1800.0

    # An express STAGE 1 is a strictly smaller draw than Full's.
    express_stage1 = build_v2_capture_plan(_roles(), FC_HZ, tier=TIER_EXPRESS)
    assert express_stage1.capture_target == 6
    assert [e.kind_label for e in express_stage1.entries] == (
        ["check", "measure"] + ["cloud_measure"] * 4
    )
    assert express_stage1.max_attempts == (
        6 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE
    ) <= MAX_CAPTURE_PLAN_ATTEMPTS
    assert session_wall_clock_ceiling_s(express_stage1) == 2160.0


def test_the_stage_2_done_screen_never_pre_commits_a_verdict_it_cannot_know():
    """#1964: every word of the phone's END screen is written when stage 2 is
    ARMED — before the first tone plays — so it may not assert an outcome the
    session has not measured.

    Full's copy read "Verified and applied.", selected only by
    ``plan_shape.has_cloud_verify_group``. The post-apply cloud's SPEC verdict
    is computed from the LAST capture and can FAIL while the tracking
    comparator passes; on such a session jts.local said "Your speaker is
    tuned, **but** the result still measures further from flat than the
    target…" while the phone in the household's hand said "Verified and
    applied." Two surfaces, one session, and the phone always optimistic.

    Two halves are pinned, because either alone is re-breakable:

    * **Structural** — this builder's entire input is a crossover frequency
      and a plan SHAPE. There is no measured outcome in scope to bind copy to,
      so a future "Verified" here would be as unearned as this one was.
    * **Cross-surface** — whatever the phone bakes has to hold under EVERY
      outcome jts.local can report. It does so by being exactly the claim each
      of jts.local's seven done verdicts OPENS with; jts.local owns the
      divergence, as the only surface whose component vocabulary can carry it.
      All seven are pinned, not the two this fix reasoned about: the phone
      bakes one headline for both tiers and all outcomes, so a single
      unpinned variant is enough to reopen the defect.
    """
    import inspect

    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )
    from jasper.web.correction_crossover_v2 import _post_apply_grade

    # The whole input, enumerated: a crossover frequency (or, on a speaker with
    # none, its declared measurement band), a plan SHAPE, and the POSE SET the
    # walk takes. Not one of the four is a measured outcome, which is the
    # structural half of the claim above — a pose set says where the microphone
    # goes, and a declared band what the speaker can be swept over, never how
    # the result came out.
    assert set(inspect.signature(build_v2_verify_capture_plan).parameters) == {
        "fc_hz", "measurement_band_hz", "plan_shape", "verify_prompts",
    }

    done = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(),
    ).entries[-1].screen
    body = done["done_body"]
    # No verdict vocabulary: the instrument that grades flatness has not
    # reported when these bytes are written.
    assert "verified" not in body.lower()
    assert "spec" not in body.lower()
    # It names the surface that DOES own the verdict instead of guessing it.
    assert "speaker page" in body

    # ONE headline is baked for BOTH tiers…
    express_done = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(TIER_EXPRESS),
    ).entries[-1].screen
    headline = done["done_title"]
    assert express_done["done_title"] == headline

    def _verdict(**v2) -> str:
        # R19: the done screen reads the PRODUCER's grade for the spatial
        # verdict and the scope/completeness fact, so a fixture that describes
        # a session has to carry what that session's state would produce.
        # Deriving it here rather than hand-writing one keeps this a pin on
        # the real path — a fixture that stops reaching its branch shows up as
        # a collapsed variant below, which is exactly what this test counts.
        block = {
            "phase": "done", "verify": {"outcome": "pass"},
            "applied": True, **v2,
        }
        if "post_apply_grade" not in block:
            block = {**block, "post_apply_grade": _post_apply_grade(block)}
        return build_crossover_envelope_v2({
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "crossover_v2": block,
        })["verdict_text"]

    # …so the invariant holds only if EVERY jts.local done verdict opens with
    # it. There are seven, independently authored across the branches of the
    # PHASE_DONE arm, and pinning only the ones a given fix reasoned about
    # would leave the rest free to drift out from under the phone.
    variants = {
        "express": _verdict(tier=TIER_EXPRESS),
        "generic": _verdict(
            tier=TIER_FULL,
            cloud={PHASE_CLOUD_VERIFY: {"overall_passed": True}},
        ),
        "spec_fail": _verdict(
            tier=TIER_FULL,
            cloud={PHASE_CLOUD_VERIFY: {"overall_passed": False}},
        ),
        # R19/#2160: a group that closed and could not grade anything is a
        # third thing, and used to render as the miss above.
        "spec_unmeasurable": _verdict(
            tier=TIER_FULL,
            cloud={PHASE_CLOUD_VERIFY: {
                "overall_passed": False, "flatness": {"evaluable": False},
            }},
        ),
        # R19/#2098: Full verified at the mark and never closed its group.
        "scope_incomplete": _verdict(tier=TIER_FULL),
        "grade_inconclusive": _verdict(
            tier=TIER_FULL,
            post_apply_grade={"graded": False, "state": "inconclusive"},
        ),
        "grade_never_finished": _verdict(
            tier=TIER_FULL, post_apply_grade={"graded": False, "state": ""},
        ),
    }
    assert len(set(variants.values())) == 7, (
        "seven DISTINCT verdicts, or a fixture stopped reaching its branch"
    )
    assert "further from flat than the target" in variants["spec_fail"]
    assert "could not read enough of the sound" in variants["spec_unmeasurable"]
    assert "unproven" in variants["scope_incomplete"]
    for name, text in variants.items():
        assert text.startswith(headline), (name, text)


def test_the_recovery_re_verify_plan_is_unchanged_by_the_split():
    """The 1-entry recovery re-arm is byte-identical to what it always was
    (work order D2: "the 1-entry form remains what it is today"), so a failed
    stage 2 still offers one cheap sweep and says so.
    """
    plan = build_v2_verify_capture_plan(FC_HZ)
    assert plan.capture_target == 1
    assert plan.max_attempts == CAPTURE_PLAN_MAX_ATTEMPTS
    (entry,) = plan.entries
    assert entry.kind_label == "verify"
    assert entry.screen["title"] == REVERIFY_NO_REWALK_HEADLINE
    assert entry.screen["body"] == (
        "Put the microphone back on the mark and hold it still."
    )
    assert entry.screen["auto_advance"] == AUTO_ADVANCE_TAP
    # It is a recovery, not the end of a journey: no done copy, no confirm tap.
    assert "done_title" not in entry.screen
    assert "confirm_title" not in entry.screen


def test_every_entry_carries_the_one_server_derived_counter():
    """§2.1: "Measurement N of T" is the ONLY counter, it is server-derived,
    and it counts the whole session — the per-group "Spot i of n" vocabulary
    is retired (it disagreed with the phone's own count on screen)."""
    for tier in (TIER_FULL, TIER_EXPRESS):
        plan = build_v2_capture_plan(_roles(), FC_HZ, tier=tier)
        target = plan.capture_target
        assert [entry.screen["progress"] for entry in plan.entries] == [
            f"Measurement {i} of {target}" for i in range(1, target + 1)
        ]
        for entry in plan.entries:
            assert "Spot " not in entry.screen.get("title", "")
            assert "hold still" not in entry.screen.get("title", "")


def test_the_verify_anchor_keeps_its_confirm_tap_on_stage_2s_own_begin():
    """§2.2's confirm-then-tone tap, RE-ANCHORED (work order D10).

    §2.2 established begin-first-then-confirm and is SHIPPED; what the split
    supersedes is only its ordering premise — that the confirm follows an
    in-session apply. There is no in-session apply any more, so the tap moves
    with the anchor to stage 2's own begin, keeping the same two strings the
    page renders and gates the arm on.

    §2.2's fallback-safety rule is re-derived rather than dropped.
    ``validate_capture_page`` still admits a phone carrying a cached
    pre-redesign bundle, which ignores ``confirm_title``/``confirm_body`` and
    renders ``title``/``body`` instead. Those two used to have to stay the
    apply-hold copy because that page would show them AS the hold heading;
    stage 2 has no hold, so they become the plain pre-arm instruction — which
    is exactly what that page needs them to be, and is true for it.
    """
    verify = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(),
    ).entries[0]
    assert verify.kind_label == "verify"
    assert verify.screen["confirm_title"] == "Back on the mark, holding still?"
    assert verify.screen["confirm_body"] == (
        "Same spot, same height, pointed at the speaker."
    )
    # No apply to arm on, so no on_apply policy anywhere in either stage.
    assert verify.screen["auto_advance"] == AUTO_ADVANCE_TAP
    assert all(
        e.screen.get("auto_advance") != AUTO_ADVANCE_ON_APPLY
        for e in build_v2_capture_plan(_roles(), FC_HZ).entries
    )
    # An older cached page reads title/body — and reads something TRUE.
    assert "mark" in verify.screen["title"]
    assert verify.screen["body"]
    assert verify.screen["title"] != "Applying"
    assert verify.screen["body"] != VERIFY_ANCHOR_HOLD_MESSAGE
    # …and the hold copy itself is retained, not deleted (D10): the deferral
    # that carries it is unreachable in a shipped session but still the honest
    # answer for any conductor built without a prior apply.
    assert VERIFY_ANCHOR_HOLD_MESSAGE


def test_a_voluntary_retake_replaces_the_take_and_never_loses_the_original():
    """§2.6's fail-safe, at the conductor's own surface.

    An ACCEPTED retake of an already-accepted position replaces the retained
    take (retention is per-index idempotent); a REJECTED one never reaches
    retention at all, so the original take stands. Either way the group stays
    accepted and the position count never changes.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    assert PHASE_CLOUD_MEASURE in c.accepted_phases
    retaken = CLOUD_MEASURE_INDEXES[1]
    before = {t["index"]: t["attempt"] for t in c.group_position_takes(
        PHASE_CLOUD_MEASURE
    )}

    # An accepted retake REPLACES: same position, newer attempt.
    assert _run_phase(c, retaken, attempt)["accepted"] is True
    after = {t["index"]: t["attempt"] for t in c.group_position_takes(
        PHASE_CLOUD_MEASURE
    )}
    assert set(after) == set(before)
    assert after[retaken] == attempt > before[retaken]
    attempt += 1

    # A rejected retake KEEPS the original — you can never end up with less
    # evidence than you had by choosing to redo a spot.
    fakes.verify = lambda program: replace(
        _verify_analysis(program), linearity_ok=False
    )
    assert _run_phase(c, retaken, attempt)["accepted"] is False
    kept = {t["index"]: t["attempt"] for t in c.group_position_takes(
        PHASE_CLOUD_MEASURE
    )}
    assert kept == after
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_a_retake_after_the_group_closed_never_drops_the_only_take(monkeypatch):
    """The specific way a voluntary retake could have cost evidence.

    The geometry-retry branch DROPS the take at the retaken index — that is
    what "the same index is measured again" means for a REJECTION. After a
    VOLUNTARY retake the replacement is the only copy of that position, so
    firing that branch would leave the household with fewer positions than
    before they chose to redo a spot.

    Discriminating by construction: the group closes CLEAN (0 geometry retries
    spent, so the ``retries < GEOMETRY_RETRY_POSITIONS`` bound is not what
    stops it), and only then is the verdict forced to ``locked``. Without the
    "group already recorded a verdict" guard this retake is rejected and its
    position vanishes.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    assert c._geometry_retries_used[PHASE_CLOUD_MEASURE] == 0
    assert c.group_geometry(PHASE_CLOUD_MEASURE) is not None
    positions_before = c.group_positions(PHASE_CLOUD_MEASURE)
    assert len(positions_before) == len(CLOUD_MEASURE_INDEXES)

    _lock(monkeypatch)
    late = CLOUD_MEASURE_INDEXES[-1]
    retake = _run_phase(c, late, attempt)
    assert retake["accepted"] is True
    assert "code" not in retake
    assert c.group_positions(PHASE_CLOUD_MEASURE) == positions_before
    # The re-combined verdict IS recorded honestly — the guard suppresses the
    # retry request, never the measurement.
    assert c.group_geometry(PHASE_CLOUD_MEASURE)["locked"] is True


def test_a_materially_different_reclose_refreshes_the_pipeline_but_not_the_publish(
    monkeypatch, caplog,
):
    """#1872, BLOCKER-level proof: a re-close must RECOMPUTE the honest-
    instrument pipeline (so the fit, the candidate's fingerprinted
    ``exclusion_evidence``, and the journal all describe the cloud actually
    retained) even though the durable evidence-artifact PUBLISH is a
    per-phase singleton.

    Reproduces #1872's own overlap deterministically (no sleeps — the
    overlap is the CALL ORDER): two geometry-locked rejects exhaust the
    retry budget (``GEOMETRY_RETRY_POSITIONS``), so the THIRD attempt at the
    same index ACCEPTS despite geometry still reading locked — matching the
    issue's own log shape (``geometry_retries=2``, "result accepted"). A
    FOURTH attempt at that same index — standing in for the late-arriving
    retake/tail capture the confirm-hold's widened admission window lets
    through (session.py's ``completion_pending`` branch), the same shape
    every VOLUNTARY retake of the final position takes (§2.6) — carries
    MATERIALLY DIFFERENT capture data, not the same fixture twice: a
    ``validity_floor_hz`` the first close's positions did not have. A test
    that repeats an IDENTICAL fixture cannot distinguish "recomputed" from
    "served a stale cached copy" (both closes would report the SAME
    flatness/floor either way) — this one can, because a stale copy would
    keep reporting the FIRST close's floor.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    fakes.verify = _comb_cloud_analysis_factory()
    published: list[tuple[str, dict]] = []
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            publish_cloud=lambda phase, result: published.append(
                (phase, dict(result))
            ),
        ),
        driver_spacing_m=0.15,
        index_phase_map=CLOUD_MAP,
        post_apply_verifies=True,
    )
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    for _ in range(GEOMETRY_RETRY_POSITIONS):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        assert verdict["accepted"] is False
        assert verdict["code"] == REASON_CLOUD_GEOMETRY_LOCKED

    # Third attempt: the retry budget is spent, so this ACCEPTS despite
    # geometry still reading locked — the group's FIRST real close. Every
    # position (including this one) came from the comb factory, whose
    # fixture hardcodes ``validity_floor_hz=140.0``.
    first_close = _run_phase(c, last, attempt)
    attempt += 1
    assert first_close["accepted"] is True
    assert first_close["group_complete"] == PHASE_CLOUD_MEASURE
    assert len(published) == 1
    assert published[0][0] == PHASE_CLOUD_MEASURE
    assert (
        caplog.text.count("event=correction.crossover_v2_cloud_group_complete")
        == 1
    )
    assert caplog.text.count("event=correction.crossover_v2_cloud_spec") == 1
    first_pipeline = c.group_cloud_result(PHASE_CLOUD_MEASURE)
    assert first_pipeline is not None
    assert first_pipeline["validity_floor_hz"] == pytest.approx(140.0)

    # Fourth attempt at the SAME index: the overlap, carrying a GATED
    # response (validity_floor_hz=400.0) the rest of the group's positions
    # do not have — ``cloud_validity_floor_hz`` reports the WORST (highest)
    # floor across all retained positions, so this shift is only visible if
    # the retake's position genuinely replaced the prior one and the group
    # was genuinely re-combined and re-assembled.
    caplog.clear()

    def _gated_retake(program: Any) -> ProgramAnalysis:
        response = replace(_comb_summed_response(9999), validity_floor_hz=400.0)
        return ProgramAnalysis(
            phase="verify",
            program_id=program.program_id,
            locations=(_loc("sweep_verify", "summed_sweep", confidence=0.9),),
            summed_response=response,
            summed_ripple_db=1.1,
            verify_tracking={
                "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            },
            linearity_ok=True,
        )

    fakes.verify = _gated_retake
    second_close = _run_phase(c, last, attempt)
    assert second_close["accepted"] is True
    assert "code" not in second_close
    assert c.group_geometry(PHASE_CLOUD_MEASURE)["locked"] is True
    assert len(c.group_positions(PHASE_CLOUD_MEASURE)) == len(CLOUD_MEASURE_INDEXES)

    # The JOURNAL carries a spec verdict for the cloud actually used — a
    # SECOND ``cloud_group_complete`` and ``cloud_spec``, not a missing or
    # stale one. This is the "normal cloud_spec/cloud_group_complete flow"
    # shape: a re-close is a real close, logged like one.
    assert (
        caplog.text.count("event=correction.crossover_v2_cloud_group_complete")
        == 1
    )
    assert caplog.text.count("event=correction.crossover_v2_cloud_spec") == 1

    # The RECOMPUTE happened: the group's pipeline result now reports the
    # RETAKEN position's floor, not the stale first-close one.
    second_pipeline = c.group_cloud_result(PHASE_CLOUD_MEASURE)
    assert second_pipeline is not None
    assert second_pipeline["validity_floor_hz"] == pytest.approx(400.0)
    assert second_pipeline["validity_floor_hz"] != first_pipeline["validity_floor_hz"]

    # ...but the durable EVIDENCE ARTIFACT write is still a per-phase
    # singleton — the write-once store refuses a write whose bytes differ
    # from what is already there (this retake's recomputed bytes normally
    # do), so the guard skips the attempt outright rather than spend it on
    # a call that would be refused. The skip itself is journalled (the one
    # fact nothing else states — the artifact now lags the fresh pipeline
    # result above).
    assert len(published) == 1, "a second close must not attempt a second publish"
    assert (
        caplog.text.count("event=correction.crossover_v2_cloud_publish_skipped")
        == 1
    )

    # End-to-end: the FIT itself, and the candidate it produces, must also
    # see the retaken cloud — not just the pipeline's own bookkeeping.
    confirmed = _confirm_cloud(c)
    assert confirmed.get("candidate_fingerprint")
    assert c.candidate is not None
    evidence = c.candidate.exclusion_evidence
    assert evidence["validity_floor_hz"] == pytest.approx(400.0)
    assert evidence["validity_floor_hz"] == second_pipeline["validity_floor_hz"]


def test_a_failed_publish_is_retried_on_the_next_close_not_locked_out():
    """#1872 resilience, pinned: ``_group_cloud_published`` marks a phase
    only on a SUCCESSFUL publish, not a bare attempt — stated in the
    ``__init__`` field comment and the publish guard's own comment, but
    asserted nowhere until this test. Marking on the
    attempt instead (so a FAILED publish also marks) would leave every
    other conductor test green, because none of them drives a publish
    failure followed by a second close.

    A transient failure — a full disk, not a write-once conflict — must not
    permanently lock the phase out of ever publishing for the rest of the
    session: the group's next close (another voluntary retake of the final
    position) has to retry.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    last = CLOUD_MEASURE_INDEXES[-1]

    calls = {"n": 0}

    def _flaky_publish(phase, result):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("synthetic full disk")

    c._seams = replace(c._seams, publish_cloud=_flaky_publish)

    # First close's publish attempt fails — fail-soft (the capture is still
    # accepted), and NOT marked published.
    first_close = _run_phase(c, last, attempt)
    attempt += 1
    assert first_close["accepted"] is True
    assert calls["n"] == 1
    assert PHASE_CLOUD_MEASURE not in c._group_cloud_published

    # A second close (another voluntary retake) retries the publish — and
    # this time it succeeds, so it IS marked.
    second_close = _run_phase(c, last, attempt)
    assert second_close["accepted"] is True
    assert calls["n"] == 2, "a failed first attempt must not lock out the retry"
    assert PHASE_CLOUD_MEASURE in c._group_cloud_published


def test_the_tier_rides_the_snapshot_and_the_pipeline_payload():
    """§1.2: every consumer can tell which instrument produced a result, and an
    UNDECLARED tier reads as unknown rather than as "full" (the
    ``echo_band_provenance`` discipline, issue #1763)."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes, tier=TIER_EXPRESS)
    assert c.tier == TIER_EXPRESS
    assert c.snapshot().tier == TIER_EXPRESS
    assert c.snapshot().to_dict()["tier"] == TIER_EXPRESS
    _walk_measure_cloud_to_close(c)
    assert c.group_cloud_result(PHASE_CLOUD_MEASURE)["tier"] == TIER_EXPRESS

    undeclared = _cloud_conductor(FakeSeams())
    assert undeclared.tier == ""
    assert undeclared.snapshot().tier == ""
    with pytest.raises(CrossoverV2FlowError):
        _cloud_conductor(FakeSeams(), tier="turbo")


def test_the_measure_sweep_fit_rides_the_snapshot():
    """#2923: a duration-fitted MEASURE program's realized length is banked on
    the snapshot, not held only in the live conductor's memory — the durable
    half of #2921's fit, so an offline reader can replay it later.

    A woofer limit below the nominal 4.0 s default forces #2921's fit
    deterministically (the nominal always realizes AT OR ABOVE its own
    request — see ``phase_closing_duration_s``), independent of which band a
    fixture's roles happen to declare.
    """
    import json

    from jasper.active_speaker.crossover_v2 import priors as _priors_mod

    fakes = FakeSeams()
    c = _conductor(fakes, driver_sweep_duration_limits_s={"woofer": 3.5})
    _run_phase(c, 1, 1)  # CHECK solve -> MEASURE composed at the fitted length

    expected = _priors_mod.measure_sweep_durations_s(
        c.program_for_phase(PHASE_MEASURE)
    )
    assert expected is not None
    # The fit actually bit: realized at or below the limit, not the nominal.
    assert expected["woofer"] <= 3.5

    snap = c.snapshot()
    assert snap.measure_sweep_durations_s == pytest.approx(expected)
    assert snap.to_dict()["measure_sweep_durations_s"] == pytest.approx(expected)

    # Round-trips through the exact JSON encoding ``save_v2_state`` uses, so
    # no float precision is lost across the real persistence path — the same
    # encoding ``jasper-read-distortion --state`` later reads back.
    roundtripped = json.loads(json.dumps(snap.to_dict()))["measure_sweep_durations_s"]
    assert roundtripped == pytest.approx(expected)

    # Before MEASURE is composed (no CHECK accept yet), the field is honestly
    # absent rather than a guessed nominal — mirrors ``gain_plan_db`` beside it.
    undeclared = _conductor(FakeSeams())
    assert undeclared.snapshot().measure_sweep_durations_s is None


def test_the_measure_sweep_fit_survives_conductor_to_rebuild_end_to_end():
    """#2923 gate fix round, nit 2: nothing previously joined this seam
    end to end.

    ``priors.measure_sweep_durations_s`` keys its returned dict by
    ``str(segment.role)`` — whatever the composed program's own roles are
    called. ``harmonic_evidence._banked_sweep_durations_s`` reads it back
    through a hardcoded ``("woofer", "tweeter")``. In this session's own
    2-way convention the two always agree, but nothing walked the WHOLE
    chain — conductor compose -> ``.snapshot()`` -> a durable-state-shaped
    dict -> the offline rebuild — to prove it; a future key-shape change on
    either half should fail here, not on a campaign.

    Caps are widened past the fixture default so the solved gain plan
    clears both ceilings with margin (``back_off_gain`` is then the
    identity for both roles, byte for byte) — the ordinary, non-clipped
    case this reproduction path is meant to serve. This is deliberately
    narrower than a full production-shaped ``candidate`` block:
    ``rebuild_measure_program`` reads only ``candidate.program_id``, so
    that is the only key supplied for it.
    """
    import json

    from jasper.active_speaker.crossover_v2 import harmonic_evidence as he

    fakes = FakeSeams()
    # Constructed directly rather than through ``_conductor()``: that helper
    # hardcodes ``driver_caps_dbfs=CAPS``, which collides with overriding it
    # here. Skipping ``_conductor()``'s entry-baseline stash is safe: that
    # stash is for stage-1 cloud grading this test never reaches, and CHECK's
    # own accept ladder (``check_screens``) does not read it.
    c = CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs={"woofer": 0.0, "tweeter": 0.0},
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
        driver_sweep_duration_limits_s={"woofer": 3.5},
    )
    _run_phase(c, 1, 1)  # CHECK solve -> MEASURE composed, woofer sweep fitted

    program = c.program_for_phase(PHASE_MEASURE)
    durable = json.loads(json.dumps(c.snapshot().to_dict()))
    state = {
        "gain_plan_db": durable["gain_plan_db"],
        "measure_sweep_durations_s": durable["measure_sweep_durations_s"],
        "candidate": {"program_id": program.program_id},
    }
    bands = {"woofer": (150.0, 6000.0), "tweeter": (300.0, 20000.0)}

    rebuilt, _downstream_db, _prelude = he.rebuild_measure_program(state, bands)

    assert rebuilt.program_id == program.program_id


def test_the_reverify_plan_leads_with_the_no_re_walk_sentence():
    """§2.4: the 2026-07-27 session ABANDONED this recovery because no screen
    said it is one sweep rather than another walk. Both of its surfaces — the
    consent steps and the entry instruction — now lead with the same
    sentence, from one constant so they cannot drift."""
    plan = build_v2_verify_capture_plan(FC_HZ)
    assert plan.capture_target == 1
    assert plan.entries[0].screen["title"] == REVERIFY_NO_REWALK_HEADLINE
    assert "do NOT need to redo the walk" in REVERIFY_NO_REWALK_HEADLINE

    spec = build_v2_verify_session_spec(FC_HZ, acknowledgement_binding="b" * 24)
    steps = next(c for c in spec.screen if c["type"] == "steps")["items"]
    assert steps[0] == REVERIFY_NO_REWALK_HEADLINE


def test_the_summed_consent_heading_names_the_job_not_crossover_crossover():
    """§2.3: the v2 cloud passed ``driver_label="crossover"`` into a heading
    template built for per-driver captures, so the household read
    "Crossover — crossover". A summed capture measures the speaker, not a
    named driver."""
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24,
    )
    heading = next(c for c in spec.screen if c["type"] == "heading")
    assert heading["text"] == "Tune your speaker"


def test_the_consent_tier_line_derives_its_counts_and_duration():
    """§1.4/§1.1: the consent screen names WHICH instrument, with numbers
    derived from the plan — never hand-written. The duration is the phone's
    OWN estimate (``CapturePlan.estimated_minutes``), so the consent screen and
    the wake-lock hint cannot quote different sessions."""
    # RE-DERIVED for the two-stage split. The consent screen belongs to ONE
    # session, so its counts are STAGE 1's — 10 at Full, 6 at Express — and
    # they are still derived from the plan the phone is about to walk, never
    # hand-written. PR-T4 finished the reconciliation the split opened: the line
    # now SAYS "in this session", so it and the pre-session tier chooser (which
    # correctly quotes the whole journey, 16 and 7) can no longer be read as
    # contradicting each other.
    for tier, label, target in (
        (TIER_FULL, "Full measurement", 10),
        (TIER_EXPRESS, "Quick tune", 6),
    ):
        spec = build_v2_session_spec(
            _roles(), FC_HZ, acknowledgement_binding="b" * 24, tier=tier,
        )
        minutes = spec.capture_plan.estimated_minutes()
        steps = next(c for c in spec.screen if c["type"] == "steps")["items"]
        assert steps[0] == (
            f"{label}, this session: {target} measurements, "
            f"about {minutes} minutes"
        )
        # The stage qualifier sits IN FRONT of the numbers so the capture
        # page's own de-dup needle ("{n} measurements, about {m} minutes")
        # still finds it — otherwise the household reads the same numbers
        # twice, two lines apart. Pinned here as well as in the page's own
        # suite because this is the side that can move it.
        assert f"{target} measurements, about {minutes} minutes" in steps[0]
    # These two calls take no include_* arguments, so they exercise the
    # BUILDER's bare defaults (pre-apply cloud ON, lateral and entry baseline
    # OFF) — 7 minutes at Full, 5 at Express. That is NOT the shipped stage 1,
    # which runs the opposite flags for 3 captures and 3 minutes at either
    # tier; the whole journey is what tier_display_info sums, pinned in its own
    # test below.
    assert build_v2_capture_plan(_roles(), FC_HZ).estimated_minutes() == 7
    assert (
        build_v2_capture_plan(_roles(), FC_HZ, tier=TIER_EXPRESS).estimated_minutes()
        == 4
    )


def test_tier_display_info_minutes_hold_across_plausible_topologies():
    """S3 fix (adversarial review of PR #1780): ``tier_display_info``'s fixed
    representative ``RoleBand`` pair does NOT make the realized sweep
    duration invariant to the band (an earlier docstring overclaimed that —
    MESM gaps and Novak sample-count rounding both depend on the swept
    band's edges). What actually holds is narrower: the displayed WHOLE
    MINUTES stay the same across the plausible 2-way band space, because
    ``CapturePlan.estimated_minutes``'s ceil-to-minute quantum absorbs the
    real (small) variance. Swept here across several genuinely different
    plausible topologies — varying woofer/tweeter bands and ``fc_hz`` — each
    built through the REAL ``build_v2_capture_plan``, never re-deriving the
    arithmetic."""
    info = tier_display_info()
    topologies = [
        # (woofer band, tweeter band, fc_hz)
        (FrequencyBand(150.0, 6000.0), FrequencyBand(1800.0, 20000.0), 1600.0),
        (FrequencyBand(80.0, 3000.0), FrequencyBand(1200.0, 20000.0), 1800.0),
        (FrequencyBand(200.0, 4500.0), FrequencyBand(1500.0, 22000.0), 2200.0),
    ]
    for woofer_band, tweeter_band, fc_hz in topologies:
        roles = [
            RoleBand("woofer", 0, woofer_band),
            RoleBand("tweeter", 1, tweeter_band),
        ]
        for tier in (TIER_FULL, TIER_EXPRESS):
            shape = resolve_plan_shape(tier)
            # BOTH stages, because the chooser quotes the whole journey (D2).
            stage1 = build_v2_capture_plan(
                roles, fc_hz, plan_shape=shape,
                include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
                include_lateral=False,
                include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
            )
            stage2 = build_v2_verify_capture_plan(fc_hz, plan_shape=shape)
            minutes = stage1.estimated_minutes() + stage2.estimated_minutes()
            assert minutes == info[tier]["estimated_minutes"], (
                f"tier={tier} woofer={woofer_band} tweeter={tweeter_band} "
                f"fc={fc_hz}: displayed minutes drifted from tier_display_info()"
            )
            assert (
                stage1.capture_target + stage2.capture_target
                == info[tier]["capture_target"]
            )


def test_the_orientation_states_the_walks_shape_instead_of_enumerating_it():
    """#1941 R1, keeping work order D7's intent (#1804 + #1805).

    D7 put every position on the consent screen so the walk would not be
    discovered one prompt at a time. The intent survives; the presentation does
    not. A SECOND ten-item ``ui_steps`` list, stacked under the first, was the
    owner's 2026-07-30 field defect — *"crazy dense with the 10 steps all
    spelled out"* — and a household standing at the first position cannot act
    on the last one anyway.

    What replaces it is one ``note`` carrying the two facts the list was
    actually being used to convey: how far from the mark this reaches, and that
    each position is prompted. The distance is DERIVED from the same
    ``[:N - 1]`` slice of the same table the per-entry screens are built from,
    which is why a plan-shape change still moves both together or neither.
    """
    for tier, positions in (
        (TIER_FULL, DEFAULT_CLOUD_MEASURE_POSITIONS),
        (TIER_EXPRESS, express_cloud_measure_positions()),
    ):
        spec = build_v2_session_spec(
            _roles(), FC_HZ, acknowledgement_binding="b" * 24, tier=tier,
        )
        step_lists = [c["items"] for c in spec.screen if c["type"] == "steps"]
        assert len(step_lists) == 1, "ONE list — the stacked preview is gone"
        # The acceptance bar #1941 sets for the pre-tone screen: at most six
        # list items, and one orientation note.
        assert len(step_lists[0]) <= 6

        walked = CLOUD_POSITION_PROMPTS[: positions - 1]
        shape = cloud_walk_shape(walked)
        notes = [c["text"] for c in spec.screen if c["type"] == "note"]
        assert shape in notes

        # The reach is DERIVED from the walked slice, in the prompts' own units
        # — not a hand-written number that could outlive the table.
        reach = cloud_walk_reach_cm(positions)
        assert format_position_distance(reach) in shape
        # …and it is a true CEILING, not the stated maximum restated. The wide
        # rows also ask the operator to step IN toward the speaker so the
        # radius holds, which puts the capsule on a chord: a stated 40 cm
        # lateral move really lands ~40.9 cm from the mark at the placement
        # copy's nominal 1 m. Re-derived here, because the first version of
        # this screen quoted the bare offset and was therefore false on the
        # very walk it described.
        nominal_mark_distance_cm = 100.0
        worst_chord = max(
            math.hypot(
                p.offset_cm,
                nominal_mark_distance_cm
                - math.sqrt(
                    max(nominal_mark_distance_cm**2 - p.offset_cm**2, 0.0)
                ),
            )
            for p in walked
        )
        assert worst_chord <= reach, (
            f"the quoted reach {reach} cm no longer covers the walk's own "
            f"step-in chord ({worst_chord:.2f} cm) — widen "
            "CLOUD_WALK_REACH_ROUNDING_CM rather than shipping a false ceiling"
        )

        # …and the claim is bounded against EVERY prompt the flow can show,
        # not just the walked slice. CLOUD_GEOMETRY_RETRY_PROMPTS is a shipped
        # path (GEOMETRY_RETRY_POSITIONS = 2) and is deliberately "past every
        # position in the table", so a bare "every spot is within X" would be
        # false the moment a capture is retaken. Whether the honesty clause is
        # needed is DERIVED from that reach, so a narrowed retake drops it.
        retry_reach = cloud_geometry_retry_reach_cm()
        if retry_reach > reach:
            assert "a redo can ask for one step further out" in shape
        else:
            assert "redo" not in shape
        # Today's constants really do exercise the first branch.
        assert retry_reach > reach

        # …and no position is enumerated on the consent screen any more.
        for prompt in walked:
            assert prompt.text not in shape
            assert prompt.text not in step_lists[0]
        # The household is told they will be prompted, and the tail sets up the
        # INTERLUDE rather than promising a tune.
        assert "you will be told each one" in shape
        assert shape.endswith(CLOUD_WALK_SHAPE_TAIL)
        assert "decide" in CLOUD_WALK_SHAPE_TAIL

        # …and the plan really does prompt exactly those, in that order.
        prompted = [
            e.screen["title"] for e in spec.capture_plan.entries
            if e.kind_label == "cloud_measure"
        ]
        assert prompted == [p.headline for p in walked]


def test_the_post_apply_walk_states_its_shape_with_its_own_tail():
    """Stage 2's walk gets the same one-line shape as stage 1's, with its own
    tail: the journey ends there rather than pausing for a decision. Express's
    1-entry stage 2 is not a walk and gets no shape line at all.

    RE-DERIVED for the 2026-08-24 geometry ruling: the post-apply group walks
    its OWN pose set now, so the sentence is quoted off that table rather than
    off a prefix of the pre-apply one.
    """
    full = build_v2_verify_session_spec(
        FC_HZ, acknowledgement_binding="b" * 24, plan_shape=resolve_plan_shape(),
    )
    shape = cloud_walk_shape(flow.CLOUD_VERIFY_POSE_PROMPTS, post_apply=True)
    assert len([c for c in full.screen if c["type"] == "steps"]) == 1
    assert shape in [c["text"] for c in full.screen if c["type"] == "note"]
    # Same derived ceiling and the same retake honesty as stage 1 — the
    # geometry-locked retake is armed on this group too.
    reach = flow.cloud_walk_reach_cm_of(flow.CLOUD_VERIFY_POSE_PROMPTS)
    assert format_position_distance(reach) in shape
    assert cloud_geometry_retry_reach_cm() > reach
    assert "a redo can ask for one step further out" in shape
    assert shape.endswith(CLOUD_WALK_SHAPE_TAIL_POST_APPLY)
    # Stage 2 grades rather than handing back a decision.
    assert CLOUD_WALK_SHAPE_TAIL_POST_APPLY != CLOUD_WALK_SHAPE_TAIL

    express = build_v2_verify_session_spec(
        FC_HZ,
        acknowledgement_binding="b" * 24,
        plan_shape=resolve_plan_shape(TIER_EXPRESS),
    )
    assert len([c for c in express.screen if c["type"] == "steps"]) == 1
    assert cloud_walk_shape(()) == ""
    assert cloud_walk_shape((), post_apply=True) == ""
    # …and an empty shape renders NO note rather than an empty one, so the
    # one-sweep screen never grows a blank section.
    assert all(
        c["text"] for c in express.screen if c["type"] == "note"
    ), "an empty shape must render no note at all"


def test_check_stops_hushing_the_room_before_it_measures_it():
    """Work order D8 / issue #1835. CHECK's ambient window is the SESSION's
    room-noise measurement and is deliberately composed to run BEFORE anyone is
    asked to go quiet — the gain solve reads it, so a pre-hushed room reads
    quieter than reality and the solve under-drives against the noise the later
    sweeps actually face.

    TWO windows are touched and a THIRD is deliberately not: CHECK's step copy
    and the phone's own pre-arm floor note both stop asking for quiet on CHECK
    only. The in-sweep ambient lines — a different measurement with a different
    purpose — are the speaker's own call (``quiet_requested``) and this must not
    collapse them into one string.
    """
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24,
    )
    entries = {e.kind_label: e for e in spec.capture_plan.entries}
    check = entries["check"].screen
    assert "stay quiet" not in check["body"].lower()
    assert "carry on" in check["body"].lower()
    # …and the phone's own sub-second floor window gets its own honest request,
    # because asking for quiet THERE hushes the room a moment before CHECK
    # measures it.
    assert "quiet" not in check["noise_note"].lower()
    assert "carry on" in check["noise_note"].lower()
    # Every OTHER entry supplies no override, so the page keeps its default —
    # which is right for them, since a sweep follows immediately.
    for label, entry in entries.items():
        if label != "check":
            assert "noise_note" not in entry.screen


def test_cloud_prompts_front_load_the_wide_offsets():
    """Fundamental 1's physics, pinned: >=10 cm spread decorrelates HF nulls and
    ~30 cm+ offsets are what support the LF edge. Each group walks its own
    ordered table from the front, so the shortest walk either can be
    CONFIGURED to run — its declared MIN, not its default — must still contain
    at least two wide moves. Reordering a table for readability would
    silently delete the LF half of the measurement — hence this test rather
    than a comment.

    RE-DERIVED 2026-08-24: the two groups walked ONE table until the geometry
    ruling gave the post-apply group its own pose set, so the floors are now
    derived per table rather than one standing in for both. The guarantee is
    unchanged; what moved is which table each floor is checked against.

    Round-2 review NEW-9: this used to compare against
    ``DEFAULT_CLOUD_VERIFY_POSITIONS``, so ``M = 2`` was accepted and voided
    the guarantee the test claims. Both groups now carry a floor, and both
    floors are checked against the SAME derivation the code enforces.

    Flow-simplification §1.2 adds a THIRD number to the same derivation: the
    express tier's pre-apply group size. Express exists precisely because a
    4-position walk still picks up both wide moves for free, so a reorder that
    pushed the second wide move later must move express with it rather than
    ship a silently one-wide "quick tune".
    """
    walked = CLOUD_POSITION_PROMPTS[: MIN_CLOUD_MEASURE_POSITIONS - 1]
    assert sum(1 for prompt in walked if prompt.wide) >= 2
    # …and the same property on the POST-apply group, which walks its own pose
    # set since the 2026-08-24 geometry ruling rather than a prefix of the one
    # above. Two tables, so two derivations — a single ``min()`` over the two
    # floors would now be checking one table against the other's number.
    post_walked = flow.CLOUD_VERIFY_POSE_PROMPTS[: MIN_CLOUD_VERIFY_POSITIONS - 1]
    assert sum(1 for prompt in post_walked if prompt.wide) >= 2
    # The floors are DERIVED from the table each group walks, so a reorder moves
    # them rather than leaving a stale literal behind.
    derived = _min_positions_for_two_wide_offsets()
    assert MIN_CLOUD_VERIFY_POSITIONS == _min_positions_for_two_wide_offsets(
        flow.CLOUD_VERIFY_POSE_PROMPTS
    )
    assert MIN_CLOUD_MEASURE_POSITIONS >= derived
    assert express_cloud_measure_positions() == derived
    # …and the express plan really does walk two wide moves at that size.
    express = resolve_plan_shape(TIER_EXPRESS)
    express_walk = CLOUD_POSITION_PROMPTS[: express.cloud_measure_positions - 1]
    assert sum(1 for prompt in express_walk if prompt.wide) == 2
    assert len(express_walk) == 4


@pytest.mark.parametrize("positions", [MIN_CLOUD_VERIFY_POSITIONS - 1, 0])
def test_a_verify_group_too_short_for_two_wide_offsets_is_refused(positions):
    """The hole NEW-9 named: nothing stopped a caller asking for a post-apply
    group that never reaches a ~30 cm-class offset."""
    with pytest.raises(CrossoverV2FlowError):
        build_v2_capture_plan(_roles(), FC_HZ, cloud_verify_positions=positions)


def test_cloud_prompts_state_numeric_absolute_poses():
    """Every prompt is real household copy, states its distance NUMERICALLY in
    both units, and states a COMPLETE pose measured from the mark.

    RE-DERIVED, not merely relaxed. The pin this replaces asserted the opposite
    (`" cm" not in prompt.text`) under a comment citing "the S0 owner ruling:
    hand-widths and forearms, never centimetres" — the 2026-07-25 studio
    ruling. Two later owner rulings superseded it, and the assertion is now
    what THEY require rather than what the old one banned:

    * 2026-07-28 field session, issue #1805 — "drop body-part units — prompts
      should use inches and/or meters". So numeric units must be PRESENT and
      body-part units ABSENT; deleting the old assertion would have left the
      new rule unpinned, and leaving it would have made the suite assert a rule
      the owner has withdrawn.
    * 2026-07-29 field session, issue #1806 — poses must be absolute, never a
      delta on ambiguous prior state, and the actor is "the microphone" rather
      than the phone (a household may measure with a laptop or a USB mic).
    """
    for prompt in CLOUD_POSITION_PROMPTS:
        assert prompt.headline.strip()
        text = prompt.text
        lowered = text.lower()
        # #1805: numbers, in both units, on every prompted move.
        assert " in (" in text and " cm)" in text, text
        assert re.search(r"\d+ in \(\d+ cm\)", text), text
        # …and no body-part unit anywhere in the copy.
        for banned in ("hand-width", "hand width", "forearm", "arm's length"):
            assert banned not in lowered, text
        # #1806: an absolute pose names the mark it is measured from, and the
        # microphone rather than the phone.
        assert "mark" in lowered, text
        assert "microphone" in lowered, text
        assert "phone" not in lowered.replace("microphone", ""), text
        # …and carries a role the attribution stage can read.
        assert prompt.role in POSITION_ROLES


def test_geometry_retry_prompts_carry_the_same_register():
    """The RETAKE rungs are the other prompt constant carrying the register —
    the work order names both, because a table converted alone would leave the
    household reading inches all session and then "two forearms' length" at the
    one moment the instruction has to be unambiguous."""
    for rung in CLOUD_GEOMETRY_RETRY_PROMPTS:
        lowered = rung.lower()
        assert re.search(r"\d+ in \(\d+ cm\)", rung), rung
        assert "forearm" not in lowered and "hand-width" not in lowered, rung
        assert "microphone" in lowered, rung
        assert "mark" in lowered, rung
    # A rung must ask for a spread the walk itself never reaches, or "wider
    # spot" is a request the household has already satisfied.
    assert GEOMETRY_RETRY_OFFSET_CM > max(
        p.offset_cm for p in CLOUD_POSITION_PROMPTS[:MIN_CLOUD_MEASURE_POSITIONS - 1]
    )


def test_wide_is_derived_from_the_offset_not_hand_set():
    """The wide-offset guarantee survives a copy edit because ``wide`` is
    COMPUTED from the row's distance.

    Before the distances became data, a row could say "a forearm's length" and
    carry ``wide=True`` independently — two facts that could disagree, on the
    one flag ``MIN_CLOUD_VERIFY_POSITIONS`` and ``express_cloud_measure_
    positions()`` are both derived from. Now narrowing the copy narrows the
    flag, which moves the floors, which fails
    ``test_cloud_prompts_front_load_the_wide_offsets`` loudly.
    """
    for prompt in CLOUD_POSITION_PROMPTS:
        assert prompt.wide == (prompt.offset_cm >= WIDE_OFFSET_MIN_CM)
        assert prompt.offset_cm >= MIN_CLOUD_OFFSET_CM
        # The stated distance IS the carried distance — the copy is generated
        # from the number, so these cannot drift.
        assert format_position_distance(prompt.offset_cm) in prompt.headline
    narrowed = replace(CLOUD_POSITION_PROMPTS[2], offset_cm=WIDE_OFFSET_MIN_CM - 1)
    assert narrowed.wide is False
    # …and the HF floor is ENFORCED at table-build time, not documented: a row
    # too short to decorrelate anything is a session minute spent on nothing.
    with pytest.raises(ValueError):
        _pose("Move it {d}", MIN_CLOUD_OFFSET_CM - 1, POSITION_ROLE_ONAX)
    with pytest.raises(ValueError):
        _pose("Move it {d}", 40.0, "sideways")


# --- courtesy-tone prelude (issue #1677): phone-contract duration ------------
#
# The phone's recording window (CapturePlanEntry.duration_ms) is derived from
# build_v2_capture_plan's OWN nominal composition, entirely separate from the
# real playback composition (``crossover_v2.programs``'s SessionExcitation
# methods, reached through the conductor's ``_excitation``). Both must ask the
# SAME ``courtesy_prelude_for_phase`` rule, or the phone would stop recording
# before the real (longer) program finishes -- mirrors the existing +15 s
# MEASURE-lengthening proof from sweep-composition PR-A (#1668).
#
# Since the 2026-08-18 trim the rule answers per PHASE, so this is now also
# where a phase that is announced in the plan but not in playback (or the other
# way round) is caught: each entry is checked against a nominal program composed
# at ITS OWN phase's answer.


def _courtesy_prelude_ms() -> float:
    """What one prelude costs, DERIVED from the composer's own constants."""
    from jasper.audio_measurement.program import (
        COURTESY_TONE_BEEP_COUNT,
        COURTESY_TONE_BEEP_DURATION_S,
        COURTESY_TONE_BEEP_GAP_S,
        COURTESY_TONE_TRAILING_SILENCE_S,
    )

    return 1000.0 * (
        COURTESY_TONE_BEEP_COUNT * COURTESY_TONE_BEEP_DURATION_S
        + (COURTESY_TONE_BEEP_COUNT - 1) * COURTESY_TONE_BEEP_GAP_S
        + COURTESY_TONE_TRAILING_SILENCE_S
    )


def test_capture_plan_duration_matches_courtesy_prelude_program_exactly():
    assert courtesy_prelude_for_phase(PHASE_CHECK) is True
    assert courtesy_prelude_for_phase(PHASE_MEASURE) is False
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    check, measure = plan.entries[0], plan.entries[1]
    # The VERIFY-shaped program's duration now rides STAGE 2's anchor (the
    # split moved the phase, not the arithmetic) — and the cloud entries, which
    # play its unannounced twin, are checked against that twin below.
    stage2 = build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape())
    verify = stage2.entries[0]
    assert verify.kind_label == "verify"

    from jasper.audio_measurement.program import (
        BASE_STIMULUS_PEAK_DBFS,
        build_check_program,
        build_measure_program,
        build_verify_program,
    )

    roles = _roles()
    nominal_gains = {rb.role: BASE_STIMULUS_PEAK_DBFS for rb in roles}
    nominal_check = build_check_program(
        roles, courtesy_prelude=courtesy_prelude_for_phase(PHASE_CHECK),
    )
    nominal_measure = build_measure_program(
        nominal_gains, roles,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_MEASURE),
    )
    nominal_verify = build_verify_program(
        FC_HZ,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_VERIFY),
    )
    nominal_cloud = build_verify_program(
        FC_HZ,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_VERIFY),
    )
    assert check.duration_ms == _program_duration_ms(nominal_check) + CAPTURE_ENTRY_MARGIN_MS
    assert measure.duration_ms == _program_duration_ms(nominal_measure) + CAPTURE_ENTRY_MARGIN_MS
    assert verify.duration_ms == _program_duration_ms(nominal_verify) + CAPTURE_ENTRY_MARGIN_MS
    # Every prompted position plays the summed sweep's UNANNOUNCED twin, so its
    # recording window must be that program's — a shorter one would truncate
    # the sweep and a longer one would record silence into the analysis.
    cloud_ms = _program_duration_ms(nominal_cloud) + CAPTURE_ENTRY_MARGIN_MS
    cloud_entries = [
        e for e in (*plan.entries, *stage2.entries)
        if e.kind_label.startswith("cloud_")
    ]
    assert cloud_entries
    for entry in cloud_entries:
        assert entry.duration_ms == cloud_ms, entry.kind_label
    # And the trim is real at the phone's own surface: a position's window is
    # exactly the prelude shorter than the anchor's.
    assert verify.duration_ms - cloud_ms == pytest.approx(_courtesy_prelude_ms(), abs=1)
    # The SHIPPED stage-1 plan, whose last entry is the one budget that has to
    # match a program composed for a DIFFERENT phase: the entry baseline plays
    # stage 2's anchor object, so it budgets the ANNOUNCED window even though
    # nothing about its own position asks for a warning.
    shipped = build_v2_capture_plan(
        _roles(), FC_HZ,
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    baseline = next(e for e in shipped.entries if e.kind_label == "entry_baseline")
    assert baseline.duration_ms == verify.duration_ms
    # A lateral pose replays MEASURE, so it budgets MEASURE's window.
    for entry in shipped.entries:
        if entry.kind_label == "lateral":
            assert entry.duration_ms == measure.duration_ms


def test_capture_plan_duration_is_longer_than_the_pre_1677_shape():
    """Direct proof the prelude actually lengthens the phone's recording
    budget (not just that the two composition paths agree with EACH OTHER,
    which the previous test already pins) -- the "+15 s"-style regression
    check named in the issue."""
    from jasper.audio_measurement.program import build_check_program

    expected_prelude_ms = _courtesy_prelude_ms()
    roles = _roles()
    legacy_check = build_check_program(roles)
    prelude_check = build_check_program(roles, courtesy_prelude=True)
    delta_ms = _program_duration_ms(prelude_check) - _program_duration_ms(legacy_check)
    assert delta_ms == pytest.approx(expected_prelude_ms, abs=1)

    plan = build_v2_capture_plan(roles, FC_HZ)
    check_entry = plan.entries[0]
    legacy_entry_duration_ms = _program_duration_ms(legacy_check) + CAPTURE_ENTRY_MARGIN_MS
    assert check_entry.duration_ms > legacy_entry_duration_ms
    assert check_entry.duration_ms - legacy_entry_duration_ms == pytest.approx(
        expected_prelude_ms, abs=1,
    )


def test_verify_only_capture_plan_duration_includes_courtesy_prelude():
    from jasper.audio_measurement.program import (
        BASE_STIMULUS_PEAK_DBFS,
        build_verify_program,
    )

    plan = build_v2_verify_capture_plan(FC_HZ)
    entry = plan.entries[0]
    nominal_verify = build_verify_program(
        FC_HZ,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=True,
    )
    assert entry.duration_ms == _program_duration_ms(nominal_verify) + CAPTURE_ENTRY_MARGIN_MS


def test_conductor_composed_programs_carry_the_prelude_where_the_rule_says():
    """The conductor's REAL playback composition (not the nominal planning path
    above) obeys the same ``courtesy_prelude_for_phase`` rule — including the
    clip-retry rearm, which recomposes MEASURE and must not put the beeps back.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    check_tone_ids = {
        s.segment_id for s in c.program_for_phase(PHASE_CHECK).segments if s.kind == KIND_COURTESY_TONE
    }
    assert check_tone_ids == {"courtesy_tone_ch0", "courtesy_tone_ch1"}

    measure_prog = c._compose_measure_program({"woofer": -11.0, "tweeter": -13.0})
    assert not [s for s in measure_prog.segments if s.kind == KIND_COURTESY_TONE]

    verify_tone_ids = {
        s.segment_id for s in c.program_for_phase(PHASE_VERIFY).segments if s.kind == KIND_COURTESY_TONE
    }
    assert verify_tone_ids == {"courtesy_tone_ch0"}  # VERIFY is mono
    assert verify_tone_ids == {
        s.segment_id
        for s in c.program_for_phase(PHASE_ENTRY_BASELINE).segments
        if s.kind == KIND_COURTESY_TONE
    }
    assert not [
        s for s in c.program_for_phase(PHASE_CLOUD_VERIFY).segments
        if s.kind == KIND_COURTESY_TONE
    ]


@pytest.mark.parametrize("lateral_armed", [False, True])
@pytest.mark.parametrize("tier", [TIER_FULL, TIER_EXPRESS, TIER_REMOTE])
def test_the_consent_beeps_sentence_matches_what_the_session_plays(
    tier, lateral_armed,
):
    """The consent screen's beeps sentence, checked against the PROGRAMS.

    The 2026-08-18 gate round found a hand-written "The first measurement has
    three short beeps" shipped against a stage 1 that beeps TWICE — its entry
    baseline plays stage 2's anchor object and announces too. A prior literal
    pin could not see it: a substring assertion is true of a sentence that is
    false of the session.

    So this walks the other way round. For each capture index it asks the
    SESSION what that phase plays and looks for a courtesy tone in the composed
    segments — the ground truth, what the speaker actually does — and then
    requires the rendered sentence to be the one that describes that set. A
    rule change that moves the announced set without moving the copy (or the
    reverse) fails here whichever way it drifts.

    **Both lateral states, and the ARMED one is the case that binds.** No
    stage-1 plan builds the lateral group any more, so the shipped stage 1 is
    three captures at the mark: not a guided walk, and it renders no beeps
    sentence at all — which would leave the two-announcement shape unexercised
    and this pin quietly vacuous. ``lateral_armed=True`` is driven straight
    into the builders below rather than through a flag, because that is
    exactly the shape an operator's staged angle walk produces for THIS
    session (``prepare_v2_session`` sets the same local ``True`` once a walk
    is taken) — and the sentence it renders is the one that was WRONG when the
    gate found it.
    """
    from jasper.audio_measurement.program import KIND_COURTESY_TONE
    from jasper.active_speaker.capture_geometry import (
        CLOUD_WALK_PLACEMENT_POLICY_ID,
    )

    shape = resolve_plan_shape(tier)
    stages = (
        (
            build_v2_session_spec(
                _roles(), FC_HZ, acknowledgement_binding="b" * 24,
                plan_shape=shape,
                include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
                include_lateral=lateral_armed,
                include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
            ),
            build_v2_cloud_index_phase_map(
                plan_shape=shape,
                include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
                include_lateral=lateral_armed,
                include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
            ),
        ),
        (
            build_v2_verify_session_spec(
                FC_HZ, acknowledgement_binding="b" * 24, plan_shape=shape,
            ),
            flow.build_v2_verify_index_phase_map(plan_shape=shape),
        ),
    )
    conductor = _conductor(FakeSeams(), gain_plan_db={"woofer": -30.0, "tweeter": -36.0})

    for spec, index_phase in stages:
        walk = len(index_phase)
        played = tuple(
            index for index, phase in sorted(index_phase.items())
            if any(
                seg.kind == KIND_COURTESY_TONE
                for seg in conductor.program_for_phase(phase).segments
            )
        )
        steps = next(c for c in spec.screen if c["type"] == "steps")["items"]
        sentence = next((i for i in steps if "beeps" in i), "")
        if not sentence:
            # The GUIDED consent surface was not rendered, so there is no
            # sentence to be wrong — and that is checked rather than assumed,
            # because "no sentence" must never be how a guided session passes.
            # Two shipped shapes land here: a single held-still sweep (Express's
            # stage 2, the recovery re-arm) and, since the 2026-08-18 lateral
            # pause, stage 1 itself — three captures all at the mark, so the
            # flow's ``walked`` is False and the stationary copy applies.
            #
            # NOTE the consequence, which is not this PR's to fix: stage 1
            # announces two of its three captures and its consent screen says
            # nothing about beeps, because the stationary copy never carried
            # that sentence. Re-arming the walk restores it.
            assert (
                spec.acknowledgement.id != CLOUD_WALK_PLACEMENT_POLICY_ID
            ), (tier, walk)
            continue
        assert played, (tier, walk)
        if played == tuple(range(1, walk + 1)):
            expected = "Each measurement has"
        elif played == (1,):
            expected = "The first measurement has"
        elif played == (1, walk):
            expected = "The first and last measurements each have"
        else:  # pragma: no cover - a shape the copy refuses to state
            raise AssertionError(f"unstateable announced set {played} of {walk}")
        assert sentence.startswith(expected), (tier, walk, played, sentence)
        # …and the OTHER two openers are pinned out, so a sentence that merely
        # contains the right words in the wrong quantifier cannot pass.
        for other in (
            "Each measurement has",
            "The first measurement has",
            "The first and last measurements each have",
        ):
            if other != expected:
                assert other not in sentence, (tier, other)


def test_a_consent_walk_must_say_which_captures_announce():
    """A guided walk with no announced set is REFUSED, not silently phrased.

    The fail-loud half of the pin above. ``build_crossover_sweep_spec`` is a
    public builder and a caller that declares a walk without saying what it
    announces has no truthful sentence available — rendering "The first
    measurement has…" by default is exactly how the shipped defect happened.
    """
    from jasper.active_speaker.crossover_v2.sweep_spec import (
        CaptureSpecError,
        build_crossover_sweep_spec,
    )

    def _spec(announced):
        return build_crossover_sweep_spec(
            driver_label="crossover",
            driver_role="summed",
            acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
            guided_captures=9,
            announced_captures=announced,
        )

    for announced in ((), (0, 3), (1, 99), (2,), (1, 4)):
        with pytest.raises(CaptureSpecError):
            _spec(announced)

    # …and the third stateable shape, which has no shipped producer since the
    # prelude trim but is the truthful sentence for a plan that announces
    # everything — the pre-trim rule's own shape, and what a re-enable would
    # render. Kept because refusing to describe a describable session is the
    # worse failure, and pinned here so it is exercised rather than assumed.
    steps = next(
        c for c in _spec(tuple(range(1, 10))).screen if c["type"] == "steps"
    )["items"]
    assert any(
        i.startswith("Each measurement has three short beeps") for i in steps
    )


def test_bind_program_playback_seams_is_the_play_transaction_and_confirms_strictly(
    tmp_path,
):
    """What the binding still owns after wave 6b, and what it hands off.

    The graph seams moved to ``MeasurementSessionGraph``; the SetConfig
    transport claim they carried — load and restore ride
    ``set_active_config_raw``, never ``set_config_file_path``, so the statefile
    boot anchor stays put and a crash mid-session reboots onto the staged
    anchor — moved with them and is pinned in
    ``tests/test_crossover_v2_session_graph.py``. ``confirm_graph_is_live``
    moved with the binding to ``crossover_v2.composition``; its strictness is
    still pinned here.
    """
    from jasper.active_speaker.crossover_v2 import composition
    from jasper.active_speaker.crossover_v2.composition import (
        bind_program_playback_seams,
    )
    from jasper.camilla import CamillaConfigRejected

    calls: list = []

    class _FakeCam:
        """Models the 2026-08-05 hardware probe of CamillaDSP 4.1.3.

        ``GetConfig`` returns a default-filled, value-normalized SUPERSET of
        what was submitted (extra null keys; a submitted ``0`` back as ``0.0``),
        and ``ReadConfig`` — ``normalize_config_raw`` — applies exactly the same
        transform without applying anything. Comparing submitted TEXT against
        the readback would refuse every load on this fake, which is the point.
        """

        live = "prior: graph\n"

        @staticmethod
        def _camilla_serde(text):
            parsed = yaml.safe_load(text) or {}
            filled = {"description": None, "bypassed": None, **parsed}
            return yaml.safe_dump(
                {k: (0.0 if v == 0 else v) for k, v in filled.items()}
            )

        async def get_config_file_path(self, *, best_effort):
            calls.append(("get_path", best_effort))
            return str(tmp_path / "entry.yml")

        async def set_active_config_raw(self, text, *, best_effort, duck=True):
            calls.append(("set_raw", text, best_effort))
            self.live = text
            return True

        async def get_active_config_raw(self, *, best_effort):
            calls.append(("get_raw", best_effort))
            return self._camilla_serde(self.live)

        async def normalize_config_raw(self, text, *, best_effort):
            # What a live, healthy CamillaDSP raises for a config it parsed and
            # refused — CamillaController._call already maps pycamilladsp's
            # ConfigValidationError onto this class.
            if "!!not-yaml" in text:
                raise CamillaConfigRejected("camilla rejected the config")
            return self._camilla_serde(text)

        async def set_config_file_path(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("must never repoint the persisted statefile")

    entry = tmp_path / "entry.yml"
    entry.write_text("prior: graph\n", encoding="utf-8")
    cam = _FakeCam()
    seams = bind_program_playback_seams(
        cam,
        bundle_dir=str(tmp_path),
        artifact=object(),
        config_dir=str(tmp_path),
        program=_dummy_program(),
        wav_path=str(tmp_path / "program.wav"),
        topology=object(),
        safety_profile={},
        role_targets={},
        session_volume_db=SESSION_VOLUME_DB,
    )
    # The count IS the claim, and wave 6b shrank it: the three graph seams
    # moved to ``MeasurementSessionGraph``, which installs one graph per session
    # instead of swapping one in and out per stimulus. What is left here is the
    # play transaction proper.
    assert set(seams) == {"play_wav", "readmit", "writer_lock"}

    from jasper.active_speaker.program_playback import ProgramPlaybackError

    # ``confirm_graph_is_live`` moved WITH the binding to ``composition`` —
    # the session graph calls it, and its strictness is the same three claims
    # it always made.
    #
    # Default-fill tolerance: the readback is a normalized SUPERSET of the
    # submitted text, and a load is still CONFIRMED.
    cam.live = "program: graph\n"
    asyncio.run(composition.confirm_graph_is_live(cam, "program: graph\n"))
    # A genuinely different graph is still rejected — the check is strict
    # equality of normalized fingerprints, not a subset comparison.
    cam.live = "different: graph\n"
    with pytest.raises(ProgramPlaybackError, match="load was not confirmed"):
        asyncio.run(
            composition.confirm_graph_is_live(cam, "program: graph\n")
        )
    # Comment-only differences are benign: camilla's serde drops them.
    cam.live = "program: graph\n"
    asyncio.run(composition.confirm_graph_is_live(cam, "# a note\nprogram: graph\n"))
    # A submitted config camilla itself refuses is a NAMED refusal, distinct
    # from a mismatch, so hardware triage can tell the two apart.
    with pytest.raises(ProgramPlaybackError, match="normalization failed"):
        asyncio.run(composition.confirm_graph_is_live(cam, "!!not-yaml\n"))


def test_v2_session_spec_is_a_valid_protocol_3_crossover_spec():
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24,
    )
    assert spec.kind == "crossover_sweep"
    assert spec.capture_protocol_version == 3
    assert spec.capture_plan is not None
    # Stage 1's own target; ``cloud_capture_target()`` names the whole journey.
    assert spec.capture_plan.capture_target == resolve_plan_shape().measure_capture_target
    # Round-trips through the strict boundary validation.
    from jasper.active_speaker.crossover_v2.sweep_spec import CaptureSpec

    reparsed = CaptureSpec.from_dict(spec.to_dict())
    assert reparsed.capture_plan.entries == spec.capture_plan.entries


def test_shipped_v2_plans_keep_their_own_retry_budget():
    """The v2 flow's retry budget is POLICY, not the sanity ceiling.

    Both builders once passed ``capture_protocol.MAX_CAPTURE_PLAN_ATTEMPTS``
    verbatim, which was harmless only while the two constants happened to be
    equal at 8. Pin each flow's budget to this flow's own constants, and pin
    that both stay within the sanity ceiling.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        CAPTURE_PLAN_MAX_ATTEMPTS,
        build_v2_capture_plan,
        build_v2_verify_capture_plan,
    )
    from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS

    assert CAPTURE_PLAN_MAX_ATTEMPTS <= MAX_CAPTURE_PLAN_ATTEMPTS

    cloud = build_v2_capture_plan(_roles(), FC_HZ)
    one_entry = build_v2_verify_capture_plan(FC_HZ)
    # RE-DERIVED for the two-stage split: no single session carries the whole
    # journey any more. Stage 1 is 1 + N = 10 captures with
    # 10 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE = 17 attempts;
    # ``cloud_capture_target()``/``cloud_plan_max_attempts()`` keep their
    # whole-journey meaning (16 / 23 since stage 2's pose set gained the design
    # axis on 2026-08-24), which is what jasper-doctor reads as the
    # conservative bound.
    assert cloud.capture_target == 10
    assert cloud.max_attempts == 17
    assert cloud_capture_target() == 16
    assert cloud_plan_max_attempts() == 23
    assert cloud.max_attempts < cloud_plan_max_attempts()
    assert one_entry.capture_target == 1
    assert one_entry.max_attempts == CAPTURE_PLAN_MAX_ATTEMPTS
    assert cloud.max_attempts <= MAX_CAPTURE_PLAN_ATTEMPTS


@pytest.mark.parametrize("positions", [MIN_CLOUD_MEASURE_POSITIONS - 1,
                                       MAX_CLOUD_MEASURE_POSITIONS + 1])
def test_cloud_position_count_outside_the_declared_range_is_refused(positions):
    with pytest.raises(CrossoverV2FlowError):
        build_v2_capture_plan(_roles(), FC_HZ, cloud_measure_positions=positions)


def test_session_wall_clock_ceiling_scales_with_the_plan_and_is_capped():
    """The walked-away guarantee survives a long crossover-cloud session —
    and stays a guarantee: the ceiling grows with plan length but can never
    be scaled away."""
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_WALL_CLOCK_CEILING_S,
        MAX_WALL_CLOCK_CEILING_S,
    )

    shipped = build_v2_capture_plan(_roles(), FC_HZ)
    # RE-DERIVED (work order D2): each STAGE arms its own ceiling from its own
    # plan. This call takes no include_* args, so it exercises the FUNCTION's
    # own bare defaults (cloud_measure on, lateral/entry_baseline off) --
    # NOT the shipped Full tier's own stage 1, which runs cloud measure OFF
    # and #2291's entry baseline ON (no stage-1 plan builds the lateral group)
    # for 9 captures and 2,520 s (see tuning-operator-runbook.md "The
    # capture flow" / "What v2 is" -- tier_display_info() is the derivation of
    # record for that number).
    # The bare-defaults scenario below is 10 captures ⇒ 1800 + (10-3)*120 =
    # 2640 s. What the split buys is a lower worst case per stage.
    assert session_wall_clock_ceiling_s(shipped) == 2640.0
    assert session_wall_clock_ceiling_s(
        build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape())
    ) == 2160.0
    biggest = build_v2_capture_plan(
        _roles(), FC_HZ,
        cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
        cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
    )
    # 1800 + (12 - 3) * 120 = 2880 s: the biggest CLOUD-configured stage-1 plan
    # does not reach the hard cap, so the cap is exercised on a plan long enough
    # to need it (the synthetic 100 below) rather than left unpinned. 12, down
    # from 13, because #2291's stage-1 entry brought
    # ``MAX_CLOUD_MEASURE_POSITIONS`` to 11 — see that constant's arithmetic.
    assert session_wall_clock_ceiling_s(biggest) == 2880.0
    assert MAX_WALL_CLOCK_CEILING_S == 3600.0
    assert session_wall_clock_ceiling_s(
        types.SimpleNamespace(capture_target=100)
    ) == MAX_WALL_CLOCK_CEILING_S
    # The 1-entry re-verify never widens the baseline.
    assert (
        session_wall_clock_ceiling_s(build_v2_verify_capture_plan(FC_HZ))
        == DEFAULT_WALL_CLOCK_CEILING_S
    )


def test_shipped_v2_plans_serialize_to_byte_identical_wire_payloads():
    """Every shipped plan's wire bytes are pinned; only an intended edit moves
    them."""
    import hashlib
    import json

    from jasper.active_speaker.crossover_v2_flow import (
        build_v2_capture_plan,
        build_v2_verify_capture_plan,
    )

    plans = {
        "stage1-full": build_v2_capture_plan(_roles(), FC_HZ),
        "stage1-express": build_v2_capture_plan(_roles(), FC_HZ, tier=TIER_EXPRESS),
        "stage2-full": build_v2_verify_capture_plan(
            FC_HZ, plan_shape=resolve_plan_shape(),
        ),
        "stage2-express": build_v2_verify_capture_plan(
            FC_HZ, plan_shape=resolve_plan_shape(TIER_EXPRESS),
        ),
        "1-entry": build_v2_verify_capture_plan(FC_HZ),
    }
    assert set(plans) == set(_GOLDEN_V2_PLAN_BYTES)
    for label, plan in plans.items():
        raw = json.dumps(plan.to_dict(), separators=(",", ":")).encode("utf-8")
        expected_len, expected_sha = _GOLDEN_V2_PLAN_BYTES[label]
        actual_sha = hashlib.sha256(raw).hexdigest()
        assert (len(raw), actual_sha) == (expected_len, expected_sha), (
            f"{label} v2 capture plan wire bytes changed: "
            f"len={len(raw)} sha256={actual_sha}"
        )


# --- W6.1 Finding A: cap-aware CHECK / MEASURE / VERIFY composition -------------
#
# The conductor fixture (CAPS) knew the caps, but the fake play seam never ran
# admission, so a CHECK/VERIFY program that ignored the caps slipped through the
# hardware-free suite and only surfaced on JTS3 (program_channel_peak_over_cap
# refused the CHECK program). These pins compose the real programs and run them
# through the ACTUAL admission the play seam uses.

from jasper.audio_measurement.program import (  # noqa: E402
    BASE_STIMULUS_PEAK_DBFS,
)

from tests.crossover_v2_fixtures import (
    CAPS,
    bank_into,
    CLOUD_MAP,
    CLOUD_MEASURE_INDEXES,
    CLOUD_VERIFY_INDEXES,
    FC_HZ,
    FakeSeams,
    SESSION,
    SESSION_VOLUME_DB,
    SHORT_VERIFY_CLOUD_INDEXES,
    SHORT_VERIFY_MAP,
    STAGE2_MAP,
    VERIFY_INDEX,
    _BLIND_SPAN_RESULT,
    _DIAG_LOGGER,
    _ENTRY_BASELINE_RESIDUAL_DB,
    _FIXTURE_FC_HZ,
    _FIXTURE_RAW_TRIM_DB,
    _GOLDEN_V2_PLAN_BYTES,
    _LINEARIZABLE_FREQS_HZ,
    _POST_APPLY_RESIDUAL_DB,
    _ROOM_SCALE_EXPECTED_RMS_DB,
    _SUMMED_FREQS_HZ,
    _absolute,
    _alignment,
    _attempt_floor,
    _boost_vocabulary_spy,
    _capture,
    _check_analysis,
    _check_analysis_with_solves,
    _cloud_conductor,
    _comb_cloud_analysis_factory,
    _comb_summed_response,
    _conductor,
    _configured_sections,
    _confirm_cloud,
    _count_builds,
    _driver_response_diag,
    _dummy_program,
    _eligible_measure_analysis,
    _emitted_boosts,
    _fixture_branch_db,
    _fixture_raw_predicted_sum,
    _gate_block,
    _gate_residuals,
    _healthy_crossed_over_pair,
    _in_room_summed_db,
    _loc,
    _lock,
    _measure_analysis,
    _moving_notch_cloud,
    _one_sided_conductor,
    _pilot_obs,
    _plan_spy,
    _preset,
    _probed_conductor,
    _profiled_conductor,
    _resp_with_repeats,
    _roles,
    _run_phase,
    _snr_analysis,
    _snr_pilot,
    _solve_fixture_raw_trim,
    _tracking_curve,
    _tracking_with_frame,
    _verify_analysis,
    _verify_only_conductor,
    _verify_to_apply,
    _vocabularies_seen,
    _walk,
    _walk_measure_cloud_to_accept,
    _walk_measure_cloud_to_close,
)


@pytest.mark.parametrize(
    "woofer_peak,tweeter_peak",
    # The JTS3-shaped 0/-8/-65 cap numbers across the two profile-valid combos
    # (a tweeter capped above code policy, e.g. -8, cannot be confirmed).
    [(0.0, -65.0), (-8.0, -65.0)],
)
def test_composed_programs_admit_at_shaped_caps(tmp_path, woofer_peak, tweeter_peak):
    """CHECK and MEASURE admit at the JTS3-shaped caps; VERIFY (no admission
    path — it rides the applied graph) is clamped to the most restrictive cap.

    This is the pin that was missing (the conductor knew the caps but the fake
    play seam never admitted). The readmit gate REFUSES VERIFY by design
    (test_active_speaker_program_admission.test_verify_program_not_admitted_here
    pins that — VERIFY is mono/summed with no per-driver target), so VERIFY's
    equivalent safety proof is its compose-time clamp: no segment can exceed the
    binding cap that its summed signal reaches every driver at.
    """
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionError,
        readmit_program_from_wav,
    )
    from jasper.audio_measurement.program import write_program_wav

    c, topology, profile, targets, sv = _profiled_conductor(
        woofer_peak=woofer_peak, tweeter_peak=tweeter_peak
    )

    def _admit(program):
        wav = tmp_path / "program.wav"
        write_program_wav(wav, program)
        return readmit_program_from_wav(
            program, wav, topology=topology, safety_profile=profile,
            role_targets=targets, session_volume_db=sv,
        )

    adm_check = _admit(c.program_for_phase(PHASE_CHECK))
    assert adm_check.allowed, adm_check.refusals

    _run_phase(c, 1, 1)  # CHECK solve → MEASURE composed
    adm_measure = _admit(c.program_for_phase(PHASE_MEASURE))
    assert adm_measure.allowed, adm_measure.refusals

    # VERIFY has no admission path by design; its clamp is the only guard.
    with pytest.raises(ProgramAdmissionError):
        _admit(c.program_for_phase(PHASE_VERIFY))
    binding_cap = min(woofer_peak, tweeter_peak)
    for seg in c.program_for_phase(PHASE_VERIFY).stimulus_segments():
        assert seg.effective_peak_dbfs <= binding_cap + 1e-9


def test_check_pilot_pairs_preserve_delta_and_degrade_honestly():
    """CHECK pilots keep the 10 dB behavioral delta where headroom allows, and
    degrade honestly (recorded in the program) where a driver cap compresses the
    level — the JTS3 tweeter drops ~33 dB but its pair stays 10 dB apart."""
    c, _topology, _profile, _targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    check = c.program_for_phase(PHASE_CHECK)

    # Woofer: cap (-8) leaves headroom, so the pair rides the reference base and
    # keeps the full 10 dB delta.
    w_hi = check.segment("pilot_woofer_hi")
    w_lo = check.segment("pilot_woofer_lo")
    assert w_hi.gain_db == pytest.approx(BASE_STIMULUS_PEAK_DBFS)
    assert w_hi.gain_db - w_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)

    # Tweeter: cap (-65) compresses the base ~33 dB down, honestly recorded in
    # the segment gains + effective peak — but the 10 dB delta is preserved so
    # the behavioral-linearity check still has its two known levels.
    t_hi = check.segment("pilot_tweeter_hi")
    t_lo = check.segment("pilot_tweeter_lo")
    assert t_hi.gain_db < BASE_STIMULUS_PEAK_DBFS
    assert t_hi.gain_db - t_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)
    assert t_hi.effective_peak_dbfs <= -65.0 + 1e-9
    assert t_hi.effective_peak_dbfs >= -65.0 - PILOT_LEVEL_DELTA_DB


def test_verify_pilot_pair_preserves_delta_after_clamp():
    """VERIFY's summed pilot pair rides the min-cap-clamped level but keeps its
    10 dB delta (no admission gate protects VERIFY, so the clamp must not
    silently collapse the pair to one level)."""
    c, _topology, _profile, _targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    verify = c.program_for_phase(PHASE_VERIFY)
    v_hi = verify.segment("pilot_summed_hi")
    v_lo = verify.segment("pilot_summed_lo")
    assert v_hi.gain_db - v_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)
    assert v_hi.effective_peak_dbfs <= -65.0 + 1e-9
    # And the summed sweep itself is clamped to the same binding cap.
    assert verify.segment("sweep_verify").effective_peak_dbfs <= -65.0 + 1e-9


def test_uncapped_check_program_would_be_refused_regression(tmp_path):
    """The pre-W6.1 shape: a CHECK program composed at the shared reference base
    (ignoring caps) is refused by admission on the JTS3 tweeter — the exact
    program_channel_peak_over_cap refusal hardware run 2 hit."""
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionRefusal,
        readmit_program_from_wav,
    )
    from jasper.audio_measurement.program import build_check_program, write_program_wav

    c, topology, profile, targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    uncapped = build_check_program(c._roles, downstream_gain_db=sv)  # no role bases
    wav = tmp_path / "uncapped.wav"
    write_program_wav(wav, uncapped)
    adm = readmit_program_from_wav(
        uncapped, wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP in adm.refusals


def test_verify_wav_rendered_sample_peak_respects_min_cap(tmp_path):
    """Byte-level pin for the VERIFY clamp (W6.1 gate nit): VERIFY has NO
    play-time readmit — the rendered WAV's actual sample peak is what the
    speaker emits — so assert the WAV bytes themselves, not just the schedule:
    sample peak + session volume ≤ min cap (+0.1 dB int16 quantization slack)."""
    import math as _math

    from scipy.io import wavfile

    from jasper.audio_measurement.program import write_program_wav

    c, _topology, _profile, _targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    wav = tmp_path / "verify_program.wav"
    write_program_wav(wav, c.program_for_phase(PHASE_VERIFY))
    rate, data = wavfile.read(str(wav))
    assert rate == c.program_for_phase(PHASE_VERIFY).sample_rate_hz
    peak = float(np.max(np.abs(data.astype(np.float64) / 32767.0)))
    assert peak > 0.0  # the clamped program still carries signal
    peak_dbfs = 20.0 * _math.log10(peak)
    binding_cap = -65.0
    assert peak_dbfs + sv <= binding_cap + 0.1
    # And it is not clamped into oblivion: the sweep sits within a few dB of
    # the cap-backoff level (the clamp targets the cap, not silence).
    assert peak_dbfs + sv >= binding_cap - 1.0


# --- W6.5: the sensitivity-derived HF ceiling drives PRODUCTION composition -----
#
# The 2026-07-19 gate blocker: the derived ceiling existed in admission but the
# conductor context resolved caps WITHOUT the proven-HP flag, so every composed
# level (CHECK pilot bases, MEASURE back_off_gain, VERIFY min(caps)) still
# clamped to the legacy -65 — reviewer-measured composed CHECK pilot: -65.01.
# This pin drives the conductor with caps resolved EXACTLY the way the fixed
# resolve_conductor_context resolves them (program_admission=True + the
# declaration's sensitivities) and asserts the composed tweeter hi pilot lands
# at the derived cap, then that admission (same declared mapping) agrees.


def test_jts3_derived_hf_ceiling_drives_production_conductor_composition(tmp_path):
    from jasper.active_speaker.excitation_safety_plan import (
        resolve_driver_excitation_ceilings,
    )
    from jasper.active_speaker.program_admission import readmit_program_from_wav
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )
    from jasper.audio_measurement.program import write_program_wav

    from tests.test_active_speaker_program_admission import _profile_and_targets

    # JTS3 declaration: Epique E150HE-44 83.3 dB / B&C DE250-8 108.5 dB.
    declared = {"woofer": 83.3, "tweeter": 108.5}
    topology, profile, targets = _profile_and_targets(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    # PRODUCTION cap resolution — the exact call the fixed context site makes.
    caps = {}
    for role, fingerprint in targets.items():
        _band, cap = resolve_driver_excitation_ceilings(
            profile,
            fingerprint,
            program_admission=True,
            declared_sensitivities=declared,
        )
        caps[role] = float(cap)
    # Probe (a): context caps == admission caps == the derived {-8, -33.2}.
    # -33.2 is the sensitivity arithmetic (-8 less the 25.2 dB delta); the
    # provisional -35 dBFS absolute hedge over it was retired 2026-08-20.
    assert caps == {"woofer": -8.0, "tweeter": pytest.approx(-33.2)}
    sv = session_measurement_volume_db(
        profile, targets.values(), declared_sensitivities=declared
    )
    assert sv == -20.0  # max(caps) is still the woofer's — volume unchanged

    roles = [
        RoleBand("woofer", 0, FrequencyBand(500.0, 1600.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 10000.0)),
    ]
    c = CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=roles,
        fc_hz=FC_HZ,
        driver_caps_dbfs=caps,
        session_volume_db=sv,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
    )
    # Probe (b): the composed CHECK tweeter hi pilot rides the DERIVED cap
    # (back_off margin under -33.2), not the legacy -65.01 the gate measured.
    t_hi = c.program_for_phase(PHASE_CHECK).segment("pilot_tweeter_hi")
    assert t_hi.effective_peak_dbfs == pytest.approx(-33.2 - GAIN_CAP_BACKOFF_DB)
    # And the play-time gate (same declared mapping, as bind_production_play
    # now threads it) admits what the conductor composed.
    wav = tmp_path / "check.wav"
    write_program_wav(wav, c.program_for_phase(PHASE_CHECK))
    adm = readmit_program_from_wav(
        c.program_for_phase(PHASE_CHECK), wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
        declared_sensitivities=declared,
    )
    assert adm.allowed, adm.refusals
    facts = {f.role: f for f in adm.channels}
    assert facts["tweeter"].cap_dbfs == pytest.approx(-33.2)
    # Without the declared mapping (the pre-fix admission view) the SAME
    # composed program is refused — the incoherence the threading closes.
    stale = readmit_program_from_wav(
        c.program_for_phase(PHASE_CHECK), wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not stale.allowed


# --- per-capture diagnostic logging (durable observability, Part 1) -------------
#
# Every CHECK/MEASURE/VERIFY capture now logs its full numeric diagnostics via
# ``log_event`` on BOTH the accepted path and every rejection — before this
# change a failed hardware run left no numbers to look at (only a partial
# ``program_analysis.glitch`` line existed, and only for a glitch MEASURE).
# These tests pin the event names + key fields on accept AND reject.


def test_diag_logging_bug_cannot_crash_or_flip_the_verdict(caplog, monkeypatch):
    """The diag-logging call is wrapped defensively (``_safe_log_diag``),
    symmetric with the capture-retention path's own best-effort guarantee —
    a bug in a ``_log_*_diag`` method must degrade to a WARN, never crash
    the capture or change the verdict already decided above it. Exercises
    all three phases through the SAME shared wrapper."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)

    monkeypatch.setattr(
        c, "_log_check_diag",
        lambda analysis, verdict: (_ for _ in ()).throw(AttributeError("boom")),
    )
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True  # the verdict is completely unaffected
    assert "event=correction.crossover_v2_diag_log_failed" in caplog.text
    assert "phase=check" in caplog.text
    caplog.clear()

    monkeypatch.setattr(
        c, "_log_measure_diag",
        lambda analysis, verdict: (_ for _ in ()).throw(TypeError("boom")),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_diag_log_failed" in caplog.text
    assert "phase=measure" in caplog.text
    caplog.clear()

    fakes.apply_done = True
    monkeypatch.setattr(
        c, "_log_verify_diag",
        lambda analysis, verdict: (_ for _ in ()).throw(ValueError("boom")),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_diag_log_failed" in caplog.text
    assert "phase=verify" in caplog.text


def test_check_diag_logs_full_numbers_on_accept(caplog):
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        pilots=(
            _pilot_obs("woofer", snr_db=20.0, target_rise_db=18.0, cross_rise_db=1.0),
            _pilot_obs("tweeter", snr_db=15.0, target_rise_db=22.0, cross_rise_db=2.0),
        ),
        linearity_ok=True, channel_map_ok=True, pilot_snr_ok=True,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_check_diag" in caplog.text
    assert "accepted=true" in caplog.text
    assert "pilot_snr_ok=true" in caplog.text
    assert "woofer_snr_db=20.0" in caplog.text
    assert "tweeter_snr_db=15.0" in caplog.text
    assert "woofer_captured_delta_db=10.0" in caplog.text
    assert "woofer_programmed_delta_db=10.0" in caplog.text
    assert "woofer_channel_map_target_rise_db=18.0" in caplog.text
    assert "tweeter_channel_map_cross_rise_db=2.0" in caplog.text
    # Both RAW rises AND the ratio derived from them: the raws keep a sweep of
    # these lines comparable across the 2026-08-21 metric switch, the ratio is
    # what the CROSS verdict is now decided on.
    assert "woofer_channel_map_isolation_db=17.0" in caplog.text
    assert "tweeter_channel_map_isolation_db=20.0" in caplog.text


def test_check_diag_names_the_isolation_ratio_and_its_bound_on_a_refusal(caplog):
    """A `channel_map_mismatch` refusal has to be readable from the journal.

    The household-facing copy stays number-free by design (the Language guide:
    one reason, one action), so the diag line IS the operator's record of the
    refusal. Since 2026-08-21 the CROSS verdict is decided on the ISOLATION
    RATIO rather than an additive cross-rise bound, so the line has to carry
    both the ratio and the bound it was graded against — without the bound, a
    journal of old lines is silently reinterpreted the next time the constant
    moves, and the operator cannot tell a refusal from a near miss.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        pilots=(
            # The swap shape: the driver played (target clears the floor) but
            # the other band rose with it, so the isolation collapses.
            _pilot_obs("woofer", target_rise_db=40.0, cross_rise_db=39.0,
                       channel_map_ok=False),
            _pilot_obs("tweeter", target_rise_db=22.0, cross_rise_db=2.0),
        ),
        linearity_ok=True, channel_map_ok=False, pilot_snr_ok=True,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is False
    assert verdict["code"] == "channel_map_mismatch"
    assert "event=correction.crossover_v2_check_diag" in caplog.text
    assert "woofer_channel_map_isolation_db=1.0" in caplog.text
    assert f"channel_map_min_isolation_db={CHANNEL_MAP_MIN_ISOLATION_DB}" in caplog.text
    # ...and the threshold, without which a sub-bound isolation figure on a
    # QUIET capture would read as the cause of a refusal that never happened:
    # below it the ratio is published but decides nothing.
    assert (
        f"channel_map_isolation_judged_above_db={CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB}"
        in caplog.text
    )
    # The raws that produced the ratio are still there to attribute it with.
    assert "woofer_channel_map_target_rise_db=40.0" in caplog.text
    assert "woofer_channel_map_cross_rise_db=39.0" in caplog.text


def test_check_priors_carry_fc_for_the_measure_level_solve():
    """#1825: CHECK's gain solve scopes each band's SNR requirement by whether
    the band sits inside the crossover overlap window, so Fc has to reach the
    CHECK analysis. It used to run on bare defaults."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    phase, _prog_phase, _result, priors, _geometry = fakes.analyzed[0]
    assert phase == "check"
    assert priors.crossover_fc_hz == pytest.approx(FC_HZ)


def test_check_diag_discloses_the_per_driver_measure_level_solve(caplog):
    """#1825 honesty: the solved MEASURE level and the ambient evidence it
    rests on land in the journal, one event per driver."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = _check_analysis_with_solves
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1)["accepted"] is True
    text = caplog.text
    assert text.count("event=correction.crossover_v2_measure_level_solve") == 2
    for fragment in (
        "role=woofer", "solved_gain_db=-19.0", "flat_target_gain_db=-11.0",
        "reduction_db=8.0", "bound_by=room_snr", "ambient_dbfs=-60.0",
        "required_snr_db=41.0", "band_lo_hz=150.0", "band_hi_hz=2000.0",
        "role=tweeter", "solved_gain_db=-31.0", "reduction_db=18.0",
        "ambient_dbfs=-72.0",
    ):
        assert fragment in text


def test_check_diag_discloses_the_level_solve_on_a_rejected_check_too(caplog):
    """Knowing what level the solve WOULD have chosen is exactly what an
    `snr_floor` refusal needs read beside it — so the disclosure rides the
    diagnostic path, not the accept path."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis_with_solves(
        program, snr_floor_ok=False,
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is False and verdict["code"] == "snr_floor"
    assert caplog.text.count("event=correction.crossover_v2_measure_level_solve") == 2


def test_check_diag_survives_a_gain_plan_without_solves(caplog):
    """A legacy/fixture plan carries no ``role_solves``; the disclosure must
    simply not fire rather than crash the diagnostic path."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)  # default _check_analysis, no role_solves
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert "event=correction.crossover_v2_check_diag" in caplog.text
    assert "event=correction.crossover_v2_measure_level_solve" not in caplog.text
    assert "event=correction.crossover_v2_diag_log_failed" not in caplog.text


def test_check_pilot_delta_is_the_delta_measure_pilots_actually_use():
    """#1825's pilot floor reserves `hi_seg.gain_db - lo_seg.gain_db` read off
    the CHECK program — because that is what MEASURE's own leading pair will
    drop its quiet side by (`SessionExcitation.pilot_gains` /
    `PILOT_LEVEL_DELTA_DB`). If the
    two ever diverged the floor would be mis-sized in silence, so pin them
    equal at the composers that produce them."""
    from jasper.active_speaker.crossover_v2_flow import PILOT_LEVEL_DELTA_DB

    fakes = FakeSeams()
    fakes.check = _check_analysis_with_solves
    c = _conductor(fakes)

    check = c.program_for_phase("check")
    for role in ("woofer", "tweeter"):
        lo = check.segment(f"pilot_{role}_lo")
        hi = check.segment(f"pilot_{role}_hi")
        assert hi.gain_db - lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)

    assert _run_phase(c, 1, 1)["accepted"] is True
    measure = c.program_for_phase("measure")
    m_lo = measure.segment("pilot_woofer_lo")
    m_hi = measure.segment("pilot_woofer_hi")
    assert m_hi.gain_db - m_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)


def test_measure_program_keeps_solved_gains_per_role_and_identical_per_repeat():
    """Constraint the drift estimator depends on: the CHECK solve moves each
    ROLE's gain independently, but every repeat of a role stays bit-identical
    (`program.build_measure_program`'s own promise) — per-ROLE differs,
    per-REPEAT must not."""
    fakes = FakeSeams()
    fakes.check = _check_analysis_with_solves
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1)["accepted"] is True
    measure = c.program_for_phase("measure")
    w_gains = {
        measure.segment(sid).gain_db
        for sid in ("sweep_w", "sweep_w_rep", "sweep_w_rep2")
    }
    t_gains = {
        measure.segment(sid).gain_db
        for sid in ("sweep_t", "sweep_t_rep", "sweep_t_rep2")
    }
    assert len(w_gains) == 1 and len(t_gains) == 1
    assert w_gains != t_gains


def test_check_diag_logs_full_numbers_on_rejection_too(caplog):
    """The bug this fixes: a rejected CHECK used to leave no numbers behind."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        pilots=(
            _pilot_obs("woofer", snr_db=5.0, snr_valid=False),
            _pilot_obs("tweeter", snr_db=15.0),
        ),
        linearity_ok=True, channel_map_ok=True, pilot_snr_ok=False,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is False
    assert verdict["code"] == "snr_floor"
    assert "event=correction.crossover_v2_check_diag" in caplog.text
    assert "accepted=false" in caplog.text
    assert "code=snr_floor" in caplog.text
    assert "pilot_snr_ok=false" in caplog.text
    # Numbers still present on the rejected capture.
    assert "woofer_snr_db=5.0" in caplog.text
    assert "tweeter_snr_db=15.0" in caplog.text


def test_measure_diag_logs_full_numbers_on_accept(caplog):
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: ProgramAnalysis(
        phase="measure", program_id=program.program_id,
        locations=(
            _loc("sweep_w"), _loc("sweep_t"), _loc("sweep_w_rep"),
        ),
        drift=DriftEstimate(
            epsilon_ppm=30.0,
            max_residual_samples=0.2, glitch_detected=False,
            repeat_level_delta_db=0.05,
        ),
        driver_responses=(
            _driver_response_diag(
                "woofer", window_ms=8.0, floor_hz=180.0, snr_db=25.0, snr_verdict="ok",
            ),
            _driver_response_diag(
                "tweeter", window_ms=9.0, snr_db=8.0, snr_verdict="insufficient",
            ),
        ),
        alignment=AlignmentEstimate(
            delay_us=150.0, raw_delay_us=161.0, parallax_us=11.0,
            polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
            confidence=0.9, seed_delay_us=120.0,
            confidence_source="gcc_phat_seed",
        ),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.0, "tweeter": 0.0}, polarity="normal",
            delay_us=150.0, predicted_ripple_db=1.23, confidence=0.9,
            alignment_seed_ripple_db=4.56, flatness_improvement_db=3.33,
            anchor_delay_us=145.0, snap_delta_us=5.0, snap_found=True,
            alignment_objective="flat_sum_committed", seed_polarity_sign=-1,
            left_anchor_lobe=True,
        ),
        linearity_ok=True,
        predicted_sum=(np.linspace(100.0, 20000.0, 64), np.zeros(64)),
        glitch_detected=False,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_measure_diag" in caplog.text
    assert "accepted=true" in caplog.text
    assert "alignment_confidence=0.9" in caplog.text
    assert "alignment_confidence_source=gcc_phat_seed" in caplog.text
    assert "alignment_seed_delay_us=120.0" in caplog.text
    assert "alignment_refinement_delta_us=30.0" in caplog.text
    assert "gate_window_ms=8.0" in caplog.text  # min(8.0, 9.0)
    assert "validity_floor_hz=180.0" in caplog.text  # max(180.0) — only one floor set
    assert "epsilon_ppm=30.0" in caplog.text
    assert "max_residual_samples=0.2" in caplog.text
    assert "repeat_level_delta_db=0.05" in caplog.text
    assert "delay_role=tweeter" in caplog.text  # positive delay_us ⇒ tweeter delayed
    # ``polarity`` here is the candidate-facing keep/invert action
    # (``alignment_to_candidate_fields``'s third return value), not the raw
    # AlignmentEstimate.polarity ("normal"/"inverted") — "normal" maps to
    # POLARITY_KEEP ("keep").
    assert "polarity=keep" in caplog.text
    assert "predicted_ripple_db=1.23" in caplog.text
    assert "alignment_seed_ripple_db=4.56" in caplog.text
    assert "flatness_improvement_db=3.33" in caplog.text
    assert "anchor_delay_us=145.0" in caplog.text
    assert "snap_delta_us=5.0" in caplog.text
    assert "snap_found=true" in caplog.text
    assert "woofer_snr_db=25.0" in caplog.text
    assert "woofer_snr_verdict=ok" in caplog.text
    assert "tweeter_snr_db=8.0" in caplog.text
    assert "tweeter_snr_verdict=insufficient" in caplog.text
    # #2598 / #2607 S2: WHICH objective committed the (polarity, delay) pair,
    # what correlation answered, whether the two agreed, and whether the
    # committed delay left the comb lobe its anchor owns. The lobe flag is the
    # compensating control for the ±1-period search — a wrong-lobe commit is
    # magnitude-flat, so an on-axis VERIFY cannot contradict it and the journal
    # and the receipt are where it has to be legible.
    assert "alignment_objective=flat_sum_committed" in caplog.text
    assert "seed_polarity=inverted" in caplog.text
    assert "polarity_agrees_with_sum=true" in caplog.text
    assert "left_anchor_lobe=true" in caplog.text
    evidence = _analysis_json(fakes.measure(c.program_for_phase(PHASE_MEASURE)))
    assert evidence["alignment_confidence_source"] == "gcc_phat_seed"
    assert evidence["alignment_seed_delay_us"] == 120.0
    assert evidence["alignment_seed_ripple_db"] == 4.56
    assert evidence["flatness_improvement_db"] == 3.33
    assert evidence["anchor_delay_us"] == 145.0
    assert evidence["snap_delta_us"] == 5.0
    assert evidence["snap_found"] is True
    assert evidence["alignment_objective"] == "flat_sum_committed"
    assert evidence["seed_polarity"] == "inverted"
    assert evidence["polarity_agrees_with_sum"] is True
    assert evidence["left_anchor_lobe"] is True


def _measure_snr_analysis(program, *, woofer, tweeter):
    """A minimal accepted MEASURE whose two driver responses carry SNR blocks."""
    return ProgramAnalysis(
        phase="measure", program_id=program.program_id,
        locations=(_loc("sweep_w"), _loc("sweep_t"), _loc("sweep_w_rep")),
        drift=DriftEstimate(
            epsilon_ppm=30.0,
            max_residual_samples=0.2, glitch_detected=False,
        ),
        driver_responses=(woofer, tweeter),
        alignment=_alignment(),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.0, "tweeter": 0.0}, polarity="normal",
            delay_us=150.0, predicted_ripple_db=1.23, confidence=0.9,
        ),
        linearity_ok=True,
        predicted_sum=(np.linspace(100.0, 20000.0, 64), np.zeros(64)),
        glitch_detected=False,
    )


def test_measure_diag_names_the_band_behind_each_driver_snr_pair(caplog):
    """#2613: `*_snr_db` and `*_snr_verdict` say how bad and how trusted,
    never WHICH band. Fourteen consecutive jts3 rounds logged
    `tweeter_snr_db=-1.2 tweeter_snr_verdict=insufficient` and the band that
    actually limited them — one the tweeter sweep never entered — had to be
    re-derived from the crossover frequency and the declared driver bands
    because no persisted artifact carried it. `*_snr_band` is that band,
    read off the same `worst_relevant` entry as the pair beside it."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    woofer = _driver_response_diag(
        "woofer", snr_db=25.0, snr_verdict="ok", snr_band="mid",
    )
    tweeter = _driver_response_diag(
        "tweeter", snr_db=-1.2, snr_verdict="insufficient", snr_band="upper_bass",
    )
    fakes.measure = lambda program: _measure_snr_analysis(
        program, woofer=woofer, tweeter=tweeter,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "woofer_snr_band=mid" in caplog.text
    assert "tweeter_snr_band=upper_bass" in caplog.text
    # Each band is the one its OWN worst_relevant entry carries, so a
    # role-crossed read cannot pass.
    assert woofer.snr["worst_relevant"]["band_id"] == "mid"
    assert tweeter.snr["worst_relevant"]["band_id"] == "upper_bass"
    # The #2613 line, now self-describing rather than a bare number.
    assert "tweeter_snr_db=-1.2" in caplog.text
    assert "tweeter_snr_verdict=insufficient" in caplog.text


def test_measure_diag_snr_band_is_null_when_the_worst_band_carries_no_id(caplog):
    """`worst_band_verdict` filters candidates on band overlap and verdict
    rank, never on identity, so it can select a band with no `band_id`. That
    has to log as `null` — Python's `None` stringified into `band=None` would
    read as a band literally named "None"."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_snr_analysis(
        program,
        woofer=_driver_response_diag(
            "woofer", snr_db=25.0, snr_verdict="ok", snr_band=None,
        ),
        tweeter=_driver_response_diag("tweeter"),  # no snr block at all
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "woofer_snr_band=null" in caplog.text
    assert "woofer_snr_band=None" not in caplog.text
    # The number beside it still reports, so the field says "this band has no
    # id", not "there was no measurement".
    assert "woofer_snr_db=25.0" in caplog.text
    # A driver with no SNR block at all reaches the same literal.
    assert "tweeter_snr_band=null" in caplog.text


def test_measure_diag_logs_per_role_repeat_epsilon_ppm(caplog):
    """#1668 PR-A/PR-C: DriftEstimate.per_role_epsilon_ppm (a first-vs-last
    per-role epsilon, one entry per role with >=2 located occurrences) now
    surfaces as woofer_repeat_epsilon_ppm / tweeter_repeat_epsilon_ppm on
    the measure_diag event — diagnostic only, never gated."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: ProgramAnalysis(
        phase="measure", program_id=program.program_id,
        locations=(
            _loc("sweep_w"), _loc("sweep_t"),
            _loc("sweep_w_rep"), _loc("sweep_t_rep"),
        ),
        drift=DriftEstimate(
            epsilon_ppm=30.0,
            max_residual_samples=0.2, glitch_detected=False,
            per_role_epsilon_ppm={"woofer": 31.5, "tweeter": -4.25},
        ),
        driver_responses=(
            _driver_response_diag("woofer", window_ms=8.0),
            _driver_response_diag("tweeter", window_ms=9.0),
        ),
        alignment=_alignment(),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.0, "tweeter": 0.0}, polarity="normal",
            delay_us=150.0, predicted_ripple_db=1.23, confidence=0.9,
        ),
        linearity_ok=True,
        predicted_sum=(np.linspace(100.0, 20000.0, 64), np.zeros(64)),
        glitch_detected=False,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "woofer_repeat_epsilon_ppm=31.5" in caplog.text
    assert "tweeter_repeat_epsilon_ppm=-4.25" in caplog.text


def test_measure_diag_per_role_repeat_epsilon_ppm_none_safe_for_legacy_drift(caplog):
    """A DriftEstimate predating per_role_epsilon_ppm (empty mapping — the
    field's own default) or a role absent from it must log None, never
    raise or fabricate a 0.0."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    # log_event renders None as the JSON literal "null", not Python's "None".
    assert "woofer_repeat_epsilon_ppm=null" in caplog.text
    assert "tweeter_repeat_epsilon_ppm=null" in caplog.text


def test_measure_diag_logs_full_numbers_on_glitch_rejection_too(caplog):
    """The headline bug this fixes: today a rejected MEASURE persists none of
    confidence/gate_window/epsilon — this proves they're all still logged."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program, glitch=True)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is False
    assert verdict["code"] == "drift_baselines_disagree"
    assert "event=correction.crossover_v2_measure_diag" in caplog.text
    assert "accepted=false" in caplog.text
    assert "code=drift_baselines_disagree" in caplog.text
    assert "gate_window_ms=8.0" in caplog.text
    assert "epsilon_ppm=30.0" in caplog.text
    assert "alignment_confidence=0.8" in caplog.text
    assert "predicted_ripple_db=0.8" in caplog.text
    # The pre-existing glitch check, not G2 — guard stays empty.
    assert 'guard=""' in caplog.text


def test_measure_diag_logs_full_numbers_on_low_alignment_confidence(caplog):
    """The numbers still ride the diag, on a capture that is now ACCEPTED.

    Transformed with the demotion, the same way the ripple test below was: what
    the capture measured is unchanged, and only the consequence moved. ``guard``
    now names the disclosure, because its siblings name checks that REFUSED and
    leaving a refusal's vocabulary on an accepting path would mislead exactly
    the reader that field exists for."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    assert 0.55 < ALIGNMENT_CONFIDENCE_TRUST_FLOOR  # keep the fixture below the floor
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, alignment=_alignment(confidence=0.55),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert not verdict.get("code")
    assert "event=correction.crossover_v2_measure_diag" in caplog.text
    assert "alignment_confidence=0.55" in caplog.text
    assert "predicted_ripple_db=0.8" in caplog.text
    assert "guard=alignment_confidence_disclosure" in caplog.text
    # The dedicated event is the stable line to alert or count on — ``guard``
    # is one field on a diagnostic that fires on every capture.
    assert "event=correction.crossover_v2_alignment_confidence_disclosed" in caplog.text
    assert "trust_floor=0.6" in caplog.text


def test_measure_diag_logs_guard_field_on_ripple_disclosure(caplog):
    """The diag ``guard`` field still names G1 on an ACCEPTED capture (#2087).

    Transformed from the pre-ruling pin, which asserted ``guard=ripple_ceiling``
    on a refusal. The value changed with the behaviour: its siblings name
    checks that REFUSED, so a path that now accepts must not keep a refusal's
    vocabulary. This is what keeps the existing per-capture telemetry able to
    find these captures — and asserting ``accepted`` alongside it is the point,
    since a reader of this field can no longer infer a rejection from it."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=27.316,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_measure_diag" in caplog.text
    assert "guard=ripple_disclosure" in caplog.text
    assert "predicted_ripple_db=27.316" in caplog.text


def test_measure_diag_logs_guard_field_on_sweep_schedule_fire(caplog):
    """The diag ``guard`` field distinguishes a G2 fire from the pre-
    existing glitch_detected branch — both share the reused
    drift_baselines_disagree code (see the glitch test above for the "guard
    empty" counterpart)."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc("sweep_w", confidence=0.8,
                 residual_samples=-25e-3 * program.sample_rate_hz),
            _loc("sweep_t", confidence=0.8),
            _loc("sweep_w_rep", confidence=0.8),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "drift_baselines_disagree"
    assert "event=correction.crossover_v2_measure_diag" in caplog.text
    assert "guard=sweep_schedule" in caplog.text
    assert "sweep_residual_ms_worst=-25.0" in caplog.text
    assert "sweep_locate_confidence_min=0.8" in caplog.text


def test_verify_diag_logs_full_numbers_on_accept(caplog):
    """The ``verify_diag`` line logs the full disclosure ON ACCEPT — accept
    meaning VERIFY's OWN capture gate, asserted straight off the line's own
    ``accepted=true`` field below.

    This fixture is a raw ``ProgramAnalysis`` with no ``capture_integrity``
    set (a "legacy-shaped" capture, see the ``pilot_transfer_db=null`` note
    below), so it is unusable evidence for the ROUND (#2537): the overall
    verdict is a refusal (untrusted evidence, no rollback anchor bound on
    this bare conductor), asserted first so the numbers-disclosure claim below
    it is not mistaken for "and therefore the round kept it"."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        summed_response=_driver_response_diag("summed", window_ms=8.5, floor_hz=900.0),
        summed_ripple_db=1.1,
        verify_tracking={
            "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            "tracking_band_hz": [800.0, 3200.0],
        },
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    # The round refuses (untrusted evidence, no rollback anchor) — #2537.
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED
    assert "event=correction.crossover_v2_verify_diag" in caplog.text
    assert "accepted=true" in caplog.text
    assert "max_db_notch_excluded=0.9" in caplog.text
    assert "verify_tolerance_db=1.5" in caplog.text
    assert "verify_gate_window_ms=8.5" in caplog.text
    assert "measure_gate_window_ms=8.0" in caplog.text
    assert "validity_floor_hz=900.0" in caplog.text
    assert "tracking_band_lo_hz=800.0" in caplog.text
    assert "tracking_band_hi_hz=3200.0" in caplog.text
    assert "rms_db=0.4" in caplog.text
    # No pilots on this fixture (a legacy-shaped ProgramAnalysis) — G3's
    # fields render as absent, never a false 0.0.
    assert "pilot_transfer_db=null" in caplog.text
    assert "pilot_transfer_step_db=null" in caplog.text
    assert 'guard=""' in caplog.text


def test_verify_diag_logs_full_numbers_on_out_of_tolerance_rejection_too(caplog):
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.verify = lambda program: _verify_analysis(program, max_db=5.0, gate_ms=8.5)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_out_of_tolerance"
    assert "event=correction.crossover_v2_verify_diag" in caplog.text
    assert "accepted=false" in caplog.text
    assert "code=verify_out_of_tolerance" in caplog.text
    assert "max_db_notch_excluded=5.0" in caplog.text
    assert "verify_gate_window_ms=8.5" in caplog.text
    assert "measure_gate_window_ms=8.0" in caplog.text


def test_verify_diag_logs_full_numbers_on_inconclusive_rejection(caplog):
    """A too-short VERIFY gate rejects as ``verify_inconclusive`` BEFORE the
    tracking-error branch even runs — confirms the diag log still fires and
    still carries the two gate-window numbers that decided it."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    # measure_gate_window_ms defaults to 8.0 (the happy-path MEASURE fixture);
    # a VERIFY gate narrower than that is inconclusive per §5.2.
    fakes.verify = lambda program: _verify_analysis(program, gate_ms=4.0)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_inconclusive"
    assert "event=correction.crossover_v2_verify_diag" in caplog.text
    assert "verify_gate_window_ms=4.0" in caplog.text
    assert "measure_gate_window_ms=8.0" in caplog.text


def test_verify_diag_logs_guard_field_and_pilot_transfer_on_level_shift_fire(caplog):
    """Measurement-honesty gate G3's own diagnostics: the baseline-setting
    attempt logs its raw transfer with a null step and empty guard; the
    fired attempt logs its own transfer, the computed step, and
    guard=pilot_level_shift."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    _run_phase(c, 3, 3)
    # transfer = level_hi_dbfs(-20.0) - programmed_hi_gain_db(-20.0) = 0.0.
    assert "pilot_transfer_db=0.0" in caplog.text
    assert "pilot_transfer_step_db=null" in caplog.text
    assert 'guard=""' in caplog.text
    caplog.clear()

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.56, max_db=0.5,
    )
    verdict = _run_phase(c, 3, 4)
    assert verdict["code"] == "verify_level_shift"
    assert "event=correction.crossover_v2_verify_diag" in caplog.text
    # transfer = level_hi_dbfs(-19.44) - programmed_hi_gain_db(-20.0) = 0.56.
    assert "pilot_transfer_db=0.56" in caplog.text
    assert "pilot_transfer_step_db=0.56" in caplog.text
    assert "guard=pilot_level_shift" in caplog.text


def test_verify_diag_pilot_transfer_step_does_not_leak_across_an_early_return(caplog):
    """Adversarial-review fix (S1): ``_verify_pilot_transfer_step_db`` must
    reset at the TOP of every ``_verify_verdict`` call (mirrors
    ``_last_measure_guard``'s method-top reset in ``_measure_verdict``) — an
    early return BEFORE the G3 block even runs (locate_failed here) must not
    leave a PRIOR attempt's REAL step number for ``_log_verify_diag`` (which
    runs unconditionally) to misreport as if it were computed this attempt."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    # Attempt 1 (N-1): establishes the baseline (independently out of
    # tolerance, so a retry is admitted).
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    _run_phase(c, 3, 3)

    # Attempt 2 (N): a REAL, non-None step gets computed and logged (0.1 dB,
    # within the ceiling — independently out of tolerance too, so a 3rd
    # attempt is admitted). ``max_db`` is deliberately more than
    # ``VERIFY_REPEAT_FLOOR_DB`` away from attempt 1's: agreeing with it would
    # earn #1873's terminal ``verify_deterministic_mismatch`` and refuse the
    # 3rd attempt this test needs.
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.1, max_db=8.0,
    )
    _run_phase(c, 3, 4)
    assert "pilot_transfer_step_db=0.1" in caplog.text
    caplog.clear()

    # Attempt 3 (N+1): locate_failed — returns BEFORE the G3 block runs at
    # all. Without the S1 fix this would still show attempt 2's stale 0.1;
    # with it, the diag must show null.
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.1, locate_confidence=0.01,
    )
    verdict = _run_phase(c, 3, 5)
    assert verdict["code"] == "locate_failed"
    assert "event=correction.crossover_v2_verify_diag" in caplog.text
    assert "pilot_transfer_step_db=null" in caplog.text


# --------------------------------------------------------------------------- #
# Layer-1a driver linearization (#1668 PR-C)
# --------------------------------------------------------------------------- #
#
# sigma composition (_compose_sigma_db, the paired-N gate + tier floor) and
# the conductor's integration reorder (_build_candidate's hard gate + the
# fit -> apply-in-linear-domain -> re-solve-trim -> sanity-backstop chain).


def test_compose_sigma_db_none_when_own_under_paired_threshold():
    own = _resp_with_repeats("woofer", 1)  # 2 total occurrences, < 3
    sibling = _resp_with_repeats("tweeter", 4)  # 5 total, plenty
    assert 1 + len(own.repeat_responses) < LINEARIZATION_MIN_PAIRED_OCCURRENCES
    sigma = _compose_sigma_db(own, sibling, tier="reference", valid_band_hz=(150.0, 4000.0))
    assert sigma is None


def test_compose_sigma_db_none_when_sibling_under_paired_threshold():
    """An under-repeated SIBLING voids the pair's trust even though ``own``
    alone clears the threshold — this is the PAIRED gate, not a per-driver
    one."""
    own = _resp_with_repeats("woofer", 4)  # 5 total, plenty
    sibling = _resp_with_repeats("tweeter", 1)  # 2 total, < 3
    sigma = _compose_sigma_db(own, sibling, tier="reference", valid_band_hz=(150.0, 4000.0))
    assert sigma is None


def test_compose_sigma_db_returns_array_when_both_meet_threshold():
    own = _resp_with_repeats("woofer", 2)  # 3 total, exactly at the gate
    sibling = _resp_with_repeats("tweeter", 2)
    sigma = _compose_sigma_db(own, sibling, tier="reference", valid_band_hz=(150.0, 4000.0))
    assert sigma is not None
    assert not np.isnan(sigma).any()


def test_compose_sigma_db_floors_at_the_tiers_own_tolerable_value():
    """Identical repeats -> live sigma ~ 0 everywhere -> floored up to the
    tier's own sigma_tolerable (consumer: 1.0 dB)."""
    own = _resp_with_repeats("woofer", 2)
    sibling = _resp_with_repeats("tweeter", 2)
    sigma = _compose_sigma_db(own, sibling, tier="consumer", valid_band_hz=(150.0, 4000.0))
    assert sigma is not None
    assert np.all(sigma >= 1.0 - 1e-9)
    assert np.allclose(sigma, 1.0, atol=1e-6)


def test_compose_sigma_db_floor_is_behaviorally_inert_on_repeatability_limit():
    """The docstring's 'currently does nothing' claim, proven end-to-end:
    repeatability_limit(floored_sigma) must equal repeatability_limit(
    raw_live_sigma) bin-for-bin, because any live sigma <=
    sigma_tolerable already saturates repeatability_limit's own
    min(1, ...) at its ceiling — flooring a value already at/below the
    floor changes nothing."""
    from jasper.active_speaker.linearization_envelope import (
        compute_sigma_curve,
        repeatability_limit,
    )

    own = _resp_with_repeats("woofer", 2)
    sibling = _resp_with_repeats("tweeter", 2)
    floored = _compose_sigma_db(own, sibling, tier="reference", valid_band_hz=(150.0, 4000.0))
    raw = compute_sigma_curve(own, valid_band_hz=(150.0, 4000.0))
    assert floored is not None and raw is not None
    assert not np.allclose(floored, raw)  # the floor DID change the sigma values themselves...
    limit_floored = repeatability_limit(floored, tier="reference")
    limit_raw = repeatability_limit(raw, tier="reference")
    np.testing.assert_allclose(limit_floored, limit_raw)  # ...but not the envelope term they feed


# --- conductor integration reorder ------------------------------------------


@pytest.mark.parametrize("woofer_level_db,tweeter_level_db,expected_trim", [
    # The tweeter is louder, so IT is the one attenuated. This direction
    # already worked even under the original hardcoded-woofer-0.0 helper,
    # because the fixture's one shipped pair always happened to have the
    # quieter woofer.
    (0.0, 20.0, {"woofer": 0.0, "tweeter": -20.0}),
    # #1938 gate follow-up (SF-1): the direction that was SILENTLY BROKEN by
    # the woofer-trim hardcode. The woofer is louder here, so the WOOFER must
    # be the one attenuated — but `_solve_fixture_raw_trim` used to return
    # {"woofer": 0.0, "tweeter": round(trim_t, 3)} unconditionally, and for a
    # louder woofer the solved `trim_t` is itself 0.0 (the tweeter needs no
    # attenuation), so the whole dict silently came back {0.0, 0.0} — a no-op
    # that left both branches at their original, still-mismatched levels.
    (20.0, 0.0, {"woofer": -20.0, "tweeter": 0.0}),
])
def test_eligible_measure_analysis_derives_trim_from_its_own_custom_curves(
    woofer_level_db, tweeter_level_db, expected_trim,
):
    """#1938 regression guard, both directions.

    A caller handing ``_eligible_measure_analysis`` CUSTOM ``woofer_db``/
    ``tweeter_db`` curves, with no explicit ``trim_db``, must get a trim
    SOLVED from those curves — never the module constant
    ``_FIXTURE_RAW_TRIM_DB``, which is solved from the DEFAULT curves and is a
    different pair. That silent fallback is the "one speaker's branches,
    another speaker's trim" incoherence :func:`_solve_fixture_raw_trim`'s own
    docstring documents for the default curves, reintroduced through the
    custom-curve parameters (#1938's finding, discovered via
    ``test_prediction_gate_logs_the_improved_path_with_both_terms`` /
    PR #1934 and the two call sites this issue's fix corrected —
    ``test_linearized_ripple_polish_is_skipped_on_a_one_sided_band`` and
    ``test_prediction_gate_refuses_a_correction_that_does_not_improve``).

    Two FLAT curves 20 dB apart, in each direction, make the expected trim a
    closed form — attenuate whichever branch is louder by exactly the gap —
    rather than a number this test would have to take on faith from the
    solver under test.
    """
    freqs = _LINEARIZABLE_FREQS_HZ
    flat_woofer_db = np.full_like(freqs, woofer_level_db)
    flat_tweeter_db = np.full_like(freqs, tweeter_level_db)
    program = types.SimpleNamespace(program_id="fixture_trim_guard")

    analysis = _eligible_measure_analysis(
        program, woofer_db=flat_woofer_db, tweeter_db=flat_tweeter_db,
    )

    assert analysis.candidate.trim_db == expected_trim
    # Not the default-curve constant: the regression this guards against is a
    # fixture that silently returns it regardless of the curves it was
    # actually handed.
    assert analysis.candidate.trim_db != dict(_FIXTURE_RAW_TRIM_DB)
    # _eligible_measure_analysis defaults trim_band_average_db to trim_db
    # when omitted, so it must agree too — a caller reading either field
    # sees the same coherent trim.
    assert analysis.candidate.trim_band_average_db == analysis.candidate.trim_db


def test_non_reference_tier_falls_back_byte_identical_to_trims_only():
    """mic_tier != 'reference' — even with a paired N>=3 both drivers —
    must take the EXACT same path as before this PR: raw trim, empty
    linearization dict."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier="consumer")
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}


def test_reference_tier_but_under_repeated_falls_back_byte_identical():
    """Reference-tier mic but the tweeter has only 1 occurrence (< the
    paired-N gate) — must still fall back, byte-identical."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="reference", tweeter_repeats=0,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}


def test_reference_tier_missing_mic_tier_none_falls_back():
    """mic_tier=None (the field's own default — a legacy/unset analysis)
    must resolve to ineligible, never crash on the `!= "reference"`
    comparison."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier=None)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization == {}


def test_eligible_candidate_fits_both_roles_and_moves_trim_toward_ripple_optimal():
    """The asymmetric-overlap fixture (PR-C offline-validated numbers): a
    tweeter bump squarely inside the crossover overlap band gets fitted
    and corrected, and the re-solved trim moves measurably away from the
    raw (uncorrected) solve — toward what the ACTUAL (linearized) branch
    responses justify, not the raw band-average bias #1667 named."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    candidate = c.candidate
    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    assert candidate.role_attenuations_db != raw_trim
    # The bump correction quiets the tweeter's overlap-band level, so the
    # RESOLVED tweeter trim needs LESS attenuation than the raw solve did
    # (moves toward 0, i.e. strictly greater than the raw fixture trim).
    assert candidate.role_attenuations_db["tweeter"] > raw_trim["tweeter"]

    assert set(candidate.linearization) == {"woofer", "tweeter"}
    tweeter_fit = candidate.linearization["tweeter"]
    assert tweeter_fit["filters"], "expected the tweeter bump to attract a filter"
    assert all(f["gain"] <= 0.0 for f in tweeter_fit["filters"])
    for role_fit in candidate.linearization.values():
        assert role_fit["mic_tier"] == "reference"
        assert role_fit["n_repeats"] == 2
        # This test passes no driver_class_by_role override, so every role
        # fits under the ctor's conservative "unknown" default. A production
        # caller now exists (#1665's resolve_conductor_context — see
        # test_declared_driver_class_reaches_the_compose_envelope_seam
        # below); this test is deliberately about the no-override path.
        assert role_fit["driver_class"] == "unknown"


def _measured_level_frame(conductor, *, woofer_db=None, tweeter_db=None):
    """The trim's OWN level frame, re-measured by the test from published inputs.

    The anchor's give-back is measured over ``branch_level_bands_hz`` — the
    same estimator, averaging domain and half-bands that solved ``raw_trim_db``
    and that grade the committed pair — because a give-back spent against a
    trim has to be measured in that trim's frame. A give-back read over each
    driver's own CORE band (``LinearizationFit.correction_giveback_db``)
    answers a different question, and its per-role DIFFERENCE lands as pure
    inter-driver level error: on the jts3 horn tweeter, 2026-08-19, that was
    3.67 dB of hot tweeter.

    **Every input is sourced independently of the planner**, which is what
    stops this being a restatement of ``plan_linearization``'s own arithmetic:
    the spans come from the conductor's OWN MEASURE program, the
    pre-correction pair is the fixture's own declared branch curves, and the
    post-correction pair is those curves times the correction the candidate
    PUBLISHES. Nothing is read back out of the planner, so a change of band,
    of estimator, of sign, or of which pair is pre and which is post fails
    here rather than being absorbed.

    Returns the frame as a namespace: ``giveback_db`` (per role),
    ``linearized`` (the post-correction pair), ``spans``, and ``freqs``.
    """
    from jasper.active_speaker.linearization_fit import (
        LinearizationFilter,
        complex_correction_response,
    )

    default_woofer_db, default_tweeter_db = _fixture_branch_db()
    curves = {
        "woofer": default_woofer_db if woofer_db is None else woofer_db,
        "tweeter": default_tweeter_db if tweeter_db is None else tweeter_db,
    }
    freqs = _LINEARIZABLE_FREQS_HZ
    program = conductor.program_for_phase(PHASE_MEASURE)
    spans = {
        role: (program.segment(seg).f1_hz, program.segment(seg).f2_hz)
        for role, seg in (("woofer", "sweep_w"), ("tweeter", "sweep_t"))
    }
    raw = {
        role: (10.0 ** (np.asarray(curve) / 20.0)).astype(complex)
        for role, curve in curves.items()
    }
    linearized = {
        role: raw[role] * complex_correction_response(
            [
                LinearizationFilter(**f)
                for f in conductor.candidate.linearization[role]["filters"]
            ],
            freqs,
        )
        for role in ("woofer", "tweeter")
    }

    def _levels(pair):
        _residual_w, _residual_t, level_w, level_t = solve_branch_trims(
            freqs, pair["woofer"], pair["tweeter"], FC_HZ,
            woofer_span_hz=spans["woofer"], tweeter_span_hz=spans["tweeter"],
        )
        return {"woofer": level_w, "tweeter": level_t}

    before, after = _levels(raw), _levels(linearized)
    return types.SimpleNamespace(
        freqs=freqs,
        spans=spans,
        linearized=linearized,
        giveback_db={
            role: before[role] - after[role] for role in ("woofer", "tweeter")
        },
    )


def _inter_driver_level_error_db(frame, trim_db):
    """One trim pair's REALIZED inter-driver level error on the linearized pair.

    The anchor's defining property, and the one the band-matched give-back
    buys: ``raw_trim`` level-matches the PRE-correction pair, and adding back
    exactly what the correction removed FROM THAT SAME BAND puts the
    POST-correction pair at the same handoff level. The residual is therefore
    not "close to zero" by luck — it is zero up to the 3-decimal rounding
    ``_solve_fixture_raw_trim`` applies to the fixture's own raw trim, which
    bounds it at 1e-3 dB.
    """
    return realized_branch_level_match(
        frame.freqs, frame.linearized["woofer"], frame.linearized["tweeter"],
        FC_HZ,
        trim_w_db=trim_db["woofer"], trim_t_db=trim_db["tweeter"],
        woofer_span_hz=frame.spans["woofer"],
        tweeter_span_hz=frame.spans["tweeter"],
    ).difference_db


def test_fit_linearization_wires_ripple_optimal_seeded_by_anchored_giveback(
    monkeypatch,
):
    """#1668 anchored give-back: `_fit_linearization`'s ripple fine-tune must be
    seeded by the ANCHORED trim — each branch's own raw candidate trim plus the
    level its emitted cascade removed, normalized non-positive — NOT the old
    `solve_branch_trims` OVERLAP-band average on the linearized pair (which
    under-returned the give-back on the live JTS3 runs). Spies on the
    module-level imported name to pin that the call happened exactly once, with
    the anchored woofer trim held fixed and the analysis's own polarity sign
    passed through.

    **Which band that give-back is read in moved, and the expectation moved
    with it.** It used to be `LinearizationFit.correction_giveback_db`, a power
    mean over each driver's own CORE band. It is now measured over
    ``branch_level_bands_hz`` — the bands that solved the raw trim and that
    grade the committed pair — so the give-back is spent in the frame it was
    measured in. The old overlap-band objection does not carry to those bands:
    PR-L3 deleted the shared overlap frame, and each branch is now read only on
    its own side of Fc. ``_measured_level_frame`` re-measures the new give-back
    from the fixture's own curves and the candidate's PUBLISHED filters, so the
    expectation below is derived rather than transcribed."""

    calls = []
    real_solve = iv.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        # Positional call shape: solve_ripple_optimal_trim(freqs, w_tf,
        # t_tf, fc_hz, *, lo_hz=..., hi_hz=..., seed_trim_db=...,
        # trim_w_db=..., sign=...) -- _fit_linearization passes the first
        # four positionally, the rest by keyword.
        freqs, w_tf, t_tf, fc_hz = args
        calls.append({"freqs": freqs, "w_tf": w_tf, "t_tf": t_tf, "fc_hz": fc_hz, **kwargs})
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    assert len(calls) == 1
    call = calls[0]
    assert call["fc_hz"] == FC_HZ
    assert call["sign"] == 1  # _alignment()'s default polarity="normal"
    # Anchored seed = the anchor's BASE + that branch's own measured give-back,
    # with the shared non-positive normalization shift applied to both roles.
    #
    # The single-datum-owner migration (#2609) deleted the two-voter frame that
    # used to add a reconciled offset to this same anchor. What is left is the
    # one design SSOT — docs/active-speaker-tuning-layers-design.md, "Anchored
    # give-back (the trim)": the committed RAW trim plus that branch's own
    # measured give-back, shared-shift normalized non-positive, no third term.
    #
    # The give-back is re-measured here in the TRIM'S OWN FRAME — see
    # ``_measured_level_frame`` — rather than read off the fit's core-band
    # number, because that is the band the anchor now spends it in.
    base = dict(_FIXTURE_RAW_TRIM_DB)
    frame = _measured_level_frame(c)
    giveback = frame.giveback_db
    unnormalized = {
        r: base[r] + giveback[r] for r in ("woofer", "tweeter")
    }
    shift = max(0.0, max(unnormalized.values()))
    expected_anchored = {r: v - shift for r, v in unnormalized.items()}
    assert call["trim_w_db"] == pytest.approx(expected_anchored["woofer"])
    assert call["seed_trim_db"] == pytest.approx(expected_anchored["tweeter"])
    # …and the property that band-matching buys, asserted independently of the
    # arithmetic above: the seeded pair hands the two linearized branches off
    # at the SAME level. A give-back read in any other band leaves
    # ``giveback_t - giveback_w`` of inter-driver error here instead.
    assert abs(_inter_driver_level_error_db(frame, {
        "woofer": call["trim_w_db"], "tweeter": call["seed_trim_db"],
    })) <= 1e-3

    # What ships is one of the TWO pairs `_fit_linearization` grades — the
    # anchor, or the scan's ripple polish — never the raw trim ("Never the RAW
    # trim, whichever pair wins"). WHICH of the two wins is the PR-L4 level
    # adjudication's business, not this test's: it commits whichever pair the
    # realized inter-driver level instrument scores better, and both branches of
    # that choice have their own pins (test_eligible_candidate_fits_both_roles_
    # and_moves_trim_toward_ripple_optimal for the polish, test_a_disagreeing_
    # frame_whose_realized_check_passes_banks_and_proceeds for the grading).
    #
    # Which pair this fixture lands on has moved more than once, each move
    # worth recording rather than papering over. R10b (panel CC-2(b)) made the
    # fit's `correction_giveback_db` grade the REALIZED biquad cascade instead
    # of `predicted_response`'s Lorentzian, which moved this pair's anchor by
    # +0.124 dB (tweeter -1.383 -> -1.260). BOTH graded pairs moved with it (the
    # polish is seeded from the anchor), in opposite directions: the anchor's
    # realized level error |-0.258| -> |-0.134| dB, the polish's |0.142| ->
    # |0.166| dB. That is what crossed them over. No filter moved.
    #
    # #2106 then collapsed the two pairs into one: the boost the ruling permits
    # (+3.72 dB at 399 Hz on the woofer here) reshapes the linearized branches
    # whose SUMMED ripple the scan minimizes, and for a while the minimum sat
    # exactly on the anchored seed, so the scan's walk was 0.000 dB.
    #
    # Moving the give-back into the trim's own band moved the seed again, and
    # the two pairs are two numbers once more: the scan walks +0.300 dB off the
    # anchor. The adjudication then commits the ANCHOR, and by the mechanism
    # this whole change is about — the band-matched give-back leaves the
    # anchored pair at a realized inter-driver level error of 0.000 dB (the
    # assertion above), against the scan's 0.300 dB, so the level instrument
    # scores the anchor better outright. Asserted below.
    resolved_trim_t, _ripple, _seed = real_solve(
        call["freqs"], call["w_tf"], call["t_tf"], FC_HZ,
        lo_hz=call["lo_hz"], hi_hz=call["hi_hz"],
        seed_trim_db=call["seed_trim_db"], trim_w_db=call["trim_w_db"],
        sign=call["sign"],
    )
    committed_t = c.candidate.role_attenuations_db["tweeter"]
    # The durable invariant, asserted first because it holds whichever way the
    # adjudication goes and on every fixture: what ships is a graded pair, and
    # the raw trim is not one of them.
    assert committed_t in (
        pytest.approx(expected_anchored["tweeter"]),
        pytest.approx(resolved_trim_t),
    )
    assert committed_t != pytest.approx(_FIXTURE_RAW_TRIM_DB["tweeter"])
    # …and the fixture-specific outcome, stated precisely rather than hedged, so
    # a future flip back is visible here rather than silent. The two pairs are
    # genuinely two numbers again (the scan walks +0.300 dB off its seed), so
    # this equality does discriminate between them: what ships is the anchor.
    #
    # WHICH pair the adjudication would pick when they DO differ is not this
    # test's claim and never was (see the paragraph above); its own pins are
    # `test_wild_trim_fallback_follows_levels_not_drift` and
    # `test_healthy_drivers_whose_declared_bands_cross_fc_are_not_refused`.
    # (NOT `test_eligible_candidate_fits_both_roles_and_moves_trim_toward_
    # ripple_optimal`, which #2138's review showed stays green when the
    # adjudication is severed.) What this test still pins, and what stays
    # is #1668's subject: the scan is SEEDED by the anchored give-back
    # (asserted on `seed_trim_db`/`trim_w_db` above) and what ships is never
    # the raw trim.
    #
    # This equality has been written both ways as the fixture moved: against
    # the anchor while the two pairs coincided, then against the scan after
    # deleting PR-L5's offset moved the anchor ~2.2 dB and the level
    # adjudication preferred the polish. Measuring the give-back in the trim's
    # own band moves it back to the anchor, because that give-back is what
    # makes the anchored pair the level-matched one. That is the adjudication
    # working, not a regression — and per the paragraph above, WHICH pair wins
    # is explicitly not this test's claim. What is asserted is the claim it
    # does make.
    assert committed_t == pytest.approx(expected_anchored["tweeter"])
    assert committed_t != pytest.approx(resolved_trim_t)
    assert committed_t != pytest.approx(_FIXTURE_RAW_TRIM_DB["tweeter"])
    assert committed_t in (
        pytest.approx(expected_anchored["tweeter"]),
        pytest.approx(resolved_trim_t),
    )


def test_linearized_ripple_polish_is_skipped_on_a_one_sided_band(caplog, monkeypatch):
    """PR-L3 review S1: the LINEARIZED ripple fine-tune carries the same
    one-sided-band hazard `program_analysis._build_candidate` guards, reached
    through the same ``overlap_band_hz`` clamp — and THIS is the call site
    whose result becomes ``role_attenuations_db``, the gain the emitted graph
    runs. With the tweeter swept from Fc the band is ``[Fc, 2*Fc]``, where the
    woofer is deep in its skirt and the summed ripple cannot express the
    handoff level. The scan must not run at all; the anchored give-back
    stands, and the skip is disclosed.

    **The realized verdict is SUPPLIED, for the same reason the sibling tests
    below supply theirs.** This harness never captures a summed at-the-mark
    baseline, so ``anchor_trims`` (single-datum-owner migration, #2609) always
    falls back to the raw measured trim — there is no owner in hand to place
    the pair any other way, and no dispute mechanism left to move it. The
    realized-level check is what decides whether that anchor ships, and it is
    supplied directly here rather than provoked, because provoking it is not
    this test's subject: the subject is that a one-sided band skips the scan
    and leaves the anchor standing, which is upstream of every level gate and
    is measured identically regardless of what the realized check says. Same
    reasoning, and the same mechanism, as
    ``test_large_raw_shift_is_accepted_by_the_guard_and_refused_by_the_level_
    check``, which supplies its own verdict for the same reason.
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    calls = []
    monkeypatch.setattr(
        iv, "solve_ripple_optimal_trim",
        lambda *a, **kw: calls.append(kw) or (kw["seed_trim_db"] - 4.0, 0.0, kw["seed_trim_db"]),
    )

    def _matched(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=0.0, difference_db=0.0,
            tolerance_db=3.0, matched=True,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    monkeypatch.setattr(iv, "realized_level_match", _matched)
    fakes = FakeSeams()
    # A defect inside the tweeter's OWN swept band (this conductor sweeps the
    # tweeter from Fc up), so the fit has real work to do and the candidate
    # clears item 2's gate.
    #
    # **Why the override below is still here is an OPEN QUESTION (#2073) — it
    # is NOT what this comment used to say.** The original rationale read: "the
    # shared fixture's bump sits at 1500 Hz — below Fc, i.e. outside this
    # geometry's tweeter band — so the fit barely moves and the session is
    # (correctly) refused …" Both halves stopped being true when R10a moved
    # that bump to +3 dB at 2400 Hz, which is ABOVE this conductor's Fc of
    # 1600 Hz, so it is INSIDE the tweeter's band: driving this setup with the
    # shared fixture and no override returns accepted, with the ripple scan
    # still correctly skipped (measured 2026-08-02, at that same R10a
    # revision). The override is left in place rather than repaired
    # because deciding whether it still earns its keep — its 8 dB at 2500 Hz is
    # a deeper defect than the shared 3 dB, and the give-back arithmetic below
    # is derived from the one-sided curve — is a design call, not a
    # transcription fix. #2073 carries it.
    _one_sided_tweeter_db = 8.0 * np.exp(
        -0.5 * ((np.log2(_LINEARIZABLE_FREQS_HZ / 2500.0) / 0.3) ** 2)
    )
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, tweeter_db=_one_sided_tweeter_db,
    )
    c = _one_sided_conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    assert calls == []  # the scan never ran
    assert "event=correction.crossover_v2_linearization_ripple_trim_skipped" in caplog.text
    assert "reason=ripple_band_one_sided" in caplog.text
    # The applied trim is the anchored give-back, untouched by any scan.
    # #1938: the raw trim has to be derived from THIS fixture's own curves —
    # the default woofer paired with the one-sided tweeter above — not from
    # _FIXTURE_RAW_TRIM_DB, which is solved from the DEFAULT tweeter and is a
    # different pair. Before this fix, `_eligible_measure_analysis` silently
    # defaulted to that mismatched constant too, and this assertion agreed
    # with it only because both sides shared the same wrong number.
    default_woofer_db, _default_tweeter_db = _fixture_branch_db()
    raw_trim = _solve_fixture_raw_trim(default_woofer_db, _one_sided_tweeter_db)
    # The give-back is measured in the band the TRIM is read in — the same
    # estimator and half-bands that solved ``raw_trim`` above — not over each
    # driver's own core band. ``_measured_level_frame`` re-measures it from the
    # fixture's own curves and this candidate's PUBLISHED filters, so the
    # expectation is derived from the same physics the planner saw rather than
    # read back out of it.
    frame = _measured_level_frame(c, tweeter_db=_one_sided_tweeter_db)
    giveback = frame.giveback_db
    # No summed capture in hand (see the docstring), so ``anchor_trims``
    # (single-datum-owner migration, #2609) falls back unconditionally to the
    # raw measured trim: the anchor is ``raw_trim + giveback``, with no third
    # term to add or exclude.
    unnormalized = {r: raw_trim[r] + giveback[r] for r in ("woofer", "tweeter")}
    shift = max(0.0, max(unnormalized.values()))
    for role in ("woofer", "tweeter"):
        assert c.candidate.role_attenuations_db[role] == pytest.approx(
            unnormalized[role] - shift
        )
    # With no scan to move it, the pair that ships IS the anchor — so the
    # property the band-matched give-back buys is directly observable on the
    # emitted gains: the two linearized branches hand off at the same level.
    # This is computed here rather than read off the plan because the realized
    # verdict is supplied by the stub above.
    assert abs(_inter_driver_level_error_db(
        frame, dict(c.candidate.role_attenuations_db)
    )) <= 1e-3
    # The magnitude, as a coarse guard on the fixture itself. It was -7.960 dB
    # while the anchor spent the CORE-band give-back; measuring that give-back
    # in the trim's own band instead moves this horn-shaped tweeter's anchor to
    # -4.918 dB — the same direction and roughly the same size as the jts3
    # correction this change was made for.
    assert c.candidate.role_attenuations_db["tweeter"] == pytest.approx(
        -4.918, abs=0.02
    )
    # ...and the guard never fired, because the trim never left the anchor.
    assert (
        "event=correction.crossover_v2_linearization_trim_rejected" not in caplog.text
    )


def test_straddling_band_still_runs_the_linearized_ripple_polish(caplog):
    """The control for the test above: the DEFAULT fixture's tweeter is swept
    from 300 Hz, so its overlap band straddles Fc and the polish still runs —
    the guard keys on the band, not on 'linearization is happening'."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert (
        "event=correction.crossover_v2_linearization_ripple_trim_skipped"
        not in caplog.text
    )


def test_linearization_giveback_ledger_carries_both_target_levels(caplog):
    """PR-L3 review S5: the give-back line carries each role's own
    ``target_level_db`` — ``raw_trim_db`` should track the negated difference
    of the two, and a large disagreement is the signature of a level defect
    like the one that shipped the 10 dB-dark tweeter. Mirrors the
    ``branch_level_match`` ledger pinned in
    tests/test_audio_measurement_program_analysis.py."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True

    assert "event=correction.crossover_v2_linearization_giveback" in caplog.text
    line = next(
        text for text in caplog.text.splitlines()
        if "event=correction.crossover_v2_linearization_giveback" in text
    )
    assert "target_level_db=" in line
    for role in ("woofer", "tweeter"):
        expected = round(
            float(c.candidate.linearization[role]["target_level_db"]), 3
        )
        assert f"'{role}': {expected}" in line


def test_analysis_json_round_trips_trim_band_average_db():
    """#1667 evidence round-trip: `_analysis_json`'s frozen fingerprint
    carries `trim_band_average_db` alongside the applied `trim_db`, rounded
    the same way, so replay/forensics can always compare the two — even
    when the candidate predates this field (`None` passthrough)."""
    freqs = np.linspace(100.0, 20000.0, 64)
    cand = CrossoverCandidate(
        trim_db={"woofer": 0.0, "tweeter": -0.0754},
        polarity="normal", delay_us=150.0,
        predicted_ripple_db=0.03, confidence=0.9,
        trim_band_average_db={"woofer": 0.0, "tweeter": -9.4754},
    )
    analysis = ProgramAnalysis(
        phase="measure", program_id="p1", locations=(),
        drift=DriftEstimate(
            epsilon_ppm=1.0, max_residual_samples=0.0,
            glitch_detected=False,
        ),
        alignment=_alignment(), candidate=cand,
        predicted_sum=(freqs, np.zeros_like(freqs)),
        glitch_detected=False,
    )
    evidence = _analysis_json(analysis)
    assert evidence["trim_db"] == {"woofer": 0.0, "tweeter": -0.0754}
    assert evidence["trim_band_average_db"] == {"woofer": 0.0, "tweeter": -9.4754}

    # Legacy/pre-#1667 construction site: candidate has no evidence field.
    legacy_cand = CrossoverCandidate(
        trim_db={"woofer": 0.0, "tweeter": -2.211}, polarity="normal",
        delay_us=150.0, predicted_ripple_db=0.8, confidence=0.8,
    )
    legacy_analysis = replace(analysis, candidate=legacy_cand)
    legacy_evidence = _analysis_json(legacy_analysis)
    assert legacy_evidence["trim_db"] == {"woofer": 0.0, "tweeter": -2.211}
    assert legacy_evidence["trim_band_average_db"] is None


def test_measure_diag_logs_trim_ripple_gain_db(caplog):
    """#1667 observability: the measure_diag line carries the
    applied-vs-band-average delta for the tweeter trim -- 0.0 when the
    ripple-optimal search left the trim exactly at its seed (or the sanity
    guard fell back to it), the actual recovery amount otherwise. `None`
    only when the candidate predates trim_band_average_db."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: replace(
        _measure_analysis(program),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.1, "tweeter": -0.5},
            polarity="normal", delay_us=150.0,
            predicted_ripple_db=0.03, confidence=0.8,
            trim_band_average_db={"woofer": -3.1, "tweeter": -9.5},
        ),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "trim_ripple_gain_db=9.0" in caplog.text  # -0.5 - (-9.5)
    caplog.clear()

    # No band-average evidence on this candidate (legacy/test construction
    # site) -> None, never a guess.
    fakes2 = FakeSeams()
    fakes2.measure = lambda program: _measure_analysis(program)
    c2 = _conductor(fakes2)
    _run_phase(c2, 1, 1)
    verdict2 = _run_phase(c2, 2, 2)
    assert verdict2["accepted"] is True
    assert "trim_ripple_gain_db=null" in caplog.text


def test_driver_class_by_role_ctor_param_threads_into_the_fit():
    """The driver_class_by_role ctor param (default None -> every role
    "unknown") was #1668 PR-C's forward-looking seam for #1665's
    component-entry declarations. #1665 has since landed
    (jasper.web.correction_crossover_v2.resolve_conductor_context is the
    production caller); this test pins the ctor-level wiring with a
    hand-typed override, and
    test_declared_driver_class_reaches_the_compose_envelope_seam below closes
    the other half by driving this SAME param from the resolver's real
    output."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes, driver_class_by_role={"tweeter": "compression_horn"})
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization["tweeter"]["driver_class"] == "compression_horn"
    # The woofer wasn't named in the override -> stays "unknown".
    assert c.candidate.linearization["woofer"]["driver_class"] == "unknown"


def test_declared_driver_class_reaches_the_compose_envelope_seam():
    """#1665: a design draft's declared driver_class, resolved by the REAL
    production helper (jasper.web.correction_crossover_v2's
    _resolve_driver_class_by_role — not a hand-typed literal), reaches
    compose_envelope through the exact ctor param the sibling test above
    proved works. Closes the seam #1668 PR-C's own test left open (its
    docstring said "no production caller populates it yet")."""
    from jasper.web.correction_crossover_v2 import _resolve_driver_class_by_role

    draft = {
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "model": "A"},
                {
                    "role": "tweeter",
                    "model": "B",
                    "driver_class": "compression_horn",
                },
            ],
            "crossover_candidates": [],
        },
    }
    driver_class_by_role = _resolve_driver_class_by_role(draft)
    assert driver_class_by_role == {"tweeter": "compression_horn"}

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes, driver_class_by_role=driver_class_by_role)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization["tweeter"]["driver_class"] == "compression_horn"
    assert c.candidate.linearization["woofer"]["driver_class"] == "unknown"


def test_large_raw_shift_is_accepted_by_the_guard_and_disclosed_by_the_level_check(
    caplog,
):
    """The two layers, on one fixture — guard pair (a) plus PR-L4 item 1.

    #1668 CD-horn re-anchor: the wild-trim guard is anchored to the
    ripple-optimal tweeter trim's OWN seed, NOT the raw candidate trim.

    **Since the single-datum-owner migration (#2609) the anchor's base is
    unconditional in this harness.** This file never captures a summed
    at-the-mark baseline, so ``anchor_trims`` always falls back to the raw
    measured trim — there is no owner in hand to place the pair any other way.
    The −20 dB raw trim therefore reaches the anchor untouched (−20.918 dB),
    the scan sits 9.700 dB away from it, and the wild-trim guard fires. The
    guard is still anchored to the seed and not to the raw trim; on this
    fixture those two are the same number, so nothing here can tell them apart
    any more. The claim survives, separably, on the DEFAULT fixture in
    ``test_wild_scan_drift_falls_back_to_anchored_pair_with_warning`` and
    ``test_a_rejected_scan_is_not_committed_however_well_it_levels``.

    What PR-L4 item 1 adds is the half the guard never had: a raw trim 20 dB
    away from what these branches justify is *invisible to drift from the
    anchor* — the anchor is the thing that is wrong — and the realized-level
    check SEES it. This is the 2026-07-27 failure shape in miniature, and
    (since #2609) item 1 is the only level check left to catch it.

    **It reports; it no longer refuses** (doctrine deviation (i)). The round
    proceeds and the candidate is published, carrying the disagreement as a
    banked finding and a journal line. That is the intended consequence of the
    demotion and this test is where it is visible end-to-end: the assertions
    below are inverted from what they were, not deleted.

    **What the demotion does NOT change, since this fixture is exactly where
    someone would look for it.** ``MIN_TRIM_SANITY_MARGIN_RATIO``'s ``M >= 2T``
    floor was argued from the gate REFUSING — see that constant for the
    restated version. The floor still earns its keep, because what it now
    guarantees is that a fallback big enough to matter is one the gate SAYS
    something about rather than one it is silent on. What bounds absolute
    loudness here is unchanged and is elsewhere: the trims are attenuations
    clamped non-positive, and the output limiters and volume rail sit
    downstream of every number in this test.

    **The realized verdict is supplied.** Item 1 grades the committed pair's
    realized inter-driver level; the −20 dB is a raw-trim INPUT the fit's
    anchor would otherwise repair on its own (giveback alone can bring a
    healthy pair back in range), so the realized instrument is held at "still
    mislevelled" here to keep the arm reachable. What this test is about is the
    guard, not item 1's own arithmetic.
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    far_raw_trim = {"woofer": 0.0, "tweeter": -20.0}
    fakes.measure = lambda program: _eligible_measure_analysis(program, trim_db=far_raw_trim)
    c = _conductor(fakes)

    def _still_mislevelled(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-20.0, difference_db=-20.0,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            iv, "realized_level_match", _still_mislevelled
        )
        _run_phase(c, 1, 1)
        # No CaptureBeginRefused: the round proceeds past the level check.
        _run_phase(c, 2, 2)
    assert LINEARIZATION_TRIM_SANITY_MARGIN_DB > 0  # the constant exists and is positive
    # The guard fires (see the docstring): the anchor carries the raw −20 dB
    # trim almost untouched — this fixture's level-band give-back is 0.917 dB,
    # so the anchor lands at −19.083 — and the scan sits 9.800 dB away.
    # Asserted with its drift, because "the guard fired" without the number is
    # the shape of telemetry nobody can check.
    #
    # (Was 9.700 against an anchor of −20.0 while the give-back was measured
    # over the driver's core band. The band-matched give-back moved this
    # fixture 0.917 dB and the drift with it; the guard's behaviour — fires,
    # commits the anchored pair, and lets item 1 grade it — is unchanged. Item 1
    # refused when that note was written; deviation (i) changed what item 1 does
    # with the pair, not what this guard does.)
    assert "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    assert "drift_db=9.8" in caplog.text
    assert "committed=anchored" in caplog.text
    # …and item 1's own realized-level check DISCLOSES the 20 dB it sees.
    assert "event=correction.crossover_v2_level_match_finding" in caplog.text
    assert "tolerance_db=3.0" in caplog.text
    assert "difference_db=-20.0" in caplog.text
    # The round proceeded: a candidate exists and was published, carrying the
    # finding. Inverted from the pre-demotion assertions on purpose — the
    # household gets a proposal plus the reservation, not silence.
    assert c.candidate is not None
    assert len(fakes.published_candidates) == 1
    assert fakes.banked_findings != []



def test_wild_scan_drift_falls_back_to_anchored_pair_with_warning(caplog, monkeypatch):
    """#1668 anchored give-back, guard pair (b): when the ripple-optimal tweeter
    scan drifts implausibly far from the ANCHOR, the guard fires and the
    conductor falls back to the ANCHORED pair — NOT the raw trim (raw trim +
    emitted filters is the known VERIFY-mismatch class). Crafting a scan that
    walks that far against a synthetic fixture is awkward, so the ripple-optimal
    solve is monkeypatched to return a far-from-anchor trim.

    PR-L4 item 9: the fallback is no longer chosen by drift alone. The event now
    carries both candidate pairs' realized level errors and which one was
    committed, and the anchor wins HERE because it levels better — which is what
    the guard was always assuming and never checking.
    """
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)

    captured: dict = {}

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        # Force the resolved tweeter trim 20 dB below the anchored seed.
        return kwargs["seed_trim_db"] - 20.0, 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    # Committed the ANCHORED pair, NOT the raw trim and NOT the wild scan value.
    committed = c.candidate.role_attenuations_db
    assert set(committed) == {"woofer", "tweeter"}
    assert committed["woofer"] == pytest.approx(captured["trim_w_db"])
    assert committed["tweeter"] == pytest.approx(captured["seed_trim_db"])
    assert committed != dict(_FIXTURE_RAW_TRIM_DB)
    assert "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    assert "anchored_trim_db=" in caplog.text
    assert "fallback_trim_db=" in caplog.text
    # PR-L4 item 9: the rejection names WHY this pair won, in levels.
    assert "committed=anchored" in caplog.text
    assert "anchored_level_error_db=" in caplog.text
    assert "resolved_level_error_db=" in caplog.text
    # linearization itself still gets reported — only the trim falls back.
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}


def test_a_rejected_scan_is_not_committed_however_well_it_levels(caplog, monkeypatch):
    """#2291's second acceptance criterion, at the conductor.

    Beyond the sanity margin the scan is REJECTED and the level-preserving
    anchor is committed — even when the scan levels better, which is exactly
    the case this fixture constructs (scan 0.2 dB, anchor 2.5 dB).

    **This assertion is inverted from what it pinned before #2291 Phase 2b**,
    and the inversion is the product. PR-L4 item 9 had the guard commit
    whichever pair levelled better *whether or not it had just been rejected*,
    on the 2026-07-27 evidence that drift alone points the wrong way: a scan
    that had walked 5.500 dB was walking TOWARD a correct level. #2291 is the
    later ruling — a guard whose rejection is telemetry is not a guard, and on
    2026-08-10 that policy shipped a −13.013 dB tweeter trim under the word
    "rejected". What replaces the old behaviour is not a blind fallback: the
    anchor is level-preserving by construction, and the committed pair still
    faces the realized-level assertion, so a badly-levelled anchor produces a
    refusal rather than a hot speaker.

    The graded level errors are still COMPUTED and still disclosed — the guard
    did not stop measuring, it stopped letting the measurement overrule the
    rejection — which is what the two ``*_level_error_db`` assertions below
    read.

    **Why the level verdicts are supplied rather than provoked (PR-L5).** This
    test used to drive the anchor mislevelled with a 12 dB-dark raw trim. That
    lever is gone, and gone on purpose: the shared level frame makes the anchor
    ``give-back + system_level − core_level``, in which the raw trim cancels
    out of every branch's level RELATIVE to the others — a dark raw trim can no
    longer mislevel the anchored pair, and one 12 dB off is refused as a frame
    disagreement long before this branch. That is the ladder working. What
    remains worth pinning is the guard's DECISION, so the two level verdicts
    are supplied directly and the physical scenario that used to produce them
    is left retired.
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)

    seed: dict[str, float] = {}

    def _scan(*_a, **k):
        # 7 dB BELOW the anchor: past the 6 dB margin (so the guard fires) and
        # still a legal attenuation — the candidate refuses a positive trim
        # outright, and a bigger walk would fail the prediction gate downstream
        # on a fixture whose subject is the guard, not the gate.
        seed["tweeter"] = k["seed_trim_db"]
        return k["seed_trim_db"] - 7.0, 0.0, k["seed_trim_db"]

    def _match(_freqs, _w, _t, _fc_hz, trims_db, _woofer_role, tweeter_role, **_kw):
        # The SCANNED pair levels well; the anchor's does not. Both inside the
        # assertion tolerance, so the session lives and the committed pair is
        # what this test can read.
        scanned = trims_db[tweeter_role] < seed["tweeter"] - 3.0
        difference = 0.2 if scanned else 2.5
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=difference, difference_db=difference,
            tolerance_db=3.0, matched=True,
            woofer_band_hz=(1000.0, 2000.0), tweeter_band_hz=(2000.0, 4000.0),
        )

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _scan)
    monkeypatch.setattr(
        iv, "realized_level_match", _match,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    # The session now COMPLETES, and that is a consequence of the fix rather
    # than a relaxation. Item 2 refused this fixture before #2291 Phase 2b
    # because the committed pair WAS the 7 dB-drifted scan, which measures
    # worse than its own baseline (see the swept table below: −1.524 dB at
    # drift 7). Rejecting the scan commits the anchor, which is the drift-0 row
    # — +0.657 dB, comfortably over the floor. The gate did not stop
    # discriminating; it stopped being handed a mistrim to catch.
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    # The guard FIRED (drift 7 dB > the 6 dB margin) and committed the ANCHOR,
    # although the scan levels better. Both level errors are still measured and
    # still disclosed, which is what makes the rejection auditable rather than
    # merely stated.
    assert "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    assert "committed=anchored" in caplog.text
    assert "committed=resolved" not in caplog.text
    assert "strategy=anchored_committed_after_sanity_drift" in caplog.text
    assert "anchored_level_error_db=2.5" in caplog.text
    assert "resolved_level_error_db=0.2" in caplog.text
    # **The swept drift table this fixture's verdicts come from** (R10a, #1817),
    # kept because it is what makes the acceptance above readable. Measured by
    # sweeping the forced drift and reading
    # ``event=correction.crossover_v2_prediction_gate`` with the pre-2b policy,
    # so every row is the COMMITTED SCAN being graded (baseline 0.957 dB rms in
    # every row; the floor is 0.5 dB):
    #
    #   drift dB     0      1      2      3       4       5       6      7      8
    #   improve  +0.657 +0.657 +0.657 +0.657  -0.324  -0.688  -1.087 -1.524 -1.998
    #   verdict   accept accept accept accept  refuse  refuse  refuse refuse refuse
    #
    # The gate DISCRIMINATES on this fixture: a correct trim ships, a mistrim of
    # 4 dB or more is caught as the regression it is. Under the flat target it
    # refused at every drift including 0.0 (-0.293 dB), because the fit's own
    # crossover-fighting cuts made even an untouched trim fail to beat its
    # baseline — the gate could not tell a wild trim from a good one.
    #
    # The last two columns are what #2291 removed from the shipping path: past
    # the 6.0 dB margin the pair no longer reaches this gate at all, because it
    # is no longer the committed pair. The gate stays as the backstop for the
    # 0-6 dB band, where a scan is still trusted to polish.


def test_anchored_trim_is_raw_plus_giveback_and_normalized_non_positive():
    """#1668 anchored give-back, the core math: each role's committed trim is
    its raw trim plus that branch's own measured LEVEL-BAND give-back, with a
    shared shift applied so no role lands POSITIVE (a boost the emitter would
    refuse). Pinned end-to-end against the conductor's committed trims.

    **This test was INERT until 2026-08-19, and how it was inert is the useful
    part.** It computed its expectation from ``correction_giveback_db`` — the
    core-band number that no longer places the trim — and then compared the
    tweeter against it under ``LINEARIZATION_TRIM_SANITY_MARGIN_DB``, a **6.0 dB**
    tolerance. The two RULES' committed anchors differ by 1.835 dB on this
    fixture (−2.691 core-band against −0.856 level-band; the 0.918 dB figure is
    the give-back *differential*, which is a different quantity and not what
    this assertion compared). Either way both sit far inside 6.0, so the
    tolerance swallowed the whole difference and the test passed on BOTH sides
    of the band change: mutating the production anchor did not move it. Its
    woofer leg was degenerate too (raw 0.0, shift equal to the woofer's own
    give-back, so it asserted 0.0 == 0.0 whatever the give-back was).

    It now reads the same frame production does (``_measured_level_frame``,
    shared with this file's other anchor tests) and grades to 1e-9, so the
    arithmetic is actually pinned. The 6.0 dB constant it used to lean on is a
    SCAN-drift guard and was never a tolerance on the anchor's own math.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    frame = _measured_level_frame(c)
    giveback = frame.giveback_db
    # Every branch that emitted filters reports a positive give-back.
    assert giveback["tweeter"] > 0.0
    # ``anchor_trims`` (single-datum-owner migration, #2609) has no summed
    # capture in hand in this harness, so it places the pair on the raw
    # measured trim alone: the anchor is ``raw_trim + giveback``, with no
    # third term.
    unnormalized = {
        r: raw_trim[r] + giveback[r] for r in ("woofer", "tweeter")
    }
    shift = max(0.0, max(unnormalized.values()))
    anchored = {r: v - shift for r, v in unnormalized.items()}

    committed = c.candidate.role_attenuations_db
    # No committed trim is a boost. The hearing-safety invariant.
    assert all(v <= 1e-9 for v in committed.values())
    # Both roles are committed at their anchor exactly on this fixture — the
    # scan walks off it and the level adjudication commits the anchor.
    assert committed["woofer"] == pytest.approx(anchored["woofer"], abs=1e-9)
    assert committed["tweeter"] == pytest.approx(anchored["tweeter"], abs=1e-9)
    # And the property the arithmetic exists to produce, asserted independently
    # of it: the committed pair hands the two branches off at the same level.
    assert abs(_inter_driver_level_error_db(frame, committed)) <= 1e-3


def test_anchored_normalization_shift_prevents_a_positive_trim(monkeypatch):
    """The normalize step: when a branch's own give-back exceeds its raw
    attenuation the unnormalized anchor would be POSITIVE; the shared shift must
    pull every role non-positive while preserving their RELATIVE leveling.

    **The raw-trim override this test used to carry is gone (R10a, #1817), and
    re-deriving it is what showed it had never done anything.** It forced
    ``{"woofer": 0.0, "tweeter": 0.0}`` on the reasoning that "any positive
    give-back pushes the unnormalized anchor above 0 and forces the shift" —
    but the raw trim always cancelled out of the frame-offset term that used
    to ride the same anchor, so overriding it changed nothing about the shift.
    Since the single-datum-owner migration (#2609) that term is gone outright:
    this harness has no summed capture in hand, so the anchor is
    unconditionally ``raw_trim + giveback``, and the raw trim is once again the
    ordinary input it always looked like.

    What the override DID do was starve a gate this test is not about. It moves
    the RAW predicted sum, which is item 2's baseline, so a zeroed trim leaves
    less than the 0.5 dB of headroom the improvement floor needs. Same sweep,
    reading ``event=correction.crossover_v2_prediction_gate`` (``after`` is
    0.300 dB rms in every row):

        raw tweeter trim dB   0.0    -0.5   -1.0   -1.5  -1.773   -2.0   -3.0
        baseline rms dB     0.647   0.708  0.792  0.895   0.957  1.012  1.284
        improvement dB      0.347   0.408  0.492  0.595   0.657  0.712  0.984
        ledger verdict      under   under  under   over    over   over   over

    (The three left-hand columns REFUSED when this table was measured; since
    the nanny burn-down the same rows bank ``not_an_improvement`` and the
    round proceeds. The arithmetic is unchanged, which is what the table is
    about.)

    So the honest value for a fixture field nobody had derived is: don't
    override it. Using ``_FIXTURE_RAW_TRIM_DB`` — solved from the same branch
    curves the conductor is handed — keeps this test's subject bit-for-bit and
    stops it riding a floor it has nothing to say about.
    """

    def _spy(*args, **kwargs):
        # Commit the anchor itself (no scan drift) so the committed pair is the
        # normalized anchor verbatim.
        return kwargs["seed_trim_db"], 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    # The anchor's give-back is measured over the bands the trim is read in,
    # not over each driver's own core band; ``_measured_level_frame``
    # re-measures it from the fixture's own curves and the candidate's
    # PUBLISHED filters. The premise this test needs — a woofer give-back that
    # exceeds its raw attenuation — is asserted below rather than assumed, so a
    # band that stopped producing one would fail here loudly.
    giveback = _measured_level_frame(c).giveback_db
    # No summed capture in hand, so the anchor is unconditionally
    # ``raw_trim + giveback`` (single-datum-owner migration, #2609) — no
    # third term to add.
    unnormalized = {
        r: raw_trim[r] + giveback[r] for r in ("woofer", "tweeter")
    }
    # The premise this test is built on: the woofer's own give-back exceeds
    # its raw attenuation (0.0 dB), so its unnormalized anchor is a BOOST the
    # emitter would refuse.
    assert giveback["woofer"] > -raw_trim["woofer"]
    assert unnormalized["woofer"] > 0.0
    assert max(unnormalized.values()) > 0.0, "fixture must actually need the shift"
    shift = max(unnormalized.values())
    expected = {r: v - shift for r, v in unnormalized.items()}

    committed = c.candidate.role_attenuations_db
    assert all(v <= 1e-9 for v in committed.values())  # nothing became a boost
    assert committed["woofer"] == pytest.approx(expected["woofer"])
    assert committed["tweeter"] == pytest.approx(expected["tweeter"])
    # Relative leveling preserved exactly by the shared shift.
    assert (committed["tweeter"] - committed["woofer"]) == pytest.approx(
        unnormalized["tweeter"] - unnormalized["woofer"]
    )


def test_wild_trim_boundary_exact_passes_just_above_falls_back(caplog, monkeypatch):
    """The sanity margin is an exclusive upper bound (matches this file's other
    boundary comparators): a seed drift EXACTLY at the margin is trusted, one
    hair over trips the guard. Seed-anchored (#1668), so the ripple-optimal
    solve is monkeypatched to return a controlled distance from its own seed.

    Pinned on the guard's OWN event rather than on the committed trim: since
    PR-L4 the trim a session ends up carrying is the joint outcome of this
    boundary AND the realized-level comparison (item 9) AND the publish-time
    assertion (item 1) — three decisions, and reading the trim alone could not
    tell which one moved. A drift of exactly 6.0 dB IS trusted here, and the
    resulting 6 dB-mislevelled pair is then refused downstream: the guard's
    bound and the accountability gate are different questions, deliberately.
    """

    def _run_at(drift_db: float):
        caplog.clear()
        monkeypatch.setattr(
            iv, "solve_ripple_optimal_trim",
            lambda *a, **k: (k["seed_trim_db"] - drift_db, 0.0, k["seed_trim_db"]),
        )
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        c = _conductor(fakes)
        _run_phase(c, 1, 1)
        try:
            _run_phase(c, 2, 2)
        except CaptureBeginRefused:
            pass  # the level gate's verdict; this test is about the guard's
        return "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    assert _run_at(LINEARIZATION_TRIM_SANITY_MARGIN_DB) is False
    assert _run_at(LINEARIZATION_TRIM_SANITY_MARGIN_DB + 0.5) is True


# --------------------------------------------------------------------------- #
# PR-L4 item 2 — spec-grade the prediction before auto-apply
# --------------------------------------------------------------------------- #


def test_predicted_spec_report_is_graded_on_the_shared_analysis_grid():
    """``spec_report_for_predicted_sum`` decimates before it smooths.

    Not cosmetic. ``smooth_fractional_octave`` is an O(bins x window) Python
    loop — ~11 s on a laptop at a raw 512k-point prediction grid, worse on a
    Pi 5 — and this runs at the confirm seam with a household waiting on the
    apply. It block-averages onto ``MAX_ANALYSIS_BINS`` first, the bound the
    combiner already adopted for the same reason, which is also what puts the
    predicted curve at the same grid density as the measured one it is compared
    against."""
    from jasper.audio_measurement.spatial_combine import MAX_ANALYSIS_BINS

    freqs = np.fft.rfftfreq(1 << 16, 1.0 / 48000.0)
    assert freqs.size > MAX_ANALYSIS_BINS  # the fixture must exercise the bound
    report = spec_report_for_predicted_sum((freqs, np.zeros(freqs.size)))

    assert report is not None
    graded_bins = sum(band.n_bins for band in report.bands)
    assert 0 < graded_bins <= MAX_ANALYSIS_BINS
    # A flat curve is flat at any grid density.
    assert report.overall_passed is True


def test_predicted_spec_report_is_unknown_never_a_pass_on_bad_input():
    """``None`` in, ``None`` out — and a malformed pair degrades the same way
    rather than raising into the confirm seam. The caller must read that as
    "no evidence", which the gate test below pins."""
    assert spec_report_for_predicted_sum(None) is None
    assert spec_report_for_predicted_sum((np.array([]), np.array([]))) is None
    assert spec_report_for_predicted_sum(("not", "arrays")) is None


def test_prediction_gate_allows_a_materially_better_correction():
    """The happy path, with the arithmetic shown rather than assumed: the
    fixture's RAW two-branch model and its LINEARIZED one are far enough apart
    that the gate passes, and the session applies."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(c)

    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.candidate is not None

    before_rms_db, after_rms_db = _gate_residuals(c)
    assert (before_rms_db - after_rms_db) >= PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB


@pytest.mark.parametrize("pre_apply_scale", [0.4, 1.0, 2.5])
def test_prediction_gate_verdict_does_not_depend_on_the_room(pre_apply_scale):
    """PR-L4 review B1, the regression that motivated the frame change.

    The first cut compared the model's residual against the MEASURED in-room
    cloud's, which made the verdict a function of the ROOM: holding the
    correction constant and varying only the pre-apply measurement flipped a
    passing session into the gate's failing arm (a refusal at the time; a
    ``not_an_improvement`` ledger entry since the nanny burn-down), and every
    BETTER room fared worse. Both of the gate's terms are now the same instrument
    at the same position, so scaling the room's own measured response — the
    only thing this parametrization changes — must not move the verdict at
    all."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    scaled = _in_room_summed_db() * pre_apply_scale
    fakes.verify = lambda program: _verify_analysis(program, summed_db=scaled)
    c = _cloud_conductor(fakes)

    verdict = _walk_measure_cloud_to_close(c)
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.candidate is not None
    # ...and the room really did move, so this is not a no-op fixture.
    measured_rms_db = c.group_cloud_result(PHASE_CLOUD_MEASURE)["flatness"]["rms_db"]
    assert measured_rms_db == pytest.approx(
        _ROOM_SCALE_EXPECTED_RMS_DB[pre_apply_scale], abs=0.05
    )


def test_prediction_gate_banks_a_correction_that_does_not_improve_and_proceeds(caplog):
    """PR-L4 item 2, after the nanny burn-down (doctrine deviation (c)).

    This used to REFUSE at the confirm seam and leave the speaker untouched.
    It was a forecast vetoing the measurement that would have settled the
    question — it took jts3's first prescribed-boost round on 2026-08-22 with
    it — and the doctrine's authority model puts a prediction on the proposing
    side of that line. So the gate now banks its verdict and the round
    proceeds: the ledger says ``not_an_improvement``, the household is not
    told its speaker was left alone, and what decides the correction's fate is
    the measured round that follows.

    **Mutation guard.** Restoring the veto makes `_walk_measure_cloud_to_close`
    raise ``CaptureBeginRefused`` and fails the first assertion here.

    Driven through the REAL threshold by a realistic bad correction — a driver
    pair whose fit cannot help, so the linearized model lands essentially on top
    of the raw one (PR-L4 review: the previous version monkeypatched the
    threshold to 100 dB, which proved the arithmetic ran and nothing about
    whether the shipped number does anything).

    **The fixture changed with PR-L5, because its old subject did.** It used to
    be a broad woofer-only suckout, "structurally unable to correct" on the
    reasoning that everything around it would have to come DOWN. Both halves of
    that stopped being true: boost can now fill a suckout, and the shared level
    frame repairs the inter-driver level error a woofer-only defect creates. A
    dense comb replaces it, and it is un-correctable for a reason no later PR
    can quietly undo — there are far more notches than the 8-filter budget, and
    chasing comb structure is precisely what the null doctrine forbids. It is
    put in BOTH drivers so the frame has nothing to fix either.

    **The comb got denser and deeper with #1809**, for a reason worth keeping
    on the record: at 6 dB / 3 cycles per octave the correction USED to be a
    regression only because the fit was boosting inside each driver's own
    crossover stopband, and each branch's stopband is the other's passband —
    so the two stopband boosts stacked in the summed prediction. Bound the lift
    to each driver's radiating band and that shape's correction becomes a
    genuine improvement (it now lands in spec). At 9 dB / 5 cycles per octave
    the comb is un-correctable on its own merits — ~35 notches against an
    8-filter budget — and the ledger reads a 0.001 dB improvement."""
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    freqs = _LINEARIZABLE_FREQS_HZ
    comb_db = 9.0 * np.sin(2.0 * np.pi * np.log2(freqs / 200.0) * 5.0)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=comb_db, tweeter_db=comb_db,
    )
    c = _cloud_conductor(fakes)

    verdict = _walk_measure_cloud_to_close(c)

    # No refusal, and the round produced a real candidate to measure.
    assert verdict["candidate_fingerprint"]
    assert c.candidate is not None
    assert c.last_failure_code is None
    # The verdict the forecast reached is on the record, at WARNING, with the
    # numbers a reader needs to weigh it.
    assert "reason=not_an_improvement" in caplog.text
    assert "required_db=0.5" in caplog.text
    assert "improvement_db=" in caplog.text


def test_prediction_gate_tolerance_is_the_models_own_tracking_error():
    """The third tolerance's derivation, pinned like its two siblings (PR-L4
    review: it was the only one without a test).

    Since B1 made both terms the same instrument, the comparison carries no
    measurement noise — so the threshold is a product-policy floor, and the
    floor is the gap between what the model predicts and what the hardware
    realizes. ``_fit_linearization`` records that as ~0.5 dB for the complex
    correction model on JTS3. An improvement smaller than the model's own
    tracking error is not one we can honestly claim."""
    complex_model_tracking_error_db = 0.5
    assert PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB == complex_model_tracking_error_db
    # And well under the zero-phase model it replaced (~2.0 dB), which is the
    # regime where "improvement" would have been indistinguishable from noise.
    assert PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB < 2.0


def test_prediction_gate_is_silent_when_the_prediction_meets_the_spec(monkeypatch):
    """A prediction that passes the spec needs no improvement argument — and
    must not be gated on one, or the flattest speakers would be refused
    hardest. Pinned with an absurd threshold so only the early return can
    explain the pass."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    monkeypatch.setattr(flow_mod, "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB", 100.0)
    monkeypatch.setattr(
        flow_mod, "spec_report_for_predicted_sum",
        lambda predicted_sum: evaluate_flat_spec(
            _SUMMED_FREQS_HZ, np.zeros(_SUMMED_FREQS_HZ.size),
        ),
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]


def test_prediction_gate_treats_an_ungradeable_prediction_as_unknown(monkeypatch):
    """An absent report is the gate having no evidence to refuse on — never a
    pass being granted, and never a refusal manufactured out of a missing
    number. Same unknown-vs-zero discipline as every other honesty instrument
    in this flow."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    monkeypatch.setattr(flow_mod, "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB", 100.0)
    monkeypatch.setattr(flow_mod, "spec_report_for_predicted_sum", lambda _s: None)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]


def test_prediction_gate_abstains_when_no_fit_ran(caplog, monkeypatch):
    """The trims-only lane has no before/after to compare.

    When linearization is ineligible (or SF2 caught a fit failure), the
    LINEARIZED prediction IS ``analysis.predicted_sum`` — the same object — so
    the two terms are identical and the improvement is exactly 0. Refusing on
    that would kill every trims-only candidate on the strength of arithmetic
    rather than evidence, so the gate abstains and says which path it took."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    monkeypatch.setattr(flow_mod, "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB", 100.0)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="consumer",  # ineligible ⇒ no fit ⇒ no linearized sum
    )
    c = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(c)
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.candidate.linearization == {}
    assert "reason=no_linearization" in caplog.text


def test_prediction_gate_logs_a_ledger_line_on_every_path(caplog):
    """PR-L4 review S4: the gate speaks whether or not it refuses, mirroring
    item 1's own ledger. A gate that is silent on success makes "it passed" and
    "it never ran" indistinguishable in the journal — the first question a
    field diagnosis of a dark speaker would ask."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]

    assert "event=correction.crossover_v2_prediction_gate" in caplog.text
    # PR-L5 moved this fixture's OUTCOME, not the ledger's contract: the shared
    # level frame flattens the default pair enough that its predicted sum now
    # meets the spec outright, which is the gate's ``predicted_in_spec`` early
    # return rather than its ``improved`` one. The claim under test — that the
    # gate speaks on every path — is what this asserts, and it is stronger for
    # covering an early-return path.
    assert "reason=predicted_in_spec" in caplog.text
    # The terms the taken path can honestly report are on the line, so the
    # verdict is re-derivable from the journal alone.
    for ledger_field in ("after_rms_db=", "required_db="):
        assert ledger_field in caplog.text


def test_the_stashed_prediction_verdict_is_the_full_resolution_grade():
    """Two-stage commission D4, the "one grading instrument" pin.

    The verdict the conductor holds for the host to persist must be the grade
    of the FULL-RESOLUTION prediction — the same tuple the accountability seam
    grades — and not a re-grade of what survives persistence. This asserts
    the identity AND that the identity is a real constraint: the 512-point
    ``_decimate_sum`` reduction is demonstrably a different instrument, grading
    45/154/206 bins per band where the full 2048-point curve grades
    180/617/823 (re-derived post-#1858: before that fix's block-average,
    ``_decimate_sum`` was a raw stride and graded 45/155/205 on this same
    fixture — the two differ by one bin in two bands because a block-average
    output point sits at its block's mean frequency rather than the block's
    first raw bin, not because the instruments-differ claim below changed).
    Two reports built from those two inputs can disagree on a narrow band,
    and the screen this feeds exists to state one honest spec verdict."""
    from jasper.web.correction_crossover_v2 import _decimate_sum

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]

    report = c.measure_predicted_spec_report
    assert report is not None
    stashed = dict(report)
    comparison = stashed.pop("comparison")
    assert comparison["reason"] == "predicted_in_spec"
    # It IS the full-resolution grade.
    assert stashed == spec_report_for_predicted_sum(c.measure_predicted_sum).to_dict()

    # ...and the thing it is NOT is reachable, so the assertion above is not
    # satisfied by the two instruments happening to agree.
    decimated = _decimate_sum(c.measure_predicted_sum)
    assert len(decimated["freqs_hz"]) < c.measure_predicted_sum[0].size
    re_graded = spec_report_for_predicted_sum((
        np.asarray(decimated["freqs_hz"], dtype=float),
        np.asarray(decimated["magnitude_db"], dtype=float),
    )).to_dict()
    assert re_graded != stashed
    assert [b["n_bins"] for b in re_graded["bands"]] != [
        b["n_bins"] for b in stashed["bands"]
    ]


def test_the_prediction_verdict_is_stashed_on_the_trims_only_lane_too():
    """The hoist above the trims-only abstain, pinned.

    A candidate with no linearization still commits trims and still predicts a
    response, so it HAS a gradeable prediction — and the gate's own abstain
    (which is about having no before/after to COMPARE) must not be what decides
    whether the household is shown a verdict. Before D4 the grade sat below
    that abstain and this lane reached the wire with no verdict at all, which
    would have rendered "we could not predict this" over a prediction we can
    grade."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="consumer",  # ineligible ⇒ no fit ⇒ no linearized sum
    )
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]

    assert c.candidate.linearization == {}
    report = c.measure_predicted_spec_report
    assert report is not None
    stashed = dict(report)
    assert stashed.pop("comparison")["reason"] == "no_linearization"
    assert stashed == spec_report_for_predicted_sum(c.measure_predicted_sum).to_dict()


def test_the_gates_ledger_and_the_stashed_verdict_never_disagree(caplog):
    """One session, one prediction, one verdict — on both surfaces.

    The trims-only ledger line carries the after-report the hoist produces, so
    a field read of the journal and a read of ``/state`` cannot state different
    things about the same prediction. (The gate's DECISION is still recorded
    separately, by ``reason=no_linearization``.)"""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="consumer",
    )
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    assert "reason=no_linearization" in caplog.text
    report = spec_report_for_predicted_sum(c.measure_predicted_sum)
    stashed = dict(c.measure_predicted_spec_report)
    assert stashed.pop("comparison")["reason"] == "no_linearization"
    assert report.to_dict() == stashed
    # ``log_event`` renders booleans JSON-style, so compare in its vocabulary
    # rather than Python's.
    assert f"after_passed={'true' if report.overall_passed else 'false'}" in caplog.text
    rms_db = round(float(spec_convergence_residual(report).rms_db), 3)
    assert f"after_rms_db={rms_db}" in caplog.text


def test_an_ungradeable_prediction_stashes_none_and_names_itself(caplog, monkeypatch):
    """D4's ``None`` propagation and its named log line.

    An absent report is a user-visible dead end — the review screen renders "we
    could not predict this" and refuses Apply on it — so it gets a line
    somebody can grep for, carrying WHICH of the two causes fired. ``None``
    must never be papered over into a fabricated verdict."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    monkeypatch.setattr(flow_mod, "spec_report_for_predicted_sum", lambda _s: None)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    # Unknown is not a refusal: the session still completes (the gate has no
    # evidence to refuse on), it just carries no verdict.
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]

    assert c.measure_predicted_spec_report is None
    assert "event=correction.crossover_v2_prediction_ungradeable" in caplog.text
    # The prediction existed; the evaluator is what refused it.
    assert "why=evaluator_refused" in caplog.text
    assert "why=no_prediction" not in caplog.text


def test_an_absent_prediction_names_the_other_cause(caplog):
    """The second ``why``: nothing was predicted at all, so there was never a
    curve to grade. Separated from the evaluator's refusal because the two have
    different remedies and collapsing them would make the line unactionable.

    Reached without monkeypatching the evaluator — an analysis that carries no
    ``predicted_sum`` on the trims-only lane (nothing overrides it there) is the
    real shape of this cause."""
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: dataclasses.replace(
        _eligible_measure_analysis(program, mic_tier="consumer"),
        predicted_sum=None,
    )
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    assert c.measure_predicted_sum is None
    assert c.measure_predicted_spec_report is None
    assert "why=no_prediction" in caplog.text
    assert "why=evaluator_refused" not in caplog.text


def test_an_accountability_gate_no_longer_stamps_a_failure_code():
    """The accountability gate has no refusal left to name to the host.

    This test used to assert the opposite — that item 1's refusal reached the
    household as ``driver_levels_disagree`` rather than as a manufactured
    ``relay_timeout``. The realized-level demotion (doctrine deviation (i))
    removed the refusal, so the correct assertion is the inverse: the same
    fixture that used to raise now completes with no failure code stamped at
    all. Kept rather than deleted because ``last_failure_code`` staying ``None``
    is exactly what a reader needs to see to know the round really did proceed.

    The realized verdict is supplied for the reason its sibling above gives:
    since the #1866 ruling a frame disagreement banks a finding and proceeds,
    so a mislevelled pair has to be handed to the gate rather than provoked."""
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    fakes = FakeSeams()
    far_raw_trim = {"woofer": 0.0, "tweeter": -20.0}
    fakes.measure = lambda program: _eligible_measure_analysis(program, trim_db=far_raw_trim)
    c = _conductor(fakes)

    def _still_mislevelled(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-20.0, difference_db=-20.0,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            iv, "realized_level_match", _still_mislevelled
        )
        _run_phase(c, 1, 1)
        assert c.last_failure_code is None
        _run_phase(c, 2, 2)
    assert c.last_failure_code is None
    assert c.last_failure_code != REASON_RELAY_TIMEOUT


def test_the_accountability_reasons_are_gone_from_the_registry():
    """No refusal, no registry row — the nanny burn-down's own bookkeeping.

    A registry row for a refusal nothing raises is copy that can never be read,
    and leaving one behind is how a veto comes back quietly: the row is the
    thing a future change would reach for. Item 2's two went with deviation
    (c); item 1's ``driver_levels_disagree`` went with deviation (i). All three
    absences are asserted together because they are one rule applied three
    times.

    A durable state persisted before either change can still carry these
    literals, and ``_failure_history_note`` reads the registry with ``.get``,
    so an old code with no row degrades to the generic clause rather than
    raising — which is why deleting the row is safe as well as correct.
    """
    assert "driver_levels_disagree" not in REASON_REGISTRY
    assert "correction_not_an_improvement" not in REASON_REGISTRY
    assert "prescribed_correction_not_an_improvement" not in REASON_REGISTRY
    assert "driver_levels_disagree" not in TRANSIENT_AUTO_RETRY_CODES


# --------------------------------------------------------------------------- #
# SF2 / SF3 (adversarial review, 2026-07-24 — #1668 PR-C review)
# --------------------------------------------------------------------------- #
#
# SF2: an eligible speaker whose fit engine raises must degrade EXACTLY to
# the ineligible path (raw trim, empty linearization) -- never fail the
# whole MEASURE accept. SF3: crossover_v2_measure_diag's new
# `linearization=` field names which of the five outcomes this attempt's
# candidate build took, for corpus-review greppability.


def test_fit_engine_bug_falls_back_to_raw_trim_with_warning(caplog, monkeypatch):
    """SF2: an eligible pair (reference tier, both paired N>=3) whose fit
    call raises must behave EXACTLY like an ineligible one -- raw trim,
    empty linearization dict, MEASURE still accepted -- never propagate and
    fail the whole accept over a bug in the fit engine."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)

    def _boom(analysis, cand, cloud=None, **_kw):
        raise ValueError("simulated fit engine bug")

    monkeypatch.setattr(c, "_plan_linearization", _boom)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}
    assert c.candidate.linearization_outcome == "fit_failed"
    # Anchored to the SAME record two ways: startswith() rather than a bare
    # `in caplog.text` substring search (the journal_dropped line's own
    # `dropped_event=` field ends in "event=", so a substring search would
    # also match a drop of this same event), and the `reason=` check reads
    # off that specific record rather than the whole caplog blob, so a drop
    # line whose port also raised ValueError could not satisfy both
    # assertions the way two independent bare-substring checks could (#2368).
    fit_failed_lines = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith(f"event={planning.EVENT_FIT_FAILED} ")
    ]
    assert fit_failed_lines, "the fit_failed event was never said"
    assert "reason=ValueError" in fit_failed_lines[0]
    assert "linearization=fit_failed" in caplog.text


def test_cut_only_invariant_violation_falls_back_instead_of_crashing(caplog, monkeypatch):
    """N1 x SF2 interaction: linearization_fit.fit_driver_linearization's own
    cut-only invariant (N1, this same review) raises RuntimeError, not
    ValueError. SF2's catch must include RuntimeError specifically so THAT
    safety net degrades to the raw-trim fallback like any other fit bug,
    instead of escaping and crashing the whole MEASURE accept -- the two
    review fixes must compose, not merely coexist."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)

    def _boom(analysis, cand, cloud=None, **_kw):
        raise RuntimeError("linearization fit emitted a boost")

    monkeypatch.setattr(c, "_plan_linearization", _boom)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}
    assert "reason=RuntimeError" in caplog.text
    assert "linearization=fit_failed" in caplog.text


def test_candidate_built_linearization_field_fitted(caplog):
    """SF3: the fitted outcome.

    The field lives on ``correction.crossover_v2_candidate_built`` since the
    2026-07-27 timing move; it could not stay on ``..._measure_diag``, which is
    emitted before the candidate exists whenever a session runs a cloud group.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_candidate_built" in caplog.text
    assert "linearization=fitted" in caplog.text
    # The retired location must not quietly come back carrying a value it
    # cannot know on a cloud session.
    measure_diag = next(
        line for line in caplog.text.splitlines()
        if "event=correction.crossover_v2_measure_diag" in line
    )
    assert "linearization=" not in measure_diag
    # Gauge fix (2026-07-24): the SAME outcome is now stamped onto the
    # persisted candidate — this is the single writer's value threading all
    # the way to the artifact, not just the log line.
    assert c.candidate.linearization_outcome == "fitted"


def test_candidate_built_linearization_field_ineligible_mic_tier(caplog):
    """SF3: the ineligible_mic_tier outcome."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier="consumer")
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "linearization=ineligible_mic_tier" in caplog.text
    assert c.candidate.linearization_outcome == "ineligible_mic_tier"


def test_candidate_built_linearization_field_ineligible_repeats(caplog):
    """SF3: the ineligible_repeats outcome."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="reference", tweeter_repeats=0,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "linearization=ineligible_repeats" in caplog.text
    assert c.candidate.linearization_outcome == "ineligible_repeats"


def test_candidate_built_linearization_field_trim_rejected(caplog, monkeypatch):
    """SF3: the trim_rejected outcome (fit succeeded, but the ripple-optimal
    tweeter re-solve drifted implausibly far from its band-average seed and
    fell back to the seed pair -- distinct from "fitted" even though
    linearization is populated in both). Seed-anchored (#1668), so force the
    drift by monkeypatching the ripple-optimal solve."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    monkeypatch.setattr(
        iv, "solve_ripple_optimal_trim",
        lambda *a, **k: (k["seed_trim_db"] - 20.0, 0.0, k["seed_trim_db"]),
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "linearization=trim_rejected" in caplog.text
    assert c.candidate.linearization_outcome == "trim_rejected"


def test_no_linearization_claim_at_all_when_the_verdict_is_rejected(caplog):
    """SF3, in its post-timing-move shape: a MEASURE verdict rejected before
    the candidate is ever built (here, the pre-existing glitch check) makes NO
    linearization claim anywhere.

    Before the move this was a ``linearization=""`` field on the measure diag —
    "never a stale value from a prior attempt, and never a guess about a path
    that was never taken." The field moved to the candidate-built event, which
    simply does not fire on a rejection, so the same promise is now kept by
    silence rather than by an empty string. What must NOT happen either way is
    a value: a rejected MEASURE has no linearization outcome to report."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program, glitch=True)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is False
    assert "event=correction.crossover_v2_candidate_built" not in caplog.text
    assert "linearization=" not in caplog.text
    assert c.candidate is None


# --------------------------------------------------------------------------- #
# VERIFY-prediction coherence fix (hardware-validation-caught, #1668 PR-D)
# --------------------------------------------------------------------------- #
#
# Measured live on JTS3: VERIFY's tracking comparison ran a deterministic
# ~1.7 dB mismatch (three-attempt repeatability 1.688-1.699 dB against the
# 1.5 dB VERIFY_TOLERANCE_DB) because the persisted prediction
# (``c.measure_predicted_sum``, threaded into ``MeasurementPriors.
# predicted_sum`` by ``_verify_priors``) was still built from the RAW
# measured branches even when Layer-1a linearization was fitted and its
# correction filters emitted into the live graph. Fix: whenever
# ``_fit_linearization`` runs (the same eligibility gate that emits), it
# also rebuilds the prediction from the SAME linearized branches (W_lin/
# T_lin) at whichever trim this attempt actually committed to.


def test_measure_predicted_sum_uses_linearized_branches_when_fitted(monkeypatch):
    """The regression: once linearization is fitted (not the wild-trim
    fallback), the persisted VERIFY prediction must equal
    ``predicted_branch_sum`` evaluated on the SAME linearized branches
    ``_fit_linearization`` used internally, at the resolved trim -- and must
    differ measurably from the fixture's own raw (all-zero) prediction,
    proving the override actually took effect."""

    captured: dict = {}
    real_solve = iv.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        # Positional call shape: solve_ripple_optimal_trim(freqs, w_tf,
        # t_tf, fc_hz, *, ..., seed_trim_db=..., trim_w_db=..., sign=...).
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    # Sanity: this fixture really fitted (not the wild-trim fallback) --
    # otherwise this test would trivially pass by exercising the untouched
    # raw path.
    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.role_attenuations_db != raw_trim
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}

    resolved_w = c.candidate.role_attenuations_db["woofer"]
    resolved_t = c.candidate.role_attenuations_db["tweeter"]
    expected_complex = predicted_branch_sum(
        captured["w_tf"], captured["t_tf"], resolved_w, resolved_t, 1,
    )
    expected_db = 20.0 * np.log10(np.maximum(np.abs(expected_complex), 1e-12))

    freqs_used, db_used = c.measure_predicted_sum
    np.testing.assert_allclose(freqs_used, captured["freqs"])
    np.testing.assert_allclose(db_used, expected_db)

    # And this must actually differ from the fixture's own raw (all-zero)
    # analysis.predicted_sum -- proves the override changed the persisted
    # value, not merely happened to already agree with it.
    assert not np.allclose(db_used, 0.0)


def test_measure_predicted_sum_carries_the_committed_delay(monkeypatch):
    """**The R10b change, linearized lane.** The persisted VERIFY prediction is
    the linearized branch pair at the committed trim AND the committed delay,
    so it models what the emitted graph will actually do.

    The default fixture alignment carries no anchor, so its residual is 0.0 and
    every sibling test above is byte-identical to the pre-R10b behaviour. This
    one supplies the anchor an aligner reports and pins that the delay term is
    live: the persisted curve equals the residual-carrying model and differs
    from the five-argument one the siblings reconstruct.

    The fixture's RAW ``predicted_sum`` is rebuilt with the same residual,
    because in production ``program_analysis._build_candidate`` puts it there —
    keeping the raw and linearized models one model apart (the correction
    filters) is what the improvement gate and ``_commanded_delta`` depend on.
    """

    # A 20 us residual: comfortably inside the +/-(period/6) snap radius
    # (83.3 us at a 2 kHz Fc) and several times the ~5.5 us snap deltas the
    # synthetic MEASURE fixtures actually produce, so it is a realistic
    # selection that still moves the curve visibly.
    anchor_delay_us = 130.0
    delay_us = 150.0
    expected_residual_us = 20.0
    assert summed_model_residual_delay_us(
        anchor_delay_us, delay_us,
    ) == pytest.approx(expected_residual_us)

    captured: dict = {}
    real_solve = iv.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    def _anchored(program):
        analysis = _eligible_measure_analysis(program)
        raw_freqs, _raw_db = analysis.predicted_sum
        woofer_db, tweeter_db = _fixture_branch_db()
        trim = _solve_fixture_raw_trim(woofer_db, tweeter_db)
        raw_complex = predicted_branch_sum(
            (10.0 ** (np.asarray(woofer_db) / 20.0)).astype(complex),
            (10.0 ** (np.asarray(tweeter_db) / 20.0)).astype(complex),
            float(trim["woofer"]), float(trim["tweeter"]), 1,
            freqs_hz=raw_freqs, residual_delay_us=expected_residual_us,
        )
        return replace(
            analysis,
            alignment=_alignment(
                delay_us=delay_us, anchor_delay_us=anchor_delay_us,
            ),
            predicted_sum=(
                raw_freqs,
                20.0 * np.log10(np.maximum(np.abs(raw_complex), 1e-12)),
            ),
        )

    fakes = FakeSeams()
    fakes.measure = _anchored
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}

    resolved_w = c.candidate.role_attenuations_db["woofer"]
    resolved_t = c.candidate.role_attenuations_db["tweeter"]
    expected_db = 20.0 * np.log10(np.maximum(np.abs(predicted_branch_sum(
        captured["w_tf"], captured["t_tf"], resolved_w, resolved_t, 1,
        freqs_hz=captured["freqs"], residual_delay_us=expected_residual_us,
    )), 1e-12))
    freqs_used, db_used = c.measure_predicted_sum
    np.testing.assert_allclose(freqs_used, captured["freqs"])
    np.testing.assert_allclose(db_used, expected_db)

    # The delay term is not a no-op: the five-argument (pre-R10b) model of the
    # SAME linearized branches at the SAME trim is a different curve.
    zero_residual_db = 20.0 * np.log10(np.maximum(np.abs(predicted_branch_sum(
        captured["w_tf"], captured["t_tf"], resolved_w, resolved_t, 1,
    )), 1e-12))
    assert not np.allclose(db_used, zero_residual_db, atol=1e-6)


def test_measure_predicted_sum_uses_linearized_branches_when_trim_rejected(monkeypatch):
    """The wild-trim sanity guard only ever changes the TRIM applied -- the
    correction filters are emitted either way
    (test_wild_seed_drift_falls_back_to_seed_pair_with_warning already pins
    this). The persisted VERIFY prediction must therefore still be built from
    the LINEARIZED branches on this fallback sub-case too, just at the band-
    average SEED trim that actually ended up in role_attenuations_db (#1668
    re-anchor) -- never the un-linearized branches, and never the REJECTED
    (wild resolved) trim. Force the rejection by monkeypatching the ripple-
    optimal solve to return a far-from-seed value while still capturing the
    linearized branches it received."""

    captured: dict = {}

    def _spy(*args, **kwargs):
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        # Force the resolved tweeter trim far from its band-average seed.
        return kwargs["seed_trim_db"] - 20.0, 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    # Sanity: this really is the trim_rejected sub-case (fell back to the SEED
    # pair, not the wild resolved value).
    committed = c.candidate.role_attenuations_db
    assert committed["woofer"] == pytest.approx(captured["trim_w_db"])
    assert committed["tweeter"] == pytest.approx(captured["seed_trim_db"])
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}

    expected_complex = predicted_branch_sum(
        captured["w_tf"], captured["t_tf"],
        captured["trim_w_db"], captured["seed_trim_db"], 1,
    )
    expected_db = 20.0 * np.log10(np.maximum(np.abs(expected_complex), 1e-12))
    freqs_used, db_used = c.measure_predicted_sum
    np.testing.assert_allclose(freqs_used, captured["freqs"])
    np.testing.assert_allclose(db_used, expected_db)


def test_measure_predicted_sum_unchanged_when_linearization_ineligible():
    """The ineligible/raw path stays byte-identical to before this fix:
    ``c.measure_predicted_sum`` is exactly ``analysis.predicted_sum`` -- the
    fixture's own RAW two-branch sum -- never overridden."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier="consumer")
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization == {}

    freqs_used, db_used = c.measure_predicted_sum
    expected_freqs, expected_db = _fixture_raw_predicted_sum()
    np.testing.assert_array_equal(freqs_used, expected_freqs)
    np.testing.assert_array_equal(db_used, expected_db)


def test_measure_predicted_sum_unchanged_when_fit_engine_raises(monkeypatch):
    """SF2 interaction: when the fit engine raises and the candidate build
    degrades to the raw-trim/empty-linearization fallback, the persisted
    VERIFY prediction must degrade with it -- exactly
    ``analysis.predicted_sum``, never a half-computed linearized value left
    over from a call that never reached its own tail."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)

    def _boom(analysis, cand, cloud=None, **_kw):
        raise ValueError("simulated fit engine bug")

    monkeypatch.setattr(c, "_plan_linearization", _boom)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization == {}

    freqs_used, db_used = c.measure_predicted_sum
    expected_freqs, expected_db = _fixture_raw_predicted_sum()
    np.testing.assert_array_equal(freqs_used, expected_freqs)
    np.testing.assert_array_equal(db_used, expected_db)


def test_verify_rearm_measure_predicted_sum_era_round_trip():
    """Era-tolerance: a verify-only re-arm conductor supplied a persisted
    ``measure_predicted_sum`` from BEFORE this coherence fix (a plain
    raw-branch prediction, no linearization awareness) must carry it
    through completely UNCHANGED. This fix only changes what
    ``_measure_verdict`` COMPUTES on a fresh MEASURE accept -- a re-arm
    conductor never calls ``_measure_verdict``/``_fit_linearization`` at all
    (MEASURE is already accepted, see ``index_phase_map={1: PHASE_VERIFY}``),
    so whatever value the constructor was handed is exactly what VERIFY
    compares against, byte for byte."""
    freqs = np.linspace(100.0, 20000.0, 64)
    old_era_prediction = (freqs, np.full(64, -3.0))
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id="era_rearm_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        gain_plan_db={"woofer": -11.0, "tweeter": -13.0},
        index_phase_map={1: PHASE_VERIFY},
        measure_predicted_sum=old_era_prediction,
        measure_gate_window_ms=8.0,
    )
    got_freqs, got_db = c.measure_predicted_sum
    np.testing.assert_array_equal(got_freqs, freqs)
    np.testing.assert_array_equal(got_db, old_era_prediction[1])

    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"
    # Untouched by the VERIFY walk -- still exactly the supplied era tuple.
    got_freqs2, got_db2 = c.measure_predicted_sum
    np.testing.assert_array_equal(got_freqs2, freqs)
    np.testing.assert_array_equal(got_db2, old_era_prediction[1])


# --------------------------------------------------------------------------- #
# PR-L5 — delta-probe verification and automatic rollback
# --------------------------------------------------------------------------- #


def test_delta_probe_verifies_the_correction_and_accepts_a_matching_one():
    """The happy path: the speaker did what the filters commanded, so the
    probe records a MATCHED map and the session is untouched."""
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program), verify_tracking_curve=_tracking_curve(c, 0.0),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"
    assert c.delta_probe is not None
    assert c.delta_probe.verdict == VERDICT_MATCHED
    assert c.delta_probe.rollback is False
    assert c.delta_probe.to_dict()["rollback"] is False


def test_delta_probe_removes_the_applys_declared_level_move(caplog):
    """#1811 wiring: the conductor threads the apply's own declared offset into
    the probe, and that is what keeps a healthy correction from being rolled
    back for the pre-split headroom its own boost was charged.

    The live shape: the apply charged 22.458 dB, so the post-apply capture
    arrives that far down against a prediction carrying no such term. Blind,
    the probe can only say the level axis is broken. Told what moved, it grades
    the correction — and passes it.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    # The apply's move is the ONLY thing that changed the level here, so the
    # pre-apply capture sat exactly on its prediction (#2533) -- which is
    # ``_probed_conductor``'s stated default since series-2 D1 made the two
    # directional safety findings changes against that capture too.
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(c, -22.458),
    )
    assert c.delta_probe is None
    _run_phase(c, 3, 3)
    # Seam unbound (this FakeSeams leaves it None) ⇒ "nothing known", and the
    # shift stays visible rather than being claimed as accounted for.
    assert c.delta_probe.verdict == VERDICT_LEVEL_MISMATCH
    assert c.delta_probe.expected_offset_db == 0.0
    assert c.delta_probe.residual_offset_db == pytest.approx(-22.458, abs=1e-6)
    assert c.delta_probe.entry_anchor_offset_db == pytest.approx(0.0, abs=1e-6)
    assert c.delta_probe.rollback is False

    fakes2 = FakeSeams()
    c2 = _probed_conductor(fakes2)
    c2._seams = dataclasses.replace(
        c2._seams, applied_offset_db=lambda: -22.458,
    )
    fakes2.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(c2, -22.458),
    )
    verdict = _run_phase(c2, 3, 3)
    assert verdict["accepted"] is True
    assert c2.delta_probe.verdict == VERDICT_MATCHED
    assert c2.delta_probe.expected_offset_db == pytest.approx(-22.458)
    assert c2.delta_probe.residual_offset_db == pytest.approx(0.0, abs=1e-6)
    assert "expected_offset_db=-22.458" in caplog.text


def test_a_level_mismatch_is_persisted_and_logged_at_warning(caplog):
    """#1811 SF1: a non-rollback finding must leave a trace, on both surfaces.

    ``level_mismatch`` is not in ``DELTA_PROBE_ROLLBACK_VERDICTS`` by design,
    so nothing escalates on it and the session passes — and until this landed the ONLY evidence was an INFO journal line
    nobody greps. It now rides WARNING (the level a reader sweeping a
    "successful" session actually sees) and is persisted so ``/state``, the
    doctor, and the done screen's caveat can all read one record.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program), verify_tracking_curve=_tracking_curve(c, -22.458),
    )
    verdict = _run_phase(c, 3, 3)
    # The session still passes — the no-rollback adjudication is unchanged.
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"
    assert c.delta_probe.verdict == VERDICT_LEVEL_MISMATCH

    probe_lines = [
        r for r in caplog.records
        if "event=correction.crossover_v2_delta_probe" in r.getMessage()
        and "verdict=level_mismatch" in r.getMessage()
    ]
    assert probe_lines, "the probe must log its verdict"
    assert all(r.levelno >= logging.WARNING for r in probe_lines)


def test_delta_probe_offset_seam_that_misbehaves_is_nothing_known():
    """A seam that raises, or hands back a non-finite number, must degrade to
    "nothing known" (0.0) — never to a claimed offset the emitter cannot
    actually vouch for, and never to a crash on the VERIFY path."""
    for broken in (
        lambda: (_ for _ in ()).throw(RuntimeError("state unreadable")),
        lambda: float("nan"),
        lambda: "loud",
    ):
        fakes = FakeSeams()
        c = _probed_conductor(fakes)
        c._seams = dataclasses.replace(c._seams, applied_offset_db=broken)
        fakes.verify = lambda program, _c=c: dataclasses.replace(
            _verify_analysis(program), verify_tracking_curve=_tracking_curve(_c, 0.0),
        )
        _run_phase(c, 3, 3)
        assert c.delta_probe.expected_offset_db == 0.0
        assert c.delta_probe.verdict == VERDICT_MATCHED


def test_delta_probe_model_error_rolls_back_automatically_and_refuses(caplog):
    """The load-bearing behaviour: a realized-vs-commanded map that does not
    match is undone BEFORE the household is told, so the copy ("the previous
    sound has been put back") is already true when they read it.

    **Which SENTENCE they read moved, and the move is the routing working.**
    The probe's own seam refused under the probe's class and consulted nothing
    else. The round consults every axis, and this fixture's ±5 dB tilt trips
    the SAFETY axis too — a commanded boost realized above its declared bound —
    which the table checks before quality. So the graph comes off under the
    stronger true sentence rather than the shape one. The unsafe-result code is
    not a demotion of the finding: the probe's own verdict is still
    ``model_error`` and still on the record, one assertion below.
    """
    # The COORDINATOR's logger, at INFO: a SUCCESSFUL restore is not an error,
    # and the line moved there with the decision.
    caplog.set_level(
        logging.INFO, logger="jasper.active_speaker.crossover_v2.coordinator",
    )
    calls: list[str] = []
    fakes = FakeSeams()
    c = _probed_conductor(fakes, rollback=lambda reason: calls.append(reason) or True)
    # A wide tilt across the commanded band: the shape is wrong, not the scale.
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0)
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_UNSAFE_RESULT
    assert c.delta_probe.verdict == VERDICT_MODEL_ERROR
    # The rollback ran, exactly once, and it ran with the cause the round
    # decided on rather than a second copy of it.
    from jasper.active_speaker.crossover_v2.verification import (
        SAFETY_BOOST_OVER_DECLARED_BOUND,
    )

    assert calls == [SAFETY_BOOST_OVER_DECLARED_BOUND]
    assert "event=correction.crossover_v2_round_restore" in caplog.text
    assert "restored=true" in caplog.text
    # The refusal names itself to the host (the same contract PR-L4 relies on).
    assert c.last_failure_code == REASON_CORRECTION_UNSAFE_RESULT


def test_delta_probe_refuses_honestly_when_no_rollback_seam_is_bound(caplog):
    """The verdict is real whether or not this process can act on it — but the
    COPY has to match what happened to the speaker.

    A conductor with no rollback binding still refuses, and refuses under
    ``correction_rollback_failed``, whose copy says the correction is STILL
    APPLIED and names Undo. The three verdict-specific codes all promise "the
    previous sound has been put back", and a household listening to a
    correction while being told it was reverted is a false statement about
    their speaker (adversarial review S4)."""
    caplog.set_level(logging.ERROR, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    assert c._seams.rollback is None
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0)
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED
    # The finding itself is still recorded and still specific.
    assert c.delta_probe.verdict == VERDICT_MODEL_ERROR
    # LOUD on the journal, from the one owner that now decides it. The table
    # knows before it tries that there is no anchor, so it does not attempt a
    # restore it cannot make — and says so, which is what keeps the STILL
    # APPLIED sentence below true.
    assert "event=correction.crossover_v2_round_recovery_required" in caplog.text
    assert "rollback_anchor_available=false" in caplog.text
    message = REASON_REGISTRY[REASON_CORRECTION_ROLLBACK_FAILED].message
    assert "STILL APPLIED" in message
    assert "put back" not in message.replace("put the previous sound back", "")


def test_delta_probe_survives_a_rollback_seam_that_raises():
    """A rollback that could not run must not swallow the verdict that asked
    for it."""
    fakes = FakeSeams()

    def _boom(_reason):
        raise RuntimeError("camilla is unreachable")

    c = _probed_conductor(fakes, rollback=_boom)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0)
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    # …and it refuses HONESTLY: the restore did not happen, so the copy must
    # not say it did.
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED
    assert c.delta_probe.verdict == VERDICT_MODEL_ERROR


def test_delta_probe_without_a_tracking_curve_is_unavailable_not_a_rollback():
    """No post-apply comparison, no verdict — and an absent measurement is not
    evidence of a bad correction. Rolling back on it would revert every session
    whose household closed the phone before the sweep."""
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = _verify_analysis  # carries no verify_tracking_curve
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.delta_probe is None


def test_delta_probe_grades_the_bands_the_captures_gate_trusts(caplog):
    """**#2521 wiring.** The probe's band is the capture's own gate-derived
    trusted band, threaded from the gating block the analysis carries — not the
    grid edges, and not a floor this flow derives a second time.

    Driven by a fixture whose gate trusts only part of its grid, with a large
    error placed OUTSIDE that part. A probe reading the grid edges rolls this
    back; a probe reading the gate's band passes it and says which band it
    graded.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    trusted_hi_hz = 8_000.0
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program, trusted_band_hz=(300.0, trusted_hi_hz)),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > trusted_hi_hz, 20.0, 0.0),
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.delta_probe.rollback is False
    assert c.delta_probe.requested_band_hz == (300.0, trusted_hi_hz)
    assert c.delta_probe.probe_band_hz[1] <= trusted_hi_hz
    # The band is on the journal line too, beside the band it actually graded —
    # a disputed verdict should be self-describing (#2521).
    assert f'trusted_band_hz="(300.0, {trusted_hi_hz})"' in caplog.text


def test_a_capture_with_no_trusted_band_leaves_the_probe_unavailable(caplog):
    """An ungateable capture has no band this probe can be honest over, and
    there is deliberately no fallback (#2521).

    Falling back to the raw grid edges would apply the widest possible band to
    the LEAST trustworthy capture — the exact inversion the trusted band
    exists to prevent. ``unavailable`` is not a pass: it refuses nothing and
    permits nothing, which is what every other honesty instrument in this flow
    does with an unknown.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program, trusted_band_hz=None),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0),
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.delta_probe is None
    assert "event=correction.crossover_v2_delta_probe_no_trusted_band" in caplog.text


def test_a_frame_carrying_capture_is_disclosed_rather_than_rolled_back(caplog):
    """**#2521's policy half, wired end to end.**

    A broadband tilt between the in-room capture and the on-axis prediction is
    the ordinary state of this comparison, and before this it rolled healthy
    corrections back. The session now passes, the household is told, and the
    journal carries the tilt that was removed and the grade that survived it.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: -0.9 * np.log2(f / 1_000.0),
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"
    assert c.delta_probe.verdict == VERDICT_FRAME_MISMATCH
    assert c.delta_probe.rollback is False
    assert "frame_removed=true" in caplog.text
    assert "frame_tilt_db_per_octave=-0.9" in caplog.text
    # A non-rollback finding on an otherwise-passing session rides WARNING, or
    # nobody sweeping the journal ever sees it (the #1811 argument, one verdict
    # over).
    probe_lines = [
        r for r in caplog.records
        if "event=correction.crossover_v2_delta_probe " in r.getMessage()
        and "verdict=frame_mismatch" in r.getMessage()
    ]
    assert probe_lines, "the probe must log its verdict"
    assert all(r.levelno >= logging.WARNING for r in probe_lines)


def test_delta_probe_runs_only_after_tracking_has_passed():
    """A session that already failed at the handoff band does not need a
    second verdict about the same capture, and its retry budget still means
    something."""
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program, max_db=2.4),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0)
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "verify_out_of_tolerance"
    assert c.delta_probe is None


def test_boost_is_granted_only_to_a_journey_that_will_verify():
    """Boost permission is EVIDENCE-gated on the post-apply sweep.

    **Re-derived for the two-stage split (work order D2).** The gate used to
    read ``PHASE_VERIFY in self.session_phases``, which was exact while one
    session carried both the fit and the post-apply sweep. Stage 1 has no
    VERIFY entry at all — the sweep is stage 2's session — so that reading
    would silently demote every two-stage correction to cut-only. The measuring
    host now DECLARES the answer from the plan shape it resolved, and the gate
    reads the declaration. It is still a condition rather than a constant: a
    session told the journey will not verify is refused the vocabulary.
    """
    fakes = FakeSeams()
    seen: list[bool] = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)
    assert seen and all(seen)
    # …on a session that does NOT itself run VERIFY — the point of the change.
    assert PHASE_VERIFY not in c.session_phases

    # A session told its journey will not verify is refused the vocabulary…
    seen.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c2 = _cloud_conductor(fakes, post_apply_verifies=False)
        _walk_measure_cloud_to_close(c2)
    assert seen and not any(seen)

    # …and so is one that declares nothing and runs no VERIFY of its own, so
    # the undeclared default stays the conservative phase-derived reading.
    seen.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c3 = _conductor(fakes, index_phase_map={1: PHASE_CHECK, 2: PHASE_MEASURE})
        _run_phase(c3, 1, 1)
        _run_phase(c3, 2, 2)
    assert seen and not any(seen)


def test_boost_is_refused_when_the_cloud_verdict_never_reached_the_envelope():
    """**The null-exclusion gate** (adversarial review B2), and the ONE case it
    still decides after the owner's boost ruling (#2106, 2026-08-05).

    ``_cloud_fit_evidence`` has two reachable ``None`` paths (the positions
    could not be combined; the honesty pipeline was unavailable). On both,
    ``compose_envelope`` gets ``excluded_bands_hz=None``, so
    ``allowed_depth_db`` is NOT zeroed in the registry's interference nulls —
    and a boost designed into a null reads MATCHED at the mark while the
    spatial arm, the one instrument that could contradict it, is absent on
    exactly those paths. So boost is withheld; cut-only proceeds.

    **What the ruling changed, and why this test survived it.** The gate used
    to read ``cloud is not None`` for EVERY session, which also caught R15's
    driver-only path — where the cloud is absent BY DESIGN and there is
    nothing to lose. The two states share the ``cloud is None`` signature and
    are different evidence, so the gate now asks the session's own plan which
    one it is. This fixture is the *planned-and-lost* one, and the precondition
    is asserted rather than inherited from the helper: a session that went
    looking for spatial evidence and came back without it does not get to
    boost. Its sibling
    ``test_boost_is_granted_on_the_driver_only_path_that_plans_no_cloud`` is
    the other side."""
    fakes = FakeSeams()
    seen: list[bool] = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c = _cloud_conductor(fakes)
        # THE precondition this test now turns on: the session PLANNED a cloud.
        # Without it the fixture would be indistinguishable from R15's
        # driver-only path, which the same gate deliberately allows.
        assert PHASE_CLOUD_MEASURE in c.session_phases
        mp.setattr(c, "_cloud_fit_evidence", lambda combined: None)
        _walk_measure_cloud_to_close(c)
    assert seen and not any(seen)
    # The correction still happened — only the LIFT vocabulary was withheld.
    assert c.candidate is not None
    assert all(
        f["gain"] <= 0.0
        for fit in c.candidate.linearization.values()
        for f in fit["filters"]
    )
    # …and the absence is already disclosed, not silent.
    assert c.candidate.exclusion_evidence == {}


def test_boost_is_granted_on_the_driver_only_path_that_plans_no_cloud():
    """**The owner's boost ruling** (#2106, 2026-08-05), on the path it is
    about — recorded in the "Boost ruling" block of
    ``docs/historical/linearization-campaign-2026-07.md`` §4.2.

    R15 took the pre-apply cloud out of stage 1 (``STAGE1_INCLUDES_CLOUD_
    MEASURE``), so a driver-only session has no cloud verdict to wait for. The
    retired ``cloud is not None`` demand would have demoted every R15
    correction to cut-only for want of evidence the plan never collects — a
    speaker with a fillable dip would have been handed a fit that cannot fill
    it, forever, with nothing in the journey that could ever change the answer.

    The ruling permits boost here on a NAMED accepted risk: a boost can land on
    a position-specific artifact that an at-mark verification cannot detect.
    What adjudicates it instead is post-apply ``VERIFY``, household listening,
    and retained Undo, with the standing rails (envelope depth, the
    realized-cascade stopband guard, the headroom charge) still bounding the
    filter — each pinned by its own test below.

    **Asserted as a filter actually PLACED, not as a permission carried in the
    vocabulary.** The gate grants a vocabulary; the point of the ruling is that
    the lift stage downstream of it runs. A version of this test that asserted
    only ``allow_boost is True`` would stay green if ``_lift_stage`` were
    disconnected tomorrow.
    """
    fakes = FakeSeams()
    seen: list = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _vocabularies_seen(seen))
        c = _conductor(fakes)
        # The scope precondition, asserted rather than inherited: this session's
        # own capture plan contains no pre-apply cloud phase. That — not
        # "``cloud`` came back ``None``" — is what the gate reads, and it is what
        # separates this fixture from its planned-and-lost sibling above.
        assert PHASE_CLOUD_MEASURE not in c.session_phases
        assert c.post_apply_verifies is True
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 2)

    assert seen and all(v.allow_boost for v in seen)
    # The accepted risk, made explicit rather than left as a silently-empty
    # set: there is no spatial evidence on this path, so there are no
    # cloud-derived exclusions to carry. The plan records the risk; this
    # records that the code is honest about where it comes from.
    assert all(v.boost_excluded_bands_hz == () for v in seen)

    boosts = _emitted_boosts(c.candidate)
    assert boosts, "the ruling is about a boost the fit actually places"


def test_a_cut_only_journey_on_the_same_fixture_places_no_boost():
    """The other half of the pair above, on the IDENTICAL fixture, so the
    difference is the gate and nothing else.

    ``post_apply_verifies=False`` is the surviving necessary condition (nothing
    will measure what the speaker did), so the same session that boosts above
    is cut-only here — and the fit's own post-hoc invariant
    (``fit_driver_linearization``'s "emitted a boost under a cut-only
    vocabulary" ``RuntimeError``) is what makes that structural rather than
    incidental.
    """
    fakes = FakeSeams()
    seen: list = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _vocabularies_seen(seen))
        c = _conductor(fakes, post_apply_verifies=False)
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 2)

    assert seen and not any(v.allow_boost for v in seen)
    assert _emitted_boosts(c.candidate) == []


def test_the_ruling_lets_a_candidate_pass_that_cut_only_graded_as_no_improvement():
    """**An intended behaviour change, pinned as intent rather than discovered
    as a regression** (#2106, conductor ruling).

    Boost expands the ACHIEVABLE set, so a candidate the improvement gate
    previously refused can now clear it on the same evidence. That is
    legitimate at the mark — the gate asks whether the proposed correction is
    materially better than doing nothing, and a fit that may fill a dip has a
    strictly larger set of answers than one that may only cut — and it is
    adjudicated downstream by post-apply ``VERIFY``, household listening, and
    retained Undo rather than by withholding the vocabulary.

    **The gate stopped refusing entirely** with the nanny burn-down (doctrine
    deviation (c)), so what the cut-only arm demonstrates now is the LEDGER
    verdict rather than a raised refusal. #2106's ruling is unaffected: it was
    about which candidates the vocabulary can reach, and that is still what
    separates the two arms.

    ``_healthy_crossed_over_pair`` is the case, and it is a real one rather
    than a contrivance: its only defects are two in-band DIPS. A cut-only fit
    cannot fill a dip at any depth (#1809's own doctrine), so it has nothing
    material to offer.

    Both arms here run the same session; only the gate differs. This is also
    the mutation evidence for the gate itself: restoring the cut-only
    vocabulary restores the no-improvement verdict.
    """
    woofer_db, tweeter_db, trim_db = _healthy_crossed_over_pair()

    def session(**kwargs):
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(
            program, woofer_db=woofer_db, tweeter_db=tweeter_db, trim_db=trim_db,
        )
        c = _conductor(fakes, **kwargs)
        _run_phase(c, 1, 1)
        return c

    # --- cut-only: nothing material to offer, and the ledger says so ---
    cut_only = session(post_apply_verifies=False)
    _run_phase(cut_only, 2, 2)
    assert (
        cut_only.measure_predicted_spec_report["comparison"]["reason"]
        == accountability.LEDGER_NOT_AN_IMPROVEMENT
    )

    # --- the shipped driver-only gate: the same session completes ---
    boosted = session()
    verdict = _run_phase(boosted, 2, 2)
    assert verdict["accepted"] is True
    assert boosted.candidate is not None
    # …and it is the BOOST that made the difference, not some unrelated drift:
    # what the cut-only arm could not do is fill the dips, and this arm does.
    assert _emitted_boosts(boosted.candidate)


def test_the_envelope_still_bounds_a_boost_on_the_driver_only_path():
    """**Rail 1 of the ruling's three**, on the path the ruling opened.

    ``allowed_depth_db`` is direction-agnostic — the same per-bin array bounds
    a cut and a boost — and it is composed from mic trust, repeatability,
    linearity, invertibility and the class prior, none of which the cloud
    supplied. So it binds identically with no cloud present, and this asserts
    that by CLAMPING it: capped at 1.0 dB, the fit may no longer place the
    boost it places unclamped.

    Written as a clamp rather than as an observation of the shipped number
    because an observation would stay green if the envelope were disconnected
    from the lift stage — the uncapped arm below is what makes the capped one
    mean something.

    **The cap is 2.0 dB, and the value is load-bearing** (gate finding on
    #2138). At 1.0 dB the capped arm places no positive gain at all — the lift
    is suppressed outright — so a "every gain <= cap" assertion would inspect
    only cuts and pass while saying nothing. At 2.0 dB the boost SURVIVES and
    is CLAMPED (exactly 2.000 dB against 3.715 unclamped), which is the state
    that discriminates. So both halves are asserted: a positive gain is placed,
    and no gain exceeds the cap. Removing either half makes the test vacuous
    again.

    **Which of the envelope's two bounds this pins, measured rather than
    assumed.** The stage bounds a lift twice: a REQUEST bound (``wanted =
    min(deficit, allowed_depth)``) and a REALIZATION gate on the emitted
    cascade (``exceeds_envelope``, for a greedy bell fit that overshoots
    BETWEEN its centres). This test kills the request bound — deleting it makes
    the fit ask for the full 3.715 dB, the realization gate then suppresses the
    lift wholesale, and the "a boost survived" assertion above fires.

    It does NOT cover the realization gate, and the docstring says so rather
    than implying it. Instrumented at the gate's own expression, this fixture's
    clamped lift is a single bell whose realized peak sits at
    ``max(realized - allowance) == -0.000000 dB`` over ``band_mask`` — exactly
    ON the allowance, which is the request bound binding — while the gate only
    fires ``_MIN_FILTER_GAIN_DB`` (0.5 dB) ABOVE that. So the fixture sits a
    full 0.5 dB below the firing threshold, the gate never fires here, and
    disarming it changes nothing this test could see. Reaching it needs a
    multi-dip response where bells overshoot BETWEEN centres; that is the
    ``unlock`` case in ``_NON_MONOTONE_SHAPES`` in
    ``tests/test_active_speaker_linearization_fit.py``, where the gate's
    discriminating assertion now lives.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)

    cap_db = 2.0
    real_compose = iv.compose_envelope
    real_fit = iv.fit_driver_linearization

    def _capped(*args, **kwargs):
        env = real_compose(*args, **kwargs)
        return replace(
            env,
            allowed_depth_db=np.minimum(env.allowed_depth_db, cap_db),
        )

    fitted: list = []

    def _record(resp, envelope, **kwargs):
        fit = real_fit(resp, envelope, **kwargs)
        fitted.append(fit)
        return fit

    # Unclamped first, so the assertion below is not vacuous: this fixture
    # really does want more boost than the cap allows.
    free = _conductor(fakes)
    _run_phase(free, 1, 1)
    _run_phase(free, 2, 2)
    free_boosts = [f["gain"] for f in _emitted_boosts(free.candidate)]
    assert free_boosts and max(free_boosts) > cap_db

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "compose_envelope", _capped)
        mp.setattr(iv, "fit_driver_linearization", _record)
        capped = _conductor(fakes)
        _run_phase(capped, 1, 1)
        _run_phase(capped, 2, 2)

    assert fitted, "the fit must have run for this to assert anything"
    capped_boosts = [
        f.gain for fit in fitted for f in fit.filters if f.gain > 0.0
    ]
    # A boost SURVIVED the clamp — without this the loop below inspects cuts.
    assert capped_boosts, "the clamped fit must still place a boost"
    # …and the envelope BOUND it.
    for fit in fitted:
        for f in fit.filters:
            assert f.gain <= cap_db + 1e-9, f


def test_the_headroom_charge_is_paid_for_a_driver_only_boost():
    """**Rail 3 of the three.** A boost is not free: the branch CHAIN's
    realized peak is charged as headroom at emission
    (``camilla_yaml.linearization_headroom_db`` via
    ``branch_chain.branch_headroom_db``), and the runtime contract re-derives
    the same peak from the emitted graph text and refuses to prove a graph that
    did not pay it (``runtime_contract._consume_linearization_chain``).

    Deliberately asserted here only as far as the CONDUCTOR's own disclosure —
    the charge exists and is the committed chain's own peak. Everything below
    that seam reads the emitted graph and is blind to which gate granted the
    boost, so it needs no driver-only variant; its pins live in
    ``tests/test_active_speaker_linearization_emission.py``
    (``test_linearization_boost_is_accepted_and_absorbed_by_baseline_
    headroom``, ``test_reproof_blocks_boost_beyond_the_absorbed_headroom``).
    """
    from jasper.active_speaker.branch_chain import branch_headroom_db

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)

    assert PHASE_CLOUD_MEASURE not in c.session_phases
    boosted_roles = {
        role
        for role, fit in c.candidate.linearization.items()
        if any(f["gain"] > 0.0 for f in fit["filters"])
    }
    assert boosted_roles, "the fixture must boost for the charge to mean anything"
    for role, fit in c.candidate.linearization.items():
        assert fit["headroom_cost_db"] == pytest.approx(
            branch_headroom_db(
                fit["filters"],
                sections=_configured_sections(c, role),
                trim_db=c.candidate.role_attenuations_db[role],
            )
        )
    # …and the charge is REAL on the boosted branch, not a zero that happens to
    # match a zero (``linearization_headroom_db`` short-circuits to 0.0 when no
    # emitted filter has positive gain, so an all-cut role legitimately reads
    # 0.0 and would satisfy the equality above by itself).
    for role in boosted_roles:
        assert c.candidate.linearization[role]["headroom_cost_db"] > 0.0


def test_every_non_matched_verdict_reaches_a_household_surface():
    """A new NON-MATCHED verdict cannot ship without reaching the household.

    This guard used to assert equality with the ROLLBACK set, which enforced
    the stated intent only for as long as the two sets were the same thing.
    ``level_mismatch`` (#1811) is the first non-matched verdict that is
    deliberately not a rollback, so it slipped through an equality check while
    rendering as a clean pass. The guard now walks the non-matched set: a
    verdict either has a refusal code with real copy, or is named here with
    the surface it does reach instead.
    """
    non_matched = set(DELTA_PROBE_VERDICTS) - {VERDICT_MATCHED, VERDICT_UNAVAILABLE}
    # Verdicts that reach the household WITHOUT a refusal. Adding one here is
    # a claim that must be true — each entry names the surface, and that
    # surface has its own test.
    surfaced_without_refusal = {
        # Persisted as ``verify.delta_probe`` by ``persist_conductor_state``
        # and rendered as the done screen's caveat nudge — see
        # ``test_a_level_mismatch_caveats_the_pass_screen`` in
        # tests/test_crossover_envelope_v2.py.
        VERDICT_LEVEL_MISMATCH,
        # The tilt-carrying sibling of the one above (#2521), on the same
        # surface and by the same route — see
        # ``test_a_frame_mismatch_caveats_the_pass_screen`` in
        # tests/test_crossover_envelope_v2.py.
        VERDICT_FRAME_MISMATCH,
        # The shape check did not RUN (#2614) — an alternative-Fc round has no
        # like-for-like previous graph, so there is no change axis to grade
        # against. Not a finding about the speaker, so not a refusal; it
        # reaches the household on the same done-screen caveat by the same
        # route — see ``test_a_safety_only_probe_caveats_the_pass_screen`` in
        # tests/test_crossover_envelope_v2.py.
        VERDICT_SAFETY_ONLY,
    }
    assert set(DELTA_PROBE_REASON_BY_VERDICT) == non_matched - surfaced_without_refusal
    assert set(DELTA_PROBE_REASON_BY_VERDICT) == set(DELTA_PROBE_ROLLBACK_VERDICTS)
    for code in DELTA_PROBE_REASON_BY_VERDICT.values():
        spec = REASON_REGISTRY[code]
        assert spec.template == "hard_stop"
        assert spec.retry_budget == 0
        assert len(spec.message) > 40
        # The correction is already undone, so the copy has to say so.
        assert "put back" in spec.message


def test_delta_probe_reason_copy_names_no_hardware_noun():
    """Mirrors the null-classification copy rule: the household is told what
    happened and what to do, never given a hardware diagnosis this measurement
    cannot support."""
    # "driver details in speaker setup" is a UI location and appears in
    # PR-L4's own copy — what is banned is naming a PART as the cause, which
    # is a diagnosis this measurement cannot support.
    banned = ("tweeter", "woofer", "amplifier", "horn", "capacitor", "resistor")
    for code in DELTA_PROBE_REASON_BY_VERDICT.values():
        message = REASON_REGISTRY[code].message.lower()
        assert not any(word in message for word in banned), code


def test_the_commanded_delta_is_none_when_a_side_is_missing():
    """A missing curve on EITHER side is ``None``, which the probe reads as
    ``unavailable`` — not as a zero curve that would classify as 'matched'.

    **Amended by #2611, and the amendment is the point.** This test also pinned
    ``_commanded_delta(predicted, predicted) is None`` — the trims-only guard,
    which was correct while the previous side was the raw crossover at the
    applied candidate's own parameters: a candidate emitting no filters produced
    the identical object on both sides and had, in that frame, commanded
    nothing. In the applied-vs-PREVIOUS-graph frame a trims-only candidate
    commands its whole trim, polarity and delay step, so that guard would now
    delete a real commanded change. Two equal-VALUED curves still yield a
    flat-zero delta, which ``classify_delta_probe``'s own commanded floor
    refuses as ``nothing_commanded`` — one owner for "was anything asked for",
    one layer down.
    """
    predicted = (np.array([100.0, 200.0]), np.array([0.0, 0.0]))
    assert flow._commanded_delta(None, predicted) is None
    assert flow._commanded_delta(predicted, None) is None
    _freqs, delta = flow._commanded_delta(predicted, predicted)
    assert list(delta) == [0.0, 0.0]


def test_the_commanded_delta_is_the_applied_minus_the_previous_graph():
    previous = (np.array([100.0, 1000.0]), np.array([0.0, 0.0]))
    applied = (np.array([100.0, 1000.0]), np.array([-1.0, 4.0]))
    freqs, delta = flow._commanded_delta(previous, applied)
    assert list(freqs) == [100.0, 1000.0]
    assert list(delta) == [-1.0, 4.0]


# --------------------------------------------------------------------------- #
# adversarial-review regressions (round 2)
# --------------------------------------------------------------------------- #


def test_the_realized_level_assertion_still_fires_on_its_own_evidence(caplog):
    """**S6(a).** Item 1 (the realized-level check) is the only level check
    left since the single-datum-owner migration (#2609) deleted the two-voter
    frame's own refusal arm, and this pins that it still fires on its own
    evidence rather than having quietly gone dead.

    **What it does when it fires changed; THAT it fires did not** (doctrine
    deviation (i)). It banks a finding and the round proceeds. This test is
    deliberately kept — inverted rather than deleted — because "the demotion"
    and "the check rotted away" are the two outcomes a reader has to be able to
    tell apart, and only an assertion that the numbers still reach the journal
    can do that.

    **Item 1's route in this harness is now the ONLY route.** The
    level-consistency check (#2609's ``compare_level_definitions``) compares the
    two per-driver estimators and banks a finding; neither has a refusal arm
    now, so every session reaches the end with whatever the anchor computed and
    a disclosure beside it. (The ripple polish is not a route around it either
    — the linearized scan can still move the committed pair, but only through
    the wild-trim guard, which grades both candidates on this same assertion
    first.)
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    # The realized level verdict is SUPPLIED rather than provoked, for the same
    # reason ``test_wild_trim_fallback_follows_levels_not_drift`` supplies its
    # pair: the physical routes that used to mislevel a committed trim are the
    # ones PR-L3 closed, and re-opening one to test the gate that catches it
    # would be testing the wrong thing. What must be pinned is that item 1
    # still reports on its own evidence, under its own event.
    def _match(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-5.2, difference_db=-5.2,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "realized_level_match", _match)
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 2)

    # Item 1's own disclosure, under its own event.
    assert "event=correction.crossover_v2_level_match_finding" in caplog.text
    assert "event=correction.crossover_v2_level_match_refused" not in caplog.text
    # …with both realized levels on the line, so the verdict is re-derivable.
    for ledger_field in (
        "difference_db=", "level_w_db=", "level_t_db=", "tolerance_db=",
    ):
        assert ledger_field in caplog.text
    # The round proceeded and banked its reservation.
    assert c.candidate is not None
    assert len(fakes.published_candidates) == 1
    assert fakes.banked_findings != []


def test_prediction_gate_logs_the_improved_path_with_both_terms(caplog):
    """**S6(b).** The ledger's ``improved`` path and its ``before_rms_db`` /
    ``improvement_db`` terms are the ones a field diagnosis reads to answer
    "did the correction actually help, and by how much" — and after PR-L5
    moved the default fixture into the ``predicted_in_spec`` early return,
    nothing asserted them any more.

    Driven by a correction that genuinely improves its own model WITHOUT
    reaching spec — the only shape that reaches this branch. A big broad peak
    the fit can take out (3.6 dB pooled residual down to 0.46) riding on a comb
    it cannot (there are far more notches than the filter budget), so the
    prediction moves materially and still fails.

    The comb went from 3 dB to 5 dB with #1809: once the fit stops spending
    gain inside each driver's own crossover stopband the corrected prediction
    is better, and at 3 dB it now clears the spec outright and takes the
    ``predicted_in_spec`` early return instead of reaching this branch.

    **The peak moved onto Fc, and the trim is now solved, with #1929.** This
    fixture was reaching the prediction gate only by cancellation. Its two
    branches carry the IDENTICAL curve, whose two mirrored ±1-octave halves
    about Fc genuinely sit 8.32 dB apart when the peak is an octave below Fc
    (level_w 11.17, level_t 2.85) — but it inherited ``_FIXTURE_RAW_TRIM_DB``,
    solved from the DEFAULT curves, which says 0.70 dB. That is exactly the "a
    fixture field nobody derived from the fixture" defect
    :func:`_solve_fixture_raw_trim`'s own docstring documents, and the shipped
    whole-band core median happened to be wrong by the same amount and sign,
    so the frame gate read 0.073 dB. Solving the trim from THESE branches and
    leaving everything else alone makes the shipped code refuse the fixture at
    **8.947 dB** — worse than #1929's 6.087 — so the cancellation, not the
    band, was carrying it.

    Recentring the peak on Fc is what makes the level well defined: a 12 dB
    peak an octave below Fc lives inside the woofer's radiating band and
    outside the tweeter's, so "where do these two drivers sit" has an 8 dB
    band-dependent answer and no level instrument can reconcile it. On Fc both
    estimators see it.

    **The two branches now carry their own halves of the crossover (#2523),
    and that is a fixture DEFECT repaired rather than a threshold re-tuned.**
    Until now both roles were handed the identical UNSHAPED curve — a tweeter
    measuring full output at 200 Hz, three octaves below its own high-pass,
    which no speaker can do. It survived because the defect was symmetric: both
    roles were fitted over the same too-wide band, drew near-identical
    corrections, and so realized matching levels. #2523 fits each role over its
    own band, the symmetry breaks, and the accountability gate correctly
    refused a pair whose tweeter correction was 8 filters of bass cut on a
    driver the crossover already silences. So each branch is built the way
    ``_healthy_crossed_over_pair`` builds its own — the shared shape THROUGH
    that role's half of the matched LR4 — and the shape is retuned to an 8 dB
    peak on a 5 dB, 5-cycle-per-octave comb, which reaches the same branch on
    the same terms: ``reason=improved``, ``after_passed=false``. Measured on
    both sides of #2523 so the fixture is not tuned to the change — 2.233 dB
    pooled residual falling to 1.094 (before) / 1.176 (after), against a
    0.5 dB floor.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    freqs = _LINEARIZABLE_FREQS_HZ
    peak_db = 8.0 * np.exp(-0.5 * ((np.log2(freqs / _FIXTURE_FC_HZ) / 0.4) ** 2))
    comb_db = 5.0 * np.sin(2.0 * np.pi * np.log2(freqs / 200.0) * 5.0)
    shape_db = peak_db + comb_db
    lowpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=False),)
    highpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=True),)
    woofer_db = crossover_response_db(freqs, lowpass) + shape_db
    tweeter_db = crossover_response_db(freqs, highpass) + shape_db
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs,
        (10.0 ** (woofer_db / 20.0)).astype(complex),
        (10.0 ** (tweeter_db / 20.0)).astype(complex),
        _FIXTURE_FC_HZ,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=woofer_db, tweeter_db=tweeter_db,
        trim_db={
            "woofer": round(float(trim_w), 3), "tweeter": round(float(trim_t), 3),
        },
    )
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    assert "event=correction.crossover_v2_prediction_gate" in caplog.text
    assert "reason=improved" in caplog.text
    assert "after_passed=false" in caplog.text
    for ledger_field in (
        "before_rms_db=", "after_rms_db=", "improvement_db=", "required_db=",
    ):
        assert ledger_field in caplog.text


def test_the_candidate_payload_discloses_the_headroom_cost_to_the_household():
    """**S3.** The owner's ruling is that headroom spend is DISCLOSED, not
    limited — and a number that only ever reaches the journal is not disclosed
    to the household that owns the speaker. It rides the same payload the host
    persists and the envelope renders."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    payload = _walk_measure_cloud_to_close(c)

    assert "headroom_cost_db" in payload
    charged = max(
        fit["headroom_cost_db"] for fit in c.candidate.linearization.values()
    )
    assert payload["headroom_cost_db"] == pytest.approx(charged)
    # This fixture's correction is granted boost, so the disclosure is a real
    # number rather than a structurally-zero field.
    assert payload["headroom_cost_db"] > 0.0


def test_a_cut_only_candidate_discloses_a_zero_headroom_cost():
    """The other half: a correction that spends nothing says so, rather than
    omitting the field and leaving the surface to guess."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(c, "_cloud_fit_evidence", lambda combined: None)  # no boost
        payload = _walk_measure_cloud_to_close(c)
    assert payload["headroom_cost_db"] == 0.0


def test_the_browser_candidate_summary_discloses_the_headroom_cost():
    """**SF3.** The owner's ruling is that headroom spend is DISCLOSED, not
    limited — and the conductor's confirm payload is read by the host for
    ``auto_apply`` alone, so a number that stopped there reached the journal
    and nothing else. This is the payload the envelope's own screens read.
    """
    from jasper.web.correction_crossover_v2 import _candidate_summary

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    summary = _candidate_summary(c.candidate)
    assert "headroom_cost_db" in summary
    charged = max(
        fit["headroom_cost_db"] for fit in c.candidate.linearization.values()
    )
    assert summary["headroom_cost_db"] == pytest.approx(charged)
    # This fixture's correction is granted boost, so the disclosure is a real
    # number rather than a structurally-zero field.
    assert summary["headroom_cost_db"] > 0.0


def test_the_browser_summary_discloses_zero_for_a_cut_only_correction():
    """PRESENT and zero, never absent — a surface must not have to guess
    whether the field is missing or the cost is nothing."""
    from jasper.web.correction_crossover_v2 import _candidate_summary

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(c, "_cloud_fit_evidence", lambda combined: None)  # no boost
        _walk_measure_cloud_to_close(c)

    summary = _candidate_summary(c.candidate)
    assert summary["headroom_cost_db"] == 0.0


def test_both_headroom_disclosures_come_from_one_reducer():
    """The conductor's confirm payload and the browser summary answer to
    different readers, so both exist — but two reducers for one
    household-facing number is the drift this ladder removes."""
    from jasper.web.correction_crossover_v2 import _candidate_summary

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    payload = _walk_measure_cloud_to_close(c)

    assert payload["headroom_cost_db"] == pytest.approx(
        _candidate_summary(c.candidate)["headroom_cost_db"]
    )


# --------------------------------------------------------------------------- #
# the fit band and the headroom charge, end to end (#1809, #1808)
# --------------------------------------------------------------------------- #


def test_the_conductor_and_the_emitter_derive_one_set_of_crossover_sections():
    """**One derivation.** The conductor stamps the disclosed
    ``headroom_cost_db`` from these sections and the emitter charges
    ``active_baseline_headroom`` from its own; if the two ever disagreed, the
    number a household is told and the level the speaker gives up would part
    company. They were separate derivations for one review cycle and had
    already drifted on the no-region case — the conductor invented a section
    at the session Fc where the emitter credited none, which makes the
    disclosure SMALLER than the charge: the one direction the ledger promises
    is impossible."""
    from jasper.active_speaker.camilla_yaml import _branch_context

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    emitter = _branch_context(c.candidate.source_preset, {})
    for role in c.candidate.linearization:
        assert _configured_sections(c, role) == emitter[role][0], role


def test_a_role_with_no_crossover_region_is_credited_nothing():
    """…and the no-region case resolves the same way on both sides, because
    both sides ask the same function: no section, so the branch is treated as
    running full range — which is exactly what the emitter would build for it.

    **The "and named" half of this test moved with the fit** (#2291 Phase 2b).
    The ``correction.crossover_v2_linearization_no_crossover`` WARNING is
    emitted by the planner, at the corner of the candidate being planned rather
    than the session's, and is pinned there by
    ``test_crossover_v2_intervention_dual_run.py::
    test_a_role_with_no_crossover_section_is_named_in_the_journal`` (plus its
    positive control). What is left here is the half this module still owns:
    the derivation credits nothing and invents nothing.
    """
    from jasper.active_speaker.branch_chain import sections_by_role

    fakes = FakeSeams()
    c = _conductor(fakes)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(c, "_preset", types.SimpleNamespace(crossover_regions=()))
        assert _configured_sections(c, "woofer") == ()
    # The shared derivation is where that answer comes from — not a branch in
    # the conductor that the emitter would have to mirror.
    assert sections_by_role(()) == {}


def test_an_ordinary_session_banks_no_estimator_finding():
    """The ordinary session mints no level-estimator finding and calls no
    banking seam.

    Pinned because "banks a finding" is a side effect
    (:func:`~jasper.active_speaker.crossover_v2.accountability.
    level_frame_record`) and the cheapest way for it to go wrong is
    to fire unconditionally — which would put a diagnosis in front of every
    household regardless of evidence.

    The check compares the two per-driver estimators and runs on every planned
    candidate; here it finds them inside tolerance. That is the assertion worth
    having: not "the check was skipped" but "the check ran and stayed quiet".
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    banked = fakes.banked_findings
    with pytest.MonkeyPatch.context() as mp:
        plans = _plan_spy(mp)
        _run_phase(c, 1, 1)
        assert _run_phase(c, 2, 2)["accepted"] is True
    consistency = plans[-1].level_consistency
    assert consistency is not None, "both estimators cover a role here"
    assert consistency.differs is False
    assert consistency.worst_delta_db < consistency.tolerance_db
    assert banked == []


def test_no_boost_lands_in_a_drivers_own_crossover_stopband():
    """**#1809, end to end.** Whatever the fit decides, no emitted boost may
    sit where this driver's own crossover has handed off. Cuts are unaffected —
    they remove leakage that still reaches the summed response.

    Held on the conductor rather than only on the fit engine because the
    radiating band is the CONDUCTOR's to solve (it owns the preset's crossover
    regions); a wiring regression here would silently restore the defect with
    the fit engine's own tests still green.

    **Both journey shapes**, since #2106. The guard is rail 2 of the three the
    boost ruling leans on, and the ruling opened a path — a driver-only session
    with no pre-apply cloud — that this test did not previously reach. The
    guard reads the branch's own crossover sections and knows nothing about
    clouds, so it should hold identically; asserting it is what makes that a
    fact rather than an expectation.
    """
    from jasper.active_speaker.branch_chain import radiating_band_hz

    def _cloud_session():
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)
        return c

    def _driver_only_session():
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        c = _conductor(fakes)
        assert PHASE_CLOUD_MEASURE not in c.session_phases
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 2)
        return c

    for label, build in (
        ("cloud", _cloud_session), ("driver-only", _driver_only_session),
    ):
        c = build()
        boosts_seen = False
        for role, fit in c.candidate.linearization.items():
            sections = _configured_sections(c, role)
            lo_hz, hi_hz = radiating_band_hz(sections)
            for f in fit["filters"]:
                if f["gain"] > 0.0:
                    boosts_seen = True
                    assert lo_hz <= f["freq"] <= hi_hz, (label, role, f)
        assert boosts_seen, (
            f"the {label} fixture must emit a boost for this to mean anything"
        )


def test_the_stamped_headroom_cost_is_the_committed_chains_own_peak():
    """One number: what the candidate discloses is what
    ``branch_chain.branch_headroom_db`` returns for the chain the graph will
    actually run — the same filters, the same crossover, and the trim the
    level-match adjudication COMMITTED (not the anchor it might have
    rejected)."""
    from jasper.active_speaker.branch_chain import branch_headroom_db

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    for role, fit in c.candidate.linearization.items():
        assert fit["headroom_cost_db"] == pytest.approx(
            branch_headroom_db(
                fit["filters"],
                sections=_configured_sections(c, role),
                trim_db=c.candidate.role_attenuations_db[role],
            )
        )


def test_the_stamped_disclosure_equals_what_the_emitter_actually_charges():
    """**The edge between the two owners**, and the one a drifted
    role -> sections derivation would break silently.

    The conductor STAMPS each branch's cost onto the candidate; the emitter
    CHARGES ``active_baseline_headroom`` when that candidate is compiled into a
    graph. Nothing else compares them, so this walks the candidate all the way
    to an emitted config and asserts the two numbers are one number — over the
    real preset, the real committed trims, and the real emitted filters.
    """
    from jasper.active_speaker.camilla_yaml import (
        _branch_context, linearization_headroom_db,
    )
    from jasper.active_speaker.linearization_fit import (
        linearization_filters_by_role, worst_headroom_cost_db,
    )

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)
    candidate = c.candidate
    assert worst_headroom_cost_db(candidate.linearization) > 0.0, (
        "the fixture must carry a real charge for this edge to mean anything"
    )

    corrections = {
        role: {"gain_db": float(gain_db)}
        for role, gain_db in candidate.role_attenuations_db.items()
    }
    charged = linearization_headroom_db(
        linearization_filters_by_role(candidate.linearization),
        branch_context=_branch_context(candidate.source_preset, corrections),
    )
    assert charged == pytest.approx(
        worst_headroom_cost_db(candidate.linearization), abs=1e-6
    )


# --- diagnosis-honesty batch: what the instruments disclose ---------------------
#
# Four shipped instruments each stated less than they measured. These pin the
# disclosure, not the physics: the numbers below are fixtures, but the SHAPE of
# what reaches a persisted record or a household screen is the contract.


def test_measure_priors_carry_the_ambient_report_check_measured():
    """#1830 — MEASURE grades its per-driver SNR against CHECK's room floor.

    ``_driver_response`` computes the SNR verdict only when it is handed an
    ambient report, and ``_measure_priors`` used to build priors without one —
    so ``DriverResponse.snr`` was ``None`` on every v2 session ever run while
    the evidence to compute it sat in the same session's ``check.json``.

    Asserted THROUGH the conductor on purpose. ``test_measure_uses_check_
    ambient_for_snr_verdicts`` in the program-analysis suite already pins the
    analyzer half, but it constructs ``MeasurementPriors(ambient_report=...)``
    by hand — which is exactly why it stayed green for the entire life of the
    bug. The production gap was the conductor never putting the report there.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)   # CHECK
    _run_phase(c, 2, 2)   # MEASURE

    measure_priors = next(
        priors for phase, _prog_phase, _result, priors, _geom in fakes.analyzed
        if phase == PHASE_MEASURE
    )
    assert measure_priors.ambient_report == {"bands": [{"level_dbfs": -70.0}]}, (
        "MEASURE must be handed CHECK's measured ambient, or the per-driver "
        "SNR verdict silently never computes"
    )


def test_measure_priors_carry_no_ambient_when_check_never_ran():
    """#1830, the other half: absence stays honest.

    A conductor rehydrated past CHECK (accepted phases + the persisted gain
    plan, which is what lets it compose a MEASURE program without re-running
    CHECK) has no ambient of its own. The report is deliberately NOT persisted
    alongside the gain plan: a noise floor is a claim about this room at this
    mic position, and the §5.6 binding rule restarts any other session at
    CHECK precisely because that position is unverifiable across sessions. So
    the SNR verdict stays absent rather than being graded against a floor
    measured somewhere else.
    """
    fakes = FakeSeams()
    c = _conductor(
        fakes,
        accepted_phases=(PHASE_CHECK,),
        gain_plan_db={"woofer": -11.0, "tweeter": -13.0},
    )
    _run_phase(c, 2, 2)   # MEASURE, with no CHECK consumed by THIS conductor

    measure_priors = next(
        priors for phase, _prog_phase, _result, priors, _geom in fakes.analyzed
        if phase == PHASE_MEASURE
    )
    assert measure_priors.ambient_report is None


def test_verify_diag_names_which_floor_the_gate_landed_on(caplog):
    """#1966 — ``gate_window_ms`` alone cannot say whether anything was gated.

    A window that stops at a found reflection and a window CAPPED at the
    search ceiling because none was found print the same number. Across the
    whole 2026-07-30 corpus every capture was the second state, and the record
    could not say so: the gate computes ``floor_source`` and every v2 consumer
    dropped it.

    This fixture carries no ``capture_integrity`` (a raw ``ProgramAnalysis``),
    so the ROUND refuses it as untrusted evidence with no rollback anchor
    bound (#2537) — asserted first so the disclosure claim below is not read
    as "and so the round kept it". The ``verify_diag`` line's own numbers are
    unaffected by what the round later decides: it is written at VERIFY's own
    capture-gate step, before the round grades anything.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        # 8.0 ms, matching the MEASURE fixture's own window: a SHORTER verify
        # gate is refused by the gate-comparability rule before tracking runs,
        # and this test is about what an accepted capture discloses.
        summed_response=_driver_response_diag(
            "summed", window_ms=8.0, floor_hz=125.0,
            floor_source=gating.FLOOR_SEARCH_BOUND,
        ),
        summed_ripple_db=1.1,
        verify_tracking={
            "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            "tracking_band_hz": [2000.0, 4000.0],
        },
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED

    assert "verify_gate_window_ms=8.0" in caplog.text
    assert f"verify_gate_floor_source={gating.FLOOR_SEARCH_BOUND}" in caplog.text
    # The two states must remain distinguishable values, not two spellings of
    # the same one — that indistinguishability IS the defect.
    assert gating.FLOOR_SEARCH_BOUND != gating.FLOOR_MEASURED


def test_every_retained_position_carries_its_gate_provenance_as_a_sentence():
    """#1966 at the surface. The enum landed first and fixed the record for a
    machine; a person opening the per-position evidence file still had to know
    that ``search_span_bound`` means "nothing was gated out".

    So the sentence rides beside the enum on every retained take — and it is
    RENDERED, not composed here: the copy has exactly one writer, so this
    file and the retained-capture sidecar cannot describe the same gate two
    different ways.
    """
    from jasper.active_speaker.crossover_v2_flow import _gate_disclosure
    from jasper.audio_measurement import gate_disclosure as gd

    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained, phase=PHASE_CLOUD_MEASURE),
        ),
        index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    assert retained, "the walk must have retained positions to check"
    # The allowlist must project it; absent, the field is silently dropped.
    assert all("gate_disclosure" in meta for meta in retained)

    # And the helper renders the two states as opposite claims.
    capped = _gate_disclosure(_driver_response_diag(
        "summed", window_ms=7.0, floor_hz=142.9,
        floor_source=gating.FLOOR_SEARCH_BOUND,
    ))
    found = _gate_disclosure(_driver_response_diag(
        "summed", window_ms=4.0, floor_hz=250.0,
        floor_source=gating.FLOOR_MEASURED,
    ))
    assert "nothing was gated out" in capped
    assert "reflection measured" in found
    assert "nothing was gated out" not in found
    # Rendered by the single writer, byte for byte.
    assert capped == gd.describe_gate(
        {"applied": True, "window_ms": 7.0,
         "floor_source": gating.FLOOR_SEARCH_BOUND}
    )
    assert _gate_disclosure(None) is None


def test_every_retained_position_carries_the_numbers_behind_that_sentence():
    """Ticket 1.5, at the same surface and for the same reason #1966 was.

    The sentence fixed the record for a PERSON; a reader mining the banked
    round for numbers still had to regex English out of it, which the evidence
    packet refuses to do and said so in its ``not_evaluated`` block. So the two
    numbers ride beside the sentence on every retained take — derived by the
    same single typed reader, never assembled here.

    This is the BANKED RECORD's half: what reaches the ``bank_take`` seam is
    ``cloud_position_record``'s own dict, so both keys are on every take
    whatever their values. The separate allowlist between that record and the
    CLOUD artifact's rows (``position_evidence._RECORD_FIELDS``, which drops a
    key whose value is ``None``) is pinned in
    ``tests/test_attribution_persistence.py``.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        _gate_moved_rms_db,
        _gate_reflection_delay_ms,
    )
    from jasper.audio_measurement import gate_disclosure as gd

    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained, phase=PHASE_CLOUD_MEASURE),
        ),
        index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    assert retained, "the walk must have retained positions to check"
    # Present as KEYS on every take, whatever their values: absent-because-
    # unmeasurable and absent-because-nobody-banks-it are different facts and
    # the record must be able to say the first.
    for meta in retained:
        assert "gate_moved_rms_db" in meta
        assert "gate_reflection_delay_ms" in meta

    # …and both are the typed reader's own derivations, on a block that has
    # something to derive. 15.73 - 10.40 = 5.33; the absolute 15.73 must not
    # be what comes back.
    block = _gate_block()
    response = dataclasses.replace(_driver_response_diag("summed"), gating=block)
    typed = gd.build_gate_disclosure(block)
    assert _gate_moved_rms_db(response) == typed.delta_rms_db == 2.59
    assert _gate_reflection_delay_ms(response) == typed.reflection_delay_ms
    assert _gate_reflection_delay_ms(response) == pytest.approx(5.33)
    assert _gate_moved_rms_db(None) is None
    assert _gate_reflection_delay_ms(None) is None


def test_measure_diag_names_the_binding_gate_and_its_floor_source(caplog):
    """#1966 — MEASURE reports the SHORTEST driver window, so it must report
    that same response's floor source, never another response's."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()

    def measure(program):
        analysis = _measure_analysis(program)
        return dataclasses.replace(
            analysis,
            driver_responses=(
                # The binding (shortest) window is the search-bound one.
                _driver_response_diag(
                    "woofer", window_ms=5.0,
                    floor_source=gating.FLOOR_SEARCH_BOUND,
                ),
                _driver_response_diag(
                    "tweeter", window_ms=9.0,
                    floor_source=gating.FLOOR_MEASURED,
                ),
            ),
        )

    fakes.measure = measure
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)

    assert "gate_window_ms=5.0" in caplog.text
    assert f"gate_floor_source={gating.FLOOR_SEARCH_BOUND}" in caplog.text, (
        "the reported floor source must belong to the response whose window "
        "was reported, not to whichever response happened to be first"
    )


def test_verify_pass_states_the_band_it_graded():
    """#1868 — "Verified." must say over what.

    The graded band is not the nominal Fc±1 octave: ``overlap_band_hz`` clamps
    its lower edge up to the tweeter's real sweep floor and ``_analyze_verify``
    clamps it again to the capture's validity floor. It used to ride the
    ``evidence`` block, which the host persists only on a NON-pass outcome — so
    the one screen that says the result is good was the one screen that never
    said what was checked.

    This fixture carries no ``capture_integrity``, so the ROUND refuses it as
    untrusted evidence with no rollback anchor bound (#2537) — asserted first.
    VERIFY's OWN pass/band bookkeeping (``verify_outcome``,
    ``verify_graded_band_hz``) is written at the capture-gate step, ahead of
    round grading, and is unaffected by the round's later refusal.
    """
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        summed_response=_driver_response_diag("summed"),
        summed_ripple_db=1.1,
        verify_tracking={
            "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            "tracking_band_hz": [2000.0, 4000.0],
        },
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED

    assert c.verify_outcome == "pass"
    assert c.verify_graded_band_hz == [2000.0, 4000.0]


def test_a_passing_verify_still_discloses_the_frame_it_compared_across():
    """Rung P1 — "Verified." must say how much of the agreement was frame.

    VERIFY differences an on-axis MODEL against an in-room MEASUREMENT. On the
    2026-07-29 corpus a single −0.79 dB/octave tilt between those two frames
    accounted for 84 % of the flow's apparent prediction error, so a pass with
    the frame unstated invites exactly the reading the panel had to correct.
    Surfaced on a PASS for the same reason the graded band is (#1868): the
    passing screen is the one that would otherwise overclaim.

    This fixture carries no ``capture_integrity``, so the ROUND refuses it as
    untrusted evidence with no rollback anchor bound (#2537) — asserted first,
    same reasoning as the graded-band test above.
    """
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        summed_response=_driver_response_diag("summed"),
        summed_ripple_db=1.1,
        verify_tracking=_tracking_with_frame(),
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED

    assert c.verify_outcome == "pass"
    assert c.verify_frame == {
        "offset_db": -0.75,
        "tilt_db_per_octave": -0.79,
        # The span the fit saw, carried because a two-parameter fit over few
        # bins or a narrow reach is ill-conditioned and the record is the only
        # place a reader can see that. It is also NOT the graded band whenever
        # the prediction has a deep notch — these bins are the ones the
        # comparison trusts.
        "pivot_hz": 2828.4,
        "n_bins": 400,
        "band_hz": [2000.0, 4000.0],
        # Both grades, so no screen can render the tilt-removed half alone.
        "rms_db_raw": 0.4,
        "max_db_raw": 0.9,
        "rms_db_tilt_removed": 0.18,
        "max_db_tilt_removed": 0.31,
    }


def test_an_unfitted_frame_is_disclosed_as_absent_never_as_agreement():
    """A comparison whose frame could not be measured says nothing, rather than
    reporting a flat frame — absence and "the frames matched" are different
    claims and must not collapse into one.

    This fixture carries no ``capture_integrity``, so the ROUND refuses it as
    untrusted evidence with no rollback anchor bound (#2537) — asserted first,
    same reasoning as the two frame/band tests above. ``verify_frame`` is
    written at VERIFY's own capture-gate step and is unaffected by the round's
    later refusal.
    """
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        summed_response=_driver_response_diag("summed"),
        summed_ripple_db=1.1,
        verify_tracking=_tracking_with_frame(
            offset_db=None, tilt_db_per_octave=None, pivot_hz=None, n_bins=0,
            band_hz=None, tilt_removed={"rms_db": None, "max_db": None},
        ),
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED

    assert c.verify_frame is None


def test_a_verify_that_graded_nothing_claims_no_frame():
    """An early refusal compared nothing, so it spanned no frame — and a prior
    attempt's frame must not leak into this one (the same reset discipline the
    graded band carries)."""
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep", confidence=0.05),),
        summed_response=_driver_response_diag("summed"),
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    assert _run_phase(c, 3, 3)["accepted"] is False

    assert c.verify_frame is None


def test_a_verify_that_graded_nothing_claims_no_band():
    """#1868 — an early refusal graded nothing, and says nothing.

    Absence must mean "no comparison happened", never "checked everywhere",
    and a previous attempt's band must not leak into this one.
    """
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep", confidence=0.05),),
        summed_response=_driver_response_diag("summed"),
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    assert _run_phase(c, 3, 3)["accepted"] is False

    assert c.verify_graded_band_hz is None


# --------------------------------------------------------------------------- #
# #1967 — the boost gate's evidence claim, made substantive
# --------------------------------------------------------------------------- #


def test_boost_exclusions_come_from_the_blind_span_below_the_registry_floor(caplog):
    """#1967. The registry's band is floored at ``ECHO_BAND_HF_REGIME_FLOOR_HZ``,
    so it contributes no exclusions below it — the gate's "null-exclusion stays
    a measured, registry-gated fact" is unbacked there. This is the check that
    backs it: dips the cloud's own positions disagree about are withheld from
    the LIFT vocabulary.

    The disclosure is asserted alongside the value because a bound that
    silently narrows a correction is the shape this whole area is trying to
    stop shipping.
    """
    c = _cloud_conductor(FakeSeams())
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    bands = c._boost_excluded_bands_hz(
        _moving_notch_cloud([1800.0] * 5 + [2400.0] * 3), _BLIND_SPAN_RESULT,
    )

    floor_hz = c._cloud_echo_band.band_hz[0]
    assert bands, "a cloud whose positions disagree must offer something"
    # Every offered band sits inside the span the registry could not reach:
    # above the cloud's own validity floor, below the registry's lower edge.
    assert all(1200.0 <= lo < hi <= floor_hz for lo, hi in bands), (bands, floor_hz)
    assert "event=correction.crossover_v2_boost_evidence" in caplog.text
    assert "registry_reason=no_corroborating_arrivals" in caplog.text
    assert f'unadjudicated_span_hz="[1200.0, {floor_hz}]"' in caplog.text
    # A withhold is WARNING, not INFO: it silently narrows a correction, so it
    # has to reach a journal a household's operator actually reads.
    withheld = [
        r for r in caplog.records
        if "crossover_v2_boost_evidence" in r.getMessage()
    ]
    assert withheld and all(r.levelno == logging.WARNING for r in withheld)


def test_a_cloud_whose_positions_agree_loses_no_boost(caplog):
    """The owner's ruling, executable: this bound withholds on CONTRADICTING
    evidence and never on absent or agreeing evidence.

    Eight positions notched at the same frequency read invariant, so nothing is
    offered — the +8.06 dB at 3633.6 Hz that motivated #1967 sits in exactly
    this class and keeps flowing. It is still disclosed, because "the registry
    could not look here" stays true whatever the check found.
    """
    c = _cloud_conductor(FakeSeams())
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    bands = c._boost_excluded_bands_hz(
        _moving_notch_cloud([1800.0] * 8), _BLIND_SPAN_RESULT,
    )

    assert bands == ()
    assert "event=correction.crossover_v2_boost_evidence" in caplog.text
    assert "boost_excluded_bands_hz=[]" in caplog.text
    # The dip WAS seen — it just did not contradict a boost. A reader must be
    # able to tell that from "nothing was measured".
    assert "n_dips=1 n_position_dependent=0" in caplog.text
    # ...and withholding nothing is INFO. Only a narrowed correction earns a
    # WARNING, or the level stops carrying information.
    kept = [
        r for r in caplog.records
        if "crossover_v2_boost_evidence" in r.getMessage()
    ]
    assert kept and all(r.levelno == logging.INFO for r in kept)


def test_the_boost_bound_fails_open_when_it_cannot_be_computed(caplog):
    """Failing CLOSED would blanket-ban boost below 4 kHz on a numeric hiccup,
    which is the blunt gate this function exists to avoid. Both unusable-input
    shapes yield today's permission exactly, and say so."""
    c = _cloud_conductor(FakeSeams())
    combined = _moving_notch_cloud([1800.0] * 5 + [2400.0] * 3)

    # No blind span at all: the cloud's validity floor is already above the
    # registry's own floor, so nothing was hidden.
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    assert c._boost_excluded_bands_hz(
        combined, {"validity_floor_hz": 9000.0, "null_registry": {}},
    ) == ()
    assert "variance_reason=no_blind_span" in caplog.text

    # And an unexpected failure inside the check is caught, disclosed at
    # WARNING, and leaves the permission where it was.
    caplog.clear()
    with pytest.MonkeyPatch.context() as mp:
        import jasper.audio_measurement.interference_nulls as nulls

        def _boom(*a, **k):
            raise RuntimeError("synthetic")

        mp.setattr(nulls, "classify_dip_position_variance", _boom)
        assert c._boost_excluded_bands_hz(combined, _BLIND_SPAN_RESULT) == ()
    assert "event=correction.crossover_v2_boost_variance_failed" in caplog.text
    assert "variance_reason=variance_check_failed" in caplog.text


def test_the_boost_evidence_disclosure_is_reached_by_an_ordinary_walk(caplog):
    """Reachability, without a monkeypatch anywhere.

    The wiring test below stubs the composer to prove the vocabulary carries
    what it returns; that says nothing about whether the composer is CALLED on
    the production path. This walks the real cloud group to close and asserts
    the real disclosure fired — so a future refactor that leaves
    ``_boost_excluded_bands_hz`` orphaned fails here rather than shipping a
    bound nothing invokes.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    disclosures = [
        r for r in caplog.records
        if "event=correction.crossover_v2_boost_evidence" in r.getMessage()
    ]
    assert len(disclosures) == 1, [r.getMessage()[:80] for r in disclosures]
    message = disclosures[0].getMessage()
    # The span it reports is the real one: this cloud's own validity floor up
    # to the registry band's real lower edge, not a placeholder.
    assert f'unadjudicated_span_hz="[100.0, {c._cloud_echo_band.band_hz[0]}]"' in message


def test_per_filter_boost_verdicts_are_disclosed_by_the_conductor(caplog):
    """``linearization_fit`` is pure computation and owns no logger, so the
    per-filter verdicts only become observable if the conductor emits them.

    Walks the real cloud group with an exclusion band placed over the boost
    the fake session's woofer actually attracts, and asserts the drop reaches
    the journal with the arithmetic that caused it. A bound that silently
    removes a correction is the failure mode this whole area exists to avoid.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow.CrossoverV2Session, "_boost_excluded_bands_hz",
            lambda self, combined, result: ((350.0, 450.0),),
        )
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    verdicts = [
        r for r in caplog.records
        if "event=correction.crossover_v2_boost_excluded_verdicts" in r.getMessage()
    ]
    assert verdicts, "the per-filter verdicts never reached the journal"
    dropped = [r for r in verdicts if "realized_in_band_db" in r.getMessage()]
    assert dropped
    # A drop narrows a correction, so it is a WARNING.
    assert all(r.levelno == logging.WARNING for r in dropped)
    assert "band_hz" in dropped[0].getMessage()


def test_the_fit_vocabulary_actually_carries_the_cloud_s_boost_exclusions():
    """The wiring, end to end at the conductor's own surface: what
    ``_boost_excluded_bands_hz`` composes is what ``fit_driver_linearization``
    is handed. Without this the bound could be computed, logged, and dropped.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    seen: list[tuple[tuple[float, float], ...]] = []
    real_fit = iv.fit_driver_linearization

    def _spy(resp, envelope, **kwargs):
        seen.append(kwargs["vocabulary"].boost_excluded_bands_hz)
        return real_fit(resp, envelope, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _spy)
        mp.setattr(
            flow.CrossoverV2Session, "_boost_excluded_bands_hz",
            lambda self, combined, result: ((1500.0, 1900.0),),
        )
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    assert seen and all(bands == ((1500.0, 1900.0),) for bands in seen)


# --------------------------------------------------------------------------- #
# R18 — honest post-apply verification (issues #1868 / #1654)
# --------------------------------------------------------------------------- #
#
# The numbers in these records are SYNTHETIC and labelled so — no hardware
# measurement is restated as a fixture value. The journal-verified fact they DO
# reproduce is the graded band: ``tracking_band_lo_hz=2000.0`` on a box whose
# tweeter is swept from Fc.


def test_absolute_miss_remains_independent_when_integration_passes():
    """An evaluated target miss is a result, not a failed capture."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=1.398262557, verify_absolute=_absolute(
            4.3139, worst_db=-4.3139, worst_hz=1590.4083,
        ),
    )
    verdict = _run_phase(c, 3, 3)

    assert verdict["accepted"] is True
    assert verdict.get("code") in {None, ""}
    assert c.verify_outcome == "pass"
    claims = c.verify_claims
    assert claims["integration"]["status"] == CLAIM_PASS  # the model agreed
    assert claims["absolute"]["status"] == CLAIM_FAIL
    assert claims["absolute"]["tolerance_db"] == 2.0
    assert claims["absolute"]["max_db"] == 4.3139
    assert claims["absolute"]["worst_hz"] == 1590.4083


def test_the_same_capture_passed_before_the_absolute_claim_existed():
    """The other direction of the mutation, at the conductor: the identical
    tracking evidence with NO crossover-region record still passes. So the new
    verdict is what changed the answer, not some unrelated tightening."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.069)
    assert _run_phase(c, 3, 3)["accepted"] is True
    assert c.verify_outcome == "pass"
    assert c.verify_claims["absolute"]["status"] == CLAIM_NOT_EVALUATED


def test_absolute_claim_inside_tolerance_passes_and_still_reports_its_numbers():
    """A passing handoff is disclosed, not silent."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.9, verify_absolute=_absolute(0.69),
    )
    assert _run_phase(c, 3, 3)["accepted"] is True
    absolute = c.verify_claims["absolute"]
    assert absolute["status"] == CLAIM_PASS
    assert absolute["max_db"] == 0.69
    assert absolute["band_hz"] == [1000.0, 4000.0]


def test_not_evaluated_claims_never_gate_and_keep_the_kernels_own_reason():
    """Refusing on a measurement nobody made is the same dishonesty pointed
    the other way — and a re-labelled reason erases which one it was."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.9,
        verify_absolute={"not_evaluated": "no_trusted_crossover_region"},
    )
    assert _run_phase(c, 3, 3)["accepted"] is True
    absolute = c.verify_claims["absolute"]
    assert absolute["status"] == CLAIM_NOT_EVALUATED
    assert absolute["reason"] == "no_trusted_crossover_region"


def test_an_ungradeable_tracking_claim_discloses_instead_of_refusing():
    """#3487, witnessed live: the documented recovery could not be receipted.

    ``POST /crossover/v2/republish`` by fingerprint then ``--apply`` is the
    runbook's own way back from any restore, including one the adoption table
    got wrong (#3485). But a republished candidate has no measure round behind
    it, so the verify's TRACKING claim has nothing to track against and
    ``max_db_notch_excluded`` is absent. The verdict collapsed that into
    ``verify_out_of_tolerance`` and refused index 1 four times — the whole retry
    budget, four rounds of audible playback — while the ABSOLUTE claim passed at
    1.503 dB. No capture was ever accepted, so no round graded and no receipt
    could mint: every rig, every republish.

    Same principle as the absolute claim's own pin above, pointed at the other
    half of §7's third claim — *refusing on a measurement nobody made is the
    same dishonesty pointed the other way*. R18's three-valued vocabulary
    already had the honest word and the claim record was already using it; only
    the gate was still two-valued. It is also the republish door's own declared
    contract, restored: ``handle_v2_republish`` clears ``verify_priors`` on
    purpose and says the consequence is that *a post-apply VERIFY of a
    republished candidate grades INDETERMINATE, never a false pass* — which a
    refusal is not either.

    The subject is the GATE's answer, which is what carried the wrong name. What
    the round then makes of an unavailable realization is the trust axis's own
    question and has its own pins.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=None, verify_absolute=_absolute(1.503),
    )
    _run_phase(c, 3, 3)

    assert c.verify_outcome == "pass"
    assert c.verify_code is None
    assert c.verify_claims["integration"]["status"] == CLAIM_NOT_EVALUATED
    # Never a pass either: the claim is on the record as ungraded, and the
    # number it would have carried stays absent rather than becoming 0.0.
    assert c.verify_claims["integration"]["max_db"] is None
    assert c.verify_claims["absolute"]["status"] == CLAIM_PASS


@pytest.mark.parametrize(
    ("verify_absolute", "badged"),
    [
        pytest.param(_absolute(1.503), True, id="absolute_graded"),
        pytest.param(
            {"not_evaluated": "no_trusted_crossover_region"}, False,
            id="nothing_graded",
        ),
    ],
)
def test_the_mark_badge_needs_a_claim_that_was_actually_graded(
    verify_absolute, badged,
):
    """The corner of the pin above: a capture that graded NOTHING.

    Accepting an ungradeable tracking claim (#3487) is what makes this
    reachable — and when the same capture also finds no trusted crossover
    region, the absolute claim is ``not_evaluated`` too, so the accepted
    VERIFY carries four claims and not one verdict. The badge over it must
    then not be the one that means *verified at the mark*: the republish
    door's own contract, which is where this shape comes from, is that such a
    VERIFY grades INDETERMINATE and never a false pass.

    The first case is the witnessed one and is unchanged — one claim graded,
    none failed, badge at the mark. What separates the two is not the
    ``outcome``, which is a ``pass`` in both: it is whether any claim was
    graded at all.
    """
    from jasper.web.correction_crossover_v2 import (
        GRADE_INCONCLUSIVE,
        GRADE_MARK_VERIFIED,
        _post_apply_grade,
    )

    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=None, verify_absolute=verify_absolute,
    )
    _run_phase(c, 3, 3)

    assert c.verify_outcome == "pass"
    assert c.verify_claims["integration"]["status"] == CLAIM_NOT_EVALUATED
    grade = _post_apply_grade({
        "applied": True,
        "verify": {"outcome": c.verify_outcome, "claims": c.verify_claims},
    })

    assert grade["state"] == (GRADE_MARK_VERIFIED if badged else GRADE_INCONCLUSIVE)
    assert grade["graded"] is badged


def test_a_tracking_claim_that_missed_its_tolerance_still_refuses():
    """The control for the pin above, and the reason the refusal keeps its name.

    ``verify_out_of_tolerance`` now fires only where a tracking max was
    MEASURED and cleared the tolerance — which is what the code has always
    said, and what it did not always mean.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)

    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_out_of_tolerance"
    assert c.verify_claims["integration"]["status"] == CLAIM_FAIL


def test_per_branch_claims_are_named_not_evaluated_never_silently_claimed():
    """§7 names three claims; VERIFY plays ONE summed sweep, so two have no
    evidence. R18 does not widen the capture plan — it refuses to let
    "Verified." imply those two were proved."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.9, verify_absolute=_absolute(0.5),
    )
    assert _run_phase(c, 3, 3)["accepted"] is True
    claims = c.verify_claims
    for name in ("woofer_branch", "hf_branch"):
        assert claims[name] == {
            "status": CLAIM_NOT_EVALUATED, "reason": CLAIM_NO_PER_BRANCH_CAPTURE,
        }


def test_claims_reset_on_an_early_return_so_no_stale_claim_leaks():
    """Same discipline as the graded band and the frame beside them: an early
    refusal graded nothing and must not surface a prior attempt's claims."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.9, verify_absolute=_absolute(0.5),
    )
    _run_phase(c, 3, 3)
    assert c.verify_claims is not None
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0)
    _run_phase(c, 3, 4)
    assert c.verify_outcome == "inconclusive"
    # Absent, not stale — "nothing was graded" is the honest record here, and
    # every consumer renders absence as silence rather than as a pass.
    assert c.verify_claims is None


def test_absolute_tolerance_is_derived_from_the_spec_table_not_chosen():
    """The threshold has no literal of its own: it is the loosest
    ``flat_spec.SPEC_BANDS`` entry the crossover region overlaps, so revising
    that table with hardware data moves this without a second edit."""
    from jasper.active_speaker import flat_spec

    assert verify_absolute_tolerance_db([1000.0, 4000.0]) == max(
        tol for lo, hi, tol in flat_spec.SPEC_BANDS if lo < 4000.0 and 1000.0 < hi
    )
    # It is NOT the model-tracking tolerance wearing a different name.
    assert verify_absolute_tolerance_db([1000.0, 4000.0]) != flow.VERIFY_TOLERANCE_DB
    # A region the spec table declines to grade yields no bar at all, and the
    # claim is recorded not-evaluated rather than held to an invented one.
    assert verify_absolute_tolerance_db([17_000.0, 20_000.0]) is None
    assert verify_absolute_tolerance_db([1000.0]) is None


def test_the_delta_probe_still_refuses_first_so_its_rollback_is_never_displaced():
    """R18 is purely additive to the refusal order (resilience review finding).

    A probe-class refusal carries an AUTOMATIC remedy — the graph comes off.
    Gating ahead of it would let a capture that fails this claim AND warrants a
    rollback get neither.

    Injected at ``_grade_round_once``: since the fifth-principle routing the
    probe reports and the ROUND decides, so "the probe's refusal" reaches this
    ordering as the round's. The subject is unchanged — R18's absolute claim
    must not displace it.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.069, verify_absolute=_absolute(3.98),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow.CrossoverV2Session, "_grade_round_once",
            lambda self, verdict: flow.PhaseVerdict(
                False, REASON_CORRECTION_MODEL_ERROR,
            ),
        )
        verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == REASON_CORRECTION_MODEL_ERROR
    # The claim was still GRADED and still says it failed — the ordering
    # decides which refusal is reported, never whether the claim was made.
    assert c.verify_claims["absolute"]["status"] == CLAIM_FAIL


def test_the_crossover_region_claim_is_not_the_cloud_flatness_gauge():
    """SSOT: the two absolute grades are NOT peers, for a structural reason.

    ``assemble_cloud_group_result``'s ``flatness`` cannot own §7 claim 3 — it
    is assembled at group close, AFTER this verdict, and never exists at all
    on a session with no post-apply cloud. Pins that the crossover-region
    verdict stands on a capture the cloud has not contributed to, so a future
    consolidation cannot quietly delete the claim on cloudless paths.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.069, verify_absolute=_absolute(3.98),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    # No cloud has closed on this conductor, so no flatness gauge exists —
    # and the §7 claim was still made and still failed.
    assert c.group_cloud_result(PHASE_CLOUD_VERIFY) is None
    assert c.verify_claims["absolute"]["status"] == CLAIM_FAIL


def test_verify_diag_names_every_claim_and_the_crossover_region_numbers(caplog):
    """The operator's grep target carries the whole claim record — including
    the two nobody graded — so a corpus sweep counts what was judged instead
    of inferring it from a bare ``accepted=true``."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.069, verify_absolute=_absolute(3.98),
    )
    with caplog.at_level(logging.INFO):
        _run_phase(c, 3, 3)
    line = next(
        r.message for r in caplog.records
        if "correction.crossover_v2_verify_diag" in r.message
    )
    assert f"woofer_branch:not_evaluated({CLAIM_NO_PER_BRANCH_CAPTURE})" in line
    assert f"hf_branch:not_evaluated({CLAIM_NO_PER_BRANCH_CAPTURE})" in line
    assert "integration:pass" in line
    assert "absolute:fail" in line
    assert "absolute_worst_hz=1700.0" in line
    assert "absolute_tolerance_db=2.0" in line
    assert "absolute_band_lo_hz=1000.0" in line


def _walk_a_session_whose_filters_reach_the_blind_zone(caplog):
    """The #2523 two-branch fixture, which places real filters inside its own
    per-branch measurement hole (1255.8-2020.0 Hz).

    Shares its shape with
    ``test_prediction_gate_logs_the_improved_path_with_both_terms`` — an 8 dB
    peak on a 5 dB comb, each branch carrying its own half of a matched LR4 —
    because that is a fixture already established to drive real filters into
    the crossover region rather than one built to make this emit fire.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    freqs = _LINEARIZABLE_FREQS_HZ
    peak_db = 8.0 * np.exp(-0.5 * ((np.log2(freqs / _FIXTURE_FC_HZ) / 0.4) ** 2))
    comb_db = 5.0 * np.sin(2.0 * np.pi * np.log2(freqs / 200.0) * 5.0)
    shape_db = peak_db + comb_db
    lowpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=False),)
    highpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=True),)
    woofer_db = crossover_response_db(freqs, lowpass) + shape_db
    tweeter_db = crossover_response_db(freqs, highpass) + shape_db
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs,
        (10.0 ** (woofer_db / 20.0)).astype(complex),
        (10.0 ** (tweeter_db / 20.0)).astype(complex),
        _FIXTURE_FC_HZ,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=woofer_db, tweeter_db=tweeter_db,
        trim_db={
            "woofer": round(float(trim_w), 3), "tweeter": round(float(trim_t), 3),
        },
    )
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)
    return c


def _records(caplog, event: str):
    return [
        r for r in caplog.records if f"event={event}" in r.getMessage()
    ]


def test_blind_zone_placements_reach_the_journal(caplog):
    """#2599 SF1. ``linearization_fit`` owns no logger, so a verdict it makes
    is only a verdict if this loop says it out loud.

    The adversarial gate's words: "the verb only differs from silence if
    something emits it." Rule 2 deliberately DISCLOSES rather than refuses, so
    a disclosure nothing surfaces makes the whole adjudication empty — the
    filter ships and no one is told, which is exactly the pre-#2599 state the
    round-3 receipt was in.
    """
    c = _walk_a_session_whose_filters_reach_the_blind_zone(caplog)

    records = _records(caplog, "correction.crossover_v2_blind_zone_placements")
    assert records, "the blind-zone placements never reached the journal"
    message = records[0].getMessage()
    # Self-contained: what was placed, and the hole it was placed in, so a
    # reader needs no second journal line to interpret it.
    assert "role=" in message
    assert "placed=" in message
    assert "measured_excess_db" in message
    assert "freq_hz" in message
    # The TOP-LEVEL ``blind_bands_hz`` must carry the session's holes, and
    # this is asserted on that field's own rendered value rather than on the
    # message as a whole. Every hole's edges also appear inside each
    # ``placed`` record, so a substring search over the line passes with the
    # top-level field emptied -- it is the one field that makes the record
    # self-contained (and that lists holes no filter landed in), so it needs
    # its own assertion.
    field = message.split("blind_bands_hz=", 1)[1].strip().strip('"')
    assert field not in ("[]", ""), f"blind_bands_hz carried nothing: {field!r}"
    holes = {
        tuple(placement["blind_band_hz"])
        for fit in c.candidate.linearization.values()
        for placement in (fit.get("blind_zone_placements") or [])
    }
    assert holes
    for lo_hz, hi_hz in holes:
        assert str(lo_hz) in field and str(hi_hz) in field


def test_the_blind_zone_emit_severity_tracks_whether_level_was_added(caplog):
    """Severity carries the distinction the disclosure exists to make.

    A cut in a hole removes level on evidence no branch has; a BOOST adds
    level into the phase-sensitive blend on the same absent evidence — the
    class the gate's 400-fit probe found shipping unnamed. Cuts-only is INFO,
    any positive gain is WARNING. Neither gates anything; both ship.
    """
    c = _walk_a_session_whose_filters_reach_the_blind_zone(caplog)

    records = _records(caplog, "correction.crossover_v2_blind_zone_placements")
    assert records
    # Re-derive the expectation from the CANDIDATE rather than by parsing the
    # log line, so this pins the emit's rule and not its formatting.
    by_role = {
        role: fit["blind_zone_placements"]
        for role, fit in c.candidate.linearization.items()
        if fit.get("blind_zone_placements")
    }
    assert by_role, "the candidate carried no placements to emit"
    assert len(records) == len(by_role)
    for record in records:
        message = record.getMessage()
        role = next(r for r in by_role if f"role={r}" in message)
        added_level = any(p["gain_db"] > 0.0 for p in by_role[role])
        assert record.levelno == (
            logging.WARNING if added_level else logging.INFO
        ), message[:200]


def test_a_refused_boost_reaches_the_journal_as_a_warning(caplog):
    """#2599 SF1, rule-1 half. A refusal nothing emits is a SILENT refusal —
    the failure mode this whole area exists to avoid, and the one the #1967
    block beside it was written for.

    The fit is wrapped rather than coaxed. Probed 2026-08-16: no conductor
    fixture in this suite tripped the measured-target bound — every role
    reported zero drops — so a test that merely walked one would have asserted
    ``0 == 0`` and passed while the emit was deleted. That is the vacuity trap
    this repo has been bitten by, so the drop is INJECTED into an otherwise
    real fit and the assertion is that the conductor SAYS it. Should a fixture
    later trip the bound for real, this test keeps working and
    ``test_the_new_verdict_events_stay_silent_on_an_ordinary_session`` is what
    tracks the count. The bound's own behaviour — when a drop is produced at
    all — is pinned in
    ``tests/test_active_speaker_linearization_fit.py``; this pins the wiring.
    """
    from dataclasses import replace

    from jasper.active_speaker.linearization_fit import BoostEvidenceDrop

    injected = BoostEvidenceDrop(
        freq_hz=434.01678699822264, q=1.0, gain_db=2.0149,
        action_band_hz=(301.8, 472.0), measured_excess_db=2.7819,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    real_fit = iv.fit_driver_linearization

    def _with_a_refused_boost(resp, envelope, **kwargs):
        return replace(
            real_fit(resp, envelope, **kwargs),
            lift_boost_evidence_drops=(injected,),
            lift_suppressed_reason="boost_above_measured_target",
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _with_a_refused_boost)
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    records = _records(
        caplog, "correction.crossover_v2_boost_measured_target_verdicts"
    )
    assert records, "a refused boost never reached the journal"
    assert len(records) == len(c.candidate.linearization)
    for record in records:
        message = record.getMessage()
        # A refusal narrows a correction, so it is always a WARNING -- unlike
        # the #1967 block, this event has no accepted-remainder case.
        assert record.levelno == logging.WARNING
        # The arithmetic that caused it, so a reader re-derives rather than
        # trusts: which filter, over what span, against what evidence.
        assert "434.0167" in message
        assert "action_band_hz" in message
        assert "measured_excess_db" in message
        assert "boost_above_measured_target" in message


def test_the_new_verdict_events_stay_silent_on_an_ordinary_session(caplog):
    """The other direction, so neither emit becomes journal spam.

    A session where the bound refused nothing and no filter landed in a hole
    must add no lines at all. Asserted against the CANDIDATE's own fields, so
    this cannot pass by the events being unreachable — it fails if the fields
    are populated and the emits are missing, and it fails if the emits fire
    when the fields are empty.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    for field, event in (
        ("lift_boost_evidence_drops",
         "correction.crossover_v2_boost_measured_target_verdicts"),
        ("blind_zone_placements",
         "correction.crossover_v2_blind_zone_placements"),
    ):
        populated = [
            role for role, fit in c.candidate.linearization.items()
            if fit.get(field)
        ]
        assert len(_records(caplog, event)) == len(populated), field


def test_a_hole_centred_BOOST_makes_the_blind_zone_emit_a_warning(caplog):
    """The severity rule's other half — the one the shipped fixtures cannot
    reach, so it is injected rather than left unpinned.

    Every conductor fixture that reaches the blind zone places only CUTS
    there (probed), so a test that merely walked one would exercise the INFO
    branch and pass with the ``WARNING if any(gain > 0)`` conjunct deleted.
    That is the half-guarded-site trap. A boost in a hole is the class the
    gate's 400-fit probe found shipping unnamed and the one this disclosure
    most exists for — adding level into a phase-sensitive blend on evidence
    no branch has — so it gets the louder level, and that has to be pinned by
    something.

    The fit-side behaviour (a hole-centred boost really is named) is pinned
    for real in ``test_a_hole_centred_lift_boost_is_named_too``; this pins the
    emit's severity rule given such a placement.
    """
    from dataclasses import replace

    from jasper.active_speaker.linearization_fit import BlindZonePlacement

    injected = BlindZonePlacement(
        freq_hz=1404.4032452955714, q=2.0, gain_db=+1.5,
        blind_band_hz=(1291.4104702195973, 2077.2411784104297),
        measured_excess_db=-2.0,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    real_fit = iv.fit_driver_linearization

    def _with_a_hole_centred_boost(resp, envelope, **kwargs):
        return replace(
            real_fit(resp, envelope, **kwargs),
            blind_zone_placements=(injected,),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _with_a_hole_centred_boost)
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    records = _records(caplog, "correction.crossover_v2_blind_zone_placements")
    assert records, "a hole-centred boost never reached the journal"
    assert len(records) == len(c.candidate.linearization)
    for record in records:
        assert record.levelno == logging.WARNING, record.getMessage()[:200]
        assert "1404.4032" in record.getMessage()


def test_a_correctly_levelled_pair_clears_the_improvement_floor(
    caplog, monkeypatch,
):
    """This fixture SHIPS again, and the reason is that it now levels exactly.

    **History, because this assertion has been inverted once before.** Until
    #2609 this fixture shipped. #2609 deleted PR-L5's ``level_frame_offset_db``
    and it began REFUSING under item 2's 0.5 dB material-improvement floor —
    correctly at the time, since the offset had been flattering the prediction.
    The band-matched give-back now lands the pair exactly level, the prediction
    is honest on its own terms, and it clears the floor without help.

    **The mechanism, measured on this fixture.** The two give-backs disagree by
    0.918 dB here, and in the OPPOSITE direction to the jts3 horn that motivated
    the fix::

        level-band (anchor)   woofer 1.147   tweeter 2.064
        core-band             woofer 2.104   tweeter 1.186
        raw trim              woofer 0.0     tweeter -1.773
        committed OLD (core)  woofer 0.0     tweeter -2.691   <- 1.835 dB DULL
        committed NEW (level) woofer 0.0     tweeter -0.856   <- realized 0.0

    That two-way behaviour is itself evidence about the defect's nature: a band
    MISMATCH mis-levels in whichever direction the correction's energy happens
    to sit relative to the graded span — hot on a horn whose shelf lives above
    it, dull here. A sign error could not do that.

    So the candidate the old rule refused was not a bad candidate; it was a
    correctly-shaped candidate whose predicted improvement was being scored
    against a pair mis-levelled 1.835 dB dull. Refusing it was the floor doing
    its job on a corrupted input.

    **What this test no longer covers, and where that lives instead.** It no
    longer exercises the floor's failing arm. That arm keeps its coverage at
    the layer that owns the decision —
    ``test_crossover_v2_accountability.py`` pins the
    ``LEDGER_NOT_AN_IMPROVEMENT`` verdict and the disclosure that rides with
    it. (Until the nanny burn-down that arm REFUSED, and the same test pinned
    ``decision.refusal_reason``.) Nothing was dropped by flipping this one; it
    was always a FIXTURE-flip test, and the fixture flipped back for a reason
    worth stating.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)

    # Force the ANCHOR to be the committed pair, which is the population this
    # is scoped to: a wild scan drift trips the sanity guard and the anchored
    # pair ships, so what the anchor computes is what gets predicted.
    monkeypatch.setattr(
        iv, "solve_ripple_optimal_trim",
        lambda *a, **k: (k["seed_trim_db"] - 20.0, 0.0, k["seed_trim_db"]),
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)

    # The floor is CLEARED, not merely un-enforced. (Before the nanny burn-down
    # this asserted the absence of a refusal code, which no longer
    # discriminates — nothing refuses on this path any more.) On this fixture
    # the levelled pair clears it a fortiori: the prediction meets the flat
    # spec outright, so the gate settles at ``predicted_in_spec`` without ever
    # needing an improvement argument. What must never appear is the failing
    # verdict the mis-levelled pair produced.
    _run_phase(c, 2, 2)
    assert (
        c.measure_predicted_spec_report["comparison"]["reason"]
        == accountability.LEDGER_PREDICTED_IN_SPEC
    )

    # Leg 1 — WHY it ships: the committed pair lands EXACTLY level. This is the
    # band-matched give-back's invariant showing up end-to-end in the planner,
    # not just in its unit test.
    assert "event=correction.crossover_v2_realized_level_match" in caplog.text
    assert "matched=true" in caplog.text
    assert "difference_db=0.0" in caplog.text

    # Leg 2 — the two give-backs, side by side, showing the 0.918 dB this
    # fixture's bands disagree by. The old anchor spent the core-band pair and
    # committed the tweeter at -2.691; the level-band pair commits -0.856.
    assert (
        'level_band_giveback_db="{\'woofer\': 1.147, \'tweeter\': 2.064}"'
        in caplog.text
    )
    assert (
        'core_band_giveback_db="{\'woofer\': 2.104, \'tweeter\': 1.186}"'
        in caplog.text
    )
    assert 'anchored_trim_db="{\'woofer\': 0.0, \'tweeter\': -0.856}"' in caplog.text
    # The precondition's instrumentation, on the ORDINARY path: this fixture's
    # base IS the band-average solve, so the polish delta is zero and the
    # invariant holds exactly. Pinned here for the zero case only — a literal
    # zero cannot distinguish "measured zero" from "hard-coded zero", so the
    # value assertion that kills that mutation lives where a NON-zero delta can
    # be driven (``test_the_journal_reports_the_polish_delta_it_measured`` in
    # test_crossover_v2_intervention_dual_run.py, mutation-verified).
    assert (
        'band_average_trim_db="{\'woofer\': 0.0, \'tweeter\': -1.773}"' in caplog.text
    )
    assert 'polish_delta_db="{\'woofer\': 0.0, \'tweeter\': 0.0}"' in caplog.text

    # Leg 3 — the prediction gate ran and passed on its own terms.
    assert "event=correction.crossover_v2_prediction_gate" in caplog.text
    assert "after_passed=true" in caplog.text


@dataclasses.dataclass(frozen=True)
class _MarginMatch:
    """The one field ``decide_trim`` reads off a realized-level match."""

    difference_db: float


#: The two ULPs of one nominal anchor. Both print as "-2.691" on every surface
#: that rounds — including the guard's own journal line, which is why the CI
#: log showed ``drift_db=6.0 margin_db=6.0`` beside a rejection — and they
#: re-derive ``abs((anchor - 6.0) - anchor)`` on OPPOSITE sides of the margin:
#: 5.999999999999999 and 6.000000000000001. Measured, not chosen.
_MARGIN_ANCHOR_UNDER_ULP = -2.691
_MARGIN_ANCHOR_OVER_ULP = -2.6910000000003


def _trim_at_exactly_the_margin(anchor_db: float):
    """``decide_trim`` on a scan that drifted EXACTLY the sanity margin.

    Driven through production rather than recomputed here. An earlier version
    of this pin evaluated the comparison inline in the test body with the
    tolerance hardcoded, which made it a tautology about ``math.isclose`` and a
    second source of truth for the rule it claimed to pin — the adversarial
    gate killed it by rebinding the module's ``math`` to an always-False shim
    and watching every arm stay green.
    """
    margin = LINEARIZATION_TRIM_SANITY_MARGIN_DB
    anchored = {"woofer": 0.0, "tweeter": anchor_db}
    resolved = {"woofer": 0.0, "tweeter": anchor_db - margin}
    return iv.decide_trim(
        anchored_db=anchored,
        resolved_db=resolved,
        tweeter_role="tweeter",
        # EQUAL realized level on both pairs, so nothing but the sanity bound
        # can decide this call. With unequal levels the ``anchor_levels_better``
        # arm would commit the anchored pair too, and a test that could not
        # tell those two apart would pass for the wrong reason.
        anchored_match=_MarginMatch(1.0),
        resolved_match=_MarginMatch(1.0),
        ripple_db=0.4,
    )


@pytest.mark.parametrize(
    ("case", "anchor_db"),
    [
        ("re-derives just under the margin", _MARGIN_ANCHOR_UNDER_ULP),
        ("re-derives just over it", _MARGIN_ANCHOR_OVER_ULP),
    ],
    ids=["under_ulp", "over_ulp"],
)
def test_a_drift_that_is_the_margin_is_trusted_whichever_ulp_it_lands_on(
    case, anchor_db,
):
    """The boundary must be a rule, not a coin flip across interpreters.

    ``drift_db`` is a difference of two doubles neither of which is exactly
    representable, so a scan that drifted EXACTLY the margin re-derives a ULP
    either side of it depending on the anchor's last bits — and those come out
    of numpy reductions whose SIMD path varies by build. A bare ``>`` therefore
    answered differently on py3.11 (trusted) and py3.12/3.13 (rejected) for the
    same input, on a test that had been green for months.

    Asserted on the RETURNED DECISION, so the pin binds production: the scan is
    not beyond the margin, it was not rejected, and the record does not carry
    the sanity-drift strategy.
    """
    decision = _trim_at_exactly_the_margin(anchor_db)

    assert decision.beyond_sanity_margin is False, case
    assert decision.outcome == "fitted", case
    assert (
        decision.strategy
        is not iv.TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
    ), case
    assert decision.anchor_drift_db == pytest.approx(
        LINEARIZATION_TRIM_SANITY_MARGIN_DB
    ), case


def test_the_over_ulp_anchor_really_does_reproduce_the_naive_failure(monkeypatch):
    """The fixture's own self-check, and it runs through production too.

    Without it, ``_MARGIN_ANCHOR_OVER_ULP`` could drift to a value landing on
    the same side as its twin, and the parametrization above would pass while
    pinning one case twice. Rather than recomputing the comparison here, this
    removes the TOLERANCE from the shipped code — exactly the mutation the gate
    used to kill the previous version of this pin — and requires the two arms
    to diverge:

    * the over-ULP anchor is rejected (the CI failure, reproduced), and
    * the under-ULP anchor is still trusted, which is what makes this a ULP
      question rather than the fixture being beyond the margin outright.
    """
    monkeypatch.setattr(iv.math, "isclose", lambda *a, **k: False, raising=True)

    assert _trim_at_exactly_the_margin(
        _MARGIN_ANCHOR_OVER_ULP
    ).beyond_sanity_margin is True, (
        "this arm must reproduce the CI failure once the tolerance is gone, "
        "or the test above pins nothing"
    )
    assert _trim_at_exactly_the_margin(
        _MARGIN_ANCHOR_UNDER_ULP
    ).beyond_sanity_margin is False, (
        "and its twin must not, or the two arms are not two ULPs of one number"
    )


def test_the_sanity_bound_reads_its_tolerance_from_one_comparison():
    """One comparison, not two that can disagree.

    A second `>` added anywhere for the same bound would reintroduce the coin
    flip on whichever path skipped the tolerance.
    """
    import inspect

    source = inspect.getsource(iv.decide_trim)
    assert source.count("> float(sanity_margin_db)") == 1
    assert "math.isclose(" in source
