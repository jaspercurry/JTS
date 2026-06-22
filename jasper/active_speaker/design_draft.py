# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persisted active-speaker design draft.

The design draft is the durable bridge between the user-facing output map and
future crossover compilation. It records what the operator is trying to build
and any externally researched driver facts, but it does not compile filters,
load CamillaDSP, or authorize playback.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.output_topology import OutputTopology
from ._common import issue as _issue

SCHEMA_VERSION = 1
DESIGN_DRAFT_KIND = "jts_active_speaker_design_draft"
DRIVER_RESEARCH_KIND = "jts_active_crossover_driver_research"
DEFAULT_DESIGN_DRAFT_PATH = Path("/var/lib/jasper/active_speaker_design_draft.json")
DESIGN_DRAFT_PATH_ENV = "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"

_SUPPORTED_RESEARCH_ROLES = {"full_range", "woofer", "mid", "tweeter", "subwoofer"}
_SUPPORTED_CONFIDENCE = {"low", "medium", "high", "unknown"}
_MAX_DRIVERS = 16
_MAX_CANDIDATES = 16
_MAX_SOURCES = 8
_CROSSOVER_ROLE_PAIRS = {
    "active_2_way": (("woofer", "tweeter"),),
    "active_3_way": (("woofer", "mid"), ("mid", "tweeter")),
}


class ActiveSpeakerDesignDraftError(ValueError):
    """Raised when a design draft or research packet has an unsupported shape."""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _design_draft_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(DESIGN_DRAFT_PATH_ENV) or DEFAULT_DESIGN_DRAFT_PATH)


