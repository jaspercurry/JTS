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
    REASON_AGC_BEHAVIORAL_FAIL,
    REASON_CLIPPED,
    REASON_CHANNEL_MAP_MISMATCH,
    REASON_NOISY_ROOM_LINEARITY,
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


# A realistic persisted candidate summary (jasper.web.correction_crossover_v2's
# _candidate_summary shape): trims_db + alignment (delay_us/delay_role/polarity)
# + alignment_confidence + fingerprint. The renderer reads exactly this.
def _candidate_summary(**overrides) -> dict:
    base = {
        "fingerprint": "fp-123",
        "program_id": "prog-9",
        "trims_db": {"woofer": -3.1, "tweeter": 0.0},
        "alignment": {"delay_us": 250.0, "delay_role": "woofer", "polarity": "invert"},
        "alignment_confidence": 0.82,
    }
    base.update(overrides)
    return base


def test_review_apply_carries_candidate_fingerprint():
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate=_candidate_summary(),
    ))
    assert env["screen"] == "review_apply"
    action = env["next_action"]
    assert action["endpoint"] == "/correction/crossover/v2/apply"
    assert action["body"]["expected_candidate_fingerprint"] == "fp-123"
    # Apply is the primary action and must render even while the phone relay is
    # still in flight (the "waiting for apply" hold) — W6.10 blocker #2.
    assert action["show_during_relay"] is True
    assert env["candidate_review"]["fingerprint"] == "fp-123"
    assert _step_statuses(env)["review_apply"] == "active"


def test_review_apply_candidate_review_is_display_shape():
    """W6.10 blocker #2: candidate_review carries the plain-language rows the
    renderer displays (trims / delay / polarity), derived from _candidate_summary
    — NOT the raw summary the generic renderer could not read."""
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate=_candidate_summary(),
    ))
    review = env["candidate_review"]
    assert review["trims"] == [
        {"role": "woofer", "attenuation_db": -3.1},
        {"role": "tweeter", "attenuation_db": 0.0},
    ]
    assert review["delay"] == {"role": "woofer", "delay_ms": 0.25}
    assert review["polarity"] == "invert"
    assert review["confidence"] == 0.82
    assert review["fingerprint"] == "fp-123"


def test_review_apply_candidate_review_trims_only_candidate():
    """A trims-only candidate (no measured delay/polarity) still renders its
    trim rows; delay/polarity are absent, not zero-valued noise."""
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate=_candidate_summary(
            alignment={"delay_us": None, "delay_role": None, "polarity": None},
            alignment_confidence=None,
        ),
    ))
    review = env["candidate_review"]
    assert [row["role"] for row in review["trims"]] == ["woofer", "tweeter"]
    assert review["delay"] is None
    assert review["polarity"] is None
    assert review["confidence"] is None


def test_review_apply_surfaces_last_blocked_apply_as_a_nudge():
    """Finding N (b): a blocked apply must not be a silent dead end. The
    endpoint persists the last blocked-apply issue into the durable v2
    state; the envelope surfaces it as a nudge on the SAME review_apply
    screen (no new template — the household stays where the Apply button
    already is, with an explanation instead of nothing happening)."""
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate={"fingerprint": "fp-123", "trims": {"woofer": -3.1}},
        apply_blocked={
            "id": "measured_candidate_preset_mismatch",
            "message": "the reviewed measured candidate no longer equals the saved crossover",
        },
    ))
    assert env["screen"] == "review_apply"
    assert env["nudges"] == [{
        "code": "measured_candidate_preset_mismatch",
        "severity": "warn",
        "text": "the reviewed measured candidate no longer equals the saved crossover",
    }]
    # The Apply action itself is untouched — the household can still retry.
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/apply"


def test_review_apply_has_no_nudge_when_nothing_is_blocked():
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate={"fingerprint": "fp-123", "trims": {"woofer": -3.1}},
    ))
    assert env["nudges"] == []


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


def test_noisy_room_linearity_renders_its_own_fix_and_retry_copy():
    """W6.12: a distinct reason from agc_behavioral_fail, naming the room
    instead of the phone's microphone — same template, different verdict
    text, so the household gets an honest fix (quiet the room) rather than a
    misdirected one (re-allow the mic)."""
    env = build_crossover_envelope_v2(_status(
        phase="check", failure={"code": REASON_NOISY_ROOM_LINEARITY},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_NOISY_ROOM_LINEARITY].message
    assert "room" in env["verdict_text"]
    assert "microphone" not in env["verdict_text"]
    assert env["next_action"]["id"] == "retry"


def test_hard_stop_template():
    env = build_crossover_envelope_v2(_status(
        phase="check", failure={"code": REASON_CHANNEL_MAP_MISMATCH},
    ))
    assert env["screen"] == "hard_stop"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_CHANNEL_MAP_MISMATCH].message
    # Fix 3 (W6.4): name both honest causes -- wiring AND a noisy/quiet room --
    # rather than blaming wiring unconditionally (§5.10 one-reason/one-action
    # shape still holds: one screen, one pair of fix actions).
    assert "wiring" in env["verdict_text"]
    assert "noisy" in env["verdict_text"]
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
    assert undo["endpoint"] == "/correction/crossover/v2/restore"
    # W6.12: Undo and Re-measure must survive the JS action-row's relay-in-
    # flight gate (a real window right after a failed capture, before the
    # phone side has fully wound down) — the same show_during_relay escape
    # hatch W6.10 gave the review screen's Apply. "Try again" starts a brand
    # new relay session, so it deliberately does NOT carry the flag.
    remeasure = next(
        a for a in env["alternate_actions"] if a["id"] == "verify_remeasure"
    )
    assert undo["show_during_relay"] is True
    assert remeasure["show_during_relay"] is True
    assert "show_during_relay" not in env["next_action"]


