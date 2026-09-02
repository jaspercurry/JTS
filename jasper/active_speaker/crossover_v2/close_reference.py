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

1. **Where to stand.** The piston geometry lives beside its sibling in
   :mod:`jasper.active_speaker.branch_chain`
   (:func:`~jasper.active_speaker.branch_chain.recommended_distance`), read
   from the driver's diameter and crossover corner.
2. **The correction.** Scale the close IR by ``r_close/r_far``, delay it by the
   geometric ``(r_far - r_close)/c``, then SUB-SAMPLE align it to the far IR's
   own direct arrival. A subtraction cancels only to the depth its phase error
   allows — ``residual/direct = 2*|sin(pi*f*dt)|`` (gate-research-results.md,
   document 2 section B3) — so the achieved lag and the cancellation-depth
   budget at the band edges are published beside every number they bound.
3. **The verdict, per band.** Where the corrected close read agrees with the
   far read, the far read was speaker-dominated; where they disagree and the
   subtraction residual is large, the far read was the room.

**The gate has three sources and says which one it had.** A declared rig
geometry (:class:`~jasper.audio_measurement.measurement_geometry.DeclaredGeometry`)
gives each window its own first bounce, and the close capture's is the longer
one because that excess path grows as the direct path shrinks. An explicit
gate overrides that; with neither, the gate is the pipeline's own
reflection-search ceiling. Whichever it was, the window each capture's
declared geometry allows is published beside the gate actually used.

**Every number carries its frame.** Window shape, lead, taper, smoothing
fraction, grid, transform length, alignment band and upsample ride in the
report's ``frame`` block: one banked feature reads a materially different
depth under each defensible frame (#3495; the evidence is
``captures/recommission-day2-2026-09-01/p1-position-window/P1-REPORT.md``).

