# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The correction envelope: how many dB of correction depth each frequency
bin is allowed.

Pure computation only: numpy plus
:func:`jasper.audio_measurement.analysis.smooth_fractional_octave` and
:class:`jasper.audio_measurement.program_analysis.DriverResponse`. No I/O, no
product policy, no CamillaDSP/emission imports.

It has live callers: ``crossover_v2.intervention``'s ``plan_linearization``
composes an envelope per driver role and hands it to
:func:`~jasper.active_speaker.linearization_fit.fit_driver_linearization`,
whose filters are persisted and applied on hardware. Read a change to any
term as a change to a live correction profile
(docs/active-speaker-tuning-layers-design.md "Layer 1a concretely").

Not to be confused with :mod:`jasper.correction.envelope` or
:mod:`jasper.active_speaker.crossover_envelope_v2`: those are *screen*
envelopes (wizard UI state), and none of them computes a correction depth.

See docs/active-speaker-tuning-layers-design.md "The correction envelope"
for the adopted design this module implements:

    allowed_depth(f) = min(
        mic_trust_limit(f, tier),
        repeatability_limit(f, sigma(f)),
        class_prior_limit(f, class),
    )

and the sigma(f) reference implementation this mirrors:
``captures/xover-e0-2026-07-21/sigma-seeding-20260723/compute_sigma.py``
(session-artifact; see the ``REPORT.md`` beside it for the corpus findings
that seeded the tier/class tables below).

Two further terms are optional and cloud-derived. When a caller has a
*spatial cloud* — several mic positions combined by
:func:`~jasper.audio_measurement.spatial_combine.combine_positions` — it may
hand :func:`compose_envelope` that cloud's honesty evidence, and the min
above gains:

    spatial_exclusion_limit(f, merged honesty intervals)
    position_stability_limit(f, cloud BandSpread, N)

Both are **narrowing only** — they enter the same ``np.min`` as everything
else, so an envelope with them can never allow more depth than the same
envelope without them. Both default to absent, and an absent term composes
to byte-identical output. They sit on opposite sides of the final smoothing
pass; :func:`compose_envelope` states why.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.spatial_combine import BandSpread

from ._common import DRIVER_CLASSES

if TYPE_CHECKING:
    from jasper.audio_measurement.program_analysis import DriverResponse


class ReasonCode(StrEnum):
    """Per-bin honesty-guard vocabulary — why a bin's allowed depth is what it is.

    Wider than what this module produces: ``LIMITED_BY_VERIFY_DIVERGENCE`` is
    reserved for closed-loop verification feedback and nothing emits it yet,
    and ``BEYOND_MEASUREMENT_CONFIDENCE`` is emitted by the FIT layer. Both
    live here so every persisted reason code reads against one enum.
    """

    FITTED = "envelope_fitted"
    LIMITED_BY_MIC_TIER = "envelope_limited_by_mic_tier"
    LIMITED_BY_REPEATABILITY = "envelope_limited_by_repeatability"
    LIMITED_BY_CLASS_PRIOR = "envelope_limited_by_class_prior"
    LIMITED_BY_SPATIAL_EXCLUSION = "envelope_limited_by_spatial_exclusion"
    LIMITED_BY_POSITION_STABILITY = "envelope_limited_by_position_stability"
    LIMITED_BY_VERIFY_DIVERGENCE = "envelope_limited_by_verify_divergence"
    BEYOND_MEASUREMENT_CONFIDENCE = "envelope_beyond_measurement_confidence"
    OUT_OF_BAND = "envelope_out_of_band"


# Closed vocabulary (design doc "Microphone doctrine"). `compose_envelope`
# and every term function taking a tier rejects anything outside this tuple.
MIC_TIERS: tuple[str, ...] = ("reference", "consumer", "phone")

# Shared working grid: log spacing from 150 Hz — the design doc's gated-
# measurement validity floor, "~143-200 Hz in the JTS3 room" — to 20 kHz.
# 176 points is >=4x finer than the smoothing ladder's finest step (1/6 oct).
DEFAULT_ENVELOPE_GRID_HZ: np.ndarray = np.geomspace(150.0, 20_000.0, 176)
# Read-only: a frozen dataclass field defaulting to this array object still
# holds a live reference, so an in-place mutation would corrupt every caller.
DEFAULT_ENVELOPE_GRID_HZ.flags.writeable = False

