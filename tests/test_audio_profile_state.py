import pytest

from jasper.audio_profile_state import (
    AecIntent,
    MicProbe,
    RuntimeAecEnv,
    audio_profile_declarations,
    build_audio_profile_status,
    expected_runtime_env_for_profile,
    profile_by_id,
    runtime_env_from_mapping,
)
from jasper import wake_legs


def test_declares_desired_profile_vocabulary():
    ids = {profile.profile_id for profile in audio_profile_declarations()}

    assert {
        "xvf_software_aec3",
        "xvf_chip_aec",
        "generic_usb_software_aec3",
        "corpus_comparison",
        "dac_validation",
    } <= ids


def test_profile_ids_are_unique():
    ids = [profile.profile_id for profile in audio_profile_declarations()]

    assert len(ids) == len(set(ids))


def test_declared_profile_legs_resolve_to_wake_leg_registry():
    for profile in audio_profile_declarations():
        for leg in (*profile.wake_legs, *profile.corpus_legs):
            registered = wake_legs.by_token(leg.token)
            assert registered.token == leg.token
            if leg.source_kind is None:
                assert leg.effective_source_kind == registered.kind


def test_chip_profile_declares_single_chip_exclusion_and_static_env_shape():
    profile = profile_by_id("xvf_chip_aec")

    primary_carrier = profile.wake_legs[0]
    assert primary_carrier.token == "on"
    assert wake_legs.by_token(primary_carrier.token).kind == wake_legs.LegKind.SOFTWARE_AEC
    assert primary_carrier.effective_source_kind == wake_legs.LegKind.HARDWARE_AEC
    assert profile.requires_xvf_6ch is True
    assert profile.requires_chip_reference is True
    assert profile.mutually_exclusive_leg_tokens == ("off", "dtln")
    assert expected_runtime_env_for_profile("xvf_chip_aec") == {
        "JASPER_AEC_CHIP_AEC_ENABLED": "1",
        "JASPER_AEC_REF_SOURCE": "outputd_udp",
        "JASPER_AEC_OUTPUTD_REF_UDP_HOST": "127.0.0.1",
        "JASPER_AEC_OUTPUTD_REF_UDP_PORT": "9891",
        "JASPER_OUTPUTD_CHIP_REF_PCM": "plughw:CARD=Array,DEV=0",
        "JASPER_OUTPUTD_REFERENCE_UDP_TARGET": "127.0.0.1:9891",
        "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE": "16000",
        "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES": "320",
        "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES": "1280",
        "JASPER_MIC_DEVICE": "udp:9876",
        "JASPER_AEC_DTLN_ENABLED": "0",
        "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
        "JASPER_MIC_DEVICE_CHIP_AEC_210": "udp:9888",
        "JASPER_MIC_DEVICE_RAW": "",
        "JASPER_MIC_DEVICE_DTLN": "",
    }


def test_software_profile_static_env_shape_tracks_optional_legs():
    default_env = expected_runtime_env_for_profile("xvf_software_aec3")
    assert default_env["JASPER_MIC_DEVICE"] == "udp:9876"
    assert default_env["JASPER_MIC_DEVICE_RAW"] == ""
    assert default_env["JASPER_MIC_DEVICE_DTLN"] == ""
    assert default_env["JASPER_AEC_DTLN_ENABLED"] == "0"
    assert default_env["JASPER_AEC_REF_SOURCE"] == "alsa"
    assert default_env["JASPER_AEC_CHIP_AEC_ENABLED"] == "0"

    dual_env = expected_runtime_env_for_profile(
        "xvf_software_aec3",
        enabled_optional_tokens=("off",),
    )
    assert dual_env["JASPER_MIC_DEVICE_RAW"] == "udp:9877"
    assert dual_env["JASPER_MIC_DEVICE_DTLN"] == ""
    assert dual_env["JASPER_AEC_DTLN_ENABLED"] == "0"

    triple_env = expected_runtime_env_for_profile(
        "xvf_software_aec3",
        enabled_optional_tokens=("off", "dtln"),
    )
    assert triple_env["JASPER_MIC_DEVICE_RAW"] == "udp:9877"
    assert triple_env["JASPER_MIC_DEVICE_DTLN"] == "udp:9878"
    assert triple_env["JASPER_AEC_DTLN_ENABLED"] == "1"


def test_profiles_without_static_runtime_env_shape_do_not_claim_one():
    for profile_id in (
        "direct_mic",
        "generic_usb_software_aec3",
        "corpus_comparison",
        "dac_validation",
    ):
        with pytest.raises(ValueError, match="no static runtime env shape"):
            expected_runtime_env_for_profile(profile_id)


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
        "requested": "xvf_chip_aec",
        "active": "xvf_chip_aec",
        "state": "active",
        "reason": "Chip-AEC runtime env is applied.",
    }
    assert status["microphone"]["processing_mode"] == "Chip-AEC"
    assert status["microphone"]["wake_legs"] == [
        "Primary chip beam",
        "Chip AEC 150",
        "Chip AEC 210",
    ]
    assert status["microphone"]["warnings"] == []


def test_chip_aec_pending_when_runtime_env_not_applied():
    status = build_audio_profile_status(
        AecIntent(mode="auto", chip_aec_enabled=True),
        RuntimeAecEnv(primary_device="udp:9876", chip_enabled=False),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"]["requested"] == "xvf_chip_aec"
    assert status["audio_profile"]["active"] is None
    assert status["audio_profile"]["state"] == "pending"
    assert status["microphone"]["processing_mode"] == "Chip-AEC pending"
    assert "not applied" in " ".join(status["microphone"]["warnings"])


def test_software_aec3_profile_reports_optional_legs():
    status = build_audio_profile_status(
        AecIntent(mode="auto", raw_enabled=True, dtln_enabled=True),
        RuntimeAecEnv(primary_device="udp:9876"),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"]["requested"] == "xvf_software_aec3"
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
    assert status["audio_profile"]["active"] == "direct_mic"
    assert status["audio_profile"]["state"] == "disabled"
    assert status["microphone"]["detected"] is True
    assert status["microphone"]["name"] == "Direct mic (USB PnP Sound Device)"
