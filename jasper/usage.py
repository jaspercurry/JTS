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

Time-billed providers (``grok``): Grok Voice bills a flat hourly rate,
not per-token, so its token rows price to $0. ``ConnectionUptimeMeter``
records connect/disconnect intervals into the ``connection_intervals``
table; the spend queries fold that uptime cost in at the flat rate, so
Grok's cost shows up on the dashboard and counts against the cap. See
``ConnectionUptimeMeter`` and ``UsageStore._time_billed_spend_by_provider``.

Display vs. circuit-breaker: the stored ``cost_usd`` is a best-effort
TRUE estimate (provider list rates). The spend cap stays conservative
without inflating the displayed number by applying a read-time
``safety_multiplier`` in ``SpendCap`` — so the dashboard reads honest
while the breaker keeps headroom.

Override file: rates here are built-in defaults. An optional
``/var/lib/jasper/pricing.json`` (``JASPER_PRICING_FILE``) overlays them
per provider using the ``Pricing`` field names as keys — see
``load_pricing_overrides``. Missing/malformed file falls back to these
defaults (fail-soft).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


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


# Gemini Live (gemini-3.1-flash-live-preview) — Google's published
# audio rates as of 2026-05-30 (ai.google.dev/gemini-api/docs/pricing):
# $3.00 / 1M audio in, $12.00 / 1M audio out. These are now the TRUE
# rates — the original 5 / 18 carried deliberate slack to keep the spend
# cap conservative, but that inflated the dashboard's displayed cost.
# The conservatism now lives in ``SpendCap``'s read-time safety
# multiplier instead, so the stored cost_usd is an honest estimate.
# Gemini Live's usage_metadata doesn't surface a modality split, so
# text/cached stay 0 and ``estimate_cost`` falls through to the
# all-audio path (audio dominates a voice turn, so this slightly
# over-estimates the cheaper text history rather than under-billing).
GEMINI_AUDIO_IN_USD_PER_1M = 3.0
GEMINI_AUDIO_OUT_USD_PER_1M = 12.0

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


# xAI Grok Voice Agent: flat $3.00 / hour of open connection (not
# per-token), per docs.x.ai/developers/pricing (2026-05-30). Token
# rates are zero; cost is metered from connection uptime by
# ``ConnectionUptimeMeter`` (keyed off this non-zero flat_per_hour_usd)
# rather than from the token rows.
GROK_VOICE_PRICING = Pricing(
    audio_input_per_million_usd=0.0,
    audio_output_per_million_usd=0.0,
    flat_per_hour_usd=3.0,
    label="grok-voice",
)


# ---------------------------------------------------------------------------
# Optional runtime pricing override
# ---------------------------------------------------------------------------
# Provider rates drift. Rather than require a code edit + redeploy each
# time, an operator (or, later, the /voice pricing paste-in) can drop a
# JSON file that overlays the built-in defaults. The schema is
# intentionally identical to the ``Pricing`` dataclass float fields so the
# future "have a chatbot fetch the latest rates and emit this JSON" flow
# writes straight into it with no key translation:
#
#   {
#     "gemini":      {"audio_input_per_million_usd": 3.0,
#                     "audio_output_per_million_usd": 12.0},
#     "openai":      {"audio_input_per_million_usd": 32.0, ...},
#     "openai_mini": {...},
#     "grok":        {"flat_per_hour_usd": 3.0}
#   }
DEFAULT_PRICING_FILE = "/var/lib/jasper/pricing.json"

# The float fields an override may set. ``label`` is intentionally not
# overridable (it identifies the rate card in logs).
_OVERRIDABLE_FIELDS = (
    "audio_input_per_million_usd",
    "audio_output_per_million_usd",
    "text_input_per_million_usd",
    "text_output_per_million_usd",
    "cached_input_per_million_usd",
    "flat_per_hour_usd",
)

# Override-file provider keys → the built-in default they overlay.
_OVERRIDE_KEYS = ("gemini", "openai", "openai_mini", "grok")


def load_pricing_overrides(path: str | None = None) -> dict[str, dict]:
    """Load the optional pricing override file.

    Returns ``{provider_key: {field: float}}`` for any provider keys in
    ``_OVERRIDE_KEYS`` with at least one recognised float field. A
    missing file returns ``{}`` (built-in rates apply). A malformed file
    logs a WARNING and returns ``{}`` — a bad hand-edit must never stop
    the daemon; the built-in rates remain authoritative. Unknown keys
    and non-numeric values are ignored (forward-compatible)."""
    path = path or os.environ.get("JASPER_PRICING_FILE", DEFAULT_PRICING_FILE)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON must be an object")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "pricing override %s ignored (%s: %s); using built-in rates",
            path, type(e).__name__, e,
        )
        return {}
    out: dict[str, dict] = {}
    for key in _OVERRIDE_KEYS:
        fields = raw.get(key)
        if not isinstance(fields, dict):
            continue
        clean = {
            k: float(v)
            for k, v in fields.items()
            if k in _OVERRIDABLE_FIELDS and isinstance(v, (int, float))
            and not isinstance(v, bool)
        }
        if clean:
            out[key] = clean
    if out:
        logger.info("pricing override loaded from %s: %s", path, sorted(out))
    return out


