from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Gemini 3.1 Flash Live published rates (May 2026): see PLAN.md / pricing notes.
# These are rough USD/token estimates for the spend-cap circuit breaker; we
# convert minutes → tokens via Gemini's published audio token billing.
# The cap is a coarse safety net, not a billing source of truth.
GEMINI_AUDIO_IN_USD_PER_1M = 5.0
GEMINI_AUDIO_OUT_USD_PER_1M = 18.0


class UsageStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0
            )
            """
        )

    def open_session(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO sessions (started_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        return int(cur.lastrowid)

    def close_session(self, session_id: int, input_tokens: int, output_tokens: int) -> float:
        cost = self._estimate_cost(input_tokens, output_tokens)
        self._conn.execute(
            """
            UPDATE sessions
            SET ended_at = ?, input_tokens = ?, output_tokens = ?, cost_usd = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                input_tokens,
                output_tokens,
                cost,
                session_id,
            ),
        )
        return cost

    def spend_last_24h_usd(self) -> float:
        cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM sessions "
            "WHERE strftime('%s', started_at) >= ?",
            (str(int(cutoff)),),
        )
        row = cur.fetchone()
        return float(row[0] if row else 0.0)

    @staticmethod
    def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * GEMINI_AUDIO_IN_USD_PER_1M / 1_000_000
            + output_tokens * GEMINI_AUDIO_OUT_USD_PER_1M / 1_000_000
        )


class SpendCap:
    def __init__(self, store: UsageStore, cap_usd: float) -> None:
        self._store = store
        self._cap_usd = cap_usd

    def allowed(self) -> bool:
        return self._store.spend_last_24h_usd() < self._cap_usd

    def remaining_usd(self) -> float:
        return max(0.0, self._cap_usd - self._store.spend_last_24h_usd())
