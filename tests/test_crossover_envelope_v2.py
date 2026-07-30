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
    _PHASE_STEP,
    build_crossover_envelope_v2,
)
from jasper.active_speaker.crossover_v2_flow import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_REVIEW,
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
    REASON_VERIFY_LEVEL_SHIFT,
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
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 11
    assert env["flow"] == "v2"
    assert tuple(step["id"] for step in env["steps"]) == V2_STEP_IDS


def test_legacy_env_still_serves_v2_envelope(monkeypatch):
    """W5b retired the ``JASPER_CROSSOVER_FLOW`` selector and the legacy flow —
    v2 is the only flow now. A box carrying a stale
    ``JASPER_CROSSOVER_FLOW=legacy`` from before the selector was deleted must
    still serve the v2 (schema 8) envelope through the ``build_crossover_envelope``
    entry point, not crash or fall back to a deleted legacy path."""
    from jasper.active_speaker.crossover_envelope import build_crossover_envelope

    monkeypatch.setenv("JASPER_CROSSOVER_FLOW", "legacy")
    env = build_crossover_envelope(_status(phase="check"))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 11
    assert env["flow"] == "v2"


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
    # Flow-simplification §3: on a fresh topology (no applied_crossover),
    # Full is the recommended (primary) tier — the first-ever-commission
    # case, never a silent express default.
    assert env["next_action"]["id"] == "start_v2_session_full"
    statuses = _step_statuses(env)
    assert statuses["speaker_setup"] == "done"
    assert statuses["microphone_check"] == "active"
    assert env["progress"] == {"position": 2, "total": 5}
    # Item 5a (#1605): the placement guidance names the load-bearing facts —
    # distance and tweeter height. Substring guards, not exact wording, so copy
    # can still be refined.
    #
    # AMENDED for the spatial cloud (flat-linearization PR-3b, round-1 review
    # blocker B1): this screen used to promise "keep it in that one spot for
    # the whole measurement", which the cloud makes FALSE on the very first
    # screen the household reads. What replaces it is not silence — the
    # starting spot is now named as the mark, and the guided moves are
    # disclosed here rather than sprung at the third capture.
    verdict = env["verdict_text"].lower()
    assert "1 m" in verdict
    assert "tweeter height" in verdict
    assert "mark" in verdict
    assert "guide you to" in verdict
    assert "whole measurement" not in verdict


# --- tier chooser (flow-simplification §3) --------------------------------------


def test_check_phase_offers_both_tiers_first_class():
    """Both tiers render every session — never a silent default. The choice
    posts ``{tier}`` to the same session-start endpoint the old single
    "Start measurement" button used."""
    env = build_crossover_envelope_v2(_status(phase="check"))
    actions = {a["id"]: a for a in [env["next_action"], *env["alternate_actions"]]}
    assert set(actions) == {"start_v2_session_full", "start_v2_session_express"}
    for action_id, tier in (
        ("start_v2_session_full", "full"),
        ("start_v2_session_express", "express"),
    ):
        action = actions[action_id]
        assert action["endpoint"] == "/correction/crossover/v2/session"
        assert action["body"] == {"tier": tier}


def test_check_phase_tier_durations_and_counts_are_derived_not_hand_written():
    """§1.1: the displayed minutes/counts must come from
    ``tier_display_info`` (built from the two plan shapes), never a
    hand-written prettier figure."""
    from jasper.active_speaker.crossover_v2_flow import tier_display_info

    info = tier_display_info()
    env = build_crossover_envelope_v2(_status(phase="check"))
    actions = {a["id"]: a for a in [env["next_action"], *env["alternate_actions"]]}
    full = actions["start_v2_session_full"]
    express = actions["start_v2_session_express"]
    # RE-DERIVED for the two-stage split (work order D7, PR-T4). The chooser
    # used to quote ONE capture count against one duration; after the split a
    # household picking a tier is picking TWO sessions with its own decision in
    # between, so the description states the per-stage counts and the
    # whole-journey duration. `capture_target` is still the sum of the two by
    # construction, which is what this asserts instead of a literal.
    assert str(info["full"]["estimated_minutes"]) in full["description"]
    assert str(info["full"]["stage1_captures"]) in full["description"]
    assert str(info["full"]["stage2_captures"]) in full["description"]
    assert str(info["express"]["estimated_minutes"]) in express["description"]
    assert str(info["express"]["stage1_captures"]) in express["description"]
    assert str(info["express"]["stage2_captures"]) in express["description"]
    for tier in ("full", "express"):
        assert (
            info[tier]["stage1_captures"] + info[tier]["stage2_captures"]
            == info[tier]["capture_target"]
        )
    # …and the interlude is NAMED, because it is the thing the split added and
    # a chooser that hid it would sell two sittings as one.
    assert "You decide whether to apply" in full["description"]
    assert "You decide whether to apply" in express["description"]
    # The one-line claims difference (§1.3): express confirms at the mark,
    # full re-checks at several spots around the mark. B2 fix (adversarial
    # review of PR #1780): "across the room" overclaimed past what the
    # post-apply cloud actually samples.
    assert "confirm the result at the mark" in express["description"]
    assert "re-check the result at several spots around the mark" in full["description"]


def test_check_phase_recommends_full_on_a_first_commission():
    """No applied_crossover at all — never measured before on this topology
    — recommends Full (§3)."""
    env = build_crossover_envelope_v2(_status(phase="check"))
    actions = {a["id"]: a for a in [env["next_action"], *env["alternate_actions"]]}
    assert actions["start_v2_session_full"]["recommended"] is True
    assert actions["start_v2_session_express"]["recommended"] is False
    assert env["next_action"]["id"] == "start_v2_session_full"


def test_check_phase_full_commissioned_recommends_quick():
    """S4 (coordinator ruling, adversarial review of PR #1780): Full
    recommended UNTIL a Full-tier commission has completed on this
    topology. An automatic crossover valid for THIS topology
    (``applied_crossover.owner == "automatic"``) AND the durable v2 state's
    own ``tier`` recording ``"full"`` — a completed Full commission — is
    exactly when Quick tune becomes recommended (a re-tune)."""
    status = {
        "active": True,
        "setup": {
            "active": True,
            "status": "ready",
            "applied_crossover": {"valid": True, "owner": "automatic"},
        },
        "crossover_v2": {"phase": "check", "tier": "full"},
    }
    env = build_crossover_envelope_v2(status)
    actions = {a["id"]: a for a in [env["next_action"], *env["alternate_actions"]]}
    assert actions["start_v2_session_express"]["recommended"] is True
    assert actions["start_v2_session_full"]["recommended"] is False
    assert env["next_action"]["id"] == "start_v2_session_express"


