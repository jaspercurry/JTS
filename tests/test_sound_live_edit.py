# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one rule that decides whether a live EQ edit fades the speaker.

A pipeline replace ducks the fader for ~0.85 s; a parameter write does not.
:func:`jasper.sound.live_edit.plan_live_edit` picks between them by comparing
the two graphs, so what these pin is that a value — and even a biquad's own
``type`` string, which lives under ``parameters`` — may move freely, and
anything structural (sections, devices, pipeline, the filter set, or a
filter's OUTER kind) falls back to the ducked swap.
"""

from __future__ import annotations

import pytest

from jasper.sound.live_edit import plan_live_edit

RUNNING = """
devices:
  samplerate: 48000
  chunksize: 1024
filters:
  sound_advanced_1:
    type: Biquad
    description: null
    parameters: {type: Peaking, freq: 1000.0, q: 1.0, gain: 2.0}
  sound_advanced_2:
    type: Biquad
    description: null
    parameters: {type: Peaking, freq: 4000.0, q: 2.0, gain: -1.0}
pipeline:
  - type: Filter
    channels: [0, 1]
    names: [sound_advanced_1, sound_advanced_2]
"""


@pytest.mark.parametrize(
    "swap_in, swap_out",
    [
        pytest.param("gain: 2.0", "gain: 5.5", id="gain_moved"),
        pytest.param("freq: 1000.0", "freq: 120.0", id="frequency_dragged"),
        pytest.param("q: 1.0", "q: 6.25", id="q_narrowed"),
        pytest.param("gain: 2.0", "gain: 0.0", id="gain_dragged_through_zero"),
        pytest.param("gain: -1.0", "gain: 3.0", id="gain_crossed_sign"),
    ],
)
def test_a_number_moving_is_written_as_a_parameter(swap_in, swap_out):
    """Every continuous EQ control is a number, and numbers write in place."""
    plan = plan_live_edit(RUNNING, RUNNING.replace(swap_in, swap_out))

    assert plan.method == "parameters"
    assert plan.reason == ""


def test_two_bands_moving_together_is_still_a_parameter_write():
    wanted = RUNNING.replace("gain: 2.0", "gain: 4.0").replace("gain: -1.0", "gain: -6.0")

    assert plan_live_edit(RUNNING, wanted).method == "parameters"


def test_a_biquad_retype_is_a_parameter_write():
    """A biquad's ``type`` lives under ``parameters``; only the KIND ducks."""
    wanted = RUNNING.replace("type: Peaking", "type: Highshelf", 1)

    assert plan_live_edit(RUNNING, wanted).method == "parameters"


def test_a_retype_to_a_gainless_shape_is_still_a_parameter_write():
    """A Highpass has no ``gain`` — the parameter key set may differ too."""
    wanted = RUNNING.replace(
        "parameters: {type: Peaking, freq: 1000.0, q: 1.0, gain: 2.0}",
        "parameters: {type: Highpass, freq: 80.0, q: 0.7}",
    )

    assert plan_live_edit(RUNNING, wanted).method == "parameters"


def test_the_filters_outer_kind_changing_is_a_ducked_swap():
    """``Biquad`` to ``Gain`` rebuilds the filter group; a retyped biquad does not."""
    wanted = RUNNING.replace(
        "  sound_advanced_1:\n"
        "    type: Biquad\n"
        "    description: null\n"
        "    parameters: {type: Peaking, freq: 1000.0, q: 1.0, gain: 2.0}\n",
        "  sound_advanced_1:\n"
        "    type: Gain\n"
        "    description: null\n"
        "    parameters: {gain: 0.0}\n",
    )

    plan = plan_live_edit(RUNNING, wanted)

    assert plan.method == "swap"
    assert plan.reason == "filter_kind_differs"


@pytest.mark.parametrize(
    "wanted, reason",
    [
        pytest.param(
            RUNNING.replace("names: [sound_advanced_1, sound_advanced_2]",
                            "names: [sound_advanced_1]"),
            "pipeline_differs",
            id="band_dropped_from_the_chain",
        ),
        pytest.param(
            RUNNING.replace("samplerate: 48000", "samplerate: 44100"),
            "devices_differs",
            id="device_touched",
        ),
        pytest.param("[unclosed", "graph_unparseable", id="unparseable"),
        pytest.param("just a string", "graph_not_a_mapping", id="not_a_mapping"),
        pytest.param(None, "graph_unreadable", id="unreadable"),
    ],
)
def test_anything_structural_falls_back_to_the_ducked_swap(wanted, reason):
    plan = plan_live_edit(RUNNING, wanted)

    assert plan.method == "swap"
    assert plan.reason == reason


def test_a_filter_the_running_graph_does_not_have_is_a_swap():
    wanted = RUNNING.replace(
        "pipeline:",
        "  sound_advanced_3:\n"
        "    type: Biquad\n"
        "    description: null\n"
        "    parameters: {type: Peaking, freq: 60.0, q: 1.0, gain: 3.0}\n"
        "pipeline:",
    )

    plan = plan_live_edit(RUNNING, wanted)

    assert plan.method == "swap"
    assert plan.reason == "filter_set_differs"


def test_an_identical_graph_is_not_written_at_all():
    """A redraw that changed nothing must not cost a swap, or anything."""
    plan = plan_live_edit(RUNNING, RUNNING)

    assert plan.method == "unchanged"
    assert plan.duck is False


@pytest.mark.parametrize(
    "method, expected_duck",
    [("swap", True), ("parameters", False), ("unchanged", False)],
)
def test_only_a_swap_ducks(method, expected_duck):
    from jasper.sound.live_edit import LiveEditPlan

    assert LiveEditPlan(method).duck is expected_duck
