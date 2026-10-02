# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Mover screens, preconditions, and shared status projections."""
from __future__ import annotations


import pytest


from jasper.active_speaker.crossover_envelope_v2 import (
    CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
    _PHASE_STEP,
    build_crossover_envelope_v2,
)
from jasper.active_speaker.measurement_programs import PROGRAM_ROWS, preset
from jasper.active_speaker.timing_status import timing_status_lines
from jasper.active_speaker.wizard_client import SESSION_PATH
from jasper.audio_measurement.timing_verification import timing_verification
from jasper.identity.reader import SPEAKER_SETUP_PAGE_PATH
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    REASON_VERIFY_INCONCLUSIVE,
    reason_message,
)
from jasper.active_speaker.round_copy import RUN_ENDED, RUN_UNDER_WAY, round_lines

V2_STEP_IDS = ("speaker_setup", "microphone_check", "measure", "verify")


@pytest.mark.parametrize("placed, terminal", [(False, False), (True, False), (True, True)])
def test_round_lines_and_pose_actions_come_from_the_coordinator(placed, terminal):
    facts = {"pose": 2, "poses": 3, "mover": "human", "sweep": 4, "sweeps_per_pose": [7, 7, 7],
             "role": "tweeter", "repeat": 2, "repeats": 3, "measurement": 2, "measurements": 3,
             "pose_details": [{}, {"azimuth_deg": -20, "elevation_deg": 0}, {}]}
    action = {"id": "position_ready", "label": "", "endpoint": "/placed", "body": {"index": 3, "attempt": 1}}
    if terminal:
        facts["status"] = "complete"
    capture = {"status": "complete" if terminal else "awaiting_capture", "run": facts,
               "position_pending": None if placed else {"mover": "human", "actions": [action]}}
    env = build_crossover_envelope_v2({**_status(phase="measure"), "capture": capture})
    assert env["round_lines"] == round_lines(facts, pending=not placed)
    if terminal:
        assert env["verdict_text"] == RUN_ENDED
        assert env["terminal_status"] == "complete"
        assert env["pending"] is None and not env["busy"]
        assert env["capture"] is None and env["next_action"]
        return
    actions = env["pending"]["actions"]
    assert [a["id"] for a in actions] == (["retake", "reset_round"] if placed else ["position_ready", "retake", "reset_round"])
    assert env["capture"] is None and env["busy"]
    if not placed:
        assert actions[0] is action
    assert actions[-2]["endpoint"] == "/sound/speaker/crossover/v2/retake"
    assert actions[-1]["endpoint"] == "/sound/speaker/crossover/capture-cancel"
    assert actions[-1]["body"] == actions[-2]["body"] == {}


def _status(**v2) -> dict:
    return {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2,
        "capture": {"status": "awaiting_capture"} if v2.get("phase") in {
            "measure", "verify", "cloud_measure", "cloud_verify", "lateral", "timing",
        } and not v2.get("failure") else None,
    }


def _step_statuses(env: dict) -> dict[str, str]:
    return {step["id"]: step["status"] for step in env["steps"]}


def _every_screen_envelope() -> dict[str, dict]:
    return {
        "inactive": build_crossover_envelope_v2({"active": False}),
        "speaker_setup": build_crossover_envelope_v2({"active": True}),
        **{phase: build_crossover_envelope_v2(_status(phase=phase))
           for phase in ("check", "measure", "verify", "closing")},
        "finished": build_crossover_envelope_v2({
            **_status(), "capture": {"status": "complete", "run": {"status": "complete"}},
        }),
    }


def test_schema_version_and_v2_step_tuple():
    env = build_crossover_envelope_v2(_status(phase="check"))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 19
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
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 19
    assert env["flow"] == "v2"


def test_inactive_speaker_gets_not_applicable():
    env = build_crossover_envelope_v2({"active": False})
    assert env["screen"] == "not_applicable"
    assert env["active"] is False
    # Nothing to do on this speaker from this flow, so it mints no action
    # rather than a link into a subsystem that does not apply to it.
    assert env["next_action"] is None
    assert env["alternate_actions"] == []


@pytest.mark.parametrize("floor_declared,screen", [(False, "speaker_setup"), (True, "awaiting_plan")])
@pytest.mark.parametrize("setup_status", ["blocked", "ready"])
def test_first_experiment_needs_driver_limits_but_no_applied_profile(floor_declared, screen, setup_status):
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": setup_status},
        "driver_safety_profile": {"issues": [] if floor_declared else [{"code": "tweeter:required_highpass_missing"}]},
        "crossover_v2": {"phase": "check"},
    })
    assert env["screen"] == screen
    assert _step_statuses(env)["speaker_setup"] == ("done" if floor_declared else "active")


