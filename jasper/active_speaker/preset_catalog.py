# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The measurement presets as an agent reads them before it writes a run request (#5737)."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .angle_capture import LateralWalkRefused
from .measurement_programs import (
    available_presets, cleared_layers, named_layout, near_field_drivers, plan_poses, preset, run_preset,
)
from .plan_run import prepare_plan_captures, preview_schedule
from .run_levels import LEVEL_OFFSETS_DB
from .run_request import RunRequest, resolve_plan


def preset_catalog(context: Any) -> list[dict[str, Any]]:
    """Every preset, what it plays, and each layout it offers on this speaker."""
    targets = near_field_drivers(context.topology)
    return [{
        "preset": row.preset, "purposes": list(row.purposes), "description": row.description,
        "use_when": row.use_when, "regime": row.regime, "branch_pair": row.branch_pair,
        "room_sweep": row.room_sweep, "cleared_layers": list(cleared_layers(row.purpose, base=True, regime=row.regime)),
        "stimulus": dict(row.stimulus) if row.stimulus else None,
        "level_ladder_db": list(LEVEL_OFFSETS_DB) if row.levels else None, "layout": row.layout,
        "layouts": [_layout(row.preset, name, targets, context) for name in row.layouts],
    } for row in map(preset, available_presets())]


def _layout(preset_id: str, name: str, targets: tuple[str, ...], context: Any) -> dict[str, Any]:
    """A layout's poses, the outputs its driver poses play here, and one level's captures
    and seconds as the page previews them; or the code the bare request refuses with."""
    named = named_layout(name)
    walked = plan_poses(run_preset(preset_id, name), targets)
    entry = {"layout": name, "description": named.description, "use_when": named.use_when, "mover": named.mover,
             "poses": [{key: value for key, value in asdict(pose).items() if value not in (None, "")}
                       for pose in named.poses],
             "targets": [driver for driver in dict.fromkeys(pose.driver for pose in walked) if driver in targets],
             "captures": None, "seconds": None, "refused": None}
    try:
        plan, _ = resolve_plan(RunRequest(program=preset_id, layout=name), targets=lambda: targets)
    except LateralWalkRefused as exc:
        return {**entry, "refused": exc.reason}
    facts = preview_schedule(plan, prepare_plan_captures(plan, roles_bands=context.roles_bands), context)
    return {**entry, "captures": facts["measurements"], "seconds": round(facts["estimated_seconds"])}
