from __future__ import annotations

from pathlib import Path

import pytest

from jasper.active_speaker import (
    HARDWARE_PROBE_EVIDENCE_SOURCE,
    OPERATOR_EVIDENCE_SOURCE,
    PATH_SAFETY_EVIDENCE_KIND,
    ActiveSpeakerConfigError,
    build_startup_load_path_safety_evidence,
    evaluate_path_safety_evidence,
    requirements_payload,
    write_path_safety_evidence,
)
from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.path_safety import _startup_muted_by_candidate
from jasper.active_speaker.staging import stage_protected_startup_config
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology() -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono cabinet",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })


def _valid_config(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(
        status=ValidationStatus.VALID,
        path=str(path),
    )


def _staged(tmp_path: Path) -> dict:
    return stage_protected_startup_config(
        _topology(),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )


def _passing_evidence() -> dict:
    paths = {}
    for requirement in requirements_payload()["requirements"]:
        paths[requirement["id"]] = {
            check: True for check in requirement["checks"]
        }
    return {
        "artifact_schema_version": 1,
        "kind": PATH_SAFETY_EVIDENCE_KIND,
        "evidence_source": OPERATOR_EVIDENCE_SOURCE,
        "paths": paths,
    }


def test_requirements_payload_lists_required_paths():
    payload = requirements_payload()

    ids = {item["id"] for item in payload["requirements"]}
    assert "music_renderers" in ids
    assert "tts_cues" in ids
    assert "correction_sweeps" in ids
    assert "rollback_configs" in ids
    assert payload["kind"] == "jts_active_speaker_path_safety_requirements"


def test_path_safety_evidence_passes_when_every_required_check_is_true():
    report = evaluate_path_safety_evidence(_passing_evidence())

    assert report["status"] == "pass"
    assert report["requirements_met"] is True
    assert report["hardware_probe_backed"] is False
    assert report["ok_to_load_active_config"] is False
    assert report["load_gate"] == "hardware_probe_required"
    assert report["blocker_count"] == 0


def test_path_safety_allows_load_only_for_probe_backed_passing_evidence():
    evidence = _passing_evidence()
    evidence["evidence_source"] = HARDWARE_PROBE_EVIDENCE_SOURCE

    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "pass"
    assert report["requirements_met"] is True
    assert report["hardware_probe_backed"] is True
    assert report["ok_to_load_active_config"] is True
    assert report["load_gate"] == "ready"


def test_path_safety_blocks_missing_required_path():
    evidence = _passing_evidence()
    del evidence["paths"]["tts_cues"]

    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "blocked"
    assert report["ok_to_load_active_config"] is False
    assert any(issue["code"] == "missing_path_evidence" for issue in report["issues"])


def test_path_safety_blocks_false_required_check():
    evidence = _passing_evidence()
    evidence["paths"]["correction_sweeps"]["level_controlled"] = False

    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "blocked"
    assert any(
        issue["code"] == "level_controlled_not_verified"
        for issue in report["issues"]
    )


def test_path_safety_blocks_missing_required_check_without_schema_error():
    evidence = _passing_evidence()
    del evidence["paths"]["test_tones"]["level_controlled"]

    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "blocked"
    assert any(
        issue["code"] == "level_controlled_missing"
        for issue in report["issues"]
    )


def test_path_safety_rejects_unknown_evidence_source():
    evidence = _passing_evidence()
    evidence["evidence_source"] = "trust_me"

    with pytest.raises(ActiveSpeakerConfigError, match="evidence source"):
        evaluate_path_safety_evidence(evidence)


def test_path_safety_requires_evidence_source():
    evidence = _passing_evidence()
    del evidence["evidence_source"]

    with pytest.raises(ActiveSpeakerConfigError, match="evidence source is required"):
        evaluate_path_safety_evidence(evidence)


def test_path_safety_rejects_non_boolean_check():
    evidence = _passing_evidence()
    evidence["paths"]["music_renderers"]["route_verified"] = "yes"

    with pytest.raises(ActiveSpeakerConfigError, match="must be boolean"):
        evaluate_path_safety_evidence(evidence)


def test_path_safety_rejects_missing_schema_metadata():
    evidence = _passing_evidence()
    del evidence["artifact_schema_version"]

    with pytest.raises(ActiveSpeakerConfigError, match="schema version"):
        evaluate_path_safety_evidence(evidence)

    evidence = _passing_evidence()
    del evidence["kind"]

    with pytest.raises(ActiveSpeakerConfigError, match="kind"):
        evaluate_path_safety_evidence(evidence)


def test_startup_load_path_probe_passes_with_protected_rollback(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    evidence = build_startup_load_path_safety_evidence(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=staged["config"]["path"],
        generated_at="2026-06-04T12:00:00Z",
    )

    report = evaluate_path_safety_evidence(evidence)

    assert evidence["evidence_source"] == HARDWARE_PROBE_EVIDENCE_SOURCE
    assert evidence["evidence_mode"] == "startup_load_preflight"
    assert evidence["scope"] == "load_only_no_audio"
    assert report["ok_to_load_active_config"] is True
    assert report["load_gate"] == "ready"


def test_startup_load_path_probe_allows_bounded_normal_rollback_target(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    prior = tmp_path / "prior_stereo.yml"
    prior.write_text(
        "# Source: jasper.sound.camilla_yaml.emit_sound_config\n"
        "devices:\n"
        "  volume_limit: 0\n"
        "  playback:\n"
        "    type: Alsa\n"
        "    device: outputd_content_playback\n"
        "    channels: 2\n",
        encoding="utf-8",
    )

    evidence = build_startup_load_path_safety_evidence(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=prior,
    )
    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "pass"
    assert report["ok_to_load_active_config"] is True
    rollback = evidence["paths"]["rollback_configs"]
    assert rollback["rollback_target_available"] is True
    assert rollback["rollback_target_restore_limited"] is True
    assert rollback["rollback_target_protected"] is False
    assert any(
        issue["code"] == "rollback_target_restores_previous_profile"
        for issue in evidence["observed_issues"]
    )


def test_write_path_safety_evidence_persists_probe_payload(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    evidence = build_startup_load_path_safety_evidence(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=staged["config"]["path"],
    )

    path = write_path_safety_evidence(evidence, path=tmp_path / "path_safety.json")

    assert path.read_text(encoding="utf-8").startswith("{\n")


def test_startup_muted_prefers_fully_muted_gate_over_text_scan(tmp_path: Path) -> None:
    # _startup_muted_by_candidate prefers the precise staged_candidate_fully_muted
    # gate (every per-output mute -120 dB AND wired) over the coarse text scan.
    payload = _staged(tmp_path)
    assert payload["status"] == "staged"
    gate = next(
        g for g in payload["required_gates"]
        if g["id"] == "staged_candidate_fully_muted"
    )
    assert gate["passed"] is True
    assert _startup_muted_by_candidate(payload) is True

    # If the gate reports NOT fully muted, the precise gate must win even though
    # the on-disk config (and the loose text scan) still shows other muted
    # outputs — the looseness the text fallback could not catch.
    for g in payload["required_gates"]:
        if g["id"] == "staged_candidate_fully_muted":
            g["passed"] = False
    assert _startup_muted_by_candidate(payload) is False