@pytest.mark.parametrize("phase", ["check", "measure", "verify", "closing"])
def test_awaiting_plan_without_a_staged_run(phase):
    env = build_crossover_envelope_v2({**_status(phase=phase), "capture": None})
    assert env["screen"] == "awaiting_plan"
    assert env["next_action"] is None
    assert env["alternate_actions"] == []


@pytest.mark.parametrize("phase", ["review", "applying", "done"])
@pytest.mark.parametrize("receipt, current_ordinal", [(None, 1), ({"round_ordinal": 3}, 4)])
def test_durable_completion_survives_an_empty_capture_slot(phase, receipt, current_ordinal):
    env = build_crossover_envelope_v2({
        **_status(phase=phase, round_receipt=receipt), "capture": None,
    })
    assert (env["screen"], env["terminal_status"], env["phase"]) == ("finished", "complete", phase)
    assert env["round_ordinal"] == (None if phase == "done" else current_ordinal)
    assert env["verdict_text"] == RUN_ENDED
    assert env["next_action"]["id"] == "reset"
    assert env["action_note"] is None


@pytest.mark.parametrize("profile,round_,expected", [
    (None, None, {"saved": "", "verification": "", "next_action": None}),
    ({"timing": {"delay_us": 22, "polarity": "normal", "provenance": "measured",
                 "measured": {"margin_db": 1.2, "repeat_spread_db": .3, "repeat_spread_us": 2}}}, None,
     {"saved": "Saved timing: delay 22 µs; polarity normal; provenance measured; margin 1.2 dB; repeat spread 0.3 dB / 2 µs.",
      "verification": "", "next_action": None}),
    ({"timing": {"delay_us": 22, "polarity": "normal", "provenance": "set_by_user"}},
     {"alignment_verdict": {"verification": {"residual_rms_db": .61, "repeat_noise_db": .2}}},
     {"saved": "Saved timing: delay 22 µs; polarity normal; provenance set_by_user.",
      "verification": "Saved timing explains today's sum to within 0.61 dB; repeat noise 0.2 dB.", "next_action": None}),
    ({"timing": {"delay_us": 22, "polarity": "normal", "provenance": "set_by_user"}},
     {"alignment_verdict": {"verification": {"residual_rms_db": .61, "repeat_noise_db": .2}},
      "next_action": {"label": "Reset timing"}},
     {"saved": "Saved timing: delay 22 µs; polarity normal; provenance set_by_user.",
      "verification": "Saved timing explains today's sum to within 0.61 dB; repeat noise 0.2 dB; Reset timing.",
      "next_action": {"label": "Reset timing"}}),
    ({"timing": {"delay_us": 22, "polarity": "normal", "provenance": "set_by_user"}},
     {"alignment_verdict": {"verification": timing_verification(10.6, .43, snr_short=("woofer", "tweeter"))},
      "next_action": {"label": "measure timing again"}},
     {"saved": "Saved timing: delay 22 µs; polarity normal; provenance set_by_user.",
      "verification": "Saved timing is not comparable with today's sum (snr short: tweeter, woofer); measure timing again.",
      "next_action": {"label": "measure timing again"}}),
])
def test_timing_status_lines(profile, round_, expected):
    assert timing_status_lines(profile, round_) == expected


@pytest.mark.parametrize("action_id, runs_a_round", [
    ("measure_timing", True), ("remeasure_timing", True), ("reset_timing", False), ("apply_timing", False),
])
def test_a_timing_action_leads_to_the_door_that_does_its_job(action_id, runs_a_round):
    """#5925: a speaker round measures timing; a reset or an apply opens the speaker page's tuning prompt."""
    env = build_crossover_envelope_v2({**_status(phase="check"), "timing": {"next_action": {"id": action_id}}})
    action = env["next_action"]
    assert action["id"] == action_id
    if runs_a_round:
        plan = preset(action["body"]["request"]["program"])
        assert (action["endpoint"], plan.purpose, plan.timing_take) == (SESSION_PATH, "speaker", True)
        assert action["body"]["request"]["layout"] in plan.layouts
    else:
        assert (action.get("endpoint"), action["href"]) == (None, SPEAKER_SETUP_PAGE_PATH)


