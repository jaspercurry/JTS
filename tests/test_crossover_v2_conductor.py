# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a conductor orchestration: the CHECK→MEASURE→APPLYING(auto)→VERIFY walk.

Fake-seam state walk per docs/crossover-measurement-productization-design.md
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
(jasper.web.correction_crossover_v2.build_v2_run_and_consume). The conductor
itself never performs the apply — see test_correction_crossover_v2_endpoints.py
for the host-level auto-apply trigger + background-thread wiring.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import math
import re
import time
import types
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.attempts_loop import (
    PROVENANCE_REALIZED,
    REASON_ATTEMPT_NOT_COMPARABLE,
    REASON_BASELINE_ESTABLISHED,
    REASON_GRADED_BINS_SHRANK,
    REASON_IMPROVEMENT_ABOVE_FLOOR,
    STOP_EVIDENCE,
    AttemptIntegrity,
    AttemptRecord,
    FloorStats,
    decide_next,
)
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    DELTA_PROBE_VERDICTS,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED,
    VERDICT_MODEL_ERROR,
    VERDICT_UNAVAILABLE,
)
from jasper.active_speaker.crossover_v2_flow import (
    DELTA_PROBE_REASON_BY_VERDICT,
    REASON_CORRECTION_MODEL_ERROR,
    REASON_CORRECTION_ROLLBACK_FAILED,
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
    COURTESY_PRELUDE_ENABLED,
    DEFAULT_CLOUD_MEASURE_POSITIONS,
    DEFAULT_CLOUD_VERIFY_POSITIONS,
    GAIN_CAP_BACKOFF_DB,
    GEOMETRY_RETRY_OFFSET_CM,
    GEOMETRY_RETRY_POSITIONS,
    LEVEL_FRAME_AGREEMENT_TOLERANCE_DB,
    LINEARIZATION_MIN_PAIRED_OCCURRENCES,
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    MAX_CLOUD_MEASURE_POSITIONS,
    MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB,
    MIN_CLOUD_MEASURE_POSITIONS,
    MIN_CLOUD_OFFSET_CM,
    MIN_CLOUD_VERIFY_POSITIONS,
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_VERIFY,
    POSITION_ROLE_ONAX,
    POSITION_ROLES,
    PILOT_LEVEL_DELTA_DB,
    PILOT_SNR_UNUSABLE_DB,
    PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    REASON_CLOUD_GEOMETRY_LOCKED,
    REASON_CORRECTION_NOT_AN_IMPROVEMENT,
    REASON_DRIVER_LEVELS_DISAGREE,
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    REASON_RELAY_TIMEOUT,
    REVERIFY_NO_REWALK_HEADLINE,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    TEMPLATE_HARD_STOP,
    TIER_EXPRESS,
    WIDE_OFFSET_MIN_CM,
    TIER_FULL,
    TRANSIENT_AUTO_RETRY_CODES,
    VERIFY_ANCHOR_HOLD_MESSAGE,
    VERIFY_PILOT_TRANSFER_STEP_CEILING_DB,
    _SIGMA_TOLERABLE_DB,
    CrossoverV2Conductor,
    CrossoverV2FlowError,
    V2FlowSeams,
    V2PlanShape,
    _analysis_json,
    _compose_sigma_db,
    _program_duration_ms,
    _worst_pilot_snr_db,
    abandon_measurement_volume,
    alignment_delay_search_bounds_us,
    alignment_to_candidate_fields,
    _min_positions_for_two_wide_offsets,
    _pose,
    assert_cloud_plan_fits_relay_capacity,
    back_off_gain,
    build_v2_capture_plan,
    build_v2_cloud_index_phase_map,
    build_v2_session_spec,
    build_v2_verify_capture_plan,
    build_v2_verify_index_phase_map,
    build_v2_verify_session_spec,
    cloud_capture_target,
    cloud_plan_max_attempts,
    cloud_geometry_retry_reach_cm,
    cloud_walk_reach_cm,
    cloud_walk_shape,
    express_cloud_measure_positions,
    format_position_distance,
    locate_failed_diagnosis,
    open_measurement_volume,
    resolve_plan_shape,
    spec_report_for_predicted_sum,
    session_wall_clock_ceiling_s,
    tier_display_info,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement import gating
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import KIND_COURTESY_TONE, RoleBand
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_OK,
    INTEGRITY_CHECK_CLIPPED_RUN,
    INTEGRITY_CHECK_REPEAT_EPSILON,
    INTEGRITY_CHECK_SWEEP_HEARD,
    INTEGRITY_CHECK_SWEEP_SCHEDULE,
    INTEGRITY_FAIL,
    INTEGRITY_NOT_EVALUATED,
    AlignmentEstimate,
    CaptureIntegrity,
    CrossoverCandidate,
    DriftEstimate,
    DriverResponse,
    GainPlan,
    IntegrityCheck,
    PilotObservation,
    ProgramAnalysis,
    RoleGainSolve,
    SegmentLocation,
    _verify_capture_integrity,
    predicted_branch_sum,
    solve_branch_trims,
    summed_model_residual_delay_us,
)
from jasper.active_speaker.flat_spec import (
    evaluate_flat_spec,
    spec_convergence_residual,
)
from jasper.capture_relay.session import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
    CaptureResult,
)

from tests.test_active_speaker_profile import _two_way_preset

SESSION = "cap_test_session_1"
FC_HZ = 1600.0
SESSION_VOLUME_DB = -20.0
CAPS = {"woofer": 0.0, "tweeter": -65.0}


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _preset() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


# --- fake analyses -------------------------------------------------------------


def _loc(segment_id: str, kind: str = "sweep", *, confidence: float = 0.9,
         clipped: bool = False, residual_samples: float = 0.0) -> SegmentLocation:
    return SegmentLocation(
        segment_id=segment_id, kind=kind, role=None,
        scheduled_start=0, located_start=0, residual_samples=residual_samples,
        confidence=confidence, peak_dbfs=-12.0, clipped=clipped,
    )


_SUMMED_FREQS_HZ = np.linspace(100.0, 20000.0, 64)


def _in_room_summed_db() -> np.ndarray:
    """A modest, physically plausible in-room summed magnitude.

    **Why this is not ``np.zeros``** (PR-L4). The cloud positions play this
    curve, so it becomes the group's spatially-combined "how flat is the
    speaker" measurement — and a perfectly flat 8-position in-room spatial
    power mean does not exist. A zero curve made the measured pre-apply spec
    residual exactly 0.00 dB, which is not a demanding fixture but an
    impossible one, and PR-L4 item 2's gate (predicted post-apply vs measured
    pre-apply) reads exactly that number. Any assertion about "did the
    correction improve on what we measured" is meaningless against a
    measurement that claims perfection.

    A broadband tilt plus one wide dip, landing at ~2.7 dB of pooled spec
    residual — the regime the 2026-07-27 JTS3 session actually measured
    (3.15-3.76 dB pooled across its ten positions). Note what the PRE-APPLY
    cloud is measuring: an UNCORRECTED speaker in a room, which is supposed to
    look like this. Deliberately smooth — the honesty screen and the null gate
    are exercised by their own fixtures, and a combed curve here would couple
    these wiring tests to those detectors.
    """
    octaves = np.log2(_SUMMED_FREQS_HZ / 1000.0)
    return -2.0 * octaves - 3.0 * np.exp(-0.5 * (octaves / 0.8) ** 2)


# The measured pre-apply pooled spec residual each room scale in
# ``test_prediction_gate_verdict_does_not_depend_on_the_room`` produces. Quoted
# so that test can prove the room ACTUALLY moved between its cases — a
# room-independence claim is worthless if the fixture room never varied. Under
# the pre-B1 gate these three scales spanned refuse / pass / pass; they must now
# all reach the same verdict.
_ROOM_SCALE_EXPECTED_RMS_DB = {0.4: 1.011, 1.0: 2.691, 2.5: 8.566}


def _driver_response(
    role: str, window_ms: float, *, summed_db: np.ndarray | None = None,
    floor_source: str | None = None,
) -> DriverResponse:
    if summed_db is not None:
        magnitude_db = np.asarray(summed_db, dtype=float)
    else:
        magnitude_db = _in_room_summed_db() if role == "summed" else np.zeros(64)
    return DriverResponse(
        role=role, freqs_hz=_SUMMED_FREQS_HZ, magnitude_db=magnitude_db,
        complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
        # ``floor_source`` is WHY the window is that long (issue #1966) —
        # optional here because most fixtures only care that a window exists,
        # and a block without it reads as "unknown" exactly like a schema-1
        # record does.
        gating={
            "applied": True, "window_ms": window_ms,
            **({"floor_source": floor_source} if floor_source else {}),
        },
        snr=None, validity_floor_hz=None,
    )


_LINEARIZABLE_FREQS_HZ = np.linspace(100.0, 20000.0, 2048)
_FIXTURE_FC_HZ = 1600.0


def _linearizable_response(
    role: str, magnitude_db: np.ndarray, *,
    n_repeats: int = 2, validity_floor_hz: float = 140.0,
) -> DriverResponse:
    """A finer-grained DriverResponse (2048 bins, vs _driver_response's
    coarse 64) carrying real repeat_responses — Layer-1a linearization
    (#1668 PR-C) needs enough frequency resolution for a synthetic bump to
    survive resampling onto DEFAULT_ENVELOPE_GRID_HZ and enough occurrences
    to clear the paired-N gate."""

    def make() -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=_LINEARIZABLE_FREQS_HZ, magnitude_db=magnitude_db,
            complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
            gating={"applied": True, "window_ms": 8.0},
            snr=None, validity_floor_hz=validity_floor_hz,
        )

    repeats = tuple(make() for _ in range(n_repeats))
    return DriverResponse(
        role=role, freqs_hz=_LINEARIZABLE_FREQS_HZ, magnitude_db=magnitude_db,
        complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
        gating={"applied": True, "window_ms": 8.0},
        snr=None, validity_floor_hz=validity_floor_hz,
        repeat_responses=repeats,
    )


def _check_analysis(
    program, *, linearity=True, channel_map=True, snr_floor_ok=True,
    locate_confidence=0.9, pilot_snr_ok=None,
) -> ProgramAnalysis:
    return ProgramAnalysis(
        phase="check",
        program_id=program.program_id,
        locations=(
            _loc("pilot_woofer_hi", "pilot", confidence=locate_confidence),
        ),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        linearity_ok=linearity,
        channel_map_ok=channel_map,
        pilot_snr_ok=pilot_snr_ok,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0,
            snr_floor_ok=snr_floor_ok,
        ),
    )


def _alignment(
    *, delay_us=150.0, status=ALIGNMENT_OK, polarity="normal", confidence=0.8,
    anchor_delay_us=None,
) -> AlignmentEstimate:
    """The fixture alignment. ``anchor_delay_us`` defaults to ``None`` — the
    shape every pre-R10b test was written against, in which the summed model's
    residual delay is 0.0 and the prediction is byte-identical to the
    independently-aligned sum. Pass it to exercise the committed-delay model.
    """
    return AlignmentEstimate(
        delay_us=delay_us, raw_delay_us=delay_us, parallax_us=11.0,
        polarity=polarity, polarity_sign=1 if polarity == "normal" else -1,
        polarity_agrees_with_sum=True, confidence=confidence, status=status,
        anchor_delay_us=anchor_delay_us,
    )


def _measure_analysis(
    program, *, glitch=False, clipped=False, linearity=True,
    alignment=None, locate_confidence=0.9, gate_ms=8.0,
    predicted_ripple_db=0.8, sweep_locations=None, pilot_snr_ok=None,
) -> ProgramAnalysis:
    freqs = np.linspace(100.0, 20000.0, 64)
    locations = (
        sweep_locations if sweep_locations is not None else (
            _loc("sweep_w", confidence=locate_confidence, clipped=clipped),
            _loc("sweep_t", confidence=locate_confidence),
            _loc("sweep_w_rep", confidence=locate_confidence),
        )
    )
    return ProgramAnalysis(
        phase="measure",
        program_id=program.program_id,
        locations=locations,
        drift=DriftEstimate(
            epsilon_ppm=30.0, baselines_ppm={"woofer_repeat": 30.0},
            max_residual_samples=0.2, glitch_detected=glitch,
        ),
        driver_responses=(
            _driver_response("woofer", gate_ms),
            _driver_response("tweeter", gate_ms + 1.0),
        ),
        alignment=alignment if alignment is not None else _alignment(),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.1, "tweeter": 0.0},
            polarity="normal", delay_us=150.0,
            predicted_ripple_db=predicted_ripple_db, confidence=0.8,
        ),
        linearity_ok=linearity,
        pilot_snr_ok=pilot_snr_ok,
        predicted_sum=(freqs, np.zeros(64)),
        glitch_detected=glitch,
    )


def _verify_pilot(hi_dbfs: float, *, programmed_hi_gain_db: float = -20.0) -> PilotObservation:
    """A VERIFY leading-pilot observation — role ``summed`` (VERIFY_PILOT_ROLE),
    the only role a v2 VERIFY program ever carries."""
    return PilotObservation(
        role="summed", level_lo_dbfs=hi_dbfs - 10.0, level_hi_dbfs=hi_dbfs,
        programmed_delta_db=10.0, captured_delta_db=10.0,
        linearity_ok=True, channel_map_ok=True,
        programmed_hi_gain_db=programmed_hi_gain_db,
    )


_INTEGRITY_FROM_LOCATIONS = object()


def _verify_analysis(
    program, *, max_db=0.9, gate_ms=8.5, linearity=True, locate_confidence=0.9,
    pilot_hi_dbfs=None, programmed_hi_gain_db=-20.0, summed_db=None,
    pilot_snr_ok=None, floor_source=None, residual_samples=0.0,
    n_graded_bins=120,
    integrity=_INTEGRITY_FROM_LOCATIONS,
) -> ProgramAnalysis:
    locations = (
        _loc(
            "sweep_verify", "summed_sweep",
            confidence=locate_confidence, residual_samples=residual_samples,
        ),
    )
    if integrity is _INTEGRITY_FROM_LOCATIONS:
        # Derived by PRODUCTION code from this fixture's own locations (#1971),
        # not hand-built: a hand-built record would let the conductor's gate be
        # tested against a shape the analyzer never produces. Pass
        # ``integrity=None`` for the pre-#1971 analysis shape.
        integrity = _verify_capture_integrity(
            program, program.sample_rate_hz, locations,
        )
    return ProgramAnalysis(
        phase="verify",
        program_id=program.program_id,
        locations=locations,
        capture_integrity=integrity,
        glitch_detected=bool(integrity is not None and integrity.glitched),
        summed_response=_driver_response(
            "summed", gate_ms, summed_db=summed_db, floor_source=floor_source,
        ),
        summed_ripple_db=1.1,
        # W6.7 ruling 1: the conductor gates on the notch-excluded max, not the
        # raw ``max_db`` — this fake keeps them equal (a fake with no notch to
        # exclude), so the ``max_db`` parameter still controls the gate.
        verify_tracking={
            "rms_db": 0.4,
            "max_db": max_db,
            "max_db_notch_excluded": max_db,
            "frame": {"n_bins": n_graded_bins},
        },
        linearity_ok=linearity,
        pilot_snr_ok=pilot_snr_ok,
        pilots=(
            (_verify_pilot(pilot_hi_dbfs, programmed_hi_gain_db=programmed_hi_gain_db),)
            if pilot_hi_dbfs is not None else ()
        ),
    )


# --- fake seams -----------------------------------------------------------------


@dataclass
class FakeSeams:
    """Recorder seams; per-phase analysis factories are swappable mid-test."""

    check: Any = _check_analysis
    measure: Any = _measure_analysis
    verify: Any = _verify_analysis
    played: list = field(default_factory=list)
    analyzed: list = field(default_factory=list)
    published_checks: list = field(default_factory=list)
    published_candidates: list = field(default_factory=list)
    apply_done: bool = False
    # Simulates the host's auto-apply background thread hitting a TERMINAL
    # failure (owner ruling, 2026-07-20) — empty string while pending/never
    # attempted, a REASON_REGISTRY code once the auto-apply gives up.
    apply_failed_code: str = ""
    # PR-L5: the delta probe's automatic-rollback seam. ``None`` (the default)
    # is the honest "no binding" case the conductor must still refuse under.
    rollback: Any = None
    # #1866: every level-frame finding the conductor banks, in order. Bound by
    # default (unlike ``rollback``) because "no findings seam" is the degraded
    # case here, not the normal one — a test that wants it unbound passes
    # ``publish_findings=None`` through ``dataclasses.replace``.
    banked_findings: list = field(default_factory=list)

    def seams(self) -> V2FlowSeams:
        def analyze(program, result, priors, geometry, *, phase=None):
            # ``phase`` is the conductor's OWN flow phase (issue #1855) —
            # recorded separately from ``program.phase`` since the two
            # diverge for cloud positions (every cloud position plays the
            # verify-shaped summed sweep, so ``program.phase`` is always
            # "verify" there; see test_cloud_positions_play_the_summed_
            # program_and_get_no_tracking_prior).
            self.analyzed.append((phase, program.phase, result, priors, geometry))
            factory = {
                "check": self.check, "measure": self.measure, "verify": self.verify,
            }[program.phase]
            return factory(program)

        return V2FlowSeams(
            play=lambda phase, program: self.played.append((phase, program)),
            analyze=analyze,
            publish_check=lambda plan, ambient: self.published_checks.append(plan),
            publish_candidate=self.published_candidates.append,
            apply_complete=lambda: self.apply_done,
            apply_failed=lambda: self.apply_failed_code,
            rollback=self.rollback,
            publish_findings=self.banked_findings.append,
        )


def _conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Conductor:
    seams = kwargs.pop("seams", fakes.seams())
    return CrossoverV2Conductor(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=seams,
        driver_spacing_m=0.15,
        **kwargs,
    )


def _attempt_floor() -> FloorStats:
    return FloorStats.from_repeat_study(
        metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        median_db=0.05,
        p95_db=0.1,
        source="test repeat study",
        measured_at="2026-08-03",
    )


def _verify_only_conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Conductor:
    return _conductor(
        fakes,
        index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        attempt_floor=_attempt_floor(),
        **kwargs,
    )


def _capture() -> CaptureResult:
    return CaptureResult(wav=b"fake-wav")


def _run_phase(conductor, index, attempt) -> dict:
    # Mirrors the production host's own authorize wrapper
    # (``correction_crossover_v2.build_v2_run_and_consume``): admission, and
    # ONLY admission. It used to call ``confirm_cloud_measure_group(index)``
    # first, because the household's confirmation was inferred from a begin
    # past the cloud group; since the two-stage split (work order D1) the
    # confirmation is its own explicit signal and rides no begin at all.
    conductor.authorize_begin(index, attempt)
    conductor.on_armed()
    return conductor.consume_capture(index, attempt, _capture())


