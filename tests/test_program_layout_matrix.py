# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
from types import SimpleNamespace

import pytest

from jasper.active_speaker.angle_capture import LevelPolicy, request_for_program
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.commission_wiring import resolve_capture_preset
from jasper.active_speaker.crossover_v2.measure_spec import branch_channels_for
from jasper.active_speaker.crossover_v2.programs import SessionExcitation
from jasper.active_speaker.excitation_safety_plan import (
    effective_sweep_duration_limit_s, resolve_driver_excitation_ceilings,
)
from jasper.active_speaker.measurement_emit import (
    MeasurementGraphProfile, compile_tuning_graph, emit_measurement_graph, measurement_graph_evidence,
)
from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS, load_programs, run_program
from jasper.active_speaker.plan_run import prepare_plan_captures
from jasper.active_speaker.preflight import preflight
from jasper.active_speaker.profile import ActiveSpeakerPreset, required_driver_roles
from jasper.active_speaker.program_admission import readmit_program_from_wav, readmit_summed_program_from_wav
from jasper.audio_measurement.program import BASE_STIMULUS_PEAK_DBFS, RoleBand, write_program_wav
from jasper.web.correction_run_host import compose_plan_program
from tests.test_active_speaker_excitation_safety_plan import _profile_and_targets as _three_way_safety
from tests.test_active_speaker_profile import _three_way_preset
from tests.test_active_speaker_program_admission import _profile_and_targets
from tests.test_crossover_v2_tuning_scope import _trial_candidate
from tests.test_preflight import ready_facts
from tests.test_rear_output_foundation import _rear_document, _rear_pair

LAYOUTS = ('one_way_passive', 'two_way_active', 'three_way_active', 'cardioid')
ROWS = {f'{name}/{size}': row for (name, size), row in load_programs().items()
        if row.purpose in RUNNABLE_PROGRAMS}
LEVEL_DB = -23.0
SENSITIVITIES = {'woofer': 84.0, 'tweeter': 109.2, 'mid': 90.0, 'full_range': 87.0}
PLAN_REFUSALS = {
    'one_way_passive': {'branches/express', 'front_rear/express', 'rear/express', 'rear/wide', 'rear/behind', 'rear/pair', 'rear/pair_behind'},
    'two_way_active': {'front_rear/express', 'rear/express', 'rear/wide', 'rear/behind', 'rear/pair', 'rear/pair_behind'},
    'three_way_active': {'branches/express', 'front_rear/express', 'rear/express', 'rear/wide', 'rear/behind', 'rear/pair', 'rear/pair_behind'},
    'cardioid': set(),
}
KNOWN_GAPS = {
    ('three_way_active', row): (
        {('graph_error', 'ActiveSpeakerConfigError', 'check', 'drivers'),
         ('compose_error', 'ValueError', 'measure', 'drivers')},
        'Remove when the neutral graph supports three ways and MEASURE accepts more than two drivers.',
    )
    for row in ('speaker/mark', 'baseline/express', 'baseline/full', 'tournament/express', 'tournament/full')
}


@pytest.fixture(scope='module', params=LAYOUTS)
def speaker(request, tmp_path_factory):
    name = request.param
    if name == 'three_way_active':
        topology, safety, targets = _three_way_safety(
            mode='active_3_way', woofer_peak=0, mid_peak=0, tweeter_peak=-65,
            hard_band=[40, 20000], measurement_band=[60, 10000],
        )
        targets = {role: target['target_fingerprint'] for role, target in targets.items()}
        preset = ActiveSpeakerPreset.from_mapping(_three_way_preset('mono'))
    else:
        topology, safety, targets = _profile_and_targets(
            rear=name == 'cardioid', passive=name == 'one_way_passive',
            woofer_floor=40, woofer_measurement_floor=60, woofer_highpass=40,
            woofer_upper=20000 if name == 'one_way_passive' else 4000, max_sweep_duration_s=4,
        )
        preset = _rear_pair('mono')[0] if name == 'cardioid' else resolve_capture_preset(topology)
    roles = required_driver_roles(preset.way_count)
    bands, caps, durations = [], {}, {}
    for channel, role in enumerate(roles):
        band, caps[role] = resolve_driver_excitation_ceilings(
            safety, targets[role], program_admission=True, declared_sensitivities=SENSITIVITIES,
        )
        bands.append(RoleBand(role, channel, band))
        durations[role] = effective_sweep_duration_limit_s(safety, targets[role])
    profile = MeasurementGraphProfile(
        preset, topology, {role: channel for channel, role in enumerate(roles)}, 'hw:CARD=DAC8x,DEV=0',
        protection_sections_by_role=confirmed_protection_sections(safety, targets),
    )
    candidate = _trial_candidate(SimpleNamespace(preset=_rear_pair('mono')[0]))
    candidate = replace(candidate, source_preset=preset,
                        role_attenuations_db={role: 0 if i == 0 else -3 for i, role in enumerate(roles)},
                        linearization={roles[0]: candidate.linearization['woofer']}, blend_correction=(),
                        rear_calibration=_rear_document() if name == 'cardioid' else {})
    excitation = SessionExcitation(tuple(bands), caps, LEVEL_DB,
                                   preset.crossover_regions[0].fc_hz if preset.crossover_regions else None, durations)
    return SimpleNamespace(
        name=name, topology=topology, safety_profile=safety, role_targets=targets, profile=profile,
        candidate=candidate, roles_bands=tuple(bands), graphs={}, rendered={}, directory=tmp_path_factory.mktemp(name),
        conductor=SimpleNamespace(_excitation=excitation, _gain_plan_db=dict.fromkeys(roles, BASE_STIMULUS_PEAK_DBFS)),
    )