def _mapping(raw: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be an object")
    return raw


def _sequence(raw: Any, field_name: str, *, limit: int | None = None) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be a list")
    if limit is not None and len(raw) > limit:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must contain <= {limit} items")
    return raw


def _text(raw: Any, field_name: str, *, required: bool = False, max_chars: int = 240) -> str | None:
    if raw is None or raw == "":
        if required:
            raise ActiveSpeakerDesignDraftError(f"{field_name} is required")
        return None
    if not isinstance(raw, str):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be a string")
    out = " ".join(raw.split())
    if not out:
        if required:
            raise ActiveSpeakerDesignDraftError(f"{field_name} is required")
        return None
    if len(out) > max_chars:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be <= {max_chars} chars")
    return out


def _finite_float(raw: Any, field_name: str) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        out = float(raw)
    except (TypeError, ValueError) as exc:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be numeric") from exc
    if not math.isfinite(out):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be finite")
    return out


def _positive_float(raw: Any, field_name: str) -> float | None:
    out = _finite_float(raw, field_name)
    if out is not None and out <= 0:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be > 0")
    return out


def _role(raw: Any, field_name: str) -> str:
    role = _text(raw, field_name, required=True, max_chars=40)
    if role not in _SUPPORTED_RESEARCH_ROLES:
        raise ActiveSpeakerDesignDraftError(f"{field_name} is unsupported: {role}")
    return role


def _string_list(raw: Any, field_name: str, *, limit: int = _MAX_SOURCES) -> list[str]:
    return [
        value
        for value in (
            _text(item, f"{field_name}[]", max_chars=320)
            for item in _sequence(raw, field_name, limit=limit)
        )
        if value
    ]


def _frequency_range(raw: Any, field_name: str) -> list[float] | None:
    if raw is None:
        return None
    values = _sequence(raw, field_name, limit=2)
    if len(values) != 2:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must contain two values")
    low = _positive_float(values[0], f"{field_name}[0]")
    high = _positive_float(values[1], f"{field_name}[1]")
    if low is None or high is None or low >= high:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be an increasing range")
    return [low, high]


def _normalise_driver(raw: Any) -> dict[str, Any]:
    raw = _mapping(raw, "driver")
    driver: dict[str, Any] = {
        "role": _role(raw.get("role"), "driver.role"),
        "model": _text(raw.get("model"), "driver.model", required=True, max_chars=120),
        "manufacturer": _text(raw.get("manufacturer"), "driver.manufacturer", max_chars=120),
        "nominal_impedance_ohm": _positive_float(
            raw.get("nominal_impedance_ohm"),
            "driver.nominal_impedance_ohm",
        ),
        "sensitivity_db_2v83_1m": _finite_float(
            raw.get("sensitivity_db_2v83_1m"),
            "driver.sensitivity_db_2v83_1m",
        ),
        "usable_frequency_range_hz": _frequency_range(
            raw.get("usable_frequency_range_hz"),
            "driver.usable_frequency_range_hz",
        ),
        "recommended_highpass_hz": _positive_float(
            raw.get("recommended_highpass_hz"),
            "driver.recommended_highpass_hz",
        ),
        "recommended_lowpass_hz": _positive_float(
            raw.get("recommended_lowpass_hz"),
            "driver.recommended_lowpass_hz",
        ),
        "do_not_test_below_hz": _positive_float(
            raw.get("do_not_test_below_hz"),
            "driver.do_not_test_below_hz",
        ),
        "gain_offset_db": _finite_float(
            raw.get("gain_offset_db"),
            "driver.gain_offset_db",
        ),
        "notes": _text(raw.get("notes"), "driver.notes", max_chars=1000),
        "sources": _string_list(raw.get("sources"), "driver.sources"),
    }
    return {key: value for key, value in driver.items() if value not in (None, [])}


def _normalise_manual_driver(raw: Any) -> dict[str, Any]:
    raw = _mapping(raw, "manual_settings.driver")
    driver: dict[str, Any] = {
        "role": _role(raw.get("role"), "manual_settings.driver.role"),
        "model": _text(raw.get("model"), "manual_settings.driver.model", max_chars=120),
        "manufacturer": _text(
            raw.get("manufacturer"),
            "manual_settings.driver.manufacturer",
            max_chars=120,
        ),
        "nominal_impedance_ohm": _positive_float(
            raw.get("nominal_impedance_ohm"),
            "manual_settings.driver.nominal_impedance_ohm",
        ),
        "sensitivity_db_2v83_1m": _finite_float(
            raw.get("sensitivity_db_2v83_1m"),
            "manual_settings.driver.sensitivity_db_2v83_1m",
        ),
        "usable_frequency_range_hz": _frequency_range(
            raw.get("usable_frequency_range_hz"),
            "manual_settings.driver.usable_frequency_range_hz",
        ),
        "recommended_highpass_hz": _positive_float(
            raw.get("recommended_highpass_hz"),
            "manual_settings.driver.recommended_highpass_hz",
        ),
        "recommended_lowpass_hz": _positive_float(
            raw.get("recommended_lowpass_hz"),
            "manual_settings.driver.recommended_lowpass_hz",
        ),
        "do_not_test_below_hz": _positive_float(
            raw.get("do_not_test_below_hz"),
            "manual_settings.driver.do_not_test_below_hz",
        ),
        "gain_offset_db": _finite_float(
            raw.get("gain_offset_db"),
            "manual_settings.driver.gain_offset_db",
        ),
        "notes": _text(raw.get("notes"), "manual_settings.driver.notes", max_chars=1000),
    }
    return {key: value for key, value in driver.items() if value not in (None, [])}


def _normalise_candidate(raw: Any) -> dict[str, Any]:
    raw = _mapping(raw, "crossover_candidate")
    roles = [
        _role(item, "crossover_candidate.between_roles[]")
        for item in _sequence(
            raw.get("between_roles"),
            "crossover_candidate.between_roles",
            limit=2,
        )
    ]
    if len(roles) != 2:
        raise ActiveSpeakerDesignDraftError(
            "crossover_candidate.between_roles must contain two roles"
        )
    confidence = _text(
        raw.get("confidence", "unknown"),
        "crossover_candidate.confidence",
        max_chars=20,
    ) or "unknown"
    if confidence not in _SUPPORTED_CONFIDENCE:
        raise ActiveSpeakerDesignDraftError(
            "crossover_candidate.confidence must be low, medium, high, or unknown"
        )
    candidate: dict[str, Any] = {
        "between_roles": roles,
        "frequency_hz": _positive_float(
            raw.get("frequency_hz"),
            "crossover_candidate.frequency_hz",
        ),
        "filter_type": _text(
            raw.get("filter_type"),
            "crossover_candidate.filter_type",
            max_chars=80,
        ),
        "slope_db_per_octave": _positive_float(
            raw.get("slope_db_per_octave"),
            "crossover_candidate.slope_db_per_octave",
        ),
        "confidence": confidence,
        "rationale": _text(
            raw.get("rationale"),
            "crossover_candidate.rationale",
            max_chars=1000,
        ),
        "warnings": _string_list(
            raw.get("warnings"),
            "crossover_candidate.warnings",
            limit=8,
        ),
    }
    return {key: value for key, value in candidate.items() if value not in (None, [])}


def normalise_driver_research(raw: Any) -> dict[str, Any] | None:
    """Return a bounded driver-research packet, or ``None`` when absent."""

    if raw is None or raw == "":
        return None
    raw = _mapping(raw, "driver_research")
    if raw.get("artifact_schema_version") != SCHEMA_VERSION:
        raise ActiveSpeakerDesignDraftError(
            "driver_research.artifact_schema_version must be 1"
        )
    if raw.get("kind") != DRIVER_RESEARCH_KIND:
        raise ActiveSpeakerDesignDraftError(
            f"driver_research.kind must be {DRIVER_RESEARCH_KIND}"
        )
    drivers = [
        _normalise_driver(item)
        for item in _sequence(raw.get("drivers"), "driver_research.drivers", limit=_MAX_DRIVERS)
    ]
    if not drivers:
        raise ActiveSpeakerDesignDraftError("driver_research.drivers is required")
    candidates = [
        _normalise_candidate(item)
        for item in _sequence(
            raw.get("crossover_candidates"),
            "driver_research.crossover_candidates",
            limit=_MAX_CANDIDATES,
        )
    ]
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": drivers,
        "crossover_candidates": candidates,
        "human_review": {
            "must_verify_wiring": True,
            "must_start_quiet": True,
            "needs_measurement_before_final": True,
        },
    }


