# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure admission contract for automatic measurement excitation.

This module answers one narrow question: is a fully described digital
excitation request inside the exact authority supplied by its caller?  It has
no knowledge of drivers, room correction, CamillaDSP, persistence, or
playback.  Feature adapters own those policies and reduce them to the stricter
limits passed here.

The contract is deliberately usable at two independent boundaries: once
before signal generation and again immediately before playback.  The
generation artifact retains the canonical request and authority fingerprints;
playback reconstructs those exact values and reruns :func:`admit_excitation`.
Bounds are closed: an exact floor, ceiling, peak, duration, or repeat limit is
allowed; crossing one refuses.

Fingerprints here are content identities, not signatures.  The owning feature
adapter remains the trusted issuer of ``ExcitationLimits`` and
``ProtectionEvidence``.  In particular, it must derive protection evidence
from a fresh graph/read-back observation and must bind the excitation-plan
fingerprint to the normalized stimulus kind, generator parameters, and
effective-peak ledger.  Deserializing structurally valid evidence does not make
an untrusted claim authoritative.  This leaf only proves exact content and
identity binding; it deliberately performs no live graph inspection.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import cast

SCHEMA_VERSION = 1
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    # JSON distinguishes -0.0 from 0.0 even though the safety policy does not.
    # Normalize it so equal numeric authority has one canonical fingerprint.
    return 0.0 if number == 0.0 else number


def _positive_number(value: object, *, field: str) -> float:
    number = _finite_number(value, field=field)
    if number <= 0.0:
        raise ValueError(f"{field} must be positive")
    return number


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _required_fingerprint(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical lowercase SHA-256 fingerprint")
    return value


def _optional_fingerprint(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_fingerprint(value, field=field)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact must contain canonical JSON data") from exc


def _content_fingerprint(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(dict(payload))).hexdigest()


def _with_fingerprint(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    result["fingerprint"] = _content_fingerprint(payload)
    return result


def _read_fingerprinted_payload(
    value: object,
    *,
    fields: frozenset[str],
    artifact: str,
    expected_fingerprint: str | None,
) -> tuple[dict[str, object], str]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{artifact} must be a mapping with string keys")
    expected_fields = fields | {"fingerprint"}
    if set(value) != expected_fields:
        raise ValueError(
            f"{artifact} fields do not match schema version {SCHEMA_VERSION}"
        )
    payload = {field: value[field] for field in fields}
    declared = _required_fingerprint(
        value["fingerprint"],
        field=f"{artifact}.fingerprint",
    )
    if _content_fingerprint(payload) != declared:
        raise ValueError(f"{artifact} fingerprint does not match its content")
    if expected_fingerprint is not None:
        expected = _required_fingerprint(
            expected_fingerprint,
            field=f"expected_{artifact}_fingerprint",
        )
        if declared != expected:
            raise ValueError(f"{artifact} does not match the expected fingerprint")
    return payload, declared


def _require_schema(payload: Mapping[str, object], *, kind: str, artifact: str) -> None:
    if type(payload.get("schema_version")) is not int or (
        payload["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError(f"{artifact} has an unsupported schema version")
    if payload.get("kind") != kind:
        raise ValueError(f"{artifact} has an unexpected kind")


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

    def to_dict(self) -> dict[str, float]:
        return {"lower_hz": self.lower_hz, "upper_hz": self.upper_hz}

    @classmethod
    def from_dict(cls, value: object) -> FrequencyBand:
        if not isinstance(value, Mapping) or set(value) != {"lower_hz", "upper_hz"}:
            raise ValueError("frequency band fields are invalid")
        return cls(lower_hz=value["lower_hz"], upper_hz=value["upper_hz"])


_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "band",
        "effective_peak_dbfs",
        "duration_s",
        "repeat_count",
        "target_fingerprint",
        "safety_profile_fingerprint",
        "authority_fingerprint",
        "excitation_plan_fingerprint",
    }
)


@dataclass(frozen=True, slots=True)
class ExcitationRequest:
    """One normalized request to generate/play a bounded stimulus.

    Missing identities are represented only by ``None`` so an untrusted
    boundary can receive a typed fail-closed verdict.  Noncanonical hashes,
    including whitespace-padded values, are malformed and raise instead of
    being silently repaired.

    ``authority_fingerprint`` must be the exact fingerprint of the
    :class:`ExcitationLimits` used at generation.  ``excitation_plan_fingerprint``
    identifies the adapter-owned canonical stimulus/level ledger.
    """

    band: FrequencyBand
    effective_peak_dbfs: float
    duration_s: float
    repeat_count: int
    target_fingerprint: str | None
    safety_profile_fingerprint: str | None
    authority_fingerprint: str | None
    excitation_plan_fingerprint: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.band, FrequencyBand):
            raise ValueError("band must be a FrequencyBand")
        peak = _finite_number(self.effective_peak_dbfs, field="effective_peak_dbfs")
        duration = _positive_number(self.duration_s, field="duration_s")
        repeats = _positive_int(self.repeat_count, field="repeat_count")
        for field in (
            "target_fingerprint",
            "safety_profile_fingerprint",
            "authority_fingerprint",
            "excitation_plan_fingerprint",
        ):
            object.__setattr__(
                self,
                field,
                _optional_fingerprint(getattr(self, field), field=field),
            )
        object.__setattr__(self, "effective_peak_dbfs", peak)
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "repeat_count", repeats)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_excitation_request",
            "band": self.band.to_dict(),
            "effective_peak_dbfs": self.effective_peak_dbfs,
            "duration_s": self.duration_s,
            "repeat_count": self.repeat_count,
            "target_fingerprint": self.target_fingerprint,
            "safety_profile_fingerprint": self.safety_profile_fingerprint,
            "authority_fingerprint": self.authority_fingerprint,
            "excitation_plan_fingerprint": self.excitation_plan_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return _content_fingerprint(self._payload())

    def to_dict(self) -> dict[str, object]:
        return _with_fingerprint(self._payload())

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        expected_fingerprint: str | None = None,
    ) -> ExcitationRequest:
        payload, declared = _read_fingerprinted_payload(
            value,
            fields=_REQUEST_FIELDS,
            artifact="excitation_request",
            expected_fingerprint=expected_fingerprint,
        )
        _require_schema(
            payload,
            kind="jts_excitation_request",
            artifact="excitation_request",
        )
        result = cls(
            band=FrequencyBand.from_dict(payload["band"]),
            effective_peak_dbfs=cast(float, payload["effective_peak_dbfs"]),
            duration_s=cast(float, payload["duration_s"]),
            repeat_count=cast(int, payload["repeat_count"]),
            target_fingerprint=cast(str | None, payload["target_fingerprint"]),
            safety_profile_fingerprint=cast(
                str | None,
                payload["safety_profile_fingerprint"],
            ),
            authority_fingerprint=cast(
                str | None,
                payload["authority_fingerprint"],
            ),
            excitation_plan_fingerprint=cast(
                str | None,
                payload["excitation_plan_fingerprint"],
            ),
        )
        if result.fingerprint != declared:
            raise ValueError("excitation_request is not canonically normalized")
        return result


