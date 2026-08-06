"""R17 PR-3: the Fc selector's CONDUCTOR WIRING.

The kernel's own arithmetic is pinned in ``test_crossover_v2_fc_selector``.
These are the seam guards — the declaration plumbing, the evaluate-and-release
loop's bounds, and the promise that a session which runs the selector publishes
exactly what a session without it would.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.branch_chain import CrossoverSection
from jasper.active_speaker.crossover_v2_flow import (
    PHASE_MEASURE,
    _FC_SWEEP_CONDUCTOR_FIELDS,
)
from tests.test_crossover_v2_conductor import (
    FC_HZ,
    FakeSeams,
    _conductor,
    _eligible_measure_analysis,
    _preset,
    _run_phase,
)
from tests.test_crossover_v2_lateral_evidence import (
    FIRST_LATERAL_INDEX,
    LAST_LATERAL_INDEX,
    _lateral_conductor,
)

# The live jts3 declaration at the R17 STOP: the tweeter's own search band
# starts AT the configured Fc, so the intersection admits no alternative.
LIVE_BANDS = {"woofer": (200.0, 2500.0), "tweeter": (2000.0, 2500.0)}
# …and the same speaker after the owner widens the tweeter to its declared
# 1600 Hz hard floor, which is what makes a real comparison possible.
WIDENED_BANDS = {"woofer": (200.0, 2500.0), "tweeter": (1600.0, 2500.0)}
PROTECTION = {"woofer": (), "tweeter": ()}


def _selector_conductor(fakes: FakeSeams, bands=WIDENED_BANDS, **kwargs):
    return _lateral_conductor(
        fakes,
        measurement_protection_sections_by_role=dict(PROTECTION),
        crossover_search_band_hz_by_role=dict(bands),
        radiating_diameter_mm_by_role={"woofer": 114.0},
        **kwargs,
    )


def _eligible_seams() -> FakeSeams:
    """Seams whose MEASURE analyses clear the Layer-1a eligibility gate, so the
    sweep actually fits candidates rather than refusing every one."""
    fakes = FakeSeams()
    # ``_eligible_measure_analysis`` is not composed by construction (no real
    # capture ran), and ``_build_candidate`` refuses an uncomposed capture on
    # the protected-neutral path — so the fixture states the composition the
    # production analyzer would have performed.
    fakes.measure = lambda program, **kw: replace(
        _eligible_measure_analysis(program, **kw), configured_path_composed=True,
    )
    return fakes


# --- the declaration reaches the candidate set --------------------------------


def test_the_declared_search_band_is_intersected_across_participating_roles():
    """PR-2's rule, now with a conductor that can actually read a declaration.

    A two-way Fc puts BOTH drivers at Fc, so the binding band is the
    intersection — and on the live jts3 declaration that leaves the configured
    Fc alone, with the tweeter named as the edge owner.
    """
    fakes = _eligible_seams()
    live = _selector_conductor(fakes, bands=LIVE_BANDS)._fc_candidate_set()
    widened = _selector_conductor(fakes, bands=WIDENED_BANDS)._fc_candidate_set()

    assert live.candidates == (FC_HZ,), "the tweeter's floor must bind"
    assert live.alternatives == ()
    assert widened.alternatives, "widening the declaration must propose"
    # Every PROPOSAL clears the declared floor strictly. The configured Fc is
    # excluded from this check on purpose — §9.8 keeps it in the set even when
    # a bound would exclude it, which is exactly the case here: this fixture's
    # configured 1600 Hz sits ON the widened floor, and the live jts3 speaker
    # has the same shape against its beaming ceiling.
    assert all(fc > WIDENED_BANDS["tweeter"][0] for fc in widened.alternatives)
    assert FC_HZ in widened.candidates
    assert widened.limits["search_lo_hz"] == WIDENED_BANDS["tweeter"][0]


def test_an_undeclared_search_band_proposes_nothing_rather_than_everything():
    """Fail-closed. A role that declared nothing has told us nothing about
    where it may be crossed — never "this role permits anything"."""
    fakes = _eligible_seams()
    partial = _selector_conductor(
        fakes, bands={"woofer": (200.0, 2500.0), "tweeter": None},
    )
    assert partial._fc_candidate_set().alternatives == ()
    # …and a conductor handed no map at all is the same case, not a bypass.
    assert _lateral_conductor(fakes)._fc_candidate_set().alternatives == ()


def test_the_search_band_resolver_reads_the_confirmed_profile_fail_soft():
    from jasper.active_speaker.excitation_safety_plan import (
        resolve_driver_crossover_search_band_hz as resolve,
    )

    profile = {
        "targets": [
            {"target_fingerprint": "fp-w", "crossover_search_band_hz": [200, 2500]},
            {"target_fingerprint": "fp-t", "crossover_search_band_hz": "nonsense"},
            {"target_fingerprint": "fp-x"},
        ],
    }
    assert resolve(profile, "fp-w") == (200.0, 2500.0)
    # Malformed, absent, and an unknown target all decline rather than raise:
    # this bounds a PROPOSAL, and refusing a measurement session over it would
    # be the wrong direction (its sibling measurement-band resolver raises).
    assert resolve(profile, "fp-t") is None
    assert resolve(profile, "fp-x") is None
    assert resolve(profile, "fp-missing") is None


# --- the candidate priors ------------------------------------------------------


def test_candidate_priors_move_three_fc_fields_and_carry_the_other_two():
    """Polarity and the protection map are preset/safety-derived: they do not
    move when the crossover corner does, and dropping either trips
    ``_compose_configured_path_ir``'s all-or-none rule."""
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)
    base = c._measure_priors()
    sections = c._fc_candidate_sections(1750.0)
    candidate = c._fc_candidate_priors(1750.0, sections)

    assert candidate.crossover_fc_hz == 1750.0 != base.crossover_fc_hz
    freqs = np.array([1000.0, 1750.0, 4000.0])
    for role in ("woofer", "tweeter"):
        moved = candidate.configured_crossover_response_by_role[role](freqs)
        assert not np.allclose(
            moved, base.configured_crossover_response_by_role[role](freqs)
        ), role
        assert candidate.candidate_required_band_hz_by_role[role] != (
            base.candidate_required_band_hz_by_role[role]
        ), role
    # Carried UNCHANGED — the same objects, not merely equal ones.
    assert (
        candidate.configured_polarity_sign_by_role
        == base.configured_polarity_sign_by_role
    )
    assert (
        candidate.measurement_protection_response_by_role
        is not None is not base.measurement_protection_response_by_role
    )


