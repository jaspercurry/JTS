# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The emitted branch chain: radiating band (#1809) and headroom charge (#1808).

The keystone here is :data:`JTS3_20260728_WOOFER` — the linearization filters
the 2026-07-28 JTS3 session actually applied, read out of the emitted
CamillaDSP config on the speaker. Both defects this module closes are
reproduced against those real numbers rather than against a shape invented to
make the fix look good.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import (
    CHAIN_GRID_HZ,
    CROSSOVER_EDGE_ATTENUATION_DB,
    HEADROOM_MARGIN_DB,
    CrossoverSection,
    branch_chain_peak_db,
    branch_headroom_db,
    chain_response,
    crossover_response_db,
    headroom_charge_db,
    radiating_band_hz,
)
from jasper.active_speaker.camilla_yaml import BASELINE_LIMITER_CLIP_LIMIT_DB

# --------------------------------------------------------------------------- #
# the 2026-07-28 JTS3 profile, verbatim
# --------------------------------------------------------------------------- #
#
# Read from /var/lib/camilladsp/configs/
# active_speaker_baseline_candidate_6a832c3ae8b2.yml on jts3.local (the graph
# the speaker ran, applied 2026-07-28T12:46:52Z). Crossover: LR4 at 2000 Hz.
# Emitted program-domain attenuation: active_baseline_headroom = -22.4581 dB,
# which is exactly the woofer branch's sum of positive gains
# (11.6155 + 5.9619 + 4.8807).


def _peaking(freq: float, q: float, gain: float) -> dict[str, float | str]:
    return {"biquad_type": "Peaking", "freq": freq, "q": q, "gain": gain}


JTS3_20260728_WOOFER: tuple[dict[str, float | str], ...] = (
    _peaking(802.8634, 2.9666, -6.6614),
    _peaking(1122.9276, 2.2072, -4.3468),
    _peaking(1328.0267, 2.0000, -1.0859),
    _peaking(262.3864, 2.0000, -1.0447),
    _peaking(574.0260, 2.0000, -0.6195),
    _peaking(2747.3374, 8.0000, 11.6155),   # 750 Hz into its own stopband
    _peaking(2196.7060, 2.0518, 5.9619),    # also above Fc
    _peaking(377.3931, 2.9666, 4.8807),     # the only in-band boost
)
JTS3_20260728_WOOFER_SECTIONS = (
    CrossoverSection(fc_hz=2000.0, order=4, highpass=False),
)
JTS3_20260728_WOOFER_TRIM_DB = 0.0
JTS3_20260728_APPLIED_HEADROOM_DB = 22.4581


# --------------------------------------------------------------------------- #
# the crossover model
# --------------------------------------------------------------------------- #


def test_linkwitz_riley_is_six_db_down_at_its_own_corner():
    """The defining property of an LR pair, and the one that makes both halves
    of this module correct: each branch is exactly half-power at Fc, so the two
    sum to unity."""
    for order in (2, 4, 8):
        for highpass in (False, True):
            at_fc = crossover_response_db(
                np.array([2000.0]),
                (CrossoverSection(2000.0, order, highpass=highpass),),
            )[0]
            assert at_fc == pytest.approx(-6.0206, abs=1e-3), (order, highpass)


def test_crossover_attenuation_reproduces_the_jts3_stopband_numbers():
    """#1809's own evidence: the two woofer boosts sat where the emitted LR4
    lowpass removes 13.3 and 7.8 dB of them."""
    freqs = np.array([2747.3374, 2196.7060, 377.3931])
    attenuation = crossover_response_db(freqs, JTS3_20260728_WOOFER_SECTIONS)
    # To a hundredth of a dB, because the model is the digital filter the
    # graph runs rather than the analog prototype — the closed form this
    # function used for one review cycle landed these at -13.18 / -7.80.
    assert attenuation[0] == pytest.approx(-13.32, abs=0.01)
    assert attenuation[1] == pytest.approx(-7.83, abs=0.01)
    # …while the one in-band boost is untouched by it.
    assert attenuation[2] == pytest.approx(0.0, abs=0.05)


