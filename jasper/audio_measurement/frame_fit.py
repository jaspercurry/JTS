# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The FRAME between two curves: one offset, one tilt, fitted and disclosed.

    frame(f) = offset_db + tilt_db_per_octave · log2(f / pivot_hz)

Least squares to ``measured_db − reference_db`` over the bins the caller hands in — two scalars
is the whole vocabulary; no general curve transformation, band, threshold, or verdict. Units:
hertz, decibels, tilt in dB per OCTAVE (never decade). The caller owes the bins the comparison
actually TRUSTS, not merely those inside the band: a straight line through a deep modelled
notch levers the slope (measured on the product path, a 25 dB notch at a band edge flipped an
injected −0.800 dB/octave frame to +0.226).

A call site keeps its REPORTED grade byte-identical and adds the frame-removed one beside it;
what it GATES on is its own decision (owner ruling #2521 —
:func:`jasper.active_speaker.delta_probe.classify_delta_probe` re-asks its rollback question
with the frame removed; ``program_analysis._analyze_verify`` still gates on the raw number).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Fewest bins a fit is attempted on: two parameters through two points is exact and says
#: nothing, so three is the smallest set with any residual to be wrong about. Call sites carry
#: their own, stricter, bin-count policy.
MIN_FRAME_FIT_BINS: int = 3


@dataclass(frozen=True)
class FrameFit:
    """``offset_db``/``tilt_db_per_octave`` are ``None`` together, meaning not measured — never
    "measured, and zero". ``band_hz`` is the fit's own bin span, **not** the graded band."""

    offset_db: float | None
    tilt_db_per_octave: float | None
    pivot_hz: float | None
    n_bins: int
    band_hz: tuple[float, float] | None

    @property
    def fitted(self) -> bool:
        return self.offset_db is not None and self.tilt_db_per_octave is not None

    def frame_db(self, freqs_hz: Any) -> np.ndarray:
        """An **unfitted** frame evaluates to all zeros — callers must gate on :attr:`fitted`
        before reporting. Bins at or below 0 Hz evaluate to the offset alone."""
        freqs = np.asarray(freqs_hz, dtype=np.float64)
        offset_db, tilt, pivot_hz = self.offset_db, self.tilt_db_per_octave, self.pivot_hz
        if offset_db is None or tilt is None or pivot_hz is None:
            return np.zeros_like(freqs)
        positive = freqs > 0.0
        octaves = np.where(
            positive, np.log2(np.where(positive, freqs, 1.0) / pivot_hz), 0.0,
        )
        return offset_db + tilt * octaves

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset_db": self.offset_db,
            "tilt_db_per_octave": self.tilt_db_per_octave,
            "pivot_hz": self.pivot_hz,
            "n_bins": self.n_bins,
            "band_hz": list(self.band_hz) if self.band_hz is not None else None,
        }


#: What :func:`fit_frame` returns when there was nothing to fit.
FRAME_UNFITTED = FrameFit(
    offset_db=None, tilt_db_per_octave=None, pivot_hz=None, n_bins=0, band_hz=None,
)


def fit_frame(freqs_hz: Any, measured_db: Any, reference_db: Any) -> FrameFit:
    """Arrays must already be restricted to the bins the comparison grades — this function owns
    no band logic. Returns :data:`FRAME_UNFITTED` below :data:`MIN_FRAME_FIT_BINS` survivors, or
    when every survivor sits at one frequency. Raises ``ValueError`` on a 1-D/length mismatch."""
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    measured = np.asarray(measured_db, dtype=np.float64)
    reference = np.asarray(reference_db, dtype=np.float64)
    if not (freqs.ndim == measured.ndim == reference.ndim == 1):
        raise ValueError("frame-fit arrays must be 1-D")
    if not (freqs.size == measured.size == reference.size):
        raise ValueError("frame-fit arrays must have matching lengths")

    usable = (
        np.isfinite(freqs) & np.isfinite(measured) & np.isfinite(reference)
        & (freqs > 0.0)
    )
    n_bins = int(usable.sum())
    if n_bins < MIN_FRAME_FIT_BINS:
        return FRAME_UNFITTED

    f = freqs[usable]
    difference = measured[usable] - reference[usable]
    octaves = np.log2(f)
    pivot_log2 = float(np.mean(octaves))
    centered = octaves - pivot_log2
    span = float(np.max(centered) - np.min(centered))
    if not (span > 0.0):
        return FRAME_UNFITTED

    # Orthogonal columns (`centered` sums to zero); closed form of the `lstsq` solve.
    offset_db = float(np.mean(difference))
    tilt = float(np.dot(centered, difference) / np.dot(centered, centered))
    if not (math.isfinite(offset_db) and math.isfinite(tilt)):
        return FRAME_UNFITTED
    return FrameFit(
        offset_db=offset_db,
        tilt_db_per_octave=tilt,
        pivot_hz=float(2.0 ** pivot_log2),
        n_bins=n_bins,
        band_hz=(float(np.min(f)), float(np.max(f))),
    )


@dataclass(frozen=True)
class FrameComparison:
    """Does NOT own how either grade is computed; the caller's own grader produces both. ``raw_*``
    is what gates and is refused on. ``tilt_removed_*`` is ``None`` when no frame was fitted —
    never defaulted to the raw twin, which would read as "nothing changed" not "not measured"."""

    fit: FrameFit
    raw_rms_db: float | None = None
    raw_max_db: float | None = None
    tilt_removed_rms_db: float | None = None
    tilt_removed_max_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Grades nest under ``raw``/``tilt_removed`` rather than flattening with suffixes."""
        payload = self.fit.to_dict()
        payload["raw"] = {"rms_db": self.raw_rms_db, "max_db": self.raw_max_db}
        payload["tilt_removed"] = {
            "rms_db": self.tilt_removed_rms_db,
            "max_db": self.tilt_removed_max_db,
        }
        return payload


__all__ = [
    "FRAME_UNFITTED",
    "MIN_FRAME_FIT_BINS",
    "FrameComparison",
    "FrameFit",
    "fit_frame",
]
