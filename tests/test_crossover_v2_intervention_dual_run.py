# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The pure planner, over the banked 2026-08-10 jts3 inputs (#2291 Phase 2).

**What this module was, and what it is now.** Phase 2a ran the pure planner in
:mod:`jasper.active_speaker.crossover_v2.intervention` side by side with the
legacy ``CrossoverV2Session._fit_linearization`` it reimplements, and
classified every difference into the two changes #2291 sanctions — candidate-Fc
consistency, and the honest trim fallback. That comparison did its job: it
proved bit-identical output at the candidate's own corner, and it is why the
cutover could be made with the differences named rather than discovered.

Phase 2b deleted legacy, so the dual run has no second implementation to run.
The tests that needed one are retired; what remains is everything that was
always about the planner alone — determinism, input contracts, the Fc
invariant, the journal port, the trim policy, and the cut-only invariant —
plus the incident fixture replayed through the planner.

**The replacement for the retired half is
``tests/test_crossover_v2_incident_replay.py``**, which since the cutover
drives the same banked numbers through the PRODUCTION path (``_build_candidate``
at the keyword pair the Fc sweep hands it) and asserts the fixed behaviour
rather than a delta against something deleted. Retired here, covered there:
bit-identity at the candidate's corner, the session-corner contrast, the
wild-scan commit comparison, the difference-classification table, and the
line-for-line journal parity.

**Where the fixture is synthetic, and what that costs.** The two seams whose
true inputs are the incident's un-committable per-driver responses —
``fit_driver_linearization`` and ``solve_ripple_optimal_trim`` — are stubbed,
exactly as ``tests/test_crossover_v2_incident_replay.py`` stubs them (see that
module's docstring for the size argument). The consequence for *this* module is
narrow and worth naming: because the fit stub ignores its ``radiating_band_hz``,
the corner's effect on the FIT is not exercised here. That is deliberate — it
isolates the Fc *arithmetic* — and the fit's own sensitivity to the candidate's
sections is pinned by
``test_crossover_v2_incident_replay.py::
test_every_fc_driven_seam_reads_the_candidates_corner_not_the_sessions``.

The ripple stub returns ``seed + scan_delta_db`` rather than a fixed number, so
a scenario can put the scan inside or outside the sanity margin without
changing anything else. ``scan_delta_db`` defaults to the incident's own
−6.300 dB drift.
"""
from __future__ import annotations

import logging
import types
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.branch_chain import radiating_band_hz
from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2.contracts import (
    CandidateAcousticContext,
    TrimStrategy,
)
from jasper.audio_measurement.program_analysis import (
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
)
from tests.crossover_v2_fixtures import _candidate_sections
from tests.test_crossover_v2_incident_replay import (
    ANCHORED_DB,
    CANDIDATE_FIT,
    COMMITTED_DB,
    CONFIGURED_FC_HZ,
    SELECTED_FC_HZ,
    _analysis,
    _conductor,
    _incident_fit,
    _response,
)

#: The incident's own scan drift: the ripple optimum sat this far below the
#: level-preserving anchor, 0.612 dB past the 6.0 dB sanity margin.
#:
#: DERIVED from the anchor rather than written down, and that is load-bearing.
#: The scan's ABSOLUTE result is the banked −13.013 dB; the stub reproduces it
#: as ``seed + delta``, so the delta has to move whenever the anchor does. It
#: has moved twice — 6.300 dB when the anchor carried PR-L5's offset, 7.864 dB
#: after #2609 deleted it, 6.612 dB now that the give-back is measured in the
#: trim's own band — and the banked −13.013 dB never moved at all.
INCIDENT_SCAN_DELTA_DB = float(COMMITTED_DB["tweeter"]) - float(
    ANCHORED_DB["tweeter"]
)


def _sections_at(fc_hz: float) -> dict[str, tuple[Any, ...]]:
    """The session preset's sections, re-cornered — ``_candidate_sections``."""

    return _candidate_sections(_conductor(), fc_hz)


# --------------------------------------------------------------------------- #
# stubs, applied identically to both namespaces
# --------------------------------------------------------------------------- #


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch, *, scan_delta_db: float
) -> list[float]:
    """Stub the two un-replayable seams, and record the corner each is handed.

    Applied to the planner's namespace alone since #2291 Phase 2b: the legacy
    fitter it used to be applied to in parallel no longer exists, and there is
    one implementation left to stub.
    """
    seen: list[float] = []

    def ripple(freqs, w_lin, t_lin, fc_hz, **kwargs):
        return (
            float(kwargs["seed_trim_db"]) + float(scan_delta_db),
            float(CANDIDATE_FIT["analysis"]["predicted_ripple_db"]),
            float(kwargs["seed_trim_db"]),
        )

    def fit(resp, envelope, **kwargs):
        return _incident_fit(resp.role)

    real_overlap = iv.overlap_band_hz

    def overlap_spy(fc_hz, **kwargs):
        seen.append(float(fc_hz))
        return real_overlap(fc_hz, **kwargs)

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", ripple)
    monkeypatch.setattr(iv, "fit_driver_linearization", fit)
    monkeypatch.setattr(iv, "overlap_band_hz", overlap_spy)
    return seen


