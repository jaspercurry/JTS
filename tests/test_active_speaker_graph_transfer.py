# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact per-output graph transfers and typed refusals."""
from __future__ import annotations

import math

import numpy as np
import pytest

from jasper.active_speaker.graph_transfer import GraphTransferError, complex_channel_transfer
from jasper.active_speaker.crossover_v2 import summed_alignment
from jasper.active_speaker.crossover_v2.graph_prediction import (
    GraphPredictionError,
    relative_branch_response,
)

MONO_SUM_GAIN_DB = 20.0 * math.log10(0.5)  # -6.020599913…, the split mixer's leg



def _devices(*, rate: int = 48000, capture: int = 2, playback: int = 2) -> dict:
    return {
        "samplerate": rate,
        "capture": {"channels": capture},
        "playback": {"channels": playback},
    }


def _passthru_mixer() -> dict:
    return {
        "passthru": {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                {
                    "dest": d,
                    "sources": [{"channel": d, "gain": 0.0, "inverted": False}],
                }
                for d in (0, 1)
            ],
        }
    }


def _split_mixer() -> dict:
    """The active-speaker split: L+R mono-summed onto every driver output."""
    return {
        "split_active_2way": {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                {
                    "dest": d,
                    "sources": [
                        {"channel": 0, "gain": MONO_SUM_GAIN_DB, "inverted": False},
                        {"channel": 1, "gain": MONO_SUM_GAIN_DB, "inverted": False},
                    ],
                }
                for d in (0, 1)
            ],
        }
    }


def test_camilladsp_readback_spellings_are_accepted():
    config = {
        "devices": _devices(),
        "filters": {
            "g": {
                "type": "Gain",
                "description": None,
                "parameters": {
                    "gain": -3.0, "inverted": False, "mute": False, "scale": None,
                },
            },
            "d": {
                "type": "Delay",
                "description": None,
                "parameters": {"delay": 0.0, "unit": "ms", "subsample": None},
            },
        },
        "mixers": {
            "split_active_2way": {
                "channels": {"in": 2, "out": 2},
                "mapping": [
                    {
                        "dest": d,
                        "mute": None,
                        "sources": [
                            {
                                "channel": c,
                                "gain": MONO_SUM_GAIN_DB,
                                "inverted": False,
                                "mute": None,
                                "scale": None,
                            }
                            for c in (0, 1)
                        ],
                    }
                    for d in (0, 1)
                ],
            }
        },
        "pipeline": [
            {"type": "Mixer", "name": "split_active_2way", "bypassed": None},
            {"type": "Filter", "channel": 0, "names": ["g", "d"], "bypassed": None},
        ],
    }
    response = complex_channel_transfer(
        config, np.array([300.0]), input_weights={0: 1}, output_channels={"w": 0},
    )
    assert response["w"] == pytest.approx([0.5 * 10 ** (-3.0 / 20)])


def _refuses(config, *, output_channels=None):
    with pytest.raises(GraphTransferError):
        complex_channel_transfer(
            config, np.array([1000.0]), input_weights={0: 1},
            output_channels={"a": 0} if output_channels is None else output_channels,
        )


@pytest.mark.parametrize("filter_spec", [
    {"type": "Conv", "parameters": {"filename": "x.wav"}},
    {"type": "Volume", "parameters": {"ramp_time": 200}},
    {"type": "Biquad", "parameters": {"type": "LinkwitzTransform",
        "freq_act": 40.0, "q_act": 0.7, "freq_target": 30.0, "q_target": 0.7}},
    {"type": "BiquadCombo", "parameters": {"type": "Tilt", "freq": 60.0, "order": 2}},
    {"type": "Gain", "parameters": {"gain": 1.0, "scale": "volts"}},
    {"type": "Delay", "parameters": {"delay": 2.0, "unit": "furlongs"}},
])
def test_an_unmodelled_filter_refuses_rather_than_being_skipped(filter_spec):
    _refuses({"devices": _devices(), "filters": {"x": filter_spec},
              "pipeline": [{"type": "Filter", "channels": [0], "names": ["x"]}]})


