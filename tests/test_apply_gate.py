# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import copy
from dataclasses import replace

import pytest

from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate
from jasper.active_speaker.candidate_bank import find_banked_candidate
from jasper.active_speaker.crossover_v2.apply_gate import candidate_trial_manifest
from jasper.active_speaker.crossover_v2.apply_gate import ApplyGraph, apply_preconditions
from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for
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
    return ApplyGraph(profile, topology, measured), bank, manifest


@pytest.mark.parametrize("fault,code", [
    (None, None), ("unbanked", "not_found"), ("partial", "candidate_trial_required"),
    ("integrity", "candidate_trial_evidence_invalid"),
    ("layers", "active_applied_profile_snapshot_invalid"), ("graph", "candidate_trial_graph_mismatch"),
    ("digest", "candidate_trial_evidence_invalid"), ("second_take", "candidate_trial_evidence_invalid"),
])
def test_apply_preconditions_fail_independently(apply_facts, fault, code):
    candidate, bank, trial = apply_facts
    trial = copy.deepcopy(trial)
    if fault == "unbanked":
        bank = None
    elif fault == "partial":
        trial["status"] = "partial"
    elif fault == "integrity":
        trial["intact"] = False
    elif fault == "second_take":
        failed_take = copy.deepcopy(trial["set"]["takes"][0])
        failed_take["quality"]["status"] = "refused"
        trial["set"]["takes"].append(failed_take)
        from jasper.active_speaker.crossover_v2.apply_gate import trial_is_intact
        trial["intact"] = trial_is_intact(trial)
    elif fault == "layers":
        profile = copy.deepcopy(candidate.profile)
        profile["recomposition_snapshot"]["schema_version"] = 999
        candidate = replace(candidate, profile=profile)
    elif fault == "graph":
        profile = copy.deepcopy(candidate.profile)
        profile["config"]["sha256"] = "0" * 64
        candidate = replace(candidate, profile=profile)
    elif fault == "digest":
        trial["records"][0]["wav_sha256"] = "0" * 64
        from jasper.active_speaker.crossover_v2.apply_gate import trial_is_intact
        trial["intact"] = trial_is_intact(trial)
    issues = apply_preconditions(candidate, bank, trial, None)
    assert [issue.code for issue in issues] == ([code] if code else [])
    if issues:
        assert issues[0].next_action == (refusal_copy_for(code)[1] or refusal_copy_for("baseline_graph_safety_proof_failed")[1])
        assert issues[0].next_action


@pytest.mark.parametrize("tracking,expected", [([0., 99.], "failed"), ([0., None], "unavailable")])
@pytest.mark.parametrize("persisted", [False, True])
def test_multi_take_advice_survives_speaker_evidence_reuse(apply_facts, tracking, expected, persisted):
    from jasper.active_speaker.crossover_v2.apply_gate import verification_disclosure

    candidate, _, _ = apply_facts
    bank_trial(candidate.measured, candidate.profile, candidate.topology, record_fields=[
        {"verify_tracking": {"max_db_notch_excluded": value}} for value in tracking
    ])
    trial = candidate_trial_manifest(candidate.measured.fingerprint, candidate.profile)
    advice = verification_disclosure(candidate.measured, trial, None, profile=candidate.profile)
    assert advice["realization"] == expected
    assert len(advice["takes"]) == 2
    applied = {**candidate.profile, **({"trial_verification": advice} if persisted else {})}
    room = copy.deepcopy(candidate.profile)
    room["recomposition_snapshot"]["room_correction"] = {"changed": True}
    reused = verification_disclosure(candidate.measured, trial, applied, profile=room)
    assert reused["speaker_evidence_reused"] is persisted
    assert reused["realization"] == expected
    assert reused["layers_changed"] == ["room"]


def test_apply_reads_recorded_facts_without_reopening_audio(apply_facts, monkeypatch):
    from pathlib import Path
    from jasper.active_speaker.crossover_v2.apply_gate import verification_disclosure

    candidate, bank, _ = apply_facts
    bank_trial(candidate.measured, candidate.profile, candidate.topology,
               record_fields={"verify_tracking": {"max_db_notch_excluded": 0.}})
    reads = []
    original = Path.read_text
    def read(path, *args, **kwargs):
        assert path.suffix != ".wav"
        reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "read_bytes", lambda path: pytest.fail("audio reopened"))
    trial = candidate_trial_manifest(candidate.measured.fingerprint, candidate.profile)
    count = len(reads)
    assert apply_preconditions(candidate, bank, trial, None) == ()
    advice = verification_disclosure(candidate.measured, trial, None, profile=candidate.profile)
    assert advice["realization"] == "matched"
    assert len(reads) == count + 1  # The protected graph proof reads its YAML.


