# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Channel-routed program graph emission (Wave 2 deliverable A).

The v2 crossover conductor plays one 2-channel program WAV through a static
CamillaDSP graph that routes program capture ch0 -> woofer output path and ch1 ->
tweeter output path (design §5.4). These tests pin: role-routed mixing, the
protected-neutral filter set on the physical output channels, the 0 dB ceiling,
the build-time protective-floor gate, and the build-and-prove return contract —
including the adversarial pre-split-HP shape that ``tweeter_guard_present`` must
reject even where ``output_highpass_protected`` alone would false-PASS.
"""
from __future__ import annotations

import logging

import pytest

from jasper.active_speaker.crossover_section import CrossoverSection
import yaml as yaml_lib

from jasper.active_speaker.camilla_yaml.emit_program import emit_active_speaker_program_config
from jasper.active_speaker.profile import ActiveSpeakerConfigError, ActiveSpeakerPreset
from jasper.active_speaker.camilla_names import driver_limiter_name as _driver_limiter_name
from jasper.active_speaker.camilla_yaml import (
    emit_active_speaker_baseline_config,
    protected_neutral_program_origin,
)
from jasper.active_speaker.camilla_yaml.gates import _assert_program_graph_proven
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.graph_safety import (
    output_highpass_protected,
    tweeter_guard_present,
    unprotected_tweeter_outputs,
    view_from_emitted_text,
)

# Reuse the canonical preset fixtures (mono 2-way == JTS3 single cabinet:
# output 0 = woofer, output 1 = tweeter).
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset
from tests._log_events import event_field_maps

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"
ROLE_CHANNELS = {"woofer": 0, "tweeter": 1}


def _preset(layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset(layout))


def _confirmed_protection(tweeter_slope_db_per_octave: float = 24.0):
    profile = {"targets": [
        {
            "role": "woofer", "target_fingerprint": "w",
            "required_protection_filters": [{
                "kind": "lowpass", "cutoff_hz": 3000.0,
                "minimum_slope_db_per_octave": 24.0,
            }],
        },
        {
            "role": "tweeter", "target_fingerprint": "t",
            "required_protection_filters": [{
                "kind": "highpass", "cutoff_hz": 1800.0,
                "minimum_slope_db_per_octave": tweeter_slope_db_per_octave,
            }],
        },
    ]}
    return confirmed_protection_sections(
        profile, {"woofer": "w", "tweeter": "t"}
    )


def test_program_config_routes_each_channel_to_its_driver_output():
    preset = _preset("mono")
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
    )
    parsed = yaml_lib.safe_load(out)

    # Capture is the program channel count; playback the physical output count.
    assert parsed["devices"]["capture"]["channels"] == 2
    assert parsed["devices"]["volume_limit"] == 0.0

    mixer = parsed["mixers"]["split_active_2way"]
    routing = {
        entry["dest"]: [s["channel"] for s in entry["sources"]]
        for entry in mixer["mapping"]
    }
    # ch0 -> woofer output 0, ch1 -> tweeter output 1 (role-routed, not side-routed).
    assert routing == {0: [0], 1: [1]}


def test_protected_neutral_program_config_contains_only_declared_safety_shaping():
    builder = _two_way_preset("mono")
    builder["crossover_regions"][0]["upper_polarity"] = "inverted"
    preset = ActiveSpeakerPreset.from_mapping(builder)
    protection = _confirmed_protection()
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=protection,
    )
    parsed = yaml_lib.safe_load(out)
    filters = parsed["filters"]
    assert set(filters) == {
        "active_startup_headroom",
        "as_woofer_program_protection_0", "as_tweeter_program_protection_0",
        "as_woofer_startup_limiter", "as_tweeter_startup_limiter",
        "as_out0_commission_mute", "as_out1_commission_mute",
    }
    assert filters["as_woofer_program_protection_0"]["parameters"] == {
        "type": "LinkwitzRileyLowpass", "freq": 3000.0, "order": 4,
    }
    assert filters["as_tweeter_program_protection_0"]["parameters"] == {
        "type": "LinkwitzRileyHighpass", "freq": 1800.0, "order": 4,
    }
    assert filters["active_startup_headroom"]["parameters"]["gain"] == pytest.approx(0.0)
    for role in ("woofer", "tweeter"):
        limiter = filters[f"as_{role}_startup_limiter"]
        assert limiter["type"] == "Limiter"
        assert limiter["parameters"] == {
            "soft_clip": True, "clip_limit": -12.0,
        }
    assert parsed["pipeline"][2]["names"] == [
        "as_woofer_program_protection_0", "as_woofer_startup_limiter",
    ]
    assert parsed["pipeline"][3]["names"] == [
        "as_tweeter_program_protection_0", "as_tweeter_startup_limiter",
    ]
    assert parsed["mixers"]["split_active_2way"]["mapping"][1]["sources"][0][
        "inverted"
    ] is False
    view = view_from_emitted_text(out)
    assert tweeter_guard_present(
        view, channels={1}, hp_name="as_tweeter_program_protection_0",
        limiter_name="as_tweeter_startup_limiter", limiter_clip_ceiling_db=-12.0,
    )
    assert protected_neutral_program_origin(out) is True


def test_protected_neutral_origin_excludes_other_and_mutated_graphs():
    preset = _preset("mono")
    neutral = yaml_lib.safe_load(emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
    ))
    applied = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    assert protected_neutral_program_origin(applied) is None
    partial = yaml_lib.safe_load(yaml_lib.safe_dump(neutral))
    partial["filters"].pop("as_tweeter_program_protection_0")
    assert protected_neutral_program_origin(partial) is False
    neutral["filters"]["as_room_extra"] = {"type": "Gain", "parameters": {"gain": -1.0}}
    assert protected_neutral_program_origin(neutral) is False


def test_program_config_stereo_routes_both_woofers_and_both_tweeters():
    preset = _preset("stereo")  # outputs 0,2 woofer; 1,3 tweeter
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
    )
    parsed = yaml_lib.safe_load(out)
    routing = {
        entry["dest"]: entry["sources"][0]["channel"]
        for entry in parsed["mixers"]["split_active_2way"]["mapping"]
    }
    # Both woofer outputs take program ch0; both tweeter outputs take ch1.
    assert routing == {0: 0, 1: 1, 2: 0, 3: 1}
    view = view_from_emitted_text(out)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1, 3}) == ()


@pytest.mark.parametrize(
    ("sections", "corner_refusal"),
    [
        # 399 Hz: a corner below the 400 Hz floor refuses at any slope.
        ({"woofer": (CrossoverSection(3000.0, 4, False),),
          "tweeter": (CrossoverSection(399.0, 4, True),)}, True),
        ({"woofer": (CrossoverSection(3000.0, 4, False),),
          "tweeter": (CrossoverSection(399.0, 2, True),)}, True),
        # No tweeter high-pass at all; then a role missing entirely.
        ({"woofer": (CrossoverSection(3000.0, 4, False),),
          "tweeter": ()}, False),
        ({"tweeter": (CrossoverSection(1800.0, 4, True),)}, False),
    ],
)
def test_protected_neutral_emit_refuses_unsafe_tweeter_protection(
    sections, corner_refusal, caplog,
):
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ActiveSpeakerConfigError):
            emit_active_speaker_program_config(
                _preset("mono"), role_channels=ROLE_CHANNELS,
                playback_device=ACTIVE_PCM, protection_sections_by_role=sections,
            )
    blocked = event_field_maps(
        caplog, "active_speaker.program_emit_gate",
        result="blocked_tweeter_protection_below_floor",
    )
    assert bool(blocked) is corner_refusal, [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize(("slope", "order", "disclosed"), [(12.0, 2, True), (24.0, 4, False)])
def test_declared_tweeter_protection_plays_as_declared_and_a_shallow_slope_discloses(
    slope, order, disclosed, caplog,
):
    """ADR-0446: a slope under the code's 24 dB/oct figure discloses, never refuses."""
    with caplog.at_level(logging.WARNING):
        out = emit_active_speaker_program_config(
            _preset("mono"), role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
            protection_sections_by_role=_confirmed_protection(slope),
        )
    parsed = yaml_lib.safe_load(out)
    assert parsed["filters"]["as_tweeter_program_protection_0"]["parameters"] == {
        "type": "LinkwitzRileyHighpass", "freq": 1800.0, "order": order,
    }
    assert parsed["pipeline"][3]["names"] == [
        "as_tweeter_program_protection_0", "as_tweeter_startup_limiter",
    ]
    assert parsed["filters"]["as_tweeter_startup_limiter"] == {
        "type": "Limiter", "parameters": {"soft_clip": True, "clip_limit": -12.0},
    }
    assert parsed["devices"]["volume_limit"] == 0.0
    notes = event_field_maps(
        caplog, "active_speaker.program_emit_gate",
        result="tweeter_hp_slope_below_commissioning_floor",
    )
    assert notes == ([{
        "result": "tweeter_hp_slope_below_commissioning_floor",
        "preset_id": _preset("mono").preset_id, "order": "2", "slope_db_per_octave": "12",
        "commissioning_floor_db_per_octave": "24",
    }] if disclosed else [])


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
            preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
            protection_sections_by_role=_confirmed_protection(),
        )


def test_program_config_refuses_outputd_playback_lane():
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_program_config(
            _preset("mono"), role_channels=ROLE_CHANNELS, playback_device="jasper_out",
            protection_sections_by_role=_confirmed_protection(),
        )


def test_program_config_refuses_a_three_way_preset():
    # Scope: this emitter routes one program channel per role, which a 1-way and
    # a 2-way both are; a 3-way needs a designed reshape (mid-band MESM
    # schedule, per-region alignment), not a silent generalization.
    preset = ActiveSpeakerPreset.from_mapping(_three_way_preset("stereo"))
    with pytest.raises(ActiveSpeakerConfigError, match="designed program reshape"):
        emit_active_speaker_program_config(
            preset,
            role_channels={"woofer": 0, "mid": 1, "tweeter": 2},
            playback_device=ACTIVE_PCM,
            protection_sections_by_role=_confirmed_protection(),
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
            doctored, preset, min_corner_hz=400.0, tweeter_hp_name=_TWEETER_HP,
        )
