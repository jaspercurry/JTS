# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Dip measurement on a response curve, and the two readings built on it.

A dip's depth is read against its own two flanking maxima (:func:`_measure_candidates`).
:func:`feature_position_variance` tracks one prescribed filter's extremum across seat curves;
:func:`branch_gap_null_depth_ceiling_db` bounds how deep an inverted pair can cancel.

Pure computation: no I/O, no logging, no globals, no randomness, no product policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from jasper.audio_measurement.alignment import parabolic_peak
from jasper.audio_measurement.evidence_reasons import REASON_TOO_FEW_POSITIONS
from jasper.audio_measurement.peq import bell_half_width_oct
from jasper.audio_measurement.series_stats import power_mean_db

# How far a flanking-maximum search may run, in octaves either side of a candidate minimum.
# Beyond about half an octave the response's own shape (baffle step, driver rolloff, crossover)
# dominates whatever comb structure is present. Also bounded by the neighbouring minima, so a
# search can never step over one null to use the far side of another. A dip broader than ~1
# octave has its flanks clipped and its depth understated (S0 1.8 kHz lobing dip: 10.08 dB here
# vs 10.71 dB with hand-picked wider flanks).
FLANK_SEARCH_MAX_OCT = 0.50


def branch_gap_null_depth_ceiling_db(gap_db: float) -> float:
    """``-20*log10(1 - 10**(-gap/20))`` — the residual when the quieter branch is inverted
    against the louder one: two SOURCES whose levels differ, the bound a reverse-null
    confirmation is read against — a pair 10 dB apart cannot cancel deeper than ~3.3 dB however
    right the delay is.

    Disclosure, never a refusal. ``gap_db`` at or below 0 returns ``inf``; large enough that the
    quieter branch contributes nothing saturates at 0.0.
    """
    if gap_db <= 0.0:
        return float("inf")
    residual = 1.0 - 10.0 ** (-float(gap_db) / 20.0)
    if residual <= 0.0:
        return 0.0
    return float(-20.0 * np.log10(residual))


# --------------------------------------------------------------------------- #
# Candidate location and depth
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Candidate:
    """One located minimum with its depth and interval. Internal."""

    f_hz: float
    index: int
    depth_db: float
    diag_depth_db: float
    baseline_db: float
    null_level_db: float
    flank_lo_hz: float
    flank_hi_hz: float
    lo_index: int
    hi_index: int
    f_lo_hz: float
    f_hi_hz: float


def _locate_minima(diag: np.ndarray, band_idx: np.ndarray, min_sep_oct: float,
                   freqs: np.ndarray) -> list[int]:
    """Local minima of ``diag`` inside the band, thinned to one per ``min_sep_oct``, keeping
    the lowest. Thinning is by *level*, not depth: depth needs flanks, flanks need the
    neighbouring minima, so choosing by depth would close that loop. Separation is one
    smoothing window (``1/diag_fraction`` octaves) — closer minima are not independently
    resolved by the curve."""
    y = diag[band_idx]
    if y.size < 3:
        return []
    interior = np.flatnonzero((y[1:-1] <= y[:-2]) & (y[1:-1] < y[2:])) + 1
    kept: list[int] = []
    for p in sorted(interior, key=lambda q: (float(y[q]), int(q))):
        f = freqs[band_idx[p]]
        if any(abs(np.log2(f / freqs[band_idx[q]])) < min_sep_oct for q in kept):
            continue
        kept.append(int(p))
    return sorted(kept)


