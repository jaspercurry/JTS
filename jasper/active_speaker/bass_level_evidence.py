# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Measured bass reach, drive and harmonic changes at one captured level."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from jasper.audio_measurement.quality_model import DRIVER
from jasper.json_fields import finite_float

from .bass_comparison import common_bass_bins
from .bass_fit import BASS_GRID_POINTS, aligned_bass_pair, smooth_bass_curve, smooth_bass_pair
from .crossover_v2.round_captures import doc_pose_key
from .measurement_bass import BASS_BANDS_HZ


def _median_curves(curves):
    values = np.asarray(curves)
    valid = np.isfinite(values).all(axis=0)
    result = np.full(values.shape[1], np.nan)
    result[valid] = np.median(values[:, valid], axis=0)
    return result


def _response(grid, curve, takes, reference_from_hz):
    valid = np.isfinite(curve)
    floor = next((lo for lo, hi in BASS_BANDS_HZ
                  if np.any(valid & (grid >= lo) & (grid < hi)) and all(
                      next((band["fundamental_qualified"] for band in take.get("bands", ())
                            if band["band_hz"] == [lo, hi]), True) for take in takes)), None)
    corners, bounded = {}, {}
    indices = np.flatnonzero(valid & (grid < reference_from_hz))
    contiguous = indices[np.r_[0, np.flatnonzero(np.diff(indices) > 1) + 1][-1]:] if indices.size else indices
    for depth in (3, 6, 10):
        if not contiguous.size or curve[contiguous[-1]] < -depth:
            corners[str(depth)] = bounded[str(depth)] = None
            continue
        crossing = None
        for low, high in zip(contiguous[-2::-1], contiguous[:0:-1]):
            if curve[low] <= -depth <= curve[high] and curve[low] < curve[high]:
                fraction = (-depth - curve[low]) / (curve[high] - curve[low])
                crossing = float(np.exp(np.log(grid[low]) + fraction * np.log(grid[high] / grid[low])))
                break
        corners[str(depth)] = crossing if crossing is not None else floor
        bounded[str(depth)] = crossing is None if floor is not None else None
    low = corners["10"]
    fit = contiguous[(grid[contiguous] >= low) & (grid[contiguous] <= corners["3"])] if low is not None and corners["3"] is not None else []
    # The octave axis increases downwards in frequency, so roll-off has a negative slope.
    slope = float(np.polyfit(-np.log2(grid[fit]), curve[fit], 1)[0]) if len(fit) >= 2 else None
    return {"corner_hz": corners, "corner_bounded": bounded, "qualified_from_hz": floor,
            "slope_db_per_octave": slope}


def _spl_at_mark(take):
    # WiredStimulusCapture banks this statistic from the take's calibrated WiredSplMonitor.
    spl = (take["record"].get("capture_integrity") or {}).get("spl") or {}
    return finite_float(spl.get("loudest_half_second_db_spl"))


def _level_spl(groups, side):
    poses = []
    for repeats in groups.values():
        readings = [_spl_at_mark(pair[side]) for pair in repeats]
        if any(value is None for value in readings):
            return None
        poses.append(float(np.median(readings)))
    return float(np.median(poses))


def _harmonics(groups, bands):
    rows, rises = [], []
    missing = False
    for lo, hi in bands:
        orders = {}
        for order in ("2", "3"):
            changes, spreads, floors = [], [], []
            for pose, repeats in groups.items():
                values = []
                for before, after in repeats:
                    a, b = (take.get("harmonics", {}).get(order) for take in (before, after))
                    if a is None or b is None:
                        break
                    f, base, candidate = common_bass_bins(a, b, "relative_db", "qualified")
                    mask = (f >= lo) & (f < hi)
                    if not mask.any():
                        break
                    values.append((float(np.mean(base[mask])), float(np.mean(candidate[mask]))))
                if len(values) != len(repeats):
                    missing = True
                    continue
                readings = np.asarray(values)
                delta = float(np.mean(readings[:, 1] - readings[:, 0]))
                spread = float(np.linalg.norm(np.std(readings, axis=0, ddof=1))) if len(values) > 1 else None
                floor = spread if spread is not None else 1.0
                changes.append(delta)
                spreads.append(spread)
                floors.append(floor)
                if delta > floor:
                    rises.append({"band_hz": [lo, hi], "order": int(order), "pose": pose,
                                  "delta_db": delta, "evidence_floor_db": floor})
            complete = len(changes) == len(groups)
            orders[order] = {"delta_db": float(np.mean(changes)) if complete else None,
                             "repeat_spread_db": max(spreads) if complete and all(s is not None for s in spreads) else None,
                             "evidence_floor_db": max(floors) if complete else None}
        rows.append({"band_hz": [lo, hi], "orders": orders})
    verdict = "harmonics_rose" if rises else "unknown" if missing or not rows else "harmonics_flat"
    return {"harmonics_delta_db": rows, "headroom_verdict": verdict, "headroom_rises": rises}


