# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a envelope schema 8: the v2 conductor screen payloads.

Pins docs/crossover-measurement-productization-design.md §5.9 (the five-step
sequence), §5.10 (the four failure-screen templates, parameterized by reason
copy), the §5.2 VERIFY-fail one-default screen, and the volume_recovery screen
keyed on ``needs_recovery`` (the W2 gate ruling — never
``unresolved_volume_safety`` alone).

Owner ruling (2026-07-20): the human ``review_apply`` screen is gone from the
happy path. MEASURE accepted + not yet applied now renders the ``applying``
screen (no candidate review, no action — the conductor's own auto-apply is in
flight); the low-confidence trust gate rejects MEASURE itself
(``low_alignment_confidence``, rendered through the ordinary fix_and_retry
template at the ``measure`` step) instead of nudging a still-available Apply
button; and ``done`` is now the RESULT screen — plain outcome first, the
measured numbers in ``candidate_review`` for the wizard's collapsed expert
disclosure, Undo as the primary action.
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
    REASON_APPLY_FAILED,
    REASON_CLIPPED,
    REASON_CHANNEL_MAP_MISMATCH,
    REASON_LOW_ALIGNMENT_CONFIDENCE,
    REASON_NOISY_ROOM_LINEARITY,
    REASON_RELAY_TIMEOUT,
    REASON_SNR_FLOOR,
    REASON_USER_STOPPED,
    REASON_VERIFY_OUT_OF_TOLERANCE,
)

V2_STEP_IDS = ("speaker_setup", "microphone_check", "measure", "apply", "verify")


def _status(**v2) -> dict:
    return {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2,
    }


def _step_statuses(env: dict) -> dict[str, str]:
    return {step["id"]: step["status"] for step in env["steps"]}


# --- shape --------------------------------------------------------------------


def test_schema_8_and_v2_step_tuple():
    env = build_crossover_envelope_v2(_status(phase="check"))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 8
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
# + alignment_confidence + predicted_ripple_db + fingerprint. The renderer reads
# exactly this.
def _candidate_summary(**overrides) -> dict:
    base = {
        "fingerprint": "fp-123",
        "program_id": "prog-9",
        "trims_db": {"woofer": -3.1, "tweeter": 0.0},
        "alignment": {"delay_us": 250.0, "delay_role": "woofer", "polarity": "invert"},
        "alignment_confidence": 0.82,
        "predicted_ripple_db": 1.4,
    }
    base.update(overrides)
    return base


# --- APPLYING (owner ruling, 2026-07-20: no human control page) -----------------


def test_applying_phase_has_no_action_and_no_candidate_review():
    """The conductor's own auto-apply is in flight — a brief machine-paced
    wait, not a human decision screen. No Apply button, no candidate detail
    (that lives on the RESULT screen once applied)."""
    env = build_crossover_envelope_v2(_status(
        phase="applying",
        candidate=_candidate_summary(),
    ))
    assert env["screen"] == "applying"
    assert env["next_action"] is None
    assert env["candidate_review"] is None
    assert _step_statuses(env)["apply"] == "active"
    assert "apply" in env["verdict_text"].lower()


