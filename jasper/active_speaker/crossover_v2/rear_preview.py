# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Rear documents evaluated on a banked pair, without playback or banking."""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.measurement_programs import PURPOSE_REAR
from jasper.active_speaker.rear_calibration import read_rear_calibration, rear_operating_facts
from jasper.audio_measurement import rear_evidence as figures
from jasper.audio_measurement.analysis import band_levels_from_magnitude, smooth_fractional_octave

from .pose_curve import lateral_evidence_grid_hz, nearest_native_bins
from .rear_views import PairTake, front_on_axis, pair_takes, rear_document
from .room_selection import purpose_take_records
from .round_captures import RoundCapturesRefused
from .round_inputs import RoundInputs

REAR_PREVIEW_NEEDS_PAIR_ROUND = "rear_preview_needs_pair_round"


def rear_compare_delta_db(preview: Mapping[str, Any]) -> float | None:
    """Front on minus off: 10*log10(mean(10**(delta/10))) on 1024 log bins
    from 40 Hz to 16 kHz (upper endpoint excluded). Delta is change_db plus
    relative_charge inside the curve's band, and 0 dB outside it.
    """
    for key, row in preview["positions"].items():
        if front_on_axis(key, row["pose_kind"]):
            curve = row["curve"]
            grid = np.geomspace(40.0, 16000.0, 1024, endpoint=False)
            change = np.asarray(curve["change_db"]) + preview["stage"]["relative_charge"]
            delta = np.interp(grid, curve["freqs_hz"], change, left=0.0, right=0.0)
            return band_levels_from_magnitude(grid, delta, [(40.0, 16000.0)])[0]
    return None


