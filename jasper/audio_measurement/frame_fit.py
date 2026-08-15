# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The FRAME between two curves: one offset, one tilt, fitted and disclosed.

**Pipeline stage: DIAGNOSE.** In the measure → derive → diagnose → prescribe →
re-measure chain this module sits at the moment two derived curves are about to
be differenced and graded. It owns the frame model and the typed disclosure
record for one such comparison. It owns **no band** (the caller has already
resolved which bins are a measurement), **no threshold**, **no verdict**, and
no notion of drivers, sittings, or instruments.

Two magnitude curves this flow compares are often not measured in the same
**frame** — different instrument, different sitting, or (the common one) an
on-axis two-branch *model* against an in-room gated *measurement*. A frame
difference is not a defect in either curve; it is a property of the comparison,
and it lands in the difference whether or not anybody names it.

This module names two terms of it, and only two:

    frame(f) = offset_db + tilt_db_per_octave · log2(f / pivot_hz)

fitted by least squares to ``measured_db − reference_db`` over the bins the
caller hands in. Nothing else. **There is no general "frame transformation"
here and there must not be one** — a curve-warping framework would let a
comparison quietly re-shape one side until it agreed, which is the opposite of
what this exists for. Two scalars is the whole vocabulary.

**Units.** Frequencies in hertz; every level, deviation, and residual in
decibels; the tilt in **decibels per octave** (per doubling of frequency, i.e.
per unit of ``log2(f)``) — never per decade. A positive ``offset_db`` means the
measured curve sits above the reference; a positive ``tilt_db_per_octave``
means it rises relative to the reference as frequency rises.

