# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 3b's four verdicts, and the adoption table those became.

Four axes since #2602 and seven rows since #2656; #2537 built the first three
and five of them.

Every row of the adoption table is a named test here, and so is every override
rule stated beside it: safety outranks trust, trust outranks quality, a failed
restore outranks everything, a measured regression never keeps, a boost is
invisible on a trusted round, and a restore that cannot be performed escalates
instead of quietly keeping the graph.

The directionality pin is the one to read first
(``test_the_level_shift_hard_stop_is_directional``): the same magnitude of
uncommanded level shift is a hard stop in one direction and a learning signal
in the other, and getting that backwards is what reverted a measured, safe,
improving candidate on 2026-08-15.
"""

from __future__ import annotations

import inspect
import itertools
import ast
import re
import types
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.contracts import (
    ADOPTION_ROW_KEEP,
    ADOPTION_ROW_KEEP_FOR_ITERATION,
    ADOPTION_ROW_KEEP_ITERATING,
    ADOPTION_ROW_KEEP_MISSED_EXHAUSTED,
    ADOPTION_ROW_RESTORE_FAILED,
    ADOPTION_ROW_RESTORE_REGRESSION,
    ADOPTION_ROW_RESTORE_UNSAFE,
    ADOPTION_ROW_RESTORE_UNTRUSTED,
    ADOPTION_ROWS,
    AdoptionOutcome,
    BenefitStatus,
    CaptureValidity,
    CrossoverV2ContractError,
    EvidenceTrust,
    IterationHeadroom,
    QualityStatus,
    RealizationStatus,
    ResponseCurve,
    SafetyStatus,
    SpecStatus,
)
from jasper.active_speaker.delta_probe import (
    REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL as DELTA_VERDICT_LEVEL_SHORTFALL,
    VERDICT_LEVEL_MISMATCH as DELTA_VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED as DELTA_VERDICT_MATCHED,
    VERDICT_MODEL_ERROR as DELTA_VERDICT_MODEL_ERROR,
    VERDICT_SPATIALLY_COSTLY as DELTA_VERDICT_SPATIALLY_COSTLY,
)
from jasper.active_speaker.crossover_v2 import verification as verification_module
from jasper.active_speaker.crossover_v2.verification import (
    ADOPTION_MEASURED_REGRESSION,
    ADOPTION_NO_ROLLBACK_ANCHOR,
    ADOPTION_RESTORE_FAILED,
    ADOPTION_UNPROVEN_BOOST,
    BENEFIT_BASELINE_UNAVAILABLE,
    BENEFIT_GRID_MISMATCH,
    BENEFIT_IMPROVED,
    BENEFIT_MARK_MISMATCH,
    BENEFIT_MASK_MISMATCH,
    BENEFIT_POST_UNAVAILABLE,
    BENEFIT_PROGRAM_MISMATCH,
    BENEFIT_REGRESSED,
    BENEFIT_RESIDUAL_UNEVALUABLE,
    BENEFIT_WITHIN_MARGIN,
    CAPTURE_INTEGRITY_CLEAN,
    CAPTURE_INTEGRITY_FAILED,
    CAPTURE_INTEGRITY_UNAVAILABLE,
    ADOPTION_REALIZATION_FAILED,
    ADOPTION_REALIZATION_UNAVAILABLE,
    ADOPTION_REALIZED_AND_IMPROVED,
    ADOPTION_UNPROVEN,
    CLIPPED_RUN_CHECK,
    HEADROOM_CAP_REACHED,
    HEADROOM_NO_OBJECTIVES,
    HEADROOM_PLATEAUED,
    HEADROOM_REACHABLE,
    HEADROOM_WITHIN_PLATEAU,
    FlatnessObjectives,
    evaluate_iteration_headroom,
    flatness_objectives,
    REALIZATION_COMPARAND,
    REALIZATION_NO_COMPARATOR,
    REALIZATION_NO_TRACKING,
    REALIZATION_OUT_OF_TOLERANCE,
    REALIZATION_WITHIN_TOLERANCE,
    SAFETY_BOOST_OVER_DECLARED_BOUND,
    SAFETY_CLIPPED_CAPTURE,
    SAFETY_NO_FINDING,
    SAFETY_NO_FINDING_UNMEASURED,
    SAFETY_UNCOMMANDED_LEVEL_LOUDER,
    SPEC_BAND_OUT_OF_TOLERANCE,
    SPEC_IN_TOLERANCE,
    SPEC_NO_EVALUABLE_BAND,
    SPEC_NO_REPORT,
    SPEC_PARTIAL_COVERAGE,
    TRACKING_COMPARATOR_KEY,
    TRUST_MEASURED,
    MeasurementComparand,
    Verdict,
    _PASSED_ROWS,
    _QUALITY_ROWS,
    _QUALITY_TABLE,
    decide_adoption,
    evaluate_applied_safety,
    evaluate_benefit,
    evaluate_capture_validity,
    evaluate_evidence_trust,
    evaluate_realization,
    evaluate_round_quality,
    evaluate_spec,
    verification_result,
)
from jasper.active_speaker.flat_spec import (
    BandResult,
    FlatSpecReport,
    evaluate_flat_spec,
    spec_band_tilt,
    spec_convergence_residual,
)
from jasper.audio_measurement.program_analysis import (
    INTEGRITY_FAIL,
    INTEGRITY_NOT_EVALUATED,
    INTEGRITY_PASS,
    CaptureIntegrity,
    IntegrityCheck,
)

MARGIN_DB = 0.5
TOLERANCE_DB = 1.5


# --------------------------------------------------------------------------
# fixtures / builders
# --------------------------------------------------------------------------


def _axis(n: int = 192) -> tuple[float, ...]:
    """A strictly-ascending log axis spanning every spec band."""

    return tuple(float(f) for f in np.geomspace(200.0, 18000.0, n))


def _ripple(axis: tuple[float, ...], amplitude_db: float) -> tuple[float, ...]:
    """A deterministic ripple whose RMS scales with ``amplitude_db``."""

    return tuple(float(amplitude_db * np.sin(3.0 * np.log(f))) for f in axis)


def _comparand(
    *,
    amplitude_db: float = 1.0,
    program_id: str = "prog-a",
    reference_mark: str = "mark-1",
    axis: tuple[float, ...] | None = None,
    db: tuple[float, ...] | None = None,
    mask: tuple[bool, ...] | None = None,
) -> MeasurementComparand:
    grid = _axis() if axis is None else axis
    levels = _ripple(grid, amplitude_db) if db is None else db
    return MeasurementComparand(
        program_id=program_id,
        reference_mark=reference_mark,
        curve=ResponseCurve(grid, levels),
        exclusion_mask=mask,
    )


def _report(
    *, amplitude_db: float = 0.2, mask: np.ndarray | None = None
) -> FlatSpecReport:
    grid = np.asarray(_axis(), dtype=np.float64)
    return evaluate_flat_spec(grid, np.asarray(_ripple(_axis(), amplitude_db)), mask)


def _integrity(*, failed: tuple[str, ...] = (), not_evaluated: tuple[str, ...] = ()):
    checks = [IntegrityCheck(name="summed_sweep_heard", status=INTEGRITY_PASS)]
    checks += [IntegrityCheck(name=name, status=INTEGRITY_FAIL) for name in failed]
    checks += [
        IntegrityCheck(name=name, status=INTEGRITY_NOT_EVALUATED, reason="no repeat")
        for name in not_evaluated
    ]
    return CaptureIntegrity(checks=tuple(checks))


# --------------------------------------------------------------------------
# 1. capture validity
# --------------------------------------------------------------------------


def test_a_missing_integrity_record_is_unusable_because_none_never_means_clean():
    """``None`` is "no evidence" everywhere in program_analysis, so it fails closed."""

    verdict = evaluate_capture_validity(None)
    assert verdict.status is CaptureValidity.UNUSABLE
    assert verdict.reason == CAPTURE_INTEGRITY_UNAVAILABLE


def test_a_failed_integrity_check_makes_the_capture_unusable_and_names_it():
    verdict = evaluate_capture_validity(_integrity(failed=("clipped_run",)))
    assert verdict.status is CaptureValidity.UNUSABLE
    assert verdict.reason == CAPTURE_INTEGRITY_FAILED
    assert verdict.evidence["failed"] == ["clipped_run"]


def test_a_record_of_only_not_evaluated_checks_stays_usable():
    """The shipped rule is ``comparable = not integrity.failed``.

    Tightening it here would discard rounds the attempts ledger already
    counts, so an unexamined check is disclosed, not fatal.
    """

    verdict = evaluate_capture_validity(_integrity(not_evaluated=("repeat_epsilon",)))
    assert verdict.status is CaptureValidity.USABLE
    assert verdict.reason == CAPTURE_INTEGRITY_CLEAN
    assert verdict.evidence["not_evaluated"] == ["repeat_epsilon"]


def test_capture_validity_agrees_with_the_shipped_glitched_property():
    for failed in ((), ("clipped_run",)):
        integrity = _integrity(failed=failed)
        unusable = evaluate_capture_validity(integrity).status is CaptureValidity.UNUSABLE
        assert unusable == integrity.glitched


# --------------------------------------------------------------------------
# 2. realization
# --------------------------------------------------------------------------


def test_the_incidents_tracking_number_still_matches():
    """1.291 dB against a 1.5 dB tolerance — the 2026-08-10 round.

    Realization was never the wrong answer; it was the only answer. This
    pins that the comparator is unchanged, so the fix is the *separation*
    and not a quietly re-tuned gate.
    """

    verdict = evaluate_realization(
        tracking={TRACKING_COMPARATOR_KEY: 1.291}, tolerance_db=TOLERANCE_DB
    )
    assert verdict.status is RealizationStatus.MATCHED
    assert verdict.reason == REALIZATION_WITHIN_TOLERANCE
    assert verdict.evidence["deviation_db"] == pytest.approx(1.291)


def test_tracking_beyond_the_tolerance_fails():
    verdict = evaluate_realization(
        tracking={TRACKING_COMPARATOR_KEY: 1.51}, tolerance_db=TOLERANCE_DB
    )
    assert verdict.status is RealizationStatus.FAILED
    assert verdict.reason == REALIZATION_OUT_OF_TOLERANCE


def test_absent_tracking_is_unavailable_and_not_failed():
    """The named distinction. The deployed gate collapses these two."""

    verdict = evaluate_realization(tracking=None, tolerance_db=TOLERANCE_DB)
    assert verdict.status is RealizationStatus.UNAVAILABLE
    assert verdict.status is not RealizationStatus.FAILED
    assert verdict.reason == REALIZATION_NO_TRACKING


@pytest.mark.parametrize(
    "value",
    [None, "1.2", True, False, float("nan"), float("inf")],
    ids=["missing", "string", "true", "false", "nan", "inf"],
)
def test_a_comparator_that_is_not_a_real_number_is_unavailable_not_failed(value):
    """``True`` is an ``int``; grading it as 1.0 dB would be a silent pass."""

    tracking = {} if value is None else {TRACKING_COMPARATOR_KEY: value}
    verdict = evaluate_realization(tracking=tracking, tolerance_db=TOLERANCE_DB)
    assert verdict.status is RealizationStatus.UNAVAILABLE
    assert verdict.reason == REALIZATION_NO_COMPARATOR


@pytest.mark.parametrize("tolerance", [0.0, -1.0, float("nan"), "1.5", True])
def test_the_realization_tolerance_must_be_a_positive_number(tolerance):
    with pytest.raises(CrossoverV2ContractError):
        evaluate_realization(
            tracking={TRACKING_COMPARATOR_KEY: 1.0}, tolerance_db=tolerance
        )


def test_realization_grades_the_same_key_the_flow_grades():
    """The comparator name is shared, not re-chosen."""

    from jasper.active_speaker import crossover_v2_flow

    assert TRACKING_COMPARATOR_KEY == crossover_v2_flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED


@pytest.mark.parametrize(
    "measured, status",
    [
        pytest.param(1.291, RealizationStatus.MATCHED, id="matched"),
        pytest.param(1.51, RealizationStatus.FAILED, id="failed"),
        pytest.param("1.2", RealizationStatus.UNAVAILABLE, id="no_comparator"),
    ],
)
def test_every_graded_realization_verdict_names_its_comparand(measured, status):
    """#3483: the verdict says HOW it compared and must say WHAT against.

    Two rounds returned composed ``realization = matched`` beside delta-probe
    band ratios of 0.4877 and 2.4309 on the same receipt. They do not
    contradict each other: this axis grades the measured VERIFY sum against the
    applied candidate's own PREDICTED sum, while those ratios grade realized
    against COMMANDED delta (ADR-0209's realized-vs-commanded claim). Nothing
    on the record said so, so two honest readers of one receipt reached
    opposite conclusions.
    """

    verdict = evaluate_realization(
        tracking={TRACKING_COMPARATOR_KEY: measured}, tolerance_db=TOLERANCE_DB
    )
    assert verdict.status is status
    assert verdict.evidence["comparand"] == REALIZATION_COMPARAND
    # The comparand is WHAT was compared against; the comparator is the
    # reduction over it. Two facts, two keys — a receipt carrying only the
    # second names a statistic and not a comparison.
    assert verdict.evidence["comparator"] == TRACKING_COMPARATOR_KEY
    assert REALIZATION_COMPARAND != TRACKING_COMPARATOR_KEY


# --------------------------------------------------------------------------
# 3. measured benefit
# --------------------------------------------------------------------------


def test_a_flatter_post_measurement_is_improved():
    verdict = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=3.0),
        post=_comparand(amplitude_db=0.5),
        margin_db=MARGIN_DB,
    )
    assert verdict.status is BenefitStatus.IMPROVED
    assert verdict.reason == BENEFIT_IMPROVED
    assert verdict.evidence["improvement_db"] > MARGIN_DB


def test_a_worse_post_measurement_is_regressed():
    verdict = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=0.5),
        post=_comparand(amplitude_db=3.0),
        margin_db=MARGIN_DB,
    )
    assert verdict.status is BenefitStatus.REGRESSED
    assert verdict.reason == BENEFIT_REGRESSED


def test_a_change_the_instrument_cannot_resolve_is_indeterminate_not_a_small_win():
    verdict = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=3.0),
        post=_comparand(amplitude_db=2.9),
        margin_db=MARGIN_DB,
    )
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_WITHIN_MARGIN
    assert 0.0 < verdict.evidence["improvement_db"] < MARGIN_DB


def test_the_benefit_metric_is_the_shipped_pooled_spec_residual():
    """Composed, not invented: the numbers equal flat_spec's own.

    If this module ever grew its own residual, this test is where the two
    would part company.
    """

    baseline = _comparand(amplitude_db=3.0)
    post = _comparand(amplitude_db=0.5)
    verdict = evaluate_benefit(
        entry_baseline=baseline, post=post, margin_db=MARGIN_DB
    )
    expected = {
        name: spec_convergence_residual(
            evaluate_flat_spec(
                np.asarray(side.curve.hz),
                np.asarray(side.curve.db),
                np.asarray(side.exclusion_mask, dtype=bool),
            )
        ).rms_db
        for name, side in (("baseline", baseline), ("post", post))
    }
    assert verdict.evidence["baseline_residual_db"] == pytest.approx(
        expected["baseline"]
    )
    assert verdict.evidence["post_residual_db"] == pytest.approx(expected["post"])
    assert verdict.evidence["improvement_db"] == pytest.approx(
        expected["baseline"] - expected["post"]
    )


@pytest.mark.parametrize(
    ("baseline", "post", "reason"),
    [
        (None, _comparand(), BENEFIT_BASELINE_UNAVAILABLE),
        (_comparand(), None, BENEFIT_POST_UNAVAILABLE),
        (None, None, BENEFIT_BASELINE_UNAVAILABLE),
    ],
    ids=["no-baseline", "no-post", "neither"],
)
def test_a_missing_side_is_indeterminate_with_its_own_reason(baseline, post, reason):
    """The 2026-08-10 run had no comparable baseline and reported success."""

    verdict = evaluate_benefit(
        entry_baseline=baseline, post=post, margin_db=MARGIN_DB
    )
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"program_id": "prog-b"}, BENEFIT_PROGRAM_MISMATCH),
        ({"reference_mark": "mark-2"}, BENEFIT_MARK_MISMATCH),
        ({"axis": _axis(191)}, BENEFIT_GRID_MISMATCH),
        ({"mask": (True,) + (False,) * (len(_axis()) - 1)}, BENEFIT_MASK_MISMATCH),
    ],
    ids=["program", "reference-mark", "grid", "mask"],
)
def test_incomparable_evidence_refuses_to_claim_a_direction(overrides, reason):
    """Comparability is checked, never assumed — with a typed reason."""

    verdict = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=3.0),
        post=_comparand(amplitude_db=0.5, **overrides),
        margin_db=MARGIN_DB,
    )
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == reason


def test_the_comparability_reason_names_the_root_difference():
    """A different program makes a different grid uninteresting."""

    verdict = evaluate_benefit(
        entry_baseline=_comparand(),
        post=_comparand(program_id="prog-b", axis=_axis(191)),
        margin_db=MARGIN_DB,
    )
    assert verdict.reason == BENEFIT_PROGRAM_MISMATCH


def test_a_curve_the_evaluator_cannot_grade_is_indeterminate_rather_than_raising():
    """``evaluate_flat_spec`` raises on a descending axis; a household
    decision must not inherit a traceback."""

    descending = tuple(reversed(_axis()))
    verdict = evaluate_benefit(
        entry_baseline=_comparand(axis=descending, db=_ripple(descending, 3.0)),
        post=_comparand(axis=descending, db=_ripple(descending, 0.5)),
        margin_db=MARGIN_DB,
    )
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_RESIDUAL_UNEVALUABLE


def test_a_fully_excluded_spectrum_is_indeterminate():
    axis = _axis()
    mask = (True,) * len(axis)
    verdict = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=3.0, mask=mask),
        post=_comparand(amplitude_db=0.5, mask=mask),
        margin_db=MARGIN_DB,
    )
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_RESIDUAL_UNEVALUABLE


@pytest.mark.parametrize("margin", [0.0, -0.5, float("inf"), "0.5", True])
def test_the_benefit_margin_must_be_a_positive_number(margin):
    with pytest.raises(CrossoverV2ContractError):
        evaluate_benefit(
            entry_baseline=_comparand(), post=_comparand(), margin_db=margin
        )


# -- cheap properties -------------------------------------------------------


@pytest.mark.parametrize("amplitudes", [(3.0, 0.5), (0.5, 3.0), (2.0, 1.0)])
def test_swapping_the_two_sides_flips_the_direction(amplitudes):
    """Antisymmetry: the metric is a difference, so the verdict must invert."""

    first, second = amplitudes
    forward = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=first),
        post=_comparand(amplitude_db=second),
        margin_db=MARGIN_DB,
    )
    backward = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=second),
        post=_comparand(amplitude_db=first),
        margin_db=MARGIN_DB,
    )
    inverse = {
        BenefitStatus.IMPROVED: BenefitStatus.REGRESSED,
        BenefitStatus.REGRESSED: BenefitStatus.IMPROVED,
        BenefitStatus.INDETERMINATE: BenefitStatus.INDETERMINATE,
    }
    assert backward.status is inverse[forward.status]
    assert backward.evidence["improvement_db"] == pytest.approx(
        -forward.evidence["improvement_db"]
    )


@pytest.mark.parametrize("amplitude", [0.2, 1.0, 3.0])
def test_a_measurement_compared_with_itself_never_claims_a_direction(amplitude):
    verdict = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=amplitude),
        post=_comparand(amplitude_db=amplitude),
        margin_db=MARGIN_DB,
    )
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.evidence["improvement_db"] == pytest.approx(0.0)


@pytest.mark.parametrize("margin", [0.1, 0.5, 1.0])
def test_an_improvement_stays_improved_as_the_margin_shrinks(margin):
    """Monotone in the margin: a looser bar cannot un-improve a result."""

    verdict = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=3.0),
        post=_comparand(amplitude_db=0.5),
        margin_db=margin,
    )
    assert verdict.status is BenefitStatus.IMPROVED


def test_the_same_mask_on_both_sides_keeps_the_denominator_equal():
    """Equal masks mean equal graded bins by construction — the
    "residual fell because the mask grew" trap cannot arise."""

    axis = _axis()
    mask = tuple(f < 400.0 for f in axis)
    verdict = evaluate_benefit(
        entry_baseline=_comparand(amplitude_db=3.0, mask=mask),
        post=_comparand(amplitude_db=0.5, mask=mask),
        margin_db=MARGIN_DB,
    )
    assert verdict.evidence["n_bins"] == verdict.evidence["baseline_n_bins"]


# -- the comparand type -----------------------------------------------------


def test_a_comparand_needs_one_mask_flag_per_curve_point():
    with pytest.raises(CrossoverV2ContractError, match="one flag per curve point"):
        _comparand(mask=(True, False))


def test_a_comparand_needs_a_real_curve_and_named_identities():
    with pytest.raises(CrossoverV2ContractError, match="ResponseCurve"):
        MeasurementComparand(
            program_id="p", reference_mark="m", curve=(1.0, 2.0)  # type: ignore[arg-type]
        )
    with pytest.raises(CrossoverV2ContractError, match="program_id"):
        _comparand(program_id="")
    with pytest.raises(CrossoverV2ContractError, match="reference_mark"):
        _comparand(reference_mark=" mark ")


# --------------------------------------------------------------------------
# 4. spec
# --------------------------------------------------------------------------


def test_a_flat_enough_report_passes():
    verdict = evaluate_spec(_report(amplitude_db=0.2))
    assert verdict.status is SpecStatus.PASSED
    assert verdict.reason == SPEC_IN_TOLERANCE


def test_a_band_out_of_tolerance_fails():
    verdict = evaluate_spec(_report(amplitude_db=6.0))
    assert verdict.status is SpecStatus.FAILED
    assert verdict.reason == SPEC_BAND_OUT_OF_TOLERANCE


def test_the_spec_verdict_carries_every_band_and_the_span_it_graded():
    """The evidence a driver reads to decide whether to run another round.

    The gauge names ONE band; a round that has to choose what to fix needs
    all of them, each with the frequency of its worst bin and the edges it
    was actually graded between — the top band's no longer equals its
    nominal one. Disclosure: the verdict itself is unchanged by the presence
    of these keys, which the STATUS assertions above and below pin.
    """
    freqs = np.geomspace(200.0, 24000.0, 600)
    curve = np.where((freqs >= 2000.0) & (freqs < 8000.0), 1.0, 0.0)
    report = evaluate_flat_spec(
        freqs, curve, trusted_floor_hz=357.14, trusted_ceiling_hz=20000.0,
    )

    evidence = evaluate_spec(report).evidence

    assert evidence["graded_band_hz"] == [357.14, 20000.0]
    assert evidence["trusted_ceiling_hz"] == 20000.0
    assert [
        (b["f_lo_hz"], b["f_hi_hz"], b["graded_lo_hz"], b["graded_hi_hz"], b["passed"])
        for b in evidence["bands"]
    ] == [
        (250.0, 2000.0, 357.14, 2000.0, True),
        (2000.0, 8000.0, 2000.0, 8000.0, True),
        # Nominal 16 kHz, graded to the microphone's own 20 kHz.
        (8000.0, 16000.0, 8000.0, 20000.0, True),
    ]
    # Only the elevated band carries the elevation — the frame is the low-mid
    # band, which no part of a 2-8 kHz bulge is pooled into.
    deviations = [b["max_deviation_db"] for b in evidence["bands"]]
    assert deviations == pytest.approx([0.0, 1.0, 0.0], abs=1e-9)
    assert all(
        b["max_deviation_hz"] is not None and b["graded_lo_hz"] is not None
        for b in evidence["bands"]
    )
    # ...and the frame-free reading names WHICH two bands disagree.
    assert evidence["tilt"]["step_db"] == pytest.approx(1.0, abs=1e-9)
    assert evidence["tilt"]["high_band_hz"] == [2000.0, 8000.0]
    assert evidence["tilt"]["low_band_hz"] == [250.0, 2000.0]


def test_no_report_at_all_is_unevaluable():
    verdict = evaluate_spec(None)
    assert verdict.status is SpecStatus.UNEVALUABLE
    assert verdict.reason == SPEC_NO_REPORT


def test_partial_coverage_is_unevaluable_and_not_a_pass():
    """A clean bill of health is not issued for a spectrum not fully measured
    — nor is the missing evidence called a failure."""

    axis = _axis()
    report = _report(
        amplitude_db=0.2, mask=np.asarray([f >= 8000.0 for f in axis], dtype=bool)
    )
    verdict = evaluate_spec(report)
    assert verdict.status is SpecStatus.UNEVALUABLE
    assert verdict.reason == SPEC_PARTIAL_COVERAGE


def test_a_measured_exceedance_outranks_missing_coverage():
    axis = _axis()
    report = _report(
        amplitude_db=6.0, mask=np.asarray([f >= 8000.0 for f in axis], dtype=bool)
    )
    verdict = evaluate_spec(report)
    assert verdict.status is SpecStatus.FAILED


def test_a_report_with_nothing_evaluable_is_unevaluable():
    report = FlatSpecReport(
        reference_db=-30.0,
        bands=(
            BandResult(
                f_lo_hz=250.0,
                f_hi_hz=2000.0,
                tolerance_db=1.5,
                max_deviation_db=None,
                max_deviation_hz=None,
                rms_deviation_db=None,
                n_bins=0,
                n_excluded=0,
                evaluable=False,
                passed=None,
            ),
        ),
        overall_passed=False,
        excluded_intervals=(),
        best_effort_above_hz=16000.0,
        smoothing_fraction=3,
    )
    verdict = evaluate_spec(report)
    assert verdict.status is SpecStatus.UNEVALUABLE
    assert verdict.reason == SPEC_NO_EVALUABLE_BAND


def test_the_spec_verdict_reads_the_reports_own_pass_rather_than_re_grading():
    """Hand a report whose ``overall_passed`` disagrees with its bands.

    If this module ever recomputed the verdict from the band metrics, it
    would answer FAILED here. It must answer what the evaluator said.
    """

    report = FlatSpecReport(
        reference_db=-30.0,
        bands=(
            BandResult(
                f_lo_hz=250.0,
                f_hi_hz=2000.0,
                tolerance_db=1.5,
                max_deviation_db=9.0,
                max_deviation_hz=428.0,
                rms_deviation_db=4.4,
                n_bins=40,
                n_excluded=0,
                evaluable=True,
                passed=False,
            ),
        ),
        overall_passed=True,
        excluded_intervals=(),
        best_effort_above_hz=16000.0,
        smoothing_fraction=3,
    )
    assert evaluate_spec(report).status is SpecStatus.PASSED


# --------------------------------------------------------------------------
# composing into the contract
# --------------------------------------------------------------------------


def test_an_unusable_capture_collapses_the_other_three_answers():
    """A capture that could not be graded cannot have produced a verdict —
    even when a stale mapping still says otherwise."""

    result = verification_result(
        capture=Verdict(CaptureValidity.UNUSABLE, CAPTURE_INTEGRITY_FAILED, {}),
        realization=Verdict(RealizationStatus.MATCHED, REALIZATION_WITHIN_TOLERANCE),
        benefit=Verdict(BenefitStatus.IMPROVED, BENEFIT_IMPROVED),
        spec=Verdict(SpecStatus.PASSED, SPEC_IN_TOLERANCE),
    )
    assert result.capture_validity is CaptureValidity.UNUSABLE
    assert result.realization is RealizationStatus.UNAVAILABLE
    assert result.benefit is BenefitStatus.INDETERMINATE
    assert result.spec is SpecStatus.UNEVALUABLE
    assert result.reason == CAPTURE_INTEGRITY_FAILED


def test_a_usable_capture_carries_all_four_reasons():
    result = verification_result(
        capture=Verdict(CaptureValidity.USABLE, CAPTURE_INTEGRITY_CLEAN),
        realization=Verdict(RealizationStatus.MATCHED, REALIZATION_WITHIN_TOLERANCE),
        benefit=Verdict(BenefitStatus.IMPROVED, BENEFIT_IMPROVED),
        spec=Verdict(SpecStatus.FAILED, SPEC_BAND_OUT_OF_TOLERANCE),
    )
    assert result.benefit is BenefitStatus.IMPROVED
    assert result.spec is SpecStatus.FAILED
    for reason in (
        CAPTURE_INTEGRITY_CLEAN,
        REALIZATION_WITHIN_TOLERANCE,
        BENEFIT_IMPROVED,
        SPEC_BAND_OUT_OF_TOLERANCE,
    ):
        assert reason in result.reason


def test_a_verdict_snapshots_the_evidence_it_was_handed():
    """A caller mutating its own dict cannot change a verdict after the fact.

    The package's #2307 N1 rule: a frozen record holding a live mapping is
    immutable in name only. Nested too — a shallow copy would leave the
    inner container shared, which is the same bug one level down.
    """

    live = {"improvement_db": 1.75, "bands": {"low": 3.0}, "legs": [1.0, 2.0]}
    verdict = Verdict(BenefitStatus.IMPROVED, BENEFIT_IMPROVED, live)

    live["improvement_db"] = -99.0
    live["bands"]["low"] = -99.0
    live["legs"].append(-99.0)
    live["injected"] = True

    assert verdict.evidence["improvement_db"] == 1.75
    assert verdict.evidence["bands"] == {"low": 3.0}
    assert verdict.evidence["legs"] == [1.0, 2.0]
    assert "injected" not in verdict.evidence


def test_a_verdict_evidence_payload_does_not_leak_the_verdicts_own_containers():
    """The reverse direction: a consumer mutating ``to_dict()`` output.

    Nested as well as top-level. A shallow ``dict()`` on the way out passes
    the verdict's own inner containers to the caller, so a host editing the
    payload it is about to log would rewrite the verdict behind itself —
    the construction-side bug in mirror image, and it was real here until
    ``to_dict`` detached too.
    """

    verdict = Verdict(
        BenefitStatus.IMPROVED,
        BENEFIT_IMPROVED,
        {"n_bins": 40, "bands": {"low": 3.0}, "legs": [1.0]},
    )
    payload = verdict.to_dict()
    payload["evidence"]["n_bins"] = -1
    payload["evidence"]["bands"]["low"] = -99.0
    payload["evidence"]["legs"].append(-99.0)

    assert verdict.evidence["n_bins"] == 40
    assert verdict.evidence["bands"] == {"low": 3.0}
    assert verdict.evidence["legs"] == [1.0]


def test_a_verdict_renders_a_loggable_payload():
    payload = Verdict(
        BenefitStatus.IMPROVED, BENEFIT_IMPROVED, {"improvement_db": 1.75}
    ).to_dict()
    assert payload == {
        "status": "improved",
        "reason": BENEFIT_IMPROVED,
        "evidence": {"improvement_db": 1.75},
    }



# --------------------------------------------------------------------------
# 5. the three adoption axes (#2537)
# --------------------------------------------------------------------------


def _capture(status=CaptureValidity.USABLE, reason=CAPTURE_INTEGRITY_CLEAN):
    return Verdict(status, reason, {})


def _realization(
    status=RealizationStatus.MATCHED, reason=REALIZATION_WITHIN_TOLERANCE
):
    return Verdict(status, reason, {})


def _benefit(status=BenefitStatus.IMPROVED, reason=BENEFIT_IMPROVED):
    return Verdict(status, reason, {})


def _spec(status=SpecStatus.PASSED, reason=SPEC_IN_TOLERANCE):
    return Verdict(status, reason, {})


class _Probe:
    """The attributes :mod:`verification` reads off a delta-probe map.

    A stand-in rather than a real :class:`DeltaProbeMap`, and deliberately: the
    evaluators read this object DUCK-TYPED (the same rule
    ``evaluate_capture_validity`` follows for ``CaptureIntegrity``), so a test
    that could only be written with the real class would be pinning an import
    rather than a contract. The real map's own fields are pinned in
    ``tests/test_active_speaker_delta_probe.py``.
    """

    def __init__(
        self,
        *,
        verdict=DELTA_VERDICT_MATCHED,
        reason="tracked",
        residual_offset_db=None,
        residual_offset_tolerance_db=1.5,
        boost_over_declared_bound=False,
        boost_overshoot_db=None,
        safety_anchored=True,
        realized_louder_than_commanded=False,
    ):
        self.verdict = verdict
        self.reason = reason
        self.residual_offset_db = residual_offset_db
        self.residual_offset_tolerance_db = residual_offset_tolerance_db
        self.boost_over_declared_bound = boost_over_declared_bound
        self.boost_overshoot_db = boost_overshoot_db
        # Which way the graded bins missed — the discriminator every
        # directional rule in this subsystem turns on, and the one
        # ``seam_rollback_deferral`` reads.
        self.realized_louder_than_commanded = realized_louder_than_commanded
        # Whether the realized-energy check ran (series-2 D1). Defaults to the
        # round that HAD a pre-apply capture, because that is the ordinary
        # shape every other assertion in this file means; the unanchored one is
        # its own test.
        self.safety_anchored = safety_anchored


class _Integrity:
    def __init__(self, failed=(), not_evaluated=()):
        self.failed = tuple(failed)
        self.not_evaluated = tuple(not_evaluated)


# --- trust ------------------------------------------------------------------


def test_a_usable_capture_with_a_graded_realization_is_trusted():
    verdict = evaluate_evidence_trust(
        capture=_capture(), realization=_realization()
    )
    assert verdict.status is EvidenceTrust.TRUSTED
    assert verdict.reason == TRUST_MEASURED


def test_an_unusable_capture_is_untrusted_under_its_own_reason():
    verdict = evaluate_evidence_trust(
        capture=_capture(CaptureValidity.UNUSABLE, CAPTURE_INTEGRITY_FAILED),
        realization=_realization(RealizationStatus.UNAVAILABLE),
    )
    assert verdict.status is EvidenceTrust.UNTRUSTED
    # The CAPTURE's reason, not the realization's: an unusable capture is why
    # the realization is unavailable, and naming the symptom would misdirect.
    assert verdict.reason == CAPTURE_INTEGRITY_FAILED


def test_an_ungraded_realization_is_untrusted_under_its_own_reason():
    verdict = evaluate_evidence_trust(
        capture=_capture(),
        realization=_realization(
            RealizationStatus.UNAVAILABLE, REALIZATION_NO_TRACKING
        ),
    )
    assert verdict.status is EvidenceTrust.UNTRUSTED
    assert verdict.reason == REALIZATION_NO_TRACKING


@pytest.mark.parametrize("benefit", list(BenefitStatus))
def test_the_benefit_verdict_never_decides_evidence_trust(benefit):
    """#2537's correction, and the whole of it.

    An indeterminate benefit means the round could not compare its measured
    state to a BEFORE. The post-apply capture is fine, so the applied state IS
    measured — and "reverting to an unknown measured state seems dumb" is the
    owner's ruling on exactly that. Trust reads capture and realization; the
    benefit is a quality question.

    Parametrised over the whole enum rather than the one interesting member:
    the claim is that this axis cannot see the benefit at all, and a signature
    with no benefit parameter is what proves it.
    """

    assert "benefit" not in inspect.signature(evaluate_evidence_trust).parameters
    del benefit


# --- safety -----------------------------------------------------------------


def test_no_finding_is_reported_as_safe_and_says_what_looked():
    verdict = evaluate_applied_safety(probe=_Probe(), integrity=_Integrity())
    assert verdict.status is SafetyStatus.SAFE
    assert verdict.reason == SAFETY_NO_FINDING
    # "Safe because nothing was found" must be distinguishable from "safe
    # because nothing looked" — see the function's own docstring.
    assert verdict.evidence["probe_graded"] is True
    assert verdict.evidence["integrity_graded"] is True


def test_no_finding_says_whether_the_realized_energy_check_could_run():
    """"Safe" had two readings and now has two reasons (series-2 D1).

    ``no_unsafe_finding`` says the realized-energy check looked and found
    nothing. ``no_unsafe_finding_realized_energy_unmeasured`` says it could not
    look — there was no pre-apply capture to difference this one against — and a
    first-ever round reaches that BY CONSTRUCTION, so it is the common case
    rather than an edge one. The status and the adoption row are identical,
    deliberately: refusing on an absent measurement would revert every first
    round. What differs is what the receipt and the journal claim was checked.
    """
    checked = evaluate_applied_safety(
        probe=_Probe(safety_anchored=True), integrity=_Integrity(),
    )
    unchecked = evaluate_applied_safety(
        probe=_Probe(safety_anchored=False), integrity=_Integrity(),
    )

    assert checked.status is unchecked.status is SafetyStatus.SAFE
    assert checked.reason == SAFETY_NO_FINDING
    assert unchecked.reason == SAFETY_NO_FINDING_UNMEASURED
    assert checked.reason != unchecked.reason
    assert checked.evidence["safety_anchored"] is True
    assert unchecked.evidence["safety_anchored"] is False


def test_the_model_departure_target_quotes_its_OWN_frequency():
    """Two reductions over two bin sets, and the target must not cross them.

    ``max_signed_error_db`` is the worst POSITIVE departure over the SAFETY
    bins; ``worst_hz`` is the worst ABSOLUTE error over the GRADED ones. On the
    banked series-2 r1b they sit 563 Hz apart and name two different acoustic
    features — the standing model error, and the dip the next round went on to
    close. A target is an instruction to the next round, so quoting one bin's dB
    at the other bin's frequency sends it after the wrong one.
    """
    from jasper.active_speaker.crossover_v2.verification import (
        QUALITY_MODEL_DEPARTURE,
        _model_departure_target,
    )

    probe = types.SimpleNamespace(
        model_departure_over_tolerance=True,
        max_signed_error_db=3.891,
        max_signed_error_hz=1384.1,
        # The decoy: a real field, on a real map, measuring something else.
        worst_hz=1947.2,
    )
    assert _model_departure_target(probe) == [
        f"{QUALITY_MODEL_DEPARTURE}:3.89dB@1384Hz"
    ]

    # Nothing when the departure did not clear the probe's own tolerance — the
    # boolean is read rather than a threshold re-derived here.
    assert _model_departure_target(
        types.SimpleNamespace(
            model_departure_over_tolerance=False,
            max_signed_error_db=3.891, max_signed_error_hz=1384.1,
        )
    ) == []
    assert _model_departure_target(None) == []


def test_an_absent_probe_is_reported_as_safe_but_ungraded():
    """An absent measurement is not evidence of a hazard.

    ``DELTA_PROBE_ROLLBACK_VERDICTS`` already holds this line for the probe's
    own rollback set, in as many words: rolling back on an absent measurement
    "would revert every session whose household closed the phone before the
    post-apply sweep". This axis must not contradict it.
    """

    verdict = evaluate_applied_safety(probe=None, integrity=None)
    assert verdict.status is SafetyStatus.SAFE
    assert verdict.evidence["probe_graded"] is False
    assert verdict.evidence["integrity_graded"] is False


def test_a_boost_realized_above_its_declared_bound_is_unsafe():
    verdict = evaluate_applied_safety(
        probe=_Probe(boost_over_declared_bound=True, boost_overshoot_db=4.2),
        integrity=_Integrity(),
    )
    assert verdict.status is SafetyStatus.UNSAFE
    assert verdict.reason == SAFETY_BOOST_OVER_DECLARED_BOUND
    assert verdict.evidence["boost_overshoot_db"] == 4.2


def test_a_clipped_capture_is_unsafe():
    verdict = evaluate_applied_safety(
        probe=_Probe(), integrity=_Integrity(failed=(CLIPPED_RUN_CHECK,))
    )
    assert verdict.status is SafetyStatus.UNSAFE
    assert verdict.reason == SAFETY_CLIPPED_CAPTURE


def test_a_non_clipping_integrity_failure_is_not_a_safety_finding():
    """It is an evidence-trust failure, which is a different row.

    A capture that failed its schedule check is unusable, not dangerous, and
    the receipt must say which.
    """

    verdict = evaluate_applied_safety(
        probe=_Probe(), integrity=_Integrity(failed=("schedule_residual",))
    )
    assert verdict.status is SafetyStatus.SAFE


@pytest.mark.parametrize(
    ("residual_db", "expected"),
    [
        (+2.3, SafetyStatus.UNSAFE),
        (-2.3, SafetyStatus.SAFE),
        (+1.4, SafetyStatus.SAFE),
        (-1.4, SafetyStatus.SAFE),
    ],
)
def test_the_level_shift_hard_stop_is_directional(residual_db, expected):
    """DIRECTION is the discriminator, at identical magnitude (#2537).

    +2.3 dB and −2.3 dB are the same number and opposite facts: one is energy
    nobody asked for, the other is a household losing some output and a signal
    for the next round. The 2026-08-15 JTS3 residual was negative, and it was
    reverted anyway.

    The two 1.4 dB rows are the tolerance's own edge — inside it, neither
    direction is a finding at all.
    """

    verdict = evaluate_applied_safety(
        probe=_Probe(
            verdict=DELTA_VERDICT_LEVEL_MISMATCH,
            residual_offset_db=residual_db,
            residual_offset_tolerance_db=1.5,
        ),
        integrity=_Integrity(),
    )
    assert verdict.status is expected
    if expected is SafetyStatus.UNSAFE:
        assert verdict.reason == SAFETY_UNCOMMANDED_LEVEL_LOUDER


def test_a_positive_residual_without_the_level_verdict_is_not_a_hard_stop():
    """The probe decides whether a residual is a FINDING; this axis reads it.

    ``residual_offset_db`` is measured on every classified map. Only
    ``level_mismatch`` says the level moved materially AND sufficiently to
    explain the map's failure. Treating a bare number as a hazard would put a
    second owner on that judgement.
    """

    verdict = evaluate_applied_safety(
        probe=_Probe(verdict=DELTA_VERDICT_MATCHED, residual_offset_db=+9.0),
        integrity=_Integrity(),
    )
    assert verdict.status is SafetyStatus.SAFE


def test_a_band_scoped_positive_shift_is_still_a_hard_stop():
    """#2533 narrows WHERE a level was measured, never WHETHER it happened."""

    verdict = evaluate_applied_safety(
        probe=_Probe(
            verdict=DELTA_VERDICT_LEVEL_MISMATCH,
            reason=REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND,
            residual_offset_db=+2.3,
        ),
        integrity=_Integrity(),
    )
    assert verdict.status is SafetyStatus.UNSAFE
    assert verdict.evidence["probe_reason"] == (
        REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND
    )


def test_the_clipped_run_check_name_matches_the_analyzers_own():
    """The one literal this module copies rather than imports.

    ``verification`` cannot import ``program_analysis`` at module scope (5,500
    lines of scipy behind a ``TYPE_CHECKING`` guard), so it repeats the check
    name. A copy with no guard is a copy that drifts.
    """

    from jasper.audio_measurement.program_analysis import (
        INTEGRITY_CHECK_CLIPPED_RUN,
    )

    assert CLIPPED_RUN_CHECK == INTEGRITY_CHECK_CLIPPED_RUN


# --- quality ----------------------------------------------------------------


def _quality(**overrides):
    kwargs = {
        "realization": _realization(),
        "benefit": _benefit(),
        "spec": _spec(),
        "probe": _Probe(),
        "spec_report": None,
    }
    kwargs.update(overrides)
    return evaluate_round_quality(**kwargs)


def test_everything_the_round_wanted_is_a_pass_with_no_targets():
    verdict = _quality()
    assert verdict.status is QualityStatus.PASSED
    assert verdict.reason == ADOPTION_REALIZED_AND_IMPROVED
    assert verdict.evidence["targets"] == []


def test_a_measured_regression_outranks_the_target_list():
    """A round that made the speaker worse has nothing to hand the next one."""

    verdict = _quality(
        benefit=_benefit(BenefitStatus.REGRESSED, BENEFIT_REGRESSED),
        spec=_spec(SpecStatus.FAILED, SPEC_BAND_OUT_OF_TOLERANCE),
    )
    assert verdict.status is QualityStatus.REGRESSED
    assert verdict.reason == ADOPTION_MEASURED_REGRESSION


@pytest.mark.parametrize(
    ("overrides", "target"),
    [
        (
            {"realization": _realization(
                RealizationStatus.FAILED, REALIZATION_OUT_OF_TOLERANCE
            )},
            f"realization:{REALIZATION_OUT_OF_TOLERANCE}",
        ),
        (
            {"benefit": _benefit(
                BenefitStatus.INDETERMINATE, BENEFIT_BASELINE_UNAVAILABLE
            )},
            f"benefit:{BENEFIT_BASELINE_UNAVAILABLE}",
        ),
        (
            {"spec": _spec(SpecStatus.FAILED, SPEC_BAND_OUT_OF_TOLERANCE)},
            f"spec:{SPEC_BAND_OUT_OF_TOLERANCE}",
        ),
        (
            {"probe": _Probe(
                verdict=DELTA_VERDICT_LEVEL_MISMATCH,
                reason=REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND,
            )},
            f"delta_probe:{REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND}",
        ),
    ],
)
def test_each_instrument_short_of_its_answer_becomes_a_named_target(
    overrides, target
):
    """Every instrument contributes a TARGET; only two contribute a STATUS.

    The target list is what the next round chases, so it is drawn from all four
    instruments. The status is #2291's table, keyed on ``(realization,
    benefit)`` alone — see the spec/probe pins below for why the other two are
    deliberately excluded from the decision.
    """
    assert target in _quality(**overrides).evidence["targets"]


def test_an_ungraded_probe_cannot_make_a_round_pass_and_cannot_fail_it():
    """No evidence to refuse on, and no permission granted either."""

    verdict = _quality(probe=None)
    assert verdict.status is QualityStatus.PASSED
    assert not any(t.startswith("delta_probe:") for t in verdict.evidence["targets"])


@pytest.mark.parametrize("spec_status", list(SpecStatus))
def test_the_spec_verdict_never_moves_the_quality_STATUS(spec_status):
    """Two independent reasons, and both have to hold.

    *Spec is an outcome, not a proxy for benefit* — every row of #2291's table
    reads "any" for spec, and the permutation pin on ``decide_adoption`` is
    load-bearing. AND the spec verdicts available today are computed over the
    raw 250 Hz-2 kHz band with no intersection against the session's own trusted
    floor (357.1 Hz on a 7 ms gate), so deciding on them would key a series on
    sub-trusted-floor evidence the same session's delta probe refuses to grade —
    a term the E4 sweep measured moving ~2 dB with gate length alone. That
    intersection is a separate filed fix and must land before any axis decides
    on a spec verdict.

    So it rides as DISCLOSURE, and this is the pin that keeps it there.
    """
    statuses = {
        _quality(spec=_spec(status, f"reason_{status.value}")).status
        for status in SpecStatus
    }
    assert len(statuses) == 1
    # …while still reaching the target list, so the next round can chase it.
    missed = _quality(spec=_spec(SpecStatus.FAILED, SPEC_BAND_OUT_OF_TOLERANCE))
    assert f"spec:{SPEC_BAND_OUT_OF_TOLERANCE}" in missed.evidence["targets"]
    del spec_status


def test_the_delta_probe_verdict_never_moves_the_quality_STATUS():
    """Its rollback verdicts have their own path; the rest are non-rollback by
    a standing ruling, so neither belongs in this status either."""
    graded = _quality(probe=_Probe())
    band_scoped = _quality(probe=_Probe(
        verdict=DELTA_VERDICT_LEVEL_MISMATCH,
        reason=REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND,
    ))
    assert graded.status is band_scoped.status
    assert graded.reason == band_scoped.reason
    assert any(
        t.startswith("delta_probe:") for t in band_scoped.evidence["targets"]
    )


@pytest.mark.parametrize(
    ("probe_verdict", "louder", "restores"),
    [
        (DELTA_VERDICT_LEVEL_SHORTFALL, False, False),
        (DELTA_VERDICT_LEVEL_SHORTFALL, True, True),
        (DELTA_VERDICT_MODEL_ERROR, False, False),
        (DELTA_VERDICT_MODEL_ERROR, True, True),
        (DELTA_VERDICT_SPATIALLY_COSTLY, False, True),
    ],
)
def test_a_quieter_realized_vs_commanded_miss_keeps_a_measured_acceptable_round(
    probe_verdict, louder, restores
):
    """#3485, witnessed live on the day-2 recommissioning campaign.

    The campaign's flattest state — every spec band inside tolerance, benefit
    improved past its margin, realization matched, trust trusted, safety safe,
    nothing realized louder than commanded — was RESTORED onto a
    measured-FAILING graph because the delta probe returned
    ``level_dependent_shortfall`` over a band that pooled a deliberate +2 dB
    realizability PROBE with a shelf increment. ``docs/measurement-loop-doctrine``
    §3: *a class that retreats from a measured-acceptable state on realized !=
    commanded alone is a bug against this principle*, and *realized-versus-
    predicted mismatch is a learning signal, never by itself a reason to
    retreat*.

    **Direction stays the discriminator, which is why the parametrization has a
    ``louder`` column.** The same class pointing LOUDER than commanded is energy
    nobody asked for and still comes off — the 2026-08-15 shape axis rule,
    unchanged. ``spatially_costly`` is the second control: it differences two
    MEASUREMENTS with no model between them, so it is the measured regression
    §3 restores ON, and it still does whichever way it points.
    """

    quality = _quality(probe=_Probe(
        verdict=probe_verdict,
        reason="realized_short_of_commanded",
        realized_louder_than_commanded=louder,
    ))
    decision = _adopt(quality=quality)

    if restores:
        assert quality.status is QualityStatus.REGRESSED
        assert quality.evidence["probe_rollback_class"] == probe_verdict
        assert decision.outcome is AdoptionOutcome.RESTORE
        assert decision.row == ADOPTION_ROW_RESTORE_REGRESSION
        return
    assert quality.status is QualityStatus.PASSED
    assert quality.reason == ADOPTION_REALIZED_AND_IMPROVED
    assert quality.evidence["probe_rollback_class"] == ""
    assert decision.outcome is AdoptionOutcome.KEEP
    assert decision.row == ADOPTION_ROW_KEEP
    # …and the probe's finding is still on the round, as the learning signal
    # §3 calls it: banked, chased next round, deciding nothing here.
    assert "delta_probe:realized_short_of_commanded" in quality.evidence["targets"]


def test_the_quality_status_is_2291s_own_table_unchanged_in_what_it_reads():
    """The nine cells, keyed on the same two statuses, with the same causes.

    #2537 changed what a non-keep cell RESOLVES TO, not what decides it. A
    lookup with no default is what makes a tenth combination impossible.
    """
    assert set(_QUALITY_TABLE) == {
        (realization, benefit)
        for realization in RealizationStatus
        for benefit in BenefitStatus
    }
    assert _QUALITY_TABLE[
        (RealizationStatus.MATCHED, BenefitStatus.IMPROVED)
    ] == (QualityStatus.PASSED, ADOPTION_REALIZED_AND_IMPROVED)
    # Every REGRESSED cell carries the regression as its cause, including the
    # one the pre-#2537 table gave to ``realization_failed``: a realization
    # failure no longer takes a graph off, so it cannot be the restoring cause.
    for realization in RealizationStatus:
        quality, reason = _QUALITY_TABLE[(realization, BenefitStatus.REGRESSED)]
        assert quality is QualityStatus.REGRESSED
        assert reason == ADOPTION_MEASURED_REGRESSION
    # And the causes that survive verbatim, on cells that used to restore or
    # ask and now keep-for-iteration.
    assert _QUALITY_TABLE[
        (RealizationStatus.FAILED, BenefitStatus.IMPROVED)
    ] == (QualityStatus.MISSED, ADOPTION_REALIZATION_FAILED)
    assert _QUALITY_TABLE[
        (RealizationStatus.UNAVAILABLE, BenefitStatus.IMPROVED)
    ] == (QualityStatus.MISSED, ADOPTION_REALIZATION_UNAVAILABLE)
    assert _QUALITY_TABLE[
        (RealizationStatus.MATCHED, BenefitStatus.INDETERMINATE)
    ] == (QualityStatus.MISSED, ADOPTION_UNPROVEN)


def test_each_failing_spec_band_rides_the_receipt_as_its_own_target():
    """"250-2000 Hz, +4.70 dB against a 1.5 dB tolerance" is an instruction.

    "spec failed" is only a mood, and the receipt is what the NEXT round reads.
    The fixture is cycle 4's own shape: TWO bands failed, and the second is
    nowhere near any gate floor — which is why the per-band list is worth
    carrying even while the spec verdict itself is not yet trusted enough to
    decide on (see ``test_the_spec_verdict_never_moves_the_quality_STATUS``).
    """

    verdict = _quality(
        spec=_spec(SpecStatus.FAILED, SPEC_BAND_OUT_OF_TOLERANCE),
        spec_report=_report_with_two_failing_bands(),
    )
    bands = verdict.evidence["spec_bands"]

    assert [(b["f_lo_hz"], b["f_hi_hz"]) for b in bands] == [
        (250.0, 2000.0), (8000.0, 16000.0),
    ]
    assert bands[0]["max_deviation_db"] == pytest.approx(4.70)
    assert bands[0]["tolerance_db"] == pytest.approx(1.5)
    # Signed, because "too loud" and "too quiet" call for opposite corrections.
    assert bands[1]["max_deviation_db"] == pytest.approx(-2.63)
    assert bands[1]["max_deviation_hz"] == pytest.approx(14072.0)


def test_a_passing_or_unevaluable_band_is_not_a_target():
    """A band that passed has nothing to aim at, and one that could not be
    measured has nothing to aim WITH — ``passed=False, evaluable=False`` means
    "not measured", not "failed"."""

    verdict = _quality(spec_report=_report_with_two_failing_bands())
    named = {(b["f_lo_hz"], b["f_hi_hz"]) for b in verdict.evidence["spec_bands"]}
    assert (2000.0, 8000.0) not in named   # passed
    assert (20.0, 250.0) not in named      # unevaluable


def _report_with_two_failing_bands():
    """Cycle 4's shape: two failing bands, one passing, one unevaluable.

    The numbers are that round's own, from the receipt: band 1 measured
    **+4.70 dB** against a 1.5 dB tolerance (3.1x over), and band 3 measured
    **-2.63 dB at 14,072 Hz**. Grading is MAX deviation, not RMS.
    """

    return types.SimpleNamespace(bands=(
        types.SimpleNamespace(
            f_lo_hz=20.0, f_hi_hz=250.0, tolerance_db=3.0, evaluable=False,
            passed=None, max_deviation_db=None, max_deviation_hz=None,
            graded_lo_hz=20.0, graded_hi_hz=250.0,
        ),
        types.SimpleNamespace(
            f_lo_hz=250.0, f_hi_hz=2000.0, tolerance_db=1.5, evaluable=True,
            passed=False, max_deviation_db=4.70, max_deviation_hz=331.8,
            graded_lo_hz=250.0, graded_hi_hz=2000.0,
        ),
        types.SimpleNamespace(
            f_lo_hz=2000.0, f_hi_hz=8000.0, tolerance_db=2.0, evaluable=True,
            passed=True, max_deviation_db=0.4, max_deviation_hz=4000.0,
            graded_lo_hz=2000.0, graded_hi_hz=8000.0,
        ),
        types.SimpleNamespace(
            f_lo_hz=8000.0, f_hi_hz=16000.0, tolerance_db=2.0, evaluable=True,
            passed=False, max_deviation_db=-2.63, max_deviation_hz=14072.0,
            graded_lo_hz=8000.0, graded_hi_hz=20000.0,
        ),
    ))


# --------------------------------------------------------------------------
# 6b. headroom — is a flatter result still reachable? (#2602)
# --------------------------------------------------------------------------


def _headroom_band(*, lo, hi, level_db=None, ripple_db=None, evaluable=True):
    """One graded band carrying only the two fields #2602's axis reads.

    Built directly rather than measured, because this section is about the
    DECISION the numbers drive. That the numbers themselves are the shipped
    evaluator's is a separate claim, and
    ``test_the_objectives_are_the_shipped_reductions_not_a_second_copy``
    below is what holds it.
    """

    return BandResult(
        f_lo_hz=lo, f_hi_hz=hi, tolerance_db=3.0,
        max_deviation_db=None if level_db is None else level_db,
        max_deviation_hz=None, rms_deviation_db=None,
        n_bins=10, n_excluded=0, evaluable=evaluable, passed=True,
        level_deviation_db=level_db, max_ripple_db=ripple_db,
    )


def _headroom_report(*bands):
    return FlatSpecReport(
        reference_db=0.0, bands=tuple(bands), overall_passed=True,
        excluded_intervals=(), best_effort_above_hz=16000.0,
        smoothing_fraction=3,
    )


#: The owner's own round-3 numbers: 250-2000 Hz sitting 2.37 dB above
#: 8000-16000 Hz, with real ripple inside each band. The case the ruling was
#: written from, so the table below is anchored to a measurement rather than
#: to a number chosen to make a test pass.
def _round_three_report():
    return _headroom_report(
        _headroom_band(lo=250.0, hi=2000.0, level_db=1.185, ripple_db=0.9),
        _headroom_band(lo=8000.0, hi=16000.0, level_db=-1.185, ripple_db=0.6),
    )


def _flat_report():
    """Both objectives inside the plateau — nothing left worth a round."""

    return _headroom_report(
        _headroom_band(lo=250.0, hi=2000.0, level_db=0.05, ripple_db=0.1),
        _headroom_band(lo=8000.0, hi=16000.0, level_db=-0.05, ripple_db=0.1),
    )


def _headroom(
    *, report=None, previous=None, ordinal=1, cap=3, plateau=0.25,
    floor_hz=None, previous_floor_hz=None,
):
    return evaluate_iteration_headroom(
        objectives=flatness_objectives(report),
        previous=previous,
        round_ordinal=ordinal,
        round_cap=cap,
        plateau_db=plateau,
        trusted_floor_hz=floor_hz,
        previous_trusted_floor_hz=previous_floor_hz,
    )


def test_the_objectives_are_the_shipped_reductions_not_a_second_copy():
    """Tilt IS ``spec_band_tilt``; ripple IS the report's own band field.

    Asserted against the owner itself rather than against a baked number, so
    the pin follows ``spec_band_tilt`` if it changes and fails if this module
    stops using it. #1857 exists precisely because a frame-dependent reading
    and a frame-free one disagree — a second copy here would be free to drift
    back to the wrong one.
    """

    report = _round_three_report()
    objectives = flatness_objectives(report)

    assert objectives.tilt_db == spec_band_tilt(report).step_db
    assert objectives.tilt_db == pytest.approx(2.37)
    assert objectives.ripple_db == pytest.approx(0.9)


def test_the_worst_objective_is_a_max_so_a_large_tilt_cannot_hide():
    """Flat bands sitting at different levels is still a speaker to fix.

    Pooling the two objectives would let 2.37 dB of tilt average away behind
    tidy in-band ripple, which is the exact result the owner was looking at
    when the ruling was written.
    """

    assert FlatnessObjectives(tilt_db=2.37, ripple_db=0.1).worst_db == 2.37
    assert FlatnessObjectives(tilt_db=0.1, ripple_db=2.37).worst_db == 2.37
    # Sign is not the question — how far from flat is.
    assert FlatnessObjectives(tilt_db=-2.37, ripple_db=None).worst_db == 2.37
    assert FlatnessObjectives(tilt_db=None, ripple_db=None).worst_db is None


@pytest.mark.parametrize(
    ("case", "kwargs", "status", "reason"),
    [
        (
            "round 1 with the owner's own tilt still on the speaker",
            {"report": _round_three_report()},
            IterationHeadroom.REACHABLE, HEADROOM_REACHABLE,
        ),
        (
            "already flat and level on both objectives",
            {"report": _flat_report()},
            IterationHeadroom.EXHAUSTED, HEADROOM_WITHIN_PLATEAU,
        ),
        (
            "still 2.37 dB out, but the last round moved it 0.03 dB",
            {
                "report": _round_three_report(),
                "previous": FlatnessObjectives(tilt_db=2.40, ripple_db=0.9),
                "ordinal": 2,
            },
            IterationHeadroom.EXHAUSTED, HEADROOM_PLATEAUED,
        ),
        (
            "still 2.37 dB out and the last round moved it 1.6 dB",
            {
                "report": _round_three_report(),
                "previous": FlatnessObjectives(tilt_db=4.0, ripple_db=0.9),
                "ordinal": 2,
            },
            IterationHeadroom.REACHABLE, HEADROOM_REACHABLE,
        ),
        (
            "round 3 of 3, with plenty still left to chase",
            {"report": _round_three_report(), "ordinal": 3},
            IterationHeadroom.EXHAUSTED, HEADROOM_CAP_REACHED,
        ),
        # The bites ruling reversed both of these: missing evidence is not a
        # plateau, so the reason still names what was missing and the STATUS
        # keeps the series open.
        (
            "no post-apply cloud, so nothing to grade",
            {"report": None},
            IterationHeadroom.REACHABLE, HEADROOM_NO_OBJECTIVES,
        ),
        (
            "a report whose every band fell below the trusted floor",
            {"report": _headroom_report(_headroom_band(lo=250.0, hi=2000.0, evaluable=False))},
            IterationHeadroom.REACHABLE, HEADROOM_NO_OBJECTIVES,
        ),
    ],
    ids=[
        "reachable_first_round", "flat_enough", "plateaued", "still_moving",
        "round_cap", "no_report", "nothing_gradable",
    ],
)
def test_the_headroom_table(case, kwargs, status, reason):
    """Every way a series continues, and every way it ends. (#2602)"""

    verdict = _headroom(**kwargs)
    assert verdict.status is status, case
    assert verdict.reason == reason, case


def test_ungradable_objectives_do_not_end_a_series():
    """The ethos's own sentence, as a guard.

    *Only the round budget, the plateau, and the safety class end a series.*
    An ungradable objective is missing evidence — a tier that walked no
    post-apply cloud, or a report whose bands all fell below the trusted floor
    — and reading that as "nothing better is reachable" is the conflation the
    ruling forbids. Held separately from the table above because the table
    would still pass if a later change flipped the status back and updated the
    expectation with it; this states WHICH statuses are allowed to end a
    series, so a fourth ending cannot be added by accident.
    """

    ended = {
        reason
        for reason in (
            HEADROOM_CAP_REACHED, HEADROOM_NO_OBJECTIVES,
            HEADROOM_WITHIN_PLATEAU, HEADROOM_PLATEAUED, HEADROOM_REACHABLE,
        )
        for verdict in [_verdict_for_reason(reason)]
        if verdict.status is IterationHeadroom.EXHAUSTED
    }

    assert ended == {
        HEADROOM_CAP_REACHED, HEADROOM_WITHIN_PLATEAU, HEADROOM_PLATEAUED,
    }


def _verdict_for_reason(reason: str):
    """The smallest input that makes the headroom axis answer with ``reason``."""

    if reason == HEADROOM_CAP_REACHED:
        return _headroom(report=_round_three_report(), ordinal=3)
    if reason == HEADROOM_NO_OBJECTIVES:
        return _headroom(report=None)
    if reason == HEADROOM_WITHIN_PLATEAU:
        return _headroom(report=_flat_report())
    if reason == HEADROOM_PLATEAUED:
        return _headroom(
            report=_round_three_report(),
            previous=FlatnessObjectives(tilt_db=2.40, ripple_db=0.9),
            ordinal=2,
        )
    return _headroom(report=_round_three_report())


def test_the_cap_outranks_every_other_ending():
    """A third round is over because it is the third, not because it stalled.

    Order matters for the SENTENCE, not just the status: telling a household
    "more rounds are unlikely to help" when the truth is "we are only allowed
    three" would claim the measurement said something it did not.
    """

    verdict = _headroom(
        report=_flat_report(),
        previous=FlatnessObjectives(tilt_db=0.1, ripple_db=0.1),
        ordinal=3,
    )
    assert verdict.status is IterationHeadroom.EXHAUSTED
    assert verdict.reason == HEADROOM_CAP_REACHED


def test_a_first_round_cannot_be_called_plateaued():
    """No previous round means no movement to judge, never zero movement.

    A round 1 that resolved "the objectives moved 0 dB" would end every series
    at its first round — the pre-#2602 behaviour, restored by a missing record.
    """

    verdict = _headroom(report=_round_three_report(), previous=None, ordinal=1)
    assert verdict.status is IterationHeadroom.REACHABLE
    assert verdict.evidence["movement_db"] is None


def test_the_headroom_verdict_shows_the_numbers_it_decided_on():
    """A support read needs the tilt, not just the word."""

    verdict = _headroom(
        report=_round_three_report(),
        previous=FlatnessObjectives(tilt_db=4.0, ripple_db=0.9),
        ordinal=2,
    )
    evidence = verdict.evidence
    assert evidence["round_ordinal"] == 2
    assert evidence["round_cap"] == 3
    assert evidence["plateau_db"] == 0.25
    assert evidence["objectives"] == {"tilt_db": pytest.approx(2.37), "ripple_db": 0.9}
    assert evidence["previous_objectives"] == {"tilt_db": 4.0, "ripple_db": 0.9}
    assert evidence["worst_db"] == pytest.approx(2.37)
    # Positive means "got flatter", the same direction ``improvement_db`` uses.
    assert evidence["movement_db"] == pytest.approx(1.63)


def test_a_movement_that_went_BACKWARDS_is_not_a_plateau_here():
    """Getting worse is the BENEFIT axis's business, not this one.

    A negative movement is below the plateau bar arithmetically, and it must
    still read as EXHAUSTED rather than as headroom — but the round that
    measured worse is restored by row 5 long before this verdict is consulted,
    so this axis never has to be the thing that catches it. Pinned so the two
    responsibilities stay separate.
    """

    verdict = _headroom(
        report=_round_three_report(),
        previous=FlatnessObjectives(tilt_db=1.0, ripple_db=0.5),
        ordinal=2,
    )
    assert verdict.status is IterationHeadroom.EXHAUSTED
    assert verdict.reason == HEADROOM_PLATEAUED
    assert verdict.evidence["movement_db"] < 0


def test_the_plateau_bar_must_be_a_positive_db():
    with pytest.raises(CrossoverV2ContractError, match="plateau_db"):
        _headroom(report=_round_three_report(), plateau=0.0)


# --------------------------------------------------------------------------
# 6c. the passing cell, split (#2602)
# --------------------------------------------------------------------------


def test_a_passing_round_with_headroom_left_keeps_going():
    """#2602's headline row: in tolerance, still improving, another round.

    The graph is KEPT — ``KEEP_FOR_ITERATION`` leaves the speaker in exactly
    the state ``KEEP`` does — and the round says the series is not over.
    """

    decision = _adopt(
        headroom=Verdict(IterationHeadroom.REACHABLE, HEADROOM_REACHABLE, {}),
    )
    assert decision.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert decision.row == ADOPTION_ROW_KEEP_ITERATING
    assert decision.reason == HEADROOM_REACHABLE


@pytest.mark.parametrize(
    "reason",
    [HEADROOM_WITHIN_PLATEAU, HEADROOM_PLATEAUED, HEADROOM_CAP_REACHED,
     HEADROOM_NO_OBJECTIVES],
)
def test_a_passing_round_with_no_headroom_left_ends_the_series(reason):
    """Row 1 stays row 1, and now names WHICH ending it was."""

    decision = _adopt(
        headroom=Verdict(IterationHeadroom.EXHAUSTED, reason, {}),
    )
    assert decision.outcome is AdoptionOutcome.KEEP
    assert decision.row == ADOPTION_ROW_KEEP
    assert decision.reason == reason


@pytest.mark.parametrize(
    "headroom_status", list(IterationHeadroom), ids=lambda s: s.value
)
def test_headroom_never_moves_a_round_that_did_not_pass(headroom_status):
    """#2602's split is confined to the one cell that used to be terminal.

    A MISSED round iterates however FLAT the headroom axis says the result is —
    it has outstanding targets by construction — and a REGRESSED one restores.
    If flatness leaked into either, "keep going" and "put the old sound back"
    would start depending on how flat the speaker happens to be.

    The one fact that does cross to the MISSED row is the spent budget, and it
    crosses as the axis's REASON (#2656) — which is why this walk over the two
    STATUSES still holds unchanged, and why it carries a reason that is not
    :data:`HEADROOM_CAP_REACHED`.
    """

    headroom = Verdict(headroom_status, "h", {})

    missed = _adopt(
        quality=Verdict(QualityStatus.MISSED, ADOPTION_UNPROVEN, {}),
        headroom=headroom,
    )
    assert missed.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert missed.row == ADOPTION_ROW_KEEP_FOR_ITERATION
    assert missed.reason == ADOPTION_UNPROVEN

    regressed = _adopt(
        quality=Verdict(
            QualityStatus.REGRESSED, ADOPTION_MEASURED_REGRESSION, {}
        ),
        headroom=headroom,
    )
    assert regressed.outcome is AdoptionOutcome.RESTORE
    assert regressed.row == ADOPTION_ROW_RESTORE_REGRESSION
    assert regressed.reason == ADOPTION_MEASURED_REGRESSION


# --------------------------------------------------------------------------
# 6d. the missing cell, bounded by the budget (#2656)
# --------------------------------------------------------------------------


def test_a_missed_round_at_the_budget_ends_the_series():
    """The cell the gate found unpinned: MISSED, with no round left to spend.

    The gate walked 40 consecutive MISSED rounds against this function and
    every one of them said keep-for-iteration, because only the PASSED cell
    read the fourth axis at all. The ethos names the budget as a series-ender
    with no row exception, so this row ends it.

    Three claims, and the third is the reason the row exists at all:

    * the series ENDS — the outcome is not ``keep_for_iteration``, which is
      what a driver and the done screen's button both read;
    * the graph STAYS — ``keep`` leaves the speaker exactly where
      ``keep_for_iteration`` did, on the best measured state known;
    * it does not fake a PASS — row 1's identifier says *passed* and this
      round did not, so the ending gets its own row.
    """

    decision = _adopt(
        quality=Verdict(QualityStatus.MISSED, ADOPTION_UNPROVEN, {}),
        headroom=Verdict(
            IterationHeadroom.EXHAUSTED, HEADROOM_CAP_REACHED, {},
        ),
    )

    assert decision.outcome is AdoptionOutcome.KEEP
    assert decision.row == ADOPTION_ROW_KEEP_MISSED_EXHAUSTED
    assert decision.row != ADOPTION_ROW_KEEP
    assert decision.reason == HEADROOM_CAP_REACHED


@pytest.mark.parametrize(
    "reason",
    [HEADROOM_REACHABLE, HEADROOM_WITHIN_PLATEAU, HEADROOM_PLATEAUED,
     HEADROOM_NO_OBJECTIVES],
)
@pytest.mark.parametrize(
    "headroom_status", list(IterationHeadroom), ids=lambda s: s.value
)
def test_below_the_budget_a_missed_round_still_iterates(headroom_status, reason):
    """#2537's choice, untouched by #2656 on every ending that is not the cap.

    A MISSED round has outstanding targets by construction, so "we stopped
    improving" is not a reason to stop trying — and the plateau stops still
    cannot fire on this row. Walked over BOTH statuses with every non-cap
    reason the axis mints, so a change that widened the new branch from the
    budget to the whole ``EXHAUSTED`` status fails here rather than quietly
    ending a household's series two rounds early.
    """

    decision = _adopt(
        quality=Verdict(QualityStatus.MISSED, ADOPTION_UNPROVEN, {}),
        headroom=Verdict(headroom_status, reason, {}),
    )

    assert decision.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert decision.row == ADOPTION_ROW_KEEP_FOR_ITERATION
    assert decision.reason == ADOPTION_UNPROVEN


def test_a_series_that_keeps_missing_terminates_at_the_budget():
    """The gate's 40-round walk, bounded — and through the REAL axis.

    The two tests above hand ``decide_adoption`` a headroom verdict directly,
    which pins the table and not the composition. This walks the shipped
    :func:`evaluate_iteration_headroom` for each round of a series that keeps
    missing, so what is asserted is what a driver chaining rounds actually
    gets.

    The series is deliberately one that PLATEAUS immediately — every round
    measures the same objectives, so movement is zero from round 2 on. That is
    the second half of the claim: a plateau does not end a MISSED series
    (#2537), and the budget does (#2656), and only walking both together shows
    the two stops did not get folded into one.
    """

    cap = 3
    previous = None
    outcomes = []
    for ordinal in range(1, 8):
        headroom = _headroom(
            report=_round_three_report(), previous=previous,
            ordinal=ordinal, cap=cap,
        )
        decision = _adopt(
            quality=Verdict(QualityStatus.MISSED, ADOPTION_UNPROVEN, {}),
            headroom=headroom,
        )
        outcomes.append(
            (decision.outcome, decision.row, decision.reason, headroom.reason)
        )
        previous = flatness_objectives(_round_three_report())

    ended = [
        ordinal for ordinal, (outcome, _, _, _) in enumerate(outcomes, start=1)
        if outcome is not AdoptionOutcome.KEEP_FOR_ITERATION
    ]
    assert ended and ended[0] == cap, (
        f"a MISSED series must end at round {cap}, ended at {ended[:1] or None}"
    )
    # The plateau ACTUALLY FIRED, asserted rather than assumed. Without this
    # the docstring's second half is narrative only: a broken plateau (a
    # refused floor comparison, a movement that never resolves) leaves every
    # round REACHABLE, and the test still passes on the budget alone — proving
    # half of what it claims while reading like it proved both.
    assert outcomes[1][3] == HEADROOM_PLATEAUED, (
        f"round 2 must be the plateau round, got {outcomes[1][3]!r}"
    )
    # Every round before the cap kept iterating, plateau and all.
    assert all(
        row == ADOPTION_ROW_KEEP_FOR_ITERATION
        for _, row, _, _ in outcomes[: cap - 1]
    )
    # And every round from the cap on says the same thing, so a driver that
    # ignored the first ending is not offered a fresh one afterwards.
    for outcome, row, reason, _ in outcomes[cap - 1:]:
        assert outcome is AdoptionOutcome.KEEP
        assert row == ADOPTION_ROW_KEEP_MISSED_EXHAUSTED
        assert reason == HEADROOM_CAP_REACHED


@pytest.mark.parametrize(
    "headroom_status", list(IterationHeadroom), ids=lambda s: s.value
)
def test_headroom_can_never_keep_a_graph_the_other_axes_took_off(
    headroom_status,
):
    """The safety claim, stated as a test rather than as a docstring.

    Whatever the fourth axis says, an unsafe result, an unmeasured one, and a
    failed restore all still come off or escalate. This is the property that
    makes a bug in the headroom evaluator a cosmetic defect rather than a
    hardware one.
    """

    headroom = Verdict(headroom_status, "h", {})

    unsafe = _adopt(
        safety=Verdict(SafetyStatus.UNSAFE, SAFETY_CLIPPED_CAPTURE, {}),
        headroom=headroom,
    )
    assert unsafe.outcome is AdoptionOutcome.RESTORE
    assert unsafe.row == ADOPTION_ROW_RESTORE_UNSAFE

    untrusted = _adopt(
        trust=Verdict(EvidenceTrust.UNTRUSTED, "no_evidence", {}),
        headroom=headroom,
    )
    assert untrusted.outcome is AdoptionOutcome.RESTORE
    assert untrusted.row == ADOPTION_ROW_RESTORE_UNTRUSTED

    failed = _adopt(headroom=headroom, restore_failed=True)
    assert failed.outcome is AdoptionOutcome.RECOVERY_REQUIRED
    assert failed.row == ADOPTION_ROW_RESTORE_FAILED


def test_a_round_at_the_cap_stops_even_with_everything_left_to_fix():
    """The end-to-end "round 3 of 3" row, through the real evaluator.

    Not ``_adopt`` with a hand-made verdict: the point is that a genuine
    report with 2.37 dB of tilt still on it reaches row 1 when the ordinal
    says there is no fourth round to spend it on.
    """

    decision = decide_adoption(
        trust=Verdict(EvidenceTrust.TRUSTED, TRUST_MEASURED, {}),
        safety=Verdict(SafetyStatus.SAFE, SAFETY_NO_FINDING, {}),
        quality=Verdict(QualityStatus.PASSED, ADOPTION_REALIZED_AND_IMPROVED, {}),
        headroom=_headroom(report=_round_three_report(), ordinal=3),
        boosted=False,
        rollback_available=True,
    )
    assert decision.outcome is AdoptionOutcome.KEEP
    assert decision.row == ADOPTION_ROW_KEEP
    assert decision.reason == HEADROOM_CAP_REACHED


# --------------------------------------------------------------------------
# 6. adoption — the seven rows (#2537, #2602, #2656)
# --------------------------------------------------------------------------


def _adopt(*, trust=None, safety=None, quality=None, headroom=None, **overrides):
    """Default axes: trusted, safe, passed, and NO headroom left.

    ``headroom`` defaults to EXHAUSTED so every row this helper predates keeps
    asking the question it was written to ask. #2602 split the passing cell on
    this axis, and an EXHAUSTED default is the half that behaves exactly as the
    pre-#2602 table did — so a test about trust, safety, or a regression is
    still testing only the thing it names. The rows that ARE about headroom
    pass it explicitly.
    """

    kwargs = {
        "trust": trust or Verdict(EvidenceTrust.TRUSTED, TRUST_MEASURED, {}),
        "safety": safety or Verdict(SafetyStatus.SAFE, SAFETY_NO_FINDING, {}),
        "quality": quality or Verdict(
            QualityStatus.PASSED, ADOPTION_REALIZED_AND_IMPROVED, {}
        ),
        "headroom": headroom or Verdict(
            IterationHeadroom.EXHAUSTED, HEADROOM_WITHIN_PLATEAU, {}
        ),
        "boosted": False,
        "rollback_available": True,
    }
    kwargs.update(overrides)
    return decide_adoption(**kwargs)


def test_row1_trusted_safe_passed_keeps():
    """The terminal keep — and since #2602, the HEADROOM axis names why.

    Amended from ``reason == ADOPTION_REALIZED_AND_IMPROVED``. Row 1 no longer
    means only "this round was good"; it means "this round was good AND the
    series is over", and the reason has to say which of those endings it was so
    the done screen can tell a household "as flat as measuring can show" apart
    from "that was the third round". Quality's own reason did not go anywhere —
    it rides on the quality axis, which the receipt records beside this
    decision. The OUTCOME and the ROW, which are what every other consumer
    keys on, are unchanged.
    """

    decision = _adopt()
    assert decision.outcome is AdoptionOutcome.KEEP
    assert decision.row == ADOPTION_ROW_KEEP
    assert decision.reason == HEADROOM_WITHIN_PLATEAU


def test_row2_trusted_safe_missed_keeps_for_iteration():
    """The measured state STAYS live, and the misses become next-round targets.

    This is the row the owner's ruling created, and the 2026-08-15 JTS3 cycle-4
    round is what lands on it: usable capture, tracked realization, a level
    residual pointing quieter, an unprovable benefit, and a spec band out of
    tolerance.
    """

    decision = _adopt(
        quality=Verdict(
            QualityStatus.MISSED, ADOPTION_UNPROVEN,
            {"targets": [f"spec:{SPEC_BAND_OUT_OF_TOLERANCE}"]},
        ),
    )
    assert decision.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert decision.row == ADOPTION_ROW_KEEP_FOR_ITERATION
    assert decision.reason == ADOPTION_UNPROVEN


def test_row3_unsafe_restores_under_the_hazards_own_name():
    decision = _adopt(
        safety=Verdict(
            SafetyStatus.UNSAFE, SAFETY_BOOST_OVER_DECLARED_BOUND, {}
        ),
    )
    assert decision.outcome is AdoptionOutcome.RESTORE
    assert decision.row == ADOPTION_ROW_RESTORE_UNSAFE
    assert decision.reason == SAFETY_BOOST_OVER_DECLARED_BOUND


def test_row4_untrusted_evidence_restores():
    """An unmeasured applied state cannot be the least bad MEASURED tune."""

    decision = _adopt(
        trust=Verdict(
            EvidenceTrust.UNTRUSTED, CAPTURE_INTEGRITY_FAILED, {}
        ),
    )
    assert decision.outcome is AdoptionOutcome.RESTORE
    assert decision.row == ADOPTION_ROW_RESTORE_UNTRUSTED
    assert decision.reason == CAPTURE_INTEGRITY_FAILED


def test_row5_a_measured_regression_restores():
    """The fifth row, and why it is not a keep_for_iteration.

    The ruling turns on UNKNOWN previous states. A regression is the one case
    where the previous state's own measurement is the evidence, so going back
    goes back to a measured tune.
    """

    decision = _adopt(
        quality=Verdict(
            QualityStatus.REGRESSED, ADOPTION_MEASURED_REGRESSION, {}
        ),
    )
    assert decision.outcome is AdoptionOutcome.RESTORE
    assert decision.row == ADOPTION_ROW_RESTORE_REGRESSION
    assert decision.reason == ADOPTION_MEASURED_REGRESSION


def test_safety_outranks_trust_so_the_row_names_the_hazard():
    """Both restore, so the order only decides which name the receipt carries.

    A clipped capture is BOTH — unusable evidence and a hazard — and naming the
    hazard is the more useful of two true statements.
    """

    decision = _adopt(
        trust=Verdict(EvidenceTrust.UNTRUSTED, CAPTURE_INTEGRITY_FAILED, {}),
        safety=Verdict(SafetyStatus.UNSAFE, SAFETY_CLIPPED_CAPTURE, {}),
    )
    assert decision.outcome is AdoptionOutcome.RESTORE
    assert decision.row == ADOPTION_ROW_RESTORE_UNSAFE
    assert decision.reason == SAFETY_CLIPPED_CAPTURE


def test_trust_outranks_quality():
    decision = _adopt(
        trust=Verdict(EvidenceTrust.UNTRUSTED, REALIZATION_NO_TRACKING, {}),
        quality=Verdict(
            QualityStatus.REGRESSED, ADOPTION_MEASURED_REGRESSION, {}
        ),
    )
    assert decision.row == ADOPTION_ROW_RESTORE_UNTRUSTED


def test_an_unproven_boost_fails_closed_on_the_untrusted_row_only():
    """The pre-#2537 cause, surviving exactly where its argument still holds.

    With no trusted evidence there is nothing to judge a boost by, and "energy
    we put into a driver and cannot justify" is still the honest sentence. With
    trusted evidence the boost is judged realized-vs-declared on the safety
    axis instead — which is what stops a measured, safe, improving candidate
    from being reverted for carrying a boost.
    """

    untrusted = _adopt(
        trust=Verdict(EvidenceTrust.UNTRUSTED, CAPTURE_INTEGRITY_FAILED, {}),
        boosted=True,
    )
    assert untrusted.outcome is AdoptionOutcome.RESTORE
    assert untrusted.reason == ADOPTION_UNPROVEN_BOOST


@pytest.mark.parametrize(
    "quality_status",
    [QualityStatus.PASSED, QualityStatus.MISSED, QualityStatus.REGRESSED],
)
def test_a_boost_never_changes_a_trusted_rounds_answer(quality_status):
    """The regression #2537 exists to fix, pinned as a property.

    On 2026-08-15 a boosted candidate with a usable capture and a tracked
    realization was reverted BECAUSE it carried a boost. With trusted evidence
    the modifier must now be invisible.
    """

    reason = (
        ADOPTION_MEASURED_REGRESSION
        if quality_status is QualityStatus.REGRESSED
        else ADOPTION_UNPROVEN
        if quality_status is QualityStatus.MISSED
        else ADOPTION_REALIZED_AND_IMPROVED
    )
    quality = Verdict(quality_status, reason, {})
    without = _adopt(quality=quality, boosted=False)
    with_boost = _adopt(quality=quality, boosted=True)
    assert with_boost.outcome is without.outcome
    assert with_boost.row == without.row
    assert with_boost.reason == without.reason


def test_a_failed_restore_outranks_every_row():
    for trust, safety, quality in itertools.product(
        EvidenceTrust, SafetyStatus, QualityStatus
    ):
        decision = _adopt(
            trust=Verdict(trust, "t", {}),
            safety=Verdict(safety, "s", {}),
            quality=Verdict(quality, "q", {}),
            restore_failed=True,
        )
        assert decision.outcome is AdoptionOutcome.RECOVERY_REQUIRED
        assert decision.reason == ADOPTION_RESTORE_FAILED
        assert decision.row == ADOPTION_ROW_RESTORE_FAILED


@pytest.mark.parametrize(
    ("trust", "safety", "quality", "row"),
    [
        (
            EvidenceTrust.UNTRUSTED, SafetyStatus.SAFE, QualityStatus.PASSED,
            ADOPTION_ROW_RESTORE_UNTRUSTED,
        ),
        (
            EvidenceTrust.TRUSTED, SafetyStatus.UNSAFE, QualityStatus.PASSED,
            ADOPTION_ROW_RESTORE_UNSAFE,
        ),
        (
            EvidenceTrust.TRUSTED, SafetyStatus.SAFE, QualityStatus.REGRESSED,
            ADOPTION_ROW_RESTORE_REGRESSION,
        ),
    ],
)
def test_a_restore_with_no_anchor_escalates_and_keeps_its_row(
    trust, safety, quality, row
):
    """The row says WHICH rule fired; a missing anchor only stops its execution."""

    decision = _adopt(
        trust=Verdict(trust, "cause", {}),
        safety=Verdict(safety, "cause", {}),
        quality=Verdict(quality, "cause", {}),
        rollback_available=False,
    )
    assert decision.outcome is AdoptionOutcome.RECOVERY_REQUIRED
    assert decision.reason.startswith(f"{ADOPTION_NO_ROLLBACK_ANCHOR}:")
    assert decision.row == row


@pytest.mark.parametrize("rollback", [True, False])
def test_a_keeping_row_never_needs_a_rollback_anchor(rollback):
    for quality_status, reason in (
        (QualityStatus.PASSED, ADOPTION_REALIZED_AND_IMPROVED),
        (QualityStatus.MISSED, ADOPTION_UNPROVEN),
    ):
        decision = _adopt(
            quality=Verdict(quality_status, reason, {}),
            rollback_available=rollback,
        )
        assert decision.outcome in (
            AdoptionOutcome.KEEP, AdoptionOutcome.KEEP_FOR_ITERATION
        )


def test_every_axis_combination_lands_on_exactly_one_known_row():
    """No combination falls through, and no row is invented.

    The pre-#2537 table got exhaustiveness from a dict lookup with no default.
    A guard ladder has to be shown to have it.
    """

    seen = set()
    for (
        trust, safety, quality, headroom, headroom_reason, boosted, rollback
    ) in itertools.product(
        EvidenceTrust, SafetyStatus, QualityStatus, IterationHeadroom,
        # #2656 added the REASON dimension, and it is load-bearing rather than
        # thorough: one row is selected by the headroom axis's reason, so a
        # walk over statuses alone cannot reach it — and would have reported a
        # complete table while a row sat unreachable.
        ("h", HEADROOM_CAP_REACHED),
        (False, True), (False, True),
    ):
        decision = _adopt(
            trust=Verdict(trust, "t", {}),
            safety=Verdict(safety, "s", {}),
            quality=Verdict(quality, "q", {}),
            headroom=Verdict(headroom, headroom_reason, {}),
            boosted=boosted,
            rollback_available=rollback,
        )
        assert decision.row in ADOPTION_ROWS
        assert decision.row != ADOPTION_ROW_RESTORE_FAILED
        seen.add(decision.row)
    # #2602 widened this from four reachable rows to five and #2656 to six: the
    # walk covers the fourth axis, so rows 6 and 7 are reachable and must be
    # REACHED — an unreachable row in the table is a row nothing tests.
    assert seen == ADOPTION_ROWS - {ADOPTION_ROW_RESTORE_FAILED}


def test_a_fourth_quality_member_would_raise_rather_than_fall_through():
    """The exhaustiveness property, checked where it actually lives.

    ``QualityStatus`` has three members today, so no test can construct a
    fourth. What CAN be checked is the structure that makes a fourth fail: a
    mapping with no default. A guard ladder's equivalent would be a trailing
    ``raise`` that ``decide_adoption``'s own type guard makes unreachable —
    a branch this test could not enter either, and one nothing would notice
    had been deleted.

    Mutation-checked: deleting a member from ``_QUALITY_ROWS`` fails this,
    and swapping the ladder back in fails it at import.
    """

    assert set(_QUALITY_ROWS) == set(QualityStatus)
    # And the mapping is the ONLY place a quality answer becomes an outcome —
    # a re-added ``is QualityStatus.X`` branch would be a second owner.
    source = Path(verification_module.__file__).read_text(encoding="utf-8")
    body = source.split("def decide_adoption(", 1)[1].split("\ndef ", 1)[0]
    assert "_QUALITY_ROWS[quality.status]" in body
    assert "is QualityStatus." not in body
    # #2602's second mapping, held to the same rule: the passing cell's split
    # is a lookup with no default, so a third ``IterationHeadroom`` member
    # raises rather than landing on whichever branch came last.
    assert set(_PASSED_ROWS) == set(IterationHeadroom)
    assert "_PASSED_ROWS[headroom.status]" in body
    assert "is IterationHeadroom." not in body


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("trust", Verdict(SafetyStatus.SAFE, "s", {})),
        ("safety", Verdict(EvidenceTrust.TRUSTED, "t", {})),
        ("quality", Verdict(SafetyStatus.SAFE, "s", {})),
        ("trust", EvidenceTrust.TRUSTED),
        ("headroom", Verdict(SafetyStatus.SAFE, "s", {})),
        ("headroom", IterationHeadroom.REACHABLE),
    ],
)
def test_adoption_refuses_an_axis_that_is_not_the_typed_one(field_name, value):
    kwargs = {
        "trust": Verdict(EvidenceTrust.TRUSTED, "t", {}),
        "safety": Verdict(SafetyStatus.SAFE, "s", {}),
        "quality": Verdict(QualityStatus.PASSED, "q", {}),
        "headroom": Verdict(IterationHeadroom.EXHAUSTED, "h", {}),
        "boosted": False,
        "rollback_available": True,
        field_name: value,
    }
    with pytest.raises(CrossoverV2ContractError, match=field_name):
        decide_adoption(**kwargs)


