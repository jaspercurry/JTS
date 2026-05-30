"""Per-session usage / cost accounting for the voice loop.

The spend cap is a coarse circuit breaker, not a billing source of
truth: Google, OpenAI, and xAI each compute final invoices on their
side. We log token counts and a USD estimate so the daemon can refuse
new wakes once a daily ceiling is hit.

Pricing is provider-aware AND modality-aware. ``UsageStore`` is
constructed with a ``Pricing`` snapshot of whichever voice provider
is active; estimated cost goes into the row at session-close time, so
the 24-hour spend sum naturally aggregates across providers if the
user switches mid-window.

Modality split (OpenAI Realtime 2 specifically): the ``response.usage``
object on each ``response.done`` carries an ``input_token_details``
sub-object with ``audio_tokens``, ``text_tokens``, and ``cached_tokens``
counts (and the same for output, minus cached). Those map to four
distinct USD-per-1M-tokens rates on OpenAI's pricing page:
$32 audio in, $4 text in, $0.40 cached input, $64 audio out, $24
text out. **Pricing all input as audio** — which is what we did
before — overstated cost by ~50× because the bulk of input on a
tool-using turn is the cached system prompt + tool defs (text), not
the user's audio. The ``Pricing.estimate_cost`` method below splits
correctly when the breakdown is present and falls back to flat audio
rates when it isn't (Gemini, which doesn't surface a breakdown).

Caveat: ``grok`` bills a flat hourly rate, not per-token. The Grok
``Pricing`` row has zero token rates and a non-zero ``flat_per_hour_usd``
field — but ``close_session`` does not currently track session
duration, so Grok-mode spend will under-count. Either (a) override
``JASPER_DAILY_SPEND_CAP_USD`` low and treat the cap as a liveness
nudge, or (b) trust xAI's own billing dashboard. A time-based row
would be a worthwhile follow-up but is out of scope here.
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
    re-check before relying on the spend cap for anything serious.

    The four input rates capture the typical Realtime price card:
      audio_input_per_million_usd:   user-microphone audio (typically the
                                     most expensive bucket per token)
      text_input_per_million_usd:    text history, system instructions,
                                     tool definitions (cheaper)
      cached_input_per_million_usd:  prompt-caching hits — usually the
                                     stable prefix (system prompt +
                                     tool defs), 80× cheaper than fresh
      audio_output_per_million_usd:  TTS output
      text_output_per_million_usd:   transcript output that accompanies
                                     audio under output_modalities=["audio"]

    Providers that don't surface a breakdown (Gemini Live as of May
    2026) leave text/cached at 0 and rely on the audio_input rate as
    a conservative all-in estimate via ``estimate_token_cost``.
    """
    audio_input_per_million_usd: float
    audio_output_per_million_usd: float
    text_input_per_million_usd: float = 0.0
    text_output_per_million_usd: float = 0.0
    cached_input_per_million_usd: float = 0.0
    # Optional: flat-rate billing (Grok). Informational only — token
    # cost still uses the audio-in/out fields. See module docstring.
    flat_per_hour_usd: float = 0.0
    label: str = ""

    def estimate_cost(self, usage: dict | None) -> float:
        """Estimate USD cost from a usage dict.

        When the dict carries ``input_token_details`` and/or
        ``output_token_details`` (OpenAI Realtime), split tokens by
        modality and apply the four-bucket rate card. When it doesn't
        (Gemini Live, legacy fallbacks), treat all tokens as audio at
        the audio rate — that's what the original implementation did
        and it remains a sensible conservative estimate for providers
        that only emit aggregates.
        """
        if not usage:
            return 0.0
        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        if input_details or output_details:
            return self._cost_with_breakdown(usage)
        return self.estimate_token_cost(
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        )

    def estimate_token_cost(
        self, input_tokens: int, output_tokens: int,
    ) -> float:
        """Flat all-audio cost — kept as a back-compat path for callers
        that only have aggregate token counts (Gemini, legacy tests).
        New code prefers ``estimate_cost`` with a breakdown."""
        return (
            input_tokens * self.audio_input_per_million_usd / 1_000_000
            + output_tokens * self.audio_output_per_million_usd / 1_000_000
        )

    def _cost_with_breakdown(self, usage: dict) -> float:
        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}

        audio_in = int(input_details.get("audio_tokens") or 0)
        text_in = int(input_details.get("text_tokens") or 0)
        cached = int(input_details.get("cached_tokens") or 0)

        # Cached tokens are a SUBSET of input_tokens — see the OpenAI
        # SDK doc on RealtimeResponseUsageInputTokenDetails:
        # "Cached tokens here are counted as a subset of input tokens,
        # meaning input tokens will include cached and uncached tokens."
        # We need to subtract them from the right bucket so we don't
        # double-bill: charge cached tokens at the cached rate AND
        # the uncached remainder at audio_input/text_input rates.
        #
        # If the SDK provides ``cached_tokens_details`` with per-modality
        # split, use it. Otherwise assume cached tokens come from text
        # first (the typical case for system-prompt caching) and only
        # spill into audio if cached > text_in.
        cached_details = input_details.get("cached_tokens_details") or {}
        cached_audio = int(cached_details.get("audio_tokens") or 0)
        cached_text = int(cached_details.get("text_tokens") or 0)
        if cached_audio == 0 and cached_text == 0 and cached > 0:
            cached_text = min(cached, text_in)
            cached_audio = max(0, cached - cached_text)

        uncached_audio_in = max(0, audio_in - cached_audio)
        uncached_text_in = max(0, text_in - cached_text)

        audio_out = int(output_details.get("audio_tokens") or 0)
        text_out = int(output_details.get("text_tokens") or 0)

        return (
            uncached_audio_in * self.audio_input_per_million_usd
            + uncached_text_in * self.text_input_per_million_usd
            + cached * self.cached_input_per_million_usd
            + audio_out * self.audio_output_per_million_usd
            + text_out * self.text_output_per_million_usd
        ) / 1_000_000


