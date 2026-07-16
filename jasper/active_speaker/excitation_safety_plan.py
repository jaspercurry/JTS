# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-owned, fail-closed preparation for admitted driver excitation.

The closed sweep/level ledger below derives every field passed to Shared's
persisted admission types. It deliberately remains pure: the production
adapter owns fresh live-graph proof, persistence, exact WAV binding, guarded
playback, and writer-lock lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Mapping

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
)
from jasper.output_topology import OutputTopology

from ._common import require_sha256_hex
from .driver_protection import driver_protection_profile
from .driver_safety import evaluate_driver_safety_profile
from .measurement import active_driver_targets
from .test_signal_plan import (
    MAX_DRIVER_TEST_FREQUENCY_HZ,
    MIN_DRIVER_TEST_FREQUENCY_HZ,
    driver_sweep_duration_s,
)

SCHEMA_VERSION = 1
PREPARED_PLAN_KIND = "jts_active_prepared_driver_excitation_plan"
ACTIVE_DRIVER_MAX_REPEAT_COUNT = 3


class ExcitationSafetyPlanError(ValueError):
    """The requested target/profile/plan cannot form a bounded preparation."""


class ExcitationSafetyPlanRefusal(str, Enum):
    PROFILE_NOT_CONFIRMED = "active_excitation_profile_not_confirmed"
    TARGET_NOT_CURRENT = "active_excitation_target_not_current"
    REQUEST_OUTSIDE_LIMITS = "active_excitation_request_outside_limits"


def _sha256(value: Any, *, field: str) -> str:
    return require_sha256_hex(
        value,
        field,
        ExcitationSafetyPlanError,
        message=f"{field} must be a lowercase SHA-256",
    )


def _finite(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExcitationSafetyPlanError(f"{field} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ExcitationSafetyPlanError(f"{field} must be finite")
    return 0.0 if number == 0.0 else number


@dataclass(frozen=True)
class DriverSweepGeneratorPlan:
    """Closed normalized sweep plus the complete effective-peak ledger."""

    f1_hz: float
    f2_hz: float
    amplitude: float
    duration_s: float
    repeat_count: int
    commissioning_gain_db: float
    main_volume_db: float

    def __post_init__(self) -> None:
        f1 = _finite(self.f1_hz, field="f1_hz")
        f2 = _finite(self.f2_hz, field="f2_hz")
        amplitude = _finite(self.amplitude, field="amplitude")
        duration = _finite(self.duration_s, field="duration_s")
        commissioning_gain = _finite(
            self.commissioning_gain_db,
            field="commissioning_gain_db",
        )
        main_volume = _finite(self.main_volume_db, field="main_volume_db")
        if f1 <= 0.0 or f2 <= f1:
            raise ExcitationSafetyPlanError("sweep frequencies must increase")
        if amplitude <= 0.0 or amplitude > 1.0:
            raise ExcitationSafetyPlanError("amplitude must be in (0, 1]")
        if duration <= 0.0:
            raise ExcitationSafetyPlanError("duration_s must be positive")
        if type(self.repeat_count) is not int or self.repeat_count <= 0:
            raise ExcitationSafetyPlanError("repeat_count must be a positive integer")
        if commissioning_gain > 0.0 or main_volume > 0.0:
            raise ExcitationSafetyPlanError(
                "commissioning gain and main volume must be non-positive"
            )
        object.__setattr__(self, "f1_hz", f1)
        object.__setattr__(self, "f2_hz", f2)
        object.__setattr__(self, "amplitude", amplitude)
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "commissioning_gain_db", commissioning_gain)
        object.__setattr__(self, "main_volume_db", main_volume)

    @property
    def band(self) -> FrequencyBand:
        return FrequencyBand(self.f1_hz, self.f2_hz)

    @property
    def effective_peak_dbfs(self) -> float:
        return (
            20.0 * math.log10(self.amplitude)
            + self.commissioning_gain_db
            + self.main_volume_db
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_active_log_sweep_generator_plan",
            "f1_hz": self.f1_hz,
            "f2_hz": self.f2_hz,
            "amplitude": self.amplitude,
            "duration_s": self.duration_s,
            "repeat_count": self.repeat_count,
            "commissioning_gain_db": self.commissioning_gain_db,
            "main_volume_db": self.main_volume_db,
            "effective_peak_dbfs": self.effective_peak_dbfs,
        }


@dataclass(frozen=True)
class RequestedDriverExcitationPlan:
    target_fingerprint: str
    commissioning_context_fingerprint: str
    generator: DriverSweepGeneratorPlan

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_fingerprint",
            _sha256(self.target_fingerprint, field="target_fingerprint"),
        )
        object.__setattr__(
            self,
            "commissioning_context_fingerprint",
            _sha256(
                self.commissioning_context_fingerprint,
                field="commissioning_context_fingerprint",
            ),
        )
        if not isinstance(self.generator, DriverSweepGeneratorPlan):
            raise ExcitationSafetyPlanError(
                "generator must be DriverSweepGeneratorPlan"
            )

    @property
    def band(self) -> FrequencyBand:
        return self.generator.band

    @property
    def effective_peak_dbfs(self) -> float:
        return self.generator.effective_peak_dbfs

    @property
    def duration_s(self) -> float:
        return self.generator.duration_s

    @property
    def repeat_count(self) -> int:
        return self.generator.repeat_count

    @property
    def fingerprint(self) -> str:
        return json_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_active_requested_driver_excitation_plan",
            "target_fingerprint": self.target_fingerprint,
            "commissioning_context_fingerprint": (
                self.commissioning_context_fingerprint
            ),
            "generator": self.generator.to_dict(),
        }


