"""Corpus test-mode crash-recovery for the wake-corpus recorder.

Entering corpus test mode stops jasper-voice so the UDP ports are free
to record. If the operator opens the recorder and then just closes the
tab without exiting test mode, jasper-voice would otherwise stay stopped
indefinitely (the socket-activated web service idle-exits after 10 min),
leaving the speaker permanently deaf — a violation of the project's
"reasonable operator actions must self-recover" rule.

These tests cover `RecordingBackend._maybe_recover_stale_test_mode()`,
the bounded self-heal that runs on backend startup (which the socket
re-runs on the next /wake-corpus/ request after an idle exit):

  - a stale test-mode marker triggers recovery (exit + voice restart),
  - a fresh marker is left alone (operator's tab is still open),
  - an active/recovered session is NOT torn down (voice must stay
    stopped while a corpus session is live).

All systemctl / subprocess seams are mocked, so this is hardware-free.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from jasper.web import wake_corpus_setup


def _make_backend(out: Path) -> wake_corpus_setup.RecordingBackend:
    """An unstarted backend rooted in a tmp dir.

    `_maybe_recover_stale_test_mode()` only reads markers + the in-memory
    session/recording fields (both set in __init__), so it needs neither
    the asyncio loop nor real systemctl. Calling it directly isolates the
    recovery decision from thread + bridge-env machinery.
    """
    return wake_corpus_setup.RecordingBackend(output_dir=out)


def _write_test_mode_marker(out: Path, *, age_sec: float) -> Path:
    md_dir = out / "metadata"
    md_dir.mkdir(parents=True, exist_ok=True)
    marker = md_dir / wake_corpus_setup.TEST_MODE_MARKER
    marker.write_text(json.dumps({"entered_at": "2026-06-09T00:00:00+00:00"}))
    mtime = time.time() - age_sec
    os.utime(marker, (mtime, mtime))
    return marker


@pytest.fixture
def recovery_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[str]]:
    """Capture the systemctl side of exit_corpus_test_mode()."""
    calls: dict[str, list[str]] = {"voice": [], "bridge_disable": []}
    monkeypatch.setattr(
        wake_corpus_setup,
        "disable_bridge_corpus_outputs",
        lambda: calls["bridge_disable"].append("disable") or False,
    )
    monkeypatch.setattr(
        wake_corpus_setup,
        "set_voice_daemon_state",
        lambda action: calls["voice"].append(action),
    )
    return calls


def test_stale_marker_recovers_voice_and_clears_marker(
    tmp_path: Path, recovery_calls: dict[str, list[str]],
) -> None:
    """A stale marker self-heals: disable bridge outputs, restart voice,
    then remove the marker so it doesn't fire again."""
    out = tmp_path / "out"
    marker = _write_test_mode_marker(
        out, age_sec=wake_corpus_setup.TEST_MODE_STALE_SEC + 60,
    )

    b = _make_backend(out)
    b._maybe_recover_stale_test_mode()

    assert recovery_calls["bridge_disable"] == ["disable"]
    assert recovery_calls["voice"] == ["start"]
    assert not marker.exists()


def test_fresh_marker_is_left_alone(
    tmp_path: Path, recovery_calls: dict[str, list[str]],
) -> None:
    """A marker newer than the stale window means the operator's tab is
    still open and working — recovery must not touch voice."""
    out = tmp_path / "out"
    marker = _write_test_mode_marker(
        out, age_sec=wake_corpus_setup.TEST_MODE_STALE_SEC - 30,
    )

    b = _make_backend(out)
    b._maybe_recover_stale_test_mode()

    assert recovery_calls["voice"] == []
    assert recovery_calls["bridge_disable"] == []
    assert marker.exists()


def test_no_marker_is_a_no_op(
    tmp_path: Path, recovery_calls: dict[str, list[str]],
) -> None:
    """No marker → nothing was stopped → nothing to recover."""
    out = tmp_path / "out"
    (out / "metadata").mkdir(parents=True)

    b = _make_backend(out)
    b._maybe_recover_stale_test_mode()

    assert recovery_calls["voice"] == []
    assert recovery_calls["bridge_disable"] == []


def test_active_session_blocks_teardown(
    tmp_path: Path, recovery_calls: dict[str, list[str]],
) -> None:
    """A resumed corpus session means voice should stay stopped — even
    with a stale marker, recovery must not restart the daemon out from
    under a live session, and the marker stays for a later attempt."""
    out = tmp_path / "out"
    marker = _write_test_mode_marker(
        out, age_sec=wake_corpus_setup.TEST_MODE_STALE_SEC + 60,
    )

    b = _make_backend(out)
    # Simulate _maybe_load_recent_session() having reattached a session.
    b._session_id = "20260609T000000Z"

    b._maybe_recover_stale_test_mode()

    assert recovery_calls["voice"] == []
    assert recovery_calls["bridge_disable"] == []
    assert marker.exists()


def test_recording_in_progress_blocks_teardown(
    tmp_path: Path, recovery_calls: dict[str, list[str]],
) -> None:
    """A recording in flight must never be interrupted by recovery."""
    out = tmp_path / "out"
    marker = _write_test_mode_marker(
        out, age_sec=wake_corpus_setup.TEST_MODE_STALE_SEC + 60,
    )

    b = _make_backend(out)
    b._current = object()  # sentinel: is_recording() == True

    b._maybe_recover_stale_test_mode()

    assert recovery_calls["voice"] == []
    assert marker.exists()


def test_failed_restart_keeps_marker_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the voice restart fails, the marker is left behind so a later
    startup retries — and the failure must not crash the recorder."""
    out = tmp_path / "out"
    marker = _write_test_mode_marker(
        out, age_sec=wake_corpus_setup.TEST_MODE_STALE_SEC + 60,
    )
    monkeypatch.setattr(
        wake_corpus_setup, "disable_bridge_corpus_outputs", lambda: False,
    )

    def boom(action: str) -> None:
        raise subprocess.CalledProcessError(1, ["systemctl", action])

    monkeypatch.setattr(wake_corpus_setup, "set_voice_daemon_state", boom)

    b = _make_backend(out)
    b._maybe_recover_stale_test_mode()  # must not raise

    assert marker.exists()


def test_start_runs_recovery(
    tmp_path: Path, recovery_calls: dict[str, list[str]],
) -> None:
    """The self-heal is wired into start(), so the socket re-spawning the
    service on the next request actually triggers it end-to-end."""
    out = tmp_path / "out"
    marker = _write_test_mode_marker(
        out, age_sec=wake_corpus_setup.TEST_MODE_STALE_SEC + 60,
    )

    b = _make_backend(out)
    b.start()
    try:
        assert recovery_calls["voice"] == ["start"]
        assert not marker.exists()
    finally:
        b.shutdown()


def test_enter_exit_marker_round_trip(tmp_path: Path) -> None:
    """note_test_mode_entered() writes the marker; note_test_mode_exited()
    removes it. Clearing when absent is a graceful no-op."""
    out = tmp_path / "out"
    b = _make_backend(out)
    marker = out / "metadata" / wake_corpus_setup.TEST_MODE_MARKER

    b.note_test_mode_entered()
    assert marker.is_file()

    b.note_test_mode_exited()
    assert not marker.exists()

    # Idempotent: clearing a missing marker doesn't raise.
    b.note_test_mode_exited()
