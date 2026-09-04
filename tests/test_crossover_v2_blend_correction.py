# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Decision 10's blend-region correction: the solve, the clamps, the loop.

The bounds, the refusals, and the iteration are the whole safety argument of a
stage that emits biquads into the audio path, so each is pinned here against a
mutation rather than against a restatement of the production expression.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker import camilla_yaml
from jasper.active_speaker.branch_chain import chain_response
from jasper.active_speaker.camilla_yaml import (
    ActiveSpeakerConfigError,
    emit_active_speaker_baseline_config,
)
from jasper.active_speaker.crossover_v2 import blend_correction as bc
from jasper.active_speaker.crossover_v2 import coordinator, round_evidence
from jasper.active_speaker.crossover_v2.verification import (
    BENEFIT_IMPROVED,
    BENEFIT_NO_REGION_BAND,
    BENEFIT_WITHIN_MARGIN,
    MeasurementComparand,
    evaluate_benefit,
    evaluate_region_benefit,
)
from jasper.active_speaker.crossover_v2.contracts import BenefitStatus, ResponseCurve
from jasper.active_speaker.flat_spec import (
    GradedSpec,
    evaluate_flat_spec,
    spec_convergence_residual,
)
from jasper.audio_measurement.comparison_bands import (
    crossover_region_band_hz,
    overlap_band_hz,
)

from tests.crossover_v2_fixtures import _preset

#: The band every series-1 round on jts3 actually graded, and the corner it was
#: graded at. Real numbers rather than round ones, so a fixture cannot quietly
#: become easier than the rig.
SERIES1_BAND_HZ = (824.35, 3297.4)
SERIES1_FC_HZ = 1648.7
#: The tweeter's MEASURE sweep floor on that rig — the edge ``overlap_band_hz``
#: would clamp the region's lower bound UP to, and the reason it is the wrong
#: owner for a summed claim.
SERIES1_TWEETER_SWEEP_LO_HZ = 1600.0
#: Where the series-1 dip sat, in every round after the first.
SERIES1_DIP_HZ = 1938.0

_GRID = np.geomspace(100.0, 16000.0, 600)


def _bell(f0_hz: float, gain_db: float, q: float = 2.0, grid=None) -> np.ndarray:
    """An RBJ peaking magnitude on the grid — a lobe to put in a fixture."""

    freqs = _GRID if grid is None else grid
    n = freqs / f0_hz
    a = 10.0 ** (gain_db / 40.0)
    return 20.0 * np.log10(
        np.sqrt((1.0 - n * n) ** 2 + (a * n / q) ** 2)
        / np.sqrt((1.0 - n * n) ** 2 + (n / (a * q)) ** 2)
    )


def _graded(curve_db: np.ndarray, mask: np.ndarray | None = None) -> GradedSpec:
    excluded = np.zeros_like(_GRID, dtype=bool) if mask is None else mask
    return GradedSpec(
        _GRID, curve_db, excluded, evaluate_flat_spec(_GRID, curve_db, excluded),
    )


def _solve(curve_db, *, incumbent=(), band=SERIES1_BAND_HZ, mask=None):
    return bc.solve_blend_correction(
        graded=_graded(curve_db, mask), band_hz=band, incumbent=incumbent,
    )


def _cascade_db(filters, grid=None) -> np.ndarray:
    freqs = _GRID if grid is None else grid
    if not filters:
        return np.zeros_like(freqs)
    return 20.0 * np.log10(np.maximum(np.abs(chain_response(filters, freqs)), 1e-12))


# --------------------------------------------------------------------------- #
# 1-2. the region's owner
# --------------------------------------------------------------------------- #


def test_the_region_is_the_summed_bands_owner_not_the_per_branch_ones():
    """#2600 §0: the blend region comes from ``crossover_region_band_hz``.

    The two functions are not spellings of one fact. ``overlap_band_hz`` clamps
    the lower edge UP to the tweeter's own sweep floor because its consumers
    read a single branch, which below that floor is deconvolution noise from a
    driver that was never excited. A summed capture has no such problem — and
    the null a two-way blends into lands exactly in the span that clamp
    removes.

    Asserted on the series-1 numbers rather than on synthetic ones, because
    what makes this load-bearing is a measured coincidence: the per-branch
    band's floor is 1600 Hz and the dip sat at 1938 Hz, so a per-branch region
    would have amputated the bottom half of the thing being corrected — and
    would still have contained the dip, which is why "the dip is in the band"
    is not sufficient evidence that the right band was used.
    """

    summed = crossover_region_band_hz(
        SERIES1_FC_HZ, validity_floor_hz=100.0, radiated_band_hz=(100.0, 20000.0),
    )
    per_branch = overlap_band_hz(
        SERIES1_FC_HZ,
        tweeter_sweep_lo_hz=SERIES1_TWEETER_SWEEP_LO_HZ,
        woofer_sweep_hi_hz=6000.0,
    )
    assert summed is not None
    assert summed[0] == pytest.approx(SERIES1_BAND_HZ[0], abs=0.1)
    assert per_branch[0] == pytest.approx(SERIES1_TWEETER_SWEEP_LO_HZ, abs=0.1)
    # Both contain the dip; only one contains the octave below it.
    assert summed[0] < SERIES1_DIP_HZ < summed[1]
    assert per_branch[0] < SERIES1_DIP_HZ < per_branch[1]
    assert summed[0] < per_branch[0], "the summed band must reach lower"


def test_the_round_reads_the_region_off_the_absolute_claim():
    """The wiring half of the assertion above, at the reader.

    The correction consumes ``crossover_region_band_hz`` through the claim that
    already calls it, so the band cut over is the band the household is shown.
    A reader pointed at the TRACKING band instead — the plausible mistake, and
    the one #1868 records as already made once — fails here, because the two
    bands differ on this fixture.
    """

    analysis = SimpleNamespace(
        verify_absolute={"band_hz": list(SERIES1_BAND_HZ)},
        verify_tracking={"band_hz": [1600.0, 3297.4]},
    )

    assert round_evidence._crossover_region(analysis)[0] == SERIES1_BAND_HZ


