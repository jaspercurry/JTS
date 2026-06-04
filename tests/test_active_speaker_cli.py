from __future__ import annotations

import json
from pathlib import Path

from jasper.active_speaker import (
    HARDWARE_PROBE_EVIDENCE_SOURCE,
    OPERATOR_EVIDENCE_SOURCE,
    PATH_SAFETY_EVIDENCE_KIND,
    requirements_payload,
)
from jasper.cli.active_speaker import main


def _preset() -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "cli-test-v1",
        "name": "CLI test preset",
        "way_count": 2,
        "channel_map": {
            "layout": "mono",
            "outputs": [
                {
                    "index": 0,
                    "side": "mono",
                    "driver_role": "woofer",
                    "label": "mono woofer",
                    "startup_muted": True,
                },
                {
                    "index": 1,
                    "side": "mono",
                    "driver_role": "tweeter",
                    "label": "mono tweeter",
                    "startup_muted": True,
                },
            ],
        },
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "tweeter": {"manufacturer": "Example", "model": "Tweeter"},
        },
        "crossover_regions": [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": 1600,
            "target_type": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "delay_range_ms": [0.0, 0.5],
            "null_depth_threshold_db": 25,
        }],
        "safety": {
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    }


def _write_preset(path: Path) -> Path:
    path.write_text(json.dumps(_preset()), encoding="utf-8")
    return path


def _passing_path_evidence() -> dict:
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


def test_startup_template_cli_writes_yaml_without_check(tmp_path: Path, capsys):
    preset = _write_preset(tmp_path / "preset.json")
    out = tmp_path / "active.yml"

    code = main([
        "startup-template",
        str(preset),
        "--playback-device",
        "hw:ActiveDAC",
        "--output",
        str(out),
        "--baseline-id",
        "baseline-cli",
        "--no-check",
    ])

    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "baseline_id=baseline-cli" in text
    assert 'device: "hw:ActiveDAC"' in text
    assert "mute: true" in text
    assert "Validation: skipped" in capsys.readouterr().out


def test_startup_template_cli_reports_missing_validator(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    preset = _write_preset(tmp_path / "preset.json")
    out = tmp_path / "active.yml"
    monkeypatch.setattr("jasper.dsp_apply._camilladsp_binary", lambda: None)

    code = main([
        "startup-template",
        str(preset),
        "--playback-device",
        "hw:ActiveDAC",
        "--output",
        str(out),
    ])

    assert code == 0
    assert out.exists()
    printed = capsys.readouterr().out
    assert "Validation: missing" in printed
    assert "syntax preflight skipped" in printed


def test_startup_template_cli_json_includes_validation_status(
    tmp_path: Path,
    capsys,
):
    preset = _write_preset(tmp_path / "preset.json")
    out = tmp_path / "active.yml"

    code = main([
        "startup-template",
        str(preset),
        "--playback-device",
        "hw:ActiveDAC",
        "--output",
        str(out),
        "--no-check",
        "--json",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["preset_id"] == "cli-test-v1"
    assert payload["output"] == str(out)
    assert payload["validation"]["status"] == "skipped"


def test_startup_template_cli_invalid_preset_exits_2(tmp_path: Path):
    preset = tmp_path / "preset.json"
    preset.write_text("[]", encoding="utf-8")

    try:
        main([
            "startup-template",
            str(preset),
            "--playback-device",
            "hw:ActiveDAC",
            "--output",
            str(tmp_path / "active.yml"),
        ])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected parser exit for invalid preset")


def test_startup_template_cli_missing_output_parent_exits_2(tmp_path: Path):
    preset = _write_preset(tmp_path / "preset.json")

    try:
        main([
            "startup-template",
            str(preset),
            "--playback-device",
            "hw:ActiveDAC",
            "--output",
            str(tmp_path / "missing" / "active.yml"),
        ])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected parser exit for missing output parent")


def test_path_audit_cli_prints_requirements(capsys):
    code = main(["path-audit", "--requirements"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "Active speaker path-safety requirements" in printed
    assert "tts_cues" in printed


def test_path_audit_cli_blocks_incomplete_evidence(tmp_path: Path, capsys):
    evidence = _passing_path_evidence()
    del evidence["paths"]["rollback_configs"]
    evidence_path = tmp_path / "path_safety.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    code = main(["path-audit", str(evidence_path)])

    assert code == 1
    printed = capsys.readouterr().out
    assert "Path safety: blocked" in printed
    assert "rollback_configs" in printed


def test_path_audit_cli_json_passes_complete_evidence(tmp_path: Path, capsys):
    evidence_path = tmp_path / "path_safety.json"
    evidence_path.write_text(json.dumps(_passing_path_evidence()), encoding="utf-8")

    code = main(["path-audit", str(evidence_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["requirements_met"] is True
    assert payload["ok_to_load_active_config"] is False
    assert payload["load_gate"] == "hardware_probe_required"


def test_path_audit_cli_requires_evidence_or_requirements():
    try:
        main(["path-audit"])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected parser exit without evidence")


def test_path_probe_cli_writes_probe_backed_evidence(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    output = tmp_path / "path_safety.json"

    def fake_evidence(*args, **kwargs):
        paths = {}
        for requirement in requirements_payload()["requirements"]:
            paths[requirement["id"]] = {
                check: True for check in requirement["checks"]
            }
        return {
            "artifact_schema_version": 1,
            "kind": PATH_SAFETY_EVIDENCE_KIND,
            "evidence_source": HARDWARE_PROBE_EVIDENCE_SOURCE,
            "evidence_mode": "startup_load_preflight",
            "paths": paths,
        }

    monkeypatch.setattr("jasper.cli.active_speaker.load_output_topology", lambda path=None: object())
    monkeypatch.setattr("jasper.cli.active_speaker.load_staged_startup_config", lambda: {})
    monkeypatch.setattr("jasper.cli.active_speaker.load_calibration_level_state", lambda: {})
    monkeypatch.setattr(
        "jasper.cli.active_speaker.build_startup_load_path_safety_evidence",
        fake_evidence,
    )

    code = main([
        "path-probe",
        "--current-config",
        "/tmp/protected.yml",
        "--output",
        str(output),
        "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["evidence_path"] == str(output)
    assert payload["report"]["load_gate"] == "ready"
    assert json.loads(output.read_text(encoding="utf-8"))["evidence_source"] == (
        HARDWARE_PROBE_EVIDENCE_SOURCE
    )


def test_environment_probe_cli_json_reports_payload(monkeypatch, capsys):
    monkeypatch.setattr(
        "jasper.cli.active_speaker.probe_active_speaker_environment",
        lambda **kwargs: {
            "status": "blocked",
            "load_gate": "path_safety_evidence_missing",
            "ok_to_load_active_config": False,
            "camilla_config": {
                "classification": "jts_outputd_stereo",
                "path": "/etc/camilladsp/outputd-cutover.yml",
                "label": "JTS outputd stereo config",
                "playback_device": "outputd_content_playback",
                "playback_channels": 2,
                "volume_limit_db": 0.0,
            },
            "camilla_validation": {"status": "valid"},
            "alsa": {"available": True, "devices": [1]},
            "path_safety": {
                "status": "missing",
                "load_gate": "evidence_missing",
            },
            "issues": [],
        },
    )

    code = main(["environment-probe", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["camilla_config"]["classification"] == "jts_outputd_stereo"
