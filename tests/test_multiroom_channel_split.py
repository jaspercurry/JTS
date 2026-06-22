# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the P1.2 channel-split CamillaDSP fragment generator.

These assert the DSP-correctness + safety invariants documented in
jasper/multiroom/channel_split.py:
  - master_gain is never touched (Ducker contract);
  - no positive source gains (clip safety);
  - mono/sub sums are exactly -6.02 dB (identical L==R -> 0 dBFS);
  - the LR4 sub crossover is CamillaDSP's native BiquadCombo;
  - stereo is a true passthrough;
  - the emitted YAML parses and composes into the real base config.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.multiroom.channel_split import (
    CHANNEL_SELECT_MIXER,
    DEFAULT_CROSSOVER_HZ,
    MONO_SUM_GAIN_DB,
    build_channel_split,
)
from jasper.multiroom.config import ALLOWED_CHANNELS

yaml = pytest.importorskip("yaml")


# A minimal but faithful copy of the runtime base config shape
# (deploy/camilladsp/outputd-cutover.yml): identity master_gain, a flat
# per-channel filter, volume_limit 0.0. Used to prove the fragment
# composes into a real config the way P1.3 will weave it.
BASE_CONFIG = """\
devices:
  samplerate: 48000
  chunksize: 1024
  volume_limit: 0.0
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
    format: S16_LE

filters:
  flat:
    type: Gain
    parameters: { gain: 0.0, inverted: false, mute: false }

mixers:
  master_gain:
    channels: { in: 2, out: 2 }
    mapping:
      - dest: 0
        sources: [{ channel: 0, gain: 0, inverted: false }]
      - dest: 1
        sources: [{ channel: 1, gain: 0, inverted: false }]

pipeline:
  - type: Mixer
    name: master_gain
  - type: Filter
    channels: [0]
    names: [flat]
  - type: Filter
    channels: [1]
    names: [flat]
"""


def _parse_mixers(block: str) -> dict:
    return yaml.safe_load("mixers:\n" + block)["mixers"]


def _parse_filters(block: str) -> dict:
    return yaml.safe_load("filters:\n" + block)["filters"]


def _parse_pipeline(step: str) -> list:
    return yaml.safe_load("pipeline:\n" + step)["pipeline"]


def _weave(base_text: str, channel: str, **kw) -> dict:
    """Splice the fragment into the base config the way P1.3 will, then
    parse the whole thing. Proves the fragment composes end-to-end.

    The crossover names are appended to the END of EVERY per-channel
    Filter step's `names: [...]` list (crossover runs last), whatever
    that chain already contains."""
    split = build_channel_split(channel, **kw)
    out: list[str] = []
    for line in base_text.splitlines():
        out.append(line)
        if line.rstrip() == "mixers:" and split.mixer_block:
            out.append(split.mixer_block)
        elif line.rstrip() == "filters:" and split.filter_block:
            out.append(split.filter_block)
        elif line.strip() == "name: master_gain" and split.pipeline_mixer_step:
            out.append(split.pipeline_mixer_step)
    text = "\n".join(out)
    if split.filter_chain_names:
        appended = ", ".join(split.filter_chain_names)
        text = re.sub(r"(names: \[[^\]]*)\]", rf"\1, {appended}]", text)
    return yaml.safe_load(text)


REAL_CUTOVER = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "camilladsp" / "outputd-cutover.yml"
)


# ----------------------------- stereo --------------------------------

def test_stereo_is_total_passthrough():
    split = build_channel_split("stereo")
    assert split.is_passthrough is True
    assert split.mixer_name is None
    assert split.mixer_block == ""
    assert split.filter_block == ""
    assert split.filter_chain_names == ()
    assert split.pipeline_mixer_step == ""


def test_stereo_weave_leaves_base_config_unchanged():
    assert _weave(BASE_CONFIG, "stereo") == yaml.safe_load(BASE_CONFIG)


# --------------------------- left / right ----------------------------