def _confirm_cloud(conductor) -> dict:
    """The confirm seam's own payload — ``{candidate_fingerprint,
    headroom_cost_db}``.

    The explicit close the host calls on the phone's set-completion signal. One
    shot by construction (``self._candidate`` is the guard), so a second call
    returns ``None`` rather than re-fitting.
    """
    return conductor.confirm_cloud_measure_group() or {}


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
            integrity=AttemptIntegrity(comparable=True),
            grade_db=1.4,
            n_graded_bins=120,
        ),
        AttemptRecord(
            attempt_id="candidate-previous",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
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


def test_live_seam_refuses_improvement_when_verify_denominator_shrinks():
    history = (
        AttemptRecord(
            attempt_id="candidate-previous",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
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
            integrity=AttemptIntegrity(comparable=True),
            grade_db=9.0,
        ),
        AttemptRecord(
            attempt_id="candidate-previous",
            metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
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


def test_low_alignment_confidence_rejects_measure_before_building_candidate():
    """Owner ruling (2026-07-20): the former review-screen nudge
    (< ALIGNMENT_CONFIDENCE_TRUST_FLOOR) is now a hard MEASURE-phase gate —
    no candidate is built or published, and the household gets guidance to
    re-measure, never an "apply anyway?" question."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program,
        alignment=_alignment(confidence=ALIGNMENT_CONFIDENCE_TRUST_FLOOR - 0.1),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict == {
        "accepted": False,
        "code": "low_alignment_confidence",
        "template": "fix_and_retry",
        "reason": REASON_REGISTRY["low_alignment_confidence"].message,
        "banner": "",
        "auto_retry": False,
        # Every rejection carries the capture's pilot evidence since #2085 —
        # here `None`, because this scenario's analysis states no pilot
        # verdict. Kept in the exact-equality assertion rather than relaxed to
        # a subset: the relay dict is the phone's contract, and a test that
        # stops noticing new keys stops defending it.
        "pilot_heard": None,
        # The honest per-position count rides EVERY verdict (#2086 item 2).
        "attempts": {
            "used": 0, "allowed": 3, "left": 3,
            "by_speaker": 0, "by_household": 0,
        },
    }
    assert not fakes.published_candidates
    assert c.candidate is None
    assert c.current_phase == PHASE_MEASURE


def test_alignment_confidence_at_the_trust_floor_is_trusted():
    """The floor is an exclusive lower bound (`<`, not `<=`) — exactly-at-floor
    is trusted, matching the former nudge's own comparator."""
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
    [50, 300] us), reusing the low_alignment_confidence guidance rather than
    auto-applying a physically implausible correction. A delay inside that
    declared bound is unaffected."""
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
    assert verdict["code"] == "low_alignment_confidence"
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


def test_measure_program_gains_back_off_from_caps():
    """W2 gate: the solver backs off ≥0.01 dB from exact per-driver caps."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    program = c._program_for_phase(PHASE_MEASURE)
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
    assert geometry.mic_distance_m == pytest.approx(1.0)
    assert geometry.parallax_us() > 0.0


# --- §5.10 failure templates ------------------------------------------------------


def test_clipped_measure_is_transient_auto_retry_with_quieter_program():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    gain_before = c._program_for_phase(PHASE_MEASURE).segment("sweep_w").gain_db

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
    # The automatic retry is gain-adjusted: 3 dB quieter.
    gain_after = c._program_for_phase(PHASE_MEASURE).segment("sweep_w").gain_db
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


def _snr_pilot(role: str, snr_db: float) -> PilotObservation:
    return PilotObservation(
        role=role, level_lo_dbfs=-40.0, level_hi_dbfs=-30.0,
        programmed_delta_db=10.0, captured_delta_db=10.0,
        linearity_ok=True, channel_map_ok=True,
        snr_valid=math.isfinite(snr_db) or snr_db > 0, snr_db=snr_db,
    )


def _snr_analysis(*pilots: PilotObservation) -> ProgramAnalysis:
    return ProgramAnalysis(
        phase="measure", program_id="p", locations=(), pilots=pilots,
    )


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


def test_verify_evidence_carried_on_tolerance_verdict_reset_on_early_return():
    """Item 5b (#1605): the conductor carries the verify_fail expert-disclosure
    numbers on a verdict that reaches the tracking comparison, and resets them
    to None on an early-return verdict (gate comparability) so no stale numbers
    leak into a later attempt's disclosure."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)
    _run_phase(c, 3, 3)
    assert c.verify_outcome == "fail"
    evidence = c.verify_evidence
    assert evidence is not None
    assert evidence["max_db"] == 2.4
    assert evidence["rms_db"] == 0.4
    assert evidence["tolerance_db"] == 1.5

    # An early-return verdict (gate-comparability inconclusive) never reaches
    # the tracking numbers ⇒ evidence resets to None, no stale leak.
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0)
    _run_phase(c, 3, 4)
    assert c.verify_outcome == "inconclusive"
    assert c.verify_evidence is None


# --- #1971: the VERIFY capture-integrity gate ------------------------------------
#
# Before this, nothing on the VERIFY path checked whether the capture the
# tracking verdict grades was intact: ``glitch_detected`` came from
# ``_estimate_drift`` (MEASURE-only), and the two flow gates that DO catch a
# splice filter ``KIND_SWEEP`` while VERIFY's sweep is ``KIND_SUMMED_SWEEP``.


def _spliced_verify(program, **kwargs):
    """A VERIFY analysis whose summed sweep landed a splice off its slot."""
    off_slot = SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * 1e-3 * program.sample_rate_hz * 3
    return _verify_analysis(program, residual_samples=off_slot, **kwargs)


def _verify_to_apply(fakes):
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    return c


def test_verify_splice_refuses_before_the_tracking_grade():
    """A glitched capture must not produce a pass/fail TRACKING verdict. The
    tracking max here is wildly out of tolerance, and the answer is still the
    capture-glitch code — because a spliced recording is not evidence about
    the speaker either way."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _spliced_verify(program, max_db=9.9)
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "drift_baselines_disagree"
    # §5.2's capture-glitch convention: a transient, silently auto-retried
    # code, not a new user-facing one and not a verify_fail decision screen.
    assert verdict["auto_retry"] is True
    assert verdict["code"] in TRANSIENT_AUTO_RETRY_CODES
    # The evidence rides the verdict, so the host event carries WHY.
    record = verdict["capture_integrity"]
    assert record["glitched"] is True
    assert [c["name"] for c in record["checks"] if c["status"] == "fail"] == [
        INTEGRITY_CHECK_SWEEP_SCHEDULE
    ]
    # And the not-evaluated register travels with it — a reader of this
    # payload can tell which checks were never asked.
    assert INTEGRITY_CHECK_REPEAT_EPSILON in [
        c["name"] for c in record["checks"] if c["status"] == "not_evaluated"
    ]
    # No tracking verdict was drawn from it.
    assert c.verify_evidence is None


def test_verify_unheard_sweep_routes_to_locate_failed_not_a_glitch():
    """#1838's D3 rule on the VERIFY path: "too quiet" and "spliced" produce
    the same symptom and must not share a verdict. This confidence clears
    ``_stimulus_locate_ok``'s 0.1 floor, so before #1971 it reached the
    tracking grade untouched."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR - 0.05,
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == REASON_LOCATE_FAILED
    # NOT the silent-auto-retry glitch code: re-running the same measurement
    # at the same level cannot fix a capture nobody could hear.
    assert verdict["code"] not in TRANSIENT_AUTO_RETRY_CODES
    record = verdict["capture_integrity"]
    failed = [c["name"] for c in record["checks"] if c["status"] == "fail"]
    assert failed == [INTEGRITY_CHECK_SWEEP_HEARD]
    # The residual the mislocated sweep shows is NOT reported as a splice.
    not_evaluated = [
        c["name"] for c in record["checks"] if c["status"] == "not_evaluated"
    ]
    assert INTEGRITY_CHECK_SWEEP_SCHEDULE in not_evaluated


def test_verify_clip_refuses_as_a_capture_glitch():
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    clipped = _loc("sweep_verify", "summed_sweep", clipped=True)
    fakes.verify = lambda program: _verify_analysis(
        program,
        integrity=_verify_capture_integrity(
            program, program.sample_rate_hz, (clipped,),
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "drift_baselines_disagree"
    record = verdict["capture_integrity"]
    assert [c["name"] for c in record["checks"] if c["status"] == "fail"] == [
        INTEGRITY_CHECK_CLIPPED_RUN
    ]
    assert record["clipped_segments"] == ["sweep_verify"]


def test_verify_clean_integrity_still_passes_end_to_end():
    """The era-exact clean path: the SAME fixture every other verify test
    uses, now carrying a production-derived integrity record, still accepts."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"


def test_verify_without_an_integrity_record_is_not_refused_but_says_so(caplog):
    """``None`` is "no evidence" — the same convention ``linearity_ok`` and
    ``pilot_snr_ok`` use two lines up, where only an explicit failure refuses.
    A pre-#1971 analysis shape must not become an un-passable capture; the
    live analyze seam always populates the record (pinned in
    tests/test_crossover_v2_program_pilots.py). It is not a SILENT pass
    either: the journal says ``unavailable``, its own value, so a missing
    record can never be read as a clean one."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(program, integrity=None)
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert "integrity=unavailable" in caplog.text
    assert "integrity_locate_confidence_min=null" in caplog.text


def test_verify_integrity_gate_runs_ahead_of_the_linearity_branch():
    """A spliced capture's pilot-derived linearity verdict is drawn from a
    corrupted timeline, so it must not be the reported cause — the same order
    ``_measure_verdict`` puts its glitch branch in."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _spliced_verify(program, linearity=False)
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "drift_baselines_disagree"


def test_verify_diag_discloses_integrity_on_pass_and_on_refusal(caplog):
    """Disclosed on EVERY verify: on a pass it is what makes "this capture was
    comparable" measured rather than assumed, and on a refusal it names which
    check fired — which is how telemetry tells this gate's ``locate_failed``
    from ``_stimulus_locate_ok``'s."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    _run_phase(c, 3, 3)
    assert "integrity=ok" in caplog.text
    assert f"integrity_not_evaluated={INTEGRITY_CHECK_REPEAT_EPSILON}" in caplog.text
    assert "integrity_locate_confidence_min=0.9" in caplog.text
    assert "integrity_residual_ms_worst=0.0" in caplog.text

    caplog.clear()
    fakes.verify = _spliced_verify
    _run_phase(c, 3, 4)
    assert f"integrity={INTEGRITY_CHECK_SWEEP_SCHEDULE}" in caplog.text
    assert "integrity_residual_ms_worst=15.0" in caplog.text


# --- #1974: the outcome and the verdict that produced it -------------------------


def test_the_verify_outcome_always_carries_the_code_that_produced_it():
    """Issue #1974: "inconclusive" is reached by two verdicts with no shared
    mechanism, so the outcome alone cannot tell a household WHY the check could
    not settle. The pair is written in one call (``_set_verify_outcome``), and
    this pins the property that makes the done screen's copy safe: whatever the
    conductor reports as ``verify_code`` is the code of the verdict it just
    returned — never a previous attempt's, never absent.

    Also the no-behaviour-change pin for the copy work: the accepted/code
    values asserted here are exactly the ones the surrounding suite already
    asserted before the copy changed. Only what a screen SAYS moved.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    # (analysis factory, expected outcome, expected code)
    cases = [
        (lambda program: _verify_analysis(program, max_db=2.4),
         "fail", "verify_out_of_tolerance"),
        (lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0),
         "inconclusive", "verify_inconclusive"),
        (_verify_analysis, "pass", None),
    ]
    for index, (factory, outcome, code) in enumerate(cases, start=3):
        fakes.verify = factory
        verdict = _run_phase(c, 3, index)
        assert c.verify_outcome == outcome, code
        assert c.verify_code == code, code
        # The pair agrees with the verdict itself, which is the whole point:
        # a screen reading the persisted code is reading THIS verdict.
        assert c.verify_code == (verdict.get("code") or None)
        assert verdict["accepted"] is (outcome == "pass")


def test_a_level_shift_records_its_own_code_not_the_gates():
    """The second road to "inconclusive". Before #1974 both roads produced the
    same household sentence, which blamed a room reflection — on a verdict
    where no reflection and no window are involved at all."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    _run_phase(c, 3, 3)
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.56, max_db=0.5,
    )
    verdict = _run_phase(c, 3, 4)
    assert verdict["code"] == "verify_level_shift"
    assert c.verify_outcome == "inconclusive"
    assert c.verify_code == "verify_level_shift"


def test_the_verify_gate_record_is_gate_disclosures_own_sentence():
    """Issue #1966's disclosure, at the seam where it enters the wizard.

    The conductor composes it ONCE, by calling ``describe_gate`` — it does not
    assemble a sentence of its own from the same fields, which is the failure
    mode the whole contract exists to prevent. So the assertion is equality
    against that function's output, not a substring match that a lookalike
    would also satisfy.
    """
    from jasper.audio_measurement import gate_disclosure

    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.5, gate_ms=5.0, floor_source=gating.FLOOR_SEARCH_BOUND,
    )
    _run_phase(c, 3, 3)
    record = c.verify_gate
    assert record is not None
    assert record["disclosure"] == gate_disclosure.describe_gate(
        {"applied": True, "window_ms": 5.0,
         "floor_source": gating.FLOOR_SEARCH_BOUND}
    )
    # The one fact the household copy branches on, and the state the whole
    # 2026-07-30 corpus was actually in.
    assert record["reflection_measured"] is False
    assert "no reflection found" in record["disclosure"]


def test_a_measured_reflection_is_recorded_as_one():
    """The other epistemic state — the only one where "reflections were
    removed" is a true thing to say (``GateDisclosure.gated_anything``)."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.5, gate_ms=5.0, floor_source=gating.FLOOR_MEASURED,
    )
    _run_phase(c, 3, 3)
    assert c.verify_gate is not None
    assert c.verify_gate["reflection_measured"] is True
    assert "reflection measured" in c.verify_gate["disclosure"]


def test_the_gate_is_recorded_on_a_passing_verify_too():
    """On EVERY outcome, like the graded band and the frame beside it: a pass
    is exactly when nobody would otherwise ask how much of the response the
    comparison could see."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(
        program, floor_source=gating.FLOOR_SEARCH_BOUND,
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_gate is not None
    assert c.verify_gate["reflection_measured"] is False


def test_an_early_return_retry_cannot_repair_the_gate_onto_a_stale_verdict():
    """The desync the PR #1994 adversarial gate found, pinned as a property.

    The outcome, the code, and the gate are written by ONE call, so an attempt
    that early-returns (``locate_failed`` / ``pilot_level_collapse`` /
    ``agc_behavioral_fail`` — none of which reach ``_set_verify_outcome``)
    leaves all three of the previous attempt's facts standing TOGETHER.

    Before the fix the gate alone was recomputed at the top of every
    ``_verify_verdict`` call, so this exact sequence — an inconclusive whose
    window was capped at the search ceiling, then a locate failure whose
    capture DID find a reflection — paired attempt 1's verdict with attempt 2's
    gate. The done screen then said "a reflection reached the microphone
    sooner…" about a verdict whose own capture had found none: issue #1974
    re-created one layer down. The symmetric understatement (measured, then a
    ceiling-capped early return) is the same bug in the other direction.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    # Attempt 1 concludes: gate-comparability inconclusive, window capped.
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.5, gate_ms=5.0, floor_source=gating.FLOOR_SEARCH_BOUND,
    )
    assert _run_phase(c, 3, 3)["code"] == "verify_inconclusive"
    assert c.verify_gate is not None
    assert c.verify_gate["reflection_measured"] is False

    # Attempt 2 never concludes — but its capture found a reflection.
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, floor_source=gating.FLOOR_MEASURED,
    )
    assert _run_phase(c, 3, 4)["code"] == REASON_LOCATE_FAILED

    # The triple is still attempt 1's, entire.
    assert c.verify_outcome == "inconclusive"
    assert c.verify_code == "verify_inconclusive"
    assert c.verify_gate is not None
    assert c.verify_gate["reflection_measured"] is False
    assert "no reflection found" in c.verify_gate["disclosure"]

    # And the screen the household actually reads says the ceiling thing.
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "done",
            "applied": True,
            "verify": {
                "outcome": c.verify_outcome,
                "code": c.verify_code,
                "gate": c.verify_gate,
            },
            "candidate": {"trims_db": {"lo": -1.0}, "delay_us": 120.0,
                          "polarity": "normal"},
            "post_apply_grade": {"state": "inconclusive", "graded": False},
        },
    })
    assert env["screen"] == "done"
    assert "less usable sound to compare" in env["verdict_text"]
    assert "reflection" not in env["verdict_text"]


def test_an_ungated_capture_records_no_gate_at_all():
    """Absent stays absent (the #1987 rule): a response carrying no gating
    block yields no record, so no screen can print a gate that never ran."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    def ungated(program):
        analysis = _verify_analysis(program)
        return dataclasses.replace(
            analysis,
            summed_response=dataclasses.replace(analysis.summed_response, gating={}),
        )

    fakes.verify = ungated
    _run_phase(c, 3, 3)
    assert c.verify_gate is None


# --- PR-5: the retired per-capture flatness relay --------------------------------
#
# The flat-linearization plan's PR-5 removed ``ProgramAnalysis.flatness_tracking``
# and the conductor's ``flatness_evidence`` stash: one VERIFY capture graded on
# its own grid against its own band mean was a SECOND construction of "is the
# speaker flat", disagreeing with the spatial cloud's spec evaluation by however
# much a single mic position differs from the cloud. The claim now has exactly
# one owner (``assemble_cloud_group_result``'s ``flatness`` key). What stays
# pinned here is the boundary that made the removal safe: the VERIFY verdict
# never consulted flatness, so removing it changed no accept/code.


def test_verify_payload_carries_tracking_only_no_flatness_claim():
    """PASS and FAIL branches both relay integration-verify's tracking and
    nothing else — no flatness key, on either branch, in either direction.

    The accepted/code assertions are byte-for-byte the ones the retired
    relay test made (``test_verify_flatness_tracking_relays_without_changing_
    accepted_or_code``): they were the point of that test, and they are
    unchanged by the removal, which is the property worth keeping."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = _verify_analysis
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert "flatness_tracking" not in verdict
    assert verdict["tracking"]["max_db_notch_excluded"] is not None

    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)
    verdict = _run_phase(c, 3, 4)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_out_of_tolerance"
    assert verdict["template"] == "verify_fail"
    assert "flatness_tracking" not in verdict


def test_conductor_exposes_no_per_capture_flatness_evidence():
    """The stash and its property are gone, not repointed — a household-facing
    flatness number must come from the cloud group's spec verdict
    (``group_cloud_result``), never from a per-VERIFY-attempt stash that a
    verify-only re-arm would silently re-derive from one position."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = _verify_analysis
    assert _run_phase(c, 3, 3)["accepted"] is True
    assert not hasattr(c, "flatness_evidence")


# --- measurement-honesty gate G3: verify inter-attempt pilot consistency --------


def test_verify_pilot_baseline_never_fires_on_first_usable_attempt():
    """Measurement-honesty gate G3 (2026-07-22): the FIRST usable VERIFY
    attempt establishes the reference and never rejects on its own — a
    normal, otherwise-clean VERIFY with its first-ever pilot pair passes."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(program, pilot_hi_dbfs=-20.0)
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"


def test_verify_pilot_level_shift_fires_on_large_step():
    """Mirrors the 2026-07-22 hardware evidence: a phone's input chain
    stepped ~0.56 dB between VERIFY attempts, producing escalating
    dishonest verify verdicts. Attempt 1 (independently out of tolerance,
    unrelated to G3) establishes the reference; attempt 2's otherwise-clean
    capture (max_db well within tolerance) still rejects because its own
    pilot transfer stepped 0.56 dB away from that reference."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    verdict1 = _run_phase(c, 3, 3)
    assert verdict1["code"] == "verify_out_of_tolerance"  # unrelated to G3

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.56, max_db=0.5,
    )
    verdict2 = _run_phase(c, 3, 4)
    assert verdict2["accepted"] is False
    assert verdict2["code"] == "verify_level_shift"
    assert c.verify_outcome == "inconclusive"


def test_verify_pilot_level_shift_within_tolerance_passes():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    _run_phase(c, 3, 3)

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.1, max_db=0.5,
    )
    verdict = _run_phase(c, 3, 4)
    assert verdict["accepted"] is True


def test_verify_pilot_level_shift_boundary_exact_passes_just_above_fires():
    """The ceiling is an exclusive upper bound (``>``, not ``>=``) — exactly
    at the ceiling passes, matching this file's other boundary comparators.
    ``programmed_hi_gain_db=0.0`` (not the -20.0 the other G3 tests use) so
    the transfer IS the pilot level with no subtraction involved — a
    baseline of -20.0 would compute ``(0.0 - (-20.0)) - (0.35 - (-20.0))``,
    which picks up a ~1e-15 float rounding artifact that would make an
    "exactly at the boundary" test flaky."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=0.0, programmed_hi_gain_db=0.0, max_db=5.0,
    )
    _run_phase(c, 3, 3)
    fakes.verify = lambda program: _verify_analysis(
        program,
        pilot_hi_dbfs=VERIFY_PILOT_TRANSFER_STEP_CEILING_DB,
        programmed_hi_gain_db=0.0,
        max_db=0.5,
    )
    verdict = _run_phase(c, 3, 4)
    assert verdict["accepted"] is True

    fakes2 = FakeSeams()
    c2 = _conductor(fakes2)
    _run_phase(c2, 1, 1)
    _run_phase(c2, 2, 2)
    c2.note_apply_complete()
    fakes2.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=0.0, programmed_hi_gain_db=0.0, max_db=5.0,
    )
    _run_phase(c2, 3, 3)
    fakes2.verify = lambda program: _verify_analysis(
        program,
        pilot_hi_dbfs=VERIFY_PILOT_TRANSFER_STEP_CEILING_DB + 0.01,
        programmed_hi_gain_db=0.0,
        max_db=0.5,
    )
    verdict2 = _run_phase(c2, 3, 4)
    assert verdict2["accepted"] is False
    assert verdict2["code"] == "verify_level_shift"


def test_verify_pilot_level_shift_skips_when_pilots_absent():
    """A legacy VERIFY program with no leading pilot pair (the default
    ``_verify_analysis`` fixture, ``pilot_hi_dbfs=None`` ⇒ ``pilots=()``)
    never gates on G3 — mirrors the other two gates' own skip conditions."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True


def test_verify_pilot_level_shift_baseline_does_not_rebaseline():
    """The baseline is frozen at the FIRST usable attempt — a later attempt
    that itself clears the ceiling vs the baseline must NOT quietly become
    the new reference. Numbers are chosen so the two readings diverge: a
    3rd attempt 0.6 dB from the ORIGINAL baseline (fires) is only 0.3 dB from
    the 2nd attempt (would NOT fire if the 2nd attempt had silently become
    the new baseline)."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    # Attempt 1: baseline = -20.0 dBFS transfer (independently out of
    # tolerance, so a retry is admitted).
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    verdict1 = _run_phase(c, 3, 3)
    assert verdict1["code"] == "verify_out_of_tolerance"

    # Attempt 2: +0.3 dB from the baseline — clears the ceiling on its OWN
    # G3 check (0.3 ≤ 0.35), so it fails for the SAME independent reason,
    # never level_shift.
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-19.7, max_db=5.0,
    )
    verdict2 = _run_phase(c, 3, 4)
    assert verdict2["code"] == "verify_out_of_tolerance"

    # Attempt 3: +0.6 dB from the ORIGINAL -20.0 baseline (fires) but only
    # +0.3 dB from attempt 2's -19.7 (would NOT fire against that). Also
    # independently out of tolerance, so a buggy re-baseline would show
    # verify_out_of_tolerance here instead — the frozen baseline is what
    # makes this show verify_level_shift.
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-19.4, max_db=5.0,
    )
    verdict3 = _run_phase(c, 3, 5)
    assert verdict3["accepted"] is False
    assert verdict3["code"] == "verify_level_shift"


def _rearm_conductor(fakes, **kwargs):
    """A verify-only re-arm's conductor — ``prepare_v2_verify``'s shape."""
    return CrossoverV2Conductor(
        session_id="verify_rearm_session",
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
        measure_gate_window_ms=8.0,
        **kwargs,
    )


def test_verify_pilot_reference_is_session_scoped_not_inherited():
    """#1927: a prior session's reference is HISTORY, never the comparator.

    This is the 2026-07-30 bench shape (#1870 finding 1): the day-later verify
    reads 0.775 dB away from yesterday's reference — over 2× the ceiling, and
    deterministic. Before the ruling that rehydrated baseline refused the
    attempt outright; now the attempt establishes its OWN reference and is
    graded on its merits."""
    fakes = FakeSeams()
    c = _rearm_conductor(
        fakes,
        verify_pilot_transfer_prior={
            "values": {"summed": -20.0}, "at": time.time() - 86400.0,
        },
    )
    # Nothing to compare against yet — the prior did not become a baseline.
    assert c.verify_pilot_transfer_reference is None
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.775, max_db=0.5,
    )
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True
    reference = c.verify_pilot_transfer_reference
    assert reference["values"]["summed"] == pytest.approx(0.775)
    assert reference["at"] > 0.0


def test_verify_level_reference_reset_is_disclosed_when_material():
    """The reset is reported (dated, with the step) — never enforced."""
    fakes = FakeSeams()
    prior_at = time.time() - 86400.0
    c = _rearm_conductor(
        fakes,
        verify_pilot_transfer_prior={"values": {"summed": 0.0}, "at": prior_at},
    )
    assert c.verify_level_reference_reset is None
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.775, max_db=0.5,
    )
    assert _run_phase(c, 1, 1)["accepted"] is True
    disclosed = c.verify_level_reference_reset
    assert disclosed["prior_at"] == prior_at
    assert disclosed["step_db"] == pytest.approx(0.775)


def test_verify_level_reference_reset_is_journalled(caplog):
    """The bench's grep target. ``pilot_transfer_step_db`` in the verify diag
    is the WITHIN-session step; this is the cross-session one it can no longer
    be, and #1870-style corpus sweeps want to count resets without parsing
    every diag line. INFO — a reset is ordinary, not a fault."""
    fakes = FakeSeams()
    c = _rearm_conductor(
        fakes,
        verify_pilot_transfer_prior={
            "values": {"summed": 0.0}, "at": time.time() - 86400.0,
        },
    )
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.775, max_db=0.5,
    )
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert "event=correction.crossover_v2_level_reference_reset" in caplog.text
    assert "step_db=0.775" in caplog.text
    assert "ceiling_db=0.35" in caplog.text
    assert "prior_age_s=" in caplog.text


def test_verify_level_reference_reset_is_silent_when_the_prior_agrees():
    """A prior the session's own chain agrees with is not news — the ceiling
    that defines "the chain moved" is the one that defines "worth saying".
    0.30 dB, comfortably inside the 0.35 dB ceiling rather than exactly on it:
    this test is about agreement, and the boundary itself is pinned (with the
    float care it needs) by
    ``test_verify_pilot_level_shift_boundary_exact_passes_just_above_fires``."""
    fakes = FakeSeams()
    c = _rearm_conductor(
        fakes,
        verify_pilot_transfer_prior={
            "values": {"summed": 0.0}, "at": time.time() - 86400.0,
        },
    )
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.30, max_db=0.5,
    )
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert c.verify_level_reference_reset is None


def test_verify_level_reference_reset_needs_a_dated_prior():
    """An undated record cannot be shown as history (#1942's rule), so it is
    not carried as one — the constructor drops it rather than inventing a
    date, and it never reaches the comparator either way."""
    fakes = FakeSeams()
    c = _rearm_conductor(
        fakes, verify_pilot_transfer_prior={"values": {"summed": 0.0}},
    )
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.775, max_db=0.5,
    )
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert c.verify_level_reference_reset is None


def test_verify_level_shift_still_fires_within_one_session():
    """The gate keeps its stated purpose: a chain that moves DURING a sitting
    still refuses to grade. Attempt 1 fails independently (out of tolerance),
    attempt 2 moves 0.775 dB from it — the round-5 number, now measured
    against a reference this session set itself."""
    fakes = FakeSeams()
    c = _rearm_conductor(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    assert _run_phase(c, 1, 1)["code"] == "verify_out_of_tolerance"
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.775, max_db=5.0,
    )
    verdict = _run_phase(c, 1, 2)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_level_shift"


def test_no_constructor_argument_can_seed_the_g3_comparator():
    """The #1927 negative, engineered rather than checked: there is no longer
    an argument through which a previous session's numbers can become this
    session's baseline. The prior travels ONLY as dated history."""
    params = inspect.signature(CrossoverV2Conductor.__init__).parameters
    assert "verify_pilot_transfer_baseline" not in params
    assert "verify_pilot_transfer_prior" in params
    fakes = FakeSeams()
    c = _rearm_conductor(
        fakes,
        verify_pilot_transfer_prior={
            "values": {"summed": -20.0}, "at": time.time() - 86400.0,
        },
    )
    assert c._verify_pilot_baseline is None


def test_verify_level_shift_copy_is_true_on_both_surfaces():
    """#1924's routing half. One string renders on the measurement page's
    in-session retry (which re-compares the same reference and CAN repeat)
    and on the wizard's fresh-session retry (which since #1927 settles it in
    one capture). So it must command neither and discredit neither: state the
    fact, contextualize the retry, name the escalation conditionally."""
    message = REASON_REGISTRY["verify_level_shift"].message
    assert message == (
        "The microphone's levels changed between measurements, so this check "
        "couldn't settle. Try again — if it repeats, re-measure, or undo to "
        "restore the previous sound."
    )
    # The retired routing: it commanded the retry the phone cannot win.
    assert "re-verify" not in message.lower()
    # The visible primary is named, not undermined — the sibling
    # ``verify_out_of_tolerance`` names its primary too.
    assert "Try again" in message
    # …and the escalation is conditional on the retry repeating, never
    # presented as the only way forward.
    assert "if it repeats, re-measure, or undo" in message


# --- alignment sign contract -----------------------------------------------------


def test_alignment_to_candidate_fields_sign_contract():
    def analysis_with(delay_us, status=ALIGNMENT_OK, polarity="normal"):
        class _A:
            alignment = _alignment(delay_us=delay_us, status=status, polarity=polarity)
        return _A()

    # positive ⇒ tweeter earlier ⇒ tweeter delayed.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0), woofer_role="woofer", tweeter_role="tweeter",
    )
    assert (delay, role, polarity) == (150.0, "tweeter", "keep")
    # negative ⇒ woofer delayed, magnitude non-negative.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(-90.0), woofer_role="woofer", tweeter_role="tweeter",
    )
    assert (delay, role, polarity) == (90.0, "woofer", "keep")
    # inverted polarity maps to the W4 "invert" vocabulary.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0, polarity="inverted"),
        woofer_role="woofer", tweeter_role="tweeter",
    )
    assert polarity == "invert"
    # An edge-clamped estimate is not applied: trims-only candidate.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0, status=ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW),
        woofer_role="woofer", tweeter_role="tweeter",
    )
    assert (delay, role, polarity) == (None, None, None)


# --- phase persistence + session binding (§5.6) -----------------------------------