def test_every_non_keep_decision_states_a_reason():
    for trust, safety, quality, boosted, rollback, failed in itertools.product(
        EvidenceTrust, SafetyStatus, QualityStatus,
        (False, True), (False, True), (False, True),
    ):
        decision = _adopt(
            trust=Verdict(trust, "t", {}),
            safety=Verdict(safety, "s", {}),
            quality=Verdict(quality, "q", {}),
            boosted=boosted,
            rollback_available=rollback,
            restore_failed=failed,
        )
        if decision.outcome is not AdoptionOutcome.KEEP:
            assert decision.reason.strip()
        assert decision.row


def test_no_axis_combination_can_keep_a_graph_that_measured_worse():
    """The safety property #2291 established, carried into the new table."""

    for trust, safety in itertools.product(EvidenceTrust, SafetyStatus):
        decision = _adopt(
            trust=Verdict(trust, "t", {}),
            safety=Verdict(safety, "s", {}),
            quality=Verdict(
                QualityStatus.REGRESSED, ADOPTION_MEASURED_REGRESSION, {}
            ),
        )
        assert decision.outcome is not AdoptionOutcome.KEEP
        assert decision.outcome is not AdoptionOutcome.KEEP_FOR_ITERATION


# --------------------------------------------------------------------------
# architecture — dependency direction and purity
# --------------------------------------------------------------------------


