# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reload-safe Stop for the /sound/ combined (summed) crossover test.

Pins the contract behind the 2026-07-06 jts3 incident: a combined test that is
still looping must be reported on the commissioning view so a freshly loaded (or
reloaded) /sound/ page can render a Stop control, instead of showing "Play
combined test" while the test audio keeps looping with no way to stop it. The
client-side render half is pinned in tests/js/sound_profile_harness.mjs.
"""

from __future__ import annotations

import asyncio
import logging
import time

import jasper.web.sound_setup as sound_setup


class _FakeProc:
    """Minimal Popen stand-in: poll() -> None while alive, 0 once exited."""

    def __init__(self, *, alive: bool = True) -> None:
        self._alive = alive

    def poll(self) -> int | None:
        return None if self._alive else 0


def _live_session(**overrides):
    now = time.monotonic()
    session = {
        "playback_id": "summed-playback-1",
        "process": _FakeProc(alive=True),
        "speaker_group_id": "main",
        "level_dbfs": -18.0,
        "started_monotonic": now,
        "progress_monotonic": now,
        "stop_reason": None,
    }
    session.update(overrides)
    return session


def _prep_session(progress, **overrides):
    """A process=None (preparing OR leaked) session with an explicit heartbeat."""
    session = {
        "playback_id": "summed-playback-1",
        "process": None,
        "speaker_group_id": "main",
        "level_dbfs": None,
        "started_monotonic": progress,
        "progress_monotonic": progress,
        "stop_reason": None,
    }
    session.update(overrides)
    return session


def test_snapshot_reports_active_while_playing(monkeypatch):
    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", _live_session())
    assert sound_setup._active_summed_test_snapshot() == {
        "active": True,
        "playback_id": "summed-playback-1",
        "speaker_group_id": "main",
        "level_dbfs": -18.0,
    }


def test_snapshot_active_while_preparing(monkeypatch):
    # process is None during config load / fanin-gate setup, before the first
    # aplay is spawned — still "active" so Stop stays reachable in that window.
    monkeypatch.setattr(
        sound_setup, "_SUMMED_TEST_TONE_SESSION", _live_session(process=None)
    )
    assert sound_setup._active_summed_test_snapshot()["active"] is True


def test_snapshot_idle_when_no_session(monkeypatch):
    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", None)
    assert sound_setup._active_summed_test_snapshot() == {"active": False}


def test_snapshot_inactive_once_stop_requested(monkeypatch):
    # As soon as a stop is requested the snapshot flips inactive, so the very
    # next view refresh returns the card to Play instead of flickering Stop.
    monkeypatch.setattr(
        sound_setup,
        "_SUMMED_TEST_TONE_SESSION",
        _live_session(stop_reason="operator_stop"),
    )
    assert sound_setup._active_summed_test_snapshot() == {"active": False}


def test_snapshot_inactive_when_process_exited(monkeypatch):
    monkeypatch.setattr(
        sound_setup,
        "_SUMMED_TEST_TONE_SESSION",
        _live_session(process=_FakeProc(alive=False)),
    )
    assert sound_setup._active_summed_test_snapshot() == {"active": False}


def test_attach_marks_only_the_matching_group():
    view = {
        "combined_groups": [
            {"group_id": "main", "label": "Main"},
            {"group_id": "sub", "label": "Sub"},
        ],
    }
    snapshot = {"active": True, "speaker_group_id": "main"}
    sound_setup._attach_active_summed_test(view, snapshot)
    assert view["active_summed_test"] is snapshot
    assert view["combined_groups"][0]["summed_test_active"] is True
    assert "summed_test_active" not in view["combined_groups"][1]


def test_attach_marks_all_groups_when_group_id_missing():
    view = {"combined_groups": [{"group_id": "main"}, {"group_id": "sub"}]}
    sound_setup._attach_active_summed_test(view, {"active": True, "speaker_group_id": ""})
    assert view["combined_groups"][0]["summed_test_active"] is True
    assert view["combined_groups"][1]["summed_test_active"] is True


def test_attach_adds_no_group_flag_when_idle():
    view = {"combined_groups": [{"group_id": "main"}]}
    sound_setup._attach_active_summed_test(view, {"active": False})
    assert view["active_summed_test"] == {"active": False}
    assert "summed_test_active" not in view["combined_groups"][0]


def test_commissioning_view_payload_surfaces_active_test(monkeypatch):
    """The GET the client polls must report the live test end-to-end."""

    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", _live_session())

    async def _fake_commission_state(*, camilla_factory):
        return {}

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_commission_state_payload",
        _fake_commission_state,
    )

    def _fake_load_view(*, commission):
        return {
            "status": "needs_combined_check",
            "next_action": {"id": "start_combined_test"},
            "combined_groups": [{"group_id": "main", "label": "Main speaker"}],
        }

    monkeypatch.setattr(
        "jasper.active_speaker.commissioning_coordinator.load_commissioning_view",
        _fake_load_view,
    )

    view = asyncio.run(
        sound_setup._active_speaker_commissioning_view_payload(
            camilla_factory=lambda: None
        )
    )
    assert view["active_summed_test"]["active"] is True
    assert view["active_summed_test"]["speaker_group_id"] == "main"
    assert view["combined_groups"][0]["summed_test_active"] is True


def test_commissioning_view_payload_idle_when_no_test(monkeypatch):
    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", None)

    async def _fake_commission_state(*, camilla_factory):
        return {}

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_commission_state_payload",
        _fake_commission_state,
    )

    def _fake_load_view(*, commission):
        return {
            "status": "needs_combined_check",
            "next_action": {"id": "start_combined_test"},
            "combined_groups": [{"group_id": "main"}],
        }

    monkeypatch.setattr(
        "jasper.active_speaker.commissioning_coordinator.load_commissioning_view",
        _fake_load_view,
    )

    view = asyncio.run(
        sound_setup._active_speaker_commissioning_view_payload(
            camilla_factory=lambda: None
        )
    )
    assert view["active_summed_test"] == {"active": False}
    assert "summed_test_active" not in view["combined_groups"][0]


# --- Leaked-session recovery (the process=None wedge) ---------------------
#
# _summed_test_session_active is the single source of truth shared by BOTH the
# reload-safe view snapshot and the start guard in
# _active_speaker_play_summed_commission_tone. Pinning it here pins both: a
# leaked process=None session (owning request died before the try/finally
# teardown) ages out of "active", so it neither shows a phantom Stop nor wedges
# every retry with summed_test_already_active until jasper-web is restarted.


def test_session_active_true_while_process_alive():
    assert sound_setup._summed_test_session_active(_live_session()) is True


def test_session_active_true_when_preparing_with_fresh_heartbeat():
    assert (
        sound_setup._summed_test_session_active(_prep_session(1000.0), now=1000.5)
        is True
    )


def test_session_active_false_when_leaked_heartbeat_stale():
    stale = sound_setup.SUMMED_TEST_SESSION_STALE_SECONDS + 5.0
    assert (
        sound_setup._summed_test_session_active(
            _prep_session(1000.0), now=1000.0 + stale
        )
        is False
    )


def test_session_active_false_when_stop_requested():
    assert (
        sound_setup._summed_test_session_active(
            _live_session(stop_reason="operator_stop")
        )
        is False
    )


def test_session_active_false_when_none():
    assert sound_setup._summed_test_session_active(None) is False


def test_session_active_false_when_process_exited():
    assert (
        sound_setup._summed_test_session_active(
            _live_session(process=_FakeProc(alive=False))
        )
        is False
    )


def test_session_active_falls_back_to_started_monotonic():
    # A session created the instant before its first heartbeat carries only
    # started_monotonic; a fresh one still reads as active.
    session = _prep_session(1000.0)
    del session["progress_monotonic"]
    assert sound_setup._summed_test_session_active(session, now=1000.5) is True


def test_session_active_false_when_heartbeat_unparseable():
    session = _prep_session(1000.0, progress_monotonic=None, started_monotonic=None)
    assert sound_setup._summed_test_session_active(session, now=1000.5) is False


def test_snapshot_inactive_for_leaked_stale_session(monkeypatch):
    # End-to-end through the reload-safe snapshot: a heartbeat far in the past
    # (relative to real time.monotonic()) is treated as leaked, so the view
    # shows no phantom Stop and the shared guard admits a fresh test.
    monkeypatch.setattr(
        sound_setup, "_SUMMED_TEST_TONE_SESSION", _prep_session(-10_000.0)
    )
    assert sound_setup._active_summed_test_snapshot() == {"active": False}


# --- The start guard actually uses the shared predicate ---------------------
#
# The guard in _active_speaker_play_summed_commission_tone is a one-line
# `if _summed_test_session_active(...)`, but pinning it end-to-end keeps it wired
# to the shared helper (not silently reverted to the old process-is-None logic)
# and exercises the reclaim log (N1). We stub the artifact backend to complete
# and make the stimulus raise, so the request returns right after the guard +
# session claim with no camilla I/O.


def _fake_completed_artifact(*_args, **_kwargs):
    return {"status": "completed", "playback_id": "pb-x", "tone": {"level_dbfs": -20.0}}


def _run_summed_play():
    return asyncio.run(
        sound_setup._active_speaker_play_summed_commission_tone(
            {"tone": {"level_dbfs": -20.0}},
            safe_session={},
            topology=None,
            speaker_group_id="main",
            startup_gate_calibration_level=None,
            preset=None,
            crossover_preview=None,
            camilla_factory=lambda: None,
        )
    )


def _issue_codes(result):
    return {i.get("code") for i in result.get("issues", []) if isinstance(i, dict)}


def test_guard_admits_start_over_leaked_stale_session(monkeypatch, caplog):
    monkeypatch.setattr(
        "jasper.active_speaker.playback.start_tone_playback", _fake_completed_artifact
    )

    def _boom():
        raise RuntimeError("stimulus-stop")

    monkeypatch.setattr(sound_setup, "_combined_speech_stimulus_wav_path", _boom)
    monkeypatch.setattr(
        sound_setup, "_SUMMED_TEST_TONE_SESSION", _prep_session(-10_000.0)
    )
    with caplog.at_level(logging.INFO):
        result = _run_summed_play()
    # Guard admitted the start (did not wedge on the leaked prior session) ...
    assert "summed_test_already_active" not in _issue_codes(result)
    # ... and surfaced the reclamation for diagnosis.
    assert "action=reclaim_prior_session reason=stale" in caplog.text


def test_guard_blocks_start_over_live_session(monkeypatch):
    monkeypatch.setattr(
        "jasper.active_speaker.playback.start_tone_playback", _fake_completed_artifact
    )
    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", _live_session())
    result = _run_summed_play()
    assert "summed_test_already_active" in _issue_codes(result)
