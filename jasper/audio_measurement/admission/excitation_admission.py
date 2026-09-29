# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from jasper.platform.json_fields import json_fingerprint, require_finite, require_sha256_hex

SCHEMA_VERSION = 2


def _finite_number(value: object, *, field: str) -> float:
    number = require_finite(value, field=field)
    # JSON distinguishes -0.0 from 0.0 even though the safety policy does not.
    # Normalize it so equal numeric authority has one canonical fingerprint.
    return 0.0 if number == 0.0 else number


def _positive_number(value: object, *, field: str) -> float:
    return require_finite(value, field=field, positive=True)


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_fingerprint(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return require_sha256_hex(value, field=field)


def _with_fingerprint(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    result["fingerprint"] = json_fingerprint(payload)
    return result


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
        return json_fingerprint(self._payload())

    def to_dict(self) -> dict[str, object]:
        return _with_fingerprint(self._payload())


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
                require_sha256_hex(getattr(self, field), field=field),
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

        return json_fingerprint(self._payload())

    def to_dict(self) -> dict[str, object]:
        return _with_fingerprint(self._payload())
