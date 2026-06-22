# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from jasper.correction import bundle_tools, bundles, fir_runtime


def _write_fir(path: Path, coeffs: np.ndarray, sample_rate: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(str(path), sample_rate, coeffs.astype(np.float32))


def test_inspect_fir_wav_reports_latency_headroom_and_memory(tmp_path: Path):
    fir_path = tmp_path / "minimum.wav"
    coeffs = np.zeros(257, dtype=np.float32)
    coeffs[0] = 1.0
    _write_fir(fir_path, coeffs)

    report = fir_runtime.inspect_fir_wav(
        fir_path,
        mode="minimum_phase",
    )

    assert report["level"] == "ok"
    assert report["sample_rate"] == 48000
    assert report["tap_count"] == 257
    assert report["channel_count"] == 1
    assert report["filter_group_delay_ms"] == 0.0
    assert report["required_headroom_db"] == 0.0
    assert report["coefficient_memory_bytes"] == 257 * 4
    assert report["applied"] is False


def test_inspect_fir_wav_flags_sample_rate_mismatch(tmp_path: Path):
    fir_path = tmp_path / "bad-rate.wav"
    _write_fir(fir_path, np.array([1.0, 0.0, 0.0]), sample_rate=44100)

    report = fir_runtime.inspect_fir_wav(fir_path)

    assert report["level"] == "fail"
    assert {
        issue["code"]
        for issue in report["issues"]
    } == {"fir_sample_rate_mismatch"}


def test_inspect_fir_wav_rejects_over_budget_before_gain_analysis(
    tmp_path: Path,
):
    fir_path = tmp_path / "too-long.wav"
    coeffs = np.zeros(fir_runtime.MAX_USER_FIR_TAPS + 1, dtype=np.float32)
    coeffs[0] = 1.0
    _write_fir(fir_path, coeffs)

    report = fir_runtime.inspect_fir_wav(fir_path)

    assert report["level"] == "fail"
    assert report["max_frequency_gain_db"] is None
    assert report["required_headroom_db"] is None
    assert {
        issue["code"]
        for issue in report["issues"]
    } == {"fir_tap_count_high"}


def test_inspect_fir_wav_rejects_unsigned_pcm(tmp_path: Path):
    fir_path = tmp_path / "unsigned.wav"
    wavfile.write(str(fir_path), 48000, np.array([128, 129], dtype=np.uint8))

    with pytest.raises(fir_runtime.FirRuntimeError, match="unsigned integer"):
        fir_runtime.inspect_fir_wav(fir_path)


def test_stage_fir_artifact_records_manifest_entries(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    bundles.write_json_artifact(
        bundle_dir,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "abc",
            "state": "ready",
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="test",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    fir_path = tmp_path / "custom linear.wav"
    coeffs = np.zeros(1025, dtype=np.float32)
    coeffs[512] = 0.5
    _write_fir(fir_path, coeffs)

    report = fir_runtime.stage_fir_artifact(
        bundle_dir=bundle_dir,
        source_wav=fir_path,
        label="Custom Linear!",
        mode="linear_phase",
    )

    assert report["path"] == "fir/Custom_Linear.wav"
    assert report["filter_group_delay_ms"] > 0
    assert (bundle_dir / "fir" / "Custom_Linear.wav").exists()
    metadata = json.loads((bundle_dir / "fir" / "Custom_Linear.json").read_text())
    assert metadata["path"] == "fir/Custom_Linear.wav"

    manifest = bundles.read_artifact_manifest(bundle_dir)
    artifact_by_path = {
        artifact["path"]: artifact
        for artifact in manifest["artifacts"]
    }
    assert artifact_by_path["fir/Custom_Linear.wav"]["kind"] == "fir_coefficients"
    assert artifact_by_path["fir/Custom_Linear.json"]["dependencies"] == [
        "fir/Custom_Linear.wav",
        "info.json",
    ]
    readiness = bundle_tools.fir_readiness(bundle_dir)
    assert readiness["staged_fir_count"] == 1


def test_stage_fir_artifact_refuses_non_bundle(tmp_path: Path):
    fir_path = tmp_path / "coeff.wav"
    _write_fir(fir_path, np.array([1.0, 0.0, 0.0]))

    with pytest.raises(fir_runtime.FirRuntimeError, match="not a correction bundle"):
        fir_runtime.stage_fir_artifact(
            bundle_dir=tmp_path / "missing",
            source_wav=fir_path,
            label="x",
        )


def test_stage_fir_artifact_refuses_fail_level_fir_before_copy(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    bundles.write_json_artifact(
        bundle_dir,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "abc",
            "state": "ready",
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="test",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    fir_path = tmp_path / "bad-rate.wav"
    _write_fir(fir_path, np.array([1.0, 0.0, 0.0]), sample_rate=44100)

    with pytest.raises(fir_runtime.FirRuntimeError, match="refusing to stage"):
        fir_runtime.stage_fir_artifact(
            bundle_dir=bundle_dir,
            source_wav=fir_path,
            label="bad",
        )

    assert not (bundle_dir / "fir" / "bad.wav").exists()
