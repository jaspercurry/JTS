# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper import audio_validation, audio_validation_artifacts as artifacts
from jasper.audio_profile_state import MicProbe
from jasper.audio_validation_route import route_live_state_issues
from tests.audio_validation_fixtures import (
    _active_chip_inputs,
    _bridge_sample,
    _chip_readback,
    _outputd_sample,
    _outputd_stability_inputs,
)


def test_route_live_state_issues_detect_runtime_mismatches():
    identity = {
        "fanin_direct_config": {
            "lane": "usbsink",
            "source": "direct",
            "device": "hw:UAC2Gadget",
            "period_frames": 256,
            "min_buffer_frames": 768,
            "buffer_period_aligned": True,
            "negotiated_buffer_frames": 768,
        },
        "fanin_resampler_config": {
            "enabled": True,
            "lane": "usbsink",
            "target_frames": 512,
            "warmup_cushion_frames": 1536,
        },
    }

    assert (
        route_live_state_issues(
            identity,
            fanin_status={
                "inputs": [
                    {
                        "label": "usbsink",
                        "source": "direct",
                        "direct": {
                            "device": "hw:UAC2Gadget",
                            "health": "capturing",
                            "period_frames": 256,
                            "buffer_frames": 768,
                        },
                        "resampler": {
                            "locked": True,
                            "target_fill_frames": 2048,
                        },
                    }
                ]
            },
        )
        == ()
    )

    issues = route_live_state_issues(
        identity,
        fanin_status={
            "inputs": [
                {
                    "label": "usbsink",
                    "source": "aloop",
                    "direct": {
                        "device": "hw:Other",
                        "health": "broken",
                        "period_frames": 128,
                        "buffer_frames": 500,
                    },
                    "resampler": {
                        "locked": False,
                        "target_fill_frames": 1536,
                    },
                }
            ]
        },
    )

    assert "live_fanin_direct_mismatch:usbsink:source" in issues
    assert "live_fanin_direct_mismatch:usbsink:device" in issues
    assert "live_fanin_direct_unhealthy:usbsink:broken" in issues
    assert "live_fanin_direct_mismatch:usbsink:period_frames" in issues
    assert "live_fanin_direct_mismatch:usbsink:buffer_frames" in issues
    assert "live_fanin_direct_mismatch:usbsink:buffer_alignment" in issues
    assert "live_fanin_resampler_unlocked:usbsink" in issues
    assert "live_fanin_resampler_mismatch:usbsink:target_fill_frames" in issues


def test_route_live_state_requires_the_negotiated_direct_buffer():
    identity = {
        "fanin_direct_config": {
            "lane": "usbsink",
            "source": "direct",
            "device": "hw:UAC2Gadget",
            "period_frames": 256,
            "min_buffer_frames": 768,
            "buffer_period_aligned": True,
            "negotiated_buffer_frames": 768,
        },
    }

    def status(buffer_frames: int):
        return {
            "inputs": [
                {
                    "label": "usbsink",
                    "source": "direct",
                    "direct": {
                        "device": "hw:UAC2Gadget",
                        "health": "capturing",
                        "period_frames": 256,
                        "buffer_frames": buffer_frames,
                    },
                }
            ]
        }

    assert route_live_state_issues(identity, fanin_status=status(768)) == ()
    assert route_live_state_issues(identity, fanin_status=status(1024)) == (
        "live_fanin_direct_mismatch:usbsink:negotiated_buffer_frames",
    )


