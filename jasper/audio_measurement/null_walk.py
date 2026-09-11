# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared timing-locked null-walk specification primitive.

Active-speaker driver alignment and bass-management sub-to-mains timing share
the specification and frozen :class:`DspPredecessor` rollback identity without
sharing either subsystem's DSP or web orchestration.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias

from jasper.audio_measurement.fingerprinted_record import FingerprintedRecord
from jasper.json_fields import finite_float

MIN_STEP_US = 50.0
MAX_STEP_US = 100.0
DEFAULT_SOUND_SPEED_M_S = 343.0
MAX_DSP_DELAY_US = 20_000.0

DelayWalkScope: TypeAlias = Literal["active_crossover", "bass_management"]
DELAY_WALK_SCOPES: frozenset[str] = frozenset({"active_crossover", "bass_management"})

_SPEC_KIND = "jts_null_walk_spec"
_SPEC_SCHEMA_VERSION = 2


class NullWalkError(ValueError):
    """The walk specification or evidence violates the timing contract."""


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
    value. This estimate bounds the host-owned walk.
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
    out = finite_float(value)
    if out is None:
        raise NullWalkError(f"{field} must be a finite number")
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
class NullWalkSpec(FingerprintedRecord):
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
    def fingerprint(self) -> str:  # type: ignore[override]
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