def test_a_crossover_section_never_passes_more_than_unity():
    """The property the cut-only short-circuit rests on: a chain of cuts behind
    a Linkwitz-Riley section cannot reach unity, so it needs no evaluation."""
    for order in (2, 4, 8):
        for highpass in (False, True):
            response = crossover_response_db(
                CHAIN_GRID_HZ, (CrossoverSection(2000.0, order, highpass=highpass),)
            )
            assert float(np.max(response)) <= 1e-9


def test_radiating_band_is_the_three_db_span_and_mirrors_about_fc():
    lo_w, hi_w = radiating_band_hz(JTS3_20260728_WOOFER_SECTIONS)
    lo_t, hi_t = radiating_band_hz((CrossoverSection(2000.0, 4, highpass=True),))
    assert lo_w == 0.0 and hi_t == float("inf")
    assert hi_w == pytest.approx(1602.9, abs=0.5)
    assert lo_t == pytest.approx(2495.5, abs=0.5)
    # Mirrored about Fc in log frequency — the same mirroring PR-L3's own
    # level bands use, so neither driver is given more of the handoff.
    assert hi_w * lo_t == pytest.approx(2000.0 ** 2, rel=1e-6)
    # …and the edges really are the declared attenuation, to within the
    # analog-prototype-vs-digital-filter difference the band solve accepts on
    # purpose (the CHARGE does not accept it — see the pair-sum test below).
    for band_hz, sections in (
        (hi_w, JTS3_20260728_WOOFER_SECTIONS),
        (lo_t, (CrossoverSection(2000.0, 4, highpass=True),)),
    ):
        assert crossover_response_db(
            np.array([band_hz]), sections
        )[0] == pytest.approx(-CROSSOVER_EDGE_ATTENUATION_DB, abs=0.05)


@pytest.mark.parametrize("fc_hz", [500.0, 2000.0, 5000.0, 10_000.0])
@pytest.mark.parametrize("order", [2, 4, 8])
@pytest.mark.parametrize("highpass", [False, True])
def test_the_band_edge_is_conservative_at_every_corner(fc_hz, order, highpass):
    """The band is solved from the ANALOG prototype while the response is the
    digital filter, so the edge does not land at exactly -3 dB — and the gap
    GROWS with the corner (LR4: -2.98 dB at 2 kHz, -2.87 at 5 kHz, -2.43 at
    10 kHz; -2.08 for the high-pass edge there).

    What has to hold is the DIRECTION, at every corner and order: never more
    attenuation than declared, so the band is never WIDER than a true 3 dB
    span and the fit is never permitted to add level over more of the spectrum
    than the policy intends. The lower bound keeps the drift from becoming
    something other than the small conservatism it is.
    """
    sections = (CrossoverSection(fc_hz, order, highpass=highpass),)
    lo_hz, hi_hz = radiating_band_hz(sections)
    edge_hz = lo_hz if highpass else hi_hz
    at_edge_db = float(crossover_response_db(np.array([edge_hz]), sections)[0])
    assert -CROSSOVER_EDGE_ATTENUATION_DB <= at_edge_db <= -1.5


def test_radiating_band_of_a_three_way_mid_is_bounded_on_both_sides():
    lo, hi = radiating_band_hz((
        CrossoverSection(400.0, 4, highpass=True),
        CrossoverSection(3000.0, 4, highpass=False),
    ))
    assert lo == pytest.approx(499.2, abs=0.5)
    assert hi == pytest.approx(2404.4, abs=0.5)


def test_a_mid_squeezed_between_two_crossovers_gets_an_empty_band():
    """It never reaches full output anywhere, so it honestly gets no lift
    band rather than an invented one. The caller's mask is simply empty."""
    lo, hi = radiating_band_hz((
        CrossoverSection(400.0, 4, highpass=True),
        CrossoverSection(500.0, 4, highpass=False),
    ))
    assert lo > hi


def test_a_branch_with_no_crossover_radiates_everywhere():
    """A one-way box's summed chain, and the default for any caller with no
    topology to declare."""
    assert radiating_band_hz(()) == (0.0, float("inf"))


def test_radiating_band_refuses_a_zero_edge():
    with pytest.raises(ValueError, match="must be positive"):
        radiating_band_hz(JTS3_20260728_WOOFER_SECTIONS, edge_attenuation_db=0.0)


# --------------------------------------------------------------------------- #
# the headroom charge (#1808)
# --------------------------------------------------------------------------- #