# Gemini Live (gemini-2.5-flash-native-audio / 3.1-flash-live-preview)
# — Google's published audio rates. These were the values pinned in
# the original UsageStore (5 / 18); they intentionally include some
# slack on top of Google's headline 3 / 12 to keep the cap conservative
# in the face of transient billing-side surprises. Gemini Live's
# usage_metadata doesn't surface a modality split, so text/cached
# stay 0 and ``estimate_cost`` falls through to the all-audio path.
GEMINI_AUDIO_IN_USD_PER_1M = 5.0
GEMINI_AUDIO_OUT_USD_PER_1M = 18.0

GEMINI_PRICING = Pricing(
    audio_input_per_million_usd=GEMINI_AUDIO_IN_USD_PER_1M,
    audio_output_per_million_usd=GEMINI_AUDIO_OUT_USD_PER_1M,
    label="gemini-live",
)


# OpenAI Realtime (gpt-realtime-2 GA, 2026-05-07).
# Per the pricing page (developers.openai.com/api/docs/pricing):
#   audio in:    $32.00 / 1M
#   audio out:   $64.00 / 1M
#   text in:     $4.00  / 1M    (system instructions, tool defs, history)
#   text out:    $24.00 / 1M    (transcripts produced alongside audio)
#   cached in:   $0.40  / 1M    (80× cheaper — applies to stable prefix)
# A typical tool-using turn after the prompt cache warms up has most of
# its input come from `cached` (system prompt + tool defs) at $0.40,
# with only ~50–100 tokens of fresh user audio at $32. Output is mostly
# audio with a small transcript companion. Per-turn cost lands in the
# ~$0.005–$0.02 range, NOT $0.40 — pricing every token at the audio
# rate (which is what the previous implementation did) overstated by
# 50–100×.
OPENAI_REALTIME_PRICING = Pricing(
    audio_input_per_million_usd=32.0,
    audio_output_per_million_usd=64.0,
    text_input_per_million_usd=4.0,
    text_output_per_million_usd=24.0,
    cached_input_per_million_usd=0.40,
    label="openai-realtime-2",
)

# OpenAI Realtime mini (gpt-realtime-mini):
#   audio in $10, audio out $20, text in $0.60, text out $2.40, cached $0.30.
OPENAI_REALTIME_MINI_PRICING = Pricing(
    audio_input_per_million_usd=10.0,
    audio_output_per_million_usd=20.0,
    text_input_per_million_usd=0.60,
    text_output_per_million_usd=2.40,
    cached_input_per_million_usd=0.30,
    label="openai-realtime-mini",
)


# xAI Grok Voice Agent: flat $3.00 / hour. Token rates are zero, so
# spend tracking under-counts (see module docstring). Stored here so
# downstream code can `pricing.flat_per_hour_usd` if it ever grows
# duration tracking.
GROK_VOICE_PRICING = Pricing(
    audio_input_per_million_usd=0.0,
    audio_output_per_million_usd=0.0,
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
        audio_input_per_million_usd=GEMINI_AUDIO_IN_USD_PER_1M,
        audio_output_per_million_usd=GEMINI_AUDIO_OUT_USD_PER_1M,
        label=f"unknown-provider:{provider}",
    )


_SESSIONS_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        provider TEXT
    )
