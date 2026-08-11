# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The two measurements one correction round compares (#2291 Phase 3c).

:mod:`jasper.active_speaker.crossover_v2.round_evidence` sits between a
capture and a verdict: it reduces one summed at-the-mark
:class:`~jasper.audio_measurement.program_analysis.ProgramAnalysis` to one side
of the before/after benefit comparison, and it makes the ONE assembly decision
:func:`~jasper.active_speaker.crossover_v2.verification.evaluate_benefit`
explicitly leaves to its caller — which exclusion mask both sides are graded
on.

So these tests are about three things, and deliberately not about verdicts
(``tests/test_crossover_v2_verification.py`` owns those):

1. the reduction is the SHIPPED owners' arithmetic, not a second copy;
2. the mask is a UNION applied to BOTH sides, and comparability failures are
   passed through rather than papered over;
3. :data:`~jasper.active_speaker.crossover_v2.round_evidence.MEASURED_BENEFIT_MARGIN_DB`
   is a FORK of ``material_improvement_db``, not an alias — the whole point of
   #2291's ledger item N8 is that the two must be free to move apart.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import round_evidence
from jasper.active_speaker.crossover_v2.contracts import BenefitStatus
from jasper.active_speaker.crossover_v2.round_evidence import (
    BENEFIT_CURVE_MAX_BINS,
    MEASURED_BENEFIT_MARGIN_DB,
    EntryBaseline,
    MeasuredResponse,
    benefit_comparands,
    measured_response_from_analysis,
)
from jasper.active_speaker.crossover_v2.verification import (
    BENEFIT_BASELINE_UNAVAILABLE,
    BENEFIT_GRID_MISMATCH,
    BENEFIT_POST_UNAVAILABLE,
    BENEFIT_PROGRAM_MISMATCH,
    evaluate_benefit,
)

_MARK = "design_axis_mark"


def _grid(n: int = 2048) -> np.ndarray:
    return np.linspace(20.0, 24000.0, n)


def _analysis(
    *,
    program_id: str = "prog-a",
    freqs: np.ndarray | None = None,
    magnitude_db: np.ndarray | None = None,
    validity_floor_hz: float | None = None,
    summed: object | None = ...,  # type: ignore[assignment]
):
    """A ProgramAnalysis-shaped double carrying only what the reducer reads.

    A double rather than a real analysis because building one needs a capture,
    a deconvolution, and a program — none of which this module touches. The
    three attributes read at runtime are named in the reducer's own body
    (``program_id``, ``summed_response``, and the summed response's
    ``freqs_hz``/``magnitude_db``/``validity_floor_hz``), so a double that
    carries exactly those is the whole contract.
    """

    if summed is ...:
        hz = _grid() if freqs is None else freqs
        db = (
            np.sin(np.log10(np.maximum(hz, 1.0)) * 5.0)
            if magnitude_db is None
            else magnitude_db
        )
        summed = SimpleNamespace(
            freqs_hz=hz, magnitude_db=db, validity_floor_hz=validity_floor_hz
        )
    return SimpleNamespace(program_id=program_id, summed_response=summed)


# --------------------------------------------------------------------------- #
# 1. the reduction is the shipped owners'
# --------------------------------------------------------------------------- #


def test_the_curve_is_the_shipped_decimate_then_smooth_not_a_second_copy():
    """Asserted against the owners themselves, so a drifted copy cannot pass.

    ``spec_report_for_predicted_sum`` puts one curve in front of
    ``evaluate_flat_spec`` with exactly these two steps in exactly this order,
    and doing it differently here would grade the round's two captures on a
    different curve from every other spec-graded curve in the subsystem. The
    expected value is computed by CALLING those owners rather than by baking
    numbers, so the pin follows them if they change and fails if this module
    stops using them.
    """
    from jasper.audio_measurement.analysis import smooth_fractional_octave
    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    hz = _grid()
    db = np.sin(np.log10(hz) * 5.0)

    reduced = measured_response_from_analysis(
        _analysis(freqs=hz, magnitude_db=db), reference_mark=_MARK
    )
    assert reduced is not None

    grid, coarse = decimate_curve_to_analysis_grid(
        hz, db, max_bins=BENEFIT_CURVE_MAX_BINS
    )
    expected = smooth_fractional_octave(grid, coarse, fraction=3)

    assert reduced.curve.hz == tuple(float(f) for f in grid)
    assert reduced.curve.db == pytest.approx(list(expected))
    assert len(reduced.curve.hz) <= BENEFIT_CURVE_MAX_BINS