_LIMIT_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "permitted_band",
        "maximum_effective_peak_dbfs",
        "maximum_duration_s",
        "maximum_repeat_count",
        "target_fingerprint",
        "safety_profile_fingerprint",
        "protection_requirement_fingerprint",
        "excitation_plan_fingerprint",
    }
)


@dataclass(frozen=True, slots=True)
class ExcitationLimits:
    """Caller-composed authority for one target, profile, and stimulus plan.

    ``permitted_band`` may be a hard-excitation band or a narrower measurement
    band.  One trusted feature adapter must intersect every applicable global,
    profile, and product limit before constructing this value.  Its content
    fingerprint covers every numeric limit and identity, so another caller
    cannot widen policy while retaining the old authority identity.

    ``protection_requirement_fingerprint`` identifies the exact normalized
    protection requirements the live/read-back proof must satisfy.
    ``excitation_plan_fingerprint`` binds this authority to the normalized
    stimulus kind, generator parameters, and effective-peak ledger.
    """

    permitted_band: FrequencyBand
    maximum_effective_peak_dbfs: float
    maximum_duration_s: float
    maximum_repeat_count: int
    target_fingerprint: str
    safety_profile_fingerprint: str
    protection_requirement_fingerprint: str
    excitation_plan_fingerprint: str

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
        for field in (
            "target_fingerprint",
            "safety_profile_fingerprint",
            "protection_requirement_fingerprint",
            "excitation_plan_fingerprint",
        ):
            object.__setattr__(
                self,
                field,
                _required_fingerprint(getattr(self, field), field=field),
            )
        object.__setattr__(self, "maximum_effective_peak_dbfs", peak)
        object.__setattr__(self, "maximum_duration_s", duration)
        object.__setattr__(self, "maximum_repeat_count", repeats)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_excitation_limits",
            "permitted_band": self.permitted_band.to_dict(),
            "maximum_effective_peak_dbfs": self.maximum_effective_peak_dbfs,
            "maximum_duration_s": self.maximum_duration_s,
            "maximum_repeat_count": self.maximum_repeat_count,
            "target_fingerprint": self.target_fingerprint,
            "safety_profile_fingerprint": self.safety_profile_fingerprint,
            "protection_requirement_fingerprint": (
                self.protection_requirement_fingerprint
            ),
            "excitation_plan_fingerprint": self.excitation_plan_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        """Content-derived authority identity used at both admission boundaries."""

        return _content_fingerprint(self._payload())

    def to_dict(self) -> dict[str, object]:
        return _with_fingerprint(self._payload())

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        expected_fingerprint: str | None = None,
    ) -> ExcitationLimits:
        payload, declared = _read_fingerprinted_payload(
            value,
            fields=_LIMIT_FIELDS,
            artifact="excitation_limits",
            expected_fingerprint=expected_fingerprint,
        )
        _require_schema(
            payload,
            kind="jts_excitation_limits",
            artifact="excitation_limits",
        )
        result = cls(
            permitted_band=FrequencyBand.from_dict(payload["permitted_band"]),
            maximum_effective_peak_dbfs=cast(
                float,
                payload["maximum_effective_peak_dbfs"],
            ),
            maximum_duration_s=cast(float, payload["maximum_duration_s"]),
            maximum_repeat_count=cast(int, payload["maximum_repeat_count"]),
            target_fingerprint=cast(str, payload["target_fingerprint"]),
            safety_profile_fingerprint=cast(
                str,
                payload["safety_profile_fingerprint"],
            ),
            protection_requirement_fingerprint=cast(
                str,
                payload["protection_requirement_fingerprint"],
            ),
            excitation_plan_fingerprint=cast(
                str,
                payload["excitation_plan_fingerprint"],
            ),
        )
        if result.fingerprint != declared:
            raise ValueError("excitation_limits are not canonically normalized")
        return result


