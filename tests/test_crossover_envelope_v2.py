# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a envelope schema 8: the v2 conductor screen payloads.

Pins docs/historical/crossover-measurement-productization-design.md §5.9 (the five-step
sequence), §5.10 (the four failure-screen templates, parameterized by reason
copy), the §5.2 VERIFY-fail one-default screen, and the volume_recovery screen
keyed on ``needs_recovery`` (the W2 gate ruling — never
``unresolved_volume_safety`` alone).

Owner ruling (2026-07-20): the human ``review_apply`` screen is gone from the
happy path. MEASURE accepted + not yet applied now renders the ``applying``
screen (no candidate review, no action — the conductor's own auto-apply is in
flight); an alignment rejection reaches MEASURE itself
(``delay_implausible``, rendered through the ordinary fix_and_retry template at
the ``measure`` step) instead of nudging a still-available Apply button; and
``done`` is now the RESULT screen — plain outcome first, the
measured numbers in ``candidate_review`` for the wizard's collapsed expert
disclosure, the way back to the previous tuning beside the forward actions.
"""
from __future__ import annotations

import json
import math
import time
from typing import Mapping

import numpy as np
import pytest

from jasper.active_speaker.attempts_loop import (
    FLOOR_SCOPE_ACROSS_SITTINGS,
    PROVENANCE_MODEL_GRADED,
    PROVENANCE_REALIZED,
    AttemptIntegrity,
    AttemptRecord,
    FloorStats,
    decide_next,
)

#: These sentence tests are about the RENDERER, so their pairs share a sitting
#: — otherwise the kernel refuses before a floor or improvement sentence can be
#: reached at all (#2081), and the test would pass for the wrong reason. The
#: cross-sitting sentence has its own test below.
SITTING = "sitting-1"
from jasper.active_speaker.crossover_envelope_v2 import (
    KEEP_FOR_ITERATION_TEXT,
    KEEP_ITERATING_TEXT,
    KEEP_ITERATING_UNGRADED_TEXT,
    KEEP_MISSED_EXHAUSTED_TEXT,
    SERIES_COMPLETE_DEFAULT_TEXT,
    CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
    MIC_CALIBRATION_RESERVATION_COPY,
    RIPPLE_RESERVATION_COPY,
    _PHASE_STEP,
    _per_band_flatness_lines,
    build_crossover_envelope_v2,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_REVIEW,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY,
    REASON_AGC_BEHAVIORAL_FAIL,
    REASON_APPLY_FAILED,
    REASON_CLIPPED,
    REASON_CHANNEL_MAP_MISMATCH,
    REASON_CORRECTION_ROLLBACK_FAILED,
    REASON_LOCATE_FAILED,
    REASON_DELAY_IMPLAUSIBLE,
    REASON_NOISY_ROOM_LINEARITY,
    REASON_CAPTURE_TIMEOUT,
    REASON_SNR_FLOOR,
    REASON_USER_STOPPED,
    REASON_VERIFY_CROSSOVER_REGION,
    REASON_VERIFY_DETERMINISTIC_MISMATCH,
    REASON_VERIFY_INCONCLUSIVE,
    REASON_VERIFY_LEVEL_SHIFT,
    REASON_VERIFY_OUT_OF_TOLERANCE,
    reason_message,
    verify_inconclusive_cause,
    verify_inconclusive_message,
)
from jasper.active_speaker.crossover_v2.attempt_grading import (
    ATTEMPT_REASON_NO_FLOOR,
)
from jasper.active_speaker.crossover_v2.contracts import (
    ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
)
# The one flow-owned symbol this suite still reaches for, and deliberately: the
# predicted-ripple disclosure is a MEASURE-phase threshold whose 20 lines of
# frame-and-provenance rationale belong to whichever wave moves the MEASURE
# region, not to the wave that lifts VERIFY.
from jasper.active_speaker.crossover_v2_flow import (
    MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB,
)
from jasper.active_speaker.crossover_v2.verification import RESULT_VERIFIED_TARGET
from jasper.active_speaker.flat_spec import evaluate_flat_spec, spec_flatness_gauge
from jasper.audio_measurement import gating
from jasper.audio_measurement.gate_disclosure import describe_gate
from jasper.web.correction_crossover_v2 import (
    GRADE_SCOPE_MARK,
    GRADE_SCOPE_SPATIAL,
    GRADE_SPATIAL_FAILED,
    GRADE_SPATIAL_PASSED,
    GRADE_SPATIAL_UNMEASURABLE,
    _post_apply_grade,
)
from jasper.web.correction_crossover_v2_status import _compact_cloud_status

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
    if "post_apply_grade" not in v2:
        # R19: the envelope reads the PRODUCER's grade — scope, spatial state,
        # completeness — instead of re-deriving any of them from the cloud
        # block. Running the real producer here rather than hand-building the
        # dict is what makes these tests a contract between the two modules:
        # the envelope spells the grade words as literals (jasper.active_speaker
        # never imports jasper.web), and a rename on the producer side stops
        # those branches firing, which fails here rather than shipping.
        # Fixtures that pass their own `post_apply_grade` are describing a
        # state file some OTHER build wrote, and keep it verbatim.
        v2 = {**v2, "post_apply_grade": _post_apply_grade(v2)}
    if "updated_at" not in v2:
        # #1947: the durable state now carries the session's own clock, and
        # only a live session renders its phase screen. Every fixture below is
        # describing the screen a household is looking at right now, so the
        # helper stamps it live — which keeps those tests pinning the LIVE path
        # they were written for. The dead and undated cases say so out loud.
        v2 = {**v2, "updated_at": time.time()}
    return {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2,
    }


def _step_statuses(env: dict) -> dict[str, str]:
    return {step["id"]: step["status"] for step in env["steps"]}


def _every_screen_envelope() -> dict[str, dict]:
    """One envelope per screen the builder can serve, keyed by a readable name.

    A sweep rather than a list of the screens a given test remembered: the
    action-shape invariants below are properties of the FLOW, and the next
    action minted with the wrong shape will be on whichever screen the author
    was not thinking about. Built through the real builder from the real
    fixtures, so a screen that stops rendering fails here too.

    The set is asserted non-trivial by its own callers (an empty sweep would
    pass every invariant vacuously); the screen NAMES are not pinned, because
    adding a screen should extend the sweep rather than fail it.
    """
    envelopes = {
        "inactive": build_crossover_envelope_v2({"active": False}),
        "speaker_setup": build_crossover_envelope_v2({
            "active": True,
            "setup": {"active": True, "status": "incomplete"},
            "crossover_v2": {},
        }),
        "review": build_crossover_envelope_v2(_review_status()),
        "review_no_candidate": build_crossover_envelope_v2(
            _review_status(candidate={})
        ),
        "done": build_crossover_envelope_v2(_done_status()),
    }
    for phase in ("check", "measure", "applying", "verify", "done"):
        envelopes[f"phase_{phase}"] = build_crossover_envelope_v2(
            _status(phase=phase)
        )
    return envelopes


# --- shape --------------------------------------------------------------------


def test_schema_8_and_v2_step_tuple():
    env = build_crossover_envelope_v2(_status(phase="check"))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 15
    assert env["flow"] == "v2"
    assert tuple(step["id"] for step in env["steps"]) == V2_STEP_IDS


def test_every_journey_phase_has_a_phase_step_entry():
    """``build_crossover_envelope_v2`` now does a direct ``_PHASE_STEP[phase]``
    lookup (a bare ``.get(phase, "microphone_check")`` used to paper over a
    gap by walking the stepper BACKWARDS to step 1 on the final capture — see
    the table's own comments), so a phase missing from the table raises
    instead of mis-stepping.

    This is the reverse direction from the ``set(_PHASE_STEP)`` tests below
    (search ``others = set(_PHASE_STEP)``): those walk the table's OWN keys
    and assume they are exhaustive. This one walks ``journey``'s ``PHASE_*``
    names — the vocabulary's actual source — and checks the table covers
    every one of them, so a phase added there without a matching entry here
    fails at test time rather than at runtime.
    """
    from jasper.active_speaker.crossover_v2 import journey

    phase_values = {
        value for name, value in vars(journey).items()
        if name.startswith("PHASE_") and isinstance(value, str)
    }
    assert phase_values, "journey should export at least one PHASE_* constant"
    missing = phase_values - set(_PHASE_STEP)
    assert not missing, f"_PHASE_STEP has no entry for: {sorted(missing)}"


def test_legacy_env_still_serves_v2_envelope(monkeypatch):
    """W5b retired the ``JASPER_CROSSOVER_FLOW`` selector and the legacy flow —
    v2 is the only flow now. A box carrying a stale
    ``JASPER_CROSSOVER_FLOW=legacy`` from before the selector was deleted must
    still be served the v2 envelope, not crash or fall back to a deleted legacy
    path. Nothing reads that variable any more, so setting it must be inert.

    This used to run through a ``build_crossover_envelope`` compatibility
    dispatcher. That dispatcher only forwarded to v2 and has been deleted; the
    web flow's entry point is the logged wrapper below, so the contract is
    pinned there instead."""
    from jasper.web.correction_crossover_flow import _build_envelope_logged

    monkeypatch.setenv("JASPER_CROSSOVER_FLOW", "legacy")
    env = _build_envelope_logged(_status(phase="check"))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 15
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
    assert env["next_action"]["href"] == "/sound/setup/"
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


def test_check_phase_states_a_staged_walks_price_before_start(tmp_path, monkeypatch):
    """WP2b (#3498): the session open takes a staged walk whichever tier is
    pressed, so the chooser is the last screen that can say what it costs.

    Both cards carry the same document, each priced against ITS OWN tier: the
    walk belongs to the session, and ``ceiling_min`` is that whole session's
    ceiling, so the card cannot quote 4 min for a 46-minute sitting. An empty
    slot adds nothing at all, and a slot the spool refuses to read costs the
    chooser its offer rather than the screen.
    """
    from jasper.active_speaker import angle_capture as ac
    from jasper.active_speaker import angle_capture_spool as spool
    from jasper.active_speaker import crossover_v2_flow as flow
    from jasper.active_speaker import measurement_programs as mp

    spool.set_angle_request_spool_path_for_tests(tmp_path / "angle_request.json")
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.DEFAULT_SESSION_VOLUME_STATE_PATH",
        tmp_path / "session_volume.json",
    )
    try:
        idle = build_crossover_envelope_v2(_status(phase="check"))
        express = mp.program("baseline", "express")
        spool.stage_angle_request(ac.request_for_program(express))
        offered = build_crossover_envelope_v2(_status(phase="check"))
        # A peek, not a take: the session open is still the only take.
        assert spool.staged_angle_request_pending() is True
        # A field the document cannot coerce refuses in the spool's own
        # vocabulary (``tests/test_angle_capture_take.py``), which this screen
        # already catches — so the chooser renders, minus the offer.
        path = spool.angle_request_spool_path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["delay_us"] = "12us"
        path.write_text(json.dumps(doc), encoding="utf-8")
        unreadable = build_crossover_envelope_v2(_status(phase="check"))
    finally:
        spool.set_angle_request_spool_path_for_tests(None)

    for env in (idle, unreadable):
        for action in [env["next_action"], *env["alternate_actions"]]:
            assert "staged_walk" not in action

    for action in [offered["next_action"], *offered["alternate_actions"]]:
        # Built through the SAME tier → shape resolution the chooser prices
        # with, so this pins that the ceiling follows the tier rather than a
        # figure written down twice.
        ceiling_min = math.ceil(
            flow.wall_clock_ceiling_s(
                flow.stage1_base_entries(
                    flow.resolve_plan_shape(action["body"]["tier"])
                )
                + express.capture_count
            ) / 60
        )
        assert action["staged_walk"] == {
            "program": "baseline/express",
            "mic_moves": express.mic_move_count,
            "captures": express.capture_count,
            "ceiling_min": ceiling_min,
        }
        # The price is on the description too, because that is the only field
        # the page renders (``wrapChoice`` in crossover/js/main.js).
        assert "baseline/express" in action["description"]
        assert str(express.mic_move_count) in action["description"]
        assert str(express.capture_count) in action["description"]
        assert str(ceiling_min) in action["description"]


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


def test_an_implausible_delay_rejects_at_the_measure_step():
    """The alignment rejection that SURVIVED the nanny burn-down, rendering.

    Transformed from the confidence floor's pin: that rung is a disclosure now
    and reaches no screen at all, while the physics backstop still refuses at
    the ``measure`` step through the ordinary fix_and_retry template (never
    ``applying``, since no candidate was ever built).

    The copy assertion inverts with it, and that is the point. It used to
    require the word "microphone", because both rungs shared one sentence; a
    confidently-wrong delay is not a mic-placement problem, so this screen must
    NOT send the household to move one (#2085)."""
    env = build_crossover_envelope_v2(_status(
        phase="measure", failure={"code": REASON_DELAY_IMPLAUSIBLE},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_DELAY_IMPLAUSIBLE].message
    assert "delay" in env["verdict_text"].lower()
    assert "microphone" not in env["verdict_text"].lower()
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
    assert any(n["code"] == "crossover_v2_verified" for n in env["nudges"])


def test_done_renders_the_floor_stop_sentence_from_kernel_output():
    floor = FloorStats.from_repeat_study(
        metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        median_db=0.04,
        p95_db=0.085,
        source="2026-07-31 fixed-mic repeat study",
        measured_at="2026-07-31",
    )
    attempts = [
        AttemptRecord(
            attempt_id="candidate-a",
            metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
            sitting_id=SITTING,
            integrity=AttemptIntegrity(comparable=True),
            grade_db=1.0,
        ),
        AttemptRecord(
            attempt_id="candidate-b",
            metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            provenance=PROVENANCE_REALIZED,
            sitting_id=SITTING,
            integrity=AttemptIntegrity(comparable=True),
            grade_db=0.91,
        ),
    ]
    decision = decide_next(attempts, floor)
    assert decision.decision == "stop_floor"

    env = build_crossover_envelope_v2(_done_status(
        attempts_loop={"last_decision": decision.to_dict(), "store_count": 2},
    ))

    assert env["verdict_text"].endswith(
        "Stopped: the change in prediction tracking from the previous attempt "
        "(0.09 dB) is below what this instrument can distinguish (floor 0.17 dB)."
    )


def test_done_describes_tracking_fidelity_without_claiming_crossover_quality():
    floor = FloorStats.from_repeat_study(
        metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        median_db=0.04,
        p95_db=0.1,
        source="test fixed-mic repeat study",
        measured_at="2026-08-03",
    )
    decision = decide_next(
        [
            AttemptRecord(
                attempt_id="candidate-a",
                metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
                provenance=PROVENANCE_REALIZED,
                sitting_id=SITTING,
                integrity=AttemptIntegrity(comparable=True),
                grade_db=1.0,
            ),
            AttemptRecord(
                attempt_id="candidate-b",
                metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
                provenance=PROVENANCE_REALIZED,
                sitting_id=SITTING,
                integrity=AttemptIntegrity(comparable=True),
                grade_db=0.6,
            ),
        ],
        floor,
    )

    env = build_crossover_envelope_v2(_done_status(
        attempts_loop={"last_decision": decision.to_dict()},
    ))

    assert env["verdict_text"].endswith(
        "The latest applied result tracked its prediction 0.4 dB more closely "
        "(realized vs realized)."
    )
    assert "Improved the crossover" not in env["verdict_text"]


def test_done_explains_a_cross_sitting_refusal_without_engine_vocabulary():
    """#2081's household sentence, rendered from the kernel's own refusal.

    The unlicensed alternative is one line above in this file: with a shared
    sitting the same two grades render "tracked its prediction 0.4 dB more
    closely". Here the phone moved, so the screen says that instead of a number
    it cannot support — and says it in household terms, with "floor", "scope"
    and "sitting" left in the decision's notes where support can read them.
    """
    floor = FloorStats.from_repeat_study(
        metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        median_db=0.04,
        p95_db=0.1,
        source="test fixed-mic repeat study",
        measured_at="2026-08-03",
    )
    decision = decide_next(
        [
            AttemptRecord(
                attempt_id="candidate-a",
                metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
                provenance=PROVENANCE_REALIZED,
                sitting_id="first-tune",
                integrity=AttemptIntegrity(comparable=True),
                grade_db=1.0,
            ),
            AttemptRecord(
                attempt_id="candidate-b",
                metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
                provenance=PROVENANCE_REALIZED,
                sitting_id="second-tune",
                integrity=AttemptIntegrity(comparable=True),
                grade_db=0.6,
            ),
        ],
        floor,
    )

    env = build_crossover_envelope_v2(_done_status(
        attempts_loop={"last_decision": decision.to_dict()},
    ))

    assert env["verdict_text"].endswith(
        "The previous result was measured with the microphone in a different "
        "position, so this attempt is recorded without comparing the two."
    )
    # The claim the same numbers would have produced within one sitting.
    assert "0.4 dB more closely" not in env["verdict_text"]
    for engine_word in ("floor", "scope", "sitting", "provenance"):
        assert engine_word not in env["verdict_text"].lower()


def test_done_does_not_blame_the_mic_when_the_sitting_is_merely_unrecorded():
    """An upgraded speaker knows nothing about where its old attempt was
    measured, and must not tell the household their microphone moved (#2081)."""
    env = build_crossover_envelope_v2(_done_status(
        attempts_loop={"last_decision": {
            "decision": "stop_evidence",
            "reason": "sitting_unrecorded",
            "basis_attempt_ids": ["candidate-a", "candidate-b"],
        }},
    ))
    assert env["verdict_text"].endswith(
        "Stopped because the latest attempt could not be compared reliably."
    )
    assert "different position" not in env["verdict_text"]


def test_first_attempt_sentence_formats_the_kernel_provenance():
    metric = "linearization_residual_rms_db"
    floor = FloorStats.from_policy_bar(
        metric=metric,
        claim_floor_db=0.5,
        source="test policy bar",
        scope=FLOOR_SCOPE_ACROSS_SITTINGS,
    )
    decision = decide_next(
        [AttemptRecord(
            attempt_id="candidate-a",
            metric=metric,
            provenance=PROVENANCE_MODEL_GRADED,
            integrity=AttemptIntegrity(comparable=True),
            grade_db=0.9,
        )],
        floor,
    )

    env = build_crossover_envelope_v2(_done_status(
        attempts_loop={"last_decision": decision.to_dict()},
    ))

    assert env["verdict_text"].endswith(
        "Recorded the first model-graded tracking result; another attempt is "
        "needed before improvement can be judged."
    )


def test_done_makes_no_claim_when_the_store_has_no_floor():
    env = build_crossover_envelope_v2(_done_status(
        attempts_loop={
            "last_decision": {
                "decision": None,
                "reason": ATTEMPT_REASON_NO_FLOOR,
                "provenance": PROVENANCE_REALIZED,
                "floor": None,
            },
            "store_count": 1,
        },
    ))

    assert env["verdict_text"].endswith(
        "No improvement claim was made because this speaker has no adopted "
        "measurement floor."
    )


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


def _shipped_claims(integration: str = "pass", absolute: str = "pass") -> dict:
    """The §7 claim record a post-R18 VERIFY writes. SYNTHETIC numbers.

    ``crossover_v2_flow._verify_claims`` ALWAYS returns an ``integration``
    entry, so ``_post_apply_grade``'s ``result_evidence`` is true — and its
    terminal result code therefore reaches the done screen — on every shipped
    session whose VERIFY produced a tracking analysis. A VERIFY that refuses
    before that record is written (a gate refusal, the pilot-transfer
    level-shift arm) writes no claims, and neither does a PRE-CLAIMS durable
    state; on both, no result code exists to qualify the screen's copy. The
    tests below that rely on that say so out loud (#2738: four of them relied
    on it silently, so none of them exercised the graded shape).
    """
    branch = {"status": "not_evaluated", "reason": "no_per_branch_capture"}
    return {
        "woofer_branch": dict(branch),
        "hf_branch": dict(branch),
        "integration": {
            "status": integration, "max_db": 0.61, "tolerance_db": 1.5,
            "band_hz": [1200.0, 2400.0],
        },
        "absolute": {
            "status": absolute, "max_db": 1.42, "rms_db": 0.71,
            "worst_db": 1.42, "worst_hz": 1780.0, "tolerance_db": 2.5,
            "band_hz": [1200.0, 2400.0],
        },
    }


def test_done_headline_states_an_out_of_spec_result_in_primary_copy():
    """PR-L4 item 7: the spec verdict gets a vote where it cannot be collapsed.

    The headline and the "Verified." badge both read the TRACKING comparator,
    which asks whether the speaker matched its own prediction — not whether it
    is flat. On 2026-07-27 the one instrument that compares the result to flat
    failed all three bands and reached only a line inside a collapsed
    disclosure, so a household read "Your speaker is tuned" over it.

    On the SHIPPED shape since #2738: the claims block gives this session a
    terminal result code, and a failed spatial grade caps it — so the vote
    below is the one this test was written to pin rather than one a fixture
    without claims happened to leave unopposed."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "claims": _shipped_claims()},
        candidate=_candidate_summary(), applied=True,
        cloud=_cloud_verify_spec(False),
    ))
    verdict = env["verdict_text"].lower()
    assert "further from flat" in verdict
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
        candidate=_candidate_summary(), applied=True,
        cloud=_cloud_verify_spec(True),
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


def test_done_headline_will_not_call_an_unmeasurable_group_a_miss():
    """#2160: ``overall_passed`` is ``False`` for a spectrum no band survived
    to grade as well as for one that was graded and missed, so the screen used
    to tell a household its speaker "measures further from flat than the
    target" on the strength of a measurement that never produced a number.
    The two states now get two sentences.

    **UNMEASURABLE is not FAILED, so #2738's cap must not reach it** — that is
    the second arm below, and it is what keeps the cap from turning "could not
    be graded" into a miss by the back door. The sentence itself is pinned on a
    PRE-CLAIMS durable state (no claims block, so no terminal result code
    qualifies the copy): on a shipped post-R18 session the result code's copy
    replaces it, which is #2605's override behaving as specified for an absent
    spatial verdict, not a regression of this branch."""
    unmeasurable = {PHASE_CLOUD_VERIFY: {
        **_cloud_verify_spec(False)[PHASE_CLOUD_VERIFY],
        "flatness": {
            "max_db": None, "max_hz": None, "max_band_hz": None,
            "tolerance_db": None, "rms_db": None, "n_bins": 0,
            "n_excluded": 900, "evaluable": False, "passed": False,
        },
    }}
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, applied=True,
        candidate=_candidate_summary(), cloud=unmeasurable,
    ))
    verdict = env["verdict_text"].lower()
    assert "could not read enough of the sound" in verdict
    assert "further from flat" not in verdict
    # And the badge stops claiming a miss it cannot support either.
    assert "crossover_v2_out_of_spec" not in {n["code"] for n in env["nudges"]}
    # The shipped shape: a result code is present and the cap leaves it alone,
    # because no spatial verdict failed.
    shipped = build_crossover_envelope_v2(_status(
        phase="done", applied=True, candidate=_candidate_summary(),
        verify={"outcome": "pass", "claims": _shipped_claims()},
        cloud=unmeasurable,
    ))
    assert "further from flat" not in shipped["verdict_text"].lower()
    assert {n["code"] for n in shipped["nudges"]} == {
        "crossover_v2_verified_target",
    }


def test_done_headline_says_a_full_session_never_closed_its_wider_check():
    """#2098: a Full session that verified at the mark and never closed its
    post-apply group read as an unqualified "Your speaker is tuned" — the
    widest of the three claims on the narrowest evidence. The local pass is
    still stated; what is added is the part that is unproven.

    A PRE-CLAIMS durable state, deliberately: with no claims block there is no
    terminal result code, so this sentence is the screen's answer. On a shipped
    post-R18 session the result copy replaces it — the spatial verdict is
    ABSENT here, not failed, so #2738's cap does not reach it and #2605's
    override stands as specified. Named rather than left implicit (#2738: this
    test read as shipped-shape coverage and was not)."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="full", verify={"outcome": "pass"}, applied=True,
        candidate=_candidate_summary(),
    ))
    verdict = env["verdict_text"].lower()
    assert "confirmed at the mark" in verdict
    # SF1 (#2242 gate): delivered-evidence wording only — never "never
    # finished", which asserts a mechanism this branch cannot know (a closed
    # group whose pipeline failed reaches the same branch and DID close).
    assert "has not produced a result" in verdict
    assert "never finished" not in verdict
    assert "unproven" in verdict


def test_done_headline_leaves_a_complete_express_result_alone():
    """Express's scope IS the mark, so the incomplete branch must not fire —
    its own copy already names both the scope and the upgrade path."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="express", verify={"outcome": "pass"}, applied=True,
        candidate=_candidate_summary(),
    ))
    assert "unproven" not in env["verdict_text"].lower()
    assert "Run a Full measurement" in env["verdict_text"]


def test_the_done_screen_spells_the_producers_grade_words():
    """The envelope branches on grade words as LITERALS — ``jasper.active_speaker``
    never imports ``jasper.web``, so it cannot spell them through the
    constants. This is the pin that keeps the two spellings identical: each
    branch is driven from the producer's own constant, so a rename there
    leaves the branch unreachable and fails here."""
    def _verdict(grade):
        return build_crossover_envelope_v2(_status(
            phase="done", tier="full", verify={"outcome": "pass"}, applied=True,
            candidate=_candidate_summary(), post_apply_grade=grade,
        ))["verdict_text"].lower()

    failed = _verdict({
        "state": "graded", "graded": True, "complete": True,
        "scope": GRADE_SCOPE_SPATIAL, "spatial": GRADE_SPATIAL_FAILED,
    })
    assert "further from flat" in failed

    unmeasurable = _verdict({
        "state": "graded", "graded": True, "complete": False,
        "scope": GRADE_SCOPE_MARK, "spatial": GRADE_SPATIAL_UNMEASURABLE,
    })
    assert "could not read enough of the sound" in unmeasurable

    passed = _verdict({
        "state": "graded", "graded": True, "complete": True,
        "scope": GRADE_SCOPE_SPATIAL, "spatial": GRADE_SPATIAL_PASSED,
    })
    assert "further from flat" not in passed
    assert "unproven" not in passed


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


def test_a_safety_only_probe_caveats_the_pass_screen():
    """#2614: "the shape check did not run" must reach the household.

    When the change axis is unavailable the probe grades the model's departure
    and nothing about the speaker. That is not a finding about the speaker and
    not a rollback — but every other word on this screen says "Verified", and a
    household reading it would take the checks to have passed. The caveat rides
    BESIDE the badge for ``level_mismatch``'s reason: the tracking comparator
    really did pass.

    **It names both halves since series-2 D1.** The pre-D1 copy said the
    loudness half had run; on this path there is no pre-apply capture to
    difference against, so what it called a loudness comparison was a comparison
    against the model — the exact confusion D1 exists to end, on the surface a
    household reads.

    **The copy names no cause, and that is asserted.** Four paths reach this
    verdict and only one of them is "the crossover point moved", so a cause
    clause is false on the screen three times out of four while the journal
    carries the true reason every time.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "pass",
            # ``overshoot_db`` is ``None`` because since series-2 D1 this path
            # measures no directional finding at all — there is no change
            # reference on a state axis to difference against.
            #
            # ``safety_anchored`` IS on the durable record (``_delta_probe_summary``
            # carries it), so the fixture carries it too: this is the shape the
            # renderer will actually be handed. Nothing in the envelope reads it
            # yet — the caveat below keys on ``verdict`` — and it is here so the
            # fixture cannot quietly drift from the record it stands in for.
            "delta_probe": {
                "verdict": "safety_only",
                "reason": "commanded_axis_unavailable",
                "safety_anchored": False,
                "boost": {"over_declared_bound": False, "overshoot_db": None},
            },
        },
        candidate=_candidate_summary(),
    ))
    codes = {n["code"] for n in env["nudges"]}
    assert codes == {"crossover_v2_verified", "crossover_v2_safety_only"}
    caveat = next(
        n for n in env["nudges"] if n["code"] == "crossover_v2_safety_only"
    )
    assert caveat["severity"] == "warn"
    assert "could not confirm the correction's shape or its loudness" in (
        caveat["text"]
    )
    # No cause clause: the reason lives on the journal, where it is specific.
    assert "crossover point" not in caveat["text"]
    assert "because" not in caveat["text"].lower()
    # The same copy rule the other two caveats carry: no hardware noun, and no
    # instruction to act.
    assert not any(
        word in caveat["text"].lower()
        for word in ("tweeter", "woofer", "amplifier", "horn")
    )


