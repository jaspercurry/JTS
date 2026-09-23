# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Output permission from the saved topology and applied speaker profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from jasper.output_topology import OutputTopology, topology_config_fingerprint

from ._common import BASELINE_TOPOLOGY_CHANGED
from .output_contract import (
    CONTRACT_UNCONFIGURED,
    classify_output_contract,
    topology_allows_flat_dac_graph,
)

IN_SEQUENCE_CAPTURE_ANCHOR_REASON = "active_speaker_commissioning_config_loaded"
_STAGED_CONFIG_BASENAMES = {
    "active_speaker_staged_startup.yml",
    "active_speaker_commissioning.yml",
}


def active_group_count(topology: OutputTopology) -> int:
    return sum(
        group.mode in {"active_2_way", "active_3_way"}
        for group in topology.speaker_groups
    )


def readiness_snapshot(
    topology: OutputTopology | None,
    *,
    applied_profile: Mapping[str, Any] | None,
    active_config_path: str | None,
    read_error: str | None = None,
) -> dict[str, Any]:
    """Derive permissions without compiling a candidate or caching file checks."""
    issues: list[dict[str, str]] = []
    groups = active_group_count(topology) if topology is not None else None
    protected = None

    def issue(code: str, message: str, severity: str = "blocker") -> None:
        issues.append({"severity": severity, "code": code, "message": message})

    if topology is None:
        issue(
            "output_topology_unreadable",
            f"output topology cannot be read safely: {read_error}",
        )
    elif groups == 0:
        contract = classify_output_contract(topology)
        if not topology_allows_flat_dac_graph(contract):
            unconfigured = contract.classification == CONTRACT_UNCONFIGURED
            issue(
                "output_topology_unconfigured"
                if unconfigured
                else "output_topology_not_ready",
                "choose and save a speaker layout before using audio"
                if unconfigured
                else "choose and save a complete passive mono or stereo layout before using audio",
            )
            issues.extend(dict(item) for item in contract.issues)
    else:
        if not active_config_path:
            issue(
                "active_config_path_unknown",
                "current CamillaDSP config path is unavailable",
            )
        elif Path(active_config_path).name in _STAGED_CONFIG_BASENAMES:
            issue(
                IN_SEQUENCE_CAPTURE_ANCHOR_REASON,
                "active speaker setup/commissioning graph is loaded",
            )
        profile = applied_profile or {}
        source = profile.get("source") or {}
        config_path = str((profile.get("config") or {}).get("path") or "")
        exists = bool(config_path and Path(config_path).exists())
        ready = profile.get("status") == "applied" and exists
        fingerprint = source.get("topology_fingerprint")
        current = not fingerprint or fingerprint == topology_config_fingerprint(
            topology
        )
        protected = {
            "available": applied_profile is not None,
            "status": "ready" if ready else "unavailable",
            "config_path": config_path or None,
            "source_fingerprint": source.get("fingerprint"),
            "candidate_fingerprint": profile.get("candidate_fingerprint"),
            "topology_current": current,
            "provisional": bool(profile.get("provisional")),
            "role": "applied_profile",
        }
        if read_error is not None:
            issue(
                "active_baseline_profile_unreadable",
                f"applied speaker profile cannot be read: {read_error}",
            )
        elif applied_profile is not None and not exists:
            issue(
                "active_baseline_config_missing",
                "applied active speaker baseline config file is missing",
            )
        elif not ready:
            issue(
                "active_baseline_profile_not_applied",
                "apply the active speaker baseline before normal output control or grouping",
            )
        elif not current:
            # Topology changes disclose staleness without parking (ADR-0019).
            issue(
                BASELINE_TOPOLOGY_CHANGED,
                "topology changed since the applied baseline; re-mint when convenient",
                "warning",
            )

    blockers = [item for item in issues if item["severity"] == "blocker"]
    blocked = bool(blockers)
    headline = next(iter(blockers or issues), None)
    status = "unknown" if topology is None else "blocked" if blocked else "ready"
    if not blocked and groups == 0:
        status = "not_active"
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_setup_status",
        "active": groups > 0 if groups is not None else None,
        "active_group_count": groups,
        "status": status,
        "configured": not blocked,
        "volume_allowed": not blocked,
        "grouping_allowed": not blocked,
        "commissioning": None,
        "safety_muted": blocked,
        "reason": headline["code"] if headline else None,
        "detail": headline["message"]
        if headline
        else (
            "speaker does not use an active crossover"
            if groups == 0
            else "active speaker baseline is applied and output control is ready"
        ),
        "active_config_path": active_config_path or None,
        "baseline_profile": None,
        "protected_profile": protected,
        "issues": issues,
    }