#: The strangler destination, module by module. ``verification.py`` was this
#: pin's original subject; the wave-1 engine modules join it because the
#: direction law binds the whole package and they are its newest members. The
#: two assertions below survive that repointing verbatim.
#:
#: A module that lands in the engine adds its name here, in the same PR. This
#: is a SOURCE-TEXT pin, which the charter otherwise forbids: it reads the
#: module's own import lines rather than its behaviour. It is kept because it
#: is the test-side guard on the zero-upward-imports invariant, and because an
#: import that does not exist has no behaviour to observe.
_STRANGLER_DESTINATION_MODULES = (
    "verification.py",
    "session.py",
    "session_seams.py",
    "playback_transaction.py",
    "measure_spec.py",
)


@pytest.mark.parametrize("module", _STRANGLER_DESTINATION_MODULES)
def test_the_evaluator_imports_no_web_host_and_no_legacy_flow(module: str):
    """#2291's dependency direction: domain modules do not import the host,
    and the strangler destination does not import the monolith it replaces."""

    source = (
        Path(__file__).resolve().parents[1]
        / "jasper"
        / "active_speaker"
        / "crossover_v2"
        / module
    ).read_text()
    imports = [
        line
        for line in source.splitlines()
        if re.match(r"\s*(import|from)\s", line) and "#" not in line.split()[0]
    ]
    joined = "\n".join(imports)
    assert "jasper.web" not in joined
    assert "crossover_v2_flow" not in joined


