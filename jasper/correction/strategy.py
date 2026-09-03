# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction strategy and target-profile orchestration.

The boundary between raw measurement math and product policy: the PEQ engine
stays small and deterministic, this layer chooses bounded presets, calls the
engine, and records the audit trail consumers should read instead of
reverse-engineering PEQ choices.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from jasper.audio_measurement import peq
from jasper.audio_measurement.room_boundary import (
    ROOM_BOUNDARY_DEFAULT_HZ,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
)

from . import spatial, target, variance_cap

# Crossover-region no-boost half-width, in octaves. A BOOST centred within
# ±(this) of the bass-management corner Fc is excluded: an LR4 sum is flat
# there by design, so a measured dip at the corner is usually phase/placement.
# Cuts near Fc are still allowed. One-third octave is the conventional width.
CROSSOVER_NO_BOOST_HALF_WIDTH_OCT = 1.0 / 3.0

# Float slack for "is this cut already inside its allowance?". Not a policy
# width; it stops the relaxation below rewriting a bit-identical gain forever.
_DEPTH_CLAMP_EPS_DB = 1e-9


@dataclass(frozen=True)
class TargetProfile:
    """A user-facing target curve choice."""

    target_id: str
    label: str
    description: str
    warmth: float | None

    def curve_db(self, freqs: np.ndarray) -> np.ndarray:
        if self.warmth is None:
            return target.flat_target(freqs)
        return target.house_curve(freqs, warmth=self.warmth)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "label": self.label,
            "description": self.description,
            "warmth": self.warmth,
        }


@dataclass(frozen=True)
class CorrectionStrategy:
    """Bounded PEQ policy for room correction."""

    strategy_id: str
    label: str
    description: str
    f_low_hz: float
    f_high_hz: float
    max_filters: int
    max_cut_db: float
    max_boost_db: float
    cuts_only: bool
    flatness_target_db: float
    q_min: float
    q_max: float
    min_filter_gain_db: float
    max_total_boost_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "label": self.label,
            "description": self.description,
            "f_low_hz": self.f_low_hz,
            "f_high_hz": self.f_high_hz,
            "max_filters": self.max_filters,
            "max_cut_db": self.max_cut_db,
            "max_boost_db": self.max_boost_db,
            "cuts_only": self.cuts_only,
            "flatness_target_db": self.flatness_target_db,
            "q_min": self.q_min,
            "q_max": self.q_max,
            "min_filter_gain_db": self.min_filter_gain_db,
            "max_total_boost_db": self.max_total_boost_db,
        }


@dataclass(frozen=True)
class CorrectionDesign:
    """Output of one strategy run."""

    target_profile: TargetProfile
    strategy: CorrectionStrategy
    target_db: np.ndarray
    peqs: list[peq.PEQ]
    predicted_db: np.ndarray
    report: dict[str, Any]


TARGET_PROFILES: dict[str, TargetProfile] = {
    "flat": TargetProfile(
        target_id="flat",
        label="Flat",
        description="Neutral reference target with no house-curve tilt.",
        warmth=None,
    ),
    "neutral": TargetProfile(
        target_id="neutral",
        label="Neutral",
        description="Zero-tilt house curve kept as a distinct named profile.",
        warmth=0.0,
    ),
    "warm": TargetProfile(
        target_id="warm",
        label="Warm",
        description="Moderate Harman-style downward tilt and bass shelf.",
        warmth=0.7,
    ),
    "bright": TargetProfile(
        target_id="bright",
        label="Bright",
        description="Slight inverse tilt for listeners who prefer more presence.",
        warmth=-0.3,
    ),
}


