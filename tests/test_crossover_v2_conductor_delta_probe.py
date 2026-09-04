# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: VERIFY-prediction coherence, the delta probe, and the fit-band headroom charge."""

from __future__ import annotations

import dataclasses
import logging
import types
import numpy as np
import pytest
from dataclasses import replace
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import (
    accountability,
    intervention as iv,
)
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    DELTA_PROBE_VERDICTS,
    VERDICT_FRAME_MISMATCH,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED,
    VERDICT_MODEL_ERROR,
    VERDICT_SAFETY_ONLY,
    VERDICT_UNAVAILABLE,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    DELTA_PROBE_REASON_BY_VERDICT,
    REASON_CORRECTION_ROLLBACK_FAILED,
    REASON_CORRECTION_UNSAFE_RESULT,
    REASON_REGISTRY,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session
from jasper.audio_measurement.program_analysis import (
    predicted_branch_sum,
    solve_branch_trims,
    summed_model_residual_delay_us,
)
from tests.crossover_v2_fixtures import (
    CAPS,
    FC_HZ,
    FakeSeams,
    SESSION_VOLUME_DB,
    _DIAG_LOGGER,
    _FIXTURE_FC_HZ,
    _FIXTURE_RAW_TRIM_DB,
    _LINEARIZABLE_FREQS_HZ,
    _alignment,
    _boost_vocabulary_spy,
    _cloud_conductor,
    _conductor,
    _configured_sections,
    _eligible_measure_analysis,
    _emitted_boosts,
    _fixture_branch_db,
    _fixture_raw_predicted_sum,
    _healthy_crossed_over_pair,
    _plan_spy,
    _preset,
    _probed_conductor,
    _roles,
    _run_phase,
    _solve_fixture_raw_trim,
    _tracking_curve,
    _verify_analysis,
    _vocabularies_seen,
    _walk_measure_cloud_to_close,
)


# --------------------------------------------------------------------------- #
# VERIFY-prediction coherence fix (hardware-validation-caught, #1668 PR-D)
# --------------------------------------------------------------------------- #
#
# Measured live on JTS3: VERIFY's tracking comparison ran a deterministic
# ~1.7 dB mismatch (three-attempt repeatability 1.688-1.699 dB against the
# 1.5 dB VERIFY_TOLERANCE_DB) because the persisted prediction
# (``c.measure_predicted_sum``, threaded into ``MeasurementPriors.
# predicted_sum`` by ``_verify_priors``) was still built from the RAW
# measured branches even when Layer-1a linearization was fitted and its
# correction filters emitted into the live graph. Fix: whenever
# ``_fit_linearization`` runs (the same eligibility gate that emits), it
# also rebuilds the prediction from the SAME linearized branches (W_lin/
# T_lin) at whichever trim this attempt actually committed to.


def test_measure_predicted_sum_uses_linearized_branches_when_fitted(monkeypatch):
    """The regression: once linearization is fitted (not the wild-trim
    fallback), the persisted VERIFY prediction must equal
    ``predicted_branch_sum`` evaluated on the SAME linearized branches
    ``_fit_linearization`` used internally, at the resolved trim -- and must
    differ measurably from the fixture's own raw (all-zero) prediction,
    proving the override actually took effect."""

    captured: dict = {}
    real_solve = iv.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        # Positional call shape: solve_ripple_optimal_trim(freqs, w_tf,
        # t_tf, fc_hz, *, ..., seed_trim_db=..., trim_w_db=..., sign=...).
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    # Sanity: this fixture really fitted (not the wild-trim fallback) --
    # otherwise this test would trivially pass by exercising the untouched
    # raw path.
    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.role_attenuations_db != raw_trim
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}

    resolved_w = c.candidate.role_attenuations_db["woofer"]
    resolved_t = c.candidate.role_attenuations_db["tweeter"]
    expected_complex = predicted_branch_sum(
        captured["w_tf"], captured["t_tf"], resolved_w, resolved_t, 1,
    )
    expected_db = 20.0 * np.log10(np.maximum(np.abs(expected_complex), 1e-12))

    freqs_used, db_used = c.measure_predicted_sum
    np.testing.assert_allclose(freqs_used, captured["freqs"])
    np.testing.assert_allclose(db_used, expected_db)

    # And this must actually differ from the fixture's own raw (all-zero)
    # analysis.predicted_sum -- proves the override changed the persisted
    # value, not merely happened to already agree with it.
    assert not np.allclose(db_used, 0.0)


def test_measure_predicted_sum_carries_the_committed_delay(monkeypatch):
    """**The R10b change, linearized lane.** The persisted VERIFY prediction is
    the linearized branch pair at the committed trim AND the committed delay,
    so it models what the emitted graph will actually do.

    The default fixture alignment carries no anchor, so its residual is 0.0 and
    every sibling test above is byte-identical to the pre-R10b behaviour. This
    one supplies the anchor an aligner reports and pins that the delay term is
    live: the persisted curve equals the residual-carrying model and differs
    from the five-argument one the siblings reconstruct.

    The fixture's RAW ``predicted_sum`` is rebuilt with the same residual,
    because in production ``program_analysis._build_candidate`` puts it there —
    keeping the raw and linearized models one model apart (the correction
    filters) is what the improvement gate and ``_commanded_delta`` depend on.
    """

    # A 20 us residual: comfortably inside the +/-(period/6) snap radius
    # (83.3 us at a 2 kHz Fc) and several times the ~5.5 us snap deltas the
    # synthetic MEASURE fixtures actually produce, so it is a realistic
    # selection that still moves the curve visibly.
    anchor_delay_us = 130.0
    delay_us = 150.0
    expected_residual_us = 20.0
    assert summed_model_residual_delay_us(
        anchor_delay_us, delay_us,
    ) == pytest.approx(expected_residual_us)

    captured: dict = {}
    real_solve = iv.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    def _anchored(program):
        analysis = _eligible_measure_analysis(program)
        raw_freqs, _raw_db = analysis.predicted_sum
        woofer_db, tweeter_db = _fixture_branch_db()
        trim = _solve_fixture_raw_trim(woofer_db, tweeter_db)
        raw_complex = predicted_branch_sum(
            (10.0 ** (np.asarray(woofer_db) / 20.0)).astype(complex),
            (10.0 ** (np.asarray(tweeter_db) / 20.0)).astype(complex),
            float(trim["woofer"]), float(trim["tweeter"]), 1,
            freqs_hz=raw_freqs, residual_delay_us=expected_residual_us,
        )
        return replace(
            analysis,
            alignment=_alignment(
                delay_us=delay_us, anchor_delay_us=anchor_delay_us,
            ),
            predicted_sum=(
                raw_freqs,
                20.0 * np.log10(np.maximum(np.abs(raw_complex), 1e-12)),
            ),
        )

    fakes = FakeSeams()
    fakes.measure = _anchored
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}

    resolved_w = c.candidate.role_attenuations_db["woofer"]
    resolved_t = c.candidate.role_attenuations_db["tweeter"]
    expected_db = 20.0 * np.log10(np.maximum(np.abs(predicted_branch_sum(
        captured["w_tf"], captured["t_tf"], resolved_w, resolved_t, 1,
        freqs_hz=captured["freqs"], residual_delay_us=expected_residual_us,
    )), 1e-12))
    freqs_used, db_used = c.measure_predicted_sum
    np.testing.assert_allclose(freqs_used, captured["freqs"])
    np.testing.assert_allclose(db_used, expected_db)

    # The delay term is not a no-op: the five-argument (pre-R10b) model of the
    # SAME linearized branches at the SAME trim is a different curve.
    zero_residual_db = 20.0 * np.log10(np.maximum(np.abs(predicted_branch_sum(
        captured["w_tf"], captured["t_tf"], resolved_w, resolved_t, 1,
    )), 1e-12))
    assert not np.allclose(db_used, zero_residual_db, atol=1e-6)


