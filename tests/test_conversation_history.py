from __future__ import annotations

import logging
import sqlite3

from jasper.conversation_history import (
    CAPTURE_ENABLED_ENV,
    ConversationStore,
    ConversationTurn,
    DB_PATH_ENV,
    RETENTION_DAYS_ENV,
    RETENTION_MAX_ROWS_ENV,
    make_turn_id,
    read_settings,
)


def _turn(
    ts_utc: str,
    seq: int,
    *,
    provider: str | None = "openai",
    user_text: str | None = "turn on the lights",
    assistant_text: str | None = "Turning on the lights.",
    tool_calls_json: str | None = None,
    data_json: str | None = None,
    session_id: int | None = 42,
) -> ConversationTurn:
    return ConversationTurn(
        id=make_turn_id(ts_utc, seq),
        ts_utc=ts_utc,
        provider=provider,
        user_text=user_text,
        assistant_text=assistant_text,
        tool_calls_json=tool_calls_json,
        data_json=data_json,
        session_id=session_id,
    )


def test_make_turn_id_is_deterministic_and_sortable():
    assert make_turn_id("2026-06-19T20:15:00Z", 1) == "20260619T201500Z-001"
    assert make_turn_id("2026-06-19T20:15:00+00:00", 2) == "20260619T201500Z-002"
    assert make_turn_id("2026-06-19T16:15:00-04:00", 3) == "20260619T201500Z-003"


def test_add_get_round_trip(tmp_path):
    store = ConversationStore(str(tmp_path / "history.db"))
    turn = _turn(
        "2026-06-19T20:15:00Z",
        1,
        provider="gemini",
        user_text="what is the next train",
        assistant_text="The next train is in four minutes.",
        tool_calls_json='[{"name":"get_subway_arrivals"}]',
        data_json='{"links":[]}',
        session_id=123,
    )

    assert store.available is True
    assert store.add(turn) is True
    assert store.get(turn.id) == turn
    assert store.get("missing") is None


def test_schema_has_reserved_columns_and_recent_index(tmp_path):
    db_path = tmp_path / "history.db"
    store = ConversationStore(str(db_path))
    store.close()

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_turns)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(conversation_turns)")}
    finally:
        conn.close()

    assert {
        "id",
        "ts_utc",
        "provider",
        "user_text",
        "assistant_text",
        "tool_calls_json",
        "data_json",
        "session_id",
    } <= columns
    assert "idx_conversation_turns_recent" in indexes


def test_recent_orders_newest_first_with_limit_and_since_filter(tmp_path):
    store = ConversationStore(str(tmp_path / "history.db"))
    turns = [
        _turn("2026-06-19T20:10:00Z", 1, user_text="old"),
        _turn("2026-06-19T20:20:00Z", 1, user_text="middle"),
        _turn("2026-06-19T20:30:00Z", 1, user_text="new"),
        _turn("2026-06-19T20:30:00Z", 2, user_text="newer same second"),
    ]
    for turn in turns:
        assert store.add(turn) is True

    assert [turn.user_text for turn in store.recent(3)] == [
        "newer same second",
        "new",
        "middle",
    ]
    assert [turn.user_text for turn in store.recent(10, since_ts="2026-06-19T20:20:00Z")] == [
        "newer same second",
        "new",
        "middle",
    ]
    assert store.recent(0) == []


def test_read_only_store_does_not_create_or_write_db(tmp_path):
    db_path = tmp_path / "missing.db"
    store = ConversationStore(str(db_path), read_only=True)
    turn = _turn("2026-06-19T20:15:00Z", 1)

    assert store.available is False
    assert db_path.exists() is False
    assert store.add(turn) is False
    assert store.delete(turn.id) is False
    assert store.clear() == 0
    assert store.prune(max_rows=1) == 0
    assert db_path.exists() is False


def test_read_only_store_can_read_existing_db_but_not_mutate(tmp_path):
    db_path = tmp_path / "history.db"
    writer = ConversationStore(str(db_path))
    first = _turn("2026-06-19T20:10:00Z", 1, user_text="first")
    second = _turn("2026-06-19T20:20:00Z", 1, user_text="second")
    assert writer.add(first) is True
    assert writer.add(second) is True
    writer.close()

    reader = ConversationStore(str(db_path), read_only=True)
    assert reader.available is True
    assert [turn.user_text for turn in reader.recent(10)] == ["second", "first"]
    stats = reader.stats()
    assert stats is not None
    assert stats.turn_count == 2
    assert reader.add(_turn("2026-06-19T20:30:00Z", 1)) is False
    assert reader.delete(first.id) is False
    reader.close()

    writer = ConversationStore(str(db_path))
    assert [turn.user_text for turn in writer.recent(10)] == ["second", "first"]


