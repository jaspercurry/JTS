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


def bind_plan_analysis(conductor: Any, records: Any, *, manifest: Any, evidence: Any,
                       verify_only: bool = False) -> tuple[Any, Any]:
    answers: dict[str, Any] = {}
    index = attempt = 0
    phase = ""
    verdict: Any = None

    def enrich(capture: Any, record: Any) -> dict[str, Any]:
        answers[record["take_id"]] = capture
        return {}

    def after_bank(record: Any, record_id: str) -> None:
        answers[record_id] = answers.pop(record["take_id"])

    records.enrich, records.after_bank = enrich, after_bank

    def analyze(record: Any, record_id: str) -> Any:
        nonlocal index, attempt, phase, verdict
        answer = answers.pop(record_id)
        index, attempt = record["index"], record["attempt"]
        phase = conductor._phase_of_index(index)
        priors = (conductor._check_priors() if phase == PHASE_CHECK else
                  conductor._measure_priors() if phase == PHASE_MEASURE else
                  conductor._verify_priors() if verify_only else conductor._lateral_priors())
        analysis = conductor._seams.analyze(
            ExcitationProgram.from_dict(record["program"]), answer, priors,
            conductor._capture_geometry(phase, index), phase=phase,
        )
        # Legacy grading crosses the loop bridge, so it must stay in the analyzer's worker.
        verdict = None
        if phase == PHASE_CHECK:
            verdict = conductor._check_verdict(analysis)
        elif verify_only:
            verdict = (conductor._consume_verify(index, attempt, analysis, answer, phase=phase)
                       if phase == PHASE_VERIFY else
                       conductor._consume_cloud_position(PHASE_CLOUD_VERIFY, index, attempt, analysis, answer))
        calibration = evidence.get("calibration", {}).get(phase, {})
        manifest.calibration = {"id": calibration.get("calibration_id"),
                                "curve_fingerprint": calibration.get("curve_fingerprint")}
        return analysis

    def assessor(analysis: Any, **kwargs: Any) -> TakeVerdict:
        if verdict is None:
            assessed = assess(analysis, **kwargs)
            if phase == PHASE_MEASURE and assessed.next in {"retake_louder", "retake_quieter"}:
                conductor._rearm_measure_after_transient(assessed)
            return assessed
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
        program = excitation.check_program()
        peak = max(segment.gain_db for segment in program.stimulus_segments())
        conductor._check_program = excitation.check_program(
            extra_backoff_db=0.0 if stimulus_dbfs is None else peak - stimulus_dbfs)
        return conductor._check_program
    if spec.graph_scope == "drivers":
        gains = conductor._gain_plan_db
        if not gains:
            raise ValueError("The CHECK level solve is unavailable")
        if stimulus_dbfs is not None and stimulus_dbfs != max(gains.values()):
            delta = stimulus_dbfs - max(gains.values())
            gains = {role: gain + delta for role, gain in gains.items()}
        return excitation.measure_program(gains)
    excitation = replace(excitation, summed_sweep_band_hz=spec.sweep_band_hz or None)
    backoff = 0.0 if stimulus_dbfs is None else BASE_STIMULUS_PEAK_DBFS - stimulus_dbfs
    program = (excitation.cloud_program(extra_backoff_db=backoff) if spec.program_phase == PHASE_CLOUD_VERIFY
               else excitation.verify_program(extra_backoff_db=backoff, sweep_s=spec.sweep_s))
    if spec.program_phase == PHASE_VERIFY:
        conductor._verify_program = program
    elif spec.program_phase == PHASE_CLOUD_VERIFY:
        conductor._cloud_program = program
    if spec.graph_scope == "candidate_branches":
        program = build_branch_program(program, {role.role: role.channel for role in excitation.roles})
    return program
