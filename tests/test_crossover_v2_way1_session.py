# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The recommissioning session's walk on a 1-way passive main.

One amp channel, ``full_range_passive`` mains, no local subwoofer, no crossover
region: the session opens, measures its plant with ONE routed solo, fits and
compiles a single-branch baseline, and grades the result. A 1-way declares no
corner, delay, polarity or inter-branch trim, so those axes are absent by name
rather than defaulted.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import numpy as np
import pytest
import yaml as yaml_lib

from jasper.active_speaker import (
    commission_wiring,
    crossover_v2_flow,
)
from jasper.active_speaker.crossover_v2 import capture_plan as _plan
from jasper.active_speaker.crossover_v2.contracts import (
    LINEARIZATION_OUTCOME_SINGLE_BRANCH,
    TrimStrategy,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.proposal import trim_strategy_for_outcome
from jasper.active_speaker.delta_probe import VERDICT_MATCHED, VERDICT_MODEL_ERROR
from jasper.audio_measurement.program_analysis import (
    ABSOLUTE_NO_CROSSOVER_TOPOLOGY,
    MEASURE_PAIR_SINGLE_DRIVER,
    MeasurementPriors,
    analyze_program_capture,
)

from tests.crossover_v2_fixtures import (
    WAY1_BAND,
    FakeSeams,
    _one_way_preset,
    _roles_way1,
    _way1_conductor,
    _way1_measure_analysis,
)


def _way1_index_phase_map() -> dict[int, str]:
    return _plan.build_v2_cloud_index_phase_map(
        include_cloud_measure=False, include_entry_baseline=True,
    )


def test_the_way1_stage_one_walk_names_one_role_and_ends_on_the_entry_baseline():
    conductor = _way1_conductor(FakeSeams(), index_phase_map=_way1_index_phase_map())

    assert _way1_index_phase_map() == {
        1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_ENTRY_BASELINE,
    }
    phases = conductor.session_phases
    assert phases == (PHASE_CHECK, PHASE_MEASURE, PHASE_ENTRY_BASELINE)
    # The "before" is taken once, and immediately before apply.
    assert phases.count(PHASE_ENTRY_BASELINE) == 1
    # The missing upper driver is absent, never aliased onto the lone branch.
    assert conductor._tweeter is None
    assert conductor._woofer.role == "full_range"


def test_a_three_role_session_is_still_refused():
    with pytest.raises(crossover_v2_flow.CrossoverV2FlowError):
        crossover_v2_flow.CrossoverV2Session(
            session_id="cap_way1_three",
            source_preset=_one_way_preset(),
            roles_bands=_roles_way1() * 3,
            fc_hz=None,
            driver_caps_dbfs={"full_range": 0.0},
            session_volume_db=-20.0,
            seams=FakeSeams().seams(),
        )


# --------------------------------------------------------------------------- #
# measure, fit, compile
# --------------------------------------------------------------------------- #


def test_a_way1_measure_capture_banks_the_solo_and_names_the_pair_it_skipped():
    """The full walk's middle: nothing is faked past the microphone — a real
    one-role MEASURE program, convolved with a synthetic full-range IR and put
    through ``analyze_program_capture``."""
    from tests.test_audio_measurement_program_analysis import (
        SR,
        _ambient,
        _band_impulse,
        _synthesize,
    )

    conductor = _way1_conductor(
        FakeSeams(),
        index_phase_map=_way1_index_phase_map(),
        gain_plan_db={"full_range": -11.0},
    )
    program = conductor.program_for_phase(PHASE_MEASURE)
    ir = _band_impulse(200, WAY1_BAND.lower_hz, WAY1_BAND.upper_hz, 1.0)
    capture = _synthesize(program, woofer_ir=ir, tweeter_ir=ir)

    analysis = analyze_program_capture(
        program, capture, SR, priors=MeasurementPriors(ambient_report=_ambient()),
    )

    assert [r.role for r in analysis.driver_responses] == ["full_range"]
    assert analysis.drift is not None
    # Absent BY NAME, never a bare None a reader could take for "measured, fine".
    assert analysis.measure_pair_not_evaluated == MEASURE_PAIR_SINGLE_DRIVER
    assert analysis.alignment is None
    assert analysis.candidate is None
    # The corner only ever BOUNDS the SNR window, so a branch with none is still
    # judged — over what it radiated.
    solo = next(r for r in analysis.driver_responses if r.role == "full_range")
    sweep = program.segment("sweep_w")
    assert solo.snr is not None
    assert solo.snr["relevant_hz"] == [sweep.f1_hz, sweep.f2_hz]
    assert solo.snr["verdict"] == "ok"
    # ``predicted_sum`` is NOT one of the absences: one branch sums to itself,
    # and the delta probe's state axis references it.
    assert analysis.predicted_sum is not None
    predicted_hz, predicted_db = analysis.predicted_sum
    in_band = (predicted_hz >= WAY1_BAND.lower_hz) & (
        predicted_hz <= WAY1_BAND.upper_hz
    )
    np.testing.assert_allclose(
        np.interp(predicted_hz[in_band], solo.freqs_hz, solo.magnitude_db),
        predicted_db[in_band],
        atol=0.5,
    )

    verdict = conductor._measure_verdict(analysis)

    assert verdict.accepted is True
    assert verdict.code is None
    assert verdict.payload["measurement_phase"] == PHASE_MEASURE
    assert verdict.payload["pair"] == {
        "status": "not_evaluated",
        "reason": MEASURE_PAIR_SINGLE_DRIVER,
    }
    # A single-branch prescription IS published, and it covers the one role.
    assert verdict.payload["candidate_fingerprint"]
    assert conductor._candidate.role_attenuations_db == {"full_range": 0.0}


def test_the_way1_candidate_carries_the_fit_and_no_inter_driver_axis():
    """Driven through the same ``_build_candidate`` the 2-way walk uses."""
    conductor = _way1_conductor(
        FakeSeams(),
        index_phase_map=_way1_index_phase_map(),
        gain_plan_db={"full_range": -11.0},
    )
    analysis = _way1_measure_analysis(conductor.program_for_phase(PHASE_MEASURE))

    candidate, state = conductor._build_candidate(analysis)

    # The fit RAN, and says so in the shape's own word rather than the pair's,
    # so the proposal cannot map it onto a committed-pair trim strategy.
    assert state.outcome == LINEARIZATION_OUTCOME_SINGLE_BRANCH
    assert candidate.linearization_outcome == LINEARIZATION_OUTCOME_SINGLE_BRANCH
    assert trim_strategy_for_outcome(candidate.linearization_outcome)[0] is (
        TrimStrategy.NO_PAIR_TO_TRIM
    )
    assert candidate.role_attenuations_db == {"full_range": 0.0}
    assert set(candidate.linearization) == {"full_range"}
    assert candidate.linearization["full_range"]["filters"]
    # Every inter-driver verdict is absent, not defaulted.
    assert state.realized_level_match is None
    assert state.level_consistency is None
    assert state.trim_band_estimate_db == {}
    assert state.polish_delta_db == {}
    assert candidate.alignment.delay_us is None
    assert candidate.alignment.polarity is None


def test_the_one_way_preset_emits_a_protected_neutral_program_graph():
    """One program channel to the one physical output; the tweeter protection
    proof is ABSENT rather than waived — no branch here is what it protects."""
    from jasper.active_speaker.branch_chain import CrossoverSection
    from jasper.active_speaker.camilla_yaml import emit_active_speaker_program_config

    config = yaml_lib.safe_load(emit_active_speaker_program_config(
        _one_way_preset(),
        role_channels={"full_range": 0},
        playback_device="hw:CARD=DAC8,DEV=0",
        protection_sections_by_role={
            "full_range": (CrossoverSection(fc_hz=30.0, order=2, highpass=True),),
        },
    ))

    assert config["devices"]["capture"]["channels"] == 1
    assert config["devices"]["volume_limit"] == 0.0
    assert [
        entry["dest"] for entry in config["mixers"]["split_active_1way"]["mapping"]
    ] == [0]


def _way1_ready_to_apply_payload(tmp_path):
    """One way-1 round's banked solo, compiled to a ready-to-apply profile.

    The shape is the subless passive main PAIR: its mono sibling declares one
    physical output and the active ring's accept-set starts at two.
    """
    from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate
    from tests.active_speaker_fixtures import (
        passive_stereo_output_topology,
        valid_camilla_config,
    )

    topology = passive_stereo_output_topology()
    conductor = _way1_conductor(
        FakeSeams(),
        index_phase_map=_way1_index_phase_map(),
        gain_plan_db={"full_range": -11.0},
        source_preset=commission_wiring.resolve_capture_preset(topology),
    )
    candidate, state = conductor._build_candidate(
        _way1_measure_analysis(conductor.program_for_phase(PHASE_MEASURE))
    )
    assert state.outcome == LINEARIZATION_OUTCOME_SINGLE_BRANCH

    return build_baseline_profile_candidate(
        topology,
        # A passive box saves no crossover preview and completes no summed
        # active-crossover validation; both are two-branch artifacts.
        design_draft={},
        crossover_preview={},
        measurements={},
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=valid_camilla_config,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )


def test_a_way1_round_compiles_and_writes_a_single_branch_baseline(tmp_path):
    """The whole Phase-2 loop, end to end: banked solo -> profile on disk.

    Non-negotiable tier (hearing): ceiling, headroom charge and per-branch
    limiter, asserted structurally on a profile carrying a real fitted
    linearization, so a way-1 apply cannot ship a chain whose limiter was
    dropped with the crossover it never had.
    """
    payload = _way1_ready_to_apply_payload(tmp_path)

    assert payload["status"] == "ready_to_apply"
    assert payload["permissions"]["may_apply"] is True
    config = yaml_lib.safe_load(
        Path(payload["config"]["path"]).read_text(encoding="utf-8")
    )

    assert config["devices"]["volume_limit"] == 0.0
    assert "active_baseline_headroom" in config["filters"]
    assert list(config["mixers"]) == ["split_active_1way"]
    branch = next(
        step["names"] for step in config["pipeline"]
        if step.get("type") == "Filter"
        and "active_baseline_headroom" not in step["names"]
    )
    assert [n for n in branch if n.endswith("_baseline_limiter")] == [
        "as_full_range_baseline_limiter"
    ]
    assert branch[-1] == "as_full_range_baseline_limiter"
    # No crossover: nothing in the graph is a high- or low-pass section.
    assert not [
        name for name, spec in config["filters"].items()
        if spec.get("type") == "BiquadCombo"
    ]
    # The fit's filters sit ahead of the branch gain, where the chain charges
    # them, not after it.
    fitted = [name for name in branch if "_linearization_" in name]
    assert fitted
    assert branch.index(fitted[-1]) < branch.index("as_full_range_baseline_gain")


def test_a_way1_apply_banks_no_base_trim_and_says_which_fact_stopped_it(
    tmp_path, caplog, monkeypatch
):
    """A base trim is a FRAME, so a lone branch has nothing to bank.

    The seam names the TOPOLOGY fact with a standing-bank result, not one of the
    evidence arms below it, which describe a round that went wrong and send an
    operator to re-measure for a frame that cannot exist.
    """
    import logging

    from jasper.active_speaker import baseline_profile as baseline_profile_mod
    from jasper.active_speaker import driver_base_trim as dbt

    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(tmp_path / "driver_base_trim.json"))
    caplog.set_level(logging.INFO, logger=baseline_profile_mod.logger.name)

    baseline_profile_mod.persist_applied_baseline_profile(
        _way1_ready_to_apply_payload(tmp_path),
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    events = [
        dict(token.partition("=")[::2] for token in shlex.split(message))
        for message in caplog.messages
        if "event=dsp.baseline_base_trim_banked" in message
    ]
    assert [event["result"] for event in events] == ["left_standing"]
    assert events[0]["reason"] == dbt.REFUSE_NO_FRAME
    assert dbt.load_base_trim() is None


# --------------------------------------------------------------------------- #
# grading
# --------------------------------------------------------------------------- #

#: The band the fixture round commands a boost in, and how much. Inside the
#: capture's own trusted span so the probe grades it, and narrow enough that the
#: rest of the span stays quiet.
_BOOST_HZ = (2000.0, 4000.0)
_BOOST_DB = 3.0


def _boost_db(freqs_hz) -> np.ndarray:
    hz = np.asarray(freqs_hz, dtype=float)
    return np.where((hz >= _BOOST_HZ[0]) & (hz <= _BOOST_HZ[1]), _BOOST_DB, 0.0)


def _way1_round_through_verify():
    """One way-1 round driven to the far side of VERIFY on a REAL capture.

    Both axes come from the session's own owners over a one-role MEASURE
    analysis; the tracking curve and trusted band from ``_consume_verify``. The
    applied graph asks for :data:`_BOOST_DB` across :data:`_BOOST_HZ` and the
    speaker delivers it, so the probe runs where a shipped round runs it.
    """
    from jasper.active_speaker.crossover_v2.priors import (
        measure_sweep_bounds,
        verify_priors,
    )
    from tests.crossover_v2_round_harness import _consume_verify
    from tests.test_audio_measurement_program_analysis import (
        SR,
        _band_impulse,
        _synthesize,
    )

    # The Layer-A profile this speaker's PREVIOUS round emitted: one role, one
    # gain, no delay and no relative polarity.
    applied_profile = {
        "status": "applied",
        "recomposition_snapshot": {
            "preset": _one_way_preset().to_dict(),
            "corrections": {"full_range": {"gain_db": 0.0, "inverted": False}},
            "linearization": {},
        },
    }
    conductor = _way1_conductor(
        FakeSeams(applied_profile_state=applied_profile),
        index_phase_map=_way1_index_phase_map(),
        gain_plan_db={"full_range": -11.0},
    )
    measure_program = conductor.program_for_phase(PHASE_MEASURE)
    measure = _way1_measure_analysis(measure_program)
    raw_hz, raw_db = measure.predicted_sum
    applied = (raw_hz, np.asarray(raw_db, dtype=float) + _boost_db(raw_hz))
    conductor._measure_commanded_delta = conductor._commanded_delta_for(
        measure, applied, None,
    )
    conductor._measure_declared_transfer = conductor._declared_transfer_for(
        measure, applied,
    )

    program = conductor.program_for_phase(PHASE_VERIFY)
    ir = _band_impulse(200, WAY1_BAND.lower_hz, WAY1_BAND.upper_hz, 1.0)
    capture = _synthesize(program, woofer_ir=ir, tweeter_ir=ir)

    def _analyze(predicted_sum):
        return analyze_program_capture(program, capture, SR, priors=verify_priors(
            fc_hz=None, source_preset=_one_way_preset(),
            predicted_sum=predicted_sum,
            sweep_bounds=measure_sweep_bounds(measure_program),
        ))

    # The prediction the applied graph is graded against is this capture's own
    # summed response, read off a first pass — a speaker that did exactly what
    # was modelled, which is the only state that reaches the probe at all.
    summed = _analyze(None).summed_response
    analysis = _analyze((summed.freqs_hz, summed.magnitude_db))
    return conductor, analysis, _consume_verify(conductor, analysis)


def test_a_way1_verify_capture_grades_through_the_shipped_path():
    """The whole chain, on a speaker with one branch: a way-1 round is GRADED
    rather than reporting unavailable, which is neither a rollback nor a
    permission."""
    conductor, analysis, verdict = _way1_round_through_verify()

    assert verdict.accepted is True
    assert analysis.verify_tracking_curve is not None
    # VERIFY's no-crossover mode widens its sweep down to the declaration, below
    # the floor MEASURE excited the lone branch at, where the prediction is
    # deconvolution noise. The graded band is the intersection, on both edges.
    measure_sweep = conductor.program_for_phase(PHASE_MEASURE).segment("sweep_w")
    verify_sweep = conductor.program_for_phase(PHASE_VERIFY).segment("sweep_verify")
    assert verify_sweep.f1_hz < measure_sweep.f1_hz
    assert verify_sweep.f2_hz > measure_sweep.f2_hz
    assert analysis.verify_tracking["tracking_band_hz"] == [
        measure_sweep.f1_hz, measure_sweep.f2_hz,
    ]
    # R18 declines by SHAPE, never by a missing corner.
    assert analysis.verify_absolute == {
        "not_evaluated": ABSOLUTE_NO_CROSSOVER_TOPOLOGY
    }
    # Both axes were built, so the probe classified rather than reporting
    # unavailable, and it graded the band the round actually commanded in.
    probe = conductor._delta_probe
    assert probe is not None
    assert probe.verdict == VERDICT_MATCHED
    assert probe.probe_band_hz[0] == pytest.approx(_BOOST_HZ[0], rel=0.05)
    assert probe.probe_band_hz[1] == pytest.approx(_BOOST_HZ[1], rel=0.05)


def test_a_way1_probe_that_contradicts_its_claim_is_graded_not_excused():
    """The round commands a boost and the speaker delivers a cut instead.

    Only the MEASURED half of the tracking curve is displaced — the grid, the
    predicted curve, the trusted band and both axes stay what the run above
    produced — so this is the same round with a different speaker in it.
    """
    conductor, _analysis, _verdict = _way1_round_through_verify()
    freqs, measured_db, predicted_db = conductor._verify_tracking_curve
    conductor._verify_tracking_curve = (
        freqs, measured_db - 2.0 * _boost_db(freqs), predicted_db,
    )

    probe = conductor._run_delta_probe()

    assert probe is not None
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.max_error_db == pytest.approx(2.0 * _BOOST_DB, abs=0.1)
    # A SHORTFALL, not an overshoot.
    assert probe.realized_louder_than_commanded is False