def test_resume_within_session_skips_accepted_phases():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    snap = c.snapshot()
    assert snap.accepted_phases == (PHASE_CHECK,)

    resumed = CrossoverV2Conductor.hydrate(
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
    program = resumed._program_for_phase(PHASE_MEASURE)
    assert program.segment("sweep_w").gain_db == pytest.approx(-11.0)


def test_new_session_invalidates_check_and_measure_evidence():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    snap = c.snapshot()
    assert PHASE_MEASURE in snap.accepted_phases

    fresh = CrossoverV2Conductor.hydrate(
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


# --- session volume lifecycle (§5.5) ----------------------------------------------


class _FakeVolumePlan:
    def __init__(self, needs_recovery: bool = False) -> None:
        self.needs_recovery = needs_recovery
        self.opened: list = []
        self.abandoned: list = []

    async def open(self, volume_db, set_cb, get_cb):
        self.opened.append(volume_db)
        return "opened"

    async def abandon(self, set_cb, get_cb):
        self.abandoned.append(True)
        return "exact_restored"


def test_open_measurement_volume_refuses_needs_recovery():
    """The recovery gate keys on needs_recovery, NOT unresolved alone (W2 gate)."""
    plan = _FakeVolumePlan(needs_recovery=True)
    with pytest.raises(CrossoverV2FlowError):
        asyncio.run(open_measurement_volume(
            plan,
            safety_profile={},
            target_fingerprints=["fp"],
            set_main_volume_db=None,
            get_main_volume_db=None,
        ))
    assert plan.opened == []


def test_open_measurement_volume_derives_via_ssot(monkeypatch):
    plan = _FakeVolumePlan()
    import jasper.active_speaker.session_volume_plan as svp

    monkeypatch.setattr(
        svp, "session_measurement_volume_db", lambda profile, fps, **kw: -20.0
    )
    result = asyncio.run(open_measurement_volume(
        plan,
        safety_profile={"profile": True},
        target_fingerprints=["fp-w", "fp-t"],
        set_main_volume_db=None,
        get_main_volume_db=None,
    ))
    assert result == "opened"
    assert plan.opened == [-20.0]


def test_session_death_abandons_volume():
    plan = _FakeVolumePlan()
    result = asyncio.run(abandon_measurement_volume(
        plan, set_main_volume_db=None, get_main_volume_db=None,
    ))
    assert result == "exact_restored"
    assert plan.abandoned == [True]


# --- position-group choreography (flat-linearization PR-3b) ------------------
#
# State-walk tests over the group lifecycle, driven through the fake seams. The
# cloud positions play the VERIFY-shaped summed program, so FakeSeams' analyze
# dispatch (keyed on the PROGRAM's phase) returns `_verify_analysis` for them
# with no new factory — the same reason `program_analysis` needed no new
# dispatch branch.


# Stage 1 (measure) and stage 2 (verify) are separate SESSIONS since the
# two-stage split (work order D1/D2), so they are separate maps and separate
# conductors — there is no single index space spanning the whole journey any
# more.
CLOUD_MAP = build_v2_cloud_index_phase_map()
CLOUD_MEASURE_INDEXES = tuple(
    i for i, p in sorted(CLOUD_MAP.items()) if p == PHASE_CLOUD_MEASURE
)
STAGE2_SHAPE = resolve_plan_shape()
STAGE2_MAP = build_v2_verify_index_phase_map(plan_shape=STAGE2_SHAPE)
VERIFY_INDEX = next(i for i, p in STAGE2_MAP.items() if p == PHASE_VERIFY)
CLOUD_VERIFY_INDEXES = tuple(
    i for i, p in sorted(STAGE2_MAP.items()) if p == PHASE_CLOUD_VERIFY
)
# A deliberately SHORT verify group — the anchor plus ONE prompted position.
# Not a shipped plan shape (the tiers ship M=1 or M=6), but the conductor takes
# its index map as a constructor argument, and this is the compact way to reach
# the position floor: give that one position up and the group has no curve left
# and nothing unwalked to recover with. Building it from the same vocabulary the
# production map builder emits, so it cannot drift into a shape the conductor
# would never see.
SHORT_VERIFY_MAP = {1: PHASE_VERIFY, 2: PHASE_CLOUD_VERIFY}
SHORT_VERIFY_CLOUD_INDEXES = tuple(
    i for i, p in sorted(SHORT_VERIFY_MAP.items()) if p == PHASE_CLOUD_VERIFY
)


def _cloud_conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Conductor:
    kwargs.setdefault("index_phase_map", CLOUD_MAP)
    # What ``prepare_v2_session`` declares: this measuring session has no
    # VERIFY entry of its own, and the correction it proposes is verified by
    # stage 2 (work order D2). Without it the fit would be refused boost, which
    # is the shape of the regression the declaration exists to prevent.
    kwargs.setdefault("post_apply_verifies", True)
    return _conductor(fakes, **kwargs)


def _walk(conductor, indexes, start_attempt: int) -> int:
    attempt = start_attempt
    for index in indexes:
        _run_phase(conductor, index, attempt)
        attempt += 1
    return attempt


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

_COMB_N_FFT = 8192
_COMB_RATE = 48_000


def _comb_summed_response(seed: int, *, r: float = 0.37, delay_samples: int = 15):
    """A two-path summed response — the shape a position-invariant
    interference comb has, and the ONE shape the power-vs-median screen
    structurally cannot catch on its own (plan "S0 executed" § e.1).

    Built as a real rfft-grid transfer function because
    ``cloud_position_capture`` derives the IR the echo detector reads by
    inverting exactly this array; the coarse 64-bin ``_driver_response`` above
    cannot carry an invertible one.
    """
    freqs = np.fft.rfftfreq(_COMB_N_FFT, 1.0 / _COMB_RATE)
    rng = np.random.default_rng(seed)
    tf = 1.0 + r * np.exp(-2j * np.pi * freqs * (delay_samples / _COMB_RATE))
    tf = tf + rng.normal(0.0, 1e-6, tf.shape)
    # The comb rides a ROOM, not a flat 0 dB reference (PR-L4) — a real
    # position measures the speaker's own in-room shape with the interference
    # comb on top of it, and the pre-apply cloud's spec residual is read off
    # exactly that product. See `_in_room_summed_db` for why a flat base is not
    # a demanding fixture but an impossible one. Zero-phase magnitude shaping,
    # so the two-path ladder the echo detector reads is untouched.
    tf = tf * 10.0 ** (
        np.interp(freqs, _SUMMED_FREQS_HZ, _in_room_summed_db()) / 20.0
    )
    return DriverResponse(
        role="summed", freqs_hz=freqs,
        magnitude_db=20.0 * np.log10(np.maximum(np.abs(tf), 1e-12)),
        complex_tf=tf.astype(complex),
        gating={"applied": True, "window_ms": 8.0},
        snr=None, validity_floor_hz=140.0,
    )


def _comb_cloud_analysis_factory():
    """A ``verify``-program analysis factory whose every capture carries the
    same comb — one per call, so a group of them is position-INVARIANT."""
    counter = {"n": 0}

    def factory(program) -> ProgramAnalysis:
        counter["n"] += 1
        return ProgramAnalysis(
            phase="verify",
            program_id=program.program_id,
            locations=(_loc("sweep_verify", "summed_sweep", confidence=0.9),),
            summed_response=_comb_summed_response(4000 + counter["n"]),
            summed_ripple_db=1.1,
            verify_tracking={
                "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            },
            linearity_ok=True,
        )

    return factory


def _walk_measure_cloud_to_close(c, *, start_attempt: int = 1) -> dict:
    """CHECK → MEASURE → every pre-apply cloud position → the CONFIRM.

    Returns the closing verdict MERGED with the confirm's own payload, which is
    where ``candidate_fingerprint``/``auto_apply`` live since
    flow-simplification §2.6 moved the fit off the final position's acceptance
    and onto the household's confirmation past it.

    A position-invariant cloud legitimately trips PR-3b's geometry-locked
    retake (that is the point of the verdict), so the last index is re-walked
    until it is accepted — bounded by ``GEOMETRY_RETRY_POSITIONS``' own budget
    rather than looping forever.
    """
    attempt = _walk(c, (1, 2), start_attempt)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    for _ in range(GEOMETRY_RETRY_POSITIONS + 1):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        if verdict["accepted"]:
            return {**verdict, **_confirm_cloud(c)}
    raise AssertionError("the cloud-measure group never closed")


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


def _walk_measure_cloud_to_accept(c, *, start_attempt: int = 1) -> int:
    """CHECK → MEASURE → the whole pre-apply cloud, stopping at the ACCEPT.

    The HELD WINDOW itself — walked, unconfirmed, the phone still offering
    Retake — which is where the eager fit lives and which
    ``_walk_measure_cloud_to_close`` walks straight past. Returns the next
    unused attempt number, so a caller can drive a voluntary retake of the
    final position from exactly where the household would.
    """
    attempt = _walk(c, (1, 2), start_attempt)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    for _ in range(GEOMETRY_RETRY_POSITIONS + 1):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        if verdict["accepted"]:
            assert verdict["awaiting_confirm"] is True
            return attempt
    raise AssertionError("the cloud-measure group never closed")


def _count_builds(c) -> list:
    """Record every FIT this conductor runs, so a test can tell a commit from
    a re-fit. The eager rider's whole claim is about which of the two the
    household's confirmation pays for."""
    builds: list = []
    real_build = c._build_candidate

    def _counting_build(analysis, cloud):
        builds.append(1)
        return real_build(analysis, cloud)

    c._build_candidate = _counting_build
    return builds


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

    _assert_room_layer_can_read_the_evidence(c.candidate, pipeline)


def _assert_room_layer_can_read_the_evidence(candidate, pipeline, tmp_root=None):
    """End-to-end: a REAL produced candidate resolves through the room seam.

    The seam's own unit tests drive synthetic payloads; this is the one place
    that proves the producer in ``crossover_v2_flow`` and the reader in
    ``jasper.correction.applied_speaker_evidence`` agree on key names and
    shapes across a live conductor run (issue #1787, plan RC1 / D2).
    """
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from jasper.correction.applied_speaker_evidence import (
        AppliedSpeakerEvidence,
        resolve_applied_speaker_evidence,
    )

    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(tmp_root or raw_root)
        artifact = (
            root / "bundle" / "evidence" / "v1" / "artifacts"
            / "crossover_v2" / "session" / "candidate.json"
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(candidate.to_dict()), encoding="utf-8")

        applied = {
            # Non-empty linearization marks this as an ACOUSTIC commission —
            # the seam's discriminator against electrical-only profiles, and
            # true here since this candidate came from a real cloud fit.
            "linearization": dict(candidate.linearization),
            "source": {"measured_candidate_fingerprint": candidate.fingerprint},
        }
        target = (
            "jasper.active_speaker.baseline_profile."
            "load_applied_baseline_profile_state"
        )
        with patch(target, return_value=applied):
            result = resolve_applied_speaker_evidence(
                state_path=root / "state.json", sessions_dir=root
            )

    assert isinstance(result, AppliedSpeakerEvidence), result
    assert result.candidate_fingerprint == candidate.fingerprint
    assert result.validity_floor_hz == pipeline["validity_floor_hz"]
    assert list(result.gated_spec_freqs_hz) == pipeline["curve"]["freqs_hz"]
    assert result.has_gated_curve is True


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


def _lock(monkeypatch, *, thin: bool = False):
    """Force the group-end geometry verdict.

    ``_geometry_verdict_from_combined`` is a pure function of an already-
    combined result; manufacturing a genuinely position-invariant echo across
    a synthetic cloud is ``spatial_combine``'s own test territory (and is
    covered there). What this file owns is the CONDUCTOR's response to a
    verdict, so the verdict is injected.

    Patches ``_geometry_verdict_from_combined`` rather than
    ``cloud_geometry_verdict`` (S3 review finding, 2026-07-26:
    ``_close_cloud_group`` combines each group's positions exactly ONCE and
    derives its retry-gating verdict from that single ``combined`` object via
    ``_geometry_verdict_from_combined`` — it no longer calls
    ``cloud_geometry_verdict`` at all, which stays a positions-only
    convenience wrapper for other callers, e.g. the corpus acceptance test).
    The real (unmocked) ``combine_cloud_positions`` still runs underneath —
    this lambda ignores its ``combined`` argument entirely, so whatever the
    fake seams' synthetic captures actually combine to is irrelevant to the
    injected verdict.
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    monkeypatch.setattr(
        flow, "_geometry_verdict_from_combined",
        lambda combined, n_positions: {
            "locked": True, "reason": "geometry_locked", "thin_evidence": thin,
            "n_positions": n_positions, "median_tau_us": 320.0,
        },
    )


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
        ({"linearity": False}, flow.REASON_AGC_BEHAVIORAL_FAIL),
        ({"pilot_snr_ok": False}, flow.REASON_SNR_FLOOR),
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

    diagnosis = flow.verify_inconclusive_diagnosis(True)
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
    # Isolate the close seam under test from delta-probe arithmetic; the real
    # classifier's mapping/copy is independently exhaustive below.
    c._delta_probe_refusal = (  # type: ignore[method-assign]
        lambda _probe: (
            REASON_CORRECTION_MODEL_ERROR
            if c.current_phase == PHASE_CLOUD_VERIFY
            else None
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


def test_retain_position_seam_gets_every_accepted_position_with_its_prompt():
    """The forensic record the choreography owes: the prompt is the only durable
    statement of WHERE a curve was measured."""
    retained: list = []
    fakes = FakeSeams()
    seams = replace(
        fakes.seams(),
        retain_position=lambda pid, result, meta: retained.append((pid, dict(meta))),
    )
    c = CrossoverV2Conductor(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=seams, index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)

    assert [pid for pid, _m in retained] == [
        f"{PHASE_CLOUD_MEASURE}_{i:02d}" for i in CLOUD_MEASURE_INDEXES
    ]
    prompts = [meta["prompt"] for _pid, meta in retained]
    assert prompts == [p.text for p in CLOUD_POSITION_PROMPTS[: len(retained)]]
    assert sum(1 for _pid, meta in retained if meta["wide"]) >= 2
    # Each position's NAMED QUESTION rides its record, from the same table row
    # the prompt came from (attribution-stage plan §5 promotion-queue item 1).
    # The prompt string cannot be parsed back into a role, so the label is the
    # only way the attribution stage sees a labelled sample rather than an
    # anonymous member of an average — and it has to be the row's, not a guess.
    roles = [meta["role"] for _pid, meta in retained]
    assert roles == [p.role for p in CLOUD_POSITION_PROMPTS[: len(retained)]]
    # …and the shipped walk really does sample all three questions, which is
    # the point of labelling them at all: a walk that only ever produced one
    # role would be the same average with extra words.
    assert set(roles) == set(POSITION_ROLES)
    for _pid, meta in retained:
        assert meta["phase"] == PHASE_CLOUD_MEASURE
        assert meta["session_id"] == SESSION
        assert meta["captured_at"] > 0


def test_a_retake_records_the_prompt_it_was_actually_given(monkeypatch):
    """B3: the sidecar's prompt is the only durable statement of WHERE a curve
    was measured. A geometry retake follows a wider-spot rung, not the position
    table's entry — recording the table entry would name a spot the operator
    was explicitly told to abandon."""
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Conductor(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            retain_position=lambda pid, r, meta: retained.append(dict(meta)),
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


def test_retain_position_failure_never_fails_the_capture(caplog):
    """Evidence retention is forensics, not a gate: a full disk must not turn an
    acoustically-good position into a retake."""
    def boom(_pid, _result, _meta):
        raise OSError("no space left on device")

    fakes = FakeSeams()
    c = CrossoverV2Conductor(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), retain_position=boom),
        index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    with caplog.at_level(logging.WARNING):
        verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[0], attempt)
    assert verdict["accepted"] is True
    assert "crossover_v2_position_retain_failed" in caplog.text


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

    resumed = CrossoverV2Conductor.hydrate(
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

    fresh = CrossoverV2Conductor.hydrate(
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


def test_preapply_cloud_uses_protected_graph_program_only():
    """R15: CLOUD_MEASURE is the distinct protected-stereo neutral
    program; post-apply VERIFY/CLOUD_VERIFY retain one mono production object."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    protected = c._program_for_phase(PHASE_CLOUD_MEASURE)
    applied = c._program_for_phase(PHASE_VERIFY)
    assert protected is not applied
    assert protected.channels == 2
    assert {segment.channel for segment in protected.stimulus_segments()} == {0}
    assert {segment.role for segment in protected.stimulus_segments()} == {"summed"}
    assert applied.channels == 1
    assert c._program_for_phase(PHASE_CLOUD_VERIFY) is c._program_for_phase(
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
    # ``cloud_capture_target()`` is unchanged at 16 because it still names the
    # WHOLE journey (10 + 6), which is what the tier chooser promises.
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


def test_the_stage_2_plan_walks_the_tiers_own_verify_shape():
    """Work order D2, owner-confirmed 2026-07-29 — and the re-derivation of
    ``test_an_express_plan_emits_no_cloud_verify_and_ends_on_verify``, whose
    subject (the ``M = 1`` done-screen placement rule) moved out of stage 1's
    builder and into stage 2's along with the post-apply group itself.

    Full's stage 2 is the six-position spatial walk; Express's is the single
    anchor at the mark. The phone's END screen rides the LAST entry either way
    (``renderPlanAllDone`` reads the final wire index), and Express's copy
    claims LESS because it verified less (§1.3).
    """
    from jasper.capture_relay.spec import MAX_CAPTURE_PLAN_ATTEMPTS

    full = build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape())
    assert full.capture_target == DEFAULT_CLOUD_VERIFY_POSITIONS == 6
    assert [e.kind_label for e in full.entries] == (
        ["verify"] + ["cloud_verify"] * (DEFAULT_CLOUD_VERIFY_POSITIONS - 1)
    )
    assert [e.index for e in full.entries] == list(range(6))
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
      of jts.local's five done verdicts OPENS with; jts.local owns the
      divergence, as the only surface whose component vocabulary can carry it.
      All five are pinned, not the two this fix reasoned about: the phone bakes
      one headline for both tiers and all outcomes, so a single unpinned
      variant is enough to reopen the defect.
    """
    import inspect

    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    assert set(inspect.signature(build_v2_verify_capture_plan).parameters) == {
        "fc_hz", "plan_shape",
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
        return build_crossover_envelope_v2({
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "crossover_v2": {
                "phase": "done", "verify": {"outcome": "pass"}, **v2,
            },
        })["verdict_text"]

    # …so the invariant holds only if EVERY jts.local done verdict opens with
    # it. There are five, independently authored in three separate branches of
    # the PHASE_DONE arm, and pinning the two this fix reasoned about would
    # leave the other three free to drift out from under the phone.
    variants = {
        "express": _verdict(tier=TIER_EXPRESS),
        "generic": _verdict(tier=TIER_FULL),
        "spec_fail": _verdict(
            tier=TIER_FULL,
            cloud={PHASE_CLOUD_VERIFY: {"overall_passed": False}},
        ),
        "grade_inconclusive": _verdict(
            tier=TIER_FULL,
            post_apply_grade={"graded": False, "state": "inconclusive"},
        ),
        "grade_never_finished": _verdict(
            tier=TIER_FULL, post_apply_grade={"graded": False, "state": ""},
        ),
    }
    assert len(set(variants.values())) == 5, (
        "five DISTINCT verdicts, or a fixture stopped reaching its branch"
    )
    assert "further from flat than the target" in variants["spec_fail"]
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
    c = CrossoverV2Conductor(
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
    only on a SUCCESSFUL publish, not a bare attempt — stated three times
    (the ``__init__`` field comment, the publish guard's own comment, and
    the HANDOFF doc) and asserted nowhere until this test. Marking on the
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
    # Stage 1 alone is 7 minutes at Full and 5 at Express; the whole journey is
    # what tier_display_info sums, pinned in its own test below.
    assert build_v2_capture_plan(_roles(), FC_HZ).estimated_minutes() == 7
    assert (
        build_v2_capture_plan(_roles(), FC_HZ, tier=TIER_EXPRESS).estimated_minutes()
        == 5
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
            stage1 = build_v2_capture_plan(roles, fc_hz, plan_shape=shape)
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
        shape = cloud_walk_shape(positions)
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
    1-entry stage 2 is not a walk and gets no shape line at all."""
    full = build_v2_verify_session_spec(
        FC_HZ, acknowledgement_binding="b" * 24, plan_shape=resolve_plan_shape(),
    )
    shape = cloud_walk_shape(DEFAULT_CLOUD_VERIFY_POSITIONS, post_apply=True)
    assert len([c for c in full.screen if c["type"] == "steps"]) == 1
    assert shape in [c["text"] for c in full.screen if c["type"] == "note"]
    # Same derived ceiling and the same retake honesty as stage 1 — the
    # geometry-locked retake is armed on this group too.
    reach = cloud_walk_reach_cm(DEFAULT_CLOUD_VERIFY_POSITIONS)
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
    assert cloud_walk_shape(1) == ""
    assert cloud_walk_shape(1, post_apply=True) == ""
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
    ~30 cm+ offsets are what support the LF edge. Both groups walk the SAME
    ordered table from the front, so the shortest group either can be
    CONFIGURED to run — its declared MIN, not its default — must still contain
    at least two wide moves. Reordering the table for readability would
    silently delete the LF half of the measurement — hence this test rather
    than a comment.

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
    shortest_group = min(
        MIN_CLOUD_MEASURE_POSITIONS, MIN_CLOUD_VERIFY_POSITIONS
    )
    walked = CLOUD_POSITION_PROMPTS[: shortest_group - 1]
    assert sum(1 for prompt in walked if prompt.wide) >= 2
    # The floors are DERIVED from the table, so a reorder moves them rather
    # than leaving a stale literal behind.
    derived = _min_positions_for_two_wide_offsets()
    assert MIN_CLOUD_VERIFY_POSITIONS == derived
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
# conductor's real _compose_*_program calls that actually play. Both must
# enable the prelude via the SAME COURTESY_PRELUDE_ENABLED constant, or the
# phone would stop recording before the real (longer) program finishes --
# mirrors the existing +15 s MEASURE-lengthening proof from sweep-composition
# PR-A (#1668).


def test_capture_plan_duration_matches_courtesy_prelude_program_exactly():
    assert COURTESY_PRELUDE_ENABLED is True
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    check, measure = plan.entries[0], plan.entries[1]
    # The VERIFY-shaped program's duration now rides STAGE 2's anchor (the
    # split moved the phase, not the arithmetic) — and stage 1's cloud entries,
    # which play the same program, are checked against it below.
    verify = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(),
    ).entries[0]
    assert verify.kind_label == "verify"

    from jasper.audio_measurement.program import (
        BASE_STIMULUS_PEAK_DBFS,
        build_check_program,
        build_measure_program,
        build_verify_program,
    )

    roles = _roles()
    nominal_gains = {rb.role: BASE_STIMULUS_PEAK_DBFS for rb in roles}
    nominal_check = build_check_program(roles, courtesy_prelude=True)
    nominal_measure = build_measure_program(
        nominal_gains, roles,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=True,
    )
    nominal_verify = build_verify_program(
        FC_HZ,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=True,
    )
    assert check.duration_ms == _program_duration_ms(nominal_check) + CAPTURE_ENTRY_MARGIN_MS
    assert measure.duration_ms == _program_duration_ms(nominal_measure) + CAPTURE_ENTRY_MARGIN_MS
    assert verify.duration_ms == _program_duration_ms(nominal_verify) + CAPTURE_ENTRY_MARGIN_MS
    # Every cloud position plays the SAME mono summed sweep VERIFY does, so its
    # recording window must be that program's — a shorter one would truncate
    # the sweep and a longer one would record silence into the analysis.
    for entry in plan.entries:
        if entry.kind_label.startswith("cloud_"):
            assert entry.duration_ms == verify.duration_ms


def test_capture_plan_duration_is_longer_than_the_pre_1677_shape():
    """Direct proof the prelude actually lengthens the phone's recording
    budget (not just that the two composition paths agree with EACH OTHER,
    which the previous test already pins) -- the "+15 s"-style regression
    check named in the issue."""
    from jasper.audio_measurement.program import (
        COURTESY_TONE_BEEP_COUNT,
        COURTESY_TONE_BEEP_DURATION_S,
        COURTESY_TONE_BEEP_GAP_S,
        COURTESY_TONE_TRAILING_SILENCE_S,
        build_check_program,
    )

    expected_prelude_ms = 1000.0 * (
        COURTESY_TONE_BEEP_COUNT * COURTESY_TONE_BEEP_DURATION_S
        + (COURTESY_TONE_BEEP_COUNT - 1) * COURTESY_TONE_BEEP_GAP_S
        + COURTESY_TONE_TRAILING_SILENCE_S
    )
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


def test_conductor_composed_programs_include_courtesy_tone_by_default():
    """The conductor's REAL playback composition (not the nominal planning
    path above) also carries the prelude -- COURTESY_PRELUDE_ENABLED wired
    into every _compose_*_program call."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    check_tone_ids = {
        s.segment_id for s in c._check_program.segments if s.kind == KIND_COURTESY_TONE
    }
    assert check_tone_ids == {"courtesy_tone_ch0", "courtesy_tone_ch1"}

    measure_prog = c._compose_measure_program({"woofer": -11.0, "tweeter": -13.0})
    measure_tone_ids = {
        s.segment_id for s in measure_prog.segments if s.kind == KIND_COURTESY_TONE
    }
    assert measure_tone_ids == {"courtesy_tone_ch0", "courtesy_tone_ch1"}

    verify_tone_ids = {
        s.segment_id for s in c._verify_program.segments if s.kind == KIND_COURTESY_TONE
    }
    assert verify_tone_ids == {"courtesy_tone_ch0"}  # VERIFY is mono


def test_bind_program_playback_seams_uses_inline_setconfig(tmp_path):
    """The production seams keep the statefile boot anchor untouched: load and
    restore both ride ``set_active_config_raw`` (SetConfig), never
    ``set_config_file_path`` — the crash-recovery-MUTED invariant."""
    from jasper.active_speaker.crossover_v2_flow import bind_program_playback_seams

    calls: list = []

    class _FakeCam:
        async def get_config_file_path(self, *, best_effort):
            calls.append(("get_path", best_effort))
            return str(tmp_path / "entry.yml")

        async def set_active_config_raw(self, text, *, best_effort):
            calls.append(("set_raw", text, best_effort))
            return True

        async def set_config_file_path(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("must never repoint the persisted statefile")

    entry = tmp_path / "entry.yml"
    entry.write_text("prior: graph\n", encoding="utf-8")
    seams = bind_program_playback_seams(
        _FakeCam(),
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
    assert set(seams) == {
        "read_current_config_path", "load_program_graph", "restore_graph",
        "play_wav", "readmit", "writer_lock", "record_entry_anchor",
        "clear_entry_anchor",
    }
    assert asyncio.run(seams["read_current_config_path"]()) == str(entry)
    assert asyncio.run(seams["load_program_graph"]("program: graph\n")) is True
    assert asyncio.run(seams["restore_graph"](str(entry))) is True
    assert calls == [
        ("get_path", False),
        ("set_raw", "program: graph\n", False),
        ("set_raw", "prior: graph\n", False),
    ]


def _dummy_program():
    from jasper.audio_measurement.program import build_check_program

    return build_check_program(_roles(), ambient_s=0.5, pilot_duration_s=0.3)


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
    from jasper.capture_relay.spec import CaptureSpec

    reparsed = CaptureSpec.from_dict(spec.to_dict())
    assert reparsed.capture_plan.entries == spec.capture_plan.entries


def test_shipped_v2_plans_keep_their_retry_budget_when_the_relay_ceiling_moves():
    """The v2 flow's retry budget is POLICY, not the relay's transport limit.

    Both builders once passed ``capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS``
    verbatim, which was harmless only while the two constants happened to be
    equal at 8. Raising the relay ceiling to 32 for multi-position capture
    plans would otherwise have quadrupled these shipped flows' retry budget and
    changed their wire bytes as a side effect. Pin each flow's budget to this
    flow's own constants, and pin that both stay storable.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        CAPTURE_PLAN_MAX_ATTEMPTS,
        build_v2_capture_plan,
        build_v2_verify_capture_plan,
    )
    from jasper.capture_relay.spec import (
        LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS,
        MAX_CAPTURE_PLAN_ATTEMPTS,
    )

    assert CAPTURE_PLAN_MAX_ATTEMPTS == LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS == 8
    assert CAPTURE_PLAN_MAX_ATTEMPTS <= MAX_CAPTURE_PLAN_ATTEMPTS

    cloud = build_v2_capture_plan(_roles(), FC_HZ)
    one_entry = build_v2_verify_capture_plan(FC_HZ)
    # RE-DERIVED for the two-stage split: no single session carries the whole
    # journey any more. Stage 1 is 1 + N = 10 captures with
    # 10 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE = 17 attempts;
    # ``cloud_capture_target()``/``cloud_plan_max_attempts()`` keep their
    # whole-journey meaning (16 / 23), which is what the relay-capacity guard
    # and jasper-doctor read as the conservative bound.
    assert cloud.capture_target == 10
    assert cloud.max_attempts == 17
    assert cloud_capture_target() == 16
    assert cloud_plan_max_attempts() == 23
    assert cloud.max_attempts < cloud_plan_max_attempts()
    assert one_entry.capture_target == 1
    assert one_entry.max_attempts == CAPTURE_PLAN_MAX_ATTEMPTS
    # The re-verify re-arm stays at or below the legacy ceiling, so it never
    # probes the relay's capability endpoint and keeps working against a
    # pre-capacity Worker. The cloud plan is above it BY DESIGN — that probe is
    # exactly the fail-closed gate PR-3a shipped for this plan.
    assert one_entry.max_attempts <= LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS
    assert cloud.max_attempts > LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS
    assert cloud.max_attempts <= MAX_CAPTURE_PLAN_ATTEMPTS


def test_worst_case_cloud_plan_fits_the_relay_index_space():
    """The choreography constants and the relay's blob-index ceiling are
    coupled: PR-3a sized ``MAX_CAPTURE_PLAN_ATTEMPTS`` from PR-3b's declared
    maxima, so raising a cloud constant past what the relay can carry must fail
    here — hardware-free — rather than stranding an operator on a refused blob
    index at position 20."""
    from jasper.capture_relay.spec import MAX_CAPTURE_PLAN_ATTEMPTS

    assert_cloud_plan_fits_relay_capacity()
    worst_entries = cloud_capture_target(
        cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
        cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
    )
    # The work order's own arithmetic, spelled out:
    # 2 (CHECK+MEASURE) + (N_MAX-1) + M + retries <= the relay ceiling.
    assert (
        2
        + (MAX_CLOUD_MEASURE_POSITIONS - 1)
        + DEFAULT_CLOUD_VERIFY_POSITIONS
        + GEOMETRY_RETRY_POSITIONS
    ) <= MAX_CAPTURE_PLAN_ATTEMPTS
    assert worst_entries == 19
    assert (
        cloud_plan_max_attempts(
            cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
            cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
        )
        == 26
        <= MAX_CAPTURE_PLAN_ATTEMPTS
    )


@pytest.mark.parametrize("positions", [MIN_CLOUD_MEASURE_POSITIONS - 1,
                                       MAX_CLOUD_MEASURE_POSITIONS + 1])
def test_cloud_position_count_outside_the_declared_range_is_refused(positions):
    with pytest.raises(CrossoverV2FlowError):
        build_v2_capture_plan(_roles(), FC_HZ, cloud_measure_positions=positions)


def test_session_wall_clock_ceiling_scales_with_the_plan_and_is_capped():
    """The walked-away guarantee survives a 16-capture session — and stays a
    guarantee: the ceiling grows with plan length but can never be scaled
    away."""
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_WALL_CLOCK_CEILING_S,
        MAX_WALL_CLOCK_CEILING_S,
    )

    shipped = build_v2_capture_plan(_roles(), FC_HZ)
    # RE-DERIVED (work order D2): each STAGE arms its own ceiling from its own
    # plan. Stage 1 is 10 captures ⇒ 1800 + (10 - 3) * 120 = 2640 s, down from
    # the single session's 3360 s. Neither number fits inside the 900 s relay
    # TTL and this test must not be read as claiming otherwise; what the split
    # buys is a lower worst case and a fresh TTL per stage.
    assert session_wall_clock_ceiling_s(shipped) == 2640.0
    assert session_wall_clock_ceiling_s(
        build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape())
    ) == 2160.0
    biggest = build_v2_capture_plan(
        _roles(), FC_HZ,
        cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
        cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
    )
    # 1800 + (13 - 3) * 120 = 3000 s: the biggest stage-1 plan no longer
    # reaches the hard cap, so the cap is exercised on a plan long enough to
    # need it rather than left unpinned.
    assert session_wall_clock_ceiling_s(biggest) == 3000.0
    assert MAX_WALL_CLOCK_CEILING_S == 3600.0
    assert session_wall_clock_ceiling_s(
        types.SimpleNamespace(capture_target=100)
    ) == MAX_WALL_CLOCK_CEILING_S
    # The 1-entry re-verify never widens the baseline.
    assert (
        session_wall_clock_ceiling_s(build_v2_verify_capture_plan(FC_HZ))
        == DEFAULT_WALL_CLOCK_CEILING_S
    )


