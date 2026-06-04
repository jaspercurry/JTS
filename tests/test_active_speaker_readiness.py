from __future__ import annotations

from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.playback import tone_backend_status
from jasper.active_speaker.readiness import (
    PLAYBACK_READINESS_KIND,
    build_playback_readiness,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology(*, verified: bool = True, protection: str = "present") -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": verified,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": verified,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": protection,
                    },
                ],
            }
        ],
        "routing": {"main_left_group_id": "left"},
    })


def _environment(*, ready: bool = True, config_path: str = "/tmp/active.yml") -> dict:
    return {
        "status": "pass" if ready else "blocked",
        "ok_to_load_active_config": ready,
        "load_gate": "ready" if ready else "environment_blocked",
        "camilla_config": {
            "path": config_path,
            "classification": "active_startup_candidate" if ready else "missing",
        },
        "issues": [] if ready else [
            {
                "severity": "blocker",
                "code": "active_startup_candidate_required",
                "message": "active config missing",
            }
        ],
    }


def _startup_load(
    *,
    loaded: bool = True,
    active_path: str = "/tmp/active.yml",
) -> dict:
    return {
        "status": "loaded" if loaded else "idle",
        "loaded": loaded,
        "candidate_config_path": active_path if loaded else None,
        "active_config_path": active_path if loaded else None,
        "previous_config_path": "/tmp/prior.yml" if loaded else None,
        "rollback_available": loaded,
    }


def _safe_session(*, armed: bool = True) -> dict:
    return {
        "status": "armed" if armed else "idle",
        "session_id": "safe-1" if armed else None,
        "expires_at": "2026-06-02T12:02:00Z" if armed else None,
        "issues": [],
    }


def test_playback_readiness_passes_preconditions_without_authorizing_audio() -> None:
    report = build_playback_readiness(
        _topology(),
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
    )

    assert report["kind"] == PLAYBACK_READINESS_KIND
    assert report["status"] == "preconditions_passed"
    assert report["preconditions_passed"] is True
    assert report["playback_allowed"] is False
    assert report["would_play"] is False
    assert report["tone_playback_implemented"] is False
    assert report["audio_emitted"] is False
    assert report["target"]["physical_output_index"] == 0
    assert report["issues"] == []
    assert all(gate["passed"] for gate in report["required_gates"])


def test_playback_readiness_allows_non_tweeter_when_audio_backend_enabled() -> None:
    report = build_playback_readiness(
        _topology(),
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
        tone_backend=tone_backend_status({
            "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
            "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
            "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
        }),
    )

    assert report["preconditions_passed"] is True
    assert report["playback_allowed"] is True
    assert report["would_play"] is True
    assert report["tone_playback_implemented"] is True
    assert report["tone_backend"]["test_pcm"] == "hw:Active"


def test_playback_readiness_keeps_tweeter_audio_disabled_in_first_slice() -> None:
    report = build_playback_readiness(
        _topology(),
        speaker_group_id="left",
        role="tweeter",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
        tone_backend=tone_backend_status({
            "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
            "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
            "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
        }),
    )

    assert report["preconditions_passed"] is True
    assert report["playback_allowed"] is False
    assert "tweeter_audio_not_enabled" in {
        issue["code"] for issue in report["issues"]
    }


def test_playback_readiness_fails_closed_for_unverified_identity() -> None:
    report = build_playback_readiness(
        _topology(verified=False),
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
    )

    codes = {issue["code"] for issue in report["issues"]}

    assert report["status"] == "blocked"
    assert report["preconditions_passed"] is False
    assert "physical_identity_verified" in codes
    assert report["playback_allowed"] is False


def test_playback_readiness_requires_tweeter_protection_for_tweeter_target() -> None:
    report = build_playback_readiness(
        _topology(protection="required_missing"),
        speaker_group_id="left",
        role="tweeter",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
    )

    codes = {issue["code"] for issue in report["issues"]}

    assert report["status"] == "blocked"
    assert "tweeter_protection_unverified" in codes
    assert "tweeter_protection" not in codes


def test_playback_readiness_keeps_software_guard_request_blocked() -> None:
    report = build_playback_readiness(
        _topology(protection="software_guard_requested"),
        speaker_group_id="left",
        role="tweeter",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
    )

    codes = {issue["code"] for issue in report["issues"]}

    assert report["status"] == "blocked"
    assert "tweeter_software_guard_requested" in codes
    assert report["playback_allowed"] is False


def test_playback_readiness_requires_environment_and_safe_session() -> None:
    report = build_playback_readiness(
        _topology(),
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(ready=False),
        safe_session=_safe_session(armed=False),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
    )

    codes = {issue["code"] for issue in report["issues"]}
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert "active_startup_candidate_required" in codes
    assert "safe_session_armed" in codes
    assert gates["active_config_load_gate"] is False
    assert gates["safe_session_armed"] is False


def test_playback_readiness_requires_loaded_protected_startup_config() -> None:
    report = build_playback_readiness(
        _topology(),
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(loaded=False),
        tone_backend=tone_backend_status({
            "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
            "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
            "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
        }),
    )
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert report["playback_allowed"] is False
    assert report["startup_load"]["loaded"] is False
    assert gates["protected_startup_config_loaded"] is False
    assert "protected_startup_config_loaded" in {
        issue["code"] for issue in report["issues"]
    }


def test_playback_readiness_blocks_when_camilla_config_no_longer_matches_loaded_startup() -> None:
    report = build_playback_readiness(
        _topology(),
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(config_path="/tmp/other.yml"),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(active_path="/tmp/active.yml"),
        tone_backend=tone_backend_status({
            "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
            "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
            "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
        }),
    )

    assert report["status"] == "blocked"
    assert report["playback_allowed"] is False
    assert report["startup_load"]["current_config_matches_loaded"] is False


def test_playback_readiness_blocks_when_current_camilla_path_is_unknown() -> None:
    environment = _environment()
    environment["camilla_config"] = {}

    report = build_playback_readiness(
        _topology(),
        speaker_group_id="left",
        role="woofer",
        environment_report=environment,
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(active_path="/tmp/active.yml"),
        tone_backend=tone_backend_status({
            "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
            "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
            "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
        }),
    )
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert report["playback_allowed"] is False
    assert report["startup_load"]["status"] == "current_config_unknown"
    assert report["startup_load"]["current_config_path"] is None
    assert report["startup_load"]["current_config_matches_loaded"] is False
    assert gates["protected_startup_config_loaded"] is False