@pytest.mark.parametrize("legacy", [False, True])
def test_restore_uses_the_previous_profiles_proof(apply_facts, legacy):
    candidate, bank, _ = apply_facts
    applied = {**candidate.profile, "status": "applied"}
    if not legacy:
        applied["trial_verification"] = {"capture_validity": "usable", "realization": "matched"}
    assert apply_preconditions(candidate, bank, None, applied) == ()
    applied["source"] = {**applied["source"], "measured_candidate_fingerprint": "another"}
    assert apply_preconditions(candidate, bank, None, applied)[0].code == "candidate_trial_required"


@pytest.mark.parametrize("failure", [ValueError, RuntimeError])
def test_openability_refuses_by_code(apply_facts, failure):
    candidate, bank, trial = apply_facts
    def unavailable():
        raise failure()
    issues = apply_preconditions(replace(candidate, openability=unavailable), bank, trial, None)
    assert [issue.code for issue in issues] == ["crossover_v2_stage2_preflight_refused"]
    assert issues[0].next_action["id"] == "speaker_setup"


def test_fresh_failure_is_not_replaced_by_prior_speaker_advice(apply_facts):
    from jasper.active_speaker.crossover_v2.apply_gate import verification_disclosure

    candidate, _, _ = apply_facts
    bank_trial(candidate.measured, candidate.profile, candidate.topology,
               record_fields={"diagnostic": {"max_db_notch_excluded": 99., "integrity_failed": ""}})
    trial = candidate_trial_manifest(candidate.measured.fingerprint, candidate.profile)
    prior = {**candidate.profile, "trial_verification": {
        "speaker_evidence": {"capture_validity": "usable", "realization": "matched"}}}
    profile = copy.deepcopy(candidate.profile)
    profile["recomposition_snapshot"]["room_correction"] = {"changed": True}
    advice = verification_disclosure(candidate.measured, trial, prior, profile=profile)
    assert advice["realization"] == advice["takes"][0]["realization"] == "failed"
    assert advice["speaker_evidence"]["realization"] == "matched"


def test_banked_analysis_supplies_all_four_dimensions_without_audio(apply_facts, monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from jasper.active_speaker.crossover_v2.apply_gate import verification_disclosure
    from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline, measured_response_from_analysis
    from jasper.active_speaker.crossover_v2.spatial import analysis_curve_records
    from jasper.audio_measurement.program import build_verify_program
    from jasper.web.correction_crossover_v2 import _capture_evidence_blocks
    from tests.crossover_v2_fixtures import _verify_analysis

    candidate, _, _ = apply_facts
    program = build_verify_program(1600.)
    analysis = _verify_analysis(program, max_db=99.)
    frequencies = np.geomspace(20., 20000., 4096)
    levels = np.sin(np.log(frequencies))
    analysis = replace(analysis, summed_response=replace(analysis.summed_response,
                       freqs_hz=frequencies, magnitude_db=levels, complex_tf=(10 ** (levels / 20)).astype(complex)))
    curves = analysis_curve_records(analysis, program)
    before = measured_response_from_analysis(SimpleNamespace(program_id=program.program_id,
        summed_response=SimpleNamespace(**{**curves[0], "magnitude_db": [2 * db for db in curves[0]["magnitude_db"]]})),
        reference_mark="1")
    baseline = EntryBaseline.from_measurement(before, graph_fingerprint="a" * 16, captured_at="2026-09-12")
    bank_trial(candidate.measured, candidate.profile, candidate.topology, record_fields={
        **_capture_evidence_blocks(None, analysis), "program": program.to_dict(),
        "curves": curves, "entry_baseline": baseline.to_dict(),
    })
    monkeypatch.setattr("jasper.audio_measurement.program_analysis.analyze_program_capture",
                        lambda *a, **k: pytest.fail("capture analyzed during apply"))
    trial = candidate_trial_manifest(candidate.measured.fingerprint, candidate.profile)
    advice = verification_disclosure(candidate.measured, trial, None, profile=candidate.profile)
    assert {key: advice[key] for key in ("capture_validity", "realization", "benefit", "spec")} == {
        "capture_validity": "usable", "realization": "failed", "benefit": "improved", "spec": "passed"}
