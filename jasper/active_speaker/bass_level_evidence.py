# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Measured bass reach, drive and harmonic changes within and across levels."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from jasper.audio_measurement.quality_model import DRIVER
from jasper.json_fields import finite_float

from .bass_comparison import bass_curve_on_grid, common_bass_bins
from .measurement_bass import BASS_BANDS_HZ


def _band(take, lo, hi):
    return next((band for band in take.get("bands", ()) if band["band_hz"] == [lo, hi]), {})


def _response(grid, curve, takes, reference_from_hz):
    valid = np.isfinite(curve)
    floor = next((lo for lo, hi in BASS_BANDS_HZ
                  if np.any(valid & (grid >= lo) & (grid < hi)) and all(
                      _band(take, lo, hi).get("fundamental_qualified") for take in takes)), None)
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


def _growth_readings(takes, lo, hi, order):
    if not all(_band(take, lo, hi).get("fundamental_qualified") for take in takes):
        return None
    curves = [(take, "fundamental_db", "fundamental_qualified") for take in takes]
    if order != "fundamental":
        if not all(order in take.get("harmonics", {}) for take in takes):
            return None
        curves += [(take["harmonics"][order], "relative_db", "qualified") for take in takes]
    grid = np.asarray(takes[0]["freqs_hz"])
    values, masks = zip(*(bass_curve_on_grid(grid, *curve) for curve in curves))
    shared = np.logical_and.reduce(masks) & (grid >= lo) & (grid < hi)
    if not shared.any():
        return None
    values = np.asarray(values)[:, shared]
    readings = np.mean(values, axis=1)
    fundamental = readings[:len(takes)]
    relative = readings[len(takes):] if order != "fundamental" else np.zeros(len(takes))
    snr = [finite_float(_band(take, lo, hi).get("estimated_snr_db")) for take in takes]
    if order != "fundamental":
        for i, take in enumerate(takes):
            harmonic = take["harmonics"][order]
            if "floor_relative_db" not in harmonic:
                snr.append(None)
                continue
            floor, qualified = bass_curve_on_grid(grid, harmonic, "floor_relative_db", "qualified")
            snr.append(float(np.min(values[len(takes) + i] - floor[shared])) if qualified[shared].all() else None)
    return fundamental, relative, snr


def _order_growth(before, after, side, lo, hi, order):
    changes, excesses, readings = [], [], []
    for pose, repeats in before.items():
        takes = [pair[side] for pair in (*repeats, *after[pose])]
        values = _growth_readings(takes, lo, hi, order)
        if values is None:
            return None
        fundamental, relative, snr = values
        n = len(repeats)
        absolute = fundamental + relative
        changes.append(float(np.median(absolute[n:]) - np.median(absolute[:n])))
        excesses.append(changes[-1] - float(np.median(fundamental[n:]) - np.median(fundamental[:n])))
        readings.append((relative[:n], relative[n:], snr))
    return {"growth_db": float(np.median(changes)), "excess_db": float(np.median(excesses)), "readings": readings}


def _growth_allowance(growth):
    if growth is None:
        return None, None
    readings = growth["readings"]
    if all(len(before) > 1 and len(after) > 1 for before, after, _ in readings):
        spread = max(float(np.sqrt(np.var(before, ddof=1) + np.var(after, ddof=1))) for before, after, _ in readings)
        return spread, "repeat_spread_db"
    snr = [value for _, _, values in readings for value in values]
    if any(value is None for value in snr):
        return None, None
    # H_after - F_after - H_before + F_before sums four level errors, each 20*log10(1 + 10**(-SNR/20)) at the worst SNR.
    return float(4 * 20 * np.log10(1 + 10 ** (-min(snr) / 20))), "snr_uncertainty_db"


def _band_growth(before, after, side, lo, hi, step):
    readings = {order: _order_growth(before, after, side, lo, hi, order)
                if step > 0 and before and before.keys() == after.keys() else None for order in ("fundamental", "2", "3")}
    allowances = {order: _growth_allowance(readings[order]) for order in ("2", "3")}
    rises = [int(order) for order, (allowance, _) in allowances.items()
             if allowance is not None and readings[order]["excess_db"] > allowance]
    return {"band_hz": [lo, hi],
            "growth_db_per_db": {order: reading["growth_db"] / step if reading is not None else None for order, reading in readings.items()},
            "allowance_db_per_db": {order: value / step if value is not None else None for order, (value, _) in allowances.items()},
            "allowance_basis": {order: basis for order, (_, basis) in allowances.items()}, "knee_orders": rises}


