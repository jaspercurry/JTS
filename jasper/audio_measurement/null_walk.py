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

import asyncio
import hashlib
import json
import logging
import math
import statistics
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, Literal, Mapping, Sequence, TypeAlias, cast

from jasper.log_event import log_event

MIN_CAPTURE_COUNT = 5
MIN_STEP_US = 50.0
MAX_STEP_US = 100.0
MAX_REPEAT_SPREAD_DB = 2.0
DEFAULT_SOUND_SPEED_M_S = 343.0
MAX_EXHAUSTIVE_CANDIDATES = 25
MAX_COARSE_CANDIDATES = MAX_EXHAUSTIVE_CANDIDATES
MAX_REFINEMENT_CANDIDATES = 2
MAX_SCHEDULED_CANDIDATES = MAX_COARSE_CANDIDATES + MAX_REFINEMENT_CANDIDATES
MAX_DSP_DELAY_US = 20_000.0
DEFAULT_RESTORE_TIMEOUT_S = 15.0
MIN_RESTORE_TIMEOUT_S = 1.0
MAX_RESTORE_TIMEOUT_S = 30.0

DelayWalkScope: TypeAlias = Literal["active_crossover", "bass_management"]
DELAY_WALK_SCOPES: frozenset[str] = frozenset({"active_crossover", "bass_management"})

_FailureCode: TypeAlias = Literal[
    "timeout",
    "readback_mismatch",
    "invalid_confirmation",
    "self_cancelled",
    "other",
]

logger = logging.getLogger(__name__)

_SPEC_KIND = "jts_null_walk_spec"
_SPEC_SCHEMA_VERSION = 2
_SCHEDULE_KIND = "jts_bounded_null_walk_schedule"
_SCHEDULE_SCHEMA_VERSION = 1
_SCHEDULE_ALGORITHM_ID = "symmetric_coarse_plus_adjacent_refinement"
_SCHEDULE_ALGORITHM_VERSION = "1"


class NullWalkError(ValueError):
    """The walk specification or evidence violates the timing contract."""


