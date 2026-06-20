"""Fail-soft SQLite persistence for captured conversation turns."""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/var/lib/jasper/conversation_history.db"
_STORE_ERRORS = (OSError, sqlite3.Error)
_TURN_COLUMNS = (
    "id",
    "ts_utc",
    "provider",
    "user_text",
    "assistant_text",
    "tool_calls_json",
    "data_json",
    "session_id",
)
_TURN_COLUMNS_SQL = ", ".join(_TURN_COLUMNS)
_RECENT_ORDER_SQL = "ts_utc DESC, id DESC"


@dataclass(frozen=True)
class ConversationTurn:
    id: str
    ts_utc: str
    provider: str | None
    user_text: str | None
    assistant_text: str | None
    tool_calls_json: str | None
    data_json: str | None
    session_id: int | None


class ConversationStore:
    """Fail-soft SQLite persistence for conversation history."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        conn: sqlite3.Connection | None = None
        try:
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS conversation_turns ("
                "  id TEXT PRIMARY KEY,"
                "  ts_utc TEXT NOT NULL,"
                "  provider TEXT,"
                "  user_text TEXT,"
                "  assistant_text TEXT,"
                "  tool_calls_json TEXT,"
                "  data_json TEXT,"
                "  session_id INTEGER"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_turns_recent "
                "ON conversation_turns (ts_utc DESC, id DESC)"
            )
        except _STORE_ERRORS as e:
            logger.warning("conversation history store unavailable (%s): %s", db_path, e)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._conn = None
        else:
            self._conn = conn

    @property
    def available(self) -> bool:
        return self._conn is not None

    def add(self, turn: ConversationTurn) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            conn.execute(
                "INSERT INTO conversation_turns (id, ts_utc, provider, user_text, "
                "assistant_text, tool_calls_json, data_json, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _row_values(turn),
            )
            return True
        except _STORE_ERRORS as e:
            logger.warning("conversation history store add failed (id=%s): %s", turn.id, e)
            return False

    def get(self, turn_id: str) -> ConversationTurn | None:
        conn = self._conn
        if conn is None:
            return None
        try:
            row = conn.execute(
                f"SELECT {_TURN_COLUMNS_SQL} FROM conversation_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
        except _STORE_ERRORS as e:
            logger.warning("conversation history store get failed (id=%s): %s", turn_id, e)
            return None
        return _turn_from_row(row) if row is not None else None

    def recent(self, limit: int, since_ts: str | None = None) -> list[ConversationTurn]:
        conn = self._conn
        if conn is None:
            return []
        limit_value = _coerce_nonnegative_int(limit, "recent limit")
        if limit_value is None or limit_value == 0:
            return []
        try:
            if since_ts is None:
                rows = conn.execute(
                    f"SELECT {_TURN_COLUMNS_SQL} FROM conversation_turns "
                    f"ORDER BY {_RECENT_ORDER_SQL} LIMIT ?",
                    (limit_value,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_TURN_COLUMNS_SQL} FROM conversation_turns "
                    f"WHERE ts_utc >= ? ORDER BY {_RECENT_ORDER_SQL} LIMIT ?",
                    (since_ts, limit_value),
                ).fetchall()
        except _STORE_ERRORS as e:
            logger.warning("conversation history store recent failed: %s", e)
            return []
        return [_turn_from_row(row) for row in rows]

    def delete(self, turn_id: str) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            cursor = conn.execute(
                "DELETE FROM conversation_turns WHERE id = ?",
                (turn_id,),
            )
            return _changed_count(cursor) > 0
        except _STORE_ERRORS as e:
            logger.warning("conversation history store delete failed (id=%s): %s", turn_id, e)
            return False

    def clear(self) -> int:
        conn = self._conn
        if conn is None:
            return 0
        try:
            cursor = conn.execute("DELETE FROM conversation_turns")
            return _changed_count(cursor)
        except _STORE_ERRORS as e:
            logger.warning("conversation history store clear failed: %s", e)
            return 0

    def prune(
        self,
        *,
        max_rows: int | None = None,
        older_than_ts: str | None = None,
    ) -> int:
        conn = self._conn
        if conn is None:
            return 0
        max_rows_value: int | None = None
        if max_rows is not None:
            max_rows_value = _coerce_nonnegative_int(max_rows, "prune max_rows")
            if max_rows_value is None:
                return 0
        if max_rows_value is None and older_than_ts is None:
            return 0

        try:
            conn.execute("BEGIN")
            deleted = 0
            if older_than_ts is not None:
                cursor = conn.execute(
                    "DELETE FROM conversation_turns WHERE ts_utc < ?",
                    (older_than_ts,),
                )
                deleted += _changed_count(cursor)
            if max_rows_value is not None:
                cursor = conn.execute(
                    "DELETE FROM conversation_turns WHERE id IN ("
                    "  SELECT id FROM conversation_turns "
                    f"  ORDER BY {_RECENT_ORDER_SQL} LIMIT -1 OFFSET ?"
                    ")",
                    (max_rows_value,),
                )
                deleted += _changed_count(cursor)
            conn.execute("COMMIT")
            return deleted
        except _STORE_ERRORS as e:
            try:
                conn.execute("ROLLBACK")
            except _STORE_ERRORS:
                pass
            logger.warning("conversation history store prune failed: %s", e)
            return 0

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass


def make_turn_id(ts_utc: str, seq: int) -> str:
    """Return a deterministic, sortable turn id from a caller-provided timestamp."""
    return f"{_compact_utc(ts_utc)}-{seq:03d}"


def _compact_utc(ts_utc: str) -> str:
    raw = ts_utc.strip()
    parse_value = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(parse_value)
    except ValueError:
        return "".join(ch for ch in raw if ch.isalnum())
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _row_values(turn: ConversationTurn) -> tuple[Any, ...]:
    return (
        turn.id,
        turn.ts_utc,
        turn.provider,
        turn.user_text,
        turn.assistant_text,
        turn.tool_calls_json,
        turn.data_json,
        turn.session_id,
    )


def _turn_from_row(row: tuple[Any, ...]) -> ConversationTurn:
    return ConversationTurn(
        id=row[0],
        ts_utc=row[1],
        provider=row[2],
        user_text=row[3],
        assistant_text=row[4],
        tool_calls_json=row[5],
        data_json=row[6],
        session_id=row[7],
    )


def _changed_count(cursor: sqlite3.Cursor) -> int:
    rowcount = cursor.rowcount
    if rowcount is None or rowcount < 0:
        return 0
    return rowcount


def _coerce_nonnegative_int(value: int, label: str) -> int | None:
    try:
        coerced = int(value)
    except (TypeError, ValueError, OverflowError) as e:
        logger.warning("conversation history store invalid %s: %s", label, e)
        return None
    return max(0, coerced)
