# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: the integration reorder, driver linearization, and spec-grading the prediction."""

from __future__ import annotations

import logging
import types
import numpy as np
import pytest
from dataclasses import replace
from jasper.active_speaker.crossover_v2 import (
    intervention as iv,
)
from jasper.active_speaker.crossover_v2_flow import (
    PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    _analysis_json,
    spec_report_for_predicted_sum,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY,
    REASON_CAPTURE_TIMEOUT,
    TRANSIENT_AUTO_RETRY_CODES,
)
from jasper.audio_measurement.program_analysis import (
    CrossoverCandidate,
    DriftEstimate,
    ProgramAnalysis,
    realized_branch_level_match,
    solve_branch_trims,
)
from tests._log_events import event_fields, event_records
from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    _DIAG_LOGGER,
    _LINEARIZABLE_FREQS_HZ,
    _alignment,
    _conductor,
    _eligible_measure_analysis,
    _fixture_branch_db,
    _measure_analysis,
    _run_phase,
)


# --- conductor integration reorder ------------------------------------------












def _measured_level_frame(conductor, *, woofer_db=None, tweeter_db=None):
    """The trim's OWN level frame, re-measured by the test from published inputs.

    The anchor's give-back is measured over ``branch_level_bands_hz`` — the
    same estimator, averaging domain and half-bands that solved ``raw_trim_db``
    and that grade the committed pair — because a give-back spent against a
    trim has to be measured in that trim's frame. A give-back read over each
    driver's own CORE band (``LinearizationFit.correction_giveback_db``)
    answers a different question, and its per-role DIFFERENCE lands as pure
    inter-driver level error: on the jts3 horn tweeter, 2026-08-19, that was
    3.67 dB of hot tweeter.

    **Every input is sourced independently of the planner**, which is what
    stops this being a restatement of ``plan_linearization``'s own arithmetic:
    the spans come from the conductor's OWN MEASURE program, the
    pre-correction pair is the fixture's own declared branch curves, and the
    post-correction pair is those curves times the correction the candidate
    PUBLISHES. Nothing is read back out of the planner, so a change of band,
    of estimator, of sign, or of which pair is pre and which is post fails
    here rather than being absorbed.

    Returns the frame as a namespace: ``giveback_db`` (per role),
    ``linearized`` (the post-correction pair), ``spans``, and ``freqs``.
    """
    from jasper.active_speaker.linearization_fit import (
        LinearizationFilter,
        complex_correction_response,
    )

    default_woofer_db, default_tweeter_db = _fixture_branch_db()
    curves = {
        "woofer": default_woofer_db if woofer_db is None else woofer_db,
        "tweeter": default_tweeter_db if tweeter_db is None else tweeter_db,
    }
    freqs = _LINEARIZABLE_FREQS_HZ
    program = conductor.program_for_phase(PHASE_MEASURE)
    spans = {
        role: (program.segment(seg).f1_hz, program.segment(seg).f2_hz)
        for role, seg in (("woofer", "sweep_w"), ("tweeter", "sweep_t"))
    }
    raw = {
        role: (10.0 ** (np.asarray(curve) / 20.0)).astype(complex)
        for role, curve in curves.items()
    }
    linearized = {
        role: raw[role] * complex_correction_response(
            [
                LinearizationFilter(**f)
                for f in conductor.candidate.linearization[role]["filters"]
            ],
            freqs,
        )
        for role in ("woofer", "tweeter")
    }

    def _levels(pair):
        _residual_w, _residual_t, level_w, level_t = solve_branch_trims(
            freqs, pair["woofer"], pair["tweeter"], FC_HZ,
            woofer_span_hz=spans["woofer"], tweeter_span_hz=spans["tweeter"],
        )
        return {"woofer": level_w, "tweeter": level_t}

    before, after = _levels(raw), _levels(linearized)
    return types.SimpleNamespace(
        freqs=freqs,
        spans=spans,
        linearized=linearized,
        giveback_db={
            role: before[role] - after[role] for role in ("woofer", "tweeter")
        },
    )


def _inter_driver_level_error_db(frame, trim_db):
    """One trim pair's REALIZED inter-driver level error on the linearized pair.

    The anchor's defining property, and the one the band-matched give-back
    buys: ``raw_trim`` level-matches the PRE-correction pair, and adding back
    exactly what the correction removed FROM THAT SAME BAND puts the
    POST-correction pair at the same handoff level. The residual is therefore
    not "close to zero" by luck — it is zero up to the 3-decimal rounding
    ``_solve_fixture_raw_trim`` applies to the fixture's own raw trim, which
    bounds it at 1e-3 dB.
    """
    return realized_branch_level_match(
        frame.freqs, frame.linearized["woofer"], frame.linearized["tweeter"],
        FC_HZ,
        trim_w_db=trim_db["woofer"], trim_t_db=trim_db["tweeter"],
        woofer_span_hz=frame.spans["woofer"],
        tweeter_span_hz=frame.spans["tweeter"],
    ).difference_db






