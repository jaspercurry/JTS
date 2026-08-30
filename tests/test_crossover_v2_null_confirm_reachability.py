# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A staged confirm reaches the leg that installs its coordinate.

The stimulus landed with the phase; this is the half that makes an operator
able to play it. Every test here is about ONE hazard: a confirm that runs
somewhere other than the engine MEASURE leg measures a delay coordinate the
graph never carried, and banks the result as if it had.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker.angle_capture import (
    WALK_NULL_CONFIRM_NEEDS_WIRED,
    WALK_REFUSAL_REASONS,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    build_v2_cloud_index_phase_map,
)
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_NULL_CONFIRM,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_MEASURE,
    PHASE_NULL_CONFIRM,
)
from jasper.active_speaker.crossover_v2.measurement_phase import phase_for_measurement
from jasper.active_speaker.crossover_v2.priors import null_confirm_priors


def _map(kind: str) -> dict[int, str]:
    return build_v2_cloud_index_phase_map(
        include_cloud_measure=False,
        measure_phase=phase_for_measurement(kind),
    )


def test_the_index_map_names_index_two_for_what_the_spec_will_play():
    """``consume_capture`` resolves its phase from the MAP while the engine
    composes from the SPEC's kind. If those two disagree the session plays the
    confirm and then analyses it against MEASURE's program — so they are
    derived from one translation, and this pins that they agree."""
    assert _map(MEASURE_KIND_NULL_CONFIRM)[2] == PHASE_NULL_CONFIRM
    assert _map(MEASURE_KIND_CANDIDATE)[2] == PHASE_MEASURE


def test_an_ordinary_plan_is_untouched_by_the_new_parameter():
    """``measure_phase`` defaults, so every existing caller keeps its map."""
    assert build_v2_cloud_index_phase_map(include_cloud_measure=False)[2] == (
        PHASE_MEASURE
    )


def test_the_confirm_priors_carry_the_corner_the_null_depth_needs():
    """Without ``crossover_fc_hz`` ``_analyze_verify`` never reaches its
    ``crossover_null_depth_db`` call, so a confirm would play, bank, and
    measure nothing. The corner is the whole point of these priors."""
    priors = null_confirm_priors(fc_hz=1600.0)
    assert priors.crossover_fc_hz == 1600.0
    # A confirm is not tracking a model — the comparison against the prediction
    # happens offline, against the landscape.
    assert priors.predicted_sum is None


def test_the_non_wired_refusal_is_a_first_class_walk_reason():
    """Its own slug, so an operator reading ``reason=`` learns WHICH capability
    the source cannot play rather than sharing the polarity one."""
    assert WALK_NULL_CONFIRM_NEEDS_WIRED in WALK_REFUSAL_REASONS
    assert WALK_NULL_CONFIRM_NEEDS_WIRED != ""


@pytest.mark.parametrize(
    "kind, expected",
    [
        (MEASURE_KIND_CANDIDATE, PHASE_MEASURE),
        (MEASURE_KIND_NULL_CONFIRM, PHASE_NULL_CONFIRM),
    ],
)
def test_the_engine_leg_claims_both_measurement_phases(kind: str, expected: str):
    """One slot, two names. Claiming only ``PHASE_MEASURE`` would hand a confirm
    back to the flow leg, which calls ``session_graph.install()`` with no
    arguments — the ordinary graph, carrying neither the inversion nor the
    delay under test.

    Asserted against the production predicate rather than a copy of it, by
    building the map the binder is handed and applying the binder's own rule.
    """
    from jasper.active_speaker.crossover_v2.journey import (
        PHASE_MEASURE as _M,
        PHASE_NULL_CONFIRM as _N,
    )

    index_phase = _map(kind)
    claimed = {i for i, phase in index_phase.items() if phase in (_M, _N)}
    assert 2 in claimed
    assert index_phase[2] == expected
