# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations


import pytest

from jasper.audio_measurement.delay_graph import (
    DelayGraphProofError,
    prove_static_delay_binding,
    quantized_delay_ms,
)
from jasper.audio_measurement.null_walk import MAX_DSP_DELAY_US
POSITIVE_IDENTITY_FILTER = "positive_lane_identity"
NEGATIVE_IDENTITY_FILTER = "negative_lane_identity"


def _graph(
    positive_filter: str,
    negative_filter: str,
    *,
    compensated_peq: bool = False,
    positive_channels: tuple[int, ...] = (0,),
    negative_channels: tuple[int, ...] = (1,),
) -> dict:
    filters = {
        positive_filter: {
            "type": "Delay",
            "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
        },
        negative_filter: {
            "type": "Delay",
            "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
        },
        POSITIVE_IDENTITY_FILTER: {
            "type": "BiquadCombo",
            "parameters": {"type": "LinkwitzRileyHighpass", "freq": 5000.0},
        },
        NEGATIVE_IDENTITY_FILTER: {
            "type": "BiquadCombo",
            "parameters": {"type": "LinkwitzRileyLowpass", "freq": 5000.0},
        },
        "cut_only": {
            "type": "Gain",
            "parameters": {
                "gain": -6.0 if compensated_peq else -3.0,
                "inverted": False,
                "mute": False,
            },
        },
    }
    positive_names = [POSITIVE_IDENTITY_FILTER, positive_filter, "cut_only"]
    if compensated_peq:
        filters["compensated_peq"] = {
            "type": "Biquad",
            "parameters": {
                "type": "Peaking",
                "freq": 900.0,
                "q": 1.0,
                "gain": 4.0,
            },
        }
        positive_names.append("compensated_peq")
    return {
        "devices": {
            "samplerate": 48_000,
            "chunksize": 1024,
            "volume_limit": 0.0,
            "capture": {"type": "Alsa", "device": "capture"},
            "playback": {"type": "Alsa", "device": "playback"},
        },
        "filters": filters,
        "mixers": {
            "route": {
                "channels": {"in": 2, "out": 2},
                "mapping": [
                    {
                        "dest": 0,
                        "sources": [{"channel": 0, "gain": 0.0, "inverted": False}],
                    }
                ],
            }
        },
        "pipeline": [
            {"type": "Mixer", "name": "route"},
            {
                "type": "Filter",
                "channels": list(positive_channels),
                "names": positive_names,
            },
            {
                "type": "Filter",
                "channels": list(negative_channels),
                "names": [NEGATIVE_IDENTITY_FILTER, negative_filter],
            },
        ],
    }


# --- prove_static_delay_binding: the one-shot static (non-walk) proof ------


def test_prove_static_delay_binding_passes_for_a_matching_graph():
    graph = _graph("as_positive_delay", "as_negative_delay")
    graph["filters"]["as_positive_delay"]["parameters"]["delay"] = 0.34

    delay_ms = prove_static_delay_binding(
        graph,
        delay_filter_name="as_positive_delay",
        channels=(0,),
        delay_us=340.0,
    )
    assert delay_ms == pytest.approx(0.34)


def test_prove_static_delay_binding_rejects_negative_delay_us():
    graph = _graph(POSITIVE_IDENTITY_FILTER, NEGATIVE_IDENTITY_FILTER)
    with pytest.raises(DelayGraphProofError) as caught:
        prove_static_delay_binding(
            graph,
            delay_filter_name="cut_only",
            channels=(0,),
            delay_us=-1.0,
        )
    assert caught.value.code == "candidate_invalid"


def test_prove_static_delay_binding_rejects_delay_us_above_dsp_ceiling():
    graph = _graph(POSITIVE_IDENTITY_FILTER, NEGATIVE_IDENTITY_FILTER)
    with pytest.raises(DelayGraphProofError) as caught:
        prove_static_delay_binding(
            graph,
            delay_filter_name="cut_only",
            channels=(0,),
            delay_us=MAX_DSP_DELAY_US + 1.0,
        )
    assert caught.value.code == "candidate_invalid"


def test_prove_static_delay_binding_rejects_mismatched_delay_value():
    graph = _graph("as_positive_delay", "as_negative_delay")
    with pytest.raises(DelayGraphProofError) as caught:
        prove_static_delay_binding(
            graph,
            delay_filter_name="as_positive_delay",
            channels=(0,),
            delay_us=340.0,
        )
    assert caught.value.code == "delay_mismatch"


def test_prove_static_delay_binding_rejects_wrong_channel_binding():
    # The Delay filter is present and its value matches, but its pipeline step
    # is wired to channel 1 (negative_channels), not the requested channel 0.
    graph = _graph(
        "as_positive_delay",
        "as_negative_delay",
        positive_channels=(1,),
    )
    graph["filters"]["as_positive_delay"]["parameters"]["delay"] = 0.34
    with pytest.raises(DelayGraphProofError) as caught:
        prove_static_delay_binding(
            graph,
            delay_filter_name="as_positive_delay",
            channels=(0,),
            delay_us=340.0,
        )
    assert caught.value.code == "lane_binding_invalid"


def test_prove_static_delay_binding_rejects_filter_occurring_in_two_steps():
    graph = _graph("as_positive_delay", "as_negative_delay")
    graph["filters"]["as_positive_delay"]["parameters"]["delay"] = 0.34
    # Duplicate the positive filter into the negative lane's pipeline step too
    # — "exactly one pipeline step" is the load-bearing proof.
    graph["pipeline"][2]["names"].append("as_positive_delay")
    with pytest.raises(DelayGraphProofError) as caught:
        prove_static_delay_binding(
            graph,
            delay_filter_name="as_positive_delay",
            channels=(0,),
            delay_us=340.0,
        )
    assert caught.value.code == "delay_filter_invalid"


def test_prove_static_delay_binding_rejects_non_delay_filter():
    graph = _graph(POSITIVE_IDENTITY_FILTER, NEGATIVE_IDENTITY_FILTER)
    with pytest.raises(DelayGraphProofError) as caught:
        prove_static_delay_binding(
            graph,
            delay_filter_name="cut_only",
            channels=(0,),
            delay_us=0.0,
        )
    assert caught.value.code == "delay_filter_invalid"


def test_prove_static_delay_binding_rejects_positive_volume_limit():
    graph = _graph("as_positive_delay", "as_negative_delay")
    graph["filters"]["as_positive_delay"]["parameters"]["delay"] = 0.0
    graph["devices"]["volume_limit"] = 1.0
    with pytest.raises(DelayGraphProofError) as caught:
        prove_static_delay_binding(
            graph,
            delay_filter_name="as_positive_delay",
            channels=(0,),
            delay_us=0.0,
        )
    assert caught.value.code == "volume_limit_invalid"


def test_quantized_delay_ms_is_the_single_fmt_quantizer():
    # One fmt pass over the raw µs value — no intermediate rounding. The
    # regression value is the S1 case where round(µs/1000, 6)-then-fmt
    # disagreed with a single fmt.
    from jasper.camilla_emit import fmt

    delay_us = 11382.15006948647
    quantized = quantized_delay_ms(delay_us)
    assert quantized == float(fmt(delay_us / 1000.0))
    assert quantized != float(fmt(round(delay_us / 1000.0, 6)))
    # Idempotent: re-quantizing an already-quantized value is a no-op, so the
    # emitter's own fmt pass over the folded value cannot shift it again.
    assert quantized_delay_ms(quantized * 1000.0) == quantized
