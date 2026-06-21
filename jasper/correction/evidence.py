# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic evidence packets for correction-bundle review.

The calibration agent should reason from facts JTS collected, not from
ad hoc parsing of raw bundle files. This module builds a compact,
read-only packet that can be rendered in the CLI today and handed to a
future LLM without granting it apply/reset privileges.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from . import acoustic_quality, bundles

SCHEMA_VERSION = 2
REPEATABILITY_BAND_HZ = (50.0, 350.0)
REPEATABILITY_HIGH_RMS_DB = 1.5
REPEATABILITY_HIGH_P95_DB = 3.0
REPEATABILITY_MEDIUM_RMS_DB = 2.5
REPEATABILITY_MEDIUM_P95_DB = 5.0


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _round(value: Any, digits: int = 2) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return round(out, digits)


def _issue_dicts(issues: list[bundles.BundleIssue]) -> list[dict[str, str]]:
    return [issue.to_dict() for issue in issues]


def _curve(result: dict[str, Any] | None, name: str) -> tuple[np.ndarray, np.ndarray] | None:
    if not isinstance(result, dict):
        return None
    curve = result.get(name)
    if not isinstance(curve, dict):
        return None
    freqs = curve.get("freqs_hz")
    mags = curve.get("magnitude_db")
    if not isinstance(freqs, list) or not isinstance(mags, list):
        return None
    n = min(len(freqs), len(mags))
    if n < 3:
        return None
    try:
        f = np.asarray(freqs[:n], dtype=float)
        m = np.asarray(mags[:n], dtype=float)
    except (TypeError, ValueError):
        return None
    if f.ndim != 1 or m.ndim != 1 or f.shape != m.shape:
        return None
    if not np.all(np.isfinite(f)) or not np.all(np.isfinite(m)):
        return None
    return f, m


