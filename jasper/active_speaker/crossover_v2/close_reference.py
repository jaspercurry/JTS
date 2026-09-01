# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A close capture, corrected to the far distance, says how much of the far
read was the room (#3501).

Between the gate's resolution floor (2.5/T) and the room's entanglement floor
(2.5/t_first_bounce) no window length separates speaker from room at one
point: the limit is information-theoretic, not tooling. One extra capture
breaks it. Move the mic from ~1 m to ~12 in on the woofer axis — still inside
the driver's far field — and the direct sound gains ``20*log10(r_far/r_close)``
on the room while the bounce paths barely move.

What this module computes, given the two impulse responses:

1. **Where to stand.** :func:`recommended_distance` from driver diameter and
   crossover corner, with both terms of the derivation printed.
2. **The correction.** Scale the close IR by ``r_close/r_far``, delay it by the
   geometric ``(r_far - r_close)/c``, then SUB-SAMPLE align it to the far IR's
   own direct arrival. A subtraction cancels only to the depth its phase error
   allows — ``residual/direct = 2*|sin(pi*f*dt)|`` (gate-research-results.md,
   document 2 section B3) — so the achieved lag and the cancellation-depth
   budget at the band edges are published beside every number they bound.
3. **The verdict, per band.** Where the corrected close read agrees with the
   far read, the far read was speaker-dominated; where they disagree and the
   subtraction residual is large, the far read was the room.

**Every number carries its frame.** Window shape, lead, taper, smoothing
fraction, grid, transform length, alignment band and upsample ride in the
report's ``frame`` block, because the P1 offline run showed a banked
-8.78 dB feature reading -1.07 to -5.45 dB depending only on frame choices.

Distances are DECLARED by the caller, never read from the sidecar: today's
sidecars pin ``mark_distance_m = 1.0`` for every pose (a per-row distance
lands on #3498). Where a sidecar disagrees with the declared value, both are
published and the declared one is used.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.flat_spec import SPEC_BANDS
from jasper.audio_measurement.deconv import regularized_deconvolution_full
from jasper.audio_measurement.gating import (
    SEARCH_T_MAX_MS,
    TAPER_FRACTION,
    build_gate_window,
    f_trusted_floor_hz,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.program_analysis import GCC_UPSAMPLE, gcc_phat
from jasper.audio_measurement.sweep import read_wav_mono

from .feature_classifier import (
    DETREND_FRACTION,
    GRID_HI_HZ,
    GRID_LO_HZ,
    MAGNITUDE_SMOOTH_FRACTION,
    PHASE_GATE_LEAD_MS,
    analysis_grid,
    detrend,
    smoothed_curve,
)

__all__ = [
    "ALIGNMENT_CONFIDENCE_FLOOR",
    "CloseReferenceRefused",
    "RoundCapture",
    "CLOSE_REFERENCE_SCHEMA_VERSION",
    "DEFAULT_GATE_MS",
    "K_MARGIN",
    "MIN_BAND_POINTS",
    "RESIDUAL_N_FFT",
    "ROOM_RESIDUAL_FLOOR_DB",
    "VERDICT_AGREEMENT",
    "VERDICT_ROOM_DOMINATED",
    "VERDICT_UNRESOLVED",
    "GENERATED_BY",
    "cancellation_depth_db",
    "capture_impulse_response",
    "compare_impulse_responses",
    "compare_rounds",
    "far_field_ceiling_hz",
    "load_round_capture",
    "placement_tolerance_db",
    "recommended_distance",
]

CLOSE_REFERENCE_SCHEMA_VERSION = 1

#: The tool a reader re-runs to reproduce a report.
GENERATED_BY = "jasper-close-reference"

#: Driver diameters of margin added to the piston far-field distance. Chosen
#: so the three anchor cases the issue states land where it says they do:
#: 5.5 in / 2.5 kHz -> ~12 in, 12 in / 500 Hz -> ~25 in, 2.5 in / 2.5 kHz ->
#: ~5 in. It is the DOMINANT term at every one of them; the far-field term is
#: a 0.3-1.9 in correction on top, which is why both are published separately.
K_MARGIN = 2.0

#: Placement slop the operator is held to, metres (+/- 0.5 in). Only the 1/r
#: correction cares, and :func:`placement_tolerance_db` prices it.
PLACEMENT_TOLERANCE_M = 0.0127

#: Aim slop that costs nothing measurable in the close capture's validity
#: band: the woofer is omnidirectional there, so +/-5 deg is free.
AIM_TOLERANCE_DEG = 5.0

#: Declared gate length, ms, when the caller states none. The pipeline's own
#: reflection-search ceiling -- the longest window the product ever calls
#: reflection-free. The close capture's clean window is LONGER (its first
#: bounce arrives later), and a caller that knows the declared heights should
#: say so rather than inherit this.
DEFAULT_GATE_MS = SEARCH_T_MAX_MS

#: Transform length for the residual and reference spectra. Fixed, so bin
#: density never varies with the gate length being compared; the padding is
#: interpolation only.
RESIDUAL_N_FFT = 1 << 16

#: Below this GCC-PHAT primary-over-secondary margin the alignment is not
#: trusted and every band reads :data:`VERDICT_UNRESOLVED` -- a subtraction
#: aligned by a peak that could be one of several is not a measurement.
ALIGNMENT_CONFIDENCE_FLOOR = 0.25

#: A residual at or above this level, relative to the direct, is "large".
#: Well above the alignment budget's own floor across the close-reference
#: band (2*sin(pi*f*dt) at 1 kHz and 5 us is -26 dB), so a room verdict is
#: never a re-reading of the tool's own timing error.
ROOM_RESIDUAL_FLOOR_DB = -12.0

#: Fewest analysis-grid points a band must keep after the validity
#: intersection before it is graded rather than left unresolved.
MIN_BAND_POINTS = 16

VERDICT_AGREEMENT = "agreement"
VERDICT_ROOM_DOMINATED = "room_dominated"
VERDICT_UNRESOLVED = "unresolved"

#: Named reasons a band is unresolved, so a reader is told which one.
UNRESOLVED_LOW_CONFIDENCE = "alignment_confidence_below_floor"
UNRESOLVED_OUTSIDE_VALIDITY = "band_outside_validity"
UNRESOLVED_RESIDUAL_SMALL = "disagreement_without_residual"
UNRESOLVED_NO_CANCELLATION = "agreement_without_cancellation"

WINDOW_FAR = "far_window"
WINDOW_CLOSE = "close_window"


# --------------------------------------------------------------------------- #
# where to stand
# --------------------------------------------------------------------------- #


def far_field_ceiling_hz(
    diameter_m: float,
    distance_m: float,
    *,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> float:
    """Highest frequency at which ``distance_m`` is still the driver's far field.

    The piston far-field (Rayleigh) distance is ``2*a**2/lambda`` for aperture
    RADIUS ``a`` (Keele's near-field limit and D'Appolito's ``f_max = 4311/D``
    are the same criterion in other clothes), and it GROWS with frequency, so
    solving ``distance_m >= 2*a**2*f/c`` for ``f`` gives a CEILING, not a
    floor: a close mic is near-field at HIGH frequencies, never at low ones.
    """
    radius = 0.5 * float(diameter_m)
    if radius <= 0.0:
        raise ValueError(f"diameter must be positive, got {diameter_m}")
    return float(sound_speed_m_s) * float(distance_m) / (2.0 * radius**2)


def placement_tolerance_db(
    distance_m: float, *, tolerance_m: float = PLACEMENT_TOLERANCE_M
) -> float:
    """What ``+/- tolerance_m`` of mic placement costs the 1/r correction, dB."""
    return 20.0 * math.log10((float(distance_m) + float(tolerance_m)) / float(distance_m))


def recommended_distance(
    diameter_m: float,
    fc_hz: float,
    *,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> dict[str, Any]:
    """Where to put the mic for a close reference of this driver.

    ``r = 2*a**2/lambda_top + K_MARGIN*diameter``, evaluated at the top of the
    close capture's validity band (``f_top = fc/2``, issue #3501). Both terms
    are returned separately: the margin dominates and the far-field term is
    the correction, and a reader who cannot see that split cannot tell whether
    a surprising answer came from the driver's size or from its crossover.
    """
    diameter = float(diameter_m)
    if diameter <= 0.0:
        raise ValueError(f"diameter must be positive, got {diameter_m}")
    if fc_hz <= 0.0:
        raise ValueError(f"fc must be positive, got {fc_hz}")
    f_top = 0.5 * float(fc_hz)
    lambda_top = float(sound_speed_m_s) / f_top
    radius = 0.5 * diameter
    far_field_m = 2.0 * radius**2 / lambda_top
    margin_m = K_MARGIN * diameter
    distance_m = far_field_m + margin_m
    return {
        "driver_diameter_m": diameter,
        "driver_diameter_in": diameter / 0.0254,
        "fc_hz": float(fc_hz),
        "band_top_hz": f_top,
        "wavelength_top_m": lambda_top,
        "far_field_term_m": far_field_m,
        "margin_term_m": margin_m,
        "k_margin": K_MARGIN,
        "distance_m": distance_m,
        "distance_in": distance_m / 0.0254,
        "direct_gain_over_1m_db": 20.0 * math.log10(1.0 / distance_m),
        "placement_tolerance_m": PLACEMENT_TOLERANCE_M,
        "placement_tolerance_db": placement_tolerance_db(distance_m),
        "aim_tolerance_deg": AIM_TOLERANCE_DEG,
        "far_field_ceiling_hz": far_field_ceiling_hz(
            diameter, distance_m, sound_speed_m_s=sound_speed_m_s
        ),
        "sound_speed_m_s": float(sound_speed_m_s),
    }


def cancellation_depth_db(f_hz: float, lag_s: float) -> float:
    """How deep a subtraction can go at ``f_hz`` given a timing error ``lag_s``.

    ``residual/direct = |1 - exp(-j*2*pi*f*dt)| = 2*|sin(pi*f*dt)|`` --
    gate-research-results.md document 2, section B3 (derived there from first
    principles, not cited to a paper).
    """
    ratio = 2.0 * abs(math.sin(math.pi * float(f_hz) * float(lag_s)))
    if ratio <= 0.0:
        return -math.inf
    return 20.0 * math.log10(ratio)


# --------------------------------------------------------------------------- #
# the comparison
# --------------------------------------------------------------------------- #


def _fractional_shift(x: np.ndarray, samples: float) -> np.ndarray:
    """Shift ``x`` right by ``samples`` (may be fractional) via linear phase."""
    n = x.size
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n)
    return np.fft.irfft(spectrum * np.exp(-2j * np.pi * freqs * samples), n=n)


def _windowed(
    ir: np.ndarray,
    *,
    peak_idx: int,
    span: int,
    lead: int,
) -> np.ndarray:
    """The pipeline gate applied at a DECLARED span, sliced to its own support.

    ``lead`` is :func:`~jasper.audio_measurement.gating.build_gate_window`'s
    own raised-cosine head: unbounded, the window would run to the array start
    and a slice of it would open on a hard edge.
    """
    n = ir.size
    span = min(span, n - 1 - peak_idx)
    win = build_gate_window(n, peak_idx=peak_idx, span=span, lead=lead)
    start = max(0, peak_idx - lead)
    return (ir * win)[start : peak_idx + span + 1]


def _band_spectra(
    segment: np.ndarray, sample_rate: int
) -> tuple[np.ndarray, np.ndarray]:
    """``(freqs, power)`` of one gated segment on the fixed residual grid."""
    spectrum = np.fft.rfft(segment, n=RESIDUAL_N_FFT)
    freqs = np.fft.rfftfreq(RESIDUAL_N_FFT, d=1.0 / sample_rate)
    return freqs, np.abs(spectrum) ** 2


def _power_ratio_db(
    numerator: np.ndarray, denominator: np.ndarray, mask: np.ndarray
) -> float:
    num = float(np.mean(numerator[mask])) if mask.any() else 0.0
    den = float(np.mean(denominator[mask])) if mask.any() else 0.0
    if den <= 0.0 or num <= 0.0:
        return -math.inf
    return 10.0 * math.log10(num / den)


def _verdict(
    *,
    points: int,
    rms_delta_db: float,
    tolerance_db: float,
    residual_rel_direct_db: float,
    alignment_trusted: bool,
) -> tuple[str, str | None]:
    if points < MIN_BAND_POINTS:
        return VERDICT_UNRESOLVED, UNRESOLVED_OUTSIDE_VALIDITY
    if not alignment_trusted:
        return VERDICT_UNRESOLVED, UNRESOLVED_LOW_CONFIDENCE
    # Shape and level both have to agree. The detrended delta is blind to
    # level, so a wrongly declared distance reads as agreeing shapes over a
    # subtraction that never cancelled; that is a finding about the
    # declaration, not a verdict about the speaker.
    within_tolerance = rms_delta_db <= tolerance_db
    cancelled = residual_rel_direct_db < ROOM_RESIDUAL_FLOOR_DB
    if within_tolerance and cancelled:
        return VERDICT_AGREEMENT, None
    if not within_tolerance and not cancelled:
        return VERDICT_ROOM_DOMINATED, None
    if within_tolerance:
        return VERDICT_UNRESOLVED, UNRESOLVED_NO_CANCELLATION
    return VERDICT_UNRESOLVED, UNRESOLVED_RESIDUAL_SMALL


def _bands(
    *,
    grid: np.ndarray,
    far_curve: np.ndarray,
    close_curve: np.ndarray,
    freqs: np.ndarray,
    far_power: np.ndarray,
    direct_power: np.ndarray,
    residual_power: np.ndarray,
    comparison_band_hz: tuple[float, float],
    alignment_trusted: bool,
) -> list[dict[str, Any]]:
    lo_edge, hi_edge = comparison_band_hz
    delta = close_curve - far_curve
    out: list[dict[str, Any]] = []
    for nominal_lo, nominal_hi, tolerance_db in SPEC_BANDS:
        lo = max(float(nominal_lo), lo_edge)
        hi = min(float(nominal_hi), hi_edge)
        in_band = (grid >= lo) & (grid < hi) if hi > lo else np.zeros_like(grid, bool)
        points = int(in_band.sum())
        bins = (freqs >= lo) & (freqs < hi) if hi > lo else np.zeros_like(freqs, bool)
        residual_rel_direct = _power_ratio_db(residual_power, direct_power, bins)
        residual_rel_far = _power_ratio_db(residual_power, far_power, bins)
        if points:
            worst = int(np.argmax(np.abs(far_curve[in_band])))
            worst_hz = float(grid[in_band][worst])
            worst_far_db = float(far_curve[in_band][worst])
            delta_at_worst_db = float(delta[in_band][worst])
            rms_delta_db = float(np.sqrt(np.mean(delta[in_band] ** 2)))
        else:
            worst_hz = worst_far_db = delta_at_worst_db = rms_delta_db = math.nan
        verdict, unresolved_reason = _verdict(
            points=points,
            rms_delta_db=rms_delta_db,
            tolerance_db=float(tolerance_db),
            residual_rel_direct_db=residual_rel_direct,
            alignment_trusted=alignment_trusted,
        )
        out.append({
            "nominal_band_hz": [float(nominal_lo), float(nominal_hi)],
            "graded_band_hz": [lo, hi] if hi > lo else None,
            "tolerance_db": float(tolerance_db),
            "points": points,
            "worst_far_bin_hz": worst_hz,
            "worst_far_deviation_db": worst_far_db,
            "delta_at_worst_db": delta_at_worst_db,
            "rms_delta_db": rms_delta_db,
            "residual_rel_direct_db": residual_rel_direct,
            "residual_rel_far_db": residual_rel_far,
            "verdict": verdict,
            "unresolved_reason": unresolved_reason,
        })
    return out


def compare_impulse_responses(
    far_ir: np.ndarray,
    close_ir: np.ndarray,
    *,
    sample_rate: int,
    far_m: float,
    close_m: float,
    fc_hz: float | None = None,
    driver_diameter_m: float | None = None,
    far_gate_ms: float = DEFAULT_GATE_MS,
    close_gate_ms: float = DEFAULT_GATE_MS,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> dict[str, Any]:
    """Correct the close IR to the far distance and say what the far read owed
    the room, band by band.

    Returns the whole report as plain data: ``frame``, ``geometry``,
    ``validity``, ``alignment`` and one ``windows`` entry per declared gate
    length, each carrying a :data:`SPEC_BANDS`-shaped band table. Nothing is
    printed and nothing is written.
    """
    far = np.asarray(far_ir, dtype=np.float64)
    close = np.asarray(close_ir, dtype=np.float64)
    if far.ndim != 1 or close.ndim != 1:
        raise ValueError("impulse responses must be 1-D")
    if far_m <= 0.0 or close_m <= 0.0 or close_m >= far_m:
        raise ValueError(
            f"need 0 < close_m < far_m; got close={close_m} far={far_m}"
        )
    sr = int(sample_rate)

    far_peak = int(np.argmax(np.abs(far)))
    close_peak = int(np.argmax(np.abs(close)))

    # 1/r on the direct, then the geometric excess path, then what the signal
    # itself says. The geometric delay is a PREDICTION the measured shift is
    # published against, never a substitute for it: back-to-back runs carry
    # their own interface latency.
    scale = close_m / far_m
    geometric_delay_s = (far_m - close_m) / float(sound_speed_m_s)

    corrected = np.zeros_like(far)
    shift_int = far_peak - close_peak
    src_lo = max(0, -shift_int)
    src_hi = min(close.size, far.size - shift_int)
    if src_hi > src_lo:
        corrected[src_lo + shift_int : src_hi + shift_int] = (
            close[src_lo:src_hi] * scale
        )

    far_span = int(round(far_gate_ms * 1e-3 * sr))
    close_span = int(round(close_gate_ms * 1e-3 * sr))
    lead = int(round(PHASE_GATE_LEAD_MS * 1e-3 * sr))
    align_span = max(far_span, close_span)

    # Validity: the gate's own resolution floor at the bottom, and the lower of
    # the crossover's half and the driver's far-field ceiling at the top.
    trusted_far_hz = f_trusted_floor_hz(far_span / sr)
    trusted_close_hz = f_trusted_floor_hz(close_span / sr)
    ceiling_hz = (
        far_field_ceiling_hz(
            driver_diameter_m, close_m, sound_speed_m_s=sound_speed_m_s
        )
        if driver_diameter_m
        else math.inf
    )
    band_top_hz = 0.5 * float(fc_hz) if fc_hz else math.inf
    lo_edge = max(GRID_LO_HZ, trusted_far_hz)
    hi_edge = min(GRID_HI_HZ, ceiling_hz, band_top_hz)
    comparison_band_hz = (lo_edge, max(lo_edge, hi_edge))

    align_lo, align_hi = comparison_band_hz
    far_align = _windowed(far, peak_idx=far_peak, span=align_span, lead=lead)
    close_align = _windowed(
        corrected, peak_idx=far_peak, span=align_span, lead=lead
    )
    lag, _sign, confidence, at_edge = gcc_phat(
        far_align,
        close_align,
        sample_rate=sr,
        band_hz=(align_lo, align_hi),
        upsample=GCC_UPSAMPLE,
        max_lag_samples=float(lead + align_span),
    )
    corrected = _fractional_shift(corrected, lag)
    refined = gcc_phat(
        far_align,
        _windowed(corrected, peak_idx=far_peak, span=align_span, lead=lead),
        sample_rate=sr,
        band_hz=(align_lo, align_hi),
        upsample=GCC_UPSAMPLE,
        max_lag_samples=float(lead + align_span),
    )[0]

    measured_shift_s = (shift_int + lag) / sr
    residual_lag_s = refined / sr
    lag_floor_s = 1.0 / (sr * GCC_UPSAMPLE)
    alignment_trusted = bool(
        confidence >= ALIGNMENT_CONFIDENCE_FLOOR and not at_edge
    )

    grid = analysis_grid()
    windows: list[dict[str, Any]] = []
    for name, gate_ms, span in (
        (WINDOW_FAR, far_gate_ms, far_span),
        (WINDOW_CLOSE, close_gate_ms, close_span),
    ):
        far_seg = _windowed(far, peak_idx=far_peak, span=span, lead=lead)
        close_seg = _windowed(corrected, peak_idx=far_peak, span=span, lead=lead)
        residual_seg = far_seg - close_seg
        freqs, far_power = _band_spectra(far_seg, sr)
        _, direct_power = _band_spectra(close_seg, sr)
        _, residual_power = _band_spectra(residual_seg, sr)
        # Each window grades down to ITS OWN resolution floor: the close
        # capture's longer clean window is the whole reason to take it.
        window_band = (max(GRID_LO_HZ, f_trusted_floor_hz(span / sr)), hi_edge)
        windows.append({
            "name": name,
            "gate_ms": float(gate_ms),
            "trusted_floor_hz": f_trusted_floor_hz(span / sr),
            "comparison_band_hz": list(window_band),
            "bands": _bands(
                grid=grid,
                far_curve=detrend(smoothed_curve(far_seg, sr, grid), grid),
                close_curve=detrend(smoothed_curve(close_seg, sr, grid), grid),
                freqs=freqs,
                far_power=far_power,
                direct_power=direct_power,
                residual_power=residual_power,
                comparison_band_hz=(
                    window_band[0], max(window_band[0], window_band[1])
                ),
                alignment_trusted=alignment_trusted,
            ),
        })

    return {
        "schema_version": CLOSE_REFERENCE_SCHEMA_VERSION,
        "generated_by": GENERATED_BY,
        "frame": {
            "window_kind": "flat_to_peak_plateau_half_hann_tail",
            "taper_fraction": TAPER_FRACTION,
            "gate_lead_ms": PHASE_GATE_LEAD_MS,
            "far_gate_ms": float(far_gate_ms),
            "close_gate_ms": float(close_gate_ms),
            "smooth_fraction": MAGNITUDE_SMOOTH_FRACTION,
            "detrend_fraction": DETREND_FRACTION,
            "grid_hz": [GRID_LO_HZ, GRID_HI_HZ],
            "grid_points": int(grid.size),
            "n_fft": RESIDUAL_N_FFT,
            "alignment_band_hz": [align_lo, align_hi],
            "gcc_upsample": GCC_UPSAMPLE,
            "sample_rate": sr,
            "sound_speed_m_s": float(sound_speed_m_s),
            "room_residual_floor_db": ROOM_RESIDUAL_FLOOR_DB,
            "alignment_confidence_floor": ALIGNMENT_CONFIDENCE_FLOOR,
        },
        "geometry": {
            "far_m": float(far_m),
            "close_m": float(close_m),
            "inverse_r_scale_db": 20.0 * math.log10(scale),
            "direct_gain_over_room_db": -20.0 * math.log10(scale),
            "geometric_delay_us": geometric_delay_s * 1e6,
            "placement_tolerance_db": placement_tolerance_db(close_m),
            "aim_tolerance_deg": AIM_TOLERANCE_DEG,
        },
        "validity": {
            "driver_diameter_m": (
                float(driver_diameter_m) if driver_diameter_m else None
            ),
            "fc_hz": float(fc_hz) if fc_hz else None,
            "band_top_hz": None if math.isinf(band_top_hz) else band_top_hz,
            "far_field_ceiling_hz": None if math.isinf(ceiling_hz) else ceiling_hz,
            "trusted_floor_far_hz": trusted_far_hz,
            "trusted_floor_close_hz": trusted_close_hz,
            # The FAR window's band, which is also the alignment band. Each
            # window entry publishes its own.
            "comparison_band_hz": list(comparison_band_hz),
        },
        "alignment": {
            "far_direct_peak_ms": 1000.0 * far_peak / sr,
            "close_direct_peak_ms": 1000.0 * close_peak / sr,
            "measured_shift_us": measured_shift_s * 1e6,
            "geometric_delay_us": geometric_delay_s * 1e6,
            "measured_minus_geometric_us": (
                measured_shift_s - geometric_delay_s
            ) * 1e6,
            "residual_lag_us": residual_lag_s * 1e6,
            # The refine cannot resolve below one upsampled correlation bin, so
            # a residual that reads zero means "under the floor", not "exact".
            # The budget is priced at the floor for that reason: a subtraction
            # advertised as infinitely deep is a promise the instrument cannot
            # keep.
            "residual_lag_floor_us": lag_floor_s * 1e6,
            "confidence": float(confidence),
            "at_search_edge": bool(at_edge),
            "trusted": alignment_trusted,
            "cancellation_budget_db": [
                {
                    "f_hz": edge,
                    "depth_db": cancellation_depth_db(
                        edge, max(abs(residual_lag_s), lag_floor_s)
                    ),
                }
                for edge in comparison_band_hz
            ],
        },
        "windows": windows,
    }


# --------------------------------------------------------------------------- #
# binding a banked round to the two impulse responses
# --------------------------------------------------------------------------- #

#: Where a wired round banks its summed captures: sidecar JSON with the WAV
#: beside it, one directory per bundle.
SUMMED_SIDECAR_GLOB = "**/summed/summed_*.json"

#: How the program a capture was played through is named on disk.
PROGRAM_WAV_GLOB = "**/*_program.wav"

REFUSE_UNREADABLE_ROUND = "close_reference_unreadable_round"
REFUSE_NO_CAPTURE = "close_reference_no_capture"
REFUSE_PROGRAM_UNMATCHED = "close_reference_program_unmatched"
REFUSE_RATE_MISMATCH = "close_reference_rate_mismatch"


class CloseReferenceRefused(RuntimeError):
    """This round cannot answer, and ``reason`` says which input is missing."""

    def __init__(self, reason: str, detail: Mapping[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail: dict[str, Any] = dict(detail or {})


@dataclass(frozen=True)
class RoundCapture:
    """One banked summed capture, bound to the program its BYTES were played
    through.

    The binding is ``provenance.stimulus.wav_sha256`` against the sha256 of
    each banked program WAV — never ``provenance.stimulus.phase``, which
    #3504 observed declaring ``verify`` on five captures whose bytes were
    ``cloud_verify_program.wav``.
    """

    sidecar: Path
    wav: Path
    program: Path
    take_id: str
    phase: str
    position_deg: int | None
    vertical_deg: int | None
    mark_distance_m: float | None
    stimulus_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "take_id": self.take_id,
            "phase": self.phase,
            "sidecar": self.sidecar.name,
            "wav": self.wav.name,
            "program": self.program.name,
            "position_deg": self.position_deg,
            "vertical_deg": self.vertical_deg,
            "mark_distance_m": self.mark_distance_m,
            "stimulus_wav_sha256": self.stimulus_sha256,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(
        value, bool
    ) else None


def load_round_capture(round_dir: Path, *, capture_id: str | None = None) -> RoundCapture:
    """The capture this comparison reads out of ``round_dir``, bound by content.

    ``capture_id`` selects by take id (or by any sidecar-stem substring). With
    none, the on-axis summed capture wins: ``position_deg == 0`` and
    ``vertical_deg == 0``, earliest take id. Raises
    :class:`CloseReferenceRefused` rather than guessing.
    """
    root = Path(round_dir)
    if not root.is_dir():
        raise CloseReferenceRefused(
            REFUSE_UNREADABLE_ROUND, {"round_dir": str(root)}
        )
    programs = {_sha256(path): path for path in sorted(root.glob(PROGRAM_WAV_GLOB))}
    sidecars = sorted(root.glob(SUMMED_SIDECAR_GLOB))
    if not sidecars:
        raise CloseReferenceRefused(
            REFUSE_NO_CAPTURE,
            {"round_dir": str(root), "glob": SUMMED_SIDECAR_GLOB},
        )

    census: list[dict[str, Any]] = []
    matched: list[RoundCapture] = []
    unmatched: list[dict[str, Any]] = []
    for sidecar in sidecars:
        try:
            doc = json.loads(sidecar.read_text())
        except (OSError, UnicodeDecodeError, ValueError):
            census.append({"sidecar": sidecar.name, "admitted": False})
            continue
        if not isinstance(doc, Mapping):
            census.append({"sidecar": sidecar.name, "admitted": False})
            continue
        take_id = str(doc.get("take_id") or sidecar.stem)
        if capture_id is not None and capture_id not in (take_id, sidecar.stem):
            continue
        wav = sidecar.with_suffix(".wav")
        provenance = doc.get("provenance")
        stimulus = (
            provenance.get("stimulus") if isinstance(provenance, Mapping) else None
        )
        sha = stimulus.get("wav_sha256") if isinstance(stimulus, Mapping) else None
        program = programs.get(sha) if isinstance(sha, str) else None
        position_deg = _int_or_none(doc.get("position_deg"))
        vertical_deg = _int_or_none(doc.get("vertical_deg"))
        row: dict[str, Any] = {
            "sidecar": sidecar.name,
            "take_id": take_id,
            "position_deg": position_deg,
            "vertical_deg": vertical_deg,
            "stimulus_wav_sha256": sha if isinstance(sha, str) else None,
            "admitted": bool(program is not None and wav.is_file()),
        }
        census.append(row)
        if program is None or not wav.is_file():
            unmatched.append(row)
            continue
        matched.append(
            RoundCapture(
                sidecar=sidecar,
                wav=wav,
                program=program,
                take_id=take_id,
                phase=str(doc.get("phase") or ""),
                position_deg=position_deg,
                vertical_deg=vertical_deg,
                mark_distance_m=(
                    float(doc["mark_distance_m"])
                    if isinstance(doc.get("mark_distance_m"), (int, float))
                    and not isinstance(doc.get("mark_distance_m"), bool)
                    else None
                ),
                stimulus_sha256=str(sha),
            )
        )

    if not matched:
        raise CloseReferenceRefused(
            REFUSE_PROGRAM_UNMATCHED if unmatched else REFUSE_NO_CAPTURE,
            {
                "round_dir": str(root),
                "capture_id": capture_id,
                "programs_present": sorted(path.name for path in programs.values()),
                "captures": census,
            },
        )
    if capture_id is not None:
        return matched[0]
    on_axis = [
        capture
        for capture in matched
        if capture.position_deg == 0 and capture.vertical_deg == 0
    ]
    if not on_axis:
        raise CloseReferenceRefused(
            REFUSE_NO_CAPTURE,
            {
                "round_dir": str(root),
                "note": "no capture declares position_deg 0 / vertical_deg 0",
                "captures": census,
            },
        )
    return sorted(on_axis, key=lambda capture: capture.take_id)[0]


def capture_impulse_response(capture: RoundCapture) -> tuple[np.ndarray, int]:
    """Deconvolve one banked capture against the program its bytes name."""
    signal, rate = read_wav_mono(capture.wav)
    program, program_rate = read_wav_mono(capture.program)
    if rate != program_rate:
        raise CloseReferenceRefused(
            REFUSE_RATE_MISMATCH,
            {
                "capture_hz": rate,
                "program_hz": program_rate,
                "wav": capture.wav.name,
                "program": capture.program.name,
            },
        )
    ir = regularized_deconvolution_full(
        np.asarray(signal, dtype=np.float32),
        np.asarray(program, dtype=np.float32),
        rate,
    )
    return np.asarray(ir, dtype=np.float64), int(rate)


def compare_rounds(
    far_round: Path,
    close_round: Path,
    *,
    far_m: float,
    close_m: float,
    far_capture_id: str | None = None,
    close_capture_id: str | None = None,
    fc_hz: float | None = None,
    driver_diameter_m: float | None = None,
    far_gate_ms: float = DEFAULT_GATE_MS,
    close_gate_ms: float = DEFAULT_GATE_MS,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> dict[str, Any]:
    """Two banked rounds in, one close-reference report out."""
    far_capture = load_round_capture(far_round, capture_id=far_capture_id)
    close_capture = load_round_capture(close_round, capture_id=close_capture_id)
    far_ir, far_rate = capture_impulse_response(far_capture)
    close_ir, close_rate = capture_impulse_response(close_capture)
    if far_rate != close_rate:
        raise CloseReferenceRefused(
            REFUSE_RATE_MISMATCH, {"far_hz": far_rate, "close_hz": close_rate}
        )
    report = compare_impulse_responses(
        far_ir,
        close_ir,
        sample_rate=far_rate,
        far_m=far_m,
        close_m=close_m,
        fc_hz=fc_hz,
        driver_diameter_m=driver_diameter_m,
        far_gate_ms=far_gate_ms,
        close_gate_ms=close_gate_ms,
        sound_speed_m_s=sound_speed_m_s,
    )
    report["captures"] = {
        "far": far_capture.to_dict() | {"round_dir": str(far_round)},
        "close": close_capture.to_dict() | {"round_dir": str(close_round)},
    }
    # The sidecar's own mark_distance_m is published beside the declared one
    # rather than silently overridden: today it is pinned to 1.0 for every
    # pose, and #3498 is where a per-row distance lands.
    report["geometry"]["sidecar_mark_distance_m"] = {
        "far": far_capture.mark_distance_m,
        "close": close_capture.mark_distance_m,
    }
    report["geometry"]["declared_distance_source"] = "caller"
    report["geometry"]["sidecar_disagrees"] = bool(
        (far_capture.mark_distance_m is not None
         and abs(far_capture.mark_distance_m - far_m) > 1e-9)
        or (close_capture.mark_distance_m is not None
            and abs(close_capture.mark_distance_m - close_m) > 1e-9)
    )
    return report
