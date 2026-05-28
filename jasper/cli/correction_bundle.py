"""Inspect and export JTS room-correction bundles."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jasper.correction import bundle_tools, bundles, fir_runtime


def _print_text_summary(payload: dict[str, Any]) -> None:
    print(f"Bundle: {payload.get('bundle_dir')}")
    print(f"Session: {payload.get('session_id')} ({payload.get('state')})")
    confidence = payload.get("confidence") or {}
    if confidence:
        print(
            "Confidence: "
            f"{confidence.get('level')} "
            f"{confidence.get('score')}/100 "
            f"({confidence.get('finding_count')} findings)"
        )
    runtime = payload.get("runtime_integrity") or {}
    if runtime:
        print(
            "Runtime integrity: "
            f"{runtime.get('level')} "
            f"({runtime.get('issue_count')} issues)"
        )
    print(f"Artifacts: {payload.get('artifact_count', 0)}")
    for kind, count in sorted((payload.get("artifact_counts_by_kind") or {}).items()):
        print(f"  {kind}: {count}")
    print(f"Raw captures: {payload.get('raw_capture_count', 0)}")
    exports = payload.get("exports_available") or {}
    print(
        "Exports: "
        f"FR={'yes' if exports.get('frequency_response_text') else 'no'}, "
        f"IR={'yes' if exports.get('impulse_response_wav') else 'no'}"
    )
    fir = payload.get("fir_readiness") or {}
    if fir:
        print(
            "FIR readiness: "
            f"runtime_import={'yes' if fir.get('ready_for_runtime_import') else 'no'}, "
            f"generated_fir={'yes' if fir.get('ready_for_generated_fir') else 'no'}, "
            f"staged={fir.get('staged_fir_count', 0)}"
        )
    recompute = payload.get("recompute")
    if recompute:
        print(
            "Recompute: "
            f"{recompute.get('position_count')} positions, "
            f"{recompute.get('freq_count')} bins"
        )
        delta = recompute.get("stored_average_delta")
        if isinstance(delta, dict) and "rms_db" in delta:
            print(
                "Stored average delta: "
                f"rms={delta['rms_db']} dB, max={delta['max_abs_db']} dB"
            )
    issues = payload.get("issues") or []
    if issues:
        print("Issues:")
        for issue in issues:
            print(
                f"  [{issue.get('severity')}] "
                f"{issue.get('code')}: {issue.get('message')}"
            )


def _cmd_inspect(args: argparse.Namespace) -> int:
    payload = bundle_tools.inspect_bundle(
        Path(args.bundle),
        recompute=args.recompute,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_text_summary(payload)
    return 1 if any(
        issue.get("severity") == "fail"
        for issue in payload.get("issues", [])
    ) else 0


def _cmd_export(args: argparse.Namespace) -> int:
    payload = bundle_tools.export_bundle(
        Path(args.bundle),
        Path(args.output),
        include_ir=not args.no_ir,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Exported {len(payload['written'])} file(s) to {payload['output_dir']}")
        for path in payload["written"]:
            print(f"  {path}")
    return 0


def _cmd_fir_inspect(args: argparse.Namespace) -> int:
    payload = fir_runtime.inspect_fir_wav(
        Path(args.wav),
        mode=args.mode,
        target_sample_rate=args.target_sample_rate,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"FIR: {payload['path']}")
        print(
            f"{payload['tap_count']} taps, {payload['channel_count']} channel(s), "
            f"{payload['sample_rate']} Hz"
        )
        print(
            f"span={payload['span_ms']} ms, "
            f"group_delay={payload['filter_group_delay_ms']} ms, "
            f"headroom={payload['required_headroom_db']} dB"
        )
        for issue in payload.get("issues") or []:
            print(
                f"  [{issue.get('severity')}] "
                f"{issue.get('code')}: {issue.get('message')}"
            )
    return 1 if payload.get("level") == "fail" else 0


def _cmd_fir_stage(args: argparse.Namespace) -> int:
    payload = fir_runtime.stage_fir_artifact(
        bundle_dir=Path(args.bundle),
        source_wav=Path(args.wav),
        label=args.label,
        mode=args.mode,
        target_sample_rate=args.target_sample_rate,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Staged FIR {payload['path']} "
            f"({payload['tap_count']} taps, headroom "
            f"{payload['required_headroom_db']} dB)"
        )
    return 1 if payload.get("level") == "fail" else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-correction-bundle",
        description="Inspect or export a JTS room-correction session bundle",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser(
        "inspect",
        help="validate and summarize one correction bundle",
    )
    inspect_parser.add_argument("bundle", help="path to a session bundle")
    inspect_parser.add_argument(
        "--recompute",
        action="store_true",
        help="replay raw captures and compare stored derived curves",
    )
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(func=_cmd_inspect)

    export_parser = sub.add_parser(
        "export",
        help="write REW-friendly .frd/.txt curves and IR WAVs",
    )
    export_parser.add_argument("bundle", help="path to a session bundle")
    export_parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="directory to write exported files",
    )
    export_parser.add_argument(
        "--no-ir",
        action="store_true",
        help="only export frequency-response text files",
    )
    export_parser.add_argument("--json", action="store_true")
    export_parser.set_defaults(func=_cmd_export)

    fir_inspect = sub.add_parser(
        "fir-inspect",
        help="inspect a FIR coefficient WAV without applying it",
    )
    fir_inspect.add_argument("wav", help="path to a FIR coefficient WAV")
    fir_inspect.add_argument(
        "--mode",
        choices=["minimum_phase", "linear_phase", "mixed_phase", "unknown"],
        default="unknown",
        help="declared FIR mode for latency accounting",
    )
    fir_inspect.add_argument(
        "--target-sample-rate",
        type=int,
        default=fir_runtime.DEFAULT_TARGET_SAMPLE_RATE,
    )
    fir_inspect.add_argument("--json", action="store_true")
    fir_inspect.set_defaults(func=_cmd_fir_inspect)

    fir_stage = sub.add_parser(
        "fir-stage",
        help="copy a FIR coefficient WAV into a bundle without applying it",
    )
    fir_stage.add_argument("bundle", help="path to a session bundle")
    fir_stage.add_argument("wav", help="path to a FIR coefficient WAV")
    fir_stage.add_argument(
        "--label",
        default="imported",
        help="bundle-local coefficient label",
    )
    fir_stage.add_argument(
        "--mode",
        choices=["minimum_phase", "linear_phase", "mixed_phase", "unknown"],
        default="unknown",
    )
    fir_stage.add_argument(
        "--target-sample-rate",
        type=int,
        default=fir_runtime.DEFAULT_TARGET_SAMPLE_RATE,
    )
    fir_stage.add_argument("--json", action="store_true")
    fir_stage.set_defaults(func=_cmd_fir_stage)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        bundle_tools.BundleToolError,
        bundles.BundleError,
        fir_runtime.FirRuntimeError,
    ) as e:
        parser.exit(2, f"{parser.prog}: error: {e}\n")


if __name__ == "__main__":
    raise SystemExit(main())
