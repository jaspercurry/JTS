# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Delta-probe verification: did the speaker do what the correction asked?

Pure computation — a realized-vs-commanded per-frequency map classified into
one of the verdicts below; the session owns I/O, state and rollback.
``commanded_delta_db`` is what the apply asks the summed response to CHANGE
(the applied graph's predicted sum minus the graph it replaces, same branches
and summation model); ``realized_delta_db`` is the measured post-apply
response minus that same previous-graph prediction. Their difference is
algebraically ``measured_post − predicted_post``, which is NOT
level-offset-invariant — hence ``expected_offset_db`` and the quiet-bin frame
— and which cancels the command, so it grades the acoustic MODEL. A
directional SAFETY finding is a statement about the speaker and therefore
needs ``entry_delta_db`` on top; without it the finding is not made.
:mod:`jasper.active_speaker.bench` is the offline twin and reuses this
vocabulary. See docs/measurement-loop-doctrine.md §3 and ADR-0209.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.frame_fit import FRAME_UNFITTED, FrameFit, fit_frame

# --------------------------------------------------------------------------- #
# verdict vocabulary
# --------------------------------------------------------------------------- #

#: The correction realized what it commanded. Keep it.
VERDICT_MATCHED = "matched"
#: Realized and commanded disagree in SHAPE — the emitted filters are not doing
#: what the fit's model of them says they do. Roll back and flag.
VERDICT_MODEL_ERROR = "model_error"
#: Realized tracks commanded in shape but falls materially short in scale where
#: what was commanded is a lift. A compression diagnostic. Roll back and flag.
VERDICT_LEVEL_DEPENDENT_SHORTFALL = "level_dependent_shortfall"
#: The map matched at the mark, but the cross-position spread WIDENED — the
#: signature of correcting a position-specific interference feature. Roll back
#: and route the household to a placement-vs-speaker service verdict.
VERDICT_SPATIALLY_COSTLY = "spatially_costly"
#: The map fails ONLY because of a level shift that survives removing the offset
#: the emitter knows it applied — measured where the correction commanded
#: nothing, and sufficient on its own to explain the failure. **Not a claim
#: about the correction**, which is why it is not in
#: :data:`DELTA_PROBE_ROLLBACK_VERDICTS`; it is named rather than absorbed so
#: the journal says what is worth looking for.
VERDICT_LEVEL_MISMATCH = "level_mismatch"
#: The map fails ONLY because of the FRAME between the two curves — one level
#: offset and one broadband tilt, fitted where the correction commanded nothing
#: and therefore uncommanded by construction (#2521). It replaces whichever
#: rollback the map would otherwise have reached, because neither a SHAPE nor a
#: SCALE claim survives evidence that turns out to be the frame.
#:
#: The tilt-carrying sibling of :data:`VERDICT_LEVEL_MISMATCH`, and not a
#: rollback verdict for the same reason: a frame difference is a property of the
#: COMPARISON, not a claim about the correction's shape, and this module
#: measures the frame without attributing it. It grants no permission either —
#: the shape question is left unanswered and it says so.
VERDICT_FRAME_MISMATCH = "frame_mismatch"
#: No verdict is available — the correction commands nothing inside the probe
#: band, or the curves could not be compared. **Not a pass**: no evidence to
#: refuse on, and no permission granted either.
VERDICT_UNAVAILABLE = "unavailable"
#: This map carries the MODEL's departure and no grade of anything else (#2614).
#: The caller had the applied graph's own declared transfer but no CHANGE axis
#: — the reachable case is a previous graph that cannot be named — so neither
#: "did the correction realize the shape it commanded" nor "did the speaker put
#: more energy into a driver than the graph declared" is answerable.
#:
#: **Not a pass, and deliberately not a rollback.** Both directional findings
#: are absences, so :attr:`DeltaProbeMap.safety_anchored` is False and neither
#: reaches ``evaluate_applied_safety`` as a hazard. Its own word rather than
#: ``matched`` (the shape check did not RUN) and rather than ``unavailable``
#: (something WAS measured).
VERDICT_SAFETY_ONLY = "safety_only"

#: Why the shape half did not run on a :data:`VERDICT_SAFETY_ONLY` map. One
#: string today; it is a constant so the journal, the receipt and the household
#: caveat all quote the same one.
REASON_COMMANDED_AXIS_UNAVAILABLE = "commanded_axis_unavailable"

#: Every verdict this module can return. Pinned by a test so a new
#: classification path cannot ship an un-enumerated string.
DELTA_PROBE_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MATCHED,
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_FRAME_MISMATCH,
    VERDICT_UNAVAILABLE,
    VERDICT_SAFETY_ONLY,
})

#: The verdicts on which rollback is AUTOMATIC. ``unavailable`` is deliberately
#: NOT here — an absent measurement is not evidence of a bad correction.
#:
#: :data:`VERDICT_LEVEL_MISMATCH` and :data:`VERDICT_FRAME_MISMATCH` are absent
#: for the same reason stated with one more term: they are findings about the
#: LEVEL (and tilt) AXIS of this comparison, not about the correction's shape.
#: The known production cause of the first is an incompleteness in our own
#: accounting — the applied crossover config is emitted without room-PEQ /
#: preference EQ — and reverting a household's correction because our
#: bookkeeping was short would be a false accusation.
DELTA_PROBE_ROLLBACK_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY,
})

#: The one reason the seam hands a rollback verdict to the adoption table
#: instead of restoring on it (#2559). A stable string, because it rides the
#: journal and the round receipt: an immediate restore that did not happen must
#: be as legible as one that did.
#:
#: Named for the DIRECTION rather than for one verdict — see ADR-0209.
SEAM_DEFERRED_QUIETER_THAN_COMMANDED = "realized_quieter_than_commanded"

#: The rollback classes whose whole claim is realized-vs-commanded, and which
#: therefore defer when the deviation points entirely quieter (ADR-0209).
#: :data:`VERDICT_SPATIALLY_COSTLY` is deliberately absent: it differences two
#: MEASUREMENTS with no model between them, and
#: docs/measurement-loop-doctrine.md §3 restores ON those.
DELTA_PROBE_REALIZED_VS_COMMANDED_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
})

#: What the band ratios grade realized against: the COMMANDED delta — a change
#: claim. The composed verdict's sibling on the same receipt grades against
#: ``verification.REALIZATION_COMPARAND`` (the applied candidate's predicted
#: sum, an absolute claim); two graders of one axis, each naming its comparand.
REALIZED_VS_COMMANDED_COMPARAND = "commanded_delta"


def seam_rollback_deferral(probe: Any | None) -> str:
    """Why this map's seam-bound rollback DEFERS to the adoption table, or ``""``.

    ADR-0209 holds the ruling: a realized-vs-commanded miss whose deviation
    points entirely QUIETER than commanded is a quality miss that keeps for
    iteration, not a hazard that comes off the speaker. The narrowing has to
    happen HERE because a seam rollback ends the session before
    ``decide_adoption`` runs at all; deferring is what makes the table
    reachable and decides nothing about the outcome.

    It narrows only :data:`DELTA_PROBE_REALIZED_VS_COMMANDED_VERDICTS`. Every
    positive-direction finding is unchanged: a map with any safety bin measured
    louder than this apply declared never defers, and ``boost_over_declared_bound``
    never defers — implied by the bin rule, and stated and pinned anyway,
    because a fence that holds only through an implication between two
    independently-tunable bounds is not a fence.

    Both fences read the ANCHORED excess and fall back to
    :attr:`DeltaProbeMap.model_departure_over_tolerance` rather than to nothing:
    "no anchor, no finding" is right for a FINDING, and inverts for a FENCE
    whose default is generous. An unanchored map that reported nothing louder
    has established nothing, and naming that ``realized_quieter_than_commanded``
    would put a false sentence on a hearing-safety record.

    ``""`` for an absent probe and for a non-rollback verdict: those never
    reached a seam rollback. Duck-typed on the probe so one function answers for
    both the seam that acts on it and the receipt that discloses it.
    """
    if probe is None:
        return ""
    if (
        str(getattr(probe, "verdict", "") or "")
        not in DELTA_PROBE_REALIZED_VS_COMMANDED_VERDICTS
    ):
        return ""
    if bool(getattr(probe, "realized_louder_than_commanded", False)):
        return ""
    if bool(getattr(probe, "boost_over_declared_bound", False)):
        return ""
    if not bool(getattr(probe, "safety_anchored", False)) and bool(
        getattr(probe, "model_departure_over_tolerance", False)
    ):
        return ""
    return SEAM_DEFERRED_QUIETER_THAN_COMMANDED

# --------------------------------------------------------------------------- #
# classification thresholds
# --------------------------------------------------------------------------- #

# Max |realized − commanded| tolerated below :data:`DELTA_PROBE_HF_SPLIT_HZ`.
#
# 1.5 dB, for two reasons that agree. (a) It is ``crossover_v2_flow.
# VERIFY_TOLERANCE_DB``, so this probe and the tracking check hold the same
# chain to one standard. (b) It sits below the defect it exists to catch: the
# 2026-07-27 shelf-Q realization error peaked at 1.70 dB. That 0.2 dB margin is
# why the exceedance-WIDTH rule matters — the error is a wide systematic tilt,
# so it clears a width test comfortably where it barely clears an amplitude one.
DELTA_PROBE_TOLERANCE_LOW_DB: float = 1.5

# Max |realized − commanded| tolerated at/above :data:`DELTA_PROBE_HF_SPLIT_HZ`.
#
# Measurement uncertainty grows with frequency and a rollback fabricated by HF
# noise is worse than no probe at all. ``linearization_fit.
# HF_AGREEMENT_LIMIT_HIGH_DB`` accepts up to 2.0 dB of spread between repeat
# sweeps of the same driver up here, and the owner's per-serial UMIK-2 research
# puts the stock-cal protocol at ~±2.3 dB @16 kHz; 2.5 clears both, and the
# width rule still has to be satisfied on top.
DELTA_PROBE_TOLERANCE_HIGH_DB: float = 2.5

