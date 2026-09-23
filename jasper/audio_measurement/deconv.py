# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""FFT-based regularized deconvolution for room-impulse extraction.

    H(f) = Y(f) * conj(X(f)) / max(|X(f)|², ε)
    h(t) = ifft(H(f))

The denominator floor bounds inversion where the stimulus has little energy
without tilting a log sweep's excited band. The recovered IR is trimmed around
the direct-arrival peak: 5 ms before (non-causal artifacts of the inversion)
and 500 ms after (domestic-room decay).
"""
from __future__ import annotations

import logging
import math
from collections.abc import Sequence

import numpy as np

from .sweep import SweepMeta

logger = logging.getLogger(__name__)

DEFAULT_PRE_ARRIVAL_MS = 5.0
DEFAULT_POST_ARRIVAL_MS = 500.0
DEFAULT_EPSILON_RELATIVE = 1e-3

# Upper bound on captured-signal length fed to the FFT, in seconds. The
# correction HTTP layer caps the WAV body at 32 MB (~350 s of 48 kHz mono),
# which would drive n_pad to 2**25 and a ~1.3 GB FFT working set: an OOM on the
# 1 GB Pi. This bound keeps n_pad at ~2**21 (~100-150 MB peak).
DEFAULT_MAX_CAPTURE_SECONDS = 30.0


def _next_power_of_two(value: int) -> int:
    """Return the smallest power of two greater than or equal to ``value``."""
    return 1 << (max(int(value), 1) - 1).bit_length()


def cap_capture_length(
    captured: np.ndarray,
    *,
    sweep_len: int,
    sample_rate: int,
    max_capture_seconds: float | None = None,
) -> np.ndarray:
    """Truncate an over-long capture to bound the FFT working set.

    Never truncates below ``sweep_len`` -- the inversion needs the full sweep
    response; ``sweep_len=0`` means there is no sweep to preserve.
    ``max_capture_seconds=None`` reads :data:`DEFAULT_MAX_CAPTURE_SECONDS` at
    call time; a value <= 0 disables the cap.
    """
    seconds = (
        DEFAULT_MAX_CAPTURE_SECONDS
        if max_capture_seconds is None
        else max_capture_seconds
    )
    if seconds <= 0 or sample_rate <= 0:
        return captured
    max_samples = max(sweep_len, int(round(seconds * sample_rate)))
    if len(captured) <= max_samples:
        return captured
    logger.warning(
        "deconv: FFT input %d samples (%.1f s) exceeds cap %d samples "
        "(%.1f s); truncating to bound FFT memory",
        len(captured), len(captured) / sample_rate,
        max_samples, max_samples / sample_rate,
    )
    return captured[:max_samples]


def cap_capture_tail(
    captured: np.ndarray,
    *,
    sweep_len: int,
    sample_rate: int,
    max_capture_seconds: float,
) -> tuple[np.ndarray, int]:
    """Retain a bounded capture tail and return its source start offset.

    Capture starts before an unbounded network wait but stops just after
    the sweep, so crossover analysis needs the TAIL, unlike
    :func:`cap_capture_length`, whose callers retain the beginning.
    """

    if max_capture_seconds <= 0 or sample_rate <= 0:
        return captured, 0
    max_samples = max(sweep_len, int(round(max_capture_seconds * sample_rate)))
    if len(captured) <= max_samples:
        return captured, 0
    start = len(captured) - max_samples
    logger.warning(
        "deconv: retaining final %d of %d samples for capture sweep analysis",
        max_samples,
        len(captured),
    )
    return captured[start:], start


def regularized_deconvolution_full(
    captured: np.ndarray,
    sweep: np.ndarray,
    sample_rate: int,
    *,
    epsilon_relative: float = DEFAULT_EPSILON_RELATIVE,
    max_capture_seconds: float | None = None,
) -> np.ndarray:
    """Recover the full regularized linear-deconvolution impulse response.

    No peak selection or time windowing, so a caller comparing signal against
    noise can apply one signal-derived window to both rather than letting
    noise choose its own argmax window.
    """
    if captured.ndim != 1 or sweep.ndim != 1:
        raise ValueError(
            f"captured and sweep must be 1-D; got shapes "
            f"{captured.shape} and {sweep.shape}"
        )
    if len(captured) < len(sweep):
        raise ValueError(
            f"captured ({len(captured)} samples) shorter than sweep "
            f"({len(sweep)} samples) — capture too short or "
            f"misaligned"
        )
    captured = cap_capture_length(
        captured,
        sweep_len=len(sweep),
        sample_rate=sample_rate,
        max_capture_seconds=max_capture_seconds,
    )
    n_pad = _next_power_of_two(len(captured) + len(sweep))
    Y = np.fft.rfft(captured, n=n_pad)
    X = np.fft.rfft(sweep, n=n_pad)
    eps = epsilon_relative * float(np.max(np.abs(X) ** 2))
    H = Y * np.conj(X) / np.maximum(np.abs(X) ** 2, max(eps, np.finfo(float).tiny))
    return np.fft.irfft(H, n=n_pad)


def direct_arrival_window(
    full_ir: np.ndarray,
    sample_rate: int,
    *,
    direct_peak_idx: int | None = None,
    pre_arrival_ms: float = DEFAULT_PRE_ARRIVAL_MS,
    post_arrival_ms: float = DEFAULT_POST_ARRIVAL_MS,
) -> tuple[int, int]:
    """Return the deterministic signal-derived ``[start, end)`` IR window."""

    if full_ir.ndim != 1 or full_ir.size == 0:
        raise ValueError("full_ir must be non-empty 1-D data")
    peak_idx = (
        int(np.argmax(np.abs(full_ir)))
        if direct_peak_idx is None
        else int(direct_peak_idx)
    )
    if not 0 <= peak_idx < len(full_ir):
        raise ValueError("direct peak is outside the full impulse response")
    pre_samples = max(0, int(round(pre_arrival_ms * sample_rate / 1000)))
    post_samples = max(1, int(round(post_arrival_ms * sample_rate / 1000)))
    return max(0, peak_idx - pre_samples), min(
        len(full_ir), peak_idx + post_samples
    )


def apply_arrival_window(
    full_ir: np.ndarray, window: tuple[int, int]
) -> np.ndarray:
    """Apply an explicit arrival window without inspecting ``full_ir``."""

    start, end = int(window[0]), int(window[1])
    if full_ir.ndim != 1 or not (0 <= start < end <= len(full_ir)):
        raise ValueError("arrival window is outside the full impulse response")
    return np.asarray(full_ir[start:end], dtype=np.float32)


def magnitude_response(
    ir: np.ndarray,
    sample_rate: int,
    *,
    n_fft: int | None = None,
    normalize: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Magnitude response of an impulse response, in dB.

    ``n_fft`` defaults to the next power of two >= max(8192, len(ir)); the 8192
    floor gives 5.86 Hz/bin at 48 kHz, enough bass resolution for 1/48-octave
    smoothing. ``normalize`` subtracts the peak; set False to keep the absolute
    deconvolution amplitude.
    """
    if n_fft is None:
        n_fft = max(8192, _next_power_of_two(len(ir)))
    H = np.fft.rfft(ir, n=n_fft)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    magnitude = np.abs(H)
    # Floor before log to avoid -inf at deep nulls.
    magnitude_db = 20 * np.log10(np.maximum(magnitude, 1e-12))
    if normalize:
        magnitude_db = magnitude_db - float(np.max(magnitude_db))
    return freqs.astype(np.float64), magnitude_db.astype(np.float64)


