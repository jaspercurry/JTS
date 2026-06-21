"""Tests for the shared CamillaDSP emission primitives.

These assert the EXACT strings the prior hand-rolled emitters produced
in sound / correction / active_speaker. They are the contract that lets
those subsystems migrate to the shared helpers with byte-identical
output. If one of these changes, every generated config re-formats —
so treat a diff here as load-bearing.
"""
from __future__ import annotations

import math

import pytest

from jasper.camilla_emit import (
    CHANNEL_SELECT_MIXER,
    MONO_SUM_GAIN_DB,
    channel_select_sources,
    emit_channel_select_mixer,
    emit_gain_filter,
    mono_sum_sources,
    emit_linkwitz_riley,
    emit_master_gain_pipeline,
    emit_mixer,
    emit_peaking_biquad,
    fmt,
)

yaml = pytest.importorskip("yaml")


def test_fmt_is_four_decimals():
    assert fmt(0.0) == "0.0000"
    assert fmt(80.0) == "80.0000"
    assert fmt(-3.0) == "-3.0000"
    assert fmt(20.0 * math.log10(0.5)) == "-6.0206"


def test_gain_filter_matches_sound_default():
    # sound._emit_gain_filter("flat", 0.0) — always mute:false.
    assert emit_gain_filter("flat", 0.0) == [
        "  flat:",
        "    type: Gain",
        "    parameters: { gain: 0.0000, inverted: false, mute: false }",
    ]


def test_gain_filter_matches_active_speaker_mute():
    # active_speaker._emit_gain_filter(name, gain_db, mute=True).
    assert emit_gain_filter("as_tweeter_startup_mute", 0.0, mute=True) == [
        "  as_tweeter_startup_mute:",
        "    type: Gain",
        "    parameters: { gain: 0.0000, inverted: false, mute: true }",
    ]


def test_peaking_biquad_matches_correction_and_sound():
    # correction emits freq/q/gain at :.4f; sound._emit_peq_filter same.
    assert emit_peaking_biquad("peq_1", freq=80.0, q=4.0, gain=-3.0) == [
        "  peq_1:",
        "    type: Biquad",
        "    parameters:",
        "      type: Peaking",
        "      freq: 80.0000",
        "      q: 4.0000",
        "      gain: -3.0000",
    ]


def test_linkwitz_riley_matches_active_speaker():
    # active_speaker._emit_linkwitz_riley_filter — native BiquadCombo.
    assert emit_linkwitz_riley("x_lp", highpass=False, freq_hz=80.0, order=4) == [
        "  x_lp:",
        "    type: BiquadCombo",
        "    parameters:",
        "      type: LinkwitzRileyLowpass",
        "      freq: 80.0000",
        "      order: 4",
    ]
    assert emit_linkwitz_riley("x_hp", highpass=True, freq_hz=2000.0, order=2) == [
        "  x_hp:",
        "    type: BiquadCombo",
        "    parameters:",
        "      type: LinkwitzRileyHighpass",
        "      freq: 2000.0000",
        "      order: 2",
    ]


def test_mixer_matches_multiroom_channel_select():
    # multiroom._select_mixer_block — no description/labels, 2->2.
    block = emit_mixer(
        "channel_select",
        channels_in=2,
        channels_out=2,
        mapping=[(0, [(0, 0.0, False)]), (1, [(0, 0.0, False)])],
    )
    assert block == (
        "  channel_select:\n"
        "    channels: { in: 2, out: 2 }\n"
        "    mapping:\n"
        "      - dest: 0\n"
        "        sources:\n"
        "          - { channel: 0, gain: 0.0000, inverted: false }\n"
        "      - dest: 1\n"
        "        sources:\n"
        "          - { channel: 0, gain: 0.0000, inverted: false }"
    )


def test_mono_sum_sources_is_exactly_clip_safe():
    # The one clip-safe L+R sum recipe: two MONO_SUM_GAIN_DB feeds summing to
    # exactly 0 dBFS for identical L==R (NOT a rounded -6.0, which gains +0.02 dB).
    assert mono_sum_sources() == [(0, MONO_SUM_GAIN_DB, False), (1, MONO_SUM_GAIN_DB, False)]
    assert sum(10 ** (g / 20.0) for _, g, _ in mono_sum_sources()) == pytest.approx(1.0)
    assert mono_sum_sources(inverted=True) == [
        (0, MONO_SUM_GAIN_DB, True), (1, MONO_SUM_GAIN_DB, True)
    ]


def test_channel_select_mono_composes_the_shared_sum():
    # channel_select("mono"/"sub") is the inter-speaker use of the one recipe.
    assert channel_select_sources("mono") == mono_sum_sources()
    assert channel_select_sources("sub") == mono_sum_sources()


def test_channel_select_sources_left_right_are_unity_routes():
    assert channel_select_sources("left") == [(0, 0.0, False)]
    assert channel_select_sources("right") == [(1, 0.0, False)]


def test_channel_select_sources_mono_is_clip_safe_sum():
    # Two -6.02 dB feeds so identical L==R sum to exactly 0 dBFS (no clip).
    sources = channel_select_sources("mono")
    assert sources == [(0, MONO_SUM_GAIN_DB, False), (1, MONO_SUM_GAIN_DB, False)]
    assert sum(10 ** (g / 20.0) for _, g, _ in sources) == pytest.approx(1.0)
    assert channel_select_sources("sub") == sources  # sub shares the mono sum