def test_check_phase_express_commissioned_still_recommends_full():
    """S4: an automatic crossover applied from a Quick-tune (Express)
    commission still recommends Full — the household has never actually
    walked the wider, comb-decorrelating cloud on this topology, so §1.3's
    HF-null mitigation keeps recommending it (never a silent express
    default just because SOMETHING is applied)."""
    status = {
        "active": True,
        "setup": {
            "active": True,
            "status": "ready",
            "applied_crossover": {"valid": True, "owner": "automatic"},
        },
        "crossover_v2": {"phase": "check", "tier": "express"},
    }
    env = build_crossover_envelope_v2(status)
    actions = {a["id"]: a for a in [env["next_action"], *env["alternate_actions"]]}
    assert actions["start_v2_session_full"]["recommended"] is True
    assert actions["start_v2_session_express"]["recommended"] is False
    assert env["next_action"]["id"] == "start_v2_session_full"


def test_check_phase_applied_automatic_with_unknown_tier_recommends_full():
    """S4: an applied automatic crossover with NO recorded tier (state
    written before tiers existed, or a legacy per-driver flow's measured
    result — N5a: "automatic" is not exclusively v2-measured) still
    recommends Full — the tier signal must say "full" explicitly, never be
    assumed from "something is applied"."""
    status = {
        "active": True,
        "setup": {
            "active": True,
            "status": "ready",
            "applied_crossover": {"valid": True, "owner": "automatic"},
        },
        "crossover_v2": {"phase": "check"},
    }
    env = build_crossover_envelope_v2(status)
    assert env["next_action"]["id"] == "start_v2_session_full"


def test_check_phase_manual_applied_still_recommends_full():
    """A manually-authored applied crossover (never run through the guided
    v2 flow) is NOT a prior automatic commission — still recommend Full."""
    status = {
        "active": True,
        "setup": {
            "active": True,
            "status": "ready",
            "applied_crossover": {"valid": True, "owner": "manual"},
        },
        "crossover_v2": {"phase": "check"},
    }
    env = build_crossover_envelope_v2(status)
    assert env["next_action"]["id"] == "start_v2_session_full"


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
    # STAGE 2's entry point (two-stage work order D2, PR-T3). The measuring
    # session ended at the review screen and the household applied from there,
    # so the post-apply check is a NEW session somebody has to start — and
    # deliberately so, because the relay TTL begins ticking at open and the
    # household is still walking back to fetch the phone. It used to be None:
    # the same screen rendered mid-session while the phone drove it, and the
    # shared relay gate still suppresses this action while stage 2's own relay
    # is in flight.
    assert env["next_action"] == {
        "id": "verify_start",
        "label": "Check the result",
        "endpoint": "/correction/crossover/v2/verify",
        "body": {"stage": "post_apply"},
    }
    assert _step_statuses(env)["verify"] == "active"
    # Full's VERIFY anchor is followed by the post-apply cloud — no
    # express-only disclosure here.
    assert "only check" not in env["verdict_text"].lower()


def test_verify_phase_express_discloses_its_the_only_check():
    """Express (M=1) has no post-apply cloud — this VERIFY anchor is the
    WHOLE post-apply check, not the first of several (flow-simplification
    §1.3 degraded-claims table)."""
    env = build_crossover_envelope_v2(_status(phase="verify", tier="express"))
    assert env["screen"] == "verify"
    assert "only check" in env["verdict_text"].lower()
    assert "at the mark" in env["verdict_text"].lower()


def test_verify_phase_express_discloses_before_tuning_flatness_from_measure_cloud():
    """B1 fix (adversarial review of PR #1780): express's pre-apply cloud has
    already closed by the time this screen renders (it walks BEFORE VERIFY),
    so its flatness/carve-out disclosure is available here too, not just on
    the done screen — read from CLOUD_MEASURE (express's only cloud), never
    CLOUD_VERIFY (which express never produces)."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", tier="express", cloud=_cloud_measure_flatness_status(),
    ))
    details = env["expert_details"]
    assert details, "express's VERIFY screen must not sit on unread measure-block data"
    assert any("Measured before tuning:" in line for line in details)


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


def _cloud_verify_spec(passed: bool):
    return {
        PHASE_CLOUD_VERIFY: {
            "geometry_locked": False, "thin_evidence": False,
            "geometry_guidance": "", "spec_bands": [],
            "overall_passed": passed, "excluded_interval_count": 0,
            "flatness": {
                "max_db": -4.85, "max_hz": 11480.0,
                "max_band_hz": [8000.0, 16000.0], "tolerance_db": 2.5,
                "rms_db": 1.37, "n_bins": 900, "n_excluded": 0,
                "evaluable": True, "passed": passed,
            },
            "carve_outs": [],
        },
    }


def test_done_headline_states_an_out_of_spec_result_in_primary_copy():
    """PR-L4 item 7: the spec verdict gets a vote where it cannot be collapsed.

    The headline and the "Verified." badge both read the TRACKING comparator,
    which asks whether the speaker matched its own prediction — not whether it
    is flat. On 2026-07-27 the one instrument that compares the result to flat
    failed all three bands and reached only a line inside a collapsed
    disclosure, so a household read "Your speaker is tuned" over it."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"},
        candidate=_candidate_summary(), cloud=_cloud_verify_spec(False),
    ))
    verdict = env["verdict_text"].lower()
    assert "further from flat" in verdict
    assert "undo" in verdict
    # The badge stops claiming more than the evidence, and says which
    # instrument passed rather than dropping the distinction.
    codes = {n["code"] for n in env["nudges"]}
    assert codes == {"crossover_v2_out_of_spec"}
    assert all(n["severity"] == "warn" for n in env["nudges"])
    # The numbers still ride the disclosure — the verdict moved, not the data.
    assert env["expert_details"]


