# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jasper.usage import (
    AggregateUsageReader,
    BillableActivityMeter,
    USAGE_RETENTION_DAYS,
    _CONNECTION_INTERVALS_TABLE_DDL,
    _SESSIONS_TABLE_DDL,
    _UNRECORDED_SESSION,
    Pricing,
    SpendCap,
    UsageStore,
    household_usage_reader,
    load_default_pricing,
    load_pricing_overrides,
    pricing_for_model,
    tuning_usage_db_path,
)


from jasper.usage_writer import VoiceUsageStore

from tests._log_events import event_fields, event_records
from tests._wake_loop import wake_loop_for_tests


def test_open_and_close_session_records_cost(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    sid = store.open_session()
    cost = store.close_session(sid, input_tokens=10_000, output_tokens=20_000)

    # A bare UsageStore falls back to the cheapest current model (gemini 3/12).
    gemini = pricing_for_model("gemini-3.1-flash-live-preview")
    expected = (
        10_000 * gemini.audio_input_per_million_usd / 1_000_000
        + 20_000 * gemini.audio_output_per_million_usd / 1_000_000
    )
    assert abs(cost - expected) < 1e-9
    assert store.spend_last_24h_usd() == cost


def test_session_writes_fail_soft_on_readonly_db(tmp_path: Path, caplog):
    """A usage-accounting write must NEVER break the voice turn.

    Reproduces the 2026-06-19 outage: usage.db ends up unwritable by
    jasper-voice, so the per-turn INSERT raises "attempt to write a
    readonly database". open_session must swallow it, return the
    _UNRECORDED_SESSION sentinel, and let the caller serve the turn;
    close_session must no-op on that sentinel and also survive a failed
    UPDATE. Neither may raise — a raise here aborted the turn and made
    the daemon play the (false) cant_connect cue."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))  # creates the schema read-write
    # Swap in a read-only handle to the same file: every subsequent
    # write now raises sqlite3.OperationalError, exactly as a DB owned
    # by the wrong user does.
    store._conn.close()
    store._conn = sqlite3.connect(
        f"file:{db}?mode=ro", uri=True, isolation_level=None,
    )

    with caplog.at_level("WARNING"):
        sid = store.open_session(provider="gemini")  # must not raise
    assert sid == _UNRECORDED_SESSION
    assert any("open_session write failed" in r.message for r in caplog.records)

    # close_session on the sentinel is a no-op that still returns a cost
    # estimate and never raises.
    cost = store.close_session(sid, input_tokens=10_000, output_tokens=20_000)
    assert cost >= 0.0

    # A real session id whose UPDATE can't persist also fails soft
    # (returns the estimate, logs, does not raise).
    assert store.close_session(1, input_tokens=1, output_tokens=1) >= 0.0


def test_write_health_tracks_degraded_and_recovers(tmp_path: Path, caplog):
    """UsageStore tracks write-failure health and emits a structured event on
    the TRANSITION into degraded and on recovery — once each, never per-turn —
    so a persistently-unwritable usage.db is monitorable (and surfaced via
    /state.voice.usage_tracking_degraded) instead of buried in per-turn
    WARNings. This is the S1 fix: the spend cap can't enforce while writes fail,
    and now that's observable rather than silent."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    store.close_session(store.open_session(), 1, 1)  # healthy round-trip
    assert store.write_degraded is False

    # Every write now raises (read-only handle to the same file).
    store._conn.close()
    store._conn = sqlite3.connect(
        f"file:{db}?mode=ro", uri=True, isolation_level=None,
    )

    with caplog.at_level("WARNING"):
        assert store.open_session() == _UNRECORDED_SESSION
    assert store.write_degraded is True
    assert len(event_records(caplog, "usage.write_degraded")) == 1, (
        "the ok->degraded transition emits the structured event exactly once"
    )

    # A further failure bumps the counter but must NOT re-emit (no journal spam).
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert store.open_session() == _UNRECORDED_SESSION
    assert store.write_degraded is True
    assert not event_records(caplog, "usage.write_degraded"), (
        "a persistent failure must not re-emit the degraded event"
    )

    # Recovery: writes succeed again -> degraded clears + ONE recovery event.
    store._conn.close()
    store._conn = sqlite3.connect(str(db), isolation_level=None)
    caplog.clear()
    with caplog.at_level("INFO"):
        store.close_session(store.open_session(), 1, 1)
    assert store.write_degraded is False
    assert len(event_records(caplog, "usage.write_recovered")) == 1, (
        "the degraded->ok transition emits the recovery event exactly once"
    )


def test_record_background_usage_records_model_specific_cost(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))

    cost = store.record_background_usage(
        provider="openai",
        model="gpt-5.4-mini",
        input_tokens=1_000,
        output_tokens=100,
        usage={
            "input_tokens": 1_000,
            "output_tokens": 100,
            "input_token_details": {"text_tokens": 1_000},
            "output_token_details": {"text_tokens": 100},
        },
    )

    expected = (1_000 * 0.75 + 100 * 4.50) / 1_000_000
    assert cost == pytest.approx(expected)
    assert store.spend_last_24h_usd() == pytest.approx(expected)


