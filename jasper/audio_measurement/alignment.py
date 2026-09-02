# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-correlation alignment: the confidence gate and the sub-sample refine.

Measurement validity must extend past transport to whether the number is
trustworthy. An integrity hash proves a capture is intact; it cannot catch an
**intact-but-misaligned** capture — the recorder captured a window, but the
stimulus is buried in noise, clipped, or absent, so the cross-correlation peak
that locates it is weak or ambiguous. That is a silently-wrong measurement unless
it fails loud, which is what this module does.

It is a reusable primitive: an owning analysis passes the capture and the
**known** stimulus that was played (a sweep, a marker, …), and gets back an
alignment with a 0..1 confidence. ``assert_alignment_confident`` can turn that
score into a hard gate, but callers must validate their per-flow threshold before
advertising a hard gate of their own. The phone-relay room-correction flow is
observation-only today and deliberately does not use this uncalibrated 0.40
default as a fleet-wide rejection threshold.

Confidence is the normalized margin between the dominant correlation peak and the
next-strongest peak OUTSIDE the main lobe: `(primary - secondary) / primary`. A
clean capture has one dominant lag (confidence → 1); noise/absent stimulus has
comparable peaks everywhere (confidence → 0).

Two honesty notes on the v1 instrument (SNR-aware thresholds are a future
refinement, mirroring the correction confidence model's staging):

  - The correlation is computed by **FFT** (`scipy.signal.correlate(method="fft")`,
    O(N log N)) — the repo's standard for capture-length signals (cf. the
    FFT-based :mod:`.deconv` and its 1 GB-Pi size cap). A naive
    time-domain `np.correlate` here was O(N·M) ≈ tens of seconds per position on
    the Pi for a 10 s sweep.
  - The metric is a peak-to-second-peak **margin**, not an SNR or peak-to-RMS
    ratio. The `secondary` is sampled outside a physically-motivated ~5 ms
    exclusion (the main correlation lobe), so a near-direct reflection counts as
    a competing peak. KNOWN false-pass class: a loud-but-wrong capture that is
    still sharply self-peaked (e.g. clipped) can clear the threshold; the gate
    catches *ambiguous* alignment, not every invalid capture. The 0.40 default
    is a conservative starting gate, not a measured-derived constant — tune it
    against real on-device sweeps before relying on small-margin decisions.

The gate above resolves to the integer sample; the sub-sample family below
(``gcc_phat``'s band-limited phase-transform correlation, its ``parabolic_peak``
refine, and the anchor-gated ``_gcc_local_peak_snap``) resolves past it, so one
module owns every way this repo locates one signal inside another —
and, in ``fractional_shift``, the way it applies what it found.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np
from scipy import signal as scipy_signal


