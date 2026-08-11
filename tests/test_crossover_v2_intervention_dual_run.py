# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Legacy vs pure planner, over identical inputs (#2291 Phase 2).

**What this proves, and how.** The pure planner in
:mod:`jasper.active_speaker.crossover_v2.intervention` is a *reimplementation*
of ``CrossoverV2Conductor._fit_linearization``, not a move, so "it still does
the same thing" is a claim that has to be measured rather than asserted. Both
implementations are live in this commit, so this module runs them side by side
on the banked 2026-08-10 jts3 numbers and classifies every difference.

Only two classes of difference are sanctioned by #2291. The harness is built so
that a third one cannot hide:

**(a) candidate-Fc consistency.** Legacy reads the SESSION's ``self._fc_hz`` at
five sites while planning a candidate cornered somewhere else; the planner
reads the candidate's own context. The isolation trick is that legacy's corner
is a *field*: point a legacy conductor at the candidate's corner and its Fc
behaviour becomes the planner's by construction. So
:func:`_legacy` takes the corner to run at, and
:func:`test_the_planner_matches_legacy_run_at_the_candidates_own_corner`
asserts **bit-identical** output — every remaining difference between that and
a legacy run at the session corner is class (a), and nothing else.

**(b) honest trim fallback.** Beyond
``LINEARIZATION_TRIM_SANITY_MARGIN_DB`` legacy commits whichever pair levels
better; the planner commits the anchor. The isolation trick is that a scan
which lands exactly ON the anchor makes legacy commit the anchor too — so a
legacy run with ``scan_delta_db=0`` produces, through production code, the very
pair the planner falls back to. That is the comparison in
:func:`test_a_wild_scan_commits_exactly_the_pair_legacy_reaches_by_not_drifting`.

**Where the fixture is synthetic, and what that costs.** The two seams whose
true inputs are the incident's un-committable per-driver responses —
``fit_driver_linearization`` and ``solve_ripple_optimal_trim`` — are stubbed,
identically for both implementations, exactly as
``tests/test_crossover_v2_incident_replay.py`` stubs them (see that module's
docstring for the size argument). The consequence for *this* module is narrow
and worth naming: because the fit stub ignores its ``radiating_band_hz``, the
corner's effect on the FIT is not exercised here. That is deliberate — it
isolates the Fc *arithmetic*, which is what class (a) is about — and the fit's
own sensitivity to the candidate's sections is already pinned by
``test_the_fit_reads_the_session_corner_while_the_candidate_carries_its_own``.

The ripple stub returns ``seed + scan_delta_db`` rather than a fixed number, so
a scenario can put the scan inside or outside the sanity margin without
changing anything else. ``scan_delta_db`` defaults to the incident's own
−6.300 dB drift.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2.contracts import (
    CandidateAcousticContext,
    TrimStrategy,
)
from tests.test_crossover_v2_incident_replay import (
    ANCHORED_DB,
    CANDIDATE_FIT,
    COMMITTED_DB,
    CONFIGURED_FC_HZ,
    SELECTED_FC_HZ,
    _analysis,
    _conductor,
    _incident_fit,
)

#: The incident's own scan drift: the ripple optimum sat this far below the
#: level-preserving anchor, 0.300 dB past the 6.0 dB sanity margin.
INCIDENT_SCAN_DELTA_DB = float(COMMITTED_DB["tweeter"]) - float(
    ANCHORED_DB["tweeter"]
)


# --------------------------------------------------------------------------- #
# one comparable result, from either implementation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Outcome:
    """Everything either implementation produces, in one comparable shape.

    Legacy scatters these across a 2-tuple return and seven ``self._last_*``
    fields; the planner returns them on one object. Normalizing both into this
    record is what makes "compare complete outputs" a single ``==``.
    """

    role_attenuations_db: dict[str, float]
    linearization: dict[str, Any]
    outcome: str
    level_frame_disagreement_db: float
    level_frame_cores: dict[str, Any]
    level_frame_trims: dict[str, float]
    level_frame_system_db: float | None
    level_frame_reference_role: str | None
    realized_level_match: tuple[Any, ...]
    predicted_sum_hz: tuple[float, ...]
    predicted_sum_db: tuple[float, ...]

    def differing_fields(self, other: "Outcome") -> list[str]:
        """Field names on which these two disagree — the classification input."""

        return [
            name
            for name in self.__dataclass_fields__
            if getattr(self, name) != getattr(other, name)
        ]

    #: The fields that derive from WHICH trim pair was committed. Everything
    #: outside this set must be identical between two runs that differ only in
    #: the commit choice — that is what makes class (b) a *bounded* difference
    #: rather than "some numbers moved".
    TRIM_DERIVED = frozenset(
        {
            "role_attenuations_db",
            "linearization",
            "realized_level_match",
            "predicted_sum_db",
        }
    )


