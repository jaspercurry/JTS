"""Per-session usage / cost accounting for the voice loop.

The spend cap is a coarse circuit breaker, not a billing source of
truth: Google, OpenAI, and xAI each compute final invoices on their
side. We log token counts and a USD estimate so the daemon can refuse
new wakes once a daily ceiling is hit.

Pricing is provider-aware. ``UsageStore`` is constructed with a
``Pricing`` snapshot of whichever voice provider is active; estimated
cost goes into the row at session-close time, so the 24-hour spend
sum naturally aggregates across providers if the user switches mid-
window.

Caveat: ``grok`` bills a flat hourly rate, not per-token. The Grok
``Pricing`` row has zero token rates and a non-zero ``flat_per_hour_usd``
field — but ``close_session`` does not currently track session
duration, so Grok-mode spend will under-count. Either (a) override
``JASPER_DAILY_SPEND_CAP_USD`` low and treat the cap as a liveness
nudge, or (b) trust xAI's own billing dashboard. A time-based row
would be a worthwhile follow-up but is out of scope for the v1
provider-abstraction landing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Pricing:
    """USD-per-1M-tokens snapshot for a single voice provider.

    Numbers come from each provider's public pricing page at the time
    of writing (early May 2026). They drift; treat as advisory and
    re-check before relying on the spend cap for anything serious."""
    input_audio_per_million_usd: float
    output_audio_per_million_usd: float
    cached_input_per_million_usd: float = 0.0
    # Optional: flat-rate billing (Grok). Informational only — token
    # cost still uses the audio-in/out fields. See module docstring.
    flat_per_hour_usd: float = 0.0
    label: str = ""

    def estimate_token_cost(
        self, input_tokens: int, output_tokens: int,
    ) -> float:
        return (
            input_tokens * self.input_audio_per_million_usd / 1_000_000
            + output_tokens * self.output_audio_per_million_usd / 1_000_000
        )


# Gemini Live (gemini-2.5-flash-native-audio / 3.1-flash-live-preview)
# — Google's published audio rates. These were the values pinned in
# the original UsageStore (5 / 18); they intentionally include some
# slack on top of Google's headline 3 / 12 to keep the cap conservative
# in the face of transient billing-side surprises.
GEMINI_AUDIO_IN_USD_PER_1M = 5.0
GEMINI_AUDIO_OUT_USD_PER_1M = 18.0

GEMINI_PRICING = Pricing(
    input_audio_per_million_usd=GEMINI_AUDIO_IN_USD_PER_1M,
    output_audio_per_million_usd=GEMINI_AUDIO_OUT_USD_PER_1M,
    label="gemini-live",
)


# OpenAI Realtime (gpt-realtime-2 GA, 2026-05-07).
# Audio: $32 / $64 / $0.40 per 1M tokens (input / output / cached).
# Tokens roughly: 1 token / 100 ms user audio, 1 token / 50 ms
# assistant audio — meaning a one-minute conversation with 50/50
# talk-time costs about $0.06 in + $0.24 out, or ~$0.30 / minute.
# That's ~5x Gemini per minute; users switching providers should
# review JASPER_DAILY_SPEND_CAP_USD accordingly.
OPENAI_REALTIME_PRICING = Pricing(
    input_audio_per_million_usd=32.0,
    output_audio_per_million_usd=64.0,
    cached_input_per_million_usd=0.40,
    label="openai-realtime-2",
)

# OpenAI Realtime mini (gpt-realtime-mini): $10 / $20 / $0.30 per 1M.
OPENAI_REALTIME_MINI_PRICING = Pricing(
    input_audio_per_million_usd=10.0,
    output_audio_per_million_usd=20.0,
    cached_input_per_million_usd=0.30,
    label="openai-realtime-mini",
)


# xAI Grok Voice Agent: flat $3.00 / hour. Token rates are zero, so
# spend tracking under-counts (see module docstring). Stored here so
# downstream code can `pricing.flat_per_hour_usd` if it ever grows
# duration tracking.
GROK_VOICE_PRICING = Pricing(
    input_audio_per_million_usd=0.0,
    output_audio_per_million_usd=0.0,
    flat_per_hour_usd=3.0,
    label="grok-voice",
)


def pricing_for_provider(
    provider: str, *, model: str | None = None,
) -> Pricing:
    """Return the pricing snapshot for a provider/model combination.

    `model` is a hint — for OpenAI we differentiate `gpt-realtime-2`
    vs `gpt-realtime-mini` based on substring match. Unknown providers
    fall back to Gemini pricing (the historical default), with a label
    indicating the fallback so journalctl makes it visible."""
    if provider == "gemini":
        return GEMINI_PRICING
    if provider == "openai":
        if model and "mini" in model.lower():
            return OPENAI_REALTIME_MINI_PRICING
        return OPENAI_REALTIME_PRICING
    if provider == "grok":
        return GROK_VOICE_PRICING
    return Pricing(
        input_audio_per_million_usd=GEMINI_AUDIO_IN_USD_PER_1M,
        output_audio_per_million_usd=GEMINI_AUDIO_OUT_USD_PER_1M,
        label=f"unknown-provider:{provider}",
    )


class UsageStore:
    def __init__(
        self, db_path: str, pricing: Pricing | None = None,
    ) -> None:
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
        # Default to Gemini pricing so existing callers (and tests)
        # that don't pass `pricing=` keep working with their historical
        # cost estimates.
        self._pricing: Pricing = pricing or GEMINI_PRICING

    def open_session(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO sessions (started_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        return int(cur.lastrowid)

    def close_session(
        self, session_id: int, input_tokens: int, output_tokens: int,
    ) -> float:
        cost = self._pricing.estimate_token_cost(input_tokens, output_tokens)
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


class SpendCap:
    def __init__(self, store: UsageStore, cap_usd: float) -> None:
        self._store = store
        self._cap_usd = cap_usd

    def allowed(self) -> bool:
        return self._store.spend_last_24h_usd() < self._cap_usd

    def remaining_usd(self) -> float:
        return max(0.0, self._cap_usd - self._store.spend_last_24h_usd())
