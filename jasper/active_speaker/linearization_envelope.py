# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The correction envelope: how many dB of correction depth each frequency
bin is allowed.

Pure computation only: no I/O, no product policy, no CamillaDSP/emission
imports. Not :mod:`jasper.correction.envelope` or
:mod:`jasper.active_speaker.crossover_envelope_v2` — those are *screen*
envelopes (wizard UI state). Implements
docs/active-speaker-tuning-layers-design.md "The correction envelope":
``allowed_depth(f) = min(mic_trust_limit, repeatability_limit,
class_prior_limit)``, plus two optional cloud-derived terms
(``spatial_exclusion_limit``, ``position_stability_limit``) that are
NARROWING ONLY and default to absent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse
from jasper.audio_measurement.spatial_combine import BandSpread

from ._common import DRIVER_CLASSES


class ReasonCode(StrEnum):
    """Per-bin honesty-guard vocabulary — why a bin's allowed depth is what
    it is. Wider than what this module produces:
    ``LIMITED_BY_VERIFY_DIVERGENCE`` is reserved (nothing emits it yet) and
    ``BEYOND_MEASUREMENT_CONFIDENCE`` is emitted by the FIT layer — both
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


# Closed vocabulary (design doc "Microphone doctrine").
MIC_TIERS: tuple[str, ...] = ("reference", "consumer", "phone")

# Shared working grid: log spacing from 150 Hz (the design doc's gated-
# measurement validity floor) to 20 kHz. 176 points is >=4x finer than the
# smoothing ladder's finest step (1/6 oct).
DEFAULT_ENVELOPE_GRID_HZ: np.ndarray = np.geomspace(150.0, 20_000.0, 176)
# Read-only: a frozen-dataclass default holding a live reference would let
# an in-place mutation corrupt every caller.
DEFAULT_ENVELOPE_GRID_HZ.flags.writeable = False

# dB ceiling for every term below. NOT a policy number: the real ceiling is
# the wiring layer's cut/boost caps (-12 dB / +6 dB).
ENVELOPE_CEILING_SENTINEL_DB: float = 24.0

# sigma_tolerable(tier), dB — design doc "Cold-start priors".
_SIGMA_TOLERABLE_DB: Mapping[str, float] = {
    "reference": 0.5,
    "consumer": 1.0,
    "phone": 1.5,
}

# mic_trust_limit's (full_to_hz, taper_zero_hz) by tier — design doc's
# canonical table.
_MIC_TRUST_TABLE_HZ: Mapping[str, tuple[float, float]] = {
    "reference": (12_000.0, 20_000.0),
    "consumer": (6_000.0, 12_000.0),
    "phone": (3_000.0, 8_000.0),
}

# class_prior_limit's full_to_hz by driver class, Hz — artifact 02 §5's
# table. taper_zero is DERIVED as full_to * 2 (a heuristic, not researched).
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
    """1/6 oct below 4 kHz, 1/3 oct to 10 kHz, 1/2 oct above, hard-stitched.
    Mirrors compute_sigma.py's ``ladder_smooth_loggrid``."""
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
    occurrences, on ``grid_hz``. Each occurrence is smoothed then centered
    to its own mean over ``valid_band_hz``; sigma is the ddof=1 std across
    those. ``None`` when fewer than 2 occurrences exist or
    ``valid_band_hz`` doesn't overlap ``grid_hz``.
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
    # np.std(..., ddof=1) on a single row returns NaN, not an exception; the
    # len < 2 check above is what stops an N=1 capture reaching here.
    return np.std(stack, axis=0, ddof=1)


