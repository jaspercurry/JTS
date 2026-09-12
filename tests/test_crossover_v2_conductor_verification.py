# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: diagnosis-honesty, the boost gate's evidence claim, and post-apply verification."""

from __future__ import annotations

import dataclasses
import logging
import pytest
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import (
    intervention as iv,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_VERIFY,
    PHASE_MEASURE,
)

from jasper.active_speaker.crossover_v2_flow import (
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    CLAIM_FAIL,
    CLAIM_NOT_EVALUATED,
    CLAIM_NO_PER_BRANCH_CAPTURE,
    CLAIM_PASS,
    verify_absolute_tolerance_db,
)
from jasper.audio_measurement import gating
from jasper.audio_measurement.program_analysis import (
    ProgramAnalysis,
)
from tests._log_events import event_fields
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _DIAG_LOGGER,
    _absolute,
    _conductor,
    _driver_response_diag,
    _loc,
    _measure_analysis,
    _run_phase,
    _tracking_with_frame,
    _verify_analysis,
    _verify_to_apply,
)


# --- diagnosis-honesty batch: what the instruments disclose ---------------------
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


def test_verify_diag_names_which_floor_the_gate_landed_on(caplog):
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        # 8.0 ms, matching the MEASURE fixture's own window: a SHORTER verify
        # gate is refused by the gate-comparability rule before tracking runs,
        # and this test is about what an accepted capture discloses.
        summed_response=_driver_response_diag(
            "summed", window_ms=8.0, floor_hz=125.0,
            floor_source=gating.FLOOR_SEARCH_BOUND,
        ),
        summed_ripple_db=1.1,
        verify_tracking={
            "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            "tracking_band_hz": [2000.0, 4000.0],
        },
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True

    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["verify_gate_window_ms"] == "8.0"
    assert fields["verify_gate_floor_source"] == gating.FLOOR_SEARCH_BOUND
    # The two states must remain distinguishable values, not two spellings of
    # the same one — that indistinguishability IS the defect.
    assert gating.FLOOR_SEARCH_BOUND != gating.FLOOR_MEASURED


