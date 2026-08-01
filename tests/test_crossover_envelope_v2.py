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

import time
from typing import Mapping

import numpy as np
import pytest

from jasper.active_speaker.crossover_envelope_v2 import (
    CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
    _PHASE_STEP,
    _per_band_flatness_lines,
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
    REASON_LOCATE_FAILED,
    REASON_LOW_ALIGNMENT_CONFIDENCE,
    REASON_NOISY_ROOM_LINEARITY,
    REASON_RELAY_TIMEOUT,
    REASON_SNR_FLOOR,
    REASON_USER_STOPPED,
    REASON_VERIFY_INCONCLUSIVE,
    REASON_VERIFY_LEVEL_SHIFT,
    REASON_VERIFY_OUT_OF_TOLERANCE,
    verify_inconclusive_cause,
    verify_inconclusive_message,
)
from jasper.active_speaker.flat_spec import evaluate_flat_spec, spec_flatness_gauge
from jasper.audio_measurement import gating
from jasper.audio_measurement.gate_disclosure import describe_gate
from jasper.web.correction_crossover_v2 import _compact_cloud_status

V2_STEP_IDS = ("speaker_setup", "microphone_check", "measure", "apply", "verify")


def _status(**v2) -> dict:
    failure = v2.get("failure")
    if isinstance(failure, Mapping) and "at" not in failure:
        # #1942: a persisted failure now carries WHEN it happened, and only a
        # fresh one renders its terminal screen. Every fixture below that
        # hands this helper a failure is describing the screen a household is
        # looking at right now, so the helper stamps it fresh — which is what
        # keeps those tests pinning the LIVE path they were written for.
        # The aged and undated (pre-#1942) cases are built inline instead, so
        # a test that means "stale" has to say so out loud.
        v2 = {**v2, "failure": {**failure, "at": time.time()}}
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
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 12
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
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 12
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
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 12
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
                "tolerance_db": 1.5,
            },
            # #1868 hoisted the band out of ``evidence`` (which is persisted
            # only on a non-pass) into a sibling key persisted on every
            # outcome. The fail screen still renders it, from here.
            "graded_band_hz": [1000.0, 4000.0],
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
        # The frame the deviation is stated against (#1857) — production's
        # ``spec_flatness_gauge`` always emits it, so the fixtures do too.
        "reference_band_hz": [250.0, 8000.0],
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
        # The frame the deviation is stated against (#1857) — production's
        # ``spec_flatness_gauge`` always emits it, so the fixtures do too.
        "reference_band_hz": [250.0, 8000.0],
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
                "tolerance_db": 1.5,
            },
            "graded_band_hz": [1000.0, 4000.0],
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
    # Flatness: spec-framed, signed, located — and never says "limit". The
    # reference frame is NAMED (#1857): the "spec 8000–16000 Hz" in the
    # parenthetical is the tolerance row being judged, not the zero the
    # deviation is measured from, and readers conflated the two.
    assert (
        "flatness -4.85 dB from the 250–8000 Hz reference mean at 11480 Hz "
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
        "flatness +1.21 dB from the 250–8000 Hz reference mean at 402 Hz "
        "(spec 250–2000 Hz, tolerance ±1.5 dB)"
    ) in details


def test_flatness_copy_names_no_frame_when_the_record_carries_none():
    """#1857 — a state file written by an older build has no
    ``reference_band_hz``, and the line must NOT invent one. It keeps the
    previous unqualified wording instead: vaguer, but not a claim about a
    frame this code would be guessing at."""
    flatness = _cloud_flatness_status()[PHASE_CLOUD_VERIFY]["flatness"]
    del flatness["reference_band_hz"]
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass"},
        cloud={PHASE_CLOUD_VERIFY: {
            "geometry_locked": False, "thin_evidence": False,
            "geometry_guidance": "", "spec_bands": [], "overall_passed": False,
            "excluded_interval_count": 3, "flatness": flatness,
        }},
        candidate=_candidate_summary(),
    ))
    details = env["expert_details"]
    assert any("from the spec reference at 11480 Hz" in line for line in details)
    assert not any("reference mean" in line for line in details)


# --- #1857: every band discloses its own deviation, not just the pointer's ---


def _dark_tweeter_compact_cloud(*, phase: str = PHASE_CLOUD_VERIFY):
    """A REAL ``evaluate_flat_spec`` report reproducing #1857's mechanism —
    reproduced from the actual evaluator, not asserted by fiat.

    A narrow +3 dB peak sits in the woofer band; the tweeter band is
    uniformly ~6 dB dark across its ENTIRE passband (no peak, no texture,
    just a whole-band offset); the top band is flat. Because the shared
    reference is a power mean pooled across the woofer+tweeter bands, the
    tweeter's own darkness drags that reference down, and the woofer's
    narrow (and much smaller) peak ends up reading a LARGER deviation from
    it than the tweeter's own uniform darkness does — the exact
    misattribution class #1857 reports.
    """
    n = 1000
    woofer_freqs = np.linspace(250.0, 1999.0, n)
    tweeter_freqs = np.linspace(2000.0, 7999.0, n)
    top_freqs = np.linspace(8000.0, 15999.0, n)
    freqs = np.concatenate([woofer_freqs, tweeter_freqs, top_freqs])

    woofer_curve = np.zeros(n)
    woofer_curve[n // 2] = 3.0  # one narrow +3 dB peak, otherwise flat
    tweeter_curve = np.full(n, -6.0)  # uniformly dark, the WHOLE band
    top_curve = np.zeros(n)
    curve = np.concatenate([woofer_curve, tweeter_curve, top_curve])

    order = np.argsort(freqs)
    freqs, curve = freqs[order], curve[order]

    report = evaluate_flat_spec(freqs, curve, None)
    gauge = spec_flatness_gauge(report)
    pipeline = {
        "available": True,
        "spec": report.to_dict(),
        "flatness": gauge.to_dict(),
        "merged_excluded_bands_hz": [],
        "validity_floor_hz": None,
    }
    compact = _compact_cloud_status({phase: {"geometry": {}, "pipeline": pipeline}})
    return compact[phase], report, gauge


def test_the_shipped_pointer_blames_the_woofer_while_the_tweeter_stays_dark():
    """Mechanism check, not the fix: confirms #1857 reproduces against the
    REAL evaluator before testing that the disclosure now names it too.

    The pointer picks the WOOFER band — the larger absolute deviation under
    the dragged full-range reference — per the SAME "absolute dB, not
    tolerance headroom" rule ``test_flat_spec_ssot.py`` already pins for
    ``spec_flatness_gauge``. The tweeter's own reading, from the identical
    reference, is smaller in magnitude despite being a whole-band defect —
    which is exactly why the pointer alone misses it. This PR does not
    change this selection (#1857's anchor choice, Q-E, is still an open
    owner decision); it only stops the pointer from being the only thing a
    reader sees.
    """
    _compact, report, gauge = _dark_tweeter_compact_cloud()
    assert gauge.max_band_hz == (250.0, 2000.0)
    assert gauge.max_db == pytest.approx(5.0336, abs=5e-4)
    tweeter = next(
        b for b in report.bands if (b.f_lo_hz, b.f_hi_hz) == (2000.0, 8000.0)
    )
    assert tweeter.max_deviation_db == pytest.approx(-3.9664, abs=5e-4)
    assert tweeter.passed is False  # a real defect, just not the FLAGGED one
    assert abs(tweeter.max_deviation_db) < abs(gauge.max_db)


def test_the_expert_disclosure_now_names_every_band_beside_the_pointer():
    """The fix. The pointer line is untouched — still the woofer, per the
    mechanism test above — but the tweeter's own -3.97 dB now sits right
    beside it, so a reader can no longer see "+5.03 dB, woofer band" without
    also seeing the tweeter's own number in the same disclosure."""
    compact, _report, _gauge = _dark_tweeter_compact_cloud()
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, cloud={PHASE_CLOUD_VERIFY: compact},
        candidate=_candidate_summary(),
    ))
    details = env["expert_details"]
    # The pointer: unchanged shape and unchanged verdict — still the woofer.
    assert any(
        line.startswith(
            "flatness +5.03 dB from the 250–8000 Hz reference mean at 1125 Hz"
        )
        for line in details
    )
    # The new line: every band, from the SAME reference, including the
    # tweeter's own honest number.
    per_band = next(
        line for line in details
        if line.startswith("every band from the same reference:")
    )
    assert "250–2000 Hz +5.03 dB (fail, tolerance ±1.5 dB)" in per_band
    assert "2000–8000 Hz -3.97 dB (fail, tolerance ±2.0 dB)" in per_band
    assert "8000–16000 Hz +2.03 dB (pass, tolerance ±2.5 dB)" in per_band


