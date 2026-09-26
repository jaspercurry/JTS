# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Driver-domain graph fixtures and their shared baseline invariants."""
from __future__ import annotations

import re

import pytest
import yaml

from jasper.active_speaker import (
    ActiveSpeakerPreset,
    channel_select_mixer_name,
    emit_active_speaker_baseline_config,
)
from jasper.active_speaker.camilla_yaml.decorate_dynamic_bass import _with_dynamic_bass
from jasper.active_speaker.output_contract import ACTIVE_BASELINE_SOURCE, ACTIVE_DRIVER_DOMAIN_SOURCE
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.camilla_emit import CHANNEL_SELECT_MIXER, MONO_SUM_GAIN_DB, emit_channel_select_mixer, emit_gain_filter
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def driver_domain_graph(preset, *, playback_device, program_channel, pair_trim_db=0.0,
                        bass_extension=None, **kwargs):
    """Relocate a baseline for verifier tests, preserving its text-mutation seams."""
    text = emit_active_speaker_baseline_config(preset, playback_device=playback_device, **kwargs)
    text = text.replace(ACTIVE_BASELINE_SOURCE, ACTIVE_DRIVER_DOMAIN_SOURCE)
    text = re.sub(r"  active_baseline_headroom:\n(?:    [^\n]*\n)+", "", text)
    text = text.replace("active_baseline_headroom", "pair_balance_trim")
    text = text.replace("filters:\n", "filters:\n" + "\n".join(
        emit_gain_filter("pair_balance_trim", -pair_trim_db)) + "\n", 1)
    text = text.replace("mixers:\n", "mixers:\n" + emit_channel_select_mixer(program_channel) + "\n", 1)
    text = text.replace("pipeline:\n", "pipeline:\n  - type: Mixer\n    name: channel_select\n", 1)
    text = f"# program_channel={program_channel}\n# pair_trim_db={pair_trim_db:.3f}\n" + text
    return _with_dynamic_bass(text, preset, bass_extension)


def _preset(layout: str, way: int) -> ActiveSpeakerPreset:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return ActiveSpeakerPreset.from_mapping(raw)


def _emit(layout: str, way: int, channel: str, **kw) -> str:
    return driver_domain_graph(
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


_CASES = [
    (layout, way, channel)
    for layout in ("mono", "stereo")
    for way in (2, 3)
    for channel in ("left", "right", "mono")
]




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
    assert "active_baseline_headroom" not in doc["filters"]
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




@pytest.mark.parametrize("layout,way", [("mono", 2), ("stereo", 2), ("mono", 3), ("stereo", 3)])
def test_driver_chain_matches_baseline(layout: str, way: int) -> None:
    preset = _preset(layout, way)
    corrections = {
        "woofer": {"gain_db": -1.25, "delay_ms": 0.15, "inverted": True},
        "mid": {"gain_db": -2.0, "delay_ms": 0.3, "inverted": False},
        "tweeter": {"gain_db": -2.75, "delay_ms": 0.45, "inverted": True},
    }
    follower = yaml.safe_load(driver_domain_graph(
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
    baseline_driver_filters = {
        k: v for k, v in baseline["filters"].items() if k != "active_baseline_headroom"
    }
    follower_driver_filters = {
        k: v for k, v in follower["filters"].items() if k != "pair_balance_trim"
    }
    assert follower_driver_filters == baseline_driver_filters
    assert follower["mixers"]["split_active_%dway" % way] == \
        baseline["mixers"]["split_active_%dway" % way]




@pytest.mark.parametrize("device", ["plughw:jasper_out", "jasper_out"])
def test_rejects_stereo_outputd_playback_lane(device: str) -> None:
    with pytest.raises(ActiveSpeakerConfigError):
        driver_domain_graph(
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
            driver_domain_graph(
                preset,
                playback_device=ACTIVE_PCM,
                program_channel="left",
                corrections=corrections,
            )


def test_emits_the_ring_chunk_it_is_given() -> None:
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAMILLA_CHUNKSIZE,
    )

    doc = yaml.safe_load(
        driver_domain_graph(
            _preset("mono", 2),
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            program_channel="left",
            chunksize=RING_CAMILLA_CHUNKSIZE,
        )
    )
    assert doc["devices"]["chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert _doc(chunksize=256)["devices"]["chunksize"] == 256
    assert _doc(chunksize=4096)["devices"]["chunksize"] == 4096


def test_chunksize_env_override_reaches_the_emitter(monkeypatch) -> None:
    monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", "256")
    assert _doc()["devices"]["chunksize"] == 256


def test_threads_capture_device() -> None:
    doc = _doc(channel="left", capture_device="loop:0,1")
    assert doc["devices"]["capture"]["device"] == "loop:0,1"
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE

    assert (
        _doc(channel="left")["devices"]["capture"]["device"] == RING_CAPTURE_DEVICE
    )


def test_metadata_records_program_channel() -> None:
    assert "# program_channel=right" in _emit("mono", 2, "right")




def test_channel_select_mixer_name_is_one_shared_constant() -> None:
    assert channel_select_mixer_name == CHANNEL_SELECT_MIXER == "channel_select"
