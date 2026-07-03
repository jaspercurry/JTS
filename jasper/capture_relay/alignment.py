# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-correlation alignment-confidence gate (phone-mic relay step 6, Pi side).

Measurement validity must extend past transport to whether the number is
trustworthy (plan §9). The integrity hash proves the WAV is intact; it cannot
catch an **intact-but-misaligned** capture — the phone recorded a window, but the
stimulus is buried in noise, clipped, or absent, so the cross-correlation peak
that locates it is weak or ambiguous. That is a silently-wrong measurement unless
it fails loud, which is what this module does.

It is a reusable primitive: the per-flow adapter passes the decrypted capture and
the **known** stimulus the Pi played (a sweep, a marker, …), and gets back an
alignment with a 0..1 confidence. When the spec's `validity.require_alignment` is
set, a confidence below threshold raises `AlignmentError` so the household is told
the measurement failed rather than handed a wrong correction.

Confidence is the normalized margin between the dominant correlation peak and the
next-strongest peak OUTSIDE the main lobe: `(primary - secondary) / primary`. A
clean capture has one dominant lag (confidence → 1); noise/absent stimulus has
comparable peaks everywhere (confidence → 0).

Two honesty notes on the v1 instrument (SNR-aware thresholds are a future
refinement, mirroring the correction confidence model's staging):

  - The correlation is computed by **FFT** (`scipy.signal.correlate(method="fft")`,
    O(N log N)) — the repo's standard for capture-length signals (cf. the
    FFT-based `jasper/audio_measurement/deconv.py` and its 1 GB-Pi size cap). A naive
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
"""
from __future__ import annotations

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
    cap = _normalize(captured)
    stim = _normalize(stimulus)
    if cap.size == 0 or stim.size == 0 or cap.size < stim.size:
        # A capture shorter than the stimulus cannot contain it — no alignment.
        return AlignmentResult(lag_samples=0, confidence=0.0, peak=0.0, secondary=0.0)

    # Cost/memory backstop: truncate a pathologically long capture (the stimulus
    # is always within the spec window, well under the cap).
    max_cap = max(stim.size, int(max_capture_s * sample_rate))
    if cap.size > max_cap:
        cap = cap[:max_cap]

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
    secondary = float(masked.max()) if masked.size else 0.0

    confidence = max(0.0, (primary - secondary) / primary)
    return AlignmentResult(
        lag_samples=primary_idx,
        confidence=confidence,
        peak=primary,
        secondary=secondary,
    )


def assert_alignment_confident(
    captured: np.ndarray,
    stimulus: np.ndarray,
    *,
    require: bool = True,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    exclude_radius: int | None = None,
) -> AlignmentResult:
    """Score alignment and, when `require`, fail loud below `threshold`.

    `require` mirrors the spec's `validity.require_alignment`. When False the
    result is returned for reporting without gating (e.g. a level measurement
    where alignment is informational, not decisive).
    """
    result = cross_correlation_alignment(
        captured,
        stimulus,
        sample_rate=sample_rate,
        exclude_radius=exclude_radius,
    )
    if require and result.confidence < threshold:
        raise AlignmentError(
            f"weak/ambiguous alignment (confidence {result.confidence:.2f} < "
            f"{threshold:.2f}) — the stimulus could not be located in the capture",
            confidence=result.confidence,
            threshold=threshold,
        )
    return result