def test_low_alignment_confidence_rejects_at_the_measure_step():
    """Owner ruling (2026-07-20): the former review-screen nudge is now a hard
    MEASURE-phase gate. The household never sees a candidate to judge — just
    guidance to re-measure, rendered through the ordinary fix_and_retry
    template at the ``measure`` step (never ``applying``, since no candidate
    was ever built)."""
    env = build_crossover_envelope_v2(_status(
        phase="measure", failure={"code": REASON_LOW_ALIGNMENT_CONFIDENCE},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_LOW_ALIGNMENT_CONFIDENCE].message
    assert "mic" in env["verdict_text"].lower() or "microphone" in env["verdict_text"].lower()
    assert env["next_action"]["id"] == "retry"
    assert _step_statuses(env)["measure"] == "active"


def test_apply_failed_renders_fix_and_retry_at_the_apply_step():
    """A TERMINAL auto-apply failure surfaces through the ordinary generic
    failure branch (phase stays "applying" — MEASURE accepted, never
    applied), rendering at the "apply" step with the honest generic message.

    This is a pure RENDERING test (given phase="applying" as an input, not
    derived from real persistence) — reachability of exactly this input in
    production is separately pinned by
    test_correction_crossover_v2_endpoints.py::test_apply_failure_keeps_measure_accepted_through_the_real_persist_path
    (an adversarial review, 2026-07-20, found the prior version of this
    module untested that _persist_terminal_failure actually produces this
    phase for an apply failure rather than resetting to "check")."""
    env = build_crossover_envelope_v2(_status(
        phase="applying", failure={"code": REASON_APPLY_FAILED},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_APPLY_FAILED].message
    assert _step_statuses(env)["apply"] == "active"
    assert env["next_action"]["id"] == "retry"


def test_apply_failed_layers_the_specific_blocked_issue_as_an_extra_nudge():
    """The generic apply_failed headline is joined by the SPECIFIC blocked-
    apply issue (handle_v2_apply's own persisted apply_blocked) when one is
    available — the household gets both the honest generic outcome and,
    when known, the concrete cause. Pure rendering test — see the module
    note above test_apply_failed_renders_fix_and_retry_at_the_apply_step for
    where reachability through real persistence is pinned."""
    env = build_crossover_envelope_v2(_status(
        phase="applying",
        failure={"code": REASON_APPLY_FAILED},
        apply_blocked={
            "id": "measured_candidate_preset_mismatch",
            "message": "the measured candidate no longer matches the saved crossover",
        },
    ))
    assert env["screen"] == "fix_and_retry"
    codes = [n["code"] for n in env["nudges"]]
    assert REASON_APPLY_FAILED in codes
    assert "measured_candidate_preset_mismatch" in codes
    texts = [n["text"] for n in env["nudges"]]
    assert "the measured candidate no longer matches the saved crossover" in texts


def test_apply_failed_has_no_extra_nudge_when_nothing_is_blocked():
    env = build_crossover_envelope_v2(_status(
        phase="applying", failure={"code": REASON_APPLY_FAILED},
    ))
    assert len(env["nudges"]) == 1
    assert env["nudges"][0]["code"] == REASON_APPLY_FAILED


def test_apply_blocked_at_a_non_apply_step_gets_no_extra_nudge():
    """The apply_blocked merge is scoped to the "apply" step only — an
    unrelated stale apply_blocked value sitting in durable state must not
    bleed into a totally different failure screen."""
    env = build_crossover_envelope_v2(_status(
        phase="check",
        failure={"code": REASON_SNR_FLOOR},
        apply_blocked={"id": "stale", "message": "stale detail"},
    ))
    assert len(env["nudges"]) == 1
    assert env["nudges"][0]["code"] == REASON_SNR_FLOOR


def test_verify_phase_screen():
    env = build_crossover_envelope_v2(_status(phase="verify"))
    assert env["screen"] == "verify"
    assert env["next_action"] is None
    assert _step_statuses(env)["verify"] == "active"


# --- done / RESULT screen (owner ruling, 2026-07-20) ----------------------------


def test_done_is_the_result_screen_plain_outcome_first():
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    assert env["screen"] == "done"
    assert set(_step_statuses(env).values()) == {"done"}
    assert env["progress"] == {"position": 5, "total": 5}
    assert "tuned" in env["verdict_text"].lower()
    assert "undo" in env["verdict_text"].lower()
    assert any(n["code"] == "crossover_v2_verified" for n in env["nudges"])


def test_done_gives_undo_the_primary_action_and_continue_as_alternate():
    """Undo prominent (owner ruling): the PRIMARY button is Undo, not
    Continue — the household's safety net is the most visible thing on the
    screen, not an afterthought."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    action = env["next_action"]
    assert action["id"] == "verify_undo"
    assert action["endpoint"] == "/correction/crossover/v2/restore"
    alternates = {a["id"]: a for a in env["alternate_actions"]}
    assert alternates["room"]["href"] == "/correction/room/"


def test_done_candidate_review_carries_the_measured_numbers():
    """The former review screen's candidate display shape is reused, unchanged,
    for the RESULT screen's collapsed expert disclosure — trims, delay,
    polarity, confidence, AND ripple (new: threaded through for the expert
    disclosure, not previously exposed on any screen)."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    review = env["candidate_review"]
    assert review["trims"] == [
        {"role": "woofer", "attenuation_db": -3.1},
        {"role": "tweeter", "attenuation_db": 0.0},
    ]
    assert review["delay"] == {"role": "woofer", "delay_ms": 0.25}
    assert review["polarity"] == "invert"
    assert review["confidence"] == 0.82
    assert review["ripple_db"] == 1.4
    assert review["fingerprint"] == "fp-123"


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


def test_user_stopped_renders_session_restart_with_honest_copy():
    """A deliberate phone Stop is not a relay-transport death (gotcha #18) —
    same session_restart template/action shape, but the copy must not claim
    a timeout that never happened."""
    env = build_crossover_envelope_v2(_status(
        phase="measure", failure={"code": REASON_USER_STOPPED},
    ))
    assert env["screen"] == "session_restart"
    assert env["next_action"]["id"] == "restart_session"
    assert "stopped" in env["verdict_text"].lower()
    assert "timed out" not in env["verdict_text"].lower()


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
    once the crossover is durably applied, the same code must render
    verify_fail instead. ``applied=True`` here is the REAL state fact a
    production status always carries whenever phase is genuinely "verify"
    (see test_applied_true_forces_verify_fail_regardless_of_phase for the
    adversarial-review case where phase and applied disagree)."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, failure={"code": REASON_AGC_BEHAVIORAL_FAIL},
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
    session_restart) gets the same applied override -- ANY failure code
    surfacing once genuinely applied is entitled to the Undo affordance."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, failure={"code": REASON_RELAY_TIMEOUT},
    ))
    assert env["screen"] == "verify_fail"


