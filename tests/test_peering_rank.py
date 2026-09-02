# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.peering.rank.

The ranking function is the safety property of the entire arbitration
design: every peer must reach the same conclusion from the same input
set. These tests pin the determinism contract + the tier priorities.
"""
from __future__ import annotations

import pytest

from jasper.peering.rank import (
    CONFIDENCE_TIE_EPS,
    PRIMARY_BIAS,
    WakeReport,
    rank,
)


def _r(
    peer_id: str,
    *,
    score: float = 0.5,
    snr_db: float | None = 15.0,
    rms_dbfs: float | None = -20.0,
    primary: bool = False,
    can_serve: bool = True,
) -> WakeReport:
    """Compact constructor with sensible defaults."""
    return WakeReport(
        peer_id=peer_id,
        score=score,
        snr_db=snr_db,
        rms_dbfs=rms_dbfs,
        primary=primary,
        can_serve=can_serve,
    )


# ---------- determinism ----------


def test_rank_is_deterministic_with_same_input():
    """Same input set → same winner, every time. This is the load-
    bearing safety property: peers don't need to agree explicitly,
    they just need to agree implicitly via this function."""
    reports = [
        _r("alice", score=0.7, primary=False),
        _r("bob",   score=0.85, primary=False),
        _r("carol", score=0.65, primary=True),
    ]
    winners = {rank(reports) for _ in range(50)}
    assert winners == {"bob"}


def test_rank_independent_of_input_order():
    """Critical: peers may receive WAKE messages in different orders
    due to multicast/scheduling jitter, but they must all pick the
    same winner."""
    reports = [
        _r("alice", score=0.7),
        _r("bob",   score=0.85),
        _r("carol", score=0.65),
    ]
    w1 = rank(reports)
    w2 = rank(list(reversed(reports)))
    w3 = rank(reports[1:] + reports[:1])
    assert w1 == w2 == w3 == "bob"


def test_empty_input_raises():
    """A bug-catching guard — empty arbitration shouldn't silently
    pick a default."""
    with pytest.raises(ValueError):
        rank([])


# ---------- tier priorities: can_serve, confidence, SNR, primary bias, ----------
# ---------- final tiebreaker, input clamping ----------


@pytest.mark.parametrize(
    ("reports", "winner"),
    [
        # A high-confidence peer that can't serve loses to a lower-confidence
        # peer that can — otherwise we'd silently route to a dead-end.
        pytest.param(
            [
                _r("alice", score=0.95, can_serve=False),
                _r("bob", score=0.50, can_serve=True),
            ],
            "bob",
            id="can_serve_beats_higher_confidence_that_cant",
        ),
        # If nobody can serve, pick the highest-confidence peer so exactly ONE
        # peer plays the failure cue rather than all of them.
        pytest.param(
            [
                _r("alice", score=0.95, can_serve=False),
                _r("bob", score=0.50, can_serve=False),
            ],
            "alice",
            id="all_cant_serve_picks_best_anyway",
        ),
        pytest.param(
            [_r("alice", score=0.95), _r("bob", score=0.50)],
            "alice",
            id="confidence_breaks_clear_cases",
        ),
        # 0.80 and 0.81 are within CONFIDENCE_TIE_EPS (0.05), so SNR wins.
        pytest.param(
            [
                _r("alice", score=0.80, snr_db=20.0),
                _r("bob", score=0.81, snr_db=10.0),  # ~tied; lower SNR
            ],
            "alice",
            id="snr_breaks_near_ties",
        ),
        # A confidence gap above tie-eps means SNR is ignored entirely.
        pytest.param(
            [
                _r("alice", score=0.50, snr_db=30.0),
                _r("bob", score=0.90, snr_db=5.0),
            ],
            "bob",
            id="confidence_beats_snr_when_gap_is_clear",
        ),
        # Primary bias is small enough not to override real signal, large
        # enough to break a near-tie: bob's effective 0.83+0.05 beats 0.85.
        pytest.param(
            [
                _r("alice", score=0.85, primary=False),
                _r("bob", score=0.83, primary=True),
            ],
            "bob",
            id="primary_wins_near_tie",
        ),
        # Primary bias must NOT make a clearly-worse-positioned peer win, or a
        # user could never grab a non-primary speaker by talking to it directly.
        pytest.param(
            [
                _r("alice", score=0.95, primary=False),
                _r("bob", score=0.60, primary=True),
            ],
            "alice",
            id="primary_doesnt_override_clear_winner",
        ),
        # When every signal is identical, the lowest peer_id wins — the final
        # deterministic tiebreaker that makes the P2P design work without
        # consensus.
        pytest.param(
            [
                _r("zzz", score=0.80),
                _r("aaa", score=0.80),
                _r("mmm", score=0.80),
            ],
            "aaa",
            id="lowest_peer_id_wins_full_tie",
        ),
        # Missing SNR shouldn't crash the sort key or change determinism.
        pytest.param(
            [
                _r("zzz", score=0.80, snr_db=None, rms_dbfs=None),
                _r("aaa", score=0.80, snr_db=None, rms_dbfs=None),
            ],
            "aaa",
            id="lowest_peer_id_wins_with_missing_snr",
        ),
        # A misbehaving peer reporting score=1.5 shouldn't take down
        # arbitration — clamp to 1.0 and still rank.
        pytest.param(
            [_r("alice", score=1.5), _r("bob", score=0.9)],
            "alice",
            id="oob_score_clamped_not_raising",
        ),
        # An out-of-range negative score must not let the bad actor win
        # against a sane positive score; the 0.05 gap puts them outside the
        # same confidence band.
        pytest.param(
            [_r("alice", score=-0.5), _r("bob", score=0.5)],
            "bob",
            id="negative_score_clamped_not_disruptive",
        ),
    ],
)
def test_rank_picks_the_expected_winner(reports, winner):
    assert rank(reports) == winner


def test_primary_bias_constant_value():
    """Document the PRIMARY_BIAS value so a careless change is loud.
    If you're updating this number, update the wizard copy too."""
    assert PRIMARY_BIAS == 0.05


# ---------- tie-eps documentation ----------


def test_confidence_tie_eps_constant_value():
    """Document the eps so a careless change is loud. 0.05 was picked
    to absorb openWakeWord's per-frame jitter on identical audio."""
    assert CONFIDENCE_TIE_EPS == 0.05
