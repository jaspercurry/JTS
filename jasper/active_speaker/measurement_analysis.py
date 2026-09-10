# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Explicit offline analysis of immutable summed Room and bass captures."""

from __future__ import annotations

import json
from pathlib import Path

from jasper.audio_measurement.gating import SEAT_EXEMPT
from jasper.audio_measurement.household_mic import resolve_setup_calibration
from jasper.audio_measurement.program import ExcitationProgram, PROGRAM_PHASE_VERIFY
from jasper.audio_measurement.program_analysis import (
    MeasurementGeometry, analysis_diagnostic_summary, analyze_program_capture,
)
from jasper.audio_measurement.wired_capture import decode_wav_to_mono
from jasper.json_fields import finite_float

from .commissioning_evidence_store import EVIDENCE_ROOT
from .crossover_v2.record_index import (
    MeasurementCaptureIdentityError, bundle_measurements, reopen_measurement_capture,
)
from .crossover_v2.spatial import analysis_curve_records
from .frequency_view import FrequencyRun
from .measurement_document import frequency_run_from_documents


class MeasurementAnalysisRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def analyze_measurement_bundle(
    bundle_dir: Path, *, calibration_root: Path | None = None,
    run_reference_db: float | None = None,
) -> FrequencyRun:
    """Read exact captured programs and WAVs; never rewrite banked evidence."""
    if run_reference_db is not None and finite_float(run_reference_db) is None:
        raise MeasurementAnalysisRefused("measurement_reference_invalid")
    info = json.loads((bundle_dir / "info.json").read_text())

    documents = []
    for row in bundle_measurements(bundle_dir):
        try:
            record, wav = reopen_measurement_capture(bundle_dir, f"{EVIDENCE_ROOT}/artifacts/{row.path}")
        except MeasurementCaptureIdentityError as exc:
            raise MeasurementAnalysisRefused("measurement_capture_identity_mismatch") from exc
        if wav is None:
            continue
        if not record.get("program"):
            raise MeasurementAnalysisRefused("measurement_program_manifest_missing")
        program = ExcitationProgram.from_dict(record["program"])
        if program.phase != PROGRAM_PHASE_VERIFY or program.channels != 1 or record.get("graph_scope") not in {
            "speaker_tune", "room_tune", "room_candidate", "bass_candidate", "applied",
        }:
            raise MeasurementAnalysisRefused("measurement_analysis_program_unsupported")
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
        documents.append({
            **record, "curves": analysis_curve_records(analysis, program),
            "diagnostic": analysis_diagnostic_summary(analysis),
            "calibration": {"applied": calibration is not None,
                            "calibration_id": calibration.calibration_id if calibration is not None else None},
        })
    if not documents:
        raise MeasurementAnalysisRefused("measurement_captures_missing")
    return frequency_run_from_documents(
        run_id=info["session_id"], documents=documents,
        started_at=info.get("started_at"), state=info.get("state"),
        run_reference_db=run_reference_db,
    )
