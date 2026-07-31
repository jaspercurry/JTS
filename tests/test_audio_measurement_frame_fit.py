# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Frame discipline (rung P1): the two-scalar frame between two curves.

The productization of the 2026-07-31 first-principles panel's CC-1. Its W1
finding was that a single −0.788 dB/octave tilt between an on-axis model and an
in-room measurement accounted for 84 % of the replay scorecard's headline
"predictions are 2.02× optimistic" (raw 2.054 dB rms → 1.335 dB with the one
scalar removed). These tests pin the fit that measures it: exact recovery of a
known frame, the orthogonality that makes ``offset_db`` the same constant the
existing graders already mean-centre away, and honest absence when there is
nothing to fit.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from jasper.audio_measurement.frame_fit import (
    FRAME_UNFITTED,
    MIN_FRAME_FIT_BINS,
    FrameComparison,
    FrameFit,
    fit_frame,
)


def _grid(n: int = 240) -> np.ndarray:
    return np.geomspace(200.0, 12800.0, n)


def _shape(freqs: np.ndarray) -> np.ndarray:
    """A non-trivial, non-monotonic curve — so a recovered tilt is the injected
    one and not an artefact of fitting a slope to a slope."""
    return 3.0 * np.sin(np.log2(freqs) * 2.4) - 1.5 * np.cos(np.log2(freqs) * 5.1)


# --------------------------------------------------------------------------- #
# recovery
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "offset_db,tilt_db_per_octave",
    [(0.0, 0.0), (1.7, -0.8), (-4.25, 2.5), (0.0, -0.788), (12.0, 0.0)],
)
def test_a_known_offset_and_tilt_are_recovered_exactly(offset_db, tilt_db_per_octave):
    """Inject a frame between two otherwise-IDENTICAL curves; get it back.

    This is the whole contract. Everything downstream — the disclosure, the
    beside-grade, the household line — is only worth as much as this recovery.
    """
    freqs = _grid()
    reference = _shape(freqs)
    measured = reference + offset_db + tilt_db_per_octave * np.log2(freqs / 1000.0)

    fit = fit_frame(freqs, measured, reference)

    assert fit.fitted
    assert fit.tilt_db_per_octave == pytest.approx(tilt_db_per_octave, abs=1e-9)
    # ``offset_db`` is stated at the PIVOT, not at 1 kHz, so re-express the
    # injected frame there before comparing.
    expected_offset = offset_db + tilt_db_per_octave * np.log2(fit.pivot_hz / 1000.0)
    assert fit.offset_db == pytest.approx(expected_offset, abs=1e-9)


def test_removing_the_fitted_frame_collapses_the_residual_the_raw_one_shows():
    """The beside-grade's reason for existing, in one assertion pair.

    A pure frame difference reads as a large raw residual and as ~nothing once
    the frame is out. That is the 84 %-of-the-headline shape, in miniature.
    """
    freqs = _grid()
    reference = _shape(freqs)
    measured = reference - 0.79 * np.log2(freqs / 2000.0)

    raw_rms = float(np.sqrt(np.mean((measured - reference) ** 2)))
    fit = fit_frame(freqs, measured, reference)
    frame_removed = measured - fit.frame_db(freqs)
    residual_rms = float(np.sqrt(np.mean((frame_removed - reference) ** 2)))

    # ~1.4 dB rms across the grid's six octaves — the same order as the
    # 2.05 dB the panel measured on the real corpus, and entirely frame.
    assert raw_rms > 1.0
    assert residual_rms == pytest.approx(0.0, abs=1e-9)


def test_the_frame_is_reconstructible_from_the_disclosed_numbers_alone():
    """offset + tilt + pivot is enough to rebuild the curve that was removed —
    which is what makes the disclosure a disclosure rather than a summary."""
    freqs = _grid()
    reference = _shape(freqs)
    measured = reference + 2.0 - 1.1 * np.log2(freqs / 500.0)
    fit = fit_frame(freqs, measured, reference)

    rebuilt = fit.offset_db + fit.tilt_db_per_octave * np.log2(freqs / fit.pivot_hz)

    assert np.allclose(rebuilt, fit.frame_db(freqs), atol=1e-12)
    assert np.allclose(measured - rebuilt, reference, atol=1e-9)


# --------------------------------------------------------------------------- #
# the orthogonality the whole design rests on
# --------------------------------------------------------------------------- #


