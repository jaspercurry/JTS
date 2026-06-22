# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Safety-session substrate for future active-speaker test tones.

This module deliberately does not generate audio, load CamillaDSP configs, or
touch hardware. It only owns the durable state machine that future playback
code must pass through before any sound-emitting action is added.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from calendar import timegm
from pathlib import Path
from typing import Any, Callable

from ._common import issue as _issue
from .calibration_level import MIN_TEST_LEVEL_DBFS

SCHEMA_VERSION = 1
SAFE_PLAYBACK_SESSION_KIND = "jts_active_speaker_safe_playback_session"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_safe_playback.json")
DEFAULT_ARM_TTL_SEC = 120
QUIET_START_POLICY_VERSION = "floor_first_per_target_v1"
FLOOR_OPERATOR_OUTCOMES = frozenset({
    "heard_correct_driver",
    "heard_wrong_driver",
    "too_loud",
    "silent",
})

NowFn = Callable[[], float]


def _utc_from_epoch(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _now() -> float:
    return time.time()


def _state_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE")
        or DEFAULT_STATE_PATH
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _base_state(*, now_epoch: float) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": SAFE_PLAYBACK_SESSION_KIND,
        "status": "idle",
        "session_id": None,
        "playback_allowed": False,
        "tone_playback_implemented": False,
        "created_at": None,
        "updated_at": _utc_from_epoch(now_epoch),
        "expires_at": None,
        "last_action": "status",
        "environment": {},
        "quiet_start": _quiet_start_base(),
        "playback": {
            "status": "idle",
            "audio_emitted": False,
        },
        "issues": [],
    }


def _environment_summary(report: dict[str, Any]) -> dict[str, Any]:
    config = report.get("camilla_config") or {}
    safe = report.get("safe_playback") or {}
    config_path = config.get("path")
    return {
        "status": report.get("status"),
        "load_gate": report.get("load_gate"),
        "ok_to_load_active_config": bool(report.get("ok_to_load_active_config")),
        "camilla_classification": config.get("classification"),
        "camilla_config_name": Path(str(config_path)).name if config_path else None,
        "safe_playback_status": safe.get("status"),
        "safe_playback_allowed": bool(safe.get("playback_allowed")),
    }


def _nonnegative_int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def playback_target_signature(target: Any) -> dict[str, Any] | None:
    """Return the stable target identity used by the quiet-start gate."""

    if not isinstance(target, dict):
        return None
    group_id = str(target.get("speaker_group_id") or "").strip() or None
    role = str(target.get("driver_role") or target.get("role") or "").strip().lower()
    output_index = _nonnegative_int(target.get("output_index"))
    if output_index is None:
        output_index = _nonnegative_int(target.get("physical_output_index"))
    if not (group_id or role or output_index is not None):
        return None
    return {
        "speaker_group_id": group_id,
        "role": role or None,
        "output_index": output_index,
    }


def _quiet_start_base() -> dict[str, Any]:
    return {
        "policy_version": QUIET_START_POLICY_VERSION,
        "status": "floor_required",
        "floor_audio_confirmed": False,
        "current_target": None,
        "last_level_dbfs": None,
        "last_playback_at": None,
        "pending_playback_id": None,
        "last_operator_result": None,
    }


def _normalise_quiet_start(raw: Any) -> dict[str, Any]:
    quiet = _quiet_start_base()
    if isinstance(raw, dict):
        quiet.update({
            key: raw.get(key)
            for key in quiet
            if key in raw
        })
    quiet["policy_version"] = QUIET_START_POLICY_VERSION
    status = str(quiet.get("status") or "floor_required")
    quiet["floor_audio_confirmed"] = bool(quiet.get("floor_audio_confirmed"))
    if status == "floor_confirmed" and quiet["floor_audio_confirmed"]:
        quiet["status"] = "floor_confirmed"
    elif status == "floor_pending_operator":
        quiet["status"] = "floor_pending_operator"
        quiet["floor_audio_confirmed"] = False
    else:
        quiet["status"] = "floor_required"
        quiet["floor_audio_confirmed"] = False
    quiet["current_target"] = playback_target_signature(quiet.get("current_target"))
    quiet["last_level_dbfs"] = _finite_float(quiet.get("last_level_dbfs"))
    pending = quiet.get("pending_playback_id")
    quiet["pending_playback_id"] = str(pending) if pending else None
    if not isinstance(quiet.get("last_operator_result"), dict):
        quiet["last_operator_result"] = None
    return quiet