def test_route_live_state_issues_relaxes_only_on_an_explicitly_idle_lane():
    identity = {
        "fanin_direct_config": {
            "lane": "usbsink",
            "source": "direct",
            "device": "hw:UAC2Gadget",
            "period_frames": 256,
            "min_buffer_frames": 768,
            "buffer_period_aligned": True,
        },
        "fanin_resampler_config": {
            "enabled": True,
            "lane": "usbsink",
            "target_frames": 512,
            "warmup_cushion_frames": 1536,
        },
    }

    def status(health: str, *, target_fill_frames: int = 2048):
        return {
            "inputs": [
                {
                    "label": "usbsink",
                    "source": "direct",
                    "direct": {
                        "device": "hw:UAC2Gadget",
                        "health": health,
                        "period_frames": 256,
                        "buffer_frames": 768,
                    },
                    "resampler": {
                        "locked": False,
                        "target_fill_frames": target_fill_frames,
                    },
                }
            ]
        }

    # An explicitly idle lane relaxes the activity-dependent legs: an idle USB
    # host is not a broken route.
    assert route_live_state_issues(identity, fanin_status=status("idle")) == ()

    # Capturing is healthy but still requires the activity-dependent lock.
    assert route_live_state_issues(
        identity,
        fanin_status=status("capturing"),
    ) == ("live_fanin_resampler_unlocked:usbsink",)

    # Broken/unknown are neither healthy nor idle, so both failures remain.
    for health in ("broken", ""):
        assert route_live_state_issues(
            identity,
            fanin_status=status(health),
        ) == (
            f"live_fanin_direct_unhealthy:usbsink:{health or 'unknown'}",
            "live_fanin_resampler_unlocked:usbsink",
        )

    # The configured target is identity, not activity state, and must still
    # match while idle.
    assert route_live_state_issues(
        identity,
        fanin_status=status("idle", target_fill_frames=1536),
    ) == ("live_fanin_resampler_mismatch:usbsink:target_fill_frames",)


def test_chip_aec_readiness_snapshot_uses_schema_helper_and_passes():
    artifact = audio_validation.build_chip_aec_readiness_artifact(
        **_active_chip_inputs(),
    )

    assert isinstance(artifact, artifacts.ValidationArtifact)
    assert artifact.schema_version == artifacts.CURRENT_SCHEMA_VERSION
    assert artifact.profile == "xvf_chip_aec"
    assert artifact.status == "pass"
    assert artifact.mic_id == "xvf3800"
    assert artifact.dac_id == "apple_usb_c_dongle"
    assert artifact.checks["runtime_identity"]["status"] == "pass"
    assert artifact.checks["runtime_identity"]["required"] is False
    assert "system_hostname" in artifact.checks["runtime_identity"]["observed"]
    assert artifact.checks["runtime_profile"]["status"] == "pass"
    assert artifact.checks["runtime_env"]["observed"]["aec_device"] == "Array"
    assert artifact.checks["mic_detected"]["observed"]["alsa_card_name"] == "Array"
    assert artifact.checks["dac_support"]["status"] == "pass"
    assert artifact.checks["dac_reference"]["status"] == "pass"
    assert artifact.checks["wake_legs"]["status"] == "pass"
    # The readiness recommendation stays gated behind an explicit hardware
    # run even though every readiness check passes.
    assert artifact.recommendation == "run_hardware_validation"
    assert "readiness_snapshot" in artifact.notes[0]


def test_chip_aec_readiness_mic_detected_fails_for_unvalidated_beam_plan():
    """A registered-but-unvalidated beam plan must read as chip-AEC NOT
    detected, mirroring the doctor / /aec pin in test_control_aec_state.py.
    build_chip_aec_readiness_artifact reads MicProbe.chip_aec_supported
    directly rather than re-deriving bool(xvf_present and chip_beam_plan),
    which ignored production_validated."""
    inputs = _active_chip_inputs()
    inputs["mic_probe"] = MicProbe(
        xvf_present=True,
        capture_channels=6,
        recommended_channels=6,
        alsa_card_name="Array",
        variant_id="experimental_variant",
        geometry="square",
        chip_beam_plan="experimental_unvalidated",
        chip_aec_supported=False,
    )

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.checks["mic_detected"]["status"] == "fail"


