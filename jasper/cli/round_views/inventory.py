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
from jasper.active_speaker.measurement_programs import bookkeeping_views
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    INVENTORY_ARTIFACT,
    PROG,
    read_run_manifest, resolve_set, set_artifact_name,
    TAKES_THIS_BUNDLE,
    TAKES_THIS_ROUND,
    ViewArtifact,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    add_set_argument, answer,
    context_artifacts,
    default_out,
)


def _runnable(
    view: str,
    spec: ViewArtifact,
    round_dir: Path,
    inputs: RoundInputs,
    set_id: str | None = None,
    optional_set_flags: tuple[str, ...] = (),
) -> tuple[str, list[str]]:
    bindings = {
        TAKES_THIS_ROUND: round_dir,
        TAKES_THIS_BUNDLE: inputs.session_dir,
        "<set-id>": set_id,
    }
    takes = []
    source = iter(spec.takes)
    for token in source:
        if token in optional_set_flags:
            value = next(source)
            if bindings.get(value):
                takes.extend((token, value))
        else:
            takes.append(token)
    missing = [
        token for token in takes
        if token.startswith("<") and not bindings.get(token)
    ]
    tokens = [str(bindings.get(token) or token) for token in takes]
    return shlex.join([*shlex.split(spec.producer or f"{PROG} {view}"), *tokens]), missing


def _cmd_inventory(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    manifest = read_run_manifest(inputs)
    sets = [resolve_set(inputs, args.set, manifest=manifest)] if args.set else [
        resolve_set(inputs, row["set_id"], manifest=manifest) for row in manifest["sets"]
    ]
    program = manifest["program"]
    artifact_dir, _ = round_artifact_dir(inputs.session_dir)
    artifacts: list[dict[str, Any]] = []
    order = dict.fromkeys((*bookkeeping_views(program), *ARTIFACT_BY_VIEW))
    for view in order:
        spec = ARTIFACT_BY_VIEW[view]
        scoped = "<set-id>" in spec.takes
        for selected in sets if scoped else [None]:
            set_id = selected.set_id if selected else None
            named = set_id if args.set or len(sets) > 1 else None
            if set_id and default_out(inputs, round_dir, spec.artifact, set_id).is_file():
                named = set_id
            path = (
                artifact_dir / spec.artifact
                if spec.in_artifact_dir and artifact_dir is not None
                else default_out(inputs, round_dir, spec.artifact, named)
            )
            stat = path.stat() if path.is_file() else None
            produced_by, required_inputs = _runnable(
                view, spec, round_dir, inputs, named, args.set_flags_by_view.get(view.split()[0], ()),
            )
            if selected and "<take-id>" in required_inputs and len(selected.selected_ids) == 1:
                produced_by = produced_by.replace(shlex.quote("<take-id>"), shlex.quote(selected.take_id()))
                required_inputs.remove("<take-id>")
            banked_index = view == "position-cycle" and inputs.banked
            artifacts.append({
                "program": program, "view": view, "set_id": set_id,
                "artifact": set_artifact_name(spec.artifact, named), "path": str(path),
                "present": stat is not None, "bytes": None if stat is None else stat.st_size,
                "produced_by": produced_by,
                "producer_needs_more_than_this_round": bool(required_inputs),
                "required_inputs": required_inputs,
                "next_command": None if banked_index else produced_by,
                "repair_reason": "banked_pose_index_missing" if banked_index and stat is None else None,
            })
    bytes_total = sum(row["bytes"] or 0 for row in artifacts)
    payload = {
        "program": program,
        "round_dir": str(round_dir),
        "banked": inputs.banked,
        "bytes_total": bytes_total,
        "artifacts": artifacts,
        **context_artifacts(inputs, round_dir),
    }
    written = _write(
        payload, args.out, default_out(inputs, round_dir, INVENTORY_ARTIFACT, args.set)
    )
    missing_rows = [row for row in artifacts if not row["present"]]
    missing = [row["next_command"] for row in missing_rows if row["next_command"]]
    return answer(
        args.command, out=written, program=program, present=len(artifacts) - len(missing_rows),
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
    add_set_argument(inventory)
    inventory.add_argument("--out", default=None, help="write the result here")
    inventory.set_defaults(func=_cmd_inventory)
