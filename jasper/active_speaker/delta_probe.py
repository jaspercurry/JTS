# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Delta-probe verification: did the speaker do what the correction asked?

Pure computation; the session owns I/O, state and rollback.
``commanded_delta_db`` is the applied graph's predicted sum minus the graph
it replaces; ``realized_delta_db`` is the measured post-apply response minus
that same prior-graph prediction — not level-offset-invariant (hence
``expected_offset_db`` and the quiet-bin frame). A directional SAFETY
finding also needs ``entry_delta_db``. See
docs/measurement-loop-doctrine.md §3 and ADR-0209.
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

#: The correction realized what it commanded.
VERDICT_MATCHED = "matched"
#: Realized and commanded disagree in SHAPE. Roll back and flag.
VERDICT_MODEL_ERROR = "model_error"
#: Realized tracks commanded shape but falls materially short in scale on a
#: lift. A compression diagnostic. Roll back and flag.
VERDICT_LEVEL_DEPENDENT_SHORTFALL = "level_dependent_shortfall"
#: Matched at the mark, but the cross-position spread WIDENED — routes to a
#: placement-vs-speaker service verdict. Roll back.
VERDICT_SPATIALLY_COSTLY = "spatially_costly"
#: Fails ONLY because of a level shift measured where nothing was commanded
#: (sufficient alone to explain the failure). Not in
#: :data:`DELTA_PROBE_ROLLBACK_VERDICTS`.
VERDICT_LEVEL_MISMATCH = "level_mismatch"
#: Fails ONLY because of the FRAME between the two curves — offset and tilt
#: fitted over quiet (uncommanded) bins (#2521). Supersedes any other
#: rollback verdict; not in :data:`DELTA_PROBE_ROLLBACK_VERDICTS`.
VERDICT_FRAME_MISMATCH = "frame_mismatch"
#: The correction commands nothing in the probe band, or curves could not
#: be compared. Not a pass: no evidence to refuse on either.
VERDICT_UNAVAILABLE = "unavailable"
#: Carries the MODEL's departure only (#2614) — a declared transfer but no
#: CHANGE axis. Not a pass, deliberately not a rollback:
#: :attr:`DeltaProbeMap.safety_anchored` is False.
VERDICT_SAFETY_ONLY = "safety_only"

#: Why the shape half did not run on a :data:`VERDICT_SAFETY_ONLY` map.
REASON_COMMANDED_AXIS_UNAVAILABLE = "commanded_axis_unavailable"

#: Every verdict this module can return. Pinned by a test.
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

#: Verdicts on which rollback is AUTOMATIC. ``unavailable`` is excluded — an
#: absent measurement is not evidence of a bad correction. LEVEL_MISMATCH
#: and FRAME_MISMATCH are excluded too (level/tilt axis, not shape; the
#: known cause is our own accounting, not the correction).
DELTA_PROBE_ROLLBACK_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY,
})

#: Why the seam defers a rollback verdict to the adoption table (#2559).
#: See ADR-0209.
SEAM_DEFERRED_QUIETER_THAN_COMMANDED = "realized_quieter_than_commanded"

#: Rollback classes that defer when the deviation points entirely quieter
#: (ADR-0209). SPATIALLY_COSTLY is absent: no model between its two
#: measurements (doctrine §3).
DELTA_PROBE_REALIZED_VS_COMMANDED_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
})

#: What band ratios grade realized against — the COMMANDED delta, vs.
#: ``verification.REALIZATION_COMPARAND`` (an absolute claim).
REALIZED_VS_COMMANDED_COMPARAND = "commanded_delta"


def seam_rollback_deferral(probe: Any | None) -> str:
    """Why this map's seam-bound rollback DEFERS to the adoption table (ADR-0209),
    or ``""`` for an absent probe or non-rollback verdict."""
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

# Max |realized − commanded| tolerated below DELTA_PROBE_HF_SPLIT_HZ.
# Matches crossover_v2_flow.VERIFY_TOLERANCE_DB.
DELTA_PROBE_TOLERANCE_LOW_DB: float = 1.5

# Max |realized − commanded| tolerated at/above DELTA_PROBE_HF_SPLIT_HZ.
# linearization_fit.HF_AGREEMENT_LIMIT_HIGH_DB allows 2.0 dB of repeat-sweep
# spread up here; UMIK-2 stock-cal is ~±2.3 dB @16 kHz.
DELTA_PROBE_TOLERANCE_HIGH_DB: float = 2.5

