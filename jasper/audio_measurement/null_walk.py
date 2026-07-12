# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared timing-locked null-walk decision primitive.

The player owns the clock-exact DSP mutation.  This module deliberately sees
only the candidate delay and repeated, gated null-depth measurements; impulse
arrival times are not part of its input vocabulary.  Active-speaker driver
alignment and bass-management sub-to-mains timing therefore share one bounded
search contract without sharing either subsystem's DSP or web orchestration.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Mapping, Sequence

MIN_CAPTURE_COUNT = 5
MIN_STEP_US = 50.0
MAX_STEP_US = 100.0
MAX_REPEAT_SPREAD_DB = 2.0
DEFAULT_SOUND_SPEED_M_S = 343.0
MAX_EXHAUSTIVE_CANDIDATES = 25
MAX_DSP_DELAY_US = 20_000.0


class NullWalkError(ValueError):
    """The walk specification or evidence violates the timing contract."""


def geometry_seed_us(
    signed_path_difference_m: Any,
    *,
    signed_transport_difference_us: Any = 0.0,
    sound_speed_m_s: Any = DEFAULT_SOUND_SPEED_M_S,
) -> float:
    """Convert signed geometry plus known transport into an a-priori seed.

    Both signed differences are ``negative target minus positive target``.
    A positive result therefore means the positive target needs that much DSP
    delay; a negative result means the negative target needs its absolute
    value. This estimate only bounds the walk; :func:`select_delay` emits the
    final measured candidate.
    """

    path = _finite(signed_path_difference_m, field="signed_path_difference_m")
    transport = _finite(
        signed_transport_difference_us,
        field="signed_transport_difference_us",
    )
    speed = _finite(sound_speed_m_s, field="sound_speed_m_s")
    if speed <= 0.0:
        raise NullWalkError("sound_speed_m_s must be positive")
    return path / speed * 1_000_000.0 + transport