def test_done_headline_is_unchanged_when_the_spec_passed():
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"},
        candidate=_candidate_summary(), cloud=_cloud_verify_spec(True),
    ))
    assert "further from flat" not in env["verdict_text"].lower()
    assert {n["code"] for n in env["nudges"]} == {"crossover_v2_verified"}


def test_done_headline_is_unchanged_when_no_spec_verdict_exists():
    """Absence of a verdict is not a failing one. Express never produces a
    post-apply cloud, and manufacturing a caveat out of a missing measurement
    would be its own dishonesty."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    assert "further from flat" not in env["verdict_text"].lower()
    assert {n["code"] for n in env["nudges"]} == {"crossover_v2_verified"}


def test_a_level_mismatch_caveats_the_pass_screen():
    """#1811 SF1: a non-rollback probe finding must not render as a clean pass.

    ``level_mismatch`` is deliberately NOT a rollback — reverting a household's
    correction over a gap in our own offset accounting would be a false
    accusation — but it means the probe never answered the shape question,
    while every other word on this screen says "Verified." The caveat rides
    BESIDE the badge rather than replacing it: the tracking comparator really
    did pass, and that claim stays true.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "pass",
            "delta_probe": {
                "verdict": "level_mismatch",
                "reason": "uncommanded_level_shift",
                "expected_offset_db": -22.458,
                "residual_offset_db": -4.0,
            },
        },
        candidate=_candidate_summary(),
    ))
    codes = {n["code"] for n in env["nudges"]}
    assert codes == {"crossover_v2_verified", "crossover_v2_level_mismatch"}
    caveat = next(
        n for n in env["nudges"] if n["code"] == "crossover_v2_level_mismatch"
    )
    assert caveat["severity"] == "warn"
    assert "could not confirm" in caveat["text"]
    # No hardware noun — the same copy rule the refusal reasons carry.
    assert not any(
        word in caveat["text"].lower()
        for word in ("tweeter", "woofer", "amplifier", "horn")
    )


def test_a_level_mismatch_rides_beside_an_out_of_spec_badge_too():
    """Two instruments, two claims, neither silencing the other."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "pass",
            "delta_probe": {"verdict": "level_mismatch"},
        },
        candidate=_candidate_summary(), cloud=_cloud_verify_spec(False),
    ))
    assert {n["code"] for n in env["nudges"]} == {
        "crossover_v2_out_of_spec", "crossover_v2_level_mismatch",
    }


def test_a_matched_probe_adds_no_caveat():
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "delta_probe": {"verdict": "matched"}},
        candidate=_candidate_summary(),
    ))
    assert {n["code"] for n in env["nudges"]} == {"crossover_v2_verified"}


def test_done_headline_says_so_when_the_result_was_never_graded():
    """PR-L4 item 4: a session ending applied-but-ungraded says the words.

    Surfaced rather than auto-restored — the work order allowed either, and a
    missing grade says nothing about the correction itself (the commonest way
    to reach it is a household closing the phone after the apply). Undo is
    already the primary button; what was missing was being told."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={}, candidate=_candidate_summary(),
        post_apply_grade={"state": "unverified", "graded": False},
    ))
    verdict = env["verdict_text"].lower()
    assert "unverified" in verdict
    assert "re-verify" in verdict
    assert env["next_action"]["id"] == "verify_undo"


def test_done_headline_trusts_a_graded_result():
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
        post_apply_grade={"state": "graded", "graded": True},
    ))
    assert "unverified" not in env["verdict_text"].lower()


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
    assert "run_full_measurement" not in alternates


def test_done_express_discloses_the_degraded_claim_and_the_upgrade_path():
    """Flow-simplification §1.3: express's done screen states plainly what
    was verified ("confirmed at the mark") and names the Full upgrade path
    — never a claim wider than what express measured."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="express",
        verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    assert env["screen"] == "done"
    verdict = env["verdict_text"].lower()
    assert "confirmed at the mark" in verdict
    assert "full measurement" in verdict
    assert env["tier"] == "express"
    # Undo stays the primary action (owner ruling) even on the express path.
    assert env["next_action"]["id"] == "verify_undo"
    alternates = {a["id"]: a for a in env["alternate_actions"]}
    assert alternates["room"]["href"] == "/correction/room/"
    upgrade = alternates["run_full_measurement"]
    assert upgrade["endpoint"] == "/correction/crossover/v2/session"
    assert upgrade["body"] == {"tier": "full"}


def test_done_full_tier_has_no_upgrade_action_and_reports_its_own_tier():
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="full",
        verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    alternates = {a["id"]: a for a in env["alternate_actions"]}
    assert "run_full_measurement" not in alternates
    assert env["tier"] == "full"


def test_envelope_tier_key_is_none_when_the_state_does_not_say():
    """Pre-tier durable state (or no session yet) reports ``None`` — never a
    guessed default (mirrors ``crossover_v2_status_block``'s own rule)."""
    env = build_crossover_envelope_v2(_status(phase="measure"))
    assert env["tier"] is None


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


def test_done_candidate_review_carries_linearization_outcome_and_octaves():
    """Gauge fix (2026-07-24): items 2/3 — the linearization run/skip
    outcome and per-role top-octave deficits ride the SAME candidate_review
    shape as trims/delay/polarity, sourced straight from
    jasper.web.correction_crossover_v2._candidate_summary's new fields."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            linearization_outcome="fitted",
            linearization_octaves={
                "woofer": {"8000": -0.3, "12000": -1.1, "16000": -2.8, "500": 0.1},
                "tweeter": {"8000": -0.1, "12000": -3.2, "16000": -9.4},
            },
        ),
    ))
    review = env["candidate_review"]
    assert review["linearization_outcome"] == "fitted"
    octaves = {row["role"]: row["bands"] for row in review["linearization_octaves"]}
    # Only the top three octaves (>= 8k) render — "500" is silently excluded,
    # matching the item's own scope ("at least the 8k/12k/16k values").
    assert octaves["woofer"] == [
        {"hz": 8000, "delta_db": -0.3},
        {"hz": 12000, "delta_db": -1.1},
        {"hz": 16000, "delta_db": -2.8},
    ]
    assert octaves["tweeter"] == [
        {"hz": 8000, "delta_db": -0.1},
        {"hz": 12000, "delta_db": -3.2},
        {"hz": 16000, "delta_db": -9.4},
    ]


def test_done_candidate_review_omits_linearization_fields_when_absent():
    """A candidate with no linearization at all (ineligible / plain trims)
    renders an empty outcome string and no octave rows — never a phantom
    "0 dB" or a KeyError."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    review = env["candidate_review"]
    assert review["linearization_outcome"] == ""
    assert review["linearization_octaves"] == []


# --- two-stage commission D4: the level-cost disclosure, era-aware ------------


def test_candidate_review_carries_the_headroom_cost_with_the_era_that_stamped_it():
    """PR-L5's "this correction costs N dB of maximum level" reaches a
    household-visible payload (D4). A candidate persisted by a build that
    records its basis discloses the number AND which derivation produced it."""
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
    )

    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            headroom_cost_db=5.2,
            headroom_cost_basis=HEADROOM_COST_BASIS_REALIZED_PEAK,
        ),
    ))
    assert env["candidate_review"]["headroom_cost"] == {
        "db": 5.2, "basis": HEADROOM_COST_BASIS_REALIZED_PEAK,
    }