def test_a_whole_band_level_shift_keeps_the_overall_loudness_sentence():
    """The reason-aware split's control side (#2537).

    ``uncommanded_level_shift`` means the quiet bins measured the level across
    the whole graded band, so "the overall loudness" is exactly what was
    measured and the sentence is unchanged from before the split existed.
    """
    caveat = _level_caveat({
        "verdict": "level_mismatch",
        "reason": "uncommanded_level_shift",
        "residual_offset_db": -4.0,
        "quiet": {"core_band_hz": [12_000.0, 20_000.0]},
    })
    assert "overall loudness" in caveat["text"]
    assert "kHz" not in caveat["text"]


def test_a_band_scoped_level_shift_names_the_band_it_actually_covered():
    """The deferred #2545 nit, and the sentence that was false (#2533/#2537).

    A level measured entirely above 12 kHz was being reported as "the overall
    loudness changed" — the 2026-08-15 JTS3 round's own shape, where 158 of 160
    quiet bins sat above 12 kHz. #2533 narrowed the REASON and gave the map the
    band; this is the screen catching up, so the household reads a true
    sentence rather than a true-sounding one.
    """
    caveat = _level_caveat({
        "verdict": "level_mismatch",
        "reason": "uncommanded_level_shift_outside_probe_band",
        "residual_offset_db": -1.457,
        "quiet": {"core_band_hz": [12_400.0, 19_800.0]},
    })
    assert "overall loudness" not in caveat["text"]
    assert "12.4 kHz" in caveat["text"]
    assert "19.8 kHz" in caveat["text"]
    assert "could not confirm" in caveat["text"]
    assert not any(
        word in caveat["text"].lower()
        for word in ("tweeter", "woofer", "amplifier", "horn")
    )


def test_a_band_scoped_reason_with_no_band_falls_back_rather_than_inventing_one():
    """A caveat that says slightly more than the evidence supports still tells
    the household the shape went unconfirmed; a band invented from an absent
    field would state a fact."""
    for quiet in ({}, {"core_band_hz": None}, {"core_band_hz": [480.0]}):
        caveat = _level_caveat({
            "verdict": "level_mismatch",
            "reason": "uncommanded_level_shift_outside_probe_band",
            "quiet": quiet,
        })
        assert "overall loudness" in caveat["text"]


def test_a_sub_kilohertz_band_reads_in_hertz():
    caveat = _level_caveat({
        "verdict": "level_mismatch",
        "reason": "uncommanded_level_shift_outside_probe_band",
        "quiet": {"core_band_hz": [331.8, 812.4]},
    })
    assert "332 Hz" in caveat["text"]
    assert "812 Hz" in caveat["text"]


