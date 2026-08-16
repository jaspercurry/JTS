# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 3b's four verdicts, and #2537's three axes and five rows.

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
import re
import types
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.contracts import (
    ADOPTION_ROW_KEEP,
    ADOPTION_ROW_KEEP_FOR_ITERATION,
    ADOPTION_ROW_KEEP_ITERATING,
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
    VERDICT_LEVEL_MISMATCH as DELTA_VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED as DELTA_VERDICT_MATCHED,
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
    REALIZATION_NO_COMPARATOR,
    REALIZATION_NO_TRACKING,
    REALIZATION_OUT_OF_TOLERANCE,
    REALIZATION_WITHIN_TOLERANCE,
    SAFETY_BOOST_OVER_DECLARED_BOUND,
    SAFETY_CLIPPED_CAPTURE,
    SAFETY_NO_FINDING,
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
    ):
        self.verdict = verdict
        self.reason = reason
        self.residual_offset_db = residual_offset_db
        self.residual_offset_tolerance_db = residual_offset_tolerance_db
        self.boost_over_declared_bound = boost_over_declared_bound
        self.boost_overshoot_db = boost_overshoot_db


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
        ),
        types.SimpleNamespace(
            f_lo_hz=250.0, f_hi_hz=2000.0, tolerance_db=1.5, evaluable=True,
            passed=False, max_deviation_db=4.70, max_deviation_hz=331.8,
        ),
        types.SimpleNamespace(
            f_lo_hz=2000.0, f_hi_hz=8000.0, tolerance_db=2.0, evaluable=True,
            passed=True, max_deviation_db=0.4, max_deviation_hz=4000.0,
        ),
        types.SimpleNamespace(
            f_lo_hz=8000.0, f_hi_hz=16000.0, tolerance_db=2.0, evaluable=True,
            passed=False, max_deviation_db=-2.63, max_deviation_hz=14072.0,
        ),
    ))


# --------------------------------------------------------------------------
# 6. adoption — the six rows (#2537, #2602)
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
    for trust, safety, quality, headroom, boosted, rollback in itertools.product(
        EvidenceTrust, SafetyStatus, QualityStatus, IterationHeadroom,
        (False, True), (False, True),
    ):
        decision = _adopt(
            trust=Verdict(trust, "t", {}),
            safety=Verdict(safety, "s", {}),
            quality=Verdict(quality, "q", {}),
            headroom=Verdict(headroom, "h", {}),
            boosted=boosted,
            rollback_available=rollback,
        )
        assert decision.row in ADOPTION_ROWS
        assert decision.row != ADOPTION_ROW_RESTORE_FAILED
        seen.add(decision.row)
    # #2602 widened this from four reachable rows to five: the walk now covers
    # the fourth axis, so row 6 is reachable and must be REACHED — an
    # unreachable row in the table is a row nothing tests.
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
    assert _adopt() == _adopt()
