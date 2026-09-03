# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.active_speaker.flat_spec (flat-linearization plan, S2).

Cross-check style follows tests/test_active_speaker_linearization_envelope.py:
numeric expectations are derived either from a literal, hand-computed pinned
constant (for the single case the task explicitly calls "pinned to exact
values") or from `_hand_power_mean_db` / `_signed_max_deviation` /
`_rms_deviation` -- independent re-implementations of the module's formulas
written fresh in this file, never by calling flat_spec's own private
helpers. The point of the independent re-implementation is to catch a
regression to the "linear-dB-mean-vs-power-mean trap" this repo has shipped
at least three times before, not to re-derive the module under test.

Two vocabulary points the assertions lean on throughout:

* `max_deviation_db` is **signed** -- the deviation at the largest-absolute
  bin, with its sign kept, plus `max_deviation_hz` naming that bin.
* A band with zero non-excluded bins is **unevaluable** (`evaluable=False`,
  `passed=None`, `None` metrics), not an exception and not a pass. Only the
  reference band still raises, because without a reference level nothing
  anywhere is computable.
"""
from __future__ import annotations

import inspect
import json
import math

import numpy as np
import pytest

import jasper.active_speaker.flat_spec as flat_spec
from jasper.active_speaker.flat_spec import (
    BEST_EFFORT_ABOVE_HZ,
    REFERENCE_BAND_HZ,
    SPEC_BANDS,
    BandResult,
    ConvergenceResidual,
    FlatSpecReport,
    evaluate_flat_spec,
    spec_convergence_residual,
)
from jasper.audio_measurement import gating
from jasper.audio_measurement.interference_nulls import identify_interference_nulls
from jasper.audio_measurement.spatial_combine import (
    combine_positions,
    merged_true_intervals,
)
from tests._flat_lin_corpus import (
    S0_MAIN,
    requires_s0_curves,
    s0_position_captures,
)

# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

# One frequency axis reused by most tests: 4 bins per SPEC_BANDS band, 3
# bins in the best-effort region above BEST_EFFORT_ABOVE_HZ, and a bin
# placed at EXACTLY each of the three band-edge frequencies (2000, 8000,
# 16000 Hz) so the inclusive-lower/exclusive-upper membership rule has
# something concrete to bite on.
_FREQS_HZ = np.array(
    [
        300.0, 600.0, 1000.0, 1500.0,       # band1: 250-2000 Hz
        2000.0, 3000.0, 5000.0, 7000.0,     # band2: 2000-8000 Hz (2000 = edge)
        8000.0, 10000.0, 12000.0, 15000.0,  # band3: 8000-16000 Hz (8000 = edge)
        16000.0, 18000.0, 20000.0,          # best-effort: >=16000 Hz (16000 = edge)
    ],
    dtype=np.float64,
)
_FREQS_HZ.flags.writeable = False

# Index groupings mirroring SPEC_BANDS / REFERENCE_BAND_HZ membership on
# _FREQS_HZ, precomputed by hand once here so individual tests don't need
# to re-derive the membership rule themselves. REFERENCE_BAND_HZ is now
# exactly band1 (250-2000 Hz, the low-mid band alone), so _REF_IDX mirrors
# _BAND1_IDX rather than spanning band1+band2 as it did pre-#1857-anchor.
_REF_IDX = list(range(0, 4))
_BAND1_IDX = list(range(0, 4))
_BAND2_IDX = list(range(4, 8))
_BAND3_IDX = list(range(8, 12))


def _flat_db(value_db: float = 0.0) -> np.ndarray:
    """A fresh, writable, all-``value_db`` curve on ``_FREQS_HZ``."""
    return np.full(_FREQS_HZ.shape, value_db, dtype=np.float64)


def _hand_power_mean_db(values_db: list[float]) -> float:
    """Independent re-derivation of the power-mean formula (``10*log10(
    mean(10**(dB/10)))``), written fresh here rather than imported from
    ``flat_spec._power_mean_db`` -- these cross-check tests exercise the
    module's OWN formula correctness, not just that it calls itself."""
    linear = [10.0 ** (v / 10.0) for v in values_db]
    return 10.0 * math.log10(sum(linear) / len(linear))


def _signed_max_deviation(values_db: list[float], reference_db: float) -> float:
    """The **signed** deviation at the largest-absolute bin -- the module's
    ``max_deviation_db`` contract, re-derived here in plain Python.

    Signed rather than absolute because "2.4 dB too loud" and "2.4 dB too
    quiet" call for opposite corrections; a bare magnitude hides which.
    """
    return max((v - reference_db for v in values_db), key=abs)


def _rms_deviation(values_db: list[float], reference_db: float) -> float:
    diffs = [v - reference_db for v in values_db]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


# --------------------------------------------------------------------------- #
# module constants
# --------------------------------------------------------------------------- #


def test_module_constants_match_pinned_contract():
    assert SPEC_BANDS == (
        (250.0, 2000.0, 1.5),
        (2000.0, 8000.0, 2.0),
        (8000.0, 16000.0, 2.5),
    )
    assert REFERENCE_BAND_HZ == (250.0, 2000.0)
    assert BEST_EFFORT_ABOVE_HZ == 16000.0


# --------------------------------------------------------------------------- #
# reference power-mean arithmetic -- the linear-vs-power-mean trap
# --------------------------------------------------------------------------- #


def test_reference_two_bin_power_mean_is_pinned_not_linear_average():
    """Two bins at 0 dB and 10 dB, both inside REFERENCE_BAND_HZ, plus one
    filler bin in each of the other two SPEC_BANDS bands so
    evaluate_flat_spec doesn't raise on THEIR zero-bin guard (this test
    only cares about the reference computation). The correct power-mean
    reference is 10*log10((10**(0/10) + 10**(10/10)) / 2) = 10*log10(5.5)
    -- pinned below to an exact literal, NOT the naive linear-dB average
    of (0 + 10) / 2 = 5.0 dB. This is the exact linear-vs-power-mean trap
    called out in the module docstring; a regression to linear averaging
    must fail this assertion loudly.
    """
    freqs_hz = np.array([1000.0, 1500.0, 4000.0, 10000.0])  # 1000/1500 -> reference band
    spec_smoothed_db = np.array([0.0, 10.0, 0.0, 0.0])

    report = evaluate_flat_spec(freqs_hz, spec_smoothed_db)

    # 10*math.log10(5.5), computed once and pinned here as a literal.
    pinned_power_mean_db = 7.403626894942439
    naive_linear_average_db = 5.0  # (0.0 + 10.0) / 2 -- the WRONG answer

    assert report.reference_db == pytest.approx(pinned_power_mean_db, abs=1e-9)
    assert report.reference_db != pytest.approx(naive_linear_average_db, abs=1e-3)


def test_exclusion_inside_reference_band_changes_reference_as_hand_computed():
    db = _flat_db(0.0)
    db[2] = 40.0  # 1000 Hz, inside REFERENCE_BAND_HZ -- a huge outlier

    report_included = evaluate_flat_spec(_FREQS_HZ, db)

    exclusion_mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    exclusion_mask[2] = True
    report_excluded = evaluate_flat_spec(_FREQS_HZ, db, exclusion_mask)

    values = db.tolist()
    expected_included = _hand_power_mean_db([values[i] for i in _REF_IDX])
    expected_excluded = _hand_power_mean_db([values[i] for i in _REF_IDX if i != 2])

    assert report_included.reference_db == pytest.approx(expected_included, abs=1e-9)
    assert report_excluded.reference_db == pytest.approx(expected_excluded, abs=1e-9)
    # The excluded run drops the 40 dB outlier from the reference-band
    # computation entirely -- the remaining 3 bins are all 0 dB.
    assert report_excluded.reference_db == 0.0
    # And confirms the outlier really did move the reference when it was
    # NOT excluded -- exclusion changed the outcome, not a no-op.
    assert report_included.reference_db > 1.0


# --------------------------------------------------------------------------- #
# band pass/fail behavior
# --------------------------------------------------------------------------- #


def test_flat_curve_passes_all_bands():
    report = evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0))

    assert isinstance(report, FlatSpecReport)
    assert report.reference_db == 0.0
    assert [band.n_bins for band in report.bands] == [4, 4, 4]
    for band in report.bands:
        assert isinstance(band, BandResult)
        assert band.evaluable is True
        assert band.max_deviation_db == 0.0
        assert band.rms_deviation_db == 0.0
        assert band.max_deviation_hz is not None
        assert band.f_lo_hz <= band.max_deviation_hz < band.f_hi_hz
        assert band.n_excluded == 0
        assert band.passed is True
    assert report.overall_passed is True
    assert report.excluded_intervals == ()


