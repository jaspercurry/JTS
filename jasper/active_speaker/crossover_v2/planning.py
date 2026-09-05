# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE candidate, assembled — and what its linearization produced (#2291).

Owns the build: the eligibility gate, the planner request this candidate's
sections imply, the cloud evidence its envelope consumed, and the emitted
``MeasuredCrossoverCandidate``. Two rules: the crossover corner is derived
from the candidate's own sections, never a session Fc; and this module logs
nothing itself except ONE guarded ``log_event`` call — the guard for a
``journal`` port that raised being handed a record (#2361), the one channel
a broken port cannot also take down.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    ProgramAnalysis,
    polarity_label,
)
from jasper.log_event import log_event

from ..branch_chain import CrossoverSection, branch_headroom_db, sections_by_role
from ..linearization_fit import linearization_filters_by_role
from .candidates import CloudFitEvidence, LinearizationState
from .contracts import CandidateAcousticContext
from .driver_prescription import (
    LINEARIZATION_CANDIDATE_FIELD,
    DriverPrescription,
    driver_prescription_to_candidate_fields,
)
from .intervention import (
    LINEARIZATION_MIN_PAIRED_OCCURRENCES,
    driver_response_by_role,
    request_from_analysis,
)
from .plan_assembly import LinearizationPlan, compose_linearized_prediction
from .journey import PHASE_CLOUD_MEASURE, PHASE_MEASURE

#: Reached for in exactly one place, :func:`build_candidate`'s journal guard.
logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_FIT_FAILED",
    "EVENT_FIT_FAILED_JOURNAL_DROPPED",
    "FailureRecord",
    "alignment_to_candidate_fields",
    "analysis_json",
    "applied_profile_delay_us",
    "build_candidate",
    "exclusion_evidence_json",
    "ineligible_reason",
    "plan_for_candidate",
]

#: The one event name this module discloses through its ``journal`` port.
EVENT_FIT_FAILED = "correction.crossover_v2_linearization_fit_failed"

#: Said INSTEAD of :data:`EVENT_FIT_FAILED` when the ``journal`` port itself
#: raises carrying that record (#2361). Must stay substring-clean of
#: :data:`EVENT_FIT_FAILED` in both directions: consumers match events by bare
#: substring against ``caplog.text``.
EVENT_FIT_FAILED_JOURNAL_DROPPED = (
    "correction.crossover_v2_linearization_fit_journal_dropped"
)

