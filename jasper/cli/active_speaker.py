# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator tools for active-speaker commissioning artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, assert_never

from jasper.active_speaker.profile import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
)
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.active_speaker.camilla_yaml import emit_active_speaker_startup_config
from jasper.active_speaker.environment import (
    DEFAULT_CAMILLA_STATEFILE,
    probe_active_speaker_environment,
    read_camilla_statefile_config_path,
)
from jasper.active_speaker.path_safety import (
    build_startup_load_path_safety_evidence,
    evaluate_path_safety_evidence,
    requirements_payload,
    write_path_safety_evidence,
)
from jasper.active_speaker.calibration_level import load_calibration_level_state
from jasper.active_speaker.runtime_contract import (
    DEFAULT_FLAT_OUTPUTD_CONFIG,
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    PARKED_MUTED_STATUS,
    parked_muted_exits,
    safe_graph_for_current_topology,
)
from jasper.active_speaker.runtime_convergence import (
    converge_boot_statefile,
)
from jasper.active_speaker.staging import load_staged_startup_config
from jasper.active_speaker.startup_load import (
    ReemitAnchorReport,
    describe_safe_graph_for_refusal,
    reemit_staged_startup_anchor,
    startup_anchor_from_decision,
)
from jasper.active_speaker.commission_load import (
    build_driver_commission_load_preflight,
    load_commission_load_state,
    load_driver_commissioning_config,
    rollback_driver_commissioning_config,
)
from jasper.active_speaker.commission_ramp import (
    abort_ramp,
    clear_pending_ramp_step,
    effective_confirmed_roles,
    load_ramp_state,
    ramp_audible_step,
    record_ramp_operator_ack,
)
from jasper.active_speaker.measurement import confirmed_driver_roles
from jasper.active_speaker.commission_wiring import (
    commission_load_config,
    commission_seams,
    read_current_config_path,
    resolve_commission_inputs,
    write_commission_path_safety,
)
from jasper.active_speaker.safe_playback import (
    FLOOR_OPERATOR_OUTCOMES,
    load_safe_playback_state,
    stop_safe_playback_session,
)
from jasper.dsp_apply import validate_camilla_config
from jasper.output_topology import (
    OutputTopology,
    OutputTopologyError,
    load_output_topology_strict,
)


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ActiveSpeakerConfigError(f"could not read {label}: {e}") from e
    except json.JSONDecodeError as e:
        raise ActiveSpeakerConfigError(f"{label} is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise ActiveSpeakerConfigError(f"{label} JSON must be an object")
    return payload


def _print_issues(
    issues: Iterable[Mapping[str, Any]], *, key: str = "code", indent: str = "  "
) -> None:
    for issue in issues:
        print(
            f"{indent}[{issue.get('severity')}] {issue.get(key)}: "
            f"{issue.get('message') or issue.get('detail')}"
        )


def _print_template_summary(payload: dict[str, Any]) -> None:
    print(f"Preset: {payload['preset_id']} ({payload['name']})")
    print(f"Topology: {payload['way_count']}-way {payload['layout']}")
    print(f"Output channels: {payload['output_count']}")
    print(f"Template: {payload['output']}")
    validation = payload.get("validation") or {}
    status = validation.get("status", "skipped")
    print(f"Validation: {status}")
    if status == "missing":
        print("  camilladsp binary not found; syntax preflight skipped")
    elif validation.get("stderr_tail"):
        print(f"  stderr: {validation['stderr_tail']}")


def _print_requirements(payload: dict[str, Any]) -> None:
    print("Active speaker path-safety requirements:")
    for requirement in payload["requirements"]:
        print(f"- {requirement['id']}: {requirement['label']}")
        print(f"  checks: {', '.join(requirement['checks'])}")
        print(f"  why: {requirement['why']}")


def _print_path_audit_summary(payload: dict[str, Any]) -> None:
    print(f"Path safety: {payload['status']}")
    print(f"Evidence source: {payload['evidence_source']}")
    print(
        f"Hardware probe backed: {'yes' if payload['hardware_probe_backed'] else 'no'}"
    )
    print(f"Load gate: {payload['load_gate']}")
    print(
        f"OK to load active config: {'yes' if payload['ok_to_load_active_config'] else 'no'}"
    )
    print(f"Blockers: {payload['blocker_count']}")
    for path in payload["paths"]:
        print(f"- {path['id']}: {path['status']}")
    if payload["issues"]:
        print("Issues:")
        _print_issues(payload["issues"], key="path_id")


def _print_environment_summary(payload: dict[str, Any]) -> None:
    config = payload["camilla_config"]
    alsa = payload["alsa"]
    path_safety = payload["path_safety"]
    validation = payload["camilla_validation"]
    print(f"Active speaker environment: {payload['status']}")
    print(f"Load gate: {payload['load_gate']}")
    print(
        f"OK to load active config: {'yes' if payload['ok_to_load_active_config'] else 'no'}"
    )
    print(
        f"Camilla config: {config['classification']} ({config.get('path') or 'none'})"
    )
    print(f"  {config['label']}")
    print(
        "  playback: "
        f"{config.get('playback_device') or 'unknown'} "
        f"channels={config.get('playback_channels') or 'unknown'} "
        f"volume_limit={config.get('volume_limit_db')!r}"
    )
    print(f"Camilla validation: {validation.get('status', 'unknown')}")
    print(
        "ALSA playback devices: "
        f"{len(alsa.get('devices', []))} "
        f"({'available' if alsa.get('available') else 'unavailable'})"
    )
    print(
        "Path safety: "
        f"{path_safety.get('status', 'unknown')} "
        f"gate={path_safety.get('load_gate', 'unknown')}"
    )
    if payload["issues"]:
        print("Issues:")
        _print_issues(payload["issues"])


def _cmd_startup_template(args: argparse.Namespace) -> int:
    preset = ActiveSpeakerPreset.from_mapping(
        _load_json_object(Path(args.preset), label="preset")
    )
    output = Path(args.output)
    emit_active_speaker_startup_config(
        preset,
        playback_device=args.playback_device,
        out_path=output,
        baseline_id=args.baseline_id,
    )

    validation = None
    if args.check:
        validation = validate_camilla_config(output).to_dict()

    payload: dict[str, Any] = {
        "preset_id": preset.preset_id,
        "name": preset.name,
        "way_count": preset.way_count,
        "layout": preset.channel_map.layout,
        "output_count": len(preset.channel_map.outputs),
        "output": str(output),
        "validation": validation or {"status": "skipped"},
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_template_summary(payload)

    status = payload["validation"].get("status")
    return 1 if status in {"invalid_config", "runner_error", "timeout"} else 0


def _cmd_path_audit(args: argparse.Namespace) -> int:
    if args.requirements:
        payload = requirements_payload()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_requirements(payload)
        return 0
    if not args.evidence:
        raise ActiveSpeakerConfigError(
            "path-audit requires evidence JSON or --requirements"
        )

    payload = evaluate_path_safety_evidence(
        _load_json_object(Path(args.evidence), label="path-safety evidence")
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_path_audit_summary(payload)
    return 0 if payload["requirements_met"] else 1


def _cmd_path_probe(args: argparse.Namespace) -> int:
    evidence = build_startup_load_path_safety_evidence(
        load_output_topology_strict(args.topology),
        staged_config=load_staged_startup_config(),
        calibration_level=load_calibration_level_state(),
        current_config_path=args.current_config,
    )
    evidence_path = write_path_safety_evidence(evidence, path=args.output)
    report = evaluate_path_safety_evidence(evidence)
    payload = {
        "evidence_path": str(evidence_path),
        "report": report,
        "evidence": evidence,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Wrote path-safety evidence: {evidence_path}")
        _print_path_audit_summary(report)
        print("No audio was emitted and CamillaDSP was not reloaded.")
    return 0 if report["ok_to_load_active_config"] else 1


def _cmd_environment_probe(args: argparse.Namespace) -> int:
    payload = probe_active_speaker_environment(
        config_path=args.config,
        statefile_path=args.statefile,
        path_safety_evidence_path=args.path_safety_evidence,
        run_config_check=args.check_config,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_environment_summary(payload)
    return 0 if payload["ok_to_load_active_config"] else 1


def _print_runtime_safe_graph_summary(
    payload: dict[str, Any],
    *,
    wrote_statefile: bool,
    topology: OutputTopology | None = None,
) -> None:
    contract = payload["topology_contract"]
    current = payload.get("current_graph") or {}
    preferred = payload.get("preferred_graph") or {}
    fallback = payload.get("fallback_graph") or {}
    print(f"Runtime graph decision: {payload['status']}")
    print(f"  reason: {payload['reason']}")
    print(
        "  topology: "
        f"{contract['classification']} "
        f"requires_roleful_graph={contract['requires_roleful_graph']}"
    )
    if current:
        print(
            "  current: "
            f"{current.get('classification')} "
            f"allowed={current.get('allowed')} "
            f"path={current.get('config_path')}"
        )
    if preferred:
        print(
            "  preferred: "
            f"{preferred.get('classification')} "
            f"allowed={preferred.get('allowed')} "
            f"path={preferred.get('config_path')}"
        )
    if fallback:
        print(
            "  fallback: "
            f"{fallback.get('classification')} "
            f"allowed={fallback.get('allowed')} "
            f"path={fallback.get('config_path')}"
        )
    if payload.get("selected_config_path"):
        print(f"  selected: {payload['selected_config_path']}")
    # A deliberately-silenced PHYSICAL output deserves a trail. The graph the
    # box is about to boot may hard-mute a DAC output the saved topology does
    # not claim (a mono speaker on a stereo DAC); install's transcript is the
    # only place an operator would ever see that, so name the channels rather
    # than let a silent output look like a fault later.
    for label, graph in (("current", current), ("fallback", fallback)):
        muted = (graph.get("details") or {}).get("hard_muted_outputs")
        if muted:
            print(
                f"  {label} hard-muted outputs: "
                f"{', '.join(str(index) for index in muted)} "
                "(not assigned by the saved topology)"
            )
    print(f"  statefile written: {'yes' if wrote_statefile else 'no'}")
    if payload["status"] == PARKED_MUTED_STATUS:
        # The parked state is an ACTION for the household, not a stack of
        # blockers for an operator to decode. Name the exits and stop —
        # the blocker wall stays for a genuinely unsafe graph.
        #
        # Through the capability-aware helper, not the bare constant: on a DAC
        # with no active outputd lane "finish crossover preview" can never
        # succeed, and offering an impossible action is worse than offering
        # none. The doctor and `/state` already resolve it this way, so this is
        # the third of the three surfaces that name the same exits agreeing on
        # one owner rather than two of them agreeing and one drifting.
        print(f"  next: {parked_muted_exits(topology)}")
    _print_issues(payload.get("issues") or [])


def _cmd_runtime_safe_graph(args: argparse.Namespace) -> int:
    result = converge_boot_statefile(
        topology_path=args.topology,
        statefile_path=args.statefile,
        current_config_path=args.current_config,
        flat_config_path=args.flat_config,
        applied_baseline_path=args.applied_baseline_state,
        staged_metadata_path=args.staged_metadata,
        consider_applied_baseline=not args.no_applied_baseline,
        write_statefile=args.write_statefile,
    )
    if result.error is not None:
        print(f"Runtime graph decision: {result.decision.status}")
        print(f"  ERROR: {result.error}")
        return 1
    payload = result.decision.to_dict()
    payload["statefile_written"] = result.statefile_written
    payload["statefile_path"] = args.statefile
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_runtime_safe_graph_summary(
            payload,
            wrote_statefile=result.statefile_written,
            # The same topology the decision was made from, so the exits named
            # here cannot come from a second, differently-read topology.
            topology=result.topology,
        )
    return 0 if result.decision.ok else 1


def _baseline_reemit_endpoint(
    topology: Any, endpoint: str | None
) -> tuple[str | None, str]:
    """Return the playback device and whether it was explicitly requested."""
    from jasper.active_speaker.playback_route import resolve_active_playback_device
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    if endpoint == "ring":
        return RING_ACTIVE_PLAYBACK_DEVICE, "explicit_endpoint_ring"
    if endpoint:
        # An explicit endpoint this function does not recognise must NOT fall
        # through to the auto-resolver. Falling through would answer the ring
        # anyway and report the provenance as auto — so a caller asking for a
        # retired endpoint (`aloop`) would be told it got what it asked for.
        # argparse's `choices` is the only entry point today, but proving that
        # stays true is more expensive than refusing here.
        raise ValueError(
            f"unrecognised playback endpoint {endpoint!r}: "
            f"{RING_ACTIVE_PLAYBACK_DEVICE} is the one legal ACTIVE endpoint"
        )
    return resolve_active_playback_device(topology)


def _print_startup_anchor_reemit(
    report: ReemitAnchorReport, args: argparse.Namespace
) -> int:
    """Render one anchor re-emit report; operator text lives here, not in the engine."""
    if report.reason is None:
        if args.json:
            print(json.dumps({
                "playback_device": report.device,
                "playback_device_source": report.source,
                "classification": report.classification,
                "preview": report.preview,
                "written_path": str(report.written_path),
                "statefile_path": str(args.statefile),
                "statefile_written": report.statefile_written,
                "bytes": report.byte_count,
                "reemitted": "staged_startup_anchor",
            }, indent=2, sort_keys=True))
            return 0
        print(f"Re-staged all-muted startup anchor against playback_device={report.device}")
        print(f"  source:         {report.source}")
        print(f"  classification: {report.classification}")
        print(f"  bytes:          {report.byte_count}")
        if report.preview:
            print(f"  PREVIEW only:   {report.written_path}")
            print("  (live artifact, staged metadata and statefile untouched)")
        else:
            print(f"  wrote:          {report.written_path}")
            print("  statefile:      " + (
                f"repointed -> {report.written_path}"
                if report.statefile_written
                else "already correct"
            ))
        return 0

    if report.reason == "commission_load_active":
        next_step = (
            "A per-driver commissioning config is loaded, and this command "
            "republishes the all-muted anchor that commission-rollback / "
            "commission-ramp abort / `ack --outcome too_loud` reload. Run "
            "`commission-rollback` first, or pass --force."
        )
        if args.json:
            print(json.dumps({
                "status": "refused",
                "reason": report.reason,
                "active_target": report.active_target,
                "candidate_config_path": report.candidate_config_path,
                "next_step": next_step,
            }, indent=2, sort_keys=True))
            return 1
        print("Re-emit refused: a per-driver commissioning load is active.")
        print(f"  active target: {report.active_target}")
        print(f"  {next_step}")
        return 1

    if report.reason == "stage_failed":
        print(
            "ERROR: could not re-stage the all-muted startup anchor against "
            f"{report.device}; NOTHING was written"
        )
    elif report.reason == "reproof_failed":
        print(
            "ERROR: the re-staged startup anchor did not re-prove as "
            f"{GRAPH_ALL_MUTED_ACTIVE_STARTUP}; NOTHING was written"
        )
        print(f"  found:  {report.detail}")
    elif report.reason == "out_parent_missing":
        print(f"ERROR: parent directory does not exist: {report.detail}")
    elif report.reason == "lock_contended":
        print(
            "ERROR: another writer is publishing the startup anchor "
            f"({report.detail}); NOTHING was written"
        )
    else:
        assert_never(report.reason)
    _print_issues(report.issues)
    return 1


def _cmd_baseline_reemit(args: argparse.Namespace) -> int:
    """Re-emit the applied baseline, or re-stage the saved startup anchor."""
    from jasper.active_speaker.baseline_profile import (
        applied_bass_extension,
        load_applied_baseline_profile_state,
        promote_applied_baseline_candidate,
        recompose_applied_baseline_yaml,
    )
    from jasper.active_speaker.runtime_contract import (
        classify_bass_extension_graph,
        write_camilla_statefile,
    )
    from jasper.atomic_io import atomic_write_text

    topology = load_output_topology_strict(args.topology)
    applied = load_applied_baseline_profile_state(args.applied_baseline_state)
    device, source = _baseline_reemit_endpoint(topology, args.endpoint)
    if not device:
        print(
            "ERROR: this topology resolves no active playback endpoint, so there "
            "is no device to re-emit against"
        )
        return 1

    if not applied:
        # No applied baseline is the fleet-typical MID-COMMISSION state, not a
        # broken one — so ask what this box is actually booting from before
        # refusing. An all-muted startup anchor is a legal roleful boot graph and
        # gets step 1; anything else is named and refused.
        decision = safe_graph_for_current_topology(
            topology,
            statefile_path=args.statefile,
            applied_baseline_path=baseline_profile_state_path(
                args.applied_baseline_state
            ),
        )
        if startup_anchor_from_decision(decision) is not None:
            report = reemit_staged_startup_anchor(
                topology, device=device, source=source, out=args.out,
                force=args.force, statefile=args.statefile,
                applied_baseline_state=args.applied_baseline_state,
            )
            return _print_startup_anchor_reemit(report, args)
        print(
            "ERROR: no APPLIED active-speaker baseline profile is saved, and this "
            "box is not on the all-muted active startup graph either, so there is "
            "nothing to re-emit"
        )
        print(f"  found:  {describe_safe_graph_for_refusal(decision)}")
        print(
            f"  accepts: an applied baseline ({GRAPH_APPROVED_ACTIVE_RUNTIME}) or "
            f"the all-muted startup anchor ({GRAPH_ALL_MUTED_ACTIVE_STARTUP})"
        )
        print(
            "  next:   commission the speaker at http://jts.local/sound/speaker/ "
            "(stage a protected startup config), then re-run this command"
        )
        return 1

    # Bass evidence is split exactly as the /sound recompose splits it: only an
    # ACCEPTED profile is emitted, while the proof is asked against whatever was
    # evaluated, so a rejected profile cannot be silently emitted OR silently
    # excused.
    yaml, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        playback_device=device,
        out_path=None,
        bass_extension=applied_bass_extension(applied),
    )
    if yaml is None or issues:
        print("ERROR: could not re-emit the applied baseline:")
        _print_issues(issues or [])
        return 1

    # RE-PROOF before any byte lands. This graph is about to become the box's
    # boot graph, so it is held to the same contract the runtime holds a loaded
    # graph to — and it is re-derived here rather than trusted from the emitter,
    # because the emitter is the thing being checked.
    graph = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=yaml,
        applied_baseline_state=applied,
    )
    if not graph.allowed or graph.classification != GRAPH_APPROVED_ACTIVE_RUNTIME:
        print(
            "ERROR: the re-emitted baseline did not re-prove as "
            f"{GRAPH_APPROVED_ACTIVE_RUNTIME} (got {graph.classification}); "
            "NOTHING was written"
        )
        _print_issues(graph.issues)
        return 1

    preview_path = Path(args.out) if args.out else None
    written_path: Path | None = None
    statefile_written = False
    if preview_path is not None:
        if not preview_path.parent.exists():
            print(
                f"ERROR: parent directory does not exist: {preview_path.parent}"
            )
            return 1
        atomic_write_text(preview_path, yaml, mode=0o640)
        written_path = preview_path
    else:
        applied_config = applied.get("config")
        raw_target = (
            applied_config.get("path") if isinstance(applied_config, Mapping) else None
        )
        if not isinstance(raw_target, str) or not raw_target.strip():
            print(
                "ERROR: the applied baseline profile records no config path, so "
                "there is no artifact to re-emit over; NOTHING was written"
            )
            return 1
        target = Path(raw_target)
        # Preserve the target's own mode when it exists (this rewrites a file
        # someone else created); fall back to the module's 0640 convention when
        # it does not.
        try:
            target_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            target_mode = 0o640
        atomic_write_text(
            target,
            yaml,
            mode=target_mode,
            durable=True,
        )
        written_path = target
        # Keep the canonical readable copy in step (fail-soft by its own
        # contract), then make sure the boot pointer names the artifact we just
        # rewrote — idempotent when it already does.
        promote_applied_baseline_candidate(applied)
        statefile = Path(args.statefile)
        if read_camilla_statefile_config_path(statefile) != str(target):
            write_camilla_statefile(statefile, target)
            statefile_written = True

    payload = {
        "playback_device": device,
        "playback_device_source": source,
        "classification": graph.classification,
        "preview": preview_path is not None,
        "written_path": str(written_path) if written_path else None,
        "statefile_path": str(args.statefile),
        "statefile_written": statefile_written,
        "bytes": len(yaml),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Re-emitted applied baseline against playback_device={device}")
        print(f"  source:         {source}")
        print(f"  classification: {graph.classification}")
        print(f"  bytes:          {len(yaml)}")
        if preview_path is not None:
            print(f"  PREVIEW only:   {preview_path}")
            print("  (live artifact, canonical copy and statefile untouched)")
        else:
            print(f"  wrote:          {written_path}")
            print(
                "  statefile:      "
                + (
                    f"repointed -> {written_path}"
                    if statefile_written
                    else "already correct"
                )
            )
    return 0


def _camilla_controller() -> Any:
    """Return a CamillaController bound to the live CamillaDSP websocket.

    An operator running the commission-load CLI reaches the same running graph
    as the daemons and web wizards.
    """
    from jasper.camilla import primary_controller

    return primary_controller()


def _resolve_commission_inputs(
    args: argparse.Namespace,
) -> tuple[ActiveSpeakerPreset | None, dict[str, Any] | None]:
    """Resolve (preset, crossover_preview) for a commission command.

    Loads the optional ``--preset`` file (CLI-specific), then delegates to the
    shared :func:`resolve_commission_inputs` so the preview/fallback choice
    matches what protected staging and the web card do.
    """
    preset = (
        ActiveSpeakerPreset.from_mapping(
            _load_json_object(Path(args.preset), label="preset")
        )
        if args.preset
        else None
    )
    return resolve_commission_inputs(preset)


def _print_commission_load_summary(payload: dict[str, Any], *, dry_run: bool) -> None:
    load = payload.get("load") or {}
    preflight = payload.get("preflight") or {}
    target = (load.get("target") or preflight.get("target") or {})
    if dry_run:
        print(f"Commission-load preflight: {preflight.get('status')}")
        print(
            f"  load_allowed: {'yes' if preflight.get('load_allowed') else 'no'}"
        )
    else:
        print(f"Commission load: {load.get('status')}")
    print(
        f"  target: group={target.get('speaker_group_id')} "
        f"role={target.get('role')} outputs={target.get('audible_outputs')}"
    )
    candidate = load.get("candidate_config_path") or preflight.get(
        "candidate_config_path"
    )
    print(f"  candidate config: {candidate}")
    if not dry_run:
        print(f"  rollback anchor (staged boot config): {load.get('previous_config_path')}")
        print(
            "  durable statefile intact (crash-recovery-MUTED): "
            f"{load.get('durable_statefile_intact')}"
        )
        live = load.get("live_evidence") or {}
        print(
            "  live read-back gate: "
            f"{'passed' if live.get('passed') else 'failed/none'}"
        )
    gates = preflight.get("required_gates") or []
    failed_gates = [g for g in gates if not g.get("passed")]
    if failed_gates:
        print("  failed gates:")
        for gate in failed_gates:
            print(f"    - {gate['id']}: {gate.get('message')}")
    issues = load.get("issues") or preflight.get("issues") or []
    if issues:
        print("  issues:")
        _print_issues(issues, indent="    ")
    if not dry_run and load.get("status") == "loaded":
        print(
            "Armed at the protected floor (gain -120 dB, mute off) — SILENT. "
            "The audible level is the Stage-5 ramp; no audio was emitted by this load."
        )


def _commission_load_exit_code(payload: dict[str, Any], *, dry_run: bool) -> int:
    if dry_run:
        return 0 if (payload.get("preflight") or {}).get("load_allowed") else 1
    return 0 if (payload.get("load") or {}).get("status") == "loaded" else 1


def _cmd_commission_load(args: argparse.Namespace) -> int:
    # Single-flight: an armed per-driver commissioning load is exclusive. The
    # commissioning config path is shared, so refuse a second concurrent arm
    # rather than silently overwrite a live load — roll back first. (Stage-5
    # gain-ramp re-loads of the SAME armed target go through their own command,
    # not this one.)
    existing = load_commission_load_state()
    if existing.get("status") == "loaded" and not args.force:
        refusal = {
            "status": "refused",
            "reason": "commission_load_already_active",
            "active_target": existing.get("target"),
            "candidate_config_path": existing.get("candidate_config_path"),
            "next_step": (
                "A per-driver commissioning config is already loaded. Run "
                "`commission-rollback` to return to the all-muted staged config, "
                "or pass --force to re-arm."
            ),
        }
        if args.json:
            print(json.dumps(refusal, indent=2, sort_keys=True))
        else:
            print("Commission load refused: a load is already active.")
            print(f"  active target: {existing.get('target')}")
            print(f"  {refusal['next_step']}")
        return 1

    topology = load_output_topology_strict(args.topology)
    staged = load_staged_startup_config()
    preset, crossover_preview = _resolve_commission_inputs(args)
    cam = _camilla_controller()

    async def _run() -> dict[str, Any]:
        current_config_path, current_config_error = await read_current_config_path(cam)
        evidence_path = write_commission_path_safety(
            topology, staged, current_config_path, current_config_error
        )
        if args.dry_run:
            return {
                "preflight": build_driver_commission_load_preflight(
                    topology,
                    speaker_group_id=args.group,
                    role=args.role,
                    staged_config=staged,
                    preset=preset,
                    crossover_preview=crossover_preview,
                    path_safety_evidence_path=evidence_path,
                    current_config_path=current_config_path,
                ),
                "load": {},
            }
        load_config, read_running_config, get_current_config_path = commission_seams(cam)
        return await load_driver_commissioning_config(
            topology,
            speaker_group_id=args.group,
            role=args.role,
            load_config=load_config,
            read_running_config=read_running_config,
            get_current_config_path=get_current_config_path,
            preset=preset,
            crossover_preview=crossover_preview,
            staged_config=staged,
            path_safety_evidence_path=evidence_path,
        )

    payload = asyncio.run(_run())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_commission_load_summary(payload, dry_run=args.dry_run)
    return _commission_load_exit_code(payload, dry_run=args.dry_run)


def _cmd_commission_rollback(args: argparse.Namespace) -> int:
    cam = _camilla_controller()
    payload = asyncio.run(
        rollback_driver_commissioning_config(
            load_config=commission_load_config(cam),
        )
    )
    rollback = payload.get("rollback") or {}
    if rollback.get("status") == "rolled_back":
        # The graph is proven back on the all-muted anchor, so the step the ramp
        # was waiting on is gone with it. Only a proven rollback clears it: a
        # blocked / failed one may still be audible.
        payload["ramp"] = clear_pending_ramp_step()
    # Unconditional, exactly as the web twin and `abort_ramp` do it: a rollback
    # ATTEMPT ends the operator's playback authority whatever the graph did, and
    # revoking it only ever makes the ramp gate stricter. Without this the CLI
    # would be the one re-mute path that clears the step but leaves the floor
    # tri-state armed, so `commission-ramp status` would print "pending step:
    # None" beside "floor_pending_operator" until the session's TTL expired.
    payload["safe_playback"] = stop_safe_playback_session(
        reason="commission_rollback"
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Commission rollback: {rollback.get('status')}")
        print(f"  reloaded staged boot config: {rollback.get('active_config_path')}")
        _print_issues(rollback.get("issues") or [])
    return 0 if rollback.get("status") in {"rolled_back", "blocked"} else 1


def _print_ramp_step_summary(payload: dict[str, Any]) -> None:
    status = payload.get("status")
    print(f"Stage-5 ramp step: {status}")
    print(
        f"  target: group={payload.get('speaker_group_id')} role={payload.get('role')}"
    )
    gate = payload.get("gate") or {}
    if gate:
        print(
            "  gain: "
            f"{gate.get('current_gain_db')} -> {gate.get('next_gain_db')} dB"
        )
        failed = sorted(k for k, ok in (gate.get("checks") or {}).items() if not ok)
        if failed:
            print(f"  gate failed: {', '.join(failed)}")
    safe = payload.get("safe_playback") or {}
    if safe:
        print(
            "  per-driver floor: "
            f"{safe.get('floor_status')} (awaiting operator ACK)"
        )
    if status == "stepped":
        print(
            "  The driver is now AUDIBLE at this level. Confirm by ear, then run "
            "`commission-ramp ack --outcome heard_correct_driver` (or too_loud / "
            "silent / heard_wrong_driver). `commission-ramp abort` re-mutes."
        )
    _print_issues(payload.get("issues") or [])


def _cmd_commission_ramp_step(args: argparse.Namespace) -> int:
    topology = load_output_topology_strict(args.topology)
    staged = load_staged_startup_config()
    preset, crossover_preview = _resolve_commission_inputs(args)
    cam = _camilla_controller()

    async def _run() -> dict[str, Any]:
        current_config_path, current_config_error = await read_current_config_path(cam)
        evidence_path = write_commission_path_safety(
            topology, staged, current_config_path, current_config_error
        )
        load_config, read_running_config, get_current_config_path = commission_seams(cam)
        return await ramp_audible_step(
            topology,
            speaker_group_id=args.group,
            role=args.role,
            load_config=load_config,
            read_running_config=read_running_config,
            get_current_config_path=get_current_config_path,
            preset=preset,
            crossover_preview=crossover_preview,
            path_safety_evidence_path=evidence_path,
            staged_config=staged,
            confirmed_roles=confirmed_driver_roles(
                topology,
                speaker_group_id=args.group,
            ),
        )

    payload = asyncio.run(_run())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_ramp_step_summary(payload)
    return 0 if payload.get("status") == "stepped" else 1


def _cmd_commission_ramp_ack(args: argparse.Namespace) -> int:
    cam = _camilla_controller()
    # load_config lets terminal by-ear outcomes re-mute the transient graph.
    payload = asyncio.run(
        record_ramp_operator_ack(
            outcome=args.outcome,
            load_config=commission_load_config(cam),
        )
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Stage-5 ramp ack ({args.outcome}): {payload.get('status')}")
        safe = payload.get("safe_playback") or {}
        if safe:
            print(f"  per-driver floor: {safe.get('floor_status')}")
        rollback = payload.get("rollback")
        if rollback:
            print(f"  re-muted via rollback: {rollback.get('status')}")
        _print_issues(payload.get("issues") or [])
    return 0 if payload.get("status") in {"confirmed", "retry", "aborted"} else 1


def _cmd_commission_ramp_status(args: argparse.Namespace) -> int:
    commission = load_commission_load_state()
    ramp = load_ramp_state()
    target = commission.get("target") or {}
    group = str(
        target.get("speaker_group_id") or ramp.get("speaker_group_id") or ""
    ).strip()
    durable_confirmed: list[str] = []
    if group:
        try:
            topology = load_output_topology_strict(args.topology)
        except OutputTopologyError:
            durable_confirmed = []
        else:
            durable_confirmed = confirmed_driver_roles(topology, speaker_group_id=group)
    payload = {
        "commission_load": commission,
        "ramp": {
            **ramp,
            "confirmed_roles": effective_confirmed_roles(
                ramp,
                speaker_group_id=group,
                confirmed_roles=durable_confirmed,
            ),
        },
        "safe_playback": load_safe_playback_state(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        commission = payload["commission_load"]
        ramp = payload["ramp"]
        quiet = (payload["safe_playback"].get("quiet_start") or {})
        target = commission.get("target") or {}
        print(f"Commission load: {commission.get('status')}")
        print(
            f"  armed target: group={target.get('speaker_group_id')} "
            f"role={target.get('role')} gain={target.get('audible_gain_db')} dB"
        )
        print(f"Ramp: confirmed_roles={ramp.get('confirmed_roles')}")
        print(f"  pending step: {ramp.get('pending')}")
        print(f"Per-driver floor tri-state: {quiet.get('status')}")
    return 0


def _cmd_commission_ramp_abort(args: argparse.Namespace) -> int:
    cam = _camilla_controller()
    payload = asyncio.run(abort_ramp(load_config=commission_load_config(cam)))
    rollback = payload.get("rollback") or {}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Stage-5 ramp abort: {payload.get('status')}")
        print(f"  re-muted via rollback: {rollback.get('status')}")
    return 0 if rollback.get("status") in {"rolled_back", "blocked"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-active-speaker",
        description="Generate and inspect active-speaker commissioning artifacts",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    template = sub.add_parser(
        "startup-template",
        help="write a muted/protected active-speaker CamillaDSP startup template",
    )
    template.add_argument("preset", help="path to an active-speaker preset JSON file")
    template.add_argument(
        "--playback-device",
        required=True,
        help="explicit active-hardware playback device, e.g. hw:MultiChannelDAC",
    )
    template.add_argument(
        "--output",
        "-o",
        required=True,
        help="path to write the generated CamillaDSP YAML",
    )
    template.add_argument(
        "--baseline-id",
        help="optional baseline id embedded in the generated template comment",
    )
    template.add_argument(
        "--check",
        dest="check",
        action="store_true",
        default=True,
        help="run camilladsp --check when the binary is available (default)",
    )
    template.add_argument(
        "--no-check",
        dest="check",
        action="store_false",
        help="write the template without CamillaDSP syntax preflight",
    )
    template.add_argument("--json", action="store_true")
    template.set_defaults(func=_cmd_startup_template)

    path_audit = sub.add_parser(
        "path-audit",
        help="evaluate or list active-speaker audible-path safety gates",
    )
    path_audit.add_argument(
        "evidence",
        nargs="?",
        help="path to path-safety evidence JSON",
    )
    path_audit.add_argument(
        "--requirements",
        action="store_true",
        help="print the required audible-path evidence checklist",
    )
    path_audit.add_argument("--json", action="store_true")
    path_audit.set_defaults(func=_cmd_path_audit)

    path_probe = sub.add_parser(
        "path-probe",
        help="generate no-audio startup-load path-safety evidence",
    )
    path_probe.add_argument(
        "--topology",
        help="optional output-topology JSON path (default: JTS output topology state)",
    )
    path_probe.add_argument(
        "--current-config",
        help=(
            "current CamillaDSP config path to treat as the rollback target; "
            "omitting it writes blocked evidence"
        ),
    )
    path_probe.add_argument(
        "--output",
        "-o",
        help=(
            "where to write path-safety evidence "
            "(default: JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE or /var/lib/jasper)"
        ),
    )
    path_probe.add_argument("--json", action="store_true")
    path_probe.set_defaults(func=_cmd_path_probe)

    environment = sub.add_parser(
        "environment-probe",
        help="read active-speaker environment evidence without playback or reloads",
    )
    environment.add_argument(
        "--config",
        help=(
            "CamillaDSP config to inspect; when omitted, read config_path from "
            "the CamillaDSP statefile"
        ),
    )
    environment.add_argument(
        "--statefile",
        help=(
            "CamillaDSP statefile to read when --config is omitted "
            "(default: JASPER_CAMILLA_STATEFILE or outputd-statefile.yml)"
        ),
    )
    environment.add_argument(
        "--path-safety-evidence",
        help="optional active-speaker path-safety evidence JSON",
    )
    environment.add_argument(
        "--check-config",
        dest="check_config",
        action="store_true",
        default=True,
        help="run camilladsp --check on the inspected config when available (default)",
    )
    environment.add_argument(
        "--no-check-config",
        dest="check_config",
        action="store_false",
        help="skip CamillaDSP config validation; load gate will remain blocked",
    )
    environment.add_argument("--json", action="store_true")
    environment.set_defaults(func=_cmd_environment_probe)

    runtime = sub.add_parser(
        "runtime-safe-graph",
        help=(
            "select the safe persisted CamillaDSP graph for the saved output "
            "topology; optionally repair the outputd statefile"
        ),
    )
    runtime.add_argument(
        "--topology",
        help="optional output-topology JSON path (default: JTS output topology state)",
    )
    runtime.add_argument(
        "--statefile",
        default=str(DEFAULT_CAMILLA_STATEFILE),
        help="outputd CamillaDSP statefile to inspect/write",
    )
    runtime.add_argument(
        "--current-config",
        help="current CamillaDSP config path; when omitted, read --statefile",
    )
    runtime.add_argument(
        "--flat-config",
        default=str(DEFAULT_FLAT_OUTPUTD_CONFIG),
        help="normal full-range outputd config path",
    )
    runtime.add_argument(
        "--applied-baseline-state",
        help=(
            "saved active-speaker baseline profile state to prefer when it "
            "has status=applied (default: active_speaker_baseline_profile.json)"
        ),
    )
    runtime.add_argument(
        "--no-applied-baseline",
        action="store_true",
        help="ignore any saved applied active-speaker baseline profile",
    )
    runtime.add_argument(
        "--staged-metadata",
        help=(
            "active-speaker staged metadata path "
            "(default: JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH or /var/lib/jasper)"
        ),
    )
    runtime.add_argument(
        "--write-statefile",
        action="store_true",
        help="write --statefile to the selected safe config path",
    )
    runtime.add_argument("--json", action="store_true")
    runtime.set_defaults(func=_cmd_runtime_safe_graph)

    reemit = sub.add_parser(
        "baseline-reemit",
        help="re-emit the active baseline or all-muted startup anchor (--endpoint ring)",
        description=(
            f"Re-emit '{GRAPH_APPROVED_ACTIVE_RUNTIME}' from its applied snapshot, "
            f"or re-stage '{GRAPH_ALL_MUTED_ACTIVE_STARTUP}' from the saved design "
            "draft and crossover preview. An applied baseline takes precedence; "
            "other graph classes are refused. Use --endpoint ring before hardware "
            "and fan-in coupling reconciliation: the endpoint marker follows the "
            "loaded graph. No rollback endpoint is supported."
        ),
    )
    reemit.add_argument(
        "--topology",
        help="optional output-topology JSON path (default: JTS output topology state)",
    )
    reemit.add_argument(
        "--applied-baseline-state",
        help=(
            "saved active-speaker baseline profile state "
            "(default: active_speaker_baseline_profile.json)"
        ),
    )
    reemit.add_argument(
        "--endpoint",
        choices=("ring",),
        help=(
            "emit against the ACTIVE ring; omit to use the resolved playback endpoint"
        ),
    )
    reemit.add_argument(
        "--statefile",
        default=str(DEFAULT_CAMILLA_STATEFILE),
        help="CamillaDSP statefile to point at the re-emitted artifact",
    )
    reemit.add_argument(
        "--out",
        help=(
            "PREVIEW: write the re-emitted YAML here and touch nothing else — "
            "no live artifact, no canonical copy, no statefile"
        ),
    )
    reemit.add_argument(
        "--force",
        action="store_true",
        help=(
            "re-stage the startup anchor even while a per-driver commissioning "
            "load is active. Refused by default: this command republishes the "
            "all-muted anchor that commission-rollback and the Stage-5 ramp's "
            "abort / `ack --outcome too_loud` reload, so moving it mid-load "
            "re-points the operator's own stop control"
        ),
    )
    reemit.add_argument("--json", action="store_true")
    reemit.set_defaults(func=_cmd_baseline_reemit)

    commission_load = sub.add_parser(
        "commission-load",
        help=(
            "load a per-driver commissioning config into the RUNNING CamillaDSP "
            "graph (armed at the protected floor — SILENT)"
        ),
    )
    commission_load.add_argument(
        "--group",
        required=True,
        help="speaker group id to commission (must be the single active group)",
    )
    commission_load.add_argument(
        "--role",
        required=True,
        help="driver role to arm audible (e.g. woofer, tweeter)",
    )
    commission_load.add_argument(
        "--preset",
        help=(
            "optional preset JSON override (preset-fallback mode); default loads "
            "the saved crossover preview to match protected staging"
        ),
    )
    commission_load.add_argument(
        "--topology",
        help="optional output-topology JSON path (default: JTS output topology state)",
    )
    commission_load.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "run the guarded preflight only (writes the candidate config; loads "
            "nothing, emits no audio)"
        ),
    )
    commission_load.add_argument(
        "--force",
        action="store_true",
        help="re-arm even if a commissioning load is already active (single-flight override)",
    )
    commission_load.add_argument("--json", action="store_true")
    commission_load.set_defaults(func=_cmd_commission_load)

    commission_rollback = sub.add_parser(
        "commission-rollback",
        help=(
            "reload the all-muted staged config, ending a per-driver "
            "commissioning load (returns the speaker to everything-muted)"
        ),
    )
    commission_rollback.add_argument("--json", action="store_true")
    commission_rollback.set_defaults(func=_cmd_commission_rollback)

    ramp = sub.add_parser(
        "commission-ramp",
        help=(
            "Stage-5: raise an armed driver from the silent floor to a low audible "
            "level, one gated step at a time (operator-confirmed, woofer first)"
        ),
    )
    ramp_sub = ramp.add_subparsers(dest="ramp_action", required=True)

    ramp_step = ramp_sub.add_parser(
        "step", help="take one gated audible gain step on the armed driver"
    )
    ramp_step.add_argument("--group", required=True, help="armed speaker group id")
    ramp_step.add_argument("--role", required=True, help="armed driver role")
    ramp_step.add_argument(
        "--preset",
        help="optional preset JSON override (must match the armed load)",
    )
    ramp_step.add_argument("--topology", help="optional output-topology JSON path")
    ramp_step.add_argument("--json", action="store_true")
    ramp_step.set_defaults(func=_cmd_commission_ramp_step)

    ramp_ack = ramp_sub.add_parser(
        "ack", help="record the operator's verdict for the pending audible step"
    )
    ramp_ack.add_argument(
        "--outcome",
        required=True,
        choices=sorted(FLOOR_OPERATOR_OUTCOMES),
        help=(
            "heard_correct_driver confirms; too_loud / heard_wrong_driver re-mute; "
            "silent allows a louder retry"
        ),
    )
    ramp_ack.add_argument("--json", action="store_true")
    ramp_ack.set_defaults(func=_cmd_commission_ramp_ack)

    ramp_status = ramp_sub.add_parser(
        "status", help="show the commission-load, ramp, and per-driver floor state"
    )
    # The handler reads args.topology to merge durable confirmed-role evidence
    # for the armed group; without this flag the read raises AttributeError on
    # every box that has ever armed a driver (the armed target outlives a
    # rollback), which is the whole life of the verb after the first arm.
    ramp_status.add_argument("--topology", help="optional output-topology JSON path")
    ramp_status.add_argument("--json", action="store_true")
    ramp_status.set_defaults(func=_cmd_commission_ramp_status)

    ramp_abort = ramp_sub.add_parser(
        "abort", help="re-mute: roll back to the all-muted staged config and reset"
    )
    ramp_abort.add_argument("--json", action="store_true")
    ramp_abort.set_defaults(func=_cmd_commission_ramp_abort)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Commissioning applies the candidate graph inline (commission_load_config
    # -> set_active_config_raw), so its swap duck needs a canonical target.
    from jasper.volume_coordinator import install_env_canonical_target_provider

    install_env_canonical_target_provider()

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ActiveSpeakerConfigError, OutputTopologyError, OSError) as e:
        parser.exit(2, f"{parser.prog}: error: {e}\n")


if __name__ == "__main__":
    raise SystemExit(main())
