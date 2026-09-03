# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The crossover flow's two "leave this where it is" routes.

``POST /crossover/reset`` — scoped "start over". Pins the KEEP/CLEAR split for
the in-flow reset that restarts the guided measurement journey without losing
driver research or disturbing whatever audio graph is currently applied/loaded
— see ``jasper.active_speaker.reset.clear_active_speaker_measurement_journey``.

``POST /crossover/v2/decline`` — the review screen's "Keep current sound"
(#2641). It shares this file rather than getting its own because it is the
same question one screen over (what a household keeps when they decline to go
further) and it is tested through the same ``flow`` handler and the same
durable-state scaffold. Its own section pins both halves: the write records
the decision without touching the speaker or the candidate, and the read stops
serving a decision screen the household has already answered — bound to the
candidate they answered, so a newer measurement brings the review back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.web import correction_crossover_backend as backend
from jasper.web import correction_crossover_flow as flow

_JOURNEY_ENVS = {
    "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE": "preview.json",
    "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH": "staged.json",
    "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE": "path-safety.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE": "commission-load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE": "commission-ramp.json",
    "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE": "measurements.json",
}
_KEPT_ENVS = {
    "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE": "design.json",
    "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE": "startup-load.json",
    "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE": "baseline.json",
}


def _seed(monkeypatch, tmp_path: Path) -> None:
    for env_name, filename in {**_JOURNEY_ENVS, **_KEPT_ENVS}.items():
        path = tmp_path / filename
        path.write_text('{"seed": true}\n', encoding="utf-8")
        monkeypatch.setenv(env_name, str(path))


def test_reset_measurement_journey_clears_journey_keeps_driver_and_applied_state(
    monkeypatch, tmp_path: Path,
) -> None:
    _seed(monkeypatch, tmp_path)
    fresh_lease = backend.CrossoverLevelLease()
    monkeypatch.setattr(backend, "level_lease", lambda: fresh_lease)

    result = backend.reset_measurement_journey()

    assert result["status"] == "cleared"
    # cleared_ids reflects the ACTUAL unlinks (all six existed), not the
    # static intent; missing/errors are empty on a clean clear.
    assert sorted(result["cleared_ids"]) == [
        "commission_load",
        "commission_ramp",
        "crossover_preview",
        "measurements",
        "path_safety",
        "staged_config",
    ]
    assert result["missing_ids"] == []
    assert result["error_ids"] == []
    assert sorted(result["kept_ids"]) == [
        "baseline_profile",
        "design_draft",
        "startup_load",
    ]
    for filename in _JOURNEY_ENVS.values():
        assert not (tmp_path / filename).exists()
    for filename in _KEPT_ENVS.values():
        assert (tmp_path / filename).exists()


def test_reset_measurement_journey_reports_actual_outcome_not_static_intent(
    monkeypatch, tmp_path: Path,
) -> None:
    """An already-absent journey file lands in missing_ids, not cleared_ids —
    the summary is the real outcome, so a partial state can never be painted
    as a full green clear (adversarial-review N1)."""
    _seed(monkeypatch, tmp_path)
    # Remove one journey file before the reset so it is already absent.
    (tmp_path / _JOURNEY_ENVS["JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE"]).unlink()
    fresh_lease = backend.CrossoverLevelLease()
    monkeypatch.setattr(backend, "level_lease", lambda: fresh_lease)

    result = backend.reset_measurement_journey()

    assert result["status"] == "cleared"  # already-absent is not an error
    assert "measurements" not in result["cleared_ids"]
    assert result["missing_ids"] == ["measurements"]
    assert result["error_ids"] == []


def test_reset_measurement_journey_refuses_when_volume_safety_unresolved(
    monkeypatch, tmp_path: Path,
) -> None:
    _seed(monkeypatch, tmp_path)
    fresh_lease = backend.CrossoverLevelLease()
    fresh_lease._volume_safety_state = {"status": "unresolved"}
    monkeypatch.setattr(backend, "level_lease", lambda: fresh_lease)

    with pytest.raises(backend.MeasurementJourneyResetRefused) as exc_info:
        backend.reset_measurement_journey()

    assert exc_info.value.reason == "crossover_volume_safety_unresolved"
    # Fail-closed: nothing was cleared.
    for filename in _JOURNEY_ENVS.values():
        assert (tmp_path / filename).exists()


def test_reset_measurement_journey_refuses_while_level_match_still_running(
    monkeypatch, tmp_path: Path,
) -> None:
    _seed(monkeypatch, tmp_path)
    fresh_lease = backend.CrossoverLevelLease()
    fresh_lease._running = object()  # sentinel: a level match is in flight
    monkeypatch.setattr(backend, "level_lease", lambda: fresh_lease)

    with pytest.raises(backend.MeasurementJourneyResetRefused) as exc_info:
        backend.reset_measurement_journey()

    assert exc_info.value.reason == "measurement_in_progress"
    for filename in _JOURNEY_ENVS.values():
        assert (tmp_path / filename).exists()


def test_handle_reset_maps_refusal_to_409(monkeypatch) -> None:
    def fake_reset() -> dict:
        raise backend.MeasurementJourneyResetRefused(
            "a crossover measurement is still stopping; try Start over again "
            "in a moment",
            reason="measurement_in_progress",
        )

    monkeypatch.setattr(backend, "reset_measurement_journey", fake_reset)

    payload, status = flow.handle_reset()

    assert status == 409
    assert payload["status"] == "refused"
    assert payload["reason"] == "measurement_in_progress"


def test_handle_reset_returns_fresh_envelope_with_honest_reset_summary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        backend,
        "reset_measurement_journey",
        lambda: {
            "status": "partial",
            "cleared_ids": ["crossover_preview"],
            "missing_ids": ["staged_config"],
            "error_ids": ["measurements"],
            "kept_ids": ["design_draft", "baseline_profile", "startup_load"],
        },
    )
    monkeypatch.setattr(flow, "handle_status", lambda *, capture=None: ({}, 200))
    monkeypatch.setattr(flow, "_active_group_member", lambda: False)
    monkeypatch.setattr(
        "jasper.web.correction_crossover_flow._build_envelope_logged",
        lambda status: {
            "screen": "start",
            "active": True,
            "steps": [],
            "nudges": [],
        },
    )

    payload, status = flow.handle_reset()

    assert status == 200
    assert payload["screen"] == "start"
    assert payload["grouping_member"] is False
    # The honest outcome is surfaced verbatim, including the partial status
    # and the errored file — the page branches on status != "cleared".
    assert payload["reset"] == {
        "status": "partial",
        "cleared": ["crossover_preview"],
        "missing": ["staged_config"],
        "errors": ["measurements"],
        "kept": ["design_draft", "baseline_profile", "startup_load"],
    }