_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "target_fingerprint",
        "safety_profile_fingerprint",
        "protection_requirement_fingerprint",
        "authority_fingerprint",
        "excitation_plan_fingerprint",
        "evidence_fingerprint",
        "current",
    }
)


@dataclass(frozen=True, slots=True)
class ProtectionEvidence:
    """Trusted-adapter proof that required protection is current and present.

    This leaf does not inspect a graph and construction alone does not establish
    trust.  The owning adapter must derive this value from fresh routing/filter
    read-back immediately before admission.  ``evidence_fingerprint`` names
    that concrete proof; the serialized artifact fingerprint binds it to the
    exact authority, excitation plan, target, profile, requirement, and
    freshness assertion below.
    """

    target_fingerprint: str | None
    safety_profile_fingerprint: str | None
    protection_requirement_fingerprint: str | None
    authority_fingerprint: str | None
    excitation_plan_fingerprint: str | None
    evidence_fingerprint: str | None
    current: bool

    def __post_init__(self) -> None:
        if type(self.current) is not bool:
            raise ValueError("current must be a bool")
        for field in (
            "target_fingerprint",
            "safety_profile_fingerprint",
            "protection_requirement_fingerprint",
            "authority_fingerprint",
            "excitation_plan_fingerprint",
            "evidence_fingerprint",
        ):
            object.__setattr__(
                self,
                field,
                _optional_fingerprint(getattr(self, field), field=field),
            )

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_excitation_protection_evidence",
            "target_fingerprint": self.target_fingerprint,
            "safety_profile_fingerprint": self.safety_profile_fingerprint,
            "protection_requirement_fingerprint": (
                self.protection_requirement_fingerprint
            ),
            "authority_fingerprint": self.authority_fingerprint,
            "excitation_plan_fingerprint": self.excitation_plan_fingerprint,
            "evidence_fingerprint": self.evidence_fingerprint,
            "current": self.current,
        }

    @property
    def fingerprint(self) -> str:
        return _content_fingerprint(self._payload())

    def to_dict(self) -> dict[str, object]:
        return _with_fingerprint(self._payload())

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        expected_fingerprint: str | None = None,
    ) -> ProtectionEvidence:
        payload, declared = _read_fingerprinted_payload(
            value,
            fields=_EVIDENCE_FIELDS,
            artifact="protection_evidence",
            expected_fingerprint=expected_fingerprint,
        )
        _require_schema(
            payload,
            kind="jts_excitation_protection_evidence",
            artifact="protection_evidence",
        )
        result = cls(
            target_fingerprint=cast(str | None, payload["target_fingerprint"]),
            safety_profile_fingerprint=cast(
                str | None,
                payload["safety_profile_fingerprint"],
            ),
            protection_requirement_fingerprint=cast(
                str | None,
                payload["protection_requirement_fingerprint"],
            ),
            authority_fingerprint=cast(
                str | None,
                payload["authority_fingerprint"],
            ),
            excitation_plan_fingerprint=cast(
                str | None,
                payload["excitation_plan_fingerprint"],
            ),
            evidence_fingerprint=cast(
                str | None,
                payload["evidence_fingerprint"],
            ),
            current=cast(bool, payload["current"]),
        )
        if result.fingerprint != declared:
            raise ValueError("protection_evidence is not canonically normalized")
        return result