def _level_caveat(delta_probe):
    """The level-mismatch nudge off a passing done screen."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "delta_probe": delta_probe},
        candidate=_candidate_summary(),
    ))
    return next(
        n for n in env["nudges"] if n["code"] == "crossover_v2_level_mismatch"
    )


def test_a_kept_for_iteration_round_says_so_rather_than_only_verified():
    """#2537's household copy for the row that keeps an imperfect result.

    ``keep_for_iteration`` leaves the speaker in the same state ``keep`` does,
    so it must not read as a failure — but the round measured something it did
    not fix, and saying nothing is how "we could not tell" becomes "verified"
    by silence.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        round_receipt={
            "round_id": "s1",
            "adoption": "keep_for_iteration",
            # #2602 keys this caveat on the ROW, because row 2 and row 6 now
            # share the ``keep_for_iteration`` outcome and carry opposite news.
            # The coordinator has always stamped a row beside the outcome.
            "row": "row2_trusted_safe_missed",
        },
    ))
    codes = {n["code"] for n in env["nudges"]}
    assert "crossover_v2_keep_for_iteration" in codes
    nudge = next(
        n for n in env["nudges"] if n["code"] == "crossover_v2_keep_for_iteration"
    )
    assert nudge["severity"] == "warn"
    assert "best sound measured so far" in nudge["text"]
    assert "measuring again" in nudge["text"]
    # No hardware noun and no instruction to press anything — the same copy
    # rule every other caveat on this screen carries.
    assert not any(
        word in nudge["text"].lower()
        for word in ("tweeter", "woofer", "amplifier", "horn", "button")
    )


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {},
        {"round_id": "s1"},
        {"round_id": "s1", "adoption": "keep"},
        # #2602's row 6: also ``keep_for_iteration``, and it must NOT get row
        # 2's copy — an in-tolerance speaker told "some of what was measured is
        # still off target" would be told something false. This is the case the
        # row-keyed dispatch exists for.
        {
            "round_id": "s1",
            "adoption": "keep_for_iteration",
            "row": "row6_trusted_safe_passed_reachable",
            "reason": "flatter_result_reachable",
        },
    ],
    ids=["absent", "empty", "no_adoption", "plain_keep", "passed_but_iterating"],
)
def test_only_a_kept_for_iteration_round_gets_that_caveat(receipt):
    """That caveat belongs to the two MISSED rows and nothing else.

    A round that restored or escalated never reaches this screen at all; a
    round with no row cannot be told apart from one that never graded; and
    since #2602 a round that PASSED gets its own sentence rather than this one.
    Row 7 is the second row that DOES get it — it also measured something it
    did not fix — and it gets its own sentence under the same code.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        round_receipt=receipt,
    ))
    assert "crossover_v2_keep_for_iteration" not in {
        n["code"] for n in env["nudges"]
    }


def _round_done_env(**receipt):
    return build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        round_receipt={"round_id": "s1", **receipt},
    ))


def _nudge(env, code):
    return next(n for n in env["nudges"] if n["code"] == code)


def test_a_passing_round_that_is_still_iterating_says_so_on_the_screen():
    """#2602's household sentence: in tolerance, and another round is coming.

    The failure this replaces is silence dressed as completion — before the
    ruling, a round with 2.37 dB of tilt left said "verified" and stopped, and
    the household had no way to know more was available.
    """

    env = _round_done_env(
        adoption="keep_for_iteration",
        row="row6_trusted_safe_passed_reachable",
        reason="flatter_result_reachable",
    )
    nudge = _nudge(env, "crossover_v2_keep_iterating")

    assert nudge["severity"] == "info"
    assert nudge["text"] == KEEP_ITERATING_TEXT
    # Both halves have to be said: the pass, and the plan.
    assert "inside the target" in nudge["text"]
    assert "measuring again" in nudge["text"]
    # It must NOT read as a fault — row 2's sentence would be false here.
    assert "still off target" not in nudge["text"]
    assert nudge["text"] != KEEP_FOR_ITERATION_TEXT


@pytest.mark.parametrize(
    ("reason", "phrase"),
    [
        ("objectives_within_plateau", "as flat and as level as measuring can show"),
        ("improvement_plateaued", "barely moved it"),
        ("round_cap_reached", "the last round"),
        ("objectives_unevaluable", "not enough of a full result"),
    ],
)
def test_a_series_that_ended_says_which_ending_it_was(reason, phrase):
    """"Flat enough", "it stopped moving", and "that was the third" are three
    different sentences, and a household told last round to measure again is
    owed the specific one."""

    env = _round_done_env(
        adoption="keep", row="row1_trusted_safe_passed", reason=reason
    )
    nudge = _nudge(env, "crossover_v2_series_complete")

    assert nudge["severity"] == "ok"
    assert phrase in nudge["text"]
    assert "inside the target" in nudge["text"]


def test_an_iterating_round_with_nothing_gradable_does_not_claim_the_target():
    """The bites ruling made this row reachable with NO measured flatness.

    ``objectives_unevaluable`` used to end the series, so row 6 always carried
    a graded post-apply cloud behind it and ``KEEP_ITERATING_TEXT``'s opening
    ("Everything measured is inside the target") was always true. Since
    missing evidence stopped ending a series, an Express round — which walks no
    post-apply cloud at all — lands here, and that sentence would be a claim
    nothing supports.
    """

    env = _round_done_env(
        adoption="keep_for_iteration",
        row="row6_trusted_safe_passed_reachable",
        reason="objectives_unevaluable",
    )
    nudge = _nudge(env, "crossover_v2_keep_iterating")

    assert nudge["text"] == KEEP_ITERATING_UNGRADED_TEXT
    assert "inside the target" not in nudge["text"]
    assert "not enough of a full result" in nudge["text"]
    # Still the good half: the speaker is playing the best measured tune.
    assert "best sound measured so far" in nudge["text"]


@pytest.mark.parametrize(
    "row",
    ["row2_trusted_safe_missed", "row6_trusted_safe_passed_reachable"],
    ids=["missed", "passed_but_reachable"],
)
def test_a_round_that_promises_another_bite_offers_the_button_to_take_it(row):
    """Copy that names an action the screen does not carry is #2641's shape.

    Both iterating rows tell the household "measuring again is how that gets
    closer", and this screen's only other exit was Room correction — so the
    sentence pointed at nothing. The action is the same re-measure the review
    screen mints, not a second way in — and on an iterating round it leads
    the screen: the head of the recommendedness-ordered list is promoted to
    the primary.
    """

    env = _round_done_env(adoption="keep_for_iteration", row=row, reason="r")
    action = env["next_action"]

    assert action["id"] == "round_remeasure"
    assert action["label"] == "Try again with what we learned"
    assert action["endpoint"] == "/correction/crossover/v2/session"
    # An EMPTY body: the tier is the lapsed session's, resolved server-side.
    # A literal here would be the #2639 demotion with extra steps.
    assert action["body"] == {}


@pytest.mark.parametrize(
    "row",
    ["row1_trusted_safe_passed", "", "row_from_the_future"],
    ids=["series_complete", "no_row", "unknown_row"],
)
def test_a_round_that_ended_the_series_offers_no_re_measure(row):
    """The other half, and the one that makes the button mean something.

    A terminal round, a receipt with no row, and a row this build does not
    know all get the same answer: no re-measure. Offering one on a finished
    series would invite a household to spend a round the ruling says is over.
    """

    env = _round_done_env(adoption="keep", row=row, reason="r")

    assert "round_remeasure" not in {
        a["id"] for a in env["alternate_actions"]
    }


def test_an_unknown_ending_still_says_the_tuning_is_finished():
    """A reason this build does not know is not a reason to say nothing.

    The fallback states only what the ROW already proves — the series is over
    — and never guesses at a cause it cannot name.
    """

    env = _round_done_env(
        adoption="keep", row="row1_trusted_safe_passed", reason="reason_from_the_future"
    )
    nudge = _nudge(env, "crossover_v2_series_complete")

    assert nudge["text"] == SERIES_COMPLETE_DEFAULT_TEXT
    assert "finished" in nudge["text"]


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ("row1_trusted_safe_passed", "crossover_v2_series_complete"),
        ("row6_trusted_safe_passed_reachable", "crossover_v2_keep_iterating"),
        ("row2_trusted_safe_missed", "crossover_v2_keep_for_iteration"),
        ("row7_trusted_safe_missed_exhausted", "crossover_v2_keep_for_iteration"),
    ],
)
def test_every_round_copy_keeps_the_screens_register(row, code):
    """No hardware noun, no instruction to press anything — the same copy rule
    every other caveat on this screen carries."""

    env = _round_done_env(adoption="keep", row=row, reason="objectives_within_plateau")
    text = _nudge(env, code)["text"].lower()

    assert not any(
        word in text
        for word in ("tweeter", "woofer", "amplifier", "horn", "button", "click")
    )


def test_exactly_one_round_sentence_is_ever_owed():
    """The four rows are alternatives, not a stack.

    A screen that rendered two of them would be telling a household both that
    the tuning is finished and that another round is coming.
    """

    for row in (
        "row1_trusted_safe_passed",
        "row6_trusted_safe_passed_reachable",
        "row2_trusted_safe_missed",
        "row7_trusted_safe_missed_exhausted",
    ):
        env = _round_done_env(
            adoption="keep", row=row, reason="objectives_within_plateau"
        )
        codes = [n["code"] for n in env["nudges"] if n["code"].startswith((
            "crossover_v2_series_complete",
            "crossover_v2_keep_iterating",
            "crossover_v2_keep_for_iteration",
        ))]
        assert len(codes) == 1, f"{row} owes exactly one round sentence, got {codes}"


def test_a_restoring_row_gets_no_round_sentence_at_all():
    """Those never reach the done screen; their copy is the failure registry's."""

    for row in (
        "row3_unsafe", "row4_untrusted_evidence",
        "row5_trusted_safe_regressed", "row0_restore_failed",
    ):
        env = _round_done_env(adoption="restore", row=row, reason="whatever")
        assert not [
            n for n in env["nudges"]
            if n["code"] in {
                "crossover_v2_series_complete",
                "crossover_v2_keep_iterating",
                "crossover_v2_keep_for_iteration",
            }
        ], row


def test_no_round_copy_spells_the_cap_out_as_a_number():
    """``ROUND_SERIES_CAP`` has one owner, and the screen is not it.

    Copy reading "that was the third round" would be a second source of truth
    for the constant — and a change to the cap would turn it into a lie on a
    household's screen with nothing red. The sentence says "the last round"
    instead, which stays true at any cap.
    """
    from jasper.active_speaker.crossover_envelope_v2 import _series_complete_text

    sentences = [
        KEEP_ITERATING_TEXT,
        KEEP_FOR_ITERATION_TEXT,
        KEEP_MISSED_EXHAUSTED_TEXT,
        SERIES_COMPLETE_DEFAULT_TEXT,
        *(
            _series_complete_text(reason)
            for reason in (
                "objectives_within_plateau", "improvement_plateaued",
                "round_cap_reached", "objectives_unevaluable",
            )
        ),
    ]
    for text in sentences:
        lowered = text.lower()
        assert not any(
            word in lowered
            for word in ("three", "third", "3 rounds", "3 more")
        ), text


def test_the_envelope_names_which_adoption_row_the_round_fired():
    """The machine half of #2537's disclosure, for a driver chaining rounds.

    Three of the seven rows restore and four keep the graph, sharing two
    outcomes between them, so ``adoption`` alone cannot say which rule applied,
    and the reason travels from whichever axis decided. The ROW is the stable
    thing to branch on.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        round_receipt={
            "round_id": "s1",
            "adoption": "keep_for_iteration",
            "row": "row2_trusted_safe_missed",
            "reason": "measured_targets_outstanding",
        },
    ))
    assert env["round"] == {
        "row": "row2_trusted_safe_missed",
        "adoption": "keep_for_iteration",
        "reason": "measured_targets_outstanding",
    }


@pytest.mark.parametrize(
    "receipt", [None, {}, {"round_id": "s1"}], ids=["absent", "empty", "id_only"]
)
def test_a_session_that_graded_no_round_reports_an_absence_not_an_empty_row(
    receipt,
):
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        round_receipt=receipt,
    ))
    assert env["round"] is None


def test_the_round_key_is_present_on_every_screen():
    """Always-present, so a driver never has to tell "no round yet" from "this
    build predates the key"."""
    for phase in ("check", "measure", "applying", "verify", "done"):
        env = build_crossover_envelope_v2(_status(phase=phase))
        assert "round" in env
    inactive = build_crossover_envelope_v2({"active": False})
    assert inactive["round"] is None


def test_a_level_mismatch_rides_beside_an_out_of_spec_badge_too():
    """Two instruments, two claims, neither silencing the other.

    On the SHIPPED shape since #2738: the claims block gives this session a
    terminal result code, which used to return before the caveat was appended
    at all — so a household with a level mismatch under a failed spatial grade
    saw one green badge and no caveat."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "pass",
            "delta_probe": {"verdict": "level_mismatch"},
            "claims": _shipped_claims(),
        },
        candidate=_candidate_summary(), applied=True,
        cloud=_cloud_verify_spec(False),
    ))
    assert {n["code"] for n in env["nudges"]} == {
        "crossover_v2_out_of_spec", "crossover_v2_level_mismatch",
    }


def test_a_failed_spatial_grade_caps_the_result_badge_and_the_primary_copy():
    """#2738 (ruled 2026-08-19), reproduced from the issue's own probe.

    ``absolute`` passed at the mark, so the producer grades this session
    ``verified_target`` — and its post-apply group closed FAILED at −4.63 dB
    near 1650 Hz. Before the cap the household read a green "Target verified."
    over copy saying the result reached the target, with the −4.63 dB reachable
    only inside the collapsed expert disclosure. The twin of #2464's cap, one
    surface over: the badge and the primary copy may not claim verified over a
    failed spatial grade, and the caveat still rides beside whichever badge
    won.

    The PRODUCER's fields are untouched — ``/state`` still reports the result
    code and the grade — because this is a cap on what the household SCREEN may
    claim, not a second opinion about what was measured.
    """
    status = _status(
        phase="done", tier="full", applied=True,
        candidate=_candidate_summary(),
        verify={
            "outcome": "pass",
            "delta_probe": {"verdict": "level_mismatch"},
            "claims": _shipped_claims(),
        },
        cloud={PHASE_CLOUD_VERIFY: {
            **_cloud_verify_spec(False)[PHASE_CLOUD_VERIFY],
            "flatness": {
                "max_db": -4.628, "max_hz": 1650.0,
                "max_band_hz": [1000.0, 2000.0], "tolerance_db": 2.5,
                "rms_db": 2.1, "n_bins": 900, "n_excluded": 0,
                "evaluable": True, "passed": False,
            },
        }},
    )
    grade = status["crossover_v2"]["post_apply_grade"]
    assert grade["outcome"] == RESULT_VERIFIED_TARGET
    assert grade["spatial"] == GRADE_SPATIAL_FAILED
    assert grade["spatial_worst_db"] == -4.628

    env = build_crossover_envelope_v2(status)
    assert {n["code"] for n in env["nudges"]} == {
        "crossover_v2_out_of_spec", "crossover_v2_level_mismatch",
    }
    assert all(n["severity"] == "warn" for n in env["nudges"])
    verdict = env["verdict_text"].lower()
    assert "further from flat" in verdict
    assert "reached the target" not in verdict
    # The numbers still ride the disclosure — the claim moved, not the data.
    assert any("1650 Hz" in line for line in env["expert_details"])


def test_a_probe_caveat_rides_beside_a_result_badge():
    """The composer's own rule — appended beside whichever badge won, never
    replacing it — applied to the badge a result code earns (#2738).

    The spatial grade PASSED here, so nothing is capped and the result badge
    is the honest one. The caveat is still owed: the probe never answered the
    shape question, and every other word on the screen says the target was
    reached. Returning early for the result code dropped it on every graded
    session."""
    env = build_crossover_envelope_v2(_status(
        phase="done", tier="full", applied=True,
        candidate=_candidate_summary(),
        verify={
            "outcome": "pass",
            "delta_probe": {"verdict": "level_mismatch"},
            "claims": _shipped_claims(),
        },
        cloud=_cloud_verify_spec(True),
    ))
    assert {n["code"] for n in env["nudges"]} == {
        "crossover_v2_verified_target", "crossover_v2_level_mismatch",
    }
    assert "reached the target" in env["verdict_text"].lower()


def test_the_cap_leaves_the_warn_result_badges_and_their_copy_alone():
    """#2738 caps the claim "verified", not every result code.

    ``keep_previous`` and ``inconclusive`` already refuse that claim, and
    ``keep_previous``'s "should not replace the previous sound" is the more
    urgent of the two sentences — swapping either for the out-of-spec badge
    would assert a prediction match one of them explicitly denies, and would
    undo the honest routing #2605 shipped. Both ride a FAILED spatial grade, the
    cell the cap fires on for ``verified_target``.

    ``keep_previous`` also pins the non-pass path: a result code renders its
    badge on a screen whose VERIFY did not pass, where the composer would
    otherwise return no badge at all."""
    def _done(**verify):
        return build_crossover_envelope_v2(_status(
            phase="done", tier="full", applied=True,
            candidate=_candidate_summary(), verify=verify,
            cloud=_cloud_verify_spec(False),
        ))

    inconclusive = _done(
        outcome="pass", claims=_shipped_claims(absolute="not_evaluated"),
    )
    assert {n["code"] for n in inconclusive["nudges"]} == {
        "crossover_v2_inconclusive",
    }
    assert "not enough complete evidence" in inconclusive["verdict_text"]

    keep_previous = _done(outcome="fail", claims=_shipped_claims(
        integration="fail", absolute="fail",
    ))
    assert {n["code"] for n in keep_previous["nudges"]} == {
        "crossover_v2_keep_previous",
    }
    assert "should not replace the previous sound" in (
        keep_previous["verdict_text"]
    )


def test_a_frame_mismatch_caveats_the_pass_screen():
    """#2521: the tilt-carrying sibling of the caveat above, on the same screen.

    ``frame_mismatch`` is deliberately NOT a rollback — a level offset plus a
    broadband tilt between an in-room capture and an on-axis model is a
    property of the comparison, not a claim about the correction — but it means
    the probe never answered the shape question, while every other word on this
    screen says "Verified." A demotion that rendered as a clean pass would be
    the silence the whole ruling was written against.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "pass",
            "delta_probe": {
                "verdict": "frame_mismatch",
                "reason": "uncommanded_frame_shift",
                "frame": {"offset_db": -2.39, "tilt_db_per_octave": -0.916},
            },
        },
        candidate=_candidate_summary(),
    ))
    codes = {n["code"] for n in env["nudges"]}
    assert codes == {"crossover_v2_verified", "crossover_v2_frame_mismatch"}
    caveat = next(
        n for n in env["nudges"] if n["code"] == "crossover_v2_frame_mismatch"
    )
    assert caveat["severity"] == "warn"
    assert "could not confirm" in caveat["text"]
    # No hardware noun — the same copy rule the refusal reasons carry.
    assert not any(
        word in caveat["text"].lower()
        for word in ("tweeter", "woofer", "amplifier", "horn")
    )


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
    to reach it is a household closing the phone after the apply)."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={}, candidate=_candidate_summary(),
        post_apply_grade={"state": "unverified", "graded": False},
    ))
    verdict = env["verdict_text"].lower()
    assert "unverified" in verdict
    assert "re-verify" in verdict


def test_done_headline_trusts_a_graded_result():
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
        post_apply_grade={"state": "graded", "graded": True},
    ))
    assert "unverified" not in env["verdict_text"].lower()


def test_done_promotes_the_recommended_next_step_to_primary():
    """The done screen leads with a forward action, never a click-to-fail one.

    The head of the recommendedness-ordered alternates is promoted — on a
    round that is NOT iterating that head is room correction. The iterating
    case has its own test below, because the head differs there and the
    promotion deliberately inherits the list's ordering rather than naming
    one action."""
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    action = env["next_action"]
    assert action["id"] == "room"
    assert action["href"] == "/correction/room/"