def _reset_scaffold(monkeypatch):
    monkeypatch.setattr(backend, "reset_measurement_journey", lambda: {
        "status": "cleared", "cleared_ids": [], "missing_ids": [],
        "error_ids": [], "kept_ids": [],
    })
    monkeypatch.setattr(flow, "handle_status", lambda *, capture=None: ({}, 200))
    monkeypatch.setattr(flow, "_active_group_member", lambda: False)
    monkeypatch.setattr(
        "jasper.web.correction_crossover_flow._build_envelope_logged",
        lambda status: {"screen": "start", "active": True, "steps": [], "nudges": []},
    )


def test_handle_reset_clears_stale_v2_state_under_v2_flow(monkeypatch, tmp_path):
    """W6.10 fold-in: Start-over must clear the durable v2 conductor state so the
    next envelope serves the clean start screen. Without this, a stale
    candidate/verify/failure re-rendered "Ready to start again" with stale
    verify-fail actions and no start button (round-1 finding #4). NOT-applied
    ⇒ the clear is total (nothing worth preserving)."""
    from jasper.web import correction_crossover_v2 as v2

    v2.set_state_path_for_tests(tmp_path / "v2_state.json")
    try:
        v2.save_v2_state({
            "session_id": "cap_x",
            "accepted_phases": ["check", "measure"],
            "applied": False,
            "candidate": {"fingerprint": "fp"},
            "failure": {"code": "relay_timeout"},
        })
        assert v2.load_v2_state() is not None
        _reset_scaffold(monkeypatch)

        payload, status = flow.handle_reset()

        assert status == 200
        # The durable v2 state is gone — a fresh journey starts at the
        # microphone check, not the stale failure screen.
        assert v2.load_v2_state() is None
    finally:
        v2.set_state_path_for_tests(None)


