# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Build and publish round view documents without a front-end dependency."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace
from typing import Any

from jasper.atomic_io import atomic_write_json
from .commissioning_evidence_store import CommissioningEvidenceStoreError
from .frequency_view import FrequencyRun, build_frequency_view
from .frequency_plot import DEFAULT_REF_BAND_HZ, prepare_plot_curve, render_frequency_view
from .measurement_analysis import MeasurementAnalysisRefused, analyze_measurement_bundle
from .measurement_bass import bass_view
from .crossover_v2.room_grade import bundle_graph_scopes, grade_room_median, read_room_median
from .crossover_v2.room_views import room_document
from .crossover_v2.room_selection import select_seat_takes
from .crossover_v2.round_captures import RoundCapturesRefused
from .crossover_v2.round_inputs import RoundInputs, RoundSetRefused, round_inputs, read_run_manifest, resolve_set, default_out
from .round_view_artifacts import ARTIFACT_BY_VIEW

REFUSE_NO_SEAT_TAKES = "room_no_seat_takes"


def analyzed_frequency_run(path: Path, *, calibration_root: Path | None = None,
                           run_reference_db: float | None = None) -> FrequencyRun:
    try:
        inputs = round_inputs(path)
        run = analyze_measurement_bundle(
            inputs.session_dir, calibration_root=calibration_root, run_reference_db=run_reference_db,
        )
    except CommissioningEvidenceStoreError as exc:
        raise MeasurementAnalysisRefused(exc.code.value) from exc
    try:
        manifest = read_run_manifest(inputs)
    except RoundSetRefused as exc:
        if exc.reason not in {"round_manifest_missing", "round_manifest_unfinalized"}:
            raise
        return run
    bases = {take["take_id"] for group in manifest["sets"] if group.get("base") for take in group["takes"]}
    return replace(run, series=tuple(replace(curve, details={
        **curve.details, "base": curve.details.get("take_id") in bases,
    }) for curve in run.series))


def frequency_payload(run_a: FrequencyRun, run_b: FrequencyRun | None = None, *, ref_band_hz=DEFAULT_REF_BAND_HZ,
                      normalize: bool = False) -> tuple[dict, list[dict]]:
    payload = build_frequency_view(run_a, run_b)
    series = []
    for run in payload["runs"]:
        for curve in run["series"]:
            curve["plot"] = prepare_plot_curve(curve, run.get("metadata"), ref_band_hz=ref_band_hz, normalize=normalize)
            series.append({
                "slot": run["slot"], "id": curve["id"], "candidate_id": curve.get("candidate_id"),
                "position": curve.get("position"),
                **{key: curve["plot"][key] for key in ("display", "rms_db", "peak_to_peak_db", "band_means")},
            })
    return payload, series


def frequency_image(payload: dict, image: Path | None, *, series=(), plot_band_hz=None,
                    ref_band_hz=DEFAULT_REF_BAND_HZ, low_end: bool = False, normalize: bool = False) -> dict:
    if image is None:
        return {"image": None}
    try:
        render_frequency_view(payload, image, selected=series, band_hz=plot_band_hz,
                              ref_band_hz=ref_band_hz, low_end=low_end, normalize=normalize)
    except ImportError as exc:
        if not (exc.name or "").startswith("matplotlib"):
            raise
        return {"image": None, "reason": "plots_extra_missing"}
    return {"image": str(image)}


def bass_payload(inputs: RoundInputs, set_id: str | None, *, calibration_root: Path | None = None) -> dict[str, Any]:
    selected = resolve_set(inputs, set_id)
    payload = bass_view(inputs.session_dir, take_ids=selected.selected_ids, calibration_root=calibration_root)
    return {**payload, "set_id": selected.set_id, "candidate_id": selected.capture_basis.get("candidate_id")}


def room_payload(inputs: RoundInputs, set_id: str | None, *, calibration_root: Path | None = None) -> dict[str, Any]:
    selected = resolve_set(inputs, set_id)
    selection = select_seat_takes(
        inputs.session_dir,
        take_ids=selected.selected_ids, basis=selected.capture_basis, calibration_root=calibration_root,
    )
    if not selection.takes:
        raise RoundCapturesRefused(REFUSE_NO_SEAT_TAKES, {"set_id": selected.set_id,
                                                        "evidence": selection.evidence})
    payload = room_document(
        selection.takes, set_id=selected.set_id, evidence=selection.evidence,
        applied_profile_path=inputs.applied_profile_path, geometry_path=inputs.declared_geometry_path,
        manifest=read_run_manifest(inputs),
    )
    return payload


def write_room_document(inputs: RoundInputs, directory: Path, set_id: str | None, *,
                        calibration_root: Path | None = None) -> tuple[dict, Path]:
    payload = room_payload(inputs, set_id, calibration_root=calibration_root)
    path = default_out(inputs, directory, ARTIFACT_BY_VIEW["room"].artifact, set_id)
    atomic_write_json(path, payload)
    return payload, path


def _document(
    inputs: RoundInputs, directory: Path, set_id: str | None, calibration_root: Path | None,
) -> tuple[dict, Path]:
    if calibration_root is not None:
        return write_room_document(inputs, directory, set_id, calibration_root=calibration_root)
    path = default_out(inputs, directory, ARTIFACT_BY_VIEW["room"].artifact, set_id)
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or not isinstance(document.get("incumbent") or {}, dict):
        raise TypeError("room_document_malformed")
    return document, path


def room_grade_payload(inputs: RoundInputs, directory: Path, set_id: str | None, *,
                       incumbent_id: str | None = None, calibration_root: Path | None = None) -> dict[str, Any]:
    selected = resolve_set(inputs, set_id)
    candidate, candidate_path = _document(inputs, directory, set_id, calibration_root)
    incumbent_id = incumbent_id or (candidate.get("incumbent") or {}).get("set_id")
    if incumbent_id == selected.set_id:
        incumbent_id = None  # the incumbent's own measurement grades against nothing
    incumbent_doc = None
    if incumbent_id is not None:
        resolve_set(inputs, incumbent_id)
        incumbent_doc = _document(inputs, directory, incumbent_id, calibration_root)[0]
    median = read_room_median(candidate.get("median", {}))
    incumbent = None if incumbent_doc is None else read_room_median(incumbent_doc.get("median", {}))
    grade = grade_room_median(median, incumbent=incumbent)
    scope = (median.evidence or {}).get("basis", {}).get("graph_scope")
    artifact = {
        **grade.to_dict(),
        "room": str(candidate_path),
        "set_id": selected.set_id, "incumbent_set_id": incumbent_id,
        "incumbent_reason": "" if incumbent_id else (
            candidate.get("incumbent_reason") or "room_incumbent_set_unavailable"),
        "evidence": median.evidence,
        "incumbent_evidence": None if incumbent is None else incumbent.evidence,
        "graph_scopes": ([scope] if scope else []) if median.evidence is not None
                        else bundle_graph_scopes(inputs.session_dir),
        "graph_scopes_source": "selected_median" if median.evidence is not None else "round",
    }
    return artifact
