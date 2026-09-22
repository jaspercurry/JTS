# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: measurement-honesty gates, alignment/phase wiring, and measured completion."""

from __future__ import annotations

import math
import types
import pytest
from dataclasses import replace
from jasper.active_speaker.crossover_v2 import admission
from jasper.active_speaker.crossover_envelope_v2 import crossover_v2_phase
from jasper.active_speaker.crossover_v2.admission import MAX_AUTOMATIC_RETAKES_PER_POSITION
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_REVIEW,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_LOCATE_FAILED, REASON_PILOT_LEVEL_COLLAPSE, REASON_REGISTRY,
)
from jasper.active_speaker.crossover_v2.diagnostics import PILOT_SNR_UNUSABLE_DB, _worst_pilot_snr_db
from jasper.active_speaker.crossover_v2.capture_dispatch import (
    LOCATE_MIN_CONFIDENCE,
    assess,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session
from jasper.active_speaker.crossover_v2.planning import alignment_to_candidate_fields
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_OK,
    SegmentLocation,
)
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginRefused
from tests.crossover_v2_fixtures import (
    CAPS,
    FC_HZ,
    FakeSeams,
    SESSION,
    SESSION_VOLUME_DB,
    _alignment,
    _check_analysis,
    _conductor,
    _loc,
    _measure_analysis,
    _preset,
    _roles,
    _run_phase,
    _snr_analysis,
    _snr_pilot,
    _stage2_after_measure,
    _verify_analysis,
)


# --- §5.10 failure templates ------------------------------------------------------


@pytest.mark.parametrize("refused", [False, True])
@pytest.mark.parametrize("charge", ["operator", "speaker"])
def test_executor_admission_uses_only_the_runs_pose_ledger(refused, charge):
    from jasper.active_speaker.crossover_v2.admission import SlotAttempts
    from jasper.active_speaker.crossover_v2.refusal_copy import NON_RETRIABLE_CODES

    conductor = _conductor(FakeSeams())
    ledger = SlotAttempts(charge=charge, retries_per_pose=0 if charge == "speaker" else 1)
    conductor.authorize_begin(1, 1, executor_ledger=ledger)
    assert ledger.admitted == 1
    if refused:
        conductor._last_reason[conductor._slot_of_index(1)] = next(iter(NON_RETRIABLE_CODES))
        with pytest.raises(CaptureBeginRefused):
            conductor.authorize_begin(1, 2, executor_ledger=ledger)
    else:
        conductor.authorize_begin(1, 2, executor_ledger=ledger)
    assert ledger.by_household == (1 if charge == "operator" and not refused else 0)
    assert ledger.by_speaker == (1 if charge == "speaker" and not refused else 0)
    assert ledger.admitted == (1 if refused else 2)
    assert conductor._slot_attempts == {}


def test_clipped_measure_is_transient_auto_retry_with_quieter_program():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    gain_before = c.program_for_phase(PHASE_MEASURE).segment("sweep_w").gain_db

    fakes.measure = lambda program: _measure_analysis(program, clipped=True)
    verdict = _run_phase(c, 2, 2)
    assert not verdict["accepted"] and verdict["code"] == "clipped"
    assert verdict["auto_retry"] and verdict["next"] == "retake_quieter"
    assert verdict["charge"] == "speaker"
    assert verdict["evidence"]["peak_dbfs"] == -12.0
    assert verdict["attempts"]["left"] == 3
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


def test_heard_sweeps_on_schedule_are_accepted():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program, pilot_snr_ok=True,
        sweep_locations=(
            _loc("sweep_w", confidence=0.12, residual_samples=1.0),
            _loc("sweep_t", confidence=0.12),
            _loc("sweep_w_rep", confidence=0.12),
        ),
    )
    program = c.program_for_phase(PHASE_MEASURE)
    verdict = assess(fakes.measure(program), phase="measure", program=program)
    assert verdict.ok is True
    assert verdict.evidence["locate_confidence_min"] == 0.12
    assert verdict.evidence["schedule_residual_ms_worst"] == pytest.approx(1000 / program.sample_rate_hz)
    assert _run_phase(c, 2, 2)["accepted"] is True