# How far a harmonic window reaches from its center, as a fraction of the
# distance to the NEAREST neighbouring order's center. Below 0.5 by
# construction, so two adjacent windows cannot touch. Also read by
# `required_pre_guard_s`, which predicts this window's leading edge.
HARMONIC_WINDOW_GAP_FRACTION = 0.4

# Radius of the local-peak search around a harmonic image's predicted center;
# absorbs sub-sample rounding and the group delay a real driver adds between a
# fundamental's arrival and its harmonic's.
HARMONIC_PEAK_SEARCH_RADIUS_S = 0.002


class HarmonicWindowOutOfRange(ValueError):
    pass


def harmonic_time_advance_s(meta: SweepMeta, order: int) -> float:
    """Return how far the order-N harmonic image leads the linear IR."""

    if type(order) is not int or order < 1:
        raise ValueError("harmonic order must be a positive integer")
    if not np.isfinite(meta.L) or meta.L <= 0.0:
        raise ValueError("sweep L must be finite and positive")
    return float(meta.L * np.log(order))


def extract_harmonic_ir(
    full_ir: np.ndarray,
    sample_rate: int,
    direct_peak_idx: int,
    meta: SweepMeta,
    order: int,
) -> np.ndarray:
    """Window one synchronized-sweep harmonic image with a Hann taper."""

    full_ir = np.asarray(full_ir, dtype=np.float64)
    if full_ir.ndim != 1 or full_ir.size == 0:
        raise ValueError("full_ir must be non-empty 1-D data")
    if sample_rate <= 0 or not 0 <= direct_peak_idx < len(full_ir):
        raise ValueError("sample rate or direct peak is invalid")
    advance = harmonic_time_advance_s(meta, order)
    predicted_center = direct_peak_idx - round(advance * sample_rate)
    search_radius = round(HARMONIC_PEAK_SEARCH_RADIUS_S * sample_rate)
    search_start = max(0, predicted_center - search_radius)
    search_end = min(len(full_ir), predicted_center + search_radius + 1)
    if search_start >= search_end:
        raise HarmonicWindowOutOfRange("harmonic window crosses t=0 or the capture boundary")
    center = search_start + int(np.argmax(np.abs(full_ir[search_start:search_end])))
    neighboring_orders = (2,) if order == 1 else (order - 1, order + 1)
    neighboring_centers = [
        direct_peak_idx - round(
            harmonic_time_advance_s(meta, neighbor) * sample_rate
        )
        for neighbor in neighboring_orders
    ]
    nearest_gap = min(abs(center - neighbor) for neighbor in neighboring_centers)
    half_width = max(
        1, int(math.floor(HARMONIC_WINDOW_GAP_FRACTION * nearest_gap))
    )
    start = center - half_width
    end = center + half_width + 1
    if start < 0 or end > len(full_ir):
        raise HarmonicWindowOutOfRange("harmonic window crosses t=0 or the capture boundary")
    for neighbor, neighbor_center in zip(neighboring_orders, neighboring_centers):
        adjacent = (2,) if neighbor == 1 else (neighbor - 1, neighbor + 1)
        neighbor_gap = min(abs(
            neighbor_center
            - (
                direct_peak_idx
                - round(harmonic_time_advance_s(meta, other) * sample_rate)
            )
        ) for other in adjacent)
        neighbor_half_width = max(
            1, int(math.floor(HARMONIC_WINDOW_GAP_FRACTION * neighbor_gap))
        )
        if start <= neighbor_center + neighbor_half_width and end > (
            neighbor_center - neighbor_half_width
        ):
            raise HarmonicWindowOutOfRange("harmonic window overlaps a neighboring order")
    window = np.hanning(end - start)
    return full_ir[start:end] * window