def test_measure_predicted_sum_uses_linearized_branches_when_trim_rejected(monkeypatch):
    """The wild-trim sanity guard only ever changes the TRIM applied -- the
    correction filters are emitted either way
    (test_wild_seed_drift_falls_back_to_seed_pair_with_warning already pins
    this). The persisted VERIFY prediction must therefore still be built from
    the LINEARIZED branches on this fallback sub-case too, just at the band-
    average SEED trim that actually ended up in role_attenuations_db (#1668
    re-anchor) -- never the un-linearized branches, and never the REJECTED
    (wild resolved) trim. Force the rejection by monkeypatching the ripple-
    optimal solve to return a far-from-seed value while still capturing the
    linearized branches it received."""

    captured: dict = {}

    def _spy(*args, **kwargs):
        freqs, w_tf, t_tf, fc_hz = args
        captured.update(freqs=freqs, w_tf=w_tf, t_tf=t_tf, fc_hz=fc_hz, **kwargs)
        # Force the resolved tweeter trim far from its band-average seed.
        return kwargs["seed_trim_db"] - 20.0, 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    # Sanity: this really is the trim_rejected sub-case (fell back to the SEED
    # pair, not the wild resolved value).
    committed = c.candidate.role_attenuations_db
    assert committed["woofer"] == pytest.approx(captured["trim_w_db"])
    assert committed["tweeter"] == pytest.approx(captured["seed_trim_db"])
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}

    expected_complex = predicted_branch_sum(
        captured["w_tf"], captured["t_tf"],
        captured["trim_w_db"], captured["seed_trim_db"], 1,
    )
    expected_db = 20.0 * np.log10(np.maximum(np.abs(expected_complex), 1e-12))
    freqs_used, db_used = c.measure_predicted_sum
    np.testing.assert_allclose(freqs_used, captured["freqs"])
    np.testing.assert_allclose(db_used, expected_db)


def test_measure_predicted_sum_unchanged_when_linearization_ineligible():
    """The ineligible/raw path stays byte-identical to before this fix:
    ``c.measure_predicted_sum`` is exactly ``analysis.predicted_sum`` -- the
    fixture's own RAW two-branch sum -- never overridden."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier="consumer")
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization == {}

    freqs_used, db_used = c.measure_predicted_sum
    expected_freqs, expected_db = _fixture_raw_predicted_sum()
    np.testing.assert_array_equal(freqs_used, expected_freqs)
    np.testing.assert_array_equal(db_used, expected_db)


def test_measure_predicted_sum_unchanged_when_fit_engine_raises(monkeypatch):
    """SF2 interaction: when the fit engine raises and the candidate build
    degrades to the raw-trim/empty-linearization fallback, the persisted
    VERIFY prediction must degrade with it -- exactly
    ``analysis.predicted_sum``, never a half-computed linearized value left
    over from a call that never reached its own tail."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)

    def _boom(analysis, cand, cloud=None, **_kw):
        raise ValueError("simulated fit engine bug")

    monkeypatch.setattr(c, "_plan_linearization", _boom)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization == {}

    freqs_used, db_used = c.measure_predicted_sum
    expected_freqs, expected_db = _fixture_raw_predicted_sum()
    np.testing.assert_array_equal(freqs_used, expected_freqs)
    np.testing.assert_array_equal(db_used, expected_db)


def test_verify_rearm_measure_predicted_sum_era_round_trip():
    """Era-tolerance: a verify-only re-arm conductor supplied a persisted
    ``measure_predicted_sum`` from BEFORE this coherence fix (a plain
    raw-branch prediction, no linearization awareness) must carry it
    through completely UNCHANGED. This fix only changes what
    ``_measure_verdict`` COMPUTES on a fresh MEASURE accept -- a re-arm
    conductor never calls ``_measure_verdict``/``_fit_linearization`` at all
    (MEASURE is already accepted, see ``index_phase_map={1: PHASE_VERIFY}``),
    so whatever value the constructor was handed is exactly what VERIFY
    compares against, byte for byte."""
    freqs = np.linspace(100.0, 20000.0, 64)
    old_era_prediction = (freqs, np.full(64, -3.0))
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id="era_rearm_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        gain_plan_db={"woofer": -11.0, "tweeter": -13.0},
        index_phase_map={1: PHASE_VERIFY},
        measure_predicted_sum=old_era_prediction,
        measure_gate_window_ms=8.0,
    )
    got_freqs, got_db = c.measure_predicted_sum
    np.testing.assert_array_equal(got_freqs, freqs)
    np.testing.assert_array_equal(got_db, old_era_prediction[1])

    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"
    # Untouched by the VERIFY walk -- still exactly the supplied era tuple.
    got_freqs2, got_db2 = c.measure_predicted_sum
    np.testing.assert_array_equal(got_freqs2, freqs)
    np.testing.assert_array_equal(got_db2, old_era_prediction[1])


# --------------------------------------------------------------------------- #
# PR-L5 — delta-probe verification and automatic rollback
# --------------------------------------------------------------------------- #


def test_delta_probe_verifies_the_correction_and_accepts_a_matching_one():
    """The happy path: the speaker did what the filters commanded, so the
    probe records a MATCHED map and the session is untouched."""
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program), verify_tracking_curve=_tracking_curve(c, 0.0),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"
    assert c.delta_probe is not None
    assert c.delta_probe.verdict == VERDICT_MATCHED
    assert c.delta_probe.rollback is False
    assert c.delta_probe.to_dict()["rollback"] is False


def test_delta_probe_removes_the_applys_declared_level_move(caplog):
    """#1811 wiring: the conductor threads the apply's own declared offset into
    the probe, and that is what keeps a healthy correction from being rolled
    back for the pre-split headroom its own boost was charged.

    The live shape: the apply charged 22.458 dB, so the post-apply capture
    arrives that far down against a prediction carrying no such term. Blind,
    the probe can only say the level axis is broken. Told what moved, it grades
    the correction — and passes it.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    # The apply's move is the ONLY thing that changed the level here, so the
    # pre-apply capture sat exactly on its prediction (#2533) -- which is
    # ``_probed_conductor``'s stated default since series-2 D1 made the two
    # directional safety findings changes against that capture too.
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(c, -22.458),
    )
    assert c.delta_probe is None
    _run_phase(c, 3, 3)
    # Seam unbound (this FakeSeams leaves it None) ⇒ "nothing known", and the
    # shift stays visible rather than being claimed as accounted for.
    assert c.delta_probe.verdict == VERDICT_LEVEL_MISMATCH
    assert c.delta_probe.expected_offset_db == 0.0
    assert c.delta_probe.residual_offset_db == pytest.approx(-22.458, abs=1e-6)
    assert c.delta_probe.entry_anchor_offset_db == pytest.approx(0.0, abs=1e-6)
    assert c.delta_probe.rollback is False

    fakes2 = FakeSeams()
    c2 = _probed_conductor(fakes2)
    c2._seams = dataclasses.replace(
        c2._seams, applied_offset_db=lambda: -22.458,
    )
    fakes2.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(c2, -22.458),
    )
    verdict = _run_phase(c2, 3, 3)
    assert verdict["accepted"] is True
    assert c2.delta_probe.verdict == VERDICT_MATCHED
    assert c2.delta_probe.expected_offset_db == pytest.approx(-22.458)
    assert c2.delta_probe.residual_offset_db == pytest.approx(0.0, abs=1e-6)
    assert "expected_offset_db=-22.458" in caplog.text


def test_a_level_mismatch_is_persisted_and_logged_at_warning(caplog):
    """#1811 SF1: a non-rollback finding must leave a trace, on both surfaces.

    ``level_mismatch`` is not in ``DELTA_PROBE_ROLLBACK_VERDICTS`` by design,
    so nothing escalates on it and the session passes — and until this landed the ONLY evidence was an INFO journal line
    nobody greps. It now rides WARNING (the level a reader sweeping a
    "successful" session actually sees) and is persisted so ``/state``, the
    doctor, and the done screen's caveat can all read one record.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program), verify_tracking_curve=_tracking_curve(c, -22.458),
    )
    verdict = _run_phase(c, 3, 3)
    # The session still passes — the no-rollback adjudication is unchanged.
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"
    assert c.delta_probe.verdict == VERDICT_LEVEL_MISMATCH

    probe_lines = [
        r for r in caplog.records
        if "event=correction.crossover_v2_delta_probe" in r.getMessage()
        and "verdict=level_mismatch" in r.getMessage()
    ]
    assert probe_lines, "the probe must log its verdict"
    assert all(r.levelno >= logging.WARNING for r in probe_lines)


