# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Translate saved measurement or analysis JSON into the frequency view."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np

from .flat_spec import REFERENCE_BAND_HZ, evaluate_flat_spec
from .frequency_view import (
    FrequencyRun,
    FrequencySeries,
    FrequencyViewError,
    frequency_series,
)


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


def _finite_float(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return float(value)
    return None


def _stored_reference_db(curve: Mapping[str, Any]) -> float | None:
    return _finite_float(curve.get("reference_db"))


def _band_limited_curve(curve: Mapping[str, Any]) -> tuple[Any, Any]:
    """Apply the producer's declared valid band before a curve is exposed."""

    freqs = curve.get("freqs_hz")
    magnitude = curve.get("magnitude_db")
    band = curve.get("band_hz")
    if (
        not isinstance(freqs, Sequence)
        or isinstance(freqs, (str, bytes))
        or not isinstance(magnitude, Sequence)
        or isinstance(magnitude, (str, bytes))
        or len(freqs) != len(magnitude)
        or not isinstance(band, Sequence)
        or isinstance(band, (str, bytes))
        or len(band) != 2
    ):
        return freqs, magnitude
    try:
        lo_hz, hi_hz = (float(value) for value in band)
        numeric = tuple((float(hz), float(db)) for hz, db in zip(freqs, magnitude))
    except (TypeError, ValueError):
        return freqs, magnitude
    if (
        not math.isfinite(lo_hz)
        or not math.isfinite(hi_hz)
        or hi_hz < lo_hz
        or not all(math.isfinite(hz) and math.isfinite(db) for hz, db in numeric)
    ):
        return freqs, magnitude
    bounded = tuple((hz, db) for hz, db in numeric if lo_hz <= hz <= hi_hz)
    return tuple(hz for hz, _ in bounded), tuple(db for _, db in bounded)


def _valid_band(series: FrequencySeries) -> tuple[float | None, float | None]:
    raw = series.details.get("band_hz")
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or len(raw) != 2
    ):
        return None, None
    try:
        lo_hz, hi_hz = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(lo_hz) or not math.isfinite(hi_hz) or hi_hz < lo_hz:
        return None, None
    return lo_hz, hi_hz


def _evaluated_reference_db(series: FrequencySeries) -> float | None:
    """Use the product's reference evaluator; do not invent a view-only zero."""

    lo_hz, hi_hz = _valid_band(series)
    try:
        report = evaluate_flat_spec(
            np.asarray(series.freqs_hz, dtype=float),
            np.asarray(series.magnitude_db, dtype=float),
            smoothing_fraction=0,
            trusted_floor_hz=lo_hz,
            trusted_ceiling_hz=hi_hz,
        )
    except (OverflowError, TypeError, ValueError):
        return None
    return float(report.reference_db)


def _anchor_rank(series: FrequencySeries) -> tuple[int, float]:
    position = series.details.get("position")
    degrees = position.get("deg") if isinstance(position, Mapping) else None
    distance = (
        abs(float(degrees))
        if isinstance(degrees, (int, float)) and not isinstance(degrees, bool)
        else math.inf
    )
    role = str(series.details.get("role") or "summed")
    return (0 if role == "summed" and distance in {0.0, math.inf} else 1, distance)


def _share_run_reference(
    series: Sequence[FrequencySeries],
    run_reference_db: float | None,
) -> tuple[FrequencySeries, ...]:
    """Give every directly comparable curve one deterministic run reference."""

    if not series:
        return ()
    reference_db = _finite_float(run_reference_db)
    if reference_db is None:
        reference_db = next(
            (item.reference_db for item in series if item.reference_db is not None),
            None,
        )
    if reference_db is None:
        lo_hz, hi_hz = REFERENCE_BAND_HZ
        candidates = sorted(
            (item for item in series if any(lo_hz <= hz < hi_hz for hz in item.freqs_hz)),
            key=_anchor_rank,
        )
        reference_db = _evaluated_reference_db(candidates[0]) if candidates else None
    if reference_db is None:
        raise FrequencyViewError(
            "direct measurement has no stored reference and no curve overlaps "
            f"{REFERENCE_BAND_HZ[0]:g}-{REFERENCE_BAND_HZ[1]:g} Hz"
        )
    return tuple(replace(item, reference_db=reference_db) for item in series)


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

    series: list[FrequencySeries] = []
    seen_ids: set[str] = set()
    angles: set[int] = set()
    phases: set[str] = set()
    graphs: set[str] = set()
    takes: set[str] = set()

    for document_index, document in enumerate(documents):
        take_id = str(document.get("take_id") or document.get("id") or "")
        source_id = take_id or f"document_{document_index + 1}"
        if take_id:
            takes.add(take_id)
        degrees = _whole_degrees(document.get("position_deg"))
        if degrees is not None:
            angles.add(degrees)
        phase = str(document.get("phase") or "")
        graph = str(document.get("graph_fingerprint") or "")
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
            freqs_hz, magnitude_db = _band_limited_curve(curve)
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
                graph_fingerprint=graph or None,
                validity_floor_hz=document.get("validity_floor_hz"),
                band_hz=curve.get("band_hz"),
            )
            if item is not None:
                series.append(item)
                seen_ids.add(series_id)

    normalized = _share_run_reference(series, run_reference_db)
    if normalized:
        normalized = (replace(normalized[0], visible_by_default=True), *normalized[1:])
    return FrequencyRun(
        id=run_id,
        measurement_family="speaker_response",
        started_at=started_at,
        state=state,
        series=normalized,
        metadata={
            "position_count": len(takes),
            "angles_deg": sorted(angles),
            "phases": sorted(phases),
            "graph_fingerprints": sorted(graphs),
            "source": "banked measurement and analysis records",
        },
    )