def test_the_pre_apply_reading_also_names_every_band():
    """The BEFORE-TUNING branch (``_pre_apply_flatness_lines``) folds every
    ``_flatness_lines_from_block`` line into one ``"Measured before
    tuning: "``-prefixed sentence (the module's own framing rule — these
    numbers must never render bare the way CLOUD-VERIFY renders them). The
    per-band disclosure is the SAME kind of before-tuning claim as the
    pointer it sits beside, so it folds into that SAME sentence rather than
    appearing as a separate, unprefixed line the way carve-outs do."""
    compact, _report, _gauge = _dark_tweeter_compact_cloud(phase=PHASE_CLOUD_MEASURE)
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"},
        cloud={PHASE_CLOUD_MEASURE: compact}, candidate=_candidate_summary(),
    ))
    details = env["expert_details"]
    lead = next(line for line in details if line.startswith("Measured before tuning: "))
    assert "250–2000 Hz +5.03 dB (fail, tolerance ±1.5 dB)" in lead
    assert "2000–8000 Hz -3.97 dB (fail, tolerance ±2.0 dB)" in lead
    assert "8000–16000 Hz +2.03 dB (pass, tolerance ±2.5 dB)" in lead


def test_per_band_lines_uniformly_flat_shows_no_alarm():
    """Edge case: nothing wrong anywhere. The new line still renders (every
    band IS evaluable) but shows nothing alarming — three passes, ~0 dB —
    confirming the disclosure does not manufacture a false impression of
    trouble where the pointer already reports none."""
    freqs = np.geomspace(250.0, 16_000.0, 1500)
    report = evaluate_flat_spec(freqs, np.zeros_like(freqs), None)
    spec_bands = [
        {
            "f_lo_hz": b.f_lo_hz, "f_hi_hz": b.f_hi_hz, "passed": b.passed,
            "max_deviation_db": b.max_deviation_db, "tolerance_db": b.tolerance_db,
        }
        for b in report.bands
    ]
    lines = _per_band_flatness_lines(spec_bands)
    assert len(lines) == 1
    assert "+0.00 dB (pass" in lines[0]
    assert "fail" not in lines[0]


def test_per_band_lines_single_band_defect_leaves_the_others_quiet():
    """Edge case: only the top band is out of spec; woofer and tweeter are
    genuinely flat. The per-band line shows two clean passes and one real
    failure — the ordinary, non-misattribution case, which this disclosure
    must render just as plainly as the drag case above."""
    freqs = np.geomspace(250.0, 16_000.0, 1500)
    curve = np.where(freqs >= 8000.0, -6.0, 0.0)
    report = evaluate_flat_spec(freqs, curve, None)
    spec_bands = [
        {
            "f_lo_hz": b.f_lo_hz, "f_hi_hz": b.f_hi_hz, "passed": b.passed,
            "max_deviation_db": b.max_deviation_db, "tolerance_db": b.tolerance_db,
        }
        for b in report.bands
    ]
    line = _per_band_flatness_lines(spec_bands)[0]
    assert "250–2000 Hz" in line and "(pass" in line.split("250–2000 Hz")[1][:20]
    assert "8000–16000 Hz" in line and "(fail" in line.split("8000–16000 Hz")[1][:20]


def test_per_band_lines_both_bands_failing_shows_both():
    """Edge case named in #1857's own remedy: BOTH the woofer and tweeter
    genuinely out of spec (not one dragging the other) — the per-band line
    must show both failures, not collapse to the single pointer."""
    spec_bands = [
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "passed": False,
         "max_deviation_db": 3.0, "tolerance_db": 1.5},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "passed": False,
         "max_deviation_db": -4.5, "tolerance_db": 2.0},
        {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "passed": True,
         "max_deviation_db": 1.0, "tolerance_db": 2.5},
    ]
    line = _per_band_flatness_lines(spec_bands)[0]
    assert "250–2000 Hz +3.00 dB (fail" in line
    assert "2000–8000 Hz -4.50 dB (fail" in line
    assert "8000–16000 Hz +1.00 dB (pass" in line


def test_per_band_lines_skips_unevaluable_bands_without_fabricating():
    """A band with no surviving evidence (``passed`` is ``None``, not a
    bool) contributes no line — the same "unevaluable is not a fabricated
    verdict" rule ``BandResult`` itself follows — rather than printing a
    fake 0 dB reading for a band nothing measured."""
    spec_bands = [
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "passed": None,
         "max_deviation_db": None, "tolerance_db": 1.5},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "passed": False,
         "max_deviation_db": -4.5, "tolerance_db": 2.0},
    ]
    line = _per_band_flatness_lines(spec_bands)[0]
    assert "250–2000 Hz" not in line
    assert "2000–8000 Hz -4.50 dB (fail" in line


def test_per_band_lines_empty_or_malformed_input_renders_nothing():
    """No fabricated line when there is nothing to disclose — mirrors every
    other honesty rule in this module (``[]``, never an empty-looking
    sentence)."""
    assert _per_band_flatness_lines([]) == []
    assert _per_band_flatness_lines(None) == []
    assert _per_band_flatness_lines("not a list") == []
    assert _per_band_flatness_lines([{"passed": None}, "not a mapping"]) == []


def test_done_screen_states_the_band_verify_graded():
    """#1868 — the screen that says "Verified." must say over what.

    The graded band used to ride ``verify.evidence``, which the host persists
    only on a NON-pass outcome, so this screen — the household's one "the
    result is good" moment — never named it. On the 2026-07-30 corpus that
    band was [2000, 4000] Hz while the crossover defect under investigation
    sat at 1919 Hz, below its floor and structurally ungradeable.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "graded_band_hz": [2000.0, 4000.0]},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert env["screen"] == "done"
    assert "checked 2000–4000 Hz" in env["expert_details"]


def test_verify_fail_states_the_band_even_when_the_numbers_are_missing():
    """#1868 — the band and the evidence block have DIFFERENT presence
    conditions, so the band must not be gated behind the numbers.

    ``_verify_evidence_from_tracking`` returns nothing unless the
    notch-excluded max is a real number, while the band exists whenever a
    tracking comparison ran — and a tracking dict with a band but no usable
    max is exactly what a ``verify_out_of_tolerance`` refusal can carry.
    """
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
        verify={"outcome": "fail", "graded_band_hz": [2000.0, 4000.0]},
    ))
    assert env["screen"] == "verify_fail"
    assert "checked 2000–4000 Hz" in env["expert_details"]


def test_done_screen_claims_no_band_when_verify_graded_none():
    """#1868 — absence stays absence. A done screen reached without a
    tracking comparison (express confirms at the mark by a different route)
    must not manufacture a band."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass"},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert not any("checked" in line for line in env["expert_details"])


_FRAME = {
    "offset_db": -0.75,
    "tilt_db_per_octave": -0.79,
    "pivot_hz": 2828.4,
    "n_bins": 400,
    "band_hz": [2000.0, 4000.0],
    "rms_db_raw": 1.90,
    "max_db_raw": 2.40,
    "rms_db_tilt_removed": 1.34,
    "max_db_tilt_removed": 0.62,
}


def test_done_screen_states_the_frame_and_BOTH_grades():
    """Rung P1 — the screen that says "Verified." must say how much of that
    was the instrument, AND must never show only the flattering half.

    VERIFY differences an on-axis MODEL against an in-room MEASUREMENT. On the
    2026-07-29 corpus a single −0.79 dB/octave tilt between those frames was
    84 % of the flow's apparent prediction error, and the household would have
    read the pass as "the model was right".

    The done screen has no ``evidence`` block — the host persists that only for
    a NON-pass outcome — so unless the frame lines carry the raw pair
    themselves, a passing household would read the tilt-removed grade with
    nothing beside it: the smaller number, alone, presented as the result. That
    is the exact over-claim this rung exists to stop, so the raw pair rides
    here too.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "graded_band_hz": [2000.0, 4000.0],
                "frame": _FRAME},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert env["screen"] == "done"
    assert "frame offset -0.75 dB, tilt -0.79 dB/oct" in env["expert_details"]
    assert (
        "raw: level error 2.40 dB, tracking average error 1.90 dB"
        in env["expert_details"]
    )
    assert (
        "tilt-removed: level error 0.62 dB, tracking average error 1.34 dB"
        in env["expert_details"]
    )
    # The ordering is the claim: raw first, then what removing the frame did.
    details = env["expert_details"]
    assert details.index("raw: level error 2.40 dB, tracking average error 1.90 dB") < (
        details.index(
            "tilt-removed: level error 0.62 dB, tracking average error 1.34 dB"
        )
    )


def test_verify_fail_states_the_frame_without_repeating_the_raw_numbers():
    """The beside-grade is BESIDE — and on THIS screen the raw pair is already
    above it, stated against its limit by the evidence block that owns it. So
    the frame lines add only what is missing: the frame, and the tilt-removed
    pair. Printing "raw:" here as well would be the same two numbers twice in
    one collapsed paragraph."""
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
        verify={
            "outcome": "fail",
            "evidence": {"max_db": 2.4, "rms_db": 1.9, "tolerance_db": 1.5},
            "graded_band_hz": [2000.0, 4000.0],
            "frame": _FRAME,
        },
    ))
    assert env["screen"] == "verify_fail"
    assert "level error 2.40 dB (limit 1.5 dB)" in env["expert_details"]
    assert "tracking average error 1.90 dB" in env["expert_details"]
    assert "frame offset -0.75 dB, tilt -0.79 dB/oct" in env["expert_details"]
    assert (
        "tilt-removed: level error 0.62 dB, tracking average error 1.34 dB"
        in env["expert_details"]
    )
    assert not any(line.startswith("raw: ") for line in env["expert_details"])


def test_a_tilt_removed_grade_is_never_rendered_without_a_raw_one():
    """The invariant behind should-fix 1, stated as a property rather than as
    two screen-specific cases: whenever a tilt-removed number is on screen, a
    raw one is on screen too. A record carrying only the tilt-removed half
    therefore renders the frame alone rather than the friendlier grade
    unaccompanied."""
    frame = dict(_FRAME)
    del frame["rms_db_raw"]
    del frame["max_db_raw"]
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "frame": frame},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert "frame offset -0.75 dB, tilt -0.79 dB/oct" in env["expert_details"]
    assert not any("tilt-removed" in line for line in env["expert_details"])


def test_a_frame_with_no_beside_grades_states_the_frame_alone():
    """The lines have different presence conditions — a frame can be fitted
    while the analysis reported no grades at all — and the first must not be
    gated behind the others."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass",
                "frame": {"offset_db": 0.1, "tilt_db_per_octave": -0.2}},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert "frame offset +0.10 dB, tilt -0.20 dB/oct" in env["expert_details"]
    assert not any("tilt-removed" in line for line in env["expert_details"])
    assert not any(line.startswith("raw: ") for line in env["expert_details"])