@pytest.mark.parametrize("glitch", [False, True])
@pytest.mark.parametrize(("pilot_snr_ok", "confidence", "code"), [
    (False, 0.0298, REASON_PILOT_LEVEL_COLLAPSE),
    (True, 0.15, REASON_LOCATE_FAILED),
])
def test_buried_measure_capture_reads_too_quiet_not_glitched(glitch, pilot_snr_ok, confidence, code):
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program, pilot_snr_ok=pilot_snr_ok, glitch=glitch,
        sweep_locations=(
            _loc("pilot_woofer_lo", kind="pilot", confidence=0.5),
            _loc("pilot_woofer_hi", kind="pilot", confidence=0.6),
            *(_loc(segment, confidence=confidence, residual_samples=21.2e-3 * program.sample_rate_hz)
              for segment in ("sweep_w", "sweep_t", "sweep_w_rep")),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == code
    assert (verdict["next"], verdict["charge"]) == ("fix_and_retake", "operator")
    assert not verdict.get("auto_retry")


def test_sweep_schedule_clean_capture_passes():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


def test_sweep_schedule_boundary_exact_values_pass():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc(
                "sweep_w", confidence=LOCATE_MIN_CONFIDENCE,
                residual_samples=(
                    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * 1e-3 * program.sample_rate_hz
                ),
            ),
            _loc("sweep_t", confidence=LOCATE_MIN_CONFIDENCE),
            _loc("sweep_w_rep", confidence=LOCATE_MIN_CONFIDENCE),
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
    from jasper.active_speaker.crossover_v2.capture_dispatch import (  # lazy: avoid measurement-stack import cost on unused paths
        _stimulus_locate_ok,
    )

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


@pytest.mark.parametrize(("fault", "code", "charge", "budget"), [
    ({"glitch_detected": True}, "drift_baselines_disagree", "speaker", MAX_AUTOMATIC_RETAKES_PER_POSITION),
    ({"linearity_ok": False}, "agc_behavioral_fail", "operator", admission.MAX_EXTRA_ATTEMPTS_PER_POSITION),
])
def test_check_faults_exhaust_only_the_responsible_attempt_budget(fault, code, charge, budget):
    fakes = FakeSeams()
    fakes.check = lambda program: replace(_check_analysis(program), **fault)
    c = _conductor(fakes)
    for extra in range(budget + 1):
        verdict = _run_phase(c, 1, 1 + extra)
        assert verdict["code"] == code and verdict["charge"] == charge
        assert verdict["attempts"]["by_household"] == (extra if charge == "operator" else 0)
        assert verdict["attempts"]["by_speaker"] == (extra if charge == "speaker" else 0)
        assert verdict["attempts"]["left"] == min(
            admission.MAX_EXTRA_ATTEMPTS_PER_POSITION - verdict["attempts"]["by_household"],
            MAX_AUTOMATIC_RETAKES_PER_POSITION - extra)

    assert verdict["next"] == "stop"
    armed_before = c.armed_capture
    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, budget + 2)
    assert excinfo.value.code == code
    assert c.armed_capture == armed_before


def test_check_agc_and_snr_and_channel_map_verdicts():
    # linearity=False with ambient looking clean (snr_floor_ok defaults True)
    # ⇒ the phone's own AGC is the honest cause.
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, linearity=False)
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1)["code"] == "agc_behavioral_fail"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, snr_floor_ok=False, pilot_snr_ok=True)
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


def test_check_without_ambient_still_requires_a_level_solution():
    fakes = FakeSeams()
    fakes.check = lambda program: replace(
        _check_analysis(program, linearity=None, channel_map=None, snr_floor_ok=False),
        ambient_report={"bands": []},
    )
    verdict = _run_phase(_conductor(fakes), 1, 1)
    assert verdict["accepted"] is False
    assert verdict["code"] == "snr_floor"
    assert verdict["evidence"]["pilot_ambient"] == "unavailable"
    assert verdict["capabilities"]["level_solve"] is False
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
    c = _stage2_after_measure(fakes)
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
    c = _stage2_after_measure(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_snr_ok=False, pilot_hi_dbfs=-45.0,
    )
    assert _run_phase(c, 3, 3)["code"] == "pilot_level_collapse"
    assert c._verify_pilot_baseline is None
    # The good re-verify then establishes the baseline itself and passes.
    fakes.verify = lambda program: _verify_analysis(program, pilot_hi_dbfs=-20.0)
    assert _run_phase(c, 3, 4)["accepted"] is True


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


@pytest.mark.parametrize(("gate_ms", "code"), [(5.0, "verify_inconclusive"), (8.0, "verify_level_shift")])
def test_verify_gate_comparability_precedes_pilot_transfer(gate_ms, code):
    c = _conductor(FakeSeams())
    program = c.program_for_phase(PHASE_VERIFY)
    c._measure_gate_window_ms = 8.0
    assert c._verify_verdict(_verify_analysis(program, gate_ms=8.0, pilot_hi_dbfs=-20.0)).accepted
    verdict = c._verify_verdict(_verify_analysis(program, gate_ms=gate_ms, pilot_hi_dbfs=-19.0))
    assert not verdict.accepted and verdict.code == code
    assert c.verify_outcome == "inconclusive"
    assert ("pilot_transfer_step_db" in verdict.evidence) is (gate_ms == 8.0)


def test_verify_out_of_tolerance_and_inconclusive():
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)
    verdict = _run_phase(c, 3, 3)
    assert c.verify_code == "verify_out_of_tolerance"
    assert c.verify_outcome == "fail"

    fakes.verify = lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0)
    verdict = _run_phase(c, 3, 4)
    assert verdict["code"] == "verify_inconclusive"
    assert c.verify_outcome == "inconclusive"

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


@pytest.mark.parametrize("phases", [(PHASE_CHECK, PHASE_MEASURE), (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY)])
def test_measure_accept_finishes_measured_without_publishing_a_candidate(phases):
    fakes = FakeSeams()
    conductor = _conductor(fakes, index_phase_map=dict(enumerate(phases, 1)))
    _run_phase(conductor, 1, 1)
    verdict = _run_phase(conductor, 2, 1)

    assert verdict["accepted"] is True
    assert verdict["next"] == "accept"
    assert conductor.current_phase == PHASE_REVIEW
    assert crossover_v2_phase({
        "accepted_phases": list(conductor.accepted_phases),
        "session_phases": phases, "applied": conductor.applied,
    }, review_declined=False) == PHASE_REVIEW
    assert conductor.candidate is None
    assert conductor.applied is False
    assert fakes.published_candidates == []