@pytest.mark.parametrize("fault, action_id, target", [
    ("agc_behavioral_fail", "crossover_v2_retake", "/sound/speaker/crossover/v2/retake"),
    ("position_hold_expired", "restart_session", "/sound/speaker/crossover/reset"),
    ("commissioning_evidence_persist_failed", "restart_session", "/sound/speaker/crossover/reset"),
    ("clipped", None, None),
    ("verify_crossover_region", "crossover_v2_retake", "/sound/speaker/crossover/v2/retake"),
])
def test_failure_templates_preserve_their_own_actions(fault, action_id, target):
    env = build_crossover_envelope_v2({
        **_status(failure={"code": fault}), "capture": {"status": "awaiting_capture"},
    })
    action = env["next_action"] or {}
    assert (env["screen"], env["terminal_status"]) == ("finished", None)
    assert action.get("id") == action_id
    assert (action.get("endpoint") or action.get("href")) == target
    assert env["alternate_actions"] == []


@pytest.mark.parametrize("code", sorted(code for code, spec in REASON_REGISTRY.items() if not spec.own_action))
def test_a_row_with_no_action_of_its_own_offers_a_cli_the_button_its_failure_screen_shows(code):
    env = build_crossover_envelope_v2({**_status(failure={"code": code}), "capture": {"status": "awaiting_capture"}})
    shown, offered = env["next_action"], REASON_REGISTRY[code].next_action
    assert (shown and shown["label"]) == (offered and offered["label"])


@pytest.mark.parametrize("capture_status, expected_action", [
    ("awaiting_capture", "crossover_v2_retake"), ("failed", None), (None, None),
])
def test_retaking_a_failure_requires_a_live_run(capture_status, expected_action):
    capture = {"status": capture_status} if capture_status else None
    env = build_crossover_envelope_v2({
        **_status(failure={"code": "agc_behavioral_fail"}), "capture": capture,
    })
    action = env["next_action"] or {}
    assert action.get("id") == expected_action
    assert env["terminal_status"] == (None if expected_action else "failed")
    assert env["capture"] == (capture if expected_action else None)
    assert [nudge["code"] for nudge in env["nudges"]] == ([] if expected_action else ["run_ended"])


@pytest.mark.parametrize("terminal, fault", [
    ("complete", ""), ("stopped", ""),
    ("failed", "measurement_graph_unavailable"), ("failed", "unregistered_fault"),
])
def test_finished_run_uses_the_registry_action_and_reset(terminal, fault):
    env = build_crossover_envelope_v2({
        **_status(phase="verify", candidate={"fingerprint": "candidate"}),
        "capture": {"status": terminal, "run": {"status": terminal, "fault": fault}},
    })
    reset = {"id": "reset", "label": "Start over",
             "endpoint": "/sound/speaker/crossover/reset", "body": {}}
    spec = REASON_REGISTRY.get(fault)
    action = dict(spec.own_action) if spec and spec.own_action else None
    assert env["screen"] == "finished"
    assert env["next_action"] == (action or reset)
    assert env["alternate_actions"] == []
    assert env["terminal_status"] == terminal
    assert env["capture"] is None


@pytest.mark.parametrize("previous_failure", [None, {"code": "user_stopped"}])
@pytest.mark.parametrize("mover", ["human", "cli", "turntable"])
def test_staged_run_uses_the_measure_screen_and_the_existing_prompt(mover, previous_failure):
    capture = {"status": "awaiting_join", "join": {"mover": mover, "prompt": {"title": "First pose"}}}
    env = build_crossover_envelope_v2({**_status(failure=previous_failure), "capture": capture})
    assert env["screen"] == "measure"
    assert env["capture"] == capture
    assert env["next_action"] is None


def test_measure_phase_is_phone_driven():
    env = build_crossover_envelope_v2(_status(phase="measure"))
    assert env["screen"] == "measure"
    assert env["next_action"] is None
    assert _step_statuses(env)["measure"] == "active"


def _lateral_headline(program: str | None) -> str:
    run = {} if program is None else {"program": program}
    status = {**_status(phase="lateral"), "capture": {"status": "awaiting_capture", "run": run}}
    return build_crossover_envelope_v2(status)["verdict_text"]


@pytest.mark.parametrize("row", PROGRAM_ROWS, ids=lambda row: row.purpose)
def test_a_walks_headline_is_the_run_programs_own(row):
    """Every take but the speaker's per-driver ones walks in the lateral phase, so the run's program words it."""
    assert _lateral_headline(preset(row.purpose).preset) == row.run_headline


