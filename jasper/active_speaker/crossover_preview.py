# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Build a no-audio active-speaker crossover preview.

The preview is the deterministic bridge from a saved design draft to a future
protected startup config. It proposes bounded filter intent only: no CamillaDSP
YAML, no config load, no playback authority, and no sound.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import time
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.output_topology import OutputTopology, OutputTopologyError
from ._common import issue as _issue

SCHEMA_VERSION = 1
CROSSOVER_PREVIEW_KIND = "jts_active_speaker_crossover_preview"
DEFAULT_CROSSOVER_PREVIEW_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_preview.json"
)
CROSSOVER_PREVIEW_PATH_ENV = "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE"

_ACTIVE_ROLE_PAIRS = {
    "active_2_way": (("woofer", "tweeter"),),
    "active_3_way": (("woofer", "mid"), ("mid", "tweeter")),
}
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
_DEFAULT_FILTER_TYPE = "Linkwitz-Riley"
_DEFAULT_SLOPE_DB_PER_OCTAVE = 24.0


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def crossover_preview_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get(CROSSOVER_PREVIEW_PATH_ENV)
        or DEFAULT_CROSSOVER_PREVIEW_PATH
    )


def _as_mapping(raw: Any) -> Mapping[str, Any] | None:
    return raw if isinstance(raw, Mapping) else None


