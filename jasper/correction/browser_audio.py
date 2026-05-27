"""Browser audio-path preflight report for room correction.

The browser can grant a microphone while silently changing capture
settings. Keep these facts in one deterministic report so the UI,
debug bundles, confidence model, and future calibration agent all
reason from the same evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


Severity = Literal["info", "warn", "fail"]


@dataclass(frozen=True)
class BrowserAudioIssue:
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
class BrowserAudioReport:
    available: bool
    level: Literal["ok", "warn", "fail"]
    summary: str
    expected_sample_rate: int
    input_device: dict[str, Any] | None
    mic_calibrated: bool
    issues: tuple[BrowserAudioIssue, ...]

    @property
    def failed(self) -> bool:
        return any(issue.severity == "fail" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warn")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "available": self.available,
            "level": self.level,
            "summary": self.summary,
            "expected_sample_rate": self.expected_sample_rate,
            "input_device": self.input_device,
            "mic_calibrated": self.mic_calibrated,
            "failed": self.failed,
            "warning_count": self.warning_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _is_enabled(value: Any) -> bool:
    return value is True


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def assess_browser_audio_path(
    *,
    input_device: dict[str, Any] | None,
    expected_sample_rate: int,
    has_mic_calibration: bool,
) -> BrowserAudioReport:
    """Assess browser-reported capture settings before a sweep.

    This does not prove acoustic loopback. It records whether the
    browser path looks measurement-safe based on metadata JTS already
    receives from `getUserMedia().getSettings()`.
    """
    issues: list[BrowserAudioIssue] = []
    if not input_device:
        issues.append(BrowserAudioIssue(
            code="browser_input_device_missing",
            severity="warn",
            message=(
                "browser input-device metadata is unavailable; measurement "
                "can continue, but confidence is lower"
            ),
        ))
        return BrowserAudioReport(
            available=False,
            level="warn",
            summary="Browser audio path metadata is unavailable.",
            expected_sample_rate=expected_sample_rate,
            input_device=None,
            mic_calibrated=has_mic_calibration,
            issues=tuple(issues),
        )

    sample_rate = _optional_int(input_device.get("sample_rate"))
    if sample_rate is None:
        issues.append(BrowserAudioIssue(
            code="browser_sample_rate_missing",
            severity="warn",
            message="browser did not report the microphone sample rate",
        ))
    elif sample_rate != int(expected_sample_rate):
        issues.append(BrowserAudioIssue(
            code="sample_rate_mismatch",
            severity="fail",
            message=(
                f"browser reported {sample_rate} Hz capture; "
                f"expected {int(expected_sample_rate)} Hz"
            ),
            details={
                "sample_rate": sample_rate,
                "expected_sample_rate": int(expected_sample_rate),
            },
        ))

    channel_count = _optional_int(input_device.get("channel_count"))
    if channel_count is None:
        issues.append(BrowserAudioIssue(
            code="browser_channel_count_missing",
            severity="warn",
            message="browser did not report microphone channel count",
        ))
    elif channel_count != 1:
        issues.append(BrowserAudioIssue(
            code="browser_channel_count",
            severity="warn",
            message=(
                f"browser reported {channel_count} input channels; "
                "JTS measures the first channel as mono"
            ),
            details={"channel_count": channel_count},
        ))

    for key, code, label in [
        ("echo_cancellation", "browser_echo_cancellation", "echo cancellation"),
        ("noise_suppression", "browser_noise_suppression", "noise suppression"),
        ("auto_gain_control", "browser_auto_gain_control", "auto gain control"),
    ]:
        if _is_enabled(input_device.get(key)):
            issues.append(BrowserAudioIssue(
                code=code,
                severity="fail",
                message=f"browser reported {label} enabled",
            ))

    requested_hash = input_device.get("requested_device_id_hash")
    actual_hash = input_device.get("actual_device_id_hash")
    if requested_hash and actual_hash and requested_hash != actual_hash:
        issues.append(BrowserAudioIssue(
            code="browser_device_mismatch",
            severity="warn",
            message=(
                "browser granted a different input device than the one "
                "selected in the picker"
            ),
        ))

    if not has_mic_calibration:
        issues.append(BrowserAudioIssue(
            code="mic_uncalibrated",
            severity="warn",
            message="no measurement-mic calibration was loaded",
        ))

    level: Literal["ok", "warn", "fail"]
    if any(issue.severity == "fail" for issue in issues):
        level = "fail"
        summary = "Browser audio path is not safe for measurement."
    elif any(issue.severity == "warn" for issue in issues):
        level = "warn"
        summary = "Browser audio path is usable with lower confidence."
    else:
        level = "ok"
        summary = "Browser audio path metadata looks measurement-ready."

    return BrowserAudioReport(
        available=True,
        level=level,
        summary=summary,
        expected_sample_rate=expected_sample_rate,
        input_device=input_device,
        mic_calibrated=has_mic_calibration,
        issues=tuple(issues),
    )
