"""Measurement quality checks for correction captures.

The DSP math can produce a curve for almost any WAV, including a
clipped, silent, or browser-processed recording. This module turns the
raw capture into explicit quality facts before deconvolution so the UI,
debug bundles, doctor, and future calibration agent all reason from the
same evidence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

Severity = Literal["warn", "fail"]

DBFS_FLOOR = -120.0
CLIP_ABS_THRESHOLD = 0.999
CLIP_FRACTION_FAIL = 1e-4
PEAK_TOO_LOW_DBFS = -45.0
RMS_TOO_LOW_DBFS = -65.0


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


def _dbfs(value: float) -> float:
    if value <= 0 or not math.isfinite(value):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(value))


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
) -> CaptureQuality:
    """Assess a browser-uploaded correction capture.

    Failures are conditions that make deconvolution unsafe or known-bad.
    Warnings are conditions where the run can continue, but downstream
    tools and humans should treat the result as lower confidence.
    """
    if captured.ndim != 1:
        raise ValueError(f"captured must be mono 1-D, got {captured.shape}")

    abs_capture = np.abs(captured.astype(np.float64))
    peak = float(np.max(abs_capture)) if len(abs_capture) else 0.0
    rms = float(np.sqrt(np.mean(abs_capture ** 2))) if len(abs_capture) else 0.0
    clipped = (
        float(np.mean(abs_capture >= CLIP_ABS_THRESHOLD))
        if len(abs_capture)
        else 0.0
    )
    peak_dbfs = _dbfs(peak)
    rms_dbfs = _dbfs(rms)
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
    if clipped >= CLIP_FRACTION_FAIL:
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
    if peak_dbfs < PEAK_TOO_LOW_DBFS:
        issues.append(QualityIssue(
            code="capture_peak_low",
            severity="warn",
            message="capture peak is very low; result may have poor SNR",
            details={"peak_dbfs": peak_dbfs, "threshold_dbfs": PEAK_TOO_LOW_DBFS},
        ))
    if rms_dbfs < RMS_TOO_LOW_DBFS:
        issues.append(QualityIssue(
            code="capture_rms_low",
            severity="warn",
            message="capture RMS is very low; room response may be noise-dominated",
            details={"rms_dbfs": rms_dbfs, "threshold_dbfs": RMS_TOO_LOW_DBFS},
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