def test_the_jts3_profile_charged_five_times_its_realized_peak():
    """**The keystone.** The applied 2026-07-28 woofer branch, evaluated.

    The emitted graph attenuated the program by 22.4581 dB — the sum of that
    branch's positive gains — for a chain whose realized peak is +4.00 dB. The
    speaker ended 8.3 dB below the household's listening level at maximum
    volume, with the master already at 0.0 dB and no travel left. The owner's
    ruling (#1808): corrections must never stack invisible headroom.
    """
    sum_of_positives = sum(
        float(f["gain"]) for f in JTS3_20260728_WOOFER if float(f["gain"]) > 0.0
    )
    assert sum_of_positives == pytest.approx(JTS3_20260728_APPLIED_HEADROOM_DB, abs=1e-3)

    peak_db = branch_chain_peak_db(
        JTS3_20260728_WOOFER,
        sections=JTS3_20260728_WOOFER_SECTIONS,
        trim_db=JTS3_20260728_WOOFER_TRIM_DB,
    )
    # +4.00 dB is the number #1808's own decomposition reports. This re-derives
    # it from the emitted graph rather than from that analysis, and since the
    # crossover term became the digital filter the two agree to 0.001 dB.
    assert peak_db == pytest.approx(4.00, abs=0.005)
    charge = branch_headroom_db(
        JTS3_20260728_WOOFER,
        sections=JTS3_20260728_WOOFER_SECTIONS,
        trim_db=JTS3_20260728_WOOFER_TRIM_DB,
    )
    assert charge == pytest.approx(peak_db + HEADROOM_MARGIN_DB)
    # 17.4 dB of maximum level, handed back to the household.
    assert JTS3_20260728_APPLIED_HEADROOM_DB - charge == pytest.approx(17.46, abs=0.05)


def test_the_realized_peak_sits_at_the_only_in_band_boost():
    """Not an arithmetic coincidence: the chain peaks at 377 Hz, the one boost
    the crossover does not attenuate. The other two are worth ~nothing
    acoustically and were worth 17.58 dB of charge."""
    magnitude_db = 20.0 * np.log10(
        np.abs(chain_response(JTS3_20260728_WOOFER, CHAIN_GRID_HZ))
    ) + crossover_response_db(CHAIN_GRID_HZ, JTS3_20260728_WOOFER_SECTIONS)
    assert float(CHAIN_GRID_HZ[int(np.argmax(magnitude_db))]) == pytest.approx(
        377.4, rel=0.02
    )


def test_the_crossover_term_is_what_makes_the_stopband_boosts_free():
    """Charge the same filters with no crossover declared and the answer is the
    +11.6 dB filter itself — the conservative fallback for a caller with no
    topology to hand, never a louder one."""
    naked = branch_chain_peak_db(JTS3_20260728_WOOFER)
    behind_crossover = branch_chain_peak_db(
        JTS3_20260728_WOOFER, sections=JTS3_20260728_WOOFER_SECTIONS,
    )
    assert naked > behind_crossover
    # The two stopband bells overlap, so the naked cascade reaches past either
    # of them on its own — and still only 14.3 of the 22.458 dB charged.
    assert naked == pytest.approx(14.34, abs=0.1)


def test_the_trim_lowers_the_charge_exactly():
    """A branch already attenuated by its own trim needs that much less
    program-domain headroom — the arithmetic that makes ``normalize_shift_db``
    net-neutral in the flow."""
    peak = branch_chain_peak_db(
        JTS3_20260728_WOOFER, sections=JTS3_20260728_WOOFER_SECTIONS,
    )
    for trim_db in (-1.0, -2.478, -6.0):
        shifted = branch_chain_peak_db(
            JTS3_20260728_WOOFER,
            sections=JTS3_20260728_WOOFER_SECTIONS, trim_db=trim_db,
        )
        assert shifted == pytest.approx(peak + trim_db, abs=1e-9)


