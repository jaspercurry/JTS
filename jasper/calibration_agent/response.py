"""Deterministic response contract for the future calibration advisor.

The model is allowed to propose bounded actions, but this module is
the gate. It rejects unsafe payloads, normalizes preference-EQ plans
through the same sound-profile substrate used by ``/sound/``, and
marks persistence as user-confirmation-only.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from jasper.sound.profile import (
    ADVANCED_GAIN_LIMIT_DB,
    CURVE_PRESETS,
    MAX_FREQ_HZ,
    MAX_PARAMETRIC_BANDS,
    MAX_Q,
    MAX_PROFILE_NAME_CHARS,
    MIN_FREQ_HZ,
    MIN_Q,
    SIMPLE_EQ_FIELDS,
    SIMPLE_EQ_LIMIT_DB,
    ParametricBand,
    SoundProfile,
    build_sound_filters,
    estimate_headroom_db,
)

RESPONSE_SCHEMA_VERSION = 1
VALIDATION_SCHEMA_VERSION = 1

ACTION_EXPLAIN = "explain"
ACTION_REMEASURE = "recommend_remeasure"
ACTION_AUDITION = "propose_preference_eq_audition"
ACTION_COMMIT = "request_user_approved_preference_commit"

ALLOWED_ACTIONS = {
    ACTION_EXPLAIN,
    ACTION_REMEASURE,
    ACTION_AUDITION,
    ACTION_COMMIT,
}

_CURVE_IDS = {preset.id for preset in CURVE_PRESETS}
_BAND_TYPES = {"Peaking", "Lowshelf", "Highshelf"}
_BAND_TYPE_ALIASES = {
    "peaking": "Peaking",
    "peak": "Peaking",
    "lowshelf": "Lowshelf",
    "low_shelf": "Lowshelf",
    "highshelf": "Highshelf",
    "high_shelf": "Highshelf",
}
_PROHIBITED_KEYS = {
    "audio_bytes",
    "camilladsp_config",
    "camilladsp_yaml",
    "coefficients",
    "command",
    "dsp_yaml",
    "execute",
    "fir_coefficients",
    "fir_taps",
    "raw_audio",
    "shell",
    "set_config_file_path",
    "set_volume",
    "volume",
    "volume_db",
    "yaml",
}
_TEXT_LIMIT_CHARS = 1_200


def response_contract() -> dict[str, Any]:
    """Return the versioned JSON contract given to the model."""

    return {
        "artifact_schema_version": RESPONSE_SCHEMA_VERSION,
        "kind": "jts_advisor_response_contract",
        "required_top_level": {
            "artifact_schema_version": RESPONSE_SCHEMA_VERSION,
            "kind": "jts_advisor_response",
        },
        "allowed_action_types": [
            {
                "type": ACTION_EXPLAIN,
                "side_effect": "none",
                "required_fields": ["message"],
            },
            {
                "type": ACTION_REMEASURE,
                "side_effect": "user_prompt_only",
                "required_fields": ["reason"],
            },
            {
                "type": ACTION_AUDITION,
                "side_effect": "ephemeral_audio_state",
                "required_fields": ["profile", "rationale"],
                "execution": (
                    "JTS may load this through the existing /sound/ audition "
                    "path after deterministic validation; it is not persisted."
                ),
            },
            {
                "type": ACTION_COMMIT,
                "side_effect": "persistent_preference_profile",
                "required_fields": ["profile", "profile_name", "rationale"],
                "execution": (
                    "JTS may save this only after explicit user confirmation. "
                    "The model cannot self-confirm."
                ),
            },
        ],
        "preference_eq_limits": {
            "simple_gain_db": SIMPLE_EQ_LIMIT_DB,
            "advanced_gain_db": ADVANCED_GAIN_LIMIT_DB,
            "max_parametric_bands": MAX_PARAMETRIC_BANDS,
            "min_freq_hz": MIN_FREQ_HZ,
            "max_freq_hz": MAX_FREQ_HZ,
            "min_q": MIN_Q,
            "max_q": MAX_Q,
            "curve_ids": sorted(_CURVE_IDS),
        },
        "preference_profile_shape": {
            "model_owned_fields": [
                "enabled",
                "curve_id",
                "simple_eq",
                "parametric_bands",
            ],
            "jts_owned_fields": [
                "profile_id",
                "profile_name",
                "updated_at",
            ],
        },
        "prohibited": sorted(_PROHIBITED_KEYS),
    }


def validate_advisor_response(
    raw: Any,
    *,
    advisor_context: dict[str, Any],
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Validate a proposed advisor response without executing it."""

    issues: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    if not isinstance(raw, dict):
        return _validation_result(
            accepted=False,
            issues=[_issue("fail", "response_not_object", "response must be JSON object")],
            actions=[],
        )

    if raw.get("artifact_schema_version") != RESPONSE_SCHEMA_VERSION:
        issues.append(_issue(
            "fail",
            "unsupported_response_schema",
            f"expected artifact_schema_version={RESPONSE_SCHEMA_VERSION}",
        ))
    if raw.get("kind") != "jts_advisor_response":
        issues.append(_issue(
            "fail",
            "unsupported_response_kind",
            "expected kind='jts_advisor_response'",
        ))

    prohibited = sorted(set(_find_prohibited_keys(raw)))
    if prohibited:
        issues.append(_issue(
            "fail",
            "prohibited_fields_present",
            "response contains fields the model may not control",
            fields=prohibited,
        ))

    action_plan = raw.get("action_plan") or []
    if not isinstance(action_plan, list):
        issues.append(_issue("fail", "action_plan_not_list", "action_plan must be a list"))
        action_plan = []
    if len(action_plan) > 6:
        issues.append(_issue(
            "fail",
            "too_many_actions",
            "action_plan is limited to 6 actions",
            count=len(action_plan),
        ))
        action_plan = action_plan[:6]

    for index, action in enumerate(action_plan):
        action_issues, normalized = _validate_action(
            action,
            index=index,
            advisor_context=advisor_context,
            user_confirmed=user_confirmed,
        )
        issues.extend(action_issues)
        if normalized is not None:
            actions.append(normalized)

    for field in ("summary", "recommended_next_action"):
        if raw.get(field) is not None and not isinstance(raw[field], str):
            issues.append(_issue(
                "fail",
                f"{field}_not_string",
                f"{field} must be a string when present",
            ))
        elif isinstance(raw.get(field), str) and len(raw[field]) > _TEXT_LIMIT_CHARS:
            issues.append(_issue(
                "fail",
                f"{field}_too_long",
                f"{field} must be <= {_TEXT_LIMIT_CHARS} characters",
            ))

    return _validation_result(
        accepted=not any(item["severity"] == "fail" for item in issues),
        issues=issues,
        actions=actions,
    )


