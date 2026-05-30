"""Voice provider configuration wizard at /voice/.

UX: a single page with one card per supported provider (Gemini /
OpenAI / Grok). Each card holds the API key (masked, with a
"currently: prefix…suffix" hint when set), a model dropdown, a voice
dropdown, and any provider-specific knob (reasoning effort for
gpt-realtime-2). At the top: a radio group picks which provider the
voice loop should USE — disabled for any provider that doesn't have a
key yet, so the user can't accidentally activate a broken backend.

Persistence: writes to /var/lib/jasper/voice_provider.env at mode 0600.
The systemd unit for jasper-voice sources this file AFTER
/etc/jasper/jasper.env, so wizard-written values win over operator-
managed defaults — same pattern as /spotify and its
spotify_credentials.env.

Restart: every successful save kicks `systemctl restart jasper-voice`.
The voice loop comes back ~3-5 s later on the new provider; the cue
manager's `cant_connect` plays if the new key is rejected upstream.

URL surface (after nginx strips the /voice/ prefix):
  GET  /                          page render
  POST /save                      save credentials + active provider, restart
  POST /clear-credentials         clear one provider's key/model/voice
  POST /refresh-models            refresh one provider's cached model list
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jasper.voice.catalog import (
    PROVIDERS,
    VALID_PROVIDER_IDS,
    ProviderCatalogEntry,
    default_model_id,
    default_voice_id,
    provider_by_id,
)
from jasper.voice.provider_state import resolve_active_provider
from jasper.voice.model_discovery import (
    DEFAULT_CACHE_PATH,
    DiscoverySnapshot,
    ModelDiscoveryError,
    load_cache,
    refresh_provider_cache,
)
from jasper.usage import (
    DEFAULT_PRICING_FILE,
    load_default_pricing,
    load_pricing_overrides,
    pricing_for_model,
    sanitize_pricing_models,
)

from ._common import (
    PAGE_STYLE,
    begin_request,
    csrf_field_html,
    delete_env_file,
    mask_secret,
    read_env_file,
    read_form,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_see_other,
    verify_csrf,
    wrap_page,
    write_env_file,
    write_json_file,
)

logger = logging.getLogger(__name__)


# Persisted at /var/lib/jasper/voice_provider.env. Operator-managed
# defaults still live in /etc/jasper/jasper.env; the systemd unit
# layers this file ON TOP so wizard-written values win.
PROVIDER_FILE = "/var/lib/jasper/voice_provider.env"
DISCOVERY_CACHE_FILE = DEFAULT_CACHE_PATH


# Provider metadata lives in jasper.voice.catalog so the wizard's provider,
# model, voice, and extra-control metadata has one code-owned catalog to
# audit. The catalog is curated, not an allow-list: unknown configured
# models are preserved by the select rendering below instead of silently
# replaced.


# Loose validation — block obvious paste mistakes (whitespace, quotes,
# crlf) without rejecting any real-world key. The provider's API will
# reject anything actually malformed when the daemon connects, and
# the cue manager will play `cant_connect`.
_KEY_VALID_RE = re.compile(r"^[A-Za-z0-9_\-.~]+$")


# ----------------------------------------------------------------------
# State helpers — pure functions, no IO except inside read_*/write_*.
# ----------------------------------------------------------------------


def _load_state(path: str = PROVIDER_FILE) -> dict[str, str]:
    """Read the wizard-managed env file into a {key: value} dict.

    Empty values, missing file, blank file all resolve to {}. The
    daemon's view of the env is this dict UNIONED with
    /etc/jasper/jasper.env — but the wizard only reads/writes this
    file."""
    return read_env_file(path)


def _value_for(state: dict[str, str], env_var: str, default: str = "") -> str:
    """Pull a single env var out of state, falling back to the
    process's own environment (in case an operator set it in
    /etc/jasper/jasper.env directly), then to `default`. The wizard
    ALWAYS shows the value the daemon will actually use, regardless
    of which file it came from."""
    val = state.get(env_var, "").strip()
    if val:
        return val
    return os.environ.get(env_var, "") or default


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
# HTML rendering.
# ----------------------------------------------------------------------


_VOICE_PAGE_STYLE = PAGE_STYLE + """
  /* Active-provider radio group at the top of the page. */
  .active-group { background: #f4f4f4; border-radius: 6px;
                   padding: 0.8em 1em; margin: 0.6em 0 1.4em; }
  .active-group h2 { margin-top: 0; }
  .active-group label.radio {
    display: flex; align-items: center; gap: 0.6em;
    padding: 0.5em 0.6em; border-radius: 5px;
    margin-bottom: 0.25em; cursor: pointer;
    background: #fff; border: 1px solid #e0e0e0;
  }
  .active-group label.radio:hover { background: #fafffb; }
  .active-group label.radio.disabled {
    color: #888; cursor: not-allowed; background: #f8f8f8;
    border-style: dashed;
  }
  .active-group label.radio input[type=radio] {
    width: auto; flex: none; margin: 0;
  }
  .active-group label.radio .name { font-weight: 600; flex: 1; }
  .active-group label.radio .pricing {
    color: #888; font-size: 0.85em; font-variant-numeric: tabular-nums;
  }

  /* Per-provider configuration cards. Reuse the .account/.account-body
     pattern from _common so they look like the Spotify accounts. */
  .provider-body label { font-size: 0.95em; }
  .provider-body .meta {
    font-size: 0.88em; color: #555; margin: 0.4em 0;
  }
  .provider-body .meta code {
    background: #fafafa; border: 1px solid #e0e0e0;
    border-radius: 3px; padding: 0.05em 0.4em;
  }
  .provider-body .actions {
    display: flex; gap: 0.6em; margin-top: 1em; align-items: center;
    flex-wrap: wrap;
  }
  .provider-body .actions form { margin: 0; }
  .provider-body .key-source {
    font-size: 0.85em; color: #888; margin-top: 0.2em;
  }
  .provider-body .model-discovery-status {
    color: #666; font-size: 0.85em; margin: 0.35em 0 0.2em;
  }
  .provider-body .model-discovery-actions {
    margin-top: 0.45em; gap: 0.7em;
  }
  .provider-body .model-discovery-actions .hint {
    flex: 1 1 14em; margin: 0;
  }
"""


def _wrap_voice_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    """Voice wizard page wrapper. Same wrap_page structure as the
    Spotify wizard, but with the additional voice-specific style
    block injected so the per-provider cards and the active-provider
    radio group render correctly."""
    page = wrap_page(title, body, status_msg=status_msg).decode()
    return page.replace(
        f"<style>{PAGE_STYLE}</style>",
        f"<style>{_VOICE_PAGE_STYLE}</style>",
    ).encode()


def _active_radio_html(state: dict[str, str]) -> str:
    """The 'use this provider' radio block at the top of the page.
    Disabled radios are marked aria-disabled so screen readers report
    the correct state — the disabled attribute alone suppresses the
    underlying input but the wrapping <label> handles the click."""
    active = _active_provider_id(state)
    rows = []
    for p in PROVIDERS:
        configured = _provider_is_configured(state, p)
        is_active = active == p.id
        radio_attrs = ["type=\"radio\"", "name=\"active\"", f"value=\"{p.id}\""]
        if is_active:
            radio_attrs.append("checked")
        if not configured:
            radio_attrs.append("disabled")
        radio_input = f"<input {' '.join(radio_attrs)}>"
        cls = "radio disabled" if not configured else "radio"
        status = (
            "configured" if configured
            else f"no {p.key_env} yet — paste below first"
        )
        rows.append(f"""
          <label class="{cls}">
            {radio_input}
            <span class="name">{html.escape(p.label)}</span>
            <span class="pricing">{html.escape(p.cost_hint)}</span>
            <span class="meta" style="margin: 0">{html.escape(status)}</span>
          </label>""")
    return f"""
<div class="active-group">
  <h2>Use this provider for voice</h2>
  <p class="hint">Pick which real-time backend the wake-word loop talks to. Only providers with a saved API key can be selected. Press <strong>Save and restart</strong> at the bottom of the page to apply.</p>
  {''.join(rows)}
</div>"""


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
    # OUTSIDE the form's <form>...</form> tags so a per-card "Clear
    # key" form can sit beside them without nesting (HTML forbids
    # nested forms).
    return f'<select name="{provider.id}_model" form="save-form">{"".join(rows)}</select>'


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
    return f'<p class="model-discovery-status">{status}</p>'


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
    return f'<select name="{provider.id}_voice" form="save-form">{"".join(rows)}</select>'


def _provider_extras_html(
    provider: ProviderCatalogEntry,
    state: dict[str, str],
) -> str:
    """Render any provider-specific extra controls (today: OpenAI's
    reasoning_effort dropdown). Empty string when the provider has no
    extras."""
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
          <label for="{provider.id}_{spec.name}">{html.escape(spec.label)}</label>
          <select id="{provider.id}_{spec.name}" name="{provider.id}_{spec.name}" form="save-form">
            {''.join(rows)}
          </select>
          <small>{html.escape(spec.hint)}</small>""")
    return "\n".join(out)


