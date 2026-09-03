# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-correlation alignment: the confidence gate and the sub-sample refine.

An integrity hash proves a capture is intact; it cannot catch an
intact-but-misaligned one. Confidence is the normalized margin between the
dominant correlation peak and the next-strongest peak OUTSIDE the main lobe.
KNOWN false-pass class: a loud-but-wrong capture that is still sharply
self-peaked (e.g. clipped) clears the gate -- this catches *ambiguous*
alignment, not every invalid capture. The 0.40 default is a conservative
starting gate, not a measurement-derived constant.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np
from scipy import signal as scipy_signal


def _env_threshold(default: float = 0.40) -> float:
    """The default confidence gate, overridable at deploy time.

    The 0.40 default is NOT empirically derived; tuning it needs on-device
    sweeps, so it is a deploy-time knob
    (``JASPER_CAPTURE_ALIGNMENT_THRESHOLD``, 0..1) rather than a code change.
    """
    raw = os.environ.get("JASPER_CAPTURE_ALIGNMENT_THRESHOLD", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return default
        if 0.0 <= value <= 1.0:
            return value
    return default


DEFAULT_CONFIDENCE_THRESHOLD = _env_threshold()
# Exclude the main correlation lobe (~a few ms) when picking the competing peak.
DEFAULT_EXCLUDE_RADIUS_S = 0.005
# Cost/memory backstop mirroring deconv.DEFAULT_MAX_CAPTURE_SECONDS; the
# stimulus always lands well within it, so truncation never drops it.
DEFAULT_MAX_CAPTURE_S = 20.0
DEFAULT_SAMPLE_RATE = 48000

# GCC-PHAT sub-sample refinement (design §5.6.5).
GCC_UPSAMPLE = 16


class AlignmentError(RuntimeError):
    """The capture could not be confidently aligned to the known stimulus."""

    def __init__(self, message: str, confidence: float, threshold: float) -> None:
        super().__init__(message)
        self.confidence = confidence
        self.threshold = threshold


@dataclass(frozen=True)
class AlignmentResult:
    lag_samples: int
    confidence: float  # 0..1 margin of the reported lag over the next-strongest
    peak: float  # normalized correlation at the reported lag (0..1 similarity)
    # Competition on each side of the reported lag, outside the exclusion.
    earlier: float = 0.0
    earlier_lag_samples: int = 0
    later: float = 0.0
    later_lag_samples: int = 0


def _strongest(values: np.ndarray, offset: int) -> tuple[int, float]:
    """Index (in the parent array) and height of the tallest of ``values``."""
    if not values.size:
        return offset, 0.0
    idx = int(np.argmax(values))
    return idx + offset, float(values[idx])


def _normalize(signal: np.ndarray) -> np.ndarray:
    x = np.asarray(signal, dtype=np.float64).ravel()
    # A NaN/inf-laden capture must not poison the norm (and the reported peak).
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = x - x.mean() if x.size else x
    norm = np.linalg.norm(x)
    return x / norm if norm > 0 else x


def correlation(
    captured: np.ndarray,
    stimulus: np.ndarray,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    max_capture_s: float = DEFAULT_MAX_CAPTURE_S,
) -> np.ndarray:
    """Normalized |cross-correlation|: index ``i`` is the 0..1 similarity of
    ``stimulus`` placed at lag ``i`` in ``captured``. Empty when the capture
    cannot contain the stimulus."""
    cap_input = np.asarray(captured)
    stim_input = np.asarray(stimulus)
    if cap_input.size == 0 or stim_input.size == 0:
        return np.empty(0)

    # Backstop applied before normalization: normalization promotes to float64,
    # so truncating afterwards would still allocate a full-sized copy.
    max_cap = max(stim_input.size, int(max_capture_s * sample_rate))
    if cap_input.size > max_cap:
        cap_input = (
            cap_input[:max_cap]
            if cap_input.ndim == 1
            else cap_input.flat[:max_cap]
        )

    cap = _normalize(cap_input)
    stim = _normalize(stim_input)
    if cap.size < stim.size:
        return np.empty(0)
    return np.abs(scipy_signal.correlate(cap, stim, mode="valid", method="fft"))


def alignment_at(
    corr: np.ndarray, lag_samples: int, *, exclude_radius: int
) -> AlignmentResult:
    """Score `lag_samples` against the competition around it in `corr`."""
    primary = float(corr[lag_samples])
    if not np.isfinite(primary) or primary <= 0.0:
        return AlignmentResult(lag_samples=lag_samples, confidence=0.0, peak=0.0)
    masked = corr.copy()
    lo = max(0, lag_samples - exclude_radius)
    hi = min(corr.size, lag_samples + exclude_radius + 1)
    masked[lo:hi] = 0.0
    earlier_lag, earlier = _strongest(masked[:lo], 0)
    later_lag, later = _strongest(masked[hi:], hi)
    return AlignmentResult(
        lag_samples=lag_samples,
        confidence=max(0.0, (primary - max(earlier, later)) / primary),
        peak=primary,
        earlier=earlier,
        earlier_lag_samples=earlier_lag,
        later=later,
        later_lag_samples=later_lag,
    )


def cross_correlation_alignment(
    captured: np.ndarray,
    stimulus: np.ndarray,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    exclude_radius: int | None = None,
    max_capture_s: float = DEFAULT_MAX_CAPTURE_S,
) -> AlignmentResult:
    """Locate ``stimulus`` inside ``captured`` and score that lag's confidence.

    ``exclude_radius`` defaults to ``DEFAULT_EXCLUDE_RADIUS_S * sample_rate``
    (the main lobe); pass an override only for tests.
    """
    corr = correlation(
        captured, stimulus, sample_rate=sample_rate, max_capture_s=max_capture_s
    )
    if corr.size == 0:
        return AlignmentResult(lag_samples=0, confidence=0.0, peak=0.0)
    if exclude_radius is None:
        exclude_radius = max(1, int(DEFAULT_EXCLUDE_RADIUS_S * sample_rate))
    return alignment_at(corr, int(np.argmax(corr)), exclude_radius=exclude_radius)


def assert_alignment_confident(
    captured: np.ndarray,
    stimulus: np.ndarray,
    *,
    require: bool = True,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    exclude_radius: int | None = None,
    max_capture_s: float = DEFAULT_MAX_CAPTURE_S,
) -> AlignmentResult:
    """Score alignment and, when ``require``, fail loud below ``threshold``."""
    result = cross_correlation_alignment(
        captured,
        stimulus,
        sample_rate=sample_rate,
        exclude_radius=exclude_radius,
        max_capture_s=max_capture_s,
    )
    if require and result.confidence < threshold:
        raise AlignmentError(
            f"weak/ambiguous alignment (confidence {result.confidence:.2f} < "
            f"{threshold:.2f}) — the stimulus could not be located in the capture",
            confidence=result.confidence,
            threshold=threshold,
        )
    return result


def parabolic_peak(values: np.ndarray, idx: int) -> float:
    """Sub-sample offset of a peak at integer ``idx`` via 3-point parabola.

    Bounded to ±1 bin: a true local maximum refines within ±0.5 bin, so a larger
    offset means near-degenerate points and an extrapolation artifact, and the
    integer peak is returned unrefined rather than the parabola's vertex.
    """
    if idx <= 0 or idx >= values.size - 1:
        return float(idx)
    y0, y1, y2 = float(values[idx - 1]), float(values[idx]), float(values[idx + 1])
    denom = y0 - 2.0 * y1 + y2
    if denom == 0.0:
        return float(idx)
    offset = 0.5 * (y0 - y2) / denom
    if not -1.0 <= offset <= 1.0:
        return float(idx)
    return idx + offset


def _bandlimit(ir: np.ndarray, sample_rate: int, lo_hz: float, hi_hz: float) -> np.ndarray:
    """Zero-phase band-pass an IR by masking its spectrum to ``[lo, hi]``."""
    n = ir.size
    spectrum = np.fft.rfft(ir)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    spectrum = spectrum * mask
    return np.fft.irfft(spectrum, n=n)


def _gcc_correlation(
    a: np.ndarray,
    b: np.ndarray,
    *,
    sample_rate: int,
    band_hz: tuple[float, float],
    upsample: int,
) -> tuple[np.ndarray, int]:
    """Band-limited GCC-PHAT of ``a`` vs ``b``, ×``upsample`` FFT-interpolated.

    Returns ``(cc, m)`` on the circular-lag axis (index ``i`` -> lag ``i`` for
    ``i <= m/2`` else ``i - m``; native lag = index / ``upsample``). The
    cross-power is phase-transform weighted **only inside ``band_hz``** --
    whitening the near-zero out-of-band bins piles a spurious peak near zero
    lag.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    L = max(a.size, b.size)
    n = 1
    while n < 2 * L:
        n *= 2
    A = np.fft.rfft(a, n=n)
    B = np.fft.rfft(b, n=n)
    R = A * np.conj(B)
    mag = np.abs(R)
    mag[mag < 1e-12] = 1e-12
    R_phat = R / mag
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    in_band = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    R_phat = R_phat * in_band
    m = n * upsample
    cc = np.fft.irfft(R_phat, n=m) * upsample
    return cc, m


def gcc_phat(
    a: np.ndarray,
    b: np.ndarray,
    *,
    sample_rate: int,
    band_hz: tuple[float, float],
    upsample: int,
    max_lag_samples: float,
):
    """Band-limited GCC-PHAT of ``a`` vs ``b``; ``a ≈ b`` shifted right by the lag.

    Returns ``(lag_samples, polarity_sign, confidence, at_edge)``. ``at_edge``
    is True when the peak lands within one native sample of the
    ±``max_lag_samples`` search bound -- the true peak is likely OUTSIDE the
    window and the returned lag is a clamped artifact the caller must refuse.
    """
    cc, m = _gcc_correlation(
        a, b, sample_rate=sample_rate, band_hz=band_hz, upsample=upsample,
    )
    max_lag_up = int(round(max_lag_samples * upsample))
    max_lag_up = max(1, min(max_lag_up, m // 2 - 1))
    idxs = np.concatenate(
        [np.arange(0, max_lag_up + 1), np.arange(m - max_lag_up, m)]
    )
    window = cc[idxs]
    peak_local = int(np.argmax(np.abs(window)))
    peak_idx = int(idxs[peak_local])
    abs_cc = np.abs(cc)
    refined = parabolic_peak(abs_cc, peak_idx)
    circ = refined if refined <= m / 2 else refined - m
    lag_samples = circ / upsample
    polarity_sign = 1 if cc[peak_idx] >= 0 else -1
    primary = float(abs_cc[peak_idx])
    # Secondary: strongest competitor outside the main lobe, which is
    # ~1/bandwidth wide -- a fixed 1-sample exclusion would sit on the lobe and
    # read a near-primary "secondary" (spuriously low confidence).
    bandwidth = max(1.0, band_hz[1] - band_hz[0])
    exclude = max(upsample, int(round(sample_rate / bandwidth * upsample)))
    masked = abs_cc[idxs].copy()
    for j, gi in enumerate(idxs):
        if abs(gi - peak_idx) <= exclude or abs(gi - peak_idx) >= m - exclude:
            masked[j] = 0.0
    secondary = float(masked.max()) if masked.size else 0.0
    confidence = max(0.0, (primary - secondary) / primary) if primary > 0 else 0.0
    max_lag_native = max_lag_up / upsample
    at_edge = abs(lag_samples) >= max_lag_native - 1.0
    return lag_samples, polarity_sign, confidence, at_edge


def _gcc_local_peak_snap(
    a: np.ndarray,
    b: np.ndarray,
    *,
    sample_rate: int,
    band_hz: tuple[float, float],
    upsample: int,
    anchor_lag_samples: float,
    radius_samples: float,
) -> float | None:
    """Snap ``anchor_lag_samples`` to the nearest local maximum of the
    band-limited GCC-PHAT correlation of ``a`` vs ``b`` within ±``radius_samples``.

    Returns the refined native lag of the nearest interior local maximum of the
    correlation MAGNITUDE whose BIN lies within the radius (the parabolic refine
    may nudge the returned lag up to one upsampled bin past it), or ``None``
    when the radius holds none (the caller then keeps the bare anchor).
    Ianniello's gated correlator
    (docs/crossover-measurement-reproducibility-plan.md §10): the anchor owns
    comb-lobe selection, so this refines inside one λ/6 lobe instead of
    trusting the global correlation peak.
    """
    cc, m = _gcc_correlation(
        a, b, sample_rate=sample_rate, band_hz=band_hz, upsample=upsample,
    )
    abs_cc = np.abs(cc)
    # Circular array read modularly (upsampled lag ℓ -> index ℓ % m); a local
    # maximum is a bin strictly greater than both neighbours.
    anchor_up = anchor_lag_samples * upsample
    radius_up = abs(radius_samples) * upsample
    lo = int(math.floor(anchor_up - radius_up))
    hi = int(math.ceil(anchor_up + radius_up))
    best_ell: int | None = None
    best_dist = float("inf")
    for ell in range(lo, hi + 1):
        # The integer sweep brackets the fractional radius; keep only bins
        # genuinely inside it.
        if abs(ell - anchor_up) > radius_up:
            continue
        idx = ell % m
        if abs_cc[idx] <= abs_cc[(idx - 1) % m] or abs_cc[idx] <= abs_cc[(idx + 1) % m]:
            continue
        dist = abs(ell - anchor_up)
        if dist < best_dist:
            best_dist = dist
            best_ell = ell
    if best_ell is None:
        return None
    refined = parabolic_peak(abs_cc, best_ell % m)
    circ = refined if refined <= m / 2 else refined - m
    return float(circ / upsample)


def fractional_shift(x: np.ndarray, samples: float) -> np.ndarray:
    """Shift ``x`` right by ``samples`` (may be fractional) via linear phase."""
    n = x.size
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n)
    return np.fft.irfft(spectrum * np.exp(-2j * np.pi * freqs * samples), n=n)
