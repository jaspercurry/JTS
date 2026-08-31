# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one rule that decides whether a live EQ edit fades the speaker.

A pipeline replace ducks the fader for ~0.85 s; a parameter write does not.
:func:`jasper.sound.live_edit.plan_live_edit` picks between them by comparing
the two graphs, so what these pin is that a value may move freely and anything
structural falls back to the swap.
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
    """Every continuous EQ control is a number, and numbers patch."""
    plan = plan_live_edit(RUNNING, RUNNING.replace(swap_in, swap_out))

    assert plan.method == "patch"
    assert plan.reason == ""
    assert set(plan.patch) == {"filters"}
    # Only the filter that moved, and it carries its whole definition.
    (changed,) = plan.patch["filters"].values()
    assert changed["type"] == "Biquad"
    assert float(str(swap_out.split(": ")[1])) in changed["parameters"].values()


@pytest.mark.parametrize(
    "wanted, reason",
    [
        pytest.param(
            RUNNING.replace("type: Peaking", "type: Highshelf", 1),
            "filter_shape_differs",
            id="biquad_type_changed",
        ),
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
        pytest.param(
            RUNNING.replace("gain: 2.0", "gain: .nan"),
            "filter_shape_differs",
            id="unreadable_number",
        ),
        pytest.param("[unclosed", "graph_unparseable", id="unparseable"),
        pytest.param("just a string", "graph_not_a_mapping", id="not_a_mapping"),
        pytest.param(None, "graph_unreadable", id="unreadable"),
    ],
)
def test_anything_structural_falls_back_to_the_ducked_swap(wanted, reason):
    plan = plan_live_edit(RUNNING, wanted)

    assert plan.method == "swap"
    assert plan.patch is None
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
    assert plan.patch == {}


def test_two_bands_moving_together_patch_together():
    wanted = RUNNING.replace("gain: 2.0", "gain: 4.0").replace("gain: -1.0", "gain: -6.0")

    plan = plan_live_edit(RUNNING, wanted)

    assert plan.method == "patch"
    assert set(plan.patch["filters"]) == {"sound_advanced_1", "sound_advanced_2"}