def normalise_manual_settings(raw: Any) -> dict[str, Any] | None:
    """Return bounded operator-entered crossover settings, or ``None`` when absent."""

    if raw is None or raw == "":
        return None
    raw = _mapping(raw, "manual_settings")
    drivers = [
        _normalise_manual_driver(item)
        for item in _sequence(raw.get("drivers"), "manual_settings.drivers", limit=_MAX_DRIVERS)
    ]
    candidates = [
        _normalise_candidate(item)
        for item in _sequence(
            raw.get("crossover_candidates"),
            "manual_settings.crossover_candidates",
            limit=_MAX_CANDIDATES,
        )
    ]
    drivers = [
        {**driver, "source": "manual_settings"}
        for driver in drivers
        if len(driver) > 1
    ]
    candidates = [
        {
            **candidate,
            "source": "manual_settings",
            "confidence": candidate.get("confidence") or "medium",
        }
        for candidate in candidates
        if candidate.get("frequency_hz") is not None
    ]
    if not drivers and not candidates:
        return None
    return {
        "drivers": drivers,
        "crossover_candidates": candidates,
    }


def normalise_operator_inputs(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, Mapping) else {}
    out: dict[str, str] = {}
    for key in ("full_range", "woofer", "mid", "tweeter", "subwoofer", "notes"):
        value = _text(raw.get(key), f"operator_inputs.{key}", max_chars=1000 if key == "notes" else 160)
        if value:
            out[key] = value
    return out


def _topology_roles(topology: OutputTopology) -> list[str]:
    roles: list[str] = []
    for group in topology.speaker_groups:
        for channel in group.channels:
            if channel.role not in roles:
                roles.append(channel.role)
    order = {"full_range": 0, "woofer": 1, "mid": 2, "tweeter": 3, "subwoofer": 4}
    return sorted(roles, key=lambda role: order.get(role, 99))


