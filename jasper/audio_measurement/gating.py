# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Impulse-response gating and the low-frequency validity floor.

A domestic room contaminates a far-field (reference-axis) capture with a
floor/wall/ceiling reflection a few milliseconds after the direct sound. Any
quantity derived from the deconvolved impulse response — magnitude, level,
polarity, delay — is only trustworthy above a low-frequency floor set by how
long the reflection-free window is: ``f_valid ~= 1 / window_seconds``. See
docs/active-crossover-information-design.md "Measurement validity: gating
and the low-frequency floor" (spec) and the P1a consult table (this module's
constants) for the full rationale.

This module is pure numpy, does no I/O, and holds no state:

* :func:`detect_first_reflection` finds the first strong reflection after
  the direct-arrival peak using an energy-envelope threshold with
  hysteresis (drop below the threshold, then rise back above it).
* :func:`gate_impulse_response` windows an IR to its reflection-free span
  (rectangular head through the peak, half-Hann taper into the reflection)
  and returns the :data:`~jasper.active_speaker.driver_acoustics` SC-2
  gating-block fragment describing what it did.
* :func:`exempt_gating_block` builds the SC-2 block for a capture that is
  exempt from gating (today's near-field driver capture, taken a few
  centimetres from the driver — too close for a room reflection to matter).
* :func:`f_valid_floor_hz` is the floor formula in isolation, for callers
  that already know the window length.

Imported lazily by :mod:`jasper.active_speaker.driver_acoustics` (mirrors
that module's existing lazy-import discipline for the rest of the
measurement kernel) so the socket-activated ``/sound/`` wizard stays light
until a measurement actually runs.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

GATING_SCHEMA_VERSION = 1
WINDOW_KIND = "half_hann_tail"

# --- P1a consult table (see docs/active-crossover-information-design.md) ---
# K: how far below the direct peak's smoothed envelope the reflection must
# rise back above to count as "found". Tuned to reliably catch a domestic
# floor bounce (~-3..-10 dB at LF) while sitting just above a bandlimited
# driver's own first sinc sidelobe (~-13 dB), so driver ringing doesn't
# false-trigger. Missing a real reflection is the dangerous direction
# (it over-claims low-frequency validity) — raise K, not lower it, if field
# corpora show misses.
REFLECTION_THRESHOLD_DB = 12.0
# Search span after the direct peak, in ms. t_min skips the direct arrival's
# own tail; t_max bounds the search (and is the fallback window when no
# reflection is found) and must stay >= a domestic floor-bounce arrival
# (~4-5 ms) so a present floor bounce is never truncated by the bound.
SEARCH_T_MIN_MS = 0.5
SEARCH_T_MAX_MS = 7.0
# Moving-RMS smoothing window for the detection envelope.
ENVELOPE_SMOOTH_MS = 0.20
# Fraction of the reflection-free span given to the half-Hann tail taper.
TAPER_FRACTION = 0.25
# Advisory (non-excluding) band above the floor: [floor, NEAR_FLOOR_RATIO *
# floor) marks a derived quantity "near_validity_floor" (reduced confidence)
# without excluding it. The hard exclusion (nothing below floor feeds a
# decision) is the load-bearing half; this ratio is a judgment call on top.
NEAR_FLOOR_RATIO = 1.25

FLOOR_MEASURED = "measured_reflection"
FLOOR_SEARCH_BOUND = "search_span_bound"
NEAR_FIELD_EXEMPT = "near_field"


@dataclass(frozen=True)
class ReflectionDetection:
    """Result of searching an impulse response for its first strong reflection.

    ``floor_source`` is ``None`` when the IR is ungateable (silent/NaN
    capture, or too little room after the direct peak to search at all) —
    distinct from :data:`FLOOR_SEARCH_BOUND`, which means the search ran to
    its bound without finding a reflection (a real, reportable floor).
    """

    direct_peak_idx: int
    reflection_idx: int | None
    floor_source: str | None