def test_delta_probe_offset_seam_that_misbehaves_is_nothing_known():
    """A seam that raises, or hands back a non-finite number, must degrade to
    "nothing known" (0.0) — never to a claimed offset the emitter cannot
    actually vouch for, and never to a crash on the VERIFY path."""
    for broken in (
        lambda: (_ for _ in ()).throw(RuntimeError("state unreadable")),
        lambda: float("nan"),
        lambda: "loud",
    ):
        fakes = FakeSeams()
        c = _probed_conductor(fakes)
        c._seams = dataclasses.replace(c._seams, applied_offset_db=broken)
        fakes.verify = lambda program, _c=c: dataclasses.replace(
            _verify_analysis(program), verify_tracking_curve=_tracking_curve(_c, 0.0),
        )
        _run_phase(c, 3, 3)
        assert c.delta_probe.expected_offset_db == 0.0
        assert c.delta_probe.verdict == VERDICT_MATCHED


def test_delta_probe_model_error_rolls_back_automatically_and_refuses(caplog):
    """The load-bearing behaviour: a realized-vs-commanded map that does not
    match is undone BEFORE the household is told, so the copy ("the previous
    sound has been put back") is already true when they read it.

    **Which SENTENCE they read moved, and the move is the routing working.**
    The probe's own seam refused under the probe's class and consulted nothing
    else. The round consults every axis, and this fixture's ±5 dB tilt trips
    the SAFETY axis too — a commanded boost realized above its declared bound —
    which the table checks before quality. So the graph comes off under the
    stronger true sentence rather than the shape one. The unsafe-result code is
    not a demotion of the finding: the probe's own verdict is still
    ``model_error`` and still on the record, one assertion below.
    """
    # The COORDINATOR's logger, at INFO: a SUCCESSFUL restore is not an error,
    # and the line moved there with the decision.
    caplog.set_level(
        logging.INFO, logger="jasper.active_speaker.crossover_v2.coordinator",
    )
    calls: list[str] = []
    fakes = FakeSeams()
    c = _probed_conductor(fakes, rollback=lambda reason: calls.append(reason) or True)
    # A wide tilt across the commanded band: the shape is wrong, not the scale.
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0)
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_UNSAFE_RESULT
    assert c.delta_probe.verdict == VERDICT_MODEL_ERROR
    # The rollback ran, exactly once, and it ran with the cause the round
    # decided on rather than a second copy of it.
    from jasper.active_speaker.crossover_v2.verification import (
        SAFETY_BOOST_OVER_DECLARED_BOUND,
    )

    assert calls == [SAFETY_BOOST_OVER_DECLARED_BOUND]
    assert "event=correction.crossover_v2_round_restore" in caplog.text
    assert "restored=true" in caplog.text
    # The refusal names itself to the host (the same contract PR-L4 relies on).
    assert c.last_failure_code == REASON_CORRECTION_UNSAFE_RESULT


def test_delta_probe_refuses_honestly_when_no_rollback_seam_is_bound(caplog):
    """The verdict is real whether or not this process can act on it — but the
    COPY has to match what happened to the speaker.

    A conductor with no rollback binding still refuses, and refuses under
    ``correction_rollback_failed``, whose copy says the correction is STILL
    APPLIED and names Undo. The three verdict-specific codes all promise "the
    previous sound has been put back", and a household listening to a
    correction while being told it was reverted is a false statement about
    their speaker (adversarial review S4)."""
    caplog.set_level(logging.ERROR, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    assert c._seams.rollback is None
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0)
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED
    # The finding itself is still recorded and still specific.
    assert c.delta_probe.verdict == VERDICT_MODEL_ERROR
    # LOUD on the journal, from the one owner that now decides it. The table
    # knows before it tries that there is no anchor, so it does not attempt a
    # restore it cannot make — and says so, which is what keeps the STILL
    # APPLIED sentence below true.
    assert "event=correction.crossover_v2_round_recovery_required" in caplog.text
    assert "rollback_anchor_available=false" in caplog.text
    message = REASON_REGISTRY[REASON_CORRECTION_ROLLBACK_FAILED].message
    assert "STILL APPLIED" in message
    assert "put back" not in message.replace("put the previous sound back", "")


def test_delta_probe_survives_a_rollback_seam_that_raises():
    """A rollback that could not run must not swallow the verdict that asked
    for it."""
    fakes = FakeSeams()

    def _boom(_reason):
        raise RuntimeError("camilla is unreachable")

    c = _probed_conductor(fakes, rollback=_boom)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0)
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    # …and it refuses HONESTLY: the restore did not happen, so the copy must
    # not say it did.
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED
    assert c.delta_probe.verdict == VERDICT_MODEL_ERROR


def test_delta_probe_without_a_tracking_curve_is_unavailable_not_a_rollback():
    """No post-apply comparison, no verdict — and an absent measurement is not
    evidence of a bad correction. Rolling back on it would revert every session
    whose household closed the phone before the sweep."""
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = _verify_analysis  # carries no verify_tracking_curve
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.delta_probe is None


def test_delta_probe_grades_the_bands_the_captures_gate_trusts(caplog):
    """**#2521 wiring.** The probe's band is the capture's own gate-derived
    trusted band, threaded from the gating block the analysis carries — not the
    grid edges, and not a floor this flow derives a second time.

    Driven by a fixture whose gate trusts only part of its grid, with a large
    error placed OUTSIDE that part. A probe reading the grid edges rolls this
    back; a probe reading the gate's band passes it and says which band it
    graded.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    trusted_hi_hz = 8_000.0
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program, trusted_band_hz=(300.0, trusted_hi_hz)),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > trusted_hi_hz, 20.0, 0.0),
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.delta_probe.rollback is False
    assert c.delta_probe.requested_band_hz == (300.0, trusted_hi_hz)
    assert c.delta_probe.probe_band_hz[1] <= trusted_hi_hz
    # The band is on the journal line too, beside the band it actually graded —
    # a disputed verdict should be self-describing (#2521).
    assert f'trusted_band_hz="(300.0, {trusted_hi_hz})"' in caplog.text


def test_a_capture_with_no_trusted_band_leaves_the_probe_unavailable(caplog):
    """An ungateable capture has no band this probe can be honest over, and
    there is deliberately no fallback (#2521).

    Falling back to the raw grid edges would apply the widest possible band to
    the LEAST trustworthy capture — the exact inversion the trusted band
    exists to prevent. ``unavailable`` is not a pass: it refuses nothing and
    permits nothing, which is what every other honesty instrument in this flow
    does with an unknown.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program, trusted_band_hz=None),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0),
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.delta_probe is None
    assert "event=correction.crossover_v2_delta_probe_no_trusted_band" in caplog.text


