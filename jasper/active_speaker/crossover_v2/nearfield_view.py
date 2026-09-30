# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A round's one-driver takes, near-field or gated far-field, read band by
band, and their distance self-test: a pure view of banked evidence (ADR-0346,
ADR-0360).

Each kept take banks its first sweep's curve with the other sweeps nested
beside it. The first sweep against the others shows an amplifier waking late
(#5684); the last two sweeps against each other give the band's SNR. Per
driver, placements key by distance and pose kind, so a take in front and one
behind stay apart, and the level step between two distances of one kind is
held to a rigid piston of the declared cone.

A take's curve already has its sweep's digital gain divided out; its raw
curve also has the fader and the played graph divided out, so every driver
reads on one digital reference: a unit program sample through a unity path.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from itertools import combinations
from types import MappingProxyType
from typing import Any

import numpy as np

from jasper.audio_measurement.band_ladders import NEAR_FIELD_BANDS_HZ
from jasper.audio_measurement.level import piston_step_db
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.series_stats import power_mean_across_db, power_mean_db
from jasper.audio_measurement.trusted_band import TrustedBand, within_trusted

from ..graph_transfer import GraphTransferError, complex_channel_transfer
from .position_cycle import OWN_WINDOW, curve_band, take_curve
from .spatial import MARK_DISTANCE_M

#: Where the distance step is read: above a port, below cone breakup (#5684),
#: and inside both placements' trusted bands (ADR-0366).
STEP_BAND_HZ = (35.0, 400.0)
#: The piston runs 0.15-0.3 dB short of jts3's measured 15 -> 30 mm step (#5684).
STEP_TOLERANCE_DB = 0.4


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
    """The curve's grid over its sweep's band, and one row of magnitude per
    sweep, first sweep first; outside its sweep a curve is noise."""
    rows = [curve, *curve.get("repeat_curves", ())]
    freqs = np.asarray(curve["freqs_hz"], dtype=float)
    swept = (freqs >= curve["band_hz"][0]) & (freqs <= curve["band_hz"][1])
    return freqs[swept], np.asarray([row["magnitude_db"] for row in rows], dtype=float)[:, swept]


def _gate(curve: Mapping[str, Any]) -> dict[str, Any]:
    """The gate a take's sweeps ran (ADR-0383 §2): gated only if every sweep
    was, at the shortest window, whose floors are the highest."""
    sweeps = [curve, *curve.get("repeat_curves", ())]
    gated = all(sweep.get("window") == "gated" for sweep in sweeps)
    shortest = min(sweeps, key=lambda sweep: sweep["gate_window_ms"]) if gated else {}
    return {"window": "gated" if gated else "ungated", "window_ms": shortest.get("gate_window_ms"),
            **{key: shortest.get(key) for key in ("validity_floor_hz", "trusted_floor_hz", "floor_source")}}


def _within(freqs: np.ndarray, sweeps: np.ndarray, band_hz: tuple[float, float],
            swept_hz: tuple[float, float]) -> np.ndarray:
    """The sweeps' bins in ``band_hz``; none unless the take swept all of it."""
    if not (swept_hz[0] <= band_hz[0] and band_hz[1] <= swept_hz[1]):
        return sweeps[:, :0]
    return sweeps[:, (freqs >= band_hz[0]) & (freqs < band_hz[1])]


def _inside(band_hz: tuple[float, float], trusted: TrustedBand) -> bool:
    return (trusted.low_hz or 0.0) <= band_hz[0] and band_hz[1] <= (trusted.high_hz or math.inf)


def _band(freqs: np.ndarray, sweeps: np.ndarray, band_hz: tuple[float, float],
          swept_hz: tuple[float, float], trusted: TrustedBand) -> dict[str, Any] | None:
    within = _within(freqs, sweeps, band_hz, swept_hz)
    if not within.size:
        return None
    row: dict[str, Any] = {"band_hz": list(band_hz), "level_db": round(power_mean_db(within), 2),
                           "first_minus_rest_db": None, "snr_db": None, "trusted": False}
    if len(within) > 1:
        row["first_minus_rest_db"] = round(power_mean_db(within[0]) - power_mean_db(within[1:]), 2)
        rms = float(np.sqrt(np.mean((within[-1] - within[-2]) ** 2)))
        if rms > 0.0:
            row["snr_db"] = round(20.0 * math.log10(20.0 / math.log(10.0) / rms), 1)
            row["trusted"] = row["snr_db"] >= DRIVER.snr_warn_db and _inside(band_hz, trusted)
    return row


def nearest_raw(driver: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """A view driver's placement nearest its cone that has a raw curve: the
    curve the cabinet model and the alignment fit read."""
    return next((placement for placement in driver.get("placements", ()) if placement["raw"]), None)


def nearfield_view(
    takes: Iterable[Mapping[str, Any]], *, radiating_diameter_mm_by_target: Mapping[str, float],
    played_graphs: Mapping[str, Mapping[str, Any]] = MappingProxyType({}),
) -> dict[str, Any]:
    """The kept one-driver takes of a round's run manifest, each read as its
    record (its driver's curve, purpose, level and verdict), band by band, and
    each driver's placements, raw curves and distance steps. Each placement
    states the trusted band its take banked on the curve read (ADR-0366 §3).
    ``played_graphs`` is each take's played CamillaDSP config by take id; a
    take without a graph the walker can model, without its fader, or on
    another frequency grid than its placement's first stays out of the raw
    curve."""
    rows: list[dict[str, Any]] = []
    reads: list[tuple[np.ndarray, np.ndarray, tuple[float, float]]] = []
    take_bands: list[TrustedBand] = []
    raw_rows: list[tuple[np.ndarray, np.ndarray] | None] = []
    placed: dict[str, dict[tuple[float, str], list[int]]] = {}
    for take in takes:
        driver = (take.get("pose") or {}).get("driver")
        if not (take.get("selected") and driver and (curve := take_curve(take, driver, OWN_WINDOW))):
            continue
        freqs, sweeps = _sweeps(curve)
        swept = curve["band_hz"]
        stated_m = take["pose"].get("distance_m")
        # A pose at the mark banks no distance of its own.
        distance_m = MARK_DISTANCE_M if stated_m is None else float(stated_m)
        trusted = curve_band(take, curve)
        graph, fader_db = played_graphs.get(take["take_id"]), (take.get("level") or {}).get("level_db")
        path_db = None if graph is None or fader_db is None else played_path_db(graph, freqs)
        # The first sweep can catch an amplifier still waking (#5684).
        raw_rows.append(None if path_db is None else
                        (freqs, (sweeps[1:] if len(sweeps) > 1 else sweeps) - fader_db - path_db))
        row = {"take_id": take["take_id"], "driver": driver, "distance_mm": round(distance_m * 1000.0, 1),
               "kind": take["pose"].get("kind"), "gate": _gate(curve),
               "level_db_spl": ((take.get("verdict") or {}).get("evidence") or {}).get("level_db_spl"),
               "bands": [band for edges in NEAR_FIELD_BANDS_HZ
                         if (band := _band(freqs, sweeps, edges, swept, trusted)) is not None]}
        placed.setdefault(driver, {}).setdefault((row["distance_mm"], row["kind"]), []).append(len(rows))
        rows.append(row)
        reads.append((freqs, sweeps, swept))
        take_bands.append(trusted)

    def level_over(indexes: list[int], band_hz: tuple[float, float]) -> float | None:
        """A placement's level over ``band_hz``, from its takes that swept all of it."""
        heard = [power_mean_db(within) for freqs, sweeps, swept in (reads[index] for index in indexes)
                 if (within := _within(freqs, sweeps, band_hz, swept)).size]
        return power_mean_db(np.asarray(heard)) if heard else None

    drivers = []
    for driver, at in sorted(placed.items()):
        diameter = radiating_diameter_mm_by_target.get(driver)
        placements = []
        for (distance_mm, kind), indexes in sorted(at.items()):
            levels = [[band["level_db"] for band in rows[index]["bands"]] for index in indexes]
            spread = (np.ptp(np.asarray(levels), axis=0).round(2).tolist()
                      if len(levels) > 1 and len({len(one) for one in levels}) == 1 else None)
            unplayed = [(rows[index]["take_id"], *one) for index in indexes if (one := raw_rows[index]) is not None]
            unplayed = [one for one in unplayed if np.array_equal(one[1], unplayed[0][1])]
            raw = {"take_ids": [take_id for take_id, _, _ in unplayed],
                   "freqs_hz": unplayed[0][1].round(3).tolist(),
                   "level_db": power_mean_across_db(np.vstack([db for _, _, db in unplayed])).round(3).tolist(),
                   } if unplayed else None
            placements.append({"distance_mm": distance_mm, "kind": kind, "trusted_band": asdict(take_bands[indexes[0]]),
                               "take_ids": [rows[index]["take_id"] for index in indexes],
                               "reseat_spread_db": spread, "raw": raw})
        steps = []
        for (near_mm, kind), (far_mm, far_kind) in combinations(sorted(at), 2):
            if kind != far_kind:
                continue
            near_at, far_at = at[near_mm, kind], at[far_mm, kind]
            # With no band left inside both placements' trusted bands, the step is stated, never graded.
            step_band = within_trusted(STEP_BAND_HZ, take_bands[near_at[0]], take_bands[far_at[0]])
            near, far = (level_over(near_at, step_band), level_over(far_at, step_band)) if step_band else (None, None)
            if step_band and (near is None or far is None):
                continue
            measured = None if near is None or far is None else round(far - near, 2)
            piston = (round(piston_step_db(near_mm / 1000.0, far_mm / 1000.0, diameter / 2000.0), 2)
                      if diameter else None)
            steps.append({"near_mm": near_mm, "far_mm": far_mm, "step_band_hz": list(step_band) if step_band else None,
                          "step_db": measured, "piston_db": piston,
                          "verdict": "not_evaluated" if piston is None or measured is None else
                          "pass" if abs(measured - piston) <= STEP_TOLERANCE_DB else "fail"})
        drivers.append({"driver": driver, "radiating_diameter_mm": diameter, "placements": placements,
                        "steps": steps})
    return {
        "parameters": {"ladder": "near_field", "trusted_snr_db": DRIVER.snr_warn_db,
                       "step_tolerance_db": STEP_TOLERANCE_DB},
        "takes": rows, "drivers": drivers,
    }
