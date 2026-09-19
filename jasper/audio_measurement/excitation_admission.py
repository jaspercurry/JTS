# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

SCHEMA_VERSION = 2
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
