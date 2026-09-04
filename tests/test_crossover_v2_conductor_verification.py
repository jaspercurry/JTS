# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: diagnosis-honesty, the boost gate's evidence claim, and post-apply verification."""

from __future__ import annotations

import dataclasses
import logging
import numpy as np
import pytest
from dataclasses import replace
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import (
    accountability,
    intervention as iv,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_CORRECTION_MODEL_ERROR,
    REASON_CORRECTION_ROLLBACK_FAILED,
)
from jasper.active_speaker.crossover_v2_flow import (
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    CLAIM_FAIL,
    CLAIM_NOT_EVALUATED,
    CLAIM_NO_PER_BRANCH_CAPTURE,
    CLAIM_PASS,
    verify_absolute_tolerance_db,
    CrossoverV2Session,
)
from jasper.audio_measurement import gating
from jasper.audio_measurement.program_analysis import (
    ProgramAnalysis,
    solve_branch_trims,
)
from tests.crossover_v2_fixtures import (
    CAPS,
    bank_into,
    CLOUD_MAP,
    CLOUD_MEASURE_INDEXES,
    FC_HZ,
    FakeSeams,
    SESSION,
    SESSION_VOLUME_DB,
    _BLIND_SPAN_RESULT,
    _DIAG_LOGGER,
    _FIXTURE_FC_HZ,
    _LINEARIZABLE_FREQS_HZ,
    _absolute,
    _cloud_conductor,
    _conductor,
    _driver_response_diag,
    _eligible_measure_analysis,
    _gate_block,
    _loc,
    _measure_analysis,
    _moving_notch_cloud,
    _preset,
    _roles,
    _run_phase,
    _tracking_with_frame,
    _verify_analysis,
    _verify_to_apply,
    _walk,
    _walk_measure_cloud_to_close,
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
    """#1966 — ``gate_window_ms`` alone cannot say whether anything was gated.

    A window that stops at a found reflection and a window CAPPED at the
    search ceiling because none was found print the same number. Across the
    whole 2026-07-30 corpus every capture was the second state, and the record
    could not say so: the gate computes ``floor_source`` and every v2 consumer
    dropped it.

    This fixture carries no ``capture_integrity`` (a raw ``ProgramAnalysis``),
    so the ROUND refuses it as untrusted evidence with no rollback anchor
    bound (#2537) — asserted first so the disclosure claim below is not read
    as "and so the round kept it". The ``verify_diag`` line's own numbers are
    unaffected by what the round later decides: it is written at VERIFY's own
    capture-gate step, before the round grades anything.
    """
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
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED

    assert "verify_gate_window_ms=8.0" in caplog.text
    assert f"verify_gate_floor_source={gating.FLOOR_SEARCH_BOUND}" in caplog.text
    # The two states must remain distinguishable values, not two spellings of
    # the same one — that indistinguishability IS the defect.
    assert gating.FLOOR_SEARCH_BOUND != gating.FLOOR_MEASURED


def test_every_retained_position_carries_its_gate_provenance_as_a_sentence():
    """#1966 at the surface. The enum landed first and fixed the record for a
    machine; a person opening the per-position evidence file still had to know
    that ``search_span_bound`` means "nothing was gated out".

    So the sentence rides beside the enum on every retained take — and it is
    RENDERED, not composed here: the copy has exactly one writer, so this
    file and the retained-capture sidecar cannot describe the same gate two
    different ways.
    """
    from jasper.active_speaker.crossover_v2_flow import _gate_disclosure
    from jasper.audio_measurement import gate_disclosure as gd

    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained, phase=PHASE_CLOUD_MEASURE),
        ),
        index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    assert retained, "the walk must have retained positions to check"
    # The allowlist must project it; absent, the field is silently dropped.
    assert all("gate_disclosure" in meta for meta in retained)

    # And the helper renders the two states as opposite claims.
    capped = _gate_disclosure(_driver_response_diag(
        "summed", window_ms=7.0, floor_hz=142.9,
        floor_source=gating.FLOOR_SEARCH_BOUND,
    ))
    found = _gate_disclosure(_driver_response_diag(
        "summed", window_ms=4.0, floor_hz=250.0,
        floor_source=gating.FLOOR_MEASURED,
    ))
    assert "nothing was gated out" in capped
    assert "reflection measured" in found
    assert "nothing was gated out" not in found
    # Rendered by the single writer, byte for byte.
    assert capped == gd.describe_gate(
        {"applied": True, "window_ms": 7.0,
         "floor_source": gating.FLOOR_SEARCH_BOUND}
    )
    assert _gate_disclosure(None) is None


