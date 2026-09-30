# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""One run request, resolved to the plan it runs, for the CLI, the page and the daemon (#5737)."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Callable, Mapping, Sequence

from .angle_capture import (
    AngleCaptureRequest, LateralWalkRefused, LevelPolicy, WALK_LEVEL_POLICY_INVALID, request_for_preset,
)
from .measurement_programs import PURPOSE_SPEAKER, run_preset
from .movers import MOVER_HUMAN


@dataclass(frozen=True)
class RunRequest:
    """What a run asks for, keyed by the ``jasper-round run`` flags' names.

    ``program`` is a preset id, or a program name for its first preset.
    ``layout`` names a layout the preset offers; ``poses`` is instead an inline
    list of pose objects or whole-degree bearings, or its flag text.
    ``candidates`` are fingerprints or ``base``, as a list or the flag text."""

    program: str = PURPOSE_SPEAKER
    layout: str | None = None
    poses: str | tuple[Any, ...] | None = None
    driver: str = ""
    candidates: tuple[str, ...] = ()
    repeats: int | None = None
    mover: str | None = None
    level_db: float | None = None

    @classmethod
    def from_mapping(cls, doc: Any) -> RunRequest:
        if not isinstance(doc, Mapping) or set(doc) - set(REQUEST_KEYS):
            raise ValueError(f"a run request is an object of {', '.join(REQUEST_KEYS)}")
        values = {key: value for key, value in doc.items() if value is not None}
        if not all(isinstance(values.get(key, ""), str) for key in ("program", "layout", "driver", "mover")):
            raise ValueError("program, layout, driver and mover are text")
        if "layout" in values and "poses" in values:
            raise ValueError("a request names a layout or its own poses, not both")
        poses = values.get("poses")
        if poses is not None and not isinstance(poses, (str, list)):
            raise ValueError("poses is a list, or its flag text")
        candidates = values.get("candidates", [])
        if isinstance(candidates, str):
            candidates = [value.strip() for value in candidates.split(",")]
        if not isinstance(candidates, list) or not all(isinstance(value, str) and value for value in candidates):
            raise ValueError("candidates must name a fingerprint or base")
        return cls(**{**values, "poses": tuple(poses) if isinstance(poses, list) else poses,
                      "candidates": tuple(candidates)})


REQUEST_KEYS = tuple(field.name for field in fields(RunRequest))


def resolve_plan(source: RunRequest | AngleCaptureRequest, *,
                 targets: Callable[[], Sequence[str]]) -> tuple[AngleCaptureRequest, str | None]:
    """The plan a request runs, and ``"auto"`` when its preset steps a level ladder (ADR-0365).

    A request plays its preset's poses with each driver role expanded to
    ``targets``, the outputs this speaker plays alone, its candidates (a rear
    pair's parent is the applied base, ADR-0386), and at its stated level, or at
    the level its run finds (ADR-0403 §4)."""
    if isinstance(source, AngleCaptureRequest):
        return source, None
    preset = run_preset(source.program, source.layout, source.poses)
    if source.repeats is not None:
        try:
            preset = replace(preset, poses=tuple(replace(pose, repeats=source.repeats) for pose in preset.poses))
        except ValueError as exc:
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, str(exc)) from exc
    level, level_source = ((LevelPolicy(level_db=source.level_db), "operator") if source.level_db is not None
                           else (LevelPolicy(), "program_default"))
    plan = request_for_preset(preset, candidates=source.candidates, level=level, level_source=level_source,
                              mover=source.mover or preset.mover or MOVER_HUMAN, targets=targets(),
                              driver=source.driver)
    return plan, preset.levels if source.level_db is None else None
