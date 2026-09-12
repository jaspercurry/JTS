# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Admission for an exact trial graph (ADR-0301)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jasper.output_topology import OutputTopology
from jasper.atomic_io import atomic_write_text
from ..candidate_bank import BankedCandidate, CandidateBankRefusal, find_banked_candidate
from ..candidate_trials import require_candidate_trial
from ..crossover_contract import crossover_snapshot_state
from ..measured_crossover_candidate import MeasuredCrossoverCandidate
from .. import runtime_contract
from .refusal_copy import REASON_REGISTRY


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str

    @property
    def next_action(self) -> Mapping[str, Any]:
        return REASON_REGISTRY[self.code].next_action or {}

    def to_dict(self) -> dict[str, Any]:
        return {"severity": "blocker", "code": self.code, "message": self.detail,
                "next_action": dict(self.next_action)}


@dataclass(frozen=True)
class ApplyGraph:
    profile: Mapping[str, Any]
    topology: OutputTopology
    measured: MeasuredCrossoverCandidate | None = None
    expected_fingerprint: str = ""
    driver_domain: bool = False


def apply_preconditions(
    candidate: ApplyGraph, bank: BankedCandidate | None,
    manifest: Mapping[str, Any] | None, applied: Mapping[str, Any] | None,
) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    if candidate.measured is not None:
        if bank is None:
            issues.append(Issue("not_found", "Select a banked candidate."))
        elif bank.fingerprint != candidate.expected_fingerprint:
            issues.append(Issue("candidate_fingerprint_mismatch", "The banked identity differs from the requested identity."))
        else:
            try:
                require_candidate_trial(candidate.measured, manifest=manifest)
            except CandidateBankRefusal as exc:
                issues.append(Issue(exc.code, exc.detail))
        if manifest is not None:
            from .apply_evidence import trial_is_intact  # lazy: record reader imports the measurement engine

            graph = str((candidate.profile.get("config") or {}).get("sha256") or "")[:16]
            if (manifest["set"]["capture_basis"].get("submitted_graph_fingerprint") != graph):
                issues.append(Issue("candidate_trial_graph_mismatch", "The trial and compiled graph digests differ."))
            if not trial_is_intact(manifest):
                issues.append(Issue("candidate_trial_evidence_invalid", "The trial set has incomplete or damaged evidence."))
    profile, topology = candidate.profile, candidate.topology
    snapshot = crossover_snapshot_state(
        profile, expected_topology_id=topology.topology_id,
        expected_topology_fingerprint=str((profile.get("source") or {}).get("topology_fingerprint") or ""),
        topology=topology, expected_domain="driver" if candidate.driver_domain else "full",
        require_applied=False,
    )
    if not snapshot["valid"]:
        issues.append(Issue("baseline_graph_safety_proof_failed", str(snapshot["reason"])))
    elif not candidate.driver_domain:
        try:
            graph_text = profile.get("_compiled_graph_text") or Path(str((profile.get("config") or {}).get("path") or "")).read_text(encoding="utf-8")
            proof = runtime_contract.classify_bass_extension_graph(
                topology, evidence_source="desired", graph_text=graph_text,
                applied_baseline_state=profile,
            )
            if not proof.allowed or proof.classification != runtime_contract.GRAPH_APPROVED_ACTIVE_RUNTIME:
                issues.append(Issue("baseline_graph_safety_proof_failed", proof.classification))
        except (OSError, UnicodeError) as exc:
            issues.append(Issue("baseline_graph_safety_proof_failed", type(exc).__name__))
    return tuple(issues)


def check_baseline_apply(
    profile: dict[str, Any], topology: OutputTopology, measured: MeasuredCrossoverCandidate | None,
    state_path: Path, *, driver_domain: bool = False,
) -> None:
    from .apply_evidence import candidate_trial_manifest, verification_disclosure  # lazy: measurement evidence

    bank = None
    manifest = None
    if measured is not None:
        try:
            bank = find_banked_candidate(measured.fingerprint)
        except CandidateBankRefusal:
            pass
        manifest = candidate_trial_manifest(measured.fingerprint, profile)
    issues = apply_preconditions(
        ApplyGraph(profile, topology, measured, measured.fingerprint if measured else "", driver_domain),
        bank, manifest, profile.get("applied_recomposition_profile"),
    )
    if issues:
        profile["status"] = "compiled_apply_blocked"
        profile["permissions"]["may_apply"] = False
        profile["issues"] = [*profile.get("issues", []), *(issue.to_dict() for issue in issues)]
        atomic_write_text(state_path, json.dumps(profile, indent=2, sort_keys=True) + "\n", mode=0o640)
    elif measured is not None and manifest is not None:
        profile["trial_verification"] = verification_disclosure(
            measured, manifest, profile.get("applied_recomposition_profile"), profile=profile,
        )
