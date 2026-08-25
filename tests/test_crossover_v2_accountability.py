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

import dataclasses
import logging

from jasper.active_speaker.crossover_v2 import accountability, intervention
from jasper.active_speaker.crossover_v2.candidates import LinearizationState
from jasper.active_speaker.flat_spec import BandResult, FlatSpecReport
from jasper.audio_measurement.program_analysis import RealizedLevelMatch

TOLERANCE_DB = 3.0
IMPROVEMENT_DB = 0.5


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


def _consistency(*, suspect, worst_delta_db=5.799):
    """A :class:`LevelConsistency` verdict — suspect or agreed.

    Not a re-derivation of :func:`~.intervention.check_level_consistency` —
    these tests exercise how ``assess_accountability`` handles the verdict,
    not how the verdict itself is computed, so the fixture states its shape
    directly. ``None`` — "the session produced no verdict at all" — is a third
    state and is passed to :func:`_state` explicitly rather than spelled as
    ``suspect=False``, which the ledger's tri-state flag distinguishes.
    """
    return intervention.LevelConsistency(
        suspect=suspect,
        reason=intervention.LEVEL_ESTIMATOR_SUSPECT_REASON if suspect else "",
        tolerance_db=TOLERANCE_DB,
        worst_delta_db=worst_delta_db if suspect else 0.4,
        estimator_delta_db={"woofer": worst_delta_db if suspect else 0.4},
    )


_UNSET = object()


def _state(
    *, suspect=False, matched=True, linearized=("f", "m"), consistency=_UNSET,
    difference_db=_UNSET, polish_delta_db=None,
):
    return LinearizationState(
        outcome="fitted",
        # Tuples on purpose — see the GateRecord fidelity pin below.
        core_level_evidence={
            "woofer": {"level_db": 0.823, "band_hz": (150.0, 1255.8),
                       "radiating_band_hz": (0.0, 1282.3)},
        },
        trim_band_estimate_db={"woofer": 0.1},
        polish_delta_db=dict(polish_delta_db or {}),
        level_consistency=(
            _consistency(suspect=suspect) if consistency is _UNSET
            else consistency
        ),
        linearized_predicted_sum=linearized,
        realized_level_match=_match(
            matched=matched,
            # A mismatched pair defaults to a mismatch worth the name. The
            # 0.2 dB default belongs to the MATCHED case and would make an
            # unmatched fixture self-contradictory.
            **({} if difference_db is _UNSET else {"difference_db": difference_db}),
        ) if matched or difference_db is not _UNSET
        else _match(matched=False, difference_db=9.0),
    )


def _assess(state, **over):
    kwargs = dict(
        predicted_sum=("f", "m"),
        raw_predicted_sum=("f", "raw"),
        state=state,
        grade_prediction=lambda _sum: _report(True),
        material_improvement_db=IMPROVEMENT_DB,
    )
    kwargs.update(over)
    return accountability.assess_accountability(**kwargs)


def _one(decision, event):
    """The single journal line for ``event`` — asserting there IS exactly one.

    A gate that emitted a line twice, or not at all, would otherwise pass a
    test that merely searched the journal.
    """
    lines = [record for record in decision.journal if record.event == event]
    assert len(lines) == 1, f"expected exactly one {event}, got {len(lines)}"
    return lines[0]


# --------------------------------------------------------------------------- #
# 1. no arm of this gate refuses any more
# --------------------------------------------------------------------------- #
#
# There were two refusal arms here. The single-datum-owner migration (#2609)
# deleted the level-consistency one: a suspect verdict banks a finding and
# proceeds. The realized-level demotion (doctrine deviation (i)) took the last
# one, for the same never-nanny reason plus a located cost of its own — see
# ``test_a_mislevelled_pair_discloses_and_the_round_proceeds``.
#
# The conductor writes the stash BEFORE it emits the journal, which is not the
# order the replaced method used. That used to be unobservable because no
# refusal arm reached a stash; now it is unobservable because no arm refuses.
# The ordering claim is asserted directly instead of through a refusal.