def test_a_candidates_sections_move_only_the_corner():
    """R17 adjudicates WHERE to cross, never what shape to cross with."""
    c = _selector_conductor(FakeSeams())
    configured = flow.sections_by_role(_preset().crossover_regions)
    moved = c._fc_candidate_sections(1750.0)
    assert set(moved) == set(configured)
    for role, sections in moved.items():
        assert [(s.order, s.highpass) for s in sections] == [
            (s.order, s.highpass) for s in configured[role]
        ], role
        assert all(s.fc_hz == 1750.0 for s in sections), role


def test_the_fit_targets_each_candidates_own_branch_not_the_configured_one():
    """Without the ``candidate_sections`` kwarg every candidate is corrected
    toward the CONFIGURED corner, and the comparison measures the fit's
    mismatch instead of the crossover's."""
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    seen: list[dict[str, tuple[CrossoverSection, ...]] | None] = []
    original = flow.CrossoverV2Conductor._fit_linearization

    def spy(self, analysis, cand, cloud=None, *, candidate_sections=None):
        seen.append(candidate_sections)
        return original(
            self, analysis, cand, cloud, candidate_sections=candidate_sections
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_fit_linearization", spy)
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 1)

    fitted = [s for s in seen if s is not None]
    assert len(fitted) >= 2, "the sweep must fit more than one candidate"
    corners = {
        next(iter(sections["woofer"])).fc_hz for sections in fitted
    }
    assert len(corners) == len(fitted), "each candidate needs its OWN corner"


# --- the evaluate-and-release loop ---------------------------------------------


def test_the_anchors_published_candidate_is_unchanged_by_the_sweep():
    """The load-bearing promise of the whole round: running the selector must
    not move the correction the household is actually offered.

    Compared against a run with the sweep replaced by a no-op — the same
    fixture, the same captures, so any difference is the selector's leakage
    through the seven ``_last_*`` conductor fields the fit writes.
    """
    def walk(sweep: bool) -> dict:
        fakes = _eligible_seams()
        c = _selector_conductor(fakes)
        with pytest.MonkeyPatch.context() as mp:
            if not sweep:
                mp.setattr(
                    flow.CrossoverV2Conductor, "_sweep_fc_candidates",
                    lambda self, *a, **k: None,
                )
            _run_phase(c, 1, 1)
            _run_phase(c, 2, 1)
            for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
                _run_phase(c, index, 1)
        return {
            "candidate": c.candidate.to_dict(),
            "predicted_sum": [
                np.asarray(part).tolist() for part in c.measure_predicted_sum
            ],
        }

    with_sweep, without = walk(True), walk(False)
    assert with_sweep["candidate"] == without["candidate"]
    assert with_sweep["predicted_sum"] == without["predicted_sum"]


def test_the_seven_conductor_fields_are_restored_after_the_sweep():
    """The mechanism behind the promise above, asserted directly so a
    regression names WHICH field leaked rather than only that one did."""
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)
    sentinels = {name: object() for name in _FC_SWEEP_CONDUCTOR_FIELDS}
    for name, value in sentinels.items():
        setattr(c, name, value)
    c._sweep_fc_candidates(
        c._program_for_phase(PHASE_MEASURE), object(), c._measure_analysis,
    )
    for name, value in sentinels.items():
        assert getattr(c, name) is value, name
    assert c._fc_evaluations, "the sweep must still have produced evidence"