# Where the low tier ends and the high tier begins. Mirrors
# ``linearization_fit._HF_AGREEMENT_TIER_SPLIT_HZ`` so "high frequencies" means
# one thing across the fit and its verification.
DELTA_PROBE_HF_SPLIT_HZ: float = 10_000.0

# A tolerance exceedance must span at least this many contiguous octaves to
# count as a finding.
#
# The measured curves are ladder-smoothed at 1/6 octave below 4 kHz and 1/3
# above, so an excursion narrower than one smoothing window is measurement
# texture, not a claim about the model. One third of an octave is the coarsest
# of those windows. Every realization defect this probe is built for — a mis-Q'd
# shelf, a mis-modelled slope, a compressed driver — is broad by construction.
DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES: float = 1.0 / 3.0

# Below this, the correction commands nothing worth verifying at that bin.
# Mirrors ``linearization_fit._MIN_FILTER_GAIN_DB``.
#
# This is also THE QUIET FLOOR: a bin under it is where the correction asked for
# nothing, which is what makes ``residual_offset_db`` and the fitted frame
# measurable there at all. Below the HF split it is the graded floor too; above
# it the stricter :data:`DELTA_PROBE_MIN_COMMANDED_HIGH_DB` applies, and the two
# roles are deliberately not merged.
DELTA_PROBE_MIN_COMMANDED_DB: float = 0.5

# The commanded floor a bin must clear to be GRADED at or above
# :data:`DELTA_PROBE_HF_SPLIT_HZ` (#2521).
#
# Numerically equal to :data:`DELTA_PROBE_TOLERANCE_HIGH_DB` and defined
# separately on purpose — the same measurement-uncertainty bar asked of a
# different quantity (what the correction ASKED for at a bin, versus how far its
# realization may miss). A bin whose commanded value is smaller than the
# concession the tolerance already grants cannot answer "did the speaker do what
# we asked": the first remote JTS3 session's headline was such a bin, |commanded|
# grazing 0.5 dB at 21.3 kHz and reporting 23.4 dB of "error".
#
# NOT lifted below the split, which is the tempting symmetric move: on this
# module's keystone fixture a flat 1.0 dB floor drops the 2026-07-27 shelf-Q
# defect's exceedance run from 0.575 to 0.307 octaves — under
# :data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES` — deleting the one defect this
# module exists to catch.
DELTA_PROBE_MIN_COMMANDED_HIGH_DB: float = 2.5

# The probe band must retain at least this many bins after masking, or there
# is not enough of a curve to regress or to measure a run width against.
DELTA_PROBE_MIN_BINS: int = 8

# Best-fit realized/commanded scale factor below which a shape-tracking map is
# called a level-dependent SHORTFALL rather than a model error.
#
# 0.85 agrees with :data:`DELTA_PROBE_TOLERANCE_LOW_DB` about what "material"
# means at the depths this fit produces: a 15 % shortfall on the ~10 dB lift a
# CD-horn continuation commands is 1.5 dB, exactly the low-band tolerance.
DELTA_PROBE_SHORTFALL_GAIN_CEILING: float = 0.85

#: The band ids a realization ratio is reported per (#2649).
#:
#: Per band rather than ONE least-squares slope over the whole graded band,
#: because that scalar can be manufactured by a defect confined to a band the
#: system is not even allowed to command in: on the 2026-08-16 shortfall round
#: the pooled slope read 0.664 while the trusted HF realized 96-101 %.
#:
#: ``crossover`` is the graded tier BELOW :data:`DELTA_PROBE_HF_SPLIT_HZ`, named
#: for what it contains rather than derived from any Fc, which this function is
#: not told and must not guess. ``trusted_hf`` is the graded tier at or above the
#: split. ``above_ceiling`` is reported and NEVER graded: the span inside the
#: requested band that the mic-trust ceiling excluded.
DELTA_PROBE_BAND_CROSSOVER = "crossover"
DELTA_PROBE_BAND_TRUSTED_HF = "trusted_hf"
DELTA_PROBE_BAND_ABOVE_CEILING = "above_ceiling"

#: Every band id a realization block can carry, in report order.
DELTA_PROBE_REALIZATION_BANDS: tuple[str, ...] = (
    DELTA_PROBE_BAND_CROSSOVER,
    DELTA_PROBE_BAND_TRUSTED_HF,
    DELTA_PROBE_BAND_ABOVE_CEILING,
)

# Widening of the across-position level spread (``BandSpread.sigma_db``, dB)
# beyond which the post-apply cloud is called spatially costly.
#
# ``linearization_envelope.position_stability_limit`` spends this spread as
# ``sigma_db / sqrt(n_positions)``, so 1.0 dB of RAW sigma growth is already
# several times the depth the cloud terms would have licensed in that band.
DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB: float = 1.0

# |residual common mode| beyond which an otherwise-unexplained whole-band level
# shift is named :data:`VERDICT_LEVEL_MISMATCH` rather than left inside the
# shape verdict (#1811).
#
# Numerically equal to :data:`DELTA_PROBE_TOLERANCE_LOW_DB` and defined
# separately on purpose — the same "material disagreement" bar asked of a
# different quantity (one constant over the band, versus a per-bin excursion).
# A common mode smaller than the per-bin tolerance cannot by itself have pushed
# a bin past it, so a smaller bar here would name a shift that explains nothing.
# A magnitude bar, NOT the discriminator — see :func:`classify_delta_probe`.
DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB: float = 1.5

# How spread the quiet evidence must be, relative to a FULL sampling of the band
# its level is claimed over, before that claim may be made band-wide (#2533).
#
# The quantity is :attr:`DeltaProbeMap.quiet_probe_coverage`: the interquartile
# octave span of the quiet bins divided by the SAME statistic over every
# graded-band bin on the same grid. Not divided by the band's whole span — that
# ratio is a property of the GRID, not of the evidence: production grids are
# linear (``rfftfreq``), so any interquartile span is pulled toward the top
# octaves, and a perfectly uniform quiet set scored 0.303 against the whole span
# on the retained 2026-08-15 grid. Same statistic over the same grid top and
# bottom means **1.0 is "spread exactly like a full sampling of the graded
# band", grid invariant** (pinned on both a linear and a log grid).
#
# **0.5 is a judgment, not a derivation.** What is derived is the 1.0 reference.
# It sits in a wide measured gap — the tightest passing shape scores 0.870 and
# the shape this exists to catch scores 0.248 — so any bar between roughly 0.3
# and 0.8 makes the same calls, which is why both numbers are disclosed on every
# map rather than reduced away to a pass/fail.
DELTA_PROBE_MIN_QUIET_COVERAGE: float = 0.5

#: ``reason`` for a level shift the quiet bins measured across the WHOLE graded
#: band — the ordinary case, and the only one supporting a whole-band claim.
REASON_UNCOMMANDED_LEVEL_SHIFT = "uncommanded_level_shift"
#: ``reason`` for the same finding when the quiet bins are less spread than
#: :data:`DELTA_PROBE_MIN_QUIET_COVERAGE` of the graded band (#2533). The finding
#: stands and the verdict is unchanged; what narrows is the BAND the claim names.
REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND = (
    "uncommanded_level_shift_outside_probe_band"
)

#: Appended to a non-matched verdict's ``reason`` when there were too few quiet
#: bins to measure ``residual_offset_db`` at all (#1811). The verdict is the
#: honest one for the evidence available, but it was reached WITHOUT the level
#: discriminator, and a rollback decided that way should say so.
_LEVEL_CHECK_UNAVAILABLE_SUFFIX = "|level_check_unavailable"


# --------------------------------------------------------------------------- #
# spatial arm
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpatialCost:
    """Did the correction make the room LESS even? (the spatial arm)

    Built from the two position groups the flow already walks — pre-apply and
    post-apply — so unlike the at-the-mark arm this one is
    measurement-minus-measurement. ``available`` is False when either group
    carries fewer than two positions.
    """

    available: bool
    widened: bool
    worst_center_hz: float
    worst_widening_db: float
    tolerance_db: float
    n_bands: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "widened": self.widened,
            "worst_center_hz": self.worst_center_hz,
            "worst_widening_db": self.worst_widening_db,
            "tolerance_db": self.tolerance_db,
            "n_bands": self.n_bands,
        }


SPATIAL_COST_UNAVAILABLE = SpatialCost(
    available=False, widened=False, worst_center_hz=0.0,
    worst_widening_db=0.0, tolerance_db=DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
    n_bands=0,
)


def evaluate_spatial_cost(
    before: Sequence[Any],
    after: Sequence[Any],
    *,
    tolerance_db: float = DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
) -> SpatialCost:
    """Compare two clouds' per-octave level spread, before vs after the apply.

    ``before``/``after`` are ``spatial_combine.BandSpread`` sequences,
    duck-typed on ``center_hz``/``sigma_db``. Bands are paired by ``center_hz``;
    a band present in only one group is skipped.

    ``sigma_db`` — the spread of each position's BAND LEVEL — not
    ``max_sigma_db``, which rides comb nulls that move with the microphone
    whether or not a correction was applied.
    """
    before_by_center = {
        round(float(b.center_hz), 3): float(b.sigma_db)
        for b in before
        if math.isfinite(float(b.sigma_db))
    }
    worst_center_hz = 0.0
    worst_widening_db = -math.inf
    n_bands = 0
    for band in after:
        sigma_after = float(band.sigma_db)
        if not math.isfinite(sigma_after):
            continue
        key = round(float(band.center_hz), 3)
        if key not in before_by_center:
            continue
        n_bands += 1
        widening = sigma_after - before_by_center[key]
        if widening > worst_widening_db:
            worst_widening_db = widening
            worst_center_hz = float(band.center_hz)
    if n_bands == 0:
        return SPATIAL_COST_UNAVAILABLE
    return SpatialCost(
        available=True,
        widened=worst_widening_db > tolerance_db,
        worst_center_hz=worst_center_hz,
        worst_widening_db=float(worst_widening_db),
        tolerance_db=float(tolerance_db),
        n_bands=n_bands,
    )


