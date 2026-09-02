# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The N-candidate rank over independent delta-probe gradings (#3498 WP4).

Synthetic maps: what is under test is the SELECTION and the ORDER, not the
grading behind them, so every map here is a hand-built
:class:`~jasper.active_speaker.delta_probe.DeltaProbeMap` with the one field
the rank reads varied. The verdicts come from the module's own constants —
naming a rollback string here would be a second copy of a vocabulary that has
one owner.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker.crossover_v2.candidate_comparator import (
    COMPARISON_REASONS,
    REASON_INSIDE_REPEAT_FLOOR,
    REASON_NO_SURVIVOR,
    REASON_REPEAT_FLOOR_UNKNOWN,
    REASON_SEPARATED,
    REASON_SINGLE_CANDIDATE,
    compare_candidates,
)
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    DELTA_PROBE_TOLERANCE_HIGH_DB,
    DELTA_PROBE_TOLERANCE_LOW_DB,
    SPATIAL_COST_UNAVAILABLE,
    VERDICT_MATCHED,
    VERDICT_SAFETY_ONLY,
    VERDICT_UNAVAILABLE,
    DeltaProbeMap,
)

_ROLLBACK = sorted(DELTA_PROBE_ROLLBACK_VERDICTS)[0]


def _map(
    verdict: str, rms_error_db: float, frame_removed_rms_db: float | None = None,
) -> DeltaProbeMap:
    return DeltaProbeMap(
        verdict=verdict, reason="", probe_band_hz=(200.0, 5000.0), n_bins=8,
        max_error_db=rms_error_db * 2.0, rms_error_db=rms_error_db,
        worst_hz=1000.0, exceedance_octaves=0.25, gain_factor=0.9,
        tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
        tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
        spatial=SPATIAL_COST_UNAVAILABLE,
        frame_removed_rms_db=frame_removed_rms_db,
    )


@pytest.mark.parametrize(
    "gradings,repeat_floor_db,winner,reason,separated,order",
    [
        pytest.param(
            {"a": (VERDICT_MATCHED, 1.0), "b": (VERDICT_MATCHED, 3.0)},
            0.5, "a", REASON_SEPARATED, True, ["a", "b"],
            id="lowest_rms_wins_when_the_gap_clears_the_floor",
        ),
        pytest.param(
            {"a": (VERDICT_MATCHED, 1.0), "b": (VERDICT_MATCHED, 1.2)},
            0.5, "a", REASON_INSIDE_REPEAT_FLOOR, False, ["a", "b"],
            id="a_gap_inside_the_floor_names_a_winner_and_no_separation",
        ),
        pytest.param(
            {"a": (VERDICT_MATCHED, 1.0), "b": (VERDICT_MATCHED, 1.5)},
            0.5, "a", REASON_INSIDE_REPEAT_FLOOR, False, ["a", "b"],
            id="a_gap_exactly_on_the_floor_is_the_floor_not_an_ordering",
        ),
        pytest.param(
            {"a": (VERDICT_MATCHED, 1.0), "b": (VERDICT_MATCHED, 3.0)},
            None, "a", REASON_REPEAT_FLOOR_UNKNOWN, False, ["a", "b"],
            id="an_unmeasured_floor_withholds_the_separation_claim",
        ),
        pytest.param(
            {"a": (VERDICT_MATCHED, 1.0), "b": (VERDICT_MATCHED, 1.0)},
            0.0, "a", REASON_REPEAT_FLOOR_UNKNOWN, False, ["a", "b"],
            id="a_zero_floor_cannot_separate_a_tie",
        ),
        pytest.param(
            {
                "a": (VERDICT_MATCHED, 1.0, 2.0),
                "b": (VERDICT_MATCHED, 2.0, 0.5),
            },
            0.5, "b", REASON_SEPARATED, True, ["b", "a"],
            id="the_frame_removed_rms_is_what_is_ranked_when_a_map_carries_one",
        ),
        pytest.param(
            {"a": (VERDICT_MATCHED, 2.0), "b": (_ROLLBACK, 0.1)},
            0.5, "a", REASON_SINGLE_CANDIDATE, False, ["a", "b"],
            id="a_rollback_is_excluded_however_low_its_error",
        ),
        pytest.param(
            {
                "a": (VERDICT_MATCHED, 2.0),
                "b": (VERDICT_UNAVAILABLE, 0.0),
                "c": (VERDICT_SAFETY_ONLY, 0.0),
            },
            0.5, "a", REASON_SINGLE_CANDIDATE, False, ["a", "b", "c"],
            id="an_ungraded_map_is_not_a_perfect_one",
        ),
        pytest.param(
            {"a": (VERDICT_MATCHED, 1.0)},
            0.5, "a", REASON_SINGLE_CANDIDATE, False, ["a"],
            id="one_candidate_is_named_without_a_comparison",
        ),
        pytest.param(
            {"a": (_ROLLBACK, 1.0), "b": (VERDICT_UNAVAILABLE, 0.0)},
            0.5, "", REASON_NO_SURVIVOR, False, ["a", "b"],
            id="no_survivor_names_no_winner",
        ),
    ],
)
def test_the_rank_selects_then_orders(
    gradings, repeat_floor_db, winner, reason, separated, order,
):
    comparison = compare_candidates(
        {cid: _map(*grade) for cid, grade in gradings.items()},
        pose_count=1,
        repeat_floor_db=repeat_floor_db,
    )
    assert comparison.winner == winner
    assert comparison.reason == reason
    assert comparison.reason in COMPARISON_REASONS
    assert comparison.separated is separated
    # Every graded candidate is carried, survivors first: a reader sees the
    # whole field that played, not only the part of it that survived.
    assert [rank.candidate_id for rank in comparison.ranked] == order


def test_each_rank_carries_its_own_maps_numbers():
    probe = _map(VERDICT_MATCHED, 1.25)
    comparison = compare_candidates({"a": probe}, pose_count=3)
    assert comparison.ranked[0].to_dict() == {
        "candidate_id": "a",
        "verdict": probe.verdict,
        "max_error_db": probe.max_error_db,
        "rms_error_db": probe.rms_error_db,
        "ranked_rms_db": probe.rms_error_db,
        "gain_factor": probe.gain_factor,
        "exceedance_octaves": probe.exceedance_octaves,
    }
    assert comparison.to_dict()["pose_count"] == 3
