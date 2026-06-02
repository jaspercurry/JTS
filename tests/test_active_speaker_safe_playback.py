from __future__ import annotations

from pathlib import Path

from jasper.active_speaker.safe_playback import (
    SAFE_PLAYBACK_SESSION_KIND,
    arm_safe_playback_session,
    load_safe_playback_state,
    stop_safe_playback_session,
)


def _env_report(
    *,
    ok: bool,
    safe_allowed: bool = False,
) -> dict:
    return {
        "status": "pass" if ok else "blocked",
        "load_gate": "ready" if ok else "path_safety_evidence_missing",
        "ok_to_load_active_config": ok,
        "camilla_config": {
            "classification": "active_startup_candidate" if ok else "jts_outputd_stereo",
            "path": "/tmp/active.yml" if ok else "/tmp/outputd.yml",
        },
        "safe_playback": {
            "status": "not_implemented",
            "playback_allowed": safe_allowed,
        },
        "issues": [] if ok else [{
            "severity": "blocker",
            "code": "path_safety_evidence_missing",
            "message": "path-safety evidence was not provided",
        }],
    }


def test_load_safe_playback_state_defaults_to_idle(tmp_path: Path) -> None:
    state = load_safe_playback_state(
        state_path=tmp_path / "state.json",
        now=lambda: 1000,
    )

    assert state["kind"] == SAFE_PLAYBACK_SESSION_KIND
    assert state["status"] == "idle"
    assert state["playback_allowed"] is False


def test_arm_safe_playback_blocks_when_environment_is_not_ready(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"

    state = arm_safe_playback_session(
        _env_report(ok=False),
        state_path=path,
        now=lambda: 1000,
    )

    assert state["status"] == "blocked"
    assert state["session_id"] is None
    assert state["playback_allowed"] is False
    assert {issue["code"] for issue in state["issues"]} >= {
        "active_environment_not_ready",
        "path_safety_evidence_missing",
    }
    assert load_safe_playback_state(state_path=path, now=lambda: 1000)["status"] == "blocked"


def test_arm_safe_playback_creates_no_audio_session_when_environment_passes(
    tmp_path: Path,
) -> None:
    state = arm_safe_playback_session(
        _env_report(ok=True),
        state_path=tmp_path / "state.json",
        now=lambda: 1000,
        ttl_sec=30,
    )

    assert state["status"] == "armed"
    assert state["session_id"]
    assert state["playback_allowed"] is False
    assert state["tone_playback_implemented"] is False
    assert state["expires_at"] == "1970-01-01T00:17:10Z"
    assert state["environment"]["ok_to_load_active_config"] is True
    assert state["environment"]["camilla_config_name"] == "active.yml"
    assert "camilla_config_path" not in state["environment"]


def test_arm_safe_playback_blocks_unexpected_playback_permission(
    tmp_path: Path,
) -> None:
    state = arm_safe_playback_session(
        _env_report(ok=True, safe_allowed=True),
        state_path=tmp_path / "state.json",
        now=lambda: 1000,
    )

    assert state["status"] == "blocked"
    assert "unexpected_playback_permission" in {
        issue["code"] for issue in state["issues"]
    }


def test_load_safe_playback_state_marks_expired_session(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    arm_safe_playback_session(
        _env_report(ok=True),
        state_path=path,
        now=lambda: 1000,
        ttl_sec=1,
    )

    state = load_safe_playback_state(state_path=path, now=lambda: 1002)

    assert state["status"] == "expired"
    assert state["playback_allowed"] is False


def test_stop_safe_playback_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    armed = arm_safe_playback_session(
        _env_report(ok=True),
        state_path=path,
        now=lambda: 1000,
    )

    stopped = stop_safe_playback_session(
        state_path=path,
        now=lambda: 1001,
    )
    stopped_again = stop_safe_playback_session(
        state_path=path,
        now=lambda: 1002,
    )

    assert stopped["status"] == "stopped"
    assert stopped["session_id"] == armed["session_id"]
    assert stopped["playback_allowed"] is False
    assert stopped_again["status"] == "stopped"
    assert stopped_again["session_id"] == armed["session_id"]