def test_a_pre_amendment_headroom_cost_never_renders_bare():
    """D3's cross-era rule, pinned structurally rather than by convention.

    ``headroom_cost_db``'s derivation changed under #1808 and the stamp is not
    re-derived on load, so the same correction discloses ~22.5 dB under the old
    rule where it now costs ~5. A candidate persisted before the basis was
    recorded therefore reads as ``unknown`` — the absence is the only honest
    evidence of era there is, and it is never assumed to be current.

    The pin that makes "never bare" enforceable is the second half: there is no
    lone ``headroom_cost_db`` scalar anywhere on this payload for a renderer to
    reach for. The number is only obtainable through the compound that carries
    its basis, so rendering it without the era is not a discipline anyone has
    to remember — it is unavailable."""
    from jasper.active_speaker.linearization_fit import HEADROOM_COST_BASIS_UNKNOWN

    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"},
        # The pre-amendment shape: a number, and nothing saying how it was
        # derived. 22.5 dB is the real JTS3 figure the old rule produced for a
        # correction the new rule charges ~5 dB for.
        candidate=_candidate_summary(headroom_cost_db=22.458),
    ))
    review = env["candidate_review"]
    assert review["headroom_cost"] == {
        "db": 22.458, "basis": HEADROOM_COST_BASIS_UNKNOWN,
    }
    assert "headroom_cost_db" not in review


def test_an_absent_headroom_cost_is_unknown_not_a_free_correction():
    """``db`` is ``None``, never ``0.0``, when the state carries no number.

    Zero is a real and common answer here — every cut-only correction charges
    nothing — so defaulting an absent value to it would state a measurement the
    payload does not have, on the screen whose purpose is honesty."""
    from jasper.active_speaker.linearization_fit import HEADROOM_COST_BASIS_UNKNOWN

    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    assert env["candidate_review"]["headroom_cost"] == {
        "db": None, "basis": HEADROOM_COST_BASIS_UNKNOWN,
    }


def test_the_envelope_schema_version_moved_with_the_candidate_review_shape():
    """The payload's schema-version bump (D4's own pin, carried forward).

    ``candidate_review`` gained a key at 9, so the version a consumer reads to
    know which shape it is holding moved with it. It moved again at 10 for
    PR-T2's own additions (the ``review`` screen and the ``prediction`` key) —
    this assertion pins the CURRENT version, and
    ``test_the_review_screen_moved_the_schema_version`` states T2's reason.
    Both bumps are additive — no key was ever removed or re-typed — which is
    why the crossover wizard, whose module does not gate on this version, keeps
    rendering an older page against a newer envelope."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 11
    assert "headroom_cost" in env["candidate_review"]


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


def test_verify_fail_folds_tracking_numbers_behind_expert_details():
    """Item 5b (#1605): the verify_fail screen keeps its primary copy short and
    folds the level-error / average-error / tracking-band numbers into a
    collapsed expert disclosure (the frontend renders env.expert_details as a
    <details>). The conductor persists them under verify.evidence; an
    early-return verify verdict carries none, so no disclosure renders."""
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
        verify={
            "outcome": "fail",
            "evidence": {
                "max_db": 2.34,
                "rms_db": 0.81,
                "tracking_band_lo_hz": 1000.0,
                "tracking_band_hi_hz": 4000.0,
                "tolerance_db": 1.5,
            },
        },
    ))
    assert env["screen"] == "verify_fail"
    details = env["expert_details"]
    assert "level error 2.34 dB (limit 1.5 dB)" in details
    # PR-5/N-4: "tracking" disambiguates this from the flatness gauge's own
    # "flatness average error" line when both land in the same collapsed
    # disclosure (see test_verify_fail_folds_spec_flatness_alongside_
    # integration_evidence below) — same number, framed so a reader can tell
    # which construction it belongs to.
    assert "tracking average error 0.81 dB" in details
    assert "checked 1000–4000 Hz" in details
    # Primary copy stays the short reason message — the numbers are NOT in it.
    assert "2.34" not in env["verdict_text"]

    # No evidence ⇒ no disclosure (early-return verify verdicts carry none).
    bare = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
    ))
    assert bare["expert_details"] == []


# The compact ``cloud`` block shape ``_compact_cloud_status`` serves — the ONE
# place a household-facing flatness number comes from since the
# flat-linearization plan's PR-5 (the spec-curve SSOT). The numbers here are a
# ``spec_flatness_gauge`` dict verbatim; tests/test_flat_spec_ssot.py is what
# pins that a REAL pipeline produces this shape and that every surface shows
# the same bytes.
def _cloud_flatness_status(**overrides):
    flatness = {
        "max_db": -4.85, "max_hz": 11480.0, "max_band_hz": [8000.0, 16000.0],
        "tolerance_db": 2.5, "rms_db": 1.37, "n_bins": 900, "n_excluded": 42,
        "evaluable": True, "passed": False,
    }
    flatness.update(overrides)
    return {
        PHASE_CLOUD_VERIFY: {
            "geometry_locked": False, "thin_evidence": False,
            "geometry_guidance": "", "spec_bands": [], "overall_passed": False,
            "excluded_interval_count": 3, "flatness": flatness,
        },
    }


# B1 fix (adversarial review of PR #1780): express (M=1) never closes a
# CLOUD-VERIFY group, so its flatness/carve-out disclosure reads the SAME
# compact shape off CLOUD-MEASURE instead — this mirrors
# ``_cloud_flatness_status`` above but keys the entry on PHASE_CLOUD_MEASURE.
def _cloud_measure_flatness_status(*, carve_outs=None, **overrides):
    flatness = {
        "max_db": -4.85, "max_hz": 11480.0, "max_band_hz": [8000.0, 16000.0],
        "tolerance_db": 2.5, "rms_db": 1.37, "n_bins": 900, "n_excluded": 42,
        "evaluable": True, "passed": False,
    }
    flatness.update(overrides)
    return {
        PHASE_CLOUD_MEASURE: {
            "geometry_locked": False, "thin_evidence": False,
            "geometry_guidance": "", "spec_bands": [], "overall_passed": False,
            "excluded_interval_count": 3, "flatness": flatness,
            "carve_outs": carve_outs or [],
        },
    }


def test_verify_fail_folds_spec_flatness_alongside_integration_evidence():
    """PR-5: flatness is a SIBLING claim, distinctly labeled from the
    integration-verify numbers above — both travel in the SAME collapsed
    disclosure since verify_fail only has the one mechanism. The flatness
    lines now name the SPEC frame (which band, which tolerance, where the
    worst bin sits) rather than the retired capture-grid one; the
    integration line is untouched."""
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
        verify={
            "outcome": "fail",
            "evidence": {
                "max_db": 2.34, "rms_db": 0.81,
                "tracking_band_lo_hz": 1000.0, "tracking_band_hi_hz": 4000.0,
                "tolerance_db": 1.5,
            },
        },
        cloud=_cloud_flatness_status(),
    ))
    details = env["expert_details"]
    # Integration-verify: unchanged, still there.
    assert "level error 2.34 dB (limit 1.5 dB)" in details
    # PR-5/N-4: the two "average error" lines are the actual sibling claim
    # this test's docstring describes — pin that they read distinctly (each
    # carries its own one-word prefix) rather than as one unqualified number
    # appearing twice.
    assert "tracking average error 0.81 dB" in details
    assert "flatness average error 1.37 dB across the spec bands" in details
    # Flatness: spec-framed, signed, located — and never says "limit".
    assert (
        "flatness -4.85 dB from the spec reference at 11480 Hz "
        "(spec 8000–16000 Hz, tolerance ±2.5 dB)"
    ) in details
    assert (
        "42 of 942 spec-band bins excluded from grading (interference, or "
        "below the measurement's validity floor)"
    ) in details
    assert not any("limit" in line and "flatness" in line for line in details)


def test_done_folds_spec_flatness_on_a_pass():
    """The reported bug stays closed: a household on the "Your speaker is
    tuned" RESULT screen (a clean PASS — no verify.evidence ever shows here,
    by unchanged product design) still sees the flatness number, now sourced
    from the spatial cloud's spec verdict."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass"},
        cloud=_cloud_flatness_status(
            max_db=1.21, max_hz=402.0, max_band_hz=[250.0, 2000.0],
            tolerance_db=1.5, passed=True,
        ),
        candidate=_candidate_summary(),
    ))
    assert env["screen"] == "done"
    details = env["expert_details"]
    assert (
        "flatness +1.21 dB from the spec reference at 402 Hz "
        "(spec 250–2000 Hz, tolerance ±1.5 dB)"
    ) in details


