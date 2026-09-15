# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare crossover measurement sessions and bind their engine stages."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2 import durable_state as v2durable
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume

from jasper.active_speaker.crossover_v2.position_gate import PositionGate


import dataclasses
import logging
import secrets
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from jasper.active_speaker import preflight, preflight_live
from typing import Any, Callable, Mapping

from jasper.active_speaker.angle_capture import BASE_CANDIDATE, AngleCaptureRequest, AngleStop, LateralWalkRefused, REGIME_SUMMED
from jasper.active_speaker.run_levels import LevelLadder, preflight_levels, prepare_level_captures
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.linearization_budget import fit_budgets_by_role
from jasper.active_speaker.crossover_v2.capture_plan import (
    POSITION_DEG_KEY, POSITION_VERTICAL_DEG_KEY, build_inline_session_spec,
    summed_sweep_band_hz,
)
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.web.correction_run_host import bind_run_door, compose_plan_program
from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError
from jasper.active_speaker.commission_wiring import commissioning_spl_ceiling_db
from jasper.active_speaker.plan_run import RunSignals, PlanCapture, prepare_plan_captures
from jasper.active_speaker.run_manifest import RunManifest, incumbent_fingerprints
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.candidate_trials import tuning_trial_matches_candidate
from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
from jasper.active_speaker.crossover_v2.journey import (
    CAPABILITY_FINDINGS,
    STAGE_MEASURE_CAPABILITIES,
    STAGE_VERIFY_CAPABILITIES,
    StageOpening,
    available_stage_priors,
    open_stage,
)
from jasper.active_speaker.capture_provenance import CaptureProvenanceRecorder
from jasper.active_speaker.crossover_v2.conductor_context import resolve_conductor_context
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

V2_CAPTURE_KIND_SESSION = "crossover_v2:session"
V2_CAPTURE_KIND_VERIFY = "crossover_v2:verify"


class CrossoverV2LocalSeamError(RuntimeError):
    """A LOCAL play/analyze seam raised ``OSError`` — not a program-family failure.

    W6 hardware run 3 finding G: the DSP writer lock's ``os.open`` on a
    read-only ``config_dir`` (finding F) raised a bare ``OSError`` from
    inside ``on_armed``, which the catch-all arm would otherwise misclassify
    identically to a genuine capture-chain fault. ``on_armed``/``consume``
    convert a local ``OSError`` to THIS type at the seam boundary, so it
    reaches the catch-all cleanup arm's honest ``internal_error``
    classification instead of any program-family code.
    """


def classify_program_failure(
    exc: BaseException,
) -> tuple[str, tuple[str, ...]] | None:
    """Map measurement failures to a reason code and refusal slugs.

    Return None for exceptions outside the measurement family.
    """
    from jasper.active_speaker.crossover_v2.capture_plan import PlanShapeError
    from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
    from jasper.active_speaker.crossover_v2.program_transaction import (
        StimulusCaptureStopped,
    )
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_MEASUREMENT_VOLUME_DRIFT, REASON_MEASUREMENT_GRAPH_UNAVAILABLE,
        REASON_PROGRAM_PLAN_SHAPE_INVALID,
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
        REASON_PROGRAM_UNPLAYABLE,
        REASON_PROTECTION_NOT_SEPARABLE,
        REASON_PROTECTION_SWEEP_TOO_LOW,
        REASON_SPL_CEILING_EXCEEDED,
    )
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionError,
        ProgramAdmissionRefusal,
    )
    from jasper.active_speaker.program_playback import (
        ProgramPlaybackError,
        ProgramPlaybackRefused,
    )
    from jasper.active_speaker.volume_latch import MeasurementFaderDrift
    from jasper.active_speaker.measurement_emit import MeasurementGraphRefused  # lazy: graph import cost
    from jasper.audio_measurement.program_analysis import (
        ConfiguredPathConditioningError,
    )
    from jasper.audio_measurement.wired_capture import WiredSplCeilingExceeded

    if isinstance(exc, MeasurementGraphRefused):
        return exc.code, ()
    if isinstance(exc, SessionGraphError):
        return REASON_MEASUREMENT_GRAPH_UNAVAILABLE, ()
    if (
        isinstance(exc, StimulusCaptureStopped)
        and exc.code == WiredSplCeilingExceeded.code
    ):
        # A wired SPL-ceiling trip is a ``RuntimeError`` outside the program
        # family (it stops a TAKE, not an admission), so without this arm it
        # fell through to ``internal_error`` and told the household nothing
        # about why the session stopped. Every other ``StimulusCaptureStopped``
        # code (e.g. ``wired_capture_failed``) keeps falling through below.
        return REASON_SPL_CEILING_EXCEEDED, ()
    if isinstance(exc, MeasurementFaderDrift):
        # #2925. Its own code because it says the OPPOSITE of
        # ``program_unplayable``: the program was admissible and the SPEAKER's
        # level was not the one it was admitted against. No refusal slugs — the
        # observed/expected pair rides the
        # ``active_speaker.measurement_fader_drift`` line that refused, not an
        # admission's refusal set.
        return REASON_MEASUREMENT_VOLUME_DRIFT, ()
    if isinstance(exc, ConfiguredPathConditioningError):
        return (
            REASON_PROTECTION_SWEEP_TOO_LOW if exc.protection_floor
            else REASON_PROTECTION_NOT_SEPARABLE
        ), (exc.slug,)
    if isinstance(exc, PlanShapeError):
        # #2059: an unknown tier or an out-of-range position count is a
        # malformed request, not a level ceiling the speaker could not meet --
        # ``program_unplayable``'s "re-check the driver details" advice is a
        # loose fit here (owner ruling, 2026-08-13).
        return REASON_PROGRAM_PLAN_SHAPE_INVALID, ()
    if not isinstance(
        exc, (ProgramPlaybackError, ProgramAdmissionError, CrossoverV2FlowError)
    ):
        return None
    refusals: tuple[str, ...] = ()
    if isinstance(exc, ProgramPlaybackRefused):
        refusals = tuple(reason.value for reason in exc.admission.refusals)
    code = (
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED
        if ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED.value in refusals
        else REASON_PROGRAM_UNPLAYABLE
    )
    return code, refusals


