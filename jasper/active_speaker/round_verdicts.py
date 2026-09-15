# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Numerical verdicts from the round's saved fit and measurement evidence."""

from __future__ import annotations

import json
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.interference_nulls import (
    branch_gap_null_depth_ceiling_db,
    feature_position_variance,
)
from jasper.audio_measurement.spatial_combine import octave_bands_hz
from jasper.json_fields import finite_float

from .crossover_v2.intervention import CloudFitTerms
from .crossover_v2.position_cycle import parse_curve_magnitude
from .crossover_v2.round_inputs import RoundInputs, capture_identity, take_order
from .measurement_programs import POSE_KIND_BEARING
from .repeat_floor import load_repeat_floor, stopping_thresholds


def _cloud_curves(
    cloud: CloudFitTerms,
    clouds: Mapping[str, CloudFitTerms],
    manifest: Mapping[str, Any],
) -> list[tuple[np.ndarray, np.ndarray]]:
    if cloud.boost_responses:
        return [
            (response.freqs_hz, response.magnitude_db)
            for response in cloud.boost_responses
        ]
    positions: dict[float, Mapping[str, Any]] = {}
    for group in manifest.get("sets", ()):
        if clouds.get(group["set_id"]) is not cloud:
            continue
        for take in group["takes"]:
            pose = take["pose"]
            if (
                not take["selected"]
                or take.get("phase") != "measure"
                or take.get("role") != group["capture_basis"].get("role")
                or pose.get("kind") != POSE_KIND_BEARING
                or pose.get("deg") is None
            ):
                continue
            previous = positions.get(pose["deg"])
            if previous is None or take_order(take) >= take_order(previous):
                positions[pose["deg"]] = take
    return [
        (freqs, magnitude)
        for take in positions.values()
        if (curve := parse_curve_magnitude(take.get("curve") or {})) is not None
        for freqs, magnitude, _ in [curve]
    ]


def _null_ceilings(
    manifest: Mapping[str, Any], regions: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    poses: dict[tuple[Any, ...], dict[str, Mapping[str, Any]]] = {}
    for group in manifest.get("sets", ()):
        basis = group["capture_basis"]
        for take in group["takes"]:
            if (
                not take["selected"]
                or take.get("phase") != "measure"
                or take.get("role") in (None, "summed")
            ):
                continue
            key = (
                *capture_identity(basis, set_id=group["set_id"]),
                basis.get("level_db"),
                basis.get("loudness_volume_db"),
                json.dumps(take["pose"], sort_keys=True),
            )
            roles = poses.setdefault(key, {})
            previous = roles.get(take["role"])
            if previous is None or take_order(take) >= take_order(previous):
                roles[take["role"]] = take
    rows = []
    for roles in poses.values():
        for region in regions:
            pair = [region["lower_driver"], region["upper_driver"]]
            if any(role not in roles for role in pair):
                continue
            takes = [roles[role] for role in pair]
            parsed = [parse_curve_magnitude(take.get("curve") or {}) for take in takes]
            row: dict[str, Any] = {
                "pose": takes[0]["pose"],
                "roles": pair,
                "take_ids": [take["take_id"] for take in takes],
                "band_hz": None,
                "branch_gap_db": None,
                "null_ceiling_db": None,
                "reason": "branch_response_unavailable",
            }
            if all(curve is not None for curve in parsed):
                lower, upper = parsed
                assert lower is not None and upper is not None
                lo, hi = overlap_band_hz(
                    region["fc_hz"],
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
                    gap = abs(lower_level - upper_level)
                    ceiling = finite_float(branch_gap_null_depth_ceiling_db(gap))
                    row.update(
                        branch_gap_db=gap,
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
) -> dict[str, Any]:
    record = (
        load_repeat_floor(state_path=inputs.repeat_floor_path)
        if inputs.repeat_floor_path
        else None
    )
    spread = (stopping_thresholds(record) or {}).get("plateau_db") if record else None
    floor_reason = (
        None
        if spread is not None
        else ("repeat_floor_unavailable" if record else "repeat_floor_not_banked")
    )
    regions = ((sources.get("candidate") or {}).get("source_preset") or {}).get(
        "crossover_regions"
    ) or []
    fits = []
    for index, fit in enumerate(packet["fits"]):
        cloud = clouds.get(fit["set_id"], CloudFitTerms())
        curves = _cloud_curves(cloud, clouds, manifest)
        residual = fit.get("residual_rms_db")
        centers = list(
            dict.fromkeys(
                center
                for region in regions
                if fit["role"] in (region["lower_driver"], region["upper_driver"])
                for center, lo, hi in octave_bands_hz(0, float("inf"))
                if lo <= region["fc_hz"] < hi
            )
        )
        fits.append(
            {
                "fit_index": index,
                "repeat_spread_db": spread,
                "residual_within_repeat_spread": residual <= spread
                if residual is not None and spread is not None
                else None,
                "reason": floor_reason
                or ("fit_residual_unavailable" if residual is None else None),
                "crossover_band_centers_hz": centers,
                "features": [
                    {
                        "filter_index": i,
                        **feature_position_variance(
                            curves,
                            freq_hz=feature["freq"],
                            q=feature["q"],
                            gain_db=feature["gain"],
                            positions_total=cloud.n_positions,
                        ),
                    }
                    for i, feature in enumerate(fit.get("filters") or [])
                ],
            }
        )
    return {"fits": fits, "poses": _null_ceilings(manifest, regions)}
