# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Classify measurement failures for HTTP hosts and banked runs."""

import logging

from jasper.log_event import log_event
from jasper.platform import control_client
from .crossover_v2.capture_plan import PlanShapeError
from .crossover_v2.contracts import CrossoverV2FlowError
from .crossover_v2.program_transaction import (
    STIMULUS_ADMISSION_REFUSED, STIMULUS_LEVEL_NOT_READY, StimulusCaptureStopped,
)
from .crossover_v2.refusal_copy import (
    REASON_MEASUREMENT_VOLUME_DRIFT, REASON_MEASUREMENT_GRAPH_UNAVAILABLE,
    REASON_PROGRAM_PLAN_SHAPE_INVALID, REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID,
    REASON_PROGRAM_UNPLAYABLE, REASON_PROTECTION_NOT_SEPARABLE,
    REASON_PROTECTION_SWEEP_TOO_LOW, REASON_SPL_CEILING_EXCEEDED,
)
from .crossover_v2.session_graph import SessionGraphError
from .program_admission import ProgramAdmissionError, ProgramAdmissionRefusal
from .program_playback import ProgramPlaybackError, ProgramPlaybackRefused
from .session_volume_plan import SessionVolumePlanError
from .volume_latch import MeasurementFaderDrift
from jasper.audio_measurement.program_analysis import ConfiguredPathConditioningError
from jasper.audio_measurement.wired_capture import WiredSplCeilingExceeded


def read_output_volume() -> dict[str, float | bool]:
    try:
        response = control_client.get("/volume")
        state = response.json() if response.ok else None
    except (control_client.ControlError, ValueError):
        return {}
    if not isinstance(state, dict) or not isinstance(state.get("muted"), bool):
        return {}
    evidence = {"muted": state["muted"]}
    if isinstance(state.get("percent"), (int, float)):
        evidence["household_percent"] = state["percent"]
    if evidence["muted"]:
        log_event(logging.getLogger(__name__), "active_speaker.measurement_output_muted", **evidence)
    return evidence


def classify_program_failure(exc: BaseException) -> tuple[str, tuple[str, ...]] | None:
    """Return the reason and refusal codes, or None outside the measurement family."""
    from .measurement_emit import MeasurementGraphRefused  # lazy: graph import cost

    if isinstance(exc, MeasurementGraphRefused):
        return exc.code, ()
    if isinstance(exc, SessionGraphError):
        return REASON_MEASUREMENT_GRAPH_UNAVAILABLE, ()
    if (
        isinstance(exc, StimulusCaptureStopped)
        and exc.code == WiredSplCeilingExceeded.code
    ):
        return REASON_SPL_CEILING_EXCEEDED, ()
    if isinstance(exc, MeasurementFaderDrift):
        return REASON_MEASUREMENT_VOLUME_DRIFT, ()
    if isinstance(exc, ConfiguredPathConditioningError):
        return (
            REASON_PROTECTION_SWEEP_TOO_LOW if exc.protection_floor
            else REASON_PROTECTION_NOT_SEPARABLE
        ), (exc.slug,)
    if isinstance(exc, PlanShapeError):
        return REASON_PROGRAM_PLAN_SHAPE_INVALID, ()
    if isinstance(exc, SessionVolumePlanError):
        return STIMULUS_LEVEL_NOT_READY, ()
    if not isinstance(
        exc, (ProgramPlaybackError, ProgramAdmissionError, CrossoverV2FlowError)
    ):
        return None
    refusals: tuple[str, ...] = ()
    if isinstance(exc, ProgramPlaybackRefused):
        refusals = tuple(reason.value for reason in exc.admission.refusals)
    code = (
        REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID
        if ProgramAdmissionRefusal.MEASUREMENT_INPUTS_INVALID.value in refusals
        else STIMULUS_ADMISSION_REFUSED if isinstance(exc, ProgramPlaybackRefused)
        else REASON_PROGRAM_UNPLAYABLE
    )
    return code, refusals
