# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.wake_corpus.capture_plan: build_capture_plan and
validate_active_capture_plan against a bare ports map, independent of the
recorder backend and HTTP layer that consume them."""

from __future__ import annotations

import pytest

from jasper.wake_corpus import bridge_session, capture_plan
from jasper.wake_ports import build_ports


def test_capture_plan_describes_chip_profile_layers() -> None:
    plan = capture_plan.build_capture_plan(
        build_ports(),
        corpus_profile=capture_plan.PROFILE_CHIP_AEC_COMPARISON,
        include_usb_mic=False,
        include_usb_dtln=False,
        include_bridge_readiness=False,
    )

    assert plan["schema_version"] == capture_plan.CAPTURE_PLAN_SCHEMA_VERSION
    assert plan["recipe"] == "chip_aec_comparison"
    assert plan["selected_physical_mics"] == ["xvf3800"]
    by_token = {leg["token"]: leg for leg in plan["legs"]}
    assert by_token["chip_aec_150"]["processing"] == "hardware_aec"
    assert by_token["xvf_raw0_webrtc_aec3"]["native_stream"] == "raw_mic_0"
    assert by_token["xvf_raw0_webrtc_aec3"]["processing"] == "webrtc_aec3"
    assert by_token["ref"]["device_id"] == "speaker_reference"


def test_capture_plan_chip_profile_is_canonical_bridge_contract() -> None:
    snapshot = {
        "system_env": {"JASPER_XVF_ALSA_CARD": "Array"},
        "merged_env": {"JASPER_AEC_USB_MIC_DEVICE": "Studio Mic"},
        "bridge_outputs": {},
        "fingerprints": {"mic": "mic-a", "dac_reference": "dac-a"},
        "fingerprint_sources": {
            "mic": {"variant_id": "xvf3800_legacy_square_6ch"},
            "dac_reference": {"audio_dac_id": "apple_usb_c_dongle"},
        },
    }

    plan = capture_plan.build_capture_plan(
        build_ports(),
        corpus_profile=capture_plan.PROFILE_CHIP_AEC_COMPARISON,
        include_bridge_readiness=True,
        runtime_snapshot=snapshot,
    )

    assert plan["plan_id"]
    assert plan["selected_legs"] == [
        "chip_aec_150", "chip_aec_210", "raw0",
        "xvf_raw0_webrtc_aec3", "ref",
    ]
    assert plan["expected_emitted_legs"] == plan["selected_legs"]
    assert plan["required_bridge_outputs"] == [
        "ref", "chip_aec", "xvf_raw0_webrtc_aec3", "outputd_ref",
    ]
    env = plan["required_bridge_env"]
    assert env["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] == "1"
    assert env["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] == "1"
    assert env["JASPER_AEC_REF_SOURCE"] == "outputd_udp"
    assert env["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] == (
        bridge_session.OUTPUTD_REF_UDP_TARGET
    )
    assert plan["fingerprints"] == {"mic": "mic-a", "dac_reference": "dac-a"}


def test_capture_plan_id_is_stable_and_changes_with_hardware() -> None:
    base_snapshot = {
        "system_env": {},
        "merged_env": {},
        "bridge_outputs": {},
        "fingerprints": {"mic": "mic-a", "dac_reference": "dac-a"},
    }
    plan1 = capture_plan.build_capture_plan(
        build_ports(),
        include_dtln=False,
        runtime_snapshot=base_snapshot,
    )
    plan2 = capture_plan.build_capture_plan(
        build_ports(),
        include_dtln=False,
        runtime_snapshot=dict(base_snapshot),
    )
    changed = {
        **base_snapshot,
        "fingerprints": {"mic": "mic-b", "dac_reference": "dac-a"},
    }
    plan3 = capture_plan.build_capture_plan(
        build_ports(),
        include_dtln=False,
        runtime_snapshot=changed,
    )

    assert plan1["plan_id"] == plan2["plan_id"]
    assert plan1["plan_id"] != plan3["plan_id"]


def test_chip_capture_plan_id_is_stable_across_its_own_env_apply() -> None:
    """Recorder-owned reference env must not change the plan that requested it."""
    mic_source = {
        "family": "xvf3800",
        "variant_id": "xvf3800_legacy_square_6ch",
        "selected_xvf_mic_device": "Array",
        "selected_usb_mic_device": "Studio Mic",
        "chip_primary_leg": "chip_aec_150",
    }
    dac_source = {
        "audio_dac_id": "apple_usb_c_dongle",
        "dac": {
            "pcm": "outputd_dac",
            "backend": "alsa",
            "control_socket": "/run/jasper-outputd/control.sock",
        },
        "reference": {
            "source": "alsa",
            "outputd_chip_ref_pcm": "",
            "outputd_reference_udp_target": "",
            "outputd_chip_ref_sample_rate": 48000,
            "outputd_chip_ref_period_frames": 256,
            "outputd_chip_ref_buffer_frames": 1024,
            "bridge_output_enabled": False,
        },
        "chip_gate": {"allowed": True},
    }
    before = {
        "identity_recomputable": True,
        "system_env": {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
        "merged_env": {
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_USB_MIC_DEVICE": "Studio Mic",
            "JASPER_AEC_CHIP_AEC_PRIMARY_LEG": "chip_aec_150",
        },
        "bridge_outputs": {"outputd_ref": False},
        "dac_reference": {"validation": {"status": "unknown"}},
        "fingerprint_sources": {
            "mic": mic_source,
            "dac_reference": dac_source,
        },
        "fingerprints": {
            "mic": capture_plan.fingerprint_mapping(mic_source),
            "dac_reference": capture_plan.fingerprint_mapping(dac_source),
        },
    }

    planned = capture_plan.build_capture_plan(
        build_ports(),
        corpus_profile=capture_plan.PROFILE_CHIP_AEC_COMPARISON,
        runtime_snapshot=before,
    )
    after = {
        **before,
        "merged_env": {
            **before["merged_env"],
            **planned["required_bridge_env"],
        },
        "bridge_outputs": {
            **before["bridge_outputs"],
            "ref": True,
            "chip_aec": True,
            "xvf_raw0_webrtc_aec3": True,
            "outputd_ref": True,
        },
    }
    observed = capture_plan.build_capture_plan(
        build_ports(),
        corpus_profile=capture_plan.PROFILE_CHIP_AEC_COMPARISON,
        runtime_snapshot=after,
    )

    assert observed["plan_id"] == planned["plan_id"]
    assert observed["fingerprints"] == planned["fingerprints"]


def test_validate_active_capture_plan_refuses_missing_promised_leg() -> None:
    snapshot = {
        "system_env": {},
        "merged_env": {},
        "bridge_outputs": {},
        "fingerprints": {"mic": "mic-a", "dac_reference": "dac-a"},
    }
    plan = capture_plan.build_capture_plan(
        build_ports(),
        corpus_profile=capture_plan.PROFILE_CHIP_AEC_COMPARISON,
        runtime_snapshot=snapshot,
    )
    stats = {
        "counters": {},
        "active_capture_plan": {
            "wake_corpus_plan_id": plan["plan_id"],
            "emitted_legs": [
                "chip_aec_150", "raw0", "xvf_raw0_webrtc_aec3", "ref",
            ],
        },
    }

    result = capture_plan.validate_active_capture_plan(
        plan,
        bridge_stats=stats,
        runtime_snapshot=snapshot,
    )

    assert result.ok is False
    assert result.missing_emitted_legs == ["chip_aec_210"]
    assert "chip_aec_210" in result.errors[0]


def test_validate_active_capture_plan_refuses_mic_fingerprint_change() -> None:
    snapshot = {
        "system_env": {},
        "merged_env": {},
        "bridge_outputs": {},
        "fingerprints": {"mic": "mic-a", "dac_reference": "dac-a"},
    }
    plan = capture_plan.build_capture_plan(
        build_ports(),
        include_dtln=False,
        runtime_snapshot=snapshot,
    )
    stats = {
        "counters": {},
        "active_capture_plan": {
            "wake_corpus_plan_id": plan["plan_id"],
            "emitted_legs": plan["expected_emitted_legs"],
        },
    }
    changed_runtime = {
        **snapshot,
        "fingerprints": {"mic": "mic-b", "dac_reference": "dac-a"},
    }

    result = capture_plan.validate_active_capture_plan(
        plan,
        bridge_stats=stats,
        runtime_snapshot=changed_runtime,
    )

    assert result.ok is False
    assert result.fingerprint_mismatches == ["mic"]


@pytest.mark.parametrize(
    "active_profile",
    ["xvf_chip_aec", "xvf_chip_aec_testing"],
)
def test_capture_plan_describes_on_leg_runtime_overlay(
    active_profile: str,
) -> None:
    plan = capture_plan.build_capture_plan(
        build_ports(),
        include_dtln=False,
        include_bridge_readiness=False,
        active_audio_profile={
            "requested": active_profile,
            "active": active_profile,
            "state": "active",
        },
        runtime_audio_env={"chip_primary_leg": "chip_aec_210"},
    )

    by_token = {leg["token"]: leg for leg in plan["legs"]}
    assert by_token["on"]["label"] == "Chip AEC ASR 210 primary"
    assert by_token["on"]["kind"] == "hardware_aec"
    assert by_token["on"]["processing"] == "hardware_aec"
    assert by_token["on"]["source_channel"] == "fixed_beam_210"
    assert by_token["on"]["runtime_role"] == "production_primary"
    assert "on" not in plan["software_transforms"]["webrtc_aec3"]


def test_capture_plan_warns_for_heavy_two_mic_dtln() -> None:
    plan = capture_plan.build_capture_plan(
        build_ports(),
        corpus_profile=capture_plan.PROFILE_CHIP_AEC_COMPARISON,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_xvf_raw0_dtln=True,
        include_bridge_readiness=False,
    )

    assert plan["recipe"] == "chip_aec_comparison_extended"
    assert set(plan["selected_physical_mics"]) == {"xvf3800", "usb_mic"}
    assert plan["resource"]["level"] in {"high", "unsafe"}
    assert any("Multiple DTLN legs" in warning for warning in plan["warnings"])