# Golden wire bytes for the two shipped v2 capture plans, canonicalized exactly
# the way `PiCaptureSession.capture_spec_json` serializes the enclosing spec
# (`json.dumps(..., separators=(",", ":"))`), so these really are the bytes the
# phone receives — not a proxy for them.
#
# WHAT MUST NEVER CHANGE THEM: raising the relay's transport ceiling
# (`capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`). That is the original point
# of this pin — the capacity raise from 8 to 32 had to be invisible to the
# shipped flows, and a value-level assertion alone would not have caught a
# serialization change that came along with it.
#
# WHAT LEGITIMATELY CHANGES THEM: editing a `screen` title/body/auto-advance,
# changing the plan's capture target or attempt budget, altering
# `CapturePlan.to_dict`'s schema, or shifting any composed program's length
# (prelude/pilot durations, `CAPTURE_ENTRY_MARGIN_MS`) — every one of those
# changes what a household's phone is told to do, so a failure here is a prompt
# to confirm the change was intended, not a nuisance.
#
# TO UPDATE: run the assertion, read the actual digest out of the failure
# message, and paste it here in the same commit as the intended change.
#
# UPDATED 2026-07-26 (flat-linearization PR-3b): the "3-entry" main-session
# entry became the CLOUD plan — the intended product change, not drift. The
# measurement is now the spatial cloud (plan fundamental 1), so the main
# session emits CHECK + MEASURE + N−1 prompted pre-apply positions + VERIFY
# + M−1 prompted post-apply positions. The re-verify re-arm plan is UNCHANGED
# and its digest is byte-for-byte the pre-PR-3b one: it re-runs the
# single-position tracking verdict, and evidence cannot cross relay sessions
# anyway (§5.6), so a cloud there could never join the original one.
#
# RE-DERIVED 2026-07-26 (round-1 review): N 8 → 9 (adjudication 3a — the
# delivered curve must rest on 8 summed sweeps, which is N−1, so the floor is
# met in CURVES not positions) took the plan 15 → 16 entries, and the entry
# titles gained the "— hold still" suffix that disambiguates them from the
# phone's own capture counter (nit N1). Both are intended copy/shape changes.
#
# UPDATED 2026-07-27 (flow-simplification PR-U1): the screen GRAMMAR changed on
# every entry of both shipped plans — each now carries the one server-derived
# counter (`progress`), the instruction as `title`, and the supporting clause
# as `body` (§2.1); the VERIFY entry additionally gained the
# `confirm_title`/`confirm_body` keys the post-apply tap renders (§2.2), and
# the 1-entry re-verify leads with the "you do NOT need to redo the walk"
# sentence (§2.4). All intended copy changes. Program DURATIONS deliberately
# did NOT move at THAT revision: the §2.5 courtesy-tone fix reordered where
# the prelude is spliced without changing its length, so every `duration_ms`
# was byte-identical to the pre-fix plan — a useful independent check that the
# pacing change was a reorder and not a lengthening. (The 2026-07-28 revision
# below is the first one that does lengthen them, and says so.)
#
# ADDED 2026-07-27: a third `"express"` pin. The express tier is a second
# SHIPPED plan shape (N=5, M=1 — flow-simplification §1.1), so it earns the
# same protection the full plan has: a change to its copy, counts, or the
# M=1 done-screen placement must be an intended edit, not drift.
#
# RE-DERIVED 2026-07-28 (issues #1810 / #1812): this time the DURATIONS did
# move, and only they — the pre-pilot ambient window adds exactly 1000 ms to
# every program that carries a leading pilot pair. Measured on this fixture:
# check 22819 ms (unchanged — CHECK's own 12 s ambient window already served
# the guard), measure 39385 → 40385, verify and all 14 cloud entries
# 16207 → 17207. Copy, counts, screen keys and byte LENGTH are all identical
# (3897 / 2107 / 324 bytes before and after), because the digit counts of the
# changed numbers did not change — which is precisely why these pins are
# hashes and not lengths.
#
# RE-DERIVED 2026-07-28 (issue #1823): MEASURE's entry — index 1 of both the
# cloud and express plans — flipped from `auto_advance: countdown` to `tap`,
# dropping the countdown-only `countdown_s`/`cancelable` keys with it, and its
# `body` now names what the tap is consenting to. Net +32 bytes on each plan:
# the removed countdown keys are smaller than the added sentence. That sentence
# was rewritten TWICE before landing, which is the useful part of the story —
# #1825/#1829 landed mid-review and made a flat "louder" false in a quiet room
# (hence the "can be the loudest" hedge), and a plain-language ruling then
# replaced "at the level the fit needs" with wording that says what the level
# is FOR. Both are household-visible copy, so both moved these digests; the
# pins are re-derived against the tree they ship on, never copied forward. The
# 1-entry re-verify plan has no MEASURE entry and its digest is byte-for-byte
# unchanged, which is the check that this edit touched only what it meant to.
#
# RE-DERIVED 2026-07-29 (issue #1806, PR-T3 — the two-stage split). This is the
# largest movement these pins have ever taken, because the SHAPE moved rather
# than the copy: one 16-entry session became a 10-entry measuring session and a
# separate 6-entry post-apply one (work order D1/D2). The keys are renamed to
# say which stage each is, and two NEW shipped shapes earn pins of their own —
# stage 2 is a shipped plan now, not a hypothetical.
#
#   cloud     → stage1-full    16 entries, 3929 B → 10 entries, 2301 B
#                              (target 16 → 10, max_attempts 23 → 17)
#   express   → stage1-express  7 entries, 2139 B →  6 entries, 1531 B
#                              (target 7 → 6, max_attempts 14 → 13)
#   (new)       stage2-full     6 entries, 1612 B  (target 6, max_attempts 13)
#   (new)       stage2-express  1 entry,    609 B  (target 1, max_attempts 8)
#
# Every attempt budget is the same derivation as before —
# ``target + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE`` — applied to
# each stage's own target rather than to the sum of both.
#
# What moved BESIDES the entry count, deliberately, and nothing else did:
#   * every ``progress`` counter, because "Measurement N of T" is per-session
#     and T is now 10 (or 6, or 6, or 1) rather than 16;
#   * the done copy moved off stage 1 entirely and onto stage 2's last entry —
#     the ``M = 1`` placement rule moved WITH the post-apply group it is about;
#   * stage 2's anchor carries a truthful pre-arm instruction plus §2.2's
#     unchanged ``confirm_title``/``confirm_body``, where the single-session
#     VERIFY entry carried the apply-hold copy and ``auto_advance: on_apply``.
#     There is no apply inside either session to hold for.
# Program DURATIONS did not move: the same three composed programs at the same
# lengths, redistributed across two plans.
#
# **The 1-entry recovery re-verify is byte-for-byte unchanged — 324 B and the
# same digest it has carried since 2026-07-28.** That is the load-bearing check
# in this revision: work order D2 keeps that form exactly as it is, and an
# identical digest is the proof that generalizing its builder over a plan shape
# left its own output untouched.
# RE-DERIVED 2026-07-30 (issue #1806, PR-T4 — the phone's honesty layer). COPY
# ONLY: every target, attempt budget, entry count, screen key, and program
# duration is byte-identical to the PR-T3 revision above. What moved is the
# prompted-position copy and CHECK's, per work order D7/D8:
#
#   * the position prompts became numeric ABSOLUTE poses in inches AND
#     centimetres, generated from each row's own ``offset_cm`` ("Move the
#     microphone 16 in (40 cm) to the LEFT of the mark, at mark height.") —
#     longer sentences than "A forearm's length LEFT of the mark", which is
#     where nearly all of the added bytes are;
#   * CHECK's entry stopped asking for quiet before the window that
#     deliberately measures an un-hushed room and gained a ``noise_note`` for
#     the phone's own pre-arm floor window (#1835);
#   * the actor became "the microphone" rather than "the phone".
#
#   stage1-full     2301 B → 2918 B  (+617; 8 prompted positions + CHECK)
#   stage1-express  1531 B → 1945 B  (+414; 4 prompted positions + CHECK)
#   stage2-full     1612 B → 1939 B  (+327; 5 prompted positions, no CHECK)
#   stage2-express   609 B →  609 B  (UNCHANGED — one anchor, no prompted move)
#   1-entry          324 B →  324 B  (UNCHANGED)
#
# The two unchanged digests are the load-bearing check in this revision: both
# are plans with no prompted position and no CHECK entry, so an identical
# digest is the proof that a prompt-copy rewrite reached exactly the entries it
# was about. Byte lengths grew and are pinned alongside the hashes because this
# copy sits inside the relay's 4 KiB per-screen cap — see
# ``test_cloud_plan_stays_inside_the_relay_spec_byte_budgets`` for the margin.
#
# RE-DERIVED 2026-07-30 (issue #1941 stage 2, R4 — the vocabulary sweep). COPY
# ONLY, and a single string of it: the 1-entry recovery re-verify's ``body``
# became "Put the microphone back on the mark and hold it still." The PR-T4
# revision above finished the sweep in the PROMPTED-position copy; this one
# reaches the last screen in this file that still called the instrument a phone.
#
#   stage1-full     2918 B → 2918 B  (UNCHANGED)
#   stage1-express  1945 B → 1945 B  (UNCHANGED)
#   stage2-full     1939 B → 1939 B  (UNCHANGED)
#   stage2-express   609 B →  609 B  (UNCHANGED)
#   1-entry          324 B →  329 B  (+5 — exactly len("microphone") - len("phone"))
#
# FOUR unchanged digests are the load-bearing check in this revision, and the
# +5 is the whole diff: a noun swap that reached one screen and nothing else.
# No target, attempt budget, entry count, screen key, or program duration moved.
_GOLDEN_V2_PLAN_BYTES = {
    "stage1-full": (
        2918,
        "b2c34282a518658d908acda6de53e69e777938c9480c415ed4e4649832e65949",
    ),
    "stage1-express": (
        1945,
        "259c69948dc954b28a408335419336f312f9045aa9307820999f19db6a2b4ff7",
    ),
    # Moved by #1964: Full's done_body no longer pre-commits "Verified and
    # applied." before the first tone plays.
    "stage2-full": (
        1942,
        "575ae0cb9e0a43a9f24492c43bc1e6192740164f96d9e9b7eb639d1bba629446",
    ),
    # Moved by #1964's fix round: Express's upgrade-path phrase drops the
    # withdrawn "verified-everywhere" overclaim for the B2 wording jts.local
    # already ships.
    "stage2-express": (
        630,
        "a5f499d6c1219460a377ee4cd083a45fc86aa93dff3d446bc2c1c4c58955f07b",
    ),
    "1-entry": (
        329,
        "5289e8602bfe37469abd91cc12dff53387b512c358a616dd7e2df20d79b0fccb",
    ),
}


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


def test_cloud_plan_stays_inside_the_relay_spec_byte_budgets():
    """The relay caps the opaque spec at 64 KiB and each entry's screen at
    4 KiB (`capture_relay.spec`). A 16-entry plan of product copy is nowhere
    near either, but the margin is what makes prompt edits safe, so measure it
    rather than assume it."""
    import json
    import re
    from pathlib import Path

    from jasper.capture_relay.spec import MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES

    # The 64 KiB spec cap lives in the deployed Worker, not in Python — read it
    # from the source of truth rather than restating it here.
    worker = Path(__file__).resolve().parents[1] / "relay" / "src" / "worker.js"
    match = re.search(
        r"const MAX_SPEC_BYTES = (\d+) \* 1024;", worker.read_text(encoding="utf-8")
    )
    assert match is not None, "relay worker no longer declares MAX_SPEC_BYTES"
    max_spec_bytes = int(match.group(1)) * 1024

    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding="b" * 24)
    raw = json.dumps(spec.to_dict(), separators=(",", ":")).encode("utf-8")
    assert len(raw) < max_spec_bytes // 4
    for entry in spec.capture_plan.entries:
        encoded = json.dumps(entry.screen, separators=(",", ":")).encode("utf-8")
        assert len(encoded) < MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES // 4


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


def _profiled_conductor(*, woofer_peak: float, tweeter_peak: float):
    """A conductor whose caps come from a REAL confirmed safety profile, plus the
    (topology, profile, targets, session_volume) that admission needs."""
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )

    from tests.test_active_speaker_program_admission import _profile_and_targets

    topology, profile, targets = _profile_and_targets(
        woofer_peak=woofer_peak, tweeter_peak=tweeter_peak
    )
    sv = session_measurement_volume_db(profile, targets.values())
    caps = {"woofer": float(woofer_peak), "tweeter": float(tweeter_peak)}
    # Bands within the profile's permitted [500, 20000] excitation band.
    roles = [
        RoleBand("woofer", 0, FrequencyBand(500.0, 1600.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 10000.0)),
    ]
    c = CrossoverV2Conductor(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=roles,
        fc_hz=FC_HZ,
        driver_caps_dbfs=caps,
        session_volume_db=sv,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
    )
    return c, topology, profile, targets, sv


@pytest.mark.parametrize(
    "woofer_peak,tweeter_peak",
    # The JTS3-shaped 0/-8/-65 cap numbers across the two profile-valid combos
    # (a tweeter capped above code policy, e.g. -8, cannot be confirmed).
    [(0.0, -65.0), (-8.0, -65.0)],
)
def test_composed_programs_admit_at_shaped_caps(woofer_peak, tweeter_peak):
    """CHECK and MEASURE admit at the JTS3-shaped caps; VERIFY (no admission
    path — it rides the applied graph) is clamped to the most restrictive cap.

    This is the pin that was missing (the conductor knew the caps but the fake
    play seam never admitted). ``admit_excitation_program`` REFUSES VERIFY by
    design (test_active_speaker_program_admission.test_verify_program_not_admitted_here
    pins that — VERIFY is mono/summed with no per-driver target), so VERIFY's
    equivalent safety proof is its compose-time clamp: no segment can exceed the
    binding cap that its summed signal reaches every driver at.
    """
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionError,
        admit_excitation_program,
    )

    c, topology, profile, targets, sv = _profiled_conductor(
        woofer_peak=woofer_peak, tweeter_peak=tweeter_peak
    )

    def _admit(program):
        return admit_excitation_program(
            program, topology=topology, safety_profile=profile,
            role_targets=targets, session_volume_db=sv,
        )

    adm_check = _admit(c._check_program)
    assert adm_check.allowed, adm_check.refusals

    _run_phase(c, 1, 1)  # CHECK solve → MEASURE composed
    adm_measure = _admit(c._program_for_phase(PHASE_MEASURE))
    assert adm_measure.allowed, adm_measure.refusals

    # VERIFY has no admission path by design; its clamp is the only guard.
    with pytest.raises(ProgramAdmissionError):
        _admit(c._verify_program)
    binding_cap = min(woofer_peak, tweeter_peak)
    for seg in c._verify_program.stimulus_segments():
        assert seg.effective_peak_dbfs <= binding_cap + 1e-9


def test_check_pilot_pairs_preserve_delta_and_degrade_honestly():
    """CHECK pilots keep the 10 dB behavioral delta where headroom allows, and
    degrade honestly (recorded in the program) where a driver cap compresses the
    level — the JTS3 tweeter drops ~33 dB but its pair stays 10 dB apart."""
    c, _topology, _profile, _targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    check = c._check_program

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
    verify = c._verify_program
    v_hi = verify.segment("pilot_summed_hi")
    v_lo = verify.segment("pilot_summed_lo")
    assert v_hi.gain_db - v_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)
    assert v_hi.effective_peak_dbfs <= -65.0 + 1e-9
    # And the summed sweep itself is clamped to the same binding cap.
    assert verify.segment("sweep_verify").effective_peak_dbfs <= -65.0 + 1e-9


