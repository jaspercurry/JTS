# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synthetic spatial scenarios for the boost-permission decision, graded
against injected ground truth.

**What these are for.** Corpus replay is a regression instrument: it shows
the code still computes
what it computed, never that the prescription was right. Hardware proves the
chain but cannot produce a defect on demand. These are the third rung — a
response built with the defect injected deliberately, so the correct answer is
known BEFORE the flow runs, and the gate's decision can be graded rather than
merely observed.

**Where they assert.** At the conductor's decision layer — what
:meth:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session._boost_excluded_bands_hz`
offers, what :func:`~jasper.active_speaker.crossover_v2_flow.assemble_cloud_group_result`
merges, what :func:`~jasper.active_speaker.linearization_fit._boost_exclusion_verdicts`
does with the result. #2075's own tests already pin the unit mechanics of the
drop (``tests/test_active_speaker_linearization_fit.py``) and of the
presence classification (``tests/test_interference_nulls.py``'s ``test_f_*``
family); these do not re-cover them.

**The generators are the module's own, and neither is the fit's model run
backwards.** Both come from ``tests.test_interference_nulls``, and which one a
scenario uses IS the injected truth:

* ``_cloud`` builds an IR per position — one direct arrival plus one delayed
  copy at a known ``tau`` and ``r`` — and takes the magnitude curve from that
  same IR. A cloud built this way carries a real reflection, so its dips are
  interference and the arrival evidence to prove it is present.
* ``_notched_cloud`` writes a notch into the magnitude domain with ``ir=None``.
  A cloud built this way carries NO reflection anywhere, so its dips are a
  property of the thing being measured and there is no arrival to attribute.

That is the whole difference between the two halves of the pair below, and it
is a difference in the fixture rather than in the assertion. No assertion here
compares two outputs of the same fit to each other: the recovered ``tau``/``r``
are checked against the values injected, which is the ladder's second clause.

**What the injected truth is, per scenario.** Stated in each docstring,
because "the flow decided X" is only a finding next to "the right answer was
Y". Two of the scenarios below deliberately reach the SAME decision from
OPPOSITE truths — that is the point of the pair, and it is the residual #1967
names rather than a bug this file is reporting.
"""

from __future__ import annotations


import numpy as np
import pytest

from jasper.active_speaker.branch_target import SIGNIFICANT_GAIN_DB
from jasper.active_speaker.crossover_v2.verification import (
    ECHO_BAND_HF_REGIME_FLOOR_HZ,
)
from jasper.active_speaker.crossover_v2_flow import assemble_cloud_group_result
from jasper.active_speaker.linearization_fit import (
    LinearizationFilter,
    _boost_exclusion_verdicts,
)
from jasper.audio_measurement.interference_nulls import (
    CLASSIFICATION_POSITION_DEPENDENT,
    CLASSIFICATION_POSITION_INVARIANT,
    REASON_NO_CORROBORATING_ARRIVALS,
    classify_dip_position_variance,
    identify_interference_nulls,
)
from jasper.audio_measurement.spatial_combine import combine_positions

# The conductor's own test helpers, and the SAME two cloud builders
# `test_interference_nulls` grades the null registry with. Imported rather than
# copied so the suite has one construction of each, not two that agree by
# inspection — and so a change to either builder reaches these scenarios.
from tests.crossover_v2_fixtures import FakeSeams, _cloud_conductor
from tests.test_interference_nulls import _cloud, _notched_cloud, _tau_us

# 15 samples at 48 kHz. Chosen because its comb (odd multiples of 1/2tau)
# straddles the registry's gating floor: rungs at 1600 Hz — BELOW the floor,
# where nothing gates — and at 4800/8000/11200/14400/17600 Hz above it. One
# physical reflection, two regimes, which is what lets the pair of scenarios
# below differ in truth while agreeing in decision.
SOURCE_FIXED_DELAY_SAMPLES = 15
SOURCE_FIXED_TAU_US = _tau_us(SOURCE_FIXED_DELAY_SAMPLES)
SOURCE_FIXED_R = 0.37
N_POSITIONS = 8

# The registry's own analysis band for these fixtures. Its lower edge IS the
# gating floor rather than a literal that happens to equal it today, so
# "above the floor, where attribution is possible" stays true if the constant
# ever moves.
HF_BAND_HZ = (ECHO_BAND_HF_REGIME_FLOOR_HZ, 19000.0)
# The span a conductor examines below the floor: a cloud's gated validity
# floor up to the registry's lower edge.
BLIND_SPAN_HZ = (1200.0, ECHO_BAND_HF_REGIME_FLOOR_HZ)

# Recovery tolerances are the module's own documented biases (the same ones
# `test_a_constructed_two_path_cloud_is_identified_with_its_own_numbers` uses):
# tau to 1 % because the ladder fits minima on a finite grid, r to 0.05
# because the depth statistic is a lower bound on the true depth.
TAU_TOLERANCE_FRACTION = 0.01
R_TOLERANCE = 0.05


def _source_fixed_cloud():
    """A cloud whose positions all share ONE reflection: the comb sits at the
    same frequencies everywhere, which is what "source-fixed" means."""
    return combine_positions(
        _cloud([SOURCE_FIXED_DELAY_SAMPLES] * N_POSITIONS, SOURCE_FIXED_R)
    )


def _comb_rung_hz(n: int) -> float:
    """The n-th cancellation of a two-path comb: odd multiples of 1/(2*tau)."""
    return (2 * n + 1) / (2 * SOURCE_FIXED_TAU_US * 1e-6)


# --------------------------------------------------------------------------- #
# The wiring contract (issue #1742 item 4): one assembly, three instruments
# --------------------------------------------------------------------------- #


def test_the_screen_alone_would_have_excluded_nothing_the_comb_put_there():
    """INJECTED TRUTH: one source-fixed reflection, ``tau`` and ``r`` known.

    ``assemble_cloud_group_result`` is the single function that consumes the
    power-vs-median screen, ``geometry.locked``, and the null registry
    TOGETHER; its docstring says a screen-only read is "the mask alone is a
    hole" because the screen structurally cannot see a position-invariant
    null. This is that claim as an experiment rather than a citation: on a
    cloud built from a real comb, the screen offers NOTHING and the registry
    offers every rung, so the merged mask is entirely the registry's
    contribution.

    A consumer reading ``combined.excluded`` alone would therefore correct
    straight through an interference comb that is demonstrably there — the
    exact failure the single-assembly invariant exists to prevent.
    """
    result = assemble_cloud_group_result(
        _source_fixed_cloud(), echo_band_hz=HF_BAND_HZ, validity_floor_hz=200.0,
    )

    assert result["available"] is True
    screen = [tuple(b) for b in result["screen_excluded_bands_hz"]]
    registry = [tuple(b) for b in result["null_registry"]["excluded_bands_hz"]]
    merged = [tuple(b) for b in result["merged_excluded_bands_hz"]]

    # The screen saw nothing. Not "less" — nothing.
    assert screen == [], screen
    # The registry saw the comb, and the merge is the union, so on this cloud
    # the merged mask IS the registry's.
    assert registry, "the registry must identify the injected comb"
    assert merged == registry
    # All three instruments were consulted in ONE call: the geometry verdict
    # rides the same result, so no caller has to assemble a second opinion.
    assert result["geometry"]["locked"] is True
    assert result["geometry"]["n_positions"] == N_POSITIONS


def test_the_registry_recovers_the_reflection_that_was_injected():
    """The generator-independence clause, executable: the numbers asserted are
    the ones PUT IN, not another output of the same analysis.

    INJECTED TRUTH: ``tau`` = one sample-aligned delay, ``r`` = 0.37.
    """
    report = identify_interference_nulls(_source_fixed_cloud(), band_hz=HF_BAND_HZ)

    assert report.classification == CLASSIFICATION_POSITION_INVARIANT
    assert report.reason == ""
    assert report.tau_ladder_us == pytest.approx(
        SOURCE_FIXED_TAU_US, rel=TAU_TOLERANCE_FRACTION
    )
    assert report.r_freq == pytest.approx(SOURCE_FIXED_R, abs=R_TOLERANCE)
    # Every identified null brackets a rung of the comb that was injected —
    # the registry located the physical feature, not an arbitrary dip.
    rungs = [_comb_rung_hz(n) for n in range(1, 6)]
    for null in report.nulls:
        assert any(null.f_lo_hz <= rung <= null.f_hi_hz for rung in rungs), (
            null.f_center_hz, rungs,
        )


# --------------------------------------------------------------------------- #
# Attributed comb, above the floor: excluded, and the per-filter criterion
# refuses the aimed boost while keeping a neighbour that only spills
# --------------------------------------------------------------------------- #


def test_an_attributed_comb_excludes_and_the_aimed_boost_is_the_one_dropped():
    """INJECTED TRUTH: a source-fixed comb above the gating floor. Boosting a
    rung of it corrects nothing any listener hears; boosting a genuine dip
    beside one is legitimate.

    CORRECT DECISION: exclude the rungs, drop a boost AIMED at one, keep a
    neighbour whose action region sits outside it — which is exactly what the
    per-filter half-gain criterion is for. A whole-cascade veto would have
    taken both.

    The neighbour is placed deliberately, not arbitrarily: half an octave
    below the band's lower edge, where it delivers MORE than
    :data:`~jasper.active_speaker.branch_target.SIGNIFICANT_GAIN_DB` into the
    band but far less than half its own peak. That is the gap between the two
    rules, so this scenario fails if the criterion is ever swapped for the
    absolute threshold the function's docstring records as measured-and-
    rejected. Placed an octave away instead it spills 0.07 dB, clears neither
    rule, and the test would pass under both — which is what a first draft did.
    """
    report = identify_interference_nulls(_source_fixed_cloud(), band_hz=HF_BAND_HZ)
    excluded = [(n.f_lo_hz, n.f_hi_hz) for n in report.nulls]
    assert excluded, "attribution must have produced bands to act on"

    lo_hz, hi_hz = excluded[0]
    centre = float(np.sqrt(lo_hz * hi_hz))
    grid = np.geomspace(20.0, 24_000.0, 4096)
    # Aimed: a moderate-Q bell sitting ON a rung.
    aimed = LinearizationFilter(
        freq=centre, gain=6.0, q=2.0, biquad_type="Peaking",
    )
    neighbour = LinearizationFilter(
        freq=lo_hz * 2.0 ** -0.5, gain=6.0, q=4.0, biquad_type="Peaking",
    )

    kept, dropped, residual = _boost_exclusion_verdicts(
        [aimed, neighbour], grid, excluded,
    )

    assert [f.freq for f in kept] == [neighbour.freq]
    assert [d.freq_hz for d in dropped] == [aimed.freq]
    assert dropped[0].band_hz == pytest.approx((lo_hz, hi_hz))
    # The drop is relative to the filter's OWN peak, not an absolute dB floor.
    assert dropped[0].realized_in_band_db >= aimed.gain / 2.0
    # The neighbour's spill into the band survives and is DISCLOSED rather
    # than refused — the post-apply sweep is the cheaper honest answer. It sits
    # in the gap: an absolute-threshold criterion would have taken it.
    spill = {r.band_hz: r.realized_max_db for r in residual}
    assert SIGNIFICANT_GAIN_DB < spill[(lo_hz, hi_hz)] < neighbour.gain / 2.0


# --------------------------------------------------------------------------- #
# The pair: one decision, two truths. Below the floor the gate cannot tell
# them apart, and that is issue #1967's residual with ground truth attached.
# --------------------------------------------------------------------------- #


def test_a_driver_property_dip_keeps_its_boost_and_that_is_correct():
    """INJECTED TRUTH: a dip that is a property of the driver — one notch, at
    the same frequency at every position, and NO reflection anywhere in the
    fixture (``ir=None``, so there is no arrival to attribute).

    CORRECT DECISION: offer nothing for exclusion, so the boost stands. The
    registry refuses attribution BY NAME rather than guessing, and the
    cross-position check finds no contradiction, so the fit keeps a correction
    that a listener would actually hear.
    """
    combined = combine_positions(_notched_cloud([1800.0] * N_POSITIONS))

    report = identify_interference_nulls(combined, band_hz=HF_BAND_HZ)
    assert report.reason == REASON_NO_CORROBORATING_ARRIVALS
    assert report.nulls == ()

    conductor = _cloud_conductor(FakeSeams())
    offered = conductor._boost_excluded_bands_hz(
        combined,
        {"validity_floor_hz": BLIND_SPAN_HZ[0], "null_registry": {
            "classification": report.classification, "reason": report.reason,
        }},
    )

    assert offered == ()


def test_the_same_decision_is_reached_for_an_injected_comb_and_that_is_the_residual():
    """INJECTED TRUTH: the opposite of the test above — a real source-fixed
    interference comb, ``tau`` and ``r`` known, with a rung sitting BELOW the
    registry's gating floor. Boosting that rung corrects nothing.

    CURRENT DECISION: identical to the driver-property case — nothing offered,
    boost permitted. This documents residual #1967 and is deliberately NOT an
    xfail: it asserts what the merged code does today, so that when the
    follow-up lands (the post-apply "did this help" arm, #1868) this test
    flips to asserting the new behaviour and the change is visible in a diff.

    What makes it sharp rather than merely unfortunate: the evidence to tell
    the two cases apart is IN THIS SAME CLOUD. The identical reflection is
    attributed above the floor — recovered ``tau`` within 1 % of the injected
    value — so the flow has, at the moment it decides, a corroborated arrival
    that predicts a rung exactly where the sub-floor dip sits. The gate is not
    short of evidence; it is short of a path from that evidence to this
    decision, because ``classify_dip_position_variance`` is ladder-free by
    construction and ``position_invariant`` is not a licence either way.
    """
    combined = _source_fixed_cloud()
    sub_floor_rung_hz = _comb_rung_hz(0)
    assert BLIND_SPAN_HZ[0] < sub_floor_rung_hz < ECHO_BAND_HF_REGIME_FLOOR_HZ

    # The same reflection IS attributed above the floor.
    hf = identify_interference_nulls(combined, band_hz=HF_BAND_HZ)
    assert hf.classification == CLASSIFICATION_POSITION_INVARIANT
    assert hf.tau_ladder_us == pytest.approx(
        SOURCE_FIXED_TAU_US, rel=TAU_TOLERANCE_FRACTION
    )

    # Below it, the ladder-free check sees the rung and reads it invariant —
    # true, and not a reason to withhold, which is the module's own rule.
    variance = classify_dip_position_variance(combined, band_hz=BLIND_SPAN_HZ)
    assert variance.reason == ""
    assert [d.classification for d in variance.dips] == [
        CLASSIFICATION_POSITION_INVARIANT
    ]
    assert variance.dips[0].f_center_hz == pytest.approx(sub_floor_rung_hz, rel=0.02)
    assert variance.dips[0].positions_present == N_POSITIONS

    conductor = _cloud_conductor(FakeSeams())
    offered = conductor._boost_excluded_bands_hz(
        combined,
        {"validity_floor_hz": BLIND_SPAN_HZ[0], "null_registry": {
            "classification": hf.classification, "reason": hf.reason,
        }},
    )

    # Same answer as the driver-property cloud, from the opposite truth.
    assert offered == ()


def test_the_bound_acts_when_the_positions_actually_contradict_each_other():
    """The control that keeps the pair above from being vacuous.

    INJECTED TRUTH: no single feature — five positions notch at one frequency
    and three at another, so the combined curve's dips are not a property of
    what the speaker radiates.

    CORRECT DECISION: offer both, below the floor, so the fit's lift stage can
    refuse a boost aimed at either. Without this the two ``offered == ()``
    assertions above would hold for a bound that never fires at all.
    """
    combined = combine_positions(
        _notched_cloud([1800.0] * 5 + [2400.0] * 3)
    )

    variance = classify_dip_position_variance(combined, band_hz=BLIND_SPAN_HZ)
    assert [d.classification for d in variance.dips] == [
        CLASSIFICATION_POSITION_DEPENDENT, CLASSIFICATION_POSITION_DEPENDENT,
    ]

    conductor = _cloud_conductor(FakeSeams())
    offered = conductor._boost_excluded_bands_hz(
        combined,
        {"validity_floor_hz": BLIND_SPAN_HZ[0], "null_registry": {}},
    )

    assert len(offered) == 2
    # Everything offered sits in the span the registry could not adjudicate.
    assert all(
        BLIND_SPAN_HZ[0] <= lo < hi <= ECHO_BAND_HF_REGIME_FLOOR_HZ
        for lo, hi in offered
    ), offered


# --------------------------------------------------------------------------- #
# The pair-3 boundary case, from the real session, as a criterion question
# --------------------------------------------------------------------------- #

# The 2026-07-30 JTS3 session's own tweeter boost and the two registered nulls
# that bracket it, read from the persisted candidate
# (`cap_J2OLTNvzmApF0cEAgxrIZw`). Literals here because the case IS these
# numbers: the filter sits ~0.1 octave from both neighbours, which is the
# closest a real session has come to the criterion's boundary.
PAIR3_BOOST = dict(freq=13148.989835978216, gain=5.82161058125954,
                   q=7.248090600565387)
PAIR3_BRACKETING_NULLS = [
    (10727.760314941406, 12189.674377441406),
    (14039.772033691406, 15388.893127441406),
]


@pytest.mark.parametrize(
    ("q", "expect_dropped"),
    [(PAIR3_BOOST["q"], False), (1.0, True)],
    ids=["session_q_7.25_kept", "counterfactual_q_1_dropped"],
)
def test_the_pair_3_boost_is_decided_by_its_own_reach_not_its_centre(q, expect_dropped):
    """A real filter that sits between two registered nulls, and the reason it
    survives.

    At the Q the session actually chose the bell's half-gain region spans
    about a tenth of an octave, which is narrower than the gap to either
    neighbour, so its action region overlaps neither and it is kept. At Q=1 —
    the same centre and the same gain — the region is ~0.8 octave and swallows
    the upper null, and it goes. Centre frequency and gain are identical in
    both rows: the criterion is reading REACH, which is the property it
    claims to read.
    """
    grid = np.geomspace(20.0, 24_000.0, 4096)
    boost = LinearizationFilter(
        freq=PAIR3_BOOST["freq"], gain=PAIR3_BOOST["gain"], q=q,
        biquad_type="Peaking",
    )

    kept, dropped, residual = _boost_exclusion_verdicts(
        [boost], grid, PAIR3_BRACKETING_NULLS,
    )

    assert bool(dropped) is expect_dropped
    assert bool(kept) is not expect_dropped
    if not expect_dropped:
        # Kept, but the spill is still measured and disclosed for both bands.
        assert {r.band_hz for r in residual} == {
            tuple(b) for b in PAIR3_BRACKETING_NULLS
        }
        assert all(
            0.0 < r.realized_max_db < PAIR3_BOOST["gain"] / 2.0 for r in residual
        )
