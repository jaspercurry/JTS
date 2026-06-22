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


# ---------- can_serve takes priority ----------


def test_can_serve_beats_higher_confidence_that_cant():
    """A high-confidence peer that can't serve loses to a lower-
    confidence peer that can. Otherwise we'd silently route to a
    dead-end."""
    reports = [
        _r("alice", score=0.95, can_serve=False),
        _r("bob",   score=0.50, can_serve=True),
    ]
    assert rank(reports) == "bob"


def test_all_cant_serve_picks_best_anyway():
    """If nobody can serve, pick the highest-confidence peer so that
    exactly ONE peer plays the failure cue rather than all of them."""
    reports = [
        _r("alice", score=0.95, can_serve=False),
        _r("bob",   score=0.50, can_serve=False),
    ]
    assert rank(reports) == "alice"


# ---------- confidence is primary ----------


def test_confidence_breaks_clear_cases():
    reports = [
        _r("alice", score=0.95),
        _r("bob",   score=0.50),
    ]
    assert rank(reports) == "alice"


# ---------- SNR tiebreaker ----------


def test_snr_breaks_near_ties():
    """Top two confidences within tie-eps — SNR decides."""
    reports = [
        _r("alice", score=0.80, snr_db=20.0),
        _r("bob",   score=0.81, snr_db=10.0),  # ~tied; lower SNR
    ]
    # 0.80 and 0.81 are within CONFIDENCE_TIE_EPS (0.05), so SNR wins.
    assert rank(reports) == "alice"


def test_confidence_beats_snr_when_gap_is_clear():
    """Confidence gap above tie-eps means SNR is ignored entirely."""
    reports = [
        _r("alice", score=0.50, snr_db=30.0),
        _r("bob",   score=0.90, snr_db=5.0),
    ]
    assert rank(reports) == "bob"


# ---------- primary bias ----------


def test_primary_wins_near_tie():
    """Primary bias is small enough not to override real signal,
    large enough to break a near-tie."""
    reports = [
        _r("alice", score=0.85, primary=False),
        _r("bob",   score=0.83, primary=True),
    ]
    # bob's effective score is 0.83 + 0.05 = 0.88, beats alice's 0.85.
    assert rank(reports) == "bob"


def test_primary_doesnt_override_clear_winner():
    """Primary bias must NOT make a clearly-worse-positioned peer win.
    Otherwise the user can never grab a non-primary speaker by talking
    to it directly."""
    reports = [
        _r("alice", score=0.95, primary=False),
        _r("bob",   score=0.60, primary=True),
    ]
    # bob's effective is 0.65; still way below alice.
    assert rank(reports) == "alice"


def test_primary_bias_constant_value():
    """Document the PRIMARY_BIAS value so a careless change is loud.
    If you're updating this number, update the wizard copy too."""
    assert PRIMARY_BIAS == 0.05


# ---------- final tiebreaker ----------


def test_lowest_peer_id_wins_full_tie():
    """When every signal is identical, the lowest peer_id wins. This
    is the final deterministic tiebreaker that makes the whole P2P
    design work without consensus."""
    reports = [
        _r("zzz", score=0.80),
        _r("aaa", score=0.80),
        _r("mmm", score=0.80),
    ]
    assert rank(reports) == "aaa"


def test_lowest_peer_id_wins_with_missing_snr():
    """Missing SNR shouldn't crash the sort key or change determinism."""
    reports = [
        _r("zzz", score=0.80, snr_db=None, rms_dbfs=None),
        _r("aaa", score=0.80, snr_db=None, rms_dbfs=None),
    ]
    assert rank(reports) == "aaa"


# ---------- input clamping ----------


def test_oob_score_clamped_not_raising():
    """A misbehaving peer reporting score=1.5 shouldn't take down
    arbitration. We clamp + still rank, so the fleet stays alive."""
    reports = [
        _r("alice", score=1.5),  # over-range; clamps to 1.0
        _r("bob",   score=0.9),
    ]
    assert rank(reports) == "alice"  # still wins, but at clamped 1.0


def test_negative_score_clamped_not_disruptive():
    """An out-of-range negative score (e.g. from a misbehaving peer)
    must not let the bad actor win against a sane positive score.
    The 0.05 gap to a real detection puts them outside the same
    confidence band, so the legitimate peer takes it."""
    reports = [
        _r("alice", score=-0.5),  # OOB; clamps to 0.0
        _r("bob",   score=0.5),   # real detection, well above any eps
    ]
    assert rank(reports) == "bob"


# ---------- tie-eps documentation ----------


def test_confidence_tie_eps_constant_value():
    """Document the eps so a careless change is loud. 0.05 was picked
    to absorb openWakeWord's per-frame jitter on identical audio."""
    assert CONFIDENCE_TIE_EPS == 0.05


# ---------- helpers ----------


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
