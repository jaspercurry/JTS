# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Translate saved measurement or analysis JSON into the frequency view."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from .crossover_v2.record_index import played_graph_fingerprint
from .crossover_v2.round_captures import doc_pose_key
from .frequency_reference import band_limited_curve, share_run_reference
from .frequency_view import (
    FrequencyRun,
    FrequencySeries,
    frequency_series,
)
from .prediction_document import frequency_run_from_capture_prediction
from jasper.json_fields import finite_float


def _whole_degrees(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _label(record: Mapping[str, Any], curve: Mapping[str, Any], fallback: str) -> str:
    degrees = _whole_degrees(record.get("position_deg"))
    vertical = _whole_degrees(record.get("vertical_deg")) or 0
    role = str(curve.get("role") or record.get("role") or "")
    parts = []
    if degrees is not None:
        parts.append(f"{degrees:+d}°" if degrees else "0°")
    if vertical:
        parts.append(f"{abs(vertical)}° {'up' if vertical > 0 else 'down'}")
    if role and role != "summed":
        parts.append(role.replace("_", " ").title())
    phase = str(record.get("phase") or "").replace("_", " ").strip()
    if phase:
        parts.append(phase.title())
    return " · ".join(parts) or fallback


def _stored_reference_db(curve: Mapping[str, Any]) -> float | None:
    return finite_float(curve.get("reference_db"))


def _curve_nodes(value: Any, path: str = "") -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Yield curve-shaped mappings from a JSON measurement or analysis."""

    if isinstance(value, Mapping):
        if "freqs_hz" in value and "magnitude_db" in value:
            yield path or "curve", value
            return
        for key, child in value.items():
            if isinstance(child, (Mapping, list, tuple)):
                child_path = f"{path}.{key}" if path else str(key)
                yield from _curve_nodes(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            if isinstance(child, Mapping):
                yield from _curve_nodes(child, f"{path}[{index}]")


def frequency_run_from_documents(
    *,
    run_id: str,
    documents: Sequence[Mapping[str, Any]],
    started_at: Any = None,
    state: str | None = None,
    run_reference_db: float | None = None,
) -> FrequencyRun:
    """Adapt saved measurement or analysis JSON without knowing its producer."""

    if len(documents) == 1 and documents[0].get("kind") == "jts_capture_prediction":
        return frequency_run_from_capture_prediction(
            run_id=run_id,
            document=documents[0],
            started_at=started_at,
            state=state,
        )

    series: list[FrequencySeries] = []
    seen_ids: set[str] = set()
    angles: set[int] = set()
    phases: set[str] = set()
    graphs: set[str] = set()
    takes: set[str] = set()
    poses: set[str] = set()

    for document_index, document in enumerate(documents):
        take_id = str(document.get("take_id") or document.get("id") or "")
        source_id = take_id or f"document_{document_index + 1}"
        if take_id:
            takes.add(take_id)
        if document.get("position_deg") is not None or document.get("seat_offset_m") is not None:
            poses.add(doc_pose_key(document))
        degrees = _whole_degrees(document.get("position_deg"))
        if degrees is not None:
            angles.add(degrees)
        phase = str(document.get("phase") or "")
        graph = played_graph_fingerprint(document)
        if phase:
            phases.add(phase)
        if graph:
            graphs.add(graph)

        for curve_index, (path, curve) in enumerate(_curve_nodes(document)):
            role = str(curve.get("role") or document.get("role") or "")
            base_id = f"{source_id}:{role or path or curve_index}"
            series_id = base_id
            suffix = 2
            while series_id in seen_ids:
                series_id = f"{base_id}:{suffix}"
                suffix += 1
            declared_kind = str(curve.get("kind") or document.get("kind") or "")
            kind = (
                declared_kind if declared_kind in {"measurement", "analysis"}
                else "analysis" if path.split(".", 1)[0] == "analysis"
                else "measurement"
            )
            fallback_label = str(
                curve.get("label") or path.replace(".", " · ").replace("_", " ")
            )
            freqs_hz, magnitude_db = band_limited_curve(curve)
            item = frequency_series(
                series_id=series_id,
                label=_label(document, curve, fallback_label),
                kind=kind,
                freqs_hz=freqs_hz,
                magnitude_db=magnitude_db,
                reference_db=_stored_reference_db(curve),
                visible_by_default=False,
                role=role or None,
                position={
                    "axis": document.get("position_axis"),
                    "deg": document.get("position_deg"),
                    "vertical_deg": _whole_degrees(document.get("vertical_deg")) or 0,
                    "mark_distance_m": document.get("mark_distance_m"),
                },
                take_id=take_id or None,
                phase=phase or None,
                candidate_id=document.get("candidate_id"),
                graph_scope=document.get("graph_scope"),
                level_db=document.get("level_db"),
                stimulus_dbfs=document.get("stimulus_dbfs"),
                calibration=document.get("calibration"),
                graph_fingerprint=graph or None,
                validity_floor_hz=curve.get("validity_floor_hz", document.get("validity_floor_hz")),
                gate_window_ms=curve.get("gate_window_ms", document.get("gate_window_ms", (document.get("diagnostic") or {}).get("verify_gate_window_ms") if role == "summed" else None)),
                smoothing_fractional_octave=curve.get("smoothing_fractional_octave"),
                band_hz=curve.get("band_hz"),
            )
            if item is not None:
                series.append(item)
                seen_ids.add(series_id)

    normalized = share_run_reference(series, run_reference_db)
    if normalized:
        normalized = (replace(normalized[0], visible_by_default=True), *normalized[1:])
    return FrequencyRun(
        id=run_id,
        measurement_family="speaker_response",
        started_at=started_at,
        state=state,
        series=normalized,
        metadata={
            "position_count": len(poses), "take_count": len(takes),
            "angles_deg": sorted(angles),
            "phases": sorted(phases),
            "graph_fingerprints": sorted(graphs),
            "source": "banked measurement and analysis records",
        },
    )