def test_measure_diag_names_the_binding_gate_and_its_floor_source(caplog):
    """#1966 — MEASURE reports the SHORTEST driver window, so it must report
    that same response's floor source, never another response's."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()

    def measure(program):
        analysis = _measure_analysis(program)
        return dataclasses.replace(
            analysis,
            driver_responses=(
                # The binding (shortest) window is the search-bound one.
                _driver_response_diag(
                    "woofer", window_ms=5.0,
                    floor_source=gating.FLOOR_SEARCH_BOUND,
                ),
                _driver_response_diag(
                    "tweeter", window_ms=9.0,
                    floor_source=gating.FLOOR_MEASURED,
                ),
            ),
        )

    fakes.measure = measure
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)

    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["gate_window_ms"] == "5.0"
    assert fields["gate_floor_source"] == gating.FLOOR_SEARCH_BOUND, (
        "the reported floor source must belong to the response whose window "
        "was reported, not to whichever response happened to be first"
    )


def test_verify_pass_states_the_band_it_graded():
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        summed_response=_driver_response_diag("summed"),
        summed_ripple_db=1.1,
        verify_tracking={
            "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            "tracking_band_hz": [2000.0, 4000.0],
        },
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True

    assert c.verify_outcome == "pass"
    assert c.verify_graded_band_hz == [2000.0, 4000.0]


def test_a_passing_verify_still_discloses_the_frame_it_compared_across():
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        summed_response=_driver_response_diag("summed"),
        summed_ripple_db=1.1,
        verify_tracking=_tracking_with_frame(),
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True

    assert c.verify_outcome == "pass"
    assert c.verify_frame == {
        "offset_db": -0.75,
        "tilt_db_per_octave": -0.79,
        # The span the fit saw, carried because a two-parameter fit over few
        # bins or a narrow reach is ill-conditioned and the record is the only
        # place a reader can see that. It is also NOT the graded band whenever
        # the prediction has a deep notch — these bins are the ones the
        # comparison trusts.
        "pivot_hz": 2828.4,
        "n_bins": 400,
        "band_hz": [2000.0, 4000.0],
        # Both grades, so no screen can render the tilt-removed half alone.
        "rms_db_raw": 0.4,
        "max_db_raw": 0.9,
        "rms_db_tilt_removed": 0.18,
        "max_db_tilt_removed": 0.31,
    }


def test_an_unfitted_frame_is_disclosed_as_absent_never_as_agreement():
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        summed_response=_driver_response_diag("summed"),
        summed_ripple_db=1.1,
        verify_tracking=_tracking_with_frame(
            offset_db=None, tilt_db_per_octave=None, pivot_hz=None, n_bins=0,
            band_hz=None, tilt_removed={"rms_db": None, "max_db": None},
        ),
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True

    assert c.verify_frame is None


def test_a_verify_that_graded_nothing_claims_no_frame():
    """An early refusal compared nothing, so it spanned no frame — and a prior
    attempt's frame must not leak into this one (the same reset discipline the
    graded band carries)."""
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep", confidence=0.05),),
        summed_response=_driver_response_diag("summed"),
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    assert _run_phase(c, 3, 3)["accepted"] is False

    assert c.verify_frame is None


def test_a_verify_that_graded_nothing_claims_no_band():
    """#1868 — an early refusal graded nothing, and says nothing.

    Absence must mean "no comparison happened", never "checked everywhere",
    and a previous attempt's band must not leak into this one.
    """
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep", confidence=0.05),),
        summed_response=_driver_response_diag("summed"),
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    assert _run_phase(c, 3, 3)["accepted"] is False

    assert c.verify_graded_band_hz is None


# --------------------------------------------------------------------------- #
# #1967 — the boost gate's evidence claim, made substantive
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# R18 — honest post-apply verification (issues #1868 / #1654)
# --------------------------------------------------------------------------- #
#
# The numbers in these records are SYNTHETIC and labelled so — no hardware
# measurement is restated as a fixture value. The journal-verified fact they DO
# reproduce is the graded band: ``tracking_band_lo_hz=2000.0`` on a box whose
# tweeter is swept from Fc.


def test_absolute_miss_remains_independent_when_integration_passes():
    """An evaluated target miss is a result, not a failed capture."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=1.398262557, verify_absolute=_absolute(
            4.3139, worst_db=-4.3139, worst_hz=1590.4083,
        ),
    )
    verdict = _run_phase(c, 3, 3)

    assert verdict["accepted"] is True
    assert verdict.get("code") in {None, ""}
    assert c.verify_outcome == "pass"
    claims = c.verify_claims
    assert claims["integration"]["status"] == CLAIM_PASS  # the model agreed
    assert claims["absolute"]["status"] == CLAIM_FAIL
    assert claims["absolute"]["tolerance_db"] == 2.0
    assert claims["absolute"]["max_db"] == 4.3139
    assert claims["absolute"]["worst_hz"] == 1590.4083


def test_the_same_capture_passed_before_the_absolute_claim_existed():
    """The other direction of the mutation, at the conductor: the identical
    tracking evidence with NO crossover-region record still passes. So the new
    verdict is what changed the answer, not some unrelated tightening."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.069)
    assert _run_phase(c, 3, 3)["accepted"] is True
    assert c.verify_outcome == "pass"
    assert c.verify_claims["absolute"]["status"] == CLAIM_NOT_EVALUATED


def test_absolute_claim_inside_tolerance_passes_and_still_reports_its_numbers():
    """A passing handoff is disclosed, not silent."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.9, verify_absolute=_absolute(0.69),
    )
    assert _run_phase(c, 3, 3)["accepted"] is True
    absolute = c.verify_claims["absolute"]
    assert absolute["status"] == CLAIM_PASS
    assert absolute["max_db"] == 0.69
    assert absolute["band_hz"] == [1000.0, 4000.0]