# dB. Every term function below is capped at this value. NOT a policy number:
# the real ceiling is the wiring layer's cut/boost caps (-12 dB / +6 dB), and
# any sentinel strictly above those is behaviorally equivalent.
ENVELOPE_CEILING_SENTINEL_DB: float = 24.0

# sigma_tolerable(tier), dB — design doc "Cold-start priors". The seeding
# corpus measured 1-2 orders of magnitude below these: generous floors.
_SIGMA_TOLERABLE_DB: Mapping[str, float] = {
    "reference": 0.5,
    "consumer": 1.0,
    "phone": 1.5,
}

# mic_trust_limit's (full_to_hz, taper_zero_hz) by tier — the design doc's
# canonical table, NOT artifact 01's separate fit/verify ceiling pair.
_MIC_TRUST_TABLE_HZ: Mapping[str, tuple[float, float]] = {
    "reference": (12_000.0, 20_000.0),
    "consumer": (6_000.0, 12_000.0),
    "phone": (3_000.0, 8_000.0),
}

# class_prior_limit's full_to_hz by driver class, Hz — artifact 02 §5's table.
# taper_zero is DERIVED as full_to * 2; that multiplier is a heuristic chosen
# to match the mic-trust rows' spacing, not a researched value.
_CLASS_PRIOR_FULL_TO_HZ: Mapping[str, float] = {
    "compression_horn": 10_000.0,
    "soft_dome": 14_000.0,
    "metal_dome": 16_000.0,
    "beryllium_diamond_dome": 17_000.0,
    "ribbon_amt": 17_000.0,
    "unknown": 6_000.0,
}


def _validate_tier(tier: str) -> None:
    if tier not in MIC_TIERS:
        raise ValueError(f"unknown mic tier {tier!r}; expected one of {MIC_TIERS}")


def _validate_driver_class(driver_class: str) -> None:
    if driver_class not in DRIVER_CLASSES:
        raise ValueError(
            f"unknown driver class {driver_class!r}; expected one of {DRIVER_CLASSES}"
        )


def _ladder_smooth(grid_hz: np.ndarray, magnitude_db: np.ndarray) -> np.ndarray:
    """The design doc's smoothing ladder: 1/6 oct below 4 kHz, 1/3 oct to
    10 kHz, 1/2 oct above, hard-stitched. Mirrors compute_sigma.py's
    ``ladder_smooth_loggrid``.
    """
    fine = smooth_fractional_octave(grid_hz, magnitude_db, fraction=6)
    mid = smooth_fractional_octave(grid_hz, magnitude_db, fraction=3)
    coarse = smooth_fractional_octave(grid_hz, magnitude_db, fraction=2)
    return np.where(grid_hz < 4_000.0, fine, np.where(grid_hz < 10_000.0, mid, coarse))


def _flat_then_taper(
    freqs_hz: np.ndarray,
    full_to_hz: float,
    taper_zero_hz: float,
    *,
    sentinel_db: float = ENVELOPE_CEILING_SENTINEL_DB,
) -> np.ndarray:
    """Flat at ``sentinel_db`` to ``full_to_hz``, octave-linear taper to 0 at
    ``taper_zero_hz``, 0 above — the shape both HF limits share.
    """
    log2_f = np.log2(freqs_hz)
    log2_full_to = math.log2(full_to_hz)
    log2_taper_zero = math.log2(taper_zero_hz)
    span = log2_taper_zero - log2_full_to
    fraction = np.clip((log2_taper_zero - log2_f) / span, 0.0, 1.0)
    return sentinel_db * fraction


def compute_sigma_curve(
    primary: DriverResponse,
    *,
    valid_band_hz: tuple[float, float],
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
) -> np.ndarray | None:
    """Repeatability spread sigma(f) across a driver's in-capture sweep
    occurrences (``primary`` plus its ``repeat_responses``), on ``grid_hz``.

    Each occurrence is smoothed, then centered to its own mean over
    ``valid_band_hz``; sigma is the ddof=1 standard deviation across those.
    ``None`` when fewer than 2 occurrences exist, or when ``valid_band_hz``
    does not overlap ``grid_hz`` — no evidence, no sigma, never a guess.
    """
    occurrences: tuple[DriverResponse, ...] = (primary, *primary.repeat_responses)
    if len(occurrences) < 2:
        return None

    lo_hz, hi_hz = valid_band_hz
    valid_mask = (grid_hz >= lo_hz) & (grid_hz <= hi_hz)
    if not valid_mask.any():
        return None

    centered_curves = []
    for occurrence in occurrences:
        resampled_db = np.interp(grid_hz, occurrence.freqs_hz, occurrence.magnitude_db)
        smoothed_db = _ladder_smooth(grid_hz, resampled_db)
        ref_db = float(np.mean(smoothed_db[valid_mask]))
        centered_curves.append(smoothed_db - ref_db)

    stack = np.stack(centered_curves)
    # np.std(..., ddof=1) on a single row divides by zero and returns NaN with
    # a RuntimeWarning, not an exception; the len < 2 check above is the only
    # thing between an N=1 capture and a silently-NaN envelope term.
    return np.std(stack, axis=0, ddof=1)


