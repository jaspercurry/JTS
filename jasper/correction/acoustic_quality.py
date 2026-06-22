# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Acoustic trust evidence for room-correction captures.

`quality.py` answers "is this WAV structurally safe to analyze?",
`browser_audio.py` answers "did getUserMedia look sane?", and
`runtime_integrity.py` answers "was the Pi healthy?". This module sits
above those facts and summarizes whether the captured acoustics are
trustworthy enough for stronger recommendations.

The report uses evidence JTS already stores: capture RMS/peak, browser
metadata, pre-sweep silence WAV summaries when present, direct-arrival
checks, banded SNR estimates, and same-position repeatability.
"""
from __future__ import annotations

import math
from typing import Any

SCHEMA_VERSION = 1
SNR_OK_DB = 25.0
SNR_WARN_DB = 20.0


def _round(value: Any, digits: int = 2) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return round(out, digits)


def _level_from_issues(issues: list[dict[str, Any]]) -> str:
    if any(issue.get("severity") == "fail" for issue in issues):
        return "fail"
    if any(issue.get("severity") == "warn" for issue in issues):
        return "warn"
    return "ok"


def _capture_summary(report: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    estimated_snr = _round(report.get("estimated_snr_db"))
    band_snr = [
        band for band in report.get("band_snr") or []
        if isinstance(band, dict)
    ]
    band_snrs = [
        _round(band.get("estimated_snr_db"))
        for band in band_snr
    ]
    band_snrs = [value for value in band_snrs if value is not None]
    min_band_snr = min(band_snrs) if band_snrs else None
    if estimated_snr is None:
        snr_level = "unavailable"
        issues.append({
            "code": "snr_unavailable",
            "severity": "info",
            "message": (
                "no measured noise floor was recorded for this capture; "
                "SNR cannot be estimated"
            ),
        })
    elif estimated_snr < SNR_WARN_DB:
        snr_level = "low"
        issues.append({
            "code": "snr_low",
            "severity": "warn",
            "message": "capture is less than 20 dB above the measured noise floor",
            "details": {
                "estimated_snr_db": estimated_snr,
                "threshold_db": SNR_WARN_DB,
            },
        })
    elif estimated_snr < SNR_OK_DB:
        snr_level = "medium"
        issues.append({
            "code": "snr_marginal",
            "severity": "warn",
            "message": "capture SNR is usable but below the high-confidence target",
            "details": {
                "estimated_snr_db": estimated_snr,
                "target_db": SNR_OK_DB,
            },
        })
    else:
        snr_level = "high"
    if min_band_snr is not None and min_band_snr < SNR_WARN_DB:
        issues.append({
            "code": "band_snr_low",
            "severity": "warn",
            "message": "one or more modal bands have low estimated SNR",
            "details": {
                "min_band_snr_db": min_band_snr,
                "threshold_db": SNR_WARN_DB,
            },
        })

    return {
        "capture_kind": report.get("capture_kind"),
        "position_index": report.get("position_index"),
        "artifact_path": report.get("artifact_path"),
        "waveform": {
            "peak_dbfs": _round(report.get("peak_dbfs")),
            "rms_dbfs": _round(report.get("rms_dbfs")),
            "duration_s": _round(report.get("duration_s"), digits=3),
            "clipped_fraction": _round(
                report.get("clipped_fraction"),
                digits=8,
            ),
        },
        "snr": {
            "level": snr_level,
            "estimated_snr_db": estimated_snr,
            "noise_floor_dbfs": _round(report.get("noise_floor_dbfs")),
            "noise_artifact_path": report.get("noise_artifact_path"),
            "method": report.get("noise_floor_method") or (
                "browser_noise_floor_minus_capture_rms"
                if estimated_snr is not None
                else "unavailable"
            ),
            "limitations": (
                "dBFS trust estimate, not calibrated acoustic SPL."
            ),
            "band_snr": band_snr,
            "min_band_snr_db": min_band_snr,
        },
        "direct_arrival": report.get("direct_arrival"),
        "issues": issues,
    }


def build_acoustic_quality_report(
    *,
    session_id: str,
    capture_quality: list[dict[str, Any]],
    noise_reports: list[dict[str, Any]] | None = None,
    repeat_quality: dict[str, Any] | None = None,
    repeatability: dict[str, Any] | None = None,
    verify_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable acoustic-quality report."""
    captures = [
        _capture_summary(report)
        for report in capture_quality
        if isinstance(report, dict)
    ]
    if isinstance(repeat_quality, dict):
        captures.append(_capture_summary(repeat_quality))
    if isinstance(verify_quality, dict):
        captures.append(_capture_summary(verify_quality))

    snrs = [
        capture["snr"]["estimated_snr_db"]
        for capture in captures
        if capture["snr"]["estimated_snr_db"] is not None
    ]
    band_snrs = [
        capture["snr"].get("min_band_snr_db")
        for capture in captures
        if capture["snr"].get("min_band_snr_db") is not None
    ]
    issues = [
        issue
        for capture in captures
        for issue in capture.get("issues", [])
        if issue.get("severity") in {"warn", "fail"}
    ]
    if captures and not snrs:
        issues.append({
            "code": "snr_evidence_missing",
            "severity": "warn",
            "message": (
                "captures are present but no measured noise floor was "
                "recorded; SNR is unavailable"
            ),
        })

    snr_level = "unavailable"
    min_snr = min(snrs) if snrs else None
    min_band_snr = min(band_snrs) if band_snrs else None
    if min_snr is not None:
        if min_snr >= SNR_OK_DB:
            snr_level = "high"
        elif min_snr >= SNR_WARN_DB:
            snr_level = "medium"
        else:
            snr_level = "low"

    if not isinstance(repeatability, dict):
        repeatability = {
            "available": False,
            "level": "unavailable",
            "reason": "same-position repeat capture was not recorded",
        }
    for issue in repeatability.get("issues") or []:
        if isinstance(issue, dict) and issue.get("severity") in {"warn", "fail"}:
            issues.append(issue)
    summary_level = _level_from_issues(issues)
    if snr_level == "low":
        summary_level = "warn"

    recommended_action = "bundle is ready for read-only review"
    if summary_level == "fail":
        recommended_action = "remeasure before interpreting correction"
    elif snr_level in {"low", "unavailable"}:
        recommended_action = "remeasure or capture a noise floor before stronger advice"
    elif repeatability["level"] == "unavailable":
        recommended_action = (
            "safe PEQ review is reasonable; repeatability is still required "
            "before assertive or FIR work"
        )

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "summary": {
            "level": summary_level,
            "snr_level": snr_level,
            "min_estimated_snr_db": _round(min_snr),
            "min_band_snr_db": _round(min_band_snr),
            "capture_count": len(captures),
            "noise_capture_count": len(noise_reports or []),
            "issue_count": len(issues),
            "repeatability_level": repeatability["level"],
            "recommended_action": recommended_action,
        },
        "captures": captures,
        "noise_captures": [
            report for report in noise_reports or []
            if isinstance(report, dict)
        ],
        "repeatability": repeatability,
        "issues": issues,
        "thresholds": {
            "snr_ok_db": SNR_OK_DB,
            "snr_warn_db": SNR_WARN_DB,
        },
    }