def _match_tuple(match: Any) -> tuple[Any, ...]:
    if match is None:
        return ()
    return (
        bool(match.matched),
        float(match.level_w_db),
        float(match.level_t_db),
        float(match.difference_db),
        float(match.tolerance_db),
        tuple(float(v) for v in match.woofer_band_hz),
        tuple(float(v) for v in match.tweeter_band_hz),
    )


def _sections_at(fc_hz: float) -> dict[str, tuple[Any, ...]]:
    """The session preset's sections, re-cornered — ``_fc_candidate_sections``."""

    return _conductor()._fc_candidate_sections(fc_hz)


# --------------------------------------------------------------------------- #
# stubs, applied identically to both namespaces
# --------------------------------------------------------------------------- #


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch, *, scan_delta_db: float
) -> dict[str, list[float]]:
    """Stub the two un-replayable seams in BOTH module namespaces.

    Patching one namespace only would compare two implementations running
    different arithmetic, which is the failure mode this whole module exists to
    detect — so every patch is applied to ``flow`` and ``intervention`` from one
    definition, and the Fc each seam is handed is recorded per namespace.
    """
    seen: dict[str, list[float]] = {"legacy": [], "pure": []}

    def ripple(freqs, w_lin, t_lin, fc_hz, **kwargs):
        return (
            float(kwargs["seed_trim_db"]) + float(scan_delta_db),
            float(CANDIDATE_FIT["analysis"]["predicted_ripple_db"]),
            float(kwargs["seed_trim_db"]),
        )

    def fit(resp, envelope, **kwargs):
        return _incident_fit(resp.role)

    real_overlap = flow.overlap_band_hz

    def overlap_for(which: str) -> Callable[..., Any]:
        def spy(fc_hz, **kwargs):
            seen[which].append(float(fc_hz))
            return real_overlap(fc_hz, **kwargs)

        return spy

    for module, which in ((flow, "legacy"), (iv, "pure")):
        monkeypatch.setattr(module, "solve_ripple_optimal_trim", ripple)
        monkeypatch.setattr(module, "fit_driver_linearization", fit)
        monkeypatch.setattr(module, "overlap_band_hz", overlap_for(which))
    return seen


# --------------------------------------------------------------------------- #
# the two runs
# --------------------------------------------------------------------------- #


def _legacy(fc_hz: float, sections: dict[str, Any]) -> Outcome:
    """``_fit_linearization`` as it stands, run at the corner ``fc_hz``.

    ``fc_hz`` is written onto the conductor because that is precisely where
    legacy reads its corner from — the defect and the isolation lever are the
    same field.
    """
    conductor = _conductor()
    conductor._fc_hz = float(fc_hz)
    analysis = _analysis(CANDIDATE_FIT["program_id"])
    trims, linearization = conductor._fit_linearization(
        analysis, analysis.candidate, None, candidate_sections=sections
    )
    frame = conductor._last_level_frame
    predicted = conductor._last_linearized_predicted_sum
    return Outcome(
        role_attenuations_db=dict(trims),
        linearization=dict(linearization),
        outcome=conductor._last_linearization_outcome,
        level_frame_disagreement_db=float(
            conductor._last_level_frame_disagreement_db
        ),
        level_frame_cores=dict(conductor._last_level_frame_cores),
        level_frame_trims=dict(conductor._last_level_frame_trims),
        level_frame_system_db=(
            None if frame is None else float(frame.system_level_db)
        ),
        level_frame_reference_role=(None if frame is None else frame.reference_role),
        realized_level_match=_match_tuple(conductor._last_realized_level_match),
        predicted_sum_hz=tuple(float(v) for v in predicted[0]),
        predicted_sum_db=tuple(float(v) for v in predicted[1]),
    )


def _pure(sections: dict[str, Any]) -> tuple[Outcome, iv.LinearizationPlan]:
    """:func:`~...intervention.plan_linearization` over the same inputs.

    The corner is never passed: it is derived from the sections themselves by
    :meth:`CandidateAcousticContext.from_sections`, which is the whole point —
    there is no second place for a corner to come from.
    """
    conductor = _conductor()
    analysis = _analysis(CANDIDATE_FIT["program_id"])
    program = conductor._program_for_phase(flow.PHASE_MEASURE)
    seg_w, seg_t = program.segment("sweep_w"), program.segment("sweep_t")
    request = iv.request_from_analysis(
        analysis,
        analysis.candidate,
        context=CandidateAcousticContext.from_sections(sections),
        woofer_role=conductor._woofer.role,
        tweeter_role=conductor._tweeter.role,
        excited_band_hz={
            conductor._woofer.role: (seg_w.f1_hz, seg_w.f2_hz),
            conductor._tweeter.role: (seg_t.f1_hz, seg_t.f2_hz),
        },
        driver_class_by_role=conductor._driver_class_by_role,
        post_apply_verifies=conductor._post_apply_verifies,
        cloud_phase_planned=flow.PHASE_CLOUD_MEASURE in conductor._phases,
        cloud=None,
    )
    plan = iv.plan_linearization(request)
    return (
        Outcome(
            role_attenuations_db=dict(plan.role_attenuations_db),
            linearization=dict(plan.linearization),
            outcome=plan.outcome,
            level_frame_disagreement_db=float(plan.level_frame_disagreement_db),
            level_frame_cores=dict(plan.level_frame_cores),
            level_frame_trims=dict(plan.level_frame_trims),
            level_frame_system_db=(
                None
                if plan.level_frame is None
                else float(plan.level_frame.system_level_db)
            ),
            level_frame_reference_role=(
                None if plan.level_frame is None else plan.level_frame.reference_role
            ),
            realized_level_match=_match_tuple(plan.realized_level_match),
            predicted_sum_hz=tuple(float(v) for v in plan.linearized_predicted_sum[0]),
            predicted_sum_db=tuple(float(v) for v in plan.linearized_predicted_sum[1]),
        ),
        plan,
    )