def test_no_two_programs_share_a_headline():
    assert len({row.run_headline for row in PROGRAM_ROWS}) == len(PROGRAM_ROWS)


@pytest.mark.parametrize("program", [None, "", "nearfield/each"])
def test_a_walk_no_program_owns_gets_the_neutral_headline(program):
    assert _lateral_headline(program) == RUN_UNDER_WAY


def _candidate_summary(**overrides) -> dict:
    base = {
        "fingerprint": "fp-123",
        "trims_db": {"woofer": -3.1, "tweeter": 0.0},
        "alignment": {"delay_us": 250.0, "delay_role": "woofer", "polarity": "invert"},
        "alignment_confidence": 0.82,
        "predicted_ripple_db": 1.4,
    }
    base.update(overrides)
    return base


def test_verify_phase_screen():
    env = build_crossover_envelope_v2(_status(phase="verify", applied=True))
    assert env["screen"] == "verify"
    assert env["next_action"] is None
    assert _step_statuses(env)["verify"] == "active"


def test_volume_recovery_keys_on_needs_recovery_not_unresolved():
    """A crash-hydrated active plan surfaces NO unresolved payload but still
    needs draining — the screen must key on needs_recovery alone."""
    env = build_crossover_envelope_v2(_status(phase="check", needs_recovery=True))
    assert env["screen"] == "volume_recovery"
    assert env["next_action"]["endpoint"] == "/sound/speaker/crossover/recover-volume"
    # And needs_recovery false ⇒ no recovery screen even with a phase set.
    env = build_crossover_envelope_v2(_status(phase="check", needs_recovery=False))
    assert env["screen"] == "awaiting_plan"


@pytest.mark.parametrize("code", [REASON_LOCATE_FAILED, REASON_VERIFY_INCONCLUSIVE])
def test_a_failure_renders_its_no_evidence_copy_over_an_old_evidence_record(code):
    """A state file from an older build may still carry the retired evidence keys."""
    env = build_crossover_envelope_v2(_status(
        applied=False,
        failure={"code": code, "pilot_heard": True},
        verify={"gate": {"reflection_measured": True}},
    ))

    assert env["verdict_text"] == REASON_REGISTRY[code].message


def test_no_registry_sentence_names_undo():
    for code, spec in REASON_REGISTRY.items():
        for text in (
            spec.message, spec.banner,
            reason_message(code, spec),
        ):
            assert "undo" not in text.lower(), (code, text)


@pytest.mark.parametrize("code,template", [
    (code, spec.template) for code, spec in REASON_REGISTRY.items()
])
def test_every_registry_code_renders_without_error(code, template):
    env = build_crossover_envelope_v2(_status(phase="measure", failure={"code": code}))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION
    assert env["screen"]
    assert env["verdict_text"]


@pytest.mark.parametrize("phase,screen", [
    ("measure", "measure"),
    ("verify", "verify"),
    ("cloud_verify", "verify"),
])
def test_live_session_phase_screen_is_untouched(phase, screen):
    """No regression to the live path: a session the household is inside
    renders the screen it renders today."""
    env = build_crossover_envelope_v2(_status(
        phase=phase, session_id="cap_live", applied=phase.endswith("verify"), tier="full",
    ))
    assert env["screen"] == screen
    assert env["nudges"] == []


def test_every_in_flow_action_the_envelope_mints_is_machine_actionable():
    """One flow, N drivers: a decision must be performable without a browser.

    The invariant, stated so it can be checked rather than intended: an action
    whose ``href`` points back INTO this flow is a decision, and a decision has
    to carry an ``endpoint`` a driver can POST. An action pointing at another
    subsystem (``/sound/``, ``/sound/speaker/``) is a navigation and is
    exempt — no endpoint here could perform it, and minting a fake one would be
    worse than the link.

    This is the shape #2641 was: ``review_decline``'s href was
    ``/sound/speaker/crossover/`` — in-flow — with no endpoint, so it looked like
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
            if not href.startswith("/sound/speaker/crossover"):
                continue
            if not action.get("endpoint"):
                offenders.append((name, str(action.get("id") or "?")))

    assert offenders == [], (
        "an in-flow action with no endpoint is a decision a driver cannot "
        "take, and a button a household clicks to no effect", offenders,
    )
