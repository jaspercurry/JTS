# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Paid tuning-assistant backend for the Room correction wizard.

This module owns the bounded provider call, shared per-process throttle,
household spend gate, and tuning-ledger write.  It deliberately does not own
HTTP parsing, session acquisition, proposal acceptance, or live correction
apply; those remain in :mod:`jasper.web.correction_setup`.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Protocol

from ..log_event import log_event

logger = logging.getLogger(__name__)


class AdvisorCall(Protocol):
    """One narrow provider operation closed over its Room context by the host."""

    def __call__(
        self,
        *,
        user_message: str | None,
        timeout_sec: float,
        max_output_tokens: int,
    ) -> dict[str, Any]: ...


def _tuning_timeout_sec() -> float:
    """Return the positive provider-call timeout, defaulting safely to 90 s."""
    try:
        value = float(os.environ.get("JASPER_TUNING_LLM_TIMEOUT_SEC", "90") or "90")
    except ValueError:
        return 90.0
    return value if value > 0 else 90.0


# A frontier text model answering the interpret/propose packet normally takes
# only a few seconds.  Ninety seconds is a generous ceiling that still bounds
# a stalled provider connection on the Pi web process.
TUNING_LLM_TIMEOUT_SEC = _tuning_timeout_sec()

# Minimum spacing between paid interpret/propose attempts.  The timestamp is
# shared by both routes and stamped before the provider call so concurrent
# retries cannot silently burn spend.
TUNING_LLM_MIN_INTERVAL_SEC = 3.0


class SpendCapExceeded(RuntimeError):
    """The household daily spend cap blocks this paid tuning call.

    This is distinct from :class:`TuningBusy`: the HTTP adapter maps it to
    429 with rollover wording because waiting a few seconds cannot clear it.
    """


class TuningBusy(RuntimeError):
    """A paid tuning call was attempted inside the minimum interval."""


class TuningProviderError(RuntimeError):
    """The tuning provider rejected or failed the bounded request."""


# Mutable single-slot state lets tests reset the process-wide gate without
# replacing the object held by another reference.  ThreadingHTTPServer runs
# each request in its own thread, so the timestamp is lock-protected.
_tuning_paid_call_lock = threading.Lock()
_tuning_last_paid_call: list[float] = [0.0]


def _tuning_paid_call_gate() -> None:
    """Refuse a second paid attempt inside the shared minimum interval."""
    now = time.monotonic()
    with _tuning_paid_call_lock:
        since = now - _tuning_last_paid_call[0]
        if since < TUNING_LLM_MIN_INTERVAL_SEC:
            raise TuningBusy(
                "the tuning assistant just made a paid call — wait a "
                "moment and tap again"
            )
        _tuning_last_paid_call[0] = now


# jasper-correction-web is the sole writer of the sibling tuning ledger; it
# must never open jasper-voice's usage.db read-write.  Use one fresh UsageStore
# per record because sqlite connections are thread-bound, and serialize
# open/write/close so concurrent handler threads cannot race.  Per-call open
# also makes the 0644 permission repair self-healing after a partial create.
_tuning_usage_lock = threading.Lock()

# Warn once per process/model when an operator-selected model is unpriced.
# Mutated under _tuning_usage_lock with the rest of the ledger state.
_tuning_unpriced_warned: set[str] = set()


def _warn_if_tuning_model_unpriced(model: str, overrides: dict[str, dict]) -> None:
    """Warn once when the tuning model has no bundled or operator rate."""
    from jasper.usage import pricing_for_model

    if not pricing_for_model(model, overrides=overrides).label.startswith(
        "unpriced:"
    ):
        return
    if model in _tuning_unpriced_warned:
        return
    _tuning_unpriced_warned.add(model)
    log_event(
        logger,
        "pricing.unpriced",
        model=model,
        surface="tuning",
        note=(
            "no rate available for the tuning model; its paid calls record "
            "$0 and the daily spend cap cannot bound tuning spend until a "
            "rate is set at /voice"
        ),
        level=logging.WARNING,
    )


def _heal_tuning_ledger_mode(tuning_db: str) -> None:
    """Keep the root-written tuning ledger readable by jasper-group readers."""
    try:
        mode = os.stat(tuning_db).st_mode & 0o777
        if mode != 0o644:
            os.chmod(tuning_db, 0o644)
    except OSError:
        logger.warning(
            "tuning ledger mode heal failed for %s",
            tuning_db,
            exc_info=True,
        )


