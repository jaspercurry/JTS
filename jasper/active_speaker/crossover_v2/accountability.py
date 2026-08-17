# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a built candidate may be PROPOSED at all (#2291 Phase 5a-v).

The gate that runs after a candidate is built and before anything downstream
can apply it.  Two refusals and one disclosure, most-specific-first: the
subordinate estimators' consistency with the summed level owner (banked, never
refusing), then PR-L4's item 1 (the realized inter-driver level) and item 2
(the spec-graded prediction).  Refusing here means no candidate is ever stashed or
published, so the review screen has nothing to offer and the household is
never asked to decide about a correction JTS cannot stand behind.

**This module DECIDES; it does not act.**  That split is the whole reason it
is a module rather than a method, and it is the completion of the #2291
"return accountability as data" principle that Phase 2b started one layer
down.  :func:`assess_accountability` computes which refusal fires, what gets
said, and what gets banked, and hands all three back as an
:class:`AccountabilityDecision`.  The session owns every irreversible half:
the logger and the ``session_id``, the ``CaptureBeginRefused`` construction
that stamps ``_last_failure_code``, and the stash the host later persists.
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
  reader does.  (There used to be a second: the level-frame agreement
  tolerance.  The single-datum-owner migration deleted the arbitration it
  gated; the surviving estimator-consistency tolerance is owned once, by
  :data:`~.intervention.LEVEL_ESTIMATOR_TOLERANCE_DB`, and rides the verdict
  this gate reads rather than being passed alongside it.)
* **The two household reason codes.**  They are opaque tokens here: this
  module never renders one, never branches on one, and only routes the one it
  was handed — into a journal payload and into
  :attr:`AccountabilityDecision.refusal_reason`.  :mod:`.spatial` returns
  refusal KINDS and lets the flow map them, which is the better shape when a
  refusal is only *selected*; it does not fit here, because these codes appear
  as VALUES inside log lines whose bytes are pinned.

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

__all__ = [
    "EVENT_LEVEL_ESTIMATOR_FINDING",
    "EVENT_LEVEL_MATCH_REFUSED",
    "EVENT_PREDICTION_GATE",
    "EVENT_PREDICTION_UNGRADEABLE",
    "LEDGER_BASELINE_UNGRADEABLE",
    "LEDGER_IMPROVED",
    "LEDGER_NO_LINEARIZATION",
    "LEDGER_PREDICTED_IN_SPEC",
    "LEDGER_PREDICTION_UNGRADEABLE",
    "LEDGER_RESIDUAL_UNEVALUABLE",
    "AccountabilityDecision",
    "GateRecord",
    "assess_accountability",
    "estimator_consistency_record",
]

#: The four event names this gate emits. Named constants rather than literals
#: because a journal name is a grep contract — ``test_crossover_v2_*`` and the
#: field runbooks both match on them, so a rename is a breaking change that
#: should be visible as one.
#:
#: Two names changed with the single-datum-owner migration, and the rename is
#: the honest half of it. ``…_level_frame_finding`` banked a disagreement
#: between two voting estimators; there is no vote now, so it is
#: ``…_level_estimator_finding`` — one summed owner, two subordinate estimates
#: graded against it. ``…_level_frame_refused`` is DELETED outright rather than
#: renamed: a consistency suspicion never refuses (the owner's never-nanny
#: ruling), so the line has no condition left to describe.
#: ``…_level_match_refused`` is untouched and is now the only level refusal.
EVENT_LEVEL_ESTIMATOR_FINDING = "correction.crossover_v2_level_estimator_finding"
EVENT_LEVEL_MATCH_REFUSED = "correction.crossover_v2_level_match_refused"
EVENT_PREDICTION_UNGRADEABLE = "correction.crossover_v2_prediction_ungradeable"
EVENT_PREDICTION_GATE = "correction.crossover_v2_prediction_gate"

