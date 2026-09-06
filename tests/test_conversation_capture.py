# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from jasper.conversation_history import (
    CAPTURE_ALIAS_ENV,
    ConversationStore,
    ConversationTurn,
    DB_PATH_ENV,
    RETENTION_DAYS_ENV,
    RETENTION_MAX_ROWS_ENV,
)
from jasper.voice.conversation_capture import ConversationCapture


def _capture(tmp_path, monkeypatch, *, capture: bool = True):
    db_path = tmp_path / "conversation_history.db"
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1" if capture else "0")
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    store = ConversationStore(str(db_path))
    return ConversationCapture(store=store, voice_provider="test"), store


def test_record_is_gated_by_capture_env(tmp_path, monkeypatch) -> None:
    capture, store = _capture(tmp_path, monkeypatch, capture=False)

    capture.record("hello", "hi", session_id=None, mic_muted=False)

    assert store.recent(10) == []


def test_record_allows_metadata_only_rows(
    tmp_path,
    monkeypatch,
) -> None:
    capture, store = _capture(tmp_path, monkeypatch)

    capture.record(
        None,
        None,
        data_json={"kind": "voice_turn", "transcripts_available": False},
        session_id=None,
        mic_muted=False,
    )

    rows = store.recent(10)
    assert len(rows) == 1
    assert rows[0].user_text is None
    assert rows[0].assistant_text is None
    assert json.loads(rows[0].data_json or "{}") == {
        "kind": "voice_turn",
        "transcripts_available": False,
    }


def test_record_lazily_opens_store_after_capture_enabled(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "conversation_history.db"
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1")
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    capture = ConversationCapture(store=None, voice_provider="test")

    capture.record("hello", "hi", session_id=None, mic_muted=False)

    assert capture.store_path == str(db_path)
    assert capture.store is not None
    rows = capture.store.recent(10)
    assert len(rows) == 1
    assert rows[0].user_text == "hello"
    assert rows[0].assistant_text == "hi"


def test_record_reopens_store_when_db_path_changes(
    tmp_path,
    monkeypatch,
) -> None:
    first_db = tmp_path / "first.db"
    second_db = tmp_path / "second.db"
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1")
    monkeypatch.setenv(DB_PATH_ENV, str(first_db))
    capture = ConversationCapture(store=None, voice_provider="test")

    capture.record("first", "one", session_id=None, mic_muted=False)
    monkeypatch.setenv(DB_PATH_ENV, str(second_db))
    capture.record("second", "two", session_id=None, mic_muted=False)

    assert capture.store_path == str(second_db)
    first_reader = ConversationStore(str(first_db), read_only=True)
    second_reader = ConversationStore(str(second_db), read_only=True)
    try:
        assert [row.user_text for row in first_reader.recent(10)] == ["first"]
        assert [row.user_text for row in second_reader.recent(10)] == ["second"]
    finally:
        first_reader.close()
        second_reader.close()


def test_record_skips_while_mic_muted(tmp_path, monkeypatch) -> None:
    capture, store = _capture(tmp_path, monkeypatch)

    capture.record("hello", "hi", session_id=None, mic_muted=True)

    assert store.recent(10) == []


def test_record_enforces_retention_max_rows(
    tmp_path,
    monkeypatch,
) -> None:
    import jasper.voice.conversation_capture as conversation_capture

    capture, store = _capture(tmp_path, monkeypatch)
    monkeypatch.setenv(RETENTION_MAX_ROWS_ENV, "2")
    timestamps = iter([
        "2026-06-19T20:10:00Z",
        "2026-06-19T20:20:00Z",
        "2026-06-19T20:30:00Z",
    ])
    monkeypatch.setattr(
        conversation_capture,
        "_conversation_ts_utc",
        lambda: next(timestamps),
    )

    capture.record("first", "one", session_id=None, mic_muted=False)
    capture.record("second", "two", session_id=None, mic_muted=False)
    capture.record("third", "three", session_id=None, mic_muted=False)

    assert [row.user_text for row in store.recent(10)] == ["third", "second"]


def test_record_enforces_retention_days(
    tmp_path,
    monkeypatch,
) -> None:
    import jasper.voice.conversation_capture as conversation_capture

    capture, store = _capture(tmp_path, monkeypatch)
    monkeypatch.setenv(RETENTION_DAYS_ENV, "1")
    assert store.add(
        ConversationTurn(
            id="old",
            ts_utc="2026-06-19T20:00:00Z",
            provider="gemini",
            user_text="old",
            assistant_text="old answer",
            tool_calls_json=None,
            data_json=None,
            session_id=1,
        ),
    )
    monkeypatch.setattr(
        conversation_capture,
        "_conversation_ts_utc",
        lambda: "2026-06-21T20:00:00Z",
    )

    capture.record("new", "new answer", session_id=None, mic_muted=False)

    assert [row.user_text for row in store.recent(10)] == ["new"]
