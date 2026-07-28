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
    DELTA_PROBE_MIN_COMMANDED_DB,
    DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES,
    DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB,
    DELTA_PROBE_ROLLBACK_VERDICTS,
    DELTA_PROBE_SHORTFALL_GAIN_CEILING,
    DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
    DELTA_PROBE_TOLERANCE_HIGH_DB,
    DELTA_PROBE_TOLERANCE_LOW_DB,
    DELTA_PROBE_VERDICTS,
    SPATIAL_COST_UNAVAILABLE,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED,
    VERDICT_MODEL_ERROR,
    VERDICT_SPATIALLY_COSTLY,
    VERDICT_UNAVAILABLE,
    classify_delta_probe,
    evaluate_spatial_cost,
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
    driver compression diagnostic."""
    commanded = _commanded_lift(depth_db=10.0)
    probe = classify_delta_probe(
        _GRID_HZ, commanded * 0.5, commanded, band_hz=_band(),
    )
    assert probe.verdict == VERDICT_LEVEL_DEPENDENT_SHORTFALL
    assert probe.reason == "realized_short_of_commanded"
    assert probe.gain_factor == pytest.approx(0.5, abs=1e-6)
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