@pytest.mark.parametrize(
    "absolute",
    [
        None,
        {"not_evaluated": "no_crossover_fc"},
        {"not_evaluated": "no_candidate_crossover_target"},
        {"not_evaluated": "no_trusted_crossover_region"},
        {"band_hz": [3297.4, 824.35]},
        {"band_hz": [float("nan"), 3297.4]},
        {"band_hz": "824-3297"},
    ],
    ids=["absent", "no_fc", "no_target", "no_region", "inverted", "nan", "text"],
)
def test_every_unevaluated_absolute_claim_yields_no_region(absolute):
    """Each ``not_evaluated`` arm becomes "no band" without its own translation.

    That is the whole reason the band is read off this claim rather than
    re-derived: the refusals come with it.
    """

    analysis = SimpleNamespace(verify_absolute=absolute)
    assert round_evidence._crossover_region(analysis)[0] is None


def test_no_band_prescribes_nothing_and_names_the_arm():
    result = bc.solve_blend_correction(graded=None, band_hz=None, incumbent=())
    assert result.filters == ()
    assert result.reason == bc.BLEND_NO_TRUSTED_BAND


# --------------------------------------------------------------------------- #
# 3-5. the fit's shape
# --------------------------------------------------------------------------- #


def test_two_hot_lobes_yield_two_bounded_cuts_at_those_frequencies():
    """#2600 §2's fit shape, every bound asserted on the EVALUATED cascade."""

    curve = _bell(1150.0, 6.0, q=4.0) + _bell(2900.0, 5.0, q=4.0)

    result = _solve(curve)

    assert result.reason == bc.BLEND_CORRECTED
    assert 1 <= len(result.filters) <= bc.BLEND_MAX_FILTERS
    for entry in result.filters:
        assert entry["biquad_type"] == "Peaking"
        assert entry["q"] <= bc.BLEND_FILTER_Q
        assert entry["gain"] < 0.0
        assert abs(entry["gain"]) <= bc.BLEND_MAX_FILTER_CUT_DB
        assert SERIES1_BAND_HZ[0] <= entry["freq"] <= SERIES1_BAND_HZ[1]
    placed = sorted(entry["freq"] for entry in result.filters)
    assert placed[0] == pytest.approx(1150.0, rel=0.15)
    assert placed[-1] == pytest.approx(2900.0, rel=0.15)
    region = (_GRID >= SERIES1_BAND_HZ[0]) & (_GRID <= SERIES1_BAND_HZ[1])
    composed = _cascade_db(result.filters)
    assert composed[region].min() >= -bc.BLEND_MAX_TOTAL_CUT_DB


def test_a_pure_dip_earns_no_cut_at_all():
    """The case series-1 is most likely to hit, and the one that must not
    "flatten" the region by cutting its shoulders.

    A cuts-only stage cannot fill a dip. The tempting near-miss is to measure
    deviation against the REGION's own mean, which makes a region that is
    merely quiet look like it has hot shoulders around its trough — cutting
    those trades one narrow notch for a wide hole across the presence band.
    The reference is the speaker's, so nothing here is hot and nothing is cut.
    """

    result = _solve(_bell(SERIES1_DIP_HZ, -4.3))

    assert result.filters == ()
    assert result.reason == bc.BLEND_NOTHING_TO_CUT
    # The region was still READ — a refusal to cut is not a refusal to measure.
    assert result.reading is not None
    assert result.reading.worst_db < 0.0
    assert result.reading.worst_hz == pytest.approx(SERIES1_DIP_HZ, rel=0.05)


def test_an_excess_smaller_than_the_model_error_earns_no_cut():
    """Below the gap between what is modelled and what the hardware realizes,
    a correction is not something that can be honestly claimed."""

    result = _solve(_bell(1500.0, bc.BLEND_MIN_CUT_DB * 0.5, q=4.0))

    assert result.filters == ()
    assert result.reason == bc.BLEND_NOTHING_TO_CUT


def test_a_lobe_the_honesty_mask_removed_is_not_cut_there():
    """The masked bins are not merely ungraded — they are un-cuttable.

    Per #2600 item 1 the null detector reports ``uncalibrated_below_hf_floor``
    across the entire blend window of any crossover below 4 kHz, so the merged
    mask is the only instrument standing between this stage and a filter cut
    into an interference null.
    """

    hot = _bell(1200.0, 6.0, q=6.0)
    masked = (_GRID > 1100.0) & (_GRID < 1320.0)

    result = _solve(hot, mask=masked)

    for entry in result.filters:
        assert not (1100.0 < entry["freq"] < 1320.0), (
            "a cut was placed on a bin the honesty mask removed"
        )


def test_a_region_left_with_too_few_bins_is_not_a_region():
    """Mostly-mask is not evidence: a "worst bin" there is an artefact of what
    little survived rather than a shape."""

    hot = _bell(1500.0, 6.0, q=4.0)
    nearly_all = (_GRID > 830.0) & (_GRID < 3290.0)

    result = _solve(hot, mask=nearly_all)

    assert result.filters == ()
    assert result.reason == bc.BLEND_NO_TRUSTED_BAND


# --------------------------------------------------------------------------- #
# 6-10. the clamps
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "curve",
    [
        _bell(1500.0, 24.0, q=4.0),
        _bell(1500.0, 12.0, q=1.0),
        _bell(1500.0, 9.0, q=4.0) + _bell(1700.0, 9.0, q=4.0),
        _bell(1500.0, 40.0, q=8.0),
    ],
    ids=["huge", "wide", "adjacent-pair", "absurd"],
)
def test_no_curve_however_hot_produces_a_boost_or_breaks_a_ceiling(curve):
    """The solver's own half of cuts-only, plus both depth ceilings.

    Swept across shapes chosen to attack each bound separately — a single
    enormous lobe (per-filter cap), a wide one (the fit's Q floor), two
    adjacent lobes whose skirts overlap (the COMPOSED cap, which a sum of gains
    would miss), and one past any physical plausibility.
    """

    result = _solve(curve)
    region = (_GRID >= SERIES1_BAND_HZ[0]) & (_GRID <= SERIES1_BAND_HZ[1])

    assert len(result.filters) <= bc.BLEND_MAX_FILTERS
    for entry in result.filters:
        assert entry["gain"] <= 0.0, "the solver produced a boost"
        assert abs(entry["gain"]) <= bc.BLEND_MAX_FILTER_CUT_DB
    composed = _cascade_db(result.filters)
    assert composed[region].min() >= -bc.BLEND_MAX_TOTAL_CUT_DB
    assert composed.max() <= 1e-9, "the composed correction rises above unity"


