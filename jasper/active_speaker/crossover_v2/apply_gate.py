# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Admission for an exact trial graph (ADR-0301)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from jasper.output_topology import OutputTopology
from jasper.atomic_io import atomic_write_text
from ..candidate_bank import BankedCandidate, CandidateBankRefusal, find_banked_candidate
from ..candidate_trials import require_candidate_trial
from ..crossover_contract import crossover_snapshot_state
from ..measured_crossover_candidate import MeasuredCrossoverCandidate
from .. import runtime_contract
from .refusal_copy import refusal_copy_for
from ..commissioning_evidence_store import EVIDENCE_ROOT


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str

    @property
    def next_action(self) -> Mapping[str, Any]:
        return refusal_copy_for(self.code)[1] or refusal_copy_for("baseline_graph_safety_proof_failed")[1] or {}

    def to_dict(self) -> dict[str, Any]:
        return {"severity": "blocker", "code": self.code, "message": self.detail,
                "next_action": dict(self.next_action)}


@dataclass(frozen=True)
class ApplyGraph:
    profile: Mapping[str, Any]
    topology: OutputTopology
    measured: MeasuredCrossoverCandidate | None = None
    driver_domain: bool = False
    openability: Callable[[], Any] | None = None


def apply_preconditions(
    candidate: ApplyGraph, bank: BankedCandidate | None,
    manifest: Mapping[str, Any] | None, applied: Mapping[str, Any] | None,
) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    if candidate.measured is not None:
        if bank is None:
            issues.append(Issue("not_found", "Select a banked candidate."))
        elif previously_applied(candidate.measured.fingerprint, applied):
            if str(((applied or {}).get("config") or {}).get("sha256") or "")[:16] != str((candidate.profile.get("config") or {}).get("sha256") or "")[:16]:
                issues.append(Issue("candidate_trial_graph_mismatch", "The displaced graph does not match the compiled graph."))
        else:
            try:
                require_candidate_trial(candidate.measured, manifest=manifest)
            except CandidateBankRefusal as exc:
                issues.append(Issue(exc.code, exc.detail))
        if manifest is not None and not previously_applied(candidate.measured.fingerprint, applied):
            graph = str((candidate.profile.get("config") or {}).get("sha256") or "")[:16]
            if (manifest["set"]["capture_basis"].get("submitted_graph_fingerprint") != graph):
                issues.append(Issue("candidate_trial_graph_mismatch", "The trial and compiled graph digests differ."))
            if manifest.get("intact") is not True:
                issues.append(Issue("candidate_trial_evidence_invalid", "The trial set has incomplete or damaged evidence."))
    profile, topology = candidate.profile, candidate.topology
    snapshot = crossover_snapshot_state(
        profile, expected_topology_id=topology.topology_id,
        expected_topology_fingerprint=str((profile.get("source") or {}).get("topology_fingerprint") or ""),
        topology=topology, expected_domain="driver" if candidate.driver_domain else "full",
        require_applied=False,
    )
    if not snapshot["valid"]:
        issues.append(Issue(str(snapshot["reason"]), str(snapshot["detail"])))
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
    if candidate.openability is not None:
        try:
            candidate.openability()
        except Exception as exc:  # noqa: BLE001 — stage 2 must resolve before any write
            issues.append(Issue("crossover_v2_stage2_preflight_refused", str(exc)))
    return tuple(issues)


def previously_applied(fingerprint: str, applied: Mapping[str, Any] | None) -> bool:
    return bool(applied and applied.get("status") == "applied"
                and (applied.get("source") or {}).get("measured_candidate_fingerprint") == fingerprint
                and ((applied or {}).get("config") or {}).get("sha256"))


def prepare_trial(measured: MeasuredCrossoverCandidate | None, profile: Mapping[str, Any] | None = None,
                  *, restored: Mapping[str, Any] | None = None, bank: BankedCandidate | None = None) -> dict[str, Any]:
    if measured is None:
        return {}
    if bank is None:
        try:
            bank = find_banked_candidate(measured.fingerprint)
        except CandidateBankRefusal:
            pass
    return {"bank": bank, "manifest": None if previously_applied(measured.fingerprint, restored)
            else candidate_trial_manifest(measured.fingerprint, profile), "restored": restored}


