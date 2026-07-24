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
import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2_flow import (
    ALIGNMENT_CONFIDENCE_TRUST_FLOOR,
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_ON_APPLY,
    AUTO_ADVANCE_TAP,
    CAPTURE_ENTRY_MARGIN_MS,
    CAPTURE_PLAN_TARGET,
    COURTESY_PRELUDE_ENABLED,
    GAIN_CAP_BACKOFF_DB,
    LINEARIZATION_MIN_PAIRED_OCCURRENCES,
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    MEASURE_PREDICTED_RIPPLE_CEILING_DB,
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_VERIFY,
    PILOT_LEVEL_DELTA_DB,
    REASON_REGISTRY,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    VERIFY_PILOT_TRANSFER_STEP_CEILING_DB,
    _SIGMA_TOLERABLE_DB,
    CrossoverV2Conductor,
    CrossoverV2FlowError,
    V2FlowSeams,
    _analysis_json,
    _compose_sigma_db,
    _program_duration_ms,
    abandon_measurement_volume,
    alignment_delay_search_bounds_us,
    alignment_to_candidate_fields,
    back_off_gain,
    build_v2_capture_plan,
    build_v2_session_spec,
    build_v2_verify_capture_plan,
    open_measurement_volume,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import KIND_COURTESY_TONE, RoleBand
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_OK,
    AlignmentEstimate,
    CrossoverCandidate,
    DriftEstimate,
    DriverResponse,
    GainPlan,
    PilotObservation,
    ProgramAnalysis,
    SegmentLocation,
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


def _driver_response(role: str, window_ms: float) -> DriverResponse:
    freqs = np.linspace(100.0, 20000.0, 64)
    return DriverResponse(
        role=role, freqs_hz=freqs, magnitude_db=np.zeros(64),
        complex_tf=np.ones(64, dtype=complex),
        gating={"applied": True, "window_ms": window_ms},
        snr=None, validity_floor_hz=None,
    )


_LINEARIZABLE_FREQS_HZ = np.linspace(100.0, 20000.0, 2048)


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
) -> AlignmentEstimate:
    return AlignmentEstimate(
        delay_us=delay_us, raw_delay_us=delay_us, parallax_us=11.0,
        polarity=polarity, polarity_sign=1 if polarity == "normal" else -1,
        polarity_agrees_with_sum=True, confidence=confidence, status=status,
    )


