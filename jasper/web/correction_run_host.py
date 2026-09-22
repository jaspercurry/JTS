# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind banked captures to the program analyzer and legacy preparation effects."""
from __future__ import annotations

from jasper.active_speaker.program_failure import classify_program_failure

from jasper.web import correction_crossover_v2_volume as v2volume

from dataclasses import replace
import asyncio
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.programs import program_for_spec, predictive_program_for_spec
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
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_INTERNAL_ERROR, TakeVerdict, PhaseVerdict, exception_detail
from jasper.active_speaker.seat_level_reference import check_target_capture_dbfs as anchored_check_target
from jasper.audio_measurement.program import ExcitationProgram


def bind_plan_analysis(conductor: Any, records: Any, *, manifest: Any, evidence: Any,
                       provenance: Any = None,
                       check_target_capture_dbfs: float | None = None,
                       capture_indexes: tuple[int, ...] = ()) -> tuple[Any, Any]:
    answers: dict[str, tuple[Any, Any]] = {}
    index = 0
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
        elif getattr(result, "branch_diagnostic", None):
            # The one moment a round can retain it: the take record is banked
            # write-once after this, so nothing downstream can put it back, and
            # without it `round_captures._capture_response` refuses every
            # non-``summed`` role. ``regime`` is NOT set alongside as the web
            # flow's `_retain_lateral_pose` does: a round's record already
            # carries its plan row's own ``regime``, and `RunManifest.append`
            # fingerprints that word into each capture set as ``stimulus``.
            fields = {**fields, "branch_diagnostic": result.branch_diagnostic}
        answers[record["take_id"]] = capture, result
        return enrich_capture_record({
            **record, **fields, "mark_distance_m": record.get("mark_distance_m"),
            "phase": record.get("program_phase"),
            **({"provenance": captured.to_dict()} if captured is not None else {}),
        }, layout=conductor._preset.channel_map.layout)

    def after_bank(record: Any, record_id: str) -> None:
        answers[record_id] = answers.pop(record["take_id"])
        _, analysis = answers[record_id]
        if (record.get("phase") == PHASE_ENTRY_BASELINE and not isinstance(analysis, Exception)
                and conductor._measure_entry_baseline is None):
            conductor._measure_entry_baseline = banked_entry_baseline(record, analysis)

    records.enrich, records.after_bank = enrich, after_bank

    def analyze_capture(record: Any, capture: Any) -> Any:
        index = index_of(record)
        phase = conductor._phase_of_index(index)
        priors = (conductor._check_priors() if phase == PHASE_CHECK else
                  conductor._measure_priors() if phase == PHASE_MEASURE else
                  conductor._lateral_priors())
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
        nonlocal index, phase, answer
        answer, analysis = answers.pop(record_id)
        index = index_of(record)
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
    gains = conductor._gain_plan_db if spec.graph_scope == "drivers" and spec.program_phase != PHASE_CHECK else None
    program = program_for_spec(spec, conductor._excitation, gains, stimulus_dbfs,
                               safety_profile=context.safety_profile, role_targets=context.role_targets)
    if spec.program_phase == PHASE_CHECK:
        conductor._check_program = program
    elif spec.program_phase == PHASE_VERIFY:
        conductor._verify_program = program
    elif spec.program_phase == PHASE_CLOUD_VERIFY:
        conductor._cloud_program = program
    return program


def bind_run_door(*, host: Any, device: Any, evidence_store: Any,
                  manifest: Any, production: Any, conductor: Any, refs: Any,
                  trims: Any, ceiling_s: float, ceiling_db_spl: float | None,
                  camilla_factory: Any, provenance: Any = None,
                  level: LevelPolicy = LevelPolicy(), ladder: LevelLadder | None = None,
                  capture_indexes: tuple[int, ...] = (), context: Any = None) -> tuple[RunDoor, Any, Any, Any]:
    if ladder is not None:
        ceiling_s *= len(ladder.admissible)
    sensitivity = resolved_household_sensitivity(device)
    predicted = level.predicted_db_spl
    check_target = (anchored_check_target(sensitivity, predicted)
                    if predicted is not None and sensitivity is not None else None)
    records = CapturedRecordStore(manifest, None)
    analyze, assessor = bind_plan_analysis(conductor, records, manifest=manifest,
                                          evidence=refs, provenance=provenance,
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
        program_for_spec=predictive_program_for_spec(context) if context else None,
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
                provenance=provenance, level=plan.level, context=context,
                capture_indexes=tuple(captures.index(capture) + 1 for capture in selected),
            )
            bound = LevelRun(child, child_door, child_analyze, child_assessor, selected)
            return bound

        try:
            results = await run_levels(ladder, hold=door.hold, prepare=prepare, gate=gate, signals=signals,
                                       aborts={}, save_ladder=packet.update_schedule)
            if signals.stop.is_set() or signals.complete.is_set():
                manifest.reason = signals.stop_reason if signals.stop.is_set() else "complete_requested"
            return replace(results[-1], reason=packet.to_dict()["reason"]) if results and not manifest.reason else manifest
        except BaseException as exc:  # noqa: BLE001 - preserve the partial packet before host failure publication
            classified = classify_program_failure(exc)
            manifest.reason = (classified[0] if classified else getattr(exc, "code", None)) or REASON_INTERNAL_ERROR
            manifest.detail = exception_detail(exc)
            raise
        finally:
            door.opened = bound.door.opened if bound else None
            await packet.finish()
            summary = packet.to_dict()
            gate.publish({key: summary[key] for key in ("status", "reason", "level", "runs", "honoured")} |
                         {"manifest": manifest.path})

    return door, analyze, assessor, execute


async def publish_round_packet(bundle: Path, gate: Any) -> None:
    from jasper.active_speaker.round_bank import finish_round  # lazy: packet analysis

    progress = gate.published().get("run") or {}
    banked, _ = await asyncio.to_thread(finish_round, bundle)
    gate.publish({**progress, **({"round_dir": str(banked.path)} if banked else {"packet_error": "packet_save_failed"})})
