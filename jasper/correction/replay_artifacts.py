# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Replay-grade derived artifacts for correction bundles.

Raw capture WAVs remain the canonical evidence. These helpers write
small, recomputable artifacts next to them so operators, future FIR
tools, and the calibration-agent evidence packet can inspect impulse
and response facts without re-running deconvolution for every report.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import interop

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReplayArtifactSet:
    """Relative paths for one capture's derived replay artifacts."""

    impulse_response_path: str
    response_path: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_schema_version": self.schema_version,
            "impulse_response_path": self.impulse_response_path,
            "response_path": self.response_path,
        }


def capture_stem(
    *,
    capture_kind: str,
    position_index: int | None,
) -> str:
    """Stable bundle artifact stem for a capture kind/position."""
    if capture_kind == "measurement" and position_index is not None:
        return f"p{position_index}"
    if capture_kind == "repeat":
        idx = 0 if position_index is None else position_index
        return f"repeat_p{idx}"
    if capture_kind == "verify":
        return "verify"
    if position_index is not None:
        return f"{capture_kind}_p{position_index}"
    return capture_kind


def _round_list(values: np.ndarray, digits: int) -> list[float]:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"expected 1-D array, got shape {arr.shape}")
    return [round(float(v), digits) for v in arr]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
        tmp_name = f.name
    os.replace(tmp_name, path)
    path.chmod(0o600)


def write_capture_replay_artifacts(
    bundle_dir: Path,
    *,
    bundle_schema_version: int,
    session_id: str,
    capture_kind: str,
    position_index: int | None,
    source_capture_path: str | None,
    ir: np.ndarray,
    sample_rate: int,
    raw_freqs_hz: np.ndarray,
    raw_magnitude_db: np.ndarray,
    smoothed_magnitude_db: np.ndarray,
    log_freqs_hz: np.ndarray,
    log_magnitude_db: np.ndarray,
    direct_arrival: dict[str, Any],
    deconvolution: dict[str, Any],
    calibration_applied: bool,
    normalized_band_hz: tuple[float, float],
) -> ReplayArtifactSet:
    """Write IR WAV + response JSON for one successful capture analysis."""
    stem = capture_stem(
        capture_kind=capture_kind,
        position_index=position_index,
    )
    analysis_dir = bundle_dir / "analysis"
    ir_rel = f"analysis/{stem}_ir.wav"
    response_rel = f"analysis/{stem}_response.json"

    interop.write_impulse_response_wav(
        bundle_dir / ir_rel,
        np.asarray(ir, dtype=np.float32),
        sample_rate=sample_rate,
        normalize=False,
    )
    (bundle_dir / ir_rel).chmod(0o600)

    payload = {
        "bundle_schema_version": int(bundle_schema_version),
        "artifact_schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "capture_kind": capture_kind,
        "position_index": position_index,
        "source_capture_path": source_capture_path,
        "impulse_response_path": ir_rel,
        "sample_rate": int(sample_rate),
        "deconvolution": {
            **deconvolution,
            "ir_sample_count": int(np.asarray(ir).shape[0]),
        },
        "direct_arrival": direct_arrival,
        "frequency_response": {
            "freqs_hz": _round_list(raw_freqs_hz, 6),
            "magnitude_db": _round_list(raw_magnitude_db, 6),
            "smoothed_1_48_octave_db": _round_list(smoothed_magnitude_db, 6),
            "normalization": "raw_deconvolution_amplitude",
        },
        "analysis_curve": {
            "freqs_hz": _round_list(log_freqs_hz, 6),
            "magnitude_db": _round_list(log_magnitude_db, 6),
            "calibration_applied": bool(calibration_applied),
            "normalization": "band_normalized_after_optional_mic_calibration",
            "normalized_band_hz": [
                float(normalized_band_hz[0]),
                float(normalized_band_hz[1]),
            ],
        },
    }
    _atomic_write_json(analysis_dir / f"{stem}_response.json", payload)
    return ReplayArtifactSet(
        impulse_response_path=ir_rel,
        response_path=response_rel,
    )