def test_no_frame_is_stated_when_none_was_measured():
    """Absence stays absence: a screen with no fitted frame says nothing about
    one, rather than printing a flat frame nobody measured."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "graded_band_hz": [2000.0, 4000.0]},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert not any("frame offset" in line for line in env["expert_details"])
    assert not any("tilt-removed" in line for line in env["expert_details"])


# --- #1966 / #1974: the gate disclosure, and the copy it corrects ----------------
#
# Until R9 ``describe_gate``'s sentence reached no household surface at all: the
# analysis summary's copy dead-ended in an off-by-default operator sidecar, and
# the position-evidence copy dead-ended in bundle artifacts and a durable
# sub-key every projection dropped. Meanwhile the two screens below asserted
# "the room reflection cut the window short" over a corpus in which no reflection
# had ever been found. These tests pin both halves — the sentence rendering, and
# each path's copy telling the truth about its own path.


def _gate_block(floor_source: str) -> dict:
    """A gating block of the shape ``describe_gate`` reads, as gating writes it."""
    return {
        "applied": True,
        "window_ms": 7.0 if floor_source == gating.FLOOR_SEARCH_BOUND else 4.19,
        "window": "half_hann_tail",
        "floor_source": floor_source,
        "f_valid_floor_hz": 357.1,
        "f_trusted_hz": 892.9,
        "direct_peak_ms": 1.02,
        "first_reflection_ms": 5.33 if floor_source == gating.FLOOR_MEASURED else None,
    }


def _gate_record(floor_source: str) -> dict:
    """What the conductor persists — ``crossover_v2_flow._gate_record``'s shape,
    built here through the SAME single writer the conductor calls."""
    block = _gate_block(floor_source)
    return {
        "disclosure": describe_gate(block),
        "reflection_measured": floor_source == gating.FLOOR_MEASURED,
    }


_GATE_CEILING = _gate_record(gating.FLOOR_SEARCH_BOUND)
_GATE_MEASURED = _gate_record(gating.FLOOR_MEASURED)


@pytest.mark.parametrize("phase, extra", [
    ("verify", {"failure": {"code": REASON_VERIFY_INCONCLUSIVE}}),
    ("done", {"candidate": _candidate_summary()}),
])
def test_the_gate_sentence_renders_verbatim_on_the_household_screens(phase, extra):
    """The render slot, and the single-writer provenance of what lands in it.

    ``describe_gate`` owns this sentence and its own docstring says consumers
    render it rather than re-phrasing the fields — re-phrasing is how "a
    reflection was measured" and "no reflection found; window capped" started
    printing identically (#1966). So this asserts EQUALITY against that
    function's output on both screens: a line that merely contained the same
    numbers, or carried a helpful prefix, would fail.
    """
    env = build_crossover_envelope_v2(_status(
        phase=phase,
        verify={"outcome": "inconclusive", "code": REASON_VERIFY_INCONCLUSIVE,
                "gate": _GATE_CEILING},
        **extra,
    ))
    expected = describe_gate(_gate_block(gating.FLOOR_SEARCH_BOUND))
    assert env["expert_details"].count(expected) == 1
    # And the sentence is the one that says the ceiling case out loud, which is
    # what the whole 2026-07-30 corpus actually was.
    assert "no reflection found" in expected
    assert "nothing was gated out" in expected


def test_the_gate_sentence_rides_a_passing_verify_too():
    """On EVERY outcome, like the graded band and the frame it sits beside: the
    screen that says "Verified." is exactly the one where an unstated validity
    floor lets a household read the claim as wider than it is."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "graded_band_hz": [2000.0, 4000.0],
                "gate": _GATE_MEASURED},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert describe_gate(_gate_block(gating.FLOOR_MEASURED)) in env["expert_details"]


_GATE_UNGATEABLE = {
    # ``describe_gate``'s ONE deictic sentence — "this capture …" — whose
    # referent comes from the screen rather than from the sentence. Persisted
    # whenever a concluding verdict ran over a capture that could not be gated
    # (a truthy gating block with no ``floor_source``).
    "disclosure": describe_gate({"direct_peak_ms": 1.0}),
    "reflection_measured": False,
}


def test_the_gate_line_is_dropped_when_the_headline_is_another_attempts():
    """The deictic-referent hole the PR #1994 delta review found.

    The gate record deliberately survives an early-return retry — it is
    written with the outcome and code it belongs to — while ``failure.code``
    moves on, and ``_failure_envelope`` routes ANY code through the
    verify_fail template once the crossover is applied. The evidence, band and
    frame lines ARE cleared per attempt, so the gate line renders alone: "this
    capture could not be gated" as the only expert line under a LATER
    capture's headline, pointing at the wrong capture.

    So the line renders only when the headline's verdict is the one that wrote
    the record.
    """
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        applied=True,
        failure={"code": REASON_LOCATE_FAILED},
        verify={
            "outcome": "fail",
            # The verdict that CONCLUDED, two attempts ago.
            "code": REASON_VERIFY_OUT_OF_TOLERANCE,
            "gate": _GATE_UNGATEABLE,
        },
    ))
    assert env["screen"] == "verify_fail"
    assert not any("this capture" in line for line in env["expert_details"])
    assert env["expert_details"] == []


def test_the_gate_line_renders_when_the_headline_wrote_the_record():
    """The other side of the same rule — and the common case. A verify_fail
    screen whose failure code IS the concluding verdict is describing that
    verdict's own capture, so the disclosure belongs there."""
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        applied=True,
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
        verify={
            "outcome": "fail",
            "code": REASON_VERIFY_OUT_OF_TOLERANCE,
            "gate": _GATE_UNGATEABLE,
        },
    ))
    assert env["screen"] == "verify_fail"
    assert _GATE_UNGATEABLE["disclosure"] in env["expert_details"]


def test_the_done_screen_keeps_the_gate_line_regardless_of_any_later_failure():
    """The done screen explains the verdict the record belongs to, so its
    referent is never ambiguous — the headline-match rule is a verify_fail
    rule only, and must not cost the done screen its disclosure."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "fail",
            "code": REASON_VERIFY_OUT_OF_TOLERANCE,
            "gate": _GATE_UNGATEABLE,
        },
        candidate=_candidate_summary(),
        post_apply_grade={"state": "failed", "graded": False},
    ))
    assert env["screen"] == "done"
    assert _GATE_UNGATEABLE["disclosure"] in env["expert_details"]


def test_no_gate_sentence_when_the_record_carries_none():
    """Absence stays absence — the #1987 rule applied to this record. A state
    file written before R9 renders no gate line at all rather than a fabricated
    one, and nothing on the screen claims a window."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "graded_band_hz": [2000.0, 4000.0]},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert not any("reflection" in line for line in env["expert_details"])
    assert not any("window" in line for line in env["expert_details"])


# --- #1974: the inconclusive copy, per path --------------------------------------


def test_verify_fail_inconclusive_blames_a_reflection_only_when_one_was_found():
    """The #1974 fix at the screen that raised it. This copy asserted "the room
    reflection cut the window short" on every capture that reached it, without
    ever consulting whether a reflection had been found — and across the whole
    2026-07-30 corpus none had."""
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": REASON_VERIFY_INCONCLUSIVE},
        verify={"outcome": "inconclusive", "code": REASON_VERIFY_INCONCLUSIVE,
                "gate": _GATE_MEASURED},
    ))
    assert env["screen"] == "verify_fail"
    assert env["verdict_text"] == verify_inconclusive_message(True)
    assert "a reflection reached the microphone sooner" in env["verdict_text"]