def _finite(value: Any, *, field: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise NullWalkError(f"{field} must be numeric") from exc
    if not math.isfinite(out):
        raise NullWalkError(f"{field} must be finite")
    return out


@dataclass(frozen=True)
class NullWalkSpec:
    """A geometry-seeded, single-cycle-safe relative-delay search."""

    crossover_fc_hz: float
    geometry_seed_us: float
    positive_delay_target: str
    negative_delay_target: str
    step_us: float = MAX_STEP_US

    def __post_init__(self) -> None:
        fc = _finite(self.crossover_fc_hz, field="crossover_fc_hz")
        seed = _finite(self.geometry_seed_us, field="geometry_seed_us")
        step = _finite(self.step_us, field="step_us")
        positive_target = str(self.positive_delay_target).strip().lower()
        negative_target = str(self.negative_delay_target).strip().lower()
        if fc <= 0.0:
            raise NullWalkError("crossover_fc_hz must be positive")
        if not MIN_STEP_US <= step <= MAX_STEP_US:
            raise NullWalkError(
                f"step_us must be between {MIN_STEP_US:g} and {MAX_STEP_US:g}"
            )
        if not positive_target or not negative_target:
            raise NullWalkError("delay targets must be non-empty")
        if positive_target == negative_target:
            raise NullWalkError("positive and negative delay targets must differ")
        object.__setattr__(self, "crossover_fc_hz", fc)
        object.__setattr__(self, "geometry_seed_us", seed)
        object.__setattr__(self, "step_us", step)
        object.__setattr__(self, "positive_delay_target", positive_target)
        object.__setattr__(self, "negative_delay_target", negative_target)

    @property
    def half_period_us(self) -> float:
        return 1_000_000.0 / (2.0 * self.crossover_fc_hz)

    @property
    def lower_bound_us(self) -> float:
        return self.geometry_seed_us - self.half_period_us

    @property
    def upper_bound_us(self) -> float:
        return self.geometry_seed_us + self.half_period_us

    @property
    def candidate_count(self) -> int:
        """Return the grid size without allocating the grid."""

        steps_each_side = math.floor(
            (self.half_period_us + self.step_us * 1e-9) / self.step_us
        )
        return 1 + 2 * steps_each_side

    def candidate_delays_us(self) -> tuple[float, ...]:
        """Return a deterministic grid containing the exact geometry seed.

        The grid walks outward from the seed without crossing either physical
        bound. Every adjacent candidate is therefore exactly the requested
        50--100 microsecond step; the bounds are limits, not extra off-grid
        candidates.
        """

        if self.candidate_count > MAX_EXHAUSTIVE_CANDIDATES:
            raise NullWalkError(
                "exhaustive null walk exceeds the bounded candidate budget; "
                "use a reviewed adaptive host scheduler"
            )
        steps_each_side = (self.candidate_count - 1) // 2
        candidates = tuple(
            round(self.geometry_seed_us + index * self.step_us, 6)
            for index in range(-steps_each_side, steps_each_side + 1)
        )
        if any(abs(candidate) > MAX_DSP_DELAY_US for candidate in candidates):
            raise NullWalkError(
                "bounded null walk exceeds the CamillaDSP 20 ms delay ceiling"
            )
        return candidates

    def dsp_candidate(self, relative_delay_us: Any) -> DelayCandidate:
        """Map one signed grid coordinate to a non-negative DSP operation."""

        relative = _finite(relative_delay_us, field="relative_delay_us")
        if not any(
            math.isclose(relative, candidate, abs_tol=1e-6)
            for candidate in self.candidate_delays_us()
        ):
            raise NullWalkError("relative delay is outside the bounded candidate grid")
        target = None
        if relative > 0.0:
            target = self.positive_delay_target
        elif relative < 0.0:
            target = self.negative_delay_target
        return DelayCandidate(
            relative_delay_us=relative,
            positive_delay_target=self.positive_delay_target,
            negative_delay_target=self.negative_delay_target,
            delay_target=target,
            delay_us=abs(relative),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "crossover_fc_hz": self.crossover_fc_hz,
            "geometry_seed_us": self.geometry_seed_us,
            "positive_delay_target": self.positive_delay_target,
            "negative_delay_target": self.negative_delay_target,
            "half_period_us": self.half_period_us,
            "lower_bound_us": self.lower_bound_us,
            "upper_bound_us": self.upper_bound_us,
            "step_us": self.step_us,
            "candidate_count": self.candidate_count,
            "candidate_delays_us": list(self.candidate_delays_us()),
        }


@dataclass(frozen=True)
class DelayCandidate:
    """One executable relative-delay coordinate for a host DSP adapter."""

    relative_delay_us: float
    positive_delay_target: str
    negative_delay_target: str
    delay_target: str | None
    delay_us: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_delay_us": self.relative_delay_us,
            "positive_delay_target": self.positive_delay_target,
            "negative_delay_target": self.negative_delay_target,
            "delay_target": self.delay_target,
            "delay_us": self.delay_us,
        }


def _capture_null_depth(capture: Mapping[str, Any]) -> float:
    acoustic = capture.get("acoustic")
    acoustic = acoustic if isinstance(acoustic, Mapping) else capture
    depth = _finite(acoustic.get("null_depth_db"), field="null_depth_db")
    if depth < 0.0:
        raise NullWalkError("null_depth_db must be non-negative")
    return depth


def _capture_issue(
    capture: Mapping[str, Any],
    *,
    expected_crossover_fc_hz: float,
) -> str | None:
    acoustic = capture.get("acoustic")
    acoustic = acoustic if isinstance(acoustic, Mapping) else capture
    gating = acoustic.get("gating")
    gating = gating if isinstance(gating, Mapping) else {}
    snr = acoustic.get("snr")
    snr = snr if isinstance(snr, Mapping) else {}
    try:
        observed_fc = _finite(
            acoustic.get("crossover_fc_hz"),
            field="crossover_fc_hz",
        )
    except NullWalkError:
        observed_fc = math.nan
    if acoustic.get("mic_clipping") is True:
        return "clipping"
    if acoustic.get("calibrated") is not True:
        return "calibrated_mic_required"
    if acoustic.get("expect_null") is not True:
        return "reverse_null_required"
    if not math.isclose(
        observed_fc,
        expected_crossover_fc_hz,
        rel_tol=1e-6,
        abs_tol=1e-3,
    ):
        return "crossover_region_mismatch"
    if gating.get("applied") is not True:
        return "gated_null_required"
    if acoustic.get("above_validity_floor") is not True:
        return "below_validity_floor"
    if snr.get("decision_class") != "alignment" or snr.get("verdict") != "ok":
        return "alignment_snr_insufficient"
    if acoustic.get("null_depth_capped") is True:
        return "null_depth_capped"
    return None


