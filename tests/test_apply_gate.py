# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import copy
from dataclasses import replace

import pytest

from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate
from jasper.active_speaker.candidate_bank import find_banked_candidate
from jasper.active_speaker.crossover_v2.apply_evidence import candidate_trial_manifest
from jasper.active_speaker.crossover_v2.apply_gate import ApplyGraph, apply_preconditions
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.crossover_preview import build_crossover_preview
from tests.active_speaker_fixtures import valid_camilla_config
from tests.apply_fixtures import bank_trial
from tests.test_active_speaker_baseline_profile import _draft, _dual_apple_topology, _MEASURE_EVIDENCE
from tests.test_active_speaker_measured_crossover_candidate import _candidate


@pytest.fixture
def apply_facts(tmp_path, monkeypatch):
    from jasper.active_speaker.baseline_profile import compile_preset_from_crossover_preview

    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: tmp_path / "sessions")
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    preset, _, _ = compile_preset_from_crossover_preview(topology, preview)
    measured = replace(_candidate(), source_preset=preset, analysis=_MEASURE_EVIDENCE)
    profile = build_baseline_profile_candidate(
        topology, design_draft=draft, crossover_preview=preview, measurements={},
        measured_candidate=measured, tuning_owner="automatic", write=True,
        state_path=tmp_path / "profile.json", config_path=tmp_path / "graph.yml", validate=valid_camilla_config,
    )
    assert profile["permissions"]["may_apply"], profile["issues"]
    bank_trial(measured, profile, topology)
    bank = find_banked_candidate(measured.fingerprint)
    manifest = candidate_trial_manifest(measured.fingerprint, profile)
    assert manifest
    return ApplyGraph(profile, topology, measured, measured.fingerprint), bank, manifest


@pytest.mark.parametrize("fault,code", [
    (None, None), ("unbanked", "not_found"), ("partial", "candidate_trial_required"),
    ("integrity", "candidate_trial_evidence_invalid"), ("identity", "candidate_fingerprint_mismatch"),
    ("layers", "baseline_graph_safety_proof_failed"), ("graph", "candidate_trial_graph_mismatch"),
    ("wav", "candidate_trial_evidence_invalid"),
])
def test_apply_preconditions_fail_independently(apply_facts, fault, code):
    candidate, bank, trial = apply_facts
    trial = copy.deepcopy(trial)
    if fault == "unbanked":
        bank = None
    elif fault == "partial":
        trial["status"] = "partial"
    elif fault == "integrity":
        trial["set"]["takes"][0]["quality"]["status"] = "refused"
    elif fault == "identity":
        candidate = replace(candidate, expected_fingerprint="0" * 64)
    elif fault == "layers":
        profile = copy.deepcopy(candidate.profile)
        profile["recomposition_snapshot"]["schema_version"] = 999
        candidate = replace(candidate, profile=profile)
    elif fault == "graph":
        profile = copy.deepcopy(candidate.profile)
        profile["config"]["sha256"] = "0" * 64
        candidate = replace(candidate, profile=profile)
    elif fault == "wav":
        from pathlib import Path
        (Path(trial["bundle"]) / "capture.wav").write_bytes(b"changed")
    issues = apply_preconditions(candidate, bank, trial, None)
    assert [issue.code for issue in issues] == ([code] if code else [])
    if issues:
        assert issues[0].next_action == REASON_REGISTRY[code].next_action
        assert issues[0].next_action
