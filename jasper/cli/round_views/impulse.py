# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One take's impulse, and its phase and group delay (REW's Impulse and Group Delay graphs)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.round_inputs import take_artifact_name
from jasper.active_speaker.crossover_v2.take_impulses import TakeImpulsesUnreadable
from jasper.active_speaker.crossover_v2.take_reading import (
    TakeRead, group_delay_report, impulse_report, read_take,
)
from jasper.cli._refusal import EXIT_UNREADABLE

from ._common import (
    ARTIFACT_BY_VIEW, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _write, add_set_argument, answer,
    refused_by_name, resolve_set, resolved_out, round_inputs, subject,
)


def _run(args: argparse.Namespace, report: Callable[[TakeRead], dict[str, Any]], line: str) -> int:
    round_dir = Path(args.round_dir)
    inputs = round_inputs(round_dir)
    selected = resolve_set(inputs, args.set)
    take_id = selected.take_id(args.take)
    try:
        written_report = report(read_take(round_dir, take_id=take_id, role=args.role))
    except RoundCapturesRefused as exc:
        return refused_by_name(exc.reason, exc.detail)
    except TakeImpulsesUnreadable as exc:
        return refused_by_name("take_impulses_unreadable", str(exc), code=EXIT_UNREADABLE)
    spec = ARTIFACT_BY_VIEW[args.command]
    written = _write(written_report, args.out,
                     resolved_out(round_dir, take_artifact_name(spec.artifact, take_id, args.role)),
                     schema=spec.schema)
    return answer(args.command, schema=spec.schema, subject=subject(inputs, selected, take_ids=[take_id]),
                  parameters=written_report["parameters"], out=written, **written_report["summary"],
                  line=f"{line}: {take_id} {args.role} -> {written}")


def _cmd_impulse(args: argparse.Namespace) -> int:
    return _run(args, lambda read: impulse_report(read, span_ms=tuple(args.span_ms)), "impulse")


def _cmd_group_delay(args: argparse.Namespace) -> int:
    return _run(args, lambda read: group_delay_report(
        read, window_ms=args.window_ms, points_per_octave=args.points_per_octave), "group delay")


def _take_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    add_set_argument(parser)
    parser.add_argument("--take", help="take id within the set; defaults to the set's on-axis take")
    parser.add_argument("--role", default="summed",
                        help="which recorded response: summed, or a driver role the take recorded")
    parser.add_argument("--out", help="artifact destination")


def add_parser(sub: argparse._SubParsersAction) -> None:
    impulse = sub.add_parser("impulse", help="a take's impulse: arrival, onset, noise and decay")
    _take_arguments(impulse)
    impulse.add_argument("--span-ms", type=float, nargs=2, default=[5.0, 100.0], metavar=("BEFORE", "AFTER"),
                         help="artifact span before the onset and after the peak (default: 5 100)")
    impulse.set_defaults(func=_cmd_impulse)

    group_delay = sub.add_parser("group-delay", help="a take's phase, group delay and excess group delay by band")
    _take_arguments(group_delay)
    group_delay.add_argument("--window-ms", type=float,
                             help="read through this window after the peak; default: the take's own gate")
    group_delay.add_argument("--points-per-octave", type=int, default=24,
                             help="artifact grid density (default: 24)")
    group_delay.set_defaults(func=_cmd_group_delay)