def test_the_emitter_refuses_a_boost_rather_than_clamping_it():
    """The SECOND, independent place cuts-only is enforced.

    Between the solver and this gate sits a JSON round trip through a persisted
    candidate, which is exactly where a value the solver never produced could
    appear. A refusal rather than a clamp, because a positive gain means the
    record was not written by the code that claims to own it.
    """

    with pytest.raises(ActiveSpeakerConfigError, match="must not exceed"):
        emit_active_speaker_baseline_config(
            _preset(),
            playback_device="hw:CARD=X,DEV=0",
            blend_correction=[
                {"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": 0.1},
            ],
        )


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"biquad_type": "Highshelf", "freq": 1.9e3, "q": 2.0, "gain": -1.0},
         "must be one of"),
        ({"biquad_type": "Peaking", "freq": 0.0, "q": 2.0, "gain": -1.0},
         "must be positive"),
        ({"biquad_type": "Peaking", "freq": 1.9e3, "q": -2.0, "gain": -1.0},
         "must be positive"),
        ({"biquad_type": "Peaking", "freq": float("inf"), "q": 2.0, "gain": -1.0},
         "finite"),
        ("not-a-mapping", "must be a mapping"),
    ],
    ids=["shelf", "zero-freq", "negative-q", "infinite-freq", "not-a-mapping"],
)
def test_the_emitter_gate_refuses_every_malformed_entry(entry, match):
    with pytest.raises(ActiveSpeakerConfigError, match=match):
        emit_active_speaker_baseline_config(
            _preset(), playback_device="hw:CARD=X,DEV=0", blend_correction=[entry],
        )


def test_the_emitter_refuses_more_filters_than_the_solver_can_make():
    with pytest.raises(ActiveSpeakerConfigError, match="count exceeds"):
        emit_active_speaker_baseline_config(
            _preset(),
            playback_device="hw:CARD=X,DEV=0",
            blend_correction=[
                {"biquad_type": "Peaking", "freq": 1000.0 + i, "q": 2.0,
                 "gain": -1.0}
                for i in range(bc.BLEND_MAX_FILTERS + 1)
            ],
        )


def test_the_bounds_are_the_numbers_the_evidence_earned():
    """The four bounds, as LITERALS.

    Every other assertion in this file reads the constants, which is right —
    they are testing behaviour against policy. This one is testing the policy,
    so reading the constant would make it a tautology that passes at any value.
    It is not decoration: a mutation raising
    ``BLEND_MAX_FILTER_CUT_DB`` to 99 left the whole suite green, because the
    composed ceiling still bounded the graph and every per-filter assertion
    moved with the constant it was checking.

    Each number's derivation, so a future change is made rather than drifted
    into:

    * **3.0 dB per filter** — the woofer's own acknowledged
      ``measured_excess_db`` inside the series-1 blind zone (2.09-2.26 dB,
      rounds r1/r2/r4 over 1291.4-2077.2 Hz) plus one model tracking error
      (0.5 dB, ``crossover_v2_flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB``):
      2.26 + 0.5 = 2.76, rounded up.
    * **4.0 dB composed** — just under the whole observed defect, which the
      cloud flat-spec gauge read at -4.24 dB worst across series-1. Correcting
      more than the defect is over-correction by definition.
    * **2 filters** — what the evidence in this region supports, given one mono
      sweep per position and a null detector that is uncalibrated across the
      entire blend window of any crossover below 4 kHz (#2600 item 1).
    * **0.5 dB floor** — this model's own measured tracking error. Below it, a
      correction is not something that can be honestly claimed.
    * **Q 2.0** — a deliberate tightening against the fit engine's ``Q <= 8``
      for cuts, and the Q every peaking filter the series-1 fits emitted used.
    * **k = 0.7** — see :data:`~.blend_correction.BLEND_DAMPING`.
    """

    assert bc.BLEND_MAX_FILTER_CUT_DB == 3.0
    assert bc.BLEND_MAX_TOTAL_CUT_DB == 4.0
    assert bc.BLEND_MAX_FILTERS == 2
    assert bc.BLEND_MIN_CUT_DB == 0.5
    assert bc.BLEND_FILTER_Q == 2.0
    assert bc.BLEND_DAMPING == 0.7


def test_no_single_filter_is_cut_deeper_than_three_decibels():
    """The per-filter ceiling, asserted against a LITERAL and on a curve where
    it binds ALONE.

    The composed ceiling is 4.0, so a single filter has a whole decibel of room
    beneath it — which is exactly the gap a raised per-filter cap escapes into,
    invisibly, if the assertion reads the constant.
    """

    for gain_db in (8.0, 12.0, 24.0):
        result = _solve(_bell(1500.0, gain_db, q=bc.BLEND_FILTER_Q))
        assert result.filters
        for entry in result.filters:
            assert -3.0 <= entry["gain"] < 0.0, (
                f"a {gain_db} dB lobe earned a {entry['gain']} dB cut"
            )


def test_the_first_round_commands_less_than_the_excess_it_measured():
    """The damping, observed rather than restated.

    An undamped loop commands the whole measured excess on round one. This one
    commands about 70% of it, which is what makes the round-over-round
    iteration a contraction rather than a step that has to be right first time.
    Asserted as a RANGE around the observed ratio rather than by recomputing
    ``BLEND_DAMPING * excess`` — which would pass at any damping, including
    none.
    """

    curve = _bell(1500.0, 3.0, q=bc.BLEND_FILTER_Q)
    graded = _graded(curve)
    result = bc.solve_blend_correction(
        graded=graded, band_hz=SERIES1_BAND_HZ, incumbent=(),
    )
    assert len(result.filters) == 1

    # The excess the solver saw: deviation against the speaker's own flat
    # reference, derived here from the arrays rather than read off the result.
    region = (_GRID >= SERIES1_BAND_HZ[0]) & (_GRID <= SERIES1_BAND_HZ[1])
    excess_db = float(np.max(curve[region]) - graded.report.reference_db)
    commanded = -result.filters[0]["gain"]
    ratio = commanded / excess_db

    assert 0.6 < ratio < 0.8, (
        f"commanded {commanded:.3f} dB against a {excess_db:.3f} dB excess "
        f"(ratio {ratio:.3f}); an undamped loop would command ~1.0"
    )