class ExcitationRefusalReason(str, Enum):
    """Closed, machine-readable reasons an excitation request is refused."""

    TARGET_IDENTITY_MISSING = "target_identity_missing"
    TARGET_IDENTITY_MISMATCH = "target_identity_mismatch"
    SAFETY_PROFILE_IDENTITY_MISSING = "safety_profile_identity_missing"
    SAFETY_PROFILE_IDENTITY_MISMATCH = "safety_profile_identity_mismatch"
    AUTHORITY_IDENTITY_MISSING = "authority_identity_missing"
    AUTHORITY_IDENTITY_MISMATCH = "authority_identity_mismatch"
    EXCITATION_PLAN_IDENTITY_MISSING = "excitation_plan_identity_missing"
    EXCITATION_PLAN_IDENTITY_MISMATCH = "excitation_plan_identity_mismatch"
    PROTECTION_EVIDENCE_MISSING = "protection_evidence_missing"
    PROTECTION_EVIDENCE_STALE = "protection_evidence_stale"
    PROTECTION_TARGET_IDENTITY_MISSING = "protection_target_identity_missing"
    PROTECTION_TARGET_IDENTITY_MISMATCH = "protection_target_identity_mismatch"
    PROTECTION_PROFILE_IDENTITY_MISSING = "protection_profile_identity_missing"
    PROTECTION_PROFILE_IDENTITY_MISMATCH = "protection_profile_identity_mismatch"
    PROTECTION_REQUIREMENT_MISSING = "protection_requirement_missing"
    PROTECTION_REQUIREMENT_MISMATCH = "protection_requirement_mismatch"
    PROTECTION_AUTHORITY_MISSING = "protection_authority_missing"
    PROTECTION_AUTHORITY_MISMATCH = "protection_authority_mismatch"
    PROTECTION_PLAN_IDENTITY_MISSING = "protection_plan_identity_missing"
    PROTECTION_PLAN_IDENTITY_MISMATCH = "protection_plan_identity_mismatch"
    PROTECTION_PROOF_MISSING = "protection_proof_missing"
    FREQUENCY_BELOW_PERMITTED_BAND = "frequency_below_permitted_band"
    FREQUENCY_ABOVE_PERMITTED_BAND = "frequency_above_permitted_band"
    EFFECTIVE_PEAK_ABOVE_LIMIT = "effective_peak_above_limit"
    DURATION_ABOVE_LIMIT = "duration_above_limit"
    REPEAT_COUNT_ABOVE_LIMIT = "repeat_count_above_limit"