# --------------------------------------------------------------------------- #
# the map
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeltaProbeMap:
    """One applied correction's realized-vs-commanded verdict and evidence.

    ``verdict`` is one of :data:`DELTA_PROBE_VERDICTS`; ``rollback`` is True
    exactly when it is in :data:`DELTA_PROBE_ROLLBACK_VERDICTS`, computed here
    so no caller can decide it differently.

    ``gain_factor`` is the least-squares realized/commanded scale over the probe
    band — 1.0 means full depth. Reported on every classified map, ``None`` on an
    unavailable one: 0.0 there would read as "delivered nothing", which is the
    opposite of "not measured". It carries an INTERCEPT
    (:attr:`gain_intercept_db`) and is measured on the frame-removed curve
    (#2521): through the origin, on a mostly-one-sign commanded curve, a pure
    level offset arrives as apparent scale — a constant −7.8 dB read as 2.02.
    """

    verdict: str
    reason: str
    probe_band_hz: tuple[float, float]
    n_bins: int
    max_error_db: float
    rms_error_db: float
    worst_hz: float
    exceedance_octaves: float
    gain_factor: float | None
    tolerance_low_db: float
    tolerance_high_db: float
    spatial: SpatialCost
    #: The level move the EMITTER told us it made, dB, removed from the realized
    #: curve before anything below was computed (#1811). Every scalar above is
    #: measured AFTER its removal.
    expected_offset_db: float = 0.0
    #: The level CHANGE across the apply that nobody commanded, measured where
    #: the correction commanded NOTHING — the mean of
    #: ``(realized − expected_offset) − commanded − entry_delta`` across the
    #: in-band bins BELOW the commanded floor. The ``− commanded`` term is small
    #: but real there (the floor admits up to
    #: :data:`DELTA_PROBE_MIN_COMMANDED_DB`) and is subtracted rather than
    #: assumed away.
    #:
    #: **A CHANGE, not an absolute** (#2533): the ``− entry_delta`` term cancels
    #: the standing disagreement between an in-room measurement and an on-axis
    #: model, which is not a level move at all. On a CHAINED round it is only
    #: trustworthy if the previous side describes the graph the entry capture
    #: went through — see :func:`classify_delta_probe`.
    #:
    #: ``None`` when there are too few quiet bins to measure it — "not
    #: measured", which 0.0 would misreport as "measured, nothing moved".
    residual_offset_db: float | None = None
    residual_offset_tolerance_db: float = DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB
    #: The band the caller HANDED IN — this capture's trusted band. Distinct from
    #: ``probe_band_hz``, the span of the bins that then cleared the commanded
    #: floor inside it: a reader diagnosing a verdict needs to know whether a bin
    #: was excluded for lack of trust or for lack of a command (#2521).
    requested_band_hz: tuple[float, float] = (0.0, 0.0)
    #: The frame fitted between the two curves over the QUIET bins — one offset,
    #: one tilt, both uncommanded by construction (#2521). ``FRAME_UNFITTED``
    #: when there were too few quiet bins.
    frame: FrameFit = FRAME_UNFITTED
    #: The three graded scalars again, taken after :attr:`frame` was removed.
    #: ``None`` together and only together, and only when no frame was fitted —
    #: never defaulted to their raw twins, which would read as "removing the
    #: frame changed nothing" instead of "nothing was measured". The RAW twins
    #: decide whether there is a finding; these decide whether it is a rollback.
    frame_removed_max_db: float | None = None
    frame_removed_rms_db: float | None = None
    frame_removed_exceedance_octaves: float | None = None
    #: Where the ``gain_factor`` regression crosses zero commanded, dB — the
    #: level term it no longer has to absorb as scale. ``None`` like
    #: ``gain_factor``.
    gain_intercept_db: float | None = None
    #: The STANDING disagreement between the pre-apply measurement and the
    #: two-branch model, dB, over the same quiet bins (#2533). Removed from
    #: :attr:`residual_offset_db` so that field measures a change.
    #:
    #: ``None`` means **not measured** and nothing is removed, so the standing
    #: offset stays visible rather than being pretended away. It discloses what
    #: was REMOVED and cannot disclose what the removal left behind: on a chained
    #: round the entry capture rides a graph whose own correction this module
    #: never sees.
    entry_anchor_offset_db: float | None = None
    #: How many quiet bins :attr:`residual_offset_db` was measured over. Distinct
    #: from ``frame.n_bins``: the frame is fitted over the whole quiet set, while
    #: an anchored residual needs the pre-apply curve to be finite too.
    quiet_n_bins: int = 0
    #: The INTERQUARTILE span of those bins' frequencies, Hz — the middle half,
    #: which is where they actually sit (#2533). ``frame.band_hz`` reports
    #: min/max, and min/max is what two stray bins defeat. ``None`` when no
    #: residual was measured.
    quiet_core_band_hz: tuple[float, float] | None = None
    #: :attr:`quiet_core_band_hz`'s span in octaves divided by the SAME statistic
    #: over every graded-band bin on this grid — how spread the evidence for a
    #: level claim is, relative to a full sampling of the band that claim is made
    #: over. A co-spanning quiet set scores exactly 1.0; below
    #: :data:`DELTA_PROBE_MIN_QUIET_COVERAGE` the verdict keeps its finding and
    #: narrows its reason. ``None`` when nothing was measured.
    quiet_probe_coverage: float | None = None
    #: Were the two directional findings below measured against the PRE-APPLY
    #: capture — are they statements about what the speaker did, or was there
    #: nothing to make them that? (series-2 D1) ``False`` means neither fired
    #: because neither ran. It is on the record because "safe" has two very
    #: different readings and a household surface must not be free to pick either.
    safety_anchored: bool = False
    #: Did a BOOST realize more lift than the applied graph declared,
    #: structurally? (#2537) The adoption table's one delta-probe-sourced hard
    #: stop — see :func:`boost_overshoot`. Asked over the SAFETY bins (this
    #: apply's changes UNION the graph's own declared transfer) since #2614.
    #: ``False`` when nothing overshot, when no safety bin carried a boost, and
    #: when :attr:`safety_anchored` is False; :attr:`boost_overshoot_db` tells
    #: the first from the rest.
    boost_over_declared_bound: bool = False
    #: The worst signed ANCHORED excess, dB, over the safety bins where a boost
    #: was on the table — ``(measured_post − measured_pre) − expected_offset −
    #: commanded``, so positive is energy this apply delivered and did not
    #: declare. ``None`` when no boosted bin was measured, including every
    #: unanchored map — "not measured", never 0.0.
    boost_overshoot_db: float | None = None
    #: The widest contiguous run, in octaves, over which that excess cleared
    #: this probe's per-bin tolerance. ``0.0`` means nothing cleared it.
    boost_overshoot_octaves: float = 0.0
    #: Did ANY safety bin come out LOUDER than this apply declared, past that
    #: bin's own tolerance? (#2559) Measured over every anchored safety bin
    #: rather than only the boosted ones — see :func:`louder_than_commanded`.
    #: ``False`` on an unavailable or unanchored map.
    realized_louder_than_commanded: bool = False
    #: The most POSITIVE ANCHORED excess over the safety bins, dB — the amount
    #: behind the finding above, and a THIRD number rather than a restatement of
    #: :attr:`boost_overshoot_db`, which is taken over the boosted bins only (a
    #: cut-only graph reports ``None`` there while this reports the level the
    #: speaker actually delivered). ``None`` when the finding was not measured.
    realized_excess_db: float | None = None
    #: Did the room depart from the two-branch MODEL, upward, past tolerance,
    #: anywhere in the safety bins? The unanchored reading of the same rule —
    #: ``(measured_post − predicted_post) − expected_offset``. A next-round
    #: target (the blend region is where this model is known blind, #2600), never
    #: a hazard: it fires on rounds where the speaker measured quieter.
    model_departure_over_tolerance: bool = False
    #: The most POSITIVE unanchored ``realized − commanded`` over the safety
    #: bins, dB. Negative on a map whose every bin sat under the model, which is
    #: a measurement. ``None`` when no bin was in the safety mask, never 0.0.
    max_signed_error_db: float | None = None
    #: The frequency :attr:`max_signed_error_db` was measured at, Hz. **Not**
    #: :attr:`worst_hz` — that is the worst ABSOLUTE error over the GRADED bins
    #: and this the worst POSITIVE departure over the SAFETY bins; they are
    #: 563 Hz apart on the banked series-2 r1b. ``None`` whenever the amount is.
    max_signed_error_hz: float | None = None
    #: How much of what was commanded arrived, PER BAND (#2649), keyed by
    #: :data:`DELTA_PROBE_REALIZATION_BANDS`. Each entry is
    #: ``{band_hz, n_bins, ratio, graded}``; ``ratio`` is ``None`` for a band too
    #: thin to fit a slope through, and ``graded`` is False for the above-ceiling
    #: band, which never enters a verdict. Empty on every map that never reached
    #: the fit.
    band_realization: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    #: The mic-trust ceiling the caller applied, Hz — ``None`` when the caller
    #: supplied none and the graded band is the requested one.
    trust_ceiling_hz: float | None = None
    #: The band actually GRADED: the requested band intersected with the ceiling
    #: above. Reported beside ``requested_band_hz`` so "what the caller asked
    #: for" and "what was judged" stay two readable facts.
    graded_band_hz: tuple[float, float] | None = None

    @property
    def trusted_floor_hz(self) -> float | None:
        """The graded band's lower edge — the gate's trusted floor.

        Named because the round receipt banks this number beside its objectives
        (#2609 SF5), so a later round can refuse a cross-floor comparison
        instead of consuming a gate-length change as movement.
        """
        band = self.graded_band_hz or self.requested_band_hz
        return None if band is None else float(band[0])

    @property
    def matched(self) -> bool:
        return self.verdict == VERDICT_MATCHED

    @property
    def rollback(self) -> bool:
        return self.verdict in DELTA_PROBE_ROLLBACK_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "rollback": self.rollback,
            "probe_band_hz": list(self.probe_band_hz),
            "n_bins": self.n_bins,
            "max_error_db": self.max_error_db,
            "rms_error_db": self.rms_error_db,
            "worst_hz": self.worst_hz,
            "exceedance_octaves": self.exceedance_octaves,
            "gain_factor": self.gain_factor,
            "tolerance_low_db": self.tolerance_low_db,
            "tolerance_high_db": self.tolerance_high_db,
            "expected_offset_db": self.expected_offset_db,
            "residual_offset_db": self.residual_offset_db,
            "residual_offset_tolerance_db": self.residual_offset_tolerance_db,
            "requested_band_hz": list(self.requested_band_hz),
            "gain_intercept_db": self.gain_intercept_db,
            # The quiet-bin evidence ``residual_offset_db`` rests on, nested so a
            # reader judging a level claim gets one set, not four keys to pair up.
            "quiet": {
                "n_bins": self.quiet_n_bins,
                "core_band_hz": (
                    None if self.quiet_core_band_hz is None
                    else list(self.quiet_core_band_hz)
                ),
                "probe_coverage": self.quiet_probe_coverage,
                "entry_anchor_offset_db": self.entry_anchor_offset_db,
            },
            # Whether the hearing half was measured at all. Top level because it
            # governs BOTH blocks below; ``False`` means every directional
            # finding under it is an absence, not a pass.
            "safety_anchored": self.safety_anchored,
            # The directional boost finding: the answer, the amount, and how wide
            # it ran, as one set (#2537).
            "boost": {
                "over_declared_bound": self.boost_over_declared_bound,
                "overshoot_db": self.boost_overshoot_db,
                "overshoot_octaves": self.boost_overshoot_octaves,
            },
            # Which WAY the bins missed (#2559). TWO findings, on two references,
            # each with its OWN amount, and they are not interchangeable: the
            # first pair is what the SPEAKER did across the apply and is what a
            # hazard is read off; the second is how far the room departed from our
            # MODEL and is a next-round target.
            "direction": {
                "realized_louder_than_commanded": (
                    self.realized_louder_than_commanded
                ),
                "realized_excess_db": self.realized_excess_db,
                "model_departure_over_tolerance": (
                    self.model_departure_over_tolerance
                ),
                "max_signed_error_db": self.max_signed_error_db,
                "max_signed_error_hz": self.max_signed_error_hz,
                "seam_rollback_deferral": seam_rollback_deferral(self),
            },
            # How much of the commanded correction arrived, PER BAND, with the
            # provenance saying which bins were allowed to answer (#2649).
            # ``pooled`` is ``gain_factor`` under its band-resolved name, kept
            # here too so a consumer never reaches outside the block to compare
            # the whole-band answer against the per-band ones.
            "realization": {
                "comparand": REALIZED_VS_COMMANDED_COMPARAND,
                "pooled": self.gain_factor,
                "bands": {
                    band_id: dict(entry)
                    for band_id, entry in self.band_realization.items()
                },
                "graded_band_hz": (
                    None if self.graded_band_hz is None
                    else list(self.graded_band_hz)
                ),
                "trusted_floor_hz": self.trusted_floor_hz,
                "trust_ceiling_hz": self.trust_ceiling_hz,
            },
            "frame": {
                **self.frame.to_dict(),
                "removed": {
                    "max_db": self.frame_removed_max_db,
                    "rms_db": self.frame_removed_rms_db,
                    "exceedance_octaves": self.frame_removed_exceedance_octaves,
                },
            },
            "spatial": self.spatial.to_dict(),
        }


