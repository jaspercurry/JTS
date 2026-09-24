# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import re
from types import SimpleNamespace

import pytest

from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS, program
from jasper.active_speaker.round_copy import pose_line, round_lines
from jasper.active_speaker.timing_status import timing_status_lines

from jasper.active_speaker import commissioning_coordinator as coordinator, measurement_view, plan_run
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_MEASUREMENT_CANDIDATE_REQUIRED,
    REASON_MEASUREMENT_TARGETS_MISSING,
    CrossoverV2Refused,
    refusal_copy_for,
)
from jasper.web import correction_crossover_flow as flow
from jasper.active_speaker.measurement_programs import available_programs
from tests.crossover_v2_fixtures import _roles


_VIEW = {"next_action": {"program": "speaker"}, "programs": RUNNABLE_PROGRAMS,
         "near_field_drivers": ("tweeter", "woofer", "woofer:rear")}


def test_choices_use_registry_and_engine_counts(monkeypatch):
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.conductor_context.resolve_conductor_context",
                        lambda *a, **kw: context)
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: _VIEW)
    planned = []
    preview = plan_run.preview_schedule
    monkeypatch.setattr(plan_run, "preview_schedule", lambda request, *args: (
        planned.append(request.program), preview(request, *args))[1])
    visible = [(name, size) for name, size in available_programs()
               if f"{name}/{size}" not in measurement_view._ALIAS_PLAN_IDS]
    choices = measurement_view.round_choices({}, "tournament/full")
    assert planned == ["tournament/full"]
    assert [c["id"] for c in choices] == [f"{name}/{size}" for name, size in visible]
    assert sum("lines" in c for c in choices) == 1
    assert [c["id"] for c in choices if c["default"]] == ["tournament/full"]
    assert [(c["poses"], c["captures"]) for c in choices] == [
        (program(name, size).mic_move_count, program(name, size).capture_count) for name, size in visible]
    selected = next(c for c in choices if c["id"] == "tournament/full")
    assert selected["action"]["body"]["plan"]["program"] == "tournament/full"
    assert len(selected["action"]["body"]["plan"]["stops"]) == 3


def test_a_branches_row_discloses_its_refusal_beside_a_startable_row(monkeypatch):
    """#5321: picking a ``regime: branches`` row 500'd the page."""
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.conductor_context.resolve_conductor_context",
                        lambda *a, **kw: context)
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: _VIEW)
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


def test_a_conductor_context_refusal_discloses_on_its_row_instead_of_500(monkeypatch):
    """#5340: resolve_conductor_context's typed refusal (e.g. a declared role
    with no measurement target) escaped ``round_choices`` uncaught and 500'd
    the envelope route."""
    def _refuse(*_args, **_kwargs):
        raise CrossoverV2Refused(
            "no measurement target for every declared driver",
            code=REASON_MEASUREMENT_TARGETS_MISSING,
        )
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_v2.conductor_context.resolve_conductor_context", _refuse,
    )
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: _VIEW)
    monkeypatch.setattr(flow, "handle_status", lambda **kw: ({"active": True,
        "setup": {"active": True, "status": "ready"}, "capture": None}, 200))

    envelope, code = flow.handle_envelope(selected_program="speaker/mark")

    assert code == 200
    choices = {c["id"]: c for c in envelope["round_choices"]}
    assert len(choices) == len(available_programs()) - len(measurement_view._ALIAS_PLAN_IDS)
    selected = choices["speaker/mark"]
    assert selected["code"] == REASON_MEASUREMENT_TARGETS_MISSING
    assert "action" not in selected
    assert selected["lines"]


def test_alias_ids_are_hidden_from_the_picker_but_still_resolve(monkeypatch):
    """R4-D9: seat/cloud duplicates room/cloud and seat/express duplicates
    room/seat. The picker offers only one of each pair, but both ids stay
    registered and keep resolving (ADR-0277: registry ids are banked-round
    identities)."""
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.conductor_context.resolve_conductor_context",
                        lambda *a, **kw: context)
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: _VIEW)

    choices = measurement_view.round_choices({}, "speaker/mark")

    ids = {c["id"] for c in choices}
    assert "seat/cloud" not in ids
    assert "seat/express" not in ids
    assert program("seat", "cloud").program_id == "seat"
    assert program("seat", "express").program_id == "seat"


