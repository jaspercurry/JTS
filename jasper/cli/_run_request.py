# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Read the CLI's run source and the facts only it can see; the plan comes from ``run_request``."""
from __future__ import annotations

import argparse
import json

from jasper.active_speaker.angle_capture import AngleCaptureRequest
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_WALK_MOVER_UNAVAILABLE, REASON_WALK_RIG_CLEAR_NOT_ATTESTED
from jasper.active_speaker.measurement_programs import near_field_drivers
from jasper.active_speaker.run_levels import LevelLadder, preflight_levels
from jasper.active_speaker.run_request import REQUEST_KEYS, RunRequest, resolve_plan
from jasper.active_speaker.preflight import PreflightFacts, PreflightReport
from jasper.active_speaker.arm_walk import mover_present
from jasper.active_speaker.preflight_live import read_preflight_facts
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.audio_measurement.household_mic import household_mic_path
from jasper.audio_routes.output_topology_store import load_output_topology, topology_path
from ._refusal import read_json_source

#: The arm facts this CLI refuses on before it posts; the session door reads them again and owns admission.
ARM_FACT_CODES = (REASON_WALK_RIG_CLEAR_NOT_ATTESTED, REASON_WALK_MOVER_UNAVAILABLE)


def _facts(request: AngleCaptureRequest, args: argparse.Namespace) -> PreflightFacts:
    return read_preflight_facts(request, mover_available=mover_present(request.mover),
                               rig_clear_attested=None if args.dry_run else args.attest_rig_clear)


def resolve_run(args: argparse.Namespace) -> PreflightReport | LevelLadder:
    # Shared loaders suppress read faults; keep this CLI check until they expose them.
    for path in (topology_path(), baseline_profile_state_path(), household_mic_path()):
        try:
            with path.open("rb"):
                pass
        except PermissionError:
            raise
        except OSError:
            pass
    stated = {key: getattr(args, key) for key in REQUEST_KEYS if getattr(args, key) is not None}
    if stated and (args.plan or args.request):
        raise ValueError("a plan or request document already states its run parameters")
    if args.plan:
        document = read_json_source(args.plan)
        if not isinstance(document, dict):
            raise ValueError("plan must be an object")
        source: RunRequest | AngleCaptureRequest = AngleCaptureRequest.from_mapping(document)
    else:
        source = RunRequest.from_mapping(json.loads(args.request) if args.request else stated)
    plan, levels = resolve_plan(source, targets=lambda: near_field_drivers(load_output_topology()))
    return preflight_levels(plan, _facts(plan, args), levels)
