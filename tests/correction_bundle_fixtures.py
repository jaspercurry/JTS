# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

from jasper.correction import bundles, evidence


def _write_tiny_wav(path: Path, *, sample_rate: int = 48_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(b"\x00\x00" * 16)
    path.chmod(0o600)


def _record_file(
    bundle: Path,
    relative_path: str,
    *,
    kind: str,
    sensitivity: str,
    recomputable: bool,
    dependencies: list[str] | None = None,
    schema_version: int | None = None,
) -> None:
    bundles.record_artifact(
        bundle,
        relative_path,
        kind=kind,
        sensitivity=sensitivity,
        recomputable=recomputable,
        generated_by="tests.correction_bundle_fixtures",
        dependencies=dependencies or [],
        schema_version=schema_version,
    )


def write_golden_correction_bundle(
    sessions_dir: Path,
    session_id: str = "golden",
    *,
    started_at: float = 2_000.0,
    state: str = "ready",
) -> Path:
    """Write a compact, schema-rich correction bundle for regression tests.

    The fixture is deliberately synthetic, but it covers the contract
    surfaces future agent/FIR work depends on: primary metadata,
    correction result, manifest entries, runtime/acoustic evidence,
    replay artifacts, optional raw-private captures, and a buildable
    evidence packet.
    """
    bundle = sessions_dir / session_id
    bundle.mkdir(parents=True, exist_ok=False)

    freqs = [50.0, 80.0, 160.0, 320.0, 500.0]
    capture_quality: list[dict[str, Any]] = [
        {
            "capture_kind": "measurement",
            "position_index": 0,
            "artifact_path": "captures/p0.wav",
            "noise_artifact_path": "noise/p0_pre.wav",
            "rms_dbfs": -32.0,
            "peak_dbfs": -12.0,
            "noise_floor_dbfs": -70.0,
            "noise_floor_method": "noise_capture",
            "estimated_snr_db": 38.0,
            "issues": [],
            "replay_artifacts": {
                "artifact_schema_version": 1,
                "impulse_response_path": "analysis/p0_ir.wav",
                "response_path": "analysis/p0_response.json",
            },
        }
    ]
    verify_quality = {
        "capture_kind": "verify",
        "artifact_path": "verify.wav",
        "rms_dbfs": -34.0,
        "peak_dbfs": -14.0,
        "issues": [],
    }
    repeatability = {
        "available": True,
        "level": "high",
        "band_hz": [50.0, 350.0],
        "metrics": {"rms_db": 0.3, "p95_abs_db": 0.8, "max_abs_db": 1.1},
        "issues": [],
    }
    mic_calibration = {
        "provider": "miniDSP",
        "model_key": "umik_2",
        "serial_hash": "sha256:golden",
        "orientation": "0deg",
    }
    info = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session_id,
        "state": state,
        "started_at": started_at,
        "current_position": 1,
        "total_positions": 1,
        "target_choice": "flat",
        "strategy_choice": "balanced",
        "mic_calibration": mic_calibration,
        "input_device": {
            "device_id_hash": "golden-device",
            "actual_device_id_hash": "golden-device",
            "label": "USB Measurement Mic",
        },
        "browser_audio_report": {
            "capture_transport": "websocket_pcm",
            "echo_cancellation": False,
            "noise_suppression": False,
            "auto_gain_control": False,
        },
        "capture_quality": capture_quality,
        "verify_quality": verify_quality,
        "runtime_integrity": {"level": "ok", "issue_count": 0},
        "acoustic_quality": {
            "level": "ok",
            "snr_level": "high",
            "min_estimated_snr_db": 38.0,
        },
        "repeatability_report": repeatability,
    }
    result = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session_id,
        "measured": {"freqs_hz": freqs, "magnitude_db": [0.0, 1.0, 2.0, 1.0, 0.0]},
        "target": {"freqs_hz": freqs, "magnitude_db": [0.0, 0.0, 0.0, 0.0, 0.0]},
        "predicted": {
            "freqs_hz": freqs,
            "magnitude_db": [0.0, 0.4, 0.6, 0.3, 0.0],
        },
        "confidence_report": {
            "level": "high",
            "score": 88,
            "strategy_gates": {
                "safe": {"allowed": True, "reasons": []},
                "balanced": {"allowed": True, "reasons": []},
                "assertive": {"allowed": False, "reasons": ["needs repeat"]},
                "future_fir": {"allowed": False, "reasons": ["needs FIR review"]},
            },
        },
        "verify": {"available": True, "rms_error_db": 1.2},
    }
    runtime = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "artifact_schema_version": 1,
        "summary": {"level": "ok", "issue_count": 0},
        "issues": [],
        "snapshots": [
            {
                "timestamp": started_at,
                "load_average_1m": 0.12,
                "memory_available_bytes": 512 * 1024 * 1024,
            }
        ],
    }
    acoustic = {
        "artifact_schema_version": 1,
        "summary": {
            "level": "ok",
            "snr_level": "high",
            "min_estimated_snr_db": 38.0,
        },
        "issues": [],
        "captures": capture_quality,
        "repeatability": repeatability,
    }
    position_analysis = {
        "artifact_path": "position_analysis.json",
        "artifact_schema_version": 1,
        "position_count": 1,
        "freq_count": len(freqs),
        "variance": {"max_range_db": 0.0, "median_std_db": 0.0},
        "chart": {
            "freqs_hz": freqs,
            "min_db": [0.0, 1.0, 2.0, 1.0, 0.0],
            "max_db": [0.0, 1.0, 2.0, 1.0, 0.0],
            "std_db": [0.0, 0.0, 0.0, 0.0, 0.0],
            "range_db": [0.0, 0.0, 0.0, 0.0, 0.0],
        },
        "bands": [],
        "feature_flags": [],
    }
    replay_response = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "artifact_schema_version": 1,
        "session_id": session_id,
        "capture_kind": "measurement",
        "position_index": 0,
        "source_capture_path": "captures/p0.wav",
        "impulse_response_path": "analysis/p0_ir.wav",
        "sample_rate": 48_000,
        "deconvolution": {"ir_sample_count": 16},
        "direct_arrival": {"sample_index": 0, "pre_arrival_floor_db": -65.0},
        "analysis_curve": {
            "freqs_hz": freqs,
            "magnitude_db": [0.0, 1.0, 2.0, 1.0, 0.0],
            "calibration_applied": True,
            "normalization": "band_normalized_after_optional_mic_calibration",
            "normalized_band_hz": [50.0, 350.0],
        },
    }

    for rel_path in (
        "captures/p0.wav",
        "noise/p0_pre.wav",
        "repeat_captures/p0_r1.wav",
        "verify.wav",
        "analysis/p0_ir.wav",
    ):
        _write_tiny_wav(bundle / rel_path)

    calibration_text_path = bundle / "mic_calibration.txt"
    calibration_text_path.write_text("10 0.0\n1000 0.1\n")
    calibration_text_path.chmod(0o600)

    bundles.write_json_artifact(
        bundle,
        "info.json",
        info,
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.correction_bundle_fixtures",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        bundle,
        "result.json",
        result,
        kind="analysis_result",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="tests.correction_bundle_fixtures",
        dependencies=["info.json"],
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        bundle,
        "runtime_integrity.json",
        runtime,
        kind="runtime_integrity",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="tests.correction_bundle_fixtures",
        dependencies=["info.json"],
        schema_version=1,
    )
    bundles.write_json_artifact(
        bundle,
        "acoustic_quality.json",
        acoustic,
        kind="acoustic_quality",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="tests.correction_bundle_fixtures",
        dependencies=["info.json"],
        schema_version=1,
    )
    bundles.write_json_artifact(
        bundle,
        "position_analysis.json",
        position_analysis,
        kind="position_analysis",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="tests.correction_bundle_fixtures",
        dependencies=["info.json", "result.json"],
        schema_version=1,
    )
    bundles.write_json_artifact(
        bundle,
        "mic_calibration.json",
        {"artifact_schema_version": 1, **mic_calibration},
        kind="mic_calibration",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.correction_bundle_fixtures",
        dependencies=["info.json"],
        schema_version=1,
    )
    bundles.write_json_artifact(
        bundle,
        "analysis/p0_response.json",
        replay_response,
        kind="replay_response",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="tests.correction_bundle_fixtures",
        dependencies=["info.json", "captures/p0.wav"],
        schema_version=1,
    )

    _record_file(
        bundle,
        "mic_calibration.txt",
        kind="mic_calibration",
        sensitivity="private_metadata",
        recomputable=False,
        dependencies=["info.json"],
        schema_version=1,
    )
    for rel_path, kind in (
        ("captures/p0.wav", "capture_audio"),
        ("noise/p0_pre.wav", "noise_audio"),
        ("repeat_captures/p0_r1.wav", "repeat_audio"),
        ("verify.wav", "verify_audio"),
    ):
        _record_file(
            bundle,
            rel_path,
            kind=kind,
            sensitivity="private_audio",
            recomputable=False,
            dependencies=["info.json"],
        )
    _record_file(
        bundle,
        "analysis/p0_ir.wav",
        kind="replay_impulse_response",
        sensitivity="debug_safe",
        recomputable=True,
        dependencies=["captures/p0.wav", "analysis/p0_response.json"],
        schema_version=1,
    )

    packet = evidence.build_evidence_packet(bundle)
    bundles.write_json_artifact(
        bundle,
        "evidence_packet.json",
        packet,
        kind="evidence_packet",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="tests.correction_bundle_fixtures",
        dependencies=[
            "info.json",
            "result.json",
            "runtime_integrity.json",
            "acoustic_quality.json",
            "position_analysis.json",
        ],
        schema_version=evidence.SCHEMA_VERSION,
    )
    return bundle
