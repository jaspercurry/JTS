"""Operator tools for active-speaker commissioning artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from jasper.active_speaker import ActiveSpeakerConfigError, ActiveSpeakerPreset
from jasper.active_speaker.camilla_yaml import emit_active_speaker_startup_config
from jasper.active_speaker.path_safety import (
    build_startup_load_path_safety_evidence,
    evaluate_path_safety_evidence,
    requirements_payload,
    write_path_safety_evidence,
)
from jasper.active_speaker.calibration_level import load_calibration_level_state
from jasper.active_speaker.environment import probe_active_speaker_environment
from jasper.active_speaker.staging import load_staged_startup_config
from jasper.active_speaker.startup_load import (
    build_driver_commission_load_preflight,
    load_commission_load_state,
    load_driver_commissioning_config,
    rollback_driver_commissioning_config,
)
from jasper.active_speaker.commission_ramp import (
    abort_ramp,
    load_ramp_state,
    ramp_audible_step,
    record_ramp_operator_ack,
)
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
)
from jasper.dsp_apply import validate_camilla_config
from jasper.output_topology import load_output_topology


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
        for issue in payload["issues"]:
            print(f"  [{issue['severity']}] {issue['path_id']}: {issue['message']}")


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
        for issue in payload["issues"]:
            print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")


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
        load_output_topology(args.topology),
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


def _camilla_controller() -> Any:
    """Return a CamillaController bound to the live CamillaDSP websocket.

    Mirrors the web wizard's ``_camilla`` factory so an operator running the
    commission-load CLI reaches the same running graph the daemons drive.
    """
    from jasper.camilla import CamillaController

    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


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
        for issue in issues:
            print(f"    [{issue['severity']}] {issue['code']}: {issue['message']}")
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

    topology = load_output_topology(args.topology)
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
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Commission rollback: {rollback.get('status')}")
        print(f"  reloaded staged boot config: {rollback.get('active_config_path')}")
        for issue in rollback.get("issues") or []:
            print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
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
    for issue in payload.get("issues") or []:
        print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")


def _cmd_commission_ramp_step(args: argparse.Namespace) -> int:
    topology = load_output_topology(args.topology)
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
        for issue in payload.get("issues") or []:
            print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
    return 0 if payload.get("status") in {"confirmed", "retry", "aborted"} else 1


def _cmd_commission_ramp_status(args: argparse.Namespace) -> int:
    payload = {
        "commission_load": load_commission_load_state(),
        "ramp": load_ramp_state(),
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
    ramp_status.add_argument("--json", action="store_true")
    ramp_status.set_defaults(func=_cmd_commission_ramp_status)

    ramp_abort = ramp_sub.add_parser(
        "abort", help="re-mute: roll back to the all-muted staged config and reset"
    )
    ramp_abort.add_argument("--json", action="store_true")
    ramp_abort.set_defaults(func=_cmd_commission_ramp_abort)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ActiveSpeakerConfigError, OSError) as e:
        parser.exit(2, f"{parser.prog}: error: {e}\n")


if __name__ == "__main__":
    raise SystemExit(main())
