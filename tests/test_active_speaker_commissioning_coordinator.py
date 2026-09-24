# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import argparse
import re
from dataclasses import replace
from types import SimpleNamespace

from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.round_copy import RUN_ENDED
from jasper.active_speaker.measurement_view import round_choices
from tests.crossover_v2_fixtures import _roles

from jasper.active_speaker import applied_tune, baseline_profile, commissioning_experiment, commissioning_coordinator as coordinator
from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.commissioning_coordinator import next_program_action, load_commissioning_view
from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS
from jasper.active_speaker import tuning_handoff
from jasper.cli.round import build_parser
from jasper.active_speaker.crossover_v2 import round_inputs
from jasper.cli.doctor import active_speaker as doctor
from jasper.doctor_contract import check_row
from jasper.identity.reader import SPEAKER_SETUP_PAGE_PATH
from jasper.json_fields import parse_utc_iso
from jasper.web import correction_crossover_v2_status as v2status, sound_active_speaker
from jasper.web.correction_crossover_v2_grade import GRADE_NOT_APPLIED
from tests.test_active_speaker_baseline_profile import _v2_candidate
from tests.test_correction_crossover_v2_endpoints import _seed_baseline_apply_environment

import pytest

from jasper.active_speaker.baseline_profile import APPLIED_PROFILE_DISPLACED
from jasper.active_speaker.commissioning_coordinator import build_commissioning_view
from tests.active_speaker_fixtures import mono_output_topology as _topology, passive_stereo_output_topology


def _ready_design() -> dict:
    return {
        "kind": "jts_active_speaker_design_draft",
        "status": "ready_for_review",
        "driver_safety_profile": {"targets": [], "issues": []},
        "summary": {
            "missing_driver_info_roles": [],
            "missing_crossover_candidate_pairs": [],
        },
    }


def _ready_preview() -> dict:
    return {
        "kind": "jts_active_speaker_crossover_preview",
        "status": "ready_for_protected_staging",
    }


def _applied_anchor(basename: str = "candidate_f7e9.yml", *, layers=("speaker",)) -> dict:
    return {
        "status": "applied",
        "applied_at": "2026-08-30T01:33:34Z",
        "source": {"measured_candidate_fingerprint": "saved-speaker"},
        "recomposition_snapshot": {
            "linearization": {"tweeter": [{"type": "Peaking"}]} if "speaker" in layers else {},
            "room_correction": {"filters": [{"gain_db": -2}]} if "room" in layers else {},
            "bass_extension": {"enabled": True} if "bass" in layers else {},
            "rear_calibration": {"mode": "cardioid"} if "rear" in layers else {},
        },
        "blend_correction": [{"type": "Peaking"}],
        "config": {
            "path": "/var/lib/camilladsp/configs/" + basename,
            "basename": basename,
            "exists": True,
            "sha256": "abcdef012345" * 5 + "abcd",
        },
    }


def _applied_baseline_profile(**overrides) -> dict:
    return {"status": "ready_to_compile", "candidate_fingerprint": "review-fp",
            "permissions": {"may_compile": True}, "issues": [], **overrides}


