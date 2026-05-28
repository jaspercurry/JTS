"""Operator tooling around correction session bundles.

Bundles are the durable evidence boundary for correction, FIR, and
future assistant work. This module keeps inspection, replay checks, and
REW-friendly exports small and shared by CLI/tests/future web surfaces.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from . import analysis, bundles, calibration, deconv, interop


class BundleToolError(RuntimeError):
    """A bundle cannot be inspected, replayed, or exported."""


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BundleToolError(f"could not read {path.name}: {e}") from e
    if not isinstance(data, dict):
        raise BundleToolError(f"{path.name} must contain a JSON object")
    return data


def _artifact_counts(manifest: dict[str, Any] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(manifest, dict):
        return counts
    for artifact in manifest.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        kind = str(artifact.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _raw_capture_paths(bundle_dir: Path) -> list[Path]:
    capture_dir = bundle_dir / "captures"
    paths = sorted(capture_dir.glob("p*.wav")) if capture_dir.exists() else []
    verify = bundle_dir / "verify.wav"
    if verify.exists():
        paths.append(verify)
    return [p for p in paths if p.is_file()]


def _load_bundle_calibration(bundle_dir: Path) -> calibration.CalibrationCurve | None:
    payload = _read_json(bundle_dir / "mic_calibration.json")
    if not payload or not isinstance(payload.get("curve"), dict):
        return None
    return calibration.CalibrationCurve.from_dict(payload["curve"])


def inspect_bundle(
    bundle_dir: Path,
    *,
    recompute: bool = False,
) -> dict[str, Any]:
    """Summarize one correction bundle without exposing raw audio."""
    bundle_dir = bundle_dir.resolve()
    summary = bundles.summarize_bundle(bundle_dir)
    issues = bundles.validate_bundle(bundle_dir)
    manifest = _read_json(bundle_dir / bundles.ARTIFACT_MANIFEST_NAME)
    result = _read_json(bundle_dir / "result.json")
    runtime = _read_json(bundle_dir / "runtime_integrity.json")

    confidence = None
    if result:
        confidence = result.get("confidence_report")
    if confidence is None:
        confidence = summary.get("confidence_report")
    runtime_summary = None
    if runtime:
        runtime_summary = runtime.get("summary") or {
            "level": runtime.get("level"),
            "issues": runtime.get("issues"),
        }
    if runtime_summary is None:
        runtime_summary = summary.get("runtime_integrity")

    out: dict[str, Any] = {
        "bundle_dir": str(bundle_dir),
        "session_id": summary.get("session_id"),
        "state": summary.get("state"),
        "started_at": summary.get("started_at"),
        "updated_at": summary.get("updated_at"),
        "bundle_schema_version": summary.get("bundle_schema_version"),
        "artifact_count": summary.get("artifact_count", 0),
        "artifact_counts_by_kind": _artifact_counts(manifest),
        "raw_capture_count": len(_raw_capture_paths(bundle_dir)),
        "issues": [issue.to_dict() for issue in issues],
        "confidence": {
            "level": confidence.get("level"),
            "score": confidence.get("score"),
            "finding_count": len(confidence.get("findings") or []),
        } if isinstance(confidence, dict) else None,
        "runtime_integrity": {
            "level": runtime_summary.get("level"),
            "issue_count": len(runtime_summary.get("issues") or []),
        } if isinstance(runtime_summary, dict) else None,
        "exports_available": exportable_artifacts(bundle_dir),
    }
    if recompute:
        out["recompute"] = recompute_bundle_summary(bundle_dir)
    return out


def _curve_diff_metrics(
    left: np.ndarray,
    right: np.ndarray,
) -> dict[str, float]:
    delta = left - right
    return {
        "rms_db": round(float(np.sqrt(np.mean(delta ** 2))), 4),
        "max_abs_db": round(float(np.max(np.abs(delta))), 4),
    }


def recompute_bundle_summary(bundle_dir: Path) -> dict[str, Any]:
    """Replay raw captures into smoothed position curves and compare stored data."""
    bundle_dir = bundle_dir.resolve()
    info = _read_json(bundle_dir / "info.json")
    if not info:
        raise BundleToolError("info.json is required for recompute")
    sweep_meta = info.get("sweep_meta")
    if not isinstance(sweep_meta, dict):
        raise BundleToolError("info.json missing sweep_meta")

    capture_paths = sorted((bundle_dir / "captures").glob("p*.wav"))
    if not capture_paths:
        raise BundleToolError("bundle has no position captures to recompute")
    cal_curve = _load_bundle_calibration(bundle_dir)

    position_magnitudes: list[np.ndarray] = []
    freqs: np.ndarray | None = None
    for capture_path in capture_paths:
        ir, sample_rate = interop.impulse_response_from_capture(
            capture_path,
            sweep_meta=sweep_meta,
        )
        raw_freqs, mag_db = deconv.magnitude_response(ir, sample_rate)
        smoothed = analysis.smooth_fractional_octave(raw_freqs, mag_db, fraction=48)
        log_freqs, log_mag = analysis.resample_log(raw_freqs, smoothed)
        if cal_curve is not None:
            log_mag = calibration.apply_calibration_curve(
                log_freqs,
                log_mag,
                cal_curve,
            )
        log_mag = analysis.normalize_to_band(log_freqs, log_mag)
        if freqs is None:
            freqs = log_freqs
        position_magnitudes.append(log_mag)

    if freqs is None:
        raise BundleToolError("no recomputed frequency grid")
    averaged = analysis.spatial_average_db(position_magnitudes)
    out: dict[str, Any] = {
        "position_count": len(position_magnitudes),
        "freq_count": int(freqs.shape[0]),
        "f_min_hz": round(float(freqs[0]), 4),
        "f_max_hz": round(float(freqs[-1]), 4),
    }

    stored_position = _read_json(bundle_dir / "position_analysis.json")
    if stored_position:
        stored_avg = np.asarray(
            stored_position.get("spatial_average_db") or [],
            dtype=float,
        )
        stored_freqs = np.asarray(stored_position.get("freqs_hz") or [], dtype=float)
        if stored_avg.shape == averaged.shape and stored_freqs.shape == freqs.shape:
            out["stored_average_delta"] = _curve_diff_metrics(stored_avg, averaged)
        else:
            out["stored_average_delta"] = {
                "unavailable": "stored and recomputed curve shapes differ",
            }
    return out


def exportable_artifacts(bundle_dir: Path) -> dict[str, bool]:
    result = _read_json(bundle_dir / "result.json")
    info = _read_json(bundle_dir / "info.json")
    has_curves = bool(result and any(result.get(k) for k in _CURVE_KEYS))
    has_ir_inputs = bool(
        info
        and isinstance(info.get("sweep_meta"), dict)
        and _raw_capture_paths(bundle_dir)
    )
    return {
        "frequency_response_text": has_curves,
        "impulse_response_wav": has_ir_inputs,
    }


_CURVE_KEYS = ("measured", "target", "predicted", "verify")


def export_bundle(
    bundle_dir: Path,
    output_dir: Path,
    *,
    include_ir: bool = True,
) -> dict[str, Any]:
    """Export bundle artifacts for REW/external analysis."""
    bundle_dir = bundle_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = _read_json(bundle_dir / "result.json")
    info = _read_json(bundle_dir / "info.json")
    written: list[str] = []
    if result:
        session_id = str(result.get("session_id") or bundle_dir.name)
        for key in _CURVE_KEYS:
            curve = result.get(key)
            if not isinstance(curve, dict):
                continue
            for suffix, include_phase in (("frd", True), ("txt", False)):
                path = output_dir / f"{session_id}-{key}.{suffix}"
                interop.write_frequency_response_text(
                    path,
                    curve,
                    title=f"JTS {key} correction curve",
                    source=str(bundle_dir),
                    include_phase=include_phase,
                )
                written.append(str(path))

    if include_ir and info and isinstance(info.get("sweep_meta"), dict):
        sweep_meta = info["sweep_meta"]
        for capture_path in _raw_capture_paths(bundle_dir):
            ir, sample_rate = interop.impulse_response_from_capture(
                capture_path,
                sweep_meta=sweep_meta,
            )
            out_name = f"{bundle_dir.name}-{capture_path.stem}-ir.wav"
            out_path = output_dir / out_name
            interop.write_impulse_response_wav(
                out_path,
                ir,
                sample_rate=sample_rate,
            )
            written.append(str(out_path))

    if not written:
        raise BundleToolError(
            "bundle has no exportable correction curves"
            + (" or raw captures with sweep metadata" if include_ir else "")
        )

    return {
        "bundle_dir": str(bundle_dir),
        "output_dir": str(output_dir),
        "written": written,
    }
