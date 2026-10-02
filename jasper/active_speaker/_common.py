# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared driver fields and diagnostics."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Collection, Mapping, Sequence

from jasper.platform.json_fields import JsonFields, issue

if TYPE_CHECKING:
    from jasper.audio_routes.output_topology import SpeakerGroup


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
REIMPORT_RESEARCH = "; import the research again at /sound/speaker/ with the current prompt"
MINIMUM_CROSSOVER_LABEL = "Minimum crossover (Hz)"
MANUAL_CANDIDATE_FIELDS = {
    "between_roles", "frequency_hz", "filter_type", "slope_db_per_octave",
    "confidence", "rationale", "warnings", "lower_polarity", "upper_polarity",
    "delay_ms", "delay_target_role", "source",
}


def software_guard_needed(groups: Sequence[SpeakerGroup]) -> bool:
    return any(
        channel.role == "tweeter"
        for group in groups for channel in group.channels
    )


def blocker_issue(code: str, message: str) -> dict[str, str]:
    return issue("blocker", code, message)


def gate(gate_id: str, *, label: str, passed: bool, message: str) -> dict[str, Any]:
    return {
        "id": gate_id,
        "label": label,
        "passed": bool(passed),
        "message": message,
    }


def coerce_finite_float(value: Any) -> float | None:
    """Accept numeric strings and booleans; ``json_fields.finite_float`` refuses both."""

    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None



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
        self, raw: Mapping[str, Any], field_name: str, allowed: Collection[str], remedy: str = "",
    ) -> None:
        unknown = sorted(str(key) for key in raw if key not in allowed)
        if unknown:
            raise self.error_type(
                f"{field_name} has unknown fields: {', '.join(unknown)}{remedy}",
                code="unknown_driver_fields",
            )


class MeasurementGraphRefused(ValueError):
    def __init__(self, reason: str, detail: Any) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")

    @property
    def code(self) -> str:
        return self.reason