@pytest.mark.parametrize("status,current,action,enabled,program,layers,rounds,reason_code", [
    ("needs_layout", "layout", "declare_speaker", True, None, (), (), None),
    ("needs_driver_values", "research", "save_driver_values", True, None, (), (), None),
    ("needs_driver_safety_profile", "research", "save_driver_values", True, None, (), (), None),
    ("ready_to_save_profile", "profile", "save_baseline_profile", True, None, (), (), None),
    ("blocked", "profile", "save_baseline_profile", False, None, (), (), None),
    ("applied", "profile", "run_program", True, "bass", ("speaker",), (), "layer_not_applied"),
    ("not_required", "layout", "run_program", True, "bass", (), (), None),
    ("applied", "profile", "copy_prompt", True, "speaker", (), (("speaker", 1),), None),
    ("applied", "profile", "run_program", True, "speaker", (), (("speaker", 0),), None),
    ("applied", "profile", "run_program", True, "bass", ("speaker",), (("speaker", 1),), None),
    ("applied", "profile", "copy_prompt", True, "room", ("speaker", "bass"), (("room", 1),), None),
    ("applied", "profile", "run_program", True, "bass", ("speaker", "room"), (("room", 1),), None),
    ("applied", "profile", "copy_prompt", True, "bass", ("speaker", "room"), (("bass", 1),), None),
    ("applied", "profile", None, False, None, ("speaker", "room", "bass"), (("speaker", 1),), "complete"),
    ("not_required", "layout", "copy_prompt", True, "bass", (), (("bass", 1),), None),
    ("applied", "profile", "run_program", True, "speaker", (), (("speaker", -1),), "layer_not_applied"),
])
def test_every_commissioning_state_has_one_next_action(status, current, action, enabled, program, layers, rounds, reason_code):
    draft = _ready_design()
    topology = passive_stereo_output_topology() if status == "not_required" else _topology()
    if status == "needs_layout":
        topology = replace(topology, speaker_groups=())
    elif status == "needs_driver_values":
        draft = {}
    elif status == "needs_driver_safety_profile":
        draft["driver_safety_profile"]["issues"] = [{"code": "tweeter:required_highpass_missing"}]
    applied = _applied_anchor(layers=layers)
    applied_at = parse_utc_iso(applied["applied_at"])
    recent = {name: {"round_dir": f"/bank/{name}", "started_at": applied_at + age} for name, age in rounds}
    view = build_commissioning_view(
        topology, design_draft=draft, crossover_preview=_ready_preview(),
        baseline_profile=_applied_baseline_profile(permissions={"may_compile": status != "blocked"}),
        applied_profile=applied if status == "applied" else None, recent_rounds=recent,
        first_experiment={"candidate_fingerprint": "measured-fp"} if action == "save_baseline_profile" else None,
    )
    assert [step["id"] for step in view["steps"]] == ["layout", "research", "profile"]
    assert sum(step["status"] == "active" for step in view["steps"]) <= 1
    assert view["current_step"] == current
    assert view["status"] == status
    assert (view["next_action"]["id"], view["next_action"]["enabled"], view["next_action"].get("program")) == (
        action, enabled, program)
    if reason_code is not None:
        assert view["next_action"]["reason_code"] == reason_code
    assert "command" not in view["next_action"]
    assert view["combined_groups"] == []
    if action == "save_baseline_profile":
        assert view["next_action"]["body"] == {}
    elif action == "copy_prompt":
        assert view["next_action"]["round_dir"] == recent[program]["round_dir"]
    elif action == "declare_speaker":
        assert view["next_action"]["endpoint"] == SPEAKER_SETUP_PAGE_PATH
        assert view["next_action"]["method"] == "GET"
    _assert_household_safe(view["next_action"]["label"], "action")
    assert "driver_values" in view


@pytest.mark.parametrize("consumer", ["cli", "coordinator"])
def test_program_order_consumers(consumer):
    if consumer == "cli":
        commands = next(action for action in build_parser()._actions
                        if isinstance(action, argparse._SubParsersAction))
        order = next(action.choices for action in commands.choices["run"]._actions
                     if action.dest == "program")
    else:
        order = tuple(next_program_action(
            _applied_anchor(layers=RUNNABLE_PROGRAMS[:index]), {},
            {"speaker": {"round_dir": "/bank/speaker", "started_at": 1}}, programs=RUNNABLE_PROGRAMS,
        )["program"] for index in range(len(RUNNABLE_PROGRAMS)))
    assert tuple(order) == RUNNABLE_PROGRAMS == ("speaker", "rear", "bass", "room")