@dataclass(frozen=True, init=False)
class PreparedDriverExcitationPlan:
    target_id: str
    target_role: str
    requested_plan: RequestedDriverExcitationPlan
    limits: ExcitationLimits
    request: ExcitationRequest
    minimum_cooldown_s: float
    refusals: tuple[ExcitationSafetyPlanRefusal, ...]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("use prepare_driver_excitation_plan")

    @classmethod
    def _from_preparation(
        cls,
        *,
        topology: OutputTopology,
        requested_plan: RequestedDriverExcitationPlan,
        limits: ExcitationLimits,
        request: ExcitationRequest,
        minimum_cooldown_s: float,
        refusals: tuple[ExcitationSafetyPlanRefusal, ...],
    ) -> "PreparedDriverExcitationPlan":
        """Freeze only a fully self-consistent bounded plan."""

        if not isinstance(topology, OutputTopology):
            raise ExcitationSafetyPlanError(
                "prepared topology must be OutputTopology"
            )
        if not isinstance(requested_plan, RequestedDriverExcitationPlan):
            raise ExcitationSafetyPlanError(
                "requested_plan must be RequestedDriverExcitationPlan"
            )
        if not isinstance(limits, ExcitationLimits) or not isinstance(
            request, ExcitationRequest
        ):
            raise ExcitationSafetyPlanError(
                "limits and request must be typed Shared admission inputs"
            )
        cooldown = _finite(minimum_cooldown_s, field="minimum_cooldown_s")
        if cooldown < 0.0:
            raise ExcitationSafetyPlanError(
                "minimum_cooldown_s must be non-negative"
            )
        current_targets = [
            target
            for target in active_driver_targets(topology)
            if target.get("target_fingerprint") == requested_plan.target_fingerprint
        ]
        if len(current_targets) != 1:
            raise ExcitationSafetyPlanError(
                ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
            )
        target_id = str(current_targets[0].get("target_id") or "")
        target_role = str(current_targets[0].get("role") or "")
        if not target_id or not target_role:
            raise ExcitationSafetyPlanError(
                ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
            )
        outside_limits = bool(
            not request.band.is_subset_of(limits.permitted_band)
            or request.effective_peak_dbfs > limits.maximum_effective_peak_dbfs
            or request.duration_s > limits.maximum_duration_s
            or request.repeat_count > limits.maximum_repeat_count
        )
        expected_refusals = (
            (ExcitationSafetyPlanRefusal.REQUEST_OUTSIDE_LIMITS,)
            if outside_limits
            else ()
        )
        if (
            type(refusals) is not tuple
            or any(not isinstance(reason, ExcitationSafetyPlanRefusal) for reason in refusals)
            or len(set(refusals)) != len(refusals)
            or refusals != expected_refusals
        ):
            raise ExcitationSafetyPlanError(
                "prepared plan refusal classification is inconsistent"
            )
        if (
            request.target_fingerprint != requested_plan.target_fingerprint
            or request.target_fingerprint != limits.target_fingerprint
            or request.safety_profile_fingerprint
            != limits.safety_profile_fingerprint
            or request.excitation_plan_fingerprint != requested_plan.fingerprint
            or limits.excitation_plan_fingerprint != requested_plan.fingerprint
            or request.authority_fingerprint != limits.fingerprint
            or request.band != requested_plan.band
            or request.effective_peak_dbfs != requested_plan.effective_peak_dbfs
            or request.duration_s != requested_plan.duration_s
            or request.repeat_count != requested_plan.repeat_count
        ):
            raise ExcitationSafetyPlanError(
                "prepared request, limits, and requested plan are inconsistent"
            )
        self = object.__new__(cls)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "target_role", target_role)
        object.__setattr__(self, "requested_plan", requested_plan)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "minimum_cooldown_s", cooldown)
        object.__setattr__(self, "refusals", refusals)
        return self

    @property
    def execution_allowed(self) -> bool:
        return not self.refusals

    @property
    def fingerprint(self) -> str:
        return json_fingerprint(self._core())

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": PREPARED_PLAN_KIND,
            "target_id": self.target_id,
            "target_role": self.target_role,
            "requested_plan": self.requested_plan.to_dict(),
            "limits": self.limits.to_dict(),
            "request": self.request.to_dict(),
            "minimum_cooldown_s": self.minimum_cooldown_s,
            "refusals": [reason.value for reason in self.refusals],
            "execution_allowed": self.execution_allowed,
            "accepts_protection_evidence": True,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


