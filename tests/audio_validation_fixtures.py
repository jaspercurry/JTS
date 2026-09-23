# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import datetime, timezone

from jasper.audio_hardware import dac
from jasper.audio_profile_state import MicProbe


NOW = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)


def _active_chip_inputs() -> dict:
    return {
        "now": NOW,
        "mode_env": {
            "JASPER_AEC_MODE": "auto",
            "JASPER_WAKE_LEG_RAW": "1",
            "JASPER_WAKE_LEG_DTLN": "0",
            "JASPER_WAKE_LEG_CHIP_AEC": "1",
        },
        "system_env": {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS": "ready",
            "JASPER_XVF_VARIANT": "xvf3800_legacy_square_6ch",
            "JASPER_XVF_GEOMETRY": "square",
            "JASPER_XVF_CHIP_BEAM_PLAN": "xvf_square_fixed_150_210",
            "JASPER_XVF_CHIP_AEC_SUPPORTED": "1",
            "JASPER_OUTPUTD_BACKEND": "alsa",
            "JASPER_OUTPUTD_DAC_PCM": "outputd_dac",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
        },
        "mic_probe": MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
            chip_aec_supported=True,
        ),
        "service_states": {
            "jasper-outputd.service": "active",
            "jasper-aec-bridge.service": "active",
            "jasper-aec-init.service": "active",
            "jasper-voice.service": "active",
        },
        "outputd_status": {
            "backend": "alsa",
            "dac": {"pcm": "outputd_dac", "sample_rate": 48000},
            "reference_outputs": {
                "speaker_reference_source": "outputd_final_electrical",
                "speaker_reference_is_fallback": False,
                "speaker_reference_active": True,
                "speaker_reference_sample_rate": 48000,
                "speaker_reference_channels": 2,
                "chip_ref_pcm": "hw:CARD=Array,DEV=0",
                "chip_ref_sample_rate": 16000,
                "chip_ref_period_frames": 128,
                "chip_ref_buffer_frames": 256,
                "chip_ref_transform": "stereo_mean_boxcar_decimate_dual_mono_v1",
                "udp_target": "127.0.0.1:9891",
            },
        },
        "bridge_stats": {
            "schema_version": 1,
            "updated_epoch_sec": NOW.timestamp(),
            "counters": {
                "frames_processed": 42,
                "ref_starved_frames": 0,
                "queue_drops": {"mic": 0, "chip": 0, "raw0": 0, "usb": 0, "ref": 0},
                "udp_send_drops_by_leg": {"on": 0},
                "packets_sent_by_leg": {"on": 10},
            },
        },
        "voice_wake_legs": {"on"},
    }


def _outputd_sample(
    *,
    reference_sequence: int,
    dac_frames_written: int = 48_000,
    dac_xruns: int = 0,
    content_xruns: int = 0,
    clipped_samples: int = 0,
    progress_age_ms: int = 20,
) -> dict:
    sample = dict(_active_chip_inputs()["outputd_status"])
    sample.update(
        {
            "content": {"xrun_count": content_xruns},
            "dac": {
                "pcm": "outputd_dac",
                "sample_rate": 48000,
                "frames_written": dac_frames_written,
                "xrun_count": dac_xruns,
            },
            "mix": {
                "reference_sequence": reference_sequence,
                "clipped_samples": clipped_samples,
            },
            "watchdog": {"last_progress_age_ms": progress_age_ms},
        }
    )
    return sample


def _bridge_sample(
    *,
    frames_processed: int,
    ref_starved_frames: int = 0,
    queue_drops: int = 0,
    udp_drops: int = 0,
) -> dict:
    return {
        "schema_version": 1,
        "updated_epoch_sec": NOW.timestamp(),
        "counters": {
            "frames_processed": frames_processed,
            "ref_starved_frames": ref_starved_frames,
            "queue_drops": {
                "mic": queue_drops,
                "chip": 0,
                "raw0": 0,
                "usb": 0,
                "ref": 0,
            },
            "udp_send_drops_by_leg": {
                "on": udp_drops,
                "chip_aec_150": 0,
                "chip_aec_210": 0,
            },
            "packets_sent_by_leg": {"on": 10, "chip_aec_150": 10, "chip_aec_210": 10},
        },
    }


def _chip_readback(sys_delay: int = 12) -> dict:
    return {
        "SHF_BYPASS": [0],
        "AUDIO_MGR_SYS_DELAY": [sys_delay],
        "AEC_ASROUTONOFF": [1],
        "AEC_FIXEDBEAMSONOFF": [1],
        "AEC_FIXEDBEAMSGATING": [1],
    }


def _outputd_stability_inputs() -> dict:
    return {
        "now": NOW,
        "system_env": {
            "JASPER_OUTPUTD_BACKEND": "alsa",
            "JASPER_OUTPUTD_DAC_PCM": "outputd_dac",
            "JASPER_AUDIO_DAC_ID": dac.HIFIBERRY_DAC8X_ID,
            "JASPER_AUDIO_DAC_CARD": "sndrpihifiberry",
        },
        "service_states": {
            "jasper-outputd.service": "active",
            "jasper-camilla.service": "active",
            "jasper-fanin.service": "active",
            "jasper-aec-bridge.service": "inactive",
            "jasper-aec-init.service": "inactive",
            "jasper-voice.service": "inactive",
        },
    }