def test_a_frame_carrying_capture_is_disclosed_rather_than_rolled_back(caplog):
    """**#2521's policy half, wired end to end.**

    A broadband tilt between the in-room capture and the on-axis prediction is
    the ordinary state of this comparison, and before this it rolled healthy
    corrections back. The session now passes, the household is told, and the
    journal carries the tilt that was removed and the grade that survived it.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: -0.9 * np.log2(f / 1_000.0),
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"
    assert c.delta_probe.verdict == VERDICT_FRAME_MISMATCH
    assert c.delta_probe.rollback is False
    assert "frame_removed=true" in caplog.text
    assert "frame_tilt_db_per_octave=-0.9" in caplog.text
    # A non-rollback finding on an otherwise-passing session rides WARNING, or
    # nobody sweeping the journal ever sees it (the #1811 argument, one verdict
    # over).
    probe_lines = [
        r for r in caplog.records
        if "event=correction.crossover_v2_delta_probe " in r.getMessage()
        and "verdict=frame_mismatch" in r.getMessage()
    ]
    assert probe_lines, "the probe must log its verdict"
    assert all(r.levelno >= logging.WARNING for r in probe_lines)


def test_delta_probe_runs_only_after_tracking_has_passed():
    """A session that already failed at the handoff band does not need a
    second verdict about the same capture, and its retry budget still means
    something."""
    fakes = FakeSeams()
    c = _probed_conductor(fakes)
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program, max_db=2.4),
        verify_tracking_curve=_tracking_curve(
            c, lambda f: np.where(f > 4000.0, 5.0, -5.0)
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "verify_out_of_tolerance"
    assert c.delta_probe is None


def test_boost_is_granted_only_to_a_journey_that_will_verify():
    """Boost permission is EVIDENCE-gated on the post-apply sweep.

    **Re-derived for the two-stage split (work order D2).** The gate used to
    read ``PHASE_VERIFY in self.session_phases``, which was exact while one
    session carried both the fit and the post-apply sweep. Stage 1 has no
    VERIFY entry at all — the sweep is stage 2's session — so that reading
    would silently demote every two-stage correction to cut-only. The measuring
    host now DECLARES the answer from the plan shape it resolved, and the gate
    reads the declaration. It is still a condition rather than a constant: a
    session told the journey will not verify is refused the vocabulary.
    """
    fakes = FakeSeams()
    seen: list[bool] = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)
    assert seen and all(seen)
    # …on a session that does NOT itself run VERIFY — the point of the change.
    assert PHASE_VERIFY not in c.session_phases

    # A session told its journey will not verify is refused the vocabulary…
    seen.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c2 = _cloud_conductor(fakes, post_apply_verifies=False)
        _walk_measure_cloud_to_close(c2)
    assert seen and not any(seen)

    # …and so is one that declares nothing and runs no VERIFY of its own, so
    # the undeclared default stays the conservative phase-derived reading.
    seen.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c3 = _conductor(fakes, index_phase_map={1: PHASE_CHECK, 2: PHASE_MEASURE})
        _run_phase(c3, 1, 1)
        _run_phase(c3, 2, 2)
    assert seen and not any(seen)


def test_boost_is_refused_when_the_cloud_verdict_never_reached_the_envelope():
    """**The null-exclusion gate** (adversarial review B2), and the ONE case it
    still decides after the owner's boost ruling (#2106, 2026-08-05).

    ``_cloud_fit_evidence`` has two reachable ``None`` paths (the positions
    could not be combined; the honesty pipeline was unavailable). On both,
    ``compose_envelope`` gets ``excluded_bands_hz=None``, so
    ``allowed_depth_db`` is NOT zeroed in the registry's interference nulls —
    and a boost designed into a null reads MATCHED at the mark while the
    spatial arm, the one instrument that could contradict it, is absent on
    exactly those paths. So boost is withheld; cut-only proceeds.

    **What the ruling changed, and why this test survived it.** The gate used
    to read ``cloud is not None`` for EVERY session, which also caught R15's
    driver-only path — where the cloud is absent BY DESIGN and there is
    nothing to lose. The two states share the ``cloud is None`` signature and
    are different evidence, so the gate now asks the session's own plan which
    one it is. This fixture is the *planned-and-lost* one, and the precondition
    is asserted rather than inherited from the helper: a session that went
    looking for spatial evidence and came back without it does not get to
    boost. Its sibling
    ``test_boost_is_granted_on_the_driver_only_path_that_plans_no_cloud`` is
    the other side."""
    fakes = FakeSeams()
    seen: list[bool] = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _boost_vocabulary_spy(seen))
        c = _cloud_conductor(fakes)
        # THE precondition this test now turns on: the session PLANNED a cloud.
        # Without it the fixture would be indistinguishable from R15's
        # driver-only path, which the same gate deliberately allows.
        assert PHASE_CLOUD_MEASURE in c.session_phases
        mp.setattr(c, "_cloud_fit_evidence", lambda combined: None)
        _walk_measure_cloud_to_close(c)
    assert seen and not any(seen)
    # The correction still happened — only the LIFT vocabulary was withheld.
    assert c.candidate is not None
    assert all(
        f["gain"] <= 0.0
        for fit in c.candidate.linearization.values()
        for f in fit["filters"]
    )
    # …and the absence is already disclosed, not silent.
    assert c.candidate.exclusion_evidence == {}


def test_boost_is_granted_on_the_driver_only_path_that_plans_no_cloud():
    """**The owner's boost ruling** (#2106, 2026-08-05), on the path it is
    about — recorded in the "Boost ruling" block of
    ``docs/historical/linearization-campaign-2026-07.md`` §4.2.

    R15 took the pre-apply cloud out of stage 1 (``STAGE1_INCLUDES_CLOUD_
    MEASURE``), so a driver-only session has no cloud verdict to wait for. The
    retired ``cloud is not None`` demand would have demoted every R15
    correction to cut-only for want of evidence the plan never collects — a
    speaker with a fillable dip would have been handed a fit that cannot fill
    it, forever, with nothing in the journey that could ever change the answer.

    The ruling permits boost here on a NAMED accepted risk: a boost can land on
    a position-specific artifact that an at-mark verification cannot detect.
    What adjudicates it instead is post-apply ``VERIFY``, household listening,
    and retained Undo, with the standing rails (envelope depth, the
    realized-cascade stopband guard, the headroom charge) still bounding the
    filter — each pinned by its own test below.

    **Asserted as a filter actually PLACED, not as a permission carried in the
    vocabulary.** The gate grants a vocabulary; the point of the ruling is that
    the lift stage downstream of it runs. A version of this test that asserted
    only ``allow_boost is True`` would stay green if ``_lift_stage`` were
    disconnected tomorrow.
    """
    fakes = FakeSeams()
    seen: list = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _vocabularies_seen(seen))
        c = _conductor(fakes)
        # The scope precondition, asserted rather than inherited: this session's
        # own capture plan contains no pre-apply cloud phase. That — not
        # "``cloud`` came back ``None``" — is what the gate reads, and it is what
        # separates this fixture from its planned-and-lost sibling above.
        assert PHASE_CLOUD_MEASURE not in c.session_phases
        assert c.post_apply_verifies is True
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 2)

    assert seen and all(v.allow_boost for v in seen)
    # The accepted risk, made explicit rather than left as a silently-empty
    # set: there is no spatial evidence on this path, so there are no
    # cloud-derived exclusions to carry. The plan records the risk; this
    # records that the code is honest about where it comes from.
    assert all(v.boost_excluded_bands_hz == () for v in seen)

    boosts = _emitted_boosts(c.candidate)
    assert boosts, "the ruling is about a boost the fit actually places"


def test_a_cut_only_journey_on_the_same_fixture_places_no_boost():
    """The other half of the pair above, on the IDENTICAL fixture, so the
    difference is the gate and nothing else.

    ``post_apply_verifies=False`` is the surviving necessary condition (nothing
    will measure what the speaker did), so the same session that boosts above
    is cut-only here — and the fit's own post-hoc invariant
    (``fit_driver_linearization``'s "emitted a boost under a cut-only
    vocabulary" ``RuntimeError``) is what makes that structural rather than
    incidental.
    """
    fakes = FakeSeams()
    seen: list = []
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _vocabularies_seen(seen))
        c = _conductor(fakes, post_apply_verifies=False)
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 2)

    assert seen and not any(v.allow_boost for v in seen)
    assert _emitted_boosts(c.candidate) == []


def test_the_ruling_lets_a_candidate_pass_that_cut_only_graded_as_no_improvement():
    """**An intended behaviour change, pinned as intent rather than discovered
    as a regression** (#2106, conductor ruling).

    Boost expands the ACHIEVABLE set, so a candidate the improvement gate
    previously refused can now clear it on the same evidence. That is
    legitimate at the mark — the gate asks whether the proposed correction is
    materially better than doing nothing, and a fit that may fill a dip has a
    strictly larger set of answers than one that may only cut — and it is
    adjudicated downstream by post-apply ``VERIFY``, household listening, and
    retained Undo rather than by withholding the vocabulary.

    **The gate stopped refusing entirely** with the nanny burn-down (doctrine
    deviation (c)), so what the cut-only arm demonstrates now is the LEDGER
    verdict rather than a raised refusal. #2106's ruling is unaffected: it was
    about which candidates the vocabulary can reach, and that is still what
    separates the two arms.

    ``_healthy_crossed_over_pair`` is the case, and it is a real one rather
    than a contrivance: its only defects are two in-band DIPS. A cut-only fit
    cannot fill a dip at any depth (#1809's own doctrine), so it has nothing
    material to offer.

    Both arms here run the same session; only the gate differs. This is also
    the mutation evidence for the gate itself: restoring the cut-only
    vocabulary restores the no-improvement verdict.
    """
    woofer_db, tweeter_db, trim_db = _healthy_crossed_over_pair()

    def session(**kwargs):
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(
            program, woofer_db=woofer_db, tweeter_db=tweeter_db, trim_db=trim_db,
        )
        c = _conductor(fakes, **kwargs)
        _run_phase(c, 1, 1)
        return c

    # --- cut-only: nothing material to offer, and the ledger says so ---
    cut_only = session(post_apply_verifies=False)
    _run_phase(cut_only, 2, 2)
    assert (
        cut_only.measure_predicted_spec_report["comparison"]["reason"]
        == accountability.LEDGER_NOT_AN_IMPROVEMENT
    )

    # --- the shipped driver-only gate: the same session completes ---
    boosted = session()
    verdict = _run_phase(boosted, 2, 2)
    assert verdict["accepted"] is True
    assert boosted.candidate is not None
    # …and it is the BOOST that made the difference, not some unrelated drift:
    # what the cut-only arm could not do is fill the dips, and this arm does.
    assert _emitted_boosts(boosted.candidate)


def test_the_envelope_still_bounds_a_boost_on_the_driver_only_path():
    """**Rail 1 of the ruling's three**, on the path the ruling opened.

    ``allowed_depth_db`` is direction-agnostic — the same per-bin array bounds
    a cut and a boost — and it is composed from mic trust, repeatability,
    linearity, invertibility and the class prior, none of which the cloud
    supplied. So it binds identically with no cloud present, and this asserts
    that by CLAMPING it: capped at 1.0 dB, the fit may no longer place the
    boost it places unclamped.

    Written as a clamp rather than as an observation of the shipped number
    because an observation would stay green if the envelope were disconnected
    from the lift stage — the uncapped arm below is what makes the capped one
    mean something.

    **The cap is 2.0 dB, and the value is load-bearing** (gate finding on
    #2138). At 1.0 dB the capped arm places no positive gain at all — the lift
    is suppressed outright — so a "every gain <= cap" assertion would inspect
    only cuts and pass while saying nothing. At 2.0 dB the boost SURVIVES and
    is CLAMPED (exactly 2.000 dB against 3.715 unclamped), which is the state
    that discriminates. So both halves are asserted: a positive gain is placed,
    and no gain exceeds the cap. Removing either half makes the test vacuous
    again.

    **Which of the envelope's two bounds this pins, measured rather than
    assumed.** The stage bounds a lift twice: a REQUEST bound (``wanted =
    min(deficit, allowed_depth)``) and a REALIZATION gate on the emitted
    cascade (``exceeds_envelope``, for a greedy bell fit that overshoots
    BETWEEN its centres). This test kills the request bound — deleting it makes
    the fit ask for the full 3.715 dB, the realization gate then suppresses the
    lift wholesale, and the "a boost survived" assertion above fires.

    It does NOT cover the realization gate, and the docstring says so rather
    than implying it. Instrumented at the gate's own expression, this fixture's
    clamped lift is a single bell whose realized peak sits at
    ``max(realized - allowance) == -0.000000 dB`` over ``band_mask`` — exactly
    ON the allowance, which is the request bound binding — while the gate only
    fires ``_MIN_FILTER_GAIN_DB`` (0.5 dB) ABOVE that. So the fixture sits a
    full 0.5 dB below the firing threshold, the gate never fires here, and
    disarming it changes nothing this test could see. Reaching it needs a
    multi-dip response where bells overshoot BETWEEN centres; that is the
    ``unlock`` case in ``_NON_MONOTONE_SHAPES`` in
    ``tests/test_active_speaker_linearization_fit.py``, where the gate's
    discriminating assertion now lives.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)

    cap_db = 2.0
    real_compose = iv.compose_envelope
    real_fit = iv.fit_driver_linearization

    def _capped(*args, **kwargs):
        env = real_compose(*args, **kwargs)
        return replace(
            env,
            allowed_depth_db=np.minimum(env.allowed_depth_db, cap_db),
        )

    fitted: list = []

    def _record(resp, envelope, **kwargs):
        fit = real_fit(resp, envelope, **kwargs)
        fitted.append(fit)
        return fit

    # Unclamped first, so the assertion below is not vacuous: this fixture
    # really does want more boost than the cap allows.
    free = _conductor(fakes)
    _run_phase(free, 1, 1)
    _run_phase(free, 2, 2)
    free_boosts = [f["gain"] for f in _emitted_boosts(free.candidate)]
    assert free_boosts and max(free_boosts) > cap_db

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "compose_envelope", _capped)
        mp.setattr(iv, "fit_driver_linearization", _record)
        capped = _conductor(fakes)
        _run_phase(capped, 1, 1)
        _run_phase(capped, 2, 2)

    assert fitted, "the fit must have run for this to assert anything"
    capped_boosts = [
        f.gain for fit in fitted for f in fit.filters if f.gain > 0.0
    ]
    # A boost SURVIVED the clamp — without this the loop below inspects cuts.
    assert capped_boosts, "the clamped fit must still place a boost"
    # …and the envelope BOUND it.
    for fit in fitted:
        for f in fit.filters:
            assert f.gain <= cap_db + 1e-9, f


def test_the_headroom_charge_is_paid_for_a_driver_only_boost():
    """**Rail 3 of the three.** A boost is not free: the branch CHAIN's
    realized peak is charged as headroom at emission
    (``camilla_yaml.linearization_headroom_db`` via
    ``branch_chain.branch_headroom_db``), and the runtime contract re-derives
    the same peak from the emitted graph text and refuses to prove a graph that
    did not pay it (``runtime_contract._consume_linearization_chain``).

    Deliberately asserted here only as far as the CONDUCTOR's own disclosure —
    the charge exists and is the committed chain's own peak. Everything below
    that seam reads the emitted graph and is blind to which gate granted the
    boost, so it needs no driver-only variant; its pins live in
    ``tests/test_active_speaker_linearization_emission.py``
    (``test_linearization_boost_is_accepted_and_absorbed_by_baseline_
    headroom``, ``test_reproof_blocks_boost_beyond_the_absorbed_headroom``).
    """
    from jasper.active_speaker.branch_chain import branch_headroom_db

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)

    assert PHASE_CLOUD_MEASURE not in c.session_phases
    boosted_roles = {
        role
        for role, fit in c.candidate.linearization.items()
        if any(f["gain"] > 0.0 for f in fit["filters"])
    }
    assert boosted_roles, "the fixture must boost for the charge to mean anything"
    for role, fit in c.candidate.linearization.items():
        assert fit["headroom_cost_db"] == pytest.approx(
            branch_headroom_db(
                fit["filters"],
                sections=_configured_sections(c, role),
                trim_db=c.candidate.role_attenuations_db[role],
            )
        )
    # …and the charge is REAL on the boosted branch, not a zero that happens to
    # match a zero (``linearization_headroom_db`` short-circuits to 0.0 when no
    # emitted filter has positive gain, so an all-cut role legitimately reads
    # 0.0 and would satisfy the equality above by itself).
    for role in boosted_roles:
        assert c.candidate.linearization[role]["headroom_cost_db"] > 0.0


def test_every_non_matched_verdict_reaches_a_household_surface():
    """A new NON-MATCHED verdict cannot ship without reaching the household.

    This guard used to assert equality with the ROLLBACK set, which enforced
    the stated intent only for as long as the two sets were the same thing.
    ``level_mismatch`` (#1811) is the first non-matched verdict that is
    deliberately not a rollback, so it slipped through an equality check while
    rendering as a clean pass. The guard now walks the non-matched set: a
    verdict either has a refusal code with real copy, or is named here with
    the surface it does reach instead.
    """
    non_matched = set(DELTA_PROBE_VERDICTS) - {VERDICT_MATCHED, VERDICT_UNAVAILABLE}
    # Verdicts that reach the household WITHOUT a refusal. Adding one here is
    # a claim that must be true — each entry names the surface, and that
    # surface has its own test.
    surfaced_without_refusal = {
        # Persisted as ``verify.delta_probe`` by ``persist_conductor_state``
        # and rendered as the done screen's caveat nudge — see
        # ``test_a_level_mismatch_caveats_the_pass_screen`` in
        # tests/test_crossover_envelope_v2.py.
        VERDICT_LEVEL_MISMATCH,
        # The tilt-carrying sibling of the one above (#2521), on the same
        # surface and by the same route — see
        # ``test_a_frame_mismatch_caveats_the_pass_screen`` in
        # tests/test_crossover_envelope_v2.py.
        VERDICT_FRAME_MISMATCH,
        # The shape check did not RUN (#2614) — an alternative-Fc round has no
        # like-for-like previous graph, so there is no change axis to grade
        # against. Not a finding about the speaker, so not a refusal; it
        # reaches the household on the same done-screen caveat by the same
        # route — see ``test_a_safety_only_probe_caveats_the_pass_screen`` in
        # tests/test_crossover_envelope_v2.py.
        VERDICT_SAFETY_ONLY,
    }
    assert set(DELTA_PROBE_REASON_BY_VERDICT) == non_matched - surfaced_without_refusal
    assert set(DELTA_PROBE_REASON_BY_VERDICT) == set(DELTA_PROBE_ROLLBACK_VERDICTS)
    for code in DELTA_PROBE_REASON_BY_VERDICT.values():
        spec = REASON_REGISTRY[code]
        assert spec.template == "hard_stop"
        assert spec.retry_budget == 0
        assert len(spec.message) > 40
        # The correction is already undone, so the copy has to say so.
        assert "put back" in spec.message


def test_delta_probe_reason_copy_names_no_hardware_noun():
    """Mirrors the null-classification copy rule: the household is told what
    happened and what to do, never given a hardware diagnosis this measurement
    cannot support."""
    # "driver details in speaker setup" is a UI location and appears in
    # PR-L4's own copy — what is banned is naming a PART as the cause, which
    # is a diagnosis this measurement cannot support.
    banned = ("tweeter", "woofer", "amplifier", "horn", "capacitor", "resistor")
    for code in DELTA_PROBE_REASON_BY_VERDICT.values():
        message = REASON_REGISTRY[code].message.lower()
        assert not any(word in message for word in banned), code


def test_the_commanded_delta_is_none_when_a_side_is_missing():
    """A missing curve on EITHER side is ``None``, which the probe reads as
    ``unavailable`` — not as a zero curve that would classify as 'matched'.

    **Amended by #2611, and the amendment is the point.** This test also pinned
    ``_commanded_delta(predicted, predicted) is None`` — the trims-only guard,
    which was correct while the previous side was the raw crossover at the
    applied candidate's own parameters: a candidate emitting no filters produced
    the identical object on both sides and had, in that frame, commanded
    nothing. In the applied-vs-PREVIOUS-graph frame a trims-only candidate
    commands its whole trim, polarity and delay step, so that guard would now
    delete a real commanded change. Two equal-VALUED curves still yield a
    flat-zero delta, which ``classify_delta_probe``'s own commanded floor
    refuses as ``nothing_commanded`` — one owner for "was anything asked for",
    one layer down.
    """
    predicted = (np.array([100.0, 200.0]), np.array([0.0, 0.0]))
    assert flow._commanded_delta(None, predicted) is None
    assert flow._commanded_delta(predicted, None) is None
    _freqs, delta = flow._commanded_delta(predicted, predicted)
    assert list(delta) == [0.0, 0.0]


def test_the_commanded_delta_is_the_applied_minus_the_previous_graph():
    previous = (np.array([100.0, 1000.0]), np.array([0.0, 0.0]))
    applied = (np.array([100.0, 1000.0]), np.array([-1.0, 4.0]))
    freqs, delta = flow._commanded_delta(previous, applied)
    assert list(freqs) == [100.0, 1000.0]
    assert list(delta) == [-1.0, 4.0]


# --------------------------------------------------------------------------- #
# adversarial-review regressions (round 2)
# --------------------------------------------------------------------------- #


def test_the_realized_level_assertion_still_fires_on_its_own_evidence(caplog):
    """**S6(a).** Item 1 (the realized-level check) is the only level check
    left since the single-datum-owner migration (#2609) deleted the two-voter
    frame's own refusal arm, and this pins that it still fires on its own
    evidence rather than having quietly gone dead.

    **What it does when it fires changed; THAT it fires did not** (doctrine
    deviation (i)). It banks a finding and the round proceeds. This test is
    deliberately kept — inverted rather than deleted — because "the demotion"
    and "the check rotted away" are the two outcomes a reader has to be able to
    tell apart, and only an assertion that the numbers still reach the journal
    can do that.

    **Item 1's route in this harness is now the ONLY route.** The
    level-consistency check (#2609's ``compare_level_definitions``) compares the
    two per-driver estimators and banks a finding; neither has a refusal arm
    now, so every session reaches the end with whatever the anchor computed and
    a disclosure beside it. (The ripple polish is not a route around it either
    — the linearized scan can still move the committed pair, but only through
    the wild-trim guard, which grades both candidates on this same assertion
    first.)
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    # The realized level verdict is SUPPLIED rather than provoked, for the same
    # reason ``test_wild_trim_fallback_follows_levels_not_drift`` supplies its
    # pair: the physical routes that used to mislevel a committed trim are the
    # ones PR-L3 closed, and re-opening one to test the gate that catches it
    # would be testing the wrong thing. What must be pinned is that item 1
    # still reports on its own evidence, under its own event.
    def _match(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-5.2, difference_db=-5.2,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "realized_level_match", _match)
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 2)

    # Item 1's own disclosure, under its own event.
    assert "event=correction.crossover_v2_level_match_finding" in caplog.text
    assert "event=correction.crossover_v2_level_match_refused" not in caplog.text
    # …with both realized levels on the line, so the verdict is re-derivable.
    for ledger_field in (
        "difference_db=", "level_w_db=", "level_t_db=", "tolerance_db=",
    ):
        assert ledger_field in caplog.text
    # The round proceeded and banked its reservation.
    assert c.candidate is not None
    assert len(fakes.published_candidates) == 1
    assert fakes.banked_findings != []


def test_prediction_gate_logs_the_improved_path_with_both_terms(caplog):
    """**S6(b).** The ledger's ``improved`` path and its ``before_rms_db`` /
    ``improvement_db`` terms are the ones a field diagnosis reads to answer
    "did the correction actually help, and by how much" — and after PR-L5
    moved the default fixture into the ``predicted_in_spec`` early return,
    nothing asserted them any more.

    Driven by a correction that genuinely improves its own model WITHOUT
    reaching spec — the only shape that reaches this branch. A big broad peak
    the fit can take out (3.6 dB pooled residual down to 0.46) riding on a comb
    it cannot (there are far more notches than the filter budget), so the
    prediction moves materially and still fails.

    The comb went from 3 dB to 5 dB with #1809: once the fit stops spending
    gain inside each driver's own crossover stopband the corrected prediction
    is better, and at 3 dB it now clears the spec outright and takes the
    ``predicted_in_spec`` early return instead of reaching this branch.

    **The peak moved onto Fc, and the trim is now solved, with #1929.** This
    fixture was reaching the prediction gate only by cancellation. Its two
    branches carry the IDENTICAL curve, whose two mirrored ±1-octave halves
    about Fc genuinely sit 8.32 dB apart when the peak is an octave below Fc
    (level_w 11.17, level_t 2.85) — but it inherited ``_FIXTURE_RAW_TRIM_DB``,
    solved from the DEFAULT curves, which says 0.70 dB. That is exactly the "a
    fixture field nobody derived from the fixture" defect
    :func:`_solve_fixture_raw_trim`'s own docstring documents, and the shipped
    whole-band core median happened to be wrong by the same amount and sign,
    so the frame gate read 0.073 dB. Solving the trim from THESE branches and
    leaving everything else alone makes the shipped code refuse the fixture at
    **8.947 dB** — worse than #1929's 6.087 — so the cancellation, not the
    band, was carrying it.

    Recentring the peak on Fc is what makes the level well defined: a 12 dB
    peak an octave below Fc lives inside the woofer's radiating band and
    outside the tweeter's, so "where do these two drivers sit" has an 8 dB
    band-dependent answer and no level instrument can reconcile it. On Fc both
    estimators see it.

    **The two branches now carry their own halves of the crossover (#2523),
    and that is a fixture DEFECT repaired rather than a threshold re-tuned.**
    Until now both roles were handed the identical UNSHAPED curve — a tweeter
    measuring full output at 200 Hz, three octaves below its own high-pass,
    which no speaker can do. It survived because the defect was symmetric: both
    roles were fitted over the same too-wide band, drew near-identical
    corrections, and so realized matching levels. #2523 fits each role over its
    own band, the symmetry breaks, and the accountability gate correctly
    refused a pair whose tweeter correction was 8 filters of bass cut on a
    driver the crossover already silences. So each branch is built the way
    ``_healthy_crossed_over_pair`` builds its own — the shared shape THROUGH
    that role's half of the matched LR4 — and the shape is retuned to an 8 dB
    peak on a 5 dB, 5-cycle-per-octave comb, which reaches the same branch on
    the same terms: ``reason=improved``, ``after_passed=false``. Measured on
    both sides of #2523 so the fixture is not tuned to the change — 2.233 dB
    pooled residual falling to 1.094 (before) / 1.176 (after), against a
    0.5 dB floor.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    freqs = _LINEARIZABLE_FREQS_HZ
    peak_db = 8.0 * np.exp(-0.5 * ((np.log2(freqs / _FIXTURE_FC_HZ) / 0.4) ** 2))
    comb_db = 5.0 * np.sin(2.0 * np.pi * np.log2(freqs / 200.0) * 5.0)
    shape_db = peak_db + comb_db
    lowpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=False),)
    highpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=True),)
    woofer_db = crossover_response_db(freqs, lowpass) + shape_db
    tweeter_db = crossover_response_db(freqs, highpass) + shape_db
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs,
        (10.0 ** (woofer_db / 20.0)).astype(complex),
        (10.0 ** (tweeter_db / 20.0)).astype(complex),
        _FIXTURE_FC_HZ,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=woofer_db, tweeter_db=tweeter_db,
        trim_db={
            "woofer": round(float(trim_w), 3), "tweeter": round(float(trim_t), 3),
        },
    )
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    assert "event=correction.crossover_v2_prediction_gate" in caplog.text
    assert "reason=improved" in caplog.text
    assert "after_passed=false" in caplog.text
    for ledger_field in (
        "before_rms_db=", "after_rms_db=", "improvement_db=", "required_db=",
    ):
        assert ledger_field in caplog.text


