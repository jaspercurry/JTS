# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: fixture premise, live-attempts loop, happy path, predicted-ripple disclosure (G1)."""

from __future__ import annotations


import numpy as np
import pytest
from jasper.active_speaker import branch_chain
from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker.crossover_v2 import durable_state
from jasper.active_speaker.crossover_v2.contracts import REFERENCE_MARK_DESIGN_AXIS
from jasper.active_speaker.crossover_v2.round_evidence import (
    MEASURED_BENEFIT_MARGIN_DB,
    measured_response_from_analysis,
)
from jasper.active_speaker.crossover_v2.durable_state import (
    PROVENANCE_REALIZED, AttemptIntegrity, AttemptRecord,
)
from jasper.active_speaker.crossover_v2.programs import GAIN_CAP_BACKOFF_DB, back_off_gain
from jasper.active_speaker.crossover_v2.alignment_prescription import alignment_delay_search_bounds_us
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.branch_chain import crossover_response_complex, sections_by_role
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.active_speaker.flat_spec import (
    evaluate_flat_spec,
    spec_convergence_residual,
)
from tests.test_active_speaker_profile import _two_way_preset
from tests.crossover_v2_fixtures import (
    CAPS,
    FakeSeams,
    _ENTRY_BASELINE_RESIDUAL_DB,
    _POST_APPLY_RESIDUAL_DB,
    _capture,
    _conductor,
    _preset,
    _run_phase,
    _verify_analysis,
)


def test_the_fixture_entry_baseline_is_measurably_worse_than_the_post_apply_one():
    """``_fixture_entry_baseline``'s whole reason to exist, made falsifiable.

    Every conductor this file builds grades its #2291 round against that
    baseline, and the grading is only honest if the "before" really is the worse
    measurement — by more than the claim margin. Nothing else in the file would
    notice if it stopped being true: ``_in_room_summed_db`` changing, the
    reducer's grid changing, or the sign of ``spec_convergence_residual``
    flipping would all turn every round into a measured REGRESSION, which the
    adoption table restores on, and the failures would surface far from here as
    refusals about rollback anchors.

    It also pins the two decimals the fixture's comment quotes, so those are
    checked numbers rather than remembered ones.
    """
    fakes = FakeSeams()
    conductor = _conductor(fakes)
    baseline = conductor.measure_entry_baseline
    assert baseline is not None

    post = measured_response_from_analysis(
        _verify_analysis(conductor.program_for_phase(PHASE_VERIFY)),
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
    )
    # Comparable by construction, or the benefit verdict is about the fixture
    # rather than about the speaker.
    assert baseline.program_id == post.program_id
    assert baseline.reference_mark == post.reference_mark
    assert baseline.curve.hz == post.curve.hz

    def residual_db(hz, db, excluded) -> float:
        report = evaluate_flat_spec(
            np.asarray(hz, dtype=np.float64),
            np.asarray(db, dtype=np.float64),
            np.asarray(excluded, dtype=bool),
        )
        convergence = spec_convergence_residual(report)
        assert convergence.evaluable and convergence.rms_db is not None
        return float(convergence.rms_db)

    before_db = residual_db(baseline.curve.hz, baseline.curve.db, baseline.excluded)
    after_db = residual_db(post.curve.hz, post.curve.db, post.excluded)
    assert before_db == pytest.approx(_ENTRY_BASELINE_RESIDUAL_DB, abs=0.001)
    assert after_db == pytest.approx(_POST_APPLY_RESIDUAL_DB, abs=0.001)
    assert (before_db - after_db) > MEASURED_BENEFIT_MARGIN_DB


def test_the_banked_sitting_survives_the_durable_state_round_trip():
    """A stamp the persistence layer drops is a stamp that never fired.

    The whole #2081 hazard lives across a restart — the predecessor is read
    back out of the state file "Start over" preserved — so the field has to
    make the round trip, not just exist in memory.
    """
    record = AttemptRecord(
        attempt_id="candidate-a",
        metric=contracts.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        provenance=PROVENANCE_REALIZED,
        sitting_id="the_session_that_measured_it",
        integrity=AttemptIntegrity(comparable=True),
        grade_db=1.0,
    )
    assert record.to_dict()["sitting_id"] == "the_session_that_measured_it"

    restored = durable_state.attempt_history_from_state(
        {"attempts_loop": {"history": [record.to_dict()]}}
    )
    assert [item.sitting_id for item in restored] == [
        "the_session_that_measured_it",
    ]


