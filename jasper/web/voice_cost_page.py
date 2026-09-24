# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Spending and pricing controls for voice setup."""
from __future__ import annotations

import html

from jasper.voice.catalog import PROVIDERS, ProviderCatalogEntry
from jasper.voice.model_discovery import DiscoverySnapshot
from jasper.usage import pricing_for_model

from ._common import csrf_field_html, pair_banner_html
from .chrome import canonical_banner, canonical_header, canonical_page
from .voice_settings import provider_model_ids as _provider_model_ids, selected_provider
from .voice_costs import fmt_env_float, fmt_env_money, read_spend_cap_status, today_iso


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.4f}"


def _badge_html(label: str, tone: str) -> str:
    return f'<span class="badge badge--{tone}">{html.escape(label)}</span>'


def _spend_cap_section_html(state: dict[str, str], csrf_token: str, selected: str) -> str:
    status = read_spend_cap_status(state)
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
    cap_value = html.escape(fmt_env_money(status["cap_usd"]), quote=True)
    multiplier_value = html.escape(
        fmt_env_float(status["safety_multiplier"]),
        quote=True,
    )
    return f"""
  <section class="section">
    <h2 class="section__title">Spending</h2>
      <dl class="deflist">
        <dt>Status</dt><dd>{status_badge}</dd>
        <dt>Rolling 24h spend</dt><dd>{_fmt_usd(status["spend_last_24h_usd"]) if status["usage_available"] else "—"}</dd>
        <dt>Cap comparison</dt><dd>{compare}</dd>
        <dt>Remaining</dt><dd>{remaining}</dd>
        <dt>Month to date</dt><dd>{_fmt_usd(status["month_to_date_usd"]) if status["usage_available"] else "—"}</dd>
        <dt>Turns today</dt><dd>{html.escape(str(status["sessions_today"])) if status["usage_available"] else "—"}</dd>
      </dl>
      <p class="form-hint">Spend figures include the tuning assistant's paid calls; Turns today counts voice turns only.</p>
      {note_html}
    <details class="disclosure">
      <summary>Change spending limit</summary>
      <div class="disclosure__body">
      <form method="post" action="spend-cap" class="spend-cap__form">
        {csrf_field_html(csrf_token)}
        <input type="hidden" name="provider" value="{selected}">
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
          <p class="form-hint">Spending is multiplied by this value before checking the cap.</p>
        </div>
        <div class="form-actions">
          <button class="btn btn--default" type="submit">Save spend cap</button>
        </div>
      </form>
      </div>
    </details>
  </section>"""


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
              <label for="{name}">{html.escape(_BUCKET_LABELS[field])}{chip}</label>
              <input type="number" min="0" step="0.01" inputmode="decimal"
                     id="{name}" name="{name}" value="{value_attr}"
                     placeholder="{html.escape(placeholder)}">
            </div>""")
        needs = (
            ' <span class="badge badge--warn">needs pricing</span>'
            if unpriced else ""
        )
        blocks.append(f"""
          <fieldset class="price-model">
            <legend>{html.escape(model_id)}{needs}</legend>
            {''.join(rows)}
          </fieldset>""")
    as_of_txt = (
        f"Bundled rates as of {html.escape(default_as_of)}. " if default_as_of else ""
    )
    return f"""
    <details class="disclosure">
      <summary>Edit {html.escape(provider.label)} rates</summary>
      <div class="disclosure__body">
        <p class="form-hint">{as_of_txt}Leave a field blank to use the default rate.
        Saving restarts voice and applies the rates to future sessions.</p>
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
    discovery = discovery or {}
    today = today_iso()
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
    selected: str,
) -> str:
    prompt = html.escape(_pricing_research_prompt(discovery))
    return f"""
    <details class="disclosure">
      <summary>Update rates from research</summary>
      <div class="disclosure__body">
        <p class="form-hint">Use this prompt with a research assistant to check official prices for all providers.</p>
        <div class="field">
          <label for="pricing-prompt">1. Copy the research prompt</label>
          <textarea id="pricing-prompt" class="prompt-box" readonly rows="6">{prompt}</textarea>
          <div class="form-actions">
            <button type="button" class="btn btn--default"
                    id="copy-prompt" data-copy-target="pricing-prompt">Copy prompt</button>
          </div>
        </div>
          <form method="post" action="pricing-import">
            {csrf_field_html(csrf_token)}
            <input type="hidden" name="provider" value="{selected}">
            <div class="field">
              <label for="pricing-payload">2. Paste the JSON response</label>
              <textarea id="pricing-payload" name="payload" class="prompt-box" rows="6"
                placeholder="{{&quot;models&quot;: {{&quot;gpt-realtime-2&quot;: {{&quot;audio_input_per_million_usd&quot;: 32}}}}}}"></textarea>
            </div>
            <div class="form-actions">
              <button class="btn btn--default" type="submit">Validate &amp; import rates</button>
            </div>
          </form>
          <p class="form-hint">Updates the models in the response, keeps other rates,
          and restarts voice.</p>
      </div>
    </details>"""


def costs_html(
    state: dict[str, str], csrf_token: str, *, status_msg: str = "",
    discovery: dict[str, DiscoverySnapshot] | None = None,
    overrides: dict[str, dict] | None = None, default_as_of: str = "",
    selected: str | None = None,
) -> bytes:
    discovery = discovery or {}
    provider = selected_provider(state, selected)
    selected_id = provider.id if provider else ""
    pricing = _pricing_section_html(
        provider, discovery.get(provider.id), overrides or {}, default_as_of, csrf_token,
    ) if provider else ""
    body = f"""
{canonical_header("Usage and costs", back_href=f"./?provider={selected_id}", back_label="Voice")}
{pair_banner_html()}
<main class="page voice-setup">
  {canonical_banner(status_msg)}
  {_spend_cap_section_html(state, csrf_token, selected_id)}
  <section class="section">
    <h2 class="section__title">Model rates</h2>
    <p class="form-hint">Rates estimate your spending and control when the spending limit stops voice.</p>
    {pricing}
    {_pricing_refresh_html(discovery, csrf_token, selected_id)}
  </section>
</main>
<script type="module" src="/assets/voice/js/main.js"></script>
"""
    return canonical_page("Usage and costs", body, csrf_token=csrf_token, page_css_href="/assets/voice/voice.css")