# --------------------------------------------------------------------------- #
# endpoint preparation (S1a/S1d)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2PreparedSession:
    """What the correction_setup dispatch needs to host one v2 session."""

    label: str
    open: Callable[[], Any]
    run_and_consume: Callable[[Any], Any]
    request_stop: Callable[[str], None]
    position_gate: PositionGate | None = None
    request_complete: Callable[[], None] | None = None
    request_retake: Callable[[], None] | None = None
    join_spec: Any = None
    session_id: str = ""


def _active_graph_fingerprint() -> str:
    """Identity of the Layer-A profile currently on the speaker, or ``""``.

    The conductor's ``entry_graph_fingerprint`` seam (#2291): which DSP graph
    the entry baseline was measured through, so a receipt can say what the
    "before" was a before OF.

    **Reuses the existing owner rather than hashing anything here.**
    :func:`~jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state`
    returns the frozen applied SSOT with its ``candidate_fingerprint``
    *recomputed* from the immutable source + snapshot by
    :func:`~jasper.active_speaker.baseline_profile.baseline_candidate_fingerprint`
    — that repair is why this reads the stored field instead of re-deriving it:
    the loader has already refused to trust a stale or absent stamp. One hash
    function, one definition of "which graph".

    ``""`` for a speaker with no applied profile — its first-ever round, where
    the entry graph genuinely has no identity to name. The conductor turns that
    into its own ``unknown`` word; this function does not invent one, because
    "the loader found nothing" and "the conductor has no seam" are the same
    answer to the round and should not become two vocabularies.
    """
    from jasper.active_speaker.baseline_profile import (
        APPLIED_PROFILE_DISPLACED,
        applied_profile_displacement,
        load_applied_baseline_profile_state,
    )

    applied = load_applied_baseline_profile_state()
    if not isinstance(applied, Mapping):
        return ""
    # The record is only an answer to "which graph is on the speaker" while it
    # is still the graph on the speaker (#2537). An out-of-band reconcile
    # changes the RUNNING config without touching this record, and a receipt
    # that then named the record's fingerprint would assert a graph the speaker
    # had not played for hours — which is exactly the 2026-08-15 cycle-4 shape,
    # one layer up from the restore it misdirected. A displaced record answers
    # ``""``, which the coordinator turns into its own ``unknown`` word: the
    # honest "we cannot name it", not a wrong name.
    #
    # ONLY on a positive displacement. The other two codes mean the comparison
    # could not be made — no statefile to read, no path on the record — and
    # this module's standing rule is that an absent measurement is not evidence
    # of a defect. Dropping a fingerprint because a statefile was unreadable
    # would make every box without one report ``unknown`` forever.
    if applied_profile_displacement(applied) == APPLIED_PROFILE_DISPLACED:
        log_event(
            logger,
            "correction.crossover_v2_applied_profile_displaced",
            level=logging.WARNING,
            surface="entry_graph_fingerprint",
        )
        return ""
    return str(applied.get("candidate_fingerprint") or "")


def _previous_candidate_known() -> bool:
    from jasper.web.correction_crossover_v2_status import rollback_candidate  # lazy: status imports commissioning state

    return rollback_candidate(v2state.load_v2_state()) is not None


def _applied_graph_boosts() -> bool:
    """Does the graph currently on the speaker put energy IN? (#2291)

    #2318's fail-closed cell asks this of the APPLIED intervention, and the
    grading conductor cannot answer it from its own state: stage 2 builds a
    fresh conductor whose ``_candidate`` is never set (only stage 1's commit
    assigns one), so the predicate read ``None`` on every shipped round and
    the cell was unreachable — a boosted round with unprovable benefit ended
    accepted, which is exactly the state that rule exists to prevent.

    **One owner for "what did we apply": the applied profile SSOT.** Not the
    durable ``state["candidate"]``, which is a display summary carrying
    ``linearization_outcome`` and per-octave figures but not the filters; and
    not a re-derivation, because
    :func:`~jasper.active_speaker.baseline_profile.profile_linearization`
    already owns which copy of a profile's linearization is authoritative.
    That mapping is ALREADY reduced, so it goes straight to the shipped
    predicate with no ``linearization_filters_by_role`` in between — that
    reducer returns ``{}`` for an already-reduced mapping, which would read as
    "this graph boosts nothing" and quietly restore the bug.

    **Fails closed.** An unreadable profile answers "boosted", so an
    intervention nobody can inspect comes off rather than staying on evidence
    nobody has — the same direction the conductor takes when this seam is
    absent entirely.
    """
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
        profile_linearization,
    )
    from jasper.active_speaker.camilla_yaml import linearization_has_boost

    try:
        return linearization_has_boost(
            profile_linearization(load_applied_baseline_profile_state())
        )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        log_event(
            logger,
            "correction.crossover_v2_applied_boost_unreadable",
            level=logging.WARNING,
            exc_info=True,
        )
        return True


