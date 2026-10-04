# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.active_speaker.measurement import (
    active_driver_targets,
    active_summed_targets,
)
from jasper.audio_routes.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology


def _topology() -> OutputTopology:
    return mono_output_topology(topology_name="Bench mono")


def test_active_driver_and_summed_targets() -> None:
    topology = _topology()

    assert [target["target_id"] for target in active_driver_targets(topology)] == [
        "mono:woofer",
        "mono:tweeter",
    ]
    assert [target["speaker_group_id"] for target in active_summed_targets(topology)] == [
        "mono",
    ]