def test_the_offset_term_is_exactly_the_mean_the_graders_already_remove():
    """``offset_db == mean(measured − reference)`` over the fitted bins.

    Load-bearing, not incidental. The pivot is the geometric mean of the fitted
    frequencies, which makes the design matrix's two columns orthogonal — so
    the offset is the plain mean and the tilt is purely the additional term.
    That identity is why removing the whole frame before re-grading with
    ``analysis.tracking_error_db`` (which mean-centres) can only move the number
    by the TILT, and therefore why the beside-grade is named ``tilt_removed``.
    """
    freqs = _grid()
    reference = _shape(freqs)
    measured = _shape(freqs * 1.03) + 5.0 - 0.6 * np.log2(freqs / 300.0)

    fit = fit_frame(freqs, measured, reference)

    assert fit.offset_db == pytest.approx(float(np.mean(measured - reference)), abs=1e-12)
    assert fit.pivot_hz == pytest.approx(
        float(2.0 ** np.mean(np.log2(freqs))), rel=1e-12
    )


def test_the_tilt_does_not_move_when_either_curve_is_shifted_in_level():
    """A pure level change is the OFFSET's business; the tilt must not absorb
    any of it, or the two disclosed numbers would not be separable claims."""
    freqs = _grid()
    reference = _shape(freqs)
    measured = reference - 0.9 * np.log2(freqs / 1000.0)

    base = fit_frame(freqs, measured, reference)
    shifted = fit_frame(freqs, measured + 11.0, reference - 4.0)

    assert shifted.tilt_db_per_octave == pytest.approx(base.tilt_db_per_octave, abs=1e-12)
    assert shifted.offset_db == pytest.approx(base.offset_db + 15.0, abs=1e-12)


def test_the_fit_is_the_least_squares_answer_numpy_would_give():
    """Pins the closed form against the general solver it replaces, so the
    hand-rolled arithmetic cannot drift from what it claims to be."""
    freqs = _grid(97)
    reference = _shape(freqs)
    rng = np.random.default_rng(11)
    measured = reference + 0.4 - 0.35 * np.log2(freqs / 800.0) + rng.normal(0, 0.5, freqs.size)

    fit = fit_frame(freqs, measured, reference)

    centered = np.log2(freqs) - np.mean(np.log2(freqs))
    design = np.column_stack([np.ones_like(centered), centered])
    expected, *_ = np.linalg.lstsq(design, measured - reference, rcond=None)
    assert fit.offset_db == pytest.approx(float(expected[0]), abs=1e-10)
    assert fit.tilt_db_per_octave == pytest.approx(float(expected[1]), abs=1e-10)


# --------------------------------------------------------------------------- #
# absence is a first-class outcome
# --------------------------------------------------------------------------- #


def test_too_few_bins_reports_no_frame_rather_than_a_flat_one():
    freqs = np.array([1000.0, 2000.0])
    fit = fit_frame(freqs, np.array([1.0, 2.0]), np.zeros(2))

    assert fit is FRAME_UNFITTED
    assert not fit.fitted
    assert fit.offset_db is None and fit.tilt_db_per_octave is None
    assert MIN_FRAME_FIT_BINS == 3


def test_a_single_frequency_repeated_has_no_span_to_fit_a_slope_over():
    freqs = np.full(8, 1000.0)
    assert not fit_frame(freqs, np.arange(8.0), np.zeros(8)).fitted


def test_non_finite_bins_are_dropped_and_the_survivors_still_fit():
    freqs = _grid(60)
    reference = _shape(freqs)
    measured = reference + 1.0 - 0.5 * np.log2(freqs / 1000.0)
    measured = measured.copy()
    measured[5] = np.nan
    measured[40] = np.inf

    fit = fit_frame(freqs, measured, reference)

    assert fit.n_bins == 58
    assert fit.tilt_db_per_octave == pytest.approx(-0.5, abs=1e-9)


def test_a_non_positive_frequency_bin_is_dropped_not_logged():
    """log2 has no answer at DC; the bin leaves rather than poisoning the fit."""
    freqs = np.concatenate([[0.0], _grid(40)])
    reference = np.concatenate([[0.0], _shape(_grid(40))])
    measured = reference + 2.0

    fit = fit_frame(freqs, measured, reference)

    assert fit.fitted
    assert fit.n_bins == 40
    assert fit.band_hz == (200.0, 12800.0)


def test_an_unfitted_frame_removes_nothing_at_all():
    """So a caller that subtracts unconditionally gets its RAW comparison back
    — the honest degradation, since no frame was measured to remove."""
    freqs = _grid(10)
    assert np.array_equal(FRAME_UNFITTED.frame_db(freqs), np.zeros(10))


def test_an_unfitted_frame_discloses_absence_not_zero():
    assert FRAME_UNFITTED.to_dict() == {
        "offset_db": None,
        "tilt_db_per_octave": None,
        "pivot_hz": None,
        "n_bins": 0,
        "band_hz": None,
    }


