# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

Time-billed providers (``grok``): Grok Voice publishes a flat realtime
hourly rate, not per-token, so its token rows price to $0. JTS records
billable realtime-activity intervals into the legacy-named
``connection_intervals`` table when a voice turn is active; warm idle
WebSocket uptime is not counted because the xAI dashboard does not bill
that like active conversation time. A schema discriminator tags old
connection-uptime rows as legacy so they no longer false-trip the cap after
upgrade. The spend queries fold active intervals in at the flat rate, so
Grok's cost shows up in spend-cap status and counts against the cap. See
``BillableActivityMeter`` and ``UsageStore._time_billed_spend_by_provider``.

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

Per-surface ledger files, one writer per file: spend is recorded into a
separate SQLite DB per writing surface — the voice daemon owns
``usage.db``; root ``jasper-correction-web`` owns the sibling
``usage-tuning.db`` (it must never touch ``usage.db``, whose owner is
jasper-voice — a root-created file or ``-journal`` sidecar would wedge the
voice ledger, the 2026-06-19 "readonly database" class). ``household_usage_reader``
is the single definition of "household spend": it sums every member file at
read time, so the cap and every display surface see one total while each file
keeps exactly one writer.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_DAILY_SPEND_CAP_USD = 1.0
DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER = 1.25
DEFAULT_USAGE_DB = "/var/lib/jasper/usage.db"


def tuning_usage_db_path(usage_db_path: str) -> str:
    """The tuning-surface ledger file: a sibling of the voice usage DB in
    the same directory, named ``usage-tuning.db``.

    Derived (not a separate env var) so the two ledgers always live side by
    side and a future consolidation is a one-function edit. ``jasper-correction-web``
    is its SOLE writer; every cap/display reader sums it with ``usage_db_path``
    through ``household_usage_reader``. It is a SEPARATE file on purpose: the
    root correction-web daemon must never open the jasper-voice-owned
    ``usage.db`` read-write, since a root-created file or ``-journal`` sidecar
    wedges the voice daemon's own writes (the 2026-06-19 outage class)."""
    parent = Path(usage_db_path).parent
    return str(parent / "usage-tuning.db")


# The tuning ledger for the default install layout. Derived so a rename of
# DEFAULT_USAGE_DB moves both.
DEFAULT_TUNING_USAGE_DB = tuning_usage_db_path(DEFAULT_USAGE_DB)

# Sentinel session id returned by ``UsageStore.open_session`` when the
# accounting INSERT fails (e.g. usage.db ends up owned by the wrong user
# and writes raise "attempt to write a readonly database"). Negative so it
# can never collide with a real AUTOINCREMENT rowid (those start at 1).
# ``close_session`` treats it as a no-op. See ``open_session``.
_UNRECORDED_SESSION = -1


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

_BILLABLE_ACTIVITY_KIND = "billable_activity"
_LEGACY_CONNECTION_UPTIME_KIND = "legacy_connection_uptime"

_CONNECTION_INTERVALS_TABLE_DDL = f"""
    CREATE TABLE IF NOT EXISTS connection_intervals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        rate_per_hour_usd REAL NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT '{_BILLABLE_ACTIVITY_KIND}'
    )
"""


@dataclass
class WriteHealth:
    """Write-failure health for a single ``UsageStore`` writer instance.

    Only the voice daemon's writable store accumulates failures; the read-only
    status surfaces (the /voice spend card, jasper-doctor) never write, so they
    keep the default (not degraded). Surfaced as
    /state.voice.usage_tracking_degraded — see ``UsageStore.write_degraded``."""
    consecutive_failures: int = 0
    last_error: str | None = None
    last_failure_at: str | None = None