# --------------------------------------------------------------------------- #
# class (a) — the corner, isolated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "scan_delta_db, label",
    [
        (0.0, "scan lands on the anchor"),
        (-2.5, "scan drifts, inside the margin"),
        (2.5, "scan drifts upward, inside the margin"),
    ],
)
def test_the_planner_matches_legacy_run_at_the_candidates_own_corner(
    monkeypatch, scan_delta_db, label
):
    """**Zero differences.** The port is faithful; only the corner ever moved.

    This is the load-bearing assertion of the whole extraction. Legacy is run
    with its ``_fc_hz`` field set to the candidate's corner — the one value the
    planner reads from its context — and every output must then agree
    exactly, including the full-resolution predicted-sum arrays.

    Scan deltas are all inside the sanity margin here so the trim policy cannot
    contribute; class (b) is isolated separately below. A failure here is not a
    sanctioned difference — it is a defect in the reimplementation.
    """
    _install_stubs(monkeypatch, scan_delta_db=scan_delta_db)
    sections = _sections_at(SELECTED_FC_HZ)
    legacy = _legacy(SELECTED_FC_HZ, sections)
    pure, _plan = _pure(sections)
    assert pure.differing_fields(legacy) == [], (
        f"#2291 Phase 2 ({label}): the pure planner must be byte-identical to "
        "legacy run at the same corner; a difference here is unexplained"
    )


def test_the_session_corner_is_what_legacy_reads_and_the_planner_never_does(
    monkeypatch,
):
    """Class (a), named: the same sections, two corners, two different answers.

    Establishes that the corner is *load-bearing* — otherwise the equality
    above would be vacuous, holding because nothing in the plan depends on Fc.
    Legacy at the session's 2,000 Hz and legacy at the candidate's 1,648.7 Hz
    disagree; the planner reproduces the second and never the first.
    """
    seen = _install_stubs(monkeypatch, scan_delta_db=-2.5)
    sections = _sections_at(SELECTED_FC_HZ)

    at_session = _legacy(CONFIGURED_FC_HZ, sections)
    at_candidate = _legacy(SELECTED_FC_HZ, sections)
    assert at_session.differing_fields(at_candidate), (
        "the corner must change the plan, or this suite proves nothing"
    )

    pure, plan = _pure(sections)
    assert pure.differing_fields(at_candidate) == []
    assert pure.differing_fields(at_session) != []
    assert plan.fc_hz == SELECTED_FC_HZ

    # The seams themselves, not just the numbers downstream of them: legacy saw
    # both corners across its two runs, the planner only ever the candidate's.
    assert set(seen["legacy"]) == {CONFIGURED_FC_HZ, SELECTED_FC_HZ}
    assert set(seen["pure"]) == {SELECTED_FC_HZ}


# --------------------------------------------------------------------------- #
# class (b) — the trim policy, isolated
# --------------------------------------------------------------------------- #


