# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind banked captures to the program analyzer and legacy preparation effects."""
from __future__ import annotations

from jasper.web import correction_crossover_v2_volume as v2volume

from dataclasses import replace
from typing import Any

from jasper.active_speaker.crossover_v2.programs import courtesy_prelude_for_phase
from jasper.active_speaker.angle_capture import LevelPolicy
from jasper.active_speaker.run_levels import LevelLadder, LevelRun, prepare_level_captures, run_levels
from jasper.active_speaker.round_packet import RoundPacket
from jasper.active_speaker.run_manifest import RunManifest
from jasper.active_speaker.crossover_v2.door import isolation_hold
from jasper.active_speaker.crossover_v2.capture_provenance import enrich_capture_record
from jasper.active_speaker.crossover_v2.session import TuningSession
from jasper.active_speaker.crossover_v2.summed_alignment import banked_entry_baseline
from jasper.active_speaker.crossover_v2.wired_stimulus import CapturedRecordStore
from jasper.active_speaker.plan_run import RunDoor
from jasper.audio_measurement.household_mic import resolved_household_sensitivity

from jasper.active_speaker.crossover_v2.capture_dispatch import assess
from jasper.active_speaker.crossover_v2.journey import PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY, PHASE_CLOUD_VERIFY, PHASE_ENTRY_BASELINE
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_INTERNAL_ERROR, TakeVerdict, PhaseVerdict
from jasper.active_speaker.seat_level_reference import check_target_capture_dbfs as anchored_check_target
from jasper.audio_measurement.program import BASE_STIMULUS_PEAK_DBFS, ExcitationProgram
from jasper.audio_measurement.branch_program import build_branch_program