def _sigma_to_depth_db(sigma_db: np.ndarray, sigma_tolerable_db: float) -> np.ndarray:
    """``ceiling . min(1, sigma_tolerable / max(sigma, eps))`` — this module's
    ONE sigma-to-allowed-depth mapping.

    Shared by :func:`repeatability_limit` and :func:`position_stability_limit`,
    which differ only in the sigma they hand in; retuning the tolerance table
    for one silently moves the other.
    """
    epsilon_db = 1e-6
    return ENVELOPE_CEILING_SENTINEL_DB * np.minimum(
        1.0, sigma_tolerable_db / np.maximum(sigma_db, epsilon_db)
    )


def repeatability_limit(
    sigma_db: np.ndarray | None,
    *,
    tier: str,
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
) -> np.ndarray:
    """``D_cap(tier) . min(1, sigma_tolerable(tier) / max(sigma, eps))`` on
    ``grid_hz`` — the shared mapping, see :func:`_sigma_to_depth_db`.

    ``sigma_db=None`` returns an ALL-ZERO array: absence of repeatability
    evidence is the tightest constraint, never an unconstrained pass-through.
    """
    _validate_tier(tier)
    if sigma_db is None:
        return np.zeros_like(grid_hz, dtype=np.float64)
    return _sigma_to_depth_db(sigma_db, _SIGMA_TOLERABLE_DB[tier])


def mic_trust_limit(freqs_hz: np.ndarray, *, tier: str) -> np.ndarray:
    """Flat at the ceiling sentinel to the tier's ``full_to``, octave-linear
    taper to 0 at ``taper_zero``, 0 above.

    Pairs from :data:`_MIC_TRUST_TABLE_HZ`. The research artifacts also hold a
    separate fit/verify ceiling table; that one is not this one.
    """
    _validate_tier(tier)
    full_to_hz, taper_zero_hz = _MIC_TRUST_TABLE_HZ[tier]
    return _flat_then_taper(freqs_hz, full_to_hz, taper_zero_hz)


def class_prior_limit(freqs_hz: np.ndarray, *, driver_class: str) -> np.ndarray:
    """Flat at the ceiling sentinel to the driver class's ``full_to``,
    octave-linear taper to 0 at ``taper_zero = full_to * 2``, 0 above.

    The one-octave taper width is a heuristic; see :data:`_CLASS_PRIOR_FULL_TO_HZ`.
    """
    _validate_driver_class(driver_class)
    full_to_hz = _CLASS_PRIOR_FULL_TO_HZ[driver_class]
    taper_zero_hz = full_to_hz * 2.0
    return _flat_then_taper(freqs_hz, full_to_hz, taper_zero_hz)


# --------------------------------------------------------------------------- #
# Cloud-derived terms: optional, narrowing only, absent unless the caller
# supplied their evidence.
# --------------------------------------------------------------------------- #