def test_a_wild_scan_commits_exactly_the_pair_legacy_reaches_by_not_drifting(
    monkeypatch,
):
    """Class (b), bounded: only the trim-derived fields move, and to the anchor.

    Run at the SESSION's own corner, so class (a) contributes nothing and the
    trim policy is the only thing that can differ. Two legacy runs bracket the
    change. One with the incident's own −6.300 dB drift is what shipped: the
    scan levels better, so legacy commits it under the outcome string
    ``trim_rejected``. One with ``scan_delta_db=0`` puts the scan ON the anchor,
    so legacy commits the anchored pair — through production code, not through
    a number this test chose.

    The planner, given the drifting scan, must equal the second: same anchor,
    same everything, and differences from the first confined to
    :attr:`Outcome.TRIM_DERIVED`.
    """
    sections = _sections_at(CONFIGURED_FC_HZ)

    with pytest.MonkeyPatch.context() as patch:
        _install_stubs(patch, scan_delta_db=INCIDENT_SCAN_DELTA_DB)
        legacy_wild = _legacy(CONFIGURED_FC_HZ, sections)
        pure, plan = _pure(sections)

    # The premise: this scan really is beyond the margin, and legacy really did
    # commit it. Asserted rather than assumed — a fixture that stopped
    # reproducing the shape would otherwise silently pass a weaker test.
    assert abs(INCIDENT_SCAN_DELTA_DB) > iv.LINEARIZATION_TRIM_SANITY_MARGIN_DB
    assert legacy_wild.outcome == "trim_rejected"
    assert legacy_wild.role_attenuations_db["tweeter"] == pytest.approx(
        float(ANCHORED_DB["tweeter"]) + INCIDENT_SCAN_DELTA_DB, abs=1e-9
    ), "legacy committed the drifted scan — the behaviour #2291 changes"

    # Every difference is trim-derived...
    moved = set(pure.differing_fields(legacy_wild))
    assert moved <= Outcome.TRIM_DERIVED, (
        f"#2291: unexplained non-trim differences {sorted(moved - Outcome.TRIM_DERIVED)}"
    )
    assert moved, "the policy change must actually change something"

    # ...and what it moved to is the anchor legacy itself reaches when the scan
    # does not drift.
    with pytest.MonkeyPatch.context() as patch:
        _install_stubs(patch, scan_delta_db=0.0)
        legacy_anchor = _legacy(CONFIGURED_FC_HZ, sections)
    assert legacy_anchor.role_attenuations_db["tweeter"] == pytest.approx(
        ANCHORED_DB["tweeter"], abs=1e-9
    )
    for name in Outcome.TRIM_DERIVED:
        assert getattr(pure, name) == getattr(legacy_anchor, name), (
            f"#2291: the fallback's {name} is not the anchored pair's"
        )

    # The outcome string is the one field that legitimately differs from BOTH:
    # the drift happened (so it is not the no-drift run's "fitted") and the trim
    # really was rejected (so, unlike legacy's, the word is now true).
    assert plan.outcome == "trim_rejected"
    assert legacy_anchor.outcome == "fitted"
    assert plan.trim.strategy is TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT


def test_at_the_candidates_corner_the_level_grading_already_prefers_the_anchor(
    monkeypatch,
):
    """A finding, pinned so it is not mistaken for the policy doing the work.

    The two sanctioned fixes are independent in principle but not on this
    fixture: at the candidate's own 1,648.7 Hz corner the anchored pair already
    realizes the better inter-driver level (+2.376 dB against the scan's
    −3.924 dB), so legacy — run at the right corner — would have committed the
    anchor by its OWN level grading, before the trim policy is consulted at all.

    Two things follow, and both matter for reading the dual-run table. The
    incident's −13.013 dB trim is downstream of the corner defect, not only of
    the trim-policy defect. And class (b)'s effect cannot be isolated at this
    corner, which is why the test above uses the session's.

    The level errors here are production's arithmetic over the fixture's
    SYNTHETIC branches (the incident's own responses are too large to commit),
    so the ORDERING is the claim, not the two dB values.
    """
    _install_stubs(monkeypatch, scan_delta_db=INCIDENT_SCAN_DELTA_DB)
    sections = _sections_at(SELECTED_FC_HZ)
    legacy_at_candidate = _legacy(SELECTED_FC_HZ, sections)
    pure, plan = _pure(sections)

    assert legacy_at_candidate.role_attenuations_db["tweeter"] == pytest.approx(
        ANCHORED_DB["tweeter"], abs=1e-9
    )
    # Same committed pair, reached two different ways — legacy by grading,
    # the planner by policy. The strategy is what distinguishes them.
    assert pure.differing_fields(legacy_at_candidate) == []
    assert plan.trim.strategy is TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
    assert abs(plan.trim.anchored_match.difference_db) < abs(
        plan.trim.resolved_match.difference_db
    )


def test_the_replay_cannot_emit_the_incident_trim_through_a_rejected_path(monkeypatch):
    """#2291's literal acceptance criterion, as one assertion.

    "The exact jts3 replay cannot emit the −13.013 dB tweeter trim through a
    'trim_rejected' path." It emits −6.713 dB with a strategy that says the
    scan was rejected and the anchor committed.
    """
    _install_stubs(monkeypatch, scan_delta_db=INCIDENT_SCAN_DELTA_DB)
    _pure_outcome, plan = _pure(_sections_at(SELECTED_FC_HZ))

    assert plan.outcome == "trim_rejected"
    assert plan.role_attenuations_db["tweeter"] != pytest.approx(
        COMMITTED_DB["tweeter"]
    )
    assert plan.role_attenuations_db["tweeter"] == pytest.approx(
        ANCHORED_DB["tweeter"], abs=1e-9
    )
    assert plan.trim.strategy is TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
    assert "rejected" in plan.trim.rationale
    # Hearing-safety direction, stated as a test rather than a comment: the
    # fallback RAISES the tweeter (less attenuation), and the bound on that
    # rise is the drift itself. It is still a cut.
    rise_db = plan.role_attenuations_db["tweeter"] - float(COMMITTED_DB["tweeter"])
    assert rise_db == pytest.approx(abs(INCIDENT_SCAN_DELTA_DB), abs=1e-9)
    assert all(v <= 0.0 for v in plan.role_attenuations_db.values())


