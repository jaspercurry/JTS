# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Page rendering for the /assistant/voice/ wizard."""
from __future__ import annotations

import html
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

from jasper.voice.catalog import (
    PROVIDERS,
    ProviderCatalogEntry,
    default_model_id,
    default_voice_id,
)
from jasper.voice.provider_state import resolve_active_provider
from jasper.voice.model_discovery import DiscoverySnapshot
from jasper.usage import (
    AggregateUsageReader,
    DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
    DEFAULT_DAILY_SPEND_CAP_USD,
    DEFAULT_USAGE_DB,
    household_usage_reader,
    pricing_for_model,
    tuning_usage_db_path,
)

from ._common import (
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    mask_secret,
    pair_banner_html,
    value_for_env as _value_for,
)

logger = logging.getLogger(__name__)


# Page-specific stylesheet served static from /assets/ (the same path as
# app.css + the fonts). Only the visuals app.css doesn't already cover
# live here: the active-provider radio group, the pricing-rate grid, and
# the readonly research-prompt textarea sizing. Cache-busted by build SHA
# via canonical_page(page_css_href=...).
VOICE_PAGE_CSS_HREF = "/assets/voice/voice.css"


def _provider_is_configured(
    state: dict[str, str],
    provider: ProviderCatalogEntry,
) -> bool:
    return bool(_value_for(state, provider.key_env))


def _active_provider_id(state: dict[str, str]) -> str:
    """Active provider per the wizard's state (or the env if the wizard
    file hasn't been written yet). Returns empty string when no
    provider has been chosen yet — the UI then renders with no radio
    selected and no card highlighted, so the user has to make an
    explicit choice. The earlier behaviour silently fell back to
    ``gemini``, which produced the stale-default class of bug where
    `/etc/jasper/jasper.env` and `/var/lib/jasper/voice_provider.env`
    disagreed about what was active."""
    active = _value_for(state, "JASPER_VOICE_PROVIDER", "")
    # Same validation rule as jasper-control (resolve_active_provider):
    # a valid id or empty, never a default. _value_for keeps the wizard's
    # file-then-env lookup so an operator-set value in jasper.env still
    # displays here.
    return resolve_active_provider({"JASPER_VOICE_PROVIDER": active})


# ----------------------------------------------------------------------
# HTML rendering (canonical design system).
# ----------------------------------------------------------------------


def _active_radio_html(state: dict[str, str]) -> str:
    """The 'use this provider' radio block at the top of the page.

    Disabled radios are also marked aria-disabled so screen readers
    report the correct state — the disabled attribute alone suppresses
    the underlying input but the wrapping <label> handles the click."""
    active = _active_provider_id(state)
    rows = []
    for p in PROVIDERS:
        configured = _provider_is_configured(state, p)
        is_active = active == p.id
        radio_attrs = [
            "type=\"radio\"",
            "name=\"active\"",
            f"value=\"{p.id}\"",
            f'data-provider-radio="{p.id}"',
        ]
        if is_active:
            radio_attrs.append("checked")
        if not configured:
            radio_attrs.append("disabled")
        radio_input = f"<input {' '.join(radio_attrs)}>"
        cls = "provider-radio is-disabled" if not configured else "provider-radio"
        aria_disabled = ' aria-disabled="true"' if not configured else ""
        originally_disabled = (
            ' data-provider-radio-originally-disabled="1"'
            if not configured else ""
        )
        status = (
            "configured" if configured
            else f"no {p.key_env} yet — add a key first"
        )
        rows.append(f"""
        <label class="{cls}" data-provider-radio-row="{p.id}"{originally_disabled}{aria_disabled}>
          {radio_input}
          <span class="provider-radio__name">{html.escape(p.label)}</span>
          <span class="provider-radio__price">{html.escape(p.cost_hint)}</span>
          <span class="provider-radio__status" data-provider-radio-status="{p.id}">{html.escape(status)}</span>
        </label>""")
    return f"""
    <div class="info-card active-group">
      <p class="eyebrow">Use this provider for voice</p>
      <p class="info-card__hint">Only providers with a saved or newly pasted API key can be selected.</p>
      {''.join(rows)}
    </div>"""


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


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.4f}"


