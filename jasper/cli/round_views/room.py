# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One room document over the selected manifest set."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from jasper.active_speaker.crossover_v2.room_views import room_document
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS,
    _write, answer, default_out, read_run_manifest, refused_by_name, resolve_set,
)

REFUSE_NO_SEAT_TAKES = "room_no_seat_takes"


def _cmd_room(args: argparse.Namespace) -> int:
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, Path(args.round_dir))
    if args.applied_profile:
        inputs = replace(inputs, applied_profile_path=Path(args.applied_profile))
    selected = resolve_set(inputs, args.set)
    try:
        selection = select_seat_takes(inputs.session_dir, take_ids=selected.selected_ids,
                                      basis=selected.capture_basis)
    except RoundCapturesRefused as exc:
        return refused_by_name(exc.reason, exc.detail)
    if not selection.takes:
        return refused_by_name(REFUSE_NO_SEAT_TAKES, {"set_id": selected.set_id,
                                                    "evidence": selection.evidence})
    payload = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, room_document, selection.takes, set_id=selected.set_id, evidence=selection.evidence,
        applied_profile_path=inputs.applied_profile_path, geometry_path=inputs.declared_geometry_path,
        manifest=read_run_manifest(inputs),
    )
    written = _write(payload, None, default_out(
        inputs, Path(args.round_dir), ARTIFACT_BY_VIEW[args.command].artifact, args.set,
    ))
    median, features = payload["median"], payload["persistence"]["features"]
    return answer(
        args.command, out=written, set_id=selected.set_id, ceiling_hz=median["ceiling_hz"],
        n_positions=median["n_positions"], spatial_support=median["spatial_support"],
        coverage_hz=median["coverage_hz"], features=len(features), incumbent=payload["incumbent"],
        line=f"room: {median['n_positions']} positions, {len(features)} features -> {written}",
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("room", help="ceiling, median, persistence, limits, incumbent and boundary")
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    parser.add_argument("--applied-profile", metavar="PATH", help="applied profile for this room")
    parser.add_argument("--set", help="set in the run manifest")
    parser.set_defaults(func=_cmd_room)