@pytest.mark.parametrize("channel,src", [("left", 0), ("right", 1)])
def test_left_right_route_single_channel_to_both_outputs(channel, src):
    split = build_channel_split(channel)
    assert split.is_passthrough is False
    assert split.mixer_name == CHANNEL_SELECT_MIXER
    # No crossover on a full-range L/R speaker.
    assert split.filter_block == ""
    assert split.filter_chain_names == ()

    mixers = _parse_mixers(split.mixer_block)
    mapping = mixers[CHANNEL_SELECT_MIXER]["mapping"]
    assert mixers[CHANNEL_SELECT_MIXER]["channels"] == {"in": 2, "out": 2}
    assert [m["dest"] for m in mapping] == [0, 1]
    for m in mapping:
        # Both outputs carry the SAME single source channel at unity.
        assert len(m["sources"]) == 1
        assert m["sources"][0]["channel"] == src
        assert m["sources"][0]["gain"] == 0.0
        assert m["sources"][0]["inverted"] is False


# ----------------------------- mono ----------------------------------

def test_mono_sums_l_plus_r_clip_safe_no_crossover():
    split = build_channel_split("mono")
    assert split.mixer_name == CHANNEL_SELECT_MIXER
    # Mono full-range -> sum only, NO lowpass.
    assert split.filter_block == ""
    assert split.filter_chain_names == ()

    mapping = _parse_mixers(split.mixer_block)[CHANNEL_SELECT_MIXER]["mapping"]
    for m in mapping:
        chans = sorted(s["channel"] for s in m["sources"])
        assert chans == [0, 1]  # both input channels summed
        for s in m["sources"]:
            assert s["gain"] == pytest.approx(MONO_SUM_GAIN_DB, abs=1e-3)


def test_mono_sum_gain_makes_identical_lr_hit_exactly_0_dbfs():
    # 0.5 + 0.5 == 1.0 -> 0 dBFS for identical L==R. No clip.
    linear = 10 ** (MONO_SUM_GAIN_DB / 20.0)
    assert linear == pytest.approx(0.5, abs=1e-6)
    assert linear + linear == pytest.approx(1.0, abs=1e-6)


# ------------------------------ sub ----------------------------------

def test_sub_sums_then_lowpasses_lr4():
    split = build_channel_split("sub")
    # Same clip-safe mono sum as `mono`...
    mapping = _parse_mixers(split.mixer_block)[CHANNEL_SELECT_MIXER]["mapping"]
    for m in mapping:
        for s in m["sources"]:
            assert s["gain"] == pytest.approx(MONO_SUM_GAIN_DB, abs=1e-3)

    # ...plus the native LR4 (BiquadCombo) lowpass crossover.
    assert split.filter_chain_names == ("sub_crossover",)
    filters = _parse_filters(split.filter_block)
    assert set(filters) == {"sub_crossover"}
    f = filters["sub_crossover"]
    assert f["type"] == "BiquadCombo"
    assert f["parameters"]["type"] == "LinkwitzRileyLowpass"
    assert f["parameters"]["freq"] == pytest.approx(DEFAULT_CROSSOVER_HZ)
    assert f["parameters"]["order"] == 4  # LR4 == 24 dB/oct


def test_sub_crossover_hz_is_tunable():
    split = build_channel_split("sub", crossover_hz=120.0)
    filters = _parse_filters(split.filter_block)
    assert filters["sub_crossover"]["parameters"]["freq"] == pytest.approx(120.0)


# --------------------------- invariants ------------------------------

@pytest.mark.parametrize("channel", ALLOWED_CHANNELS)
def test_never_emits_master_gain(channel):
    # The Ducker drives main_volume and relies on master_gain staying
    # the identity mixer. This module must never name it.
    split = build_channel_split(channel)
    blob = split.mixer_block + split.filter_block + split.pipeline_mixer_step
    assert "master_gain" not in blob


@pytest.mark.parametrize("channel", ALLOWED_CHANNELS)
def test_no_positive_source_gain_anywhere(channel):
    split = build_channel_split(channel)
    if not split.mixer_block:
        return
    mapping = _parse_mixers(split.mixer_block)[CHANNEL_SELECT_MIXER]["mapping"]
    for m in mapping:
        for s in m["sources"]:
            assert s["gain"] <= 0.0


@pytest.mark.parametrize("channel", ALLOWED_CHANNELS)
def test_pipeline_step_applies_channel_select_after_master_gain(channel):
    split = build_channel_split(channel)
    if split.is_passthrough:
        assert split.pipeline_mixer_step == ""
        return
    steps = _parse_pipeline(split.pipeline_mixer_step)
    assert steps == [{"type": "Mixer", "name": CHANNEL_SELECT_MIXER}]


