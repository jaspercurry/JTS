"""Fail-soft SQLite persistence for captured conversation turns."""
from __future__ import annotations

import logging
import os
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/var/lib/jasper/conversation_history.db"
DEFAULT_SETTINGS_PATH = "/var/lib/jasper/conversation_history.env"
SETTINGS_PATH_ENV = "JASPER_CONVERSATION_HISTORY_FILE"
DB_PATH_ENV = "JASPER_CONVERSATION_HISTORY_DB"
CAPTURE_ENABLED_ENV = "JASPER_CONVERSATION_HISTORY_ENABLED"
CAPTURE_ALIAS_ENV = "JASPER_CONVERSATION_CAPTURE"
RETENTION_DAYS_ENV = "JASPER_CONVERSATION_HISTORY_RETENTION_DAYS"
RETENTION_MAX_ROWS_ENV = "JASPER_CONVERSATION_HISTORY_MAX_ROWS"
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


@dataclass(frozen=True)
class ConversationStats:
    turn_count: int
    last_write_ts_utc: str | None


@dataclass(frozen=True)
class ConversationSettings:
    """Fresh read of the conversation-history feature settings."""

    capture_enabled: bool
    db_path: str
    retention_days: int | None
    retention_max_rows: int | None
    settings_path: str

    @property
    def retention(self) -> dict[str, int | None]:
        return {
            "days": self.retention_days,
            "max_rows": self.retention_max_rows,
        }


class ConversationStore:
    """Fail-soft SQLite persistence for conversation history."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        *,
        read_only: bool = False,
        warn_unavailable: bool = True,
    ):
        self._db_path = db_path
        self._read_only = read_only
        self._warn_unavailable = warn_unavailable
        self._conn: sqlite3.Connection | None = None
        conn: sqlite3.Connection | None = None
        try:
            if read_only:
                conn = sqlite3.connect(
                    _read_only_uri(db_path),
                    isolation_level=None,
                    uri=True,
                )
            else:
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
            self._warn(
                "conversation history store unavailable (%s): %s",
                db_path,
                e,
            )
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

    @property
    def db_path(self) -> str:
        return self._db_path

    def _warn(self, msg: str, *args: Any) -> None:
        if self._warn_unavailable:
            logger.warning(msg, *args)

    def add(self, turn: ConversationTurn) -> bool:
        conn = self._conn
        if conn is None or self._read_only:
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
            self._warn("conversation history store add failed (id=%s): %s", turn.id, e)
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
            self._warn("conversation history store get failed (id=%s): %s", turn_id, e)
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
            self._warn("conversation history store recent failed: %s", e)
            return []
        return [_turn_from_row(row) for row in rows]

    def delete(self, turn_id: str) -> bool:
        conn = self._conn
        if conn is None or self._read_only:
            return False
        try:
            cursor = conn.execute(
                "DELETE FROM conversation_turns WHERE id = ?",
                (turn_id,),
            )
            return _changed_count(cursor) > 0
        except _STORE_ERRORS as e:
            self._warn("conversation history store delete failed (id=%s): %s", turn_id, e)
            return False

    def clear(self) -> int:
        conn = self._conn
        if conn is None or self._read_only:
            return 0
        try:
            cursor = conn.execute("DELETE FROM conversation_turns")
            return _changed_count(cursor)
        except _STORE_ERRORS as e:
            self._warn("conversation history store clear failed: %s", e)
            return 0

    def prune(
        self,
        *,
        max_rows: int | None = None,
        older_than_ts: str | None = None,
    ) -> int:
        conn = self._conn
        if conn is None or self._read_only:
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
            self._warn("conversation history store prune failed: %s", e)
            return 0

    def stats(self) -> ConversationStats | None:
        conn = self._conn
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT COUNT(*), MAX(ts_utc) FROM conversation_turns",
            ).fetchone()
        except _STORE_ERRORS as e:
            self._warn("conversation history store stats failed: %s", e)
            return None
        if row is None:
            return ConversationStats(0, None)
        return ConversationStats(
            turn_count=int(row[0] or 0),
            last_write_ts_utc=row[1],
        )

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


def read_settings(
    *,
    path: str | None = None,
    environ: dict[str, str] | None = None,
) -> ConversationSettings:
    """Read conversation-history settings fresh from the wizard-owned file.

    The future privacy/retention controls own
    ``/var/lib/jasper/conversation_history.env``. Read-side surfaces such as
    ``/state`` and ``jasper-doctor`` must not rely on their process
    environment, because those daemons are not restarted by a wizard save.
    """
    from .env_load import read_env_file_state

    base_env = dict(os.environ if environ is None else environ)
    settings_path = path or base_env.get(SETTINGS_PATH_ENV) or DEFAULT_SETTINGS_PATH
    file_state = read_env_file_state(settings_path)
    merged = {**base_env, **file_state.values}
    file_values = file_state.values
    if CAPTURE_ALIAS_ENV in file_values:
        capture_raw = file_values.get(CAPTURE_ALIAS_ENV)
    elif CAPTURE_ENABLED_ENV in file_values:
        capture_raw = file_values.get(CAPTURE_ENABLED_ENV)
    elif CAPTURE_ALIAS_ENV in base_env:
        capture_raw = base_env.get(CAPTURE_ALIAS_ENV)
    else:
        capture_raw = base_env.get(CAPTURE_ENABLED_ENV)
    return ConversationSettings(
        capture_enabled=_env_bool(capture_raw, default=False),
        db_path=(merged.get(DB_PATH_ENV) or DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH,
        retention_days=_env_optional_positive_int(merged.get(RETENTION_DAYS_ENV)),
        retention_max_rows=_env_optional_positive_int(
            merged.get(RETENTION_MAX_ROWS_ENV),
        ),
        settings_path=settings_path,
    )


def _read_only_uri(db_path: str) -> str:
    path = os.path.abspath(db_path)
    return f"file:{urllib.parse.quote(path, safe='/')}?mode=ro"


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _env_optional_positive_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value.strip(), 10)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


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