def _interval_mask(
    freqs_hz: np.ndarray, intervals: Sequence[tuple[float, float]],
) -> np.ndarray:
    """Rasterize frequency intervals onto ``freqs_hz`` — True where a bin's own
    cell overlaps any interval.

    Each bin owns the cell between the geometric midpoints to its neighbours,
    and any overlap excludes: the envelope grid steps ~2.84 % per bin while the
    intervals arrive on a grid three orders finer, so a bin is almost never
    exactly covered, and the looser "bin centre inside the interval" rule would
    leave a bin 90 % inside an identified null fully correctable. Intervals need
    not be sorted or disjoint; a descending pair would silently under-exclude,
    so it raises.
    """
    for f_lo, f_hi in intervals:
        if f_hi < f_lo:
            raise ValueError(
                f"exclusion interval ({f_lo}, {f_hi}) is descending; intervals "
                "must be (f_lo, f_hi) with f_lo <= f_hi"
            )
    mask = np.zeros_like(freqs_hz, dtype=bool)
    if freqs_hz.size == 0 or not intervals:
        return mask

    log_f = np.log2(freqs_hz)
    cell_lo_log = np.empty_like(log_f)
    cell_hi_log = np.empty_like(log_f)
    if log_f.size == 1:
        # A one-bin grid has no neighbour to take a midpoint against; the
        # cell degenerates to the point itself, so "any overlap" reduces to
        # "the interval contains this frequency".
        cell_lo_log[0] = cell_hi_log[0] = log_f[0]
    else:
        midpoints = 0.5 * (log_f[:-1] + log_f[1:])
        cell_lo_log[1:] = midpoints
        cell_hi_log[:-1] = midpoints
        cell_lo_log[0] = log_f[0] - (midpoints[0] - log_f[0])
        cell_hi_log[-1] = log_f[-1] + (log_f[-1] - midpoints[-1])
    cell_lo = np.exp2(cell_lo_log)
    cell_hi = np.exp2(cell_hi_log)

    for f_lo, f_hi in intervals:
        mask |= (cell_lo <= f_hi) & (cell_hi >= f_lo)
    return mask


def spatial_exclusion_limit(
    freqs_hz: np.ndarray, excluded_bands_hz: Sequence[tuple[float, float]],
) -> np.ndarray:
    """Zero allowed depth on honesty-masked bins, the ceiling sentinel elsewhere.

    ``excluded_bands_hz`` is the merged honesty mask — the combiner's
    power-vs-median screen unioned with the identified-null registry — as
    frequency intervals rather than a bin mask, because the two producers live
    on the combiner's grid and this module on its own. Encodes one non-goal of
    docs/historical/linearization-campaign-2026-07.md: no EQ of
    interference-flagged bins, ever. See :func:`_interval_mask` for the edge rule.
    """
    return np.where(
        _interval_mask(freqs_hz, excluded_bands_hz), 0.0, ENVELOPE_CEILING_SENTINEL_DB
    )


def position_stability_limit(
    freqs_hz: np.ndarray,
    band_spread: Sequence[BandSpread],
    *,
    n_positions: int,
    tier: str,
) -> np.ndarray:
    """Shrink allowed depth where the cloud's positions disagree about a band's
    level.

    The quantity handed to :func:`_sigma_to_depth_db` is the **standard error**
    of the combined level, ``sigma_db / sqrt(n_positions)``, per octave band:
    what the fit corrects is the cloud's mean, so the bound belongs on that
    mean's uncertainty. ``sigma_db`` rather than ``max_sigma_db`` — the level
    spread, not the structure spread, which rides comb nulls that
    :func:`spatial_exclusion_limit`'s instruments already own.

    Bands the cloud did not report leave their bins at the sentinel: no
    reading, no additional constraint. That is deliberately NOT the "no
    evidence, no permission" rule :func:`repeatability_limit` applies, because
    a missing octave band is a coverage fact already bounded by the OUT_OF_BAND
    pre-mask, the mic tier and the class prior. Overlapping bands take the
    larger standard error. ``n_positions < 2`` raises: a spread across fewer
    than two positions is undefined.

    On a protocol-following cloud (8-12 positions) the tightest limit is
    ~12.26 dB, just above the fit's 12 dB per-filter cut cap, so such a cloud
    pays nothing at the fit's surface; that ~0.26 dB is the whole margin.
    """
    _validate_tier(tier)
    if n_positions < 2:
        raise ValueError(
            f"n_positions must be >= 2 for a cross-position spread (got {n_positions})"
        )
    standard_error_db = np.zeros_like(freqs_hz, dtype=np.float64)
    root_n = math.sqrt(n_positions)
    for band in band_spread:
        band_mask = (freqs_hz >= band.f_lo) & (freqs_hz <= band.f_hi)
        standard_error_db[band_mask] = np.maximum(
            standard_error_db[band_mask], band.sigma_db / root_n
        )
    return _sigma_to_depth_db(standard_error_db, _SIGMA_TOLERABLE_DB[tier])


@dataclass(frozen=True)
class EnvelopeTerm:
    """One term's reason code paired with its full per-bin curve."""

    code: ReasonCode
    depth_db: np.ndarray


