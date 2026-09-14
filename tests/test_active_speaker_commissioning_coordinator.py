# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

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
    assert view["combined_groups"] == []
    if action == "apply_candidate":
        assert view["next_action"]["body"] == {"expected_candidate_fingerprint": "measured-fp"}
    assert {"driver_values", "driver_target_proof", "driver_checks", "summed_validation", "output_identity"} <= view.keys()


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("applied_fingerprint", [None, "saved-fp", "review-fp"])
def test_applied_identity_change_is_disclosed_without_parking_review(ready, applied_fingerprint):
    review = _applied_baseline_profile(
        status="ready_to_compile" if ready else "blocked",
        permissions={"may_compile": ready, "may_apply": False},
        issues=[] if ready else [{"severity": "blocker", "code": "compose_refused"}],
    )
    applied = {**_applied_anchor(), "candidate_fingerprint": applied_fingerprint}
    view = build_commissioning_view(
        _topology(), design_draft=_ready_design(), crossover_preview=_ready_preview(),
        first_experiment={"candidate_fingerprint": "measured-fp"}, baseline_profile=review,
        applied_profile=applied if applied_fingerprint else None,
    )
    if applied_fingerprint:
        assert view["status"] == "applied"
        assert view["applied_profile"]["candidate_fingerprint"] == applied_fingerprint
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
