"""Single-audio-path commissioning config emission.

The active-crossover flow validates THROUGH the production graph, not a
separate diagnostic path: `emit_active_speaker_commissioning_config` emits the
real protected graph with a per-OUTPUT mute mask so one physical driver at a
time is excited through its actual crossover/limiter chain. These tests pin the
mask behavior and that production safety (0 dB ceiling, protective tweeter
high-pass, per-driver limiters) is preserved.
"""
from __future__ import annotations

import pytest
import yaml as yaml_lib

from jasper.active_speaker import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    audible_outputs_for_role,
    emit_active_speaker_commissioning_config,
)

# Reuse the canonical preset fixtures.
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def _preset(builder) -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(builder)


def _mutes(parsed: dict) -> dict[int, bool]:
    """Map output index -> its commission-mute `mute` flag from parsed YAML."""
    out: dict[int, bool] = {}
    for name, spec in parsed["filters"].items():
        if "_commission_mute" not in name:
            continue
        index = int(name.split("as_out")[1].split("_")[0])
        out[index] = bool(spec["parameters"]["mute"])
    return out


def test_commissioning_config_isolates_one_output():
    preset = _preset(_two_way_preset("stereo"))  # 4 outputs: 0..3
    out = emit_active_speaker_commissioning_config(
        preset, playback_device=ACTIVE_PCM, audible_outputs={0}
    )
    parsed = yaml_lib.safe_load(out)
    mutes = _mutes(parsed)
    assert mutes == {0: False, 1: True, 2: True, 3: True}


def test_commissioning_config_empty_mask_is_fully_muted():
    preset = _preset(_two_way_preset("stereo"))
    out = emit_active_speaker_commissioning_config(
        preset, playback_device=ACTIVE_PCM
    )
    mutes = _mutes(yaml_lib.safe_load(out))
    assert set(mutes.values()) == {True}
    assert len(mutes) == 4


def test_commissioning_config_full_mask_all_audible():
    preset = _preset(_three_way_preset("stereo"))  # 6 outputs
    out = emit_active_speaker_commissioning_config(
        preset, playback_device=ACTIVE_PCM, audible_outputs={0, 1, 2, 3, 4, 5}
    )
    mutes = _mutes(yaml_lib.safe_load(out))
    assert set(mutes.values()) == {False}
    assert len(mutes) == 6


def test_commissioning_config_preserves_production_safety():
    preset = _preset(_two_way_preset("stereo"))
    out = emit_active_speaker_commissioning_config(
        preset, playback_device=ACTIVE_PCM, audible_outputs={1}
    )
    parsed = yaml_lib.safe_load(out)
    # 0 dB ceiling preserved.
    assert parsed["devices"]["volume_limit"] == 0.0
    # No per-role startup mute survives (isolation is per-output now).
    assert not any("_startup_mute" in name for name in parsed["filters"])
    # Protective tweeter high-pass is present AND correct: a Linkwitz-Riley
    # high-pass an octave above the crossover (1600 Hz * 2.0 = 3200 Hz). Assert
    # the parameters, not just the name — a HP at the wrong frequency is a
    # driver-damage hazard the name alone would not catch.
    hp = parsed["filters"]["as_tweeter_protective_hp"]["parameters"]
    assert hp["type"] == "LinkwitzRileyHighpass"
    assert hp["freq"] == 3200.0
    assert hp["order"] == 4
    # Per-driver limiters preserved at the startup clip ceiling.
    for role in ("woofer", "tweeter"):
        limiter = parsed["filters"][f"as_{role}_startup_limiter"]["parameters"]
        assert limiter["clip_limit"] == -12.0
        assert limiter["soft_clip"] is True


def test_commissioning_config_rejects_outputd_lane():
    preset = _preset(_two_way_preset())
    # The existing stereo content lane must never be used as an active playback
    # device by accident (same guard as the startup/baseline emitters).
    for device in ("plug:jasper_out", "jasper_out"):
        with pytest.raises(ActiveSpeakerConfigError, match="existing .* lane"):
            emit_active_speaker_commissioning_config(
                preset, playback_device=device, audible_outputs={0}
            )


def test_commissioning_config_rejects_out_of_range_output():
    preset = _preset(_two_way_preset())  # mono 2-way: 2 outputs
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_commissioning_config(
            preset, playback_device=ACTIVE_PCM, audible_outputs={5}
        )


def test_commissioning_config_rejects_positive_volume_limit():
    preset = _preset(_two_way_preset())
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_commissioning_config(
            preset, playback_device=ACTIVE_PCM, audible_outputs={0},
            volume_limit_db=3.0,
        )


def test_audible_outputs_for_role_groups_both_sides():
    preset = _preset(_two_way_preset("stereo"))
    # Stereo 2-way: woofers on 0 and 2, tweeters on 1 and 3.
    assert audible_outputs_for_role(preset, "woofer") == frozenset({0, 2})
    assert audible_outputs_for_role(preset, "tweeter") == frozenset({1, 3})


def test_commissioning_config_is_parseable_yaml_for_all_layouts():
    for builder in (_two_way_preset("mono"), _two_way_preset("stereo"),
                    _three_way_preset("stereo")):
        preset = _preset(builder)
        out = emit_active_speaker_commissioning_config(
            preset, playback_device=ACTIVE_PCM, audible_outputs={0}
        )
        parsed = yaml_lib.safe_load(out)
        assert "filters" in parsed and "mixers" in parsed and "pipeline" in parsed
