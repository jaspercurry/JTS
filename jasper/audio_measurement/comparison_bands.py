# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The bands a measurement comparison may be judged over.

Dependency direction: :mod:`.program_analysis` imports this, never the reverse.
"""

from __future__ import annotations

import math

from jasper.audio_measurement import gate_disclosure

__all__ = [
    "OVERLAP_OCTAVE_RATIO",
    "branch_snr_band_hz",
    "crossover_region_band_hz",
    "overlap_band_hz",
    "verify_tracking_band_hz",
]

# Overlap band for trims / alignment / ripple: Fc ± 1 octave.
OVERLAP_OCTAVE_RATIO = 2.0


def overlap_band_hz(
    fc_hz: float,
    *,
    tweeter_sweep_lo_hz: float | None = None,
    woofer_sweep_hi_hz: float | None = None,
) -> tuple[float, float]:
    """SSOT overlap band ``Fc ± 1 octave``, clamped to the true driver-sweep overlap.

    A driver's MEASURE sweep covers only its own declared band (design §5.4), so
    outside it that branch is deconvolution noise. ``None`` bounds leave that
    side at the nominal edge.
    """
    lo = fc_hz / OVERLAP_OCTAVE_RATIO
    hi = fc_hz * OVERLAP_OCTAVE_RATIO
    if tweeter_sweep_lo_hz is not None:
        lo = max(lo, float(tweeter_sweep_lo_hz))
    if woofer_sweep_hi_hz is not None:
        hi = min(hi, float(woofer_sweep_hi_hz))
    return lo, hi


def branch_snr_band_hz(
    fc_hz: float | None,
    radiated_band_hz: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """The band ONE branch's capture-SNR verdict may be judged over.

    A kept row can still be wider than the sweep's coverage of it, understating
    SNR by ``10*log10(row_width / covered_width)`` — always toward REFUSING.
    An empty intersection (``lo >= hi``) is returned AS-IS, never narrowed to
    ``None`` as the two functions below do: ``snr_policy.worst_band_verdict``
    tests ``row_hi > lo`` and ``row_lo < hi`` independently, so an inverted
    interval is a narrower franchise, not "no verdict".
    """
    if fc_hz is None:
        return radiated_band_hz
    lo = fc_hz / OVERLAP_OCTAVE_RATIO
    hi = fc_hz * OVERLAP_OCTAVE_RATIO
    if radiated_band_hz is None:
        return lo, hi
    return max(lo, float(radiated_band_hz[0])), min(hi, float(radiated_band_hz[1]))


def crossover_region_band_hz(
    fc_hz: float,
    *,
    validity_floor_hz: float | None,
    radiated_band_hz: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """The crossover region a SUMMED capture can be judged over, or ``None``.

    Bounded by this capture's own gate VALIDITY floor (``1/T``), never the
    trusted floor (``2.5/T``), which is disclosed beside a verdict instead.
    """
    if not math.isfinite(fc_hz) or fc_hz <= 0.0:
        return None
    band = gate_disclosure.evaluation_band_hz(validity_floor_hz, radiated_band_hz)
    if band is None:
        return None
    lo = max(fc_hz / OVERLAP_OCTAVE_RATIO, band[0])
    hi = min(fc_hz * OVERLAP_OCTAVE_RATIO, band[1])
    return (lo, hi) if lo < hi else None


def verify_tracking_band_hz(
    fc_hz: float | None,
    *,
    radiated_band_hz: tuple[float, float] | None,
    measure_excited_band_hz: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """The band a VERIFY capture's tracking comparison may be graded over."""
    if fc_hz is not None:
        sweep_lo_hz, sweep_hi_hz = measure_excited_band_hz or (None, None)
        return overlap_band_hz(
            fc_hz,
            tweeter_sweep_lo_hz=sweep_lo_hz,
            woofer_sweep_hi_hz=sweep_hi_hz,
        )
    if radiated_band_hz is None or measure_excited_band_hz is None:
        return radiated_band_hz
    lo = max(radiated_band_hz[0], measure_excited_band_hz[0])
    hi = min(radiated_band_hz[1], measure_excited_band_hz[1])
    return (lo, hi) if lo < hi else None
