# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: diagnosis-honesty — MEASURE's priors carry CHECK's ambient."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE

from tests.crossover_v2_fixtures import (
    FakeSeams,
    _conductor,
    _run_phase,
)


def test_measure_priors_carry_the_ambient_report_check_measured():
    """#1830 — MEASURE grades its per-driver SNR against CHECK's room floor.

    ``driver_response`` computes the SNR verdict only when it is handed an
    ambient report, and ``_measure_priors`` used to build priors without one —
    so ``DriverResponse.snr`` was ``None`` on every v2 session ever run while
    the evidence to compute it sat in the same session's ``check.json``.

    Asserted THROUGH the conductor on purpose. ``test_measure_uses_check_
    ambient_for_snr_verdicts`` in the program-analysis suite already pins the
    analyzer half, but it constructs ``MeasurementPriors(ambient_report=...)``
    by hand — which is exactly why it stayed green for the entire life of the
    bug. The production gap was the conductor never putting the report there.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)   # CHECK
    _run_phase(c, 2, 2)   # MEASURE

    measure_priors = next(
        priors for phase, _prog_phase, _result, priors, _geom in fakes.analyzed
        if phase == PHASE_MEASURE
    )
    assert measure_priors.ambient_report == {"bands": [{"level_dbfs": -70.0}]}, (
        "MEASURE must be handed CHECK's measured ambient, or the per-driver "
        "SNR verdict silently never computes"
    )


def test_measure_priors_carry_no_ambient_when_check_never_ran():
    """#1830, the other half: absence stays honest.

    A conductor rehydrated past CHECK (accepted phases + the persisted gain
    plan, which is what lets it compose a MEASURE program without re-running
    CHECK) has no ambient of its own. The report is deliberately NOT persisted
    alongside the gain plan: a noise floor is a claim about this room at this
    mic position, and the §5.6 binding rule restarts any other session at
    CHECK precisely because that position is unverifiable across sessions. So
    the SNR verdict stays absent rather than being graded against a floor
    measured somewhere else.
    """
    fakes = FakeSeams()
    c = _conductor(fakes, gain_plan_db={"woofer": -11.0, "tweeter": -13.0})
    _run_phase(c, 2, 2)   # MEASURE, with no CHECK consumed by THIS conductor

    measure_priors = next(
        priors for phase, _prog_phase, _result, priors, _geom in fakes.analyzed
        if phase == PHASE_MEASURE
    )
    assert measure_priors.ambient_report is None