def test_bump_confined_to_band1_fails_only_that_band():
    db = _flat_db(0.0)
    db[3] = 2.6  # 1500 Hz, inside band1 (250-2000 Hz)

    report = evaluate_flat_spec(_FREQS_HZ, db)

    values = db.tolist()
    expected_reference_db = _hand_power_mean_db([values[i] for i in _REF_IDX])
    expected_band1_max = _signed_max_deviation(
        [values[i] for i in _BAND1_IDX], expected_reference_db
    )
    expected_band1_rms = _rms_deviation([values[i] for i in _BAND1_IDX], expected_reference_db)
    expected_band2_max = _signed_max_deviation(
        [values[i] for i in _BAND2_IDX], expected_reference_db
    )
    expected_band3_max = _signed_max_deviation(
        [values[i] for i in _BAND3_IDX], expected_reference_db
    )

    assert report.reference_db == pytest.approx(expected_reference_db, abs=1e-9)

    band1, band2, band3 = report.bands
    assert band1.max_deviation_db == pytest.approx(expected_band1_max, abs=1e-9)
    assert band1.rms_deviation_db == pytest.approx(expected_band1_rms, abs=1e-9)
    assert band1.passed is False  # +2.6 dB exceeds band1's +/-1.5 dB tolerance
    # The +2.6 dB bump is a LOUD excursion, and the report says so and names
    # where -- the sign and the frequency are the actionable half of "this
    # band failed".
    assert band1.max_deviation_db > 0.0
    assert band1.max_deviation_hz == 1500.0
    assert band2.max_deviation_db == pytest.approx(expected_band2_max, abs=1e-9)
    assert band2.passed is True
    assert band3.max_deviation_db == pytest.approx(expected_band3_max, abs=1e-9)
    assert band3.passed is True
    assert report.overall_passed is False


def test_max_deviation_keeps_the_sign_of_a_dip():
    """The companion of the test above: an equally-large *quiet* excursion
    must report a NEGATIVE max_deviation_db, or the field is just a
    magnitude wearing a signed type.
    """
    quiet = _flat_db(0.0)
    quiet[1] = -4.0  # 600 Hz
    loud = _flat_db(0.0)
    loud[1] = 4.0

    quiet_band1 = evaluate_flat_spec(_FREQS_HZ, quiet).bands[0]
    loud_band1 = evaluate_flat_spec(_FREQS_HZ, loud).bands[0]

    assert quiet_band1.max_deviation_db < 0.0
    assert loud_band1.max_deviation_db > 0.0
    assert quiet_band1.max_deviation_hz == 600.0
    assert loud_band1.max_deviation_hz == 600.0
    # Both fail -- the tolerance is symmetric, only the reported sign differs.
    assert quiet_band1.passed is False
    assert loud_band1.passed is False


def test_dip_confined_to_band2_fails_only_that_band():
    """A -3.5 dB dip in the 2-8 kHz band (band2), against that band's
    +/-2.0 dB tolerance -- an unambiguous fail for band2 alone.

    REFERENCE_BAND_HZ is now band1 (250-2000 Hz) alone, so a dip confined to
    band2 never touches the reference-band bins at all: band1/band3 pass
    from their own untouched 0 dB bins, with no reference-level shift in
    play (unlike the pre-#1857-anchor days, when band2 was itself inside
    the pooled reference and a dip there would shrink its own apparent
    deviation slightly).
    """
    db = _flat_db(0.0)
    db[6] = -3.5  # 5000 Hz, inside band2 (2000-8000 Hz)

    report = evaluate_flat_spec(_FREQS_HZ, db)

    values = db.tolist()
    expected_reference_db = _hand_power_mean_db([values[i] for i in _REF_IDX])
    expected_band2_max = _signed_max_deviation(
        [values[i] for i in _BAND2_IDX], expected_reference_db
    )

    assert report.reference_db == pytest.approx(expected_reference_db, abs=1e-9)

    band1, band2, band3 = report.bands
    assert band2.max_deviation_db == pytest.approx(expected_band2_max, abs=1e-9)
    assert band2.max_deviation_db < 0.0  # a dip, and the report says so
    assert band2.max_deviation_hz == 5000.0
    assert band1.passed is True
    assert band2.passed is False  # -3.5 dB clears the +/-2.0 dB band2 tolerance
    assert band3.passed is True
    assert report.overall_passed is False


# --------------------------------------------------------------------------- #
# band-membership edge rule: f_lo <= f < f_hi
# --------------------------------------------------------------------------- #


def test_boundary_bin_at_2000hz_lands_in_second_band():
    """A bin at exactly 2000 Hz must land in the 2-8 kHz band (tol 2.0),
    not 250 Hz-2 kHz (tol 1.5). A +5.0 dB bump placed only at that bin
    must show up as band2's deviation, not band1's."""
    db = _flat_db(0.0)
    db[4] = 5.0  # exactly 2000 Hz

    report = evaluate_flat_spec(_FREQS_HZ, db)

    values = db.tolist()
    expected_reference_db = _hand_power_mean_db([values[i] for i in _REF_IDX])
    expected_band1_max = _signed_max_deviation(
        [values[i] for i in _BAND1_IDX], expected_reference_db
    )
    expected_band2_max = _signed_max_deviation(
        [values[i] for i in _BAND2_IDX], expected_reference_db
    )

    band1, band2, band3 = report.bands
    assert band1.max_deviation_db == pytest.approx(expected_band1_max, abs=1e-9)
    assert band2.max_deviation_db == pytest.approx(expected_band2_max, abs=1e-9)

    # The 5.0 dB spike sits at 2000 Hz -- it must land in band2, not band1.
    # REFERENCE_BAND_HZ is band1 alone now, so a spike confined to band2
    # never touches the reference: band1's own bins read exactly 0 dB,
    # undisturbed.
    assert abs(band1.max_deviation_db) < 1.5
    assert band1.passed is True
    assert band2.max_deviation_db > 2.0
    assert band2.max_deviation_hz == 2000.0
    assert band2.passed is False


def test_boundary_bin_at_8000hz_lands_in_third_band():
    """Same edge rule at the 8 kHz seam: a bin at exactly 8000 Hz belongs
    to the 8-16 kHz band (tol 2.5), not 2-8 kHz (tol 2.0), and it sits
    OUTSIDE REFERENCE_BAND_HZ (250-8000 Hz, exclusive-upper) so it does
    not perturb the reference at all -- this boundary case is exact, no
    floating-point approximation needed anywhere in the assertions."""
    db = _flat_db(0.0)
    db[8] = 5.0  # exactly 8000 Hz

    report = evaluate_flat_spec(_FREQS_HZ, db)
    band1, band2, band3 = report.bands

    assert report.reference_db == 0.0  # 8000 Hz is outside REFERENCE_BAND_HZ
    assert band1.max_deviation_db == 0.0
    assert band2.max_deviation_db == 0.0
    assert band2.passed is True
    assert band3.max_deviation_db == 5.0
    assert band3.passed is False  # 5.0 dB exceeds band3's +/-2.5 dB tolerance


# --------------------------------------------------------------------------- #
# exclusion handling
# --------------------------------------------------------------------------- #


def test_deep_dip_inside_excluded_bin_does_not_fail_band_or_appear_in_max_deviation():
    db = _flat_db(0.0)
    db[6] = -20.0  # 5000 Hz, inside band2 -- would fail badly if counted
    exclusion_mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    exclusion_mask[6] = True

    report = evaluate_flat_spec(_FREQS_HZ, db, exclusion_mask)

    # Every OTHER reference-band bin is still 0 dB, so excluding the
    # -20 dB bin from the reference computation leaves an exact 0.0 dB
    # reference.
    assert report.reference_db == 0.0

    band1, band2, band3 = report.bands
    assert band2.n_bins == 4
    assert band2.n_excluded == 1
    assert band2.evaluable is True  # 3 of 4 bins survived
    # The -20 dB dip must not appear here at all:
    assert band2.max_deviation_db == 0.0
    assert band2.rms_deviation_db == 0.0
    assert band2.max_deviation_hz != 5000.0
    assert band2.passed is True
    assert report.overall_passed is True


def test_excluded_intervals_merge_contiguous_runs():
    exclusion_mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    exclusion_mask[5] = True   # 3000 Hz
    exclusion_mask[6] = True   # 5000 Hz -- adjacent index to 5000 Hz -> merges
    exclusion_mask[12] = True  # 16000 Hz -- isolated -> its own interval

    report = evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0), exclusion_mask)

    assert report.excluded_intervals == (
        (3000.0, 5000.0),
        (16000.0, 16000.0),
    )


def test_excluded_intervals_empty_when_nothing_excluded():
    report = evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0))
    assert report.excluded_intervals == ()


# --------------------------------------------------------------------------- #
# to_dict() -- stability, keys, types
# --------------------------------------------------------------------------- #


