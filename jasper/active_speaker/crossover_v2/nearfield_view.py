# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A round's near-field driver takes read band by band, and their distance
self-test: a pure view of banked evidence (ADR-0346, ADR-0354).

Each kept take banks its first sweep's curve with the other sweeps nested
beside it. The first sweep against the others shows an amplifier waking late
(#5684); the last two sweeps against each other give the band's SNR. Per
driver, the level step between two distances is held to a rigid piston of the
declared cone.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from itertools import combinations
from typing import Any

import numpy as np

#: The low bass a gated take cannot resolve, up to the near-field sweep's ~2 kHz top.
NEAR_FIELD_BANDS_HZ = ((20.0, 35.0), (35.0, 50.0), (50.0, 100.0), (100.0, 200.0),
                       (200.0, 400.0), (400.0, 800.0), (800.0, 2000.0))
#: A band whose last two sweeps agree to this SNR is trusted (the cabinet-model analyzer's bar).
TRUSTED_SNR_DB = 20.0
#: Where the distance step is read: above a port, below cone breakup (#5684).
STEP_BAND_HZ = (35.0, 400.0)
#: The piston runs 0.15-0.3 dB short of jts3's measured 15 -> 30 mm step (#5684).
STEP_TOLERANCE_DB = 0.4


def piston_step_db(near_m: float, far_m: float, radius_m: float) -> float:
    """How far a rigid piston's on-axis level falls from ``near_m`` to ``far_m``,
    in its low-frequency limit, dB (negative moving away)."""
    def reach(distance_m: float) -> float:
        return math.hypot(distance_m, radius_m) - distance_m
    return 20.0 * math.log10(reach(far_m) / reach(near_m))


def _power_db(magnitude_db: np.ndarray) -> float:
    return float(10.0 * np.log10(np.mean(10.0 ** (magnitude_db / 10.0))))


def _sweeps(curve: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """The curve's grid, and one row of magnitude per sweep, first sweep first."""
    rows = [curve, *curve.get("repeat_curves", ())]
    return (np.asarray(curve["freqs_hz"], dtype=float),
            np.asarray([row["magnitude_db"] for row in rows], dtype=float))


def _band(freqs: np.ndarray, sweeps: np.ndarray, band_hz: tuple[float, float]) -> dict[str, Any] | None:
    inside = (freqs >= band_hz[0]) & (freqs < band_hz[1])
    if not inside.any():
        return None
    within = sweeps[:, inside]
    row: dict[str, Any] = {"band_hz": list(band_hz), "level_db": round(_power_db(within), 2),
                           "first_minus_rest_db": None, "snr_db": None, "trusted": False}
    if len(within) > 1:
        row["first_minus_rest_db"] = round(_power_db(within[0]) - _power_db(within[1:]), 2)
        rms = float(np.sqrt(np.mean((within[-1] - within[-2]) ** 2)))
        if rms > 0.0:
            row["snr_db"] = round(20.0 * math.log10(20.0 / math.log(10.0) / rms), 1)
            row["trusted"] = row["snr_db"] >= TRUSTED_SNR_DB
    return row


def _step_band_level_db(takes: list[Mapping[str, Any]]) -> float | None:
    levels = []
    for take in takes:
        freqs, sweeps = _sweeps(take["curve"])
        inside = (freqs >= STEP_BAND_HZ[0]) & (freqs <= STEP_BAND_HZ[1])
        if inside.any():
            levels.append(_power_db(sweeps[:, inside]))
    return _power_db(np.asarray(levels)) if levels else None


def nearfield_view(
    takes: Iterable[Mapping[str, Any]], *, radiating_diameter_mm_by_role: Mapping[str, float],
) -> dict[str, Any]:
    """The kept near-field takes of a round's run manifest, band by band, and
    each driver's placements and distance steps."""
    kept = [take for take in takes
            if take.get("selected") and (take.get("pose") or {}).get("driver") and take.get("curve")]
    rows = []
    for take in kept:
        freqs, sweeps = _sweeps(take["curve"])
        rows.append({
            "take_id": take["take_id"], "driver": take["pose"]["driver"],
            "distance_mm": round(float(take["pose"]["distance_m"]) * 1000.0, 1),
            "max_window_db_spl": ((take.get("quality") or {}).get("evidence") or {}).get("max_window_db_spl"),
            "bands": [row for band in NEAR_FIELD_BANDS_HZ if (row := _band(freqs, sweeps, band)) is not None],
        })
    drivers = []
    for driver in sorted({row["driver"] for row in rows}):
        diameter = radiating_diameter_mm_by_role.get(driver.partition(":")[0])
        at: dict[float, list[Mapping[str, Any]]] = {}
        for take, row in zip(kept, rows):
            if row["driver"] == driver:
                at.setdefault(row["distance_mm"], []).append(take)
        placements = []
        for distance_mm, placed in sorted(at.items()):
            levels = [[band["level_db"] for band in row["bands"]] for row in rows
                      if row["driver"] == driver and row["distance_mm"] == distance_mm]
            spread = (np.ptp(np.asarray(levels), axis=0).round(2).tolist()
                      if len(levels) > 1 and len({len(one) for one in levels}) == 1 else None)
            placements.append({"distance_mm": distance_mm, "take_ids": [take["take_id"] for take in placed],
                               "reseat_spread_db": spread})
        steps = []
        for near_mm, far_mm in combinations(sorted(at), 2):
            near, far = _step_band_level_db(at[near_mm]), _step_band_level_db(at[far_mm])
            if near is None or far is None:
                continue
            measured = round(far - near, 2)
            piston = (round(piston_step_db(near_mm / 1000.0, far_mm / 1000.0, diameter / 2000.0), 2)
                      if diameter else None)
            steps.append({"near_mm": near_mm, "far_mm": far_mm, "step_db": measured, "piston_db": piston,
                          "verdict": "not_evaluated" if piston is None else
                          "pass" if abs(measured - piston) <= STEP_TOLERANCE_DB else "fail"})
        drivers.append({"driver": driver, "radiating_diameter_mm": diameter, "placements": placements,
                        "steps": steps})
    return {
        "parameters": {"bands_hz": [list(band) for band in NEAR_FIELD_BANDS_HZ], "trusted_snr_db": TRUSTED_SNR_DB,
                       "step_band_hz": list(STEP_BAND_HZ), "step_tolerance_db": STEP_TOLERANCE_DB},
        "takes": rows, "drivers": drivers,
    }
