"""Operator tools for active-speaker commissioning artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jasper.active_speaker import ActiveSpeakerConfigError, ActiveSpeakerPreset
from jasper.active_speaker.camilla_yaml import emit_active_speaker_startup_config
from jasper.dsp_apply import validate_camilla_config


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ActiveSpeakerConfigError(f"could not read preset: {e}") from e
    except json.JSONDecodeError as e:
        raise ActiveSpeakerConfigError(f"preset is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise ActiveSpeakerConfigError("preset JSON must be an object")
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


def _cmd_startup_template(args: argparse.Namespace) -> int:
    preset = ActiveSpeakerPreset.from_mapping(_load_json_object(Path(args.preset)))
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