def _with_overrides(base: Pricing, overrides: dict | None, key: str) -> Pricing:
    fields = (overrides or {}).get(key)
    if not fields:
        return base
    return replace(base, **fields)


def pricing_for_provider(
    provider: str,
    *,
    model: str | None = None,
    overrides: dict | None = None,
) -> Pricing:
    """Return the pricing snapshot for a provider/model combination.

    `model` is a hint — for OpenAI we differentiate `gpt-realtime-2`
    vs `gpt-realtime-mini` based on substring match. `overrides` is the
    parsed ``load_pricing_overrides()`` mapping; when present, the
    matching provider's fields overlay the built-in defaults. Unknown
    providers fall back to Gemini pricing (the historical default), with
    a label indicating the fallback so journalctl makes it visible."""
    if provider == "gemini":
        return _with_overrides(GEMINI_PRICING, overrides, "gemini")
    if provider == "openai":
        if model and "mini" in model.lower():
            return _with_overrides(
                OPENAI_REALTIME_MINI_PRICING, overrides, "openai_mini",
            )
        return _with_overrides(OPENAI_REALTIME_PRICING, overrides, "openai")
    if provider == "grok":
        return _with_overrides(GROK_VOICE_PRICING, overrides, "grok")
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
        # Connection-uptime intervals for time-billed providers (Grok).
        # Each open connection is a row; cost = duration × rate snapshot.
        # Separate from `sessions` (which is per-turn) because the billable
        # unit for a flat-rate provider is connection time, not turns.
        # NOTE: do NOT clean up dangling intervals here — UsageStore is
        # constructed read-only by the dashboard on every poll, and
        # closing the live connection's open interval from a reader would
        # be wrong. Crash cleanup lives in ConnectionUptimeMeter (daemon
        # startup only).
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connection_intervals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                rate_per_hour_usd REAL NOT NULL DEFAULT 0
            )
            """
        )
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

    # ------------------------------------------------------------------
    # Connection-uptime intervals (time-billed providers, e.g. Grok)
    # ------------------------------------------------------------------
    def record_connection_open(
        self, provider: str, rate_per_hour_usd: float,
    ) -> None:
        """Open a connection-uptime interval — called when a time-billed
        provider's WebSocket connects. The rate is snapshotted so a later
        rate change doesn't retroactively re-price past connection time."""
        self._conn.execute(
            "INSERT INTO connection_intervals "
            "(provider, opened_at, rate_per_hour_usd) VALUES (?, ?, ?)",
            (
                provider,
                datetime.now(timezone.utc).isoformat(),
                float(rate_per_hour_usd),
            ),
        )

    def record_connection_close(self) -> None:
        """Close any open connection interval — called on teardown /
        reconnect / shutdown. Closes all open rows; there is only ever
        one live connection, so this targets exactly it."""
        self._conn.execute(
            "UPDATE connection_intervals SET closed_at = ? "
            "WHERE closed_at IS NULL",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def close_dangling_intervals(self) -> None:
        """Conservatively close intervals a crash left open (no clean
        teardown ran): set closed_at = opened_at (zero duration) so a
        stale open row can't bill phantom uptime up to 'now' on the next
        read. Run once at daemon start (ConnectionUptimeMeter), never
        from the read-only dashboard path."""
        self._conn.execute(
            "UPDATE connection_intervals SET closed_at = opened_at "
            "WHERE closed_at IS NULL"
        )

    def _time_billed_spend_by_provider(
        self, since: datetime, until: datetime,
    ) -> dict[str, float]:
        """Connection-uptime cost per provider over ``[since, until]``.
        Open intervals (closed_at IS NULL) are billed up to ``until``.
        Returns ``{}`` when no intervals overlap the window."""
        cur = self._conn.execute(
            "SELECT provider, opened_at, closed_at, rate_per_hour_usd "
            "FROM connection_intervals "
            "WHERE opened_at <= ? AND (closed_at IS NULL OR closed_at >= ?)",
            (until.isoformat(), since.isoformat()),
        )
        out: dict[str, float] = {}
        for provider, opened_at, closed_at, rate in cur.fetchall():
            if not rate:
                continue
            try:
                start = max(datetime.fromisoformat(opened_at), since)
                end = min(
                    datetime.fromisoformat(closed_at) if closed_at else until,
                    until,
                )
            except (ValueError, TypeError):
                continue
            secs = (end - start).total_seconds()
            if secs > 0:
                out[provider] = (
                    out.get(provider, 0.0) + secs / 3600.0 * float(rate)
                )
        return out

    def _time_billed_spend(self, since: datetime, until: datetime) -> float:
        return sum(self._time_billed_spend_by_provider(since, until).values())

    def spend_last_24h_usd(self) -> float:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM sessions "
            "WHERE strftime('%s', started_at) >= ?",
            (str(int(cutoff.timestamp())),),
        )
        row = cur.fetchone()
        token_cost = float(row[0] if row else 0.0)
        return token_cost + self._time_billed_spend(cutoff, now)

    def spend_month_to_date_usd(self) -> float:
        """Cumulative cost since the start of the current calendar
        month (UTC). Used by the /system dashboard's cloud-activity
        card to surface a stable monthly figure (the 24h rolling
        number bounces too much for an at-a-glance view)."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM sessions "
            "WHERE started_at >= ?",
            (month_start.isoformat(),),
        )
        row = cur.fetchone()
        token_cost = float(row[0] if row else 0.0)
        return token_cost + self._time_billed_spend(month_start, now)

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
        Pre-migration rows (NULL provider) bucket under "unknown".
        For time-billed providers (Grok) the per-turn token cost is $0;
        their connection-uptime cost is folded into ``cost_usd`` here."""
        now = datetime.now(timezone.utc)
        if since_utc is None:
            since_utc = now.replace(
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
        seen: set[str] = set()
        for row in cur.fetchall():
            out.append({
                "provider": row[0],
                "sessions": int(row[1]),
                "input_tokens": int(row[2]),
                "output_tokens": int(row[3]),
                "cost_usd": float(row[4]),
                "last_session_at": row[5],
            })
            seen.add(row[0])
        # Fold connection-uptime cost into each provider's total, and add
        # a row for any provider that has connection time but no session
        # rows in this window.
        time_billed = self._time_billed_spend_by_provider(since_utc, now)
        for r in out:
            r["cost_usd"] += time_billed.get(r["provider"], 0.0)
        for provider, cost in time_billed.items():
            if provider not in seen and cost:
                out.append({
                    "provider": provider,
                    "sessions": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": cost,
                    "last_session_at": None,
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
    """Daily spend circuit breaker.

    The stored ``cost_usd`` is a best-effort TRUE estimate. To keep the
    breaker conservative without inflating the dashboard's displayed
    cost, the rolling 24h spend is multiplied by ``safety_multiplier``
    before comparing to the ceiling — headroom for estimation error and
    provider-side surprises lives here, not in the rate card.

    Default ``1.0`` (no padding) keeps tests and incidental callers
    unsurprising; the daemon and doctor pass
    ``JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER`` (default 1.25).

    A safety multiplier never *weakens* the cap: values below 1.0 are
    floored to 1.0. Disabling the cap is solely the job of
    ``JASPER_DAILY_SPEND_CAP_USD=0`` — a multiplier of 0 must not silently
    turn the breaker off."""

    def __init__(
        self,
        store: UsageStore,
        cap_usd: float,
        safety_multiplier: float = 1.0,
    ) -> None:
        self._store = store
        self._cap_usd = cap_usd
        self._safety_multiplier = max(1.0, float(safety_multiplier))

    def _padded_spend(self) -> float:
        return self._store.spend_last_24h_usd() * self._safety_multiplier

    def allowed(self) -> bool:
        return self._padded_spend() < self._cap_usd

    def remaining_usd(self) -> float:
        return max(0.0, self._cap_usd - self._padded_spend())


class ConnectionUptimeMeter:
    """Meters connection uptime for a time-billed provider (Grok: flat
    $/hour of open connection, not per-token).

    The voice daemon wires one meter to the active connection — only when
    ``pricing.flat_per_hour_usd > 0`` — before ``start()``. The connection
    calls ``mark_connected()`` after each successful open and
    ``mark_disconnected()`` on teardown / reconnect / shutdown, so the
    recorded intervals exclude reconnect-backoff gaps. Cost is folded into
    the spend queries from those intervals.

    All methods are fail-soft: a usage-accounting write must never break
    the voice path (mirrors the rest of this module)."""

    def __init__(
        self, store: UsageStore, provider: str, rate_per_hour_usd: float,
    ) -> None:
        self._store = store
        self._provider = provider
        self._rate = float(rate_per_hour_usd)
        # A prior process that crashed without a clean teardown leaves an
        # interval open; close it conservatively now so it can't bill
        # phantom uptime against this run.
        try:
            store.close_dangling_intervals()
        except Exception as e:  # noqa: BLE001
            logger.warning("uptime meter: dangling cleanup failed: %s", e)

    def mark_connected(self) -> None:
        try:
            self._store.record_connection_open(self._provider, self._rate)
        except Exception as e:  # noqa: BLE001
            logger.warning("uptime meter: open failed: %s", e)

    def mark_disconnected(self) -> None:
        try:
            self._store.record_connection_close()
        except Exception as e:  # noqa: BLE001
            logger.warning("uptime meter: close failed: %s", e)
