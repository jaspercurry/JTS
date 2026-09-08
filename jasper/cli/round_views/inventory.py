# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Artifact presence, producer provenance and usable next commands for one round."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    INVENTORY_ARTIFACT,
    PROG,
    TAKES_THIS_BUNDLE,
    TAKES_THIS_ROUND,
    ViewArtifact,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    context_artifacts,
    default_out,
)


def _runnable(
    view: str, spec: ViewArtifact, round_dir: Path, inputs: RoundInputs,
) -> tuple[str, list[str]]:
    bindings = {
        TAKES_THIS_ROUND: round_dir,
        TAKES_THIS_BUNDLE: inputs.session_dir,
        "<flow-state>": inputs.state_path,
        "<applied-profile>": inputs.applied_profile_path,
    }
    missing = [token for token in spec.takes if token.startswith("<") and not bindings.get(token)]
    tokens = [str(bindings.get(token) or token) for token in spec.takes]
    return shlex.join([*shlex.split(spec.producer or f"{PROG} {view}"), *tokens]), missing


def _cmd_inventory(args: argparse.Namespace) -> int:
    # The round is RESOLVED, never graded: which artifacts sit beside a round
    # is a directory question, and building the evidence packet to answer it
    # would make the cheapest verb here cost the most (415 MB target, ADR-0226).
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    # ``None`` only for a directory holding no round artifacts at all, where
    # every row below is missing whichever path it is read at.
    artifact_dir, _why = round_artifact_dir(inputs.session_dir)
    artifacts: list[dict[str, Any]] = []
    for view, spec in ARTIFACT_BY_VIEW.items():
        path = (
            artifact_dir / spec.artifact
            if spec.in_artifact_dir and artifact_dir is not None
            else default_out(inputs, round_dir, spec.artifact)
        )
        stat = path.stat() if path.is_file() else None
        produced_by, required_inputs = _runnable(view, spec, round_dir, inputs)
        banked_index = view == "position-cycle" and inputs.banked
        artifacts.append({
            "artifact": spec.artifact,
            "path": str(path),
            "present": stat is not None,
            "bytes": None if stat is None else stat.st_size,
            "produced_by": produced_by,
            "producer_needs_more_than_this_round": bool(required_inputs),
            "required_inputs": required_inputs,
            "next_command": None if banked_index else produced_by,
            "repair_reason": "banked_pose_index_missing" if banked_index and stat is None else None,
        })
    bytes_total = sum(row["bytes"] or 0 for row in artifacts)
    payload = {
        "round_dir": str(round_dir),
        "banked": inputs.banked,
        "bytes_total": bytes_total,
        "artifacts": artifacts,
        **context_artifacts(inputs, round_dir),
    }
    written = _write(
        payload, args.out, default_out(inputs, round_dir, INVENTORY_ARTIFACT)
    )
    missing_rows = [row for row in artifacts if not row["present"]]
    missing = [row["next_command"] for row in missing_rows if row["next_command"]]
    return answer(
        args.command, out=written, present=len(artifacts) - len(missing_rows),
        total=len(artifacts), bytes_total=bytes_total, missing=missing,
        unavailable_repairs=[
            {"artifact": row["artifact"], "reason": row["repair_reason"]}
            for row in missing_rows if row["next_command"] is None
        ],
        frozen_packet=payload["frozen_packet"],
        latest_agent_note=payload["latest_agent_note"],
        line=(
            f"inventory: {len(artifacts) - len(missing_rows)}/{len(artifacts)} "
            f"artifact(s) present"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    inventory = sub.add_parser(
        "inventory",
        help="which analysis artifacts this round has, and the command that produces each missing one",
    )
    inventory.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    inventory.add_argument("--out", default=None, help="write the result here (- for stdout)")
    inventory.set_defaults(func=_cmd_inventory)