@pytest.mark.parametrize("bad", ["stereo", "", "garbage"])
def test_channel_select_sources_rejects_passthrough_or_unknown(bad):
    with pytest.raises(ValueError):
        channel_select_sources(bad)


def test_emit_channel_select_mixer_matches_hand_built_block():
    # emit_channel_select_mixer("right") == the equivalent emit_mixer call, so
    # the shared follower/member recipe spells the pick one canonical way: both
    # output channels carry the picked source.
    right = [(1, 0.0, False)]
    assert emit_channel_select_mixer("right") == emit_mixer(
        CHANNEL_SELECT_MIXER,
        channels_in=2,
        channels_out=2,
        mapping=[(0, right), (1, right)],
    )
    assert emit_channel_select_mixer("left", name="cs2").startswith("  cs2:\n")


def test_mixer_matches_active_speaker_split_with_description_and_labels():
    # active_speaker._emit_split_mixer — description + labels + N outs.
    block = emit_mixer(
        "split_active_2way",
        channels_in=2,
        channels_out=2,
        mapping=[(0, [(0, 0.0, False)]), (1, [(1, 0.0, True)])],
        description="stereo source -> 2 protected active outputs",
        labels=["Woofer", "Tweeter"],
    )
    assert block == (
        "  split_active_2way:\n"
        '    description: "stereo source -> 2 protected active outputs"\n'
        '    labels: ["Woofer", "Tweeter"]\n'
        "    channels: { in: 2, out: 2 }\n"
        "    mapping:\n"
        "      - dest: 0\n"
        "        sources:\n"
        "          - { channel: 0, gain: 0.0000, inverted: false }\n"
        "      - dest: 1\n"
        "        sources:\n"
        "          - { channel: 1, gain: 0.0000, inverted: true }"
    )


def test_mixer_mono_sum_multiple_sources_per_dest():
    block = emit_mixer(
        "channel_select",
        channels_in=2,
        channels_out=2,
        mapping=[
            (0, [(0, -6.0206, False), (1, -6.0206, False)]),
            (1, [(0, -6.0206, False), (1, -6.0206, False)]),
        ],
    )
    # both inputs summed onto each output at -6.02 dB
    assert block.count("- { channel: 0, gain: -6.0206, inverted: false }") == 2
    assert block.count("- { channel: 1, gain: -6.0206, inverted: false }") == 2


# --- well-formedness: every emitter parses as valid YAML in context ---

def test_emitters_parse_as_valid_yaml():
    filters = "\n".join(
        emit_gain_filter("flat", 0.0)
        + emit_peaking_biquad("peq_1", freq=80.0, q=4.0, gain=-3.0)
        + emit_linkwitz_riley("sub_lp", highpass=False, freq_hz=80.0, order=4)
    )
    parsed = yaml.safe_load("filters:\n" + filters)["filters"]
    assert parsed["flat"]["type"] == "Gain"
    assert parsed["peq_1"]["parameters"]["type"] == "Peaking"
    assert parsed["sub_lp"]["type"] == "BiquadCombo"
    assert parsed["sub_lp"]["parameters"]["order"] == 4

    mixer = emit_mixer(
        "channel_select",
        channels_in=2,
        channels_out=2,
        mapping=[(0, [(0, 0.0, False)]), (1, [(1, 0.0, False)])],
    )
    m = yaml.safe_load("mixers:\n" + mixer)["mixers"]["channel_select"]
    assert m["channels"] == {"in": 2, "out": 2}
    assert m["mapping"][0]["sources"][0]["channel"] == 0
    assert m["mapping"][1]["sources"][0]["channel"] == 1


def test_master_gain_pipeline_solo_duplicates_left_byte_for_byte():
    # Reproduces correction._emit_pipeline / sound._emit_pipeline exactly
    # (the solo-impact contract both consumers also lock in their own
    # byte-exact tests).
    assert emit_master_gain_pipeline(["peq_1", "flat"]) == (
        "  - type: Mixer\n"
        "    name: master_gain\n"
        "  - type: Filter\n"
        "    channels: [0]\n"
        "    names: [peq_1, flat]\n"
        "  - type: Filter\n"
        "    channels: [1]\n"
        "    names: [peq_1, flat]"
    )


def test_master_gain_pipeline_right_names_differ_and_parse():
    # The multi-room leader-bake shape: distinct per-channel chains.
    block = emit_master_gain_pipeline(
        ["peq_1", "flat"], ["peq_r1", "flat"]
    )
    assert "    channels: [0]\n    names: [peq_1, flat]" in block
    assert "    channels: [1]\n    names: [peq_r1, flat]" in block
    steps = yaml.safe_load("pipeline:\n" + block)["pipeline"]
    assert [s["type"] for s in steps] == ["Mixer", "Filter", "Filter"]
    assert steps[0]["name"] == "master_gain"
    assert steps[1]["names"] == ["peq_1", "flat"]
    assert steps[2]["names"] == ["peq_r1", "flat"]