def _outcome(speaker, row):
    selected = run_program(ROWS[row].purpose, row)
    request = request_for_program(
        selected, mover=selected.mover or 'human', level=LevelPolicy(level_db=LEVEL_DB),
        candidates=(speaker.candidate.fingerprint,) if selected.regime == 'branches' else ('base', speaker.candidate.fingerprint) if selected.purpose == 'rear' else (),
    )
    report = preflight(request, ready_facts(
        request, candidates={speaker.candidate.fingerprint: speaker.candidate},
        declared_target_ids=tuple(speaker.role_targets), roles_bands=speaker.roles_bands,
    ))
    if report.blocking:
        return {('plan_refused', issue.code) for issue in report.issues if issue.blocking}
    captures = prepare_plan_captures(report.plan, roles_bands=speaker.roles_bands)
    assert captures
    failures = set()
    for capture in captures:
        spec, stage = capture.spec, 'compose'
        try:
            program = compose_plan_program(speaker.conductor, spec, None, context=speaker)
            stage = 'graph'
            branches = branch_channels_for(spec) if spec.graph_scope == 'candidate_branches' else {}
            key = (spec.graph_scope, tuple(sorted(branches.items())))
            if key not in speaker.graphs:
                speaker.graphs[key] = (
                    emit_measurement_graph(speaker.profile) if spec.graph_scope == 'drivers' else
                    compile_tuning_graph(speaker.profile, speaker.candidate, scope=spec.graph_scope,
                                         branch_channels=branches or None)
                )
            stage = 'render'
            if program.program_id not in speaker.rendered:
                wav = speaker.directory / f'{program.program_id}.wav'
                write_program_wav(wav, program)
                speaker.rendered[program.program_id] = wav
            stage = 'admit'
            kwargs = dict(topology=speaker.topology, safety_profile=speaker.safety_profile,
                          role_targets=speaker.role_targets, session_volume_db=LEVEL_DB,
                          declared_sensitivities=SENSITIVITIES)
            wav = speaker.rendered[program.program_id]
            admission = (readmit_program_from_wav(program, wav, **kwargs) if spec.graph_scope == 'drivers' else
                         readmit_summed_program_from_wav(program, wav, graph_yaml=speaker.graphs[key],
                             graph_evidence=measurement_graph_evidence(scope=spec.graph_scope, candidate=speaker.candidate),
                             **kwargs))
            if not admission.allowed:
                failures.add(('admit_refused', tuple(code.value for code in admission.refusals),
                              spec.program_phase, spec.graph_scope))
        except Exception as exc:  # noqa: BLE001 - pin every take's failure stage, type/code, phase and scope
            failures.add((f'{stage}_error', getattr(exc, 'code', type(exc).__name__),
                          spec.program_phase, spec.graph_scope))
    return failures or {('pass',)}


@pytest.mark.parametrize('row', ROWS)
def test_every_program_on_every_layout(speaker, row):
    expected = ({('plan_refused', 'walk_branch_pair_undeclared')} if row in PLAN_REFUSALS[speaker.name]
                else KNOWN_GAPS.get((speaker.name, row), ({('pass',)}, ''))[0])
    assert _outcome(speaker, row) == expected
