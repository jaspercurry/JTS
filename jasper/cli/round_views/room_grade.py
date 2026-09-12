# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grade one room set against its incumbent from the same run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

from jasper.active_speaker.crossover_v2.room_grade import (
    bundle_graph_scopes,
    grade_room_median,
    read_room_median,
)
from jasper.active_speaker.crossover_v2.room_prescription import (
    RoomPrescriptionRefused,
)
from jasper.cli._refusal import EXIT_UNREADABLE, StageFailed, read_json_source, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    add_set_argument, answer,
    default_out,
    refused_by_name,
    resolve_set, round_inputs,
)


def _document(path: Path) -> dict:
    document = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, read_json_source, str(path))
    if not isinstance(document, dict) or not isinstance(document.get("incumbent") or {}, dict):
        raise StageFailed(EXIT_UNREADABLE, TypeError("room_document_malformed"))
    return document


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
    selected = resolve_set(inputs, args.set)
    candidate_path = default_out(inputs, directory, ARTIFACT_BY_VIEW["room"].artifact, args.set)
    candidate = _document(candidate_path)
    incumbent_id = args.incumbent or (candidate.get("incumbent") or {}).get("set_id")
    incumbent_doc = None
    if incumbent_id is not None:
        resolve_set(inputs, incumbent_id)
        incumbent_doc = candidate if incumbent_id == selected.set_id else _document(default_out(
            inputs, directory, ARTIFACT_BY_VIEW["room"].artifact, incumbent_id,
        ))
    try:
        median = read_room_median(candidate.get("median", {}))
        incumbent = None if incumbent_doc is None else read_room_median(incumbent_doc.get("median", {}))
        grade = grade_room_median(median, incumbent=incumbent)
    except RoomPrescriptionRefused as exc:
        # A document that will not read into a median is the INPUT failing, not
        # this view declining a round it read.
        return refused_by_name(exc.reason, exc.detail, code=EXIT_UNREADABLE)

    scope = (median.evidence or {}).get("basis", {}).get("graph_scope")
    artifact = {
        **grade.to_dict(),
        "room": str(candidate_path),
        "set_id": selected.set_id, "incumbent_set_id": incumbent_id,
        "incumbent_reason": "" if incumbent_id else candidate.get(
            "incumbent_reason", "room_incumbent_set_unavailable"),
        "evidence": median.evidence,
        "incumbent_evidence": None if incumbent is None else incumbent.evidence,
        "graph_scopes": ([scope] if scope else []) if median.evidence is not None
                        else bundle_graph_scopes(inputs.session_dir),
        "graph_scopes_source": "selected_median" if median.evidence is not None else "round",
    }
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
        args.command, out=written, set_id=selected.set_id, incumbent_set_id=incumbent_id,
        incumbent_reason=artifact["incumbent_reason"], ceiling_hz=grade.ceiling_hz,
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
    parser = sub.add_parser("room-grade", help="grade a room set against its incumbent in this run")
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    add_set_argument(parser)
    parser.add_argument("--incumbent", help="override the incumbent with this set from the same run")
    parser.set_defaults(func=_cmd_room_grade)
