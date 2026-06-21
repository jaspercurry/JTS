# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""FIR coefficient runtime substrate.

This module does not design room-correction FIR filters. It provides
the boring, reviewable pieces needed before generation is safe:
coefficient inspection, latency/headroom accounting, and bundle-local
storage for imported FIR WAVs.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal

import numpy as np

from . import bundles

SCHEMA_VERSION = 1
DEFAULT_TARGET_SAMPLE_RATE = 48000
MAX_USER_FIR_TAPS = 65536
WARN_FILTER_GROUP_DELAY_MS = 100.0
WARN_REQUIRED_HEADROOM_DB = 6.0
_LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]+")

FirMode = Literal["minimum_phase", "linear_phase", "mixed_phase", "unknown"]


class FirRuntimeError(ValueError):
    """A FIR artifact is missing or unsafe to stage."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_label(label: str) -> str:
    cleaned = _LABEL_RE.sub("_", label.strip()).strip("._-")
    return cleaned[:64] or "fir"


def _to_float64(data: np.ndarray) -> np.ndarray:
    arr = np.asarray(data)
    if arr.ndim == 1:
        arr = arr[:, None]
    if arr.ndim != 2:
        raise FirRuntimeError(f"FIR WAV must be mono/stereo, got shape {arr.shape}")
    if np.issubdtype(arr.dtype, np.floating):
        out = arr.astype(np.float64)
    elif np.issubdtype(arr.dtype, np.unsignedinteger):
        raise FirRuntimeError(
            "unsigned integer FIR WAVs are ambiguous coefficient data; "
            "export float32 or signed PCM"
        )
    elif np.issubdtype(arr.dtype, np.integer):
        max_abs = max(abs(np.iinfo(arr.dtype).min), np.iinfo(arr.dtype).max)
        out = arr.astype(np.float64) / float(max_abs)
    else:
        raise FirRuntimeError(f"unsupported FIR WAV dtype: {arr.dtype}")
    if not np.all(np.isfinite(out)):
        raise FirRuntimeError("FIR WAV contains non-finite samples")
    return out


def _max_frequency_gain_db(coeffs: np.ndarray) -> float:
    if coeffs.size == 0:
        return 0.0
    n_fft = max(8192, 1 << (max(coeffs.shape[0], 1) - 1).bit_length())
    response = np.fft.rfft(coeffs, n=n_fft, axis=0)
    max_gain = float(np.max(np.abs(response))) if response.size else 0.0
    return round(float(20.0 * np.log10(max(max_gain, 1e-12))), 3)


def _filter_group_delay_ms(
    *,
    tap_count: int,
    sample_rate: int,
    mode: FirMode,
) -> float | None:
    if mode == "minimum_phase":
        return 0.0
    if mode in {"linear_phase", "mixed_phase", "unknown"}:
        return round(((tap_count - 1) / 2.0) / sample_rate * 1000.0, 3)
    return None


def inspect_fir_wav(
    path: Path,
    *,
    mode: FirMode = "unknown",
    target_sample_rate: int = DEFAULT_TARGET_SAMPLE_RATE,
) -> dict[str, Any]:
    """Inspect a FIR coefficient WAV without applying it."""
    from scipy.io import wavfile

    source = Path(path)
    if not source.exists():
        raise FirRuntimeError(f"FIR WAV does not exist: {source}")
    sample_rate, data = wavfile.read(str(source), mmap=True)
    sample_rate = int(sample_rate)
    if sample_rate <= 0:
        raise FirRuntimeError(f"invalid FIR sample rate: {sample_rate}")
    shape = data.shape
    if len(shape) == 1:
        tap_count = int(shape[0])
        channel_count = 1
    elif len(shape) == 2:
        tap_count = int(shape[0])
        channel_count = int(shape[1])
    else:
        raise FirRuntimeError(f"FIR WAV must be mono/stereo, got shape {shape}")
    if tap_count <= 0:
        raise FirRuntimeError("FIR WAV has no taps")
    if channel_count not in {1, 2}:
        raise FirRuntimeError(
            f"FIR WAV must be mono or stereo, got {channel_count} channels"
        )
    if np.issubdtype(np.asarray(data).dtype, np.unsignedinteger):
        raise FirRuntimeError(
            "unsigned integer FIR WAVs are ambiguous coefficient data; "
            "export float32 or signed PCM"
        )

    group_delay_ms = _filter_group_delay_ms(
        tap_count=tap_count,
        sample_rate=sample_rate,
        mode=mode,
    )
    span_ms = round(tap_count / sample_rate * 1000.0, 3)
    coefficient_memory_bytes = tap_count * channel_count * 4
    byte_size = source.stat().st_size
    sha256 = _sha256_file(source)

    early_issues: list[dict[str, Any]] = []
    if sample_rate != target_sample_rate:
        early_issues.append({
            "code": "fir_sample_rate_mismatch",
            "severity": "fail",
            "message": (
                f"FIR sample rate {sample_rate} Hz does not match "
                f"target {target_sample_rate} Hz"
            ),
        })
    if tap_count > MAX_USER_FIR_TAPS:
        early_issues.append({
            "code": "fir_tap_count_high",
            "severity": "fail",
            "message": f"FIR tap count {tap_count} exceeds first-pass JTS budget",
        })
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "path": str(source),
            "sha256": sha256,
            "byte_size": byte_size,
            "mode": mode,
            "sample_rate": sample_rate,
            "target_sample_rate": int(target_sample_rate),
            "tap_count": tap_count,
            "channel_count": channel_count,
            "span_ms": span_ms,
            "filter_group_delay_ms": group_delay_ms,
            "coefficient_memory_bytes": coefficient_memory_bytes,
            "peak_sample": None,
            "max_frequency_gain_db": None,
            "required_headroom_db": None,
            "issues": early_issues,
            "level": "fail",
            "applied": False,
        }

    coeffs = _to_float64(data)
    peak = float(np.max(np.abs(coeffs))) if coeffs.size else 0.0
    if peak <= 0.0:
        raise FirRuntimeError("FIR WAV is silent")

    max_gain_db = _max_frequency_gain_db(coeffs)
    required_headroom_db = round(max(0.0, max_gain_db), 3)

    issues: list[dict[str, Any]] = list(early_issues)
    if (
        group_delay_ms is not None
        and group_delay_ms > WARN_FILTER_GROUP_DELAY_MS
    ):
        issues.append({
            "code": "fir_latency_high",
            "severity": "warn",
            "message": "FIR filter group delay is high for interactive audio",
            "details": {"filter_group_delay_ms": group_delay_ms},
        })
    if required_headroom_db > WARN_REQUIRED_HEADROOM_DB:
        issues.append({
            "code": "fir_headroom_high",
            "severity": "warn",
            "message": "FIR frequency response needs substantial preamp headroom",
            "details": {"required_headroom_db": required_headroom_db},
        })

    level = "ok"
    if any(issue["severity"] == "fail" for issue in issues):
        level = "fail"
    elif issues:
        level = "warn"

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "path": str(source),
        "sha256": sha256,
        "byte_size": byte_size,
        "mode": mode,
        "sample_rate": sample_rate,
        "target_sample_rate": int(target_sample_rate),
        "tap_count": tap_count,
        "channel_count": channel_count,
        "span_ms": span_ms,
        "filter_group_delay_ms": group_delay_ms,
        "coefficient_memory_bytes": coefficient_memory_bytes,
        "peak_sample": round(peak, 6),
        "max_frequency_gain_db": max_gain_db,
        "required_headroom_db": required_headroom_db,
        "issues": issues,
        "level": level,
        "applied": False,
    }


def stage_fir_artifact(
    *,
    bundle_dir: Path,
    source_wav: Path,
    label: str,
    mode: FirMode = "unknown",
    target_sample_rate: int = DEFAULT_TARGET_SAMPLE_RATE,
) -> dict[str, Any]:
    """Copy an imported FIR WAV into a bundle and write metadata.

    This is intentionally not an apply path. It creates replayable
    evidence so later FIR/runtime work can validate configs and
    CamillaDSP behavior from a known coefficient file.
    """
    bundle = Path(bundle_dir)
    if not (bundle / "info.json").exists():
        raise FirRuntimeError(f"not a correction bundle: {bundle}")
    report = inspect_fir_wav(
        Path(source_wav),
        mode=mode,
        target_sample_rate=target_sample_rate,
    )
    if report["level"] == "fail":
        codes = ", ".join(issue["code"] for issue in report.get("issues") or [])
        raise FirRuntimeError(f"refusing to stage failed FIR artifact: {codes}")
    safe_label = _sanitize_label(label)
    fir_dir = bundle / "fir"
    fir_dir.mkdir(parents=True, exist_ok=True)
    coeff_rel = f"fir/{safe_label}.wav"
    meta_rel = f"fir/{safe_label}.json"
    coeff_path = bundle / coeff_rel
    shutil.copy2(source_wav, coeff_path)
    coeff_path.chmod(0o600)
    report = {
        **report,
        "path": coeff_rel,
        "label": safe_label,
        "source_path": str(source_wav),
    }
    bundles.record_artifact(
        bundle,
        coeff_rel,
        kind="fir_coefficients",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="jasper.correction.fir_runtime.stage_fir_artifact",
        dependencies=("info.json",),
        schema_version=SCHEMA_VERSION,
        metadata={
            "mode": mode,
            "tap_count": report["tap_count"],
            "required_headroom_db": report["required_headroom_db"],
        },
    )
    bundles.write_json_artifact(
        bundle,
        meta_rel,
        report,
        kind="fir_metadata",
        sensitivity="private_metadata",
        recomputable=True,
        generated_by="jasper.correction.fir_runtime.stage_fir_artifact",
        dependencies=("info.json", coeff_rel),
        schema_version=SCHEMA_VERSION,
        file_mode=0o600,
    )
    os.chmod(bundle / meta_rel, 0o600)
    return report