def test_the_candidate_payload_discloses_the_headroom_cost_to_the_household():
    """**S3.** The owner's ruling is that headroom spend is DISCLOSED, not
    limited — and a number that only ever reaches the journal is not disclosed
    to the household that owns the speaker. It rides the same payload the host
    persists and the envelope renders."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    payload = _walk_measure_cloud_to_close(c)

    assert "headroom_cost_db" in payload
    charged = max(
        fit["headroom_cost_db"] for fit in c.candidate.linearization.values()
    )
    assert payload["headroom_cost_db"] == pytest.approx(charged)
    # This fixture's correction is granted boost, so the disclosure is a real
    # number rather than a structurally-zero field.
    assert payload["headroom_cost_db"] > 0.0


def test_a_cut_only_candidate_discloses_a_zero_headroom_cost():
    """The other half: a correction that spends nothing says so, rather than
    omitting the field and leaving the surface to guess."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(c, "_cloud_fit_evidence", lambda combined: None)  # no boost
        payload = _walk_measure_cloud_to_close(c)
    assert payload["headroom_cost_db"] == 0.0


def test_the_browser_candidate_summary_discloses_the_headroom_cost():
    """**SF3.** The owner's ruling is that headroom spend is DISCLOSED, not
    limited — and the conductor's confirm payload is read by the host for
    ``auto_apply`` alone, so a number that stopped there reached the journal
    and nothing else. This is the payload the envelope's own screens read.
    """
    from jasper.web.correction_crossover_v2 import _candidate_summary

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    summary = _candidate_summary(c.candidate)
    assert "headroom_cost_db" in summary
    charged = max(
        fit["headroom_cost_db"] for fit in c.candidate.linearization.values()
    )
    assert summary["headroom_cost_db"] == pytest.approx(charged)
    # This fixture's correction is granted boost, so the disclosure is a real
    # number rather than a structurally-zero field.
    assert summary["headroom_cost_db"] > 0.0