_DONE_VERDICT_VARIANTS = {
    "plain_pass": {},
    "express": {"tier": "express"},
    "verified_target": {"post_apply_grade": {"outcome": "verified_target",
                                             "graded": True}},
    "keep_previous": {"post_apply_grade": {"outcome": "keep_previous",
                                           "graded": True}},
    "inconclusive": {"post_apply_grade": {"outcome": "inconclusive",
                                          "graded": True}},
    "ungraded": {"post_apply_grade": {"state": "unverified", "graded": False}},
    "grade_failed": {"post_apply_grade": {"state": "failed", "graded": False}},
    "grade_inconclusive": {"post_apply_grade": {"state": "inconclusive",
                                                "graded": False}},
    "incomplete": {"post_apply_grade": {"state": "graded", "graded": True,
                                        "complete": False}},
}


@pytest.mark.parametrize("variant", sorted(_DONE_VERDICT_VARIANTS))
def test_no_done_verdict_names_undo(variant):
    """The Undo verb left the wizard (owner ruling): no verdict, no button.

    Swept across every done verdict rather than one branch: a branch that
    quietly re-grew the promise or the ``verify_undo`` action would pass a
    single-screen pin while shipping a control the flow no longer has.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done", applied=True, verify={"outcome": "pass"},
        candidate=_candidate_summary(),
        **_DONE_VERDICT_VARIANTS[variant],
    ))
    assert env["screen"] == "done"
    assert "undo" not in env["verdict_text"].lower(), env["verdict_text"]
    actions = [env["next_action"], *env["alternate_actions"]]
    assert not any(a["id"] == "verify_undo" for a in actions)


def test_a_session_restart_on_an_applied_speaker_still_discloses_the_apply():
    """The applied-override's addendum survives the Undo removal.

    The registry's session-restart copy assumes nothing was applied; the
    envelope must still tell the household the crossover landed, without
    promising a control the flow no longer has.
    """
    restart = {"code": REASON_CAPTURE_TIMEOUT}
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, failure=restart,
    ))
    assert "The crossover was already applied." in env["verdict_text"]
    assert "undo" not in env["verdict_text"].lower()


def test_an_iterating_round_leads_with_another_bite():
    """The promotion takes the HEAD of ``alternate_actions``, not "room".

    That list is already ordered by recommendedness, and an iterating round
    puts ``round_remeasure`` at position 0 precisely because "taking another
    bite is the recommended next step" there. Pins the two halves that could
    silently diverge: which action is promoted, and that it is no longer
    duplicated below.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
        round_receipt={
            "round_id": "s1", "adoption": "keep_for_iteration",
            "row": "row6_trusted_safe_passed_reachable", "reason": "r",
        },
    ))
    assert env["next_action"]["id"] == "round_remeasure"
    assert env["next_action"]["label"] == "Try again with what we learned"
    ids = [a["id"] for a in env["alternate_actions"]]
    assert "round_remeasure" not in ids   # promoted, never offered twice
    assert ids[0] == "room"


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
    assert env["next_action"]["id"] == "room"
    alternates = {a["id"]: a for a in env["alternate_actions"]}
    assert "run_full_measurement" in alternates
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
    # ``pinned`` is on every row, never only the pinned ones: the renderer reads
    # it ``=== true``, so an absent key and a solved trim have to be the same
    # thing on the wire.
    assert review["trims"] == [
        {"role": "woofer", "attenuation_db": -3.1, "pinned": False},
        {"role": "tweeter", "attenuation_db": 0.0, "pinned": False},
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


def test_out_of_band_octaves_carry_the_fit_engines_reason_to_the_screen():
    """#2638: a stopband octave's number is not passband performance.

    ``observe_octave_summary`` runs to the grid's top, so past the woofer's
    own band — target diving 24 dB/oct, measurement floor staying put — the
    subtraction returns a large POSITIVE number. On 2026-08-16 that rendered
    as a bare "+23.0 dB" on the review screen and nearly indicted a candidate
    whose largest filter gain anywhere was +2.5 dB. The fit engine labels
    every one of those octaves in the SAME pass; this asserts the label
    reaches the band the renderer draws, so the renderer never has to
    re-derive it from the frequency.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            linearization_outcome="fitted",
            linearization_octaves={
                "woofer": {"8000": -0.3, "12000": 4.1, "16000": 23.0},
            },
            linearization_octave_reasons={
                "woofer": {
                    "8000": "envelope_fitted",
                    "12000": "envelope_out_of_band",
                    "16000": "envelope_out_of_band",
                    # The fit engine keys reasons over its own full octave
                    # ladder (down to 250 Hz); the top-octave row ignores the
                    # rest exactly as it does for the numbers.
                    "500": "envelope_fitted",
                },
            },
        ),
    ))
    bands = env["candidate_review"]["linearization_octaves"][0]["bands"]
    assert bands == [
        {"hz": 8000, "delta_db": -0.3, "reason": "envelope_fitted"},
        {"hz": 12000, "delta_db": 4.1, "reason": "envelope_out_of_band"},
        {"hz": 16000, "delta_db": 23.0, "reason": "envelope_out_of_band"},
    ]


def test_a_candidate_with_no_recorded_reasons_renders_exactly_as_before():
    """The pre-#2638 candidate, unperturbed.

    Candidates persist (``/state.crossover_v2.candidate``), so a household
    can review one this build never fitted. Nothing recorded WHY its octaves
    read as they do, and inventing a verdict would be worse than the defect —
    so ``reason`` is absent rather than guessed, and the row is byte-identical
    to what the same candidate produced before this key existed.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            linearization_outcome="fitted",
            linearization_octaves={"woofer": {"8000": -0.3, "16000": -2.8}},
        ),
    ))
    assert env["candidate_review"]["linearization_octaves"] == [
        {
            "role": "woofer",
            "bands": [{"hz": 8000, "delta_db": -0.3}, {"hz": 16000, "delta_db": -2.8}],
        },
    ]


def test_class_prior_limited_octaves_carry_their_declared_driver_class():
    """Audit item 4i: the renderer (main.js) needs the declared driver_class
    beside the reason code to tell an already-declared class's own real prior
    apart from the undeclared ("unknown") default — only main.js decides what
    to say about that pair, so this pins the structured passthrough this
    module is responsible for and nothing about rendered prose.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            linearization_outcome="fitted",
            linearization_octaves={
                "tweeter": {"8000": -0.2, "12000": -6.5, "16000": -11.0},
            },
            linearization_octave_reasons={
                "tweeter": {
                    "8000": "envelope_fitted",
                    "12000": "envelope_limited_by_class_prior",
                    "16000": "envelope_limited_by_class_prior",
                },
            },
            linearization_driver_class={"tweeter": "unknown"},
        ),
    ))
    row = env["candidate_review"]["linearization_octaves"][0]
    assert row["driver_class"] == "unknown"
    assert row["bands"] == [
        {"hz": 8000, "delta_db": -0.2, "reason": "envelope_fitted"},
        {"hz": 12000, "delta_db": -6.5, "reason": "envelope_limited_by_class_prior"},
        {"hz": 16000, "delta_db": -11.0, "reason": "envelope_limited_by_class_prior"},
    ]


def test_a_role_with_no_declared_driver_class_omits_the_key():
    """Absent, not empty-stringed — the same present/absent discipline
    ``reason`` uses, so a pre-#4i candidate (or a role ``_candidate_summary``
    never resolved a class for) renders exactly as before this key existed.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            linearization_outcome="fitted",
            linearization_octaves={"woofer": {"8000": -0.3, "16000": -2.8}},
        ),
    ))
    row = env["candidate_review"]["linearization_octaves"][0]
    assert "driver_class" not in row


def test_the_browser_and_python_agree_on_the_out_of_band_octave_code():
    """The one cross-language reason literal, pinned (#2638).

    The renderer decides whether an octave's number is a residual or stopband
    arithmetic by comparing the band's server-supplied ``reason`` against a
    literal it cannot import. Drift costs exactly the bug this fixed: a
    +23.0 dB stopband artifact rendered on the review screen as if it were
    the correction's own doing. Same guard shape, and same reason, as
    ``test_the_browser_and_python_agree_on_which_objectives_are_unmeasured``
    directly above.
    """
    import re
    from pathlib import Path

    from jasper.active_speaker.linearization_envelope import ReasonCode

    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"const OCTAVE_REASON_OUT_OF_BAND = '([a-z0-9_]+)';", source,
    )
    assert match, "the renderer no longer carries a named out-of-band reason code"
    assert match.group(1) == ReasonCode.OUT_OF_BAND.value


def test_the_browser_and_python_agree_on_every_linearization_outcome():
    """The renderer maps this enum to plain language and renders NOTHING for a
    value it does not know, so a Python outcome the browser has never heard of
    makes the round go silent about whether linearization ran."""
    import re
    from pathlib import Path

    from jasper.active_speaker.measured_crossover_candidate import (
        _LINEARIZATION_OUTCOME_VALUES,
    )

    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")
    block = re.search(
        r"const LINEARIZATION_OUTCOME_TEXT = \{(.*?)\n\};", source, re.S,
    )
    assert block, "the renderer no longer carries a linearization-outcome map"
    rendered = set(re.findall(r"^\s{2}([a-z0-9_]+):", block.group(1), re.M))

    # "" is the one Python value with no line to render — it means linearization
    # was never evaluated this attempt, and silence is the honest rendering.
    assert rendered == _LINEARIZATION_OUTCOME_VALUES - {""}


def test_the_browser_and_python_agree_on_the_class_prior_octave_code():
    """The second cross-language reason literal, pinned (audit item 4i).

    Same guard shape and the same reason as the out-of-band test above: the
    renderer decides whether a class-prior-limited band earns a remedy
    sentence by comparing the band's server-supplied ``reason`` against a
    literal it cannot import.
    """
    import re
    from pathlib import Path

    from jasper.active_speaker.linearization_envelope import ReasonCode

    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"const OCTAVE_REASON_LIMITED_BY_CLASS_PRIOR = '([a-z0-9_]+)';", source,
    )
    assert match, "the renderer no longer carries a named class-prior reason code"
    assert match.group(1) == ReasonCode.LIMITED_BY_CLASS_PRIOR.value


def test_the_class_prior_remedy_points_at_the_driver_class_declaration_route():
    """The remedy pointer's route, pinned at the structured level — never the
    rendered sentence's prose, only the ``href`` string it is built from
    (audit item 4i; the safety-limits deep-link rows in refusal_copy.py are
    the mirrored shape, ``{id, label, href}``).
    """
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"const CLASS_PRIOR_REMEDY = \{.*?href: '([^']+)',", source, re.DOTALL,
    )
    assert match, "the renderer no longer carries a named class-prior remedy href"
    assert match.group(1) == "/sound/setup/"


def test_done_candidate_review_carries_the_alignment_objective():
    """WHERE the polarity came from reaches the screen, not just what it is.

    The measurement can decline to decide polarity — on the low-SNR path it
    commits the polarity the PRESET declares — and the review row must not word
    that as a measured result (#2607 S3). The renderer branches on this enum,
    so the enum has to survive the projection from ``_candidate_summary`` into
    ``candidate_review``; the copy itself is pinned by
    ``tests/js/crossover_polarity_provenance_test.mjs``.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            alignment_objective="declared_committed_after_low_snr",
        ),
    ))
    assert env["candidate_review"]["alignment_objective"] == (
        "declared_committed_after_low_snr"
    )
    # An older candidate carries no objective, and renders the empty string
    # rather than a missing key the renderer would have to guard.
    plain = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(),
    ))
    assert plain["candidate_review"]["alignment_objective"] == ""


def test_the_browser_and_python_agree_on_which_objectives_are_unmeasured():
    """The one cross-language list, pinned (#2617).

    The wording decision lives in a browser module, which cannot import
    ``ALIGNMENT_DECLARED_POLARITY_OBJECTIVES`` — so it carries a literal list,
    and a literal list is a second source of truth unless something compares
    them. What a drift would cost is exactly #2607 S3 reopened: a commitment
    whose polarity nothing checked, rendered to a household as "Inverted
    (measured)". Adding a refusal objective in Python and forgetting the
    renderer is the failure this fails on.
    """
    import re
    from pathlib import Path

    from jasper.audio_measurement.program_analysis import (
        ALIGNMENT_DECLARED_POLARITY_OBJECTIVES,
    )

    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"const declaredByDesign = \[(.*?)\]\.includes\(", source, re.DOTALL,
    )
    assert match, "the renderer no longer branches on a literal objective list"
    in_browser = set(re.findall(r"'([a-z0-9_]+)'", match.group(1)))
    assert in_browser == set(ALIGNMENT_DECLARED_POLARITY_OBJECTIVES)


def test_done_candidate_review_carries_whether_the_polarity_was_pinned():
    """The basin pin survives the projection into ``candidate_review``.

    The sibling above carries WHICH objective committed the polarity. That is
    not enough on its own: a round that PINNED the polarity commits the very
    same ``explicit_prescription_committed`` an unpinned prescription does, so
    the objective cannot tell the renderer that nothing measured this polarity.
    The bit does, and it has to survive the same projection the objective does
    or the row silently reverts to "Inverted (measured)".
    """
    pinned = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            alignment_objective="explicit_prescription_committed",
            polarity_pinned=True,
        ),
    ))
    assert pinned["candidate_review"]["polarity_pinned"] is True

    # An UNPINNED prescription is the control, and it is the whole point: the
    # same objective must NOT carry the bit, or the fix would reword every
    # prescribed round rather than the pinned ones.
    unpinned = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            alignment_objective="explicit_prescription_committed",
        ),
    ))
    assert unpinned["candidate_review"]["polarity_pinned"] is False


def test_the_browser_and_python_agree_on_the_pinned_polarity_key():
    """The cross-language guard for a class the list comparison cannot see.

    ``test_the_browser_and_python_agree_on_which_objectives_are_unmeasured``
    compares one literal LIST against one frozenset. The basin pin is carried
    by neither — it is a payload KEY the renderer reads by name — so that guard
    was structurally blind to it, and #2607 S3 reopened through the gap: the
    review row rendered "Inverted (measured)" for a polarity an operator had
    pinned and nothing had measured.

    A key name is a cross-language contract exactly as a literal list is. If
    Python renames the field, or the renderer reads a different one, the bit
    silently reads as absent and the copy silently reverts — no exception, no
    failing assertion anywhere else.
    """
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"const polarityPinned = review\.([a-z0-9_]+) === true;", source,
    )
    assert match, "the renderer no longer reads a named pinned-polarity bit"

    payload = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            alignment_objective="explicit_prescription_committed",
            polarity_pinned=True,
        ),
    ))["candidate_review"]
    # The name the browser reads is a key Python actually sends, and it is True
    # on a pinned round — the renderer's `=== true` accepts nothing weaker.
    assert match.group(1) in payload
    assert payload[match.group(1)] is True


def test_the_browser_and_python_agree_on_the_pinned_crossover_key():
    """The crossover twin of the guard above.

    A topology prescription pins a crossover CORNER + ORDER for one
    measurement round — "the SAME rule one level up" from a pinned polarity,
    per main.js's own comment above `crossoverPinned`. It is carried by a
    SECOND, separate key
    (``CrossoverV2Session._topology_prescription`` -> ``_candidate_summary``
    -> ``_candidate_review_payload``) read by a second `=== true` check in
    the same renderer, so the polarity guard above cannot see it: a rename
    of this key is invisible to that assertion.

    A key name is a cross-language contract exactly as a literal list is. A
    browser module cannot import a Python constant, so if Python renames the
    field, or the renderer reads a different one, the pinned wording
    silently reverts to unworded "measured" prose — no exception, no failing
    assertion anywhere else. The second half closes the sibling gap the
    polarity guard doesn't have to worry about: the key names could agree
    and the row could still render `undefined` if the corner beside the bit
    (`review.crossover.fc_hz`) is not itself a key Python actually sends.
    """
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"const crossoverPinned = review\.([a-z0-9_]+) === true;", source,
    )
    assert match, "the renderer no longer reads a named pinned-crossover bit"

    payload = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            crossover={"fc_hz": 2400.0, "order": 4, "slope_db_per_octave": 24.0},
            crossover_pinned=True,
        ),
    ))["candidate_review"]
    # The name the browser reads is a key Python actually sends, and it is
    # True on a pinned round — the renderer's `=== true` accepts nothing
    # weaker.
    assert match.group(1) in payload
    assert payload[match.group(1)] is True
    # And the corner beside it survived the same round trip — the renderer
    # reads `review.crossover.fc_hz` by name too, so the row cannot render a
    # real pin marker beside an `undefined` number.
    assert payload["crossover"]["fc_hz"] == 2400.0


def test_the_browser_and_python_agree_on_the_pinned_trim_key():
    """The per-driver twin of the two guards above.

    A pinned TRIM is the third thing on this screen that is carried rather than
    measured, and it is the first that is per-ROLE: the bit rides each trim row
    instead of sitting flat beside the list, so neither guard above can see it.
    Same failure mode either way — a rename leaves the row wording a level the
    round was told to hold as one it measured.
    """
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")
    match = re.search(r"const trimPinned = trim\.([a-z0-9_]+) === true;", source)
    assert match, "the renderer no longer reads a named pinned-trim bit"

    payload = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, candidate=_candidate_summary(
            trims_pinned={
                "tweeter": {
                    "pinned_db": -7.25, "displaced_db": -2.0, "delta_db": -5.25,
                },
            },
            trims_db={"woofer": -3.1, "tweeter": -7.25},
        ),
    ))["candidate_review"]
    rows = {row["role"]: row for row in payload["trims"]}
    # The name the browser reads is a key Python actually sends, True on the
    # pinned role and False — never absent — on the one the round solved.
    assert rows["tweeter"][match.group(1)] is True
    assert rows["woofer"][match.group(1)] is False
    # …beside a number that survived the same round trip: the renderer reads
    # `trim.attenuation_db` by name too, so a real pin marker can never sit
    # next to an `undefined` level.
    assert rows["tweeter"]["attenuation_db"] == -7.25


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


def test_every_stamped_era_reaches_the_renderer_by_its_own_name():
    """#2758 minted a THIRD basis, and the reader must pass each through rather
    than collapse the two peak eras.

    They disagree in the direction the earlier eras never had — a
    ``realized_peak`` stamp can read SMALLER than re-emitting the same filters
    charges today — so telling them apart is the whole reason the field exists.
    An unrecognised value still reads ``unknown``: absent-means-unknown extends
    to "a name this build does not know", never to "assume current".
    """
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
        HEADROOM_COST_BASIS_UNKNOWN,
    )

    def _basis_for(stamped: str) -> str:
        env = build_crossover_envelope_v2(_status(
            phase="done", verify={"outcome": "pass"},
            candidate=_candidate_summary(
                headroom_cost_db=5.2, headroom_cost_basis=stamped,
            ),
        ))
        return env["candidate_review"]["headroom_cost"]["basis"]

    for stamped in (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    ):
        assert _basis_for(stamped) == stamped
    assert _basis_for("some_era_from_the_future") == HEADROOM_COST_BASIS_UNKNOWN


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
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 15
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
    assert "wiring" in env["verdict_text"]
    assert "speaker setup" in env["verdict_text"]
    assert "try again" not in env["verdict_text"]
    assert env["next_action"]["href"] == "/sound/setup/"


def test_session_restart_template():
    env = build_crossover_envelope_v2(_status(
        phase="measure", failure={"code": REASON_CAPTURE_TIMEOUT},
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
    """§5.2: one default ("Try again") + the way back when a prior candidate
    exists; the explicit trio behind the expert disclosure."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
        previous_candidate_fingerprint="b" * 64,
    ))
    assert env["screen"] == "verify_fail"
    assert env["next_action"]["label"] == "Try again"
    expert = [a for a in env["alternate_actions"] if a.get("expert")]
    assert [a["id"] for a in expert] == ["verify_remeasure"]
    # W6.12: the way back and Re-measure must survive the JS action-row's
    # relay-in-flight gate (a real window right after a failed capture,
    # before the phone side has fully wound down) — the same show_during_capture
    # escape hatch W6.10 gave the review screen's Apply. "Try again" starts a
    # brand new relay session, so it deliberately does NOT carry the flag.
    way_back = next(
        a for a in env["alternate_actions"] if a["id"] == "republish_previous"
    )
    remeasure = next(
        a for a in env["alternate_actions"] if a["id"] == "verify_remeasure"
    )
    assert way_back["show_during_capture"] is True
    assert remeasure["show_during_capture"] is True
    assert "show_during_capture" not in env["next_action"]


