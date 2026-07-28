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
    DELTA_PROBE_ROLLBACK_VERDICTS,
    DELTA_PROBE_SHORTFALL_GAIN_CEILING,
    DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
    DELTA_PROBE_TOLERANCE_HIGH_DB,
    DELTA_PROBE_TOLERANCE_LOW_DB,
    DELTA_PROBE_VERDICTS,
    SPATIAL_COST_UNAVAILABLE,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
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
    session whose household closed the phone before the post-apply sweep."""
    assert DELTA_PROBE_ROLLBACK_VERDICTS == {
        VERDICT_MODEL_ERROR,
        VERDICT_LEVEL_DEPENDENT_SHORTFALL,
        VERDICT_SPATIALLY_COSTLY,
    }
    assert VERDICT_MATCHED not in DELTA_PROBE_ROLLBACK_VERDICTS
    assert VERDICT_UNAVAILABLE not in DELTA_PROBE_ROLLBACK_VERDICTS


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
    assert set(payload["spatial"]) == {
        "available", "widened", "worst_center_hz", "worst_widening_db",
        "tolerance_db", "n_bands",
    }


def test_the_commanded_floor_is_the_fit_engines_own_cosmetic_floor():
    """One definition of "this filter does nothing" across the fit and its
    verification."""
    from jasper.active_speaker.linearization_fit import _MIN_FILTER_GAIN_DB

    assert DELTA_PROBE_MIN_COMMANDED_DB == _MIN_FILTER_GAIN_DB
