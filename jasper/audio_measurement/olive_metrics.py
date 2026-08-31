# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""NBD and SM: Olive's published audibility-weighted deviation metrics.

Olive, "A Multiple Regression Model for Predicting Loudspeaker Preference
Using Objective Measurements," Part I, AES 116th Convention, preprint 6113
(2004); Part II, AES 117th Convention, preprint 6190 (2004); embodied in
US Patent 8,311,232 B2, "Method for predicting loudspeaker preference." The
patent's Eq. 9 (verbatim, for provenance — NOT implemented here, see below):
``Pref. Rating = 12.69 - 2.49*NBD_ON - 2.99*NBD_PIR - 4.31*LFX + 2.32*SM_PIR``,
every term "smoothed in 1/20 octave bands." Reconciled in
``docs/research/2026-08-31-tuning-methodology-deep-research/01-correction-granularity-and-audibility.md``.

This module implements the two METRICS the patent's coefficients weight —
:func:`nbd` (Narrow Band Deviation, "how bumpy") and :func:`sm` (Smoothness,
the r² of a line fit to the curve) — never the preference regression itself,
which is a *listening-test-fitted prediction*, not a measurement. Per
ADR-0202 rule 2, both are co-metrics: they inform a graded round, and
neither gates nor vetoes — ``jasper.active_speaker.flat_spec.SPEC_BANDS``
stays the sole acceptance metric.

Distinct from :func:`~jasper.audio_measurement.analysis.deviation_metrics`,
which reads RMS/max deviation of a measured curve from an EXTERNAL target
curve (room correction's before/after readout). NBD and SM have no external
target — the reference each curve is judged against is its own local trend.

Both functions run the caller's raw curve through
:func:`~jasper.audio_measurement.analysis.smooth_fractional_octave` at
:data:`OLIVE_SMOOTHING_FRACTION` themselves — never a second smoother, and
never a precondition on the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave

__all__ = [
    "NBD_BAND_OCTAVES",
    "OLIVE_SMOOTHING_FRACTION",
    "NBDResult",
    "SMResult",
    "nbd",
    "sm",
]

#: Olive 2004 / US 8,311,232 B2: every term is "smoothed in 1/20 octave
#: bands" before any deviation or regression statistic is taken.
OLIVE_SMOOTHING_FRACTION = 20

#: Olive 2004: NBD's narrow-band deviation is read off contiguous 1/2-octave
#: bands of the 1/20-octave-smoothed curve.
NBD_BAND_OCTAVES = 0.5

#: Multiplicative half-octave-band edge step (2**0.5).
_HALF_OCTAVE_FACTOR = 2.0 ** NBD_BAND_OCTAVES

#: Below this SS_tot (dB^2, the smoothed curve's own variance in the graded
#: band) a curve is flat to float noise, and the r^2 ratio is 0/0 rather than
#: informative. Reported as a perfect fit (1.0) — a flat line through a flat
#: curve has zero residual, which is exactly what r^2 = 1 means; the
#: alternative (0.0, "no fit") would score a perfectly smooth curve as
#: perfectly bumpy. 1e-18 dB^2 is ~20 orders below any real measurement
#: curve's variance, so it only fires on curves that are flat in floating
#: point, per the flat-curve test this constant is calibrated against.
_SM_ZERO_VARIANCE_EPS_DB2 = 1e-18


@dataclass(frozen=True)
class NBDResult:
    """Narrow Band Deviation (Olive 2004 / US 8,311,232 B2), in dB.

    The mean, over contiguous :data:`NBD_BAND_OCTAVES`-wide bands spanning
    ``band_hz``, of each band's own mean absolute deviation from that
    band's own mean — read off the curve after
    :data:`OLIVE_SMOOTHING_FRACTION`-octave power smoothing. Lower is
    smoother/flatter; a perfectly flat curve reads 0.0. A co-metric
    (ADR-0202) — it never gates or vetoes a round.

    Args:
      nbd_db: the metric itself.
      band_hz: the ``(lo, hi)`` band actually scored — the caller's
        ``band_hz``, echoed for provenance.
      smoothing_fraction: the 1/N-octave fraction actually applied
        (:data:`OLIVE_SMOOTHING_FRACTION`), echoed rather than assumed.
      band_octaves: the band width actually used (:data:`NBD_BAND_OCTAVES`),
        echoed for the same reason.
      n_bands: how many half-octave bands contributed — fewer than the
        geometric count when a trailing partial band still held at least
        one sample, and short of the full geometric span when a band held
        none (skipped, never fabricated as a zero deviation).
      n_samples: total curve samples that fell inside ``band_hz`` and
        contributed to some band's deviation.
    """

    nbd_db: float
    band_hz: tuple[float, float]
    smoothing_fraction: int
    band_octaves: float
    n_bands: int
    n_samples: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "nbd_db": self.nbd_db,
            "band_hz": list(self.band_hz),
            "smoothing_fraction": self.smoothing_fraction,
            "band_octaves": self.band_octaves,
            "n_bands": self.n_bands,
            "n_samples": self.n_samples,
        }