def check_baseline_apply(
    profile: dict[str, Any], topology: OutputTopology, measured: MeasuredCrossoverCandidate | None,
    state_path: Path, *, trial_evidence: Mapping[str, Any], driver_domain: bool = False,
) -> None:
    manifest, restored = trial_evidence.get("manifest"), trial_evidence.get("restored")
    issues = apply_preconditions(
        ApplyGraph(profile, topology, measured, driver_domain), trial_evidence.get("bank"), manifest, restored,
    )
    if issues:
        profile["status"] = "compiled_apply_blocked"
        profile["permissions"]["may_apply"] = False
        profile["issues"] = [*profile.get("issues", []), *(issue.to_dict() for issue in issues)]
        atomic_write_text(state_path, json.dumps(profile, indent=2, sort_keys=True) + "\n", mode=0o640)
    elif restored:
        profile["trial_verification"] = restored.get("trial_verification")
    elif measured is not None and manifest is not None:
        profile["trial_verification"] = verification_disclosure(
            measured, manifest, profile.get("applied_recomposition_profile"), profile=profile,
        )


def candidate_trial_manifest(fingerprint: str, profile: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    from .round_inputs import iter_round_sessions, round_artifact_dir  # lazy: round inputs import bank readers
    from ..run_manifest import RUN_MANIFEST_FILENAME, TAKE_MEASURED  # lazy: the baseline truth layer must not import the run engine

    graph = str(((profile or {}).get("config") or {}).get("sha256") or "")[:16]
    best: tuple[bool, bool, float, dict[str, Any]] | None = None
    for bundle in iter_round_sessions():
        directory, _ = round_artifact_dir(bundle)
        if directory is None:
            continue
        path = directory / RUN_MANIFEST_FILENAME
        try:
            document = json.loads(path.read_text())
            for group in document.get("sets", []):
                basis = group.get("capture_basis") or {}
                if basis.get("candidate_id") == fingerprint and basis.get("role") == "summed":
                    trial = {**document, "set": group, "bundle": str(bundle), "manifest_path": str(path)}
                    rank = (document.get("status") == "complete" and basis.get("submitted_graph_fingerprint") == graph,
                            all(take.get("quality", {}).get("status") == TAKE_MEASURED for take in group.get("takes", [])), path.stat().st_mtime, trial)
                    if best is None or rank[:-1] > best[:-1]:
                        best = rank
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if best is None:
        return None
    trial = best[-1]
    records = []
    for take in trial["set"].get("takes", []):
        try:
            path = Path(trial["bundle"]) / EVIDENCE_ROOT / "artifacts" / take["artifacts"]["record_id"]
            record = json.loads(path.read_text())
            records.append(record if isinstance(record, dict) else {})
        except (OSError, ValueError, TypeError, KeyError):
            records.append({})
    trial["records"] = records
    trial["intact"] = trial_is_intact(trial)
    trial["verification"] = _trial_verification(trial) if trial["intact"] else {}
    return trial


def trial_is_intact(trial: Mapping[str, Any]) -> bool:
    from ..run_manifest import TAKE_MEASURED  # lazy: the baseline truth layer must not import the run engine

    try:
        basis, takes, records = trial["set"]["capture_basis"], trial["set"]["takes"], trial["records"]
        return bool(takes) and len(takes) == len(records) and all(
            take["quality"]["status"] == TAKE_MEASURED
            and record.get("candidate_id") == basis["candidate_id"]
            and record.get("graph_scope") == "candidate"
            and record.get("graph_fingerprint") == basis["submitted_graph_fingerprint"]
            and len(str(take["artifacts"].get("wav_sha256") or "")) == 64
            and record.get("wav_sha256") == take["artifacts"]["wav_sha256"]
            for take, record in zip(takes, records)
        )
    except (TypeError, AttributeError, KeyError):
        return False


def changed_layers(profile: Mapping[str, Any], applied: Mapping[str, Any] | None) -> list[str]:
    now = profile.get("recomposition_snapshot") or {}
    before = (applied or {}).get("recomposition_snapshot") or {}
    speaker = ("preset", "corrections", "linearization", "blend_correction")
    return [name for name, keys in (("speaker", speaker), ("room", ("room_correction",)),
                                    ("bass", ("bass_extension",)))
            if not before or any(now.get(key) != before.get(key) for key in keys)]


def _trial_verification(trial: Mapping[str, Any]) -> dict[str, Any]:
    import numpy as np  # lazy: analysis import cost, paid before the writer lock
    from types import SimpleNamespace
    from jasper.audio_measurement.program_analysis import (CaptureIntegrity, IntegrityCheck, INTEGRITY_FAIL,
                                                          INTEGRITY_NOT_EVALUATED)  # lazy: numpy import cost
    from ..flat_spec import FlatSpecReport, evaluate_flat_spec  # lazy: numpy import cost
    from .contracts import VERIFY_TOLERANCE_DB, BenefitStatus, CaptureValidity, RealizationStatus, SpecStatus
    from .round_evidence import (MEASURED_BENEFIT_MARGIN_DB, EntryBaseline, benefit_comparands,
                                measured_response_from_analysis)  # lazy: round evidence imports the measurement engine
    from .verification import (Verdict, evaluate_benefit, evaluate_capture_validity, evaluate_realization,
                               evaluate_spec, verification_result)

    takes = []
    for take, record in zip(trial["set"]["takes"], trial["records"]):
        raw_integrity = record.get("capture_integrity") or {}
        diagnostic = record.get("diagnostic") or {}
        post = None
        try:
            integrity = CaptureIntegrity(checks=tuple(IntegrityCheck(**check) for check in raw_integrity.get("checks", ()))) if raw_integrity else None
            if "integrity_failed" in diagnostic:
                integrity = CaptureIntegrity(checks=tuple(
                    IntegrityCheck(name, status) for key, status in (("integrity_failed", INTEGRITY_FAIL),
                                                                    ("integrity_not_evaluated", INTEGRITY_NOT_EVALUATED))
                    for name in diagnostic.get(key, "").split(",") if name))
        except (ValueError, TypeError, KeyError, AttributeError):
            integrity = None
        tracking = record.get("verify_tracking") or diagnostic
        realization = evaluate_realization(tracking=tracking if isinstance(tracking, Mapping) else None,
                                           tolerance_db=VERIFY_TOLERANCE_DB)
        try:
            raw_spec = record.get("spec_report") or {}
            report = FlatSpecReport.from_dict(raw_spec) if raw_spec.get("bands") else None
            summed = next((curve for curve in record.get("curves", []) if curve.get("role") == "summed"), None)
            if summed:
                analysis: Any = SimpleNamespace(
                    program_id=(record.get("program") or {}).get("program_id") or record.get("program_id"),
                    summed_response=SimpleNamespace(**summed))
                post = measured_response_from_analysis(analysis, reference_mark=str(take.get("pose_index", "")))
                if post is not None:
                    report = evaluate_flat_spec(np.asarray(post.curve.hz), np.asarray(post.curve.db),
                                                exclusion_mask=np.asarray(post.excluded))
        except (ValueError, TypeError, KeyError, AttributeError):
            report, post = None, None
        capture = evaluate_capture_validity(integrity)
        spec = evaluate_spec(report)
        baseline = EntryBaseline.from_dict(record.get("entry_baseline"))
        before, after = benefit_comparands(baseline=baseline.as_measurement() if baseline else None, post=post)
        benefit = evaluate_benefit(entry_baseline=before, post=after, margin_db=MEASURED_BENEFIT_MARGIN_DB)
        result = verification_result(capture=capture, realization=realization, benefit=benefit, spec=spec)
        takes.append({"take_id": take["take_id"], **result.to_dict()})
    def set_verdict(key, priority):
        return Verdict(next(status for status in priority if any(row[key] == status.value for row in takes)),
                       "trial_set", {"takes": [row["take_id"] for row in takes]})

    dimensions = verification_result(
        capture=set_verdict("capture_validity", (CaptureValidity.UNUSABLE, CaptureValidity.USABLE)),
        realization=set_verdict("realization", (RealizationStatus.FAILED, RealizationStatus.UNAVAILABLE, RealizationStatus.MATCHED)),
        benefit=set_verdict("benefit", (BenefitStatus.REGRESSED, BenefitStatus.INDETERMINATE, BenefitStatus.IMPROVED)),
        spec=set_verdict("spec", (SpecStatus.FAILED, SpecStatus.UNEVALUABLE, SpecStatus.PASSED)),
    ).to_dict()
    return {**dimensions, "takes": takes}


def verification_disclosure(
    candidate: MeasuredCrossoverCandidate, trial: Mapping[str, Any],
    applied: Mapping[str, Any] | None, *, profile: Mapping[str, Any],
) -> dict[str, Any]:
    layers = changed_layers(profile, applied)
    previous = (applied or {}).get("trial_verification") or {}
    reuse = bool(layers) and "speaker" not in layers and bool(previous.get("speaker_evidence"))
    dimensions = trial["verification"]
    speaker_evidence = previous["speaker_evidence"] if reuse else {
        **{key: dimensions[key] for key in ("capture_validity", "realization")},
        "candidate_fingerprint": candidate.fingerprint, "manifest_path": trial["manifest_path"],
        "set_id": trial["set"]["set_id"],
    }
    return {**dimensions, "candidate_fingerprint": candidate.fingerprint,
            "manifest_path": trial["manifest_path"], "set_id": trial["set"]["set_id"],
            "graph_fingerprint": trial["set"]["capture_basis"]["submitted_graph_fingerprint"],
            "layers_changed": layers, "speaker_evidence_reused": reuse,
            "speaker_evidence": speaker_evidence}