"""


class UsageStore:
    def __init__(
        self, db_path: str, pricing: Pricing | None = None,
    ) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute(_SESSIONS_TABLE_DDL)
        # `provider` is recorded per session at open_session() time. We
        # deliberately do NOT backfill historic NULL rows: the active
        # provider's source of truth is /var/lib/jasper/voice_provider.env,
        # not this process's frozen env, and guessing a provider for rows
        # that predate the column is exactly the kind of legacy-accounting
        # code this project doesn't carry. Pre-existing rows aggregate as
        # "unknown".
        #
        # Schema self-heal (a wipe, NOT a value-preserving migration): a
        # usage DB that predates the `provider` column (pre-PR-#85) would
        # fail open_session()'s INSERT with "no such column: provider" on
        # every turn. Rather than carry migration code, drop & recreate —
        # this is disposable cost telemetry, so the household loses nothing
        # that matters, and the voice loop self-recovers instead of wedging.
        # One-time: a no-op once the schema has the column (every current
        # and fresh DB).
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "provider" not in cols:
            self._conn.execute("DROP TABLE sessions")
            self._conn.execute(_SESSIONS_TABLE_DDL)
        # Default to Gemini pricing so existing callers (and tests)
        # that don't pass `pricing=` keep working with their historical
        # cost estimates.
        self._pricing: Pricing = pricing or GEMINI_PRICING

    def open_session(self, provider: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO sessions (started_at, provider) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), provider),
        )
        return int(cur.lastrowid)

    def close_session(
        self,
        session_id: int,
        input_tokens: int,
        output_tokens: int,
        usage: dict | None = None,
    ) -> float:
        """Record the end of a session and return the estimated cost.

        ``usage`` is the rich form: a dict that may carry
        ``input_token_details`` / ``output_token_details`` for
        modality-aware billing (OpenAI Realtime). When None, falls back
        to the scalar all-audio estimate via the ``input_tokens`` /
        ``output_tokens`` arguments — this is the path Gemini Live and
        the legacy unit tests take."""
        if usage is None:
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        cost = self._pricing.estimate_cost(usage)
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

    def spend_month_to_date_usd(self) -> float:
        """Cumulative cost since the start of the current calendar
        month (UTC). Used by the /system dashboard's cloud-activity
        card to surface a stable monthly figure (the 24h rolling
        number bounces too much for an at-a-glance view)."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM sessions "
            "WHERE started_at >= ?",
            (month_start,),
        )
        row = cur.fetchone()
        return float(row[0] if row else 0.0)

    def aggregate_by_provider(
        self, since_utc: datetime | None = None,
    ) -> list[dict]:
        """Per-provider session/token/cost rollup. Used by the
        dashboard's "Cloud activity" card. Default window is the
        current calendar month.

        Returns rows like::
          {"provider": "gemini", "sessions": 12, "input_tokens": 1234,
           "output_tokens": 567, "cost_usd": 0.42,
           "last_session_at": "2026-05-11T..."}
        Pre-migration rows (NULL provider) bucket under "unknown"."""
        if since_utc is None:
            since_utc = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0,
            )
        cur = self._conn.execute(
            """
            SELECT
              COALESCE(provider, 'unknown') AS p,
              COUNT(*) AS sessions,
              COALESCE(SUM(input_tokens), 0) AS input_tokens,
              COALESCE(SUM(output_tokens), 0) AS output_tokens,
              COALESCE(SUM(cost_usd), 0) AS cost_usd,
              MAX(COALESCE(ended_at, started_at)) AS last_session_at
            FROM sessions
            WHERE started_at >= ?
            GROUP BY p
            ORDER BY sessions DESC
            """,
            (since_utc.isoformat(),),
        )
        out: list[dict] = []
        for row in cur.fetchall():
            out.append({
                "provider": row[0],
                "sessions": int(row[1]),
                "input_tokens": int(row[2]),
                "output_tokens": int(row[3]),
                "cost_usd": float(row[4]),
                "last_session_at": row[5],
            })
        return out

    def session_count_today_utc(self) -> int:
        """Sessions since UTC midnight. Cheap counter for the
        dashboard's 'turns today' tile."""
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at >= ?",
            (today,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def last_successful_turn_at(self) -> str | None:
        """ISO timestamp of the most recently-ended session, or None
        if no session has ever closed. The dashboard renders this as
        '8 min ago' for the cloud-activity card."""
        cur = self._conn.execute(
            "SELECT ended_at FROM sessions "
            "WHERE ended_at IS NOT NULL "
            "ORDER BY ended_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        return row[0] if row else None


class SpendCap:
    def __init__(self, store: UsageStore, cap_usd: float) -> None:
        self._store = store
        self._cap_usd = cap_usd

    def allowed(self) -> bool:
        return self._store.spend_last_24h_usd() < self._cap_usd

    def remaining_usd(self) -> float:
        return max(0.0, self._cap_usd - self._store.spend_last_24h_usd())