def test_the_direction_pin_still_names_modules_that_exist():
    """Anti-vacuity. A renamed engine module must fail here rather than quietly
    narrowing the scan to the four that still resolve."""
    package = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2"
    )
    missing = [
        name for name in _STRANGLER_DESTINATION_MODULES
        if not (package / name).is_file()
    ]
    assert not missing, missing


def test_the_evaluator_is_pure():
    """Same inputs, identical outputs — no clock, no IO, no hidden state."""

    baseline, post = _comparand(amplitude_db=3.0), _comparand(amplitude_db=0.5)
    first = evaluate_benefit(
        entry_baseline=baseline, post=post, margin_db=MARGIN_DB
    )
    second = evaluate_benefit(
        entry_baseline=baseline, post=post, margin_db=MARGIN_DB
    )
    assert first == second
    assert evaluate_spec(_report()) == evaluate_spec(_report())
    assert _adopt() == _adopt()


# --------------------------------------------------------------------------- #
# The graded FRAME, beside the objectives (#2609 SF5)
# --------------------------------------------------------------------------- #


def _plateau_case(**floors):
    """The exact input that fires the plateau stop, plus the two floors.

    2.37 dB out with 0.03 dB of movement — the shape
    ``test_the_headroom_table``'s ``plateaued`` case pins. Isolating it here
    means the floor tests differ from that case in the floors alone.
    """
    return _headroom(
        report=_round_three_report(),
        previous=FlatnessObjectives(tilt_db=2.40, ripple_db=0.9),
        ordinal=2,
        **floors,
    )


