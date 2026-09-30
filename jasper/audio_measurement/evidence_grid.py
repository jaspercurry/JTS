# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one log grid every banked curve is sampled onto."""

from __future__ import annotations

import math

import numpy as np

# One fixed log-spaced basis for every retained pose curve: fixed rather than
# per-role so both branches land on the SAME frequencies and a consumer can sum
# them without resampling; log-spaced because a crossover argument is a
# per-octave one. 1/12 octave is ~118 Hz at 2 kHz — a COARSE gate, never a polar
# measurement (#1968).
LATERAL_EVIDENCE_BAND_HZ = (20.0, 20_000.0)
LATERAL_EVIDENCE_POINTS_PER_OCTAVE = 12


def lateral_evidence_grid_hz() -> np.ndarray:
    """The shared log basis every retained pose curve is sampled onto."""
    lo, hi = LATERAL_EVIDENCE_BAND_HZ
    octaves = math.log2(hi / lo)
    return np.geomspace(
        lo, hi, num=int(round(octaves * LATERAL_EVIDENCE_POINTS_PER_OCTAVE)) + 1,
    )


def evidence_bins(freqs: np.ndarray) -> np.ndarray:
    """Indices of the native bins of ``freqs`` nearest the shared basis; ties
    use the lower bin. A banked curve holds its values there, never
    interpolated: a phase interpolated across a wrap is simply wrong."""
    grid = lateral_evidence_grid_hz()
    # ``searchsorted`` + a one-step comparison is the nearest native bin on a
    # monotonically increasing rfft grid, without materialising an N x M
    # distance matrix: the analysis grid is hundreds of thousands of bins.
    right = np.searchsorted(freqs, grid).clip(1, freqs.size - 1)
    left = right - 1
    return np.where(
        np.abs(grid - freqs[left]) <= np.abs(freqs[right] - grid), left, right
    )
