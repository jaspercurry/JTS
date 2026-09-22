# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice spending status and pricing validation; no page rendering."""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

from jasper.voice.catalog import ProviderCatalogEntry
from jasper.usage import (
    AggregateUsageReader, DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
    DEFAULT_DAILY_SPEND_CAP_USD, DEFAULT_USAGE_DB, household_usage_reader,
    pricing_for_model, tuning_usage_db_path, sanitize_pricing_models,
)
from ._common import value_for_env as _value_for

logger = logging.getLogger(__name__)


def _float_from_state(
    state: dict[str, str],
    env_var: str,
    default: float,
) -> tuple[float, str, str | None]:
    raw = _value_for(state, env_var, f"{default:g}").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default, raw, f"{env_var} is not numeric; showing default {default:g}."
    if not math.isfinite(value):
        return default, raw, f"{env_var} is not finite; showing default {default:g}."
    return value, raw, None


def _fmt_env_money(value: float) -> str:
    if value == 0:
        return "0"
    return f"{value:.2f}"


def _fmt_env_float(value: float) -> str:
    return f"{value:g}"


def _read_spend_cap_status(state: dict[str, str]) -> dict[str, Any]:
    cap_usd, cap_raw, cap_error = _float_from_state(
        state,
        "JASPER_DAILY_SPEND_CAP_USD",
        DEFAULT_DAILY_SPEND_CAP_USD,
    )
    safety_multiplier, multiplier_raw, multiplier_error = _float_from_state(
        state,
        "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER",
        DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
    )
    errors = [e for e in (cap_error, multiplier_error) if e]
    if cap_usd < 0:
        errors.append("JASPER_DAILY_SPEND_CAP_USD is below 0; showing 0.")
    if safety_multiplier < 1:
        errors.append(
            "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER is below 1; showing 1.",
        )
    cap_usd = max(0.0, cap_usd)
    safety_multiplier = max(1.0, safety_multiplier)
    usage_db = _value_for(state, "JASPER_USAGE_DB", DEFAULT_USAGE_DB)
    # Either household ledger member counts as "usage exists": on a
    # tuning-only box (the tuning assistant used before the first voice turn)
    # the daemon's cap already counts that spend, so the card must render the
    # real household dollars rather than "no usage yet". Mirrors the doctor's
    # two-file treatment in jasper.cli.doctor.voice.check_spend_cap.
    usage_available = os.path.exists(usage_db) or os.path.exists(
        tuning_usage_db_path(usage_db)
    )
    usage_error = ""
    spend_last_24h = 0.0
    month_to_date = 0.0
    sessions_today = 0
    if usage_available:
        try:
            # Read HOUSEHOLD spend: the voice ledger plus the sibling
            # tuning-spend ledger, still summed into household spend. The
            # aggregate opens each member read_only and lazily —
            # this runs in jasper-web (root), not jasper-voice, and a
            # read-write open could re-own usage.db and lock the voice daemon
            # out of its own DB (see UsageStore.__init__).
            reader = household_usage_reader(usage_db)
            spend_last_24h = reader.spend_last_24h_usd()
            month_to_date = reader.spend_month_to_date_usd()
            # "Turns today" stays VOICE-only: a tuning-ledger row is not a
            # voice turn, and folding those into a figure labelled "Turns
            # today" would over-count. The DOLLAR figures above deliberately
            # include tuning (household spend); the card carries a hint saying
            # so. Single-member aggregate = the same lazy/read-only/fail-open
            # discipline for one file.
            sessions_today = AggregateUsageReader(
                paths=[usage_db],
            ).session_count_today_utc()
        except Exception as e:  # noqa: BLE001
            usage_available = False
            usage_error = str(e)
            logger.warning("spend-cap status read failed: %s", e)
    disabled = cap_usd == 0
    padded_spend = spend_last_24h * safety_multiplier
    return {
        "cap_usd": cap_usd,
        "cap_raw": cap_raw,
        "safety_multiplier": safety_multiplier,
        "multiplier_raw": multiplier_raw,
        "errors": errors,
        "usage_db": usage_db,
        "usage_available": usage_available,
        "usage_error": usage_error,
        "disabled": disabled,
        "spend_last_24h_usd": spend_last_24h,
        "padded_spend_usd": padded_spend,
        "month_to_date_usd": month_to_date,
        "sessions_today": sessions_today,
        "remaining_usd": None if disabled else max(0.0, cap_usd - padded_spend),
        "allowed": disabled or not usage_available or padded_spend < cap_usd,
    }


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_spend_float(raw: str, *, label: str, minimum: float) -> tuple[float, str | None]:
    text = (raw or "").strip()
    if not text:
        return 0.0, f"{label} is required."
    try:
        value = float(text)
    except ValueError:
        return 0.0, f"{label} must be a number."
    if not math.isfinite(value):
        return 0.0, f"{label} must be a finite number."
    if value < minimum:
        return 0.0, f"{label} must be at least {minimum:g}."
    return value, None