def test_to_dict_round_trip_stability_keys_and_types():
    db = _flat_db(0.0)
    exclusion_mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    exclusion_mask[6] = True  # gives a non-empty excluded_intervals too

    report = evaluate_flat_spec(_FREQS_HZ, db, exclusion_mask)
    d = report.to_dict()

    assert set(d.keys()) == {
        "reference_db",
        "bands",
        "overall_passed",
        "excluded_intervals",
        "best_effort_above_hz",
        "smoothing_fraction",
        # #2551: the honesty floor these numbers were graded at, and the span
        # `reference_db` was pooled over once that floor applied.
        "trusted_floor_hz",
        "reference_band_hz",
        # The ceiling's mirror of the floor pair above, and the whole span
        # the table graded.
        "trusted_ceiling_hz",
        "graded_band_hz",
        # #3495: the ROOM's floor and where it came from. Echoed like the
        # clamps above, but it clamps nothing -- it marks bands.
        "entanglement_floor_hz",
        "entanglement_floor_source",
        # The frame the bands' gate fields are stated in. Stamped by a reader
        # holding the round's captures, never by this module.
        "gate_sweep_frame",
    }
    assert type(d["reference_db"]) is float
    assert type(d["overall_passed"]) is bool
    assert type(d["best_effort_above_hz"]) is float
    assert d["best_effort_above_hz"] == 16000.0
    assert type(d["smoothing_fraction"]) is int
    assert d["smoothing_fraction"] == 3
    # No floor supplied here, so "not stated" — never a fabricated 0.0 — and
    # the reference band is the module constant, JSON-shaped as a list.
    assert d["trusted_floor_hz"] is None
    assert d["reference_band_hz"] == list(REFERENCE_BAND_HZ)
    assert all(type(v) is float for v in d["reference_band_hz"])
    # No ceiling supplied either, so the graded span runs the module's own
    # nominal bottom-to-best-effort range.
    assert d["trusted_ceiling_hz"] is None
    assert d["graded_band_hz"] == [SPEC_BANDS[0][0], BEST_EFFORT_ABOVE_HZ]
    assert all(type(v) is float for v in d["graded_band_hz"])
    # No entanglement floor supplied.
    assert d["entanglement_floor_hz"] is None
    assert d["entanglement_floor_source"] == gating.ENTANGLEMENT_SOURCE_UNKNOWN
    # No sweep has read this report, so it carries no frame and no band
    # carries a gate number -- pinned below beside the band keys.
    assert d["gate_sweep_frame"] is None

    assert isinstance(d["bands"], list)
    assert len(d["bands"]) == len(SPEC_BANDS)
    for band_dict in d["bands"]:
        assert set(band_dict.keys()) == {
            "f_lo_hz",
            "f_hi_hz",
            "tolerance_db",
            "max_deviation_db",
            "max_deviation_hz",
            "rms_deviation_db",
            "n_bins",
            "n_excluded",
            "evaluable",
            "passed",
            # The #1857 attribution split -- disclosure beside the graded
            # numbers, never an input to them. See
            # tests/test_flat_spec_attribution.py.
            "level_deviation_db",
            "max_ripple_db",
            "max_ripple_hz",
            # #2551: the edges these numbers came from, beside the nominal ones.
            "graded_lo_hz",
            "graded_hi_hz",
            # #2599: whether that edge is where the reported extremum landed,
            # making it a lower bound on the band's real worst deviation.
            "max_at_graded_edge",
            # #3495: where inside this band the ROOM, not the window choice,
            # bounds what a gate can separate. Disclosure beside the grade.
            "room_entangled_below_hz",
            # The gate ladder's read at THIS band's own worst bin -- room or
            # speaker. Stamped afterwards by a reader holding the round's
            # captures (`round_views.spec_with_gate_sensitivity`), so this
            # module always emits them as None.
            "gate_sensitivity_db",
            "sigma_growth_ratio",
            "n_valid_rungs",
            "gate_sensitivity_note",
            "gate_sensitivity_detail",
            # #3564: the ladder's own room/speaker verdict at that same bin,
            # beside the numbers it was read from.
            "gate_window_verdict",
            "gate_window_verdict_reasons",
        }
        assert band_dict["gate_sensitivity_db"] is None
        assert band_dict["sigma_growth_ratio"] is None
        assert band_dict["n_valid_rungs"] is None
        assert band_dict["gate_sensitivity_note"] is None
        assert band_dict["gate_sensitivity_detail"] is None
        assert band_dict["gate_window_verdict"] is None
        assert band_dict["gate_window_verdict_reasons"] is None
        for key in (
            "f_lo_hz",
            "f_hi_hz",
            "tolerance_db",
            "max_deviation_db",
            "max_deviation_hz",
            "rms_deviation_db",
            "level_deviation_db",
            "max_ripple_db",
            "max_ripple_hz",
            "graded_lo_hz",
            "graded_hi_hz",
        ):
            assert type(band_dict[key]) is float, key
        for key in ("n_bins", "n_excluded"):
            assert type(band_dict[key]) is int, key
        assert type(band_dict["evaluable"]) is bool
        assert type(band_dict["passed"]) is bool
        # A real `bool`, not `np.bool_` -- the latter passes `isinstance` and
        # then breaks `json.dumps`, which is the trap this whole block exists
        # for. No floor was supplied to this report, so every band is
        # untruncated and the honest value is False rather than None.
        assert type(band_dict["max_at_graded_edge"]) is bool
        assert band_dict["max_at_graded_edge"] is False

    assert d["excluded_intervals"] == [[5000.0, 5000.0]]
    assert isinstance(d["excluded_intervals"], list)
    assert all(isinstance(interval, list) for interval in d["excluded_intervals"])

    # Round-trip / determinism: same report, same inputs -> identical dict.
    assert report.to_dict() == d
    report_again = evaluate_flat_spec(_FREQS_HZ, db, exclusion_mask)
    assert report_again.to_dict() == d

    # Later /state consumption needs this JSON-safe -- no numpy scalar
    # leakage (np.int64 / np.bool_ both fail json.dumps; np.float64
    # happens to subclass float so it would NOT be caught here, which is
    # exactly why the `type(...) is float` checks above exist too).
    json.dumps(d)


def test_from_dict_round_trips_and_field_count_is_pinned():
    """The inverse of `to_dict`: `from_dict(report.to_dict()) == report`.

    A caller that persists these reports (issue #2769's round-grading views
    among them) needs one owner for this round trip rather than a private
    per-caller rehydration -- the trap that motivated `from_dict` existing at
    all was two independent copies of this exact logic already living in the
    repo, each one more chance to drift from `to_dict`'s actual shape.

    Two tripwires, not one: the field COUNT of each dataclass is pinned, so
    a field added to `BandResult` or `FlatSpecReport` without a matching
    `to_dict`/`from_dict` update fails this test immediately by count alone;
    and every field is given a NON-DEFAULT value before the round trip, so a
    field silently dropped by `to_dict` (and therefore invisible to
    `from_dict`) produces a report that differs from the original rather
    than one that happens to match by coincidence of a shared default.
    """
    import dataclasses

    assert len(dataclasses.fields(BandResult)) == 24
    assert len(dataclasses.fields(FlatSpecReport)) == 13

    band = BandResult(
        f_lo_hz=100.0, f_hi_hz=200.0, tolerance_db=1.5,
        max_deviation_db=-2.0, max_deviation_hz=150.0, rms_deviation_db=1.0,
        n_bins=10, n_excluded=1, evaluable=True, passed=False,
        level_deviation_db=0.5, max_ripple_db=-1.5, max_ripple_hz=160.0,
        graded_lo_hz=110.0, graded_hi_hz=190.0, max_at_graded_edge=True,
        room_entangled_below_hz=150.0,
        gate_sensitivity_db=-1.25, sigma_growth_ratio=2.4, n_valid_rungs=5,
        gate_sensitivity_note="short_rung_sigma_is_zero",
        gate_sensitivity_detail={"reason": "round_no_captures", "round_dir": "/r"},
        gate_window_verdict="moved",
        gate_window_verdict_reasons=("sigma_growth", "depth_delta"),
    )
    report = FlatSpecReport(
        reference_db=-20.0, bands=(band,), overall_passed=False,
        excluded_intervals=((300.0, 310.0),), best_effort_above_hz=16000.0,
        smoothing_fraction=6, trusted_floor_hz=142.86,
        reference_band_hz=(250.0, 8000.0),
        trusted_ceiling_hz=15000.0,
        graded_band_hz=(142.86, 15000.0),
        entanglement_floor_hz=1000.0,
        entanglement_floor_source=gating.ENTANGLEMENT_SOURCE_DECLARED,
        gate_sweep_frame={"rungs_ms": [3.0, 20.0], "n_fft": 65536},
    )
    assert FlatSpecReport.from_dict(report.to_dict()) == report

    # A report predating the #1857/#2551 optional fields (all five `None` on
    # BandResult, `trusted_floor_hz=None`, default `reference_band_hz`)
    # rehydrates to the dataclass's own defaults, not a fabricated value.
    bare_band = BandResult(
        f_lo_hz=100.0, f_hi_hz=200.0, tolerance_db=1.5,
        max_deviation_db=-2.0, max_deviation_hz=150.0, rms_deviation_db=1.0,
        n_bins=10, n_excluded=1, evaluable=True, passed=False,
    )
    bare_report = FlatSpecReport(
        reference_db=-20.0, bands=(bare_band,), overall_passed=False,
        excluded_intervals=(), best_effort_above_hz=16000.0, smoothing_fraction=3,
    )
    assert FlatSpecReport.from_dict(bare_report.to_dict()) == bare_report