def test_verify_fail_on_a_first_ever_apply_keeps_its_forward_actions():
    """A first-ever apply has no prior banked candidate, so no way back is
    minted — "Try again" and "Re-measure" are still there."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
    ))
    assert env["screen"] == "verify_fail"
    assert env["next_action"]["label"] == "Try again"
    assert [a["id"] for a in env["alternate_actions"]] == ["verify_remeasure"]


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
    just a whole-band offset); the top band is flat.

    This shape is #1857's misattribution class. While the reference was
    pooled across the woofer+tweeter bands the tweeter's own darkness
    dragged that reference down, and the woofer's narrow (and much smaller)
    peak read a LARGER deviation from it than the tweeter's own uniform
    darkness did. The frame is now the low-mid band alone (ADR-0194), which
    no part of the tweeter band is inside, so the same shape charges each
    band its own deviation — kept here because a shape that USED to
    mis-point is the one worth still rendering the disclosure for.
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


def test_the_shipped_pointer_names_the_dark_tweeter_not_the_woofer():
    """Mechanism check against the REAL evaluator, and the anchor ruling
    landing at the household surface.

    This asserted the OPPOSITE while the reference was pooled across the
    woofer and tweeter bands: the tweeter's own darkness dragged the shared
    zero down, the woofer's small narrow peak read the larger absolute
    deviation, and the pointer blamed the flat driver. #1857's anchor choice
    (Q-E) is decided — the frame is the low-mid band alone (ADR-0194) — so
    the same fixture, through the same "absolute dB, not tolerance headroom"
    selection rule, now names the band that is genuinely 6 dB down.
    """
    _compact, report, gauge = _dark_tweeter_compact_cloud()
    assert gauge.max_band_hz == (2000.0, 8000.0)
    assert gauge.max_db == pytest.approx(-6.0043, abs=5e-4)
    woofer = next(
        b for b in report.bands if (b.f_lo_hz, b.f_hi_hz) == (250.0, 2000.0)
    )
    # The woofer is charged its own narrow peak and nothing else: all ripple,
    # no level offset, where it used to read +5.03 dB.
    assert woofer.max_deviation_db == pytest.approx(2.9957, abs=5e-4)
    assert woofer.level_deviation_db == pytest.approx(0.0, abs=1e-9)
    assert abs(woofer.max_deviation_db) < abs(gauge.max_db)
    # ...and the band nothing touched reads as untouched, not +2.03 dB.
    top = next(
        b for b in report.bands if (b.f_lo_hz, b.f_hi_hz) == (8000.0, 16000.0)
    )
    assert top.max_deviation_db == pytest.approx(0.0, abs=5e-3)
    assert top.passed is True


def test_the_expert_disclosure_now_names_every_band_beside_the_pointer():
    """The disclosure. The pointer names one band — per the mechanism test
    above, now the tweeter — and every other band's own number sits right
    beside it, so a reader is never handed the pointer alone. That claim is
    what this test is for and it did not move with the anchor; only which
    band the pointer picks, and the numbers, did."""
    compact, _report, _gauge = _dark_tweeter_compact_cloud()
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"}, cloud={PHASE_CLOUD_VERIFY: compact},
        candidate=_candidate_summary(),
    ))
    details = env["expert_details"]
    # The pointer: unchanged shape, and it NAMES ITS FRAME — which is now
    # the low-mid band, so the sentence a household reads says so.
    assert any(
        line.startswith(
            "flatness -6.00 dB from the 250–2000 Hz reference mean at 2000 Hz"
        )
        for line in details
    )
    # The other line: every band, from the SAME reference, so the woofer's
    # own honest number sits beside the pointed-at one.
    per_band = next(
        line for line in details
        if line.startswith("every band from the same reference:")
    )
    assert "250–2000 Hz +3.00 dB (1.5 dB outside the ±1.5 dB target)" in per_band
    assert "2000–8000 Hz -6.00 dB (4.0 dB outside the ±2.0 dB target)" in per_band
    assert "8000–16000 Hz -0.00 dB (within the ±2.5 dB target)" in per_band


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
    assert "250–2000 Hz +3.00 dB (1.5 dB outside the ±1.5 dB target)" in lead
    assert "2000–8000 Hz -6.00 dB (4.0 dB outside the ±2.0 dB target)" in lead
    assert "8000–16000 Hz -0.00 dB (within the ±2.5 dB target)" in lead


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
    assert "+0.00 dB (within" in lines[0]
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
    assert "250–2000 Hz +0.00 dB (within" in line
    assert "8000–16000 Hz -6.00 dB (3.5 dB outside" in line


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
    assert "250–2000 Hz +3.00 dB (1.5 dB outside" in line
    assert "2000–8000 Hz -4.50 dB (2.5 dB outside" in line
    assert "8000–16000 Hz +1.00 dB (within" in line


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
    assert "2000–8000 Hz -4.50 dB (2.5 dB outside" in line


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


_R18_BRANCH = {"status": "not_evaluated", "reason": "no_per_branch_verify_capture"}
# SYNTHETIC numbers — no hardware measurement is restated here (#2152).
_R18_CLAIMS_FAIL = {
    "woofer_branch": dict(_R18_BRANCH),
    "hf_branch": dict(_R18_BRANCH),
    "integration": {
        "status": "pass", "max_db": 0.069, "tolerance_db": 1.5,
        "band_hz": [2000.0, 4000.0],
    },
    "absolute": {
        "status": "fail", "max_db": 3.98, "rms_db": 1.2, "worst_db": -3.98,
        "worst_hz": 1700.0, "tolerance_db": 2.0, "band_hz": [1000.0, 4000.0],
    },
}


def test_done_screen_names_the_crossover_region_finding_and_the_unchecked_claims():
    """R18 (#1868) — "Verified." must name what the handoff measured and which
    of §7's claims nobody made; two of four are structurally not-evaluated."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "pass", "graded_band_hz": [2000.0, 4000.0],
            "claims": _R18_CLAIMS_FAIL,
        },
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert env["screen"] == "done"
    # The finding carries the CLAIM's own band, not the tracking band beside
    # it — a 1700 Hz dip under a bare "checked 2000–4000 Hz" reads as noise.
    assert "checked 2000–4000 Hz" in env["expert_details"]
    assert (
        "crossover blend -3.98 dB at 1700 Hz over 1000–4000 Hz (limit 2.0 dB)"
        in env["expert_details"]
    )
    assert "each driver on its own was not checked" in env["expert_details"]


def test_the_done_screen_says_what_the_next_round_will_do_about_the_blend():
    """Decision 10's one household sentence, under the number it answers.

    A household that sees the same blend defect reported round after round,
    with nothing saying a lever is aimed at it, reasonably concludes the loop
    is doing nothing. The line is read off the durable receipt, never
    re-derived, so the screen and the graph cannot disagree about what is
    coming.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "pass", "graded_band_hz": [2000.0, 4000.0],
            "claims": _R18_CLAIMS_FAIL,
        },
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
        round_receipt={"blend": {"filters": [
            {"biquad_type": "Peaking", "freq": 1700.0, "q": 2.0, "gain": -2.5},
            {"biquad_type": "Peaking", "freq": 2600.0, "q": 2.0, "gain": -1.0},
        ], "residual_db": 1.4}},
    ))

    details = env["expert_details"]
    assert "the next round trims this region (2 cuts, deepest -2.5 dB)" in details
    # Directly under the finding it answers, not floating elsewhere.
    finding = next(i for i, line in enumerate(details) if "crossover blend" in line)
    trims = next(i for i, line in enumerate(details) if "trims this region" in line)
    assert trims == finding + 1


def test_the_done_screen_says_nothing_when_no_blend_correction_is_coming():
    """Absence is silence, not a line saying "no cuts" — the screen does not
    narrate a stage that has nothing to report."""
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={
            "outcome": "pass", "graded_band_hz": [2000.0, 4000.0],
            "claims": _R18_CLAIMS_FAIL,
        },
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
        round_receipt={"blend": None},
    ))

    assert not any("trims this region" in line for line in env["expert_details"])


def test_verify_fail_screen_names_the_crossover_region_finding():
    """Same disclosure on the failure screen, where the household chooses."""
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": "verify_crossover_region"},
        verify={
            "outcome": "fail", "graded_band_hz": [2000.0, 4000.0],
            "claims": _R18_CLAIMS_FAIL,
        },
    ))
    assert env["screen"] == "verify_fail"
    assert (
        "crossover blend -3.98 dB at 1700 Hz over 1000–4000 Hz (limit 2.0 dB)"
        in env["expert_details"]
    )
    text = env["nudges"][0]["text"]
    assert "didn't blend as designed" in text
    # The copy names the lever that EXISTS on this screen and changes the
    # outcome — never a dead one. It deliberately omits "try again": that
    # re-checks the same applied graph, and this defect is deterministic.
    assert "Re-measure to fit it again." in text
    assert "Try again" not in text
    assert env["next_action"]["id"] == "verify_retry"  # still offered, not sold
    assert {a["id"] for a in env["alternate_actions"]} == {"verify_remeasure"}


def test_a_passing_crossover_region_is_disclosed_not_silent():
    """The number IS the claim: shown only on a failure, a household could not
    know a passing handoff was measured at all."""
    claims = {
        **_R18_CLAIMS_FAIL,
        "absolute": {
            "status": "pass", "max_db": 0.69, "rms_db": 0.3, "worst_db": 0.69,
            "worst_hz": 1050.0, "tolerance_db": 2.0, "band_hz": [1000.0, 4000.0],
        },
    }
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "claims": claims},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert (
        "crossover blend +0.69 dB at 1050 Hz over 1000–4000 Hz (limit 2.0 dB)"
        in env["expert_details"]
    )


def test_best_evaluated_keeps_the_target_miss_visible_without_overclaiming():
    """The miss stays visible, and the sentence claims no comparison (2.4).

    Two wrong referents have been through this copy. It used to open "the best
    measured option from the complete comparison" — a field of alternatives the
    corner selector once produced and no round produces now. Replacing that with
    "improved on the sound it replaced" was wrong the other way: the margin is
    THIS candidate's linearized forecast against THIS SAME candidate's
    un-linearized one (``accountability`` binds ``before = grade_prediction(
    raw_predicted_sum)``), never the previously-applied graph — that is
    ``commanded.py``'s separate instrument.

    So the sentence claims the prediction match and the miss, and nothing else.
    The assertions below pin BOTH failed referents as negatives, because each
    one read plausibly enough to ship once. ``RESULT_VERIFIED_BEST_EVALUATED``
    is unchanged — it is banked in log events and receipts.
    """
    env = build_crossover_envelope_v2(_status(
        phase="done", applied=True,
        verify={"outcome": "pass"},
        post_apply_grade={
            "outcome": "verified_best_evaluated", "graded": True,
            "absolute_miss_db": 4.3139, "absolute_worst_hz": 1590.4083,
        },
    ))
    text = env["verdict_text"].lower()
    assert "matched its prediction" in text
    assert "misses the target by 4.31 db near 1.59 khz" in text
    # Both failed referents, pinned as negatives.
    assert "comparison" not in text
    assert "best measured option" not in text
    assert "improved on the sound it replaced" not in text
    assert "within spec" not in text
    assert "best achievable" not in text
    assert "perfect" not in text
    assert "microphone" not in text and "measurement page" not in text
    assert env["nudges"][0]["code"] == "crossover_v2_verified_best_evaluated"


@pytest.mark.parametrize(
    ("outcome", "copy", "badge"),
    (
        ("verified_target", "reached the target", "Target verified."),
        ("keep_previous", "should not replace", "Keep the previous sound."),
        ("inconclusive", "not enough complete evidence", "Result inconclusive."),
    ),
)
def test_terminal_outcome_household_copy(outcome, copy, badge):
    env = build_crossover_envelope_v2(_status(
        phase="done", applied=True, verify={"outcome": "pass"},
        post_apply_grade={"outcome": outcome, "graded": True},
    ))
    assert copy in env["verdict_text"]
    assert env["nudges"][0]["text"] == badge


# --- #2464: a failed mark-VERIFY is not masked by a passing group ------------


def _passing_post_apply_group():
    """A closed post-apply group whose spatial verdict PASSED, in the compact
    shape ``crossover_v2_status_block`` publishes."""
    return {
        PHASE_CLOUD_VERIFY: {
            "geometry_locked": False, "thin_evidence": False,
            "geometry_guidance": "", "spec_bands": [], "overall_passed": True,
            "excluded_interval_count": 0,
            "flatness": {
                "max_db": 0.9, "max_hz": 1650.0, "max_band_hz": [1250.0, 2000.0],
                "reference_band_hz": [250.0, 8000.0], "tolerance_db": 2.5,
                "rms_db": 0.4, "n_bins": 900, "n_excluded": 12,
                "evaluable": True, "passed": True,
            },
        },
    }


def test_a_failed_verify_keeps_the_household_on_the_previous_sound():
    """#2464, ruled 2026-08-19: the copy and badge a re-verify that FAILED
    against a carried-forward passing spatial group must produce. The terminal
    result code already routed this cell honestly; what the ruling caps is the
    grade state underneath it, so this pins the household's own two sentences
    against a regression in either producer."""
    env = build_crossover_envelope_v2(_status(
        phase="done", applied=True, tier="full",
        verify={
            "outcome": "fail",
            "claims": {"integration": {"status": "fail", "max_db": 4.2}},
        },
        cloud=_passing_post_apply_group(),
    ))
    assert "should not replace" in env["verdict_text"]
    assert env["nudges"][0]["text"] == "Keep the previous sound."


def test_a_failed_check_is_never_described_as_one_that_never_finished():
    """A pre-claims state file has no claim record and no comparison, so no
    terminal result code overrides this screen's copy — and "never finished"
    is false of a check that ran and did not pass, exactly the argument the
    inconclusive arm beside it was written for. Before #2464 the closed group
    masked the state entirely and the household read "Your speaker is
    tuned." over a failed check."""
    env = build_crossover_envelope_v2(_status(
        phase="done", applied=True, tier="full",
        verify={"outcome": "fail"},
        cloud=_passing_post_apply_group(),
    ))
    assert "did not match its prediction" in env["verdict_text"]
    assert "never finished" not in env["verdict_text"]
    assert "could not tell either way" not in env["verdict_text"]