def _fmt_env_money(value: float) -> str:
    if value == 0:
        return "0"
    return f"{value:.2f}"


def _fmt_env_float(value: float) -> str:
    return f"{value:g}"


def _badge_html(label: str, tone: str) -> str:
    return f'<span class="badge badge--{tone}">{html.escape(label)}</span>'


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


def _spend_cap_section_html(state: dict[str, str], csrf_token: str) -> str:
    status = _read_spend_cap_status(state)
    disabled = bool(status["disabled"])
    if disabled:
        status_badge = _badge_html("disabled", "idle")
        compare = "disabled"
        remaining = "disabled"
    elif not status["usage_available"]:
        status_badge = _badge_html("no usage yet", "idle")
        compare = "—"
        remaining = _fmt_usd(status["cap_usd"])
    elif status["allowed"]:
        status_badge = _badge_html("available", "ok")
        compare = (
            f'{_fmt_usd(status["padded_spend_usd"])} / '
            f'{_fmt_usd(status["cap_usd"])}'
        )
        remaining = _fmt_usd(status["remaining_usd"])
    else:
        status_badge = _badge_html("blocked", "danger")
        compare = (
            f'{_fmt_usd(status["padded_spend_usd"])} / '
            f'{_fmt_usd(status["cap_usd"])}'
        )
        remaining = "$0.0000"
    notes = []
    if status["errors"]:
        notes.extend(status["errors"])
    if status["usage_error"]:
        notes.append(f'Could not read usage ledger: {status["usage_error"]}')
    elif not status["usage_available"]:
        notes.append("No usage ledger exists yet; the first voice turn creates one.")
    note_html = "".join(
        f'<p class="form-hint">{html.escape(note)}</p>' for note in notes
    )
    cap_value = html.escape(_fmt_env_money(status["cap_usd"]), quote=True)
    multiplier_value = html.escape(
        _fmt_env_float(status["safety_multiplier"]),
        quote=True,
    )
    return f"""
  <section class="section">
    <h2 class="section__title">Voice spend cap</h2>
    <div class="info-card spend-cap-card">
      <dl class="deflist spend-cap__stats">
        <dt>Status</dt><dd>{status_badge}</dd>
        <dt>Rolling 24h spend</dt><dd>{_fmt_usd(status["spend_last_24h_usd"]) if status["usage_available"] else "—"}</dd>
        <dt>Cap comparison</dt><dd>{compare}</dd>
        <dt>Remaining</dt><dd>{remaining}</dd>
        <dt>Month to date</dt><dd>{_fmt_usd(status["month_to_date_usd"]) if status["usage_available"] else "—"}</dd>
        <dt>Turns today</dt><dd>{html.escape(str(status["sessions_today"])) if status["usage_available"] else "—"}</dd>
      </dl>
      <p class="form-hint">Spend figures include the tuning assistant's paid calls; Turns today counts voice turns only.</p>
      {note_html}
      <form method="post" action="spend-cap" class="spend-cap__form">
        {csrf_field_html(csrf_token)}
        <div class="field">
          <label for="daily_spend_cap_usd">Rolling 24h cap (USD)</label>
          <input id="daily_spend_cap_usd" name="daily_spend_cap_usd"
                 type="number" min="0" step="0.01" inputmode="decimal"
                 value="{cap_value}" required>
          <p class="form-hint">Set to 0 to disable the cap.</p>
        </div>
        <div class="field">
          <label for="daily_spend_cap_safety_multiplier">Safety multiplier</label>
          <input id="daily_spend_cap_safety_multiplier"
                 name="daily_spend_cap_safety_multiplier"
                 type="number" min="1" step="0.05" inputmode="decimal"
                 value="{multiplier_value}" required>
          <p class="form-hint">The breaker compares rolling spend times this multiplier to the cap.</p>
        </div>
        <div class="form-actions">
          <button class="btn btn--default" type="submit">Save spend cap</button>
        </div>
      </form>
    </div>
  </section>"""


