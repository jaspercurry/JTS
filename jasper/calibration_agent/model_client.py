# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Opt-in model-call adapter for the calibration advisor harness.

The model is a suggestion engine, not an execution engine. This module
only turns a prompt package into a candidate advisor JSON response. The
response still has to pass ``response.validate_advisor_response`` before
any action runner sees it.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jasper.calibration_agent import response as advisor_contract
from jasper.log_event import log_event
from jasper.sound.profile import (
    MAX_PARAMETRIC_BANDS,
    MAX_PROFILE_NAME_CHARS,
    SIMPLE_EQ_FIELDS,
)

logger = logging.getLogger(__name__)

MODEL_CALL_SCHEMA_VERSION = 1
MODEL_CALL_KIND = "jts_advisor_model_call"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_SEC = 60.0
# Per-call output-token budget for the P6 tuning surface. On the
# Responses API a GPT-5-class model's REASONING tokens count against
# max_output_tokens before any visible JSON is emitted — the first live
# check (2026-07-06) saw propose come back status=incomplete at a
# 400-token cap even with `reasoning.effort=low`. 2500 leaves room for
# low-effort reasoning plus the strict-schema JSON while still bounding
# a runaway response to ~$0.025 at list rates. Shared by the paid
# /correction/ endpoints and the live harness so the two cannot drift.
TUNING_LLM_MAX_OUTPUT_TOKENS = 2500
# Model-facing cap on a proposed correction filter set — the widest
# shipped strategy's max_filters. The deterministic validator re-checks
# against the ACTIVE strategy's (possibly tighter) cap.
_CORRECTION_PEQ_MAX_ITEMS = 5

Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]


class AdvisorModelError(RuntimeError):
    """Raised when the advisor model call cannot produce valid JSON."""


@dataclass(frozen=True)
class AdvisorModelSettings:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_OPENAI_BASE_URL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "timeout_sec": self.timeout_sec,
        }


def resolve_settings(
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_sec: float | None = None,
    environ: Mapping[str, str] | None = None,
    api_key: str | None = None,
    default_model: str | None = None,
) -> AdvisorModelSettings:
    """Resolve provider settings without accepting secrets on argv.

    ``api_key`` lets a caller that already resolved the key out-of-band
    (P6's tuning surface reads it from the ``jasper-secrets`` compartment
    file directly) inject it instead of requiring it in ``environ``.
    ``default_model`` is the fallback model id when neither an explicit
    ``model`` nor an env override is set — the tuning surface passes its
    seeded ``JASPER_TUNING_LLM_MODEL`` default here so it never has to be
    operator-supplied, while the calibration-agent CLI leaves it ``None``
    and keeps its "you must name a model" behavior.
    """

    env = environ or os.environ
    resolved_provider = (
        provider
        or env.get("JASPER_CALIBRATION_ADVISOR_PROVIDER")
        or "openai"
    ).strip().lower()
    if resolved_provider != "openai":
        raise AdvisorModelError(
            f"unsupported advisor provider: {resolved_provider or '(missing)'}"
        )

    resolved_key = (api_key or env.get("OPENAI_API_KEY", "")).strip()
    if not resolved_key:
        raise AdvisorModelError("OPENAI_API_KEY is required for --call-advisor")
    api_key = resolved_key

    resolved_model = (
        model
        or env.get("JASPER_CALIBRATION_ADVISOR_MODEL")
        or env.get("OPENAI_ADVISOR_MODEL")
        or (default_model or "")
    ).strip()
    if not resolved_model:
        raise AdvisorModelError(
            "set --advisor-model or JASPER_CALIBRATION_ADVISOR_MODEL"
        )

    base_url = (
        env.get("JASPER_CALIBRATION_ADVISOR_OPENAI_BASE_URL")
        or env.get("OPENAI_BASE_URL")
        or DEFAULT_OPENAI_BASE_URL
    ).rstrip("/")
    resolved_timeout = timeout_sec
    if resolved_timeout is None:
        raw_timeout = env.get("JASPER_CALIBRATION_ADVISOR_TIMEOUT_SEC")
        if raw_timeout:
            try:
                resolved_timeout = float(raw_timeout)
            except ValueError as e:
                raise AdvisorModelError(
                    "JASPER_CALIBRATION_ADVISOR_TIMEOUT_SEC must be numeric"
                ) from e
    if resolved_timeout is None:
        resolved_timeout = DEFAULT_TIMEOUT_SEC
    if resolved_timeout <= 0:
        raise AdvisorModelError("advisor timeout must be positive")

    return AdvisorModelSettings(
        provider=resolved_provider,
        model=resolved_model,
        api_key=api_key,
        base_url=base_url,
        timeout_sec=resolved_timeout,
    )


