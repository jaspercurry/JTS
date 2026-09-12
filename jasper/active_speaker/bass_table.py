# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Fit retained bass comparisons at their captured operating levels."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from jasper.bass_extension.dynamic import DynamicBassDescriptor, loudness_boost_db
from jasper.json_fields import finite_float

from .bass_comparison import CHANGE_FIELDS, bass_capture_context
from .bass_fit import REFERENCE_BAND_HZ, BassFitCoverageUnavailable, fit_bass_shape
from .crossover_v2.measurement_context import compare_capture_basis
from .crossover_v2.refusal_copy import CrossoverV2Refused, refusal_copy_for

LEVEL_FIELDS = (*CHANGE_FIELDS["volume"], "program_id")


def level_key(basis: Mapping[str, Any], **identity: Any) -> tuple[float, float, str]:
    volume, reference = (finite_float(basis.get(key)) for key in CHANGE_FIELDS["volume"])
    program = basis.get("program_id")
    missing = [key for key, value in zip(CHANGE_FIELDS["volume"], (volume, reference))
               if value is None or not -100 <= value <= 0]
    if not isinstance(program, str) or not program.strip():
        missing.append("program_id")
    if missing:
        raise CrossoverV2Refused({**identity, "fields": missing}, code="bass_table_operating_level_missing")
    assert volume is not None and reference is not None and isinstance(program, str)
    return volume, reference, program


def fit_bass_table(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    candidate_id: str, descriptor: Mapping[str, Any], target: Mapping[str, Any],
    tolerance_db: float, reference_band_hz: tuple[float, float] = REFERENCE_BAND_HZ,
) -> dict[str, Any]:
    tolerance = finite_float(tolerance_db)
    if tolerance is None or tolerance <= 0:
        raise CrossoverV2Refused(code="bass_table_tolerance_invalid")
    if not pairs:
        raise CrossoverV2Refused(code="bass_fit_inputs_missing")
    settings = DynamicBassDescriptor(**dict(descriptor))
    first = bass_capture_context(pairs[0][0])
    interventions = (*CHANGE_FIELDS["volume"], "pose_key")
    required = tuple(key for key in first if key not in interventions)
    groups: dict[tuple[float, float, str], list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
    contexts = []
    for before, after in pairs:
        for take in (before, after):
            if take.get("diagnostics", {}).get("integrity_failed"):
                raise CrossoverV2Refused({"record_path": take["record_path"]}, code="bass_table_capture_integrity_failed")
            level_key(bass_capture_context(take), record_path=take["record_path"])
        context = bass_capture_context(before)
        comparison = compare_capture_basis(context, first, interventions=interventions, required=required)
        if comparison["incompatible_fields"]:
            raise CrossoverV2Refused(comparison, code="bass_table_capture_context_changed")
        groups[level_key(context)].append((before, after))
        contexts.append(comparison)
    levels = []
    for key, group in sorted(groups.items()):
        row = {"level_key": dict(zip(LEVEL_FIELDS, key)), "loudness_boost_db": loudness_boost_db(key[1], settings),
               "sources": [{"before": before["record_path"], "after": after["record_path"]} for before, after in group]}
        try:
            fit = fit_bass_shape(group, candidate_id=candidate_id, descriptor=descriptor,
                                target=target, reference_band_hz=reference_band_hz)
        except BassFitCoverageUnavailable as refusal:
            levels.append({**row, "outcome": "insufficient_evidence", "code": refusal.code,
                           "next_action": refusal_copy_for(refusal.code)[1], "fit": None,
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
            "tested_volume_range_db": [min(key[0] for key in groups), max(key[0] for key in groups)],
            "stimulus_dbfs": first["stimulus_dbfs"], "capture_context": contexts, "levels": levels,
            "limits": ["This table fits the recorded stimulus and bass-reference settings; it is not a runtime schedule.",
                       "No extrapolation beyond measured boost or operating levels. Hardware headroom is not established.",
                       "Intermediate descriptors require measurement before application. Missing frequency bins remain unproven."]}