def test_pre_round_choice_survives_a_stopped_run(monkeypatch):
    choices = [{"id": "tournament/full", "lines": [], "action": {"id": "run_program"}}]
    monkeypatch.setattr(flow, "handle_status", lambda **kw: ({"active": True,
        "setup": {"active": True, "status": "ready"}, "capture": {"status": "stopped"}}, 200))
    monkeypatch.setattr(measurement_view, "round_choices", lambda status, selected: choices)
    envelope, code = flow.handle_envelope()
    assert code == 200
    assert envelope["round_choices"] is choices


def test_every_pose_hold_reaches_the_page_with_its_placement_words(monkeypatch):
    """#5632 F5: poses 2..N sent the page their buttons but not where the microphone goes."""
    hold = {"index": 2, "attempt": 2, "degrees": 0, "vertical_deg": 0, "mover": "human",
            "prompt": {"progress": "", "title": "Move the microphone 12 in (30 cm) FORWARD.", "body": ""},
            "actions": [{"id": "position_ready", "label": "Microphone is at the seat", "endpoint": "/placed", "body": {}}]}
    facts = {"pose": 2, "poses": 3, "mover": "human", "pose_details": [{"kind": "seat"}] * 3}
    monkeypatch.setattr(flow, "handle_status", lambda **kw: ({"active": True, "setup": {"active": True, "status": "ready"},
        "crossover_v2": {"phase": "measure"},
        "capture": {"status": "awaiting_capture", "run": facts, "position_pending": hold}}, 200))
    envelope, _ = flow.handle_envelope()
    pending = envelope["pending"]
    assert (pending["prompt"], pending["mover"], pending["degrees"]) == (hold["prompt"], "human", 0)
    assert pending["actions"][0] == hold["actions"][0]


@pytest.mark.parametrize("mover, action, held, names_release", [
    ("human", "fix_and_retake", True, True),
    ("human", "fix_and_retake", False, False),
    ("arm", "fix_and_retake", True, False),
    ("human", "retake_same", True, False),
])
def test_a_retake_names_its_release_only_while_it_waits_for_one(mover, action, held, names_release):
    """#5632 F9: after ``anchor_ambiguous`` the page said "Taking it again." while it waited for a click."""
    release = "Microphone is at the seat"
    hold = {"mover": mover, "actions": [{"id": "position_ready", "label": release}] if mover == "human" else []}
    facts = {"mover": mover, "retake_pose": 2, "retake_measurement": 5,
             "retake_reason": "anchor_ambiguous", "retake_action": action}
    line = measurement_view.round_status({"run": facts, **({"position_pending": hold} if held else {})})[0]
    assert refusal_copy_for("anchor_ambiguous")[0] in line
    assert (release in line) is names_release


@pytest.mark.parametrize("n", [1, 2])
def test_round_counts_of_one_read_in_the_singular(n):
    """#5632 F13: "1 measurements in total" and "Allow about 1 minutes"."""
    before = {"poses": 1, "mover": "human", "measurements_per_pose": [n], "measurements": n, "estimated_seconds": 50 * n}
    after = {"status": "partial", "takes": n, "not_measured": n}
    text = " ".join(round_lines(before) + round_lines(after))
    plural_after_n = re.findall(rf"\b{n} (?:[a-z]+ )?[a-z]+s\b", text)
    assert (plural_after_n == []) is (n == 1), plural_after_n


def test_a_one_measurement_pose_names_its_measurement_once():
    """#5632 F13: "measurements 2–2"."""
    facts = {"pose": 2, "poses": 3, "mover": "human", "measurements_per_pose": [1, 1, 1],
             "pose_details": [{"kind": "seat"}] * 3}
    assert re.findall(r"\d+", pose_line(facts)) == ["2", "3", "2"]


def test_page_figures_are_rounded_for_reading():
    """#5632 F13: "within 10.6152 dB; repeat noise 0.426631 dB"."""
    profile = {"timing": {"delay_us": -186.04, "polarity": "normal", "provenance": "set_by_user"}}
    round_ = {"alignment_verdict": {"verification": {"residual_rms_db": 10.6152, "repeat_noise_db": 0.426631}}}
    lines = timing_status_lines(profile, round_)
    figures = r"-?\d+(?:\.\d+)?"
    assert re.findall(figures, lines["saved"] + lines["verification"]) == ["-186.04", "10.62", "0.43"]
    assert re.findall(figures, round_lines({"level_raise_dbfs": -21.345678})[0]) == ["-21.3"]
