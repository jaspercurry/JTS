# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Greedy peak-fit parametric-EQ designer for the modal range.

Defaults follow the known-good REW workflow: 20 Hz to the room-correction
ceiling (owned by jasper.audio_measurement.room_boundary, 350 Hz today), at
most 5 filters, cuts only, -10 dB max cut, +3 dB per-filter boost when
``cuts_only=False``, Q in [1.0, 8.0]. Each filter maps 1:1 to a CamillaDSP
``Biquad {type: Peaking, freq, q, gain}``; biquad coefficients are CamillaDSP's
job at config-load time, not this module's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from jasper.audio_measurement.room_boundary import ROOM_BOUNDARY_DEFAULT_HZ
from jasper.camilla_config_contract import total_positive_boost_db


@dataclass(frozen=True)
class PEQ:
    """A single peaking-EQ filter (parametric EQ biquad).

    Maps 1:1 to a CamillaDSP `Biquad / Peaking` filter.
    """
    freq: float    # Hz — the bell's center frequency
    q: float       # quality factor; ~1 ≈ octave wide, ~8 ≈ 1/8 oct
    gain: float    # dB — negative = cut, positive = boost


def _bell_response_db(
    eval_freqs: np.ndarray,
    fc: float,
    q: float,
    gain_db: float,
) -> np.ndarray:
    """Approximate magnitude response of a peaking bell, in dB.

    A Lorentzian in log-frequency: ``gain_db / (1 + (delta_oct / bw)**2)`` with
    ``bw = asinh(1/(2Q)) / ln(2)``, the RBJ peaking half-bandwidth in octaves,
    so the model's half-gain width matches the biquad CamillaDSP realizes. The
    far skirts stay an approximation; the half-width is what the greedy
    residual subtraction needs to pick a sensible next peak.
    """
    if fc <= 0:
        return np.zeros_like(eval_freqs)
    omega = eval_freqs / fc
    # Avoid log of 0 / negative
    safe = np.where(omega > 0, omega, 1.0)
    delta_oct = np.log2(safe)
    # RBJ peaking-EQ half-bandwidth (octaves) for this Q; max() guards q -> 0.
    bw = math.asinh(1.0 / (2.0 * max(q, 1e-3))) / math.log(2.0)
    response = gain_db / (1.0 + (delta_oct / bw) ** 2)
    response[omega <= 0] = 0.0
    return response


def _estimate_q(
    band_freqs: np.ndarray,
    band_residual_db: np.ndarray,
    peak_idx: int,
    *,
    q_min: float,
    q_max: float,
) -> float:
    """Estimate Q from the -3 dB width around a peak.

    ``Q = fc / bandwidth``. Returns 2.0 (~half-octave) when the peak is under
    3 dB and the -3 dB rule does not apply.
    """
    peak_db = band_residual_db[peak_idx]
    abs_peak = abs(peak_db)
    if abs_peak < 3.0:
        # The only return path that does NOT clip to [q_min, q_max]. Inert
        # while every caller's floor is below 2.0, but a floor raised above 2.0
        # would be silently violated here, and since #1967 that floor bounds a
        # safety property.
        return 2.0

    threshold = abs_peak - 3.0
    n = len(band_residual_db)

    lower = peak_idx
    while lower > 0 and abs(band_residual_db[lower]) > threshold:
        lower -= 1
    upper = peak_idx
    while upper < n - 1 and abs(band_residual_db[upper]) > threshold:
        upper += 1

    f_lower = band_freqs[lower]
    f_upper = band_freqs[upper]
    bandwidth = f_upper - f_lower
    fc = band_freqs[peak_idx]

    if bandwidth <= 0:
        return float(np.clip(4.0, q_min, q_max))
    return float(np.clip(fc / bandwidth, q_min, q_max))