def f_valid_floor_hz(window_s: float) -> float:
    """Low-frequency validity floor for a reflection-free window of ``window_s``.

    ``f_valid ~= 1 / window_s`` — a 4 ms window resolves nothing below
    250 Hz. A non-positive or non-finite window is not analyzable at any
    frequency, so this returns ``+inf``, which safely propagates to "no
    frequency clears the floor" in every downstream comparison.
    """
    if not (window_s > 0) or not math.isfinite(window_s):
        return float("inf")
    return 1.0 / window_s


def _idx_to_ms(idx: int, sample_rate: float) -> float:
    """Sample index to milliseconds; guards the divide for a degenerate rate."""
    if sample_rate <= 0 or not math.isfinite(sample_rate):
        return 0.0
    return 1000.0 * idx / sample_rate


def detect_first_reflection(
    ir: np.ndarray,
    sample_rate: int,
    *,
    direct_peak_idx: int | None = None,
    threshold_db: float = REFLECTION_THRESHOLD_DB,
    t_min_ms: float = SEARCH_T_MIN_MS,
    t_max_ms: float = SEARCH_T_MAX_MS,
    smooth_ms: float = ENVELOPE_SMOOTH_MS,
) -> ReflectionDetection:
    """Find the first strong reflection after the direct-arrival peak.

    Energy-envelope threshold with hysteresis: the smoothed envelope must
    first drop below ``peak - threshold_db`` (the end of the direct
    arrival's own tail) and then rise back above that same threshold (the
    reflection onset), searched only in
    ``[direct_peak + t_min_ms, direct_peak + t_max_ms]``. Vectorized over
    that bounded search span — not a Python loop over the full IR.

    ``direct_peak_idx`` defaults to ``argmax(|ir|)``. Returns a
    :class:`ReflectionDetection` with ``floor_source=None`` (ungateable) for
    a silent/NaN capture or a search span with no room to search;
    :data:`FLOOR_SEARCH_BOUND` when the direct arrival never separates from
    the noise floor or no reflection crosses back above threshold before the
    search bound; :data:`FLOOR_MEASURED` when a reflection onset is found.
    """
    ab = np.abs(np.asarray(ir, dtype=np.float64))
    n = ab.size
    if n == 0:
        return ReflectionDetection(0, None, None)

    sr = float(sample_rate)
    if sr <= 0 or not math.isfinite(sr):
        # Degenerate rate: nothing about ms-based windows is meaningful.
        p = int(direct_peak_idx) if direct_peak_idx is not None else int(np.argmax(ab))
        return ReflectionDetection(int(np.clip(p, 0, n - 1)), None, None)

    w = max(1, int(round(smooth_ms * 1e-3 * sr)))
    if w > 1:
        kernel = np.ones(w, dtype=np.float64) / w
        env = np.sqrt(np.convolve(ab**2, kernel, mode="same"))
    else:
        env = ab

    p = int(direct_peak_idx) if direct_peak_idx is not None else int(np.argmax(ab))
    p = int(np.clip(p, 0, n - 1))

    peak = float(env[p])
    if not math.isfinite(peak) or peak <= 0:
        return ReflectionDetection(p, None, None)

    thr = peak * (10.0 ** (-threshold_db / 20.0))
    t_min = max(1, int(round(t_min_ms * 1e-3 * sr)))
    t_max = int(round(t_max_ms * 1e-3 * sr))
    end = min(n - 1, p + t_max)
    if p + t_min >= end:
        # No usable room after the direct peak to search at all.
        return ReflectionDetection(p, None, None)

    seg = env[p + t_min : end + 1]
    rel = np.arange(p + t_min, end + 1)
    below = seg < thr
    if not bool(np.any(below)):
        # The direct arrival's tail never separates from threshold within
        # the search span — report the conservative span-bound floor.
        return ReflectionDetection(p, None, FLOOR_SEARCH_BOUND)
    first_below = int(np.argmax(below))
    after = seg[first_below:] >= thr
    if not bool(np.any(after)):
        # Separated but nothing rose back above threshold before the bound.
        return ReflectionDetection(p, None, FLOOR_SEARCH_BOUND)
    reflection_idx = int(rel[first_below + int(np.argmax(after))])
    return ReflectionDetection(p, reflection_idx, FLOOR_MEASURED)


