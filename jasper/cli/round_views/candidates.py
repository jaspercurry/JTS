# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compare candidates at each held pose and frequency-view window."""

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
    omitted_note,
    refused_by_name,
    subject,
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
    spec = ARTIFACT_BY_VIEW[args.command]
    written = _write(document, args.out, default_out(inputs, round_dir, spec.artifact), schema=spec.schema)
    worst = (
        "no trusted-window pair shared a role" if summary["max_abs_delta_db"] is None else
        f"widest gap {summary['max_abs_delta_between'][0]} vs "
        f"{summary['max_abs_delta_between'][1]} "
        f"{summary['max_abs_delta_db']:.2f} dB @ "
        f"{summary['max_abs_delta_hz']:.0f} Hz "
        f"({' '.join(filter(None, (summary['max_abs_delta_role'], summary['max_abs_delta_window'])))} "
        f"{summary['max_abs_delta_band_hz'][0]:g}-{summary['max_abs_delta_band_hz'][1]:g} Hz "
        f"at {summary['max_abs_delta_pose_key']})"
    )
    return answer(
        args.command, schema=spec.schema, subject=subject(inputs), parameters={}, out=written, **summary,
        line=(
            f"candidates: {len(summary['candidates'])} candidate(s) over "
            f"{summary['poses']} held pose(s), {summary['pairs']} pair(s); "
            f"{worst}{omitted_note(summary['omitted'])}{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    candidates = sub.add_parser(
        "candidates",
        help="compare candidates at each held pose and window; read banked frequency curves or in-record curves",
    )
    candidates.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    candidates.add_argument("--out", default=None, help="write the result here")
    candidates.set_defaults(func=_cmd_candidates)
