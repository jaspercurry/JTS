# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The analysis each summed take banked on its record (ADR-0383), read without its recording."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable
from jasper.audio_measurement.program import ExcitationProgram, PROGRAM_PHASE_VERIFY

from .crossover_v2.record_index import (
    MeasurementCaptureIdentityError, bundle_measurements, record_path, reopen_measurement_record,
)


def banked_document(record: Mapping[str, Any]) -> dict[str, Any]:
    """A take record stating the calibration its capture applied, as the frequency view reads it."""
    calibration = record.get("capture_calibration") or {}
    return {**record, "calibration": {"applied": bool(calibration.get("applied")),
                                      "calibration_id": calibration.get("calibration_id")}}


@dataclass(frozen=True)
class BankedMeasurement:
    """A take whose record banked its analysed curves (ADR-0383), read without its recording."""

    record: dict[str, Any]
    record_path: str

    def document(self) -> dict[str, Any]:
        return banked_document(self.record)


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
