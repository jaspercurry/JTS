# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A round's near-field driver takes, band by band, with each driver's raw curve and distance self-test."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.nearfield_view import nearfield_view
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs
from jasper.active_speaker.run_manifest import view_sets
from jasper.atomic_io import read_json_mapping
from jasper.audio_measurement.evidence_reasons import EVIDENCE_REASONS, REFUSE_NO_NEAR_FIELD_TAKES
from jasper.cli._refusal import EXIT_UNREADABLE, stage
from jasper.speaker_layout import declared_radiating_diameters_mm

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    default_out,
    read_run_manifest,
    refused_by_name,
    round_inputs,
    subject,
)


def _played_graph(inputs: RoundInputs, take: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The CamillaDSP config a take played, as its banked record read it back."""
    record = read_json_mapping(take_artifact_path(inputs.session_dir, take["artifacts"]["record_id"])) or {}
    return ((record.get("provenance") or {}).get("graph") or {}).get("config")


def _cmd_nearfield(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    manifest = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, read_run_manifest, inputs)
    draft = (read_json_mapping(inputs.design_draft_path) if inputs.design_draft_path else None) or {}
    takes = [take for row in view_sets(manifest) for take in row["takes"]
             if take.get("selected") and (take.get("pose") or {}).get("driver")]
    graphs = {take["take_id"]: graph for take in takes if (graph := _played_graph(inputs, take)) is not None}
    document = nearfield_view(takes, radiating_diameter_mm_by_role=declared_radiating_diameters_mm(draft),
                              played_graphs=graphs)
    if not document["takes"]:
        return refused_by_name(REFUSE_NO_NEAR_FIELD_TAKES, EVIDENCE_REASONS[REFUSE_NO_NEAR_FIELD_TAKES])
    spec = ARTIFACT_BY_VIEW[args.command]
    written = _write({"round_dir": str(round_dir), **document}, args.out,
                     default_out(inputs, round_dir, spec.artifact, None), schema=spec.schema)
    steps = [step for driver in document["drivers"] for step in driver["steps"]]
    return answer(
        args.command, schema=spec.schema,
        subject=subject(inputs, take_ids=[take["take_id"] for take in document["takes"]]),
        parameters=document["parameters"], out=written, drivers=document["drivers"],
        line=(f"nearfield: {len(document['takes'])} take(s) of {len(document['drivers'])} driver(s), "
              f"{sum(step['verdict'] == 'pass' for step in steps)}/{len(steps)} distance step(s) pass -> {written}"),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "nearfield", help=("each near-field take band by band, each driver's raw curve per distance, and its "
                           "step between distances against a piston"),
    )
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    parser.add_argument("--out", default=None, help="write the result here")
    parser.set_defaults(func=_cmd_nearfield)