def bass_ladder_evidence(levels: list[dict[str, Any]], groups: list[Mapping[str, Any]]) -> None:
    for row in levels:
        row["headroom"] = {"base": [], "candidate": []}
    for side, stack in enumerate(("base", "candidate")):
        for lo, hi in BASS_BANDS_HZ:
            rows = [_band_growth(groups[i - 1] if i else {}, group, side, lo, hi,
                                 levels[i]["level_key"]["level_db"] - levels[i - 1]["level_key"]["level_db"] if i else 0)
                    for i, group in enumerate(groups)]
            knee = next((i for i, row in enumerate(rows) if row["knee_orders"]), None)
            clean = [i for i, row in enumerate(rows) if i and (knee is None or i < knee)
                     and all(value is not None for value in row["allowance_db_per_db"].values())]
            top = clean[-1] if clean else 0 if knee == 1 else None
            unmeasured = [levels[i]["level_key"] for i, row in enumerate(rows) if i
                          and any(value is None for value in row["allowance_db_per_db"].values())]
            spl_key = f"{stack}_db_spl_at_mark"
            top_spl = levels[top][spl_key] if top is not None else None
            for level, row in zip(levels, rows):
                current_spl = level[spl_key]
                row.update(knee_level_db_spl=levels[knee][spl_key] if knee is not None else None,
                           knee_bounded="above_top_rung" if knee is None and clean else None,
                           unmeasured_level_keys=unmeasured,
                           top_clean_level_db_spl=top_spl,
                           headroom_remaining_db=top_spl - current_spl if top_spl is not None and current_spl is not None else None,
                           extrapolated=knee is None)
                level["headroom"][stack].append(row)


def bass_level_evidence(
    aligned: Mapping[str, Any], *,
    descriptor: Mapping[str, Any] | None, prescribed_boost_db: float | None,
) -> dict[str, Any]:
    grid, delta = aligned["freqs_hz"], aligned["delta"]
    groups, curves = aligned["groups"], aligned["curves"]
    pairs = [pair for repeats in groups.values() for pair in repeats]
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
    snr = [value for pair in pairs for take in pair
           for lo, hi in BASS_BANDS_HZ if boost_band and lo < boost_band[1] and hi > boost_band[0]
           if (value := finite_float(_band(take, lo, hi).get("estimated_snr_db"))) is not None]
    spreads = []
    for repeats in curves.values():
        if len(repeats) > 1:
            for side in (0, 1):
                values = np.asarray(repeats)[:, side]
                valid = np.isfinite(values).all(axis=0)
                if boost_band:
                    valid &= (grid >= boost_band[0]) & (grid < boost_band[1])
                spreads.extend(np.std(values[:, valid], axis=0, ddof=1).tolist())
    return {"base_response": _response(grid, aligned["base"], [pair[0] for pair in pairs], aligned["reference_band_hz"][0]),
            "candidate_response": _response(grid, aligned["candidate"], [pair[1] for pair in pairs], aligned["reference_band_hz"][0]),
            "sources": aligned["sources"], "position_count": len(groups), "take_pair_count": len(pairs),
            "prescribed_boost_db": prescribed_boost_db, "boost_band_hz": boost_band,
            "realized_boost_db": [{"band_hz": [lo, hi], "value_db": mean_delta(lo, hi)} for lo, hi in BASS_BANDS_HZ],
            "compression_db": compression,
            "compression_includes": ["compressor", "driver"],
            **_harmonics(groups, boost_bands),
            "base_db_spl_at_mark": _level_spl(groups, 0), "candidate_db_spl_at_mark": _level_spl(groups, 1),
            "snr_margin_db": min(snr) - DRIVER.snr_warn_db if snr else None,
            "repeat_spread_db": float(np.sqrt(np.mean(np.square(spreads)))) if spreads else None,
            "position_spread_db": None}
