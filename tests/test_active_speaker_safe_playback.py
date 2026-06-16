from __future__ import annotations

from pathlib import Path

from jasper.active_speaker.safe_playback import (
    SAFE_PLAYBACK_SESSION_KIND,
    arm_safe_playback_session,
    floor_audio_retry_allowed_for_target,
    load_safe_playback_state,
    record_floor_audio_operator_result,
    record_safe_playback_result,
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
    assert state["quiet_start"]["status"] == "floor_required"
    assert state["quiet_start"]["floor_audio_confirmed"] is False
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
    assert state["quiet_start"]["status"] == "floor_required"
    assert state["quiet_start"]["floor_audio_confirmed"] is False


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
    assert stopped["quiet_start"]["status"] == "floor_required"
    assert stopped["quiet_start"]["floor_audio_confirmed"] is False
    assert stopped_again["status"] == "stopped"
    assert stopped_again["session_id"] == armed["session_id"]


def test_record_safe_playback_marks_floor_audio_pending_operator_confirmation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    arm_safe_playback_session(
        _env_report(ok=True),
        state_path=path,
        now=lambda: 1000,
    )

    state = record_safe_playback_result(
        {
            "status": "completed",
            "backend": "aplay",
            "playback_id": "play-1",
            "audio_emitted": True,
            "target": {
                "speaker_group_id": "mono",
                "driver_role": "woofer",
                "output_index": 0,
            },
            "tone": {"level_dbfs": -80.0},
            "issues": [],
        },
        state_path=path,
        now=lambda: 1001,
    )

    quiet = state["quiet_start"]
    assert quiet["status"] == "floor_pending_operator"
    assert quiet["floor_audio_confirmed"] is False
    assert quiet["pending_playback_id"] == "play-1"
    assert quiet["current_target"] == {
        "speaker_group_id": "mono",
        "role": "woofer",
        "output_index": 0,
    }
    assert quiet["last_level_dbfs"] == -80.0
    assert quiet["last_playback_at"] == "1970-01-01T00:16:41Z"

    confirmed = record_floor_audio_operator_result(
        outcome="heard_correct_driver",
        playback_id="play-1",
        state_path=path,
        now=lambda: 1002,
    )

    assert confirmed["quiet_start"]["status"] == "floor_confirmed"
    assert confirmed["quiet_start"]["floor_audio_confirmed"] is True
    assert confirmed["quiet_start"]["last_operator_result"]["outcome"] == (
        "heard_correct_driver"
    )


def test_silent_floor_result_allows_same_driver_raised_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    target = {
        "speaker_group_id": "mono",
        "driver_role": "woofer",
        "output_index": 0,
    }
    arm_safe_playback_session(
        _env_report(ok=True),
        state_path=path,
        now=lambda: 1000,
    )
    record_safe_playback_result(
        {
            "status": "completed",
            "backend": "aplay",
            "playback_id": "floor-1",
            "audio_emitted": True,
            "target": target,
            "tone": {"level_dbfs": -80.0},
            "issues": [],
        },
        state_path=path,
        now=lambda: 1001,
    )
    silent = record_floor_audio_operator_result(
        outcome="silent",
        playback_id="floor-1",
        state_path=path,
        now=lambda: 1002,
    )

    assert floor_audio_retry_allowed_for_target(silent, target) is True
    assert floor_audio_retry_allowed_for_target(
        silent,
        {
            "speaker_group_id": target["speaker_group_id"],
            "role": target["driver_role"],
            "physical_output_index": target["output_index"],
        },
    ) is True

    resumed = {
        **silent,
        "quiet_start": {
            **silent["quiet_start"],
            "last_operator_result": {
                **silent["quiet_start"]["last_operator_result"],
                "target": {
                    "speaker_group_id": target["speaker_group_id"],
                    "driver_role": target["driver_role"],
                    "physical_output_index": target["output_index"],
                },
            },
        },
    }
    assert floor_audio_retry_allowed_for_target(resumed, target) is True

    pending = record_safe_playback_result(
        {
            "status": "completed",
            "backend": "aplay",
            "playback_id": "raised-1",
            "audio_emitted": True,
            "target": target,
            "tone": {"level_dbfs": -68.0},
            "issues": [],
        },
        state_path=path,
        now=lambda: 1003,
    )

    assert pending["quiet_start"]["status"] == "floor_pending_operator"
    assert pending["quiet_start"]["pending_playback_id"] == "raised-1"

    confirmed = record_floor_audio_operator_result(
        outcome="heard_correct_driver",
        playback_id="raised-1",
        state_path=path,
        now=lambda: 1004,
    )

    assert confirmed["quiet_start"]["status"] == "floor_confirmed"
    assert confirmed["quiet_start"]["floor_audio_confirmed"] is True


def test_record_safe_playback_does_not_confirm_artifact_only_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    arm_safe_playback_session(
        _env_report(ok=True),
        state_path=path,
        now=lambda: 1000,
    )

    state = record_safe_playback_result(
        {
            "status": "completed",
            "backend": "wav_artifact",
            "playback_id": "play-1",
            "audio_emitted": False,
            "target": {
                "speaker_group_id": "mono",
                "driver_role": "woofer",
                "output_index": 0,
            },
            "tone": {"level_dbfs": -80.0},
            "artifact": {"wav_basename": "tone.wav"},
            "issues": [],
        },
        state_path=path,
        now=lambda: 1001,
    )

    assert state["quiet_start"]["status"] == "floor_required"
    assert state["quiet_start"]["floor_audio_confirmed"] is False
    assert state["quiet_start"]["current_target"] == {
        "speaker_group_id": "mono",
        "role": "woofer",
        "output_index": 0,
    }


def test_record_safe_playback_does_not_confirm_result_with_issues(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    arm_safe_playback_session(
        _env_report(ok=True),
        state_path=path,
        now=lambda: 1000,
    )

    state = record_safe_playback_result(
        {
            "status": "completed",
            "backend": "aplay",
            "playback_id": "play-1",
            "audio_emitted": True,
            "target": {
                "speaker_group_id": "mono",
                "driver_role": "woofer",
                "output_index": 0,
            },
            "tone": {"level_dbfs": -80.0},
            "issues": [{"code": "backend_warning"}],
        },
        state_path=path,
        now=lambda: 1001,
    )

    assert state["quiet_start"]["status"] == "floor_required"
    assert state["quiet_start"]["floor_audio_confirmed"] is False


def test_record_safe_playback_target_change_resets_floor_confirmation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    arm_safe_playback_session(
        _env_report(ok=True),
        state_path=path,
        now=lambda: 1000,
    )
    record_safe_playback_result(
        {
            "status": "completed",
            "backend": "aplay",
            "playback_id": "play-1",
            "audio_emitted": True,
            "target": {
                "speaker_group_id": "mono",
                "driver_role": "woofer",
                "output_index": 0,
            },
            "tone": {"level_dbfs": -80.0},
            "issues": [],
        },
        state_path=path,
        now=lambda: 1001,
    )
    record_floor_audio_operator_result(
        outcome="heard_correct_driver",
        playback_id="play-1",
        state_path=path,
        now=lambda: 1002,
    )

    state = record_safe_playback_result(
        {
            "status": "blocked",
            "backend": "aplay",
            "playback_id": "play-2",
            "audio_emitted": False,
            "target": {
                "speaker_group_id": "mono",
                "driver_role": "mid",
                "output_index": 1,
            },
            "tone": {"level_dbfs": -55.0},
            "issues": [{"code": "floor_audio_not_confirmed"}],
        },
        state_path=path,
        now=lambda: 1003,
    )

    assert state["quiet_start"]["status"] == "floor_required"
    assert state["quiet_start"]["floor_audio_confirmed"] is False
    assert state["quiet_start"]["current_target"] == {
        "speaker_group_id": "mono",
        "role": "mid",
        "output_index": 1,
    }


def test_pending_floor_confirmation_survives_later_artifact_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    arm_safe_playback_session(
        _env_report(ok=True),
        state_path=path,
        now=lambda: 1000,
    )
    record_safe_playback_result(
        {
            "status": "completed",
            "backend": "aplay",
            "playback_id": "floor-1",
            "audio_emitted": True,
            "target": {
                "speaker_group_id": "mono",
                "driver_role": "woofer",
                "output_index": 0,
            },
            "tone": {"level_dbfs": -80.0},
            "issues": [],
        },
        state_path=path,
        now=lambda: 1001,
    )
    state = record_safe_playback_result(
        {
            "status": "completed",
            "backend": "wav_artifact",
            "playback_id": "artifact-1",
            "audio_emitted": False,
            "target": {
                "speaker_group_id": "mono",
                "driver_role": "woofer",
                "output_index": 0,
            },
            "tone": {"level_dbfs": -80.0},
            "artifact": {"wav_basename": "tone.wav"},
            "issues": [],
        },
        state_path=path,
        now=lambda: 1002,
    )

    assert state["playback"]["playback_id"] == "artifact-1"
    assert state["quiet_start"]["status"] == "floor_pending_operator"
    assert state["quiet_start"]["pending_playback_id"] == "floor-1"

    confirmed = record_floor_audio_operator_result(
        outcome="heard_correct_driver",
        playback_id="floor-1",
        state_path=path,
        now=lambda: 1003,
    )

    assert confirmed["quiet_start"]["status"] == "floor_confirmed"
    assert confirmed["quiet_start"]["floor_audio_confirmed"] is True
