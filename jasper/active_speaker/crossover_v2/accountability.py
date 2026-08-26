# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a built candidate may be PROPOSED at all (#2291 Phase 5a-v).

The gate that runs after a candidate is built and before anything downstream
can apply it.  **Three disclosures and no refusals**, most-specific-first:
whether the two per-driver level estimates agree with each other, then PR-L4's
item 1 (the realized inter-driver level) and item 2 (the spec-graded
prediction).  Every one banks what it found and lets the round proceed to the
measurement that decides.

It is still a gate in the sense that matters — it is the last place a
candidate's own evidence is read before anything can apply it, and everything
it reads reaches the receipt — but it stops nothing.  The two refusals it used
to hold went in the two nanny burn-downs recorded below and in deviation (i);
the class of thing it grades (a forecast, a tonal-balance quality) names no
component-damage mechanism, so `docs/measurement-loop-doctrine.md` §4's closed
list never covered either.

**Item 2 stopped refusing (docs/measurement-loop-doctrine.md deviation (c)).**
Until that burn-down it raised ``correction_not_an_improvement`` — a forecast
vetoing the measurement that would have settled the question, which is the
authority model exactly inverted: "The LLM recommends; the measurement
decides.  Priors, confidence scores and rankings are advisory: they never veto
an in-band experiment."  It fired in the
field on 2026-08-22 against jts3's first prescribed-boost round
(``improvement_db=-0.703`` against ``required_db=0.0``), and the log line one
above it was this module's own estimator-consistency finding reporting the
forecast's inputs 11.635 dB apart against a 3.0 dB tolerance — a prediction
refusing an experiment on numbers it had already disclosed it could not trust.
Item 2 now settles under :data:`LEDGER_NOT_AN_IMPROVEMENT` and the round
proceeds.  Every number it computed still rides, in the same three places it
always did: the :data:`EVENT_PREDICTION_GATE` journal line, the
``spec_report["comparison"]`` block the session stashes, and — through that
stash — the durable ``verify_priors.predicted_spec`` the wire serves
(:func:`.durable_state._predicted_spec_prior`).  What the
burn-down removed is the refusal, not one field of the account.

**This module DECIDES; it does not act.**  That split is the whole reason it
is a module rather than a method, and it is the completion of the #2291
"return accountability as data" principle that Phase 2b started one layer
down.  :func:`assess_accountability` computes what gets said and what gets
banked and hands both back as an :class:`AccountabilityDecision`.  The session
owns every irreversible half: the logger and the ``session_id``, the stash the
host later persists, and the ``CaptureBeginRefused`` construction that stamps
``_last_failure_code`` — which nothing here now asks it to build.
A pure gate can be asked the same question twice and answer the same way,
which is what makes the speculative build safe to drop.

**Inputs are stated, never reached for** — the rule :mod:`.priors`
established.  Two kinds are worth naming because they look like things this
module should own and are deliberately not:

* **The threshold.**  ``material_improvement_db`` arrives as an argument.  It
  carries long field-evidence provenance in the flow, and has a second in-flow
  reader in the prediction ledger's ``required_db`` field.  Moving the constant
  here while that reader stays there would create exactly the cross-module twin
  5a-v just closed for the candidate-required band.  It moves when its other
  reader does.  It still arrives even though it no longer decides a refusal:
  it decides ``improved`` against :data:`LEDGER_NOT_AN_IMPROVEMENT`, and its
  value is what tells the fitted and prescribed bars apart on the wire.  (There
  used to be a second: the level-frame agreement tolerance.  The
  single-datum-owner migration deleted the arbitration it gated; the surviving
  estimator-consistency tolerance is owned once, by
  :data:`~.intervention.LEVEL_ESTIMATOR_TOLERANCE_DB`, and rides the verdict
  this gate reads rather than being passed alongside it.)
* **The household reason codes — all of them, now gone.**  This module used to
  be handed the token to refuse under, precisely because it never rendered or
  branched on one and only routed it into a journal payload and into a
  ``refusal_reason`` on the decision it returns.  Item 2's went with item 2's
  refusal, and item 1's with the realized-level demotion, so the parameter is
  deleted rather than left unused — and so is that field, since deleting the
  last writer of a thing and keeping the thing is how a dead branch outlives
  the condition it described.  The finding vocabulary that replaced it is
  owned where the verdicts are — :data:`~.intervention.LEVEL_DEFINITIONS_DIFFER_REASON`
  and :data:`~.intervention.REALIZED_LEVEL_SUSPECT_REASON` — and is imported
  rather than passed, because a disclosure's reason is this gate's own word for
  what it measured and not the household's word for why it stopped.