def test_verify_fail_inconclusive_claims_no_reflection_when_the_window_was_capped():
    """The state the corpus was actually in: the search ran to its ceiling and
    found nothing, so the window was capped rather than cut. Nothing was proven
    about reflections and the sentence must not imply otherwise."""
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": REASON_VERIFY_INCONCLUSIVE},
        verify={"outcome": "inconclusive", "code": REASON_VERIFY_INCONCLUSIVE,
                "gate": _GATE_CEILING},
    ))
    assert env["verdict_text"] == verify_inconclusive_message(False)
    assert "reflection" not in env["verdict_text"]
    assert "room" not in env["verdict_text"]


def test_the_registry_holds_the_cause_unknown_rendering_not_a_literal():
    """SSOT: the sentence has ONE writer, and the registry entry is that
    writer's cause-unknown output rather than a second copy of the words that
    could drift from it. Any reader of REASON_REGISTRY therefore gets copy that
    is true, not copy that guesses."""
    assert (
        REASON_REGISTRY[REASON_VERIFY_INCONCLUSIVE].message
        == verify_inconclusive_message(None)
    )
    assert "reflection" not in REASON_REGISTRY[REASON_VERIFY_INCONCLUSIVE].message


@pytest.mark.parametrize("code, gate, expected_fragment", [
    (REASON_VERIFY_INCONCLUSIVE, _GATE_MEASURED,
     "a reflection reached the microphone sooner than it did during tuning, "
     "so there was less of the sound to compare"),
    (REASON_VERIFY_INCONCLUSIVE, _GATE_CEILING,
     "this measurement had less usable sound to compare than the tuning did"),
    (REASON_VERIFY_LEVEL_SHIFT, _GATE_CEILING,
     "the microphone's levels changed between measurements"),
])
def test_the_done_screen_names_the_cause_of_its_own_path(
    code, gate, expected_fragment,
):
    """The household-visible half of #1974. This screen carried its OWN
    paraphrase of the reflection claim, and rendered it for BOTH roads to
    "inconclusive" — including the pilot level-shift road, where no reflection
    and no window are involved at all.

    The clause now comes from the same single writer the verify_fail screen
    uses, so the two surfaces cannot drift apart again the way they did.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "inconclusive", "code": code, "gate": gate},
        candidate=_candidate_summary(),
        post_apply_grade={"state": "inconclusive", "graded": False},
    ))
    verdict = env["verdict_text"]
    assert env["screen"] == "done"
    assert "could not tell either way" in verdict
    assert expected_fragment in verdict
    assert verdict.endswith(
        "Re-verify to try again, or undo to restore the previous sound."
    )
    assert verify_inconclusive_cause(
        code, gate["reflection_measured"],
    ) == expected_fragment
    if code == REASON_VERIFY_LEVEL_SHIFT or gate is _GATE_CEILING:
        assert "reflection" not in verdict


def test_the_done_screen_names_no_cause_a_legacy_record_cannot_support():
    """A durable state written before #1974 records neither the verdict nor the
    gate. The outcome is still stated — it is the household's own screen — but
    the cause is left unnamed rather than guessed, which is the same
    absent-is-absent rule the gate line above follows."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "inconclusive"},
        candidate=_candidate_summary(),
        post_apply_grade={"state": "inconclusive", "graded": False},
    ))
    verdict = env["verdict_text"]
    assert verdict == (
        "Your speaker is tuned, but the check that confirms it could not tell "
        "either way. Re-verify to try again, or undo to restore the previous "
        "sound."
    )
    assert "reflection" not in verdict
    assert "—" not in verdict


def test_the_never_finished_verdict_is_untouched_by_the_inconclusive_fix():
    """The sibling branch, pinned so the #1974 restructure cannot bleed into
    it: "never finished" and "could not tell either way" are different things
    to tell someone and point at different fixes."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "fail"},
        candidate=_candidate_summary(),
        post_apply_grade={"state": "unverified", "graded": False},
    ))
    assert "never finished" in env["verdict_text"]
    assert "unverified" in env["verdict_text"]
    assert "could not tell either way" not in env["verdict_text"]


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


@pytest.mark.parametrize("tier", (None, "full"))
def test_cloud_measure_flatness_never_renders_as_the_speakers_flatness(tier):
    """``cloud_measure`` is the PRE-APPLY, uncorrected baseline that exists in
    order to be out of spec — the same distinction PR-4's doctor blocker drew.
    Rendering it as the CURRENT state would report a correctly-corrected
    speaker as bad forever.

    **The invariant that survives #1965 is the FRAME, not the silence.** These
    two cases used to assert ``expert_details == []``, which enforced the frame
    by rendering nothing at all — and that is precisely what left the FULL tier
    showing LESS measured evidence than Express on every stage-1 screen. A
    state with no post-apply cloud now renders the same measured numbers under
    the explicit BEFORE-TUNING lead, on every tier; what must never happen is
    the BARE rendering the post-apply CLOUD-VERIFY path produces, which is the
    one that reads as "how flat your speaker is now".

    (The former ``…_tier_full`` sibling is the ``"full"`` parameter here. It
    existed to confirm the B1 tier branch read the durable tier rather than its
    absence; there is no tier branch any more — the choice is which cloud
    exists — so the case is kept as coverage, not as its own claim.)
    """
    extra = {"tier": tier} if tier is not None else {}
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
        cloud={PHASE_CLOUD_MEASURE: _cloud_flatness_status()[PHASE_CLOUD_VERIFY]},
        **extra,
    ))
    details = env["expert_details"]
    assert details, "the measured pre-apply cloud must not be withheld (#1965)"
    assert details[0].startswith("Measured before tuning: ")
    assert not any(line.startswith("flatness ") for line in details)


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
    assert (
        "flatness -4.85 dB from the 250–8000 Hz reference mean at 11480 Hz"
    ) in combined
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
    actual cause (the mic chain moved, not the speaker)."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_VERIFY_LEVEL_SHIFT},
    ))
    assert env["screen"] == "verify_fail"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_VERIFY_LEVEL_SHIFT].message
    assert env["next_action"]["label"] == "Try again"
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Undo (restore previous sound)" in labels


def test_verify_level_shift_copy_matches_the_controls_on_its_own_screen():
    """#1924's routing half, checked against the rendered action row: every
    control the sentence names is on this screen, and the sentence names the
    VISIBLE PRIMARY rather than routing around it. On the wizard that retry
    mints a fresh session, which since #1927 re-baselines — so it settles this
    verdict in one capture and must not be discredited."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_VERIFY_LEVEL_SHIFT},
    ))
    verdict = env["verdict_text"]
    assert "Try again" in verdict
    assert "re-measure" in verdict
    assert "undo" in verdict
    assert "re-verify" not in verdict.lower()
    assert env["next_action"]["label"] == "Try again"
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Re-measure" in labels
    assert "Undo (restore previous sound)" in labels


def test_level_reference_reset_is_disclosed_on_a_failed_verify():
    """#1927's disclosure: one dated line in the collapsed expert details,
    saying the reference was set fresh and how far the previous one sat."""
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": REASON_VERIFY_LEVEL_SHIFT},
        verify={
            "outcome": "inconclusive",
            "level_reference": {
                "prior_at": time.time() - 24 * 60 * 60, "step_db": 0.775,
            },
        },
    ))
    assert (
        "level reference reset for this session "
        "(the previous one, yesterday, was 0.78 dB away)"
    ) in env["expert_details"]


def test_level_reference_reset_is_disclosed_on_a_passing_verify():
    """Rendered on EVERY outcome, like the graded band beside it: a PASS is
    exactly when an unstated reset would let a household read cross-day
    identity into a claim that only ever covered this sitting."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="express",
        verify={
            "outcome": "pass",
            "level_reference": {
                "prior_at": time.time() - 24 * 60 * 60, "step_db": 1.2,
            },
        },
        candidate=_candidate_summary(),
    ))
    assert env["screen"] == "done"
    assert [line for line in env["expert_details"] if "level reference" in line] == [
        "level reference reset for this session "
        "(the previous one, yesterday, was 1.20 dB away)"
    ]


def test_level_reference_disclosure_is_silent_without_a_reset():
    """No prior, or a prior this session's own chain agreed with, says
    nothing — the reset itself is unconditional and unremarkable."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="express", verify={"outcome": "pass"},
        candidate=_candidate_summary(),
    ))
    assert env["expert_details"] == []


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
    assert env["schema_version"] == 12
    assert env["screen"]
    assert env["verdict_text"]


# --- #1942: a failure that outlived its session is history, not the screen ------
#
# The owner opened /correction/crossover/ on a fresh visit and was greeted by
# the PREVIOUS DAY's terminal screen — "The measurement link timed out… The
# crossover was already applied… Expert details: level error 3.82 dB (limit
# 1.5 dB)" — presented as the live verdict, with Try again / Undo / Re-measure.
# The 3.82 dB is the giveaway: last session's stored evidence, replayed as if
# it were now. #1941 R11: never greet a returning household with a stale
# terminal state; report it as dated history, and keep Undo reachable whenever
# something is applied.

