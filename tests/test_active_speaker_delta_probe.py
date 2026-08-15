# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Delta-probe verification (linearization-integrity PR-L5).

Synthetic-fixture style, mirroring ``test_active_speaker_linearization_fit``:
each realized/commanded pair is built from a closed-form shape so the expected
verdict is derivable rather than corpus-replayed. The keystone is
``test_the_shelf_q_realization_error_class_is_caught``, which reproduces the
2026-07-27 defect's own magnitude (a shelf modelled at Q 0.707 and realized at
Q 0.476) and proves the probe refuses it — the bug class this module exists to
catch permanently.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_HF_SPLIT_HZ,
    DELTA_PROBE_MIN_BINS,
    DELTA_PROBE_MIN_COMMANDED_DB,
    DELTA_PROBE_MIN_COMMANDED_HIGH_DB,
    DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES,
    DELTA_PROBE_MIN_QUIET_COVERAGE,
    DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB,
    DELTA_PROBE_ROLLBACK_VERDICTS,
    DELTA_PROBE_SHORTFALL_GAIN_CEILING,
    DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
    DELTA_PROBE_TOLERANCE_HIGH_DB,
    DELTA_PROBE_TOLERANCE_LOW_DB,
    DELTA_PROBE_VERDICTS,
    REASON_UNCOMMANDED_LEVEL_SHIFT,
    REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND,
    SPATIAL_COST_UNAVAILABLE,
    VERDICT_FRAME_MISMATCH,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED,
    VERDICT_MODEL_ERROR,
    VERDICT_SPATIALLY_COSTLY,
    VERDICT_UNAVAILABLE,
    boost_overshoot,
    classify_delta_probe,
    evaluate_spatial_cost,
    graded_command_floor_db,
    interquartile_band_hz,
    spatial_cost_from_group_spreads,
    widest_exceedance_octaves,
)

_GRID_HZ = np.logspace(math.log10(100.0), math.log10(20_000.0), 400)


def _commanded_lift(depth_db: float = 8.0, corner_hz: float = 5_000.0) -> np.ndarray:
    """A commanded top-end lift: 0 below the corner, rising to ``depth_db``."""
    x = np.clip(np.log2(_GRID_HZ / corner_hz), 0.0, 1.5) / 1.5
    return depth_db * x


def _band() -> tuple[float, float]:
    return float(_GRID_HZ[0]), float(_GRID_HZ[-1])


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #


def test_every_verdict_a_classification_can_return_is_enumerated():
    """A new classification path must not ship an un-enumerated verdict string
    — the conductor's reason mapping and the rollback set are both keyed on
    this vocabulary."""
    commanded = _commanded_lift()
    seen = set()
    for realized in (
        commanded,                                  # matched
        commanded * 0.4,                            # shortfall
        commanded + 6.0 * np.sin(np.log2(_GRID_HZ)),  # model error
        np.zeros_like(commanded),                   # nothing commanded case
    ):
        seen.add(
            classify_delta_probe(
                _GRID_HZ, realized, commanded, band_hz=_band()
            ).verdict
        )
    assert seen <= DELTA_PROBE_VERDICTS


def test_rollback_verdicts_are_exactly_the_non_matched_measurable_ones():
    """``unavailable`` is deliberately NOT a rollback: an absent measurement is
    not evidence of a bad correction, and rolling back on it would revert every
    session whose household closed the phone before the post-apply sweep.

    ``level_mismatch`` is not one either (#1811): it is a finding about this
    comparison's LEVEL AXIS, not about the correction's shape, and its most
    likely production cause is a known incompleteness in our own offset
    accounting (an applied crossover config is emitted without room-PEQ /
    preference EQ). Reverting a household's correction because our bookkeeping
    was short would be a false accusation against a correction that may be
    perfect.
    """
    assert DELTA_PROBE_ROLLBACK_VERDICTS == {
        VERDICT_MODEL_ERROR,
        VERDICT_LEVEL_DEPENDENT_SHORTFALL,
        VERDICT_SPATIALLY_COSTLY,
    }
    assert VERDICT_MATCHED not in DELTA_PROBE_ROLLBACK_VERDICTS
    assert VERDICT_UNAVAILABLE not in DELTA_PROBE_ROLLBACK_VERDICTS
    assert VERDICT_LEVEL_MISMATCH not in DELTA_PROBE_ROLLBACK_VERDICTS
    assert VERDICT_LEVEL_MISMATCH in DELTA_PROBE_VERDICTS


def test_rollback_flag_is_derived_from_the_verdict_not_set_by_a_caller():
    commanded = _commanded_lift()
    matched = classify_delta_probe(
        _GRID_HZ, commanded, commanded, band_hz=_band()
    )
    assert matched.matched is True
    assert matched.rollback is False
    broken = classify_delta_probe(
        _GRID_HZ, commanded + 6.0 * np.sin(np.log2(_GRID_HZ)), commanded,
        band_hz=_band(),
    )
    assert broken.rollback is (broken.verdict in DELTA_PROBE_ROLLBACK_VERDICTS)


# --------------------------------------------------------------------------- #
# the at-the-mark map
# --------------------------------------------------------------------------- #


