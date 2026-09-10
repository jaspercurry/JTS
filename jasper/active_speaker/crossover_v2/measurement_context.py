# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Capture context and comparison disclosures shared by Room and bass views."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..measurement_programs import POSE_KIND_BEARING
from .record_index import played_graph_fingerprint

def capture_basis(record: Mapping[str, Any]) -> dict[str, Any]:
    provenance = record.get("provenance") or {}
    setup = record.get("capture_setup") or {}
    device = record.get("capture_device") or {}
    calibration = record.get("capture_calibration") or {}
    stimulus = provenance.get("stimulus") or {}
    return {
        "candidate_id": record.get("candidate_id") or None,
        "speaker_candidate_id": (provenance.get("graph") or {}).get("speaker_candidate_id"),
        "submitted_graph_fingerprint": record.get("graph_fingerprint") or None,
        "graph_fingerprint": played_graph_fingerprint(record) or None,
        "played_graph_recorded": bool((provenance.get("graph") or {}).get("fingerprint")),
        "graph_scope": record.get("graph_scope") or None,
        "side": record.get("side"),
        "pose_kind": record.get("pose_kind") or POSE_KIND_BEARING,
        "calibration_reference": setup.get("calibration"),
        "calibration_applied": calibration.get("applied"),
        "capture_calibration": calibration or None,
        "capture_device": {k: device.get(k) for k in ("card", "usb_id", "model_key", "pcm", "channel_selected")} if device else None,
        "level_db": provenance.get("session_volume_db") if provenance.get("session_volume_db") is not None else record.get("level_db"),
        "loudness_volume_db": record.get("loudness_volume_db"),
        "stimulus_dbfs": record.get("stimulus_dbfs"),
        "stimulus_wav_sha256": stimulus.get("wav_sha256"),
        "stimulus_peak_dbfs": stimulus.get("peak_dbfs"),
        "gating_applied": record.get("gating_applied"),
    }


GRAPH_FIELDS = (
    "candidate_id", "submitted_graph_fingerprint", "graph_fingerprint", "graph_scope",
)
CAPTURE_FIELDS = (
    "side", "capture_device", "level_db", "stimulus_dbfs", "stimulus_wav_sha256",
    "stimulus_peak_dbfs", "loudness_volume_db", "gating_applied",
)


def _capture_calibration_identity(value: Any) -> tuple[bool, str | None, str | None] | None:
    if not isinstance(value, Mapping) or type(value.get("applied")) is not bool:
        return None
    calibration_id = value.get("calibration_id")
    fingerprint = value.get("curve_fingerprint")
    if calibration_id is not None and not isinstance(calibration_id, str):
        return None
    if value["applied"] and not isinstance(fingerprint, str):
        return None
    if not value["applied"] and fingerprint is not None:
        return None
    return value["applied"], calibration_id, fingerprint


def compare_capture_basis(
    now: Mapping[str, Any], was: Mapping[str, Any], *,
    interventions: Sequence[str] = GRAPH_FIELDS,
    required: Sequence[str] = CAPTURE_FIELDS,
) -> dict[str, Any]:
    changed = [
        field for field in interventions
        if field in now and field in was and now.get(field) != was.get(field)
    ]
    incompatible: list[str] = []
    unknown: list[str] = []
    for field in required:
        left, right = now.get(field), was.get(field)
        if left is None or right is None:
            unknown.append(field)
        elif left != right:
            incompatible.append(field)

    # New captures carry this per-take resolution. Old medians have only the
    # reference/applied pair; those remain usable, with their missing fact named.
    left_calibration = _capture_calibration_identity(now.get("capture_calibration"))
    right_calibration = _capture_calibration_identity(was.get("capture_calibration"))
    if left_calibration is not None and right_calibration is not None:
        if left_calibration != right_calibration:
            incompatible.append("capture_calibration")
    else:
        unknown.append("capture_calibration")
        for field in ("calibration_reference", "calibration_applied"):
            left, right = now.get(field), was.get(field)
            if left is None or right is None:
                unknown.append(field)
            elif left != right:
                incompatible.append(field)

    return {
        "basis_status": (
            "incompatible" if incompatible else "unknown" if unknown else "compatible"
        ),
        "intervention_fields": sorted(set(changed)),
        "incompatible_fields": sorted(set(incompatible)),
        "unknown_fields": sorted(set(unknown)),
    }