def test_every_retained_position_carries_the_numbers_behind_that_sentence():
    """Ticket 1.5, at the same surface and for the same reason #1966 was.

    The sentence fixed the record for a PERSON; a reader mining the banked
    round for numbers still had to regex English out of it, which the evidence
    packet refuses to do and said so in its ``not_evaluated`` block. So the two
    numbers ride beside the sentence on every retained take — derived by the
    same single typed reader, never assembled here.

    This is the BANKED RECORD's half: what reaches the ``bank_take`` seam is
    ``cloud_position_record``'s own dict, so both keys are on every take
    whatever their values. The separate allowlist between that record and the
    CLOUD artifact's rows (``position_evidence._RECORD_FIELDS``, which drops a
    key whose value is ``None``) is pinned in
    ``tests/test_attribution_persistence.py``.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        _gate_moved_rms_db,
        _gate_reflection_delay_ms,
    )
    from jasper.audio_measurement import gate_disclosure as gd

    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained, phase=PHASE_CLOUD_MEASURE),
        ),
        index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    assert retained, "the walk must have retained positions to check"
    # Present as KEYS on every take, whatever their values: absent-because-
    # unmeasurable and absent-because-nobody-banks-it are different facts and
    # the record must be able to say the first.
    for meta in retained:
        assert "gate_moved_rms_db" in meta
        assert "gate_reflection_delay_ms" in meta

    # …and both are the typed reader's own derivations, on a block that has
    # something to derive. 15.73 - 10.40 = 5.33; the absolute 15.73 must not
    # be what comes back.
    block = _gate_block()
    response = dataclasses.replace(_driver_response_diag("summed"), gating=block)
    typed = gd.build_gate_disclosure(block)
    assert _gate_moved_rms_db(response) == typed.delta_rms_db == 2.59
    assert _gate_reflection_delay_ms(response) == typed.reflection_delay_ms
    assert _gate_reflection_delay_ms(response) == pytest.approx(5.33)
    assert _gate_moved_rms_db(None) is None
    assert _gate_reflection_delay_ms(None) is None


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

    assert "gate_window_ms=5.0" in caplog.text
    assert f"gate_floor_source={gating.FLOOR_SEARCH_BOUND}" in caplog.text, (
        "the reported floor source must belong to the response whose window "
        "was reported, not to whichever response happened to be first"
    )


def test_verify_pass_states_the_band_it_graded():
    """#1868 — "Verified." must say over what.

    The graded band is not the nominal Fc±1 octave: ``overlap_band_hz`` clamps
    its lower edge up to the tweeter's real sweep floor and ``_analyze_verify``
    clamps it again to the capture's validity floor. It used to ride the
    ``evidence`` block, which the host persists only on a NON-pass outcome — so
    the one screen that says the result is good was the one screen that never
    said what was checked.

    This fixture carries no ``capture_integrity``, so the ROUND refuses it as
    untrusted evidence with no rollback anchor bound (#2537) — asserted first.
    VERIFY's OWN pass/band bookkeeping (``verify_outcome``,
    ``verify_graded_band_hz``) is written at the capture-gate step, ahead of
    round grading, and is unaffected by the round's later refusal.
    """
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
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED

    assert c.verify_outcome == "pass"
    assert c.verify_graded_band_hz == [2000.0, 4000.0]


def test_a_passing_verify_still_discloses_the_frame_it_compared_across():
    """Rung P1 — "Verified." must say how much of the agreement was frame.

    VERIFY differences an on-axis MODEL against an in-room MEASUREMENT. On the
    2026-07-29 corpus a single −0.79 dB/octave tilt between those two frames
    accounted for 84 % of the flow's apparent prediction error, so a pass with
    the frame unstated invites exactly the reading the panel had to correct.
    Surfaced on a PASS for the same reason the graded band is (#1868): the
    passing screen is the one that would otherwise overclaim.

    This fixture carries no ``capture_integrity``, so the ROUND refuses it as
    untrusted evidence with no rollback anchor bound (#2537) — asserted first,
    same reasoning as the graded-band test above.
    """
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
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED

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
    """A comparison whose frame could not be measured says nothing, rather than
    reporting a flat frame — absence and "the frames matched" are different
    claims and must not collapse into one.

    This fixture carries no ``capture_integrity``, so the ROUND refuses it as
    untrusted evidence with no rollback anchor bound (#2537) — asserted first,
    same reasoning as the two frame/band tests above. ``verify_frame`` is
    written at VERIFY's own capture-gate step and is unaffected by the round's
    later refusal.
    """
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
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_ROLLBACK_FAILED

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


def test_boost_exclusions_come_from_the_blind_span_below_the_registry_floor(caplog):
    """#1967. The registry's band is floored at ``ECHO_BAND_HF_REGIME_FLOOR_HZ``,
    so it contributes no exclusions below it — the gate's "null-exclusion stays
    a measured, registry-gated fact" is unbacked there. This is the check that
    backs it: dips the cloud's own positions disagree about are withheld from
    the LIFT vocabulary.

    The disclosure is asserted alongside the value because a bound that
    silently narrows a correction is the shape this whole area is trying to
    stop shipping.
    """
    c = _cloud_conductor(FakeSeams())
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    bands = c._boost_excluded_bands_hz(
        _moving_notch_cloud([1800.0] * 5 + [2400.0] * 3), _BLIND_SPAN_RESULT,
    )

    floor_hz = c._cloud_echo_band.band_hz[0]
    assert bands, "a cloud whose positions disagree must offer something"
    # Every offered band sits inside the span the registry could not reach:
    # above the cloud's own validity floor, below the registry's lower edge.
    assert all(1200.0 <= lo < hi <= floor_hz for lo, hi in bands), (bands, floor_hz)
    assert "event=correction.crossover_v2_boost_evidence" in caplog.text
    assert "registry_reason=no_corroborating_arrivals" in caplog.text
    assert f'unadjudicated_span_hz="[1200.0, {floor_hz}]"' in caplog.text
    # A withhold is WARNING, not INFO: it silently narrows a correction, so it
    # has to reach a journal a household's operator actually reads.
    withheld = [
        r for r in caplog.records
        if "crossover_v2_boost_evidence" in r.getMessage()
    ]
    assert withheld and all(r.levelno == logging.WARNING for r in withheld)


def test_a_cloud_whose_positions_agree_loses_no_boost(caplog):
    """The owner's ruling, executable: this bound withholds on CONTRADICTING
    evidence and never on absent or agreeing evidence.

    Eight positions notched at the same frequency read invariant, so nothing is
    offered — the +8.06 dB at 3633.6 Hz that motivated #1967 sits in exactly
    this class and keeps flowing. It is still disclosed, because "the registry
    could not look here" stays true whatever the check found.
    """
    c = _cloud_conductor(FakeSeams())
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    bands = c._boost_excluded_bands_hz(
        _moving_notch_cloud([1800.0] * 8), _BLIND_SPAN_RESULT,
    )

    assert bands == ()
    assert "event=correction.crossover_v2_boost_evidence" in caplog.text
    assert "boost_excluded_bands_hz=[]" in caplog.text
    # The dip WAS seen — it just did not contradict a boost. A reader must be
    # able to tell that from "nothing was measured".
    assert "n_dips=1 n_position_dependent=0" in caplog.text
    # ...and withholding nothing is INFO. Only a narrowed correction earns a
    # WARNING, or the level stops carrying information.
    kept = [
        r for r in caplog.records
        if "crossover_v2_boost_evidence" in r.getMessage()
    ]
    assert kept and all(r.levelno == logging.INFO for r in kept)


def test_the_boost_bound_fails_open_when_it_cannot_be_computed(caplog):
    """Failing CLOSED would blanket-ban boost below 4 kHz on a numeric hiccup,
    which is the blunt gate this function exists to avoid. Both unusable-input
    shapes yield today's permission exactly, and say so."""
    c = _cloud_conductor(FakeSeams())
    combined = _moving_notch_cloud([1800.0] * 5 + [2400.0] * 3)

    # No blind span at all: the cloud's validity floor is already above the
    # registry's own floor, so nothing was hidden.
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    assert c._boost_excluded_bands_hz(
        combined, {"validity_floor_hz": 9000.0, "null_registry": {}},
    ) == ()
    assert "variance_reason=no_blind_span" in caplog.text

    # And an unexpected failure inside the check is caught, disclosed at
    # WARNING, and leaves the permission where it was.
    caplog.clear()
    with pytest.MonkeyPatch.context() as mp:
        import jasper.audio_measurement.interference_nulls as nulls

        def _boom(*a, **k):
            raise RuntimeError("synthetic")

        mp.setattr(nulls, "classify_dip_position_variance", _boom)
        assert c._boost_excluded_bands_hz(combined, _BLIND_SPAN_RESULT) == ()
    assert "event=correction.crossover_v2_boost_variance_failed" in caplog.text
    assert "variance_reason=variance_check_failed" in caplog.text


def test_the_boost_evidence_disclosure_is_reached_by_an_ordinary_walk(caplog):
    """Reachability, without a monkeypatch anywhere.

    The wiring test below stubs the composer to prove the vocabulary carries
    what it returns; that says nothing about whether the composer is CALLED on
    the production path. This walks the real cloud group to close and asserts
    the real disclosure fired — so a future refactor that leaves
    ``_boost_excluded_bands_hz`` orphaned fails here rather than shipping a
    bound nothing invokes.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    disclosures = [
        r for r in caplog.records
        if "event=correction.crossover_v2_boost_evidence" in r.getMessage()
    ]
    assert len(disclosures) == 1, [r.getMessage()[:80] for r in disclosures]
    message = disclosures[0].getMessage()
    # The span it reports is the real one: this cloud's own validity floor up
    # to the registry band's real lower edge, not a placeholder.
    assert f'unadjudicated_span_hz="[100.0, {c._cloud_echo_band.band_hz[0]}]"' in message


def test_per_filter_boost_verdicts_are_disclosed_by_the_conductor(caplog):
    """``linearization_fit`` is pure computation and owns no logger, so the
    per-filter verdicts only become observable if the conductor emits them.

    Walks the real cloud group with an exclusion band placed over the boost
    the fake session's woofer actually attracts, and asserts the drop reaches
    the journal with the arithmetic that caused it. A bound that silently
    removes a correction is the failure mode this whole area exists to avoid.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow.CrossoverV2Session, "_boost_excluded_bands_hz",
            lambda self, combined, result: ((350.0, 450.0),),
        )
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    verdicts = [
        r for r in caplog.records
        if "event=correction.crossover_v2_boost_excluded_verdicts" in r.getMessage()
    ]
    assert verdicts, "the per-filter verdicts never reached the journal"
    dropped = [r for r in verdicts if "realized_in_band_db" in r.getMessage()]
    assert dropped
    # A drop narrows a correction, so it is a WARNING.
    assert all(r.levelno == logging.WARNING for r in dropped)
    assert "band_hz" in dropped[0].getMessage()


def test_the_fit_vocabulary_actually_carries_the_cloud_s_boost_exclusions():
    """The wiring, end to end at the conductor's own surface: what
    ``_boost_excluded_bands_hz`` composes is what ``fit_driver_linearization``
    is handed. Without this the bound could be computed, logged, and dropped.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    seen: list[tuple[tuple[float, float], ...]] = []
    real_fit = iv.fit_driver_linearization

    def _spy(resp, envelope, **kwargs):
        seen.append(kwargs["vocabulary"].boost_excluded_bands_hz)
        return real_fit(resp, envelope, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _spy)
        mp.setattr(
            flow.CrossoverV2Session, "_boost_excluded_bands_hz",
            lambda self, combined, result: ((1500.0, 1900.0),),
        )
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    assert seen and all(bands == ((1500.0, 1900.0),) for bands in seen)


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


def test_the_delta_probe_still_refuses_first_so_its_rollback_is_never_displaced():
    """R18 is purely additive to the refusal order (resilience review finding).

    A probe-class refusal carries an AUTOMATIC remedy — the graph comes off.
    Gating ahead of it would let a capture that fails this claim AND warrants a
    rollback get neither.

    Injected at ``_grade_round_once``: since the fifth-principle routing the
    probe reports and the ROUND decides, so "the probe's refusal" reaches this
    ordering as the round's. The subject is unchanged — R18's absolute claim
    must not displace it.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.069, verify_absolute=_absolute(3.98),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow.CrossoverV2Session, "_grade_round_once",
            lambda self, verdict: flow.PhaseVerdict(
                False, REASON_CORRECTION_MODEL_ERROR,
            ),
        )
        verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == REASON_CORRECTION_MODEL_ERROR
    # The claim was still GRADED and still says it failed — the ordering
    # decides which refusal is reported, never whether the claim was made.
    assert c.verify_claims["absolute"]["status"] == CLAIM_FAIL


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


def _walk_a_session_whose_filters_reach_the_blind_zone(caplog):
    """The #2523 two-branch fixture, which places real filters inside its own
    per-branch measurement hole (1255.8-2020.0 Hz).

    Shares its shape with
    ``test_prediction_gate_logs_the_improved_path_with_both_terms`` — an 8 dB
    peak on a 5 dB comb, each branch carrying its own half of a matched LR4 —
    because that is a fixture already established to drive real filters into
    the crossover region rather than one built to make this emit fire.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
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
    return c


def _records(caplog, event: str):
    return [
        r for r in caplog.records if f"event={event}" in r.getMessage()
    ]


def test_blind_zone_placements_reach_the_journal(caplog):
    """#2599 SF1. ``linearization_fit`` owns no logger, so a verdict it makes
    is only a verdict if this loop says it out loud.

    The adversarial gate's words: "the verb only differs from silence if
    something emits it." Rule 2 deliberately DISCLOSES rather than refuses, so
    a disclosure nothing surfaces makes the whole adjudication empty — the
    filter ships and no one is told, which is exactly the pre-#2599 state the
    round-3 receipt was in.
    """
    c = _walk_a_session_whose_filters_reach_the_blind_zone(caplog)

    records = _records(caplog, "correction.crossover_v2_blind_zone_placements")
    assert records, "the blind-zone placements never reached the journal"
    message = records[0].getMessage()
    # Self-contained: what was placed, and the hole it was placed in, so a
    # reader needs no second journal line to interpret it.
    assert "role=" in message
    assert "placed=" in message
    assert "measured_excess_db" in message
    assert "freq_hz" in message
    # The TOP-LEVEL ``blind_bands_hz`` must carry the session's holes, and
    # this is asserted on that field's own rendered value rather than on the
    # message as a whole. Every hole's edges also appear inside each
    # ``placed`` record, so a substring search over the line passes with the
    # top-level field emptied -- it is the one field that makes the record
    # self-contained (and that lists holes no filter landed in), so it needs
    # its own assertion.
    field = message.split("blind_bands_hz=", 1)[1].strip().strip('"')
    assert field not in ("[]", ""), f"blind_bands_hz carried nothing: {field!r}"
    holes = {
        tuple(placement["blind_band_hz"])
        for fit in c.candidate.linearization.values()
        for placement in (fit.get("blind_zone_placements") or [])
    }
    assert holes
    for lo_hz, hi_hz in holes:
        assert str(lo_hz) in field and str(hi_hz) in field


def test_the_blind_zone_emit_severity_tracks_whether_level_was_added(caplog):
    """Severity carries the distinction the disclosure exists to make.

    A cut in a hole removes level on evidence no branch has; a BOOST adds
    level into the phase-sensitive blend on the same absent evidence — the
    class the gate's 400-fit probe found shipping unnamed. Cuts-only is INFO,
    any positive gain is WARNING. Neither gates anything; both ship.
    """
    c = _walk_a_session_whose_filters_reach_the_blind_zone(caplog)

    records = _records(caplog, "correction.crossover_v2_blind_zone_placements")
    assert records
    # Re-derive the expectation from the CANDIDATE rather than by parsing the
    # log line, so this pins the emit's rule and not its formatting.
    by_role = {
        role: fit["blind_zone_placements"]
        for role, fit in c.candidate.linearization.items()
        if fit.get("blind_zone_placements")
    }
    assert by_role, "the candidate carried no placements to emit"
    assert len(records) == len(by_role)
    for record in records:
        message = record.getMessage()
        role = next(r for r in by_role if f"role={r}" in message)
        added_level = any(p["gain_db"] > 0.0 for p in by_role[role])
        assert record.levelno == (
            logging.WARNING if added_level else logging.INFO
        ), message[:200]


def test_a_refused_boost_reaches_the_journal_as_a_warning(caplog):
    """#2599 SF1, rule-1 half. A refusal nothing emits is a SILENT refusal —
    the failure mode this whole area exists to avoid, and the one the #1967
    block beside it was written for.

    The fit is wrapped rather than coaxed. Probed 2026-08-16: no conductor
    fixture in this suite tripped the measured-target bound — every role
    reported zero drops — so a test that merely walked one would have asserted
    ``0 == 0`` and passed while the emit was deleted. That is the vacuity trap
    this repo has been bitten by, so the drop is INJECTED into an otherwise
    real fit and the assertion is that the conductor SAYS it. Should a fixture
    later trip the bound for real, this test keeps working and
    ``test_the_new_verdict_events_stay_silent_on_an_ordinary_session`` is what
    tracks the count. The bound's own behaviour — when a drop is produced at
    all — is pinned in
    ``tests/test_active_speaker_linearization_fit.py``; this pins the wiring.
    """
    from dataclasses import replace

    from jasper.active_speaker.linearization_fit import BoostEvidenceDrop

    injected = BoostEvidenceDrop(
        freq_hz=434.01678699822264, q=1.0, gain_db=2.0149,
        action_band_hz=(301.8, 472.0), measured_excess_db=2.7819,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    real_fit = iv.fit_driver_linearization

    def _with_a_refused_boost(resp, envelope, **kwargs):
        return replace(
            real_fit(resp, envelope, **kwargs),
            lift_boost_evidence_drops=(injected,),
            lift_suppressed_reason="boost_above_measured_target",
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _with_a_refused_boost)
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    records = _records(
        caplog, "correction.crossover_v2_boost_measured_target_verdicts"
    )
    assert records, "a refused boost never reached the journal"
    assert len(records) == len(c.candidate.linearization)
    for record in records:
        message = record.getMessage()
        # A refusal narrows a correction, so it is always a WARNING -- unlike
        # the #1967 block, this event has no accepted-remainder case.
        assert record.levelno == logging.WARNING
        # The arithmetic that caused it, so a reader re-derives rather than
        # trusts: which filter, over what span, against what evidence.
        assert "434.0167" in message
        assert "action_band_hz" in message
        assert "measured_excess_db" in message
        assert "boost_above_measured_target" in message


def test_the_new_verdict_events_stay_silent_on_an_ordinary_session(caplog):
    """The other direction, so neither emit becomes journal spam.

    A session where the bound refused nothing and no filter landed in a hole
    must add no lines at all. Asserted against the CANDIDATE's own fields, so
    this cannot pass by the events being unreachable — it fails if the fields
    are populated and the emits are missing, and it fails if the emits fire
    when the fields are empty.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    for field, event in (
        ("lift_boost_evidence_drops",
         "correction.crossover_v2_boost_measured_target_verdicts"),
        ("blind_zone_placements",
         "correction.crossover_v2_blind_zone_placements"),
    ):
        populated = [
            role for role, fit in c.candidate.linearization.items()
            if fit.get(field)
        ]
        assert len(_records(caplog, event)) == len(populated), field


def test_a_hole_centred_BOOST_makes_the_blind_zone_emit_a_warning(caplog):
    """The severity rule's other half — the one the shipped fixtures cannot
    reach, so it is injected rather than left unpinned.

    Every conductor fixture that reaches the blind zone places only CUTS
    there (probed), so a test that merely walked one would exercise the INFO
    branch and pass with the ``WARNING if any(gain > 0)`` conjunct deleted.
    That is the half-guarded-site trap. A boost in a hole is the class the
    gate's 400-fit probe found shipping unnamed and the one this disclosure
    most exists for — adding level into a phase-sensitive blend on evidence
    no branch has — so it gets the louder level, and that has to be pinned by
    something.

    The fit-side behaviour (a hole-centred boost really is named) is pinned
    for real in ``test_a_hole_centred_lift_boost_is_named_too``; this pins the
    emit's severity rule given such a placement.
    """
    from dataclasses import replace

    from jasper.active_speaker.linearization_fit import BlindZonePlacement

    injected = BlindZonePlacement(
        freq_hz=1404.4032452955714, q=2.0, gain_db=+1.5,
        blind_band_hz=(1291.4104702195973, 2077.2411784104297),
        measured_excess_db=-2.0,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    real_fit = iv.fit_driver_linearization

    def _with_a_hole_centred_boost(resp, envelope, **kwargs):
        return replace(
            real_fit(resp, envelope, **kwargs),
            blind_zone_placements=(injected,),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(iv, "fit_driver_linearization", _with_a_hole_centred_boost)
        c = _cloud_conductor(fakes)
        _walk_measure_cloud_to_close(c)

    records = _records(caplog, "correction.crossover_v2_blind_zone_placements")
    assert records, "a hole-centred boost never reached the journal"
    assert len(records) == len(c.candidate.linearization)
    for record in records:
        assert record.levelno == logging.WARNING, record.getMessage()[:200]
        assert "1404.4032" in record.getMessage()


def test_a_correctly_levelled_pair_clears_the_improvement_floor(
    caplog, monkeypatch,
):
    """This fixture SHIPS again, and the reason is that it now levels exactly.

    **History, because this assertion has been inverted once before.** Until
    #2609 this fixture shipped. #2609 deleted PR-L5's ``level_frame_offset_db``
    and it began REFUSING under item 2's 0.5 dB material-improvement floor —
    correctly at the time, since the offset had been flattering the prediction.
    The band-matched give-back now lands the pair exactly level, the prediction
    is honest on its own terms, and it clears the floor without help.

    **The mechanism, measured on this fixture.** The two give-backs disagree by
    0.918 dB here, and in the OPPOSITE direction to the jts3 horn that motivated
    the fix::

        level-band (anchor)   woofer 1.147   tweeter 2.064
        core-band             woofer 2.104   tweeter 1.186
        raw trim              woofer 0.0     tweeter -1.773
        committed OLD (core)  woofer 0.0     tweeter -2.691   <- 1.835 dB DULL
        committed NEW (level) woofer 0.0     tweeter -0.856   <- realized 0.0

    That two-way behaviour is itself evidence about the defect's nature: a band
    MISMATCH mis-levels in whichever direction the correction's energy happens
    to sit relative to the graded span — hot on a horn whose shelf lives above
    it, dull here. A sign error could not do that.

    So the candidate the old rule refused was not a bad candidate; it was a
    correctly-shaped candidate whose predicted improvement was being scored
    against a pair mis-levelled 1.835 dB dull. Refusing it was the floor doing
    its job on a corrupted input.

    **What this test no longer covers, and where that lives instead.** It no
    longer exercises the floor's failing arm. That arm keeps its coverage at
    the layer that owns the decision —
    ``test_crossover_v2_accountability.py`` pins the
    ``LEDGER_NOT_AN_IMPROVEMENT`` verdict and the disclosure that rides with
    it. (Until the nanny burn-down that arm REFUSED, and the same test pinned
    ``decision.refusal_reason``.) Nothing was dropped by flipping this one; it
    was always a FIXTURE-flip test, and the fixture flipped back for a reason
    worth stating.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)

    # Force the ANCHOR to be the committed pair, which is the population this
    # is scoped to: a wild scan drift trips the sanity guard and the anchored
    # pair ships, so what the anchor computes is what gets predicted.
    monkeypatch.setattr(
        iv, "solve_ripple_optimal_trim",
        lambda *a, **k: (k["seed_trim_db"] - 20.0, 0.0, k["seed_trim_db"]),
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)

    # The floor is CLEARED, not merely un-enforced. (Before the nanny burn-down
    # this asserted the absence of a refusal code, which no longer
    # discriminates — nothing refuses on this path any more.) On this fixture
    # the levelled pair clears it a fortiori: the prediction meets the flat
    # spec outright, so the gate settles at ``predicted_in_spec`` without ever
    # needing an improvement argument. What must never appear is the failing
    # verdict the mis-levelled pair produced.
    _run_phase(c, 2, 2)
    assert (
        c.measure_predicted_spec_report["comparison"]["reason"]
        == accountability.LEDGER_PREDICTED_IN_SPEC
    )

    # Leg 1 — WHY it ships: the committed pair lands EXACTLY level. This is the
    # band-matched give-back's invariant showing up end-to-end in the planner,
    # not just in its unit test.
    assert "event=correction.crossover_v2_realized_level_match" in caplog.text
    assert "matched=true" in caplog.text
    assert "difference_db=0.0" in caplog.text

    # Leg 2 — the two give-backs, side by side, showing the 0.918 dB this
    # fixture's bands disagree by. The old anchor spent the core-band pair and
    # committed the tweeter at -2.691; the level-band pair commits -0.856.
    assert (
        'level_band_giveback_db="{\'woofer\': 1.147, \'tweeter\': 2.064}"'
        in caplog.text
    )
    assert (
        'core_band_giveback_db="{\'woofer\': 2.104, \'tweeter\': 1.186}"'
        in caplog.text
    )
    assert 'anchored_trim_db="{\'woofer\': 0.0, \'tweeter\': -0.856}"' in caplog.text
    # The precondition's instrumentation, on the ORDINARY path: this fixture's
    # base IS the band-average solve, so the polish delta is zero and the
    # invariant holds exactly. Pinned here for the zero case only — a literal
    # zero cannot distinguish "measured zero" from "hard-coded zero", so the
    # value assertion that kills that mutation lives where a NON-zero delta can
    # be driven (``test_the_journal_reports_the_polish_delta_it_measured`` in
    # test_crossover_v2_intervention_dual_run.py, mutation-verified).
    assert (
        'band_average_trim_db="{\'woofer\': 0.0, \'tweeter\': -1.773}"' in caplog.text
    )
    assert 'polish_delta_db="{\'woofer\': 0.0, \'tweeter\': 0.0}"' in caplog.text

    # Leg 3 — the prediction gate ran and passed on its own terms.
    assert "event=correction.crossover_v2_prediction_gate" in caplog.text
    assert "after_passed=true" in caplog.text


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
