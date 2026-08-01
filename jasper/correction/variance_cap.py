# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-position spread as a per-frequency ceiling on room-correction depth.

**Pipeline stage: PRESCRIBE.** In the measure → derive → diagnose → prescribe →
re-measure chain this module sits at the moment a room correction is about to
be *designed*, between the spatial statistics the capture already produced and
the greedy PEQ designer that will fit filters against them.

**What it owns.** One curve — cross-position sigma to allowed cut depth — its
tolerance, and the typed disclosure record for one design run.

**What it does NOT own.** Computing sigma: :mod:`jasper.correction.spatial` is
the single writer of the room layer's per-frequency cross-position statistics
(:class:`~jasper.correction.spatial.SpatialMatrix`), and this module reads
:attr:`~jasper.correction.spatial.SpatialMatrix.std_db` rather than stacking
the position curves a second time. Nor does it own the strategy's base depth or
its minimum meaningful filter (:mod:`jasper.correction.strategy` hands both
in), how filters are fitted (:mod:`jasper.correction.peq`), or any *band* — the
caller hands in the bins it will actually design over, and that mask scopes the
disclosure counts only; it never shapes the curve.

**Units.** Frequencies in hertz. Sigma and every depth in decibels. Allowed
depth is **non-positive**, in the same sign convention as
:attr:`~jasper.correction.strategy.CorrectionStrategy.max_cut_db`: ``-10.0``
means "a filter here may cut up to 10 dB", and ``0.0`` means "no cut may be
designed here at all". ``max_depth_forgone_db`` is the opposite sign — a
non-negative *reduction* in allowed depth.

Why this exists (issue #1954)
-----------------------------
The room layer designs its correction on the spatial **mean** across seats and
then only *labels* the spread afterwards: ``strategy._filter_audit`` annotates
each already-chosen filter with a ``spatial_confidence`` block. The position
argument in the Dirac Research room-correction paper (owner-supplied digest,
``captures/paper-digest-20260731.md``, 2026-07-31) is that a correction good
for the mean is not good at any measured position, and that inverting a feature
which moves with the microphone manufactures ripple whose "correction" is
itself audible. **Invert only what is stable across positions.**

This module turns that label into a constraint.

The curve
---------
::

    fraction(sigma)  = min(1, tolerable / max(sigma, eps))
    allowed_depth_db = base_max_cut_db * fraction(sigma)

Saturating below ``tolerable`` and tapering as ``1/sigma`` above it. Three
choices make it, and each is inherited rather than invented:

**The mapping shape** is the sibling layer's single sigma-to-depth mapping,
:func:`jasper.active_speaker.linearization_envelope._sigma_to_depth_db`, which
is the shape the digest's recommendation A points at. Its direction is that
module's design-doc rule: *a literal ``allowed_depth`` proportional to sigma is
backwards — a noisier measurement must never justify deeper correction.* The
taper is smooth and never reaches zero, which also means an ordinary spread can
never clamp the designer's first filter to zero gain (see
:func:`no_correction_mask` for why that would matter).

**The tolerance** is :data:`~jasper.correction.spatial.MEDIUM_CONFIDENCE_STD_DB`
— the room layer's own already-shipped repeatability vocabulary, not a new
number. Deliberately the *generous* end of that band (6 dB) rather than the
tight end (:data:`~jasper.correction.spatial.HIGH_CONFIDENCE_STD_DB`, 4 dB),
for the reason :class:`jasper.correction.acceptance.AcceptanceThresholds`
already states about the other automatic action this system takes without
asking: per-frequency seat-to-seat spread of 4-6 dB is *normal* in this repo's
own measurements, so a trigger seeded from the tight end would fire on ordinary
rooms. Every frequency the product itself would call high **or** medium
repeatability keeps the full depth its strategy allows; only the frequencies it
calls **low** lose any, and they lose it gradually.

