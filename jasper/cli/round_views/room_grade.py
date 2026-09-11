# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grade the round's own per-set room median and optional baseline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

from jasper.active_speaker.crossover_v2.room_grade import (
    RoomMedian,
    bundle_graph_scopes,
    grade_room_median,
    read_room_median,
)
from jasper.active_speaker.crossover_v2.room_prescription import (
    RoomPrescriptionRefused,
)
from jasper.cli._refusal import EXIT_UNREADABLE, read_json_source, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _load_round,
    _view_out,
    _write,
    answer,
    default_out,
    refused_by_name,
    resolved_out, resolve_set, round_inputs,
)


#: The seat cube's median, written by ``jasper-round-views room-median``.
ROOM_MEDIAN_ARTIFACT = ARTIFACT_BY_VIEW["room-median"].artifact

def _median(path: Path) -> RoomMedian:
    """One median document as a value. A missing or unparsable file is the
    ROUND failing to carry its median, which is the load stage's code."""
    return read_room_median(
        stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, read_json_source, str(path))
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
    banked = _load_round(args.round_dir)
    resolve_set(banked.inputs, args.set)
    candidate_path = default_out(banked.inputs, banked.round_dir, ROOM_MEDIAN_ARTIFACT, args.set)
    incumbent_path = None
    if args.baseline:
        resolve_set(round_inputs(Path(args.baseline)), args.baseline_set)
        incumbent_path = resolved_out(Path(args.baseline), ROOM_MEDIAN_ARTIFACT, args.baseline_set)
    try:
        median = _median(candidate_path)
        incumbent = None if incumbent_path is None else _median(incumbent_path)
        grade = grade_room_median(median, incumbent=incumbent)
    except RoomPrescriptionRefused as exc:
        # A document that will not read into a median is the INPUT failing, not
        # this view declining a round it read.
        return refused_by_name(exc.reason, exc.detail, code=EXIT_UNREADABLE)

    scope = (median.evidence or {}).get("basis", {}).get("graph_scope")
    artifact = {
        **grade.to_dict(),
        "room_median": str(candidate_path),
        "evidence": median.evidence,
        "incumbent_evidence": None if incumbent is None else incumbent.evidence,
        "graph_scopes": ([scope] if scope else []) if median.evidence is not None
                        else bundle_graph_scopes(banked.session_dir),
        "graph_scopes_source": "selected_median" if median.evidence is not None else "round",
    }
    # Read before filed, as close-reference does: a grade survives an --out the
    # operator may not write.
    for band in artifact["bands"]:
        print(_band_line(band), file=sys.stderr)
    written = _write(artifact, args.out, _view_out(args, banked))
    regressed = artifact["regressed_bands"]
    comparison = artifact["comparison"]
    comparison_result = (
        f"comparison unavailable: {comparison['unavailable_reason']}"
        if comparison is not None and not comparison["available"]
        else (
            ", ".join(f"{low:g} Hz" for low in regressed) if regressed
            else "none" if artifact["incumbent"] else "no baseline named"
        )
    )
    return answer(
        args.command, out=written, ceiling_hz=grade.ceiling_hz,
        ceiling_source=grade.ceiling_source, n_positions=grade.n_positions,
        spatial_support=artifact["spatial_support"],
        bands=artifact["bands"], regressed_bands=regressed,
        incumbent=artifact["incumbent"], graph_scopes=artifact["graph_scopes"],
        comparison=artifact["comparison"],
        evidence=median.evidence, incumbent_evidence=artifact["incumbent_evidence"],
        graph_scopes_source=artifact["graph_scopes_source"],
        line=(
            f"room-grade: {len(grade.bands)} band(s) to "
            f"{grade.ceiling_hz:g} Hz ({grade.ceiling_source}); regressed: "
            + comparison_result
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    room_grade = sub.add_parser(
        "room-grade",
        help="grade this round's seat-cube median against flat, band by band, "
             "and disclose how each band moved against a baseline round",
    )
    room_grade.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    room_grade.add_argument(
        "--baseline", default=None, metavar="<round-dir>",
        help=f"{_ROUND_DIR_HELP} whose median is the incumbent; its bands are "
             "disclosed beside this round's, never acted on",
    )
    room_grade.add_argument("--set", help="set whose room median is graded")
    room_grade.add_argument("--baseline-set", help="set in --baseline")
    room_grade.add_argument("--out", default=None, help="write the result here")
    room_grade.set_defaults(func=_cmd_room_grade)