def _record_tuning_spend(out: dict[str, Any], usage_db: str) -> None:
    """Record one paid call, failing soft on filesystem and SQLite errors."""
    from jasper.calibration_agent.key_provisioning import resolve_tuning_model

    usage_in = out.get("usage") or {}
    try:
        input_tokens = int(usage_in.get("input_tokens") or 0)
        output_tokens = int(usage_in.get("output_tokens") or 0)
    except (TypeError, ValueError):
        input_tokens = output_tokens = 0

    # The advisor reports aggregate text tokens.  Supply the modality details
    # expected by usage pricing so text-only tuning calls never fall through to
    # an absent audio rate and record an incorrect zero cost.
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_token_details": {"text_tokens": input_tokens},
        "output_token_details": {"text_tokens": output_tokens},
    }
    model = resolve_tuning_model()
    from jasper.usage import (
        UsageStore,
        load_pricing_overrides,
        tuning_usage_db_path,
    )

    tuning_db = tuning_usage_db_path(usage_db)
    overrides = load_pricing_overrides()
    try:
        with _tuning_usage_lock:
            _warn_if_tuning_model_unpriced(model, overrides)
            store = UsageStore(tuning_db, pricing_overrides=overrides)
            try:
                _heal_tuning_ledger_mode(tuning_db)
                cost = store.record_background_usage(
                    provider="openai",
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    usage=usage,
                )
                write_degraded = store.write_degraded
            finally:
                store._conn.close()
    except (OSError, sqlite3.Error) as exc:
        log_event(
            logger,
            "tuning_spend.record_failed",
            level=logging.WARNING,
            error=type(exc).__name__,
        )
        return
    if write_degraded:
        log_event(
            logger,
            "tuning_spend.record_failed",
            level=logging.WARNING,
            error="write_degraded",
        )
        return
    log_event(
        logger,
        "tuning_spend.recorded",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost, 6),
    )


def _spend_settings() -> tuple[str, float, float]:
    """Read the usage DB, daily cap, and safety multiplier fresh per call.

    The wizard-owned voice-provider file wins over ``os.environ`` because the
    socket-activated correction process can outlive a settings save and does
    not source that file itself.  Malformed values degrade to the shared usage
    defaults so an environment typo cannot turn the optional tuning surface
    into a server error.  This intentionally does not use ``Config.from_env``:
    that parser requires a configured voice provider, while tuning can be
    valid before the household selects one.
    """
    from jasper.env_load import read_env_file_state
    from jasper.usage import (
        DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
        DEFAULT_DAILY_SPEND_CAP_USD,
        DEFAULT_USAGE_DB,
    )
    from jasper.voice.provider_state import PROVIDER_FILE

    provider_file = os.environ.get("JASPER_VOICE_PROVIDER_FILE", PROVIDER_FILE)
    file_state = read_env_file_state(provider_file)
    file_values = file_state.values if file_state.loaded else {}

    def _value(name: str) -> str:
        return (file_values.get(name) or os.environ.get(name) or "").strip()

    def _float(name: str, default: float) -> float:
        raw = _value(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    usage_db = _value("JASPER_USAGE_DB") or DEFAULT_USAGE_DB
    cap_usd = _float("JASPER_DAILY_SPEND_CAP_USD", DEFAULT_DAILY_SPEND_CAP_USD)
    multiplier = _float(
        "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER",
        DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
    )
    return usage_db, cap_usd, multiplier


def _spend_usage_db() -> str:
    """Return the fresh voice DB path used to locate its tuning sibling."""
    return _spend_settings()[0]


def _tuning_spend_cap_gate() -> None:
    """Refuse a paid call when aggregate household spend reaches its cap.

    The household reader retains the established fail-open posture for an
    unreadable ledger; an accounting failure never blocks the requested call.
    """
    from jasper.usage import SpendCap, household_usage_reader

    usage_db, cap_usd, multiplier = _spend_settings()
    reader = household_usage_reader(usage_db)
    cap = SpendCap(reader, cap_usd, multiplier)
    if cap.allowed():
        return
    log_event(
        logger,
        "tuning_spend.cap_blocked",
        level=logging.WARNING,
        spend_last_24h_usd=round(reader.spend_last_24h_usd(), 6),
        safety_multiplier=multiplier,
        cap_usd=cap_usd,
    )
    raise SpendCapExceeded(
        "daily spend cap reached — the tuning assistant will be "
        "available again after the daily rollover"
    )


def interpret(
    advisor_call: AdvisorCall,
    *,
    user_message: str | None,
) -> dict[str, Any]:
    """Run one bounded, spend-gated read-only Room interpretation."""
    from jasper.calibration_agent import model_client

    _tuning_paid_call_gate()
    _tuning_spend_cap_gate()
    try:
        out = advisor_call(
            user_message=user_message,
            timeout_sec=float(TUNING_LLM_TIMEOUT_SEC),
            max_output_tokens=model_client.TUNING_LLM_MAX_OUTPUT_TOKENS,
        )
    except model_client.AdvisorModelError as exc:
        raise TuningProviderError(str(exc)) from exc
    # Read the ledger setting again after the provider returns, preserving the
    # existing fresh-settings behavior if the wizard changes it mid-call.
    _record_tuning_spend(out, _spend_usage_db())
    return out


def propose(
    advisor_call: AdvisorCall,
    *,
    user_message: str | None,
) -> dict[str, Any]:
    """Run one bounded, spend-gated proposal call without applying it."""
    from jasper.calibration_agent import model_client

    _tuning_paid_call_gate()
    _tuning_spend_cap_gate()
    try:
        out = advisor_call(
            user_message=user_message,
            timeout_sec=float(TUNING_LLM_TIMEOUT_SEC),
            max_output_tokens=model_client.TUNING_LLM_MAX_OUTPUT_TOKENS,
        )
    except model_client.AdvisorModelError as exc:
        raise TuningProviderError(str(exc)) from exc
    _record_tuning_spend(out, _spend_usage_db())
    return out
