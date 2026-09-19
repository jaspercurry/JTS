# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared driver fields and diagnostics."""

from __future__ import annotations

import math
import re
from typing import Any, Collection, Mapping, Sequence

from jasper.json_fields import JsonFields
from jasper.output_topology import SpeakerGroup


# Float round-trip noise only; must never bridge a real crossover setting change.
REGION_FC_MATCH_TOLERANCE_HZ = 1e-6


# See ADR-0019: a changed topology is a disclosure.
BASELINE_TOPOLOGY_CHANGED = "active_baseline_topology_changed"


DRIVER_CLASSES: tuple[str, ...] = (
    "compression_horn",
    "soft_dome",
    "metal_dome",
    "beryllium_diamond_dome",
    "ribbon_amt",
    "unknown",
)

# Tolerate retired fields in older research packets.
LEGACY_DROPPED_DRIVER_FIELDS: frozenset[str] = frozenset({
    "horn_coverage_deg",
    "crossover_search_band_hz",
    "target_fingerprint",
})

MANUAL_DRIVER_FIELDS = (
    "target_id", "role", "model", "manufacturer",
    "sensitivity_db_2v83_1m",
    "nominal_impedance_ohm",
    "driver_class",
    "radiating_diameter_mm",
    "do_not_test_below_hz",
    "gain_offset_db",
    "gain_offset_db_provenance",
    "recommended_highpass_hz",
    "recommended_highpass_slope_db_per_octave",
    "hard_excitation_band_hz",
    "measurement_band_hz",
    "required_protection_filters",
    "level_duration_limits",
    "cabinet",
    "usable_frequency_range_hz",
    "recommended_lowpass_hz",
    "fit_budget",
    "notes", "pad", "installation", "source",
)
DRIVER_RESEARCH_FIELDS = frozenset(MANUAL_DRIVER_FIELDS) | {
    "sources", "unknowns", "field_provenance",
}
MANUAL_CANDIDATE_FIELDS = {
    "between_roles", "frequency_hz", "filter_type", "slope_db_per_octave",
    "confidence", "rationale", "warnings", "lower_polarity", "upper_polarity",
    "delay_ms", "delay_target_role", "source",
}
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def software_guard_needed(groups: Sequence[SpeakerGroup]) -> bool:
    return any(
        channel.role == "tweeter"
        for group in groups for channel in group.channels
    )


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def blocker_issue(code: str, message: str) -> dict[str, str]:
    return issue("blocker", code, message)


def gate(gate_id: str, *, label: str, passed: bool, message: str) -> dict[str, Any]:
    return {
        "id": gate_id,
        "label": label,
        "passed": bool(passed),
        "message": message,
    }


def finite_float(value: Any) -> float | None:
    """Accept numeric strings and booleans."""

    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def bounded_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    """Coerce an integer and clamp it to the inclusive ``lo``/``hi`` range."""

    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


def require_sha256_hex(
    value: Any,
    field_name: str,
    exc_type: type[BaseException],
    *,
    message: str | None = None,
) -> str:
    """Require a lowercase SHA-256 digest."""

    if isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None:
        return value
    raise exc_type(
        message
        if message is not None
        else f"{field_name} must be a lowercase SHA-256 fingerprint"
    )


class DriverFields(JsonFields):
    def _text(
        self, raw: Any, field_name: str, *, required: bool = False, max_chars: int = 240,
    ) -> str | None:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if required:
                raise self.error_type(f"{field_name} is required", code="field_required")
            return None
        if not isinstance(raw, str):
            raise self.error_type(f"{field_name} must be a string", code="field_not_string")
        return super().text(raw, field_name, max_length=max_chars)

    def _finite_float(self, raw: Any, field_name: str) -> float | None:
        if isinstance(raw, bool):
            raise self.error_type(f"{field_name} must be numeric", code="field_not_numeric")
        return self.optional_number(raw, field_name)

    def _positive_float(self, raw: Any, field_name: str) -> float | None:
        out = self._finite_float(raw, field_name)
        if out is not None and out <= 0:
            raise self.error_type(f"{field_name} must be > 0", code="field_not_positive")
        return out

    def _sequence(self, raw: Any, field_name: str, *, limit: int | None = None) -> list[Any]:
        out = [] if raw is None else super().sequence(raw, field_name)
        if limit is not None and len(out) > limit:
            raise self.error_type(f"{field_name} must contain <= {limit} items", code="field_too_many_items")
        return out

    def _reject_unknown_keys(
        self, raw: Mapping[str, Any], field_name: str, allowed: Collection[str],
    ) -> None:
        unknown = sorted(str(key) for key in raw if key not in allowed)
        if unknown:
            raise self.error_type(
                f"{field_name} has unknown fields: {', '.join(unknown)}",
                code="unknown_driver_fields",
            )
