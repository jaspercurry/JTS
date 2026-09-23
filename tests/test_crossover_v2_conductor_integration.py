# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: the integration reorder, driver linearization, and spec-grading the prediction."""

from __future__ import annotations

import numpy as np
from dataclasses import replace
from jasper.active_speaker.crossover_v2.diagnostics import spec_report_for_predicted_sum
from jasper.active_speaker.crossover_v2_flow import PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB
from jasper.active_speaker.crossover_v2.planning import analysis_json as _analysis_json
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY,
    TRANSIENT_AUTO_RETRY_CODES,
)
from jasper.audio_measurement.program_analysis import (
    CrossoverCandidate,
    DriftEstimate,
    ProgramAnalysis,
)
from tests.crossover_v2_fixtures import (
    _alignment,
)


def test_analysis_json_round_trips_trim_band_average_db():
    """#1667 evidence round-trip: `_analysis_json`'s frozen fingerprint
    carries `trim_band_average_db` alongside the applied `trim_db`, rounded
    the same way, so replay/forensics can always compare the two — even
    when the candidate predates this field (`None` passthrough)."""
    freqs = np.linspace(100.0, 20000.0, 64)
    cand = CrossoverCandidate(
        trim_db={"woofer": 0.0, "tweeter": -0.0754},
        polarity="normal", delay_us=150.0,
        predicted_ripple_db=0.03, confidence=0.9,
        trim_band_average_db={"woofer": 0.0, "tweeter": -9.4754},
    )
    analysis = ProgramAnalysis(
        phase="measure", program_id="p1", locations=(),
        drift=DriftEstimate(
            epsilon_ppm=1.0, max_residual_samples=0.0,
            glitch_detected=False,
        ),
        alignment=_alignment(), candidate=cand,
        predicted_sum=(freqs, np.zeros_like(freqs)),
        glitch_detected=False,
    )
    evidence = _analysis_json(analysis)
    assert evidence["trim_db"] == {"woofer": 0.0, "tweeter": -0.0754}
    assert evidence["trim_band_average_db"] == {"woofer": 0.0, "tweeter": -9.4754}

    # Legacy/pre-#1667 construction site: candidate has no evidence field.
    legacy_cand = CrossoverCandidate(
        trim_db={"woofer": 0.0, "tweeter": -2.211}, polarity="normal",
        delay_us=150.0, predicted_ripple_db=0.8, confidence=0.8,
    )
    legacy_analysis = replace(analysis, candidate=legacy_cand)
    legacy_evidence = _analysis_json(legacy_analysis)
    assert legacy_evidence["trim_db"] == {"woofer": 0.0, "tweeter": -2.211}
    assert legacy_evidence["trim_band_average_db"] is None

    # **The swept drift table this fixture's verdicts come from** (R10a, #1817),
    # kept because it is what makes the acceptance above readable. Measured by
    # sweeping the forced drift and reading
    # ``event=correction.crossover_v2_prediction_gate`` with the pre-2b policy,
    # so every row is the COMMITTED SCAN being graded (baseline 0.957 dB rms in
    # every row; the floor is 0.5 dB):
    #
    #   drift dB     0      1      2      3       4       5       6      7      8
    #   improve  +0.657 +0.657 +0.657 +0.657  -0.324  -0.688  -1.087 -1.524 -1.998
    #   verdict   accept accept accept accept  refuse  refuse  refuse refuse refuse
    #
    # The gate DISCRIMINATES on this fixture: a correct trim ships, a mistrim of
    # 4 dB or more is caught as the regression it is. Under the flat target it
    # refused at every drift including 0.0 (-0.293 dB), because the fit's own
    # crossover-fighting cuts made even an untouched trim fail to beat its
    # baseline — the gate could not tell a wild trim from a good one.
    #
    # The last two columns are what #2291 removed from the shipping path: past
    # the 6.0 dB margin the pair no longer reaches this gate at all, because it
    # is no longer the committed pair. The gate stays as the backstop for the
    # 0-6 dB band, where a scan is still trusted to polish.