**Order is the decision.**  Each step is a narrower diagnosis of the one after
it, so when more than one is true the earliest cause is the one named — more
useful in the journal and more actionable in the household copy.  The returned
journal is in emission order for the same reason: a host that iterates and
logs produces the journal a logging gate would have.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .candidates import LinearizationState
from .intervention import LEVEL_MATCH_AXIS, REALIZED_LEVEL_SUSPECT_REASON

__all__ = [
    "EVENT_LEVEL_ESTIMATOR_FINDING",
    "EVENT_LEVEL_MATCH_FINDING",
    "EVENT_PREDICTION_GATE",
    "EVENT_PREDICTION_UNGRADEABLE",
    "LEDGER_BASELINE_UNGRADEABLE",
    "LEDGER_IMPROVED",
    "LEDGER_NOT_AN_IMPROVEMENT",
    "LEDGER_NO_LINEARIZATION",
    "LEDGER_PREDICTED_IN_SPEC",
    "LEDGER_PREDICTION_UNGRADEABLE",
    "LEDGER_RESIDUAL_UNEVALUABLE",
    "AccountabilityDecision",
    "GateRecord",
    "assess_accountability",
    "level_frame_record",
]

#: The four event names this gate emits. Named constants rather than literals
#: because a journal name is a grep contract — ``test_crossover_v2_*`` and the
#: field runbooks both match on them, so a rename is a breaking change that
#: should be visible as one.
#:
#: Two names changed with the single-datum-owner migration, and the rename is
#: the honest half of it. ``…_level_frame_finding`` banked a disagreement
#: between two VOTING estimators; the same two are still compared, but nothing
#: they say places the pair, so it is ``…_level_estimator_finding``.
#: ``…_level_frame_refused`` is DELETED outright rather than
#: renamed: a consistency suspicion never refuses (the owner's never-nanny
#: ruling), so the line has no condition left to describe.
#:
#: A third changed on the same terms with the realized-level demotion
#: (doctrine deviation (i)). ``…_level_match_refused`` is RENAMED to
#: ``…_level_match_finding``: the condition it describes is unchanged and still
#: fires with every number it carried, but the round now proceeds, so a line
#: whose name says the session stopped would be a false statement about the
#: speaker. Renamed rather than deleted — unlike ``…_level_frame_refused``,
#: whose condition went away — because there is still exactly this much to say.
#: **There is now no level refusal.**
EVENT_LEVEL_ESTIMATOR_FINDING = "correction.crossover_v2_level_estimator_finding"
EVENT_LEVEL_MATCH_FINDING = "correction.crossover_v2_level_match_finding"
EVENT_PREDICTION_UNGRADEABLE = "correction.crossover_v2_prediction_ungradeable"
EVENT_PREDICTION_GATE = "correction.crossover_v2_prediction_gate"

#: Item 2's ledger vocabulary — one value per path the gate can take, so
#: "it passed" and "it never ran" never look the same in the journal. Every
#: path now settles into one of these: the burn-down that stopped item 2
#: refusing is what gave the last one, :data:`LEDGER_NOT_AN_IMPROVEMENT`, a row
#: here rather than a household reason code.
LEDGER_NO_LINEARIZATION = "no_linearization"
LEDGER_PREDICTION_UNGRADEABLE = "prediction_ungradeable"
LEDGER_PREDICTED_IN_SPEC = "predicted_in_spec"
LEDGER_BASELINE_UNGRADEABLE = "baseline_ungradeable"
LEDGER_RESIDUAL_UNEVALUABLE = "residual_unevaluable"
LEDGER_IMPROVED = "improved"
#: The forecast says this correction would measure WORSE than its own pre-fit
#: model, by more than the bar allows. One value for both bars — the fitted
#: class's 0.5 dB and the prescribed class's non-worsening 0.0 — because the
#: two are told apart by ``required_db`` on the same line, and the pair of
#: codes that used to distinguish them existed only to address two different
#: authors in two different refusal sentences. Nobody is refused now, so there
#: is one thing to say and one value to say it under.
LEDGER_NOT_AN_IMPROVEMENT = "not_an_improvement"


@dataclass(frozen=True)
class GateRecord:
    """One log line this gate would have emitted, as data.

    **Why this is not
    :class:`~jasper.active_speaker.crossover_v2.intervention.JournalRecord`,
    which answers the same question one module over.**  That type detaches its
    payload through ``detached_json``, which normalizes *containers* — and a
    tuple becomes a list on the way through.  That is correct for the planner,
    whose payloads become JSON.  It is wrong here: ``core_level_db`` carries
    per-role ``band_hz`` pairs as TUPLES, ``log_event`` renders them with
    ``str``, and the shipped line reads ``'band_hz': (150.0, 1255.8)``.
    Routing it through the planner's record silently rewrote that to
    ``[150.0, 1255.8]`` in a field-diagnosis surface — caught by the 5a-v
    dual run, which is the reason this type exists rather than an argument
    that it should.

    The payload is therefore held EXACTLY as built.  That is safe here for a
    reason it is not safe there: every value is computed inside
    :func:`assess_accountability` from scalars, except ``core_level_db``,
    whose one shared structure the caller is documented never to mutate — and
    the caller is the session's own journal delegate, which only reads.
    """

    event: str
    fields: Mapping[str, Any]
    level: int = logging.INFO