# --------------------------------------------------------------------------- #
# the two runs
# --------------------------------------------------------------------------- #


def _planner_request(sections: dict[str, Any]) -> iv.LinearizationRequest:
    """The planner's inputs, derived from the same conductor legacy is given.

    The corner is never passed: it is derived from the sections themselves by
    :meth:`CandidateAcousticContext.from_sections`, which is the whole point —
    there is no second place for a corner to come from.
    """
    conductor = _conductor()
    analysis = _analysis(CANDIDATE_FIT["program_id"])
    program = conductor.program_for_phase(flow.PHASE_MEASURE)
    seg_w, seg_t = program.segment("sweep_w"), program.segment("sweep_t")
    return iv.request_from_analysis(
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
        post_apply_verifies=conductor.post_apply_verifies,
        cloud_phase_planned=flow.PHASE_CLOUD_MEASURE in conductor.session_phases,
        cloud=None,
    )


def _pure(sections: dict[str, Any]) -> iv.LinearizationPlan:
    """:func:`~...intervention.plan_linearization` over the same inputs."""

    return iv.plan_linearization(_planner_request(sections))


def _giveback_record(plan: iv.LinearizationPlan) -> dict[str, Any]:
    """The anchor's own journal line, as fields."""
    for record in plan.journal:
        if record.event == "correction.crossover_v2_linearization_giveback":
            return dict(record.fields)
    raise AssertionError("the planner emitted no give-back record")