def _candidate_roles(candidates: list[dict[str, Any]]) -> set[frozenset[str]]:
    out: set[frozenset[str]] = set()
    for candidate in candidates:
        roles = candidate.get("between_roles")
        if isinstance(roles, list) and len(roles) == 2:
            out.add(frozenset(str(role) for role in roles))
    return out


def _active_crossover_pairs(topology: OutputTopology) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for group in topology.speaker_groups:
        for pair in _CROSSOVER_ROLE_PAIRS.get(group.mode, ()):
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def _summary(
    topology: OutputTopology,
    driver_research: dict[str, Any] | None,
    manual_settings: dict[str, Any] | None,
) -> dict[str, Any]:
    topology_roles = _topology_roles(topology)
    research_roles = []
    if driver_research:
        for driver in driver_research.get("drivers", []):
            role = driver.get("role")
            if role and role not in research_roles:
                research_roles.append(role)
    candidates = driver_research.get("crossover_candidates", []) if driver_research else []
    manual_drivers = manual_settings.get("drivers", []) if manual_settings else []
    manual_candidates = (
        manual_settings.get("crossover_candidates", []) if manual_settings else []
    )
    manual_roles = []
    for driver in manual_drivers:
        role = driver.get("role")
        if role and role not in manual_roles:
            manual_roles.append(role)
    combined_roles = set(research_roles) | set(manual_roles)
    combined_candidate_roles = _candidate_roles(candidates) | _candidate_roles(manual_candidates)
    missing_candidate_pairs = [
        list(pair)
        for pair in _active_crossover_pairs(topology)
        if frozenset(pair) not in combined_candidate_roles
    ]
    return {
        "speaker_group_count": len(topology.speaker_groups),
        "topology_roles": topology_roles,
        "driver_count": len(driver_research.get("drivers", [])) if driver_research else 0,
        "research_roles": research_roles,
        "missing_research_roles": [
            role for role in topology_roles if role not in research_roles
        ],
        "extra_research_roles": [
            role for role in research_roles if role not in topology_roles
        ],
        "crossover_candidate_count": len(candidates),
        "manual_driver_count": len(manual_drivers),
        "manual_crossover_candidate_count": len(manual_candidates),
        "manual_roles": manual_roles,
        "missing_driver_info_roles": [
            role for role in topology_roles if role not in combined_roles
        ],
        "missing_crossover_candidate_pairs": missing_candidate_pairs,
        "candidate_frequencies_hz": [
            candidate.get("frequency_hz")
            for candidate in [*candidates, *manual_candidates]
            if candidate.get("frequency_hz") is not None
        ],
        "warning_count": sum(
            len(candidate.get("warnings", []))
            for candidate in [*candidates, *manual_candidates]
            if isinstance(candidate, Mapping)
        ),
    }