class _LifecycleFailure(NullWalkError):
    """One safely classifiable transaction failure for structured logs."""

    def __init__(self, failure_code: _FailureCode, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


def _canonical_state(
    state: Mapping[str, Any],
    *,
    field_name: str,
) -> tuple[str, str]:
    """Freeze one JSON-domain DSP state and return JSON plus SHA-256.

    JSON's encoder accepts lossy Python shapes such as tuples and mappings with
    non-string keys. Those shapes are unsuitable for an *exact* rollback
    identity: ``{1: ...}`` and ``{"1": ...}``, for example, serialize to the
    same object key. Normalize only the real JSON data model and reject the
    ambiguous shapes before any DSP mutation.
    """

    if not isinstance(state, Mapping) or not state:
        raise NullWalkError(f"{field_name} must be a non-empty mapping")

    def freeze(value: Any, *, path: str) -> Any:
        if value is None or type(value) in {bool, int, str}:
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise NullWalkError(f"{field_name} contains a non-finite number")
            return value
        if isinstance(value, Mapping):
            frozen: dict[str, Any] = {}
            for key, nested in value.items():
                if type(key) is not str:
                    raise NullWalkError(
                        f"{field_name} contains a non-string key at {path}"
                    )
                frozen[key] = freeze(nested, path=f"{path}.{key}")
            return frozen
        if type(value) is list:
            return [
                freeze(nested, path=f"{path}[{index}]")
                for index, nested in enumerate(value)
            ]
        raise NullWalkError(f"{field_name} contains a non-JSON value at {path}")

    frozen = freeze(state, path="$")
    canonical = json.dumps(
        frozen,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, init=False)
class DspPredecessor:
    """Frozen, host-owned identity and payload for the exact entry DSP state.

    The shared walk does not interpret the payload. Active-speaker and bass
    orchestration may carry a CamillaDSP path plus active-graph snapshot (or a
    transaction object with equivalent authority), while ``fingerprint`` gives
    the journal a stable, non-secret identity. The payload is canonicalized into
    immutable JSON before any candidate mutation, and ``state`` returns a fresh
    copy, so a mutable caller object cannot silently move the rollback target.
    """

    _state_json: str = field(repr=False)
    fingerprint: str

    def __init__(self, state: Mapping[str, Any]) -> None:
        canonical, fingerprint = _canonical_state(
            state,
            field_name="predecessor state",
        )
        object.__setattr__(self, "_state_json", canonical)
        object.__setattr__(self, "fingerprint", fingerprint)

    @property
    def state(self) -> dict[str, Any]:
        """Return a fresh copy of the frozen host payload."""

        state = json.loads(self._state_json)
        assert isinstance(state, dict)  # guaranteed by _canonical_state
        return state


@dataclass(frozen=True, init=False)
class DspRestoreConfirmation:
    """Fingerprint derived from the host's post-restore DSP read-back."""

    fingerprint: str

    def __init__(self, state: Mapping[str, Any]) -> None:
        _canonical, fingerprint = _canonical_state(
            state,
            field_name="restored DSP read-back",
        )
        object.__setattr__(self, "fingerprint", fingerprint)


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


def _canonical_payload(payload: Mapping[str, Any]) -> str:
    """Serialize one already-validated JSON payload for strict identity."""

    try:
        return json.dumps(
            dict(payload),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise NullWalkError("null-walk payload is not canonical JSON data") from exc


def _payload_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


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

        return 1 + 2 * self.steps_each_side

    @property
    def steps_each_side(self) -> int:
        """Return the number of aligned fine-grid steps on either side."""

        return math.floor((self.half_period_us + self.step_us * 1e-9) / self.step_us)

    @property
    def fine_grid_index_min(self) -> int:
        return -self.steps_each_side

    @property
    def fine_grid_index_max(self) -> int:
        return self.steps_each_side

    @property
    def fingerprint(self) -> str:
        return _payload_fingerprint(self._core())

    def fine_grid_coordinate(self, index: Any) -> float:
        """Return one canonical aligned coordinate without allocating the grid."""

        if type(index) is not int:
            raise NullWalkError("fine-grid index must be an integer")
        if not self.fine_grid_index_min <= index <= self.fine_grid_index_max:
            raise NullWalkError("fine-grid index is outside the physical half-period")
        coordinate = round(self.geometry_seed_us + index * self.step_us, 6)
        if abs(coordinate) > MAX_DSP_DELAY_US:
            raise NullWalkError(
                "bounded null walk exceeds the CamillaDSP 20 ms delay ceiling"
            )
        return coordinate

    def fine_grid_index(self, relative_delay_us: Any) -> int:
        """Return the exact aligned index for one bounded coordinate.

        This is the non-allocating membership gate used by resumable host
        schedulers. It deliberately does not relax the exhaustive runner's
        separate 25-candidate budget.
        """

        if isinstance(relative_delay_us, bool):
            raise NullWalkError("relative_delay_us must be numeric")
        relative = _finite(relative_delay_us, field="relative_delay_us")
        raw_index = (relative - self.geometry_seed_us) / self.step_us
        nearest = round(raw_index)
        if not math.isclose(raw_index, nearest, rel_tol=0.0, abs_tol=1e-8):
            raise NullWalkError("relative delay is outside the bounded fine grid")
        index = int(nearest)
        coordinate = self.fine_grid_coordinate(index)
        if not math.isclose(relative, coordinate, rel_tol=0.0, abs_tol=1e-6):
            raise NullWalkError("relative delay is outside the bounded fine grid")
        return index

    def coarse_candidate_delays_us(self) -> tuple[float, ...]:
        """Return the deterministic bounded first phase of a host schedule.

        Coordinates are symmetric about the geometry seed, retain the seed and
        both aligned fine-grid endpoints, and never exceed the existing
        25-coordinate exhaustive budget. A later explicit refinement anchor may
        add only its immediate fine-grid neighbours.
        """

        steps = self.steps_each_side
        if steps == 0:
            return (self.fine_grid_coordinate(0),)
        slots_each_side = (MAX_COARSE_CANDIDATES - 1) // 2
        stride = max(1, math.ceil(steps / slots_each_side))
        positive = set(range(stride, steps + 1, stride))
        positive.add(steps)
        indexes = tuple(sorted({0, *positive, *(-index for index in positive)}))
        if len(indexes) > MAX_COARSE_CANDIDATES:
            raise AssertionError("coarse null-walk schedule exceeded its hard bound")
        return tuple(self.fine_grid_coordinate(index) for index in indexes)

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
        candidates = tuple(
            self.fine_grid_coordinate(index)
            for index in range(
                self.fine_grid_index_min,
                self.fine_grid_index_max + 1,
            )
        )
        return candidates

    def dsp_candidate(self, relative_delay_us: Any) -> DelayCandidate:
        """Map one signed grid coordinate to a non-negative DSP operation."""

        index = self.fine_grid_index(relative_delay_us)
        relative = self.fine_grid_coordinate(index)
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

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": _SPEC_SCHEMA_VERSION,
            "kind": _SPEC_KIND,
            "crossover_fc_hz": self.crossover_fc_hz,
            "geometry_seed_us": self.geometry_seed_us,
            "positive_delay_target": self.positive_delay_target,
            "negative_delay_target": self.negative_delay_target,
            "half_period_us": self.half_period_us,
            "lower_bound_us": self.lower_bound_us,
            "upper_bound_us": self.upper_bound_us,
            "step_us": self.step_us,
            "candidate_count": self.candidate_count,
            "fine_grid_index_min": self.fine_grid_index_min,
            "fine_grid_index_max": self.fine_grid_index_max,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> NullWalkSpec:
        """Strictly reconstruct the bounded schema-v2 spec projection."""

        expected = {
            "schema_version",
            "kind",
            "crossover_fc_hz",
            "geometry_seed_us",
            "positive_delay_target",
            "negative_delay_target",
            "half_period_us",
            "lower_bound_us",
            "upper_bound_us",
            "step_us",
            "candidate_count",
            "fine_grid_index_min",
            "fine_grid_index_max",
            "fingerprint",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise NullWalkError("null-walk spec fields are invalid")
        if raw["schema_version"] != _SPEC_SCHEMA_VERSION or raw["kind"] != _SPEC_KIND:
            raise NullWalkError("null-walk spec schema is unsupported")
        result = cls(
            crossover_fc_hz=raw["crossover_fc_hz"],
            geometry_seed_us=raw["geometry_seed_us"],
            positive_delay_target=raw["positive_delay_target"],
            negative_delay_target=raw["negative_delay_target"],
            step_us=raw["step_us"],
        )
        if _canonical_payload(dict(raw)) != _canonical_payload(result.to_dict()):
            raise NullWalkError("null-walk spec is not the exact canonical grid")
        return result


@dataclass(frozen=True, init=False)
class BoundedNullWalkSchedule:
    """Deterministic coarse scan plus explicit local fine-grid refinement.

    The schedule describes only which coordinates a host may measure. It does
    not evaluate evidence or claim a selected delay; the exhaustive shared
    runner and selector remain deliberately separate and capped at 25 points.
    """

    spec_fingerprint: str
    coarse_delays_us: tuple[float, ...]
    refinement_anchor_us: float
    refinement_delays_us: tuple[float, ...]
    scheduled_delays_us: tuple[float, ...]
    fingerprint: str

    def __init__(
        self,
        spec: NullWalkSpec,
        *,
        refinement_anchor_us: Any,
    ) -> None:
        if not isinstance(spec, NullWalkSpec):
            raise NullWalkError("schedule spec must be NullWalkSpec")
        coarse = spec.coarse_candidate_delays_us()
        anchor_index = spec.fine_grid_index(refinement_anchor_us)
        anchor = spec.fine_grid_coordinate(anchor_index)
        if anchor not in coarse:
            raise NullWalkError(
                "refinement anchor must be an exact coarse schedule coordinate"
            )
        refinement: list[float] = []
        for index in (anchor_index - 1, anchor_index + 1):
            if not spec.fine_grid_index_min <= index <= spec.fine_grid_index_max:
                continue
            coordinate = spec.fine_grid_coordinate(index)
            if coordinate not in coarse:
                refinement.append(coordinate)
        refinement_delays = tuple(sorted(refinement))
        if len(refinement_delays) > MAX_REFINEMENT_CANDIDATES:
            raise AssertionError("null-walk refinement exceeded its hard bound")
        scheduled = tuple(sorted({*coarse, *refinement_delays}))
        if len(scheduled) > MAX_SCHEDULED_CANDIDATES:
            raise AssertionError("null-walk schedule exceeded its hard bound")
        object.__setattr__(self, "spec_fingerprint", spec.fingerprint)
        object.__setattr__(self, "coarse_delays_us", coarse)
        object.__setattr__(self, "refinement_anchor_us", anchor)
        object.__setattr__(self, "refinement_delays_us", refinement_delays)
        object.__setattr__(self, "scheduled_delays_us", scheduled)
        object.__setattr__(self, "fingerprint", _payload_fingerprint(self._core()))

    @classmethod
    def from_coarse_evidence(
        cls,
        spec: NullWalkSpec,
        evidence_by_delay: Mapping[Any, Sequence[Mapping[str, Any]]],
    ) -> BoundedNullWalkSchedule:
        """Choose the deepest repeatable coarse anchor deterministically.

        This selects only where the bounded host measures its two optional fine
        neighbors. It does not select or authorize a final delay. Every coarse
        coordinate must have a complete repeatable capture set before the
        refinement phase can begin.
        """

        if not isinstance(spec, NullWalkSpec):
            raise NullWalkError("schedule spec must be NullWalkSpec")
        if not isinstance(evidence_by_delay, Mapping):
            raise NullWalkError("coarse evidence must be a mapping")
        coarse = spec.coarse_candidate_delays_us()
        evidence: dict[float, Sequence[Mapping[str, Any]]] = {}
        for raw_delay, captures in evidence_by_delay.items():
            index = spec.fine_grid_index(raw_delay)
            coordinate = spec.fine_grid_coordinate(index)
            if coordinate not in coarse:
                raise NullWalkError(
                    "coarse evidence contains a coordinate outside the coarse schedule"
                )
            if coordinate in evidence:
                raise NullWalkError("coarse evidence contains a duplicate coordinate")
            evidence[coordinate] = captures
        if set(evidence) != set(coarse):
            raise NullWalkError("coarse evidence must cover the exact coarse schedule")

        summarized = [
            summarize_candidate(spec, coordinate, evidence[coordinate])
            for coordinate in coarse
        ]
        if any(item["repeatable"] is not True for item in summarized):
            raise NullWalkError(
                "every coarse coordinate requires complete repeatable evidence"
            )
        anchor = min(
            summarized,
            key=lambda item: (
                -float(item["median_null_depth_db"]),
                abs(float(item["relative_delay_us"]) - spec.geometry_seed_us),
                float(item["relative_delay_us"]),
            ),
        )
        return cls(
            spec,
            refinement_anchor_us=anchor["relative_delay_us"],
        )

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEDULE_SCHEMA_VERSION,
            "kind": _SCHEDULE_KIND,
            "algorithm_id": _SCHEDULE_ALGORITHM_ID,
            "algorithm_version": _SCHEDULE_ALGORITHM_VERSION,
            "spec_fingerprint": self.spec_fingerprint,
            "maximum_coarse_candidates": MAX_COARSE_CANDIDATES,
            "maximum_refinement_candidates": MAX_REFINEMENT_CANDIDATES,
            "maximum_scheduled_candidates": MAX_SCHEDULED_CANDIDATES,
            "coarse_delays_us": list(self.coarse_delays_us),
            "refinement_anchor_us": self.refinement_anchor_us,
            "refinement_delays_us": list(self.refinement_delays_us),
            "scheduled_delays_us": list(self.scheduled_delays_us),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        spec: NullWalkSpec,
    ) -> BoundedNullWalkSchedule:
        expected = {
            "schema_version",
            "kind",
            "algorithm_id",
            "algorithm_version",
            "spec_fingerprint",
            "maximum_coarse_candidates",
            "maximum_refinement_candidates",
            "maximum_scheduled_candidates",
            "coarse_delays_us",
            "refinement_anchor_us",
            "refinement_delays_us",
            "scheduled_delays_us",
            "fingerprint",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise NullWalkError("bounded null-walk schedule fields are invalid")
        for field_name in (
            "coarse_delays_us",
            "refinement_delays_us",
            "scheduled_delays_us",
        ):
            if type(raw[field_name]) is not list:
                raise NullWalkError(
                    "bounded null-walk schedule coordinate fields must be lists"
                )
        result = cls(spec, refinement_anchor_us=raw["refinement_anchor_us"])
        if _canonical_payload(dict(raw)) != _canonical_payload(result.to_dict()):
            raise NullWalkError(
                "bounded null-walk schedule is not the exact canonical schedule"
            )
        return result


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

    return _select_summarized_delay(spec, summarized)


def _select_summarized_delay(
    spec: NullWalkSpec,
    summarized: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply the one repeatability, plateau, and tie policy to candidates."""

    candidates = [dict(item) for item in summarized]
    incomplete = [
        item
        for item in candidates
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
            "candidates": candidates,
        }
    eligible = [item for item in candidates if item["repeatable"]]
    if len(eligible) != len(candidates):
        return {
            "schema_version": 1,
            "status": "refused",
            "reason": "candidate_repeatability_failed",
            "selected_delay_us": None,
            "selected_relative_delay_us": None,
            "selected_delay_target": None,
            "spec": spec.to_dict(),
            "candidates": candidates,
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
        "candidates": candidates,
    }


def select_scheduled_delay(
    spec: NullWalkSpec,
    schedule: BoundedNullWalkSchedule,
    evidence_by_delay: Mapping[Any, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Select from one exact bounded coarse-plus-refinement schedule.

    This is the final evaluator for low-frequency grids whose exhaustive fine
    grid exceeds :data:`MAX_EXHAUSTIVE_CANDIDATES`.  It changes only which
    coordinates are eligible: evidence must cover the persisted schedule
    exactly, while candidate quality, repeatability, plateau handling, and tie
    breaking remain owned by :func:`_select_summarized_delay`.
    """

    if not isinstance(spec, NullWalkSpec):
        raise NullWalkError("scheduled selection spec must be NullWalkSpec")
    if not isinstance(schedule, BoundedNullWalkSchedule):
        raise NullWalkError(
            "scheduled selection schedule must be BoundedNullWalkSchedule"
        )
    if schedule.spec_fingerprint != spec.fingerprint:
        raise NullWalkError("bounded schedule belongs to a different null-walk spec")
    if not isinstance(evidence_by_delay, Mapping):
        raise NullWalkError("scheduled evidence must be a mapping")

    evidence: dict[float, Sequence[Mapping[str, Any]]] = {}
    allowed = set(schedule.scheduled_delays_us)
    for raw_delay, captures in evidence_by_delay.items():
        index = spec.fine_grid_index(raw_delay)
        coordinate = spec.fine_grid_coordinate(index)
        if coordinate not in allowed:
            raise NullWalkError(
                "scheduled evidence contains a coordinate outside the exact schedule"
            )
        if coordinate in evidence:
            raise NullWalkError("scheduled evidence contains a duplicate coordinate")
        evidence[coordinate] = captures
    if set(evidence) != allowed:
        raise NullWalkError("scheduled evidence must cover the exact schedule")
    expected_schedule = BoundedNullWalkSchedule.from_coarse_evidence(
        spec,
        {
            coordinate: evidence[coordinate]
            for coordinate in schedule.coarse_delays_us
        },
    )
    if expected_schedule.fingerprint != schedule.fingerprint:
        raise NullWalkError(
            "bounded schedule refinement does not match its coarse evidence"
        )

    result = _select_summarized_delay(
        spec,
        [
            summarize_candidate(spec, coordinate, evidence[coordinate])
            for coordinate in schedule.scheduled_delays_us
        ],
    )
    return {**result, "schedule": schedule.to_dict()}


async def _resolve(value: Any) -> Any:
    return await value if isawaitable(value) else value


async def _drain_task_through_cancellation(
    task: asyncio.Task[Any],
) -> asyncio.CancelledError | None:
    """Wait for ``task`` without propagating its result or caller cancellation."""

    waiter = asyncio.create_task(asyncio.wait({task}))
    cancellation: asyncio.CancelledError | None = None
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError as error:
            cancellation = error
    waiter.result()
    return cancellation


async def _apply_candidate_resilient(
    apply_candidate: Callable[[DelayCandidate], Awaitable[Any] | Any],
    operation: DelayCandidate,
) -> Any:
    """Settle one DSP mutation before cancellation can start restoration.

    A host adapter may offload CamillaDSP I/O to a worker. Awaiting it directly
    lets caller cancellation detach that worker; it could then finish *after*
    the predecessor restore and put the candidate graph back live. Shielding a
    dedicated task and draining repeated cancellation closes that race.
    """

    async def _apply_once() -> Any:
        return await _resolve(apply_candidate(operation))

    apply_task = asyncio.create_task(_apply_once())
    cancellation = await _drain_task_through_cancellation(apply_task)
    if apply_task.cancelled():
        apply_error: BaseException | None = _LifecycleFailure(
            "self_cancelled",
            "candidate DSP apply cancelled itself",
        )
    else:
        apply_error = apply_task.exception()
    if apply_error is not None:
        if cancellation is not None:
            raise BaseExceptionGroup(
                "null walk cancellation arrived while candidate apply failed",
                [cancellation, apply_error],
            )
        raise apply_error
    if cancellation is not None:
        raise cancellation
    return apply_task.result()


def _failure_type(error: BaseException) -> str:
    if isinstance(error, _LifecycleFailure):
        return NullWalkError.__name__
    return type(error).__name__


def _failure_code(error: BaseException | None) -> _FailureCode:
    """Return a closed, non-secret lifecycle reason for structured logs."""

    if isinstance(error, _LifecycleFailure):
        return error.failure_code
    if isinstance(error, BaseExceptionGroup):
        classified = {
            code
            for nested in error.exceptions
            if (code := _failure_code(nested)) != "other"
        }
        if len(classified) == 1:
            return classified.pop()
    return "other"


def _validate_scope(scope: Any) -> DelayWalkScope:
    if isinstance(scope, str) and scope in DELAY_WALK_SCOPES:
        return cast(DelayWalkScope, scope)
    allowed = ", ".join(sorted(DELAY_WALK_SCOPES))
    raise NullWalkError(f"scope must be one of: {allowed}")


class _RestorePredecessorOnExit:
    """Cancellation-draining exact-predecessor restoration transaction edge."""

    def __init__(
        self,
        predecessor: DspPredecessor,
        restore_predecessor: Callable[
            [DspPredecessor], Awaitable[DspRestoreConfirmation] | DspRestoreConfirmation
        ],
        *,
        scope: DelayWalkScope,
        timeout_s: float,
    ) -> None:
        self._predecessor = predecessor
        self._restore_predecessor = restore_predecessor
        self._scope = scope
        self._timeout_s = timeout_s

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, _tb: Any) -> bool:
        trigger = (
            "cancelled"
            if isinstance(exc, asyncio.CancelledError)
            else "failed"
            if exc is not None
            else "completed"
        )

        async def _restore_once() -> None:
            try:
                async with asyncio.timeout(self._timeout_s):
                    restored = await _resolve(
                        self._restore_predecessor(self._predecessor)
                    )
            except TimeoutError as timeout_error:
                raise _LifecycleFailure(
                    "timeout",
                    "predecessor restore timed out",
                ) from timeout_error
            except asyncio.CancelledError as cancelled:
                raise _LifecycleFailure(
                    "self_cancelled",
                    "predecessor restore cancelled itself",
                ) from cancelled
            if not isinstance(restored, DspRestoreConfirmation):
                raise _LifecycleFailure(
                    "invalid_confirmation",
                    "restore_predecessor must return DspRestoreConfirmation",
                )
            if restored.fingerprint != self._predecessor.fingerprint:
                raise _LifecycleFailure(
                    "readback_mismatch",
                    "restore_predecessor confirmed the wrong DSP predecessor",
                )

        # A dedicated task prevents caller cancellation from cancelling the
        # restoration itself. Repeated cancellation is absorbed until that task
        # terminates; only then is cancellation propagated outward.
        restore_task = asyncio.create_task(_restore_once())
        cleanup_cancellation = await _drain_task_through_cancellation(restore_task)
        if restore_task.cancelled():
            restore_error: BaseException | None = _LifecycleFailure(
                "self_cancelled",
                "predecessor restore cancelled itself",
            )
        else:
            restore_error = restore_task.exception()
        effective_trigger = "cancelled" if cleanup_cancellation is not None else trigger
        if restore_error is not None:
            log_event(
                logger,
                "correction.delay_walk_restore_failed",
                level=logging.WARNING,
                scope=self._scope,
                predecessor_fingerprint=self._predecessor.fingerprint,
                trigger=effective_trigger,
                error_type=_failure_type(restore_error),
                failure_code=_failure_code(restore_error),
            )
            failures: list[BaseException] = []
            if exc is not None:
                failures.append(exc)
            if cleanup_cancellation is not None:
                failures.append(cleanup_cancellation)
            failures.append(restore_error)
            if len(failures) > 1:
                raise BaseExceptionGroup(
                    "null walk did not complete and exact predecessor restore failed",
                    failures,
                )
            raise restore_error

        log_event(
            logger,
            "correction.delay_walk_restored",
            scope=self._scope,
            predecessor_fingerprint=self._predecessor.fingerprint,
            trigger=effective_trigger,
        )
        if cleanup_cancellation is not None:
            if exc is None:
                raise cleanup_cancellation
            if not isinstance(exc, asyncio.CancelledError):
                raise BaseExceptionGroup(
                    "null walk failed and cancellation arrived during restore",
                    [exc, cleanup_cancellation],
                )
        return False


async def run_null_walk(
    spec: NullWalkSpec,
    *,
    apply_candidate: Callable[[DelayCandidate], Awaitable[Any] | Any],
    capture_null: Callable[
        [DelayCandidate, int],
        Awaitable[Mapping[str, Any]] | Mapping[str, Any],
    ],
    snapshot_predecessor: Callable[[], Awaitable[DspPredecessor] | DspPredecessor],
    restore_predecessor: Callable[
        [DspPredecessor], Awaitable[DspRestoreConfirmation] | DspRestoreConfirmation
    ],
    scope: DelayWalkScope,
    captures_per_candidate: int = MIN_CAPTURE_COUNT,
    restore_timeout_s: float = DEFAULT_RESTORE_TIMEOUT_S,
) -> dict[str, Any]:
    """Execute the shared candidate/apply/capture/restore transaction.

    ``apply_candidate`` is the host-owned DSP mutation (active-driver delay or
    sub-to-mains delay).  ``capture_null`` is the host-owned gated measurement
    transport. ``snapshot_predecessor`` freezes the exact host-owned entry DSP
    identity and state before the first mutation; ``restore_predecessor`` must
    restore that same snapshot, read back the active DSP state, and construct a
    :class:`DspRestoreConfirmation` from that read-back. This shared layer
    sequences the walk and drains the bounded restoration despite repeated
    cancellation. Host DSP adapters must themselves bound and cancellation-drain
    mutation I/O, and the caller must exclude concurrent DSP writers for this
    whole transaction. ``restore_timeout_s`` is the cancellation deadline; wall
    completion can additionally include the adapter's bounded drain. An explicit,
    raised, mismatched, or timed-out restore failure is surfaced and never
    reported as restored. The selected value is evidence for a later reviewed
    apply, not permission to retain a candidate graph.
    """

    if captures_per_candidate < MIN_CAPTURE_COUNT:
        raise NullWalkError(
            f"captures_per_candidate must be at least {MIN_CAPTURE_COUNT}"
        )
    validated_scope = _validate_scope(scope)
    restore_timeout = _finite(restore_timeout_s, field="restore_timeout_s")
    if not MIN_RESTORE_TIMEOUT_S <= restore_timeout <= MAX_RESTORE_TIMEOUT_S:
        raise NullWalkError(
            "restore_timeout_s must be between "
            f"{MIN_RESTORE_TIMEOUT_S:g} and {MAX_RESTORE_TIMEOUT_S:g}"
        )
    candidates = spec.candidate_delays_us()
    predecessor: DspPredecessor | None = None
    completed = False
    try:
        predecessor = await _resolve(snapshot_predecessor())
        if not isinstance(predecessor, DspPredecessor):
            raise NullWalkError("snapshot_predecessor must return DspPredecessor")
        log_event(
            logger,
            "correction.delay_walk_started",
            scope=validated_scope,
            predecessor_fingerprint=predecessor.fingerprint,
            crossover_fc_hz=spec.crossover_fc_hz,
            candidate_count=len(candidates),
            captures_per_candidate=captures_per_candidate,
            positive_delay_target=spec.positive_delay_target,
            negative_delay_target=spec.negative_delay_target,
        )
        evidence: dict[float, list[Mapping[str, Any]]] = {}
        async with _RestorePredecessorOnExit(
            predecessor,
            restore_predecessor,
            scope=validated_scope,
            timeout_s=restore_timeout,
        ):
            for candidate in candidates:
                operation = spec.dsp_candidate(candidate)
                applied = await _apply_candidate_resilient(
                    apply_candidate,
                    operation,
                )
                if applied is False:
                    raise NullWalkError("apply_candidate reported failure")
                rows: list[Mapping[str, Any]] = []
                for index in range(captures_per_candidate):
                    capture = await _resolve(capture_null(operation, index))
                    if not isinstance(capture, Mapping):
                        raise NullWalkError("capture_null must return a mapping")
                    rows.append(capture)
                evidence[candidate] = rows
        result = select_delay(spec, evidence)
        completed = True
    finally:
        if not completed:
            error = sys.exception()
            event = (
                "correction.delay_walk_cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "correction.delay_walk_failed"
            )
            failure_fields: dict[str, Any] = {}
            if not isinstance(error, asyncio.CancelledError):
                failure_fields["failure_code"] = _failure_code(error)
            log_event(
                logger,
                event,
                level=(
                    logging.INFO
                    if isinstance(error, asyncio.CancelledError)
                    else logging.WARNING
                ),
                scope=validated_scope,
                predecessor_fingerprint=(
                    predecessor.fingerprint
                    if isinstance(predecessor, DspPredecessor)
                    else None
                ),
                error_type=_failure_type(error) if error is not None else None,
                **failure_fields,
            )
    log_event(
        logger,
        "correction.delay_walk_completed",
        scope=validated_scope,
        predecessor_fingerprint=predecessor.fingerprint,
        status=result.get("status"),
        reason=result.get("reason"),
        selected_relative_delay_us=result.get("selected_relative_delay_us"),
    )
    return result