@pytest.mark.parametrize("injected_delta_db", [0.75, -1.25])
def test_the_journal_reports_the_polish_delta_it_measured(
    monkeypatch, injected_delta_db,
):
    """``polish_delta_db`` is INSTRUMENTATION, so it needs a value assertion.

    The invariant's precondition — that the anchor's base came from the same
    band-average solve the give-back is calibrated to — is not enforced in code.
    It is *published*, and both ``intervention.py``'s invariant and the design
    doc promise it is "published on every round so the precondition is observed
    rather than assumed". A field that silently reported ``0.0`` would restore
    exactly the invisibility this whole change removes, and would do it while
    every existing test stayed green.

    **Presence assertions cannot catch that**, and on the ordinary fixture
    neither can a literal: the honest value there IS ``0.0`` (no polish), so
    pinning the literal would pass against a hard-coded zero too. This drives a
    base that DIFFERS from the band-average solve by a known amount and asserts
    the journal reports that amount — the mutation that forces the field to
    ``0.0`` fails here and nowhere else.

    Asserted as the RELATION rather than a magic number, because the
    band-average solve is the fixture's to produce, not this test's to restate:
    ``polish_delta_db == raw_trim_db − band_average_trim_db``, per role.

    **This fixture's own base is already 2.688 dB off its band-average solve**,
    which is worth knowing rather than normalising away: it means the banked
    2026-08-10 incident violated the precondition, and 2.688 dB is exactly the
    residual realized level error that incident's anchor still carries in
    ``test_crossover_v2_incident_replay``. The δ→δ pass-through, on real banked
    data rather than a synthetic pair. So the assertion below is on the SHIFT
    between an unpolished and a polished run, not on an absolute value — a
    hard-coded ``0.0`` moves the shift to zero and fails, while the fixture's
    pre-existing offset is left as the fact it is.
    """
    request = _planner_request(_sections_at(SELECTED_FC_HZ))
    tweeter = request.tweeter.role

    baseline = _giveback_record(iv.plan_linearization(request))
    polished_trims = dict(request.raw_trim_db)
    polished_trims[tweeter] = polished_trims[tweeter] + injected_delta_db
    polished = _giveback_record(
        iv.plan_linearization(replace(request, raw_trim_db=polished_trims))
    )

    # The field tracks the base it was handed, per role, in BOTH runs.
    for fields in (baseline, polished):
        for role in (request.woofer.role, tweeter):
            assert fields["polish_delta_db"][role] == pytest.approx(
                fields["raw_trim_db"][role] - fields["band_average_trim_db"][role],
                abs=1e-3,
            ), f"{role}: polish delta does not match raw − band-average"

    # Moving the base by δ moves the reported delta by exactly δ — the
    # assertion a constant-valued field cannot survive.
    shift = polished["polish_delta_db"][tweeter] - baseline["polish_delta_db"][tweeter]
    assert shift == pytest.approx(injected_delta_db, abs=1e-3)
    # The untouched role does not move with it.
    assert polished["polish_delta_db"][request.woofer.role] == pytest.approx(
        baseline["polish_delta_db"][request.woofer.role], abs=1e-9
    )


# --------------------------------------------------------------------------- #
# class (b) — the trim policy, isolated
# --------------------------------------------------------------------------- #


def test_the_replay_cannot_emit_the_incident_trim_through_a_rejected_path(monkeypatch):
    """#2291's literal acceptance criterion, as one assertion.

    "The exact jts3 replay cannot emit the −13.013 dB tweeter trim through a
    'trim_rejected' path." It emits −6.401 dB with a strategy that says the
    scan was rejected and the anchor committed.

    That anchored number has moved as the anchor's own definition did, most
    recently −5.149 → −6.401 when the give-back moved into the band the trim is
    read in — 1.252 dB CLOSER to the −10.885 this session's own raw solve asked
    for. The criterion it serves has not moved: whatever the anchor is, it is
    not the rejected scan's pair.
    """
    _install_stubs(monkeypatch, scan_delta_db=INCIDENT_SCAN_DELTA_DB)
    plan = _pure(_sections_at(SELECTED_FC_HZ))

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
    program = conductor.program_for_phase(flow.PHASE_MEASURE)
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


def test_the_journal_port_receives_every_record_in_plan_order(monkeypatch):
    """The disclosure port streams exactly what the plan carries, in order.

    The port exists so a fit that raises part-way still discloses the lines it
    reached; that is only true if a record reaches the port when it is
    produced rather than at the end.
    """
    _install_stubs(monkeypatch, scan_delta_db=INCIDENT_SCAN_DELTA_DB)
    conductor = _conductor()
    analysis = _analysis(CANDIDATE_FIT["program_id"])
    program = conductor.program_for_phase(flow.PHASE_MEASURE)
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

    Not a message check: the refusal is a *type*, so a caller classifies on the
    exception class rather than on its wording. (The classifier of record was
    the Phase-1 ``planner_facade``, deleted in #2291 Phase 5c-iii; the property
    this test pins is the contract's, not that consumer's, which is why it
    outlived it.)
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

    plan = _pure(_sections_at(SELECTED_FC_HZ))

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
    plan = _pure(sections)

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
# the margin/tolerance coupling the fallback's safety promise rests on
# --------------------------------------------------------------------------- #


