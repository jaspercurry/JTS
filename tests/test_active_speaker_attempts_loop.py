# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Percentile and floor arithmetic pinned against the frozen repeat study."""

from __future__ import annotations


import pytest

from jasper.active_speaker.attempts_loop import (
    CLAIM_FLOOR_P95_MULTIPLE, FLOOR_BASIS_MEASURED, FLOOR_BASIS_POLICY,
    FLOOR_SCOPE_ACROSS_SITTINGS, FLOOR_SCOPE_WITHIN_SITTING, FLOOR_SCOPES, FloorStats, percentile,
)

METRIC = "max_db_notch_excluded"

BANKED_CONSECUTIVE_PAIRS_DB = (
    0.085368, 0.053913, 0.067807, 0.043458, 0.049502, 0.084889, 0.059320,
    0.039868, 0.070366, 0.047792, 0.043053, 0.035829, 0.051826,
)
BANKED_P95_DB = 0.08508
BANKED_MEDIAN_DB = 0.05183


SITTING = "sitting-1"


def test_percentile_reproduces_the_banked_studys_own_summary():
    """The claim floor's inputs must be derivable, not transcribed.

    The banked analysis published median 0.05183 and p95 0.08508 for these
    thirteen pairs; the floor it derives is twice that p95. A percentile that
    disagreed would silently move every floor this module computes.
    """
    assert percentile(BANKED_CONSECUTIVE_PAIRS_DB, 50.0) == pytest.approx(
        BANKED_MEDIAN_DB, abs=5e-6,
    )
    assert percentile(BANKED_CONSECUTIVE_PAIRS_DB, 95.0) == pytest.approx(
        BANKED_P95_DB, abs=5e-6,
    )
    assert CLAIM_FLOOR_P95_MULTIPLE * percentile(
        BANKED_CONSECUTIVE_PAIRS_DB, 95.0
    ) == pytest.approx(0.17016, abs=1e-5)


def test_percentile_edges():
    assert percentile([4.0], 95.0) == 4.0
    assert percentile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0], 100.0) == 3.0
    assert percentile([1.0, 2.0, 3.0], 50.0) == 2.0
    with pytest.raises(ValueError):
        percentile([], 50.0)


def test_claim_floor_is_twice_the_measured_p95_not_a_transcribed_decimal():
    floor = FloorStats.from_repeat_study(
        metric=METRIC,
        median_db=BANKED_MEDIAN_DB,
        p95_db=BANKED_P95_DB,
        source="captures/repeat-floor-20260731",
        measured_at="2026-07-31",
    )
    assert floor.claim_floor_db == pytest.approx(
        CLAIM_FLOOR_P95_MULTIPLE * BANKED_P95_DB,
    )
    assert floor.claim_floor_db == pytest.approx(0.17016, abs=1e-9)
    assert floor.claim_floor_db < 0.2
    assert floor.basis == FLOOR_BASIS_MEASURED


def test_a_larger_measured_p95_moves_the_floor_with_it():
    """Mutation check: the floor tracks the measurement, not a constant."""

    tight = FloorStats.from_repeat_study(
        metric=METRIC, median_db=0.05, p95_db=0.085,
        source="s", measured_at="2026-07-31",
    )
    loose = FloorStats.from_repeat_study(
        metric=METRIC, median_db=0.10, p95_db=0.170,
        source="s", measured_at="2026-07-31",
    )
    assert loose.claim_floor_db == pytest.approx(2 * tight.claim_floor_db)


def test_policy_bar_floor_refuses_to_invent_a_measurement():
    floor = FloorStats.from_policy_bar(
        metric="anything", claim_floor_db=0.5, source="a shipped constant",
        scope=FLOOR_SCOPE_ACROSS_SITTINGS,
    )
    assert floor.basis == FLOOR_BASIS_POLICY
    assert floor.p95_db is None
    assert floor.median_db is None


def test_floor_construction_refuses_nonsense():
    with pytest.raises(ValueError):
        FloorStats.from_repeat_study(
            metric=METRIC, median_db=0.0, p95_db=0.0, source="s", measured_at="",
        )
    with pytest.raises(ValueError):
        FloorStats.from_policy_bar(
            metric="", claim_floor_db=0.5, source="s",
            scope=FLOOR_SCOPE_WITHIN_SITTING,
        )
    with pytest.raises(ValueError):
        FloorStats.from_policy_bar(
            metric="m", claim_floor_db=0.5, source="",
            scope=FLOOR_SCOPE_WITHIN_SITTING,
        )
    with pytest.raises(ValueError):
        FloorStats(
            metric="m", claim_floor_db=0.5, basis="made_up", source="s",
        )
    with pytest.raises(ValueError):
        FloorStats(
            metric="m", claim_floor_db=0.5, basis=FLOOR_BASIS_POLICY,
            source="s", scope="whenever",
        )


def test_every_floor_scope_is_declared_and_the_default_is_the_narrow_one():
    assert FLOOR_SCOPES == {FLOOR_SCOPE_WITHIN_SITTING, FLOOR_SCOPE_ACROSS_SITTINGS}
    bare = FloorStats(
        metric=METRIC, claim_floor_db=0.5, basis=FLOOR_BASIS_POLICY, source="s",
    )
    assert bare.scope == FLOOR_SCOPE_WITHIN_SITTING
    assert bare.to_dict()["scope"] == FLOOR_SCOPE_WITHIN_SITTING
    study = FloorStats.from_repeat_study(
        metric=METRIC, median_db=0.05, p95_db=0.085, source="s",
        measured_at="2026-07-31",
    )
    assert study.scope == FLOOR_SCOPE_WITHIN_SITTING
    assert FloorStats.from_repeat_study(
        metric=METRIC, median_db=0.05, p95_db=0.085, source="s",
        measured_at="2026-07-31", scope=FLOOR_SCOPE_ACROSS_SITTINGS,
    ).scope == FLOOR_SCOPE_ACROSS_SITTINGS


def test_a_policy_bar_must_say_what_it_licenses():
    """No default, because there is no fact behind a declared bar to infer one
    from — see ``from_policy_bar``'s own note."""

    with pytest.raises(TypeError):
        FloorStats.from_policy_bar(  # type: ignore[call-arg]
            metric=METRIC, claim_floor_db=0.5, source="s",
        )
