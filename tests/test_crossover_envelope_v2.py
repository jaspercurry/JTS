# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a envelope schema 7: the v2 conductor screen payloads.

Pins docs/crossover-measurement-productization-design.md §5.9 (the five-step
sequence), §5.10 (the four failure-screen templates, parameterized by reason
copy), the §5.2 VERIFY-fail one-default screen, and the volume_recovery screen
keyed on ``needs_recovery`` (the W2 gate ruling — never
``unresolved_volume_safety`` alone).
"""
from __future__ import annotations

import pytest

from jasper.active_speaker.crossover_envelope_v2 import (
    CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
    build_crossover_envelope_v2,
)
from jasper.active_speaker.crossover_v2_flow import (
    REASON_REGISTRY,
    REASON_CLIPPED,
    REASON_CHANNEL_MAP_MISMATCH,
    REASON_RELAY_TIMEOUT,
    REASON_SNR_FLOOR,
    REASON_VERIFY_OUT_OF_TOLERANCE,
)

V2_STEP_IDS = ("speaker_setup", "microphone_check", "measure", "review_apply", "verify")


def _status(**v2) -> dict:
    return {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2,
    }


def _step_statuses(env: dict) -> dict[str, str]:
    return {step["id"]: step["status"] for step in env["steps"]}


# --- shape --------------------------------------------------------------------


def test_schema_7_and_v2_step_tuple():
    env = build_crossover_envelope_v2(_status(phase="check"))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 7
    assert env["flow"] == "v2"
    assert tuple(step["id"] for step in env["steps"]) == V2_STEP_IDS


def test_inactive_speaker_gets_not_applicable():
    env = build_crossover_envelope_v2({"active": False})
    assert env["screen"] == "not_applicable"
    assert env["active"] is False
    assert env["next_action"]["href"] == "/correction/room/"


def test_setup_not_ready_blocks_before_any_capture():
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "blocked"},
        "crossover_v2": {"phase": "check"},
    })
    assert env["screen"] == "speaker_setup"
    assert env["next_action"]["href"] == "/sound/"
    assert _step_statuses(env)["speaker_setup"] == "active"


# --- the phase screens -----------------------------------------------------------


def test_check_phase_screen():
    env = build_crossover_envelope_v2(_status(phase="check"))
    assert env["screen"] == "microphone_check"
    assert env["next_action"]["id"] == "start_v2_session"
    statuses = _step_statuses(env)
    assert statuses["speaker_setup"] == "done"
    assert statuses["microphone_check"] == "active"
    assert env["progress"] == {"position": 2, "total": 5}


def test_measure_phase_is_phone_driven():
    env = build_crossover_envelope_v2(_status(phase="measure"))
    assert env["screen"] == "measure"
    assert env["next_action"] is None
    assert _step_statuses(env)["measure"] == "active"


def test_review_apply_carries_candidate_fingerprint():
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate={"fingerprint": "fp-123", "trims": {"woofer": -3.1}},
    ))
    assert env["screen"] == "review_apply"
    action = env["next_action"]
    assert action["endpoint"] == "/correction/crossover/apply"
    assert action["body"]["tuning_owner"] == "automatic"
    assert action["body"]["expected_candidate_fingerprint"] == "fp-123"
    assert env["candidate_review"]["fingerprint"] == "fp-123"
    assert _step_statuses(env)["review_apply"] == "active"


def test_verify_phase_screen():
    env = build_crossover_envelope_v2(_status(phase="verify"))
    assert env["screen"] == "verify"
    assert env["next_action"] is None
    assert _step_statuses(env)["verify"] == "active"


def test_done_marks_every_step_done():
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"},
    ))
    assert env["screen"] == "done"
    assert set(_step_statuses(env).values()) == {"done"}
    assert env["progress"] == {"position": 5, "total": 5}
    assert env["next_action"]["href"] == "/correction/room/"
    assert any(n["code"] == "crossover_v2_verified" for n in env["nudges"])


# --- volume recovery (W2 gate ruling) ----------------------------------------------


def test_volume_recovery_keys_on_needs_recovery_not_unresolved():
    """A crash-hydrated active plan surfaces NO unresolved payload but still
    needs draining — the screen must key on needs_recovery alone."""
    env = build_crossover_envelope_v2(_status(phase="check", needs_recovery=True))
    assert env["screen"] == "volume_recovery"
    assert env["next_action"]["endpoint"] == "/correction/crossover/recover-volume"
    # And needs_recovery false ⇒ no recovery screen even with a phase set.
    env = build_crossover_envelope_v2(_status(phase="check", needs_recovery=False))
    assert env["screen"] == "microphone_check"


# --- the four §5.10 failure templates ----------------------------------------------


def test_silent_auto_retry_template_has_banner_and_no_decision_action():
    env = build_crossover_envelope_v2(_status(
        phase="measure", failure={"code": REASON_CLIPPED},
    ))
    # No decision screen: stays on the phase step with an informational banner.
    assert env["screen"] == "measure"
    assert env["next_action"] is None
    assert env["verdict_text"] == REASON_REGISTRY[REASON_CLIPPED].banner
    assert env["nudges"] == [{
        "code": REASON_CLIPPED, "severity": "info",
        "text": REASON_REGISTRY[REASON_CLIPPED].banner,
    }]


def test_fix_and_retry_template():
    env = build_crossover_envelope_v2(_status(
        phase="check", failure={"code": REASON_SNR_FLOOR},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_SNR_FLOOR].message
    assert env["next_action"]["id"] == "retry"


def test_hard_stop_template():
    env = build_crossover_envelope_v2(_status(
        phase="check", failure={"code": REASON_CHANNEL_MAP_MISMATCH},
    ))
    assert env["screen"] == "hard_stop"
    assert "wiring" in env["verdict_text"]
    assert env["next_action"]["href"] == "/sound/"


def test_session_restart_template():
    env = build_crossover_envelope_v2(_status(
        phase="measure", failure={"code": REASON_RELAY_TIMEOUT},
    ))
    assert env["screen"] == "session_restart"
    assert env["next_action"]["id"] == "restart_session"
    # A restarted session begins at CHECK (evidence invalidated).
    assert _step_statuses(env)["microphone_check"] == "active"


def test_verify_fail_one_default_screen():
    """§5.2: one default ("Try again") + Undo; the explicit trio behind the
    expert disclosure."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
    ))
    assert env["screen"] == "verify_fail"
    assert env["next_action"]["label"] == "Try again"
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Undo (restore previous sound)" in labels
    expert = [a for a in env["alternate_actions"] if a.get("expert")]
    assert [a["id"] for a in expert] == ["verify_remeasure"]
    # The applied graph stays in force — undo routes through the existing
    # apply-rollback path.
    undo = next(a for a in env["alternate_actions"] if a["id"] == "verify_undo")
    assert undo["endpoint"] == "/correction/crossover/restore"


def test_unknown_failure_code_still_renders_a_retry_screen():
    env = build_crossover_envelope_v2(_status(
        phase="check", failure={"code": "some_future_code"},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["next_action"] is not None


@pytest.mark.parametrize("code,template", [
    (code, spec.template) for code, spec in REASON_REGISTRY.items()
])
def test_every_registry_code_renders_without_error(code, template):
    env = build_crossover_envelope_v2(_status(phase="measure", failure={"code": code}))
    assert env["schema_version"] == 7
    assert env["screen"]
    assert env["verdict_text"]