def _request(**overrides):
    """One minimal, valid request — the fixture the coupling tests vary."""

    evidence = iv.DriverEvidence(
        role="woofer", response=object(), excited_band_hz=(100.0, 2000.0)
    )
    kwargs = dict(
        context=CandidateAcousticContext.from_sections(_sections_at(SELECTED_FC_HZ)),
        woofer=evidence,
        tweeter=replace(evidence, role="tweeter"),
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
    kwargs.update(overrides)
    return iv.LinearizationRequest(**kwargs)


def test_the_shipped_margin_satisfies_the_coupling_its_safety_rests_on():
    """The two constants, in two modules, pinned against each other.

    ``decide_trim``'s "a badly-levelled anchor produces a loud refusal, not a
    loud speaker" holds only while the margin is at least twice the
    realized-level tolerance. Today that is 6.0 against 2 × 3.0 — exactly on
    the edge — so a one-line retune of either constant, in either module,
    silently voids the promise. Same shape as
    ``test_realized_level_match_tolerance_clears_the_measured_frame_noise``,
    which pins that tolerance against ITS own floor argument.
    """
    assert iv.LINEARIZATION_TRIM_SANITY_MARGIN_DB >= (
        iv.MIN_TRIM_SANITY_MARGIN_RATIO * REALIZED_LEVEL_MATCH_TOLERANCE_DB
    )


@pytest.mark.parametrize(
    "margin_db, accepted",
    [
        (6.0, True),  # the shipped value
        (12.0, True),  # comfortably above
        (6.0000001, True),
        (2 * REALIZED_LEVEL_MATCH_TOLERANCE_DB, True),  # exactly on the floor
        (5.999999, False),  # a hair under
        (4.0, False),  # the panel's measured +4.05 dB-hotter retune
        (0.0, False),
        (-1.0, False),
    ],
)
def test_a_margin_below_twice_the_tolerance_is_refused(margin_db, accepted):
    """Both directions, including the boundary, because the floor is inclusive.

    At a 4.0 dB margin the hearing-safety lens measured a tweeter landing
    +4.05 dB hotter than legacy would have shipped, THROUGH a matched
    accountability gate — the fallback commits an anchor louder than the scan
    by more than the gate can see. Refusing at construction is what keeps that
    unreachable.
    """
    if accepted:
        assert _request(trim_sanity_margin_db=margin_db).trim_sanity_margin_db == (
            pytest.approx(margin_db)
        )
        return
    with pytest.raises(iv.PlannerInputError, match="realized-level tolerance"):
        _request(trim_sanity_margin_db=margin_db)


@pytest.mark.parametrize("margin_db", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_margin_is_refused_before_it_can_disable_the_guard(margin_db):
    """NaN is the silent one, and why ``isfinite`` is checked separately.

    Every comparison against NaN is False, so ``drift > margin`` is False for
    every drift: the guard does not misfire, it stops existing, and every scan
    commits as though the sanity check were absent. A plain ``margin < floor``
    test would not catch it — NaN fails that comparison too.
    """
    with pytest.raises(iv.PlannerInputError, match="finite"):
        _request(trim_sanity_margin_db=margin_db)
    # The mechanism, demonstrated rather than described: this is what the
    # refusal prevents.
    assert not (99.0 > float("nan"))


def test_an_unregistered_mic_tier_is_a_named_refusal_not_a_bare_key_error():
    """A caller error that reads as one, instead of as malformed planner output."""

    own = replace(_response("woofer"))
    with pytest.raises(iv.PlannerInputError, match="unknown mic tier"):
        iv.compose_sigma_db(
            own, own, tier="studio", valid_band_hz=(150.0, 4000.0)
        )


# --------------------------------------------------------------------------- #
# the disclosure port is genuinely write-only
# --------------------------------------------------------------------------- #


def test_a_raising_journal_consumer_cannot_abort_the_plan(monkeypatch):
    """One bad log line must not cost a household a candidate.

    Legacy called ``log_event`` inline and inherited logging's never-raise
    posture, so the extraction had to be at least as tolerant. In a
    six-candidate sweep an aborting port would not merely lose a line — it
    would silently narrow the comparison the household is shown.
    """
    _install_stubs(monkeypatch, scan_delta_db=INCIDENT_SCAN_DELTA_DB)
    sections = _sections_at(SELECTED_FC_HZ)
    request = _planner_request(sections)

    def hostile(record):
        raise RuntimeError(f"formatter blew up on {record.event}")

    plan = iv.plan_linearization(request, journal=hostile)

    # The plan is complete...
    reference = iv.plan_linearization(request)
    assert dict(plan.role_attenuations_db) == dict(reference.role_attenuations_db)
    assert [r.event for r in plan.journal] == [r.event for r in reference.journal]
    # ...and the loss is disclosed rather than swallowed.
    assert len(plan.journal_dropped) == len(plan.journal)
    assert all("RuntimeError: formatter blew up" in d for d in plan.journal_dropped)
    assert plan.journal_dropped[0].startswith(
        "correction.crossover_v2_linearization_fit_band:"
    )
    assert reference.journal_dropped == ()


def test_a_mutating_journal_consumer_cannot_rewrite_the_plan(monkeypatch):
    """The port is write-only in the other direction too.

    ``JournalRecord`` used to hold whatever container the planner built it
    from, so a consumer that popped a key — an ordinary thing for a formatter
    to do — changed what the returned plan reported. Same defect class the
    proposal contracts fixed with ``detached_json``; this was the missed site.
    """
    _install_stubs(monkeypatch, scan_delta_db=INCIDENT_SCAN_DELTA_DB)
    sections = _sections_at(SELECTED_FC_HZ)
    request = _planner_request(sections)

    def vandal(record):
        record.fields.clear()
        record.fields["injected"] = "tampered"

    plan = iv.plan_linearization(request, journal=vandal)
    reference = iv.plan_linearization(request)

    assert plan.journal_dropped == ()
    assert [dict(r.fields) for r in plan.journal] == [
        dict(r.fields) for r in reference.journal
    ]
    assert all("injected" not in r.fields for r in plan.journal)


def test_cloud_evidence_without_a_boost_bound_fails_closed():
    """No ``getattr`` default: a missing boost bound raises, never reads empty.

    Empty means "nothing contradicted a boost" and grants the lift stage the
    full band, so defaulting to it on an absent attribute would fail OPEN in
    the one direction this bound must never fail in.
    """
    incomplete = types.SimpleNamespace(
        excluded_bands_hz=(), band_spread=(), n_positions=0
    )
    with pytest.raises(AttributeError, match="boost_excluded_bands_hz"):
        iv.CloudFitTerms.from_evidence(incomplete)


def test_a_journal_record_detaches_the_fields_it_was_handed():
    """The snapshot itself, at the type rather than through a plan.

    Both directions have to hold. The SOURCE side (a caller mutating the dict
    it passed in) is what ``detached_json`` closes; the CONSUMER side (a reader
    mutating what the record hands back, at any depth) is what copy-on-read
    closes. Detaching alone leaves the record's own dict live, which is the
    hole the panel found — so the nested edits below are the load-bearing half.
    """
    band = [100.0, 200.0]
    fields = {"role": "tweeter", "band_hz": band, "nested": {"gain_db": -2.0}}
    record = iv.JournalRecord("correction.test", fields)

    # Source side.
    fields["role"] = "woofer"
    band.append(300.0)
    fields["nested"]["gain_db"] = -40.0

    assert record.fields["role"] == "tweeter"
    assert list(record.fields["band_hz"]) == [100.0, 200.0]
    assert record.fields["nested"]["gain_db"] == -2.0

    # Consumer side, top level and nested.
    handed = record.fields
    handed.clear()
    handed["injected"] = "tampered"
    record.fields["nested"]["gain_db"] = -99.0
    record.fields["band_hz"].append(9999.0)

    assert record.fields["role"] == "tweeter"
    assert "injected" not in record.fields
    assert record.fields["nested"]["gain_db"] == -2.0
    assert list(record.fields["band_hz"]) == [100.0, 200.0]


# --------------------------------------------------------------------------- #
# the seventh Fc site's disclosure, relocated to its detection point
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", ["empty", "absent"])
def test_a_role_with_no_crossover_section_is_named_in_the_journal(monkeypatch, shape):
    """Legacy's ``no_crossover`` WARNING, emitted where the condition is seen.

    ``_branch_crossover_sections`` is legacy's SEVENTH reader of the session
    corner and the disclosure's only production home; it loses its last caller
    at the 2b cutover. The planner emits the same event, with the CANDIDATE's
    corner — which is the point: legacy named the session's.

    Both shapes are exercised because production only produces one of them:
    ``_candidate_sections`` OMITS a role with no region rather than mapping
    it to ``()``. Legacy's ``sections_by_role(...).get(role, ())`` collapses the
    two, and ``sections_for`` must collapse them the same way — otherwise the
    disclosure would fire in tests and not in the field.
    """
    _install_stubs(monkeypatch, scan_delta_db=0.0)
    both = _sections_at(SELECTED_FC_HZ)
    # The woofer runs full range; the tweeter still carries the corner, so the
    # context is buildable and its invariant holds.
    one_sided: dict[str, Any] = {"tweeter": both["tweeter"]}
    if shape == "empty":
        one_sided["woofer"] = ()

    plan = _pure(one_sided)

    named = [
        r
        for r in plan.journal
        if r.event == "correction.crossover_v2_linearization_no_crossover"
    ]
    assert [r.fields["role"] for r in named] == ["woofer"]
    assert named[0].fields["fc_hz"] == pytest.approx(SELECTED_FC_HZ, abs=1e-3)
    assert named[0].fields["fc_hz"] != pytest.approx(CONFIGURED_FC_HZ)
    assert named[0].level == logging.WARNING
    # It is a disclosure, not a refusal: the plan is still produced, and the
    # role that HAS a crossover is unaffected.
    assert plan.role_attenuations_db
    assert plan.radiating_band_hz["woofer"] == radiating_band_hz(())


def test_both_roles_carrying_sections_names_nobody(monkeypatch):
    """The positive control: the disclosure is conditional, not unconditional."""

    _install_stubs(monkeypatch, scan_delta_db=0.0)
    plan = _pure(_sections_at(SELECTED_FC_HZ))
    assert not [
        r
        for r in plan.journal
        if r.event == "correction.crossover_v2_linearization_no_crossover"
    ]


# --------------------------------------------------------------------------- #
# the emitted trims are cut-only, tested on the planner's own output
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("giveback_db", [0.0, 5.0, 20.0, 60.0])
def test_no_committed_trim_is_ever_positive_however_large_the_giveback(
    monkeypatch, giveback_db
):
    """The hearing-safety invariant, on a fixture that reaches it.

    A branch whose own cuts give back more than its raw attenuation lands
    POSITIVE before ``normalize_shift_db`` subtracts the common shift — a boost
    the emitter refuses and the hardware must never see. This fixture does land
    there on its own: the anchor's unnormalized woofer is ``+3.916`` dB and the
    shared shift subtracts exactly that, so the clamp is genuinely exercised
    below.

    **The parametrization drives the give-back at the ESTIMATOR, and it has to.**
    It was originally written to raise ``LinearizationFit.correction_giveback_db``.
    The 2026-08-19 band fix moved the anchor's give-back onto
    ``solve_branch_trims`` over ``branch_level_bands_hz``, and that stub then
    stopped reaching the anchor at all: measured before this was repaired, all
    four values handed ``anchor_trims`` the identical
    ``{'woofer': 3.916, 'tweeter': 8.400}`` and committed the identical pair
    (``tweeter`` −6.400575020419777). One case four times, wearing the name of a
    ladder.

    It now injects where the anchor actually reads, the way the sibling
    ``test_a_non_finite_giveback_is_refused_before_the_anchor_uses_it`` does:
    the POST-correction level read is lowered by ``giveback_db``, so the
    measured give-back — ``level_pre − level_post`` — rises by exactly that on
    both roles and the unnormalized anchor is pushed arbitrarily far positive.
    At 60 dB it is nowhere near legal, which is the point: the clamp, not the
    fixture, is what has to hold.

    **Why the COMMITTED pair is the wrong thing to watch, and the anchor's
    inputs are the right thing.** The injection raises both roles' give-back by
    the same amount, and the shared shift subtracts the same amount from both —
    so the committed pair is *identical* at every rung (tweeter
    −6.400575020419777 throughout). That is the normalize doing exactly its job:
    it preserves relative leveling and spends only ledger. Asserting on the
    committed pair would therefore look green whether or not the ladder ran,
    which is precisely how this test was inert before. The assertions below
    watch the anchor's own inputs and the shift, where the ladder is visible.

    **The call-count assertion is not decoration.** This test went inert once
    because production stopped calling what it stubbed, silently. Pinning that
    the planner made exactly the two estimator reads this injection assumes
    means the next such move fails here loudly instead of quietly returning to
    one case four times.
    """
    _install_stubs(monkeypatch, scan_delta_db=0.0)

    real_solve = iv.solve_branch_trims
    reads = {"n": 0}

    def laddered(*args, **kwargs):
        trim_w, trim_t, level_w, level_t = real_solve(*args, **kwargs)
        reads["n"] += 1
        # Call 1 is the PRE-correction read, call 2 the POST-correction one.
        # Lowering only the second raises `pre - post` by exactly `giveback_db`.
        if reads["n"] == 2:
            return trim_w, trim_t, level_w - giveback_db, level_t - giveback_db
        return trim_w, trim_t, level_w, level_t

    monkeypatch.setattr(iv, "solve_branch_trims", laddered)

    # Capture what the anchor was actually handed, so the ladder's effect is
    # asserted at the seam it acts on rather than inferred from the committed
    # pair (which, correctly, does not move — see below).
    anchor_inputs: dict[str, float] = {}
    real_anchor = iv.anchor_trims

    def spy(*, roles, anchor_base_db, giveback_db):
        anchored, shift = real_anchor(
            roles=roles, anchor_base_db=anchor_base_db, giveback_db=giveback_db,
        )
        anchor_inputs.update(
            {
                f"unnormalized_{role}": float(
                    anchor_base_db.get(role, 0.0) + giveback_db.get(role, 0.0)
                )
                for role in roles
            },
            shift=float(shift),
        )
        return anchored, shift

    monkeypatch.setattr(iv, "anchor_trims", spy)
    plan = _pure(_sections_at(SELECTED_FC_HZ))

    assert reads["n"] == 2, (
        f"the anchor made {reads['n']} estimator reads, not 2 — this injection "
        "no longer lands where the give-back is measured, and the sweep below "
        "is inert again"
    )
    # The injection reached the anchor and the clamp scaled with it. Measured
    # across the ladder: the unnormalized TWEETER anchor runs -2.485 dB (needing
    # no clamp) up to +57.515 dB (needing a very large one), and the shared
    # shift tracks at 3.916 + giveback_db.
    assert anchor_inputs["shift"] >= giveback_db, (
        f"shift {anchor_inputs['shift']:.3f} did not absorb an injected "
        f"give-back of {giveback_db:.1f} dB — the ladder is not reaching the "
        "clamp"
    )
    # At the top of the ladder the TWEETER's own unnormalized anchor is far
    # positive, which is the give-back-exceeds-attenuation case this test is
    # named for. Pinned so the ladder cannot quietly shrink to values the
    # fixture would have cleared anyway.
    if giveback_db >= 20.0:
        assert anchor_inputs["unnormalized_tweeter"] > 10.0, (
            "the ladder no longer pushes the tweeter's unnormalized anchor "
            "well positive, so the clamp is not being exercised by the sweep"
        )
    assert plan.role_attenuations_db
    for role, trim_db in plan.role_attenuations_db.items():
        assert trim_db <= 0.0, f"{role} committed a boost of {trim_db:+.3f} dB"
    for pair in (plan.trim.anchored_db, plan.trim.resolved_db):
        assert all(v <= 0.0 for v in pair.values())


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
    # ``committed_side`` is what the journal's ``committed`` field reads, so it
    # is pinned over BOTH outcomes here rather than only where the record is
    # emitted — the record only ever carries the anchored case, which is why a
    # literal there was indistinguishable from the derivation under mutation.
    assert decision.committed_side == (
        "anchored" if decision.committed_db == dict(anchored) else "resolved"
    )
    assert decision.committed_side in ("anchored", "resolved")
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


# --------------------------------------------------------------------------- #
# every door into the anchor's arithmetic, pinned
# --------------------------------------------------------------------------- #
#
# **Why these exist and why they are separate tests.** ``anchor_trims.place``
# computes ``base + giveback`` and clamps it with
# ``shift = max(0.0, max(unnormalized.values()))``. That clamp is the
# hearing-safety invariant — no committed trim may be positive — and it is a
# ``max``, so every comparison against NaN is False: a NaN term does not make
# the clamp misfire, it makes the clamp STOP EXISTING, and the non-finite trim
# goes to the emitter. Since #2609 made the raw measured trim the anchor's
# base, the complete set of doors is ``raw_trim_db`` and
# ``trim_band_average_db`` (guarded in ``LinearizationRequest.__post_init__``)
# plus the anchor's give-back term (guarded at the anchor's own call site).
# That third door moved bands on 2026-08-19: it is the LEVEL-BAND give-back
# ``solve_branch_trims`` measures over ``branch_level_bands_hz``, not
# ``LinearizationFit.correction_giveback_db``, which no longer reaches the
# anchor at all.
#
# The safety delta caught these guards UNPINNED: deleting the whole
# trim-finiteness loop left 432 tests green. The three ``match="finite"``
# cases already in this file cover ``trim_sanity_margin_db`` ONLY, so a grep
# for the guard read "covered" when two thirds of it was not — the
# half-guarded-site trap. Each case below was verified to go red under that
# same deletion.


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["raw_trim_db", "trim_band_average_db"])
def test_a_non_finite_request_trim_is_refused_at_the_door(field, bad):
    """The two trim mappings the anchor's base can come from.

    Refused at construction rather than at use, because by the time a NaN
    reaches ``place`` the clamp that would have caught a too-loud value is
    already inert.
    """
    with pytest.raises(iv.PlannerInputError, match="finite"):
        _request(**{field: {"woofer": 0.0, "tweeter": bad}})
    # The mechanism, demonstrated rather than described: this is what the
    # refusal prevents — the clamp silently passing a non-finite trim through.
    assert not (max(0.0, float("nan")) > 0.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_giveback_is_refused_before_the_anchor_uses_it(
    bad, monkeypatch,
):
    """The anchor's OTHER term, guarded at its own call site.

    The give-back is measured rather than handed in, so it cannot be guarded in
    ``__post_init__`` with the trims — the check lives where the value enters
    the anchor. Driven here by making the measurement return the bad value,
    which is the only way to reach that door.

    **The term this guards moved bands, and the guard moved with it.** It used
    to read ``LinearizationFit.correction_giveback_db`` (the fit's core-band
    number) and is now the level-band give-back measured through
    ``solve_branch_trims`` over the bands the verdict grades — so the injection
    site is the estimator, not the fit. The property under test is unchanged:
    a non-finite give-back must be refused BEFORE ``anchor_trims``, because the
    non-positive normalize is a ``max`` and a ``max`` against NaN is inert
    (demonstrated in the sibling test above).
    """
    real_solve = iv.solve_branch_trims

    def _bad_level(*args, **kwargs):
        trim_w, trim_t, _level_w, _level_t = real_solve(*args, **kwargs)
        return trim_w, trim_t, 0.0, bad

    monkeypatch.setattr(iv, "solve_branch_trims", _bad_level)
    with pytest.raises(iv.PlannerInputError, match="finite"):
        _pure(_sections_at(SELECTED_FC_HZ))
