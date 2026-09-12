# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: VERIFY-prediction coherence, the delta probe, and the fit-band headroom charge."""

from __future__ import annotations

import numpy as np
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session
from tests.crossover_v2_fixtures import (
    CAPS,
    FC_HZ,
    FakeSeams,
    SESSION_VOLUME_DB,
    _preset,
    _roles,
    _run_phase,
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


































# --------------------------------------------------------------------------- #
# adversarial-review regressions (round 2)
# --------------------------------------------------------------------------- #