@dataclass(frozen=True)
class AccountabilityDecision:
    """What the gate decided, and everything the caller must do about it.

    **There is no ``refusal_reason``, and its absence is the ruling rather than
    an omission.**  This gate carried one until the realized-level demotion
    (`docs/measurement-loop-doctrine.md` deviation (i)) deleted its last writer;
    item 2's had already gone with the nanny burn-down (deviation (c)).  The
    field went WITH that writer rather than staying behind as a permanently
    ``None`` seam, because a field nothing can set is not an ability the gate
    retains — it is a branch every caller must trace to discover is dead, and
    the flow carried exactly such a ``raise`` until this change removed it.  A
    future refusal here would be a deliberate act under §4's closed list, and it
    adds the field back beside the writer that earns it.

    ``spec_report_written`` went in the same cut and for the same reason.  It
    existed to tell "item 2 ran and produced no report — clear the stash" from
    "item 2 never ran — do not touch it", and deviation (i)'s deleted return was
    the last path that could produce the second.  Item 2 is now reached on every
    path this gate takes, so the caller assigns the stash unconditionally and
    ``spec_report`` alone carries the one fact left: a report, or ``None``
    meaning there was no summed model to grade.

    ``finding`` is the banked record when EITHER level check had something to
    report: the two per-driver estimates in disagreement, the committed pair's
    realized levels too far apart, or both.  Neither has a refusal arm, so a
    ``finding`` now accompanies a round that proceeds — which is the only kind
    of round there is.

    ``journal`` is in emission order.  A caller that writes the stash first and
    then iterates produces the same journal, and the same session state, as
    the method this replaced — the one ordering claim worth pinning rather than
    arguing, which ``test_crossover_v2_accountability`` does directly, now that
    no arm returns early enough to demonstrate it.
    """

    journal: tuple[GateRecord, ...] = ()
    finding: Mapping[str, Any] | None = None
    #: A ``dict`` rather than a ``Mapping``: this IS the host's stash, which the
    #: host owns and the review screen's persistence later reads back, and the
    #: gate has already merged item 2's ``comparison`` block into it.
    spec_report: dict[str, Any] | None = None


