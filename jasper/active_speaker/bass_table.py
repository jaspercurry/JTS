# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Fit retained bass comparisons at their recorded operating levels."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from jasper.json_fields import finite_float

from .bass_comparison import CHANGE_FIELDS, bass_capture_context
from .bass_fit import BassFitCoverageUnavailable, fit_bass_shape
from .crossover_v2.measurement_context import compare_capture_basis


def fit_bass_table(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    candidate_id: str, descriptor: Mapping[str, Any], target: Mapping[str, Any],
    tolerance_db: float,
    reference_band_hz: tuple[float, float] = (300.0, 1000.0),
) -> dict[str, Any]:
    tolerance = finite_float(tolerance_db)
    if tolerance is None or tolerance <= 0:
        raise ValueError("bass_table_tolerance_invalid")
    if not pairs:
        raise ValueError("bass_fit_inputs_missing")
    first = bass_capture_context(pairs[0][0])
    interventions = (*CHANGE_FIELDS["volume"], "pose_key")
    required = tuple(key for key in first if key not in interventions)
    groups: dict[tuple[float, float], list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
    contexts = []
    for before, after in pairs:
        for take in (before, after):
            if take.get("diagnostics", {}).get("integrity_failed"):
                raise ValueError("bass_table_capture_integrity_failed")
            context = bass_capture_context(take)
            volume, reference = (finite_float(context.get(key)) for key in CHANGE_FIELDS["volume"])
            if any(value is None or not -100 <= value <= 0 for value in (volume, reference)):
                raise ValueError("bass_table_operating_level_missing")
        context = bass_capture_context(before)
        comparison = compare_capture_basis(context, first, interventions=interventions, required=required)
        if comparison["incompatible_fields"]:
            raise ValueError("bass_table_capture_context_changed")
        assert volume is not None and reference is not None
        groups[volume, reference].append((before, after))
        contexts.append(comparison)
    if len({volume for volume, _ in groups}) != len(groups):
        raise ValueError("bass_table_conflicting_reference_levels")
    levels = []
    for (volume, reference), group in sorted(groups.items()):
        row = {"volume_db": volume, "bass_reference_db": reference,
               "sources": [{"before": before["record_path"], "after": after["record_path"]} for before, after in group]}
        try:
            fit = fit_bass_shape(group, candidate_id=candidate_id, descriptor=descriptor,
                                target=target, reference_band_hz=reference_band_hz)
        except BassFitCoverageUnavailable:
            levels.append({**row, "outcome": "insufficient_evidence", "fit": None,
                           "selected_is_measured": False, "within_tolerance_on_qualified_bins": None,
                           "selected_scale": None, "selected_descriptor": None})
            continue
        passing = [choice for choice in fit["choices"] if choice["scale"] in (0.0, 1.0)
                   and choice["max_abs_error_db"] <= tolerance]
        selected = min(passing, key=lambda choice: choice["mean_pose_rms_db"]) if passing else next(
            choice for choice in fit["choices"] if choice["scale"] == fit["selected_scale"])
        measured = selected["scale"] in (0.0, 1.0)
        complete = not fit["unqualified_hz"]
        within = selected["max_abs_error_db"] <= tolerance
        outcome = ("insufficient_evidence" if not complete else
                   "target_not_met" if not within else
                   "measurement_required" if not measured else "target_met")
        levels.append({**row, "outcome": outcome,
                       "selected_is_measured": measured, "within_tolerance_on_qualified_bins": within,
                       "selected_scale": selected["scale"], "selected_descriptor": selected["descriptor"], "fit": fit})
    return {"schema": "jts_bass_table/1", "candidate_id": candidate_id, "target": dict(target),
            "tolerance_db": tolerance, "reference_band_hz": list(reference_band_hz),
            "tested_volume_range_db": [levels[0]["volume_db"], levels[-1]["volume_db"]],
            "stimulus_dbfs": first["stimulus_dbfs"], "capture_context": contexts, "levels": levels,
            "limits": ["This table fits the recorded stimulus and bass-reference settings; it is not a runtime schedule.",
                       "No extrapolation beyond measured boost or operating levels. Hardware headroom is not established.",
                       "Intermediate descriptors require measurement before application. Missing frequency bins remain unproven."]}
