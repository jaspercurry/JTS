"""Correction strategy and target-profile orchestration.

This module is the boundary between "raw measurement math" and
"product policy." The PEQ engine stays small and deterministic; this
layer chooses bounded presets, calls the engine, and records an audit
trail explaining what happened. Future assistant code should consume
these structured reports instead of reverse-engineering PEQ choices
from plots.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from . import peq, target


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
        description="Flat target exposed as the middle target preset.",
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


CORRECTION_STRATEGIES: dict[str, CorrectionStrategy] = {
    "safe": CorrectionStrategy(
        strategy_id="safe",
        label="Safe",
        description=(
            "Conservative cuts-only modal correction for first-time runs "
            "and lower-confidence measurements."
        ),
        f_low_hz=25.0,
        f_high_hz=250.0,
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
        f_high_hz=350.0,
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
        f_high_hz=500.0,
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


def correction_strategy_options() -> list[dict[str, Any]]:
    return [strategy.to_dict() for strategy in CORRECTION_STRATEGIES.values()]


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
) -> list[dict[str, Any]]:
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
        audit.append({
            "index": idx,
            "freq_hz": filt.freq,
            "q": filt.q,
            "gain_db": filt.gain,
            "action": action,
            "residual_before_db": before,
            "residual_after_db": after,
            "local_improvement_db": abs(before) - abs(after),
            "rationale": rationale,
        })
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

    `peq.design_peq` has a per-filter boost cap, but a greedy designer
    can stack several small boosts near one dip. Product policy owns
    the total headroom budget, so enforce it here before prediction,
    YAML emission, UI, and bundles see the filters.
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


def design_correction(
    measured_db: np.ndarray,
    freqs: np.ndarray,
    *,
    target_choice: str | None = None,
    strategy_choice: str | None = None,
) -> CorrectionDesign:
    """Design PEQs and return an assistant-readable audit report."""
    target_profile = resolve_target_profile(target_choice)
    correction_strategy = resolve_correction_strategy(strategy_choice)
    target_db = target_profile.curve_db(freqs)

    raw_filters = peq.design_peq(
        measured_db,
        target_db,
        freqs,
        f_low=correction_strategy.f_low_hz,
        f_high=correction_strategy.f_high_hz,
        max_filters=correction_strategy.max_filters,
        max_cut_db=correction_strategy.max_cut_db,
        max_boost_db=correction_strategy.max_boost_db,
        cuts_only=correction_strategy.cuts_only,
        flatness_target_db=correction_strategy.flatness_target_db,
        q_min=correction_strategy.q_min,
        q_max=correction_strategy.q_max,
        min_filter_gain_db=correction_strategy.min_filter_gain_db,
    )
    filters = _enforce_total_boost_cap(raw_filters, correction_strategy)
    predicted_shift = peq.predicted_response(filters, freqs)
    predicted_db = measured_db + predicted_shift

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
    raw_total_boost = peq.total_max_boost_db(raw_filters)
    final_total_boost = peq.total_max_boost_db(filters)
    if raw_total_boost > final_total_boost + 1e-6:
        warnings.append({
            "code": "boosts_capped",
            "severity": "info",
            "message": (
                f"Positive boosts were capped from {raw_total_boost:.1f} dB "
                f"to {final_total_boost:.1f} dB to preserve headroom."
            ),
        })

    report = {
        "target_profile": target_profile.to_dict(),
        "correction_strategy": correction_strategy.to_dict(),
        "band_hz": [
            correction_strategy.f_low_hz,
            correction_strategy.f_high_hz,
        ],
        "before": before_metrics,
        "after": after_metrics,
        "improvement": {
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
        ),
        "warnings": warnings,
    }
    return CorrectionDesign(
        target_profile=target_profile,
        strategy=correction_strategy,
        target_db=target_db,
        peqs=filters,
        predicted_db=predicted_db,
        report=report,
    )