def test_an_exactly_realized_correction_matches():
    commanded = _commanded_lift()
    probe = classify_delta_probe(_GRID_HZ, commanded, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MATCHED
    assert probe.max_error_db == pytest.approx(0.0, abs=1e-9)
    assert probe.gain_factor == pytest.approx(1.0, abs=1e-9)
    assert probe.exceedance_octaves == 0.0


def test_a_correction_commanding_nothing_is_unavailable_not_a_pass():
    """The probe band is where the correction ASKED for something. With
    nothing asked, there is nothing to verify — and that is reported as
    unknown, never as permission."""
    probe = classify_delta_probe(
        _GRID_HZ, np.zeros_like(_GRID_HZ), np.zeros_like(_GRID_HZ),
        band_hz=_band(),
    )
    assert probe.verdict == VERDICT_UNAVAILABLE
    assert probe.reason == "nothing_commanded"
    assert probe.rollback is False


def test_the_probe_band_excludes_bins_below_the_commanded_floor():
    """A bin the correction barely touches carries no claim about the chain,
    so it must not be able to fail the probe."""
    commanded = np.zeros_like(_GRID_HZ)
    commanded[_GRID_HZ > 5_000.0] = 6.0
    realized = commanded.copy()
    # A large error where NOTHING was commanded.
    realized[_GRID_HZ < 1_000.0] += 20.0
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MATCHED
    assert probe.probe_band_hz[0] > 1_000.0


def test_a_single_bin_excursion_is_texture_not_a_finding():
    """The width rule: an isolated spike at the smoothing scale is measurement
    texture, and the same argument ``HF_REALIZATION_TOLERANCE_DB`` records."""
    commanded = _commanded_lift()
    realized = commanded.copy()
    spike = int(np.flatnonzero(commanded >= DELTA_PROBE_MIN_COMMANDED_DB)[10])
    realized[spike] += 6.0
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MATCHED
    # …and it was genuinely over tolerance: the width rule is what acquitted
    # it, not the amplitude one.
    assert probe.max_error_db > DELTA_PROBE_TOLERANCE_LOW_DB
    assert probe.exceedance_octaves < DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


def test_a_wide_shape_error_is_a_model_error():
    commanded = _commanded_lift()
    realized = commanded + np.where(_GRID_HZ > 6_000.0, 4.0, -4.0)
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.reason == "realized_shape_differs_from_commanded"
    assert probe.exceedance_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


def test_a_proportional_undershoot_of_a_lift_is_a_level_shortfall():
    """Shape tracks, depth does not, and what was asked for was LEVEL — the
    driver compression diagnostic.

    ``gain_factor`` is read at 1e-3 rather than 1e-6 since #2521, and the slack
    is a known quantity rather than a fudge: the quiet bins the frame is fitted
    over admit commands up to ``DELTA_PROBE_MIN_COMMANDED_DB``, so a shortfall
    that scales those small commands too puts a trace of itself into the fitted
    tilt (measured: −0.002 dB/octave here, moving the gain by 3e-4). It is the
    same bounded bias ``residual_offset_db``'s own docstring already discloses
    for the offset term, one derivative up.
    """
    commanded = _commanded_lift(depth_db=10.0)
    probe = classify_delta_probe(
        _GRID_HZ, commanded * 0.5, commanded, band_hz=_band(),
    )
    assert probe.verdict == VERDICT_LEVEL_DEPENDENT_SHORTFALL
    assert probe.reason == "realized_short_of_commanded"
    assert probe.gain_factor == pytest.approx(0.5, abs=1e-3)
    assert probe.gain_factor < DELTA_PROBE_SHORTFALL_GAIN_CEILING


def test_a_proportional_undershoot_of_CUTS_is_a_model_error_not_compression():
    """Attenuation does not compress. A uniform shortfall on a set of cuts is
    a claim about the filter math, and belongs where someone will look at it."""
    commanded = -_commanded_lift(depth_db=10.0)
    probe = classify_delta_probe(
        _GRID_HZ, commanded * 0.5, commanded, band_hz=_band(),
    )
    assert probe.verdict == VERDICT_MODEL_ERROR


def test_an_overshoot_with_perfect_shape_is_a_model_error_not_a_shortfall():
    commanded = _commanded_lift(depth_db=10.0)
    probe = classify_delta_probe(
        _GRID_HZ, commanded * 1.5, commanded, band_hz=_band(),
    )
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.gain_factor > 1.0


def test_the_shelf_q_realization_error_class_is_caught():
    """**The keystone.** The 2026-07-27 defect, built from the real evaluator
    rather than sketched: a −11 dB Highshelf the fit MODELLED at Butterworth
    Q (0.7071) and CamillaDSP REALIZED at Q 0.476, because the emitter wrote
    ``slope: 6`` believing that was Butterworth (PR-L2).

    Every gate inside the fit engine evaluated the same wrong model and scored
    it exact — a model cannot audit itself. This probe measures the speaker
    instead, and refuses. It is also the case VERIFY's tracking check
    structurally cannot see: the error lives at 5–12 kHz, an octave and a half
    above the ``[Fc/2, 2·Fc]`` window that comparator gates on.
    """
    from jasper.active_speaker.linearization_fit import (
        _HIGHSHELF_Q, _highshelf_response_db,
    )

    corner_hz, gain_db = 7_000.0, -11.0
    commanded = _highshelf_response_db(_GRID_HZ, corner_hz, gain_db, _HIGHSHELF_Q)
    realized = _highshelf_response_db(_GRID_HZ, corner_hz, gain_db, 0.476)
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.rollback is True
    assert probe.verdict == VERDICT_MODEL_ERROR
    # The magnitude the forensics measured: max pointwise error ~1.70 dB.
    assert probe.max_error_db == pytest.approx(1.70, abs=0.15)
    # …and it is a WIDE systematic tilt, which is why a tolerance only 0.2 dB
    # under the peak error still catches it comfortably.
    assert probe.exceedance_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


# --------------------------------------------------------------------------- #
# the apply-boundary level offset (#1811)
# --------------------------------------------------------------------------- #
#
# The applied graph absorbs its correction's boost as a pre-split common
# attenuation, so every post-apply capture sits that many dB below the
# prediction it is compared against. That attenuation is the excitation-safety
# property and is NOT compensated at the speaker; it is declared to this
# module, which removes it before classifying.

# The charge the live session that surfaced this actually carried.
_LIVE_APPLY_OFFSET_DB = -22.458


def test_a_known_apply_offset_is_removed_before_classification():
    """(a) A post-apply curve carrying exactly the declared offset MATCHES.

    The contrast is the point. Undeclared, the identical curve reaches
    ``level_mismatch``: the probe can see that the level moved (the quiet bins
    say so) but cannot say the correction is good — and, critically, no longer
    rolls a healthy correction back for it, which is what this module did to
    the live session. Declared, the same evidence resolves to a clean pass.
    """
    commanded = _commanded_lift()
    realized = commanded + _LIVE_APPLY_OFFSET_DB

    blind = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert blind.verdict == VERDICT_LEVEL_MISMATCH
    assert blind.rollback is False
    assert blind.residual_offset_db == pytest.approx(_LIVE_APPLY_OFFSET_DB)

    aware = classify_delta_probe(
        _GRID_HZ, realized, commanded, band_hz=_band(),
        expected_offset_db=_LIVE_APPLY_OFFSET_DB,
    )
    assert aware.verdict == VERDICT_MATCHED
    assert aware.rollback is False
    assert aware.max_error_db == pytest.approx(0.0, abs=1e-9)
    assert aware.gain_factor == pytest.approx(1.0, abs=1e-9)
    # Both halves of the accounting are on the record.
    assert aware.expected_offset_db == pytest.approx(_LIVE_APPLY_OFFSET_DB)
    assert aware.residual_offset_db == pytest.approx(0.0, abs=1e-9)


def test_an_unknown_extra_offset_is_its_own_finding_not_a_model_error():
    """(b) A level shift BEYOND what the emitter declared is named, not
    absorbed and not misfiled — something moved that nobody commanded."""
    commanded = _commanded_lift()
    extra_db = -4.0
    realized = commanded + _LIVE_APPLY_OFFSET_DB + extra_db

    probe = classify_delta_probe(
        _GRID_HZ, realized, commanded, band_hz=_band(),
        expected_offset_db=_LIVE_APPLY_OFFSET_DB,
    )
    assert probe.verdict == VERDICT_LEVEL_MISMATCH
    assert probe.reason == "uncommanded_level_shift"
    # Named, but not an accusation against the correction.
    assert probe.rollback is False
    assert probe.residual_offset_db == pytest.approx(extra_db, abs=1e-9)


def test_an_unknown_offset_inside_tolerance_still_matches():
    """The magnitude bar means something: a residual smaller than the per-bin
    tolerance cannot by itself have pushed any bin past that tolerance, so it
    is disclosed and the map still matches."""
    commanded = _commanded_lift()
    small_db = -(DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB - 0.5)
    probe = classify_delta_probe(
        _GRID_HZ, commanded + small_db, commanded, band_hz=_band(),
    )
    assert probe.verdict == VERDICT_MATCHED
    assert probe.residual_offset_db == pytest.approx(small_db, abs=1e-9)


def test_the_shelf_q_keystone_still_classifies_under_a_known_offset():
    """(c) The defect this whole module exists for must survive the new
    arithmetic: the 2026-07-27 shelf-Q realization error, measured through a
    chain that also carries the live session's 22.458 dB apply charge, is
    still a model error at the same magnitude.
    """
    from jasper.active_speaker.linearization_fit import (
        _HIGHSHELF_Q, _highshelf_response_db,
    )

    corner_hz, gain_db = 7_000.0, -11.0
    commanded = _highshelf_response_db(_GRID_HZ, corner_hz, gain_db, _HIGHSHELF_Q)
    realized = _highshelf_response_db(_GRID_HZ, corner_hz, gain_db, 0.476)
    probe = classify_delta_probe(
        _GRID_HZ, realized + _LIVE_APPLY_OFFSET_DB, commanded, band_hz=_band(),
        expected_offset_db=_LIVE_APPLY_OFFSET_DB,
    )
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.rollback is True
    assert probe.max_error_db == pytest.approx(1.70, abs=0.15)
    assert probe.exceedance_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


def test_a_proportional_shortfall_is_not_stolen_by_the_level_verdict():
    """Measuring the residual in the QUIET bins is what protects this.

    A shortfall's error inside the probe band is ``(g−1)·commanded``, which has
    a large mean — a magnitude test taken there would have filed it as a level
    shift and lost the compression diagnostic entirely. Outside the probe band
    it scales a command that is zero, so it moves nothing: the residual is
    ~0 dB and the level verdict never engages.
    """
    commanded = _commanded_lift()
    probe = classify_delta_probe(
        _GRID_HZ, commanded * 0.4, commanded, band_hz=_band(),
    )
    assert abs(probe.residual_offset_db) < DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB
    assert probe.verdict == VERDICT_LEVEL_DEPENDENT_SHORTFALL


def test_an_overshoot_confined_to_the_commanded_region_is_still_a_model_error():
    """The other half of that argument, from the opposite direction.

    A correction that overshoots uniformly ACROSS ITS WHOLE commanded region
    looks flat in every probe bin — indistinguishable from a level shift if you
    only look there. The quiet bins settle it: nothing moved where nothing was
    asked, so this is the chain doing the wrong thing, not the level drifting.
    """
    commanded = np.zeros_like(_GRID_HZ)
    in_region = (_GRID_HZ >= 6_000.0) & (_GRID_HZ <= 16_000.0)
    commanded[in_region] = 5.0
    realized = commanded + np.where(in_region, 4.0, 0.0)
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.residual_offset_db == pytest.approx(0.0, abs=1e-9)
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.rollback is True


def test_a_residual_that_cannot_be_measured_is_reported_as_not_measured():
    """No quiet bins ⇒ ``None``, never 0.0.

    A correction that commands something across the entire band leaves nowhere
    to observe an uncommanded level, so the level verdict cannot engage and the
    record must say the number is absent rather than claim a measured zero —
    the same distinction ``gain_factor`` draws on an unavailable map.
    """
    commanded = np.full_like(_GRID_HZ, 6.0)
    probe = classify_delta_probe(
        _GRID_HZ, commanded + 4.0, commanded, band_hz=_band(),
    )
    assert probe.residual_offset_db is None
    assert probe.verdict != VERDICT_LEVEL_MISMATCH
    assert probe.to_dict()["residual_offset_db"] is None


def test_a_verdict_reached_without_the_level_check_says_so():
    """SF3: when there are no quiet bins the level discriminator never ran, so
    the verdict below it — a ROLLBACK, in the model_error case — was decided
    without the check that separates "the level moved" from "the shape is
    wrong". The reason string is what a reader has in front of them, so it is
    where that has to be said.
    """
    commanded = np.full_like(_GRID_HZ, 6.0)
    blind = classify_delta_probe(
        _GRID_HZ, commanded + 4.0, commanded, band_hz=_band(),
    )
    assert blind.residual_offset_db is None
    assert blind.rollback is True
    assert blind.reason.endswith("|level_check_unavailable")
    assert blind.reason.startswith("realized_shape_differs_from_commanded")

    # …and a verdict that DID have the discriminator carries no such caveat.
    lift = _commanded_lift()
    seen = classify_delta_probe(
        _GRID_HZ, lift + 6.0 * np.sin(np.log2(_GRID_HZ)), lift, band_hz=_band(),
    )
    assert seen.residual_offset_db is not None
    assert "level_check_unavailable" not in seen.reason


def test_a_non_finite_offset_is_nothing_known_not_a_crash():
    """"Nothing known" is 0.0, which leaves the whole shift visible in the
    residual rather than pretending it was accounted for."""
    commanded = _commanded_lift()
    for bad in (float("nan"), float("inf")):
        probe = classify_delta_probe(
            _GRID_HZ, commanded + _LIVE_APPLY_OFFSET_DB, commanded,
            band_hz=_band(), expected_offset_db=bad,
        )
        assert probe.expected_offset_db == 0.0
        assert probe.residual_offset_db == pytest.approx(
            _LIVE_APPLY_OFFSET_DB, abs=1e-9
        )


def test_the_residual_offset_tolerance_agrees_with_the_per_bin_tolerance():
    """Numerically equal, defined separately: the same "material" bar asked of
    a different quantity. A smaller bar here would name a shift that explains
    nothing; a larger one would let a real shift ride inside a shape verdict.
    """
    assert DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB == DELTA_PROBE_TOLERANCE_LOW_DB


def test_the_hf_tolerance_tier_tolerates_what_the_fit_engine_already_accepts():
    """A 2.0 dB error above 10 kHz is inside the spread the fit's own
    repeat-agreement gate accepts between two sweeps of the same driver
    (``HF_AGREEMENT_LIMIT_HIGH_DB``), so it must not fabricate a rollback."""
    from jasper.active_speaker.linearization_fit import HF_AGREEMENT_LIMIT_HIGH_DB

    assert DELTA_PROBE_TOLERANCE_HIGH_DB > HF_AGREEMENT_LIMIT_HIGH_DB
    commanded = np.full_like(_GRID_HZ, 4.0)
    realized = commanded + np.where(_GRID_HZ >= DELTA_PROBE_HF_SPLIT_HZ, 2.0, 0.0)
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MATCHED


def test_the_low_tolerance_sits_under_the_defect_it_must_catch():
    """1.5 dB has to be below the 1.70 dB the shelf-Q bug peaked at, or the
    keystone above would be a coincidence."""
    assert DELTA_PROBE_TOLERANCE_LOW_DB < 1.70


def test_a_mismatched_grid_is_unavailable_not_a_crash():
    probe = classify_delta_probe(
        _GRID_HZ, _GRID_HZ[:-1], _GRID_HZ, band_hz=_band(),
    )
    assert probe.verdict == VERDICT_UNAVAILABLE
    assert probe.reason == "grid_mismatch"


# --------------------------------------------------------------------------- #
# exceedance width
# --------------------------------------------------------------------------- #


def test_widest_exceedance_measures_index_contiguous_runs_only():
    """Two exceeding bins either side of a compliant one are two runs — which
    is the entire point of a width rule."""
    freqs = np.array([100.0, 200.0, 400.0, 800.0, 1600.0])
    split = np.array([True, True, False, True, True])
    width, lo_hz = widest_exceedance_octaves(freqs, split)
    assert width == pytest.approx(1.0)
    assert lo_hz in (100.0, 800.0)
    joined = np.array([True, True, True, True, True])
    assert widest_exceedance_octaves(freqs, joined)[0] == pytest.approx(4.0)


def test_widest_exceedance_of_nothing_is_zero():
    assert widest_exceedance_octaves(
        _GRID_HZ, np.zeros_like(_GRID_HZ, dtype=bool)
    ) == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# the spatial arm
# --------------------------------------------------------------------------- #


class _Band:
    def __init__(self, center_hz: float, sigma_db: float) -> None:
        self.center_hz = center_hz
        self.sigma_db = sigma_db


def test_spatial_cost_flags_a_widened_spread():
    before = [_Band(1000.0, 1.0), _Band(2000.0, 1.2)]
    after = [_Band(1000.0, 1.1), _Band(2000.0, 3.0)]
    cost = evaluate_spatial_cost(before, after)
    assert cost.available is True
    assert cost.widened is True
    assert cost.worst_center_hz == 2000.0
    assert cost.worst_widening_db == pytest.approx(1.8)


def test_spatial_cost_accepts_a_narrowed_spread():
    """A correction that makes the room MORE even is the good outcome, and
    must never read as costly."""
    before = [_Band(1000.0, 3.0)]
    after = [_Band(1000.0, 1.0)]
    cost = evaluate_spatial_cost(before, after)
    assert cost.widened is False
    assert cost.worst_widening_db == pytest.approx(-2.0)


def test_spatial_cost_pairs_bands_by_centre_and_skips_unmatched():
    before = [_Band(1000.0, 1.0)]
    after = [_Band(1000.0, 1.1), _Band(8000.0, 9.0)]
    cost = evaluate_spatial_cost(before, after)
    assert cost.n_bands == 1
    assert cost.widened is False


def test_spatial_cost_is_unavailable_without_both_groups():
    assert evaluate_spatial_cost([], [_Band(1000.0, 1.0)]).available is False
    assert spatial_cost_from_group_spreads(None, None).available is False
    assert spatial_cost_from_group_spreads(
        {"band_spread": []}, {"band_spread": [{"center_hz": 1.0, "sigma_db": 1.0}]},
    ).available is False


def test_spatial_cost_reads_json_round_tripped_bands():
    cost = spatial_cost_from_group_spreads(
        {"band_spread": [{"center_hz": 1000.0, "sigma_db": 1.0}]},
        {"band_spread": [{"center_hz": 1000.0, "sigma_db": 4.0}]},
    )
    assert cost.available is True
    assert cost.widened is True
    assert cost.tolerance_db == DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB


def test_a_matched_mark_with_a_widened_room_is_spatially_costly():
    """The verdict that is otherwise invisible: the correction did exactly what
    it was asked at the mark, and the room got less even for it."""
    commanded = _commanded_lift()
    probe = classify_delta_probe(
        _GRID_HZ, commanded, commanded, band_hz=_band(),
        spatial=evaluate_spatial_cost(
            [_Band(1000.0, 1.0)], [_Band(1000.0, 4.0)],
        ),
    )
    assert probe.verdict == VERDICT_SPATIALLY_COSTLY
    assert probe.reason == "cross_position_spread_widened"
    assert probe.rollback is True


def test_a_chain_defect_outranks_the_spatial_arm():
    """Priority, stated in the module docstring: a map that does not match at
    the mark is diagnosed as the chain defect it is, because that is the more
    proximate cause and the more actionable remedy. The spatial evidence still
    travels in the record."""
    commanded = _commanded_lift()
    realized = commanded + np.where(_GRID_HZ > 6_000.0, 4.0, -4.0)
    probe = classify_delta_probe(
        _GRID_HZ, realized, commanded, band_hz=_band(),
        spatial=evaluate_spatial_cost(
            [_Band(1000.0, 1.0)], [_Band(1000.0, 9.0)],
        ),
    )
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.spatial.widened is True  # recorded, not the verdict


def test_an_unavailable_spatial_arm_cannot_produce_a_costly_verdict():
    commanded = _commanded_lift()
    probe = classify_delta_probe(
        _GRID_HZ, commanded, commanded, band_hz=_band(),
        spatial=SPATIAL_COST_UNAVAILABLE,
    )
    assert probe.verdict == VERDICT_MATCHED


# --------------------------------------------------------------------------- #
# serialization
# --------------------------------------------------------------------------- #


def test_to_dict_carries_the_thresholds_it_judged_against():
    """A ledger row that reports a verdict without the tolerance behind it is
    unreadable six months later."""
    commanded = _commanded_lift()
    payload = classify_delta_probe(
        _GRID_HZ, commanded, commanded, band_hz=_band(),
    ).to_dict()
    assert payload["tolerance_low_db"] == DELTA_PROBE_TOLERANCE_LOW_DB
    assert payload["tolerance_high_db"] == DELTA_PROBE_TOLERANCE_HIGH_DB
    assert payload["verdict"] == VERDICT_MATCHED
    assert payload["rollback"] is False
    # #1811: the level axis is part of the record too — every scalar above was
    # measured AFTER removing ``expected_offset_db``, so a reader cannot judge
    # them without knowing what was removed and what was left.
    assert payload["expected_offset_db"] == 0.0
    assert payload["residual_offset_db"] == pytest.approx(0.0, abs=1e-9)
    assert (
        payload["residual_offset_tolerance_db"]
        == DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB
    )
    assert set(payload["spatial"]) == {
        "available", "widened", "worst_center_hz", "worst_widening_db",
        "tolerance_db", "n_bands",
    }


def test_the_commanded_floor_is_the_fit_engines_own_cosmetic_floor():
    """One definition of "this filter does nothing" across the fit and its
    verification."""
    from jasper.active_speaker.linearization_fit import _MIN_FILTER_GAIN_DB

    assert DELTA_PROBE_MIN_COMMANDED_DB == _MIN_FILTER_GAIN_DB


# --------------------------------------------------------------------------- #
# adversarial-review regressions (round 2)
# --------------------------------------------------------------------------- #


def test_a_sub_floor_gap_breaks_the_exceedance_run():
    """**B1 regression — the blocker, at its reproduction.**

    An ordinary correction cuts low and boosts high, so the commanded floor
    puts a HOLE in the probe mask between them. Measuring the exceedance width
    on the compacted ``freqs[mask]`` subarray welded the bins either side of
    that hole into one run: two isolated single-bin 2.5 dB errors, 135 grid
    bins and nearly three octaves apart in Hz, scored 2.931 octaves of
    "structure" and rolled the correction back.

    Each of those errors is one bin wide — precisely what
    ``test_a_single_bin_excursion_is_texture_not_a_finding`` declares to be
    texture. Evaluating the run on the full grid keeps every removed bin as a
    run-breaker, which is what it physically is.
    """
    commanded = np.zeros_like(_GRID_HZ)
    commanded[(_GRID_HZ >= 200.0) & (_GRID_HZ <= 800.0)] = -4.0
    commanded[(_GRID_HZ >= 6_000.0) & (_GRID_HZ <= 16_000.0)] = 5.0
    probe_bins = np.flatnonzero(np.abs(commanded) >= DELTA_PROBE_MIN_COMMANDED_DB)
    # One isolated bin at each edge of the sub-floor gap: the last bin of the
    # cut region and the first bin of the boost region.
    gap = int(np.flatnonzero(np.diff(probe_bins) != 1)[0])
    low_edge = int(probe_bins[gap])
    high_edge = int(probe_bins[gap + 1])
    realized = commanded.copy()
    realized[low_edge] += 2.5
    realized[high_edge] += 2.5
    assert high_edge - low_edge > 100, "the two errors must straddle the gap"

    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MATCHED
    assert probe.rollback is False
    assert probe.exceedance_octaves < DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


def test_a_wide_error_inside_one_masked_region_is_still_caught():
    """The control for the test above: closing the false-positive must not
    close the true one. A genuinely wide run INSIDE a single contiguous probe
    region still reads as structure."""
    commanded = np.zeros_like(_GRID_HZ)
    commanded[(_GRID_HZ >= 6_000.0) & (_GRID_HZ <= 16_000.0)] = 5.0
    realized = commanded + np.where(
        (_GRID_HZ >= 6_000.0) & (_GRID_HZ <= 16_000.0), 4.0, 0.0
    )
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.rollback is True
    assert probe.exceedance_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


def test_exceedance_is_measured_on_the_full_grid_not_the_masked_subarray():
    """The mechanism, pinned directly: ``_structured_exceedance`` takes the
    full grid plus a mask, so a caller cannot reintroduce B1 by compacting."""
    from jasper.active_speaker.delta_probe import _structured_exceedance

    freqs = np.array([100.0, 200.0, 400.0, 800.0, 1600.0, 3200.0])
    error = np.array([3.0, 0.0, 0.0, 0.0, 0.0, 3.0])
    tolerance = np.full_like(freqs, 1.0)
    # Both exceeding bins are in the probe band but are not adjacent.
    mask = np.ones_like(freqs, dtype=bool)
    found, width = _structured_exceedance(freqs, error, tolerance, mask)
    assert width == 0.0 and found is False
    # A mask hole cannot join two runs either.
    mask_holed = np.array([True, False, False, False, False, True])
    assert _structured_exceedance(freqs, error, tolerance, mask_holed)[1] == 0.0


def test_an_unavailable_map_reports_no_gain_factor():
    """**N8.** 0.0 would read as the measured claim "delivered nothing", which
    is the opposite of "not measured"."""
    probe = classify_delta_probe(
        _GRID_HZ, np.zeros_like(_GRID_HZ), np.zeros_like(_GRID_HZ),
        band_hz=_band(),
    )
    assert probe.verdict == VERDICT_UNAVAILABLE
    assert probe.gain_factor is None
    assert probe.to_dict()["gain_factor"] is None


# --- derivation pins (N3, N5) ---------------------------------------------


def test_the_low_tolerance_is_the_flows_own_measured_vs_predicted_bar():
    """**N3.** The probe and the tracking check must not hold one chain to two
    different standards in two different bands — so the constant is pinned to
    the flow's, not merely documented as equal to it."""
    from jasper.active_speaker.crossover_v2_flow import VERIFY_TOLERANCE_DB

    assert DELTA_PROBE_TOLERANCE_LOW_DB == VERIFY_TOLERANCE_DB


def test_the_hf_split_matches_the_fit_engines_own_tier_split():
    """**N3.** "High frequencies" means one thing across the fit and its
    verification."""
    from jasper.active_speaker.linearization_fit import _HF_AGREEMENT_TIER_SPLIT_HZ

    assert DELTA_PROBE_HF_SPLIT_HZ == _HF_AGREEMENT_TIER_SPLIT_HZ


def test_the_shortfall_ceiling_agrees_with_the_low_tolerance_about_material():
    """**N5.** 0.85 is not a taste: 15% short on the ~10 dB lift a CD-horn
    continuation commands is 1.5 dB, exactly the low-band tolerance. So a
    shortfall large enough to be NAMED is always large enough to have failed
    the amplitude test that reaches that branch, and the two constants cannot
    disagree about a correction of that size."""
    from jasper.active_speaker.linearization_fit import HF_SINGLE_SHELF_SPEND_CAP_DB

    commanded_depth_db = HF_SINGLE_SHELF_SPEND_CAP_DB  # 11.0, the realizable lift
    shortfall_at_ceiling_db = (
        1.0 - DELTA_PROBE_SHORTFALL_GAIN_CEILING
    ) * commanded_depth_db
    assert shortfall_at_ceiling_db >= DELTA_PROBE_TOLERANCE_LOW_DB


def test_the_spread_widening_tolerance_is_pinned_to_its_stated_value():
    """**N5.** The literal, with its rationale — like the N3 pins, and NOT a
    relation that would hold for any value.

    1.0 dB of RAW across-position sigma growth is the line. The envelope spends
    that spread as ``sigma_db / sqrt(n_positions)`` when deciding how much
    correction depth a band may have at all, so at the production cloud size
    this widening is several times the depth those terms would have licensed —
    a correction that buys flatness at the mark by trading it this far
    elsewhere is fitting one microphone position, not flattening the speaker.
    Moving the constant is a product decision about that line and should fail
    here first.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        DEFAULT_CLOUD_MEASURE_POSITIONS,
    )

    assert DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB == 1.0
    # …and the "several times" in the rationale is real at the shipped cloud
    # size, not a figure of speech.
    licensed_depth_db = DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB / math.sqrt(
        DEFAULT_CLOUD_MEASURE_POSITIONS - 1
    )
    assert licensed_depth_db < 0.4


# --------------------------------------------------------------------------- #
# the trusted band, the graded floor, and the frame (#2521)
#
# Four instrument defects, found by re-deriving the first remote JTS3 session's
# ``verdict=model_error rollback=true``: the probe graded the raw grid edges
# instead of the capture's trusted band, admitted bins whose command grazed the
# 0.5 dB floor, left the fitted frame inside the graded error, and regressed
# gain through the origin so a level offset arrived as scale.
# --------------------------------------------------------------------------- #

#: A grid that runs PAST the trusted ceiling, the way the production analysis
#: grid does — the live session's ran to 22,480 Hz while its capture's own gate
#: disclosure trusted it only to 20,000.
_WIDE_GRID_HZ = np.logspace(math.log10(100.0), math.log10(22_480.0), 460)
#: The fixture's trusted ceiling. Deliberately NOT the live session's 20 kHz:
#: between 20 kHz and this grid's end there is only 0.17 of an octave, which
#: cannot carry a :data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES` run by itself, so a
#: fixture at that ceiling could not exercise the control below at all. The
#: mechanism being pinned — a bin outside the trusted band contributes to no
#: claim and breaks a run — is the same one at any ceiling.
_TRUSTED_CEILING_HZ = 12_000.0
_LIVE_TRUSTED_FLOOR_HZ = 357.0


def _above_ceiling_fixture() -> tuple[np.ndarray, np.ndarray]:
    """A clean correction, plus a large error ABOVE the trusted ceiling."""
    commanded = np.where(
        (_WIDE_GRID_HZ >= 1_000.0) & (_WIDE_GRID_HZ <= 6_000.0), 5.0, 0.0
    )
    # Something is commanded up there too, and it is badly missed — the live
    # session's ``worst_hz=21,266 / max_error_db=23.4`` in miniature.
    above = _WIDE_GRID_HZ > _TRUSTED_CEILING_HZ
    commanded = np.where(above, 4.0, commanded)
    realized = commanded + np.where(above, 20.0, 0.0)
    return realized, commanded


def test_content_above_the_trusted_ceiling_cannot_create_an_exceedance():
    """**#2521 defect 1.** The band is the caller's, and the caller's band is
    the capture's own gate-derived trusted one — so a bin the capture is not a
    measurement at cannot roll a correction back, and cannot supply the
    ``worst_hz`` / ``max_error_db`` a reader will quote either.
    """
    realized, commanded = _above_ceiling_fixture()
    probe = classify_delta_probe(
        _WIDE_GRID_HZ, realized, commanded,
        band_hz=(_LIVE_TRUSTED_FLOOR_HZ, _TRUSTED_CEILING_HZ),
    )
    assert probe.verdict == VERDICT_MATCHED
    assert probe.rollback is False
    assert probe.probe_band_hz[1] <= _TRUSTED_CEILING_HZ
    assert probe.worst_hz <= _TRUSTED_CEILING_HZ
    # The band it was HANDED is on the record beside the band it graded, so a
    # disputed verdict says which bins it saw.
    assert probe.requested_band_hz == (_LIVE_TRUSTED_FLOOR_HZ, _TRUSTED_CEILING_HZ)


def test_the_same_content_graded_to_the_grid_edge_is_the_bug_it_was():
    """The control for the test above, and the mutation that proves it.

    Identical curves, one changed argument: graded to the raw grid edge — which
    is exactly what ``_run_delta_probe`` passed before #2521 — the same
    untrusted content is a rollback whose worst bin nobody measured anything at.
    """
    realized, commanded = _above_ceiling_fixture()
    probe = classify_delta_probe(
        _WIDE_GRID_HZ, realized, commanded,
        band_hz=(_LIVE_TRUSTED_FLOOR_HZ, float(_WIDE_GRID_HZ[-1])),
    )
    assert probe.rollback is True
    assert probe.worst_hz > _TRUSTED_CEILING_HZ


def test_an_hf_bin_grazing_the_old_floor_is_no_longer_graded():
    """**#2521 defect 1, second half.** Above the HF split this probe already
    concedes 2.5 dB of per-bin uncertainty; a bin commanding less than that
    cannot answer "did the speaker do what we asked"."""
    commanded = np.where(
        (_GRID_HZ >= 1_000.0) & (_GRID_HZ <= 5_000.0), 5.0, 0.0
    )
    grazing = _GRID_HZ >= 12_000.0
    commanded = np.where(grazing, 0.6, commanded)
    realized = commanded + np.where(grazing, 20.0, 0.0)
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MATCHED
    assert probe.probe_band_hz[1] <= 5_000.0

    # The control: the SAME 20 dB error, at a bin that commands more than the
    # tolerance it is graded against, is still a finding.
    commanded_real = np.where(grazing, 3.0, commanded)
    realized_real = commanded_real + np.where(grazing, 20.0, 0.0)
    loud = classify_delta_probe(
        _GRID_HZ, realized_real, commanded_real, band_hz=_band(),
    )
    assert loud.rollback is True


def test_the_hf_graded_floor_agrees_with_the_hf_tolerance():
    """Numerically equal, defined separately: the same measurement-uncertainty
    bar asked of what was ASKED FOR rather than of how far realization may
    miss. A floor under the tolerance would grade bins whose whole commanded
    value is inside the uncertainty already conceded there."""
    assert DELTA_PROBE_MIN_COMMANDED_HIGH_DB == DELTA_PROBE_TOLERANCE_HIGH_DB
    floors = graded_command_floor_db(_GRID_HZ)
    below = _GRID_HZ < DELTA_PROBE_HF_SPLIT_HZ
    assert np.all(floors[below] == DELTA_PROBE_MIN_COMMANDED_DB)
    assert np.all(floors[~below] == DELTA_PROBE_MIN_COMMANDED_HIGH_DB)


def test_the_tiered_floor_leaves_the_keystone_fixture_untouched():
    """Why the floor is tiered rather than simply lifted.

    The 2026-07-27 shelf-Q defect's commanded curve passes through 0.5-1.5 dB
    across the very octaves its error lives in, so a flat lift deletes the one
    defect this module exists to catch (measured: a 1.0 dB floor takes its
    exceedance run from 0.575 to 0.307 octaves, under the width rule). The
    tiered floor grades exactly the bins the old flat 0.5 dB floor did on this
    fixture — pinned here rather than asserted in prose.
    """
    from jasper.active_speaker.linearization_fit import (
        _HIGHSHELF_Q, _highshelf_response_db,
    )

    commanded = _highshelf_response_db(_GRID_HZ, 7_000.0, -11.0, _HIGHSHELF_Q)
    tiered = np.abs(commanded) >= graded_command_floor_db(_GRID_HZ)
    flat = np.abs(commanded) >= DELTA_PROBE_MIN_COMMANDED_DB
    assert np.array_equal(tiered, flat)


def test_a_broadband_tilt_is_disclosed_not_rolled_back():
    """**#2521 defect 2 — the arbitrary-hardware blocker.**

    An in-room gated measurement against an on-axis two-branch model differs by
    a frame, and on the 2026-07-29 corpus one −0.79 dB/octave tilt accounted for
    84 % of a headline model error. Left in, this probe failed every speaker,
    room, and microphone with a broadband slope no matter how well its filters
    realized. Removed, the same curves grade to nothing.
    """
    commanded = _commanded_lift()
    tilt_db_per_octave = -0.9
    realized = commanded + tilt_db_per_octave * np.log2(_GRID_HZ / 1_000.0)

    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())

    assert probe.verdict == VERDICT_FRAME_MISMATCH
    assert probe.reason == "uncommanded_frame_shift"
    assert probe.rollback is False
    # The raw grade still SAW it — the demotion is a judgement about what the
    # finding means, not a failure to notice one.
    assert probe.exceedance_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES
    assert probe.max_error_db > DELTA_PROBE_TOLERANCE_LOW_DB
    # …and what it means is on the record: the tilt that was removed, and the
    # grade that survived removing it.
    assert probe.frame.tilt_db_per_octave == pytest.approx(
        tilt_db_per_octave, abs=1e-6
    )
    assert probe.frame_removed_max_db == pytest.approx(0.0, abs=1e-6)
    assert probe.frame_removed_rms_db == pytest.approx(0.0, abs=1e-6)
    assert probe.frame_removed_exceedance_octaves == 0.0


@pytest.mark.parametrize("depth", [1.0, 0.8])
def test_a_tilt_does_not_become_a_shortfall_either(depth):
    """**The other rollback door, and it takes TWO mechanisms to close it.**

    ``level_dependent_shortfall`` is as much a rollback as ``model_error``, so a
    room tilt must not be able to reach it either. Two different things get it
    there, and this test only proved the first until the adversarial gate found
    the second (2026-08-15):

    ``depth=1.0`` — a perfectly realized correction under a tilt. A tilt against
    a ramp-shaped command IS a scale factor, more cleanly with an intercept than
    without, so a raw-curve regression reads it as depth loss. Running the
    regression on the FRAME-REMOVED curve is what fixes this one: the same
    speaker reads full depth.

    ``depth=0.8`` — a REAL but in-tolerance depth shortfall under the same tilt,
    which the frame-removed regression correctly still reads as 0.8. Nothing
    about the regression can save this one: the verdict has to be gated on
    whether the EXCEEDANCE survives the frame, and the exceedance here is
    entirely the tilt. The gate's own reproducer (a 4 dB Gaussian lift at 5 kHz)
    is `test_a_real_but_in_tolerance_shortfall_under_a_tilt_is_not_a_rollback`
    below; this parametrization is the same class on this file's fixture.
    """
    commanded = _commanded_lift()
    realized = depth * commanded - 0.9 * np.log2(_GRID_HZ / 1_000.0)
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict != VERDICT_LEVEL_DEPENDENT_SHORTFALL
    assert probe.rollback is False
    # 1e-3, for the reason
    # ``test_a_proportional_undershoot_of_a_lift_is_a_level_shortfall`` records:
    # the quiet bins admit commands up to ``DELTA_PROBE_MIN_COMMANDED_DB``, so a
    # depth shortfall that scales those too leaves a trace of itself in the
    # fitted tilt (measured here: 1.8e-4 of gain).
    assert probe.gain_factor == pytest.approx(depth, abs=1e-3)


def test_a_real_but_in_tolerance_shortfall_under_a_tilt_is_not_a_rollback():
    """**The adversarial gate's reproducer** (2026-08-15), pinned verbatim.

    A 4 dB Gaussian lift at 5 kHz realized at 80 % depth. On its own the 0.8 dB
    miss never clears tolerance, so the map is ``matched`` — the probe has no
    finding about this speaker at all. Add a −0.9 dB/octave room frame and the
    first cut of the frame gate turned it into a ``level_dependent_shortfall``
    ROLLBACK, with its frame-removed exceedance still exactly 0.0: the entire
    finding was the frame, reached through the one rollback door the gate did
    not guard. Over a randomized sweep, 203 of 4,000 draws did this.

    Both halves are asserted, because the second is only meaningful against the
    first: no tilt ⇒ no finding, tilt ⇒ a DISCLOSED finding, never a rollback.
    """
    lift = 4.0 * np.exp(-0.5 * (np.log2(_GRID_HZ / 5_000.0) / 0.5) ** 2)

    clean = classify_delta_probe(_GRID_HZ, 0.8 * lift, lift, band_hz=_band())
    assert clean.verdict == VERDICT_MATCHED

    tilted = classify_delta_probe(
        _GRID_HZ, 0.8 * lift - 0.9 * np.log2(_GRID_HZ / 1_000.0), lift,
        band_hz=_band(),
    )
    assert tilted.rollback is False
    assert tilted.verdict == VERDICT_FRAME_MISMATCH
    # The number that made this a bug: the graded evidence, frame removed, is
    # nothing at all — so there was never anything here to refuse on.
    assert tilted.frame_removed_exceedance_octaves == 0.0
    # …and the shortfall really was measurable, which is what makes this the
    # hard case rather than a restatement of the pure-tilt one above. (1e-3 for
    # the quiet-bin bias the sibling test above records — the Gaussian's own
    # tails sit in those bins, so the shortfall scales them too.)
    assert tilted.gain_factor == pytest.approx(0.8, abs=1e-3)


def test_no_rollback_survives_a_zero_frame_removed_exceedance():
    """The gate's randomized sweep, as a property rather than one fixture.

    Every rollback this probe returns must rest on evidence that survived the
    frame. Swept over shape, depth, tilt and offset, the invariant is: if the
    verdict rolls back, the frame-removed exceedance is a real one. This is the
    guard that would have caught the shortfall door — the class it closes is
    wider than any single reproducer, and a future third rollback verdict added
    below the gate would fail here rather than in production.
    """
    rng = np.random.default_rng(20260815)
    rollbacks = 0
    for _ in range(400):
        center = 10.0 ** rng.uniform(math.log10(200.0), math.log10(15_000.0))
        commanded = rng.uniform(-10.0, 10.0) * np.exp(
            -0.5 * (np.log2(_GRID_HZ / center) / rng.uniform(0.2, 2.0)) ** 2
        )
        realized = (
            rng.uniform(0.3, 1.6) * commanded
            + rng.uniform(-1.5, 1.5) * np.log2(_GRID_HZ / 1_000.0)
            + rng.uniform(-3.0, 3.0)
        )
        probe = classify_delta_probe(
            _GRID_HZ, realized, commanded, band_hz=_band(),
        )
        if probe.rollback:
            rollbacks += 1
            assert probe.frame_removed_exceedance_octaves != 0.0, (
                f"{probe.verdict} rolled back on evidence that is entirely "
                f"frame (offset {probe.frame.offset_db}, tilt "
                f"{probe.frame.tilt_db_per_octave})"
            )
    # The sweep must actually reach the rollback verdicts, or it proves nothing.
    assert rollbacks > 50


def test_a_genuine_in_band_shape_error_still_rolls_back():
    """The control for the frame gate: closing the false positive must not
    close the true one. A localized bump is not a line in log-frequency, so no
    frame can absorb it."""
    commanded = np.full_like(_GRID_HZ, 6.0)
    bump = (_GRID_HZ >= 2_000.0) & (_GRID_HZ <= 3_000.0)
    probe = classify_delta_probe(
        _GRID_HZ, commanded + np.where(bump, 6.0, 0.0), commanded, band_hz=_band(),
    )
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.rollback is True


def test_the_keystone_survives_frame_removal():
    """**The keystone, re-asked of the new gate.** The 2026-07-27 shelf-Q
    realization error must still be a rollback with the frame out — and it is
    for a reason, not by luck: the frame is fitted where the correction
    commanded NOTHING, so a defect confined to the commanded region cannot
    contribute to the line that is then subtracted from it.
    """
    from jasper.active_speaker.linearization_fit import (
        _HIGHSHELF_Q, _highshelf_response_db,
    )

    commanded = _highshelf_response_db(_GRID_HZ, 7_000.0, -11.0, _HIGHSHELF_Q)
    realized = _highshelf_response_db(_GRID_HZ, 7_000.0, -11.0, 0.476)
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.rollback is True
    assert probe.frame.fitted is True
    assert (
        probe.frame_removed_exceedance_octaves
        >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES
    )


def test_fitting_the_frame_over_the_graded_bins_would_delete_the_keystone():
    """Why the quiet bins, stated as a measurement rather than a preference.

    Re-runs the keystone's own arithmetic with the frame fitted over the GRADED
    bins instead — the obvious alternative — and shows the defect subtracting
    itself: the exceedance disappears. This is the mutation that makes the
    quiet-bin choice load-bearing rather than incidental.
    """
    from jasper.active_speaker.delta_probe import (
        _structured_exceedance, _tolerance_curve,
    )
    from jasper.active_speaker.linearization_fit import (
        _HIGHSHELF_Q, _highshelf_response_db,
    )
    from jasper.audio_measurement.frame_fit import fit_frame

    commanded = _highshelf_response_db(_GRID_HZ, 7_000.0, -11.0, _HIGHSHELF_Q)
    realized = _highshelf_response_db(_GRID_HZ, 7_000.0, -11.0, 0.476)
    graded = np.abs(commanded) >= graded_command_floor_db(_GRID_HZ)
    wrong_frame = fit_frame(
        _GRID_HZ[graded], realized[graded], commanded[graded],
    )
    deframed = realized - wrong_frame.frame_db(_GRID_HZ)
    exceeded, _width = _structured_exceedance(
        _GRID_HZ,
        np.where(graded, deframed - commanded, 0.0),
        _tolerance_curve(_GRID_HZ),
        graded,
    )
    assert exceeded is False
    # …while the shipped rule keeps it.
    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert probe.verdict == VERDICT_MODEL_ERROR


def test_an_unfitted_frame_demotes_nothing():
    """A correction that commands something everywhere leaves no quiet bins, so
    no frame is measured and the verdict stands exactly as it did before this
    gate existed. Strict is the honest direction for an absent measurement."""
    commanded = np.full_like(_GRID_HZ, 6.0)
    probe = classify_delta_probe(
        _GRID_HZ, commanded + 4.0, commanded, band_hz=_band(),
    )
    assert probe.residual_offset_db is None
    assert probe.frame.fitted is False
    assert probe.frame_removed_max_db is None
    assert probe.frame_removed_rms_db is None
    assert probe.frame_removed_exceedance_octaves is None
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.rollback is True


def test_the_frame_gate_can_only_narrow_a_finding():
    """The safety property the whole design rests on, asserted directly.

    The RAW grade decides whether there is a finding; the frame-removed grade
    decides only whether that finding is a rollback. So a frame fitted from a
    noisy quiet region cannot fabricate a rollback — the worst it can do is
    fail to demote one, or move a demotion between the two rollback names.
    Walked over this file's fixture vocabulary.

    **Scope, named rather than implied** (adversarial gate, 2026-08-15): these
    fixtures supply no spatial arm, and that is not an oversight to fix by
    extending them. ``spatially_costly`` is returned from the branch where the
    mark map MATCHED — no exceedance was ever in play — so the frame gate is
    structurally upstream of it and this property has nothing to say about it.
    That verdict rests on two real measurements of the room's spread, with no
    model between them for a frame to explain away;
    ``test_a_chain_defect_outranks_the_spatial_arm`` and
    ``test_an_unavailable_spatial_arm_cannot_produce_a_costly_verdict`` own its
    behaviour. The claim here is about the two verdicts the gate does guard.
    """
    commanded = _commanded_lift()
    fixtures = [
        (commanded, commanded),
        (commanded * 0.4, commanded),
        (commanded * 1.5, commanded),
        (commanded + 6.0 * np.sin(np.log2(_GRID_HZ)), commanded),
        (commanded - 0.9 * np.log2(_GRID_HZ / 1_000.0), commanded),
        (commanded + np.where(_GRID_HZ > 6_000.0, 4.0, -4.0), commanded),
    ]
    for realized, cmd in fixtures:
        probe = classify_delta_probe(_GRID_HZ, realized, cmd, band_hz=_band())
        if probe.rollback:
            assert probe.exceedance_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


def test_a_spatially_costly_verdict_is_reached_without_the_frame_gate():
    """The boundary above, exercised rather than only described.

    A map that matches at the mark and widened the room still rolls back, and it
    does so from ahead of the frame gate — so a fitted frame, of any size, is
    irrelevant to it. Pinned so "the gate guards every rollback" can never be
    read into the narrowing property.
    """
    commanded = _commanded_lift()
    # Matched at the mark AND carrying a real quiet-bin frame, so the frame is
    # genuinely fitted and genuinely non-zero at the moment this verdict is
    # returned — a fixture with no frame could not tell the two orderings apart.
    probe = classify_delta_probe(
        _GRID_HZ, commanded - 0.2 * np.log2(_GRID_HZ / 1_000.0), commanded,
        band_hz=_band(),
        spatial=evaluate_spatial_cost(
            [_Band(1_000.0, 1.0)], [_Band(1_000.0, 3.0)],
        ),
    )
    assert probe.verdict == VERDICT_SPATIALLY_COSTLY
    assert probe.rollback is True
    assert probe.frame.fitted is True
    assert probe.frame.tilt_db_per_octave == pytest.approx(-0.2, abs=1e-6)


def test_the_frames_offset_term_is_the_residual_offset_it_already_reported():
    """One quiet-bin set, two estimators, and they agree by construction.

    ``fit_frame`` pivots at the fitted bins' geometric mean, which makes its
    offset the plain mean of the difference — the same number
    ``residual_offset_db`` has reported since #1811. Pinned so the frame cannot
    silently start measuring its level term somewhere else.
    """
    commanded = _commanded_lift()
    probe = classify_delta_probe(
        _GRID_HZ, commanded - 0.8, commanded, band_hz=_band(),
    )
    assert probe.frame.fitted is True
    assert probe.frame.offset_db == pytest.approx(probe.residual_offset_db, abs=1e-9)


def test_a_constant_offset_reads_as_offset_not_as_scale():
    """**#2521 defect 3.** Through the origin, on a commanded curve that is
    mostly negative, a constant level shift arrives as apparent SCALE — the live
    session's −7.8 dB read as gain ≈2.02 and then drove the
    shortfall-vs-model-error branch. A regression that may state where it
    crosses zero cannot make that mistake.
    """
    commanded = np.zeros_like(_GRID_HZ)
    commanded[(_GRID_HZ >= 300.0) & (_GRID_HZ <= 3_000.0)] = -6.0
    commanded[(_GRID_HZ >= 8_000.0) & (_GRID_HZ <= 16_000.0)] = 2.0
    offset_db = -7.8
    realized = commanded + offset_db

    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())

    assert probe.gain_factor == pytest.approx(1.0, abs=1e-6)
    assert probe.gain_intercept_db == pytest.approx(0.0, abs=1e-6)
    # The fixture really is the shape that broke: fitted through the origin on
    # these same bins, the identical curves read as more than 2x scale.
    graded = np.abs(commanded) >= graded_command_floor_db(_GRID_HZ)
    c, r = commanded[graded], realized[graded]
    through_origin = float(np.dot(r, c) / np.dot(c, c))
    assert through_origin > 2.0
    # …and the level move itself is still named, by the verdict it always was.
    assert probe.verdict == VERDICT_LEVEL_MISMATCH
    assert probe.residual_offset_db == pytest.approx(offset_db, abs=1e-9)


def test_the_demotion_and_the_survival_are_the_same_gate():
    """**#2521's policy half, both arms in one place** (owner ruling
    2026-08-15: least-bad is adoptable with disclosure; hard stops are reserved
    for the safety class).

    An exceedance that vanishes under trusted-band + frame-removed grading is
    an advisory the household is told about and keeps its tuning through; one
    that survives is the rollback it has always been. Neither arm is a new
    trigger — both start from a raw grade that already failed.
    """
    from jasper.active_speaker.linearization_fit import (
        _HIGHSHELF_Q, _highshelf_response_db,
    )

    commanded = _commanded_lift()
    demoted = classify_delta_probe(
        _GRID_HZ, commanded - 0.9 * np.log2(_GRID_HZ / 1_000.0), commanded,
        band_hz=_band(),
    )
    survived = classify_delta_probe(
        _GRID_HZ,
        _highshelf_response_db(_GRID_HZ, 7_000.0, -11.0, 0.476),
        _highshelf_response_db(_GRID_HZ, 7_000.0, -11.0, _HIGHSHELF_Q),
        band_hz=_band(),
    )
    assert demoted.exceedance_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES
    assert survived.exceedance_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES
    assert demoted.rollback is False
    assert survived.rollback is True
    assert demoted.verdict == VERDICT_FRAME_MISMATCH
    assert survived.verdict == VERDICT_MODEL_ERROR


def test_frame_mismatch_is_a_finding_not_a_pass():
    """Like ``level_mismatch`` and ``unavailable``, it grants no permission: it
    leaves the shape question unanswered and is not ``matched``."""
    assert VERDICT_FRAME_MISMATCH in DELTA_PROBE_VERDICTS
    assert VERDICT_FRAME_MISMATCH not in DELTA_PROBE_ROLLBACK_VERDICTS
    assert VERDICT_FRAME_MISMATCH != VERDICT_MATCHED


def test_to_dict_carries_the_band_the_frame_and_the_intercept():
    """A ledger row that cannot be re-graded is a verdict nobody can check."""
    commanded = _commanded_lift()
    payload = classify_delta_probe(
        _GRID_HZ, commanded - 0.9 * np.log2(_GRID_HZ / 1_000.0), commanded,
        band_hz=_band(),
    ).to_dict()
    assert payload["requested_band_hz"] == list(_band())
    assert payload["gain_intercept_db"] is not None
    frame = payload["frame"]
    assert frame["tilt_db_per_octave"] == pytest.approx(-0.9, abs=1e-6)
    assert frame["n_bins"] >= DELTA_PROBE_MIN_BINS
    assert set(frame["removed"]) == {"max_db", "rms_db", "exceedance_octaves"}


# --------------------------------------------------------------------------- #
# #2533 — the residual is a CHANGE, and its claim is bounded by where it was
#         measured
# --------------------------------------------------------------------------- #


#: A PRODUCTION-shaped grid: linear, at the retained 2026-08-15 session's own
#: geometry (511 bins, 46.9208 Hz apart, from 23.4375 Hz).
#:
#: The coverage gate has to be exercised on this shape and not only on
#: ``_GRID_HZ``, because a log grid is the ONE shape production cannot produce:
#: captures arrive on an ``rfftfreq`` grid and
#: ``spatial_combine._capture_arrays`` refuses anything whose bin steps are not
#: uniform (``smooth_fractional_octave`` assumes linear bins). A ratio that only
#: behaves on ``np.logspace`` is a ratio that never runs.
_LINEAR_GRID_HZ = 23.4375 + 46.9208 * np.arange(511)

#: Both shapes, for the gate's own tests. ``ids`` so a failure names the grid.
_GRID_SHAPES = [
    pytest.param(_LINEAR_GRID_HZ, id="linear-production"),
    pytest.param(_GRID_HZ, id="log-fixture"),
]


def _entry_anchored(commanded, *, realized, grid=None, band=None, anchor_db=0.0,
                    **kwargs):
    """Classify with a flat pre-apply anchor of ``anchor_db`` dB."""
    f = _GRID_HZ if grid is None else grid
    return classify_delta_probe(
        f, realized, commanded, band_hz=band or (float(f[0]), float(f[-1])),
        entry_delta_db=np.full_like(f, anchor_db), **kwargs,
    )


def _half_octave_blocks(f, lo_hz, hi_hz, depth_db=-4.0):
    """A commanded curve that alternates commanded/quiet every HALF OCTAVE.

    Co-spanning by construction: the quiet bins are interleaved THROUGH the
    graded band rather than sitting beside it, which is the shape a genuinely
    whole-band level shift leaves behind. Half an octave because every graded run
    must still clear :data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES`, and a run
    specified in BINS cannot promise that on a linear grid — 40 bins is three
    octaves at 400 Hz and a seventh of one at 18 kHz.
    """
    in_band = (f >= lo_hz) & (f <= hi_hz)
    block = np.floor(np.log2(np.maximum(f, 1.0) / lo_hz) / 0.5)
    return np.where(in_band & (block % 2 == 0), depth_db, 0.0)


def _mirror_profile(peak_gain_db: float) -> dict:
    """An era-older frozen profile whose linearization is one bell.

    Deliberately the mirror-only shape (no ``recomposition_snapshot``), because
    that is the path whose charge is the naked cascade's peak plus the 1 dB
    margin — the one arrangement in which a headroom can be stated exactly
    rather than quoted to four places off an emitted config.
    """
    return {
        "linearization": {
            "woofer": [
                {"type": "Peaking", "freq": 1_000.0, "q": 1.0, "gain": peak_gain_db},
            ],
        },
    }


def test_a_declared_common_attenuation_move_is_realized_and_grades_to_zero():
    """**#2533 (a).** Two profiles differing only by a pre-split common
    attenuation; a speaker that realizes exactly that move has nothing left over.

    The apply hands 3 dB of headroom BACK (5.00 dB charged before, 2.00 after),
    so the speaker gets 3 dB louder and the emitter says so. Measured against the
    pre-apply capture, that is the whole of the change — the residual is zero and
    no level verdict is reached.
    """
    from jasper.active_speaker.baseline_profile import (
        applied_program_level_delta_db,
        profile_program_headroom_db,
    )

    previous, applied = _mirror_profile(4.0), _mirror_profile(1.0)
    assert profile_program_headroom_db(previous) == pytest.approx(5.00, abs=1e-9)
    assert profile_program_headroom_db(applied) == pytest.approx(2.00, abs=1e-9)
    declared_db = applied_program_level_delta_db(previous, applied)
    assert declared_db == pytest.approx(3.00, abs=1e-9)

    # The pre-apply capture sat exactly on its prediction; the post-apply one
    # sits on its own prediction plus the declared move, everywhere.
    commanded = _commanded_lift()
    probe = _entry_anchored(
        commanded, realized=commanded + declared_db, anchor_db=0.0,
        expected_offset_db=declared_db,
    )
    assert probe.residual_offset_db == pytest.approx(0.0, abs=1e-9)
    assert probe.entry_anchor_offset_db == pytest.approx(0.0, abs=1e-9)
    assert probe.verdict != VERDICT_LEVEL_MISMATCH
    assert probe.verdict == VERDICT_MATCHED

    # In-test control for the mutation the brief names: a profile charged 2.50
    # instead of 2.00 declares only 2.50 dB, and the half a dB the speaker moved
    # beyond what was declared is exactly what the residual is for.
    off_by_half = applied_program_level_delta_db(previous, _mirror_profile(1.5))
    assert off_by_half == pytest.approx(2.50, abs=1e-9)
    strayed = _entry_anchored(
        commanded, realized=commanded + declared_db, anchor_db=0.0,
        expected_offset_db=off_by_half,
    )
    assert strayed.residual_offset_db == pytest.approx(0.5, abs=1e-9)

    # The same speaker, on a session whose model was NOT anchored exactly right
    # going in — which is the ordinary case, and the arm that proves the anchor
    # is what holds the residual at zero rather than a happy zero doing it. The
    # standing offset is still a finding (it is a real difference between these
    # two curves) but it is the FRAME's, not a level move nobody commanded.
    standing_db = -2.0
    anchored = _entry_anchored(
        commanded, realized=commanded + declared_db + standing_db,
        anchor_db=standing_db, expected_offset_db=declared_db,
    )
    assert anchored.entry_anchor_offset_db == pytest.approx(standing_db, abs=1e-9)
    assert anchored.residual_offset_db == pytest.approx(0.0, abs=1e-9)
    assert anchored.verdict != VERDICT_LEVEL_MISMATCH
    assert anchored.rollback is False


@pytest.mark.parametrize("grid", _GRID_SHAPES)
def test_a_level_shift_measured_only_above_the_graded_band_is_not_whole_band(grid):
    """**#2533 (b).** The 2026-08-15 JTS3 shape: the correction commands the
    midband, so the only bins that can measure a level sit above it.

    The finding stands — the verdict, the rollback answer and the household
    surface are all unchanged, because narrowing them would make this instrument
    stricter on evidence it had just called unrepresentative. What narrows is the
    CLAIM: the reason says the shift was measured outside the graded band, and
    ``quiet_core_band_hz`` names the band it actually covers.
    """
    band = (400.0, 20_000.0)
    commanded = np.where((grid >= 400.0) & (grid <= 12_000.0), -4.0, 0.0)
    probe = _entry_anchored(
        commanded, realized=commanded - 3.0, grid=grid, band=band,
    )

    assert probe.verdict == VERDICT_LEVEL_MISMATCH
    assert probe.rollback is False
    assert probe.residual_offset_db == pytest.approx(-3.0, abs=1e-9)
    # The claim is band-scoped, not whole-band.
    assert probe.reason == REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND
    assert probe.reason != REASON_UNCOMMANDED_LEVEL_SHIFT
    # …and the record says which band, and how little of the graded one it is.
    assert probe.quiet_core_band_hz[0] > 12_000.0
    assert probe.probe_band_hz[1] < 12_100.0
    assert probe.quiet_probe_coverage < DELTA_PROBE_MIN_QUIET_COVERAGE


def test_a_standing_model_offset_present_before_the_apply_never_reaches_it():
    """**#2533 (c).** An offset in BOTH captures is not a level move.

    An in-room measurement and an on-axis two-branch model do not share a level
    anchor, and the mismatch is a standing property of the comparison. Anchored,
    it cancels and the residual reports the zero that is true. Unanchored — the
    control, and the pre-#2533 behaviour — the identical evidence is named an
    ``uncommanded_level_shift`` of the full standing amount.
    """
    commanded = _commanded_lift()
    standing_db = -6.0
    realized = commanded + standing_db

    anchored = _entry_anchored(commanded, realized=realized, anchor_db=standing_db)
    assert anchored.entry_anchor_offset_db == pytest.approx(standing_db, abs=1e-9)
    assert anchored.residual_offset_db == pytest.approx(0.0, abs=1e-9)
    assert anchored.verdict != VERDICT_LEVEL_MISMATCH
    assert anchored.rollback is False

    blind = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    assert blind.entry_anchor_offset_db is None
    assert blind.residual_offset_db == pytest.approx(standing_db, abs=1e-9)
    assert blind.verdict == VERDICT_LEVEL_MISMATCH


@pytest.mark.parametrize("grid", _GRID_SHAPES)
def test_a_genuine_whole_band_shift_still_trips_the_level_verdict(grid):
    """**#2533 (d).** The control that keeps the two fixes from swallowing the
    thing this verdict exists to catch.

    A real uncommanded drop, on top of a standing anchor AND a declared graph
    move, still fires with the WHOLE-BAND reason — and the residual reports the
    REAL shift rather than the sum of all three, which is the whole point of
    anchoring it.

    The quiet bins are interleaved THROUGH the graded band, which is what a
    whole-band shift actually leaves behind and what makes this a test of the
    coverage gate rather than a test that happens to route around it: a quiet set
    sitting beside the band, as the first cut of this fixture had it, scores well
    over the bar on both grids for reasons that have nothing to do with the
    mechanism.
    """
    lo_hz, hi_hz = 400.0, 12_800.0
    commanded = _half_octave_blocks(grid, lo_hz, hi_hz)
    standing_db, declared_db, shift_db = -2.0, 1.0, -3.0
    realized = commanded + standing_db + declared_db + shift_db

    probe = _entry_anchored(
        commanded, realized=realized, grid=grid, band=(lo_hz, hi_hz),
        anchor_db=standing_db, expected_offset_db=declared_db,
    )
    assert probe.verdict == VERDICT_LEVEL_MISMATCH
    assert probe.reason == REASON_UNCOMMANDED_LEVEL_SHIFT
    assert probe.residual_offset_db == pytest.approx(shift_db, abs=1e-9)
    assert probe.entry_anchor_offset_db == pytest.approx(standing_db, abs=1e-9)
    assert probe.quiet_probe_coverage >= DELTA_PROBE_MIN_QUIET_COVERAGE
    # The fixture really is co-spanning: the quiet evidence's middle half sits
    # INSIDE the band it is claimed over, on either grid.
    assert probe.quiet_core_band_hz[0] > probe.probe_band_hz[0]
    assert probe.quiet_core_band_hz[0] < probe.probe_band_hz[1]


@pytest.mark.parametrize("grid", _GRID_SHAPES)
def test_a_co_spanning_quiet_set_scores_one_whatever_the_grid_shape(grid):
    """**The 1.0 reference is what is derived**, and it is grid-invariant.

    ``quiet_probe_coverage`` divides the quiet bins' interquartile octave span by
    the SAME statistic over every graded-band bin on the same grid. A quiet set
    that samples the band exactly as densely as the band's own bins therefore
    scores 1.0 — on a production linear grid and on a log one alike, because
    whatever the grid does to the numerator it does to the denominator.

    Read off the CLASSIFIER's own field rather than recomputed here, so the
    reference this bar rests on is the number the shipped code produces. This is
    the pin the earlier whole-span denominator could not have passed; see the
    sibling test for what that one scored on real geometry.
    """
    lo_hz, hi_hz = 400.0, 12_000.0
    band = (grid >= lo_hz) & (grid <= hi_hz)
    idx = np.flatnonzero(band)
    # Every other in-band bin is quiet, so the two sets sample the band
    # identically. The alternation breaks every exceedance run, so the map is
    # ``matched`` — the coverage is measured on every map, not only a failing
    # one, which is what lets the reference be pinned on its own.
    commanded = np.zeros_like(grid)
    commanded[idx[1::2]] = -4.0
    probe = _entry_anchored(
        commanded, realized=commanded - 3.0, grid=grid, band=(lo_hz, hi_hz),
    )

    assert probe.quiet_probe_coverage == pytest.approx(1.0, abs=0.02)
    assert probe.quiet_probe_coverage > DELTA_PROBE_MIN_QUIET_COVERAGE
    # The same number, recomputed from the two disclosed spans — so the field is
    # the ratio it says it is and not an unrelated scalar that happens to be 1.
    band_core = interquartile_band_hz(
        grid[(grid >= probe.probe_band_hz[0]) & (grid <= probe.probe_band_hz[1])]
    )
    assert probe.quiet_probe_coverage == pytest.approx(
        math.log2(probe.quiet_core_band_hz[1] / probe.quiet_core_band_hz[0])
        / math.log2(band_core[1] / band_core[0]),
        abs=1e-9,
    )


def test_a_whole_span_denominator_would_be_unclearable_on_a_production_grid():
    """Why the denominator is the band's own interquartile span and not its span.

    Production grids are LINEAR (``rfftfreq``; ``spatial_combine`` refuses
    anything else because ``smooth_fractional_octave`` assumes linear bins), so
    bin density rises with frequency and any interquartile span is pulled toward
    the top octaves. Against the band's WHOLE span, a quiet set sampling the real
    357 Hz–10 kHz graded band perfectly and uniformly scores ~0.30 — under the
    bar, on the best possible evidence. That form of the ratio measured the grid,
    not the evidence, and no real quiet set could ever have cleared it.
    """
    lo_hz, hi_hz = 357.1, 10_000.0
    band = (_LINEAR_GRID_HZ >= lo_hz) & (_LINEAR_GRID_HZ <= hi_hz)
    idx = np.flatnonzero(band)
    quiet = np.zeros_like(_LINEAR_GRID_HZ, dtype=bool)
    quiet[idx[::2]] = True
    quiet_core = interquartile_band_hz(_LINEAR_GRID_HZ[quiet])

    whole_span = math.log2(
        _LINEAR_GRID_HZ[idx[-1]] / _LINEAR_GRID_HZ[idx[0]]
    )
    old_form = math.log2(quiet_core[1] / quiet_core[0]) / whole_span
    assert old_form < DELTA_PROBE_MIN_QUIET_COVERAGE
    assert old_form == pytest.approx(0.303, abs=0.01)

    # The shipped form, on the identical bins, clears it comfortably.
    band_core = interquartile_band_hz(_LINEAR_GRID_HZ[band])
    shipped = math.log2(quiet_core[1] / quiet_core[0]) / math.log2(
        band_core[1] / band_core[0]
    )
    assert shipped > DELTA_PROBE_MIN_QUIET_COVERAGE


@pytest.mark.parametrize("grid", _GRID_SHAPES)
def test_two_stray_bins_cannot_make_a_sliver_look_band_wide(grid):
    """Why the coverage reads the INTERQUARTILE span and not the min/max.

    ``frame.band_hz`` already reports the quiet bins' min/max, and on 2026-08-15
    two strays (493 Hz and 1.9 kHz) made a set with 158 of its 160 bins above
    12 kHz span 463 Hz-20 kHz on paper. This fixture is that shape: the min/max
    reading clears the bar comfortably and the interquartile one does not, on the
    same bins and on either grid.
    """
    band = (400.0, 20_000.0)
    commanded = np.where((grid >= 400.0) & (grid <= 12_000.0), -4.0, 0.0)
    # The two strays: in-band, and quiet, but far below where the rest sit.
    for stray_hz in (500.0, 1_900.0):
        commanded[int(np.argmin(np.abs(grid - stray_hz)))] = 0.0
    probe = _entry_anchored(
        commanded, realized=commanded - 3.0, grid=grid, band=band,
    )

    assert probe.reason == REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND
    assert probe.quiet_probe_coverage < DELTA_PROBE_MIN_QUIET_COVERAGE
    # The strays really did reach the min/max reading: it spans nearly the whole
    # analysis band while the middle half sits entirely above 12 kHz.
    assert probe.frame.band_hz[0] < 1_000.0
    assert probe.quiet_core_band_hz[0] > 12_000.0
    # The control: score the same bins through min/max — which is what
    # ``frame.band_hz`` is — against the same denominator, and the bar clears.
    band_bins = (grid >= probe.probe_band_hz[0]) & (grid <= probe.probe_band_hz[1])
    band_core = interquartile_band_hz(grid[band_bins])
    min_max_coverage = math.log2(
        probe.frame.band_hz[1] / probe.frame.band_hz[0]
    ) / math.log2(band_core[1] / band_core[0])
    assert min_max_coverage > DELTA_PROBE_MIN_QUIET_COVERAGE


@pytest.mark.parametrize("grid", _GRID_SHAPES)
def test_a_repeat_round_carries_the_previous_rounds_command_into_the_residual(grid):
    """**The limitation, pinned rather than hidden** (#2533, adversarial gate).

    ``commanded`` is measured against the RAW crossover while the entry capture
    rides whatever graph was active, so on a chained round the residual carries
    ``−mean(previous round's commanded curve over this round's quiet bins)``.
    That term is not an input to the classifier and cannot be bounded by it. Both
    directions are reproduced here so the documented behaviour is what the code
    actually does, and so a future change that fixes it fails this test loudly
    instead of silently improving prose.
    """
    band = (400.0, 20_000.0)
    # This round stops at 8 kHz; the previous one corrected all the way out, so
    # its command overlaps every one of this round's quiet bins.
    this_round = np.where((grid >= 400.0) & (grid <= 8_000.0), -6.0, 0.0)
    previous_round = np.where((grid >= 400.0) & (grid <= 20_000.0), -6.0, 0.0)

    # FABRICATES: the speaker realized this round exactly and nothing moved, yet
    # the residual reads +6.000 dB — the previous round's command, with its sign
    # flipped, read as an uncommanded level shift.
    phantom = classify_delta_probe(
        grid, this_round, this_round, band_hz=band, entry_delta_db=previous_round,
    )
    assert phantom.residual_offset_db == pytest.approx(6.0, abs=1e-9)

    # MASKS: a genuine −2.2 dB uncommanded drop, against a previous round that
    # commanded −2.2 dB across exactly this round's quiet bins, re-grades to a
    # residual of zero — the level verdict never fires and the finding arrives as
    # the frame instead.
    overlap = np.where(grid > 8_000.0, -2.2, 0.0)
    masked = classify_delta_probe(
        grid, this_round - 2.2, this_round, band_hz=band, entry_delta_db=overlap,
    )
    assert masked.residual_offset_db == pytest.approx(0.0, abs=1e-9)
    assert masked.verdict != VERDICT_LEVEL_MISMATCH
    assert masked.rollback is False
    # The same evidence with no overlapping previous round names the shift.
    clean = classify_delta_probe(
        grid, this_round - 2.2, this_round, band_hz=band,
        entry_delta_db=np.zeros_like(grid),
    )
    assert clean.residual_offset_db == pytest.approx(-2.2, abs=1e-9)
    assert clean.verdict == VERDICT_LEVEL_MISMATCH


def test_the_residual_is_the_frames_offset_minus_the_anchor():
    """The decomposition, as an identity rather than as prose.

    ``frame.offset_db`` is the quiet bins' ABSOLUTE disagreement with the model
    (``fit_frame`` pivots at their geometric mean, so its offset is their plain
    mean) and ``entry_anchor_offset_db`` is the part of it that was already there
    before the apply. What is left is the change, and that is the residual. The
    pre-#2533 identity — ``frame.offset_db == residual_offset_db`` — is the same
    statement with a zero anchor, and is still pinned by
    ``test_the_frames_offset_term_is_the_residual_offset_it_already_reported``.
    """
    commanded = _commanded_lift()
    probe = _entry_anchored(commanded, realized=commanded - 5.0, anchor_db=-3.25)
    assert probe.frame.fitted is True
    assert probe.entry_anchor_offset_db == pytest.approx(-3.25, abs=1e-9)
    assert probe.frame.offset_db - probe.entry_anchor_offset_db == pytest.approx(
        probe.residual_offset_db, abs=1e-9,
    )
    assert probe.residual_offset_db == pytest.approx(-1.75, abs=1e-9)


def test_an_unusable_entry_curve_leaves_the_residual_exactly_where_it_was():
    """An absent anchor is "nothing known", never a lost verdict.

    Governed by exactly ``expected_offset_db``'s rule: unsupplied, or a length
    that disagrees with the grid, both mean nothing is removed — so the standing
    offset stays VISIBLE in the residual rather than being pretended away, and
    every pre-#2533 caller keeps its numbers byte for byte.
    """
    commanded = _commanded_lift()
    realized = commanded - 4.0
    baseline = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())
    for unusable in (None, np.zeros(_GRID_HZ.size - 3)):
        probe = classify_delta_probe(
            _GRID_HZ, realized, commanded, band_hz=_band(),
            entry_delta_db=unusable,
        )
        assert probe.entry_anchor_offset_db is None
        assert probe.residual_offset_db == pytest.approx(
            baseline.residual_offset_db, abs=1e-12,
        )
        assert probe.verdict == baseline.verdict
        assert probe.reason == baseline.reason


def test_anchor_bins_the_pre_apply_capture_could_not_trust_are_dropped():
    """A non-finite anchor bin is a bin that capture excluded, not a zero.

    The remaining bins still anchor the residual as long as there are enough of
    them; too few and the anchor is absent, which is the "nothing known" case
    above rather than a partially-trusted number.
    """
    commanded = _commanded_lift()
    realized = commanded - 5.0
    anchor = np.full_like(_GRID_HZ, -2.0)
    anchor[:5] = np.nan
    probe = classify_delta_probe(
        _GRID_HZ, realized, commanded, band_hz=_band(), entry_delta_db=anchor,
    )
    assert probe.entry_anchor_offset_db == pytest.approx(-2.0, abs=1e-9)
    assert probe.residual_offset_db == pytest.approx(-3.0, abs=1e-9)

    starved = classify_delta_probe(
        _GRID_HZ, realized, commanded, band_hz=_band(),
        entry_delta_db=np.full_like(_GRID_HZ, np.nan),
    )
    assert starved.entry_anchor_offset_db is None
    assert starved.residual_offset_db == pytest.approx(-5.0, abs=1e-9)


def test_the_quiet_coverage_bar_and_the_smoothing_width_rule_are_independent():
    """Two octave rules, deliberately not the same constant.

    :data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES` is an absolute width (how wide a
    run of error must be to be structure); the coverage bar is a FRACTION of the
    graded band. Pinned so a future retune of one is a decision about that
    quantity rather than a silent inheritance from the other.
    """
    assert 0.0 < DELTA_PROBE_MIN_QUIET_COVERAGE <= 1.0
    assert DELTA_PROBE_MIN_QUIET_COVERAGE != DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


def test_to_dict_carries_the_quiet_evidence_the_level_verdict_rests_on():
    """A band-scoped verdict is a claim ABOUT the band it covered, so the band,
    the coverage, the bin count and the anchor removed all ride the record."""
    band = (400.0, 20_000.0)
    grid = _LINEAR_GRID_HZ
    commanded = np.where((grid >= 400.0) & (grid <= 12_000.0), -4.0, 0.0)
    payload = _entry_anchored(
        commanded, realized=commanded - 3.0, grid=grid, band=band, anchor_db=-1.0,
    ).to_dict()
    quiet = payload["quiet"]
    assert set(quiet) == {
        "n_bins", "core_band_hz", "probe_coverage", "entry_anchor_offset_db",
    }
    assert quiet["n_bins"] >= DELTA_PROBE_MIN_BINS
    assert len(quiet["core_band_hz"]) == 2
    assert quiet["probe_coverage"] < DELTA_PROBE_MIN_QUIET_COVERAGE
    assert quiet["entry_anchor_offset_db"] == pytest.approx(-1.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# the boost-overshoot hard stop (#2537)
# --------------------------------------------------------------------------- #


def test_an_exactly_realized_boost_measures_no_overshoot():
    commanded = _commanded_lift()
    probe = classify_delta_probe(_GRID_HZ, commanded, commanded, band_hz=_band())
    assert probe.boost_over_declared_bound is False
    assert probe.boost_overshoot_db == pytest.approx(0.0, abs=1e-9)
    assert probe.boost_overshoot_octaves == 0.0


def test_a_boost_realized_far_above_what_it_commanded_clears_the_bound():
    """Energy the graph did not declare, over a structured run of the band."""
    commanded = _commanded_lift()
    probe = classify_delta_probe(
        _GRID_HZ, commanded + 5.0, commanded, band_hz=_band()
    )
    assert probe.boost_over_declared_bound is True
    assert probe.boost_overshoot_db == pytest.approx(5.0, abs=1e-6)
    assert probe.boost_overshoot_octaves >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES


def test_the_boost_finding_is_DIRECTIONAL_where_every_other_one_is_not():
    """Under-realizing a boost is a quality miss; over-realizing it is a hazard.

    Same magnitude, opposite sign, opposite answer — and the control matters:
    the under-realized case still produces a large ``max_error_db``, so the
    rest of the module DOES see it. Only this finding is silent about it.
    """
    commanded = _commanded_lift()
    under = classify_delta_probe(
        _GRID_HZ, commanded - 5.0, commanded, band_hz=_band()
    )
    over = classify_delta_probe(
        _GRID_HZ, commanded + 5.0, commanded, band_hz=_band()
    )

    assert under.boost_over_declared_bound is False
    assert under.boost_overshoot_db == pytest.approx(-5.0, abs=1e-6)
    assert over.boost_over_declared_bound is True
    # The control: the module is not blind to the under-realized case, it just
    # does not call it a hazard.
    assert under.max_error_db == pytest.approx(over.max_error_db, abs=1e-6)


def test_a_cut_only_correction_reports_not_measured_never_zero():
    """``None`` is "no graded bin commanded a boost", which 0.0 would misreport
    as "measured, and it did not overshoot" — the distinction ``gain_factor``
    and ``residual_offset_db`` both draw."""
    commanded = -_commanded_lift()
    probe = classify_delta_probe(_GRID_HZ, commanded, commanded, band_hz=_band())
    assert probe.boost_overshoot_db is None
    assert probe.boost_over_declared_bound is False


def test_a_single_overshooting_bin_is_texture_not_a_hard_stop():
    """The structured-exceedance rule, so noise cannot revert a correction.

    A hard stop that fired on one bin would pull a household's graph off for a
    measurement artefact — the same argument ``_structured_exceedance`` already
    makes for every other finding in this module.
    """
    commanded = _commanded_lift()
    realized = commanded.copy()
    realized[300] += 9.0

    probe = classify_delta_probe(_GRID_HZ, realized, commanded, band_hz=_band())

    assert probe.boost_overshoot_db == pytest.approx(9.0, abs=1e-6)
    assert probe.boost_overshoot_octaves < DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES
    assert probe.boost_over_declared_bound is False


def test_the_overshoot_is_measured_on_the_raw_curve_not_the_frame_removed_one():
    """A frame is removed to ask whether the SHAPE is right; this asks how much
    energy reached the driver. Subtracting a fitted offset first would hide
    exactly the whole-band overshoot the hard stop exists for."""
    grid = _LINEAR_GRID_HZ
    commanded = np.where((grid >= 400.0) & (grid <= 12_000.0), 4.0, 0.0)
    probe = classify_delta_probe(
        grid, commanded + 3.0, commanded, band_hz=(400.0, 20_000.0)
    )
    assert probe.frame.fitted is True
    assert probe.frame.offset_db == pytest.approx(3.0, abs=1e-6)
    # The frame says "the whole comparison sits 3 dB high"; the overshoot says
    # the boosted bins delivered 3 dB more than they asked for. Both are true,
    # and only the second is the safety question.
    assert probe.boost_overshoot_db == pytest.approx(3.0, abs=1e-6)
    assert probe.boost_over_declared_bound is True


def test_boost_overshoot_reports_no_finding_when_no_bin_is_boosted():
    """The helper's own contract, on the grid rather than through a verdict."""
    grid = _GRID_HZ
    commanded = -np.ones_like(grid) * 4.0
    mask = np.ones_like(grid, dtype=bool)
    over, worst, octaves = boost_overshoot(
        grid, commanded + 9.0, commanded, np.full_like(grid, 1.5), mask
    )
    assert (over, worst, octaves) == (False, None, 0.0)


def test_to_dict_carries_the_boost_evidence_the_hard_stop_rests_on():
    commanded = _commanded_lift()
    payload = classify_delta_probe(
        _GRID_HZ, commanded + 5.0, commanded, band_hz=_band()
    ).to_dict()
    assert set(payload["boost"]) == {
        "over_declared_bound", "overshoot_db", "overshoot_octaves",
    }
    assert payload["boost"]["over_declared_bound"] is True
    assert payload["boost"]["overshoot_db"] == pytest.approx(5.0, abs=1e-6)
