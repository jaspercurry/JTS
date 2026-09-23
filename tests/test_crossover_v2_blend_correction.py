# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Decision 10's blend-region correction: its region, the emitter's clamps and
the persisted-incumbent reader.

The bounds and the refusals are the safety argument of a stage that emits
biquads into the audio path, so each is pinned here against a mutation rather
than against a restatement of the production expression.
"""

from __future__ import annotations


import pytest

from jasper.active_speaker import camilla_yaml
from jasper.active_speaker.camilla_yaml import (
    ActiveSpeakerConfigError,
    emit_active_speaker_baseline_config,
)
from jasper.active_speaker.crossover_v2 import blend_correction as bc
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


# --------------------------------------------------------------------------- #
# 6-10. the clamps
# --------------------------------------------------------------------------- #


def test_the_bounds_are_the_numbers_the_evidence_earned():
    """The bounds, as LITERALS — this tests the policy, so reading the
    constant would make it a tautology that passes at any value.

    * **3.0 dB per filter** — the woofer's own acknowledged
      ``measured_excess_db`` inside the series-1 blind zone (2.09-2.26 dB,
      rounds r1/r2/r4 over 1291.4-2077.2 Hz) plus one model tracking error
      (0.5 dB): 2.26 + 0.5 = 2.76, rounded up.
    * **2 filters** — what the evidence in this region supports, given one mono
      sweep per position and a null detector that is uncalibrated across the
      entire blend window of any crossover below 4 kHz (#2600 item 1).
    * **0.5 dB floor** — this model's own measured tracking error. Below it, a
      correction is not something that can be honestly claimed.
    * **Q 2.0** — a deliberate tightening against the fit engine's ``Q <= 8``
      for cuts, and the Q every peaking filter the series-1 fits emitted used.
    """

    assert bc.BLEND_MAX_FILTER_CUT_DB == 3.0
    assert bc.BLEND_MAX_FILTERS == 2
    assert bc.BLEND_MIN_CUT_DB == 0.5
    assert bc.BLEND_FILTER_Q == 2.0


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
    """Every corrupt shape resolves to a VALUE, never a raise."""

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
