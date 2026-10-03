# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind banked captures to the program analyzer and legacy preparation effects."""
from __future__ import annotations

from jasper.web import correction_crossover_v2_volume as v2volume

from dataclasses import replace
from functools import partial
import asyncio
import logging
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.programs import program_for_spec, predictive_program_for_spec
from jasper.active_speaker.crossover_v2.door import isolation_hold
from jasper.active_speaker.crossover_v2.capture_provenance import (
    analysis_blocks, enrich_capture_record, take_distance_m, take_trusted_bands,
)
from jasper.active_speaker.crossover_v2.session import TuningSession
from jasper.active_speaker.crossover_v2.summed_alignment import timing_prior
from jasper.active_speaker.crossover_v2.take_impulses import IMPULSES_KEY, write_take_impulses
from jasper.active_speaker.crossover_v2.wired_stimulus import CapturedRecordStore
from jasper.active_speaker.measurement_programs import SPOT_LEVEL, gate_exemption
from jasper.active_speaker.plan_run import RunDoor, after_grading
from jasper.audio_measurement.bundles import BundleError
from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from jasper.audio_measurement.measurement_geometry import load_declared_geometry
from jasper.audio_measurement.program_analysis.model import SWEEP_PEAK_TO_RMS_DB
from jasper.platform.log_event import log_event

from jasper.active_speaker.crossover_v2.capture_dispatch import assess
from jasper.active_speaker.crossover_v2.journey import PHASE_CHECK, PHASE_MEASURE, PHASE_TIMING
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_INTERNAL_ERROR, TakeVerdict, PhaseVerdict
from jasper.audio_measurement.program import ExcitationProgram

logger = logging.getLogger(__name__)


def _kept_impulses(records: Any, take_id: str, analysis: Any, answer: Any) -> dict[str, Any] | None:
    """The take record's impulses block once they are written beside its
    recording, or ``None`` when none were kept (a CHECK take).

    A failed write costs only the saved copy: the raw recording stays, so the
    impulses remain recomputable.
    """
    bundle_dir = getattr(getattr(records, "capture", None), "bundle_dir", None)
    if bundle_dir is None:
        return None
    try:
        return write_take_impulses(Path(bundle_dir), take_id, analysis,
                                   recording=getattr(answer, "wav_path", None) or None)
    except (OSError, BundleError) as exc:
        log_event(logger, "correction.take_impulses_not_saved", level=logging.WARNING,
                  take_id=take_id, error_type=type(exc).__name__)
        return None


def bind_plan_analysis(conductor: Any, records: Any, *, manifest: Any, evidence: Any,
                       provenance: Any = None,
                       check_target_capture_dbfs: float | None = None, context: Any = None) -> tuple[Any, Any]:
    answers: dict[str, tuple[Any, Any]] = {}
    roles = tuple(band.role for band in conductor.roles_bands)
    diameters = context.radiating_diameter_mm_by_target if context is not None else {}
    phase, index = "", 0
    answer: Any = None

    def index_of(record: Any) -> int:
        return record.get("capture_index", record["index"])

    def declared_room(record: Any) -> tuple[float | None, dict[str, dict[str, Any]] | None]:
        """The take's first bounce in the declared room, which bounds its gate
        (#3665 item 10), and the band each window banks (ADR-0366 §3), from one read."""
        kind, distance_m = record.get("pose_kind"), record.get("mark_distance_m")
        try:
            room = load_declared_geometry()
            bands = take_trusted_bands(kind=kind, distance_m=distance_m, driver=record.get("pose_driver") or "",
                                       roles=roles, diameters_mm_by_target=diameters, room=room)
            return None if room is None else room.first_bounce_s(take_distance_m(kind, distance_m)), bands
        except (OSError, ValueError) as exc:
            # The take gates to the default bound and its curves bank no band; their readers refuse by name.
            log_event(logger, "correction.take_band_not_banked", level=logging.WARNING,
                      take_id=record["take_id"], error_type=type(exc).__name__)
            return None, None

    def enrich(capture: Any, record: Any) -> dict[str, Any]:
        captured = provenance.take() if provenance is not None else None
        record = manifest.capture_record(record)
        first_bounce_s, bands = declared_room(record)
        program = getattr(capture, "program", None) or record.get("program")
        fields: dict[str, Any] = {}
        result: Any = KeyError("program")
        if program is not None:
            try:
                phase = conductor.phase_of_index(index_of(record))
                played = ExcitationProgram.from_dict(program)
                result = analyze_capture(record, played, capture, first_bounce_s)
                fields = {**evidence.get("capture_provenance", {}).get(phase, {}),
                          **analysis_blocks(result, played, bands)}
            except Exception as exc:  # noqa: BLE001 - bank raw evidence before the executor propagates failure
                result = exc
        if isinstance(result, Exception):
            fields = {"analysis_error": {"code": REASON_INTERNAL_ERROR, "error_type": type(result).__name__}}
        else:
            fields = {**fields, IMPULSES_KEY: _kept_impulses(records, record["take_id"], result, capture)}
        answers[record["take_id"]] = capture, result
        return enrich_capture_record({
            **record, **fields, "mark_distance_m": record.get("mark_distance_m"),
            **({"provenance": captured.to_dict()} if captured is not None else {}),
        }, layout=conductor.source_preset.channel_map.layout)

    def after_bank(record: Any, _record_id: str) -> None:
        _, analysis = answers.pop(record["take_id"])
        if (record.get("phase") == PHASE_TIMING and not isinstance(analysis, Exception)
                and conductor.timing_prior is None):
            conductor.set_timing_prior(timing_prior(record, analysis))

    records.enrich, records.after_bank = enrich, after_bank

    def analyze_capture(record: Any, program: ExcitationProgram, capture: Any, first_bounce_s: float | None) -> Any:
        index = index_of(record)
        phase = conductor.phase_of_index(index)
        priors = (conductor.check_priors() if phase == PHASE_CHECK else
                  conductor.measure_priors() if phase == PHASE_MEASURE else
                  conductor.lateral_priors())
        if phase == PHASE_CHECK and check_target_capture_dbfs is not None:
            priors = replace(priors, target_capture_dbfs=check_target_capture_dbfs)
        # See ADR-0400.
        kind = record.get("pose_kind")
        exemption = gate_exemption(kind, driver=record.get("pose_driver") or "",
                                   distance_m=take_distance_m(kind, record.get("mark_distance_m")))
        analysis = conductor.analyze(
            program, capture, priors,
            replace(conductor.capture_geometry(phase, index), declared_first_bounce_s=first_bounce_s,
                    gate_exempt_reason=exemption), phase=phase,
        )
        calibration = evidence.get("calibration", {}).get(phase, {})
        manifest.calibration = {"id": calibration.get("calibration_id"),
                                "curve_fingerprint": calibration.get("curve_fingerprint")}
        return analysis

    def analyze(record: Any) -> Any:
        nonlocal phase, answer, index
        answer, analysis = answers[record["take_id"]]
        index = index_of(record)
        phase = conductor.phase_of_index(index)
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
            verdict = conductor.check_verdict(analysis)
        prior = None if verdict is None else TakeVerdict(verdict.accepted, fault=verdict.code, evidence=verdict.evidence,
                           capabilities=verdict.capabilities, next=verdict.next or (
                               "accept" if verdict.accepted else "fix_and_retake"),
                           next_gain_db=verdict.next_gain_db, charge=verdict.charge)
        if kwargs.get("phase") == PHASE_MEASURE:
            kwargs.update(gain_ceiling_db=conductor.measure_gain_ceiling_db, caps_dbfs=conductor.caps_dbfs,
                          session_volume_db=conductor.excitation.session_volume_db,
                          spl_stop_db_spl=conductor.spl_stop_db_spl,
                          spl=(getattr(answer, "capture_integrity", None) or {}).get("spl"),
                          raise_rides_next=conductor.measures_again(index))
        assessed = assess(analysis, prior_verdict=prior, **kwargs)
        # A raise, from a kept take or a retake, moves the session's gain plan that every later MEASURE take plays (ADR-0433).
        if verdict is None and phase == PHASE_MEASURE and any(key.startswith("next_gain_db.") for key in assessed.evidence):
            after_grading(partial(conductor.rearm_measure_after_transient, assessed))
        return assessed

    return analyze, assessor