def test_handle_reset_while_applied_keeps_undo_pointers(monkeypatch, tmp_path):
    """Gate ruling (W6.10 should-fix): Start-over while a candidate is APPLIED
    must preserve `applied` + `previous_candidate_fingerprint` — the way
    back's pointer — while clearing the journey fields so the envelope serves
    the clean start screen. A full clear here would strand the household on
    the applied graph with no way back."""
    from jasper.web import correction_crossover_v2 as v2
    from jasper.web import correction_crossover_v2_status as v2status

    v2.set_state_path_for_tests(tmp_path / "v2_state.json")
    try:
        v2.save_v2_state({
            "session_id": "cap_x",
            "accepted_phases": ["check", "measure"],
            "applied": True,
            "candidate": {"fingerprint": "fp-new"},
            "verify": {"outcome": "fail"},
            "failure": {"code": "verify_out_of_tolerance"},
            "gain_plan_db": {"woofer": -6.0},
            "previous_candidate_fingerprint": "fp-prior",
        })
        _reset_scaffold(monkeypatch)

        payload, status = flow.handle_reset()

        assert status == 200
        state = v2.load_v2_state()
        assert state is not None
        # The way back's pointers preserved…
        assert state["applied"] is True
        assert state["previous_candidate_fingerprint"] == "fp-prior"
        # …journey fields cleared, so the envelope lands on the clean start
        # screen (phase derives to the microphone check).
        assert state["accepted_phases"] == []
        assert state["candidate"] is None
        assert state["verify"] is None
        assert state["failure"] is None
        assert state["gain_plan_db"] is None
        assert state["session_id"] is None
        block = v2status.crossover_v2_status_block()
        assert block is not None and block["phase"] == "check"
    finally:
        v2.set_state_path_for_tests(None)


def test_handle_reset_refusal_leaves_v2_state_intact(monkeypatch, tmp_path):
    """A refused reset (volume-safety unresolved / measurement still stopping)
    must NOT clear the v2 state — nothing was reset, so nothing is lost."""
    from jasper.web import correction_crossover_v2 as v2

    v2.set_state_path_for_tests(tmp_path / "v2_state.json")
    try:
        v2.save_v2_state({"session_id": "cap_x", "accepted_phases": ["microphone_check"]})

        def _refuse():
            raise backend.MeasurementJourneyResetRefused(
                "still stopping", reason="measurement_in_progress",
            )

        monkeypatch.setattr(backend, "reset_measurement_journey", _refuse)

        payload, status = flow.handle_reset()

        assert status == 409
        assert v2.load_v2_state() is not None  # untouched on refusal
    finally:
        v2.set_state_path_for_tests(None)


def test_handle_envelope_carries_grouping_member_flag(monkeypatch) -> None:
    """The polled envelope carries the grouping-member flag the grouping-aware
    Start-over confirm copy reads (adversarial-review S1b)."""
    monkeypatch.setattr(flow, "handle_status", lambda *, capture=None: ({}, 200))
    monkeypatch.setattr(flow, "_active_group_member", lambda: True)
    monkeypatch.setattr(
        "jasper.web.correction_crossover_flow._build_envelope_logged",
        lambda status: {"screen": "start", "active": True, "steps": [], "nudges": []},
    )

    payload, status = flow.handle_envelope()

    assert status == 200
    assert payload["grouping_member"] is True


def test_active_group_member_reads_grouping_config(monkeypatch) -> None:
    """_active_group_member is a thin read of the declared grouping config:
    True for an active leader OR bonded follower, False for solo. load_config
    is total (never raises), so the only failure the helper guards is an
    ImportError of the multiroom module, which fails open to the solo copy."""
    import jasper.multiroom.config as grouping_config

    monkeypatch.setattr(grouping_config, "load_config", lambda: object())
    monkeypatch.setattr(grouping_config, "is_bonded_follower", lambda cfg: False)

    monkeypatch.setattr(grouping_config, "is_active_leader", lambda cfg: True)
    assert flow._active_group_member() is True

    monkeypatch.setattr(grouping_config, "is_active_leader", lambda cfg: False)
    assert flow._active_group_member() is False

    monkeypatch.setattr(grouping_config, "is_bonded_follower", lambda cfg: True)
    assert flow._active_group_member() is True

    # Fail-open to the solo copy if the multiroom module can't be imported
    # (the `from jasper.multiroom.config import ...` line is the only raiser).
    monkeypatch.setattr(grouping_config, "is_bonded_follower", lambda cfg: False)
    monkeypatch.delattr(grouping_config, "is_active_leader")
    assert flow._active_group_member() is False