def test_two_rounds_graded_over_different_floors_refuse_the_comparison():
    """#2609 SF5, and the number that forced it.

    Measured on an UNCHANGED curve, a 7 ms vs 10 ms gate alone moves these
    objectives by ±0.518 dB — 2.1x the plateau bar. Differencing across that
    reads a gate-length artefact as progress, or manufactures a plateau out of
    one. The round still runs; only the movement CLAIM is withheld.
    """
    verdict = _plateau_case(floor_hz=100.0, previous_floor_hz=143.0)

    assert verdict.status is IterationHeadroom.REACHABLE
    assert verdict.reason == HEADROOM_REACHABLE
    assert verdict.evidence["movement_comparable"] is False
    # The arithmetic is still reported — withholding the CLAIM is not hiding
    # the number a support read needs.
    assert verdict.evidence["movement_db"] == pytest.approx(0.03)
    assert verdict.evidence["trusted_floor_hz"] == 100.0
    assert verdict.evidence["previous_trusted_floor_hz"] == 143.0


def test_the_same_floor_still_plateaus():
    """The control. Without it the test above would pass on a change that
    disabled the plateau stop outright."""
    verdict = _plateau_case(floor_hz=143.0, previous_floor_hz=143.0)

    assert verdict.status is IterationHeadroom.EXHAUSTED
    assert verdict.reason == HEADROOM_PLATEAUED
    assert verdict.evidence["movement_comparable"] is True