def test_chip_aec_readiness_accepts_explicit_extra_wake_beams():
    inputs = _active_chip_inputs()
    inputs["system_env"] = {
        **inputs["system_env"],
        "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
        "JASPER_MIC_DEVICE_CHIP_AEC_210": "udp:9888",
    }
    inputs["voice_wake_legs"] = {"on", "chip_aec_150", "chip_aec_210"}

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.checks["wake_legs"]["status"] == "pass"
    assert artifact.checks["wake_legs"]["expected"] == [
        "chip_aec_150",
        "chip_aec_210",
        "on",
    ]


def test_chip_aec_readiness_fails_unexpected_extra_wake_beams():
    inputs = _active_chip_inputs()
    inputs["voice_wake_legs"] = {"on", "chip_aec_150"}

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.checks["wake_legs"]["status"] == "fail"
    assert artifact.checks["wake_legs"]["expected"] == ["on"]


def test_chip_aec_readiness_requires_calibrated_output_dac():
    inputs = _active_chip_inputs()
    inputs["system_env"] = {
        **inputs["system_env"],
        "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x_studio",
        "JASPER_AUDIO_DAC_CARD": "sndrpihifiberry",
    }

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.status == "fail"
    assert artifact.dac_id == "hifiberry_dac8x_studio"
    assert artifact.checks["dac_support"]["status"] == "fail"
    assert artifact.checks["dac_support"]["observed"]["status"] == "needs_calibration"
    # The record cannot contradict the verdict beside it: this check asks about
    # production approval, which an uncodified DAC fails even though ADR-0101
    # still lets an explicit testing request arm it.
    assert artifact.checks["dac_support"]["observed"]["permitted"] is False
    assert "needs per-profile chip-AEC" in artifact.checks["dac_support"]["summary"]
    assert artifact.recommendation == "calibrate_output_dac_before_chip_aec"


def test_chip_aec_readiness_treats_dac8x_as_approved_gate():
    inputs = _active_chip_inputs()
    inputs["system_env"] = {
        **inputs["system_env"],
        "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
        "JASPER_AUDIO_DAC_CARD": "sndrpihifiberry",
    }

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.checks["dac_support"]["status"] == "pass"
    assert artifact.checks["dac_support"]["observed"]["status"] == "approved"


def test_chip_aec_readiness_names_stale_saved_aec_card():
    inputs = _active_chip_inputs()
    inputs["system_env"] = {
        **inputs["system_env"],
        "JASPER_AEC_MIC_DEVICE": "L16K6Ch",
        "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        "JASPER_MIC_DEVICE_CHIP_AEC_150": "",
        "JASPER_MIC_DEVICE_CHIP_AEC_210": "",
    }
    inputs["service_states"] = {
        **inputs["service_states"],
        "jasper-aec-bridge.service": "inactive",
    }

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.status == "fail"
    runtime_profile = artifact.checks["runtime_profile"]
    assert runtime_profile["status"] == "fail"
    assert "configured AEC mic L16K6Ch" in str(runtime_profile["observed"])
    assert "detected XVF card Array" in str(runtime_profile["observed"])
    assert artifact.checks["runtime_env"]["observed"]["aec_device"] == "L16K6Ch"
    assert artifact.checks["mic_detected"]["observed"]["alsa_card_name"] == "Array"


def test_chip_aec_readiness_requires_validated_mic_beam_plan():
    inputs = _active_chip_inputs()
    inputs["mic_probe"] = MicProbe(
        xvf_present=True,
        capture_channels=6,
        recommended_channels=6,
        display_name="Seeed ReSpeaker Flex XVF3800 LINEAR-4",
        variant_id="xvf3800_flex_linear_6ch",
        geometry="linear",
        chip_beam_plan="",
    )
    inputs["system_env"] = {
        **inputs["system_env"],
        "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        "JASPER_MIC_DEVICE_CHIP_AEC_150": "",
        "JASPER_MIC_DEVICE_CHIP_AEC_210": "",
        "JASPER_XVF_VARIANT": "xvf3800_flex_linear_6ch",
        "JASPER_XVF_GEOMETRY": "linear",
        "JASPER_XVF_CHIP_BEAM_PLAN": "",
        "JASPER_XVF_CHIP_AEC_SUPPORTED": "0",
    }

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.status == "fail"
    assert artifact.checks["mic_detected"]["status"] == "fail"
    assert (
        "validated XVF3800 chip beam plan"
        in (artifact.checks["mic_detected"]["summary"])
    )
    assert artifact.checks["runtime_profile"]["status"] == "fail"


