"""R17 PR-3: the Fc selector's scoring and adjudication kernel.

Every test here pins a sentence the module or the campaign promises. The
declaration-state pair at the bottom is the load-bearing one: the selector must
behave honestly on the live jts3 declaration TODAY (one candidate, tweeter
named as the blocker) and bloom into a real comparison the moment the operator
widens it, with no other input changed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from jasper.active_speaker import fc_selector as sel
from jasper.active_speaker.crossover_v2_flow import (
    fc_candidate_set,
    resolve_fc_search_band,
)

GRID = np.geomspace(20.0, 20_000.0, 121)
BAND = (1000.0, 4000.0)
JTS3_LIVE_BANDS = {"woofer": (1200.0, 2500.0), "tweeter": (2000.0, 2500.0)}
JTS3_WIDENED_BANDS = {"woofer": (1200.0, 2500.0), "tweeter": (1600.0, 2500.0)}


@dataclass(frozen=True)
class _Curve:
    """A duck-typed stand-in for R16's ``LateralPoseCurve``."""

    role: str
    complex_tf: np.ndarray
    band_hz: tuple[float, float]


def _evaluation(fc_hz=2000.0, *, sum_db=None, headroom_cost_db=0.0,
                refusal=None, band=BAND, operators=None):
    if sum_db is None:
        sum_db = np.zeros_like(GRID)
    if operators is None:
        operators = {
            "woofer": np.ones_like(GRID, dtype=np.complex128),
            "tweeter": np.zeros_like(GRID, dtype=np.complex128),
        }
    return sel.FcCandidateEvaluation(
        fc_hz=fc_hz, freqs_hz=GRID, branch_operator_by_role=operators,
        anchor_sum_db=np.asarray(sum_db, dtype=np.float64),
        scoring_band_hz=band, headroom_cost_db=headroom_cost_db, refusal=refusal,
    )


def _dip(depth_db, lo=1500.0, hi=1800.0):
    curve = np.zeros_like(GRID)
    curve[(GRID >= lo) & (GRID <= hi)] = depth_db
    return curve


# --- band_flatness: R18's borrowed arithmetic --------------------------------


def test_flatness_is_mean_removed_so_a_level_offset_is_not_a_defect():
    """Level is the trim solve's business; this measures WOBBLE only. A
    constant offset must not register, or every candidate would be scored on
    where its overall level happened to land."""
    flat = sel.band_flatness(GRID, np.zeros_like(GRID), BAND)
    offset = sel.band_flatness(GRID, np.zeros_like(GRID) + 7.5, BAND)
    assert flat.worst_db == pytest.approx(0.0, abs=1e-9)
    assert offset.worst_db == pytest.approx(0.0, abs=1e-9)

    shaped = _dip(-5.0)
    assert sel.band_flatness(GRID, shaped, BAND).worst_db == pytest.approx(
        sel.band_flatness(GRID, shaped + 12.0, BAND).worst_db
    )


def test_flatness_keeps_its_sign_because_a_dip_and_a_peak_differ():
    """A dip through a handoff is cancellation; a peak is summation. Reporting
    a magnitude would hide which one a candidate ships."""
    assert sel.band_flatness(GRID, _dip(-5.0), BAND).worst_db < 0.0
    assert sel.band_flatness(GRID, _dip(+5.0), BAND).worst_db > 0.0
    assert sel.band_flatness(GRID, _dip(-5.0), BAND).penalty_db == pytest.approx(
        sel.band_flatness(GRID, _dip(+5.0), BAND).penalty_db
    )


def test_flatness_invents_no_number_for_a_band_the_evidence_lacks():
    assert sel.band_flatness(GRID, np.zeros_like(GRID), None) is None
    assert sel.band_flatness(GRID, np.zeros_like(GRID), (50_000.0, 60_000.0)) is None
    assert sel.band_flatness(GRID, np.full_like(GRID, np.nan), BAND) is None


def test_flatness_reports_where_the_worst_deviation_landed():
    verdict = sel.band_flatness(GRID, _dip(-6.0, 1550.0, 1750.0), BAND)
    assert 1500.0 <= verdict.worst_hz <= 1800.0
    assert verdict.n_bins > 0


# --- predict_pose_sum_db: R16's consumer arithmetic --------------------------