def _target_for_request(
    safety_profile: Mapping[str, Any],
    target_fingerprint: str,
) -> Mapping[str, Any]:
    targets = safety_profile.get("targets")
    if not isinstance(targets, list):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
        )
    matches = [
        target
        for target in targets
        if isinstance(target, Mapping)
        and target.get("target_fingerprint") == target_fingerprint
    ]
    if len(matches) != 1:
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
        )
    return matches[0]


def resolve_driver_excitation_ceilings(
    safety_profile: Mapping[str, Any],
    target_fingerprint: str,
) -> tuple[FrequencyBand, float]:
    """The confirmed permitted band + maximum effective-peak ceiling for one
    driver target.

    Extracted from :func:`prepare_driver_excitation_plan` so a caller that
    needs ONLY these two ceilings -- the level solver (W2.1), choosing a
    sweep's ``main_volume_db``/``commissioning_gain_db`` before any
    ``DriverSweepGeneratorPlan`` exists to admit -- does not have to
    duplicate this derivation. Admission itself (:func:`admit_excitation`
    via :func:`prepare_driver_excitation_plan`) still re-derives and
    re-validates these same ceilings against the actual requested plan; this
    function has no authority of its own, it is shared math.
    """

    target = _target_for_request(safety_profile, target_fingerprint)
    role = str(target.get("role") or "")
    target_id = str(target.get("target_id") or "")
    hard_band = target.get("hard_excitation_band_hz")
    measurement_band = target.get("measurement_band_hz")
    profile_limits = target.get("level_duration_limits")
    required_filters = target.get("required_protection_filters")
    if (
        not target_id
        or not role
        or not isinstance(hard_band, list)
        or len(hard_band) != 2
        or not isinstance(measurement_band, list)
        or len(measurement_band) != 2
        or not isinstance(profile_limits, Mapping)
        or not isinstance(required_filters, list)
    ):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    lower = max(
        MIN_DRIVER_TEST_FREQUENCY_HZ,
        float(hard_band[0]),
        float(measurement_band[0]),
    )
    upper = min(
        MAX_DRIVER_TEST_FREQUENCY_HZ,
        float(hard_band[1]),
        float(measurement_band[1]),
    )
    permitted_band = FrequencyBand(lower, upper)
    protection = driver_protection_profile(
        role,
        driver_style=target.get("driver_style"),
    )
    maximum_peak = min(
        float(profile_limits["max_effective_peak_dbfs"]),
        protection.max_auto_level_dbfs,
    )
    return permitted_band, maximum_peak


