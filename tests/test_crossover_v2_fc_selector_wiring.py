"""R17 PR-3: the Fc selector's CONDUCTOR WIRING.

The kernel's own arithmetic is pinned in ``test_crossover_v2_fc_selector``.
These are the seam guards — the declaration plumbing, the evaluate-and-release
loop's bounds, and the promise that a session which runs the selector publishes
exactly what a session without it would.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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
    kwargs.setdefault(
        "measurement_protection_sections_by_role", dict(PROTECTION)
    )
    return _lateral_conductor(
        fakes,
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


def _operators(c, *, fc_hz=1750.0, polarity="normal", trims=None, delay_us=0.0):
    """``_fc_branch_operators`` over a hand-built alignment, so each factor can
    be moved on its own. The analysis stand-in carries only ``alignment`` —
    which is all this method reads from it."""
    alignment = SimpleNamespace(
        polarity_sign=1 if polarity == "normal" else -1,
        anchor_delay_us=0.0, delay_us=delay_us, status=flow.ALIGNMENT_OK,
    )
    return c._fc_branch_operators(
        flow.lateral_evidence_grid_hz(),
        SimpleNamespace(alignment=alignment),
        c._fc_candidate_sections(fc_hz),
        {},
        trims if trims is not None else {"woofer": 0.0, "tweeter": 0.0},
    )


def test_the_branch_operator_carries_polarity_trim_and_the_candidates_corner():
    """``_fc_branch_operators`` is what turns a pose's NEUTRAL measurement into
    this candidate's model, so a wrong factor there makes the whole lateral
    robustness term noise while every other test stays green.

    Asserted as PROPERTIES — move one factor, see the expected change — rather
    than by re-deriving the formula, which would only prove the test and the
    code were written by the same hand.
    """
    c = _selector_conductor(FakeSeams())
    base = _operators(c)
    assert set(base) == {"woofer", "tweeter"}

    # 1. The alignment's polarity applies to the TWEETER alone — it is the
    #    branch ``predicted_branch_sum`` signs, and signing both would leave
    #    the summation identical and the model silently wrong.
    inverted = _operators(c, polarity="inverted")
    assert np.allclose(inverted["woofer"], base["woofer"])
    assert np.allclose(inverted["tweeter"], -base["tweeter"])

    # 2. Trim enters as a per-role linear gain.
    trimmed = _operators(c, trims={"woofer": -6.0, "tweeter": 0.0})
    assert np.allclose(trimmed["woofer"], base["woofer"] * 10.0 ** (-6.0 / 20.0))
    assert np.allclose(trimmed["tweeter"], base["tweeter"])

    # 3. A committed delay phases the tweeter and leaves its magnitude alone.
    delayed = _operators(c, delay_us=250.0)
    assert np.allclose(np.abs(delayed["tweeter"]), np.abs(base["tweeter"]))
    assert not np.allclose(delayed["tweeter"], base["tweeter"])
    assert np.allclose(delayed["woofer"], base["woofer"])

    # 4. …and the candidate's OWN corner is in there, or every candidate would
    #    predict the same pose sum and the comparison would be vacuous.
    assert not np.allclose(
        _operators(c, fc_hz=1650.0)["tweeter"], base["tweeter"]
    )


def test_the_operator_divides_out_the_protection_filter_the_graph_emitted():
    """§4.2's ``C_c / P``: a pose's ``M`` is ``plant * P``, so the emitted
    protection filter has to be DIVIDED out before the candidate's own
    crossover is applied — otherwise the model carries P twice.

    Needs a protection section that is not the identity to be visible at all:
    with the empty tuple most fixtures use, ``P == 1`` and dropping the divide
    changes nothing (verified by mutation).
    """
    protection = {
        "woofer": (),
        "tweeter": (CrossoverSection(fc_hz=900.0, order=2, highpass=True),),
    }
    grid = flow.lateral_evidence_grid_hz()
    guarded = _operators(_selector_conductor(
        FakeSeams(), measurement_protection_sections_by_role=protection,
    ))
    bare = _operators(_selector_conductor(FakeSeams()))
    p_response = flow.crossover_response_complex(grid, protection["tweeter"])

    assert not np.allclose(guarded["tweeter"], bare["tweeter"]), (
        "the emitted protection filter is not reaching the operator at all"
    )
    assert np.allclose(guarded["tweeter"] * p_response, bare["tweeter"])
    assert np.allclose(guarded["woofer"], bare["woofer"]), "P is per-role"


def test_an_ineligible_session_refuses_candidates_without_calling_the_fit():
    """``_fit_linearization`` states that it ASSUMES eligibility and does not
    re-check, so calling it on a phone-tier or under-repeated session raises
    inside the fit engine instead of declining — six spurious WARNING refusals
    per capture, and a documented precondition violated. The sweep applies the
    same gate ``_build_candidate`` does and refuses honestly instead.
    """
    fakes = FakeSeams()  # the DEFAULT measure fixture: no mic tier, no repeats
    c = _selector_conductor(fakes)
    calls: list[object] = []
    original = flow.CrossoverV2Conductor._fit_linearization

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow.CrossoverV2Conductor, "_fit_linearization",
            lambda self, *a, **k: (calls.append(1), original(self, *a, **k))[1],
        )
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 1)

    assert calls == [], "the fit was called outside its own precondition"
    assert c._fc_evaluations, "the candidates must still be disclosed"
    assert all(
        e.refusal == flow.EVAL_REFUSED_UNFITTABLE for e in c._fc_evaluations
    )


# --- the evaluate-and-release loop ---------------------------------------------


def test_the_anchors_published_candidate_is_unchanged_by_the_sweep():
    """The load-bearing promise of the whole round: running the selector must
    not move the correction the household is actually offered.

    Compared against a run with the sweep replaced by a no-op — the same
    fixture, the same captures, so any difference is the selector's doing.

    **This test does NOT prove the ``_last_*`` restore.** On the ordinary
    eligible path the anchor's own fit re-runs at the walk's close and
    overwrites all seven fields anyway, so deleting the restore leaves this
    green (verified by mutation). What it covers is everything else the sweep
    touches; the restore has its own two guards below.
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


