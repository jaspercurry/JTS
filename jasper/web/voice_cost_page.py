# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Spending and pricing controls for voice setup."""
from __future__ import annotations

import html

from jasper.voice.catalog import PROVIDERS, ProviderCatalogEntry
from jasper.voice.model_discovery import DiscoverySnapshot
from jasper.usage import pricing_for_model

from ._common import csrf_field_html
from .voice_settings import provider_model_ids as _provider_model_ids
from .voice_costs import _fmt_env_float, _fmt_env_money, _read_spend_cap_status, _today_iso


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.4f}"


def _badge_html(label: str, tone: str) -> str:
    return f'<span class="badge badge--{tone}">{html.escape(label)}</span>'


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
  <details class="disclosure">
    <summary>Spending and limits</summary>
    <div class="disclosure__body">
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
    </div>
  </details>"""


_BUCKET_LABELS = {
    "audio_input_per_million_usd": "Audio in ($/1M tokens)",
    "audio_output_per_million_usd": "Audio out ($/1M tokens)",
    "text_input_per_million_usd": "Text in ($/1M tokens)",
    "text_output_per_million_usd": "Text out ($/1M tokens)",
    "cached_input_per_million_usd": "Cached in ($/1M tokens)",
    "flat_per_hour_usd": "Flat rate ($/hour)",
}


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