def test_the_program_id_and_mark_ride_through_unchanged():
    """Comparability's two identity fields are carried, never re-derived.

    ``program_id`` equality is the whole comparability check
    (``MeasurementComparand``'s docstring: a SHA-256 over the excitation
    schedule, so equal ids are a cryptographic guarantee of same program AND
    same level). A reducer that minted its own id would make that guarantee
    meaningless.
    """
    reduced = measured_response_from_analysis(
        _analysis(program_id="prog-xyz"), reference_mark=_MARK
    )

    assert reduced is not None
    assert reduced.program_id == "prog-xyz"
    assert reduced.reference_mark == _MARK


@pytest.mark.parametrize(
    "analysis",
    [
        None,
        _analysis(summed=None),
        _analysis(program_id=""),
        # A non-finite bin: ``ResponseCurve`` refuses it rather than hashing
        # it, so the reduction cannot produce a comparand. "Cannot compare
        # this" is an answer, not a crash to propagate into a household
        # decision.
        _analysis(
            freqs=_grid(64),
            magnitude_db=np.concatenate([np.zeros(63), [np.nan]]),
        ),
    ],
    ids=["no_analysis", "no_summed_response", "no_program_id", "non_finite_bin"],
)
def test_an_unreducible_capture_is_none_never_a_raise(analysis):
    assert measured_response_from_analysis(analysis, reference_mark=_MARK) is None


def test_a_curve_too_narrow_to_grade_still_reduces_and_the_evaluator_says_so():
    """The reducer reduces; it does not grade, and it does not pre-judge.

    A two-bin curve reaches no spec band, so there is no pooled residual to
    difference — but that is the EVALUATOR's answer
    (:data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_RESIDUAL_UNEVALUABLE`),
    not a reason for the reducer to withhold the capture. Keeping the two
    responsibilities apart is why "unevaluable" arrives with a reason instead
    of as a missing side.
    """
    from jasper.active_speaker.crossover_v2.verification import (
        BENEFIT_RESIDUAL_UNEVALUABLE,
    )

    reduced = measured_response_from_analysis(
        _analysis(
            freqs=np.array([100.0, 200.0]), magnitude_db=np.array([0.0, 1.0])
        ),
        reference_mark=_MARK,
    )
    assert reduced is not None

    baseline, post = benefit_comparands(baseline=reduced, post=reduced)
    verdict = evaluate_benefit(entry_baseline=baseline, post=post, margin_db=0.5)

    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_RESIDUAL_UNEVALUABLE


# --------------------------------------------------------------------------- #
# 2. the validity clamp
# --------------------------------------------------------------------------- #


def test_bins_below_the_captures_own_validity_floor_are_screened():
    """The same clamp the cloud pipeline unions into its spec mask.

    Below ``gating.f_valid_floor_hz`` the response is an artifact of a
    truncated gate window, so those bins must not decide a verdict either way.
    """
    reduced = measured_response_from_analysis(
        _analysis(validity_floor_hz=500.0), reference_mark=_MARK
    )

    assert reduced is not None
    flagged = [
        hz for hz, excluded in zip(reduced.curve.hz, reduced.excluded) if excluded
    ]
    unflagged = [
        hz for hz, excluded in zip(reduced.curve.hz, reduced.excluded) if not excluded
    ]
    assert flagged, "a 500 Hz floor must screen the bins beneath it"
    assert max(flagged) < 500.0
    assert min(unflagged) >= 500.0