def test_the_browser_summary_discloses_zero_for_a_cut_only_correction():
    """PRESENT and zero, never absent — a surface must not have to guess
    whether the field is missing or the cost is nothing."""
    from jasper.web.correction_crossover_v2 import _candidate_summary

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(c, "_cloud_fit_evidence", lambda combined: None)  # no boost
        _walk_measure_cloud_to_close(c)

    summary = _candidate_summary(c.candidate)
    assert summary["headroom_cost_db"] == 0.0


def test_both_headroom_disclosures_come_from_one_reducer():
    """The conductor's confirm payload and the browser summary answer to
    different readers, so both exist — but two reducers for one
    household-facing number is the drift this ladder removes."""
    from jasper.web.correction_crossover_v2 import _candidate_summary

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    payload = _walk_measure_cloud_to_close(c)

    assert payload["headroom_cost_db"] == pytest.approx(
        _candidate_summary(c.candidate)["headroom_cost_db"]
    )


# --------------------------------------------------------------------------- #
# the fit band and the headroom charge, end to end (#1809, #1808)
# --------------------------------------------------------------------------- #


def test_the_conductor_and_the_emitter_derive_one_set_of_crossover_sections():
    """**One derivation.** The conductor stamps the disclosed
    ``headroom_cost_db`` from these sections and the emitter charges
    ``active_baseline_headroom`` from its own; if the two ever disagreed, the
    number a household is told and the level the speaker gives up would part
    company. They were separate derivations for one review cycle and had
    already drifted on the no-region case — the conductor invented a section
    at the session Fc where the emitter credited none, which makes the
    disclosure SMALLER than the charge: the one direction the ledger promises
    is impossible."""
    from jasper.active_speaker.camilla_yaml import _branch_context

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    emitter = _branch_context(c.candidate.source_preset, {})
    for role in c.candidate.linearization:
        assert _configured_sections(c, role) == emitter[role][0], role


