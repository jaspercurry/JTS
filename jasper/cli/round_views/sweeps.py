# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One gate ladder, read over a round set or one take."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping

from jasper.active_speaker.crossover_v2.gate_sweep import summary_lines, sweep_round
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.window_view import window_view
from .frequency import add_image_args, render_image
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    resolve_set, read_run_manifest, round_inputs,
    _write,
    add_rungs_ms_argument,
    add_set_argument, answer,
    omitted_note,
    refused_by_name,
    resolved_out,
    subject,
)


def _frame_parameters(frame: Mapping[str, Any]) -> dict[str, Any]:
    """The ladder and smoothing a sweep's own frame states it used."""
    return {"rungs_ms": frame["rungs_ms"], "smoothing_fraction": frame["smoothing"]["magnitude_fraction"]}


def _cmd_gate_sweep(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = round_inputs(round_dir)
    manifest = read_run_manifest(inputs)
    selected = resolve_set(inputs, args.set, manifest=manifest) if args.set else None
    try:
        report = stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, sweep_round, round_dir,
            rungs_ms=args.rungs_ms, at_hz=args.at_hz or (),
            candidate_id=args.candidate, graph_fingerprint=args.graph, take_ids=selected.selected_ids if selected else None,
        )
    except RoundCapturesRefused as exc:
        # The ladder's own named refusal, never the resolver's coarser bucket.
        return refused_by_name(exc.reason, exc.detail)
    spec = ARTIFACT_BY_VIEW[f"sweep --scope {args.scope}"]
    written = _write(report, args.out, resolved_out(round_dir, spec.artifact, args.set), schema=spec.schema)
    return answer(
        args.command, schema=spec.schema,
        subject=subject(inputs, selected, take_ids=[pose["capture_id"] for pose in report["poses"]],
                        candidate_id=args.candidate),
        parameters={**_frame_parameters(report["frame"]), "at_hz": list(args.at_hz or ())},
        out=written, scope=args.scope, poses=len(report["poses"]),
        omitted=report["omitted"], rungs_ms=report["frame"]["rungs_ms"],
        ladder=report["ladder"], bands=[
            {"band_hz": band["band_hz"], "verdict": band["window_verdict"]}
            for band in report["bands"]
        ],
        features=[
            {"bin_hz": feature["bin_hz"], "verdict": feature["window_verdict"]}
            for feature in report["features"]
        ],
        line=(
            "gate-sweep [evidence only, no grade moves]: "
            + "; ".join(summary_lines(report))
            + (f" -> {written}" if written else "")
            + omitted_note(report["omitted"])
        ),
    )


def _cmd_windows(args: argparse.Namespace) -> int:
    inputs = round_inputs(Path(args.round_dir))
    selected = resolve_set(inputs, args.set)
    take_id = selected.take_id(args.take)
    try:
        report = window_view(Path(args.round_dir), capture_id=take_id, rungs_ms=args.rungs_ms, role=args.role)
    except RoundCapturesRefused as exc:
        return refused_by_name(exc.reason, exc.detail)
    spec = ARTIFACT_BY_VIEW[f"sweep --scope {args.scope}"]
    written = _write(report, args.out, resolved_out(Path(args.round_dir), spec.artifact, args.set), schema=spec.schema)
    run, = report["runs"]
    return answer(args.command, schema=spec.schema, subject=subject(inputs, selected, take_ids=[take_id]),
                  parameters={**_frame_parameters(run["metadata"]["frame"]), "role": args.role},
                  out=written, scope=args.scope, **render_image(args, report), capture_id=take_id,
                  line=f"sweep take: {take_id} -> {written}")


def _cmd_sweep(args: argparse.Namespace) -> int:
    if args.scope == "take" and not args.take:
        args.parser.error("--scope take requires --take")
    if args.scope != "take" and (args.take or args.role != "summed" or args.image):
        args.parser.error("--take, --role and --image require --scope take")
    if args.scope != "round" and (args.candidate or args.graph or args.at_hz):
        args.parser.error("--candidate, --graph and --at-hz require --scope round")
    return {"round": _cmd_gate_sweep, "take": _cmd_windows}[args.scope](args)


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("sweep", help="read the window ladder over a round or take")
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    parser.add_argument("--scope", required=True, choices=("round", "take"))
    add_set_argument(parser)
    parser.add_argument("--take", help="selected take ID within the set; required for take scope")
    parser.add_argument("--role", choices=("summed", "woofer", "tweeter"), default="summed")
    parser.add_argument("--candidate", help="round scope: candidate ID")
    parser.add_argument("--graph", help="round scope: played graph fingerprint")
    parser.add_argument("--at-hz", type=float, nargs="+", metavar="HZ", help="round scope: extra bins")
    add_rungs_ms_argument(parser)
    add_image_args(parser)
    parser.add_argument("--out", help="artifact destination")
    parser.set_defaults(func=_cmd_sweep, parser=parser)
