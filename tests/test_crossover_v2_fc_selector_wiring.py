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
from jasper.active_speaker.crossover_v2_flow import PHASE_MEASURE
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
    mismatch instead of the crossover's.

    Since #2291 Phase 2b the sections do more than bound the fit: the conductor
    derives the planner's whole
    :class:`~jasper.active_speaker.crossover_v2.contracts.CandidateAcousticContext`
    from them, so they are also the only place the corner every Fc-driven seam
    reads comes from. This spies the seam that receives them.
    """
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    seen: list[dict[str, tuple[CrossoverSection, ...]] | None] = []
    original = flow.CrossoverV2Conductor._plan_linearization

    def spy(self, analysis, cand, cloud=None, *, candidate_sections=None):
        seen.append(candidate_sections)
        return original(
            self, analysis, cand, cloud, candidate_sections=candidate_sections
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_plan_linearization", spy)
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
    """``plan_linearization`` states that it ASSUMES eligibility and does not
    re-check, so calling it on a phone-tier or under-repeated session raises
    inside the fit engine instead of declining — six spurious WARNING refusals
    per capture, and a documented precondition violated. The sweep applies the
    same gate ``_build_candidate`` does and refuses honestly instead.
    """
    fakes = FakeSeams()  # the DEFAULT measure fixture: no mic tier, no repeats
    c = _selector_conductor(fakes)
    calls: list[object] = []
    original = flow.CrossoverV2Conductor._plan_linearization

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow.CrossoverV2Conductor, "_plan_linearization",
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

    **What it does NOT prove** is that a swept candidate's values cannot reach
    the anchor: on the ordinary eligible path the anchor's own fit runs at the
    walk's close and produces its own state anyway. That separation is the
    subject of the two guards below, which since #2291 Phase 2b assert the
    stronger structural property — there is no shared channel to leak through —
    rather than that a save/restore put seven fields back.
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
    """The leak the comparison above cannot see, guarded at its worst case.

    A session whose sweep succeeded and whose ANCHOR fit then failed must
    publish the raw two-branch prediction as its VERIFY prior — never the last
    swept candidate's, which models a crossover this speaker does not have and
    which VERIFY would then grade the real one against.

    Until #2291 Phase 2b this was a save/restore promise: the fit wrote
    ``_last_linearized_predicted_sum`` on the conductor, ``_build_candidate``'s
    SF2 degrade cleared six of the seven fields but not that one, and only the
    sweep's restore stood between a swept prediction and the anchor's artifact.
    It is now structural — each build's prediction is a value on the state it
    returns, and the SF2 degrade returns a state with none — so this asserts
    the outcome rather than the mechanism, and stays honest whichever way the
    mechanism is built next.
    """
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    original = flow.CrossoverV2Conductor._plan_linearization
    swept: list = []

    def anchor_fit_fails(self, analysis, cand, cloud=None, *, candidate_sections=None):
        # Raise ONLY for the anchor's own close (no candidate sections), so
        # the sweep's successful fits stay exactly as they were.
        if candidate_sections is None:
            raise ValueError("forced anchor fit failure")
        plan = original(
            self, analysis, cand, cloud, candidate_sections=candidate_sections
        )
        swept.append(plan.linearized_predicted_sum)
        return plan

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_plan_linearization", anchor_fit_fails)
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


def test_the_sweep_leaves_no_candidate_state_on_the_conductor():
    """The mechanism behind the promise above, asserted directly (#2291 2b).

    This replaces ``test_the_seven_conductor_fields_are_restored_after_the_
    sweep``, whose subject no longer exists: the sweep used to snapshot seven
    ``self._last_*`` fields and put them back, and the fields are gone. The
    stronger property is that there is nothing to restore — a build's
    linearization output is a value it returns, so no swept candidate can leave
    a trace on the conductor for the anchor to read.

    Asserted by NAME so a regression says which channel came back. The
    conductor has no ``__slots__``, so a re-introduced ``self._last_* = …``
    anywhere on the build path fails this immediately, which is the mutation
    that motivates it.
    """
    retired = (
        "_last_level_frame",
        "_last_level_frame_cores",
        "_last_level_frame_disagreement_db",
        "_last_level_frame_trims",
        "_last_linearization_outcome",
        "_last_linearized_predicted_sum",
        "_last_realized_level_match",
    )
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)
    c._sweep_fc_candidates(
        c.program_for_phase(PHASE_MEASURE), object(), c._measure_analysis,
    )
    assert c._fc_evaluations, "the sweep must still have produced evidence"
    for name in retired:
        assert not hasattr(c, name), (
            f"{name} is back — a candidate's planner output is on the conductor "
            "again, which is the channel #2291 removed"
        )

    # …and it stays absent through the walk's own close, where the anchor's
    # build runs. A sweep that leaked nothing and a close that leaked instead
    # would be the same defect one call later.
    _run_phase(c, 2, 1)
    for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
        _run_phase(c, index, 1)
    for name in retired:
        assert not hasattr(c, name), name


def test_each_evaluation_retains_its_own_predicted_spec_report():
    """The twin of the realized-level guard below, for the graded prediction.

    **Why this needs a test of its own.** The spec report is not a value the
    evaluation is handed — it is read back off the conductor AFTER the
    candidate's own build has written it there, which makes the ORDER of two
    statements the whole correctness argument. Reading it one line earlier
    files the PREVIOUS candidate's grading beside this candidate's proposal:
    the same cross-context leak #2291 exists to close, and the family the
    2026-08-10 incident belongs to.

    Nothing caught that before this test. Measured while writing it (#2291
    Phase 5a-v(b)): moving the read above the build leaves the whole
    ``test_crossover_v2_fc_selector_wiring`` / ``_fc_candidates`` /
    ``_fc_selector`` / ``_priors`` set green, and shows up only as a 99-line
    diff in the slice's dual run. That is exactly the shape a guard is for,
    and it is why ``evaluate_candidate`` takes ``predicted_spec_report`` as a
    CALLABLE rather than a value — a value cannot be read after the build.

    Asserted against what each build actually wrote, not merely "present":
    a sweep that stamped every evaluation with the same report, or with the
    anchor's, satisfies a presence check completely.
    """
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)

    written: list[tuple[float, object]] = []
    original = flow.CrossoverV2Conductor._build_measure_candidate

    def spy(self, analysis, cloud, *, candidate_sections=None, source_preset=None):
        built = original(
            self, analysis, cloud,
            candidate_sections=candidate_sections, source_preset=source_preset,
        )
        # The report as it stands the moment this build finished — which is
        # the only moment it describes THIS candidate.
        fc_hz = next(
            iter(section.fc_hz for sections in (candidate_sections or {}).values()
                 for section in sections),
            None,
        )
        report = self._measure_predicted_spec_report
        written.append((fc_hz, None if report is None else dict(report)))
        return built

    with pytest.MonkeyPatch.context() as mp:
        # The MEASURE consume alone: the sweep's builds happen here, the
        # anchor's own happens later at the walk's close.
        mp.setattr(flow.CrossoverV2Conductor, "_build_measure_candidate", spy)
        _run_phase(c, 2, 1)

    by_fc = {round(fc, 3): report for fc, report in written if fc is not None}
    assert len(by_fc) >= 2, "the sweep must have built more than one corner"
    scored = [e for e in c._fc_evaluations if e.refusal is None]
    assert scored, "the sweep must have produced scoreable candidates"
    # The reports must not be all-identical, or the identity assertion below
    # would hold for a sweep that leaked one report into every evaluation.
    distinct = {repr(report) for report in by_fc.values()}
    assert len(distinct) >= 2, (
        "this fixture must grade its corners differently, or the per-candidate "
        "identity below is unfalsifiable"
    )
    for evaluation in scored:
        assert evaluation.predicted_spec_report == by_fc[
            round(evaluation.fc_hz, 3)
        ], evaluation.fc_hz


def test_a_speaker_with_no_protection_map_sweeps_nothing():
    """No §4.2 composition means no candidate crossover can be substituted.

    The sweep's first gate, and previously unpinned in either direction: the
    conductor simply returned. Worth a guard now that the condition crosses a
    module boundary as ``protection_declared`` — a parameter that silently
    stopped being consulted would let an uncomposed capture be re-cornered,
    which is a model built on a filter the graph never emitted.
    """
    fakes = _eligible_seams()
    unprotected = _selector_conductor(
        fakes, measurement_protection_sections_by_role=None,
    )
    _run_phase(unprotected, 1, 1)
    accepted = _run_phase(unprotected, 2, 1)

    assert accepted is not None, "the capture must still be accepted"
    assert PHASE_MEASURE in unprotected.accepted_phases
    assert unprotected._fc_evaluations == ()
    # …and the positive control, so this cannot pass by the fixture simply
    # never sweeping at all.
    protected = _selector_conductor(_eligible_seams())
    _run_phase(protected, 1, 1)
    _run_phase(protected, 2, 1)
    assert protected._fc_evaluations, "the protected fixture must sweep"


def test_each_evaluation_retains_its_own_realized_level_verdict():
    """#2307 gate note N6, closed at the seam that closes it (#2291 Phase 2b).

    Before the cutover the sweep restored the conductor's scratch fields when it
    ended, so the only realized-level verdict reachable at selection time
    belonged to the ANCHOR — and ``FcCandidateEvaluation`` carried none, which
    left a selected alternative corner committing a proposal with the field
    empty. Each candidate's plan is now a value the sweep can retain.

    Asserted as IDENTITY against the plan each candidate's own build returned,
    not as "non-empty": a sweep that stamped every evaluation with the same
    verdict, or with the anchor's, would satisfy a presence check completely.
    """
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)

    plans: list = []
    original = flow.CrossoverV2Conductor._plan_linearization

    def spy(self, *args, **kwargs):
        plan = original(self, *args, **kwargs)
        plans.append(plan)
        return plan

    with pytest.MonkeyPatch.context() as mp:
        # Collected during the MEASURE consume alone, which is where the sweep
        # runs — the anchor's own build happens later, at the walk's close.
        mp.setattr(flow.CrossoverV2Conductor, "_plan_linearization", spy)
        _run_phase(c, 2, 1)

    by_fc = {round(plan.fc_hz, 3): plan for plan in plans}
    assert len(by_fc) >= 2, "the sweep must have planned more than one corner"
    scored = [e for e in c._fc_evaluations if e.refusal is None]
    assert scored, "the sweep must have produced scoreable candidates"
    for evaluation in scored:
        plan = by_fc[round(evaluation.fc_hz, 3)]
        assert evaluation.realized_branch_level == (
            plan.realized_level_match.to_dict()
        ), evaluation.fc_hz

    # …and the corners really do produce DIFFERENT verdicts, which is what
    # makes the identity above a comparison rather than one value matched N
    # times. Read off the plans rather than the evaluations because a fixture
    # may score only one candidate — the prediction gate refuses the rest —
    # while every attempted candidate still planned.
    assert len({
        repr(plan.realized_level_match.to_dict()) for plan in by_fc.values()
    }) > 1, "every corner reported the same realized level"


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
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, np.ndarray):
                    total += item.nbytes
                else:
                    assert isinstance(item, (int, float)), field.name
        elif isinstance(value, Mapping):
            if all(isinstance(item, np.ndarray) for item in value.values()):
                total += sum(item.nbytes for item in value.values())
            else:
                # Executable candidate/finding records are bounded exact-JSON
                # data, never analysis objects hidden behind a Mapping.
                total += len(json.dumps(value, sort_keys=True).encode())
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
    planned = c._fc_candidate_set().candidates
    assert tuple(e.fc_hz for e in c._fc_evaluations) == planned
    assert len(planned) == len(set(planned)) == 6
    assert all(e.refusal != flow.EVAL_REFUSED_BUDGET for e in c._fc_evaluations)
    grid = flow.lateral_evidence_grid_hz()
    for evaluation in c._fc_evaluations:
        assert isinstance(evaluation, flow.FcCandidateEvaluation)
        for operator in evaluation.branch_operator_by_role.values():
            assert operator.shape == grid.shape
        assert evaluation.anchor_sum_db.size in (0, grid.size)

    # GROWTH, bounded across the whole sweep rather than per record. The
    # executable prediction/delta curves remain sub-megabyte; a retained
    # ProgramAnalysis is hundreds of MB and fails this by orders of magnitude.
    swept = sum(_retained_bytes(e) for e in c._fc_evaluations)
    assert swept < 512_000, f"the retained sweep grew to {swept} bytes"

    # …and the walk's close releases even those.
    for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
        _run_phase(c, index, 1)
    assert c._fc_evaluations == ()


def test_a_failed_later_fc_cannot_reuse_or_publish_the_prior_fitted_prediction():
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    original = flow.CrossoverV2Conductor._plan_linearization
    calls = 0

    def fail_after_first(self, analysis, cand, cloud=None, *, candidate_sections=None):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("forced later-Fc fit failure")
        return original(
            self, analysis, cand, cloud, candidate_sections=candidate_sections,
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Conductor, "_plan_linearization", fail_after_first)
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 1)
        assert c._fc_evaluations[0].refusal is None
        assert all(
            item.refusal == flow.EVAL_REFUSED_UNFITTABLE
            and item.predicted_sum is None
            and item.candidate is None
            for item in c._fc_evaluations[1:]
        )
        for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
            _run_phase(c, index, 1)

    assert c.fc_selection.recommended_hz is None
    assert c.candidate.source_preset.crossover_regions[0].fc_hz == FC_HZ


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


def test_the_evaluation_budget_has_one_explicit_owner():
    c = _selector_conductor(_eligible_seams())
    assert c._fc_evaluation_budget_s() == flow.FC_SWEEP_COMPUTE_BUDGET_S
    assert 0 < flow.FC_SWEEP_COMPUTE_BUDGET_S <= 70.0


def test_zero_budget_attempts_configured_then_discloses_every_skip():
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow.CrossoverV2Conductor, "_fc_evaluation_budget_s",
            lambda self: 0.0,
        )
        _run_phase(c, 2, 1)
    planned = len(c._fc_candidate_set().candidates)
    assert planned > 1
    assert len(c._fc_evaluations) == planned, "a skipped candidate is disclosed"
    assert c._fc_evaluations[0].fc_hz == c._fc_hz
    assert [e.refusal for e in c._fc_evaluations[1:]] == (
        [flow.EVAL_REFUSED_BUDGET] * (planned - 1)
    )
    for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
        _run_phase(c, index, 1)
    assert c.candidate is not None
    assert c.fc_selection.comparison_complete is False
    assert c.fc_selection.recommended_hz is None
    from jasper.web.correction_crossover_v2 import _fc_selection_summary
    summary = _fc_selection_summary(c)
    assert summary["comparison_complete"] is False
    assert summary["recommended_hz"] is None
    assert summary["attempted"] == [c._fc_hz]
    assert all(item["reason"] == flow.EVAL_REFUSED_BUDGET for item in summary["skipped"])


def test_budget_exhaustion_never_skips_the_configured_baseline(caplog):
    """Regression: a numerically-high configured Fc used to sort last.

    With two 0.9 s alternatives and a 2.0 s budget, the forecast then skipped
    the 2000 Hz baseline before it was attempted.  The baseline is the golden
    comparison and must instead be first regardless of numeric order.
    """
    caplog.set_level("INFO")
    c = _selector_conductor(_eligible_seams())
    c._fc_hz = 2000.0
    candidates = flow.fc_candidate_set(
        configured_hz=2000.0,
        hf_hard_floor_hz=1600.0,
        lower_driver_hard_ceiling_hz=4000.0,
        search_band_hz=(1600.0, 2500.0),
        lower_driver_diameter_mm=114.0,
    )
    attempted: list[float] = []

    def evaluate(self, fc_hz, anchor, program, result):
        attempted.append(fc_hz)
        return flow._fc_refusal(fc_hz, "attempted")

    clock = iter((0.0, 0.0, 0.9, 0.9, 1.8, 1.8, 1.8))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(c, "_fc_candidate_set", lambda: candidates)
        mp.setattr(c, "_fc_evaluation_budget_s", lambda: 2.0)
        mp.setattr(flow.CrossoverV2Conductor, "_evaluate_fc_candidate", evaluate)
        mp.setattr(flow.time, "monotonic", lambda: next(clock))
        c._sweep_fc_candidates(object(), object(), object())

    assert attempted[0] == 2000.0
    assert [e.fc_hz for e in c._fc_evaluations] == list(candidates.candidates)
    assert all(
        e.refusal == flow.EVAL_REFUSED_BUDGET for e in c._fc_evaluations[2:]
    )
    sweep_log = next(
        record.getMessage() for record in caplog.records
        if "event=correction.crossover_v2_fc_sweep " in record.getMessage()
    )
    for field in (
        "candidate_order=", "attempted=", "skipped=", "elapsed_s=",
        "budget_s=", "comparison_complete=false",
    ):
        assert field in sweep_log


def test_the_result_wait_is_named_once_and_exceeds_the_compute_budget():
    source = (
        Path(__file__).resolve().parents[1] / "capture-page" / "js" / "main.js"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"const CAPTURE_RESULT_WAIT_BUDGET_MS = (\d+);",
        source,
    )
    assert match
    wait_s = int(match.group(1)) / 1000.0
    assert wait_s >= flow.FC_SWEEP_COMPUTE_BUDGET_S + 20.0
    assert source.count("CAPTURE_RESULT_WAIT_BUDGET_MS") == 2
    assert "CAPTURE_RESULT_WAIT_BUDGET_MS, Number(spec.duration_ms) || 0" in source


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
    assert summary["comparison_complete"] is True
    assert summary["candidate_order"][0] == FC_HZ
    assert summary["attempted"] == summary["candidate_order"]
    assert summary["skipped"] == []
    # Small enough for durable state, and carrying no working evidence.
    assert len(json.dumps(summary)) < 4096

    lines = _fc_recommendation_lines({"crossover_v2": {"fc_selection": summary}})
    assert lines, "keep-configured is a verdict too, never silence"
    assert "crossover" in " ".join(lines).lower()


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


def test_an_alternative_winner_publishes_its_exact_candidate_and_preset():
    fakes = _eligible_seams()
    c = _selector_conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)
    winner = next(
        item for item in c._fc_evaluations
        if item.fc_hz != FC_HZ and item.candidate is not None
    )

    def choose(*args, **kwargs):
        return flow.FcSelection(
            verdict="recommend_alternative", configured_hz=FC_HZ,
            recommended_hz=winner.fc_hz, margin_db=2.0, scores=(), refusals=(),
            limits={}, evaluated=2, planned=len(c._fc_evaluations),
            candidate_order=tuple(item.fc_hz for item in c._fc_evaluations),
            attempted=tuple(item.fc_hz for item in c._fc_evaluations), skipped=(),
            comparison_complete=True,
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "select_fc", choose)
        for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
            _run_phase(c, index, 1)

    assert c._fc_hz == FC_HZ  # measurement context does not silently move
    assert c.candidate.to_dict() == winner.candidate
    assert c.candidate.source_preset.crossover_regions[0].fc_hz == winner.fc_hz
    assert fakes.published_candidates == [c.candidate]
    assert np.array_equal(c.measure_predicted_sum[1], winner.predicted_sum[1])
    assert c.measure_predicted_spec_report == winner.predicted_spec_report


# --------------------------------------------------------------------------- #
# the committed sweep golden — #2291 Phase 5a-iii's equivalence spine for 5a-v
#
# The fc sweep's per-candidate half stays on the conductor until 5a-v, because
# ``_evaluate_fc_candidate`` consumes ``_SpeculativeClose`` (and through it
# ``_LinearizationState`` / ``_CloudFitEvidence``), all of which belong to the
# candidate/planning organ. When that slice moves the sweep, THIS is what it
# compares against: the decisions the sweep makes before any fit runs — which
# corners, in what order, under which declared bounds, and what each corner does
# to the sections and to the priors' three corner-dependent fields.
#
# Captured from the pre-extraction conductor (origin/main at 53eb2d980) and
# written as literals. Re-derive them by re-running that comparison; editing a
# number here to make a test pass deletes the evidence it exists to be.
#
# THE CAMPAIGN PATTERN, recorded here because this is where the next slice will
# read it: **a strangler phase's goldens must anchor on the DECLARED source of
# truth, never on the method under test — self-referential pins are blind to
# uniform drift.** Earned: the first cut of the re-cornering test below compared
# ``_fc_candidate_sections(fc)`` against ``_fc_candidate_sections(FC_HZ)``, and a
# uniform ``order += 2`` applied to every section passed it (measured: 6 passed
# with that anchor, 6 failed with the declared one). A port that changed the
# whole family the same way — which is what a port does — would have shipped.
# --------------------------------------------------------------------------- #

#: ``declaration shape -> (bands, ordered candidates, declared limits)``.
#: Both jts3 shapes from the R17 STOP: the LIVE tweeter declaration whose search
#: band starts AT the configured corner (so the intersection admits nothing), and
#: the WIDENED one that opens five alternatives — including 1648.7 Hz, the corner
#: the 2026-08-10 incident actually selected.
SWEEP_GOLDEN = {
    "live_no_alternative": (
        LIVE_BANDS,
        [1600.0],
        {
            "beaming_ceiling_hz": 1915.443701,
            "declared_floor_hz": 300.0,
            "lower_driver_ceiling_hz": 6000.0,
            "search_hi_hz": 2500.0,
            "search_lo_hz": 2000.0,
        },
    ),
    "widened": (
        WIDENED_BANDS,
        [1600.0, 1648.7, 1698.9, 1750.6, 1803.9, 1858.8],
        {
            "beaming_ceiling_hz": 1915.443701,
            "declared_floor_hz": 300.0,
            "lower_driver_ceiling_hz": 6000.0,
            "search_hi_hz": 2500.0,
            "search_lo_hz": 1600.0,
        },
    ),
}

#: ``fc -> the woofer's candidate-required upper bound``. The tweeter's is
#: ``(fc/2, inf)`` at every corner, so one column carries the whole table: the
#: union of the radiating span with the UNCLAMPED overlap band, which is what
#: makes a superset the safe side for a required mask.
REQUIRED_BAND_WOOFER_HI_HZ = {
    1600.0: 3200.0, 1648.7: 3297.4, 1698.9: 3397.8,
    1750.6: 3501.2, 1803.9: 3607.8, 1858.8: 3717.6,
}


@pytest.mark.parametrize("shape", sorted(SWEEP_GOLDEN))
def test_the_proposed_corners_and_their_bounds_are_the_ones_that_shipped(shape):
    """WHICH corners this speaker's declarations admit, and in what ORDER.

    Order is not cosmetic: the budget forecast spends the wall clock in this
    sequence and stops where it stops, so a reordering silently changes which
    candidates a slow session gets to evaluate at all.
    """
    bands, candidates, limits = SWEEP_GOLDEN[shape]
    c = _selector_conductor(FakeSeams(), bands=bands)

    got = c._fc_candidate_set()

    assert [round(float(fc), 4) for fc in got.candidates] == candidates
    assert {k: round(float(v), 6) for k, v in got.limits.items()} == limits
    assert list(got.rejected) == []
    # The configured corner is always proposable, even where every bound would
    # exclude it — otherwise this speaker has no golden candidate to prove
    # equivalence against (§9.8).
    assert candidates[0] == pytest.approx(FC_HZ)


def test_the_evaluation_budget_is_the_declared_one():
    """A wall budget, stated. The forecast that spends it is the sweep's, but
    the number is a product decision and belongs in the golden."""
    assert _selector_conductor(FakeSeams())._fc_evaluation_budget_s() == 70.0


@pytest.mark.parametrize("fc_hz", sorted(REQUIRED_BAND_WOOFER_HI_HZ))
def test_only_the_corner_moves_when_a_candidate_is_re_cornered(fc_hz):
    """R17 adjudicates WHERE to cross, never what shape to cross with.

    So every section keeps the preset's order, direction and role assignment and
    takes the candidate's corner — and the priors' three corner-dependent fields
    move with it while polarity and protection do not (they describe how the
    drivers are wired and what the graph emitted, neither of which a corner
    changes).
    """
    c = _selector_conductor(FakeSeams(), bands=WIDENED_BANDS)
    # The DECLARED sections, not ``_fc_candidate_sections(FC_HZ)``. Anchoring on
    # the method under test makes this blind to any UNIFORM change to the
    # re-cornering — a ``+2`` applied to every section's order compares equal to
    # itself and passes. That is precisely the 5a-v port failure mode this golden
    # exists to catch, so the anchor is the preset.
    configured = flow.sections_by_role(_preset().crossover_regions)

    sections = c._fc_candidate_sections(fc_hz)

    assert set(sections) == set(configured)
    for role, at_corner in sections.items():
        assert [s.fc_hz for s in at_corner] == [fc_hz] * len(at_corner)
        # Shape preserved, corner moved: same count, same slope order, same
        # direction — everything a ``CrossoverSection`` carries except ``fc_hz``.
        assert [(s.order, s.highpass) for s in at_corner] == [
            (s.order, s.highpass) for s in configured[role]
        ]

    priors = c._fc_candidate_priors(fc_hz, sections)
    assert priors.crossover_fc_hz == fc_hz
    lo, hi = priors.candidate_required_band_hz_by_role["woofer"]
    assert lo == 0.0
    assert hi == pytest.approx(REQUIRED_BAND_WOOFER_HI_HZ[fc_hz])
    t_lo, t_hi = priors.candidate_required_band_hz_by_role["tweeter"]
    assert t_lo == pytest.approx(fc_hz / 2.0)
    assert t_hi == float("inf")
    # Carried UNCHANGED, and load-bearing: ``_compose_configured_path_ir``
    # raises on a PARTIAL prior set, so dropping either would refuse the
    # composition outright instead of producing a candidate.
    base = c._measure_priors()
    assert (
        priors.configured_polarity_sign_by_role
        == base.configured_polarity_sign_by_role
    )
    assert priors.measurement_protection_response_by_role is not None
