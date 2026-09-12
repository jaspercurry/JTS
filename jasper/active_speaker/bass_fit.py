# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor

from .candidate_bank import find_banked_candidate
from .bass_comparison import bass_capture_context, common_bass_bins, compare_bass_takes
from .crossover_v2.measurement_context import compare_capture_basis
from .crossover_v2.round_captures import doc_pose_key
from .measurement_bass import BASS_BANDS_HZ


def fit_bass_shape(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    candidate_id: str, descriptor: Mapping[str, Any], target: Mapping[str, Any],
    reference_band_hz: tuple[float, float] = (300.0, 1000.0),
) -> dict[str, Any]:
    settings = validate_dynamic_bass_descriptor(descriptor)
    tf = np.asarray(target["freqs_hz"], dtype=float)
    ty = np.asarray(target["magnitude_db"], dtype=float)
    if (tf.ndim != 1 or len(tf) < 2 or ty.shape != tf.shape or not np.isfinite(tf).all()
            or not np.isfinite(ty).all() or tf[0] < 20 or tf[-1] > 200 or not np.all(np.diff(tf) > 0)):
        raise ValueError("bass_target_invalid")
    if not pairs or not 0 < reference_band_hz[0] < reference_band_hz[1]:
        raise ValueError("bass_fit_inputs_missing")
    grid = np.geomspace(tf[0], tf[-1], 121)
    desired = np.interp(np.log(grid), np.log(tf), ty)
    grouped: dict[Any, list[tuple[np.ndarray, np.ndarray]]] = defaultdict(list)
    sources = []
    first = bass_capture_context(pairs[0][0])
    for before, after in pairs:
        if (not before["record"].get("candidate_id") or after["record"].get("candidate_id") != candidate_id
                or find_banked_candidate(before["record"]["candidate_id"]).candidate.bass_extension):
            raise ValueError("bass_fit_requires_room_baseline_and_exact_candidate")
        match = compare_bass_takes(before, after, change="candidate")
        context = bass_capture_context(before)
        across = compare_capture_basis(context, first, interventions=("pose_key",),
                                       required=tuple(key for key in first if key != "pose_key"))
        if not match["available"] or across["incompatible_fields"]:
            raise ValueError("bass_fit_capture_context_changed")
        curve = before["frequency_curve"]
        rf, ry = np.asarray(curve["freqs_hz"]), np.asarray(curve["magnitude_db"])
        anchor = (rf >= reference_band_hz[0]) & (rf <= reference_band_hz[1]) & np.isfinite(ry)
        if not anchor.any():
            raise ValueError("bass_fit_reference_band_unavailable")
        reference = float(np.median(ry[anchor]))
        # Use the full qualification masks when resampling so holes stay holes.
        curves = []
        for take in (before, after):
            probe = {"freqs_hz": grid, "fundamental_db": np.zeros(grid.size), "fundamental_qualified": np.ones(grid.size)}
            f, _, y = common_bass_bins(probe, take, "fundamental_db", "fundamental_qualified")
            values = np.full(grid.shape, np.nan)
            values[np.searchsorted(grid, f)] = y - reference
            curves.append(values)
        shared = np.isfinite(curves).all(axis=0)
        for values in curves:
            values[~shared] = np.nan
            values[shared] = smooth_fractional_octave(grid[shared], values[shared], fraction=3)
        pose = doc_pose_key(before["record"])
        if before["record"].get("position_deg") is None and before["record"].get("seat_offset_m") is None:
            raise ValueError("bass_fit_pose_missing")
        grouped[pose].append((curves[0], curves[1]))
        sources.append({"before": before["record_path"], "after": after["record_path"],
                        "reference_db": reference, "comparison": match["context"], "across_positions": across})
    # Equal pose weight: repeated measurements estimate variation, not more seats.
    baseline, treated, positions = [], [], []
    for pose, repeats in grouped.items():
        a, b = np.asarray([row[0] for row in repeats]), np.asarray([row[1] for row in repeats])
        valid = np.isfinite(a).all(axis=0) & np.isfinite(b).all(axis=0)
        base_curve, trial_curve = np.full(grid.shape, np.nan), np.full(grid.shape, np.nan)
        base_curve[valid], trial_curve[valid] = np.median(a[:, valid], axis=0), np.median(b[:, valid], axis=0)
        baseline.append(base_curve)
        treated.append(trial_curve)
        positions.append({"pose": pose, "repeat_count": len(repeats), "repeat_gain_spread_db": [
            {"band_hz": [lo, hi], "median_range_db": float(np.median(np.ptp((b-a)[:, mask], axis=0))) if mask.any() and len(repeats) > 1 else None}
            for lo, hi in BASS_BANDS_HZ
            for mask in [valid & (grid >= lo) & (grid < hi)]
        ]})
    a, b = np.asarray(baseline), np.asarray(treated)
    valid = np.isfinite(a).all(axis=0) & np.isfinite(b).all(axis=0)
    if valid.sum() < 2:
        raise ValueError("bass_fit_common_coverage_unavailable")
    delta = b[:, valid] - a[:, valid]
    error = desired[valid] - a[:, valid]
    energy = float(np.sum(delta ** 2))
    fraction = float(np.clip(np.sum(delta * error) / energy, 0, 1)) if energy > 0 else 0.0
    choices = []
    for scale in sorted({0.0, fraction, 1.0}):
        prediction = a[:, valid] + scale * delta
        rms = np.sqrt(np.mean((prediction - desired[valid]) ** 2, axis=1))
        choices.append({"scale": scale, "descriptor": {**settings, "low_boost_db": scale * settings["low_boost_db"]} if scale else None,
                        "mean_pose_rms_db": float(np.mean(rms)), "per_pose_rms_db": rms.tolist(),
                        "predicted_median_db": np.median(prediction, axis=0).tolist()})
    return {"schema": "jts_bass_fit/1", "candidate_id": candidate_id, "source_descriptor": settings,
            "sources": sources, "positions": positions, "position_count": len(positions), "take_pair_count": len(pairs),
            "reference_band_hz": list(reference_band_hz), "target": dict(target), "smoothing_fraction": 3,
            "freqs_hz": grid[valid].tolist(), "unqualified_hz": grid[~valid].tolist(), "choices": choices,
            "selected_scale": fraction,
            "limits": ["Intermediate shapes are empirical dB interpolation within the measured boost range, not an exact dynamic DSP prediction; measure before saving.",
                       "The fit retains the tested volume and demand settings. It does not fit new taper settings or establish a driver limit.",
                       "Reference alignment is per pose. Missing bins are not fitted. Correct Room peaks in Room before fitting extension."]}