def _unavailable(
    reason: str,
    spatial: SpatialCost,
    *,
    expected_offset_db: float = 0.0,
    requested_band_hz: tuple[float, float] = (0.0, 0.0),
) -> DeltaProbeMap:
    return DeltaProbeMap(
        verdict=VERDICT_UNAVAILABLE, reason=reason, probe_band_hz=(0.0, 0.0),
        n_bins=0, max_error_db=0.0, rms_error_db=0.0, worst_hz=0.0,
        exceedance_octaves=0.0, gain_factor=None,
        tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
        tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
        spatial=spatial,
        expected_offset_db=expected_offset_db,
        requested_band_hz=requested_band_hz,
    )


def _safety_only(
    spatial: SpatialCost,
    *,
    expected_offset_db: float,
    requested_band_hz: tuple[float, float],
    probe_band_hz: tuple[float, float],
    n_bins: int,
    safety_anchored: bool,
    boost_over_declared_bound: bool,
    boost_overshoot_db: float | None,
    boost_overshoot_octaves: float,
    realized_louder_than_commanded: bool,
    realized_excess_db: float | None,
    model_departure_over_tolerance: bool,
    max_signed_error_db: float | None,
    max_signed_error_hz: float | None,
) -> DeltaProbeMap:
    """A map carrying the model's departure and NO grade of anything else (#2614).

    Every shape and level scalar keeps its dataclass default, because on this
    path the classifier was handed the STATE axis in the commanded slot and each
    of those numbers computed against a state axis would be a claim in the wrong
    frame. Returning them "for information" is how a wrong number gets read as a
    right one.

    What survives the frame change is the MODEL's departure and only that
    (series-2 D1): ``realized − commanded`` is well-defined here, but this path
    has no pre-apply capture to turn it into a statement about the speaker. So
    ``safety_anchored`` is False and the map says the hearing half did not run.
    """
    return DeltaProbeMap(
        verdict=VERDICT_SAFETY_ONLY,
        reason=REASON_COMMANDED_AXIS_UNAVAILABLE,
        probe_band_hz=probe_band_hz,
        n_bins=n_bins,
        max_error_db=0.0, rms_error_db=0.0, worst_hz=0.0,
        exceedance_octaves=0.0, gain_factor=None,
        tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
        tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
        spatial=spatial,
        expected_offset_db=expected_offset_db,
        requested_band_hz=requested_band_hz,
        safety_anchored=safety_anchored,
        boost_over_declared_bound=boost_over_declared_bound,
        boost_overshoot_db=boost_overshoot_db,
        boost_overshoot_octaves=boost_overshoot_octaves,
        realized_louder_than_commanded=realized_louder_than_commanded,
        realized_excess_db=realized_excess_db,
        model_departure_over_tolerance=model_departure_over_tolerance,
        max_signed_error_db=max_signed_error_db,
        max_signed_error_hz=max_signed_error_hz,
    )


def _tolerance_curve(freqs_hz: np.ndarray) -> np.ndarray:
    """The two-tier per-bin tolerance (see the two tolerance constants)."""
    return np.where(
        freqs_hz < DELTA_PROBE_HF_SPLIT_HZ,
        DELTA_PROBE_TOLERANCE_LOW_DB,
        DELTA_PROBE_TOLERANCE_HIGH_DB,
    )


def graded_command_floor_db(freqs_hz: np.ndarray) -> np.ndarray:
    """The per-bin commanded floor a bin must clear to be GRADED (#2521).

    The two-tier sibling of :func:`_tolerance_curve`, split at the same
    frequency — see :data:`DELTA_PROBE_MIN_COMMANDED_HIGH_DB`. Public because
    the band a probe graded is only reconstructible offline with the rule that
    produced it.

    NOT the quiet floor, which stays flat across the band because "the
    correction asked for nothing here" is a statement about the command rather
    than about measurement uncertainty. Between the two, at HF, sits a band of
    bins that are neither: graded by nothing and corroborating nothing.
    """
    return np.where(
        freqs_hz < DELTA_PROBE_HF_SPLIT_HZ,
        DELTA_PROBE_MIN_COMMANDED_DB,
        DELTA_PROBE_MIN_COMMANDED_HIGH_DB,
    )