def test_a_pre_2081_persisted_row_restores_as_unrecorded_not_as_a_match():
    """Every shipped speaker's history looks like this on the upgrade deploy.

    Two such rows must not compare equal as one sitting — the restore has to
    hand the kernel the value it refuses on, which is what makes the upgrade
    stop claiming rather than claim something it cannot support.
    """
    legacy_row = {
        "attempt_id": "candidate-old",
        "metric": contracts.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        "provenance": PROVENANCE_REALIZED,
        "integrity": {"comparable": True, "reasons": []},
        "grade_db": 4.0,
    }
    restored = durable_state.attempt_history_from_state(
        {"attempts_loop": {"history": [legacy_row]}}
    )
    assert len(restored) == 1
    assert restored[0].sitting_id == ""


#
# These four tests pinned the OPPOSITE behaviour until the owner's 2026-08-03
# ruling (#2087): crossing the threshold refused the capture and reused
# ``low_alignment_confidence``. They are transformed rather than deleted, so
# every boundary the old gate was pinned at is still pinned — the threshold,
# its exclusive ``>``, and the trims-only skip all survive; only the
# consequence of crossing it changed from a refusal to a disclosure.


def test_measure_priors_thread_declared_delay_magnitudes_without_applied_target():
    """T2 threads declared magnitudes even before a target is applied.

    The reference preset declares [50, 300] us; Fix 3's 100 us margin makes
    [0, 400] us. ``delay_target_driver`` may legitimately be absent on a fresh
    preset; the drift-corrected physical peak gap later orients the signed
    lobe, so that must not disable T2.
    """
    c = _conductor(FakeSeams())
    expected = (0.0, 400.0)
    assert alignment_delay_search_bounds_us(_preset()) == expected
    assert c.measure_priors().alignment_delay_bounds_us == expected

    raw = _two_way_preset()
    raw["crossover_regions"][0]["delay_target_driver"] = None
    fresh = ActiveSpeakerPreset.from_mapping(raw)
    assert alignment_delay_search_bounds_us(fresh) == expected


def test_measure_priors_carry_the_applied_alignment_and_no_other_phase_does(
    monkeypatch,
):
    """The seam: the session reads Layer-A state, the analysis is handed it.

    Contract #4 — the analysis is a pure function of (program, WAV, priors) —
    is what makes this a prior rather than a read from inside the analyzer.
    And MEASURE is the only phase that commits an alignment, so it is the only
    phase told what the speaker already plays: handing it to VERIFY would put
    the current answer inside the comparison meant to be independent of it.
    """
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: {"timing": {"delay_us": 59.6, "polarity": "normal", "provenance": "authored_by_model"}},
    )
    c = _conductor(FakeSeams())

    applied = c.measure_priors().applied_alignment
    assert applied is not None and applied.delay_us == pytest.approx(59.6)
    for factory in (c.check_priors, c.lateral_priors):
        assert factory().applied_alignment is None, factory.__name__


