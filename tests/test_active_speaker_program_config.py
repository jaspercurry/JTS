# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-owned protected-neutral program graph emission (R15).

The conductor plays three-channel pre-apply WAVs through one static graph:
ch0 woofer, ch1 tweeter, ch2 summed. These tests pin routing, the declared
protection-only filter set on the physical output channels, the 0 dB ceiling,
the build-time protective-floor gate, and the build-and-prove return contract —
including the adversarial pre-split-HP shape that ``tweeter_guard_present`` must
reject even where ``output_highpass_protected`` alone would false-PASS.
"""
from __future__ import annotations

import pytest
import yaml as yaml_lib

from jasper.active_speaker import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    emit_active_speaker_program_config,
)
from jasper.active_speaker.camilla_yaml import (
    _assert_program_graph_proven,
    _driver_limiter_name,
)
from jasper.active_speaker.graph_safety import (
    output_highpass_protected,
    tweeter_guard_present,
    unprotected_tweeter_outputs,
    view_from_emitted_text,
)
from jasper.audio_measurement.transfer_composition import LinearTransferSection

# Reuse the canonical preset fixtures (mono 2-way == JTS3 single cabinet:
# output 0 = woofer, output 1 = tweeter).
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"
ROLE_CHANNELS = {"woofer": 0, "tweeter": 1}
PROTECTION = {
    "woofer": (
        LinearTransferSection(True, 150.0, reason="declared_hard_excitation_floor"),
        LinearTransferSection(False, 6000.0, reason="declared_hard_excitation_ceiling"),
    ),
    "tweeter": (
        LinearTransferSection(True, 1600.0, reason="declared_hard_excitation_floor"),
        LinearTransferSection(False, 23000.0, reason="declared_hard_excitation_ceiling"),
    ),
}


def _emit(preset: ActiveSpeakerPreset, **kwargs) -> str:
    return emit_active_speaker_program_config(
        preset,
        role_channels=kwargs.pop("role_channels", ROLE_CHANNELS),
        summed_channel=2,
        measurement_protection_by_role=PROTECTION,
        playback_device=kwargs.pop("playback_device", ACTIVE_PCM),
        **kwargs,
    )


def _preset(layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset(layout))


def _low_fc_preset() -> ActiveSpeakerPreset:
    """A 2-way preset whose tweeter crosses too low for the protective floor."""
    builder = _two_way_preset("mono")
    builder["crossover_regions"][0]["fc_hz"] = 300  # < TWEETER_PROTECTIVE_HP floor
    return ActiveSpeakerPreset.from_mapping(builder)


def test_program_config_routes_each_channel_to_its_driver_output():
    preset = _preset("mono")
    out = _emit(preset)
    parsed = yaml_lib.safe_load(out)

    # Capture is the program channel count; playback the physical output count.
    assert parsed["devices"]["capture"]["channels"] == 3
    assert parsed["devices"]["volume_limit"] == 0.0

    mixer = parsed["mixers"]["split_active_2way"]
    routing = {
        entry["dest"]: [s["channel"] for s in entry["sources"]]
        for entry in mixer["mapping"]
    }
    # ch0 -> woofer output 0, ch1 -> tweeter output 1 (role-routed, not side-routed).
    assert routing == {0: [0, 2], 1: [1, 2]}


def test_program_config_carries_only_declared_measurement_protection():
    preset = _preset("mono")
    out = _emit(preset)
    parsed = yaml_lib.safe_load(out)

    assert "as_tweeter_protective_hp" not in parsed["filters"]
    assert "as_tweeter_woofer_tweeter_hp" not in parsed["filters"]
    hp = parsed["filters"]["as_tweeter_measure_p0_hp"]["parameters"]
    assert hp == {"type": "LinkwitzRileyHighpass", "freq": 1600.0, "order": 4}
    assert parsed["filters"]["as_tweeter_measure_p1_lp"]["parameters"] == {
        "type": "LinkwitzRileyLowpass",
        "freq": 23000.0,
        "order": 4,
    }
    assert parsed["filters"]["as_woofer_measure_p0_hp"]["parameters"] == {
        "type": "LinkwitzRileyHighpass",
        "freq": 150.0,
        "order": 4,
    }
    assert parsed["filters"]["as_woofer_measure_p1_lp"]["parameters"] == {
        "type": "LinkwitzRileyLowpass",
        "freq": 6000.0,
        "order": 4,
    }
    assert parsed["filters"]["as_woofer_delay"]["parameters"]["delay"] == 0
    assert parsed["filters"]["as_tweeter_delay"]["parameters"]["delay"] == 0
    assert set(parsed["filters"]) == {
        "active_startup_headroom",
        "as_woofer_measure_p0_hp",
        "as_woofer_measure_p1_lp",
        "as_woofer_delay",
        "as_woofer_startup_limiter",
        "as_tweeter_measure_p0_hp",
        "as_tweeter_measure_p1_lp",
        "as_tweeter_delay",
        "as_tweeter_startup_limiter",
        "as_out0_commission_mute",
        "as_out1_commission_mute",
    }
    # Program headroom is the commissioning headroom (0 dB) so the effective-peak
    # ledger is main_volume + program peak with no hidden graph attenuation.
    assert parsed["filters"]["active_startup_headroom"]["parameters"]["gain"] in (
        0.0,
        -0.0,
    )


def test_program_config_preserves_declared_physical_polarity_only():
    raw = _two_way_preset("mono")
    raw["crossover_regions"][0]["upper_polarity"] = "inverted"
    parsed = yaml_lib.safe_load(_emit(ActiveSpeakerPreset.from_mapping(raw)))
    mapping = parsed["mixers"]["split_active_2way"]["mapping"]
    tweeter_sources = next(item for item in mapping if item["dest"] == 1)["sources"]
    assert [source["inverted"] for source in tweeter_sources] == [True, True]


def test_program_config_passes_all_graph_safety_proofs():
    preset = _preset("mono")
    out = _emit(preset)
    view = view_from_emitted_text(out)
    tweeter = {1}

    assert unprotected_tweeter_outputs(view, tweeter_channels=tweeter) == ()
    assert output_highpass_protected(view, channel=1, allowed_channels=tweeter)
    assert tweeter_guard_present(
        view,
        channels=tweeter,
        hp_name="as_tweeter_measure_p0_hp",
        limiter_name=_driver_limiter_name("tweeter"),
        limiter_clip_ceiling_db=-12.0,
    )


def test_program_config_stereo_routes_both_woofers_and_both_tweeters():
    preset = _preset("stereo")  # outputs 0,2 woofer; 1,3 tweeter
    out = _emit(preset)
    parsed = yaml_lib.safe_load(out)
    routing = {
        entry["dest"]: entry["sources"][0]["channel"]
        for entry in parsed["mixers"]["split_active_2way"]["mapping"]
    }
    # Both woofer outputs take program ch0; both tweeter outputs take ch1.
    assert routing == {0: 0, 1: 1, 2: 0, 3: 1}
    view = view_from_emitted_text(out)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1, 3}) == ()


def test_program_config_refuses_measurement_hp_below_protective_floor():
    # Explicit floor above the 1600 Hz crossover -> build-time refusal.
    with pytest.raises(ActiveSpeakerConfigError, match="below the declared protective"):
        _emit(
            _preset("mono"),
            protective_hp_min_corner_hz=2000.0,
        )
    # The configured crossover is excluded and cannot alter graph admission.
    out = _emit(_low_fc_preset())
    assert "freq: 300" not in out


def test_program_config_refuses_tweeter_hp_slope_below_floor():
    with pytest.raises(ActiveSpeakerConfigError, match="slope"):
        _emit(
            _preset("mono"),
            protective_hp_min_slope_db_per_octave=30.0,  # LR4 is 24 dB/oct
        )


def test_program_config_refuses_local_subwoofer_preset():
    builder = _two_way_preset("mono")
    builder["local_subwoofer"] = {
        "physical_output_index": 2,
        "crossover_fc_hz": 80,
        "label": "sub",
    }
    preset = ActiveSpeakerPreset.from_mapping(builder)
    with pytest.raises(ActiveSpeakerConfigError, match="local subwoofer"):
        _emit(preset)


def test_program_config_refuses_outputd_playback_lane():
    with pytest.raises(ActiveSpeakerConfigError):
        _emit(_preset("mono"), playback_device="jasper_out")


def test_program_config_refuses_non_two_way_preset():
    # W2 scope: the conductor's program topology is designed for a 2-way; a
    # 3-way needs a designed reshape, not a silent generalization.
    preset = ActiveSpeakerPreset.from_mapping(_three_way_preset("stereo"))
    with pytest.raises(ActiveSpeakerConfigError, match="scoped to 2-way"):
        _emit(
            preset,
            role_channels={"woofer": 0, "mid": 1, "tweeter": 2},
        )


# --- Adversarial: pre-split per-channel HP must be rejected (contract §1) -----
#
# On the 2-way preset program ch1 numerically coincides with tweeter output 1,
# so a high-pass emitted PRE-mixer on channel [1] can false-PASS
# ``output_highpass_protected`` (its channel set [1] is a subset of the tweeter
# role set {1}). ``tweeter_guard_present`` is the discriminator: it requires the
# high-pass AND the limiter together on exactly the tweeter output channels in
# ONE post-mixer step, which a pre-split HP can never satisfy.

_TWEETER_HP = "as_tweeter_measure_p0_hp"
_TWEETER_LIMITER = _driver_limiter_name("tweeter")

_FILTERS_BLOCK = f"""filters:
  {_TWEETER_HP}:
    type: BiquadCombo
    parameters: {{ type: LinkwitzRileyHighpass, freq: 1600.0, order: 4 }}
  as_tweeter_delay:
    type: Delay
    parameters: {{ delay: 0.0, unit: ms }}
  {_TWEETER_LIMITER}:
    type: Limiter
    parameters: {{ soft_clip: true, clip_limit: -12.0 }}
