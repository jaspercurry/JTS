from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jasper.usage import (
    GEMINI_AUDIO_IN_USD_PER_1M,
    GEMINI_AUDIO_OUT_USD_PER_1M,
    OPENAI_REALTIME_MINI_PRICING,
    OPENAI_REALTIME_PRICING,
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


def test_openai_breakdown_priced_correctly(tmp_path: Path):
    """A single OpenAI Realtime tool-using turn should NOT be priced
    as if every input token were audio. The previous implementation
    ($32/M flat across all input) overstated cost by 50–100x for a
    tool turn where the bulk of input is the cached system prompt.

    Realistic numbers from the live Pi at the bug report time: total
    input_tokens ≈ 12000, of which ≈ 1200 are cached system + tool
    defs, ≈ 50 are user audio, and the rest are text history. Expected
    cost: ~$0.005, not $0.40."""
    usage = {
        "input_tokens": 12000,
        "input_token_details": {
            "audio_tokens": 50,
            "text_tokens": 11950,
            "cached_tokens": 1200,
        },
        "output_tokens": 200,
        "output_token_details": {
            "audio_tokens": 80,
            "text_tokens": 120,
        },
    }
    cost = OPENAI_REALTIME_PRICING.estimate_cost(usage)
    # Manual:
    #   uncached_audio_in = 50 - 0    = 50      × $32/M = $0.0016
    #   uncached_text_in  = 11950 - 1200 = 10750 × $4/M  = $0.0430
    #   cached            = 1200             × $0.40/M = $0.00048
    #   audio_out         = 80               × $64/M  = $0.00512
    #   text_out          = 120              × $24/M  = $0.00288
    #   total ≈ $0.0531
    expected = (
        50 * 32.0
        + 10750 * 4.0
        + 1200 * 0.40
        + 80 * 64.0
        + 120 * 24.0
    ) / 1_000_000
    assert abs(cost - expected) < 1e-9
    # Sanity: this should be DRAMATICALLY lower than the all-audio
    # mispricing the previous code produced ($32/M × 12000 + $64/M × 200
    # = $0.397). Anything within 10× of that means we still have the
    # bug.
    all_audio_misprice = (12000 * 32.0 + 200 * 64.0) / 1_000_000
    assert cost < all_audio_misprice / 5, (
        f"breakdown-aware cost ${cost:.4f} should be at least 5× smaller "
        f"than the all-audio misprice ${all_audio_misprice:.4f}"
    )


def test_openai_cached_tokens_priced_at_cached_rate():
    """When 1000 of the 1000 input tokens are cached, cost should
    drop to the cached rate ($0.40/M) instead of audio ($32/M)."""
    fully_cached = OPENAI_REALTIME_PRICING.estimate_cost({
        "input_tokens": 1000,
        "input_token_details": {
            "text_tokens": 1000,
            "cached_tokens": 1000,
        },
        "output_tokens": 0,
        "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
    })
    # 1000 × $0.40/M = $0.0004
    assert abs(fully_cached - 0.0004) < 1e-9


def test_close_session_with_usage_dict_uses_breakdown(tmp_path: Path):
    """``UsageStore.close_session`` accepts a ``usage`` dict and
    should pass it through to ``Pricing.estimate_cost``. Without the
    dict it falls back to the scalar all-audio path (Gemini-style)."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db), pricing=OPENAI_REALTIME_PRICING)

    # WITH breakdown: small expected cost.
    sid = store.open_session()
    breakdown = {
        "input_tokens": 1500,
        "input_token_details": {
            "audio_tokens": 50,
            "text_tokens": 1450,
            "cached_tokens": 1200,
        },
        "output_tokens": 100,
        "output_token_details": {
            "audio_tokens": 50,
            "text_tokens": 50,
        },
    }
    cost_with_breakdown = store.close_session(
        sid, input_tokens=1500, output_tokens=100, usage=breakdown,
    )
    assert cost_with_breakdown < 0.01

    # WITHOUT breakdown (scalar fallback): treats everything as audio.
    sid2 = store.open_session()
    cost_scalar = store.close_session(
        sid2, input_tokens=1500, output_tokens=100,
    )
    # 1500 × $32/M + 100 × $64/M = $0.0544
    assert cost_scalar > cost_with_breakdown * 5


def test_realistic_per_turn_cost_stays_within_bounds():
    """Regression guardrail: a realistic single OpenAI Realtime turn
    must not estimate at >$0.20 on gpt-realtime-2 (or >$0.10 on
    gpt-realtime-mini). Trips on the next schema drift if every input
    token starts being priced as audio again — the bug that produced
    `est $0.1377` per turn on the live Pi before commit 07537ce.

    Numbers represent a typical multi-sentence tool turn after the
    prompt cache has warmed up: ~12k input total, ~1k of audio, the
    rest text history + cached system + tool defs; ~300 output total
    (~150 audio + ~150 transcript)."""
    realistic_turn = {
        "input_tokens": 12000,
        "input_token_details": {
            "audio_tokens": 1000,
            "text_tokens": 11000,
            "cached_tokens": 8000,
        },
        "output_tokens": 300,
        "output_token_details": {
            "audio_tokens": 150,
            "text_tokens": 150,
        },
    }
    cost_2 = OPENAI_REALTIME_PRICING.estimate_cost(realistic_turn)
    assert cost_2 < 0.20, (
        f"gpt-realtime-2 turn estimated at ${cost_2:.4f} — schema drift "
        f"or pricing regression suspected"
    )
    cost_mini = OPENAI_REALTIME_MINI_PRICING.estimate_cost(realistic_turn)
    assert cost_mini < 0.10, (
        f"gpt-realtime-mini turn estimated at ${cost_mini:.4f} — schema "
        f"drift or pricing regression suspected"
    )


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


def test_incompatible_old_schema_is_self_healed(tmp_path: Path):
    """A usage DB predating the `provider` column is wiped & recreated (a
    self-heal, not a migration) so open_session() works instead of
    crashing every turn with 'no such column: provider'."""
    db = tmp_path / "usage.db"
    # Simulate a pre-`provider`-column schema (pre-PR-#85).
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, "
            "ended_at TEXT, input_tokens INTEGER NOT NULL DEFAULT 0, "
            "output_tokens INTEGER NOT NULL DEFAULT 0, "
            "cost_usd REAL NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO sessions (started_at) VALUES ('2026-01-01T00:00:00')"
        )
        conn.commit()

    # Construction self-heals: the incompatible table is dropped & recreated.
    store = UsageStore(str(db))
    sid = store.open_session(provider="openai")
    store.close_session(sid, input_tokens=1, output_tokens=1)

    with sqlite3.connect(str(db)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        rows = conn.execute("SELECT provider FROM sessions").fetchall()
    assert "provider" in cols
    # The stale row was wiped; only the new, correctly-tagged row remains.
    assert rows == [("openai",)]