def floor_audio_confirmed_for_target(
    safe_session: dict[str, Any],
    target: Any,
) -> bool:
    """Return whether this armed session has floor audio evidence for target."""

    quiet = _normalise_quiet_start(safe_session.get("quiet_start"))
    target_sig = playback_target_signature(target)
    return (
        safe_session.get("status") == "armed"
        and bool(target_sig)
        and quiet.get("status") == "floor_confirmed"
        and quiet.get("floor_audio_confirmed") is True
        and quiet.get("current_target") == target_sig
    )


def floor_audio_retry_allowed_for_target(
    safe_session: dict[str, Any],
    target: Any,
) -> bool:
    """Return whether a same-target silent floor test may be retried louder."""

    quiet = _normalise_quiet_start(safe_session.get("quiet_start"))
    target_sig = playback_target_signature(target)
    last_result = quiet.get("last_operator_result")
    return (
        safe_session.get("status") == "armed"
        and bool(target_sig)
        and isinstance(last_result, dict)
        and last_result.get("outcome") == "silent"
        and quiet.get("current_target") == target_sig
        and playback_target_signature(last_result.get("target")) == target_sig
    )


def _normalise_state(raw: Any, *, now_epoch: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _base_state(now_epoch=now_epoch)
    state = _base_state(now_epoch=now_epoch)
    state.update(raw)
    state["playback_allowed"] = False
    state["tone_playback_implemented"] = False
    state.setdefault("issues", [])
    state.setdefault("playback", {"status": "idle", "audio_emitted": False})
    state["quiet_start"] = _normalise_quiet_start(state.get("quiet_start"))
    return state


def load_safe_playback_state(
    *,
    state_path: str | Path | None = None,
    now: NowFn = _now,
) -> dict[str, Any]:
    """Return the current safe-playback session state.

    Expiry is reported as derived state but not persisted. That keeps status
    reads side-effect-free while still preventing stale armed sessions from
    looking usable to callers.
    """

    now_epoch = now()
    path = _state_path(state_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _base_state(now_epoch=now_epoch)

    state = _normalise_state(raw, now_epoch=now_epoch)
    expires_at = state.get("expires_at")
    if state.get("status") == "armed" and isinstance(expires_at, str):
        try:
            expires_epoch = timegm(
                time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ")
            )
        except ValueError:
            expires_epoch = now_epoch
        if expires_epoch <= now_epoch:
            state["status"] = "expired"
            state["playback_allowed"] = False
            state["quiet_start"] = _quiet_start_base()
    return state


def arm_safe_playback_session(
    environment_report: dict[str, Any],
    *,
    state_path: str | Path | None = None,
    now: NowFn = _now,
    ttl_sec: int = DEFAULT_ARM_TTL_SEC,
) -> dict[str, Any]:
    """Create an armed no-audio safety session when environment gates pass."""

    now_epoch = now()
    path = _state_path(state_path)
    env = _environment_summary(environment_report)
    issues: list[dict[str, str]] = []
    if not env["ok_to_load_active_config"]:
        issues.append(
            _issue(
                "blocker",
                "active_environment_not_ready",
                "active-speaker environment load gate is not ready",
            )
        )
    if (environment_report.get("safe_playback") or {}).get("playback_allowed"):
        issues.append(
            _issue(
                "blocker",
                "unexpected_playback_permission",
                "environment report unexpectedly allowed playback in a no-audio build",
            )
        )
    issues.extend(
        issue
        for issue in environment_report.get("issues", [])
        if isinstance(issue, dict)
    )

    if issues:
        state = _base_state(now_epoch=now_epoch)
        state.update(
            {
                "status": "blocked",
                "last_action": "arm_blocked",
                "environment": env,
                "issues": issues,
            }
        )
        _atomic_write_json(path, state)
        return state

    expires_epoch = now_epoch + max(1, int(ttl_sec))
    state = _base_state(now_epoch=now_epoch)
    state.update(
        {
            "status": "armed",
            "session_id": uuid.uuid4().hex,
            "created_at": _utc_from_epoch(now_epoch),
            "expires_at": _utc_from_epoch(expires_epoch),
            "last_action": "arm",
            "environment": env,
            "quiet_start": _quiet_start_base(),
            "issues": [],
        }
    )
    _atomic_write_json(path, state)
    return state


def stop_safe_playback_session(
    *,
    reason: str = "operator_stop",
    state_path: str | Path | None = None,
    now: NowFn = _now,
) -> dict[str, Any]:
    """Idempotently stop any active safe-playback session."""

    now_epoch = now()
    prior = load_safe_playback_state(state_path=state_path, now=now)
    state = _base_state(now_epoch=now_epoch)
    state.update(
        {
            "status": "stopped" if prior.get("session_id") else "idle",
            "session_id": prior.get("session_id"),
            "created_at": prior.get("created_at"),
            "updated_at": _utc_from_epoch(now_epoch),
            "expires_at": None,
            "last_action": reason or "operator_stop",
            "environment": prior.get("environment") or {},
            "quiet_start": _quiet_start_base(),
            "playback": {
                "status": "stopped" if prior.get("session_id") else "idle",
                "playback_id": (prior.get("playback") or {}).get("playback_id"),
                "audio_emitted": False,
                "reason": reason or "operator_stop",
            },
            "issues": [],
        }
    )
    _atomic_write_json(_state_path(state_path), state)
    return state


def record_safe_playback_result(
    result: dict[str, Any],
    *,
    state_path: str | Path | None = None,
    now: NowFn = _now,
) -> dict[str, Any]:
    """Persist the latest no-audio tone-backend lifecycle result."""

    now_epoch = now()
    prior = load_safe_playback_state(state_path=state_path, now=now)
    playback = {
        "status": result.get("status") or "unknown",
        "backend": result.get("backend"),
        "playback_id": result.get("playback_id"),
        "audio_emitted": bool(result.get("audio_emitted")),
        "target": result.get("target"),
        "tone": result.get("tone"),
        "artifact": _artifact_summary(result.get("artifact")),
        "issue_count": len(result.get("issues") or []),
    }
    state = _normalise_state(prior, now_epoch=now_epoch)
    quiet_start = _quiet_start_after_result(
        state.get("quiet_start"),
        result,
        now_epoch=now_epoch,
    )
    state.update(
        {
            "updated_at": _utc_from_epoch(now_epoch),
            "last_action": f"tone_playback_{playback['status']}",
            "playback": playback,
            "quiet_start": quiet_start,
        }
    )
    _atomic_write_json(_state_path(state_path), state)
    return state


def _quiet_start_after_result(
    prior: Any,
    result: dict[str, Any],
    *,
    now_epoch: float,
) -> dict[str, Any]:
    quiet = _normalise_quiet_start(prior)
    completed_audio = (
        result.get("status") == "completed"
        and bool(result.get("audio_emitted"))
        and not result.get("issues")
    )
    if quiet.get("status") == "floor_pending_operator" and not completed_audio:
        return quiet

    target_sig = playback_target_signature(result.get("target"))
    if target_sig and quiet.get("current_target") != target_sig:
        quiet = _quiet_start_base()
        quiet["current_target"] = target_sig
    elif target_sig and not quiet.get("current_target"):
        quiet["current_target"] = target_sig

    tone = result.get("tone") if isinstance(result.get("tone"), dict) else {}
    level = _finite_float(tone.get("level_dbfs"))
    if level is not None:
        quiet["last_level_dbfs"] = level

    if completed_audio:
        quiet["last_playback_at"] = _utc_from_epoch(now_epoch)
        if (
            quiet.get("status") == "floor_pending_operator"
            and target_sig
            and quiet.get("current_target") == target_sig
            and not quiet.get("floor_audio_confirmed")
        ):
            quiet["status"] = "floor_pending_operator"
            quiet["floor_audio_confirmed"] = False
            quiet["pending_playback_id"] = result.get("playback_id")
            quiet["last_operator_result"] = None
        elif _level_at_floor(level):
            quiet["status"] = "floor_pending_operator"
            quiet["floor_audio_confirmed"] = False
            quiet["pending_playback_id"] = result.get("playback_id")
            quiet["last_operator_result"] = None
        elif floor_audio_retry_allowed_for_target(
            {"status": "armed", "quiet_start": quiet},
            {
                "speaker_group_id": target_sig.get("speaker_group_id"),
                "driver_role": target_sig.get("role"),
                "output_index": target_sig.get("output_index"),
            },
        ):
            quiet["status"] = "floor_pending_operator"
            quiet["floor_audio_confirmed"] = False
            quiet["pending_playback_id"] = result.get("playback_id")
            quiet["last_operator_result"] = None
        elif not quiet.get("floor_audio_confirmed"):
            quiet["status"] = "floor_required"
    return quiet


def record_floor_audio_operator_result(
    *,
    outcome: str,
    playback_id: str | None = None,
    state_path: str | Path | None = None,
    now: NowFn = _now,
) -> dict[str, Any]:
    """Record the operator's floor-test result for the latest audible tone.

    Playback completion only proves the backend emitted samples. The operator
    must confirm that the correct physical driver was heard before raised tests
    may use this target/session as floor-audio evidence.
    """

    outcome = str(outcome or "").strip().lower()
    if outcome not in FLOOR_OPERATOR_OUTCOMES:
        raise ValueError("unsupported floor audio operator outcome")

    now_epoch = now()
    prior = load_safe_playback_state(state_path=state_path, now=now)
    state = _normalise_state(prior, now_epoch=now_epoch)
    quiet = _normalise_quiet_start(state.get("quiet_start"))
    pending_target = playback_target_signature(quiet.get("current_target"))
    pending_playback_id = str(quiet.get("pending_playback_id") or "") or None
    requested_playback_id = str(playback_id or "").strip() or None
    issues: list[dict[str, str]] = []

    if state.get("status") != "armed":
        issues.append(_issue(
            "blocker",
            "safe_session_not_armed",
            "floor audio confirmation requires an armed safe session",
        ))
    if not pending_playback_id:
        issues.append(_issue(
            "blocker",
            "floor_playback_missing",
            "no audible floor playback result is available to confirm",
        ))
    if requested_playback_id and requested_playback_id != pending_playback_id:
        issues.append(_issue(
            "blocker",
            "playback_id_mismatch",
            "operator result does not match the latest floor playback",
        ))
    if not pending_target:
        issues.append(_issue(
            "blocker",
            "floor_playback_target_missing",
            "pending floor playback target is missing",
        ))
    if quiet.get("status") != "floor_pending_operator":
        issues.append(_issue(
            "blocker",
            "floor_confirmation_not_pending",
            "floor audio is not waiting for operator confirmation",
        ))

    operator_result = {
        "outcome": outcome,
        "accepted": not issues and outcome == "heard_correct_driver",
        "playback_id": pending_playback_id,
        "target": pending_target,
        "recorded_at": _utc_from_epoch(now_epoch),
    }
    if issues:
        operator_result["issues"] = issues
    quiet["last_operator_result"] = operator_result

    if issues:
        state.update({
            "updated_at": _utc_from_epoch(now_epoch),
            "last_action": f"floor_operator_rejected_{outcome}",
            "quiet_start": quiet,
            "issues": issues,
        })
        _atomic_write_json(_state_path(state_path), state)
        return state

    quiet["pending_playback_id"] = None
    if outcome == "heard_correct_driver":
        quiet["status"] = "floor_confirmed"
        quiet["floor_audio_confirmed"] = True
    else:
        quiet["status"] = "floor_required"
        quiet["floor_audio_confirmed"] = False

    state.update({
        "updated_at": _utc_from_epoch(now_epoch),
        "last_action": f"floor_operator_{outcome}",
        "quiet_start": quiet,
        "issues": issues,
    })
    _atomic_write_json(_state_path(state_path), state)
    return state


def _level_at_floor(level: float | None) -> bool:
    return level is not None and level <= MIN_TEST_LEVEL_DBFS + 1e-6


def _artifact_summary(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        key: raw.get(key)
        for key in (
            "wav_basename",
            "metadata_basename",
            "sample_rate_hz",
            "sample_format",
            "channel_count",
            "target_output_index",
            "frame_count",
            "duration_ms",
            "peak_dbfs",
            "retention_keep",
            "retention_removed",
        )
        if raw.get(key) is not None
    }
