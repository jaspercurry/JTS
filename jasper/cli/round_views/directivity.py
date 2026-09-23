# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How each spec band changes off axis, from one set's bearing takes."""

from __future__ import annotations

import argparse
from pathlib import Path

from jasper.active_speaker.crossover_v2.round_views import set_directivity
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    add_set_argument,
    answer,
    default_out,
    resolve_set,
    round_inputs,
)


def _cmd_directivity(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    document = set_directivity(resolve_set(inputs, args.set))
    written = _write(
        {"round_dir": str(round_dir), **document}, args.out,
        default_out(inputs, round_dir, ARTIFACT_BY_VIEW[args.command].artifact, args.set),
    )
    poses = [
        {
            "take_id": row["take_id"], **document["poses"][row["take_id"]],
            "level_offset_db": row["level_offset_db"],
            "bands": [
                {"band_hz": [band["f_lo_hz"], band["f_hi_hz"]], "level_offset_db": band["level_offset_db"],
                 "shape_rms_db": band["shape_rms_db"]}
                for band in row["bands"]
            ],
        }
        for row in document["directivity"]["rows"] if not row["in_reference"]
    ]
    lo, hi = document["parameters"]["band_hz"]
    return answer(
        args.command, out=written, set_id=document["set_id"], role=document["role"],
        parameters=document["parameters"], reference_take_ids=document["reference_take_ids"],
        omitted_take_ids=document["omitted_take_ids"], poses=poses,
        line=(
            f"directivity: {len(poses)} off-axis take(s) against "
            f"{len(document['reference_take_ids'])} 0°/0° take(s), {lo:g}-{hi:g} Hz -> {written}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "directivity", help="each spec band's level and shape at every bearing, against the 0°/0° takes",
    )
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    add_set_argument(parser)
    parser.add_argument("--out", default=None, help="write the result here")
    parser.set_defaults(func=_cmd_directivity)