def call_advisor(
    prompt_package: Mapping[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_sec: float | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    api_key: str | None = None,
    default_model: str | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Call the configured advisor model and return its candidate JSON.

    This function has exactly one external side effect: the provider API
    request. It never applies filters, stores profiles, reads raw audio,
    or logs response content. ``api_key`` / ``default_model`` are for
    callers that resolved the key/model out-of-band (see
    :func:`resolve_settings`); ``max_output_tokens`` caps the response
    length (a budget guard).
    """

    settings = resolve_settings(
        provider=provider,
        model=model,
        timeout_sec=timeout_sec,
        environ=environ,
        api_key=api_key,
        default_model=default_model,
    )
    payload = build_openai_request(
        prompt_package, settings.model, max_output_tokens=max_output_tokens,
    )
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    url = f"{settings.base_url}/responses"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }

    log_event(
        logger,
        "calibration_agent.model_call",
        provider=settings.provider,
        model=settings.model,
        status="started",
    )
    started_at = time.monotonic()
    status, raw_body = (transport or _post_json)(url, headers, body, settings.timeout_sec)
    elapsed_ms = _elapsed_ms(started_at)
    if status < 200 or status >= 300:
        log_event(
            logger,
            "calibration_agent.model_call",
            provider=settings.provider,
            model=settings.model,
            status="http_error",
            http_status=status,
            elapsed_ms=elapsed_ms,
            level=logging.WARNING,
        )
        raise AdvisorModelError(f"advisor provider returned HTTP {status}")

    try:
        provider_response = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AdvisorModelError("advisor provider returned non-JSON response") from e

    provider_status = str(provider_response.get("status") or "")
    if provider_status and provider_status != "completed":
        # Surface WHY when the provider says so — status=incomplete with
        # reason=max_output_tokens is the actionable shape (raise the
        # TUNING_LLM_MAX_OUTPUT_TOKENS budget or lower reasoning effort).
        detail = ""
        incomplete = provider_response.get("incomplete_details")
        if isinstance(incomplete, Mapping) and incomplete.get("reason"):
            detail = f" ({incomplete['reason']})"
        raise AdvisorModelError(
            f"advisor provider response status={provider_status}{detail}"
        )

    text = _extract_response_text(provider_response)
    advisor_response = _loads_json_object(text)
    result = {
        "artifact_schema_version": MODEL_CALL_SCHEMA_VERSION,
        "kind": MODEL_CALL_KIND,
        "provider": settings.provider,
        "model": settings.model,
        "response_id": provider_response.get("id"),
        "provider_status": provider_status or "unknown",
        "elapsed_ms": elapsed_ms,
        "advisor_response": advisor_response,
        "usage": _usage_summary(provider_response.get("usage")),
        "side_effects": ["provider_api_call"],
    }
    log_event(
        logger,
        "calibration_agent.model_call",
        provider=settings.provider,
        model=settings.model,
        status="completed",
        response_id=result["response_id"] or "-",
        elapsed_ms=elapsed_ms,
    )
    return result


def build_openai_request(
    prompt_package: Mapping[str, Any],
    model: str,
    *,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Build a small, replayable Responses API request payload.

    ``max_output_tokens`` caps the response length when set — the P6 live
    harness passes it as a hard per-call budget guard."""

    messages = list(prompt_package.get("messages") or [])
    system = _first_message(messages, "system") or ""
    user = _first_message(messages, "user") or (
        "Review this JTS measurement evidence and produce only JSON."
    )
    advisor_context = prompt_package.get("advisor_context") or {}
    response_contract = prompt_package.get("response_contract") or {}

    user_content = "\n\n".join([
        user,
        "Return only a JSON object matching JTS_RESPONSE_CONTRACT_JSON.",
        (
            "If the provider JSON schema includes fields that do not apply to "
            "an action, use empty strings and a disabled flat profile; JTS "
            "will validate the actual action contract locally."
        ),
        "JTS_ADVISOR_CONTEXT_JSON:",
        json.dumps(advisor_context, separators=(",", ":"), sort_keys=True),
        "JTS_RESPONSE_CONTRACT_JSON:",
        json.dumps(response_contract, separators=(",", ":"), sort_keys=True),
    ])
    payload: dict[str, Any] = {
        "model": model,
        "store": False,
        # Low reasoning effort: the task is bounded JSON against a compact
        # packet, and JTS re-validates + re-simulates everything locally.
        # Default (medium) effort burns output-token budget on reasoning —
        # the live check saw status=incomplete from exactly that. Assumes a
        # reasoning-capable GPT-5-class model (the surface's documented
        # contract for JASPER_TUNING_LLM_MODEL); an incompatible model
        # fails at call time with an honest AdvisorModelError.
        "reasoning": {"effort": "low"},
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "jts_advisor_response",
                "strict": True,
                "schema": _advisor_response_schema(),
            }
        },
    }
    if max_output_tokens is not None and max_output_tokens > 0:
        payload["max_output_tokens"] = int(max_output_tokens)
    return payload