def test_a_half_measured_pose_answers_nothing():
    """Every §4.4 question is a woofer-versus-HF comparison, so a pose missing
    a branch must yield None rather than a one-branch 'sum'."""
    evaluation = _evaluation()
    only_woofer = [_Curve("woofer", np.ones_like(GRID, dtype=np.complex128), (150.0, 4000.0))]
    assert sel.predict_pose_sum_db(evaluation, only_woofer) is None
    assert sel.predict_pose_sum_db(evaluation, []) is None


def test_bins_outside_a_roles_driven_band_contribute_nothing():
    """Outside its sweep a driver was never excited, so the sample is noise;
    summing it would manufacture a handoff out of deconvolution noise."""
    evaluation = _evaluation(
        operators={"woofer": np.ones_like(GRID, dtype=np.complex128)}
    )
    loud_everywhere = np.full_like(GRID, 10.0, dtype=np.complex128)
    narrow = [_Curve("woofer", loud_everywhere, (1000.0, 2000.0))]
    predicted = sel.predict_pose_sum_db(evaluation, narrow)
    outside = (GRID < 1000.0) | (GRID > 2000.0)
    assert np.all(predicted[outside] < -200.0)
    assert np.all(predicted[~outside] > 15.0)


def test_a_pose_sum_composes_both_branches_against_their_operators():
    evaluation = _evaluation(
        operators={
            "woofer": np.full_like(GRID, 1.0, dtype=np.complex128),
            "tweeter": np.full_like(GRID, 1.0, dtype=np.complex128),
        }
    )
    both = [
        _Curve("woofer", np.full_like(GRID, 0.5, dtype=np.complex128), (20.0, 20_000.0)),
        _Curve("tweeter", np.full_like(GRID, 0.5, dtype=np.complex128), (20.0, 20_000.0)),
    ]
    predicted = sel.predict_pose_sum_db(evaluation, both)
    assert np.allclose(predicted, 20.0 * np.log10(1.0))


def test_opposite_operators_cancel_which_is_the_defect_being_hunted():
    """The checkpoint's -4.80 dB dip was summation cancellation. The kernel
    must actually see that, not just carry it."""
    evaluation = _evaluation(
        operators={
            "woofer": np.full_like(GRID, 1.0, dtype=np.complex128),
            "tweeter": np.full_like(GRID, -1.0, dtype=np.complex128),
        }
    )
    both = [
        _Curve("woofer", np.ones_like(GRID, dtype=np.complex128), (20.0, 20_000.0)),
        _Curve("tweeter", np.ones_like(GRID, dtype=np.complex128), (20.0, 20_000.0)),
    ]
    assert np.all(sel.predict_pose_sum_db(evaluation, both) < -200.0)


# --- scoring policy ----------------------------------------------------------


def test_a_flatter_handoff_scores_better():
    shallow = sel.score_candidate(_evaluation(sum_db=_dip(-1.0)), [])
    deep = sel.score_candidate(_evaluation(sum_db=_dip(-6.0)), [])
    assert shallow.score < deep.score


def test_lateral_evidence_can_only_penalise_never_credit():
    """A coarse six-pose gate must not be able to talk a candidate UP; it may
    only reveal that one falls apart off-axis."""
    flat_poses = [[
        _Curve("woofer", np.ones_like(GRID, dtype=np.complex128), (20.0, 20_000.0)),
        _Curve("tweeter", np.zeros_like(GRID, dtype=np.complex128), (20.0, 20_000.0)),
    ]]
    evaluation = _evaluation(sum_db=_dip(-4.0))
    without = sel.score_candidate(evaluation, [])
    with_poses = sel.score_candidate(evaluation, flat_poses)
    assert with_poses.lateral_excess_db >= 0.0
    assert with_poses.score >= without.score
    assert with_poses.n_poses == 1


def test_headroom_is_a_cost_but_a_small_one():
    cheap = sel.score_candidate(_evaluation(sum_db=_dip(-3.0)), [])
    dear = sel.score_candidate(
        _evaluation(sum_db=_dip(-3.0), headroom_cost_db=4.0), []
    )
    assert dear.score > cheap.score
    assert dear.score - cheap.score == pytest.approx(
        4.0 * sel.HEADROOM_COST_WEIGHT
    )