def test_uncapped_check_program_would_be_refused_regression():
    """The pre-W6.1 shape: a CHECK program composed at the shared reference base
    (ignoring caps) is refused by admission on the JTS3 tweeter — the exact
    program_channel_peak_over_cap refusal hardware run 2 hit."""
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionRefusal,
        admit_excitation_program,
    )
    from jasper.audio_measurement.program import build_check_program

    c, topology, profile, targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    uncapped = build_check_program(c._roles, downstream_gain_db=sv)  # no role bases
    adm = admit_excitation_program(
        uncapped, topology=topology, safety_profile=profile,
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
    write_program_wav(wav, c._verify_program)
    rate, data = wavfile.read(str(wav))
    assert rate == c._verify_program.sample_rate_hz
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


def test_jts3_derived_hf_ceiling_drives_production_conductor_composition():
    from jasper.active_speaker.excitation_safety_plan import (
        resolve_driver_excitation_ceilings,
    )
    from jasper.active_speaker.program_admission import admit_excitation_program
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )

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
    # Probe (a): context caps == admission caps == the derived {-8, -35}.
    assert caps == {"woofer": -8.0, "tweeter": pytest.approx(-35.0)}
    sv = session_measurement_volume_db(
        profile, targets.values(), declared_sensitivities=declared
    )
    assert sv == -20.0  # max(caps) is still the woofer's — volume unchanged

    roles = [
        RoleBand("woofer", 0, FrequencyBand(500.0, 1600.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 10000.0)),
    ]
    c = CrossoverV2Conductor(
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
    # (back_off margin under -35), not the legacy -65.01 the gate measured.
    t_hi = c._check_program.segment("pilot_tweeter_hi")
    assert t_hi.effective_peak_dbfs == pytest.approx(-35.0 - GAIN_CAP_BACKOFF_DB)
    # And the play-time gate (same declared mapping, as bind_production_play
    # now threads it) admits what the conductor composed.
    adm = admit_excitation_program(
        c._check_program, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
        declared_sensitivities=declared,
    )
    assert adm.allowed, adm.refusals
    facts = {f.role: f for f in adm.channels}
    assert facts["tweeter"].cap_dbfs == pytest.approx(-35.0)
    # Without the declared mapping (the pre-fix admission view) the SAME
    # composed program is refused — the incoherence the threading closes.
    stale = admit_excitation_program(
        c._check_program, topology=topology, safety_profile=profile,
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

_DIAG_LOGGER = "jasper.active_speaker.crossover_v2_flow"


def _pilot_obs(
    role: str, *,
    snr_db: float = 20.0,
    captured_delta_db: float = 10.0,
    programmed_delta_db: float = 10.0,
    target_rise_db: float | None = 18.0,
    cross_rise_db: float | None = 1.0,
    snr_valid: bool = True,
    linearity_ok: bool = True,
    channel_map_ok: bool = True,
) -> PilotObservation:
    return PilotObservation(
        role=role, level_lo_dbfs=-40.0, level_hi_dbfs=-30.0,
        programmed_delta_db=programmed_delta_db, captured_delta_db=captured_delta_db,
        linearity_ok=linearity_ok, channel_map_ok=channel_map_ok, snr_valid=snr_valid,
        snr_db=snr_db,
        channel_map_target_rise_db=target_rise_db,
        channel_map_cross_rise_db=cross_rise_db,
    )


def _driver_response_diag(
    role: str, *, window_ms: float = 8.0, floor_hz: float | None = None,
    snr_db: float | None = None, snr_verdict: str | None = None,
    floor_source: str = gating.FLOOR_MEASURED,
) -> DriverResponse:
    freqs = np.linspace(100.0, 20000.0, 64)
    snr = (
        {"worst_relevant": {"estimated_snr_db": snr_db, "verdict": snr_verdict}}
        if snr_db is not None else None
    )
    return DriverResponse(
        role=role, freqs_hz=freqs, magnitude_db=np.zeros(64),
        complex_tf=np.ones(64, dtype=complex),
        # ``floor_source`` defaults to the "gate found a reflection" state and
        # is overridden per test — the two states print the same
        # ``window_ms`` and mean opposite things (#1966).
        gating={
            "applied": True, "window_ms": window_ms, "floor_source": floor_source,
        },
        snr=snr, validity_floor_hz=floor_hz,
    )


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


def _check_analysis_with_solves(program, *, snr_floor_ok=True, pilot_snr_ok=True):
    """A CHECK analysis whose gain plan carries #1825 per-role solves."""
    return ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        pilots=(_pilot_obs("woofer"), _pilot_obs("tweeter")),
        linearity_ok=True, channel_map_ok=True, pilot_snr_ok=pilot_snr_ok,
        gain_plan=GainPlan(
            gain_db={"woofer": -19.0, "tweeter": -31.0},
            predicted_peak_dbfs=-19.0, snr_floor_ok=snr_floor_ok,
            role_solves={
                "woofer": RoleGainSolve(
                    role="woofer", gain_db=-19.0, flat_target_gain_db=-11.0,
                    bound_by="room_snr", band_hz=(150.0, 2000.0),
                    ambient_dbfs=-60.0, required_snr_db=41.0,
                    required_capture_dbfs=-19.0,
                ),
                "tweeter": RoleGainSolve(
                    role="tweeter", gain_db=-31.0, flat_target_gain_db=-13.0,
                    bound_by="room_snr", band_hz=(1500.0, 20000.0),
                    ambient_dbfs=-72.0, required_snr_db=41.0,
                    required_capture_dbfs=-31.0,
                ),
            },
        ),
    )


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
    drop its quiet side by (`_pilot_gains` / `PILOT_LEVEL_DELTA_DB`). If the
    two ever diverged the floor would be mis-sized in silence, so pin them
    equal at the composers that produce them."""
    from jasper.active_speaker.crossover_v2_flow import PILOT_LEVEL_DELTA_DB

    fakes = FakeSeams()
    fakes.check = _check_analysis_with_solves
    c = _conductor(fakes)

    check = c._program_for_phase("check")
    for role in ("woofer", "tweeter"):
        lo = check.segment(f"pilot_{role}_lo")
        hi = check.segment(f"pilot_{role}_hi")
        assert hi.gain_db - lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)

    assert _run_phase(c, 1, 1)["accepted"] is True
    measure = c._program_for_phase("measure")
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
    measure = c._program_for_phase("measure")
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
            epsilon_ppm=30.0, baselines_ppm={"woofer_repeat": 30.0},
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
    evidence = _analysis_json(fakes.measure(c._measure_program))
    assert evidence["alignment_confidence_source"] == "gcc_phat_seed"
    assert evidence["alignment_seed_delay_us"] == 120.0
    assert evidence["alignment_seed_ripple_db"] == 4.56
    assert evidence["flatness_improvement_db"] == 3.33
    assert evidence["anchor_delay_us"] == 145.0
    assert evidence["snap_delta_us"] == 5.0
    assert evidence["snap_found"] is True


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
            epsilon_ppm=30.0, baselines_ppm={"woofer_repeat": 30.0},
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


def test_measure_diag_logs_full_numbers_on_low_alignment_confidence_rejection(caplog):
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    assert 0.55 < ALIGNMENT_CONFIDENCE_TRUST_FLOOR  # keep the fixture below the gate
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, alignment=_alignment(confidence=0.55),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is False
    assert verdict["code"] == "low_alignment_confidence"
    assert "event=correction.crossover_v2_measure_diag" in caplog.text
    assert "alignment_confidence=0.55" in caplog.text
    # ``analysis.candidate`` is populated by program_analysis's own
    # ``_build_candidate`` before this ever reaches the conductor (real
    # ``_analyze_measure`` always builds it) — so its ripple number is still
    # available for the diagnostic even though THIS rejection means the
    # conductor's own candidate is never built or published.
    assert "predicted_ripple_db=0.8" in caplog.text
    # The pre-existing confidence-floor check, not G1 — guard stays empty.
    assert 'guard=""' in caplog.text


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
    assert verdict["accepted"] is True
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
    # attempt is admitted).
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.1, max_db=5.0,
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


def _resp_with_repeats(role: str, n_repeats: int) -> DriverResponse:
    freqs = np.linspace(150.0, 20000.0, 256)
    mag = np.zeros_like(freqs)

    def make() -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=freqs, magnitude_db=mag,
            complex_tf=np.ones_like(freqs, dtype=complex),
            gating={}, snr=None, validity_floor_hz=140.0,
        )

    repeats = tuple(make() for _ in range(n_repeats))
    return DriverResponse(
        role=role, freqs_hz=freqs, magnitude_db=mag,
        complex_tf=np.ones_like(freqs, dtype=complex),
        gating={}, snr=None, validity_floor_hz=140.0,
        repeat_responses=repeats,
    )


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


def test_sigma_tolerable_db_matches_linearization_envelopes_own_table():
    """SF1 (adversarial review, 2026-07-24): lockstep requirement. This
    module's own comment on ``_SIGMA_TOLERABLE_DB`` explains why it is a
    local mirror rather than an import — production code deliberately does
    not cross that "no cross-module private imports" boundary
    (linearization_envelope's module docstring). Tests are allowed to reach
    across it anyway, specifically to pin the two tables in lockstep, so a
    future edit to one can never silently drift from the other."""
    from jasper.active_speaker import linearization_envelope

    assert _SIGMA_TOLERABLE_DB == linearization_envelope._SIGMA_TOLERABLE_DB


# --- conductor integration reorder ------------------------------------------


def _fixture_branch_db() -> tuple[np.ndarray, np.ndarray]:
    """The eligible fixture's two branch magnitude curves.

    Split out from ``_eligible_measure_analysis`` so ``_FIXTURE_RAW_TRIM_DB``
    below can be SOLVED from the same curves the fixture hands the conductor
    (see that constant for why a hand-written trim stopped being acceptable).

    **Each branch carries its own crossover (R10a, #1817).** These curves stand
    in for a per-driver MEASUREMENT, and a real one is captured through the
    graph the speaker is running — so the woofer's low-pass and the tweeter's
    high-pass belong in them. Verified on the banked 2026-07-30 bench session
    rather than assumed (``captures/r10a-objective-20260801/premise_probe.py``):
    against its own passband median the real JTS3 woofer measures **-3.22 dB at
    0.79*Fc, -7.32 dB at Fc and -32.62 dB at 2*Fc**, and the real tweeter
    **-38.81 dB at 0.79*Fc**, both tracking their committed LR4 sections (and
    falling faster still, since the driver's natural rolloff adds to the
    electrical one).

    Before R10a they did NOT, and it did not show: a flat target and a
    crossover-free curve are self-consistent, so the fixture modelled an
    impossible speaker without contradicting anything. The crossover-shaped
    target is what makes the omission observable — a fit graded against the
    shape of a crossover its own measurement never went through would be asked
    to CUT its passband edges into existence. Two tests in this file
    (``test_..._one_set_of_crossover_sections``'s neighbours at the
    ``crossover_response_db`` call sites below) had already adopted the
    faithful shape locally; this makes it the fixture's own.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    freqs = _LINEARIZABLE_FREQS_HZ
    # A mild, monotonic -1.5 dB/octave tilt around Fc (capped at +/-6 dB
    # at the band extremes) -- NOT a perfectly flat 0 dB reference.
    # #1667's ripple-optimal trim solve needs the woofer branch to carry
    # SOME of its own frequency-dependent shape: against a perfectly
    # flat woofer, attenuating the tweeter toward silence is always
    # "more flat" (there is nothing on the woofer side to trade off
    # against), so the search has no genuine interior minimum and walks
    # to its own scan-window edge -- which the sanity guard then
    # (correctly) distrusts and rejects. A mild tilt is enough for a
    # real interior optimum to exist while leaving the tweeter-bump
    # linearity checks below unaffected (the fit and its own filters are
    # sibling-independent; verified offline).
    woofer_db = np.clip(-1.5 * np.log2(np.maximum(freqs, 1.0) / 1600.0), -6.0, 6.0)
    # …plus a -6 dB dip at 400 Hz, INSIDE the woofer's own radiating band
    # (#1809). Without it this fixture cannot exercise the lift vocabulary at
    # all any more, and the reason is the defect #1809 filed: the tilt's only
    # deficit is its falling tail, which lives ABOVE the 1600 Hz crossover,
    # where the woofer has handed off. Every boost this fixture used to emit
    # was a stopband boost — a miniature of the 2026-07-28 JTS3 profile, where
    # +11.6155 dB at 2747 Hz sat 750 Hz into the woofer's own LR4 stopband. A
    # real in-band dip is what a driver the fit should lift actually looks
    # like, and it lands the fixture's own boost at +4.25 dB / 399 Hz, beside
    # that profile's real +4.8807 dB / 377.4 Hz. It moves the solved raw trim
    # by 0.002 dB, so nothing else in this file shifts under it.
    woofer_db = woofer_db - 6.0 * np.exp(
        -0.5 * ((np.log2(freqs / 400.0) / 0.3) ** 2)
    )
    # A bump inside the [800, 3200] Hz overlap band (Fc=1600) — validated
    # offline (PR-C sanity pass) to survive envelope/fit and move the
    # re-solved trim measurably vs the raw candidate.
    #
    # **+3 dB at 2400 Hz since R10a; it was +6 dB at 1500 Hz.** Both numbers
    # moved for the same reason — the fixture became faithful above — and both
    # were measured rather than guessed
    # (``captures/r10a-objective-20260801/fixture_sweep.txt``):
    #
    # * 1500 -> 2400 Hz, because the bump has to sit in the tweeter's OWN
    #   radiating band (LR4 high-passed at 1600 Hz, so 1997 Hz up) to be a
    #   driver defect at all. At 1500 Hz the branch's own crossover is ~7 dB
    #   down, so a faithful fixture attenuates the bump before it reaches the
    #   sum and the fit is asked to correct something the graph already
    #   removed. 2400 Hz keeps it inside the overlap band the trim scans.
    # * 6 -> 3 dB, because a bump that big in the tweeter's own passband
    #   drives the ripple-optimal trim to -3.9 dB, which puts the shared level
    #   frame 4.361 dB apart — past its frozen 3.0 dB tolerance, so the gate
    #   (correctly) refuses before any of this file's other assertions run.
    #   The sweep is monotonic and unambiguous: at 2400 Hz the disagreement is
    #   2.643 dB at +3, 3.275 at +4, 3.760 at +5, 4.361 at +6. +3 dB is a
    #   realistic driver defect that still moves the trim measurably
    #   (-2.267 dB) and still lands the fixture's boost at +3.72 dB / 399 Hz.
    tweeter_db = 3.0 * np.exp(-0.5 * ((np.log2(freqs / 2400.0) / 0.25) ** 2))
    # …each behind its own committed section, so the curve the fixture hands
    # the conductor is the one a measurement would have produced.
    woofer_db = woofer_db + crossover_response_db(
        freqs, (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=False),),
    )
    tweeter_db = tweeter_db + crossover_response_db(
        freqs, (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=True),),
    )
    return woofer_db, tweeter_db


def _solve_fixture_raw_trim(
    woofer_db: np.ndarray | None = None, tweeter_db: np.ndarray | None = None,
) -> dict[str, float]:
    """The raw trim a pair of branch curves actually call for.

    **Why this is solved and not written down** (PR-L4). The fixture carried a
    hand-written ``{"woofer": 0.0, "tweeter": -2.211}`` chosen to exercise the
    ripple scan, but its two branches are near-equal-sensitivity synthetics
    that call for about -0.7 dB. Nothing noticed, because until PR-L4 nothing
    in the chain ever compared the two branches' realized levels — which is
    precisely the hole PR-L4 item 1 exists to close, reproduced inside the test
    fixture that was meant to model the thing. A production MEASURE analysis
    derives ``candidate.trim_db`` from its own branches via
    ``solve_branch_trims``, so this fixture now does the same and cannot drift
    from its own physics again.

    **Why it takes optional curves instead of only reading the default pair**
    (#1938). ``_eligible_measure_analysis`` reuses this exact solve for
    whichever ``woofer_db``/``tweeter_db`` a call is actually using, default or
    custom. Defaulting a custom-curve call's trim to a constant solved from the
    DEFAULT curves (below) hands the conductor one speaker's branches and
    another speaker's trim — the identical incoherence this function was
    written to remove for the default curves in PR-L4, reintroduced through
    the custom-curve parameters (#1938). Arguments default to
    :func:`_fixture_branch_db`'s pair, so ``_FIXTURE_RAW_TRIM_DB`` below is
    unaffected by this generalization.

    **Why the woofer trim is returned too, not hardcoded to 0.0** (#1938
    gate follow-up). Before this generalization, the woofer trim was always
    ``0.0`` by construction: the one fixed pair this function ever solved had
    a quieter woofer, so ``solve_branch_trims``'s "attenuate the louder
    branch" rule always left the woofer untouched. That stopped being true
    the moment this function started solving ARBITRARY curve pairs — a pair
    whose woofer is louder in its own band needs a nonzero woofer trim, and
    hardcoding it to 0.0 silently drops that attenuation. Returning both
    solved values is the same fix this function exists for, applied to
    itself.
    """
    freqs = _LINEARIZABLE_FREQS_HZ
    if woofer_db is None or tweeter_db is None:
        default_woofer_db, default_tweeter_db = _fixture_branch_db()
        woofer_db = default_woofer_db if woofer_db is None else woofer_db
        tweeter_db = default_tweeter_db if tweeter_db is None else tweeter_db
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs,
        (10.0 ** (np.asarray(woofer_db) / 20.0)).astype(complex),
        (10.0 ** (np.asarray(tweeter_db) / 20.0)).astype(complex),
        _FIXTURE_FC_HZ,
    )
    return {"woofer": round(float(trim_w), 3), "tweeter": round(float(trim_t), 3)}


_FIXTURE_RAW_TRIM_DB = _solve_fixture_raw_trim()


def _fixture_raw_predicted_sum(
    *, woofer_db=None, tweeter_db=None, trim_db=None,
) -> tuple[np.ndarray, np.ndarray]:
    """The RAW pre-fit two-branch sum of the eligible fixture's own branches.

    PR-L4 review B1: item 2's gate grades this against the LINEARIZED
    prediction through the same evaluator, so the baseline has to be the
    fixture's real uncorrected sum. It used to be a hardcoded flat zero curve,
    which claimed the uncorrected speaker was already perfect and made every
    correction score as a regression — the same "a fixture field nobody derived
    from the fixture" shape as the hand-written raw trim above.

    #1938 gate follow-up: an omitted ``trim_db`` is derived from THESE curves
    (default or custom), never from ``_FIXTURE_RAW_TRIM_DB`` — the same
    default-curves-constant trap ``_eligible_measure_analysis`` had, closed
    here too even though every current caller already passes matching
    curves/trim together (no caller today hits this branch with an
    incoherent pair — this is closing the trap-door, not fixing an observed
    incoherence).
    """
    if woofer_db is None or tweeter_db is None:
        default_woofer_db, default_tweeter_db = _fixture_branch_db()
        woofer_db = default_woofer_db if woofer_db is None else woofer_db
        tweeter_db = default_tweeter_db if tweeter_db is None else tweeter_db
    if trim_db is None:
        trim_db = _solve_fixture_raw_trim(woofer_db, tweeter_db)
    summed = predicted_branch_sum(
        (10.0 ** (np.asarray(woofer_db) / 20.0)).astype(complex),
        (10.0 ** (np.asarray(tweeter_db) / 20.0)).astype(complex),
        float(trim_db.get("woofer", 0.0)), float(trim_db.get("tweeter", 0.0)), 1,
    )
    return (
        _LINEARIZABLE_FREQS_HZ,
        20.0 * np.log10(np.maximum(np.abs(summed), 1e-12)),
    )


def _eligible_measure_analysis(
    program, *, mic_tier="reference", woofer_repeats=2, tweeter_repeats=2,
    woofer_db=None, tweeter_db=None, trim_db=None, trim_band_average_db=None,
) -> ProgramAnalysis:
    default_woofer_db, default_tweeter_db = _fixture_branch_db()
    if woofer_db is None:
        woofer_db = default_woofer_db
    if tweeter_db is None:
        tweeter_db = default_tweeter_db
    if trim_db is None:
        # #1938: derive the trim from THESE branches (default or custom) —
        # never default to a constant solved from a DIFFERENT pair. A caller
        # that hands the conductor custom woofer_db/tweeter_db but inherits
        # _FIXTURE_RAW_TRIM_DB (solved from the DEFAULT curves) hands it one
        # speaker's branches and another speaker's trim — the exact defect
        # _solve_fixture_raw_trim's docstring documents for the default curves,
        # closed here for every curve pair. A caller that deliberately wants an
        # incoherent pair passes trim_db= explicitly (the level gates' own
        # branch-forcers, e.g.
        # test_the_level_frame_refusal_names_the_levels_and_bands_it_read).
        trim_db = _solve_fixture_raw_trim(woofer_db, tweeter_db)
    if trim_band_average_db is None:
        # Production always sets it (`_build_candidate`), and it COINCIDES with
        # the applied trim whenever the ripple polish did not move it. Splitting
        # the two is what a test does to exercise PR-L5's level-frame gate,
        # which reads the level-match result rather than the polished trim.
        trim_band_average_db = dict(trim_db)
    return ProgramAnalysis(
        phase="measure",
        program_id=program.program_id,
        locations=(
            _loc("sweep_w"), _loc("sweep_t"), _loc("sweep_w_rep"), _loc("sweep_t_rep"),
        ),
        drift=DriftEstimate(
            epsilon_ppm=5.0, baselines_ppm={"woofer_repeat": 5.0},
            max_residual_samples=0.1, glitch_detected=False,
        ),
        mic_tier=mic_tier,
        driver_responses=(
            _linearizable_response("woofer", woofer_db, n_repeats=woofer_repeats),
            _linearizable_response("tweeter", tweeter_db, n_repeats=tweeter_repeats),
        ),
        alignment=_alignment(),
        candidate=CrossoverCandidate(
            trim_db=trim_db, polarity="normal", delay_us=150.0,
            predicted_ripple_db=0.8, confidence=0.8,
            trim_band_average_db=trim_band_average_db,
        ),
        linearity_ok=True,
        # The RAW pre-fit two-branch sum of THIS fixture's own branches at THIS
        # fixture's own trim — not a flat zero curve (PR-L4 review B1). Item 2's
        # gate grades this against the LINEARIZED prediction through the same
        # evaluator, so a hardcoded-flat baseline claimed the uncorrected
        # speaker was already perfect and made every correction look like a
        # regression. Same incoherence class as the hand-written trim above:
        # a fixture field that was never derived from the fixture.
        predicted_sum=_fixture_raw_predicted_sum(
            woofer_db=woofer_db, tweeter_db=tweeter_db, trim_db=trim_db,
        ),
        glitch_detected=False,
    )


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


def test_fit_linearization_wires_ripple_optimal_seeded_by_anchored_giveback(
    monkeypatch,
):
    """#1668 anchored give-back: `_fit_linearization`'s ripple fine-tune must be
    seeded by the ANCHORED trim — each branch's own raw candidate trim plus the
    level its emitted cascade removed from its reference band
    (`LinearizationFit.correction_giveback_db`), normalized non-positive — NOT
    the old `solve_branch_trims` overlap-band average on the linearized pair
    (which under-returned the give-back on the live JTS3 runs). Spies on the
    module-level imported name to pin that the call happened exactly once, with
    the anchored woofer trim held fixed and the analysis's own polarity sign
    passed through."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    calls = []
    real_solve = flow_mod.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        # Positional call shape: solve_ripple_optimal_trim(freqs, w_tf,
        # t_tf, fc_hz, *, lo_hz=..., hi_hz=..., seed_trim_db=...,
        # trim_w_db=..., sign=...) -- _fit_linearization passes the first
        # four positionally, the rest by keyword.
        freqs, w_tf, t_tf, fc_hz = args
        calls.append({"freqs": freqs, "w_tf": w_tf, "t_tf": t_tf, "fc_hz": fc_hz, **kwargs})
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(flow_mod, "solve_ripple_optimal_trim", _spy)

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
    # Anchored seed = raw trim + that branch's own measured give-back, with the
    # shared non-positive normalization shift applied to both roles.
    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    giveback = {
        role: c.candidate.linearization[role]["correction_giveback_db"]
        for role in ("woofer", "tweeter")
    }
    # PR-L5 adds the shared-level-frame offset to the same anchor: the
    # give-back returns a branch to its OWN pre-correction system level, and
    # the offset then places that level where the session's one frame says it
    # belongs. Read off the fit rather than recomputed, for the same reason
    # ``giveback`` is.
    frame_offset = {
        role: c.candidate.linearization[role]["level_frame_offset_db"]
        for role in ("woofer", "tweeter")
    }
    unnormalized = {
        r: raw_trim[r] + giveback[r] + frame_offset[r] for r in ("woofer", "tweeter")
    }
    shift = max(0.0, max(unnormalized.values()))
    expected_anchored = {r: v - shift for r, v in unnormalized.items()}
    assert call["trim_w_db"] == pytest.approx(expected_anchored["woofer"])
    assert call["seed_trim_db"] == pytest.approx(expected_anchored["tweeter"])

    # What ships is one of the TWO pairs `_fit_linearization` grades — the
    # anchor, or the scan's ripple polish — never the raw trim ("Never the RAW
    # trim, whichever pair wins"). WHICH of the two wins is the PR-L4 level
    # adjudication's business, not this test's: it commits whichever pair the
    # realized inter-driver level instrument scores better, and both branches of
    # that choice have their own pins (test_eligible_candidate_fits_both_roles_
    # and_moves_trim_toward_ripple_optimal for the polish, test_a_disagreeing_
    # frame_whose_realized_check_passes_banks_and_proceeds for the grading).
    #
    # This fixture used to land on the polish and now lands on the anchor, for a
    # reason worth recording rather than papering over: R10b (panel CC-2(b))
    # made the
    # fit's `correction_giveback_db` grade the REALIZED biquad cascade instead
    # of `predicted_response`'s Lorentzian, which moved this pair's anchor by
    # +0.124 dB (tweeter -1.383 -> -1.260). BOTH graded pairs moved with it (the
    # polish is seeded from the anchor), in opposite directions: the anchor's
    # realized level error |-0.258| -> |-0.134| dB, the polish's |0.142| ->
    # |0.166| dB. That is what crossed them over. No filter moved.
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
    assert committed_t != pytest.approx(raw_trim["tweeter"])
    # …and the fixture-specific outcome, stated precisely rather than hedged, so
    # a future flip back is visible here rather than silent. The scan did move
    # (0.300 dB off its seed) — it simply did not level better.
    assert committed_t == pytest.approx(expected_anchored["tweeter"])
    assert resolved_trim_t != pytest.approx(expected_anchored["tweeter"])


def _one_sided_conductor(fakes: FakeSeams) -> CrossoverV2Conductor:
    """A conductor whose TWEETER sweep starts AT Fc — JTS3's real geometry.

    ``overlap_band_hz`` then clamps the shared band to ``[Fc, 2*Fc]``, the
    one-sided shape PR-L3 is about. Built inline rather than through
    ``_conductor`` because the role bands are the whole point of the fixture.
    """
    return CrossoverV2Conductor(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=[
            RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
            RoleBand("tweeter", 1, FrequencyBand(FC_HZ, 20000.0)),
        ],
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
    )


def test_linearized_ripple_polish_is_skipped_on_a_one_sided_band(caplog, monkeypatch):
    """PR-L3 review S1: the LINEARIZED ripple fine-tune carries the same
    one-sided-band hazard `program_analysis._build_candidate` guards, reached
    through the same ``overlap_band_hz`` clamp — and THIS is the call site
    whose result becomes ``role_attenuations_db``, the gain the emitted graph
    runs. With the tweeter swept from Fc the band is ``[Fc, 2*Fc]``, where the
    woofer is deep in its skirt and the summed ripple cannot express the
    handoff level. The scan must not run at all; the anchored give-back
    stands, and the skip is disclosed."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    calls = []
    monkeypatch.setattr(
        flow_mod, "solve_ripple_optimal_trim",
        lambda *a, **kw: calls.append(kw) or (kw["seed_trim_db"] - 4.0, 0.0, kw["seed_trim_db"]),
    )
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
    giveback = {
        role: c.candidate.linearization[role]["correction_giveback_db"]
        for role in ("woofer", "tweeter")
    }
    # PR-L5 adds the shared-level-frame offset to the same anchor: the
    # give-back returns a branch to its OWN pre-correction system level, and
    # the offset then places that level where the session's one frame says it
    # belongs. Read off the fit rather than recomputed, for the same reason
    # ``giveback`` is.
    frame_offset = {
        role: c.candidate.linearization[role]["level_frame_offset_db"]
        for role in ("woofer", "tweeter")
    }
    unnormalized = {
        r: raw_trim[r] + giveback[r] + frame_offset[r] for r in ("woofer", "tweeter")
    }
    shift = max(0.0, max(unnormalized.values()))
    for role in ("woofer", "tweeter"):
        assert c.candidate.role_attenuations_db[role] == pytest.approx(
            unnormalized[role] - shift
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


def test_linearization_giveback_ledger_carries_both_level_frames(caplog):
    """PR-L3 review S5: the give-back line is where the TRIM frame and the FIT
    frame meet for one capture, so it carries both — ``raw_trim_db`` should
    track the negated difference of the two ``target_level_db`` values, and a
    large disagreement is the signature of the level-frame defect that shipped
    the 10 dB-dark tweeter. Mirrors the ``branch_level_match`` ledger pinned in
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
            epsilon_ppm=1.0, baselines_ppm={}, max_residual_samples=0.0,
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


def test_large_raw_shift_is_accepted_by_the_guard_and_refused_by_the_level_check(
    caplog,
):
    """The two layers, on one fixture — guard pair (a) plus PR-L4 item 1.

    #1668 CD-horn re-anchor: the wild-trim guard is anchored to the
    ripple-optimal tweeter trim's OWN seed, NOT the raw candidate trim, so a
    large shift vs the raw trim (what a legitimate CD-horn give-back produces)
    does not trip it. That is still true and still asserted here.

    What PR-L4 added is the half the guard never had: a raw trim 20 dB away
    from what these branches justify is *invisible to drift from the anchor* —
    the anchor is the thing that is wrong — and the level layer sees it and
    refuses rather than shipping a 20 dB-mislevelled speaker. This is the
    2026-07-27 failure shape in miniature.

    PR-L5 moved WHICH level instrument catches it, one stage earlier and to a
    strictly more specific diagnosis. The shared level frame reconciles the
    trim solve's estimate against the fit's before either reaches a trim, so a
    20 dB gap between them is refused as a frame disagreement rather than
    surviving into the committed pair for the realized-level assertion to find.
    Same code, same copy, same untouched speaker — the assertion is still
    there, now as the backstop it should have been from the start. The event
    name is what distinguishes them in the journal, which is why this test
    asserts on it.

    **The realized verdict is supplied, since the #1866 ruling.** A frame
    disagreement no longer refuses on its own: when the realized-level check
    passes on the pair about to ship, the session banks a finding and
    proceeds. That is the correct answer for THIS fixture's own physics —
    the −20 dB is a raw-trim INPUT the fit's anchor then repairs, so the
    speaker that would ship is level — and it is not what this test is about.
    What this test is about is the guard, so the realized instrument is held
    at "still mislevelled" to keep the refusal arm reachable.
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
            flow.CrossoverV2Conductor, "_realized_level_match", _still_mislevelled
        )
        _run_phase(c, 1, 1)
        with pytest.raises(CaptureBeginRefused) as excinfo:
            _run_phase(c, 2, 2)
    assert excinfo.value.code == REASON_DRIVER_LEVELS_DISAGREE
    assert LINEARIZATION_TRIM_SANITY_MARGIN_DB > 0  # the constant exists and is positive
    # The GUARD did not fire — a near-seed scan is trusted, exactly as #1668
    # intended. The refusal above came from the level layer, one stage later.
    assert "event=correction.crossover_v2_linearization_trim_rejected" not in caplog.text
    assert "event=correction.crossover_v2_level_frame_refused" in caplog.text
    assert "tolerance_db=3.0" in caplog.text
    # Nothing was published or stashed: the speaker is untouched.
    assert c.candidate is None
    assert fakes.published_candidates == []


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
    from jasper.active_speaker import crossover_v2_flow as flow_mod
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)

    captured: dict = {}

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        # Force the resolved tweeter trim 20 dB below the anchored seed.
        return kwargs["seed_trim_db"] - 20.0, 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(flow_mod, "solve_ripple_optimal_trim", _spy)

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