def _model_select_html(
    provider: ProviderCatalogEntry,
    current: str,
    discovered: DiscoverySnapshot | None = None,
) -> str:
    rows = []
    seen = set()
    for model in provider.models:
        sel = " selected" if model.id == current else ""
        rows.append(
            f'<option value="{html.escape(model.id)}"{sel}>'
            f'{html.escape(model.display_label)}</option>'
        )
        seen.add(model.id)
    if discovered is not None:
        for model_id in discovered.models:
            if model_id in seen:
                continue
            sel = " selected" if model_id == current else ""
            rows.append(
                f'<option value="{html.escape(model_id)}"{sel}>'
                f'{html.escape(model_id)} '
                f'(experimental; discovered)</option>'
            )
            seen.add(model_id)
    # If the daemon's configured model is something the wizard doesn't
    # know about, surface it as a custom row so the user doesn't get
    # silently switched to something else when they hit Save.
    if current and current not in seen:
        rows.insert(
            0,
            f'<option value="{html.escape(current)}" selected>'
            f'{html.escape(current)} (custom; experimental)</option>',
        )
    # `form="save-form"` associates this input with the outer
    # save-form by ID — necessary because the cards visually live
    # OUTSIDE the form's <form>...</form> tags so standalone per-provider
    # forms can sit beside them without nesting (HTML forbids nested forms).
    return (
        f'<select id="{provider.id}_model" name="{provider.id}_model" '
        f'form="save-form">{"".join(rows)}</select>'
    )


def _model_discovery_status_html(
    provider: ProviderCatalogEntry,
    discovered: DiscoverySnapshot | None,
) -> str:
    status = ""
    if discovered is not None and discovered.fetched_at:
        catalog_ids = {model.id for model in provider.models}
        unknown_count = len(
            {model_id for model_id in discovered.models if model_id not in catalog_ids},
        )
        suffix = (
            f"; {unknown_count} untested provider model(s) shown as experimental"
            if unknown_count else ""
        )
        status = f"Last refreshed {html.escape(discovered.fetched_at)}{suffix}."
    if discovered is not None and discovered.last_error:
        failed = (
            f"Last refresh failed {html.escape(discovered.last_error_at)}: "
            f"{html.escape(discovered.last_error)}."
        )
        status = f"{status} {failed}".strip()
    if not status:
        status = (
            "Catalog models are shown. Refresh is manual and never "
            "changes the active model by itself."
        )
    return f'<p class="form-hint">{status}</p>'


def _voice_select_html(provider: ProviderCatalogEntry, current: str) -> str:
    rows = []
    seen = set()
    for voice in provider.voices:
        sel = " selected" if voice.id == current else ""
        rows.append(
            f'<option value="{html.escape(voice.id)}"{sel}>'
            f'{html.escape(voice.label)}</option>'
        )
        seen.add(voice.id)
    if current and current not in seen:
        rows.insert(
            0,
            f'<option value="{html.escape(current)}" selected>'
            f'{html.escape(current)} (custom)</option>',
        )
    return (
        f'<select id="{provider.id}_voice" name="{provider.id}_voice" '
        f'form="save-form">{"".join(rows)}</select>'
    )


def _provider_extras_html(
    provider: ProviderCatalogEntry,
    state: dict[str, str],
) -> str:
    """Render any provider-specific extra controls (today: OpenAI's
    reasoning_effort dropdown). Empty string when the provider has no
    extras. Each extra is a canonical .field (eyebrow label + select +
    hint)."""
    if not provider.extras:
        return ""
    out = []
    for spec in provider.extras:
        current = _value_for(state, spec.env, spec.default)
        rows = []
        seen = set()
        for opt in spec.options:
            sel = " selected" if opt.id == current else ""
            rows.append(
                f'<option value="{html.escape(opt.id)}"{sel}>'
                f'{html.escape(opt.label)}</option>'
            )
            seen.add(opt.id)
        if current and current not in seen:
            rows.insert(
                0,
                f'<option value="{html.escape(current)}" selected>'
                f'{html.escape(current)} (custom)</option>',
            )
        out.append(f"""
        <div class="field">
          <label for="{provider.id}_{spec.name}">{html.escape(spec.label)}</label>
          <select id="{provider.id}_{spec.name}" name="{provider.id}_{spec.name}" form="save-form">
            {''.join(rows)}
          </select>
          <p class="form-hint">{html.escape(spec.hint)}</p>
        </div>""")
    return "\n".join(out)