def test_done_expert_details_empty_when_no_cloud_group_closed():
    """No cloud-verify entry at all — a session still walking, or one that
    never had a group. Nothing measured, so nothing is said: PR-5's
    cloud-absent rule forbids inventing a spec-frame number here."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    assert env["expert_details"] == []


def test_done_says_unavailable_when_the_cloud_pipeline_failed():
    """The middle cloud-absent state: the group closed but its pipeline never
    became available (a combine or DSP-step failure). Say so — never a
    number, and never the same silence as the no-group case."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
        cloud={PHASE_CLOUD_VERIFY: {
            "geometry_locked": False, "thin_evidence": False,
            "geometry_guidance": "", "spec_bands": [], "overall_passed": None,
            "excluded_interval_count": None, "flatness": None,
        }},
    ))
    assert env["expert_details"] == [
        "flatness not available for this measurement — the spatial "
        "measurement could not be analysed"
    ]


def test_done_distinguishes_a_pre_gauge_record_from_a_failed_pipeline():
    """A durable state written between PR-4 and PR-5 has a WORKING pipeline
    (``overall_passed`` is a real verdict) but no gauge key. Telling that
    household "the spatial measurement could not be analysed" would be a
    false statement about a session that analysed fine — the two states get
    different copy, and neither invents a number."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
        cloud={PHASE_CLOUD_VERIFY: {
            "geometry_locked": False, "thin_evidence": False,
            "geometry_guidance": "", "spec_bands": [], "overall_passed": True,
            "excluded_interval_count": 2, "flatness": None,
        }},
    ))
    assert env["expert_details"] == [
        "flatness not recorded for this measurement — it predates the "
        "spec gauge; re-measure to see it"
    ]


def test_done_says_unmeasurable_when_the_gauge_ran_but_found_no_bins():
    """The gauge ran and could not measure (every spec band excluded or out
    of range). ``passed`` is False there by ``FlatSpecReport.overall_passed``'s
    own rule, so this must never render as a fail."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
        cloud=_cloud_flatness_status(
            max_db=None, max_hz=None, max_band_hz=None, tolerance_db=None,
            rms_db=None, n_bins=0, evaluable=False, passed=False,
        ),
    ))
    assert env["expert_details"] == [
        "flatness could not be measured — every spec band was excluded "
        "or out of range"
    ]


def test_cloud_measure_flatness_never_renders_as_the_speakers_flatness():
    """``cloud_measure`` is the PRE-APPLY, uncorrected baseline that exists in
    order to be out of spec — the same distinction PR-4's doctor blocker
    drew. Rendering it here would report a correctly-corrected speaker as bad
    forever, so only ``cloud_verify`` feeds the gauge — for the FULL tier
    (unchanged by the B1 fix below, which only redirects EXPRESS)."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
        cloud={PHASE_CLOUD_MEASURE: _cloud_flatness_status()[PHASE_CLOUD_VERIFY]},
    ))
    assert env["expert_details"] == []


def test_cloud_measure_flatness_never_renders_as_the_speakers_flatness_tier_full():
    """Same as above, with an explicit ``tier="full"`` — confirms the B1
    tier branch reads the durable tier, not just its absence."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="full", verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        cloud={PHASE_CLOUD_MEASURE: _cloud_flatness_status()[PHASE_CLOUD_VERIFY]},
    ))
    assert env["expert_details"] == []