def summarize_candidate(
    spec: NullWalkSpec,
    relative_delay_us: Any,
    captures: Sequence[Mapping[str, Any]],
    *,
    minimum_captures: int = MIN_CAPTURE_COUNT,
    maximum_spread_db: float = MAX_REPEAT_SPREAD_DB,
) -> dict[str, Any]:
    """Summarize one DSP-applied delay from repeated gated null reads."""

    operation = spec.dsp_candidate(relative_delay_us)
    if minimum_captures < MIN_CAPTURE_COUNT:
        raise NullWalkError(f"minimum_captures must be at least {MIN_CAPTURE_COUNT}")
    spread_limit = _finite(maximum_spread_db, field="maximum_spread_db")
    if spread_limit <= 0.0 or spread_limit > MAX_REPEAT_SPREAD_DB:
        raise NullWalkError(
            f"maximum_spread_db must be in (0, {MAX_REPEAT_SPREAD_DB:g}]"
        )

    issues: list[dict[str, Any]] = []
    depths: list[float] = []
    for index, capture in enumerate(captures):
        if not isinstance(capture, Mapping):
            issues.append({"capture": index, "code": "capture_malformed"})
            continue
        issue = _capture_issue(
            capture,
            expected_crossover_fc_hz=spec.crossover_fc_hz,
        )
        if issue is not None:
            issues.append({"capture": index, "code": issue})
            continue
        try:
            depths.append(_capture_null_depth(capture))
        except NullWalkError as exc:
            issues.append(
                {"capture": index, "code": "null_depth_invalid", "detail": str(exc)}
            )

    spread = max(depths) - min(depths) if len(depths) >= 2 else None
    repeatable = bool(
        len(captures) >= minimum_captures
        and len(depths) == len(captures)
        and spread is not None
        and spread < spread_limit
    )
    if len(captures) < minimum_captures:
        issues.append(
            {
                "code": "captures_missing",
                "required": minimum_captures,
                "observed": len(captures),
            }
        )
    if spread is not None and spread >= spread_limit:
        issues.append(
            {
                "code": "repeatability_low",
                "spread_db": spread,
                "maximum_spread_db": spread_limit,
            }
        )
    return {
        "relative_delay_us": operation.relative_delay_us,
        "delay_target": operation.delay_target,
        "delay_us": operation.delay_us,
        "capture_count": len(captures),
        "accepted_capture_count": len(depths),
        "null_depths_db": depths,
        "median_null_depth_db": statistics.median(depths) if depths else None,
        "spread_db": spread,
        "repeatable": repeatable,
        "issues": issues,
    }


