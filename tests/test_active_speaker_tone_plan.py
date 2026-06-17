from __future__ import annotations

from jasper.active_speaker import (
    DRIVER_TEST_SIGNAL_PLAN_KIND,
    TONE_PLAN_KIND,
    ActiveSpeakerPreset,
    build_safe_tone_plan,
    driver_test_signal_plan,
    driver_test_signal_plan_from_edges,
    tone_targets_payload,
)
from jasper.active_speaker.calibration_level import MAX_TEST_LEVEL_DBFS


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


def _environment(*, ok: bool) -> dict:
    return {
        "status": "pass" if ok else "blocked",
        "load_gate": "ready" if ok else "path_safety_evidence_missing",
        "ok_to_load_active_config": ok,
        "camilla_config": {
            "classification": "active_startup_candidate",
            "path": "/tmp/active.yml",
        },
        "safe_playback": {
            "status": "not_implemented",
            "playback_allowed": False,
        },
        "issues": [],
    }


def _armed_session() -> dict:
    return {"status": "armed", "session_id": "session-test"}


def test_tone_targets_payload_is_preset_derived() -> None:
    payload = tone_targets_payload(_preset())

    assert payload["preset_id"] == "tone-plan-test-v1"
    assert payload["calibration_level"]["test_signal"]["default_level_dbfs"] == -80.0
    assert [target["driver_role"] for target in payload["targets"]] == [
        "woofer",
        "tweeter",
    ]


def test_build_safe_tone_plan_ready_still_cannot_play() -> None:
    plan = build_safe_tone_plan(
        _preset(),
        safe_session=_armed_session(),
        environment_report=_environment(ok=True),
        side="mono",
        driver_role="tweeter",
        requested_level_dbfs=-10,
        requested_duration_ms=5000,
    )

    assert plan["kind"] == TONE_PLAN_KIND
    assert plan["status"] == "ready"
    assert plan["would_play"] is False
    assert plan["playback_allowed"] is False
    assert plan["tone_playback_implemented"] is False
    assert plan["channel_map"] == {"layout": "mono", "output_count": 2}
    assert plan["target"]["output_index"] == 1
    assert plan["tone"]["frequency_hz"] == 6250.0
    assert plan["tone"]["level_dbfs"] == MAX_TEST_LEVEL_DBFS
    assert plan["tone"]["duration_ms"] == 500
    assert (
        plan["calibration_level"]["test_signal"]["requested_level_dbfs"]
        == MAX_TEST_LEVEL_DBFS
    )
    assert plan["tone"]["band_limit"] == {
        "type": "highpass",
        "highpass_hz": 5000.0,
    }
    assert plan["tone"]["signal_plan"]["selection_reason"] == (
        "above_strictest_highpass_edge"
    )
    assert plan["driver_protection"]["min_highpass_hz"] == 5000.0
    assert plan["driver_protection"]["max_auto_level_dbfs"] == -65.0


def test_driver_test_signal_plan_two_way_uses_crossover_and_protection_edges() -> None:
    preset = _preset(fc_hz=2000)

    woofer = driver_test_signal_plan(preset, "woofer")
    tweeter = driver_test_signal_plan(preset, "tweeter")

    assert woofer["kind"] == DRIVER_TEST_SIGNAL_PLAN_KIND
    assert woofer["status"] == "ready"
    assert woofer["frequency_hz"] < 2000
    assert woofer["frequency_hz"] <= woofer["allowed_band"]["maximum_tone_hz"]
    assert woofer["allowed_band"]["lowpass_hz"] == 2000

    assert tweeter["status"] == "ready"
    assert tweeter["frequency_hz"] > 5000
    assert tweeter["frequency_hz"] != 5000
    assert tweeter["allowed_band"]["highpass_hz"] == 5000
    assert {
        edge["kind"] for edge in tweeter["allowed_band"]["edges"]
    } == {
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
    assert woofer["frequency_hz"] < 300
    assert woofer["allowed_band"]["lowpass_hz"] == 300

    assert mid["status"] == "ready"
    assert 300 < mid["frequency_hz"] < 3000
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
    assert 25 < plan["frequency_hz"] < 80
    assert plan["frequency_hz"] == 44.7
    assert {
        edge["kind"] for edge in plan["allowed_band"]["edges"]
    } == {"subwoofer_subsonic_floor", "crossover_lowpass"}


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


def test_build_safe_tone_plan_defaults_to_minimum_level() -> None:
    plan = build_safe_tone_plan(
        _preset(),
        safe_session=_armed_session(),
        environment_report=_environment(ok=True),
        side="mono",
        driver_role="woofer",
    )

    assert plan["tone"]["level_dbfs"] == -80.0
    assert plan["calibration_level"]["test_signal"]["requested_level_dbfs"] == -80.0


def test_build_safe_tone_plan_blocks_without_armed_session() -> None:
    plan = build_safe_tone_plan(
        _preset(),
        safe_session={"status": "idle"},
        environment_report=_environment(ok=True),
        side="mono",
        driver_role="woofer",
    )

    assert plan["status"] == "blocked"
    assert "safe_session_not_armed" in {issue["code"] for issue in plan["issues"]}
    assert plan["would_play"] is False


def test_build_safe_tone_plan_requires_explicit_target() -> None:
    plan = build_safe_tone_plan(
        _preset(),
        safe_session=_armed_session(),
        environment_report=_environment(ok=True),
    )

    assert plan["status"] == "blocked"
    assert "target_output_required" in {issue["code"] for issue in plan["issues"]}
    assert plan["target"]["output_index"] is None


def test_build_safe_tone_plan_blocks_when_environment_is_not_ready() -> None:
    plan = build_safe_tone_plan(
        _preset(),
        safe_session=_armed_session(),
        environment_report=_environment(ok=False),
        side="mono",
        driver_role="woofer",
    )

    assert plan["status"] == "blocked"
    assert "active_environment_not_ready" in {
        issue["code"] for issue in plan["issues"]
    }


def test_build_safe_tone_plan_blocks_unknown_target() -> None:
    plan = build_safe_tone_plan(
        _preset(),
        safe_session=_armed_session(),
        environment_report=_environment(ok=True),
        side="mono",
        driver_role="mid",
    )

    assert plan["status"] == "blocked"
    assert "target_output_not_found" in {issue["code"] for issue in plan["issues"]}