# --------------------------------------------------------------------------- #
# "Keep current sound" — POST /crossover/v2/decline (#2641)
# --------------------------------------------------------------------------- #


def _declinable_state(v2, tmp_path, **overrides):
    state = {
        "session_id": "cap_x",
        "accepted_phases": ["check", "measure"],
        "applied": False,
        "candidate": {"fingerprint": "fp-current"},
    }
    state.update(overrides)
    v2.set_state_path_for_tests(tmp_path / "v2_state.json")
    v2.save_v2_state(state)
    return state


def test_a_decline_is_recorded_and_a_no_show_is_distinguishable(
    monkeypatch, tmp_path,
):
    """#2641's load-bearing half: the record must tell the two apart.

    Before this the review's Keep button performed no request at all, so
    "the household declined" and "the household never looked" were the same
    state on disk — and a series that offers another bite has no way to know
    the answer was no. The distinction is an ABSENT key versus a present one,
    not a flag defaulting to False, so a state file written by an older build
    reads as "never looked" rather than as a decline nobody made.
    """
    from jasper.web import correction_crossover_v2 as v2

    try:
        _declinable_state(v2, tmp_path)
        _reset_scaffold(monkeypatch)
        # Never looked.
        assert "review_decision" not in (v2.load_v2_state() or {})

        payload, status = flow.handle_v2_decline(
            {"expected_candidate_fingerprint": "fp-current"}
        )

        assert status == 200
        # The resting screen, in one round trip — not a poll for it.
        assert payload["screen"] == "start"
        decision = (v2.load_v2_state() or {})["review_decision"]
        assert decision["decision"] == v2.REVIEW_DECISION_DECLINED
        assert decision["candidate_fingerprint"] == "fp-current"
    finally:
        v2.set_state_path_for_tests(None)


def test_a_decline_does_not_touch_the_candidate_or_the_speaker(
    monkeypatch, tmp_path,
):
    """The review screen's own contract: declining changes nothing.

    Deleting the proposal on decline would make an accidental tap cost ten
    captures to undo, and the applied flag is what the speaker is playing.
    Both must read back exactly as they were.
    """
    from jasper.web import correction_crossover_v2 as v2

    try:
        _declinable_state(v2, tmp_path, applied=True)
        _reset_scaffold(monkeypatch)

        _payload, status = flow.handle_v2_decline(
            {"expected_candidate_fingerprint": "fp-current"}
        )

        assert status == 200
        state = v2.load_v2_state() or {}
        assert state["candidate"] == {"fingerprint": "fp-current"}
        assert state["applied"] is True
        assert state["accepted_phases"] == ["check", "measure"]
    finally:
        v2.set_state_path_for_tests(None)


def test_a_decline_against_a_superseded_candidate_is_refused(
    monkeypatch, tmp_path,
):
    """The same guard ``/v2/apply`` carries, and for the same reason.

    A newer measurement can replace the proposal while the screen is open. A
    decline recorded against the old one would close a review the household
    never saw, and the next screen would silently stop offering the bite the
    new candidate earned.
    """
    from jasper.web import correction_crossover_v2 as v2

    try:
        _declinable_state(v2, tmp_path)
        _reset_scaffold(monkeypatch)

        payload, status = flow.handle_v2_decline(
            {"expected_candidate_fingerprint": "fp-stale"}
        )

        assert status == 409
        assert payload["ok"] is False
        assert "review_decision" not in (v2.load_v2_state() or {})
    finally:
        v2.set_state_path_for_tests(None)


