# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Read the run the CLI asks for, and for a dry run this speaker's facts; the plan comes from ``run_request``."""
from __future__ import annotations

import argparse
import json
from typing import Any

from jasper.active_speaker.measurement_programs import near_field_drivers
from jasper.active_speaker.run_request import REQUEST_KEYS, RunRequest, resolve_plan
from jasper.active_speaker.preflight import PreflightReport, priced_preflight
from jasper.active_speaker.arm_walk import mover_present
from jasper.active_speaker.preflight_live import read_preflight_facts
from jasper.audio_routes.output_topology_store import load_output_topology


def read_request(args: argparse.Namespace) -> tuple[dict[str, Any], RunRequest]:
    """The run as the CLI posts it, keyed by the flags' names, and as the session door reads it."""
    stated = {key: getattr(args, key) for key in REQUEST_KEYS if getattr(args, key) is not None}
    if stated and args.request:
        raise ValueError("a request document already states its run parameters")
    asked = json.loads(args.request) if args.request else stated
    return asked, RunRequest.from_mapping(asked)


def preflight_run(request: RunRequest) -> PreflightReport:
    """A dry run: the plan the session door would resolve, judged on this speaker's own facts."""
    plan = resolve_plan(request, targets=lambda: near_field_drivers(load_output_topology()))
    return priced_preflight(plan, read_preflight_facts(plan, mover_available=mover_present(plan.mover)))
