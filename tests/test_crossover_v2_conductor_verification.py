# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: diagnosis-honesty, the boost gate's evidence claim, and post-apply verification."""

from __future__ import annotations

import pytest
from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_MEASURE,
)

from jasper.active_speaker.crossover_v2.contracts import CLAIM_NOT_EVALUATED
from jasper.active_speaker.crossover_v2.verification import (
    verify_absolute_tolerance_db,
)
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _absolute,
    _conductor,
    _run_phase,
    _verify_analysis,
)


#
# Four shipped instruments each stated less than they measured. These pin the
# disclosure, not the physics: the numbers below are fixtures, but the SHAPE of
# what reaches a persisted record or a household screen is the contract.


def test_measure_priors_carry_the_ambient_report_check_measured():
    """#1830 — MEASURE grades its per-driver SNR against CHECK's room floor.

    ``_driver_response`` computes the SNR verdict only when it is handed an
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
    c = _conductor(
        fakes,
        accepted_phases=(PHASE_CHECK,),
        gain_plan_db={"woofer": -11.0, "tweeter": -13.0},
    )
    _run_phase(c, 2, 2)   # MEASURE, with no CHECK consumed by THIS conductor

    measure_priors = next(
        priors for phase, _prog_phase, _result, priors, _geom in fakes.analyzed
        if phase == PHASE_MEASURE
    )
    assert measure_priors.ambient_report is None


# #1967 — the boost gate's evidence claim, made substantive


# R18 — honest post-apply verification (issues #1868 / #1654)
#
# The numbers in these records are SYNTHETIC and labelled so — no hardware
# measurement is restated as a fixture value. The journal-verified fact they DO
# reproduce is the graded band: ``tracking_band_lo_hz=2000.0`` on a box whose
# tweeter is swept from Fc.


@pytest.mark.parametrize(
    ("verify_absolute", "badged"),
    [
        pytest.param(_absolute(1.503), True, id="absolute_graded"),
        pytest.param(
            {"not_evaluated": "no_trusted_crossover_region"}, False,
            id="nothing_graded",
        ),
    ],
)
def test_the_mark_badge_needs_a_claim_that_was_actually_graded(
    verify_absolute,
    badged,
):
    """The corner of the pin above: a capture that graded NOTHING.

    Accepting an ungradeable tracking claim (#3487) is what makes this
    reachable — and when the same capture also finds no trusted crossover
    region, the absolute claim is ``not_evaluated`` too, so the accepted
    VERIFY carries four claims and not one verdict. The badge over it must
    then not be the one that means *verified at the mark*: the republish
    door's own contract, which is where this shape comes from, is that such a
    VERIFY grades INDETERMINATE and never a false pass.

    The first case is the witnessed one and is unchanged — one claim graded,
    none failed, badge at the mark. What separates the two is not the
    ``outcome``, which is a ``pass`` in both: it is whether any claim was
    graded at all.
    """
    from jasper.web.correction_crossover_v2_grade import GRADE_INCONCLUSIVE, GRADE_MARK_VERIFIED, _post_apply_grade

    from jasper.active_speaker.crossover_v2 import verification, capture_dispatch
    c = _conductor(FakeSeams())
    analysis = _verify_analysis(c.program_for_phase("verify"), max_db=None, verify_absolute=verify_absolute)
    verdict = capture_dispatch.assess(analysis, phase="verify", program=c.program_for_phase("verify"))
    claims = verification._verify_claims(analysis.verify_tracking or {}, analysis.verify_absolute)
    assert verdict.ok is True
    assert claims["integration"]["status"] == CLAIM_NOT_EVALUATED
    grade = _post_apply_grade(
        {
            "applied": True,
            "verify": {"outcome": "pass", "claims": claims},
        }
    )

    assert grade["state"] == (GRADE_MARK_VERIFIED if badged else GRADE_INCONCLUSIVE)
    assert grade["graded"] is badged


def test_absolute_tolerance_is_derived_from_the_spec_table_not_chosen():
    """The threshold has no literal of its own: it is the loosest
    ``flat_spec.SPEC_BANDS`` entry the crossover region overlaps, so revising
    that table with hardware data moves this without a second edit."""
    from jasper.active_speaker import flat_spec

    assert verify_absolute_tolerance_db([1000.0, 4000.0]) == max(
        tol for lo, hi, tol in flat_spec.SPEC_BANDS if lo < 4000.0 and 1000.0 < hi
    )
    # It is NOT the model-tracking tolerance wearing a different name.
    assert verify_absolute_tolerance_db([1000.0, 4000.0]) != contracts.VERIFY_TOLERANCE_DB
    # A region the spec table declines to grade yields no bar at all, and the
    # claim is recorded not-evaluated rather than held to an invented one.
    assert verify_absolute_tolerance_db([17_000.0, 20_000.0]) is None
    assert verify_absolute_tolerance_db([1000.0]) is None