def test_a_decline_with_no_candidate_to_name_is_still_a_decline(
    monkeypatch, tmp_path,
):
    """The review screen mints the button even when there is nothing to
    propose ("JTS measured your speaker but has no correction to propose"),
    and "keep what you have" is a real answer to that screen too. An empty
    expectation therefore passes the guard rather than tripping it."""
    from jasper.web import correction_crossover_v2 as v2

    try:
        _declinable_state(v2, tmp_path, candidate=None)
        _reset_scaffold(monkeypatch)

        _payload, status = flow.handle_v2_decline(
            {"expected_candidate_fingerprint": ""}
        )

        assert status == 200
        decision = (v2.load_v2_state() or {})["review_decision"]
        assert decision["decision"] == v2.REVIEW_DECISION_DECLINED
        assert decision["candidate_fingerprint"] == ""
    finally:
        v2.set_state_path_for_tests(None)


def test_the_decline_route_is_reachable_from_the_endpoint_the_envelope_mints():
    """The mint and the router have to agree on one string.

    An envelope action naming an endpoint the server does not route is the
    inert button all over again, one layer down — and nothing else compares
    the two. Read off the ENVELOPE rather than from a copy of the literal, so
    a rename on either side fails here.
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )
    from jasper.web import correction_setup

    status = {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "review",
            "candidate": {"fingerprint": "fp", "linearization": {}},
        },
    }
    decline = next(
        a for a in build_crossover_envelope_v2(status)["alternate_actions"]
        if a["id"] == "review_decline"
    )

    # The correction router mounts its paths under /correction.
    assert decline["endpoint"].startswith("/correction/")
    routed = decline["endpoint"][len("/correction"):]
    assert routed in correction_setup._POST_ROUTES


def test_a_declined_review_stops_being_served_and_a_new_one_is_not(tmp_path):
    """#2641's acknowledgement half: the screen closes, and only for THIS one.

    Recording the decline is worthless if the household is handed the same
    decision screen on the next poll — that was the observed defect (five
    clicks, five reloads, same screen). And a decline that outlived its own
    candidate would be worse than the bug: a later measurement's proposal
    would never be offered, because a "no" the household gave to a different
    question was still on file.
    """
    from jasper.web import correction_crossover_v2 as v2
    from jasper.web import correction_crossover_v2_status as v2status

    v2.set_state_path_for_tests(tmp_path / "v2_state.json")
    try:
        v2.save_v2_state({
            "session_id": "cap_x",
            "accepted_phases": ["check", "measure"],
            "session_phases": ["check", "measure"],
            "applied": False,
            "candidate": {"fingerprint": "fp-1"},
        })
        # Before: the review interlude.
        assert v2status._phase_from_state(v2.load_v2_state()) == "review"

        v2.observe_review_decline("fp-1")

        # After: the journey's resting screen, not the decision again.
        assert v2status._phase_from_state(v2.load_v2_state()) == "check"

        # A NEWER measurement mints a different candidate, and the decline
        # does not cover it.
        state = v2.load_v2_state()
        state["candidate"] = {"fingerprint": "fp-2"}
        v2.save_v2_state(state)
        assert v2status._phase_from_state(v2.load_v2_state()) == "review"
    finally:
        v2.set_state_path_for_tests(None)


def test_a_state_that_was_never_declined_reads_as_never_looked(tmp_path):
    """The control the distinction rests on: absence is not a decline.

    ``review_declined`` must not answer True for a state written by a build
    that predates the key, for a malformed record, or for a decline naming
    some other proposal — each of those would close a review nobody answered.
    """
    from jasper.web import correction_crossover_v2 as v2

    base = {"candidate": {"fingerprint": "fp-1"}}
    assert v2.review_declined(None) is False
    assert v2.review_declined(base) is False
    assert v2.review_declined({**base, "review_decision": "yes"}) is False
    assert v2.review_declined({
        **base, "review_decision": {"decision": "declined",
                                    "candidate_fingerprint": "fp-other"},
    }) is False
    assert v2.review_declined({
        **base, "review_decision": {"decision": "somethingelse",
                                    "candidate_fingerprint": "fp-1"},
    }) is False
    # …and the one shape that IS a decline.
    assert v2.review_declined({
        **base, "review_decision": {"decision": "declined",
                                    "candidate_fingerprint": "fp-1"},
    }) is True
