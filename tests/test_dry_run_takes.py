# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""A dry run builds, composes and admits each distinct take graph its run plays (#6113)."""
from __future__ import annotations

import pytest

from jasper.active_speaker import baseline_profile, commission_wiring, design_draft, dry_run_takes
from jasper.active_speaker.angle_capture import request_for_preset
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.crossover_v2 import conductor_context, door
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_PROGRAM_OUTPUT_MUTED
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile
from jasper.active_speaker.measurement_programs import (
    available_presets, offered_here, preset, programs_for_topology, run_preset,
)
from jasper.active_speaker.movers import MOVER_HUMAN
from jasper.active_speaker.preflight import PreflightReport
from jasper.cli import _run_request, round as cli
from jasper.platform.speaker_layout import measurement_target_id
from tests.active_speaker_fixtures import isolated_candidate_bank as isolated_candidate_bank
from tests.apply_fixtures import bank_candidate
from tests.crossover_v2_fixtures import _preset
from tests.test_active_speaker_audition import ACTIVE_PCM
from tests.test_active_speaker_program_admission import _fresh_cardioid, _profile_and_targets
from tests.test_crossover_v2_tuning_scope import _trial_candidate
from tests.test_preflight import ready_facts
from tests.test_rear_output_foundation import _rear_document

pytestmark = pytest.mark.usefixtures("isolated_candidate_bank")


def _base(monkeypatch, base):
    """``base``'s conductor context, with its candidate banked and applied as the run's base."""
    if base == "two_way":
        topology, safety, targets = _profile_and_targets(woofer_floor=20, woofer_highpass=20)
        monkeypatch.setattr(design_draft, "load_design_draft", lambda **kw: {"driver_safety_profile": safety})
        monkeypatch.setattr(conductor_context, "ensure_crossover_preview_ready", lambda draft: None)
        monkeypatch.setattr(commission_wiring, "resolve_capture_preset", lambda topology: _preset())
        context = conductor_context.resolve_conductor_context(
            {"active": True, "targets": {"drivers": active_driver_targets(topology)}}, topology=topology)
        candidate = _trial_candidate(MeasurementGraphProfile(
            _preset(), topology, context.role_channels, ACTIVE_PCM,
            protection_sections_by_role=confirmed_protection_sections(safety, targets)))
    else:
        _topology, _safety, context, _profile, candidate = _fresh_cardioid(
            monkeypatch, rear_calibration=_rear_document() if base == "rear_tuned_cardioid" else None)
    bank_candidate(candidate)
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: {
        "source": {"measured_candidate_fingerprint": candidate.fingerprint}})
    monkeypatch.setattr(dry_run_takes, "baseline_candidate_id", lambda: candidate.fingerprint)
    return context, candidate


def _shipped_plans(context, candidate_id):
    """Every plan the shipped presets offer this speaker, at each layout they offer."""
    targets = tuple(measurement_target_id(target["role"], target.get("output_variant") or "primary")
                    for target in active_driver_targets(context.topology))
    for name in available_presets():
        for layout in preset(name).layouts:
            plan = run_preset(name, layout)
            if offered_here(plan, programs=programs_for_topology(context.topology), targets=targets):
                yield f"{name}@{layout}", request_for_preset(
                    plan, candidates=(candidate_id,) if plan.regime == "branches" else (),
                    mover=plan.mover or MOVER_HUMAN, targets=targets)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "run"])
def test_a_dry_run_refuses_a_take_its_admission_refuses_and_names_it(monkeypatch, dry_run):
    """Bug 4 (#6113) through the door before its park: on a fresh cardioid the speaker
    program's timing take feeds the rear its graph keeps muted. The dry run refuses with
    the code that take's run banks and names the take; a run's start composes nothing."""
    context, _candidate = _base(monkeypatch, "fresh_cardioid")
    monkeypatch.setattr(door, "park_muted_outputs", lambda text: text)
    monkeypatch.setattr(_run_request, "load_output_topology", lambda: context.topology)
    monkeypatch.setattr(_run_request, "read_preflight_facts", lambda plan, **kw: ready_facts(
        plan, **kw, context=context, roles_bands=context.roles_bands))
    report = _run_request.resolve_run(cli.build_parser().parse_args(
        ["run", "--program", "speaker", *(["--dry-run"] if dry_run else [])]))

    refused = [(issue.code, issue.blocking, issue.evidence) for issue in report.issues if "take" in issue.evidence]
    assert refused == ([(REASON_PROGRAM_OUTPUT_MUTED, True, {
        "take": {"index": 2, "candidate_id": "base", "regime": "summed", "place": ("bearing", 0, 0, None, None),
                 "graph_scope": "timing"},
        "level_db": -30.0, "refusals": ["program_graph_not_proven"],
        "muted_output": {"target_id": "woofer:rear", "output_index": 2}})] if dry_run else [])


@pytest.mark.parametrize("base", ["two_way", "rear_tuned_cardioid", "fresh_cardioid"])
def test_the_dry_run_refuses_no_shipped_plan(monkeypatch, base):
    """Every offered preset and layout on a two-way, a rear-tuned cardioid and a fresh
    cardioid, whose take graphs admission admits: the dry run adds no blocking issue."""
    context, candidate = _base(monkeypatch, base)
    for name, request in _shipped_plans(context, candidate.fingerprint):
        report = dry_run_takes.admit_dry_run_takes(PreflightReport(request, (), (), {}, None), context)
        assert not report.blocking, (name, [issue.evidence for issue in report.issues])
        assert report.take_admission["graphs"] > 0, name