def _design_draft_fingerprint(design_draft: Mapping[str, Any]) -> str:
    """Return a stable content fingerprint for freshness checks."""

    stable = {
        "status": design_draft.get("status"),
        "topology": design_draft.get("topology"),
        "operator_inputs": design_draft.get("operator_inputs"),
        "driver_research": design_draft.get("driver_research"),
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _finite_positive(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def _driver_map(
    research: Mapping[str, Any] | None,
) -> tuple[dict[str, Mapping[str, Any]], list[dict[str, str]]]:
    drivers: dict[str, Mapping[str, Any]] = {}
    issues: list[dict[str, str]] = []
    for item in research.get("drivers", []) if research else []:
        driver = _as_mapping(item)
        role = driver.get("role") if driver else None
        if not isinstance(role, str) or not role:
            continue
        if role in drivers:
            issues.append(
                _issue(
                    "warning",
                    "duplicate_driver_research_role",
                    (
                        f"multiple driver research entries were provided for {role}; "
                        "using the first"
                    ),
                )
            )
            continue
        drivers[role] = driver
    return drivers, issues


def _candidate_key(candidate: Mapping[str, Any]) -> frozenset[str]:
    roles = candidate.get("between_roles")
    if not isinstance(roles, list):
        return frozenset()
    return frozenset(role for role in roles if isinstance(role, str))


def _candidate_map(
    research: Mapping[str, Any] | None,
) -> dict[frozenset[str], Mapping[str, Any]]:
    ranked: dict[frozenset[str], tuple[int, int, int, Mapping[str, Any]]] = {}
    for index, item in enumerate(
        research.get("crossover_candidates", []) if research else []
    ):
        candidate = _as_mapping(item)
        if not candidate:
            continue
        key = _candidate_key(candidate)
        if len(key) != 2:
            continue
        confidence = candidate.get("confidence", "unknown")
        rank = _CONFIDENCE_RANK.get(str(confidence), 0)
        if candidate.get("source") == "manual_settings":
            rank += 10
        has_frequency = 1 if _finite_positive(candidate.get("frequency_hz")) else 0
        existing = ranked.get(key)
        if existing is None or (has_frequency, rank, -index) > (
            existing[0],
            existing[1],
            -existing[2],
        ):
            ranked[key] = (has_frequency, rank, index, candidate)
    return {key: item[3] for key, item in ranked.items()}


def _merged_design_inputs(design_draft: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return research-shaped inputs with operator settings taking precedence."""

    research = _as_mapping(design_draft.get("driver_research"))
    manual = _as_mapping(design_draft.get("manual_settings"))
    if research is None and manual is None:
        return None

    drivers_by_role: dict[str, Mapping[str, Any]] = {}
    for source in (research, manual):
        for item in source.get("drivers", []) if source else []:
            driver = _as_mapping(item)
            role = driver.get("role") if driver else None
            if isinstance(role, str) and role:
                drivers_by_role[role] = driver

    candidates = []
    for source in (research, manual):
        for item in source.get("crossover_candidates", []) if source else []:
            candidate = _as_mapping(item)
            if candidate:
                candidates.append(candidate)

    return {
        "drivers": list(drivers_by_role.values()),
        "crossover_candidates": candidates,
    }


def _range_floor(driver: Mapping[str, Any] | None) -> float | None:
    if not driver:
        return None
    raw = driver.get("usable_frequency_range_hz")
    if isinstance(raw, list) and raw:
        return _finite_positive(raw[0])
    return None


def _range_ceiling(driver: Mapping[str, Any] | None) -> float | None:
    if not driver:
        return None
    raw = driver.get("usable_frequency_range_hz")
    if isinstance(raw, list) and len(raw) >= 2:
        return _finite_positive(raw[1])
    return None


def _upper_soft_floor(driver: Mapping[str, Any] | None) -> float | None:
    """Lowest frequency a crossover may be *raised up to* for the upper driver.

    Prefers the researched ``recommended_highpass_hz`` and the usable-range
    floor. It deliberately excludes ``do_not_test_below_hz``: that value is the
    hard "never cross at or below this" protection line, enforced separately as
    a blocker, not a target a crossover may land on.
    """
    values = [
        _range_floor(driver),
        _finite_positive(driver.get("recommended_highpass_hz")) if driver else None,
    ]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _do_not_test_floor(driver: Mapping[str, Any] | None) -> float | None:
    """The hard protection line for the upper driver.

    A compression/horn driver must be crossed *strictly above* this frequency;
    crossing at or below it (even after a 24 dB/oct highpass) passes damaging
    energy into the diaphragm. ``None`` when the research did not declare one.
    """
    if not driver:
        return None
    return _finite_positive(driver.get("do_not_test_below_hz"))


def _filter_type(candidate: Mapping[str, Any]) -> str:
    raw = candidate.get("filter_type")
    if isinstance(raw, str) and raw.strip():
        return " ".join(raw.split())[:80]
    return _DEFAULT_FILTER_TYPE


def _slope(candidate: Mapping[str, Any]) -> float:
    return (
        _finite_positive(candidate.get("slope_db_per_octave"))
        or _DEFAULT_SLOPE_DB_PER_OCTAVE
    )


def _channel_payload(topology: OutputTopology, group_id: str, role: str) -> dict[str, Any]:
    for group in topology.speaker_groups:
        if group.id != group_id:
            continue
        for channel in group.channels:
            if channel.role == role:
                return channel.to_dict()
    return {"role": role}


def _build_crossover(
    *,
    topology: OutputTopology,
    group_id: str,
    lower_role: str,
    upper_role: str,
    drivers: Mapping[str, Mapping[str, Any]],
    candidates: Mapping[frozenset[str], Mapping[str, Any]],
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    lower_driver = drivers.get(lower_role)
    upper_driver = drivers.get(upper_role)
    if lower_driver is None:
        issues.append(
            _issue(
                "blocker",
                "lower_driver_research_missing",
                f"missing driver research for {lower_role}",
            )
        )
    if upper_driver is None:
        issues.append(
            _issue(
                "blocker",
                "upper_driver_research_missing",
                f"missing driver research for {upper_role}",
            )
        )

    candidate = candidates.get(frozenset((lower_role, upper_role)))
    candidate_frequency = (
        _finite_positive(candidate.get("frequency_hz")) if candidate else None
    )
    if candidate is None:
        issues.append(
            _issue(
                "blocker",
                "crossover_candidate_missing",
                f"missing crossover candidate for {lower_role}/{upper_role}",
            )
        )
    elif candidate_frequency is None:
        issues.append(
            _issue(
                "blocker",
                "crossover_candidate_frequency_missing",
                f"crossover candidate for {lower_role}/{upper_role} is missing frequency_hz",
            )
        )

    proposed_frequency = candidate_frequency
    soft_floor = _upper_soft_floor(upper_driver)
    if (
        proposed_frequency is not None
        and soft_floor is not None
        and proposed_frequency < soft_floor
    ):
        proposed_frequency = soft_floor
        issues.append(
            _issue(
                "warning",
                "crossover_frequency_raised_for_driver_floor",
                f"{upper_role} research requires at least {round(soft_floor)} Hz",
            )
        )
    do_not_test = _do_not_test_floor(upper_driver)
    if (
        proposed_frequency is not None
        and do_not_test is not None
        and proposed_frequency <= do_not_test
    ):
        issues.append(
            _issue(
                "blocker",
                "crossover_below_do_not_test_floor",
                (
                    f"{upper_role} must be crossed strictly above its do-not-test "
                    f"floor of {round(do_not_test)} Hz; {round(proposed_frequency)} Hz "
                    "would risk the driver"
                ),
            )
        )
        # Fail closed: never carry an at/below-do-not-test crossover into filter
        # intent or downstream staging. Drop the frequency so no filters emit.
        proposed_frequency = None
    ceiling = _range_ceiling(lower_driver)
    if proposed_frequency is not None and ceiling is not None and proposed_frequency > ceiling:
        issues.append(
            _issue(
                "blocker",
                "crossover_frequency_above_lower_driver_range",
                f"{lower_role} research only claims usable response to {round(ceiling)} Hz",
            )
        )

    slope = _slope(candidate or {})
    if upper_role == "tweeter" and slope < 24:
        issues.append(
            _issue(
                "warning",
                "tweeter_slope_below_recommended_floor",
                "tweeter crossover slope is below the conservative 24 dB/octave floor",
            )
        )
    confidence = str((candidate or {}).get("confidence") or "unknown")
    if confidence in {"low", "unknown"}:
        issues.append(
            _issue(
                "warning",
                "crossover_candidate_low_confidence",
                f"{lower_role}/{upper_role} crossover confidence is {confidence}",
            )
        )
    warnings = (candidate or {}).get("warnings", [])
    if isinstance(warnings, list):
        for warning in warnings[:4]:
            if isinstance(warning, str) and warning.strip():
                issues.append(
                    _issue(
                        "warning",
                        "research_candidate_warning",
                        " ".join(warning.split())[:240],
                    )
                )

    filters: list[dict[str, Any]] = []
    if proposed_frequency is not None:
        filter_type = _filter_type(candidate or {})
        filters = [
            {
                "role": lower_role,
                "filter": "lowpass",
                "frequency_hz": round(proposed_frequency, 2),
                "filter_type": filter_type,
                "slope_db_per_octave": slope,
                "channel": _channel_payload(topology, group_id, lower_role),
            },
            {
                "role": upper_role,
                "filter": "highpass",
                "frequency_hz": round(proposed_frequency, 2),
                "filter_type": filter_type,
                "slope_db_per_octave": slope,
                "channel": _channel_payload(topology, group_id, upper_role),
            },
        ]

    return {
        "id": f"{group_id}:{lower_role}-{upper_role}",
        "between_roles": [lower_role, upper_role],
        "status": (
            "blocked"
            if any(issue["severity"] == "blocker" for issue in issues)
            else "ready_for_review"
        ),
        "source": str((candidate or {}).get("source") or "driver_research"),
        "candidate": dict(candidate or {}),
        "proposed_frequency_hz": (
            round(proposed_frequency, 2) if proposed_frequency is not None else None
        ),
        "do_not_test_below_hz": round(do_not_test, 2) if do_not_test is not None else None,
        "filters": filters,
        "issues": issues,
    }


def build_crossover_preview(
    design_draft: Mapping[str, Any],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Return a versioned crossover preview without hardware side effects."""

    now = created_at or _utc_now()
    issues: list[dict[str, str]] = []
    topology: OutputTopology | None = None
    topology_raw = _as_mapping(design_draft.get("topology"))
    if not topology_raw:
        issues.append(
            _issue(
                "blocker",
                "design_draft_topology_missing",
                "design draft has no topology",
            )
        )
    else:
        try:
            topology = OutputTopology.from_mapping(topology_raw)
        except OutputTopologyError as exc:
            issues.append(
                _issue(
                    "blocker",
                    "design_draft_topology_invalid",
                    f"saved topology is invalid: {exc}",
                )
            )

    draft_status = str(design_draft.get("status") or "unknown")
    if draft_status in {"not_saved", "unreadable"}:
        issues.append(
            _issue(
                "blocker",
                "design_draft_not_ready",
                "save a readable speaker design draft before preparing a crossover preview",
            )
        )
    elif draft_status == "needs_research":
        issues.append(
            _issue(
                "blocker",
                "design_draft_needs_research",
                "driver research is required before preparing a crossover preview",
            )
        )

    design_inputs = _merged_design_inputs(design_draft)
    if design_inputs is None:
        issues.append(
            _issue(
                "blocker",
                "driver_research_missing",
                "crossover settings are not saved",
            )
        )
    drivers, driver_issues = _driver_map(design_inputs)
    issues.extend(driver_issues)
    candidates = _candidate_map(design_inputs)

    groups: list[dict[str, Any]] = []
    active_crossover_count = 0
    if topology is not None:
        for blocker in topology.evaluation().get("blockers", []):
            if isinstance(blocker, Mapping):
                issues.append(
                    _issue(
                        "blocker",
                        str(blocker.get("code") or "output_topology_blocker"),
                        str(blocker.get("message") or "output topology is blocked"),
                    )
                )
        for group in topology.speaker_groups:
            pairs = _ACTIVE_ROLE_PAIRS.get(group.mode, ())
            if not pairs:
                continue
            crossovers = [
                _build_crossover(
                    topology=topology,
                    group_id=group.id,
                    lower_role=lower_role,
                    upper_role=upper_role,
                    drivers=drivers,
                    candidates=candidates,
                )
                for lower_role, upper_role in pairs
            ]
            active_crossover_count += len(crossovers)
            groups.append({
                "group_id": group.id,
                "label": group.label,
                "kind": group.kind,
                "mode": group.mode,
                "crossovers": crossovers,
            })

    if topology is not None and active_crossover_count == 0:
        issues.append(
            _issue(
                "warning",
                "active_crossover_not_applicable",
                "saved output topology has no active 2-way or 3-way speaker groups",
            )
        )

    crossover_issues = [
        issue
        for group in groups
        for crossover in group["crossovers"]
        for issue in crossover["issues"]
    ]
    all_issues = issues + crossover_issues
    blocker_count = sum(1 for issue in all_issues if issue["severity"] == "blocker")
    if blocker_count:
        status = "blocked"
    elif active_crossover_count == 0:
        status = "not_applicable"
    else:
        status = "ready_for_protected_staging"

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": CROSSOVER_PREVIEW_KIND,
        "status": status,
        "created_at": now,
        "updated_at": now,
        "source": {
            "design_draft_status": draft_status,
            "topology_id": topology.topology_id if topology else None,
            "design_draft_updated_at": design_draft.get("updated_at"),
            "design_draft_fingerprint": _design_draft_fingerprint(design_draft),
        },
        "drivers": {role: dict(driver) for role, driver in drivers.items()},
        "summary": {
            "speaker_group_count": len(groups),
            "active_crossover_count": active_crossover_count,
            "ready_crossover_count": sum(
                1
                for group in groups
                for crossover in group["crossovers"]
                if crossover["status"] == "ready_for_review"
            ),
            "blocker_count": blocker_count,
            "warning_count": sum(
                1 for issue in all_issues if issue["severity"] == "warning"
            ),
        },
        "groups": groups,
        "permissions": {
            "may_explain": True,
            "may_prepare_protected_startup_config": status == "ready_for_protected_staging",
            "may_not_emit_camilla_yaml": True,
            "may_not_load_camilla": True,
            "may_not_emit_audio": True,
            "may_not_authorize_playback": True,
        },
        "safety": {
            "no_audio": True,
            "loads_camilla": False,
            "applies_filters": False,
            "emits_camilla_yaml": False,
            "authorizes_playback": False,
            "requires_human_review": True,
            "requires_measurement_before_final": True,
        },
        "issues": all_issues,
        "next_step": (
            "Resolve design-draft and driver-research blockers before staging."
            if status == "blocked"
            else "This topology does not need an active crossover preview."
            if status == "not_applicable"
            else "Review the crossover preview, then stage a protected startup config in a separate step."
        ),
    }


def _stale_preview(
    preview: Mapping[str, Any],
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    out = dict(preview)
    out["status"] = "stale"
    out["permissions"] = dict(out.get("permissions") or {})
    out["permissions"]["may_prepare_protected_startup_config"] = False
    out["issues"] = [
        *[issue for issue in out.get("issues", []) if isinstance(issue, Mapping)],
        _issue("blocker", code, message),
    ]
    out["summary"] = dict(out.get("summary") or {})
    out["summary"]["blocker_count"] = sum(
        1 for issue in out["issues"] if issue.get("severity") == "blocker"
    )
    out["next_step"] = "Prepare a fresh crossover preview from the saved design draft."
    return out


def _validate_preview_freshness(
    preview: Mapping[str, Any],
    current_design_draft: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if current_design_draft is None:
        return dict(preview)
    if current_design_draft.get("status") in {"not_saved", "unreadable"}:
        return _stale_preview(
            preview,
            code="crossover_preview_design_draft_unavailable",
            message="current design draft is unavailable; prepare a fresh crossover preview",
        )

    source = _as_mapping(preview.get("source")) or {}
    expected = source.get("design_draft_fingerprint")
    actual = _design_draft_fingerprint(current_design_draft)
    if expected != actual:
        return _stale_preview(
            preview,
            code="crossover_preview_stale_design_draft",
            message="saved design draft changed after this crossover preview was prepared",
        )
    return dict(preview)


def load_crossover_preview(
    path: str | Path | None = None,
    *,
    current_design_draft: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the saved crossover preview, failing soft when absent."""

    target = crossover_preview_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": CROSSOVER_PREVIEW_KIND,
            "status": "not_prepared",
            "path": str(target),
            "summary": {},
            "groups": [],
            "issues": [],
            "next_step": "Prepare a crossover preview from the saved speaker design draft.",
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": CROSSOVER_PREVIEW_KIND,
            "status": "unreadable",
            "path": str(target),
            "summary": {},
            "groups": [],
            "issues": [
                _issue(
                    "blocker",
                    "crossover_preview_unreadable",
                    f"could not read active-speaker crossover preview: {type(exc).__name__}",
                )
            ],
            "next_step": "Prepare a fresh crossover preview.",
        }
    if not isinstance(raw, dict):
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": CROSSOVER_PREVIEW_KIND,
            "status": "unreadable",
            "path": str(target),
            "summary": {},
            "groups": [],
            "issues": [
                _issue(
                    "blocker",
                    "crossover_preview_not_object",
                    "active-speaker crossover preview is not a JSON object",
                )
            ],
            "next_step": "Prepare a fresh crossover preview.",
        }
    if raw.get("artifact_schema_version") != SCHEMA_VERSION or raw.get("kind") != CROSSOVER_PREVIEW_KIND:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": CROSSOVER_PREVIEW_KIND,
            "status": "unreadable",
            "path": str(target),
            "summary": {},
            "groups": [],
            "issues": [
                _issue(
                    "blocker",
                    "crossover_preview_unsupported_schema",
                    "active-speaker crossover preview has an unsupported schema",
                )
            ],
            "next_step": "Prepare a fresh crossover preview.",
        }
    raw["path"] = str(target)
    return _validate_preview_freshness(raw, current_design_draft)


def save_crossover_preview(
    design_draft: Mapping[str, Any],
    *,
    path: str | Path | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Persist a crossover preview atomically. This does not authorize audio."""

    target = crossover_preview_path(path)
    prior = load_crossover_preview(target)
    preview = build_crossover_preview(
        design_draft,
        created_at=created_at or (
            prior.get("created_at")
            if prior.get("status") not in {"not_prepared", "unreadable"}
            else None
        ),
    )
    preview["path"] = str(target)
    preview["updated_at"] = _utc_now() if created_at is None else preview["updated_at"]
    atomic_write_text(
        target,
        json.dumps(preview, indent=2, sort_keys=True) + "\n",
        mode=0o640,
    )
    return preview
