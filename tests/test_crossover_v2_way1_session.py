# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#3507 phase 1: the recommissioning session's shape on a 1-way passive main.

One amp channel, ``full_range_passive`` mains, no local subwoofer, no crossover
region. The session opens, walks CHECK -> MEASURE -> ENTRY_BASELINE, and
measures its plant with ONE routed solo of the whole speaker. Nothing here
grades a candidate: a 1-way declares no corner, delay, polarity or inter-branch
trim, so those axes do not exist rather than being defaulted.
"""

from __future__ import annotations

from typing import Any

import pytest

from jasper.active_speaker import commission_wiring, design_draft
from jasper.active_speaker import driver_safety as driver_safety_mod
from jasper.active_speaker import excitation_safety_plan as excitation_safety_plan_mod
from jasper.active_speaker import crossover_v2_flow
from jasper.active_speaker.crossover_v2 import capture_plan as _plan
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_MEASUREMENT_TARGETS_MISSING,
    REASON_REGISTRY,
    REASON_SPEAKER_SHAPE_UNSUPPORTED,
)
from jasper.audio_measurement.program import (
    VERIFY_PILOT_F_HI_HZ,
    VERIFY_PILOT_F_LO_HZ,
    build_verify_program,
)
from jasper.audio_measurement.program_analysis import (
    MEASURE_PAIR_SINGLE_DRIVER,
    MeasurementPriors,
    analyze_program_capture,
)
from jasper.output_topology import ACTIVE_PLAYBACK_DEVICE_ENV
from jasper.web import correction_crossover_v2 as v2host

from tests.active_speaker_fixtures import mono_output_topology
from tests.crossover_v2_fixtures import (
    WAY1_BAND,
    FakeSeams,
    _one_way_preset,
    _roles_way1,
    _way1_conductor,
)


def _way1_index_phase_map() -> dict[int, str]:
    return _plan.build_v2_cloud_index_phase_map(
        include_cloud_measure=False, include_entry_baseline=True,
    )


def test_the_way1_stage_one_walk_is_check_measure_then_the_entry_baseline():
    conductor = _way1_conductor(FakeSeams(), index_phase_map=_way1_index_phase_map())

    assert _way1_index_phase_map() == {
        1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_ENTRY_BASELINE,
    }
    phases = conductor.session_phases
    assert phases == (PHASE_CHECK, PHASE_MEASURE, PHASE_ENTRY_BASELINE)
    # The "before" is taken once, and immediately before apply.
    assert phases.count(PHASE_ENTRY_BASELINE) == 1
    assert phases[-1] == PHASE_ENTRY_BASELINE


def test_the_way1_entry_baseline_plays_the_verify_object_itself():
    """The plant/entry distinction survives with one driver.

    ENTRY_BASELINE is the summed sweep through the APPLIED graph, and its
    comparability with the post-apply VERIFY is object identity — the same
    ``program_id``, which is what ``evaluate_benefit`` compares on.
    """
    conductor = _way1_conductor(FakeSeams(), index_phase_map=_way1_index_phase_map())

    entry = conductor.program_for_phase(PHASE_ENTRY_BASELINE)
    verify = conductor.program_for_phase(PHASE_VERIFY)

    assert entry is verify
    assert entry.program_id == verify.program_id


def test_the_way1_measure_program_is_one_role_and_still_anchors_drift():
    conductor = _way1_conductor(
        FakeSeams(),
        index_phase_map=_way1_index_phase_map(),
        gain_plan_db={"full_range": -12.0},
    )

    measure = conductor.program_for_phase(PHASE_MEASURE)
    sweeps = [
        seg for seg in measure.segments
        if seg.segment_id.startswith("sweep_")
    ]

    assert {seg.role for seg in sweeps} == {"full_range"}
    assert measure.channels == 1
    # ``program_analysis`` anchors the drift estimator on this literal id, so a
    # 1-way's single role keeps the woofer spelling.
    assert measure.segment("sweep_w").role == "full_range"
    assert not any(seg.segment_id.startswith("sweep_t") for seg in measure.segments)
    # No inter-driver gap exists; the repeats are still separated by the MESM
    # settle that lets each sweep's tail decay.
    assert not any(seg.segment_id.startswith("gap_w_t") for seg in measure.segments)
    assert any(seg.segment_id.startswith("gap_w_w") for seg in measure.segments)


@pytest.mark.parametrize(
    "band_hz, expected_pilot",
    [
        # The fixed flat window, clamped INTO the declaration.
        pytest.param(
            (WAY1_BAND.lower_hz, WAY1_BAND.upper_hz),
            (VERIFY_PILOT_F_LO_HZ, VERIFY_PILOT_F_HI_HZ),
            id="clamped_into_the_declaration",
        ),
        # The clamp can collapse; the speaker's own band is then the answer —
        # never ``[fc/8, fc/4]``, which has no meaning with no corner.
        pytest.param((2000.0, 12000.0), (2000.0, 12000.0), id="declaration_is_its_own_hull"),
    ],
)
def test_the_way1_verify_pilot_band_comes_from_the_declaration(band_hz, expected_pilot):
    program = build_verify_program(
        None, measurement_band_hz=band_hz, leading_pilot_gains_db=(-30.0, -20.0),
    )
    pilots = [
        seg for seg in program.segments
        if seg.segment_id.startswith("pilot_summed_")
    ]

    assert pilots
    assert {seg.f1_hz for seg in pilots} == {expected_pilot[0]}
    assert {seg.f2_hz for seg in pilots} == {expected_pilot[1]}
    # The sweep's low edge widens off the declaration the same way a corner
    # widens it.
    assert program.segment("sweep_verify").f1_hz == pytest.approx(
        min(150.0, band_hz[0])
    )


def test_a_no_crossover_verify_needs_the_declaration_it_derives_from():
    with pytest.raises(ValueError):
        build_verify_program(None)


def test_a_subless_passive_topology_resolves_its_own_one_way_preset(monkeypatch):
    """Never the bundled 2-way JSON.

    That fallback is the worst outcome this route closes: it opened a session
    naming a woofer and a tweeter over a box that has neither. Driven through
    ``resolve_capture_preset`` — the entry every capture-analysis surface
    actually calls — which answers a passive box without ever reading the
    design draft or the saved preview off disk.
    """
    from jasper.active_speaker.tone_plan import load_active_speaker_preset

    topology = mono_output_topology(mode="full_range_passive")
    monkeypatch.setattr(
        commission_wiring, "resolve_commission_inputs",
        lambda: pytest.fail("a passive box must not read the preview inputs"),
    )

    preset = commission_wiring.resolve_capture_preset(topology)

    assert preset.way_count == 1
    assert preset.crossover_regions == ()
    assert preset.local_subwoofer is None
    assert set(preset.drivers) == {"full_range"}
    assert preset.preset_id != load_active_speaker_preset().preset_id


@pytest.fixture
def _passive_session_inputs(monkeypatch):
    """Every conductor-context input except the shape under test.

    Mirrors ``tests/test_correction_crossover_v2_conductor_context.py``'s own
    stub set: the driver-safety contract and the volume derivation are covered
    there, and stubbing them keeps this module on the 1-way shape.
    """
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.test")
    monkeypatch.setattr(
        design_draft, "load_design_draft",
        lambda **kw: {"driver_safety_profile": {"targets": [
            {"role": "full_range", "target_fingerprint": "fp-full_range"},
        ]}},
    )
    monkeypatch.setattr(
        driver_safety_mod, "evaluate_driver_safety_profile",
        lambda profile, topology: driver_safety_mod.DriverSafetyProfileEvaluation(
            "confirmed", True, "f" * 64, (),
        ),
    )
    monkeypatch.setattr(
        excitation_safety_plan_mod, "resolve_driver_excitation_ceilings",
        lambda safety_profile, fingerprint, **kw: (WAY1_BAND, 90.0),
    )
    monkeypatch.setattr(
        excitation_safety_plan_mod, "effective_sweep_duration_limit_s",
        lambda safety_profile, fingerprint: 6.0,
    )
    monkeypatch.setattr(
        crossover_v2_flow, "derive_session_volume_db",
        lambda safety_profile, fps, **kw: -20.0,
    )
    monkeypatch.setenv(ACTIVE_PLAYBACK_DEVICE_ENV, "hw:Lab")
    yield


def _passive_status(topology) -> dict[str, Any]:
    """The status a passive box really serves, from the REAL derivation.

    Not a hand-built target: the production path derives ``targets.drivers``
    from ``measurement.active_driver_targets``, and until #3507 that filter
    admitted only ``active_2_way``/``active_3_way`` groups — so a fabricated
    target would have hidden the one gate a real passive box actually hits.
    """
    from jasper.active_speaker.measurement import (
        active_driver_targets,
        active_summed_targets,
    )

    drivers = active_driver_targets(topology)
    summed = active_summed_targets(topology)
    return {
        # Derived exactly as ``correction_crossover_backend.status_payload``
        # derives it: a summed target is a claim about two branches, so a
        # passive box is still honestly "no active crossover".
        "active": bool(summed),
        "targets": {"drivers": drivers, "summed": summed},
    }


def _patch_topology(monkeypatch, topology) -> None:
    from jasper import output_topology as output_topology_mod

    monkeypatch.setattr(
        output_topology_mod, "load_output_topology", lambda *a, **k: topology,
    )


def test_a_subless_passive_speaker_opens_a_session(
    monkeypatch, _passive_session_inputs,
):
    """The three gates that refused a passive box are answered by its shape.

    ``status["active"]`` is False and there is no ``setup`` block at all — both
    are questions about an ACTIVE crossover, which this speaker does not have.
    """
    topology = mono_output_topology(mode="full_range_passive")
    _patch_topology(monkeypatch, topology)
    # A passive box compiles no crossover preview; asking for one would refuse
    # a session it cannot serve.
    monkeypatch.setattr(
        v2host, "ensure_crossover_preview_ready",
        lambda: pytest.fail("a passive topology must not need a crossover preview"),
    )

    context = v2host.resolve_conductor_context(_passive_status(topology))

    assert context.preset.way_count == 1
    assert context.fc_hz is None
    assert [rb.role for rb in context.roles_bands] == ["full_range"]
    assert context.role_channels == {"full_range": 0}
    assert set(context.driver_caps_dbfs) == {"full_range"}
    assert set(context.measurement_band_hz_by_role) <= {"full_range"}


def test_an_unsupported_shape_refuses_with_a_registered_code(
    monkeypatch, _passive_session_inputs,
):
    topology = mono_output_topology(mode="full_range_passive")
    _patch_topology(monkeypatch, topology)
    monkeypatch.setattr(v2host, "ensure_crossover_preview_ready", lambda: None)

    from tests.test_active_speaker_profile import _three_way_preset
    from jasper.active_speaker.profile import ActiveSpeakerPreset

    monkeypatch.setattr(
        commission_wiring, "resolve_capture_preset",
        lambda topo: ActiveSpeakerPreset.from_mapping(_three_way_preset("mono")),
    )

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.resolve_conductor_context(_passive_status(topology))

    assert excinfo.value.code == REASON_SPEAKER_SHAPE_UNSUPPORTED
    assert REASON_SPEAKER_SHAPE_UNSUPPORTED in REASON_REGISTRY


def test_the_session_refuses_one_declaration_it_cannot_name_a_target_for(
    monkeypatch, _passive_session_inputs,
):
    topology = mono_output_topology(mode="full_range_passive")
    _patch_topology(monkeypatch, topology)
    monkeypatch.setattr(v2host, "ensure_crossover_preview_ready", lambda: None)
    status = _passive_status(topology)
    status["targets"] = {"drivers": [], "summed": []}

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.resolve_conductor_context(status)

    assert excinfo.value.code == REASON_MEASUREMENT_TARGETS_MISSING
    assert REASON_MEASUREMENT_TARGETS_MISSING in REASON_REGISTRY


def test_a_one_way_session_never_aliases_its_missing_upper_driver():
    conductor = _way1_conductor(FakeSeams(), index_phase_map=_way1_index_phase_map())

    assert conductor._tweeter is None
    assert conductor._woofer.role == "full_range"


def test_a_three_role_session_is_still_refused():
    fakes = FakeSeams()
    with pytest.raises(crossover_v2_flow.CrossoverV2FlowError):
        crossover_v2_flow.CrossoverV2Session(
            session_id="cap_way1_three",
            source_preset=_one_way_preset(),
            roles_bands=_roles_way1() * 3,
            fc_hz=None,
            driver_caps_dbfs={"full_range": 0.0},
            session_volume_db=-20.0,
            seams=fakes.seams(),
        )


def test_a_way1_measure_capture_banks_the_solo_and_publishes_no_candidate():
    """The full walk's middle: analysis and verdict, end to end.

    Nothing here is faked past the microphone — a real one-role MEASURE program
    is rendered, convolved with a synthetic full-range IR, and put through
    ``analyze_program_capture``. The alignment and the candidate come back
    ABSENT WITH A NAME; the solo's own evidence (its response, its repeat drift)
    is what the round banks.
    """
    from tests.test_audio_measurement_program_analysis import (
        SR,
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

    analysis = analyze_program_capture(program, capture, SR, priors=MeasurementPriors())

    # The solo IS the evidence: one role, its first occurrence, and the drift
    # the repeated ``sweep_w`` cycles still estimate.
    assert [r.role for r in analysis.driver_responses] == ["full_range"]
    assert analysis.drift is not None
    # Absent BY NAME, never a bare None a reader could take for "measured, fine".
    assert analysis.alignment is None
    assert analysis.candidate is None
    assert analysis.measure_pair_not_evaluated == MEASURE_PAIR_SINGLE_DRIVER
    assert analysis.predicted_sum is None

    verdict = conductor._measure_verdict(analysis)

    assert verdict.accepted is True
    assert verdict.code is None
    assert verdict.payload["measurement_phase"] == PHASE_MEASURE
    assert verdict.payload["candidate"] == {
        "status": "not_evaluated",
        "reason": MEASURE_PAIR_SINGLE_DRIVER,
    }


def test_the_candidate_door_itself_still_refuses_one_role():
    """The Phase-2b door, closed rather than degraded.

    The walk above never reaches it (the verdict publishes a named absence
    instead), so this is the belt: were a caller to drive the fit directly it is
    refused, not handed a single-branch lookalike of a two-branch answer.
    """
    conductor = _way1_conductor(FakeSeams(), index_phase_map=_way1_index_phase_map())

    with pytest.raises(crossover_v2_flow.CrossoverV2FlowError):
        conductor._build_candidate(object())


def test_the_one_way_preset_emits_a_protected_neutral_program_graph(tmp_path):
    """The plant graph: one program channel routed to the one physical output.

    The tweeter protection proof is ABSENT rather than waived — there is no
    branch here a high-pass could be the protection of.
    """
    from jasper.active_speaker.branch_chain import CrossoverSection
    from jasper.active_speaker.camilla_yaml import (
        emit_active_speaker_program_config,
    )

    out = tmp_path / "program.yml"
    text = emit_active_speaker_program_config(
        _one_way_preset(),
        role_channels={"full_range": 0},
        playback_device="hw:CARD=DAC8,DEV=0",
        protection_sections_by_role={"full_range": (
            CrossoverSection(fc_hz=30.0, order=2, highpass=True),
        )},
        out_path=out,
    )

    assert "filter_mode=protected_neutral" in text
    assert "role_channels={'full_range': 0}" in text
    assert "program_channels=1" in text
