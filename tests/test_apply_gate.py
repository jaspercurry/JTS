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
    ("wav", "candidate_trial_evidence_invalid"), ("second_take", "candidate_trial_evidence_invalid"),
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
    elif fault == "second_take":
        failed_take = copy.deepcopy(trial["set"]["takes"][0])
        failed_take["quality"]["status"] = "refused"
        trial["set"]["takes"].append(failed_take)
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


@pytest.mark.parametrize("tracking,expected", [([0., 99.], "failed"), ([0., None], "unavailable")])
def test_multi_take_advice_survives_speaker_evidence_reuse(apply_facts, tracking, expected):
    from jasper.active_speaker.crossover_v2.apply_evidence import verification_disclosure

    candidate, _, _ = apply_facts
    bank_trial(candidate.measured, candidate.profile, candidate.topology, record_fields=[
        {"verify_tracking": {"max_db_notch_excluded": value}} for value in tracking
    ])
    trial = candidate_trial_manifest(candidate.measured.fingerprint, candidate.profile)
    advice = verification_disclosure(candidate.measured, trial, None, profile=candidate.profile)
    assert advice["realization"] == expected
    assert len(advice["takes"]) == 2
    applied = {**candidate.profile, "trial_verification": advice}
    room = copy.deepcopy(candidate.profile)
    room["recomposition_snapshot"]["room_correction"] = {"changed": True}
    reused = verification_disclosure(candidate.measured, trial, applied, profile=room)
    assert reused["speaker_evidence_reused"]
    assert reused["realization"] == expected
    assert reused["layers_changed"] == ["room"]


def test_an_intact_trial_remains_usable_after_a_damaged_repeat(apply_facts):
    from pathlib import Path

    candidate, bank, original = apply_facts
    bundle, _ = bank_trial(candidate.measured, candidate.profile, candidate.topology)
    (bundle / "capture.wav").write_bytes(b"damaged")
    selected = candidate_trial_manifest(candidate.measured.fingerprint, candidate.profile)
    assert Path(selected["bundle"]) == Path(original["bundle"])
    assert apply_preconditions(candidate, bank, selected, None) == ()


def test_trial_wav_is_graded_at_apply_time(apply_facts):
    import io
    import numpy as np
    from scipy.io import wavfile
    from jasper.audio_measurement.program import build_verify_program, render_program_pcm
    from jasper.active_speaker.crossover_v2.apply_evidence import verification_disclosure

    candidate, _, _ = apply_facts
    program = build_verify_program(1600., sweep_s=0.6)
    samples = np.concatenate((np.zeros(800), render_program_pcm(program)[:, 0], np.zeros(5000)))
    buffer = io.BytesIO()
    wavfile.write(buffer, program.sample_rate_hz, samples.astype(np.float32))
    bank_trial(candidate.measured, candidate.profile, candidate.topology,
               record_fields={"program": program.to_dict()}, wav_bytes=buffer.getvalue())
    trial = candidate_trial_manifest(candidate.measured.fingerprint, candidate.profile)
    advice = verification_disclosure(candidate.measured, trial, None, profile=candidate.profile)
    assert advice["takes"][0]["analysis_error"] is None
    assert advice["capture_validity"] == "usable"
    assert advice["spec"] == "passed"