def test_the_emitters_bounds_equal_the_solvers():
    """The two constants are held apart on purpose — the emitter re-validates
    what a persisted candidate claims rather than importing the solver's policy
    and inheriting a future change to it silently — so a test is what keeps
    them numerically equal."""

    assert camilla_yaml.MAX_BLEND_CORRECTION_FILTERS == bc.BLEND_MAX_FILTERS
    assert camilla_yaml.MAX_BLEND_CORRECTION_GAIN_DB == 0.0


def _emitted(blend) -> str:
    return emit_active_speaker_baseline_config(
        _preset(), playback_device="hw:CARD=X,DEV=0", blend_correction=blend,
    )


def test_the_blend_block_is_pre_split_and_above_the_headroom_gain():
    """Placement IS the safety argument, so placement is asserted, not assumed.

    Above the split mixer, so the stage is upstream of every per-driver
    crossover, limiter, and the tweeter high-pass that is its only protection
    in the durable baseline.

    Above ``active_baseline_headroom``, so the stage sits where a boost WOULD
    be absorbable — necessary for absorption, and not sufficient for it. See
    ``test_the_blend_stage_charges_no_headroom_and_is_not_a_term`` for the
    other half, and for why the earlier claim here (that position alone
    absorbed a future boost) was false.
    """

    yaml = _emitted([
        {"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -2.5},
    ])
    pipeline = yaml.split("pipeline:", 1)[1]

    blend_at = pipeline.index("as_blend_1")
    headroom_at = pipeline.index("active_baseline_headroom")
    split_at = pipeline.index("split_active_")
    assert blend_at < headroom_at < split_at


def test_the_correction_is_common_mode_by_construction():
    """One summed fact, one filter, on both program channels and nowhere else.

    Applying the same ``B(f)`` to every role scales the sum and leaves the
    inter-driver complex ratio untouched. An asymmetric application would move
    the interference pattern, which is alignment work — contract clause (c)'s
    tool, not this one's. Pre-split makes that unrepresentable, and this is the
    assertion that says so: the filter is wired exactly once, on ``[0, 1]``,
    and appears in no per-driver chain.
    """

    yaml = _emitted([
        {"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -2.5},
    ])
    pipeline = yaml.split("pipeline:", 1)[1]

    steps = [
        line.strip() for line in pipeline.splitlines()
        if "as_blend_1" in line
    ]
    assert len(steps) == 1, f"the blend filter is wired {len(steps)} times"
    lines = pipeline.splitlines()
    index = next(i for i, line in enumerate(lines) if "as_blend_1" in line)
    assert "channels: [0, 1]" in lines[index - 1]
    after_split = pipeline.split("split_active_", 1)[1]
    assert "as_blend" not in after_split


def test_a_candidate_with_no_blend_correction_emits_the_same_graph_as_before():
    """Absent means absent: no filter definition, no pipeline step, no
    difference from a graph written before this stage existed."""

    assert _emitted(None) == _emitted([]) == _emitted(())
    assert "as_blend" not in _emitted(None)


def _headroom_gain(yaml: str) -> str:
    block = yaml.split("active_baseline_headroom:", 1)[1]
    return block.split("gain:", 1)[1].splitlines()[0].strip()


def test_the_blend_stage_charges_no_headroom():
    """The graph's gain staging is byte-identical with and without the stage.

    Correct because the stage cannot boost — pinned at the solver by
    ``test_no_curve_however_hot_produces_a_boost_or_breaks_a_ceiling`` and at
    the emitter by ``test_the_emitter_refuses_a_boost_rather_than_clamping_it``.
    A boostable blend stage would need a term in ``total_headroom_db``.
    """

    plain = _emitted(None)
    with_blend = _emitted([
        {"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -3.0},
    ])
    assert _headroom_gain(plain) == _headroom_gain(with_blend)


# --------------------------------------------------------------------------- #
# 11-14. the iteration
# --------------------------------------------------------------------------- #


def _round_trip(underlying_db: np.ndarray, incumbent, *, realized: float = 1.0):
    """One simulated round: measure through the incumbent, then re-prescribe.

    ``realized`` scales what the incumbent actually delivered against what it
    commanded — the loop gain the damping exists to survive. Series-1 measured
    that ratio from 0.136 to 11.736 across bands.
    """

    measured = underlying_db + realized * _cascade_db(incumbent)
    return _solve(measured, incumbent=incumbent)


#: A defect the correction can actually REPRESENT — one peaking lobe at the
#: solver's own Q. The iteration math and the fit's shape-matching are separate
#: questions, and a fixture that mixes them tests neither cleanly.
_UNDERLYING = _bell(1500.0, 4.0, q=bc.BLEND_FILTER_Q)


def _region_rms(curve_db: np.ndarray) -> float:
    region = (_GRID >= SERIES1_BAND_HZ[0]) & (_GRID <= SERIES1_BAND_HZ[1])
    return float(np.sqrt(np.mean(curve_db[region] ** 2)))


def _series(rounds: int, *, incumbent_accounted: bool) -> list[float]:
    """The region residual after each of ``rounds`` prescriptions.

    ``incumbent_accounted=False`` drives the NAIVE form §2 names — deriving the
    correction as if the measurement had not been taken through the incumbent.
    """

    incumbent: tuple = ()
    residuals = []
    for _ in range(rounds):
        measured = _UNDERLYING + _cascade_db(incumbent)
        result = bc.solve_blend_correction(
            graded=_graded(measured),
            band_hz=SERIES1_BAND_HZ,
            incumbent=incumbent if incumbent_accounted else (),
        )
        incumbent = result.filters
        residuals.append(_region_rms(_UNDERLYING + _cascade_db(incumbent)))
    return residuals


def test_the_loop_converges_on_the_underlying_defect():
    """#2600 §2's fixed point: ``B* = −u``.

    ``B_{N+1} = B_N − k·d_{N+1}`` with ``d_{N+1} = u + B_N`` has the fixed
    point ``B* = −u`` and contracts by ``(1 − k)`` each round. Asserted as a
    monotone approach to that fixed point rather than as an exact value,
    because the prescription is refit to biquads every round and the refit is
    not the identity — the floor below is that refit's own residual.

    **The fixed point is asserted; the per-round RATE is not.** ``k`` is how
    much of the region's deviation the round reads, and that depends on how
    much of the defect sits inside ``REFERENCE_BAND_HZ``: a defect the frame
    is pooled over is partly charged to the frame instead of to the region,
    so the loop reads less of it per round and takes more rounds. This
    fixture's bell sits at 1500 Hz, inside the frame, which is the slow case
    — and it still lands on the same floor. Pinning a rate would pin the
    frame, which is a separate ruling.
    """

    residuals = _series(9, incumbent_accounted=True)

    assert residuals == sorted(residuals, reverse=True), (
        f"not monotone: {residuals}"
    )
    assert residuals[-1] < 0.05, f"did not reach the refit floor: {residuals}"


def test_the_absolute_re_derive_this_replaces_does_not_converge():
    """The positive control for the test above — the exact bug §2 names.

    Deriving the correction from scratch each round, as if the measurement had
    not been taken through the incumbent, is not merely slower: it has a
    different fixed point and never reaches ``−u``. ``B_{N+1} = −k(u + B_N)``
    alternates about ``−ku/(1+k)`` — measured here as a residual that stops
    falling and then rises again, while the shipped form's keeps falling.

    Without this contrast, "the shipped form converges" would be a restatement
    of what the code does rather than a claim about which of two forms is
    right.
    """

    naive = _series(9, incumbent_accounted=False)
    shipped = _series(9, incumbent_accounted=True)

    assert naive[1] > naive[0], "the naive form's second round made it worse"
    assert naive != sorted(naive, reverse=True), (
        f"the naive form should not be monotone: {naive}"
    )
    assert min(naive[1:]) > 10 * shipped[-1], (
        f"the naive form should stall well above the shipped one: "
        f"naive={naive} shipped={shipped}"
    )


def test_a_wildly_over_realizing_band_stays_inside_every_cap():
    """R1's trusted-HF band realized 11.736x what it commanded. A loop gain
    that large diverges at ``k = 1``; the damping plus the per-round clamp is
    what keeps the excursion bounded even when the gain assumption is wrong."""

    underlying = _bell(1500.0, 4.0, q=4.0)
    incumbent: tuple = ()
    region = (_GRID >= SERIES1_BAND_HZ[0]) & (_GRID <= SERIES1_BAND_HZ[1])
    for _ in range(6):
        result = _round_trip(underlying, incumbent, realized=11.736)
        incumbent = result.filters
        for entry in incumbent:
            assert entry["gain"] <= 0.0
            assert abs(entry["gain"]) <= bc.BLEND_MAX_FILTER_CUT_DB
        composed = _cascade_db(incumbent)
        assert composed[region].min() >= -bc.BLEND_MAX_TOTAL_CUT_DB


def test_an_incumbent_that_cannot_be_established_prescribes_nothing():
    """#2653's condition applied to this quantity: refuse when the
    reconciliation cannot be established, never assume zero.

    Assuming an empty incumbent when the real one is unknown double-counts the
    correction the measurement was taken through — the precise shape #2653
    reverted for the level datum.
    """

    result = _solve(_bell(1500.0, 6.0, q=4.0), incumbent=None)

    assert result.filters == ()
    assert result.reason == bc.BLEND_NO_INCUMBENT
    assert result.band_hz == SERIES1_BAND_HZ, (
        "a refusal must still name the region it refused about"
    )


def test_a_perfect_incumbent_is_re_prescribed_rather_than_removed():
    """The prescription is a TOTAL, not a delta: a round that measures flat
    because its incumbent is working must command that incumbent again."""

    incumbent = (
        {"biquad_type": "Peaking", "freq": 1500.0, "q": 2.0, "gain": -2.5},
    )
    flat_through_it = np.zeros_like(_GRID)

    result = _solve(flat_through_it, incumbent=incumbent)

    assert len(result.filters) == 1
    assert result.filters[0]["freq"] == pytest.approx(1500.0, rel=0.05)
    assert result.filters[0]["gain"] == pytest.approx(-2.5, abs=0.3)


@pytest.mark.parametrize(
    "raw",
    [
        [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": 0.5}],
        [{"biquad_type": "Highshelf", "freq": 1900.0, "q": 2.0, "gain": -1.0}],
        [{"biquad_type": "Peaking", "freq": -1.0, "q": 2.0, "gain": -1.0}],
        [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0}],
        [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": "loud"}],
        "not-a-list",
        {"biquad_type": "Peaking"},
        [{"biquad_type": "Peaking", "freq": 1e3 + i, "q": 2.0, "gain": -1.0}
         for i in range(bc.BLEND_MAX_FILTERS + 1)],
    ],
    ids=["boost", "shelf", "negative-freq", "missing-gain", "text-gain",
         "string", "mapping", "too-many"],
)
def test_an_unreadable_persisted_incumbent_reads_as_unknown_not_as_empty(raw):
    """``None`` (unknown) and ``()`` (applied none) must not collapse.

    A record this reader cannot vouch for is unknown, and unknown refuses.
    Returning ``()`` instead would silently claim the capture rode a flat
    graph, which is the assumption that double-counts.
    """

    assert bc.blend_filters_from_mapping(raw) is None


def test_a_genuinely_empty_incumbent_is_empty_not_unknown():
    assert bc.blend_filters_from_mapping([]) == ()
    assert bc.blend_filters_from_mapping(None) is None


# --------------------------------------------------------------------------- #
# the PRODUCTION incumbent path (panel: correctness SF1 == safety SF3)
# --------------------------------------------------------------------------- #


_CORRUPT_PROFILE_SHAPES = [
    [{"biquad_type": "Peaking", "freq": "1900", "q": 2.0, "gain": -1.0}],
    [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": 0.5}],
    [{"biquad_type": "Highshelf", "freq": 1900.0, "q": 2.0, "gain": -1.0}],
    [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0}],
    [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -1.0},
     "not-a-mapping"],
    [{"biquad_type": "Peaking", "freq": float("nan"), "q": 2.0, "gain": -1.0}],
    [{"biquad_type": "Peaking", "freq": 1e3 + i, "q": 2.0, "gain": -1.0}
     for i in range(bc.BLEND_MAX_FILTERS + 1)],
    ["not-a-mapping"],
]


@pytest.mark.parametrize(
    "corrupt", _CORRUPT_PROFILE_SHAPES, ids=range(len(_CORRUPT_PROFILE_SHAPES)),
)
def test_a_corrupt_applied_profile_reads_as_unknown_through_production(corrupt):
    """The guard, re-pointed at the reader production actually uses.

    Both panel lenses found this independently: the strict reader guarded the
    APPLY path while the SOLVE's incumbent came from
    ``baseline_profile.profile_blend_correction``, which answers "where is it"
    and not "is it valid". Every shape below took one of two wrong paths and
    neither was ``no_incumbent`` — a non-numeric ``freq`` RAISED, and garbage
    collapsed to ``()``, which claims the capture rode a flat graph.

    This drives the two production functions in sequence, exactly as
    ``crossover_v2_flow._applied_blend_correction`` does, so a fix applied to
    the wrong reader cannot satisfy it.
    """

    from jasper.active_speaker.baseline_profile import profile_blend_correction

    located = profile_blend_correction({"blend_correction": corrupt})
    assert located is not None, "the structural reader lost the list entirely"
    assert len(located) == len(corrupt), (
        "the structural reader TRUNCATED a corrupt list into a shorter "
        "valid-looking one"
    )
    assert bc.blend_filters_from_mapping(list(located)) is None


def test_the_production_reader_does_not_raise_on_any_corrupt_shape():
    """A corrupt entry must never cost a round its receipt or its restore.

    The escape shape matters as much as the verdict: ``evaluate_round`` is
    called inside the coordinator's broad except, so a raise here does not
    surface as an error — it produces a ``RoundDecision`` of ``None``s, which
    means no receipt banked AND a round that should restore silently not
    restoring. Every shape must resolve to a VALUE.
    """

    from jasper.active_speaker.baseline_profile import profile_blend_correction

    for corrupt in [*_CORRUPT_PROFILE_SHAPES, "text", 7, {"a": 1}, None]:
        located = profile_blend_correction({"blend_correction": corrupt})
        resolved = (
            None if located is None
            else bc.blend_filters_from_mapping(list(located))
        )
        assert resolved is None or isinstance(resolved, tuple)


def test_a_well_formed_applied_profile_still_reads_through():
    """The positive control: the strict reader must not refuse a real record.

    Without this, "everything reads as unknown" would pass every assertion
    above while making the correction permanently unreachable.
    """

    from jasper.active_speaker.baseline_profile import profile_blend_correction

    good = [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -2.5}]
    located = profile_blend_correction({"blend_correction": good})
    assert bc.blend_filters_from_mapping(list(located)) == tuple(good)


# --------------------------------------------------------------------------- #
# ruling 1 — a refusal HOLDS the adopted incumbent, it does not revert it
# --------------------------------------------------------------------------- #


_INCUMBENT = (
    {"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -3.0},
    {"biquad_type": "Peaking", "freq": 2600.0, "q": 2.0, "gain": -2.0},
)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"graded": None, "band_hz": SERIES1_BAND_HZ},
         bc.BLEND_NOT_COMPARABLE),
        ({"graded": None, "band_hz": None},
         bc.BLEND_NO_TRUSTED_BAND),
    ],
    ids=["not_comparable", "no_trusted_band"],
)
def test_a_refusal_re_prescribes_the_incumbent_rather_than_reverting(
    kwargs, reason,
):
    """Panel ruling, 2026-08-18.

    Every refusal arm used to write ``[]``, which on the next apply silently
    removed up to the full composed ceiling of ADOPTED cut — a change to what
    a household hears, decided by an instrument that had just said it could
    not measure. A round whose evidence failed has no standing to remove a
    correction adopted on measured evidence.

    The hop asserted is the NEXT GRAPH — ``filters``, which is what the
    candidate build applies — not merely the reason code. The sibling
    re-prescribe path was already tested while these were not, which is the
    half-guarded-site pattern the lens named.
    """

    result = bc.solve_blend_correction(incumbent=_INCUMBENT, **kwargs)

    assert result.reason == reason
    assert result.filters == _INCUMBENT, "an adopted correction was reverted"
    assert result.incumbent == _INCUMBENT