def test_a_bypassed_pipeline_step_refuses():
    config = {
        "devices": _devices(),
        "filters": {},
        "mixers": _passthru_mixer(),
        "pipeline": [{"type": "Mixer", "name": "passthru", "bypassed": True}],
    }
    _refuses(config)


def test_an_undefined_filter_name_refuses():
    config = {
        "devices": _devices(),
        "filters": {},
        "mixers": _passthru_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["ghost"]},
        ],
    }
    _refuses(config)


def test_an_unknown_pipeline_step_or_mixer_refuses():
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Processor", "name": "compressor"}],
        },
    )
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": {},
            "pipeline": [{"type": "Mixer", "name": "absent"}],
        },
    )


def test_a_rate_the_shared_evaluator_does_not_model_refuses():
    _refuses(
        {
            "devices": _devices(rate=44100),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Mixer", "name": "passthru"}],
        },
    )


def test_an_output_channel_the_graph_does_not_reach_refuses():
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Mixer", "name": "passthru"}],
        },
        output_channels={"a": 7},
    )


def test_a_biquad_without_a_numeric_q_refuses():
    for params in (
        {"type": "Highshelf", "freq": 4000.0, "slope": 6.0, "gain": -6.0},
        {"type": "Peaking", "freq": 2600.0, "bandwidth": 0.5, "gain": -3.0},
        {"type": "Peaking", "freq": 2600.0, "q": None, "gain": -3.0},
        {"type": "Peaking", "freq": 2600.0, "q": "1.0", "gain": -3.0},
        {"type": "Peaking", "freq": 2600.0, "gain": -3.0},
    ):
        config = {
            "devices": _devices(),
            "filters": {"x": {"type": "Biquad", "parameters": params}},
            "mixers": _passthru_mixer(),
            "pipeline": [
                {"type": "Mixer", "name": "passthru"},
                {"type": "Filter", "channels": [0], "names": ["x"]},
            ],
        }
        _refuses(config)


def test_an_odd_linkwitz_riley_order_refuses():
    for order in (1, 3, 5):
        config = {
            "devices": _devices(),
            "filters": {
                "x": {
                    "type": "BiquadCombo",
                    "parameters": {
                        "type": "LinkwitzRileyLowpass", "freq": 1648.7, "order": order,
                    },
                }
            },
            "mixers": _passthru_mixer(),
            "pipeline": [
                {"type": "Mixer", "name": "passthru"},
                {"type": "Filter", "channels": [0], "names": ["x"]},
            ],
        }
        _refuses(config)


@pytest.mark.parametrize("config", [None, [], ["a", "b"], "just a string", 42])
def test_a_non_mapping_applied_config_refuses_typed(config):
    _refuses(config)


def test_complex_transfer_includes_shared_gain_polarity_delay_and_mono_sum():
    config = {
        "devices": _devices(),
        "filters": {
            "shared": {"type": "Gain", "parameters": {
                "gain": -3.0, "inverted": True, "mute": False,
            }},
            "delay": {"type": "Delay", "parameters": {
                "delay": 1, "unit": "samples",
            }},
        },
        "mixers": _split_mixer(),
        "pipeline": [
            {"type": "Filter", "names": ["shared", "delay"]},
            {"type": "Mixer", "name": "split_active_2way"},
        ],
    }
    freqs = np.asarray([100.0, 1000.0, 10000.0])
    response = complex_channel_transfer(
        config, freqs,
        input_weights={0: 1.0, 1: 1.0}, output_channels={"woofer": 0},
    )["woofer"]
    expected = -10.0 ** (-3.0 / 20.0) * np.exp(-2j * np.pi * freqs / 48000.0)
    assert response == pytest.approx(expected)


def test_complex_transfer_rounds_default_delay_to_camilladsp_samples():
    config = {
        "devices": _devices(),
        "filters": {"delay": {"type": "Delay", "parameters": {
            "delay": 0.03, "unit": "ms",
        }}},
        "mixers": _passthru_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["delay"]},
        ],
    }
    freqs = np.asarray([1000.0, 10000.0])
    response = complex_channel_transfer(
        config, freqs,
        input_weights={0: 1.0}, output_channels={"woofer": 0},
    )["woofer"]
    # 0.03 ms at 48 kHz is 1.44 samples. CamillaDSP defaults subsample to
    # false and rounds that request to a one-sample delay line.
    assert response == pytest.approx(np.exp(-2j * np.pi * freqs / 48000.0))