def test_from_dict_raises_on_a_document_missing_an_original_vintage_field():
    """The mutation this guards against: an earlier version of ``from_dict``
    read every field with ``.get()``, so a document missing an
    original-vintage (#1741) field silently rehydrated a plausible-looking
    default instead of raising -- ``excluded_intervals`` defaulted to ``()``
    (reads as "nothing was excluded" when the truth is "the field was
    lost"), and ``BandResult.passed`` defaulted to ``None`` (reads as
    "unevaluable" when the truth is the same). Both must now raise
    ``KeyError``, not rehydrate.
    """
    band = BandResult(
        f_lo_hz=100.0, f_hi_hz=200.0, tolerance_db=1.5,
        max_deviation_db=-2.0, max_deviation_hz=150.0, rms_deviation_db=1.0,
        n_bins=10, n_excluded=1, evaluable=True, passed=False,
    )
    report = FlatSpecReport(
        reference_db=-20.0, bands=(band,), overall_passed=False,
        excluded_intervals=((300.0, 310.0),), best_effort_above_hz=16000.0,
        smoothing_fraction=3,
    )
    raw = report.to_dict()
    assert "excluded_intervals" in raw  # sanity: the field really is there to remove

    corrupted = dict(raw)
    del corrupted["excluded_intervals"]
    with pytest.raises(KeyError, match="excluded_intervals"):
        FlatSpecReport.from_dict(corrupted)

    band_raw = dict(raw["bands"][0])
    del band_raw["passed"]
    with pytest.raises(KeyError, match="passed"):
        BandResult.from_dict(band_raw)


# --------------------------------------------------------------------------- #
# degenerate inputs -> ValueError, never a silent pass
# --------------------------------------------------------------------------- #


def test_empty_arrays_raise_value_error():
    with pytest.raises(ValueError, match="must not be empty"):
        evaluate_flat_spec(np.array([]), np.array([]))


def test_non_1d_arrays_raise_value_error():
    with pytest.raises(ValueError, match="must be 1-D arrays"):
        evaluate_flat_spec(np.zeros((3, 3)), np.zeros((3, 3)))


def test_mismatched_lengths_raise_value_error():
    with pytest.raises(ValueError, match="does not match"):
        evaluate_flat_spec(np.array([1000.0, 2000.0, 3000.0]), np.array([0.0, 0.0]))


def test_exclusion_mask_length_mismatch_raises_value_error():
    with pytest.raises(ValueError, match="exclusion_mask shape"):
        evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0), np.zeros(5, dtype=bool))


def test_band_with_no_frequency_coverage_is_unevaluable_not_fatal():
    """An axis that never reaches 8 kHz is a narrower instrument, not a
    broken report -- band3 is unevaluable and the other two still say what
    they measured.
    """
    freqs_hz = np.array([300.0, 600.0, 1000.0, 1500.0, 2000.0, 3000.0, 5000.0, 7000.0])
    report = evaluate_flat_spec(freqs_hz, np.zeros(freqs_hz.shape))

    band1, band2, band3 = report.bands
    assert (band3.n_bins, band3.n_excluded) == (0, 0)
    assert band3.evaluable is False
    assert band3.passed is None
    assert band1.evaluable is True and band1.passed is True
    assert band2.evaluable is True and band2.passed is True
    assert report.overall_passed is False


def test_fully_excluded_band_is_unevaluable_and_never_silently_passes():
    """S8 -- the honesty screen must not be able to *hide* a band.

    Every bin in 8-16 kHz is interference-flagged. Two failure modes are
    both unacceptable and both excluded here: raising (one band losing its
    evidence would destroy the whole report, including the two bands that
    measured fine), and quietly reporting the band as passed (the screen
    would then be a way to make any band disappear from pass/fail).

    The band is reported as unevaluable with ``None`` metrics, the other two
    bands are untouched, and ``overall_passed`` is False -- an unmeasured
    band is never a clean one.
    """
    exclusion_mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    exclusion_mask[8:12] = True  # every band3 bin
    db = _flat_db(0.0)
    db[8:12] = -30.0  # ...and they are dreadful, which must not leak out

    report = evaluate_flat_spec(_FREQS_HZ, db, exclusion_mask)

    band1, band2, band3 = report.bands
    assert band3.evaluable is False
    assert band3.passed is None
    assert band3.max_deviation_db is None
    assert band3.max_deviation_hz is None
    assert band3.rms_deviation_db is None
    assert (band3.n_bins, band3.n_excluded) == (4, 4)
    assert (band3.f_lo_hz, band3.f_hi_hz, band3.tolerance_db) == (8000.0, 16000.0, 2.5)

    # The evaluated bands are intact and unaffected by the excluded band.
    assert report.reference_db == 0.0
    for band in (band1, band2):
        assert band.evaluable is True
        assert band.passed is True
        assert band.max_deviation_db == 0.0
        assert band.rms_deviation_db == 0.0

    assert report.overall_passed is False, (
        "an unevaluable band must never read as a clean bill of health"
    )
    assert report.excluded_intervals == ((8000.0, 15000.0),)

    # JSON-safe, with the Nones rendering as null.
    d = report.to_dict()
    assert d["bands"][2]["passed"] is None
    assert d["bands"][2]["max_deviation_db"] is None
    assert d["bands"][2]["evaluable"] is False
    assert json.loads(json.dumps(d))["bands"][2]["rms_deviation_db"] is None


def test_the_most_bands_that_can_be_unevaluable_at_once_is_two():
    """The degenerate end of the same rule, and the structural reason the
    reference band is the one case that still raises.

    ``REFERENCE_BAND_HZ`` is now exactly band1 (250-2000 Hz), so a mask that
    empties band1 always empties the reference band too -- and with no
    reference level there is nothing to compute a deviation against
    anywhere, so that raises before a report is ever produced. Band2 and
    band3 carry no such protection and can both go empty at once, which is
    therefore the worst case that still survives as a report: the one band
    that kept a bin (band1, which is also the reference) still reports, and
    the overall verdict is False.
    """
    exclusion_mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    exclusion_mask[:12] = True  # every bin in all three spec bands
    exclusion_mask[0] = False  # ...except one, so the reference is computable

    report = evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0), exclusion_mask)

    assert [band.evaluable for band in report.bands] == [True, False, False]
    assert [band.passed for band in report.bands] == [True, None, None]
    assert report.overall_passed is False

    # Take that last bin away and there is no reference at all -- raise.
    exclusion_mask[0] = True
    with pytest.raises(
        ValueError, match=r"reference band 250\.0-2000\.0 Hz has zero non-excluded bins"
    ):
        evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0), exclusion_mask)


def test_non_ascending_freqs_raise_value_error():
    """S3 -- required, not assumed. The merged exclusion intervals treat
    index adjacency as frequency adjacency, which is only true on a sorted
    axis; a descending or duplicated axis is a caller bug worth hearing
    about, not a plausible-looking report. Mirrors
    ``spatial_combine._validate_capture``'s strictly-increasing check.
    """
    descending = _FREQS_HZ[::-1].copy()
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_flat_spec(descending, _flat_db(0.0))

    duplicated = _FREQS_HZ.copy()
    duplicated[5] = duplicated[4]  # a repeated frequency is not ascending
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_flat_spec(duplicated, _flat_db(0.0))

    shuffled = _FREQS_HZ.copy()
    shuffled[[2, 3]] = shuffled[[3, 2]]
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_flat_spec(shuffled, _flat_db(0.0))


def test_smoothing_fraction_is_recorded_verbatim_as_caller_attestation():
    """S4 -- provenance the arrays cannot carry.

    The plan evaluates pass/fail at 1/3-octave, but a bare magnitude array
    has no evidence of how it was smoothed and this module deliberately does
    not smooth. The value is therefore recorded exactly as handed over and
    changes nothing about the evaluation -- which is the point, and is what
    these assertions pin.
    """
    db = _flat_db(0.0)
    default = evaluate_flat_spec(_FREQS_HZ, db)
    assert default.smoothing_fraction == 3
    assert default.to_dict()["smoothing_fraction"] == 3

    attested = evaluate_flat_spec(_FREQS_HZ, db, smoothing_fraction=6)
    assert attested.smoothing_fraction == 6
    assert attested.to_dict()["smoothing_fraction"] == 6

    # Attestation only: the verdict is byte-identical either way.
    assert [band.to_dict() for band in attested.bands] == [
        band.to_dict() for band in default.bands
    ]
    assert attested.reference_db == default.reference_db
    assert attested.overall_passed == default.overall_passed


def test_merged_interval_rule_has_exactly_one_owner():
    """S5 -- the combiner's reported exclusion intervals and this module's
    are the same fact, so they are computed by the same function rather than
    by two implementations that can drift.
    """
    assert flat_spec.merged_true_intervals is merged_true_intervals
    assert not hasattr(flat_spec, "_merged_excluded_intervals")

    exclusion_mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    exclusion_mask[[5, 6, 12]] = True
    report = evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0), exclusion_mask)
    assert report.excluded_intervals == merged_true_intervals(_FREQS_HZ, exclusion_mask)


def test_reference_band_with_no_coverage_raises_value_error():
    freqs_hz = np.array([9000.0, 10000.0, 12000.0, 15000.0])  # all >= 8000 Hz
    spec_smoothed_db = np.zeros(freqs_hz.shape)
    with pytest.raises(
        ValueError, match=r"reference band 250\.0-2000\.0 Hz has zero non-excluded bins"
    ):
        evaluate_flat_spec(freqs_hz, spec_smoothed_db)