# Every strategy's upper band edge is expressed against the room-correction
# boundary SSOT (jasper.audio_measurement.room_boundary), never as its own
# literal: safe -> MIN, balanced -> DEFAULT, assertive -> MAX (250 / 350 /
# 500 Hz today). assertive's equality is an OPEN RC3 decision, not a settled
# one — see docs/room-correction-regime-plan.md D1. These bind the SSOT's
# static module-level values at import time, so a per-room f_t requires
# changing this composition.
CORRECTION_STRATEGIES: dict[str, CorrectionStrategy] = {
    "safe": CorrectionStrategy(
        strategy_id="safe",
        label="Safe",
        description=(
            "Conservative cuts-only modal correction for first-time runs "
            "and lower-confidence measurements."
        ),
        f_low_hz=25.0,
        f_high_hz=ROOM_BOUNDARY_MIN_HZ,
        max_filters=4,
        max_cut_db=-6.0,
        max_boost_db=0.0,
        cuts_only=True,
        flatness_target_db=1.5,
        q_min=1.0,
        q_max=6.0,
        min_filter_gain_db=0.75,
        max_total_boost_db=0.0,
    ),
    "balanced": CorrectionStrategy(
        strategy_id="balanced",
        label="Balanced",
        description=(
            "Default cuts-only modal correction. Matches the original JTS "
            "PEQ policy while making the policy explicit."
        ),
        f_low_hz=20.0,
        f_high_hz=ROOM_BOUNDARY_DEFAULT_HZ,
        max_filters=5,
        max_cut_db=-10.0,
        max_boost_db=3.0,
        cuts_only=True,
        flatness_target_db=1.0,
        q_min=1.0,
        q_max=8.0,
        min_filter_gain_db=0.5,
        max_total_boost_db=0.0,
    ),
    "assertive": CorrectionStrategy(
        strategy_id="assertive",
        label="Assertive",
        description=(
            "Power-user mode with more filters and small bounded boosts. "
            "Use only with calibrated, repeatable measurements."
        ),
        f_low_hz=20.0,
        f_high_hz=ROOM_BOUNDARY_MAX_HZ,
        max_filters=8,
        max_cut_db=-12.0,
        max_boost_db=2.0,
        cuts_only=False,
        flatness_target_db=0.8,
        q_min=0.7,
        q_max=10.0,
        min_filter_gain_db=0.5,
        max_total_boost_db=3.0,
    ),
}


DEFAULT_TARGET_PROFILE_ID = "flat"
DEFAULT_CORRECTION_STRATEGY_ID = "balanced"
HOUSEHOLD_CORRECTION_STRATEGY_IDS = ("safe", DEFAULT_CORRECTION_STRATEGY_ID)


def resolve_target_profile(target_id: str | None) -> TargetProfile:
    if target_id in TARGET_PROFILES:
        return TARGET_PROFILES[str(target_id)]
    return TARGET_PROFILES[DEFAULT_TARGET_PROFILE_ID]


def resolve_correction_strategy(strategy_id: str | None) -> CorrectionStrategy:
    if strategy_id in CORRECTION_STRATEGIES:
        return CORRECTION_STRATEGIES[str(strategy_id)]
    return CORRECTION_STRATEGIES[DEFAULT_CORRECTION_STRATEGY_ID]


def target_profile_options() -> list[dict[str, Any]]:
    return [profile.to_dict() for profile in TARGET_PROFILES.values()]


def household_correction_strategy_options() -> list[dict[str, Any]]:
    """Correction strategies authorized on the ordinary household surface."""
    return [
        CORRECTION_STRATEGIES[strategy_id].to_dict()
        for strategy_id in HOUSEHOLD_CORRECTION_STRATEGY_IDS
    ]


def _band_mask(freqs: np.ndarray, strategy: CorrectionStrategy) -> np.ndarray:
    return (freqs >= strategy.f_low_hz) & (freqs <= strategy.f_high_hz)


def _residual_metrics(
    residual_db: np.ndarray,
    freqs: np.ndarray,
    strategy: CorrectionStrategy,
) -> dict[str, Any]:
    band = _band_mask(freqs, strategy)
    if not band.any():
        return {
            "rms_db": 0.0,
            "max_abs_db": 0.0,
            "max_peak_db": 0.0,
            "deepest_null_db": 0.0,
            "n_points": 0,
        }
    band_residual = residual_db[band]
    return {
        "rms_db": float(np.sqrt(np.mean(band_residual ** 2))),
        "max_abs_db": float(np.max(np.abs(band_residual))),
        "max_peak_db": float(np.max(band_residual)),
        "deepest_null_db": float(np.min(band_residual)),
        "n_points": int(band.sum()),
    }


