# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the conversation-capture integration in jasper.voice_daemon.

ConversationCapture's own gating, lazy-open, reopen and retention
behavior is covered by tests/test_conversation_capture.py. These pin
the WakeLoop-level wiring that can't be exercised without a full
WakeLoop: `_end_turn_inner`'s single write path via `turn.capture()`,
and `record_research_delivery`'s research-window bookkeeping.
"""

from __future__ import annotations

import json

from jasper.conversation_history import (
    CAPTURE_ALIAS_ENV,
    ConversationStore,
    DB_PATH_ENV,
)
from jasper.research import DONE, ResearchJob
from tests._live_turn_fake import FakeLiveTurn as _FakeTurn
from tests.usage_store_fixtures import FakeUsageStore


def _wake_loop(tmp_path, monkeypatch, *, capture: bool = True):
    from jasper.voice_daemon import WakeLoop

    db_path = tmp_path / "conversation_history.db"
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1" if capture else "0")
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    store = ConversationStore(str(db_path))
    wl = WakeLoop.for_tests(conversation_store=store)
    return wl, store


def _put_in_session(wl, turn: _FakeTurn) -> None:
    from jasper.voice_daemon import State

    wl._state = State.SESSION
    wl._turn = turn
    wl._session_id = 7
    wl._usage_store = FakeUsageStore()
    wl._user_speech_seen = True
    wl._input_ended = False

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

    await wl._end_turn_inner("test")

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

    await wl._end_turn_inner("gemini")

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


async def test_research_readback_records_query_report_and_data_json(
    tmp_path,
    monkeypatch,
) -> None:
    wl, store = _wake_loop(tmp_path, monkeypatch)
    job = ResearchJob(
        id="research123",
        query="research induction cooktops",
        status=DONE,
        result="Induction is fast and efficient.",
        error=None,
        created_at=1.0,
        finished_at=2.0,
        announced=False,
        read=False,
    )

    wl.record_research_delivery(
        job,
        "Induction is fast and efficient.",
        "yes",
    )

    rows = store.recent(10)
    assert len(rows) == 1
    assert rows[0].user_text == "research induction cooktops"
    assert rows[0].assistant_text == "Induction is fast and efficient."
    assert json.loads(rows[0].data_json or "{}") == {
        "kind": "research",
        "job_id": "research123",
    }
