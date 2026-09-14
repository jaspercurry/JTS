# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Provenance of the analysis and playback represented by one take."""

from __future__ import annotations

from typing import Any, Mapping

from jasper.active_speaker.profile import SIDES_BY_LAYOUT
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.program import ExcitationProgram, KIND_SWEEP, KIND_SUMMED_SWEEP


def analysis_provenance(
    program: ExcitationProgram, analysis: Any, calibration: Any, curve: Any, geometry: Any,
) -> dict[str, Any]:
    summed = getattr(analysis, "summed_response", None)
    responses = (summed,) if summed is not None else getattr(analysis, "driver_responses", ())
    gates = {(response.gating or {}).get("applied") for response in responses}
    stimuli = [segment for segment in program.segments if segment.kind in {KIND_SWEEP, KIND_SUMMED_SWEEP}]
    return {
        "capture_calibration": {
            "applied": curve is not None,
            "calibration_id": getattr(calibration, "calibration_id", None),
            "curve_fingerprint": json_fingerprint(curve.to_dict()) if curve is not None else None,
        },
        "gating_applied": next(iter(gates)) if len(gates) == 1 else None,
        "stimulus_dbfs": max((float(segment.gain_db) for segment in stimuli or program.stimulus_segments()), default=None),
        "mark_distance_m": float(geometry.mic_distance_m) if geometry is not None else None,
    }


def enrich_capture_record(record: Mapping[str, Any], *, layout: str | None) -> dict[str, Any]:
    sides = SIDES_BY_LAYOUT.get(layout or "", ())
    side = record.get("side")
    provenance = record.get("provenance") or {}
    graph = provenance.get("graph") or {}
    candidate = graph.get("speaker_candidate_id") or (
        record.get("candidate_id") if record.get("graph_scope") in {"candidate", "candidate_branches"} else None
    )
    return {
        **record,
        "side": side if side in sides else sides[0] if len(sides) == 1 else None,
        **({"provenance": {**provenance, "graph": {**graph, "speaker_candidate_id": candidate}}} if graph else {}),
    }
