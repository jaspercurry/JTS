# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Resolve the CLI's run flags through the plan and preflight owners."""
from __future__ import annotations

import argparse
from dataclasses import replace

from jasper.active_speaker.angle_capture import (
    AngleCaptureRequest, LevelPolicy, LateralWalkRefused,
    WALK_CANDIDATE_NOT_MEASURABLE, WALK_LEVEL_POLICY_INVALID,
    default_run_level, request_for_program,
)
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, publish_authored_candidate
from jasper.active_speaker.crossover_v2.prescription_document import bank_section
from jasper.active_speaker.measurement_programs import (
    PURPOSE_REAR, REGIME_BRANCHES, run_program,
)
from jasper.active_speaker.run_levels import LevelLadder, preflight_levels
from jasper.active_speaker.preflight import PreflightFacts, PreflightReport
from jasper.active_speaker.movers import MOVER_ARM
from jasper.active_speaker.preflight_live import read_preflight_facts
from jasper.active_speaker.seat_level_reference import seat_level_reference_state_path
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.audio_measurement.bundles import BundleError
from jasper.audio_measurement.household_mic import household_mic_path
from jasper.output_topology_store import topology_path
from ._refusal import read_json_source


def _rear_cleared_candidate() -> str:
    """Bank the applied tune with its rear calibration cleared.

    A rear pair take measures the two woofers raw, so the stage a later
    prediction superposes may not already be in what was measured (issue
    #5330). Composed on ``--base saved`` like every other authored section;
    the candidate records the clearing in its own ``analysis.resolution``.
    """
    try:
        return publish_authored_candidate(bank_section(
            "rear_calibration", None,
            rationale="Measure both woofers with no rear stage.",
        )).fingerprint
    except (CandidateBankRefusal, BundleError) as exc:
        raise LateralWalkRefused(WALK_CANDIDATE_NOT_MEASURABLE, str(exc)) from exc


def _facts(request: AngleCaptureRequest, args: argparse.Namespace) -> PreflightFacts:
    available = True
    if request.mover == MOVER_ARM:
        from jasper.active_speaker.arm_walk import TurntableMover  # lazy: arm-only
        available = TurntableMover().available()
    return read_preflight_facts(request, mover_available=available,
                               rig_clear_attested=None if args.dry_run else args.attest_rig_clear)


def resolve_run(args: argparse.Namespace) -> PreflightReport | LevelLadder:
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
        if any(getattr(args, key) is not None for key in ("program", "poses", "candidates", "repeats", "mover", "level_db")):
            raise ValueError("a plan document already states its run parameters")
        document = read_json_source(args.plan)
        if not isinstance(document, dict):
            raise ValueError("plan must be an object")
        request = AngleCaptureRequest.from_mapping(document)
        return preflight_levels(request, _facts(request, args))
    program = run_program(args.program or "speaker", args.poses)
    if args.repeats is not None:
        try:
            program = replace(program, poses=tuple(replace(pose, repeats=args.repeats) for pose in program.poses))
        except ValueError as exc:
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, str(exc)) from exc
    candidates = tuple(value.strip() for value in args.candidates.split(",")) if args.candidates is not None else ()
    if any(not value for value in candidates):
        raise ValueError("candidates must name a fingerprint or base")
    if not candidates and (program.purpose, program.regime) == (PURPOSE_REAR, REGIME_BRANCHES):
        candidates = (_rear_cleared_candidate(),)
    operator_level = args.level_db is not None
    level, level_source = default_run_level(program, state_path=seat_level_reference_state_path())
    request = request_for_program(
        program, candidates=candidates,
        level=LevelPolicy(level_db=args.level_db) if operator_level else level,
        level_source="operator" if operator_level else level_source,
        mover=args.mover or program.mover or "human",
    )
    facts = _facts(request, args)
    return preflight_levels(request, facts, program.levels if args.level_db is None else None)
