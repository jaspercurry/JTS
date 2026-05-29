"""LLM-ready, read-only context for calibration-agent review.

This module is the narrow bridge between deterministic bundle evidence
and a future language-model advisor. The raw correction bundle remains
the source of truth, while this packet is the intentionally curated
view an advisor is allowed to see: compact, redacted, versioned, and
explicit about permissions.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jasper.correction import bundles
from jasper.sound import profile as sound_profile

SCHEMA_VERSION = 1
GENERATED_BY = "jasper.calibration_agent.advisor_context.build_advisor_context"

_MIC_ALLOWED_FIELDS = {
    "provider",
    "model",
    "model_key",
    "serial_hash",
    "file_sha256",
    "orientation",
    "sign_convention",
}
_INPUT_ALLOWED_FIELDS = {
    "device_id_hash",
    "requested_device_id_hash",
    "actual_device_id_hash",
    "sample_rate",
    "channel_count",
    "echo_cancellation",
    "noise_suppression",
    "auto_gain_control",
    "capture_transport",
}
_BROWSER_AUDIO_ALLOWED_FIELDS = {
    "available",
    "level",
    "summary",
    "expected_sample_rate",
    "mic_calibrated",
    "failed",
    "warning_count",
    "issues",
}
_PRIVATE_AUDIO_SENSITIVITIES = {"private_audio", "private_raw_audio"}
_PRIVATE_AUDIO_KINDS = {
    "capture_audio",
    "noise_audio",
    "repeat_audio",
    "raw_capture",
    "verify_audio",
}
_PROHIBITED_ACTIONS = (
    {
        "id": "read_raw_audio",
        "label": "may not read raw recordings or request raw audio bytes",
    },
    {
        "id": "emit_camilladsp_yaml",
        "label": "may not emit unconstrained CamillaDSP YAML",
    },
    {
        "id": "apply_filters",
        "label": "may not apply filters or change active DSP state",
    },
    {
        "id": "generate_fir_taps",
        "label": "may not generate FIR taps or convolution coefficients",
    },
    {
        "id": "override_safety_gates",
        "label": "may not override JTS safety or confidence gates",
    },
    {
        "id": "merge_layers",
        "label": "may not silently merge room correction and preference EQ",
    },
)


def _copy_allowed(raw: Any, allowed: set[str]) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(raw, dict):
        return {}, []
    out = {
        key: raw[key]
        for key in sorted(allowed)
        if raw.get(key) is not None
    }
    redacted = sorted(
        key for key in raw
        if key not in allowed and raw.get(key) is not None
    )
    return out, redacted


def _mic_context(mic: Any) -> dict[str, Any]:
    fields, redacted = _copy_allowed(mic, _MIC_ALLOWED_FIELDS)
    return {
        "present": bool(fields),
        "fields": fields,
        "redacted_fields": redacted,
        "raw_serial_redacted": "serial" in redacted,
    }


def _input_context(device: Any, browser_audio: Any) -> dict[str, Any]:
    device_fields, device_redacted = _copy_allowed(device, _INPUT_ALLOWED_FIELDS)
    browser_fields, browser_redacted = _copy_allowed(
        browser_audio,
        _BROWSER_AUDIO_ALLOWED_FIELDS,
    )
    return {
        "present": bool(device_fields or browser_fields),
        "fields": device_fields,
        "browser_audio": browser_fields,
        "redacted_fields": sorted(set(device_redacted + browser_redacted)),
        "browser_labels_redacted": bool(
            {"label", "deviceId", "groupId"} & set(device_redacted)
        ),
    }


def _curve_summary(raw: Any, *, max_points: int = 9) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"available": False}
    freqs = raw.get("freqs_hz")
    mags = raw.get("magnitude_db")
    if not isinstance(freqs, list) or not isinstance(mags, list):
        return {"available": False}
    pairs = []
    for freq, mag in zip(freqs, mags, strict=False):
        try:
            pairs.append((float(freq), float(mag)))
        except (TypeError, ValueError):
            continue
    if not pairs:
        return {"available": False}
    if len(pairs) <= max_points:
        sample = pairs
    else:
        last = len(pairs) - 1
        indexes = sorted({round(i * last / (max_points - 1)) for i in range(max_points)})
        sample = [pairs[int(i)] for i in indexes]
    magnitudes = [mag for _, mag in pairs]
    return {
        "available": True,
        "point_count": len(pairs),
        "freq_min_hz": round(min(freq for freq, _ in pairs), 3),
        "freq_max_hz": round(max(freq for freq, _ in pairs), 3),
        "magnitude_min_db": round(min(magnitudes), 3),
        "magnitude_max_db": round(max(magnitudes), 3),
        "sample_points": [
            {"freq_hz": round(freq, 3), "db": round(mag, 3)}
            for freq, mag in sample
        ],
    }


def _confidence_context(raw: Any) -> dict[str, Any] | None:
    """Return only confidence fields that are safe for an advisor packet."""
    if not isinstance(raw, dict):
        return None
    allowed = (
        "version",
        "level",
        "score",
        "summary",
        "strategy_choice",
        "correction_band_hz",
        "evidence",
        "position_variance",
        "position_bands",
        "feature_flags",
        "strategy_gates",
        "findings",
    )
    return {
        key: raw[key]
        for key in allowed
        if raw.get(key) is not None
    }


def _manifest_context(bundle_dir: Path) -> dict[str, Any]:
    try:
        manifest = bundles.read_artifact_manifest(bundle_dir)
    except bundles.BundleError:
        return {
            "available": False,
            "reason": "artifact_manifest_unavailable",
            "private_audio_count": 0,
            "private_audio_bytes": 0,
        }
    artifacts = [
        item for item in manifest.get("artifacts") or []
        if isinstance(item, dict)
    ]
    private_audio = [
        item for item in artifacts
        if item.get("sensitivity") in _PRIVATE_AUDIO_SENSITIVITIES
        or item.get("kind") in _PRIVATE_AUDIO_KINDS
    ]
    return {
        "available": True,
        "manifest_schema_version": manifest.get("manifest_schema_version"),
        "artifact_count": len(artifacts),
        "debug_safe_artifact_count": sum(
            1 for item in artifacts
            if item.get("sensitivity") == "debug_safe"
        ),
        "private_audio_count": len(private_audio),
        "private_audio_bytes": sum(
            int(item.get("byte_size") or 0)
            for item in private_audio
        ),
        "raw_recordings_excluded": True,
    }


def _load_sound_profile(path: Path | None) -> dict[str, Any]:
    profile_path = path
    if profile_path is None:
        profile_path = Path(
            os.environ.get("JASPER_SOUND_PROFILE_PATH", sound_profile.PROFILE_PATH)
        )
    try:
        exists = profile_path.exists()
        profile = sound_profile.load_profile(profile_path)
    except OSError as e:
        return {"available": False, "reason": str(e)}

    profile_id = profile.profile_id or ""
    profile_name = profile.profile_name or ""
    return {
        "available": exists,
        "enabled": profile.enabled,
        "curve_id": profile.curve_id,
        "simple_eq": profile.simple_eq.to_dict(),
        "parametric_band_count": len(profile.parametric_bands),
        "parametric_bands": [
            band.to_dict() for band in profile.parametric_bands
        ],
        "profile_identity": {
            "kind": "custom" if profile_id else "stock_or_unsaved",
            "profile_id": profile_id or None,
            "profile_name_redacted": bool(profile_name),
        },
        "updated_at": profile.updated_at,
    }


def _safe_doc_path(raw_path: Any) -> str | None:
    if not raw_path:
        return None
    path = Path(str(raw_path))
    parts = path.parts
    for idx in range(len(parts) - 1):
        if parts[idx] == "docs" and parts[idx + 1] == "calibration-agent":
            return Path(*parts[idx:]).as_posix()
    return path.name


def _allowed_action(
    action_id: str,
    label: str,
    *,
    allowed: bool = True,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "label": label,
        "allowed": bool(allowed),
        "reasons": reasons or [],
    }


def _advisor_policy(evidence_packet: dict[str, Any]) -> dict[str, Any]:
    permissions = (
        (evidence_packet.get("capability_permissions") or {}).get("permissions")
        or {}
    )
    safe = permissions.get("safe_peq") or {}
    balanced = permissions.get("balanced_peq") or {}
    bounded_peq_allowed = bool(safe.get("allowed") or balanced.get("allowed"))
    bounded_peq_reasons = sorted(set(
        [str(reason) for reason in safe.get("reasons") or []]
        + [str(reason) for reason in balanced.get("reasons") or []]
    ))
    return {
        "mode": "read_only_advisor",
        "allowed_actions": [
            _allowed_action(
                "explain",
                "may explain collected evidence and its confidence",
            ),
            _allowed_action(
                "recommend_remeasure",
                "may recommend remeasurement or missing evidence collection",
            ),
            _allowed_action(
                "suggest_bounded_peq_strategy",
                "may suggest bounded PEQ strategy within JTS confidence gates",
                allowed=bounded_peq_allowed,
                reasons=bounded_peq_reasons,
            ),
        ],
        "prohibited_actions": list(_PROHIBITED_ACTIONS),
        "capability_permissions": permissions,
    }


def _design_context(result: dict[str, Any] | None, info: dict[str, Any]) -> dict[str, Any]:
    result = result or {}
    design = result.get("design_report") or info.get("design_report") or {}
    strategy = (
        result.get("correction_strategy")
        or info.get("correction_strategy")
        or design.get("correction_strategy")
    )
    peqs = result.get("peqs") or info.get("peqs") or []
    return {
        "room_strategy": strategy,
        "proposed_filter_count": len(peqs) if isinstance(peqs, list) else 0,
        "target_curve": {
            "choice": info.get("target_choice"),
            "profile": info.get("target_profile"),
            "curve_summary": _curve_summary(result.get("target")),
        },
        "measured_curve_summary": _curve_summary(result.get("measured")),
        "predicted_curve_summary": _curve_summary(result.get("predicted")),
    }


def build_advisor_context(
    *,
    bundle_dir: Path,
    info: dict[str, Any],
    result: dict[str, Any] | None,
    evidence_packet: dict[str, Any],
    peaks_nulls: dict[str, Any],
    schroeder: dict[str, Any] | None = None,
    corpus_hits: list[dict[str, Any]] | None = None,
    sound_profile_path: Path | None = None,
) -> dict[str, Any]:
    """Build the redacted packet a future LLM advisor may consume."""
    measurement = evidence_packet.get("measurement") or {}
    position = evidence_packet.get("position_analysis") or {}
    acoustic = evidence_packet.get("acoustic_quality") or {}
    runtime = evidence_packet.get("runtime_integrity") or {}
    repeatability = evidence_packet.get("repeatability") or {}

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": "llm_ready_advisor_context",
        "generated_by": GENERATED_BY,
        "privacy": {
            "debug_safe": True,
            "raw_audio_excluded": True,
            "absolute_paths_excluded": True,
            "untrusted_browser_labels_redacted": True,
            "secrets_excluded": True,
        },
        "bundle": {
            "session_id": evidence_packet.get("session_id") or info.get("session_id"),
            "state": (evidence_packet.get("bundle") or {}).get("state"),
            "schema_version": (evidence_packet.get("bundle") or {}).get(
                "schema_version"
            ),
            "has_result": (evidence_packet.get("bundle") or {}).get("has_result"),
            "has_artifact_manifest": (evidence_packet.get("bundle") or {}).get(
                "has_artifact_manifest"
            ),
            "validation_issues": (evidence_packet.get("bundle") or {}).get("issues")
            or [],
            "artifact_manifest": _manifest_context(bundle_dir),
        },
        "advisor_policy": _advisor_policy(evidence_packet),
        "measurement": {
            "positions_completed": measurement.get("positions_completed"),
            "positions_requested": measurement.get("positions_requested"),
            "verify_available": bool((result or {}).get("verify")),
            "mic_calibration": _mic_context(measurement.get("mic_calibration")),
            "input_device": _input_context(
                measurement.get("input_device"),
                measurement.get("browser_audio"),
            ),
        },
        "quality": {
            "confidence": _confidence_context(evidence_packet.get("confidence")),
            "acoustic_summary": acoustic.get("summary") or {},
            "acoustic_issues": acoustic.get("issues") or [],
            "runtime_summary": runtime.get("summary") or {},
            "runtime_issues": runtime.get("issues") or [],
            "repeatability": repeatability,
            "spatial_spread": {
                "available": position.get("available"),
                "position_count": position.get("position_count"),
                "bands": position.get("bands") or [],
                "feature_flags": position.get("feature_flags") or [],
            },
        },
        "correction": {
            **_design_context(result, info),
            "rejected_or_caution_features": position.get("feature_flags") or [],
            "bass_residual": peaks_nulls,
            "schroeder": schroeder or {"available": False},
        },
        "preference": {
            "current_sound_profile": _load_sound_profile(sound_profile_path),
        },
        "evidence_gaps": evidence_packet.get("missing_evidence") or [],
        "agent_readiness": evidence_packet.get("agent_readiness") or {},
        "corpus": {
            "hits": [
                {
                    "title": hit.get("title"),
                    "path": _safe_doc_path(hit.get("path")),
                    "excerpt": hit.get("excerpt"),
                }
                for hit in (corpus_hits or [])[:6]
                if isinstance(hit, dict)
            ],
        },
        "side_effects": [],
    }
