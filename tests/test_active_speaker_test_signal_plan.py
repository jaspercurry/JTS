# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.active_speaker import (
    DRIVER_TEST_SIGNAL_PLAN_KIND,
    ActiveSpeakerPreset,
    driver_test_signal_plan,
    driver_test_signal_plan_from_edges,
)
from jasper.active_speaker.test_signal_plan import driver_sweep_duration_s


def _preset(*, fc_hz: float = 1600) -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "tone-plan-test-v1",
        "name": "Tone plan test preset",
        "way_count": 2,
        "channel_map": {
            "layout": "mono",
            "outputs": [
                {
                    "index": 0,
                    "side": "mono",
                    "driver_role": "woofer",
                    "label": "mono woofer",
                    "startup_muted": True,
                },
                {
                    "index": 1,
                    "side": "mono",
                    "driver_role": "tweeter",
                    "label": "mono tweeter",
                    "startup_muted": True,
                },
            ],
        },
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "tweeter": {"manufacturer": "Example", "model": "Tweeter"},
        },
        "crossover_regions": [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": fc_hz,
            "target_type": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "delay_range_ms": [0.0, 0.5],
            "null_depth_threshold_db": 25,
        }],
        "safety": {
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    })


def _three_way_preset(
    *,
    woofer_mid_hz: float = 300,
    mid_tweeter_hz: float = 3000,
) -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "tone-plan-3way-v1",
        "name": "Tone plan 3-way test preset",
        "way_count": 3,
        "channel_map": {
            "layout": "mono",
            "outputs": [
                {
                    "index": 0,
                    "side": "mono",
                    "driver_role": "woofer",
                    "label": "mono woofer",
                    "startup_muted": True,
                },
                {
                    "index": 1,
                    "side": "mono",
                    "driver_role": "mid",
                    "label": "mono mid",
                    "startup_muted": True,
                },
                {
                    "index": 2,
                    "side": "mono",
                    "driver_role": "tweeter",
                    "label": "mono tweeter",
                    "startup_muted": True,
                },
            ],
        },
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "mid": {"manufacturer": "Example", "model": "Mid"},
            "tweeter": {"manufacturer": "Example", "model": "Tweeter"},
        },
        "crossover_regions": [
            {
                "id": "woofer_mid",
                "lower_driver": "woofer",
                "upper_driver": "mid",
                "fc_hz": woofer_mid_hz,
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
                "fc_hz": mid_tweeter_hz,
                "target_type": "LinkwitzRiley",
                "order": 4,
                "lower_polarity": "non-inverted",
                "upper_polarity": "non-inverted",
                "delay_range_ms": [0.0, 0.5],
                "null_depth_threshold_db": 25,
            },
        ],
        "safety": {
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    })


def test_driver_test_signal_plan_two_way_uses_crossover_and_protection_edges() -> None:
    preset = _preset(fc_hz=2000)

    woofer = driver_test_signal_plan(preset, "woofer")
    tweeter = driver_test_signal_plan(preset, "tweeter")

    assert woofer["kind"] == DRIVER_TEST_SIGNAL_PLAN_KIND
    assert woofer["status"] == "ready"
    assert woofer["frequency_hz"] == 250.0
    assert woofer["selection_reason"] == "role_native_woofer_below_lowpass_edge"
    assert woofer["frequency_hz"] <= woofer["allowed_band"]["maximum_tone_hz"]
    assert woofer["allowed_band"]["lowpass_hz"] == 2000

    assert tweeter["status"] == "ready"
    assert tweeter["frequency_hz"] > 5000
    assert tweeter["frequency_hz"] != 5000
    assert tweeter["allowed_band"]["highpass_hz"] == 5000
    assert {edge["kind"] for edge in tweeter["allowed_band"]["edges"]} == {
        "crossover_highpass",
        "protective_tweeter_highpass",
        "driver_protection_minimum",
    }


def test_driver_test_signal_plan_three_way_places_each_role_in_its_band() -> None:
    preset = _three_way_preset(woofer_mid_hz=300, mid_tweeter_hz=3000)

    woofer = driver_test_signal_plan(preset, "woofer")
    mid = driver_test_signal_plan(preset, "mid")
    tweeter = driver_test_signal_plan(preset, "tweeter")

    assert woofer["status"] == "ready"
    assert woofer["frequency_hz"] == 120.0
    assert woofer["allowed_band"]["lowpass_hz"] == 300
    assert mid["status"] == "ready"
    assert mid["frequency_hz"] == 948.7
    assert mid["allowed_band"]["highpass_hz"] == 300
    assert mid["allowed_band"]["lowpass_hz"] == 3000
    assert tweeter["status"] == "ready"
    assert tweeter["frequency_hz"] > 6000
    assert tweeter["allowed_band"]["highpass_hz"] == 6000


def test_driver_test_signal_plan_subwoofer_stays_above_floor_and_below_lowpass() -> None:
    plan = driver_test_signal_plan_from_edges(
        "subwoofer",
        crossover_lowpass_hz=80,
        crossover_edge_source="future_subwoofer_compiled_edges",
    )

    assert plan["status"] == "ready"
    assert plan["allowed_band"]["highpass_hz"] == 25.0
    assert plan["allowed_band"]["lowpass_hz"] == 80.0
    assert plan["frequency_hz"] == 50.0
    assert plan["selection_reason"] == "role_native_subwoofer_tone"


def test_driver_test_signal_plan_blocks_impossibly_narrow_band() -> None:
    plan = driver_test_signal_plan(
        _three_way_preset(woofer_mid_hz=1000, mid_tweeter_hz=1100),
        "mid",
    )

    assert plan["status"] == "blocked"
    assert plan["frequency_hz"] is None
    assert "driver_test_signal_no_safe_band" in {
        issue["code"] for issue in plan["issues"]
    }


def test_driver_sweep_duration_is_longer_for_lf_and_bounded_for_tweeter() -> None:
    assert driver_sweep_duration_s("subwoofer") == 12.0
    assert driver_sweep_duration_s("woofer") == 12.0
    assert driver_sweep_duration_s("mid") == 8.0
    assert driver_sweep_duration_s("tweeter") == 4.0
    assert driver_sweep_duration_s("future_role") == 6.0