# --------------------------------------------------------------------------- #
# the classification, as one table
# --------------------------------------------------------------------------- #


def test_every_difference_from_legacy_classifies_into_the_two_sanctioned_fixes(
    monkeypatch,
):
    """The dual-run's summary claim: no third class of difference exists.

    Walks the four corners of the (corner × drift) space against legacy's own
    session-corner behaviour and asserts each cell's differences are entirely
    accounted for by (a), (b), or both — where "accounted for" means the
    planner's output equals a legacy run reconstructed under that classification,
    not merely that a plausible label was attached.
    """
    sections_session = _sections_at(CONFIGURED_FC_HZ)
    sections_candidate = _sections_at(SELECTED_FC_HZ)

    for sections, corner, drift, classes in (
        (sections_session, CONFIGURED_FC_HZ, -2.5, set()),
        (sections_candidate, SELECTED_FC_HZ, -2.5, {"a"}),
        (sections_session, CONFIGURED_FC_HZ, INCIDENT_SCAN_DELTA_DB, {"b"}),
        (sections_candidate, SELECTED_FC_HZ, INCIDENT_SCAN_DELTA_DB, {"a", "b"}),
    ):
        with pytest.MonkeyPatch.context() as patch:
            _install_stubs(patch, scan_delta_db=drift)
            baseline = _legacy(CONFIGURED_FC_HZ, sections)
            reference = _legacy(corner, sections)
            pure, _plan = _pure(sections)

            moved_from_baseline = set(pure.differing_fields(baseline))
            if not classes:
                assert moved_from_baseline == set(), (
                    "with the session corner and an in-margin scan the planner "
                    "must be indistinguishable from legacy"
                )
                continue
            if "a" in classes:
                # Class (a) is fully explained iff legacy at the candidate's
                # corner accounts for it.
                assert set(reference.differing_fields(baseline))
            if "b" not in classes:
                assert pure.differing_fields(reference) == []
            else:
                assert set(pure.differing_fields(reference)) <= Outcome.TRIM_DERIVED
            assert moved_from_baseline, "a classified cell must differ from baseline"


# --------------------------------------------------------------------------- #
# purity
# --------------------------------------------------------------------------- #


def test_planning_twice_over_one_request_returns_equal_output(monkeypatch):
    """Same inputs, same outputs — and the request survives being planned from.

    A planner that mutated its request would still look deterministic on the
    first call, so the second plan is compared field by field AND the request's
    own trim mappings are compared before and after.
    """
    _install_stubs(monkeypatch, scan_delta_db=-2.5)
    sections = _sections_at(SELECTED_FC_HZ)
    conductor = _conductor()
    analysis = _analysis(CANDIDATE_FIT["program_id"])
    program = conductor._program_for_phase(flow.PHASE_MEASURE)
    seg_w, seg_t = program.segment("sweep_w"), program.segment("sweep_t")
    request = iv.request_from_analysis(
        analysis,
        analysis.candidate,
        context=CandidateAcousticContext.from_sections(sections),
        woofer_role="woofer",
        tweeter_role="tweeter",
        excited_band_hz={
            "woofer": (seg_w.f1_hz, seg_w.f2_hz),
            "tweeter": (seg_t.f1_hz, seg_t.f2_hz),
        },
        driver_class_by_role=conductor._driver_class_by_role,
        post_apply_verifies=True,
        cloud_phase_planned=False,
    )
    before = (dict(request.raw_trim_db), dict(request.trim_band_average_db))

    first = iv.plan_linearization(request)
    second = iv.plan_linearization(request)

    assert (dict(request.raw_trim_db), dict(request.trim_band_average_db)) == before
    assert dict(first.role_attenuations_db) == dict(second.role_attenuations_db)
    assert dict(first.linearization) == dict(second.linearization)
    assert first.outcome == second.outcome
    assert np.array_equal(
        first.linearized_predicted_sum[1], second.linearized_predicted_sum[1]
    )
    assert [r.event for r in first.journal] == [r.event for r in second.journal]


