from __future__ import annotations

import json
from pathlib import Path

from jasper.calibration_agent import cli, tools
from jasper.correction import bundles


def _write_bundle(root: Path, session_id: str = "abc") -> Path:
    bundle = root / session_id
    bundle.mkdir(parents=True)
    info = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session_id,
        "state": "ready",
        "started_at": 1000,
        "updated_at": 1001,
        "target_choice": "flat",
        "total_positions": 1,
        "current_position": 1,
        "input_device": {"label": "USB measurement mic"},
        "mic_calibration": {"provider": "manual_upload", "model": "other"},
        "capture_quality": [{
            "capture_kind": "measurement",
            "position_index": 0,
            "artifact_path": "captures/p0.wav",
            "issues": [{
                "code": "browser_echo_cancellation",
                "severity": "warn",
                "message": "browser reported echo cancellation enabled",
            }],
        }],
    }
    result = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session_id,
        "measured": {
            "freqs_hz": [20, 40, 80, 160, 320],
            "magnitude_db": [0, 2, 8, -8, 1],
        },
        "target": {
            "freqs_hz": [20, 40, 80, 160, 320],
            "magnitude_db": [0, 0, 0, 0, 0],
        },
        "predicted": None,
        "verify": None,
        "verify_metrics": None,
        "peqs": [{"freq_hz": 80, "q": 4, "gain_db": -6}],
    }
    (bundle / "info.json").write_text(json.dumps(info))
    (bundle / "result.json").write_text(json.dumps(result))
    return bundle


def _write_corpus(root: Path) -> Path:
    corpus = root / "corpus"
    concepts = corpus / "concepts"
    concepts.mkdir(parents=True)
    (concepts / "measurement-quality.md").write_text(
        "# Measurement Quality\n\n"
        "Use calibrated microphones, avoid clipping, and verify repeatability.\n",
    )
    (concepts / "room-correction-limits.md").write_text(
        "# Room Correction Limits\n\n"
        "Narrow nulls are not usually fixed by EQ.\n",
    )
    return corpus


def test_build_intake_summarizes_quality_and_bass_residual(tmp_path: Path):
    bundle_dir = _write_bundle(tmp_path / "sessions")
    corpus_dir = _write_corpus(tmp_path)

    bundle = tools.load_measurement_bundle(bundle_dir=bundle_dir)
    intake = tools.build_intake(bundle, corpus_dir=corpus_dir)

    summary = intake["summary"]
    assert summary["session_id"] == "abc"
    assert summary["mic_calibrated"] is True
    assert summary["quality_issues"][0]["artifact_path"] == "captures/p0.wav"
    assert intake["peaks_nulls"]["peaks"][0]["freq_hz"] == 80.0
    assert intake["peaks_nulls"]["nulls"][0]["freq_hz"] == 160.0
    assert [hit["title"] for hit in intake["corpus_hits"]] == [
        "Measurement Quality",
        "Room Correction Limits",
    ]
    assert intake["side_effects"] == []


def test_cli_json_loads_latest_bundle(tmp_path: Path, capsys):
    sessions = tmp_path / "sessions"
    _write_bundle(sessions, "old")
    latest = _write_bundle(sessions, "new")
    info = json.loads((latest / "info.json").read_text())
    info["started_at"] = 2000
    (latest / "info.json").write_text(json.dumps(info))

    rc = cli.main([
        "--sessions-dir",
        str(sessions),
        "--corpus-dir",
        str(_write_corpus(tmp_path)),
        "--json",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["session_id"] == "new"
    assert out["summary"]["peq_count"] == 1


def test_cli_returns_2_for_missing_bundle(tmp_path: Path, capsys):
    rc = cli.main([
        "missing",
        "--sessions-dir",
        str(tmp_path / "sessions"),
    ])

    assert rc == 2
    assert "bundle not found" in capsys.readouterr().err