def test_nothing_to_cut_holds_the_incumbent_too():
    """The arm that was already right, pinned beside the two that were not.

    The incumbent here is shallower than :data:`~.blend_correction.
    BLEND_MIN_CUT_DB`, which is what makes ``nothing_to_cut`` reachable WITH
    one: a deeper incumbent is re-derived by the fit (the prescription is a
    total, so re-emitting it is ``corrected``, not ``nothing_to_cut``), and
    only a correction too shallow to earn a filter leaves the fit empty. That
    is the case where the arm's hold has to carry the incumbent itself.
    """

    shallow = ({"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0,
                "gain": -0.3},)

    result = _solve(_bell(SERIES1_DIP_HZ, -4.3), incumbent=shallow)

    assert result.reason == bc.BLEND_NOTHING_TO_CUT
    assert result.filters == shallow


def test_only_an_unestablished_incumbent_prescribes_nothing():
    """The one arm that cannot hold, because it is the state of not knowing
    what to hold. Kept distinct from the three above so a future change that
    made refusals revert again cannot hide behind this one legitimately
    empty case."""

    result = bc.solve_blend_correction(
        graded=None, band_hz=SERIES1_BAND_HZ, incumbent=None,
    )

    assert result.reason == bc.BLEND_NO_INCUMBENT
    assert result.filters == ()