def test_a_not_evaluated_crossover_region_renders_no_number():
    """Absence stays absence — an ungraded claim produces no sentence."""
    claims = {
        **_R18_CLAIMS_FAIL,
        "absolute": {
            "status": "not_evaluated", "reason": "no_trusted_crossover_region",
        },
    }
    env = build_crossover_envelope_v2(_status(
        phase="done",
        verify={"outcome": "pass", "claims": claims},
        cloud=_cloud_flatness_status(passed=True),
        candidate=_candidate_summary(),
    ))
    assert not any("crossover blend" in line for line in env["expert_details"])
    # The per-branch gap is still named — it is a different claim.
    assert "each driver on its own was not checked" in env["expert_details"]


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
    assert verdict.endswith("Re-verify to try again.")
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
        "either way. Re-verify to try again."
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
    ("Try again") screen shape — with its own copy naming the actual cause
    (the mic chain moved, not the speaker)."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", failure={"code": REASON_VERIFY_LEVEL_SHIFT},
    ))
    assert env["screen"] == "verify_fail"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_VERIFY_LEVEL_SHIFT].message
    assert env["next_action"]["label"] == "Try again"


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
    assert "re-verify" not in verdict.lower()
    assert env["next_action"]["label"] == "Try again"
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Re-measure" in labels


# --- #1873: the wizard drops a retry its own copy has just ruled out ----------


def test_deterministic_mismatch_promotes_remeasure_over_a_dead_retry():
    """The household's field report on this screen was that "Try again" was the
    only obvious control, so they took it until the relay session expired. For a
    verdict that has already established the mismatch repeats, that button is a
    dead lever presented as the next step: it opens a fresh /v2/verify and
    re-checks the SAME applied graph.

    So the primary becomes Re-measure — the lever that CAN change the outcome,
    by fitting a new crossover — and the way back stays beside it when a
    prior banked candidate exists. What changes is which action the screen
    steers towards, and that the dead one is gone."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        failure={"code": REASON_VERIFY_DETERMINISTIC_MISMATCH},
    ))
    assert env["screen"] == "verify_fail"
    assert env["verdict_text"] == (
        REASON_REGISTRY[REASON_VERIFY_DETERMINISTIC_MISMATCH].message
    )
    assert env["next_action"]["id"] == "verify_remeasure"
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/session"
    # Promoted, not gated behind the expert disclosure it sat in as an
    # alternate — a primary the screen steers towards is not a disclosure.
    assert "expert" not in env["next_action"]
    # …and it keeps the flag that makes it survive the wizard's
    # relay-in-flight gate while the ended session winds down. On a primary
    # the same flag also suppresses the connect link/QR for that dead relay,
    # which is the wanted behaviour for a verdict that ends the session.
    assert env["next_action"]["show_during_capture"] is True
    ids = [a["id"] for a in env["alternate_actions"]]
    # …and not offered twice.
    assert "verify_remeasure" not in ids
    assert "verify_retry" not in ids


def test_deterministic_mismatch_copy_matches_the_controls_on_its_own_screen():
    """The #1924 rule: every control the sentence names is on this screen, and
    the sentence does not name one that is not. It must not say "Try again" —
    that button is gone precisely because this verdict says it cannot help."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        failure={"code": REASON_VERIFY_DETERMINISTIC_MISMATCH},
    ))
    verdict = env["verdict_text"]
    assert "Re-measure" in verdict
    assert "Try again" not in verdict
    assert "Re-measure" in env["next_action"]["label"]


def test_no_registry_sentence_names_undo():
    """The Undo verb left the wizard (owner ruling): no reason copy may point
    a household at a control the flow no longer has. Counted over the whole
    registry — banners, messages, and the anchor-branched
    ``correction_rollback_failed`` renderings — so a future row that re-grows
    the promise fails here rather than shipping."""
    for code, spec in REASON_REGISTRY.items():
        for text in (
            spec.message, spec.banner,
            reason_message(code, spec),
            reason_message(code, spec, rollback_anchor_available=True),
            reason_message(code, spec, rollback_anchor_available=False),
        ):
            assert "undo" not in text.lower(), (code, text)


def test_the_rollback_failed_copy_never_names_a_way_back_the_screen_cannot_mint():
    """Review row 6 of the normal-path revert: sentence/button non-drift.

    The failure record's ``rollback_anchor_available`` describes the ROUND,
    but the True-arm sentence's remedy clause ("Go back to the previous
    tuning") describes a control on THIS screen — and the way-back action is
    minted only from a ``previous_candidate_fingerprint`` the republish door's
    own read-only admission has passed. When the pointer is not offerable the
    copy must fall to the no-way-back arm (which asserts no cause, #2859)
    rather than advertise a button that refuses on the same fact.
    """
    recorded_true = {
        "code": REASON_CORRECTION_ROLLBACK_FAILED,
        "rollback_anchor_available": True,
    }

    offered = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        failure=dict(recorded_true),
        previous_candidate_fingerprint="b" * 64,
    ))
    assert offered["screen"] == "verify_fail"
    assert "previous tuning" in offered["verdict_text"]
    assert any(
        action["id"] == "republish_previous"
        for action in offered["alternate_actions"]
    )

    unofferable = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        failure=dict(recorded_true),
    ))
    assert unofferable["screen"] == "verify_fail"
    assert "previous tuning" not in unofferable["verdict_text"]
    assert "measure again" in unofferable["verdict_text"].lower()
    assert not any(
        action["id"] == "republish_previous"
        for action in unofferable["alternate_actions"]
    )


def test_a_retriable_verify_fail_code_keeps_its_try_again():
    """The regression guard on the swap above. Every OTHER verify_fail code is
    retriable and its retry is a real lever, so the shipped screen — "Try
    again" primary, expert Re-measure alternate — is untouched."""
    for code in (
        REASON_VERIFY_OUT_OF_TOLERANCE, REASON_VERIFY_INCONCLUSIVE,
        REASON_VERIFY_LEVEL_SHIFT, REASON_VERIFY_CROSSOVER_REGION,
    ):
        env = build_crossover_envelope_v2(_status(
            phase="verify", applied=True,
            failure={"code": code},
        ))
        assert env["screen"] == "verify_fail", code
        assert env["next_action"]["id"] == "verify_retry", code
        alternates = {a["id"]: a for a in env["alternate_actions"]}
        assert alternates["verify_remeasure"]["expert"] is True, code


def test_a_budget_zero_code_reaching_this_screen_by_the_applied_override_keeps_retry():
    """The swap is keyed on the code's OWN registry row — verify_fail template
    AND budget 0 — never on budget alone, and this is why.

    ``capture_timeout`` and ``user_stopped`` are budget-0 ``session_restart``
    rows that land on this screen only because something is applied (W6.7
    ruling 3). Their zero budget says no further CAPTURE of what failed can
    help; it says nothing about the VERIFY check, and here "Try again" means a
    fresh /v2/verify session — which for a dead relay is precisely the fix.
    Keying on budget alone would have taken the working button away."""
    for code in (REASON_CAPTURE_TIMEOUT, REASON_USER_STOPPED):
        assert REASON_REGISTRY[code].retry_budget == 0, code
        env = build_crossover_envelope_v2(_status(
            phase="verify", applied=True, failure={"code": code},
        ))
        assert env["screen"] == "verify_fail", code
        assert env["next_action"]["id"] == "verify_retry", code


def test_an_unknown_code_on_the_verify_fail_screen_keeps_its_retry():
    """A code with no registry row makes no claim about whether a retry can
    help, so the defensive branch keeps the affordance rather than removing one
    on the strength of an absent record."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, failure={"code": "not_a_registered_code"},
    ))
    assert env["screen"] == "verify_fail"
    assert env["next_action"]["id"] == "verify_retry"


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
    apply) rendered fix_and_retry and displaced the verify_fail screen's
    route out. REASON_AGC_BEHAVIORAL_FAIL's OWN registry template is
    fix_and_retry (correct for CHECK/MEASURE, where nothing is applied yet);
    once the crossover is durably applied, the same code must render
    verify_fail instead. ``applied=True`` here is the REAL state fact a
    production status always carries whenever phase is genuinely "verify"
    (see test_applied_true_forces_verify_fail_regardless_of_phase for the
    adversarial-review case where phase and applied disagree)."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        previous_candidate_fingerprint="b" * 64,
        failure={"code": REASON_AGC_BEHAVIORAL_FAIL},
    ))
    assert env["screen"] == "verify_fail"
    assert env["verdict_text"] == REASON_REGISTRY[REASON_AGC_BEHAVIORAL_FAIL].message
    way_back = next(
        a for a in env["alternate_actions"] if a["id"] == "republish_previous"
    )
    assert way_back["endpoint"] == "/correction/crossover/v2/republish"


def test_check_phase_agc_failure_still_renders_its_normal_template():
    """The SAME code at CHECK (nothing applied yet) is untouched — still
    fix_and_retry, no Undo affordance to offer."""
    env = build_crossover_envelope_v2(_status(
        phase="check", failure={"code": REASON_AGC_BEHAVIORAL_FAIL},
    ))
    assert env["screen"] == "fix_and_retry"
    assert env["alternate_actions"] == []


def test_verify_phase_relay_timeout_also_renders_verify_fail():
    """A non-agc code (REASON_CAPTURE_TIMEOUT's own template is
    session_restart) gets the same applied override -- ANY failure code
    surfacing once genuinely applied is entitled to the route out."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, failure={"code": REASON_CAPTURE_TIMEOUT},
    ))
    assert env["screen"] == "verify_fail"


def test_verify_phase_unknown_code_renders_verify_fail_too():
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        previous_candidate_fingerprint="b" * 64,
        failure={"code": "some_future_code"},
    ))
    assert env["screen"] == "verify_fail"
    ids = [a["id"] for a in env["alternate_actions"]]
    assert "republish_previous" in ids


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
        phase="check", applied=True,
        previous_candidate_fingerprint="b" * 64,
        failure={"code": REASON_USER_STOPPED},
    ))
    assert env["screen"] == "verify_fail"
    assert "already applied" in env["verdict_text"].lower()
    ids = [a["id"] for a in env["alternate_actions"]]
    assert "republish_previous" in ids


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
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION
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
        REASON_CAPTURE_TIMEOUT, phase="verify", applied=True,
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


_WAY_BACK_FP = "b" * 64


@pytest.mark.parametrize(
    "screen, status",
    [
        ("done", _status(
            phase="done", applied=True, verify={"outcome": "pass"},
            candidate=_candidate_summary(),
            previous_candidate_fingerprint=_WAY_BACK_FP,
        )),
        ("verify_fail", _status(
            phase="verify", failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
            previous_candidate_fingerprint=_WAY_BACK_FP,
        )),
        ("microphone_check", _aged_status(
            REASON_CAPTURE_TIMEOUT, phase="verify", applied=True,
            previous_candidate_fingerprint=_WAY_BACK_FP,
        )),
    ],
    ids=["done", "verify_fail", "aged_entry"],
)
def test_the_three_way_back_screens_offer_the_banked_way_back(screen, status):
    """The done funnel, verify_fail, and the aged resume all carry the way
    back when a prior banked candidate exists.

    The action carries the PRIOR candidate's fingerprint in its own body, so
    the tap needs no client-side knowledge — the JS POSTs endpoint+body
    generically. Republish, not restore: the ordinary republish -> review ->
    apply path is the route back.
    """
    env = build_crossover_envelope_v2(status)
    assert env["screen"] == screen
    way_back = [
        a for a in env["alternate_actions"] if a["id"] == "republish_previous"
    ]
    assert len(way_back) == 1
    assert way_back[0]["endpoint"] == "/correction/crossover/v2/republish"
    assert way_back[0]["body"] == {"fingerprint": _WAY_BACK_FP}
    # Survives the JS relay-in-flight gate (W6.12): a get-me-out affordance
    # must stay visible while a failed capture's relay is still winding down.
    assert way_back[0]["show_during_capture"] is True


@pytest.mark.parametrize("screen_status", [
    _status(
        phase="done", applied=True, verify={"outcome": "pass"},
        candidate=_candidate_summary(),
    ),
    _status(
        phase="verify", failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
    ),
    _aged_status(
        REASON_CAPTURE_TIMEOUT, phase="verify", applied=True,
    ),
], ids=["done", "verify_fail", "aged_entry"])
def test_no_way_back_is_minted_without_a_prior_candidate_fingerprint(screen_status):
    """The control: a stash naming no banked candidate mints no dead button.

    A first-ever apply, and a prior profile that was not a measured-candidate
    apply, both leave ``previous_candidate_fingerprint`` unset — republish
    could only refuse, so the action must not appear at all."""
    env = build_crossover_envelope_v2(screen_status)
    assert not any(
        a["id"] == "republish_previous" for a in env["alternate_actions"]
    )


def test_aged_entry_screen_differs_from_a_clean_start_in_EXACTLY_two_keys():
    """IA over copy (#1941 design principle 1), pinned over the FULL envelope.

    The resume is not a new screen: it is the same entry screen a clean start
    renders, differing only by one quiet history nudge and — because a prior
    banked candidate exists — the way back. An earlier revision of this test
    hand-listed six keys to compare and so let `cloud` / `cloud_chart` /
    `tier` diverge unnoticed; comparing every key means a third diverging key
    can never pass again."""
    clean = build_crossover_envelope_v2(_status(phase="check"))
    aged = build_crossover_envelope_v2(
        _stale_measurement_status(
            REASON_CAPTURE_TIMEOUT, phase="verify",
            previous_candidate_fingerprint=_WAY_BACK_FP,
        ),
    )
    diverged = {
        key for key in set(clean) | set(aged)
        if clean.get(key) != aged.get(key)
    }
    assert diverged == {"alternate_actions", "nudges"}, (
        f"aged resume diverges from a clean start in {sorted(diverged)}; only "
        "the history nudge and the way-back action may differ"
    )
    # And the two intended deltas are exactly what they claim to be.
    assert clean["nudges"] == []
    assert len(aged["nudges"]) == 1
    assert [a for a in aged["alternate_actions"]
            if a not in clean["alternate_actions"]] == [
        {
            "id": "republish_previous",
            "label": "Go back to the previous tuning",
            "endpoint": "/correction/crossover/v2/republish",
            "body": {"fingerprint": _WAY_BACK_FP},
            "show_during_capture": True,
        },
    ]


def test_way_back_action_never_shares_mutable_state_between_envelopes():
    """The action carries a mutable ``body``. If it came from one module-level
    dict copied shallowly, every envelope this process ever served would share
    that dict — one mutation would poison the daemon for its whole life."""
    first = build_crossover_envelope_v2(_aged_status(
        REASON_CAPTURE_TIMEOUT, phase="verify", applied=True,
        previous_candidate_fingerprint=_WAY_BACK_FP,
    ))
    way_back = [
        a for a in first["alternate_actions"] if a["id"] == "republish_previous"
    ][0]
    way_back["body"]["poisoned"] = True

    second = build_crossover_envelope_v2(_aged_status(
        REASON_CAPTURE_TIMEOUT, phase="verify", applied=True,
        previous_candidate_fingerprint=_WAY_BACK_FP,
    ))
    assert [a for a in second["alternate_actions"]
            if a["id"] == "republish_previous"][0]["body"] == {
        "fingerprint": _WAY_BACK_FP,
    }
    # The live verify-fail screen shares the same factory, so it is covered
    # by the same guarantee.
    live = build_crossover_envelope_v2(_status(
        phase="verify", applied=True,
        previous_candidate_fingerprint=_WAY_BACK_FP,
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
    ))
    assert [a for a in live["alternate_actions"]
            if a["id"] == "republish_previous"][0]["body"] == {
        "fingerprint": _WAY_BACK_FP,
    }


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
        " — it wasn't confirmed."
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
    assert note == f"Your last measurement ended {expected} — it wasn't confirmed."


def test_aged_failure_note_dates_a_previous_year_explicitly():
    """A speaker that sat unused for a year must not date its history with a
    bare "July 29" that reads as this year — the same year rule household
    dates already follow elsewhere in the tree (jasper.tools.gmail)."""
    stamp = time.localtime(time.time() - 400 * _DAY_S)
    env = build_crossover_envelope_v2(_aged_status(
        REASON_CAPTURE_TIMEOUT, age_s=400 * _DAY_S, phase="check",
    ))
    assert str(stamp.tm_year) in _history_note(env)


# --- B2: a reason whose copy is durable state, not a session outcome ---------