"""

_ROUTED_PIPELINE = f"""pipeline:
  - type: Mixer
    name: split_active_2way
  - type: Filter
    channels: [1]
    names: [{_TWEETER_HP}, as_tweeter_delay, {_TWEETER_LIMITER}]
"""

_PRESPLIT_PIPELINE = f"""pipeline:
  - type: Filter
    channels: [1]
    names: [{_TWEETER_HP}]
  - type: Mixer
    name: split_active_2way
  - type: Filter
    channels: [1]
    names: [as_tweeter_delay, {_TWEETER_LIMITER}]
"""


def test_routed_hp_variant_passes_both_proofs():
    view = view_from_emitted_text(_FILTERS_BLOCK + "\n" + _ROUTED_PIPELINE)
    assert output_highpass_protected(view, channel=1, allowed_channels={1})
    assert tweeter_guard_present(
        view,
        channels={1},
        hp_name=_TWEETER_HP,
        limiter_name=_TWEETER_LIMITER,
        limiter_clip_ceiling_db=-12.0,
    )


def test_pre_split_hp_variant_rejected_by_tweeter_guard():
    view = view_from_emitted_text(_FILTERS_BLOCK + "\n" + _PRESPLIT_PIPELINE)
    # output_highpass_protected alone false-PASSES the coincident-channel HP...
    assert output_highpass_protected(view, channel=1, allowed_channels={1})
    # ...but tweeter_guard_present rejects it: no single step wires HP + limiter
    # together on the tweeter output channels.
    assert not tweeter_guard_present(
        view,
        channels={1},
        hp_name=_TWEETER_HP,
        limiter_name=_TWEETER_LIMITER,
        limiter_clip_ceiling_db=-12.0,
    )


def test_build_and_prove_refuses_pre_split_hp_graph():
    preset = _preset("mono")
    doctored = (
        "---\n"
        + _FILTERS_BLOCK
        + "\nmixers:\n  split_active_2way:\n    channels: { in: 2, out: 2 }\n"
        + _PRESPLIT_PIPELINE
    )
    with pytest.raises(ActiveSpeakerConfigError, match="provably high-pass"):
        _assert_program_graph_proven(
            doctored,
            preset,
            min_corner_hz=400.0,
            tweeter_hp_name=_TWEETER_HP,
        )
