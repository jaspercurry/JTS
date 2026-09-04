# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a built candidate may be PROPOSED at all.

Three disclosures and no refusals, most-specific-first: the two per-driver
level estimates against each other, then the realized inter-driver level,
then the spec-graded prediction. Order is the decision — each step is a
narrower diagnosis of the one after it, so the earliest true cause is the one
named, journal in emission order. This module DECIDES and does not act; the
session owns every irreversible half. See docs/adr/0003-prediction-gate-frame.md.
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

#: A journal name is a grep contract — tests and the field runbooks both match
#: on them. None of the four describes a refusal; this gate has none.
EVENT_LEVEL_ESTIMATOR_FINDING = "correction.crossover_v2_level_estimator_finding"
EVENT_LEVEL_MATCH_FINDING = "correction.crossover_v2_level_match_finding"
EVENT_PREDICTION_UNGRADEABLE = "correction.crossover_v2_prediction_ungradeable"
EVENT_PREDICTION_GATE = "correction.crossover_v2_prediction_gate"

#: Item 2's ledger vocabulary: every path settles into exactly one of these, so
#: "it passed" and "it never ran" never look the same in the journal.
LEDGER_NO_LINEARIZATION = "no_linearization"
LEDGER_PREDICTION_UNGRADEABLE = "prediction_ungradeable"
LEDGER_PREDICTED_IN_SPEC = "predicted_in_spec"
LEDGER_BASELINE_UNGRADEABLE = "baseline_ungradeable"
LEDGER_RESIDUAL_UNEVALUABLE = "residual_unevaluable"
LEDGER_IMPROVED = "improved"
#: One value for both bars — the fitted class's 0.5 dB and the prescribed
#: class's non-worsening 0.0 dB — which ``required_db`` on the same line tells
#: apart.
LEDGER_NOT_AN_IMPROVEMENT = "not_an_improvement"


@dataclass(frozen=True)
class GateRecord:
    """One log line this gate would have emitted, as data.

    The payload is held EXACTLY as built, unlike
    :class:`~.plan_assembly.JournalRecord`, whose ``detached_json`` normalizes
    containers and would render ``core_level_db``'s per-role ``band_hz`` tuples
    as lists.
    """

    event: str
    fields: Mapping[str, Any]
    level: int = logging.INFO


@dataclass(frozen=True)
class AccountabilityDecision:
    """What the gate decided, and everything the caller must do about it.

    ``journal`` is in emission order. ``finding`` is the banked record when
    EITHER level check had something to report, and accompanies a round that
    proceeds. ``spec_report`` is the stash the host persists; ``None`` means
    there was no summed model to grade. There is no ``refusal_reason``: this
    gate refuses nothing (`docs/measurement-loop-doctrine.md`).
    """

    journal: tuple[GateRecord, ...] = ()
    finding: Mapping[str, Any] | None = None
    #: A ``dict`` rather than a ``Mapping``: this IS the host's stash, which the
    #: host owns and the review screen's persistence later reads back.
    spec_report: dict[str, Any] | None = None


