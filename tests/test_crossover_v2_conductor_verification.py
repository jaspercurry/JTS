# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: diagnosis-honesty, the boost gate's evidence claim, and post-apply verification."""

from __future__ import annotations

import dataclasses
import pytest
from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker.crossover_v2 import (
    intervention as iv,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_MEASURE,
)

from jasper.active_speaker.crossover_v2.intervention import LINEARIZATION_TRIM_SANITY_MARGIN_DB
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


@dataclasses.dataclass(frozen=True)
class _MarginMatch:
    """The one field ``decide_trim`` reads off a realized-level match."""

    difference_db: float


#: The two ULPs of one nominal anchor. Both print as "-2.691" on every surface
#: that rounds — including the guard's own journal line, which is why the CI
#: log showed ``drift_db=6.0 margin_db=6.0`` beside a rejection — and they
#: re-derive ``abs((anchor - 6.0) - anchor)`` on OPPOSITE sides of the margin:
#: 5.999999999999999 and 6.000000000000001. Measured, not chosen.
_MARGIN_ANCHOR_UNDER_ULP = -2.691
_MARGIN_ANCHOR_OVER_ULP = -2.6910000000003


def _trim_at_exactly_the_margin(anchor_db: float):
    """``decide_trim`` on a scan that drifted EXACTLY the sanity margin.

    Driven through production rather than recomputed here. An earlier version
    of this pin evaluated the comparison inline in the test body with the
    tolerance hardcoded, which made it a tautology about ``math.isclose`` and a
    second source of truth for the rule it claimed to pin — the adversarial
    gate killed it by rebinding the module's ``math`` to an always-False shim
    and watching every arm stay green.
    """
    margin = LINEARIZATION_TRIM_SANITY_MARGIN_DB
    anchored = {"woofer": 0.0, "tweeter": anchor_db}
    resolved = {"woofer": 0.0, "tweeter": anchor_db - margin}
    return iv.decide_trim(
        anchored_db=anchored,
        resolved_db=resolved,
        tweeter_role="tweeter",
        # EQUAL realized level on both pairs, so nothing but the sanity bound
        # can decide this call. With unequal levels the ``anchor_levels_better``
        # arm would commit the anchored pair too, and a test that could not
        # tell those two apart would pass for the wrong reason.
        anchored_match=_MarginMatch(1.0),
        resolved_match=_MarginMatch(1.0),
        ripple_db=0.4,
    )


@pytest.mark.parametrize(
    ("case", "anchor_db"),
    [
        ("re-derives just under the margin", _MARGIN_ANCHOR_UNDER_ULP),
        ("re-derives just over it", _MARGIN_ANCHOR_OVER_ULP),
    ],
    ids=["under_ulp", "over_ulp"],
)
def test_a_drift_that_is_the_margin_is_trusted_whichever_ulp_it_lands_on(
    case, anchor_db,
):
    """The boundary must be a rule, not a coin flip across interpreters.

    ``drift_db`` is a difference of two doubles neither of which is exactly
    representable, so a scan that drifted EXACTLY the margin re-derives a ULP
    either side of it depending on the anchor's last bits — and those come out
    of numpy reductions whose SIMD path varies by build. A bare ``>`` therefore
    answered differently on py3.11 (trusted) and py3.12/3.13 (rejected) for the
    same input, on a test that had been green for months.

    Asserted on the RETURNED DECISION, so the pin binds production: the scan is
    not beyond the margin, it was not rejected, and the record does not carry
    the sanity-drift strategy.
    """
    decision = _trim_at_exactly_the_margin(anchor_db)

    assert decision.beyond_sanity_margin is False, case
    assert decision.outcome == "fitted", case
    assert (
        decision.strategy
        is not iv.TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
    ), case
    assert decision.anchor_drift_db == pytest.approx(
        LINEARIZATION_TRIM_SANITY_MARGIN_DB
    ), case


def test_the_over_ulp_anchor_really_does_reproduce_the_naive_failure(monkeypatch):
    """The fixture's own self-check, and it runs through production too.

    Without it, ``_MARGIN_ANCHOR_OVER_ULP`` could drift to a value landing on
    the same side as its twin, and the parametrization above would pass while
    pinning one case twice. Rather than recomputing the comparison here, this
    removes the TOLERANCE from the shipped code — exactly the mutation the gate
    used to kill the previous version of this pin — and requires the two arms
    to diverge:

    * the over-ULP anchor is rejected (the CI failure, reproduced), and
    * the under-ULP anchor is still trusted, which is what makes this a ULP
      question rather than the fixture being beyond the margin outright.
    """
    monkeypatch.setattr(iv.math, "isclose", lambda *a, **k: False, raising=True)

    assert _trim_at_exactly_the_margin(
        _MARGIN_ANCHOR_OVER_ULP
    ).beyond_sanity_margin is True, (
        "this arm must reproduce the CI failure once the tolerance is gone, "
        "or the test above pins nothing"
    )
    assert _trim_at_exactly_the_margin(
        _MARGIN_ANCHOR_UNDER_ULP
    ).beyond_sanity_margin is False, (
        "and its twin must not, or the two arms are not two ULPs of one number"
    )


def test_the_sanity_bound_reads_its_tolerance_from_one_comparison():
    """One comparison, not two that can disagree.

    A second `>` added anywhere for the same bound would reintroduce the coin
    flip on whichever path skipped the tolerance.
    """
    import inspect

    source = inspect.getsource(iv.decide_trim)
    assert source.count("> float(sanity_margin_db)") == 1
    assert "math.isclose(" in source