def test_the_sweep_retains_no_analysis_sized_object():
    """The memory contract, at the WIRING level: whatever the loop retains has
    to be the kernel's small record, never a live analysis or driver response.
    """
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)
    assert c._fc_evaluations
    grid = flow.lateral_evidence_grid_hz()
    for evaluation in c._fc_evaluations:
        assert isinstance(evaluation, flow.FcCandidateEvaluation)
        for operator in evaluation.branch_operator_by_role.values():
            assert operator.shape == grid.shape
        assert evaluation.anchor_sum_db.size in (0, grid.size)
    # …and the walk's close releases even those.
    for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
        _run_phase(c, index, 1)
    assert c._fc_evaluations == ()


def test_the_evaluation_budget_is_bounded_by_the_phones_own_deadline():
    """The page throws a TERMINAL ``sweepFailed`` when its result wait expires,
    so the sweep's budget must sit strictly inside that deadline — and a
    spent budget must DISCLOSE the candidates it skipped, never drop them."""
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)
    measure_ms = flow._program_duration_ms(
        c._program_for_phase(PHASE_MEASURE)
    ) + flow.CAPTURE_ENTRY_MARGIN_MS
    deadline_s = max(flow.PHONE_RESULT_WAIT_FLOOR_MS, measure_ms) / 1000.0
    assert 0 < c._fc_evaluation_budget_s() < deadline_s

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow.CrossoverV2Conductor, "_fc_evaluation_budget_s",
            lambda self: 0.0,
        )
        _run_phase(c, 2, 1)
    planned = len(c._fc_candidate_set().candidates)
    assert planned > 1
    assert len(c._fc_evaluations) == planned, "a skipped candidate is disclosed"
    assert [e.refusal for e in c._fc_evaluations[1:]] == (
        [flow.EVAL_REFUSED_BUDGET] * (planned - 1)
    )


def test_the_phone_result_wait_floor_matches_the_page():
    """The Pi bounds itself by a number the capture page owns, so the two are
    pinned against each other rather than agreeing by inspection."""
    source = (
        Path(__file__).resolve().parents[1] / "capture-page" / "js" / "main.js"
    ).read_text(encoding="utf-8")
    # Anchored on ``Date.now() + Math.max(...)``, which is
    # ``waitForCaptureResult``'s own form: the page ALSO carries a 5 000 ms
    # ``Math.max(…, spec.duration_ms)`` in ``waitForSweepComplete``, and a
    # looser pattern pins the Pi's budget to the wrong wait entirely.
    match = re.search(
        r"Date\.now\(\)\s*\+\s*Math\.max\((\d+),\s*Number\(spec\.duration_ms\)",
        source,
    )
    assert match, "waitForCaptureResult's deadline floor moved or was renamed"
    assert int(match.group(1)) == flow.PHONE_RESULT_WAIT_FLOOR_MS


# --- the recommendation, and what it may not do -------------------------------


def test_the_walk_close_publishes_a_recommendation_that_names_sound_settings():
    """Reading 1: the selector RECOMMENDS. Whatever the verdict, the household
    is told the action is theirs — the declaration in ``/sound`` remains Fc's
    only writer, so copy implying JTS changed the crossover would describe
    something that did not happen."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        _fc_recommendation_lines,
    )
    from jasper.web.correction_crossover_v2 import _fc_selection_summary

    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)
    for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
        _run_phase(c, index, 1)

    selection = c.fc_selection
    assert selection is not None
    summary = _fc_selection_summary(c)
    assert summary["configured_hz"] == FC_HZ
    assert summary["evaluated"] <= summary["planned"]
    # Small enough for durable state, and carrying no working evidence.
    assert len(json.dumps(summary)) < 4096

    lines = _fc_recommendation_lines({"crossover_v2": {"fc_selection": summary}})
    assert lines, "keep-configured is a verdict too, never silence"
    joined = " ".join(lines).lower()
    assert "crossover" in joined
    if selection.recommended_hz is not None:
        assert "sound settings" in joined and "measure again" in joined
    assert not any(
        word in joined for word in ("applied", "changed it", "we moved")
    ), joined


def test_no_recommendation_is_rendered_when_no_sweep_ran():
    """A session that never swept must print nothing at all — "keep what you
    have" from a selector that never ran would be a verdict nobody reached."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        _fc_recommendation_lines,
    )
    from jasper.web.correction_crossover_v2 import _fc_selection_summary

    fakes = _eligible_seams()
    c = _conductor(fakes)
    assert c.fc_selection is None
    assert _fc_selection_summary(c) is None
    assert _fc_recommendation_lines({"crossover_v2": {}}) == []


def test_the_selection_never_reaches_the_emitted_candidate():
    """The hearing-safety half of Reading 1, structural rather than asserted by
    inspection: the selector's Fc has nowhere to go. Nothing on the published
    candidate carries a crossover frequency, and the conductor's own
    ``_fc_hz`` is the one the session opened with."""
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)
    for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
        _run_phase(c, index, 1)
    assert c._fc_hz == FC_HZ
    published = json.dumps(c.candidate.to_dict())
    for evaluation in c.fc_selection.scores:
        if evaluation.fc_hz != FC_HZ:
            assert f"{evaluation.fc_hz}" not in published