def _dominant_residuals(
    residual_db: np.ndarray,
    freqs: np.ndarray,
    strategy: CorrectionStrategy,
    *,
    limit: int = 5,
) -> dict[str, list[dict[str, float]]]:
    band = _band_mask(freqs, strategy)
    entries = [
        (float(freq), float(residual))
        for freq, residual in zip(freqs[band], residual_db[band], strict=False)
    ]
    peaks = sorted(entries, key=lambda item: item[1], reverse=True)
    nulls = sorted(entries, key=lambda item: item[1])
    return {
        "peaks": [
            {"freq_hz": freq, "residual_db": residual}
            for freq, residual in peaks[:limit]
            if residual > 0
        ],
        "nulls": [
            {"freq_hz": freq, "residual_db": residual}
            for freq, residual in nulls[:limit]
            if residual < 0
        ],
    }


def _filter_audit(
    filters: list[peq.PEQ],
    *,
    before_residual_db: np.ndarray,
    after_residual_db: np.ndarray,
    freqs: np.ndarray,
    spatial_matrix: spatial.SpatialMatrix | None = None,
    spatial_error: str | None = None,
) -> list[dict[str, Any]]:
    # The caller builds the spatial matrix ONCE for both this audit and the
    # variance depth cap. `build_spatial_matrix` returns exactly one of
    # (matrix, error) non-None, so no third "were positions supplied" flag.
    spatial_requested = spatial_matrix is not None or spatial_error is not None
    audit: list[dict[str, Any]] = []
    for idx, filt in enumerate(filters, start=1):
        nearest = int(np.argmin(np.abs(freqs - filt.freq)))
        before = float(before_residual_db[nearest])
        after = float(after_residual_db[nearest])
        action = "cut_peak" if filt.gain < 0 else "boost_dip"
        if action == "cut_peak":
            rationale = (
                f"Cut a +{max(before, 0.0):.1f} dB residual peak near "
                f"{filt.freq:.1f} Hz."
            )
        else:
            rationale = (
                f"Added a bounded boost for a {before:.1f} dB dip near "
                f"{filt.freq:.1f} Hz."
            )
        entry: dict[str, Any] = {
            "index": idx,
            "freq_hz": filt.freq,
            "q": filt.q,
            "gain_db": filt.gain,
            "action": action,
            "residual_before_db": before,
            "residual_after_db": after,
            # PREDICTED residual change at this filter's frequency (positive =
            # the model expects the residual to shrink). Named "predicted", not
            # "improvement", per the report-level honesty rule.
            "local_predicted_delta_db": abs(before) - abs(after),
            "rationale": rationale,
        }
        if spatial_requested:
            if spatial_matrix is None:
                entry["spatial_confidence"] = {
                    "available": False,
                    "reason": spatial_error,
                }
            else:
                entry["spatial_confidence"] = spatial.point_summary(
                    spatial_matrix,
                    freq_hz=filt.freq,
                )
        audit.append(entry)
    return audit