def test_complex_transfer_refuses_a_subsample_delay_allpass():
    config = {
        "devices": _devices(),
        "filters": {"delay": {"type": "Delay", "parameters": {
            "delay": 0.03, "unit": "ms", "subsample": True,
        }}},
        "mixers": _passthru_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["delay"]},
        ],
    }
    with pytest.raises(GraphTransferError):
        complex_channel_transfer(
            config, np.asarray([1000.0]),
            input_weights={0: 1.0}, output_channels={"woofer": 0},
        )


def test_relative_response_equates_role_routing_with_the_coherent_summed_mixer():
    source = {
        "devices": _devices(), "filters": {}, "mixers": _passthru_mixer(),
        "pipeline": [{"type": "Mixer", "name": "passthru"}],
    }
    target = {
        "devices": _devices(), "filters": {}, "mixers": _split_mixer(),
        "pipeline": [{"type": "Mixer", "name": "split_active_2way"}],
    }
    freqs = np.asarray([100.0, 1000.0, 10000.0])
    result = relative_branch_response(
        source, target, freqs,
        role_output_channels={"woofer": 0, "tweeter": 1},
        source_input_weights_by_role={
            "woofer": {0: 1.0}, "tweeter": {1: 1.0},
        },
        target_input_weights_by_role={
            "woofer": {0: 1.0, 1: 1.0},
            "tweeter": {0: 1.0, 1: 1.0},
        },
        valid_band_hz_by_role={
            "woofer": (100.0, 10000.0), "tweeter": (100.0, 10000.0),
        },
    )
    assert result.responses_by_role["woofer"] == pytest.approx(np.ones(3))
    assert result.responses_by_role["tweeter"] == pytest.approx(np.ones(3))
    assert result.to_dict()["limiter"]["model"] == "unchanged_pass_through"


def test_relative_response_masks_a_near_zero_source_instead_of_filling_it():
    silent = {
        "devices": _devices(),
        "filters": {"mute": {"type": "Gain", "parameters": {
            "gain": 0.0, "mute": True,
        }}},
        "mixers": _passthru_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["mute"]},
        ],
    }
    target = {
        "devices": _devices(), "filters": {}, "mixers": _passthru_mixer(),
        "pipeline": [{"type": "Mixer", "name": "passthru"}],
    }
    result = relative_branch_response(
        silent, target, np.asarray([100.0, 1000.0]),
        role_output_channels={"woofer": 0},
        source_input_weights_by_role={"woofer": {0: 1.0}},
        target_input_weights_by_role={"woofer": {0: 1.0}},
        valid_band_hz_by_role={"woofer": (100.0, 1000.0)},
    )
    assert not result.usable_by_role["woofer"].any()
    assert np.isnan(result.responses_by_role["woofer"]).all()
    assert result.excluded_bins_by_role["woofer"]["source_near_zero"] == 2


def test_relative_response_refuses_a_changed_limiter():
    def graph(limit):
        return {
            "devices": _devices(),
            "filters": {"lim": {"type": "Limiter", "parameters": {
                "soft_clip": True, "clip_limit": limit,
            }}},
            "mixers": _passthru_mixer(),
            "pipeline": [
                {"type": "Mixer", "name": "passthru"},
                {"type": "Filter", "channels": [0], "names": ["lim"]},
            ],
        }

    with pytest.raises(GraphPredictionError):
        relative_branch_response(
            graph(-1.0), graph(-2.0), np.asarray([1000.0]),
            role_output_channels={"woofer": 0},
            source_input_weights_by_role={"woofer": {0: 1.0}},
            target_input_weights_by_role={"woofer": {0: 1.0}},
            valid_band_hz_by_role={"woofer": (500.0, 2000.0)},
        )