@pytest.mark.parametrize("rear,passive", [(False, False), (True, False), (False, True)])
def test_round_and_handoff_menus_follow_topology(monkeypatch, rear, passive):
    topology = passive_stereo_output_topology() if passive else _topology()
    if rear:
        group, = topology.speaker_groups
        channel = replace(group.channels[0], output_variant="rear", physical_output_index=2)
        topology = replace(topology, speaker_groups=(replace(group, channels=(*group.channels, channel)),))
    view = build_commissioning_view(topology)
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: view)
    monkeypatch.setattr(tuning_handoff, "build_tuning_handoff_binding", lambda *args: {})
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    monkeypatch.setattr(applied_tune, "compile_commissioning_profile", lambda **kw: {})

    choices = round_choices({}, "front_rear/express")
    ids = {choice["id"] for choice in choices}
    rear_ids = {"rear/express", "rear/wide", "rear/behind", "rear/pair", "rear/pair_behind", "front_rear/express"}
    assert ids & rear_ids == (rear_ids if rear else set())
    assert ("speaker/mark" in ids) is not passive
    assert ("branches/express" in ids) is not passive
    assert {"seat/cube", "room/cloud", "room/seat", "close/spot"} <= ids
    assert sum(choice["default"] for choice in choices) == 1
    programs = ("bass", "room") if passive else ("speaker", "rear", "bass", "room") if rear else ("speaker", "bass", "room")
    handoff = tuning_handoff.build_tuning_handoff(commissioning_view=view, design_draft={})
    assert (handoff["status"] == "ready") is passive
    assert bool(handoff["prompt"]) is passive
    page = sound_active_speaker._active_speaker_baseline_profile_payload()
    assert tuple(entry["id"] for entry in handoff["programs"]) == programs
    assert tuple(entry["id"] for entry in page["tuning_programs"]) == programs


@pytest.mark.parametrize("rear", [False, True])
@pytest.mark.parametrize("layers,rounds,expected,reason", [
    (None, {}, "speaker", "never_measured"),
    ((), {}, "speaker", "layer_not_applied"),
    (("speaker",), {}, "bass", "layer_not_applied"),
    (("speaker", "rear"), {}, "bass", "layer_not_applied"),
    (("speaker", "rear", "bass"), {}, "room", "layer_not_applied"),
    (RUNNABLE_PROGRAMS, {}, None, "complete"),
    (RUNNABLE_PROGRAMS, {"room": {"started_at": 1}, "speaker": {"started_at": 2}}, "room", "upstream_changed"),
    (("speaker", "rear"), {"bass": {"round_dir": "/bank/bass", "started_at": 1}}, "bass", "round_available"),
])
def test_next_program_follows_applied_layers_and_rounds(rear, layers, rounds, expected, reason):
    topology = _topology()
    if rear:
        group, = topology.speaker_groups
        channel = replace(group.channels[0], output_variant="rear", physical_output_index=2)
        topology = replace(topology, speaker_groups=(replace(group, channels=(*group.channels, channel)),))
    action = next_program_action(None if layers is None else _applied_anchor(layers=layers), {}, rounds,
                                 programs=coordinator.programs_for_topology(topology))
    assert (action["program"], action["reason_code"]) == ("rear" if rear and layers == ("speaker",) else expected, reason)
    assert action["enabled"] is (expected is not None)


@pytest.mark.parametrize("upstream", ["speaker", "rear", "bass"])
@pytest.mark.parametrize("age", [0, 1, 2])
@pytest.mark.parametrize("room_applied", [False, True])
def test_room_repeats_after_newer_upstream_round(upstream, age, room_applied):
    layers = RUNNABLE_PROGRAMS if room_applied else RUNNABLE_PROGRAMS[:-1]
    rounds = {"room": {"round_dir": "/bank/room", "started_at": 1},
              upstream: {"round_dir": "/bank/upstream", "started_at": age}}
    action = next_program_action(_applied_anchor(layers=layers), {}, rounds, programs=RUNNABLE_PROGRAMS)
    expected = ("run_program", "room") if age > 1 else (
        (None, None) if room_applied else ("copy_prompt", "room"))
    assert (action["id"], action["program"]) == expected


def _assert_household_safe(text: str, where: str) -> None:
    assert "/" not in text, f"{where}: filesystem path in household copy: {text!r}"
    assert not re.search(r"\b\w*(?:Error|Exception)\b", text), (
        f"{where}: exception class in household copy: {text!r}"
    )
    assert "_" not in text, f"{where}: raw identifier in household copy: {text!r}"
    for token in ("camilladsp", "yaml", "alsa", "configfs", "systemd", "snd-aloop", "jasper-"):
        assert token not in text.lower(), f"{where}: {token!r} in household copy: {text!r}"


