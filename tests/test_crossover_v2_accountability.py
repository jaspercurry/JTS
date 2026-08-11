# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 5a-v: the accountability gate decides; the conductor acts.

The gate's *verdicts* are already covered, and better, elsewhere: the
dual run in this PR compares every gate journal line — content AND order —
against the pre-extraction conductor across five suites (281 lines, byte
identical, against a measured zero noise floor), and
``test_crossover_v2_conductor.py`` grades what each refusal does to a session.

What those cannot cover is what became NEW when the gate stopped acting: it
now hands back a decision the caller replays, so the properties that used to
be guaranteed by the order of statements in one method have to be guaranteed
by the shape of a value instead. These pin exactly those, and nothing else.
"""

from __future__ import annotations

import logging

import pytest

from jasper.active_speaker.crossover_v2 import accountability
from jasper.active_speaker.crossover_v2.candidates import LinearizationState
from jasper.active_speaker.flat_spec import BandResult, FlatSpecReport
from jasper.audio_measurement.program_analysis import RealizedLevelMatch

TOLERANCE_DB = 3.0
IMPROVEMENT_DB = 0.5
REASON_DISAGREE = "driver_levels_disagree"
REASON_NOT_BETTER = "correction_not_an_improvement"


class _Frame:
    """The planner's level frame, as the gate reads it — three attributes."""

    def __init__(self, offsets):
        self.system_level_db = 0.823
        self.reference_role = "woofer"
        self.offset_db = offsets


def _report(passed, *, rms_db=1.0):
    """A REAL :class:`FlatSpecReport`, not a stub.

    The gate hands whatever the evaluator returned to
    ``spec_convergence_residual``, so a stub carrying only ``overall_passed``
    would make the ledger's ``before_rms_db``/``after_rms_db`` unreachable —
    a suite green over a code path production takes on every session. Only the
    evaluator ITSELF is injected, which is the system boundary.
    """
    return FlatSpecReport(
        reference_db=0.0,
        bands=(
            BandResult(
                f_lo_hz=200.0, f_hi_hz=2000.0, tolerance_db=3.0,
                max_deviation_db=rms_db, max_deviation_hz=1000.0,
                rms_deviation_db=rms_db, n_bins=100, n_excluded=0,
                evaluable=True, passed=passed,
            ),
        ),
        overall_passed=passed,
        excluded_intervals=(),
        best_effort_above_hz=10000.0,
        smoothing_fraction=6,
    )


def _match(*, matched, difference_db=0.2):
    return RealizedLevelMatch(
        level_w_db=0.0, level_t_db=difference_db, difference_db=difference_db,
        tolerance_db=TOLERANCE_DB, matched=matched,
        woofer_band_hz=(150.0, 1255.8), tweeter_band_hz=(2020.0, 9000.0),
    )


def _state(*, disagreement_db=0.0, matched=True, linearized=("f", "m")):
    return LinearizationState(
        outcome="fitted",
        level_frame=_Frame({"woofer": 0.0, "tweeter": -0.674}),
        level_frame_disagreement_db=disagreement_db,
        # Tuples on purpose — see the GateRecord fidelity pin below.
        level_frame_cores={
            "woofer": {"level_db": 0.823, "band_hz": (150.0, 1255.8),
                       "radiating_band_hz": (0.0, 1282.3)},
        },
        level_frame_trims={"woofer": 0.1},
        linearized_predicted_sum=linearized,
        realized_level_match=_match(matched=matched),
    )


def _assess(state, **over):
    kwargs = dict(
        predicted_sum=("f", "m"),
        raw_predicted_sum=("f", "raw"),
        state=state,
        grade_prediction=lambda _sum: _report(True),
        level_frame_tolerance_db=TOLERANCE_DB,
        material_improvement_db=IMPROVEMENT_DB,
        reason_levels_disagree=REASON_DISAGREE,
        reason_not_an_improvement=REASON_NOT_BETTER,
    )
    kwargs.update(over)
    return accountability.assess_accountability(**kwargs)


# --------------------------------------------------------------------------- #
# 1. a refusal must not touch a stash it never reached
# --------------------------------------------------------------------------- #
#
# The conductor writes the stash BEFORE it emits the journal, which is not the
# order the replaced method used. That reordering is unobservable for exactly
# one reason — a decision only carries a stash when it got PAST both refusal
# arms — and "unobservable for a reason" is an argument, not a guard. These are
# the guard.


@pytest.mark.parametrize(
    ("state", "why"),
    [
        (_state(disagreement_db=20.87, matched=False),
         "the frame gate refuses before item 2 grades anything"),
        (_state(disagreement_db=0.0, matched=False),
         "the realized-level gate refuses before item 2 grades anything"),
    ],
    ids=["frame_refusal", "realized_level_refusal"],
)
def test_a_refusal_arm_never_carries_a_stash(state, why):
    decision = _assess(state)

    assert decision.refusal_reason == REASON_DISAGREE, why
    assert decision.spec_report_written is False, why
    assert decision.spec_report is None, why


