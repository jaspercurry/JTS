# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The analysis each summed take banked on its record (ADR-0383), read without its recording."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable
from jasper.audio_measurement.program import ExcitationProgram, PROGRAM_PHASE_VERIFY
from jasper.platform.json_fields import finite_float

from .crossover_v2.record_index import (
    MeasurementCaptureIdentityError, bundle_measurements, record_path, reopen_measurement_record,
)
from .frequency_view import FrequencyRun
from .measurement_document import frequency_run_from_documents


@dataclass(frozen=True)
class BankedMeasurement:
    """A take whose record banked its analysed curves (ADR-0383), read without its recording."""

    record: dict[str, Any]
    record_path: str

    def document(self) -> dict[str, Any]:
        calibration = self.record.get("capture_calibration") or {}
        return {**self.record, "calibration": {"applied": bool(calibration.get("applied")),
                                               "calibration_id": calibration.get("calibration_id")}}


def _reopened(bundle_dir: Path, paths: Iterable[str] | None) -> Iterator[tuple[str, dict[str, Any]]]:
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
        yield path, record


def analyzed_measurements(
    bundle_dir: Path, *, paths: Iterable[str] | None = None, refuse_failed: bool = False,
) -> Iterator[BankedMeasurement]:
    """Each take's banked analysis (ADR-0383), with its recording's identity
    checked and its bytes never read. A take whose analysis failed has none: it
    is passed over, or with ``refuse_failed`` refuses by name."""
    for path, record in _reopened(bundle_dir, paths):
        if "analysis_error" in record:
            if refuse_failed:
                raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {
                    "record": path, "take_id": record.get("take_id"), "analysis_error": record["analysis_error"]})
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
