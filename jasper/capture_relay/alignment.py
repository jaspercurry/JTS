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
next-strongest peak elsewhere: `(primary - secondary) / primary`. A clean capture
has one dominant lag (confidence → 1); noise/absent stimulus has comparable peaks
everywhere (confidence → 0). This is a v1 instrument; SNR-aware thresholds are a
future refinement (mirrors the correction confidence model's staging).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# A clean swept-sine alignment is strongly peaked; default gate is conservative.
DEFAULT_CONFIDENCE_THRESHOLD = 0.40


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
    x = x - x.mean() if x.size else x
    norm = np.linalg.norm(x)
    return x / norm if norm > 0 else x


def cross_correlation_alignment(
    captured: np.ndarray,
    stimulus: np.ndarray,
    *,
    exclude_radius: int | None = None,
) -> AlignmentResult:
    """Locate `stimulus` inside `captured` and score the confidence of that lag.

    Both are mean-removed and unit-normalized so `peak` is a 0..1 similarity. The
    `secondary` peak is the strongest correlation outside a small exclusion window
    around the primary; `confidence` is the normalized margin between them.
    """
    cap = _normalize(captured)
    stim = _normalize(stimulus)
    if cap.size == 0 or stim.size == 0:
        return AlignmentResult(lag_samples=0, confidence=0.0, peak=0.0, secondary=0.0)

    corr = np.abs(np.correlate(cap, stim, mode="valid"))
    if corr.size == 0:
        return AlignmentResult(lag_samples=0, confidence=0.0, peak=0.0, secondary=0.0)

    primary_idx = int(np.argmax(corr))
    primary = float(corr[primary_idx])
    if primary <= 0.0:
        return AlignmentResult(lag_samples=primary_idx, confidence=0.0, peak=0.0, secondary=0.0)

    radius = exclude_radius if exclude_radius is not None else max(1, stim.size // 8)
    masked = corr.copy()
    lo = max(0, primary_idx - radius)
    hi = min(corr.size, primary_idx + radius + 1)
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
    exclude_radius: int | None = None,
) -> AlignmentResult:
    """Score alignment and, when `require`, fail loud below `threshold`.

    `require` mirrors the spec's `validity.require_alignment`. When False the
    result is returned for reporting without gating (e.g. a level measurement
    where alignment is informational, not decisive).
    """
    result = cross_correlation_alignment(
        captured, stimulus, exclude_radius=exclude_radius
    )
    if require and result.confidence < threshold:
        raise AlignmentError(
            f"weak/ambiguous alignment (confidence {result.confidence:.2f} < "
            f"{threshold:.2f}) — the stimulus could not be located in the capture",
            confidence=result.confidence,
            threshold=threshold,
        )
    return result
