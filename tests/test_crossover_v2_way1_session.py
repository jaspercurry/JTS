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

from tests.active_speaker_fixtures import isolated_candidate_bank as isolated_candidate_bank

import shlex
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.usefixtures("isolated_candidate_bank")

import yaml as yaml_lib

from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker import (
    commission_wiring,
    crossover_v2_flow,
)
from jasper.active_speaker.branch_chain import branch_headroom_db
from jasper.active_speaker.crossover_v2 import capture_plan as _plan
from jasper.active_speaker.crossover_v2.intervention import DriverEvidence, fit_branches
from jasper.active_speaker.crossover_v2.planning import analysis_json
from jasper.active_speaker.linearization_fit import FitVocabulary
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate
from jasper.active_speaker.crossover_v2.contracts import (
    LINEARIZATION_OUTCOME_SINGLE_BRANCH,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
)
from jasper.audio_measurement.program_analysis import (
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
        include_entry_baseline=True,
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
    assert conductor.roles_bands[0].role == "full_range"


def test_a_three_role_session_is_still_refused():
    with pytest.raises(contracts.CrossoverV2FlowError):
        crossover_v2_flow.CrossoverV2Session(
            session_id="cap_way1_three",
            source_preset=_one_way_preset(),
            roles_bands=_roles_way1() * 3,
            fc_hz=None,
            driver_caps_dbfs={"full_range": 0.0},
            session_volume_db=-20.0,
            seams=FakeSeams().seams(),
        )


# measure, fit, compile


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

    from jasper.active_speaker.crossover_v2.capture_dispatch import assess
    verdict = assess(analysis, phase=PHASE_MEASURE, program=program)

    assert verdict.ok is True
    assert verdict.fault is None
    assert analysis.phase == PHASE_MEASURE
    assert (analysis.alignment, analysis.measure_pair_not_evaluated) == (None, MEASURE_PAIR_SINGLE_DRIVER)


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
    from tests.apply_fixtures import prepare_candidate
    from tests.active_speaker_fixtures import (
        passive_stereo_output_topology,
    )

    topology = passive_stereo_output_topology()
    conductor = _way1_conductor(
        FakeSeams(),
        index_phase_map=_way1_index_phase_map(),
        gain_plan_db={"full_range": -11.0},
        source_preset=commission_wiring.resolve_capture_preset(topology),
    )
    candidate = _way1_candidate(conductor,
        _way1_measure_analysis(conductor.program_for_phase(PHASE_MEASURE))
    )

    return prepare_candidate(candidate, topology, tmp_path / "active_speaker_baseline.yml")


def test_a_way1_round_compiles_and_writes_a_single_branch_baseline(tmp_path):
    """The whole Phase-2 loop, end to end: banked solo -> profile on disk.

    Non-negotiable tier (hearing): ceiling, headroom charge and per-branch
    limiter, asserted structurally on a profile carrying a real fitted
    linearization, so a way-1 apply cannot ship a chain whose limiter was
    dropped with the crossover it never had.
    """
    payload = _way1_ready_to_apply_payload(tmp_path)

    config = yaml_lib.safe_load(
        Path(payload["config"]["path"]).read_text(encoding="utf-8")
    )

    assert config["devices"]["volume_limit"] == 0.0
    assert "active_baseline_headroom" in config["filters"]
    assert list(config["mixers"]) == ["split_active_1way"]
    branch = next(
        step["names"] for step in config["pipeline"]
        if step.get("type") == "Filter"
        and "as_full_range_baseline_gain" in step["names"]
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


# grading

#: The band the fixture round commands a boost in, and how much. Inside the
#: capture's own trusted span so the probe grades it, and narrow enough that the
#: rest of the span stays quiet.


def _way1_candidate(conductor, analysis):
    """The solo's fit, charged into its own chain, at a fixed 0 dB: a lone
    branch has no pair to trim."""
    (response,) = analysis.driver_responses
    sweep = conductor.program_for_phase(PHASE_MEASURE).segment("sweep_w")
    fit = fit_branches(
        [DriverEvidence("full_range", response, (sweep.f1_hz, sweep.f2_hz))],
        mic_tiers={"full_range": str(analysis.mic_tier)},
        vocabulary=FitVocabulary(allow_boost=True), sections={},
    ).fits["full_range"]
    charge_db = branch_headroom_db([f.to_dict() for f in fit.filters])
    return MeasuredCrossoverCandidate(
        program_id=analysis.program_id,
        analysis=analysis_json(analysis),
        source_preset=conductor.source_preset,
        role_attenuations_db={"full_range": 0.0},
        linearization={"full_range": replace(fit, headroom_cost_db=charge_db).to_dict()},
        linearization_outcome=LINEARIZATION_OUTCOME_SINGLE_BRANCH,
    )