def test_a_candidates_prediction_never_becomes_the_anchors_when_its_fit_fails():
    """The restore's REAL failure mode, which the comparison above cannot see.

    ``_build_candidate``'s SF2 degrade clears six of the seven fields when the
    anchor's own fit raises — but not ``_last_linearized_predicted_sum``. So
    without the restore, a session whose sweep succeeded and whose anchor fit
    then failed publishes the LAST CANDIDATE's predicted sum as the anchor's
    VERIFY prior: a prediction for a crossover this speaker does not have,
    which VERIFY would then grade the real one against.
    """
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    original = flow.CrossoverV2Conductor._fit_linearization
    swept: list = []

    def anchor_fit_fails(self, analysis, cand, cloud=None, *, candidate_sections=None):
        # Raise ONLY for the anchor's own close (no candidate sections), so
        # the sweep's successful fits stay exactly as they were. Each
        # candidate's prediction is captured the moment it is written —
        # ``_last_linearized_predicted_sum`` cannot be read afterwards,
        # because the restore under test has already put it back.
        if candidate_sections is None:
            raise ValueError("forced anchor fit failure")
        out = original(
            self, analysis, cand, cloud, candidate_sections=candidate_sections
        )
        swept.append(self._last_linearized_predicted_sum)
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_fit_linearization", anchor_fit_fails)
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 1)
        assert swept and swept[-1] is not None, "the sweep must have fitted first"
        for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
            _run_phase(c, index, 1)

    assert c.candidate.linearization_outcome == "fit_failed"
    published = np.asarray(c.measure_predicted_sum[1])
    for candidate_sum in swept:
        assert not np.array_equal(published, np.asarray(candidate_sum[1])), (
            "a candidate's predicted sum reached the anchor's published prior"
        )


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


def _retained_bytes(evaluation) -> int:
    """Bytes one retained record holds, refusing anything not small by TYPE.

    A **whitelist over the record's actual fields**, walked with
    ``dataclasses.fields`` rather than a list of known names, because the
    failure this guards against is a NEW field hoarding — and a blacklist of
    forbidden attributes (`driver_responses`, `complex_tf`, …) cannot see an
    object that simply does not have them. Anything outside the whitelist
    raises here, naming the field.
    """
    total = 0
    for field in dataclasses.fields(evaluation):
        value = getattr(evaluation, field.name)
        if value is None or isinstance(value, (bool, int, float, str)):
            continue
        if isinstance(value, np.ndarray):
            total += value.nbytes
        elif isinstance(value, tuple) and all(
            isinstance(item, (int, float)) for item in value
        ):
            continue
        elif isinstance(value, Mapping):
            for key, item in value.items():
                assert isinstance(item, np.ndarray), (
                    f"{field.name}[{key!r}] retains a {type(item).__name__}"
                )
                total += item.nbytes
        else:
            raise AssertionError(
                f"{field.name} retains a {type(value).__name__} — the retained "
                "record must stay scalars plus small arrays"
            )
    return total