def compose_plan_program(conductor: Any, spec: Any, stimulus_dbfs: float | None) -> Any:
    gains = conductor.gain_plan_db if spec.graph_scope == "drivers" and spec.program_phase != PHASE_CHECK else None
    program = program_for_spec(spec, conductor.excitation, gains, stimulus_dbfs)
    conductor.set_program(spec.program_phase, program)
    return program


def bind_run_door(*, host: Any, device: Any, evidence_store: Any,
                  manifest: Any, production: Any, conductor: Any, refs: Any,
                  ceiling_s: float, ceiling_db_spl: float | None,
                  camilla_factory: Any, provenance: Any = None, context: Any = None) -> tuple[RunDoor, Any, Any]:
    sensitivity = resolved_household_sensitivity(device)
    check_target = None
    if sensitivity is not None:
        # CHECK aims at the first spot's target at the microphone (ADR-0403 §4). That
        # target is a located sweep's RMS level; CHECK's solve compares peaks.
        check_target = float(sensitivity.dbfs_from_db_spl(SPOT_LEVEL.target_db_spl)) + SWEEP_PEAK_TO_RMS_DB
        log_event(logger, "correction.check_level_target", target_db_spl=SPOT_LEVEL.target_db_spl,
                  target_capture_dbfs=check_target)
    records = CapturedRecordStore(manifest, None)
    analyze, assessor = bind_plan_analysis(conductor, records, manifest=manifest,
                                          evidence=refs, provenance=provenance,
                                          check_target_capture_dbfs=check_target, context=context)

    def build(door: Any, allocate_take_id: Any) -> TuningSession:
        capture = host._wired_stimulus_capture(device, evidence_store, spl_monitor=door.spl_monitor)
        records.capture = capture
        conductor.set_excitation(replace(conductor.excitation, session_volume_db=door.measurement_volume_db))
        return TuningSession(
            manifest.run_id, host.bind_v2_engine_seams(
                session_graph=door.graph, compose_stimulus=production.compose,
                capture_stimulus=capture, records=records, volume_claim=door.claim,
            ), door.measurement_volume_db, allocate_take_id,
        )

    door = RunDoor(
        isolation_hold(graph=production.graph, camilla_factory=camilla_factory,
                       action="measuring", plan=v2volume.session_volume_plan(), wall_clock_ceiling_s=ceiling_s),
        build, sensitivity, device, ceiling_db_spl,
        program_for_spec=predictive_program_for_spec(context) if context else None,
    )
    door.caps_dbfs = conductor.caps_dbfs
    return door, analyze, assessor


async def publish_round_packet(bundle: Path, gate: Any) -> None:
    from jasper.active_speaker.round_bank import finish_round  # lazy: packet analysis

    progress = gate.published().get("run") or {}
    banked, _ = await asyncio.to_thread(finish_round, bundle)
    gate.publish({**progress, **({"round_dir": str(banked.path)} if banked else {"packet_error": "packet_save_failed"})})
