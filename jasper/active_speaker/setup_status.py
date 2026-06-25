# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Product-level active-speaker setup readiness.

This is the single, household-facing contract for whether an active speaker is
ready for normal output controls and grouping. Lower-level modules still own
their detailed graph/proof work; this module composes their durable artifacts
into the answer that UI, control, and multiroom gates consume.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from jasper.output_topology import OutputTopologyError, load_output_topology_strict

from .baseline_profile import (
    baseline_profile_state_path,
    build_baseline_profile_candidate,
)
from .crossover_preview import load_crossover_preview
from .design_draft import load_design_draft
from .measurement import load_measurement_state

SETUP_STATUS_KIND = "jts_active_speaker_setup_status"

_CAMILLA_STATEFILE_ENV = "JASPER_CAMILLA_STATEFILE"
_DEFAULT_CAMILLA_STATEFILE = "/var/lib/camilladsp/outputd-statefile.yml"
_STAGED_CONFIG_BASENAMES = {
    "active_speaker_staged_startup.yml",
    "active_speaker_commissioning.yml",
}
_READINESS_DERIVATION_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    KeyError,
)


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _active_group_count(topology: Any) -> int:
    return sum(
        1 for group in getattr(topology, "speaker_groups", ())
        if getattr(group, "mode", "") in {"active_2_way", "active_3_way"}
    )


def active_config_path_from_statefile(
    path: str | Path | None = None,
) -> str:
    """Best-effort active CamillaDSP config path from the outputd statefile."""

    statefile = Path(
        path or os.environ.get(_CAMILLA_STATEFILE_ENV) or _DEFAULT_CAMILLA_STATEFILE
    )
    try:
        text = statefile.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^\s*config_path:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip().strip("'\"")


def read_active_speaker_setup_status(
    *,
    active_config_path: str | None = None,
    baseline_state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the authoritative active-speaker setup readiness snapshot.

    For a passive/ordinary speaker, active setup is not required and both
    ``volume_allowed`` and ``grouping_allowed`` are true. For an active speaker,
    the durable baseline profile must be applied and the active CamillaDSP config
    must not be one of the commissioning/staged safety graphs.

    Total + fail-closed for active-output safety: an unreadable topology or
    unreadable baseline profile returns a blocked snapshot instead of silently
    treating the speaker as ready.
    """

    issues: list[dict[str, str]] = []
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        issues.append(_issue(
            "blocker",
            "output_topology_unreadable",
            f"output topology cannot be read safely: {exc}",
        ))
        return {
            "artifact_schema_version": 1,
            "kind": SETUP_STATUS_KIND,
            "active": True,
            "active_group_count": None,
            "status": "unknown",
            "configured": False,
            "volume_allowed": False,
            "grouping_allowed": False,
            "safety_muted": True,
            "reason": "output_topology_unreadable",
            "detail": "output topology cannot be read safely",
            "active_config_path": active_config_path or None,
            "baseline_profile": None,
            "issues": issues,
        }

    active_group_count = _active_group_count(topology)
    if active_group_count == 0:
        return {
            "artifact_schema_version": 1,
            "kind": SETUP_STATUS_KIND,
            "active": False,
            "active_group_count": 0,
            "status": "not_active",
            "configured": True,
            "volume_allowed": True,
            "grouping_allowed": True,
            "safety_muted": False,
            "reason": None,
            "detail": "speaker does not use an active crossover",
            "active_config_path": active_config_path or None,
            "baseline_profile": None,
            "issues": [],
        }

    config_path = active_config_path
    if config_path is None:
        config_path = active_config_path_from_statefile()
    config_basename = os.path.basename(config_path or "")
    if not config_path:
        issues.append(_issue(
            "blocker",
            "active_config_path_unknown",
            "current CamillaDSP config path is unavailable",
        ))
    elif config_basename in _STAGED_CONFIG_BASENAMES:
        issues.append(_issue(
            "blocker",
            "active_speaker_commissioning_config_loaded",
            "active speaker setup/commissioning graph is loaded",
        ))

    profile_summary: dict[str, Any] | None = None
    try:
        design_draft = load_design_draft()
        crossover_preview = load_crossover_preview(
            current_design_draft=design_draft,
        )
        measurements = load_measurement_state(topology)
        profile = build_baseline_profile_candidate(
            topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            write=False,
            state_path=baseline_state_path,
        )
    except _READINESS_DERIVATION_ERRORS as exc:
        profile = None
        issues.append(_issue(
            "blocker",
            "active_baseline_profile_unreadable",
            f"active speaker baseline readiness could not be derived: {type(exc).__name__}",
        ))

    if profile is not None:
        raw_config = profile.get("config")
        config: Mapping[str, Any] = (
            raw_config
            if isinstance(raw_config, Mapping)
            else {}
        )
        raw_source = profile.get("source")
        source: Mapping[str, Any] = (
            raw_source
            if isinstance(raw_source, Mapping)
            else {}
        )
        raw_revalidation = profile.get("revalidation")
        revalidation: Mapping[str, Any] = (
            raw_revalidation
            if isinstance(raw_revalidation, Mapping)
            else {"required": False, "status": "not_required"}
        )
        profile_issues = [
            {
                "severity": str(issue.get("severity") or "blocker"),
                "code": str(issue.get("code") or "baseline_profile_issue"),
                "message": str(issue.get("message") or "active speaker baseline issue"),
            }
            for issue in profile.get("issues", [])
            if isinstance(issue, Mapping)
        ]
        profile_summary = {
            "status": profile.get("status"),
            "path": str(baseline_profile_state_path(baseline_state_path)),
            "config_path": config.get("path"),
            "source_fingerprint": source.get("fingerprint"),
            "provisional": bool(profile.get("provisional")),
            "revalidation": dict(revalidation),
        }
        if profile.get("status") != "applied":
            profile_blockers = [
                issue for issue in profile_issues
                if issue["severity"] == "blocker"
            ]
            if profile_blockers:
                issues.extend(profile_blockers)
            else:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_profile_not_applied",
                    (
                        "apply the active speaker baseline before normal output "
                        "control or grouping"
                    ),
                ))
        if config.get("path") and not Path(str(config.get("path"))).exists():
            issues.append(_issue(
                "blocker",
                "active_baseline_config_missing",
                "active speaker baseline config file is missing",
            ))

    blocked = any(issue["severity"] == "blocker" for issue in issues)
    reason = issues[0]["code"] if issues else None
    detail = (
        issues[0]["message"]
        if issues
        else "active speaker baseline is applied and output control is ready"
    )
    return {
        "artifact_schema_version": 1,
        "kind": SETUP_STATUS_KIND,
        "active": True,
        "active_group_count": active_group_count,
        "status": "blocked" if blocked else "ready",
        "configured": not blocked,
        "volume_allowed": not blocked,
        "grouping_allowed": not blocked,
        "safety_muted": blocked,
        "reason": reason,
        "detail": detail,
        "active_config_path": config_path or None,
        "baseline_profile": profile_summary,
        "issues": issues,
    }
