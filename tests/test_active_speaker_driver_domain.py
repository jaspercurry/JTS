# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver-domain-only active emit variant — the follower's relocated Layer A.

Covers the emitter half of distributed-active Slice 2 and pins **invariant 4**
(``docs/HANDOFF-distributed-active.md``): the driver-domain-only graph has no
program-prefix filter, no positive gains, ``volume_limit == 0.0``, and the
inter-speaker channel-select precedes the intra-speaker split. The
classifier-side keystone (invariant 3) lives in
``test_active_speaker_runtime_contract.py``.
"""
from __future__ import annotations

import pytest
import yaml

from jasper.active_speaker import (
    ActiveSpeakerPreset,
    DRIVER_DOMAIN_PROGRAM_CHANNELS,
    channel_select_mixer_name,
    emit_active_speaker_baseline_config,
    emit_active_speaker_driver_domain_config,
)
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.camilla_emit import CHANNEL_SELECT_MIXER, MONO_SUM_GAIN_DB
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def _preset(layout: str, way: int) -> ActiveSpeakerPreset:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return ActiveSpeakerPreset.from_mapping(raw)


def _emit(layout: str, way: int, channel: str, **kw) -> str:
    return emit_active_speaker_driver_domain_config(
        _preset(layout, way),
        playback_device=ACTIVE_PCM,
        program_channel=channel,
        **kw,
    )


def _doc(layout: str = "mono", way: int = 2, channel: str = "right", **kw) -> dict:
    return yaml.safe_load(_emit(layout, way, channel, **kw))


def _mixer_step_names(doc: dict) -> list[str]:
    return [
        step["name"]
        for step in doc["pipeline"]
        if step.get("type") == "Mixer"
    ]


def _filter_step_channels(doc: dict) -> list[list[int]]:
    return [
        step["channels"]
        for step in doc["pipeline"]
        if step.get("type") == "Filter"
    ]


_CASES = [
    (layout, way, channel)
    for layout in ("mono", "stereo")
    for way in (2, 3)
    for channel in DRIVER_DOMAIN_PROGRAM_CHANNELS
]


# --- invariant 4: graph shape ------------------------------------------------


@pytest.mark.parametrize("layout,way,channel", _CASES)
def test_channel_select_precedes_split(layout: str, way: int, channel: str) -> None:
    names = _mixer_step_names(_doc(layout, way, channel))
    assert channel_select_mixer_name in names
    split = [n for n in names if n.startswith("split_active_")]
    assert split, "driver-domain graph must contain the split mixer"
    assert names.index(channel_select_mixer_name) < names.index(split[0])


@pytest.mark.parametrize("layout,way,channel", _CASES)
def test_no_program_prefix(layout: str, way: int, channel: str) -> None:
    doc = _doc(layout, way, channel)
    # The leader baked Layer B/C: no program-domain headroom gain exists...
    assert "active_baseline_headroom" not in doc["filters"]
    # ...and no Filter pipeline step targets the [0, 1] program bus except the
    # live pair-balance scalar between channel-select and the driver split.
    program_filter_steps = [
        step
        for step in doc["pipeline"]
        if step.get("type") == "Filter"
        and step.get("channels") == [0, 1]
        and step.get("names") != ["pair_balance_trim"]
    ]
    assert program_filter_steps == []


@pytest.mark.parametrize("layout,way,channel", _CASES)
def test_volume_limit_is_zero(layout: str, way: int, channel: str) -> None:
    assert _doc(layout, way, channel)["devices"]["volume_limit"] == 0.0


@pytest.mark.parametrize("layout,way,channel", _CASES)
def test_no_positive_gain_anywhere(layout: str, way: int, channel: str) -> None:
    doc = _doc(layout, way, channel)
    for name, spec in doc["filters"].items():
        if spec.get("type") == "Gain":
            assert spec["parameters"]["gain"] <= 0.0, f"{name} has positive gain"
    for mixer in doc["mixers"].values():
        for dest in mixer["mapping"]:
            for src in dest["sources"]:
                assert src["gain"] <= 0.0, "mixer source gain must be non-positive"


@pytest.mark.parametrize("channel,expected", [
    ("left", [(0, 0.0)]),
    ("right", [(1, 0.0)]),
    ("mono", [(0, MONO_SUM_GAIN_DB), (1, MONO_SUM_GAIN_DB)]),
])
def test_channel_select_picks_the_program_channel(channel, expected) -> None:
    doc = _doc(channel=channel)
    mixer = doc["mixers"][CHANNEL_SELECT_MIXER]
    assert mixer["channels"] == {"in": 2, "out": 2}
    for dest in mixer["mapping"]:  # both outputs carry the same picked content
        got = [(s["channel"], pytest.approx(s["gain"], abs=1e-3)) for s in dest["sources"]]
        assert got == [(c, pytest.approx(g, abs=1e-3)) for c, g in expected]


# --- the relocated Layer A is byte-for-byte the solo baseline's driver chain --


@pytest.mark.parametrize("layout,way", [("mono", 2), ("stereo", 2), ("mono", 3), ("stereo", 3)])
def test_driver_chain_matches_baseline(layout: str, way: int) -> None:
    preset = _preset(layout, way)
    corrections = {
        "woofer": {"gain_db": -1.25, "delay_ms": 0.15, "inverted": True},
        "mid": {"gain_db": -2.0, "delay_ms": 0.3, "inverted": False},
        "tweeter": {"gain_db": -2.75, "delay_ms": 0.45, "inverted": True},
    }
    follower = yaml.safe_load(emit_active_speaker_driver_domain_config(
        preset,
        playback_device=ACTIVE_PCM,
        program_channel="left",
        corrections=corrections,
    ))
    baseline = yaml.safe_load(emit_active_speaker_baseline_config(
        preset,
        playback_device=ACTIVE_PCM,
        corrections=corrections,
    ))
    # Drop the program-domain headroom the baseline carries (and the follower
    # must not) plus the follower's inter-speaker balance trim: the remaining
    # per-driver crossover/delay/gain/limiter chain is IDENTICAL, so relocating
    # Layer A onto a follower cannot weaken protection.
    baseline_driver_filters = {
        k: v for k, v in baseline["filters"].items() if k != "active_baseline_headroom"
    }
    follower_driver_filters = {
        k: v for k, v in follower["filters"].items() if k != "pair_balance_trim"
    }
    assert follower_driver_filters == baseline_driver_filters
    assert follower["mixers"]["split_active_%dway" % way] == \
        baseline["mixers"]["split_active_%dway" % way]


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("bad", ["stereo", "sub", "", "garbage"])
def test_rejects_non_follower_channel(bad: str) -> None:
    with pytest.raises(ActiveSpeakerConfigError):
        _emit("mono", 2, bad)


@pytest.mark.parametrize("device", ["plughw:jasper_out", "jasper_out"])
def test_rejects_stereo_outputd_playback_lane(device: str) -> None:
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_driver_domain_config(
            _preset("mono", 2), playback_device=device, program_channel="left"
        )


def test_rejects_positive_correction_gain() -> None:
    with pytest.raises(ActiveSpeakerConfigError):
        _emit("mono", 2, "left", corrections={"woofer": {"gain_db": 3.0}})


@pytest.mark.parametrize("emitter", ["baseline", "driver_domain"])
@pytest.mark.parametrize(
    "corrections",
    [
        {"woofer": {"gain_db": 0.01}},
        {"woofer": {"delay_ms": -0.01}},
        {"woofer": {"delay_ms": 20.01}},
    ],
)
def test_both_emitters_share_correction_safety_gate(
    emitter: str,
    corrections: dict[str, dict[str, float | bool]],
) -> None:
    preset = _preset("mono", 2)
    with pytest.raises(ActiveSpeakerConfigError):
        if emitter == "baseline":
            emit_active_speaker_baseline_config(
                preset,
                playback_device=ACTIVE_PCM,
                corrections=corrections,
            )
        else:
            emit_active_speaker_driver_domain_config(
                preset,
                playback_device=ACTIVE_PCM,
                program_channel="left",
                corrections=corrections,
            )


def test_chunksize_floored_to_loopback_minimum() -> None:
    # S1: the follower captures the leader's stream from an snd-aloop loopback
    # that EPIPEs below ~1024 frames, so the G7 chunksize knob is floored here
    # even though the direct-DAC paths may tune it lower.
    from jasper.active_speaker.camilla_yaml import FOLLOWER_LOOPBACK_MIN_CHUNKSIZE

    # A sub-floor explicit value is clamped up to the loopback minimum.
    assert (
        _doc(chunksize=256)["devices"]["chunksize"]
        == FOLLOWER_LOOPBACK_MIN_CHUNKSIZE
    )
    # A value at/above the floor passes through unchanged.
    assert _doc(chunksize=4096)["devices"]["chunksize"] == 4096
    # The shipped default (unset) is exactly the floor — byte-identical, no clamp.
    assert _doc()["devices"]["chunksize"] == FOLLOWER_LOOPBACK_MIN_CHUNKSIZE


def test_chunksize_env_override_is_floored(monkeypatch) -> None:
    # The runtime path leaves chunksize=None so resolve_camilla_chunksize() reads
    # the env; a too-low operator override must not reach the loopback emitter
    # unclamped (the EPIPE landmine S1 guards against).
    from jasper.active_speaker.camilla_yaml import FOLLOWER_LOOPBACK_MIN_CHUNKSIZE

    monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", "256")
    assert _doc()["devices"]["chunksize"] == FOLLOWER_LOOPBACK_MIN_CHUNKSIZE


def test_threads_capture_device() -> None:
    # The gap-1 seam: the reconciler will pass the round-trip loopback here.
    doc = _doc(channel="left", capture_device="loop:0,1")
    assert doc["devices"]["capture"]["device"] == "loop:0,1"
    # Default keeps the fan-in tap (unchanged from the solo baseline capture).
    assert _doc(channel="left")["devices"]["capture"]["device"] == "plug:jasper_capture"


def test_metadata_records_program_channel() -> None:
    assert "# program_channel=right" in _emit("mono", 2, "right")


# --- cross-module name contract (the point of the shared-leaf promotion) ------


def test_channel_select_mixer_name_is_one_shared_constant() -> None:
    from jasper.multiroom.channel_split import CHANNEL_SELECT_MIXER as cs_split

    assert channel_select_mixer_name == CHANNEL_SELECT_MIXER == cs_split == "channel_select"