#: What :func:`build_candidate`'s ``journal`` call is guarded against.
#: Enumerated rather than a blind ``except Exception`` (ruff BLE; the frozen
#: broad-except budget). ``OSError`` is in the set because the port is a
#: logging delegate, and a handler with nowhere to write raises exactly that.
_JOURNAL_ERRORS = (
    ArithmeticError,
    AttributeError,
    IndexError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


@dataclass(frozen=True)
class FailureRecord:
    """One log line this module would have emitted from inside an ``except``.

    ``logging`` resolves ``exc_info=True`` against ``sys.exc_info()`` at
    LogRecord-creation time, so a record emitted after the handler exits would
    render no traceback. Carrying the live exception makes the deferred
    emission identical: ``logging`` expands a ``BaseException`` to
    ``(type(e), e, e.__traceback__)``.
    """

    event: str
    fields: Mapping[str, Any]
    level: int = logging.WARNING
    #: The caught exception, or ``None`` for a record with no stack to carry.
    exc_info: BaseException | None = field(default=None, repr=False)


def alignment_to_candidate_fields(
    analysis: ProgramAnalysis, *, roles: Sequence[str],
) -> tuple[float | None, str | None, str | None]:
    """Map a MEASURE ``AlignmentEstimate`` to ``(delay_us, delay_role, polarity)``.

    Sign contract (design §5.6.5): ``analysis.delay_us`` is
    ``(D_woofer − D_tweeter)``, so positive ⇒ the TWEETER branch is delayed,
    negative ⇒ the woofer is. ``MeasuredCrossoverAlignment`` wants a
    non-negative magnitude plus the delayed role, so the sign folds into the
    role choice. ``(None, None, None)`` when no alignment is trustworthy or
    there is a lone branch — the candidate falls back to a trims-only apply.
    """
    from jasper.active_speaker.crossover_alignment import (
        POLARITY_INVERT,
        POLARITY_KEEP,
    )

    est = analysis.alignment
    if est is None or est.status != ALIGNMENT_OK or len(roles) < 2:
        return None, None, None
    woofer_role, tweeter_role = roles[0], roles[1]
    delay_us = float(est.delay_us)
    if delay_us >= 0.0:
        role, magnitude = tweeter_role, delay_us
    else:
        role, magnitude = woofer_role, -delay_us
    polarity = POLARITY_INVERT if est.polarity == "inverted" else POLARITY_KEEP
    return magnitude, role, polarity


def applied_profile_delay_us(
    applied_profile: Mapping[str, Any] | None,
    *,
    woofer_role: str,
    tweeter_role: str,
) -> float | None:
    """The inter-driver delay the APPLIED graph carries, in the analysis frame.

    The inverse of :func:`alignment_to_candidate_fields`; one module owns both
    directions of the sign fold. Its one consumer (#2617) is
    ``MeasurementPriors.applied_alignment``. An absent ``delay_ms`` is a ZERO,
    not a gap — the profile records a magnitude only on the delayed role
    (``MeasuredCrossoverCandidate.driver_corrections``). ``None`` — never a
    guessed ``0.0`` — when there is no applied profile, when the authoritative
    corrections name fewer than both roles, or when the result is non-finite.
    """
    from jasper.active_speaker.baseline_profile import profile_driver_corrections

    corrections = profile_driver_corrections(applied_profile)
    woofer, tweeter = corrections.get(woofer_role), corrections.get(tweeter_role)
    if not isinstance(woofer, Mapping) or not isinstance(tweeter, Mapping):
        return None
    try:
        delay_us = 1000.0 * (
            float(tweeter.get("delay_ms") or 0.0)
            - float(woofer.get("delay_ms") or 0.0)
        )
    except (TypeError, ValueError):
        return None
    return delay_us if math.isfinite(delay_us) else None


def analysis_json(analysis: ProgramAnalysis) -> dict[str, Any]:
    """Compact JSON-safe evidence core for the measured candidate fingerprint.

    The W4 candidate freezes ``analysis`` as exact JSON data, so only the
    scalar verdicts travel — never the numpy response arrays. Enough to identify
    the exact measurement that authorized the candidate (§5.6/§5.8).
    """
    drift = analysis.drift
    align = analysis.alignment
    cand = analysis.candidate
    return {
        "schema_version": 1,
        "kind": "jts_program_analysis_evidence",
        "program_id": analysis.program_id,
        "epsilon_ppm": round(float(drift.epsilon_ppm), 3) if drift else None,
        "glitch_detected": bool(analysis.glitch_detected),
        "delay_us": round(float(align.delay_us), 3) if align else None,
        "alignment_seed_delay_us": (
            round(float(align.seed_delay_us), 3)
            if align and align.seed_delay_us is not None else None
        ),
        "polarity": align.polarity if align else None,
        # The committed polarity above is a SELECTION, not correlation's own
        # answer (#2598), so what chose it is frozen beside it.
        "alignment_objective": cand.alignment_objective if cand else None,
        "seed_polarity": (
            None if cand is None or cand.seed_polarity_sign is None
            else polarity_label(int(cand.seed_polarity_sign))
        ),
        "polarity_agrees_with_sum": (
            align.polarity_agrees_with_sum if align else None
        ),
        # Whether anything MEASURED the polarity at all — the two fields above
        # cannot answer that between them, and the household row words an
        # operator's instruction differently from a measured result.
        "polarity_pinned": bool(cand.polarity_pinned) if cand else False,
        # The mode this names is magnitude-flat and time-wrong, so an on-axis
        # VERIFY cannot contradict it: the receipt is the only place a later
        # reader can find it (#2607 S2).
        "left_anchor_lobe": bool(cand.left_anchor_lobe) if cand else None,
        "alignment_confidence": round(float(align.confidence), 4) if align else None,
        "alignment_confidence_source": align.confidence_source if align else None,
        "trim_db": (
            {k: round(float(v), 4) for k, v in cand.trim_db.items()} if cand else None
        ),
        # #1667: the band-average seed the ripple-optimal solve started from.
        # Evidence only, and kept even when it coincides with ``trim_db``.
        "trim_band_average_db": (
            {k: round(float(v), 4) for k, v in cand.trim_band_average_db.items()}
            if cand and cand.trim_band_average_db is not None else None
        ),
        # Why the two maps above coincide, when they do: the only field that
        # says a flatness answer was computed and discarded, and by how much.
        # ``None`` on the two paths that discarded nothing.
        "ripple_polish_rejected_delta_db": (
            round(float(cand.ripple_polish_rejected_delta_db), 4)
            if cand and cand.ripple_polish_rejected_delta_db is not None else None
        ),
        "predicted_ripple_db": (
            round(float(cand.predicted_ripple_db), 4) if cand else None
        ),
        "alignment_seed_ripple_db": (
            round(float(cand.alignment_seed_ripple_db), 4)
            if cand and cand.alignment_seed_ripple_db is not None else None
        ),
        "flatness_improvement_db": (
            round(float(cand.flatness_improvement_db), 4)
            if cand and cand.flatness_improvement_db is not None else None
        ),
        "anchor_delay_us": (
            round(float(cand.anchor_delay_us), 3)
            if cand and cand.anchor_delay_us is not None else None
        ),
        "snap_delta_us": (
            round(float(cand.snap_delta_us), 3)
            if cand and cand.snap_delta_us is not None else None
        ),
        "snap_found": bool(cand.snap_found) if cand else None,
    }


def ineligible_reason(
    analysis: ProgramAnalysis, *, roles: Sequence[str],
) -> str | None:
    """HARD GATE for the Layer-1a fit path, as a named reason or ``None``.

    Eligible means a reference-tier mic AND every driver paired
    N >= :data:`~.intervention.LINEARIZATION_MIN_PAIRED_OCCURRENCES` in-capture
    occurrences. Anything else falls back to the plain trims-only candidate.
    """
    if analysis.mic_tier != "reference":
        return "ineligible_mic_tier"
    for role in roles:
        response = driver_response_by_role(analysis, role)
        if (
            response is None
            or 1 + len(response.repeat_responses)
            < LINEARIZATION_MIN_PAIRED_OCCURRENCES
        ):
            return "ineligible_repeats"
    return None


def exclusion_evidence_json(
    cloud: CloudFitEvidence, *, cloud_result: Mapping[str, Any],
) -> dict[str, Any]:
    """The fit's cloud inputs, as the candidate's exclusion reason of record.

    Enough that a reader holding only ``candidate.json`` can re-derive
    ``spatial_exclusion_limit`` and ``position_stability_limit``.
    ``cloud_result`` must be read by the caller at CALL time: only its CURRENT
    value describes the cloud retained at confirm, since a retake re-closes the
    group (#1872). ``cloud_measure.json``'s own copy can lag it — the evidence
    store's ``publish_cloud`` write is a per-phase singleton, and that gap is
    accepted (forensic artifact vs. product). ``validity_floor_hz`` and
    ``gated_spec_curve`` (#1787) ride here for the room layer, not the fit;
    the curve adds roughly 15-20 KB of JSON per candidate (<=512 points), so
    decimate at this boundary rather than dropping the field if it grows.
    """
    registry = cloud_result.get("null_registry")
    floor = cloud_result.get("validity_floor_hz")
    curve = cloud_result.get("curve")
    return {
        "phase": PHASE_CLOUD_MEASURE,
        "excluded_bands_hz": [list(band) for band in cloud.excluded_bands_hz],
        "n_positions": cloud.n_positions,
        "band_spread": [
            {
                "center_hz": float(band.center_hz),
                "f_lo": float(band.f_lo),
                "f_hi": float(band.f_hi),
                "sigma_db": float(band.sigma_db),
                "max_sigma_db": float(band.max_sigma_db),
                "n_bins": int(band.n_bins),
            }
            for band in cloud.band_spread
        ],
        "null_registry": dict(registry) if isinstance(registry, Mapping) else {},
        # ``None`` is "the floor is unverified", never "the floor is 0 Hz".
        "validity_floor_hz": (
            float(floor)
            if isinstance(floor, (int, float)) and math.isfinite(float(floor))
            else None
        ),
        "gated_spec_curve": (
            {
                "freqs_hz": [float(v) for v in curve.get("freqs_hz", ())],
                "magnitude_db": [float(v) for v in curve.get("magnitude_db", ())],
            }
            if isinstance(curve, Mapping)
            else {}
        ),
    }


def _sections_for_candidate(
    candidate_sections: Mapping[str, Sequence[CrossoverSection]] | None,
    preset: Any,
) -> dict[str, tuple[CrossoverSection, ...]]:
    """Role -> the Linkwitz-Riley sections THIS candidate's branch runs through.

    ``candidate_sections`` when a caller overrides, the preset's own crossover
    regions otherwise. One derivation because two consumers must describe the
    same emitted graph: the planner bounds its fit band and charges its
    headroom with it, and :func:`build_candidate` charges a PRESCRIBED branch's
    disclosure with it (#2759).
    """
    if candidate_sections is not None:
        return {role: tuple(regions) for role, regions in candidate_sections.items()}
    return sections_by_role(getattr(preset, "crossover_regions", ()) or ())


def plan_for_candidate(
    analysis: ProgramAnalysis,
    cand: Any,
    cloud: CloudFitEvidence | None,
    *,
    candidate_sections: Mapping[str, Sequence[CrossoverSection]] | None = None,
    preset: Any,
    program_for_phase: Callable[[str], Any],
    roles: Sequence[str],
    driver_class_by_role: Mapping[str, str],
    post_apply_verifies: bool,
    cloud_phase_planned: bool,
    plan_linearization: Callable[..., LinearizationPlan],
    journal: Callable[[Any], None] | None = None,
) -> LinearizationPlan:
    """Assemble ONE candidate's planner request and run the pure planner.

    The corner comes from the sections this candidate is realized with; the
    session's ``_fc_hz`` is not read and cannot be reached from here. A split
    or empty section set raises (``CandidateFcDisagreementError`` /
    ``NoCrossoverSectionsError``, both ``ValueError`` subclasses), which
    :func:`build_candidate`'s SF2 arm degrades to the trims-only lane.

    Only called after :func:`ineligible_reason` returns ``None``; the planner
    assumes eligibility. ``program_for_phase`` is injected rather than resolved
    by the caller because it can raise (before the CHECK gain solve there is no
    MEASURE program) and must do so AFTER the section set has been judged.
    ``plan_linearization`` is injected and deliberately NOT imported here, so
    a substitution of the flow module's own name still binds (#2354). The
    ``journal_dropped`` notice on the returned plan is the HOST's to say: it
    reports on the journal port, so it cannot be routed through it.
    """
    context = CandidateAcousticContext.for_candidate(
        _sections_for_candidate(candidate_sections, preset), roles=roles,
    )
    measure_program = program_for_phase(PHASE_MEASURE)
    # The MEASURE program keeps its ``_w``/``_t`` segment spelling whatever the
    # roles are called, and a 1-way program carries only the first.
    excited_band_hz: dict[str, tuple[float, float]] = {}
    for role, segment_id in zip(roles, ("sweep_w", "sweep_t")):
        segment = measure_program.segment(segment_id)
        # ``f1_hz``/``f2_hz`` are ``float | None`` on the general segment shape;
        # ``__post_init__`` guarantees neither is None on a KIND_SWEEP stimulus.
        assert segment.f1_hz is not None and segment.f2_hz is not None
        excited_band_hz[role] = (segment.f1_hz, segment.f2_hz)
    request = request_from_analysis(
        analysis, cand,
        context=context,
        roles=roles,
        excited_band_hz=excited_band_hz,
        driver_class_by_role=driver_class_by_role,
        # Boost permission's one necessary condition, plus the clause telling
        # "no cloud by design" apart from "a cloud was planned and lost". The
        # planner cannot see a session's phase list, so the host answers both.
        post_apply_verifies=post_apply_verifies,
        cloud_phase_planned=cloud_phase_planned,
        cloud=cloud,
    )
    return plan_linearization(request, journal=journal)


def build_candidate(
    analysis: ProgramAnalysis,
    cand: Any,
    cloud: CloudFitEvidence | None = None,
    *,
    candidate_sections: Mapping[str, Sequence[CrossoverSection]] | None = None,
    source_preset: Any,
    roles: Sequence[str],
    plan: Callable[..., LinearizationPlan],
    exclusion_evidence: Callable[[CloudFitEvidence], Mapping[str, Any]],
    journal: Callable[[Any], None],
    blend_correction: Sequence[Mapping[str, Any]] = (),
    driver_prescription: DriverPrescription | None = None,
) -> tuple[Any, LinearizationState]:
    """Build one candidate, and return what its linearization produced.

    The state is RETURNED, never stashed, so it can only describe the candidate
    returned beside it. ``cand`` is ``analysis.candidate``, passed rather than
    re-read; ``None`` is legal for exactly one shape, a 1-way main.

    ``plan``, ``exclusion_evidence`` and ``journal`` are ports. ``journal`` is
    REQUIRED (#2361) and stays safe when it raises — see the guard below.
    ``driver_prescription`` is the round's staged per-driver instruction and
    merges HERE because the merge needs the fit and the fit is only final
    inside this function: ``MeasuredCrossoverCandidate.fingerprint`` is
    ``field(init=False)``, so a value stamped on afterwards is refused as
    ``candidate_tampered``.
    """
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverAlignment,
        MeasuredCrossoverCandidate,
    )

    delay_us, delay_role, polarity = alignment_to_candidate_fields(
        analysis, roles=roles,
    )
    alignment = (
        MeasuredCrossoverAlignment(
            delay_us=delay_us, delay_role=delay_role, polarity=polarity,
        )
        if delay_role is not None
        else MeasuredCrossoverAlignment()
    )

    # ``cand`` is ``None`` on a 1-way main, whose lone branch's attenuation is
    # a fixed 0 dB rather than a solved one.
    role_attenuations_db: Mapping[str, float] = (
        {role: 0.0 for role in roles} if cand is None else dict(cand.trim_db)
    )
    linearization: Mapping[str, Any] = {}
    # Held so the per-driver merge below can recompose the prediction from the
    # frame the fit composed one in. ``None`` on every arm that produced no fit.
    fit_plan: LinearizationPlan | None = None
    ineligible = ineligible_reason(analysis, roles=roles)
    state = LinearizationState(outcome=ineligible or "")
    if ineligible is None:
        try:
            fit = plan(
                analysis, cand, cloud, candidate_sections=candidate_sections,
            )
        except (
            ArithmeticError, AttributeError, RuntimeError, TypeError, ValueError,
            KeyError, IndexError,
        ) as exc:
            # SF2: the fit path is strictly additive — an eligible speaker
            # with a bug in the fit engine degrades EXACTLY to the ineligible
            # path rather than failing the whole MEASURE accept. That covers
            # the planner's own typed refusals too (``NoCrossoverSectionsError``
            # / ``CandidateFcDisagreementError``, both ``ValueError``): nothing
            # gets fitted toward a corner the candidate does not describe.
            #
            # Disclosed HERE, inside the handler, carrying the exception —
            # see :class:`FailureRecord` for why a later record renders no
            # traceback. The port call is guarded in turn (#2361): the
            # candidate must not be lost because its own disclosure broke, and
            # there is no return slot to carry the drop, so it goes out this
            # module's own logger.
            try:
                journal(FailureRecord(
                    EVENT_FIT_FAILED,
                    {"reason": type(exc).__name__},
                    logging.WARNING,
                    exc,
                ))
            except _JOURNAL_ERRORS as port_exc:
                log_event(
                    logger, EVENT_FIT_FAILED_JOURNAL_DROPPED,
                    level=logging.WARNING,
                    dropped_event=EVENT_FIT_FAILED,
                    reason=type(port_exc).__name__,
                    exc_info=True,
                )
            role_attenuations_db = (
                {role: 0.0 for role in roles} if cand is None else dict(cand.trim_db)
            )
            linearization = {}
            # A fit that raised part-way may hold a partial verdict, and none
            # of it survives: a fresh state IS that clearing, the linearized
            # VERIFY prior included.
            state = LinearizationState(outcome="fit_failed")
        else:
            role_attenuations_db = dict(fit.role_attenuations_db)
            linearization = dict(fit.linearization)
            state = LinearizationState.from_plan(fit)
            fit_plan = fit

    # TWO locals, because two consumers have two different right answers:
    #   * ``linearization`` — the FIT's map — is what ``exclusion_evidence``
    #     below tests, since that record names what the cloud envelope fed the
    #     FIT and must not ride corrections from anywhere else.
    #   * ``candidate_linearization`` — the SHIPPING map — is what the candidate
    #     carries and what the prediction below recomposes from.
    # ``linearization_outcome`` keeps naming the FIT's verdict; a prescribed
    # branch is told apart by its own ``prescribed_by`` stamp. A prescription
    # therefore rides a ``fit_failed`` round rather than being dropped with the
    # fit — the document passed its evidence gates at staging and at the take.
    candidate_linearization: Mapping[str, Any] = linearization
    if driver_prescription is not None:
        # THE TRIM PIN, folded above every arm that assigns
        # ``role_attenuations_db`` and above the headroom charge and prediction
        # recompose below, so a pin does not depend on which lane the round
        # took. Restricted to roles the candidate already carries: a pin
        # REPLACES a trim and never invents one. Its bound is the door's
        # (non-positive, floored at ``MAX_ATTENUATION_DB``) and
        # ``MeasuredCrossoverCandidate`` re-proves it, so the pin folds INSIDE
        # the clamp.
        pinned = dict(driver_prescription.pinned_trim_db)
        # What each pin DISPLACED, captured before the substitution below. The
        # only place both numbers are in hand, and so the single writer of the
        # disclosure the receipt reads; the program-analysis ``trim_db`` is only
        # the fitted lane's PRE-commit number and would misstate the change.
        displaced_trim_db = {
            role: float(role_attenuations_db[role])
            for role in pinned
            if role in role_attenuations_db
        }
        if pinned:
            role_attenuations_db = {
                role: pinned.get(role, db)
                for role, db in role_attenuations_db.items()
            }
        if displaced_trim_db:
            # A pin REPLACES what ``decide_trim`` committed, so this build no
            # longer ships that pair and must not name it: the proposal falls
            # back to ``TrimStrategy.COMMITTED_PAIR_UNRECORDED``.
            state = replace(state, trim_strategy=None, anchor_drift_db=None)
        candidate_linearization = driver_prescription_to_candidate_fields(
            driver_prescription, fitted=linearization
        )[LINEARIZATION_CANDIDATE_FIELD]
        # #2759: the DISCLOSURE has to describe that same graph. A prescribed
        # entry arrives from the merge with no ``headroom_cost_db``, so it is
        # charged here — through the same ``branch_headroom_db`` over the same
        # three terms ``camilla_yaml.linearization_headroom_db`` charges the
        # speaker with, so the household is told one number, not two that agree
        # by inspection. Prescribed roles only: a fitted role already carries
        # the planner's stamp for the identical chain.
        sections = _sections_for_candidate(candidate_sections, source_preset)
        charged = dict(candidate_linearization)
        for role in driver_prescription.roles:
            entry = dict(charged[role])
            entry["headroom_cost_db"] = branch_headroom_db(
                entry["filters"],
                sections=sections.get(role, ()),
                trim_db=float(role_attenuations_db.get(role, 0.0)),
            )
            # Pinned roles only; the rest displaced nothing.
            if role in displaced_trim_db:
                entry["displaced_trim_db"] = displaced_trim_db[role]
            charged[role] = entry
        candidate_linearization = charged
        # SF1: the prediction must model the EMITTED graph, recomposed through
        # the fit module's own composition from the same raw branches and the
        # trim the fit committed.
        #
        # Skipped when the fit produced no frame (ineligible, or the SF2
        # degrade), and that arm is a known gap, not an unfitted round: the
        # graph carries the document's cuts while the prediction stays raw —
        # 2.9699 dB of divergence against VERIFY's 1.5 dB tolerance, so a round
        # on this arm would false-fail VERIFY. Tracked as #2757; the live
        # session path is the FITTED one.
        frame = None if fit_plan is None else fit_plan.summation_frame
        if frame is not None and state.linearized_predicted_sum is not None:
            state = replace(
                state,
                linearized_predicted_sum=compose_linearized_prediction(
                    frame,
                    filters_by_role=linearization_filters_by_role(
                        candidate_linearization
                    ),
                    role_attenuations_db=role_attenuations_db,
                ),
            )

    # The trim DECISION the applied profile remembers, read whole off the
    # decision that made it — ``committed_side`` is that dataclass's own
    # derivation, not a second one here. ``state`` is only the gate: a pin
    # clears its strategy, and this block drops with it.
    trim_decision: Mapping[str, Any] = {}
    decision = None if fit_plan is None else fit_plan.trim
    if state.trim_strategy is not None and decision is not None:
        trim_decision = {
            "strategy": decision.strategy.value,
            "committed_side": decision.committed_side,
            "anchor_drift_db": round(float(decision.anchor_drift_db), 3),
        }

    return MeasuredCrossoverCandidate(
        program_id=analysis.program_id,
        analysis=analysis_json(analysis),
        source_preset=source_preset,
        role_attenuations_db=role_attenuations_db,
        alignment=alignment,
        linearization=candidate_linearization,
        # Empty whenever no cloud evidence reached the fit, the failed fit
        # included: a record of what the envelope consumed must not ride a
        # candidate whose corrections came from the trims-only fallback.
        exclusion_evidence=(
            exclusion_evidence(cloud)
            if cloud is not None and linearization
            else {}
        ),
        # Stamped verbatim from this build's own returned state, never
        # re-derived, so the candidate and the state beside it cannot describe
        # different builds.
        linearization_outcome=state.outcome,
        trim_decision=trim_decision,
        # Carried VERBATIM from what the previous round's summed evidence
        # prescribed: the solve happens at that round's tail, and a second
        # derivation here would be a second owner of a filter that reaches
        # hardware. Empty on the first round of a series.
        blend_correction=[dict(entry) for entry in blend_correction],
    ), state