def _mic_identity(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    mic = payload.get("mic_calibration")
    if not isinstance(mic, dict):
        return None
    return {
        key: mic.get(key)
        for key in (
            "provider",
            "model",
            "model_key",
            "serial_hash",
            # Legacy bundles may have persisted a raw serial before
            # public metadata was tightened. Use it only as an identity
            # input for local comparison; do not surface it in issues.
            "serial",
            "file_sha256",
            "orientation",
            "sign_convention",
        )
        if mic.get(key) is not None
    }


def _input_identity(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    device = payload.get("input_device")
    if not isinstance(device, dict):
        return None
    stable = {
        key: device.get(key)
        for key in (
            # Legacy browser field names:
            "deviceId",
            "groupId",
            # Current JTS sanitized fields:
            "device_id_hash",
            "requested_device_id_hash",
            "actual_device_id_hash",
            "sample_rate",
            "channel_count",
            "echo_cancellation",
            "noise_suppression",
            "auto_gain_control",
        )
        if device.get(key)
    }
    return stable or None


def _level_for_repeatability(rms_db: float, p95_abs_db: float) -> str:
    if (
        rms_db <= REPEATABILITY_HIGH_RMS_DB
        and p95_abs_db <= REPEATABILITY_HIGH_P95_DB
    ):
        return "high"
    if (
        rms_db <= REPEATABILITY_MEDIUM_RMS_DB
        and p95_abs_db <= REPEATABILITY_MEDIUM_P95_DB
    ):
        return "medium"
    return "low"


def compare_bundle_repeatability(
    bundle_dir: Path,
    repeat_bundle_dir: Path | None,
    *,
    band_hz: tuple[float, float] = REPEATABILITY_BAND_HZ,
) -> dict[str, Any]:
    """Compare two same-position bundles over the correction band."""
    if repeat_bundle_dir is None:
        return {
            "available": False,
            "level": "unavailable",
            "reason": "no same-position repeat bundle was provided",
        }
    result_a = _read_json(bundle_dir / "result.json")
    result_b = _read_json(repeat_bundle_dir / "result.json")
    curve_a = _curve(result_a, "measured")
    curve_b = _curve(result_b, "measured")
    if curve_a is None or curve_b is None:
        return {
            "available": False,
            "level": "unavailable",
            "reason": "one or both bundles are missing measured curves",
        }
    freqs_a, mag_a = curve_a
    freqs_b, mag_b = curve_b
    if freqs_a.shape != freqs_b.shape or not np.allclose(freqs_a, freqs_b):
        return {
            "available": False,
            "level": "unavailable",
            "reason": "measured curves use different frequency grids",
        }
    mask = (freqs_a >= band_hz[0]) & (freqs_a <= band_hz[1])
    if int(mask.sum()) < 3:
        return {
            "available": False,
            "level": "unavailable",
            "reason": "not enough points in the repeatability band",
        }

    delta = mag_a[mask] - mag_b[mask]
    abs_delta = np.abs(delta)
    rms_db = float(np.sqrt(np.mean(delta ** 2)))
    p95_abs_db = float(np.percentile(abs_delta, 95))
    max_abs_db = float(np.max(abs_delta))
    level = _level_for_repeatability(rms_db, p95_abs_db)

    info_a = _read_json(bundle_dir / "info.json")
    info_b = _read_json(repeat_bundle_dir / "info.json")
    issues: list[dict[str, Any]] = []
    if _mic_identity(info_a) != _mic_identity(info_b):
        issues.append({
            "code": "repeatability_mic_mismatch",
            "severity": "warn",
            "message": "repeat bundle used different microphone calibration metadata",
        })
    if _input_identity(info_a) != _input_identity(info_b):
        issues.append({
            "code": "repeatability_input_device_mismatch",
            "severity": "warn",
            "message": "repeat bundle used different browser input-device metadata",
        })
    if level == "low":
        issues.append({
            "code": "repeatability_low",
            "severity": "warn",
            "message": "same-position measurements differ enough to limit trust",
        })

    return {
        "available": True,
        "level": level,
        "band_hz": [_round(band_hz[0]), _round(band_hz[1])],
        "metrics": {
            "rms_db": _round(rms_db),
            "p95_abs_db": _round(p95_abs_db),
            "max_abs_db": _round(max_abs_db),
        },
        "issues": issues,
    }


def _load_or_build_acoustic_quality(
    *,
    bundle_dir: Path,
    info: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    report = _read_json(bundle_dir / "acoustic_quality.json")
    if report is not None:
        return report
    capture_quality = list(info.get("capture_quality") or [])
    verify_quality = info.get("verify_quality")
    if result:
        existing = {
            json.dumps(r, sort_keys=True, default=str)
            for r in capture_quality
            if isinstance(r, dict)
        }
        for report_item in result.get("capture_quality") or []:
            if not isinstance(report_item, dict):
                continue
            key = json.dumps(report_item, sort_keys=True, default=str)
            if key not in existing:
                capture_quality.append(report_item)
                existing.add(key)
        if not verify_quality:
            verify_quality = result.get("verify_quality")
    return acoustic_quality.build_acoustic_quality_report(
        session_id=str(info.get("session_id") or bundle_dir.name),
        capture_quality=[r for r in capture_quality if isinstance(r, dict)],
        noise_reports=[
            r for r in info.get("noise_reports") or []
            if isinstance(r, dict)
        ],
        repeat_quality=(
            info.get("repeat_quality")
            if isinstance(info.get("repeat_quality"), dict)
            else None
        ),
        repeatability=(
            info.get("repeatability_report")
            if isinstance(info.get("repeatability_report"), dict)
            else None
        ),
        verify_quality=verify_quality if isinstance(verify_quality, dict) else None,
    )


def _position_summary(
    position_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(position_analysis, dict):
        return {"available": False, "reason": "position_analysis.json unavailable"}
    available = position_analysis.get("available")
    if available is None:
        available = bool(
            position_analysis.get("position_count")
            or position_analysis.get("positions")
            or position_analysis.get("bands")
            or position_analysis.get("chart")
        )
    flags = [
        f for f in position_analysis.get("feature_flags") or []
        if isinstance(f, dict)
    ]
    return {
        "available": bool(available),
        "position_count": position_analysis.get("position_count"),
        "feature_flag_count": len(flags),
        "feature_flags": flags[:6],
        "bands": [
            {
                "band_id": band.get("band_id"),
                "label": band.get("label"),
                "confidence_level": band.get("confidence_level"),
                "p90_std_db": band.get("p90_std_db"),
                "max_range_db": band.get("max_range_db"),
            }
            for band in position_analysis.get("bands") or []
            if isinstance(band, dict)
        ],
    }


def _gate_payload(
    gate: dict[str, Any] | None,
    *,
    fallback_reason: str,
) -> dict[str, Any]:
    if isinstance(gate, dict):
        reasons = [
            str(reason)
            for reason in gate.get("reasons") or []
            if reason
        ]
        return {
            "allowed": bool(gate.get("allowed")),
            "reasons": reasons,
        }
    return {"allowed": False, "reasons": [fallback_reason]}


def _capability_permissions(
    *,
    confidence_report: dict[str, Any] | None,
    acoustic_report: dict[str, Any],
    runtime_summary: dict[str, Any],
    repeatability: dict[str, Any],
) -> dict[str, Any]:
    gates = (
        confidence_report.get("strategy_gates")
        if isinstance(confidence_report, dict)
        else None
    )
    gates = gates if isinstance(gates, dict) else {}
    permissions = {
        "safe_peq": _gate_payload(
            gates.get("safe"),
            fallback_reason="confidence gate unavailable",
        ),
        "balanced_peq": _gate_payload(
            gates.get("balanced"),
            fallback_reason="confidence gate unavailable",
        ),
        "assertive_peq": _gate_payload(
            gates.get("assertive"),
            fallback_reason="confidence gate unavailable",
        ),
        "future_fir": _gate_payload(
            gates.get("future_fir"),
            fallback_reason="future-FIR confidence gate unavailable",
        ),
    }

    global_reasons: list[str] = []
    acoustic_summary = acoustic_report.get("summary") or {}
    if acoustic_summary.get("level") == "fail":
        global_reasons.append("acoustic quality has blocking failures")
    if runtime_summary.get("level") == "fail":
        global_reasons.append("runtime integrity has blocking failures")
    for payload in permissions.values():
        if global_reasons:
            payload["allowed"] = False
            payload["reasons"] = sorted(set(payload["reasons"] + global_reasons))
    if repeatability.get("level") == "low":
        repeatability_reason = "same-position repeatability is low"
        for key in ("assertive_peq", "future_fir"):
            payload = permissions[key]
            payload["allowed"] = False
            payload["reasons"] = sorted(set(payload["reasons"] + [repeatability_reason]))

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "permissions": permissions,
        "summary": {
            "safe_peq_allowed": permissions["safe_peq"]["allowed"],
            "balanced_peq_allowed": permissions["balanced_peq"]["allowed"],
            "assertive_peq_allowed": permissions["assertive_peq"]["allowed"],
            "future_fir_allowed": permissions["future_fir"]["allowed"],
        },
    }


def _missing_evidence(
    *,
    info: dict[str, Any],
    result: dict[str, Any] | None,
    bundle_dir: Path,
    acoustic_report: dict[str, Any],
    runtime_summary: dict[str, Any],
    position_summary: dict[str, Any],
    repeatability: dict[str, Any],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []

    def add(code: str, severity: str, message: str) -> None:
        missing.append({
            "code": code,
            "severity": severity,
            "message": message,
        })

    if result is None:
        add("result_json_missing", "warn", "result.json is unavailable")
    if not (bundle_dir / bundles.ARTIFACT_MANIFEST_NAME).exists():
        add(
            "artifact_manifest_missing",
            "warn",
            "artifact manifest is unavailable",
        )
    if not info.get("mic_calibration"):
        add(
            "mic_calibration_missing",
            "warn",
            "measurement microphone calibration is unavailable",
        )
    acoustic_summary = acoustic_report.get("summary") or {}
    if acoustic_summary.get("snr_level") in {None, "unknown", "unavailable"}:
        add(
            "snr_evidence_missing",
            "warn",
            "pre-sweep noise / SNR evidence is unavailable",
        )
    if repeatability.get("level") in {None, "unknown", "unavailable"}:
        add(
            "repeatability_missing",
            "info",
            "same-position repeatability has not been checked",
        )
    if runtime_summary.get("level") in {None, "unknown"}:
        add(
            "runtime_integrity_missing",
            "warn",
            "runtime-integrity evidence is unavailable",
        )
    if not position_summary.get("available"):
        add(
            "position_analysis_missing",
            "warn",
            "position-analysis evidence is unavailable",
        )
    if not ((result or {}).get("verify") or info.get("verify_metrics")):
        add(
            "verify_measurement_missing",
            "info",
            "post-correction verification sweep is unavailable",
        )
    return missing


def _runtime_summary(
    runtime: dict[str, Any] | None,
    info: dict[str, Any],
) -> dict[str, Any]:
    summary = None
    if isinstance(runtime, dict):
        summary = runtime.get("summary")
    if not isinstance(summary, dict):
        summary = info.get("runtime_integrity")
    if isinstance(summary, dict):
        return summary
    return {"level": "unknown", "issue_count": 0}


def _agent_readiness(
    *,
    bundle_issues: list[bundles.BundleIssue],
    confidence_report: dict[str, Any] | None,
    acoustic_report: dict[str, Any],
    runtime_summary: dict[str, Any],
    repeatability: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    failures = [issue for issue in bundle_issues if issue.severity == "fail"]
    if failures:
        reasons.extend(issue.message for issue in failures[:3])

    acoustic_summary = acoustic_report.get("summary") or {}
    acoustic_level = acoustic_summary.get("level")
    snr_level = acoustic_summary.get("snr_level")
    if acoustic_level == "fail":
        reasons.append("acoustic quality has blocking failures")
    elif snr_level in {"low", "unavailable"}:
        reasons.append("SNR evidence is weak or missing")

    runtime_level = runtime_summary.get("level")
    if runtime_level == "fail":
        reasons.append("runtime integrity has blocking failures")
    elif runtime_level == "warn":
        reasons.append("runtime integrity has warnings")

    confidence_level = (
        confidence_report.get("level")
        if isinstance(confidence_report, dict)
        else None
    )
    if confidence_level == "low":
        reasons.append("measurement confidence is low")

    repeatability_level = repeatability.get("level")
    if repeatability_level == "low":
        reasons.append("same-position repeatability is low")
    elif repeatability_level == "unavailable":
        reasons.append("same-position repeatability has not been checked")

    level = "ready"
    if failures or acoustic_level == "fail" or runtime_level == "fail":
        level = "blocked"
    elif reasons:
        level = "caution"

    recommended_action = "ready for read-only critique"
    if level == "blocked":
        recommended_action = "remeasure or repair bundle evidence first"
    elif snr_level in {"low", "unavailable"}:
        recommended_action = "collect stronger pre-sweep noise/SNR evidence"
    elif repeatability_level == "unavailable":
        recommended_action = (
            "safe PEQ critique is reasonable; collect a repeat bundle "
            "before assertive or FIR recommendations"
        )

    return {
        "level": level,
        "allowed_review": level != "blocked",
        "recommended_action": recommended_action,
        "reasons": reasons,
    }


def build_evidence_packet(
    bundle_dir: Path,
    *,
    repeat_bundle_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the read-only evidence packet for agent/human review."""
    info = _read_json(bundle_dir / "info.json")
    if info is None:
        raise bundles.BundleError(f"bundle missing info.json: {bundle_dir}")
    result = _read_json(bundle_dir / "result.json")
    runtime = _read_json(bundle_dir / "runtime_integrity.json")
    position_analysis = (
        _read_json(bundle_dir / "position_analysis.json")
        or info.get("position_analysis")
    )
    if not isinstance(position_analysis, dict):
        position_analysis = None

    bundle_issues = bundles.validate_bundle(bundle_dir)
    acoustic_report = _load_or_build_acoustic_quality(
        bundle_dir=bundle_dir,
        info=info,
        result=result,
    )
    native_repeatability = acoustic_report.get("repeatability")
    if repeat_bundle_dir is not None:
        repeatability = compare_bundle_repeatability(
            bundle_dir,
            repeat_bundle_dir,
        )
    elif isinstance(native_repeatability, dict):
        repeatability = native_repeatability
    else:
        repeatability = compare_bundle_repeatability(bundle_dir, None)
    confidence_report = (
        (result or {}).get("confidence_report")
        or info.get("confidence_report")
        or ((result or {}).get("design_report") or {}).get("confidence_report")
        or (info.get("design_report") or {}).get("confidence_report")
    )
    runtime_summary = _runtime_summary(runtime, info)
    position_summary = _position_summary(position_analysis)
    capability_permissions = _capability_permissions(
        confidence_report=(
            confidence_report if isinstance(confidence_report, dict) else None
        ),
        acoustic_report=acoustic_report,
        runtime_summary=runtime_summary,
        repeatability=repeatability,
    )
    missing_evidence = _missing_evidence(
        info=info,
        result=result,
        bundle_dir=bundle_dir,
        acoustic_report=acoustic_report,
        runtime_summary=runtime_summary,
        position_summary=position_summary,
        repeatability=repeatability,
    )

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "bundle_dir": str(bundle_dir),
        "session_id": info.get("session_id") or bundle_dir.name,
        "bundle": {
            "state": info.get("state"),
            "schema_version": info.get("bundle_schema_version"),
            "has_result": result is not None,
            "has_artifact_manifest": (
                bundle_dir / bundles.ARTIFACT_MANIFEST_NAME
            ).exists(),
            "issues": _issue_dicts(bundle_issues),
        },
        "measurement": {
            "target_choice": info.get("target_choice"),
            "strategy_choice": info.get("strategy_choice"),
            "positions_completed": info.get("current_position"),
            "positions_requested": info.get("total_positions"),
            "mic_calibration": info.get("mic_calibration"),
            "input_device": info.get("input_device"),
            "browser_audio": info.get("browser_audio_report"),
        },
        "confidence": confidence_report,
        "acoustic_quality": acoustic_report,
        "runtime_integrity": {
            "summary": runtime_summary,
            "issues": (
                runtime.get("issues") if isinstance(runtime, dict) else None
            ),
        },
        "position_analysis": position_summary,
        "repeatability": repeatability,
        "capability_permissions": capability_permissions,
        "missing_evidence": missing_evidence,
        "agent_readiness": _agent_readiness(
            bundle_issues=bundle_issues,
            confidence_report=(
                confidence_report if isinstance(confidence_report, dict) else None
            ),
            acoustic_report=acoustic_report,
            runtime_summary=runtime_summary,
            repeatability=repeatability,
        ),
        "side_effects": [],
    }
