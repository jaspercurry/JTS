"""The Fc selector's scoring and adjudication kernel, and how a verdict reads.

Every test here pins a sentence the module or the campaign promises: the
borrowed flatness arithmetic, the pose-sum consumer, the scoring policy, §9.8's
adjudication, the retained record's memory contract, and — at the bottom — what
an :class:`~jasper.active_speaker.fc_selector.FcSelection` becomes in the
household-facing envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker import fc_selector as sel
from tests.crossover_v2_fixtures import FakeSeams, _conductor

GRID = np.geomspace(20.0, 20_000.0, 121)
BAND = (1000.0, 4000.0)


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
    """#1675's ruling, kept on this side of the seam: the ka/beaming onset is
    DISCLOSED and never enforced. ``fc_sweep._fc_rejection`` carries no beaming
    term, and the number rides
    :attr:`~jasper.active_speaker.crossover_v2.topology_prescription.TopologyPrescription.beaming_ceiling_hz`
    as disclosure alone. Scoring it here would turn that disclosure into a
    penalty — and on jts3 (ceiling 1915.4, configured 2000) the penalty would
    land on the corner the household actually crosses at."""
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
    assert sel.MIN_RECOMMENDATION_MARGIN_DB == 1.0
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
    assert result.comparison_complete is False
    assert result.attempted == (2000.0, 1700.0)
    assert result.skipped == ((1800.0, sel.EVAL_REFUSED_BUDGET),)


def test_an_incomplete_sweep_cannot_recommend_a_scored_alternative():
    """A budget-short sweep may keep configured, never crown a partial best."""
    result = _select([
        _evaluation(fc_hz=2000.0, sum_db=_dip(-6.0)),
        _evaluation(fc_hz=1700.0, sum_db=_dip(-0.5)),
        _evaluation(fc_hz=1800.0, refusal=sel.EVAL_REFUSED_BUDGET),
    ])
    assert result.comparison_complete is False
    assert result.verdict == sel.SELECTION_KEEP_CONFIGURED
    assert result.recommended_hz is None


def test_missing_candidate_record_makes_the_comparison_incomplete():
    result = _select([_evaluation(fc_hz=2000.0, sum_db=_dip(-3.0))], planned=6)
    assert (result.evaluated, result.planned) == (1, 6)
    assert result.comparison_complete is False
    assert result.recommended_hz is None


def test_attempted_but_invalid_candidate_keeps_the_comparison_complete():
    result = _select([
        _evaluation(fc_hz=2000.0, sum_db=_dip(-3.0)),
        _evaluation(fc_hz=1700.0, refusal=sel.EVAL_REFUSED_UNFITTABLE),
    ])
    assert result.comparison_complete is True
    assert result.attempted == (2000.0, 1700.0)
    assert result.skipped == ()


def test_no_configured_candidate_is_named_as_such_rather_than_guessed():
    orphan = _select([_evaluation(fc_hz=1700.0, sum_db=_dip(-3.0))])
    assert orphan.verdict == sel.SELECTION_NOTHING_TO_COMPARE
    assert orphan.kept_configured is True
    assert orphan.recommended_hz is None


# --- the memory contract (#1894 ruling) --------------------------------------


def test_a_candidate_evaluation_retains_no_analysis_sized_object():
    """The retained record's DECLARED shape, on a constructed instance.

    **Scope, stated because an earlier version of this docstring overclaimed
    it:** these subjects are hand-built, so this cannot see a field that a
    caller populates and the fixture does not — the resilience lens proved
    exactly that by adding an ``analysis`` field, hoarding a full
    ``ProgramAnalysis`` per candidate, and leaving the suite green. What this
    pins is that the shape as declared stays in the tens of kilobytes.

    There is no production counterpart any more: the counterpart walked a real
    corner sweep's records, and the sweep was deleted by ticket 2.3 of
    ``docs/tuning-master-plan.md``. Nothing in the tree builds these records
    today, so the declared shape is the whole of what can be checked.
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
    """Growth must be LINEAR in candidates and small: one record holds a grid,
    an anchor curve and one operator per role, and nothing that scales with the
    analysis behind it. Pinned per record, so N records cost N times this."""
    one = _evaluation()
    per_candidate = sum(
        a.nbytes for a in
        [one.freqs_hz, one.anchor_sum_db, *one.branch_operator_by_role.values()]
    )
    assert per_candidate < 11_000, f"one candidate retains {per_candidate} bytes"


# --- what a verdict becomes for the household --------------------------------
#
# Relocated here when ticket 2.3 of ``docs/tuning-master-plan.md`` deleted the
# corner sweep and with it ``test_crossover_v2_fc_selector_wiring.py``, which
# was the only suite reading these two renderers. ``FcSelection`` is this
# module's own type, so the copy a verdict turns into belongs beside the
# adjudication that produces it. (``tests/test_crossover_envelope_v2.py`` is the
# tidier long-term home; moving them there is the envelope's PR to make.)


