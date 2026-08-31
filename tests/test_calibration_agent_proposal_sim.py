# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P6 deterministic proposal simulation — no paid calls, no hardware.

Pins the disclosure promise: the simulation computes what a proposed
filter set is predicted to do (curve, improvement, ring Q, boost total)
and REFUSES NOTHING. The strategy caps, the user's confirm, and the
emitter's re-clip own the real constraints.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.calibration_agent import proposal_sim as ps


def _curve(freqs, mags):
    return {"freqs_hz": freqs.tolist(), "magnitude_db": mags.tolist()}


def _room_with_mode(fc=62.0, gain=8.0, width=0.25):
    freqs = np.geomspace(20, 350, 60)
    mags = gain * np.exp(-((np.log2(freqs / fc)) ** 2) / (2 * width ** 2))
    return freqs, mags


def _flat():
    freqs = np.geomspace(20, 350, 60)
    return freqs, np.zeros_like(freqs)


def test_good_cut_discloses_predicted_improvement():
    freqs, mags = _room_with_mode()
    mc = _curve(freqs, mags)
    tc = _curve(freqs, np.zeros_like(freqs))
    r = ps.simulate_correction_proposal(
        [{"freq_hz": 62.0, "q": 3.0, "gain_db": -7.0}],
        measured=mc, baseline=mc, target=tc, max_total_boost_db=0.0,
    )
    assert r.issues == ()
    assert r.predicted_curve is not None
    assert r.predicted_rms_delta_db is not None
    assert r.predicted_rms_delta_db > 0  # positive = closer to target


@pytest.mark.parametrize(
    ("peqs", "max_total_boost_db", "code"),
    [
        # A narrow high-gain boost: disclosed as ring-prone, not refused.
        ([{"freq_hz": 62.0, "q": 6.0, "gain_db": 6.0}], 6.0, "boost_would_ring"),
        # Stacked boost over the headroom ceiling: disclosed, not refused.
        # Q 1.0 stays under the +2 dB ring ceiling, so headroom is the
        # only note these two raise.
        (
            [
                {"freq_hz": 80.0, "q": 1.0, "gain_db": 2.0},
                {"freq_hz": 120.0, "q": 1.0, "gain_db": 2.0},
            ],
            0.0,
            "boost_stack_exceeds_headroom",
        ),
    ],
)
def test_flagged_sets_are_disclosed_not_refused(peqs, max_total_boost_db, code):
    freqs, mags = _room_with_mode()
    mc = _curve(freqs, mags)
    tc = _curve(freqs, np.zeros_like(freqs))
    r = ps.simulate_correction_proposal(
        peqs, measured=mc, baseline=mc, target=tc,
        max_total_boost_db=max_total_boost_db,
    )
    assert [i.code for i in r.issues] == [code]
    # The full disclosure is still computed for a flagged set.
    assert r.predicted_curve is not None
    assert r.predicted_rms_delta_db is not None
    assert r.max_total_boost_db == max_total_boost_db


def test_regressing_set_discloses_a_negative_delta_without_an_issue():
    """The demoted veto. A multi-cut gouging an already-flat room used to
    be refused by an acceptance verdict on the predicted curve; now the
    sim reports the predicted regression as a number and raises nothing.

    **Mutation guard.** Restoring the verdict veto puts a code back in
    ``issues`` and fails the emptiness assertion."""
    freqs, mags = _flat()
    flat = _curve(freqs, mags)
    r = ps.simulate_correction_proposal(
        [
            {"freq_hz": 50.0, "q": 2.0, "gain_db": -10.0},
            {"freq_hz": 90.0, "q": 2.0, "gain_db": -10.0},
            {"freq_hz": 160.0, "q": 2.0, "gain_db": -10.0},
            {"freq_hz": 280.0, "q": 2.0, "gain_db": -10.0},
        ],
        measured=flat, baseline=flat, target=flat, max_total_boost_db=0.0,
    )
    assert r.issues == ()
    assert r.predicted_rms_delta_db is not None
    assert r.predicted_rms_delta_db < 0  # negative = further from target


@pytest.mark.parametrize(
    ("peqs", "code"),
    [
        ([], "empty_proposal"),
        ([{"freq_hz": 62.0, "q": 3.0, "gain_db": -7.0}], "missing_measured_curve"),
    ],
)
def test_unsimulatable_input_leaves_the_predictions_empty(peqs, code):
    r = ps.simulate_correction_proposal(
        peqs, measured=None, baseline=None, target=None,
    )
    assert [i.code for i in r.issues] == [code]
    assert r.predicted_curve is None
    assert r.predicted_rms_delta_db is None


def test_ring_ceiling_tightens_with_gain():
    # A larger boost must have a lower Q ceiling.
    assert ps.ring_guard_q_ceiling(0.0) > ps.ring_guard_q_ceiling(6.0)
    assert ps.ring_guard_q_ceiling(6.0) >= ps.RING_GUARD_MIN_Q


def test_predicted_curve_survives_a_missing_baseline():
    """Without baseline/target there is nothing to measure improvement
    against, but the predicted curve is still simulated and disclosed."""
    freqs, mags = _room_with_mode()
    mc = _curve(freqs, mags)
    r = ps.simulate_correction_proposal(
        [{"freq_hz": 62.0, "q": 3.0, "gain_db": -7.0}],
        measured=mc, baseline=None, target=None, max_total_boost_db=0.0,
    )
    assert r.issues == ()
    assert r.predicted_curve is not None
    assert r.predicted_rms_delta_db is None


def test_payload_carries_no_go_no_go_flag():
    """The wire shape is disclosure-only: no ``accepted`` field for a
    client (or a future endpoint) to read as a veto.

    **Mutation guard.** Re-adding an ``accepted``/``acceptance`` key to
    ``SimResult.to_dict`` fails this."""
    freqs, mags = _room_with_mode()
    mc = _curve(freqs, mags)
    tc = _curve(freqs, np.zeros_like(freqs))
    payload = ps.simulate_correction_proposal(
        [{"freq_hz": 62.0, "q": 3.0, "gain_db": -7.0}],
        measured=mc, baseline=mc, target=tc,
    ).to_dict()
    assert set(payload) == {
        "issues",
        "total_boost_db",
        "max_total_boost_db",
        "predicted_curve",
        "predicted_rms_delta_db",
    }
