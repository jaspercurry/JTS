"""Deterministic confidence report for room-correction measurements.

The report is intentionally modest: it summarizes evidence JTS already
collects rather than pretending to know the room. Research-backed
thresholds can refine these heuristics later without changing the
bundle shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np


ConfidenceLevel = Literal["high", "medium", "low"]

DEFAULT_BAND_HZ = (20.0, 350.0)


@dataclass(frozen=True)
class ConfidenceFinding:
    code: str
    severity: Literal["info", "warn", "fail"]
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


def _level_for_score(score: int) -> ConfidenceLevel:
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _quality_issues(
    capture_quality: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for report in capture_quality or []:
        if not isinstance(report, dict):
            continue
        for issue in report.get("issues") or []:
            if isinstance(issue, dict):
                enriched = dict(issue)
                enriched.setdefault("capture_kind", report.get("capture_kind"))
                enriched.setdefault("position_index", report.get("position_index"))
                out.append(enriched)
    return out


def _position_variance(
    *,
    position_magnitudes: list[np.ndarray],
    freqs_hz: np.ndarray | None,
    band_hz: tuple[float, float],
) -> dict[str, Any]:
    if freqs_hz is None or len(position_magnitudes) < 2:
        return {
            "available": False,
            "reason": "need at least two completed positions",
        }

    curves = [np.asarray(m, dtype=float) for m in position_magnitudes]
    freqs = np.asarray(freqs_hz, dtype=float)
    if not curves or any(curve.ndim != 1 for curve in curves):
        return {"available": False, "reason": "position curves must be 1-D"}
    if any(curve.shape[0] != freqs.shape[0] for curve in curves):
        return {"available": False, "reason": "position curve shapes differ"}
    if not np.all(np.isfinite(freqs)) or any(
        not np.all(np.isfinite(curve)) for curve in curves
    ):
        return {
            "available": False,
            "reason": "position curves contain non-finite values",
        }

    matrix = np.vstack(curves)

    mask = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    if not np.any(mask):
        return {"available": False, "reason": "no points in correction band"}

    band_freqs = freqs[mask]
    band_values = matrix[:, mask]
    std_db = np.std(band_values, axis=0)
    range_db = np.ptp(band_values, axis=0)

    median_std = float(np.median(std_db))
    p90_std = float(np.percentile(std_db, 90))
    max_range = float(np.max(range_db))

    worst_indices = np.argsort(range_db)[-5:][::-1]
    worst = [
        {
            "freq_hz": round(float(band_freqs[idx]), 2),
            "range_db": round(float(range_db[idx]), 2),
            "std_db": round(float(std_db[idx]), 2),
        }
        for idx in worst_indices
    ]

    if p90_std <= 4.0:
        variance_confidence: ConfidenceLevel = "high"
    elif p90_std <= 6.0:
        variance_confidence = "medium"
    else:
        variance_confidence = "low"

    return {
        "available": True,
        "confidence_level": variance_confidence,
        "band_hz": [band_hz[0], band_hz[1]],
        "position_count": len(position_magnitudes),
        "median_std_db": round(median_std, 2),
        "p90_std_db": round(p90_std, 2),
        "max_range_db": round(max_range, 2),
        "worst_bands": worst,
    }


def _strategy_gates(
    *,
    score: int,
    has_mic_calibration: bool,
    completed_positions: int,
    quality_failed: bool,
    browser_processing_warning: bool,
    variance: dict[str, Any],
) -> dict[str, Any]:
    has_measurement = completed_positions > 0
    gates: dict[str, Any] = {
        "safe": {"allowed": not quality_failed and has_measurement, "reasons": []},
        "balanced": {"allowed": False, "reasons": []},
        "assertive": {"allowed": False, "reasons": []},
    }

    if not has_measurement:
        for gate in gates.values():
            gate["reasons"].append("no completed measurements are available")
        return gates

    if quality_failed:
        for gate in gates.values():
            gate["reasons"].append("capture quality has blocking failures")
        return gates

    if score >= 55:
        gates["balanced"]["allowed"] = True
    else:
        gates["balanced"]["reasons"].append("overall confidence is low")

    assertive_reasons: list[str] = []
    if score < 80:
        assertive_reasons.append("overall confidence is not high")
    if not has_mic_calibration:
        assertive_reasons.append("measurement mic is not calibrated")
    if completed_positions < 3:
        assertive_reasons.append("fewer than three positions were measured")
    if browser_processing_warning:
        assertive_reasons.append("browser reported audio processing")
    if variance.get("available"):
        if variance.get("confidence_level") == "low":
            assertive_reasons.append("position variance is high")
    elif completed_positions >= 3:
        assertive_reasons.append("position variance is unavailable")
    gates["assertive"]["allowed"] = not assertive_reasons
    gates["assertive"]["reasons"] = assertive_reasons

    return gates


def build_confidence_report(
    *,
    total_positions: int,
    completed_positions: int,
    has_mic_calibration: bool,
    input_device: dict[str, Any] | None,
    capture_quality: list[dict[str, Any]],
    strategy_choice: str,
    position_magnitudes: list[np.ndarray] | None = None,
    freqs_hz: np.ndarray | None = None,
    correction_band_hz: tuple[float, float] = DEFAULT_BAND_HZ,
) -> dict[str, Any]:
    """Build a JSON-serializable confidence report.

    The output is suitable for `info.json`, `result.json`, the web
    status payload, and read-only calibration-agent tools.
    """
    findings: list[ConfidenceFinding] = []
    score = 100

    issues = _quality_issues(capture_quality)
    failed_issues = [i for i in issues if i.get("severity") == "fail"]
    warn_issues = [i for i in issues if i.get("severity") == "warn"]
    browser_processing = [
        i for i in warn_issues
        if str(i.get("code", "")).startswith("browser_")
    ]

    if completed_positions <= 0:
        score -= 45
        findings.append(ConfidenceFinding(
            code="no_completed_positions",
            severity="fail",
            message="no completed measurement positions are available",
        ))
    elif completed_positions == 1:
        score -= 20
        findings.append(ConfidenceFinding(
            code="single_position",
            severity="warn",
            message="only one listening position was measured",
        ))
    elif completed_positions == 2:
        score -= 10
        findings.append(ConfidenceFinding(
            code="two_positions",
            severity="warn",
            message="two positions are usable, but three or more is stronger",
        ))

    if completed_positions < total_positions:
        score -= 10
        findings.append(ConfidenceFinding(
            code="incomplete_position_set",
            severity="warn",
            message="not all requested positions were completed",
            details={
                "completed_positions": completed_positions,
                "total_positions": total_positions,
            },
        ))

    if not has_mic_calibration:
        score -= 25
        findings.append(ConfidenceFinding(
            code="uncalibrated_mic",
            severity="warn",
            message="no measurement-mic calibration was applied",
        ))

    if not input_device:
        score -= 5
        findings.append(ConfidenceFinding(
            code="missing_input_device",
            severity="warn",
            message="browser input-device metadata is unavailable",
        ))

    if failed_issues:
        score -= 60
        findings.append(ConfidenceFinding(
            code="capture_quality_failed",
            severity="fail",
            message="one or more captures had blocking quality failures",
            details={"count": len(failed_issues)},
        ))

    if warn_issues:
        score -= min(20, 5 * len(warn_issues))
        findings.append(ConfidenceFinding(
            code="capture_quality_warnings",
            severity="warn",
            message="capture quality warnings lowered confidence",
            details={"count": len(warn_issues)},
        ))

    if browser_processing:
        score -= 10
        findings.append(ConfidenceFinding(
            code="browser_processing_reported",
            severity="warn",
            message="browser-reported audio processing may affect measurement",
            details={"count": len(browser_processing)},
        ))

    variance = _position_variance(
        position_magnitudes=position_magnitudes or [],
        freqs_hz=freqs_hz,
        band_hz=correction_band_hz,
    )
    if variance.get("available"):
        if variance.get("confidence_level") == "medium":
            score -= 10
            findings.append(ConfidenceFinding(
                code="moderate_position_variance",
                severity="warn",
                message="position variance is moderate in the correction band",
            ))
        elif variance.get("confidence_level") == "low":
            score -= 25
            findings.append(ConfidenceFinding(
                code="high_position_variance",
                severity="warn",
                message="position variance is high in the correction band",
            ))

    score = max(0, min(100, score))
    level = _level_for_score(score)
    gates = _strategy_gates(
        score=score,
        has_mic_calibration=has_mic_calibration,
        completed_positions=completed_positions,
        quality_failed=bool(failed_issues),
        browser_processing_warning=bool(browser_processing),
        variance=variance,
    )

    return {
        "version": 1,
        "level": level,
        "score": score,
        "summary": (
            f"{level.capitalize()} confidence based on {completed_positions} "
            f"of {total_positions} requested position(s)."
        ),
        "strategy_choice": strategy_choice,
        "correction_band_hz": [correction_band_hz[0], correction_band_hz[1]],
        "evidence": {
            "total_positions": total_positions,
            "completed_positions": completed_positions,
            "mic_calibrated": has_mic_calibration,
            "input_device_present": bool(input_device),
            "quality_issue_count": len(issues),
            "quality_warning_count": len(warn_issues),
            "quality_failure_count": len(failed_issues),
        },
        "position_variance": variance,
        "strategy_gates": gates,
        "findings": [finding.to_dict() for finding in findings],
    }
