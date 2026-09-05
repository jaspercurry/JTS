# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A close capture, corrected to the far distance, says how much of the far
read was the room (#3501).

Between :func:`~jasper.audio_measurement.gating.f_trusted_floor_hz` and
:func:`~jasper.audio_measurement.gating.f_entanglement_floor_hz` ONE point
cannot separate speaker from room; the limit is information-theoretic, not
tooling, and one extra capture breaks it. Moving the mic from ~1 m to ~12 in
on the woofer axis — still inside the driver's far field — gains the direct
sound ``20*log10(r_far/r_close)`` on the room while the bounce paths hardly
move.
The close IR is scaled by ``r_close/r_far``, delayed by the geometric
``(r_far - r_close)/c`` and sub-sample aligned to the far IR's own direct
arrival; the subtraction cancels only to ``residual/direct =
2*|sin(pi*f*dt)|`` (gate-research-results.md document 2 section B3), so the
achieved lag and its cancellation-depth budget ride beside every number they
bound. Every number also carries the frame it was read under (#3495), and
distances are DECLARED by the caller, never read from the sidecar, which pins
``mark_distance_m = 1.0`` for every pose today (#3498).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.branch_chain import (
    AIM_TOLERANCE_DEG,
    far_field_ceiling_hz,
    placement_tolerance_db,
)
from jasper.active_speaker.flat_spec import SPEC_BANDS
from jasper.audio_measurement.alignment import (
    GCC_UPSAMPLE,
    fractional_shift,
    gcc_phat,
)
from jasper.audio_measurement.gating import (
    ENTANGLEMENT_SOURCE_DECLARED,
    TAPER_FRACTION,
    f_trusted_floor_hz,
    intersect_bands,
)
from jasper.audio_measurement.measurement_geometry import (
    DeclaredGeometry,
    GeometryFieldError,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S

from .feature_classifier import (
    CLASSIFICATION_GRID_HI_HZ,
    CLASSIFICATION_GRID_LO_HZ,
    DEFAULT_GATE_MS,
    classification_grid,
    smoothed_curve,
)
from .feature_optics import (
    DETREND_FRACTION,
    MAGNITUDE_SMOOTH_FRACTION,
    NEIGHBOURHOOD_OCT,
    PHASE_GATE_LEAD_MS,
    detrend,
)
from .gate_sweep import gated_segment
from .round_captures import (
    RoundCapturesRefused,
    capture_row,
    select_capture,
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
    "summary_lines",
]

CLOSE_REFERENCE_SCHEMA_VERSION = 1

#: The tool a reader re-runs to reproduce a report.
GENERATED_BY = "jasper-round-views close-reference"

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

#: Two rounds captured at different rates cannot be subtracted. The selector's
#: own two refusals live with it, in :mod:`.round_captures`.
REFUSE_RATE_MISMATCH = "close_reference_rate_mismatch"

#: Refused before any computation: a bin outside :data:`SPEC_BANDS` has no
#: tolerance to be graded against, and inventing one would publish a verdict
#: no spec ever stated.
REFUSE_AT_HZ_OFF_SPEC_TABLE = "close_reference_at_hz_off_spec_table"


def _narrow_band(at_hz: float | None) -> tuple[tuple[float, float, float], ...]:
    """The one narrow band a caller named, in :data:`SPEC_BANDS`' own shape.

    :data:`~.feature_optics.NEIGHBOURHOOD_OCT` wide either side -- the
    half-width every other reader of this grid states a feature over -- and
    graded against the tolerance of the SPEC band it sits inside, never a bar
    of its own: the narrow row is the spec row asked at one frequency, not a
    second standard.
    """
    if at_hz is None:
        return ()
    for lo_hz, hi_hz, tolerance_db in SPEC_BANDS:
        if lo_hz <= at_hz < hi_hz:
            return (
                (
                    at_hz * 2**-NEIGHBOURHOOD_OCT,
                    at_hz * 2**NEIGHBOURHOOD_OCT,
                    float(tolerance_db),
                ),
            )
    raise RoundCapturesRefused(
        REFUSE_AT_HZ_OFF_SPEC_TABLE,
        {"at_hz": at_hz, "spec_table_hz": [SPEC_BANDS[0][0], SPEC_BANDS[-1][1]]},
    )


def cancellation_depth_db(f_hz: float, lag_s: float) -> float | None:
    """How deep a subtraction can go at ``f_hz`` given a timing error ``lag_s``,
    or ``None`` when the ratio is zero and the depth is unbounded.

    ``residual/direct = |1 - exp(-j*2*pi*f*dt)| = 2*|sin(pi*f*dt)|`` --
    gate-research-results.md document 2, section B3. ``-Infinity`` is not a
    number JSON can carry, so an unbounded depth publishes ``null``.
    """
    ratio = 2.0 * abs(math.sin(math.pi * float(f_hz) * float(lag_s)))
    if ratio <= 0.0:
        return None
    return 20.0 * math.log10(ratio)


#: Where the gate length actually used came from. The declared word is the
#: gating vocabulary's, not a second spelling of it.
GATE_SOURCE_CALLER = "caller"
GATE_SOURCE_DEFAULT = "default"


def _gate(explicit_ms: float | None, declared_ms: float | None) -> tuple[float, str]:
    """The gate this window is cut at, and which of the three said so."""
    if explicit_ms is not None:
        return float(explicit_ms), GATE_SOURCE_CALLER
    if declared_ms is not None:
        return float(declared_ms), ENTANGLEMENT_SOURCE_DECLARED
    return DEFAULT_GATE_MS, GATE_SOURCE_DEFAULT


def declared_clean_window_ms(
    geometry: DeclaredGeometry, distance_m: float
) -> float | None:
    """This rig's reflection-free window at ``distance_m``, ms, or ``None``.

    The first bounce's EXCESS path over the direct one grows as the mic nears
    the speaker, so a close capture's clean window is the longer one — half of
    why the close capture can say what the far one cannot. ``None`` when the
    declared geometry cannot be evaluated there at all (it accepts 0.15-3 m),
    so a mic closer than the declaration allows keeps the caller's gate, or
    the default, rather than inventing a window.
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
    answer, and ``-Infinity`` is not a number JSON can carry. A numerator at
    or below zero is not the same absence: a perfectly cancelled residual is
    the best possible read, so it floors at :data:`RESIDUAL_FLOOR_DB`.
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


# This verb publishes a per-band VERDICT by design (#3501 asked for the
# comparison, not for more evidence); its sibling `gate_sweep` publishes
# evidence only and leaves the attribution to its reader.
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
    # Shape and level both have to agree: the detrended delta is blind to level,
    # so a wrongly declared distance reads as agreeing shapes over a
    # subtraction that never cancelled — a finding about the declaration.
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
    table: Sequence[tuple[float, float, float]],
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
    delta = close_curve - far_curve
    out: list[dict[str, Any]] = []
    for nominal_lo, nominal_hi, tolerance_db in table:
        graded = intersect_bands(
            (float(nominal_lo), float(nominal_hi)), comparison_band_hz
        )
        lo, hi = graded or (0.0, 0.0)  # no overlap: both masks select nothing
        in_band = (grid >= lo) & (grid < hi)
        points = int(in_band.sum())
        bins = (freqs >= lo) & (freqs < hi)
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
            "graded_band_hz": list(graded) if graded else None,
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
    at_hz: float | None = None,
    geometry: DeclaredGeometry | None = None,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> dict[str, Any]:
    """Correct the close IR to the far distance and say what the far read owed
    the room, band by band.

    Returns the whole report as plain data: ``frame``, ``geometry``,
    ``validity``, ``alignment`` and one ``windows`` entry per declared gate
    length. Nothing is printed and nothing is written.

    ``at_hz`` adds ONE narrow row per window beside the spec rows, under
    ``features`` -- the same reading at one frequency, for a question the
    low-mid spec band is too wide to be an answer to.

    **Both alignment segments are cut at the SHORTER of the two gates**
    (published as ``alignment.alignment_gate_ms``): the far capture's clean
    window is the bound because its first bounce arrives earliest, and one lag
    aligned over a far segment that reached into room arrivals would be
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
    narrow = _narrow_band(at_hz)
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
    lo_edge = max(CLASSIFICATION_GRID_LO_HZ, trusted_far_hz)
    hi_edge = min(CLASSIFICATION_GRID_HI_HZ, ceiling_hz, band_top_hz)
    comparison_band_hz = (lo_edge, max(lo_edge, hi_edge))

    align_lo, align_hi = comparison_band_hz
    far_align = gated_segment(far, sr, gate_ms=align_ms, peak_idx=far_peak)[0]
    close_align = gated_segment(corrected, sr, gate_ms=align_ms, peak_idx=far_peak)[0]
    lag, _sign, confidence, at_edge = gcc_phat(
        far_align,
        close_align,
        sample_rate=sr,
        band_hz=(align_lo, align_hi),
        upsample=GCC_UPSAMPLE,
        max_lag_samples=float(lead + align_span),
    )
    corrected = fractional_shift(corrected, lag)
    refined = gcc_phat(
        far_align,
        gated_segment(corrected, sr, gate_ms=align_ms, peak_idx=far_peak)[0],
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

    grid = classification_grid()
    windows: list[dict[str, Any]] = []
    for name, gate_ms, span, gate_source, declared_ms in (
        (WINDOW_FAR, far_gate_ms, far_span, far_gate_source, declared_far_ms),
        (WINDOW_CLOSE, close_gate_ms, close_span, close_gate_source, declared_close_ms),
    ):
        far_seg = gated_segment(far, sr, gate_ms=gate_ms, peak_idx=far_peak)[0]
        close_seg = gated_segment(corrected, sr, gate_ms=gate_ms, peak_idx=far_peak)[0]
        residual_seg = far_seg - close_seg
        freqs, far_power = _band_spectra(far_seg, sr)
        _, direct_power = _band_spectra(close_seg, sr)
        _, residual_power = _band_spectra(residual_seg, sr)
        # Each window grades down to ITS OWN resolution floor: the close
        # capture's longer clean window is the whole reason to take it.
        window_band = (max(CLASSIFICATION_GRID_LO_HZ, f_trusted_floor_hz(span / sr)), hi_edge)
        graded = partial(
            _bands,
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
        )
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
            "bands": graded(table=SPEC_BANDS),
            "features": [
                {"requested_hz": at_hz, **row} for row in graded(table=narrow)
            ],
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
            "grid_hz": [CLASSIFICATION_GRID_LO_HZ, CLASSIFICATION_GRID_HI_HZ],
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
            # The refine cannot resolve below one upsampled correlation bin, so a
            # residual that reads zero means "under the floor", not "exact". The
            # budget is priced at the floor for that reason.
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
    at_hz: float | None = None,
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
        at_hz=at_hz,
        geometry=geometry,
        sound_speed_m_s=sound_speed_m_s,
    )
    report["captures"] = {
        "far": capture_row(far_capture) | {"round_dir": str(far_round)},
        "close": capture_row(close_capture) | {"round_dir": str(close_round)},
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


def _band_line(row: Mapping[str, Any]) -> str:
    """One graded band: its span, its verdict, and the two figures behind it."""
    graded = row["graded_band_hz"]
    span = f"{graded[0]:.0f}-{graded[1]:.0f} Hz" if graded else "not graded"
    rms, residual = row["rms_delta_db"], row["residual_rel_direct_db"]
    return (
        f"{span}: {row['verdict']}"
        + (f" ({row['unresolved_reason']})" if row["unresolved_reason"] else "")
        + (
            ""
            if rms is None or residual is None
            else f", close-vs-far RMS {rms:.2f} dB "
                 f"(tolerance {row['tolerance_db']:.1f}), residual "
                 f"{residual:.1f} dB"
        )
    )


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    """The alignment, each window's gate, then one line per band, for a caller
    to print. The alignment leads because no verdict under it means anything
    if the subtraction was aligned on the wrong peak.
    """
    alignment = report["alignment"]
    lo, hi = report["validity"]["comparison_band_hz"]
    lines = [
        f"aligned to {alignment['residual_lag_us']:.2f} us residual "
        f"(measured {alignment['measured_shift_us']:.1f} us vs geometric "
        f"{alignment['geometric_delay_us']:.1f} us, confidence "
        f"{alignment['confidence']:.2f}); comparison band "
        f"{lo:.0f}-{hi:.0f} Hz"
    ]
    for window in report["windows"]:
        declared = window["declared_clean_window_ms"]
        lines.append(
            f"  {window['name']} gated at {window['gate_ms']:.2f} ms "
            f"({window['gate_source']}); declared clean window "
            + ("undeclared" if declared is None else f"{declared:.2f} ms")
        )
        lines += [
            f"  {window['name']} {_band_line(row)}" for row in window["bands"]
        ]
        lines += [
            f"  {window['name']} at {row['requested_hz']:g} Hz {_band_line(row)}"
            for row in window["features"]
        ]
    return lines