def test_verify_phase_unknown_code_renders_verify_fail_too():
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, failure={"code": "some_future_code"},
    ))
    assert env["screen"] == "verify_fail"
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Undo (restore previous sound)" in labels


def test_applied_true_forces_verify_fail_regardless_of_phase():
    """Second adversarial-review pass (2026-07-20, "interleaving A"):
    ``_persist_terminal_failure`` for a NON-apply-failed code (e.g.
    ``user_stopped``) can land WHILE the auto-apply transaction is still
    mid-flight — at that instant ``applied`` reads False, so the reset
    (§5.6, scoped away from ``apply_failed`` only) clears ``accepted_phases``.
    If the auto-apply's OWN success then lands moments later, the final
    durable state is applied=True with accepted_phases still cleared —
    ``_phase_from_state`` resolves that combination to PHASE_CHECK, not
    PHASE_VERIFY. The render must not trust that phase derivation: keying
    on the RAW ``applied`` state fact catches this even when phase says
    "check" and active_step says "microphone_check"."""
    env = build_crossover_envelope_v2(_status(
        phase="check", applied=True, failure={"code": REASON_USER_STOPPED},
    ))
    assert env["screen"] == "verify_fail"
    assert "already applied" in env["verdict_text"].lower()
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Undo (restore previous sound)" in labels


def test_applied_false_with_verify_phase_does_not_force_verify_fail():
    """Defensive converse of the above: if ``applied`` is explicitly False,
    the override must not fire even if some other field claims phase
    "verify" — the state fact is authoritative, not a hint."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=False, failure={"code": REASON_AGC_BEHAVIORAL_FAIL},
    ))
    assert env["screen"] == "fix_and_retry"


@pytest.mark.parametrize("code,template", [
    (code, spec.template) for code, spec in REASON_REGISTRY.items()
])
def test_every_registry_code_renders_without_error(code, template):
    env = build_crossover_envelope_v2(_status(phase="measure", failure={"code": code}))
    assert env["schema_version"] == 8
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