def _measure_analysis(
    program, *, glitch=False, clipped=False, linearity=True,
    alignment=None, locate_confidence=0.9, gate_ms=8.0,
    predicted_ripple_db=0.8, sweep_locations=None,
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


def _verify_analysis(
    program, *, max_db=0.9, gate_ms=8.5, linearity=True, locate_confidence=0.9,
    pilot_hi_dbfs=None, programmed_hi_gain_db=-20.0,
) -> ProgramAnalysis:
    return ProgramAnalysis(
        phase="verify",
        program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep", confidence=locate_confidence),),
        summed_response=_driver_response("summed", gate_ms),
        summed_ripple_db=1.1,
        # W6.7 ruling 1: the conductor gates on the notch-excluded max, not the
        # raw ``max_db`` — this fake keeps them equal (a fake with no notch to
        # exclude), so the ``max_db`` parameter still controls the gate.
        verify_tracking={"rms_db": 0.4, "max_db": max_db, "max_db_notch_excluded": max_db},
        linearity_ok=linearity,
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

    def seams(self) -> V2FlowSeams:
        def analyze(program, result, priors, geometry):
            self.analyzed.append((program.phase, result, priors, geometry))
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
        )


def _conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Conductor:
    return CrossoverV2Conductor(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
        **kwargs,
    )


def _capture() -> CaptureResult:
    return CaptureResult(wav=b"fake-wav")


def _run_phase(conductor, index, attempt) -> dict:
    conductor.authorize_begin(index, attempt)
    conductor.on_armed()
    return conductor.consume_capture(index, attempt, _capture())


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
    # Owner ruling (2026-07-20): a trusted candidate tells the HOST to fire
    # the auto-apply immediately — no human review step in between.
    assert verdict["auto_apply"] is True
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
    assert verdict["auto_apply"] is True


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
    assert verdict["auto_apply"] is True
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


# --- measurement-honesty gate G1: predicted-ripple sanity ceiling -----------------


def test_predicted_ripple_ceiling_rejects_measure_reusing_low_alignment_confidence():
    """Measurement-honesty gate G1 (2026-07-22): a candidate whose OWN
    predicted ripple is implausibly bad — mirrors the 2026-07-22 corrupted-
    phone-chain hardware evidence (27.316 dB at a confidence that cleared
    ALIGNMENT_CONFIDENCE_TRUST_FLOOR) — must not auto-apply even though
    confidence and the Fix 3 plausibility check both pass. Reuses
    low_alignment_confidence (same household action, "measure again"); the
    diag ``guard`` field disambiguates it from the other two checks sharing
    that code in telemetry."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=27.316,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is False
    assert verdict["code"] == "low_alignment_confidence"
    assert not fakes.published_candidates
    assert c.candidate is None
    assert c.current_phase == PHASE_MEASURE


def test_predicted_ripple_well_under_ceiling_passes():
    """The 2026-07-22 clean-corpus worst case (12 captures, UMIK-2 +
    iMM-6C, 4.387-9.031 dB) passes cleanly — the ceiling sits well above it."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=9.0,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


def test_predicted_ripple_ceiling_boundary_exact_passes_just_above_fires():
    """The ceiling is an exclusive upper bound (``>``, not ``>=``) — exactly
    at the ceiling passes, matching this file's other boundary comparators
    (e.g. test_alignment_confidence_at_the_trust_floor_is_trusted)."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=MEASURE_PREDICTED_RIPPLE_CEILING_DB,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True

    fakes2 = FakeSeams()
    fakes2.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=MEASURE_PREDICTED_RIPPLE_CEILING_DB + 0.01,
    )
    c2 = _conductor(fakes2)
    _run_phase(c2, 1, 1)
    verdict2 = _run_phase(c2, 2, 2)
    assert verdict2["accepted"] is False
    assert verdict2["code"] == "low_alignment_confidence"


def test_predicted_ripple_ceiling_skips_when_no_alignment():
    """A trims-only candidate (no alignment estimate at all) is never
    ripple-gated — same condition the confidence floor and Fix 3 use (see
    test_no_alignment_estimate_skips_the_confidence_gate)."""
    from dataclasses import replace

    fakes = FakeSeams()
    fakes.measure = lambda program: replace(
        _measure_analysis(program, predicted_ripple_db=27.316), alignment=None,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


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
    phase, seen_result, _priors, geometry = fakes.analyzed[0]
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


def test_sweep_schedule_fires_on_low_confidence_even_with_small_residual():
    """The CONFIDENCE half of the gate — mirrors the 2026-07-22 xrun
    evidence's 0.07-0.12 per-segment confidence, here with a negligible
    residual so only the confidence floor is exercised. 0.12 clears
    LOCATE_MIN_CONFIDENCE (0.1, the pre-existing ``_stimulus_locate_ok``
    check earlier in the ladder) but is still under
    SWEEP_LOCATE_CONFIDENCE_FLOOR (0.3), so G2 alone is what fires here."""
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
    assert verdict["code"] == "drift_baselines_disagree"
    assert verdict["template"] == "silent_auto_retry"


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


def test_locate_failed_and_budget_exhaustion():
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, locate_confidence=0.01)
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "locate_failed"
    assert verdict["template"] == "fix_and_retry"
    # Budget 1 ⇒ one retry admitted, then the third begin is refused.
    verdict = _run_phase(c, 1, 2)
    assert verdict["code"] == "locate_failed"
    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, 3)
    assert excinfo.value.code == "locate_failed"


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


def test_verify_pilot_transfer_baseline_rehydrates_from_a_prior_session():
    """A verify-only re-arm session (``prepare_v2_verify``) supplies the
    PRIOR session's frozen baseline through the constructor — exactly like
    ``measure_gate_window_ms`` (see ``__init__``'s comment). This conductor's
    OWN first VERIFY attempt then compares against the SUPPLIED baseline
    rather than treating itself as attempt 1."""
    fakes = FakeSeams()
    c = CrossoverV2Conductor(
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
        verify_pilot_transfer_baseline={"summed": -20.0},
    )
    assert c.verify_pilot_transfer_baseline == {"summed": -20.0}
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.56, max_db=0.5,
    )
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_level_shift"
    # The supplied baseline is untouched — this attempt did not overwrite it.
    assert c.verify_pilot_transfer_baseline == {"summed": -20.0}


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


# --- capture plan (auto-advance policy, §5.2/§5.7) ---------------------------------


def test_capture_plan_entries_carry_auto_advance_policy():
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    assert plan.schema_version == 2
    assert plan.capture_target == CAPTURE_PLAN_TARGET
    kinds = [entry.kind_label for entry in plan.entries]
    assert kinds == ["check", "measure", "verify"]
    assert [entry.index for entry in plan.entries] == [0, 1, 2]
    check, measure, verify = plan.entries
    # One tap per session: CHECK is the tap; MEASURE auto-advances behind a
    # visible cancelable countdown; VERIFY arms on apply.
    assert check.screen["auto_advance"] == AUTO_ADVANCE_TAP
    assert measure.screen["auto_advance"] == AUTO_ADVANCE_COUNTDOWN
    assert measure.screen["cancelable"] == "1"
    assert int(measure.screen["countdown_s"]) > 0
    assert verify.screen["auto_advance"] == AUTO_ADVANCE_ON_APPLY
    # Durations are per-entry (heterogeneous) and positive.
    assert all(entry.duration_ms > 0 for entry in plan.entries)
    assert len({entry.duration_ms for entry in plan.entries}) > 1


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
    check, measure, verify = plan.entries

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
        "play_wav", "readmit", "writer_lock",
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
    assert spec.capture_plan.capture_target == CAPTURE_PLAN_TARGET
    # Round-trips through the strict boundary validation.
    from jasper.capture_relay.spec import CaptureSpec

    reparsed = CaptureSpec.from_dict(spec.to_dict())
    assert reparsed.capture_plan.entries == spec.capture_plan.entries


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
) -> DriverResponse:
    freqs = np.linspace(100.0, 20000.0, 64)
    snr = (
        {"worst_relevant": {"estimated_snr_db": snr_db, "verdict": snr_verdict}}
        if snr_db is not None else None
    )
    return DriverResponse(
        role=role, freqs_hz=freqs, magnitude_db=np.zeros(64),
        complex_tf=np.ones(64, dtype=complex),
        gating={"applied": True, "window_ms": window_ms},
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


def test_measure_diag_logs_guard_field_on_ripple_ceiling_fire(caplog):
    """The diag ``guard`` field distinguishes a G1 fire from the two
    pre-existing checks (confidence floor, Fix 3 plausibility) that share
    the SAME reused low_alignment_confidence code — see the two tests
    above for the "guard empty" counterpart on each of those."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=27.316,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "low_alignment_confidence"
    assert "event=correction.crossover_v2_measure_diag" in caplog.text
    assert "guard=ripple_ceiling" in caplog.text
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


def _eligible_measure_analysis(
    program, *, mic_tier="reference", woofer_repeats=2, tweeter_repeats=2,
    woofer_db=None, tweeter_db=None, trim_db=None,
) -> ProgramAnalysis:
    freqs = _LINEARIZABLE_FREQS_HZ
    if woofer_db is None:
        woofer_db = np.zeros_like(freqs)
    if tweeter_db is None:
        # A +6 dB bump inside the [800, 3200] Hz overlap band (Fc=1600) —
        # validated offline (PR-C sanity pass) to survive envelope/fit and
        # move the re-solved trim measurably vs the raw candidate.
        tweeter_db = 6.0 * np.exp(-0.5 * ((np.log2(freqs / 1500.0) / 0.25) ** 2))
    if trim_db is None:
        trim_db = {"woofer": 0.0, "tweeter": -2.211}
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
        ),
        linearity_ok=True,
        predicted_sum=(freqs, np.zeros_like(freqs)),
        glitch_detected=False,
    )


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
    assert c.candidate.role_attenuations_db == {"woofer": 0.0, "tweeter": -2.211}
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
    assert c.candidate.role_attenuations_db == {"woofer": 0.0, "tweeter": -2.211}
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
    raw_trim = {"woofer": 0.0, "tweeter": -2.211}
    assert candidate.role_attenuations_db != raw_trim
    # The bump correction quiets the tweeter's overlap-band level, so the
    # RESOLVED tweeter trim needs LESS attenuation than the raw solve did
    # (moves toward 0, i.e. strictly greater than the raw -2.211).
    assert candidate.role_attenuations_db["tweeter"] > raw_trim["tweeter"]

    assert set(candidate.linearization) == {"woofer", "tweeter"}
    tweeter_fit = candidate.linearization["tweeter"]
    assert tweeter_fit["filters"], "expected the tweeter bump to attract a filter"
    assert all(f["gain"] <= 0.0 for f in tweeter_fit["filters"])
    for role_fit in candidate.linearization.values():
        assert role_fit["mic_tier"] == "reference"
        assert role_fit["n_repeats"] == 2
        # No caller populates driver_class_by_role yet (#1665 not landed) —
        # every role fits under the conservative "unknown" default.
        assert role_fit["driver_class"] == "unknown"