def test_relative_response_allows_candidate_filters_before_an_unchanged_limiter():
    def graph(with_filter):
        filters = {
            "lim": {"type": "Limiter", "parameters": {
                "soft_clip": True, "clip_limit": -1.0,
            }},
            "candidate": {"type": "Biquad", "parameters": {
                "type": "Peaking", "freq": 1000.0, "q": 1.0, "gain": -3.0,
            }},
        }
        names = ["candidate", "lim"] if with_filter else ["lim"]
        return {
            "devices": _devices(), "filters": filters, "mixers": _passthru_mixer(),
            "pipeline": [
                {"type": "Mixer", "name": "passthru"},
                {"type": "Filter", "channels": [0], "names": names},
            ],
        }

    result = relative_branch_response(
        graph(True), graph(False), np.asarray([1000.0]),
        role_output_channels={"woofer": 0},
        source_input_weights_by_role={"woofer": {0: 1.0}},
        target_input_weights_by_role={"woofer": {0: 1.0}},
        valid_band_hz_by_role={"woofer": (500.0, 2000.0)},
    )
    assert result.usable_by_role["woofer"].tolist() == [True]
    assert abs(result.responses_by_role["woofer"][0]) > 1.0


@pytest.mark.parametrize("shape,expected", [
    ("Allpass", -1.0), ("ButterworthHighpass", 1j / np.sqrt(2)),
    ("ButterworthLowpass", -1j / np.sqrt(2)),
])
def test_complex_transfer_models_compiled_rear_filters(shape, expected):
    params = {"type": shape, "freq": 1000.0, **({"q": 0.5} if shape == "Allpass" else {"order": 2})}
    config = {"devices": _devices(), "filters": {"rear": {
        "type": "Biquad" if shape == "Allpass" else "BiquadCombo", "parameters": params}},
        "mixers": _passthru_mixer(), "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["rear"]}]}
    response = complex_channel_transfer(config, np.array([1000.0]), input_weights={0: 1}, output_channels={"rear": 0})
    assert response["rear"] == pytest.approx([expected])


def test_summed_alignment_evaluates_the_emitted_shelf_q():
    filters = {"shelf": {"type": "Biquad", "parameters": {
        "type": "Lowshelf", "freq": 200.0, "q": 1.0, "gain": 6.0,
    }}}
    response = summed_alignment.filter_transfer(
        ["shelf"], filters, np.geomspace(20.0, 20000.0, 4096),
    )
    assert np.max(20.0 * np.log10(abs(response))) > 6.3


@pytest.mark.parametrize("shape,passband_hz", [
    (None, 250.0), ("LinkwitzRileyLowpass", 250.0), ("LinkwitzRileyHighpass", 4000.0),
])
def test_complex_transfer_pins_split_gain_limiter_passthrough_and_lr4(shape, passband_hz):
    config = {
        "devices": _devices(), "mixers": _split_mixer(),
        "filters": {"xo": {"type": "BiquadCombo", "parameters": {
            "type": shape, "freq": 1000.0, "order": 4,
        }}} if shape else {},
        "pipeline": [{"type": "Mixer", "name": "split_active_2way"}],
    }
    if shape:
        config["pipeline"].append({"type": "Filter", "channels": [0], "names": ["xo"]})
    freqs = np.array([1000.0, passband_hz])
    kwargs = {
        "input_weights": {0: 1.0, 1: 1.0} if shape else {0: 1.0},
        "output_channels": {"out": 0}, "allow_limiter_passthrough": True,
    }
    response = complex_channel_transfer(config, freqs, **kwargs)["out"]
    assert abs(response[0]) == pytest.approx(0.5, abs=1e-3)
    assert 20 * np.log10(abs(response[1])) == pytest.approx(
        0.0 if shape else MONO_SUM_GAIN_DB, abs=0.05,
    )
    config["filters"]["limiter"] = {"type": "Limiter", "parameters": {
        "soft_clip": True, "clip_limit": -1.0,
    }}
    config["pipeline"].append({"type": "Filter", "channels": [0], "names": ["limiter"]})
    limited = complex_channel_transfer(config, freqs, **kwargs)["out"]
    assert np.allclose(limited, response)