def test_not_evaluated_claims_never_gate_and_keep_the_kernels_own_reason():
    """Refusing on a measurement nobody made is the same dishonesty pointed
    the other way — and a re-labelled reason erases which one it was."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.9,
        verify_absolute={"not_evaluated": "no_trusted_crossover_region"},
    )
    assert _run_phase(c, 3, 3)["accepted"] is True
    absolute = c.verify_claims["absolute"]
    assert absolute["status"] == CLAIM_NOT_EVALUATED
    assert absolute["reason"] == "no_trusted_crossover_region"


def test_an_ungradeable_tracking_claim_discloses_instead_of_refusing():
    """#3487, witnessed live: the documented recovery could not be receipted.

    ``POST /crossover/v2/republish`` by fingerprint then ``--apply`` is the
    runbook's own way back from any restore, including one the adoption table
    got wrong (#3485). But a republished candidate has no measure round behind
    it, so the verify's TRACKING claim has nothing to track against and
    ``max_db_notch_excluded`` is absent. The verdict collapsed that into
    ``verify_out_of_tolerance`` and refused index 1 four times — the whole retry
    budget, four rounds of audible playback — while the ABSOLUTE claim passed at
    1.503 dB. No capture was ever accepted, so no round graded and no receipt
    could mint: every rig, every republish.

    Same principle as the absolute claim's own pin above, pointed at the other
    half of §7's third claim — *refusing on a measurement nobody made is the
    same dishonesty pointed the other way*. R18's three-valued vocabulary
    already had the honest word and the claim record was already using it; only
    the gate was still two-valued. It is also the republish door's own declared
    contract, restored: ``handle_v2_republish`` clears ``verify_priors`` on
    purpose and says the consequence is that *a post-apply VERIFY of a
    republished candidate grades INDETERMINATE, never a false pass* — which a
    refusal is not either.

    The subject is the GATE's answer, which is what carried the wrong name. What
    the round then makes of an unavailable realization is the trust axis's own
    question and has its own pins.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=None, verify_absolute=_absolute(1.503),
    )
    _run_phase(c, 3, 3)

    assert c.verify_outcome == "pass"
    assert c.verify_code is None
    assert c.verify_claims["integration"]["status"] == CLAIM_NOT_EVALUATED
    # Never a pass either: the claim is on the record as ungraded, and the
    # number it would have carried stays absent rather than becoming 0.0.
    assert c.verify_claims["integration"]["max_db"] is None
    assert c.verify_claims["absolute"]["status"] == CLAIM_PASS


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
    verify_absolute, badged,
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
    from jasper.web.correction_crossover_v2 import (
        GRADE_INCONCLUSIVE,
        GRADE_MARK_VERIFIED,
        _post_apply_grade,
    )

    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=None, verify_absolute=verify_absolute,
    )
    _run_phase(c, 3, 3)

    assert c.verify_outcome == "pass"
    assert c.verify_claims["integration"]["status"] == CLAIM_NOT_EVALUATED
    grade = _post_apply_grade({
        "applied": True,
        "verify": {"outcome": c.verify_outcome, "claims": c.verify_claims},
    })

    assert grade["state"] == (GRADE_MARK_VERIFIED if badged else GRADE_INCONCLUSIVE)
    assert grade["graded"] is badged


def test_a_tracking_claim_that_missed_its_tolerance_still_refuses():
    """The control for the pin above, and the reason the refusal keeps its name.

    ``verify_out_of_tolerance`` now fires only where a tracking max was
    MEASURED and cleared the tolerance — which is what the code has always
    said, and what it did not always mean.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)

    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_out_of_tolerance"
    assert c.verify_claims["integration"]["status"] == CLAIM_FAIL


def test_per_branch_claims_are_named_not_evaluated_never_silently_claimed():
    """§7 names three claims; VERIFY plays ONE summed sweep, so two have no
    evidence. R18 does not widen the capture plan — it refuses to let
    "Verified." imply those two were proved."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.9, verify_absolute=_absolute(0.5),
    )
    assert _run_phase(c, 3, 3)["accepted"] is True
    claims = c.verify_claims
    for name in ("woofer_branch", "hf_branch"):
        assert claims[name] == {
            "status": CLAIM_NOT_EVALUATED, "reason": CLAIM_NO_PER_BRANCH_CAPTURE,
        }