@pytest.mark.parametrize("channel", ALLOWED_CHANNELS)
def test_weave_preserves_volume_limit_and_identity_master_gain(channel):
    cfg = _weave(BASE_CONFIG, channel)
    # The hard clip ceiling is untouched...
    assert cfg["devices"]["volume_limit"] == 0.0
    # ...and master_gain stays the identity mixer the Ducker relies on.
    mg = cfg["mixers"]["master_gain"]["mapping"]
    assert mg[0]["sources"][0]["channel"] == 0
    assert mg[1]["sources"][0]["channel"] == 1
    for m in mg:
        assert m["sources"][0]["gain"] == 0


def test_weave_sub_pipeline_order_mixer_then_filters_with_crossover_last():
    cfg = _weave(BASE_CONFIG, "sub")
    pipeline = cfg["pipeline"]
    # master_gain, then channel_select, then the per-channel filters.
    assert pipeline[0] == {"type": "Mixer", "name": "master_gain"}
    assert pipeline[1] == {"type": "Mixer", "name": CHANNEL_SELECT_MIXER}
    for step in pipeline[2:]:
        assert step["type"] == "Filter"
        # crossover runs LAST, after `flat` (room correction / EQ).
        assert step["names"][-1] == "sub_crossover"


def test_weave_sub_crossover_appends_after_existing_correction_chain():
    # Real generated configs carry multi-filter per-channel chains
    # (e.g. [room_peq_1, room_peq_2, flat]). The crossover must land at
    # the END (post-correction) — not replace, not reorder.
    base = BASE_CONFIG.replace(
        "names: [flat]", "names: [room_peq_1, room_peq_2, flat]"
    )
    cfg = _weave(base, "sub")
    for step in cfg["pipeline"]:
        if step.get("type") == "Filter":
            assert step["names"] == [
                "room_peq_1", "room_peq_2", "flat", "sub_crossover",
            ]


@pytest.mark.parametrize("channel", ALLOWED_CHANNELS)
def test_weave_into_real_runtime_base_config(channel):
    # Weave into the ACTUAL shipped runtime config, not the inline copy
    # — catches drift between this test's BASE_CONFIG and the real file,
    # and proves the real splice points (mixers:/filters:/pipeline) exist.
    cfg = _weave(REAL_CUTOVER.read_text(), channel)
    assert cfg["devices"]["volume_limit"] == 0.0
    assert "master_gain" in cfg["mixers"]
    if channel == "stereo":
        assert "channel_select" not in cfg["mixers"]
    else:
        assert "channel_select" in cfg["mixers"]
    if channel == "sub":
        assert "sub_crossover" in cfg["filters"]


@pytest.mark.parametrize("channel", ALLOWED_CHANNELS)
def test_summed_output_cannot_exceed_0_dbfs(channel):
    # Worst case for any |L|,|R| <= 1.0 is the sum of |linear gains| on
    # a dest. Assert <= 1.0 (0 dBFS) for every output, every mode — locks
    # clip-safety against any future edit that adds a source or bumps a gain.
    split = build_channel_split(channel)
    if not split.mixer_block:
        return  # stereo passthrough: identity, inherently <= input
    mapping = _parse_mixers(split.mixer_block)[CHANNEL_SELECT_MIXER]["mapping"]
    for m in mapping:
        worst = sum(10 ** (s["gain"] / 20.0) for s in m["sources"])
        assert worst <= 1.0 + 1e-9


def test_sub_mixer_is_mono_mixer_plus_crossover():
    # sub == mono sum + crossover; the summing mixer is byte-identical.
    mono = build_channel_split("mono")
    sub = build_channel_split("sub")
    assert sub.mixer_block == mono.mixer_block
    assert mono.filter_block == "" and mono.filter_chain_names == ()
    assert sub.filter_chain_names == ("sub_crossover",)


# ---- boundary: inter-speaker channel-select vs intra-speaker drivers ----
# `multiroom.channel` (this module) is which PROGRAM channel a whole
# speaker plays in a bond. `output_topology.SpeakerChannel.role` is which
# DRIVER a DAC output feeds. They compose because channel-select is
# interface-preserving (2->2); these tests pin that contract.