def bind_plan_analysis(conductor: Any, records: Any, *, manifest: Any, evidence: Any,
                       verify_only: bool = False, provenance: Any = None,
                       check_target_capture_dbfs: float | None = None,
                       capture_indexes: tuple[int, ...] = ()) -> tuple[Any, Any]:
    answers: dict[str, tuple[Any, Any]] = {}
    index = attempt = 0
    phase = ""
    answer: Any = None

    def index_of(record: Any) -> int:
        index = record.get("capture_index", record["index"])
        return capture_indexes[index - 1] if capture_indexes else index

    def enrich(capture: Any, record: Any) -> dict[str, Any]:
        captured = provenance.take() if provenance is not None else None
        record = manifest.capture_record(record)
        program = getattr(capture, "program", None) or record.get("program")
        fields: dict[str, Any] = {}
        result: Any = KeyError("program")
        if program is not None:
            try:
                phase = conductor._phase_of_index(index_of(record))
                result = analyze_capture({**record, "program": program}, capture)
                fields = evidence.get("capture_provenance", {}).get(phase, {})
            except Exception as exc:  # noqa: BLE001 - bank raw evidence before the executor propagates failure
                result = exc
        if isinstance(result, Exception):
            fields = {"analysis_error": {"code": REASON_INTERNAL_ERROR, "error_type": type(result).__name__}}
        answers[record["take_id"]] = capture, result
        return enrich_capture_record({
            **record, **fields, "mark_distance_m": record.get("mark_distance_m"),
            "phase": record.get("program_phase"),
            **({"provenance": captured.to_dict()} if captured is not None else {}),
        }, layout=conductor._preset.channel_map.layout)

    def after_bank(record: Any, record_id: str) -> None:
        answers[record_id] = answers.pop(record["take_id"])
        _, analysis = answers[record_id]
        if record.get("phase") == PHASE_ENTRY_BASELINE and not isinstance(analysis, Exception):
            conductor._measure_entry_baseline = banked_entry_baseline(record, analysis)

    records.enrich, records.after_bank = enrich, after_bank

    def analyze_capture(record: Any, capture: Any) -> Any:
        index = index_of(record)
        phase = conductor._phase_of_index(index)
        priors = (conductor._check_priors() if phase == PHASE_CHECK else
                  conductor._measure_priors() if phase == PHASE_MEASURE else
                  conductor._verify_priors() if verify_only else conductor._lateral_priors())
        if phase == PHASE_CHECK and check_target_capture_dbfs is not None:
            priors = replace(priors, target_capture_dbfs=check_target_capture_dbfs)
        analysis = conductor._seams.analyze(
            ExcitationProgram.from_dict(record["program"]), capture, priors,
            conductor._capture_geometry(phase, index), phase=phase,
        )
        calibration = evidence.get("calibration", {}).get(phase, {})
        manifest.calibration = {"id": calibration.get("calibration_id"),
                                "curve_fingerprint": calibration.get("curve_fingerprint")}
        return analysis

    def analyze(record: Any, record_id: str) -> Any:
        nonlocal index, attempt, phase, answer
        answer, analysis = answers.pop(record_id)
        index, attempt = index_of(record), record["attempt"]
        phase = conductor._phase_of_index(index)
        if isinstance(analysis, Exception):
            raise analysis
        return analysis

    def assessor(analysis: Any, **kwargs: Any) -> TakeVerdict:
        # Legacy grading crosses the loop bridge; the executor runs this in its worker.
        level_verdict = kwargs.get("level_verdict")
        verdict = None
        if level_verdict is not None and not level_verdict.ok:
            verdict = PhaseVerdict.from_take(level_verdict)
        elif phase == PHASE_CHECK:
            verdict = conductor._check_verdict(analysis)
        elif verify_only:
            verdict = (conductor._consume_verify(index, attempt, analysis, answer, phase=phase)
                       if phase == PHASE_VERIFY else
                       conductor._consume_cloud_position(PHASE_CLOUD_VERIFY, index, attempt, analysis, answer))
        prior = None if verdict is None else TakeVerdict(verdict.accepted, fault=verdict.code, evidence=verdict.evidence,
                           capabilities=verdict.capabilities, next=verdict.next or (
                               "accept" if verdict.accepted else "fix_and_retake"),
                           next_gain_db=verdict.next_gain_db, charge=verdict.charge)
        if kwargs.get("phase") == PHASE_MEASURE:
            kwargs.update(gain_ceiling_db=conductor._measure_gain_ceiling_db, caps_dbfs=conductor._excitation.caps_dbfs,
                          session_volume_db=conductor._excitation.session_volume_db,
                          spl_stop_db_spl=conductor._preset.safety.max_commissioning_level_db_spl,
                          spl=(getattr(answer, "capture_integrity", None) or {}).get("spl"))
        assessed = assess(analysis, prior_verdict=prior, **kwargs)
        if verdict is None and phase == PHASE_MEASURE and assessed.next in {"retake_louder", "retake_quieter"}:
            conductor._rearm_measure_after_transient(assessed)
        elif verdict is not None and assessed.ok:
            conductor._note_accepted(phase, index)
        return assessed

    return analyze, assessor


def compose_plan_program(conductor: Any, spec: Any, stimulus_dbfs: float | None, *, context: Any) -> Any:
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
    if spec.stimulus is not None:
        from jasper.active_speaker.bass_stimulus import build_bass_program  # lazy: keeps jasper.web numpy-free

        program = build_bass_program(excitation, spec.stimulus, safety_profile=context.safety_profile,
                                     role_targets=context.role_targets, extra_backoff_db=backoff,
                                     courtesy_prelude=courtesy_prelude_for_phase(spec.program_phase))
    else:
        program = (excitation.cloud_program(extra_backoff_db=backoff) if spec.program_phase == PHASE_CLOUD_VERIFY
                   else excitation.verify_program(extra_backoff_db=backoff, sweep_s=spec.sweep_s))
    if spec.program_phase == PHASE_VERIFY:
        conductor._verify_program = program
    elif spec.program_phase == PHASE_CLOUD_VERIFY:
        conductor._cloud_program = program
    if spec.graph_scope == "candidate_branches":
        program = build_branch_program(program, {role.role: role.channel for role in excitation.roles})
    return program