def harmonic_magnitude_response(
    harmonic_ir,
    sample_rate,
    order,
    n_fft=None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return harmonic magnitude on the excitation-frequency axis."""

    if type(order) is not int or order < 1:
        raise ValueError("harmonic order must be a positive integer")
    output_freqs, magnitude_db = magnitude_response(
        np.asarray(harmonic_ir, dtype=np.float64),
        sample_rate,
        n_fft=n_fft,
        normalize=False,
    )
    return output_freqs / order, magnitude_db


# The orders a synchronized sweep separates cleanly at the gaps the MEASURE
# program actually schedules. Deliberately NOT imported from
# `program.MESM_MAX_HARMONIC_ORDER`: that constant sizes the STIMULUS, this one
# bounds the ANALYSIS.
DEFAULT_HARMONIC_ORDERS: tuple[int, ...] = (2, 3)


# Extra pre-guard beyond the predicted window, covering `extract_harmonic_ir`'s
# ±2 ms local-peak search: a centre refined EARLIER than predicted drags the
# window's leading edge with it. True worst case is 0.6× this; costs ~96 samples.
PRE_GUARD_SEARCH_MARGIN_S = HARMONIC_PEAK_SEARCH_RADIUS_S


# Shrink applied to the largest phantom window that clears both neighbouring
# harmonic images, so a sub-sample rounding cannot push its edge into one.
PHANTOM_WINDOW_SAFETY = 0.9


def validated_orders(orders: Sequence[int]) -> tuple[int, ...]:
    """The requested orders, or a refusal naming what is wrong with them."""
    checked = tuple(int(order) for order in orders)
    if not checked:
        raise ValueError("at least one harmonic order is required")
    if any(order < 2 for order in checked):
        raise ValueError("harmonic orders must be integers of at least 2")
    if len(set(checked)) != len(checked):
        raise ValueError("harmonic orders must be distinct")
    return checked


def image_half_width_s(meta: SweepMeta, order: int) -> float:
    """Half-width :func:`extract_harmonic_ir` gives order ``order``.

    Computed from the PREDICTED centre; the runtime function measures its gap
    from the SEARCHED one, so the two differ by at most 0.6× the ±2 ms search
    radius, which :data:`PRE_GUARD_SEARCH_MARGIN_S` absorbs.
    """
    return (
        HARMONIC_WINDOW_GAP_FRACTION
        * meta.L
        * math.log((order + 1) / order)
    )


def phantom_window_s(meta: SweepMeta, order: int) -> tuple[float, float]:
    """``(centre_advance_s, half_width_s)`` of order ``order``'s phantom window.

    Centred at ``L·ln(order − ½)`` -- the gap BELOW image ``order`` -- and
    widened until it just clears both neighbouring image windows. The LOWER gap
    because the deconvolution's own artefacts are strongest near the direct
    arrival and fall away from it: an upper-gap phantom under-reports the floor,
    and on a provably linear synthetic path it left H2's pure artefact more than
    6 dB "clear" of its own floor. The lower gap over-estimates instead, so the
    reading errs toward refusing to claim distortion.

    The one owner of this geometry: :func:`required_pre_guard_s` sizes the
    deconvolution from it and the distortion reading's phantom floor cuts the
    window with it.
    """
    if order < 2:
        raise ValueError("a phantom floor is only defined for orders above 1")
    advance = meta.L * math.log(order - 0.5)
    clearance = min(
        advance
        - harmonic_time_advance_s(meta, order - 1)
        - image_half_width_s(meta, order - 1),
        harmonic_time_advance_s(meta, order)
        - image_half_width_s(meta, order)
        - advance,
    )
    return advance, PHANTOM_WINDOW_SAFETY * clearance


def required_pre_guard_s(
    meta: SweepMeta, orders: Sequence[int] = DEFAULT_HARMONIC_ORDERS
) -> float:
    """Seconds of pre-guard every window the harmonic reading cuts needs.

    The order-``N`` image sits ``L·ln(N)`` ahead of the linear IR and is
    windowed to ``±`` :func:`image_half_width_s`, so its leading edge sits at
    ``L·ln(N) + half_width`` before the direct arrival; the maximum over the
    requested orders binds, plus :data:`PRE_GUARD_SEARCH_MARGIN_S` for the
    local-peak search. The fundamental's window and the phantom-floor windows
    are enumerated too, though neither binds, so the guard follows the window
    geometry if it ever moves.
    """
    orders = validated_orders(orders)
    edges = [
        harmonic_time_advance_s(meta, order) + image_half_width_s(meta, order)
        for order in (1, *orders)
    ]
    edges += [sum(phantom_window_s(meta, order)) for order in orders]
    return max(edges) + PRE_GUARD_SEARCH_MARGIN_S