def test_a_mislevelled_pair_discloses_and_the_round_proceeds():
    """The last level refusal, burnt down — doctrine deviation (i).

    A committed pair whose realized inter-driver levels sit past the tolerance
    used to raise ``driver_levels_disagree`` at the confirm seam and leave the
    speaker alone. It was a QUALITY check naming no component-damage mechanism,
    so §4's closed list never covered it and §3's disclose-and-recommend rule
    always did.

    It also had a cost the estimator arm did not: the number it graded is the
    MEASURE ripple polish's trim excursion, and the polish was admitted out to
    6.0 dB while this gate refused past 3.0 — a dead band in which the session
    manufactured its own refusal. ``program_analysis`` closes that band by
    coupling the admission to this same tolerance.

    Four promises, each asserted. The round is not refused. The disclosure
    still carries every number the refusal carried. It names the polish, so a
    reader can tell whether the polish explains it. And item 2 still runs,
    which it never did behind a refusal.

    **Mutation guard.** Re-adding the early ``return`` fails the last
    assertion. The refusal itself cannot be restored without also restoring the
    field it was carried on, which
    ``test_the_decision_carries_no_refusal_field_to_set`` pins.
    """
    decision = _assess(_state(suspect=False, matched=False))

    line = _one(decision, accountability.EVENT_LEVEL_MATCH_FINDING)
    assert line.level == logging.WARNING
    assert line.fields["reason"] == intervention.REALIZED_LEVEL_SUSPECT_REASON
    assert line.fields["difference_db"] == 9.0
    assert line.fields["tolerance_db"] == TOLERANCE_DB
    assert line.fields["level_w_db"] == 0.0
    assert line.fields["level_t_db"] == 9.0
    # Item 2 ran, which is only reachable because item 1 stopped returning:
    # its ledger line is the last thing this gate emits.
    assert decision.journal[-1].event == accountability.EVENT_PREDICTION_GATE


def test_a_realized_disclosure_survives_a_fit_that_produced_no_core_bands():
    """The record is the DURABLE half of the disclosure, so it must not vanish
    on the one input the realized check does not need.

    ``level_frame_record`` takes the finding's band from the per-role CORE
    spans — the bands the fit's two medians were computed over. When the fit
    produced none, that used to drop the whole record and leave the journal
    line as the only trace. Harmless while the realized check REFUSED (the
    session had already stopped and said why); a real loss now that the round
    proceeds and the banked finding is what the household reads.

    The realized verdict carries its own two spans — the mirrored half-bands
    about Fc it read the levels on — so the record falls back to those. The
    estimator condition has no such fallback and needs none: with no core
    median for any role there is nothing for it to have disagreed about.
    """
    state = _state(suspect=False, matched=False)
    state = dataclasses.replace(state, core_level_evidence={})

    decision = _assess(state)

    assert decision.finding is not None
    assert decision.finding["reason"] == intervention.REALIZED_LEVEL_SUSPECT_REASON
    # The union of the realized check's own half-bands, outer hull.
    assert decision.finding["f_lo_hz"] == 150.0
    assert decision.finding["f_hi_hz"] == 9000.0
    assert decision.finding["realized_difference_db"] == 9.0
    # The journal line fires either way — it always did. This test is about the
    # DURABLE record, which is the half that used to go missing.
    _one(decision, accountability.EVENT_LEVEL_MATCH_FINDING)


def test_the_realized_band_fallback_does_not_describe_an_estimator_finding():
    """The fallback is guarded on the REASON, not on the bands being absent.

    A band has to be the span its own reason was measured over. An estimator
    disagreement with no per-role core spans has no band it can honestly
    state — the realized check's half-bands belong to a different
    instrument — so the record is still ``None`` there rather than borrowing
    them. The realized NUMBERS ride an estimator record freely; the BAND is
    the one field that cannot be lent.
    """
    state = _state(suspect=True, matched=False)
    state = dataclasses.replace(state, core_level_evidence={})

    decision = _assess(state)

    assert decision.finding is None
    # Both journal lines still fire: losing the record is not losing the
    # disclosure, only its durable half.
    _one(decision, accountability.EVENT_LEVEL_ESTIMATOR_FINDING)
    _one(decision, accountability.EVENT_LEVEL_MATCH_FINDING)


def test_the_decision_carries_no_refusal_field_to_set():
    """**The demotion's structural mutation guard.**

    Every "the round proceeds" assertion in this file used to be
    ``decision.refusal_reason is None``, which is a weak guard: it holds
    whether the field is dead or merely unset on that one path, and it kept a
    ``raise`` alive in ``crossover_v2_flow`` that nothing could reach. Both
    accountability refusals are gone — item 2's with the nanny burn-down
    (deviation (c)) and item 1's with the realized-level demotion (deviation
    (i)) — so the field went with its last writer, and the guard is now that
    there is nowhere to put a refusal back without a visible edit here.

    ``spec_report_written`` went in the same cut: it existed to tell "item 2
    ran and graded nothing" from "item 2 never ran", and deviation (i)'s
    deleted return was the only path that could produce the second.
    """
    names = {
        field.name
        for field in dataclasses.fields(accountability.AccountabilityDecision)
    }
    assert names == {"journal", "finding", "spec_report"}