@pytest.mark.parametrize("channel", ["left", "right", "mono", "sub"])
def test_channel_select_is_interface_preserving_2x2(channel):
    # Always 2->2: changes WHAT is on the two channels, never the channel
    # COUNT. This is why every downstream stage (per-channel correction,
    # the active-speaker 2->N driver split) still receives two channels.
    split = build_channel_split(channel)
    mixer = _parse_mixers(split.mixer_block)[CHANNEL_SELECT_MIXER]
    assert mixer["channels"] == {"in": 2, "out": 2}


def test_channel_select_chains_into_active_speaker_driver_split():
    # Compose the two axes: inter-speaker channel-select (2->2) feeding an
    # active-speaker-style driver split (intra-speaker, 2->N). The
    # interface lines up — channel-select OUT == driver-split IN == 2 — so
    # a multi-way speaker in a bond plays its bond channel, split across
    # drivers. (Live weaving into an active-speaker config is P1.3; this
    # pins the channel-count contract that makes that weave sound.)
    from jasper.camilla_emit import emit_mixer

    sel = build_channel_split("left").mixer_block
    driver_split = emit_mixer(
        "split_active_2way",
        channels_in=2,
        channels_out=2,
        mapping=[(0, [(0, 0.0, False)]), (1, [(1, 0.0, False)])],
        description="stereo source -> 2 protected active outputs",
        labels=["Woofer", "Tweeter"],
    )
    sel_m = _parse_mixers(sel)[CHANNEL_SELECT_MIXER]
    split_m = _parse_mixers(driver_split)["split_active_2way"]
    assert sel_m["channels"]["out"] == split_m["channels"]["in"] == 2


# --------------------------- error path ------------------------------

@pytest.mark.parametrize("bad", ["surround", "", "Left", "rear", "lfe"])
def test_unknown_channel_raises(bad):
    with pytest.raises(ValueError):
        build_channel_split(bad)


@pytest.mark.parametrize("bad_hz", [0.0, -1.0, -80.0])
def test_sub_rejects_nonpositive_crossover(bad_hz):
    with pytest.raises(ValueError):
        build_channel_split("sub", crossover_hz=bad_hz)


def test_nonsub_ignores_crossover_hz():
    # crossover_hz is only meaningful for sub; a nonsense value on a
    # non-sub channel is harmless (the param is unused, so no raise).
    assert build_channel_split("left", crossover_hz=-5.0).filter_block == ""


def test_deterministic():
    a = build_channel_split("sub", crossover_hz=90.0)
    b = build_channel_split("sub", crossover_hz=90.0)
    assert a == b


# ---------- weave_channel_split: splicing into a generated config (PR-2) -----


def test_weave_stereo_is_byte_for_byte_passthrough():
    from jasper.multiroom.channel_split import weave_channel_split
    assert weave_channel_split(BASE_CONFIG, build_channel_split("stereo")) == BASE_CONFIG


@pytest.mark.parametrize("channel", ["left", "right", "mono"])
def test_weave_inserts_channel_select_right_after_master_gain(channel):
    from jasper.multiroom.channel_split import weave_channel_split
    doc = yaml.safe_load(weave_channel_split(BASE_CONFIG, build_channel_split(channel)))
    assert CHANNEL_SELECT_MIXER in doc["mixers"]
    # master_gain stays the identity mixer (Ducker contract — never touched).
    assert doc["mixers"]["master_gain"]["mapping"][0]["sources"][0]["channel"] == 0
    # The channel_select Mixer step runs immediately AFTER master_gain.
    steps = [(s.get("type"), s.get("name")) for s in doc["pipeline"]]
    i_mg = steps.index(("Mixer", "master_gain"))
    assert steps[i_mg + 1] == ("Mixer", CHANNEL_SELECT_MIXER)
    # Non-sub: no crossover, per-channel Filter names untouched.
    assert "sub_crossover" not in (doc.get("filters") or {})
    for s in doc["pipeline"]:
        if s.get("type") == "Filter":
            assert s["names"] == ["flat"]


