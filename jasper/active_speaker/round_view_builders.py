# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Build and publish round view documents without a front-end dependency."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.evidence_reasons import EvidenceUnavailable

from .commissioning_evidence_store import CommissioningEvidenceStoreError
from .frequency_view import FrequencyRun, build_frequency_view
from .frequency_plot import DEFAULT_REF_BAND_HZ, prepare_plot_curve, render_frequency_view
from .measurement_archive import ArchivedMeasurement, load_measurement
from .measurement_bass import bass_view
from .measurement_programs import run_purposes
from .crossover_v2.rear_views import rear_document
from .crossover_v2.record_index import measurement_documents
from .crossover_v2.room_grade import bundle_graph_scopes, grade_room_median, read_room_median
from .crossover_v2.room_views import room_document
from .crossover_v2.room_selection import select_seat_takes
from .crossover_v2.round_inputs import RoundInputs, RoundSetRefused, RoundViewsError, round_inputs, read_run_manifest, resolve_set, set_view_path
from .round_view_artifacts import ARTIFACT_BY_VIEW
from .round_packet_report import gate_fields
from .run_manifest import view_sets

REFUSE_NO_SEAT_TAKES = "room_no_seat_takes"


def analyzed_frequency_run(path: Path) -> FrequencyRun:
    """Every banked take's curves, read as the measurements page reads them and
    tagged with the set and selection its run manifest gives them."""
    try:
        inputs = round_inputs(path)
        try:
            manifest = read_run_manifest(inputs)
        except RoundSetRefused as exc:
            if exc.reason not in {"round_manifest_missing", "round_manifest_unfinalized"}:
                raise
            manifest = {}
        info = json.loads((inputs.session_dir / "info.json").read_text())
        documents = list(measurement_documents(inputs.session_dir))
        run = load_measurement(ArchivedMeasurement(info["session_id"], inputs.session_dir, info.get("started_at"),
                                                   info.get("state")), documents=documents)
        if unbanked := run.metadata.get("curves"):
            raise EvidenceUnavailable(unbanked["reason"], {"bundle_dir": str(inputs.session_dir)})
        rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = [
            (group, take) for group in manifest.get("sets", ()) for take in group["takes"]]
        series = []
        for curve in run.series:
            role = curve.details.get("role")
            group, take = next(((g, t) for g, t in rows if t["take_id"] == curve.details.get("take_id")
                               and (g["capture_basis"].get("role") or "summed") == role), ({}, {}))
            series.append(replace(curve, details={**curve.details, "base": group.get("base", False),
                                                  "set_id": group.get("set_id"), "selected": bool(take.get("selected")),
                                                  **gate_fields(curve.details)}))
        return replace(run, series=tuple(series))
    except CommissioningEvidenceStoreError as exc:
        raise RoundViewsError(f"{exc.code.value}: {exc}", code=exc.code.value) from exc



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


def bass_payload(inputs: RoundInputs, set_id: str | None) -> dict[str, Any]:
    selected = resolve_set(inputs, set_id)
    payload = bass_view(inputs.session_dir, take_ids=selected.selected_ids)
    return {**payload, "set_id": selected.set_id, "candidate_id": selected.capture_basis.get("candidate_id")}


def rear(inputs: RoundInputs, target: Path, set_id: str | None,
         incumbent: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The rear comparison reads the whole batch, so it takes no ``--set``."""
    payload = rear_document(inputs, manifest=read_run_manifest(inputs))
    comparison = payload["comparison"]
    return payload, {"set_id": payload["set_id"], "candidates": len(payload["candidates"]),
                     "positions": len(comparison["positions"]),
                     "band_hz": comparison["band_hz"], "band_source": comparison["band_source"],
                     "reference": comparison["reference"]}


def room_payload(inputs: RoundInputs, set_id: str | None) -> dict[str, Any]:
    manifest = read_run_manifest(inputs)
    selected = resolve_set(inputs, set_id, manifest=manifest)
    selection = select_seat_takes(
        inputs.session_dir, purposes=run_purposes(manifest["preset"]),
        take_ids=selected.selected_ids, basis=selected.capture_basis,
    )
    if not selection.takes:
        raise EvidenceUnavailable(REFUSE_NO_SEAT_TAKES, {"set_id": selected.set_id,
                                                       "evidence": selection.evidence})
    payload = room_document(
        selection.takes, set_id=selected.set_id, evidence=selection.evidence,
        applied_profile_path=inputs.applied_profile_path, geometry_path=inputs.declared_geometry_path,
        manifest=manifest,
    )
    return payload


def _document(inputs: RoundInputs, set_id: str | None, sets: Sequence[Mapping[str, Any]]) -> tuple[dict, Path]:
    path = set_view_path(inputs, ARTIFACT_BY_VIEW["room"].artifact, set_id, sets)
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or not isinstance(document.get("incumbent") or {}, dict):
        raise TypeError("room_document_malformed")
    return document, path


def room_grade_payload(inputs: RoundInputs, set_id: str | None, *, incumbent_id: str | None = None) -> dict[str, Any]:
    manifest = read_run_manifest(inputs)
    sets = view_sets(manifest)
    selected = resolve_set(inputs, set_id, manifest=manifest)
    candidate, candidate_path = _document(inputs, set_id, sets)
    incumbent_id = incumbent_id or (candidate.get("incumbent") or {}).get("set_id")
    if incumbent_id == selected.set_id:
        incumbent_id = None  # the incumbent's own measurement grades against nothing
    incumbent_doc = None
    if incumbent_id is not None:
        resolve_set(inputs, incumbent_id, manifest=manifest)
        incumbent_doc = _document(inputs, incumbent_id, sets)[0]
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
