# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The applied profile's crossover-corner reader, on the 2026-08-16 jts3 round-1
profile (#2611, #2614)."""
from __future__ import annotations

import pytest

from jasper.active_speaker.crossover_v2 import commanded as cmd

#: The round-3 profile the round-1 apply replaced.
PREVIOUS_TWEETER_TRIM_DB = -10.2141
PREVIOUS_DELAY_US = 96.0

FC_HZ = 1500.0


def _incident_profile(*, fc_hz: float = FC_HZ) -> dict[str, object]:
    """The round-3 applied profile, in the shape the reader consumes.

    ``fc_hz`` is the corner the graph was built at, which
    :func:`~jasper.active_speaker.crossover_v2.commanded.profile_crossover_fc_hz`
    reads off the snapshot preset and the session checks the capture against.
    """
    from tests.test_active_speaker_profile import _two_way_preset

    preset = _two_way_preset()
    preset["crossover_regions"] = [
        {**region, "fc_hz": float(fc_hz)} for region in preset["crossover_regions"]
    ]
    return {
        "status": "applied",
        "recomposition_snapshot": {
            "preset": preset,
            "corrections": {
                "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
                "tweeter": {
                    "gain_db": PREVIOUS_TWEETER_TRIM_DB,
                    "delay_ms": PREVIOUS_DELAY_US / 1000.0,
                    "inverted": True,
                },
            },
            "linearization": {},
        },
    }


def test_the_snapshot_preset_names_the_corner_the_graph_was_built_at():
    """The corner reader, and its "cannot say" (#2614).

    ``None`` for a profile with no snapshot preset — an era-older record — so
    the session refuses rather than affirming a previous graph whose crossover
    it cannot check.
    """
    assert cmd.profile_crossover_fc_hz(
        _incident_profile(fc_hz=1234.0),
    ) == pytest.approx(1234.0)
    assert cmd.profile_crossover_fc_hz(None) is None
    assert cmd.profile_crossover_fc_hz({"status": "applied"}) is None
    assert cmd.profile_crossover_fc_hz(
        {"recomposition_snapshot": {"corrections": {}}},
    ) is None