@pytest.mark.parametrize(
    "floor",
    [None, float("nan"), float("inf"), True, "500"],
    ids=["absent", "nan", "inf", "bool", "string"],
)
def test_a_floor_that_is_not_a_finite_number_screens_nothing(floor):
    """"No evidence of a floor" is not "the floor is at zero".

    Over-screening is not the safe direction here: it shrinks the graded
    denominator the whole before/after comparison depends on, which is the
    exact hazard ``spec_convergence_residual`` warns a loop about. ``True`` is
    listed because it is an ``int`` and would otherwise clamp at 1 Hz.
    """
    reduced = measured_response_from_analysis(
        _analysis(validity_floor_hz=floor), reference_mark=_MARK
    )

    assert reduced is not None
    assert not any(reduced.excluded)


# --------------------------------------------------------------------------- #
# 3. the one assembly decision — a shared mask
# --------------------------------------------------------------------------- #


def _measured(program_id: str, hz, db, excluded) -> MeasuredResponse:
    from jasper.active_speaker.crossover_v2.contracts import ResponseCurve

    return MeasuredResponse(
        program_id=program_id,
        reference_mark=_MARK,
        curve=ResponseCurve(hz, db),
        excluded=tuple(excluded),
    )


def test_the_two_sides_are_graded_on_the_union_of_their_screens():
    """Union, applied to BOTH — the decision the evaluator delegates here.

    Equal masks mean equal graded bins by construction, so the residual cannot
    fall merely because the honesty screen grew. Asserted as the union of two
    DIFFERENT single-bin screens, which an intersection (or either side's own
    mask carried through unchanged) cannot satisfy.
    """
    hz, db = [100.0, 200.0, 300.0], [0.0, 1.0, 2.0]
    baseline = _measured("p", hz, db, [True, False, False])
    post = _measured("p", hz, db, [False, True, False])

    got_baseline, got_post = benefit_comparands(baseline=baseline, post=post)

    assert got_baseline is not None and got_post is not None
    assert got_baseline.exclusion_mask == (True, True, False)
    assert got_post.exclusion_mask == (True, True, False)


def test_a_grid_disagreement_reaches_the_evaluator_rather_than_being_resampled():
    """Comparability is the evaluator's answer, never manufactured here.

    A caller that quietly interpolated one side onto the other's grid would be
    inventing the comparability the round exists to check, and the verdict
    would read as a measurement instead of an assumption.
    """
    baseline = _measured("p", [100.0, 200.0], [0.0, 0.0], [False, False])
    post = _measured("p", [100.0, 250.0], [0.0, 0.0], [False, False])

    got_baseline, got_post = benefit_comparands(baseline=baseline, post=post)

    assert got_baseline is not None and got_post is not None
    assert got_baseline.curve.hz != got_post.curve.hz
    verdict = evaluate_benefit(
        entry_baseline=got_baseline, post=got_post, margin_db=0.5
    )
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_GRID_MISMATCH


def test_a_program_disagreement_is_named_by_the_evaluator_not_hidden():
    """The 2026-08-10 shape: a "before" that was never the same question."""
    hz, db = [100.0, 200.0], [0.0, 0.0]
    baseline = _measured("prog-old", hz, db, [False, False])
    post = _measured("prog-new", hz, db, [False, False])

    got_baseline, got_post = benefit_comparands(baseline=baseline, post=post)
    verdict = evaluate_benefit(
        entry_baseline=got_baseline, post=got_post, margin_db=0.5
    )

    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_PROGRAM_MISMATCH


@pytest.mark.parametrize(
    ("drop", "reason"),
    [("baseline", BENEFIT_BASELINE_UNAVAILABLE), ("post", BENEFIT_POST_UNAVAILABLE)],
)
def test_a_missing_side_is_indeterminate_and_says_which_side(drop, reason):
    hz, db = [100.0, 200.0], [0.0, 0.0]
    present = _measured("p", hz, db, [False, False])

    got_baseline, got_post = benefit_comparands(
        baseline=None if drop == "baseline" else present,
        post=None if drop == "post" else present,
    )
    verdict = evaluate_benefit(
        entry_baseline=got_baseline, post=got_post, margin_db=0.5
    )

    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == reason


# --------------------------------------------------------------------------- #
# 4. the persisted entry baseline
# --------------------------------------------------------------------------- #


