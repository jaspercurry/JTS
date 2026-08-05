# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Validator-only frozen envelope for the future R17 crossover selector."""

from __future__ import annotations

import math
from itertools import combinations
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.transfer_composition import SELECTOR_RESULT_KIND

ROLE_ORDER = ("woofer", "tweeter")
SELECTOR_SCALAR_KEYS = (
    "worst_anchor_branch_target_error_db",
    "anchor_absolute_sum_error_db",
    "worst_lateral_handoff_mismatch_db",
    "required_positive_headroom_db",
    "filter_biquad_count",
    "filter_absolute_gain_sum_db",
)
_DETERMINISTIC_KEYS = set(SELECTOR_SCALAR_KEYS[3:])
_BOUND_KEYS = {
    "component_policy",
    "measurement_protection",
    "measured_trust",
    "meaningful_contribution",
    "fit_authority",
}
_CANDIDATE_REASONS = {
    "unsupported_comparison_bins",
    "ill_conditioned_protection_deembedding",
    "stopband_correction_required",
    "protection_contract_violated",
    "headroom_contract_violated",
    "fit_failed",
    "runtime_contract_invalid",
    "incomplete_prescription",
}
_OUTCOME_REASONS = {
    "unique_lexicographic_dominance",
    "metric_uncertainty_tie",
    "cyclic_comparison",
    "left_right_winner_instability",
    "leave_one_wide_side_winner_instability",
    "invalid_evidence_schema",
    "invalid_evidence_identity",
    "invalid_measurement_authority",
    "no_admissible_candidate",
    "configured_fc_inadmissible_for_abstention",
}
_RESULT_FIELDS = {
    "schema_version",
    "kind",
    "evidence_set_id",
    "evidence_set_fingerprint",
    "grid_policy_fingerprint",
    "candidate_interval_bound_fingerprints",
    "candidate_interval_hz",
    "grid_fc",
    "grid_fingerprint",
    "metric_policy_fingerprint",
    "common_mask_fingerprint",
    "role_mask_fingerprints",
    "candidate_mask_fingerprints",
    "mask_bin_counts",
    "repeat_realization_policy_fingerprint",
    "comparison_policy_fingerprint",
    "candidate_records",
    "comparison_trace",
    "sensitivity_check_results",
    "outcome",
    "fingerprint",
}
_CANDIDATE_FIELDS = {
    "fc_hz",
    "protection_transfer_fingerprint",
    "configured_transfer_fingerprint",
    "composition_policy_fingerprint",
    "required_mask_fingerprint",
    "transfer_ratio_fingerprint",
    "fitter_input_fingerprint",
    "complete_prescription_fingerprint",
    "scalar_metrics",
    "admissible",
    "inadmissibility_reason_codes",
}
_TRACE_FIELDS = {
    "left_fc_hz",
    "right_fc_hz",
    "examined_keys",
    "first_deciding_key",
    "decision",
}
_EXAMINED_FIELDS = {"key", "left", "right", "verdict"}
_SENSITIVITY_FIELDS = {
    "check_id",
    "included_pose_ids",
    "candidate_scalar_metrics",
    "comparison_trace",
    "unbeaten_fc_hz",
    "subreason",
}
_SENSITIVITY_POSES = {
    "left_only": ["anchor_00", "left_12_cm", "left_40_cm"],
    "right_only": ["anchor_00", "right_12_cm", "right_40_cm"],
    "omit_left_40_cm": [
        "anchor_00",
        "left_12_cm",
        "right_12_cm",
        "right_40_cm",
    ],
    "omit_right_40_cm": [
        "anchor_00",
        "left_12_cm",
        "right_12_cm",
        "left_40_cm",
    ],
}


class SelectorContractError(ValueError):
    """A selector result does not satisfy the frozen version-one envelope."""


def _sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _fc_key(value: int | float) -> str:
    return format(float(value), ".17g")