def _apply_spend_cap(
    form: dict[str, str],
    current: dict[str, str],
) -> tuple[dict[str, str], str | None]:
    cap_usd, cap_err = _parse_spend_float(
        form.get("daily_spend_cap_usd") or "",
        label="Rolling 24h cap",
        minimum=0.0,
    )
    if cap_err is not None:
        return current, cap_err
    safety_multiplier, multiplier_err = _parse_spend_float(
        form.get("daily_spend_cap_safety_multiplier") or "",
        label="Safety multiplier",
        minimum=1.0,
    )
    if multiplier_err is not None:
        return current, multiplier_err
    new = dict(current)
    new["JASPER_DAILY_SPEND_CAP_USD"] = _fmt_env_money(cap_usd)
    new["JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER"] = _fmt_env_float(
        safety_multiplier,
    )
    return {k: v for k, v in new.items() if v}, None


def _apply_pricing_save(
    form: dict[str, str],
    provider: ProviderCatalogEntry,
    model_ids: list[str],
    existing: dict[str, dict],
) -> dict[str, dict]:
    """Merge one provider's posted per-model rates into the existing
    override map and return the new full ``{model_id: {field: float}}``.

    Sparse: a blank field, a non-numeric/negative value, or a value equal
    to the bundled default is omitted (→ falls back to the default). A
    model whose fields are all omitted is removed entirely (a reset). Only
    the posted provider's models are touched; other providers' overrides
    are preserved."""
    buckets = provider.pricing_buckets
    result = {mid: dict(fields) for mid, fields in existing.items()}
    for model_id in model_ids:
        default = pricing_for_model(model_id)
        sparse: dict[str, float] = {}
        for field in buckets:
            raw = (form.get(f"price__{model_id}__{field}") or "").strip()
            if not raw:
                continue
            try:
                val = float(raw)
            except ValueError:
                continue
            if val < 0:
                continue
            if abs(val - getattr(default, field)) < 1e-9:
                continue  # at the bundled default → keep file sparse
            sparse[field] = val
        if sparse:
            result[model_id] = sparse
        else:
            result.pop(model_id, None)  # reset: no overrides for this model
    return result


def _apply_pricing_paste(
    raw_text: str,
) -> tuple[dict[str, dict] | None, str, str | None]:
    """Parse a chatbot's pasted pricing JSON → ``(models_map, as_of, None)``
    or ``(None, "", error_message)``. Tolerant of a ```json fence and of a
    bare ``{model_id: {...}}`` map without the ``{"models": ...}`` wrapper.
    Validation reuses ``sanitize_pricing_models`` so pasted JSON is held to
    the same rules as a hand-edited override file. ``as_of`` is the pasted
    value (the date the chatbot researched the prices), preserved so the
    file records data vintage rather than import time."""
    text = (raw_text or "").strip()
    if not text:
        return None, "", "Paste the JSON your chatbot produced first."
    if text.startswith("```"):
        # Strip a leading ```/```json fence line and a trailing ``` fence.
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        return None, "", f"That doesn't parse as JSON ({e})."
    if not isinstance(data, dict):
        return None, "", 'Expected a JSON object with a "models" map.'
    # Accept either {"models": {...}} or a bare {model_id: {...}} map.
    models = sanitize_pricing_models(data.get("models", data))
    if not models:
        return None, "", (
            "No usable model rates found. Expected "
            '{"models": {"<model-id>": {"audio_input_per_million_usd": '
            "<number>, ...}}}."
        )
    raw_as_of = data.get("as_of")
    as_of = raw_as_of if isinstance(raw_as_of, str) else ""
    return models, as_of, None


def _sparsify_overrides(models: dict[str, dict]) -> dict[str, dict]:
    """Drop fields equal to the bundled default, and models left empty, so
    ``pricing.json`` stays a minimal sparse override (the invariant the
    per-provider editor maintains). Idempotent on already-sparse maps."""
    out: dict[str, dict] = {}
    for model_id, fields in models.items():
        default = pricing_for_model(model_id)
        sparse = {
            k: v for k, v in fields.items()
            if abs(float(v) - getattr(default, k, 0.0)) > 1e-9
        }
        if sparse:
            out[model_id] = sparse
    return out