def test_the_beaming_prior_is_not_a_scoring_term():
    """PR-1's settled collision policy: ka bounds what may be PROPOSED, and the
    configured Fc is evaluated even above the ceiling (jts3: 1915.4 vs 2000).
    Scoring it again would charge the configured candidate twice for a bound
    the records call guidance."""
    above_ceiling = sel.score_candidate(_evaluation(fc_hz=2000.0, sum_db=_dip(-3.0)), [])
    below_ceiling = sel.score_candidate(_evaluation(fc_hz=1700.0, sum_db=_dip(-3.0)), [])
    assert above_ceiling.score == pytest.approx(below_ceiling.score)


def test_an_unscoreable_candidate_scores_nothing_rather_than_zero():
    assert sel.score_candidate(_evaluation(refusal=sel.EVAL_REFUSED_UNFITTABLE), []) is None
    assert sel.score_candidate(_evaluation(band=None), []) is None


# --- adjudication (plan §9.8) ------------------------------------------------


def _select(evaluations, poses=(), configured_hz=2000.0, planned=None):
    return sel.select_fc(
        evaluations, poses, configured_hz=configured_hz, limits={},
        planned=len(evaluations) if planned is None else planned,
    )


def test_an_alternative_must_clear_the_margin_to_be_recommended():
    """§9.8: configured is golden until a candidate proves improvement. A
    win smaller than the margin is not a win."""
    barely = _select([
        _evaluation(fc_hz=2000.0, sum_db=_dip(-4.0)),
        _evaluation(fc_hz=1700.0, sum_db=_dip(-3.5)),
    ])
    assert barely.verdict == sel.SELECTION_KEEP_CONFIGURED
    assert barely.kept_configured is True
    assert barely.recommended_hz is None

    clearly = _select([
        _evaluation(fc_hz=2000.0, sum_db=_dip(-4.8)),
        _evaluation(fc_hz=1700.0, sum_db=_dip(-1.0)),
    ])
    assert clearly.verdict == sel.SELECTION_RECOMMEND
    assert clearly.recommended_hz == 1700.0
    assert clearly.margin_db >= sel.MIN_RECOMMENDATION_MARGIN_DB


def test_a_tie_goes_to_the_configured_crossover():
    """The burden of proof is on the change: it costs the household a
    declaration edit and a re-measure."""
    tied = _select([
        _evaluation(fc_hz=2000.0, sum_db=_dip(-3.0)),
        _evaluation(fc_hz=1700.0, sum_db=_dip(-3.0)),
    ])
    assert tied.verdict == sel.SELECTION_KEEP_CONFIGURED
    assert tied.recommended_hz is None


def test_the_configured_candidate_winning_outright_is_an_honest_verdict():
    won = _select([
        _evaluation(fc_hz=2000.0, sum_db=_dip(-0.5)),
        _evaluation(fc_hz=1700.0, sum_db=_dip(-6.0)),
    ])
    assert won.verdict == sel.SELECTION_KEEP_CONFIGURED
    assert won.scores[0].fc_hz == 2000.0


def test_a_refused_candidate_is_disclosed_not_dropped():
    result = _select([
        _evaluation(fc_hz=2000.0, sum_db=_dip(-3.0)),
        _evaluation(fc_hz=1700.0, refusal=sel.EVAL_REFUSED_UNFITTABLE),
        _evaluation(fc_hz=1800.0, refusal=sel.EVAL_REFUSED_BUDGET),
    ])
    assert dict(result.refusals) == {
        1700.0: sel.EVAL_REFUSED_UNFITTABLE, 1800.0: sel.EVAL_REFUSED_BUDGET,
    }
    assert result.evaluated == 1


def test_k_of_n_is_reported_when_the_budget_cut_the_sweep_short():
    """A loaded Pi may score fewer than it proposed. That is disclosed, never
    hidden, and never a session failure."""
    result = _select([_evaluation(fc_hz=2000.0, sum_db=_dip(-3.0))], planned=6)
    assert (result.evaluated, result.planned) == (1, 6)


