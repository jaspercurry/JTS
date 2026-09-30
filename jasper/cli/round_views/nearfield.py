# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A round's near-field driver takes, band by band, with each driver's raw curve and distance self-test."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.nearfield_view import nearfield_view
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, with_records
from jasper.active_speaker.design_inputs import declared_by_target
from jasper.active_speaker.run_manifest import LEVEL_MISMATCH_DB, driver_level_mismatches, view_sets
from jasper.platform.atomic_io import read_json_mapping
from jasper.audio_measurement.evidence_reasons import REFUSE_NO_NEAR_FIELD_TAKES, EvidenceUnavailable
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    default_out,
    read_run_manifest,
    round_inputs,
    subject,
)


def round_nearfield(inputs: RoundInputs) -> tuple[dict[str, Any], dict[str, Any]]:
    """The round's run manifest read with its records, and its near-field view;
    a round with no kept one-driver take refuses by name."""
    # The run manifest refuses by name, as every round view's does.
    manifest = with_records(inputs.session_dir, read_run_manifest(inputs))
    draft = (read_json_mapping(inputs.design_draft_path) if inputs.design_draft_path else None) or {}
    takes = [take for row in view_sets(manifest) for take in row["takes"]
             if take["selected"] and (take.get("pose") or {}).get("driver")]
    graphs = {take["take_id"]: graph for take in takes
              if (graph := ((take.get("provenance") or {}).get("graph") or {}).get("config")) is not None}
    document = nearfield_view(takes, radiating_diameter_mm_by_target=declared_by_target(draft, "radiating_diameter_mm"),
                              played_graphs=graphs)
    if not document["takes"]:
        raise EvidenceUnavailable(REFUSE_NO_NEAR_FIELD_TAKES, {"driver_take_ids": [take["take_id"] for take in takes]})
    return manifest, document


def _cmd_nearfield(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    manifest, document = round_nearfield(inputs)
    document["level_mismatches"] = driver_level_mismatches(manifest)
    document["parameters"] = {**document["parameters"], "level_mismatch_db": LEVEL_MISMATCH_DB}
    spec = ARTIFACT_BY_VIEW[args.command]
    written = _write({"round_dir": str(round_dir), **document}, args.out,
                     default_out(inputs, round_dir, spec.artifact, None), schema=spec.schema)
    steps = [step for driver in document["drivers"] for step in driver["steps"]]
    return answer(
        args.command, schema=spec.schema,
        subject=subject(inputs, take_ids=[take["take_id"] for take in document["takes"]]),
        parameters=document["parameters"], out=written, drivers=document["drivers"],
        level_mismatches=document["level_mismatches"],
        line=(f"nearfield: {len(document['takes'])} take(s) of {len(document['drivers'])} driver(s), "
              f"{sum(step['verdict'] == 'pass' for step in steps)}/{len(steps)} distance step(s) pass -> {written}"),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "nearfield", help=("each near-field take band by band, each driver's raw curve per distance, its "
                           "step between distances against a piston, and drivers of one size that play apart"),
    )
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    parser.add_argument("--out", default=None, help="write the result here")
    parser.set_defaults(func=_cmd_nearfield)
