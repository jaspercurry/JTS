# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The capture schedule shared by execution, previews, and prices."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import groupby
from typing import Sequence

from jasper.audio_measurement.program import RoleBand
from .angle_capture import AngleCaptureRequest, AngleStop, ResolvedStop, resolve_request, stop_specs, design_axis_spec
from .crossover_v2.capture_plan import wall_clock_ceiling_s
from .crossover_v2.journey import PHASE_CHECK, PHASE_ENTRY_BASELINE, PHASE_MEASURE, PHASE_LATERAL
from .crossover_v2.measure_spec import MeasureSpec
from .measurement_programs import (
    BASE_CANDIDATE, REGIME_PER_DRIVER, REGIME_SUMMED, PURPOSE_SPEAKER,
    POSE_KIND_BEARING, BRANCH_PAIR_DRIVERS, candidate_identity,
)


@dataclass(frozen=True)
class PlanCapture:
    stop: AngleStop
    spec: MeasureSpec
    repeat: int = 1

    def resolved(self, request: AngleCaptureRequest) -> ResolvedStop:
        return resolve_request(replace(request, stops=(self.stop,),
            candidates=(self.stop.candidate_id or BASE_CANDIDATE,), repeats=1))[0]


def prepare_plan_captures(
    request: AngleCaptureRequest, *, roles_bands: Sequence[RoleBand] = (),
) -> tuple[PlanCapture, ...]:
    """Derive preparation and requested captures together (ADR-0297)."""
    resolved = resolve_request(request)
    placed = stop_specs(request,
                        prompts=tuple(stop.prompt for stop in resolved), baseline_id=BASE_CANDIDATE,
                        roles_bands=roles_bands)
    captures: list[PlanCapture] = []
    if any(stop.regime == REGIME_PER_DRIVER for stop in request.stops):
        captures.append(PlanCapture(
            AngleStop(0, REGIME_PER_DRIVER),
            replace(design_axis_spec(request), program_phase=PHASE_CHECK),
        ))
    base_stop = next((stop for stop in request.stops if candidate_identity(stop.candidate_id) == BASE_CANDIDATE and stop.purpose == PURPOSE_SPEAKER), None)
    # The speaker flow needs an entry baseline; other rounds use their first take as the level reference.
    if base_stop is not None:
        base_request = replace(request, stops=(replace(base_stop, angle_deg=0, elevation_deg=0,
            kind=POSE_KIND_BEARING, distance_m=None, seat_offset_m=None,
            headline="", detail="", regime=REGIME_SUMMED, branch_pair=BRANCH_PAIR_DRIVERS),),
                               candidates=(), repeats=1)
        base_spec, = stop_specs(base_request,
                                prompts=(resolve_request(base_request)[0].prompt,), baseline_id=BASE_CANDIDATE,
                                roles_bands=roles_bands)
        assert base_spec is not None
        captures.extend(PlanCapture(base_request.stops[0], replace(
            base_spec, graph_scope="timing", program_phase=PHASE_ENTRY_BASELINE,
        ), repeat) for repeat in range(1, request.repeats + 1))
    for offset, spec in enumerate(placed):
        stop = request.stops[offset // request.repeats]
        if spec is None:
            spec = replace(design_axis_spec(request), positions=(stop.angle_deg,),
                           vertical_deg=stop.elevation_deg,
                           pose_prompts=(resolved[offset // request.repeats].prompt.text,))
            if stop.driver:
                spec = replace(spec, branch_target_ids=(stop.driver,), regime=stop.regime)
        captures.append(PlanCapture(stop, replace(spec, program_phase=(
            PHASE_MEASURE if stop.regime == REGIME_PER_DRIVER else PHASE_LATERAL
        )), offset % request.repeats + 1))
    return tuple(captures)


def walk_price(request: AngleCaptureRequest, *, roles_bands: Sequence[RoleBand] = ()) -> dict[str, int | float | None]:
    """Price the same capture schedule shown by the page, including preparation."""
    captures = len(prepare_plan_captures(request, roles_bands=roles_bands))
    return {
        "mic_moves": sum(1 for _place, _stops in groupby(s.place for s in request.stops)),
        "captures": captures,
        "ceiling_min": math.ceil(
            wall_clock_ceiling_s(captures) / 60
        ),
        "stimulus_s": (
            None if request.template.sweep_s is None
            else captures * request.template.sweep_s
            * max(1, len(request.template.level_ladder_dbfs))
        ),
    }


