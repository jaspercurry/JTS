from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jasper.calibration_agent import tools
from jasper.correction import bundles

from .correction_bundle_fixtures import write_golden_correction_bundle


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    assert isinstance(data, dict)
    return data


def _write_artifact(
    bundle: Path,
    rel_path: str,
    payload: dict[str, Any],
    *,
    kind: str,
    sensitivity: str = "debug_safe",
    schema_version: int = 1,
) -> None:
    bundles.write_json_artifact(
        bundle,
        rel_path,
        payload,
        kind=kind,
        sensitivity=sensitivity,
        recomputable=True,
        generated_by="tests.test_calibration_agent_advisor_context",
        dependencies=["info.json"] if rel_path != "info.json" else [],
        schema_version=schema_version,
    )


def _update_info(bundle: Path, updates: dict[str, Any]) -> None:
    info = _read_json(bundle / "info.json")
    for key, value in updates.items():
        if value is None:
            info.pop(key, None)
        else:
            info[key] = value
    _write_artifact(
        bundle,
        "info.json",
        info,
        kind="session_metadata",
        sensitivity="private_metadata",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )


def _update_result(bundle: Path, updates: dict[str, Any]) -> None:
    result = _read_json(bundle / "result.json")
    result.update(updates)
    _write_artifact(
        bundle,
        "result.json",
        result,
        kind="analysis_result",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )


def _context(
    bundle: Path,
    *,
    corpus_dir: Path | None = None,
    sound_profile_path: Path | None = None,
) -> dict[str, Any]:
    loaded = tools.load_measurement_bundle(bundle_dir=bundle)
    intake = tools.build_intake(
        loaded,
        corpus_dir=corpus_dir,
        sound_profile_path=sound_profile_path,
    )
    return intake["advisor_context"]


def _sound_profile(path: Path) -> None:
    path.write_text(json.dumps({
        "enabled": True,
        "curve_id": "harman",
        "simple_eq": {"bass_db": 2.0, "mid_db": 0.0, "treble_db": -1.0},
        "parametric_bands": [{
            "enabled": True,
            "type": "Peaking",
            "freq_hz": 1400.0,
            "gain_db": -1.5,
            "q": 1.2,
        }],
        "profile_id": "custom_aaaaaaaaaaaa",
        "profile_name": "Private living-room profile",
    }))


def test_advisor_context_is_redacted_and_permissioned(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)
    sound_profile_path = tmp_path / "sound_profile.json"
    _sound_profile(sound_profile_path)
    corpus_dir = tmp_path / "docs" / "calibration-agent"
    (corpus_dir / "concepts").mkdir(parents=True)
    (corpus_dir / "concepts" / "measurement-quality.md").write_text(
        "# Measurement Quality\n\nUse calibrated microphones.\n"
    )
    _update_info(bundle, {
        "mic_calibration": {
            "provider": "manual_upload",
            "model_key": "umik_2",
            "serial": "810-8494",
            "serial_hash": "sha256:redacted",
            "file_sha256": "abc123",
        },
        "input_device": {
            "label": "Private USB Mic Name",
            "deviceId": "browser-device-id",
            "actual_device_id_hash": "hash-a",
            "sample_rate": 48000,
            "channel_count": 1,
        },
    })

    context = _context(
        bundle,
        corpus_dir=corpus_dir,
        sound_profile_path=sound_profile_path,
    )
    encoded = json.dumps(context, sort_keys=True)

    assert context["kind"] == "llm_ready_advisor_context"
    assert context["artifact_schema_version"] == 1
    assert context["privacy"]["raw_audio_excluded"] is True
    assert context["bundle"]["artifact_manifest"]["private_audio_count"] == 4
    assert context["advisor_policy"]["mode"] == "read_only_first_bounded_actions"
    prohibited = {
        action["id"]
        for action in context["advisor_policy"]["prohibited_actions"]
    }
    assert {
        "read_raw_audio",
        "emit_camilladsp_yaml",
        "apply_filters",
        "generate_fir_taps",
    } <= prohibited
    assert any(
        action["id"] == "propose_preference_eq_audition" and action["allowed"]
        for action in context["advisor_policy"]["allowed_actions"]
    )
    assert context["advisor_policy"]["execution_boundary"]["model_may_execute"] is False
    assert context["measurement"]["mic_calibration"]["raw_serial_redacted"] is True
    assert context["measurement"]["input_device"]["browser_labels_redacted"] is True
    assert context["preference"]["current_sound_profile"]["curve_id"] == "harman"
    assert (
        context["preference"]["current_sound_profile"]["profile_identity"][
            "profile_name_redacted"
        ]
        is True
    )
    assert context["corpus"]["hits"][0]["path"] == (
        "docs/calibration-agent/concepts/measurement-quality.md"
    )
    assert str(tmp_path) not in encoded
    assert "810-8494" not in encoded
    assert "Private USB Mic Name" not in encoded
    assert "Private living-room profile" not in encoded


