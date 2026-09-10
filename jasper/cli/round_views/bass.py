# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Replay retained bass captures on the laptop."""

from __future__ import annotations

import argparse
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
