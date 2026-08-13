# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a built candidate may be PROPOSED at all (#2291 Phase 5a-v).

The gate that runs after a candidate is built and before anything downstream
can apply it.  Three assertions, most-specific-first: PR-L5's shared level
frame, then PR-L4's item 1 (the realized inter-driver level) and item 2 (the
spec-graded prediction).  Refusing here means no candidate is ever stashed or
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

* **The two thresholds.**  ``level_frame_tolerance_db`` and
  ``material_improvement_db`` arrive as arguments.  Both carry long
  field-evidence provenance in the flow, and the item-2 threshold has a second
  in-flow reader in the prediction ledger's ``required_db`` field.  Moving the
  constants here while that reader stays there would create exactly the
  cross-module twin 5a-v just closed for the candidate-required band.  They
  move when their other reader does.
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
    "EVENT_LEVEL_FRAME_FINDING",
    "EVENT_LEVEL_FRAME_REFUSED",
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
    "level_frame_finding_record",
]

#: The five event names this gate emits. Named constants rather than literals
#: because a journal name is a grep contract — ``test_crossover_v2_*`` and the
#: field runbooks both match on them, so a rename is a breaking change that
#: should be visible as one.
EVENT_LEVEL_FRAME_FINDING = "correction.crossover_v2_level_frame_finding"
EVENT_LEVEL_FRAME_REFUSED = "correction.crossover_v2_level_frame_refused"
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


def level_frame_finding_record(
    state: LinearizationState, *, tolerance_db: float,
) -> Mapping[str, Any] | None:
    """This session's banked frame disagreement, as flat evidence (#1866).

    Built ONLY on the finding+proceed path, from the plan this candidate's
    own build returned — no measurement, no re-derivation, no second
    verdict. Taking the state as an argument rather than reading it off
    ``self`` is what makes "this session's" true of one candidate rather
    than of whichever build ran last (#2291 Phase 2b). The
    attribution package turns it into an M7 finding
    (:func:`~jasper.attribution.promotion.promote_level_frame_disagreement`);
    this function owns *what the evidence is*, that one owns *what it means*.
    Nothing here imports attribution, so the flow keeps no dependency on
    the diagnosis layer.

    **Flat, and every value a finite scalar or a string**, because that is
    what :class:`~jasper.attribution.findings.Finding` accepts — nesting
    would be rejected at construction, and rejection is a lost diagnosis.
    Per-role numbers are therefore suffixed with the role, which is also
    what makes the record self-describing to a reader who has never seen
    this schema.

    **All THREE instruments ride, not just the two that disagreed.** A
    reader of this finding is being asked to believe that a session
    proceeded past a gate that would have stopped it, so the record has to
    carry the whole basis for that: the fit's per-driver median
    (``core_level_db_*``), the trim solve's per-driver level-match term
    (``trim_band_average_db_*``), the reconciled per-role offset that IS
    their disagreement, and the realized-level check whose PASS is what
    let the session proceed. Banking only the first two would record the
    argument and drop the reason it was allowed to stand.

    Returns ``None`` when the frame produced no per-role bands to describe
    — unreachable on this path (the gate fired on a frame that had roles),
    but a record with no band cannot become a finding, and returning
    ``None`` here says so at the producer instead of failing validation
    two layers away.
    """

    frame = state.level_frame
    cores = state.level_frame_cores
    realized = state.realized_level_match
    # The band this finding is ABOUT: the span the two level reads were
    # actually taken over, unioned across roles. Deliberately the CORE
    # bands and not the radiating ones — a high-pass branch radiates to
    # infinity, so a radiating union has no upper edge, while the core
    # band is exactly the finite span each median was computed on.
    #
    # **The union is an OUTER hull, and it spans a gap neither median
    # read** — on the session fixture the woofer's core stops at 1255.8
    # Hz and the tweeter's starts at 2020.0, so 1255.8-2020.0 Hz is inside
    # the finding's band and inside no measurement. That is the right shape
    # rather than a rounding of it: this finding is about the RELATIONSHIP
    # between two drivers, which lives in the handoff sitting in that gap,
    # and a band stated as two disjoint intervals would say the finding is
    # about two places when it is about one. It is not, and must not be
    # read as, a claim that anything was measured in the gap — the
    # per-role ``core_band_*`` keys below are what say where each number
    # actually came from.
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
        "disagreement_db": round(
            float(state.level_frame_disagreement_db), 3
        ),
        "tolerance_db": float(tolerance_db),
        "reference_role": frame.reference_role if frame is not None else "",
        "system_level_db": (
            round(float(frame.system_level_db), 3)
            if frame is not None else None
        ),
    }
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
        if role in state.level_frame_trims:
            record[f"trim_band_average_db_{role}"] = round(
                float(state.level_frame_trims[role]), 3
            )
        if frame is not None and role in frame.offset_db:
            record[f"frame_offset_db_{role}"] = round(
                float(frame.offset_db[role]), 3
            )
    return record