def test_driver_class_by_role_ctor_param_threads_into_the_fit():
    """The optional driver_class_by_role ctor param (default None -> every
    role "unknown") is a forward-looking seam for #1665's component-entry
    declarations — no production caller populates it yet, but the wiring
    itself must work when a caller does."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes, driver_class_by_role={"tweeter": "compression_horn"})
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization["tweeter"]["driver_class"] == "compression_horn"
    # The woofer wasn't named in the override -> stays "unknown".
    assert c.candidate.linearization["woofer"]["driver_class"] == "unknown"


def test_wild_resolved_trim_falls_back_to_raw_with_warning(caplog):
    """If the re-solved trim lands implausibly far (> 6 dB) from the raw
    solve, the conductor must distrust it and fall back to the raw trim —
    logging a WARNING, never silently swapping in a wild value."""
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    # A deliberately-inconsistent "raw" trim (nothing derives the resolved
    # trim from this value — it's the fixture's own declared candidate —
    # so setting it far from what the actual responses justify is exactly
    # how to trigger the sanity backstop deterministically).
    wild_raw_trim = {"woofer": 0.0, "tweeter": -20.0}
    fakes.measure = lambda program: _eligible_measure_analysis(program, trim_db=wild_raw_trim)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == wild_raw_trim
    assert "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    assert LINEARIZATION_TRIM_SANITY_MARGIN_DB > 0  # sanity: the constant exists and is positive
    # linearization itself still gets reported — only the trim falls back.
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}


def test_wild_trim_boundary_exact_passes_just_above_falls_back():
    """The sanity margin is an exclusive upper bound (matches this file's
    other boundary comparators, e.g.
    test_predicted_ripple_ceiling_boundary_exact_passes_just_above_fires):
    exactly at the margin is trusted, one hair over falls back."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    resolved_tweeter = c.candidate.role_attenuations_db["tweeter"]

    # Exactly at the margin from the resolved value -> still trusted.
    at_margin_raw = {
        "woofer": 0.0,
        "tweeter": resolved_tweeter - LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    }
    fakes2 = FakeSeams()
    fakes2.measure = lambda program: _eligible_measure_analysis(program, trim_db=at_margin_raw)
    c2 = _conductor(fakes2)
    _run_phase(c2, 1, 1)
    _run_phase(c2, 2, 2)
    assert c2.candidate.role_attenuations_db != at_margin_raw  # resolved value used, not raw

    # A hair past the margin -> falls back to raw.
    past_margin_raw = {
        "woofer": 0.0,
        "tweeter": resolved_tweeter - LINEARIZATION_TRIM_SANITY_MARGIN_DB - 0.5,
    }
    fakes3 = FakeSeams()
    fakes3.measure = lambda program: _eligible_measure_analysis(program, trim_db=past_margin_raw)
    c3 = _conductor(fakes3)
    _run_phase(c3, 1, 1)
    _run_phase(c3, 2, 2)
    assert c3.candidate.role_attenuations_db == past_margin_raw


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

    def _boom(analysis, cand):
        raise ValueError("simulated fit engine bug")

    monkeypatch.setattr(c, "_fit_linearization", _boom)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == {"woofer": 0.0, "tweeter": -2.211}
    assert c.candidate.linearization == {}
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

    def _boom(analysis, cand):
        raise RuntimeError("linearization fit emitted a boost")

    monkeypatch.setattr(c, "_fit_linearization", _boom)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == {"woofer": 0.0, "tweeter": -2.211}
    assert c.candidate.linearization == {}
    assert "reason=RuntimeError" in caplog.text
    assert "linearization=fit_failed" in caplog.text