_DAY_S = 24 * 60 * 60
# Old enough to render as a calendar date rather than "yesterday".
_WEEK_S = 7 * _DAY_S

# The owner's actual stale numbers, so these tests fail on the real screen.
_PRIOR_SESSION_EVIDENCE = {
    "outcome": "fail",
    "evidence": {
        "max_db": 3.82,
        "tolerance_db": 1.5,
        "rms_db": 1.46,
    },
    "graded_band_hz": [2000.0, 4000.0],
}

# The SECOND numbers surface (gate finding on the first round of this fix).
# `_envelope` copies `cloud` / `cloud_chart` / `tier` from status on every
# screen, `persist_conductor_state` writes `cloud` beside `failure`, and
# `crossover/main.js` calls `renderCloud` with no screen switch — so without
# suppression the aged greeting paints the dead session's before/after chart
# card, its spec-band numbers, and a caption promising that the after-
# correction curve "appears once the second measurement pass finishes".
#
# Note `cloud[phase]["session_id"]`: the block already stamps its producing
# session, and on an aged resume that id EQUALS the state's own `session_id`
# (it is the same dead session), so a provenance filter downstream could not
# have caught this. The aged branch declaring "no session" is the fix.
_PRIOR_SESSION_CLOUD = {
    "cloud_measure": {
        "geometry": {"verdict": "ok"},
        "positions": [["mark", 1]],
        "pipeline": {"spec": {"bands": [{"name": "handoff", "max_deviation_db": 6.66,
                                         "tolerance_db": 3.0, "passed": False}]}},
        "session_id": "cap_dead_session",
    },
}
_PRIOR_SESSION_CLOUD_CHART = {
    "cloud_measure": {"curve": [[100.0, -6.66], [1000.0, -3.0]]},
}


def _stale_measurement_status(code: str, *, age_s: float = _DAY_S, **v2) -> dict:
    """The full returning-household state: an aged failure over a dead
    session that left BOTH numbers surfaces populated."""
    return _aged_status(
        code, age_s=age_s, applied=True, session_id="cap_dead_session",
        verify=_PRIOR_SESSION_EVIDENCE,
        cloud=_PRIOR_SESSION_CLOUD,
        cloud_chart=_PRIOR_SESSION_CLOUD_CHART,
        tier="full",
        **v2,
    )


def _aged_status(code: str, *, age_s: float = _DAY_S, **v2) -> dict:
    """A durable failure written ``age_s`` ago — the returning household."""
    return _status(failure={"code": code, "at": time.time() - age_s}, **v2)


def _history_note(env: dict) -> str:
    """The ONE quiet history line, asserting it is the only nudge on screen."""
    assert len(env["nudges"]) == 1, env["nudges"]
    note = env["nudges"][0]
    assert note["severity"] == "info", "history is not a problem to solve"
    return note["text"]


def _labels(env: dict) -> list[str]:
    return [action["label"] for action in env["alternate_actions"]]


def test_aged_failure_greets_with_the_entry_screen_not_the_terminal_one():
    """The headline acceptance: a day-old failure over an APPLIED crossover
    renders the entry / tier-choice screen, not verify_fail."""
    env = build_crossover_envelope_v2(_aged_status(
        REASON_RELAY_TIMEOUT, phase="verify", applied=True,
        verify=_PRIOR_SESSION_EVIDENCE,
    ))
    assert env["screen"] == "microphone_check"
    # A way forward, and it is the ordinary one — start a measurement.
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/session"
    # Not the terminal screen's actions.
    assert env["next_action"]["id"] != "verify_retry"


def test_aged_failure_never_replays_the_previous_sessions_numbers():
    """The 3.82 dB defect itself, over BOTH numbers surfaces.

    An earlier revision of this test populated only ``verify.evidence`` and so
    passed vacuously while the chart card happily rendered the dead session's
    curve and spec bands. The fixture now populates `cloud`/`cloud_chart` too,
    which is what the real state file carries beside `failure`."""
    env = build_crossover_envelope_v2(
        _stale_measurement_status(REASON_VERIFY_OUT_OF_TOLERANCE, phase="verify"),
    )
    # Surface 1: the collapsed expert disclosure.
    assert env["expert_details"] == []
    # Surface 2: the before/after chart card (main.js renderCloud, no screen
    # switch — it draws from whatever these keys carry).
    assert env["cloud"] is None
    assert env["cloud_chart"] is None
    assert env["tier"] is None
    # Belt and braces: no stale number survives anywhere in the payload,
    # however a future key might carry it.
    rendered = repr(env)
    for stale in ("3.82", "1.46", "2000", "6.66", "cap_dead_session"):
        assert stale not in rendered, f"{stale} crossed sessions: {rendered}"


def test_aged_failure_fixture_would_paint_a_chart_without_the_fix():
    """Guards the guard: proves the fixture above is not vacuous a SECOND
    time. The same status rendered while the failure is FRESH still carries
    both numbers surfaces, so the assertions above are testing suppression
    rather than an absence that was there all along."""
    fresh = _stale_measurement_status(REASON_VERIFY_OUT_OF_TOLERANCE,
                                      age_s=0, phase="verify")
    env = build_crossover_envelope_v2(fresh)
    assert env["screen"] == "verify_fail"
    assert env["cloud"] is not None
    assert env["cloud_chart"] is not None
    assert env["tier"] == "full"
    assert "6.66" in repr(env)


def test_aged_failure_keeps_undo_reachable_while_applied():
    """W6.7 ruling 3 is not weakened by the resume: the household is entitled
    to Undo the moment something is live on the speaker, and an aged failure
    does not make the applied graph any less live."""
    env = build_crossover_envelope_v2(_aged_status(
        REASON_RELAY_TIMEOUT, phase="verify", applied=True,
    ))
    undo = [a for a in env["alternate_actions"] if a["id"] == "verify_undo"]
    assert len(undo) == 1
    # The v2-aware restore path, not the legacy one that 500s here.
    assert undo[0]["endpoint"] == "/correction/crossover/v2/restore"


def test_aged_failure_offers_no_undo_when_nothing_was_applied():
    """The converse (done-screen rule D6): Undo restores what an apply
    changed, so a session that died before applying must not offer to
    "restore" a speaker that was never changed."""
    env = build_crossover_envelope_v2(_aged_status(
        REASON_RELAY_TIMEOUT, phase="check", applied=False,
    ))
    assert env["screen"] == "microphone_check"
    assert not any("Undo" in label for label in _labels(env))


def test_aged_entry_screen_differs_from_a_clean_start_in_EXACTLY_two_keys():
    """IA over copy (#1941 design principle 1), pinned over the FULL envelope.

    The resume is not a new screen: it is the same entry screen a clean start
    renders, differing only by one quiet history nudge and — because something
    is applied — Undo. An earlier revision of this test hand-listed six keys
    to compare and so let `cloud` / `cloud_chart` / `tier` diverge unnoticed;
    comparing every key means a third diverging key can never pass again."""
    clean = build_crossover_envelope_v2(_status(phase="check"))
    aged = build_crossover_envelope_v2(
        _stale_measurement_status(REASON_RELAY_TIMEOUT, phase="verify"),
    )
    diverged = {
        key for key in set(clean) | set(aged)
        if clean.get(key) != aged.get(key)
    }
    assert diverged == {"alternate_actions", "nudges"}, (
        f"aged resume diverges from a clean start in {sorted(diverged)}; only "
        "the history nudge and the Undo action may differ"
    )
    # And the two intended deltas are exactly what they claim to be.
    assert clean["nudges"] == []
    assert len(aged["nudges"]) == 1
    assert [a for a in aged["alternate_actions"]
            if a not in clean["alternate_actions"]] == [
        {
            "id": "verify_undo",
            "label": "Undo (restore previous sound)",
            "endpoint": "/correction/crossover/v2/restore",
            "body": {},
            "show_during_relay": True,
        },
    ]


def test_undo_action_never_shares_mutable_state_between_envelopes():
    """The action carries a mutable ``body``. If it came from one module-level
    dict copied shallowly, every envelope this process ever served would share
    that dict — one mutation would poison the daemon for its whole life."""
    first = build_crossover_envelope_v2(_aged_status(
        REASON_RELAY_TIMEOUT, phase="verify", applied=True,
    ))
    undo = [a for a in first["alternate_actions"] if a["id"] == "verify_undo"][0]
    undo["body"]["poisoned"] = True

    second = build_crossover_envelope_v2(_aged_status(
        REASON_RELAY_TIMEOUT, phase="verify", applied=True,
    ))
    assert [a for a in second["alternate_actions"]
            if a["id"] == "verify_undo"][0]["body"] == {}
    # The live verify-fail screen shares the same factory, so it is covered
    # by the same guarantee.
    live = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
    ))
    assert [a for a in live["alternate_actions"]
            if a["id"] == "verify_undo"][0]["body"] == {}