def _validation_result(
    *,
    accepted: bool,
    issues: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "artifact_schema_version": VALIDATION_SCHEMA_VERSION,
        "kind": "jts_advisor_response_validation",
        "accepted": bool(accepted),
        "issues": issues,
        "validated_action_plan": actions if accepted else [],
        "side_effects": [],
    }


def _issue(
    severity: str,
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        **extra,
    }


def _find_prohibited_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _PROHIBITED_KEYS:
                yield normalized
            yield from _find_prohibited_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _find_prohibited_keys(child)


def _validate_action(
    raw: Any,
    *,
    index: int,
    advisor_context: dict[str, Any],
    user_confirmed: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    issues: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return [
            _issue("fail", "action_not_object", "action must be an object", index=index)
        ], None

    action_type = str(raw.get("type") or raw.get("action") or "").strip()
    if action_type not in ALLOWED_ACTIONS:
        return [
            _issue(
                "fail",
                "unknown_action_type",
                f"unknown action type: {action_type or '(missing)'}",
                index=index,
            )
        ], None

    allowed, reasons = _policy_allows(advisor_context, action_type)
    if not allowed:
        return [
            _issue(
                "fail",
                "action_not_allowed_by_context",
                f"{action_type} is blocked by the current JTS confidence policy",
                index=index,
                reasons=reasons,
            )
        ], None

    if action_type == ACTION_EXPLAIN:
        message = _bounded_text(raw.get("message"), "message", index, issues)
        return issues, {
            "type": ACTION_EXPLAIN,
            "status": "ready",
            "side_effect": "none",
            "execution_ready": True,
            "message": message,
        } if not issues else None

    if action_type == ACTION_REMEASURE:
        reason = _bounded_text(raw.get("reason"), "reason", index, issues)
        hint = _optional_bounded_text(raw.get("position_hint"), "position_hint", issues)
        return issues, {
            "type": ACTION_REMEASURE,
            "status": "ready",
            "side_effect": "user_prompt_only",
            "execution_ready": True,
            "reason": reason,
            "position_hint": hint,
        } if not issues else None

    profile_issues, profile_payload = _validate_profile(raw.get("profile"), index=index)
    issues.extend(profile_issues)
    rationale = _bounded_text(raw.get("rationale"), "rationale", index, issues)
    if issues:
        return issues, None

    if action_type == ACTION_AUDITION:
        return issues, {
            "type": ACTION_AUDITION,
            "status": "ready_for_ephemeral_audition",
            "side_effect": "ephemeral_audio_state",
            "execution_ready": True,
            "requires_user_confirmation": False,
            "rationale": rationale,
            **profile_payload,
        }

    profile_name = _bounded_profile_name(raw.get("profile_name"), issues, index=index)
    status = (
        "ready_for_user_approved_commit"
        if user_confirmed
        else "awaiting_user_confirmation"
    )
    return issues, {
        "type": ACTION_COMMIT,
        "status": status,
        "side_effect": "persistent_preference_profile",
        "execution_ready": bool(user_confirmed),
        "requires_user_confirmation": True,
        "user_confirmed": bool(user_confirmed),
        "profile_name": profile_name,
        "rationale": rationale,
        **profile_payload,
    }


def _policy_allows(
    advisor_context: dict[str, Any],
    action_type: str,
) -> tuple[bool, list[str]]:
    policy = advisor_context.get("advisor_policy") or {}
    actions = {
        action.get("id"): action
        for action in policy.get("allowed_actions") or []
        if isinstance(action, dict)
    }
    policy_id = {
        ACTION_EXPLAIN: "explain",
        ACTION_REMEASURE: "recommend_remeasure",
        ACTION_AUDITION: "propose_preference_eq_audition",
        ACTION_COMMIT: "request_user_approved_preference_commit",
    }.get(action_type, action_type)
    payload = actions.get(policy_id)
    if payload is None and action_type == ACTION_AUDITION:
        payload = actions.get("suggest_bounded_peq_strategy")
    if not payload:
        return False, ["advisor policy does not list this action"]
    return bool(payload.get("allowed")), [str(r) for r in payload.get("reasons") or []]


def _bounded_text(
    value: Any,
    field: str,
    index: int,
    issues: list[dict[str, Any]],
) -> str:
    if not isinstance(value, str) or not value.strip():
        issues.append(_issue(
            "fail",
            f"{field}_missing",
            f"{field} is required",
            index=index,
        ))
        return ""
    text = " ".join(value.split())
    if len(text) > _TEXT_LIMIT_CHARS:
        issues.append(_issue(
            "fail",
            f"{field}_too_long",
            f"{field} must be <= {_TEXT_LIMIT_CHARS} characters",
            index=index,
        ))
        return ""
    return text


def _optional_bounded_text(
    value: Any,
    field: str,
    issues: list[dict[str, Any]],
) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        issues.append(_issue(
            "fail",
            f"{field}_not_string",
            f"{field} must be a string when present",
        ))
        return None
    text = " ".join(value.split())
    if len(text) > _TEXT_LIMIT_CHARS:
        issues.append(_issue(
            "fail",
            f"{field}_too_long",
            f"{field} must be <= {_TEXT_LIMIT_CHARS} characters",
        ))
        return None
    return text


def _bounded_profile_name(
    value: Any,
    issues: list[dict[str, Any]],
    *,
    index: int,
) -> str:
    if not isinstance(value, str) or not value.strip():
        issues.append(_issue(
            "fail",
            "profile_name_missing",
            "profile_name is required for persistent commits",
            index=index,
        ))
        return ""
    name = " ".join(value.split())
    if len(name) > MAX_PROFILE_NAME_CHARS:
        issues.append(_issue(
            "fail",
            "profile_name_too_long",
            f"profile_name must be <= {MAX_PROFILE_NAME_CHARS} characters",
            index=index,
        ))
        return ""
    return name


def _validate_profile(raw: Any, *, index: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return [
            _issue("fail", "profile_not_object", "profile must be an object", index=index)
        ], {}

    curve_id = str(raw.get("curve_id", raw.get("curve", "flat"))).strip()
    if curve_id not in _CURVE_IDS:
        issues.append(_issue(
            "fail",
            "curve_id_invalid",
            "curve_id must be one of the stock JTS curves",
            index=index,
            allowed=sorted(_CURVE_IDS),
        ))

    simple = raw.get("simple_eq", raw)
    if isinstance(simple, dict):
        for field in SIMPLE_EQ_FIELDS:
            if field in simple:
                _validate_range(
                    simple[field],
                    field,
                    -SIMPLE_EQ_LIMIT_DB,
                    SIMPLE_EQ_LIMIT_DB,
                    issues,
                    index=index,
                )
    elif simple is not raw:
        issues.append(_issue(
            "fail",
            "simple_eq_not_object",
            "simple_eq must be an object",
            index=index,
        ))

    bands = raw.get("parametric_bands", raw.get("bands", []))
    if not isinstance(bands, list):
        issues.append(_issue(
            "fail",
            "parametric_bands_not_list",
            "parametric_bands must be a list",
            index=index,
        ))
        bands = []
    if len(bands) > MAX_PARAMETRIC_BANDS:
        issues.append(_issue(
            "fail",
            "too_many_parametric_bands",
            f"parametric_bands is limited to {MAX_PARAMETRIC_BANDS}",
            index=index,
            count=len(bands),
        ))

    for band_index, band in enumerate(bands[:MAX_PARAMETRIC_BANDS]):
        _validate_band(band, issues, index=index, band_index=band_index)

    if issues:
        return issues, {}

    profile = SoundProfile.from_mapping(raw)
    return [], {
        "profile": _profile_dsp_shape(profile),
        "headroom_db": estimate_headroom_db(profile),
        "sound_filter_count": len(build_sound_filters(profile)),
    }


def _profile_dsp_shape(profile: SoundProfile) -> dict[str, Any]:
    """Return only the DSP shape the model is allowed to propose."""

    return {
        "enabled": profile.enabled,
        "curve_id": profile.curve_id,
        "simple_eq": profile.simple_eq.to_dict(),
        "parametric_bands": [band.to_dict() for band in profile.parametric_bands],
    }


def _validate_band(
    raw: Any,
    issues: list[dict[str, Any]],
    *,
    index: int,
    band_index: int,
) -> None:
    if not isinstance(raw, dict):
        issues.append(_issue(
            "fail",
            "parametric_band_not_object",
            "each parametric band must be an object",
            index=index,
            band_index=band_index,
        ))
        return

    kind = str(raw.get("type", raw.get("biquad_type", "Peaking"))).strip()
    normalized = _BAND_TYPE_ALIASES.get(kind.lower(), kind)
    if normalized not in _BAND_TYPES:
        issues.append(_issue(
            "fail",
            "parametric_band_type_invalid",
            "parametric band type must be Peaking, Lowshelf, or Highshelf",
            index=index,
            band_index=band_index,
        ))
    _validate_range(
        raw.get("freq_hz", raw.get("freq", ParametricBand().freq_hz)),
        "freq_hz",
        MIN_FREQ_HZ,
        MAX_FREQ_HZ,
        issues,
        index=index,
        band_index=band_index,
    )
    _validate_range(
        raw.get("gain_db", raw.get("gain", 0.0)),
        "gain_db",
        -ADVANCED_GAIN_LIMIT_DB,
        ADVANCED_GAIN_LIMIT_DB,
        issues,
        index=index,
        band_index=band_index,
    )
    _validate_range(
        raw.get("q", 1.0),
        "q",
        MIN_Q,
        MAX_Q,
        issues,
        index=index,
        band_index=band_index,
    )


def _validate_range(
    value: Any,
    field: str,
    lo: float,
    hi: float,
    issues: list[dict[str, Any]],
    *,
    index: int,
    band_index: int | None = None,
) -> None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        issues.append(_issue(
            "fail",
            f"{field}_not_number",
            f"{field} must be numeric",
            index=index,
            band_index=band_index,
        ))
        return
    if not math.isfinite(numeric) or not lo <= numeric <= hi:
        issues.append(_issue(
            "fail",
            f"{field}_out_of_range",
            f"{field} must be between {lo:g} and {hi:g}",
            index=index,
            band_index=band_index,
            value=numeric if math.isfinite(numeric) else str(numeric),
        ))