def _sigma_to_depth_db(sigma_db: np.ndarray, sigma_tolerable_db: float) -> np.ndarray:
    """``ceiling . min(1, sigma_tolerable / max(sigma, eps))`` — this
    module's ONE sigma-to-allowed-depth mapping, shared by
    :func:`repeatability_limit` and :func:`position_stability_limit`.
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
    """``D_cap(tier) . min(1, sigma_tolerable(tier) / max(sigma, eps))``,
    see :func:`_sigma_to_depth_db`. ``sigma_db=None`` returns ALL-ZERO:
    absence of evidence is the tightest constraint, never a pass-through.
    """
    _validate_tier(tier)
    if sigma_db is None:
        return np.zeros_like(grid_hz, dtype=np.float64)
    return _sigma_to_depth_db(sigma_db, _SIGMA_TOLERABLE_DB[tier])


def mic_trust_limit(freqs_hz: np.ndarray, *, tier: str) -> np.ndarray:
    """Flat at the ceiling sentinel to the tier's ``full_to``, octave-linear
    taper to 0 at ``taper_zero``, 0 above. Pairs from
    :data:`_MIC_TRUST_TABLE_HZ`.
    """
    _validate_tier(tier)
    full_to_hz, taper_zero_hz = _MIC_TRUST_TABLE_HZ[tier]
    return _flat_then_taper(freqs_hz, full_to_hz, taper_zero_hz)


def class_prior_limit(freqs_hz: np.ndarray, *, driver_class: str) -> np.ndarray:
    """Flat at the ceiling sentinel to the driver class's ``full_to``,
    octave-linear taper to 0 at ``taper_zero = full_to * 2``, 0 above. See
    :data:`_CLASS_PRIOR_FULL_TO_HZ`.
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
    """Rasterize frequency intervals onto ``freqs_hz`` — True where a bin's
    own cell (between geometric midpoints to its neighbours) overlaps any
    interval; ANY overlap excludes, since a "centre inside" rule would
    leave a bin 90% inside an identified null fully correctable. Intervals
    need not be sorted or disjoint; a descending pair raises.
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
        # No neighbour for a midpoint; the cell degenerates to the point.
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
    """Zero allowed depth on honesty-masked bins, the ceiling sentinel
    elsewhere. ``excluded_bands_hz`` is the merged honesty mask (combiner's
    power-vs-median screen union the identified-null registry) as
    frequency intervals, since the two producers live on the combiner's
    grid and this module on its own. See :func:`_interval_mask`.
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
    """Shrink allowed depth where the cloud's positions disagree about a
    band's level. Hands :func:`_sigma_to_depth_db` the STANDARD ERROR of the
    combined level, ``sigma_db / sqrt(n_positions)`` (the fit corrects the
    cloud's mean, so the bound belongs on that mean's uncertainty).
    ``sigma_db``, not ``max_sigma_db`` — the level spread, not the
    structure spread (comb nulls, owned by :func:`spatial_exclusion_limit`).
    Bands the cloud did not report leave their bins at the sentinel (a
    coverage fact already bounded elsewhere). Overlapping bands take the
    larger standard error. ``n_positions < 2`` raises.
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

    ``reason`` is the PRE-smoothing argmin, one code per bin. ``terms`` holds
    every term's full unmasked curve; its KEY SET VARIES — the two cloud
    terms appear only when the caller supplied their evidence. ``n_repeats``
    always reports ``primary``'s own occurrence count.
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
# three states that parameter distinguishes, so unsetness needs a third
# value. Checked with `is` only.
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

    In-band = ``excited_band_hz`` intersected with
    ``[conservative_validity_floor_hz, grid top]`` — the HIGHEST
    ``validity_floor_hz`` across primary and repeats (+inf if none has
    one). A bin at the ceiling sentinel is reported as
    :attr:`ReasonCode.FITTED` rather than an arbitrary tie-break; a term at
    exactly 0 is a hard boundary the smoothing pass may not blur across.

    The spatial exclusion is applied AFTER smoothing —
    ``min(ladder_smooth(min(other terms)), exclusion)`` — so zeroing before
    smoothing would blur those zeros outward and cost the comb peaks
    between identified nulls.

    ``sigma_db`` is a tri-state seam: unset computes from ``primary``'s
    repeats, an ndarray is used verbatim, explicit ``None`` forces "no
    repeatability evidence". Cloud arguments default to absent (term not
    added); ``band_spread`` and ``n_positions`` must come together.
    """
    _validate_tier(mic_tier)
    _validate_driver_class(driver_class)

    # Resolved once so term construction below cannot reach a half-supplied
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

    # The three original terms keep their order (argmin tie-break).
    # Position stability joins the SMOOTHED group; spatial exclusion does not.
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
    # An exact zero from any term is a hard boundary, like OUT_OF_BAND: the
    # ladder window would otherwise blur it into neighbouring depth. `<= 0.0`,
    # not isclose, so a tiny but non-zero permission keeps it.
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