def test_non_finite_freqs_raise_value_error():
    freqs_hz = _FREQS_HZ.copy()
    freqs_hz[0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        evaluate_flat_spec(freqs_hz, _flat_db(0.0))


def test_non_finite_spec_db_raise_value_error():
    db = _flat_db(0.0)
    db[0] = np.inf
    with pytest.raises(ValueError, match="finite"):
        evaluate_flat_spec(_FREQS_HZ, db)


# --------------------------------------------------------------------------- #
# the trusted-floor intersection (issue #2551)
# --------------------------------------------------------------------------- #
#
# `_FREQS_HZ`'s band-1 bins are 300 / 600 / 1000 / 1500 Hz, so a 700 Hz floor
# splits that band cleanly in half — two bins below it, two above — and the
# reference band (indices 0-3, now exactly band1) loses exactly the same two.
_CLAMP_FLOOR_HZ = 700.0
_CLAMPED_BAND1_IDX = [2, 3]
_CLAMPED_REF_IDX = list(range(2, 4))


def test_the_trusted_floor_raises_every_bands_lower_edge():
    """#2551 item 1. A band is graded from ``max(f_lo, trusted_floor_hz)``,
    and the metrics come from that span and no other.

    The two sub-floor bins carry a 20 dB spike here, so a grader that ignored
    the floor could not possibly produce the same numbers as one that
    honoured it — the assertion is not vacuous on a flat curve."""
    db = _flat_db(0.0)
    db[0] = 20.0   # 300 Hz — below the floor
    db[1] = 20.0   # 600 Hz — below the floor
    report = evaluate_flat_spec(_FREQS_HZ, db, trusted_floor_hz=_CLAMP_FLOOR_HZ)

    reference_db = _hand_power_mean_db([db[i] for i in _CLAMPED_REF_IDX])
    assert report.reference_db == pytest.approx(reference_db)

    low = report.bands[0]
    assert low.f_lo_hz == 250.0            # the NOMINAL row is still named
    assert low.graded_lo_hz == 700.0       # ...beside the edge actually used
    assert low.evaluable is True
    assert low.n_bins == len(_CLAMPED_BAND1_IDX)
    assert low.max_deviation_db == pytest.approx(
        _signed_max_deviation([db[i] for i in _CLAMPED_BAND1_IDX], reference_db)
    )
    assert low.rms_deviation_db == pytest.approx(
        _rms_deviation([db[i] for i in _CLAMPED_BAND1_IDX], reference_db)
    )
    # The 20 dB spike is below the floor, so it cannot be the worst bin and
    # cannot fail the band.
    assert low.max_deviation_hz >= 700.0
    assert low.passed is True

    # Bands entirely above the floor keep their nominal edge, unmoved.
    for band in report.bands[1:]:
        assert band.graded_lo_hz == band.f_lo_hz

    assert report.trusted_floor_hz == 700.0


def test_the_trusted_floor_raises_the_reference_bands_lower_edge_too():
    """#2551 item 2. The reference is a power mean, so leaving sub-floor bins
    in the frame while removing them from every band would let untrustworthy
    energy re-centre the zero each surviving deviation is stated against.

    The clamp therefore MOVES the reference — asserted against a hand mean
    over the surviving bins, and against the unclamped mean being different —
    and the report publishes the span it was pooled over."""
    db = _flat_db(0.0)
    db[0] = 20.0
    db[1] = 20.0

    unclamped = evaluate_flat_spec(_FREQS_HZ, db)
    clamped = evaluate_flat_spec(_FREQS_HZ, db, trusted_floor_hz=_CLAMP_FLOOR_HZ)

    assert unclamped.reference_db == pytest.approx(
        _hand_power_mean_db([db[i] for i in _REF_IDX])
    )
    assert clamped.reference_db == pytest.approx(
        _hand_power_mean_db([db[i] for i in _CLAMPED_REF_IDX])
    )
    assert clamped.reference_db != unclamped.reference_db

    assert unclamped.reference_band_hz == REFERENCE_BAND_HZ
    assert clamped.reference_band_hz == (700.0, REFERENCE_BAND_HZ[1])
    # And the gauge names the frame that was USED, not the module constant.
    assert flat_spec.spec_flatness_gauge(clamped).reference_band_hz == (
        700.0, REFERENCE_BAND_HZ[1]
    )


def test_a_band_wholly_outside_the_trusted_range_is_unevaluable_never_failed():
    """#2551 item 1's honesty half. No evidence is not a failure.

    ``graded_lo_hz >= graded_hi_hz`` is the tell that separates this from
    "the axis never reached this band" — that case reports the nominal edge.

    Read through the CEILING rather than the floor, because the floor can no
    longer reach this state: the reference band is ``SPEC_BANDS[0]`` exactly,
    so a floor high enough to swallow the low band swallows the frame with
    it and the evaluator raises instead of reporting (pinned by
    ``test_a_floor_at_or_above_the_reference_band_top_raises``). The claim is
    about a clamp swallowing a band, not about which clamp did it."""
    report = evaluate_flat_spec(
        _FREQS_HZ, _flat_db(0.0), trusted_ceiling_hz=8000.0,
    )
    top = report.bands[2]
    assert top.f_hi_hz == 16000.0
    assert top.graded_hi_hz == 8000.0
    assert top.graded_lo_hz is not None and top.graded_lo_hz >= top.graded_hi_hz
    assert top.evaluable is False
    assert top.passed is None          # never False
    assert top.max_deviation_db is None
    assert top.rms_deviation_db is None
    assert top.n_bins == 0
    assert top.n_excluded == 0
    # ...and an unevaluable band still cannot be mistaken for a clean one.
    assert report.overall_passed is False
    assert all(b.evaluable for b in report.bands[:2])

    # Contrast: no-coverage-on-the-axis reports the NOMINAL edges, because
    # nothing clamped them.
    sparse = evaluate_flat_spec(_FREQS_HZ[:8], _flat_db(0.0)[:8])
    assert sparse.bands[2].evaluable is False
    assert sparse.bands[2].graded_lo_hz == sparse.bands[2].f_lo_hz
    assert sparse.bands[2].graded_hi_hz == sparse.bands[2].f_hi_hz


def test_a_band_failing_above_the_trusted_floor_still_fails():
    """The clamp must not become a way to stop failing. A band entirely above
    the floor and genuinely out of tolerance keeps ``passed=False``;
    ``evaluable=False`` is reserved for absent evidence and is never borrowed
    to soften a verdict."""
    db = _flat_db(0.0)
    for i in _BAND3_IDX:
        db[i] = -6.0  # 8-16 kHz, far above any floor asserted here
    report = evaluate_flat_spec(_FREQS_HZ, db, trusted_floor_hz=_CLAMP_FLOOR_HZ)
    top = report.bands[2]
    assert top.graded_lo_hz == top.f_lo_hz
    assert top.evaluable is True
    assert top.passed is False
    assert abs(top.max_deviation_db) > top.tolerance_db
    assert report.overall_passed is False


def test_no_floor_or_an_unusable_floor_clamps_nothing():
    """``None`` is "no floor was measured", and a non-finite one is the same
    statement arriving badly — both grade exactly as the pre-#2551 evaluator
    did rather than silently clamping at zero or at NaN.

    The NaN case is why the guard is a finiteness check and not a bare
    ``max()``: ``max(250.0, nan)`` returns whichever argument came first."""
    db = _flat_db(0.0)
    db[0] = 20.0
    baseline = json.dumps(evaluate_flat_spec(_FREQS_HZ, db).to_dict(), sort_keys=True)
    for floor in (None, float("nan"), float("inf")):
        report = evaluate_flat_spec(_FREQS_HZ, db, trusted_floor_hz=floor)
        assert json.dumps(report.to_dict(), sort_keys=True) == baseline, floor
        assert report.trusted_floor_hz is None, floor
        assert report.bands[0].graded_lo_hz == 250.0, floor


def test_a_floor_at_or_above_the_reference_band_top_raises():
    """The documented raise, at the documented boundary. With no reference
    level there is nothing to state a deviation against anywhere, so the
    honest answer is "this capture cannot be graded" rather than a report of
    three unevaluable bands with a fabricated frame.

    The wiring layer's own fail-soft turns this into an unavailable cloud
    block; it is not a crash path."""
    with pytest.raises(
        ValueError, match=r"reference band 2000\.0-2000\.0 Hz has zero non-excluded"
    ):
        evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0), trusted_floor_hz=2000.0)


@pytest.mark.parametrize("ceiling_hz", [20000.0, 12000.0])
def test_the_top_scored_band_and_best_effort_both_end_at_the_ceiling(ceiling_hz):
    """The session's trusted ceiling IS where grading stops.

    ``SPEC_BANDS[-1]``'s upper edge and the best-effort boundary are one
    number, so they move together — up on a microphone trusted past the
    nominal 16 kHz and down on one trusted below it. Two ceilings, one above
    the nominal edge and one below, because a clamp that only ever narrowed
    would leave the whole 16-20 kHz gap ungraded on the tier that motivated
    this.
    """
    report = evaluate_flat_spec(
        _FREQS_HZ, _flat_db(0.0), trusted_ceiling_hz=ceiling_hz,
    )
    top = report.bands[-1]

    assert top.f_hi_hz == 16000.0            # the NOMINAL row is still named
    assert top.graded_hi_hz == ceiling_hz
    assert report.best_effort_above_hz == ceiling_hz
    assert report.graded_band_hz == (250.0, ceiling_hz)
    assert report.trusted_ceiling_hz == ceiling_hz
    # Bins between the nominal edge and a higher ceiling are GRADED now, and
    # bins at or above the ceiling still are not.
    assert all(
        (f < ceiling_hz) == bool(top.graded_lo_hz <= f < top.graded_hi_hz)
        for f in _FREQS_HZ[_FREQS_HZ >= 8000.0]
    )
    # The lower bands are untouched: nothing here widens 250-2000 Hz.
    assert [(b.f_lo_hz, b.graded_hi_hz) for b in report.bands[:2]] == [
        (250.0, 2000.0), (2000.0, 8000.0),
    ]