def _applied_profile_now() -> Mapping[str, Any] | None:
    """The Layer-A profile the speaker is playing right now, or ``None`` (#2611).

    The conductor's PREVIOUS-graph seam. Read at MEASURE time, when the apply
    has not happened yet, so "currently applied" and "the graph this apply would
    replace" are the same profile — and read through the same
    :func:`~jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state`
    record ``_applied_graph_boosts`` and :func:`_active_graph_fingerprint` read,
    so the commanded axis, the boost predicate and the entry identity all
    describe one graph.

    **``_applied_offset_gate`` is a DIFFERENT source, and saying otherwise was
    wrong.** That seam reads ``expected_post_apply_offset_db`` off the v2
    durable state (``load_v2_state``), written at Apply time by
    :func:`observe_apply_success` from the two profiles' program headrooms. It
    is a number about an apply, banked once; this is a record about a graph,
    re-read live. They are two accounts of one apply that are meant to be
    disjoint (per-role gains on the commanded axis, the common pre-split gain in
    the offset) and they are not one SSOT with two readers.

    **The displaced-record guard, the same one
    :func:`_active_graph_fingerprint` applies** (#2537's 2026-08-15 cycle-4
    shape). An out-of-band reconcile changes the RUNNING config without
    touching this record, so a displaced record no longer answers "which graph
    is on the speaker" — and this PR makes that record rollback-DECIDING, which
    is a stronger claim than the fingerprint it was first refused for. Only a
    POSITIVE displacement refuses: the other two codes mean the comparison could
    not be made, and an absent measurement is not evidence of a defect.

    **Fails to ``None``, and that is not the fail-closed direction here — it is
    the honest one.** ``None`` makes the commanded axis unavailable and the delta
    probe ``unavailable``: no rollback, and no pass either. There is deliberately
    no fabricated substitute, because grading against a graph nobody ran is the
    defect #2611 records.
    """
    from jasper.active_speaker.baseline_profile import (
        APPLIED_PROFILE_DISPLACED,
        applied_profile_displacement,
        load_applied_baseline_profile_state,
    )

    try:
        applied = load_applied_baseline_profile_state()
        if applied is not None and (
            applied_profile_displacement(applied) == APPLIED_PROFILE_DISPLACED
        ):
            log_event(
                logger,
                "correction.crossover_v2_applied_profile_displaced",
                level=logging.WARNING,
                surface="commanded_axis",
            )
            return None
        return applied
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        log_event(
            logger,
            "correction.crossover_v2_applied_profile_unreadable",
            level=logging.WARNING,
            exc_info=True,
        )
        return None


def bind_v2_engine_seams(
    *, session_graph: Any, compose_stimulus: Any, capture_stimulus: Any,
    records: Any, volume_claim: Any,
) -> Any:
    """Bind the shared take owner to this host's session volume plan."""
    from jasper.active_speaker.crossover_v2.composition import bind_engine_seams

    if volume_claim is None:
        raise v2volume._refuse_without_a_volume_owner("session")
    return bind_engine_seams(
        session_graph=session_graph, records=records,
        volume_claim=volume_claim, session_volume_plan=v2volume.session_volume_plan(),
        compose_stimulus=compose_stimulus, capture_stimulus=capture_stimulus,
    )


