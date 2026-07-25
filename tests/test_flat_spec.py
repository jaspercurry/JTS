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
    FlatSpecReport,
    evaluate_flat_spec,
)
from jasper.audio_measurement.spatial_combine import merged_true_intervals

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
# to re-derive the membership rule themselves.
_REF_IDX = list(range(0, 8))
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
    assert REFERENCE_BAND_HZ == (250.0, 8000.0)
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
    freqs_hz = np.array([1000.0, 4000.0, 10000.0])  # 1000/4000 -> reference band
    spec_smoothed_db = np.array([0.0, 10.0, 0.0])

    report = evaluate_flat_spec(freqs_hz, spec_smoothed_db)

    # 10*math.log10(5.5), computed once and pinned here as a literal.
    pinned_power_mean_db = 7.403626894942439
    naive_linear_average_db = 5.0  # (0.0 + 10.0) / 2 -- the WRONG answer

    assert report.reference_db == pytest.approx(pinned_power_mean_db, abs=1e-9)
    assert report.reference_db != pytest.approx(naive_linear_average_db, abs=1e-3)


def test_exclusion_inside_reference_band_changes_reference_as_hand_computed():
    db = _flat_db(0.0)
    db[4] = 40.0  # 2000 Hz, inside REFERENCE_BAND_HZ -- a huge outlier

    report_included = evaluate_flat_spec(_FREQS_HZ, db)

    exclusion_mask = np.zeros(_FREQS_HZ.shape, dtype=bool)
    exclusion_mask[4] = True
    report_excluded = evaluate_flat_spec(_FREQS_HZ, db, exclusion_mask)

    values = db.tolist()
    expected_included = _hand_power_mean_db([values[i] for i in _REF_IDX])
    expected_excluded = _hand_power_mean_db([values[i] for i in _REF_IDX if i != 4])

    assert report_included.reference_db == pytest.approx(expected_included, abs=1e-9)
    assert report_excluded.reference_db == pytest.approx(expected_excluded, abs=1e-9)
    # The excluded run drops the 40 dB outlier from the reference-band
    # computation entirely -- the remaining 7 bins are all 0 dB.
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
    """A -2.2 dB dip in the 2-8 kHz band, against that band's +/-2.0 dB
    tolerance, reads as an obvious fail at first glance -- but the dipped
    bin is itself one of the 8 reference-band bins, so it also pulls the
    power-mean reference down slightly, which *shrinks* its own apparent
    deviation to ~1.979 dB (hand-verified while writing this test) --
    just under 2.0 dB, so a -2.2 dB dip on this exact fixture actually
    PASSES. That razor's-edge outcome is too fragile to pin reliably (it
    depends on the exact reference bin count), so this test uses a
    decisively larger -3.5 dB dip instead -- confirmed well clear of the
    tolerance boundary in both directions: an unambiguous fail for band2,
    with band1/band3 an unambiguous pass from the small reference-level
    shift alone.
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
    # abs(), because band1's own deviation is NEGATIVE here: the spike lifts
    # the shared reference, so band1's flat 0 dB bins read slightly quiet.
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
    }
    assert type(d["reference_db"]) is float
    assert type(d["overall_passed"]) is bool
    assert type(d["best_effort_above_hz"]) is float
    assert d["best_effort_above_hz"] == 16000.0
    assert type(d["smoothing_fraction"]) is int
    assert d["smoothing_fraction"] == 3

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
        }
        for key in (
            "f_lo_hz",
            "f_hi_hz",
            "tolerance_db",
            "max_deviation_db",
            "max_deviation_hz",
            "rms_deviation_db",
        ):
            assert type(band_dict[key]) is float, key
        for key in ("n_bins", "n_excluded"):
            assert type(band_dict[key]) is int, key
        assert type(band_dict["evaluable"]) is bool
        assert type(band_dict["passed"]) is bool

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

    ``REFERENCE_BAND_HZ`` spans exactly band1 union band2, so a mask that
    left *all three* spec bands unevaluable would also leave the reference
    band with zero non-excluded bins -- and with no reference level there is
    nothing to compute a deviation against anywhere. Two unevaluable bands
    is therefore the worst survivable case, and it survives: the one band
    that kept a bin still reports, and the overall verdict is False.
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
        ValueError, match=r"reference band 250\.0-8000\.0 Hz has zero non-excluded bins"
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
        ValueError, match=r"reference band 250\.0-8000\.0 Hz has zero non-excluded bins"
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
