from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jasper.usage import (
    ConnectionUptimeMeter,
    Pricing,
    SpendCap,
    UsageStore,
    load_default_pricing,
    load_pricing_overrides,
    pricing_for_model,
)


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
    disabled_lines = [
        r for r in caplog.records if "event=spend_cap.disabled" in r.getMessage()
    ]
    assert len(disabled_lines) == 1
    caplog.clear()
    with caplog.at_level("WARNING", logger="jasper.usage"):
        for _ in range(5):  # per-wake calls must not re-log
            assert cap.allowed() is True
    assert not [
        r for r in caplog.records if "event=spend_cap.disabled" in r.getMessage()
    ]


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
# Connection-uptime metering (time-billed providers, e.g. Grok)
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
    """A 30-minute Grok connection at $3/hour contributes $1.50 to the
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
    the live connection's ongoing cost shows immediately."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    now = datetime.now(timezone.utc)
    _insert_interval(
        db, "grok", (now - timedelta(hours=1)).isoformat(), None, 3.0,
    )
    assert abs(store.spend_last_24h_usd() - 3.0) < 0.05


def test_uptime_meter_records_open_then_close(tmp_path: Path):
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    meter = ConnectionUptimeMeter(store, "grok", 3.0)
    meter.mark_connected()
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT provider, closed_at, rate_per_hour_usd "
            "FROM connection_intervals"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "grok" and rows[0][1] is None and rows[0][2] == 3.0
    meter.mark_disconnected()
    with sqlite3.connect(str(db)) as conn:
        closed = conn.execute(
            "SELECT closed_at FROM connection_intervals"
        ).fetchone()[0]
    assert closed is not None


def test_dangling_interval_closed_at_meter_start(tmp_path: Path):
    """A crash leaves an interval open. The next meter construction
    closes it conservatively (zero duration) so a stale open row can't
    bill phantom uptime up to 'now'."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _insert_interval(db, "grok", opened, None, 3.0)
    # Before cleanup the open interval would bill ~2h = ~$6.
    assert store.spend_last_24h_usd() > 5.0
    ConnectionUptimeMeter(store, "grok", 3.0)  # runs dangling cleanup
    assert store.spend_last_24h_usd() < 1e-6


def test_aggregate_by_provider_folds_in_connection_cost(tmp_path: Path):
    """Grok's per-turn token rows cost $0; the per-provider rollup folds
    the connection-uptime cost into its cost_usd so the dashboard shows
    a real number."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db), pricing=pricing_for_model("grok-voice-think-fast-1.0"))
    sid = store.open_session(provider="grok")
    store.close_session(sid, input_tokens=1000, output_tokens=1000)  # $0
    now = datetime.now(timezone.utc)
    _insert_interval(
        db, "grok", (now - timedelta(hours=1)).isoformat(), now.isoformat(), 3.0,
    )
    rows = store.aggregate_by_provider()
    grok = next(r for r in rows if r["provider"] == "grok")
    assert grok["sessions"] == 1
    assert grok["input_tokens"] == 1000  # tokens still tracked
    assert abs(grok["cost_usd"] - 3.0) < 1e-3  # connection cost folded in


def test_token_billed_provider_has_no_intervals(tmp_path: Path):
    """Sanity: a provider with no connection intervals (OpenAI/Gemini)
    gets zero time-billed cost — the meter is never wired for them."""
    db = tmp_path / "usage.db"
    store = UsageStore(str(db))
    now = datetime.now(timezone.utc)
    assert store._time_billed_spend(now - timedelta(hours=24), now) == 0.0