# Human labels for the Pricing buckets. Covers all six fields; each
# provider exposes the subset it actually uses via
# ``ProviderCatalogEntry.pricing_buckets`` (the single per-provider source).
_BUCKET_LABELS = {
    "audio_input_per_million_usd": "Audio in ($/1M tokens)",
    "audio_output_per_million_usd": "Audio out ($/1M tokens)",
    "text_input_per_million_usd": "Text in ($/1M tokens)",
    "text_output_per_million_usd": "Text out ($/1M tokens)",
    "cached_input_per_million_usd": "Cached in ($/1M tokens)",
    "flat_per_hour_usd": "Flat rate ($/hour)",
}


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _provider_model_ids(
    provider: ProviderCatalogEntry,
    discovered: DiscoverySnapshot | None,
) -> list[str]:
    """Models to offer pricing rows for: catalog ∪ discovered, in that
    order. Mirrors the model dropdown's enumeration."""
    ids = [m.id for m in provider.models]
    seen = set(ids)
    if discovered is not None:
        for model_id in discovered.models:
            if model_id not in seen:
                ids.append(model_id)
                seen.add(model_id)
    return ids


def _pricing_section_html(
    provider: ProviderCatalogEntry,
    discovered: DiscoverySnapshot | None,
    overrides: dict[str, dict],
    default_as_of: str,
    csrf_token: str,
) -> str:
    """Collapsible per-model rate editor for one provider. Standalone form
    POSTing to /pricing (writes /var/lib/jasper/pricing.json) — independent
    of the key/model save-form."""
    buckets = provider.pricing_buckets
    if not buckets:
        return ""
    blocks = []
    for model_id in _provider_model_ids(provider, discovered):
        default = pricing_for_model(model_id)
        effective = pricing_for_model(model_id, overrides=overrides)
        unpriced = default.label.startswith("unpriced:")
        rows = []
        for field in buckets:
            d = getattr(default, field)
            e = getattr(effective, field)
            is_custom = abs(e - d) > 1e-9
            value_attr = f"{e:g}" if is_custom else ""
            placeholder = "set a rate" if unpriced else f"default {d:g}"
            chip = (
                ' <span class="badge badge--ok">custom</span>'
                if is_custom else ""
            )
            name = f"price__{html.escape(model_id)}__{field}"
            rows.append(f"""
            <div class="field">
              <label>{html.escape(_BUCKET_LABELS[field])}{chip}</label>
              <input type="number" min="0" step="0.01" inputmode="decimal"
                     name="{name}" value="{value_attr}"
                     placeholder="{html.escape(placeholder)}">
            </div>""")
        needs = (
            ' <span class="badge badge--warn">needs pricing</span>'
            if unpriced else ""
        )
        blocks.append(f"""
          <div class="price-model">
            <p class="form-hint"><code>{html.escape(model_id)}</code>{needs}</p>
            {''.join(rows)}
          </div>""")
    as_of_txt = (
        f"Bundled rates as of {html.escape(default_as_of)}. " if default_as_of else ""
    )
    return f"""
    <details class="disclosure pricing-disclosure">
      <summary>{html.escape(provider.label)} Pricing rates</summary>
      <div class="disclosure__body">
        <p class="form-hint">{as_of_txt}Used by the /voice spend cap status
        and circuit breaker. Blank = use the bundled default; clear a box to reset.
        Edits apply to future sessions after the daemon restarts.</p>
        <form method="post" action="pricing">
          {csrf_field_html(csrf_token)}
          <input type="hidden" name="provider" value="{provider.id}">
          {''.join(blocks)}
          <div class="form-actions">
            <button class="btn btn--default" type="submit">Save {html.escape(provider.label)} rates</button>
          </div>
        </form>
      </div>
    </details>"""


