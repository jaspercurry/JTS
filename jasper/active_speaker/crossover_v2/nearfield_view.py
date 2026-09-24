# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A round's near-field driver takes read band by band, and their distance
self-test: a pure view of banked evidence (ADR-0346, ADR-0360).

Each kept take banks its first sweep's curve with the other sweeps nested
beside it. The first sweep against the others shows an amplifier waking late
(#5684); the last two sweeps against each other give the band's SNR. Per
driver, the level step between two distances is held to a rigid piston of the
declared cone.

A take's curve already has its sweep's digital gain divided out; its raw
curve also has the fader and the played graph divided out, so every driver
reads on one digital reference: a unit program sample through a unity path.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from itertools import combinations
from types import MappingProxyType
from typing import Any

import numpy as np

from jasper.audio_measurement.band_ladders import NEAR_FIELD_BANDS_HZ
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.series_stats import power_mean_across_db, power_mean_db
from jasper.speaker_layout import measurement_target_parts

from ..graph_transfer import GraphTransferError, complex_channel_transfer

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


def played_path_db(config: Mapping[str, Any], freqs_hz: np.ndarray) -> np.ndarray | None:
    """The played graph's level from program channel 0 to its loudest output,
    which on a one-driver graph is the target's: every other output is parked.
    ``None`` for a graph the shared walker cannot model."""
    try:
        outputs = complex_channel_transfer(
            config, freqs_hz, input_weights={0: 1.0},
            output_channels={channel: channel for channel in range(config["devices"]["playback"]["channels"])},
            allow_limiter_passthrough=True, dynamic_bass_at_rest=True)
    except (KeyError, TypeError, ValueError, GraphTransferError):
        return None
    return 20.0 * np.log10(np.abs(max(outputs.values(), key=lambda path: float(np.sum(np.abs(path) ** 2)))))


def _sweeps(curve: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """The curve's grid, and one row of magnitude per sweep, first sweep first."""
    rows = [curve, *curve.get("repeat_curves", ())]
    return (np.asarray(curve["freqs_hz"], dtype=float),
            np.asarray([row["magnitude_db"] for row in rows], dtype=float))


def _within(freqs: np.ndarray, sweeps: np.ndarray, band_hz: tuple[float, float]) -> np.ndarray:
    return sweeps[:, (freqs >= band_hz[0]) & (freqs < band_hz[1])]


def _band(freqs: np.ndarray, sweeps: np.ndarray, band_hz: tuple[float, float]) -> dict[str, Any] | None:
    within = _within(freqs, sweeps, band_hz)
    if not within.size:
        return None
    row: dict[str, Any] = {"band_hz": list(band_hz), "level_db": round(power_mean_db(within), 2),
                           "first_minus_rest_db": None, "snr_db": None, "trusted": False}
    if len(within) > 1:
        row["first_minus_rest_db"] = round(power_mean_db(within[0]) - power_mean_db(within[1:]), 2)
        rms = float(np.sqrt(np.mean((within[-1] - within[-2]) ** 2)))
        if rms > 0.0:
            row["snr_db"] = round(20.0 * math.log10(20.0 / math.log(10.0) / rms), 1)
            row["trusted"] = row["snr_db"] >= DRIVER.snr_warn_db
    return row


def nearfield_view(
    takes: Iterable[Mapping[str, Any]], *, radiating_diameter_mm_by_role: Mapping[str, float],
    played_graphs: Mapping[str, Mapping[str, Any]] = MappingProxyType({}),
) -> dict[str, Any]:
    """The kept near-field takes of a round's run manifest, band by band, and
    each driver's placements, raw curves and distance steps. ``played_graphs``
    is each take's played CamillaDSP config by take id; a take without a
    graph the walker can model, without its fader, or on another frequency
    grid than its placement's first stays out of the raw curve."""
    rows: list[dict[str, Any]] = []
    step_levels: list[float | None] = []
    raw_rows: list[tuple[np.ndarray, np.ndarray] | None] = []
    placed: dict[str, dict[float, list[int]]] = {}
    for take in takes:
        if not (take.get("selected") and (take.get("pose") or {}).get("driver") and take.get("curve")):
            continue
        freqs, sweeps = _sweeps(take["curve"])
        step = _within(freqs, sweeps, STEP_BAND_HZ)
        graph, fader_db = played_graphs.get(take["take_id"]), (take.get("level") or {}).get("level_db")
        path_db = None if graph is None or fader_db is None else played_path_db(graph, freqs)
        # The first sweep can catch an amplifier still waking (#5684).
        raw_rows.append(None if path_db is None else
                        (freqs, (sweeps[1:] if len(sweeps) > 1 else sweeps) - fader_db - path_db))
        row = {"take_id": take["take_id"], "driver": take["pose"]["driver"],
               "distance_mm": round(float(take["pose"]["distance_m"]) * 1000.0, 1),
               "max_window_db_spl": ((take.get("quality") or {}).get("evidence") or {}).get("max_window_db_spl"),
               "bands": [band for edges in NEAR_FIELD_BANDS_HZ if (band := _band(freqs, sweeps, edges)) is not None]}
        placed.setdefault(row["driver"], {}).setdefault(row["distance_mm"], []).append(len(rows))
        rows.append(row)
        step_levels.append(power_mean_db(step) if step.size else None)
    drivers = []
    for driver, at in sorted(placed.items()):
        diameter = radiating_diameter_mm_by_role.get(measurement_target_parts(driver)[0])
        placements, step_level_at = [], {}
        for distance_mm, indexes in sorted(at.items()):
            levels = [[band["level_db"] for band in rows[index]["bands"]] for index in indexes]
            spread = (np.ptp(np.asarray(levels), axis=0).round(2).tolist()
                      if len(levels) > 1 and len({len(one) for one in levels}) == 1 else None)
            unplayed = [(rows[index]["take_id"], *one) for index in indexes if (one := raw_rows[index]) is not None]
            unplayed = [one for one in unplayed if np.array_equal(one[1], unplayed[0][1])]
            raw = {"take_ids": [take_id for take_id, _, _ in unplayed],
                   "freqs_hz": unplayed[0][1].round(3).tolist(),
                   "level_db": power_mean_across_db(np.vstack([db for _, _, db in unplayed])).round(3).tolist(),
                   } if unplayed else None
            placements.append({"distance_mm": distance_mm, "take_ids": [rows[index]["take_id"] for index in indexes],
                               "reseat_spread_db": spread, "raw": raw})
            heard = [level for index in indexes if (level := step_levels[index]) is not None]
            step_level_at[distance_mm] = power_mean_db(np.asarray(heard)) if heard else None
        steps = []
        for near_mm, far_mm in combinations(sorted(at), 2):
            near, far = step_level_at[near_mm], step_level_at[far_mm]
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
        "parameters": {"ladder": "near_field", "trusted_snr_db": DRIVER.snr_warn_db,
                       "step_band_hz": list(STEP_BAND_HZ), "step_tolerance_db": STEP_TOLERANCE_DB},
        "takes": rows, "drivers": drivers,
    }
