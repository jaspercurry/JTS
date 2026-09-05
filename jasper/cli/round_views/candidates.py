# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compare the configs one round played at a held pose.

* ``candidates <round-dir>`` — this round's lateral takes grouped by
  ``candidate_id`` at each pose that played more than one, per candidate the
  deviation of its own curve from its own median level, and per pair the delta
  between them over the band both were swept across. Writes
  ``candidates.json``. A round no pose of which played two candidates is
  refused by name: one config at a pose is a repeat, and ``repeat`` is the
  instrument that measures those, as spread.
  :mod:`~jasper.active_speaker.crossover_v2.candidate_ladder` owns every
  number below.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jasper.active_speaker.crossover_v2.candidate_ladder import (
    CandidateLadderRefused,
    candidate_ladder,
)
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    default_out,
    refused_by_name,
)


def _cmd_candidates(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    try:
        # Unstaged on purpose: resolving the round is the LOAD stage above,
        # and what the ladder itself raises is a view declining a round it
        # read -- ``main``'s bucket, the same one every sibling verb uses.
        document = candidate_ladder(round_dir, inputs)
    except CandidateLadderRefused as refusal:
        return refused_by_name(refusal.reason, refusal.detail)
    summary = document["summary"]
    written = _write(
        document, args.out,
        default_out(inputs, round_dir, ARTIFACT_BY_VIEW[args.command].artifact),
    )
    worst = (
        "no pair shared a role" if summary["max_abs_delta_db"] is None else
        f"widest gap {summary['max_abs_delta_between'][0]} vs "
        f"{summary['max_abs_delta_between'][1]} "
        f"{summary['max_abs_delta_db']:.2f} dB @ "
        f"{summary['max_abs_delta_hz']:.0f} Hz "
        f"({summary['max_abs_delta_role']} at "
        f"{summary['max_abs_delta_position_deg']}deg)"
    )
    return answer(
        args.command, out=written, **summary,
        line=(
            f"candidates: {len(summary['candidates'])} candidate(s) over "
            f"{summary['poses']} held pose(s), {summary['pairs']} pair(s); "
            f"{worst}{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    candidates = sub.add_parser(
        "candidates",
        help="compare the configs this round played at each held pose, pair by pair",
    )
    candidates.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    candidates.add_argument("--out", default=None, help="write the result here (- for stdout)")
    candidates.set_defaults(func=_cmd_candidates)
