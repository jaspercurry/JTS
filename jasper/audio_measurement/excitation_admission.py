# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure admission contract for automatic measurement excitation.

This module answers one narrow question: is a fully described digital
excitation request inside the authority supplied by its caller?  It has no
knowledge of drivers, room correction, CamillaDSP, persistence, or playback.
Feature adapters own those policies and reduce them to the stricter limits
passed here.

The contract is deliberately usable at two independent boundaries: once before
signal generation and again immediately before playback.  Both callers can
compare immutable target/profile/protection identities and receive the same
closed refusal vocabulary.  Bounds are closed: an exact floor, ceiling, peak,
duration, or repeat limit is allowed; crossing one refuses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


def _finite_number(value: object, *, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _positive_number(value: object, *, field: str) -> float:
    number = _finite_number(value, field=field)
    if number <= 0.0:
        raise ValueError(f"{field} must be positive")
    return number


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _required_identity(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_identity(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or None")
    return value.strip() or None


@dataclass(frozen=True, slots=True)
class FrequencyBand:
    """A closed positive-frequency interval.

    A zero-width interval is intentional: it represents a single-frequency
    tone and participates in the same subset decision as a sweep.
    """

    lower_hz: float
    upper_hz: float

    def __post_init__(self) -> None:
        lower = _positive_number(self.lower_hz, field="lower_hz")
        upper = _positive_number(self.upper_hz, field="upper_hz")
        if lower > upper:
            raise ValueError("lower_hz must not exceed upper_hz")
        object.__setattr__(self, "lower_hz", lower)
        object.__setattr__(self, "upper_hz", upper)

    def is_subset_of(self, other: FrequencyBand) -> bool:
        """Return whether this closed interval is contained by ``other``."""

        return self.lower_hz >= other.lower_hz and self.upper_hz <= other.upper_hz


@dataclass(frozen=True, slots=True)
class ExcitationRequest:
    """One normalized request to generate/play a bounded stimulus.

    Missing identities are representable so an untrusted boundary can receive
    a typed fail-closed verdict instead of first inventing placeholder values.
    Other malformed values raise during construction and never reach policy.
    """

    band: FrequencyBand
    effective_peak_dbfs: float
    duration_s: float
    repeat_count: int
    target_fingerprint: str | None
    safety_profile_fingerprint: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.band, FrequencyBand):
            raise ValueError("band must be a FrequencyBand")
        peak = _finite_number(self.effective_peak_dbfs, field="effective_peak_dbfs")
        duration = _positive_number(self.duration_s, field="duration_s")
        repeats = _positive_int(self.repeat_count, field="repeat_count")
        target = _optional_identity(
            self.target_fingerprint,
            field="target_fingerprint",
        )
        profile = _optional_identity(
            self.safety_profile_fingerprint,
            field="safety_profile_fingerprint",
        )
        object.__setattr__(self, "effective_peak_dbfs", peak)
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "repeat_count", repeats)
        object.__setattr__(self, "target_fingerprint", target)
        object.__setattr__(self, "safety_profile_fingerprint", profile)


@dataclass(frozen=True, slots=True)
class ExcitationLimits:
    """The caller-composed authority for one target and safety profile.

    ``permitted_band`` may be a hard-excitation band or a narrower measurement
    band.  The feature adapter is responsible for intersecting every applicable
    global/profile limit before constructing this value; this leaf contract
    never widens policy.

    ``protection_requirement_fingerprint`` identifies the exact normalized
    protection requirements the live/read-back proof must satisfy.  It is kept
    separate from ``safety_profile_fingerprint`` so a profile edit and a graph
    protection mismatch remain distinguishable.
    """

    permitted_band: FrequencyBand
    maximum_effective_peak_dbfs: float
    maximum_duration_s: float
    maximum_repeat_count: int
    target_fingerprint: str
    safety_profile_fingerprint: str
    protection_requirement_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.permitted_band, FrequencyBand):
            raise ValueError("permitted_band must be a FrequencyBand")
        peak = _finite_number(
            self.maximum_effective_peak_dbfs,
            field="maximum_effective_peak_dbfs",
        )
        if peak > 0.0:
            raise ValueError("maximum_effective_peak_dbfs must not exceed 0 dBFS")
        duration = _positive_number(self.maximum_duration_s, field="maximum_duration_s")
        repeats = _positive_int(
            self.maximum_repeat_count,
            field="maximum_repeat_count",
        )
        target = _required_identity(
            self.target_fingerprint,
            field="target_fingerprint",
        )
        profile = _required_identity(
            self.safety_profile_fingerprint,
            field="safety_profile_fingerprint",
        )
        protection = _required_identity(
            self.protection_requirement_fingerprint,
            field="protection_requirement_fingerprint",
        )
        object.__setattr__(self, "maximum_effective_peak_dbfs", peak)
        object.__setattr__(self, "maximum_duration_s", duration)
        object.__setattr__(self, "maximum_repeat_count", repeats)
        object.__setattr__(self, "target_fingerprint", target)
        object.__setattr__(self, "safety_profile_fingerprint", profile)
        object.__setattr__(self, "protection_requirement_fingerprint", protection)