def bind_v2_stage_seams(
    opening: StageOpening,
    *,
    evidence_store: Any,
    capture_session_id: str,
    # ``dict``, not ``Mapping``: the four evidence binders below take the
    # MUTABLE refs dict ``bind_evidence_publishers`` returns and write artifact
    # fingerprints into it. Widening this to ``Mapping`` would type-check here
    # and lie about that.
    refs: dict[str, Any],
    publish_check: Any,
    publish_candidate: Any,
    run_async: Any,
    camilla_factory: Any = None,
    provenance: CaptureProvenanceRecorder | None = None,
    layout: str | None = None,
) -> Any:
    """Build one stage's :class:`V2FlowSeams`, and declare what it opened with.

    The unconditional seams are unconditional on purpose. ``apply_failed`` is
    never consulted by stage 2 (its conductor is constructed ``applied=True``,
    so ``authorize_begin``'s apply-observed short-circuit runs first), and
    ``records.cloud`` does nothing for a single-entry recovery re-verify.
    ``bank_take`` is no longer in that company: a recovery re-verify IS an
    accepted VERIFY capture, so it banks a take like any other, and that is
    the wanted behaviour rather than an accident of binding — the round whose
    evidence a household is recovering is exactly the one whose capture should
    survive it. Full's stage 2 is also a post-apply position group whose combined
    curve the after-chart, the post-apply spec verdict, and the delta probe all
    read, and ``V2FlowSeams`` requires the two apply gates outright. Binding a
    seam a plan never exercises costs nothing; omitting one a plan does
    exercise is a silently missing publication.

    ``provenance`` is threaded here only to reach the analyze seam: the SAME
    recorder the caller handed ``bind_production_play``, which is the pairing.

    The shortfall is :attr:`~...journey.StageOpening.missing`, derived where the
    declaration lives so the two cannot disagree. Logging it lives HERE rather
    than in the journey or at the call sites: the journey is a pure aggregate
    with no journal of its own, and a third caller that bound seams without
    declaring them would put the journal's account of stage shape back out of
    one owner's hands.
    """
    from jasper.active_speaker.crossover_v2_flow import V2FlowSeams, V2RecordPublishers

    capabilities = opening.capabilities
    missing = opening.missing
    log_event(
        logger, "correction.crossover_v2_stage_capabilities",
        stage=capabilities.stage, session_id=capture_session_id,
        provides=",".join(sorted(capabilities.provides)),
        requires=",".join(sorted(capabilities.requires)),
        missing=",".join(missing),
    )
    if missing:
        # WARNING, and its own event: a required prior that did not cross the
        # bridge does not stop this stage, so nothing else would ever say the
        # verdict it is about to produce was reached with an input absent.
        log_event(
            logger, "correction.crossover_v2_stage_capability_unavailable",
            level=logging.WARNING, stage=capabilities.stage,
            session_id=capture_session_id, missing=",".join(missing),
        )
    # The one-capture handoff from the analyze seam to the banking seam, in
    # the same type the play seam already uses for its own hop. A second
    # recorder rather than a shared slot because the single-shot ``take`` is
    # exactly the semantics this hop needs too: a take banked with no analyze
    # behind it must name no provenance, never the previous capture's.
    banked_provenance = CaptureProvenanceRecorder()
    # The analyze seam's own hop over the same gap, for the blocks only it
    # holds. Bound unconditionally: with the capture-dump ring gone the banked
    # record is the only file these numbers can land in.
    banked_evidence = v2evidence.CaptureEvidenceCarry()
    from jasper.web.correction_crossover_v2_restore import bind_boost_restore, current_graph_fingerprint  # lazy: host binding cycle

    from jasper.active_speaker.crossover_v2.summed_alignment import session_reference  # lazy: NumPy analysis boundary

    return V2FlowSeams(
        summed_alignment_reference=partial(session_reference, Path(evidence_store.bundle_dir)),
        analyze=v2evidence.bind_production_analyze(
            meta=refs, provenance=provenance, carry=banked_provenance,
            evidence=banked_evidence,
        ),
        # ADR-0227 §12 FOLD: one seam, discriminated by kind, over the same
        # four binders this always called — only the V2FlowSeams shape they
        # land in changed.
        records=V2RecordPublishers(
            check=publish_check,
            candidate=publish_candidate,
            cloud=v2evidence.bind_cloud_publisher(
                evidence_store, capture_session_id, refs, run_async
            ),
            # #2291's round receipt. Bound on both stages rather than gated on a
            # capability: only the stage that GRADES a round ever calls it, and a
            # binding that exists everywhere cannot be the reason a receipt went
            # unwritten on the stage that needed it.
            round_receipt=v2evidence.bind_round_receipt(
                evidence_store, capture_session_id, refs, run_async
            ),
            findings=(
                v2evidence.bind_findings_publisher(
                    evidence_store, capture_session_id, refs, run_async
                )
                if CAPABILITY_FINDINGS in capabilities.provides else None
            ),
        ),
        apply_complete=v2state._applied_gate,
        apply_failed=v2state._apply_failure_gate,
        bank_take=v2evidence.bind_position_retention(
            evidence_store, refs,
            provenance=banked_provenance, evidence=banked_evidence, layout=layout,
        ),
        applied_offset_db=v2state._applied_offset_gate,
        # #2611: the graph an apply replaces, for the commanded axis. Bound on
        # both stages for ``entry_graph_fingerprint``'s reason — "what is live
        # right now" is not a stage asymmetry — though only stage 1 commits a
        # candidate and therefore only stage 1 reads it today.
        applied_profile=_applied_profile_now,
        record_model_error=v2state._record_live_model_error,
        rollback_available=_previous_candidate_known,
        restore_boost=bind_boost_restore(run_async, camilla_factory),
        tuning_graph_fingerprint=current_graph_fingerprint,
        # #2291/#2318: "does the APPLIED graph boost". Bound on both stages for
        # ``entry_graph_fingerprint``'s reason — what is live right now is not
        # a stage asymmetry — and it is the only way the grading stage can
        # answer at all, since its conductor never holds the candidate.
        applied_boosts=_applied_graph_boosts,
        # Unconditional on both stages: "which graph is live right now" is not
        # a stage asymmetry, and #2291's receipt is what lets a LATER round
        # bind the currently-active profile as its own entry graph.
        entry_graph_fingerprint=_active_graph_fingerprint,
    )


