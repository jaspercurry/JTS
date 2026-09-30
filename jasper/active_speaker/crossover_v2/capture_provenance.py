# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Provenance of the analysis and playback represented by one take."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping, Sequence

from jasper.active_speaker.profile import SIDES_BY_LAYOUT
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.audio_measurement.program import ExcitationProgram, KIND_SWEEP, KIND_SUMMED_SWEEP
from jasper.audio_measurement.program_analysis import analysis_diagnostic_summary
from jasper.audio_measurement.trusted_band import trusted_band
from jasper.platform.json_fields import finite_float
from ..measurement_programs import POSE_KIND_SEAT
from .measure_spec import CANDIDATE_SCOPES
from .planning import analysis_json
from .pose_curve import WINDOW_GATED, WINDOW_UNGATED
from .spatial import MARK_DISTANCE_M, analysis_curve_records


def finite_json(value: Any) -> Any:
    """``value`` with every non-finite float nulled and every key kept.

    The evidence store refuses a non-finite number, so one unmeasurable
    diagnostic would cost the whole take; a dropped key would erase the
    summary's deliberate difference between ``None`` and absent.
    """
    if isinstance(value, float):
        return finite_float(value)
    if isinstance(value, Mapping):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(item) for item in value]
    return value


def analysis_blocks(
    analysis: Any, program: ExcitationProgram, bands: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """What one analysis leaves on its banked take, beside its provenance: its
    ``curves`` and ``analysis``, which a round's readers read from the take
    itself (ADR-0395). Each curve carries the trusted band of its window from
    ``bands`` (:func:`take_trusted_bands`); none when the take has none. The
    evidence packet's ``capture_snr`` block publishes the SNR columns of
    ``diagnostic``.
    """
    curves = analysis_curve_records(analysis, program)
    return finite_json({
        "curves": [{**curve, "trusted_band": bands[curve["window"]]} for curve in curves] if bands else curves,
        "analysis": {**analysis_json(analysis), "bass": getattr(analysis, "bass", None),
                     "distortion": getattr(analysis, "distortion", None)},
        "diagnostic": analysis_diagnostic_summary(analysis),
        "branch_diagnostic": getattr(analysis, "branch_diagnostic", None) or None,
    })


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


def take_distance_m(kind: str | None, distance_m: float | None) -> float | None:
    """How far a take's microphone sits from its pose's reference: a seat
    states no distance; any other pose that states none sits at the mark."""
    return None if kind == POSE_KIND_SEAT else MARK_DISTANCE_M if distance_m is None else float(distance_m)


def take_trusted_bands(
    *, kind: str | None, distance_m: float | None, driver: str,
    roles: Sequence[str], diameters_mm_by_target: Mapping[str, float], room: DeclaredGeometry | None,
) -> dict[str, dict[str, Any]]:
    """The band each window of a take trusts, from its pose, the drivers that
    played (its ``driver`` alone, or every one of ``roles``) and the declared
    room (ADR-0366 §3), at :func:`take_distance_m`: the gated window's floor is
    the gate's, and an ungated reading has none."""
    distance = take_distance_m(kind, distance_m)
    diameters = tuple(diameters_mm_by_target.get(target) for target in ((driver,) if driver else roles))
    return {window: asdict(trusted_band(distance_m=distance, driver=driver, room=room,
                                        gated=window == WINDOW_GATED, diameters_mm=diameters))
            for window in (WINDOW_GATED, WINDOW_UNGATED)}


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
