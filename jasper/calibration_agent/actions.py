# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic action runner for validated calibration-advisor plans.

The runner is intentionally boring: it consumes the output of
``jasper.calibration_agent.response.validate_advisor_response`` and
executes only known, validated actions. Model text never reaches DSP or
profile storage directly; future integration points pass explicit
executor callables for reversible auditions and user-approved commits.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from . import response
from ..log_event import log_event

logger = logging.getLogger(__name__)

ACTION_RUN_SCHEMA_VERSION = 1
ACTION_RUN_KIND = "jts_advisor_action_run"

ActionExecutor = Callable[[dict[str, Any]], Mapping[str, Any] | None]


def run_validated_action_plan(
    validation: Mapping[str, Any],
    *,
    audition_executor: ActionExecutor | None = None,
    commit_executor: ActionExecutor | None = None,
) -> dict[str, Any]:
    """Run a validated advisor action plan through deterministic gates.

    The default runner has no external side effects: explain/remeasure
    actions are converted into presentation payloads, while audition and
    commit actions remain pending until a caller supplies an explicit
    executor. This lets the future web/voice surface wire action effects
    through the existing JTS substrates without letting an LLM pick those
    effects ad hoc.
    """

    issues: list[dict[str, Any]] = []
    action_results: list[dict[str, Any]] = []

    if validation.get("kind") != "jts_advisor_response_validation":
        return _run_result(
            accepted=False,
            status="rejected",
            issues=[_issue("fail", "invalid_validation_kind", "invalid validation kind")],
            action_results=[],
        )
    if not validation.get("accepted"):
        return _run_result(
            accepted=False,
            status="rejected",
            issues=[_issue(
                "fail",
                "validation_not_accepted",
                "advisor response validation was not accepted",
            )],
            action_results=[],
        )

    for index, action in enumerate(validation.get("validated_action_plan") or []):
        if not isinstance(action, dict):
            issues.append(_issue(
                "fail",
                "validated_action_not_object",
                "validated actions must be objects",
                index=index,
            ))
            continue
        result = _run_one_action(
            action,
            index=index,
            audition_executor=audition_executor,
            commit_executor=commit_executor,
        )
        action_results.append(result)
        if isinstance(result.get("issue"), dict):
            issues.append(result["issue"])

    status = _overall_status(action_results, issues)
    log_event(
        logger,
        "calibration_agent.action_run",
        status=status,
        actions=len(action_results),
        executed=sum(1 for item in action_results if item.get("executed")),
        pending=sum(1 for item in action_results if item.get("pending")),
    )
    return _run_result(
        accepted=not any(item["severity"] == "fail" for item in issues),
        status=status,
        issues=issues,
        action_results=action_results,
    )


def _run_one_action(
    action: dict[str, Any],
    *,
    index: int,
    audition_executor: ActionExecutor | None,
    commit_executor: ActionExecutor | None,
) -> dict[str, Any]:
    action_type = str(action.get("type") or "")
    execution_ready = bool(action.get("execution_ready"))

    if not execution_ready:
        return {
            "index": index,
            "type": action_type,
            "status": "awaiting_human_confirmation",
            "executed": False,
            "pending": True,
            "side_effect": "none",
            "human_in_loop": _human_loop(
                role="approval_gate",
                prompt="Confirm this change before JTS persists anything.",
                subjective_judgement_required=True,
            ),
        }

    if action_type == response.ACTION_EXPLAIN:
        return {
            "index": index,
            "type": action_type,
            "status": "presented",
            "executed": True,
            "pending": False,
            "side_effect": "none",
            "message": str(action.get("message") or ""),
            "human_in_loop": _human_loop(
                role="read",
                prompt="Review the explanation; no DSP state changed.",
            ),
        }

    if action_type == response.ACTION_REMEASURE:
        return {
            "index": index,
            "type": action_type,
            "status": "presented",
            "executed": True,
            "pending": False,
            "side_effect": "user_prompt_only",
            "reason": str(action.get("reason") or ""),
            "position_hint": action.get("position_hint"),
            "human_in_loop": _human_loop(
                role="operator_next_step",
                prompt="Decide whether to collect the requested evidence.",
            ),
        }

    if action_type == response.ACTION_AUDITION:
        if audition_executor is None:
            return _pending_executor_result(
                action,
                index=index,
                status="ready_for_human_audition",
                executor_name="audition_executor",
                prompt=(
                    "Audition this preference profile through /sound/, compare it "
                    "against the current or neutral profile, and decide what you like."
                ),
            )
        ok, payload, issue = _call_executor(
            audition_executor,
            action,
            index=index,
            action_type=action_type,
        )
        if not ok:
            return _executor_failed_result(action, index=index, issue=issue)
        return {
            "index": index,
            "type": action_type,
            "status": "audition_executed",
            "executed": True,
            "pending": False,
            "side_effect": "ephemeral_audio_state",
            "profile": action.get("profile"),
            "rationale": action.get("rationale"),
            "executor_result": payload,
            "human_in_loop": _human_loop(
                role="listener_judgement",
                prompt=(
                    "Listen to the audition, A/B compare it, and decide whether "
                    "it sounds better to you."
                ),
                subjective_judgement_required=True,
            ),
        }

    if action_type == response.ACTION_COMMIT:
        if commit_executor is None:
            return _pending_executor_result(
                action,
                index=index,
                status="ready_for_user_approved_commit",
                executor_name="commit_executor",
                prompt=(
                    "Save this profile only after the listener confirms the "
                    "audition is preferred."
                ),
            )
        ok, payload, issue = _call_executor(
            commit_executor,
            action,
            index=index,
            action_type=action_type,
        )
        if not ok:
            return _executor_failed_result(action, index=index, issue=issue)
        return {
            "index": index,
            "type": action_type,
            "status": "commit_executed",
            "executed": True,
            "pending": False,
            "side_effect": "persistent_preference_profile",
            "profile_name": action.get("profile_name"),
            "executor_result": payload,
            "human_in_loop": _human_loop(
                role="post_commit_review",
                prompt="Confirm the saved profile remains the preferred sound.",
                subjective_judgement_required=True,
            ),
        }

    return {
        "index": index,
        "type": action_type,
        "status": "unsupported_action",
        "executed": False,
        "pending": False,
        "side_effect": "none",
        "issue": _issue(
            "fail",
            "unsupported_validated_action",
            f"unsupported validated action: {action_type}",
            index=index,
        ),
    }