def test_aged_failure_note_is_one_quiet_dated_line():
    """"Your last session ended with X on <date>" — dated, because an undated
    outcome on a resume is precisely what read as a live verdict."""
    at = time.time() - _WEEK_S
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE, "at": at},
    ))
    note = _history_note(env)
    assert note == (
        f"Your last measurement ended on {time.strftime('%B %-d', time.localtime(at))}"
        " — the check didn't pass."
    )
    # One LINE, not a paragraph (#1941: "clearer" is never "more words").
    assert len(note.split()) <= 12
    assert note.count(".") == 1


@pytest.mark.parametrize("age_s,expected", [
    (2 * 60 * 60, "earlier today"),
    (_DAY_S, "yesterday"),
])
def test_recent_history_reads_as_a_phrase_not_a_date(age_s, expected):
    """A bare "on July 30" when today IS July 30 makes the household decode a
    date into "oh, this morning". Follows the today/yesterday shape of
    ``jasper.tools._format_relative_date``, the household-date precedent
    already in the tree. Reachable in ordinary use: the freshness window is
    30 minutes, so a same-day aged failure is not exotic."""
    at = time.time() - age_s
    if age_s < _DAY_S and time.localtime(at).tm_yday != time.localtime().tm_yday:
        pytest.skip("clock is within 2 h of local midnight")
    note = _history_note(build_crossover_envelope_v2(
        _aged_status(REASON_VERIFY_OUT_OF_TOLERANCE, age_s=age_s),
    ))
    assert note == f"Your last measurement ended {expected} — the check didn't pass."


def test_aged_failure_note_dates_a_previous_year_explicitly():
    """A speaker that sat unused for a year must not date its history with a
    bare "July 29" that reads as this year — the same year rule household
    dates already follow elsewhere in the tree (jasper.tools.gmail)."""
    stamp = time.localtime(time.time() - 400 * _DAY_S)
    env = build_crossover_envelope_v2(_aged_status(
        REASON_RELAY_TIMEOUT, age_s=400 * _DAY_S, phase="check",
    ))
    assert str(stamp.tm_year) in _history_note(env)


# --- B2: a reason whose copy is durable state, not a session outcome ---------


def test_rollback_failed_keeps_its_fact_and_its_instruction_when_aged():
    """`correction_rollback_failed` says the speaker is STILL playing a tuning
    JTS measured as faulty, and that Undo is what fixes it. That is true today
    and true tomorrow. Aging it into a generic "it couldn't continue" would
    leave the household on the faulty correction with the reason to press Undo
    deleted — so this row keeps its fact and its instruction inside the dated
    history treatment."""
    from jasper.active_speaker.crossover_v2_flow import (
        REASON_CORRECTION_ROLLBACK_FAILED,
    )

    env = build_crossover_envelope_v2(
        _stale_measurement_status(REASON_CORRECTION_ROLLBACK_FAILED, phase="verify"),
    )
    # Still the entry screen — exempt from the generic COPY, not from aging.
    assert env["screen"] == "microphone_check"
    note = _history_note(env)
    assert "still applied" in note
    assert "Undo restores it" in note
    assert "it didn't finish" not in note
    # And the button the sentence names is actually on the screen.
    assert any("Undo" in label for label in _labels(env))


def test_rollback_failed_falls_back_to_the_generic_note_when_nothing_is_applied():
    """The exemption is gated on the state still corroborating the claim.
    "The newer tuning is still applied" is not a sentence to print over a
    durable state that says nothing is applied — that would be the #1942
    defect wearing the fix's clothes."""
    from jasper.active_speaker.crossover_v2_flow import (
        REASON_CORRECTION_ROLLBACK_FAILED,
    )

    env = build_crossover_envelope_v2(_aged_status(
        REASON_CORRECTION_ROLLBACK_FAILED, phase="check", applied=False,
    ))
    note = _history_note(env)
    assert "still applied" not in note
    # Falls back to its TEMPLATE's generic clause (hard_stop), like any other.
    assert note.endswith("— it couldn't continue.")


def test_only_audited_durable_state_codes_are_exempt():
    """The exemption list is a considered audit of the whole registry, not a
    place things drift into. A new exempt row is a copy decision that needs
    its own reasoning — see _DURABLE_STATE_FACTS for the line and the three
    shapes deliberately left off it."""
    from jasper.active_speaker.crossover_envelope_v2 import _DURABLE_STATE_FACTS
    from jasper.active_speaker.crossover_v2_flow import (
        REASON_CORRECTION_ROLLBACK_FAILED,
    )

    assert set(_DURABLE_STATE_FACTS) == {REASON_CORRECTION_ROLLBACK_FAILED}
    assert set(_DURABLE_STATE_FACTS) <= set(REASON_REGISTRY)


@pytest.mark.parametrize("code,expected", [
    (REASON_VERIFY_OUT_OF_TOLERANCE, "the check didn't pass"),
    (REASON_RELAY_TIMEOUT, "it stopped before finishing"),
    (REASON_USER_STOPPED, "it stopped before finishing"),
    (REASON_CHANNEL_MAP_MISMATCH, "it couldn't continue"),
    (REASON_CLIPPED, "it didn't finish"),
    ("some_future_code", "it didn't finish"),
])
def test_aged_failure_note_states_the_shape_not_the_live_instruction(code, expected):
    """The note reports WHAT happened, never the terminal screen's fix
    instruction — "Start over from this page to measure again" is advice for a
    session that is over, and would be both stale and a wall of text here."""
    note = _history_note(build_crossover_envelope_v2(_aged_status(code)))
    assert note.endswith(f"— {expected}.")
    spec = REASON_REGISTRY.get(code)
    if spec is not None and spec.message:
        assert spec.message not in note


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("code", sorted(REASON_REGISTRY))
def test_every_registry_code_ages_into_a_household_readable_note(code, applied):
    """No reason code can age into a bare slug, an empty line, an unterminated
    sentence, or copy that names hardware the flow does not talk about (#1941
    R4: the actor is the microphone; household copy never says "phone").

    Both `applied` values, because that flag selects between the generic
    reason clause and a `_DURABLE_STATE_FACTS` row's own sentence — every
    code has to read well down both branches."""
    env = build_crossover_envelope_v2(
        _aged_status(code, age_s=_WEEK_S, phase="measure", applied=applied),
    )
    assert env["screen"] == "microphone_check"
    note = _history_note(env)
    assert note.startswith("Your last measurement ended on ")
    assert note.endswith(".")
    assert code not in note
    assert "phone" not in note.lower()
    # Sentence-cased, never a clause spliced in mid-word.
    assert "  " not in note


def test_failure_without_a_timestamp_reads_as_aged():
    """Migration, fail-honest. Every failure written before #1942 is undated,
    and the state file's schema version is deliberately NOT bumped for the new
    key (a bump makes load_v2_state reject every deployed Pi's file, which
    would discard pre_apply_profile and take Undo with it). Undated means "we
    cannot say this is current", so it renders as history — and says "earlier"
    rather than inventing a date."""
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        # Built inline: _status() would stamp it fresh. This is exactly the
        # record shape a pre-#1942 build left on disk.
        "crossover_v2": {
            "phase": "verify",
            "applied": True,
            "failure": {"code": REASON_VERIFY_OUT_OF_TOLERANCE},
            "verify": _PRIOR_SESSION_EVIDENCE,
        },
    })
    assert env["screen"] == "microphone_check"
    assert env["expert_details"] == []
    assert _history_note(env) == (
        "Your last measurement ended earlier — the check didn't pass."
    )
    assert any("Undo" in label for label in _labels(env))


@pytest.mark.parametrize("stamp", [
    "yesterday", None, float("nan"), float("inf"), True,
    # Finite but out of the platform's calendar range. `time.localtime`
    # raises OSError below roughly -1e16 on glibc, and an uncaught raise here
    # is a 500 on the wizard's MAIN GET — the whole screen, not a corner.
    -1e16, -1e18, -1.7e308,
    # Finite, in range, and absurd: -1e11 renders as the year -1199. A
    # nonsense date is not better than no date.
    -1e11, -1.0,
])
def test_unreadable_timestamp_reads_as_aged_and_never_raises(stamp):
    """A stamp that cannot be believed is not evidence of currency. Same
    direction as a missing one — never assert a live screen off a value the
    envelope could not parse, and never let a corrupt byte on disk turn the
    entry screen into a 500."""
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "check", "failure": {"code": REASON_RELAY_TIMEOUT, "at": stamp},
        },
    })
    assert env["screen"] == "microphone_check"
    note = _history_note(env)
    # No date claim at all, rather than a fabricated or nonsensical one.
    assert note == "Your last measurement ended earlier — it stopped before finishing."


# --- the fresh path, unchanged ---------------------------------------------------