def test_wild_trim_fallback_follows_levels_not_drift(caplog, monkeypatch):
    """PR-L4 item 9's teeth: when the guard fires, the pair that LEVELS better
    is committed — even when that is the scan the guard just called wild.

    The 2026-07-27 evidence for why drift alone is the wrong verdict: the scan
    had walked 5.500 dB (missing the 6.0 dB guard by half a dB) and its walk was
    TOWARD a correct level, while the anchor it would have fallen back to was
    5.5 dB darker. Had the drift been a hair larger, the guard would have made
    the speaker worse. Here the forced scan is both wild AND better-levelled, so
    the guard fires and commits it anyway.

    **Why the level verdicts are supplied rather than provoked (PR-L5).** This
    test used to drive the anchor mislevelled with a 12 dB-dark raw trim. That
    lever is gone, and gone on purpose: the shared level frame makes the anchor
    ``give-back + system_level − core_level``, in which the raw trim cancels
    out of every branch's level RELATIVE to the others — a dark raw trim can no
    longer mislevel the anchored pair, and one 12 dB off is refused as a frame
    disagreement long before this branch. That is the ladder working. What
    remains worth pinning is the guard's DECISION — that it commits on levels
    and not on drift — so the two level verdicts are supplied directly and the
    physical scenario that used to produce them is left retired.
    """
    from jasper.active_speaker import crossover_v2_flow as flow_mod
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

    def _match(_self, _freqs, _w, _t, trims_db, _woofer_role, tweeter_role, **_kw):
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

    monkeypatch.setattr(flow_mod, "solve_ripple_optimal_trim", _scan)
    monkeypatch.setattr(
        flow_mod.CrossoverV2Conductor, "_realized_level_match", _match,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    # Item 2 refuses this session downstream (see the note below). The guard
    # runs — and logs its decision — inside ``_fit_linearization``, well before
    # ``_publish_measure_candidate`` grades the prediction, so every assertion
    # this test makes is already in the journal when the refusal arrives.
    with pytest.raises(CaptureBeginRefused) as excinfo:
        _run_phase(c, 2, 2)
    assert excinfo.value.code == REASON_CORRECTION_NOT_AN_IMPROVEMENT

    # The guard FIRED (drift 7 dB > the 6 dB margin) and still committed the
    # SCAN, because the scan levels better than the anchor it would have fallen
    # back to. Pre-PR-L4 this fell back to the darker pair unconditionally.
    assert "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    assert "committed=resolved" in caplog.text
    assert "anchored_level_error_db=2.5" in caplog.text
    assert "resolved_level_error_db=0.2" in caplog.text
    # **Why item 2 refuses again (R10a, #1817), and why that is the gate
    # getting its teeth back rather than this test's subject moving.** This
    # fixture refused here before #1809, stopped refusing after it, and refuses
    # again now — but the third state is not a return to the first. Measured on
    # the faithful fixture by sweeping the forced drift and reading
    # ``event=correction.crossover_v2_prediction_gate`` (baseline 0.957 dB rms
    # in every row; the floor is 0.5 dB):
    #
    #   drift dB     0      1      2      3       4       5       6      7      8
    #   improve  +0.657 +0.657 +0.657 +0.657  -0.324  -0.688  -1.087 -1.524 -1.998
    #   verdict   accept accept accept accept  refuse  refuse  refuse refuse refuse
    #
    # So the gate now DISCRIMINATES on this fixture: a correct trim ships, and a
    # mistrim of 4 dB or more is caught as the regression it is. Under the flat
    # target it refused at every drift including 0.0 (-0.293 dB), because the
    # fit's own crossover-fighting cuts made even an untouched trim fail to beat
    # its baseline — the gate could not tell a wild trim from a good one.
    #
    # There is no drift that both fires the guard and clears the floor: the
    # guard needs > 6.0 dB and the floor is lost above 3 dB. That is physics,
    # not a fixture limit — a pair mistrimmed past the guard's own margin does
    # not measure better. The guard's DECISION — commit on levels, not on drift
    # — is what this test pins, and it is unchanged.


def test_anchored_trim_is_raw_plus_giveback_and_normalized_non_positive():
    """#1668 anchored give-back, the core math: each role's committed trim is
    its raw trim plus that branch's own measured `correction_giveback_db`, with
    a shared shift applied so no role lands POSITIVE (a boost the emitter would
    refuse). Pinned end-to-end against the conductor's committed trims."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    giveback = {
        role: c.candidate.linearization[role]["correction_giveback_db"]
        for role in ("woofer", "tweeter")
    }
    # Every branch that emitted filters reports a positive give-back.
    assert giveback["tweeter"] > 0.0
    # PR-L5's shared-level-frame offset rides the same anchor (see the
    # sibling tests). Read off the fit, never recomputed.
    frame_offset = {
        role: c.candidate.linearization[role]["level_frame_offset_db"]
        for role in ("woofer", "tweeter")
    }
    unnormalized = {
        r: raw_trim[r] + giveback[r] + frame_offset[r] for r in ("woofer", "tweeter")
    }
    shift = max(0.0, max(unnormalized.values()))
    anchored = {r: v - shift for r, v in unnormalized.items()}

    committed = c.candidate.role_attenuations_db
    # No committed trim is a boost.
    assert all(v <= 1e-9 for v in committed.values())
    # The woofer is committed at its anchor exactly (only the tweeter is scanned).
    assert committed["woofer"] == pytest.approx(anchored["woofer"])
    # The tweeter sits at/near its anchor (the scan only fine-tunes around it).
    assert abs(committed["tweeter"] - anchored["tweeter"]) <= (
        LINEARIZATION_TRIM_SANITY_MARGIN_DB
    )
    # And the give-back genuinely moved it up from the raw trim toward level
    # preservation -- the whole point of the anchor.
    assert committed["tweeter"] > raw_trim["tweeter"] - 1e-9


def test_anchored_normalization_shift_prevents_a_positive_trim(monkeypatch):
    """The normalize step: when a branch's own give-back exceeds its raw
    attenuation the unnormalized anchor would be POSITIVE; the shared shift must
    pull every role non-positive while preserving their RELATIVE leveling.

    **The raw-trim override this test used to carry is gone (R10a, #1817), and
    re-deriving it is what showed it had never done anything.** It forced
    ``{"woofer": 0.0, "tweeter": 0.0}`` on the reasoning that "any positive
    give-back pushes the unnormalized anchor above 0 and forces the shift" —
    but PR-L5's frame offset is ``system − trim − core``, so the raw trim
    CANCELS out of ``raw + giveback + level_frame_offset``. Measured across a
    raw-tweeter-trim sweep on this fixture, the anchor and everything
    downstream of it are byte-identical at every value:
    ``unnormalized = {woofer: 3.4743, tweeter: 2.0908}``, ``shift = 3.4743``,
    ``committed = {woofer: 0.0, tweeter: -1.3835}`` at −0.0, −0.5, −1.0, −1.5,
    −1.773 (the fixture's own solved trim), −2.0 and −3.0 dB. The shift fires
    on the fixture's own trim, and always did.

    What the override DID do was starve a gate this test is not about. It moves
    the RAW predicted sum, which is item 2's baseline, so a zeroed trim leaves
    less than the 0.5 dB of headroom the improvement floor needs. Same sweep,
    reading ``event=correction.crossover_v2_prediction_gate`` (``after`` is
    0.300 dB rms in every row):

        raw tweeter trim dB   0.0    -0.5   -1.0   -1.5  -1.773   -2.0   -3.0
        baseline rms dB     0.647   0.708  0.792  0.895   0.957  1.012  1.284
        improvement dB      0.347   0.408  0.492  0.595   0.657  0.712  0.984
        verdict            refuse  refuse refuse accept  accept accept accept

    So the honest value for a fixture field nobody had derived is: don't
    override it. Using ``_FIXTURE_RAW_TRIM_DB`` — solved from the same branch
    curves the conductor is handed — keeps this test's subject bit-for-bit and
    stops it riding a floor it has nothing to say about.
    """
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    def _spy(*args, **kwargs):
        # Commit the anchor itself (no scan drift) so the committed pair is the
        # normalized anchor verbatim.
        return kwargs["seed_trim_db"], 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(flow_mod, "solve_ripple_optimal_trim", _spy)

    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    giveback = {
        role: c.candidate.linearization[role]["correction_giveback_db"]
        for role in ("woofer", "tweeter")
    }
    frame_offset = {
        role: c.candidate.linearization[role]["level_frame_offset_db"]
        for role in ("woofer", "tweeter")
    }
    unnormalized = {
        r: raw_trim[r] + giveback[r] + frame_offset[r] for r in ("woofer", "tweeter")
    }
    # The premise this test is built on: the woofer's own give-back (3.4743 dB)
    # exceeds its raw attenuation (0.0 dB), so its unnormalized anchor is a
    # BOOST the emitter would refuse.
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
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    def _run_at(drift_db: float):
        caplog.clear()
        monkeypatch.setattr(
            flow_mod, "solve_ripple_optimal_trim",
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


def _gate_residuals(conductor) -> tuple[float, float]:
    """``(before_rms_db, after_rms_db)`` — item 2's two terms, recomputed from
    the conductor's own predictions through the SAME evaluator the gate used."""
    before = spec_report_for_predicted_sum(
        _fixture_raw_predicted_sum()
    )
    after = spec_report_for_predicted_sum(conductor.measure_predicted_sum)
    return (
        spec_convergence_residual(before).rms_db,
        spec_convergence_residual(after).rms_db,
    )


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
    passing session into ``correction_not_an_improvement``, and every BETTER
    room refused harder. Both of the gate's terms are now the same instrument
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


def test_prediction_gate_refuses_a_correction_that_does_not_improve(caplog):
    """PR-L4 item 2, and the deliberate amendment to PR-6b's unconditional
    auto-apply: a prediction that still fails the spec and does not materially
    better its own pre-fit model refuses at the confirm seam, and the speaker is
    never touched.

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
    caplog.set_level(logging.ERROR, logger=_DIAG_LOGGER)
    freqs = _LINEARIZABLE_FREQS_HZ
    comb_db = 9.0 * np.sin(2.0 * np.pi * np.log2(freqs / 200.0) * 5.0)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=comb_db, tweeter_db=comb_db,
    )
    c = _cloud_conductor(fakes)

    with pytest.raises(CaptureBeginRefused) as excinfo:
        _walk_measure_cloud_to_close(c)

    assert excinfo.value.code == REASON_CORRECTION_NOT_AN_IMPROVEMENT
    assert "reason=correction_not_an_improvement" in caplog.text
    # The speaker is untouched: nothing stashed, nothing published, and no
    # payload carrying auto_apply ever came back.
    assert c.candidate is None
    assert fakes.published_candidates == []


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
    of the FULL-RESOLUTION prediction — the same tuple the accountability veto
    refused on — and not a re-grade of what survives persistence. This asserts
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

    stashed = c.measure_predicted_spec_report
    assert stashed is not None
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
    stashed = c.measure_predicted_spec_report
    assert stashed is not None
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
    assert report.to_dict() == c.measure_predicted_spec_report
    # ``log_event`` renders booleans JSON-style, so compare in its vocabulary
    # rather than Python's.
    assert f"after_passed={'true' if report.overall_passed else 'false'}" in caplog.text
    rms_db = round(float(spec_convergence_residual(report).rms_db), 3)
    assert f"after_rms_db={rms_db}" in caplog.text


def test_an_ungradeable_prediction_stashes_none_and_names_itself(caplog, monkeypatch):
    """D4's ``None`` propagation and its named log line.

    An absent report is a user-visible dead end — the review screen renders "we
    could not predict this" and refuses Apply on it — so per AGENTS.md's
    no-silent-failure rule it gets a line somebody can grep for, carrying WHICH
    of the two causes fired. ``None`` must never be papered over into a
    fabricated verdict."""
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


def test_an_accountability_refusal_names_itself_to_the_host():
    """The refusal must reach the household as ITS OWN reason, not as a
    manufactured timeout.

    The host's ``CaptureBeginRefused`` arm persists
    ``conductor.last_failure_code`` and falls back to ``relay_timeout`` when it
    is unset, so a refusal that raised without stamping the code would render
    "The measurement link timed out" over a session that was deliberately
    refused. Pinned because the exception's own code is NOT what the host
    reads.

    The realized verdict is supplied for the reason its sibling above gives:
    since the #1866 ruling a frame disagreement banks a finding and proceeds
    whenever the realized check passes on the pair about to ship, so reaching
    the refusal at all now needs both instruments to fail."""
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
            flow.CrossoverV2Conductor, "_realized_level_match", _still_mislevelled
        )
        _run_phase(c, 1, 1)
        assert c.last_failure_code is None
        with pytest.raises(CaptureBeginRefused):
            _run_phase(c, 2, 2)
    assert c.last_failure_code == REASON_DRIVER_LEVELS_DISAGREE
    assert c.last_failure_code != REASON_RELAY_TIMEOUT


def test_reason_registry_covers_both_accountability_refusals():
    """Every refusal this flow can raise has household copy and a screen — a
    bare code must never reach a phone (§5.10)."""
    for code in (REASON_DRIVER_LEVELS_DISAGREE, REASON_CORRECTION_NOT_AN_IMPROVEMENT):
        spec = REASON_REGISTRY[code]
        assert spec.template == TEMPLATE_HARD_STOP
        assert spec.retry_budget == 0
        assert spec.message and spec.message.endswith(".")
        assert code not in TRANSIENT_AUTO_RETRY_CODES


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

    def _boom(analysis, cand, cloud=None):
        raise ValueError("simulated fit engine bug")

    monkeypatch.setattr(c, "_fit_linearization", _boom)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}
    assert c.candidate.linearization_outcome == "fit_failed"
    assert "event=correction.crossover_v2_linearization_fit_failed" in caplog.text
    assert "reason=ValueError" in caplog.text
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

    def _boom(analysis, cand, cloud=None):
        raise RuntimeError("linearization fit emitted a boost")

    monkeypatch.setattr(c, "_fit_linearization", _boom)
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
    from jasper.active_speaker import crossover_v2_flow as flow_mod
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    monkeypatch.setattr(
        flow_mod, "solve_ripple_optimal_trim",
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
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    captured: dict = {}
    real_solve = flow_mod.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        # Positional call shape: solve_ripple_optimal_trim(freqs, w_tf,
        # t_tf, fc_hz, *, ..., seed_trim_db=..., trim_w_db=..., sign=...).
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(flow_mod, "solve_ripple_optimal_trim", _spy)

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
    from jasper.active_speaker import crossover_v2_flow as flow_mod

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
    real_solve = flow_mod.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(flow_mod, "solve_ripple_optimal_trim", _spy)

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
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    captured: dict = {}

    def _spy(*args, **kwargs):
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        # Force the resolved tweeter trim far from its band-average seed.
        return kwargs["seed_trim_db"] - 20.0, 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(flow_mod, "solve_ripple_optimal_trim", _spy)

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

    def _boom(analysis, cand, cloud=None):
        raise ValueError("simulated fit engine bug")

    monkeypatch.setattr(c, "_fit_linearization", _boom)
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
    c = CrossoverV2Conductor(
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


def _probed_conductor(fakes: FakeSeams, *, rollback=None):
    """A conductor walked to the point where VERIFY is the next capture.

    Uses the ELIGIBLE measure fixture because a probe needs something to have
    been commanded: an ineligible session emits no linearization filters, so
    relative to the raw crossover it commands nothing this probe can grade
    (pinned by ``test_the_commanded_delta_is_none_for_a_trims_only_candidate``).
    """
    fakes.rollback = rollback
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    return c


def _tracking_curve(c, error_db):
    """VERIFY's smoothed ``(freqs, measured, predicted)`` triple, on the grid
    the session's own commanded delta lives on, with ``error_db`` (a callable
    of frequency, or a scalar) as measured−predicted."""
    freqs = np.asarray(c.measure_commanded_delta[0], dtype=float)
    predicted = np.asarray(c.measure_predicted_sum[1], dtype=float)
    error = error_db(freqs) if callable(error_db) else np.full_like(freqs, error_db)
    return freqs, predicted + error, predicted


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

    ``_delta_probe_refusal`` returns ``None`` for it by design, so the session
    passes — and until this landed the ONLY evidence was an INFO journal line
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
    sound has been put back") is already true when they read it."""
    caplog.set_level(logging.ERROR, logger=_DIAG_LOGGER)
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
    assert verdict["code"] == REASON_CORRECTION_MODEL_ERROR
    assert c.verify_outcome == "fail"
    assert c.delta_probe.verdict == VERDICT_MODEL_ERROR
    # The rollback ran, and it ran with the reason the household will see.
    assert calls == [REASON_CORRECTION_MODEL_ERROR]
    assert "event=correction.crossover_v2_delta_probe_rollback" in caplog.text
    assert "restored=true" in caplog.text
    # The refusal names itself to the host (the same contract PR-L4 relies on).
    assert c.last_failure_code == REASON_CORRECTION_MODEL_ERROR


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
    assert "restored=false" in caplog.text
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


def _boost_vocabulary_spy(seen: list[bool]):
    real_fit = flow.fit_driver_linearization

    def _spy(resp, envelope, **kwargs):
        seen.append(kwargs["vocabulary"].allow_boost)
        return real_fit(resp, envelope, **kwargs)

    return _spy


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
        mp.setattr(flow, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)
    assert seen and all(seen)
    # …on a session that does NOT itself run VERIFY — the point of the change.
    assert PHASE_VERIFY not in c.session_phases

    # A session told its journey will not verify is refused the vocabulary…
    seen.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c2 = _cloud_conductor(fakes, post_apply_verifies=False)
        _walk_measure_cloud_to_close(c2)
    assert seen and not any(seen)

    # …and so is one that declares nothing and runs no VERIFY of its own, so
    # the undeclared default stays the conservative phase-derived reading.
    seen.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c3 = _conductor(fakes, index_phase_map={1: PHASE_CHECK, 2: PHASE_MEASURE})
        _run_phase(c3, 1, 1)
        _run_phase(c3, 2, 2)
    assert seen and not any(seen)


def test_boost_is_refused_when_the_cloud_verdict_never_reached_the_envelope():
    """**The null-exclusion gate** (adversarial review B2). The owner's ruling
    kept exactly one constraint on boost — null-exclusion stays a measured,
    registry-gated fact — and without this the ruling is unenforceable.

    ``_cloud_fit_evidence`` has two reachable ``None`` paths (the positions
    could not be combined; the honesty pipeline was unavailable). On both,
    ``compose_envelope`` gets ``excluded_bands_hz=None``, so
    ``allowed_depth_db`` is NOT zeroed in the registry's interference nulls —
    and a boost designed into a null reads MATCHED at the mark while the
    spatial arm, the one instrument that could contradict it, is absent on
    exactly those paths. So boost is withheld; cut-only proceeds."""
    fakes = FakeSeams()
    seen: list[bool] = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c = _cloud_conductor(fakes)
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


def test_the_commanded_delta_is_none_for_a_trims_only_candidate():
    """A candidate that emits no filters commands nothing this probe can grade
    relative to the raw crossover, and says so rather than inventing a zero
    curve that would classify as 'matched'."""
    predicted = (np.array([100.0, 200.0]), np.array([0.0, 0.0]))
    assert flow._commanded_delta(predicted, predicted) is None
    assert flow._commanded_delta(None, predicted) is None
    assert flow._commanded_delta(predicted, None) is None


def test_the_commanded_delta_is_the_linearized_minus_raw_prediction():
    raw = (np.array([100.0, 1000.0]), np.array([0.0, 0.0]))
    post = (np.array([100.0, 1000.0]), np.array([-1.0, 4.0]))
    freqs, delta = flow._commanded_delta(raw, post)
    assert list(freqs) == [100.0, 1000.0]
    assert list(delta) == [-1.0, 4.0]


# --------------------------------------------------------------------------- #
# adversarial-review regressions (round 2)
# --------------------------------------------------------------------------- #


def test_the_level_frame_gate_reads_the_level_match_not_the_polished_trim(caplog):
    """**S5 regression.** The frame reconciles two LEVEL-MATCH estimates, so its
    trim term is ``trim_band_average_db`` — the trim solve's own result — not
    ``trim_db``, which is that result AFTER the ripple-optimal polish moved it
    for summed flatness.

    Reading the applied trim made the gate sensitive to a refinement it is not
    measuring: the polish is bounded by ``LINEARIZATION_TRIM_SANITY_MARGIN_DB``
    (6.0), DOUBLE this gate's 3.0 dB tolerance, so an ordinary 4 dB polish
    hard-stopped an otherwise healthy session.
    """
    caplog.set_level(logging.ERROR, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    # A legitimate 4 dB ripple polish: the level match says one thing, the
    # applied trim carries the polish on top of it.
    fakes.measure = lambda program: _eligible_measure_analysis(
        program,
        trim_db={"woofer": 0.0, "tweeter": -4.701},
        trim_band_average_db=dict(_FIXTURE_RAW_TRIM_DB),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_level_frame_refused" not in caplog.text
    assert c.candidate is not None


def test_the_realized_level_assertion_still_fires_when_the_frame_agreed(caplog):
    """**S6(a).** PR-L5's frame gate closed the 2026-07-27 shape one stage
    earlier, which is why every pre-existing test of PR-L4 item 1's firing moved
    to the frame's event. Item 1 is NOT thereby dead, and this pins the gap it
    still owns.

    The two gates measure different things: the frame grades the two
    LEVEL-MATCH estimates against each other, item 1 grades the level the
    COMMITTED trim realizes.

    **Item 1's real remaining route is the NO-FRAME path.** When no driver has
    a usable core level (``core_levels_db`` empty — every envelope allows
    correction nowhere), ``level_frame`` is ``None``: the frame gate has
    nothing to grade and abstains, every ``level_frame_offset_db`` is 0.0, and
    the anchor falls back to the raw applied trim exactly as it did before
    PR-L5. A mislevelled trim then reaches the committed pair with item 1 as
    the only thing standing in front of it. (The ripple polish is NOT that
    route any more — PR-L5 anchors on the same trim term the frame solves on,
    so the polish cancels out of the anchor; the linearized scan can still move
    the committed pair, but only through the wild-trim guard, which grades both
    candidates on this same assertion first.)
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.ERROR, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    # The realized level verdict is SUPPLIED rather than provoked, for the same
    # reason ``test_wild_trim_fallback_follows_levels_not_drift`` supplies its
    # pair: the physical routes that used to mislevel a committed trim are the
    # ones PR-L3 and PR-L5 closed, and re-opening one to test the gate that
    # catches it would be testing the wrong thing. What must be pinned is that
    # item 1 still refuses on its own evidence, under its own event.
    def _match(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-5.2, difference_db=-5.2,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_realized_level_match", _match)
        _run_phase(c, 1, 1)
        with pytest.raises(CaptureBeginRefused) as excinfo:
            _run_phase(c, 2, 2)

    assert excinfo.value.code == REASON_DRIVER_LEVELS_DISAGREE
    # The FRAME passed — this is item 1's own refusal, and its own event.
    assert "event=correction.crossover_v2_level_frame_refused" not in caplog.text
    assert "event=correction.crossover_v2_level_match_refused" in caplog.text
    # …with both realized levels on the line, so the verdict is re-derivable.
    for ledger_field in (
        "difference_db=", "level_w_db=", "level_t_db=", "tolerance_db=",
    ):
        assert ledger_field in caplog.text
    # The speaker is untouched.
    assert c.candidate is None
    assert fakes.published_candidates == []


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
    estimators see it. The fit still takes the peak out and still cannot fix
    the comb, which is all this test needs: 3.90 dB pooled residual to 0.605,
    ``after_passed=false``.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    freqs = _LINEARIZABLE_FREQS_HZ
    peak_db = 12.0 * np.exp(-0.5 * ((np.log2(freqs / _FIXTURE_FC_HZ) / 0.4) ** 2))
    comb_db = 5.0 * np.sin(2.0 * np.pi * np.log2(freqs / 200.0) * 3.0)
    shape_db = peak_db + comb_db
    shape_tf = (10.0 ** (shape_db / 20.0)).astype(complex)
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs, shape_tf, shape_tf, _FIXTURE_FC_HZ,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=shape_db, tweeter_db=shape_db,
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
        assert c._branch_crossover_sections(role) == emitter[role][0], role


def test_a_role_with_no_crossover_region_is_credited_nothing_and_named(caplog):
    """…and the no-region case resolves the same way on both sides, because
    both sides ask the same function: no section, so the branch is treated as
    running full range — which is exactly what the emitter would build for it.
    It is still a defect on a 2-way conductor, so it is named in the journal
    rather than silently absorbed."""
    from jasper.active_speaker.branch_chain import sections_by_role

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(c, "_preset", types.SimpleNamespace(crossover_regions=()))
        assert c._branch_crossover_sections("woofer") == ()
    assert "event=correction.crossover_v2_linearization_no_crossover" in caplog.text
    # The shared derivation is where that answer comes from — not a branch in
    # the conductor that the emitter would have to mirror.
    assert sections_by_role(()) == {}


def _healthy_crossed_over_pair(dip_db: float = 7.0):
    """Two HEALTHY drivers, each measured THROUGH its own side of a matched
    LR4 pair at the fixture's Fc, each with one benign in-band dip.

    "Healthy" is the load-bearing word and it is true by construction: the two
    branches are the SAME flat driver behind mirrored halves of one crossover,
    so they sit at the same level and a level gate has nothing to find. The
    dips (400 Hz on the woofer, 6 kHz on the tweeter) are inside each driver's
    own radiating band, so whatever the fit does about them is driver work
    rather than crossover work.

    **The depth no longer selects an outcome, and the sweep that says so is
    below (R10a, #1817).** It used to: the depth was tuned so the two arms
    straddled item 2's 0.5 dB improvement floor, 7 dB being the value that
    maximised the smaller margin (pre 0.132 dB below the floor, post 0.091 dB
    above it). That straddle is gone. Re-swept against the shaped target,
    reading ``event=correction.crossover_v2_prediction_gate`` (floor 0.5 dB;
    ``pre`` is the pre-#1929 arm, ``post`` the shipped one):

        depth dB        3        5        7        9       11       15
        pre  improve  -0.044   +0.023   +0.036   +0.045   +0.062   +0.106
        post improve  -0.011   +0.024   +0.035   +0.046   +0.062   +0.105
        pre  frame     7.886    6.916    6.156    5.509    5.566    6.389
        post frame     0.947    0.973    0.988    1.058    0.919    0.951

    Both arms are refused at every depth, and the two arms' improvements never
    differ by more than 0.001 dB. The cause is #1809's own doctrine rather than
    a fixture that needs more amplitude: **a cut-only fit cannot fill a dip.**
    Under the old flat target the fit generated large spurious cuts instead —
    the tweeter drew about 9 dB of broadband cut, because a level mask spanning
    the crossover dragged its ``target_level_db`` to −9.80 dB for a driver whose
    passband is ~0 dB — and those cuts moved the predicted sum enough to clear
    the floor. With the shaped target the tweeter draws ZERO filters and the
    woofer's residual halves (3.4308 → 1.7063 dB rms), so what is left to grade
    is a speaker a cut-only correction genuinely cannot improve. Flipping the
    sign does not restore the straddle either: at +5/+7/+9/+11 dB bumps BOTH
    arms complete.

    7 dB is retained because nothing selects a depth any more, and moving it
    would churn the two frame numbers the caller pins for no gain. What still
    binds at 7 dB, and is what the caller reads: the pre-#1929 frame is over
    the 3.0 dB tolerance while its realized check passes (so #1866's
    finding-and-proceed path is the one exercised, not the hard refusal), and
    the shipped frame is well under it.

    The declared sweep spans (``_roles()``: woofer 150-6000, tweeter
    300-20000) both cross the 1600 Hz Fc, which is the #1929 premise and the
    ordinary case for a real declaration — the woofer radiates only below
    1282 Hz and the tweeter only above 1996 Hz.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    freqs = _LINEARIZABLE_FREQS_HZ

    def dip(center_hz: float) -> np.ndarray:
        return -dip_db * np.exp(-0.5 * ((np.log2(freqs / center_hz) / 0.3) ** 2))

    lowpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=False),)
    highpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=True),)
    woofer_db = crossover_response_db(freqs, lowpass) + dip(400.0)
    tweeter_db = crossover_response_db(freqs, highpass) + dip(6000.0)
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs,
        (10.0 ** (woofer_db / 20.0)).astype(complex),
        (10.0 ** (tweeter_db / 20.0)).astype(complex),
        _FIXTURE_FC_HZ,
    )
    return woofer_db, tweeter_db, {
        "woofer": round(float(trim_w), 3), "tweeter": round(float(trim_t), 3),
    }


def test_healthy_drivers_whose_declared_bands_cross_fc_are_not_refused(caplog):
    """**#1929, end to end.** A speaker with nothing wrong with it must not be
    refused because its drivers were swept over spans that reach past Fc.

    This is the 2026-07-30 JTS3 shape (#1870) in miniature: a clean session,
    two matched drivers, and a frame gate that read each driver's level over
    its declared CAPTURE span — which includes that driver's own crossover
    stopband — and concluded they sat 3.395 dB further apart than the trim
    solve's mirrored ±1-octave estimate of the same physical quantity, past
    the 3.0 dB tolerance. Nothing about the speaker could fix it.

    Both arms run the real production path on the identical session; the
    pre-#1929 arm is produced by taking the radiating band away from the ONE
    call that gained it, so what is compared is the band and nothing else.

    **What the #1866 ruling changed about the pre-#1929 arm, and what it did
    not.** That arm used to die at the frame gate. It no longer does — its
    6.16 dB disagreement is banked as a finding and the session continues — but
    it still does not COMPLETE: item 2 refuses it as
    ``correction_not_an_improvement``.

    **What R10a (#1817) changed, and what it did not — read this before
    trusting the older account.** Two of this test's claims were true only
    because the fit was grading each branch against a FLAT target, and both are
    now retired rather than restated:

    * *"with the radiating band, the identical session ships one."* **No longer
      true, and its failure is not #1929's.** The correction the shipped arm
      used to apply was made largely of the spurious cuts #1817 filed — with a
      level mask spanning the crossover the tweeter's ``target_level_db`` sat at
      −9.80 dB for a driver whose passband is ~0 dB, and it drew ~9 dB of
      broadband cut to reach it. With the shaped target the tweeter draws ZERO
      filters, and what is left is a pair whose only defects are two in-band
      DIPS a cut-only fit cannot fill (#1809's own doctrine). So the improvement
      gate refuses both arms — at 0.036 dB (pre) and 0.035 dB (post) against a
      0.5 dB floor — and it is right to. The full depth sweep, and the bump
      variant that does not restore the straddle either, are in
      ``_healthy_crossed_over_pair``'s docstring. The honest reading is that
      this fixture can no longer separate the two arms on the OUTCOME, because
      a gate downstream of #1929 now decides both of them identically.
    * *"the committed trim lands the two branches 0.32 dB apart where the
      pre-#1929 trim left them 2.85 dB apart."* **No longer true, and the reason
      is a genuine improvement.** Those were 2.847 and −0.321 dB under the flat
      target; they are now 0.973 and 1.005 dB — the two arms land within 0.04 dB
      of each other and both pass. **R10a made the pipeline robust to the
      pre-#1929 frame error.** The frame still mislevels the ANCHOR by its full
      6.16 dB, but the ripple-optimal scan now walks it back: 5.4 dB in the
      pre-#1929 arm against 0.2 dB in the shipped one, asserted below. Under the
      flat target the scan could not, because the fit had already buried ~9 dB
      of spurious cut in the branch it was scanning, and the mislevel survived
      into the shipped pair. So the realized instrument no longer corroborates
      #1929 — it now says the frame error does not reach the household at all.

    **What this test still pins, and why it is not vacuous.** #1929's own
    instrument, on the identical session: 6.16 dB over the declared span against
    0.99 dB over the radiating band, and with it the difference that still
    reaches a household — the pre-#1929 arm mints a finding telling the owner
    their two matched drivers sit 6.16 dB apart, and the shipped arm mints none.
    Reverting #1929 fails this test on both.
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    woofer_db, tweeter_db, trim_db = _healthy_crossed_over_pair()

    def session():
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(
            program, woofer_db=woofer_db, tweeter_db=tweeter_db, trim_db=trim_db,
        )
        conductor = _conductor(fakes)
        _run_phase(conductor, 1, 1)
        return conductor

    # Both candidate trim pairs the level adjudication grades, in the order
    # ``_fit_linearization`` grades them (resolved, then anchored) — two per
    # arm. This is what makes the scan's rescue readable instead of inferred.
    graded: list[dict] = []
    real_match = flow.CrossoverV2Conductor._realized_level_match

    def _spy(self, freqs, w_tf, t_tf, trims_db, *args, **kwargs):
        graded.append(dict(trims_db))
        return real_match(self, freqs, w_tf, t_tf, trims_db, *args, **kwargs)

    # --- pre-#1929: the whole declared span, and a healthy speaker refused ---
    #
    # WARNING, not ERROR: the frame's refusal is an ERROR line and is still
    # caught by its absence below, and capturing one level lower also makes the
    # WARNING finding line readable — which is the difference between the arms
    # that still reaches a household.
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    # Both lambdas swallow **kwargs on purpose: they exist to drop the
    # radiating band specifically, and dropping any FUTURE keyword the same way
    # is the behaviour that keeps this arm a faithful pre-#1929 call rather
    # than a half-updated one. If a later argument must survive into this arm,
    # that is a deliberate edit here, not something to inherit silently.
    with pytest.MonkeyPatch.context() as mp:
        whole_band = flow.driver_core_level_db
        whole_band_span = flow.core_level_band_hz
        mp.setattr(
            flow, "driver_core_level_db",
            lambda resp, env, **_band: whole_band(resp, env),
        )
        # ...and the disclosure with it, so the pre-fix arm is a faithful
        # pre-#1929 session rather than one whose journal and whose number
        # disagree about which band was used.
        mp.setattr(
            flow, "core_level_band_hz", lambda env, **_band: whole_band_span(env),
        )
        mp.setattr(flow.CrossoverV2Conductor, "_realized_level_match", _spy)
        before = session()
        with pytest.raises(CaptureBeginRefused) as excinfo:
            _run_phase(before, 2, 2)
    # Still refused, and still on this session's own evidence — but since the
    # #1866 ruling the frame BANKS its 6.16 dB rather than stopping on it, and
    # the refusal comes from the prediction gate.
    assert excinfo.value.code == REASON_CORRECTION_NOT_AN_IMPROVEMENT
    assert before._last_level_frame_disagreement_db == pytest.approx(6.16, abs=0.1)
    assert (
        before._last_level_frame_disagreement_db
        > LEVEL_FRAME_AGREEMENT_TOLERANCE_DB
    )
    # The frame's refusal is the ERROR line — its absence is the assertion that
    # the frame banked rather than stopped. The banked line is the WARNING
    # beside it, and IS asserted here rather than only in
    # ``test_a_disagreeing_frame_whose_realized_check_passes_banks_and_
    # proceeds``: since R10a it is the difference between the arms that still
    # reaches a household, so it has to be read on both.
    assert "event=correction.crossover_v2_level_frame_refused" not in caplog.text
    assert "event=correction.crossover_v2_level_frame_finding" in caplog.text
    assert before.candidate is None

    # --- and with the radiating band: the identical session, judged fairly ---
    caplog.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_realized_level_match", _spy)
        after = session()
        # Refused too, since R10a — and NOT for anything the level frame said;
        # see this test's docstring for the measured account and
        # ``_healthy_crossed_over_pair``'s for the depth sweep. A cut-only fit
        # cannot fill this pair's two in-band dips, so there is no material
        # improvement to grade, at any depth, in either arm.
        with pytest.raises(CaptureBeginRefused) as after_excinfo:
            _run_phase(after, 2, 2)
    assert after_excinfo.value.code == REASON_CORRECTION_NOT_AN_IMPROVEMENT
    assert after._last_level_frame_disagreement_db == pytest.approx(0.99, abs=0.1)
    assert after._last_level_frame_disagreement_db < LEVEL_FRAME_AGREEMENT_TOLERANCE_DB
    # THE DIFFERENCE THAT STILL REACHES A HOUSEHOLD, and the reason this test is
    # not vacuous now that both arms end the same way. Over the declared span
    # the owner of two matched drivers is told they sit 6.16 dB apart; over the
    # radiating band nothing is minted at all, because there is nothing to
    # report. Reverting #1929 flips both of these, not just the magnitude above.
    assert "event=correction.crossover_v2_level_frame_finding" not in caplog.text
    assert "event=correction.crossover_v2_level_frame_refused" not in caplog.text

    # THE REALIZED INSTRUMENT, which since R10a says something different from
    # what it used to — and it is the better news. PR-L4 item 1 reads different
    # inputs (the post-fit branches), a different band (mirrored ±1 octave) and
    # a different statistic (power mean) from the fit's median, so it is where a
    # frame error would show up in the shipped pair. It does not show up: the
    # two arms land within 0.04 dB of each other and both pass, where under the
    # flat target they read 2.847 and −0.321 dB. (It is not a fully independent
    # third opinion — it is ``solve_branch_trims``' own estimator re-read on the
    # trimmed pair, "One estimator, not a second opinion" per its own docstring.)
    before_difference_db = before._last_realized_level_match.difference_db
    after_difference_db = after._last_realized_level_match.difference_db
    assert before_difference_db == pytest.approx(0.973, abs=0.05)
    assert after_difference_db == pytest.approx(1.005, abs=0.05)
    assert abs(before_difference_db - after_difference_db) < 0.1
    assert before._last_realized_level_match.matched is True
    assert after._last_realized_level_match.matched is True

    # …and the MECHANISM that makes them agree, so the line above reads as a
    # measured rescue rather than a coincidence. Four graded pairs, two per arm,
    # ``(resolved, anchored)`` each: the pre-#1929 arm's anchor is mislevelled by
    # the frame's full error and the ripple-optimal scan walks the tweeter 5.6 dB
    # to undo it; the shipped arm's anchor is already right and the scan moves it
    # 0.4 dB. That walk is what the flat target used to prevent, by burying ~9 dB
    # of spurious cut in the very branch the scan reads.
    #
    # Both walks grew 0.2 dB at R10b (5.4 -> 5.6 and 0.2 -> 0.4) because
    # the anchor each is measured FROM now carries the realized biquad cascade's
    # give-back rather than `predicted_response`'s Lorentzian. The ratio the
    # claim rests on survives with room to spare, and the two arms' realized
    # level match — the actual subject above — moved by under 0.04 dB.
    assert len(graded) == 4, graded
    (pre_resolved, pre_anchored, post_resolved, post_anchored) = graded
    pre_walk_db = abs(pre_resolved["tweeter"] - pre_anchored["tweeter"])
    post_walk_db = abs(post_resolved["tweeter"] - post_anchored["tweeter"])
    assert pre_walk_db == pytest.approx(5.6, abs=0.1)
    assert post_walk_db == pytest.approx(0.4, abs=0.1)
    assert pre_walk_db > 10.0 * post_walk_db


def test_the_frame_still_disagrees_on_a_pair_that_is_perfect_by_construction():
    """**What #1929 did NOT close, pinned so nobody has to rediscover it.**

    Two branches that are the SAME flat driver behind mirrored halves of one
    LR4 — no dips, no tilt, nothing to measure wrong — still leave the frame's
    two estimators 0.910 dB apart. Banding the median took that from 9.408 dB,
    which is the whole of #1929; it did not take it to zero, and it was never
    going to: the trim solve reads a power mean over one octave either side of
    Fc while the fit reads a median over the driver's entire radiating band,
    and those are different questions about a curve that is not flat in the
    same way over both.

    Why pin it. 0.910 dB of the 3.0 dB tolerance is spent before any real
    speaker contributes a single dB, so the headroom for genuine measurement
    spread is 2.09 dB, not 3.0 — and an ordinary −2 dB/oct woofer passband tilt
    is enough to refuse at 3.574 dB while the realized-level instrument reads
    1.41 dB and passes (both measured in this module). That is where the next
    field refusal comes from. A future change that narrows this gap should move
    this number; one that widens it should have to argue with this test first.

    EXTERNAL corroboration, deliberately not asserted here: an offline re-fit
    of the 2026-07-30 field bundle puts that session at 3.2307 dB under the
    banded estimator — still refused. That is laptop-side gitignored evidence
    recorded on #1870, with no in-repo replay path, so it is cited and not
    tested; the numbers this test DOES assert are the synthetic ones above.
    Completing that field session needs the frame-gate semantics change ruled
    on #1866, not a further estimator fix.

    **This fixture COMPLETES the journey since R10a, and that is the point of
    the second half of this test.** It used to stop at the prediction gate:
    against a FLAT target the fit read each branch's own crossover rolloff as
    a deficit, placed filters fighting it, and made the predicted sum WORSE
    than no correction at all — so a *perfect* speaker was refused. Against
    the crossover-shaped target (#1817) the same pair draws **zero filters on
    both branches** and 0.000 dB of headroom cost, the prediction stays in
    spec, and the session is accepted. A speaker with nothing wrong with it
    now gets nothing done to it.

    The frame number is asserted either way, because it is this test's actual
    subject and it did NOT move: 0.910 dB before R10a and 0.910 dB after, which
    is also the evidence that the shaped target left ``target_level_db``'s
    meaning alone (it is re-centred to add no level).
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    freqs = _LINEARIZABLE_FREQS_HZ
    woofer_db = crossover_response_db(
        freqs, (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=False),),
    )
    tweeter_db = crossover_response_db(
        freqs, (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=True),),
    )
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
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    accepted = _run_phase(c, 2, 2)["accepted"]

    # #1929's own number, unmoved by R10a's shaped target.
    assert c._last_level_frame_disagreement_db == pytest.approx(0.910, abs=0.02)
    assert c._last_level_frame_disagreement_db < LEVEL_FRAME_AGREEMENT_TOLERANCE_DB

    # R10a: a pair that is perfect by construction is left ALONE and accepted.
    # Both halves matter — zero filters is the #1817 claim, and `accepted` is
    # what proves the old objective's refusal of a perfect speaker is gone.
    assert accepted is True
    for role, fit in c.candidate.linearization.items():
        assert fit["filters"] == [], (role, fit["filters"])
        assert fit["headroom_cost_db"] == 0.0, role