def _pricing_research_prompt(
    discovery: dict[str, DiscoverySnapshot] | None,
) -> str:
    """Build a copy-paste prompt enumerating the EXACT current models
    (catalog ∪ discovered) and the JSON schema we want back. Generated
    dynamically so it always reflects the models this speaker actually
    offers, including any newly discovered ones."""
    discovery = discovery or {}
    today = _today_iso()
    lines = []
    for provider in PROVIDERS:
        buckets = provider.pricing_buckets
        if not buckets:
            continue
        url = provider.pricing_url or "(official pricing page)"
        lines.append(f"- {provider.label} ({provider.vendor}) — pricing: {url}")
        fields = ", ".join(buckets)
        for model_id in _provider_model_ids(provider, discovery.get(provider.id)):
            lines.append(f"    - {model_id}: {fields}")
    model_block = "\n".join(lines)
    return (
        "You are helping keep a smart speaker's voice-model cost estimates "
        f"accurate. Today is {today}. For each model below, look up its "
        "CURRENT official price from the linked pricing page.\n\n"
        "Models and the rate fields I need (token rates are USD per "
        "1,000,000 tokens; flat_per_hour_usd is USD per hour of billable "
        "realtime activity):\n\n"
        f"{model_block}\n\n"
        "Reply with ONLY a JSON object in EXACTLY this shape — same model "
        "IDs and field names, numbers only (no \"$\" or units), and omit "
        "any field or model you can't find a confident official price for:\n\n"
        "{\n"
        f'  "as_of": "{today}",\n'
        '  "source": "<where you found the prices>",\n'
        '  "models": {\n'
        '    "<model-id>": { "audio_input_per_million_usd": 0.0 }\n'
        "  }\n"
        "}\n\n"
        "Double-check against the official pricing page; do not guess."
    )


def _pricing_refresh_html(
    discovery: dict[str, DiscoverySnapshot] | None,
    csrf_token: str,
) -> str:
    """Phase-3 section: a copyable research prompt (auto-filled with the
    speaker's exact current models) + a paste-back box that imports the
    chatbot's JSON. Standalone form POSTing to /pricing-import.

    The "Copy prompt" button is wired by the page's ES module (it carries
    no inline JS); it targets the textarea by id."""
    prompt = html.escape(_pricing_research_prompt(discovery))
    return f"""
    <section class="section">
      <h2 class="section__title">Refresh pricing rates</h2>
      <p class="form-hint">Copy a model-specific pricing prompt, then paste back validated JSON.</p>
      <details class="disclosure pricing-disclosure">
        <summary>1. Copy this research prompt</summary>
        <div class="disclosure__body">
          <textarea id="pricing-prompt" class="prompt-box" readonly rows="14">{prompt}</textarea>
          <div class="form-actions">
            <button type="button" class="btn btn--default"
                    id="copy-prompt" data-copy-target="pricing-prompt">Copy prompt</button>
          </div>
        </div>
      </details>
      <details class="disclosure pricing-disclosure">
        <summary>2. Paste the JSON it gives you back</summary>
        <div class="disclosure__body">
          <form method="post" action="pricing-import">
            {csrf_field_html(csrf_token)}
            <div class="field">
              <textarea name="payload" class="prompt-box" rows="12"
                placeholder="{{&quot;models&quot;: {{&quot;gpt-realtime-2&quot;: {{&quot;audio_input_per_million_usd&quot;: 32}}}}}}"></textarea>
            </div>
            <div class="form-actions">
              <button class="btn btn--default" type="submit">Validate &amp; import rates</button>
            </div>
          </form>
          <p class="form-hint">Replaces the per-model overrides with the validated
          values, then restarts the voice daemon.</p>
        </div>
      </details>
    </section>"""


def _provider_status_badge_html(*, configured: bool, is_active: bool) -> str:
    if is_active:
        return '<span class="badge badge--ok">active</span>'
    if configured:
        return '<span class="badge badge--idle">configured</span>'
    return '<span class="badge badge--warn">not configured</span>'


