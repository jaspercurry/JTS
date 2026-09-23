# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Provenance of the analysis and playback represented by one take."""

from __future__ import annotations

from typing import Any, Mapping

from jasper.active_speaker.profile import SIDES_BY_LAYOUT
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.program import ExcitationProgram, KIND_SWEEP, KIND_SUMMED_SWEEP
from jasper.audio_measurement.program_analysis import analysis_diagnostic_summary
from jasper.json_fields import finite_float
from .measure_spec import CANDIDATE_SCOPES


def _finite(value: Any) -> Any:
    """``value`` with every non-finite float nulled and every key kept.

    The evidence store refuses a non-finite number, so one unmeasurable
    diagnostic would cost the whole take; a dropped key would erase the
    summary's deliberate difference between ``None`` and absent.
    """
    if isinstance(value, float):
        return finite_float(value)
    if isinstance(value, Mapping):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def analysis_blocks(analysis: Any) -> dict[str, Any]:
    """What one analysis leaves on its banked take, beside its provenance.

    The evidence packet's ``capture_snr`` block publishes the SNR columns of
    ``diagnostic``, and the distortion view gates its replay against it.
    """
    ledger = getattr(analysis, "frame_ledger", None)
    branch = getattr(analysis, "branch_diagnostic", None)
    return {
        "diagnostic": _finite(analysis_diagnostic_summary(analysis)),
        **({"frame_ledger": ledger.to_dict()} if ledger is not None else {}),
        **({"branch_diagnostic": branch} if branch else {}),
    }


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
    stimulus = provenance.get("stimulus")
    candidate = graph.get("speaker_candidate_id") or (
        record.get("candidate_id") if record.get("graph_scope") in CANDIDATE_SCOPES else None
    )
    return {
        **record,
        "side": side if side in sides else sides[0] if len(sides) == 1 else None,
        **({"provenance": {**provenance, "graph": {**graph, "speaker_candidate_id": candidate}}} if graph else {}),
        # The top-level sibling of ``wav_sha256`` the packet's per-capture rows read.
        **({"stimulus_wav_sha256": stimulus.get("wav_sha256")} if stimulus else {}),
    }