def test_the_level_frame_refusal_names_the_levels_and_bands_it_read(caplog):
    """#1929 observability: the refusal that used to report only a
    disagreement now carries the inputs that produced it — each role's core
    level, the band the median was actually taken over, and the radiating
    bound that was asked for — so a field diagnosis does not have to re-derive
    which driver read what.

    The −20 dB tweeter trim is a deliberate BRANCH-FORCER, not a physics
    fixture: it is the cheapest way to drive the frame past its tolerance so
    the refusal arm runs at all. Nothing here should be read as a claim about
    what a real speaker's trim looks like — the physics claims live in
    ``test_healthy_drivers_whose_declared_bands_cross_fc_are_not_refused`` and
    in the corpus regression, both of which derive their trims from their own
    branches.

    **Two forcers now, and the second one arrived with the #1866 ruling.**
    Over-tolerance alone no longer reaches the REFUSAL: since the frame gate's
    semantics change, a disagreement is banked as a finding and the session
    proceeds whenever the realized-level check passes on the pair about to
    ship. So this test
    supplies the unmatched realized verdict that the refusal arm now requires,
    the same way ``test_the_realized_level_assertion_still_fires_when_the_
    frame_agreed`` supplies its own. Every assertion below is unchanged — the
    disclosure this test exists for is #1929's and neither ruling touched it.
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.ERROR, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, trim_db={"woofer": 0.0, "tweeter": -20.0},
    )
    c = _conductor(fakes)

    def _unmatched(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-9.0, difference_db=-9.0,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_realized_level_match", _unmatched)
        _run_phase(c, 1, 1)
        with pytest.raises(CaptureBeginRefused):
            _run_phase(c, 2, 2)

    assert "event=correction.crossover_v2_level_frame_refused" in caplog.text
    assert "core_level_db=" in caplog.text
    for key in ("'level_db'", "'band_hz'", "'radiating_band_hz'"):
        assert key in caplog.text, key
    assert set(c._last_level_frame_cores) == {"woofer", "tweeter"}
    # The band reported is the one the median was taken over. On this ordinary
    # two-way both roles clear the width floor, so it sits inside the bound
    # rather than falling back to the whole mask — and the two keys differ,
    # which is what makes reporting both worth the space.
    for role in ("woofer", "tweeter"):
        disclosed = c._last_level_frame_cores[role]
        assert disclosed["band_hz"] != disclosed["radiating_band_hz"]
        assert disclosed["band_hz"][0] >= disclosed["radiating_band_hz"][0]


# --------------------------------------------------------------------------- #
# #1866: the frame gate's finding+proceed path (owner ruling, 2026-07-30)
# --------------------------------------------------------------------------- #
#
# The synthetic stand-in for the 2026-07-30 field session. That session is
# laptop-side and gitignored — 3.2307 dB frame under #1929's banded estimator,
# realized −0.247 dB matched, predicted on-axis residual 3.106 → 1.333 dB, all
# recorded on #1870 — so it is CITED and never replayed. What is replayed is a
# fixture with the same SHAPE: an extra −1.6 dB/octave of woofer passband tilt,
# an ordinary driver in baffle-step territory, which lands the frame at
# 3.894 dB against the 3.0 tolerance while the realized-level instrument reads
# −0.978 dB and passes. Both numbers are asserted below, so a change that moves
# either has to argue with these tests.
#
# The tilt is the fixture's physical premise and has never moved; these two
# numbers are its CONSEQUENCE and have, twice:
#
#  * 3.276 → 3.209 (#1938 gate follow-up) — _solve_fixture_raw_trim's
#    hardcoded-woofer-0.0 return was fixed, and at this tilt the woofer is the
#    louder branch, so the coherent trim attenuates it by 0.067 dB. See
#    _tilted_woofer_fixture's docstring.
#  * 3.209 → 3.894 (R10a, #1817) — _fixture_branch_db became faithful, so the
#    curves this fixture tilts now carry each branch's own crossover, as a real
#    per-driver measurement does. Both of the frame's estimators read the
#    MEASUREMENT, so a truer measurement moves them; neither reads the fit's
#    target, and the value is 3.894 with the shaped target and with a flat one
#    alike (measured both ways).
_LEVEL_FRAME_FINDING_TILT_DB_PER_OCT = -1.6


def _tilted_woofer_fixture(fakes: FakeSeams, *, tilt_db_per_oct: float) -> None:
    """Point ``fakes`` at the eligible fixture with extra woofer tilt.

    The trim is SOLVED from the tilted branches rather than written down, for
    the reason ``_solve_fixture_raw_trim`` gives: a hand-picked trim is a
    fixture field nobody derived from the fixture, and this test is about the
    relationship between two estimators of exactly that number.
    """
    freqs = _LINEARIZABLE_FREQS_HZ
    base_woofer_db, tweeter_db = _fixture_branch_db()
    woofer_db = base_woofer_db + tilt_db_per_oct * np.log2(
        np.maximum(freqs, 1.0) / _FIXTURE_FC_HZ
    )
    # #1938 gate follow-up (SF-1): this used to hand-roll the same
    # hardcoded-woofer-0.0 solve _solve_fixture_raw_trim had, which was a
    # silent no-op at production tilt (-1.6 dB/oct) — that tilt makes the
    # WOOFER the louder branch (level_w 1.4467 > level_t 1.3797), so the true
    # trim is {"woofer": -0.067, "tweeter": 0.0}, not {"woofer": 0.0, ...}.
    # Reusing the now-general helper instead of a second hand-rolled copy.
    trim_db = _solve_fixture_raw_trim(woofer_db, tweeter_db)
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=woofer_db, tweeter_db=tweeter_db, trim_db=trim_db,
    )


def test_a_disagreeing_frame_whose_realized_check_passes_banks_and_proceeds(
    caplog,
):
    """**The ruling, end to end** (owner, 2026-07-30 bench, #1866): "when
    ``solve_shared_level_frame``'s two estimators disagree beyond
    ``LEVEL_FRAME_AGREEMENT_TOLERANCE_DB`` but the
    ``realized_branch_level_match`` check PASSES, the fit **banks the
    disagreement as an M7-class finding** … and proceeds."

    Four things are pinned:

    1. the session COMPLETES rather than refusing;
    2. a finding is banked carrying both estimators, both bands, and the
       realized difference;
    3. **proceeding is the same tune, not refused** — the committed trims are
       the pair the fit's own level adjudication picked out of the two IT
       computed, and the inter-driver placement they carry is the fit's
       anchor's, not the trim solve's;
    4. the decision is visible in the journal under its own event.

    **Claim 3 is deliberately NOT "proceeds on the near-Fc anchor (the trim
    solve)", which is the ruling's wording and is inverted.** The trim term
    cancels out of ``anchor_base + giveback + level_frame_offset``, so the
    committed inter-driver placement is set by the CORE-MEDIAN frame — the
    *disputed* estimator. This fixture measures that directly: the anchor
    places the pair 0.756 dB apart, which is the core-median frame's own value;
    anchoring on the trim solve's placement instead would give 4.650; and the
    two differ by 3.894 — exactly the banked disagreement. What proceeding buys
    is that the session is not refused; it does not switch estimators, and the
    realized check grades the outcome rather than picking a winner. The gate
    comment in ``_assert_accountable`` derives all of this.

    **Where claim 3's old "byte-for-byte the anchor" wording went (R10a,
    #1817).** Before R10a the ripple-optimal scan returned its own seed on this
    fixture, so committed, anchored and resolved were one number and the claim
    could be written as an identity on the anchor. It never was one:
    ``_fit_linearization`` grades BOTH candidate pairs — the anchor and the
    scan's ripple polish — against the realized-level instrument
    unconditionally, and commits whichever LEVELS better ("inter-driver level is
    the load-bearing property, summed ripple is the polish"). Once the fit stops
    burying the pair under crossover-fighting cuts the linearized branches
    differ, the scan finds a genuinely better ripple point 0.400 dB off its
    seed, and that pair levels better as well (|−0.952| against |−1.352| dB), so
    it is what ships. The polish sits well inside the 6.0 dB sanity margin, so
    no guard runs. The claim itself is unchanged and is asserted below in the
    form it always had: the placement follows the core median.

    **Why every magnitude here moved ~0.03-0.13 dB at R10b** (the claim-seam
    change; first-principles panel CC-2(b)).
    ``correction_giveback_db`` is the anchor's own input, and it now measures
    the level the REALIZED biquad cascade removes rather than the level
    ``peq.predicted_response``'s Lorentzian bell said it would. The anchor, the
    placement, the scan's walk and both realized differences all ride on it, so
    all of them shifted together; the DISAGREEMENT (3.894 dB) did not, because
    the give-back cancels out of it. No filter moved, and every verdict — the
    session completes, the finding banks, the polish ships, no guard fires — is
    the same. The numbers in this docstring are the post-R10b ones.

    **Why the trim-solve estimator's own number moved once before** (#1938 gate
    follow-up). ``_tilted_woofer_fixture`` used to hand a coherent-looking but
    silently wrong trim to this fixture at this exact tilt. This is about
    ``solve_branch_trims``'s mirrored ±1-octave band-power average, NOT the
    fit's core median — on that estimator the woofer is the louder branch at
    this tilt, so the true trim attenuates the WOOFER rather than the tweeter by
    0.0 (the old hardcoded-woofer-0.0 helper's answer, regardless of which
    branch the solve said was louder). The placement identity was untouched by
    it, because the trim term genuinely cancels out of ``anchor_base + giveback
    + level_frame_offset`` as this docstring already claimed; the trim-solve
    anchor and the disagreement it produces both shifted by exactly the new
    woofer trim, because both carry ``trims["woofer"]`` directly while the
    core-median frame never reads the raw trim at all. R10a then moved all three
    again, for the unrelated reason recorded above
    ``_LEVEL_FRAME_FINDING_TILT_DB_PER_OCT``.

    Why the tilt is the right provocation rather than a contrivance: #1929
    removed a structural bias from one estimator, it did not make the two
    agree, and what is left scales with ordinary driver shape. A speaker with
    a −1.6 dB/oct woofer passband — baffle-step territory, not a defect —
    refuses under the pre-ruling gate while the instrument that grades the
    OUTPUT says the tune is fine.
    """
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    _tilted_woofer_fixture(
        fakes, tilt_db_per_oct=_LEVEL_FRAME_FINDING_TILT_DB_PER_OCT
    )
    c = _conductor(fakes)
    banked = fakes.banked_findings

    # Record every trim pair the level adjudication grades, so claim 3 below
    # can assert against the ANCHOR the fit itself solved instead of a number
    # written down here. The wrapper is transparent — it defers to the real
    # estimator and only observes.
    graded: list[dict] = []
    real_match = flow.CrossoverV2Conductor._realized_level_match

    def _spy(self, freqs, w_tf, t_tf, trims_db, *args, **kwargs):
        graded.append(dict(trims_db))
        return real_match(self, freqs, w_tf, t_tf, trims_db, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_realized_level_match", _spy)
        _run_phase(c, 1, 1)

        # 1 — the session completes. Under the pre-ruling gate this raised
        # CaptureBeginRefused(driver_levels_disagree).
        verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate is not None
    assert c.last_failure_code is None

    # The two instruments, and the fact that they disagree about a speaker
    # whose OUTPUT is fine. This is the whole premise of the ruling, so it is
    # asserted as magnitudes rather than as booleans.
    assert c._last_level_frame_disagreement_db == pytest.approx(3.894, abs=0.02)
    assert (
        c._last_level_frame_disagreement_db > LEVEL_FRAME_AGREEMENT_TOLERANCE_DB
    )
    assert c._last_realized_level_match.difference_db == pytest.approx(
        -0.952, abs=0.02
    )
    assert c._last_realized_level_match.matched is True

    # 2 — one finding banked, carrying all three instruments and both bands.
    assert len(banked) == 1
    record = banked[0]
    assert record["disagreement_db"] == pytest.approx(3.894, abs=0.02)
    assert record["tolerance_db"] == LEVEL_FRAME_AGREEMENT_TOLERANCE_DB
    assert record["realized_difference_db"] == pytest.approx(-0.952, abs=0.02)
    for role in ("woofer", "tweeter"):
        # estimator 1 (the fit's median) and estimator 2 (the trim solve's
        # own level-match term) — the two numbers that disagreed
        assert f"core_level_db_{role}" in record
        assert f"trim_band_average_db_{role}" in record
        # both bands, per #1929's disclosure pair
        assert record[f"core_band_lo_hz_{role}"] is not None
        assert record[f"core_band_hi_hz_{role}"] is not None
        assert record[f"radiating_band_lo_hz_{role}"] is not None
    # The band the finding is about is the span the medians were read over —
    # its edges are the OUTER edges of the two core bands, so the interval
    # spans the 1255.8–2020 Hz gap between them, which NEITHER median read.
    # That is the honest shape for a finding about the relationship BETWEEN
    # two drivers (it is about the handoff, which lives in that gap) and it is
    # deliberately not a claim that anything was measured there.
    assert record["f_lo_hz"] == record["core_band_lo_hz_woofer"]
    assert record["f_hi_hz"] == record["core_band_hi_hz_tweeter"]
    assert record["core_band_hi_hz_woofer"] < record["core_band_lo_hz_tweeter"]

    # 3 — proceeding is the SAME TUNE, not refused: the committed trims are one
    # of the two pairs the fit itself computed and graded, taken from the fit's
    # OWN call rather than recomputed here (``graded`` records both, in the
    # order ``_fit_linearization`` grades them: resolved, then anchored).
    assert len(graded) == 2, graded
    committed = dict(c.candidate.role_attenuations_db)
    resolved, anchored = graded[0], graded[1]
    assert set(anchored) == set(committed) == set(resolved)
    # The scan's ripple polish is what ships here, because the adjudication
    # commits whichever pair LEVELS better and this one does. That is the
    # ordinary trusted path, not a fallback: the polish is 0.400 dB off its own
    # seed, an order of magnitude inside the sanity margin, so no guard ran.
    for role, value in resolved.items():
        assert committed[role] == pytest.approx(value, abs=1e-9)
    drift_db = max(abs(resolved[role] - anchored[role]) for role in anchored)
    assert drift_db == pytest.approx(0.400, abs=0.02)
    assert drift_db < LINEARIZATION_TRIM_SANITY_MARGIN_DB
    assert "event=correction.crossover_v2_linearization_trim_rejected" not in (
        caplog.text
    )

    # …and WHICH estimator the ANCHOR places on, because the ruling's own
    # wording ("the trim solve") says the opposite of what the code does. The
    # trim term cancels out of ``anchor_base + giveback + level_frame_offset``,
    # leaving ``giveback + system − core``, so the anchored inter-driver
    # placement follows the CORE MEDIAN — the disputed estimator. Read off the
    # ANCHOR and not off ``committed`` because the ripple polish above moves the
    # committed pair by its own 0.400 dB, which is the scan's business and not
    # the frame's; the anchor is the number the frame produced. Asserted as the
    # identity rather than as a magic number: the placement the trim solve would
    # have produced differs from the anchored one by exactly the banked
    # disagreement.
    lin = c.candidate.linearization
    giveback = {role: lin[role]["correction_giveback_db"] for role in lin}
    cores = {r: v["level_db"] for r, v in c._last_level_frame_cores.items()}
    trims = dict(c._last_level_frame_trims)
    placed = anchored["woofer"] - anchored["tweeter"]
    core_frame = (
        (giveback["woofer"] - cores["woofer"])
        - (giveback["tweeter"] - cores["tweeter"])
    )
    trim_frame = (
        (giveback["woofer"] + trims["woofer"])
        - (giveback["tweeter"] + trims["tweeter"])
    )
    # 1e-3 is the disclosure's own rounding: ``_last_level_frame_cores`` reports
    # each core level to 3 dp, so ``core_frame`` carries up to a half-step of
    # rounding on each of its two terms.
    assert placed == pytest.approx(core_frame, abs=1e-3)
    assert placed == pytest.approx(0.756, abs=0.02)
    assert trim_frame == pytest.approx(4.650, abs=0.02)
    assert abs(trim_frame - core_frame) == pytest.approx(
        c._last_level_frame_disagreement_db, abs=1e-3
    )

    # 4 — the decision is in the journal under its own event, and the refusal
    # event is NOT, so a reader can tell a banked session from a stopped one.
    assert "event=correction.crossover_v2_level_frame_finding" in caplog.text
    assert "event=correction.crossover_v2_level_frame_refused" not in caplog.text
    assert "trim_band_average_db=" in caplog.text
    assert "realized_difference_db=" in caplog.text


def test_a_disagreeing_frame_the_realized_check_also_fails_still_refuses(caplog):
    """**The half the ruling did NOT change**: "The hard refusal remains when
    the realized check ALSO fails."

    Same fixture, same disagreement — only the realized verdict differs. The
    refusal keeps PR-L4's own ``driver_levels_disagree`` code and therefore its
    shipped household sentence, no finding is minted, and the candidate is
    never stashed. **No finding on this path is deliberate, not an omission**:
    the record's whole content is "two estimators disagreed AND the pair we
    were about to ship still came out level", and with the realized check
    failing the second half is false — banking one anyway would persist a
    diagnosis whose own evidence contradicts it, in a session the household is
    being told to go fix.

    The realized verdict is SUPPLIED rather than provoked, for the reason
    ``test_the_realized_level_assertion_still_fires_when_the_frame_agreed``
    gives: the physical routes that mislevel a committed trim are the ones
    PR-L3 and PR-L5 closed, and re-opening one would test the wrong thing.
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    _tilted_woofer_fixture(
        fakes, tilt_db_per_oct=_LEVEL_FRAME_FINDING_TILT_DB_PER_OCT
    )
    c = _conductor(fakes)
    banked = fakes.banked_findings

    def _unmatched(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=-20.0, level_t_db=-11.0, difference_db=-9.0,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_realized_level_match", _unmatched)
        _run_phase(c, 1, 1)
        with pytest.raises(CaptureBeginRefused) as excinfo:
            _run_phase(c, 2, 2)

    # Today's code and today's copy — both unchanged by the ruling.
    assert excinfo.value.code == REASON_DRIVER_LEVELS_DISAGREE
    spec = REASON_REGISTRY[REASON_DRIVER_LEVELS_DISAGREE]
    assert excinfo.value.user_message == (spec.message or spec.banner)
    assert c.last_failure_code == REASON_DRIVER_LEVELS_DISAGREE
    assert c.candidate is None
    assert banked == []
    assert "event=correction.crossover_v2_level_frame_refused" in caplog.text
    assert "event=correction.crossover_v2_level_frame_finding" not in caplog.text
    # The refusal line stays the shape #1934 shipped: the two fields the
    # finding path adds carry nothing here.
    assert "trim_band_average_db={}" in caplog.text
    assert "realized_difference_db=null" in caplog.text


def test_a_banked_finding_never_costs_the_session_it_was_banked_for(caplog):
    """§3.4: findings are *optional* evidence artifacts — "a session with no
    findings behaves exactly as it does today". So a store that refuses must
    lose the diagnosis, never the tune.

    Two degraded modes, both of which must still complete: a conductor with no
    findings seam at all (every unit test, and any host with no evidence
    store), and a seam that raises.
    """
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)

    # (a) no seam bound at all — the default for a conductor with no store.
    unbound = FakeSeams()
    _tilted_woofer_fixture(
        unbound, tilt_db_per_oct=_LEVEL_FRAME_FINDING_TILT_DB_PER_OCT
    )
    c = CrossoverV2Conductor(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(unbound.seams(), publish_findings=None),
        driver_spacing_m=0.15,
    )
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert c.candidate is not None
    # The number is still in the journal even with nowhere to persist it.
    assert "event=correction.crossover_v2_level_frame_finding" in caplog.text

    # (b) the seam raises the store's own failure family.
    caplog.clear()
    fakes = FakeSeams()
    _tilted_woofer_fixture(
        fakes, tilt_db_per_oct=_LEVEL_FRAME_FINDING_TILT_DB_PER_OCT
    )

    def _explode(_record):
        raise RuntimeError("write-once conflict")

    base = fakes.seams()
    exploding = CrossoverV2Conductor(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(base, publish_findings=_explode),
        driver_spacing_m=0.15,
    )
    _run_phase(exploding, 1, 1)
    assert _run_phase(exploding, 2, 2)["accepted"] is True
    assert exploding.candidate is not None
    assert (
        "event=correction.crossover_v2_level_frame_finding_failed" in caplog.text
    )


