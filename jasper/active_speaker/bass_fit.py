# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any, NamedTuple

import numpy as np
from scipy.optimize import least_squares

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.evidence_reasons import REASON_COVERAGE_SHORT, unavailable
from jasper.audio_measurement.trusted_band import TrustedBand, within_trusted


def smooth_bass_curve(grid: np.ndarray, values: np.ndarray) -> np.ndarray:
    indices = np.flatnonzero(np.isfinite(values))
    # A qualification gap cannot contribute power to either neighbouring region.
    for section in np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1):
        if section.size:
            values[section] = smooth_fractional_octave(grid[section], values[section], fraction=3)
    return values


class SealedFit(NamedTuple):
    corner_hz: float
    q: float
    residual_db: float
    level_db: float
    model_db: np.ndarray
    #: Per parameter (level, corner, Q): -1 on its lower bound, 1 on its upper, 0 inside (``least_squares``).
    active_mask: np.ndarray


def sealed_fit(freqs: np.ndarray, y_db: np.ndarray, lo: float, hi: float,
               smooth: Callable[[np.ndarray], np.ndarray] = lambda db: db) -> SealedFit:
    """2nd-order high-pass fit of a level curve over lo..hi Hz, its model put
    through ``smooth``, the smoothing the curve had: its corner, Q, rms miss,
    passband level, the fitted curve at the bins it read, and which parameters
    stopped on a bound."""
    sel = (freqs >= lo) & (freqs <= hi)

    def model(p):
        s = 1j * freqs[sel] / p[1]
        return smooth(p[0] + 20 * np.log10(np.abs(s * s / (s * s + s / p[2] + 1))))

    fit = least_squares(lambda p: model(p) - y_db[sel], x0=(np.median(y_db[sel]), 70.0, 0.7),
                        bounds=((-300, 20, 0.3), (300, 200, 3.0)))
    return SealedFit(float(fit.x[1]), float(fit.x[2]), float(np.sqrt(np.mean(fit.fun ** 2))), float(fit.x[0]),
                     y_db[sel] + fit.fun, fit.active_mask)


def bass_alignment(freqs_hz: np.ndarray, level_db: np.ndarray, band_hz: Sequence[float],
                   trusted: TrustedBand, qualified: np.ndarray) -> dict[str, Any]:
    """The sealed-box alignment one curve fits over ``band_hz`` clipped to its
    trusted band (ADR-0366), from its ``qualified`` bins alone, the curve and
    the model smoothed alike (ADR-0419): the Linkwitz transform's ``source_hz``
    and ``source_q`` (ADR-0359), the fit's rms miss and the bins it read. A band
    left with fewer qualified bins than the fit's three parameters, a corner or
    Q stopped on the fit's bound, or bins that do not straddle the corner
    cannot place it: its coverage gap, naming the trusted band that clipped the
    band and the qualified bins left."""
    grid, level = np.asarray(freqs_hz, dtype=float), np.asarray(level_db, dtype=float)
    within = within_trusted(band_hz, trusted)
    read = (np.asarray(qualified, dtype=bool) & np.isfinite(level) & (grid >= within[0]) & (grid <= within[1])
            if within else np.zeros(grid.shape, dtype=bool))
    count = int(np.count_nonzero(read))
    gap = {"band_hz": list(band_hz), "trusted_band": asdict(trusted), "qualified_bins": count,
           "curve_hz": [float(grid.min()), float(grid.max())] if grid.size else None}
    if count < 3:
        return unavailable(REASON_COVERAGE_SHORT, gap)

    def smoothed(values: np.ndarray) -> np.ndarray:
        full = np.full(grid.shape, np.nan)
        full[read] = values
        return smooth_bass_curve(grid, full)[read]

    freqs, level = grid[read], smoothed(level[read])
    fit = sealed_fit(freqs, level, freqs.min(), freqs.max(), smoothed)
    at_bound = [name for name, active in zip(("source_hz", "source_q"), fit.active_mask[1:]) if active]
    if at_bound or not freqs.min() < fit.corner_hz < freqs.max():
        return unavailable(REASON_COVERAGE_SHORT, {**gap, "source_hz": round(fit.corner_hz, 1),
                                                   "source_q": round(fit.q, 2), "at_bound": at_bound})
    return {"status": "available", "band_hz": [round(float(freqs.min()), 2), round(float(freqs.max()), 2)],
            "source_hz": round(fit.corner_hz, 1), "source_q": round(fit.q, 2), "residual_db": round(fit.residual_db, 2),
            "level_db": round(fit.level_db, 2), "freqs_hz": freqs.round(3).tolist(),
            "measured_db": level.round(3).tolist(), "model_db": fit.model_db.round(3).tolist()}
