"""Read-only tools for calibration-agent intake.

These functions are deliberately deterministic. The future LLM layer
should call this module for facts instead of reparsing bundles or
corpus markdown ad hoc.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.correction import bundles


DEFAULT_SESSIONS_DIR = Path("/var/lib/jasper/correction/sessions")
DEFAULT_CORPUS_CANDIDATES = (
    Path.cwd() / "docs" / "calibration-agent",
    Path("/home/pi/jts/docs/calibration-agent"),
    Path(__file__).resolve().parents[2] / "docs" / "calibration-agent",
)


class AgentToolError(RuntimeError):
    """A read-only calibration-agent tool could not load its input."""


@dataclass(frozen=True)
class MeasurementBundle:
    bundle_dir: Path
    info: dict[str, Any]
    result: dict[str, Any] | None
    issues: tuple[bundles.BundleIssue, ...]


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise AgentToolError(f"{path} must be a JSON object")
    return data


def resolve_corpus_dir(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.is_dir() else None
    for candidate in DEFAULT_CORPUS_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return None


def load_measurement_bundle(
    *,
    session_id: str | None = None,
    bundle_dir: Path | None = None,
    sessions_dir: Path = DEFAULT_SESSIONS_DIR,
) -> MeasurementBundle:
    if bundle_dir is None:
        if session_id in {None, "", "latest"}:
            latest = bundles.latest_bundle(sessions_dir)
            if latest is None:
                raise AgentToolError(f"no bundles found under {sessions_dir}")
            bundle_dir = Path(str(latest["bundle_dir"]))
        else:
            bundle_dir = sessions_dir / str(session_id)

    if not bundle_dir.is_dir():
        raise AgentToolError(f"bundle not found: {bundle_dir}")
    info = _read_json(bundle_dir / "info.json")
    if info is None:
        raise AgentToolError(f"bundle missing info.json: {bundle_dir}")
    result = _read_json(bundle_dir / "result.json")
    issues = tuple(bundles.validate_bundle(bundle_dir))
    return MeasurementBundle(
        bundle_dir=bundle_dir,
        info=info,
        result=result,
        issues=issues,
    )


def _all_quality_reports(bundle: MeasurementBundle) -> list[dict[str, Any]]:
    reports = list(bundle.info.get("capture_quality") or [])
    if bundle.info.get("verify_quality"):
        reports.append(bundle.info["verify_quality"])
    if bundle.result:
        for report in bundle.result.get("capture_quality") or []:
            if report not in reports:
                reports.append(report)
        verify = bundle.result.get("verify_quality")
        if verify and verify not in reports:
            reports.append(verify)
    return [r for r in reports if isinstance(r, dict)]


def get_measurement_summary(bundle: MeasurementBundle) -> dict[str, Any]:
    info = bundle.info
    result = bundle.result or {}
    mic = info.get("mic_calibration") or {}
    quality_issues: list[dict[str, Any]] = []
    for report in _all_quality_reports(bundle):
        for issue in report.get("issues") or []:
            if isinstance(issue, dict):
                enriched = dict(issue)
                enriched.setdefault("capture_kind", report.get("capture_kind"))
                enriched.setdefault("position_index", report.get("position_index"))
                enriched.setdefault("artifact_path", report.get("artifact_path"))
                quality_issues.append(enriched)

    return {
        "session_id": info.get("session_id"),
        "state": info.get("state"),
        "target_choice": info.get("target_choice"),
        "total_positions": info.get("total_positions"),
        "current_position": info.get("current_position"),
        "has_result": bundle.result is not None,
        "has_verify": result.get("verify") is not None,
        "verify_metrics": result.get("verify_metrics") or info.get("verify_metrics"),
        "mic_calibrated": bool(mic),
        "mic": mic or None,
        "input_device": info.get("input_device"),
        "peq_count": len(result.get("peqs") or info.get("peqs") or []),
        "quality_issue_count": len(quality_issues),
        "quality_issues": quality_issues,
        "bundle_issues": [issue.to_dict() for issue in bundle.issues],
    }


def _curve(result: dict[str, Any], name: str) -> tuple[list[float], list[float]]:
    curve = result.get(name) or {}
    freqs = curve.get("freqs_hz") or []
    mags = curve.get("magnitude_db") or []
    if not isinstance(freqs, list) or not isinstance(mags, list):
        return [], []
    n = min(len(freqs), len(mags))
    return [float(v) for v in freqs[:n]], [float(v) for v in mags[:n]]


def analyze_peaks_nulls(
    bundle: MeasurementBundle,
    *,
    f_min_hz: float = 20.0,
    f_max_hz: float = 350.0,
    peak_threshold_db: float = 3.0,
    null_threshold_db: float = -6.0,
    limit: int = 5,
) -> dict[str, Any]:
    if not bundle.result:
        return {"available": False, "reason": "bundle has no result.json"}
    freqs, measured = _curve(bundle.result, "measured")
    target_freqs, target = _curve(bundle.result, "target")
    if not freqs or not target or len(freqs) != len(target_freqs):
        return {"available": False, "reason": "measured/target curves missing"}

    residuals = []
    for freq, mag, tgt in zip(freqs, measured, target, strict=False):
        if f_min_hz <= freq <= f_max_hz:
            residuals.append((freq, mag - tgt))
    if len(residuals) < 3:
        return {"available": False, "reason": "not enough bass-band points"}

    peaks: list[dict[str, float]] = []
    nulls: list[dict[str, float]] = []
    for idx in range(1, len(residuals) - 1):
        freq, residual = residuals[idx]
        prev_residual = residuals[idx - 1][1]
        next_residual = residuals[idx + 1][1]
        if (
            residual >= peak_threshold_db
            and residual >= prev_residual
            and residual >= next_residual
        ):
            peaks.append({"freq_hz": freq, "residual_db": residual})
        if (
            residual <= null_threshold_db
            and residual <= prev_residual
            and residual <= next_residual
        ):
            nulls.append({"freq_hz": freq, "residual_db": residual})

    peaks.sort(key=lambda p: p["residual_db"], reverse=True)
    nulls.sort(key=lambda p: p["residual_db"])
    return {
        "available": True,
        "band_hz": [f_min_hz, f_max_hz],
        "peak_threshold_db": peak_threshold_db,
        "null_threshold_db": null_threshold_db,
        "peaks": peaks[:limit],
        "nulls": nulls[:limit],
    }


def compute_schroeder(
    *,
    room_volume_m3: float | None = None,
    rt60_s: float | None = None,
) -> dict[str, Any]:
    if not room_volume_m3 or not rt60_s:
        return {
            "available": False,
            "reason": "room volume and RT60 are not in the bundle yet",
        }
    if room_volume_m3 <= 0 or rt60_s <= 0:
        return {"available": False, "reason": "room volume and RT60 must be positive"}
    return {
        "available": True,
        "freq_hz": 2000.0 * math.sqrt(rt60_s / room_volume_m3),
        "room_volume_m3": room_volume_m3,
        "rt60_s": rt60_s,
    }


def _excerpt(text: str, terms: list[str], *, max_chars: int = 360) -> str:
    lower = text.lower()
    hits = [lower.find(term) for term in terms if lower.find(term) >= 0]
    start = min(hits) if hits else 0
    start = max(0, start - 90)
    excerpt = re.sub(r"\s+", " ", text[start:start + max_chars]).strip()
    return excerpt


def look_up(
    query: str,
    *,
    corpus_dir: Path | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    corpus = resolve_corpus_dir(corpus_dir)
    if corpus is None:
        return []
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    if not terms:
        return []

    hits: list[tuple[int, Path, str]] = []
    for path in sorted(corpus.rglob("*.md")):
        text = path.read_text(errors="replace")
        haystack = f"{path.name}\n{text}".lower()
        score = sum(haystack.count(term) for term in terms)
        if score > 0:
            hits.append((score, path, text))
    hits.sort(key=lambda item: item[0], reverse=True)

    out = []
    for score, path, text in hits[:limit]:
        title = path.stem.replace("-", " ")
        for line in text.splitlines():
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                break
        out.append({
            "path": str(path),
            "title": title,
            "score": score,
            "excerpt": _excerpt(text, terms),
        })
    return out


def build_intake(
    bundle: MeasurementBundle,
    *,
    corpus_dir: Path | None = None,
) -> dict[str, Any]:
    summary = get_measurement_summary(bundle)
    peaks_nulls = analyze_peaks_nulls(bundle)
    lookups = []
    for topic in ["measurement quality", "room correction limits"]:
        lookups.extend(look_up(topic, corpus_dir=corpus_dir, limit=2))
    seen_paths: set[str] = set()
    unique_lookups = []
    for hit in lookups:
        if hit["path"] not in seen_paths:
            unique_lookups.append(hit)
            seen_paths.add(hit["path"])

    return {
        "bundle_dir": str(bundle.bundle_dir),
        "summary": summary,
        "peaks_nulls": peaks_nulls,
        "schroeder": compute_schroeder(),
        "corpus_hits": unique_lookups[:4],
        "side_effects": [],
    }
