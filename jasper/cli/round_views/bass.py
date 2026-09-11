# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bass views."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused, refusal_copy_for
from jasper.active_speaker.measurement_bass import bass_view
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE, failed, stage

from ._bass_inputs import compare_sets, fit_run
from ._common import ARTIFACT_BY_VIEW, _ROUND_TOOL_ERRORS, _write, answer, default_out, resolve_set, round_inputs


def add_parser(sub: argparse._SubParsersAction) -> None:
    for name, help_text in (("bass", "bass response, quiet-window SNR and H2/H3"),
                            ("bass-compare", "compare two manifest-selected bass sets"),
                            ("bass-fit-table", "fit all candidate/level pairs in a run")):
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument("round_dir", type=Path)
        parser.add_argument("--out")
        parser.set_defaults(func=_cmd)
        if name == "bass":
            parser.add_argument("--set")
            parser.add_argument("--calibration-root", type=Path)
        elif name == "bass-compare":
            parser.add_argument("--set", action="append", help="before, then after; supply twice")
            parser.add_argument("after", nargs="?", type=Path)
            parser.add_argument("--before-set")
            parser.add_argument("--after-set")
            parser.add_argument("--change", required=True, choices=("candidate", "volume", "demand", "diagnostic"))
        else:
            parser.add_argument("--run", required=True, help="run ID recorded in this round's manifest")
            parser.add_argument("--candidate", type=Path, action="append", required=True, help="measured candidate artifact; repeat for each candidate")
            parser.add_argument("--target", type=Path, required=True, help="target curve JSON: freqs_hz, magnitude_db")
            parser.add_argument("--tolerance-db", type=float, required=True)
            parser.add_argument("--reference-band-hz", type=float, nargs=2, default=(300., 1000.))


def _cmd(args: argparse.Namespace) -> int:
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, args.round_dir)
    destination = default_out(inputs, args.round_dir, ARTIFACT_BY_VIEW[args.command].artifact,
                              args.set if args.command == "bass" else None)
    try:
        if args.command == "bass":
            selected = resolve_set(inputs, args.set)
            payload = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, bass_view, inputs.session_dir,
                            calibration_root=args.calibration_root)
            payload["takes"] = [take for take in payload["takes"] if take["record"]["take_id"] in selected.selected_ids]
            summary: dict[str, Any] = {"takes": len(payload["takes"])}
        elif args.command == "bass-compare":
            payload, destination = compare_sets(args)
            summary = {key: payload[key] for key in ("available", "context", "bands")}
        else:
            payload = fit_run(inputs, args)
            levels = [row for table in payload["tables"] for row in table["levels"]]
            summary = {"run_id": payload["run_id"], "level_count": len(levels),
                       "outcomes": [row["outcome"] for row in levels]}
    except CrossoverV2Refused as refusal:
        message, action = refusal_copy_for(refusal.code)
        return failed(EXIT_REFUSED, refusal.code, refusal.args[0] if refusal.args else message,
                      code=refusal.code, next_action=action)
    written = _write(payload, args.out, destination)
    return answer(args.command, out=written, **summary, line=f"{args.command} -> {written}")
