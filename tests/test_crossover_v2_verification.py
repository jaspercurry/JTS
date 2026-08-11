# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 3b — the four verdicts and the adoption table.

Every row of the issue's adoption table is a named test here, and so is
every override rule stated beside it: a realization pass never beats a
measured regression, improved-but-out-of-spec is a legal keep, indeterminate
never claims success, an unproven boost fails closed, and a restore that
cannot be performed escalates instead of quietly keeping the graph.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.contracts import (
    AdoptionOutcome,
    BenefitStatus,
    CaptureValidity,
    CrossoverV2ContractError,
    RealizationStatus,
    ResponseCurve,
    SpecStatus,
)
from jasper.active_speaker.crossover_v2.verification import (
    ADOPTION_MEASURED_REGRESSION,
    ADOPTION_NO_ROLLBACK_ANCHOR,
    ADOPTION_REALIZATION_FAILED,
    ADOPTION_REALIZATION_UNAVAILABLE,
    ADOPTION_REALIZED_AND_IMPROVED,
    ADOPTION_RESTORE_FAILED,
    ADOPTION_UNPROVEN,
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
    REALIZATION_NO_COMPARATOR,
    REALIZATION_NO_TRACKING,
    REALIZATION_OUT_OF_TOLERANCE,
    REALIZATION_WITHIN_TOLERANCE,
    SPEC_BAND_OUT_OF_TOLERANCE,
    SPEC_IN_TOLERANCE,
    SPEC_NO_EVALUABLE_BAND,
    SPEC_NO_REPORT,
    SPEC_PARTIAL_COVERAGE,
    TRACKING_COMPARATOR_KEY,
    MeasurementComparand,
    Verdict,
    _ADOPTION_TABLE,
    decide_adoption,
    evaluate_benefit,
    evaluate_capture_validity,
    evaluate_realization,
    evaluate_spec,
    verification_result,
)
from jasper.active_speaker.flat_spec import (
    BandResult,
    FlatSpecReport,
    evaluate_flat_spec,
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
# 5. adoption — the issue's table, row by row
# --------------------------------------------------------------------------


def _adopt(realization, benefit, **overrides):
    kwargs = {
        "realization": realization,
        "benefit": benefit,
        "spec": SpecStatus.FAILED,
        "boosted": False,
        "rollback_available": True,
    }
    kwargs.update(overrides)
    return decide_adoption(**kwargs)


def test_row_matched_improved_keeps():
    """``matched | improved | pass or fail | keep`` — the only keep row."""

    decision = _adopt(RealizationStatus.MATCHED, BenefitStatus.IMPROVED)
    assert decision.outcome is AdoptionOutcome.KEEP
    assert decision.reason == ADOPTION_REALIZED_AND_IMPROVED


@pytest.mark.parametrize(
    "benefit",
    [BenefitStatus.IMPROVED, BenefitStatus.REGRESSED, BenefitStatus.INDETERMINATE],
)
def test_row_realization_failed_restores_whatever_the_benefit_says(benefit):
    """``failed | any | any | restore``."""

    decision = _adopt(RealizationStatus.FAILED, benefit)
    assert decision.outcome is AdoptionOutcome.RESTORE
    assert decision.reason == ADOPTION_REALIZATION_FAILED


@pytest.mark.parametrize(
    "realization", [RealizationStatus.MATCHED, RealizationStatus.UNAVAILABLE]
)
def test_row_clear_regression_restores(realization):
    """``matched or unavailable | clearly regressed | any | restore``."""

    decision = _adopt(realization, BenefitStatus.REGRESSED)
    assert decision.outcome is AdoptionOutcome.RESTORE
    assert decision.reason == ADOPTION_MEASURED_REGRESSION


def test_row_unavailable_indeterminate_asks_rather_than_claiming_success():
    """``unavailable | indeterminate | any | do not claim success``."""

    decision = _adopt(RealizationStatus.UNAVAILABLE, BenefitStatus.INDETERMINATE)
    assert decision.outcome is AdoptionOutcome.USER_DECISION
    assert decision.reason == ADOPTION_UNPROVEN


def test_row_unavailable_improved_does_not_claim_the_graph_is_why():
    """The keep row names ``matched``; this table does not widen it.

    And the cause names the *realization* gap, not the benefit: the speaker
    did measure better, so a receipt reading "benefit unproven" here would
    be false.
    """

    decision = _adopt(RealizationStatus.UNAVAILABLE, BenefitStatus.IMPROVED)
    assert decision.outcome is AdoptionOutcome.USER_DECISION
    assert decision.reason == ADOPTION_REALIZATION_UNAVAILABLE
    assert decision.reason != ADOPTION_UNPROVEN


def test_row_restore_attempted_but_failed_requires_recovery():
    """``restore attempted but failed | any | any | recovery required``."""

    decision = _adopt(
        RealizationStatus.MATCHED, BenefitStatus.IMPROVED, restore_failed=True
    )
    assert decision.outcome is AdoptionOutcome.RECOVERY_REQUIRED
    assert decision.reason == ADOPTION_RESTORE_FAILED


def test_a_failed_restore_outranks_every_other_row():
    for realization, benefit, spec, boosted, rollback in itertools.product(
        RealizationStatus, BenefitStatus, SpecStatus, (False, True), (False, True)
    ):
        decision = decide_adoption(
            realization=realization,
            benefit=benefit,
            spec=spec,
            boosted=boosted,
            rollback_available=rollback,
            restore_failed=True,
        )
        assert decision.outcome is AdoptionOutcome.RECOVERY_REQUIRED


# -- the named override rules ----------------------------------------------


def test_a_realization_pass_never_overrides_a_measured_regression():
    """The 2026-08-10 shape, and the reason the table is keyed on both."""

    decision = _adopt(RealizationStatus.MATCHED, BenefitStatus.REGRESSED)
    assert decision.outcome is not AdoptionOutcome.KEEP
    assert decision.outcome is AdoptionOutcome.RESTORE


@pytest.mark.parametrize("spec", list(SpecStatus))
def test_improved_but_out_of_spec_is_a_valid_keep(spec):
    """#2291's named valid first pass: realized, improved, not yet in spec."""

    decision = _adopt(
        RealizationStatus.MATCHED, BenefitStatus.IMPROVED, spec=spec
    )
    assert decision.outcome is AdoptionOutcome.KEEP


@pytest.mark.parametrize("realization", list(RealizationStatus))
@pytest.mark.parametrize("spec", list(SpecStatus))
@pytest.mark.parametrize("boosted", [False, True])
def test_indeterminate_evidence_never_claims_success(realization, spec, boosted):
    decision = _adopt(
        realization,
        BenefitStatus.INDETERMINATE,
        spec=spec,
        boosted=boosted,
    )
    assert decision.outcome is not AdoptionOutcome.KEEP


def test_spec_never_changes_the_adoption_decision():
    """Every row of the issue's table reads "any" for spec."""

    for realization, benefit, boosted, rollback in itertools.product(
        RealizationStatus, BenefitStatus, (False, True), (False, True)
    ):
        outcomes = {
            decide_adoption(
                realization=realization,
                benefit=benefit,
                spec=spec,
                boosted=boosted,
                rollback_available=rollback,
            ).outcome
            for spec in SpecStatus
        }
        assert len(outcomes) == 1, (realization, benefit, boosted, rollback)


@pytest.mark.parametrize("realization", list(RealizationStatus))
def test_an_unproven_boost_fails_closed_instead_of_asking(realization):
    """Energy we put into a driver and cannot show was warranted comes off.

    Whatever realization said. When realization *failed*, that row was
    already restoring and states its own, more specific cause — the boost
    modifier only has to promote the rows that would otherwise have asked.
    """

    decision = _adopt(realization, BenefitStatus.INDETERMINATE, boosted=True)
    assert decision.outcome is AdoptionOutcome.RESTORE
    assert decision.outcome is not AdoptionOutcome.USER_DECISION
    expected = (
        ADOPTION_REALIZATION_FAILED
        if realization is RealizationStatus.FAILED
        else ADOPTION_UNPROVEN_BOOST
    )
    assert expected in decision.reason


@pytest.mark.parametrize("rollback", [True, False])
def test_a_boosted_but_measured_improvement_is_not_an_unproven_boost(rollback):
    """``(unavailable, improved, boosted)`` — the benefit was MEASURED.

    The fail-closed modifier is scoped to indeterminate benefit, so this
    cell lands exactly where its unboosted sibling does: ``user_decision``,
    cause ``realization_unavailable``. Restoring it under
    ``unproven_boost_failed_closed`` would put a false statement on a
    receipt — the benefit is not unproven, only unattributed — and with no
    rollback anchor it would escalate a round that measured *better* to
    ``recovery_required``.
    """

    decision = _adopt(
        RealizationStatus.UNAVAILABLE,
        BenefitStatus.IMPROVED,
        boosted=True,
        rollback_available=rollback,
    )
    assert decision.outcome is AdoptionOutcome.USER_DECISION
    assert decision.reason == ADOPTION_REALIZATION_UNAVAILABLE
    assert ADOPTION_UNPROVEN_BOOST not in decision.reason
    unboosted = _adopt(
        RealizationStatus.UNAVAILABLE,
        BenefitStatus.IMPROVED,
        boosted=False,
        rollback_available=rollback,
    )
    assert decision == unboosted


def test_the_boost_modifier_reads_only_the_indeterminate_cells():
    """Scope check across the whole surface: a boost changes the answer
    only where the benefit is indeterminate."""

    for realization, benefit, rollback in itertools.product(
        RealizationStatus, BenefitStatus, (False, True)
    ):
        if benefit is BenefitStatus.INDETERMINATE:
            continue
        boosted = _adopt(
            realization, benefit, boosted=True, rollback_available=rollback
        )
        plain = _adopt(
            realization, benefit, boosted=False, rollback_available=rollback
        )
        assert boosted == plain, (realization, benefit, rollback)


def test_an_unproven_cut_asks_the_household_rather_than_restoring():
    decision = _adopt(
        RealizationStatus.MATCHED, BenefitStatus.INDETERMINATE, boosted=False
    )
    assert decision.outcome is AdoptionOutcome.USER_DECISION


def test_an_unproven_boost_with_no_rollback_anchor_escalates():
    """A restore the host cannot perform is never reported as a restore,
    and never silently kept."""

    decision = _adopt(
        RealizationStatus.MATCHED,
        BenefitStatus.INDETERMINATE,
        boosted=True,
        rollback_available=False,
    )
    assert decision.outcome is AdoptionOutcome.RECOVERY_REQUIRED
    assert ADOPTION_NO_ROLLBACK_ANCHOR in decision.reason
    assert ADOPTION_UNPROVEN_BOOST in decision.reason


@pytest.mark.parametrize(
    ("realization", "benefit"),
    [
        (RealizationStatus.MATCHED, BenefitStatus.REGRESSED),
        (RealizationStatus.UNAVAILABLE, BenefitStatus.REGRESSED),
        (RealizationStatus.FAILED, BenefitStatus.IMPROVED),
    ],
)
def test_a_regression_with_no_rollback_anchor_escalates(realization, benefit):
    decision = _adopt(realization, benefit, rollback_available=False)
    assert decision.outcome is AdoptionOutcome.RECOVERY_REQUIRED
    assert decision.outcome is not AdoptionOutcome.KEEP


def test_no_combination_can_keep_without_measured_improvement():
    """The safety claim in one sweep: KEEP requires matched + improved."""

    for realization, benefit, spec, boosted, rollback, failed in itertools.product(
        RealizationStatus,
        BenefitStatus,
        SpecStatus,
        (False, True),
        (False, True),
        (False, True),
    ):
        decision = decide_adoption(
            realization=realization,
            benefit=benefit,
            spec=spec,
            boosted=boosted,
            rollback_available=rollback,
            restore_failed=failed,
        )
        if decision.outcome is AdoptionOutcome.KEEP:
            assert realization is RealizationStatus.MATCHED
            assert benefit is BenefitStatus.IMPROVED
            assert not failed


def test_the_table_covers_every_realization_and_benefit_pair():
    """No combination falls through to a default, and a new enum member
    fails here rather than landing quietly on an existing row."""

    assert set(_ADOPTION_TABLE) == set(
        itertools.product(RealizationStatus, BenefitStatus)
    )


def test_every_table_cell_states_its_own_cause():
    """The reason rides in the cell, so there is no second table to drift."""

    for cell, (_intent, reason) in _ADOPTION_TABLE.items():
        assert reason.strip(), cell


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("realization", "matched"), ("benefit", "improved"), ("spec", "passed")],
)
def test_adoption_refuses_a_status_that_is_not_the_typed_one(field_name, value):
    kwargs = {
        "realization": RealizationStatus.MATCHED,
        "benefit": BenefitStatus.IMPROVED,
        "spec": SpecStatus.FAILED,
        "boosted": False,
        "rollback_available": True,
        field_name: value,
    }
    with pytest.raises(CrossoverV2ContractError, match=field_name):
        decide_adoption(**kwargs)


