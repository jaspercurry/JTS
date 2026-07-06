# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P6 deterministic simulate-and-reject gate — no paid calls, no hardware.

Pins the safety promise: a proposal that would ring, overflow headroom,
or (in the noise-free simulation) make the room measurably worse is
rejected BEFORE apply, and a good cut is accepted with a P4 verdict.
"""
from __future__ import annotations

import numpy as np

from jasper.calibration_agent import proposal_sim as ps


def _curve(freqs, mags):
    return {"freqs_hz": freqs.tolist(), "magnitude_db": mags.tolist()}


def _room_with_mode(fc=62.0, gain=8.0, width=0.25):
    freqs = np.geomspace(20, 350, 60)
    mags = gain * np.exp(-((np.log2(freqs / fc)) ** 2) / (2 * width ** 2))
    return freqs, mags


def test_good_cut_accepted_with_accept_verdict():
    freqs, mags = _room_with_mode()
    mc = _curve(freqs, mags)
    tc = _curve(freqs, np.zeros_like(freqs))
    r = ps.simulate_correction_proposal(
        [{"freq_hz": 62.0, "q": 3.0, "gain_db": -7.0}],
        measured=mc, baseline=mc, target=tc, max_total_boost_db=0.0,
    )
    assert r.accepted
    assert r.acceptance is not None
    assert r.acceptance["verdict"] == "accept"
    assert r.predicted_curve is not None


def test_ringing_boost_rejected():
    freqs, mags = _room_with_mode()
    mc = _curve(freqs, mags)
    tc = _curve(freqs, np.zeros_like(freqs))
    # +6 dB at Q 6 exceeds the gain-scaled ring ceiling.
    r = ps.simulate_correction_proposal(
        [{"freq_hz": 62.0, "q": 6.0, "gain_db": 6.0}],
        measured=mc, baseline=mc, target=tc, max_total_boost_db=6.0,
    )
    assert not r.accepted
    assert any(i.code == "boost_would_ring" for i in r.issues)


def test_headroom_overflow_rejected():
    freqs, mags = _room_with_mode()
    mc = _curve(freqs, mags)
    tc = _curve(freqs, np.zeros_like(freqs))
    r = ps.simulate_correction_proposal(
        [
            {"freq_hz": 80.0, "q": 1.5, "gain_db": 2.0},
            {"freq_hz": 120.0, "q": 1.5, "gain_db": 2.0},
        ],
        measured=mc, baseline=mc, target=tc, max_total_boost_db=0.0,
    )
    assert not r.accepted
    assert any(i.code == "boost_stack_exceeds_headroom" for i in r.issues)


def test_catastrophic_proposal_rejected_by_acceptance():
    # A multi-cut gouging an already-flat room -> the noise-free sim says
    # revert-class; the gate rejects before apply.
    freqs = np.geomspace(20, 350, 60)
    flat = _curve(freqs, np.zeros_like(freqs))
    r = ps.simulate_correction_proposal(
        [
            {"freq_hz": 50.0, "q": 2.0, "gain_db": -10.0},
            {"freq_hz": 90.0, "q": 2.0, "gain_db": -10.0},
            {"freq_hz": 160.0, "q": 2.0, "gain_db": -10.0},
            {"freq_hz": 280.0, "q": 2.0, "gain_db": -10.0},
        ],
        measured=flat, baseline=flat, target=flat, max_total_boost_db=0.0,
    )
    assert not r.accepted
    assert any(i.code == "simulation_regresses_room" for i in r.issues)


def test_empty_proposal_rejected():
    r = ps.simulate_correction_proposal(
        [], measured=None, baseline=None, target=None,
    )
    assert not r.accepted
    assert any(i.code == "empty_proposal" for i in r.issues)


def test_missing_measured_curve_rejected():
    r = ps.simulate_correction_proposal(
        [{"freq_hz": 62.0, "q": 3.0, "gain_db": -7.0}],
        measured=None, baseline=None, target=None,
    )
    assert not r.accepted
    assert any(i.code == "missing_measured_curve" for i in r.issues)


def test_ring_ceiling_tightens_with_gain():
    # A larger boost must have a lower Q ceiling.
    assert ps.ring_guard_q_ceiling(0.0) > ps.ring_guard_q_ceiling(6.0)
    assert ps.ring_guard_q_ceiling(6.0) >= ps.RING_GUARD_MIN_Q


def test_simulation_without_baseline_still_returns_predicted():
    """Pins the lenient-PREVIEW vs strict-APPLY split.

    The SIM stays lenient without baseline/target: no acceptance verdict,
    but ring + headroom checks still run and (if clean) the proposal is
    ``accepted`` with a predicted curve — an honest ring+headroom-only
    preview for /propose. The APPLY seam is the strict half:
    _handle_propose_apply requires ``sim.acceptance is not None`` (the P4
    judge actually ran) and rejects with ``missing_acceptance_basis``
    otherwise — pinned by tests/test_web_correction_tuning.py::
    test_propose_apply_fails_closed_without_acceptance_basis.
    """
    freqs, mags = _room_with_mode()
    mc = _curve(freqs, mags)
    r = ps.simulate_correction_proposal(
        [{"freq_hz": 62.0, "q": 3.0, "gain_db": -7.0}],
        measured=mc, baseline=None, target=None, max_total_boost_db=0.0,
    )
    assert r.accepted
    assert r.acceptance is None
    assert r.predicted_curve is not None
