# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One take's impulse, its phase and group delay, and its decay (REW's Impulse,
Group Delay and RT60 graphs)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.round_inputs import take_artifact_name
from jasper.active_speaker.crossover_v2.take_reading import TakeRead, decay_report, group_delay_report, impulse_report

from ._common import (
    ARTIFACT_BY_VIEW, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _write, add_set_argument, answer, read_set_take,
    refused_by_name, resolved_out,
)


def _run(args: argparse.Namespace, report: Callable[[TakeRead], dict[str, Any]], line: str) -> int:
    round_dir = Path(args.round_dir)
    try:
        take_subject, read = read_set_take(round_dir, args.set, args.take, args.role)
        written_report = report(read)
    except RoundCapturesRefused as exc:
        return refused_by_name(exc.reason, exc.detail)
    take_id, = take_subject["take_ids"]
    spec = ARTIFACT_BY_VIEW[args.command]
    written = _write(written_report, args.out,
                     resolved_out(round_dir, take_artifact_name(spec.artifact, take_id, read.role)),
                     schema=spec.schema)
    return answer(args.command, schema=spec.schema, subject=take_subject,
                  parameters=written_report["parameters"], out=written, **written_report["summary"],
                  line=f"{line}: {take_id} {read.role} -> {written}")


def _cmd_impulse(args: argparse.Namespace) -> int:
    return _run(args, lambda read: impulse_report(read, span_ms=tuple(args.span_ms)), "impulse")


def _cmd_group_delay(args: argparse.Namespace) -> int:
    return _run(args, lambda read: group_delay_report(
        read, window_ms=args.window_ms, points_per_octave=args.points_per_octave), "group delay")


def _cmd_decay(args: argparse.Namespace) -> int:
    return _run(args, decay_report, "decay")


def _take_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    add_set_argument(parser, take=True)
    parser.add_argument("--role", help="which recorded response: summed, or a driver role the take recorded; "
                                       "default: the set's own")
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

    decay = sub.add_parser("decay", help="a take's decay by octave: EDT, T20 and T30 from its onset")
    _take_arguments(decay)
    decay.set_defaults(func=_cmd_decay)