def test_weave_sub_adds_crossover_and_appends_it_last_to_each_filter():
    from jasper.multiroom.channel_split import (
        SUB_CROSSOVER_FILTER,
        weave_channel_split,
    )
    doc = yaml.safe_load(weave_channel_split(BASE_CONFIG, build_channel_split("sub")))
    assert CHANNEL_SELECT_MIXER in doc["mixers"]
    assert SUB_CROSSOVER_FILTER in doc["filters"]
    # Existing correction stays first; the crossover runs LAST (just pre-DAC).
    for s in doc["pipeline"]:
        if s.get("type") == "Filter":
            assert s["names"][0] == "flat"
            assert s["names"][-1] == SUB_CROSSOVER_FILTER


def test_weave_preserves_volume_limit_ceiling():
    from jasper.multiroom.channel_split import weave_channel_split
    doc = yaml.safe_load(weave_channel_split(BASE_CONFIG, build_channel_split("sub")))
    assert doc["devices"]["volume_limit"] == 0.0  # safety floor untouched by the weave


def test_weave_fails_loud_when_config_missing_anchors():
    from jasper.multiroom.channel_split import weave_channel_split
    # No `mixers:` section -> cannot splice the channel_select mixer. The weave
    # must raise, never hand CamillaDSP a half-spliced (silent / mis-routed)
    # config.
    broken = "devices:\n  samplerate: 48000\npipeline:\n  - type: Mixer\n    name: master_gain\n"
    with pytest.raises(ValueError, match="weave failed|missing"):
        weave_channel_split(broken, build_channel_split("left"))


def test_augment_names_line_empty_and_nonempty():
    from jasper.multiroom.channel_split import _augment_names_line
    assert _augment_names_line("    names: []", ("sub_crossover",)) == \
        "    names: [sub_crossover]"
    assert _augment_names_line("    names: [a, b]", ("sub_crossover",)) == \
        "    names: [a, b, sub_crossover]"


def test_emit_sound_config_weaves_channel_split():
    """The live apply path: emit_sound_config(channel_split=...) weaves, and a
    passthrough/None leaves the config byte-for-byte."""
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SimpleEq, SoundProfile
    profile = SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=6.0))
    base = emit_sound_config(profile)
    assert emit_sound_config(profile, channel_split=build_channel_split("stereo")) == base
    assert emit_sound_config(profile, channel_split=None) == base
    woven = emit_sound_config(profile, channel_split=build_channel_split("sub"))
    doc = yaml.safe_load(woven)
    assert CHANNEL_SELECT_MIXER in doc["mixers"]
    assert "sub_crossover" in doc["filters"]


# ---------- hardened weave validation ----------


def test_weave_rejects_non_2_channel_config():
    """The splice appends to EACH per-channel Filter + routes a 2->2 mixer; a
    multi-driver / active-speaker config must fail LOUD, not mis-apply (the
    active-speaker weave is separate future work)."""
    from jasper.multiroom.channel_split import weave_channel_split
    four_ch = BASE_CONFIG.replace("channels: 2", "channels: 4")
    with pytest.raises(ValueError, match="2-channel"):
        weave_channel_split(four_ch, build_channel_split("left"))


def test_weave_validation_asserts_channel_select_runs_after_master_gain():
    """The validator asserts POSITION (immediately after master_gain), not just
    presence — defends the splice landing point against a future regression."""
    from jasper.multiroom.channel_split import _validate_woven
    # channel_select present but mis-positioned (after a Filter, not master_gain).
    bad = (
        "devices:\n  capture:\n    channels: 2\n  playback:\n    channels: 2\n"
        "mixers:\n  master_gain: {}\n  channel_select: {}\n"
        "pipeline:\n"
        "  - type: Mixer\n    name: master_gain\n"
        "  - type: Filter\n    channels: [0]\n    names: [flat]\n"
        "  - type: Mixer\n    name: channel_select\n"
    )
    with pytest.raises(ValueError, match="immediately after"):
        _validate_woven(bad, build_channel_split("left"))


def test_weave_rejects_config_missing_channels():
    """A config that OMITS `channels` (not just one that sets it != 2) is also
    rejected — the weave must not wave through an unverified channel count."""
    from jasper.multiroom.channel_split import weave_channel_split
    no_channels = BASE_CONFIG.replace("    channels: 2\n", "")
    with pytest.raises(ValueError, match="2-channel"):
        weave_channel_split(no_channels, build_channel_split("left"))