def test_the_request_snapshots_the_trim_mappings_it_was_handed():
    """A caller mutating its own dict cannot change a request already built.

    The two trim mappings are read at four points spread across the plan, so a
    live reference would let one run see two different inputs — a result that
    matches no single request. Mutating the caller's dict after construction is
    the only way to observe this, which is why determinism alone does not pin
    it.
    """
    raw = {"woofer": 0.0, "tweeter": -10.0}
    average = {"woofer": 0.0, "tweeter": -9.0}
    evidence = iv.DriverEvidence(
        role="woofer", response=object(), excited_band_hz=(100.0, 2000.0)
    )
    request = iv.LinearizationRequest(
        context=CandidateAcousticContext.from_sections(_sections_at(SELECTED_FC_HZ)),
        woofer=evidence,
        tweeter=replace(evidence, role="tweeter"),
        raw_trim_db=raw,
        trim_band_average_db=average,
        predicted_ripple_db=0.0,
        polarity_sign=1,
        delay_us=0.0,
        anchor_delay_us=None,
        mic_tier="reference",
        branch_floor_hz=None,
        post_apply_verifies=True,
        cloud_phase_planned=False,
    )
    raw["tweeter"] = -99.0
    average["tweeter"] = -99.0
    raw["subwoofer"] = -3.0

    assert request.raw_trim_db == {"woofer": 0.0, "tweeter": -10.0}
    assert request.trim_band_average_db == {"woofer": 0.0, "tweeter": -9.0}


def test_the_planner_writes_no_conductor_state(monkeypatch):
    """No ``_last_*`` field moves while the pure planner runs.

    The scratch fields are the implicit return channel #2291 is removing. This
    pins that the planner does not use them even incidentally — it is handed no
    conductor, so the only way it could is through an import, and an import is
    exactly what a future edit might reach for.
    """
    _install_stubs(monkeypatch, scan_delta_db=-2.5)
    conductor = _conductor()
    watched = (
        "_last_level_frame",
        "_last_level_frame_cores",
        "_last_level_frame_disagreement_db",
        "_last_level_frame_trims",
        "_last_linearization_outcome",
        "_last_linearized_predicted_sum",
        "_last_realized_level_match",
    )
    sentinels = {name: object() for name in watched}
    for name, sentinel in sentinels.items():
        setattr(conductor, name, sentinel)

    _pure(_sections_at(SELECTED_FC_HZ))

    for name, sentinel in sentinels.items():
        assert getattr(conductor, name) is sentinel, f"the planner touched {name}"


def test_the_journal_port_receives_every_record_in_plan_order(monkeypatch):
    """The disclosure port streams exactly what the plan carries, in order.

    The port exists so a fit that raises part-way still discloses the lines it
    reached; that is only true if a record reaches the port when it is
    produced rather than at the end.
    """
    _install_stubs(monkeypatch, scan_delta_db=INCIDENT_SCAN_DELTA_DB)
    conductor = _conductor()
    analysis = _analysis(CANDIDATE_FIT["program_id"])
    program = conductor._program_for_phase(flow.PHASE_MEASURE)
    seg_w, seg_t = program.segment("sweep_w"), program.segment("sweep_t")
    request = iv.request_from_analysis(
        analysis,
        analysis.candidate,
        context=CandidateAcousticContext.from_sections(_sections_at(SELECTED_FC_HZ)),
        woofer_role="woofer",
        tweeter_role="tweeter",
        excited_band_hz={
            "woofer": (seg_w.f1_hz, seg_w.f2_hz),
            "tweeter": (seg_t.f1_hz, seg_t.f2_hz),
        },
        driver_class_by_role=conductor._driver_class_by_role,
        post_apply_verifies=True,
        cloud_phase_planned=False,
    )
    streamed: list[iv.JournalRecord] = []
    plan = iv.plan_linearization(request, journal=streamed.append)
    assert streamed == list(plan.journal)
    assert [r.event for r in plan.journal] == [
        "correction.crossover_v2_linearization_fit_band",
        "correction.crossover_v2_linearization_giveback",
        "correction.crossover_v2_linearization_trim_rejected",
        "correction.crossover_v2_realized_level_match",
        "correction.crossover_v2_linearization_headroom",
    ]

    # The port's reason for existing: a fit that raises still discloses.
    partial: list[iv.JournalRecord] = []

    def boom(resp, envelope, **kwargs):
        raise RuntimeError("fit engine bug")

    monkeypatch.setattr(iv, "fit_driver_linearization", boom)
    with pytest.raises(RuntimeError):
        iv.plan_linearization(request, journal=partial.append)
    assert [r.event for r in partial] == [
        "correction.crossover_v2_linearization_fit_band"
    ]


# --------------------------------------------------------------------------- #
# the corner cannot be smuggled in
# --------------------------------------------------------------------------- #


