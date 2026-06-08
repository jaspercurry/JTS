"""Tests for the P1.2 channel-split CamillaDSP fragment generator.

These assert the DSP-correctness + safety invariants documented in
jasper/multiroom/channel_split.py:
  - master_gain is never touched (Ducker contract);
  - no positive source gains (clip safety);
  - mono/sub sums are exactly -6.02 dB (identical L==R -> 0 dBFS);
  - the LR4 sub crossover is two Butterworth lowpasses at the corner;
  - stereo is a true passthrough;
  - the emitted YAML parses and composes into the real base config.
"""
from __future__ import annotations

import math
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

    # ...plus a 2-section (LR4) Butterworth lowpass crossover.
    assert split.filter_chain_names == ("sub_lp_1", "sub_lp_2")
    filters = _parse_filters(split.filter_block)
    assert set(filters) == {"sub_lp_1", "sub_lp_2"}
    for name in ("sub_lp_1", "sub_lp_2"):
        f = filters[name]
        assert f["type"] == "Biquad"
        assert f["parameters"]["type"] == "Lowpass"
        assert f["parameters"]["freq"] == pytest.approx(DEFAULT_CROSSOVER_HZ)
        # Butterworth Q = 1/sqrt(2); two cascaded == LR4 (24 dB/oct).
        assert f["parameters"]["q"] == pytest.approx(1.0 / math.sqrt(2.0), abs=1e-3)


def test_sub_crossover_hz_is_tunable():
    split = build_channel_split("sub", crossover_hz=120.0)
    filters = _parse_filters(split.filter_block)
    for name in ("sub_lp_1", "sub_lp_2"):
        assert filters[name]["parameters"]["freq"] == pytest.approx(120.0)


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
        # crossover sections run LAST, after `flat` (room correction / EQ).
        assert step["names"][-2:] == ["sub_lp_1", "sub_lp_2"]


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
                "room_peq_1", "room_peq_2", "flat", "sub_lp_1", "sub_lp_2",
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
        assert {"sub_lp_1", "sub_lp_2"} <= set(cfg["filters"])


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
    assert sub.filter_chain_names == ("sub_lp_1", "sub_lp_2")


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