def test_measure_priors_compose_configured_path_from_ssots_and_freeze_input():
    raw = _two_way_preset()
    raw["crossover_regions"][0]["upper_polarity"] = "inverted"
    preset = ActiveSpeakerPreset.from_mapping(raw)
    woofer = branch_chain.CrossoverSection(6000.0, 4, False)
    tweeter = branch_chain.CrossoverSection(300.0, 4, True)
    supplied = {"woofer": [woofer], "tweeter": [tweeter]}
    c = _conductor(
        FakeSeams(), source_preset=preset,
        measurement_protection_sections_by_role=supplied,
    )
    supplied["woofer"].clear()
    supplied["tweeter"] = [woofer]
    priors = c.measure_priors()
    # The measurement kernel may not import this package, so priors carry an
    # evaluated `freqs -> complex response` rather than CrossoverSections. The
    # transfer must still come from the sections the conductor copied at
    # construction, NOT from the caller's list mutated above.
    freqs = np.array([100.0, 1000.0, 8000.0])
    assert priors.measurement_protection_response_by_role.keys() == {
        "woofer", "tweeter",
    }
    for role, section in (("woofer", woofer), ("tweeter", tweeter)):
        np.testing.assert_allclose(
            priors.measurement_protection_response_by_role[role](freqs),
            crossover_response_complex(freqs, (section,)),
        )
    for role, sections in sections_by_role(preset.crossover_regions).items():
        np.testing.assert_allclose(
            priors.configured_crossover_response_by_role[role](freqs),
            crossover_response_complex(freqs, sections),
        )
    assert priors.configured_polarity_sign_by_role == {"woofer": 1, "tweeter": -1}
    # Pins the WIRING of §4.2's candidate-required mask: every role's band must
    # be derived, and must cover the overlap band it is unioned with (a None
    # derivation silently returns the policy to the whole driven band).
    overlap = overlap_band_hz(priors.crossover_fc_hz)
    required = priors.candidate_required_band_hz_by_role
    assert required is not None and required.keys() == {"woofer", "tweeter"}
    for role, (lo, hi) in required.items():
        assert lo <= overlap[0] and hi >= overlap[1], role
    legacy = _conductor(FakeSeams()).measure_priors()
    assert legacy.measurement_protection_response_by_role is None
    assert legacy.configured_crossover_response_by_role is None
    assert legacy.configured_polarity_sign_by_role is None
    assert legacy.candidate_required_band_hz_by_role is None


def test_measure_program_gains_back_off_from_caps():
    """W2 gate: the solver backs off ≥0.01 dB from exact per-driver caps."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    program = c.program_for_phase(PHASE_MEASURE)
    sweep_t = program.segment("sweep_t")
    # tweeter cap −65, session −20 ⇒ ceiling −45 − backoff.
    assert sweep_t.gain_db == pytest.approx(-45.0 - GAIN_CAP_BACKOFF_DB)
    assert sweep_t.effective_peak_dbfs <= CAPS["tweeter"] - GAIN_CAP_BACKOFF_DB + 1e-9
    # Woofer's solved gain is far under its cap and passes through unchanged.
    assert program.segment("sweep_w").gain_db == pytest.approx(-11.0)
    # MEASURE opens with the pilot pair riding the woofer's solved level.
    pilot_hi = program.segment("pilot_woofer_hi")
    assert pilot_hi.gain_db == pytest.approx(-11.0)
    assert program.segment("pilot_woofer_lo").gain_db == pytest.approx(-21.0)


def test_back_off_gain_at_cap():
    assert back_off_gain(-45.0, -20.0, -65.0) == pytest.approx(-45.01)
    assert back_off_gain(-50.0, -20.0, -65.0) == pytest.approx(-50.0)


def test_conductor_threads_geometry_and_result_to_analyze():
    """The declared driver spacing + prescribed 1 m mic distance reach the
    analyze seam (so the §3.2 parallax correction is live, not dead config),
    and the WHOLE CaptureResult crosses it (the production binding resolves
    the mic calibration from result.setup/device)."""
    from jasper.audio_measurement.program_analysis import MeasurementGeometry

    fakes = FakeSeams()
    c = _conductor(fakes)  # driver_spacing_m=0.15
    result = _capture()
    c.authorize_begin(1, 1)
    _run_phase(c, 1, 1, result)
    assert len(fakes.analyzed) == 1
    phase, _prog_phase, seen_result, _priors, geometry = fakes.analyzed[0]
    assert phase == PHASE_CHECK
    assert seen_result is result  # the CaptureResult itself, not just bytes
    assert isinstance(geometry, MeasurementGeometry)
    assert geometry.driver_spacing_m == pytest.approx(0.15)
    # This literal is the only tripwire for
    # ``crossover_v2_flow.MEASUREMENT_DISTANCE_M``, which nothing in the tree
    # imports — importing it here would make the assertion pass at any value.
    assert geometry.mic_distance_m == pytest.approx(1.0)
    assert geometry.parallax_us() > 0.0