def test_the_entry_baseline_round_trips_through_the_durable_shape():
    """It crosses the stage bridge as JSON; nothing may be lost on the way.

    Every field, exhaustively — a partial round-trip is how a curve arrives in
    stage 2 with its mask silently reset to all-false, which would grade the
    two captures over different bins while looking comparable.
    """
    reduced = measured_response_from_analysis(
        _analysis(validity_floor_hz=300.0), reference_mark=_MARK
    )
    assert reduced is not None
    original = EntryBaseline.from_measurement(
        reduced,
        graph_fingerprint="graph-fp",
        captured_at="2026-08-11T00:00:00Z",
        artifact_ref="entry_baseline_a01",
    )

    rehydrated = EntryBaseline.from_dict(original.to_dict())

    assert rehydrated == original
    assert rehydrated is not None
    assert rehydrated.as_measurement() == reduced


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        "not a mapping",
        {"freqs_hz": [1.0], "magnitude_db": [0.0], "excluded": [False, False]},
        {"freqs_hz": [1.0], "magnitude_db": [0.0]},
        {"freqs_hz": [1.0], "magnitude_db": [0.0], "excluded": [False]},
        {
            "freqs_hz": [1.0],
            "magnitude_db": [float("nan")],
            "excluded": [False],
            "program_id": "p",
            "reference_mark": "m",
            "graph_fingerprint": "g",
            "captured_at": "t",
        },
    ],
    ids=[
        "none",
        "empty",
        "not_a_mapping",
        "mask_length_mismatch",
        "no_mask",
        "no_identity_fields",
        "non_finite_level",
    ],
)
def test_anything_this_build_did_not_write_rehydrates_as_no_baseline(record):
    """``None``, never a partially-trusted record and never a raise.

    A state file from before this key shipped, a truncated write, and a
    hand-edited file all mean one thing to the round — there is no comparable
    baseline — and that already has an honest verdict.
    """
    assert EntryBaseline.from_dict(record) is None


# --------------------------------------------------------------------------- #
# 5. the margin is a fork, not an alias (#2291 ledger item N8)
# --------------------------------------------------------------------------- #


def test_the_benefit_margin_does_not_move_when_the_model_constant_moves():
    """The fork, pinned as a property rather than as two equal numbers.

    ``material_improvement_db()`` bounds model-vs-hardware error; this bounds
    capture repeatability. They agree today, which is exactly why an equality
    assertion would be worthless — it would pass just as happily if someone
    "simplified" this constant into an import of that one. So the pin is
    behavioural: move the model constant and this must not follow.
    """
    from jasper.active_speaker import attempts_loop

    before = round_evidence.MEASURED_BENEFIT_MARGIN_DB
    assert attempts_loop.material_improvement_db() == pytest.approx(before)

    import jasper.active_speaker.crossover_v2_flow as flow

    original = flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB
    try:
        flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = original + 1.0
        assert attempts_loop.material_improvement_db() == pytest.approx(original + 1.0)
        assert round_evidence.MEASURED_BENEFIT_MARGIN_DB == pytest.approx(before)
    finally:
        flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = original


def test_the_margin_is_positive_and_the_evaluator_accepts_it():
    """A margin the evaluator would refuse is a margin nothing can use.

    ``evaluate_benefit`` validates ``margin_db`` through the contracts
    module's positive-dB rule, so this both pins the sign and proves the two
    modules agree about what a margin is.
    """
    side = measured_response_from_analysis(_analysis(), reference_mark=_MARK)
    assert side is not None
    baseline, post = benefit_comparands(baseline=side, post=side)

    verdict = evaluate_benefit(
        entry_baseline=baseline, post=post, margin_db=MEASURED_BENEFIT_MARGIN_DB
    )

    assert MEASURED_BENEFIT_MARGIN_DB > 0.0
    # The same capture on both sides: exactly zero improvement, which is
    # inside any positive margin — so the round declines to claim a change it
    # did not measure, and the margin it declined against is disclosed.
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.evidence["improvement_db"] == pytest.approx(0.0)
    assert verdict.evidence["margin_db"] == pytest.approx(MEASURED_BENEFIT_MARGIN_DB)