def _resolve_prepare_wired_mic() -> Any:
    """Resolve once at admission and keep the microphone's refusal code."""
    from jasper.audio_measurement.wired_capture import WiredCaptureError
    from jasper.web import correction_crossover_v2_wired as wired

    try:
        return wired.resolve_v2_wired_mic()
    except WiredCaptureError as exc:
        raise CrossoverV2Refused(
            str(exc), code=str(getattr(exc, "code", "") or ""),
        ) from exc


def _hand_released_plan_shape(plan_shape: Any) -> Any:
    """The same shape, told whether a PERSON releases each of its begins.

    A hand-walked round is the shape that needs saying: nothing paces it, and
    without a hold the local runner fires every capture back to back while the
    household is still walking to the next spot. Its begins are therefore held
    and released by hand (``V2PlanShape.hand_released_positions``), the same
    ``POST /crossover/v2/position-ready`` an external driver uses.

    Every other shape is returned untouched: the arm already holds behind its
    driver's report, and the tier-less recovery re-arm (``plan_shape is
    None``) is one sweep at the mark with no walk to pace at all.
    """
    if plan_shape is None:
        return plan_shape
    if plan_shape.externally_positioned:
        return plan_shape
    return dataclasses.replace(plan_shape, hand_released_positions=True)


def _mint_wired_session(wired_device: Any, spec: Any) -> Any:
    from jasper.web import correction_crossover_v2_wired as wired

    return wired.open_wired_capture(spec, device=wired_device)


def _wired_stimulus_capture(
    wired_device: Any, evidence_store: Any, *, spl_monitor: Any = None, read_loudness_volume_db: Any = None,
) -> Any:
    from jasper.active_speaker.crossover_v2.wired_stimulus import (
        WiredStimulusCapture,
    )  # lazy: ALSA capture boundary
    from jasper.audio_measurement.wired_capture import setup_from_hint

    return WiredStimulusCapture(
        device=wired_device, bundle_dir=Path(evidence_store.bundle_dir),
        setup_reference=lambda: setup_from_hint(v2evidence.default_setup_calibration_for_v2()),
        spl_monitor=spl_monitor, read_loudness_volume_db=read_loudness_volume_db,
    )


def _build_wired_run(conductor: Any, **host: Any) -> Callable[[Any], Any]:
    from jasper.web import correction_crossover_v2_wired as wired  # lazy: wired host seam

    return wired.build_v2_wired_run_and_consume(conductor, **host)


# The request field that selects which post-apply instrument a verify-only
# prepare opens, and its one non-default value.
VERIFY_STAGE_KEY = "stage"
VERIFY_STAGE_POST_APPLY = "post_apply"
VERIFY_STAGE_RECOVERY = "recovery"


def _verify_plan_shape(
    raw: Mapping[str, Any] | None,
) -> Any:
    """The caller chooses a full post-apply walk or one recovery sweep."""
    from jasper.active_speaker.crossover_v2.capture_plan import resolve_plan_shape

    stage = str((raw or {}).get(VERIFY_STAGE_KEY) or VERIFY_STAGE_RECOVERY).strip()
    if stage == VERIFY_STAGE_RECOVERY:
        return None
    if stage != VERIFY_STAGE_POST_APPLY:
        raise CrossoverV2Refused(
            f"unknown verify stage {stage!r} (expected "
            f"{VERIFY_STAGE_POST_APPLY!r} or {VERIFY_STAGE_RECOVERY!r})"
        )
    return resolve_plan_shape()


