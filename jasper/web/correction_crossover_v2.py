# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare crossover measurement sessions and bind their engine stages."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused, REASON_VOLUME_UNRESOLVED
from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume

from jasper.active_speaker.crossover_v2.position_gate import PositionGate


import dataclasses
import secrets
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from jasper.active_speaker import preflight_live
from typing import Any, Callable, Mapping

from jasper.active_speaker.angle_capture import (
    AngleCaptureRequest, LateralWalkRefused,
    default_run_level,
)
from jasper.active_speaker.preflight import PreflightIssue
from jasper.active_speaker.run_levels import LevelLadder, preflight_levels, prepare_level_captures
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.crossover_v2.capture_plan import (
    build_inline_session_spec,
)
from jasper.web.correction_run_host import bind_run_door, compose_plan_program, publish_round_packet
from jasper.active_speaker.plan_run import RunSignals, prepare_plan_captures, preview_schedule
from jasper.active_speaker.run_manifest import RunManifest, incumbent_fingerprints
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
from jasper.active_speaker.capture_provenance import CaptureProvenanceRecorder
from jasper.active_speaker.crossover_v2.conductor_context import resolve_conductor_context
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.summed_alignment import session_reference

V2_CAPTURE_KIND_SESSION = "crossover_v2:session"


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
    *,
    evidence_store: Any,
    # ``dict``, not ``Mapping``: the analyze seam writes each phase's
    # calibration and provenance into it.
    refs: dict[str, Any],
    publish_check: Any,
) -> Any:
    """Build one stage's :class:`V2FlowSeams`."""
    from jasper.active_speaker.crossover_v2_flow import V2FlowSeams, V2RecordPublishers  # lazy: avoid measurement-stack import cost on unused paths

    return V2FlowSeams(
        summed_alignment_reference=partial(session_reference, Path(evidence_store.bundle_dir)),
        analyze=v2evidence.bind_production_analyze(meta=refs),
        records=V2RecordPublishers(check=publish_check),
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


def prepare_v2_session(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> V2PreparedSession:
    """Prepare the inline measurement run."""
    from jasper.active_speaker.crossover_v2.capture_plan import (
        wall_clock_ceiling_s,
    )
    from jasper.active_speaker.crossover_v2_flow import (  # lazy: avoid measurement-stack import cost on unused paths
        CrossoverV2Session,
    )
    from jasper.active_speaker.crossover_v2.durable_state import (  # lazy: avoid measurement-stack import cost on unused paths
        attempt_history_from_state,
    )

    from jasper.active_speaker.branch_chain import confirmed_protection_sections
    from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
    from jasper.active_speaker.crossover_v2.journey import (
        LATERAL_CONSUMER_FORWARD_MODEL,
    )
    from jasper.active_speaker.crossover_v2.durable_state import (  # lazy: avoid measurement-stack import cost on unused paths
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
    level, level_source = default_run_level(request)
    if request.level_source == "program_default":
        request = dataclasses.replace(request, level=level, level_source=level_source)
    if v2volume.session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before starting "
            "a new session", code=REASON_VOLUME_UNRESOLVED,
        )
    try:
        context = resolve_conductor_context(status)
    except CrossoverV2Refused as exc:  # answered as the preflight reports it, default action included
        refusal = PreflightIssue.from_code(exc.code, str(exc))
        raise CrossoverV2Refused(refusal.detail, code=refusal.code, next_action=refusal.next_action) from exc
    facts = preflight_live.read_preflight_facts(request, context=context)
    report = preflight_levels(request, facts)
    issue = next((issue for issue in report.issues if issue.blocking), None)
    if issue is not None:
        raise CrossoverV2Refused(issue.evidence or issue.detail, code=issue.code, next_action=issue.next_action)
    request = report.plan
    assert request.level.resolved is not None
    captures = (prepare_level_captures if request.levels else prepare_plan_captures)(
        request, roles_bands=context.roles_bands,
    )
    try:
        protection_sections = confirmed_protection_sections(
            context.safety_profile, context.role_targets
        )
    except ValueError as exc:
        raise CrossoverV2Refused("The confirmed driver protection cannot be used for this measurement.",
                                 code="driver_protection_invalid") from exc

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
    position_gate = PositionGate(mover=request.mover)
    capture_session_id = "wired-" + secrets.token_hex(8)
    spec = build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, c.stop.candidate_id) for c in captures],
        roles_bands=context.roles_bands, fc_hz=context.fc_hz,
        safety_profile=context.safety_profile, role_targets=context.role_targets,
        acknowledgement_binding=acknowledgement_binding,
        retries_per_pose=request.retries_per_pose,
        default_setup_calibration=v2evidence.default_setup_calibration_for_v2(),
    )
    evidence_store.publish_json_artifact(f"crossover_v2/{capture_session_id}/plan.json", request.to_dict())
    schedule = preview_schedule(request, captures, context)
    if position_gate:
        position_gate.publish(schedule)

    held: v2evidence._HeldSession | None = None

    def _open() -> Any:
        device = _resolve_prepare_wired_mic()
        assert device is not None
        ceiling_s = wall_clock_ceiling_s(spec.capture_plan.capture_target)
        rc = _mint_wired_session(device, spec)
        rc = dataclasses.replace(rc, pi_session=dataclasses.replace(rc.pi_session, session_id=capture_session_id))
        session_id = rc.pi_session.session_id
        v2volume.session_volume_plan().set_wall_clock_ceiling_s(ceiling_s)
        publish_check, refs = v2evidence.bind_evidence_publishers(
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
            roles=context.roles_bands,
            protection_sections_by_role=protection_sections,
            declared_sensitivities=context.declared_sensitivities,
            provenance=capture_provenance,
            program_for_phase=lambda phase: conductor.program_for_phase(phase),
            program_for_spec=lambda spec, gain: compose_plan_program(conductor, spec, gain, context=context),
        )
        seams = bind_v2_stage_seams(
            evidence_store=evidence_store, refs=refs, publish_check=publish_check,
        )
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
            index_phase_map=stage1_index_phase,
            post_apply_verifies=False,
            driver_spacing_m=context.driver_spacing_m,
            lateral_consumer=LATERAL_CONSUMER_FORWARD_MODEL,
            lateral_prompts=lateral_prompts,
            measure_specs_by_index=engine_measure_specs,
            measurement_protection_sections_by_role=protection_sections,
            sound_design_revision=context.sound_design_revision,
        )
        v2state.persist_conductor_state(conductor, failure_code=None, evidence=refs)
        manifest = RunManifest(session_id, v2evidence._record_store(evidence_store, session_id),
                               incumbent=incumbent_fingerprints(load_applied_baseline_profile_state()))
        from jasper.web import correction_crossover_v2 as host  # lazy: bind this host's seams
        tuning, analyze, assessor, execute = bind_run_door(
            host=host, device=device, evidence_store=evidence_store,
            manifest=manifest, production=production_play, conductor=conductor, refs=refs, provenance=capture_provenance,
            trims=engine_level_trims, ceiling_s=ceiling_s, camilla_factory=camilla_factory, context=context,
            ceiling_db_spl=report.spl_ceiling_db_spl,
            level=report.plan.level, ladder=report if isinstance(report, LevelLadder) else None,
        )
        nonlocal held
        source_run = _build_wired_run(
            conductor,
            door=tuning,
            signals=signals,
            position_gate=position_gate,
            evidence_refs=refs,
            ceiling_s=ceiling_s,
            manifest=manifest, analyze=analyze, assessor=assessor, execute=execute,
            request=request, captures=captures,
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
                if closed is not None and position_gate:
                    await publish_round_packet(Path(evidence_store.bundle_dir), position_gate)

    return V2PreparedSession(
        label=V2_CAPTURE_KIND_SESSION,
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