def level_frame_record(
    state: LinearizationState,
) -> Mapping[str, Any] | None:
    """This session's banked level-frame reservation, as flat evidence.

    Built when EITHER level check has something to report — the two per-driver
    estimates in disagreement, the committed pair's realized levels past
    tolerance, or both — from the plan this candidate's own build returned; no
    measurement, no re-derivation, no second verdict. Taking the state as an
    argument rather than reading it off ``self`` is what makes "this session's"
    true of one candidate rather than of whichever build ran last (#2291 Phase
    2b). The attribution package turns it into a finding
    (:func:`~jasper.attribution.promotion.promote_level_frame_disagreement`);
    this function owns *what the evidence is*, that one owns *what it means*.
    Nothing here imports attribution, so the flow keeps no dependency on the
    diagnosis layer.

    **Named for the frame, not for one of its two instruments.** It was
    ``estimator_consistency_record`` while the estimator check was the only one
    that could bank — the realized check refused instead of banking, so it had
    no record to build. The realized-level demotion (`docs/measurement-loop-
    doctrine.md` deviation (i)) gave it one, and the promoter, the seam, and the
    session field this feeds were all already called *level frame*. Renaming is
    the honest half of widening it, exactly as the two event renames above are.

    **Flat, and every value a finite scalar or a string**, because that is
    what :class:`~jasper.attribution.findings.Finding` accepts — nesting would
    be rejected at construction, and rejection is a lost diagnosis. Per-role
    numbers are therefore suffixed with the role, which is also what makes the
    record self-describing to a reader who has never seen this schema.

    **BOTH estimates ride, plus the gap between them.** The record carries the
    fit's per-driver median (``core_level_db_*``), the trim solve's per-driver
    level-match term (``trim_band_average_db_*``), and their per-role distance
    (``estimator_delta_db_*``). There is no third instrument and no referee —
    that is the point of the migration rather than an omission: the pair is
    anchored on the raw measured trim, so nothing has to adjudicate between
    these two. Before #2609 a reader of this finding was being asked to believe
    a session had proceeded past a gate that would have stopped it; now they
    are being told something narrower and truer — a capture whose two level
    estimates disagree is worth re-taking.

    **What the session DID about it: nothing, and that is the point.** The
    round proceeds on the raw measured trim whatever this record says. Its
    predecessor carried ``frame_exclusion_reason`` and per-role
    ``anchor_delta_db_*`` because a disputed frame USED to zero the anchor's
    offsets; no verdict here moves a number, so there is no dB consequence to
    report and those two fields are deleted rather than left reading zero.

    **A realized-only record does not need the fit's core bands, and no longer
    dies without them.** The band a finding carries has to come from somewhere,
    and the estimator condition can only take it from ``cores`` — the spans its
    two medians were computed on. The realized condition has its own: the
    mirrored half-bands about Fc that ``realized_branch_level_match`` read the
    two levels on, which ride on the verdict itself. So a missing core band
    falls back to those rather than dropping the record. This mattered the
    moment the realized check stopped refusing: while it refused, losing the
    record cost a diagnosis beside a session that had already stopped and said
    why; now the record is the only DURABLE half of a disclosure on a round that
    proceeds, and the journal line the gate also emits is not a reader.

    Returns ``None`` when the condition this record is ABOUT cannot name a
    band — which needs an estimator disagreement with no per-role core spans,
    since the realized verdict always carries its two. NEITHER instrument
    having one is deliberately NOT the bar: in that same case the realized
    spans do exist, and the guard below declines to borrow them to describe an
    estimator finding.
    """

    consistency = state.level_consistency
    cores = state.core_level_evidence
    realized = state.realized_level_match
    definitions_differ = consistency is not None and consistency.differs
    realized_suspect = realized is not None and not realized.matched
    if not definitions_differ and not realized_suspect:
        return None
    # The band this finding is ABOUT: the span the level reads were actually
    # taken over, unioned across roles. Deliberately the CORE bands and not the
    # radiating ones — a high-pass branch radiates to infinity, so a radiating
    # union has no upper edge, while the core band is exactly the finite span
    # each median was computed on.
    #
    # **The union is an OUTER hull, and it spans a gap neither median read** —
    # on the session fixture the woofer's core stops at 1255.8 Hz and the
    # tweeter's starts at 2020.0, so 1255.8-2020.0 Hz is inside the finding's
    # band and inside no per-branch measurement. That is the right shape rather
    # than a rounding of it: this finding is about the RELATIONSHIP between two
    # drivers, which lives in the handoff sitting in that gap, and a band stated
    # as two disjoint intervals would say the finding is about two places when
    # it is about one. It is not, and must not be read as, a claim that either
    # per-branch capture measured inside the gap — the per-role ``core_band_*``
    # keys below are what say where each number came from. (The summed
    # at-the-mark capture DOES cover the gap, and still does not own the level
    # datum: the raw per-branch trim solve places the pair, because the two
    # captures ride different graphs — see
    # :func:`~.intervention.plan_linearization`'s anchor block and #2653.)
    edges = [
        band for role in cores
        if (band := cores[role].get("band_hz")) is not None
    ]
    if not edges and realized is not None and not definitions_differ:
        # The realized instrument's OWN spans — the mirrored half-bands about
        # Fc that its two levels were read on — for a record the REALIZED
        # condition is the reason for. Guarded on that rather than on the
        # bands' absence alone: the band a finding carries has to be the span
        # its own reason was measured over, so an estimator disagreement with
        # no core spans still returns ``None`` (as the docstring says) rather
        # than borrowing the other instrument's bands to describe itself.
        # Both spans are non-optional on ``RealizedLevelMatch``, and the
        # ``hi_edges`` filter below is the backstop either way.
        edges = [realized.woofer_band_hz, realized.tweeter_band_hz]
    lo_edges = [float(band[0]) for band in edges]
    hi_edges = [float(band[1]) for band in edges if band[1] is not None]
    if not lo_edges or not hi_edges:
        return None
    record: dict[str, Any] = {
        "f_lo_hz": min(lo_edges),
        "f_hi_hz": max(hi_edges),
        # WHICH axis every level on this record was read on. Toole: where
        # woofer beaming and horn directivity mismatch, the on-axis,
        # listening-window and power-response ratios differ and there is no
        # single correct level — so a banked level that does not name its axis
        # is a number a later reader cannot place. It rides unconditionally,
        # like both instruments' numbers, because it describes the capture and
        # not whichever condition happened to be the reason.
        "level_match_axis": LEVEL_MATCH_AXIS,
    }
    # ONE reason field, and the estimator disagreement wins when both fire —
    # for the same reason its gate runs first below: it is the more specific
    # diagnosis of the same disease. Nothing is lost to the precedence, because
    # BOTH sub-verdicts' numbers ride this record unconditionally; the field
    # says which one is why the record exists.
    #
    # Spelled as the full condition rather than reusing ``definitions_differ``
    # so the ``consistency is not None`` narrowing is expressed where the
    # attribute is read; a bare boolean carries the fact but not the type, and
    # this attribute access is the one place it matters.
    if consistency is not None and consistency.differs:
        record["reason"] = consistency.reason
    else:
        record["reason"] = REALIZED_LEVEL_SUSPECT_REASON
    # PREFIXED, like every other key that belongs to one of the two
    # instruments. They were bare while this record could only ever be about
    # the estimators, and widening it to the realized condition is what made
    # bare ambiguous: a reader who finds ``reason=realized_levels_disagree``
    # beside an unqualified ``worst_delta_db`` has no way to know the number
    # belongs to the OTHER instrument and is not the realized error. The prefix
    # is the record's own existing one — ``estimator_delta_db_{role}`` below —
    # so this disambiguates without minting a second vocabulary, and it matches
    # what ``plan_linearization``'s journal already spells
    # ``level_estimator_worst_delta_db`` one module over.
    if consistency is not None:
        record["estimator_worst_delta_db"] = round(
            float(consistency.worst_delta_db), 3
        )
        record["estimator_tolerance_db"] = float(consistency.tolerance_db)
    if realized is not None:
        record.update(
            realized_difference_db=round(float(realized.difference_db), 3),
            realized_tolerance_db=float(realized.tolerance_db),
            realized_level_w_db=round(float(realized.level_w_db), 3),
            realized_level_t_db=round(float(realized.level_t_db), 3),
        )
    for role, core in cores.items():
        band = core.get("band_hz") or (None, None)
        radiating = core.get("radiating_band_hz") or (None, None)
        record[f"core_level_db_{role}"] = core.get("level_db")
        record[f"core_band_lo_hz_{role}"] = band[0]
        record[f"core_band_hi_hz_{role}"] = band[1]
        record[f"radiating_band_lo_hz_{role}"] = radiating[0]
        record[f"radiating_band_hi_hz_{role}"] = radiating[1]
        if role in state.trim_band_estimate_db:
            record[f"trim_band_average_db_{role}"] = round(
                float(state.trim_band_estimate_db[role]), 3
            )
        # What the two per-driver estimators made of EACH OTHER, per role, in
        # the relative frame the check compares in — a symmetric distance with
        # no owner term in it, since neither of them places the pair. Both
        # estimators ride whether or not either one is the reason this record
        # exists: which of them disagreed is the diagnosis, and banking only
        # the worst would drop it.
        if consistency is not None and role in consistency.estimator_delta_db:
            record[f"estimator_delta_db_{role}"] = round(
                float(consistency.estimator_delta_db[role]), 3
            )
    # THE ATTRIBUTION, and the whole reason a realized-level record can now
    # exist without an estimator disagreement beside it. The MEASURE ripple
    # polish moves one trim off the level-matching solve, and the excursion
    # passes straight through to the realized inter-driver level error. Since
    # the polish is admitted only within ``REALIZED_LEVEL_MATCH_TOLERANCE_DB``
    # it can no longer be the whole story of a realized disagreement — so a
    # reader who finds this near zero knows to look somewhere other than the
    # polish, which is exactly what a disclosure is for.
    #
    # Its OWN loop, keyed on its own mapping rather than folded into the
    # per-core loop above: the polish is a property of the trim solve and its
    # roles are the request's, while ``cores`` carries only the roles the fit
    # produced a median for. Keying this on ``cores`` silently dropped a role
    # the fit had no core band for — the case where the attribution is most
    # worth having.
    for role, delta_db in state.polish_delta_db.items():
        record[f"polish_delta_db_{role}"] = round(float(delta_db), 3)
    return record