@pytest.mark.parametrize("ready,passive", [(False, False), (True, False), (False, True)])
def test_every_step_message_is_household_safe(ready, passive):
    view = build_commissioning_view(
        passive_stereo_output_topology() if passive else _topology(),
        design_draft=_ready_design() if ready else {}, crossover_preview=_ready_preview(),
    )
    for step in view["steps"]:
        _assert_household_safe(step["message"], f"step {step['id']}")
        _assert_household_safe(step["label"], f"step {step['id']}")
    _assert_household_safe(view["next_action"]["label"], "action")
    assert "command" not in view["next_action"]


@pytest.mark.parametrize("displaced", [False, True])
def test_preview_and_displaced_profile_keep_existing_actions(displaced):
    view = build_commissioning_view(
        _topology(), design_draft=_ready_design(), crossover_preview=_ready_preview() if displaced else {},
        baseline_profile=_applied_baseline_profile(), applied_profile=_applied_anchor() if displaced else None,
        applied_profile_verdict=APPLIED_PROFILE_DISPLACED if displaced else "",
        first_experiment={"candidate_fingerprint": "measured-fp"},
    )
    assert view["status"] == ("ready_to_save_profile" if displaced else "needs_driver_values")
    assert view["next_action"]["id"] == ("save_baseline_profile" if displaced else "save_driver_values")
    assert view["next_action"]["enabled"] is True


def test_loaded_commissioning_view_uses_banked_rounds(monkeypatch, tmp_path):
    topology, _ = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    profile = _applied_anchor(layers=())
    identities = []

    def recent(identity, *, programs):
        identities.append((identity, programs))
        return {"speaker": {"round_dir": "/bank/speaker", "started_at": parse_utc_iso(profile["applied_at"]) + 1}}

    monkeypatch.setattr(round_inputs, "latest_banked_rounds", recent)
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: profile)
    view = load_commissioning_view(topology)
    assert identities == [(applied_identity(profile), ("speaker", "bass", "room"))]
    assert view["programs"] == identities[0][1]
    assert view["next_action"]["id"] == "copy_prompt"
    assert view["next_action"]["program"] == "speaker"
    assert view["next_action"]["round_dir"] == "/bank/speaker"


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("applied_fingerprint", [None, "saved-fp", "review-fp"])
def test_applied_identity_change_is_disclosed_without_parking_review(ready, applied_fingerprint):
    review = _applied_baseline_profile(
        status="ready_to_compile" if ready else "blocked",
        permissions={"may_compile": ready},
        issues=[] if ready else [{"severity": "blocker", "code": "compose_refused"}],
    )
    applied = {**_applied_anchor(), "candidate_fingerprint": applied_fingerprint,
               "source": {"measured_candidate_fingerprint": "content-fp"}}
    view = build_commissioning_view(
        _topology(), design_draft=_ready_design(), crossover_preview=_ready_preview(),
        first_experiment={"candidate_fingerprint": "measured-fp"}, baseline_profile=review,
        applied_profile=applied if applied_fingerprint else None,
    )
    if applied_fingerprint:
        assert view["status"] == "applied"
        assert view["applied_profile"]["candidate_fingerprint"] == "content-fp"
        assert view["applied_profile"]["applied_at"] == applied["applied_at"]
        assert view["applied_profile"]["config_path"] == applied["config"]["path"]
        disclosures = view["applied_profile"]["disclosures"]
        if applied_fingerprint == review["candidate_fingerprint"]:
            assert disclosures == []
        else:
            assert [(item["code"], item["severity"], item["status"]) for item in disclosures] == [
                ("baseline_candidate_fingerprint_mismatch", "warning", "disclosed_stale"),
            ]
    else:
        assert view["status"] == ("ready_to_save_profile" if ready else "blocked")
        assert view["next_action"]["enabled"] is ready
    assert view["review"]["ready"] is ready
    assert view["review"]["issues"] == review["issues"]