def test_a_graded_refusal_DOES_carry_its_stash():
    """Item 2 stashes and then refuses — the stash survives the refusal.

    The other direction of the pin above, and the one that makes it a
    discrimination rather than a blanket "refusals write nothing": the
    prediction the household is refused ON is the prediction the host
    persists, so a reader can see what was graded.
    """
    decision = _assess(
        _state(), grade_prediction=lambda _sum: _report(False, rms_db=2.0),
    )

    assert decision.refusal_reason == REASON_NOT_BETTER
    assert decision.spec_report_written is True
    assert decision.spec_report is not None
    assert decision.spec_report["comparison"]["reason"] == REASON_NOT_BETTER


def test_an_ungradeable_prediction_clears_the_stash_rather_than_leaving_it():
    """``spec_report is None`` with ``written`` True is a third state.

    Collapsing the two fields would make this indistinguishable from a frame
    refusal, and the conductor would leave a previous session's report in
    place where the gate meant to clear it.
    """
    decision = _assess(_state(), grade_prediction=lambda _sum: None)

    assert decision.spec_report is None
    assert decision.spec_report_written is True
    assert decision.refusal_reason is None


# --------------------------------------------------------------------------- #
# 2. the properties that only a pure gate has
# --------------------------------------------------------------------------- #


def test_asked_twice_it_answers_the_same_and_writes_nothing_between():
    """What makes a speculative build safe to drop and refit.

    The eager path runs this gate on a build a retake may moot, and then again
    on the confirm. A gate that accumulated state across calls would give the
    second run a different answer — which is the ``_last_*`` failure mode
    #2291 exists to close, one layer up.
    """
    state = _state(disagreement_db=5.799, matched=True)

    first = _assess(state)
    second = _assess(state)

    assert [(r.event, r.level, r.fields) for r in first.journal] == [
        (r.event, r.level, r.fields) for r in second.journal
    ]
    assert first.refusal_reason == second.refusal_reason
    assert first.finding == second.finding


def test_the_baseline_is_graded_only_when_the_verdict_turns_on_it():
    """At most two grader calls, and the second only where it is needed.

    The caller may not pre-compute both reports for this reason: grading the
    baseline unconditionally would run the evaluator — and emit its own
    diagnostics — on paths that today never reach it.
    """
    calls = []

    def grade(summed):
        calls.append(summed)
        return _report(True)

    _assess(_state(), grade_prediction=grade)
    assert calls == [("f", "m")], "an in-spec prediction needs no baseline"

    calls.clear()

    def grade_failing(summed):
        calls.append(summed)
        return _report(False, rms_db=2.0)

    _assess(_state(), grade_prediction=grade_failing)
    assert calls == [("f", "m"), ("f", "raw")], "a failing one needs both"


def test_a_refusal_never_banks_a_finding():
    """A banked finding describes a proposal; a refusal leaves none.

    Reachable: the frame can disagree, bank, and then item 2 can still refuse.
    """
    decision = _assess(
        _state(disagreement_db=5.799, matched=True),
        grade_prediction=lambda _sum: _report(False, rms_db=2.0),
    )

    assert decision.refusal_reason == REASON_NOT_BETTER
    assert decision.finding is None


def test_the_journal_is_in_emission_order_frame_then_ledger():
    decision = _assess(_state(disagreement_db=5.799, matched=True))

    assert [r.event for r in decision.journal] == [
        accountability.EVENT_LEVEL_FRAME_FINDING,
        accountability.EVENT_PREDICTION_GATE,
    ]
    assert decision.journal[0].level == logging.WARNING


def test_the_trims_only_lane_abstains_rather_than_grading_a_thing_against_itself():
    decision = _assess(_state(linearized=None))

    assert decision.refusal_reason is None
    assert [r.fields["reason"] for r in decision.journal] == [
        accountability.LEDGER_NO_LINEARIZATION
    ]


# --------------------------------------------------------------------------- #
# 3. the record type the dual run earned
# --------------------------------------------------------------------------- #


def test_the_gate_payload_keeps_containers_the_planner_record_would_flatten():
    """``GateRecord`` holds the payload as built; ``JournalRecord`` normalizes.

    Not a style preference. ``core_level_db`` carries per-role ``band_hz``
    pairs as TUPLES and ``log_event`` renders them with ``str``, so the shipped
    line reads ``'band_hz': (150.0, 1255.8)``. Routing the gate through the
    planner's record — which detaches through ``detached_json``, and a
    ``Sequence`` comes back a ``list`` — silently rewrote that to
    ``[150.0, 1255.8]`` in a field-diagnosis surface. The 5a-v dual run caught
    it; this keeps it caught.
    """
    from jasper.active_speaker.crossover_v2.intervention import JournalRecord

    decision = _assess(_state(disagreement_db=5.799, matched=True))
    payload = decision.journal[0].fields

    band = payload["core_level_db"]["woofer"]["band_hz"]
    assert isinstance(band, tuple), band
    assert f"{band}" == "(150.0, 1255.8)"

    # The positive control: the same payload through the planner's record is
    # where the flattening comes from, so this test fails for a real reason
    # rather than because tuples happen to survive everything.
    flattened = JournalRecord("e", payload).fields
    assert isinstance(flattened["core_level_db"]["woofer"]["band_hz"], list)
