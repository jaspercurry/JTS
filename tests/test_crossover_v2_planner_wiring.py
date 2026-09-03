# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The conductor's half of the planner cutover (#2291 Phase 2b).

The planner's own behaviour is pinned in
``tests/test_crossover_v2_intervention_dual_run.py``, and the fixed prescription
is pinned end to end on the banked incident in
``tests/test_crossover_v2_incident_replay.py``. What is left, and what this
module owns, is the seam between them: which measurement objects become which
named planner input, how the returned journal reaches this session's log, and
what the conductor does when the planner refuses.

Each of the three is a place a cutover can go wrong silently — a request built
from the wrong corner still plans, a journal nobody forwards still returns a
plan, and a refusal nobody catches still fails a household's capture.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import replace

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2 import planning
from jasper.active_speaker.crossover_v2.contracts import (
    CandidateFcDisagreementError,
    NoCrossoverSectionsError,
)
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _candidate_sections,
    _conductor,
    _eligible_measure_analysis,
    _run_phase,
)

_DIAG_LOGGER = "jasper.active_speaker.crossover_v2_flow"


def _walked_to_measure():
    """A conductor past CHECK, with an eligible MEASURE analysis to plan from."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    analysis = _eligible_measure_analysis(c.program_for_phase(flow.PHASE_MEASURE))
    return c, analysis


# --------------------------------------------------------------------------- #
# the request: which measurement object becomes which named input
# --------------------------------------------------------------------------- #


def test_the_request_takes_its_corner_from_the_candidates_sections():
    """Not from ``self._fc_hz``, which is the 2026-08-10 defect at this seam.

    Both branches are exercised: a candidate built at another corner
    (``candidate_sections`` supplied) and the configured walk (derived from the
    session preset). Each must produce a context at ITS OWN corner, and the
    supplied one must differ from the session's or the assertion proves nothing.
    """
    c, analysis = _walked_to_measure()
    swept_fc_hz = 1750.0
    assert swept_fc_hz != c._fc_hz, "the two corners must differ"

    seen: list[float] = []
    real = iv.plan_linearization

    def spy(request, **kwargs):
        seen.append(request.context.fc_hz)
        return real(request, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "plan_linearization", spy)
        c._plan_linearization(analysis, analysis.candidate, None)
        c._plan_linearization(
            analysis, analysis.candidate, None,
            candidate_sections=_candidate_sections(c, swept_fc_hz),
        )

    assert seen == [c._fc_hz, swept_fc_hz]


def test_the_request_carries_the_measure_programs_own_sweep_bands():
    """The excited band per role is the MEASURE program's, not a constant.

    It bounds σ-composition, the envelope's validity and the realized-level
    spans, so a request built from anything else plans against a band the
    speaker was never swept over.
    """
    c, analysis = _walked_to_measure()
    program = c.program_for_phase(flow.PHASE_MEASURE)
    seg_w, seg_t = program.segment("sweep_w"), program.segment("sweep_t")

    seen: list[iv.LinearizationRequest] = []
    real = iv.plan_linearization

    def spy(request, **kwargs):
        seen.append(request)
        return real(request, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "plan_linearization", spy)
        c._plan_linearization(analysis, analysis.candidate, None)

    request = seen[0]
    woofer, tweeter = request.drivers
    assert woofer.excited_band_hz == (seg_w.f1_hz, seg_w.f2_hz)
    assert tweeter.excited_band_hz == (seg_t.f1_hz, seg_t.f2_hz)
    assert request.roles == (c._woofer.role, c._tweeter.role)


def test_the_request_carries_the_two_facts_the_analysis_cannot_know():
    """``post_apply_verifies`` and ``cloud_phase_planned`` are the host's.

    Both gate boost permission, and neither is derivable from a
    ``ProgramAnalysis`` — they describe the JOURNEY this session resolved. A
    request that defaulted either would grant or withhold a lift on the wrong
    evidence, so both are asserted against the conductor's own answer, and both
    are exercised in both states.
    """
    for verifies in (True, False):
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        c = _conductor(fakes, post_apply_verifies=verifies)
        _run_phase(c, 1, 1)
        analysis = _eligible_measure_analysis(
            c.program_for_phase(flow.PHASE_MEASURE)
        )

        seen: list[iv.LinearizationRequest] = []
        real = iv.plan_linearization

        def spy(request, __real=real, __seen=seen, **kwargs):
            __seen.append(request)
            return __real(request, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(flow, "plan_linearization", spy)
            c._plan_linearization(analysis, analysis.candidate, None)

        assert seen[0].post_apply_verifies is verifies
        assert seen[0].cloud_phase_planned is (
            flow.PHASE_CLOUD_MEASURE in c.session_phases
        )


# --------------------------------------------------------------------------- #
# the journal port
# --------------------------------------------------------------------------- #


def test_every_planner_record_reaches_this_sessions_journal(caplog):
    """Line for line, in order, at the record's own level, with the session id.

    The planner returns its journal as data and has no logger; if the host
    forwards nothing, a plan still comes back and the operator-facing account
    of how it was reached silently disappears. Compared against the plan's own
    ``journal`` rather than a hand-written list, so a planner that adds a record
    cannot leave this test passing while the line goes unemitted.
    """
    caplog.set_level("INFO", logger=_DIAG_LOGGER)
    c, analysis = _walked_to_measure()
    plan = c._plan_linearization(analysis, analysis.candidate, None)

    assert plan.journal, "the fixture must produce a journal to forward"
    emitted = [
        record for record in caplog.records
        if record.getMessage().startswith("event=correction.crossover_v2_")
    ]
    by_event = [
        record for record in emitted
        if any(record.getMessage().startswith(f"event={r.event} ")
               for r in plan.journal)
    ]
    assert [
        r.getMessage().split(" ", 1)[0][len("event="):] for r in by_event
    ] == [r.event for r in plan.journal]
    for logged, record in zip(by_event, plan.journal):
        assert logged.levelno == record.level, record.event
        assert f"session_id={c.session_id}" in logged.getMessage()


@pytest.mark.parametrize(
    "matched, expected_level", [(True, logging.INFO), (False, logging.WARNING)]
)
def test_the_realized_level_record_is_a_warning_only_when_it_did_not_match(
    caplog, matched, expected_level,
):
    """The severity is the operator's early signal that the pair did not level.

    An unmatched inter-driver level is what the accountability seam DISCLOSES
    one step later (doctrine deviation (i) demoted that gate; it banks
    `event=…_level_match_finding` and the round proceeds), so this line is the
    journal's first sight of the condition and has to stand out before anyone
    knows to look for the finding. A round that discloses rather than stops is
    if anything the stronger reason for the severity: nothing downstream halts
    to make the operator notice. That is a *conditional*, and the sibling
    forwarding test cannot see it: that one compares the forwarded level
    against the record's own, so flattening the planner's conditional to
    always-INFO moves both sides together and stays green (verified by
    mutation). This asserts the ABSOLUTE severity, on both arms.

    The unmatched arm is supplied rather than provoked. Producing a genuinely
    mislevelled pair from the fixture would need branches shaped until the
    estimator disagrees, and the estimator is not this test's subject — the
    severity rule is.
    """
    caplog.set_level("INFO", logger=_DIAG_LOGGER)
    c, analysis = _walked_to_measure()

    real = iv.realized_level_match

    def graded(*args, **kwargs):
        return replace(real(*args, **kwargs), matched=matched)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "realized_level_match", graded)
        plan = c._plan_linearization(analysis, analysis.candidate, None)

    event = "correction.crossover_v2_realized_level_match"
    record = next(r for r in plan.journal if r.event == event)
    assert record.fields["matched"] is matched
    assert record.level == expected_level, "the planner's own severity"

    logged = [
        r for r in caplog.records
        if r.getMessage().startswith(f"event={event} ")
    ]
    assert len(logged) == 1, logged
    assert logged[0].levelno == expected_level, "the forwarded severity"


def test_a_journal_consumer_that_raises_is_disclosed_not_swallowed(caplog):
    """The port is write-only in both directions, and the loss is named.

    A host logger that throws on one field must not cost a household its
    candidate — and must not silently shorten the journal either. The planner
    contains the raise and lists it on ``journal_dropped``; the conductor is
    what turns that field into something an operator can read.
    """
    caplog.set_level("WARNING", logger=_DIAG_LOGGER)
    c, analysis = _walked_to_measure()

    def hostile(_self, _record):
        raise OSError("simulated closed log stream")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Session, "_journal_linearization", hostile)
        plan = c._plan_linearization(analysis, analysis.candidate, None)

    assert plan.role_attenuations_db, "the plan must still be complete"
    assert plan.journal_dropped, "every refused record must be named"
    assert len(plan.journal_dropped) == len(plan.journal)
    assert all("OSError" in entry for entry in plan.journal_dropped)

    # …and the loss reaches an operator rather than living only on the plan.
    disclosure = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith(
            "event=correction.crossover_v2_linearization_journal_dropped "
        )
    ]
    assert len(disclosure) == 1, disclosure
    assert f"dropped={len(plan.journal_dropped)}" in disclosure[0]
    assert "OSError" in disclosure[0]


def test_a_healthy_journal_reports_nothing_dropped(caplog):
    """The control for the test above: silence must mean silence."""
    caplog.set_level("WARNING", logger=_DIAG_LOGGER)
    c, analysis = _walked_to_measure()
    plan = c._plan_linearization(analysis, analysis.candidate, None)
    assert plan.journal_dropped == ()
    assert "linearization_journal_dropped" not in caplog.text


# --------------------------------------------------------------------------- #
# refusals: the planner declines, the conductor degrades
# --------------------------------------------------------------------------- #


def test_a_candidate_with_no_crossover_degrades_to_trims_only(caplog):
    """The deliberate degenerate case, and it fails CLOSED.

    ``CandidateAcousticContext`` refuses to describe a candidate carrying no
    crossover section at all, where the retired fitter would have planned with
    the session's corner and an empty section set. There is no honest
    prescription to compute for a crossover the candidate does not describe, so
    the refusal takes the conductor's established SF2 arm: the household keeps
    its measured trims, ``linearization`` is empty, and nothing is fitted toward
    a corner nobody committed to.

    Driven through ``candidate_sections`` rather than a region-less preset
    because ``MeasuredCrossoverCandidate`` refuses such a preset itself, one
    layer later — so a region-less session cannot produce a candidate at all,
    and the reachable shape of this refusal is a supplied section set that came
    back empty.
    """
    caplog.set_level("WARNING", logger=_DIAG_LOGGER)
    c, analysis = _walked_to_measure()
    empty: dict = {"woofer": (), "tweeter": ()}

    with pytest.raises(NoCrossoverSectionsError):
        c._plan_linearization(
            analysis, analysis.candidate, None, candidate_sections=empty,
        )

    candidate, state = c._build_candidate(
        analysis, None, candidate_sections=empty, source_preset=c._preset,
    )

    assert state.outcome == "fit_failed"
    assert candidate.linearization_outcome == "fit_failed"
    assert dict(candidate.role_attenuations_db) == dict(analysis.candidate.trim_db)
    assert candidate.linearization == {}
    assert state.linearized_predicted_sum is None
    assert state.realized_level_match is None
    # startswith(), not a bare `in caplog.text` substring check: the
    # journal_dropped line's own `dropped_event=` field ends in the six
    # characters "event=", so a substring search would also match a drop of
    # this same event -- see test_the_fit_failure_line_is_said_through_the_
    # host_and_keeps_its_traceback's comment on the same hazard (#2368).
    assert any(
        r.getMessage().startswith(f"event={planning.EVENT_FIT_FAILED} ")
        for r in caplog.records
    )
    assert "reason=NoCrossoverSectionsError" in caplog.text


def test_sections_naming_two_corners_degrade_to_trims_only(caplog):
    """The other refusal, and the one that IS the 2026-08-10 shape.

    A section set naming two corners has no single crossover to plan for. It is
    refused at the context rather than resolved by preferring one, and the
    conductor degrades exactly as above — never by picking a corner.
    """
    caplog.set_level("WARNING", logger=_DIAG_LOGGER)
    c, analysis = _walked_to_measure()
    split = _candidate_sections(c, 1750.0)
    split["tweeter"] = tuple(
        replace(section, fc_hz=1648.7) for section in split["tweeter"]
    )

    with pytest.raises(CandidateFcDisagreementError):
        c._plan_linearization(
            analysis, analysis.candidate, None, candidate_sections=split,
        )

    candidate, state = c._build_candidate(
        analysis, None, candidate_sections=split, source_preset=c._preset,
    )
    assert state.outcome == "fit_failed"
    assert candidate.linearization == {}
    assert "reason=CandidateFcDisagreementError" in caplog.text


def test_a_valid_candidate_still_plans(caplog):
    """The positive control for both refusals above.

    Without it, a conductor that degraded EVERY candidate to trims-only would
    satisfy the two tests above completely.
    """
    caplog.set_level("WARNING", logger=_DIAG_LOGGER)
    c, analysis = _walked_to_measure()
    candidate, state = c._build_candidate(analysis, None)
    assert state.outcome in ("fitted", "trim_rejected")
    assert set(candidate.linearization) == {"woofer", "tweeter"}
    assert state.linearized_predicted_sum is not None
    assert state.realized_level_match is not None
    assert "linearization_fit_failed" not in caplog.text


def test_the_fit_failure_line_is_said_through_the_host_and_keeps_its_traceback(
    caplog,
):
    """The SF2 degrade's disclosure, and the two facts that make it useful.

    Both are claims #2291 Phase 5a-v(c) makes and neither was asserted before
    it: the line is said through the conductor's OWN journal delegate, and it
    still carries the failure's stack.

    * **``session_id``** can only be on the line if it came through
      ``CrossoverV2Session._journal_linearization`` — a pure module has no
      session identity to add. An organ that logged the degrade itself would
      produce a line that reads almost the same and names no session.
    * **``exc_info``** is why the build hands over a
      :class:`~jasper.active_speaker.crossover_v2.planning.FailureRecord`
      rather than one of its two sibling record types. ``logging`` resolves
      ``exc_info=True`` against ``sys.exc_info()`` when the ``LogRecord`` is
      created, so a record carrying only ``(event, fields, level)`` and emitted
      after the handler exits renders NO traceback — the disclosure survives
      and the diagnosis does not. Carrying the caught exception makes the
      deferred emission identical to the inline one.
    """
    caplog.set_level("WARNING", logger=_DIAG_LOGGER)
    c, analysis = _walked_to_measure()
    empty: dict = {"woofer": (), "tweeter": ()}

    c._build_candidate(
        analysis, None, candidate_sections=empty, source_preset=c._preset,
    )

    said = [
        record for record in caplog.records
        if record.getMessage().startswith(f"event={planning.EVENT_FIT_FAILED} ")
    ]
    assert len(said) == 1, "the degrade discloses exactly once"
    line = said[0]
    assert f"session_id={c.session_id}" in line.getMessage()
    assert line.exc_info is not None, "the degrade must carry its stack"
    exc_type, _exc, tb = line.exc_info
    assert exc_type is NoCrossoverSectionsError
    assert tb is not None, "an exception with no traceback renders none"


# --------------------------------------------------------------------------- #
# the build's own disclosure port (#2361)
# --------------------------------------------------------------------------- #


def test_log_event_is_called_from_exactly_one_site_in_planning():
    """Pins the module docstring's "One exception" paragraph to a count,
    not prose alone: ``planning.py`` otherwise writes nothing and logs
    nothing itself, exactly like ``intervention.py`` and ``fc_sweep.py`` —
    the SF2 guard below is the one, deliberate exception. A second call
    site landing here quietly would mean the module grew a second one
    without anybody updating that claim. Source-scanned rather than asserted
    behaviorally, because the property is about how many PLACES in the source
    can log, not about what any one call does.
    """
    source = inspect.getsource(planning)
    assert source.count("log_event(") == 1


@pytest.mark.parametrize("port_error", [OSError, ValueError])
def test_a_raising_journal_costs_a_log_line_not_the_candidate(caplog, port_error):
    """#2361 — ``build_candidate``'s OWN journal call is guarded, not just the
    planner's.

    ``test_a_journal_consumer_that_raises_is_disclosed_not_swallowed`` above
    covers ``plan_linearization``'s ``emit()`` — a DIFFERENT call, one layer
    in. This is the ``journal(FailureRecord(...))`` call inside
    ``build_candidate``'s own SF2 arm, which said port failure PROPAGATED
    through before this fix (probed directly on the pre-guard code): the
    household would have lost the whole candidate to a broken log handler
    reporting an unrelated, already-degraded fit.

    Reached through the SAME ``empty``-sections fixture as
    ``test_a_candidate_with_no_crossover_degrades_to_trims_only`` above,
    which raises inside ``CandidateAcousticContext.from_sections`` — BEFORE
    the planner's own ``emit()`` calls ever run — so the hostile journal is
    invoked exactly once here, by ``build_candidate``'s own SF2 arm, and
    nothing else in this path exercises the guard under test.
    """
    caplog.set_level("WARNING")
    c, analysis = _walked_to_measure()
    empty: dict = {"woofer": (), "tweeter": ()}

    def hostile(_self, _record):
        raise port_error("simulated closed log stream")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow.CrossoverV2Session, "_journal_linearization", hostile)
        candidate, state = c._build_candidate(
            analysis, None, candidate_sections=empty, source_preset=c._preset,
        )

    # The degrade result survives, byte-for-byte the healthy-journal case in
    # test_a_candidate_with_no_crossover_degrades_to_trims_only above.
    assert state.outcome == "fit_failed"
    assert candidate.linearization_outcome == "fit_failed"
    assert dict(candidate.role_attenuations_db) == dict(analysis.candidate.trim_db)
    assert candidate.linearization == {}
    assert state.linearized_predicted_sum is None
    assert state.realized_level_match is None

    # …and the drop is disclosed, not silently swallowed.
    dropped = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith(f"event={planning.EVENT_FIT_FAILED_JOURNAL_DROPPED} ")
    ]
    assert len(dropped) == 1, dropped
    assert f"dropped_event={planning.EVENT_FIT_FAILED}" in dropped[0]
    assert f"reason={port_error.__name__}" in dropped[0]

    # The ORIGINAL fit_failed line never got said — the broken port is
    # exactly why this test exists. Checked per-record with startswith()
    # rather than a raw substring search on caplog.text: the dropped line's
    # own "dropped_event=" field ends in the six characters "event=", so a
    # bare `in` check on the text blob would find EVENT_FIT_FAILED inside it
    # and pass even if the original line were never said.
    said_fit_failed = [
        r for r in caplog.records
        if r.getMessage().startswith(f"event={planning.EVENT_FIT_FAILED} ")
    ]
    assert said_fit_failed == []


def test_a_healthy_journal_never_says_the_port_dropped_anything(caplog):
    """The control for the test above: a working port discloses nothing extra.

    Without this, a build that ALWAYS said ``EVENT_FIT_FAILED_JOURNAL_DROPPED``
    — whether or not the port actually failed — would satisfy the test above
    just as well.
    """
    caplog.set_level("WARNING")
    c, analysis = _walked_to_measure()
    empty: dict = {"woofer": (), "tweeter": ()}

    candidate, state = c._build_candidate(
        analysis, None, candidate_sections=empty, source_preset=c._preset,
    )

    assert state.outcome == "fit_failed"
    assert candidate.linearization_outcome == "fit_failed"
    assert planning.EVENT_FIT_FAILED_JOURNAL_DROPPED not in caplog.text
    assert f"event={planning.EVENT_FIT_FAILED} " in caplog.text


def test_a_split_section_set_refuses_before_the_missing_measure_program():
    """Why ``program_for_phase`` is passed to the organ instead of its answer.

    Both can raise, and the order decides which failure a household is told
    about. The candidate's own section set is the more specific answer, so it
    must be judged FIRST — and a caller that resolved the program eagerly to
    hand the organ a value would invert exactly that, turning a named contract
    refusal into the conductor's generic phase-transition error.

    The un-walked conductor is the reachable shape: before the CHECK gain solve
    there is no MEASURE program at all.
    """
    _walked, analysis = _walked_to_measure()
    fresh = _conductor(FakeSeams())
    with pytest.raises(flow.CrossoverV2FlowError):
        fresh.program_for_phase(flow.PHASE_MEASURE)

    with pytest.raises(NoCrossoverSectionsError):
        fresh._plan_linearization(
            analysis, analysis.candidate, None,
            candidate_sections={"woofer": (), "tweeter": ()},
        )


# --------------------------------------------------------------------------- #
# the no-candidate precondition: kept, on evidence (#2291 Phase 5c-iii)
# --------------------------------------------------------------------------- #


def test_the_no_candidate_refusal_is_not_the_same_as_its_fallback():
    """``_build_candidate``'s ``analysis.candidate is None`` raise is load-bearing.

    #2291's 5c plan carried it as a candidate for deletion — a "duplicate"
    precondition, because ``_measure_verdict`` hoisted the same check to the
    capture that produces the analysis, so production cannot reach this one.
    The ruling was conditional: delete it **only** if the fallback reaches the
    same host mapping. It does not, for two independent reasons, and this pins
    both so the question is settled by a test rather than re-argued.

    **One — the household is told something different.**
    ``correction_crossover_v2.classify_program_failure`` is the ONE classifier
    the failure screen and the operator wizard both read. It claims
    ``CrossoverV2FlowError`` for the program family; it returns ``None`` for
    bare builtins, which is the catch-all arm's ``internal_error``. Delete the
    raise and whatever ``build_candidate`` eventually does with ``None`` is a
    builtin — so a named, classified refusal silently becomes an unclassified
    internal error.

    **Two — the organ would not refuse in its place.**
    ``planning.build_candidate`` accepts ``cand=None``: that is the honest
    shape of a 1-way main, whose MEASURE is one routed solo. Handed a
    two-branch analysis with no candidate it therefore BUILDS one — a
    trims-only candidate at a fixed 0 dB — instead of refusing, so deleting
    this raise would not move the responsibility, it would drop it.
    """
    from jasper.web.correction_crossover_v2 import classify_program_failure

    c, analysis = _walked_to_measure()

    with pytest.raises(flow.CrossoverV2FlowError):
        c._build_candidate(replace(analysis, candidate=None))

    # One: the two outcomes are different sentences, not the same one.
    assert classify_program_failure(
        flow.CrossoverV2FlowError("MEASURE analysis produced no candidate")
    ) == (refusal_copy.REASON_PROGRAM_UNPLAYABLE, ())
    for fallback in (AttributeError("NoneType"), TypeError("NoneType"), ValueError("x")):
        assert classify_program_failure(fallback) is None

    # Two: the organ does not refuse in its place — it builds.
    built, state = planning.build_candidate(
        replace(analysis, candidate=None), None,
        source_preset=c._preset,
        roles=(c._woofer.role, c._tweeter.role),
        plan=c._plan_linearization,
        exclusion_evidence=c._exclusion_evidence_json,
        journal=c._journal_linearization,
    )
    assert set(built.role_attenuations_db) == {c._woofer.role, c._tweeter.role}
    assert state.outcome