def test_every_non_keep_decision_states_a_reason():
    for realization, benefit, boosted, rollback, failed in itertools.product(
        RealizationStatus, BenefitStatus, (False, True), (False, True), (False, True)
    ):
        decision = decide_adoption(
            realization=realization,
            benefit=benefit,
            spec=SpecStatus.FAILED,
            boosted=boosted,
            rollback_available=rollback,
            restore_failed=failed,
        )
        if decision.outcome is not AdoptionOutcome.KEEP:
            assert decision.reason.strip()


# --------------------------------------------------------------------------
# architecture — dependency direction and purity
# --------------------------------------------------------------------------


def test_the_evaluator_imports_no_web_host_and_no_legacy_flow():
    """#2291's dependency direction: domain modules do not import the host,
    and the strangler destination does not import the monolith it replaces."""

    source = (
        Path(__file__).resolve().parents[1]
        / "jasper"
        / "active_speaker"
        / "crossover_v2"
        / "verification.py"
    ).read_text()
    imports = [
        line
        for line in source.splitlines()
        if re.match(r"\s*(import|from)\s", line) and "#" not in line.split()[0]
    ]
    joined = "\n".join(imports)
    assert "jasper.web" not in joined
    assert "crossover_v2_flow" not in joined


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
    assert _adopt(RealizationStatus.MATCHED, BenefitStatus.IMPROVED) == _adopt(
        RealizationStatus.MATCHED, BenefitStatus.IMPROVED
    )
