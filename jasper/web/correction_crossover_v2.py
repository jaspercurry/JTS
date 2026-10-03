# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare crossover measurement sessions and bind their engine stages."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused, REASON_VOLUME_UNRESOLVED
from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume
from jasper.platform.route_health import snapshot_route_health

from jasper.active_speaker.crossover_v2.position_gate import PositionGate


import dataclasses
import secrets
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from jasper.active_speaker import preflight_live
from typing import Any, Callable, Mapping

from jasper.active_speaker.angle_capture import LateralWalkRefused
from jasper.active_speaker.arm_walk import mover_present
from jasper.active_speaker.measurement_programs import near_field_drivers
from jasper.active_speaker.preflight import PreflightIssue, preflight
from jasper.active_speaker.run_request import RunRequest, resolve_plan, run_envelope
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.crossover_v2.capture_plan import (
    build_inline_session_spec,
)
from jasper.active_speaker.crossover_v2.programs import probe_fader_db
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
    #: What a run's answer states of the run staged here: its subject, parameters and preflight (ADR-0389).
    staged: Mapping[str, Any] = field(default_factory=dict)


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


def _refused(exc: Exception) -> CrossoverV2Refused:
    """A request this door cannot run, under the code it names."""
    if isinstance(exc, LateralWalkRefused):
        return CrossoverV2Refused(exc.detail, code=exc.reason)
    return CrossoverV2Refused(str(exc), code=getattr(exc, "reason", None) or "program_plan_shape_invalid")


def _mint_wired_session(wired_device: Any, spec: Any) -> Any:
    from jasper.web import correction_crossover_v2_wired as wired

    return wired.open_wired_capture(spec, device=wired_device)


def _wired_stimulus_capture(
    wired_device: Any, evidence_store: Any, *, spl_monitor: Any = None,
) -> Any:
    from jasper.active_speaker.crossover_v2.wired_stimulus import (
        WiredStimulusCapture,
    )  # lazy: ALSA capture boundary
    from jasper.audio_measurement.wired_capture import setup_from_hint

    return WiredStimulusCapture(
        device=wired_device, bundle_dir=Path(evidence_store.bundle_dir),
        setup_reference=lambda: setup_from_hint(v2evidence.default_setup_calibration_for_v2()),
        spl_monitor=spl_monitor, read_route_health=snapshot_route_health,
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

    from jasper.active_speaker.branch_chain import confirmed_protection_sections
    from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError

    if set(raw) - {"request", "attest_rig_clear"} or not isinstance(raw.get("request"), Mapping):
        raise CrossoverV2Refused("A run request is required", code="program_plan_shape_invalid")
    try:
        source = RunRequest.from_mapping(raw["request"])
    except ValueError as exc:
        raise _refused(exc) from exc
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
    try:
        request = resolve_plan(source, targets=lambda: near_field_drivers(context.topology))
    except (ValueError, CrossoverV2FlowError) as exc:
        raise _refused(exc) from exc
    # An arm plan plays only on the operator's word that the arm's path is clear.
    facts = preflight_live.read_preflight_facts(request, context=context,
                                                mover_available=mover_present(request.mover),
                                                rig_clear_attested=raw.get("attest_rig_clear") is True)
    report = preflight(request, facts)
    issue = next((issue for issue in report.issues if issue.blocking), None)
    if issue is not None:
        raise CrossoverV2Refused(issue.evidence or issue.detail, code=issue.code, next_action=issue.next_action)
    request = report.plan
    captures = prepare_plan_captures(request, roles_bands=context.roles_bands)
    try:
        protection_sections = confirmed_protection_sections(
            context.safety_profile, context.role_targets
        )
    except ValueError as exc:
        raise CrossoverV2Refused("The confirmed driver protection cannot be used for this measurement.",
                                 code="driver_protection_invalid") from exc

    stage1_index_phase = {index: capture.spec.program_phase for index, capture in enumerate(captures, 1)}
    engine_measure_specs = {index: capture.spec for index, capture in enumerate(captures, 1)}
    lateral_prompts = tuple(capture.resolved(request).prompt
        for capture in captures if capture.spec.program_phase == PHASE_LATERAL)
    evidence_store, _bundle_id = v2evidence.open_v2_evidence_store(context.topology)

    acknowledgement_binding = secrets.token_urlsafe(24)
    signals = RunSignals()
    position_gate = PositionGate(mover=request.mover)
    capture_session_id = "wired-" + secrets.token_hex(8)
    schedule = preview_schedule(request, captures, context)
    spec = build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, c.stop.candidate_id) for c in captures],
        acknowledgement_binding=acknowledgement_binding,
        default_setup_calibration=v2evidence.default_setup_calibration_for_v2(),
    )
    evidence_store.publish_json_artifact(f"crossover_v2/{capture_session_id}/plan.json", request.to_dict())
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
            roles=context.roles_bands,
            protection_sections_by_role=protection_sections,
            provenance=capture_provenance,
            program_for_spec=lambda spec, gain: compose_plan_program(conductor, spec, gain),
        )
        seams = bind_v2_stage_seams(
            evidence_store=evidence_store, refs=refs, publish_check=publish_check,
        )
        conductor = CrossoverV2Session(
            session_id=session_id,
            source_preset=context.preset,
            roles_bands=context.roles_bands,
            fc_hz=context.fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            driver_sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
            target_bands=context.driver_bands,
            # The fader a run opens at when no level is asked; the run door replaces it at the first level window.
            session_volume_db=probe_fader_db(context.driver_caps_dbfs),
            seams=seams,
            index_phase_map=stage1_index_phase,
            driver_spacing_m=context.driver_spacing_m,
            lateral_prompts=lateral_prompts,
            measure_specs_by_index=engine_measure_specs,
            measurement_protection_sections_by_role=protection_sections,
        )
        v2state.persist_conductor_state(conductor, failure_code=None, evidence=refs)
        manifest = RunManifest(session_id, v2evidence._record_store(evidence_store, session_id),
                               incumbent=incumbent_fingerprints(load_applied_baseline_profile_state()))
        from jasper.web import correction_crossover_v2 as host  # lazy: bind this host's seams
        tuning, analyze, assessor = bind_run_door(
            host=host, device=device, evidence_store=evidence_store,
            manifest=manifest, production=production_play, conductor=conductor, refs=refs, provenance=capture_provenance,
            ceiling_s=ceiling_s, camilla_factory=camilla_factory, context=context,
            ceiling_db_spl=report.spl_ceiling_db_spl,
        )
        nonlocal held
        source_run = _build_wired_run(
            conductor,
            door=tuning,
            signals=signals,
            position_gate=position_gate,
            evidence_refs=refs,
            ceiling_s=ceiling_s,
            manifest=manifest, analyze=analyze, assessor=assessor,
            request=request, captures=captures,
        )
        held = v2evidence._HeldSession(tuning=tuning, run=source_run)
        return rc

    async def _run(pi_session: Any) -> None:
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
                    SessionVolumeRestoreResult.ALREADY_RESOLVED, "not_opened",
                }
            ):
                closed = mark_state(Path(evidence_store.bundle_dir), "closed")
                if closed is None and completed:
                    raise OSError("the measurement bundle could not be closed")
                if closed is not None and position_gate:
                    await publish_round_packet(Path(evidence_store.bundle_dir), position_gate)

    subject, parameters = run_envelope(request)
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
        staged={"subject": subject, "parameters": parameters, "schedule": report.to_dict()},
    )
