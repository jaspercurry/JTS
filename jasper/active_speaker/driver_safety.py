# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver-research request and confirmed safety-profile contracts.

This module is deliberately silent.  It turns the current physical active-
speaker targets plus operator-visible limits into immutable JSON contracts; it
does not generate a signal, compile a filter, load CamillaDSP, or grant
playback permission.

Research remains advice.  A version-2 research result must echo the exact
server-authored request and target identities, but only the values visible in
``manual_settings`` enter the confirmed safety profile.  Downstream audio code
must additionally perform its own excitation and live-graph admission checks.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from jasper.output_topology import OutputTopology

from .driver_protection import (
    DRIVER_PROTECTION_POLICY_VERSION,
    driver_protection_profile,
)
from .measurement import active_driver_targets

DRIVER_RESEARCH_KIND = "jts_active_crossover_driver_research"
DRIVER_RESEARCH_REQUEST_KIND = "jts_active_crossover_driver_research_request"
DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION = 1
DRIVER_RESEARCH_RESULT_SCHEMA_VERSION = 2

DRIVER_SAFETY_PROFILE_KIND = "jts_active_speaker_driver_safety_profile"
DRIVER_SAFETY_PROFILE_SCHEMA_VERSION = 1

SUPPORTED_ENCLOSURE_KINDS = {
    "sealed",
    "vented",
    "passive_radiator",
    "open_baffle",
    "transmission_line",
    "unknown",
}
SUPPORTED_PROTECTION_KINDS = {"highpass", "lowpass"}
SUPPORTED_FIELD_CONFIDENCE = {"low", "medium", "high", "unknown"}
MAX_UNKNOWNS = 32
MAX_PROVENANCE_FIELDS = 32
MAX_PROVENANCE_SOURCES = 8

_MANUAL_SETTINGS_FIELDS = {"drivers", "crossover_candidates"}
_MANUAL_DRIVER_FIELDS = {
    "target_id",
    "role",
    "model",
    "manufacturer",
    "nominal_impedance_ohm",
    "sensitivity_db_2v83_1m",
    "usable_frequency_range_hz",
    "recommended_highpass_hz",
    "recommended_lowpass_hz",
    "do_not_test_below_hz",
    "gain_offset_db",
    "gain_offset_db_provenance",
    "notes",
    "hard_excitation_band_hz",
    "required_protection_filters",
    "measurement_band_hz",
    "crossover_search_band_hz",
    "level_duration_limits",
    "cabinet",
    "source",
}
_MANUAL_CANDIDATE_FIELDS = {
    "between_roles",
    "frequency_hz",
    "filter_type",
    "slope_db_per_octave",
    "confidence",
    "rationale",
    "warnings",
    "lower_polarity",
    "upper_polarity",
    "delay_ms",
    "delay_target_role",
    "source",
}


class DriverSafetyProfileError(ValueError):
    """Raised when research or safety-profile input is malformed."""


