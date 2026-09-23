# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-position band statistics and analysis-grid helpers.

:class:`BandSpread` states how far a cloud's positions disagree per octave band;
:func:`decimate_curve_to_analysis_grid` block-averages a too-fine grid in linear power;
:func:`merged_true_intervals` turns a per-bin mask into frequency intervals.

Pure computation: no I/O, no logging, no globals, no randomness, no product policy.

See docs/historical/linearization-campaign-2026-07.md.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from jasper.audio_measurement.band_ladders import OCTAVE_BAND_CENTERS_HZ as OCTAVE_BAND_CENTERS_HZ, OCTAVE_BANDS_HZ

# 1/6-octave for diagnostics, 1/3-octave for pass/fail.
DEFAULT_DIAG_FRACTION = 6
DEFAULT_SPEC_FRACTION = 3

MIN_BAND_BINS = 4

# Upper bound on the analysis grid; a finer canonical grid is block-averaged in linear power
# onto a coarser one first. No resolution lost: 16385 bins over 24 kHz is ~1.46 Hz spacing
# against a ~29 Hz narrowest window (1/6-octave at 250 Hz). Averaging, never subsampling —
# subsampling a combed curve aliases onto whichever bins land on peaks or nulls.
MAX_ANALYSIS_BINS = 16385


@dataclass(frozen=True)
class BandSpread:
    """Cross-position magnitude spread in one octave band — two numbers, two questions.

    ``sigma_db`` is the *level* spread: each position collapsed to one band level (linear
    power mean), then sample std dev (``ddof=1``) across positions — insensitive to comb
    structure by construction, since a band holds many comb periods. Large means the positions
    genuinely disagree about loudness (mic distance, gain, directivity), which averaging won't
    fix. ``max_sigma_db`` is the *structure* spread: the worst single bin's cross-position
    sigma, unsmoothed, riding comb nulls on purpose. ``max_sigma_db`` dwarfing ``sigma_db``
    means null-dominated at a few frequencies (decorrelation working); comparable means
    broadly noisy.
    """

    center_hz: float
    f_lo: float
    f_hi: float
    sigma_db: float
    max_sigma_db: float
    n_bins: int


def _decimate_to_analysis_grid(
    grid: np.ndarray, stacked: np.ndarray, *, max_bins: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Block-average a too-fine analysis grid down to ``max_bins`` in **linear power**, so
    decimation composes with the power mean instead of biasing it (subsampling would alias a
    combed curve onto whichever bins land on peaks or nulls; see ``MAX_ANALYSIS_BINS``).
    ``max_bins=None`` reads that constant fresh per call, staying monkeypatchable.

    Blocks are fixed-width so the decimated grid stays exactly linear; a trailing partial
    block is dropped (at most ``block - 1`` bins lost) rather than averaged at a different
    centre. Identity when the grid is already within the bound.
    """
    if max_bins is None:
        max_bins = MAX_ANALYSIS_BINS
    n_bins = int(grid.size)
    if n_bins <= max_bins:
        return grid, stacked
    block = -(-n_bins // max_bins)  # ceil division
    n_blocks = n_bins // block
    kept = n_blocks * block
    coarse_grid = grid[:kept].reshape(n_blocks, block).mean(axis=1)
    power = 10.0 ** (stacked[:, :kept] / 10.0)
    coarse_stacked = 10.0 * np.log10(
        power.reshape(stacked.shape[0], n_blocks, block).mean(axis=2)
    )
    coarse_grid.flags.writeable = False
    return coarse_grid, coarse_stacked


def decimate_curve_to_analysis_grid(
    grid: np.ndarray, magnitude_db: np.ndarray, *, max_bins: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """The 1-D public face of :func:`_decimate_to_analysis_grid`. Identity when already within
    ``max_bins``."""
    coarse_grid, coarse_stacked = _decimate_to_analysis_grid(
        grid, np.asarray(magnitude_db, dtype=float).reshape(1, -1), max_bins=max_bins,
    )
    return coarse_grid, coarse_stacked[0]


def merged_true_intervals(
    freqs_hz: np.ndarray, mask: np.ndarray
) -> tuple[tuple[float, float], ...]:
    """Contiguous ``True`` runs of ``mask`` as merged ``(f_lo, f_hi)`` intervals. The single
    owner of this rule; :mod:`jasper.active_speaker.flat_spec` imports it rather than keeping
    its own copy. Adjacency is by **array index**, valid only when ``freqs_hz`` is ascending
    (enforced upstream, not re-checked here). No gap-bridging.
    """
    flagged = np.flatnonzero(mask)
    if flagged.size == 0:
        return ()
    breaks = np.flatnonzero(np.diff(flagged) > 1)
    starts = np.concatenate(([flagged[0]], flagged[breaks + 1]))
    ends = np.concatenate((flagged[breaks], [flagged[-1]]))
    return tuple(
        (float(freqs_hz[s]), float(freqs_hz[e]))
        for s, e in zip(starts, ends, strict=True)
    )


def octave_bands_hz(
    grid_lo_hz: float, grid_hi_hz: float,
) -> tuple[tuple[float, float, float], ...]:
    """``(center, lo, hi)`` for every :data:`OCTAVE_BAND_CENTERS_HZ` band a grid
    spanning ``grid_lo_hz``..``grid_hi_hz`` reaches, clamped to that span.

    Edges are ``center / sqrt(2) .. center * sqrt(2)``, and a band the grid does
    not reach at all is omitted rather than returned empty. Public because the
    cross-position spread below and every reader that bands an answer AGAINST
    that spread must cut the spectrum at the same places.
    """
    bands = []
    for center, (low, high) in zip(OCTAVE_BAND_CENTERS_HZ, OCTAVE_BANDS_HZ):
        lo = max(low, grid_lo_hz)
        hi = min(high, grid_hi_hz)
        if lo < hi:
            bands.append((float(center), float(lo), float(hi)))
    return tuple(bands)


def _band_spread(freqs: np.ndarray, stacked: np.ndarray) -> tuple[BandSpread, ...]:
    """Octave-band cross-position spread from raw per-position curves. Deliberately
    **unsmoothed**: a band-power average gives the same statistic directly, without one
    ``smooth_fractional_octave`` pass per position. See :class:`BandSpread`."""
    if stacked.shape[0] < 2:
        return ()
    power = 10.0 ** (stacked / 10.0)
    per_bin_sigma = np.std(stacked, axis=0, ddof=1)
    bands: list[BandSpread] = []
    for center, lo, hi in octave_bands_hz(float(freqs[0]), float(freqs[-1])):
        mask = (freqs >= lo) & (freqs <= hi)
        n_bins = int(np.count_nonzero(mask))
        if n_bins < MIN_BAND_BINS:
            continue
        # One level per position: band energy in power, then dB.
        band_level_db = 10.0 * np.log10(np.mean(power[:, mask], axis=1))
        bands.append(
            BandSpread(
                center_hz=float(center),
                f_lo=float(freqs[mask][0]),
                f_hi=float(freqs[mask][-1]),
                sigma_db=float(np.std(band_level_db, ddof=1)),
                max_sigma_db=float(np.max(per_bin_sigma[mask])),
                n_bins=n_bins,
            )
        )
    return tuple(bands)