def test_no_configured_candidate_is_named_as_such_rather_than_guessed():
    orphan = _select([_evaluation(fc_hz=1700.0, sum_db=_dip(-3.0))])
    assert orphan.verdict == sel.SELECTION_NOTHING_TO_COMPARE
    assert orphan.kept_configured is True
    assert orphan.recommended_hz is None


# --- the memory contract (#1894 ruling) --------------------------------------


def test_a_candidate_evaluation_retains_no_analysis_sized_object():
    """The ruling caps the selector at one analysis + 50 MB by releasing each
    candidate's intermediates before the next. This pins the retained half:
    a six-candidate sweep must stay in the tens of kilobytes, and no field may
    reference a ProgramAnalysis / DriverResponse / EnvelopeCurve.
    """
    sweep = [_evaluation(fc_hz=fc) for fc in
             (1648.7, 1698.9, 1750.6, 1803.9, 1858.9, 2000.0)]

    def _arrays(obj):
        for value in vars(obj).values():
            if isinstance(value, np.ndarray):
                yield value
            elif isinstance(value, dict):
                yield from (v for v in value.values() if isinstance(v, np.ndarray))

    total = sum(a.nbytes for e in sweep for a in _arrays(e))
    assert total < 64_000, f"retained sweep grew to {total} bytes"

    forbidden = ("driver_responses", "repeat_responses", "complex_tf", "filters")
    for evaluation in sweep:
        for value in vars(evaluation).values():
            for attribute in forbidden:
                assert not hasattr(value, attribute), (
                    f"an analysis-sized object reached {type(value).__name__}"
                )


def test_retained_growth_is_bounded_per_candidate_not_per_analysis():
    """Growth must be LINEAR in candidates and small — the release points are
    what keep it so. One candidate's retained bytes times six must still be
    the whole sweep's."""
    one = _evaluation()
    per_candidate = sum(
        a.nbytes for a in
        [one.freqs_hz, one.anchor_sum_db, *one.branch_operator_by_role.values()]
    )
    assert per_candidate < 11_000, f"one candidate retains {per_candidate} bytes"


# --- both declaration states (the owner's hardware gate) ---------------------


def _candidates_for(bands):
    band = resolve_fc_search_band(bands)
    return band, fc_candidate_set(
        configured_hz=2000.0, hf_hard_floor_hz=1600.0,
        lower_driver_hard_ceiling_hz=4000.0, search_band_hz=band.band_hz,
        lower_driver_diameter_mm=114.0,
    )


def test_on_the_live_declaration_the_selector_proposes_nothing_and_says_who_blocks():
    """TODAY, unedited: the tweeter's declared [2000, 2500] sits entirely above
    the 1915.4 Hz beaming ceiling, so exactly one candidate exists and the
    honest verdict is keep-configured — with the tweeter named so the operator
    knows which declaration to edit."""
    band, candidates = _candidates_for(JTS3_LIVE_BANDS)
    assert candidates.candidates == (2000.0,)
    assert candidates.alternatives == ()
    assert band.lo_role == "tweeter"

    verdict = _select([_evaluation(fc_hz=2000.0, sum_db=_dip(-4.8))], planned=1)
    assert verdict.kept_configured is True
    assert verdict.recommended_hz is None


def test_widening_the_tweeter_declaration_blooms_the_sweep_with_nothing_else_changed():
    """The same inputs with the tweeter widened to its declared 1600 Hz hard
    floor reopen the downward set — six candidates, spanning the ~1.7 kHz the
    physics prior and the measured dip both point at."""
    _, live = _candidates_for(JTS3_LIVE_BANDS)
    _, widened = _candidates_for(JTS3_WIDENED_BANDS)
    assert len(live.candidates) == 1
    assert widened.candidates == (1648.7, 1698.9, 1750.6, 1803.9, 1858.9, 2000.0)
    assert 2000.0 in widened.candidates, "§9.8: configured is always evaluated"

    # …and the selector can then actually recommend one.
    verdict = _select(
        [_evaluation(fc_hz=fc, sum_db=_dip(-4.8 if fc == 2000.0 else -1.2))
         for fc in widened.candidates],
        planned=len(widened.candidates),
    )
    assert verdict.verdict == sel.SELECTION_RECOMMEND
    assert verdict.recommended_hz in widened.alternatives
    assert verdict.evaluated == len(widened.candidates)