def test_express_done_discloses_before_tuning_flatness_from_measure_cloud():
    """B1 fix (adversarial review of PR #1780) — coordinator design
    direction: express (M=1) never closes a CLOUD-VERIFY group, so its
    done-screen expert disclosure must not sit on unread CLOUD-MEASURE data.
    Renders the SAME numeric lines as the full tier's CLOUD-VERIFY path, but
    framed explicitly as the BEFORE-TUNING state — never presented as "how
    flat your speaker is now" (express made no post-apply cross-position
    claim at all)."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="express", verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        cloud=_cloud_measure_flatness_status(),
    ))
    details = env["expert_details"]
    assert details, "express's done screen must not sit on unread measure-block data"
    combined = " ".join(details)
    assert "Measured before tuning:" in combined
    assert "confirmed at the mark only" in combined
    # The same numeric arithmetic the full tier's CLOUD-VERIFY path reports —
    # one construction (_flatness_lines_from_block), not two.
    assert "flatness -4.85 dB from the spec reference at 11480 Hz" in combined
    assert "flatness average error 1.37 dB across the spec bands" in combined
    assert (
        "42 of 942 spec-band bins excluded from grading (interference, or "
        "below the measurement's validity floor)"
    ) in combined
    # Never claims the CURRENT state — that claim needs a post-apply cloud,
    # which express does not make.
    assert "how flat your speaker is now" not in combined.lower()


def test_express_done_carve_outs_render_verbatim_from_measure_cloud():
    """Owner decision 1: carve-outs are a post-apply-persistent fact ("EQ
    cannot fill these") disclosed on EVERY tier — express renders them
    verbatim from CLOUD_MEASURE, not re-composed or reframed."""
    carve_out_line = (
        "8.7 kHz (rung 26, 6.2 dB deep); delay τ 299 µs, reflection ratio "
        "r 0.375 measured in time"
    )
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="express", verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        cloud=_cloud_measure_flatness_status(carve_outs=[
            {"band_hz": [8000.0, 16000.0], "expert": carve_out_line},
        ]),
    ))
    details = env["expert_details"]
    assert any(carve_out_line in line for line in details)


def test_express_done_says_nothing_when_the_measure_cloud_never_closed():
    """No CLOUD-MEASURE entry at all — nothing measured, nothing to say
    (the same honest-silence rule the full tier's cloud-absent states
    follow), never a fabricated before-tuning claim."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="express", verify={"outcome": "pass"},
        candidate=_candidate_summary(),
    ))
    assert env["expert_details"] == []