def test_every_reason_code_has_a_pinned_next_graph():
    """The completeness check behind the four tests above.

    A new arm added without a next-graph assertion is exactly the
    half-guarded-site shape this section exists to close, and an enumeration
    that is checked is the only thing that notices one.
    """

    pinned = {
        bc.BLEND_CORRECTED, bc.BLEND_NOTHING_TO_CUT, bc.BLEND_NOT_COMPARABLE,
        bc.BLEND_NO_TRUSTED_BAND, bc.BLEND_NO_INCUMBENT,
        bc.BLEND_REGION_NOT_IMPROVING,
    }
    declared = {
        getattr(bc, name) for name in dir(bc)
        if name.startswith("BLEND_") and isinstance(getattr(bc, name), str)
    }
    assert declared == pinned, (
        "a blend outcome exists with no test pinning what graph it leaves"
    )


# --------------------------------------------------------------------------- #
# malformed evidence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "graded",
    [
        None,
        GradedSpec(np.array([1.0]), np.array([1.0, 2.0]), np.array([False]), None),
        GradedSpec(_GRID, np.full_like(_GRID, np.nan), np.zeros_like(_GRID, bool),
                   SimpleNamespace(reference_db=0.0, reference_band_hz=(250., 8e3))),
        GradedSpec(_GRID, np.zeros_like(_GRID), np.zeros_like(_GRID, bool),
                   SimpleNamespace(reference_db=float("nan"),
                                   reference_band_hz=(250.0, 8000.0))),
        SimpleNamespace(freqs_hz=_GRID, curve_db=_GRID, excluded=None, report=None),
    ],
    ids=["absent", "shape-mismatch", "nan-curve", "nan-reference", "junk"],
)
def test_malformed_summed_evidence_fails_to_a_no_op_never_to_a_boost(graded):
    result = bc.solve_blend_correction(
        graded=graded, band_hz=SERIES1_BAND_HZ, incumbent=(),
    )

    assert result.filters == ()
    assert result.reason == bc.BLEND_NOT_COMPARABLE
    assert result.band_hz == SERIES1_BAND_HZ