def test_a_fitted_frame_discloses_the_span_it_was_measured_over():
    """``n_bins``/``band_hz`` are part of the answer: a two-parameter fit over
    a narrow span is ill-conditioned, and this module reports rather than
    adjudicates that (see its docstring's "not a goodness-of-fit test")."""
    freqs = _grid(31)
    fit = fit_frame(freqs, _shape(freqs) + 1.0, _shape(freqs))

    assert fit.to_dict() == {
        "offset_db": pytest.approx(1.0),
        "tilt_db_per_octave": pytest.approx(0.0, abs=1e-12),
        "pivot_hz": pytest.approx(1600.0),
        "n_bins": 31,
        "band_hz": [200.0, 12800.0],
    }


# --------------------------------------------------------------------------- #
# caller bugs are loud
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "freqs,measured,reference",
    [
        (np.ones((2, 2)), np.ones(4), np.ones(4)),
        (np.ones(4), np.ones(3), np.ones(4)),
        (np.ones(4), np.ones(4), np.ones(5)),
    ],
)
def test_a_grid_mismatch_raises_rather_than_reporting_no_frame(
    freqs, measured, reference,
):
    """A mismatched grid is a caller bug, not a measurement outcome — reporting
    it as "no frame" would hide the bug behind an honest-looking absence."""
    with pytest.raises(ValueError):
        fit_frame(freqs, measured, reference)


def test_frame_fit_is_frozen():
    fit = fit_frame(_grid(20), _shape(_grid(20)), _shape(_grid(20)))
    assert isinstance(fit, FrameFit)
    with pytest.raises(Exception):
        fit.offset_db = 1.0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# the typed disclosure record
# --------------------------------------------------------------------------- #


def test_the_disclosure_record_carries_the_frame_and_both_grades_together():
    """One structure, so no surface can render half a cross-frame claim."""
    freqs = _grid(50)
    fit = fit_frame(freqs, _shape(freqs) + 1.0 - 0.4 * np.log2(freqs), _shape(freqs))

    payload = FrameComparison(
        fit=fit,
        raw_rms_db=2.05,
        raw_max_db=4.1,
        tilt_removed_rms_db=1.34,
        tilt_removed_max_db=2.2,
    ).to_dict()

    assert payload["tilt_db_per_octave"] == pytest.approx(-0.4, abs=1e-9)
    assert payload["raw"] == {"rms_db": 2.05, "max_db": 4.1}
    assert payload["tilt_removed"] == {"rms_db": 1.34, "max_db": 2.2}


def test_an_unfitted_comparison_reports_absent_grades_not_its_raw_twins():
    """A tilt-removed number equal to its raw twin is a MEASUREMENT ("removing
    the frame changed nothing"). Absence is a different claim and must stay
    distinguishable from it."""
    payload = FrameComparison(
        fit=FRAME_UNFITTED, raw_rms_db=2.05, raw_max_db=4.1,
    ).to_dict()

    assert payload["offset_db"] is None
    assert payload["tilt_removed"] == {"rms_db": None, "max_db": None}
    assert payload["raw"] == {"rms_db": 2.05, "max_db": 4.1}


def test_the_disclosure_record_is_json_safe_scalars_only():
    """It rides a ``PhaseVerdict`` payload to the phone AND into the durable
    state file, so one leaked ``numpy.float64`` would break both — silently on
    the wire, loudly at persist time. ``fit_frame`` coerces every term with
    ``float()``/``int()``; this is what keeps that true."""
    freqs = np.geomspace(800.0, 3200.0, 400)
    reference = _shape(freqs)
    measured = reference + np.float64(1.5) - np.float64(0.8) * np.log2(freqs / 2000.0)

    payload = FrameComparison(
        fit=fit_frame(freqs, measured, reference),
        raw_rms_db=2.05, raw_max_db=4.1,
        tilt_removed_rms_db=1.34, tilt_removed_max_db=2.2,
    ).to_dict()

    assert json.loads(json.dumps(payload)) == payload
    scalars = [
        payload["offset_db"], payload["tilt_db_per_octave"], payload["pivot_hz"],
        *payload["band_hz"], *payload["raw"].values(), *payload["tilt_removed"].values(),
    ]
    # `type(...) is float` on purpose: `numpy.float64` passes `isinstance(...,
    # float)` and is exactly what this test exists to catch.
    assert all(type(value) is float for value in scalars)
    assert type(payload["n_bins"]) is int


def test_the_disclosure_record_never_computes_a_grade_of_its_own():
    """It owns the association and the JSON shape; the caller's grader owns
    every number. Given no grades, it reports none — it cannot derive one from
    the fit, and must not appear to."""
    freqs = _grid(50)
    fit = fit_frame(freqs, _shape(freqs) + 3.0, _shape(freqs))
    payload = FrameComparison(fit=fit).to_dict()

    assert payload["offset_db"] == pytest.approx(3.0)
    assert payload["raw"] == {"rms_db": None, "max_db": None}
    assert payload["tilt_removed"] == {"rms_db": None, "max_db": None}