**The statistic** is the raw per-frequency cross-position sigma, and this is
where the room layer parts company with the speaker layer, on purpose. The
digest names ``sigma/sqrt(N)`` — the standard error of the mean — as the
precedent, because dispersing a measurement cloud is the *speaker* protocol's
own instruction and a term that bites harder the better a household follows an
instruction is backwards. That reasoning does not carry here. The room layer's
positions are **listening positions** ("Move the microphone to position N of
M"), so disagreement between them is a property of the room where people
actually sit, not an artefact of a protocol asking for dispersion. And the
speaker layer does not in fact put a *structural* spread through that division:
:func:`~jasper.active_speaker.linearization_envelope.position_stability_limit`
consumes ``BandSpread.sigma_db``, the deliberately comb-insensitive band LEVEL
spread, and explicitly refuses ``max_sigma_db``, the per-bin structure spread,
because dedicated interference instruments own that evidence there. The room
layer has no such instrument, and
:attr:`~jasper.correction.spatial.SpatialMatrix.std_db` IS a per-bin structure
spread — so it is the analogue of what the speaker layer sends to
``spatial_exclusion_limit``, not of what it sends through ``sqrt(N)``.

**What would overturn that call:** evidence that the room flow's guided cloud
is dispersion-like rather than seat-like — several captures around one seat
rather than several places people sit. Then the ``sqrt(N)`` softening is the
right shape and this tolerance is too tight. That is a measurement question
about the flow, not a maths question, and it is not settled here.

The estimator this is calibrated against
----------------------------------------
:attr:`~jasper.correction.spatial.SpatialMatrix.std_db` is a **population**
standard deviation (``np.std``, ``ddof=0``) over the completed positions — the
same array the confidence thresholds were chosen against.
(:mod:`jasper.audio_measurement.spatial_combine`, the speaker layer's combiner,
uses ``ddof=1`` and publishes only band-level scalars; different lane, different
estimator, and this tolerance does not transfer to it.) Two positions is the
fewest this module will act on, the same floor
:func:`~jasper.correction.spatial.band_summary` and
:func:`~jasper.correction.spatial.point_summary` already refuse below.

What this deliberately does NOT cap
-----------------------------------
**Boosts.** :func:`jasper.correction.peq.design_peq` accepts a per-frequency
array for ``max_cut_db`` only; its ``max_boost_db`` is a scalar, and widening
that is a change to an engine the speaker layer also fits against. So a boost
at an unstable frequency is still bounded only by the strategy's own
per-filter and total-boost caps — this cap does not touch it. What limits the
exposure is that no strategy on the household surface boosts at all
(``HOUSEHOLD_CORRECTION_STRATEGY_IDS`` is cuts-only), that the only
boost-capable strategy caps its total at 3 dB, and that a dip is the safer
half of the paper's argument anyway: filling a position-dependent null is
already refused outright by ``cuts_only``. Closing the gap for ``assertive``
means giving the engine a per-frequency boost cap, and that is a change to the
shared engine, not to this module.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from . import spatial


#: The cross-position sigma at or below which a frequency keeps the whole depth
#: its strategy allows, dB. An **alias**, not a second number: it is the room
#: layer's own "medium repeatability" threshold, chosen over the tighter "high"
#: one for the reason the module docstring gives. Retuning the confidence
#: vocabulary retunes this with it, which is the point.
TOLERABLE_STD_DB: float = spatial.MEDIUM_CONFIDENCE_STD_DB

#: Guards the division for a bin whose positions agree exactly (sigma 0).
_SIGMA_EPSILON_DB: float = 1e-6


def depth_fraction(std_db: np.ndarray | float) -> np.ndarray:
    """The fraction of a strategy's cut depth this spread supports, in [0, 1].

    ``min(1, TOLERABLE_STD_DB / max(sigma, eps))`` — pure, elementwise, and
    band-free. See the module docstring for where the shape and the tolerance
    come from. Returns exactly ``1.0`` at or below the tolerance, so an
    ordinary room's allowed depth is bit-identical to the strategy's own
    scalar and its design cannot move.
    """
    sigma = np.asarray(std_db, dtype=np.float64)
    return np.minimum(
        1.0, TOLERABLE_STD_DB / np.maximum(sigma, _SIGMA_EPSILON_DB),
    )


def allowed_depth_db(
    std_db: np.ndarray | float,
    *,
    base_max_cut_db: float,
) -> np.ndarray:
    """Per-frequency cut floor, dB, non-positive, on ``std_db``'s own grid.

    ``base_max_cut_db`` is the strategy's scalar floor (negative). The result is
    the array :func:`jasper.correction.peq.design_peq` accepts as
    ``max_cut_db``.
    """
    return base_max_cut_db * depth_fraction(std_db)


def no_correction_mask(
    depth_db: np.ndarray,
    *,
    min_filter_gain_db: float,
) -> np.ndarray:
    """Bins whose allowance cannot produce a filter the designer would keep.

    A bin qualifies when its allowed depth is shallower than the strategy's
    smallest worthwhile filter.

    **Why the caller must act on this and not merely pass the cap array.**
    :func:`jasper.correction.peq.design_peq` ``break``s out of its greedy loop
    when the tallest remaining peak clamps to a gain below
    ``min_filter_gain_db``; it does not skip that peak and carry on. So a
    single un-correctable frequency, if it happens to hold the tallest
    residual, would end the design and forfeit every *stable* peak underneath
    it — the opposite of what capping depth is for. The room layer therefore
    removes these bins from the designer's view of the residual rather than
    offering an allowance the engine will stop on. (Changing that ``break`` is
    not an option from here: the speaker layer fits against the same function.)

    **This is the tail, not the common path.** :func:`depth_fraction` tapers
    smoothly and never reaches zero, so under the shipped tolerance a bin only
    qualifies at a spread no real room produces — for ``balanced`` (-10 dB
    floor, 0.5 dB minimum filter) a sigma above ~120 dB. It stays because the
    engine's stopping rule is real, the mapping's tolerance is a tunable, and a
    future retune must not silently reintroduce the forfeit; a wrecked capture
    (one position at the noise floor, another at full level) can reach it too.
    """
    return np.asarray(depth_db, dtype=np.float64) > -abs(min_filter_gain_db)


@dataclass(frozen=True)
class VarianceCapDisclosure:
    """What the cross-position depth cap did to one design's **ceiling**.

    The disclosure half of this module's contract, and deliberately a claim
    about the *ceiling the designer worked under* — not about the filters that
    came out. ``n_bins_capped`` says how many in-band frequencies were allowed
    less depth than the strategy alone would have allowed; it does **not** say
    a filter was removed at any of them, because whether the designer would
    have placed one there is not knowable from this record. Read it beside the
    design report's ``filters`` list, which is where the shipped filters are.

    Policy fields (``tolerable_std_db``, ``base_max_cut_db``)
    are always populated — they are inputs, not measurements, and stating them
    when the cap could not run costs nothing and tells a reader what *would*
    have applied. Measurement fields are ``None`` when nothing was measured,
    never defaulted to a neutral-looking zero: ``max_depth_forgone_db`` of
    ``0.0`` means "measured, and the cap took nothing", while ``None`` means
    "no cap ran". ``available`` and ``reason`` are the discriminator.

    Args:
      available: whether a cap was computed and handed to the designer.
      reason: why not, when it was not. One of the vocabulary
        :mod:`jasper.correction.spatial` already uses for the same refusals,
        so a reader meets one phrasing across the spread surfaces.
      position_count: completed positions the spread was taken across; 0 when
        no usable spatial matrix existed.
      tolerable_std_db: the cross-position sigma at or below which a
        frequency keeps its whole allowance, dB — the curve's saturation
        point.
      base_max_cut_db: the strategy's own scalar cut floor, dB (non-positive),
        which is what the cap reduces *from*.
      n_bins: in-band grid bins the cap was evaluated over.
      n_bins_capped: of those, how many were allowed less depth than
        ``base_max_cut_db``.
      n_bins_no_cut: of those, how many were allowed no usable correction at
        all and were therefore withheld from the designer's search (see
        :func:`no_correction_mask`).
      max_depth_forgone_db: the largest single-bin **reduction** in allowed
        depth, dB, non-negative.
      worst_freq_hz: where that largest reduction sits; ``None`` when no bin
        was capped.
      filters_depth_trimmed: how many designed filters the room layer then had
        to shallow or drop because their *cumulative* cut at one centre
        frequency would have exceeded the allowance there. Set by the caller
        that performs that enforcement (``strategy._enforce_variance_depth_cap``)
        — a per-filter cap cannot bound a stack, exactly as on the boost side.
    """

    available: bool
    reason: str | None
    position_count: int
    tolerable_std_db: float
    base_max_cut_db: float
    n_bins: int = 0
    n_bins_capped: int = 0
    n_bins_no_cut: int = 0
    max_depth_forgone_db: float | None = None
    worst_freq_hz: float | None = None
    filters_depth_trimmed: int = 0

    @property
    def active(self) -> bool:
        """Whether the cap actually reduced the depth allowed anywhere in band.

        The room layer gates on this before handing the array to the designer,
        so a run in which nothing was capped takes the identical code path it
        took before this cap existed — the honest-neutrality guarantee, made
        structural rather than argued from floating-point reasoning.
        """
        return self.available and self.n_bins_capped > 0

    def with_trimmed(self, filters_depth_trimmed: int) -> VarianceCapDisclosure:
        """A copy carrying the caller's cumulative-trim count.

        ``replace`` rather than a field-by-field rebuild, so a field added to
        this record cannot be silently dropped on the way through.
        """
        return replace(self, filters_depth_trimmed=filters_depth_trimmed)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe disclosure record."""
        return {
            "available": self.available,
            "reason": self.reason,
            "position_count": self.position_count,
            "tolerable_std_db": self.tolerable_std_db,
            "base_max_cut_db": self.base_max_cut_db,
            "n_bins": self.n_bins,
            "n_bins_capped": self.n_bins_capped,
            "n_bins_no_cut": self.n_bins_no_cut,
            "max_depth_forgone_db": self.max_depth_forgone_db,
            "worst_freq_hz": self.worst_freq_hz,
            "filters_depth_trimmed": self.filters_depth_trimmed,
        }


def _unavailable(
    reason: str,
    *,
    position_count: int,
    base_max_cut_db: float,
) -> VarianceCapDisclosure:
    return VarianceCapDisclosure(
        available=False,
        reason=reason,
        position_count=position_count,
        tolerable_std_db=TOLERABLE_STD_DB,
        base_max_cut_db=base_max_cut_db,
    )


def plan_depth_cap(
    matrix: spatial.SpatialMatrix | None,
    *,
    base_max_cut_db: float,
    min_filter_gain_db: float,
    band_mask: np.ndarray,
    unavailable_reason: str | None = None,
) -> tuple[np.ndarray | None, VarianceCapDisclosure]:
    """Plan one design run's per-frequency cut ceiling from cross-position spread.

    Returns ``(allowed_depth_db, disclosure)`` — the array to hand
    :func:`jasper.correction.peq.design_peq` as ``max_cut_db``, or ``None``
    when no cap could be planned, in which case the disclosure says why. The
    two-value shape mirrors :func:`jasper.correction.spatial.build_spatial_matrix`,
    which is where ``matrix`` and ``unavailable_reason`` come from.

    Refusing is never an error and never raises: a single-position session, or
    a session whose position curves could not be stacked, simply designs the
    way it always did and says so. Only a caller *bug* is loud.

    Args:
      matrix: the room layer's stacked per-position curves, or ``None`` when
        they could not be built.
      base_max_cut_db: the strategy's scalar cut floor, dB. Must be deeper than
        ``min_filter_gain_db``; a strategy whose entire cut budget is already
        shallower than its own smallest worthwhile filter has nothing for this
        cap to shape, and scaling it down would mark *every* frequency
        undesignable — silently suppressing the whole correction rather than
        leaving it alone. Refused instead.
      min_filter_gain_db: the strategy's smallest worthwhile filter gain, dB.
        Bounds the guard above, and counts ``n_bins_no_cut`` on the same
        definition :func:`no_correction_mask` gives the caller.
      band_mask: boolean over the same grid, true on the bins the caller will
        design over. Scopes the disclosure counts; never shapes the curve.
      unavailable_reason: the reason ``matrix`` is ``None``, passed straight
        through so the refusal keeps its original wording.

    Raises:
      ValueError: if ``band_mask`` does not match the matrix's grid. That is a
        caller bug, not a measurement outcome.
    """
    if matrix is None:
        return None, _unavailable(
            unavailable_reason or spatial.TOO_FEW_POSITIONS_REASON,
            position_count=0,
            base_max_cut_db=base_max_cut_db,
        )

    band = np.asarray(band_mask, dtype=bool)
    if band.shape != matrix.std_db.shape:
        raise ValueError(
            f"band_mask shape {band.shape} does not match the spatial "
            f"matrix grid {matrix.std_db.shape}"
        )

    positions = matrix.position_count
    if positions < spatial.MIN_POSITIONS_FOR_SPREAD:
        return None, _unavailable(
            spatial.TOO_FEW_POSITIONS_REASON,
            position_count=positions,
            base_max_cut_db=base_max_cut_db,
        )
    if base_max_cut_db > -abs(min_filter_gain_db):
        return None, _unavailable(
            "strategy designs no cuts",
            position_count=positions,
            base_max_cut_db=base_max_cut_db,
        )
    n_bins = int(band.sum())
    if n_bins == 0:
        return None, _unavailable(
            "no points in band",
            position_count=positions,
            base_max_cut_db=base_max_cut_db,
        )

    depth_db = allowed_depth_db(matrix.std_db, base_max_cut_db=base_max_cut_db)
    # Reduction in allowed depth, non-negative, on the same grid.
    forgone_db = abs(base_max_cut_db) - np.abs(depth_db)
    band_forgone = forgone_db[band]
    # `depth_fraction` returns exactly 1.0 at or below the stable knee, so an
    # untouched bin's reduction is exactly 0.0 — no epsilon is needed to tell
    # "capped" from "not capped".
    capped = band_forgone > 0.0
    n_capped = int(capped.sum())
    no_cut = no_correction_mask(depth_db, min_filter_gain_db=min_filter_gain_db)
    worst_freq_hz: float | None = None
    if n_capped:
        band_freqs = matrix.freqs_hz[band]
        worst_freq_hz = float(band_freqs[int(np.argmax(band_forgone))])

    return depth_db, VarianceCapDisclosure(
        available=True,
        reason=None,
        position_count=positions,
        tolerable_std_db=TOLERABLE_STD_DB,
        base_max_cut_db=base_max_cut_db,
        n_bins=n_bins,
        n_bins_capped=n_capped,
        n_bins_no_cut=int((no_cut & band).sum()),
        max_depth_forgone_db=float(np.max(band_forgone)),
        worst_freq_hz=worst_freq_hz,
    )