def test_claims_reset_on_an_early_return_so_no_stale_claim_leaks():
    """Same discipline as the graded band and the frame beside them: an early
    refusal graded nothing and must not surface a prior attempt's claims."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.9, verify_absolute=_absolute(0.5),
    )
    _run_phase(c, 3, 3)
    assert c.verify_claims is not None
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0)
    _run_phase(c, 3, 4)
    assert c.verify_outcome == "inconclusive"
    # Absent, not stale — "nothing was graded" is the honest record here, and
    # every consumer renders absence as silence rather than as a pass.
    assert c.verify_claims is None


def test_absolute_tolerance_is_derived_from_the_spec_table_not_chosen():
    """The threshold has no literal of its own: it is the loosest
    ``flat_spec.SPEC_BANDS`` entry the crossover region overlaps, so revising
    that table with hardware data moves this without a second edit."""
    from jasper.active_speaker import flat_spec

    assert verify_absolute_tolerance_db([1000.0, 4000.0]) == max(
        tol for lo, hi, tol in flat_spec.SPEC_BANDS if lo < 4000.0 and 1000.0 < hi
    )
    # It is NOT the model-tracking tolerance wearing a different name.
    assert verify_absolute_tolerance_db([1000.0, 4000.0]) != flow.VERIFY_TOLERANCE_DB
    # A region the spec table declines to grade yields no bar at all, and the
    # claim is recorded not-evaluated rather than held to an invented one.
    assert verify_absolute_tolerance_db([17_000.0, 20_000.0]) is None
    assert verify_absolute_tolerance_db([1000.0]) is None



def test_the_crossover_region_claim_is_not_the_cloud_flatness_gauge():
    """SSOT: the two absolute grades are NOT peers, for a structural reason.

    ``assemble_cloud_group_result``'s ``flatness`` cannot own §7 claim 3 — it
    is assembled at group close, AFTER this verdict, and never exists at all
    on a session with no post-apply cloud. Pins that the crossover-region
    verdict stands on a capture the cloud has not contributed to, so a future
    consolidation cannot quietly delete the claim on cloudless paths.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.069, verify_absolute=_absolute(3.98),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    # No cloud has closed on this conductor, so no flatness gauge exists —
    # and the §7 claim was still made and still failed.
    assert c.group_cloud_result(PHASE_CLOUD_VERIFY) is None
    assert c.verify_claims["absolute"]["status"] == CLAIM_FAIL


def test_verify_diag_names_every_claim_and_the_crossover_region_numbers(caplog):
    """The operator's grep target carries the whole claim record — including
    the two nobody graded — so a corpus sweep counts what was judged instead
    of inferring it from a bare ``accepted=true``."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.069, verify_absolute=_absolute(3.98),
    )
    with caplog.at_level(logging.INFO):
        _run_phase(c, 3, 3)
    line = next(
        r.message for r in caplog.records
        if "correction.crossover_v2_verify_diag" in r.message
    )
    assert f"woofer_branch:not_evaluated({CLAIM_NO_PER_BRANCH_CAPTURE})" in line
    assert f"hf_branch:not_evaluated({CLAIM_NO_PER_BRANCH_CAPTURE})" in line
    assert "integration:pass" in line
    assert "absolute:fail" in line
    assert "absolute_worst_hz=1700.0" in line
    assert "absolute_tolerance_db=2.0" in line
    assert "absolute_band_lo_hz=1000.0" in line




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