def test_verify_level_shift_renders_the_same_verify_fail_screen_shape():
    """Measurement-honesty gate G3 (crossover_v2_flow._verify_verdict): a
    THIRD, distinct reason code alongside verify_out_of_tolerance/
    verify_inconclusive, rendered through the exact same one-default
    ("Try again") + Undo screen shape — with its own copy naming the
    actual cause (the phone's mic chain drifted, not the speaker)."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_VERIFY_LEVEL_SHIFT},
    ))
    assert env["screen"] == "verify_fail"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_VERIFY_LEVEL_SHIFT].message
    assert env["next_action"]["label"] == "Try again"
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Undo (restore previous sound)" in labels


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
    assert env["schema_version"] == 11
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


# --- the REVIEW interlude (two-stage commission D3 + D6, issue #1806) ---------
#
# The apply decision point the 2026-07-28 owner ruling restored. A dedicated
# human review screen existed and was removed on purpose (2026-07-20, "no human
# mid-flow Apply gate"); auto-applying a fit that failed its own spec by
# +6.04 dB — in-session, three seconds before VERIFY — left a box
# applied-and-ungraded with no household-visible decision anywhere, and this is
# the screen that closes that.


def _prediction(
    *, curve=True, overall_passed=True, bands=None, reference_db=80.0,
) -> dict:
    """One ``_prediction_status`` projection, in the shape the wire sends.

    Mirrors ``jasper.web.correction_crossover_v2._prediction_status``'s output
    key-for-key rather than inventing a convenient shape — the four
    absence/presence combinations exercised below are its OWN enumerated
    states, and a fixture that smoothed them over would pin nothing.
    """
    return {
        "curve": {"freqs_hz": [100.0, 1000.0], "magnitude_db": [80.0, 74.0]}
        if curve else None,
        "spec_bands": bands if bands is not None else [
            {
                "f_lo_hz": 250.0, "f_hi_hz": 500.0, "passed": True,
                "max_deviation_db": 1.2, "tolerance_db": 3.0,
            },
        ],
        "overall_passed": overall_passed,
        "reference_db": reference_db,
    }


_FAILING_BANDS = [
    {
        "f_lo_hz": 100.0, "f_hi_hz": 250.0, "passed": True,
        "max_deviation_db": 1.0, "tolerance_db": 3.0,
    },
    # The worst miss: |-9.04| - 3.0 = 6.04 dB past tolerance — the 2026-07-28
    # session's own number, which is what makes this the case the work order
    # was written from rather than a synthetic one.
    {
        "f_lo_hz": 250.0, "f_hi_hz": 500.0, "passed": False,
        "max_deviation_db": -9.04, "tolerance_db": 3.0,
    },
    {
        "f_lo_hz": 500.0, "f_hi_hz": 2000.0, "passed": False,
        "max_deviation_db": 4.5, "tolerance_db": 3.0,
    },
]


def _review_status(**overrides) -> dict:
    v2 = {
        "phase": "review",
        "candidate": _candidate_summary(),
        "prediction": _prediction(),
        "stage2_preflight": {"ok": True, "message": "", "next_action": None},
    }
    v2.update(overrides)
    return _status(**v2)


def test_review_screen_offers_the_three_way_decision_and_never_undo():
    """D3.5 + **D6**: apply-and-verify / measure again / leave it as it is.

    D6 is the load-bearing half here. ``_failure_envelope``'s applied-override
    is keyed on the raw ``applied`` state fact, correctly false through all of
    stage 1 — but the override's mere existence makes offering Undo a one-line
    accident, and Undo on this screen would invite a household to "restore" a
    speaker that was never changed. Nothing was replaced, so nothing may be
    offered as a restore: no action id, no label, and no endpoint on this
    screen may reach the restore path.
    """
    env = build_crossover_envelope_v2(_review_status())
    assert env["screen"] == "review"
    assert env["next_action"]["id"] == "review_apply"
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/apply"
    assert [a["id"] for a in env["alternate_actions"]] == [
        "review_remeasure", "review_decline",
    ]
    every_action = [env["next_action"], *env["alternate_actions"]]
    assert not any("restore" in str(a.get("endpoint") or "") for a in every_action)
    assert not any("undo" in str(a.get("id") or "").lower() for a in every_action)
    assert not any("undo" in str(a.get("label") or "").lower() for a in every_action)


def test_review_apply_posts_the_reviewed_candidates_own_fingerprint():
    """The screen does not need a new apply path — it needs a button that posts
    to the one that is there (work-order premise 4). ``handle_v2_apply``'s FIRST
    gate is ``expected_candidate_fingerprint``; it refuses outright without one,
    and refuses a stale one against the durable candidate."""
    env = build_crossover_envelope_v2(_review_status())
    assert env["next_action"]["body"] == {"expected_candidate_fingerprint": "fp-123"}


def test_review_shows_the_measured_evidence_and_the_predicted_curve():
    """D3.1/D3.3: the measured cloud and the prediction ride ONE envelope so the
    chart can draw them in one deviation frame. The prediction is sent on THIS
    screen only — that is what lets the JS stay data-driven instead of growing
    the ``env.screen`` switch PR-T2 is not allowed to add."""
    env = build_crossover_envelope_v2(_review_status())
    assert env["prediction"]["curve"]["freqs_hz"] == [100.0, 1000.0]
    assert env["prediction"]["reference_db"] == 80.0
    assert env["candidate_review"]["trims"]  # "what we propose"
    # Every other screen carries no prediction, so no shipped chart changes.
    #
    # DERIVED from the phase→step map, not hand-listed (review N-3). The
    # suppression is what keeps `cloud.js`'s `specSourceFor` on the pre-apply
    # cloud for the review screen ONLY; a phase added to the vocabulary without
    # a line here would silently start swapping that spec source on a screen
    # nobody checked. Deriving the set means a new phase joins this assertion
    # the moment it exists.
    others = set(_PHASE_STEP) - {PHASE_REVIEW}
    assert others, "the phase vocabulary must have screens other than review"
    for phase in sorted(others):
        other = build_crossover_envelope_v2(_status(
            phase=phase, candidate=_candidate_summary(), prediction=_prediction(),
        ))
        assert other["prediction"] is None, phase


def test_review_names_the_band_and_the_margin_when_the_prediction_fails():
    """**D3.4, the honest verdict.** "When the prediction fails the spec, the
    screen says so in the household's language and names the band and the
    margin."

    The margin is the OVERSHOOT past that band's own tolerance, not the raw
    deviation — a deviation on its own says nothing about whether it was
    allowed. The worst-missing band wins the sentence.

    And it is still APPLYABLE: "improved-but-failing is presented, never
    applied silently". A graded miss keeps the decision with the household,
    which is the entire reason this screen exists.
    """
    env = build_crossover_envelope_v2(_review_status(
        prediction=_prediction(overall_passed=False, bands=_FAILING_BANDS),
    ))
    verdict = env["verdict_text"]
    assert "6.0 dB" in verdict          # 9.04 - 3.0, the worst band's overshoot
    assert "250 and 500 Hz" in verdict  # ...and the band it happened in
    assert env["next_action"]["enabled"] is True
    assert "crossover_v2_prediction_out_of_spec" in [n["code"] for n in env["nudges"]]


def test_review_never_states_the_prediction_as_a_measurement():
    """The work order's own trap: "the prediction is a model, and the screen
    says so". The measured evidence on this screen is the pre-apply cloud; the
    predicted response is model-vs-model, from the same instrument on both
    sides, so the room cancels and nothing here may be phrased as a finding
    about the room."""
    env = build_crossover_envelope_v2(_review_status(
        prediction=_prediction(overall_passed=False, bands=_FAILING_BANDS),
    ))
    verdict = env["verdict_text"]
    assert "worked out from the measurement, not measured" in verdict
    # The one thing measured on this screen is what the microphone heard.
    assert "JTS measured your speaker" in verdict


def test_an_ungradeable_prediction_disables_apply_rather_than_guessing():
    """D4: ``None`` is load-bearing. "An ungradeable prediction renders as 'we
    could not predict this' and DISABLES the Apply control, rather than
    presenting an unevidenced proposal."

    Both ungradeable shapes — a curve with no stored report (state 2) and no
    prediction block at all (state 3) — land here. ``None`` means unknown, and
    a consumer must never read it as permission.
    """
    for prediction in (_prediction(overall_passed=None, bands=[]), None):
        env = build_crossover_envelope_v2(_review_status(prediction=prediction))
        assert env["screen"] == "review"
        assert env["next_action"]["enabled"] is False, prediction
        assert "could not" in env["verdict_text"]
        # Never a fabricated verdict in either direction.
        assert "meets the target" not in env["verdict_text"]


def test_a_graded_miss_is_not_an_ungradeable_prediction():
    """``False`` and ``None`` are opposite answers and must not collapse.

    ``False`` is a real graded verdict that the prediction misses the spec —
    presented, and applyable. ``None`` is "we could not tell" — refused. A
    renderer that treated them alike would either hide a real miss behind a
    dead end or offer an unevidenced proposal as if it had been checked.
    """
    missed = build_crossover_envelope_v2(_review_status(
        prediction=_prediction(overall_passed=False, bands=_FAILING_BANDS),
    ))
    unknown = build_crossover_envelope_v2(_review_status(
        prediction=_prediction(overall_passed=None, bands=[]),
    ))
    assert missed["next_action"]["enabled"] is True
    assert unknown["next_action"]["enabled"] is False
    assert missed["verdict_text"] != unknown["verdict_text"]


def test_the_refusal_lane_states_its_verdict_and_stages_no_decision():
    """``_prediction_status``'s **4th** state: report present, curve absent.

    The improvement gate refused, so the verdict was stashed before the gate ran
    while ``predicted_sum`` was never assigned — ``overall_passed`` is a REAL
    ``False`` here, not the ``None`` that means unknown, and there is no
    candidate behind it. So the verdict is still stated (it genuinely evaluated
    that prediction) and NO decision is staged: an Apply control over a
    candidate that does not exist would refuse at the endpoint's first gate.
    """
    env = build_crossover_envelope_v2(_review_status(
        candidate=None,
        prediction=_prediction(
            curve=False, overall_passed=False, bands=_FAILING_BANDS,
        ),
    ))
    assert env["next_action"] is None
    assert env["prediction"]["curve"] is None
    assert "6.0 dB" in env["verdict_text"]
    assert "no correction to propose" in env["verdict_text"]
    assert [a["id"] for a in env["alternate_actions"]] == [
        "review_remeasure", "review_decline",
    ]


def test_a_box_that_cannot_open_stage_2_gets_the_named_refusal_and_no_apply():
    """**D3's stage-2 openability preflight**, the hole premise 5 was hiding.

    "Without this, the failure mode is precisely the applied-and-ungraded end
    state this work order exists to eliminate: a household applies, stage 2
    refuses at open, and the box sits corrected with no verdict."

    The refusal renders AS ITSELF — the predicate's own sentence, which already
    names what to finish first — and its declared resolution control is offered
    beside it, from the same registry entry the hard-stop screen reads (#1820's
    precedent). Never a generic "cannot apply".
    """
    env = build_crossover_envelope_v2(_review_status(stage2_preflight={
        "ok": False,
        "message": "This speaker's safety limits are not confirmed.",
        "next_action": {
            "id": "confirm_safety_limits",
            "label": "Confirm safety limits",
            "href": "/sound/#confirm-safety-limits",
        },
    }))
    assert env["next_action"]["enabled"] is False
    refusal = [n for n in env["nudges"]
               if n["code"] == "crossover_v2_stage2_preflight_refused"]
    assert refusal and "safety limits are not confirmed" in refusal[0]["text"]
    assert env["alternate_actions"][0]["id"] == "confirm_safety_limits"


def test_a_refusal_button_never_renders_without_its_explaining_sentence():
    """Review N-2: the two halves of one refusal are gated as one thing.

    The sentence used to also require a gradeable prediction while the button
    did not, so an ungradeable prediction beside an action-carrying refusal
    rendered a bare "Confirm safety limits" control with nothing on screen
    saying why it was there.

    Aligned toward SHOWING both rather than hiding one: the household has two
    independent blockers, and the stage-2 refusal is still true after they
    re-measure. Suppressing it would let them walk the whole measurement again
    only to meet a refusal that was knowable now — the exact cost #1828 moved
    this predicate earlier to avoid.
    """
    env = build_crossover_envelope_v2(_review_status(
        prediction=_prediction(overall_passed=None, bands=[]),  # ungradeable
        stage2_preflight={
            "ok": False,
            "message": "This speaker's safety limits are not confirmed.",
            "next_action": {
                "id": "confirm_safety_limits",
                "label": "Confirm safety limits",
                "href": "/sound/#confirm-safety-limits",
            },
        },
    ))
    assert env["next_action"]["enabled"] is False
    # The button is there...
    assert env["alternate_actions"][0]["id"] == "confirm_safety_limits"
    # ...and so is the sentence that explains it.
    assert [n for n in env["nudges"]
            if n["code"] == "crossover_v2_stage2_preflight_refused"]
    # Both blockers reach the household: the verdict copy names the one the
    # nudge does not.
    assert "could not check" in env["verdict_text"]


def test_no_refusal_button_survives_when_there_is_no_apply_decision():
    """The other side of the same gate: with no candidate there is no Apply
    control, so neither the refusal sentence nor its button has anything to
    explain — both stay off rather than annotating a decision that is not on
    offer."""
    env = build_crossover_envelope_v2(_review_status(
        candidate=None,
        stage2_preflight={
            "ok": False,
            "message": "This speaker's safety limits are not confirmed.",
            "next_action": {
                "id": "confirm_safety_limits",
                "label": "Confirm safety limits",
                "href": "/sound/#confirm-safety-limits",
            },
        },
    ))
    assert env["next_action"] is None
    assert [a["id"] for a in env["alternate_actions"]] == [
        "review_remeasure", "review_decline",
    ]
    assert not [n for n in env["nudges"]
                if n["code"] == "crossover_v2_stage2_preflight_refused"]


def test_an_unresolved_preflight_is_not_permission():
    """Absence is not a pass. An unset key means the predicate never ran, and
    "we never checked" must render exactly like "we checked and it refused" —
    the end state being prevented (applied, then stage 2 refuses at open) is
    identical either way. Only an explicit ``ok: True`` enables Apply."""
    for preflight in ({}, None, {"ok": "yes"}, {"message": "..."}):
        env = build_crossover_envelope_v2(_review_status(stage2_preflight=preflight))
        assert env["next_action"]["enabled"] is False, preflight
        assert env["nudges"], preflight


def test_review_puts_the_measured_flatness_where_it_informs_the_decision():
    """D3.1: the pre-apply cloud IS the measured evidence on this screen, so its
    flatness/carve-out disclosure belongs here — the same lines the RESULT
    screen folds away, on the screen where they inform a choice rather than
    explain a fait accompli."""
    env = build_crossover_envelope_v2(_review_status(
        tier="express",
        cloud={PHASE_CLOUD_MEASURE: {
            "flatness": {
                "evaluable": True, "max_db": 6.2, "max_hz": 310.0,
                "tolerance_db": 3.0, "max_band_hz": [250.0, 500.0],
            },
        }},
    ))
    assert any("flatness" in line for line in env["expert_details"])


def test_the_review_screen_moved_the_schema_version():
    """PR-T2's bump (9 → 10): the screen vocabulary gained ``review`` and the
    envelope gained ``prediction``. PR-T3's (10 → 11): the vocabulary gained
    ``closing`` and the envelope gained ``busy``. Both additive — no key
    removed or re-typed — so an unredeployed page ignores the new keys rather
    than refusing the envelope, the same property the 8 → 9 bump had."""
    env = build_crossover_envelope_v2(_review_status())
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 11
    assert "prediction" in env
    assert env["busy"] is False  # present on every screen, true on one