def test_a_retaken_eager_fit_banks_its_finding_exactly_once():
    """**The reason the finding is minted at the gate but persisted at the
    commit** — pinned, because it is the whole justification for a two-step
    that would otherwise look like indirection for its own sake.

    The gate runs on the EAGER close and again on the confirm when a retake
    moots the bank (``run_speculative_group_close``: "the bank stays empty and
    the confirm refits"), so it mints more than once. The findings store is
    write-once. Publishing at the gate would therefore hand the store the same
    path twice on this ordinary, household-reachable path — a ``PATH_CONFLICT``
    on the second — and the first write would describe a candidate that no
    longer exists. Riding ``_SpeculativeClose`` and publishing from
    ``_commit_measure_candidate`` makes both impossible: the dropped build
    takes its record with it, and the commit fires once behind
    ``confirm_cloud_measure_group``'s ``_candidate`` guard.
    """
    fakes = FakeSeams()
    _tilted_woofer_fixture(
        fakes, tilt_db_per_oct=_LEVEL_FRAME_FINDING_TILT_DB_PER_OCT
    )
    c = _cloud_conductor(fakes)
    attempt = _walk_measure_cloud_to_accept(c)

    # The eager fit runs the gate — and banks nothing durable, because nothing
    # is committed yet.
    assert c.run_speculative_group_close() is True
    assert c._speculative_close.level_frame_finding is not None
    assert fakes.banked_findings == []

    # The household redoes the final spot: the eager build is discarded whole,
    # its record with it.
    for _ in range(GEOMETRY_RETRY_POSITIONS + 1):
        verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
        attempt += 1
        if verdict["accepted"]:
            break
    assert c._speculative_close is None
    assert fakes.banked_findings == []

    # The confirm refits, re-mints, and commits — once.
    assert _confirm_cloud(c)["candidate_fingerprint"]
    assert len(fakes.banked_findings) == 1
    # 3.894 since R10a made _fixture_branch_db faithful — the frame's own
    # consequence of a truer measurement, derived in the section comment above
    # _LEVEL_FRAME_FINDING_TILT_DB_PER_OCT rather than restated here.
    assert fakes.banked_findings[0]["disagreement_db"] == pytest.approx(
        3.894, abs=0.02
    )
    # A re-delivered confirm signal cannot double-publish it.
    assert c.confirm_cloud_measure_group() is None
    assert len(fakes.banked_findings) == 1


def test_a_gate_that_refuses_after_the_frame_banked_persists_nothing():
    """The other half of the same design: a record minted at the frame gate
    dies with the candidate when a LATER gate refuses.

    Item 2 refuses this session — the correction does not improve its own
    model — after the frame gate has already banked. Nothing is committed, so
    nothing is published: the record describes the frame behind a proposal,
    and there is no proposal. A gate-site publish would have persisted a
    diagnosis for a tune the household was never offered.
    """
    fakes = FakeSeams()
    _tilted_woofer_fixture(
        fakes, tilt_db_per_oct=_LEVEL_FRAME_FINDING_TILT_DB_PER_OCT
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    # This fixture clears item 2 by 0.983 dB against the shipped 0.5 dB floor
    # (``reason=improved``), so raising the floor is the smallest change that
    # makes a LATER gate refuse while the frame gate above still banks. The
    # constant itself is not under test here — the ordering is.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB", 5.0)
        with pytest.raises(CaptureBeginRefused) as excinfo:
            _run_phase(c, 2, 2)

    assert excinfo.value.code == REASON_CORRECTION_NOT_AN_IMPROVEMENT
    assert c.candidate is None
    assert fakes.banked_findings == []


def test_an_agreeing_frame_banks_nothing():
    """The ordinary session, which the ruling does not touch: a frame inside
    tolerance mints no finding and calls no seam.

    Pinned because "banks a finding" is a NEW side effect on the path every
    healthy speaker takes, and the cheapest way for it to go wrong is to fire
    unconditionally — which would put a diagnosis of a disagreement in front
    of every household whose instruments agreed.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    banked = fakes.banked_findings
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert (
        c._last_level_frame_disagreement_db < LEVEL_FRAME_AGREEMENT_TOLERANCE_DB
    )
    assert banked == []


def test_no_boost_lands_in_a_drivers_own_crossover_stopband():
    """**#1809, end to end.** Whatever the fit decides, no emitted boost may
    sit where this driver's own crossover has handed off. Cuts are unaffected —
    they remove leakage that still reaches the summed response.

    Held on the conductor rather than only on the fit engine because the
    radiating band is the CONDUCTOR's to solve (it owns the preset's crossover
    regions); a wiring regression here would silently restore the defect with
    the fit engine's own tests still green.
    """
    from jasper.active_speaker.branch_chain import radiating_band_hz

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    boosts_seen = False
    for role, fit in c.candidate.linearization.items():
        sections = c._branch_crossover_sections(role)
        lo_hz, hi_hz = radiating_band_hz(sections)
        for f in fit["filters"]:
            if f["gain"] > 0.0:
                boosts_seen = True
                assert lo_hz <= f["freq"] <= hi_hz, (role, f)
    assert boosts_seen, "the fixture must emit a boost for this to mean anything"


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
                sections=c._branch_crossover_sections(role),
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
    assert _run_phase(c, 3, 3)["accepted"] is True

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
    c = CrossoverV2Conductor(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            retain_position=lambda pid, r, meta: retained.append(dict(meta)),
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
    assert _run_phase(c, 3, 3)["accepted"] is True

    assert c.verify_outcome == "pass"
    assert c.verify_graded_band_hz == [2000.0, 4000.0]


def _tracking_with_frame(**frame_overrides):
    frame = {
        "offset_db": -0.75,
        "tilt_db_per_octave": -0.79,
        "pivot_hz": 2828.4,
        "n_bins": 400,
        "band_hz": [2000.0, 4000.0],
        "raw": {"rms_db": 0.4, "max_db": 0.9},
        "tilt_removed": {"rms_db": 0.18, "max_db": 0.31},
    }
    frame.update(frame_overrides)
    return {
        "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
        "tracking_band_hz": [2000.0, 4000.0],
        "frame": frame,
    }


def test_a_passing_verify_still_discloses_the_frame_it_compared_across():
    """Rung P1 — "Verified." must say how much of the agreement was frame.

    VERIFY differences an on-axis MODEL against an in-room MEASUREMENT. On the
    2026-07-29 corpus a single −0.79 dB/octave tilt between those two frames
    accounted for 84 % of the flow's apparent prediction error, so a pass with
    the frame unstated invites exactly the reading the panel had to correct.
    Surfaced on a PASS for the same reason the graded band is (#1868): the
    passing screen is the one that would otherwise overclaim.
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
    assert _run_phase(c, 3, 3)["accepted"] is True

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
    claims and must not collapse into one."""
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
    assert _run_phase(c, 3, 3)["accepted"] is True

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


def _moving_notch_cloud(notch_hz: list[float]):
    """A real ``CombinedResponse`` whose positions each carry one narrow notch
    at their own frequency — so the cross-position check has something to
    disagree about, below the registry's 4 kHz floor."""
    from jasper.audio_measurement.spatial_combine import (
        PositionCapture,
        combine_positions,
    )

    freqs = np.fft.rfftfreq(4096, 1.0 / 48_000)
    log_f = np.log2(np.maximum(freqs, 1.0))
    baseline = 1.5 * np.sin(2.0 * np.pi * log_f / 1.7)
    return combine_positions([
        PositionCapture(
            position_id=f"p{k:02d}", freqs_hz=freqs,
            magnitude_db=baseline
            - 18.0 * np.exp(-0.5 * ((log_f - np.log2(f0)) / 0.06) ** 2),
            sample_rate=48_000, ir=None,
        )
        for k, f0 in enumerate(notch_hz)
    ])


_BLIND_SPAN_RESULT = {"validity_floor_hz": 1200.0, "null_registry": {
    "classification": "insufficient_evidence", "reason": "no_corroborating_arrivals",
}}


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
            flow.CrossoverV2Conductor, "_boost_excluded_bands_hz",
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
    real_fit = flow.fit_driver_linearization

    def _spy(resp, envelope, **kwargs):
        seen.append(kwargs["vocabulary"].boost_excluded_bands_hz)
        return real_fit(resp, envelope, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "fit_driver_linearization", _spy)
        mp.setattr(
            flow.CrossoverV2Conductor, "_boost_excluded_bands_hz",
            lambda self, combined, result: ((1500.0, 1900.0),),
        )
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    assert seen and all(bands == ((1500.0, 1900.0),) for bands in seen)
