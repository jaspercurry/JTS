# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Reset the measurement journey while preserving the speaker setup."""

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

import pytest

from pathlib import Path

from jasper.web import correction_crossover_backend as backend
from jasper.web import correction_crossover_flow as flow

_JOURNEY_ENVS = {
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

    result = backend.reset_measurement_journey()

    assert result["status"] == "cleared"
    assert sorted(result["cleared_ids"]) == [
        "commission_load",
        "commission_ramp",
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

    result = backend.reset_measurement_journey()

    assert result["status"] == "cleared"  # already-absent is not an error
    assert "measurements" not in result["cleared_ids"]
    assert result["missing_ids"] == ["measurements"]
    assert result["error_ids"] == []


def test_handle_reset_returns_fresh_envelope_with_honest_reset_summary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        backend,
        "reset_measurement_journey",
        lambda: {
            "status": "partial",
            "cleared_ids": ["commission_load"],
            "missing_ids": ["staged_config"],
            "error_ids": ["measurements"],
            "kept_ids": ["design_draft", "baseline_profile", "startup_load"],
        },
    )
    monkeypatch.setattr(flow, "handle_status", lambda *, capture=None: ({}, 200))
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
    # The honest outcome is surfaced verbatim, including the partial status
    # and the errored file — the page branches on status != "cleared".
    assert payload["reset"] == {
        "status": "partial",
        "cleared": ["commission_load"],
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

    v2state.set_state_path_for_tests(tmp_path / "v2_state.json")
    try:
        v2state.save_v2_state({
            "session_id": "cap_x",
            "accepted_phases": ["check", "measure"],
            "applied": False,
            "candidate": {"fingerprint": "fp"},
            "failure": {"code": "capture_timeout"},
        })
        assert v2state.load_v2_state() is not None
        _reset_scaffold(monkeypatch)

        payload, status = flow.handle_reset()

        assert status == 200
        # The durable v2 state is gone — a fresh journey starts at the
        # microphone check, not the stale failure screen.
        assert v2state.load_v2_state() is None
    finally:
        v2state.set_state_path_for_tests(None)


def test_handle_reset_while_applied_keeps_undo_pointers(monkeypatch, tmp_path):
    """Gate ruling (W6.10 should-fix): Start-over while a candidate is APPLIED
    must preserve `applied` + `previous_candidate_fingerprint` — the way
    back's pointer — while clearing the journey fields so the envelope serves
    the clean start screen. A full clear here would strand the household on
    the applied graph with no way back."""
    from jasper.web import correction_crossover_v2_status as v2status

    v2state.set_state_path_for_tests(tmp_path / "v2_state.json")
    try:
        v2state.save_v2_state({
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
        state = v2state.load_v2_state()
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
        v2state.set_state_path_for_tests(None)


@pytest.mark.parametrize("terminal", ["complete", "stopped", "failed"])
def test_reset_clears_the_terminal_capture_from_the_page(monkeypatch, terminal):
    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
    from jasper.web import correction_capture, correction_handlers

    _reset_scaffold(monkeypatch)
    monkeypatch.setattr(v2state, "reset_v2_journey_state", lambda: None)
    monkeypatch.setattr(correction_capture, "_pending_capture", None)
    monkeypatch.setattr(correction_capture, "_capture_slot", {
        "kind": "crossover_v2:session", "status": terminal,
    })
    monkeypatch.setattr(flow, "_build_envelope_logged", build_crossover_envelope_v2)
    monkeypatch.setattr(flow, "handle_status", lambda *, capture=None: ({
        "active": True, "setup": {"active": True, "status": "ready"}, "capture": capture,
    }, 200))
    envelope, status = correction_handlers._handle_crossover_reset()
    assert status == 200
    assert envelope["screen"] == "awaiting_plan"
    assert correction_capture._get_capture_slot_for("crossover_v2:") is None