def test_fresh_failure_still_renders_todays_terminal_screen_exactly():
    """No regression to the live path. A failure the household is looking at
    right now renders the screen it renders today, numbers and all — this is
    the case the recency check must leave completely alone."""
    fresh = {"code": REASON_RELAY_TIMEOUT, "at": time.time()}
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, failure=fresh,
        verify=_PRIOR_SESSION_EVIDENCE,
    ))
    assert env["screen"] == "verify_fail"
    assert env["next_action"]["id"] == "verify_retry"
    assert _labels(env) == ["Undo (restore previous sound)", "Re-measure"]
    # The applied-override addendum and this session's own numbers, intact.
    assert "you can undo" in env["verdict_text"]
    assert "level error 3.82 dB (limit 1.5 dB)" in env["expert_details"]


def test_freshness_boundary_is_the_declared_window():
    """The window is a stated contract, not an accident of rounding: just
    inside it is the live screen, just outside it is history."""
    from jasper.active_speaker.crossover_envelope_v2 import FAILURE_FRESH_WINDOW_S

    inside = build_crossover_envelope_v2(_aged_status(
        REASON_VERIFY_OUT_OF_TOLERANCE, age_s=FAILURE_FRESH_WINDOW_S - 30,
        phase="verify", applied=True,
    ))
    outside = build_crossover_envelope_v2(_aged_status(
        REASON_VERIFY_OUT_OF_TOLERANCE, age_s=FAILURE_FRESH_WINDOW_S + 30,
        phase="verify", applied=True,
    ))
    assert inside["screen"] == "verify_fail"
    assert outside["screen"] == "microphone_check"


def test_a_clock_that_stepped_backward_reads_as_fresh():
    """A Pi has no RTC, so a stamp can land in the future after an NTP step.
    Fresh is the safe answer: the worst case is the screen that ships today,
    where aging out a live failure would strand the household mid-session."""
    env = build_crossover_envelope_v2(_aged_status(
        REASON_VERIFY_OUT_OF_TOLERANCE, age_s=-_DAY_S, phase="verify", applied=True,
    ))
    assert env["screen"] == "verify_fail"


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


@pytest.mark.parametrize("tier", ("express", "full"))
def test_review_puts_the_measured_flatness_where_it_informs_the_decision(tier):
    """D3.1: the pre-apply cloud IS the measured evidence on this screen, so its
    flatness/carve-out disclosure belongs here — the same lines the RESULT
    screen folds away, on the screen where they inform a choice rather than
    explain a fait accompli.

    **Parametrized by #1965.** This pinned ``tier="express"`` only, which is
    exactly why the Full-tier hole survived: ``_flatness_details_lines`` read
    ``_cloud_verify_block`` for every non-Express tier, and the post-apply
    cloud does not exist at stage 1, so the tier the household spent the most
    time on rendered NOTHING on its own decision screen.
    """
    env = build_crossover_envelope_v2(_review_status(
        tier=tier,
        cloud={PHASE_CLOUD_MEASURE: {
            "flatness": {
                "evaluable": True, "max_db": 6.2, "max_hz": 310.0,
                "tolerance_db": 3.0, "max_band_hz": [250.0, 500.0],
            },
        }},
    ))
    assert any("flatness" in line for line in env["expert_details"])


def test_full_review_carries_at_least_the_evidence_express_carries():
    """#1965: the tier with MORE measurement may never show LESS measured
    evidence on the decision screen.

    Both tiers walk a pre-apply cloud, both have closed it by the time the
    review screen renders, and neither has a post-apply cloud yet — so the
    measured evidence available at stage 1 is the same evidence, and the two
    screens must read the same. (Equality, not containment: the numbers come
    from one construction — ``_flatness_lines_from_block`` — over one block,
    so any divergence here would mean a tier branch had crept back in.)
    """
    cloud = {PHASE_CLOUD_MEASURE: _cloud_flatness_status()[PHASE_CLOUD_VERIFY]}
    express = build_crossover_envelope_v2(
        _review_status(tier="express", cloud=cloud),
    )["expert_details"]
    full = build_crossover_envelope_v2(
        _review_status(tier="full", cloud=cloud),
    )["expert_details"]
    assert express, "fixture must produce evidence for the comparison to mean anything"
    assert full == express
    # (The scope clause's own suppression is pinned across all three fixed
    # screens by ``test_the_before_tuning_scope_clause_waits_for_a_passing_check``
    # below; this line keeps the review case beside the equality it belongs to.)
    # …and it is still framed as the BEFORE state, on both tiers: stage 1 has
    # applied nothing, so nothing here may read as "how flat your speaker is".
    assert full[0].startswith("Measured before tuning: ")
    assert not any("confirmed at the mark" in line for line in full)


@pytest.mark.parametrize("tier", ("express", "full"))
@pytest.mark.parametrize("v2", (
    # The three screens #1965 gave the Full tier back. None of them has a
    # PASSING post-apply check, so none may carry the clause that claims one.
    pytest.param({"phase": "review", "candidate": _candidate_summary()}, id="review"),
    pytest.param({"phase": "closing"}, id="closing"),
    pytest.param(
        {"phase": "verify", "verify": {"outcome": "fail"},
         "failure": {"code": REASON_VERIFY_OUT_OF_TOLERANCE}},
        id="verify_fail",
    ),
))
def test_the_before_tuning_scope_clause_waits_for_a_passing_check(v2, tier):
    """"The applied correction targets these; the result was confirmed at the
    mark only" is a claim about the POST-APPLY check — that one exists, and
    that it was the single anchor sweep. #1965 made these three screens read
    the pre-apply cloud on BOTH tiers, so the clause had to stop riding along
    unconditionally: at review and closing nothing is applied at all, and at
    verify_fail the check ran and FAILED.

    Pinned on both tiers and all three screens because the clause was already
    wrong on Express here before #1965 — pinning only the screen the issue
    named would leave the other two free to regain it.
    """
    env = build_crossover_envelope_v2(_status(
        tier=tier, cloud=_cloud_measure_flatness_status(), **v2,
    ))
    details = env["expert_details"]
    assert any("Measured before tuning: " in line for line in details), details
    assert not any("confirmed at the mark" in line for line in details), details
    assert not any("applied correction targets" in line for line in details), details


def test_both_before_tuning_arms_lead_with_the_same_words():
    """The pre-apply reader has two arms — a numeric one and an "every spec
    band was excluded" one — and both are the BEFORE state, so both open the
    same way. The non-evaluable arm's lead used to be lowercase, which is the
    shape a pin on one arm lets drift in the other."""
    env = build_crossover_envelope_v2(_status(
        phase="review", tier="full", candidate=_candidate_summary(),
        cloud=_cloud_measure_flatness_status(evaluable=False),
    ))
    assert env["expert_details"][0] == (
        "Measured before tuning: flatness could not be measured — every "
        "spec band was excluded or out of range"
    )


