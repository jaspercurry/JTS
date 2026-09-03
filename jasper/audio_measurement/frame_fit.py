# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The FRAME between two curves: one offset, one tilt, fitted and disclosed.

    frame(f) = offset_db + tilt_db_per_octave · log2(f / pivot_hz)

fitted by least squares to ``measured_db − reference_db`` over the bins the
caller hands in. Two scalars is the whole vocabulary — **there is no general
curve transformation here and there must not be one**, which would let a
comparison re-shape one side until it agreed. This module owns no band, no
threshold and no verdict, it is not a goodness-of-fit test, and it does not
attribute the tilt to instrument or model.

Units: frequencies in hertz, every level in decibels, the tilt in decibels per
OCTAVE (per unit of ``log2(f)``) — never per decade. A positive ``offset_db``
means the measured curve sits above the reference; a positive tilt means it
rises relative to the reference as frequency rises.

``pivot_hz`` is the geometric mean of the fitted bins, so the design matrix's
two columns are orthogonal over them and ``offset_db`` is exactly
``mean(measured − reference)`` there. Downstream the consequence is stronger
and does not depend on that identity: both graders in
:mod:`jasper.audio_measurement.analysis` mean-centre their error, so removing a
fitted frame can only move their numbers by its TILT, whatever bin set either
used.

The caller owes this the bins the comparison actually TRUSTS, not merely those
inside the band. A straight line through a deep modelled notch lets the notch
lever the slope: measured on the product path with a 25 dB notch at a band
edge, an injected −0.800 dB/octave frame came back **+0.226** — the wrong sign.

A call site keeps its REPORTED grade byte-identical and adds the frame-removed
one beside it; what it GATES on is its own decision.
:func:`jasper.active_speaker.delta_probe.classify_delta_probe` re-asks the
ROLLBACK question with the frame removed, because refusing a correction on
evidence that cannot be told apart from the microphone is the worse error
(owner ruling, #2521). ``program_analysis._analyze_verify`` still gates on its
raw number: it refuses a capture, not a correction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Fewest bins a fit is attempted on. Two parameters through two points is
#: exact and says nothing, so three is the smallest set with any residual left
#: to be wrong about. Deliberately low: it keeps the solve well-posed and
#: expresses no policy about when a frame is worth believing — the call sites
#: carry their own, stricter, band and bin-count rules.
MIN_FRAME_FIT_BINS: int = 3


@dataclass(frozen=True)
class FrameFit:
    """The fitted ``(offset, tilt)`` between two curves' frames.

    ``offset_db``/``tilt_db_per_octave`` are ``None`` together and only
    together, and ``None`` means **not measured** — never "measured, and zero".

    Args:
      offset_db: the frame's level term, dB, stated at ``pivot_hz``. Equal to
        the mean difference over the fitted bins, which is NOT necessarily the
        constant any particular grader mean-centres away.
      tilt_db_per_octave: the frame's slope term, dB per octave.
      pivot_hz: the frequency at which the frame equals ``offset_db``; the
        geometric mean of the fitted bins. Derived, not fitted.
      n_bins: how many bins the two numbers were fitted over.
      band_hz: ``(lowest, highest)`` fitted bin frequency — the span the fit
        saw, which is **not** the comparison's graded band. With ``n_bins`` it
        is the fit's own ill-conditioning defence, so both ride the persisted
        record rather than being reduced away.
    """

    offset_db: float | None
    tilt_db_per_octave: float | None
    pivot_hz: float | None
    n_bins: int
    band_hz: tuple[float, float] | None

    @property
    def fitted(self) -> bool:
        """Whether a frame was measured at all."""
        return self.offset_db is not None and self.tilt_db_per_octave is not None

    def frame_db(self, freqs_hz: Any) -> np.ndarray:
        """The fitted frame evaluated on ``freqs_hz``.

        Subtracting this from the measured curve removes the frame. An
        **unfitted** frame evaluates to all zeros, so callers must gate what
        they REPORT on :attr:`fitted` — otherwise a frame-removed residual
        identical to the raw one reads as "the frames agreed" instead of
        "nothing was measured".

        Bins at or below 0 Hz evaluate to the offset alone: ``log2`` has no
        answer there.
        """
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
        """JSON-safe disclosure record."""
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
    """Least-squares ``offset + tilt·log2(f)`` through ``measured − reference``.

    All three arrays share one grid and are **already restricted to the bins
    the comparison grades**. This function owns no band logic: the call site has
    already resolved which bins are a measurement, and a second opinion here
    would be a second owner of that decision.

    A bin is dropped when either curve is non-finite there, or when its
    frequency is not strictly positive (``log2`` has no answer at DC). Returns
    :data:`FRAME_UNFITTED` when fewer than :data:`MIN_FRAME_FIT_BINS` bins
    survive that, or when every survivor sits at one frequency — no span, no
    slope to fit.

    Raises:
      ValueError: if the three arrays are not 1-D and the same length. A grid
        mismatch is a caller bug, not a measurement outcome, so it is loud.
    """
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

    # Orthogonal columns (`centered` sums to zero by construction), so this is
    # the closed form of the same solve `np.linalg.lstsq` would run.
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
    """One cross-frame comparison's disclosure: the frame, and both grades.

    The single typed owner of the frame disclosure. It owns the association
    between one :class:`FrameFit` and its grade pair, and their JSON shape; it
    deliberately does NOT own how either grade is computed. The caller's own
    grader produces both — the raw one as it always did, the tilt-removed one by
    re-running that same grader on a frame-removed curve — so the two are one
    construction over different inputs rather than two reductions that can
    drift.

    Three bin sets meet here: the frame was fitted over
    ``fit.band_hz``/``fit.n_bins`` — the bins the comparison TRUSTS — while each
    grade keeps whatever bins its own grader reduces. Only the tilt can cross
    those bin sets, because mean-centring is invariant to any additive constant.

    Args:
      fit: the fitted frame. ``FRAME_UNFITTED`` when none could be measured.
      raw_rms_db, raw_max_db: the comparison's grades as its own grader produced
        them, dB. **These are what gates and what is refused on.** ``None`` when
        the caller reports no such grade.
      tilt_removed_rms_db, tilt_removed_max_db: the same two grades, dB, after
        the fitted frame was removed from the measured curve. ``None`` when no
        frame was fitted — never defaulted to the raw twin, which would read as
        "removing the frame changed nothing" instead of "no frame was measured".
    """

    fit: FrameFit
    raw_rms_db: float | None = None
    raw_max_db: float | None = None
    tilt_removed_rms_db: float | None = None
    tilt_removed_max_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe disclosure record — the frame's terms, then both grades.

        Grades nest under ``raw``/``tilt_removed`` rather than flattening with
        suffixes, so a reader picks a frame of reference once and reads a
        matching pair instead of pairing keys by name.
        """
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