class UsageStore:
    def __init__(
        self, db_path: str, pricing: Pricing | None = None,
        *, read_only: bool = False, pricing_overrides: dict | None = None,
    ) -> None:
        # Status surfaces (the /voice spend-cap card, jasper-doctor) read
        # this DB but run as root, NOT as jasper-voice. They MUST pass
        # read_only=True: a read-WRITE open auto-creates the file and runs
        # the CREATE TABLE / self-heal DDL below — which can leave usage.db
        # owned by the wrong user (root/jasper-mux, mode 644). Once that
        # happens jasper-voice can no longer write its own DB, open_session()
        # raises "attempt to write a readonly database" on EVERY wake, and
        # the daemon plays the cant_connect cue instead of answering (the
        # 2026-06-16 outage). mode=ro never creates the file and never
        # writes, so a reader cannot corrupt ownership. Callers gate on
        # os.path.exists() and fail soft, so an absent DB is handled
        # upstream rather than silently created here.
        if read_only:
            self._conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, isolation_level=None,
            )
        else:
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
            # Billable realtime-activity intervals for time-billed providers
            # (Grok). The table name is historical; each active turn is a row,
            # and cost = duration × rate snapshot. Separate from `sessions`
            # because sessions close with token usage while these rows cover
            # active realtime duration for flat-rate providers.
            # NOTE: do NOT clean up dangling intervals here — UsageStore is
            # also constructed read-only by status surfaces, and closing the
            # live connection's open interval from a reader would be wrong.
            # Crash cleanup lives in BillableActivityMeter (daemon startup
            # only).
            self._conn.execute(_CONNECTION_INTERVALS_TABLE_DDL)
            self._ensure_connection_interval_kind_column()
        self._connection_intervals_have_kind = (
            self._connection_interval_kind_column_exists()
        )
        # Callers that don't pass `pricing=` (the dashboard read path, which
        # never computes cost, and tests) fall back to the cheapest current
        # model's rates. Production always passes the active model's pricing.
        self._pricing: Pricing = pricing or pricing_for_model(
            _DEFAULT_DISPLAY_MODEL
        )
        self._pricing_overrides = pricing_overrides or {}
        # Write-failure health — only meaningful on the writable voice store
        # (read-only surfaces never write). Drives the
        # /state.voice.usage_tracking_degraded signal via ``write_degraded``.
        self._write_health = WriteHealth()

    def open_session(self, provider: str | None = None) -> int:
        """Insert a new session row and return its id.

        Fail-soft: a usage-accounting write must NEVER break the voice
        turn (this mirrors ``BillableActivityMeter`` and the module
        contract). The voice loop calls this on the turn-open hot path,
        before the connection is even acquired. If the INSERT raises —
        chiefly ``sqlite3.OperationalError: attempt to write a readonly
        database`` when usage.db is owned by the wrong user — we log and
        return ``_UNRECORDED_SESSION`` instead of propagating. The caller
        stores that id and serves the turn anyway; ``close_session``
        no-ops on it. This turn's cost goes unrecorded, which is fine for
        disposable spend telemetry — far better than aborting the turn
        and playing a failure cue (the 2026-06-19 outage, where this
        raising on every wake made the daemon say "I can't connect"
        instead of answering)."""
        try:
            cur = self._conn.execute(
                "INSERT INTO sessions (started_at, provider) VALUES (?, ?)",
                (datetime.now(timezone.utc).isoformat(), provider),
            )
        except sqlite3.Error as e:
            logger.warning(
                "usage: open_session write failed (%s: %s); serving turn "
                "unrecorded", type(e).__name__, e,
            )
            self._note_write_failed(e)
            return _UNRECORDED_SESSION
        self._note_write_ok()
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
        the legacy unit tests take.
        """
        return self._close_session_with_pricing(
            session_id,
            input_tokens,
            output_tokens,
            usage,
            pricing=self._pricing,
        )

    def record_background_usage(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        usage: dict | None = None,
    ) -> float:
        """Record one short-lived non-voice job against the spend ledger.

        Background jobs such as async research do not have a live voice
        session id to close. Keep that as an explicit API so callers that
        forget a normal session id fail loudly instead of creating a
        phantom row.
        """
        session_id = self.open_session(provider)
        pricing = (
            pricing_for_model(model, overrides=self._pricing_overrides)
            if model
            else self._pricing
        )
        return self._close_session_with_pricing(
            session_id,
            input_tokens,
            output_tokens,
            usage,
            pricing=pricing,
        )

    def _close_session_with_pricing(
        self,
        session_id: int,
        input_tokens: int,
        output_tokens: int,
        usage: dict | None,
        *,
        pricing: Pricing,
    ) -> float:
        if usage is None:
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        cost = pricing.estimate_cost(usage)
        # No row to update when open_session's write failed — the cost
        # estimate is still returned for the caller's logging, it just
        # isn't persisted (see _UNRECORDED_SESSION / open_session).
        if session_id == _UNRECORDED_SESSION:
            return cost
        # Fail-soft for the same reason open_session is: a telemetry
        # write must never break the voice turn. The cost estimate is
        # already computed above, so a failed persist just means this
        # turn drops out of the stored 24 h spend total.
        try:
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
        except sqlite3.Error as e:
            logger.warning(
                "usage: close_session write failed (%s: %s); cost unrecorded",
                type(e).__name__, e,
            )
            self._note_write_failed(e)
            return cost
        self._note_write_ok()
        return cost

    def _note_write_ok(self) -> None:
        """A successful write clears degraded state, emitting ONE recovery
        event on the failure->ok transition (not per successful write)."""
        if self._write_health.consecutive_failures:
            log_event(
                logger,
                "usage.write_recovered",
                after_failures=self._write_health.consecutive_failures,
                note="usage.db writable again; spend recording resumed",
            )
            self._write_health = WriteHealth()

    def _note_write_failed(self, exc: Exception) -> None:
        """A failed write marks accounting degraded. Emits ONE structured event
        on the ok->degraded transition; subsequent failures bump the counter
        without re-emitting, so a persistent failure does not spam the journal.
        This is the monitorable signal (beyond the per-call WARNING) that the
        daily spend cap can no longer enforce: turns are still served, but their
        cost isn't persisted, so the rolling 24 h total stops growing."""
        first = self._write_health.consecutive_failures == 0
        self._write_health = WriteHealth(
            consecutive_failures=self._write_health.consecutive_failures + 1,
            last_error=f"{type(exc).__name__}: {exc}",
            last_failure_at=datetime.now(timezone.utc).isoformat(),
        )
        if first:
            log_event(
                logger,
                "usage.write_degraded",
                error_type=type(exc).__name__,
                note=(
                    "usage.db writes failing; turns still served but their cost "
                    "is unrecorded, so the daily spend cap cannot enforce until "
                    "writes recover"
                ),
                level=logging.WARNING,
            )

    @property
    def write_degraded(self) -> bool:
        """True once a usage write has failed and not yet recovered. Surfaced as
        /state.voice.usage_tracking_degraded so the spend-cap status can warn
        that recorded spend may be stale instead of silently flatlining."""
        return self._write_health.consecutive_failures > 0

    def _connection_interval_columns(self) -> set[str]:
        return {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(connection_intervals)")
        }

    def _connection_interval_kind_column_exists(self) -> bool:
        return "kind" in self._connection_interval_columns()

    def _ensure_connection_interval_kind_column(self) -> None:
        """Mark pre-fix uptime rows as legacy so they stop counting as spend.

        Before 2026-06-24 this table represented warm WebSocket uptime.
        After the Grok billing investigation it represents active realtime
        turn duration. Existing rows have the old meaning, so a value-preserving
        migration would preserve the bug; instead we keep the rows for
        forensics and tag them out of spend queries.
        """
        if self._connection_interval_kind_column_exists():
            return
        self._conn.execute(
            "ALTER TABLE connection_intervals "
            f"ADD COLUMN kind TEXT NOT NULL DEFAULT '{_LEGACY_CONNECTION_UPTIME_KIND}'"
        )

    # ------------------------------------------------------------------
    # Billable realtime-activity intervals (time-billed providers, e.g. Grok)
    # ------------------------------------------------------------------
    def record_billable_activity_open(
        self, provider: str, rate_per_hour_usd: float,
    ) -> None:
        """Open a billable activity interval — called when a time-billed
        provider starts a voice turn. The rate is snapshotted so a later
        rate change doesn't retroactively re-price past activity time."""
        self._conn.execute(
            "INSERT INTO connection_intervals "
            "(provider, opened_at, rate_per_hour_usd, kind) VALUES (?, ?, ?, ?)",
            (
                provider,
                datetime.now(timezone.utc).isoformat(),
                float(rate_per_hour_usd),
                _BILLABLE_ACTIVITY_KIND,
            ),
        )

    def record_billable_activity_close(self) -> None:
        """Close any open billable activity interval — called on turn
        release / loss. Closes all open rows; there is only ever one
        active voice turn, so this targets exactly it."""
        self._conn.execute(
            "UPDATE connection_intervals SET closed_at = ? "
            "WHERE closed_at IS NULL AND kind = ?",
            (datetime.now(timezone.utc).isoformat(), _BILLABLE_ACTIVITY_KIND),
        )

    def close_dangling_intervals(self) -> None:
        """Conservatively close intervals a crash left open (no clean
        teardown ran): set closed_at = opened_at (zero duration) so a
        stale open row can't bill phantom activity up to 'now' on the next
        read. Run once at daemon start (BillableActivityMeter), never
        from the read-only dashboard path."""
        self._conn.execute(
            "UPDATE connection_intervals SET closed_at = opened_at "
            "WHERE closed_at IS NULL AND kind = ?",
            (_BILLABLE_ACTIVITY_KIND,),
        )

    def _time_billed_spend_by_provider(
        self, since: datetime, until: datetime,
    ) -> dict[str, float]:
        """Billable realtime-activity cost per provider over ``[since, until]``.
        Open intervals (closed_at IS NULL) are billed up to ``until``.
        Returns ``{}`` when no intervals overlap the window."""
        if not self._connection_intervals_have_kind:
            return {}
        cur = self._conn.execute(
            "SELECT provider, opened_at, closed_at, rate_per_hour_usd "
            "FROM connection_intervals "
            "WHERE kind = ? "
            "AND opened_at <= ? AND (closed_at IS NULL OR closed_at >= ?)",
            (_BILLABLE_ACTIVITY_KIND, until.isoformat(), since.isoformat()),
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
        their billable activity cost is folded into ``cost_usd`` here."""
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
        # Fold billable activity cost into each provider's total, and add
        # a row for any provider that has active time but no session
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


class AggregateUsageReader:
    """Sums the reader trio across every per-surface ledger file.

    Implements exactly the three methods the display / cap surfaces call —
    ``spend_last_24h_usd`` / ``spend_month_to_date_usd`` /
    ``session_count_today_utc`` — so it duck-types as a ``UsageStore`` wherever
    those are read (and behind ``SpendCap``, which only calls
    ``spend_last_24h_usd``). It never writes.

    Members are a mix of already-open stores (the voice daemon passes its own
    live writer, so its just-recorded spend is visible without a re-open) and
    paths (opened READ-ONLY, LAZILY, on EVERY read). A missing or unopenable
    path-member contributes zero — logged at DEBUG, never WARN — because the
    voice daemon runs for weeks and MUST pick up a tuning DB that
    ``jasper-correction-web`` creates later without a restart. A failed open is
    never cached: the next read retries, so the member appears the moment it
    exists. This matches the module's fail-open direction (an unreadable ledger
    reads as zero spend, never blocks)."""

    def __init__(
        self,
        *,
        stores: "list[UsageStore] | None" = None,
        paths: list[str] | None = None,
    ) -> None:
        self._stores = list(stores or [])
        self._paths = list(paths or [])

    def _read_all(self, method_name: str) -> "list[float | int]":
        values: list[float | int] = []
        for store in self._stores:
            try:
                values.append(getattr(store, method_name)())
            except sqlite3.Error as e:
                logger.debug(
                    "usage aggregate: open store %s failed (%s: %s); "
                    "counting zero",
                    method_name, type(e).__name__, e,
                )
        for path in self._paths:
            if not os.path.exists(path):
                # The common steady state before a surface has ever spent —
                # DEBUG, not WARN, so weeks of uptime don't spam the journal.
                logger.debug(
                    "usage aggregate: member %s absent; counting zero", path,
                )
                continue
            try:
                # read_only never creates the file and never writes, so a
                # reader cannot re-own another surface's DB. Open fresh per
                # read and close immediately — never cache a handle (or a
                # failed open).
                store = UsageStore(path, read_only=True)
                try:
                    values.append(getattr(store, method_name)())
                finally:
                    store._conn.close()
            except sqlite3.Error as e:
                logger.debug(
                    "usage aggregate: member %s unreadable (%s: %s); "
                    "counting zero", path, type(e).__name__, e,
                )
        return values

    def spend_last_24h_usd(self) -> float:
        return float(sum(self._read_all("spend_last_24h_usd")))

    def spend_month_to_date_usd(self) -> float:
        return float(sum(self._read_all("spend_month_to_date_usd")))

    def session_count_today_utc(self) -> int:
        return int(sum(self._read_all("session_count_today_utc")))


def household_usage_reader(
    usage_db_path: str, *, main_store: "UsageStore | None" = None,
) -> AggregateUsageReader:
    """THE definition of "household spend": the voice usage DB + the tuning
    sibling, summed at read time.

    This is the single place the member list lives. Every cap and display
    surface builds its reader here, so "household spend" has exactly one
    definition and consolidating later (if correction-web ever de-roots into
    the jasper group and can share one DB) is a one-function edit.

    ``main_store`` lets the voice daemon pass its OWN open writer instance so
    the reader sees spend it just recorded (its live connection) rather than a
    stale read-only reopen; the tuning sibling is always a path (correction-web
    owns that file, this process only reads it). Callers without a live writer
    (the /voice card, doctor) pass both as paths."""
    tuning_db = tuning_usage_db_path(usage_db_path)
    if main_store is not None:
        return AggregateUsageReader(stores=[main_store], paths=[tuning_db])
    return AggregateUsageReader(paths=[usage_db_path, tuning_db])


class SpendReader(Protocol):
    """The one method SpendCap reads from its store. Both ``UsageStore`` and
    ``AggregateUsageReader`` satisfy it structurally, so the breaker takes a
    single live DB or the household aggregate interchangeably."""

    def spend_last_24h_usd(self) -> float: ...


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
        store: SpendReader,
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


class BillableActivityMeter:
    """Meters billable realtime activity for a time-billed provider.

    Grok Voice publishes a flat $/hour realtime rate, not per-token.
    The provider dashboard shows idle warm WebSocket time is not billed
    like active conversation time, so the adapter marks this meter only
    around voice turns.

    The voice daemon wires one meter to the active connection — only when
    ``pricing.flat_per_hour_usd > 0`` — before ``start()``. The connection
    calls ``mark_started()`` when a turn starts and ``mark_ended()`` when
    that turn releases or is lost. Cost is folded into the spend queries
    from those intervals.

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
        # phantom activity against this run.
        try:
            store.close_dangling_intervals()
        except Exception as e:  # noqa: BLE001
            logger.warning("activity meter: dangling cleanup failed: %s", e)

    def mark_started(self) -> None:
        try:
            self._store.record_billable_activity_open(self._provider, self._rate)
        except Exception as e:  # noqa: BLE001
            logger.warning("activity meter: open failed: %s", e)

    def mark_ended(self) -> None:
        try:
            self._store.record_billable_activity_close()
        except Exception as e:  # noqa: BLE001
            logger.warning("activity meter: close failed: %s", e)