@pytest.mark.parametrize("record", ["absent", "applied", "legacy"])
@pytest.mark.parametrize("session_applied", [False, True])
def test_applied_identity_is_shared_by_status_commissioning_and_doctor(monkeypatch, record, session_applied):
    applied = {**_applied_anchor(), "candidate_fingerprint": "e17afd20" * 8,
               "source": {"measured_candidate_fingerprint": "48a805ab" * 8}}
    applied["config"]["sha256"] = "7edfa758981e" + "a" * 52
    expected = {"candidate": "48a805ab" * 8, "record": "7edfa758981e",
                "config_path": applied["config"]["path"], "applied_at": applied["applied_at"]}
    if record == "absent":
        applied = expected = None
    elif record == "legacy":
        applied = {"status": "applied", "candidate_fingerprint": "unrelated-profile-id"}
        expected = dict.fromkeys(expected)
    monkeypatch.setattr(v2status, "load_applied_baseline_profile_state", lambda: applied)
    monkeypatch.setattr(v2status.v2state, "load_v2_state", lambda: {"session_id": "fresh", "applied": session_applied})
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: applied)
    monkeypatch.setattr(doctor.evidence, "active_speaker_setup_status", lambda: {
        "protected_profile": {"layer_a_binding": {"matches": True}} if applied else None,
    })
    assert applied_identity(applied) == expected
    block = v2status.crossover_v2_status_block()
    assert block["applied_identity"] == expected
    assert block["applied"] is session_applied
    if not session_applied:
        assert block["post_apply_grade"]["state"] == GRADE_NOT_APPLIED
        assert block["post_apply_grade"]["complete"] is True
    view = build_commissioning_view(_topology(), applied_profile=applied)["applied_profile"]
    assert {"candidate" if key == "candidate_fingerprint" else key: view[key]
            for key in ("candidate_fingerprint", "record", "config_path", "applied_at")} == (
                expected or dict.fromkeys(("candidate", "record", "config_path", "applied_at")))
    assert check_row(doctor.check_active_speaker_applied_graph()).get("applied_identity") == expected


@pytest.mark.parametrize("packet,status,reason", [
    ({"status": "awaiting_apply", "reason": ""}, "measured", None),
    ({"status": "alignment_unmeasured", "reason": "delay_out_of_bounds"}, "alignment_unmeasured", "delay_out_of_bounds"),
    ({}, "declared", None),
])
def test_household_experiment_reads_packet_alignment(monkeypatch, tmp_path, packet, status, reason):
    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = replace(_v2_candidate(preset), analysis={"evidence": {"commissioning": packet}})
    monkeypatch.setattr(commissioning_experiment, "commissioning_candidate", lambda *a: candidate)
    view = load_commissioning_view(topology)
    assert view["first_experiment"]["alignment"] == {"status": status, "reason": reason}
    assert view["first_experiment"]["candidate_fingerprint"] == (candidate.fingerprint if packet else None)
    assert view["first_experiment"]["complete"] is bool(packet)


@pytest.mark.parametrize("selected_id", ["rear/express", "speaker/mark"])
def test_finished_round_names_the_next_pose_set(monkeypatch, selected_id):
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, safety_profile={}, role_targets={})
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.conductor_context.resolve_conductor_context", lambda *a, **kw: context)
    monkeypatch.setattr(coordinator, "load_commissioning_view", lambda: {"next_action": {"program": "speaker"}, "programs": RUNNABLE_PROGRAMS})
    status = {"active": True, "setup": {"active": True, "status": "ready"},
              "capture": {"status": "complete", "run": {"status": "complete", "poses": 1}}}
    choices = round_choices(status, selected_id)
    env = build_crossover_envelope_v2({**status, "round_choices": choices})
    action = next(c["action"] for c in env["round_choices"] if c["id"] == selected_id)
    assert action["body"]["plan"]["program"] == selected_id
    assert (env["screen"], env["terminal_status"], env["verdict_text"]) == ("finished", "complete", RUN_ENDED)
