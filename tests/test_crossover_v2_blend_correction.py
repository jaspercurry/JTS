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
from jasper.audio_measurement.program_analysis import (
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
        SERIES1_FC_HZ, trusted_floor_hz=100.0, radiated_band_hz=(100.0, 20000.0),
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

    assert round_evidence._crossover_region_band_hz(analysis) == SERIES1_BAND_HZ


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
    assert round_evidence._crossover_region_band_hz(analysis) is None


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

    Above ``active_baseline_headroom`` so any future boost posture is absorbed
    by the same common attenuation automatically; above the split mixer so the
    stage is upstream of every per-driver crossover, limiter, and the tweeter
    high-pass that is its only protection in the durable baseline.
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


def test_a_cuts_only_blend_correction_charges_no_headroom():
    """Its ``total_positive_boost_db`` is 0.0 by construction, so the common
    attenuation has nothing to absorb and the graph's gain staging is
    unchanged. A future boost posture would change this; today it must not."""

    plain = _emitted(None)
    with_blend = _emitted([
        {"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -3.0},
    ])

    def _headroom(yaml: str) -> str:
        block = yaml.split("active_baseline_headroom:", 1)[1]
        return block.split("gain:", 1)[1].splitlines()[0].strip()

    assert _headroom(plain) == _headroom(with_blend)


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
    monotone approach rather than as an exact value, because the prescription
    is refit to biquads every round and the refit is not the identity.
    """

    residuals = _series(5, incumbent_accounted=True)

    assert residuals == sorted(residuals, reverse=True), (
        f"not monotone: {residuals}"
    )
    assert residuals[-1] < residuals[0] * 0.1, (
        f"too slow to be convergence: {residuals}"
    )


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

    naive = _series(5, incumbent_accounted=False)
    shipped = _series(5, incumbent_accounted=True)

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
# 15. the receipt
# --------------------------------------------------------------------------- #


def _blend_record(result: bc.BlendCorrection) -> dict:
    evaluation = SimpleNamespace(blend=result, region_benefit=None)
    evidence = SimpleNamespace(
        delta_probe=None, position_residuals=(),
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
