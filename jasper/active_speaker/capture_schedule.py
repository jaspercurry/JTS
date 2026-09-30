# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The capture schedule shared by execution, previews, and prices."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import groupby
from typing import Sequence

from jasper.audio_measurement.program import RoleBand
from .angle_capture import (
    AngleCaptureRequest, AngleStop, ResolvedStop, design_axis_spec, level_sets, resolve_request, stop_specs,
)
from .crossover_v2.capture_plan import wall_clock_ceiling_s
from .crossover_v2.journey import PHASE_CHECK, PHASE_MEASURE, PHASE_LATERAL, PHASE_TIMING
from .crossover_v2.measure_spec import MeasureSpec
from .measurement_programs import (
    BASE_CANDIDATE, REGIME_PER_DRIVER, REGIME_SUMMED, PURPOSE_SPEAKER,
    Pose, UnknownPresetError, candidate_identity, preset,
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
    if any(stop.regime == REGIME_PER_DRIVER and not stop.pose.driver for stop in request.stops):
        captures.append(PlanCapture(
            AngleStop(Pose(0, 0), REGIME_PER_DRIVER, purpose=PURPOSE_SPEAKER),
            replace(design_axis_spec(request), program_phase=PHASE_CHECK),
        ))
    if takes_timing(request):
        base_request = replace(request, stops=(AngleStop(Pose(0, 0), REGIME_SUMMED, purpose=PURPOSE_SPEAKER),),
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
            spec = replace(design_axis_spec(request), positions=(stop.pose.azimuth_deg,),
                           vertical_deg=stop.pose.elevation_deg, stimulus=stop.stimulus,
                           pose_prompts=(resolved[offset // request.repeats].prompt.text,),
                           branch_target_ids=(stop.pose.driver,) if stop.pose.driver else ())
        captures.append(PlanCapture(stop, replace(spec, program_phase=(
            PHASE_MEASURE if stop.regime == REGIME_PER_DRIVER and not stop.pose.driver else PHASE_LATERAL
        )), offset % request.repeats + 1))
    # A driver's takes, and the first take of each close driverless set, find their level (ADR-0365, ADR-0403).
    starts = level_sets([capture.stop for capture in captures])
    return tuple(replace(capture, spec=replace(capture.spec, level_probe=True))
                 if start is not None and (capture.stop.pose.driver or start == index) else capture
                 for index, (capture, start) in enumerate(zip(captures, starts)))


def takes_timing(request: AngleCaptureRequest) -> bool:
    """Whether the run takes its preset's timing take: the base's front drivers
    summed at the mark (ADR-0319), so only with a base stop that plays every
    driver (ADR-0366). A plan naming no preset takes none."""
    try:
        timing = preset(request.program).timing_take
    except UnknownPresetError:
        return False
    return timing and any(
        candidate_identity(stop.candidate_id) == BASE_CANDIDATE and not stop.pose.driver for stop in request.stops)


def walk_price(request: AngleCaptureRequest, *, roles_bands: Sequence[RoleBand] = ()) -> dict[str, int | float | None]:
    """Price the same capture schedule shown by the page, including preparation."""
    captures = len(prepare_plan_captures(request, roles_bands=roles_bands))
    return {
        "mic_moves": sum(1 for _place, _stops in groupby(s.pose.place for s in request.stops)),
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


