# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The bands a measurement comparison may be judged over.

One owner for every "which frequencies may vote" question in the analysis; each
docstring below says what it clamps to and why.

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
    """SSOT overlap band for the GCC alignment, trim solve, ripple, and
    VERIFY-tracking comparisons: the nominal ``Fc ± 1 octave`` band, clamped to
    the TRUE driver-sweep overlap.

    The nominal ``[Fc/OVERLAP_OCTAVE_RATIO, Fc*OVERLAP_OCTAVE_RATIO]`` band
    silently assumes both drivers were excited across the whole span, but each
    driver's MEASURE sweep only covers its own declared band (design §5.4) — a
    tweeter sweep starting AT Fc makes ``[Fc/2, Fc)`` pure deconvolution noise
    for that branch. That noise corrupts the GCC delay/confidence, the trim
    solve, the predicted ripple, and, via the MEASURE-predicted sum, VERIFY's
    tracking comparison; a hardware run failed to clear the alignment
    confidence floor because of it. Clamping ``lo`` UP to the tweeter's actual
    sweep floor and ``hi`` DOWN to the woofer's actual sweep ceiling keeps
    every consumer inside frequencies BOTH branches have real excited energy
    at. ``None`` bounds leave that side at the nominal Fc/octave edge.
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

    ``[Fc/ρ, Fc·ρ]`` intersected with the band this branch's stimulus actually
    radiated, so a row the sweep never entered cannot vote. Whichever edge
    binds, binds: a tweeter swept from above ``Fc/ρ`` raises ``lo``, a woofer
    swept to below ``Fc·ρ`` lowers ``hi``. ``radiated_band_hz`` of ``None``
    leaves the nominal band untouched.

    The corner only ever BOUNDS this window, so a branch that has none (a 1-way
    main) is judged over what it radiated; neither fact means ``None``.

    Erring CONSERVATIVE on row width: a row the window keeps can still be WIDER
    than the sweep's coverage of it, and under a flat noise floor that
    understates SNR by ``10*log10(row_width / covered_width)`` — always toward
    REFUSING. The dilution is set by how deep inside a wide row the sweep's
    edge lands, so it has no natural ceiling: 0.97 dB for a tweeter swept from
    1600 Hz, 4.77 dB for a woofer swept only to 2000 Hz, 14.77 dB for one whose
    ceiling sits just above the ``mid`` row's 1000 Hz floor.

    An empty intersection (``lo >= hi``) is returned as-is, not widened. It
    still admits rows overlapping the radiated band —
    :func:`~jasper.audio_measurement.snr_policy.worst_band_verdict`'s
    ``_band_overlaps`` tests ``row_hi > lo`` and ``row_lo < hi``
    INDEPENDENTLY, so a row spanning the whole inverted interval satisfies both
    — which makes "empty" a narrower franchise, not "no verdict". The guarantee
    that holds either way: whatever a window admits still overlaps the radiated
    band, since admission needs ``row_hi > lo >= radiated_lo`` and ``row_lo <
    hi <= radiated_hi``.
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

    ``[Fc/ρ, Fc·ρ]`` intersected with
    :func:`jasper.audio_measurement.gate_disclosure.evaluation_band_hz` over
    this capture's own gate VALIDITY floor (``1/T``,
    :func:`~jasper.audio_measurement.gating.f_valid_floor_hz`) and the band its
    stimulus actually radiated, so the floor is always the evidence's own and
    never a literal. Not the trusted floor (``2.5/T``): that one is disclosed
    beside a verdict and never bounds this one
    (:data:`~jasper.audio_measurement.gating.TRUSTED_FLOOR_MULTIPLIER`).
    ``None`` when that intersection is empty: no band this capture supports, so
    no number is invented for one.

    Deliberately NOT :func:`overlap_band_hz`. That one clamps ``lo`` UP to the
    tweeter's MEASURE sweep floor because its consumers read the TWEETER BRANCH
    ALONE, which below that floor is deconvolution noise from a driver that was
    never excited. A VERIFY summed capture has no such problem — one mono sweep
    through the applied graph spanning ``[min(150, Fc/2), 20 kHz]`` — so the
    composite is real below the tweeter's sweep floor, which is exactly where a
    null lands when the tweeter is swept from Fc. Widening the MODEL-tracking
    band there WOULD be dishonest; this band is for the absolute claim, which
    needs no per-branch model. A JTS3 checkpoint graded ``[2000, 4000] Hz`` and
    passed at 0.919 dB against 1.5 dB while its post-apply cloud measured
    −4.80 dB at 1656 Hz, 344 Hz below the graded floor.

    That figure is signal-derived and stated at 1/3-octave smoothing, the spec
    gauge's convention. This function's consumer smooths at
    :data:`VERIFY_TRACKING_SMOOTHING_FRACTION` (1/6 octave), which resolves a
    narrow dip more sharply, so the two numbers are not expected to match.

    Its one caller is ``_verify_absolute_result``, and that claim's ``band_hz``
    is ALSO the region the blend correction is solved and graded over
    (``crossover_v2.round_evidence._crossover_region`` reads it back).
    The correction consumes this function's output through that consumer rather
    than calling it again, so the band a household is shown and the band a
    filter is cut over are the same number by construction — and this stays a
    one-caller function.
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
    """The band a VERIFY capture's tracking comparison may be graded over.

    With a corner, :func:`overlap_band_hz` clamped to what MEASURE actually
    excited. Without one (a 1-way main), THIS sweep's radiated span intersected
    with that excited band: the verify sweep reaches below MEASURE's floor, and
    the predicted sum down there is deconvolution noise. ``None`` where no band
    is stated, rather than one nobody established.
    """
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