def test_a_role_with_no_crossover_region_is_credited_nothing():
    """…and the no-region case resolves the same way on both sides, because
    both sides ask the same function: no section, so the branch is treated as
    running full range — which is exactly what the emitter would build for it.

    **The "and named" half of this test moved with the fit** (#2291 Phase 2b).
    The ``correction.crossover_v2_linearization_no_crossover`` WARNING is
    emitted by the planner, at the corner of the candidate being planned rather
    than the session's, and is pinned there by
    ``test_crossover_v2_intervention_dual_run.py::
    test_a_role_with_no_crossover_section_is_named_in_the_journal`` (plus its
    positive control). What is left here is the half this module still owns:
    the derivation credits nothing and invents nothing.
    """
    from jasper.active_speaker.branch_chain import sections_by_role

    fakes = FakeSeams()
    c = _conductor(fakes)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(c, "_preset", types.SimpleNamespace(crossover_regions=()))
        assert _configured_sections(c, "woofer") == ()
    # The shared derivation is where that answer comes from — not a branch in
    # the conductor that the emitter would have to mirror.
    assert sections_by_role(()) == {}


def test_an_ordinary_session_banks_no_estimator_finding():
    """The ordinary session mints no level-estimator finding and calls no
    banking seam.

    Pinned because "banks a finding" is a side effect
    (:func:`~jasper.active_speaker.crossover_v2.accountability.
    level_frame_record`) and the cheapest way for it to go wrong is
    to fire unconditionally — which would put a diagnosis in front of every
    household regardless of evidence.

    The check compares the two per-driver estimators and runs on every planned
    candidate; here it finds them inside tolerance. That is the assertion worth
    having: not "the check was skipped" but "the check ran and stayed quiet".
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    banked = fakes.banked_findings
    with pytest.MonkeyPatch.context() as mp:
        plans = _plan_spy(mp)
        _run_phase(c, 1, 1)
        assert _run_phase(c, 2, 2)["accepted"] is True
    consistency = plans[-1].level_consistency
    assert consistency is not None, "both estimators cover a role here"
    assert consistency.differs is False
    assert consistency.worst_delta_db < consistency.tolerance_db
    assert banked == []


def test_no_boost_lands_in_a_drivers_own_crossover_stopband():
    """**#1809, end to end.** Whatever the fit decides, no emitted boost may
    sit where this driver's own crossover has handed off. Cuts are unaffected —
    they remove leakage that still reaches the summed response.

    Held on the conductor rather than only on the fit engine because the
    radiating band is the CONDUCTOR's to solve (it owns the preset's crossover
    regions); a wiring regression here would silently restore the defect with
    the fit engine's own tests still green.

    **Both journey shapes**, since #2106. The guard is rail 2 of the three the
    boost ruling leans on, and the ruling opened a path — a driver-only session
    with no pre-apply cloud — that this test did not previously reach. The
    guard reads the branch's own crossover sections and knows nothing about
    clouds, so it should hold identically; asserting it is what makes that a
    fact rather than an expectation.
    """
    from jasper.active_speaker.branch_chain import radiating_band_hz

    def _cloud_session():
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)
        return c

    def _driver_only_session():
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        c = _conductor(fakes)
        assert PHASE_CLOUD_MEASURE not in c.session_phases
        _run_phase(c, 1, 1)
        _run_phase(c, 2, 2)
        return c

    for label, build in (
        ("cloud", _cloud_session), ("driver-only", _driver_only_session),
    ):
        c = build()
        boosts_seen = False
        for role, fit in c.candidate.linearization.items():
            sections = _configured_sections(c, role)
            lo_hz, hi_hz = radiating_band_hz(sections)
            for f in fit["filters"]:
                if f["gain"] > 0.0:
                    boosts_seen = True
                    assert lo_hz <= f["freq"] <= hi_hz, (label, role, f)
        assert boosts_seen, (
            f"the {label} fixture must emit a boost for this to mean anything"
        )


def test_the_stamped_headroom_cost_is_the_committed_chains_own_peak():
    """One number: what the candidate discloses is what
    ``branch_chain.branch_headroom_db`` returns for the chain the graph will
    actually run — the same filters, the same crossover, and the trim the
    level-match adjudication COMMITTED (not the anchor it might have
    rejected)."""
    from jasper.active_speaker.branch_chain import branch_headroom_db

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    for role, fit in c.candidate.linearization.items():
        assert fit["headroom_cost_db"] == pytest.approx(
            branch_headroom_db(
                fit["filters"],
                sections=_configured_sections(c, role),
                trim_db=c.candidate.role_attenuations_db[role],
            )
        )


def test_the_stamped_disclosure_equals_what_the_emitter_actually_charges():
    """**The edge between the two owners**, and the one a drifted
    role -> sections derivation would break silently.

    The conductor STAMPS each branch's cost onto the candidate; the emitter
    CHARGES ``active_baseline_headroom`` when that candidate is compiled into a
    graph. Nothing else compares them, so this walks the candidate all the way
    to an emitted config and asserts the two numbers are one number — over the
    real preset, the real committed trims, and the real emitted filters.
    """
    from jasper.active_speaker.camilla_yaml import (
        _branch_context, linearization_headroom_db,
    )
    from jasper.active_speaker.linearization_fit import (
        linearization_filters_by_role, worst_headroom_cost_db,
    )

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)
    candidate = c.candidate
    assert worst_headroom_cost_db(candidate.linearization) > 0.0, (
        "the fixture must carry a real charge for this edge to mean anything"
    )

    corrections = {
        role: {"gain_db": float(gain_db)}
        for role, gain_db in candidate.role_attenuations_db.items()
    }
    charged = linearization_headroom_db(
        linearization_filters_by_role(candidate.linearization),
        branch_context=_branch_context(candidate.source_preset, corrections),
    )
    assert charged == pytest.approx(
        worst_headroom_cost_db(candidate.linearization), abs=1e-6
    )