# Mirrors linearization_fit._HF_AGREEMENT_TIER_SPLIT_HZ.
DELTA_PROBE_HF_SPLIT_HZ: float = 10_000.0

# Minimum contiguous octave span for a tolerance exceedance to count as a
# finding — the coarser of the two ladder-smoothing windows (1/6 octave
# below 4 kHz, 1/3 above).
DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES: float = 1.0 / 3.0

# Below this, the correction commands nothing worth verifying at that bin.
# Mirrors linearization_fit._MIN_FILTER_GAIN_DB; also THE QUIET FLOOR (below
# the HF split it is also the graded floor).
DELTA_PROBE_MIN_COMMANDED_DB: float = 0.5

# Commanded floor a bin must clear to be GRADED at/above
# DELTA_PROBE_HF_SPLIT_HZ (#2521), equal to DELTA_PROBE_TOLERANCE_HIGH_DB by
# design. Do not lower below the split — on this module's keystone fixture
# a flat 1.0 dB floor drops the 2026-07-27 shelf-Q defect's exceedance from
# 0.575 to 0.307 octaves, under DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES.
DELTA_PROBE_MIN_COMMANDED_HIGH_DB: float = 2.5

# Minimum bins the probe band must retain after masking to regress or
# measure a run width.
DELTA_PROBE_MIN_BINS: int = 8

# Best-fit realized/commanded scale below which a shape-tracking map is a
# level-dependent SHORTFALL rather than a model error.
DELTA_PROBE_SHORTFALL_GAIN_CEILING: float = 0.85

#: Band ids a realization ratio is reported per (#2649), one per band
#: rather than one pooled slope. ``crossover`` = below
#: DELTA_PROBE_HF_SPLIT_HZ, ``trusted_hf`` = at/above it,
#: ``above_ceiling`` = reported, never graded.
DELTA_PROBE_BAND_CROSSOVER = "crossover"
DELTA_PROBE_BAND_TRUSTED_HF = "trusted_hf"
DELTA_PROBE_BAND_ABOVE_CEILING = "above_ceiling"

#: Every band id a realization block can carry, in report order.
DELTA_PROBE_REALIZATION_BANDS: tuple[str, ...] = (
    DELTA_PROBE_BAND_CROSSOVER,
    DELTA_PROBE_BAND_TRUSTED_HF,
    DELTA_PROBE_BAND_ABOVE_CEILING,
)

# Widening of across-position level spread (BandSpread.sigma_db, dB) beyond
# which the post-apply cloud is spatially costly.
DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB: float = 1.0

# |residual common mode| beyond which a whole-band level shift is named
# VERDICT_LEVEL_MISMATCH rather than left in the shape verdict (#1811). A
# magnitude bar, NOT the discriminator; see classify_delta_probe.
DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB: float = 1.5

# How spread the quiet evidence (DeltaProbeMap.quiet_probe_coverage) must
# be, relative to a full sampling of the graded band, before a level claim
# may be made band-wide (#2533). 0.5 sits in a wide measured gap: tightest
# passing shape scores 0.870, the shape this catches scores 0.248.
DELTA_PROBE_MIN_QUIET_COVERAGE: float = 0.5

#: ``reason`` for a level shift measured across the whole graded band.
REASON_UNCOMMANDED_LEVEL_SHIFT = "uncommanded_level_shift"
#: ``reason`` for the same finding when quiet bins are less spread than
#: DELTA_PROBE_MIN_QUIET_COVERAGE (#2533) — verdict unchanged, only the
#: claimed band narrows.
REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND = (
    "uncommanded_level_shift_outside_probe_band"
)

#: Appended to a non-matched verdict's ``reason`` when too few quiet bins
#: existed to measure ``residual_offset_db`` (#1811) — the verdict stands but
#: was reached without the level discriminator.
_LEVEL_CHECK_UNAVAILABLE_SUFFIX = "|level_check_unavailable"