@dataclass(frozen=True, slots=True)
class ProtectionEvidence:
    """Caller-produced proof that required protection is current and present.

    This leaf does not inspect a graph.  The owning adapter proves routing,
    filters, and freshness, then binds that observation to the identities below.
    ``evidence_fingerprint`` names that concrete proof/read-back rather than the
    requirement it was checked against.
    """

    target_fingerprint: str | None
    safety_profile_fingerprint: str | None
    protection_requirement_fingerprint: str | None
    evidence_fingerprint: str | None
    current: bool

    def __post_init__(self) -> None:
        if type(self.current) is not bool:
            raise ValueError("current must be a bool")
        for field in (
            "target_fingerprint",
            "safety_profile_fingerprint",
            "protection_requirement_fingerprint",
            "evidence_fingerprint",
        ):
            object.__setattr__(
                self,
                field,
                _optional_identity(getattr(self, field), field=field),
            )


class ExcitationRefusalReason(str, Enum):
    """Closed, machine-readable reasons an excitation request is refused."""

    TARGET_IDENTITY_MISSING = "target_identity_missing"
    TARGET_IDENTITY_MISMATCH = "target_identity_mismatch"
    SAFETY_PROFILE_IDENTITY_MISSING = "safety_profile_identity_missing"
    SAFETY_PROFILE_IDENTITY_MISMATCH = "safety_profile_identity_mismatch"
    PROTECTION_EVIDENCE_MISSING = "protection_evidence_missing"
    PROTECTION_EVIDENCE_STALE = "protection_evidence_stale"
    PROTECTION_TARGET_IDENTITY_MISSING = "protection_target_identity_missing"
    PROTECTION_TARGET_IDENTITY_MISMATCH = "protection_target_identity_mismatch"
    PROTECTION_PROFILE_IDENTITY_MISSING = "protection_profile_identity_missing"
    PROTECTION_PROFILE_IDENTITY_MISMATCH = "protection_profile_identity_mismatch"
    PROTECTION_REQUIREMENT_MISSING = "protection_requirement_missing"
    PROTECTION_REQUIREMENT_MISMATCH = "protection_requirement_mismatch"
    PROTECTION_PROOF_MISSING = "protection_proof_missing"
    FREQUENCY_BELOW_PERMITTED_BAND = "frequency_below_permitted_band"
    FREQUENCY_ABOVE_PERMITTED_BAND = "frequency_above_permitted_band"
    EFFECTIVE_PEAK_ABOVE_LIMIT = "effective_peak_above_limit"
    DURATION_ABOVE_LIMIT = "duration_above_limit"
    REPEAT_COUNT_ABOVE_LIMIT = "repeat_count_above_limit"


@dataclass(frozen=True, slots=True)
class ExcitationAdmission:
    """Immutable result retaining the exact normalized decision inputs."""

    request: ExcitationRequest
    limits: ExcitationLimits
    protection_evidence: ProtectionEvidence | None
    refusal_reasons: tuple[ExcitationRefusalReason, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExcitationRequest):
            raise ValueError("request must be an ExcitationRequest")
        if not isinstance(self.limits, ExcitationLimits):
            raise ValueError("limits must be ExcitationLimits")
        if self.protection_evidence is not None and not isinstance(
            self.protection_evidence,
            ProtectionEvidence,
        ):
            raise ValueError("protection_evidence must be ProtectionEvidence or None")
        if not isinstance(self.refusal_reasons, tuple) or any(
            not isinstance(reason, ExcitationRefusalReason)
            for reason in self.refusal_reasons
        ):
            raise ValueError("refusal_reasons must be typed and immutable")

    @property
    def allowed(self) -> bool:
        return not self.refusal_reasons