@pytest.mark.parametrize(
    ("case", "floors"),
    [
        ("neither round banked a floor", {}),
        ("only this round did", {"floor_hz": 143.0}),
        ("only the previous round did", {"previous_floor_hz": 143.0}),
        (
            "a float that survived a JSON round trip",
            {"floor_hz": 143.00000000000003, "previous_floor_hz": 143.0},
        ),
    ],
    ids=["neither", "this_only", "previous_only", "json_noise"],
)
def test_only_positive_evidence_of_a_moved_floor_refuses(case, floors):
    """The fail direction, which is the whole design of the check.

    An unknown floor is not evidence that the frame moved. Refusing on it
    would disable the plateau stop the ruling names — on every round until
    every path threads a floor, and forever on a tier that banks none. So the
    refusal needs two KNOWN floors that actually disagree, and nothing else.
    """
    verdict = _plateau_case(**floors)

    assert verdict.status is IterationHeadroom.EXHAUSTED, case
    assert verdict.reason == HEADROOM_PLATEAUED, case
    assert verdict.evidence["movement_comparable"] is True, case


def test_a_non_finite_floor_banks_as_unknown_rather_than_as_a_number():
    """A NaN would compare false against everything, which is a silent
    permanent refusal wearing the costume of a measurement."""
    verdict = _plateau_case(
        floor_hz=float("nan"), previous_floor_hz=float("inf"),
    )

    assert verdict.evidence["trusted_floor_hz"] is None
    assert verdict.evidence["previous_trusted_floor_hz"] is None
    assert verdict.evidence["movement_comparable"] is True