@dataclass(frozen=True)
class DriverSafetyProfileEvaluation:
    """Fail-closed freshness result for one persisted safety profile.

    ``confirmed_and_current`` describes only this contract.  It is explicitly
    not permission to emit audio; excitation and live protected-graph checks
    remain separate downstream gates.
    """

    status: str
    confirmed_and_current: bool
    profile_fingerprint: str | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confirmed_and_current": self.confirmed_and_current,
            "profile_fingerprint": self.profile_fingerprint,
            "reasons": list(self.reasons),
            "authorizes_playback": False,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _text(
    value: Any,
    field_name: str,
    *,
    required: bool = False,
    max_chars: int = 320,
) -> str | None:
    if value in (None, ""):
        if required:
            raise DriverSafetyProfileError(f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise DriverSafetyProfileError(f"{field_name} must be a string")
    out = " ".join(value.split())
    if not out:
        if required:
            raise DriverSafetyProfileError(f"{field_name} is required")
        return None
    if len(out) > max_chars:
        raise DriverSafetyProfileError(f"{field_name} must be <= {max_chars} chars")
    return out


def _finite_float(value: Any, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise DriverSafetyProfileError(f"{field_name} must be numeric")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise DriverSafetyProfileError(f"{field_name} must be numeric") from exc
    if not math.isfinite(out):
        raise DriverSafetyProfileError(f"{field_name} must be finite")
    return out


def _positive_float(value: Any, field_name: str) -> float | None:
    out = _finite_float(value, field_name)
    if out is not None and out <= 0:
        raise DriverSafetyProfileError(f"{field_name} must be > 0")
    return out


def _bounded_int(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise DriverSafetyProfileError(f"{field_name} must be an integer")
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise DriverSafetyProfileError(f"{field_name} must be an integer") from exc
    if isinstance(value, float) and value != out:
        raise DriverSafetyProfileError(f"{field_name} must be an integer")
    if not minimum <= out <= maximum:
        raise DriverSafetyProfileError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return out


def _sequence(
    value: Any,
    field_name: str,
    *,
    maximum: int,
) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DriverSafetyProfileError(f"{field_name} must be a list")
    if len(value) > maximum:
        raise DriverSafetyProfileError(f"{field_name} must contain <= {maximum} items")
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any],
    field_name: str,
    allowed: set[str],
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise DriverSafetyProfileError(
            f"{field_name} has unknown fields: {', '.join(unknown)}"
        )


def _reject_bool_tree(value: Any, field_name: str) -> None:
    if isinstance(value, bool):
        raise DriverSafetyProfileError(f"{field_name} must not be boolean")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_bool_tree(item, f"{field_name}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_bool_tree(item, f"{field_name}[{index}]")


def _frequency_band(value: Any, field_name: str) -> list[float] | None:
    if value is None:
        return None
    items = _sequence(value, field_name, maximum=2)
    if len(items) != 2:
        raise DriverSafetyProfileError(f"{field_name} must contain two values")
    low = _positive_float(items[0], f"{field_name}[0]")
    high = _positive_float(items[1], f"{field_name}[1]")
    if low is None or high is None or low >= high:
        raise DriverSafetyProfileError(f"{field_name} must be an increasing range")
    return [low, high]


def _normalise_protection_filters(value: Any, field_name: str) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(_sequence(value, field_name, maximum=2)):
        prefix = f"{field_name}[{index}]"
        if not isinstance(raw, Mapping):
            raise DriverSafetyProfileError(f"{prefix} must be an object")
        _reject_unknown_keys(
            raw,
            prefix,
            {
                "kind",
                "cutoff_hz",
                "minimum_slope_db_per_octave",
                "family_or_equivalent",
            },
        )
        kind = _text(raw.get("kind"), f"{prefix}.kind", required=True, max_chars=20)
        if kind not in SUPPORTED_PROTECTION_KINDS:
            raise DriverSafetyProfileError(f"{prefix}.kind must be highpass or lowpass")
        if kind in seen:
            raise DriverSafetyProfileError(
                f"{field_name} may contain only one {kind} requirement"
            )
        seen.add(kind)
        cutoff = _positive_float(raw.get("cutoff_hz"), f"{prefix}.cutoff_hz")
        slope = _positive_float(
            raw.get("minimum_slope_db_per_octave"),
            f"{prefix}.minimum_slope_db_per_octave",
        )
        if cutoff is None or slope is None:
            raise DriverSafetyProfileError(
                f"{prefix} requires cutoff_hz and minimum_slope_db_per_octave"
            )
        if slope > 96:
            raise DriverSafetyProfileError(
                f"{prefix}.minimum_slope_db_per_octave must be <= 96"
            )
        family = _text(
            raw.get("family_or_equivalent") or "equivalent_or_steeper",
            f"{prefix}.family_or_equivalent",
            max_chars=80,
        )
        if family != "equivalent_or_steeper":
            raise DriverSafetyProfileError(
                f"{prefix}.family_or_equivalent must be equivalent_or_steeper"
            )
        filters.append(
            {
                "kind": kind,
                "cutoff_hz": cutoff,
                "minimum_slope_db_per_octave": slope,
                "family_or_equivalent": family,
            }
        )
    return sorted(filters, key=lambda item: str(item["kind"]))


def _normalise_cabinet(value: Any, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DriverSafetyProfileError(f"{field_name} must be an object")
    _reject_unknown_keys(
        value,
        field_name,
        {
            "enclosure_kind",
            "radiator_count",
            "effective_radiating_diameter_mm",
            "baffle_width_mm",
            "lf_reconstruction_capability",
        },
    )
    enclosure = (
        _text(
            value.get("enclosure_kind") or "unknown",
            f"{field_name}.enclosure_kind",
            max_chars=40,
        )
        or "unknown"
    )
    if enclosure not in SUPPORTED_ENCLOSURE_KINDS:
        raise DriverSafetyProfileError(
            f"{field_name}.enclosure_kind is unsupported: {enclosure}"
        )
    radiator_count = _bounded_int(
        value.get("radiator_count"),
        f"{field_name}.radiator_count",
        minimum=1,
        maximum=16,
    )
    diameter = _positive_float(
        value.get("effective_radiating_diameter_mm"),
        f"{field_name}.effective_radiating_diameter_mm",
    )
    baffle_width = _positive_float(
        value.get("baffle_width_mm"),
        f"{field_name}.baffle_width_mm",
    )
    if (
        enclosure == "sealed"
        and radiator_count == 1
        and diameter is not None
        and baffle_width is not None
    ):
        reconstruction = "sealed_single_radiator_supported"
    elif enclosure == "unknown":
        reconstruction = "refused_unknown_enclosure"
    elif enclosure in {"vented", "passive_radiator"}:
        reconstruction = "refused_multi_radiator_contract_missing"
    elif radiator_count != 1:
        reconstruction = "refused_single_radiator_contract_not_proven"
    else:
        reconstruction = "refused_geometry_incomplete"
    out: dict[str, Any] = {
        "enclosure_kind": enclosure,
        "lf_reconstruction_capability": reconstruction,
    }
    if radiator_count is not None:
        out["radiator_count"] = radiator_count
    if diameter is not None:
        out["effective_radiating_diameter_mm"] = diameter
    if baffle_width is not None:
        out["baffle_width_mm"] = baffle_width
    return out


def _normalise_level_duration_limits(
    value: Any,
    field_name: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DriverSafetyProfileError(f"{field_name} must be an object")
    _reject_unknown_keys(
        value,
        field_name,
        {
            "max_effective_peak_dbfs",
            "max_sweep_duration_s",
            "max_repeat_count",
            "minimum_cooldown_s",
        },
    )
    peak = _finite_float(
        value.get("max_effective_peak_dbfs"),
        f"{field_name}.max_effective_peak_dbfs",
    )
    if peak is not None and peak > 0:
        raise DriverSafetyProfileError(
            f"{field_name}.max_effective_peak_dbfs must be <= 0"
        )
    duration = _positive_float(
        value.get("max_sweep_duration_s"),
        f"{field_name}.max_sweep_duration_s",
    )
    repeats = _bounded_int(
        value.get("max_repeat_count"),
        f"{field_name}.max_repeat_count",
        minimum=1,
        maximum=16,
    )
    cooldown = _finite_float(
        value.get("minimum_cooldown_s"),
        f"{field_name}.minimum_cooldown_s",
    )
    if cooldown is not None and cooldown < 0:
        raise DriverSafetyProfileError(f"{field_name}.minimum_cooldown_s must be >= 0")
    out = {
        "max_effective_peak_dbfs": peak,
        "max_sweep_duration_s": duration,
        "max_repeat_count": repeats,
        "minimum_cooldown_s": cooldown,
    }
    return {key: item for key, item in out.items() if item is not None} or None


def _normalise_unknowns(value: Any, field_name: str) -> list[str]:
    unknowns: list[str] = []
    for index, raw in enumerate(_sequence(value, field_name, maximum=MAX_UNKNOWNS)):
        item = _text(raw, f"{field_name}[{index}]", required=True, max_chars=160)
        if item and item not in unknowns:
            unknowns.append(item)
    return unknowns


def _normalise_field_provenance(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DriverSafetyProfileError(f"{field_name} must be an object")
    if len(value) > MAX_PROVENANCE_FIELDS:
        raise DriverSafetyProfileError(
            f"{field_name} must contain <= {MAX_PROVENANCE_FIELDS} fields"
        )
    out: dict[str, Any] = {}
    for raw_key, raw_assertion in value.items():
        key = _text(raw_key, f"{field_name} key", required=True, max_chars=80)
        if not isinstance(raw_assertion, Mapping):
            raise DriverSafetyProfileError(f"{field_name}.{key} must be an object")
        _reject_unknown_keys(
            raw_assertion,
            f"{field_name}.{key}",
            {"confidence", "basis", "sources"},
        )
        confidence = (
            _text(
                raw_assertion.get("confidence") or "unknown",
                f"{field_name}.{key}.confidence",
                max_chars=20,
            )
            or "unknown"
        )
        if confidence not in SUPPORTED_FIELD_CONFIDENCE:
            raise DriverSafetyProfileError(
                f"{field_name}.{key}.confidence is unsupported"
            )
        basis = _text(
            raw_assertion.get("basis"),
            f"{field_name}.{key}.basis",
            required=True,
            max_chars=240,
        )
        sources: list[str] = []
        for index, raw_source in enumerate(
            _sequence(
                raw_assertion.get("sources"),
                f"{field_name}.{key}.sources",
                maximum=MAX_PROVENANCE_SOURCES,
            )
        ):
            source = _text(
                raw_source,
                f"{field_name}.{key}.sources[{index}]",
                required=True,
                max_chars=320,
            )
            if source and source not in sources:
                sources.append(source)
        out[str(key)] = {
            "confidence": confidence,
            "basis": basis,
            "sources": sources,
        }
    return out


def normalise_driver_safety_fields(
    value: Any,
    field_name: str,
    *,
    include_research_evidence: bool,
) -> dict[str, Any]:
    """Normalize the safety fields shared by research and visible settings."""

    if not isinstance(value, Mapping):
        raise DriverSafetyProfileError(f"{field_name} must be an object")
    out: dict[str, Any] = {}
    for key in (
        "hard_excitation_band_hz",
        "measurement_band_hz",
        "crossover_search_band_hz",
    ):
        band = _frequency_band(value.get(key), f"{field_name}.{key}")
        if band is not None:
            out[key] = band
    if "required_protection_filters" in value:
        out["required_protection_filters"] = _normalise_protection_filters(
            value.get("required_protection_filters"),
            f"{field_name}.required_protection_filters",
        )
    cabinet = _normalise_cabinet(value.get("cabinet"), f"{field_name}.cabinet")
    if cabinet is not None:
        out["cabinet"] = cabinet
    limits = _normalise_level_duration_limits(
        value.get("level_duration_limits"),
        f"{field_name}.level_duration_limits",
    )
    if limits is not None:
        out["level_duration_limits"] = limits
    if include_research_evidence:
        out["target_id"] = _text(
            value.get("target_id"),
            f"{field_name}.target_id",
            required=True,
            max_chars=160,
        )
        target_fingerprint = _text(
            value.get("target_fingerprint"),
            f"{field_name}.target_fingerprint",
            required=True,
            max_chars=64,
        )
        if not _is_sha256(target_fingerprint):
            raise DriverSafetyProfileError(
                f"{field_name}.target_fingerprint must be a lowercase SHA-256"
            )
        out["target_fingerprint"] = target_fingerprint
        out["unknowns"] = _normalise_unknowns(
            value.get("unknowns"), f"{field_name}.unknowns"
        )
        out["field_provenance"] = _normalise_field_provenance(
            value.get("field_provenance"), f"{field_name}.field_provenance"
        )
    return out


_V2_RESEARCH_TOP_LEVEL_FIELDS = {
    "artifact_schema_version",
    "kind",
    "request_fingerprint",
    "result_fingerprint",
    "drivers",
    "crossover_candidates",
    "human_review",
}
_V2_RESEARCH_DRIVER_FIELDS = {
    "target_id",
    "target_fingerprint",
    "role",
    "model",
    "manufacturer",
    "nominal_impedance_ohm",
    "sensitivity_db_2v83_1m",
    "usable_frequency_range_hz",
    "recommended_highpass_hz",
    "recommended_lowpass_hz",
    "do_not_test_below_hz",
    "hard_excitation_band_hz",
    "required_protection_filters",
    "measurement_band_hz",
    "crossover_search_band_hz",
    "level_duration_limits",
    "cabinet",
    "unknowns",
    "field_provenance",
    "gain_offset_db",
    "gain_offset_db_provenance",
    "notes",
    "sources",
}
_V2_RESEARCH_CANDIDATE_FIELDS = {
    "between_roles",
    "frequency_hz",
    "filter_type",
    "slope_db_per_octave",
    "confidence",
    "rationale",
    "warnings",
    "lower_polarity",
    "upper_polarity",
    "delay_ms",
    "delay_target_role",
}


def validate_driver_research_result_shape(raw: Any) -> None:
    """Reject ambiguous or extension-by-typo fields in the v2 result schema."""

    if not isinstance(raw, Mapping):
        raise DriverSafetyProfileError("driver_research must be an object")
    _reject_unknown_keys(
        raw,
        "driver_research",
        _V2_RESEARCH_TOP_LEVEL_FIELDS,
    )
    if type(raw.get("artifact_schema_version")) is not int:  # noqa: E721
        raise DriverSafetyProfileError(
            "driver_research.artifact_schema_version must be integer 2"
        )
    if raw.get("artifact_schema_version") != DRIVER_RESEARCH_RESULT_SCHEMA_VERSION:
        raise DriverSafetyProfileError(
            "driver_research.artifact_schema_version must be integer 2"
        )
    if raw.get("kind") != DRIVER_RESEARCH_KIND:
        raise DriverSafetyProfileError(
            f"driver_research.kind must be {DRIVER_RESEARCH_KIND}"
        )
    if not _is_sha256(raw.get("request_fingerprint")):
        raise DriverSafetyProfileError(
            "driver_research.request_fingerprint must be a lowercase SHA-256"
        )
    for index, driver in enumerate(
        _sequence(raw.get("drivers"), "driver_research.drivers", maximum=16)
    ):
        if not isinstance(driver, Mapping):
            raise DriverSafetyProfileError(
                f"driver_research.drivers[{index}] must be an object"
            )
        _reject_unknown_keys(
            driver,
            f"driver_research.drivers[{index}]",
            _V2_RESEARCH_DRIVER_FIELDS,
        )
        _reject_bool_tree(driver, f"driver_research.drivers[{index}]")
    for index, candidate in enumerate(
        _sequence(
            raw.get("crossover_candidates"),
            "driver_research.crossover_candidates",
            maximum=8,
        )
    ):
        if not isinstance(candidate, Mapping):
            raise DriverSafetyProfileError(
                f"driver_research.crossover_candidates[{index}] must be an object"
            )
        _reject_unknown_keys(
            candidate,
            f"driver_research.crossover_candidates[{index}]",
            _V2_RESEARCH_CANDIDATE_FIELDS,
        )
        _reject_bool_tree(
            candidate,
            f"driver_research.crossover_candidates[{index}]",
        )


def build_driver_research_request(
    topology: OutputTopology,
    operator_inputs: Mapping[str, Any],
    manual_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exact physical-target-bound request copied by ``/sound/``."""

    validate_manual_target_bindings(topology, manual_settings)
    manual_by_role = _manual_by_role(manual_settings)
    manual_by_target = _manual_by_target(manual_settings)
    role_counts: dict[str, int] = {}
    for target in active_driver_targets(topology):
        role = str(target.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    driver_styles = {
        f"{group.id}:{channel.role}": channel.driver_style
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.driver_style
    }
    targets: list[dict[str, Any]] = []
    for target in active_driver_targets(topology):
        role = str(target.get("role") or "")
        target_id = str(target["target_id"])
        target_models = operator_inputs.get("target_models")
        target_models = target_models if isinstance(target_models, Mapping) else {}
        model_value = target_models.get(target_id)
        if model_value in (None, "") and role_counts.get(role) == 1:
            model_value = operator_inputs.get(role)
        model = _text(
            model_value,
            f"operator_inputs.target_models.{target_id}",
            required=True,
            max_chars=160,
        )
        visible = manual_by_target.get(target_id)
        if visible is None and role_counts.get(role) == 1:
            visible = manual_by_role.get(role, {})
        visible = visible or {}
        declared_context = (
            normalise_driver_safety_fields(
                visible,
                f"manual_settings.{role}",
                include_research_evidence=False,
            )
            if visible
            else {}
        )
        notes = _text(
            visible.get("notes"),
            f"manual_settings.{role}.notes",
            max_chars=2048,
        )
        if notes:
            declared_context["operator_notes"] = notes
        request_target = {
            "target_id": target_id,
            "target_fingerprint": str(target["target_fingerprint"]),
            "speaker_group_id": str(target["speaker_group_id"]),
            "speaker_group_mode": str(target["speaker_group_mode"]),
            "role": role,
            "driver_style": driver_styles.get(str(target["target_id"]))
            or "unspecified",
            "physical_output_index": target.get("output_index"),
            "physical_output_label": target.get("output_label"),
            "manufacturer_and_model": model,
            "operator_declared_context": declared_context or None,
        }
        targets.append(
            {key: value for key, value in request_target.items() if value is not None}
        )
    if not targets:
        raise DriverSafetyProfileError(
            "driver research requires an active two-way or three-way topology"
        )
    core: dict[str, Any] = {
        "artifact_schema_version": DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION,
        "kind": DRIVER_RESEARCH_REQUEST_KIND,
        "topology_id": topology.topology_id,
        "hardware": topology.hardware.to_dict(),
        "targets": targets,
        "build_notes": _text(
            operator_inputs.get("notes"),
            "operator_inputs.notes",
            max_chars=1000,
        ),
    }
    core = {key: value for key, value in core.items() if value is not None}
    return {**core, "request_fingerprint": _fingerprint(core)}


def validate_driver_research_request(
    request: Any,
    topology: OutputTopology,
    operator_inputs: Mapping[str, Any],
    manual_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a canonical current request or refuse stale/self-invalid input."""

    if not isinstance(request, Mapping):
        raise DriverSafetyProfileError("driver_research_request must be an object")
    _reject_unknown_keys(
        request,
        "driver_research_request",
        {
            "artifact_schema_version",
            "kind",
            "topology_id",
            "hardware",
            "targets",
            "build_notes",
            "request_fingerprint",
        },
    )
    if (
        type(request.get("artifact_schema_version")) is not int  # noqa: E721
        or request.get("artifact_schema_version")
        != DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION
        or request.get("kind") != DRIVER_RESEARCH_REQUEST_KIND
    ):
        raise DriverSafetyProfileError(
            "driver_research_request schema or kind is unsupported"
        )
    if request.get("topology_id") != topology.topology_id:
        raise DriverSafetyProfileError(
            "driver_research_request topology does not match the current topology"
        )
    if request.get("hardware") != topology.hardware.to_dict():
        raise DriverSafetyProfileError(
            "driver_research_request hardware does not match the current topology"
        )
    raw_targets = _sequence(
        request.get("targets"),
        "driver_research_request.targets",
        maximum=16,
    )
    current_targets = active_driver_targets(topology)
    role_counts: dict[str, int] = {}
    for target in current_targets:
        role = str(target.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    if len(raw_targets) != len(current_targets):
        raise DriverSafetyProfileError(
            "driver_research_request targets do not exactly match the current physical targets"
        )
    driver_styles = {
        f"{group.id}:{channel.role}": channel.driver_style
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.driver_style
    }
    targets: list[dict[str, Any]] = []
    for index, (raw_target, current) in enumerate(zip(raw_targets, current_targets)):
        field_name = f"driver_research_request.targets[{index}]"
        if not isinstance(raw_target, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        _reject_unknown_keys(
            raw_target,
            field_name,
            {
                "target_id",
                "target_fingerprint",
                "speaker_group_id",
                "speaker_group_mode",
                "role",
                "driver_style",
                "physical_output_index",
                "physical_output_label",
                "manufacturer_and_model",
                "operator_declared_context",
            },
        )
        _reject_bool_tree(raw_target, field_name)
        role = str(current["role"])
        model = _text(
            raw_target.get("manufacturer_and_model"),
            f"{field_name}.manufacturer_and_model",
            required=True,
            max_chars=160,
        )
        target_models = operator_inputs.get("target_models")
        target_models = target_models if isinstance(target_models, Mapping) else {}
        current_model_value = target_models.get(str(current["target_id"]))
        if current_model_value in (None, "") and role_counts.get(role) == 1:
            current_model_value = operator_inputs.get(role)
        current_model = _text(
            current_model_value,
            f"operator_inputs.target_models.{current['target_id']}",
            required=True,
            max_chars=160,
        )
        expected_fields = {
            "target_id": str(current["target_id"]),
            "target_fingerprint": str(current["target_fingerprint"]),
            "speaker_group_id": str(current["speaker_group_id"]),
            "speaker_group_mode": str(current["speaker_group_mode"]),
            "role": role,
            "physical_output_index": current.get("output_index"),
            "physical_output_label": current.get("output_label"),
        }
        for key, expected in expected_fields.items():
            if raw_target.get(key) != expected:
                raise DriverSafetyProfileError(
                    "driver_research_request targets do not exactly match "
                    "the current physical targets"
                )
        if model != current_model:
            raise DriverSafetyProfileError(
                f"driver_research_request model is stale for {role}"
            )
        expected_style = driver_styles.get(str(current["target_id"])) or "unspecified"
        if raw_target.get("driver_style") != expected_style:
            if raw_target.get("driver_style") is not None or expected_style is not None:
                raise DriverSafetyProfileError(
                    "driver_research_request driver style is stale"
                )
        context_raw = raw_target.get("operator_declared_context")
        context: dict[str, Any] = {}
        if context_raw is not None:
            if not isinstance(context_raw, Mapping):
                raise DriverSafetyProfileError(
                    f"{field_name}.operator_declared_context must be an object"
                )
            _reject_unknown_keys(
                context_raw,
                f"{field_name}.operator_declared_context",
                {
                    "hard_excitation_band_hz",
                    "required_protection_filters",
                    "measurement_band_hz",
                    "crossover_search_band_hz",
                    "cabinet",
                    "level_duration_limits",
                    "operator_notes",
                },
            )
            context = normalise_driver_safety_fields(
                context_raw,
                f"{field_name}.operator_declared_context",
                include_research_evidence=False,
            )
            notes = _text(
                context_raw.get("operator_notes")
                if isinstance(context_raw, Mapping)
                else None,
                f"{field_name}.operator_declared_context.operator_notes",
                max_chars=2048,
            )
            if notes:
                context["operator_notes"] = notes
        target = {
            **expected_fields,
            "driver_style": expected_style,
            "manufacturer_and_model": model,
            "operator_declared_context": context or None,
        }
        targets.append(
            {key: value for key, value in target.items() if value is not None}
        )
    core: dict[str, Any] = {
        "artifact_schema_version": DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION,
        "kind": DRIVER_RESEARCH_REQUEST_KIND,
        "topology_id": topology.topology_id,
        "hardware": topology.hardware.to_dict(),
        "targets": targets,
        "build_notes": _text(
            request.get("build_notes"),
            "driver_research_request.build_notes",
            max_chars=1000,
        ),
    }
    core = {key: value for key, value in core.items() if value is not None}
    fingerprint = request.get("request_fingerprint")
    if not _is_sha256(fingerprint) or fingerprint != _fingerprint(core):
        raise DriverSafetyProfileError("driver_research_request fingerprint is invalid")
    canonical = {**core, "request_fingerprint": fingerprint}
    expected = build_driver_research_request(
        topology,
        operator_inputs,
        manual_settings,
    )
    if _canonical_json(canonical) != _canonical_json(expected):
        raise DriverSafetyProfileError(
            "driver_research_request is stale for the current visible inputs"
        )
    return canonical


def validate_research_result_binding(
    result: Mapping[str, Any],
    expected_request: Mapping[str, Any],
) -> None:
    """Refuse a v2 result that is stale, incomplete, or target-mismatched."""

    expected_fingerprint = expected_request.get("request_fingerprint")
    if result.get("request_fingerprint") != expected_fingerprint:
        raise DriverSafetyProfileError(
            "driver_research.request_fingerprint does not match the current request"
        )
    expected = {
        str(target.get("target_id")): (
            str(target.get("target_fingerprint")),
            str(target.get("role")),
            str(target.get("manufacturer_and_model")),
        )
        for target in expected_request.get("targets", [])
        if isinstance(target, Mapping)
    }
    observed: dict[str, tuple[str, str, str]] = {}
    for driver in result.get("drivers", []):
        if not isinstance(driver, Mapping):
            continue
        target_id = str(driver.get("target_id") or "")
        target_fingerprint = str(driver.get("target_fingerprint") or "")
        if target_id in observed:
            raise DriverSafetyProfileError(
                f"driver_research has duplicate target_id: {target_id}"
            )
        observed[target_id] = (
            target_fingerprint,
            str(driver.get("role") or ""),
            str(driver.get("model") or ""),
        )
    if observed != expected:
        raise DriverSafetyProfileError(
            "driver_research targets do not exactly match the current physical targets"
        )


def finalise_research_result(
    result: Mapping[str, Any],
    expected_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate binding and add the server-computed immutable result digest."""

    validate_research_result_binding(result, expected_request)
    core = dict(result)
    core.pop("result_fingerprint", None)
    return {**core, "result_fingerprint": _fingerprint(core)}


def build_driver_research_prompt(request: Mapping[str, Any]) -> str:
    """Return the copyable v2 research prompt for one exact request."""

    request_json = json.dumps(request, indent=2, sort_keys=True)
    return "\n".join(
        (
            "You are researching safe starting constraints for a JTS active speaker.",
            "Research manufacturer datasheets first, then reputable independent measurements.",
            "Do not invent missing facts. Put every unresolved fact in unknowns and use null where appropriate.",
            "Every field assertion needs confidence, a short basis, and source URLs. Research is advisory; the operator will review every value before confirmation.",
            "A filter cutoff is not a brick wall. Keep the hard excitation band distinct from required filter cutoff/slope, the measurement band, and the crossover-search band.",
            "For cabinet data, identify sealed/vented/passive-radiator/open-baffle/other, radiator count, effective radiating diameter, and baffle width when supported by evidence.",
            "Return JSON only. Echo request_fingerprint and every target_id/target_fingerprint exactly.",
            "",
            "Exact server-authored request:",
            request_json,
            "",
            "Return this result shape:",
            "{",
            '  "artifact_schema_version": 2,',
            f'  "kind": "{DRIVER_RESEARCH_KIND}",',
            '  "request_fingerprint": "echo from request",',
            '  "drivers": [{',
            '    "target_id": "echo from request",',
            '    "target_fingerprint": "echo from request",',
            '    "role": "woofer|mid|tweeter",',
            '    "model": "exact model",',
            '    "manufacturer": "string|null",',
            '    "nominal_impedance_ohm": 8,',
            '    "sensitivity_db_2v83_1m": 90,',
            '    "usable_frequency_range_hz": [80, 5000],',
            '    "recommended_highpass_hz": 80,',
            '    "recommended_lowpass_hz": 2200,',
            '    "do_not_test_below_hz": 1200,',
            '    "hard_excitation_band_hz": [1200, 20000],',
            '    "required_protection_filters": [{"kind":"highpass","cutoff_hz":1800,"minimum_slope_db_per_octave":24,"family_or_equivalent":"equivalent_or_steeper"}],',
            '    "measurement_band_hz": [1800, 18000],',
            '    "crossover_search_band_hz": [2200, 4000],',
            '    "level_duration_limits": {"max_effective_peak_dbfs":null,"max_sweep_duration_s":4,"max_repeat_count":3,"minimum_cooldown_s":0},',
            '    "cabinet": {"enclosure_kind":"sealed|vented|passive_radiator|open_baffle|transmission_line|unknown","radiator_count":1,"effective_radiating_diameter_mm":null,"baffle_width_mm":null},',
            '    "unknowns": ["facts that could not be established"],',
            '    "field_provenance": {"hard_excitation_band_hz":{"confidence":"low|medium|high|unknown","basis":"short explanation","sources":["https://..."]}},',
            '    "gain_offset_db": -6,',
            '    "notes": "concise safety summary",',
            '    "sources": ["https://..."]',
            "  }],",
            '  "crossover_candidates": [{"between_roles":["woofer","tweeter"],"frequency_hz":2500,"filter_type":"Linkwitz-Riley","slope_db_per_octave":24,"confidence":"low|medium|high","rationale":"safe starting point","warnings":[]}]',
            "}",
        )
    )


def _research_by_target(
    driver_research: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(driver_research, Mapping):
        return {}
    if (
        driver_research.get("artifact_schema_version")
        != DRIVER_RESEARCH_RESULT_SCHEMA_VERSION
    ):
        return {}
    return {
        str(driver.get("target_id")): driver
        for driver in driver_research.get("drivers", [])
        if isinstance(driver, Mapping) and driver.get("target_id")
    }


def validate_manual_target_bindings(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
) -> None:
    """Refuse ambiguous or contradictory physical-target driver rows."""

    if not isinstance(manual_settings, Mapping):
        return
    targets = active_driver_targets(topology)
    by_id = {str(target["target_id"]): target for target in targets}
    by_role: dict[str, list[str]] = {}
    for physical_target in targets:
        by_role.setdefault(str(physical_target["role"]), []).append(
            str(physical_target["target_id"])
        )
    resolved_targets: set[str] = set()
    legacy_roles: set[str] = set()
    for index, driver in enumerate(manual_settings.get("drivers", [])):
        if not isinstance(driver, Mapping):
            raise DriverSafetyProfileError(
                f"manual_settings.drivers[{index}] must be an object"
            )
        role = _text(
            driver.get("role"),
            f"manual_settings.drivers[{index}].role",
            required=True,
            max_chars=40,
        )
        target_id = _text(
            driver.get("target_id"),
            f"manual_settings.drivers[{index}].target_id",
            max_chars=160,
        )
        if target_id:
            target = by_id.get(target_id)
            if target is None:
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers[{index}].target_id is not a current physical target"
                )
            if role != target.get("role"):
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers[{index}] role does not match target_id"
                )
            if target_id in resolved_targets:
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers resolves target {target_id} more than once"
                )
            resolved_targets.add(target_id)
            continue
        if role in legacy_roles:
            raise DriverSafetyProfileError(
                f"manual_settings.drivers contains duplicate legacy role {role}"
            )
        legacy_roles.add(str(role))
        matches = by_role.get(str(role), [])
        if not matches:
            raise DriverSafetyProfileError(
                f"manual_settings.drivers[{index}].role is not a current driver role"
            )
        if len(matches) == 1:
            resolved = matches[0]
            if resolved in resolved_targets:
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers resolves target {resolved} more than once"
                )
            resolved_targets.add(resolved)


def _normalise_profile_manual_settings(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Canonicalize direct safety-builder input before deriving authority."""

    if manual_settings is None:
        return None
    if not isinstance(manual_settings, Mapping):
        raise DriverSafetyProfileError("manual_settings must be an object")
    _reject_unknown_keys(
        manual_settings,
        "manual_settings",
        _MANUAL_SETTINGS_FIELDS,
    )
    drivers: list[dict[str, Any]] = []
    for index, raw in enumerate(
        _sequence(
            manual_settings.get("drivers"),
            "manual_settings.drivers",
            maximum=16,
        )
    ):
        field_name = f"manual_settings.drivers[{index}]"
        if not isinstance(raw, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        _reject_unknown_keys(raw, field_name, _MANUAL_DRIVER_FIELDS)
        _reject_bool_tree(raw, field_name)
        driver: dict[str, Any] = {
            "role": _text(
                raw.get("role"),
                f"{field_name}.role",
                required=True,
                max_chars=40,
            ),
        }
        for key, max_chars in (("target_id", 160), ("model", 120), ("manufacturer", 120)):
            value = _text(raw.get(key), f"{field_name}.{key}", max_chars=max_chars)
            if value:
                driver[key] = value
        driver.update(
            normalise_driver_safety_fields(
                raw,
                field_name,
                include_research_evidence=False,
            )
        )
        drivers.append(driver)
    for index, raw_candidate in enumerate(
        _sequence(
            manual_settings.get("crossover_candidates"),
            "manual_settings.crossover_candidates",
            maximum=16,
        )
    ):
        field_name = f"manual_settings.crossover_candidates[{index}]"
        if not isinstance(raw_candidate, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        _reject_unknown_keys(
            raw_candidate,
            field_name,
            _MANUAL_CANDIDATE_FIELDS,
        )
        _reject_bool_tree(raw_candidate, field_name)
    normalised = {"drivers": drivers, "crossover_candidates": []}
    validate_manual_target_bindings(topology, normalised)
    return normalised


def _manual_by_role(
    manual_settings: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(manual_settings, Mapping):
        return {}
    return {
        str(driver.get("role")): driver
        for driver in manual_settings.get("drivers", [])
        if (
            isinstance(driver, Mapping)
            and driver.get("role")
            and not driver.get("target_id")
        )
    }


def _manual_by_target(
    manual_settings: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(manual_settings, Mapping):
        return {}
    return {
        str(driver.get("target_id")): driver
        for driver in manual_settings.get("drivers", [])
        if isinstance(driver, Mapping) and driver.get("target_id")
    }


def _band_subset(inner: Sequence[float], outer: Sequence[float]) -> bool:
    return inner[0] >= outer[0] and inner[1] <= outer[1]


def _target_issues(target: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    role = str(target.get("role") or "driver")
    target_id = str(target.get("target_id") or role)
    if target.get("target_values_binding") == "missing":
        reasons.append(f"{target_id}:target_specific_values_missing")
    if not target.get("model"):
        reasons.append(f"{role}:model_missing")
    hard = target.get("hard_excitation_band_hz")
    measurement = target.get("measurement_band_hz")
    search = target.get("crossover_search_band_hz")
    if not isinstance(hard, list):
        reasons.append(f"{role}:hard_excitation_band_missing")
    if not isinstance(measurement, list):
        reasons.append(f"{role}:measurement_band_missing")
    if not isinstance(search, list):
        reasons.append(f"{role}:crossover_search_band_missing")
    limits = target.get("level_duration_limits")
    required_limit_fields = (
        "max_effective_peak_dbfs",
        "max_sweep_duration_s",
        "max_repeat_count",
        "minimum_cooldown_s",
    )
    if not isinstance(limits, Mapping):
        reasons.append(f"{role}:level_duration_limits_missing")
    else:
        for field in required_limit_fields:
            if limits.get(field) is None:
                reasons.append(f"{role}:{field}_missing")
    policy = driver_protection_profile(
        role,
        driver_style=target.get("driver_style"),
    )
    if isinstance(limits, Mapping):
        peak = limits.get("max_effective_peak_dbfs")
        if (
            isinstance(peak, (int, float))
            and not isinstance(peak, bool)
            and float(peak) > policy.max_auto_level_dbfs
        ):
            reasons.append(f"{role}:max_effective_peak_above_code_policy")
    if isinstance(hard, list) and isinstance(measurement, list):
        if not _band_subset(measurement, hard):
            reasons.append(f"{role}:measurement_band_outside_hard_band")
    if isinstance(measurement, list) and isinstance(search, list):
        if not _band_subset(search, measurement):
            reasons.append(f"{role}:search_band_outside_measurement_band")
    filters = target.get("required_protection_filters")
    filters = filters if isinstance(filters, list) else []
    kinds = {str(item.get("kind")) for item in filters if isinstance(item, Mapping)}
    if role == "tweeter" and "highpass" not in kinds:
        reasons.append("tweeter:required_highpass_missing")
    if role == "mid":
        if "highpass" not in kinds:
            reasons.append("mid:required_highpass_missing")
        if "lowpass" not in kinds:
            reasons.append("mid:required_lowpass_missing")
    if isinstance(hard, list):
        for item in filters:
            if not isinstance(item, Mapping):
                continue
            cutoff = float(item["cutoff_hz"])
            if not hard[0] <= cutoff <= hard[1]:
                reasons.append(f"{role}:{item.get('kind')}_cutoff_outside_hard_band")
    if policy.min_highpass_hz is not None:
        highpass = next(
            (
                item
                for item in filters
                if isinstance(item, Mapping) and item.get("kind") == "highpass"
            ),
            None,
        )
        if (
            isinstance(highpass, Mapping)
            and float(highpass["cutoff_hz"]) < policy.min_highpass_hz
        ):
            reasons.append(f"{role}:highpass_below_code_policy")
    return reasons


def _profile_core(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
    driver_research: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    manual_by_role = _manual_by_role(manual_settings)
    manual_by_target = _manual_by_target(manual_settings)
    research_by_target = _research_by_target(driver_research)
    physical_targets = active_driver_targets(topology)
    role_counts: dict[str, int] = {}
    for physical in physical_targets:
        role = str(physical.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    driver_styles = {
        f"{group.id}:{channel.role}": channel.driver_style
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.driver_style
    }
    targets: list[dict[str, Any]] = []
    issues: list[str] = []
    for physical in physical_targets:
        target_id = str(physical["target_id"])
        role = str(physical["role"])
        visible = manual_by_target.get(target_id)
        used_legacy_role_value = False
        if visible is None and role_counts.get(role) == 1:
            visible = manual_by_role.get(role)
            used_legacy_role_value = visible is not None
        visible = visible or {}
        research = research_by_target.get(target_id, {})
        safety_field_names = (
            "hard_excitation_band_hz",
            "required_protection_filters",
            "measurement_band_hz",
            "crossover_search_band_hz",
            "level_duration_limits",
            "cabinet",
        )
        provenance: dict[str, Any] = {}
        unknowns = list(research.get("unknowns", []))
        research_provenance = research.get("field_provenance", {})
        research_provenance = (
            research_provenance if isinstance(research_provenance, Mapping) else {}
        )
        for field in safety_field_names:
            if field not in visible:
                continue
            if (
                field in research
                and _canonical_json(visible.get(field))
                == _canonical_json(research.get(field))
                and field in research_provenance
            ):
                provenance[field] = research_provenance[field]
            else:
                provenance[field] = {
                    "confidence": "unknown",
                    "basis": (
                        "Operator-entered visible value; no matching research "
                        "assertion is authoritative."
                    ),
                    "sources": [],
                }
                unknown = f"{field}: operator override has no matching research source"
                if unknown not in unknowns:
                    unknowns.append(unknown)
        entry: dict[str, Any] = {
            "target_id": target_id,
            "target_fingerprint": str(physical["target_fingerprint"]),
            "speaker_group_id": str(physical["speaker_group_id"]),
            "speaker_group_mode": str(physical["speaker_group_mode"]),
            "role": role,
            "driver_style": driver_styles.get(target_id) or "unspecified",
            "target_values_binding": (
                "explicit_target"
                if target_id in manual_by_target
                else "unique_legacy_role"
                if used_legacy_role_value
                else "missing"
            ),
            "physical_output_index": physical.get("output_index"),
            "model": visible.get("model"),
            "manufacturer": visible.get("manufacturer"),
            "hard_excitation_band_hz": visible.get("hard_excitation_band_hz"),
            "required_protection_filters": visible.get(
                "required_protection_filters", []
            ),
            "measurement_band_hz": visible.get("measurement_band_hz"),
            "crossover_search_band_hz": visible.get("crossover_search_band_hz"),
            "level_duration_limits": visible.get("level_duration_limits", {}),
            "cabinet": visible.get(
                "cabinet",
                {
                    "enclosure_kind": "unknown",
                    "lf_reconstruction_capability": "refused_unknown_enclosure",
                },
            ),
            "unknowns": unknowns,
            "field_provenance": provenance,
            "authority": "operator_visible_values",
        }
        policy = driver_protection_profile(
            role,
            driver_style=driver_styles.get(target_id) or "unspecified",
        )
        entry["code_owned_policy"] = {
            "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
            "max_auto_level_dbfs": policy.max_auto_level_dbfs,
            "min_highpass_hz": policy.min_highpass_hz,
            "floor_test_frequency_hz": policy.floor_test_frequency_hz,
            "floor_test_duration_ms": policy.floor_test_duration_ms,
        }
        entry = {
            key: value
            for key, value in entry.items()
            if value not in (None, {}, [])
            or key
            in {
                "required_protection_filters",
                "unknowns",
                "field_provenance",
            }
        }
        issues.extend(_target_issues(entry))
        targets.append(entry)
    if not targets:
        issues.append("active_driver_targets_missing")
    research_digest = None
    request_digest = None
    if isinstance(driver_research, Mapping):
        research_digest = driver_research.get("result_fingerprint")
        request_digest = driver_research.get("request_fingerprint")
    core = {
        "artifact_schema_version": DRIVER_SAFETY_PROFILE_SCHEMA_VERSION,
        "kind": DRIVER_SAFETY_PROFILE_KIND,
        "topology_id": topology.topology_id,
        "targets": targets,
        "research": {
            "request_fingerprint": request_digest,
            "result_fingerprint": research_digest,
            "advisory_only": True,
        },
        "authority": "operator_visible_values",
        "authorizes_playback": False,
    }
    return core, issues


def _profile_issue_payload(issues: Sequence[str]) -> list[dict[str, str]]:
    return [
        {
            "severity": "blocker",
            "code": reason,
            "message": reason.replace(":", " ").replace("_", " "),
        }
        for reason in issues
    ]


def build_driver_safety_profile(
    topology: OutputTopology,
    *,
    manual_settings: Mapping[str, Any] | None,
    driver_research: Mapping[str, Any] | None,
    prior_profile: Mapping[str, Any] | None = None,
    confirm: bool = False,
    confirmed_at: str | None = None,
) -> dict[str, Any]:
    """Build or preserve the immutable profile for the visible current values."""

    normalised_manual = _normalise_profile_manual_settings(topology, manual_settings)
    core, issues = _profile_core(topology, normalised_manual, driver_research)
    fingerprint = _fingerprint(core)
    prior_evaluation = evaluate_driver_safety_profile(prior_profile, topology)
    prior_confirmation = (
        prior_profile.get("confirmation")
        if isinstance(prior_profile, Mapping)
        and prior_profile.get("profile_fingerprint") == fingerprint
        and isinstance(prior_profile.get("confirmation"), Mapping)
        and prior_evaluation.confirmed_and_current
        else None
    )
    if confirm and issues:
        raise DriverSafetyProfileError(
            "driver safety profile cannot be confirmed: " + ", ".join(issues)
        )
    confirmation_time = None
    if confirm:
        confirmation_time = _text(
            confirmed_at,
            "driver_safety_profile.confirmed_at",
            required=True,
            max_chars=64,
        )
    confirmation: dict[str, Any] | None = None
    if confirm:
        confirmation = {
            "confirmed_fingerprint": fingerprint,
            "confirmed_at": confirmation_time,
            "method": "operator_reviewed_visible_values",
        }
    elif (
        not issues
        and prior_confirmation
        and prior_confirmation.get("confirmed_fingerprint") == fingerprint
    ):
        confirmation = dict(prior_confirmation)
    if issues:
        status = "incomplete"
    elif confirmation:
        status = "confirmed"
    else:
        status = "needs_confirmation"
    profile = {
        **core,
        "profile_fingerprint": fingerprint,
        "status": status,
        "confirmation": confirmation,
        "issues": _profile_issue_payload(issues),
    }
    evaluation = evaluate_driver_safety_profile(profile, topology)
    expected_evaluation_status = {
        "incomplete": "incomplete",
        "needs_confirmation": "unconfirmed",
        "confirmed": "confirmed",
    }[status]
    if evaluation.status != expected_evaluation_status:
        raise DriverSafetyProfileError(
            "driver safety profile builder produced an incoherent artifact"
        )
    if confirm and not evaluation.confirmed_and_current:
        raise DriverSafetyProfileError(
            "driver safety profile confirmation did not validate as current"
        )
    return profile


def _require_canonical_text_field(
    value: Mapping[str, Any],
    key: str,
    field_name: str,
    *,
    required: bool,
    max_chars: int,
) -> str | None:
    normalised = _text(
        value.get(key),
        field_name,
        required=required,
        max_chars=max_chars,
    )
    if normalised is None:
        if key in value:
            raise DriverSafetyProfileError(f"{field_name} must be omitted when empty")
    elif value.get(key) != normalised:
        raise DriverSafetyProfileError(f"{field_name} is not canonical")
    return normalised


def _validate_driver_safety_profile_shape(profile: Mapping[str, Any]) -> None:
    _reject_unknown_keys(
        profile,
        "driver_safety_profile",
        {
            "artifact_schema_version",
            "kind",
            "topology_id",
            "targets",
            "research",
            "authority",
            "authorizes_playback",
            "profile_fingerprint",
            "status",
            "confirmation",
            "issues",
        },
    )
    if type(profile.get("artifact_schema_version")) is not int:  # noqa: E721
        raise DriverSafetyProfileError(
            "driver_safety_profile.artifact_schema_version must be integer 1"
        )
    if profile.get("artifact_schema_version") != DRIVER_SAFETY_PROFILE_SCHEMA_VERSION:
        raise DriverSafetyProfileError(
            "driver_safety_profile.artifact_schema_version must be integer 1"
        )
    if profile.get("kind") != DRIVER_SAFETY_PROFILE_KIND:
        raise DriverSafetyProfileError("driver_safety_profile kind is unsupported")
    _require_canonical_text_field(
        profile,
        "topology_id",
        "driver_safety_profile.topology_id",
        required=True,
        max_chars=160,
    )
    if profile.get("status") not in {
        "incomplete",
        "needs_confirmation",
        "confirmed",
    }:
        raise DriverSafetyProfileError("driver_safety_profile status is unsupported")
    if not _is_sha256(profile.get("profile_fingerprint")):
        raise DriverSafetyProfileError(
            "driver_safety_profile.profile_fingerprint is invalid"
        )
    if profile.get("authority") != "operator_visible_values":
        raise DriverSafetyProfileError("driver_safety_profile authority is invalid")
    if profile.get("authorizes_playback") is not False:
        raise DriverSafetyProfileError(
            "driver_safety_profile must not authorize playback"
        )
    research = profile.get("research")
    if not isinstance(research, Mapping):
        raise DriverSafetyProfileError(
            "driver_safety_profile.research must be an object"
        )
    _reject_unknown_keys(
        research,
        "driver_safety_profile.research",
        {"request_fingerprint", "result_fingerprint", "advisory_only"},
    )
    if research.get("advisory_only") is not True:
        raise DriverSafetyProfileError(
            "driver_safety_profile.research must remain advisory"
        )
    for digest_field in ("request_fingerprint", "result_fingerprint"):
        digest = research.get(digest_field)
        if digest is not None and not _is_sha256(digest):
            raise DriverSafetyProfileError(
                f"driver_safety_profile.research.{digest_field} is invalid"
            )
    targets = _sequence(
        profile.get("targets"),
        "driver_safety_profile.targets",
        maximum=16,
    )
    for index, target in enumerate(targets):
        field_name = f"driver_safety_profile.targets[{index}]"
        if not isinstance(target, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        _reject_unknown_keys(
            target,
            field_name,
            {
                "target_id",
                "target_fingerprint",
                "speaker_group_id",
                "speaker_group_mode",
                "role",
                "driver_style",
                "target_values_binding",
                "physical_output_index",
                "model",
                "manufacturer",
                "hard_excitation_band_hz",
                "required_protection_filters",
                "measurement_band_hz",
                "crossover_search_band_hz",
                "level_duration_limits",
                "cabinet",
                "unknowns",
                "field_provenance",
                "authority",
                "code_owned_policy",
            },
        )
        _require_canonical_text_field(
            target,
            "target_id",
            f"{field_name}.target_id",
            required=True,
            max_chars=160,
        )
        target_fingerprint = _require_canonical_text_field(
            target,
            "target_fingerprint",
            f"{field_name}.target_fingerprint",
            required=True,
            max_chars=64,
        )
        if not _is_sha256(target_fingerprint):
            raise DriverSafetyProfileError(
                f"{field_name}.target_fingerprint is invalid"
            )
        for key, max_chars in (
            ("speaker_group_id", 160),
            ("speaker_group_mode", 64),
            ("role", 32),
        ):
            _require_canonical_text_field(
                target,
                key,
                f"{field_name}.{key}",
                required=True,
                max_chars=max_chars,
            )
        for key in ("model", "manufacturer"):
            _require_canonical_text_field(
                target,
                key,
                f"{field_name}.{key}",
                required=False,
                max_chars=120,
            )
        _require_canonical_text_field(
            target,
            "driver_style",
            f"{field_name}.driver_style",
            required=True,
            max_chars=80,
        )
        if target.get("target_values_binding") not in {
            "explicit_target",
            "unique_legacy_role",
            "missing",
        }:
            raise DriverSafetyProfileError(
                f"{field_name}.target_values_binding is invalid"
            )
        code_policy = target.get("code_owned_policy")
        if not isinstance(code_policy, Mapping):
            raise DriverSafetyProfileError(
                f"{field_name}.code_owned_policy must be an object"
            )
        _reject_unknown_keys(
            code_policy,
            f"{field_name}.code_owned_policy",
            {
                "policy_version",
                "max_auto_level_dbfs",
                "min_highpass_hz",
                "floor_test_frequency_hz",
                "floor_test_duration_ms",
            },
        )
        current_policy = driver_protection_profile(
            target.get("role"),
            driver_style=target.get("driver_style"),
        )
        expected_policy = {
            "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
            "max_auto_level_dbfs": current_policy.max_auto_level_dbfs,
            "min_highpass_hz": current_policy.min_highpass_hz,
            "floor_test_frequency_hz": current_policy.floor_test_frequency_hz,
            "floor_test_duration_ms": current_policy.floor_test_duration_ms,
        }
        if _canonical_json(code_policy) != _canonical_json(expected_policy):
            raise DriverSafetyProfileError(
                f"{field_name}.code_owned_policy is stale or noncanonical"
            )
        if "physical_output_index" in target:
            output_index = target.get("physical_output_index")
            if isinstance(output_index, bool) or not isinstance(output_index, int):
                raise DriverSafetyProfileError(
                    f"{field_name}.physical_output_index must be an integer"
                )
            if output_index < 0:
                raise DriverSafetyProfileError(
                    f"{field_name}.physical_output_index must be >= 0"
                )
        normalised_safety = normalise_driver_safety_fields(
            target,
            field_name,
            include_research_evidence=False,
        )
        safety_fields = {
            "hard_excitation_band_hz",
            "required_protection_filters",
            "measurement_band_hz",
            "crossover_search_band_hz",
            "level_duration_limits",
            "cabinet",
        }
        raw_safety = {key: target[key] for key in safety_fields if key in target}
        if _canonical_json(raw_safety) != _canonical_json(normalised_safety):
            raise DriverSafetyProfileError(
                f"{field_name} safety fields are not canonical"
            )
        normalised_unknowns = _normalise_unknowns(
            target.get("unknowns"),
            f"{field_name}.unknowns",
        )
        if "unknowns" not in target or _canonical_json(
            target.get("unknowns")
        ) != _canonical_json(normalised_unknowns):
            raise DriverSafetyProfileError(f"{field_name}.unknowns are not canonical")
        normalised_provenance = _normalise_field_provenance(
            target.get("field_provenance"),
            f"{field_name}.field_provenance",
        )
        if "field_provenance" not in target or _canonical_json(
            target.get("field_provenance")
        ) != _canonical_json(normalised_provenance):
            raise DriverSafetyProfileError(
                f"{field_name}.field_provenance is not canonical"
            )
        if target.get("authority") != "operator_visible_values":
            raise DriverSafetyProfileError(f"{field_name}.authority is invalid")
    confirmation = profile.get("confirmation")
    if confirmation is not None:
        if not isinstance(confirmation, Mapping):
            raise DriverSafetyProfileError(
                "driver_safety_profile.confirmation must be an object or null"
            )
        _reject_unknown_keys(
            confirmation,
            "driver_safety_profile.confirmation",
            {"confirmed_fingerprint", "confirmed_at", "method"},
        )
        confirmed_fingerprint = _require_canonical_text_field(
            confirmation,
            "confirmed_fingerprint",
            "driver_safety_profile.confirmation.confirmed_fingerprint",
            required=True,
            max_chars=64,
        )
        if not _is_sha256(confirmed_fingerprint):
            raise DriverSafetyProfileError(
                "driver_safety_profile.confirmation.confirmed_fingerprint is invalid"
            )
        _require_canonical_text_field(
            confirmation,
            "confirmed_at",
            "driver_safety_profile.confirmation.confirmed_at",
            required=True,
            max_chars=64,
        )
        _require_canonical_text_field(
            confirmation,
            "method",
            "driver_safety_profile.confirmation.method",
            required=True,
            max_chars=80,
        )
    for index, issue in enumerate(
        _sequence(profile.get("issues"), "driver_safety_profile.issues", maximum=64)
    ):
        if not isinstance(issue, Mapping):
            raise DriverSafetyProfileError(
                f"driver_safety_profile.issues[{index}] must be an object"
            )
        _reject_unknown_keys(
            issue,
            f"driver_safety_profile.issues[{index}]",
            {"severity", "code", "message"},
        )
        for key, max_chars in (("severity", 20), ("code", 160), ("message", 320)):
            _require_canonical_text_field(
                issue,
                key,
                f"driver_safety_profile.issues[{index}].{key}",
                required=True,
                max_chars=max_chars,
            )


def evaluate_driver_safety_profile(
    profile: Any,
    topology: OutputTopology,
) -> DriverSafetyProfileEvaluation:
    """Evaluate schema, integrity, confirmation, and current target binding."""

    if not isinstance(profile, Mapping):
        return DriverSafetyProfileEvaluation(
            "missing", False, None, ("driver_safety_profile_missing",)
        )
    try:
        _validate_driver_safety_profile_shape(profile)
    except DriverSafetyProfileError:
        fingerprint = profile.get("profile_fingerprint")
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint) if isinstance(fingerprint, str) else None,
            ("driver_safety_profile_schema_invalid",),
        )
    fingerprint = profile.get("profile_fingerprint")
    if (
        profile.get("artifact_schema_version") != DRIVER_SAFETY_PROFILE_SCHEMA_VERSION
        or profile.get("kind") != DRIVER_SAFETY_PROFILE_KIND
        or not _is_sha256(fingerprint)
    ):
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint) if isinstance(fingerprint, str) else None,
            ("driver_safety_profile_schema_invalid",),
        )
    core = {
        key: profile.get(key)
        for key in (
            "artifact_schema_version",
            "kind",
            "topology_id",
            "targets",
            "research",
            "authority",
            "authorizes_playback",
        )
    }
    if _fingerprint(core) != fingerprint:
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint),
            ("driver_safety_profile_fingerprint_mismatch",),
        )
    current_targets = active_driver_targets(topology)
    saved_targets = profile.get("targets", [])
    targets_match = len(saved_targets) == len(current_targets)
    if targets_match:
        for saved, current in zip(saved_targets, current_targets):
            expected = {
                "target_id": str(current["target_id"]),
                "target_fingerprint": str(current["target_fingerprint"]),
                "speaker_group_id": str(current["speaker_group_id"]),
                "speaker_group_mode": str(current["speaker_group_mode"]),
                "role": str(current["role"]),
                "physical_output_index": current.get("output_index"),
            }
            group = next(
                (
                    item
                    for item in topology.speaker_groups
                    if item.id == current["speaker_group_id"]
                ),
                None,
            )
            channel = next(
                (
                    item
                    for item in (group.channels if group is not None else ())
                    if item.role == current["role"]
                ),
                None,
            )
            expected["driver_style"] = (
                channel.driver_style
                if channel and channel.driver_style
                else "unspecified"
            )
            if any(saved.get(key) != value for key, value in expected.items()):
                targets_match = False
                break
    if profile.get("topology_id") != topology.topology_id or not targets_match:
        return DriverSafetyProfileEvaluation(
            "stale",
            False,
            str(fingerprint),
            ("driver_safety_profile_target_mismatch",),
        )
    derived_issues: list[str] = []
    for target in saved_targets:
        derived_issues.extend(_target_issues(target))
    if not saved_targets:
        derived_issues.append("active_driver_targets_missing")
    expected_issue_payload = _profile_issue_payload(derived_issues)
    if _canonical_json(profile.get("issues")) != _canonical_json(
        expected_issue_payload
    ):
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint),
            ("driver_safety_profile_derived_state_mismatch",),
        )
    confirmation = profile.get("confirmation")
    if derived_issues:
        if profile.get("status") != "incomplete" or confirmation is not None:
            return DriverSafetyProfileEvaluation(
                "malformed",
                False,
                str(fingerprint),
                ("driver_safety_profile_derived_state_mismatch",),
            )
        return DriverSafetyProfileEvaluation(
            "incomplete",
            False,
            str(fingerprint),
            tuple(derived_issues),
        )
    if profile.get("status") == "needs_confirmation" and confirmation is None:
        return DriverSafetyProfileEvaluation(
            "unconfirmed",
            False,
            str(fingerprint),
            ("driver_safety_profile_not_confirmed",),
        )
    if (
        profile.get("status") != "confirmed"
        or not isinstance(confirmation, Mapping)
        or confirmation.get("confirmed_fingerprint") != fingerprint
        or confirmation.get("method") != "operator_reviewed_visible_values"
    ):
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint),
            ("driver_safety_profile_derived_state_mismatch",),
        )
    return DriverSafetyProfileEvaluation("confirmed", True, str(fingerprint), ())
