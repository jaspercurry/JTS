from __future__ import annotations

import pytest

from jasper.active_speaker import (
    PATH_SAFETY_EVIDENCE_KIND,
    ActiveSpeakerConfigError,
    evaluate_path_safety_evidence,
    requirements_payload,
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
    assert report["ok_to_load_active_config"] is True
    assert report["blocker_count"] == 0


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
