from __future__ import annotations

import json
from pathlib import Path

from jasper.active_speaker import PATH_SAFETY_EVIDENCE_KIND, requirements_payload
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
    assert payload["ok_to_load_active_config"] is True


def test_path_audit_cli_requires_evidence_or_requirements():
    try:
        main(["path-audit"])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected parser exit without evidence")