def test_advisor_context_redacts_nested_confidence_browser_audio(
    tmp_path: Path,
):
    bundle = write_golden_correction_bundle(tmp_path)
    result = _read_json(bundle / "result.json")
    result["confidence_report"]["browser_audio_report"] = {
        "level": "ok",
        "input_device": {
            "label": "Private Nested Browser Label",
            "deviceId": "nested-device-id",
        },
    }
    _write_artifact(
        bundle,
        "result.json",
        result,
        kind="analysis_result",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )

    context = _context(bundle)
    encoded = json.dumps(context, sort_keys=True)

    assert context["quality"]["confidence"]["level"] == "high"
    assert "browser_audio_report" not in context["quality"]["confidence"]
    assert "Private Nested Browser Label" not in encoded
    assert "nested-device-id" not in encoded


def test_advisor_context_uses_stable_manifest_error_reason(
    tmp_path: Path,
):
    bundle = tmp_path / "missing-manifest"
    bundle.mkdir()
    (bundle / "info.json").write_text(json.dumps({
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": "missing-manifest",
        "state": "failed",
        "current_position": 0,
        "total_positions": 1,
    }))

    context = _context(bundle)
    manifest = context["bundle"]["artifact_manifest"]
    encoded = json.dumps(context, sort_keys=True)

    assert manifest["available"] is False
    assert manifest["reason"] == "artifact_manifest_unavailable"
    assert str(tmp_path) not in encoded


def test_advisor_fixture_matrix_good_peq_but_not_fir(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)

    context = _context(bundle)

    permissions = context["advisor_policy"]["capability_permissions"]
    assert context["agent_readiness"]["level"] == "ready"
    assert permissions["safe_peq"]["allowed"] is True
    assert permissions["balanced_peq"]["allowed"] is True
    assert permissions["future_fir"]["allowed"] is False
    assert context["correction"]["target_curve"]["curve_summary"]["available"] is True


def test_advisor_fixture_matrix_low_snr(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)
    acoustic = _read_json(bundle / "acoustic_quality.json")
    acoustic["summary"]["level"] = "warn"
    acoustic["summary"]["snr_level"] = "low"
    acoustic["summary"]["min_estimated_snr_db"] = 9.0
    acoustic["issues"] = [{
        "code": "snr_low",
        "severity": "warn",
        "message": "estimated SNR is low",
    }]
    _write_artifact(bundle, "acoustic_quality.json", acoustic, kind="acoustic_quality")

    context = _context(bundle)

    assert context["quality"]["acoustic_summary"]["snr_level"] == "low"
    assert context["agent_readiness"]["level"] == "caution"
    assert "SNR evidence is weak or missing" in context["agent_readiness"]["reasons"]


def test_advisor_fixture_matrix_high_spatial_variance(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)
    position = _read_json(bundle / "position_analysis.json")
    position["position_count"] = 3
    position["bands"] = [{
        "band_id": "bass",
        "label": "Bass",
        "confidence_level": "low",
        "p90_std_db": 4.5,
        "max_range_db": 12.0,
    }]
    position["feature_flags"] = [{
        "decision": "avoid_aggressive_correction",
        "reason": "high seat-to-seat variance around 80 Hz",
    }]
    _write_artifact(bundle, "position_analysis.json", position, kind="position_analysis")

    context = _context(bundle)

    spatial = context["quality"]["spatial_spread"]
    assert spatial["position_count"] == 3
    assert spatial["bands"][0]["confidence_level"] == "low"
    assert context["correction"]["rejected_or_caution_features"][0][
        "decision"
    ] == "avoid_aggressive_correction"


def test_advisor_fixture_matrix_missing_mic_calibration(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)
    _update_info(bundle, {"mic_calibration": None})

    context = _context(bundle)

    assert context["measurement"]["mic_calibration"]["present"] is False
    assert "mic_calibration_missing" in {
        item["code"] for item in context["evidence_gaps"]
    }


def test_advisor_fixture_matrix_runtime_warning(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)
    runtime = _read_json(bundle / "runtime_integrity.json")
    runtime["summary"] = {"level": "warn", "issue_count": 1}
    runtime["issues"] = [{
        "code": "load_high",
        "severity": "warn",
        "message": "load was high during sweep",
    }]
    _write_artifact(bundle, "runtime_integrity.json", runtime, kind="runtime_integrity")

    context = _context(bundle)

    assert context["quality"]["runtime_summary"]["level"] == "warn"
    assert context["agent_readiness"]["level"] == "caution"
    assert "runtime integrity has warnings" in context["agent_readiness"]["reasons"]


def test_advisor_fixture_matrix_deep_null_rejected(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)
    result = _read_json(bundle / "result.json")
    result["measured"]["magnitude_db"] = [0.0, 1.0, -9.0, 1.0, 0.0]
    _write_artifact(
        bundle,
        "result.json",
        result,
        kind="analysis_result",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )

    context = _context(bundle)

    nulls = context["correction"]["bass_residual"]["nulls"]
    assert nulls[0]["freq_hz"] == 160.0
    assert nulls[0]["residual_db"] == -9.0


def test_advisor_fixture_matrix_incomplete_bundle(tmp_path: Path):
    bundle = tmp_path / "incomplete"
    bundle.mkdir()
    _write_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "incomplete",
            "state": "failed",
            "current_position": 0,
            "total_positions": 3,
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )

    context = _context(bundle)

    assert context["bundle"]["has_result"] is False
    assert context["agent_readiness"]["level"] == "caution"
    assert "session_failed" in {
        issue["code"] for issue in context["bundle"]["validation_issues"]
    }
    assert "result_json_missing" in {
        item["code"] for item in context["evidence_gaps"]
    }