def test_chip_aec_readiness_fails_when_outputd_reference_missing():
    inputs = _active_chip_inputs()
    inputs["outputd_status"] = {
        "backend": "alsa",
        "dac": {"pcm": "outputd_dac", "sample_rate": 48000},
        "reference_outputs": {
            "speaker_reference_source": "outputd_final_electrical",
            "speaker_reference_is_fallback": False,
            "speaker_reference_active": False,
            "speaker_reference_sample_rate": 48000,
            "speaker_reference_channels": 2,
            "chip_ref_pcm": None,
            "chip_ref_sample_rate": 16000,
            "udp_target": None,
        },
    }

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.status == "fail"
    assert artifact.checks["dac_reference"]["status"] == "fail"
    assert artifact.recommendation == "fix_outputd_chip_reference_before_chip_aec"


def test_chip_aec_readiness_unknown_runtime_recommends_observability_fix():
    inputs = _active_chip_inputs()
    inputs["outputd_status"] = {}

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.status == "warn"
    assert artifact.checks["dac_reference"]["status"] == "unknown"
    assert (
        artifact.recommendation
        == "fix_runtime_observability_before_hardware_validation"
    )


def test_chip_aec_hardware_validation_clean_passive_evidence_still_recommends_drift_probe():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=14, dac_frames_written=5000),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        chip_readback=_chip_readback(),
        chip_convergence_polls=[
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [0]},
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
        ],
        duration_seconds=10,
    )

    assert artifact.status == "pass"
    assert artifact.checks["outputd_reference_health"]["status"] == "pass"
    assert artifact.checks["bridge_counter_window"]["status"] == "pass"
    assert artifact.checks["chip_profile_readback"]["status"] == "pass"
    assert artifact.checks["chip_convergence"]["status"] == "pass"
    # The hardware recommendation stays gated behind an explicit drift/delay
    # probe even though every sampled check passes.
    assert artifact.recommendation == "run_drift_delay_validation"
    assert "No playback stimulus was generated." in artifact.notes
    assert "No XVF chip settings were written or persisted." in artifact.notes


def test_outputd_stability_profile_passes_without_chip_aec_or_voice():
    inputs = _outputd_stability_inputs()
    artifact = audio_validation.build_outputd_stability_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=16, dac_frames_written=7000),
        ],
        duration_seconds=10,
    )

    assert artifact.profile == audio_validation.DAC8X_OUTPUTD_STABILITY_PROFILE
    assert artifact.status == "pass"
    assert artifact.mic_id == "not_applicable"
    assert artifact.dac_id == "hifiberry_dac8x"
    assert artifact.checks["service_state"]["status"] == "pass"
    assert artifact.checks["dac_identity"]["status"] == "pass"
    assert artifact.checks["dac_identity"]["observed"]["card"] == "sndrpihifiberry"
    assert "route" not in artifact.checks["dac_identity"]["observed"]
    assert artifact.checks["dac_output"]["status"] == "pass"
    assert artifact.checks["outputd_reference_health"]["status"] == "pass"
    assert "runtime_profile" not in artifact.checks
    assert "bridge_counter_window" not in artifact.checks
    assert "chip_profile_readback" not in artifact.checks
    assert artifact.recommendation == "outputd_dac_stability_validated"