def build_design_draft(
    topology: OutputTopology,
    *,
    driver_research: Any = None,
    manual_settings: Any = None,
    operator_inputs: Any = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a versioned, non-authoritative speaker design draft."""

    research = normalise_driver_research(driver_research)
    manual = normalise_manual_settings(manual_settings)
    inputs = normalise_operator_inputs(operator_inputs)
    evaluation = topology.evaluation()
    summary = _summary(topology, research, manual)
    issues: list[dict[str, str]] = []
    if not topology.speaker_groups:
        issues.append(_issue("blocker", "output_topology_empty", "choose and save a speaker layout"))
    for blocker in evaluation.get("blockers", []):
        if isinstance(blocker, Mapping):
            issues.append(
                _issue(
                    "blocker",
                    str(blocker.get("code") or "output_topology_blocker"),
                    str(blocker.get("message") or "output topology is blocked"),
                )
            )
    if research is None:
        issues.append(
            _issue(
                "warning",
                "driver_research_missing",
                "AI driver research is not saved; manual crossover settings may still be used",
            )
        )
    for role in summary["missing_driver_info_roles"]:
        issues.append(
            _issue(
                "warning",
                "driver_role_info_missing",
                f"no driver info saved for {role}",
            )
        )
    for pair in summary["missing_crossover_candidate_pairs"]:
        issues.append(
            _issue(
                "warning",
                "crossover_setting_missing",
                f"no crossover point saved for {'/'.join(pair)}",
            )
        )
    if any(issue["severity"] == "blocker" for issue in issues):
        status = "blocked"
    elif summary["missing_driver_info_roles"] or summary["missing_crossover_candidate_pairs"]:
        status = "needs_research"
    else:
        status = "ready_for_review"
    now = created_at or _utc_now()
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": DESIGN_DRAFT_KIND,
        "status": status,
        "created_at": now,
        "updated_at": now,
        "topology": topology.to_dict(include_evaluation=True),
        "operator_inputs": inputs,
        "driver_research": research,
        "manual_settings": manual,
        "summary": summary,
        "permissions": {
            "may_explain": True,
            "may_recommend_research_or_measurement": True,
            "may_suggest_bounded_crossover_starting_points": True,
            "may_not_apply_filters": True,
            "may_not_load_camilla": True,
            "may_not_emit_audio": True,
        },
        "safety": {
            "no_audio": True,
            "loads_camilla": False,
            "applies_filters": False,
            "authorizes_playback": False,
            "requires_human_review": True,
        },
        "issues": issues,
        "next_step": (
            "Resolve output-map blockers before using this draft."
            if status == "blocked"
            else "Add or review crossover settings before compiling a speaker preset."
            if status == "needs_research"
            else "Review the crossover settings before preparing a no-audio preview."
        ),
    }


def load_design_draft(path: str | Path | None = None) -> dict[str, Any]:
    """Return the saved design draft, failing soft when it has not been saved."""

    target = _design_draft_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": DESIGN_DRAFT_KIND,
            "status": "not_saved",
            "path": str(target),
            "driver_research": None,
            "manual_settings": None,
            "operator_inputs": {},
            "summary": {},
            "issues": [],
            "next_step": "Save a speaker design draft from /sound/.",
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": DESIGN_DRAFT_KIND,
            "status": "unreadable",
            "path": str(target),
            "driver_research": None,
            "manual_settings": None,
            "operator_inputs": {},
            "summary": {},
            "issues": [
                _issue(
                    "blocker",
                    "design_draft_unreadable",
                    f"could not read active-speaker design draft: {type(exc).__name__}",
                )
            ],
            "next_step": "Save a fresh speaker design draft.",
        }
    if not isinstance(raw, dict):
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": DESIGN_DRAFT_KIND,
            "status": "unreadable",
            "path": str(target),
            "driver_research": None,
            "manual_settings": None,
            "operator_inputs": {},
            "summary": {},
            "issues": [
                _issue(
                    "blocker",
                    "design_draft_not_object",
                    "active-speaker design draft is not a JSON object",
                )
            ],
            "next_step": "Save a fresh speaker design draft.",
        }
    if raw.get("artifact_schema_version") != SCHEMA_VERSION or raw.get("kind") != DESIGN_DRAFT_KIND:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": DESIGN_DRAFT_KIND,
            "status": "unreadable",
            "path": str(target),
            "driver_research": None,
            "manual_settings": None,
            "operator_inputs": {},
            "summary": {},
            "issues": [
                _issue(
                    "blocker",
                    "design_draft_unsupported_schema",
                    "active-speaker design draft has an unsupported schema",
                )
            ],
            "next_step": "Save a fresh speaker design draft.",
        }
    return raw


def save_design_draft(
    topology: OutputTopology,
    *,
    driver_research: Any = None,
    manual_settings: Any = None,
    operator_inputs: Any = None,
    path: str | Path | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Persist a design draft atomically. This does not authorize playback."""

    target = _design_draft_path(path)
    prior = load_design_draft(target)
    draft = build_design_draft(
        topology,
        driver_research=driver_research,
        manual_settings=manual_settings,
        operator_inputs=operator_inputs,
        created_at=created_at or (
            prior.get("created_at") if prior.get("status") != "not_saved" else None
        ),
    )
    draft["path"] = str(target)
    draft["updated_at"] = _utc_now() if created_at is None else draft["updated_at"]
    atomic_write_text(
        target,
        json.dumps(draft, indent=2, sort_keys=True) + "\n",
        mode=0o640,
    )
    return draft