def design_peq(
    measured_db: np.ndarray,
    target_db: np.ndarray,
    freqs: np.ndarray,
    *,
    f_low: float = 20.0,
    f_high: float = ROOM_BOUNDARY_DEFAULT_HZ,
    max_filters: int = 5,
    max_cut_db: float | np.ndarray = -10.0,
    max_boost_db: float = 3.0,
    cuts_only: bool = True,
    flatness_target_db: float = 1.0,
    q_min: float = 1.0,
    q_max: float = 8.0,
    min_filter_gain_db: float = 0.5,
) -> list[PEQ]:
    """Greedy peak-fit PEQ designer.

    ``measured_db`` / ``target_db`` are dB on the strictly increasing ``freqs``
    grid; no filter is placed outside ``[f_low, f_high]``. ``max_cut_db`` is
    either a scalar per-filter floor or an array on ``freqs`` (the per-bin
    linearization envelope), interpolated at each candidate peak so it need not
    share the grid; ``max_boost_db`` is always a scalar. ``cuts_only`` fits
    only negative gains. Design stops at ``max_filters`` or when residual RMS
    in band drops below ``flatness_target_db``, and a filter whose absolute
    gain would fall below ``min_filter_gain_db`` is not added.

    Returns the PEQs in the order they were added (largest impact first).
    """
    if len(measured_db) != len(target_db) or len(measured_db) != len(freqs):
        raise ValueError(
            f"length mismatch: measured={len(measured_db)} "
            f"target={len(target_db)} freqs={len(freqs)}"
        )
    if f_high <= f_low:
        raise ValueError(f"f_high ({f_high}) must be > f_low ({f_low})")
    max_cut_is_array = isinstance(max_cut_db, np.ndarray)
    if max_cut_is_array and max_cut_db.shape != np.shape(freqs):
        raise ValueError(
            f"max_cut_db array shape {max_cut_db.shape} does not match "
            f"freqs shape {np.shape(freqs)}"
        )

    band_mask = (freqs >= f_low) & (freqs <= f_high)
    if not band_mask.any():
        return []

    # Work on a copy — design_peq is pure with respect to its inputs.
    residual = (measured_db - target_db).astype(np.float64).copy()
    peqs: list[PEQ] = []

    band_freqs = freqs[band_mask]

    for _ in range(max_filters):
        band_residual = residual[band_mask]

        # cuts_only -> consider only positive excursions; else absolute peak.
        if cuts_only:
            search = np.where(band_residual > 0, band_residual, 0.0)
        else:
            search = np.abs(band_residual)
        peak_idx = int(np.argmax(search))
        peak_db = float(band_residual[peak_idx])

        # Stop early only when BOTH the band RMS is low and no narrow peak
        # remains: RMS alone would miss a sharp narrow mode.
        rms = float(np.sqrt(np.mean(band_residual ** 2)))
        if rms < flatness_target_db and abs(peak_db) < flatness_target_db * 2:
            break

        if cuts_only and peak_db <= 0:
            break
        if abs(peak_db) < min_filter_gain_db:
            break

        peak_freq = float(band_freqs[peak_idx])
        q_est = _estimate_q(
            band_freqs, band_residual, peak_idx,
            q_min=q_min, q_max=q_max,
        )

        # Per-bin cap: interpolate the array at this peak's frequency; a scalar
        # cap applies unchanged.
        cut_floor = (
            float(np.interp(peak_freq, freqs, max_cut_db))
            if max_cut_is_array else max_cut_db
        )

        proposed = -peak_db
        if cuts_only:
            gain_db = float(np.clip(proposed, cut_floor, 0.0))
        else:
            gain_db = float(np.clip(proposed, cut_floor, max_boost_db))

        if abs(gain_db) < min_filter_gain_db:
            break

        peq = PEQ(freq=peak_freq, q=q_est, gain=gain_db)
        peqs.append(peq)

        # A peaking filter adds `bell(f, gain_db)` to the response, so the new
        # residual is old_residual + bell.
        bell = _bell_response_db(freqs, peak_freq, q_est, gain_db)
        residual = residual + bell

    return peqs


def total_max_boost_db(peqs: list[PEQ]) -> float:
    """Worst-case additive boost across the PEQ set, in dB.

    Boost stacking is the load-bearing concern: one +3 dB filter is fine, two
    at adjacent frequencies summing to +6 dB is not. Delegates to the canonical
    contract helper so design-time and emit-time share one definition.
    """
    return total_positive_boost_db(peqs)


def predicted_response(
    peqs: list[PEQ],
    freqs: np.ndarray,
) -> np.ndarray:
    """The dB shift the PEQ chain applies at each frequency (sum of bells)."""
    if not peqs:
        return np.zeros_like(freqs, dtype=np.float64)
    out = np.zeros_like(freqs, dtype=np.float64)
    for peq in peqs:
        out += _bell_response_db(freqs, peq.freq, peq.q, peq.gain)
    return out
