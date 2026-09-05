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
from ._common import ACTIVE_CROSSOVER_ROLE_PAIRS, issue as _issue
from .driver_protection import (
    LOW_LIMIT_DECLARED,
    PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE,
    apply_driver_low_limit,
    declared_protection_highpass_floor_hz,
    driver_protection_profile,
    format_protection_hz,
    protection_highpass_floor_satisfied,
    resolve_driver_low_limit,
)

#: Bumped to 2 for the one-owner low-limit collapse (#2603). A preview saved
#: before it carries UN-DERIVED driver payloads, and staging compiles the
#: preset straight from those, so such a file must not be reused. It cannot be
#: caught by a content re-prove -- ``crossover_preview_fingerprint`` reads the
#: version out of the artifact itself, so an old preview stays self-consistent
#: -- which is why the version is what moves. ``load_crossover_preview``'s
#: existing guard turns that into the actionable "Prepare a fresh crossover
#: preview."
SCHEMA_VERSION = 2
CROSSOVER_PREVIEW_KIND = "jts_active_speaker_crossover_preview"
DEFAULT_CROSSOVER_PREVIEW_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_preview.json"
)
CROSSOVER_PREVIEW_PATH_ENV = "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE"

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
#: What a candidate that declares no filter/slope is previewed and compiled as.
#: Public because the /sound/ crossover editor must pre-select the SAME member
#: of the offered vocabulary this module would fill in — a second default in the
#: page would let the editor show one filter and the compiler build another.
#: Both are members of the compiler's own vocabulary, pinned by
#: ``tests/test_crossover_declaration.py``.
DEFAULT_FILTER_TYPE = "Linkwitz-Riley"
DEFAULT_SLOPE_DB_PER_OCTAVE = 24.0


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
        "manual_settings": design_draft.get("manual_settings"),
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def crossover_preview_fingerprint(preview: Mapping[str, Any]) -> str:
    """Fingerprint the exact normalized preview content that can compile.

    Volatile persistence/UI metadata is deliberately excluded. Every field
    consumed by protected staging is included, including the normalized driver
    and crossover candidate payloads. ``source.preview_fingerprint`` itself is
    excluded so a saved preview can carry and re-prove this identity.
    """

    source = _as_mapping(preview.get("source")) or {}
    stable = {
        "artifact_schema_version": preview.get("artifact_schema_version"),
        "kind": preview.get("kind"),
        "status": preview.get("status"),
        "source": {
            "design_draft_status": source.get("design_draft_status"),
            "topology_id": source.get("topology_id"),
            "design_draft_fingerprint": source.get("design_draft_fingerprint"),
        },
        "drivers": preview.get("drivers"),
        "groups": preview.get("groups"),
        "permissions": preview.get("permissions"),
        "safety": preview.get("safety"),
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


def _range_ceiling(driver: Mapping[str, Any] | None) -> float | None:
    if not driver:
        return None
    raw = driver.get("usable_frequency_range_hz")
    if isinstance(raw, list) and len(raw) >= 2:
        return _finite_positive(raw[1])
    return None


def _driver_style_for_role(topology: OutputTopology, role: str) -> str | None:
    """The topology-declared driver style for one role, or ``None``."""

    for group in topology.speaker_groups:
        for channel in group.channels:
            if channel.role == role and channel.driver_style:
                return str(channel.driver_style)
    return None


# A corner below the declared protection floor is DISCLOSED here and REFUSED by
# ``path_safety`` at load (#2491); this page must not block it, or that load gate
# becomes unreachable.


def _filter_type(candidate: Mapping[str, Any]) -> str:
    raw = candidate.get("filter_type")
    if isinstance(raw, str) and raw.strip():
        return " ".join(raw.split())[:80]
    return DEFAULT_FILTER_TYPE


def _slope(candidate: Mapping[str, Any]) -> float:
    return (
        _finite_positive(candidate.get("slope_db_per_octave"))
        or DEFAULT_SLOPE_DB_PER_OCTAVE
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
    # #2491 disclosure. Preview is the household's confirm-and-commit surface,
    # and it was silent about a candidate crossing below the upper driver's own
    # confirmed protective high-pass floor. Preview stays advisory — the hard
    # refusal for this condition lives on the load gate (path_safety) — but a
    # blocker_count of 0 on a design the next gate must refuse is a dishonest
    # green, so the conflict is named here at confirm time. Scoped to the
    # tweeter: that is the role whose derived protective high-pass is clamped
    # to this same floor and the role the load gate refuses on.
    protection_floor = (
        declared_protection_highpass_floor_hz(upper_driver)
        if upper_role == "tweeter"
        else None
    )
    if proposed_frequency is not None and not protection_highpass_floor_satisfied(
        highpass_hz=proposed_frequency,
        floor_hz=protection_floor,
    ):
        issues.append(
            _issue(
                "warning",
                "crossover_below_declared_protection_floor",
                (
                    f"{upper_role} declares a protective high-pass floor of "
                    f"{format_protection_hz(protection_floor)}; crossing at "
                    f"{format_protection_hz(proposed_frequency)} sits below it "
                    "and the protected startup load will refuse this design"
                ),
            )
        )
    # Disclose-and-recommend, never nanny (#2603). A declared low limit BELOW
    # its style's class default is legal and wins -- that is the ruling -- but
    # the household confirms designs on this page, so the disagreement is named
    # here rather than left for someone to discover. Never fires for an
    # inferred or defaulted limit: only a number a human or a research reply
    # actually declared can disagree with the default.
    upper_style = _driver_style_for_role(topology, upper_role)
    upper_limit = resolve_driver_low_limit(
        upper_driver, role=upper_role, driver_style=upper_style
    )
    style_default_hz = driver_protection_profile(
        upper_role, driver_style=upper_style
    ).min_highpass_hz
    if (
        upper_limit is not None
        and upper_limit.provenance == LOW_LIMIT_DECLARED
        and style_default_hz is not None
        and upper_limit.frequency_hz < style_default_hz
    ):
        issues.append(
            _issue(
                "warning",
                "low_limit_below_style_default",
                (
                    f"{upper_role} declares a minimum crossover of "
                    f"{format_protection_hz(upper_limit.frequency_hz)}, below the "
                    f"{format_protection_hz(style_default_hz)} default for its "
                    "driver type; confirm this is the manufacturer's published "
                    "figure"
                ),
            )
        )
    ceiling = _range_ceiling(lower_driver)
    if proposed_frequency is not None and ceiling is not None and proposed_frequency > ceiling:
        issues.append(
            _issue(
                "blocker",
                "crossover_frequency_above_lower_driver_range",
                f"{lower_role} research only claims usable response to {round(ceiling)} Hz",
            )
        )

    # Disclose-and-recommend, the same rule as the low-limit warning above and
    # the same number the round's own receipt discloses against
    # (``TopologyPrescription.recommended_slope_db_per_octave``). Read from the
    # constant rather than typed here, because a second spelling of a
    # recommendation is how the design page and the receipt come to name two
    # different floors for one build.
    slope = _slope(candidate or {})
    if upper_role == "tweeter" and slope < PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE:
        issues.append(
            _issue(
                "warning",
                "tweeter_slope_below_recommended_floor",
                "tweeter crossover slope is below the conservative "
                f"{PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE:g} dB/octave floor",
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

    # Persisted working-crossover values (Slice 0): copy polarity/delay from the
    # candidate, absent-in -> absent-out. The candidate's own ``lower_polarity``/
    # ``upper_polarity`` describe ``candidate["between_roles"][0]``/``[1]`` — the
    # frozenset lookup above loses that order, so realign to THIS function's own
    # (lower_role, upper_role) convention before copying, or a reversed candidate
    # would silently swap which driver gets inverted.
    lower_polarity: str | None = None
    upper_polarity: str | None = None
    delay_ms: float | None = None
    delay_target_role: str | None = None
    if candidate is not None:
        candidate_between = candidate.get("between_roles")
        if candidate_between == [lower_role, upper_role]:
            lower_polarity = candidate.get("lower_polarity")
            upper_polarity = candidate.get("upper_polarity")
        elif candidate_between == [upper_role, lower_role]:
            lower_polarity = candidate.get("upper_polarity")
            upper_polarity = candidate.get("lower_polarity")
        delay_ms = candidate.get("delay_ms")
        delay_target_role = candidate.get("delay_target_role")

    out: dict[str, Any] = {
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
    }
    if lower_polarity is not None:
        out["lower_polarity"] = lower_polarity
    if upper_polarity is not None:
        out["upper_polarity"] = upper_polarity
    if delay_ms is not None:
        out["delay_ms"] = delay_ms
    if delay_target_role is not None:
        out["delay_target_role"] = delay_target_role
    out["declared_protection_floor_hz"] = (
        round(protection_floor, 2) if protection_floor is not None else None
    )
    out["filters"] = filters
    out["issues"] = issues
    return out


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
    if topology is not None:
        # One owner, every consumer derives (#2603). Stamped HERE, before the
        # per-crossover checks read a floor and before ``preview["drivers"]`` is
        # frozen -- staging compiles the preset from that payload, so this is
        # what makes the emitted graph, the load gate, and this page's own
        # disclosures agree on where each driver stops.
        drivers = {
            role: apply_driver_low_limit(
                driver,
                role=role,
                driver_style=_driver_style_for_role(topology, role),
            )
            for role, driver in drivers.items()
        }
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
            pairs = ACTIVE_CROSSOVER_ROLE_PAIRS.get(group.mode, ())
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

    preview = {
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
    preview["source"]["preview_fingerprint"] = crossover_preview_fingerprint(preview)
    return preview


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
    source = _as_mapping(preview.get("source")) or {}
    declared_preview_fingerprint = source.get("preview_fingerprint")
    actual_preview_fingerprint = crossover_preview_fingerprint(preview)
    if (
        not declared_preview_fingerprint
        or declared_preview_fingerprint != actual_preview_fingerprint
    ):
        return _stale_preview(
            preview,
            code="crossover_preview_content_mismatch",
            message=(
                "saved crossover preview content no longer matches its frozen "
                "candidate; prepare a fresh crossover preview"
            ),
        )
    if current_design_draft is None:
        return dict(preview)
    if current_design_draft.get("status") in {"not_saved", "unreadable"}:
        return _stale_preview(
            preview,
            code="crossover_preview_design_draft_unavailable",
            message="current design draft is unavailable; prepare a fresh crossover preview",
        )

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
    durable: bool = False,
) -> dict[str, Any]:
    """Persist a crossover preview atomically. This does not authorize audio.

    ``durable=True`` fsyncs the write before it is visible (see
    :func:`jasper.atomic_io.atomic_write_text`). The default stays ``False``
    for the routine Preview regenerations; the crossover-accept seam opts in
    explicitly so the accepted preview survives a power loss, not just a torn
    write.
    """

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
    # group_from_parent: same reason as the design-draft store this preview is
    # derived from. The crossover-accept seam re-prepares it from the ROOT
    # jasper-correction-web process while /sound/ reads it as jasper-web, so a
    # root write without this hides the preview behind root:root 0640.
    atomic_write_text(
        target,
        json.dumps(preview, indent=2, sort_keys=True) + "\n",
        mode=0o640,
        durable=durable,
    )
    return preview