def _provider_clear_form_html(
    provider: ProviderCatalogEntry,
    csrf_token: str,
) -> str:
    return f"""
        <form method="post" action="clear-credentials"
              data-confirm="Clear the saved {html.escape(provider.label, quote=True)} key and model/voice override? The daemon will fall back to /etc/jasper/jasper.env defaults."
              data-confirm-danger="1">
          {csrf_field_html(csrf_token)}
          <input type="hidden" name="provider" value="{provider.id}">
          <div class="form-actions">
            <button class="btn btn--danger" type="submit">Clear key</button>
          </div>
        </form>"""


def _provider_key_card_html(
    provider: ProviderCatalogEntry,
    state: dict[str, str],
    csrf_token: str,
    *,
    is_active: bool,
) -> str:
    """API-key card for one provider. Model/voice and pricing live in
    their own sections so this card has one job: add or clear a key."""
    configured = _provider_is_configured(state, provider)
    key_value = _value_for(state, provider.key_env)
    masked = mask_secret(key_value) if key_value else ""
    status_badge = _provider_status_badge_html(
        configured=configured,
        is_active=is_active,
    )
    key_source = ""
    if configured and not state.get(provider.key_env):
        # Key came from /etc/jasper/jasper.env (set by the operator,
        # not the wizard). Saving here writes a wizard-owned override.
        key_source = (
            '<p class="form-hint">Currently sourced from '
            '<code>/etc/jasper/jasper.env</code>. Saving here writes a '
            'wizard-owned override.</p>'
        )
    placeholder = (
        "paste new key — leave blank to keep" if configured
        else f"paste your key ({provider.key_prefix_hint})"
    )
    clear_form = _provider_clear_form_html(provider, csrf_token) if configured else ""
    saved_hint = (
        f'<p class="form-hint">Saved: <code>{html.escape(masked)}</code></p>'
        if masked else ""
    )
    return f"""
    <div class="info-card provider-card">
      <div class="provider-card__head">
        <div>
          <h3 class="provider-card__title">{html.escape(provider.label)}</h3>
          <p class="eyebrow">{html.escape(provider.vendor)}</p>
        </div>
        {status_badge}
      </div>
      <p class="info-card__hint">
        {html.escape(provider.cost_hint)} ·
        <a href="{html.escape(provider.key_url, quote=True)}" target="_blank" rel="noopener">Get key ↗</a>
      </p>

      <div class="field">
        <label for="{provider.id}_key">API key ({html.escape(provider.key_env)})</label>
        <input id="{provider.id}_key" name="{provider.id}_key" form="save-form"
               type="password" autocomplete="off" autocapitalize="off"
               autocorrect="off" spellcheck="false"
               data-provider-key="{provider.id}"
               placeholder="{html.escape(placeholder, quote=True)}">
        {saved_hint}
        {key_source}
      </div>

      {clear_form}
    </div>"""


def _provider_model_card_html(
    provider: ProviderCatalogEntry,
    state: dict[str, str],
    csrf_token: str,
    discovered: DiscoverySnapshot | None,
    *,
    is_active: bool,
) -> str:
    """Model/voice card for one provider. Inputs are associated with the
    outer save form via ``form=save-form`` because this card also owns the
    standalone refresh-models form."""
    configured = _provider_is_configured(state, provider)
    status_badge = _provider_status_badge_html(
        configured=configured,
        is_active=is_active,
    )
    model_value = _value_for(
        state, provider.model_env, default_model_id(provider.id),
    )
    voice_value = _value_for(
        state, provider.voice_env, default_voice_id(provider.id),
    )
    refresh_disabled = "" if configured else " disabled"
    refresh_hint = (
        "Fetches this provider's available models."
        if configured else
        f"Add {provider.key_env} first."
    )
    extras = _provider_extras_html(provider, state)
    return f"""
    <div class="info-card provider-model-card">
      <div class="provider-card__head">
        <div>
          <h3 class="provider-card__title">{html.escape(provider.label)}</h3>
          <p class="eyebrow">{html.escape(provider.vendor)}</p>
        </div>
        {status_badge}
      </div>

      <div class="provider-settings-grid">
        <div class="field">
          <label for="{provider.id}_model">Model</label>
          {_model_select_html(provider, model_value, discovered)}
          {_model_discovery_status_html(provider, discovered)}
        </div>
        <form method="post" action="refresh-models" class="model-refresh-form">
          {csrf_field_html(csrf_token)}
          <input type="hidden" name="provider" value="{provider.id}">
          <div class="form-actions">
            <button class="btn btn--ghost" type="submit"{refresh_disabled}>Refresh available models</button>
            <span class="form-hint">{html.escape(refresh_hint)}</span>
          </div>
        </form>

        <div class="field">
          <label for="{provider.id}_voice">TTS voice</label>
          {_voice_select_html(provider, voice_value)}
        </div>
      </div>

      {extras}
    </div>"""


