# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import re
from dataclasses import replace

from jasper.active_speaker import baseline_profile, commissioning_experiment
from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.commissioning_coordinator import load_commissioning_view
from jasper.active_speaker.crossover_v2 import round_inputs
from jasper.cli.doctor import active_speaker as doctor
from jasper.doctor_contract import check_row
from jasper.identity.reader import SPEAKER_SETUP_PAGE_PATH
from jasper.json_fields import parse_utc_iso
from jasper.web import correction_crossover_v2_status as v2status
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
            "permissions": {"may_compile": True, "may_apply": False}, "issues": [], **overrides}


@pytest.mark.parametrize("status,current,action,enabled,program,layers,rounds", [
    ("needs_layout", "layout", "declare_speaker", True, None, (), ()),
    ("needs_driver_values", "research", "save_driver_values", True, None, (), ()),
    ("needs_driver_safety_profile", "research", "save_driver_values", True, None, (), ()),
    ("needs_first_experiment", "experiment", "run_speaker_program", True, "speaker", (), ()),
    ("ready_to_save_profile", "profile", "apply_candidate", True, None, (), ()),
    ("blocked", "profile", "apply_candidate", False, None, (), ()),
    ("applied", "profile", "run_program", True, "speaker", ("speaker",), ()),
    ("not_required", "layout", "run_program", True, "room", (), ()),
    ("applied", "profile", "copy_prompt", True, "speaker", (), (("speaker", 1),)),
    ("applied", "profile", "run_program", True, "speaker", (), (("speaker", 0),)),
    ("applied", "profile", "run_program", True, "room", ("speaker",), (("speaker", 1),)),
    ("applied", "profile", "copy_prompt", True, "room", ("speaker",), (("room", 1),)),
    ("applied", "profile", "run_program", True, "bass", ("speaker", "room"), (("room", 1),)),
    ("applied", "profile", "copy_prompt", True, "bass", ("speaker", "room"), (("bass", 1),)),
    ("applied", "profile", "run_program", True, "speaker", ("speaker", "room", "bass"), (("speaker", 1),)),
    ("not_required", "layout", "copy_prompt", True, "room", (), (("room", 1),)),
])
def test_every_commissioning_state_has_one_next_action(status, current, action, enabled, program, layers, rounds):
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
        baseline_profile=_applied_baseline_profile(permissions={"may_apply": status != "blocked"}),
        applied_profile=applied if status == "applied" else None, recent_rounds=recent,
        first_experiment={"candidate_fingerprint": "measured-fp"} if action == "apply_candidate" else None,
    )
    assert [step["id"] for step in view["steps"]] == ["layout", "research", "experiment", "profile"]
    assert sum(step["status"] == "active" for step in view["steps"]) <= 1
    assert view["current_step"] == current
    assert view["status"] == status
    assert (view["next_action"]["id"], view["next_action"]["enabled"], view["next_action"].get("program")) == (
        action, enabled, program)
    assert "command" not in view["next_action"]
    assert view["combined_groups"] == []
    if action == "apply_candidate":
        assert view["next_action"]["body"] == {"expected_candidate_fingerprint": "measured-fp"}
    elif action == "copy_prompt":
        assert view["next_action"]["round_dir"] == recent[program]["round_dir"]
    elif action == "declare_speaker":
        assert view["next_action"]["endpoint"] == SPEAKER_SETUP_PAGE_PATH
        assert view["next_action"]["method"] == "GET"
    _assert_household_safe(view["next_action"]["label"], "action")
    assert {"driver_values", "driver_checks"} <= view.keys()


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
    assert view["next_action"]["id"] == ("apply_candidate" if displaced else "save_driver_values")
    assert view["next_action"]["enabled"] is True


def test_loaded_commissioning_view_uses_banked_rounds(monkeypatch, tmp_path):
    topology, _ = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    profile = _applied_anchor(layers=())
    identities = []

    def recent(identity):
        identities.append(identity)
        return {"speaker": {"round_dir": "/bank/speaker", "started_at": parse_utc_iso(profile["applied_at"]) + 1}}

    monkeypatch.setattr(round_inputs, "latest_banked_rounds", recent)
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: profile)
    view = load_commissioning_view(topology)
    assert identities == [applied_identity(profile)]
    assert view["next_action"]["id"] == "copy_prompt"
    assert view["next_action"]["program"] == "speaker"
    assert view["next_action"]["round_dir"] == "/bank/speaker"


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("applied_fingerprint", [None, "saved-fp", "review-fp"])
def test_applied_identity_change_is_disclosed_without_parking_review(ready, applied_fingerprint):
    review = _applied_baseline_profile(
        status="ready_to_compile" if ready else "blocked",
        permissions={"may_compile": ready, "may_apply": False},
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
    assert view["review"]["may_apply"] is ready
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
