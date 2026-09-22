# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Numerical verdicts from the round's saved fit and measurement evidence."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.evidence_reasons import REASON_REPEAT_FLOOR_NOT_BANKED
from jasper.audio_measurement.interference_nulls import (
    branch_gap_null_depth_ceiling_db,
    feature_position_variance,
)
from jasper.json_fields import finite_float

from .crossover_v2.commanded import profile_crossover_regions
from .crossover_v2.intervention import CloudFitTerms
from .crossover_v2.position_cycle import parse_curve_magnitude
from .crossover_v2.round_inputs import RoundInputs, capture_identity, latest_measure_takes
from .repeat_floor import load_repeat_floor, stopping_thresholds
from .profile import CrossoverRegion
from .speaker_fit import fit_feature_curves


def _null_ceilings(
    manifest: Mapping[str, Any], regions: Sequence[CrossoverRegion]
) -> list[dict[str, Any]]:
    poses: dict[tuple[Any, ...], dict[str, Mapping[str, Any]]] = {}
    latest = latest_measure_takes(
        ((group, take) for group in manifest.get("sets", ()) for take in group["takes"]),
        key=lambda group, take: (*capture_identity(group["capture_basis"], set_id=group["set_id"]),
                                group["capture_basis"].get("level_db"), group["capture_basis"].get("loudness_volume_db"),
                                json.dumps(take["pose"], sort_keys=True), take["role"])
        if take.get("role") not in (None, "summed") else None,
    )
    for key, (_group, take) in latest.items():
        poses.setdefault(key[:-1], {})[take["role"]] = take
    rows = []
    for (_candidate, capture_graph, *_), roles in poses.items():
        for region in regions:
            pair = [region.lower_driver, region.upper_driver]
            if any(role not in roles for role in pair):
                continue
            takes = [roles[role] for role in pair]
            parsed = [parse_curve_magnitude(take.get("curve") or {}) for take in takes]
            row: dict[str, Any] = {
                "pose": takes[0]["pose"],
                "capture_graph": capture_graph,
                "take_ids": [take["take_id"] for take in takes],
                "louder_role": None,
                "band_hz": None,
                "branch_gap_db": None,
                "null_ceiling_db": None,
                "reason": "branch_response_unavailable",
            }
            if all(curve is not None for curve in parsed):
                lower, upper = parsed
                assert lower is not None and upper is not None
                lo, hi = overlap_band_hz(
                    region.fc_hz,
                    tweeter_sweep_lo_hz=upper[2][0],
                    woofer_sweep_hi_hz=lower[2][1],
                )
                lo = max(
                    lo,
                    *(
                        take.get("curve", {}).get("validity_floor_hz") or 0
                        for take in takes
                    ),
                )
                row["band_hz"] = [lo, hi]
                levels = []
                for freqs, magnitude, _ in (lower, upper):
                    mask = (freqs >= lo) & (freqs <= hi) & np.isfinite(magnitude)
                    levels.append(
                        float(np.mean(magnitude[mask])) if np.any(mask) else None
                    )
                lower_level, upper_level = levels
                if lower_level is not None and upper_level is not None:
                    louder, quieter = sorted(zip((lower_level, upper_level), pair), reverse=True)
                    gap = louder[0] - quieter[0]
                    ceiling = finite_float(branch_gap_null_depth_ceiling_db(gap))
                    row.update(
                        branch_gap_db=gap,
                        louder_role=louder[1] if gap else None,
                        null_ceiling_db=ceiling,
                        reason=None if ceiling is not None else "equal_branch_levels",
                    )
                else:
                    row["reason"] = "overlap_bins_unavailable"
            rows.append(row)
    return rows


def round_verdicts(
    packet: Mapping[str, Any],
    inputs: RoundInputs,
    *,
    manifest: Mapping[str, Any],
    clouds: Mapping[str, CloudFitTerms],
    sources: Mapping[str, Any],
) -> list[dict[str, Any]]:
    record = (
        load_repeat_floor(state_path=inputs.repeat_floor_path)
        if inputs.repeat_floor_path
        else None
    )
    metrics = (record or {}).get("metrics")
    metric = metrics.get(record.get("aggregate_metric")) if isinstance(metrics, Mapping) and record else None
    unit = metric.get("unit", "db") if isinstance(metric, Mapping) else "db"
    spread = (stopping_thresholds(record) or {}).get(f"plateau_{unit}") if record else None
    floor_reason = (REASON_REPEAT_FLOOR_NOT_BANKED if record is None else "repeat_floor_unit_mismatch" if unit != "db"
                    else "repeat_floor_unavailable" if spread is None else None)
    if unit != "db":
        spread = None
    regions = profile_crossover_regions(sources.get("applied_profile"))
    for fit in packet["fits"]:
        cloud = clouds.get(fit["set_id"], CloudFitTerms())
        curves = fit_feature_curves(cloud)
        residual = fit.get("residual_rms_db")
        fit["crossover_band_spread"] = {
            f"{band['center_hz']:g} Hz": band
            for region in regions if fit["role"] in (region.lower_driver, region.upper_driver)
            for band in (fit.get("boost_evidence") or {}).get("band_spread", ())
            if band["f_lo"] <= region.fc_hz < band["f_hi"]
        } if regions else None
        fit["crossover_band_spread_reason"] = None if regions else "no_applied_crossover"
        fit["verdict"] = {"repeat_spread_db": spread,
                          "residual_within_repeat_spread": residual <= spread if residual is not None and spread is not None else None,
                          "reason": floor_reason or ("fit_residual_unavailable" if residual is None else None)}
        for feature in fit.get("filters") or []:
            feature["position_variance"] = feature_position_variance(
                curves, freq_hz=feature["freq"], q=feature["q"], gain_db=feature["gain"], positions_total=cloud.n_positions,
            )
    return _null_ceilings(manifest, regions)
