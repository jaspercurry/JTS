# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grade one room set against its incumbent from the same run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

from jasper.active_speaker.crossover_v2.room_prescription import (
    RoomPrescriptionRefused,
)
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.round_view_builders import room_grade_payload
from jasper.cli._refusal import EXIT_UNREADABLE, StageFailed, stage

from ._common import (
    ARTIFACT_BY_VIEW, RoundSetRefused,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    add_set_argument, answer,
    default_out,
    refused_by_name,
    round_inputs,
)


def _band_line(band: Mapping[str, Any]) -> str:
    if band["rms_db"] is None:
        return f"{band['lo_hz']:g}-{band['hi_hz']:g} Hz: unavailable"
    spread = "n/a" if band["spread_db"] is None else f"{band['spread_db']:.1f} dB"
    line = (
        f"{band['lo_hz']:g}-{band['hi_hz']:g} Hz: rms {band['rms_db']:.1f} dB "
        f"max {band['max_db']:.1f} dB spread {spread}"
    )
    if band["incumbent_rms_db"] is None:
        return line
    return f"{line} | incumbent rms {band['incumbent_rms_db']:.1f} (Δ {band['delta_rms_db']:+.1f})"


def _cmd_room_grade(args: argparse.Namespace) -> int:
    directory = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, directory)
    try:
        artifact = room_grade_payload(inputs, directory, args.set,
                         incumbent_id=args.incumbent, calibration_root=args.calibration_root)
    except RoundSetRefused:
        raise
    except RoundCapturesRefused as exc:
        return refused_by_name(exc.reason, exc.detail)
    except RoomPrescriptionRefused as exc:
        # A document that will not read into a median is an input failure.
        return refused_by_name(exc.reason, exc.detail, code=EXIT_UNREADABLE)
    except _ROUND_TOOL_ERRORS as exc:
        raise StageFailed(EXIT_UNREADABLE, exc) from exc
    incumbent_id = artifact["incumbent_set_id"]
    # A grade survives an artifact write failure.
    for band in artifact["bands"]:
        print(_band_line(band), file=sys.stderr)
    written = _write(artifact, None, default_out(
        inputs, directory, ARTIFACT_BY_VIEW[args.command].artifact, args.set,
    ))
    regressed = artifact["regressed_bands"]
    comparison = artifact["comparison"]
    comparison_result = (
        f"comparison unavailable: {comparison['unavailable_reason']}"
        if comparison is not None and not comparison["available"]
        else (
            ", ".join(f"{low:g} Hz" for low in regressed) if regressed
            else "none" if artifact["incumbent"] else "incumbent set unavailable"
        )
    )
    return answer(
        args.command, out=written, set_id=artifact["set_id"], incumbent_set_id=incumbent_id,
        incumbent_reason=artifact["incumbent_reason"], ceiling_hz=artifact["ceiling_hz"],
        ceiling_source=artifact["ceiling_source"], n_positions=artifact["n_positions"],
        spatial_support=artifact["spatial_support"],
        ladder=artifact["ladder"], bands=artifact["bands"], regressed_bands=regressed,
        incumbent=artifact["incumbent"], graph_scopes=artifact["graph_scopes"],
        comparison=artifact["comparison"],
        evidence=artifact["evidence"], incumbent_evidence=artifact["incumbent_evidence"],
        graph_scopes_source=artifact["graph_scopes_source"],
        line=(
            f"room-grade: {len(artifact['bands'])} band(s) to "
            f"{artifact['ceiling_hz']:g} Hz ({artifact['ceiling_source']}); regressed: "
            + comparison_result
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("room-grade", help="grade a room set against its incumbent in this run")
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    parser.add_argument("--calibration-root", type=Path, help="rebuild room documents with this microphone calibration registry")
    add_set_argument(parser)
    parser.add_argument("--incumbent", help="override the incumbent with this set from the same run")
    parser.set_defaults(func=_cmd_room_grade)