def test_no_recommendation_is_rendered_when_no_selection_exists():
    """No session runs a corner sweep any more, so ``fc_selection`` is always
    ``None`` and the summary declines — and the envelope must then print nothing
    at all. "Keep what you have" from a selector that never ran would be a
    verdict nobody reached."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        _fc_recommendation_lines,
    )
    from jasper.web.correction_crossover_v2 import _fc_selection_summary

    fakes = FakeSeams()
    c = _conductor(fakes)
    assert c.fc_selection is None
    assert _fc_selection_summary(c) is None
    assert _fc_recommendation_lines({"crossover_v2": {}}) == []


def test_a_recommending_verdict_says_apply_saves_the_exact_sound_choice():
    from jasper.active_speaker.crossover_envelope_v2 import (
        _fc_recommendation_lines,
    )

    lines = _fc_recommendation_lines({"crossover_v2": {"fc_selection": {
        "verdict": "recommend_alternative",
        "configured_hz": 2000.0, "recommended_hz": 1750.0, "margin_db": 1.4,
        "evaluated": 6, "planned": 6, "limits": {}, "refusals": [], "scores": [],
        "attempted": [2000.0, 1650.0, 1700.0, 1750.0, 1800.0, 1850.0],
        "comparison_complete": True,
    }}})
    joined = " ".join(lines).lower()
    assert "1750 hz" in joined and "2000 hz" in joined
    assert "save this choice in sound" in joined, joined


def test_incomplete_or_legacy_state_never_presents_an_alternative_as_best():
    from jasper.active_speaker.crossover_envelope_v2 import (
        _fc_recommendation_lines,
    )

    selection = {
        "verdict": "recommend_alternative",
        "configured_hz": 2000.0,
        "recommended_hz": 1700.0,
        "evaluated": 2,
        "planned": 6,
        "attempted": [2000.0, 1700.0],
    }
    for completeness in (False, None):
        payload = dict(selection)
        if completeness is not None:
            payload["comparison_complete"] = completeness
        joined = " ".join(_fc_recommendation_lines({
            "crossover_v2": {"fc_selection": payload},
        })).lower()
        assert "incomplete" in joined
        assert "no alternative was selected" in joined
        assert "measured better" not in joined
        assert "best" not in joined


# --- the lateral weight's own claim, checked against the corpus it cites ------

#: The banked rounds the ``LATERAL_ROBUSTNESS_WEIGHT`` comment's numbers are
#: measured over. Named here rather than in the comment so a reader can see
#: exactly what "the banked evidence" is, and so the guard below can recompute
#: it. Laptop-durable and absent in CI, where the guard skips.
_LATERAL_CORPORA = (
    "jts3-incident-20260810-issue2291",
    "xover-armrun-2026-08-18",
    "xover-blenditer-2026-08-18",
    "xover-series1-2026-08-17",
    "xover-series2-2026-08-17",
)

_CAPTURES_ROOT = Path(
    os.environ.get("JTS_CAPTURES_ROOT", "").strip()
    or Path(__file__).resolve().parents[1] / "captures"
)


def _banked_fc_scores() -> list[dict]:
    """Every DISTINCT banked ``fc_selection`` score row, deduplicated.

    Content-hashed rather than path-keyed: the walk banks a ``-preapply`` state
    beside the applied one, so nine of the fourteen blocks appear in two files.
    Counting files would double them, which is exactly how the first version of
    the comment above reported a majority that is not there.
    """
    seen: dict[str, dict] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if {"scores", "comparison_complete", "verdict"} <= set(node):
                seen.setdefault(
                    hashlib.sha256(
                        json.dumps(node, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                    node,
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for corpus in _LATERAL_CORPORA:
        for path in sorted((_CAPTURES_ROOT / corpus).rglob("*.json")):
            try:
                walk(json.loads(path.read_text(errors="replace")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    return [
        row
        for block in seen.values()
        for row in (block.get("scores") or [])
        if isinstance(row, dict) and "lateral_excess_db" in row
    ]


@pytest.mark.skipif(
    not all((_CAPTURES_ROOT / c).is_dir() for c in _LATERAL_CORPORA),
    reason=(
        f"laptop-durable banked corpora absent under {_CAPTURES_ROOT} "
        "(set JTS_CAPTURES_ROOT to point at them)"
    ),
)
def test_the_lateral_term_still_measures_what_this_comment_claims():
    """``LATERAL_ROBUSTNESS_WEIGHT``'s comment states measured numbers, and a
    measured number nothing recomputes is a claim with no keeper.

    It failed that way once already: the first version of that comment counted
    the preapply duplicates and reported the term as largest in a MAJORITY of
    records. It is 35 of 74. This recomputes every figure the comment states, so
    the next person to widen the corpus finds out whether the sentence still
    holds instead of inheriting it.
    """
    rows = _banked_fc_scores()
    assert len(rows) == 74, "the comment's denominator moved; restate it"

    largest = 0
    shares: list[float] = []
    anchor_worst = lateral_peak = raw_peak = 0.0
    for row in rows:
        anchor = abs(float(row["worst_db"]))
        lateral = sel.LATERAL_ROBUSTNESS_WEIGHT * float(row["lateral_excess_db"])
        headroom = sel.HEADROOM_COST_WEIGHT * float(row["headroom_cost_db"])
        anchor_worst = max(anchor_worst, anchor)
        lateral_peak = max(lateral_peak, lateral)
        raw_peak = max(raw_peak, float(row["lateral_excess_db"]))
        if lateral > anchor and lateral > headroom:
            largest += 1
        total = anchor + lateral + headroom
        if total > 0:
            shares.append(lateral / total)

    assert largest == 35
    assert round(lateral_peak, 2) == 18.03
    assert round(raw_peak, 2) == 36.05
    # Pinned at three decimals, not two: the measured value is 4.975, and
    # ``round(4.975, 2)`` is 4.97 under Python's banker's rounding while the
    # comment renders it 4.98 half-up. Asserting the rendering would pin the
    # rounding rule; asserting the measurement pins the fact.
    assert round(anchor_worst, 3) == 4.975
    assert round(100 * sum(shares) / len(shares), 1) == 36.3
    assert round(100 * max(shares), 1) == 91.9
    # The load-bearing direction: NOT a majority. The comment says "35 of 74 —
    # not a majority, but far from a discount", and that reading is the whole
    # reason the weighting question stays open rather than settled.
    assert largest < len(rows) / 2
