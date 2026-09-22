# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The planned trim decision survives the candidate's state conversion."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2.candidates import LinearizationState
from jasper.active_speaker.crossover_v2.contracts import TrimStrategy
from jasper.active_speaker.crossover_v2.plan_assembly import LinearizationPlan

FC_HZ = 1648.7


@dataclasses.dataclass(frozen=True)
class _FakeMatch:
    """``decide_trim``'s graded pair, reduced to the field the policy reads."""

    difference_db: float
    matched: bool = True
    level_w_db: float = 0.0
    level_t_db: float = 0.0
    tolerance_db: float = 3.0
    woofer_band_hz: tuple[float, float] = (100.0, 1000.0)
    tweeter_band_hz: tuple[float, float] = (2000.0, 16000.0)


def _decision(*, drift_db: float, anchor_error: float, scan_error: float):
    anchored = {"woofer": -1.0, "tweeter": -6.0}
    return iv.decide_trim(
        anchored_db=anchored,
        resolved_db={"woofer": -1.0, "tweeter": -6.0 - drift_db},
        tweeter_role="tweeter",
        anchored_match=_FakeMatch(anchor_error),
        resolved_match=_FakeMatch(scan_error),
        ripple_db=0.4,
    )


def _plan(decision) -> LinearizationPlan:
    """One plan around one real decision.

    Every field :meth:`LinearizationState.from_plan` does not read is left at a
    placeholder; the round-trip under test is the trim decision's.
    """
    return LinearizationPlan(
        fc_hz=FC_HZ,
        role_attenuations_db=dict(decision.committed_db),
        linearization={},
        trim=decision,
        core_level_evidence={},
        trim_band_estimate_db={},
        polish_delta_db={},
        level_consistency=None,
        linearized_predicted_sum=(np.array([FC_HZ]), np.array([0.0])),
        summation_frame=None,
        radiating_band_hz={},
    )


@pytest.mark.parametrize(
    ("anchor_error", "scan_error", "expected"),
    [
        (5.0, 0.1, TrimStrategy.RESOLVED_COMMITTED),
        (0.1, 5.0, TrimStrategy.ANCHORED_COMMITTED),
    ],
)
def test_the_committed_pair_and_its_drift_survive_the_commit_seam(
    anchor_error, scan_error, expected,
):
    decision = _decision(
        drift_db=1.0, anchor_error=anchor_error, scan_error=scan_error
    )
    assert decision.strategy is expected
    assert decision.outcome == "fitted"

    state = LinearizationState.from_plan(_plan(decision))
    assert state.trim_strategy is expected
    assert state.anchor_drift_db == pytest.approx(decision.anchor_drift_db)