def test_unknown_failure_code_still_renders_a_retry_screen():
    env = build_crossover_envelope_v2(_status(
        phase="check", failure={"code": "some_future_code"},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["next_action"] is not None


# --- W6.7 ruling 3: VERIFY-phase failures always get the verify_fail screen ------


def test_verify_phase_agc_failure_renders_verify_fail_not_fix_and_retry():
    """The run-7 hardware bug: an agc_behavioral_fail during VERIFY (post-
    apply) rendered fix_and_retry and displaced the verify_fail screen's Undo
    affordance. REASON_AGC_BEHAVIORAL_FAIL's OWN registry template is
    fix_and_retry (correct for CHECK/MEASURE, where nothing is applied yet);
    once phase is verify, the applied graph is already live, so the same
    code must render verify_fail instead."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_AGC_BEHAVIORAL_FAIL},
    ))
    assert env["screen"] == "verify_fail"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_AGC_BEHAVIORAL_FAIL].message
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Undo (restore previous sound)" in labels
    undo = next(a for a in env["alternate_actions"] if a["id"] == "verify_undo")
    assert undo["endpoint"] == "/correction/crossover/v2/restore"


def test_check_phase_agc_failure_still_renders_its_normal_template():
    """The SAME code at CHECK (nothing applied yet) is untouched — still
    fix_and_retry, no Undo affordance to offer."""
    env = build_crossover_envelope_v2(_status(
        phase="check", failure={"code": REASON_AGC_BEHAVIORAL_FAIL},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["alternate_actions"] == []


def test_verify_phase_relay_timeout_also_renders_verify_fail():
    """A non-agc code (REASON_RELAY_TIMEOUT's own template is
    session_restart) gets the same VERIFY-phase override -- ANY failure code
    surfacing post-apply is entitled to the Undo affordance."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_RELAY_TIMEOUT},
    ))
    assert env["screen"] == "verify_fail"


def test_verify_phase_unknown_code_renders_verify_fail_too():
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": "some_future_code"},
    ))
    assert env["screen"] == "verify_fail"
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Undo (restore previous sound)" in labels


# --- W6.7 ruling 4: low-confidence nudge on review_apply -------------------------


def test_review_apply_nudges_on_low_alignment_confidence():
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate={"fingerprint": "fp-123", "alignment_confidence": 0.3},
    ))
    assert env["screen"] == "review_apply"
    codes = [n["code"] for n in env["nudges"]]
    assert "crossover_v2_alignment_low_confidence" in codes
    # Apply is NOT blocked -- informed consent, not a gate.
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/apply"


def test_review_apply_no_nudge_when_confidence_is_high():
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate={"fingerprint": "fp-123", "alignment_confidence": 0.9},
    ))
    assert env["nudges"] == []


def test_review_apply_no_nudge_when_confidence_is_absent():
    """A legacy/trims-only candidate with no alignment estimate at all must
    not nudge -- only a KNOWN low confidence value nudges."""
    env = build_crossover_envelope_v2(_status(
        phase="review_apply",
        candidate={"fingerprint": "fp-123"},
    ))
    assert env["nudges"] == []


@pytest.mark.parametrize("code,template", [
    (code, spec.template) for code, spec in REASON_REGISTRY.items()
])
def test_every_registry_code_renders_without_error(code, template):
    env = build_crossover_envelope_v2(_status(phase="measure", failure={"code": code}))
    assert env["schema_version"] == 7
    assert env["screen"]
    assert env["verdict_text"]


# --- W6.1 Finding D: the v2 relay slot is visible in the envelope ----------------


def test_envelope_carries_relay_block_awaiting_and_after_failure():
    """The v2 envelope threads status['relay'] into BOTH the awaiting-phone
    screen and the failure screen, so a page reload keeps the tap link and the
    failure copy reaches the household (Finding D — the slot was invisible)."""
    from jasper.active_speaker.crossover_v2_flow import REASON_PROGRAM_UNPLAYABLE

    relay = {"tap_link": "https://capture.test/#s=cap_x", "status": "awaiting_phone"}

    awaiting = build_crossover_envelope_v2({**_status(phase="check"), "relay": relay})
    assert awaiting["relay"] == relay

    failed = build_crossover_envelope_v2({
        **_status(phase="check", failure={"code": REASON_PROGRAM_UNPLAYABLE}),
        "relay": relay,
    })
    assert failed["screen"] == "hard_stop"
    assert failed["relay"] == relay
    assert "safe limits" in failed["verdict_text"]
