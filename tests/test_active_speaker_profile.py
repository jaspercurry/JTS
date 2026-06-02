from __future__ import annotations

import pytest

from jasper.active_speaker import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    BaselineVerification,
    SpeakerBaselineProfile,
    emit_active_speaker_startup_config,
)


def _two_way_preset(layout: str = "mono") -> dict:
    sides = ["mono"] if layout == "mono" else ["left", "right"]
    outputs = []
    index = 0
    for side in sides:
        for role in ("woofer", "tweeter"):
            outputs.append({
                "index": index,
                "side": side,
                "driver_role": role,
                "label": f"{side} {role}",
                "startup_muted": True,
            })
            index += 1
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "DE250-E150HE44-v1",
        "name": "DE250 + E150HE-44 test preset",
        "way_count": 2,
        "channel_map": {
            "layout": layout,
            "outputs": outputs,
        },
        "drivers": {
            "woofer": {
                "manufacturer": "Dayton Audio",
                "model": "Epique E150HE-44",
                "fs_hz": 40,
                "rated_power_w": 60,
            },
            "tweeter": {
                "manufacturer": "B&C Speakers",
                "model": "DE250",
                "rated_power_w": 60,
            },
        },
        "crossover_regions": [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": 1600,
            "target_type": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "delay_target_driver": "woofer",
            "delay_range_ms": [0.05, 0.30],
            "null_depth_threshold_db": 25,
        }],
        "safety": {
            "initial_sweep_level_db_spl": 65,
            "max_commissioning_level_db_spl": 85,
            "escalation_step_db": 5,
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    }


def _three_way_preset(layout: str = "stereo") -> dict:
    sides = ["mono"] if layout == "mono" else ["left", "right"]
    outputs = []
    index = 0
    for side in sides:
        for role in ("woofer", "mid", "tweeter"):
            outputs.append({
                "index": index,
                "side": side,
                "driver_role": role,
                "label": f"{side} {role}",
                "startup_muted": True,
            })
            index += 1
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "test-3way-v1",
        "name": "Test 3-way preset",
        "way_count": 3,
        "channel_map": {"layout": layout, "outputs": outputs},
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "mid": {"manufacturer": "Example", "model": "Midrange"},
            "tweeter": {"manufacturer": "Example", "model": "Tweeter"},
        },
        "crossover_regions": [
            {
                "id": "woofer_mid",
                "lower_driver": "woofer",
                "upper_driver": "mid",
                "fc_hz": 350,
                "target_type": "LinkwitzRiley",
                "order": 4,
                "lower_polarity": "non-inverted",
                "upper_polarity": "non-inverted",
                "delay_range_ms": [0.0, 0.5],
                "null_depth_threshold_db": 25,
            },
            {
                "id": "mid_tweeter",
                "lower_driver": "mid",
                "upper_driver": "tweeter",
                "fc_hz": 2500,
                "target_type": "LinkwitzRiley",
                "order": 4,
                "lower_polarity": "non-inverted",
                "upper_polarity": "non-inverted",
                "delay_range_ms": [0.0, 0.5],
                "null_depth_threshold_db": 25,
            },
        ],
        "safety": _two_way_preset()["safety"],
    }


def test_two_way_active_speaker_preset_round_trips():
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())

    assert preset.way_count == 2
    assert preset.channel_map.layout == "mono"
    assert [o.driver_role for o in preset.channel_map.outputs] == ["woofer", "tweeter"]
    assert preset.crossover_regions[0].fc_hz == 1600

    assert ActiveSpeakerPreset.from_mapping(preset.to_dict()) == preset


def test_stereo_channel_map_requires_each_driver_on_each_side():
    raw = _two_way_preset(layout="stereo")
    raw["channel_map"]["outputs"] = raw["channel_map"]["outputs"][:-1]

    with pytest.raises(ActiveSpeakerConfigError, match="missing output channels"):
        ActiveSpeakerPreset.from_mapping(raw)


def test_crossover_regions_must_match_adjacent_driver_pairs():
    raw = _two_way_preset()
    raw["crossover_regions"][0]["lower_driver"] = "tweeter"
    raw["crossover_regions"][0]["upper_driver"] = "woofer"

    with pytest.raises(ActiveSpeakerConfigError, match="crossover regions"):
        ActiveSpeakerPreset.from_mapping(raw)


def test_safety_envelope_rejects_levels_above_commissioning_cap():
    raw = _two_way_preset()
    raw["safety"]["max_commissioning_level_db_spl"] = 95

    with pytest.raises(ActiveSpeakerConfigError, match="max commissioning"):
        ActiveSpeakerPreset.from_mapping(raw)


def test_channel_map_requires_muted_startup_outputs():
    raw = _two_way_preset()
    raw["channel_map"]["outputs"][1]["startup_muted"] = False

    with pytest.raises(ActiveSpeakerConfigError, match="must start muted"):
        ActiveSpeakerPreset.from_mapping(raw)