# --------------------------------------------------------------------------- #
# spatial arm
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpatialCost:
    """Did the correction make the room LESS even? Measurement-minus-measurement
    over the pre/post-apply position groups; ``available`` is False when
    either has fewer than two positions."""

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

    ``before``/``after`` are BandSpread sequences paired by ``center_hz``.
    Uses ``sigma_db`` (per-position BAND LEVEL spread), not
    ``max_sigma_db``, which rides comb nulls unrelated to correction.
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
    exactly when it is in :data:`DELTA_PROBE_ROLLBACK_VERDICTS`.
    ``gain_factor`` is the least-squares realized/commanded scale (1.0 =
    full depth), ``None`` when unavailable (never 0.0). Measured on the
    frame-removed curve (#2521), with an intercept
    (:attr:`gain_intercept_db`).
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
    #: Level move the EMITTER told us it made, dB, removed before anything
    #: below is computed (#1811).
    expected_offset_db: float = 0.0
    #: Level CHANGE across the apply that nobody commanded, dB — a CHANGE,
    #: not an absolute (#2533). On a CHAINED round trustworthy only if the
    #: previous side describes the entry graph — see
    #: :func:`classify_delta_probe`. ``None`` when too few quiet bins.
    residual_offset_db: float | None = None
    residual_offset_tolerance_db: float = DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB
    #: The band the caller HANDED IN. Distinct from ``probe_band_hz`` (bins
    #: that cleared the commanded floor inside it) — a reader needs to know
    #: whether a bin was excluded for lack of trust or of a command (#2521).
    requested_band_hz: tuple[float, float] = (0.0, 0.0)
    #: Frame fitted over the QUIET bins — offset and tilt, uncommanded by
    #: construction (#2521). ``FRAME_UNFITTED`` when too few quiet bins.
    frame: FrameFit = FRAME_UNFITTED
    #: The three graded scalars again, after :attr:`frame` is removed —
    #: ``None`` together, only when no frame was fitted.
    frame_removed_max_db: float | None = None
    frame_removed_rms_db: float | None = None
    frame_removed_exceedance_octaves: float | None = None
    #: Where the ``gain_factor`` regression crosses zero commanded, dB.
    gain_intercept_db: float | None = None
    #: Standing disagreement between pre-apply measurement and the
    #: two-branch model, dB, over the same quiet bins (#2533) — removed from
    #: :attr:`residual_offset_db`. ``None`` means not measured.
    entry_anchor_offset_db: float | None = None
    #: Quiet bins :attr:`residual_offset_db` was measured over.
    quiet_n_bins: int = 0
    #: Interquartile span of those bins' frequencies, Hz (#2533) — robust
    #: to the stray bins that defeat ``frame.band_hz``'s min/max.
    quiet_core_band_hz: tuple[float, float] | None = None
    #: :attr:`quiet_core_band_hz`'s octave span over the whole graded band's
    #: — 1.0 is co-spanning; below :data:`DELTA_PROBE_MIN_QUIET_COVERAGE`
    #: the verdict narrows its reason.
    quiet_probe_coverage: float | None = None
    #: Were the two directional findings below measured against the
    #: PRE-APPLY capture (series-2 D1)? ``False`` means neither ran.
    safety_anchored: bool = False
    #: Did a BOOST realize more lift than declared, structurally? (#2537)
    #: The adoption table's one hard stop from this probe — see
    #: :func:`boost_overshoot`. Measured over the SAFETY bins (#2614).
    boost_over_declared_bound: bool = False
    #: Worst signed ANCHORED excess, dB, over boosted safety bins; positive
    #: is undeclared delivered energy. ``None`` when no boosted bin was
    #: measured.
    boost_overshoot_db: float | None = None
    #: Widest contiguous run, octaves, over which that excess cleared
    #: tolerance. ``0.0`` means nothing cleared it.
    boost_overshoot_octaves: float = 0.0
    #: Did ANY safety bin come out LOUDER than declared, past its own
    #: tolerance? (#2559) See :func:`louder_than_commanded`.
    realized_louder_than_commanded: bool = False
    #: Most POSITIVE ANCHORED excess over safety bins, dB — distinct from
    #: :attr:`boost_overshoot_db` (boosted bins only). ``None`` if not
    #: measured.
    realized_excess_db: float | None = None
    #: Did the room depart from the two-branch MODEL, upward past
    #: tolerance, in the safety bins? Unanchored reading of the same rule —
    #: a next-round target (#2600), never a hazard.
    model_departure_over_tolerance: bool = False
    #: Most POSITIVE unanchored ``realized − commanded`` over safety bins,
    #: dB. ``None`` when no bin was in the safety mask.
    max_signed_error_db: float | None = None
    #: Frequency :attr:`max_signed_error_db` was measured at, Hz — not
    #: :attr:`worst_hz` (worst ABSOLUTE over GRADED bins vs. worst POSITIVE
    #: over SAFETY bins).
    max_signed_error_hz: float | None = None
    #: How much commanded arrived, PER BAND (#2649), keyed by
    #: :data:`DELTA_PROBE_REALIZATION_BANDS`; ``{band_hz, n_bins, ratio,
    #: graded}`` per entry.
    band_realization: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    #: Mic-trust ceiling the caller applied, Hz.
    trust_ceiling_hz: float | None = None
    #: Band actually GRADED: requested band intersected with the ceiling.
    graded_band_hz: tuple[float, float] | None = None

    @property
    def trusted_floor_hz(self) -> float | None:
        """Graded band's lower edge, banked by the round receipt (#2609 SF5)
        so a later round can refuse a cross-floor comparison."""
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
            # The quiet-bin evidence ``residual_offset_db`` rests on, nested.
            "quiet": {
                "n_bins": self.quiet_n_bins,
                "core_band_hz": (
                    None if self.quiet_core_band_hz is None
                    else list(self.quiet_core_band_hz)
                ),
                "probe_coverage": self.quiet_probe_coverage,
                "entry_anchor_offset_db": self.entry_anchor_offset_db,
            },
            # Governs both blocks below; ``False`` means every directional
            # finding under it is an absence, not a pass.
            "safety_anchored": self.safety_anchored,
            "boost": {
                "over_declared_bound": self.boost_over_declared_bound,
                "overshoot_db": self.boost_overshoot_db,
                "overshoot_octaves": self.boost_overshoot_octaves,
            },
            # Two findings on two references (#2559): the first is what the
            # SPEAKER did (a hazard is read off it); the second is how far the
            # room departed from our MODEL (a next-round target).
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
            # ``pooled`` is ``gain_factor`` under its band-resolved name (#2649).
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

    Every shape and level scalar keeps its dataclass default: on this path
    the classifier was handed the STATE axis in the commanded slot, and each
    of those numbers computed against it would be a claim in the wrong
    frame. ``safety_anchored`` is False — this path has no pre-apply capture
    to turn ``realized − commanded`` into a statement about the speaker
    (series-2 D1).
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

    Two-tier sibling of :func:`_tolerance_curve`, split at the same
    frequency — see :data:`DELTA_PROBE_MIN_COMMANDED_HIGH_DB`. Public so a
    probe's graded band is reconstructible offline. Not the quiet floor
    (flat across the band): that is about the command, this about
    measurement uncertainty.
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

    A run is contiguous in GRID INDEX, not the exceeding set — two exceeding
    bins either side of a compliant one are two runs. Width is log2
    frequency (comparable at any center frequency). ``(0.0, 0.0)`` if none.
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

    Every array is on the FULL grid; ``probe_mask`` marks the probe band —
    load-bearing, since a compacted subarray would weld distant bins into
    one "wide" run at every mask hole.
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

    ``(over the bound, worst signed excess in dB, widest run in octaves)``
    over bins where a boost is on the table. ``excess_db`` must be a
    measured CHANGE (series-2 D1): ``(measured_post − measured_pre) −
    expected_offset − commanded``. A bin qualifies when EITHER
    ``commanded_db`` or ``declared_db`` (graph's own predicted transfer)
    boosts (#2614); ``declared_db=None`` falls back to ``commanded_db``
    alone. Directional and STRUCTURED
    (:data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES`) — under-realizing a boost
    is not the hazard this asks about. Middle value ``None``, never 0.0,
    when no bin carried a boost.
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

    ``(over the bound anywhere, most POSITIVE excess in dB)``. Called on
    the ANCHORED excess (a hearing fact, withholds ADR-0209's lenience) and
    on the unanchored ``realized − commanded`` (an acoustic-MODEL target).
    Deliberately unstructured, unlike :func:`boost_overshoot` — one bin is
    enough. ``probe_mask`` is the SAFETY mask, not the graded one (#2614).
    ``None`` only when the mask selects nothing.
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

    Robust reading of where evidence sits — min/max (``frame.band_hz``) is
    what two stray bins defeat. ``None`` for an empty set or degenerate
    quartiles.
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

    One least-squares slope per band rather than one over all of them, so a
    defect confined to one band cannot be smeared across the whole graded
    span. Each entry carries ``band_hz`` (bins actually present), ``n_bins``,
    ``ratio`` (``None`` under :data:`DELTA_PROBE_MIN_BINS`), and ``graded``
    (always False for ``above_ceiling``).
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

    All three arrays share one frequency grid. Probe band is ``band_hz``
    intersected with bins commanding at least :func:`graded_command_floor_db`
    — this function owns no gate/floor/ceiling beyond that (#2521).

    ``expected_offset_db``: whole-band level move the EMITTER made and did
    NOT command, subtracted from ``realized`` first. ``entry_delta_db``:
    pre-apply capture in the same frame, so ``residual_offset_db`` is a
    CHANGE (#2533); also required for both directional SAFETY findings
    (series-2 D1) — its absence means those findings are not made.
    ``declared_transfer_db``: the STATE axis the two directional rules
    union into their bin selection (#2614). ``state_axis_only``: the
    ``commanded_delta_db`` slot holds a STATE axis (no change axis
    available, #2614) — returns :data:`VERDICT_SAFETY_ONLY`; do NOT pass
    ``entry_delta_db`` alongside it.

    CHAINED ROUNDS: ``commanded_delta_db`` and ``entry_delta_db`` must both
    be stated against the graph live at entry (#2611).
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

    # Remove the KNOWN move before any measurement below.
    realized = realized - offset

    # Intersect the mic-trust ceiling here (#2649) — the caller derives the
    # ceiling, this function owns no gate of its own, but one place decides
    # which bins are graded. Grading bins measured through a mic nobody
    # trusts manufactured 90% of the 2026-08-16 round's squared error.
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
    # ``commanded`` is a CHANGE, so on a repeat round the graded ``mask``
    # stops covering a band the applied graph still boosts by 5 dB; those
    # rules watch the UNION instead. ``None`` degrades to the graded mask
    # alone (an identity on a first-ever apply).
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
    # measured on the full grid with the mask applied to the exceedance
    # itself, so grid adjacency survives (see _structured_exceedance).
    error = r - c
    max_error_db = float(np.max(np.abs(error)))
    rms_error_db = float(np.sqrt(np.mean(error ** 2)))
    worst_hz = float(f[int(np.argmax(np.abs(error)))])

    # The uncommanded remainder, measured in the QUIET bins (#1811) — inside
    # the analysis band but BELOW the commanded floor: inside the probe
    # band, "flat everywhere" is also what an overshot correction looks
    # like.
    quiet = in_band & (np.abs(commanded) < DELTA_PROBE_MIN_COMMANDED_DB)
    quiet_measurable = int(quiet.sum()) >= DELTA_PROBE_MIN_BINS

    # Measured as a CHANGE, not an absolute disagreement with the model
    # (#2533): subtracting the PRE-apply capture cancels the standing
    # ``measured_post − predicted_post`` mismatch:
    #     (measured_post − predicted − offset) − (measured_pre − predicted)
    #         − commanded == (measured_post − measured_pre) − commanded − offset
    # Holds only because ``commanded`` and ``entry_delta`` share one
    # reference graph (#2611, the ENTRY graph).
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

    # WHERE those bins sit, and how spread relative to a full sampling of the
    # band their level is claimed over (#2533) — see
    # DELTA_PROBE_MIN_QUIET_COVERAGE.
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

    # The FRAME between the two curves, fitted over the QUIET bins (#2521):
    # a slope measured where the correction asked for nothing is uncommanded
    # by construction. NOT fitted over the graded bins — on the keystone
    # fixture, a two-parameter fit there let the 2026-07-27 shelf-Q defect
    # set its own frame and subtract itself, taking its exceedance from
    # 0.575 octaves to zero.
    frame = (
        fit_frame(freqs[quiet], realized[quiet], commanded[quiet])
        if quiet_measurable
        else FRAME_UNFITTED
    )
    deframed = realized - frame.frame_db(freqs)

    # Least-squares realized/commanded scale WITH an intercept, on the
    # frame-removed curve: the intercept stops a level offset from arriving
    # as apparent scale (#2521), the frame-removed input stops a room tilt
    # doing the same. Rank-deficient only if every graded bin commands the
    # SAME value, which ``lstsq`` resolves to the minimum-norm solution
    # rather than raising — unreachable off a filter cascade in production.
    design = np.column_stack((np.ones_like(c), c))
    intercept, gain_factor = (
        float(v) for v in np.linalg.lstsq(design, deframed[mask], rcond=None)[0]
    )
    # The same question asked per band (#2649); the shortfall verdict reads
    # this band-resolved answer.
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
    # Same three graded scalars with the frame removed, beside the raw ones:
    # the raw grade decides whether there is a finding, only the ROLLBACK
    # question is re-asked here (#2521).
    frame_error_full = np.where(mask, deframed - commanded, 0.0)
    frame_exceeded, frame_exceedance_octaves = _structured_exceedance(
        freqs, frame_error_full, tolerance_full, mask,
    )
    frame_error = frame_error_full[mask]

    # THE TWO DIRECTIONAL SAFETY FINDINGS (series-2 D1), on the RAW curves
    # (a frame answers SHAPE; this asks energy reaching the driver) and the
    # SAFETY mask, not the graded one (#2614):
    #   model_excess  = realized − commanded
    #   safety_excess = model_excess − entry (cancels a standing model error
    #                   present in both captures)
    model_excess = realized - commanded
    # ENFORCED: a state axis shares no reference with a change measurement.
    safety_anchor = None if state_axis_only else entry
    safety_excess = (
        model_excess if safety_anchor is None else model_excess - safety_anchor
    )
    # No anchor, no finding: a bin with no usable pre-apply level cannot say
    # what the speaker DID there.
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
    # The MODEL's own departure, always, on the unanchored curve — a
    # next-round target (the blend region is known blind, #2600), never a
    # hazard.
    model_departure_over_tolerance, max_signed_error_db = louder_than_commanded(
        model_excess, tolerance_full, safety_mask,
    )
    # WHERE it peaks — often a different bin from ``worst_hz`` (worst
    # ABSOLUTE error over GRADED bins vs. worst POSITIVE over SAFETY bins:
    # 1947.2 Hz and 1384.1 Hz on the banked series-2 r1b).
    max_signed_error_hz: float | None = (
        float(freqs[safety_mask][int(np.argmax(model_excess[safety_mask]))])
        if max_signed_error_db is not None
        else None
    )

    # The caller had no CHANGE axis (#2614): every shape/level scalar above
    # is a claim in the wrong frame, so return the model's departure and
    # none of it. The shape work above is not skipped, only discarded — one
    # lstsq and frame fit per session is cheaper than a second exit path.
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
        if spatial.available and spatial.widened:
            return _map(VERDICT_SPATIALLY_COSTLY, "cross_position_spread_widened")
        return _map(VERDICT_MATCHED, "")

    # Before shape-or-scale: does the map fail only because the level moved
    # by something nobody commanded (#1811)? Requires BOTH (a) the
    # quiet-bin residual is material on its own (``residual_offset_db``)
    # and (b) removing the quiet-bin offset makes the map pass (their whole
    # disagreement with the MODEL, #2533).
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
            # A finding stands (not re-litigated below, which can ROLL
            # BACK); only the CLAIM narrows if the quiet evidence is
            # concentrated rather than band-wide (#2533).
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
        # No quiet bins: the level discriminator could not run — an
        # undeclared offset lands as ``model_error`` (a rollback) reached
        # blind.
        unavailable_suffix = _LEVEL_CHECK_UNAVAILABLE_SUFFIX

    # THE FRAME GATE sits ahead of both rollback doors on purpose (#2521;
    # docs/measurement-loop-doctrine.md §3): an exceedance that doesn't
    # survive removing the frame is a statement about the curves' frames,
    # not the correction. Guards the SHORTFALL door too — 203 of 4,000
    # randomized draws rolled back on evidence that was entirely frame.
    if not frame_exceeded:
        return _map(VERDICT_FRAME_MISMATCH, "uncommanded_frame_shift")

    # Shape or scale? Re-measure against the best-fit SCALED command: pass
    # means depth-only shortfall, fail means the shape itself is wrong. The
    # intercept is USED, not just estimated — dropping it would re-admit
    # the level term this is meant to hold out.
    scaled_error_full = np.where(
        mask, deframed - (intercept + gain_factor * commanded), 0.0
    )
    scaled_exceeded, _ = _structured_exceedance(
        freqs, scaled_error_full, tolerance_full, mask,
    )
    # A level-dependent shortfall requires that level was what was asked
    # for — a proportional undershoot of CUTS is not compression.
    commanded_is_lift = float(np.max(c)) >= DELTA_PROBE_MIN_COMMANDED_DB
    # THE GRADED BANDS decide, not the pooled slope (#2649): with two
    # disjoint COMMANDED ranges, ``lstsq`` reports the chord between band
    # centroids, not either band's realized fraction (a round where both
    # realized 1.00x fit a chord of 0.459 and was ROLLED BACK). EVERY
    # graded band must fall short; falls back to the pooled slope only when
    # no band cleared :data:`DELTA_PROBE_MIN_BINS`.
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

    Reads the ``"band_spread"`` list each group publishes (plain dicts after
    a JSON round-trip, or ``BandSpread`` objects in-process). Absent or
    short spreads degrade to :data:`SPATIAL_COST_UNAVAILABLE` rather than
    raising.
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