def _metric_block(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or tuple(raw) != SELECTOR_SCALAR_KEYS:
        raise SelectorContractError("selector scalar metric order is invalid")
    for key, metric in raw.items():
        if (
            not isinstance(metric, Mapping)
            or set(metric) != {"value", "uncertainty_half_width"}
            or not _number(metric["value"])
            or not _number(metric["uncertainty_half_width"])
            or float(metric["value"]) < 0.0
            or float(metric["uncertainty_half_width"]) < 0.0
            or (
                key in _DETERMINISTIC_KEYS
                and float(metric["uncertainty_half_width"]) != 0.0
            )
        ):
            raise SelectorContractError("selector scalar metric meaning is invalid")
    if type(raw["filter_biquad_count"]["value"]) is not int:
        raise SelectorContractError("selector biquad count must be an integer")
    return raw


def _candidate_records(raw: Any, grid: Sequence[int | float]) -> list[Mapping[str, Any]]:
    if not isinstance(raw, list) or len(raw) != len(grid):
        raise SelectorContractError("selector candidate records are incomplete")
    result: list[Mapping[str, Any]] = []
    for index, candidate in enumerate(raw):
        if (
            not isinstance(candidate, Mapping)
            or set(candidate) != _CANDIDATE_FIELDS
            or candidate["fc_hz"] != grid[index]
            or any(
                not _sha(candidate[field])
                for field in (
                    "protection_transfer_fingerprint",
                    "configured_transfer_fingerprint",
                    "composition_policy_fingerprint",
                    "required_mask_fingerprint",
                    "transfer_ratio_fingerprint",
                    "fitter_input_fingerprint",
                    "complete_prescription_fingerprint",
                )
            )
        ):
            raise SelectorContractError("selector candidate identity is invalid")
        _metric_block(candidate["scalar_metrics"])
        admissible = candidate["admissible"]
        reasons = candidate["inadmissibility_reason_codes"]
        if (
            type(admissible) is not bool
            or not isinstance(reasons, list)
            or len(set(reasons)) != len(reasons)
            or any(reason not in _CANDIDATE_REASONS for reason in reasons)
            or (admissible and reasons)
            or (not admissible and not reasons)
        ):
            raise SelectorContractError("selector candidate admissibility is inconsistent")
        result.append(candidate)
    return result


def _interval_verdict(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    left_hi = float(left["value"]) + float(left["uncertainty_half_width"])
    left_lo = float(left["value"]) - float(left["uncertainty_half_width"])
    right_hi = float(right["value"]) + float(right["uncertainty_half_width"])
    right_lo = float(right["value"]) - float(right["uncertainty_half_width"])
    if left_hi < right_lo:
        return "left"
    if right_hi < left_lo:
        return "right"
    return "tied"


def _validate_trace(
    raw: Any,
    *,
    admissible_grid: Sequence[int | float],
    metrics_by_fc: Mapping[str, Mapping[str, Any]],
) -> list[tuple[float, float, str]]:
    pairs = list(combinations(admissible_grid, 2))
    if not isinstance(raw, list) or len(raw) != len(pairs):
        raise SelectorContractError("selector pairwise trace is incomplete")
    decisions: list[tuple[float, float, str]] = []
    for trace, (left_fc, right_fc) in zip(raw, pairs):
        if (
            not isinstance(trace, Mapping)
            or set(trace) != _TRACE_FIELDS
            or trace["left_fc_hz"] != left_fc
            or trace["right_fc_hz"] != right_fc
            or trace["decision"] not in {"left", "right", "tie"}
            or not isinstance(trace["examined_keys"], list)
            or not trace["examined_keys"]
        ):
            raise SelectorContractError("selector pairwise trace identity is invalid")
        left_metrics = metrics_by_fc[_fc_key(left_fc)]
        right_metrics = metrics_by_fc[_fc_key(right_fc)]
        expected_keys: list[str] = []
        expected_decision = "tie"
        deciding_key: str | None = None
        for key in SELECTOR_SCALAR_KEYS:
            expected_keys.append(key)
            verdict = _interval_verdict(left_metrics[key], right_metrics[key])
            if verdict != "tied":
                expected_decision = verdict
                deciding_key = key
                break
        if len(trace["examined_keys"]) != len(expected_keys):
            raise SelectorContractError("selector trace did not stop at the deciding key")
        for examined, key in zip(trace["examined_keys"], expected_keys):
            if (
                not isinstance(examined, Mapping)
                or set(examined) != _EXAMINED_FIELDS
                or examined["key"] != key
                or examined["left"] != left_metrics[key]
                or examined["right"] != right_metrics[key]
                or examined["verdict"]
                != _interval_verdict(left_metrics[key], right_metrics[key])
            ):
                raise SelectorContractError("selector examined interval is inconsistent")
        if (
            trace["decision"] != expected_decision
            or trace["first_deciding_key"] != deciding_key
        ):
            raise SelectorContractError("selector pairwise decision is inconsistent")
        decisions.append((float(left_fc), float(right_fc), expected_decision))
    return decisions


def _unbeaten(
    grid: Sequence[int | float], decisions: Sequence[tuple[float, float, str]]
) -> list[float]:
    beaten: set[float] = set()
    for left, right, decision in decisions:
        if decision == "left":
            beaten.add(right)
        elif decision == "right":
            beaten.add(left)
    return [float(value) for value in grid if float(value) not in beaten]


def validate_selector_result_v1(
    raw: Mapping[str, Any], *, configured_fc_hz: float
) -> None:
    """Validate the complete frozen envelope without selecting a crossover."""

    if not _number(configured_fc_hz) or float(configured_fc_hz) <= 0.0:
        raise SelectorContractError("configured crossover fallback is invalid")

    if (
        not isinstance(raw, Mapping)
        or set(raw) != _RESULT_FIELDS
        or raw["schema_version"] != 1
        or raw["kind"] != SELECTOR_RESULT_KIND
        or not isinstance(raw["evidence_set_id"], str)
        or not raw["evidence_set_id"]
        or any(
            not _sha(raw[field])
            for field in (
                "evidence_set_fingerprint",
                "grid_policy_fingerprint",
                "metric_policy_fingerprint",
                "common_mask_fingerprint",
                "repeat_realization_policy_fingerprint",
                "comparison_policy_fingerprint",
            )
        )
    ):
        raise SelectorContractError("selector result identity or schema is invalid")
    grid = raw["grid_fc"]
    if (
        not isinstance(grid, list)
        or not grid
        or len(grid) > 5
        or not all(_number(value) and float(value) > 0.0 for value in grid)
        or any(float(right) <= float(left) for left, right in zip(grid, grid[1:]))
    ):
        raise SelectorContractError("selector grid must be finite and ascending")
    interval = raw["candidate_interval_hz"]
    if (
        not isinstance(interval, list)
        or len(interval) != 2
        or not all(_number(value) and float(value) > 0.0 for value in interval)
        or float(interval[0]) > float(interval[1])
        or any(not float(interval[0]) <= float(value) <= float(interval[1]) for value in grid)
    ):
        raise SelectorContractError("selector candidate interval is invalid")
    bounds = raw["candidate_interval_bound_fingerprints"]
    if (
        not isinstance(bounds, Mapping)
        or set(bounds) != _BOUND_KEYS
        or not all(_sha(value) for value in bounds.values())
    ):
        raise SelectorContractError("selector candidate bounds are incomplete")
    grid_core = {
        "schema_version": 1,
        "kind": "jts_crossover_selector_grid_v1",
        "grid_fc": grid,
    }
    if raw["grid_fingerprint"] != json_fingerprint(grid_core):
        raise SelectorContractError("selector grid fingerprint mismatch")
    candidate_keys = [_fc_key(value) for value in grid]
    role_masks = raw["role_mask_fingerprints"]
    candidate_masks = raw["candidate_mask_fingerprints"]
    counts = raw["mask_bin_counts"]
    if (
        not isinstance(role_masks, Mapping)
        or tuple(role_masks) != ROLE_ORDER
        or not all(_sha(value) for value in role_masks.values())
        or not isinstance(candidate_masks, Mapping)
        or list(candidate_masks) != candidate_keys
        or not all(_sha(value) for value in candidate_masks.values())
        or not isinstance(counts, Mapping)
        or set(counts) != {"common", "roles", "candidates"}
        or type(counts["common"]) is not int
        or counts["common"] < 3
        or not isinstance(counts["roles"], Mapping)
        or tuple(counts["roles"]) != ROLE_ORDER
        or not all(type(value) is int and value >= 3 for value in counts["roles"].values())
        or not isinstance(counts["candidates"], Mapping)
        or list(counts["candidates"]) != candidate_keys
        or not all(type(value) is int and value >= 3 for value in counts["candidates"].values())
    ):
        raise SelectorContractError("selector masks or bin counts are incomplete")
    candidates = _candidate_records(raw["candidate_records"], grid)
    for candidate in candidates:
        key = _fc_key(candidate["fc_hz"])
        if candidate["required_mask_fingerprint"] != candidate_masks[key]:
            raise SelectorContractError("candidate mask identity is inconsistent")
    admissible = [candidate for candidate in candidates if candidate["admissible"]]
    admissible_grid = [candidate["fc_hz"] for candidate in admissible]
    base_metrics = {
        _fc_key(candidate["fc_hz"]): candidate["scalar_metrics"]
        for candidate in admissible
    }
    decisions = _validate_trace(
        raw["comparison_trace"],
        admissible_grid=admissible_grid,
        metrics_by_fc=base_metrics,
    )
    baseline_unbeaten = _unbeaten(admissible_grid, decisions)
    sensitivity = raw["sensitivity_check_results"]
    if not isinstance(sensitivity, list) or [item.get("check_id") for item in sensitivity if isinstance(item, Mapping)] != list(_SENSITIVITY_POSES):
        raise SelectorContractError("selector sensitivity results are incomplete")
    sensitivity_unbeaten: dict[str, list[float]] = {}
    for result in sensitivity:
        check_id = result["check_id"]
        if (
            set(result) != _SENSITIVITY_FIELDS
            or result["included_pose_ids"] != _SENSITIVITY_POSES[check_id]
            or not isinstance(result["candidate_scalar_metrics"], Mapping)
            or list(result["candidate_scalar_metrics"]) != [
                _fc_key(value) for value in admissible_grid
            ]
        ):
            raise SelectorContractError("selector sensitivity identity is invalid")
        metrics_by_fc: dict[str, Mapping[str, Any]] = {}
        for candidate in admissible:
            key = _fc_key(candidate["fc_hz"])
            metrics = _metric_block(result["candidate_scalar_metrics"][key])
            for metric_key in SELECTOR_SCALAR_KEYS:
                if metric_key != "worst_lateral_handoff_mismatch_db" and metrics[
                    metric_key
                ] != candidate["scalar_metrics"][metric_key]:
                    raise SelectorContractError(
                        "sensitivity changed a frozen non-lateral metric"
                    )
            metrics_by_fc[key] = metrics
        run_decisions = _validate_trace(
            result["comparison_trace"],
            admissible_grid=admissible_grid,
            metrics_by_fc=metrics_by_fc,
        )
        unbeaten = _unbeaten(admissible_grid, run_decisions)
        expected_subreason = (
            "unique"
            if len(unbeaten) == 1
            else "cyclic_comparison"
            if not unbeaten
            else "metric_uncertainty_tie"
        )
        if result["unbeaten_fc_hz"] != unbeaten or result["subreason"] != expected_subreason:
            raise SelectorContractError("selector sensitivity outcome is inconsistent")
        sensitivity_unbeaten[check_id] = unbeaten
    outcome = raw["outcome"]
    if (
        not isinstance(outcome, Mapping)
        or set(outcome)
        != {"status", "selected_fc_hz", "retained_fc_hz", "reason_codes"}
        or outcome["status"] not in {"selected", "abstained", "refused"}
        or not isinstance(outcome["reason_codes"], list)
        or not outcome["reason_codes"]
        or len(set(outcome["reason_codes"])) != len(outcome["reason_codes"])
        or any(reason not in _OUTCOME_REASONS for reason in outcome["reason_codes"])
    ):
        raise SelectorContractError("selector outcome envelope is invalid")
    instability_reason: str | None
    if not baseline_unbeaten:
        instability_reason = "cyclic_comparison"
    elif len(baseline_unbeaten) != 1:
        instability_reason = "metric_uncertainty_tie"
    elif any(
        sensitivity_unbeaten[key] != baseline_unbeaten
        for key in ("left_only", "right_only")
    ):
        instability_reason = "left_right_winner_instability"
    elif any(
        sensitivity_unbeaten[key] != baseline_unbeaten
        for key in ("omit_left_40_cm", "omit_right_40_cm")
    ):
        instability_reason = "leave_one_wide_side_winner_instability"
    else:
        instability_reason = None

    expected_outcome: dict[str, Any]
    if not admissible:
        expected_outcome = {
            "status": "refused",
            "selected_fc_hz": None,
            "retained_fc_hz": None,
            "reason_codes": ["no_admissible_candidate"],
        }
    elif instability_reason is not None:
        configured = float(configured_fc_hz)
        if configured in admissible_grid:
            expected_outcome = {
                "status": "abstained",
                "selected_fc_hz": None,
                "retained_fc_hz": configured,
                "reason_codes": [instability_reason],
            }
        else:
            expected_outcome = {
                "status": "refused",
                "selected_fc_hz": None,
                "retained_fc_hz": None,
                "reason_codes": ["configured_fc_inadmissible_for_abstention"],
            }
    else:
        expected_outcome = {
            "status": "selected",
            "selected_fc_hz": baseline_unbeaten[0],
            "retained_fc_hz": None,
            "reason_codes": ["unique_lexicographic_dominance"],
        }
    if outcome != expected_outcome:
        raise SelectorContractError("selector outcome is inconsistent with its evidence")
    core = {key: value for key, value in raw.items() if key != "fingerprint"}
    if raw["fingerprint"] != json_fingerprint(core):
        raise SelectorContractError("selector result fingerprint mismatch")