def test_a_context_cannot_be_built_from_sections_naming_two_corners():
    """The 2026-08-10 shape, refused at construction rather than planned.

    Not a message check: the refusal is a *type*, which is what
    ``planner_facade`` classifies on.
    """
    from jasper.active_speaker.crossover_v2.contracts import (
        CandidateFcDisagreementError,
    )

    mixed = {
        "woofer": _sections_at(CONFIGURED_FC_HZ)["woofer"],
        "tweeter": _sections_at(SELECTED_FC_HZ)["tweeter"],
    }
    with pytest.raises(CandidateFcDisagreementError) as caught:
        CandidateAcousticContext.from_sections(mixed)
    assert caught.value.refusal_reason == "candidate_fc_disagreement"


def test_the_request_refuses_a_context_whose_corner_disagrees_with_its_sections():
    """A hand-built context cannot re-open the hole ``from_sections`` closes."""

    from jasper.active_speaker.crossover_v2.contracts import (
        CandidateFcDisagreementError,
    )

    sections = _sections_at(SELECTED_FC_HZ)
    with pytest.raises(CandidateFcDisagreementError):
        CandidateAcousticContext(fc_hz=CONFIGURED_FC_HZ, sections_by_role=sections)


def test_every_fc_driven_seam_reads_the_context_and_no_other_value(monkeypatch):
    """The Fc-ownership property, with a decoy session corner in scope.

    Every seam that takes a corner is spied inside the planner's own namespace
    while a conductor carrying a *different* corner sits in the same test. All
    three must see the context's, and the decoy must appear nowhere.
    """
    _install_stubs(monkeypatch, scan_delta_db=-2.5)
    seen: dict[str, list[float]] = {"ripple": [], "match": []}

    stub_ripple = iv.solve_ripple_optimal_trim
    real_match = iv.realized_branch_level_match

    def ripple(freqs, w_lin, t_lin, fc_hz, **kwargs):
        seen["ripple"].append(float(fc_hz))
        return stub_ripple(freqs, w_lin, t_lin, fc_hz, **kwargs)

    def match(freqs, w_tf, t_tf, fc_hz, **kwargs):
        seen["match"].append(float(fc_hz))
        return real_match(freqs, w_tf, t_tf, fc_hz, **kwargs)

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", ripple)
    monkeypatch.setattr(iv, "realized_branch_level_match", match)

    _outcome, plan = _pure(_sections_at(SELECTED_FC_HZ))

    assert seen["ripple"] == [SELECTED_FC_HZ]
    assert seen["match"] == [SELECTED_FC_HZ, SELECTED_FC_HZ]  # both graded pairs
    assert plan.fc_hz == SELECTED_FC_HZ
    for record in plan.journal:
        if "fc_hz" in record.fields:
            assert record.fields["fc_hz"] == pytest.approx(SELECTED_FC_HZ, abs=1e-3)
            assert record.fields["fc_hz"] != pytest.approx(CONFIGURED_FC_HZ)


