# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure automatic-crossover measurement eligibility.

The web envelope and the direct apply boundary must answer the same question:
does the *current* protected topology/profile have complete, usable near-field
and fixed-axis driver evidence, backed by the exact durable playback ledger?
This module owns that decision without reading files, logging, or playing audio.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .capture_geometry import (
    DRIVER_PLACEMENT_POLICY_ID,
    REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
    capture_proof_valid,
    comparison_set_valid,
    driver_repeat_binding,
)
from .commissioning_capture import DEFAULT_REPEAT_TARGET
from .repeat_admission import MAX_ATTEMPTS


def mapping(value: Any) -> Mapping[str, Any]:
    """Return a mapping view, or an empty fail-closed view."""

    return value if isinstance(value, Mapping) else {}


def mapping_sequence(value: Any) -> tuple[Mapping[str, Any], ...]:
    """Return only mapping entries from a bounded JSON-style sequence."""

    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def nonnegative_int(value: Any, *, default: int = 0) -> int:
    """Parse a non-negative integer without accepting bools or coercing text."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def finite_float(value: Any) -> float | None:
    """Return one finite real value without accepting bools."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


@dataclass(frozen=True)
class RepeatProgress:
    """Safe bounded projection of one UI repeat target."""

    attempts: int
    accepted: int
    target: int
    failure: Mapping[str, Any]


def repeat_progress(repeats: Any, target_id: str) -> RepeatProgress:
    """Parse one geometry's process/durable repeat progress without coercion."""

    repeats_map = mapping(repeats)
    entry = mapping(mapping(repeats_map.get("targets")).get(target_id))
    failure = mapping(mapping(repeats_map.get("failures")).get(target_id))
    attempts = nonnegative_int(entry.get("attempts"))
    accepted = nonnegative_int(entry.get("accepted"))
    target = nonnegative_int(entry.get("target"), default=3)
    if attempts > 4:
        attempts = 0
    if accepted > 3:
        accepted = 0
    if target != 3:
        target = 3
    return RepeatProgress(attempts, accepted, target, failure)


def render_repeat_progress(progress: RepeatProgress) -> str:
    """Render the shared near/fixed stationary-repeat status sentence."""

    if progress.attempts:
        return (
            f" Repeat {progress.attempts + 1}; {progress.accepted} of "
            f"{progress.target} accepted so far."
        )
    return f" JTS takes {progress.target} stationary repeats."


@dataclass(frozen=True)
class AutomaticMeasurementEligibility:
    """One fail-closed automatic measurement decision."""

    ready: bool
    reason: str | None
    missing: tuple[str, ...]


def driver_repeat_completed(
    target: Mapping[str, Any],
    repeat_targets: Mapping[str, Any],
    *,
    capture_geometry: str,
) -> bool:
    try:
        target_id, target_fingerprint = driver_repeat_binding(
            speaker_group_id=str(target.get("speaker_group_id") or ""),
            role=str(target.get("role") or ""),
            target_fingerprint=str(target.get("target_fingerprint") or ""),
            capture_geometry=capture_geometry,
        )
    except ValueError:
        return False
    entry = mapping(repeat_targets.get(target_id))
    attempts = nonnegative_int(entry.get("attempts"))
    declared_target = (
        nonnegative_int(entry.get("target"))
        if "target" in entry
        else DEFAULT_REPEAT_TARGET
    )
    results = mapping_sequence(entry.get("results"))
    accepted_results = sum(
        1 for result in results if result.get("accepted") is True
    )
    declared_accepted = (
        nonnegative_int(entry.get("accepted"))
        if "accepted" in entry
        else accepted_results
    )
    return bool(
        entry.get("status") == "completed"
        and entry.get("target_fingerprint") == target_fingerprint
        and declared_target == DEFAULT_REPEAT_TARGET
        and DEFAULT_REPEAT_TARGET <= attempts <= MAX_ATTEMPTS
        and declared_accepted == DEFAULT_REPEAT_TARGET
        and accepted_results == DEFAULT_REPEAT_TARGET
    )