@pytest.mark.parametrize("code,expected", [
    (REASON_VERIFY_OUT_OF_TOLERANCE, "it wasn't confirmed"),
    (REASON_CAPTURE_TIMEOUT, "it stopped before finishing"),
    (REASON_USER_STOPPED, "it stopped before finishing"),
    (REASON_CHANNEL_MAP_MISMATCH, "it couldn't continue"),
    # The one code that used to keep a durable-fact sentence through aging;
    # the exemption left with the Undo machinery, so it ages like any other
    # hard_stop and the way-back action beside the note is the remedy.
    (REASON_CORRECTION_ROLLBACK_FAILED, "it couldn't continue"),
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


@pytest.mark.parametrize("code", sorted(REASON_REGISTRY))
def test_every_registry_code_ages_into_a_household_readable_note(code):
    """No reason code can age into a bare slug, an empty line, an unterminated
    sentence, or copy that names hardware the flow does not talk about (#1941
    R4: the actor is the microphone; household copy never says "phone")."""
    env = build_crossover_envelope_v2(
        _aged_status(code, age_s=_WEEK_S, phase="measure"),
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
    would discard previous_candidate_fingerprint and the way back with it).
    Undated means
    "we cannot say this is current", so it renders as history — and says
    "earlier" rather than inventing a date."""
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
        "Your last measurement ended earlier — it wasn't confirmed."
    )


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
            "phase": "check", "failure": {"code": REASON_CAPTURE_TIMEOUT, "at": stamp},
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
    fresh = {"code": REASON_CAPTURE_TIMEOUT, "at": time.time()}
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, failure=fresh,
        previous_candidate_fingerprint=_WAY_BACK_FP,
        verify=_PRIOR_SESSION_EVIDENCE,
    ))
    assert env["screen"] == "verify_fail"
    assert env["next_action"]["id"] == "verify_retry"
    assert _labels(env) == ["Go back to the previous tuning", "Re-measure"]
    # The applied-override addendum and this session's own numbers, intact.
    assert "The crossover was already applied." in env["verdict_text"]
    assert "level error 3.82 dB (limit 1.5 dB)" in env["expert_details"]


def test_freshness_boundary_is_the_declared_window():
    """The window is a stated contract, not an accident of rounding: just
    inside it is the live screen, just outside it is history."""
    from jasper.active_speaker.crossover_envelope_v2 import SESSION_FRESH_WINDOW_S

    inside = build_crossover_envelope_v2(_aged_status(
        REASON_VERIFY_OUT_OF_TOLERANCE, age_s=SESSION_FRESH_WINDOW_S - 30,
        phase="verify", applied=True,
    ))
    outside = build_crossover_envelope_v2(_aged_status(
        REASON_VERIFY_OUT_OF_TOLERANCE, age_s=SESSION_FRESH_WINDOW_S + 30,
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


# --- #1947: a session, not just a failure, has a lifetime ------------------------
#
# #1942 gave the FAILURE record a clock. A session can end — walked away from,
# browser closed, Pi rebooted — without ever persisting one, and those states
# replayed the dead session's screen, its live imperative ("put the microphone
# back… follow the measurement page") and its numbers on the next plain GET.


def _dead_session_status(phase: str, **v2) -> dict:
    """A durable session that last did something a day ago, and left no
    failure record — the household that closed the browser and came back."""
    return _status(
        phase=phase, session_id="cap_dead_session",
        updated_at=time.time() - _DAY_S,
        verify=_PRIOR_SESSION_EVIDENCE, cloud=_PRIOR_SESSION_CLOUD,
        cloud_chart=_PRIOR_SESSION_CLOUD_CHART, tier="full", **v2,
    )


@pytest.mark.parametrize("phase,applied", [
    ("check", False),
    ("measure", False),
    ("cloud_measure", False),
    ("lateral", False),
    ("closing", False),
    # The one that files the issue: post-apply the stale state resolves to
    # PHASE_VERIFY, whose screen tells the household to go stand in front of
    # the speaker and confirm yesterday's result.
    ("verify", True),
    ("cloud_verify", True),
])
def test_dead_session_phase_screen_becomes_the_dated_entry_screen(phase, applied):
    """The aged marker on every phase whose session is over, and no live
    imperative: the only offer is one that OPENS a session, never one that
    continues the dead one, and none of its numbers survive."""
    env = build_crossover_envelope_v2(_dead_session_status(phase, applied=applied))
    assert env["screen"] == "microphone_check"
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/session"
    assert [a.get("endpoint") for a in env["alternate_actions"]] == [
        "/correction/crossover/v2/session",
    ]
    assert (env["cloud"], env["cloud_chart"], env["tier"]) == (None, None, None)
    assert env["expert_details"] == []
    assert _history_note(env).startswith("Your last measurement ended yesterday")


@pytest.mark.parametrize("phase,screen", [
    ("check", "microphone_check"),
    ("measure", "measure"),
    ("verify", "verify"),
    ("cloud_verify", "verify"),
])
def test_live_session_phase_screen_is_untouched(phase, screen):
    """No regression to the live path: a session the household is inside
    renders the screen it renders today, numbers and all."""
    env = build_crossover_envelope_v2(_status(
        phase=phase, session_id="cap_live", applied=phase.endswith("verify"),
        cloud=_PRIOR_SESSION_CLOUD, tier="full",
    ))
    assert env["screen"] == screen
    assert env["cloud"] == _PRIOR_SESSION_CLOUD
    assert env["nudges"] == []


def test_dead_post_apply_session_never_offers_the_confirm_prompt():
    """The hazard the aged branch exists to avoid: PHASE_VERIFY's own screen
    invites the household to confirm a result measured a day ago."""
    live = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, session_id="cap_live", tier="full",
    ))
    assert live["next_action"]["id"] == "verify_start"
    dead = build_crossover_envelope_v2(_dead_session_status("verify", applied=True))
    assert dead["next_action"]["id"] != "verify_start"
    assert "verify_start" not in [a["id"] for a in dead["alternate_actions"]]


@pytest.mark.parametrize("phase,screen", [
    # A receipt about what the speaker is playing NOW.
    ("done", "done"),
    # Untimed by construction (D3.5): the entry screen has no route back to a
    # pending candidate, so aging this out would strand the whole commission.
    ("review", "review"),
])
def test_durable_phases_are_exempt_from_the_session_clock(phase, screen):
    env = build_crossover_envelope_v2(_dead_session_status(
        phase, applied=phase == "done", candidate=_candidate_summary(),
    ))
    assert env["screen"] == screen


@pytest.mark.parametrize("relay_status,screen", [
    # The wizard still holds the slot: the session cannot be over, whatever
    # the clock says. An unknown status reads as in flight for the same
    # reason ``SESSION_ENDED_STATUSES`` does.
    ("awaiting_capture", "verify"),
    ("committing", "verify"),
    ("some_future_status", "verify"),
    ("complete", "microphone_check"),
    ("stopped", "microphone_check"),
    ("failed", "microphone_check"),
])
def test_a_live_relay_outranks_the_clock(relay_status, screen):
    """A commission's own wall-clock ceiling is 3600 s and the closing screen's
    confirm waits on a human, so an in-flight session can idle past the
    freshness window — and a slot the wizard still holds proves it is not
    over."""
    status = _dead_session_status("verify", applied=True)
    status["capture"] = {"status": relay_status}
    assert build_crossover_envelope_v2(status)["screen"] == screen


def test_session_without_a_timestamp_reads_as_not_current():
    """Migration, fail-honest, and without a schema bump: a state file this
    build cannot date is one whose currency it cannot assert."""
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "verify", "applied": True, "session_id": "cap_undated",
            "verify": _PRIOR_SESSION_EVIDENCE,
        },
    })
    assert env["screen"] == "microphone_check"
    assert _history_note(env) == (
        "Your last measurement ended earlier, with the tuning it found "
        "already applied to your speaker."
    )


def test_a_speaker_that_never_measured_is_not_greeted_with_history():
    """The opposite dishonesty: no state file means no session to be dead, and
    the clock reads absent for the same reason a stale one does."""
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {"phase": "check"},
    })
    assert env["screen"] == "microphone_check"
    assert env["nudges"] == []


# --- #2100: a failed walk names what it banked ----------------------------------


def test_failed_post_apply_walk_names_what_survived():
    """The failed Full stage 2 kept an applied tuning and a passing check at
    the mark, and the terminal screen said only that the measurement ended —
    so the honest complete re-walk read as losing everything."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", applied=True, tier="full", session_id="cap_live",
        verify={"outcome": "pass"},
        failure={"code": REASON_CAPTURE_TIMEOUT},
    ))
    banked = [n for n in env["nudges"] if n["code"] == "crossover_v2_banked_progress"]
    assert len(banked) == 1
    assert banked[0]["severity"] == "info"


@pytest.mark.parametrize("v2", [
    # Nothing applied — there is no banked tuning to name.
    {"applied": False, "tier": "full"},
    # Express has no cross-position post-apply walk to be missing.
    {"applied": True, "tier": "express"},
    # The walk closed: nothing about it is outstanding.
    {"applied": True, "tier": "full", "cloud": {"cloud_verify": {"geometry": {}}}},
])
def test_banked_progress_line_is_silent_when_state_cannot_support_it(v2):
    env = build_crossover_envelope_v2(_status(
        phase="verify", session_id="cap_live",
        failure={"code": REASON_CAPTURE_TIMEOUT}, **v2,
    ))
    assert [n for n in env["nudges"]
            if n["code"] == "crossover_v2_banked_progress"] == []


# --- W6.1 Finding D: the v2 relay slot is visible in the envelope ----------------