def _decision_reasons(
    request: ExcitationRequest,
    limits: ExcitationLimits,
    protection_evidence: ProtectionEvidence | None,
) -> tuple[ExcitationRefusalReason, ...]:
    reasons: list[ExcitationRefusalReason] = []

    if request.target_fingerprint is None:
        reasons.append(ExcitationRefusalReason.TARGET_IDENTITY_MISSING)
    elif request.target_fingerprint != limits.target_fingerprint:
        reasons.append(ExcitationRefusalReason.TARGET_IDENTITY_MISMATCH)

    if request.safety_profile_fingerprint is None:
        reasons.append(ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISSING)
    elif request.safety_profile_fingerprint != limits.safety_profile_fingerprint:
        reasons.append(ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISMATCH)

    if request.authority_fingerprint is None:
        reasons.append(ExcitationRefusalReason.AUTHORITY_IDENTITY_MISSING)
    elif request.authority_fingerprint != limits.fingerprint:
        reasons.append(ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH)

    if request.excitation_plan_fingerprint is None:
        reasons.append(ExcitationRefusalReason.EXCITATION_PLAN_IDENTITY_MISSING)
    elif request.excitation_plan_fingerprint != limits.excitation_plan_fingerprint:
        reasons.append(ExcitationRefusalReason.EXCITATION_PLAN_IDENTITY_MISMATCH)

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
        if protection_evidence.authority_fingerprint is None:
            reasons.append(ExcitationRefusalReason.PROTECTION_AUTHORITY_MISSING)
        elif protection_evidence.authority_fingerprint != limits.fingerprint:
            reasons.append(ExcitationRefusalReason.PROTECTION_AUTHORITY_MISMATCH)
        if protection_evidence.excitation_plan_fingerprint is None:
            reasons.append(ExcitationRefusalReason.PROTECTION_PLAN_IDENTITY_MISSING)
        elif (
            protection_evidence.excitation_plan_fingerprint
            != limits.excitation_plan_fingerprint
        ):
            reasons.append(ExcitationRefusalReason.PROTECTION_PLAN_IDENTITY_MISMATCH)
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

    return tuple(reasons)


_ADMISSION_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "request",
        "limits",
        "protection_evidence",
        "refusal_reasons",
        "allowed",
    }
)


@dataclass(frozen=True, slots=True)
class ExcitationAdmission:
    """Immutable, content-addressed result retaining exact decision inputs.

    Direct construction cannot forge an allow result: ``refusal_reasons`` are
    recomputed from the exact typed inputs and inconsistent values are rejected.
    Playback should still rerun :func:`admit_excitation`; an admission artifact
    is evidence of a decision, not a transferable playback capability.
    """

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
        expected = _decision_reasons(
            self.request,
            self.limits,
            self.protection_evidence,
        )
        if self.refusal_reasons != expected:
            raise ValueError("refusal_reasons do not match the exact decision inputs")

    @property
    def allowed(self) -> bool:
        return not self.refusal_reasons

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_excitation_admission",
            "request": self.request.to_dict(),
            "limits": self.limits.to_dict(),
            "protection_evidence": (
                self.protection_evidence.to_dict()
                if self.protection_evidence is not None
                else None
            ),
            "refusal_reasons": [reason.value for reason in self.refusal_reasons],
            "allowed": self.allowed,
        }

    @property
    def fingerprint(self) -> str:
        return _content_fingerprint(self._payload())

    def to_dict(self) -> dict[str, object]:
        return _with_fingerprint(self._payload())

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        expected_fingerprint: str | None = None,
    ) -> ExcitationAdmission:
        payload, declared = _read_fingerprinted_payload(
            value,
            fields=_ADMISSION_FIELDS,
            artifact="excitation_admission",
            expected_fingerprint=expected_fingerprint,
        )
        _require_schema(
            payload,
            kind="jts_excitation_admission",
            artifact="excitation_admission",
        )
        raw_reasons = payload["refusal_reasons"]
        if not isinstance(raw_reasons, list) or any(
            not isinstance(reason, str) for reason in raw_reasons
        ):
            raise ValueError("excitation_admission refusal reasons are invalid")
        try:
            reasons = tuple(ExcitationRefusalReason(reason) for reason in raw_reasons)
        except ValueError as exc:
            raise ValueError(
                "excitation_admission has an unknown refusal reason"
            ) from exc
        raw_evidence = payload["protection_evidence"]
        evidence = (
            None if raw_evidence is None else ProtectionEvidence.from_dict(raw_evidence)
        )
        result = cls(
            request=ExcitationRequest.from_dict(payload["request"]),
            limits=ExcitationLimits.from_dict(payload["limits"]),
            protection_evidence=evidence,
            refusal_reasons=reasons,
        )
        if type(payload["allowed"]) is not bool or payload["allowed"] != result.allowed:
            raise ValueError("excitation_admission allowed flag is inconsistent")
        if result.fingerprint != declared:
            raise ValueError("excitation_admission is not canonically normalized")
        return result


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

    return ExcitationAdmission(
        request=request,
        limits=limits,
        protection_evidence=protection_evidence,
        refusal_reasons=_decision_reasons(request, limits, protection_evidence),
    )