def driver_acoustic_usable(
    record: Mapping[str, Any],
    comparison_set: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    capture_geometry: str,
) -> bool:
    group_id = str(target.get("speaker_group_id") or "")
    role = str(target.get("role") or "").lower()
    target_fingerprint = str(target.get("target_fingerprint") or "")
    acoustic = mapping(record.get("acoustic"))
    gating = mapping(acoustic.get("gating"))
    repeats = mapping(record.get("repeats"))
    overlap_levels = mapping_sequence(acoustic.get("overlap_levels"))
    accepted = nonnegative_int(repeats.get("accepted"))
    repeat_target = nonnegative_int(repeats.get("target"))
    admission_attempts = nonnegative_int(repeats.get("admission_attempts"))
    common_ready = bool(
        group_id
        and role
        and record.get("speaker_group_id") == group_id
        and str(record.get("role") or "").lower() == role
        and record.get("target_fingerprint") == target_fingerprint
        and record.get("captured") is True
        and acoustic.get("capture_geometry") == capture_geometry
        and acoustic.get("verdict") == "present"
        and record.get("mic_clipping") is False
        and acoustic.get("mic_clipping") is False
        and repeat_target == DEFAULT_REPEAT_TARGET
        and accepted == DEFAULT_REPEAT_TARGET
        and DEFAULT_REPEAT_TARGET <= admission_attempts <= MAX_ATTEMPTS
        and any(
            entry.get("usable") is True
            and entry.get("above_validity_floor") is True
            for entry in overlap_levels
        )
        and capture_proof_valid(
            record,
            comparison_set,
            policy_id=(
                DRIVER_PLACEMENT_POLICY_ID
                if capture_geometry == "near_field"
                else REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID
            ),
            role=role,
            speaker_group_id=group_id,
            target_fingerprint=target_fingerprint,
        )
    )
    if not common_ready:
        return False
    floor = gating.get("f_valid_floor_hz")
    if capture_geometry == "near_field":
        return bool(
            gating.get("applied") is False
            and gating.get("exempt_reason") == "near_field"
            and floor is None
        )
    return bool(
        gating.get("applied") is True
        and not isinstance(floor, bool)
        and isinstance(floor, (int, float))
        and math.isfinite(float(floor))
        and float(floor) > 0
    )


def automatic_measurement_eligibility(
    *,
    topology_id: str,
    profile_context_id: str,
    driver_targets: Any,
    measurements: Any,
    repeat_state: Any,
) -> AutomaticMeasurementEligibility:
    """Return the shared, exact automatic-measurement apply decision."""

    measurements_map = mapping(measurements)
    comparison_set = mapping(measurements_map.get("active_comparison_set"))
    if (
        not comparison_set_valid(comparison_set)
        or not topology_id
        or comparison_set.get("topology_id") != topology_id
        or not profile_context_id
        or comparison_set.get("profile_context_id") != profile_context_id
    ):
        return AutomaticMeasurementEligibility(
            False,
            "automatic_measurement_context_invalid",
            ("comparison_set",),
        )

    targets = mapping_sequence(driver_targets)
    if not targets:
        return AutomaticMeasurementEligibility(
            False,
            "automatic_measurement_targets_missing",
            ("driver_targets",),
        )
    summary = mapping(measurements_map.get("summary"))
    latest_near = mapping(summary.get("latest_driver_measurements"))
    latest_fixed = mapping(
        summary.get("latest_reference_axis_driver_measurements")
    )
    repeat_targets = mapping(mapping(repeat_state).get("targets"))
    missing: list[str] = []
    for target in targets:
        group_id = str(target.get("speaker_group_id") or "")
        role = str(target.get("role") or "").lower()
        target_id = f"{group_id}:{role}"
        if not driver_acoustic_usable(
            mapping(latest_near.get(target_id)),
            comparison_set,
            target,
            capture_geometry="near_field",
        ):
            missing.append(f"near_field:{target_id}")
        if not driver_acoustic_usable(
            mapping(latest_fixed.get(target_id)),
            comparison_set,
            target,
            capture_geometry="reference_axis",
        ):
            missing.append(f"reference_axis:{target_id}")
        for geometry in ("near_field", "reference_axis"):
            if not driver_repeat_completed(
                target,
                repeat_targets,
                capture_geometry=geometry,
            ):
                missing.append(f"repeat:{geometry}:{target_id}")

    if missing:
        return AutomaticMeasurementEligibility(
            False,
            "automatic_measurement_evidence_incomplete",
            tuple(missing),
        )
    return AutomaticMeasurementEligibility(True, None, ())
