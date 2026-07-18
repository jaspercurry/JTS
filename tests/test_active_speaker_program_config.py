# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Channel-routed program graph emission (Wave 2 deliverable A).

The v2 crossover conductor plays one 2-channel program WAV through a static
CamillaDSP graph that routes program capture ch0 -> woofer output path and ch1 ->
tweeter output path (design §5.4). These tests pin: role-routed mixing, the
APPLIED_RESPONSE filter set on the physical output channels, the 0 dB ceiling,
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

# Reuse the canonical preset fixtures (mono 2-way == JTS3 single cabinet:
# output 0 = woofer, output 1 = tweeter).
from tests.test_active_speaker_profile import _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"
ROLE_CHANNELS = {"woofer": 0, "tweeter": 1}


def _preset(layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset(layout))


def _low_fc_preset() -> ActiveSpeakerPreset:
    """A 2-way preset whose tweeter crosses too low for the protective floor."""
    builder = _two_way_preset("mono")
    builder["crossover_regions"][0]["fc_hz"] = 300  # < TWEETER_PROTECTIVE_HP floor
    return ActiveSpeakerPreset.from_mapping(builder)


def test_program_config_routes_each_channel_to_its_driver_output():
    preset = _preset("mono")
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
    )
    parsed = yaml_lib.safe_load(out)

    # Capture is the program channel count; playback the physical output count.
    assert parsed["devices"]["capture"]["channels"] == 2
    assert parsed["devices"]["volume_limit"] == 0.0

    mixer = parsed["mixers"]["program_route_2way"]
    routing = {
        entry["dest"]: [s["channel"] for s in entry["sources"]]
        for entry in mixer["mapping"]
    }
    # ch0 -> woofer output 0, ch1 -> tweeter output 1 (role-routed, not side-routed).
    assert routing == {0: [0], 1: [1]}


def test_program_config_carries_target_crossover_not_bringup_hp():
    preset = _preset("mono")
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
    )
    parsed = yaml_lib.safe_load(out)

    # APPLIED_RESPONSE: the extra bring-up protective HP is dropped; the tweeter
    # is protected by its TARGET crossover high-pass, on the OUTPUT channel.
    assert "as_tweeter_protective_hp" not in parsed["filters"]
    hp = parsed["filters"]["as_tweeter_woofer_tweeter_hp"]["parameters"]
    assert hp == {"type": "LinkwitzRileyHighpass", "freq": 1600.0, "order": 4}
    # Program headroom is the commissioning headroom (0 dB) so the effective-peak
    # ledger is main_volume + program peak with no hidden graph attenuation.
    assert parsed["filters"]["active_startup_headroom"]["parameters"]["gain"] in (
        0.0,
        -0.0,
    )


def test_program_config_passes_all_graph_safety_proofs():
    preset = _preset("mono")
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
    )
    view = view_from_emitted_text(out)
    tweeter = {1}

    assert unprotected_tweeter_outputs(view, tweeter_channels=tweeter) == ()
    assert output_highpass_protected(view, channel=1, allowed_channels=tweeter)
    assert tweeter_guard_present(
        view,
        channels=tweeter,
        hp_name="as_tweeter_woofer_tweeter_hp",
        limiter_name=_driver_limiter_name("tweeter"),
        limiter_clip_ceiling_db=-12.0,
    )


def test_program_config_stereo_routes_both_woofers_and_both_tweeters():
    preset = _preset("stereo")  # outputs 0,2 woofer; 1,3 tweeter
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
    )
    parsed = yaml_lib.safe_load(out)
    routing = {
        entry["dest"]: entry["sources"][0]["channel"]
        for entry in parsed["mixers"]["program_route_2way"]["mapping"]
    }
    # Both woofer outputs take program ch0; both tweeter outputs take ch1.
    assert routing == {0: 0, 1: 1, 2: 0, 3: 1}
    view = view_from_emitted_text(out)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1, 3}) == ()


def test_program_config_refuses_tweeter_hp_below_protective_floor():
    # Explicit floor above the 1600 Hz crossover -> build-time refusal.
    with pytest.raises(ActiveSpeakerConfigError, match="below the declared protective"):
        emit_active_speaker_program_config(
            _preset("mono"),
            role_channels=ROLE_CHANNELS,
            playback_device=ACTIVE_PCM,
            protective_hp_min_corner_hz=2000.0,
        )
    # A preset that natively crosses the tweeter at 300 Hz -> refused at default floor.
    with pytest.raises(ActiveSpeakerConfigError, match="below the declared protective"):
        emit_active_speaker_program_config(
            _low_fc_preset(),
            role_channels=ROLE_CHANNELS,
            playback_device=ACTIVE_PCM,
        )


def test_program_config_refuses_tweeter_hp_slope_below_floor():
    with pytest.raises(ActiveSpeakerConfigError, match="slope"):
        emit_active_speaker_program_config(
            _preset("mono"),
            role_channels=ROLE_CHANNELS,
            playback_device=ACTIVE_PCM,
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
        emit_active_speaker_program_config(
            preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
        )


def test_program_config_refuses_outputd_playback_lane():
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_program_config(
            _preset("mono"), role_channels=ROLE_CHANNELS, playback_device="jasper_out"
        )


# --- Adversarial: pre-split per-channel HP must be rejected (contract §1) -----
#
# On the 2-way preset program ch1 numerically coincides with tweeter output 1,
# so a high-pass emitted PRE-mixer on channel [1] can false-PASS
# ``output_highpass_protected`` (its channel set [1] is a subset of the tweeter
# role set {1}). ``tweeter_guard_present`` is the discriminator: it requires the
# high-pass AND the limiter together on exactly the tweeter output channels in
# ONE post-mixer step, which a pre-split HP can never satisfy.

_TWEETER_HP = "as_tweeter_woofer_tweeter_hp"
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
    name: program_route_2way
  - type: Filter
    channels: [1]
    names: [{_TWEETER_HP}, as_tweeter_delay, {_TWEETER_LIMITER}]
"""

_PRESPLIT_PIPELINE = f"""pipeline:
  - type: Filter
    channels: [1]
    names: [{_TWEETER_HP}]
  - type: Mixer
    name: program_route_2way
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
        + "\nmixers:\n  program_route_2way:\n    channels: { in: 2, out: 2 }\n"
        + _PRESPLIT_PIPELINE
    )
    with pytest.raises(ActiveSpeakerConfigError, match="provably high-pass"):
        _assert_program_graph_proven(doctored, preset, min_corner_hz=400.0)