def test_the_before_tuning_scope_clause_renders_once_the_check_has_passed():
    """The other side of the gate — without which the test above would also
    pass if the clause were simply deleted. Express's done screen is the state
    the clause was written for and the only one where both its halves are
    true: a correction is applied, and the only confirmation of it was the
    single sweep at the mark."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="express", verify={"outcome": "pass"},
        candidate=_candidate_summary(), cloud=_cloud_measure_flatness_status(),
    ))
    combined = " ".join(env["expert_details"])
    assert "The applied correction targets these; the result was confirmed " \
           "at the mark only" in combined


def test_the_review_screen_moved_the_schema_version():
    """PR-T2's bump (9 → 10): the screen vocabulary gained ``review`` and the
    envelope gained ``prediction``. PR-T3's (10 → 11): the vocabulary gained
    ``closing`` and the envelope gained ``busy``. CC1's (11 → 12): the envelope
    gained ``findings``. All additive — no key removed or re-typed — so an
    unredeployed page ignores the new keys rather than refusing the envelope,
    the same property the 8 → 9 bump had."""
    env = build_crossover_envelope_v2(_review_status())
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 12
    assert "prediction" in env
    assert env["busy"] is False  # present on every screen, true on one
    # Present on EVERY screen, populated on two — the key's absence would make
    # a renderer branch on the screen, which is what the data-driven contract
    # exists to prevent.
    assert env["findings"] == []


# --- banked findings (WO-1's read half; panel lens C, CC1) --------------------
#
# The corpus-shaped fixture: the 2026-07-30 session's own frame-gate outcome
# (#1949's ruling class) as the durable projection carries it — the household
# sentence the attribution package minted and validated, plus the clock it was
# banked on. Built from the SHIPPED constant, never a copy of it: a test that
# retyped the sentence would keep passing while the product's own words drifted.


def _banked_finding(*, age_s: float = 0.0) -> list[dict]:
    from jasper.attribution.promotion import LEVEL_FRAME_HOUSEHOLD_COPY

    return [{
        "household_copy": LEVEL_FRAME_HOUSEHOLD_COPY,
        "at": time.time() - age_s,
    }]


def _done_status(**overrides) -> dict:
    v2 = {
        "phase": "done",
        "verify": {"outcome": "pass"},
        "candidate": _candidate_summary(),
    }
    v2.update(overrides)
    return _status(**v2)


def test_the_banked_frame_finding_reaches_both_decision_screens():
    """**The acceptance.** A finding the machine banked is READ BACK and shown.

    Panel lens C, W1: ``read_finding_set`` had zero non-test callers, so
    #1949's "bank a finding and proceed" was, to a household, just "proceed" —
    the flow started proceeding on disputed evidence at the exact moment its
    disclosure went from one sentence to none. The sentence lands on the two
    screens that owe it: the apply DECISION, and the RESULT.
    """
    from jasper.attribution.promotion import LEVEL_FRAME_HOUSEHOLD_COPY

    review = build_crossover_envelope_v2(_review_status(findings=_banked_finding()))
    done = build_crossover_envelope_v2(_done_status(findings=_banked_finding()))
    assert [f["text"] for f in review["findings"]] == [LEVEL_FRAME_HOUSEHOLD_COPY]
    assert [f["text"] for f in done["findings"]] == [LEVEL_FRAME_HOUSEHOLD_COPY]
    # One line, not a paragraph: the producer owns the sentence and this
    # screen adds nothing to it while it is current.
    assert len(review["findings"]) == 1


def test_no_internal_finding_vocabulary_ever_reaches_the_envelope():
    """§3.1's two vocabularies, enforced at the last hop before a household.

    ``mechanism`` names physics and may name hardware; ``evidence`` is raw
    scalars; ``confidence`` and the probe lists are the plan's internal
    routing. All of them are ops/forensic surfaces by the artifact's own
    definition. The projection carries none of them, so none can reach the
    wire — asserted over the WHOLE envelope rather than the findings key,
    because the failure that matters is a field arriving anywhere a renderer
    can reach it.
    """
    status = _review_status(findings=[{
        "household_copy": "Two measurements of this speaker disagreed.",
        "at": time.time(),
        # Fields a future writer might be tempted to add to the projection.
        # They are ignored on the way through, not rendered somewhere quieter.
        "mechanism": "M7",
        "confidence": "unsure",
        "fix_class": "refit",
        "evidence": {"disagreement_db": 3.2307},
        "probes_recommended": ["P6"],
    }])
    env = build_crossover_envelope_v2(status)
    assert [f["text"] for f in env["findings"]] == [
        "Two measurements of this speaker disagreed."
    ]
    assert [set(f) for f in env["findings"]] == [{"text"}]
    rendered = repr(env)
    for internal in ("M7", "unsure", "refit", "disagreement_db", "3.2307", "P6"):
        assert internal not in rendered, f"{internal} reached a household surface"


def test_a_finding_from_an_earlier_day_reads_as_history():
    """#1944's lesson, one instrument over: history is presented AS history.

    The review interlude is untimed by construction and the done screen
    persists, so both are re-walkable days later — which is exactly how a
    household meets a finding banked last week. Undated, last week's diagnosis
    reads as this moment's verdict, which is the defect #1942 fixed for failure
    records. Same clock, same window, same date vocabulary.
    """
    from jasper.attribution.promotion import LEVEL_FRAME_HOUSEHOLD_COPY

    two_days = 2 * 24 * 60 * 60
    env = build_crossover_envelope_v2(
        _review_status(findings=_banked_finding(age_s=two_days))
    )
    line = env["findings"][0]["text"]
    assert line.startswith("From your measurement on ")
    # Dated, and still the producer's own sentence — never paraphrased.
    assert line.endswith(LEVEL_FRAME_HOUSEHOLD_COPY)


@pytest.mark.parametrize("age_s,expected", [
    (0.0, ""),                       # this moment: no date at all
    (29 * 60.0, ""),                 # still inside the freshness window
    (2 * 60 * 60.0, "earlier today"),  # outside it, same day
    (_DAY_S, "yesterday"),
])
def test_a_finding_is_dated_only_once_it_stops_being_this_moment(age_s, expected):
    """The boundary is the module's ONE freshness window, shared with the
    failure record — so a finding and a failure can never describe the same
    afternoon differently.

    The same near-midnight skip the failure-note parametrization carries, for
    the same reason: "earlier today" is a statement about the LOCAL calendar
    day, and a suite that runs at 00:30 would otherwise fail on a correct
    "on July 30".
    """
    at = time.time() - age_s
    if 0 < age_s < _DAY_S and time.localtime(at).tm_yday != time.localtime().tm_yday:
        pytest.skip("clock is within 2 h of local midnight")
    env = build_crossover_envelope_v2(
        _review_status(findings=_banked_finding(age_s=age_s))
    )
    line = env["findings"][0]["text"]
    if not expected:
        assert not line.startswith("From your measurement")
    else:
        assert line.startswith(f"From your measurement {expected}:")


def test_an_undated_finding_says_it_cannot_say_when():
    """A projection written without a clock (or with a corrupt one) is not
    current until proven current: it reads as history with no date CLAIM,
    which is the honest answer to "when was this?" when the record cannot
    say. Never a 500 on the wizard's main GET, and never a guessed date."""
    for at in (None, "not-a-time", float("nan"), -1e18):
        env = build_crossover_envelope_v2(_review_status(findings=[
            {"household_copy": "Two ranges disagreed.", "at": at},
        ]))
        assert env["findings"][0]["text"] == (
            "From your measurement earlier: Two ranges disagreed."
        )


def test_nothing_banked_renders_nothing_at_all():
    """No heading, no "no findings" line, no empty state. A clean speaker has
    nothing to say and says it — the same honest-silence rule the carve-out
    lines follow for a band that carved nothing."""
    assert build_crossover_envelope_v2(_review_status())["findings"] == []
    assert build_crossover_envelope_v2(_done_status())["findings"] == []


def test_a_row_without_a_readable_sentence_is_never_invented():
    """Rehydration must not fabricate. A durable row whose copy is missing,
    empty, or not a string is DROPPED rather than rendered as a blank line or
    a stringified placeholder — the state file is JSON written by some build,
    possibly not this one, and a finding a household cannot read is not a
    finding."""
    env = build_crossover_envelope_v2(_review_status(findings=[
        {"at": time.time()},
        {"household_copy": "", "at": time.time()},
        {"household_copy": "   ", "at": time.time()},
        {"household_copy": 42, "at": time.time()},
        "not even an object",
        {"household_copy": "A real one survived.", "at": time.time()},
    ]))
    assert [f["text"] for f in env["findings"]] == ["A real one survived."]


def test_two_findings_both_render_in_the_order_they_were_banked():
    """Order is the PRODUCER's, preserved, and nothing is de-duplicated.

    Re-ordering here would make the envelope a second owner of a decision the
    promotion path already made (it sorts carve-outs by band). Dropping a
    repeat would be this screen silently deciding a banked finding does not
    exist — "must not drop a finding" outranks a repeated sentence, and a
    producer emitting one twice is a bug to fix at the producer.
    """
    now = time.time()
    env = build_crossover_envelope_v2(_review_status(findings=[
        {"household_copy": "Second-lowest range first.", "at": now},
        {"household_copy": "Then the higher one.", "at": now},
        {"household_copy": "Second-lowest range first.", "at": now},
    ]))
    assert [f["text"] for f in env["findings"]] == [
        "Second-lowest range first.",
        "Then the higher one.",
        "Second-lowest range first.",
    ]


def test_findings_ride_the_decision_and_result_screens_only():
    """Same data-driven policy as ``prediction``: the SCREEN decides what it
    carries, here, so the renderer keeps one honest rule (draw the lines the
    envelope carries) instead of growing an ``env.screen`` switch.

    DERIVED from the phase→step map rather than hand-listed, so a phase added
    to the vocabulary joins this assertion the moment it exists.
    """
    from jasper.active_speaker.crossover_v2_flow import PHASE_DONE

    others = set(_PHASE_STEP) - {PHASE_REVIEW, PHASE_DONE}
    assert others, "the phase vocabulary must have screens other than these two"
    for phase in sorted(others):
        env = build_crossover_envelope_v2(_status(
            phase=phase, candidate=_candidate_summary(),
            verify={"outcome": "pass"}, findings=_banked_finding(),
        ))
        assert env["findings"] == [], phase


def test_an_aged_resume_stays_one_quiet_line_and_carries_no_finding():
    """#1942's entry screen is a CLEAN START plus one history note, and this
    does not make it two.

    A returning household whose last session banked something is landing on the
    screen that starts a NEW measurement — not on a report — so the finding
    belongs to the review/done screens they can still reach, never here. Pinned
    because the aged branch reads the same status block the report screens do,
    and adding a second history line to this screen would be a one-line
    accident.
    """
    env = build_crossover_envelope_v2(_status(
        failure={"code": REASON_RELAY_TIMEOUT, "at": time.time() - _DAY_S},
        phase="verify", applied=True, findings=_banked_finding(age_s=_DAY_S),
    ))
    assert env["screen"] == "microphone_check"
    assert env["findings"] == []
    assert len(env["nudges"]) == 1
