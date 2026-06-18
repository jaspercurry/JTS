from __future__ import annotations

import pytest

from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.playback import tone_backend_status
from jasper.active_speaker.readiness import (
    HIGH_FREQUENCY_FLOOR_TEST_PREVIEW_KIND,
    PLAYBACK_READINESS_KIND,
    build_playback_readiness,
)
from jasper.output_topology import (
    DUAL_APPLE_ACTIVE_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
)
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    OutputCardFact,
    classify_output_cards,
    write_state as write_output_hardware_state,
)


def _dual_apple_hardware() -> dict:
    return {
        "device_id": DUAL_APPLE_ACTIVE_DEVICE_ID,
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "left_dac",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FHL2FN3AC",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "right_dac",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FLL2FN3A3",
                "physical_output_indexes": [2, 3],
            },
        ],
        "clock_domain_evidence": {
            "evidence_kind": "dual_apple_usb_c_dac_drift_measurement",
            "measurement_id": "scarlett-ticks-900s-repeat-buffered",
            "status": "passed",
            "duration_seconds": 900,
            "sample_rate_hz": 48000,
            "offset_frames": -7,
            "max_offset_delta_frames": 0,
            "drift_ppm": 0,
            "xrun_count": 0,
            "dac_serials": [
                "DWH53530FHL2FN3AC",
                "DWH53530FLL2FN3A3",
            ],
        },
    }


def _write_dual_apple_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    write_output_hardware_state(
        classify_output_cards([
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FHL2FN3AC",
                usb_path="usb1/1-2",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
            OutputCardFact(
                card_id="A_1",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FLL2FN3A3",
                usb_path="usb1/1-1",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
        ]),
        path=tmp_path / "output_hardware.json",
    )


def _topology(
    *,
    verified: bool = True,
    protection: str = "present",
    hardware: dict | None = None,
) -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": hardware or {
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


def test_playback_readiness_accepts_measured_dual_apple_clock_precondition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _write_dual_apple_observation(monkeypatch, tmp_path)
    report = build_playback_readiness(
        _topology(hardware=_dual_apple_hardware()),
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
    )

    assert report["status"] == "preconditions_passed"
    assert report["preconditions_passed"] is True
    assert report["playback_allowed"] is False
    assert report["clock_domain"]["status"] == "dual_apple_composite_clock"
    assert report["clock_domain"]["measured_composite_supported"] is True
    assert report["clock_domain"]["coherent_physical_output_count"] == 4
    assert all(gate["passed"] for gate in report["required_gates"])


def test_playback_readiness_surfaces_missing_dual_apple_clock_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _write_dual_apple_observation(monkeypatch, tmp_path)
    hardware = _dual_apple_hardware()
    hardware.pop("clock_domain_evidence")

    report = build_playback_readiness(
        _topology(hardware=hardware),
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
    )

    codes = {issue["code"] for issue in report["issues"]}

    assert report["status"] == "preconditions_passed"
    assert report["preconditions_passed"] is True
    assert report["clock_domain"]["status"] == "dual_apple_composite_clock"
    assert report["clock_domain"]["composite_clock_supported"] is True
    assert report["clock_domain"]["measured_composite_supported"] is False
    assert "clock_evidence_missing" in codes
    assert "single_clock_domain" not in codes


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
            "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
            "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
        }),
    )

    assert report["preconditions_passed"] is True
    assert report["playback_allowed"] is True
    assert report["would_play"] is True
    assert report["tone_playback_implemented"] is True
    assert report["tone_backend"]["test_pcm"] == "hw:Active"
    assert report["audible_test"] == {
        "policy_version": "driver_protection_auto_level_v1",
        "allowed_roles": ["mid", "subwoofer", "woofer"],
        "target_role": "woofer",
        "target_role_allowed": True,
        "driver_role_class": "low_frequency",
        "driver_style": None,
        "driver_protection_audio_allowed": True,
    }


def test_playback_readiness_allows_tweeter_floor_audio_with_protection_profile() -> None:
    report = build_playback_readiness(
        _topology(),
        speaker_group_id="left",
        role="tweeter",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
        tone_backend=tone_backend_status({
            "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
            "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
        }),
    )

    assert report["preconditions_passed"] is True
    assert report["playback_allowed"] is True
    assert report["audible_test"]["target_role_allowed"] is True
    assert report["driver_protection"]["role_class"] == "high_frequency"
    assert report["driver_protection"]["audio_allowed"] is True


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


def test_playback_readiness_accepts_software_guard_request() -> None:
    report = build_playback_readiness(
        _topology(protection="software_guard_requested"),
        speaker_group_id="left",
        role="tweeter",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
    )

    assert report["status"] == "preconditions_passed"
    assert report["issues"] == []
    assert report["driver_protection"]["protection_status"] == "software_guard_requested"
    assert report["driver_protection"]["audio_allowed"] is True
    assert report["playback_allowed"] is False


def test_playback_readiness_reports_high_frequency_guided_readiness() -> None:
    report = build_playback_readiness(
        _topology(protection="software_guard_requested"),
        speaker_group_id="left",
        role="tweeter",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(observed_mic_dbfs=-32),
        startup_load_state=_startup_load(),
        tone_backend=tone_backend_status({
            "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
            "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
        }),
    )

    hf = report["high_frequency_driver"]

    assert report["preconditions_passed"] is True
    assert report["playback_allowed"] is True
    assert hf["status"] == "guided_ready"
    assert hf["audio_allowed"] is True
    assert hf["protection_mode"] == "software_guarded"
    assert hf["manual_floor_test_candidate"] is True
    assert hf["guided_floor_test_candidate"] is True
    assert hf["microphone"]["status"] == "usable"
    assert hf["floor_test_preview"]["kind"] == HIGH_FREQUENCY_FLOOR_TEST_PREVIEW_KIND
    assert hf["floor_test_preview"]["would_play"] is False
    assert hf["floor_test_preview"]["audio_allowed"] is False
    assert hf["floor_test_preview"]["tone"]["level_dbfs"] == -80.0
    assert hf["floor_test_preview"]["tone"]["frequency_hz"] == 5000.0
    assert hf["floor_test_preview"]["tone"]["band_limit"] == {
        "type": "highpass",
        "highpass_hz": 5000.0,
    }
    assert hf["auto_level"]["status"] == "locked"


def test_playback_readiness_blocks_high_frequency_guidance_on_clipping() -> None:
    report = build_playback_readiness(
        _topology(protection="software_guard_requested"),
        speaker_group_id="left",
        role="tweeter",
        environment_report=_environment(),
        safe_session=_safe_session(),
        calibration_level=calibration_level_payload(
            observed_mic_dbfs=-18,
            mic_clipping=True,
        ),
        startup_load_state=_startup_load(),
    )

    hf = report["high_frequency_driver"]
    codes = {issue["code"] for issue in hf["issues"]}

    assert hf["status"] == "blocked"
    assert hf["manual_floor_test_candidate"] is False
    assert hf["guided_floor_test_candidate"] is False
    assert hf["microphone"]["status"] == "clipping"
    assert hf["floor_test_preview"]["status"] == "blocked"
    assert hf["floor_test_preview"]["would_play"] is False
    assert hf["auto_level"]["status"] == "reset"
    assert "mic_not_too_loud" in codes


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
            "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
            "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
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
            "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
            "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
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
            "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
            "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
        }),
    )
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert report["playback_allowed"] is False
    assert report["startup_load"]["status"] == "current_config_unknown"
    assert report["startup_load"]["current_config_path"] is None
    assert report["startup_load"]["current_config_matches_loaded"] is False
    assert gates["protected_startup_config_loaded"] is False