def test_no_ceiling_grades_the_nominal_table_exactly_as_before():
    """The default is the pre-clamp behaviour, bit for bit — an absent
    ceiling is "not stated", never a ceiling of zero and never a widening."""
    stated = evaluate_flat_spec(
        _FREQS_HZ, _flat_db(0.0), trusted_ceiling_hz=BEST_EFFORT_ABOVE_HZ,
    )
    absent = evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0))

    assert absent.trusted_ceiling_hz is None
    assert absent.best_effort_above_hz == BEST_EFFORT_ABOVE_HZ
    assert [b.graded_hi_hz for b in absent.bands] == [
        b.graded_hi_hz for b in stated.bands
    ]
    assert absent.to_dict()["bands"] == stated.to_dict()["bands"]


def test_the_reference_cannot_be_moved_by_a_band_above_it():
    """The low-mid anchor's whole point: an elevation confined to 2-8 kHz is
    charged to 2-8 kHz and to nothing else.

    Under the pre-ruling 250 Hz-8 kHz frame this same curve lifted the
    reference and split the elevation three ways — the elevated band read
    HALF its real size while two untouched bands read a deficit that was not
    there. Asserted as attribution (which band carries the number), not as
    prose about the frame.
    """
    hot = _flat_db(0.0)
    for index in _BAND2_IDX:
        hot[index] = 1.0

    flat_report = evaluate_flat_spec(_FREQS_HZ, _flat_db(0.0))
    hot_report = evaluate_flat_spec(_FREQS_HZ, hot)

    assert hot_report.reference_db == pytest.approx(
        flat_report.reference_db, abs=1e-12
    )
    assert [b.max_deviation_db for b in hot_report.bands] == pytest.approx(
        [0.0, 1.0, 0.0], abs=1e-12
    )
    assert [b.level_deviation_db for b in hot_report.bands] == pytest.approx(
        [0.0, 1.0, 0.0], abs=1e-12
    )
    # ...and the elevation does not push an untouched band out of tolerance.
    assert [b.passed for b in hot_report.bands] == [True, True, True]


# --------------------------------------------------------------------------- #
# spec_convergence_residual -- the S3 convergence guard (PR-6a)
# --------------------------------------------------------------------------- #


def _hand_pooled_rms(
    spec_smoothed_db: np.ndarray, reference_db: float, indices: list[int]
) -> float:
    """Independent re-derivation of the pooled residual: RMS deviation over
    the named bins, computed straight from the curve rather than from the
    report's per-band figures the module reassembles."""
    diffs = [float(spec_smoothed_db[i]) - reference_db for i in indices]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


def test_convergence_residual_flat_curve_is_zero():
    report = evaluate_flat_spec(_FREQS_HZ, _flat_db(-30.0))
    residual = spec_convergence_residual(report)
    assert isinstance(residual, ConvergenceResidual)
    assert residual.evaluable is True
    assert residual.rms_db == pytest.approx(0.0, abs=1e-12)
    # Every spec-band bin counted, none excluded; the best-effort bins above
    # 16 kHz are never in scope.
    assert residual.n_bins == len(_BAND1_IDX + _BAND2_IDX + _BAND3_IDX) == 12
    assert residual.n_excluded == 0


def test_convergence_residual_matches_a_hand_computed_pooled_rms():
    """The pooled figure equals the RMS over the union of the spec bands'
    non-excluded bins, recomputed here from the curve. This is what pins the
    ``sqrt(sum(n_b * rms_b**2) / sum(n_b))`` reassembly as exact rather than
    approximately right."""
    db = _flat_db(-30.0)
    db[_BAND1_IDX[1]] += 2.0
    db[_BAND2_IDX[2]] -= 3.0
    db[_BAND3_IDX[0]] += 4.5
    db[13] += 30.0  # best-effort region, must not move the answer

    report = evaluate_flat_spec(_FREQS_HZ, db)
    residual = spec_convergence_residual(report)
    expected = _hand_pooled_rms(
        db, report.reference_db, _BAND1_IDX + _BAND2_IDX + _BAND3_IDX
    )
    assert residual.rms_db == pytest.approx(expected, rel=1e-12)
    assert residual.n_bins == 12


def test_convergence_residual_honors_the_exclusion_mask():
    """A masked bin is not merely down-weighted, it is absent -- so an
    arbitrarily deep excluded dip cannot move the number, and the dropped
    bins are reported."""
    db = _flat_db(-30.0)
    mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    mask[_BAND3_IDX[1]] = True

    clean = spec_convergence_residual(evaluate_flat_spec(_FREQS_HZ, db, mask))

    db[_BAND3_IDX[1]] = -60.0
    dipped = spec_convergence_residual(evaluate_flat_spec(_FREQS_HZ, db, mask))

    assert dipped.rms_db == pytest.approx(clean.rms_db, rel=1e-12)
    assert dipped.rms_db == pytest.approx(0.0, abs=1e-12)
    assert dipped.n_bins == 11
    assert dipped.n_excluded == 1


def test_convergence_residual_counts_excluded_bins_of_an_unevaluable_band():
    """A band the mask emptied contributes nothing to the RMS but its bins
    still show up in ``n_excluded`` -- the whole point of carrying the
    counts."""
    db = _flat_db(-30.0)
    mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    mask[_BAND3_IDX] = True

    report = evaluate_flat_spec(_FREQS_HZ, db, mask)
    assert report.bands[2].evaluable is False

    residual = spec_convergence_residual(report)
    assert residual.evaluable is True
    assert residual.n_bins == 8
    assert residual.n_excluded == 4


def test_convergence_residual_with_no_evaluable_band_is_none_not_zero():
    """"Unevaluable is a first-class outcome" applies here too: no bins
    means no residual, never a residual of 0.0 for a spectrum nothing was
    measured in.

    The report is built by hand because ``evaluate_flat_spec`` **cannot**
    produce this state: :data:`REFERENCE_BAND_HZ` is exactly
    ``SPEC_BANDS[0] union SPEC_BANDS[1]``, so a call that did not raise on
    an empty reference band left at least one counted bin behind. The guard
    exists for a report that arrives from somewhere else -- hand-built here,
    rehydrated from persistence in the plan's PR-6b -- where a
    ZeroDivisionError would be the alternative.
    """
    empty_band = BandResult(
        f_lo_hz=250.0, f_hi_hz=2000.0, tolerance_db=1.5,
        max_deviation_db=None, max_deviation_hz=None, rms_deviation_db=None,
        n_bins=7, n_excluded=7, evaluable=False, passed=None,
    )
    report = FlatSpecReport(
        reference_db=-30.0,
        bands=(empty_band,),
        overall_passed=False,
        excluded_intervals=(),
        best_effort_above_hz=BEST_EFFORT_ABOVE_HZ,
        smoothing_fraction=3,
    )
    residual = spec_convergence_residual(report)
    assert residual.rms_db is None
    assert residual.evaluable is False
    assert residual.n_bins == 0
    assert residual.n_excluded == 7
    assert residual.to_dict() == {
        "rms_db": None, "n_bins": 0, "n_excluded": 7, "evaluable": False,
    }


def test_convergence_residual_ignores_the_best_effort_region():
    """Bins at or above :data:`BEST_EFFORT_ABOVE_HZ` are never specced, so a
    top octave the speaker cannot reach must not be able to stall the loop.
    """
    quiet_top = _flat_db(-30.0)
    loud_top = quiet_top.copy()
    loud_top[12:] += 25.0  # 16 k / 18 k / 20 k -- all best-effort

    assert spec_convergence_residual(
        evaluate_flat_spec(_FREQS_HZ, loud_top)
    ) == spec_convergence_residual(evaluate_flat_spec(_FREQS_HZ, quiet_top))