def test_measure_diag_linearization_field_fitted(caplog):
    """SF3: the fitted outcome."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_measure_diag" in caplog.text
    assert "linearization=fitted" in caplog.text


def test_measure_diag_linearization_field_ineligible_mic_tier(caplog):
    """SF3: the ineligible_mic_tier outcome."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier="consumer")
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "linearization=ineligible_mic_tier" in caplog.text


def test_measure_diag_linearization_field_ineligible_repeats(caplog):
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


def test_measure_diag_linearization_field_trim_rejected(caplog):
    """SF3: the trim_rejected outcome (fit succeeded, but the resolved trim
    was implausible and fell back to raw -- distinct from "fitted" even
    though linearization is populated in both)."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    wild_raw_trim = {"woofer": 0.0, "tweeter": -20.0}
    fakes.measure = lambda program: _eligible_measure_analysis(program, trim_db=wild_raw_trim)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "linearization=trim_rejected" in caplog.text


def test_measure_diag_linearization_field_empty_when_verdict_rejected_before_candidate(
    caplog,
):
    """SF3: a MEASURE verdict rejected before _build_candidate ever runs
    (here, the pre-existing glitch check) must log linearization="" -- never
    a stale value from a prior attempt, and never a guess about a path that
    was never taken. Mirrors the `guard` field's own empty-on-reject
    convention."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program, glitch=True)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is False
    assert 'linearization=""' in caplog.text