def test_close_session_requires_explicit_session_id(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))

    with pytest.raises(TypeError):
        store.close_session(input_tokens=1, output_tokens=1)  # type: ignore[call-arg]


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
    cost = pricing_for_model("gpt-realtime-2").estimate_cost(usage)
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
    fully_cached = pricing_for_model("gpt-realtime-2").estimate_cost({
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
    store = UsageStore(str(db), pricing=pricing_for_model("gpt-realtime-2"))

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
    cost_2 = pricing_for_model("gpt-realtime-2").estimate_cost(realistic_turn)
    assert cost_2 < 0.20, (
        f"gpt-realtime-2 turn estimated at ${cost_2:.4f} — schema drift "
        f"or pricing regression suspected"
    )
    cost_mini = pricing_for_model("gpt-realtime-mini").estimate_cost(realistic_turn)
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


def test_spend_window_query_uses_started_at_index(tmp_path: Path):
    """The 24h spend window range-scans the started_at index instead of
    running strftime() over every row, on the path production reads through
    (the buffered writer's periodic disk snapshot)."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    store._snapshot([])  # creates the TEMP VIEW the buffered writer reads through
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT COALESCE(SUM(cost_usd), 0) FROM sessions "
        "WHERE started_at >= ?", ("2020-01-01",),
    ).fetchall()
    detail = " ".join(row[-1] for row in plan)
    assert "USING INDEX" in detail
    assert "idx_sessions_started_at" in detail


def test_expired_rows_are_pruned_on_open(tmp_path: Path):
    db = tmp_path / "usage.db"
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=USAGE_RETENTION_DAYS + 1)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(_SESSIONS_TABLE_DDL)
        conn.execute(_CONNECTION_INTERVALS_TABLE_DDL)
        conn.executemany(
            "INSERT INTO sessions (started_at, cost_usd) VALUES (?, ?)",
            [(old, 1.0), (recent, 2.0)],
        )
        conn.executemany(
            "INSERT INTO connection_intervals (provider, opened_at) VALUES (?, ?)",
            [("grok", old), ("grok", recent)],
        )

    store = UsageStore(str(db))  # opening the writer runs the one-time prune
    assert store._conn.execute(
        "SELECT started_at FROM sessions",
    ).fetchall() == [(recent,)]
    assert store._conn.execute(
        "SELECT opened_at FROM connection_intervals",
    ).fetchall() == [(recent,)]


def test_read_only_store_reads_existing_spend(tmp_path: Path):
    """A read-only reopen of a DDL-only DB (created by another surface, no
    rows written yet) must resolve spend without error once rows exist."""
    db = tmp_path / "usage.db"
    UsageStore(str(db))._conn.close()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO sessions (started_at, cost_usd) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), 0.42),
        )
        conn.commit()

    reader = UsageStore(str(db), read_only=True)
    assert reader.spend_last_24h_usd() == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Bundled defaults: real rates AND model-specific (the reason for model keys)
# ---------------------------------------------------------------------------
def test_bundled_defaults_are_current_and_model_specific():
    """Bundled defaults reflect real list rates (gemini 3/12) AND keep
    models distinct: gpt-realtime-1.5 text-out (16) differs from -2 (24) —
    the gap that motivated model-ID keying over provider keying."""
    gemini = pricing_for_model("gemini-3.1-flash-live-preview")
    assert gemini.audio_input_per_million_usd == 3.0
    assert gemini.audio_output_per_million_usd == 12.0
    assert pricing_for_model("gpt-realtime-2").text_output_per_million_usd == 24.0
    assert pricing_for_model("gpt-realtime-1.5").text_output_per_million_usd == 16.0
    mini = pricing_for_model("gpt-5.4-mini")
    assert mini.text_input_per_million_usd == 0.75
    assert mini.text_output_per_million_usd == 4.50
    assert mini.cached_input_per_million_usd == 0.075


def test_unknown_model_is_unpriced_not_invented():
    """A model with no bundled/override rate resolves to all-zero pricing
    labelled 'unpriced:<id>' — we never fabricate a value."""
    p = pricing_for_model("gpt-realtime-3")
    assert p.label == "unpriced:gpt-realtime-3"
    assert p.audio_input_per_million_usd == 0.0
    assert p.audio_output_per_million_usd == 0.0
    assert p.flat_per_hour_usd == 0.0
    # Any usage against an unpriced card costs $0 (surfaced loudly elsewhere).
    assert p.estimate_cost(
        {"input_tokens": 10_000, "output_tokens": 10_000}
    ) == 0.0


def test_load_default_pricing_has_as_of_and_models():
    table, as_of = load_default_pricing()
    assert as_of  # non-empty date string
    assert "gpt-realtime-2" in table
    assert "grok-voice-think-fast-1.0" in table


def test_pricing_for_model_accepts_injected_defaults():
    """`defaults` is a test seam that overrides the bundled table."""
    custom = {"foo-1": Pricing(
        audio_input_per_million_usd=1.0, audio_output_per_million_usd=2.0,
        label="foo-1",
    )}
    p = pricing_for_model("foo-1", defaults=custom)
    assert p.audio_output_per_million_usd == 2.0


# ---------------------------------------------------------------------------
# SpendCap safety multiplier
# ---------------------------------------------------------------------------
def test_spend_cap_safety_multiplier_pads_breaker_not_storage(tmp_path: Path):
    """The stored/displayed spend is a true estimate; the cap pads it at
    read time. A true spend of $0.60 fits under a $1.00 cap at x1.0 but
    trips it at x2.0 (0.60*2 = 1.20) — without changing what the
    dashboard would show."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))  # default Gemini pricing: 3/12 per M
    sid = store.open_session()
    # 50_000 output tokens × $12/M = exactly $0.60; no input.
    store.close_session(sid, input_tokens=0, output_tokens=50_000)
    assert abs(store.spend_last_24h_usd() - 0.60) < 1e-9

    lenient = SpendCap(store, cap_usd=1.00, safety_multiplier=1.0)
    assert lenient.allowed() is True
    strict = SpendCap(store, cap_usd=1.00, safety_multiplier=2.0)
    assert strict.allowed() is False
    assert strict.remaining_usd() == 0.0
    # The multiplier does not mutate stored cost — display stays honest.
    assert abs(store.spend_last_24h_usd() - 0.60) < 1e-9


def test_spend_cap_default_multiplier_is_one(tmp_path: Path):
    """Callers that omit the multiplier (tests, incidental callers) get
    no padding, so behaviour is unsurprising."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    sid = store.open_session()
    store.close_session(sid, input_tokens=0, output_tokens=50_000)  # $0.60
    cap = SpendCap(store, cap_usd=0.61)
    assert cap.allowed() is True  # 0.60 * 1.0 < 0.61


def test_spend_cap_multiplier_floored_at_one_never_disables(tmp_path: Path):
    """A safety multiplier below 1.0 (incl. 0) is floored to 1.0 so it can
    never weaken — let alone disable — the cap. A 0 multiplier would
    otherwise zero the padded spend and silently turn the breaker off;
    disabling is solely JASPER_DAILY_SPEND_CAP_USD=0's job."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    sid = store.open_session()
    store.close_session(sid, input_tokens=0, output_tokens=50_000)  # $0.60
    capped = SpendCap(store, cap_usd=0.50, safety_multiplier=0.0)
    assert capped.allowed() is False  # floored to 1.0 → 0.60 >= 0.50
    assert capped.remaining_usd() == 0.0


# ---------------------------------------------------------------------------
# SpendCap disabled state — JASPER_DAILY_SPEND_CAP_USD=0
# ---------------------------------------------------------------------------
def test_spend_cap_zero_means_disabled_never_blocks(tmp_path: Path, caplog):
    """cap_usd=0 is the documented disable value (Config validation
    text, .env.example, the /voice wizard). It must allow every wake —
    the pre-fix `padded < 0.0` comparison blocked all of them. The
    disabled posture is logged once at construction, never per-wake."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    sid = store.open_session()
    store.close_session(sid, input_tokens=0, output_tokens=50_000)  # $0.60

    with caplog.at_level("WARNING", logger="jasper.usage"):
        cap = SpendCap(store, cap_usd=0.0, safety_multiplier=1.25)
    assert cap.disabled is True
    assert cap.allowed() is True
    # remaining is meaningless without a ceiling; display surfaces
    # branch on `disabled` — the value itself stays a harmless 0.0.
    assert cap.remaining_usd() == 0.0
    # Logged exactly once, at construction.
    assert len(event_records(caplog, "spend_cap.disabled")) == 1
    caplog.clear()
    with caplog.at_level("WARNING", logger="jasper.usage"):
        for _ in range(5):  # per-wake calls must not re-log
            assert cap.allowed() is True
    assert not event_records(caplog, "spend_cap.disabled")


def test_spend_cap_negative_treated_as_disabled(tmp_path: Path):
    """Config.from_env rejects negatives, but a directly-constructed
    SpendCap (doctor with a hand-built Config, tests) should not turn a
    nonsensical negative ceiling into a permanent block — it degrades to
    the same disabled posture as 0."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    cap = SpendCap(store, cap_usd=-1.0)
    assert cap.disabled is True
    assert cap.allowed() is True


def test_spend_cap_tiny_positive_still_enforces(tmp_path: Path):
    """The disabled carve-out is exactly cap <= 0 — any positive cap,
    however small, keeps the breaker armed."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    cap = SpendCap(store, cap_usd=0.0001)
    assert cap.disabled is False
    assert cap.allowed() is True  # nothing spent yet
    sid = store.open_session()
    store.close_session(sid, input_tokens=0, output_tokens=50_000)  # $0.60
    assert cap.allowed() is False
    assert cap.remaining_usd() == 0.0


# ---------------------------------------------------------------------------
# Optional pricing.json override (model-ID keyed)
# ---------------------------------------------------------------------------
def test_pricing_override_missing_file_uses_defaults():
    overrides = load_pricing_overrides("/nonexistent/dir/pricing.json")
    assert overrides == {}
    p = pricing_for_model("gpt-realtime-2", overrides=overrides)
    assert p.text_output_per_million_usd == 24.0  # bundled default


def test_pricing_override_applies_per_model(tmp_path: Path):
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"models": {
        "gemini-3.1-flash-live-preview": {
            "audio_input_per_million_usd": 99.0,
            "audio_output_per_million_usd": 88.0,
        },
        "grok-voice-think-fast-1.0": {"flat_per_hour_usd": 5.0},
    }}))
    ov = load_pricing_overrides(str(f))
    g = pricing_for_model("gemini-3.1-flash-live-preview", overrides=ov)
    assert g.audio_input_per_million_usd == 99.0
    assert g.audio_output_per_million_usd == 88.0
    assert g.label == "gemini-3.1-flash-live-preview"  # label not overridable
    grok = pricing_for_model("grok-voice-think-fast-1.0", overrides=ov)
    assert grok.flat_per_hour_usd == 5.0
    # A model absent from the override keeps its bundled default.
    p = pricing_for_model("gpt-realtime-2", overrides=ov)
    assert p.text_output_per_million_usd == 24.0


def test_pricing_override_is_sparse_overlay(tmp_path: Path):
    """Only the named field changes; the rest of the model's bundled rate
    card is preserved."""
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps(
        {"models": {"gpt-realtime-2": {"text_output_per_million_usd": 28.0}}}
    ))
    ov = load_pricing_overrides(str(f))
    p = pricing_for_model("gpt-realtime-2", overrides=ov)
    assert p.text_output_per_million_usd == 28.0   # overridden
    assert p.audio_input_per_million_usd == 32.0    # bundled default kept


def test_pricing_override_stale_provider_keyed_file_is_ignored(tmp_path: Path):
    """A pre-existing provider-keyed file (no 'models' map) degrades to
    bundled defaults — no migration code, no crash."""
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"gemini": {"audio_input_per_million_usd": 1.0}}))
    assert load_pricing_overrides(str(f)) == {}


def test_pricing_override_malformed_falls_back(tmp_path: Path):
    f = tmp_path / "pricing.json"
    f.write_text("{ not valid json ")
    assert load_pricing_overrides(str(f)) == {}


def test_pricing_override_ignores_unknown_and_nonnumeric(tmp_path: Path):
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"models": {
        "gpt-realtime-2": {
            "text_output_per_million_usd": 7.0,
            "label": "hacked",                       # not overridable
            "bogus_field": 1.0,                      # unknown field
            "audio_output_per_million_usd": "lots",  # non-numeric
            "cached_input_per_million_usd": True,    # bool is not a rate
        },
    }}))
    ov = load_pricing_overrides(str(f))
    p = pricing_for_model("gpt-realtime-2", overrides=ov)
    assert p.text_output_per_million_usd == 7.0
    assert p.label == "gpt-realtime-2"
    assert p.audio_output_per_million_usd == 64.0  # bundled default kept
    assert p.cached_input_per_million_usd == 0.40  # bundled default kept


# ---------------------------------------------------------------------------
# Billable realtime-activity metering (time-billed providers, e.g. Grok)
# ---------------------------------------------------------------------------
def _insert_interval(db_path, provider, opened, closed, rate):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO connection_intervals "
            "(provider, opened_at, closed_at, rate_per_hour_usd) "
            "VALUES (?, ?, ?, ?)",
            (provider, opened, closed, rate),
        )
        conn.commit()


def test_connection_interval_cost_in_24h_spend(tmp_path: Path):
    """A 30-minute Grok activity interval at $3/hour contributes $1.50 to the
    rolling spend even though its token rows price to $0."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    now = datetime.now(timezone.utc)
    _insert_interval(
        db, "grok",
        (now - timedelta(minutes=30)).isoformat(), now.isoformat(), 3.0,
    )
    assert abs(store.spend_last_24h_usd() - 1.50) < 1e-3


def test_open_interval_billed_up_to_now(tmp_path: Path):
    """An interval still open (closed_at NULL) bills up to the present —
    an active turn's ongoing cost shows immediately."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    now = datetime.now(timezone.utc)
    _insert_interval(
        db, "grok", (now - timedelta(hours=1)).isoformat(), None, 3.0,
    )
    assert abs(store.spend_last_24h_usd() - 3.0) < 0.05


def test_billable_activity_meter_records_start_then_end(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    meter = BillableActivityMeter(store, "grok", 3.0)
    meter.mark_started()
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT provider, closed_at, rate_per_hour_usd, kind "
            "FROM connection_intervals"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("grok", None, 3.0, "billable_activity")
    meter.mark_ended()
    with sqlite3.connect(str(db)) as conn:
        closed = conn.execute(
            "SELECT closed_at FROM connection_intervals"
        ).fetchone()[0]
    assert closed is not None


def test_dangling_interval_closed_at_meter_start(tmp_path: Path):
    """A crash leaves an interval open. The next meter construction
    closes it conservatively (zero duration) so a stale open row can't
    bill phantom activity up to 'now'."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _insert_interval(db, "grok", opened, None, 3.0)
    # Before cleanup the open interval would bill ~2h = ~$6.
    assert store.spend_last_24h_usd() > 5.0
    BillableActivityMeter(store, "grok", 3.0)  # runs dangling cleanup
    assert store.spend_last_24h_usd() < 1e-6


def test_legacy_connection_uptime_rows_are_tagged_and_ignored(tmp_path: Path):
    """Rows written by the old idle-WebSocket meter must not keep the cap
    tripped after upgrade. The migration preserves them for forensics but
    tags them out of spend queries."""
    db = tmp_path / "usage.db"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(_SESSIONS_TABLE_DDL)
        conn.execute(
            """
            CREATE TABLE connection_intervals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                rate_per_hour_usd REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO connection_intervals "
            "(provider, opened_at, closed_at, rate_per_hour_usd) "
            "VALUES (?, ?, ?, ?)",
            (
                "grok",
                (now - timedelta(hours=8)).isoformat(),
                now.isoformat(),
                3.0,
            ),
        )

    store = UsageStore(str(db))
    assert store.spend_last_24h_usd() == 0.0
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT kind FROM connection_intervals"
        ).fetchall()
    assert rows == [("legacy_connection_uptime",)]
    meter = BillableActivityMeter(store, "grok", 3.0)
    meter.mark_started()
    meter.mark_ended()
    with sqlite3.connect(str(db)) as conn:
        kinds = conn.execute(
            "SELECT kind FROM connection_intervals ORDER BY id"
        ).fetchall()
    assert kinds == [
        ("legacy_connection_uptime",),
        ("billable_activity",),
    ]


def test_read_only_old_interval_schema_ignores_legacy_rows(tmp_path: Path):
    """The /voice status card may read an old DB before the daemon writer
    migrates it; that read-only path must fail open to $0, not crash."""
    db = tmp_path / "usage.db"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(_SESSIONS_TABLE_DDL)
        conn.execute(
            """
            CREATE TABLE connection_intervals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                rate_per_hour_usd REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO connection_intervals "
            "(provider, opened_at, closed_at, rate_per_hour_usd) "
            "VALUES (?, ?, ?, ?)",
            (
                "grok",
                (now - timedelta(hours=8)).isoformat(),
                now.isoformat(),
                3.0,
            ),
        )

    store = UsageStore(str(db), read_only=True)
    assert store.spend_last_24h_usd() == 0.0


def test_time_billed_provider_bills_no_token_cost(tmp_path: Path):
    """Grok is billed for connected time, not tokens: a closed session with
    2,000 tokens must add nothing on top of the flat interval rate.

    model_pricing.json gives grok-voice-think-fast-1.0 only
    flat_per_hour_usd, and _pricing_from_fields defaults every token rate to
    0.0 -- so adding any per-million rate there would silently bill a Grok
    turn twice and trip the daily cap early."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db), pricing=pricing_for_model("grok-voice-think-fast-1.0"))
    sid = store.open_session(provider="grok")
    store.close_session(sid, input_tokens=1_000, output_tokens=1_000)

    now = datetime.now(timezone.utc)
    _insert_interval(
        db, "grok", (now - timedelta(hours=1)).isoformat(), now.isoformat(), 3.0,
    )

    assert store.spend_last_24h_usd() == pytest.approx(3.0, abs=1e-3)


def test_token_billed_provider_has_no_intervals(tmp_path: Path):
    """Sanity: a provider with no connection intervals (OpenAI/Gemini)
    gets zero time-billed cost — the meter is never wired for them."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    now = datetime.now(timezone.utc)
    assert store._time_billed_spend(now - timedelta(hours=24), now) == 0.0


# --- Catalog ↔ bundled pricing completeness ---------------------------------
#
# pricing_for_model() deliberately never invents a rate: an unknown model
# resolves to an all-zero Pricing labelled ``unpriced:<id>``, which means
# the daily spend cap silently never accrues for that model. The bundled
# table's own contract (jasper/data/model_pricing.json `_comment`) is
# "Bundled voice rates ... keyed by exact model ID", so every
# model the /voice wizard offers from the catalog must have an entry —
# otherwise picking a curated model quietly disables spend accounting
# until the runtime doctor warning is noticed.


def test_every_catalog_model_has_bundled_pricing():
    from jasper.voice.catalog import PROVIDERS

    table, _as_of = load_default_pricing()
    for provider in PROVIDERS:
        for model in provider.models:
            assert model.id in table, (
                f"{provider.id} catalog model {model.id!r} has no entry in "
                "jasper/data/model_pricing.json — pricing_for_model() would "
                "return the all-zero unpriced fallback and the daily spend "
                "cap would never accrue for it. Add the rate (and bump "
                "as_of) in the same PR that adds the model."
            )
            pricing = pricing_for_model(model.id, defaults=table)
            assert not pricing.label.startswith("unpriced:")


# ---------------------------------------------------------------------------
# read_only mode — status surfaces (the /voice spend card, jasper-doctor) run
# as root, not jasper-voice. A read-WRITE open from them can re-own usage.db
# and lock the voice daemon out of its own DB (open_session then raises
# "attempt to write a readonly database" on every wake → the cant_connect
# cue). These tests pin that read_only=True never creates and never writes.
# Regression for the 2026-06-16 outage.
# ---------------------------------------------------------------------------
def test_read_only_does_not_create_missing_db(tmp_path: Path):
    db = tmp_path / "usage.db"
    assert not db.exists()
    with pytest.raises(sqlite3.OperationalError):
        UsageStore(str(db), read_only=True)
    # The whole point: a reader must not bring the file into existence (a
    # root-created file would lock jasper-voice out).
    assert not db.exists()


def test_read_only_cannot_write(tmp_path: Path):
    db = tmp_path / "usage.db"
    UsageStore(str(db))  # create schema as the writer
    ro = UsageStore(str(db), read_only=True)
    # open_session is fail-soft as of 2026-06-19: it must NOT raise — a
    # raise on the turn-open hot path aborted the turn and fired a false
    # cant_connect cue. The read-only connection must still genuinely
    # reject the write, so a reader can never create/mutate usage.db (the
    # 2026-06-16 protection): the call returns the unrecorded sentinel and
    # no session row is persisted.
    assert ro.open_session(provider="openai") == _UNRECORDED_SESSION
    assert ro.session_count_today_utc() == 0


def test_read_only_reads_existing_spend(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db), pricing=pricing_for_model("gpt-realtime-2"))
    sid = store.open_session(provider="openai")
    cost = store.close_session(sid, input_tokens=10_000, output_tokens=2_000)

    ro = UsageStore(str(db), read_only=True)
    assert ro.spend_last_24h_usd() == cost
    assert ro.spend_month_to_date_usd() == cost
    assert ro.session_count_today_utc() == 1
    # And the breaker reads correctly through the read-only store.
    assert SpendCap(ro, cap_usd=cost / 2).allowed() is False
    assert SpendCap(ro, cap_usd=cost * 10).allowed() is True


def test_read_only_open_performs_no_writes(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    store.close_session(store.open_session(provider="openai"), 1_000, 100)
    before = db.read_bytes()

    ro = UsageStore(str(db), read_only=True)
    _ = ro.spend_last_24h_usd()  # exercise a read

    # No rollback/WAL sidecars created, and the DB file is byte-identical:
    # a reader must leave the writer's file untouched.
    assert not os.path.exists(str(db) + "-journal")
    assert not os.path.exists(str(db) + "-wal")
    assert db.read_bytes() == before

# ---------------------------------------------------------------------------
# Household aggregation seam — AggregateUsageReader + household_usage_reader
# ---------------------------------------------------------------------------


def _record_cost(db_path: str, cost_usd: float, *, provider: str = "openai") -> None:
    """Write one closed session with an explicit cost into a usage DB."""
    store = UsageStore(db_path)
    sid = store.open_session(provider=provider)
    # Force the row's stored cost to a known value regardless of pricing.
    store._conn.execute(
        "UPDATE sessions SET ended_at = ?, cost_usd = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), cost_usd, sid),
    )
    store._conn.close()


def test_tuning_db_is_sibling_of_usage_db():
    assert tuning_usage_db_path("/var/lib/jasper/usage.db") == (
        "/var/lib/jasper/usage-tuning.db"
    )


def test_aggregate_sums_across_two_dbs(tmp_path: Path):
    a = tmp_path / "usage.db"
    b = tmp_path / "usage-tuning.db"
    _record_cost(str(a), 0.30)
    _record_cost(str(b), 0.12)
    reader = household_usage_reader(str(a))  # members: [a, sibling b]
    assert reader.spend_last_24h_usd() == pytest.approx(0.42)
    assert reader.spend_month_to_date_usd() == pytest.approx(0.42)
    assert reader.session_count_today_utc() == 2


def test_aggregate_missing_second_member_counts_zero(tmp_path: Path):
    a = tmp_path / "usage.db"
    _record_cost(str(a), 0.30)
    # The tuning sibling does not exist yet.
    reader = household_usage_reader(str(a))
    assert reader.spend_last_24h_usd() == pytest.approx(0.30)
    assert reader.session_count_today_utc() == 1


def test_aggregate_picks_up_member_created_after_first_read(tmp_path: Path):
    """The lazy-reopen pin: a long-lived voice daemon reads its aggregate for
    weeks; a tuning DB created LATER must be summed on the next read, with no
    restart and no cached failed-open."""
    a = tmp_path / "usage.db"
    b = tmp_path / "usage-tuning.db"
    _record_cost(str(a), 0.30)
    reader = household_usage_reader(str(a))
    assert reader.spend_last_24h_usd() == pytest.approx(0.30)  # b absent
    # Now the tuning surface creates + writes its ledger.
    _record_cost(str(b), 0.12)
    assert reader.spend_last_24h_usd() == pytest.approx(0.42)  # picked up


def test_aggregate_with_live_main_store_reflects_unwritten_reopen(tmp_path: Path):
    """When a live writer store is passed as main_store, its recorded spend is
    visible through the reader's own connection (not a stale read-only reopen),
    plus the tuning sibling path."""
    a = tmp_path / "usage.db"
    b = tmp_path / "usage-tuning.db"
    main = UsageStore(str(a), pricing=pricing_for_model("gpt-realtime-2"))
    cost = main.close_session(main.open_session(provider="openai"), 10_000, 2_000)
    _record_cost(str(b), 0.05)
    reader = household_usage_reader(str(a), main_store=main)
    assert reader.spend_last_24h_usd() == pytest.approx(cost + 0.05)


def test_aggregate_multiplier_applies_once_through_spend_cap(tmp_path: Path):
    """SpendCap over the aggregate pads the summed spend exactly once."""
    a = tmp_path / "usage.db"
    b = tmp_path / "usage-tuning.db"
    _record_cost(str(a), 0.40)
    _record_cost(str(b), 0.40)  # household = 0.80
    reader = household_usage_reader(str(a))
    # 0.80 * 1.25 = 1.00 padded; cap 0.99 -> blocked, cap 1.01 -> allowed.
    assert SpendCap(reader, cap_usd=0.99, safety_multiplier=1.25).allowed() is False
    assert SpendCap(reader, cap_usd=1.01, safety_multiplier=1.25).allowed() is True


def test_aggregate_unreadable_member_counts_zero_not_raise(tmp_path: Path, caplog):
    """A corrupt/unopenable member contributes zero (fail-open), logged at
    DEBUG — never raising, never WARN spam."""
    a = tmp_path / "usage.db"
    b = tmp_path / "usage-tuning.db"
    _record_cost(str(a), 0.30)
    b.write_bytes(b"not a sqlite database at all")  # exists but unreadable
    reader = household_usage_reader(str(a))
    with caplog.at_level("WARNING"):
        total = reader.spend_last_24h_usd()
    assert total == pytest.approx(0.30)  # only the good member counted
    # Nothing at WARNING or above — the noise stays at DEBUG.
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


def test_background_usage_without_modality_details_would_be_zero_dollars(
    tmp_path: Path,
):
    """Proves WHY the caller-side synthesis is load-bearing: the SAME token
    counts priced WITHOUT modality details price at gpt-5.4's (absent) audio
    rate -> $0."""
    db = str(tmp_path / "usage.db")
    store = UsageStore(db)
    naive = store.record_background_usage(
        provider="openai", model="gpt-5.4",
        input_tokens=1000, output_tokens=1000,  # no usage= details
    )
    assert naive == pytest.approx(0.0)


def test_read_only_ctor_closes_connection_on_corrupt_file(
    tmp_path: Path, monkeypatch,
):
    """Exception safety in UsageStore.__init__: sqlite3.connect is lazy, so a
    corrupt member file only raises on the first post-connect probe — the
    half-built store's connection must be CLOSED before the raise, not left
    for a GC pass to reclaim (review-proven +1 FD per aggregate read with gc
    disabled, on the voice daemon's wake-fire cap check)."""
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not a sqlite database at all")

    created: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    monkeypatch.setattr("jasper.usage.sqlite3.connect", spy_connect)
    with pytest.raises(sqlite3.Error):
        UsageStore(str(db), read_only=True)
    assert len(created) == 1
    # Executing on a closed connection raises ProgrammingError — the
    # deterministic "was closed" probe.
    with pytest.raises(sqlite3.ProgrammingError, match="[Cc]losed"):
        created[0].execute("SELECT 1")

    # And the aggregate stays fail-open over the same corrupt member: zero
    # contribution, no raise (its per-read open now cannot leak the FD).
    reader = AggregateUsageReader(paths=[str(db)])
    assert reader.spend_last_24h_usd() == 0.0


async def _wait_usage(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("locked", [False, True])
async def test_buffered_usage_keeps_receipt_time_cost_and_attribution(tmp_path, locked):
    db = str(tmp_path / "usage.db")
    store = await VoiceUsageStore.start(db, pricing=pricing_for_model("gpt-realtime-2"))
    lock = sqlite3.connect(db, isolation_level=None)
    try:
        if locked:
            lock.execute("BEGIN IMMEDIATE")
        start = datetime.now(timezone.utc)
        tick = asyncio.create_task(asyncio.sleep(0.01))
        began = time.monotonic()
        first = store.open_session("openai")
        cost = store.close_session(first, 1000, 2000)
        second = store.open_session("gemini")
        meter = BillableActivityMeter(store, "grok", 3600)
        meter.mark_started()
        await tick
        assert time.monotonic() - began < 0.1
        assert store.spend_last_24h_usd() >= cost
        assert not SpendCap(store, cost / 2).allowed()
        assert store.session_count_today_utc() == 2
        await asyncio.sleep(0.05)
        meter.mark_ended()
        end = datetime.now(timezone.utc)
        total = store.spend_last_24h_usd()
        assert store.spend_month_to_date_usd() == pytest.approx(total)
        store.close_session(second, 0, 0)
        await asyncio.sleep(0.15)
        assert store.spend_last_24h_usd() == pytest.approx(total)
    finally:
        lock.close()
        await store.aclose()
    assert not store.write_degraded
    with sqlite3.connect(db) as conn:
        sessions = conn.execute(
            "SELECT id, provider, started_at, ended_at, cost_usd FROM sessions "
            "ORDER BY started_at",
        ).fetchall()
        assert [(r[0], r[1], r[4]) for r in sessions] == [
            (first, "openai", cost), (second, "gemini", 0),
        ]
        assert start <= datetime.fromisoformat(sessions[0][2]) <= end
        assert start <= datetime.fromisoformat(sessions[0][3]) <= end
        opened, closed = conn.execute(
            "SELECT opened_at, closed_at FROM connection_intervals",
        ).fetchone()
        assert start <= datetime.fromisoformat(opened) < datetime.fromisoformat(closed) <= end
    reader = UsageStore(db, read_only=True)
    try:
        assert reader.spend_last_24h_usd() == pytest.approx(total)
    finally:
        reader._conn.close()


async def test_buffered_queue_reserves_closes_and_discloses_loss(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(VoiceUsageStore, "_MAX_PENDING", 2)
    db = str(tmp_path / "usage.db")
    store = await VoiceUsageStore.start(db)
    lock = sqlite3.connect(db, isolation_level=None)
    try:
        lock.execute("BEGIN IMMEDIATE")
        accepted = store.open_session("openai")
        meter = BillableActivityMeter(store, "grok", 3600)
        meter.mark_started()
        await asyncio.sleep(0.01)
        for _ in range(4):
            store.close_session(store.open_session("overflow"), 1000, 2000)
        meter.mark_ended()
        cost = store.close_session(accepted, 1000, 2000)
        assert len(store._pending) == 2
        assert store.write_degraded
        assert store.spend_last_24h_usd() >= cost
        await _wait_usage(lambda: bool(event_records(caplog, "usage.queue_overflow")))
        assert event_fields(caplog, "usage.queue_overflow")["lost"] == "4"
    finally:
        lock.close()
        await store.aclose()
    assert store.write_degraded  # Saving other rows cannot repair the missing history.
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT id, cost_usd FROM sessions").fetchall() == [(accepted, cost)]
        opened, closed = conn.execute(
            "SELECT opened_at, closed_at FROM connection_intervals",
        ).fetchone()
        assert datetime.fromisoformat(closed) > datetime.fromisoformat(opened)


@pytest.mark.parametrize("recover", [False, True])
async def test_buffered_write_errors_and_shutdown_are_bounded(tmp_path, monkeypatch, caplog, recover):
    monkeypatch.setattr(VoiceUsageStore, "_DRAIN_SECONDS", 0.1)
    db = str(tmp_path / "usage.db")
    store = await VoiceUsageStore.start(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER reject_usage BEFORE INSERT ON sessions "
                     "BEGIN SELECT RAISE(FAIL, 'test'); END")
    sid = store.open_session("openai")
    cost = store.close_session(sid, 1000, 2000)
    await _wait_usage(lambda: store._write_error is not None)
    assert store.write_degraded
    assert store.spend_last_24h_usd() == pytest.approx(cost)
    if recover:
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TRIGGER reject_usage")
        await _wait_usage(lambda: not store.write_degraded)
    began = time.monotonic()
    await store.aclose()
    assert time.monotonic() - began < 0.3
    assert not store._thread.is_alive()
    assert store.write_degraded is not recover
    if not recover:
        assert event_fields(caplog, "usage.drain_incomplete")["pending"] == "1"
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT id, cost_usd FROM sessions").fetchall()
    assert rows == ([(sid, cost)] if recover else [])


async def test_buffered_household_refresh_is_read_only_and_keeps_last_good_spend(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceUsageStore, "_REFRESH_SECONDS", 0.05)
    db = str(tmp_path / "usage.db")
    store = await VoiceUsageStore.start(db)
    household = store
    tuning = str(tmp_path / "usage-tuning.db")
    try:
        assert not Path(tuning).exists()
        _record_cost(tuning, 0.4)
        await _wait_usage(lambda: household.spend_last_24h_usd() >= 0.4)
        cap = SpendCap(household, 0.45)
        assert cap.allowed()
        own_cost = store.close_session(store.open_session(), 10000, 10000)
        assert not cap.allowed()
        contents = Path(tuning).read_bytes()
        with sqlite3.connect(tuning, isolation_level=None) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            await _wait_usage(lambda: store.write_degraded)
            start = time.monotonic()
            assert household.spend_last_24h_usd() == pytest.approx(0.4 + own_cost)
            assert time.monotonic() - start < 0.05
            lock.rollback()
        await _wait_usage(lambda: not store.write_degraded)
        assert Path(tuning).read_bytes() == contents
        _record_cost(tuning, 0.2)
        await _wait_usage(lambda: household.spend_last_24h_usd() >= 0.6 + own_cost - 1e-9)
        assert household.session_count_today_utc() == 3
    finally:
        await store.aclose()


async def test_buffered_start_recovers_history_without_rebilling_crash_interval(tmp_path, monkeypatch):
    db = str(tmp_path / "usage.db")
    _record_cost(db, 0.25)
    disk = UsageStore(db)
    disk.record_billable_activity_open("grok", 3600)
    disk._conn.close()
    lock = sqlite3.connect(db, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    store = await VoiceUsageStore.start(db)
    refreshing, release = threading.Event(), threading.Event()
    snapshot = UsageStore._snapshot

    def delayed_snapshot(writer, excluded):
        closed_at = writer._conn.execute(
            "SELECT closed_at FROM connection_intervals"
        ).fetchone()[0]
        if closed_at is not None:
            refreshing.set()
            assert release.wait(2)
        return snapshot(writer, excluded)

    monkeypatch.setattr(UsageStore, "_snapshot", delayed_snapshot)
    try:
        BillableActivityMeter(store, "grok", 3600)
        assert store.write_degraded
        sid = store.open_session("openai")
        cost = store.close_session(sid, 1000, 1000)
        lock.close()
        await _wait_usage(refreshing.is_set)
        assert store.write_degraded
        release.set()
        await _wait_usage(lambda: not store.write_degraded)
        assert store.spend_last_24h_usd() == pytest.approx(0.25 + cost)
        assert store.session_count_today_utc() == 2
    finally:
        release.set()
        lock.close()
        await store.aclose()


async def test_usage_lock_does_not_delay_live_turn_acquisition(tmp_path):
    db = str(tmp_path / "usage.db")
    store = await VoiceUsageStore.start(db)
    wl = wake_loop_for_tests(usage_store=store)
    wl._spend_cap = SpendCap(store, 1)
    wl._content_activity.refresh_now = AsyncMock()
    wl._connection.acquire_turn = AsyncMock(side_effect=RuntimeError())
    try:
        with sqlite3.connect(db, isolation_level=None) as lock:
            lock.execute("BEGIN IMMEDIATE")
            began = time.monotonic()
            tick = asyncio.create_task(asyncio.sleep(0.01))
            with pytest.raises(RuntimeError):
                await wl._turns.begin_inner(pre_roll=False)
            wl._connection.acquire_turn.assert_awaited_once()
            assert wl._turns.session_id != _UNRECORDED_SESSION
            store.close_session(wl._turns.session_id, 0, 0)
            await tick
            assert time.monotonic() - began < 0.1
            lock.rollback()
    finally:
        await store.aclose()


async def test_buffered_start_cancellation_stops_its_worker(tmp_path, monkeypatch):
    db = str(tmp_path / "usage.db")
    UsageStore(db)._conn.close()
    instances = []
    original_init = VoiceUsageStore.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(VoiceUsageStore, "__init__", capture)
    with sqlite3.connect(db, isolation_level=None) as lock:
        lock.execute("BEGIN EXCLUSIVE")
        starting = asyncio.create_task(VoiceUsageStore.start(db))
        await asyncio.sleep(0.01)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        lock.rollback()
    assert len(instances) == 1
    assert not instances[0]._thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        instances[0]._conn.execute("SELECT 1")


async def test_buffered_history_and_concurrent_writer_refresh_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceUsageStore, "_REFRESH_SECONDS", 0.02)
    db = str(tmp_path / "usage.db")
    seed = UsageStore(db)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO sessions (started_at, cost_usd) VALUES (?, ?)",
            [(now.isoformat(), 0.001)] * 1000,
        )
    seed._conn.close()
    first = await VoiceUsageStore.start(db)
    second = await VoiceUsageStore.start(db)
    try:
        await _wait_usage(lambda: first.spend_last_24h_usd() == pytest.approx(1))
        assert first.session_count_today_utc() == 1000
        ids = [s.open_session("concurrent") for s in (first, second)]
        assert len(set(ids)) == 2
        costs = [s.close_session(sid, 1000, 1000) for s, sid in zip((first, second), ids)]
        expected = 1 + sum(costs)
        for s in (first, second):
            # Spend is unchanged by the retire (the memory row leaves as the
            # durable one enters), so only an empty ledger orders the count.
            await _wait_usage(
                lambda: not s._pending
                and abs(s.spend_last_24h_usd() - expected) < 1e-9,
            )
            assert s.spend_month_to_date_usd() == pytest.approx(expected)
            assert s.session_count_today_utc() == 1002
            assert s._conn.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
    finally:
        await first.aclose()
        await second.aclose()
    restarted = await VoiceUsageStore.start(db)
    try:
        await _wait_usage(lambda: restarted.spend_last_24h_usd() == pytest.approx(expected))
        with sqlite3.connect(db) as conn:
            assert {r[0] for r in conn.execute("SELECT id FROM sessions WHERE provider = 'concurrent'")} == set(ids)
    finally:
        await restarted.aclose()


async def test_buffered_snapshots_follow_day_month_and_rolling_windows(tmp_path, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr("jasper.usage.datetime", Clock)
    monkeypatch.setattr(VoiceUsageStore, "_REFRESH_SECONDS", 0.02)
    db = str(tmp_path / "usage.db")
    UsageStore(db)._conn.close()
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO sessions (started_at, cost_usd) VALUES (?, ?)",
            [("2026-09-01T00:00:00+00:00", 0.4),
             ("2026-09-30T00:00:00+00:00", 0.2),
             ("2026-09-30T23:59:00+00:00", 0.3)],
        )
    store = await VoiceUsageStore.start(db)
    try:
        assert store.spend_last_24h_usd() == pytest.approx(0.5)
        assert store.spend_month_to_date_usd() == pytest.approx(0.9)
        assert store.session_count_today_utc() == 2
        Clock.current = datetime(2026, 10, 1, 0, 0, 1, tzinfo=timezone.utc)
        await _wait_usage(lambda: abs(store.spend_last_24h_usd() - 0.3) < 1e-9)
        assert store.spend_month_to_date_usd() == 0
        assert store.session_count_today_utc() == 0
    finally:
        await store.aclose()


async def test_unreadable_companion_cannot_fill_the_voice_queue(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(VoiceUsageStore, "_MAX_PENDING", 2)
    db = str(tmp_path / "usage.db")
    (tmp_path / "usage-tuning.db").write_bytes(b"not sqlite")
    store = await VoiceUsageStore.start(db)
    total = 0.0
    try:
        for _ in range(6):
            sid = store.open_session("openai")
            assert sid != _UNRECORDED_SESSION
            total += store.close_session(sid, 1000, 1000)
            await _wait_usage(lambda: not store._pending)
        assert store.write_degraded
        assert store.spend_last_24h_usd() == pytest.approx(total)
        assert store.session_count_today_utc() == 6
        assert store._lost == 0
    finally:
        await store.aclose()
    assert not event_records(caplog, "usage.drain_incomplete")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT sum(cost_usd) FROM sessions").fetchone()[0] == pytest.approx(total)


async def test_starting_another_writer_preserves_live_billable_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceUsageStore, "_REFRESH_SECONDS", 0.02)
    db = str(tmp_path / "usage.db")
    first = await VoiceUsageStore.start(db)
    meter = BillableActivityMeter(first, "grok", 3600)
    meter.mark_started()
    try:
        await _wait_usage(lambda: not first._dirty and not first._cleanup_requested)
        await asyncio.sleep(0.05)
        second = await VoiceUsageStore.start(db)
        try:
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT closed_at FROM connection_intervals").fetchone() == (None,)
            assert second.spend_last_24h_usd() >= 0.05
            meter.mark_ended()
            total = first.spend_last_24h_usd()
            await _wait_usage(lambda: abs(second.spend_last_24h_usd() - total) < 1e-9)
        finally:
            await second.aclose()
    finally:
        meter.mark_ended()
        await first.aclose()


@pytest.mark.parametrize("seconds", [0.0, 12.5])
def test_billable_activity_uses_final_provider_duration(tmp_path, seconds):
    store = UsageStore(str(tmp_path / "usage.db"))
    meter = BillableActivityMeter(store, "openai_live", 3.0)
    meter.mark_started()
    meter.mark_ended(seconds=seconds)
    with sqlite3.connect(str(tmp_path / "usage.db")) as conn:
        opened, closed = conn.execute("SELECT opened_at, closed_at FROM connection_intervals").fetchone()
    assert (datetime.fromisoformat(closed) - datetime.fromisoformat(opened)).total_seconds() == seconds