def test_outputd_stability_profile_requires_dac8x_identity():
    inputs = _outputd_stability_inputs()
    inputs["system_env"] = {
        **inputs["system_env"],
        "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
    }
    artifact = audio_validation.build_outputd_stability_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=16, dac_frames_written=7000),
        ],
        duration_seconds=10,
    )

    assert artifact.status == "fail"
    assert artifact.dac_id == "apple_usb_c_dongle"
    assert artifact.checks["dac_identity"]["status"] == "fail"
    assert artifact.recommendation == "run_on_hifiberry_dac8x_target_before_validation"


def test_outputd_stability_profile_rejects_fallback_dac_card():
    inputs = _outputd_stability_inputs()
    inputs["system_env"] = {
        **inputs["system_env"],
        "JASPER_AUDIO_DAC_CARD": "A",
    }
    artifact = audio_validation.build_outputd_stability_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=16, dac_frames_written=7000),
        ],
        duration_seconds=10,
    )

    assert artifact.status == "fail"
    assert artifact.dac_id == "hifiberry_dac8x"
    assert artifact.checks["dac_identity"]["status"] == "fail"
    assert artifact.checks["dac_identity"]["observed"]["card"] == "A"
    assert artifact.recommendation == "run_on_hifiberry_dac8x_target_before_validation"


def test_outputd_stability_profile_accepts_string_sample_rate_from_status():
    inputs = _outputd_stability_inputs()
    artifact = audio_validation.build_outputd_stability_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            {
                **_outputd_sample(reference_sequence=10, dac_frames_written=1000),
                "dac": {"pcm": "outputd_dac", "sample_rate": "48000"},
            },
            _outputd_sample(reference_sequence=16, dac_frames_written=7000),
        ],
        duration_seconds=10,
    )

    assert artifact.checks["dac_output"]["status"] == "pass"
    assert artifact.checks["dac_output"]["observed"]["sample_rate"] == 48000


def test_chip_aec_hardware_validation_zero_convergence_is_not_observed():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=14, dac_frames_written=5000),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        chip_readback=_chip_readback(),
        chip_convergence_polls=[
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [0]},
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [0]},
        ],
        duration_seconds=10,
    )

    assert artifact.status == "warn"
    assert artifact.checks["chip_convergence"]["status"] == "not_observed"
    assert "nothing meaningful" in artifact.checks["chip_convergence"]["summary"]
    assert artifact.recommendation == "run_drift_delay_validation"


def test_chip_aec_hardware_validation_warns_when_convergence_is_lost():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=14, dac_frames_written=5000),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        chip_readback=_chip_readback(),
        chip_convergence_polls=[
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [0]},
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [0]},
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
        ],
        duration_seconds=10,
    )

    check = artifact.checks["chip_convergence"]
    assert artifact.status == "warn"
    assert check["status"] == "warn"
    assert "did not remain converged" in check["summary"]
    assert check["observed"]["first_converged_sample_index"] == 1
    assert check["observed"]["nonconverged_after_first_count"] == 1


def test_chip_aec_hardware_validation_fails_on_outputd_xrun_window():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_xruns=0),
            _outputd_sample(reference_sequence=14, dac_xruns=1),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        duration_seconds=10,
    )

    assert artifact.status == "fail"
    assert artifact.checks["outputd_reference_health"]["status"] == "fail"
    assert (
        artifact.recommendation == "fix_outputd_reference_health_before_chip_validation"
    )
    assert "outputd_reference_health" in artifact.errors[0]


def test_chip_aec_hardware_validation_gates_chip_poll_until_ref_health_passes():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10),
            _outputd_sample(reference_sequence=10),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        chip_readback=_chip_readback(),
        chip_convergence_polls=[
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
        ],
        duration_seconds=10,
    )

    assert artifact.status == "warn"
    assert artifact.checks["outputd_reference_health"]["status"] == "warn"
    assert artifact.checks["chip_profile_readback"]["status"] == "not_run"
    assert artifact.checks["chip_convergence"]["status"] == "not_run"
    assert (
        artifact.recommendation
        == "review_outputd_reference_health_before_chip_validation"
    )