def _fragment(
    *,
    direct_peak_ms: float,
    first_reflection_ms: float | None,
    window_ms: float | None,
    floor_hz: float | None,
    floor_source: str | None,
) -> dict[str, Any]:
    """The SC-2 gating block's core fields (everything but ``applied`` /
    ``exempt_reason``, which the caller supplies — see
    :func:`gate_impulse_response` and :func:`exempt_gating_block`)."""
    return {
        "schema_version": GATING_SCHEMA_VERSION,
        "direct_peak_ms": direct_peak_ms,
        "first_reflection_ms": first_reflection_ms,
        "window_ms": window_ms,
        "window": WINDOW_KIND,
        "f_valid_floor_hz": floor_hz,
        "floor_source": floor_source,
    }


def gate_impulse_response(
    ir: np.ndarray,
    sample_rate: int,
    *,
    direct_peak_idx: int | None = None,
    taper_fraction: float = TAPER_FRACTION,
    threshold_db: float = REFLECTION_THRESHOLD_DB,
    t_min_ms: float = SEARCH_T_MIN_MS,
    t_max_ms: float = SEARCH_T_MAX_MS,
    smooth_ms: float = ENVELOPE_SMOOTH_MS,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Window an impulse response to its reflection-free span.

    Returns ``(gated_ir, fragment)``. ``gated_ir`` is the SAME length as
    ``ir``: 1.0 from the start through the direct peak, a flat plateau, a
    half-Hann taper down to 0 into the detected reflection (or the
    search-span bound when none is found), and 0 after. ``fragment`` is the
    SC-2 gating block MINUS ``applied``/``exempt_reason`` — the caller
    derives those with one rule: ``applied = fragment["floor_source"] is not
    None``, ``exempt_reason = None`` (this function is only called for
    reference-axis captures, which are never near-field-exempt).

    When the IR is ungateable (silent/NaN, or no room after the peak to
    search), the input is returned unchanged and every fragment field except
    ``direct_peak_ms`` is ``None``.
    """
    ir_arr = np.asarray(ir)
    n = ir_arr.shape[0] if ir_arr.ndim == 1 else 0
    sr = float(sample_rate)

    det = detect_first_reflection(
        ir_arr,
        sample_rate,
        direct_peak_idx=direct_peak_idx,
        threshold_db=threshold_db,
        t_min_ms=t_min_ms,
        t_max_ms=t_max_ms,
        smooth_ms=smooth_ms,
    )
    direct_peak_ms = _idx_to_ms(det.direct_peak_idx, sr)
    ungated_fragment = _fragment(
        direct_peak_ms=direct_peak_ms,
        first_reflection_ms=None,
        window_ms=None,
        floor_hz=None,
        floor_source=None,
    )

    if det.floor_source is None:
        logger.debug(
            "gating: ungateable IR (n=%d direct_peak_idx=%d) — no reflection-free "
            "span could be searched; leaving ungated",
            n, det.direct_peak_idx,
        )
        return np.asarray(ir_arr, dtype=np.float32), ungated_fragment

    p = det.direct_peak_idx
    if det.floor_source == FLOOR_MEASURED:
        end = int(det.reflection_idx)  # type: ignore[arg-type]
    else:
        t_max_samples = int(round(t_max_ms * 1e-3 * sr)) if sr > 0 else 0
        end = min(max(n - 1, 0), p + t_max_samples)

    span = end - p
    if span <= 0:
        # Defensive: detect_first_reflection's own p+t_min>=end guard should
        # already prevent this, but a caller-supplied direct_peak_idx near
        # the array end could in principle land past the search bound.
        logger.debug(
            "gating: non-positive reflection-free span (p=%d end=%d) — "
            "leaving ungated", p, end,
        )
        return np.asarray(ir_arr, dtype=np.float32), ungated_fragment

    window_ms = 1000.0 * span / sr
    floor_hz = f_valid_floor_hz(span / sr)
    first_reflection_ms = (
        _idx_to_ms(end, sr) if det.floor_source == FLOOR_MEASURED else None
    )

    win = np.zeros(n, dtype=np.float64)
    win[: p + 1] = 1.0
    taper_len = max(1, int(round(taper_fraction * span)))
    flat_end = max(p, end - taper_len)
    win[p:flat_end] = 1.0
    tail_len = end - flat_end  # always > 0 here: taper_len >= 1 and span > 0
    idx = np.arange(flat_end, end + 1)
    t = (idx - flat_end) / tail_len
    win[flat_end : end + 1] = 0.5 * (1.0 + np.cos(np.pi * t))
    # win[end + 1:] stays 0 from initialization.

    gated = (ir_arr.astype(np.float64) * win).astype(np.float32)
    fragment = _fragment(
        direct_peak_ms=direct_peak_ms,
        first_reflection_ms=first_reflection_ms,
        window_ms=window_ms,
        floor_hz=floor_hz,
        floor_source=det.floor_source,
    )
    return gated, fragment


def apply_gate_fragment(
    ir: np.ndarray,
    sample_rate: int,
    fragment: dict[str, Any],
    *,
    taper_fraction: float = TAPER_FRACTION,
) -> np.ndarray:
    """Apply a signal-derived gate fragment to another equal-length IR.

    This is the paired-noise seam: reflection detection runs on the signal
    exactly once through :func:`gate_impulse_response`; the resulting integer
    peak/span (round-tripped through the fragment's millisecond fields) builds
    the same half-Hann operator for noise.  The noise IR is never inspected to
    choose a peak, reflection, or window.
    """

    ir_arr = np.asarray(ir)
    if ir_arr.ndim != 1:
        raise ValueError("paired gate input must be 1-D")
    if fragment.get("floor_source") is None:
        return np.asarray(ir_arr, dtype=np.float32)
    sr = float(sample_rate)
    direct_ms = fragment.get("direct_peak_ms")
    window_ms = fragment.get("window_ms")
    if not (
        sr > 0
        and isinstance(direct_ms, (int, float))
        and isinstance(window_ms, (int, float))
    ):
        raise ValueError("signal gate fragment is incomplete")
    p = int(round(float(direct_ms) * sr / 1000.0))
    span = int(round(float(window_ms) * sr / 1000.0))
    end = p + span
    if not (0 <= p < end < len(ir_arr)):
        raise ValueError("signal gate fragment is outside the paired IR")
    win = np.zeros(len(ir_arr), dtype=np.float64)
    win[: p + 1] = 1.0
    taper_len = max(1, int(round(taper_fraction * span)))
    flat_end = max(p, end - taper_len)
    win[p:flat_end] = 1.0
    tail_len = end - flat_end
    idx = np.arange(flat_end, end + 1)
    t = (idx - flat_end) / tail_len
    win[flat_end : end + 1] = 0.5 * (1.0 + np.cos(np.pi * t))
    return (ir_arr.astype(np.float64) * win).astype(np.float32)


def exempt_gating_block(
    ir: np.ndarray,
    sample_rate: int,
    *,
    reason: str = NEAR_FIELD_EXEMPT,
) -> dict[str, Any]:
    """Full SC-2 gating block for a capture that is exempt from gating.

    A near-field capture (today's shipped driver measurement, taken a few
    centimetres from the driver) is too close for a room reflection to
    contaminate it, so it is never gated — the caller uses the ungated IR
    for magnitude, byte-identical to before this module existed. This still
    records ``direct_peak_ms`` (a benign IR-peak position from the deconv
    window offset, not a room measurement) so the SC-2 invariant — a gating
    block is persisted whenever an IR exists — holds uniformly.
    """
    ir_arr = np.asarray(ir)
    n = ir_arr.shape[0] if ir_arr.ndim == 1 else 0
    peak_idx = int(np.argmax(np.abs(ir_arr))) if n else 0
    fragment = _fragment(
        direct_peak_ms=_idx_to_ms(peak_idx, float(sample_rate)),
        first_reflection_ms=None,
        window_ms=None,
        floor_hz=None,
        floor_source=None,
    )
    return {
        "schema_version": fragment["schema_version"],
        "applied": False,
        "exempt_reason": reason,
        "direct_peak_ms": fragment["direct_peak_ms"],
        "first_reflection_ms": fragment["first_reflection_ms"],
        "window_ms": fragment["window_ms"],
        "window": fragment["window"],
        "f_valid_floor_hz": fragment["f_valid_floor_hz"],
        "floor_source": fragment["floor_source"],
    }
