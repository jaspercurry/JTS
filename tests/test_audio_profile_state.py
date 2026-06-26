# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.audio_profile_state import (
    AecIntent,
    MicProbe,
    RuntimeAecEnv,
    build_audio_profile_status,
    profile_env_updates,
    resolve_audio_input_intent,
    runtime_env_from_mapping,
)


def test_runtime_env_from_mapping_prefers_fresh_env_file_over_process_env():
    runtime = runtime_env_from_mapping(
        {"JASPER_AEC_CHIP_AEC_ENABLED": "1"},
        process_env={
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
        },
    )

    assert runtime.chip_enabled is True
    assert runtime.chip_aec_150_device == "udp:9887"


def test_chip_aec_active_requires_bridge_firmware_and_runtime_env():
    status = build_audio_profile_status(
        AecIntent(mode="auto", chip_aec_enabled=True),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=True,
            chip_aec_150_device="udp:9887",
            chip_aec_210_device="udp:9888",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"] == {
        "selection": "xvf_chip_aec",
        "requested": "xvf_chip_aec",
        "resolved": "xvf_chip_aec",
        "active": "xvf_chip_aec",
        "state": "active",
        "reason": "Chip-AEC runtime env is applied.",
        "validation_profile": "xvf_chip_aec",
    }
    assert status["microphone"]["processing_mode"] == "Chip-AEC"
    assert status["microphone"]["wake_legs"] == [
        "Primary chip beam",
        "Chip AEC 150",
        "Chip AEC 210",
    ]
    assert status["microphone"]["warnings"] == []


def test_chip_aec_request_reports_runtime_software_until_chip_applied():
    status = build_audio_profile_status(
        AecIntent(mode="auto", chip_aec_enabled=True),
        RuntimeAecEnv(primary_device="udp:9876", chip_enabled=False),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"]["requested"] == "xvf_chip_aec"
    assert status["audio_profile"]["active"] == "xvf_software_aec3"
    assert status["audio_profile"]["state"] == "pending"
    assert status["microphone"]["processing_mode"] == "Software AEC3"
    assert "not applied" in " ".join(status["microphone"]["warnings"])


def test_profile_status_warns_when_saved_aec_card_is_stale():
    status = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="xvf_chip_aec"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            aec_device="L16K6Ch",
            chip_enabled=False,
        ),
        MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
        bridge_active=False,
        chip_available=True,
    )

    assert status["audio_profile"]["state"] == "waiting_bridge"
    assert "configured AEC mic L16K6Ch" in status["audio_profile"]["reason"]
    assert "detected XVF card Array" in status["audio_profile"]["reason"]
    warnings = " ".join(status["microphone"]["warnings"])
    assert "Configured AEC mic L16K6Ch" in warnings
    assert "detected XVF card Array" in warnings
    assert "run the reconciler" in warnings


def test_chip_aec_active_requires_detected_aec_card_match():
    status = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="xvf_chip_aec"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            aec_device="L16K6Ch",
            chip_enabled=True,
            chip_aec_150_device="udp:9887",
            chip_aec_210_device="udp:9888",
        ),
        MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"]["active"] is None
    assert status["audio_profile"]["state"] == "pending"
    assert status["microphone"]["processing_mode"] == "Chip-AEC pending"
    assert status["microphone"]["wake_legs"] == []
    assert "Configured AEC mic L16K6Ch" in " ".join(
        status["microphone"]["warnings"]
    )


def test_software_aec3_profile_reports_optional_legs():
    status = build_audio_profile_status(
        AecIntent(mode="auto", raw_enabled=True, dtln_enabled=True),
        RuntimeAecEnv(
            primary_device="udp:9876",
            raw_device="udp:9877",
            dtln_device="udp:9878",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"]["requested"] == "xvf_software_aec3"
    assert status["audio_profile"]["selection"] == "custom"
    assert status["audio_profile"]["active"] == "xvf_software_aec3"
    assert status["microphone"]["wake_legs"] == ["AEC3", "Chip-direct raw", "DTLN"]


def test_disabled_mode_reports_direct_mic_profile():
    status = build_audio_profile_status(
        AecIntent(mode="disabled"),
        RuntimeAecEnv(primary_device="USB PnP Sound Device", aec_device="Array"),
        MicProbe(xvf_present=False, capture_channels=None),
        bridge_active=False,
        chip_available=False,
    )

    assert status["audio_profile"]["requested"] == "direct_mic"
    assert status["audio_profile"]["selection"] == "direct_mic"
    assert status["audio_profile"]["active"] == "direct_mic"
    assert status["audio_profile"]["state"] == "disabled"
    assert status["microphone"]["detected"] is True
    assert status["microphone"]["name"] == "Direct mic (USB PnP Sound Device)"


def test_auto_profile_resolves_to_chip_aec_when_available():
    intent = resolve_audio_input_intent(
        AecIntent(profile_selection="auto", raw_enabled=True),
        chip_available=True,
    )

    assert intent.mode == "auto"
    assert intent.raw_enabled is False
    assert intent.dtln_enabled is False
    assert intent.chip_aec_enabled is True


def test_auto_profile_falls_back_to_software_aec3_when_chip_unavailable():
    intent = resolve_audio_input_intent(
        AecIntent(profile_selection="auto", chip_aec_enabled=True),
        chip_available=False,
    )

    assert intent.mode == "auto"
    assert intent.raw_enabled is True
    assert intent.dtln_enabled is False
    assert intent.chip_aec_enabled is False


def test_profile_env_updates_stamp_rollback_safe_legacy_keys():
    assert profile_env_updates("xvf_chip_aec") == {
        "JASPER_AUDIO_INPUT_PROFILE": "xvf_chip_aec",
        "JASPER_AEC_MODE": "auto",
        "JASPER_WAKE_LEG_RAW": "0",
        "JASPER_WAKE_LEG_DTLN": "0",
        "JASPER_WAKE_LEG_CHIP_AEC": "1",
    }
    assert profile_env_updates("auto")["JASPER_WAKE_LEG_CHIP_AEC"] == "0"


def test_testing_profile_uses_chip_aec_runtime_but_same_validation_profile():
    status = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="xvf_chip_aec_testing"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=True,
            chip_aec_150_device="udp:9887",
            chip_aec_210_device="udp:9888",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
        chip_gate={
            "status": "testing",
            "permitted": True,
            "auto_allowed": False,
            "detail": "operator validation",
        },
    )

    assert status["audio_profile"]["selection"] == "xvf_chip_aec_testing"
    assert status["audio_profile"]["requested"] == "xvf_chip_aec_testing"
    assert status["audio_profile"]["active"] == "xvf_chip_aec_testing"
    assert status["audio_profile"]["validation_profile"] == "xvf_chip_aec"
    assert status["microphone"]["processing_mode"] == "Chip-AEC testing"