@dataclass(frozen=True)
class SMResult:
    """Smoothness (Olive 2004 / US 8,311,232 B2): unitless, 0..1.

    The r² goodness-of-fit of an ordinary-least-squares line, fit in
    (log2(frequency), dB) space, to the curve after
    :data:`OLIVE_SMOOTHING_FRACTION`-octave power smoothing, over
    ``band_hz``. 1.0 is a perfectly line-shaped response (a flat curve
    included — see :data:`_SM_ZERO_VARIANCE_EPS_DB2`); lower is bumpier. A
    co-metric (ADR-0202) — it never gates or vetoes a round.

    Args:
      sm_r2: the metric itself.
      band_hz: the ``(lo, hi)`` band actually scored.
      smoothing_fraction: the 1/N-octave fraction actually applied.
      n_samples: curve samples inside ``band_hz`` the regression was fit
        over.
    """

    sm_r2: float
    band_hz: tuple[float, float]
    smoothing_fraction: int
    n_samples: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sm_r2": self.sm_r2,
            "band_hz": list(self.band_hz),
            "smoothing_fraction": self.smoothing_fraction,
            "n_samples": self.n_samples,
        }


def _smoothed_curve(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Validate shape, then the one shared :data:`OLIVE_SMOOTHING_FRACTION`
    smoothing pass both :func:`nbd` and :func:`sm` score."""

    freqs = np.asarray(freqs_hz, dtype=float)
    magnitude = np.asarray(magnitude_db, dtype=float)
    if freqs.ndim != 1 or magnitude.ndim != 1:
        raise ValueError(
            f"freqs_hz and magnitude_db must be 1-D, got shapes "
            f"{freqs.shape} and {magnitude.shape}"
        )
    if freqs.size != magnitude.size:
        raise ValueError(
            f"length mismatch: freqs_hz={freqs.size} magnitude_db={magnitude.size}"
        )
    if freqs.size == 0:
        raise ValueError("freqs_hz must not be empty")
    smoothed = smooth_fractional_octave(
        freqs, magnitude, fraction=OLIVE_SMOOTHING_FRACTION,
    )
    return freqs, smoothed


def _validated_band(band_hz: tuple[float, float]) -> tuple[float, float]:
    lo, hi = float(band_hz[0]), float(band_hz[1])
    if not (lo > 0.0 and hi > lo):
        raise ValueError(
            f"band_hz must be a positive, increasing (lo, hi) pair, got {band_hz}"
        )
    return lo, hi


def _half_octave_edges(lo: float, hi: float) -> list[tuple[float, float]]:
    """Contiguous :data:`NBD_BAND_OCTAVES`-wide band edges spanning
    ``[lo, hi]``. The last band may be narrower when ``hi/lo`` is not an
    exact power of :data:`_HALF_OCTAVE_FACTOR` — included at whatever width
    it has rather than dropped, so the full caller-stated band is covered.
    """

    edges: list[tuple[float, float]] = []
    edge = lo
    while edge < hi:
        nxt = min(edge * _HALF_OCTAVE_FACTOR, hi)
        edges.append((edge, nxt))
        edge = nxt
    return edges


def nbd(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray, band_hz: tuple[float, float],
) -> NBDResult:
    """Narrow Band Deviation over ``band_hz`` (Olive 2004 / US 8,311,232 B2).

    Band-clamped to ``band_hz``: the smoothing pass runs on the full input
    (so a caller supplying context either side of the band gets an unbiased
    smoothed value at the band edges), but every sample outside
    ``[band_hz[0], band_hz[1]]`` is excluded from the deviation itself.

    Raises :class:`ValueError` for malformed input (see
    :func:`_smoothed_curve` / :func:`_validated_band`) or a ``band_hz`` that
    selects no sample from ``freqs_hz`` — a caller error, not a measurement
    absence, so this raises rather than returning a fabricated 0.0.
    """

    freqs, smoothed = _smoothed_curve(freqs_hz, magnitude_db)
    lo, hi = _validated_band(band_hz)
    edges = _half_octave_edges(lo, hi)
    deviations: list[float] = []
    n_samples = 0
    last_index = len(edges) - 1
    for index, (band_lo, band_hi) in enumerate(edges):
        mask = (
            (freqs >= band_lo) & (freqs <= band_hi)
            if index == last_index
            else (freqs >= band_lo) & (freqs < band_hi)
        )
        values = smoothed[mask]
        if values.size == 0:
            continue
        band_mean = float(np.mean(values))
        deviations.append(float(np.mean(np.abs(values - band_mean))))
        n_samples += int(values.size)
    if not deviations:
        raise ValueError(f"band_hz {band_hz} selects no sample from freqs_hz")
    return NBDResult(
        nbd_db=float(np.mean(deviations)),
        band_hz=(lo, hi),
        smoothing_fraction=OLIVE_SMOOTHING_FRACTION,
        band_octaves=NBD_BAND_OCTAVES,
        n_bands=len(deviations),
        n_samples=n_samples,
    )


def sm(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray, band_hz: tuple[float, float],
) -> SMResult:
    """Smoothness over ``band_hz`` (Olive 2004 / US 8,311,232 B2).

    Band-clamped exactly as :func:`nbd` is: smoothed over the full input,
    scored only over ``[band_hz[0], band_hz[1]]``.

    Raises :class:`ValueError` for malformed input, a ``band_hz`` selecting
    fewer than 2 samples (a line needs two points), or one selecting samples
    at a single frequency (no spread to regress against) — all caller
    errors, not measurement absences.
    """

    freqs, smoothed = _smoothed_curve(freqs_hz, magnitude_db)
    lo, hi = _validated_band(band_hz)
    mask = (freqs >= lo) & (freqs <= hi)
    x = np.log2(freqs[mask])
    y = smoothed[mask]
    if x.size < 2:
        raise ValueError(
            f"band_hz {band_hz} selects {x.size} sample(s) from freqs_hz; "
            "need at least 2 to fit a regression line"
        )
    x_mean = float(np.mean(x))
    if not np.any(x != x[0]):
        raise ValueError(
            f"band_hz {band_hz} selects samples at a single frequency; "
            "cannot fit a regression line"
        )
    y_mean = float(np.mean(y))
    x_centered = x - x_mean
    y_centered = y - y_mean
    ss_tot = float(np.sum(y_centered ** 2))
    if ss_tot <= _SM_ZERO_VARIANCE_EPS_DB2:
        # A curve flat to float noise: the best-fit line is that same flat
        # value, so the residual is 0 too. 0/0 read as a perfect fit, not a
        # failed one — see the constant's own docstring.
        r2 = 1.0
    else:
        slope = float(np.sum(x_centered * y_centered) / np.sum(x_centered ** 2))
        intercept = y_mean - slope * x_mean
        residual = y - (intercept + slope * x)
        ss_res = float(np.sum(residual ** 2))
        r2 = 1.0 - ss_res / ss_tot
    return SMResult(
        sm_r2=r2,
        band_hz=(lo, hi),
        smoothing_fraction=OLIVE_SMOOTHING_FRACTION,
        n_samples=int(x.size),
    )