# Which Pricing buckets each provider's cost model actually uses, in
# display order. Gemini Live can't split text/cached (audio only); Grok is
# flat-rate. This drives which number inputs the editor shows per model.
_BUCKET_LABELS = {
    "audio_input_per_million_usd": "Audio in ($/1M tokens)",
    "audio_output_per_million_usd": "Audio out ($/1M tokens)",
    "text_input_per_million_usd": "Text in ($/1M tokens)",
    "text_output_per_million_usd": "Text out ($/1M tokens)",
    "cached_input_per_million_usd": "Cached in ($/1M tokens)",
    "flat_per_hour_usd": "Flat rate ($/hour)",
}
_PROVIDER_BUCKETS: dict[str, tuple[str, ...]] = {
    "gemini": (
        "audio_input_per_million_usd",
        "audio_output_per_million_usd",
    ),
    "openai": (
        "audio_input_per_million_usd",
        "audio_output_per_million_usd",
        "text_input_per_million_usd",
        "text_output_per_million_usd",
        "cached_input_per_million_usd",
    ),
    "grok": ("flat_per_hour_usd",),
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
    buckets = _PROVIDER_BUCKETS.get(provider.id, ())
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
            chip = ' <span class="badge">custom</span>' if is_custom else ""
            name = f"price__{html.escape(model_id)}__{field}"
            rows.append(f"""
            <label>{html.escape(_BUCKET_LABELS[field])}{chip}</label>
            <input type="number" min="0" step="0.01" inputmode="decimal"
                   name="{name}" value="{value_attr}"
                   placeholder="{html.escape(placeholder)}">""")
        needs = (
            ' <span class="badge muted">needs pricing</span>' if unpriced else ""
        )
        blocks.append(f"""
          <div class="price-model" style="margin:0.75em 0; padding-top:0.5em; border-top:1px solid rgba(0,0,0,0.08)">
            <p class="meta" style="margin:0 0 0.3em"><code>{html.escape(model_id)}</code>{needs}</p>
            {''.join(rows)}
          </div>""")
    as_of_txt = (
        f"Bundled rates as of {html.escape(default_as_of)}. " if default_as_of else ""
    )
    return f"""
    <details class="disclosure">
      <summary>Pricing rates</summary>
      <div class="disclosure-body">
        <p class="hint">{as_of_txt}Used to estimate spend on the /system
        dashboard. Blank = use the bundled default; clear a box to reset.
        Edits apply to future sessions after the daemon restarts.</p>
        <form method="post" action="pricing">
          {csrf_field_html(csrf_token)}
          <input type="hidden" name="provider" value="{provider.id}">
          {''.join(blocks)}
          <div class="actions" style="margin-top:0.75em">
            <button class="secondary" type="submit">Save {html.escape(provider.label)} rates</button>
          </div>
        </form>
      </div>
    </details>"""


# Official pricing pages the research prompt points a chatbot at. The
# provider APIs don't expose voice-model prices (see HANDOFF-pricing-editor),
# so a human/chatbot reads these.
_PRICING_PAGE_URLS = {
    "gemini": "https://ai.google.dev/gemini-api/docs/pricing",
    "openai": "https://platform.openai.com/docs/pricing",
    "grok": "https://docs.x.ai/developers/pricing",
}


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
        buckets = _PROVIDER_BUCKETS.get(provider.id, ())
        if not buckets:
            continue
        url = _PRICING_PAGE_URLS.get(provider.id, "(official pricing page)")
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
        "1,000,000 tokens; flat_per_hour_usd is USD per hour of open "
        "connection):\n\n"
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
    chatbot's JSON. Standalone form POSTing to /pricing-import."""
    prompt = html.escape(_pricing_research_prompt(discovery))
    return f"""
<h2 style="margin-top:2em">Refresh all rates from a chatbot</h2>
<p class="hint">No provider API returns voice-model prices, so this speaker
can't fetch them automatically. Copy the prompt below into any AI chatbot
— it lists the exact models this speaker uses and asks for current official
prices — then paste back the JSON it replies with.</p>
<details class="disclosure">
  <summary>1. Copy this research prompt</summary>
  <div class="disclosure-body">
    <textarea id="pricing-prompt" readonly rows="14"
      style="width:100%; font-family:monospace; font-size:0.8em">{prompt}</textarea>
    <div class="actions" style="margin-top:0.5em">
      <button type="button" class="secondary"
        onclick="const t=document.getElementById('pricing-prompt');t.focus();t.select();navigator.clipboard&amp;&amp;navigator.clipboard.writeText(t.value)">Copy prompt</button>
    </div>
  </div>
</details>
<details class="disclosure">
  <summary>2. Paste the JSON it gives you back</summary>
  <div class="disclosure-body">
    <form method="post" action="pricing-import">
      {csrf_field_html(csrf_token)}
      <textarea name="payload" rows="12"
        style="width:100%; font-family:monospace; font-size:0.8em"
        placeholder="{{&quot;models&quot;: {{&quot;gpt-realtime-2&quot;: {{&quot;audio_input_per_million_usd&quot;: 32}}}}}}"></textarea>
      <div class="actions" style="margin-top:0.5em">
        <button class="secondary" type="submit">Validate &amp; import rates</button>
      </div>
    </form>
    <p class="hint">Replaces the per-model overrides with the validated
    values, then restarts the voice daemon.</p>
  </div>
</details>"""


def _provider_card_html(
    provider: ProviderCatalogEntry,
    state: dict[str, str],
    csrf_token: str,
    discovered: DiscoverySnapshot | None,
    overrides: dict[str, dict],
    default_as_of: str,
    *,
    is_active: bool,
) -> str:
    """One <details> card per provider. Open by default for the active
    provider so the user lands on what's currently in flight."""
    configured = _provider_is_configured(state, provider)
    key_value = _value_for(state, provider.key_env)
    masked = mask_secret(key_value) if key_value else ""
    model_value = _value_for(
        state, provider.model_env, default_model_id(provider.id),
    )
    voice_value = _value_for(
        state, provider.voice_env, default_voice_id(provider.id),
    )
    badge_html = (
        '<span class="badge">configured</span>' if configured
        else '<span class="badge muted">not configured</span>'
    )
    active_badge = (
        ' <span class="badge" style="background:#1db954">active</span>'
        if is_active else ""
    )
    open_attr = " open" if (is_active or not configured) else ""
    key_source = ""
    if configured and not state.get(provider.key_env):
        # Key came from /etc/jasper/jasper.env (set by the operator,
        # not the wizard). Saving here writes a wizard-owned override.
        key_source = (
            '<p class="key-source">Currently sourced from '
            '/etc/jasper/jasper.env — saving here will override it '
            'in /var/lib/jasper/voice_provider.env.</p>'
        )
    extras = _provider_extras_html(provider, state)
    placeholder = (
        "paste new key — leave blank to keep" if configured
        else f"paste your key ({provider.key_prefix_hint})"
    )
    clear_form = ""
    if configured:
        clear_form = f'''
    <div class="actions">
      <form method="post" action="clear-credentials"
            onsubmit="return jtsConfirmSubmit(this, 'Clear the saved {html.escape(provider.label)} key and model/voice override? The daemon will fall back to /etc/jasper/jasper.env defaults.', {{danger:true}});">
        {csrf_field_html(csrf_token)}
        <input type="hidden" name="provider" value="{provider.id}">
        <button class="danger" type="submit">Clear key</button>
      </form>
    </div>
    '''
    refresh_disabled = "" if configured else " disabled"
    refresh_hint = (
        "Queries the provider with this speaker's saved key, caches "
        "the result locally, and labels unknown models as experimental."
        if configured else
        f"Paste a {provider.key_env} first, then refresh available models."
    )
    return f"""
<details class="account"{open_attr}>
  <summary>
    <span class="name">{html.escape(provider.label)}</span>
    <span class="meta" style="margin:0; font-size:0.85em">{html.escape(provider.vendor)}</span>
    {badge_html}{active_badge}
  </summary>
  <div class="account-body provider-body">
    <p class="meta">
      Cost: <strong>{html.escape(provider.cost_hint)}</strong>.
      Get a key:
      <a href="{html.escape(provider.key_url)}" target="_blank" rel="noopener">{html.escape(provider.vendor)} console ↗</a>
    </p>

    <label for="{provider.id}_key">{html.escape(provider.key_env)}</label>
    <input id="{provider.id}_key" name="{provider.id}_key" form="save-form"
           type="password" autocomplete="off" autocapitalize="off"
           autocorrect="off" spellcheck="false"
           placeholder="{html.escape(placeholder)}">
    {f'<p class="meta">Currently saved: <code>{html.escape(masked)}</code></p>' if masked else ''}
    {key_source}

    <label for="{provider.id}_model">Model</label>
    {_model_select_html(provider, model_value, discovered)}
    {_model_discovery_status_html(provider, discovered)}
    <div class="actions model-discovery-actions">
      <form method="post" action="refresh-models">
        {csrf_field_html(csrf_token)}
        <input type="hidden" name="provider" value="{provider.id}">
        <button class="secondary" type="submit"{refresh_disabled}>Refresh available models</button>
      </form>
      <span class="hint">{html.escape(refresh_hint)}</span>
    </div>

    <label for="{provider.id}_voice">TTS voice</label>
    {_voice_select_html(provider, voice_value)}

    {extras}

    {_pricing_section_html(provider, discovered, overrides, default_as_of, csrf_token)}

    {clear_form}
  </div>
</details>"""


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
    cards = "".join(
        _provider_card_html(
            p,
            state,
            csrf_token,
            discovery.get(p.id),
            overrides,
            default_as_of,
            is_active=(p.id == active_id),
        )
        for p in PROVIDERS
    )
    # Page structure note: HTML forbids nested forms, so the outer
    # "save" form CANNOT enclose the per-card "Clear key" forms. Layout:
    #   <form id="save-form">  ← active radios + csrf
    #     ...
    #   </form>                ← form closes BEFORE the cards
    #   <h2>Provider keys</h2>
    #   {cards}                ← card inputs use form="save-form"
    #                            attribute to associate with the outer
    #                            form by ID. Clear-key forms inside
    #                            cards stand alone with their own csrf field.
    #   <button form="save-form">  ← submit explicitly attaches
    body = f"""
<p class="sub">Configure the real-time voice backend for this speaker.
Paste an API key into any provider you want to enable, pick which one is
active, and save — the voice daemon picks up the change on its next
restart (about 5 seconds).</p>

<form method="post" action="save" id="save-form">
{csrf_field_html(csrf_token)}
{_active_radio_html(state)}
</form>

<h2>Provider keys</h2>
<p class="hint">Pasted keys are stored on this speaker only, written to
<code>/var/lib/jasper/voice_provider.env</code> at mode 0600. They are
never sent anywhere except the relevant provider's API.</p>
{cards}

<p style="margin-top:2em">
  <button type="submit" form="save-form">Save and restart voice</button>
</p>
{_pricing_refresh_html(discovery, csrf_token)}

<p class="hint" style="margin-top:2em">
  See <a href="https://github.com/jaspercurry/JTS/blob/main/docs/HANDOFF-voice-providers.md" target="_blank" rel="noopener">HANDOFF-voice-providers.md</a> for architecture, per-provider trade-offs, and the steps for adding a fourth backend.
</p>
"""
    return _wrap_voice_page(
        "Voice provider on this speaker", body, status_msg=status_msg,
    )


# ----------------------------------------------------------------------
# Save logic — pure where possible, IO at the edges.
# ----------------------------------------------------------------------


def _validate_key(key: str) -> str | None:
    """Return a complaint string if `key` is structurally bad, else
    None. We refuse anything with whitespace or non-base64-URL-safe
    characters — that catches the most common paste mistake (an
    accidental copied newline or trailing space) without rejecting
    keys we don't have a regex for."""
    if not key:
        return None
    if any(ch.isspace() for ch in key):
        return "Pasted key contains whitespace; copy it again without leading/trailing spaces."
    if not _KEY_VALID_RE.fullmatch(key):
        return "Pasted key contains characters that don't look like an API key — copy it again."
    return None


def _apply_save(form: dict[str, str], current: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """Pure: take the existing wizard state plus the submitted form and
    return the new state along with an optional error string.

    Rules:
      * For each provider, an EMPTY key field means 'leave the saved
        key alone'. A non-empty value replaces.
      * Model and voice always overwrite (the dropdowns always submit
        a value).
      * Reasoning effort (OpenAI) overwrites when present.
      * Active provider must reference a provider that has, OR will
        have after this save, an API key. Otherwise we reject.

    Returning the updated dict keeps the IO concern (atomic file
    write) out of this function so the test can drive the math
    directly."""
    new = dict(current)
    for p in PROVIDERS:
        pid = p.id
        key = (form.get(f"{pid}_key") or "").strip()
        if key:
            err = _validate_key(key)
            if err:
                return current, f"{p.label}: {err}"
            new[p.key_env] = key
        model = (form.get(f"{pid}_model") or "").strip()
        if model:
            new[p.model_env] = model
        voice = (form.get(f"{pid}_voice") or "").strip()
        if voice:
            new[p.voice_env] = voice
        for spec in p.extras:
            val = (form.get(f"{pid}_{spec.name}") or "").strip()
            if val:
                new[spec.env] = val

    active = (form.get("active") or "").strip()
    if active not in VALID_PROVIDER_IDS:
        return current, f"Unknown provider {active!r}."
    active_provider = provider_by_id(active)
    if active_provider is None:
        return current, f"Unknown provider {active!r}."
    has_key = bool(
        new.get(active_provider.key_env)
        or os.environ.get(active_provider.key_env)
    )
    if not has_key:
        return current, (
            f"{active_provider.label} has no API key configured "
            f"yet. Paste a {active_provider.key_env} value before "
            f"selecting it as active."
        )
    new["JASPER_VOICE_PROVIDER"] = active

    # Drop any blank values we accidentally produced (e.g. user picks
    # "(custom)" placeholder — defensive against future UI changes).
    new = {k: v for k, v in new.items() if v}
    return new, None


def _apply_clear(form: dict[str, str], current: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """Clear one provider's stored key + model + voice + extras. The
    active provider is NOT changed by this — if the user clears their
    active provider, the next page render will show "no key" on it
    and warn at save time. Operator can recover by either pasting a
    new key or hand-editing /etc/jasper/jasper.env."""
    pid = (form.get("provider") or "").strip()
    p = provider_by_id(pid)
    if p is None:
        return current, f"Unknown provider {pid!r}."
    new = dict(current)
    for env in (p.key_env, p.model_env, p.voice_env):
        new.pop(env, None)
    for spec in p.extras:
        new.pop(spec.env, None)
    return new, None


def _provider_key_for_discovery(
    provider: ProviderCatalogEntry,
    state: dict[str, str],
) -> str:
    return _value_for(state, provider.key_env).strip()


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
    buckets = _PROVIDER_BUCKETS.get(provider.id, ())
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
) -> tuple[dict[str, dict] | None, str | None]:
    """Parse a chatbot's pasted pricing JSON → ``(models_map, None)`` or
    ``(None, error_message)``. Tolerant of a ```json fence and of a bare
    ``{model_id: {...}}`` map without the ``{"models": ...}`` wrapper.
    Validation reuses ``sanitize_pricing_models`` so pasted JSON is held to
    the same rules as a hand-edited override file."""
    text = (raw_text or "").strip()
    if not text:
        return None, "Paste the JSON your chatbot produced first."
    if text.startswith("```"):
        # Strip a leading ```/```json fence line and a trailing ``` fence.
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        return None, f"That doesn't parse as JSON ({e})."
    if not isinstance(data, dict):
        return None, 'Expected a JSON object with a "models" map.'
    # Accept either {"models": {...}} or a bare {model_id: {...}} map.
    models = sanitize_pricing_models(data.get("models", data))
    if not models:
        return None, (
            "No usable model rates found. Expected "
            '{"models": {"<model-id>": {"audio_input_per_million_usd": '
            "<number>, ...}}}."
        )
    return models, None


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Returns a request handler class closed over the config dict.
    `cfg` carries the persisted-state file path so tests can swap a
    tempdir."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                state = _load_state(cfg["state_path"])
                discovery = load_cache(cfg["discovery_cache_path"])
                overrides = load_pricing_overrides(cfg["pricing_path"])
                _, default_as_of = load_default_pricing()
                ctx = begin_request(self)
                send_html_response(self, _index_html(
                    state,
                    ctx["csrf_token"],
                    status_msg=ctx["flash"],
                    discovery=discovery,
                    overrides=overrides,
                    default_as_of=default_as_of,
                ))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path not in (
                "/save", "/clear-credentials", "/refresh-models", "/pricing",
                "/pricing-import",
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not verify_csrf(self, form):
                reject_csrf(self)
                return
            if path == "/save":
                self._handle_save(form)
                return
            if path == "/clear-credentials":
                self._handle_clear(form)
                return
            if path == "/refresh-models":
                self._handle_refresh_models(form)
                return
            if path == "/pricing":
                self._handle_pricing(form)
                return
            if path == "/pricing-import":
                self._handle_pricing_import(form)
                return

        # --- route bodies ---

        def _handle_save(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_save(form, current)
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            try:
                if new:
                    write_env_file(cfg["state_path"], new)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write voice provider env file")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            restart_voice_daemon()
            active = new.get("JASPER_VOICE_PROVIDER", "")
            label = next(
                (p.label for p in PROVIDERS if p.id == active),
                active,
            )
            send_see_other(
                self, "./",
                flash=f"Saved. Voice daemon restarting on {label}.",
            )

        def _handle_clear(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_clear(form, current)
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            try:
                if new:
                    write_env_file(cfg["state_path"], new)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write voice provider env file")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            restart_voice_daemon()
            pid = (form.get("provider") or "").strip()
            label = next(
                (p.label for p in PROVIDERS if p.id == pid),
                pid,
            )
            send_see_other(self, "./", flash=f"Cleared {label} credentials.")

        def _handle_refresh_models(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            pid = (form.get("provider") or "").strip()
            provider = provider_by_id(pid)
            if provider is None:
                send_see_other(self, "./", flash=f"Unknown provider {pid!r}.")
                return
            api_key = _provider_key_for_discovery(provider, current)
            if not api_key:
                send_see_other(
                    self,
                    "./",
                    flash=(
                        f"{provider.label} has no API key configured yet. "
                        f"Paste a {provider.key_env} value before refreshing "
                        "available models."
                    ),
                )
                return
            try:
                snapshot = refresh_provider_cache(
                    provider.id,
                    api_key,
                    path=cfg["discovery_cache_path"],
                    http=cfg.get("discovery_http_client"),
                )
            except (ModelDiscoveryError, OSError) as e:
                logger.warning(
                    "event=voice_model_discovery provider=%s result=error error=%r",
                    provider.id,
                    str(e),
                )
                send_see_other(
                    self,
                    "./",
                    flash=f"Could not refresh {provider.label} models: {e}",
                )
                return
            logger.info(
                "event=voice_model_discovery provider=%s result=ok count=%d",
                provider.id,
                len(snapshot.models),
            )
            send_see_other(
                self,
                "./",
                flash=(
                    f"Refreshed {provider.label} models. "
                    "Newly discovered models are experimental until tested."
                ),
            )

        def _handle_pricing(self, form: dict[str, str]) -> None:
            pid = (form.get("provider") or "").strip()
            provider = provider_by_id(pid)
            if provider is None:
                send_see_other(self, "./", flash=f"Unknown provider {pid!r}.")
                return
            discovery = load_cache(cfg["discovery_cache_path"])
            model_ids = _provider_model_ids(provider, discovery.get(provider.id))
            existing = load_pricing_overrides(cfg["pricing_path"])
            new_models = _apply_pricing_save(form, provider, model_ids, existing)
            try:
                if new_models:
                    write_json_file(cfg["pricing_path"], {
                        "as_of": _today_iso(),
                        "source": "edited via /voice",
                        "models": new_models,
                    })
                else:
                    # No overrides anywhere now → remove the file so the
                    # daemon falls back entirely to the bundled defaults.
                    try:
                        os.remove(cfg["pricing_path"])
                    except FileNotFoundError:
                        pass
            except OSError as e:
                logger.exception("could not write pricing override")
                send_see_other(
                    self, "./", flash=f"Could not save pricing: {e}",
                )
                return
            restart_voice_daemon()
            send_see_other(
                self, "./",
                flash=(
                    f"Saved {provider.label} pricing. "
                    "Voice daemon restarting."
                ),
            )

        def _handle_pricing_import(self, form: dict[str, str]) -> None:
            models, err = _apply_pricing_paste(form.get("payload") or "")
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            try:
                write_json_file(cfg["pricing_path"], {
                    "as_of": _today_iso(),
                    "source": "imported via /voice",
                    "models": models,
                })
            except OSError as e:
                logger.exception("could not write imported pricing")
                send_see_other(
                    self, "./", flash=f"Could not save pricing: {e}",
                )
                return
            restart_voice_daemon()
            send_see_other(
                self, "./",
                flash=(
                    f"Imported rates for {len(models)} model(s). "
                    "Voice daemon restarting."
                ),
            )

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(
    target,
    *,
    state_path: str = PROVIDER_FILE,
    discovery_cache_path: str = DISCOVERY_CACHE_FILE,
    discovery_http_client: Any | None = None,
    pricing_path: str | None = None,
) -> ThreadingHTTPServer:
    """Build a configured server. `target` is one of:
      - `socket.socket` — pre-bound listener handed off by systemd
      - `(host, port)` tuple — explicit bind
      - `int` — port, binds 127.0.0.1
    Mirrors the other wizard `make_server` signatures so jasper.web.__main__
    can drive all four uniformly. `pricing_path` defaults to the same
    JASPER_PRICING_FILE the daemon reads, so edits land where it looks."""
    from . import _systemd
    cfg = {
        "state_path": state_path,
        "discovery_cache_path": discovery_cache_path,
        "discovery_http_client": discovery_http_client,
        "pricing_path": pricing_path or os.environ.get(
            "JASPER_PRICING_FILE", DEFAULT_PRICING_FILE,
        ),
    }
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-voice-web",
        description="Voice provider configuration UI for the Jasper smart speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_VOICE_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_VOICE_WEB_PORT", "8767")),
    )
    parser.add_argument(
        "--state", default=os.environ.get("JASPER_VOICE_PROVIDER_FILE", PROVIDER_FILE),
    )
    parser.add_argument(
        "--discovery-cache",
        default=os.environ.get(
            "JASPER_VOICE_MODEL_DISCOVERY_FILE",
            DISCOVERY_CACHE_FILE,
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(
        (args.host, args.port),
        state_path=args.state,
        discovery_cache_path=args.discovery_cache,
    )
    logger.info(
        "jasper-voice-web listening on http://%s:%d (state=%s discovery_cache=%s)",
        args.host, args.port, args.state, args.discovery_cache,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
