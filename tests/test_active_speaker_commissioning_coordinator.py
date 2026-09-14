# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import re
from dataclasses import replace

from jasper.active_speaker import baseline_profile, commissioning_experiment
from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.commissioning_coordinator import load_commissioning_view
from jasper.cli.doctor import active_speaker as doctor
from jasper.doctor_contract import check_row
from jasper.web import correction_crossover_v2_status as v2status
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
        "driver_safety_profile_evaluation": {"confirmed_and_current": True},
        "summary": {
            "missing_driver_info_roles": [],
            "missing_crossover_candidate_pairs": [],
        },
    }


def _ready_preview() -> dict:
    return {
        "kind": "jts_active_speaker_crossover_preview",
        "status": "ready_for_protected_staging",
        "permissions": {"may_prepare_protected_startup_config": True},
    }


def _applied_anchor(basename: str = "candidate_f7e9.yml") -> dict:
    return {
        "status": "applied",
        "applied_at": "2026-08-30T01:33:34Z",
        "linearization": {"tweeter": [{"type": "Peaking"}]},
        "blend_correction": [{"type": "Peaking"}],
        "config": {
            "path": "/var/lib/camilladsp/configs/" + basename,
            "basename": basename,
            "exists": True,
        },
    }


def _applied_baseline_profile(**overrides) -> dict:
    return {"status": "ready_to_compile", "candidate_fingerprint": "review-fp",
            "permissions": {"may_compile": True, "may_apply": False}, "issues": [], **overrides}


@pytest.mark.parametrize("stage,current,status,action", [
    ("declaration", "research", "needs_driver_values", "save_driver_values"),
    ("safety", "research", "needs_driver_safety_profile", "save_driver_values"),
    ("experiment", "experiment", "needs_first_experiment", "run_speaker_program"),
    ("apply", "profile", "ready_to_save_profile", "apply_candidate"),
    ("applied", "profile", "applied", None),
    ("displaced", "profile", "ready_to_save_profile", "apply_candidate"),
    ("passive", "layout", "not_required", None),
])
def test_commissioning_is_declaration_safety_experiment_apply(stage, current, status, action):
    draft = _ready_design()
    if stage == "declaration":
        draft = {}
    elif stage == "safety":
        draft["driver_safety_profile_evaluation"]["confirmed_and_current"] = False
    view = build_commissioning_view(
        passive_stereo_output_topology() if stage == "passive" else _topology(),
        design_draft=draft, crossover_preview=_ready_preview(), baseline_profile=_applied_baseline_profile(),
        applied_profile=_applied_anchor() if stage in {"applied", "displaced"} else None,
        applied_profile_verdict=APPLIED_PROFILE_DISPLACED if stage == "displaced" else "",
        first_experiment={"candidate_fingerprint": "measured-fp"} if stage in {"apply", "displaced"} else None,
    )
    assert [step["id"] for step in view["steps"]] == ["layout", "research", "experiment", "profile"]
    assert sum(step["status"] == "active" for step in view["steps"]) <= 1
    assert view["current_step"] == current
    assert view["status"] == status
    assert view["next_action"].get("id") == action
    assert "command" not in view["next_action"]
    assert view["combined_groups"] == []
    if action == "apply_candidate":
        assert view["next_action"]["body"] == {"expected_candidate_fingerprint": "measured-fp"}
    assert {"driver_values", "driver_target_proof", "driver_checks", "summed_validation", "output_identity"} <= view.keys()


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
    for action in (view["next_action"], view["secondary_action"]):
        _assert_household_safe(action.get("label", ""), "action")
        assert "command" not in action


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
def test_applied_identity_is_shared_by_status_commissioning_and_doctor(monkeypatch, record):
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
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: applied)
    monkeypatch.setattr(doctor.evidence, "active_speaker_setup_status", lambda: {
        "protected_profile": {"layer_a_binding": {"matches": True}} if applied else None,
    })
    assert applied_identity(applied) == expected
    assert v2status.crossover_v2_status_block()["applied"] == expected
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
