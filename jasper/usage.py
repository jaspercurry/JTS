"""Per-session usage / cost accounting for the voice loop.

The spend cap is a coarse circuit breaker, not a billing source of
truth: Google, OpenAI, and xAI each compute final invoices on their
side. We log token counts and a USD estimate so the daemon can refuse
new wakes once a daily ceiling is hit.

Pricing is per-model AND modality-aware. ``UsageStore`` is constructed
with a ``Pricing`` snapshot for whichever model is active; estimated
cost goes into the row at session-close time, so the 24-hour spend sum
naturally aggregates across models/providers if the user switches
mid-window. Default rates ship dated in
``jasper/data/model_pricing.json`` (see ``load_default_pricing`` /
``pricing_for_model``); there is no provider-level price.

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
Grok's cost shows up in spend-cap status and counts against the cap. See
``ConnectionUptimeMeter`` and ``UsageStore._time_billed_spend_by_provider``.

Display vs. circuit-breaker: the stored ``cost_usd`` is a best-effort
TRUE estimate (provider list rates). The spend cap stays conservative
without inflating the displayed number by applying a read-time
``safety_multiplier`` in ``SpendCap`` — so the status card reads honest
while the breaker keeps headroom.

Override file: the bundled rates are defaults. An optional
``/var/lib/jasper/pricing.json`` (``JASPER_PRICING_FILE``) overlays them
per MODEL ID using the ``Pricing`` field names as keys — see
``load_pricing_overrides``. Missing/malformed file falls back to the
bundled defaults (fail-soft).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_DAILY_SPEND_CAP_USD = 1.0
DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER = 1.25
DEFAULT_USAGE_DB = "/var/lib/jasper/usage.db"


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


# ---------------------------------------------------------------------------
# Bundled default pricing — dated, model-ID-keyed, shipped in the repo
# ---------------------------------------------------------------------------
# Default rates live in version-controlled data, not Python constants, so
# they carry an ``as_of`` date and can be refreshed by editing JSON (or via
# the /voice pricing prompt flow) rather than a code change. Keyed by exact
# model ID — there is no provider-level price (a single rate for a whole
# provider isn't a real thing). User overrides in /var/lib/jasper/pricing.json
# overlay these per model. See docs/HANDOFF-pricing-editor.md.
BUNDLED_PRICING_FILE = str(
    Path(__file__).resolve().parent / "data" / "model_pricing.json"
)

# The float fields a pricing entry (bundled or override) may set. ``label``
# is not data — it identifies the rate card in logs.
_OVERRIDABLE_FIELDS = (
    "audio_input_per_million_usd",
    "audio_output_per_million_usd",
    "text_input_per_million_usd",
    "text_output_per_million_usd",
    "cached_input_per_million_usd",
    "flat_per_hour_usd",
)


def _clean_pricing_fields(fields: object) -> dict[str, float]:
    """Keep only recognised, numeric (non-bool) rate fields from a raw JSON
    object. Forward-compatible: unknown keys / bad values are dropped."""
    if not isinstance(fields, dict):
        return {}
    return {
        k: float(v)
        for k, v in fields.items()
        if k in _OVERRIDABLE_FIELDS
        and isinstance(v, (int, float))
        and not isinstance(v, bool)
    }


def _pricing_from_fields(label: str, fields: dict[str, float]) -> Pricing:
    return Pricing(
        audio_input_per_million_usd=fields.get("audio_input_per_million_usd", 0.0),
        audio_output_per_million_usd=fields.get("audio_output_per_million_usd", 0.0),
        text_input_per_million_usd=fields.get("text_input_per_million_usd", 0.0),
        text_output_per_million_usd=fields.get("text_output_per_million_usd", 0.0),
        cached_input_per_million_usd=fields.get("cached_input_per_million_usd", 0.0),
        flat_per_hour_usd=fields.get("flat_per_hour_usd", 0.0),
        label=label,
    )


def load_default_pricing(
    path: str | None = None,
) -> tuple[dict[str, Pricing], str]:
    """Load the bundled, dated default rates → ``({model_id: Pricing}, as_of)``.

    The file is package data (``jasper/data/model_pricing.json``); it's
    missing only on a packaging bug. Treat unreadable/corrupt as a logged
    ERROR + an empty map (every model then resolves "unpriced" and is
    surfaced) rather than crashing the daemon."""
    path = path or BUNDLED_PRICING_FILE
    try:
        with open(path) as f:
            raw = json.load(f)
        models = raw["models"]
        if not isinstance(models, dict):
            raise ValueError("'models' must be an object")
        as_of = str(raw.get("as_of", ""))
    except Exception as e:  # noqa: BLE001
        logger.error(
            "default pricing %s unreadable (%s: %s); all models will be "
            "unpriced until a rate is provided",
            path, type(e).__name__, e,
        )
        return {}, ""
    out = {
        str(mid): _pricing_from_fields(str(mid), _clean_pricing_fields(fields))
        for mid, fields in models.items()
    }
    return out, as_of


# Loaded once at import. Refreshing the file needs a process restart (the
# daemon restarts on every /voice save, so that path is automatic).
_DEFAULT_MODEL_PRICING, _DEFAULT_PRICING_AS_OF = load_default_pricing()


def default_pricing_as_of() -> str:
    """The ``as_of`` date of the bundled default rates (read once at
    import). Used by the /voice editor to show how fresh defaults are
    without re-reading the file on every page load."""
    return _DEFAULT_PRICING_AS_OF


# Fallback model for a UsageStore built without explicit pricing — the
# dashboard read path (never computes cost) and tests. Production always
# passes the active model's pricing.
_DEFAULT_DISPLAY_MODEL = "gemini-3.1-flash-live-preview"


# ---------------------------------------------------------------------------
# Optional user override — /var/lib/jasper/pricing.json
# ---------------------------------------------------------------------------
# Same shape as the bundled file (a ``models`` map keyed by model ID), but
# sparse: only the rates the user changed. Overlays the bundled defaults per
# model. Written by the /voice pricing editor; refreshable via the prompt
# flow. Example:
#
#   {"as_of": "2026-08-01",
#    "models": {"gpt-realtime-2": {"text_output_per_million_usd": 28.0}}}
DEFAULT_PRICING_FILE = "/var/lib/jasper/pricing.json"


def sanitize_pricing_models(raw_models: object) -> dict[str, dict]:
    """Validate a raw ``{model_id: {field: value}}`` map → a clean
    ``{model_id: {field: float}}`` keeping only recognised numeric rate
    fields and dropping models with none left. Shared by the override-file
    loader and the ``/voice`` paste-import path so both apply identical
    rules to operator- and chatbot-supplied JSON."""
    if not isinstance(raw_models, dict):
        return {}
    out: dict[str, dict] = {}
    for model_id, fields in raw_models.items():
        clean = _clean_pricing_fields(fields)
        if clean:
            out[str(model_id)] = clean
    return out


def load_pricing_overrides(path: str | None = None) -> dict[str, dict]:
    """Load the optional override file → ``{model_id: {field: float}}``.

    Expects ``{"models": {"<model_id>": {field: value}}}`` (plus optional
    ``as_of`` / ``source``). A missing file → ``{}`` (bundled rates apply).
    A malformed file logs a WARNING and returns ``{}`` — a bad hand-edit
    must never stop the daemon. Non-numeric / unknown fields are dropped. A
    stale provider-keyed file (no ``models`` map) harmlessly returns ``{}``,
    so the old format degrades to bundled defaults with no migration code."""
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
            "pricing override %s ignored (%s: %s); using bundled rates",
            path, type(e).__name__, e,
        )
        return {}
    out = sanitize_pricing_models(raw.get("models"))
    if out:
        logger.info(
            "pricing override loaded from %s: %d model(s)", path, len(out),
        )
    return out


def pricing_for_model(
    model_id: str,
    *,
    overrides: dict | None = None,
    defaults: dict[str, Pricing] | None = None,
) -> Pricing:
    """Resolve the rate card for an exact model ID.

    The bundled default for the model, with any ``overrides[model_id]``
    fields overlaid. An unknown model (in neither the bundled file nor the
    override) has genuinely no price → an all-zero ``Pricing`` labelled
    ``unpriced:<id>``; callers should surface that loudly rather than treat
    $0 as "free". There is deliberately no provider-level fallback — we
    never invent a rate.

    ``defaults`` overrides the bundled table (tests); otherwise the table
    loaded at import is used."""
    table = _DEFAULT_MODEL_PRICING if defaults is None else defaults
    base = table.get(model_id)
    if base is None:
        base = Pricing(
            audio_input_per_million_usd=0.0,
            audio_output_per_million_usd=0.0,
            label=f"unpriced:{model_id}",
        )
    fields = (overrides or {}).get(model_id)
    if fields:
        base = replace(base, **fields)
    return base


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
        # Callers that don't pass `pricing=` (the dashboard read path, which
        # never computes cost, and tests) fall back to the cheapest current
        # model's rates. Production always passes the active model's pricing.
        self._pricing: Pricing = pricing or pricing_for_model(
            _DEFAULT_DISPLAY_MODEL
        )

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
        month (UTC). Used by the /voice spend-cap status card to surface
        a stable monthly figure (the 24h rolling number bounces too much
        for an at-a-glance view)."""
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
        """Per-provider session/token/cost rollup. Useful for diagnostics
        and future spend details. Default window is the current calendar
        month.

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
        if no session has ever closed. Status surfaces can render this
        as relative time when they need recent-provider-call context."""
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
    turn the breaker off.

    ``cap_usd <= 0`` means **disabled**: ``allowed()`` is always True.
    This matches the documented contract everywhere the knob is
    described (``Config.from_env``'s validation message, ``.env.example``,
    the /voice wizard). Before 2026-06 a cap of 0 inverted the contract —
    ``padded < 0.0`` is False from the first wake, so the documented
    "disable" value blocked every session instead."""

    def __init__(
        self,
        store: UsageStore,
        cap_usd: float,
        safety_multiplier: float = 1.0,
    ) -> None:
        self._store = store
        self._cap_usd = cap_usd
        self._safety_multiplier = max(1.0, float(safety_multiplier))
        if self.disabled:
            # Once per construction (the daemon builds exactly one at
            # startup) — never per-wake. An unbounded-spend posture is
            # deliberate but worth one loud line in the journal.
            log_event(
                logger,
                "spend_cap.disabled",
                cap_usd=self._cap_usd,
                note=(
                    "daily spend cap is OFF "
                    "(JASPER_DAILY_SPEND_CAP_USD<=0); sessions are "
                    "not spend-limited"
                ),
                level=logging.WARNING,
            )

    @property
    def disabled(self) -> bool:
        """True when no ceiling is configured (cap <= 0). Display
        surfaces should branch on this before rendering
        ``remaining_usd()`` — "remaining" is meaningless without a cap."""
        return self._cap_usd <= 0

    def _padded_spend(self) -> float:
        return self._store.spend_last_24h_usd() * self._safety_multiplier

    def allowed(self) -> bool:
        if self.disabled:
            return True
        return self._padded_spend() < self._cap_usd

    def remaining_usd(self) -> float:
        """Headroom left under the cap. Only meaningful when the cap is
        enabled; returns 0.0 when ``disabled`` (check that first — the
        /voice wizard renders "disabled" instead of a dollar figure)."""
        if self.disabled:
            return 0.0
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