def assess_accountability(
    *,
    predicted_sum: Any,
    raw_predicted_sum: Any,
    state: LinearizationState | None,
    grade_prediction: Callable[[Any], Any],
    material_improvement_db: float,
) -> AccountabilityDecision:
    """The three accountability assertions, as a decision rather than an act.

    ``state`` is the candidate's own planner output.  ``None`` means no build
    produced one, which is the same evidence state as an ineligible session and
    takes the same path: no consistency verdict to disclose, no realized verdict
    to fail, and item 2's abstain below.

    ``grade_prediction`` is the spec evaluator, injected rather than imported.
    It is called AT MOST twice and the second call is conditional, which is why
    the caller may not pre-compute both reports: grading the baseline
    unconditionally would run an evaluator — and emit its own diagnostics — on
    a path that today never grades it.
    """
    journal: list[GateRecord] = []
    state = state if state is not None else LinearizationState()

    # --- the two per-driver level estimates against each other -------
    #
    # Runs before item 1 because it is the more specific diagnosis of the
    # same disease: item 1 grades the level the committed trim REALIZES,
    # this grades whether the two per-driver instruments that trim was
    # cross-checked against still agree with each other about where the
    # drivers sit. On the 2026-07-27 captures those reads sat 10.9-13.1
    # dB apart; PR-L3 fixed that cause, and this is what stops the next one
    # from shipping unremarked.
    #
    # **It banks and proceeds. It never refuses** — the owner's
    # never-nanny ruling (2026-08-17, #2609): a subordinate estimate that
    # disagrees with the other one flags the CAPTURE as retriable; it does not
    # discard the datum and does not stop the session. There is nothing
    # left for it to refuse on, either: the disagreement no longer changes
    # any committed number, because the raw measured trim owns the placement
    # (:func:`~.intervention.anchor_trims`).
    #
    # **What this replaced, and why the deleted refusal arm was reachable
    # by nothing.** Until #2609 a disagreement past 3.0 dB asked one more
    # question — does the realized-level check pass on the pair about to
    # ship? — and refused when it did not, under item 1's own
    # ``driver_levels_disagree`` code. Every case that arm could refuse,
    # item 1 refused on its own two branches below, under the same code:
    # the arm's extra reach was the ``realized is None`` case, and the
    # code's own prior finding is that a state carrying no realized verdict
    # carries no disagreement either (a fit that raised part-way yields
    # neither), so the pair never co-occurred. What the arm's deletion
    # therefore removed was a second owner of one refusal, not a stop.
    # (Item 1's refusal has since gone too — deviation (i) — so both halves
    # of this paragraph are now archaeology; the code it names is deleted.)
    #
    # The cliff that arm sat on is the located mechanism of the 2026-08-16
    # shortfall round — 3.326 dB against a 3.0 dB bar, +3.79 dB of
    # unrequested tweeter level, and a rolled-back round. #2609's conviction
    # comment carries the full chain.
    if state.level_consistency is not None and state.level_consistency.differs:
        consistency = state.level_consistency
        realized = state.realized_level_match
        journal.append(GateRecord(
            EVENT_LEVEL_ESTIMATOR_FINDING,
            {
                "reason": consistency.reason,
                "worst_delta_db": round(float(consistency.worst_delta_db), 3),
                "tolerance_db": float(consistency.tolerance_db),
                # WHICH axis both levels were read on. Toole: where beaming and
                # horn directivity mismatch there is no single correct level, so
                # a gap reported without its axis is a number a reader cannot
                # place.
                "matched_axis": consistency.matched_axis,
                "core_level_db": dict(state.core_level_evidence),
                "trim_band_average_db": {
                    k: round(float(v), 3)
                    for k, v in state.trim_band_estimate_db.items()
                },
                # What the two per-driver estimators made of each other, per
                # role — symmetric, with no owner term. Which one disagreed is
                # the diagnosis; the worst alone would drop it.
                "estimator_delta_db": {
                    k: round(float(v), 3)
                    for k, v in consistency.estimator_delta_db.items()
                },
                # The outcome check, for the reader deciding how much to
                # care. It no longer DECIDES anything here — it decides item
                # 1 below, on its own — but a suspicion beside a passing
                # realized level reads very differently from one beside a
                # failing one.
                "realized_difference_db": (
                    None if realized is None
                    else round(float(realized.difference_db), 3)
                ),
            },
            level=logging.WARNING,
        ))
    # The one fact item 2's ledger borrows from this gate: had the two level
    # definitions parted company past the disclosure trigger when the forecast
    # below was built? ``None`` when no verdict exists at all, which is a third
    # state and not a quiet "no". Same name and same tri-state
    # as the giveback event's ``level_definitions_differ``
    # (:func:`~.intervention.plan_linearization`) on purpose — one vocabulary
    # per question, so a reader greps one field name across the round.
    # Deliberately the BOOLEAN and not the magnitude — ``worst_delta_db`` has
    # one owner (the journal line and the banked finding directly above, both
    # keyed to the same session), and a second copy on the prediction line is a
    # datum that can drift. What the flag adds is the LINK: on 2026-08-22 these
    # two lines sat one after the other in the field journal, an 11.635 dB
    # disagreement and a -0.703 dB verdict computed on top of it, and nothing
    # in either line said they were about the same numbers.
    definitions_differ = (
        None if state.level_consistency is None
        else bool(state.level_consistency.differs)
    )

    # --- item 1: the inter-driver realized level ---------------------
    #
    # **It banks and proceeds. It never refuses** — the doctrine's never-nanny
    # rule (`docs/measurement-loop-doctrine.md` §3 and §5, deviation (i)). This
    # was the last level refusal, and it was a QUALITY check: it names no
    # component-damage mechanism, so it is outside §4's closed list and
    # discloses rather than blocking. The absolute rails that DO bound loudness
    # are untouched and are elsewhere — the output limiters, the non-positive
    # trim clamps, `devices.volume_limit`, and the commissioning SPL stop.
    #
    # **What made refusing here indefensible rather than merely strict.** The
    # number it grades is the MEASURE ripple polish's trim excursion, near
    # enough exactly: the polish moves one trim off the level-matching solve and
    # the give-back passes that excursion straight through. Until this change
    # the polish was ADMITTED out to 6.0 dB while this gate refused past 3.0, so
    # every polish landing in that band produced a round the session was
    # guaranteed to refuse — a refusal it manufactured for itself, between two
    # thresholds neither of which had measured anything. `program_analysis`
    # closes the band by coupling the admission to this same tolerance; with
    # that in place a firing here is no longer the polish's doing, which is
    # precisely when a disclosure is worth reading. `polish_delta_db_*` rides
    # the banked record so the reader can check that for themselves.
    match = state.realized_level_match
    if match is not None and not match.matched:
        journal.append(GateRecord(
            EVENT_LEVEL_MATCH_FINDING,
            {
                # The FINDING vocabulary, not the retired household refusal
                # code — see `intervention.REALIZED_LEVEL_SUSPECT_REASON` for
                # why the old literal is not reused here.
                "reason": REALIZED_LEVEL_SUSPECT_REASON,
                "difference_db": round(float(match.difference_db), 3),
                "tolerance_db": match.tolerance_db,
                "level_w_db": round(float(match.level_w_db), 3),
                "level_t_db": round(float(match.level_t_db), 3),
                # The attribution, on the line as well as in the record: a
                # journal reader diagnosing a round in the field should not have
                # to open the finding store to learn whether the polish explains
                # this. ``None`` when no plan measured it.
                "polish_delta_db": (
                    {k: round(float(v), 3) for k, v in state.polish_delta_db.items()}
                    or None
                ),
            },
            level=logging.WARNING,
        ))
    # Built from the state, so it covers whichever of the two checks above
    # fired — and both, when both did. ``None`` when neither has anything to
    # say, which is the ordinary round.
    finding = level_frame_record(state)

    # --- item 2: spec-grade the prediction ---------------------------
    #
    # PR-6b made auto-apply unconditional at this seam ("this is
    # unconditionally True here, not a second decision"). This deliberately
    # AMENDS that, under the linearization-integrity work order's PR-L4
    # item 2 (docs/linearization-integrity-plan.md), which is the sanction
    # for the change: on 2026-07-27 the honest flatness instrument failed
    # all three bands two seconds before an unconditional auto-apply, and
    # its verdict reached zero surfaces. PR-6b's claim — that MEASURE's
    # trust gates already decided — was true about the CAPTURE and silent
    # about the CORRECTION. This adds the missing half: the capture is
    # trusted, and now the thing built from it has to show its work.
    #
    # **BEFORE and AFTER, on the same instrument** (PR-L4 review B1). The
    # first cut of this gate compared the model's residual against the
    # MEASURED pre-apply cloud's, which is not a comparison: an
    # eight-position in-room spatial mean and a gated two-branch model at
    # the mark are different instruments in different frames, so the margin
    # between them is room-sized. Held to a constant, excellent correction
    # (predicted pooled 0.858 dB) the reviewer varied only the ROOM and
    # watched the verdict flip — the shipped fixture applied at +0.333, and
    # every BETTER room refused. That is exactly backwards: it punished
    # good rooms, and it tightened as the correction improved. Worse, it was
    # a live trap — an owner who undoes first re-measures a decent speaker
    # and re-runs into a stricter bar.
    #
    # The fix is to ask the question the gate was always trying to ask, of
    # one instrument: grade the RAW pre-fit two-branch prediction and the
    # LINEARIZED one through the IDENTICAL evaluator, and require the
    # correction to move ITS OWN model materially. Same branches, same
    # grid, same evaluator, same position — the room cancels because it is
    # not in either term.
    #
    # **Graded ONCE, here** (two-stage commission D4). This is the last
    # place the FULL-RESOLUTION `(freqs, magnitudes)` tuple exists: what
    # survives to the durable state is `_decimate_sum`'s 512-point block
    # average (issue #1858 — a raw stride before that fix), and re-grading
    # that later would be a DIFFERENT instrument from the one this ledger
    # reports — the two can disagree on a narrow band,
    # on the one screen whose entire purpose is the honest spec verdict. So
    # the report this gate computes is the report the host persists, and
    # the persisted curve stays what it is: a drawing, not the instrument.
    #
    # It is hoisted ABOVE the trims-only abstain below (it used to sit
    # underneath) for a reason the gate itself does not care about but the
    # review screen does: the trims-only lane still commits trims and still
    # predicts a response, so it HAS a gradeable prediction. Leaving it
    # ungraded would put "we could not predict this" in front of a
    # household about a prediction we can in fact grade. **The gate's own
    # decisions are untouched** — every exit below is exactly where it was,
    # reached on exactly the same condition.
    after = grade_prediction(predicted_sum)
    # The stash, and the named line an absent report earns (two-stage
    # commission D4). The line lands with the ``None`` rather than with the
    # screen that renders it, because per AGENTS.md's no-silent-failure rule
    # a disclosure nobody can grep for is not a disclosure. ``why`` separates
    # the two causes, which have different remedies — a prediction that was
    # never built (no summed model to grade) from one the evaluator refused
    # (a malformed or degenerate curve, already logged in detail by the
    # evaluator itself).
    spec_report: dict[str, Any] | None = (
        after.to_dict() if after is not None else None
    )
    if after is None:
        journal.append(GateRecord(
            EVENT_PREDICTION_UNGRADEABLE,
            {"why": "no_prediction" if predicted_sum is None else "evaluator_refused"},
            level=logging.WARNING,
        ))

    def _settle(
        reason: str,
        *,
        before: Any = None,
        improvement_db: float | None = None,
        level: int = logging.INFO,
    ) -> AccountabilityDecision:
        """One ledger line per session for item 2's gate, on EVERY path.

        Mirrors item 1's realized-level event, which logs whether or not it
        refuses (PR-L4 review S4). A gate that only speaks when it fires
        leaves "it passed" and "it never ran" looking identical in the
        journal — the exact ambiguity this gate exists to remove, and the
        one a field diagnosis of a dark speaker would need first.

        **Every path through here proceeds.** The ``refusal`` argument went
        with item 2's veto: there is now exactly one thing this helper does,
        which is say what the forecast found and hand it on.
        """
        from jasper.active_speaker.flat_spec import spec_convergence_residual

        def _rms(report: Any) -> float | None:
            if report is None:
                return None
            value = spec_convergence_residual(report).rms_db
            return round(float(value), 3) if value is not None else None

        rounded = (
            round(float(improvement_db), 3) if improvement_db is not None else None
        )
        if spec_report is not None:
            spec_report["comparison"] = {
                "reason": reason,
                "baseline_rms_db": _rms(before),
                "selected_rms_db": _rms(after),
                "improvement_db": rounded,
                "required_db": material_improvement_db,
                "level_definitions_differ": definitions_differ,
            }
        journal.append(GateRecord(
            EVENT_PREDICTION_GATE,
            {
                "reason": reason,
                "before_rms_db": _rms(before),
                "after_rms_db": _rms(after),
                "after_passed": (
                    after.overall_passed if after is not None else None
                ),
                "improvement_db": rounded,
                "required_db": material_improvement_db,
                "level_definitions_differ": definitions_differ,
            },
            level=level,
        ))
        return AccountabilityDecision(
            journal=tuple(journal),
            finding=finding,
            spec_report=spec_report,
        )

    if raw_predicted_sum is None or state.linearized_predicted_sum is None:
        # No fit ran this attempt (ineligible mic tier, or the fit failed
        # into SF2's trims-only fallback), so `predicted_sum` IS
        # `raw_predicted_sum` — the same object. Grading a thing against
        # itself always returns "no improvement", which would file every
        # trims-only candidate under :data:`LEDGER_NOT_AN_IMPROVEMENT` on the
        # strength of arithmetic rather than evidence — a false entry in the
        # ledger even now that it is only an entry. Abstain, loudly — carrying
        # the after-report the hoist
        # above just produced, so the ledger and the wire cannot state
        # different verdicts about one session's one prediction.
        return _settle(LEDGER_NO_LINEARIZATION)
    if after is None:
        return _settle(LEDGER_PREDICTION_UNGRADEABLE)
    if after.overall_passed:
        # A prediction that meets the spec on its own needs no improvement
        # argument, and judging an in-spec result on "how much did it
        # improve" would read the flattest speakers worst.
        return _settle(LEDGER_PREDICTED_IN_SPEC)
    before = grade_prediction(raw_predicted_sum)
    if before is None:
        return _settle(LEDGER_BASELINE_UNGRADEABLE)
    from jasper.active_speaker.flat_spec import spec_convergence_residual

    after_rms_db = spec_convergence_residual(after).rms_db
    before_rms_db = spec_convergence_residual(before).rms_db
    if after_rms_db is None or before_rms_db is None:
        return _settle(LEDGER_RESIDUAL_UNEVALUABLE, before=before)
    improvement_db = float(before_rms_db) - float(after_rms_db)
    if improvement_db >= material_improvement_db:
        return _settle(
            LEDGER_IMPROVED, before=before, improvement_db=improvement_db,
        )
    # The forecast says worse, and says so at WARNING — loud enough to grep
    # for, and not ERROR, because nothing failed: a model that expects a
    # candidate to measure worse is a prediction, and under the doctrine's
    # authority model a prediction only recommends. What decides is the round
    # this no longer stops.
    return _settle(
        LEDGER_NOT_AN_IMPROVEMENT, before=before, improvement_db=improvement_db,
        level=logging.WARNING,
    )