def test_a_common_trim_shift_is_net_neutral():
    """Why ``crossover_v2_flow``'s ``normalize_shift_db`` costs nothing under
    this rule, and why it was left exactly as it was.

    The conductor subtracts a common shift S from every branch's trim so none
    lands positive. Under the retired sum-of-positives rule the charge could
    not see the trim at all, so that shift was pure lost level — 2.478 dB of
    it on the 2026-07-28 JTS3 profile. Charging the chain's peak makes it
    exact: the charge falls by the same S the branches gave up, so the
    program-domain attenuation plus the branch trim is invariant.
    """
    def _system_level(trim_db: float) -> float:
        charge = branch_headroom_db(
            JTS3_20260728_WOOFER,
            sections=JTS3_20260728_WOOFER_SECTIONS, trim_db=trim_db,
        )
        return -charge + trim_db  # the pre-split gain plus the branch's own

    baseline = _system_level(0.0)
    for shift_db in (0.5, 2.478, 3.9):
        assert _system_level(-shift_db) == pytest.approx(baseline, abs=1e-9)
    # …and the one case it is NOT neutral, stated rather than hidden: once the
    # whole chain sits below unity the charge floors at 0 and cannot fall
    # further, so a shift past that point is real, disclosed ledger.
    assert _system_level(-8.0) < baseline


def test_a_cut_only_chain_costs_nothing_and_needs_no_evaluation():
    """Every graph before PR-L5, and every follower graph. The short-circuit is
    the reason neither the emitter nor the runtime contract imports numpy for
    the ordinary case."""
    cuts = tuple(f for f in JTS3_20260728_WOOFER if float(f["gain"]) < 0.0)
    assert branch_chain_peak_db(cuts, sections=JTS3_20260728_WOOFER_SECTIONS) == 0.0
    assert branch_headroom_db(cuts, sections=JTS3_20260728_WOOFER_SECTIONS) == 0.0
    # …and a trimmed cut-only branch reports its trim, not a fabricated 0.
    assert branch_chain_peak_db(cuts, trim_db=-3.0) == -3.0
    assert branch_headroom_db((), trim_db=-3.0) == 0.0


def test_the_short_circuit_agrees_with_the_evaluator_it_skips():
    """The shortcut is an optimisation, not a second rule: evaluate the same
    cut-only chain the long way and it lands at or below unity too."""
    cuts = tuple(f for f in JTS3_20260728_WOOFER if float(f["gain"]) < 0.0)
    evaluated = float(np.max(
        20.0 * np.log10(np.abs(chain_response(cuts, CHAIN_GRID_HZ)))
        + crossover_response_db(CHAIN_GRID_HZ, JTS3_20260728_WOOFER_SECTIONS)
    ))
    assert evaluated <= 0.0


def test_a_chain_that_cannot_clip_is_charged_nothing():
    """The doctrine, stated arithmetically: no possible clip, no lost
    loudness. A +6 dB boost buried under a crossover that removes 13 dB of it
    never lifts its branch above the unity it already passes at, so it costs
    the household nothing."""
    buried = (_peaking(2747.3374, 8.0, 6.0),)
    assert branch_chain_peak_db(
        buried, sections=JTS3_20260728_WOOFER_SECTIONS
    ) == pytest.approx(0.0, abs=0.001)
    assert branch_headroom_db(buried, sections=JTS3_20260728_WOOFER_SECTIONS) == 0.0


def test_overlapping_boosts_at_one_frequency_still_add():
    """The case the retired sum-of-positives rule existed to survive. A
    bin-by-bin evaluation already contains it — two coincident +3 dB bells
    charge as +6 dB, not as +3."""
    stacked = (_peaking(800.0, 2.0, 3.0), _peaking(800.0, 2.0, 3.0))
    assert branch_chain_peak_db(stacked) == pytest.approx(6.0, abs=0.05)


def test_the_margin_lands_the_loudest_bin_on_the_limiter_threshold():
    """Why 1.0 dB and not an arbitrary number: the per-driver soft-clip
    limiters sit at -1.0 dBFS, so a fully-charged correction's loudest bin
    arrives exactly at the backstop rather than into it."""
    assert HEADROOM_MARGIN_DB == pytest.approx(-BASELINE_LIMITER_CLIP_LIMIT_DB)


@pytest.mark.parametrize("peak_db", [-12.0, -0.5, 0.0])
def test_no_charge_below_unity(peak_db):
    assert headroom_charge_db(peak_db) == 0.0


def test_charge_above_unity_is_peak_plus_margin():
    assert headroom_charge_db(4.0) == pytest.approx(4.0 + HEADROOM_MARGIN_DB)