# --------------------------------------------------------------------------- #
# the region reading, and the region-scoped benefit claim
# --------------------------------------------------------------------------- #


def test_the_region_residual_is_the_pooled_one_with_a_narrower_bin_set():
    """Not a second estimator — the same one, restricted.

    Asserted by widening the region to the whole graded span and requiring the
    two numbers to agree, which is a real equivalence rather than a
    recomputation of the production expression: one side comes from this
    module's arrays, the other from ``spec_convergence_residual``'s per-band
    records.

    **What the fixture has to do to make them comparable, stated rather than
    hidden**: the mask below also removes every bin outside the reference band.
    Without it the two grade different bin SETS — ``spec_convergence_residual``
    pools ``SPEC_BANDS[2]`` (8–16 kHz) as well, and the reference band stops at
    8 kHz — so they would differ for a reason that has nothing to do with the
    estimator. The equivalence being asserted is "same formula, same bins",
    and the extra masking is how the second half is arranged.
    """

    curve = _bell(1500.0, 3.0, q=4.0) + _bell(600.0, -2.0, q=3.0)
    # Both sides must grade the SAME bins for the equivalence to mean anything,
    # so the mask removes everything outside the reference band — which is
    # where the two would otherwise differ, since `spec_convergence_residual`
    # also pools SPEC_BANDS[2] (8-16 kHz) and the reference band stops at 8k.
    probe = _graded(curve)
    lo, hi = probe.report.reference_band_hz
    outside = (_GRID < lo) | (_GRID > hi)
    graded = _graded(curve, outside)

    result = bc.solve_blend_correction(
        graded=graded, band_hz=graded.report.reference_band_hz, incumbent=(),
    )
    pooled = spec_convergence_residual(graded.report)

    assert result.reading is not None
    assert pooled.rms_db is not None
    assert result.reading.residual_db == pytest.approx(pooled.rms_db, rel=1e-6)
    assert result.reading.n_bins == pooled.n_bins


def _comparand(curve_db: np.ndarray) -> MeasurementComparand:
    return MeasurementComparand(
        program_id="prog-1",
        reference_mark="design_axis",
        curve=ResponseCurve(_GRID, curve_db),
        exclusion_mask=[False] * len(_GRID),
    )


def test_a_localized_win_the_pooled_claim_cannot_see_grades_in_the_region():
    """#2600 §4: the axis was right and the granularity was not.

    Series-1 banked ``benefit=residual_within_margin`` on every round — the
    axis ran, compared, and correctly reported that a speaker which had not
    moved had not moved. What it CANNOT do is credit a win confined to two
    octaves, because it pools across six. This builds exactly that curve pair:
    a real improvement inside the blend region and nothing outside it.
    """

    # Content OUTSIDE the region, identical before and after, that dominates
    # the pooled residual — which is the series-1 situation, not a contrived
    # one: the pooled band runs 250 Hz-16 kHz and the blend spans two octaves
    # of it.
    elsewhere = _bell(350.0, 9.0, q=1.0) + _bell(11000.0, 9.0, q=1.0)
    before = elsewhere + _bell(1500.0, 5.0, q=6.0)
    after = elsewhere + _bell(1500.0, 0.4, q=6.0)

    pooled = evaluate_benefit(
        entry_baseline=_comparand(before), post=_comparand(after), margin_db=0.5,
    )
    region = evaluate_region_benefit(
        entry_baseline=_comparand(before), post=_comparand(after),
        band_hz=SERIES1_BAND_HZ, margin_db=0.5,
    )

    assert pooled.status is BenefitStatus.INDETERMINATE
    assert pooled.reason == BENEFIT_WITHIN_MARGIN
    assert region.status is BenefitStatus.IMPROVED
    assert region.reason == BENEFIT_IMPROVED


def test_the_region_claim_keeps_the_pooled_margin():
    """0.5 dB is the model's own measured tracking error. Narrowing a band does
    not sharpen an instrument, so the bar does not move with it."""

    before = _bell(1500.0, 1.0, q=3.0)
    after = _bell(1500.0, 0.85, q=3.0)

    region = evaluate_region_benefit(
        entry_baseline=_comparand(before), post=_comparand(after),
        band_hz=SERIES1_BAND_HZ, margin_db=0.5,
    )

    assert region.status is BenefitStatus.INDETERMINATE
    assert region.reason == BENEFIT_WITHIN_MARGIN


def test_the_region_claim_says_so_when_there_is_no_region():
    verdict = evaluate_region_benefit(
        entry_baseline=_comparand(np.zeros_like(_GRID)),
        post=_comparand(np.zeros_like(_GRID)),
        band_hz=None, margin_db=0.5,
    )

    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_NO_REGION_BAND


def test_the_region_claim_narrows_both_sides_so_comparability_survives():
    """``evaluate_benefit`` refuses a pair whose exclusion masks differ. The
    narrowing is applied identically to both, so that guarantee is preserved
    rather than forfeited — asserted by the verdict NOT being a mask
    mismatch."""

    verdict = evaluate_region_benefit(
        entry_baseline=_comparand(_bell(1500.0, 3.0, q=3.0)),
        post=_comparand(_bell(1500.0, 1.0, q=3.0)),
        band_hz=SERIES1_BAND_HZ, margin_db=0.5,
    )

    assert verdict.reason != "incomparable_exclusion_mask"
    assert verdict.status is not None


# --------------------------------------------------------------------------- #
# the narrow-defect stop (panel: correctness SF2)
# --------------------------------------------------------------------------- #