def widest_exceedance_octaves(
    freqs_hz: np.ndarray, exceeds: np.ndarray,
) -> tuple[float, float]:
    """``(widest contiguous run in octaves, that run's low edge in Hz)``.

    A run is contiguous in GRID INDEX, not merely in the exceeding set — two
    exceeding bins either side of a compliant one are two runs, which is the
    point of a width rule. Width is log2 frequency, so it is the same quantity
    at 500 Hz and at 15 kHz. ``(0.0, 0.0)`` when nothing exceeds.
    """
    widest = 0.0
    widest_lo_hz = 0.0
    idx = np.flatnonzero(exceeds)
    if idx.size == 0:
        return 0.0, 0.0
    breaks = np.flatnonzero(np.diff(idx) != 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    for s, e in zip(starts, ends):
        lo_hz = float(freqs_hz[idx[s]])
        hi_hz = float(freqs_hz[idx[e]])
        if lo_hz <= 0.0 or hi_hz <= 0.0:
            continue
        span = math.log2(hi_hz / lo_hz) if hi_hz > lo_hz else 0.0
        if span > widest:
            widest = span
            widest_lo_hz = lo_hz
    return widest, widest_lo_hz


def _structured_exceedance(
    freqs_hz: np.ndarray,
    error_db: np.ndarray,
    tolerance_db: np.ndarray,
    probe_mask: np.ndarray,
) -> tuple[bool, float]:
    """``(is a real finding, widest run in octaves)`` for one error curve.

    **Every array is on the FULL grid**, with ``probe_mask`` marking the bins
    inside the probe band. Load-bearing, not a convenience: the width rule
    counts a run as contiguous in GRID INDEX, so evaluating it on the compacted
    ``freqs[mask]`` subarray welds bins octaves apart into one "wide" run
    wherever the mask has a hole — and the commanded floor puts a hole in it on
    every correction that cuts low and boosts high. Masking the EXCEEDANCE
    instead keeps every removed bin as a run-breaker, which is what it
    physically is.
    """
    exceeds = probe_mask & (np.abs(error_db) > tolerance_db)
    widest, _ = widest_exceedance_octaves(freqs_hz, exceeds)
    return widest >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES, widest


def boost_overshoot(
    freqs_hz: np.ndarray,
    excess_db: np.ndarray,
    commanded_db: np.ndarray,
    tolerance_db: np.ndarray,
    probe_mask: np.ndarray,
    declared_db: np.ndarray | None = None,
) -> tuple[bool, float | None, float]:
    """Did a BOOST realize MORE lift than the graph declared? (#2537)

    ``(over the bound, worst signed excess in dB, widest run in octaves)``,
    measured only in bins where a boost is on the table. Every array is on the
    full grid, for :func:`_structured_exceedance`'s run-contiguity reason.

    **``excess_db`` is a measured CHANGE, and the caller owes that** (series-2
    D1): ``(measured_post − measured_pre) − expected_offset − commanded``. Taken
    as ONE array rather than derived from a realized and a commanded curve,
    because that derivation cancels the commanded term and leaves the acoustic
    model's own error — a caller with no pre-apply capture cannot express this
    quantity and must not call this rule at all.

    **Two axes select the bins** (#2614). ``commanded_db`` is a CHANGE, so on a
    repeat round it is ~0 across every band the apply leaves alone, including a
    band the applied graph still boosts by 5 dB; ``declared_db`` is that graph's
    own predicted transfer, so a bin qualifies when EITHER curve boosts. Neither
    contributes a VALUE — they choose bins — which is why a union mask is sound
    here and not in :func:`_structured_exceedance`. ``None`` falls back to
    ``commanded_db`` alone, exactly right for a first-ever apply.

    The union sees a hazard the moment it APPEARS, and does **not** see one
    already present in BOTH captures, which subtracts to zero identically. That
    is the price of an instrument that cannot be fooled by the model.

    **Directional, where every other exceedance rule here is not**: this asks
    the hearing-safety question — is the speaker putting more energy into a
    driver than the graph declared — and under-realizing a boost is not that.
    The bound is this probe's own per-bin tolerance, i.e. measurement
    uncertainty and NOT a declared boost limit, and the finding is STRUCTURED
    (:data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES`) so one noisy bin cannot pull a
    household's correction off the speaker.

    The middle value is ``None`` when no bin carried a boost on either axis —
    "not measured", never 0.0.
    """
    declared = commanded_db if declared_db is None else declared_db
    boosted = probe_mask & ((commanded_db > 0.0) | (declared > 0.0))
    if not bool(boosted.any()):
        return False, None, 0.0
    worst = float(np.max(excess_db[boosted]))
    widest, _ = widest_exceedance_octaves(
        freqs_hz, boosted & (excess_db > tolerance_db)
    )
    return widest >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES, worst, float(widest)


def louder_than_commanded(
    excess_db: np.ndarray,
    tolerance_db: np.ndarray,
    probe_mask: np.ndarray,
) -> tuple[bool, float | None]:
    """Did ANY bin come out LOUDER than the excess curve's reference? (#2559)

    ``(over the bound anywhere, the most POSITIVE excess in dB)`` — the
    direction of a miss, where every other exceedance rule here takes ``abs``.

    **Called twice, on two curves, and the reference decides what the answer
    means** (series-2 D1). On the ANCHORED excess it is a hearing fact about the
    speaker and is what withholds ADR-0209's lenience; on the unanchored
    ``realized − commanded`` it is a fact about the acoustic MODEL — a real
    next-round target and not a hazard.

    **Deliberately unstructured**, where :func:`boost_overshoot` and
    :func:`_structured_exceedance` require a run: those decide whether a defect
    EXISTS, and this decides whether an existing finding may be handled the
    LENIENT way, so one bin measured louder is enough to withhold it. A width
    rule here would admit more positive-direction evidence into the quiet class,
    which is the wrong direction to be generous in; no octaves term is returned,
    so nothing downstream can gate on structure by accident.

    **Bins only, no frame removal**: a frame is removed to ask whether the SHAPE
    is right, and this asks how much energy reached the driver.

    ``probe_mask`` is :func:`classify_delta_probe`'s SAFETY mask, not its graded
    one (#2614). ``None`` for the scalar only when the mask selects nothing.
    """
    if not bool(probe_mask.any()):
        return False, None
    return (
        bool((probe_mask & (excess_db > tolerance_db)).any()),
        float(np.max(excess_db[probe_mask])),
    )


def _octave_span(span_hz: tuple[float, float]) -> float:
    """A ``(low, high)`` frequency span's width in octaves; ``0.0`` if degenerate."""
    lo, hi = float(span_hz[0]), float(span_hz[1])
    if not (lo > 0.0 and hi > lo):
        return 0.0
    return math.log2(hi / lo)


def interquartile_band_hz(freqs_hz: np.ndarray) -> tuple[float, float] | None:
    """The middle half of a bin set, as ``(low, high)`` in hertz (#2533).

    The robust reading of "where does this evidence sit": min/max is what two
    stray bins defeat, and the probe already reports that as ``frame.band_hz``.
    Public because the coverage ratio a verdict turns on is only reconstructible
    offline with the statistic that produced it. ``None`` for an empty set, or
    one whose quartiles do not straddle a positive span.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    if freqs.size == 0:
        return None
    lo, hi = (float(v) for v in np.percentile(freqs, (25.0, 75.0)))
    return (lo, hi) if hi > lo > 0.0 else None


def _band_realization(
    freqs: np.ndarray,
    deframed: np.ndarray,
    commanded: np.ndarray,
    *,
    graded: np.ndarray,
    in_band: np.ndarray,
    ceiling_hz: float | None,
) -> dict[str, dict[str, Any]]:
    """Per-band realization ratio: how much of what was commanded arrived.

    One least-squares slope per band instead of one over all of them, so a
    defect confined to a band cannot be smeared across the whole graded span,
    and a band the fitter was never allowed to command in cannot contribute to
    the verdict at all. Each entry carries ``band_hz`` (the bins actually
    present, not the nominal edges), ``n_bins``, ``ratio`` — ``None`` under
    :data:`DELTA_PROBE_MIN_BINS`, because a slope that says nothing must not be
    reported as a number that does — and ``graded``, always False for
    ``above_ceiling``.
    """

    hf = freqs >= DELTA_PROBE_HF_SPLIT_HZ
    above = (
        in_band & (freqs > float(ceiling_hz))
        if ceiling_hz is not None
        else np.zeros_like(in_band)
    )
    selectors = (
        (DELTA_PROBE_BAND_CROSSOVER, graded & ~hf, True),
        (DELTA_PROBE_BAND_TRUSTED_HF, graded & hf, True),
        (DELTA_PROBE_BAND_ABOVE_CEILING, above & np.isfinite(commanded), False),
    )
    out: dict[str, dict[str, Any]] = {}
    for band_id, sel, is_graded in selectors:
        n = int(np.count_nonzero(sel))
        entry: dict[str, Any] = {
            "band_hz": (
                [float(freqs[sel][0]), float(freqs[sel][-1])] if n else None
            ),
            "n_bins": n,
            "ratio": None,
            "graded": bool(is_graded),
        }
        if n >= DELTA_PROBE_MIN_BINS:
            c_b = commanded[sel]
            design = np.column_stack((np.ones_like(c_b), c_b))
            try:
                _, slope = np.linalg.lstsq(
                    design, deframed[sel], rcond=None,
                )[0]
            except (np.linalg.LinAlgError, ValueError):
                slope = np.nan
            if np.isfinite(slope):
                entry["ratio"] = float(slope)
        out[band_id] = entry
    return out


def classify_delta_probe(
    freqs_hz: np.ndarray,
    realized_delta_db: np.ndarray,
    commanded_delta_db: np.ndarray,
    *,
    band_hz: tuple[float, float],
    spatial: SpatialCost = SPATIAL_COST_UNAVAILABLE,
    expected_offset_db: float = 0.0,
    entry_delta_db: Any | None = None,
    declared_transfer_db: Any | None = None,
    trust_ceiling_hz: float | None = None,
    state_axis_only: bool = False,
) -> DeltaProbeMap:
    """Classify one applied correction's realized-vs-commanded map.

    All three arrays share one frequency grid (the caller interpolates). The
    probe band is ``band_hz`` intersected with the bins commanding at least
    :func:`graded_command_floor_db`.

    ``band_hz`` is the band the CALLER trusts, and this function does not
    second-guess it: it owns no gate, no validity floor and no ceiling. Handing
    in the raw grid edges instead is what let a bin 1.3 kHz above the trusted
    ceiling produce a 23.4 dB headline and a rollback (#2521).

    ``expected_offset_db`` is the whole-band level move the EMITTER knows it
    made and did NOT command as part of the correction's shape (in production,
    the pre-split headroom the applied graph charges for its own boost). It is
    subtracted from the realized curve before anything is measured. Default
    ``0.0`` — "nothing known" — leaves the whole shift visible in
    ``residual_offset_db`` rather than pretending it was accounted for.

    ``entry_delta_db`` is the PRE-apply capture in the same frame as
    ``realized_delta_db``, so ``residual_offset_db`` can be a CHANGE (#2533).
    Optional, on ``expected_offset_db``'s rule: unsupplied, length-disagreeing
    or non-finite all mean "nothing known". **It is also what makes the two
    directional SAFETY findings possible at all** (series-2 D1), and there the
    absence rule is stricter than "leave it visible": without this curve the
    findings are not made rather than made on the model's error, and
    ``safety_anchored`` reports which happened. Applied per bin, because a
    whole-band scalar cannot cancel a standing error that lives at one frequency.

    ``declared_transfer_db`` is the STATE axis the two directional rules select
    bins with (#2614): the applied graph's own predicted transfer against the
    uncorrected crossover, on the same grid. The rules are then measured over
    the UNION of the two axes' graded bins, so a repeat round that leaves an
    existing boost band untouched still has it watched. Optional on
    ``entry_delta_db``'s rule, and an identity on a first-ever apply. Nothing
    else this function measures is touched by it.

    ``state_axis_only`` says the curve in the ``commanded_delta_db`` slot is
    that STATE axis, because the caller has no change axis to give (#2614) — the
    reachable case is a previous graph the caller could not name. The verdict is
    :data:`VERDICT_SAFETY_ONLY` and the map carries no shape, level or
    directional grade. Do NOT pass ``entry_delta_db`` on that path: it is a
    change measurement and shares no reference with a state axis.

    **CHAINED ROUNDS:** ``commanded_delta_db`` and ``entry_delta_db`` are both
    stated against the graph that was live at entry, so they subtract cleanly
    and the residual is ``mean(measured_post − measured_pre − commanded)`` over
    the quiet bins (#2611). What the caller owes is that its previous side
    describe the graph the entry capture actually went through; the crossover
    corner is the part that can move, and both ways it can are checked and
    refused by ``crossover_v2.commanded``.

    Topology-agnostic by construction: a measured curve, a commanded curve and a
    band. A 1-way passive speaker's summed chain classifies through this same
    path with no special case.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    realized = np.asarray(realized_delta_db, dtype=np.float64)
    commanded = np.asarray(commanded_delta_db, dtype=np.float64)
    offset = float(expected_offset_db)
    if not math.isfinite(offset):
        offset = 0.0
    requested_band_hz = (float(band_hz[0]), float(band_hz[1]))
    if not (freqs.shape == realized.shape == commanded.shape):
        return _unavailable(
            "grid_mismatch", spatial, expected_offset_db=offset,
            requested_band_hz=requested_band_hz,
        )

    # Remove the KNOWN move before any measurement below, so everything reported
    # is a statement about what is left after the emitter's own accounting.
    realized = realized - offset

    # The mic-trust ceiling, intersected in (#2649). The fitter may not command
    # there; the probe may not grade there. The caller derives the ceiling — this
    # function owns no gate, floor or ceiling of its own — but the intersection
    # happens here so one place decides which bins are graded.
    #
    # The grading band comes from the capture's own gate disclosure, which is
    # blind to the mic tier: grading bins commanded at zero and measured through
    # a microphone nobody trusts manufactured 90 % of the 2026-08-16 round's
    # squared error.
    lo_hz, hi_hz = requested_band_hz
    graded_hi_hz = hi_hz
    if trust_ceiling_hz is not None and float(trust_ceiling_hz) < graded_hi_hz:
        graded_hi_hz = float(trust_ceiling_hz)
    measurable = np.isfinite(realized) & np.isfinite(commanded)
    # What the CALLER asked to grade, before the ceiling narrowed it. Kept so the
    # map can report the excluded span rather than silently dropping it.
    requested_in_band = (freqs >= lo_hz) & (freqs <= hi_hz) & measurable
    in_band = requested_in_band & (freqs <= graded_hi_hz)
    floor = graded_command_floor_db(freqs)
    mask = in_band & (np.abs(commanded) >= floor)
    if int(mask.sum()) < DELTA_PROBE_MIN_BINS:
        return _unavailable(
            "nothing_commanded", spatial, expected_offset_db=offset,
            requested_band_hz=requested_band_hz,
        )

    # The STATE axis, read by the two directional safety rules only (#2614).
    # ``commanded`` is a CHANGE, so on a repeat round the graded ``mask`` stops
    # covering a band the applied graph still boosts by 5 dB. The hearing-safety
    # question is not "did the change realize" but *is the speaker putting more
    # energy into a driver than the applied graph declares, anywhere*, so those
    # rules watch the UNION.
    #
    # A union rather than a swap: the graded mask stays exactly as it is, so no
    # bin watched before this change stops being watched. ``None`` degrades to
    # the graded mask alone, which is an identity on a first-ever apply.
    declared: np.ndarray | None = None
    if declared_transfer_db is not None:
        candidate_declared = np.asarray(declared_transfer_db, dtype=np.float64)
        if candidate_declared.shape == freqs.shape:
            declared = candidate_declared
    safety_mask = mask
    if declared is not None:
        safety_mask = mask | (
            in_band & np.isfinite(declared) & (np.abs(declared) >= floor)
        )

    f = freqs[mask]
    r = realized[mask]
    c = commanded[mask]
    probe_band_hz = (float(f[0]), float(f[-1]))

    # Scalar statistics read the probe bins only. The exceedance WIDTH is
    # measured on the full grid with the mask applied to the exceedance itself,
    # so grid adjacency survives (see _structured_exceedance).
    error = r - c
    max_error_db = float(np.max(np.abs(error)))
    rms_error_db = float(np.sqrt(np.mean(error ** 2)))
    worst_hz = float(f[int(np.argmax(np.abs(error)))])

    # The uncommanded remainder, measured in the QUIET bins (#1811) — inside the
    # analysis band but BELOW the commanded floor. It has to be measured there:
    # inside the probe band, "flat across every bin we look at" is also what a
    # correction that overshot its whole commanded region looks like. Where the
    # correction asked for nothing, any level is uncommanded by construction.
    quiet = in_band & (np.abs(commanded) < DELTA_PROBE_MIN_COMMANDED_DB)
    quiet_measurable = int(quiet.sum()) >= DELTA_PROBE_MIN_BINS

    # ...and measured as a CHANGE across the apply, not as an absolute
    # disagreement with the model (#2533). ``realized − commanded`` is
    # ``measured_post − predicted_post``: two curves that do not share a level
    # anchor, whose mismatch is a standing property of the comparison rather than
    # a level MOVE. Subtracting the same quantity measured on the PRE-apply
    # capture cancels it exactly, because ``entry_delta_db`` is built against the
    # same model curve:
    #
    #     (measured_post − predicted − offset) − (measured_pre − predicted)
    #         − commanded  ==  (measured_post − measured_pre) − commanded − offset
    #
    # On the 2026-08-15 JTS3 session that standing term was the largest of the
    # three: −3.342 dB decomposed as −1.660 standing, −1.457 real measured
    # change, −0.221 declared graph move.
    #
    # The subtraction is an identity only because the two curves share one
    # reference graph (#2611): ``commanded`` is the applied graph's predicted sum
    # minus the ENTRY graph's, and ``entry_delta`` measures that same entry
    # graph. While ``commanded`` was stated against the RAW crossover instead, a
    # repeat round's residual carried ``−mean(previous round's commanded curve
    # over these bins)`` — a term this function is never given and cannot bound,
    # able to fabricate a +6.000 dB phantom or to mask a genuine −2.2 dB shift.
    entry: np.ndarray | None = None
    if entry_delta_db is not None:
        candidate = np.asarray(entry_delta_db, dtype=np.float64)
        if candidate.shape == freqs.shape:
            entry = candidate
    anchored = (
        quiet_measurable
        and entry is not None
        and int((quiet & np.isfinite(entry)).sum()) >= DELTA_PROBE_MIN_BINS
    )
    # One bin set for the residual and for the anchor removed from it, so the
    # decomposition below is an identity rather than an approximation.
    residual_bins = (quiet & np.isfinite(entry)) if anchored and entry is not None else quiet
    entry_anchor_offset_db: float | None = (
        float(np.mean(entry[residual_bins])) if anchored and entry is not None else None
    )
    residual_offset_db: float | None = (
        float(
            np.mean(realized[residual_bins] - commanded[residual_bins])
            - (entry_anchor_offset_db or 0.0)
        )
        if quiet_measurable
        else None
    )

    # WHERE those bins sit, and how spread they are relative to a FULL sampling
    # of the band their level is claimed over (#2533). The INTERQUARTILE span is
    # the robust reading, and the robustness is load-bearing: min/max is what two
    # stray bins defeat. The denominator is that SAME statistic over every
    # graded-band bin on this grid, never the band's whole span — see
    # :data:`DELTA_PROBE_MIN_QUIET_COVERAGE`.
    quiet_n_bins = int(residual_bins.sum()) if quiet_measurable else 0
    quiet_core_band_hz: tuple[float, float] | None = None
    quiet_probe_coverage: float | None = None
    if quiet_measurable:
        quiet_core_band_hz = interquartile_band_hz(freqs[residual_bins])
        band_core_hz = interquartile_band_hz(
            freqs[in_band & (freqs >= probe_band_hz[0]) & (freqs <= probe_band_hz[1])]
        )
        band_core_octaves = 0.0 if band_core_hz is None else _octave_span(band_core_hz)
        if quiet_core_band_hz is not None and band_core_octaves > 0.0:
            quiet_probe_coverage = (
                _octave_span(quiet_core_band_hz) / band_core_octaves
            )

    # The FRAME between the two curves, fitted over the QUIET bins (#2521) — the
    # same argument one term further: a slope measured where the correction asked
    # for nothing is uncommanded by construction.
    #
    # Its offset term is the ABSOLUTE quiet-bin disagreement, so the identity is
    # ``residual == frame.offset_db − entry_anchor_offset_db`` when every quiet
    # bin carried a usable anchor, degrading to ``residual == frame.offset_db``
    # when none did.
    #
    # NOT fitted over the graded bins: a two-parameter fit over the region the
    # correction commands lets the defect set its own frame and subtract itself —
    # on the keystone fixture that took the 2026-07-27 shelf-Q error's exceedance
    # from 0.575 octaves to zero.
    frame = (
        fit_frame(freqs[quiet], realized[quiet], commanded[quiet])
        if quiet_measurable
        else FRAME_UNFITTED
    )
    deframed = realized - frame.frame_db(freqs)

    # Least-squares realized/commanded scale WITH an intercept, on the
    # frame-removed curve. Two different fixes: the intercept stops a level
    # offset from arriving as apparent scale (#2521's ~2.02 on a −7.8 dB
    # constant), and the frame-removed input stops a room's broadband tilt from
    # doing the same against a ramp-shaped command.
    #
    # ``c`` carries only bins at or above the graded floor, so the design matrix
    # is rank-deficient only if every graded bin commands the SAME value, which
    # ``lstsq`` resolves to the minimum-norm solution rather than raising. On
    # that degenerate shape a flat ``k`` dB command realized exactly returns
    # ``k²/(1+k²)`` — a perfectly realized shallow flat lift reading as a deep
    # shortfall. It stays off production paths because the degeneracy is
    # knife-edge: a commanded range of 1e-9 dB already returns gain exactly 1.0,
    # a filter cascade's graded bins are never one repeated constant, and the
    # branch is unreachable unless something else already put the map over
    # tolerance.
    design = np.column_stack((np.ones_like(c), c))
    intercept, gain_factor = (
        float(v) for v in np.linalg.lstsq(design, deframed[mask], rcond=None)[0]
    )
    # The same question asked per band (#2649). ``gain_factor`` above keeps its
    # place on the wire; this is the band-resolved answer the shortfall verdict
    # reads.
    realization = _band_realization(
        freqs, deframed, commanded,
        graded=mask,
        in_band=requested_in_band,
        ceiling_hz=graded_hi_hz if trust_ceiling_hz is not None else None,
    )

    tolerance_full = _tolerance_curve(freqs)
    error_full = np.where(mask, realized - commanded, 0.0)
    exceeded, exceedance_octaves = _structured_exceedance(
        freqs, error_full, tolerance_full, mask,
    )
    # The same three graded scalars with the frame removed, reported beside the
    # raw ones rather than instead of them: the raw grade decides whether there
    # is a finding, and only the ROLLBACK question is re-asked here (#2521).
    frame_error_full = np.where(mask, deframed - commanded, 0.0)
    frame_exceeded, frame_exceedance_octaves = _structured_exceedance(
        freqs, frame_error_full, tolerance_full, mask,
    )
    frame_error = frame_error_full[mask]

    # THE TWO DIRECTIONAL SAFETY FINDINGS, and the anchor that makes them
    # findings about the SPEAKER rather than about our model of it (series-2 D1):
    #
    #   model_excess  = realized − commanded  ==  (measured_post − predicted)
    #                                             − expected_offset
    #   safety_excess = model_excess − entry  ==  (measured_post − measured_pre)
    #                                             − expected_offset − commanded
    #
    # The first cancels ``commanded`` identically, so it grades how far the room
    # departed from the two-branch model and nothing else. The second subtracts
    # the pre-apply capture in the same frame, so a standing model error present
    # in BOTH captures cancels — the same anchor and the same bins-argument
    # ``residual_offset_db`` already uses.
    #
    # Measured on the RAW curves, not the frame-removed one: a frame is removed
    # to answer whether the SHAPE is right, and this asks how much energy reached
    # the driver. On the SAFETY mask, not the graded one (#2614).
    model_excess = realized - commanded
    # ENFORCED, not merely documented: a state axis shares no reference with a
    # change measurement, so an anchor supplied alongside one would produce a
    # finding in a mixed frame. A separate name rather than clearing ``entry``,
    # whose meaning for the residual above is unaffected. The one production
    # caller passes no anchor here, so this makes the invariant unbreakable by
    # the next caller rather than closing a reachable defect.
    safety_anchor = None if state_axis_only else entry
    safety_excess = (
        model_excess if safety_anchor is None else model_excess - safety_anchor
    )
    # No anchor, no finding: a bin with no usable pre-apply level cannot say what
    # the speaker DID there. The bar is the module's own minimum, so a handful of
    # surviving bins cannot carry a hard stop either.
    safety_bins = safety_mask & np.isfinite(safety_excess)
    safety_anchored = (
        safety_anchor is not None
        and int(safety_bins.sum()) >= DELTA_PROBE_MIN_BINS
    )
    if safety_anchored:
        boost_over_bound, boost_overshoot_db, boost_overshoot_octaves = (
            boost_overshoot(
                freqs, safety_excess, commanded, tolerance_full, safety_bins,
                declared_db=declared,
            )
        )
        realized_louder, realized_excess_db = louder_than_commanded(
            safety_excess, tolerance_full, safety_bins,
        )
    else:
        boost_over_bound, boost_overshoot_db, boost_overshoot_octaves = (
            False, None, 0.0,
        )
        realized_louder, realized_excess_db = False, None
    # ...and the MODEL's own departure, always, on the unanchored curve — a real
    # defect for the next round to chase (the blend region is where this model is
    # known blind, #2600) and never a hazard.
    model_departure_over_tolerance, max_signed_error_db = louder_than_commanded(
        model_excess, tolerance_full, safety_mask,
    )
    # WHERE it peaks, and a different bin from ``worst_hz`` often enough to
    # matter: that one is the worst ABSOLUTE error over the GRADED bins, this the
    # worst POSITIVE departure over the SAFETY bins (1947.2 Hz and 1384.1 Hz on
    # the banked series-2 r1b). An amount and its frequency travel together.
    max_signed_error_hz: float | None = (
        float(freqs[safety_mask][int(np.argmax(model_excess[safety_mask]))])
        if max_signed_error_db is not None
        else None
    )

    # The caller had no CHANGE axis and said so (#2614): every shape and level
    # scalar computed above is a claim in the wrong frame, so return the model's
    # departure and none of it. The shape work is not skipped, only discarded —
    # one lstsq and one frame fit per session is a smaller price than a second
    # exit path through half this function. The two directional findings do not
    # survive either: this path has no pre-apply capture to anchor against.
    if state_axis_only:
        return _safety_only(
            spatial,
            expected_offset_db=offset,
            requested_band_hz=requested_band_hz,
            probe_band_hz=probe_band_hz,
            n_bins=int(f.size),
            safety_anchored=safety_anchored,
            boost_over_declared_bound=boost_over_bound,
            boost_overshoot_db=boost_overshoot_db,
            boost_overshoot_octaves=boost_overshoot_octaves,
            realized_louder_than_commanded=realized_louder,
            realized_excess_db=realized_excess_db,
            model_departure_over_tolerance=model_departure_over_tolerance,
            max_signed_error_db=max_signed_error_db,
            max_signed_error_hz=max_signed_error_hz,
        )

    def _map(verdict: str, reason: str) -> DeltaProbeMap:
        return DeltaProbeMap(
            verdict=verdict, reason=reason, probe_band_hz=probe_band_hz,
            n_bins=int(f.size), max_error_db=max_error_db,
            rms_error_db=rms_error_db, worst_hz=worst_hz,
            exceedance_octaves=float(exceedance_octaves),
            gain_factor=gain_factor,
            tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
            tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
            spatial=spatial,
            expected_offset_db=offset,
            residual_offset_db=residual_offset_db,
            requested_band_hz=requested_band_hz,
            frame=frame,
            frame_removed_max_db=(
                float(np.max(np.abs(frame_error))) if frame.fitted else None
            ),
            frame_removed_rms_db=(
                float(np.sqrt(np.mean(frame_error ** 2))) if frame.fitted else None
            ),
            frame_removed_exceedance_octaves=(
                float(frame_exceedance_octaves) if frame.fitted else None
            ),
            gain_intercept_db=intercept,
            entry_anchor_offset_db=entry_anchor_offset_db,
            quiet_n_bins=quiet_n_bins,
            quiet_core_band_hz=quiet_core_band_hz,
            quiet_probe_coverage=quiet_probe_coverage,
            safety_anchored=safety_anchored,
            boost_over_declared_bound=boost_over_bound,
            boost_overshoot_db=boost_overshoot_db,
            boost_overshoot_octaves=boost_overshoot_octaves,
            realized_louder_than_commanded=realized_louder,
            realized_excess_db=realized_excess_db,
            model_departure_over_tolerance=model_departure_over_tolerance,
            max_signed_error_db=max_signed_error_db,
            max_signed_error_hz=max_signed_error_hz,
            band_realization=realization,
            trust_ceiling_hz=(
                None if trust_ceiling_hz is None else float(trust_ceiling_hz)
            ),
            graded_band_hz=(float(lo_hz), float(graded_hi_hz)),
        )

    if not exceeded:
        # The chain did what it was told at the mark. Now — and only now — is the
        # spatial question the interesting one.
        if spatial.available and spatial.widened:
            return _map(VERDICT_SPATIALLY_COSTLY, "cross_position_spread_widened")
        return _map(VERDICT_MATCHED, "")

    # The map does not match. Before asking shape-or-scale, ask whether it fails
    # only because the level moved by something nobody commanded (#1811). Two
    # conditions, BOTH required:
    #
    # (a) the quiet-bin residual is material on its own terms — the EVIDENCE that
    #     a level shift exists, from bins no shape defect can reach;
    # (b) removing the quiet bins' offset from the probe band makes the map pass
    #     — what makes the shift SUFFICIENT.
    #
    # Together they keep every diagnostic underneath intact: a proportional
    # shortfall moves the quiet bins by nothing and fails (a); a mis-realized
    # shelf leaves structure that survives subtracting a constant and fails (b).
    #
    # **They subtract DIFFERENT numbers, and they have to** (#2533). (a) is a
    # change question and reads the anchored ``residual_offset_db``; (b) asks
    # whether the quiet bins explain a failure measured against the MODEL, so
    # what comes out is their whole disagreement with it, standing anchor
    # included. Handing (b) the anchored number would leave the standing offset
    # inside the levelled error and send a genuine uncommanded shift one gate
    # later, as the less specific ``frame_mismatch``.
    if residual_offset_db is not None:
        quiet_offset_db = residual_offset_db + (entry_anchor_offset_db or 0.0)
        levelled_error_full = np.where(
            mask, realized - commanded - quiet_offset_db, 0.0
        )
        levelled_exceeded, _ = _structured_exceedance(
            freqs, levelled_error_full, tolerance_full, mask,
        )
        if (
            abs(residual_offset_db) > DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB
            and not levelled_exceeded
        ):
            # (c) WHERE the evidence sits (#2533). (a) and (b) both hold, so
            # there IS a finding, and that is not re-litigated: falling through
            # would hand the same evidence to the frame gate and the shape branch,
            # either of which can ROLL BACK. An instrument that has just declared
            # its evidence unrepresentative must not become stricter on it.
            #
            # So the verdict, the rollback answer and the household surface are
            # unchanged; what narrows is the CLAIM, with ``quiet_core_band_hz``
            # naming the band it covers. A co-spanning quiet set scores 1.0 and
            # keeps the whole-band reason; what the bar narrows is evidence
            # concentrated somewhere the band is not (0.248 for a 12-20 kHz
            # sliver, 0.036 for a single mid-band notch).
            band_scoped = (
                quiet_probe_coverage is not None
                and quiet_probe_coverage < DELTA_PROBE_MIN_QUIET_COVERAGE
            )
            return _map(
                VERDICT_LEVEL_MISMATCH,
                REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND if band_scoped
                else REASON_UNCOMMANDED_LEVEL_SHIFT,
            )
        unavailable_suffix = ""
    else:
        # No quiet bins: the level discriminator could not run, so the verdicts
        # below are reached WITHOUT the check that separates "the level moved"
        # from "the shape is wrong". An undeclared offset lands as ``model_error``
        # here — a rollback — so say that the call was made blind.
        unavailable_suffix = _LEVEL_CHECK_UNAVAILABLE_SUFFIX

    # THE FRAME GATE, and it sits AHEAD OF BOTH ROLLBACK DOORS on purpose
    # (#2521; docs/measurement-loop-doctrine.md §3). An exceedance that does not
    # survive removing the frame is a statement about the two curves' frames,
    # which this comparison cannot attribute to the correction — disclosed
    # loudly, and the household keeps the tuning.
    #
    # It guards the SHORTFALL door too, and it has to: a real but in-tolerance
    # depth shortfall combined with a room tilt walks through a gate that guards
    # ``model_error`` alone. Reproduced: a 4 dB lift realized at 80 % depth is
    # ``matched`` on its own and becomes a ``level_dependent_shortfall`` ROLLBACK
    # once a −0.9 dB/octave frame is added, with its frame-removed exceedance
    # still exactly 0.0; 203 of 4,000 randomized draws rolled back on evidence
    # that was entirely frame.
    #
    # An unfitted frame removes nothing, so this branch cannot fire: no frame
    # measured, no demotion. :data:`VERDICT_SPATIALLY_COSTLY` is not behind this
    # gate and cannot be — it is returned above, from the branch where the mark
    # map MATCHED, and there is no model between its two measurements.
    if not frame_exceeded:
        return _map(VERDICT_FRAME_MISMATCH, "uncommanded_frame_shift")

    # Shape or scale? Re-measure the error against the best-fit SCALED command:
    # if the residual then passes, the shape is right and only the depth is
    # short; if it still fails, the shape itself is wrong, which is a claim about
    # our model of the filters rather than about the driver's headroom.
    #
    # Both sides are the frame-removed curve and the fitted line through it, so
    # the intercept is USED and not merely estimated: a residual measured against
    # ``gain·commanded`` alone would re-admit the level term it exists to hold out.
    scaled_error_full = np.where(
        mask, deframed - (intercept + gain_factor * commanded), 0.0
    )
    scaled_exceeded, _ = _structured_exceedance(
        freqs, scaled_error_full, tolerance_full, mask,
    )
    # A *level-dependent* shortfall is a claim about a driver failing to deliver
    # LEVEL, so it requires that level was what the correction asked for. A
    # proportional undershoot of a set of CUTS is not compression.
    commanded_is_lift = float(np.max(c)) >= DELTA_PROBE_MIN_COMMANDED_DB
    # **The GRADED BANDS decide, not the pooled slope** (#2649). Bins above the
    # mic-trust ceiling are already out of ``mask``, and the ratio tested is the
    # per-band one because the pooled fit is not a realization measurement when
    # the graded band carries two disjoint COMMANDED ranges: ``lstsq`` puts one
    # line through the (commanded, realized) cloud, which is then the CHORD
    # BETWEEN THE TWO BAND CENTROIDS and reports the level difference between the
    # bands rather than the fraction either realized. A round where both bands
    # realized 1.00x fit a chord of 0.459 and was ROLLED BACK.
    #
    # EVERY graded band must fall short, not just the worst: a driver failing to
    # deliver LEVEL fails everywhere it was asked, and a band-localised miss is a
    # shape error. Falls back to the pooled slope only when no graded band
    # cleared :data:`DELTA_PROBE_MIN_BINS`.
    graded_ratios = [
        entry["ratio"] for entry in realization.values()
        if entry["graded"] and entry["ratio"] is not None
    ]
    shortfall_ratio = max(graded_ratios) if graded_ratios else gain_factor
    if (
        not scaled_exceeded
        and commanded_is_lift
        and 0.0 <= shortfall_ratio < DELTA_PROBE_SHORTFALL_GAIN_CEILING
    ):
        return _map(
            VERDICT_LEVEL_DEPENDENT_SHORTFALL,
            "realized_short_of_commanded" + unavailable_suffix,
        )
    return _map(
        VERDICT_MODEL_ERROR,
        "realized_shape_differs_from_commanded" + unavailable_suffix,
    )


def spatial_cost_from_group_spreads(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None,
) -> SpatialCost:
    """Adapter: two cloud-group result mappings → a :class:`SpatialCost`.

    Reads the ``"band_spread"`` list each group publishes (plain dicts after a
    JSON round-trip, or ``BandSpread`` objects in-process). Absent or short
    spreads degrade to :data:`SPATIAL_COST_UNAVAILABLE` rather than raising: an
    express session has no post-apply group, and "no spatial evidence" is an
    honest answer, not an error.
    """

    def _bands(group: Mapping[str, Any] | None) -> list[Any]:
        if not isinstance(group, Mapping):
            return []
        raw = group.get("band_spread")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return []
        out: list[Any] = []
        for entry in raw:
            if isinstance(entry, Mapping):
                center = entry.get("center_hz")
                sigma = entry.get("sigma_db")
                if isinstance(center, (int, float)) and isinstance(sigma, (int, float)):
                    out.append(_PlainBand(float(center), float(sigma)))
            elif hasattr(entry, "center_hz") and hasattr(entry, "sigma_db"):
                out.append(entry)
        return out

    before_bands, after_bands = _bands(before), _bands(after)
    if not before_bands or not after_bands:
        return SPATIAL_COST_UNAVAILABLE
    return evaluate_spatial_cost(before_bands, after_bands)


@dataclass(frozen=True)
class _PlainBand:
    """The two fields :func:`evaluate_spatial_cost` reads, off a JSON dict."""

    center_hz: float
    sigma_db: float


__all__ = [
    "DELTA_PROBE_BAND_ABOVE_CEILING",
    "DELTA_PROBE_BAND_CROSSOVER",
    "DELTA_PROBE_BAND_TRUSTED_HF",
    "DELTA_PROBE_HF_SPLIT_HZ",
    "DELTA_PROBE_MIN_BINS",
    "DELTA_PROBE_MIN_COMMANDED_DB",
    "DELTA_PROBE_MIN_COMMANDED_HIGH_DB",
    "DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES",
    "DELTA_PROBE_MIN_QUIET_COVERAGE",
    "DELTA_PROBE_REALIZATION_BANDS",
    "DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB",
    "DELTA_PROBE_REALIZED_VS_COMMANDED_VERDICTS",
    "DELTA_PROBE_ROLLBACK_VERDICTS",
    "DELTA_PROBE_SHORTFALL_GAIN_CEILING",
    "DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB",
    "DELTA_PROBE_TOLERANCE_HIGH_DB",
    "DELTA_PROBE_TOLERANCE_LOW_DB",
    "DELTA_PROBE_VERDICTS",
    "SEAM_DEFERRED_QUIETER_THAN_COMMANDED",
    "SPATIAL_COST_UNAVAILABLE",
    "REASON_COMMANDED_AXIS_UNAVAILABLE",
    "REASON_UNCOMMANDED_LEVEL_SHIFT",
    "REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND",
    "DeltaProbeMap",
    "SpatialCost",
    "VERDICT_FRAME_MISMATCH",
    "VERDICT_LEVEL_DEPENDENT_SHORTFALL",
    "VERDICT_LEVEL_MISMATCH",
    "VERDICT_MATCHED",
    "VERDICT_MODEL_ERROR",
    "VERDICT_SAFETY_ONLY",
    "VERDICT_SPATIALLY_COSTLY",
    "VERDICT_UNAVAILABLE",
    "boost_overshoot",
    "classify_delta_probe",
    "evaluate_spatial_cost",
    "graded_command_floor_db",
    "interquartile_band_hz",
    "louder_than_commanded",
    "seam_rollback_deferral",
    "spatial_cost_from_group_spreads",
    "widest_exceedance_octaves",
]