def test_the_skipped_scan_journal_names_the_candidate_corner(monkeypatch):
    """The sixth Fc site, which the incident's own band could not distinguish.

    ``test_the_fit_reads_the_session_corner…`` says so explicitly: on the
    incident's 1600-4000 Hz overlap band both corners straddle, so the
    else-branch never runs and no assertion about that session can tell the two
    apart. Forcing a one-sided band reaches it, and the record it emits carries
    the candidate's corner.
    """
    _install_stubs(monkeypatch, scan_delta_db=0.0)
    sections = _sections_at(SELECTED_FC_HZ)

    # A corner below the whole overlap band makes the straddle test false.
    def one_sided(fc_hz, **kwargs):
        return (float(fc_hz) + 100.0, float(fc_hz) + 900.0)

    monkeypatch.setattr(iv, "overlap_band_hz", one_sided)
    _outcome, plan = _pure(sections)

    skipped = [
        r
        for r in plan.journal
        if r.event == "correction.crossover_v2_linearization_ripple_trim_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0].fields["fc_hz"] == pytest.approx(SELECTED_FC_HZ, abs=1e-3)
    assert skipped[0].fields["reason"] == "ripple_band_one_sided"
    # A skipped scan leaves the trim AT the anchor, so the drift is zero and the
    # sanity guard cannot fire — the ``resolved_ripple_db is None`` branch the
    # trim-rejected record documents as unreachable.
    assert plan.trim.ripple_db is None
    assert plan.trim.anchor_drift_db == 0.0
    assert plan.trim.strategy is TrimStrategy.ANCHORED_COMMITTED


# --------------------------------------------------------------------------- #
# the trim policy, as a table over its own inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FakeMatch:
    difference_db: float
    matched: bool = True
    level_w_db: float = 0.0
    level_t_db: float = 0.0
    tolerance_db: float = 3.0
    woofer_band_hz: tuple[float, float] = (100.0, 1000.0)
    tweeter_band_hz: tuple[float, float] = (2000.0, 16000.0)


@pytest.mark.parametrize(
    "drift_db, anchor_error, scan_error, expected_strategy, expected_tweeter",
    [
        # within margin — the level grading decides, ties to the anchor
        (2.0, 0.2, 2.5, TrimStrategy.ANCHORED_COMMITTED, -6.0),
        (2.0, 2.5, 0.2, TrimStrategy.RESOLVED_COMMITTED, -8.0),
        (2.0, 1.0, -1.0, TrimStrategy.ANCHORED_COMMITTED, -6.0),
        # exactly ON the margin is still within it
        (6.0, 5.0, 0.1, TrimStrategy.RESOLVED_COMMITTED, -12.0),
        # beyond the margin — the anchor is committed WHICHEVER pair levels
        # better, which is the whole of #2291's honest-fallback change
        (6.5, 5.0, 0.1, TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT, -6.0),
        (6.5, 0.1, 5.0, TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT, -6.0),
    ],
)
def test_the_trim_policy_table(
    drift_db, anchor_error, scan_error, expected_strategy, expected_tweeter
):
    """Every cell of (within/beyond margin) × (anchor/scan levels better).

    ``decide_trim`` is pure and takes both graded pairs, so the policy is
    testable without a fit, a response, or a conductor — which is the point of
    having extracted it.
    """
    anchored = {"woofer": -1.0, "tweeter": -6.0}
    resolved = {"woofer": -1.0, "tweeter": -6.0 - drift_db}
    decision = iv.decide_trim(
        anchored_db=anchored,
        resolved_db=resolved,
        tweeter_role="tweeter",
        anchored_match=_FakeMatch(anchor_error),
        resolved_match=_FakeMatch(scan_error),
        ripple_db=0.4,
    )
    assert decision.strategy is expected_strategy
    assert decision.committed_db["tweeter"] == pytest.approx(expected_tweeter)
    assert decision.anchor_drift_db == pytest.approx(drift_db)
    assert decision.beyond_sanity_margin is (
        drift_db > iv.LINEARIZATION_TRIM_SANITY_MARGIN_DB
    )
    # The name never says "rejected" about a pair that shipped, and the outcome
    # string tracks the drift rather than the commit.
    assert "reject" not in decision.strategy.value or decision.committed_db == dict(
        anchored
    )
    assert decision.outcome == (
        "trim_rejected" if decision.beyond_sanity_margin else "fitted"
    )
    assert decision.committed_match.difference_db == (
        anchor_error if decision.committed_db == dict(anchored) else scan_error
    )


def test_a_rejected_scan_can_never_be_the_committed_pair():
    """The invariant behind the enum, swept rather than argued.

    #2291's acceptance criterion is a universal, so it is tested as one: across
    a range of drifts and both level orderings, a beyond-margin scan is never
    what ships.
    """
    anchored = {"woofer": -1.0, "tweeter": -6.0}
    for drift_db in (6.001, 7.0, 12.0, 40.0):
        for anchor_error, scan_error in ((0.1, 5.0), (5.0, 0.1), (1.0, 1.0)):
            resolved = {"woofer": -1.0, "tweeter": -6.0 - drift_db}
            decision = iv.decide_trim(
                anchored_db=anchored,
                resolved_db=resolved,
                tweeter_role="tweeter",
                anchored_match=_FakeMatch(anchor_error),
                resolved_match=_FakeMatch(scan_error),
                ripple_db=None,
            )
            assert decision.committed_db == dict(anchored)
            assert decision.outcome == "trim_rejected"
            assert (
                decision.strategy
                is TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
            )
            # Hearing safety: falling back can only move the committed trim
            # toward the anchor, and the anchor is never a boost.
            assert decision.committed_db["tweeter"] <= 0.0


def test_the_request_refuses_inputs_the_eligibility_gate_should_have_caught():
    """Malformed inputs become typed refusals, never a plan built on a guess."""

    sections = _sections_at(SELECTED_FC_HZ)
    context = CandidateAcousticContext.from_sections(sections)
    evidence = iv.DriverEvidence(
        role="woofer", response=object(), excited_band_hz=(100.0, 2000.0)
    )
    common = dict(
        context=context,
        raw_trim_db={},
        trim_band_average_db={},
        predicted_ripple_db=0.0,
        polarity_sign=1,
        delay_us=0.0,
        anchor_delay_us=None,
        mic_tier="reference",
        branch_floor_hz=None,
        post_apply_verifies=True,
        cloud_phase_planned=False,
    )
    with pytest.raises(iv.PlannerInputError):
        iv.LinearizationRequest(
            woofer=evidence, tweeter=replace(evidence, role="woofer"), **common
        )
    with pytest.raises(iv.PlannerInputError):
        iv.LinearizationRequest(
            woofer=evidence,
            tweeter=replace(evidence, role="tweeter"),
            **{**common, "polarity_sign": 0},
        )
    with pytest.raises(iv.PlannerInputError):
        iv.LinearizationRequest(
            woofer=replace(evidence, response=None),
            tweeter=replace(evidence, role="tweeter"),
            **common,
        )