def test_straddling_band_still_runs_the_linearized_ripple_polish(caplog):
    """The control for the test above: the DEFAULT fixture's tweeter is swept
    from 300 Hz, so its overlap band straddles Fc and the polish still runs —
    the guard keys on the band, not on 'linearization is happening'."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert not event_records(
        caplog, "correction.crossover_v2_linearization_ripple_trim_skipped"
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


def test_measure_diag_logs_trim_ripple_gain_db(caplog):
    """#1667 observability: the measure_diag line carries the
    applied-vs-band-average delta for the tweeter trim -- 0.0 when the
    ripple-optimal search left the trim exactly at its seed (or the sanity
    guard fell back to it), the actual recovery amount otherwise. `None`
    only when the candidate predates trim_band_average_db."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: replace(
        _measure_analysis(program),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.1, "tweeter": -0.5},
            polarity="normal", delay_us=150.0,
            predicted_ripple_db=0.03, confidence=0.8,
            trim_band_average_db={"woofer": -3.1, "tweeter": -9.5},
        ),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["trim_ripple_gain_db"] == "9.0"  # -0.5 - (-9.5)
    caplog.clear()

    # No band-average evidence on this candidate (legacy/test construction
    # site) -> None, never a guess.
    fakes2 = FakeSeams()
    fakes2.measure = lambda program: _measure_analysis(program)
    c2 = _conductor(fakes2)
    _run_phase(c2, 1, 1)
    verdict2 = _run_phase(c2, 2, 2)
    assert verdict2["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["trim_ripple_gain_db"] == "null"










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








# --------------------------------------------------------------------------- #
# PR-L4 item 2 — spec-grade the prediction before auto-apply
# --------------------------------------------------------------------------- #


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


def test_an_accountability_gate_no_longer_stamps_a_failure_code():
    """The accountability gate has no refusal left to name to the host.

    This test used to assert the opposite — that item 1's refusal reached the
    household as ``driver_levels_disagree`` rather than as a manufactured
    ``capture_timeout``. The realized-level demotion (doctrine deviation (i))
    removed the refusal, so the correct assertion is the inverse: the same
    fixture that used to raise now completes with no failure code stamped at
    all. Kept rather than deleted because ``last_failure_code`` staying ``None``
    is exactly what a reader needs to see to know the round really did proceed.

    The realized verdict is supplied for the reason its sibling above gives:
    since the #1866 ruling a frame disagreement banks a finding and proceeds,
    so a mislevelled pair has to be handed to the gate rather than provoked."""
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    fakes = FakeSeams()
    far_raw_trim = {"woofer": 0.0, "tweeter": -20.0}
    fakes.measure = lambda program: _eligible_measure_analysis(program, trim_db=far_raw_trim)
    c = _conductor(fakes)

    def _still_mislevelled(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-20.0, difference_db=-20.0,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            iv, "realized_level_match", _still_mislevelled
        )
        _run_phase(c, 1, 1)
        assert c.last_failure_code is None
        _run_phase(c, 2, 2)
    assert c.last_failure_code is None
    assert c.last_failure_code != REASON_CAPTURE_TIMEOUT


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


# --------------------------------------------------------------------------- #
# SF2 / SF3 (adversarial review, 2026-07-24 — #1668 PR-C review)
# --------------------------------------------------------------------------- #
#
# SF2: an eligible speaker whose fit engine raises must degrade EXACTLY to
# the ineligible path (raw trim, empty linearization) -- never fail the
# whole MEASURE accept. SF3: crossover_v2_measure_diag's new
# `linearization=` field names which of the five outcomes this attempt's
# candidate build took, for corpus-review greppability.






def _configure_fitted(fakes, _monkeypatch):
    fakes.measure = lambda program: _eligible_measure_analysis(program)


def _configure_ineligible_mic_tier(fakes, _monkeypatch):
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier="consumer")


def _configure_ineligible_repeats(fakes, _monkeypatch):
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="reference", tweeter_repeats=0,
    )


def _configure_trim_rejected(fakes, monkeypatch):
    # Seed-anchored (#1668): force the ripple-optimal tweeter re-solve to
    # drift implausibly far from its band-average seed so it falls back to
    # the seed pair -- distinct from "fitted" even though linearization is
    # populated in both.
    monkeypatch.setattr(
        iv, "solve_ripple_optimal_trim",
        lambda *a, **k: (k["seed_trim_db"] - 20.0, 0.0, k["seed_trim_db"]),
    )
    fakes.measure = lambda program: _eligible_measure_analysis(program)




def test_candidate_built_linearization_field_not_on_measure_diag(caplog):
    """The retired location must not quietly come back carrying a value it
    cannot know on a cloud session."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    measure_diag_fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert "linearization" not in measure_diag_fields