def test_the_grid_spans_the_audio_band_the_contract_must_police():
    """Deliberately wider than the fit's own 150 Hz-floored envelope grid: the
    runtime contract reads this against a graph it does not trust, and a grid
    that starts at 150 Hz cannot see a boost tampered in at 60 Hz."""
    assert float(CHAIN_GRID_HZ[0]) == pytest.approx(20.0)
    assert float(CHAIN_GRID_HZ[-1]) == pytest.approx(20_000.0)
    assert branch_chain_peak_db(
        (_peaking(60.0, 2.0, 5.0),)
    ) == pytest.approx(5.0, abs=0.05)


# --------------------------------------------------------------------------- #
# grid RESOLUTION, not just span — tamper hardening
# --------------------------------------------------------------------------- #


def _between_grid_bins_hz() -> float:
    """The worst place to hide a narrow filter: the geometric midpoint of two
    ADJACENT ``CHAIN_GRID_HZ`` bins near 1 kHz.

    Derived from the grid rather than written down, because a hand-picked
    "half a 1/48-octave step above 1 kHz" is only half a step from 1 kHz — and
    1 kHz is not itself a bin, so that value landed 0.33 % from a real bin
    instead of the full 0.72 % a true midpoint sits at.
    """
    grid = np.asarray(CHAIN_GRID_HZ)
    index = int(np.searchsorted(grid, 1000.0))
    return float(math.sqrt(grid[index - 1] * grid[index]))


@pytest.mark.parametrize("q", [0.5, 8.0, 50.0, 500.0, 2000.0, 20000.0])
def test_a_boost_is_read_at_its_full_gain_at_any_q(q):
    """**The span test above is not enough.** A fixed log grid is blind to
    anything narrower than its own spacing, and the prover reads graphs it does
    not trust: a +12 dB Peaking filter at Q 2000, parked at the midpoint
    between two 1/48-octave bins, read -0.0 dB before this — it would have
    proved SAFE against a graph that charged nothing for it.

    No honest fit can emit that (``linearization_fit._PEAKING_Q_MAX`` is 8),
    which is exactly why it has to be pinned here rather than assumed away.
    Placed deliberately BETWEEN grid bins, at any Q, the peak reads true.
    """
    assert branch_chain_peak_db(
        (_peaking(_between_grid_bins_hz(), q, 12.0),)
    ) == pytest.approx(12.0, abs=1e-6)


@pytest.mark.parametrize(
    ("biquad_type", "freq"), [("Lowshelf", 30.0), ("Highshelf", 18_000.0)],
)
def test_a_shelf_is_read_at_its_full_gain_past_the_band_edges(biquad_type, freq):
    """A shelf's extreme is at an EDGE, not at its corner. A +12 dB Lowshelf
    cornered at 30 Hz has only reached 9.69 dB by 20 Hz, so a grid stopping
    there under-read it by 2.3 dB — past the margin, in the loud direction."""
    assert branch_chain_peak_db(
        ({"biquad_type": biquad_type, "freq": freq, "q": 0.7071, "gain": 12.0},)
    ) == pytest.approx(12.0, abs=0.01)


@pytest.mark.parametrize("q", [0.5, 8.0, 30.0, 200.0])
@pytest.mark.parametrize("separation", [1.001, 1.01, 1.06, 1.25])
def test_a_cascade_peak_between_two_centres_is_not_missed(q, separation):
    """Sampling each filter's own centre is not sufficient on its own: two
    near-coincident bells reach more TOGETHER in the middle than either does
    at the other's centre. Measured, the centres alone under-read by up to
    0.58 dB — inside the margin, but a gap that grows with whatever a tampered
    graph chooses. The adjacent-pair midpoints close it to 0.07 dB.

    Graded against a dense sweep, so what is pinned is the distance to the
    CONTINUOUS maximum rather than to another sampling of it. The sweep is
    WINDOWED to the neighbourhood the two bells occupy: a 40 000-point local
    window and a 400 000-point full-band sweep agree on the maximum to
    1.3e-5 dB (both cases peak between the centres by construction), for a
    fortieth of the CI cost across three Python legs.
    """
    filters = (
        _peaking(1000.0, q, 6.0),
        _peaking(1000.0 * separation, q, 6.0),
    )
    dense = np.geomspace(1000.0 * 0.8, 1000.0 * separation * 1.25, 40_000)
    true_peak = float(np.max(
        20.0 * np.log10(np.abs(chain_response(filters, dense)))
    ))
    assert branch_chain_peak_db(filters) > true_peak - 0.1