def select_delay(
    spec: NullWalkSpec,
    evidence_by_delay: Mapping[Any, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Select the deepest repeatable null inside ``spec``'s physical bound.

    Ties choose the smallest movement from the geometry estimate, then the
    numerically smaller delay.  The result never derives a delay from a capture
    arrival time: only the exact candidate value applied by the DSP is emitted.
    """

    allowed = spec.candidate_delays_us()
    summarized: list[dict[str, Any]] = []
    evidence: dict[float, Sequence[Mapping[str, Any]]] = {}
    for raw_delay, captures in evidence_by_delay.items():
        delay = _finite(raw_delay, field="delay_us")
        if not any(
            math.isclose(delay, candidate, abs_tol=1e-6) for candidate in allowed
        ):
            raise NullWalkError("evidence delay is outside the bounded candidate grid")
        evidence[delay] = captures
    for candidate in allowed:
        captures = next(
            (
                rows
                for delay, rows in evidence.items()
                if math.isclose(delay, candidate, abs_tol=1e-6)
            ),
            (),
        )
        summarized.append(summarize_candidate(spec, candidate, captures))

    incomplete = [
        item
        for item in summarized
        if item["capture_count"] < MIN_CAPTURE_COUNT
        or item["accepted_capture_count"] != item["capture_count"]
    ]
    if incomplete:
        return {
            "schema_version": 1,
            "status": "refused",
            "reason": "candidate_evidence_incomplete",
            "selected_delay_us": None,
            "selected_relative_delay_us": None,
            "selected_delay_target": None,
            "spec": spec.to_dict(),
            "candidates": summarized,
        }
    eligible = [item for item in summarized if item["repeatable"]]
    if len(eligible) != len(summarized):
        return {
            "schema_version": 1,
            "status": "refused",
            "reason": "candidate_repeatability_failed",
            "selected_delay_us": None,
            "selected_relative_delay_us": None,
            "selected_delay_target": None,
            "spec": spec.to_dict(),
            "candidates": summarized,
        }
    deepest = max(float(item["median_null_depth_db"]) for item in eligible)
    # Candidate-to-candidate differences inside the measured repeat spread are
    # not resolvable. Treat them as one plateau and make the smallest geometry
    # correction, rather than chasing a tenth-of-a-decibel noise fluctuation to
    # an extreme edge of the allowed cycle.
    deepest_spread = max(
        float(item["spread_db"])
        for item in eligible
        if math.isclose(float(item["median_null_depth_db"]), deepest, abs_tol=1e-9)
    )
    plateau = [
        item
        for item in eligible
        if deepest - float(item["median_null_depth_db"])
        <= max(float(item["spread_db"]), deepest_spread)
    ]
    winner = min(
        plateau,
        key=lambda item: (
            abs(float(item["relative_delay_us"]) - spec.geometry_seed_us),
            -float(item["median_null_depth_db"]),
            float(item["relative_delay_us"]),
        ),
    )
    selected = spec.dsp_candidate(winner["relative_delay_us"])
    return {
        "schema_version": 1,
        "status": "selected",
        "reason": None,
        "selected_relative_delay_us": selected.relative_delay_us,
        "selected_delay_target": selected.delay_target,
        "selected_delay_us": selected.delay_us,
        "selected_null_depth_db": winner["median_null_depth_db"],
        "best_measured_null_depth_db": deepest,
        "indistinguishable_delays_us": [item["relative_delay_us"] for item in plateau],
        "spec": spec.to_dict(),
        "candidates": summarized,
    }


async def _resolve(value: Any) -> Any:
    return await value if isawaitable(value) else value


class _RestoreOnExit:
    """Async transaction edge that never hides a walk or restore failure."""

    def __init__(self, restore: Callable[[], Awaitable[Any] | Any]) -> None:
        self._restore = restore

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, _tb: Any) -> bool:
        try:
            restored = await _resolve(self._restore())
            if restored is False:
                raise NullWalkError("restore reported failure")
        except BaseException as restore_error:  # noqa: BLE001
            if exc is not None:
                raise BaseExceptionGroup(
                    "null walk failed and entry graph restore also failed",
                    [exc, restore_error],
                )
            raise
        return False


async def run_null_walk(
    spec: NullWalkSpec,
    *,
    apply_candidate: Callable[[DelayCandidate], Awaitable[Any] | Any],
    capture_null: Callable[
        [DelayCandidate, int],
        Awaitable[Mapping[str, Any]] | Mapping[str, Any],
    ],
    restore: Callable[[], Awaitable[Any] | Any],
    captures_per_candidate: int = MIN_CAPTURE_COUNT,
) -> dict[str, Any]:
    """Execute the shared candidate/apply/capture/restore transaction.

    ``apply_candidate`` is the host-owned DSP mutation (active-driver delay or
    sub-to-mains delay).  ``capture_null`` is the host-owned gated measurement
    transport. This shared layer sequences them and always attempts to restore
    the entry graph, including after cancellation; an explicit or raised
    restore failure is surfaced, never reported as restored. The selected
    value is evidence for a later reviewed apply, not permission to retain a
    candidate graph.
    """

    if captures_per_candidate < MIN_CAPTURE_COUNT:
        raise NullWalkError(
            f"captures_per_candidate must be at least {MIN_CAPTURE_COUNT}"
        )
    candidates = spec.candidate_delays_us()
    evidence: dict[float, list[Mapping[str, Any]]] = {}
    async with _RestoreOnExit(restore):
        for candidate in candidates:
            operation = spec.dsp_candidate(candidate)
            applied = await _resolve(apply_candidate(operation))
            if applied is False:
                raise NullWalkError("apply_candidate reported failure")
            rows: list[Mapping[str, Any]] = []
            for index in range(captures_per_candidate):
                capture = await _resolve(capture_null(operation, index))
                if not isinstance(capture, Mapping):
                    raise NullWalkError("capture_null must return a mapping")
                rows.append(capture)
            evidence[candidate] = rows
    return select_delay(spec, evidence)
