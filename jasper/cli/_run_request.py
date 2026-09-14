# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Resolve the CLI's run flags through the plan and preflight owners."""
from __future__ import annotations

import argparse
from dataclasses import replace

from jasper.active_speaker.angle_capture import (
    AngleCaptureRequest, LevelPolicy, LateralWalkRefused, WALK_LEVEL_POLICY_INVALID, request_for_program,
)
from jasper.active_speaker.measurement_programs import run_program
from jasper.active_speaker.bass_levels import BassLevelLadder, preflight_levels
from jasper.active_speaker.preflight import PreflightReport, preflight
from jasper.active_speaker.preflight_live import read_preflight_facts
from jasper.active_speaker.seat_level_reference import seat_level_reference_state_path
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.audio_measurement.household_mic import household_mic_path
from jasper.output_topology import topology_path
from ._refusal import read_json_source


def resolve_run(args: argparse.Namespace) -> PreflightReport | BassLevelLadder:
    # Shared loaders suppress read faults; keep this CLI check until they expose them.
    for path in (topology_path(), baseline_profile_state_path(), household_mic_path(), seat_level_reference_state_path()):
        try:
            with path.open("rb"):
                pass
        except PermissionError:
            raise
        except OSError:
            pass
    if args.plan:
        if any(getattr(args, key) is not None for key in ("program", "poses", "candidates", "repeats", "mover", "level_db", "levels")):
            raise ValueError("a plan document already states its run parameters")
        document = read_json_source(args.plan)
        if not isinstance(document, dict):
            raise ValueError("plan must be an object")
        request = AngleCaptureRequest.from_mapping(document)
        return preflight(request, read_preflight_facts(request))
    program = run_program(args.program or "speaker", args.poses)
    if args.repeats is not None:
        try:
            program = replace(program, poses=tuple(replace(pose, repeats=args.repeats) for pose in program.poses))
        except ValueError as exc:
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, str(exc)) from exc
    candidates = tuple(value.strip() for value in args.candidates.split(",")) if args.candidates is not None else ()
    if any(not value for value in candidates):
        raise ValueError("candidates must name a fingerprint or base")
    if args.mover:
        program = replace(program, mover="arm" if args.mover == "arm" else "human")
    request = request_for_program(
        program, candidates=candidates, level=LevelPolicy(level_db=args.level_db),
        mover=program.mover or "human",
    )
    facts = read_preflight_facts(request)
    levels = args.levels if args.levels is not None else ("auto" if args.program == "bass" and args.level_db is None else None)
    return preflight_levels(request, facts, levels)
