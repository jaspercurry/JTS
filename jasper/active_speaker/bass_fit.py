# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any, NamedTuple

import numpy as np
from scipy.optimize import least_squares

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.band_ladders import BASS_FIT_REFERENCE_BAND_HZ
from jasper.audio_measurement.evidence_reasons import REASON_COVERAGE_SHORT, unavailable
from jasper.audio_measurement.trusted_band import TrustedBand, within_trusted

from .bass_comparison import CHANGE_FIELDS, COMPARISON_FIELDS, bass_capture_context, common_bass_bins
from .crossover_v2.measurement_context import compare_capture_basis
from .crossover_v2.round_captures import doc_pose_key
from .crossover_v2.refusal_copy import CrossoverV2Refused
from .measurement_bass import BASS_BANDS_HZ

REFERENCE_BAND_HZ = BASS_FIT_REFERENCE_BAND_HZ
BASS_GRID_POINTS = 121


def aligned_bass_pair(
    before: Mapping[str, Any], after: Mapping[str, Any], grid: np.ndarray,
    reference_band_hz: tuple[float, float],
) -> tuple[float, list[np.ndarray]]:
    curve = before["frequency_curve"]
    rf, ry = np.asarray(curve["freqs_hz"]), np.asarray(curve["magnitude_db"], dtype=float)
    anchor = (rf >= reference_band_hz[0]) & (rf <= reference_band_hz[1]) & np.isfinite(ry)
    if (not before["sweep_band_hz"][0] <= reference_band_hz[0] < reference_band_hz[1] <= before["sweep_band_hz"][1]
            or not anchor.any()):
        raise CrossoverV2Refused(code="bass_fit_reference_band_unavailable")
    reference = float(np.median(ry[anchor]))
    probe = {"freqs_hz": grid, "fundamental_db": np.zeros(grid.size), "fundamental_qualified": np.ones(grid.size)}
    curves = []
    for take in (before, after):
        f, _, y = common_bass_bins(probe, take, "fundamental_db", "fundamental_qualified")
        values = np.full(grid.shape, np.nan)
        values[np.searchsorted(grid, f)] = y - reference
        curves.append(values)
    return reference, curves


def smooth_bass_curve(grid: np.ndarray, values: np.ndarray) -> np.ndarray:
    indices = np.flatnonzero(np.isfinite(values))
    # A qualification gap cannot contribute power to either neighbouring region.
    for section in np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1):
        if section.size:
            values[section] = smooth_fractional_octave(grid[section], values[section], fraction=3)
    return values


def _median_curves(curves):
    values = np.asarray(curves)
    valid = np.isfinite(values).all(axis=0)
    result = np.full(values.shape[1], np.nan)
    result[valid] = np.median(values[:, valid], axis=0)
    return result


def fit_bass_shape(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    candidate_id: str, reference_band_hz: tuple[float, float] = REFERENCE_BAND_HZ,
) -> dict[str, Any]:
    if not pairs or not 0 < reference_band_hz[0] < reference_band_hz[1]:
        raise CrossoverV2Refused(code="bass_fit_inputs_missing")
    grid = np.geomspace(BASS_BANDS_HZ[0][0], BASS_BANDS_HZ[-1][1], BASS_GRID_POINTS)
    groups, curves, shared_curves = defaultdict(list), defaultdict(list), defaultdict(list)
    sources = []
    first = bass_capture_context(pairs[0][0])
    for before, after in pairs:
        if not before["record"].get("candidate_id") or after["record"].get("candidate_id") != candidate_id:
            raise CrossoverV2Refused(code="bass_fit_requires_room_baseline_and_exact_candidate")
        context = bass_capture_context(before)
        match = compare_capture_basis(bass_capture_context(after), context, interventions=CHANGE_FIELDS["candidate"],
                                      required=tuple(key for key in COMPARISON_FIELDS if key not in CHANGE_FIELDS["candidate"]))
        across = compare_capture_basis(context, first, interventions=("pose_key",),
                                       required=tuple(key for key in first if key != "pose_key"))
        if match["incompatible_fields"] or across["incompatible_fields"]:
            raise CrossoverV2Refused({"pair": match, "across": across}, code="bass_fit_capture_context_changed")
        pose = doc_pose_key(before["record"])
        if before["record"].get("position_deg") is None and before["record"].get("seat_offset_m") is None:
            raise CrossoverV2Refused(code="bass_fit_pose_missing")
        reference, aligned = aligned_bass_pair(before, after, grid, reference_band_hz)
        shared = np.isfinite(aligned).all(axis=0)
        shared_curves[pose].append([smooth_bass_curve(grid, np.where(shared, curve, np.nan)) for curve in aligned])
        curves[pose].append([smooth_bass_curve(grid, curve) for curve in aligned])
        groups[pose].append((before, after))
        sources.append({"before": before["record_path"], "after": after["record_path"],
                        "reference_db": reference, "comparison": match, "across_positions": across})
    # Equal pose weight: repeated measurements estimate variation, not more seats.
    responses = [_median_curves([_median_curves(np.asarray(repeats)[:, side]) for repeats in curves.values()])
                 for side in (0, 1)]
    delta = _median_curves([_median_curves(np.asarray(repeats)[:, 1]) - _median_curves(np.asarray(repeats)[:, 0])
                            for repeats in shared_curves.values()])
    return {"freqs_hz": grid, "groups": groups, "curves": curves, "sources": sources,
            "base": responses[0], "candidate": responses[1], "delta": delta,
            "reference_band_hz": reference_band_hz}