def bind_run_door(*, host: Any, device: Any, evidence_store: Any,
                  manifest: Any, production: Any, conductor: Any, refs: Any,
                  trims: Any, ceiling_s: float, ceiling_db_spl: float | None,
                  camilla_factory: Any, verify_only: bool, provenance: Any = None,
                  level: LevelPolicy = LevelPolicy(), ladder: LevelLadder | None = None,
                  capture_indexes: tuple[int, ...] = ()) -> tuple[RunDoor, Any, Any, Any]:
    if ladder is not None:
        ceiling_s *= len(ladder.admissible)
    sensitivity = resolved_household_sensitivity(device)
    predicted = level.predicted_db_spl
    check_target = (anchored_check_target(sensitivity, predicted)
                    if predicted is not None and sensitivity is not None else None)
    records = CapturedRecordStore(manifest, None)
    analyze, assessor = bind_plan_analysis(conductor, records, manifest=manifest,
                                          evidence=refs, verify_only=verify_only, provenance=provenance,
                                          check_target_capture_dbfs=check_target, capture_indexes=capture_indexes)

    def build(door: Any, allocate_take_id: Any) -> TuningSession:
        capture = host._wired_stimulus_capture(
            device, evidence_store, spl_monitor=door.spl_monitor,
            read_loudness_volume_db=lambda: camilla_factory().get_loudness_volume_db(best_effort=True),
        )
        records.capture = capture
        conductor._excitation = replace(conductor._excitation, session_volume_db=door.measurement_volume_db)
        return TuningSession(
            manifest.run_id, host.bind_v2_engine_seams(
                session_graph=door.graph, compose_stimulus=production.compose,
                capture_stimulus=capture, records=records, volume_claim=door.claim,
            ), door.measurement_volume_db, allocate_take_id, level_match_trims_db=trims,
        )

    door = RunDoor(
        isolation_hold(graph=production.graph, camilla_factory=camilla_factory,
                       action="measuring", plan=v2volume.session_volume_plan(), wall_clock_ceiling_s=ceiling_s),
        build, sensitivity, device, ceiling_db_spl,
    )
    if ladder is None or ladder.plan.levels is None:
        return door, analyze, assessor, None

    async def execute(request: Any, *, gate: Any, signals: Any, captures: Any, **_kwargs: Any) -> Any:
        packet = RoundPacket(manifest, ladder.to_dict())
        bound: LevelRun | None = None

        def prepare(plan: Any) -> LevelRun:
            nonlocal bound
            child = RunManifest(f"{manifest.run_id}-level-{len(packet.runs) + 1}", packet,
                                incumbent=manifest.incumbent)
            selected = prepare_level_captures(plan, roles_bands=conductor._roles)
            child_door, child_analyze, child_assessor, _ = bind_run_door(
                host=host, device=device, evidence_store=evidence_store, manifest=child,
                production=production, conductor=conductor, refs=refs, trims=trims,
                ceiling_s=ceiling_s, ceiling_db_spl=ceiling_db_spl, camilla_factory=camilla_factory,
                verify_only=False, provenance=provenance, level=plan.level,
                capture_indexes=tuple(captures.index(capture) + 1 for capture in selected),
            )
            bound = LevelRun(child, child_door, child_analyze, child_assessor, selected)
            return bound

        try:
            results = await run_levels(ladder, hold=door.hold, prepare=prepare, gate=gate, signals=signals, aborts={})
            if signals.stop.is_set() or signals.complete.is_set():
                manifest.reason = signals.stop_reason if signals.stop.is_set() else "complete_requested"
            return results[-1] if results and not manifest.reason else manifest
        except BaseException as exc:  # noqa: BLE001 - preserve the partial packet before host failure publication
            manifest.reason = getattr(exc, "code", None) or REASON_INTERNAL_ERROR
            raise
        finally:
            door.opened = bound.door.opened if bound else None
            await packet.finish()
            summary = packet.to_dict()
            gate.publish({key: summary[key] for key in ("status", "reason", "level", "runs", "honoured")} |
                         {"manifest": manifest.path})

    return door, analyze, assessor, execute
