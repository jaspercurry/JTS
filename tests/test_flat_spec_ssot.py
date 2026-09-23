# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Flat-spec data stays identical across the pipeline, persisted state, and doctor."""
from __future__ import annotations


import numpy as np

from jasper.active_speaker.flat_spec import (
    REFERENCE_BAND_HZ,
    SPEC_BANDS,
    evaluate_flat_spec,
    spec_convergence_residual,
    spec_flatness_gauge,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _report(curve_db: np.ndarray, freqs_hz: np.ndarray, mask=None):
    return evaluate_flat_spec(freqs_hz, curve_db, mask)


def _tilted_report(*, slope_db: float = -9.0, mask=None):
    """A synthetic report with a real, locatable worst bin per band."""
    freqs = np.geomspace(100.0, 20_000.0, 2000)
    curve = slope_db * np.log10(freqs / 100.0) / np.log10(200.0)
    return _report(curve, freqs, mask)


# --------------------------------------------------------------------------- #
# the gauge lifts, it never recomputes
# --------------------------------------------------------------------------- #


def test_every_gauge_figure_is_a_figure_from_the_report():
    """The SSOT property at its smallest: no field on the gauge is a new
    computation. Each one is traced back to the exact report field it came
    from, so a future edit that starts deriving a number here fails."""
    report = _tilted_report()
    gauge = spec_flatness_gauge(report)
    residual = spec_convergence_residual(report)

    worst = max(
        (b for b in report.bands if b.evaluable),
        key=lambda b: abs(b.max_deviation_db),
    )
    assert gauge.max_db == worst.max_deviation_db
    assert gauge.max_hz == worst.max_deviation_hz
    assert gauge.max_band_hz == (worst.f_lo_hz, worst.f_hi_hz)
    assert gauge.tolerance_db == worst.tolerance_db
    assert gauge.rms_db == residual.rms_db
    assert gauge.n_bins == residual.n_bins
    assert gauge.n_excluded == residual.n_excluded
    assert gauge.passed == report.overall_within_target
    assert gauge.evaluable is True


def test_the_gauge_names_the_frame_its_worst_band_is_stated_against():
    """#1857 — a worst-band pointer without its reference frame is half a
    claim, and the missing half is the one that decides which driver gets
    blamed.

    Every deviation on every spec surface is ``curve - reference_db``, where
    ``reference_db`` is a power mean pooled over ``REFERENCE_BAND_HZ``
    (250 Hz-8 kHz). On the 2026-07-30 corpus a dark tweeter pulls that
    full-range mean ~2.7 dB below a woofer-anchored one, and the SAME
    persisted curve's 250-2000 Hz pointer reads +5.44 dB @ 428 Hz in the
    shipped frame but -5.86 dB @ 1901 Hz woofer-anchored — a sign flip and a
    different band. The gauge carried the pointer and not the frame, so no
    reader downstream could tell which of those two readings they had.

    WHICH frame should win is a separate, deliberately open question (Q-E).
    This pins only that the gauge states the one it used.
    """
    report = _tilted_report()
    gauge = spec_flatness_gauge(report)

    assert gauge.reference_band_hz == REFERENCE_BAND_HZ
    assert gauge.to_dict()["reference_band_hz"] == list(REFERENCE_BAND_HZ)
    # It is not the band the worst bin lives in — conflating the two is the
    # mistake the issue is about.
    assert gauge.reference_band_hz != gauge.max_band_hz


def test_the_frame_is_named_even_when_no_band_could_be_graded():
    """#1857 — which frame WOULD have been used is knowable even when the
    gauge is unevaluable, and a reader comparing two sessions needs it
    either way."""
    from dataclasses import replace

    report = _tilted_report()
    # Same hand-built every-band-lost-its-bins shape the unevaluable-gauge
    # test above uses — evaluate_flat_spec cannot produce it directly.
    blanked = replace(
        report,
        bands=tuple(
            replace(
                b, evaluable=False, within_target=None, max_deviation_db=None,
                max_deviation_hz=None, rms_deviation_db=None,
                n_excluded=b.n_bins,
            )
            for b in report.bands
        ),
        overall_within_target=False,
    )
    gauge = spec_flatness_gauge(blanked)

    assert gauge.evaluable is False
    assert gauge.max_band_hz is None
    assert gauge.reference_band_hz == REFERENCE_BAND_HZ


def test_the_gauge_keeps_the_sign_of_the_worst_bin():
    """``BandResult.max_deviation_db``'s own rule — "2.4 dB too loud" and
    "2.4 dB too quiet" call for opposite corrections — survives the
    reduction. A gauge that took an absolute value here would hide which.

    The two slopes are NOT mirror images of each other in magnitude, and the
    test deliberately does not claim they are: the reference level is a POWER
    mean over the tight bands, which is not symmetric under a sign flip of a
    log-frequency tilt. Only the sign is the contract."""
    for slope, expect_negative in ((-9.0, True), (+9.0, False)):
        report = _tilted_report(slope_db=slope)
        gauge = spec_flatness_gauge(report)
        assert (gauge.max_db < 0.0) is expect_negative
        worst = max(
            (b for b in report.bands if b.evaluable),
            key=lambda b: abs(b.max_deviation_db),
        )
        assert gauge.max_db == worst.max_deviation_db


def test_the_worst_band_is_chosen_by_absolute_dB_not_tolerance_headroom():
    """Deliberately NOT "the band that failed by the widest margin relative to
    its own tolerance": the rendered claim is a dB reading of how far from
    flat the speaker measured. Here the 8-16 kHz band is worst in dB while
    every band is out of spec, so a tolerance-relative ranking could pick a
    different one; the gauge must report the dB-worst."""
    report = _tilted_report()
    gauge = spec_flatness_gauge(report)
    worst_by_db = max(
        (b for b in report.bands if b.evaluable), key=lambda b: abs(b.max_deviation_db)
    )
    assert gauge.max_band_hz == (worst_by_db.f_lo_hz, worst_by_db.f_hi_hz)


def test_an_exact_tie_between_bands_resolves_to_the_lowest_band():
    """Determinism, not dict order: two bands equally far from flat must pick
    the same one every run. ``SPEC_BANDS`` is ordered low-to-high and the scan
    uses a strict ``>``, so the lowest wins."""
    from dataclasses import replace

    report = _tilted_report()
    tied = replace(
        report,
        bands=tuple(
            replace(b, max_deviation_db=-3.0, max_deviation_hz=b.f_lo_hz + 1.0)
            if b.evaluable else b
            for b in report.bands
        ),
    )
    gauge = spec_flatness_gauge(tied)
    assert gauge.max_band_hz == (SPEC_BANDS[0][0], SPEC_BANDS[0][1])
    assert gauge.max_db == -3.0


def test_the_gauge_is_unevaluable_never_a_fabricated_zero_when_all_bins_are_masked():
    """Every spec-band bin excluded ⇒ ``evaluable=False`` and ``None`` metrics.
    ``passed`` is False there too (``FlatSpecReport.overall_within_target``'s own
    "will not report a clean bill of health" rule), which is exactly why a
    renderer must read the two together — pinned so a future reader does not
    mistake this state for a failing speaker."""
    freqs = np.geomspace(100.0, 20_000.0, 2000)
    curve = np.zeros_like(freqs)
    # Mask every spec-band bin but leave the reference band a foothold below
    # its own edge is impossible (the reference band IS two spec bands), so
    # mask everything at or above 2 kHz and let 250 Hz-2 kHz carry the
    # reference — then assert the two upper bands are unevaluable.
    mask = freqs >= SPEC_BANDS[1][0]
    gauge = spec_flatness_gauge(_report(curve, freqs, mask))
    assert gauge.evaluable is True  # band 1 survived
    assert gauge.max_band_hz == (SPEC_BANDS[0][0], SPEC_BANDS[0][1])

    # Now the genuinely-unevaluable case, built directly on a report whose
    # every band lost its bins: an axis that never reaches the spec bands is
    # rejected by evaluate_flat_spec (no reference), so this is constructed
    # from a hand-built report instead — the same corner
    # ``spec_convergence_residual``'s own docstring calls unreachable from
    # ``evaluate_flat_spec`` and guards anyway.
    from dataclasses import replace

    report = _report(curve, freqs)
    blanked = replace(
        report,
        bands=tuple(
            replace(
                b, evaluable=False, within_target=None, max_deviation_db=None,
                max_deviation_hz=None, rms_deviation_db=None,
                n_excluded=b.n_bins,
            )
            for b in report.bands
        ),
        overall_within_target=False,
    )
    blank_gauge = spec_flatness_gauge(blanked)
    assert blank_gauge.evaluable is False
    assert blank_gauge.max_db is None
    assert blank_gauge.max_hz is None
    assert blank_gauge.max_band_hz is None
    assert blank_gauge.tolerance_db is None
    assert blank_gauge.rms_db is None
    assert blank_gauge.n_bins == 0
    assert blank_gauge.passed is False  # read WITH evaluable, never alone
