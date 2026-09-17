# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from jasper.active_speaker.measurement_programs import program

from jasper.active_speaker import commissioning_coordinator as coordinator, plan_run
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_MEASUREMENT_CANDIDATE_REQUIRED
from jasper.web import correction_crossover_flow as flow
from jasper.active_speaker.measurement_programs import available_programs
from tests.crossover_v2_fixtures import _roles


def test_choices_use_registry_and_engine_counts(monkeypatch):
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, safety_profile={}, role_targets={})
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.conductor_context.resolve_conductor_context",
                        lambda *a, **kw: context)
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: {"next_action": {"program": "speaker"}})
    planned = []
    preview = plan_run.preview_schedule
    monkeypatch.setattr(plan_run, "preview_schedule", lambda request, *args: (
        planned.append(request.program), preview(request, *args))[1])
    choices = coordinator.round_choices({}, "tournament/full")
    assert planned == ["tournament/full"]
    assert [c["id"] for c in choices] == [f"{name}/{size}" for name, size in available_programs()]
    assert sum("lines" in c for c in choices) == 1
    assert [c["id"] for c in choices if c["default"]] == ["speaker/mark"]
    assert [(c["poses"], c["captures"]) for c in choices] == [
        (program(name, size).mic_move_count, program(name, size).capture_count) for name, size in available_programs()]
    selected = next(c for c in choices if c["id"] == "tournament/full")
    assert selected["action"]["body"]["plan"]["program"] == "tournament/full"
    assert len(selected["action"]["body"]["plan"]["stops"]) == 3


def test_a_branches_row_discloses_its_refusal_beside_a_startable_row(monkeypatch):
    """#5321: picking a ``regime: branches`` row 500'd the page."""
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, safety_profile={}, role_targets={})
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.conductor_context.resolve_conductor_context",
                        lambda *a, **kw: context)
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: {"next_action": {"program": "speaker"}})
    monkeypatch.setattr(flow, "handle_status", lambda **kw: ({"active": True,
        "setup": {"active": True, "status": "ready"}, "capture": None}, 200))
    picked = {}
    for row in ("front_rear/express", "branches/express", "speaker/mark"):
        envelope, code = flow.handle_envelope(selected_program=row)
        assert code == 200
        picked[row] = next(c for c in envelope["round_choices"] if c["id"] == row)
    for row in ("front_rear/express", "branches/express"):
        assert picked[row]["code"] == REASON_MEASUREMENT_CANDIDATE_REQUIRED
        assert "action" not in picked[row]
        assert picked[row]["lines"]
    assert picked["speaker/mark"]["action"]["id"] == "run_program"
    assert "code" not in picked["speaker/mark"]


def test_pre_round_choice_survives_a_stopped_run(monkeypatch):
    choices = [{"id": "tournament/full", "lines": [], "action": {"id": "run_program"}}]
    monkeypatch.setattr(flow, "handle_status", lambda **kw: ({"active": True,
        "setup": {"active": True, "status": "ready"}, "capture": {"status": "stopped"}}, 200))
    monkeypatch.setattr(coordinator, "round_choices", lambda status, selected: choices)
    envelope, code = flow.handle_envelope()
    assert code == 200
    assert envelope["round_choices"] is choices
