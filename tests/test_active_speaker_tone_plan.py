from __future__ import annotations

from jasper.active_speaker import (
    TONE_PLAN_KIND,
    ActiveSpeakerPreset,
    build_safe_tone_plan,
    tone_targets_payload,
)


def _preset() -> ActiveSpeakerPreset:
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
            "fc_hz": 1600,
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
    assert plan["target"]["output_index"] == 1
    assert plan["tone"]["frequency_hz"] == 3200
    assert plan["tone"]["level_dbfs"] == -45.0
    assert plan["tone"]["duration_ms"] == 500
    assert plan["calibration_level"]["test_signal"]["requested_level_dbfs"] == -45.0
    assert plan["tone"]["band_limit"] == {
        "type": "highpass",
        "highpass_hz": 1600.0,
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