def _env_threshold(default: float = 0.40) -> float:
    """The default confidence gate, overridable at deploy time.

    The 0.40 default is NOT empirically derived — a conservative v1 starting
    point. Tuning it needs on-device sweeps, so it is a deploy-time knob
    (`JASPER_CAPTURE_ALIGNMENT_THRESHOLD`, 0..1) rather than a code change: set it
    in jasper.env once measured, no rebuild required.
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


# A clean swept-sine alignment is strongly peaked; default gate is conservative.
DEFAULT_CONFIDENCE_THRESHOLD = _env_threshold()
# Exclude the main correlation lobe (~a few ms) when picking the competing peak.
DEFAULT_EXCLUDE_RADIUS_S = 0.005
# Cost/memory backstop: truncate a pathologically long capture. The stimulus
# always lands within the spec's pre+stimulus+post window (well under this), so
# truncation never drops it. Mirrors deconv.py's DEFAULT_MAX_CAPTURE_SECONDS.
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
    confidence: float  # 0..1 margin of the dominant peak over the next-strongest
    peak: float  # normalized correlation at the dominant lag (0..1 similarity)
    secondary: float  # strongest competing peak (normalized)
    secondary_lag_samples: int = 0  # where it sits; only meaningful when secondary > 0


def _normalize(signal: np.ndarray) -> np.ndarray:
    x = np.asarray(signal, dtype=np.float64).ravel()
    # A NaN/inf-laden capture must not poison the norm (and the reported peak).
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = x - x.mean() if x.size else x
    norm = np.linalg.norm(x)
    return x / norm if norm > 0 else x


def cross_correlation_alignment(
    captured: np.ndarray,
    stimulus: np.ndarray,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    exclude_radius: int | None = None,
    max_capture_s: float = DEFAULT_MAX_CAPTURE_S,
) -> AlignmentResult:
    """Locate `stimulus` inside `captured` and score the confidence of that lag.

    Both are mean-removed and unit-normalized so `peak` is a 0..1 similarity. The
    correlation is FFT-accelerated. The `secondary` peak is the strongest
    correlation outside a ~5 ms exclusion around the primary (the main lobe), and
    `confidence` is the normalized margin between them. `exclude_radius` defaults
    to `DEFAULT_EXCLUDE_RADIUS_S * sample_rate`; pass an override only for tests.
    """
    cap_input = np.asarray(captured)
    stim_input = np.asarray(stimulus)
    if cap_input.size == 0 or stim_input.size == 0:
        # A capture shorter than the stimulus cannot contain it — no alignment.
        return AlignmentResult(lag_samples=0, confidence=0.0, peak=0.0, secondary=0.0)

    # Apply the cost/memory backstop before normalization.  Normalization
    # promotes to float64, so truncating afterwards would still allocate a
    # full-sized copy of a pathological capture on the 1 GB Pi.
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
        # A capture shorter than the stimulus cannot contain it — no alignment.
        return AlignmentResult(lag_samples=0, confidence=0.0, peak=0.0, secondary=0.0)

    corr = np.abs(scipy_signal.correlate(cap, stim, mode="valid", method="fft"))
    if corr.size == 0:
        return AlignmentResult(lag_samples=0, confidence=0.0, peak=0.0, secondary=0.0)

    primary_idx = int(np.argmax(corr))
    primary = float(corr[primary_idx])
    if not np.isfinite(primary) or primary <= 0.0:
        return AlignmentResult(lag_samples=primary_idx, confidence=0.0, peak=0.0, secondary=0.0)

    if exclude_radius is None:
        exclude_radius = max(1, int(DEFAULT_EXCLUDE_RADIUS_S * sample_rate))
    masked = corr.copy()
    lo = max(0, primary_idx - exclude_radius)
    hi = min(corr.size, primary_idx + exclude_radius + 1)
    masked[lo:hi] = 0.0
    secondary_idx = int(np.argmax(masked))
    secondary = float(masked[secondary_idx])

    confidence = max(0.0, (primary - secondary) / primary)
    return AlignmentResult(
        lag_samples=primary_idx,
        confidence=confidence,
        peak=primary,
        secondary=secondary,
        secondary_lag_samples=secondary_idx,
    )


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
    """Score alignment and, when `require`, fail loud below `threshold`.

    ``require`` selects enforcement for the owning, threshold-calibrated flow.
    When False the result is returned for reporting without gating.
    """
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


# --------------------------------------------------------------------------- #
# sub-sample refinement: past the integer lag the gate above stops at
# --------------------------------------------------------------------------- #


def parabolic_peak(values: np.ndarray, idx: int) -> float:
    """Sub-sample offset of a peak at integer ``idx`` via 3-point parabola.

    The refinement is clamped to ±1 bin: a true local maximum refines within
    ±0.5 bin, so a larger offset means the three points are near-degenerate
    (tiny ``denom``) and the parabola vertex is an extrapolation artifact —
    unclamped, a flat-topped correlation once "refined" a 96-bounded peak out
    to 128 samples. In that case the integer peak is the honest answer.
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
    """Band-limited GCC-PHAT cross-correlation of ``a`` vs ``b``, ×``upsample``
    FFT-interpolated.

    Returns ``(cc, m)``: ``cc`` is the length-``m`` upsampled real
    cross-correlation on the circular-lag axis (index ``i`` → lag ``i`` for
    ``i <= m/2`` else ``i - m``; native lag = index / ``upsample``). The
    cross-power is phase-transform weighted **only inside ``band_hz``**
    (whitening the near-zero out-of-band bins otherwise piles a spurious peak
    near zero lag). Shared core of :func:`gcc_phat` (global-peak seed) and
    :func:`_gcc_local_peak_snap` (anchor-gated fine snap), so both read one
    correlation formula rather than two that could silently drift apart.
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

    Returns ``(lag_samples, polarity_sign, confidence, at_edge)``. The
    correlation (see :func:`_gcc_correlation`) is ×``upsample`` FFT-interpolated
    and parabolically refined. ``polarity_sign`` is the sign of the (signed)
    correlation at the peak, and ``confidence`` mirrors
    :func:`cross_correlation_alignment`'s primary-over-secondary margin.

    ``at_edge`` is True when the peak lands within one native sample of the
    ±``max_lag_samples`` search bound — the true peak is likely OUTSIDE the
    window and the returned lag is a clamped artifact the caller must refuse.
    """
    cc, m = _gcc_correlation(
        a, b, sample_rate=sample_rate, band_hz=band_hz, upsample=upsample,
    )
    # Circular-lag axis: index i → lag i for i<=m/2 else i-m; native = /upsample.
    max_lag_up = int(round(max_lag_samples * upsample))
    max_lag_up = max(1, min(max_lag_up, m // 2 - 1))
    idxs = np.concatenate(
        [np.arange(0, max_lag_up + 1), np.arange(m - max_lag_up, m)]
    )
    window = cc[idxs]
    peak_local = int(np.argmax(np.abs(window)))
    peak_idx = int(idxs[peak_local])
    # Parabolic refine on |cc| around the (unwrapped) peak.
    abs_cc = np.abs(cc)
    refined = parabolic_peak(abs_cc, peak_idx)
    circ = refined if refined <= m / 2 else refined - m
    lag_samples = circ / upsample
    polarity_sign = 1 if cc[peak_idx] >= 0 else -1
    primary = float(abs_cc[peak_idx])
    # Secondary: strongest competitor outside the correlation main lobe. A
    # band-limited correlation's main lobe is ~1/bandwidth wide, so a fixed
    # 1-sample exclusion would sit on the main lobe and read a near-primary
    # "secondary" (spuriously low confidence). Exclude one main-lobe half-width.
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

    Reuses the exact upsampled phase-transform machinery of :func:`gcc_phat`
    (via the shared :func:`_gcc_correlation` core) and the same ±1-bin
    :func:`parabolic_peak` sub-sample refine. Returns the refined native lag of
    the nearest genuine interior local maximum of the correlation MAGNITUDE — an
    upsampled bin strictly greater than both its neighbours — whose bin lies
    within the radius of the anchor (the parabolic refine may nudge the returned
    lag by up to one upsampled bin past it); ``None`` when the radius contains no
    such peak (the caller then keeps the bare anchor). "Nearest" = smallest
    ``|lag − anchor|``.

    Ianniello's gated correlator
    (docs/crossover-measurement-reproducibility-plan.md §10): the
    drift-corrected physical peak-gap anchor already owns comb-lobe selection,
    so this refines it inside one λ/6 lobe instead of trusting the global
    correlation peak, which can land on a neighbouring stable-but-wrong lobe.
    """
    cc, m = _gcc_correlation(
        a, b, sample_rate=sample_rate, band_hz=band_hz, upsample=upsample,
    )
    abs_cc = np.abs(cc)
    # Search the ±radius neighbourhood in UPSAMPLED-lag units around the anchor,
    # reading the circular array modularly (upsampled lag ℓ → index ℓ % m). A
    # local maximum is an upsampled bin strictly greater than both neighbours.
    anchor_up = anchor_lag_samples * upsample
    radius_up = abs(radius_samples) * upsample
    lo = int(math.floor(anchor_up - radius_up))
    hi = int(math.ceil(anchor_up + radius_up))
    best_ell: int | None = None
    best_dist = float("inf")
    for ell in range(lo, hi + 1):
        # The integer sweep brackets the fractional radius; keep only bins
        # genuinely inside it. (The parabolic refine below can nudge the RETURNED
        # lag by at most one upsampled bin past the radius — negligible against
        # the comb-lobe spacing, so no lobe jump.)
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
    """Shift ``x`` right by ``samples`` (may be fractional) via linear phase.

    The companion to :func:`gcc_phat`: that one measures a sub-sample lag,
    this one applies it without quantising it back to a whole sample.
    """
    n = x.size
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n)
    return np.fft.irfft(spectrum * np.exp(-2j * np.pi * freqs * samples), n=n)
