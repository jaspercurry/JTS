# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Replay retained bass captures on the laptop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import ARTIFACT_BY_VIEW, _ROUND_TOOL_ERRORS, _write, answer, default_out
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs


def _cmd_bass(args: argparse.Namespace) -> int:
    from jasper.active_speaker.measurement_bass import bass_view  # lazy: laptop FFT analysis

    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, args.round_dir)
    payload = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, bass_view, inputs.session_dir,
                    calibration_root=args.calibration_root)
    written = _write(payload, args.out, default_out(inputs, args.round_dir, ARTIFACT_BY_VIEW[args.command].artifact))
    return answer(args.command, out=written, takes=len(payload["takes"]),
                  line=f"bass: {len(payload['takes'])} take(s) -> {written}")


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("bass", help="replay bass fundamental, quiet-window SNR and H2/H3 (laptop)")
    parser.add_argument("round_dir", type=Path)
    parser.add_argument("--calibration-root", type=Path, help="copied microphone calibration registry")
    parser.add_argument("--out", help="artifact destination (- for stdout)")
    parser.set_defaults(func=_cmd_bass)
    compare = sub.add_parser("bass-compare", help="compare two exact takes on common qualified bass bins")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--before-take", required=True)
    compare.add_argument("--after-take", required=True)
    compare.add_argument("--change", required=True, choices=("candidate", "volume", "demand", "diagnostic"))
    compare.add_argument("--out")
    compare.set_defaults(func=_cmd_compare)


def _cmd_compare(args: argparse.Namespace) -> int:
    from jasper.active_speaker.bass_comparison import compare_bass_takes, selected_take  # lazy: laptop array analysis

    def compare():
        before = json.loads(args.before.read_text())
        after = json.loads(args.after.read_text())
        return {**compare_bass_takes(selected_take(before, args.before_take), selected_take(after, args.after_take), change=args.change),
                "source_views": [str(args.before), str(args.after)]}
    payload = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, compare)
    written = _write(payload, args.out, args.after.parent / ARTIFACT_BY_VIEW[args.command].artifact)
    return answer(args.command, out=written, available=payload["available"], context=payload["context"],
                  bands=payload["bands"], line=f"bass-compare -> {written}")
