# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The analysis of summed takes: banked on the take, or decoded from its recording for the bass view."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.audio_measurement.calibration import CalibrationRecord
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable
from jasper.audio_measurement.gating import SEAT_EXEMPT
from jasper.audio_measurement.household_mic import resolve_setup_calibration
from jasper.audio_measurement.program import ExcitationProgram, PROGRAM_PHASE_VERIFY
from jasper.audio_measurement.program_analysis import (
    MeasurementGeometry, ProgramAnalysis, analysis_diagnostic_summary, analyze_program_capture,
)
from jasper.audio_measurement.wired_capture import decode_wav_to_mono
from jasper.audio_measurement.repeated_sweep import average_summed_capture
from jasper.json_fields import finite_float

from .crossover_v2.record_index import (
    MeasurementCaptureIdentityError, bundle_measurements, record_path, reopen_measurement_record,
)
from .crossover_v2.spatial import analysis_curve_records
from .frequency_view import FrequencyRun
from .measurement_document import frequency_run_from_documents


@dataclass(frozen=True)
class AnalyzedMeasurement:
    record: dict[str, Any]
    record_path: str
    program: ExcitationProgram
    samples: np.ndarray
    sample_rate: int
    calibration: CalibrationRecord | None
    analysis: ProgramAnalysis

    def document(self) -> dict[str, Any]:
        summed = self.analysis.summed_response
        analyzed_gating = bool((summed.gating or {}).get("applied")) if summed is not None else None
        banked_gating = self.record.get("gating_applied")
        return {
            **self.record, "curves": analysis_curve_records(self.analysis, self.program),
            "gating_applied": analyzed_gating if banked_gating is None else banked_gating,
            "diagnostic": analysis_diagnostic_summary(self.analysis),
            "calibration": {"applied": self.calibration is not None,
                            "calibration_id": self.calibration.calibration_id if self.calibration else None},
        }


@dataclass(frozen=True)
class BankedMeasurement:
    """A take whose record banked its analysed curves (ADR-0383), read without its recording."""

    record: dict[str, Any]
    record_path: str

    def document(self) -> dict[str, Any]:
        calibration = self.record.get("capture_calibration") or {}
        return {**self.record, "calibration": {"applied": bool(calibration.get("applied")),
                                               "calibration_id": calibration.get("calibration_id")}}


def _reopened(
    bundle_dir: Path, paths: Iterable[str] | None,
) -> Iterator[tuple[str, dict[str, Any], ExcitationProgram, Callable[[], bytes]]]:
    for path in paths if paths is not None else map(record_path, bundle_measurements(bundle_dir)):
        try:
            record, capture = reopen_measurement_record(bundle_dir, path)
        except MeasurementCaptureIdentityError as exc:
            raise EvidenceUnavailable("measurement_capture_identity_mismatch", {"record": path, "detail": str(exc)}) from exc
        if capture is None:
            continue
        if not record.get("program"):
            raise EvidenceUnavailable("measurement_program_manifest_missing", {"record": path})
        program = ExcitationProgram.from_dict(record["program"])
        if program.phase != PROGRAM_PHASE_VERIFY or program.channels != 1 or (record.get("graph_scope") != "candidate" or not record.get("candidate_id")):
            raise EvidenceUnavailable("measurement_analysis_program_unsupported", {"record": path})
        yield path, record, program, capture


def _decoded(
    path: str, record: dict[str, Any], program: ExcitationProgram, wav: bytes, calibration_root: Path | None,
) -> AnalyzedMeasurement:
    samples, rate = decode_wav_to_mono(wav)
    calibration = resolve_setup_calibration(
        record.get("capture_setup"), device=record.get("capture_device"),
        root=calibration_root,
    )
    analysis = analyze_program_capture(
        program, samples, rate,
        calibration=calibration.curve if calibration is not None else None,
        geometry=MeasurementGeometry(gate_exempt_reason=SEAT_EXEMPT),
        capture_report=record.get("capture_integrity"),
    )
    offset = analysis.locations[0].scheduled_start - program.segments[0].start_sample
    return AnalyzedMeasurement(record, path, program, average_summed_capture(program, samples, offset,
                               analysis.capture_integrity.pass_alignment if analysis.capture_integrity else None),
                               rate, calibration, analysis)


def decoded_measurements(
    bundle_dir: Path, *, calibration_root: Path | None = None, paths: Iterable[str] | None = None,
) -> Iterator[AnalyzedMeasurement]:
    """Reopen exact takes once and decode each recording, releasing it before loading the next."""
    for path, record, program, capture in _reopened(bundle_dir, paths):
        yield _decoded(path, record, program, capture(), calibration_root)


def analyzed_measurements(bundle_dir: Path, *, paths: Iterable[str] | None = None) -> Iterator[BankedMeasurement]:
    """Each take's banked analysis (ADR-0383). A take whose analysis failed has
    none and is passed over."""
    for path, record, _program, _capture in _reopened(bundle_dir, paths):
        if "analysis_error" in record:
            continue
        if "curves" not in record:
            raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {"record": path})
        yield BankedMeasurement(record, path)


def analyze_measurement_bundle(bundle_dir: Path, *, run_reference_db: float | None = None) -> FrequencyRun:
    if run_reference_db is not None and finite_float(run_reference_db) is None:
        raise ValueError(f"measurement_reference_invalid: {run_reference_db}")
    info = json.loads((bundle_dir / "info.json").read_text())
    documents = [take.document() for take in analyzed_measurements(bundle_dir)]
    if not documents:
        raise EvidenceUnavailable("measurement_captures_missing", {"bundle_dir": str(bundle_dir)})
    return frequency_run_from_documents(
        run_id=info["session_id"], documents=documents,
        started_at=info.get("started_at"), state=info.get("state"),
        run_reference_db=run_reference_db,
    )