def test_the_honest_emitters_worst_case_was_already_inside_the_margin():
    """Stated so the hardening is not mistaken for a bug fix in the charge: at
    the fit engine's own Q ceiling the grid error never reached the margin.
    This is about tampered graphs, not about what the emitter writes."""
    from jasper.active_speaker.linearization_fit import _PEAKING_Q_MAX

    filters = (_peaking(_between_grid_bins_hz(), _PEAKING_Q_MAX, 12.0),)
    on_the_fixed_grid = float(np.max(20.0 * np.log10(
        np.abs(chain_response(filters, CHAIN_GRID_HZ))
    )))
    assert 12.0 - on_the_fixed_grid < HEADROOM_MARGIN_DB


# --------------------------------------------------------------------------- #
# the analytic LR model vs the digital filter the graph realizes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("order", [2, 4, 8])
@pytest.mark.parametrize("fc_hz", [200.0, 2000.0, 6000.0, 10_000.0])
def test_a_linkwitz_riley_pair_sums_to_unity_at_every_frequency(order, fc_hz):
    """**The defining property, and the strongest correctness check there is
    for this model.** A Linkwitz-Riley pair is chosen precisely because its
    two branches sum flat — that is what makes it a crossover rather than two
    filters. Nothing else in this file would catch a wrong Butterworth pole-Q
    table or an off-by-one in the order-to-passes arithmetic; this catches
    both, at every order, on the frequencies the digital filter actually
    realises rather than on the analog prototype's algebra.

    It also pins the reason the model was rebuilt digitally: the closed form
    ``1/(1 + (f/fc)^N)`` this function used for one review cycle under-read a
    branch's own output by up to 1.02 dB (LR4) / 1.58 dB (LR2) near a 10 kHz
    corner — the whole of ``HEADROOM_MARGIN_DB``, in the loud direction. At
    10 kHz, above, that error is gone.
    """
    freqs = np.geomspace(20.0, 20_000.0, 1000)
    low = crossover_response_db(
        freqs, (CrossoverSection(fc_hz, order, highpass=False),)
    )
    high = crossover_response_db(
        freqs, (CrossoverSection(fc_hz, order, highpass=True),)
    )
    summed_db = 20.0 * np.log10(np.abs(10.0 ** (low / 20.0) + 10.0 ** (high / 20.0)))
    assert float(np.max(np.abs(summed_db))) < 0.01
    # …and neither branch ever passes more than unity, the property the
    # cut-only short-circuit rests on.
    assert float(np.max(low)) <= 1e-9 and float(np.max(high)) <= 1e-9


@pytest.mark.parametrize("order", [4, 8])
@pytest.mark.parametrize("highpass", [False, True])
def test_the_model_is_the_butterworth_biquad_cascade_the_graph_realizes(
    order, highpass,
):
    """The decomposition itself, pinned against the SHARED evaluator rather
    than against this module's own arithmetic: LR(N) is two cascaded
    Butterworth passes of order N/2, and for the biquad-expressible orders
    that is a spelled-out list of RBJ sections through
    :func:`chain_response`. (LR2's passes are first-order, which no biquad
    spells — the pair-sum test above is what covers it.)
    """
    fc_hz, butterworth_order = 2000.0, order // 2
    qs = [
        1.0 / (2.0 * math.sin(math.pi * (2 * k + 1) / (2 * butterworth_order)))
        for k in range(butterworth_order // 2)
    ]
    expected_sections = [
        {"biquad_type": "Highpass" if highpass else "Lowpass",
         "freq": fc_hz, "q": q, "gain": 0.0}
        for q in qs for _ in range(2)   # the Butterworth pass, run twice
    ]
    freqs = np.geomspace(20.0, 20_000.0, 400)
    expected_db = 20.0 * np.log10(np.abs(chain_response(expected_sections, freqs)))
    actual_db = crossover_response_db(
        freqs, (CrossoverSection(fc_hz, order, highpass=highpass),)
    )
    assert float(np.max(np.abs(actual_db - expected_db))) < 1e-9
