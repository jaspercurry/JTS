"""Operator tools for active-speaker commissioning artifacts."""

from __future__ import annotations

import argparse
import json
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
