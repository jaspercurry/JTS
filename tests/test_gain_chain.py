from __future__ import annotations

from jasper.control.gain_chain import build_gain_chain_snapshot


def _stage_by_id(snapshot: dict, stage_id: str) -> dict:
    for stage in snapshot["stages"]:
        if stage["id"] == stage_id:
            return stage
    raise AssertionError(f"stage {stage_id!r} not found")


def test_gain_chain_surfaces_active_baseline_and_driver_trims(tmp_path):
    active_config = tmp_path / "active_speaker_baseline.yml"
    active_config.write_text(
        """
devices:
  volume_limit: 0.0
filters:
  active_baseline_headroom:
    type: Gain
    parameters: { gain: 0.0000, inverted: false, mute: false }
  as_woofer_baseline_gain:
    type: Gain
    parameters: { gain: 0.0000, inverted: false, mute: false }
  as_tweeter_baseline_gain:
    type: Gain
    parameters: { gain: -25.0000, inverted: false, mute: false }
  as_tweeter_baseline_limiter:
    type: Limiter
    parameters:
      soft_clip: true
      clip_limit: -1.0000
mixers:
  split_active_2way:
    channels: { in: 2, out: 2 }
    mapping:
      - dest: 0
        sources:
          - { channel: 0, gain: -6.0000, inverted: false }
          - { channel: 1, gain: -6.0000, inverted: false }
      - dest: 1
        sources:
          - { channel: 0, gain: -6.0000, inverted: false }
          - { channel: 1, gain: -6.0000, inverted: false }
""".strip()
    )

    snapshot = build_gain_chain_snapshot(
        active_source="airplay",
        volume_policy={
            "carrier": "camilla",
            "volume_mode": "local",
            "listening_level_percent": 81,
            "main_volume_db": -9.5,
        },
        camilla_status={
            "main_volume_db": -9.5,
            "active_config_path": str(active_config),
        },
        sound_profile=None,
        fanin_status=None,
        outputd_status=None,
    )

    assert snapshot["common_static_gain_db"] == -9.5
    assert snapshot["complete_common_static_gain"] is True
    assert snapshot["warnings"] == []

    user_volume = _stage_by_id(snapshot, "user_volume")
    assert user_volume["gain_db"] == -9.5
    assert user_volume["included_in_common_total"] is True

    baseline = _stage_by_id(snapshot, "active_baseline_headroom")
    assert baseline["gain_db"] == 0.0
    assert baseline["included_in_common_total"] is True

    mono = _stage_by_id(snapshot, "active_mono_fold_down")
    assert mono["gain_db"] == 0.0
    assert mono["included_in_common_total"] is True
    assert mono["details"]["source_gains_db"] == [-6.0, -6.0]

    tweeter_trim = _stage_by_id(snapshot, "as_tweeter_baseline_gain")
    assert tweeter_trim["gain_db"] == -25.0
    assert tweeter_trim["included_in_common_total"] is False
    assert tweeter_trim["scope"] == "driver"

    limiter = _stage_by_id(snapshot, "as_tweeter_baseline_limiter")
    assert limiter["dynamic"] is True
    assert limiter["nonlinear"] is True
    assert limiter["included_in_common_total"] is False

    volume_limit = _stage_by_id(snapshot, "camilla_volume_limit")
    assert volume_limit["gain_db"] is None
    assert volume_limit["details"]["ceiling_db"] == 0.0


def test_gain_chain_marks_source_owned_volume_as_unknown():
    snapshot = build_gain_chain_snapshot(
        active_source="spotify",
        volume_policy={
            "carrier": "source",
            "volume_mode": "push",
            "source": "spotify",
            "listening_level_percent": 64,
            "main_volume_db": None,
        },
        camilla_status={"main_volume_db": None, "active_config_path": None},
        sound_profile=None,
        fanin_status=None,
        outputd_status=None,
    )

    assert snapshot["common_static_gain_db"] == 0.0
    assert snapshot["complete_common_static_gain"] is False
    assert "source_volume_db_unknown" in snapshot["warnings"]
    source_stage = _stage_by_id(snapshot, "source_owned_volume")
    assert source_stage["gain_db"] is None
    assert source_stage["category"] == "source_volume"
    assert source_stage["details"]["listening_level_percent"] == 64


def test_gain_chain_lists_dynamic_tts_and_outputd_trims():
    snapshot = build_gain_chain_snapshot(
        active_source="voice",
        volume_policy={
            "carrier": "camilla",
            "volume_mode": "local",
            "listening_level_percent": 60,
            "main_volume_db": -18.0,
        },
        camilla_status={"main_volume_db": -18.0, "active_config_path": None},
        sound_profile=None,
        fanin_status={
            "tts": {
                "program_duck_active": True,
                "assistant_loudness": {
                    "decision_seen": True,
                    "final_gain_db": 3.25,
                    "requested_gain_db": 5.0,
                    "peak_cap_gain_db": 3.25,
                },
            },
        },
        outputd_status={
            "dac_content": {"enabled": True, "trim_db": -2.0},
        },
    )

    assert snapshot["common_static_gain_db"] == -20.0
    duck = _stage_by_id(snapshot, "fanin_tts_program_duck")
    assert duck["dynamic"] is True
    assert duck["gain_db"] is None
    assistant = _stage_by_id(snapshot, "fanin_assistant_loudness")
    assert assistant["scope"] == "assistant_tts"
    assert assistant["included_in_common_total"] is False
    output_trim = _stage_by_id(snapshot, "outputd_dac_content_trim")
    assert output_trim["gain_db"] == -2.0
    assert output_trim["included_in_common_total"] is True


def test_gain_chain_prefers_loaded_sound_preamp_over_state_estimate(tmp_path):
    sound_config = tmp_path / "sound_current.yml"
    sound_config.write_text(
        """
devices:
  volume_limit: 0.0
filters:
  sound_preamp:
    type: Gain
    parameters: { gain: -4.0000, inverted: false, mute: false }
  room_headroom:
    type: Gain
    parameters: { gain: -1.0000, inverted: false, mute: false }
""".strip()
    )

    snapshot = build_gain_chain_snapshot(
        active_source="airplay",
        volume_policy={
            "carrier": "camilla",
            "volume_mode": "local",
            "listening_level_percent": 80,
            "main_volume_db": -10.0,
        },
        camilla_status={
            "main_volume_db": -10.0,
            "active_config_path": str(sound_config),
        },
        sound_profile={
            "runtime_active": True,
            "headroom_db": 7.0,
            "output_trim_db": 4.0,
            "match_loudness": True,
            "headroom_trim_db": 0.0,
        },
        fanin_status=None,
        outputd_status=None,
    )

    assert snapshot["common_static_gain_db"] == -15.0
    assert _stage_by_id(snapshot, "sound_preamp")["gain_db"] == -4.0
    assert _stage_by_id(snapshot, "room_headroom")["gain_db"] == -1.0
    assert all(stage["id"] != "sound_output_trim" for stage in snapshot["stages"])
