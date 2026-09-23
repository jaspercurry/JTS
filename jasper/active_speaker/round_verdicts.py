# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Numerical verdicts from the round's saved fit and measurement evidence."""

from __future__ import annotations

import json
from itertools import combinations
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.evidence_reasons import (
    REASON_FIT_BAND_UNAVAILABLE,
    REASON_MARK_FIT_BAND_UNAVAILABLE,
    REASON_MARK_RESPONSE_UNAVAILABLE,
    REASON_NO_MARK_PAIRS,
)
from jasper.audio_measurement.interference_nulls import (
    branch_gap_null_depth_ceiling_db,
    feature_position_variance,
)
from jasper.json_fields import finite_float
from jasper.audio_measurement.seat_figures import spread_rms_db

from .crossover_v2.commanded import profile_crossover_regions
from .crossover_v2.intervention import CloudFitTerms
from .crossover_v2.position_cycle import measured_curve_band, parse_curve_magnitude
from .crossover_v2.round_inputs import SetTakes, capture_identity, latest_measure_takes
from .linearization_envelope import DEFAULT_ENVELOPE_GRID_HZ
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


def mark_takes(selected: SetTakes, role: str | None) -> list[Mapping[str, Any]]:
    """One driver's selected MEASURE takes at exactly 0°/0° (ADR-0341)."""
    return [take for take in selected.on_axis if take.get("phase") == "measure" and take.get("role") == role]


def held_pairs(takes: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Every unordered pair held at one placement of one run (ADR-0341)."""
    placements: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for take in takes:
        key = (take.get("run_id"), take.get("pose_index"), json.dumps(take["pose"], sort_keys=True))
        placements.setdefault(key, []).append(take)
    return [pair for held in placements.values() for pair in combinations(held, 2)]


def pair_spread(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], band_hz: Sequence[float] | None,
) -> dict[str, Any]:
    """The largest per-pair RMS dB difference over ``band_hz``, edges included,
    on the fit's grid, with no level removal or smoothing (ADR-0341)."""
    result: dict[str, Any] = {"repeat_spread_db": None, "n_pairs": len(pairs),
                              "repeat_basis": "mark_pairs_max_rms" if pairs else REASON_NO_MARK_PAIRS}
    if not pairs:
        return {**result, "reason": REASON_NO_MARK_PAIRS}
    lo, hi = band_hz or (0, 0)
    grid = DEFAULT_ENVELOPE_GRID_HZ[(DEFAULT_ENVELOPE_GRID_HZ >= lo) & (DEFAULT_ENVELOPE_GRID_HZ <= hi)]
    if not grid.size:
        return {**result, "reason": REASON_FIT_BAND_UNAVAILABLE}
    curves = {}
    for take in {id(take): take for pair in pairs for take in pair}.values():
        measured = measured_curve_band(take.get("curve") or {})
        if measured is None:
            return {**result, "reason": REASON_MARK_RESPONSE_UNAVAILABLE}
        freqs, magnitude, (covered_lo, covered_hi) = measured
        if grid[0] < covered_lo or grid[-1] > covered_hi:
            return {**result, "reason": REASON_MARK_FIT_BAND_UNAVAILABLE}
        curves[id(take)] = np.interp(grid, freqs, magnitude)
    # The band includes its upper edge; seat_figures uses half-open bands.
    spreads = [finite_float(spread_rms_db(curves[id(a)] - curves[id(b)], grid,
                                        band_hz=(lo, np.nextafter(hi, np.inf)))) for a, b in pairs]
    if any(spread is None for spread in spreads):
        return {**result, "reason": REASON_MARK_RESPONSE_UNAVAILABLE}
    return {**result, "repeat_spread_db": max(spread for spread in spreads if spread is not None), "reason": None}


def _mark_repeat_spread(fit: Mapping[str, Any], group: Mapping[str, Any]) -> dict[str, Any]:
    return pair_spread(held_pairs(mark_takes(SetTakes.from_row(group), fit["role"])), fit.get("fit_band_hz"))


def round_verdicts(
    packet: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    clouds: Mapping[str, CloudFitTerms],
    sources: Mapping[str, Any],
) -> list[dict[str, Any]]:
    groups = {group["set_id"]: group for group in manifest.get("sets", ())}
    regions = profile_crossover_regions(sources.get("applied_profile"))
    for fit in packet["fits"]:
        repeat = _mark_repeat_spread(fit, groups[fit["set_id"]])
        spread = repeat["repeat_spread_db"]
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
        fit["verdict"] = {**repeat,
                          "residual_within_repeat_spread": residual <= spread if residual is not None and spread is not None else None,
                          "reason": repeat["reason"] or ("fit_residual_unavailable" if residual is None else None)}
        for feature in fit.get("filters") or []:
            feature["position_variance"] = feature_position_variance(
                curves, freq_hz=feature["freq"], q=feature["q"], gain_db=feature["gain"], positions_total=cloud.n_positions,
            )
    return _null_ceilings(manifest, regions)