def test_channel_map_requires_contiguous_camilla_output_indexes():
    raw = _two_way_preset()
    raw["channel_map"]["outputs"][1]["index"] = 3

    with pytest.raises(ActiveSpeakerConfigError, match="contiguous"):
        ActiveSpeakerPreset.from_mapping(raw)


def test_driver_polarity_must_be_consistent_across_regions():
    raw = _three_way_preset()
    raw["crossover_regions"][1]["lower_polarity"] = "inverted"

    with pytest.raises(ActiveSpeakerConfigError, match="inconsistent polarity"):
        ActiveSpeakerPreset.from_mapping(raw)


def test_safety_envelope_requires_physical_tweeter_protection_for_now():
    raw = _two_way_preset()
    raw["safety"]["require_physical_tweeter_protection"] = False

    with pytest.raises(ActiveSpeakerConfigError, match="physical tweeter protection"):
        ActiveSpeakerPreset.from_mapping(raw)


def test_safety_envelope_rejects_malformed_section():
    raw = _two_way_preset()
    raw["safety"] = "safe enough"

    with pytest.raises(ActiveSpeakerConfigError, match="safety must be an object"):
        ActiveSpeakerPreset.from_mapping(raw)


def test_baseline_verification_rejects_malformed_section():
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    raw = SpeakerBaselineProfile.from_preset(
        preset,
        baseline_id="baseline-test",
    ).to_dict()
    raw["verification"] = "done"

    with pytest.raises(ActiveSpeakerConfigError, match="verification must be an object"):
        SpeakerBaselineProfile.from_mapping(raw)


def test_commissioned_baseline_requires_measurement_evidence():
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())

    with pytest.raises(ActiveSpeakerConfigError, match="commissioned baseline"):
        SpeakerBaselineProfile.from_preset(
            preset,
            baseline_id="baseline-test",
            status="commissioned",
        )

    baseline = SpeakerBaselineProfile.from_preset(
        preset,
        baseline_id="baseline-test",
        status="commissioned",
        verification=BaselineVerification(
            channel_identity_verified=True,
            all_paths_protected=True,
            per_driver_measurements_captured=True,
            crossover_nulls_captured=True,
            gated_sum_captured=True,
        ),
    )
    assert baseline.status == "commissioned"
    assert SpeakerBaselineProfile.from_mapping(baseline.to_dict()) == baseline


def test_active_startup_config_requires_explicit_active_playback_device():
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())

    with pytest.raises(ActiveSpeakerConfigError, match="explicit active playback"):
        emit_active_speaker_startup_config(
            preset,
            playback_device="outputd_content_playback",
        )


def test_active_startup_config_rejects_positive_volume_limit():
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())

    with pytest.raises(ActiveSpeakerConfigError, match="volume_limit_db"):
        emit_active_speaker_startup_config(
            preset,
            playback_device="hw:ActiveDAC",
            volume_limit_db=1.0,
        )


def test_two_way_active_startup_config_is_muted_and_protected():
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    yaml = emit_active_speaker_startup_config(
        preset,
        playback_device="hw:ActiveDAC",
        baseline_id="baseline-test",
    )

    assert (
        "Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_startup_config"
    ) in yaml
    assert "preset_id=DE250-E150HE44-v1" in yaml
    assert "baseline_id=baseline-test" in yaml
    assert "volume_limit: 0.0" in yaml
    assert 'device: "hw:ActiveDAC"' in yaml
    assert "channels: 2" in yaml
    assert 'labels: ["mono woofer", "mono tweeter"]' in yaml
    assert "active_startup_headroom:" in yaml
    assert "gain: -40.0000" in yaml
    assert "as_tweeter_protective_hp:" in yaml
    assert "freq: 3200.0000" in yaml
    assert yaml.count("type: Limiter") == 2
    assert yaml.count("mute: true") == 2
    assert "clip_limit: -12.0000" in yaml
    assert (
        "names: [as_woofer_woofer_tweeter_lp, as_woofer_delay, "
        "as_woofer_startup_mute, as_woofer_startup_limiter]"
    ) in yaml
    assert (
        "names: [as_tweeter_protective_hp, as_tweeter_woofer_tweeter_hp, "
        "as_tweeter_delay, as_tweeter_startup_mute, "
        "as_tweeter_startup_limiter]"
    ) in yaml


def test_three_way_active_startup_config_bandpasses_midrange():
    preset = ActiveSpeakerPreset.from_mapping(_three_way_preset())
    yaml = emit_active_speaker_startup_config(
        preset,
        playback_device="hw:SixChannelDAC",
    )

    assert "channels: 6" in yaml
    assert "channels: { in: 2, out: 6 }" in yaml
    assert "as_woofer_woofer_mid_lp:" in yaml
    assert "as_mid_woofer_mid_hp:" in yaml
    assert "as_mid_mid_tweeter_lp:" in yaml
    assert "as_tweeter_mid_tweeter_hp:" in yaml
    assert (
        "names: [as_mid_woofer_mid_hp, as_mid_mid_tweeter_lp, "
        "as_mid_delay, as_mid_startup_mute, as_mid_startup_limiter]"
    ) in yaml
    assert yaml.count("type: Limiter") == 3