def prepare_driver_excitation_plan(
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    requested_plan: RequestedDriverExcitationPlan,
) -> PreparedDriverExcitationPlan:
    """Bind exact current policy for Shared admission or a typed refusal."""

    if not isinstance(topology, OutputTopology):
        raise ExcitationSafetyPlanError("topology must be OutputTopology")
    if not isinstance(safety_profile, Mapping):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    if not isinstance(requested_plan, RequestedDriverExcitationPlan):
        raise ExcitationSafetyPlanError(
            "requested_plan must be RequestedDriverExcitationPlan"
        )
    evaluation = evaluate_driver_safety_profile(safety_profile, topology)
    if not evaluation.confirmed_and_current or evaluation.profile_fingerprint is None:
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    target = _target_for_request(safety_profile, requested_plan.target_fingerprint)
    role = str(target.get("role") or "")
    target_id = str(target.get("target_id") or "")
    profile_limits = target.get("level_duration_limits")
    required_filters = target.get("required_protection_filters")
    # resolve_driver_excitation_ceilings already validated an equivalent
    # profile_limits mapping (on its own re-fetched target) and would have
    # raised above if it were malformed; this re-check is for mypy's
    # narrowing in THIS function's scope, not new runtime behavior.
    if not isinstance(profile_limits, Mapping):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    permitted_band, maximum_peak = resolve_driver_excitation_ceilings(
        safety_profile, requested_plan.target_fingerprint
    )
    protection = driver_protection_profile(
        role,
        driver_style=target.get("driver_style"),
    )
    maximum_duration = min(
        float(profile_limits["max_sweep_duration_s"]),
        driver_sweep_duration_s(role),
    )
    maximum_repeats = min(
        int(profile_limits["max_repeat_count"]),
        ACTIVE_DRIVER_MAX_REPEAT_COUNT,
    )
    minimum_cooldown = float(profile_limits["minimum_cooldown_s"])
    requirement_fingerprint = json_fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_active_driver_protection_requirement",
            "target_id": target_id,
            "target_fingerprint": requested_plan.target_fingerprint,
            "driver_protection_policy": protection.to_dict(),
            "required_filters": required_filters,
        }
    )
    plan_fingerprint = requested_plan.fingerprint
    limits = ExcitationLimits(
        permitted_band=permitted_band,
        maximum_effective_peak_dbfs=maximum_peak,
        maximum_duration_s=maximum_duration,
        maximum_repeat_count=maximum_repeats,
        target_fingerprint=requested_plan.target_fingerprint,
        safety_profile_fingerprint=evaluation.profile_fingerprint,
        protection_requirement_fingerprint=requirement_fingerprint,
        excitation_plan_fingerprint=plan_fingerprint,
    )
    request = ExcitationRequest(
        band=requested_plan.band,
        effective_peak_dbfs=requested_plan.effective_peak_dbfs,
        duration_s=requested_plan.duration_s,
        repeat_count=requested_plan.repeat_count,
        target_fingerprint=requested_plan.target_fingerprint,
        safety_profile_fingerprint=evaluation.profile_fingerprint,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=plan_fingerprint,
    )
    outside_limits = bool(
        not requested_plan.band.is_subset_of(permitted_band)
        or requested_plan.effective_peak_dbfs > maximum_peak
        or requested_plan.duration_s > maximum_duration
        or requested_plan.repeat_count > maximum_repeats
    )
    refusals = (
        (ExcitationSafetyPlanRefusal.REQUEST_OUTSIDE_LIMITS,)
        if outside_limits
        else ()
    )
    return PreparedDriverExcitationPlan._from_preparation(
        topology=topology,
        requested_plan=requested_plan,
        limits=limits,
        request=request,
        minimum_cooldown_s=minimum_cooldown,
        refusals=refusals,
    )