def bass_level_evidence(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    descriptor: Mapping[str, Any] | None, prescribed_boost_db: float | None,
    reference_band_hz: tuple[float, float],
) -> dict[str, Any]:
    grid = np.geomspace(BASS_BANDS_HZ[0][0], BASS_BANDS_HZ[-1][1], BASS_GRID_POINTS)
    groups = defaultdict(list)
    curves = defaultdict(list)
    shared_curves = defaultdict(list)
    for before, after in pairs:
        pose = doc_pose_key(before["record"])
        groups[pose].append((before, after))
        _, aligned = aligned_bass_pair(before, after, grid, reference_band_hz)
        shared_curves[pose].append(smooth_bass_pair(grid, aligned))
        curves[pose].append([smooth_bass_curve(grid, curve) for curve in aligned])
    per_pose = [_median_curves(np.asarray(repeats)[:, side])
                for side in (0, 1) for repeats in curves.values()]
    base = _median_curves(per_pose[:len(curves)])
    candidate = _median_curves(per_pose[len(curves):])
    delta = _median_curves([_median_curves(np.asarray(repeats)[:, 1]) - _median_curves(np.asarray(repeats)[:, 0])
                            for repeats in shared_curves.values()])
    boost_band = [descriptor.get("delta_highpass_hz") or BASS_BANDS_HZ[0][0],
                  descriptor["detector_lowpass_hz"]] if descriptor else None
    boost_bands = [(max(lo, boost_band[0]), min(hi, boost_band[1])) for lo, hi in BASS_BANDS_HZ
                   if boost_band and lo < boost_band[1] and hi > boost_band[0]]

    def mean_delta(lo, hi):
        mask = np.isfinite(delta) & (grid >= lo) & (grid < hi)
        return float(np.mean(delta[mask])) if mask.any() else None

    compression = [{"band_hz": [lo, hi], "value_db": prescribed_boost_db - measured
                    if (measured := mean_delta(lo, hi)) is not None and prescribed_boost_db is not None else None}
                   for lo, hi in boost_bands]
    snr = [finite_float(band.get("estimated_snr_db")) for pair in pairs for take in pair
           for lo, hi in BASS_BANDS_HZ if boost_band and lo < boost_band[1] and hi > boost_band[0]
           for band in [next((b for b in take.get("bands", ()) if b["band_hz"] == [lo, hi]), {})]]
    spreads = []
    for repeats in curves.values():
        if len(repeats) > 1:
            for side in (0, 1):
                values = np.asarray(repeats)[:, side]
                valid = np.isfinite(values).all(axis=0)
                if boost_band:
                    valid &= (grid >= boost_band[0]) & (grid < boost_band[1])
                spreads.extend(np.std(values[:, valid], axis=0, ddof=1).tolist())
    return {"base_response": _response(grid, base, [pair[0] for pair in pairs], reference_band_hz[0]),
            "candidate_response": _response(grid, candidate, [pair[1] for pair in pairs], reference_band_hz[0]),
            "boost_band_hz": boost_band,
            "realized_boost_db": [{"band_hz": [lo, hi], "value_db": mean_delta(lo, hi)} for lo, hi in BASS_BANDS_HZ],
            "compression_db": compression,
            "compression_includes": ["compressor", "driver"],
            **_harmonics(groups, boost_bands),
            "base_db_spl_at_mark": _level_spl(groups, 0), "candidate_db_spl_at_mark": _level_spl(groups, 1),
            "snr_margin_db": min(snr) - DRIVER.snr_warn_db if snr and all(s is not None for s in snr) else None,
            "repeat_spread_db": float(np.sqrt(np.mean(np.square(spreads)))) if spreads else None,
            "position_spread_db": None}
