# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #1857 — the flat-spec worst-band pointer, and the attribution split
that stops it being read as "here is the peak to EQ".

**The mechanism, stated once.** ``FlatSpecReport.reference_db`` is a power
mean pooled over ``REFERENCE_BAND_HZ`` (250 Hz–8 kHz), and every graded
number is a distance from it. So a band that is uniformly off drags the
shared zero toward itself and inflates *every other* band's deviation. On
the 2026-07-29 corpus session that produced a verdict reading
"+4.84 dB @ 1339.6 Hz" — the woofer band — against a woofer flat to
±0.1 dB, because a ~5 dB dark tweeter had already pulled the frame ~3 dB
down. The issue's own words for the cost: "A household reading this would EQ
the wrong driver."

:func:`_dark_tweeter` reproduces it synthetically, on the linear rfft bin
axis the evaluator is actually handed (bin DENSITY is half the mechanism:
2–8 kHz carries ~3.4× the bins of 250 Hz–2 kHz on a linear axis, so a dark
tweeter dominates the pooled mean by weight of numbers).
:func:`test_the_pointer_names_the_flat_band_as_the_worst_one` is the red
statement: it asserts the misattribution AS IT STILL SHIPS.

**What this module's fix does and deliberately does not do.** WHICH anchor
the spec's reference frame should use is an explicitly open owner decision —
#1857's Q-E (``docs/historical/attribution-stage-plan.md`` §9), which
``docs/historical/attribution-stage-plan.md``'s WO-5 says the implementer "does not
pick". Re-anchoring would move graded verdicts. So nothing here touches the
frame, and
:func:`test_no_graded_number_moved_on_any_corpus_shape` pins that claim
against numbers frozen from ``origin/main`` BEFORE the change: the verdicts
are identical, shape for shape.