def test_the_mislevelled_disclosure_names_the_polish_that_could_explain_it():
    """The attribution, and why it is worth carrying.

    With the admission coupled to this gate's tolerance, a polish can no longer
    push the realized error past the bar on its own — so a firing here means a
    NON-polish source of inter-driver mismatch, which is exactly the thing a
    disclosure exists to surface. The reader can only draw that conclusion if
    the round says what the polish did, so it rides both the journal line and
    the banked record.

    **Mutation guard.** Dropping ``polish_delta_db`` from either surface fails
    here; the two assertions are deliberately on different surfaces.
    """
    decision = _assess(_state(
        suspect=False, matched=False,
        polish_delta_db={"woofer": 0.0, "tweeter": 1.25},
    ))

    line = _one(decision, accountability.EVENT_LEVEL_MATCH_FINDING)
    assert line.fields["polish_delta_db"] == {"woofer": 0.0, "tweeter": 1.25}
    assert decision.finding is not None
    assert decision.finding["polish_delta_db_tweeter"] == 1.25
    assert decision.finding["reason"] == intervention.REALIZED_LEVEL_SUSPECT_REASON


def test_an_estimator_disagreement_still_owns_the_reason_when_both_fire():
    """One reason field, and the more specific diagnosis wins.

    Both checks grade the same disease and both can fire on one round. The
    record carries ONE ``reason``, so the precedence is a decision rather than
    an accident: the estimator disagreement is the narrower finding and keeps
    it, exactly as its gate runs first. Nothing is lost — both sets of numbers
    ride the record either way, which is what this asserts.
    """
    decision = _assess(_state(suspect=True, matched=False))

    assert decision.finding["reason"] == intervention.LEVEL_ESTIMATOR_SUSPECT_REASON
    # The realized numbers ride anyway, so the precedence costs no evidence.
    assert decision.finding["realized_difference_db"] == 9.0
    assert decision.finding["estimator_worst_delta_db"] is not None
    # Both journal lines fired, in emission order — the more specific
    # diagnosis first, which is the same precedence the reason field carries.
    # Asserted here because no arm returns early any more, so the ordering can
    # no longer be demonstrated by which line a refusal cut off.
    events = [record.event for record in decision.journal]
    assert events.index(accountability.EVENT_LEVEL_ESTIMATOR_FINDING) < events.index(
        accountability.EVENT_LEVEL_MATCH_FINDING
    )
    _one(decision, accountability.EVENT_LEVEL_ESTIMATOR_FINDING)
    _one(decision, accountability.EVENT_LEVEL_MATCH_FINDING)


def test_a_predicted_worse_correction_proceeds_and_discloses_its_numbers():
    """The nanny burn-down, at the gate — doctrine deviation (c).

    Item 2 used to refuse here under ``correction_not_an_improvement``. It was
    a forecast vetoing the measurement that would have settled the question,
    which the doctrine's authority model forbids: "Predictions and heuristics
    PROPOSE… They never veto an in-band experiment. Measurements DISPOSE."
    Tonight's shape exactly: the linearized model grades WORSE than its own
    pre-fit baseline, so ``improvement_db`` is negative against a bar it
    cannot meet.

    Three things are asserted, and each is the burn-down's own promise. The
    round is not refused. The stash a downstream reader persists carries the
    forecast in full — both residuals, the delta, and the bar it was judged
    against — so nothing the veto used to compute is lost. And the ledger
    names the verdict, so "the forecast said worse" and "the forecast was
    never run" stay distinguishable in the journal.

    **Mutation guard.** Restoring the veto needs the field it would be
    carried on, which ``test_the_decision_carries_no_refusal_field_to_set``
    pins as absent.
    """
    decision = _assess(
        _state(), grade_prediction=lambda _sum: _report(False, rms_db=2.0),
    )

    assert decision.spec_report is not None
    comparison = decision.spec_report["comparison"]
    assert comparison["reason"] == accountability.LEDGER_NOT_AN_IMPROVEMENT
    # Graded against itself, so both residuals are 2.0 and the delta is 0.0 —
    # under the 0.5 dB bar, which is what makes this the refusing shape.
    assert comparison["baseline_rms_db"] == 2.0
    assert comparison["selected_rms_db"] == 2.0
    assert comparison["improvement_db"] == 0.0
    assert comparison["required_db"] == IMPROVEMENT_DB
    ledger = decision.journal[-1]
    assert ledger.event == accountability.EVENT_PREDICTION_GATE
    assert ledger.level == logging.WARNING
    assert ledger.fields["reason"] == accountability.LEDGER_NOT_AN_IMPROVEMENT
    assert ledger.fields["improvement_db"] == 0.0
    assert ledger.fields["required_db"] == IMPROVEMENT_DB