Distances are DECLARED by the caller, never read from the sidecar: today's
sidecars pin ``mark_distance_m = 1.0`` for every pose (a per-row distance
lands on #3498). Where a sidecar disagrees with the declared value, both are
published and the declared one is used.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.branch_chain import (
    AIM_TOLERANCE_DEG,
    far_field_ceiling_hz,
    placement_tolerance_db,
)
from jasper.active_speaker.flat_spec import SPEC_BANDS
from jasper.audio_measurement.alignment import GCC_UPSAMPLE, gcc_phat
from jasper.audio_measurement.gating import (
    SEARCH_T_MAX_MS,
    TAPER_FRACTION,
    f_trusted_floor_hz,
)
from jasper.audio_measurement.measurement_geometry import (
    DeclaredGeometry,
    GeometryFieldError,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S

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
from .gate_sweep import gated_segment
from .round_captures import (
    PoseCapture,
    RoundCapturesRefused,
    discover_captures,
    doc_pose_key,
)

__all__ = [
    "ALIGNMENT_CONFIDENCE_FLOOR",
    "CLOSE_REFERENCE_SCHEMA_VERSION",
    "DEFAULT_GATE_MS",
    "MIN_BAND_POINTS",
    "RESIDUAL_FLOOR_DB",
    "RESIDUAL_N_FFT",
    "ROOM_RESIDUAL_FLOOR_DB",
    "VERDICT_AGREEMENT",
    "VERDICT_ROOM_DOMINATED",
    "VERDICT_UNRESOLVED",
    "GENERATED_BY",
    "cancellation_depth_db",
    "compare_impulse_responses",
    "compare_rounds",
    "declared_clean_window_ms",
    "select_capture",
]

CLOSE_REFERENCE_SCHEMA_VERSION = 1

#: The tool a reader re-runs to reproduce a report.
GENERATED_BY = "jasper-close-reference"

#: Gate length, ms, with neither an explicit gate nor a declared geometry to
#: derive one from: the pipeline's own reflection-search ceiling, the longest
#: window the product ever calls reflection-free.
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
#: band (2*sin(pi*f*dt) at 1 kHz and 5 us is -30.1 dB), so a room verdict is
#: never a re-reading of the tool's own timing error.
ROOM_RESIDUAL_FLOOR_DB = -12.0

#: What a residual power at or below zero reads as: the best a subtraction
#: can promise, not an absent measurement -- a perfectly cancelled band is
#: the goal, not a hole in the data.
RESIDUAL_FLOOR_DB = -120.0

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

#: Refused before any computation: a non-finite or non-positive explicit gate
#: makes :func:`~jasper.audio_measurement.gating.f_trusted_floor_hz` return
#: ``+inf``, which the strict JSON writer cannot carry.
REFUSE_GATE_NOT_POSITIVE = "close_reference_gate_not_positive"


# --------------------------------------------------------------------------- #
# what a subtraction can promise
# --------------------------------------------------------------------------- #


def cancellation_depth_db(f_hz: float, lag_s: float) -> float | None:
    """How deep a subtraction can go at ``f_hz`` given a timing error ``lag_s``,
    or ``None`` when the ratio is zero and the depth is unbounded.

    ``residual/direct = |1 - exp(-j*2*pi*f*dt)| = 2*|sin(pi*f*dt)|`` --
    gate-research-results.md document 2, section B3 (derived there from first
    principles, not cited to a paper). ``-Infinity`` is not a number JSON can
    carry, so an unbounded depth publishes ``null`` rather than a token a
    strict parser rejects.
    """
    ratio = 2.0 * abs(math.sin(math.pi * float(f_hz) * float(lag_s)))
    if ratio <= 0.0:
        return None
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


#: Where the gate length actually used came from.
GATE_SOURCE_CALLER = "caller"
GATE_SOURCE_DECLARED = "declared_geometry"
GATE_SOURCE_DEFAULT = "default"


def _gate(explicit_ms: float | None, declared_ms: float | None) -> tuple[float, str]:
    """The gate this window is cut at, and which of the three said so."""
    if explicit_ms is not None:
        return float(explicit_ms), GATE_SOURCE_CALLER
    if declared_ms is not None:
        return float(declared_ms), GATE_SOURCE_DECLARED
    return DEFAULT_GATE_MS, GATE_SOURCE_DEFAULT


def _segment(
    ir: np.ndarray, sample_rate: int, *, gate_ms: float, peak_idx: int
) -> np.ndarray:
    """The pipeline gate at a DECLARED length, sliced to its own support.

    :func:`~.gate_sweep.gated_segment` is the one owner of a forced-span
    window, lead included; this comparison gates two IRs with it at the same
    length so that what survives the subtraction is the speaker, not the two
    windows disagreeing.
    """
    return gated_segment(
        ir, sample_rate, gate_ms=gate_ms, peak_idx=peak_idx,
        lead_ms=PHASE_GATE_LEAD_MS,
    )[0]


def declared_clean_window_ms(
    geometry: DeclaredGeometry, distance_m: float
) -> float | None:
    """This rig's reflection-free window at ``distance_m``, ms, or ``None``.

    The first bounce's EXCESS path over the direct one grows as the mic nears
    the speaker, so a close capture's clean window is longer than the far
    one's — which is half of why the close capture can say anything the far
    one cannot. ``None`` when the declared geometry cannot be evaluated there
    at all (:class:`~jasper.audio_measurement.measurement_geometry.DeclaredGeometry`
    accepts 0.15-3 m), so a mic closer than the declaration allows keeps the
    caller's gate — or the default — rather than inventing a window.
    """
    try:
        return 1e3 * replace(geometry, distance_m=float(distance_m)).first_bounce_s()
    except GeometryFieldError:
        return None


def _band_spectra(
    segment: np.ndarray, sample_rate: int
) -> tuple[np.ndarray, np.ndarray]:
    """``(freqs, power)`` of one gated segment on the fixed residual grid."""
    spectrum = np.fft.rfft(segment, n=RESIDUAL_N_FFT)
    freqs = np.fft.rfftfreq(RESIDUAL_N_FFT, d=1.0 / sample_rate)
    return freqs, np.abs(spectrum) ** 2


def _power_ratio_db(
    numerator: np.ndarray, denominator: np.ndarray, mask: np.ndarray
) -> float | None:
    """The band's power ratio in dB, or ``None`` when there is no data to read.

    A band with no bins, or with no reference power to divide by, has no
    answer -- and ``-Infinity`` is not a number JSON can carry, so the report
    publishes ``null`` rather than a token a strict parser rejects. A
    numerator at or below zero is not the same absence: a perfectly
    cancelled residual is the best possible read, so it floors at
    :data:`RESIDUAL_FLOOR_DB` rather than reading as no data.
    """
    if not mask.any():
        return None
    num = float(np.mean(numerator[mask]))
    den = float(np.mean(denominator[mask]))
    if den <= 0.0:
        return None
    if num <= 0.0:
        return RESIDUAL_FLOOR_DB
    return 10.0 * math.log10(num / den)


# This verb publishes a per-band VERDICT by design: #3501 asked for the
# comparison, not for more evidence. Its sibling `gate_sweep` publishes
# evidence only and leaves the attribution to its reader; that difference is
# deliberate, not drift.
def _verdict(
    *,
    points: int,
    rms_delta_db: float | None,
    tolerance_db: float,
    residual_rel_direct_db: float | None,
    alignment_trusted: bool,
) -> tuple[str, str | None]:
    # A missing delta or residual is the same state a thin band is in: nothing
    # to grade. Both are `None` exactly when the band kept no points/bins.
    if (
        points < MIN_BAND_POINTS
        or rms_delta_db is None
        or residual_rel_direct_db is None
    ):
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
        worst_hz: float | None = None
        worst_far_db: float | None = None
        delta_at_worst_db: float | None = None
        rms_delta_db: float | None = None
        if points:
            worst = int(np.argmax(np.abs(far_curve[in_band])))
            worst_hz = float(grid[in_band][worst])
            worst_far_db = float(far_curve[in_band][worst])
            delta_at_worst_db = float(delta[in_band][worst])
            rms_delta_db = float(np.sqrt(np.mean(delta[in_band] ** 2)))
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
    far_gate_ms: float | None = None,
    close_gate_ms: float | None = None,
    geometry: DeclaredGeometry | None = None,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> dict[str, Any]:
    """Correct the close IR to the far distance and say what the far read owed
    the room, band by band.

    Returns the whole report as plain data: ``frame``, ``geometry``,
    ``validity``, ``alignment`` and one ``windows`` entry per declared gate
    length, each carrying a :data:`SPEC_BANDS`-shaped band table. Nothing is
    printed and nothing is written.

    **Both alignment segments are cut at the SHORTER of the two gates**
    (published as ``alignment.alignment_gate_ms``): the far capture's clean
    window is the bound because its first bounce arrives earliest, and one
    lag aligned over a far segment that reached into room arrivals is then
    inherited by every band of the corrected close IR.
    """
    for window_name, gate_ms in (
        (WINDOW_FAR, far_gate_ms), (WINDOW_CLOSE, close_gate_ms)
    ):
        if gate_ms is not None and not (math.isfinite(gate_ms) and gate_ms > 0.0):
            raise RoundCapturesRefused(
                REFUSE_GATE_NOT_POSITIVE,
                {"window": window_name, "gate_ms": gate_ms},
            )
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

    declared_far_ms = (
        declared_clean_window_ms(geometry, far_m) if geometry else None
    )
    declared_close_ms = (
        declared_clean_window_ms(geometry, close_m) if geometry else None
    )
    far_gate_ms, far_gate_source = _gate(far_gate_ms, declared_far_ms)
    close_gate_ms, close_gate_source = _gate(close_gate_ms, declared_close_ms)

    far_span = int(round(far_gate_ms * 1e-3 * sr))
    close_span = int(round(close_gate_ms * 1e-3 * sr))
    lead = int(round(PHASE_GATE_LEAD_MS * 1e-3 * sr))
    align_span = min(far_span, close_span)
    align_ms = min(far_gate_ms, close_gate_ms)

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
    far_align = _segment(far, sr, gate_ms=align_ms, peak_idx=far_peak)
    close_align = _segment(corrected, sr, gate_ms=align_ms, peak_idx=far_peak)
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
        _segment(corrected, sr, gate_ms=align_ms, peak_idx=far_peak),
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
    for name, gate_ms, span, gate_source, declared_ms in (
        (WINDOW_FAR, far_gate_ms, far_span, far_gate_source, declared_far_ms),
        (WINDOW_CLOSE, close_gate_ms, close_span, close_gate_source, declared_close_ms),
    ):
        far_seg = _segment(far, sr, gate_ms=gate_ms, peak_idx=far_peak)
        close_seg = _segment(corrected, sr, gate_ms=gate_ms, peak_idx=far_peak)
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
            "gate_source": gate_source,
            # The clean window the DECLARED heights allow at this window's own
            # distance, beside the gate actually used, so a reader can see a
            # caller's gate outrunning the rig's first bounce.
            "declared_clean_window_ms": declared_ms,
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
            "declared_geometry": None if geometry is None else {
                "speaker_height_m": geometry.speaker_height_m,
                "mic_height_m": geometry.mic_height_m,
                "declared_distance_m": geometry.distance_m,
                "ceiling_height_m": geometry.ceiling_height_m,
                "clean_window_far_ms": declared_far_ms,
                "clean_window_close_ms": declared_close_ms,
            },
        },
        "validity": {
            "driver_diameter_m": (
                float(driver_diameter_m) if driver_diameter_m else None
            ),
            "fc_hz": float(fc_hz) if fc_hz else None,
            "band_top_hz": None if math.isinf(band_top_hz) else band_top_hz,
            "far_field_ceiling_hz": None if math.isinf(ceiling_hz) else ceiling_hz,
            "trusted_floor_far_hz": (
                None if math.isinf(trusted_far_hz) else trusted_far_hz
            ),
            "trusted_floor_close_hz": (
                None if math.isinf(trusted_close_hz) else trusted_close_hz
            ),
            # The FAR window's band, which is also the alignment band. Each
            # window entry publishes its own.
            "comparison_band_hz": list(comparison_band_hz),
        },
        "alignment": {
            # The window BOTH segments were cut at: the shorter of the two
            # gates, so neither segment reaches past the far capture's own
            # first bounce into the room.
            "alignment_gate_ms": float(align_ms),
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
# which capture of a banked round this comparison reads
# --------------------------------------------------------------------------- #

#: Named refusals this comparison owns. Discovery's own — a round with no
#: captures, a capture whose declared program hash matches nothing — are
#: :mod:`.round_captures`'s and are raised there.
REFUSE_UNREADABLE_ROUND = "close_reference_unreadable_round"
REFUSE_NO_CAPTURE = "close_reference_no_capture"
REFUSE_RATE_MISMATCH = "close_reference_rate_mismatch"


def select_capture(
    round_dir: Path, *, capture_id: str | None = None
) -> PoseCapture:
    """The one capture this comparison reads out of ``round_dir``.

    ``capture_id`` selects by the capture's own id or its WAV stem. With none,
    the on-axis capture wins: azimuth 0, elevation 0, first by capture id.
    Raises :class:`~.round_captures.RoundCapturesRefused` rather than guessing.

    The choice is made on each sidecar DOC, through
    :func:`~.round_captures.discover_captures`'s ``select``, so the poses this
    comparison discards are never deconvolved. The predicate records what it
    was shown, which is what a refusal here has to name.
    """
    root = Path(round_dir)
    if not root.is_dir():
        raise RoundCapturesRefused(REFUSE_UNREADABLE_ROUND, {"round_dir": str(root)})
    seen: list[str] = []
    if capture_id is not None:
        def wanted(doc: Mapping[str, Any]) -> bool:
            declared = doc.get("position_id")
            seen.append(str(declared) if declared else "")
            # A sidecar that declares no id takes its capture id from its own
            # file name, which this predicate cannot see; it stays in and the
            # WAV-stem match below decides.
            return not declared or str(declared) == capture_id

        named = [
            capture
            for capture in discover_captures(root, select=wanted)
            if capture_id in (capture.capture_id, capture.wav.stem)
        ]
        if not named:
            raise RoundCapturesRefused(
                REFUSE_NO_CAPTURE,
                {
                    "round_dir": str(root),
                    "capture_id": capture_id,
                    "captures": seen,
                },
            )
        return named[0]

    def on_axis_doc(doc: Mapping[str, Any]) -> bool:
        seen.append(doc_pose_key(doc))
        # A pose declared as anything but a number compares False here, the
        # same answer the decoded ``None`` gave.
        return doc.get("position_deg") == 0 and doc.get("vertical_deg") == 0

    on_axis = discover_captures(root, select=on_axis_doc)
    if not on_axis:
        raise RoundCapturesRefused(
            REFUSE_NO_CAPTURE,
            {
                "round_dir": str(root),
                "note": "no capture declares azimuth 0 / elevation 0",
                "poses": seen,
            },
        )
    return on_axis[0]


def _capture_row(capture: PoseCapture) -> dict[str, Any]:
    """What the report says about a capture it read."""
    return {
        "capture_id": capture.capture_id,
        "phase": capture.phase,
        "pose_key": capture.pose_key,
        "wav": capture.wav.name,
        "program": capture.program.name,
        "position_deg": capture.azimuth_deg,
        "vertical_deg": capture.vertical_deg,
        "mark_distance_m": capture.mark_distance_m,
        "stimulus_wav_sha256": capture.program_sha256,
    }

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
    far_gate_ms: float | None = None,
    close_gate_ms: float | None = None,
    geometry: DeclaredGeometry | None = None,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> dict[str, Any]:
    """Two banked rounds in, one close-reference report out."""
    far_capture = select_capture(far_round, capture_id=far_capture_id)
    close_capture = select_capture(close_round, capture_id=close_capture_id)
    if far_capture.sample_rate != close_capture.sample_rate:
        raise RoundCapturesRefused(
            REFUSE_RATE_MISMATCH,
            {
                "far_hz": far_capture.sample_rate,
                "close_hz": close_capture.sample_rate,
            },
        )
    report = compare_impulse_responses(
        far_capture.ir,
        close_capture.ir,
        sample_rate=far_capture.sample_rate,
        far_m=far_m,
        close_m=close_m,
        fc_hz=fc_hz,
        driver_diameter_m=driver_diameter_m,
        far_gate_ms=far_gate_ms,
        close_gate_ms=close_gate_ms,
        geometry=geometry,
        sound_speed_m_s=sound_speed_m_s,
    )
    report["captures"] = {
        "far": _capture_row(far_capture) | {"round_dir": str(far_round)},
        "close": _capture_row(close_capture) | {"round_dir": str(close_round)},
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