def assess_accountability(
    *,
    predicted_sum: Any,
    raw_predicted_sum: Any,
    state: LinearizationState | None,
    grade_prediction: Callable[[Any], Any],
    level_frame_tolerance_db: float,
    material_improvement_db: float,
    reason_levels_disagree: str,
    reason_not_an_improvement: str,
) -> AccountabilityDecision:
    """The three accountability assertions, as a decision rather than an act.

    ``state`` is the candidate's own planner output.  ``None`` means no build
    produced one, which is the same evidence state as an ineligible session and
    takes the same path: no frame to disagree, no realized verdict to fail, and
    item 2's abstain below.

    ``grade_prediction`` is the spec evaluator, injected rather than imported.
    It is called AT MOST twice and the second call is conditional, which is why
    the caller may not pre-compute both reports: grading the baseline
    unconditionally would run an evaluator — and emit its own diagnostics — on
    a path that today never grades it.
    """
    journal: list[GateRecord] = []
    state = state if state is not None else LinearizationState()

    # --- PR-L5: the two level FRAMES agree ---------------------------
    #
    # Runs before item 1 because it is the more specific diagnosis of the
    # same disease: item 1 grades the level the committed trim REALIZES,
    # this grades whether the two instruments that trim was derived from
    # still agree about where the drivers sit. On the 2026-07-27 captures
    # the disagreement was 10.9-13.1 dB; PR-L3 fixed its cause, and this
    # is what stops the next cause from shipping silently.
    #
    # It refuses under PR-L4's own ``driver_levels_disagree`` code, not a
    # new one: the household's remedy is identical (re-check sensitivity
    # and the pad in speaker setup) and one consistent sentence beats two
    # near-duplicates. The journal separates them by ``event=``.
    #
    # **The refusal is no longer unconditional (owner ruling, #1866,
    # 2026-07-30).** A disagreement over tolerance now asks ONE more
    # question before it stops the session: does the realized-level check
    # pass on the pair this session is about to ship? If it does, the
    # session banks the disagreement as a finding and PROCEEDS; the hard
    # refusal remains only when the realized check ALSO fails. Why the
    # ruling went that way, in one line: #1929 removed a structural bias
    # from one estimator, it did not make the two agree, and what is left
    # refuses healthy speakers — a pair identical by construction reads
    # 0.910 dB apart and ordinary woofer passband tilt adds ~1.33 dB per
    # dB/octave, so a −2 dB/oct woofer refuses at 3.574 while the realized
    # instrument reads 1.41 and passes. The field case is the 2026-07-30
    # session: 3.2307 dB under the banded estimator, realized −0.247,
    # predicted on-axis residual 3.106 → 1.333 dB (all recorded on #1870).
    # Refusing that is a false negative on a good tune, and the diagnosis
    # the gate already computed reached no artifact at all.
    #
    # **What "proceeds" commits, stated precisely — because the obvious
    # reading is wrong.** The ruling's own wording is "proceeds on the
    # near-Fc anchor (the trim solve)", and that describes an outcome the
    # code does not produce. Proceeding changes NOTHING about the trims:
    # the fit commits the anchor it always computed, and in
    # ``anchor_base + giveback + level_frame_offset`` the trim term
    # CANCELS — ``offset = system − trim − core``, leaving
    # ``giveback + system − core`` (the cancellation is derived in
    # ``anchor_base_db``'s own comment). So the committed inter-driver
    # placement is set by the CORE-MEDIAN frame — the disputed estimator —
    # not by the trim solve. On the session fixture: committed −0.674,
    # which is the core-median value to 4 dp; anchoring on the trim solve's
    # placement instead would give +2.535; the two differ by 3.209, exactly
    # the banked disagreement, which is not a coincidence but the identity
    # ``placement_trim − placement_core = −offset``.
    #
    # The honest description of this branch is therefore: **the pipeline
    # commits the anchor it always computed (which embeds the disputed
    # estimator); proceeding is the same tune, not refused; the realized
    # check gates the OUTCOME rather than selecting an estimator.** That is
    # a weaker claim than "we proceed on the corroborated estimator" and it
    # is the true one — the realized check's pass is evidence that the
    # shipped pair is level, not evidence about which estimator was right.
    #
    # **RATIFIED.** This description differs from the ruling's original
    # wording, so it was put to the owner rather than merged under the
    # inverted account; the owner confirmed it on 2026-07-30 (#1866 comment
    # 5137494519) as "the ruling's operative form". The two phrases above
    # are retired: anything still asserting them is describing a mechanism
    # this code does not implement.
    #
    # **What the realized check is, and is not.** It is a CLOSED-LOOP
    # check, not cross-band arbitration: its own docstring says "One
    # estimator, not a second opinion" — the levels come from
    # ``solve_branch_trims`` on the TRIMMED pair, the same power-band
    # average over the same ``branch_level_bands_hz`` halves that set the
    # trim. So it cannot referee the two frames against each other, and
    # nothing here should read as if it did. What it IS: independent of the
    # fit's core median (different inputs — the post-fit linearized
    # branches — different band, different statistic), and non-vacuous —
    # it fails on a −6 dB/oct woofer where the frame gate also fails. It
    # answers one question, the useful one: did the pair we are about to
    # ship end up level?
    #
    # **Ordering: nothing moved, and nothing needed to.** The realized
    # verdict this branch consults is item 1's own
    # ``state.realized_level_match``, which reads later in this
    # function but was computed earlier in the build — the planner returns
    # the frame and the realized match on one plan, complete before the
    # build calls this. There is no reordering here
    # and no second computation: item 1 keeps its own gate, its own event,
    # and its own refusal below, and every OTHER gate's semantics are
    # byte-identical to before this change.
    #
    # ``match is None`` (no fit ran) falls to the refusal, and that is the
    # fail-closed direction rather than an oversight: with no realized
    # verdict there is no outcome check to gate on, so the ruling's
    # precondition is unmet. In practice it is unreachable from here — the
    # frame is only non-zero when a fit completed, and a fit that raised
    # part-way yields a state carrying neither — but a future path that
    # separates them must refuse, not proceed.
    if state.level_frame_disagreement_db > level_frame_tolerance_db:
        frame = state.level_frame
        realized = state.realized_level_match
        banked = realized is not None and realized.matched
        journal.append(GateRecord(
            EVENT_LEVEL_FRAME_FINDING if banked else EVENT_LEVEL_FRAME_REFUSED,
            {
                "reason": "" if banked else reason_levels_disagree,
                "disagreement_db": round(
                    float(state.level_frame_disagreement_db), 3
                ),
                "tolerance_db": level_frame_tolerance_db,
                "system_level_db": (
                    round(float(frame.system_level_db), 3)
                    if frame is not None else None
                ),
                "reference_role": (
                    frame.reference_role if frame is not None else ""
                ),
                "offset_db": (
                    {k: round(float(v), 3) for k, v in frame.offset_db.items()}
                    if frame is not None else {}
                ),
                "core_level_db": dict(state.level_frame_cores),
                # The two fields the finding path adds, and only it: the OTHER
                # estimator's per-role level-match term, and the realized
                # verdict that decided which way this went. Both are ``None``/
                # ``{}`` on the refusal arm so that line stays what #1934
                # shipped.
                "trim_band_average_db": (
                    {k: round(float(v), 3)
                     for k, v in state.level_frame_trims.items()}
                    if banked else {}
                ),
                "realized_difference_db": (
                    round(float(realized.difference_db), 3)
                    if banked and realized is not None else None
                ),
            },
            level=logging.WARNING if banked else logging.ERROR,
        ))
        if not banked:
            return AccountabilityDecision(
                journal=tuple(journal), refusal_reason=reason_levels_disagree,
            )
        finding = level_frame_finding_record(
            state, tolerance_db=level_frame_tolerance_db,
        )
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
