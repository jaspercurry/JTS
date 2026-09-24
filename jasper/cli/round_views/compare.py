# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How one take differs from another, or from a forecast: REW's |B|/|A| in dB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.round_inputs import take_artifact_name
from jasper.active_speaker.crossover_v2.take_reading import (
    REFUSE_PREVIEW_UNREADABLE, compare_preview_report, compare_report, read_preview, read_take,
)
from jasper.cli._refusal import EXIT_UNREADABLE

from ._common import (
    ARTIFACT_BY_VIEW, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _write, add_set_argument, answer, refused_by_name,
    resolve_set_take, resolved_out,
)


def _cmd_compare(args: argparse.Namespace) -> int:
    if args.a_preview and args.source_b:
        args.parser.error("--a-preview is side A: name only the measured round")
    b_round = Path(args.source_b or args.source_a)
    try:
        preview_document = json.loads(Path(args.a_preview).read_text()) if args.a_preview else None
    except (OSError, ValueError) as exc:
        return refused_by_name(REFUSE_PREVIEW_UNREADABLE, str(exc), code=EXIT_UNREADABLE)
    try:
        b_subject, b_take, b_role = resolve_set_take(b_round, args.b_set, args.b_take, args.b_role)
        b = read_take(b_round, take_id=b_take, role=b_role)
        if preview_document is not None:
            preview = read_preview(preview_document)
            report = compare_preview_report(preview, b, smoothing_fraction=args.smoothing,
                                            points_per_octave=args.points_per_octave)
            a_subject: dict[str, Any] = {"candidate_id": preview.candidate_id}
            # Named by the forecast's identity, so two forecasts read against one take keep two artifacts.
            a_label = f"preview-{preview.fingerprint[:12]}"
        else:
            a_subject, a_take, a_role = resolve_set_take(Path(args.source_a), args.a_set, args.a_take, args.a_role)
            a = read_take(Path(args.source_a), take_id=a_take, role=a_role)
            report = compare_report(a, b, window_ms=args.window_ms, smoothing_fraction=args.smoothing,
                                    points_per_octave=args.points_per_octave, remove_level=args.remove_level)
            a_label = f"{a.capture.capture_id}-{a.role}"
    except RoundCapturesRefused as exc:
        return refused_by_name(exc.reason, exc.detail)
    spec = ARTIFACT_BY_VIEW[args.command]
    name = take_artifact_name(spec.artifact, f"{a_label}-vs-{b.capture.capture_id}", b.role)
    written = _write(report, args.out, resolved_out(b_round, name), schema=spec.schema)
    return answer(args.command, schema=spec.schema, subject=[a_subject, b_subject],
                  parameters=report["parameters"], out=written, **report["summary"],
                  line=f"compare: {a_label} vs {b.capture.capture_id}-{b.role} -> {written}")


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "compare", help="how take B differs from take A (or from a forecast), through one window and smoothing")
    parser.add_argument("source_a", metavar=_ROUND_DIR_METAVAR,
                        help=f"side A's round ({_ROUND_DIR_HELP}); with --a-preview, the measured round")
    parser.add_argument("source_b", metavar="<round-b>", nargs="?", help="side B's round; default: side A's")
    for side in ("a", "b"):
        add_set_argument(parser, name=f"--{side}-set", take=True)
        parser.add_argument(f"--{side}-role",
                            help=f"side {side.upper()}'s recorded response: summed, or a driver role; "
                                 "default: its set's own")
    parser.add_argument("--a-preview", metavar="PREVIEW.json",
                        help="side A is this forecast (judge --preview --out), read through its own window")
    parser.add_argument("--window-ms", type=float,
                        help="read both sides through this window; default: the shorter take window")
    parser.add_argument("--smoothing", type=int, default=6, metavar="N",
                        help="1/N-octave power smoothing of each side before differencing; 0 is none (default: 6)")
    parser.add_argument("--remove-level", action="store_true",
                        help="take the median level difference off before comparing shapes")
    parser.add_argument("--points-per-octave", type=int, default=48, help="artifact grid density (default: 48)")
    parser.add_argument("--out", help="artifact destination")
    parser.set_defaults(func=_cmd_compare, parser=parser)