def test_an_ungradeable_prediction_clears_the_stash_rather_than_leaving_it():
    """``spec_report is None`` means item 2 ran and graded nothing, so the
    conductor CLEARS its stash rather than leaving a previous session's report
    in place.

    This used to need a second field (``spec_report_written``) to tell "ran and
    produced nothing" from "never ran". The realized-level demotion deleted the
    only return that could produce the second, so ``None`` now has one meaning
    and the flag went with the path it described.
    """
    decision = _assess(_state(), grade_prediction=lambda _sum: None)

    assert decision.spec_report is None
    # Item 2 ran: it is the last line on the journal either way.
    assert decision.journal[-1].event == accountability.EVENT_PREDICTION_GATE


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
    state = _state(suspect=True, matched=True)

    first = _assess(state)
    second = _assess(state)

    assert [(r.event, r.level, r.fields) for r in first.journal] == [
        (r.event, r.level, r.fields) for r in second.journal
    ]
    assert first.spec_report == second.spec_report
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


def test_a_distrusted_forecast_rides_WITH_the_prediction_it_produced():
    """The two lines that sat next to each other in the field, now linked.

    On 2026-08-22 the estimator-consistency finding reported the two
    per-driver level estimates 11.635 dB apart against a 3.0 dB tolerance, and
    the very next line was a prediction gate refusing on numbers built from
    them. Nothing in either line said they were about the same forecast, and
    the one that refused was the one with the weaker claim.

    Two things now say it. The finding is banked rather than dropped — it used
    to be discarded whenever item 2 refused, which threw away the diagnosis of
    the forecast that did the refusing — and the ledger itself carries
    ``level_estimator_suspect``, so a reader of the prediction line learns
    the forecast's inputs were in dispute without correlating by session.
    The magnitude is deliberately NOT copied here: it has one owner, the
    finding and the estimator journal line above.
    """
    decision = _assess(
        _state(suspect=True, matched=True),
        grade_prediction=lambda _sum: _report(False, rms_db=2.0),
    )

    assert decision.finding is not None
    assert decision.finding["estimator_worst_delta_db"] == 5.799
    ledger = decision.journal[-1]
    assert ledger.fields["level_estimator_suspect"] is True
    assert decision.spec_report["comparison"]["level_estimator_suspect"] is True


def test_an_undisputed_forecast_says_so_rather_than_staying_silent():
    """The other two states of the same flag.

    ``False`` is a real answer — the estimators were checked and agreed — and
    is not the same as ``None``, which is a session that produced no
    consistency verdict at all. A single boolean would make a candidate built
    without a fit look like one whose estimators passed.
    """
    agreed = _assess(_state(suspect=False))
    assert agreed.journal[-1].fields["level_estimator_suspect"] is False

    unverdicted = _assess(_state(consistency=None))
    assert unverdicted.journal[-1].fields["level_estimator_suspect"] is None


def test_the_journal_is_in_emission_order_estimator_finding_then_ledger():
    decision = _assess(_state(suspect=True, matched=True))

    assert [r.event for r in decision.journal] == [
        accountability.EVENT_LEVEL_ESTIMATOR_FINDING,
        accountability.EVENT_PREDICTION_GATE,
    ]
    assert decision.journal[0].level == logging.WARNING


def test_the_trims_only_lane_abstains_rather_than_grading_a_thing_against_itself():
    decision = _assess(_state(linearized=None))

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

    decision = _assess(_state(suspect=True, matched=True))
    payload = decision.journal[0].fields

    band = payload["core_level_db"]["woofer"]["band_hz"]
    assert isinstance(band, tuple), band
    assert f"{band}" == "(150.0, 1255.8)"

    # The positive control: the same payload through the planner's record is
    # where the flattening comes from, so this test fails for a real reason
    # rather than because tuples happen to survive everything.
    flattened = JournalRecord("e", payload).fields
    assert isinstance(flattened["core_level_db"]["woofer"]["band_hz"], list)