def test_the_sweep_retains_no_analysis_sized_object():
    """The memory contract, at the WIRING level and against REAL swept records.

    ``_eligible_seams`` drives an actual sweep through
    ``_evaluate_fc_candidate``, so this asserts what production retained rather
    than what a fixture was hand-built to hold.

    **The earlier version of this test was vacuous** and the resilience lens
    proved it: adding an ``analysis`` field to ``FcCandidateEvaluation`` and
    populating it in production left all six candidates hoarding a full
    ``ProgramAnalysis`` with 42 tests green, because the assertions enumerated
    the fields that already existed. The whitelist in ``_retained_bytes`` is
    the fix — a new field of an unexpected type fails by construction.
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

    # GROWTH, bounded across the whole sweep rather than per record: the
    # ruling's cap is on what the walk carries, and one small record times N is
    # what makes it small. 64 kB is ~4x the real figure, so it fails on a
    # hoarded object (megabytes) without being a tripwire on an honest field.
    swept = sum(_retained_bytes(e) for e in c._fc_evaluations)
    assert swept < 64_000, f"the retained sweep grew to {swept} bytes"

    # …and the walk's close releases even those.
    for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
        _run_phase(c, index, 1)
    assert c._fc_evaluations == ()


def test_a_failing_sweep_never_costs_the_household_an_accepted_measure():
    """"Never raises" as a STRUCTURAL property, not a claim about which of the
    sweep's steps happens to be total today.

    Deriving the candidate set reads household declarations and the budget
    reads the MEASURE program; both are ordinary sources of a malformed-input
    raise, and both sat OUTSIDE the try until the resilience lens named it. An
    advisory must never cost a household a capture they already completed.
    """
    for broken in ("_fc_candidate_set", "_fc_evaluation_budget_s"):
        fakes = _eligible_seams()
        c = _selector_conductor(fakes)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                flow.CrossoverV2Conductor, broken,
                lambda self: (_ for _ in ()).throw(ValueError("forced")),
            )
            _run_phase(c, 1, 1)
            accepted = _run_phase(c, 2, 1)  # must NOT raise
        assert accepted is not None, broken
        assert PHASE_MEASURE in c.accepted_phases, broken
        # …and the sweep declined cleanly rather than half-populating.
        assert c._fc_evaluations == (), broken


def test_the_evaluation_budget_tracks_the_measure_programs_real_duration():
    """The budget must be DERIVED from the phone's deadline, not a constant.

    ``max(PHONE_RESULT_WAIT_FLOOR_MS, measure_ms)`` is the whole derivation, and
    the neighbouring bound test cannot see it: that one asserts
    ``0 < budget < deadline``, an inequality with slack that a hardcoded
    floor-based constant satisfies just as well (correctness lens — hardcoding
    it left 601 tests passing). A broken derivation would then be
    indistinguishable from a loaded Pi, because both surface only as a smaller
    ``k`` in the k-of-N disclosure.

    Two durations straddling the 30 000 ms floor, so the ``max`` is exercised in
    both directions. This bites on the shipped shape rather than in principle:
    the live stage-1 spec is 41 885 ms and even this fixture's own MEASURE
    program is 40 385 ms — both above the floor, so the floor is NOT what
    governs today and a constant would be wrong right now.
    """
    c = _selector_conductor(_eligible_seams())

    def budget_for(duration_ms: int) -> float:
        # ``_program_duration_ms`` is samples/rate, so a duck-typed stub is
        # enough and keeps this about the derivation rather than program
        # composition.
        c._measure_program = SimpleNamespace(
            total_samples=duration_ms * 48, sample_rate_hz=48_000,
        )
        return c._fc_evaluation_budget_s()

    below = budget_for(10_000)   # + margin = 12 000 ms, under the floor
    above = budget_for(50_000)   # + margin = 52 000 ms, over it
    assert below < above, "the budget does not track the MEASURE program at all"

    fraction = flow.FC_EVALUATION_BUDGET_FRACTION
    assert below == pytest.approx(
        fraction * flow.PHONE_RESULT_WAIT_FLOOR_MS / 1000.0
    ), "under the floor, the floor governs"
    assert above == pytest.approx(
        fraction * (50_000 + flow.CAPTURE_ENTRY_MARGIN_MS) / 1000.0
    ), "over the floor, the recording window governs"


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
    assert "crossover" in " ".join(lines).lower()


def test_a_recommending_verdict_always_says_the_household_must_edit_sound():
    """The hearing-safety promise of Reading 1, asserted on the RECOMMENDING
    branch unconditionally.

    Written against a constructed payload rather than the fixture's own
    verdict: the fixture keeps configured, so a conditional assertion here
    guards nothing — verified by mutation (rewriting the copy to "JTS moved
    your crossover" left the fixture-driven version green).
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        _fc_recommendation_lines,
    )

    lines = _fc_recommendation_lines({"crossover_v2": {"fc_selection": {
        "verdict": "recommend_alternative",
        "configured_hz": 2000.0, "recommended_hz": 1750.0, "margin_db": 1.4,
        "evaluated": 6, "planned": 6, "limits": {}, "refusals": [], "scores": [],
    }}})
    joined = " ".join(lines).lower()
    assert "1750 hz" in joined and "2000 hz" in joined
    # The ACTION, and whose it is. Without these two the screen reads as a
    # change JTS made, and the household waits for something nothing will do.
    assert "sound settings" in joined, joined
    assert "measure again" in joined, joined
    assert "does not change it for you" in joined, joined


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