def _first_message(messages: list[Any], role: str) -> str | None:
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != role:
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return None


def _advisor_response_schema() -> dict[str, Any]:
    text = {"type": "string", "maxLength": advisor_contract.TEXT_LIMIT_CHARS}
    simple_eq = {
        "type": "object",
        "properties": {field: {"type": "number"} for field in SIMPLE_EQ_FIELDS},
        "required": list(SIMPLE_EQ_FIELDS),
        "additionalProperties": False,
    }
    band = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "type": {"type": "string"},
            "freq_hz": {"type": "number"},
            "gain_db": {"type": "number"},
            "q": {"type": "number"},
        },
        "required": ["enabled", "type", "freq_hz", "gain_db", "q"],
        "additionalProperties": False,
    }
    profile = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "curve_id": {"type": "string"},
            "simple_eq": simple_eq,
            "parametric_bands": {
                "type": "array",
                "items": band,
                "maxItems": MAX_PARAMETRIC_BANDS,
            },
        },
        "required": ["enabled", "curve_id", "simple_eq", "parametric_bands"],
        "additionalProperties": False,
    }
    correction_peq = {
        "type": "object",
        "properties": {
            "freq_hz": {"type": "number"},
            "q": {"type": "number"},
            "gain_db": {"type": "number"},
        },
        "required": ["freq_hz", "q", "gain_db"],
        "additionalProperties": False,
    }
    action = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": sorted(advisor_contract.ALLOWED_ACTIONS)},
            "message": text,
            "reason": text,
            "position_hint": text,
            "rationale": text,
            "profile_name": {
                "type": "string",
                "maxLength": MAX_PROFILE_NAME_CHARS,
            },
            "profile": profile,
            # P6 correction-scope fields. Not-applicable actions send an
            # empty list / empty string / 0.0 (same "empty when unused"
            # convention as profile above); JTS validates the real action
            # contract locally.
            "correction_peqs": {
                "type": "array",
                "items": correction_peq,
                "maxItems": _CORRECTION_PEQ_MAX_ITEMS,
            },
            "target_id": {"type": "string"},
            "warmth": {"type": "number"},
        },
        "required": [
            "type",
            "message",
            "reason",
            "position_hint",
            "rationale",
            "profile_name",
            "profile",
            "correction_peqs",
            "target_id",
            "warmth",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "artifact_schema_version": {
                "type": "integer",
                "enum": [advisor_contract.RESPONSE_SCHEMA_VERSION],
            },
            "kind": {"type": "string", "enum": ["jts_advisor_response"]},
            "summary": text,
            "recommended_next_action": text,
            "action_plan": {
                "type": "array",
                "items": action,
                "maxItems": advisor_contract.MAX_ACTION_PLAN_ITEMS,
            },
        },
        "required": [
            "artifact_schema_version",
            "kind",
            "summary",
            "recommended_next_action",
            "action_plan",
        ],
        "additionalProperties": False,
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, int(round((time.monotonic() - started_at) * 1000)))


def _post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_sec: float,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()
    except urllib.error.URLError as e:
        raise AdvisorModelError(f"advisor provider request failed: {e.reason}") from e


def _extract_response_text(response_payload: Mapping[str, Any]) -> str:
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    chunks: list[str] = []
    for item in response_payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    combined = "".join(chunks).strip()
    if not combined:
        raise AdvisorModelError("advisor provider response did not include output text")
    return combined


def _loads_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise AdvisorModelError("advisor output was not valid JSON") from e
    if not isinstance(parsed, dict):
        raise AdvisorModelError("advisor output JSON must be an object")
    return parsed


def _usage_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }
    return {key: value[key] for key in allowed if key in value}
