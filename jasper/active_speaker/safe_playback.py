"""Safety-session substrate for future active-speaker test tones.

This module deliberately does not generate audio, load CamillaDSP configs, or
touch hardware. It only owns the durable state machine that future playback
code must pass through before any sound-emitting action is added.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from calendar import timegm
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
SAFE_PLAYBACK_SESSION_KIND = "jts_active_speaker_safe_playback_session"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_safe_playback.json")
DEFAULT_ARM_TTL_SEC = 120

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


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _normalise_state(raw: Any, *, now_epoch: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _base_state(now_epoch=now_epoch)
    state = _base_state(now_epoch=now_epoch)
    state.update(raw)
    state["playback_allowed"] = False
    state["tone_playback_implemented"] = False
    state.setdefault("issues", [])
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
            "issues": [],
        }
    )
    _atomic_write_json(_state_path(state_path), state)
    return state
