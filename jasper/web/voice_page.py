# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Server-rendered provider → key → model voice setup."""
from __future__ import annotations

import html

from jasper.voice.catalog import PROVIDERS
from jasper.voice.model_discovery import DiscoverySnapshot

from ._common import csrf_field_html, pair_banner_html
from .chrome import canonical_banner, canonical_header, canonical_page
from .voice_cost_page import (
    _pricing_refresh_html, _pricing_section_html, _spend_cap_section_html,
)
from .voice_settings import (
    Choice, ProviderSettings, active_provider_id, provider_settings, selected_provider,
)

VOICE_PAGE_CSS_HREF = "/assets/voice/voice.css"


def _choice_html(choice: Choice) -> str:
    options = "".join(
        f'<option value="{html.escape(value)}"'
        f'{" selected" if value == choice.value else ""}>{html.escape(label)}</option>'
        for value, label in choice.options.items()
    )
    return f"""
    <div class="field">
      <label for="{choice.name}">{html.escape(choice.label)}</label>
      <select id="{choice.name}" name="{choice.name}" form="save-form">{options}</select>
      {f'<p class="form-hint">{html.escape(choice.hint)}</p>' if choice.hint else ''}
    </div>"""


def _key_html(settings: ProviderSettings, csrf_token: str) -> str:
    provider = settings.provider
    configured = bool(settings.masked_key)
    placeholder = "Key saved — type a new key to replace" if configured else provider.key_prefix_hint
    key_field = f"""
      <div class="field">
        <label for="{provider.id}_key">{html.escape(provider.vendor)} API key</label>
        <input id="{provider.id}_key" name="{provider.id}_key" form="save-form"
               type="password" autocomplete="off" autocapitalize="off"
               autocorrect="off" spellcheck="false"{'' if configured else ' required'}
               placeholder="{html.escape(placeholder)}">
        {f'<p class="form-hint">Saved: <code>{html.escape(settings.masked_key)}</code>. Leave blank to keep.</p>' if configured else ''}
        <p class="form-hint"><a href="{html.escape(provider.key_url)}"
           target="_blank" rel="noopener">Get an API key ↗</a></p>
      </div>"""
    if not configured:
        return key_field
    return f"""
      {key_field}
      <details class="disclosure">
        <summary>Remove saved key</summary>
        <div class="disclosure__body">
          <form method="post" action="clear-credentials"
                data-confirm="Clear this saved key and model/voice settings? Providers that share this key will also need a key."
                data-confirm-danger="1">
            {csrf_field_html(csrf_token)}
            <input type="hidden" name="provider" value="{provider.id}">
            <button class="btn btn--danger" type="submit">Clear key</button>
          </form>
        </div>
      </details>"""


def _provider_html(
    settings: ProviderSettings, csrf_token: str, discovered: DiscoverySnapshot | None,
) -> str:
    provider = settings.provider
    discovery_status = "Catalog models are shown. Refresh is manual."
    if discovered and discovered.fetched_at:
        discovery_status = f"Last refreshed {discovered.fetched_at}."
    if discovered and discovered.last_error:
        discovery_status += f" Last refresh failed: {discovered.last_error}."
    return f"""
    <form method="post" action="save" id="save-form">
      {csrf_field_html(csrf_token)}
      <input type="hidden" name="active" value="{provider.id}">
    </form>
    <section class="section">
      <h2 class="section__title">2. Enter API key</h2>
      {_key_html(settings, csrf_token)}
    </section>
    <section class="section">
      <h2 class="section__title">3. Select model</h2>
      {_choice_html(settings.model)}
      <details class="disclosure">
        <summary>Available models</summary>
        <div class="disclosure__body">
          <p class="form-hint">{html.escape(discovery_status)}</p>
          <form method="post" action="refresh-models">
            {csrf_field_html(csrf_token)}
            <input type="hidden" name="provider" value="{provider.id}">
            <button class="btn btn--default" type="submit"{'' if settings.masked_key else ' disabled'}>Refresh available models</button>
          </form>
          {'' if settings.masked_key else '<p class="form-hint">Save your key before refreshing models.</p>'}
        </div>
      </details>
    </section>
    <details class="disclosure">
      <summary>Voice and options</summary>
      <div class="disclosure__body">{''.join(_choice_html(c) for c in settings.options)}</div>
    </details>
    <div class="form-actions voice-savebar">
      <button type="submit" form="save-form" class="btn btn--primary">Save and restart voice</button>
      <button type="submit" form="save-form" formaction="save-test" class="btn btn--default">Save and Test</button>
    </div>"""


def _index_html(
    state: dict[str, str], csrf_token: str, *, status_msg: str = "",
    discovery: dict[str, DiscoverySnapshot] | None = None,
    overrides: dict[str, dict] | None = None, default_as_of: str = "",
    selected: str | None = None,
) -> bytes:
    discovery = discovery or {}
    provider = selected_provider(state, selected)
    selected_id = provider.id if provider else ""
    active_id = active_provider_id(state)
    options = '<option value="">Choose a provider</option>' + "".join(
        f'<option value="{p.id}"{" selected" if p.id == selected_id else ""}>'
        f'{html.escape(p.label)}{" — active" if p.id == active_id else ""}</option>'
        for p in PROVIDERS
    )
    setup = pricing = ""
    if provider:
        setup = _provider_html(
            provider_settings(provider, state, discovery.get(provider.id)),
            csrf_token, discovery.get(provider.id),
        )
        pricing = _pricing_section_html(
            provider, discovery.get(provider.id), overrides or {}, default_as_of, csrf_token,
        )
    body = f"""
{canonical_header("Voice", back_href="/assistant/", back_label="Assistant")}
{pair_banner_html()}
<main class="page voice-setup">
  {canonical_banner(status_msg)}
  <p class="form-hint">Choose a provider, add its key, then select a model. Changes apply when you save.</p>
  <section class="section">
    <h2 class="section__title">1. Select provider</h2>
    <form method="get" action="./" id="provider-form">
      <div class="field">
        <label for="provider">Provider</label>
        <select id="provider" name="provider">{options}</select>
      </div>
      <button type="submit" class="btn btn--default" id="choose-provider">Continue</button>
    </form>
    {f'<p class="form-hint">{html.escape(provider.cost_hint)}</p>' if provider else ''}
  </section>
  {setup}
  {_spend_cap_section_html(state, csrf_token)}
  <details class="disclosure">
    <summary>Advanced pricing</summary>
    <div class="disclosure__body">
      {pricing}
      {_pricing_refresh_html(discovery, csrf_token)}
    </div>
  </details>
</main>
<script type="module" src="/assets/voice/js/main.js"></script>
"""
    return canonical_page("Voice", body, csrf_token=csrf_token, page_css_href=VOICE_PAGE_CSS_HREF)