def _warnings(
    filters: list[peq.PEQ],
    before_metrics: dict[str, Any],
    after_metrics: dict[str, Any],
    strategy: CorrectionStrategy,
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if not filters and before_metrics["max_abs_db"] >= 3.0:
        warnings.append({
            "code": "no_filters_for_residual",
            "severity": "warn",
            "message": (
                "Residual error remains but no filter passed the current "
                "strategy bounds."
            ),
        })
    if before_metrics["deepest_null_db"] <= -6.0 and strategy.cuts_only:
        warnings.append({
            "code": "nulls_not_boosted",
            "severity": "info",
            "message": (
                "Deep nulls were detected and left unboosted. That is "
                "intentional in cuts-only room correction."
            ),
        })
    total_boost = peq.total_max_boost_db(filters)
    if total_boost > strategy.max_total_boost_db + 1e-6:
        warnings.append({
            "code": "boost_headroom_exceeded",
            "severity": "warn",
            "message": (
                f"Predicted stacked boost is {total_boost:.1f} dB, above "
                f"the strategy cap of {strategy.max_total_boost_db:.1f} dB."
            ),
        })
    if after_metrics["max_abs_db"] > before_metrics["max_abs_db"] + 0.5:
        warnings.append({
            "code": "prediction_worse",
            "severity": "warn",
            "message": "Predicted correction increased max residual error.",
        })
    if not strategy.cuts_only:
        warnings.append({
            "code": "boosts_enabled",
            "severity": "info",
            "message": (
                "This strategy allows limited boosts. Use calibrated, "
                "repeatable measurements and preserve headroom."
            ),
        })
    return warnings


def _enforce_total_boost_cap(
    filters: list[peq.PEQ],
    strategy: CorrectionStrategy,
) -> list[peq.PEQ]:
    """Clamp cumulative positive gain for boost-capable strategies.

    `peq.design_peq` caps per filter, but a greedy designer can stack several
    small boosts near one dip; the total headroom budget is product policy and
    is enforced here, before prediction, YAML emission, UI and bundles.
    """
    if strategy.cuts_only:
        return [f for f in filters if f.gain <= 0]
    if strategy.max_total_boost_db <= 0:
        return [f for f in filters if f.gain <= 0]

    capped: list[peq.PEQ] = []
    used_boost = 0.0
    for filt in filters:
        if filt.gain <= 0:
            capped.append(filt)
            continue
        remaining = strategy.max_total_boost_db - used_boost
        if remaining < strategy.min_filter_gain_db:
            continue
        gain = min(filt.gain, remaining)
        capped.append(peq.PEQ(freq=filt.freq, q=filt.q, gain=gain))
        used_boost += gain
    return capped


def _variance_cap_warning(
    disclosure: variance_cap.VarianceCapDisclosure,
) -> dict[str, str]:
    """The household-facing nudge for a cross-position depth cap that acted.

    Says what the cap did — lowered the depth ALLOWED at some frequencies —
    and never claims a filter was removed. Only called when
    ``disclosure.active``, so the measurement fields it formats are populated.
    """
    forgone_db = disclosure.max_depth_forgone_db or 0.0
    where = (
        f" near {disclosure.worst_freq_hz:.0f} Hz"
        if disclosure.worst_freq_hz is not None else ""
    )
    # Ceiling register throughout: "allowed none" is what the spread decided.
    # A neighbouring filter's skirt still reaches these frequencies.
    none_at_all = (
        f" At {disclosure.n_bins_no_cut} of them it allowed none at all."
        if disclosure.n_bins_no_cut else ""
    )
    return {
        "code": "spatial_variance_capped_depth",
        "severity": "info",
        "message": (
            f"Your seats disagreed about the level at {disclosure.n_bins_capped} "
            f"of {disclosure.n_bins} frequencies in the correction band, so less "
            f"correction depth was allowed there — up to {forgone_db:.1f} dB "
            f"less{where}.{none_at_all} A level that changes that much from seat "
            "to seat isn't one room feature a filter can fix; correcting it for "
            "the average would be wrong at every seat."
        ),
    }


def _enforce_variance_depth_cap(
    filters: list[peq.PEQ],
    depth_cap_db: np.ndarray | None,
    freqs: np.ndarray,
    strategy: CorrectionStrategy,
) -> tuple[list[peq.PEQ], int]:
    """Hold the REALIZED chain to the spread's allowance at PROTECTED centres.

    ``peq.design_peq``'s per-bin ``max_cut_db`` bounds one filter, but a greedy
    designer can stack several at nearly the same frequency and restore the
    depth the cap refused. Returns ``(filters, n_trimmed)``, counting filters
    shallowed or dropped; a ``None`` cap is a no-op. Pure.

    Only PROTECTED centres are constrained (``variance_cap.protected_mask``
    owns that definition). Each constraint is ``own_gain >= allowance -
    others``, where ``others`` is every other surviving filter's predicted
    response at that centre, so it is solved by relaxation: every update makes
    a gain less negative, which only loosens other constraints, and passes are
    capped at one per filter plus a confirming pass.

    Bins BETWEEN centres are not bound — a bell inevitably spills into its
    neighbours — so a residue can land off-centre. The caller measures it per
    design (``variance_cap.realized_overshoot_db``) and publishes it as
    ``max_overshoot_db``; that per-design number is the protection. A 3360-design
    search found worst-so-far 11.625 dB (safe) / 11.474 (balanced) / 9.231
    (assertive), median 0.000 — worst-found on a finite grid, NEVER a bound.
    """
    if depth_cap_db is None:
        return filters, 0

    # Freeze each cut's allowance once: interpolated at its own centre, and
    # None wherever the spread left that centre alone (never constrained).
    allowances: list[float | None] = []
    for filt in filters:
        if filt.gain >= 0:
            allowances.append(None)
            continue
        allowed_db = float(np.interp(filt.freq, freqs, depth_cap_db))
        constrained = bool(
            variance_cap.protected_mask(
                allowed_db, base_max_cut_db=strategy.max_cut_db,
            )
        )
        allowances.append(allowed_db if constrained else None)

    working: list[peq.PEQ | None] = list(filters)
    for _ in range(len(filters) + 1):
        changed = False
        for idx, centre_allowance_db in enumerate(allowances):
            current = working[idx]
            if centre_allowance_db is None or current is None:
                continue
            at_centre = np.asarray([current.freq], dtype=np.float64)
            others = [f for j, f in enumerate(working) if f is not None and j != idx]
            others_db = float(peq.predicted_response(others, at_centre)[0])
            # Deepest this filter may go with the CHAIN still inside the
            # allowance here (a bell equals its own gain at its own centre).
            floor_db = centre_allowance_db - others_db
            if current.gain >= floor_db - _DEPTH_CLAMP_EPS_DB:
                continue
            if floor_db > -strategy.min_filter_gain_db:
                working[idx] = None
            else:
                working[idx] = peq.PEQ(
                    freq=current.freq, q=current.q, gain=floor_db,
                )
            changed = True
        if not changed:
            break

    trimmed = sum(
        1 for before, after in zip(filters, working, strict=True)
        if after is None or after.gain != before.gain
    )
    return [f for f in working if f is not None], trimmed


def _crossover_no_boost_band_hz(crossover_hz: float) -> tuple[float, float]:
    """The ``(lo, hi)`` Hz band, ±1/3 octave around Fc, where boosts are excluded."""
    factor = 2.0 ** CROSSOVER_NO_BOOST_HALF_WIDTH_OCT
    return crossover_hz / factor, crossover_hz * factor


def _exclude_boosts_near_crossover(
    filters: list[peq.PEQ],
    crossover_hz: float | None,
) -> tuple[list[peq.PEQ], list[dict[str, float]]]:
    """Drop BOOST filters whose center is within ±1/3 octave of ``crossover_hz``.

    Returns ``(kept_filters, excluded)`` where ``excluded`` is a list of
    ``{freq_hz, gain_db}`` for the audit report. Cuts are never excluded — a
    real peak at the corner is still corrected. A ``None`` corner is a no-op.
    """
    if crossover_hz is None or not math.isfinite(crossover_hz) or crossover_hz <= 0:
        return filters, []
    lo, hi = _crossover_no_boost_band_hz(crossover_hz)
    kept: list[peq.PEQ] = []
    excluded: list[dict[str, float]] = []
    for filt in filters:
        if filt.gain > 0 and lo <= filt.freq <= hi:
            excluded.append({"freq_hz": float(filt.freq), "gain_db": float(filt.gain)})
            continue
        kept.append(filt)
    return kept, excluded


def design_correction(
    measured_db: np.ndarray,
    freqs: np.ndarray,
    *,
    target_choice: str | None = None,
    strategy_choice: str | None = None,
    position_magnitudes: list[np.ndarray] | None = None,
    crossover_hz: float | None = None,
) -> CorrectionDesign:
    """Design PEQs and return an assistant-readable audit report.

    ``crossover_hz`` is the ACTIVE bass-management corner (Hz), or ``None``.
    The designer only READS it — the speaker layer owns the corner — and
    excludes boosts within ±1/3 octave of it.

    ``position_magnitudes`` are the per-seat curves behind ``measured_db``'s
    spatial average: they annotate each filter's repeatability and BOUND the
    depth designed at each frequency (see
    :mod:`jasper.correction.variance_cap`). Fewer than two positions designs
    exactly as before and says so in the report.
    """
    target_profile = resolve_target_profile(target_choice)
    correction_strategy = resolve_correction_strategy(strategy_choice)
    target_db = target_profile.curve_db(freqs)

    # Build the cross-position stack ONCE; the depth cap and the filter audit
    # both read it. `spatial` is the single writer of this layer's spread.
    spatial_matrix: spatial.SpatialMatrix | None = None
    spatial_error: str | None = None
    depth_cap_db: np.ndarray | None = None
    cap_disclosure: variance_cap.VarianceCapDisclosure | None = None
    if position_magnitudes is not None:
        spatial_matrix, spatial_error = spatial.build_spatial_matrix(
            position_magnitudes, freqs,
        )
        planned_cap_db, cap_disclosure = variance_cap.plan_depth_cap(
            spatial_matrix,
            base_max_cut_db=correction_strategy.max_cut_db,
            min_filter_gain_db=correction_strategy.min_filter_gain_db,
            band_mask=_band_mask(freqs, correction_strategy),
            unavailable_reason=spatial_error,
        )
        # Gate on `active`, not merely availability: a run in which the spread
        # capped nothing in band takes the identical pre-cap path.
        if cap_disclosure.active:
            depth_cap_db = planned_cap_db

    # A frequency the spread allows no usable correction at is withheld from
    # the designer's search rather than offered at zero depth: design_peq STOPS
    # when its tallest remaining peak clamps below min_filter_gain_db. Only the
    # curve the DESIGNER sees is masked; every residual the report states is
    # still measured against the real `measured_db`. Unreachable for a real
    # room — a bin qualifies only above 48 dB (safe) / 120 dB (balanced) /
    # 144 dB (assertive) of sigma. A withheld bin is ZEROED in the residual,
    # not excluded, so design_peq's "flat enough" stop keeps its bin count.
    design_measured_db = measured_db
    if depth_cap_db is not None:
        withheld = variance_cap.no_correction_mask(
            depth_cap_db,
            min_filter_gain_db=correction_strategy.min_filter_gain_db,
        )
        if withheld.any():
            design_measured_db = np.where(withheld, target_db, measured_db)

    raw_filters = peq.design_peq(
        design_measured_db,
        target_db,
        freqs,
        f_low=correction_strategy.f_low_hz,
        f_high=correction_strategy.f_high_hz,
        max_filters=correction_strategy.max_filters,
        max_cut_db=(
            correction_strategy.max_cut_db if depth_cap_db is None else depth_cap_db
        ),
        max_boost_db=correction_strategy.max_boost_db,
        cuts_only=correction_strategy.cuts_only,
        flatness_target_db=correction_strategy.flatness_target_db,
        q_min=correction_strategy.q_min,
        q_max=correction_strategy.q_max,
        min_filter_gain_db=correction_strategy.min_filter_gain_db,
    )
    capped = _enforce_total_boost_cap(raw_filters, correction_strategy)
    # Read the active crossover corner (never pick it) and forbid boosts inside
    # the crossover region. AFTER the boost cap, so the excluded set is final.
    boosts_settled, crossover_excluded_boosts = _exclude_boosts_near_crossover(
        capped, crossover_hz
    )
    # The variance depth clamp runs LAST, on the settled chain: a per-filter
    # floor cannot bound a stack, and both stages above can REMOVE a boost,
    # which deepens the net near a protected centre. Safe in this order because
    # the clamp only makes cuts shallower or drops them.
    filters, depth_trimmed = _enforce_variance_depth_cap(
        boosts_settled, depth_cap_db, freqs, correction_strategy,
    )
    predicted_shift = peq.predicted_response(filters, freqs)
    predicted_db = measured_db + predicted_shift
    # Measured on the FINAL filter set, so no later stage can move the number
    # the report states. Gated on `available`, not presence: a run where no cap
    # could be planned measured nothing, and 0.0 would read as "measured and
    # clean". Available-but-inert does measure — the protected set is empty.
    if cap_disclosure is not None and cap_disclosure.available:
        cap_disclosure = cap_disclosure.with_enforcement(
            filters_depth_trimmed=depth_trimmed,
            max_overshoot_db=(
                variance_cap.realized_overshoot_db(
                    depth_cap_db,
                    predicted_shift,
                    base_max_cut_db=correction_strategy.max_cut_db,
                    band_mask=_band_mask(freqs, correction_strategy),
                )
                if depth_cap_db is not None else 0.0
            ),
        )

    before_residual = measured_db - target_db
    after_residual = predicted_db - target_db
    before_metrics = _residual_metrics(
        before_residual, freqs, correction_strategy,
    )
    after_metrics = _residual_metrics(
        after_residual, freqs, correction_strategy,
    )
    warnings = _warnings(
        filters,
        before_metrics,
        after_metrics,
        correction_strategy,
    )
    # Each warning owns exactly its own reduction: boosts_capped compares the
    # raw design against the PRE-exclusion capped list, while the crossover
    # annotation below owns the boosts the near-Fc rule removed. Comparing
    # against the final filters would misattribute exclusions to headroom.
    raw_total_boost = peq.total_max_boost_db(raw_filters)
    capped_total_boost = peq.total_max_boost_db(capped)
    if raw_total_boost > capped_total_boost + 1e-6:
        warnings.append({
            "code": "boosts_capped",
            "severity": "info",
            "message": (
                f"Positive boosts were capped from {raw_total_boost:.1f} dB "
                f"to {capped_total_boost:.1f} dB to preserve headroom."
            ),
        })
    # Present whenever a corner is being read, so verdict_text can distinguish
    # "that's your crossover, not a room mode". Only warns when a boost was
    # actually excluded there.
    crossover_region: dict[str, Any] | None = None
    if crossover_hz is not None and math.isfinite(crossover_hz) and crossover_hz > 0:
        lo, hi = _crossover_no_boost_band_hz(crossover_hz)
        crossover_region = {
            "corner_hz": float(crossover_hz),
            "no_boost_band_hz": [lo, hi],
            "excluded_boosts": crossover_excluded_boosts,
        }
        if crossover_excluded_boosts:
            warnings.append({
                "code": "crossover_region_dip_not_boosted",
                "severity": "info",
                "message": (
                    f"A dip near your {crossover_hz:.0f} Hz crossover was left "
                    "un-boosted on purpose — that's where your subwoofer and "
                    "speakers hand off, not a room mode. Boosting there fights "
                    "the crossover instead of fixing the room."
                ),
            })
    # The cross-position depth cap's own nudge — only when it actually lowered
    # the ceiling somewhere in band. It claims the CEILING, never the filters.
    if cap_disclosure is not None and cap_disclosure.active:
        warnings.append(_variance_cap_warning(cap_disclosure))

    report = {
        "target_profile": target_profile.to_dict(),
        "correction_strategy": correction_strategy.to_dict(),
        "band_hz": [
            correction_strategy.f_low_hz,
            correction_strategy.f_high_hz,
        ],
        "before": before_metrics,
        "after": after_metrics,
        # PREDICTED, not measured: `after` is measured+PEQ run through the
        # filter model, never re-measured. The measured before/after delta only
        # exists once a verify sweep lands (session.verify_before_after).
        "predicted": {
            "rms_db": before_metrics["rms_db"] - after_metrics["rms_db"],
            "max_abs_db": (
                before_metrics["max_abs_db"] - after_metrics["max_abs_db"]
            ),
            "filter_count": len(filters),
            "total_positive_boost_db": peq.total_max_boost_db(filters),
        },
        "dominant_residuals": _dominant_residuals(
            before_residual, freqs, correction_strategy,
        ),
        "filters": _filter_audit(
            filters,
            before_residual_db=before_residual,
            after_residual_db=after_residual,
            freqs=freqs,
            spatial_matrix=spatial_matrix,
            spatial_error=spatial_error,
        ),
        "warnings": warnings,
    }
    # Additive: only present when a corner is being read (no bass management ->
    # key absent), so old consumers are unaffected.
    if crossover_region is not None:
        report["crossover_region"] = crossover_region
    # Likewise additive: present whenever positions were supplied at all, so a
    # reader can tell "took nothing" from "could not run" from "no positions".
    if cap_disclosure is not None:
        report["spatial_variance_cap"] = cap_disclosure.to_dict()
    return CorrectionDesign(
        target_profile=target_profile,
        strategy=correction_strategy,
        target_db=target_db,
        peqs=filters,
        predicted_db=predicted_db,
        report=report,
    )
