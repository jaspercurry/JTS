from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jasper.usage import (
    GEMINI_AUDIO_IN_USD_PER_1M,
    GEMINI_AUDIO_OUT_USD_PER_1M,
    SpendCap,
    UsageStore,
)


def test_open_and_close_session_records_cost(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    sid = store.open_session()
    cost = store.close_session(sid, input_tokens=10_000, output_tokens=20_000)

    expected = (
        10_000 * GEMINI_AUDIO_IN_USD_PER_1M / 1_000_000
        + 20_000 * GEMINI_AUDIO_OUT_USD_PER_1M / 1_000_000
    )
    assert abs(cost - expected) < 1e-9
    assert store.spend_last_24h_usd() == cost


def test_spend_cap_blocks_when_exceeded(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    cap = SpendCap(store, cap_usd=0.01)

    assert cap.allowed() is True
    sid = store.open_session()
    store.close_session(sid, input_tokens=5_000_000, output_tokens=5_000_000)
    assert cap.allowed() is False
    assert cap.remaining_usd() == 0.0


def test_old_sessions_excluded_from_24h_window(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    sid = store.open_session()
    store.close_session(sid, input_tokens=1_000_000, output_tokens=1_000_000)
    cost_today = store.spend_last_24h_usd()
    assert cost_today > 0

    # Backdate the session 25 hours.
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (old, sid))
        conn.commit()

    assert store.spend_last_24h_usd() == 0.0