def _index_html(
    state: dict[str, str],
    csrf_token: str,
    *,
    status_msg: str = "",
    discovery: dict[str, DiscoverySnapshot] | None = None,
    overrides: dict[str, dict] | None = None,
    default_as_of: str = "",
) -> bytes:
    active_id = _active_provider_id(state)
    discovery = discovery or {}
    overrides = overrides or {}
    key_cards = "".join(
        _provider_key_card_html(
            p,
            state,
            csrf_token,
            is_active=(p.id == active_id),
        )
        for p in PROVIDERS
    )
    model_cards = "".join(
        _provider_model_card_html(
            p,
            state,
            csrf_token,
            discovery.get(p.id),
            is_active=(p.id == active_id),
        )
        for p in PROVIDERS
    )
    pricing_cards = "".join(
        _pricing_section_html(
            p,
            discovery.get(p.id),
            overrides,
            default_as_of,
            csrf_token,
        )
        for p in PROVIDERS
    )
    # Page structure note: HTML forbids nested forms, so the outer
    # "save" form CANNOT enclose key clear / model refresh / pricing forms.
    # Layout:
    #   key cards              ← key inputs use form="save-form"; clear-key
    #                            forms stand alone beside them
    #   <form id="save-form">  ← active radios + csrf
    #   </form>
    #   model cards            ← model/voice/extras use form="save-form";
    #                            refresh forms stand alone beside them
    #   pricing sections       ← standalone pricing forms
    #   <button form="save-form">  ← the Save submit explicitly attaches
    body = f"""
{canonical_header("Voice", back_href="/assistant/", back_label="Assistant")}
{pair_banner_html()}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Manage the real-time voice backend. Save applies provider, model, voice, and key changes together.</p>

  <section class="section">
    <h2 class="section__title">1. Enter API keys</h2>
    <p class="form-hint">Add or replace provider keys. Keys stay on this speaker in <code>/var/lib/jasper-secrets/voice_keys.env</code>.</p>
    <div class="provider-stack">
      {key_cards}
    </div>
  </section>

  <section class="section">
    <h2 class="section__title">2. Select provider</h2>
    <form method="post" action="save" id="save-form">
      {csrf_field_html(csrf_token)}
      {_active_radio_html(state)}
    </form>
  </section>

  <section class="section">
    <h2 class="section__title">3. Select model and voice</h2>
    <p class="form-hint">Tune each configured provider here. The active provider is the one JTS uses.</p>
    <div class="provider-stack">
      {model_cards}
    </div>
  </section>

  <div class="form-actions voice-savebar">
    <button type="submit" form="save-form" class="btn btn--primary">Save and restart voice</button>
    <button type="submit" form="save-form" formaction="save-test" class="btn btn--default">Save and Test</button>
  </div>

  {_spend_cap_section_html(state, csrf_token)}

  <section class="section">
    <h2 class="section__title">Advanced pricing</h2>
    <p class="form-hint">Used only for spend estimates and the daily cap.</p>
    {pricing_cards}
  </section>

  {_pricing_refresh_html(discovery, csrf_token)}
</main>
<script type="module" src="/assets/voice/js/main.js"></script>
"""
    return canonical_page(
        "Voice",
        body,
        csrf_token=csrf_token,
        page_css_href=VOICE_PAGE_CSS_HREF,
    )

