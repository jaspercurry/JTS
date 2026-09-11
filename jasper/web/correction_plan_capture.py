# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind banked captures to the program analyzer and legacy preparation effects."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from jasper.active_speaker.crossover_v2.capture_dispatch import assess
from jasper.active_speaker.crossover_v2.journey import PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY, PHASE_CLOUD_VERIFY
from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict
from jasper.audio_measurement.program import BASE_STIMULUS_PEAK_DBFS, ExcitationProgram
from jasper.audio_measurement.branch_program import build_branch_program


def bind_plan_analysis(conductor: Any, records: Any, *, verify_only: bool = False) -> tuple[Any, Any]:
    answer: Any = None
    index = attempt = 0
    phase = ""

    def enrich(capture: Any, record: Any) -> dict[str, Any]:
        nonlocal answer
        answer = capture
        return {}

    records.enrich = enrich

    def analyze(record: Any, record_id: str) -> Any:
        nonlocal index, attempt, phase
        index, attempt = record["index"], record["attempt"]
        phase = conductor._phase_of_index(index)
        priors = (conductor._check_priors() if phase == PHASE_CHECK else
                  conductor._measure_priors() if phase == PHASE_MEASURE else
                  conductor._verify_priors() if verify_only else conductor._lateral_priors())
        return conductor._seams.analyze(
            ExcitationProgram.from_dict(record["program"]), answer, priors,
            conductor._capture_geometry(phase, index), phase=phase,
        )

    def assessor(analysis: Any, **kwargs: Any) -> TakeVerdict:
        if phase == PHASE_CHECK:
            verdict = conductor._check_verdict(analysis)
        elif verify_only:
            verdict = (conductor._consume_verify(index, attempt, analysis, answer, phase=phase)
                       if phase == PHASE_VERIFY else
                       conductor._consume_cloud_position(PHASE_CLOUD_VERIFY, index, attempt, analysis, answer))
        else:
            return assess(analysis, **kwargs)
        if verdict.accepted:
            conductor._note_accepted(phase, index)
        return TakeVerdict(verdict.accepted, fault=verdict.code, evidence=verdict.evidence,
                           capabilities=verdict.capabilities, next=verdict.next or (
                               "accept" if verdict.accepted else "fix_and_retake"),
                           next_gain_db=verdict.next_gain_db, charge=verdict.charge)

    return analyze, assessor


def compose_plan_program(conductor: Any, spec: Any, stimulus_dbfs: float | None) -> Any:
    excitation = conductor._excitation
    if spec.program_phase == PHASE_CHECK:
        return excitation.check_program()
    if spec.graph_scope == "drivers":
        gains = conductor._gain_plan_db
        if not gains:
            raise ValueError("The CHECK level solve is unavailable")
        if stimulus_dbfs is not None:
            gains = {role: stimulus_dbfs for role in gains}
        return excitation.measure_program(gains)
    excitation = replace(excitation, summed_sweep_band_hz=spec.sweep_band_hz or None)
    program = excitation.verify_program(
        extra_backoff_db=0.0 if stimulus_dbfs is None else BASE_STIMULUS_PEAK_DBFS - stimulus_dbfs,
        sweep_s=spec.sweep_s,
    )
    if spec.graph_scope == "candidate_branches":
        program = build_branch_program(program, {role.role: role.channel for role in excitation.roles})
    return program