class SealedFit(NamedTuple):
    corner_hz: float
    q: float
    residual_db: float
    level_db: float
    model_db: np.ndarray
    #: Per parameter (level, corner, Q): -1 on its lower bound, 1 on its upper, 0 inside (``least_squares``).
    active_mask: np.ndarray


def sealed_fit(freqs: np.ndarray, y_db: np.ndarray, lo: float, hi: float) -> SealedFit:
    """2nd-order high-pass fit of a level curve over lo..hi Hz: its corner, Q, rms
    miss, passband level, the fitted curve at the bins it read, and which
    parameters stopped on a bound."""
    sel = (freqs >= lo) & (freqs <= hi)

    def model(p):
        s = 1j * freqs[sel] / p[1]
        return p[0] + 20 * np.log10(np.abs(s * s / (s * s + s / p[2] + 1)))

    fit = least_squares(lambda p: model(p) - y_db[sel], x0=(np.median(y_db[sel]), 70.0, 0.7),
                        bounds=((-300, 20, 0.3), (300, 200, 3.0)))
    return SealedFit(float(fit.x[1]), float(fit.x[2]), float(np.sqrt(np.mean(fit.fun ** 2))), float(fit.x[0]),
                     y_db[sel] + fit.fun, fit.active_mask)


def bass_alignment(freqs_hz: np.ndarray, level_db: np.ndarray, band_hz: Sequence[float],
                   trusted: TrustedBand) -> dict[str, Any]:
    """The sealed-box alignment one curve fits over ``band_hz`` clipped to its
    trusted band (ADR-0366): the Linkwitz transform's ``source_hz`` and
    ``source_q`` (ADR-0359), the fit's rms miss and the bins it read. A band
    left with fewer bins than the fit's three parameters, a corner or Q stopped
    on the fit's bound, or bins that do not straddle the corner cannot place
    it: its coverage gap, naming the trusted band that clipped the band."""
    freqs, level = np.asarray(freqs_hz, dtype=float), np.asarray(level_db, dtype=float)
    within = within_trusted(band_hz, trusted)
    read = (np.isfinite(level) & (freqs >= within[0]) & (freqs <= within[1]) if within
            else np.zeros(freqs.shape, dtype=bool))
    gap = {"band_hz": list(band_hz), "trusted_band": asdict(trusted),
           "curve_hz": [float(freqs.min()), float(freqs.max())] if freqs.size else None}
    if np.count_nonzero(read) < 3:
        return unavailable(REASON_COVERAGE_SHORT, gap)
    freqs, level = freqs[read], level[read]
    fit = sealed_fit(freqs, level, freqs.min(), freqs.max())
    at_bound = [name for name, active in zip(("source_hz", "source_q"), fit.active_mask[1:]) if active]
    if at_bound or not freqs.min() < fit.corner_hz < freqs.max():
        return unavailable(REASON_COVERAGE_SHORT, {**gap, "source_hz": round(fit.corner_hz, 1),
                                                   "source_q": round(fit.q, 2), "at_bound": at_bound})
    return {"status": "available", "band_hz": [round(float(freqs.min()), 2), round(float(freqs.max()), 2)],
            "source_hz": round(fit.corner_hz, 1), "source_q": round(fit.q, 2), "residual_db": round(fit.residual_db, 2),
            "level_db": round(fit.level_db, 2), "freqs_hz": freqs.round(3).tolist(),
            "measured_db": level.round(3).tolist(), "model_db": fit.model_db.round(3).tolist()}