#: Item 2's ledger vocabulary — one value per path the gate can take, so
#: "it passed" and "it never ran" never look the same in the journal. The
#: refusing path carries the household reason code instead, which is why there
#: is no ``LEDGER_NOT_AN_IMPROVEMENT`` here.
LEDGER_NO_LINEARIZATION = "no_linearization"
LEDGER_PREDICTION_UNGRADEABLE = "prediction_ungradeable"
LEDGER_PREDICTED_IN_SPEC = "predicted_in_spec"
LEDGER_BASELINE_UNGRADEABLE = "baseline_ungradeable"
LEDGER_RESIDUAL_UNEVALUABLE = "residual_unevaluable"
LEDGER_IMPROVED = "improved"


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

    ``refusal_reason`` is the household code to refuse under, or ``None`` to
    proceed.  ``finding`` is the #1866 banked record when the frame gate took
    the finding+proceed path.

    **``spec_report`` and ``spec_report_written`` are two facts, not one.**
    ``None`` with ``spec_report_written`` True means "graded, and the grader
    refused" — the stash must be cleared.  ``None`` with it False means the
    gate refused before item 2 ran at all and the stash must not be touched.
    Collapsing them would make a frame refusal clear a stash it never reached,
    which is a different session state than the one that happened.

    ``journal`` is in emission order.  A caller that writes the stash first and
    then iterates produces the same journal, and the same session state, as
    the method this replaced — the one ordering claim worth pinning rather than
    arguing, which
    ``test_crossover_v2_accountability`` does from both refusal arms.
    """

    journal: tuple[GateRecord, ...] = ()
    refusal_reason: str | None = None
    finding: Mapping[str, Any] | None = None
    #: A ``dict`` rather than a ``Mapping``: this IS the host's stash, which the
    #: host owns and the review screen's persistence later reads back, and the
    #: gate has already merged item 2's ``comparison`` block into it.
    spec_report: dict[str, Any] | None = None
    spec_report_written: bool = False


def estimator_consistency_record(
    state: LinearizationState,
) -> Mapping[str, Any] | None:
    """This session's banked estimator disagreement, as flat evidence.

    Built ONLY when the planner's own consistency verdict came back SUSPECT,
    from the plan this candidate's own build returned — no measurement, no
    re-derivation, no second verdict. Taking the state as an argument rather
    than reading it off ``self`` is what makes "this session's" true of one
    candidate rather than of whichever build ran last (#2291 Phase 2b). The
    attribution package turns it into a finding
    (:func:`~jasper.attribution.promotion.promote_level_frame_disagreement`);
    this function owns *what the evidence is*, that one owns *what it means*.
    Nothing here imports attribution, so the flow keeps no dependency on the
    diagnosis layer.

    **Flat, and every value a finite scalar or a string**, because that is
    what :class:`~jasper.attribution.findings.Finding` accepts — nesting would
    be rejected at construction, and rejection is a lost diagnosis. Per-role
    numbers are therefore suffixed with the role, which is also what makes the
    record self-describing to a reader who has never seen this schema.

    **All THREE instruments ride, and now they have a referee.** The record
    carries the fit's per-driver median (``core_level_db_*``), the trim solve's
    per-driver level-match term (``trim_band_average_db_*``), each one's
    distance from the summed owner (``*_delta_db_*``), and the owner's own
    per-role placement (``summed_level_reference_db_*``). Before the
    single-datum-owner migration a reader of this finding was being asked to
    believe a session had proceeded past a gate that would have stopped it;
    now they are being told something narrower and truer — a capture whose
    subordinate estimates disagree with the measurement that placed the pair is
    worth re-taking.

    **What the session DID about it: nothing, and that is the point.** The
    round proceeds on the owner's placement whatever this record says. Its
    predecessor carried ``frame_exclusion_reason`` and per-role
    ``anchor_delta_db_*`` because a disputed frame USED to zero the anchor's
    offsets; no verdict here moves a number, so there is no dB consequence to
    report and those two fields are deleted rather than left reading zero.

    Returns ``None`` when the planner produced no per-role bands to describe —
    unreachable on this path (a suspicion needs an owner and both estimators,
    which needs roles), but a record with no band cannot become a finding, and
    returning ``None`` here says so at the producer instead of failing
    validation two layers away.
    """

    consistency = state.level_consistency
    cores = state.core_level_evidence
    realized = state.realized_level_match
    if consistency is None:
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
    # keys below are what say where each number came from. (The summed capture
    # that owns the level datum DOES cover the gap, which is exactly why it owns
    # it.)
    edges = [
        band for role in cores
        if (band := cores[role].get("band_hz")) is not None
    ]
    lo_edges = [float(band[0]) for band in edges]
    hi_edges = [float(band[1]) for band in edges if band[1] is not None]
    if not lo_edges or not hi_edges:
        return None
    record: dict[str, Any] = {
        "f_lo_hz": min(lo_edges),
        "f_hi_hz": max(hi_edges),
        "worst_delta_db": round(float(consistency.worst_delta_db), 3),
        "tolerance_db": float(consistency.tolerance_db),
        "reason": consistency.reason,
    }
    if realized is not None:
        record.update(
            realized_difference_db=round(float(realized.difference_db), 3),
            realized_tolerance_db=float(realized.tolerance_db),
            realized_level_w_db=round(float(realized.level_w_db), 3),
            realized_level_t_db=round(float(realized.level_t_db), 3),
        )
    owner = state.summed_level_reference_db or {}
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
        if role in owner:
            record[f"summed_level_reference_db_{role}"] = round(
                float(owner[role]), 3
            )
        # How far each SUBORDINATE estimate sat from the owner, per role, in the
        # relative frame the check compares in. Both estimators ride whether or
        # not either one is the reason this record exists: which of them
        # disagreed is the diagnosis, and banking only the worst would drop it.
        if role in consistency.trim_band_delta_db:
            record[f"trim_band_delta_db_{role}"] = round(
                float(consistency.trim_band_delta_db[role]), 3
            )
        if role in consistency.core_level_delta_db:
            record[f"core_level_delta_db_{role}"] = round(
                float(consistency.core_level_delta_db[role]), 3
            )
    return record


def assess_accountability(
    *,
    predicted_sum: Any,
    raw_predicted_sum: Any,
    state: LinearizationState | None,
    grade_prediction: Callable[[Any], Any],
    material_improvement_db: float,
    reason_levels_disagree: str,
    reason_not_an_improvement: str,
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

    # --- the subordinate estimators against the summed owner ---------
    #
    # Runs before item 1 because it is the more specific diagnosis of the
    # same disease: item 1 grades the level the committed trim REALIZES,
    # this grades whether the two per-driver instruments that trim was
    # cross-checked against still agree with the summed capture that placed
    # it. On the 2026-07-27 captures the two per-driver reads sat 10.9-13.1
    # dB apart; PR-L3 fixed that cause, and this is what stops the next one
    # from shipping unremarked.
    #
    # **It banks and proceeds. It never refuses** — the owner's
    # never-nanny ruling (2026-08-17, #2609): a subordinate estimate that
    # disagrees with the owner flags the CAPTURE as retriable; it does not
    # discard the datum and does not stop the session. There is nothing
    # left for it to refuse on, either: the disagreement no longer changes
    # any committed number, because the summed capture owns the placement
    # (:func:`~.intervention.anchor_trims`).
    #
    # **What this replaced, and why the deleted refusal arm was reachable
    # by nothing.** Until #2609 a disagreement past 3.0 dB asked one more
    # question — does the realized-level check pass on the pair about to
    # ship? — and refused when it did not, under item 1's own
    # ``driver_levels_disagree`` code. Every case that arm could refuse,
    # item 1 refuses on its own two branches below, under the same code:
    # the arm's extra reach was the ``realized is None`` case, and the
    # code's own prior finding is that a state carrying no realized verdict
    # carries no disagreement either (a fit that raised part-way yields
    # neither), so the pair never co-occurred. What the arm's deletion
    # therefore removes is a second owner of one refusal, not a stop.
    #
    # The cliff that arm sat on is the located mechanism of the 2026-08-16
    # shortfall round — 3.326 dB against a 3.0 dB bar, +3.79 dB of
    # unrequested tweeter level, and a rolled-back round. #2609's conviction
    # comment carries the full chain.
    if state.level_consistency is not None and state.level_consistency.suspect:
        consistency = state.level_consistency
        realized = state.realized_level_match
        journal.append(GateRecord(
            EVENT_LEVEL_ESTIMATOR_FINDING,
            {
                "reason": consistency.reason,
                "worst_delta_db": round(float(consistency.worst_delta_db), 3),
                "tolerance_db": float(consistency.tolerance_db),
                "summed_level_reference_db": {
                    k: round(float(v), 3)
                    for k, v in (state.summed_level_reference_db or {}).items()
                },
                "core_level_db": dict(state.core_level_evidence),
                "trim_band_average_db": {
                    k: round(float(v), 3)
                    for k, v in state.trim_band_estimate_db.items()
                },
                # Both estimators' distance from the owner, per role. Which
                # one disagreed is the diagnosis; the worst alone would drop
                # it.
                "trim_band_delta_db": {
                    k: round(float(v), 3)
                    for k, v in consistency.trim_band_delta_db.items()
                },
                "core_level_delta_db": {
                    k: round(float(v), 3)
                    for k, v in consistency.core_level_delta_db.items()
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
        finding = estimator_consistency_record(state)
    else:
        finding = None

    # --- item 1: the inter-driver realized level ---------------------
    match = state.realized_level_match
    if match is not None and not match.matched:
        journal.append(GateRecord(
            EVENT_LEVEL_MATCH_REFUSED,
            {
                "reason": reason_levels_disagree,
                "difference_db": round(float(match.difference_db), 3),
                "tolerance_db": match.tolerance_db,
                "level_w_db": round(float(match.level_w_db), 3),
                "level_t_db": round(float(match.level_t_db), 3),
            },
            level=logging.ERROR,
        ))
        return AccountabilityDecision(
            journal=tuple(journal), refusal_reason=reason_levels_disagree,
        )

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
    # that later would be a DIFFERENT instrument from the one this veto
    # refuses on — the two can disagree on a narrow band,
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
        refusal: str | None = None,
    ) -> AccountabilityDecision:
        """One ledger line per session for item 2's gate, on EVERY path.

        Mirrors item 1's realized-level event, which logs whether or not it
        refuses (PR-L4 review S4). A gate that only speaks when it fires
        leaves "it passed" and "it never ran" looking identical in the
        journal — the exact ambiguity this gate exists to remove, and the
        one a field diagnosis of a dark speaker would need first.
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
            },
            level=level,
        ))
        return AccountabilityDecision(
            journal=tuple(journal),
            refusal_reason=refusal,
            finding=None if refusal is not None else finding,
            spec_report=spec_report,
            spec_report_written=True,
        )

    if raw_predicted_sum is None or state.linearized_predicted_sum is None:
        # No fit ran this attempt (ineligible mic tier, or the fit failed
        # into SF2's trims-only fallback), so `predicted_sum` IS
        # `raw_predicted_sum` — the same object. Grading a thing against
        # itself always returns "no improvement", which would refuse every
        # trims-only candidate on the strength of arithmetic rather than
        # evidence. Abstain, loudly — carrying the after-report the hoist
        # above just produced, so the ledger and the wire cannot state
        # different verdicts about one session's one prediction.
        return _settle(LEDGER_NO_LINEARIZATION)
    if after is None:
        return _settle(LEDGER_PREDICTION_UNGRADEABLE)
    if after.overall_passed:
        # A prediction that meets the spec on its own needs no improvement
        # argument, and gating an in-spec result on "how much did it
        # improve" would refuse the flattest speakers hardest.
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
    return _settle(
        reason_not_an_improvement, before=before, improvement_db=improvement_db,
        level=logging.ERROR, refusal=reason_not_an_improvement,
    )
