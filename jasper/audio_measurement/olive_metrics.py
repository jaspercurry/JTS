# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""NBD and SM: Olive's published audibility-weighted deviation metrics.

Olive, AES preprints 6113 (2004) and 6190 (2004); US Patent 8,311,232 B2. The
two METRICS only -- never the preference regression, a listening-test-fitted
prediction. Both are co-metrics (ADR-0202 rule 2): neither gates nor vetoes.
Olive's definitions assume the 1/20-octave pass is the ONLY smoothing before
the statistic, so a coarser input reads flattering -- unrecoverable from the
samples, hence the caller-stated ``input_smoothing_fraction`` (``None`` =
unknown) echoed in every result.
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
    "nbd_and_sm",
    "sm",
]

#: Olive 2004 / US 8,311,232 B2: every term is smoothed in 1/20-octave bands.
OLIVE_SMOOTHING_FRACTION = 20

#: Olive 2004: NBD reads deviation off contiguous 1/2-octave bands.
NBD_BAND_OCTAVES = 0.5

#: Multiplicative half-octave-band edge step (2**0.5).
_HALF_OCTAVE_FACTOR = 2.0 ** NBD_BAND_OCTAVES

#: Below this SS_tot (dB^2) a curve is flat to float noise and r^2 is 0/0;
#: reported as a perfect fit (1.0). ~20 orders below any real curve's variance.
_SM_ZERO_VARIANCE_EPS_DB2 = 1e-18


@dataclass(frozen=True)
class NBDResult:
    """Narrow Band Deviation (Olive 2004 / US 8,311,232 B2), in dB.

    The mean, over contiguous :data:`NBD_BAND_OCTAVES`-wide bands spanning
    ``band_hz``, of each band's own mean absolute deviation from that band's
    own mean, after :data:`OLIVE_SMOOTHING_FRACTION`-octave power smoothing.
    Lower is smoother; a perfectly flat curve reads 0.0. A band holding no
    sample is skipped, never fabricated as a zero deviation, so ``n_bands`` can
    fall short of the geometric count over ``band_hz``.
    ``input_smoothing_fraction`` is the fraction the caller states its input
    already carried; ``smoothing_fraction`` is the one THIS module applied.
    """

    nbd_db: float
    band_hz: tuple[float, float]
    smoothing_fraction: int
    input_smoothing_fraction: int | None
    band_octaves: float
    n_bands: int
    n_samples: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "nbd_db": self.nbd_db,
            "band_hz": list(self.band_hz),
            "smoothing_fraction": self.smoothing_fraction,
            "input_smoothing_fraction": self.input_smoothing_fraction,
            "band_octaves": self.band_octaves,
            "n_bands": self.n_bands,
            "n_samples": self.n_samples,
        }


@dataclass(frozen=True)
class SMResult:
    """Smoothness (Olive 2004 / US 8,311,232 B2): unitless, 0..1.

    The r² of an ordinary-least-squares line fit in (log2(frequency), dB) space
    to the :data:`OLIVE_SMOOTHING_FRACTION`-octave-smoothed curve over
    ``band_hz``. 1.0 is perfectly line-shaped (a flat curve included, see
    :data:`_SM_ZERO_VARIANCE_EPS_DB2`); lower is bumpier.
    """

    sm_r2: float
    band_hz: tuple[float, float]
    smoothing_fraction: int
    input_smoothing_fraction: int | None
    n_samples: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sm_r2": self.sm_r2,
            "band_hz": list(self.band_hz),
            "smoothing_fraction": self.smoothing_fraction,
            "input_smoothing_fraction": self.input_smoothing_fraction,
            "n_samples": self.n_samples,
        }


def _smoothed_curve(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Validate shape, then the one smoothing pass both metrics score."""

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
    """Contiguous :data:`NBD_BAND_OCTAVES`-wide band edges spanning ``[lo, hi]``.

    The last band may be narrower, and is included at whatever width it has so
    the full caller-stated band is covered.
    """

    edges: list[tuple[float, float]] = []
    edge = lo
    while edge < hi:
        nxt = min(edge * _HALF_OCTAVE_FACTOR, hi)
        edges.append((edge, nxt))
        edge = nxt
    return edges


def nbd(
    freqs_hz: np.ndarray,
    magnitude_db: np.ndarray,
    band_hz: tuple[float, float],
    *,
    input_smoothing_fraction: int | None = None,
) -> NBDResult:
    """Narrow Band Deviation over ``band_hz`` (Olive 2004 / US 8,311,232 B2).

    Smoothing runs on the full input so the band edges are unbiased; only
    samples inside ``band_hz`` enter the deviation. A ``band_hz`` selecting no
    sample raises rather than returning a fabricated 0.0.
    """

    freqs, smoothed = _smoothed_curve(freqs_hz, magnitude_db)
    return _nbd_scored(
        freqs, smoothed, band_hz,
        input_smoothing_fraction=input_smoothing_fraction,
    )


def _nbd_scored(
    freqs: np.ndarray,
    smoothed: np.ndarray,
    band_hz: tuple[float, float],
    *,
    input_smoothing_fraction: int | None,
) -> NBDResult:
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
        input_smoothing_fraction=input_smoothing_fraction,
        band_octaves=NBD_BAND_OCTAVES,
        n_bands=len(deviations),
        n_samples=n_samples,
    )


def sm(
    freqs_hz: np.ndarray,
    magnitude_db: np.ndarray,
    band_hz: tuple[float, float],
    *,
    input_smoothing_fraction: int | None = None,
) -> SMResult:
    """Smoothness over ``band_hz`` (Olive 2004 / US 8,311,232 B2).

    Band-clamped as :func:`nbd` is. A ``band_hz`` selecting fewer than two
    samples, or samples at a single frequency, raises.
    """

    freqs, smoothed = _smoothed_curve(freqs_hz, magnitude_db)
    return _sm_scored(
        freqs, smoothed, band_hz,
        input_smoothing_fraction=input_smoothing_fraction,
    )


def _sm_scored(
    freqs: np.ndarray,
    smoothed: np.ndarray,
    band_hz: tuple[float, float],
    *,
    input_smoothing_fraction: int | None,
) -> SMResult:
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
        # 0/0 on a curve flat to float noise reads as a perfect fit.
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
        input_smoothing_fraction=input_smoothing_fraction,
        n_samples=int(x.size),
    )


def nbd_and_sm(
    freqs_hz: np.ndarray,
    magnitude_db: np.ndarray,
    band_hz: tuple[float, float],
    *,
    input_smoothing_fraction: int | None = None,
) -> tuple[NBDResult, SMResult]:
    """Both metrics off ONE shared smoothing pass."""

    freqs, smoothed = _smoothed_curve(freqs_hz, magnitude_db)
    return (
        _nbd_scored(
            freqs, smoothed, band_hz,
            input_smoothing_fraction=input_smoothing_fraction,
        ),
        _sm_scored(
            freqs, smoothed, band_hz,
            input_smoothing_fraction=input_smoothing_fraction,
        ),
    )