def _defect_series(defect_q: float, rounds: int, *, stop: bool) -> list[float]:
    """Region rms per round for a defect of ``defect_q``.

    ``stop=False`` drives the pre-fix loop — the previous reading withheld —
    so the guard below has the contrast that makes it a claim rather than a
    restatement.
    """

    underlying = _bell(1500.0, 4.0, q=defect_q)
    incumbent: tuple = ()
    previous: float | None = None
    residuals = []
    for _ in range(rounds):
        result = bc.solve_blend_correction(
            graded=_graded(underlying + _cascade_db(incumbent)),
            band_hz=SERIES1_BAND_HZ,
            incumbent=incumbent,
            previous_residual_db=previous if stop else None,
        )
        if result.reading is not None:
            previous = result.reading.residual_db
        incumbent = result.filters
        residuals.append(_region_rms(underlying + _cascade_db(incumbent)))
    return residuals


@pytest.mark.parametrize(
    ("defect_q", "settles_at"), [(3.0, 0.3439), (4.0, 0.7320), (6.0, 1.0094)],
)
def test_a_defect_narrower_than_the_filter_stops_instead_of_wandering(
    defect_q, settles_at,
):
    """A ``Q = 2`` cut cannot match a narrower defect, so each round's fit
    over-corrects the shoulders and the over-correction becomes next round's
    defect. Unstopped, that limit-cycles — at ``Q = 4`` the unstopped series
    alternates 0.493 ↔ 0.732 forever and never settles, while the stopped one
    holds 0.732.

    **The settle point is pinned to its value, not bounded.** A ``<= max(the
    unstopped series)`` assertion is satisfied by almost any behaviour,
    including a stop that fires immediately or never — it cannot distinguish
    the shipped rule from several wrong ones. These three numbers are what the
    rule actually produces, they are the ones the module docstring quotes, and
    a change to the stop moves them.

    Note ``Q = 6``: it settles at 1.009, ABOVE the 0.767 the unstopped
    alternation touches. That is the honest worst case and is asserted rather
    than avoided — the stop buys a stable value there, not a better one.
    """

    stopped = _defect_series(defect_q, 6, stop=True)
    wandering = _defect_series(defect_q, 6, stop=False)

    assert stopped[-1] == pytest.approx(settles_at, abs=5e-4), (
        f"the stop settled somewhere new: {stopped}"
    )
    assert stopped[-1] == pytest.approx(stopped[-2], abs=1e-9), (
        f"the region is still moving at round 6: {stopped}"
    )
    assert stopped != wandering, (
        "the stop changed nothing — this fixture no longer exercises it"
    )


def test_the_stop_does_not_fire_on_a_defect_the_loop_can_converge_on():
    """The positive control: a stop that always fired would pass every
    assertion above while destroying the convergence this module exists for."""

    stopped = _defect_series(bc.BLEND_FILTER_Q, 6, stop=True)

    assert stopped == sorted(stopped, reverse=True), f"not monotone: {stopped}"
    assert stopped[-1] < stopped[0] * 0.1, f"stop fired too early: {stopped}"


def test_the_stop_reports_its_own_arm_and_holds_the_incumbent():
    result = bc.solve_blend_correction(
        graded=_graded(_bell(1500.0, 4.0, q=6.0)),
        band_hz=SERIES1_BAND_HZ,
        incumbent=_INCUMBENT,
        previous_residual_db=0.0,
    )

    assert result.reason == bc.BLEND_REGION_NOT_IMPROVING
    assert result.filters == _INCUMBENT


def test_an_absent_previous_reading_keeps_the_loop_prescribing():
    """The stop's fail direction: absent evidence must never freeze a series."""

    hot = _bell(1500.0, 6.0, q=bc.BLEND_FILTER_Q)

    assert _solve(hot).reason == bc.BLEND_CORRECTED
    assert bc.solve_blend_correction(
        graded=_graded(hot), band_hz=SERIES1_BAND_HZ, incumbent=(),
        previous_residual_db=float("nan"),
    ).reason == bc.BLEND_CORRECTED


# --------------------------------------------------------------------------- #
# 15. the receipt
# --------------------------------------------------------------------------- #


def _blend_record(result: bc.BlendCorrection) -> dict:
    evaluation = SimpleNamespace(blend=result, region_benefit=None)
    evidence = SimpleNamespace(
        delta_probe=None, position_residuals=(), alignment_prescription=None,
        # Stated rather than left to a ``getattr`` in the reader: the
        # coordinator reads both provenance records as plain attributes, and
        # making only the newer one lenient would buy a stand-in's
        # convenience with an asymmetry in the code under test.
        topology_prescription=None,
    )
    return coordinator._round_measurements(evidence, evaluation)


def test_the_receipt_banks_the_regions_commanded_and_realized_pair():
    """Decision 11's deterministic-forever requirement, for this region.

    The pair is what makes the loop auditable no matter who eventually
    prescribes: what was asked for, what it was derived from, and what the
    incumbent actually achieved.
    """

    result = _solve(_bell(1500.0, 6.0, q=4.0))

    record = _blend_record(result)["blend"]

    assert record["reason"] == bc.BLEND_CORRECTED
    assert record["band_hz"] == list(SERIES1_BAND_HZ)
    assert record["damping"] == bc.BLEND_DAMPING
    assert record["commanded"] and all(
        entry["gain"] < 0.0 for entry in record["commanded"]
    )
    assert record["incumbent"] == []
    realized = record["realized"]
    assert realized["n_bins"] > 0
    assert math.isfinite(realized["residual_db"])
    assert math.isfinite(realized["worst_db"])
    assert math.isfinite(realized["worst_hz"])


def test_a_round_that_prescribed_nothing_banks_why():
    """"The region was already clean" and "the instrument refused" are
    different facts, and a reader with only the empty list cannot tell them
    apart."""

    clean = _blend_record(_solve(_bell(SERIES1_DIP_HZ, -4.3)))["blend"]
    refused = _blend_record(
        _solve(_bell(1500.0, 6.0, q=4.0), incumbent=None)
    )["blend"]

    assert clean["commanded"] == [] and refused["commanded"] == []
    assert clean["reason"] == bc.BLEND_NOTHING_TO_CUT
    assert refused["reason"] == bc.BLEND_NO_INCUMBENT
    assert clean["reason"] != refused["reason"]


def test_a_round_with_no_region_banks_no_blend_record_at_all():
    """The same rule the empty position-residual list follows: a zero-length
    record is a claim that the question was asked and answered."""

    no_region = bc.solve_blend_correction(
        graded=None, band_hz=None, incumbent=(),
    )

    assert "blend" not in _blend_record(no_region)