@dataclass(frozen=True)
class EnvelopeCurve:
    """The composed correction envelope for one driver role in one session.

    ``reason`` is the PRE-smoothing argmin, one code per bin: which term bound
    that bin before the final cliff-smoothing pass blended neighbours. ``terms``
    holds every term's full unmasked curve, and its KEY SET VARIES — the two
    cloud terms appear only when the caller supplied their evidence.
    ``sigma_db`` is the sigma actually consumed; ``n_repeats`` always reports
    ``primary``'s own occurrence count, independent of that.
    """

    role: str
    freqs_hz: np.ndarray
    allowed_depth_db: np.ndarray
    reason: tuple[ReasonCode, ...]
    terms: Mapping[ReasonCode, np.ndarray]
    sigma_db: np.ndarray | None
    n_repeats: int
    mic_tier: str
    driver_class: str


# Sentinel for compose_envelope's `sigma_db`: `None` is already one of the
# three states that parameter distinguishes, so unsetness needs a third value.
# Checked with `is`, never compared, logged, or handed back to a caller.
_COMPUTE: object = object()


def compose_envelope(
    role: str,
    primary: DriverResponse,
    *,
    excited_band_hz: tuple[float, float],
    mic_tier: str,
    driver_class: str = "unknown",
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
    sigma_db: np.ndarray | None | object = _COMPUTE,
    excluded_bands_hz: Sequence[tuple[float, float]] | None = None,
    band_spread: Sequence[BandSpread] | None = None,
    n_positions: int | None = None,
) -> EnvelopeCurve:
    """Compose the correction envelope: the min of every term below, with a
    hard OUT_OF_BAND pre-mask evaluated BEFORE the min and a final
    ladder-smoothing pass so term handoffs have no audible cliffs.

    The in-band region is ``excited_band_hz`` intersected with
    ``[conservative_validity_floor_hz, grid top]``, where that floor is the
    HIGHEST ``validity_floor_hz`` across primary and repeats — a bin counts as
    validated only if it cleared every occurrence's own reflection gate, and if
    no occurrence has a floor it is +inf. ``excited_band_hz`` does double duty
    as the ``valid_band_hz`` :func:`compute_sigma_curve` centres over.

    A bin whose winning term equals the ceiling sentinel is reported as
    :attr:`ReasonCode.FITTED` — no term constrained it — rather than whichever
    term argmin's first-index-wins tie-break happened to pick. A term reaching
    exactly 0 is a hard boundary the smoothing pass may not blur across: 0 is a
    statement of no trust, not a small number to average.

    **The spatial exclusion is applied AFTER the smoothing pass** — the
    composed curve is ``min(ladder_smooth(min(other terms)), exclusion)`` — so
    an excluded bin reads exactly 0.0 while a bin outside the mask reads bit
    for bit what it would with no mask. Zeroing before the pass would blur
    those zeros outward and cost the comb peaks between identified nulls, which
    the registry sizes its intervals to leave correctable.

    ``sigma_db`` is a tri-state seam for a wiring layer's own sigma policy:
    unset computes from ``primary``'s repeats, an ndarray (matching
    ``grid_hz``'s shape) is used verbatim, explicit ``None`` forces "no
    repeatability evidence" regardless of occurrence count. The cloud
    arguments all default to absent, and absent (or empty) means the term is
    not added at all; ``band_spread`` and ``n_positions`` must come together.
    """
    _validate_tier(mic_tier)
    _validate_driver_class(driver_class)

    # Resolved once so the term construction below cannot reach a half-supplied
    # pair.
    stability_evidence: tuple[Sequence[BandSpread], int] | None
    if band_spread is None and n_positions is None:
        stability_evidence = None
    elif band_spread is not None and n_positions is not None:
        stability_evidence = (band_spread, n_positions)
    else:
        raise ValueError(
            "band_spread and n_positions must be supplied together (got "
            f"band_spread={'a sequence' if band_spread is not None else None}, "
            f"n_positions={n_positions})"
        )

    occurrences: tuple[DriverResponse, ...] = (primary, *primary.repeat_responses)
    known_floors = [
        o.validity_floor_hz for o in occurrences if o.validity_floor_hz is not None
    ]
    conservative_floor_hz = max(known_floors) if known_floors else math.inf

    lo_hz, hi_hz = excited_band_hz
    excited_mask = (grid_hz >= lo_hz) & (grid_hz <= hi_hz)
    floor_mask = grid_hz >= conservative_floor_hz
    # Grid top is grid_hz's own maximum -- no separate upper check needed.
    in_band_mask = excited_mask & floor_mask

    # Tri-state; see the docstring.
    resolved_sigma_db: np.ndarray | None
    if sigma_db is _COMPUTE:
        resolved_sigma_db = compute_sigma_curve(
            primary, valid_band_hz=excited_band_hz, grid_hz=grid_hz
        )
    elif sigma_db is None:
        resolved_sigma_db = None
    elif isinstance(sigma_db, np.ndarray):
        resolved_sigma_db = sigma_db.astype(np.float64)
        if resolved_sigma_db.shape != grid_hz.shape:
            raise ValueError(
                f"sigma_db shape {resolved_sigma_db.shape} does not match "
                f"grid_hz shape {grid_hz.shape}"
            )
    else:
        raise TypeError(
            f"sigma_db must be an ndarray, None, or omitted; got {type(sigma_db)!r}"
        )

    # The three original terms keep their order, so an argmin tie among them
    # resolves as it always did. Position stability joins the SMOOTHED group;
    # spatial exclusion does not (see below).
    smoothed_terms: list[EnvelopeTerm] = [
        EnvelopeTerm(ReasonCode.LIMITED_BY_MIC_TIER, mic_trust_limit(grid_hz, tier=mic_tier)),
        EnvelopeTerm(
            ReasonCode.LIMITED_BY_REPEATABILITY,
            repeatability_limit(resolved_sigma_db, tier=mic_tier, grid_hz=grid_hz),
        ),
        EnvelopeTerm(
            ReasonCode.LIMITED_BY_CLASS_PRIOR,
            class_prior_limit(grid_hz, driver_class=driver_class),
        ),
    ]
    if stability_evidence is not None and stability_evidence[0]:
        cloud_band_spread, cloud_n_positions = stability_evidence
        smoothed_terms.append(
            EnvelopeTerm(
                ReasonCode.LIMITED_BY_POSITION_STABILITY,
                position_stability_limit(
                    grid_hz,
                    cloud_band_spread,
                    n_positions=cloud_n_positions,
                    tier=mic_tier,
                ),
            )
        )

    term_specs: tuple[EnvelopeTerm, ...] = tuple(smoothed_terms)
    spatially_excluded_mask = np.zeros_like(grid_hz, dtype=bool)
    if excluded_bands_hz:
        spatial_term = EnvelopeTerm(
            ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION,
            spatial_exclusion_limit(grid_hz, excluded_bands_hz),
        )
        # Read back off the term rather than rasterizing twice.
        spatially_excluded_mask = spatial_term.depth_db <= 0.0
        term_specs = (*term_specs, spatial_term)
    stacked = np.stack([term.depth_db for term in term_specs])
    winning_value = np.min(stacked, axis=0)
    winning_index = np.argmin(stacked, axis=0)
    codes_by_index = [term.code for term in term_specs]

    at_sentinel = np.isclose(winning_value, ENVELOPE_CEILING_SENTINEL_DB)
    n_bins = len(grid_hz)
    pre_smoothing_reason = tuple(
        ReasonCode.FITTED if at_sentinel[i] else codes_by_index[int(winning_index[i])]
        for i in range(n_bins)
    )
    final_reason = tuple(
        ReasonCode.OUT_OF_BAND if not in_band_mask[i] else pre_smoothing_reason[i]
        for i in range(n_bins)
    )

    # The spatial exclusion is applied to the smoothing result, not blended
    # into it; see the docstring.
    smoothable_value = np.min(
        np.stack([term.depth_db for term in smoothed_terms]), axis=0
    )
    # An exact zero from any term is a hard boundary, like OUT_OF_BAND: without
    # this mask the ladder window blurs neighbouring in-band depth back across
    # it, putting the fit band's top edge above what the mic resolves. `<= 0.0`
    # rather than isclose: a genuinely tiny but non-zero permission keeps it.
    hard_zero_mask = smoothable_value <= 0.0
    masked_depth_db = np.where(in_band_mask, smoothable_value, 0.0)
    smoothed_depth_db = _ladder_smooth(grid_hz, masked_depth_db)
    smoothed_depth_db = np.where(
        in_band_mask & ~spatially_excluded_mask & ~hard_zero_mask,
        smoothed_depth_db,
        0.0,
    )

    terms_map: Mapping[ReasonCode, np.ndarray] = {
        term.code: term.depth_db for term in term_specs
    }

    return EnvelopeCurve(
        role=role,
        freqs_hz=grid_hz,
        allowed_depth_db=smoothed_depth_db,
        reason=final_reason,
        terms=terms_map,
        sigma_db=resolved_sigma_db,
        n_repeats=len(occurrences) - 1,
        mic_tier=mic_tier,
        driver_class=driver_class,
    )