What was added instead is an attribution reading that no frame choice can
move — each band's own level, its own ripple measured from that level, and
the step between two bands' levels (:func:`spec_band_tilt`). The reference
cancels in that subtraction, which
:func:`test_the_tilt_is_the_same_number_under_every_candidate_frame` proves
by evaluating the same curve under five candidate reference bands including
the two Q-E names.
"""
from __future__ import annotations

import numpy as np
import pytest

import jasper.active_speaker.flat_spec as flat_spec
from jasper.active_speaker.crossover_envelope_v2 import _flatness_lines_from_block
from jasper.active_speaker.crossover_v2.verification import (
    _flatness_tilt_log_field,
)
from jasper.active_speaker.flat_spec import (
    NO_BAND_TILT,
    REFERENCE_BAND_HZ,
    SPEC_BANDS,
    BandResult,
    BandTilt,
    FlatSpecReport,
    evaluate_flat_spec,
    spec_band_tilt,
    spec_flatness_gauge,
)

# --------------------------------------------------------------------------- #
# the corpus of shapes
#
# Deterministic, hardware-free, and pure numpy — this is the population the
# verdict-invariance pin is asserted over, so it has to be reproducible in CI
# rather than read off the gitignored laptop corpora (tests/_flat_lin_corpus).
# --------------------------------------------------------------------------- #

# A LINEAR bin axis, because that is what the evaluator is handed in
# production (program_analysis returns rfft bins). The linear spacing is not
# incidental: it is what makes 2–8 kHz outweigh 250 Hz–2 kHz in the pooled
# reference by bin count, which is half of #1857's mechanism.
_FS_HZ = 48000.0
_N_FFT = 2048
_FREQS_HZ = np.arange(1, _N_FFT // 2) * (_FS_HZ / _N_FFT)  # ~23.4 Hz spacing
# One LOG-spaced shape too, so the pin covers an axis whose bin density
# weights the reference the other way round.
_LOG_FREQS_HZ = np.geomspace(180.0, 20000.0, 371)


def _ripple(freqs: np.ndarray, depth_db: float, cycles: float) -> np.ndarray:
    """A small deterministic in-band wobble, so no shape is pathologically
    flat and ``max_ripple_db`` always has something real to report."""
    return depth_db * np.sin(np.log(freqs / freqs[0]) * cycles)


def _dark_tweeter(depth_db: float, freqs: np.ndarray = _FREQS_HZ) -> np.ndarray:
    """#1857's shape: a woofer band flat to a tenth of a dB, and everything
    above the 2 kHz crossover uniformly ``depth_db`` down. Nothing in this
    curve is proud — one driver is quiet."""
    curve = _ripple(freqs, 0.10, 6.0)
    curve[freqs >= 2000.0] -= depth_db
    return curve


def _dark_woofer(depth_db: float, freqs: np.ndarray = _FREQS_HZ) -> np.ndarray:
    """The mirror of :func:`_dark_tweeter` — the band BELOW the crossover is
    the quiet one, so the largest level step runs the other way."""
    curve = _ripple(freqs, 0.10, 6.0)
    curve[freqs < 2000.0] -= depth_db
    return curve


def _corpus() -> list[tuple[str, np.ndarray, np.ndarray, np.ndarray | None]]:
    """``(name, freqs_hz, curve_db, exclusion_mask)`` per pinned shape."""
    out: list[tuple[str, np.ndarray, np.ndarray, np.ndarray | None]] = []

    out.append(("flat", _FREQS_HZ, np.zeros_like(_FREQS_HZ), None))

    for depth in (2.0, 5.0, 8.0, 12.0):
        out.append((f"dark_tweeter_{depth:g}db", _FREQS_HZ, _dark_tweeter(depth), None))

    # The mirror shape — a genuinely proud woofer, so the pin covers the
    # case where naming the low band IS correct.
    curve = _ripple(_FREQS_HZ, 0.10, 6.0)
    curve[_FREQS_HZ < 2000.0] += 5.0
    out.append(("proud_woofer_5db", _FREQS_HZ, curve, None))

    # The other mirror — the LOW band is the quiet one. Deliberately present:
    # it is the only shape here where the largest level step is negative in
    # SPEC_BANDS walk order, so it is what distinguishes a tilt taken on
    # magnitude from one taken on a signed difference.
    out.append(("dark_woofer_5db", _FREQS_HZ, _dark_woofer(5.0), None))

    # Ripple with no level step at all — the tilt must stay near zero here.
    out.append(("ripple_only_3db", _FREQS_HZ, _ripple(_FREQS_HZ, 3.0, 9.0), None))

    curve = np.zeros_like(_FREQS_HZ)
    curve[(_FREQS_HZ >= 700.0) & (_FREQS_HZ < 780.0)] -= 9.0
    out.append(("notch_in_woofer", _FREQS_HZ, curve, None))

    out.append(
        ("tilt_droop", _FREQS_HZ, -12.0 * np.log10(_FREQS_HZ / 250.0), None)
    )

    out.append(
        ("dark_tweeter_log_axis", _LOG_FREQS_HZ, _dark_tweeter(5.0, _LOG_FREQS_HZ), None)
    )

    # The interference-exclusion band the shipped spec carves around the
    # crossover, so the pin covers a masked seam.
    mask = (_FREQS_HZ >= 1627.0) & (_FREQS_HZ <= 1921.0)
    out.append(("dark_tweeter_masked_seam", _FREQS_HZ, _dark_tweeter(5.0), mask))

    # A whole band masked out -> unevaluable band 3.
    out.append(
        ("top_band_unevaluable", _FREQS_HZ, _dark_tweeter(5.0), _FREQS_HZ >= 8000.0)
    )

    # An axis that never reaches band 3 -> unevaluable for a different reason.
    short = _FREQS_HZ[_FREQS_HZ < 6000.0]
    out.append(("axis_stops_short", short, _dark_tweeter(5.0, short), None))

    curve = np.zeros_like(_FREQS_HZ)
    curve[_FREQS_HZ >= 8000.0] -= 6.0
    out.append(("dark_top_octave_only", _FREQS_HZ, curve, None))

    curve = np.zeros_like(_FREQS_HZ)
    curve[(_FREQS_HZ >= 400.0) & (_FREQS_HZ < 500.0)] += 1.5
    out.append(("woofer_bump_at_tolerance", _FREQS_HZ, curve, None))

    return out


def _verdict_tuple(name: str) -> tuple:
    for candidate, freqs, curve, mask in _corpus():
        if candidate != name:
            continue
        report = evaluate_flat_spec(freqs, curve, mask)
        gauge = spec_flatness_gauge(report)
        return (
            report.reference_db,
            tuple(
                (
                    b.f_lo_hz, b.f_hi_hz, b.tolerance_db,
                    b.max_deviation_db, b.max_deviation_hz, b.rms_deviation_db,
                    b.n_bins, b.n_excluded, b.evaluable, b.passed,
                )
                for b in report.bands
            ),
            report.overall_passed,
            (
                gauge.max_db, gauge.max_hz, gauge.max_band_hz, gauge.tolerance_db,
                gauge.rms_db, gauge.n_bins, gauge.n_excluded,
                gauge.evaluable, gauge.passed,
            ),
        )
    raise KeyError(name)


# Every graded number this evaluator produced for the shapes above, captured
# from ``origin/main`` at commit 0620206b2 — BEFORE the #1857 attribution
# split was added. Layout per entry, matching :func:`_verdict_tuple`:
#
#   (reference_db,
#    ((f_lo, f_hi, tolerance, max_dev_db, max_dev_hz, rms_dev_db,
#      n_bins, n_excluded, evaluable, passed), ... one per SPEC_BANDS entry),
#    overall_passed,
#    (gauge max_db, max_hz, max_band_hz, tolerance_db, rms_db,
#     n_bins, n_excluded, evaluable, passed))
#
# Regenerating these is a deliberate act, not a refresh: a diff here means
# the change under review MOVED A VERDICT, which #1857 explicitly forbids
# (the anchor question is an owner decision, Q-E). See this module's
# docstring.
VERDICT_GOLDEN = {
    'flat': (0.0, ((250.0, 2000.0, 1.5, 0.0, 257.8125, 0.0, 75, 0, True, True), (2000.0, 8000.0, 2.0, 0.0, 2015.625, 0.0, 256, 0, True, True), (8000.0, 16000.0, 2.5, 0.0, 8015.625, 0.0, 341, 0, True, True)), True, (0.0, 257.8125, (250.0, 2000.0), 1.5, 0.0, 672, 0, True, True)),
    'dark_tweeter_2db': (-1.4464566685979507, ((250.0, 2000.0, 1.5, 1.546447998966855, 703.125, 1.4507268264144606, 75, 0, True, False), (2000.0, 8000.0, 2.0, -0.6535316695547118, 3398.4375, 0.5403129917379897, 256, 0, True, True), (8000.0, 16000.0, 2.5, -0.6535430947794936, 9656.25, 0.5698952631167461, 341, 0, True, True)), False, (1.546447998966855, 703.125, (250.0, 2000.0), 1.5, 0.7147801586404772, 672, 0, True, False)),
    'dark_tweeter_5db': (-3.257635951403108, ((250.0, 2000.0, 1.5, 3.3576272817720123, 703.125, 3.2609447552005895, 75, 0, True, False), (2000.0, 8000.0, 2.0, -1.8423523867495541, 3398.4375, 1.7264236272449565, 256, 0, True, True), (8000.0, 16000.0, 2.5, -1.8423638119743364, 9656.25, 1.7556891477291627, 341, 0, True, True)), False, (3.3576272817720123, 703.125, (250.0, 2000.0), 1.5, 1.9713964463125693, 672, 0, True, False)),
    'dark_tweeter_8db': (-4.561479470621921, ((250.0, 2000.0, 1.5, 4.661470800990825, 703.125, 4.564568397866856, 75, 0, True, False), (2000.0, 8000.0, 2.0, -3.5385088675307417, 3398.4375, 3.4219711717736154, 256, 0, True, False), (8000.0, 16000.0, 2.5, -3.538520292755522, 9656.25, 3.4511344616261757, 341, 0, True, False)), False, (4.661470800990825, 703.125, (250.0, 2000.0), 1.5, 3.581907154938261, 672, 0, True, False)),
    'dark_tweeter_12db': (-5.594896923321704, ((250.0, 2000.0, 1.5, 5.694888253690608, 703.125, 5.597884335575984, 75, 0, True, False), (2000.0, 8000.0, 2.0, -6.505091414830959, 3398.4375, 6.388265952494163, 256, 0, True, False), (8000.0, 16000.0, 2.5, -6.50510284005574, 9656.25, 6.417376794557139, 341, 0, True, False)), False, (-6.50510284005574, 9656.25, (8000.0, 16000.0), 2.5, 6.319951106660187, 672, 0, True, False)),
    'proud_woofer_5db': (1.7423640485968914, ((250.0, 2000.0, 1.5, 3.3576272817720128, 703.125, 3.260944755200591, 75, 0, True, False), (2000.0, 8000.0, 2.0, -1.842352386749554, 3398.4375, 1.7264236272449558, 256, 0, True, True), (8000.0, 16000.0, 2.5, -1.8423638119743355, 9656.25, 1.7556891477291623, 341, 0, True, True)), False, (3.3576272817720128, 703.125, (250.0, 2000.0), 1.5, 1.971396446312569, 672, 0, True, False)),
    'dark_woofer_5db': (-0.7146645553721245, ((250.0, 2000.0, 1.5, -4.385293371780634, 1195.3125, 4.28338241692964, 75, 0, True, False), (2000.0, 8000.0, 2.0, 0.8146638658647442, 5718.75, 0.7347252460114149, 256, 0, True, True), (8000.0, 16000.0, 2.5, 0.8139486449069242, 15984.375, 0.7063911021814592, 341, 0, True, True)), False, (-4.385293371780634, 1195.3125, (250.0, 2000.0), 1.5, 1.5832087831778079, 672, 0, True, False)),
    'ripple_only_3db': (0.7427371291370201, ((250.0, 2000.0, 1.5, -3.736913929442932, 1289.0625, 2.21783882302713, 75, 0, True, False), (2000.0, 8000.0, 2.0, -3.742583255665609, 5250.0, 2.2277006362948075, 256, 0, True, False), (8000.0, 16000.0, 2.5, -3.7426940861671647, 10546.875, 2.231066612521004, 341, 0, True, False)), False, (-3.7426940861671647, 10546.875, (8000.0, 16000.0), 2.5, 2.228311662894292, 672, 0, True, False)),
    'notch_in_woofer': (-0.04611955113916253, ((250.0, 2000.0, 1.5, -8.953880448860838, 703.125, 2.0682969428744227, 75, 0, True, False), (2000.0, 8000.0, 2.0, 0.04611955113916253, 2015.625, 0.04611955113916253, 256, 0, True, True), (8000.0, 16000.0, 2.5, 0.04611955113916253, 8015.625, 0.04611955113916253, 341, 0, True, True)), False, (-8.953880448860838, 703.125, (250.0, 2000.0), 1.5, 0.6923355325714109, 672, 0, True, False)),
    'tilt_droop': (-10.911640088355416, ((250.0, 2000.0, 1.5, 10.751272549659637, 257.8125, 4.70329329576914, 75, 0, True, False), (2000.0, 8000.0, 2.0, -7.145067776351635, 7992.1875, 4.784360383416945, 256, 0, True, False), (8000.0, 16000.0, 2.5, -10.75742772431941, 15984.375, 9.221183281797144, 341, 0, True, False)), False, (-10.75742772431941, 15984.375, (8000.0, 16000.0), 2.5, 7.371341372867073, 672, 0, True, False)),
    'dark_tweeter_log_axis': (-1.3836333704650758, ((250.0, 2000.0, 1.5, 1.4836318165291584, 1897.3665961010286, 1.3851441801402933, 164, 0, True, True), (2000.0, 8000.0, 2.0, -3.7163549235321867, 3197.741083509517, 3.612694124570874, 109, 0, True, False), (8000.0, 16000.0, 2.5, -3.7163062445541604, 9082.959738844835, 3.628510803982266, 54, 0, True, False)), False, (-3.7163549235321867, 3197.741083509517, (2000.0, 8000.0), 2.0, 2.736235058298098, 327, 0, True, False)),
    'dark_tweeter_masked_seam': (-3.449930011529045, ((250.0, 2000.0, 1.5, 3.5499213418979494, 703.125, 3.440433820352848, 75, 12, True, False), (2000.0, 8000.0, 2.0, -1.650058326623617, 3398.4375, 1.5342835608115575, 256, 0, True, True), (8000.0, 16000.0, 2.5, -1.6500697518483993, 9656.25, 1.5635730731102475, 341, 0, True, True)), False, (3.5499213418979494, 703.125, (250.0, 2000.0), 1.5, 1.8182571291579095, 660, 12, True, False)),
    'top_band_unevaluable': (-3.257635951403108, ((250.0, 2000.0, 1.5, 3.3576272817720123, 703.125, 3.2609447552005895, 75, 0, True, False), (2000.0, 8000.0, 2.0, -1.8423523867495541, 3398.4375, 1.7264236272449565, 256, 0, True, True), (8000.0, 16000.0, 2.5, None, None, None, 341, 341, False, None)), False, (3.3576272817720123, 703.125, (250.0, 2000.0), 1.5, 2.1713250153365764, 331, 341, True, False)),
    'axis_stops_short': (-2.7879764847214057, ((250.0, 2000.0, 1.5, 2.88796781509031, 703.125, 2.7914148071971066, 75, 0, True, False), (2000.0, 8000.0, 2.0, -2.3120118534312564, 3398.4375, 2.2040028052297718, 170, 0, True, False), (8000.0, 16000.0, 2.5, None, None, None, 0, 0, False, None)), False, (2.88796781509031, 703.125, (250.0, 2000.0), 1.5, 2.3991465906725606, 245, 0, True, False)),
    'dark_top_octave_only': (0.0, ((250.0, 2000.0, 1.5, 0.0, 257.8125, 0.0, 75, 0, True, True), (2000.0, 8000.0, 2.0, 0.0, 2015.625, 0.0, 256, 0, True, True), (8000.0, 16000.0, 2.5, -6.0, 8015.625, 6.0, 341, 0, True, False)), False, (-6.0, 8015.625, (8000.0, 16000.0), 2.5, 4.274091382136927, 672, 0, True, False)),
    'woofer_bump_at_tolerance': (0.021597300733122, ((250.0, 2000.0, 1.5, 1.478402699266878, 421.875, 0.34206852424866185, 75, 0, True, True), (2000.0, 8000.0, 2.0, -0.021597300733122, 2015.625, 0.021597300733122, 256, 0, True, True), (8000.0, 16000.0, 2.5, -0.021597300733122, 8015.625, 0.021597300733122, 341, 0, True, True)), True, (1.478402699266878, 421.875, (250.0, 2000.0), 1.5, 0.1160759857045979, 672, 0, True, True)),
}

# The frozen dB figures are compared to this, not to bit equality: a numpy
# reduction's summation order is a build/SIMD detail, and a verdict-moving
# change is at least nine orders of magnitude larger than this. The DISCRETE
# fields (passed / evaluable / counts / the frequency axis values) are
# compared exactly, because nothing about them is a rounding artefact.
_VERDICT_TOLERANCE_DB = 1e-12

# The candidate reference frames #1857's Q-E weighs, plus three more, so the
# frame-invariance claim is tested across a span rather than one alternative.
# 250-2000 is the woofer-anchored candidate; 250-8000 is what ships today.
_CANDIDATE_FRAMES_HZ = (
    (250.0, 2000.0),
    (250.0, 8000.0),
    (2000.0, 8000.0),
    (250.0, 16000.0),
    (300.0, 6000.0),
)


def _power_mean_db(values_db: np.ndarray) -> float:
    """An independent re-implementation of the module's power mean, written
    fresh here so a regression to the linear-dB-average trap this repo has
    shipped three times is caught rather than mirrored."""
    return float(10.0 * np.log10(np.mean(np.power(10.0, np.asarray(values_db) / 10.0))))


# --------------------------------------------------------------------------- #
# the mechanism, red-first
# --------------------------------------------------------------------------- #


def test_the_pointer_names_the_flat_band_as_the_worst_one():
    """#1857 reproduced: the shipped pointer blames a band that is flat.

    Asserted as it STILL SHIPS, deliberately. Fixing the pointer means
    re-anchoring the reference frame, which moves graded verdicts, and WHICH
    anchor to use is an open owner decision (Q-E). So this test does not
    describe a bug that was fixed — it is the standing reproduction the
    ruling will be applied against, and the reason the attribution reading
    below exists.
    """
    curve = _dark_tweeter(5.0)
    report = evaluate_flat_spec(_FREQS_HZ, curve)
    gauge = spec_flatness_gauge(report)
    woofer, mid, top = report.bands

    # The pointer names the woofer band, at a frequency in it.
    assert gauge.max_band_hz == (250.0, 2000.0)
    assert gauge.max_db == pytest.approx(3.3576, abs=5e-4)
    assert 250.0 <= gauge.max_hz < 2000.0

    # ...and that band is the ONLY one that fails, while the two bands that
    # are genuinely 5 dB down both pass.
    assert woofer.passed is False
    assert mid.passed is True and top.passed is True

    # ...even though the woofer band is flat to a tenth of a dB: essentially
    # all of its +3.36 dB is where the band SITS, not anything at 703 Hz.
    assert abs(woofer.max_ripple_db) < 0.11
    assert woofer.level_deviation_db == pytest.approx(3.26, abs=0.01)

    # The frame drag that does it, measured against a woofer-anchored mean.
    woofer_anchored_db = _power_mean_db(curve[(_FREQS_HZ >= 250.0) & (_FREQS_HZ < 2000.0)])
    assert woofer_anchored_db - report.reference_db == pytest.approx(3.26, abs=0.01)


def test_darkening_only_the_tweeter_inflates_the_untouched_woofers_number():
    """The contamination, isolated: the woofer's own bins never change, and
    its reported deviation grows anyway — because the shared reference moved.

    This is what makes the pointer a frame artefact rather than a reading of
    the woofer. The tilt, on the same shapes, tracks the tweeter's ACTUAL
    deficit instead.
    """
    woofer_slice = _FREQS_HZ < 2000.0
    baseline = _dark_tweeter(0.0)
    reported = []
    for depth_db in (0.0, 2.0, 5.0, 8.0, 12.0):
        curve = _dark_tweeter(depth_db)
        # Control: the woofer half of the curve is byte-identical every time.
        assert np.array_equal(curve[woofer_slice], baseline[woofer_slice])
        report = evaluate_flat_spec(_FREQS_HZ, curve)
        reported.append(report.bands[0].max_deviation_db)
        # The frame-free reading tracks the real deficit, within the ripple.
        tilt = spec_band_tilt(report)
        assert tilt.step_db == pytest.approx(depth_db, abs=0.25)

    # Strictly increasing: every dB the tweeter loses is partly charged to
    # the woofer.
    assert reported == sorted(reported)
    assert reported[0] < 0.0 < 1.5 < reported[2]


# --------------------------------------------------------------------------- #
# the verdict-invariance pin
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", sorted(VERDICT_GOLDEN))
def test_no_graded_number_moved_on_any_corpus_shape(shape):
    """Every graded figure equals what ``origin/main`` produced before the
    attribution split existed — shape for shape, band for band.

    This is #1857's load-bearing constraint made mechanical: the issue is
    about the POINTER, and the reference normalization the pointer rides on
    is what the verdicts are computed from, so a fix that quietly re-framed
    anything would change what passes. The golden was captured from the
    pre-change tree; a diff here is that change, not a flake.
    """
    expected_reference_db, expected_bands, expected_overall, expected_gauge = (
        VERDICT_GOLDEN[shape]
    )
    reference_db, bands, overall_passed, gauge = _verdict_tuple(shape)

    assert reference_db == pytest.approx(
        expected_reference_db, abs=_VERDICT_TOLERANCE_DB
    )
    assert overall_passed is expected_overall
    assert len(bands) == len(expected_bands)
    for band, expected in zip(bands, expected_bands):
        # Discrete first, so a failure names the verdict rather than a digit.
        assert band[8] is expected[8], "evaluable"
        assert band[9] is expected[9], "passed"
        assert (band[0], band[1], band[2]) == (expected[0], expected[1], expected[2])
        assert (band[4], band[6], band[7]) == (expected[4], expected[6], expected[7])
        for index in (3, 5):
            if expected[index] is None:
                assert band[index] is None
            else:
                assert band[index] == pytest.approx(
                    expected[index], abs=_VERDICT_TOLERANCE_DB
                )
    assert (gauge[2], gauge[3], gauge[5], gauge[6], gauge[7], gauge[8]) == (
        expected_gauge[2], expected_gauge[3], expected_gauge[5],
        expected_gauge[6], expected_gauge[7], expected_gauge[8],
    )
    assert gauge[1] == expected_gauge[1], "max_hz"
    for index in (0, 4):
        if expected_gauge[index] is None:
            assert gauge[index] is None
        else:
            assert gauge[index] == pytest.approx(
                expected_gauge[index], abs=_VERDICT_TOLERANCE_DB
            )


def test_the_corpus_covers_both_verdicts_and_every_band_outcome():
    """The pin above is only worth its literals if the population it covers
    is not all one answer. Fails loudly if the corpus is ever trimmed down to
    shapes that cannot distinguish a regression."""
    overalls = {golden[2] for golden in VERDICT_GOLDEN.values()}
    band_verdicts = {
        band[9] for golden in VERDICT_GOLDEN.values() for band in golden[1]
    }
    assert overalls == {True, False}
    assert band_verdicts == {True, False, None}  # None == unevaluable
    assert len(VERDICT_GOLDEN) >= 16


# --------------------------------------------------------------------------- #
# the frame-free attribution
# --------------------------------------------------------------------------- #


def test_the_tilt_is_the_same_number_under_every_candidate_frame(monkeypatch):
    """``step_db`` and ``max_ripple_db`` do not move when the reference band
    does — which is the whole reason they can ship while Q-E is open.

    The positive control rides in the same loop: ``max_deviation_db`` DOES
    move, by dB, across the same frames. Without it this test could pass on
    an evaluator that ignored ``REFERENCE_BAND_HZ`` entirely.
    """
    curve = _dark_tweeter(5.0)
    steps: list[float] = []
    ripples: list[tuple[float | None, ...]] = []
    deviations: list[float] = []
    for frame in _CANDIDATE_FRAMES_HZ:
        monkeypatch.setattr(flat_spec, "REFERENCE_BAND_HZ", frame)
        report = evaluate_flat_spec(_FREQS_HZ, curve)
        steps.append(spec_band_tilt(report).step_db)
        ripples.append(tuple(band.max_ripple_db for band in report.bands))
        deviations.append(report.bands[0].max_deviation_db)

    assert max(steps) - min(steps) < 1e-12
    assert len(set(ripples)) == 1, "ripple is bit-identical, not merely close"
    # Positive control: the graded number really is frame-dependent.
    assert max(deviations) - min(deviations) > 3.0


def test_the_tilt_names_the_step_the_dark_tweeter_actually_has():
    curve = _dark_tweeter(5.0)
    tilt = spec_band_tilt(evaluate_flat_spec(_FREQS_HZ, curve))

    assert tilt.evaluable is True
    assert tilt.n_bands == 3
    assert tilt.step_db == pytest.approx(5.0, abs=0.05)
    assert tilt.high_band_hz == (250.0, 2000.0)
    assert tilt.low_band_hz == (8000.0, 16000.0)
    assert tilt.step_db >= 0.0


def test_the_tilt_finds_a_dark_low_band_too():
    """The mirror case, and the one that separates a MAGNITUDE step from a
    signed one: here the largest level difference is negative in SPEC_BANDS
    walk order, so a tilt built on ``level_a - level_b`` would pick the two
    bands that agree and report ~0 dB while a 5 dB step sat right there.
    """
    tilt = spec_band_tilt(evaluate_flat_spec(_FREQS_HZ, _dark_woofer(5.0)))

    assert tilt.step_db == pytest.approx(5.0, abs=0.05)
    assert tilt.low_band_hz == (250.0, 2000.0)
    assert tilt.high_band_hz in ((2000.0, 8000.0), (8000.0, 16000.0))


def test_the_band_level_is_a_power_mean_not_a_dB_average():
    """The linear-dB-average trap ``_power_mean_db`` exists to prevent, now
    also guarding the split.

    The band levels must be checked on a curve with a LARGE within-band
    spread: on a nearly-flat band the two averages agree to a rounding error
    and the shape proves nothing. The 9 dB notch shape separates them by
    ~0.6 dB, well outside the tolerance below. The expected value comes from
    :func:`_power_mean_db`, an independent re-implementation written in this
    file, never from the module's own helper.
    """
    curve = np.zeros_like(_FREQS_HZ)
    curve[(_FREQS_HZ >= 700.0) & (_FREQS_HZ < 780.0)] -= 9.0
    curve[(_FREQS_HZ >= 3000.0) & (_FREQS_HZ < 4000.0)] += 6.0
    report = evaluate_flat_spec(_FREQS_HZ, curve)

    separated = 0
    for band in report.bands:
        inside = (_FREQS_HZ >= band.f_lo_hz) & (_FREQS_HZ < band.f_hi_hz)
        power_mean_db = _power_mean_db(curve[inside])
        db_average = float(np.mean(curve[inside]))
        assert band.level_deviation_db == pytest.approx(
            power_mean_db - report.reference_db, abs=1e-9
        )
        if abs(power_mean_db - db_average) > 0.2:
            separated += 1
    # The shape has to actually distinguish the two averages, or the
    # assertion above is satisfied by either formula.
    assert separated >= 2


def test_the_tilt_is_zero_when_the_bands_sit_level():
    """Ripple alone is not a tilt: a curve that wobbles ±3 dB inside every
    band but has no level step must not read as one."""
    tilt = spec_band_tilt(
        evaluate_flat_spec(_FREQS_HZ, _ripple(_FREQS_HZ, 3.0, 9.0))
    )
    assert tilt.evaluable is True
    assert tilt.step_db < 0.6


def test_the_tilt_needs_two_bands_and_never_fabricates_a_step():
    """One band cannot tilt against itself — ``None``, not ``0.0``."""
    band = BandResult(
        f_lo_hz=250.0, f_hi_hz=2000.0, tolerance_db=1.5,
        max_deviation_db=1.0, max_deviation_hz=400.0, rms_deviation_db=0.5,
        n_bins=10, n_excluded=0, evaluable=True, passed=True,
        level_deviation_db=1.0, max_ripple_db=0.0, max_ripple_hz=400.0,
    )
    tilt = spec_band_tilt(
        FlatSpecReport(
            reference_db=-30.0, bands=(band,), overall_passed=True,
            excluded_intervals=(), best_effort_above_hz=16000.0,
            smoothing_fraction=3,
        )
    )
    assert tilt.evaluable is False
    assert tilt.step_db is None
    assert tilt.high_band_hz is None and tilt.low_band_hz is None
    assert tilt.n_bands == 1


def test_a_band_with_no_measured_level_is_skipped_not_defaulted_to_zero():
    """A report from before the split (or hand-built) carries
    ``level_deviation_db=None``. Treating that as 0 dB would manufacture a
    step against whatever the other bands measured — the exact false reading
    the tilt exists to expose."""
    levelled = BandResult(
        f_lo_hz=250.0, f_hi_hz=2000.0, tolerance_db=1.5,
        max_deviation_db=1.0, max_deviation_hz=400.0, rms_deviation_db=0.5,
        n_bins=10, n_excluded=0, evaluable=True, passed=True,
        level_deviation_db=4.0, max_ripple_db=0.0, max_ripple_hz=400.0,
    )
    legacy = BandResult(
        f_lo_hz=2000.0, f_hi_hz=8000.0, tolerance_db=2.0,
        max_deviation_db=1.0, max_deviation_hz=3000.0, rms_deviation_db=0.5,
        n_bins=10, n_excluded=0, evaluable=True, passed=True,
    )
    tilt = spec_band_tilt(
        FlatSpecReport(
            reference_db=-30.0, bands=(levelled, legacy), overall_passed=True,
            excluded_intervals=(), best_effort_above_hz=16000.0,
            smoothing_fraction=3,
        )
    )
    assert tilt.evaluable is False
    assert tilt.n_bands == 1
    assert tilt.step_db is None


def test_an_exact_tie_between_pairs_resolves_to_the_lowest_pair():
    """Deterministic, and the same tie rule ``spec_flatness_gauge`` uses:
    an ordered walk with a strict ``>``, so the first pair wins."""
    def _band(lo, hi, level_db):
        return BandResult(
            f_lo_hz=lo, f_hi_hz=hi, tolerance_db=1.5,
            max_deviation_db=level_db, max_deviation_hz=lo, rms_deviation_db=0.0,
            n_bins=10, n_excluded=0, evaluable=True, passed=True,
            level_deviation_db=level_db, max_ripple_db=0.0, max_ripple_hz=lo,
        )

    # Levels 2, 0, -2: |2-0| = |0-(-2)| = 2, and |2-(-2)| = 4 is the winner;
    # drop the outer pair by making it tie with the first instead.
    tilt = spec_band_tilt(
        FlatSpecReport(
            reference_db=0.0,
            bands=(_band(250.0, 2000.0, 2.0), _band(2000.0, 8000.0, 0.0),
                   _band(8000.0, 16000.0, 2.0)),
            overall_passed=True, excluded_intervals=(),
            best_effort_above_hz=16000.0, smoothing_fraction=3,
        )
    )
    assert tilt.step_db == pytest.approx(2.0)
    assert tilt.high_band_hz == (250.0, 2000.0)
    assert tilt.low_band_hz == (2000.0, 8000.0)


def test_the_split_adds_up_bin_for_bin():
    """``deviation_i == ripple_i + level_deviation_db`` at the bin the ripple
    pointer names — the identity that makes the split an explanation of the
    pointer rather than a second, unrelated number."""
    curve = _dark_tweeter(5.0)
    report = evaluate_flat_spec(_FREQS_HZ, curve)
    for band in report.bands:
        index = int(np.flatnonzero(_FREQS_HZ == band.max_ripple_hz)[0])
        deviation_db = curve[index] - report.reference_db
        assert deviation_db == pytest.approx(
            band.max_ripple_db + band.level_deviation_db, abs=1e-12
        )
        # ...and the named bin is the band's largest ABSOLUTE excursion, not
        # merely its largest positive one: a dip is as much a defect as a
        # peak, and the sign is kept rather than the direction chosen.
        inside = (_FREQS_HZ >= band.f_lo_hz) & (_FREQS_HZ < band.f_hi_hz)
        band_level_db = report.reference_db + band.level_deviation_db
        assert abs(band.max_ripple_db) == pytest.approx(
            float(np.max(np.abs(curve[inside] - band_level_db))), abs=1e-12
        )
        # A power mean lies between its inputs, so some bin always has ripple
        # of each sign -- the band's distance from the frame can never read
        # SMALLER than where the band sits.
        assert abs(band.max_deviation_db) >= abs(band.level_deviation_db) - 1e-12


def test_the_gauge_carries_the_pointed_bands_own_split():
    """Lifted from the band the pointer named, never recomputed."""
    report = evaluate_flat_spec(_FREQS_HZ, _dark_tweeter(5.0))
    gauge = spec_flatness_gauge(report)
    worst = next(
        band for band in report.bands
        if (band.f_lo_hz, band.f_hi_hz) == gauge.max_band_hz
    )
    assert gauge.max_band_level_deviation_db == worst.level_deviation_db
    assert gauge.max_band_ripple_db == worst.max_ripple_db
    assert gauge.tilt == spec_band_tilt(report)


def test_an_ungradeable_report_reports_no_split_rather_than_zero():
    """A report whose every band is unevaluable: the gauge's split is
    ``None``, not ``0.0``, and the tilt is unevaluable with an honest
    ``n_bands``.

    Hand-built for the reason :func:`spec_convergence_residual`'s own guard
    documents: :data:`REFERENCE_BAND_HZ` is exactly ``SPEC_BANDS[0]`` union
    ``SPEC_BANDS[1]``, so an :func:`evaluate_flat_spec` call that did not
    raise on an empty reference band always leaves a gradeable bin behind.
    This state arrives from persistence, not from the evaluator.
    """
    def _unevaluable(lo, hi, tolerance_db):
        return BandResult(
            f_lo_hz=lo, f_hi_hz=hi, tolerance_db=tolerance_db,
            max_deviation_db=None, max_deviation_hz=None, rms_deviation_db=None,
            n_bins=12, n_excluded=12, evaluable=False, passed=None,
        )

    gauge = spec_flatness_gauge(
        FlatSpecReport(
            reference_db=-30.0,
            bands=tuple(_unevaluable(*band) for band in SPEC_BANDS),
            overall_passed=False, excluded_intervals=(),
            best_effort_above_hz=16000.0, smoothing_fraction=3,
        )
    )
    assert gauge.evaluable is False
    assert gauge.max_band_level_deviation_db is None
    assert gauge.max_band_ripple_db is None
    assert gauge.tilt.evaluable is False
    assert gauge.tilt.step_db is None
    assert gauge.tilt.n_bands == 0


def test_the_no_tilt_default_is_a_null_object_not_a_reading():
    assert NO_BAND_TILT == BandTilt(
        step_db=None, high_band_hz=None, low_band_hz=None,
        n_bands=0, evaluable=False,
    )
    bare = spec_flatness_gauge(
        FlatSpecReport(
            reference_db=0.0, bands=(), overall_passed=False,
            excluded_intervals=(), best_effort_above_hz=16000.0,
            smoothing_fraction=3,
        )
    )
    assert bare.tilt == NO_BAND_TILT
    assert bare.to_dict()["tilt"]["step_db"] is None


def test_the_gauge_dict_is_json_safe_and_carries_the_tilt():
    import json

    payload = spec_flatness_gauge(
        evaluate_flat_spec(_FREQS_HZ, _dark_tweeter(5.0))
    ).to_dict()
    assert set(payload["tilt"]) == {
        "step_db", "high_band_hz", "low_band_hz", "n_bands", "evaluable",
    }
    assert payload["tilt"]["high_band_hz"] == [250.0, 2000.0]
    json.loads(json.dumps(payload))


# --------------------------------------------------------------------------- #
# the surfaces that render it
# --------------------------------------------------------------------------- #


def test_the_household_lines_say_the_band_is_flat_and_names_the_real_step():
    block = spec_flatness_gauge(
        evaluate_flat_spec(_FREQS_HZ, _dark_tweeter(5.0))
    ).to_dict()
    lines = _flatness_lines_from_block(block)
    rendered = " | ".join(lines)

    # The pointer still says what it says...
    assert "flatness +3.36 dB from the 250–8000 Hz reference mean" in rendered
    # ...but it is no longer the whole sentence.
    assert "+3.26 dB is where the whole 250–2000 Hz band sits" in rendered
    assert "its own worst excursion from that level is -0.10 dB" in rendered
    assert "band levels differ by 5.01 dB, a reading no reference choice moves" in rendered
    assert "250–2000 Hz sits above 8000–16000 Hz" in rendered


def test_a_block_from_an_older_build_renders_neither_attribution_line():
    """Absent keys mean absent lines — never a line built around a missing
    half, and never a fabricated zero."""
    block = spec_flatness_gauge(
        evaluate_flat_spec(_FREQS_HZ, _dark_tweeter(5.0))
    ).to_dict()
    legacy = {
        key: value for key, value in block.items()
        if key not in ("tilt", "max_band_level_deviation_db", "max_band_ripple_db")
    }
    lines = _flatness_lines_from_block(legacy)
    assert any("flatness +3.36 dB from" in line for line in lines)
    assert not any("band levels differ by" in line for line in lines)
    assert not any("is where the whole" in line for line in lines)


def test_a_level_tilt_renders_no_direction_it_cannot_show():
    """At 0.00 dB the printed step supports no ordering, so the "sits above"
    clause is dropped rather than asserting one."""
    block = spec_flatness_gauge(
        evaluate_flat_spec(_FREQS_HZ, np.zeros_like(_FREQS_HZ))
    ).to_dict()
    line = next(
        line for line in _flatness_lines_from_block(block)
        if "band levels differ by" in line
    )
    assert line.endswith("a reading no reference choice moves")
    assert "sits above" not in line


def test_the_journal_carries_the_frame_free_step():
    block = spec_flatness_gauge(
        evaluate_flat_spec(_FREQS_HZ, _dark_tweeter(5.0))
    ).to_dict()
    assert _flatness_tilt_log_field(block) == "5.01dB:250-2000Hz>8000-16000Hz"
    # logfmt safety: one token, nothing that needs quoting.
    assert " " not in _flatness_tilt_log_field(block)


@pytest.mark.parametrize(
    "block",
    [
        None,
        {},
        {"tilt": None},
        {"tilt": {"evaluable": False, "step_db": None}},
        # A complete, well-formed payload that the tilt itself says is not
        # evaluable: the token must obey that flag, not the numbers beside it.
        {"tilt": {"evaluable": False, "step_db": 5.0,
                  "high_band_hz": [250.0, 2000.0], "low_band_hz": [2000.0, 8000.0]}},
        {"tilt": {"evaluable": True, "step_db": None,
                  "high_band_hz": [250.0, 2000.0], "low_band_hz": [2000.0, 8000.0]}},
        {"tilt": {"evaluable": True, "step_db": 5.0, "high_band_hz": [250.0],
                  "low_band_hz": [2000.0, 8000.0]}},
        {"tilt": {"evaluable": True, "step_db": True,
                  "high_band_hz": [250.0, 2000.0], "low_band_hz": [2000.0, 8000.0]}},
    ],
)
def test_the_journal_field_is_empty_rather_than_fabricated(block):
    assert _flatness_tilt_log_field(block) == ""


def test_module_constants_still_describe_the_shipped_frame():
    """The frame this module reasons about is the one that ships. If Q-E is
    ever ruled and these move, every claim above needs re-reading — which is
    what this assertion is for."""
    assert REFERENCE_BAND_HZ == (250.0, 8000.0)
    assert SPEC_BANDS == (
        (250.0, 2000.0, 1.5), (2000.0, 8000.0, 2.0), (8000.0, 16000.0, 2.5),
    )
