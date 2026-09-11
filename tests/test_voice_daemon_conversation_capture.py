# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the conversation-capture integration in jasper.voice_daemon.

ConversationCapture's own gating, lazy-open, reopen and retention
behavior is covered by tests/test_conversation_capture.py. These pin
the WakeLoop-level wiring that can't be exercised without a full
WakeLoop: `_end_turn_inner`'s single write path via `turn.capture()`.
"""

from __future__ import annotations

import json

from jasper.conversation_history import (
    CAPTURE_ALIAS_ENV,
    ConversationStore,
    DB_PATH_ENV,
)
from tests._live_turn_fake import FakeLiveTurn as _FakeTurn
from tests._wake_loop import wake_loop_for_tests
from tests.usage_store_fixtures import FakeUsageStore


def _wake_loop(tmp_path, monkeypatch, *, capture: bool = True):

    db_path = tmp_path / "conversation_history.db"
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1" if capture else "0")
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    store = ConversationStore(str(db_path))
    wl = wake_loop_for_tests(
        conversation_store=store, usage_store=FakeUsageStore(),
    )
    return wl, store


def _put_in_session(wl, turn: _FakeTurn) -> None:
    from jasper.voice.turn_lifecycle import State

    wl._turns.state = State.SESSION
    wl._turns.turn = turn
    wl._turns.session_id = 7
    wl._turns.user_speech_seen = True
    wl._turns.input_ended = False

    async def _noop(*_args, **_kwargs):
        return None

    async def _noop_chirp(*, going_on):
        return None

    wl._wake_telemetry.stage = _noop
    wl._wake_telemetry.outcome = _noop
    wl._peering.session_ended = _noop
    wl._play_listening_chirp = _noop_chirp


async def test_end_turn_records_transcripts_through_single_write_path(
    tmp_path,
    monkeypatch,
) -> None:
    wl, store = _wake_loop(tmp_path, monkeypatch)
    _put_in_session(wl, _FakeTurn("what is the next train", "Four minutes."))

    await wl._turns._end_turn_inner("test")
    await wl._turns.pending_release

    rows = store.recent(10)
    assert len(rows) == 1
    assert rows[0].provider == "test"
    assert rows[0].user_text == "what is the next train"
    assert rows[0].assistant_text == "Four minutes."
    assert rows[0].session_id == 7
    assert rows[0].data_json is None


async def test_end_turn_records_metadata_when_provider_has_no_transcripts(
    tmp_path,
    monkeypatch,
) -> None:
    wl, store = _wake_loop(tmp_path, monkeypatch)
    _put_in_session(
        wl,
        _FakeTurn(
            None,
            None,
            metadata={
                "kind": "voice_turn",
                "transcripts_available": False,
                "tools": ["get_weather"],
            },
        ),
    )

    await wl._turns._end_turn_inner("gemini")
    await wl._turns.pending_release

    rows = store.recent(10)
    assert len(rows) == 1
    assert rows[0].provider == "test"
    assert rows[0].user_text is None
    assert rows[0].assistant_text is None
    assert json.loads(rows[0].data_json or "{}") == {
        "kind": "voice_turn",
        "transcripts_available": False,
        "tools": ["get_weather"],
    }


async def test_capture_includes_transcript_from_the_close_handshake(
    tmp_path,
    monkeypatch,
) -> None:
    """The provider keeps streaming transcript while it closes, so history
    is snapshotted after `release()` returns rather than before it starts."""
    wl, store = _wake_loop(tmp_path, monkeypatch)
    turn = _FakeTurn("when is the next train", "Four minutes")

    async def release() -> None:
        turn._assistant_text = "Four minutes, from Union Square."

    turn.release = release
    _put_in_session(wl, turn)

    await wl._turns._end_turn_inner("test")
    await wl._turns.pending_release

    (row,) = store.recent(10)
    assert row.assistant_text == "Four minutes, from Union Square."