def prepare_v2_session(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
    verify_only: bool = False,
) -> V2PreparedSession:
    """Prepare the inline run or the existing post-apply verification."""
    from jasper.active_speaker.crossover_v2.capture_plan import (
        wall_clock_ceiling_s,
    )
    from jasper.active_speaker.crossover_v2.coordinator import (
        series_position_from_state,
    )
    from jasper.active_speaker.crossover_v2.programs import measurement_band_hz
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2Session,
        attempt_history_from_state,
    )

    if verify_only:
        from jasper.active_speaker.crossover_v2.capture_plan import (
            build_v2_verify_index_phase_map,
            build_v2_verify_session_spec,
        )
        from jasper.active_speaker.crossover_v2.journey import (
            PHASE_CHECK,
            PHASE_MEASURE,
        )

        if v2volume.session_volume_plan().needs_recovery:
            raise CrossoverV2Refused(
                "the measurement volume needs recovery; recover it before verifying"
            )
        state = v2state.load_v2_state() or {}
        if not state.get("applied"):
            raise CrossoverV2Refused(
                "verification needs an applied measured crossover; measure and "
                "apply first"
            )
        candidate_state = state.get("candidate")
        candidate_fingerprint = (
            candidate_state.get("fingerprint")
            if isinstance(candidate_state, Mapping) else None
        )
        if tuning_trial_matches_candidate(
            state.get("tuning_trial"), candidate_fingerprint,
        ):
            raise CrossoverV2Refused(
                "this measured tuning is already applied; it does not "
                "use the speaker-fit verification stage"
            )
        tuning_attempt_id = (
            str(candidate_state.get("fingerprint") or "")
            if isinstance(candidate_state, Mapping) else ""
        )
        plan_shape = _verify_plan_shape(raw)
        context = resolve_conductor_context(status)
    else:
        from jasper.active_speaker.branch_chain import confirmed_protection_sections
        from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
        from jasper.active_speaker.crossover_v2.journey import (
            LATERAL_CONSUMER_FORWARD_MODEL,
        )
        from jasper.active_speaker.crossover_v2_flow import (
            V2ConductorSnapshot,
        )

        if "tier" in raw or "stage" in raw or not isinstance(raw.get("plan"), Mapping):
            raise CrossoverV2Refused("An inline v5 plan is required", code="program_plan_shape_invalid")
        try:
            request = AngleCaptureRequest.from_mapping(raw["plan"])
        except LateralWalkRefused as exc:
            raise CrossoverV2Refused(exc.detail, code=exc.reason) from exc
        except (ValueError, TypeError, CrossoverV2FlowError) as exc:
            raise CrossoverV2Refused(str(exc), code="program_plan_shape_invalid") from exc
        plan_shape = None
        if v2volume.session_volume_plan().needs_recovery:
            raise CrossoverV2Refused(
                "the measurement volume needs recovery; recover it before starting "
                "a new session"
            )
        context = resolve_conductor_context(status)
        facts = preflight_live.read_preflight_facts(request, context=context)
        report = preflight_levels(request, facts, raw.get("levels"))
        issue = next((issue for issue in report.issues if issue.blocking), None)
        if issue is not None:
            raise CrossoverV2Refused(issue.evidence or issue.detail, code=issue.code, next_action=issue.next_action)
        request = report.plan
        assert request.level.resolved is not None
        captures = (prepare_level_captures if isinstance(report, LevelLadder) else prepare_plan_captures)(
            request, roles_bands=context.roles_bands,
        )
        try:
            protection_sections = confirmed_protection_sections(
                context.safety_profile, context.role_targets
            )
        except ValueError as exc:
            raise CrossoverV2Refused(
                "The confirmed driver protection cannot be used for this measurement."
            ) from exc

    wired_device = _resolve_prepare_wired_mic() if verify_only else None
    if verify_only:
        session_plan = AngleCaptureRequest(stops=(AngleStop(0, REGIME_SUMMED),))
        report = preflight.preflight(session_plan, preflight_live.read_preflight_facts(
            session_plan, context=context, device=wired_device))
        if report.blocking:
            issue = next(issue for issue in report.issues if issue.blocking)
            raise CrossoverV2Refused(issue.evidence or issue.detail, code=issue.code, next_action=issue.next_action)
    plan_shape = _hand_released_plan_shape(plan_shape)
    engine_measure_specs: dict[int, Any] = {}
    engine_level_trims: dict[str, float] = {}
    if not verify_only:
        stage1_index_phase = {index: capture.spec.program_phase for index, capture in enumerate(captures, 1)}
        engine_measure_specs = {index: capture.spec for index, capture in enumerate(captures, 1)}
        engine_level_trims, _ = v2state._resolve_measurement_level_trims(
            request.template, preset=context.preset, topology=context.topology,
        )
        if request.template.level_matched and not engine_level_trims:
            raise CrossoverV2Refused("No measured driver levels are available", code="walk_level_match_no_evidence")
        lateral_prompts = tuple(capture.resolved(request).prompt
            for capture in captures if capture.spec.program_phase == PHASE_LATERAL)
    evidence_store, _bundle_id = v2evidence.open_v2_evidence_store(context.topology)
    if verify_only:
        import numpy as np

        priors_raw = state.get("verify_priors") or {}
        sum_raw = priors_raw.get("predicted_sum") if isinstance(priors_raw, Mapping) else None
        predicted_sum = None
        if isinstance(sum_raw, Mapping) and sum_raw.get("freqs_hz"):
            predicted_sum = (
                np.asarray(sum_raw["freqs_hz"], dtype=float),
                np.asarray(sum_raw["magnitude_db"], dtype=float),
            )
        predicted_spec = (
            priors_raw.get("predicted_spec") if isinstance(priors_raw, Mapping) else None
        )
        predicted_spec = predicted_spec if isinstance(predicted_spec, Mapping) else None
        commanded_delta = v2durable.commanded_delta_prior_from_state(state)
        declared_transfer = v2durable.declared_transfer_prior_from_state(state)
        proposal_fingerprint = (
            str(priors_raw.get("proposal_fingerprint") or "")
            if isinstance(priors_raw, Mapping) else ""
        )
        entry_baseline = v2durable.entry_baseline_prior_from_state(state)
        alignment_objective = str(
            (priors_raw.get("alignment_objective") if isinstance(priors_raw, Mapping)
             else "") or ""
        )
        gate_ms = (
            priors_raw.get("gate_window_ms") if isinstance(priors_raw, Mapping) else None
        )
        pilot_transfer_prior = v2durable.pilot_transfer_prior_from_state(state)
    else:
        prior_raw = v2state.load_v2_state()
        prior_snapshot = (
            V2ConductorSnapshot(
                session_id=str(prior_raw.get("session_id") or ""),
                accepted_phases=tuple(prior_raw.get("accepted_phases") or ()),
                applied=bool(prior_raw.get("applied")),
                gain_plan_db=prior_raw.get("gain_plan_db"),
                measure_gain_ceiling_db=prior_raw.get("measure_gain_ceiling_db"),
                attempt_history=attempt_history_from_state(prior_raw),
            )
            if isinstance(prior_raw, Mapping)
            else None
        )

    acknowledgement_binding = secrets.token_urlsafe(24)
    signals = RunSignals()
    position_gate = PositionGate(mover=request.mover) if not verify_only else PositionGate() if plan_shape and plan_shape.positions_gated else None
    capture_session_id = "wired-" + secrets.token_hex(8)
    spec = None if verify_only else build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, c.stop.candidate_id) for c in captures],
        roles_bands=context.roles_bands, fc_hz=context.fc_hz,
        acknowledgement_binding=acknowledgement_binding,
        retries_per_pose=request.retries_per_pose,
        default_setup_calibration=v2evidence.default_setup_calibration_for_v2(),
    )
    if not verify_only:
        evidence_store.publish_json_artifact(f"crossover_v2/{capture_session_id}/plan.json", request.to_dict())

    held: v2evidence._HeldSession | None = None

    def _open() -> Any:
        nonlocal spec
        device = wired_device if verify_only else _resolve_prepare_wired_mic()
        assert device is not None
        if verify_only:
            spec = build_v2_verify_session_spec(
                context.fc_hz,
                measurement_band_hz=measurement_band_hz(context.roles_bands),
                acknowledgement_binding=acknowledgement_binding,
                plan_shape=plan_shape,
                default_setup_calibration=v2evidence.default_setup_calibration_for_v2(),
            )
        assert spec is not None
        ceiling_s = wall_clock_ceiling_s(spec.capture_plan.capture_target)
        rc = _mint_wired_session(device, spec)
        if not verify_only:
            rc = dataclasses.replace(rc, pi_session=dataclasses.replace(rc.pi_session, session_id=capture_session_id))
        session_id = rc.pi_session.session_id
        v2volume.session_volume_plan().set_wall_clock_ceiling_s(ceiling_s)
        publish_check, publish_candidate, refs = v2evidence.bind_evidence_publishers(
            evidence_store, session_id, run_async
        )
        capture_provenance = CaptureProvenanceRecorder()
        production_play = v2evidence.bind_production_play(
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            capture_session_id=session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
            protection_sections_by_role=(
                None if verify_only else protection_sections
            ),
            declared_sensitivities=context.declared_sensitivities,
            provenance=capture_provenance,
            program_for_phase=lambda phase: conductor.program_for_phase(phase),
            program_for_spec=lambda spec, gain: (
                conductor.program_for_phase(spec.program_phase) if verify_only and gain is None
                else compose_plan_program(conductor, spec, gain)),
        )
        if verify_only:
            opening = open_stage(
                STAGE_VERIFY_CAPABILITIES,
                index_phase_map=build_v2_verify_index_phase_map(plan_shape=plan_shape),
                available=available_stage_priors(
                    commanded_delta=commanded_delta is not None,
                    predicted_sum=predicted_sum is not None,
                    entry_baseline=entry_baseline is not None,
                ),
            )
        else:
            opening = open_stage(
                STAGE_MEASURE_CAPABILITIES,
                index_phase_map=stage1_index_phase,
                verify_capture_target=0,
            )
        seams = bind_v2_stage_seams(
            opening,
            evidence_store=evidence_store,
            capture_session_id=session_id,
            refs=refs,
            publish_check=publish_check,
            publish_candidate=publish_candidate,
            run_async=run_async,
            camilla_factory=camilla_factory,
            provenance=capture_provenance, layout=context.preset.channel_map.layout,
        )
        if verify_only:
            conductor = CrossoverV2Session(
                session_id=session_id,
                source_preset=context.preset,
                positions_gated=bool(plan_shape and plan_shape.positions_gated),
                roles_bands=context.roles_bands,
                fc_hz=context.fc_hz,
                driver_caps_dbfs=context.driver_caps_dbfs,
                driver_sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
                session_volume_db=context.session_volume_db,
                seams=seams,
                driver_spacing_m=context.driver_spacing_m,
                driver_class_by_role=context.driver_class_by_role,
                fit_budget_by_role=fit_budgets_by_role(context.safety_profile),
                radiating_diameter_mm_by_role=context.radiating_diameter_mm_by_role,
                tweeter_measurement_band_hz=context.measurement_band_hz_by_role.get("tweeter"),
                accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
                applied=True,
                gain_plan_db=state.get("gain_plan_db"),
                measure_gain_ceiling_db=state.get("measure_gain_ceiling_db"),
                index_phase_map=opening.plan.index_phase_map,
                measure_predicted_sum=predicted_sum,
                measure_predicted_spec_report=predicted_spec,
                measure_commanded_delta=commanded_delta,
                measure_declared_transfer=declared_transfer,
                measure_proposal_fingerprint=proposal_fingerprint,
                measure_entry_baseline=entry_baseline,
                measure_alignment_objective=alignment_objective,
                measure_gate_window_ms=(
                    float(gate_ms) if isinstance(gate_ms, (int, float)) else None
                ),
                verify_pilot_transfer_prior=pilot_transfer_prior,
                attempt_history=attempt_history_from_state(state),
                series_position=series_position_from_state(state),
                speaker_id=context.topology.topology_id,
                tuning_attempt_id=tuning_attempt_id,
            )
        else:
            series_position = series_position_from_state(prior_raw)
            conductor = CrossoverV2Session.hydrate(
                prior_snapshot,
                session_id=session_id,
                source_preset=context.preset,
                roles_bands=context.roles_bands,
                fc_hz=context.fc_hz,
                driver_caps_dbfs=context.driver_caps_dbfs,
                driver_sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
                session_volume_db=context.session_volume_db,
                seams=seams,
                positions_gated=True,
                index_phase_map=opening.plan.index_phase_map,
                post_apply_verifies=opening.plan.post_apply_verifies,
                driver_spacing_m=context.driver_spacing_m,
                driver_class_by_role=context.driver_class_by_role,
                fit_budget_by_role=fit_budgets_by_role(context.safety_profile),
                radiating_diameter_mm_by_role=context.radiating_diameter_mm_by_role,
                lateral_consumer=LATERAL_CONSUMER_FORWARD_MODEL,
                lateral_prompts=lateral_prompts,
                measure_specs_by_index=engine_measure_specs,
                measurement_protection_sections_by_role=protection_sections,
                sound_design_revision=context.sound_design_revision,
                tweeter_measurement_band_hz=context.measurement_band_hz_by_role.get("tweeter"),
                speaker_id=context.topology.topology_id,
                series_position=series_position,
            )
        v2state.persist_conductor_state(conductor, failure_code=None, evidence=refs)
        manifest = RunManifest(session_id, v2evidence._record_store(evidence_store, session_id),
                               incumbent=incumbent_fingerprints(load_applied_baseline_profile_state()))
        from jasper.web import correction_crossover_v2 as host  # lazy: bind this host's seams
        tuning, analyze, assessor, execute = bind_run_door(
            host=host, device=device, evidence_store=evidence_store,
            manifest=manifest, production=production_play, conductor=conductor, refs=refs, provenance=capture_provenance,
            trims=engine_level_trims, ceiling_s=ceiling_s, camilla_factory=camilla_factory,
            ceiling_db_spl=(commissioning_spl_ceiling_db(context.topology, preset=context.preset)
                            if verify_only else report.spl_ceiling_db_spl), verify_only=verify_only,
            level=report.plan.level, levels=report.plan.levels, ladder=report if isinstance(report, LevelLadder) else None,
        )
        run_request = None if verify_only else request
        run_captures = None if verify_only else captures
        if verify_only:
            run_request = AngleCaptureRequest(level=report.plan.level, stops=tuple(
                AngleStop(int(entry.screen.get(POSITION_DEG_KEY, 0)), REGIME_SUMMED,
                          elevation_deg=int(entry.screen.get(POSITION_VERTICAL_DEG_KEY, 0)), purpose="room")
                for entry in spec.capture_plan.entries
            ))
            run_captures = tuple(PlanCapture(stop, MeasureSpec(
                kind="verify", graph_scope="candidate", candidate_id=BASE_CANDIDATE, positions=(stop.angle_deg,),
                vertical_deg=stop.elevation_deg, program_phase=opening.plan.index_phase_map[index],
                sweep_band_hz=summed_sweep_band_hz(context.roles_bands),
            )) for index, stop in enumerate(run_request.stops, 1))
        nonlocal held
        source_run = _build_wired_run(
            conductor,
            door=tuning,
            signals=signals,
            position_gate=position_gate,
            evidence_refs=refs,
            ceiling_s=ceiling_s,
            manifest=manifest, analyze=analyze, assessor=assessor, execute=execute,
            request=run_request, captures=run_captures,
        )
        held = v2evidence._HeldSession(tuning=tuning, run=source_run)
        return rc

    async def _run(pi_session: Any) -> None:
        """Close the evidence bundle after the worker releases the speaker."""
        if held is None:
            raise RuntimeError(
                "the v2 measurement session was run before it was opened"
            )
        completed = False
        try:
            await held.run(pi_session)
            completed = True
        finally:
            state = v2state.load_v2_state() or {}
            restored = (state.get("execution") or {}).get("volume_restore")
            if (
                state.get("session_id") == pi_session.session_id
                and not held.tuning.is_open
                and restored in {
                    SessionVolumeRestoreResult.EXACT_RESTORED,
                    SessionVolumeRestoreResult.EMERGENCY_ATTENUATED,
                    SessionVolumeRestoreResult.ALREADY_RESOLVED,
                }
            ):
                closed = mark_state(Path(evidence_store.bundle_dir), "closed")
                if closed is None and completed:
                    raise OSError("the measurement bundle could not be closed")

    return V2PreparedSession(
        label=V2_CAPTURE_KIND_VERIFY if verify_only else V2_CAPTURE_KIND_SESSION,
        join_spec=spec,
        session_id=capture_session_id,
        open=_open,
        run_and_consume=_run,
        request_stop=signals.request_stop,
        position_gate=position_gate,
        request_complete=signals.complete.set,
        request_retake=(
            signals.retake.set if position_gate is not None else None
        ),
    )