def test_envelope_carries_relay_block_awaiting_and_after_failure():
    """The v2 envelope threads status['relay'] into BOTH the awaiting-phone
    screen and the failure screen, so a page reload keeps the tap link and the
    failure copy reaches the household (Finding D — the slot was invisible)."""
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_PROGRAM_UNPLAYABLE,
    )

    capture = {"tap_link": "https://capture.test/#s=cap_x", "status": "awaiting_capture"}

    awaiting = build_crossover_envelope_v2({**_status(phase="check"), "capture": capture})
    assert awaiting["capture"] == capture

    failed = build_crossover_envelope_v2({
        **_status(phase="check", failure={"code": REASON_PROGRAM_UNPLAYABLE}),
        "capture": capture,
    })
    assert failed["screen"] == "hard_stop"
    assert failed["capture"] == capture
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

    Mirrors ``jasper.web.correction_crossover_v2_status._prediction_status``'s
    output
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
    """D3.5 + **D6**: apply-and-verify / measure again / Keep current sound.

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
    assert env["next_action"]["label"] == "Apply and verify"
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/apply"
    assert [a["id"] for a in env["alternate_actions"]] == [
        "review_remeasure", "review_decline",
    ]
    every_action = [env["next_action"], *env["alternate_actions"]]
    assert not any("restore" in str(a.get("endpoint") or "") for a in every_action)
    assert not any("undo" in str(a.get("id") or "").lower() for a in every_action)
    assert not any("undo" in str(a.get("label") or "").lower() for a in every_action)


def test_review_decline_exits_to_the_active_speaker_entry_not_the_hub():
    """#1985: "Keep current sound" must land the household back where the
    journey started, not in another subsystem's permission flow.

    The generic ``/correction/`` hub is the Room-correction wizard, and its
    first act is the browser-mic HTTPS-transition interstitial — a non-sequitur
    for someone who just finished a crossover measurement and chose to keep
    things as they are. The R8 slice check found exactly that landing.

    Pinned as a literal, not "anything under /correction/": ``/correction/`` is
    a prefix of ``/correction/crossover/``, so a containment assertion would
    have passed against the bug.

    Since #2641 the href is a PRESENTATION HINT beside a real endpoint rather
    than the action itself — see
    :func:`test_the_decline_is_an_action_not_a_navigation`. Where it points is
    still the household-visible fact this test owns.
    """
    env = build_crossover_envelope_v2(_review_status())
    decline = next(
        a for a in env["alternate_actions"] if a["id"] == "review_decline"
    )
    assert decline["label"] == "Keep current sound"
    assert decline["href"] == "/correction/crossover/"
    remeasure = next(
        a for a in env["alternate_actions"] if a["id"] == "review_remeasure"
    )
    assert remeasure["endpoint"] == "/correction/crossover/v2/session"
    assert "href" not in remeasure


def test_the_decline_is_an_action_not_a_navigation():
    """#2641, measured live: the Keep button performed no action at all.

    Five clicks on a real round-2 review screen produced a plain page reload
    back onto the SAME decision screen — no state-changing request, no
    acknowledgement — because the mint carried an ``href`` and no ``endpoint``.
    A household that has decided was asked again indefinitely, and the round
    record could not tell a decline from a household that never looked.

    The candidate guard is pinned alongside, because a decline recorded
    against a superseded candidate would close a review nobody saw.
    """
    status = _review_status()
    fingerprint = status["crossover_v2"]["candidate"]["fingerprint"]

    decline = next(
        a for a in build_crossover_envelope_v2(status)["alternate_actions"]
        if a["id"] == "review_decline"
    )

    assert decline["endpoint"] == "/correction/crossover/v2/decline"
    assert decline["body"] == {"expected_candidate_fingerprint": fingerprint}


def test_every_in_flow_action_the_envelope_mints_is_machine_actionable():
    """One flow, N drivers: a decision must be performable without a browser.

    The invariant, stated so it can be checked rather than intended: an action
    whose ``href`` points back INTO this flow is a decision, and a decision has
    to carry an ``endpoint`` a driver can POST. An action pointing at another
    subsystem (``/correction/room/``, ``/sound/setup/``) is a navigation and is
    exempt — no endpoint here could perform it, and minting a fake one would be
    worse than the link.

    This is the shape #2641 was: ``review_decline``'s href was
    ``/correction/crossover/`` — in-flow — with no endpoint, so it looked like
    an exit and behaved like a reload. Every screen is swept rather than the
    one that was reported, because the next instance of this bug will be on a
    different screen.
    """
    offenders: list[tuple[str, str]] = []
    for name, env in _every_screen_envelope().items():
        actions = [env.get("next_action"), *(env.get("alternate_actions") or [])]
        for action in actions:
            if not isinstance(action, dict):
                continue
            href = str(action.get("href") or "")
            if not href.startswith("/correction/crossover"):
                continue
            if not action.get("endpoint"):
                offenders.append((name, str(action.get("id") or "?")))

    assert offenders == [], (
        "an in-flow action with no endpoint is a decision a driver cannot "
        "take, and a button a household clicks to no effect", offenders,
    )


def test_review_names_no_corner_in_the_apply_action():
    """One fixed label, because nothing on this screen re-answers "will Apply
    move the declared crossover?" (ticket 2.4).

    The button used to read the retired corner selector's banked recommendation
    and name it — "Use 1750.6 Hz and apply". That was a second reading of a
    question ``handle_v2_apply`` already answers from the candidate's own
    preset, and once an operator's topology pin became the live producer of a
    candidate crossing away from the declaration it could only UNDER-claim: a
    pinned round publishes no recommendation, so the button fell back to the
    generic label on exactly the rounds that do move the corner.

    A legacy recommendation still sitting in durable state must not resurrect
    it — a stale corner in the button is a promise about an apply that is not
    about to make it.
    """
    selection = {
        "verdict": "recommend_alternative",
        "configured_hz": 2500.0,
        "recommended_hz": 1750.6,
        "comparison_complete": True,
    }
    env = build_crossover_envelope_v2(_review_status(fc_selection=selection))

    assert env["next_action"]["label"] == "Apply and verify"
    assert env["next_action"]["id"] == "review_apply"
    assert not any("1750.6" in line for line in env["expert_details"])

    # The apply-blocked nudge is now gated on the fact its sentence is about —
    # Sound holds a saved revision and the DSP load behind it is unconfirmed —
    # rather than on a selector verdict that used to be its only route here.
    retry = build_crossover_envelope_v2(_review_status(
        accepted_sound_revision=7,
        apply_blocked={
            "id": "load_failed",
            "message": "The saved crossover was not applied",
        },
    ))
    assert retry["next_action"]["label"] == "Apply and verify"
    assert any(nudge["code"] == "load_failed" for nudge in retry["nudges"])


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


def test_a_box_that_cannot_open_stage_2_still_offers_apply_and_discloses():
    """**D3's stage-2 openability preflight, demoted from gate to disclosure.**

    The refusal renders AS ITSELF — the predicate's own sentence, which
    already names what to finish first — and its declared resolution control
    is offered beside it, from the same registry entry the hard-stop screen
    reads (#1820's precedent). Never a generic "cannot apply". Early, because
    the refusal is knowable now (#1828).

    Apply itself stays ENABLED: a disabled control was never the boundary,
    and the apply transaction re-runs the same predicate
    (``_assert_stage_2_can_open``) and refuses a truly un-openable stage 2.
    """
    env = build_crossover_envelope_v2(_review_status(stage2_preflight={
        "ok": False,
        "message": "JTS could not use this speaker's saved safety limits.",
        "next_action": {
            "id": "review_safety_limits",
            "label": "Review safety limits",
            "href": "/sound/#confirm-safety-limits",
        },
    }))
    assert env["next_action"]["enabled"] is True
    refusal = [n for n in env["nudges"]
               if n["code"] == "crossover_v2_stage2_preflight_refused"]
    assert refusal and "could not use this speaker" in refusal[0]["text"]
    assert "will be refused" in refusal[0]["text"]
    assert env["alternate_actions"][0]["id"] == "review_safety_limits"


def test_a_refusal_button_never_renders_without_its_explaining_sentence():
    """Review N-2: the two halves of one refusal are gated as one thing.

    The sentence used to also require a gradeable prediction while the button
    did not, so an ungradeable prediction beside an action-carrying refusal
    rendered a bare "Review safety limits" control with nothing on screen
    saying why it was there. Both render: the D4 disable (ungradeable) and
    the stage-2 disclosure are independent facts, and the disclosure is
    knowable now (#1828).
    """
    env = build_crossover_envelope_v2(_review_status(
        prediction=_prediction(overall_passed=None, bands=[]),  # ungradeable
        stage2_preflight={
            "ok": False,
            "message": "JTS could not use this speaker's saved safety limits.",
            "next_action": {
                "id": "review_safety_limits",
                "label": "Review safety limits",
                "href": "/sound/#confirm-safety-limits",
            },
        },
    ))
    # Disabled by D4 (ungradeable prediction) — the preflight no longer
    # disables anything.
    assert env["next_action"]["enabled"] is False
    # The button is there...
    assert env["alternate_actions"][0]["id"] == "review_safety_limits"
    # ...and so is the sentence that explains it.
    assert [n for n in env["nudges"]
            if n["code"] == "crossover_v2_stage2_preflight_refused"]
    # Both facts reach the household: the verdict copy names the one the
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
            "message": "JTS could not use this speaker's saved safety limits.",
            "next_action": {
                "id": "review_safety_limits",
                "label": "Review safety limits",
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


def test_an_unresolved_preflight_still_discloses():
    """Absence is not a clean reading. An unset key means the predicate never
    ran, and "we never checked" must disclose exactly like "we checked and it
    refused" — only an explicit ``ok: True`` renders quiet. Apply stays
    enabled either way: the disclosure is not a gate, and the apply
    transaction owns the refusal."""
    for preflight in ({}, None, {"ok": "yes"}, {"message": "..."}):
        env = build_crossover_envelope_v2(_review_status(stage2_preflight=preflight))
        assert env["next_action"]["enabled"] is True, preflight
        assert [n for n in env["nudges"]
                if n["code"] == "crossover_v2_stage2_preflight_refused"], preflight


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
    gained ``findings``. #2881's (14 → 15): the ``relay`` block gained
    ``source`` and its hold gained ``prompt``/``hand_released``. All additive —
    no key removed or re-typed — so an unredeployed page ignores the new keys
    rather than refusing the envelope, the same property the 8 → 9 bump had."""
    env = build_crossover_envelope_v2(_review_status())
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 15
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
    from jasper.active_speaker.crossover_v2.journey import PHASE_DONE

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
        failure={"code": REASON_CAPTURE_TIMEOUT, "at": time.time() - _DAY_S},
        phase="verify", applied=True, findings=_banked_finding(age_s=_DAY_S),
    ))
    assert env["screen"] == "microphone_check"
    assert env["findings"] == []
    assert len(env["nudges"]) == 1


# --- G1's ripple reservation (owner ruling 2026-08-03, issue #2087) -----------


def _reservation(ripple_db: float = 15.244, threshold_db: float = 15.0) -> dict:
    """A banked reservation as ``persist_conductor_state`` writes it.

    Defaults are the live 2026-08-03 bench validation that produced the ruling:
    15.244 dB against the 15.0 dB threshold, refused 58 s after an
    identically-positioned 11.324 dB capture was accepted.
    """
    return {
        "ripple_reservation": {
            "predicted_ripple_db": ripple_db,
            "threshold_db": threshold_db,
        }
    }


def test_the_ripple_reservation_reaches_both_decision_screens():
    """**The acceptance.** A capture the flow accepted WITH a reservation says
    so, in one plain sentence, on the two screens that owe it.

    Owner ruling #2087: the threshold stopped refusing and became a disclosure
    trigger. The screens are the decision (review) and the result (done) —
    the same pair the banked-finding line lands on, and for the same reason:
    one is where the household chooses, the other is where they are told the
    speaker is tuned.
    """
    review = build_crossover_envelope_v2(_review_status(measure=_reservation()))
    done = build_crossover_envelope_v2(_done_status(measure=_reservation()))
    for env in (review, done):
        texts = [n["text"] for n in env["nudges"]]
        assert RIPPLE_RESERVATION_COPY in texts
        # ONE sentence, not a lecture (the ruling's own 80/20 scope).
        assert texts.count(RIPPLE_RESERVATION_COPY) == 1
        # `info`, never `warn`: the session succeeded and this is something to
        # know, not a problem to solve.
        reservation = next(
            n for n in env["nudges"] if n["text"] == RIPPLE_RESERVATION_COPY
        )
        assert reservation["severity"] == "info"
        assert reservation["code"] == "crossover_v2_ripple_reservation"


def test_a_clean_capture_says_nothing_about_ripple_at_all():
    """No reservation banked means silence — no line, no reassurance.

    The counterpart every disclosure needs: a screen that says "no concerns"
    on a clean measurement spends a household's attention on a non-event, and
    would make the reservation itself easy to skim past.
    """
    for env in (
        build_crossover_envelope_v2(_review_status()),
        build_crossover_envelope_v2(_done_status()),
    ):
        assert all(n["text"] != RIPPLE_RESERVATION_COPY for n in env["nudges"])
        assert all("predicted ripple" not in line for line in env["expert_details"])


def test_the_reservation_numbers_ride_the_expert_disclosure():
    """The sentence carries no number; the collapsed expert block carries both.

    Household copy stays plain, and the measured value plus the threshold it
    was judged against stay available to whoever wants them.
    """
    for env in (
        build_crossover_envelope_v2(_review_status(measure=_reservation())),
        build_crossover_envelope_v2(_done_status(measure=_reservation())),
    ):
        assert (
            "predicted ripple 15.24 dB, above the 15.0 dB disclosure threshold"
            in env["expert_details"]
        )
    # The plain sentence quotes neither number — a household reading it is not
    # asked to judge a decibel figure.
    assert "15.2" not in RIPPLE_RESERVATION_COPY
    assert "15.0" not in RIPPLE_RESERVATION_COPY


def test_the_calibration_reservation_reaches_both_decision_screens():
    """Audit gauntlet 5a: an accepted MEASURE with no resolved mic
    calibration says so, in one plain sentence, on the two screens the
    ripple reservation above owes it to and for the same reason: one is
    where the household decides, the other is where they are told the
    speaker is tuned.
    """
    review = build_crossover_envelope_v2(
        _review_status(measure={"calibration_reservation": True})
    )
    done = build_crossover_envelope_v2(
        _done_status(measure={"calibration_reservation": True})
    )
    for env in (review, done):
        texts = [n["text"] for n in env["nudges"]]
        assert MIC_CALIBRATION_RESERVATION_COPY in texts
        # ONE sentence, not a lecture — the same 80/20 scope the ripple
        # reservation's own single-sentence rule follows.
        assert texts.count(MIC_CALIBRATION_RESERVATION_COPY) == 1
        reservation = next(
            n for n in env["nudges"]
            if n["text"] == MIC_CALIBRATION_RESERVATION_COPY
        )
        # `warn`, not `info`: unlike the ripple reading, this one is directly
        # actionable before the NEXT measurement.
        assert reservation["severity"] == "warn"
        assert reservation["code"] == "crossover_v2_mic_calibration_reservation"
    # The sentence names the concrete surface, never a vague "check your
    # setup" — the structured fact this pin is actually about.
    assert "/correction/" in MIC_CALIBRATION_RESERVATION_COPY


def test_a_calibrated_capture_says_nothing_about_the_mic_at_all():
    """No reservation banked means silence — the same counterpart every
    disclosure needs (mirrors
    ``test_a_clean_capture_says_nothing_about_ripple_at_all``).
    """
    for env in (
        build_crossover_envelope_v2(_review_status()),
        build_crossover_envelope_v2(_done_status()),
    ):
        assert all(
            n["text"] != MIC_CALIBRATION_RESERVATION_COPY for n in env["nudges"]
        )


def test_the_expert_line_quotes_the_threshold_the_capture_was_judged_against():
    """The threshold is read from the BANKED RECORD, never re-read from the
    live constant.

    Why this is pinned rather than assumed: the reservation is a statement
    about what was true when the capture was judged, and
    ``MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB`` is documented PROVISIONAL. A
    renderer that re-read the constant would retro-caption every previously
    banked reservation the moment it moved — including making one read
    "above the 18.0 dB threshold" for a capture judged at 15.0.
    """
    env = build_crossover_envelope_v2(_done_status(
        measure=_reservation(ripple_db=16.5, threshold_db=12.0),
    ))
    assert (
        "predicted ripple 16.50 dB, above the 12.0 dB disclosure threshold"
        in env["expert_details"]
    )
    assert str(MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB) not in "".join(
        env["expert_details"]
    )


@pytest.mark.parametrize("record", [
    {"ripple_reservation": {"predicted_ripple_db": None, "threshold_db": 15.0}},
    {"ripple_reservation": {"threshold_db": 15.0}},
    {"ripple_reservation": {"predicted_ripple_db": float("nan")}},
    {"ripple_reservation": "15.244"},
    {"ripple_reservation": {}},
    {},
    None,
])
def test_an_unusable_reservation_record_renders_silence(record):
    """A malformed record says nothing rather than half a disclosure.

    ``crossover_v2_status_block`` copies this key through unvalidated (like
    ``candidate`` and ``verify`` beside it), so the envelope is the validating
    reader — and it runs on the wizard's 1.5 s poll, where an escaping
    conversion is a 500 on a plain page load.
    """
    env = build_crossover_envelope_v2(_done_status(measure=record))
    assert all(n["text"] != RIPPLE_RESERVATION_COPY for n in env["nudges"])
    assert all("predicted ripple" not in line for line in env["expert_details"])


def test_the_reservation_sentence_names_no_part_of_the_speaker():
    """Household copy stays phenomenon-level, per the register
    ``jasper.attribution.findings`` enforces on its own household strings.

    This sentence does not go through ``Finding``, so nothing enforces the
    prohibition at construction — which is exactly why it is pinned here. The
    honest words for what interferes would be hardware nouns, and naming one
    would be a device-taxonomy guess this session never measured.
    """
    from jasper.attribution.findings import _HARDWARE_NOUN_RE

    assert _HARDWARE_NOUN_RE.search(RIPPLE_RESERVATION_COPY) is None
    # And no un-glossed instrument jargon: the owner himself asked what
    # "ripple" meant, so the household sentence does not use the word.
    assert "ripple" not in RIPPLE_RESERVATION_COPY.lower()


def test_the_reservation_never_claims_the_result_is_worse():
    """It qualifies the EVIDENCE, not the outcome.

    The measurement says how coherently two branches summed; it does not say
    how the speaker will sound, and every accountability gate that grades the
    correction itself still ran. Copy promising a worse result would be a
    claim no instrument in this session made.
    """
    lowered = RIPPLE_RESERVATION_COPY.lower()
    assert "evidence" in lowered
    for overclaim in ("will sound", "worse", "may not work", "poor result"):
        assert overclaim not in lowered


def test_verify_fail_carries_the_reservation_numbers_but_no_second_sentence():
    """The failure screen gets G1's numbers and NOT its household sentence.

    Numbers, because "the measurement this tuning was built from was rough" is
    exactly the context for a verify that did not settle, and this screen's
    reader has already opened the disclosure to look at numbers.

    No sentence, matching the banked-findings precedent (`_verify_fail_envelope`
    passes no `findings` either): the household copy here is the failure's own,
    and a competing caveat beside the one action they are asked to take would
    dilute it.
    """
    env = build_crossover_envelope_v2(_status(
        phase="verify",
        failure={"code": REASON_VERIFY_OUT_OF_TOLERANCE},
        measure=_reservation(),
    ))
    assert env["screen"] == "verify_fail"
    assert (
        "predicted ripple 15.24 dB, above the 15.0 dB disclosure threshold"
        in env["expert_details"]
    )
    assert all(n["text"] != RIPPLE_RESERVATION_COPY for n in env["nudges"])


def test_the_reservation_does_not_displace_the_verified_badge():
    """It is appended BESIDE whichever badge the verify outcome earned, never
    instead of it — the same rule the level-mismatch caveat follows.

    They answer different questions ("did it match its prediction" vs "how
    good was the evidence"), so neither may silence the other.
    """
    env = build_crossover_envelope_v2(_done_status(measure=_reservation()))
    texts = [n["text"] for n in env["nudges"]]
    assert "Verified." in texts
    assert RIPPLE_RESERVATION_COPY in texts
    # The badge keeps the slot it earned; the reservation follows it.
    assert texts.index("Verified.") < texts.index(RIPPLE_RESERVATION_COPY)


@pytest.mark.parametrize(
    ("case", "ordinal", "offered"),
    [
        ("mid-series", 1, True),
        ("the last round the budget allows", 2, True),
        ("the cap itself", 3, False),
        ("past it, from a state nobody should be able to produce", 9, False),
        ("an ordinal no build wrote", None, True),
        ("a corrupt ordinal", True, True),
    ],
    ids=["round1", "round2", "at_cap", "past_cap", "absent", "corrupt"],
)
def test_a_missed_round_at_the_cap_offers_no_fourth_bite(case, ordinal, offered):
    """The budget at the button, and since #2656 the second of two bounds.

    A capped MISSED series now ends in the adoption table itself, on row 7, so
    a round graded by this build never reaches this screen carrying row 2 at
    the cap. Every receipt in this case list is therefore one banked BEFORE
    that change — row 2 at every ordinal — and they are exactly what this check
    still covers: the done screen reads persisted receipts, and offering a
    fourth bite on a three-round budget is what "only the budget, the plateau,
    and the safety class end a series" forbids however old the record is.

    The unreadable cases OFFER, matching the direction
    ``series_position_from_state`` already fails in: a lost history resolves to
    the first round, never to "the cap was reached".
    """
    receipt = {
        "round_id": "s1",
        "adoption": "keep_for_iteration",
        "row": "row2_trusted_safe_missed",
        "reason": "benefit_unproven",
    }
    if ordinal is not None:
        receipt["round_ordinal"] = ordinal

    env = _round_done_env(**receipt)

    # Anywhere on the action row: an iterating round's re-measure is promoted
    # to the primary, so both slots count as "offered".
    ids = {a["id"] for a in [env["next_action"], *env["alternate_actions"]]}
    assert ("round_remeasure" in ids) is offered, case
    # The CAVEAT stays either way — the round did miss something, and saying so
    # is honest whether or not another round is on offer.
    assert "crossover_v2_keep_for_iteration" in {n["code"] for n in env["nudges"]}


def test_a_capped_missed_round_says_the_series_is_over_without_claiming_a_pass():
    """#2656's household sentence, and the three things it has to get right.

    A capped MISSED end is the one ending where both halves of the news are
    true at once: the speaker is playing the best measured sound, and some of
    what was measured is still off target, and there is no round left to fix
    it. Row 1's sentence would claim the opposite of the second half; row 2's
    would promise the remedy this screen no longer offers.
    """

    env = _round_done_env(
        adoption="keep",
        row="row7_trusted_safe_missed_exhausted",
        reason="round_cap_reached",
        round_ordinal=3,
    )
    nudge = _nudge(env, "crossover_v2_keep_for_iteration")

    assert nudge["severity"] == "warn"
    assert nudge["text"] == KEEP_MISSED_EXHAUSTED_TEXT
    # The best measured state stays, and the outstanding targets are said.
    assert "best sound measured so far" in nudge["text"]
    assert "still off target" in nudge["text"]
    # The series is over: no remedy is promised, and no pass is claimed.
    assert "measuring again" not in nudge["text"]
    assert "inside the target" not in nudge["text"]
    assert nudge["text"] != KEEP_FOR_ITERATION_TEXT
    # And the button matches the sentence — a screen that said "the last round"
    # beside a re-measure control would be two answers to one question.
    assert "round_remeasure" not in {a["id"] for a in env["alternate_actions"]}


def test_the_capped_missed_row_is_not_offered_a_bite_at_any_ordinal():
    """The row alone withholds it, whatever ordinal the receipt carries.

    The ordinal check above is the bound for receipts banked before #2656.
    This row is the bound for every round graded since, and it must not depend
    on the ordinal being readable — a receipt whose ordinal was lost still
    ended its series, and the row says so.
    """

    for ordinal in (3, 9, None, True):
        receipt = {
            "adoption": "keep",
            "row": "row7_trusted_safe_missed_exhausted",
            "reason": "round_cap_reached",
        }
        if ordinal is not None:
            receipt["round_ordinal"] = ordinal
        env = _round_done_env(**receipt)
        assert "round_remeasure" not in {
            a["id"] for a in env["alternate_actions"]
        }, ordinal


def test_the_cap_the_button_reads_is_the_headroom_axis_own_constant():
    """One budget, two readers. A literal here would be a second definition
    that a change to ``ROUND_SERIES_CAP`` would silently turn into a lie."""
    import inspect

    from jasper.active_speaker import crossover_envelope_v2 as envelope
    from jasper.active_speaker.crossover_v2.round_evidence import ROUND_SERIES_CAP

    source = inspect.getsource(envelope._round_is_iterating)
    assert "ROUND_SERIES_CAP" in source
    assert str(ROUND_SERIES_CAP) not in source, (
        "the cap must be read from its owner, never spelled into this screen"
    )
