# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One gate ladder, read over a verdict, a round set, or one take."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.gate_sweep import summary_lines, sweep_round
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.window_view import window_view
from .frequency import add_image_args, render_image
from jasper.active_speaker.crossover_v2.round_views import spec_with_gate_sensitivity
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _load_round,
    resolve_set, round_inputs,
    _write,
    add_rungs_ms_argument,
    answer,
    refused_by_name,
    resolved_out,
)

def _band_sweep_line(band: Any) -> str:
    """One band's gate read as the operator reads it: which band, then whether
    that band's own worst bin is the room or the speaker.

    ``gate_window_verdict`` is ``None`` only when the ladder never ran on this
    band; a ladder that ran and still could not call it stamps
    ``"unresolved"`` rather than nothing, and that is told apart from "never
    swept" here too.
    """
    label = f"{band.f_lo_hz:g}-{band.f_hi_hz:g} Hz"
    verdict = band.gate_window_verdict
    if verdict is None:
        return f"{label} NOT SWEPT ({band.gate_sensitivity_note})"
    if band.sigma_growth_ratio is None:
        return f"{label} {verdict.upper()} ({band.gate_sensitivity_note})"
    return (
        f"{label} @{band.max_deviation_hz:.1f} Hz {verdict.upper()} "
        f"sigma x{band.sigma_growth_ratio:.2f} over {band.n_valid_rungs} rung(s), "
        f"window {band.gate_sensitivity_db:+.2f} dB"
    )


def _cmd_spec_sweep(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    resolve_set(banked.inputs, args.set)
    report = spec_with_gate_sensitivity(banked, rungs_ms=args.rungs_ms)
    payload = {"round_dir": str(banked.round_dir), "spec": report.to_dict()}
    written = _write(payload, args.out, resolved_out(banked.round_dir, ARTIFACT_BY_VIEW[f"sweep --scope {args.scope}"].artifact, args.set))
    return answer(
        args.command, out=written, scope=args.scope, overall_within_target=report.overall_within_target,
        bands=[
            {
                "band_hz": [band.f_lo_hz, band.f_hi_hz],
                "within_target": band.within_target,
                "gate_window_verdict": band.gate_window_verdict,
                "sigma_growth_ratio": band.sigma_growth_ratio,
                "n_valid_rungs": band.n_valid_rungs,
                "gate_sensitivity_db": band.gate_sensitivity_db,
                "max_deviation_hz": band.max_deviation_hz,
            }
            for band in report.bands
        ],
        line=(
            "spec-sweep [disclosure only, no grade moves]: "
            + "; ".join(_band_sweep_line(band) for band in report.bands)
            + (f" -> {written}" if written else "")
        ),
    )


def _cmd_gate_sweep(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    selected = resolve_set(round_inputs(round_dir), args.set)
    try:
        report = stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, sweep_round, round_dir,
            rungs_ms=args.rungs_ms, at_hz=args.at_hz or (),
            candidate_id=args.candidate, graph_fingerprint=args.graph, take_ids=selected.selected_ids,
        )
    except RoundCapturesRefused as exc:
        # The ladder's own named refusal, never the resolver's coarser bucket.
        return refused_by_name(exc.reason, exc.detail)
    written = _write(
        report, args.out,
        resolved_out(round_dir, ARTIFACT_BY_VIEW[f"sweep --scope {args.scope}"].artifact, args.set),
    )
    return answer(
        args.command, out=written, scope=args.scope, poses=len(report["poses"]),
        rungs_ms=report["frame"]["rungs_ms"],
        bands=[
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
        ),
    )


def _cmd_windows(args: argparse.Namespace) -> int:
    selected = resolve_set(round_inputs(Path(args.round_dir)), args.set)
    take_id = selected.take_id(args.take)
    try:
        report = window_view(Path(args.round_dir), capture_id=take_id, rungs_ms=args.rungs_ms, role=args.role)
    except RoundCapturesRefused as exc:
        return refused_by_name(exc.reason, exc.detail)
    written = _write(report, args.out, resolved_out(Path(args.round_dir), ARTIFACT_BY_VIEW[f"sweep --scope {args.scope}"].artifact, args.set))
    image = render_image(args, report)
    return answer(args.command, out=written, scope=args.scope, image=image, capture_id=take_id,
                  line=f"sweep take: {take_id} -> {written}")


def _cmd_sweep(args: argparse.Namespace) -> int:
    if args.scope == "take" and not args.take:
        args.parser.error("--scope take requires --take")
    if args.scope != "take" and (args.take or args.role != "summed" or args.image):
        args.parser.error("--take, --role and --image require --scope take")
    if args.scope != "round" and (args.candidate or args.graph or args.at_hz):
        args.parser.error("--candidate, --graph and --at-hz require --scope round")
    return {"verdict": _cmd_spec_sweep, "round": _cmd_gate_sweep, "take": _cmd_windows}[args.scope](args)


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("sweep", help="read the window ladder over a verdict, round set, or take")
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    parser.add_argument("--scope", required=True, choices=("verdict", "round", "take"))
    parser.add_argument("--set", help="set in the run manifest")
    parser.add_argument("--take", help="selected take ID within the set; required for take scope")
    parser.add_argument("--role", choices=("summed", "woofer", "tweeter"), default="summed")
    parser.add_argument("--candidate", help="round scope: candidate ID")
    parser.add_argument("--graph", help="round scope: played graph fingerprint")
    parser.add_argument("--at-hz", type=float, nargs="+", metavar="HZ", help="round scope: extra bins")
    add_rungs_ms_argument(parser)
    add_image_args(parser)
    parser.add_argument("--out", help="artifact destination")
    parser.set_defaults(func=_cmd_sweep, parser=parser)
