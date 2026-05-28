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

from . import spatial


ConfidenceLevel = Literal["high", "medium", "low"]

DEFAULT_BAND_HZ = (20.0, 350.0)

POSITION_ANALYSIS_BANDS: tuple[dict[str, Any], ...] = (
    {
        "band_id": "sub_bass",
        "label": "Sub bass",
        "band_hz": (20.0, 80.0),
    },
    {
        "band_id": "bass",
        "label": "Bass",
        "band_hz": (80.0, 160.0),
    },
    {
        "band_id": "upper_bass",
        "label": "Upper bass",
        "band_hz": (160.0, 300.0),
    },
    {
        "band_id": "transition",
        "label": "Transition",
        "band_hz": (300.0, 500.0),
    },
)


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


def _browser_audio_issues(
    browser_audio_report: dict[str, Any] | None,
    *,
    existing_severity_by_code: dict[str, str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(browser_audio_report, dict):
        return out
    severity_rank = {"info": 0, "warn": 1, "fail": 2}
    for issue in browser_audio_report.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "")
        severity = str(issue.get("severity") or "info")
        if (
            code
            and severity_rank.get(existing_severity_by_code.get(code, ""), -1)
            >= severity_rank.get(severity, 0)
        ):
            continue
        enriched = dict(issue)
        enriched.setdefault("capture_kind", "browser_audio_path")
        out.append(enriched)
    return out


def _runtime_integrity_issues(
    runtime_integrity: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(runtime_integrity, dict):
        return []
    raw_issues = runtime_integrity.get("issues")
    if raw_issues is None:
        summary = runtime_integrity.get("summary")
        if isinstance(summary, dict):
            raw_issues = summary.get("issues")
    out: list[dict[str, Any]] = []
    for issue in raw_issues or []:
        if not isinstance(issue, dict):
            continue
        enriched = dict(issue)
        enriched.setdefault("capture_kind", issue.get("capture_kind"))
        enriched.setdefault("position_index", issue.get("position_index"))
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

    matrix, reason = spatial.build_spatial_matrix(position_magnitudes, freqs_hz)
    if matrix is None:
        return {"available": False, "reason": reason}

    freqs = matrix.freqs_hz
    mask = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    if not np.any(mask):
        return {"available": False, "reason": "no points in correction band"}

    band_freqs = freqs[mask]
    std_db = matrix.std_db[mask]
    range_db = matrix.range_db[mask]

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

    return {
        "available": True,
        "confidence_level": spatial.confidence_for_std(p90_std),
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


def _residual_metrics_for_band(
    *,
    residual_db: np.ndarray | None,
    freqs_hz: np.ndarray,
    band_hz: tuple[float, float],
) -> dict[str, Any] | None:
    if residual_db is None:
        return None
    mask = (freqs_hz >= band_hz[0]) & (freqs_hz <= band_hz[1])
    if not np.any(mask):
        return None
    values = residual_db[mask]
    return {
        "rms_db": round(float(np.sqrt(np.mean(values ** 2))), 2),
        "max_abs_db": round(float(np.max(np.abs(values))), 2),
        "peak_db": round(float(np.max(values)), 2),
        "deepest_null_db": round(float(np.min(values)), 2),
    }


def _residual_curve(
    measured_db: np.ndarray | None,
    target_db: np.ndarray | None,
    freqs_hz: np.ndarray,
) -> np.ndarray | None:
    if measured_db is None or target_db is None:
        return None
    measured = np.asarray(measured_db, dtype=float)
    target_curve = np.asarray(target_db, dtype=float)
    if measured.shape != target_curve.shape:
        return None
    if measured.ndim != 1 or measured.shape[0] != freqs_hz.shape[0]:
        return None
    if not np.all(np.isfinite(measured)) or not np.all(np.isfinite(target_curve)):
        return None
    return measured - target_curve


def _contiguous_regions(indices: np.ndarray) -> list[np.ndarray]:
    if indices.size == 0:
        return []
    regions: list[list[int]] = [[int(indices[0])]]
    for raw_idx in indices[1:]:
        idx = int(raw_idx)
        if idx == regions[-1][-1] + 1:
            regions[-1].append(idx)
        else:
            regions.append([idx])
    return [np.asarray(region, dtype=int) for region in regions]


def build_position_report(
    *,
    position_magnitudes: list[np.ndarray] | None,
    freqs_hz: np.ndarray | None,
    measured_db: np.ndarray | None = None,
    target_db: np.ndarray | None = None,
    correction_band_hz: tuple[float, float] = DEFAULT_BAND_HZ,
) -> dict[str, Any]:
    """Build deterministic multi-position reporting.

    This report is deliberately descriptive: it identifies where the
    measurement is spatially stable, where it is not, and where deep
    nulls should be treated as evidence rather than something to
    blindly boost.
    """
    matrix, reason = spatial.build_spatial_matrix(
        position_magnitudes or [],
        freqs_hz,
    )
    if matrix is None:
        return {
            "version": 1,
            "available": False,
            "reason": reason,
            "bands": [],
            "feature_flags": [],
        }

    residual_db = _residual_curve(
        measured_db,
        target_db,
        matrix.freqs_hz,
    )
    bands: list[dict[str, Any]] = []
    for definition in POSITION_ANALYSIS_BANDS:
        band_hz = definition["band_hz"]
        summary = spatial.band_summary(
            matrix,
            band_hz=band_hz,
            band_id=definition["band_id"],
            label=definition["label"],
        )
        residual_metrics = _residual_metrics_for_band(
            residual_db=residual_db,
            freqs_hz=matrix.freqs_hz,
            band_hz=band_hz,
        )
        if residual_metrics is not None:
            summary["residual"] = residual_metrics
        bands.append(summary)

    correction_summary = spatial.band_summary(
        matrix,
        band_hz=correction_band_hz,
        band_id="correction_band",
        label="Current correction band",
    )
    residual_metrics = _residual_metrics_for_band(
        residual_db=residual_db,
        freqs_hz=matrix.freqs_hz,
        band_hz=correction_band_hz,
    )
    if residual_metrics is not None:
        correction_summary["residual"] = residual_metrics
    bands.append(correction_summary)

    feature_flags: list[dict[str, Any]] = []
    for summary in bands:
        if summary.get("available") and summary.get("confidence_level") == "low":
            feature_flags.append({
                "kind": "high_position_variance",
                "band_id": summary.get("band_id"),
                "label": summary.get("label"),
                "band_hz": summary.get("band_hz"),
                "worst_freq_hz": summary.get("worst_freq_hz"),
                "p90_std_db": summary.get("p90_std_db"),
                "max_range_db": summary.get("max_range_db"),
                "decision": "avoid_aggressive_correction",
                "reason": (
                    "Seat-to-seat variation is high here, so this region "
                    "should not drive aggressive or full-range correction."
                ),
            })

    if residual_db is not None:
        correction_mask = (
            (matrix.freqs_hz >= correction_band_hz[0])
            & (matrix.freqs_hz <= correction_band_hz[1])
        )
        null_regions = _contiguous_regions(
            np.where(correction_mask & (residual_db <= -6.0))[0],
        )
        worst_nulls = sorted(
            null_regions,
            key=lambda region: float(np.min(residual_db[region])),
        )[:5]
        for region in worst_nulls:
            idx = int(region[int(np.argmin(residual_db[region]))])
            point = spatial.point_summary(
                matrix,
                freq_hz=float(matrix.freqs_hz[idx]),
            )
            feature_flags.append({
                "kind": "deep_null",
                "freq_hz": round(float(matrix.freqs_hz[idx]), 2),
                "region_hz": [
                    round(float(matrix.freqs_hz[int(region[0])]), 2),
                    round(float(matrix.freqs_hz[int(region[-1])]), 2),
                ],
                "residual_db": round(float(residual_db[idx]), 2),
                "spatial_confidence": point,
                "decision": "do_not_boost_by_default",
                "reason": (
                    "Deep in-room nulls are often position-dependent "
                    "cancellations; bounded correction should explain and "
                    "usually leave them unboosted."
                ),
            })

    return {
        "version": 1,
        "available": True,
        "position_count": matrix.position_count,
        "bands": bands,
        "feature_flags": feature_flags,
    }


def build_confidence_report(
    *,
    total_positions: int,
    completed_positions: int,
    has_mic_calibration: bool,
    input_device: dict[str, Any] | None,
    capture_quality: list[dict[str, Any]],
    strategy_choice: str,
    browser_audio_report: dict[str, Any] | None = None,
    runtime_integrity: dict[str, Any] | None = None,
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
    severity_rank = {"info": 0, "warn": 1, "fail": 2}
    quality_severity_by_code: dict[str, str] = {}
    for issue in issues:
        code = str(issue.get("code") or "")
        severity = str(issue.get("severity") or "info")
        if not code:
            continue
        existing = quality_severity_by_code.get(code, "")
        if severity_rank.get(severity, 0) > severity_rank.get(existing, -1):
            quality_severity_by_code[code] = severity
    browser_issues = _browser_audio_issues(
        browser_audio_report,
        existing_severity_by_code=quality_severity_by_code,
    )
    runtime_issues = _runtime_integrity_issues(runtime_integrity)
    quality_failed_issues = [
        i for i in issues if i.get("severity") == "fail"
    ]
    quality_warn_issues = [
        i for i in issues if i.get("severity") == "warn"
    ]
    issues = issues + browser_issues + runtime_issues
    failed_issues = [i for i in issues if i.get("severity") == "fail"]
    warn_issues = [i for i in issues if i.get("severity") == "warn"]
    browser_processing = [
        i for i in warn_issues
        if str(i.get("code", "")).startswith("browser_")
    ]
    runtime_failures = [
        i for i in runtime_issues if i.get("severity") == "fail"
    ]
    runtime_warnings = [
        i for i in runtime_issues if i.get("severity") == "warn"
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

    if quality_failed_issues:
        score -= 60
        findings.append(ConfidenceFinding(
            code="capture_quality_failed",
            severity="fail",
            message="one or more captures had blocking quality failures",
            details={"count": len(quality_failed_issues)},
        ))

    browser_audio_failures = [
        i for i in browser_issues if i.get("severity") == "fail"
    ]
    if browser_audio_failures:
        score -= 60
        findings.append(ConfidenceFinding(
            code="browser_audio_path_failed",
            severity="fail",
            message="browser audio-path preflight reported blocking failures",
            details={"count": len(browser_audio_failures)},
        ))

    if runtime_failures:
        score -= 60
        findings.append(ConfidenceFinding(
            code="runtime_integrity_failed",
            severity="fail",
            message="runtime-integrity evidence reported blocking failures",
            details={"count": len(runtime_failures)},
        ))

    if runtime_warnings:
        score -= min(15, 5 * len(runtime_warnings))
        findings.append(ConfidenceFinding(
            code="runtime_integrity_warnings",
            severity="warn",
            message="runtime-integrity warnings lowered confidence",
            details={"count": len(runtime_warnings)},
        ))

    if quality_warn_issues:
        score -= min(20, 5 * len(quality_warn_issues))
        findings.append(ConfidenceFinding(
            code="capture_quality_warnings",
            severity="warn",
            message="capture quality warnings lowered confidence",
            details={"count": len(quality_warn_issues)},
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
    position_report = build_position_report(
        position_magnitudes=position_magnitudes or [],
        freqs_hz=freqs_hz,
        correction_band_hz=correction_band_hz,
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
            "quality_warning_count": len(quality_warn_issues),
            "quality_failure_count": len(quality_failed_issues),
            "total_issue_count": len(issues),
            "browser_audio_issue_count": len(browser_issues),
            "runtime_integrity_issue_count": len(runtime_issues),
            "runtime_integrity_warning_count": len(runtime_warnings),
            "runtime_integrity_failure_count": len(runtime_failures),
        },
        "browser_audio_report": browser_audio_report,
        "runtime_integrity": runtime_integrity,
        "position_variance": variance,
        "position_bands": position_report["bands"],
        "feature_flags": position_report["feature_flags"],
        "strategy_gates": gates,
        "findings": [finding.to_dict() for finding in findings],
    }