Why these two, and why now
--------------------------
The offset half is already implicit everywhere in this flow: every spec-facing
gauge normalises a curve to its own band power mean, and
:func:`jasper.audio_measurement.analysis.tracking_error_db` mean-centres its
error. The tilt half was left free, and on the 2026-07-30/07-29 corpus it was
**the headline**. The replay scorecard reported the flow's predictions as
"2.02× optimistic"; the 2026-07-31 first-principles panel refitted the same
arrays and found a single −0.788 dB/octave tilt between the frames accounted
for **84 %** of that gap (raw model error 2.054 dB rms → 1.335 dB with the one
scalar removed, and the crossover band's shape correlation +0.97). One number
that read as "the model is twice as wrong as it claims" was mostly the
instrument.

So the product's job is not to *correct* for the tilt. It is to stop reporting
a cross-frame difference as if the frames matched. Fit it, say what it was, and
show the residual both ways.

What this does NOT do
---------------------
* **It never replaces a REPORTED grade.** Every call site keeps its existing
  number byte-identical and adds the frame-removed one beside it. A tilt is
  evidence, not a licence to restate a measurement.

  **What a call site may GATE on is its own decision, and one of them now
  differs** (owner ruling 2026-08-15, recorded in issue #2521). This module's
  original doctrine said the raw number was always the honest one to gate on,
  on the reasoning that an unattributed tilt could be model error as easily as
  instrument. That reasoning still holds — this fit does not attribute anything
  — but it settles the question the other way for a consumer whose verdict
  REVERTS a household's tuning. :func:`jasper.active_speaker.delta_probe.
  classify_delta_probe` therefore splits the two questions: the raw grade
  decides whether there is a finding at all, and the ROLLBACK question is
  re-asked with the frame removed, because refusing a correction on evidence
  that cannot be told apart from the microphone is the worse error of the two.
  Its frame is fitted over its own quiet bins, not over a graded band, which is
  what keeps a defect from removing itself; the verdict it demotes to is
  disclosed rather than silent.

  The tracking check in ``program_analysis._analyze_verify`` is unchanged and
  still gates on its raw number: it refuses a capture, not a correction, and a
  refused capture costs a re-measure rather than a tuning.
* **It does not attribute the tilt.** A smooth monotonic ramp across several
  octaves is *consistent with* a directivity/spatial-frame difference; it is
  also consistent with a mis-modelled broad filter. This module measures; it
  makes no claim about cause.
* **It is not a goodness-of-fit test.** A two-parameter fit over a narrow span,
  or over a handful of bins, is ill-conditioned and will happily return a large
  tilt that means nothing. :attr:`FrameFit.n_bins` and :attr:`FrameFit.band_hz`
  ride in the record so a reader can see the span the two numbers were measured
  over, rather than this module inventing a confidence policy nobody asked for.

The pivot, and why only the TILT can move a grade
-------------------------------------------------
``pivot_hz`` is the geometric mean of the fitted bins' frequencies, so the
design matrix's two columns are orthogonal over those bins. That makes
``offset_db`` **exactly** ``mean(measured − reference)`` over the fitted bins,
and ``tilt_db_per_octave`` purely the additional term: the two disclosed
numbers decompose the frame without interacting. The pivot is derived from the
caller's bins, not a third fitted parameter; it travels in the record so the
frame curve can be reconstructed, and so a reader can see where the offset is
stated.

The consequence that matters downstream is stronger than that identity, and
does not depend on it: **mean-centring is invariant to any additive constant.**
Both graders in
:mod:`jasper.audio_measurement.analysis` mean-centre their error
(:func:`~jasper.audio_measurement.analysis._offset_invariant_rms_and_max`), so
removing a fitted frame can only move their numbers by its TILT — whatever bin
set the frame was fitted over, and whichever bins each grader then reduces.
(The identity above holds against ``tracking_error_db``'s full-band mean;
``notch_excluded_tracking_error_db`` centres over its notch-kept subset
instead, so the two constants are not the same number. The invariant is what
makes that difference irrelevant.)

Which bins to fit over — the caller's decision, and it matters
--------------------------------------------------------------
This function fits whatever bins it is handed, so the caller owes it the bins
the comparison actually **trusts**, not merely the ones inside the band. A
deep modelled interference notch is the case that bites: its depth is
hypersensitive to sub-dB branch differences, so a straight line through one
lets the notch lever the slope. Measured on the product path with a 25 dB notch
at a band edge, an injected −0.800 dB/octave frame came back **+0.226** — the
wrong sign — and a notch at band centre made the frame-removed residual WORSE
than the raw one. ``program_analysis._analyze_verify`` therefore fits over
:func:`jasper.audio_measurement.analysis.notch_excluded_band_mask`'s bins, and
records that span in :attr:`FrameFit.band_hz` / :attr:`FrameFit.n_bins`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Fewest bins a fit is attempted on. Two parameters through two points is
#: exact and says nothing, so three is the smallest set with any residual left
#: to be wrong about. Deliberately low: the call sites carry their own,
#: stricter, band and bin-count rules, and this floor exists only to keep the
#: least-squares solve well-posed rather than to express a policy about when a
#: frame is worth believing.
MIN_FRAME_FIT_BINS: int = 3


@dataclass(frozen=True)
class FrameFit:
    """The fitted ``(offset, tilt)`` between two curves' frames.

    The measurement half of this module's contract: two scalars and the span
    they were measured over. It carries no grade — :class:`FrameComparison`
    holds the grades this frame was fitted in service of.

    ``offset_db``/``tilt_db_per_octave`` are ``None`` together and only
    together, and ``None`` means **not measured** — never "measured, and zero".
    A comparison that could not be fitted (too few finite shared bins, or every
    bin at one frequency) discloses absence rather than a fabricated flat frame.

    Args:
      offset_db: the frame's level term, dB, stated at ``pivot_hz``. Positive
        means ``measured`` sits above ``reference``. Equal to the mean of the
        difference over the fitted bins (see the module docstring's pivot
        note) — which is NOT necessarily the constant any particular grader
        mean-centres away, and does not need to be.
      tilt_db_per_octave: the frame's slope term, dB per octave. Negative means
        ``measured`` falls relative to ``reference`` as frequency rises — the
        sign the 2026-07-29 corpus's model-vs-room comparison carried.
      pivot_hz: the frequency at which the frame equals ``offset_db``; the
        geometric mean of the fitted bins. Derived, not fitted.
      n_bins: how many bins the two numbers were fitted over.
      band_hz: ``(lowest, highest)`` fitted bin frequency — the span the fit
        saw, which is **not** the comparison's graded band: the caller hands in
        the bins it trusts (see the module docstring's bin-set section), and
        non-finite ones are dropped on top of that. Together with ``n_bins``
        this is the fit's own ill-conditioning defence, which is why both ride
        the persisted record rather than being reduced away.
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
        **unfitted** frame evaluates to all zeros, so a caller that subtracts it
        unconditionally gets its raw comparison back unchanged — the honest
        degradation, since no frame was measured to remove. Callers must still
        gate what they *report* on :attr:`fitted`, or a reader would see a
        frame-removed residual identical to the raw one and read that as "the
        frames agreed" instead of "nothing was measured".

        Bins at or below 0 Hz evaluate to the offset alone: ``log2`` has no
        answer there, and a DC bin is not a place a tilt is defined.
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
    the comparison grades** — the shared valid band. This function owns no band
    logic on purpose: the call site has already resolved which bins are a
    measurement (validity floor, sweep coverage, notch exclusion), and a second
    opinion here would be a second owner of that decision.

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

    # Orthogonal columns (``centered`` sums to zero by construction), so this
    # is the closed form of the same least-squares solve `np.linalg.lstsq`
    # would run — and it makes the offset's identity with the plain mean
    # visible rather than implied.
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

    **The single typed owner of the frame disclosure** (rung P1). Everything a
    reader needs to judge a cross-frame difference travels together here — what
    the frame was, what the comparison graded, and what it grades to once the
    frame is out — so no surface can render half of it and no consumer has to
    reassemble it from loose keys.

    What it owns: the association between one :class:`FrameFit` and the grade
    pair it belongs to, and their JSON shape. What it deliberately does NOT
    own: **how either grade is computed.** The caller's own grader produces
    both numbers — the raw one exactly as it always did, and the tilt-removed
    one by re-running that same grader on a frame-removed curve — so the two
    are the same construction over different inputs rather than two
    reductions that could drift. This record carries the results; it never
    recomputes them.

    **Three bin sets meet here, and the record names them.** The frame was
    fitted over ``fit.band_hz``/``fit.n_bins`` — the bins the comparison
    TRUSTS — while each grade keeps whatever bins its own grader reduces
    (which for a notch-excluded max is not the plain band). That is deliberate:
    estimating the frame from bins a comparator refuses to grade lets a
    modelled notch lever the slope, while re-grading with anything other than
    each grader's established convention would change what the number means.
    The offset term cannot cross those bin sets — mean-centring is invariant to
    any additive constant — so only the tilt travels.

    **The raw pair here is the reported pair, not a copy of it.** The caller
    assigns both from the same locals it publishes to its own record; a test
    pins the identity so the two views can never disagree.

    Args:
      fit: the fitted frame. ``FRAME_UNFITTED`` when none could be measured.
      raw_rms_db, raw_max_db: the comparison's grades as its own grader
        produced them, dB. **These are what gates and what is refused on** —
        rung P1 changes neither. ``None`` when the caller reports no such
        grade.
      tilt_removed_rms_db, tilt_removed_max_db: the same two grades, dB, taken
        after the fitted frame was removed from the measured curve. ``None``
        when no frame was fitted — never defaulted to the raw twin, which
        would read as "removing the frame changed nothing" (a measurement)
        instead of "no frame was measured" (an absence).
    """

    fit: FrameFit
    raw_rms_db: float | None = None
    raw_max_db: float | None = None
    tilt_removed_rms_db: float | None = None
    tilt_removed_max_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe disclosure record — the frame's terms, then both grades.

        Grades are nested under ``raw``/``tilt_removed`` rather than flattened
        with suffixes so a reader (or a renderer) picks a frame of reference
        once and reads a matching pair, instead of pairing keys by name.
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