def test_the_floor_is_banked_even_when_it_decides_nothing():
    """The NEXT round is what reads it back, so a first round has to bank it
    too — otherwise round 2 has this round's objectives and no frame to check
    them against, which is the state SF5 exists to end."""
    verdict = _headroom(report=_round_three_report(), floor_hz=143.0)

    assert verdict.status is IterationHeadroom.REACHABLE
    assert verdict.evidence["trusted_floor_hz"] == 143.0
    assert verdict.evidence["previous_trusted_floor_hz"] is None


#: Modules allowed to call through ``EngineSeams``. Exactly one: the session
#: the seams belong to. A burn-down list that must never grow — an entry here
#: would be a front end doing engine work.
_ENGINE_SEAM_CALLERS = frozenset({"jasper/active_speaker/crossover_v2/session.py"})


def _seam_reach_through_sites() -> list[str]:
    """Every ``<x>.seams.<y>`` attribute access under ``jasper/``."""
    root = Path(__file__).resolve().parents[1]
    found: list[str] = []
    for path in sorted((root / "jasper").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in _ENGINE_SEAM_CALLERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "seams"
            ):
                found.append(f"{relative}:{node.lineno}: .seams.{node.attr}")
    return found


def test_no_front_end_reaches_through_the_engine_seams():
    """Wave 2's enforcement pin, landing now that a front end exists.

    ``EngineSeams`` is public because construction and testing need it, and
    engine-INTERNAL because only ``TuningSession`` may call through it. The
    discipline had nothing to point at until the session was constructed in
    production; it does now.

    The failure this prevents is quiet: a host calling
    ``session.seams.records.bank(...)`` banks a record the session never counts
    in ``banked_record_ids`` — evidence on disk that the session denies taking.
    Nothing raises, and every other assertion stays green.

    A source-text pin under the same exception the import-direction guard above
    already records: an access that does not exist has no behaviour to observe,
    and the property is about the SET of accesses rather than any one call.
    """
    offenders = _seam_reach_through_sites()

    assert not offenders, (
        "a front end reaches through EngineSeams — only TuningSession may. "
        "Drive the four verbs instead:\n  " + "\n  ".join(offenders)
    )


def test_the_seam_reach_through_scan_detects_the_shape_it_guards():
    """Anti-vacuity: a guard that cannot see its subject reports silence.

    The construction under test IS the one the scan walks for, planted in a
    throwaway tree — so a scan narrowed to nothing fails here rather than
    letting the pin above pass forever on an empty sweep.
    """
    tree = ast.parse("session.seams.records.bank(record)\n")
    hits = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "seams"
    ]

    assert [node.attr for node in hits] == ["records"]