def _call_executor(
    executor: ActionExecutor,
    action: dict[str, Any],
    *,
    index: int,
    action_type: str,
) -> tuple[bool, dict[str, Any], dict[str, Any] | None]:
    try:
        payload = dict(executor(action) or {})
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "calibration_agent.action_executor",
            result="failed",
            action_type=action_type,
            index=index,
            err=repr(e),
            level=logging.WARNING,
        )
        return False, {}, _issue(
            "fail",
            "action_executor_failed",
            f"{action_type} executor failed",
            index=index,
        )
    return True, payload, None


def _executor_failed_result(
    action: dict[str, Any],
    *,
    index: int,
    issue: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "index": index,
        "type": action.get("type"),
        "status": "executor_failed",
        "executed": False,
        "pending": False,
        "side_effect": "none",
        "issue": issue or _issue(
            "fail",
            "action_executor_failed",
            "action executor failed",
            index=index,
        ),
        "human_in_loop": _human_loop(
            role="operator_recovery",
            prompt="Review the failure before trying the action again.",
        ),
    }


def _pending_executor_result(
    action: dict[str, Any],
    *,
    index: int,
    status: str,
    executor_name: str,
    prompt: str,
) -> dict[str, Any]:
    return {
        "index": index,
        "type": action.get("type"),
        "status": status,
        "executed": False,
        "pending": True,
        "side_effect": "none",
        "required_executor": executor_name,
        "profile": action.get("profile"),
        "rationale": action.get("rationale"),
        "human_in_loop": _human_loop(
            role="listener_judgement",
            prompt=prompt,
            subjective_judgement_required=True,
        ),
    }


def _human_loop(
    *,
    role: str,
    prompt: str,
    subjective_judgement_required: bool = False,
) -> dict[str, Any]:
    return {
        "role": role,
        "prompt": prompt,
        "subjective_judgement_required": subjective_judgement_required,
    }


def _overall_status(
    action_results: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> str:
    if any(item["severity"] == "fail" for item in issues):
        return "rejected"
    if any(result.get("status") == "unsupported_action" for result in action_results):
        return "rejected"
    if any(result.get("pending") for result in action_results):
        return "pending_human"
    return "complete"


def _run_result(
    *,
    accepted: bool,
    status: str,
    issues: list[dict[str, Any]],
    action_results: list[dict[str, Any]],
) -> dict[str, Any]:
    side_effects = [
        str(item["side_effect"])
        for item in action_results
        if item.get("executed")
        and item.get("side_effect") not in {None, "none", "user_prompt_only"}
    ]
    return {
        "artifact_schema_version": ACTION_RUN_SCHEMA_VERSION,
        "kind": ACTION_RUN_KIND,
        "accepted": bool(accepted),
        "status": status,
        "issues": issues,
        "action_results": action_results,
        "side_effects": side_effects,
        "human_in_loop": {
            "required": any(
                (item.get("human_in_loop") or {}).get("subjective_judgement_required")
                for item in action_results
            ),
            "principle": (
                "Preference tuning is subjective; JTS can propose safe options, "
                "but the listener decides what sounds better."
            ),
        },
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