# PR-L4 item 2 — spec-grade the prediction before auto-apply


def test_predicted_spec_report_is_graded_on_the_shared_analysis_grid():
    """``spec_report_for_predicted_sum`` decimates before it smooths.

    Not cosmetic. ``smooth_fractional_octave`` is an O(bins x window) Python
    loop — ~11 s on a laptop at a raw 512k-point prediction grid, worse on a
    Pi 5 — and this runs at the confirm seam with a household waiting on the
    apply. It block-averages onto ``MAX_ANALYSIS_BINS`` first, the bound the
    combiner already adopted for the same reason, which is also what puts the
    predicted curve at the same grid density as the measured one it is compared
    against."""
    from jasper.audio_measurement.spatial_combine import MAX_ANALYSIS_BINS

    freqs = np.fft.rfftfreq(1 << 16, 1.0 / 48000.0)
    assert freqs.size > MAX_ANALYSIS_BINS  # the fixture must exercise the bound
    report = spec_report_for_predicted_sum((freqs, np.zeros(freqs.size)))

    assert report is not None
    graded_bins = sum(band.n_bins for band in report.bands)
    assert 0 < graded_bins <= MAX_ANALYSIS_BINS
    # A flat curve is flat at any grid density.
    assert report.overall_within_target is True


def test_predicted_spec_report_is_unknown_never_a_pass_on_bad_input():
    """``None`` in, ``None`` out — and a malformed pair degrades the same way
    rather than raising into the confirm seam. The caller must read that as
    "no evidence", which the gate test below pins."""
    assert spec_report_for_predicted_sum(None) is None
    assert spec_report_for_predicted_sum((np.array([]), np.array([]))) is None
    assert spec_report_for_predicted_sum(("not", "arrays")) is None


def test_prediction_gate_tolerance_is_the_models_own_tracking_error():
    """The third tolerance's derivation, pinned like its two siblings (PR-L4
    review: it was the only one without a test).

    Since B1 made both terms the same instrument, the comparison carries no
    measurement noise — so the threshold is a product-policy floor, and the
    floor is the gap between what the model predicts and what the hardware
    realizes. ``_fit_linearization`` records that as ~0.5 dB for the complex
    correction model on JTS3. An improvement smaller than the model's own
    tracking error is not one we can honestly claim."""
    complex_model_tracking_error_db = 0.5
    assert PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB == complex_model_tracking_error_db
    # And well under the zero-phase model it replaced (~2.0 dB), which is the
    # regime where "improvement" would have been indistinguishable from noise.
    assert PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB < 2.0


def test_the_accountability_reasons_are_gone_from_the_registry():
    """No refusal, no registry row — the nanny burn-down's own bookkeeping.

    A registry row for a refusal nothing raises is copy that can never be read,
    and leaving one behind is how a veto comes back quietly: the row is the
    thing a future change would reach for. Item 2's two went with deviation
    (c); item 1's ``driver_levels_disagree`` went with deviation (i). All three
    absences are asserted together because they are one rule applied three
    times.

    A durable state persisted before either change can still carry these
    literals, and ``_failure_history_note`` reads the registry with ``.get``,
    so an old code with no row degrades to the generic clause rather than
    raising — which is why deleting the row is safe as well as correct.
    """
    assert "driver_levels_disagree" not in REASON_REGISTRY
    assert "correction_not_an_improvement" not in REASON_REGISTRY
    assert "prescribed_correction_not_an_improvement" not in REASON_REGISTRY
    assert "driver_levels_disagree" not in TRANSIENT_AUTO_RETRY_CODES


# SF2 / SF3 (adversarial review, 2026-07-24 — #1668 PR-C review)
#
# SF2: an eligible speaker whose fit engine raises must degrade EXACTLY to
# the ineligible path (raw trim, empty linearization) -- never fail the
# whole MEASURE accept. SF3: crossover_v2_measure_diag's new
# `linearization=` field names which of the five outcomes this attempt's
# candidate build took, for corpus-review greppability.