def _measure_candidates(
    freqs: np.ndarray,
    diag: np.ndarray,
    raw: np.ndarray,
    band_idx: np.ndarray,
    min_sep_oct: float,
) -> tuple[list[_Candidate], list[float]]:
    """Locate the band's minima and measure each one's depth and interval. Returns the
    measurable candidates plus the frequencies of ones with no flanking maximum on one side —
    only reachable on a grid coarse enough that ``FLANK_SEARCH_MAX_OCT`` falls inside one bin.
    Refuses rather than reading a flank from whichever bin was nearest.
    """
    positions = _locate_minima(diag, band_idx, min_sep_oct, freqs)
    y = diag[band_idx]
    out: list[_Candidate] = []
    unmeasurable: list[float] = []
    for j, p in enumerate(positions):
        f0 = float(freqs[band_idx[p]])
        # Bound by the neighbouring minima (can't step over one null) and FLANK_SEARCH_MAX_OCT.
        lo_bound = positions[j - 1] if j > 0 else 0
        hi_bound = positions[j + 1] if j + 1 < len(positions) else int(band_idx.size - 1)
        lo_bound = max(
            lo_bound,
            int(np.searchsorted(freqs[band_idx], f0 * 2.0**-FLANK_SEARCH_MAX_OCT)),
        )
        hi_bound = min(
            hi_bound,
            int(np.searchsorted(freqs[band_idx], f0 * 2.0**FLANK_SEARCH_MAX_OCT)),
        )
        if lo_bound >= p or hi_bound <= p:
            unmeasurable.append(f0)
            continue
        pl = lo_bound + int(np.argmax(y[lo_bound:p]))
        pr = p + 1 + int(np.argmax(y[p + 1 : hi_bound + 1]))
        i, il, ir = int(band_idx[p]), int(band_idx[pl]), int(band_idx[pr])
        baseline = power_mean_db(np.array([diag[il], diag[ir]], dtype=float))
        depth = baseline - float(raw[i])
        diag_depth = baseline - float(diag[i])
        # Half-depth width on the diagnostic curve, bounded by the two flanking maxima.
        half = baseline - 0.5 * diag_depth
        a = p
        while a > pl and diag[band_idx[a - 1]] <= half:
            a -= 1
        b = p
        while b < pr and diag[band_idx[b + 1]] <= half:
            b += 1
        out.append(
            _Candidate(
                f_hz=f0,
                index=i,
                depth_db=float(depth),
                diag_depth_db=float(diag_depth),
                baseline_db=float(baseline),
                null_level_db=float(raw[i]),
                flank_lo_hz=float(freqs[il]),
                flank_hi_hz=float(freqs[ir]),
                lo_index=int(band_idx[a]),
                hi_index=int(band_idx[b]),
                f_lo_hz=float(freqs[band_idx[a]]),
                f_hi_hz=float(freqs[band_idx[b]]),
            )
        )
    return out, unmeasurable


# See docs/research/2026-07-29-attribution/07-reanalysis-position-variance.md §3, §6.
FEATURE_MIN_DEPTH_DB = 2.0
FEATURE_MIN_DEEP_POSITIONS = 6
FEATURE_SOURCE_FIXED_CV_PERCENT = 3.0
FEATURE_POSITION_VARIANT_CV_PERCENT = 8.0


def feature_position_variance(
    curves: Sequence[tuple[np.ndarray, np.ndarray]], *, freq_hz: float, q: float,
    gain_db: float, positions_total: int,
) -> dict[str, Any]:
    """Track extrema on curves sampled uniformly in log frequency."""
    bw = bell_half_width_oct(q)
    lo, hi = freq_hz * 2 ** -bw, freq_hz * 2 ** bw
    flank_lo, flank_hi = freq_hz * 2 ** -(bw + FLANK_SEARCH_MAX_OCT), freq_hz * 2 ** (bw + FLANK_SEARCH_MAX_OCT)
    frequencies = []
    for freqs, magnitude in curves:
        signed = magnitude if gain_db > 0 else -magnitude
        valid = np.flatnonzero((freqs >= flank_lo) & (freqs <= flank_hi) & np.isfinite(signed))
        candidates, _ = _measure_candidates(freqs, signed, signed, valid, 0.0)
        deepest = max((c for c in candidates if lo <= c.f_hz <= hi),
                      key=lambda c: c.depth_db, default=None)
        if deepest is not None and deepest.depth_db >= FEATURE_MIN_DEPTH_DB:
            refined = parabolic_peak(signed, deepest.index)
            frequencies.append(float(2 ** np.interp(refined, np.arange(freqs.size), np.log2(freqs))))
    count = len(frequencies)
    cv = float(np.std(frequencies, ddof=1) / np.mean(frequencies) * 100) if count >= 2 else None
    if count < FEATURE_MIN_DEEP_POSITIONS:
        classification = REASON_TOO_FEW_POSITIONS
    elif cv is not None and cv < FEATURE_SOURCE_FIXED_CV_PERCENT:
        classification = "source_fixed"
    elif cv is not None and cv > FEATURE_POSITION_VARIANT_CV_PERCENT:
        classification = "position_variant"
    else:
        classification = "unsure"
    return {"cv_percent": cv, "positions_deep": count, "positions_total": positions_total,
            "classification": classification, "frequencies_hz": frequencies}
