# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement quality checks for sweep captures.

The DSP math can produce a curve for almost any WAV, including a
clipped, silent, or browser-processed recording. This module turns the
raw capture into explicit quality facts before deconvolution so the UI,
debug bundles, doctor, and future calibration agent all reason from the
same evidence.

The thresholds are supplied by a :class:`~jasper.audio_measurement.quality_model.QualityModel`
profile so the room, driver, and level-ramp layers can differ by data rather
than by forked constants; :func:`assess_capture` defaults to the ``ROOM``
profile, which carries the pre-extraction values verbatim. QualityModel
profiles are the sole source of capture-quality thresholds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from jasper.audio_measurement.quality_model import ROOM, QualityModel

Severity = Literal["warn", "fail"]

# Default used by the local dBFS conversion helper.
DBFS_FLOOR = ROOM.dbfs_floor


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: Severity
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.details:
            out["details"] = self.details
        return out


@dataclass(frozen=True)
class CaptureQuality:
    sample_rate: int
    duration_s: float
    peak_dbfs: float
    rms_dbfs: float
    clipped_fraction: float
    issues: tuple[QualityIssue, ...]

    @property
    def failed(self) -> bool:
        return any(issue.severity == "fail" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warn")

    def fail_messages(self) -> list[str]:
        return [
            issue.message
            for issue in self.issues
            if issue.severity == "fail"
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "duration_s": self.duration_s,
            "peak_dbfs": self.peak_dbfs,
            "rms_dbfs": self.rms_dbfs,
            "clipped_fraction": self.clipped_fraction,
            "failed": self.failed,
            "warning_count": self.warning_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


class CaptureQualityError(ValueError):
    """Raised when a capture has blocking quality failures."""

    def __init__(self, report: CaptureQuality) -> None:
        self.report = report
        super().__init__(
            "capture quality failed: " + "; ".join(report.fail_messages())
        )


def _dbfs(value: float, floor: float = DBFS_FLOOR) -> float:
    if value <= 0 or not math.isfinite(value):
        return floor
    return max(floor, 20.0 * math.log10(value))


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def assess_capture(
    captured: np.ndarray,
    *,
    sample_rate: int,
    expected_sample_rate: int,
    sweep_n_samples: int,
    has_mic_calibration: bool,
    input_device: dict[str, Any] | None = None,
    truncated_from_samples: int | None = None,
    quality_model: QualityModel = ROOM,
) -> CaptureQuality:
    """Assess a browser-uploaded sweep capture.

    Failures are conditions that make deconvolution unsafe or known-bad.
    Warnings are conditions where the run can continue, but downstream
    tools and humans should treat the result as lower confidence.

    truncated_from_samples is the capture's length BEFORE the caller
    bounded it for memory (see deconv.cap_capture_length). When it
    exceeds the assessed length, a `capture_truncated` warning is
    emitted so the truncation is visible at /status / bundle / doctor,
    not just in the journal.

    quality_model selects the layer's threshold profile (clip, dBFS floor,
    peak/RMS gates). Defaults to ROOM, whose values equal the pre-extraction
    constants, so existing callers are unaffected.
    """
    if captured.ndim != 1:
        raise ValueError(f"captured must be mono 1-D, got {captured.shape}")

    abs_capture = np.abs(captured.astype(np.float64))
    peak = float(np.max(abs_capture)) if len(abs_capture) else 0.0
    rms = float(np.sqrt(np.mean(abs_capture ** 2))) if len(abs_capture) else 0.0
    clipped = (
        float(np.mean(abs_capture >= quality_model.clip_abs_threshold))
        if len(abs_capture)
        else 0.0
    )
    peak_dbfs = _dbfs(peak, quality_model.dbfs_floor)
    rms_dbfs = _dbfs(rms, quality_model.dbfs_floor)
    duration_s = float(len(captured) / sample_rate) if sample_rate > 0 else 0.0

    issues: list[QualityIssue] = []
    if sample_rate != expected_sample_rate:
        issues.append(QualityIssue(
            code="sample_rate_mismatch",
            severity="fail",
            message=(
                f"captured sample rate {sample_rate} Hz does not match "
                f"expected {expected_sample_rate} Hz"
            ),
            details={
                "sample_rate": sample_rate,
                "expected_sample_rate": expected_sample_rate,
            },
        ))
    if len(captured) < sweep_n_samples:
        issues.append(QualityIssue(
            code="capture_too_short",
            severity="fail",
            message="capture is shorter than the played sweep",
            details={
                "captured_samples": len(captured),
                "sweep_samples": sweep_n_samples,
            },
        ))
    if (
        truncated_from_samples is not None
        and truncated_from_samples > len(captured)
    ):
        issues.append(QualityIssue(
            code="capture_truncated",
            severity="warn",
            message=(
                "capture exceeded the analysis window and was truncated to "
                "bound memory; re-measure if this was unintended"
            ),
            details={
                "original_samples": int(truncated_from_samples),
                "analyzed_samples": int(len(captured)),
                "analyzed_seconds": round(duration_s, 1),
            },
        ))
    if clipped >= quality_model.clip_fraction_fail:
        issues.append(QualityIssue(
            code="capture_clipped",
            severity="fail",
            message="capture clipped; lower speaker volume and re-measure",
            details={"clipped_fraction": clipped},
        ))
    elif clipped > 0:
        issues.append(QualityIssue(
            code="capture_near_clip",
            severity="warn",
            message="capture has samples at digital full scale",
            details={"clipped_fraction": clipped},
        ))
    if peak_dbfs < quality_model.peak_too_low_dbfs:
        issues.append(QualityIssue(
            code="capture_peak_low",
            severity="warn",
            message="capture peak is very low; result may have poor SNR",
            details={
                "peak_dbfs": peak_dbfs,
                "threshold_dbfs": quality_model.peak_too_low_dbfs,
            },
        ))
    if rms_dbfs < quality_model.rms_too_low_dbfs:
        issues.append(QualityIssue(
            code="capture_rms_low",
            severity="warn",
            message="capture RMS is very low; room response may be noise-dominated",
            details={
                "rms_dbfs": rms_dbfs,
                "threshold_dbfs": quality_model.rms_too_low_dbfs,
            },
        ))
    if not has_mic_calibration:
        issues.append(QualityIssue(
            code="mic_uncalibrated",
            severity="warn",
            message="no measurement-mic calibration was applied",
        ))

    if input_device:
        for key, code, label in [
            (
                "echo_cancellation",
                "browser_echo_cancellation",
                "echo cancellation",
            ),
            (
                "noise_suppression",
                "browser_noise_suppression",
                "noise suppression",
            ),
            (
                "auto_gain_control",
                "browser_auto_gain_control",
                "auto gain control",
            ),
        ]:
            if _optional_bool(input_device.get(key)) is True:
                issues.append(QualityIssue(
                    code=code,
                    severity="warn",
                    message=f"browser reported {label} enabled",
                ))
        channel_count = input_device.get("channel_count")
        if isinstance(channel_count, (int, float)) and int(channel_count) != 1:
            issues.append(QualityIssue(
                code="browser_channel_count",
                severity="warn",
                message=(
                    f"browser reported {channel_count:g} input channels; "
                    "expected mono"
                ),
                details={"channel_count": channel_count},
            ))

    return CaptureQuality(
        sample_rate=int(sample_rate),
        duration_s=duration_s,
        peak_dbfs=peak_dbfs,
        rms_dbfs=rms_dbfs,
        clipped_fraction=clipped,
        issues=tuple(issues),
    )