@requires_s0_curves
def test_s0_convergence_residual_falls_because_the_mask_grew(s0_combined):
    """The S0 reading, and the reason the counts are part of the record.

    Measured on the S0 main leg's ten-position combined spec curve
    (1/3-octave, the curve the plan grades). Three maskings of the SAME
    curve -- the speaker never changed:

      mask                     residual    bins    excluded
      none                     6.4961 dB   10752          0
      power-vs-median screen   6.8226 dB   10616        136
      screen + null registry   5.7705 dB    7678       3074

    Adding the registry drops the residual by 1.05 dB while removing 2938
    bins from the denominator. Read alone that looks like convergence; read
    with the counts it is visibly the 8-16 kHz band losing 54 % of its bins.
    A loop that watched only ``rms_db`` would call that progress.

    RE-PINNED 2026-08-02 (#2045) for PR #1991's prominence vote re-gating
    ``cloud_04`` -- see ``tests._flat_lin_corpus`` "The 2026-08-02 re-pin
    era". Every column moved a little and the LESSON did not move at all,
    which is what this test is for: the residual still falls by ~0.9 dB while
    the denominator loses ~2900 bins, so the drop is still the mask growing
    rather than the speaker improving.

    RE-PINNED AGAIN when ``REFERENCE_BAND_HZ`` narrowed from 250 Hz-8 kHz to
    250 Hz-2 kHz, the low-mid band alone (#1857): all three rows rose,
    because the reference no longer sits among the whole graded span --
    only the low-mid band -- so the other two bands now read further from
    it. The BIN COUNTS in every row are exactly what they were:
    ``REFERENCE_BAND_HZ`` picks which bins define the zero, not which bins
    are in a spec band. The registry's drop is now ~1.05 dB rather than
    ~0.87 dB, and the lesson holds just as hard -- the denominator still
    loses the same ~2900 bins.

    The last row is also the exactness check: the reassembled figure matches
    a direct from-the-arrays recomputation to 1e-12 relative.
    """
    combined, registry = s0_combined
    freqs, spec = combined.freqs_hz, combined.power_mean_spec_db

    readings = {}
    for label, mask in (
        ("none", None),
        ("screen", combined.excluded),
        ("screen_plus_registry", combined.excluded | registry.excluded),
    ):
        readings[label] = spec_convergence_residual(
            evaluate_flat_spec(freqs, spec, mask)
        )

    assert readings["none"].rms_db == pytest.approx(6.4961, abs=0.002)
    assert readings["none"].n_bins == 10_752
    assert readings["none"].n_excluded == 0

    assert readings["screen"].rms_db == pytest.approx(6.8226, abs=0.002)
    assert readings["screen"].n_bins == 10_616
    assert readings["screen"].n_excluded == 136

    both = readings["screen_plus_registry"]
    assert both.rms_db == pytest.approx(5.7705, abs=0.002)
    assert both.n_bins == 7678
    assert both.n_excluded == 3074
    assert readings["screen"].rms_db - both.rms_db == pytest.approx(1.05, abs=0.02)

    # Exactness of the per-band reassembly, against the arrays directly.
    mask = combined.excluded | registry.excluded
    report = evaluate_flat_spec(freqs, spec, mask)
    in_spec = np.zeros_like(freqs, dtype=bool)
    for f_lo, f_hi, _tolerance in SPEC_BANDS:
        in_spec |= (freqs >= f_lo) & (freqs < f_hi)
    selected = in_spec & ~mask
    direct = float(
        np.sqrt(np.mean((spec[selected] - report.reference_db) ** 2))
    )
    assert both.rms_db == pytest.approx(direct, rel=1e-12)
    assert both.n_bins == int(selected.sum())


@pytest.fixture(scope="module")
def s0_combined():
    """The S0 main-leg cloud and its null registry, built once."""
    combined = combine_positions(s0_position_captures(S0_MAIN))
    # The band the S0 report grades the 8-16 kHz family in (REPORT.md Q1/Q2).
    return combined, identify_interference_nulls(combined, band_hz=(5000.0, 19_000.0))


# --------------------------------------------------------------------------- #
# #2599 -- an extremum sitting on the trusted floor's own cut edge
# --------------------------------------------------------------------------- #

# The round-3 shape, on an axis dense enough below 400 Hz to carry it: the
# 250-2000 Hz band graded from 357.14 Hz reported +4.49 dB @ 358, its FIRST
# graded bin, while the ungraded region below continued to +5.08 dB @ 329.
# Both numbers honest; only one reported, and nothing said it was an edge.
_EDGE_FREQS_HZ = np.array(
    [
        280.0, 329.0, 358.0, 420.0, 600.0, 1000.0, 1500.0,  # band1
        2000.0, 3000.0, 5000.0, 7000.0,                     # band2
        8000.0, 10000.0, 12000.0, 15000.0,                  # band3
    ],
    dtype=np.float64,
)
_ROUND3_FLOOR_HZ = 357.1425


def _rising_into_the_floor_db() -> np.ndarray:
    """A curve whose LF rise keeps climbing below the trusted floor, so the
    worst GRADED bin is the lowest graded bin and the true extremum is not
    graded at all.

    280/329 Hz are sized so that, self-referenced against band1 alone (the
    band this rise lives in, and -- since #1857's anchor ruling -- also
    REFERENCE_BAND_HZ itself), 280 Hz remains the single worst-deviation bin
    of the whole (untruncated) band even after its own bump pulls the
    band's power-mean level up. That property is exercised by
    ``test_an_untruncated_bands_lowest_bin_is_not_an_edge_extremum``; the
    floor-truncated tests below never see these two bins at all, so this
    choice does not affect them.
    """
    db = np.zeros_like(_EDGE_FREQS_HZ)
    db[0] = 10.3  # 280 Hz -- ungraded
    db[1] = 4.6   # 329 Hz -- ungraded, and the curve's real maximum region
    db[2] = 4.5   # 358 Hz -- the FIRST graded bin, and the reported extremum
    db[3] = 3.0   # 420 Hz
    return db


def test_an_extremum_on_the_graded_edge_is_disclosed():
    """#2599 rule 3. The reported maximum is a maximum over the GRADED bins,
    so when it lands on the LOWEST of them it is a lower bound on the band's
    real worst deviation -- and the report now says which case it is. Note
    what is NOT claimed: the flag tests two conjuncts and no slope, so it
    licenses "it may well be worse below", never "it is still rising"."""
    report = evaluate_flat_spec(
        _EDGE_FREQS_HZ, _rising_into_the_floor_db(),
        trusted_floor_hz=_ROUND3_FLOOR_HZ,
    )
    low = report.bands[0]

    assert low.evaluable is True
    assert low.f_lo_hz == SPEC_BANDS[0][0]
    assert low.graded_lo_hz == pytest.approx(_ROUND3_FLOOR_HZ)
    assert low.max_deviation_hz == 358.0
    assert low.max_at_graded_edge is True
    # The tell is load-bearing: the ungraded region really does continue
    # higher, which is exactly what the flag warns the reader about.
    below = _EDGE_FREQS_HZ < low.graded_lo_hz
    assert float(np.max(_rising_into_the_floor_db()[below])) > low.max_deviation_db
    # Disclosure only -- the verdict is untouched.
    assert low.passed is (abs(low.max_deviation_db) <= low.tolerance_db)
    assert report.to_dict()["bands"][0]["max_at_graded_edge"] is True


def test_an_extremum_inside_a_truncated_band_is_not_disclosed_as_an_edge():
    """Truncation alone is not the warning. The flag fires only when the
    extremum ALSO sits on the cut edge -- otherwise the graded span contains
    its own worst bin and there is nothing an unseen remainder could add."""
    db = _rising_into_the_floor_db()
    db[4] = 6.0  # 600 Hz -- well inside the graded span
    report = evaluate_flat_spec(
        _EDGE_FREQS_HZ, db, trusted_floor_hz=_ROUND3_FLOOR_HZ,
    )
    low = report.bands[0]
    assert low.graded_lo_hz == pytest.approx(_ROUND3_FLOOR_HZ)
    # Not 600 Hz: band1 is now the reference band itself, so the 600 Hz bump
    # pulls the graded span's own power-mean level up enough that the flat
    # 1000 Hz bin -- quiet relative to that raised level -- reads as a
    # bigger deviation than the bump. Either way the worst bin sits inside
    # the graded span, not on its cut edge, which is the thing under test.
    assert low.max_deviation_hz == 1000.0
    assert low.max_at_graded_edge is False


def test_an_untruncated_bands_lowest_bin_is_not_an_edge_extremum():
    """The other conjunct. With no floor the band starts where the table says
    it does, so its first bin has no ungraded remainder beneath it and a
    maximum landing there warns of nothing."""
    report = evaluate_flat_spec(_EDGE_FREQS_HZ, _rising_into_the_floor_db())
    low = report.bands[0]
    assert low.graded_lo_hz == low.f_lo_hz == SPEC_BANDS[0][0]
    # 280 Hz IS the band's lowest bin and IS the worst -- and that is fine.
    assert low.max_deviation_hz == 280.0
    assert low.max_at_graded_edge is False


def test_an_unevaluable_band_states_no_edge_verdict():
    """No bins, no extremum, so no claim about where one sat -- `None`, never
    a fabricated `False`, exactly like every other metric on the band.

    Emptied by the ceiling rather than the floor, for the reason
    ``test_a_band_wholly_outside_the_trusted_range_is_unevaluable_never_failed``
    states: a floor that empties a band now empties the frame with it and
    raises. ``max_at_graded_edge`` is a FLOOR-truncation disclosure either
    way, and an unevaluable band makes no claim about it."""
    report = evaluate_flat_spec(
        _EDGE_FREQS_HZ, _rising_into_the_floor_db(), trusted_ceiling_hz=8000.0,
    )
    top = report.bands[2]
    assert top.evaluable is False
    assert top.max_at_graded_edge is None
    assert report.to_dict()["bands"][2]["max_at_graded_edge"] is None