def summary_rows(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {"headroom_charge_db": preview["stage"]["headroom_charge_db"], "positions": {
        key: {"trough_fill_db": row["trough_fill_db"],
              "gradient_residual_db": row["gradient_residual"]["db"],
              **{name: row["late_energy"][name] for name in ("early_late_change_db", "arrival_shift_ms")},
              "bands": {f"{band['band_hz'][0]:g}-{band['band_hz'][1]:g}": band["change_db"]
                        for band in row["bands"]}}
        for key, row in preview["positions"].items()
    }}


def _rounded(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_rounded(item) for item in value]
    return round(float(value), 3) if isinstance(value, (float, np.floating)) else value


def _position(takes: list[PairTake], row: Mapping[str, Any],
              section: Mapping[str, Any], charge_delta: float) -> dict[str, Any]:
    take = takes[0]
    grid = take.freqs_hz
    rear, front = rear_stage_response(section, grid)
    coverage, pair_band = row["coverage_hz"], row["band_hz"]
    operating = rear_operating_facts(section)
    band = operating["band_hz"] if operating["band_hz"] is not None else pair_band
    if band is not None:
        low, high = max(band[0], coverage[0]), min(band[1], coverage[1])
        band = [low, high] if low < high else None
    bins = nearest_native_bins(grid, lateral_evidence_grid_hz())
    freqs = grid[bins]
    # See ADR-0325: the reference carries this document's front chain.
    muted = [take.front * front for take in takes]
    predicted = [reference + take.rear * rear for take, reference in zip(takes, muted)]
    muted_db, predicted_db = (np.mean([figures.magnitude_db(tf[bins]) for tf in repeats], axis=0)
                              for repeats in (muted, predicted))
    reference = figures.reference_curve_db(freqs, muted_db)
    symptoms = {name: figures.position_figures(
        freqs, curve, reference_db=reference, band_hz=band, coverage_hz=coverage,
        handover_hz=operating["handover_hz"],
    ) for name, curve in (("muted", muted_db), ("predicted", predicted_db))}
    dip = symptoms["muted"]["dip"]
    muted_db, predicted_db = (smooth_fractional_octave(
        freqs, curve, fraction=figures.FIGURE_FRACTION) for curve in (muted_db, predicted_db))
    change = predicted_db - muted_db - charge_delta
    front_db = figures.magnitude_db(front)
    bands = []
    for low, high in figures.LEVEL_BANDS_HZ:
        electrical = band_levels_from_magnitude(grid, front_db, [(low, high)])[0]
        covered = coverage[0] <= low and high <= coverage[1]
        muted_level = predicted_level = None
        if covered:
            muted_level = band_levels_from_magnitude(freqs, muted_db, [(low, high)])[0]
            predicted_level = band_levels_from_magnitude(freqs, predicted_db, [(low, high)])[0]
        bands.append({"band_hz": [low, high], "muted_db": muted_level, "predicted_db": predicted_level,
                      "change_db": None if muted_level is None or predicted_level is None else predicted_level - muted_level - charge_delta,
                      "front_chain_db": electrical, "reason": "" if covered else figures.REASON_COVERAGE_SHORT})
    late_covered = coverage[0] <= figures.LATE_ENERGY_BAND_HZ[0] and figures.LATE_ENERGY_BAND_HZ[1] <= coverage[1]
    energies = [[figures.impulse_energy_figures(
        figures.band_limited_impulse(grid, tf, figures.LATE_ENERGY_BAND_HZ),
        sample_rate_hz=take.sample_rate_hz,
    ) for tf in pair] for pair in zip(muted, predicted)] if late_covered else []
    late = {"band_hz": figures.LATE_ENERGY_BAND_HZ,
            "reason": "" if late_covered else figures.REASON_COVERAGE_SHORT,
            **{label: float(np.median([after[key] - before[key] for before, after in energies]))
               if energies else None for label, key in figures.LATE_ENERGY_CHANGE_KEYS}}
    gap = figures.confident_arrival_gap_s(row["arrival_gap"])
    keep = (grid >= coverage[0]) & (grid <= min(coverage[1], 5000.0))
    display = (freqs >= coverage[0]) & (freqs <= min(coverage[1], 5000.0))
    return {**{key: row[key] for key in ("coverage_hz", "band_hz", "arrival_gap", "superposition_residual_db", "reason")},
            "pose_kind": take.pose_kind, "repeats": len(takes), "figures": symptoms, "figures_band_hz": band,
            "trough_fill_db": None if dip is None else change[np.argmin(np.abs(freqs - dip["hz"]))],
            "bands": bands, "late_energy": late,
            "gradient_residual": {"db": None if not np.all(front[keep]) else figures.gradient_residual_db(
                grid[keep], rear[keep] / front[keep], gap, pair_band),
                "reason": figures.REASON_GAP_NOT_CONFIDENT if gap is None else ""},
            "curve": {"freqs_hz": freqs[display], "change_db": change[display]}}


def preview_rear_section(section: Mapping[str, Any], *, inputs: RoundInputs,
                         manifest: Mapping[str, Any]) -> dict[str, Any]:
    takes = pair_takes(record for _, record in purpose_take_records(inputs.session_dir, purpose=PURPOSE_REAR))
    if not takes:
        raise RoundCapturesRefused(REAR_PREVIEW_NEEDS_PAIR_ROUND, {})
    validated = read_rear_calibration(section, sample_rate=takes[0].sample_rate_hz)
    view = rear_document(inputs, manifest=manifest)
    grouped: dict[str, list[PairTake]] = {}
    for take in takes:
        grouped.setdefault(take.pose_key, []).append(take)
    charge = rear_branch_sum_headroom_db(validated)
    relative_charge = charge - rear_branch_sum_headroom_db({**validated, "rear_muted": True})
    positions = {key: _position(grouped[key], row, validated, relative_charge)
                 for key, row in view["pair"]["positions"].items() if key in grouped}
    return _rounded({"reason": "", "stage": {
        "headroom_charge_db": charge, "relative_charge": relative_charge, **rear_operating_facts(validated),
        "round_id": inputs.session_dir.name, "pair_candidate_id": view["pair"]["candidate_id"],
    }, "positions": positions})
