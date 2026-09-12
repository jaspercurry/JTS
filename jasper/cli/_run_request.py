# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Resolve the CLI's run flags through the plan and preflight owners."""
from __future__ import annotations

import argparse
from dataclasses import replace

from jasper.active_speaker.angle_capture import AngleCaptureRequest, request_for_program
from jasper.active_speaker.measurement_programs import run_program
from jasper.active_speaker.preflight import PreflightReport, preflight
from jasper.active_speaker.preflight_live import read_preflight_facts
from ._refusal import read_json_source


def resolve_run(args: argparse.Namespace) -> PreflightReport:
    if args.plan:
        if any(getattr(args, key) is not None for key in ("program", "poses", "candidates", "level", "ceiling", "repeats", "mover")):
            raise ValueError("a plan document already states its run parameters")
        document = read_json_source(args.plan)
        if not isinstance(document, dict):
            raise ValueError("plan must be an object")
        request = AngleCaptureRequest.from_mapping(document)
        return preflight(request, read_preflight_facts(request))
    program = run_program(args.program or "speaker", args.poses)
    if args.repeats is not None:
        program = replace(program, poses=tuple(replace(pose, repeats=1) for pose in program.poses))
    candidates = tuple(value.strip() for value in args.candidates.split(",")) if args.candidates is not None else ()
    if any(not value for value in candidates):
        raise ValueError("candidates must name a fingerprint or base")
    request = request_for_program(
        program, candidates=candidates, mover="arm" if args.mover == "arm" else "human",
        repeats=args.repeats if args.repeats is not None else 1,
        spl_ceiling_db_spl=args.ceiling,
        operating_levels_db=() if args.level is None else (args.level,),
    )
    return preflight(request, read_preflight_facts(request))
