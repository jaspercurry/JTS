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
from pathlib import Path
from typing import Any

import numpy as np

from jasper.audio_measurement import deconv, snr_policy, sweep
from jasper.audio_measurement.quality_model import ROOM as _ROOM_QUALITY

SCHEMA_VERSION = 1
DBFS_FLOOR = -120.0
SNR_BANDS_HZ: tuple[tuple[str, float, float], ...] = (
    ("sub_bass", 20.0, 80.0),
    ("bass", 80.0, 160.0),
    ("upper_bass", 160.0, 350.0),
    ("transition", 350.0, 1000.0),
)
# SNR trust thresholds now live on the shared ROOM QualityModel profile so the
# room, driver, and level-ramp layers differ by data rather than forked
# constants; values are unchanged (25.0 / 20.0). Kept as module-level aliases so
# existing references still resolve.
SNR_OK_DB = _ROOM_QUALITY.snr_ok_db
SNR_WARN_DB = _ROOM_QUALITY.snr_warn_db


def dbfs(value: float) -> float:
    """Convert a linear amplitude to the Room evidence dBFS floor."""
    if value <= 0 or not np.isfinite(value):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(value))


def band_levels_dbfs(
    samples: np.ndarray,
    sample_rate: int,
) -> list[dict[str, Any]]:
    """Estimate Room's four fixed trust bands with the shared FFT kernel."""
    return snr_policy.band_levels_dbfs(samples, sample_rate, SNR_BANDS_HZ)


def capture_band_snr(
    captured_wav_path: Path,
    noise_report: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Join capture and pre-sweep-noise levels by band identity."""
    if not noise_report:
        return []
    try:
        captured, sample_rate = sweep.read_wav_mono(captured_wav_path)
    except Exception:  # noqa: BLE001
        return []
    # Bound before the float64 cast: this re-reads the raw upload from disk, so
    # an oversized capture must not pay a full-length 64-bit copy before the
    # shared FFT cap fires.
    captured = deconv.cap_capture_length(
        captured,
        sweep_len=0,
        sample_rate=sample_rate,
    )
    capture_levels = band_levels_dbfs(
        captured.astype(np.float64),
        sample_rate,
    )
    noise_by_band = {
        band.get("band_id"): band
        for band in noise_report.get("band_noise_dbfs") or []
        if isinstance(band, dict)
    }
    out: list[dict[str, Any]] = []
    for capture_band in capture_levels:
        band_id = capture_band.get("band_id")
        noise_band = noise_by_band.get(band_id)
        if not noise_band:
            continue
        capture_db = float(capture_band["level_dbfs"])
        noise_db = float(noise_band["level_dbfs"])
        out.append({
            "band_id": band_id,
            "band_hz": capture_band.get("band_hz"),
            "capture_level_dbfs": round(capture_db, 2),
            "noise_level_dbfs": round(noise_db, 2),
            "estimated_snr_db": round(capture_db - noise_db, 2),
            "method": "fft_band_power_difference",
        })
    return out


def direct_arrival_report(
    impulse_response: np.ndarray,
    *,
    sample_rate: int,
) -> dict[str, Any]:
    """Summarize direct-peak strength against its pre-arrival floor."""
    ir = np.asarray(impulse_response, dtype=np.float64)
    if ir.ndim != 1 or ir.size < 8:
        return {"available": False, "reason": "impulse response unavailable"}
    peak_index = int(np.argmax(np.abs(ir)))
    pre_end = max(0, peak_index - int(0.002 * sample_rate))
    pre_start = max(0, pre_end - int(0.02 * sample_rate))
    pre = ir[pre_start:pre_end]
    if pre.size < 8:
        return {
            "available": False,
            "reason": "not enough pre-arrival samples before direct peak",
            "direct_peak_index": peak_index,
        }
    floor_rms = float(np.sqrt(np.mean(pre ** 2)))
    direct_peak = float(np.max(np.abs(ir)))
    return {
        "available": True,
        "direct_peak_index": peak_index,
        "direct_peak_dbfs": round(dbfs(direct_peak), 2),
        "pre_arrival_floor_dbfs": round(dbfs(floor_rms), 2),
        "direct_to_pre_arrival_db": round(
            dbfs(direct_peak) - dbfs(floor_rms),
            2,
        ),
        "pre_arrival_window_ms": [
            round(pre_start / sample_rate * 1000.0, 2),
            round(pre_end / sample_rate * 1000.0, 2),
        ],
    }


def repeatability_from_arrays(
    first: np.ndarray,
    repeat: np.ndarray,
    freqs_hz: np.ndarray,
    *,
    peq_f_high: float,
) -> dict[str, Any]:
    """Compare two captures at the same physical microphone position."""
    if first.shape != repeat.shape or first.shape != freqs_hz.shape:
        return {
            "available": False,
            "level": "unavailable",
            "reason": "repeat and original curves use different shapes",
        }
    upper_band_hz = min(350.0, peq_f_high)
    mask = (freqs_hz >= 50.0) & (freqs_hz <= upper_band_hz)
    if int(mask.sum()) < 3:
        return {
            "available": False,
            "level": "unavailable",
            "reason": "not enough points in the repeatability band",
        }
    delta = first[mask] - repeat[mask]
    abs_delta = np.abs(delta)
    rms_db = float(np.sqrt(np.mean(delta ** 2)))
    p95_abs_db = float(np.percentile(abs_delta, 95))
    max_abs_db = float(np.max(abs_delta))
    if rms_db <= 1.5 and p95_abs_db <= 3.0:
        level = "high"
    elif rms_db <= 2.5 and p95_abs_db <= 5.0:
        level = "medium"
    else:
        level = "low"
    issues: list[dict[str, Any]] = []
    if level == "low":
        issues.append({
            "code": "repeatability_low",
            "severity": "warn",
            "message": (
                "same-position repeat capture differs enough to limit "
                "assertive correction"
            ),
        })
    return {
        "available": True,
        "level": level,
        "band_hz": [50.0, upper_band_hz],
        "metrics": {
            "rms_db": round(rms_db, 2),
            "p95_abs_db": round(p95_abs_db, 2),
            "max_abs_db": round(max_abs_db, 2),
        },
        "issues": issues,
    }


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