# --------------------------------------------------------------------------- #
# #3495 -- the ROOM's floor, marked beside the grade and never inside it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("entanglement_floor_hz", "source", "expected"),
    [
        # Unknown marks nothing.
        (None, gating.ENTANGLEMENT_SOURCE_UNKNOWN, (None, None, None)),
        # Below every graded band: nothing in the table is entangled.
        (150.0, gating.ENTANGLEMENT_SOURCE_DECLARED, (None, None, None)),
        # Straddling band 1: only the part of it below the floor is marked.
        (1000.0, gating.ENTANGLEMENT_SOURCE_DECLARED, (1000.0, None, None)),
        # Exactly a shared band edge: band 1 is wholly entangled, band 2 wholly
        # clear of it -- the same inclusive-lower rule membership uses.
        (2000.0, gating.ENTANGLEMENT_SOURCE_MEASURED, (2000.0, None, None)),
        # Above the whole graded span: every band is marked to its own top.
        (
            30000.0,
            gating.ENTANGLEMENT_SOURCE_MEASURED,
            (2000.0, 8000.0, BEST_EFFORT_ABOVE_HZ),
        ),
    ],
)
def test_room_entangled_span_is_marked_per_band_from_the_entanglement_floor(
    entanglement_floor_hz, source, expected
):
    report = evaluate_flat_spec(
        _FREQS_HZ,
        _flat_db(),
        entanglement_floor_hz=entanglement_floor_hz,
        entanglement_floor_source=source,
    )
    assert tuple(band.room_entangled_below_hz for band in report.bands) == expected
    assert report.entanglement_floor_hz == entanglement_floor_hz
    assert report.entanglement_floor_source == source
    raw = report.to_dict()
    assert raw["entanglement_floor_source"] == source
    assert [b["room_entangled_below_hz"] for b in raw["bands"]] == list(expected)
    json.dumps(raw)


def test_the_entanglement_floor_moves_no_graded_number():
    """Disclosure only. Unlike ``trusted_floor_hz`` this floor clamps no band
    edge, so every metric and every verdict must be bit-identical to the same
    evaluation without it -- the marks are the ONLY difference."""
    import dataclasses

    db = _flat_db()
    db[0] = 3.0
    plain = evaluate_flat_spec(_FREQS_HZ, db)
    marked = evaluate_flat_spec(
        _FREQS_HZ,
        db,
        entanglement_floor_hz=1000.0,
        entanglement_floor_source=gating.ENTANGLEMENT_SOURCE_DECLARED,
    )
    assert marked.bands[0].room_entangled_below_hz == 1000.0
    assert marked.overall_passed == plain.overall_passed
    assert marked.reference_db == plain.reference_db
    assert tuple(
        dataclasses.replace(band, room_entangled_below_hz=None)
        for band in marked.bands
    ) == plain.bands


@pytest.mark.parametrize(
    ("floor_hz", "source"),
    [
        # A word outside the vocabulary, and a pair that disagrees about
        # whether a floor is known -- the rule itself is pinned on
        # `gating.EntanglementFloor`; what this pins is that the STRICT door
        # is the one this seam takes.
        (1000.0, "measured"),
        (None, gating.ENTANGLEMENT_SOURCE_DECLARED),
    ],
)
def test_evaluate_flat_spec_refuses_a_provenance_it_cannot_publish(floor_hz, source):
    """The seam is a caller's own vocabulary, not a document's: a word outside
    the three, or one that contradicts the floor handed in beside it, is a bug
    at the call site and raises rather than being echoed onto the report."""
    with pytest.raises(ValueError):
        evaluate_flat_spec(
            _FREQS_HZ,
            _flat_db(),
            entanglement_floor_hz=floor_hz,
            entanglement_floor_source=source,
        )


def test_a_pre_3495_document_rehydrates_as_unknown_and_unmarked():
    """The keys are new, so every banked report predates them. Their absence
    must read as UNKNOWN -- ``None`` floor, ``unknown`` source, no band marked
    -- and never as a report that was evaluated and found clear of the room.
    """
    report = evaluate_flat_spec(
        _FREQS_HZ,
        _flat_db(),
        entanglement_floor_hz=1000.0,
        entanglement_floor_source=gating.ENTANGLEMENT_SOURCE_MEASURED,
    )
    raw = report.to_dict()
    del raw["entanglement_floor_hz"]
    del raw["entanglement_floor_source"]
    for band in raw["bands"]:
        del band["room_entangled_below_hz"]

    older = FlatSpecReport.from_dict(raw)
    assert older.entanglement_floor_hz is None
    assert older.entanglement_floor_source == gating.ENTANGLEMENT_SOURCE_UNKNOWN
    assert all(band.room_entangled_below_hz is None for band in older.bands)
    assert older.overall_passed == report.overall_passed


def test_a_pre_gate_sweep_document_rehydrates_as_never_swept():
    """A banked report predating the gate fields must read as NOT MEASURED --
    no numbers, no verdict and no note -- never as a round whose worst bins
    were swept and found window-invariant.
    """
    report = evaluate_flat_spec(_FREQS_HZ, _flat_db())
    raw = report.to_dict()
    del raw["gate_sweep_frame"]
    for band in raw["bands"]:
        for key in (
            "gate_sensitivity_db",
            "sigma_growth_ratio",
            "n_valid_rungs",
            "gate_sensitivity_note",
            "gate_window_verdict",
            "gate_window_verdict_reasons",
        ):
            del band[key]

    older = FlatSpecReport.from_dict(raw)
    assert older.gate_sweep_frame is None
    assert all(band.gate_sensitivity_db is None for band in older.bands)
    assert all(band.sigma_growth_ratio is None for band in older.bands)
    assert all(band.n_valid_rungs is None for band in older.bands)
    assert all(band.gate_sensitivity_note is None for band in older.bands)
    assert all(band.gate_window_verdict is None for band in older.bands)
    assert all(band.gate_window_verdict_reasons is None for band in older.bands)
    assert older.overall_passed == report.overall_passed


@pytest.mark.parametrize("stored", [7.0, "a frame", ["rungs_ms"], True])
def test_a_gate_sweep_frame_that_is_not_a_mapping_rehydrates_as_absent(stored):
    """A frame no reader can index is the same evidence as no frame at all,
    and must not rehydrate as one that looks present.
    """
    raw = evaluate_flat_spec(_FREQS_HZ, _flat_db()).to_dict()
    raw["gate_sweep_frame"] = stored
    assert FlatSpecReport.from_dict(raw).gate_sweep_frame is None


@pytest.mark.parametrize(
    ("floor_hz", "source"),
    [
        (None, gating.ENTANGLEMENT_SOURCE_DECLARED),
        # A word outside the vocabulary leaves the source unknown; the number
        # beside it must not survive that.
        (1000.0, "surveyed"),
    ],
)
def test_a_document_whose_floor_and_source_disagree_rehydrates_as_unknown(
    floor_hz, source
):
    """This seam takes the LENIENT door, and what comes out survives the
    strict one.

    ``evaluate_flat_spec`` refuses a known floor with no provenance and a
    provenance with no floor. A document carrying one is corrupt in exactly
    that way, and rehydrating it verbatim would build a report that raises the
    moment anything re-grades in its frame. Which pairs are refusable is
    pinned on ``gating.EntanglementFloor``, not here.
    """
    raw = evaluate_flat_spec(
        _FREQS_HZ,
        _flat_db(),
        entanglement_floor_hz=1000.0,
        entanglement_floor_source=gating.ENTANGLEMENT_SOURCE_MEASURED,
    ).to_dict()
    raw["entanglement_floor_hz"] = floor_hz
    raw["entanglement_floor_source"] = source

    rehydrated = FlatSpecReport.from_dict(raw)

    assert rehydrated.entanglement_floor_hz is None
    assert rehydrated.entanglement_floor_source == gating.ENTANGLEMENT_SOURCE_UNKNOWN
    # It survives the seam it was coerced for.
    evaluate_flat_spec(_FREQS_HZ, _flat_db(), **rehydrated.frame_kwargs)


def test_a_reports_frame_is_the_evaluators_own_clamp_and_floor_keywords():
    """``frame_kwargs`` is splatted at every re-grade site, so its keys are a
    contract with ``evaluate_flat_spec``'s signature rather than a convenience.

    Asserted on the key SET, not on values: what a re-grade must not be able to
    do is state three of the four and quietly take a default for the fourth.
    """
    report = evaluate_flat_spec(
        _FREQS_HZ,
        _flat_db(),
        trusted_floor_hz=200.0,
        trusted_ceiling_hz=16000.0,
        entanglement_floor_hz=1000.0,
        entanglement_floor_source=gating.ENTANGLEMENT_SOURCE_DECLARED,
    )

    assert set(report.frame_kwargs) == {
        "trusted_floor_hz",
        "trusted_ceiling_hz",
        "entanglement_floor_hz",
        "entanglement_floor_source",
    }
    assert set(report.frame_kwargs) <= set(
        inspect.signature(evaluate_flat_spec).parameters
    )
    # A re-grade in this frame reports the same frame back.
    assert evaluate_flat_spec(
        _FREQS_HZ, _flat_db(), **report.frame_kwargs
    ).frame_kwargs == report.frame_kwargs
