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
from .crossover_v2.contracts import REGIME_NEAR_FIELD as MEASURE_REGIME_NEAR_FIELD
from .crossover_v2.journey import PHASE_CHECK, PHASE_MEASURE, PHASE_LATERAL, PHASE_TIMING
from .crossover_v2.measure_spec import MeasureSpec
from .measurement_programs import (
    BASE_CANDIDATE, REGIME_NEAR_FIELD, REGIME_PER_DRIVER, REGIME_SUMMED, PURPOSE_SPEAKER,
    UnknownPresetError, candidate_identity, preset,
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
    # CHECK plays every driver; a stop naming its driver plays that one alone and needs none (ADR-0366).
    if any(stop.regime == REGIME_PER_DRIVER and not stop.driver for stop in request.stops):
        captures.append(PlanCapture(
            AngleStop(0, REGIME_PER_DRIVER, purpose=PURPOSE_SPEAKER),
            replace(design_axis_spec(request), program_phase=PHASE_CHECK),
        ))
    # A preset's timing take plays the base's front drivers summed at the mark (ADR-0319), so it
    # needs a base stop that plays every driver (ADR-0366).
    if _takes_timing(request.program) and any(
            candidate_identity(stop.candidate_id) == BASE_CANDIDATE and not stop.driver for stop in request.stops):
        base_request = replace(request, stops=(AngleStop(0, REGIME_SUMMED, purpose=PURPOSE_SPEAKER),),
                               candidates=(), repeats=1)
        base_spec, = stop_specs(base_request,
                                prompts=(resolve_request(base_request)[0].prompt,), baseline_id=BASE_CANDIDATE,
                                roles_bands=roles_bands)
        assert base_spec is not None
        captures.extend(PlanCapture(base_request.stops[0], replace(
            base_spec, graph_scope="timing", program_phase=PHASE_TIMING,
        ), repeat) for repeat in range(1, request.repeats + 1))
    for offset, spec in enumerate(placed):
        stop = request.stops[offset // request.repeats]
        if spec is None:
            spec = replace(design_axis_spec(request), positions=(stop.angle_deg,),
                           vertical_deg=stop.elevation_deg,
                           pose_prompts=(resolved[offset // request.repeats].prompt.text,))
            if stop.driver:
                spec = replace(spec, branch_target_ids=(stop.driver,), regime=(
                    MEASURE_REGIME_NEAR_FIELD if stop.regime == REGIME_NEAR_FIELD else spec.regime))
        captures.append(PlanCapture(stop, replace(spec, program_phase=(
            PHASE_MEASURE if stop.regime == REGIME_PER_DRIVER and not stop.driver else PHASE_LATERAL
        )), offset % request.repeats + 1))
    return tuple(captures)


def _takes_timing(program: str) -> bool:
    """Whether the run's preset takes the timing take; a plan naming no preset takes none."""
    try:
        return preset(program).timing_take
    except UnknownPresetError:
        return False


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


