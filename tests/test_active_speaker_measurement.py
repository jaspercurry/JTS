# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.active_speaker.measurement import (
    active_driver_targets,
    active_summed_targets,
    empty_driver_check_summary,
)
from jasper.output_topology import OutputTopology, canonical_fingerprint
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


@pytest.mark.parametrize(
    ("mode", "fingerprint"),
    (
        ("active_2_way", "ee54aa80f2d967077a93e6c73262002a3465a83ffe353c999e7dfff873a221b1"),
        ("active_3_way", "a61150e650663a972ef12fd8b5301037d2522d5ff5b210a2a9c6f6500090aff0"),
        ("full_range_passive", "34163bbba7f11682eb3df1523ca4bde28c32b38ad211e7cdc0aa9ca28c7f1744"),
    ),
)
def test_empty_driver_check_summary_keeps_the_retired_record_fingerprint(
    mode: str, fingerprint: str,
) -> None:
    """Applied profiles carry this hash as ``source.measurement_summary_fingerprint``.

    Each value is what the retired record answered for the fixture with no
    records, so a change here re-fingerprints every applied profile.
    """
    summary = empty_driver_check_summary(mono_output_topology(mode=mode))

    assert canonical_fingerprint(summary) == fingerprint