def admit_excitation(
    request: ExcitationRequest,
    limits: ExcitationLimits,
    *,
    protection_evidence: ProtectionEvidence | None,
) -> ExcitationAdmission:
    """Return a deterministic allow/refuse decision without side effects."""

    if not isinstance(request, ExcitationRequest):
        raise ValueError("request must be an ExcitationRequest")
    if not isinstance(limits, ExcitationLimits):
        raise ValueError("limits must be ExcitationLimits")
    if protection_evidence is not None and not isinstance(
        protection_evidence,
        ProtectionEvidence,
    ):
        raise ValueError("protection_evidence must be ProtectionEvidence or None")

    reasons: list[ExcitationRefusalReason] = []

    if request.target_fingerprint is None:
        reasons.append(ExcitationRefusalReason.TARGET_IDENTITY_MISSING)
    elif request.target_fingerprint != limits.target_fingerprint:
        reasons.append(ExcitationRefusalReason.TARGET_IDENTITY_MISMATCH)

    if request.safety_profile_fingerprint is None:
        reasons.append(ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISSING)
    elif request.safety_profile_fingerprint != limits.safety_profile_fingerprint:
        reasons.append(ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISMATCH)

    if protection_evidence is None:
        reasons.append(ExcitationRefusalReason.PROTECTION_EVIDENCE_MISSING)
    else:
        if not protection_evidence.current:
            reasons.append(ExcitationRefusalReason.PROTECTION_EVIDENCE_STALE)
        if protection_evidence.target_fingerprint is None:
            reasons.append(ExcitationRefusalReason.PROTECTION_TARGET_IDENTITY_MISSING)
        elif protection_evidence.target_fingerprint != limits.target_fingerprint:
            reasons.append(ExcitationRefusalReason.PROTECTION_TARGET_IDENTITY_MISMATCH)
        if protection_evidence.safety_profile_fingerprint is None:
            reasons.append(ExcitationRefusalReason.PROTECTION_PROFILE_IDENTITY_MISSING)
        elif (
            protection_evidence.safety_profile_fingerprint
            != limits.safety_profile_fingerprint
        ):
            reasons.append(ExcitationRefusalReason.PROTECTION_PROFILE_IDENTITY_MISMATCH)
        if protection_evidence.protection_requirement_fingerprint is None:
            reasons.append(ExcitationRefusalReason.PROTECTION_REQUIREMENT_MISSING)
        elif (
            protection_evidence.protection_requirement_fingerprint
            != limits.protection_requirement_fingerprint
        ):
            reasons.append(ExcitationRefusalReason.PROTECTION_REQUIREMENT_MISMATCH)
        if protection_evidence.evidence_fingerprint is None:
            reasons.append(ExcitationRefusalReason.PROTECTION_PROOF_MISSING)

    if request.band.lower_hz < limits.permitted_band.lower_hz:
        reasons.append(ExcitationRefusalReason.FREQUENCY_BELOW_PERMITTED_BAND)
    if request.band.upper_hz > limits.permitted_band.upper_hz:
        reasons.append(ExcitationRefusalReason.FREQUENCY_ABOVE_PERMITTED_BAND)
    if request.effective_peak_dbfs > limits.maximum_effective_peak_dbfs:
        reasons.append(ExcitationRefusalReason.EFFECTIVE_PEAK_ABOVE_LIMIT)
    if request.duration_s > limits.maximum_duration_s:
        reasons.append(ExcitationRefusalReason.DURATION_ABOVE_LIMIT)
    if request.repeat_count > limits.maximum_repeat_count:
        reasons.append(ExcitationRefusalReason.REPEAT_COUNT_ABOVE_LIMIT)

    return ExcitationAdmission(
        request=request,
        limits=limits,
        protection_evidence=protection_evidence,
        refusal_reasons=tuple(reasons),
    )
