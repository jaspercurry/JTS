# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Artifact presence and producer provenance for one measured round."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any
from .measurement_programs import bookkeeping_views, run_purpose
from .run_manifest import room_sets
from .crossover_v2.round_inputs import (RoundInputs, read_run_manifest, resolve_set, set_artifact_name,
                                        round_artifact_dir, default_out)
from .round_view_artifacts import PROG, ARTIFACT_BY_VIEW, TAKES_THIS_ROUND, TAKES_THIS_BUNDLE, ViewArtifact, context_artifacts


def _runnable(
    view: str,
    spec: ViewArtifact,
    round_dir: Path,
    inputs: RoundInputs,
    set_id: str | None = None,
) -> tuple[str, list[str]]:
    bindings = {
        TAKES_THIS_ROUND: round_dir,
        TAKES_THIS_BUNDLE: inputs.session_dir,
        "<set-id>": set_id,
    }
    takes: list[str] = []
    source = iter(spec.takes)
    for token in source:
        if token == "--set" or token.endswith("-set"):
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


def inventory_payload(inputs: RoundInputs, round_dir: Path, requested_set: str | None = None) -> dict[str, Any]:
    manifest = read_run_manifest(inputs)
    sets = [resolve_set(inputs, requested_set, manifest=manifest)] if requested_set else [
        resolve_set(inputs, row["set_id"], manifest=manifest) for row in manifest["sets"]
    ]
    program = run_purpose(manifest["program"])
    artifact_dir, _ = round_artifact_dir(inputs.session_dir)
    artifacts: list[dict[str, Any]] = []
    order = dict.fromkeys((
        *(name for name, _, _ in bookkeeping_views(program, has_room=bool(room_sets(manifest)))),
        *(name for name, spec in ARTIFACT_BY_VIEW.items()
          if not spec.purposes or program in spec.purposes),
    ))
    for view in order:
        spec = ARTIFACT_BY_VIEW[view]
        scoped = "<set-id>" in spec.takes
        for selected in sets if scoped else [None]:
            set_id = selected.set_id if selected else None
            named = set_id if requested_set or len(sets) > 1 else None
            if set_id and default_out(inputs, round_dir, spec.artifact, set_id).is_file():
                named = set_id
            path = (
                artifact_dir / spec.artifact
                if spec.in_artifact_dir and artifact_dir is not None
                else default_out(inputs, round_dir, spec.artifact, named)
            )
            stat = path.stat() if path.is_file() else None
            produced_by, required_inputs = _runnable(
                view, spec, round_dir, inputs, named,
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
    return payload


def inventory_summary(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload["artifacts"]
    missing = [row for row in rows if not row["present"]]
    return {
        "program": payload["program"], "present": len(rows) - len(missing), "total": len(rows),
        "bytes_total": payload["bytes_total"],
        "missing": [row["next_command"] for row in missing if row["next_command"]],
        "unavailable_repairs": [{"artifact": row["artifact"], "reason": row["repair_reason"]}
                                for row in missing if row["next_command"] is None],
        "latest_agent_note": payload["latest_agent_note"],
    }
