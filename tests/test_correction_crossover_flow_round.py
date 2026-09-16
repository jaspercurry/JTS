# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace
import pytest

from jasper.active_speaker import plan_run
from jasper.active_speaker.angle_capture import request_for_program
from jasper.active_speaker.measurement_programs import program

from jasper.active_speaker import commissioning_coordinator as coordinator
from jasper.web import correction_crossover_flow as flow
from jasper.active_speaker.measurement_programs import available_programs
from tests.crossover_v2_fixtures import _roles


def test_choices_use_registry_and_engine_counts(monkeypatch):
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, safety_profile={}, role_targets={})
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.conductor_context.resolve_conductor_context",
                        lambda *a, **kw: context)
    choices = coordinator.round_choices({})
    assert [c["id"] for c in choices] == [f"{name}/{size}" for name, size in available_programs()]
    selected = next(c for c in choices if c["id"] == "tournament/full")
    assert selected["action"]["body"]["plan"]["program"] == "tournament/full"
    assert len(selected["action"]["body"]["plan"]["stops"]) == 3


def test_pre_round_choice_survives_a_stopped_run(monkeypatch):
    choices = [{"id": "tournament/full", "lines": [], "action": {"id": "run_program"}}]
    monkeypatch.setattr(flow, "handle_status", lambda **kw: ({"active": True,
        "setup": {"active": True, "status": "ready"}, "capture": {"status": "stopped"}}, 200))
    monkeypatch.setattr(coordinator, "round_choices", lambda status: choices)
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: {"next_action": {"program": "speaker"}})
    envelope, code = flow.handle_envelope()
    assert code == 200
    assert envelope["round_choices"] is choices
    assert envelope["round_default"] == "speaker/mark"


@pytest.mark.parametrize("repeats, counts, timing, preparation", [(1, [15, 8, 8], 1, 12), (2, [26, 16, 16], 2, 20)])
def test_three_pose_preview_counts_preparation_and_timing(repeats, counts, timing, preparation):
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, safety_profile={}, role_targets={})
    request = request_for_program(program("tournament", "full"), repeats=repeats)
    captures = plan_run.prepare_plan_captures(request, roles_bands=context.roles_bands)
    facts = plan_run.preview_schedule(request, captures, context)
    assert facts["sweeps_per_pose"] == counts
    assert facts["timing_sweeps"] == timing
    assert facts["preparation_sweeps"] == preparation
    assert facts["sweeps"] == sum(counts)
    timing_rows = [row for row in facts["pose_sweeps"][0] if row["kind"] == "summed_sweep"]
    assert [(row["repeat"], row["repeats"]) for row in timing_rows] == [(n, repeats) for n in range(1, repeats + 1)]


@pytest.mark.parametrize("failure", [False, True])
async def test_browser_round_publishes_its_packet_or_save_failure(tmp_path, monkeypatch, failure):
    from jasper.active_speaker import round_bank
    from jasper.active_speaker.crossover_v2.position_gate import PositionGate
    from jasper.web.correction_run_host import publish_round_packet

    def bank(*args, **kwargs):
        if failure:
            raise OSError("disk unavailable")
        return round_bank.BankedRound(tmp_path, {})
    monkeypatch.setattr(round_bank, "bank_round", bank)
    gate = PositionGate()
    gate.publish({"status": "complete", "takes": 3})
    await publish_round_packet(tmp_path, gate)
    assert gate.published()["run"] == {"status": "complete", "takes": 3, "faults": [],
        **({"packet_error": "OSError"} if failure else {"round_dir": str(tmp_path)})}