def level_frame_record(
    state: LinearizationState,
) -> Mapping[str, Any] | None:
    """This session's banked level-frame reservation, as flat evidence.

    Built when EITHER level check has something to report, from the plan this
    candidate's own build returned; no measurement and no second verdict. Flat,
    with every value a finite scalar or a string, because that is what
    :class:`~jasper.attribution.findings.Finding` accepts — nesting is rejected
    at construction and a rejected record is a lost diagnosis.

    ``None`` when the condition this record is ABOUT cannot name a band: an
    estimator disagreement with no per-role core spans. A realized-only record
    falls back to the realized verdict's own mirrored half-bands about Fc.
    """

    consistency = state.level_consistency
    cores = state.core_level_evidence
    realized = state.realized_level_match
    definitions_differ = consistency is not None and consistency.differs
    realized_suspect = realized is not None and not realized.matched
    if not definitions_differ and not realized_suspect:
        return None
    # The band this finding is ABOUT: the CORE spans the level reads were taken
    # over, unioned across roles. Not the radiating bands — a high-pass branch
    # radiates to infinity, so a radiating union has no upper edge. The union is
    # an OUTER hull and spans the handoff gap between the two cores, which is
    # where the relationship between the drivers lives.
    edges = [
        band for role in cores
        if (band := cores[role].get("band_hz")) is not None
    ]
    if not edges and realized is not None and not definitions_differ:
        # The realized instrument's OWN mirrored half-bands about Fc. Guarded
        # on the realized condition, not on the bands' absence alone: a
        # finding's band must be the span its own reason was measured over.
        edges = [realized.woofer_band_hz, realized.tweeter_band_hz]
    lo_edges = [float(band[0]) for band in edges]
    hi_edges = [float(band[1]) for band in edges if band[1] is not None]
    if not lo_edges or not hi_edges:
        return None
    record: dict[str, Any] = {
        "f_lo_hz": min(lo_edges),
        "f_hi_hz": max(hi_edges),
        # WHICH axis every level on this record was read on: where woofer
        # beaming and horn directivity mismatch, on-axis, listening-window and
        # power-response levels differ and there is no single correct one.
        "level_match_axis": LEVEL_MATCH_AXIS,
    }
    # ONE reason field, and the estimator disagreement wins when both fire: it
    # is the more specific diagnosis. Both sub-verdicts' numbers ride
    # unconditionally, so nothing is lost to the precedence.
    if consistency is not None and consistency.differs:
        record["reason"] = consistency.reason
    else:
        record["reason"] = REALIZED_LEVEL_SUSPECT_REASON
    # PREFIXED, like every other key belonging to one of the two instruments:
    # the record carries both, so an unqualified ``worst_delta_db`` could not be
    # attributed.
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
        # What the two per-driver estimators made of EACH OTHER, per role: a
        # symmetric distance with no owner term. Which one disagreed is the
        # diagnosis, so banking only the worst would drop it.
        if consistency is not None and role in consistency.estimator_delta_db:
            record[f"estimator_delta_db_{role}"] = round(
                float(consistency.estimator_delta_db[role]), 3
            )
    # THE ATTRIBUTION: the MEASURE ripple polish moves one trim off the
    # level-matching solve and that excursion passes straight through to the
    # realized inter-driver level error. Its OWN loop, keyed on its own mapping:
    # the polish's roles are the request's, while ``cores`` carries only roles
    # the fit produced a median for.
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

    ``state`` is the candidate's own planner output; ``None`` is the same
    evidence state as an ineligible session and takes the same path.
    ``grade_prediction`` is the spec evaluator, injected rather than imported.
    It is called AT MOST twice and the second call is conditional, so the caller
    may NOT pre-compute both reports — grading the baseline unconditionally
    would run the evaluator, and emit its diagnostics, on a path that never
    grades it.
    """
    journal: list[GateRecord] = []
    state = state if state is not None else LinearizationState()

    # --- the two per-driver level estimates against each other -------
    #
    # Before item 1 because it is the more specific diagnosis of the same
    # disease. It banks and proceeds, and never refuses: the disagreement
    # changes no committed number, since the raw measured trim owns the
    # placement (:func:`~.intervention.anchor_trims`).
    if state.level_consistency is not None and state.level_consistency.differs:
        consistency = state.level_consistency
        realized = state.realized_level_match
        journal.append(GateRecord(
            EVENT_LEVEL_ESTIMATOR_FINDING,
            {
                "reason": consistency.reason,
                "worst_delta_db": round(float(consistency.worst_delta_db), 3),
                "tolerance_db": float(consistency.tolerance_db),
                # WHICH axis both levels were read on: where beaming and horn
                # directivity mismatch there is no single correct level.
                "matched_axis": consistency.matched_axis,
                "core_level_db": dict(state.core_level_evidence),
                "trim_band_average_db": {
                    k: round(float(v), 3)
                    for k, v in state.trim_band_estimate_db.items()
                },
                # Symmetric, with no owner term. Which estimator disagreed is
                # the diagnosis; the worst alone would drop it.
                "estimator_delta_db": {
                    k: round(float(v), 3)
                    for k, v in consistency.estimator_delta_db.items()
                },
                # The outcome check, for the reader deciding how much to care;
                # it decides nothing here.
                "realized_difference_db": (
                    None if realized is None
                    else round(float(realized.difference_db), 3)
                ),
            },
            level=logging.WARNING,
        ))
    # The one fact item 2's ledger borrows from this gate. Same name and
    # tri-state as the giveback event's ``level_definitions_differ``
    # (:func:`~.intervention.plan_linearization`), so one field name greps
    # across the round. The BOOLEAN and not the magnitude, which has one owner
    # above; what the flag adds is the LINK to the verdict built on top of it.
    definitions_differ = (
        None if state.level_consistency is None
        else bool(state.level_consistency.differs)
    )

    # --- item 1: the inter-driver realized level ---------------------
    #
    # It banks and proceeds, and never refuses: a QUALITY check naming no
    # component-damage mechanism discloses rather than blocking
    # (`docs/measurement-loop-doctrine.md` §3-§5). The number it grades is near
    # enough the MEASURE ripple polish's trim excursion, whose admission
    # `program_analysis` couples to this same tolerance, so `polish_delta_db_*`
    # rides the banked record for a reader to check that.
    match = state.realized_level_match
    if match is not None and not match.matched:
        journal.append(GateRecord(
            EVENT_LEVEL_MATCH_FINDING,
            {
                # The FINDING vocabulary — see
                # `intervention.REALIZED_LEVEL_SUSPECT_REASON`.
                "reason": REALIZED_LEVEL_SUSPECT_REASON,
                "difference_db": round(float(match.difference_db), 3),
                "tolerance_db": match.tolerance_db,
                "level_w_db": round(float(match.level_w_db), 3),
                "level_t_db": round(float(match.level_t_db), 3),
                # The attribution, on the line as well as in the record, so a
                # field reader need not open the finding store. ``None`` when
                # no plan measured it.
                "polish_delta_db": (
                    {k: round(float(v), 3) for k, v in state.polish_delta_db.items()}
                    or None
                ),
            },
            level=logging.WARNING,
        ))
    # Built from the state, so it covers whichever of the two checks fired.
    finding = level_frame_record(state)

    # --- item 2: spec-grade the prediction ---------------------------
    #
    # BEFORE and AFTER on the SAME instrument: the raw pre-fit and the
    # linearized two-branch prediction through the identical evaluator, so the
    # room is in neither term and cancels
    # (docs/adr/0003-prediction-gate-frame.md). Graded ONCE, here, the last
    # place the full-resolution `(freqs, magnitudes)` tuple exists — the durable
    # state keeps `_decimate_sum`'s 512-point block average, a drawing rather
    # than the instrument. Hoisted ABOVE the trims-only abstain below, which
    # still commits trims and so still has a gradeable prediction.
    after = grade_prediction(predicted_sum)
    # ``why`` separates the two causes, which have different remedies: a
    # prediction never built, or one the evaluator refused.
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

        A gate that only speaks when it fires leaves "it passed" and "it never
        ran" identical in the journal.
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
        # No fit ran this attempt, so `predicted_sum` IS `raw_predicted_sum` —
        # the same object, and grading a thing against itself always returns
        # "no improvement". Abstain, carrying the after-report the hoist above
        # produced so the ledger and the wire cannot disagree.
        return _settle(LEDGER_NO_LINEARIZATION)
    if after is None:
        return _settle(LEDGER_PREDICTION_UNGRADEABLE)
    if after.overall_passed:
        # A prediction that meets the spec needs no improvement argument, and
        # judging an in-spec result on improvement reads flat speakers worst.
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
    # WARNING and not ERROR: nothing failed. A prediction only recommends.
    return _settle(
        LEDGER_NOT_AN_IMPROVEMENT, before=before, improvement_db=improvement_db,
        level=logging.WARNING,
    )