def test_read_only_store_can_suppress_query_warnings(tmp_path, caplog):
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()

    caplog.set_level(logging.WARNING, logger="jasper.conversation_history")
    reader = ConversationStore(
        str(db_path),
        read_only=True,
        warn_unavailable=False,
    )
    try:
        assert reader.stats() is None
        assert reader.recent(10) == []
    finally:
        reader.close()

    assert caplog.text == ""


def test_read_settings_merges_process_env_and_fresh_wizard_file(tmp_path):
    settings_file = tmp_path / "conversation_history.env"
    db_path = tmp_path / "wizard.db"
    settings_file.write_text(
        "\n".join([
            f"{CAPTURE_ENABLED_ENV}=1",
            f"{DB_PATH_ENV}={db_path}",
            f"{RETENTION_DAYS_ENV}=14",
            f"{RETENTION_MAX_ROWS_ENV}=250",
        ])
        + "\n",
        encoding="utf-8",
    )

    settings = read_settings(
        path=str(settings_file),
        environ={
            CAPTURE_ENABLED_ENV: "0",
            DB_PATH_ENV: "/tmp/stale.db",
        },
    )

    assert settings.capture_enabled is True
    assert settings.db_path == str(db_path)
    assert settings.retention == {"days": 14, "max_rows": 250}


def test_prune_by_max_rows_keeps_newest_rows(tmp_path):
    store = ConversationStore(str(tmp_path / "history.db"))
    for idx, minute in enumerate(["10", "20", "30", "40"], start=1):
        assert store.add(_turn(f"2026-06-19T20:{minute}:00Z", idx)) is True

    assert store.prune(max_rows=2) == 2
    assert [turn.ts_utc for turn in store.recent(10)] == [
        "2026-06-19T20:40:00Z",
        "2026-06-19T20:30:00Z",
    ]


def test_prune_by_older_than_timestamp(tmp_path):
    store = ConversationStore(str(tmp_path / "history.db"))
    for idx, minute in enumerate(["10", "20", "30", "40"], start=1):
        assert store.add(_turn(f"2026-06-19T20:{minute}:00Z", idx)) is True

    assert store.prune(older_than_ts="2026-06-19T20:30:00Z") == 2
    assert [turn.ts_utc for turn in store.recent(10)] == [
        "2026-06-19T20:40:00Z",
        "2026-06-19T20:30:00Z",
    ]


def test_delete_and_clear(tmp_path):
    store = ConversationStore(str(tmp_path / "history.db"))
    first = _turn("2026-06-19T20:10:00Z", 1)
    second = _turn("2026-06-19T20:20:00Z", 1)
    assert store.add(first) is True
    assert store.add(second) is True

    assert store.delete(first.id) is True
    assert store.delete(first.id) is False
    assert store.get(first.id) is None
    assert store.get(second.id) == second

    assert store.clear() == 1
    assert store.recent(10) == []
    assert store.clear() == 0


def test_fail_soft_when_sqlite_unavailable(tmp_path):
    bad_path = tmp_path / "not-a-db-file"
    bad_path.mkdir()
    store = ConversationStore(str(bad_path))
    turn = _turn("2026-06-19T20:15:00Z", 1)

    assert store.available is False
    assert store.add(turn) is False
    assert store.get(turn.id) is None
    assert store.recent(10) == []
    assert store.delete(turn.id) is False
    assert store.clear() == 0
    assert store.prune(max_rows=1) == 0
    assert store.prune(older_than_ts="2026-06-19T20:00:00Z") == 0
    store.close()


def test_methods_fail_soft_when_sqlite_connection_errors(tmp_path):
    store = ConversationStore(str(tmp_path / "history.db"))
    turn = _turn("2026-06-19T20:15:00Z", 1)
    assert store.add(turn) is True

    conn = store._conn
    assert conn is not None
    conn.close()

    assert store.add(_turn("2026-06-19T20:16:00Z", 1)) is False
    assert store.get(turn.id) is None
    assert store.recent(10) == []
    assert store.delete(turn.id) is False
    assert store.clear() == 0
    assert store.prune(max_rows=1) == 0
    assert store.prune(older_than_ts="2026-06-19T20:00:00Z") == 0
    store.close()
