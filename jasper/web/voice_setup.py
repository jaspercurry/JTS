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

This page renders on the canonical design system (the redesigned
management look): `canonical_page()` + the shared /assets/app.css
primitives, with a small page-specific stylesheet at
/assets/voice/voice.css for the active-provider radio group and the
pricing-rate grid. Page behaviour (clipboard copy + clear-key confirm)
ships as the ES module /assets/voice/js/main.js — no inline <script>.
The forms stay server-rendered request/response POSTs; only the
presentation changed.

URL surface (after nginx strips the /voice/ prefix):
  GET  /                          page render
  POST /save                      save credentials + active provider, restart
  POST /save-test                 save, run one silent voice-level test, restart
  POST /clear-credentials         clear one provider's key/model/voice
  POST /refresh-models            refresh one provider's cached model list
  POST /spend-cap                 save daily spend cap settings
  POST /pricing                   save one provider's pricing overrides
  POST /pricing-import            import pricing overrides from pasted JSON
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

from jasper.assistant_loudness import (
    DEFAULT_PROFILE_PATH as DEFAULT_LOUDNESS_PROFILE_PATH,
    ensure_seed_profile,
)
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
    DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
    DEFAULT_DAILY_SPEND_CAP_USD,
    DEFAULT_PRICING_FILE,
    DEFAULT_USAGE_DB,
    UsageStore,
    default_pricing_as_of,
    load_pricing_overrides,
    pricing_for_model,
    sanitize_pricing_models,
)

from ._common import (
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    delete_env_file,
    mask_secret,
    read_env_file,
    read_form,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_see_other,
    guard_mutating_request,
    write_env_file,
    write_json_file,
)

logger = logging.getLogger(__name__)


# Persisted at /var/lib/jasper/voice_provider.env. Operator-managed
# defaults still live in /etc/jasper/jasper.env; the systemd unit
# layers this file ON TOP so wizard-written values win.
PROVIDER_FILE = "/var/lib/jasper/voice_provider.env"
DISCOVERY_CACHE_FILE = DEFAULT_CACHE_PATH

# Page-specific stylesheet served static from /assets/ (the same path as
# app.css + the fonts). Only the visuals app.css doesn't already cover
# live here: the active-provider radio group, the pricing-rate grid, and
# the readonly research-prompt textarea sizing. Cache-busted by build SHA
# via canonical_page(page_css_href=...).
VOICE_PAGE_CSS_HREF = "/assets/voice/voice.css"


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


def _provider_label(provider_id: str) -> str:
    return next((p.label for p in PROVIDERS if p.id == provider_id), provider_id)


def _seed_config_from_state(state: dict[str, str]) -> SimpleNamespace:
    """Build the tiny Config-shaped object assistant_loudness needs.

    The wizard owns the env file, not the running jasper-voice process, so
    Save and Test uses this local view of the just-saved state.
    """
    values: dict[str, str] = {
        "voice_provider": _active_provider_id(state),
        "gemini_tts_model": os.environ.get("JASPER_GEMINI_TTS_MODEL", ""),
    }
    for provider in PROVIDERS:
        prefix = provider.id
        values[f"{prefix}_api_key"] = _value_for(state, provider.key_env)
        values[f"{prefix}_model"] = _value_for(
            state,
            provider.model_env,
            default_model_id(provider.id),
        )
        values[f"{prefix}_voice"] = _value_for(
            state,
            provider.voice_env,
            default_voice_id(provider.id),
        )
    return SimpleNamespace(**values)


def _redact_provider_error(exc: Exception, state: dict[str, str]) -> str:
    """Return a flash-safe error string without raw provider secrets."""
    msg = str(exc) or exc.__class__.__name__
    for provider in PROVIDERS:
        secret = _value_for(state, provider.key_env)
        if secret:
            msg = msg.replace(secret, mask_secret(secret))
    msg = " ".join(msg.split())
    if len(msg) > 220:
        msg = msg[:217] + "..."
    return msg


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
            else f"no {p.key_env} yet — paste below first"
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
      <p class="info-card__hint">Pick which real-time backend the wake-word
      loop talks to. Only providers with a saved API key can be selected.
      Paste a key below to enable a provider before saving.</p>
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
    return (
        f'<span class="badge" style="--tone:var(--status-{tone})">'
        f'{html.escape(label)}</span>'
    )


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
    usage_available = os.path.exists(usage_db)
    usage_error = ""
    spend_last_24h = 0.0
    month_to_date = 0.0
    sessions_today = 0
    if usage_available:
        try:
            store = UsageStore(usage_db)
            spend_last_24h = store.spend_last_24h_usd()
            month_to_date = store.spend_month_to_date_usd()
            sessions_today = store.session_count_today_utc()
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
    return f'<select name="{provider.id}_voice" form="save-form">{"".join(rows)}</select>'


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
                ' <span class="badge" style="--tone:var(--status-ok)">custom</span>'
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
            ' <span class="badge" style="--tone:var(--status-warn)">needs pricing</span>'
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
    <details class="pricing-disclosure">
      <summary>Pricing rates</summary>
      <div class="pricing-disclosure__body">
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
    chatbot's JSON. Standalone form POSTing to /pricing-import.

    The "Copy prompt" button is wired by the page's ES module (it carries
    no inline JS); it targets the textarea by id."""
    prompt = html.escape(_pricing_research_prompt(discovery))
    return f"""
    <section class="section">
      <h2 class="section__title">Refresh all rates from a chatbot</h2>
      <p class="form-hint">No provider API returns voice-model prices, so this
      speaker can't fetch them automatically. Copy the prompt below into any AI
      chatbot — it lists the exact models this speaker uses and asks for current
      official prices — then paste back the JSON it replies with.</p>
      <details class="pricing-disclosure">
        <summary>1. Copy this research prompt</summary>
        <div class="pricing-disclosure__body">
          <textarea id="pricing-prompt" class="prompt-box" readonly rows="14">{prompt}</textarea>
          <div class="form-actions">
            <button type="button" class="btn btn--default"
                    id="copy-prompt" data-copy-target="pricing-prompt">Copy prompt</button>
          </div>
        </div>
      </details>
      <details class="pricing-disclosure">
        <summary>2. Paste the JSON it gives you back</summary>
        <div class="pricing-disclosure__body">
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
    """One .info-card per provider (the canonical provider_cards
    archetype). The card body holds the masked API-key field, the model
    + voice selects, any provider extras, the collapsible pricing editor,
    and a clear-key form (when configured)."""
    configured = _provider_is_configured(state, provider)
    key_value = _value_for(state, provider.key_env)
    masked = mask_secret(key_value) if key_value else ""
    model_value = _value_for(
        state, provider.model_env, default_model_id(provider.id),
    )
    voice_value = _value_for(
        state, provider.voice_env, default_voice_id(provider.id),
    )
    if is_active:
        status_badge = (
            '<span class="badge" style="--tone:var(--status-ok)">active</span>'
        )
    elif configured:
        status_badge = (
            '<span class="badge" style="--tone:var(--status-idle)">configured</span>'
        )
    else:
        status_badge = (
            '<span class="badge" style="--tone:var(--status-warn)">'
            'not configured</span>'
        )
    key_source = ""
    if configured and not state.get(provider.key_env):
        # Key came from /etc/jasper/jasper.env (set by the operator,
        # not the wizard). Saving here writes a wizard-owned override.
        key_source = (
            '<p class="form-hint">Currently sourced from '
            '<code>/etc/jasper/jasper.env</code> — saving here will override it '
            'in <code>/var/lib/jasper/voice_provider.env</code>.</p>'
        )
    extras = _provider_extras_html(provider, state)
    placeholder = (
        "paste new key — leave blank to keep" if configured
        else f"paste your key ({provider.key_prefix_hint})"
    )
    clear_form = ""
    if configured:
        # The clear-key confirm is wired by the page's ES module (a
        # delegated submit handler on [data-confirm]); no inline JS.
        clear_form = f"""
        <form method="post" action="clear-credentials"
              data-confirm="Clear the saved {html.escape(provider.label, quote=True)} key and model/voice override? The daemon will fall back to /etc/jasper/jasper.env defaults."
              data-confirm-danger="1">
          {csrf_field_html(csrf_token)}
          <input type="hidden" name="provider" value="{provider.id}">
          <div class="form-actions">
            <button class="btn btn--danger" type="submit">Clear key</button>
          </div>
        </form>"""
    refresh_disabled = "" if configured else " disabled"
    refresh_hint = (
        "Queries the provider with this speaker's saved key, caches "
        "the result locally, and labels unknown models as experimental."
        if configured else
        f"Paste a {provider.key_env} first, then refresh available models."
    )
    return f"""
    <div class="info-card provider-card">
      <div class="provider-card__head">
        <div>
          <h2 class="section__title">{html.escape(provider.label)}</h2>
          <p class="eyebrow">{html.escape(provider.vendor)}</p>
        </div>
        {status_badge}
      </div>
      <p class="info-card__hint">
        Cost: <strong>{html.escape(provider.cost_hint)}</strong>.
        Get a key:
        <a href="{html.escape(provider.key_url, quote=True)}" target="_blank" rel="noopener">{html.escape(provider.vendor)} console ↗</a>
      </p>

      <div class="field">
        <label for="{provider.id}_key">{html.escape(provider.key_env)}</label>
        <input id="{provider.id}_key" name="{provider.id}_key" form="save-form"
               type="password" autocomplete="off" autocapitalize="off"
               autocorrect="off" spellcheck="false"
               data-provider-key="{provider.id}"
               placeholder="{html.escape(placeholder, quote=True)}">
        {f'<p class="form-hint">Currently saved: <code>{html.escape(masked)}</code></p>' if masked else ''}
        {key_source}
      </div>

      <div class="field">
        <label for="{provider.id}_model">Model</label>
        {_model_select_html(provider, model_value, discovered)}
        {_model_discovery_status_html(provider, discovered)}
      </div>
      <form method="post" action="refresh-models">
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

      {extras}

      {_pricing_section_html(provider, discovered, overrides, default_as_of, csrf_token)}

      {clear_form}
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
    # "save" form CANNOT enclose the per-card "Clear key" / "Refresh" /
    # "Pricing" forms. Layout:
    #   <form id="save-form">  ← active radios + csrf
    #   </form>                ← form closes BEFORE the cards
    #   {cards}                ← card inputs (key/model/voice/extras) use
    #                            form="save-form" to associate with the
    #                            outer form by ID. The per-card clear /
    #                            refresh / pricing forms stand alone, each
    #                            with their own csrf field.
    #   <button form="save-form">  ← the Save submit explicitly attaches
    body = f"""
{canonical_header("Voice provider")}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Configure the real-time voice backend for this speaker.
  Paste an API key into any provider you want to enable, pick which one is
  active, and save — the voice daemon picks up the change on its next restart
  (about 5 seconds).</p>

  <form method="post" action="save" id="save-form">
    {csrf_field_html(csrf_token)}
    {_active_radio_html(state)}
  </form>

  {_spend_cap_section_html(state, csrf_token)}

  <section class="section">
    <h2 class="section__title">Provider keys</h2>
    <p class="form-hint">Pasted keys are stored on this speaker only, written
    to <code>/var/lib/jasper/voice_provider.env</code> at mode 0600. They are
    never sent anywhere except the relevant provider's API.</p>
    {cards}
  </section>

  <div class="form-actions">
    <button type="submit" form="save-form" class="btn btn--primary">Save and restart voice</button>
    <button type="submit" form="save-form" formaction="save-test" class="btn btn--default">Save and Test</button>
  </div>

  {_pricing_refresh_html(discovery, csrf_token)}

  <p class="form-hint" style="margin-block-start:2rem">
    See <a href="https://github.com/jaspercurry/JTS/blob/main/docs/HANDOFF-voice-providers.md" target="_blank" rel="noopener">HANDOFF-voice-providers.md</a>
    for architecture, per-provider trade-offs, and the steps for adding a fourth backend.
  </p>
</main>
<script type="module" src="/assets/voice/js/main.js"></script>
"""
    return canonical_page(
        "Voice provider",
        body,
        csrf_token=csrf_token,
        page_css_href=VOICE_PAGE_CSS_HREF,
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
                default_as_of = default_pricing_as_of()
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
                "/save", "/save-test", "/clear-credentials",
                "/refresh-models", "/spend-cap", "/pricing", "/pricing-import",
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return
            if path == "/save":
                self._handle_save(form)
                return
            if path == "/save-test":
                self._handle_save_test(form)
                return
            if path == "/clear-credentials":
                self._handle_clear(form)
                return
            if path == "/refresh-models":
                self._handle_refresh_models(form)
                return
            if path == "/spend-cap":
                self._handle_spend_cap(form)
                return
            if path == "/pricing":
                self._handle_pricing(form)
                return
            if path == "/pricing-import":
                self._handle_pricing_import(form)
                return

        # --- route bodies ---

        def _save_provider_state(
            self,
            form: dict[str, str],
        ) -> tuple[dict[str, str] | None, str | None]:
            current = _load_state(cfg["state_path"])
            new, err = _apply_save(form, current)
            if err is not None:
                return None, err
            try:
                # _apply_save always sets JASPER_VOICE_PROVIDER to a valid id on
                # the success path (errors guarded above), and it survives the
                # blank-value filter — so `new` is never empty: always write,
                # never delete.
                write_env_file(cfg["state_path"], new)
            except OSError as e:
                logger.exception("could not write voice provider env file")
                return None, f"Could not save: {e}"
            return new, None

        def _handle_save(self, form: dict[str, str]) -> None:
            new, err = self._save_provider_state(form)
            if err is not None or new is None:
                send_see_other(self, "./", flash=err or "Could not save.")
                return
            restart_voice_daemon()
            active = new.get("JASPER_VOICE_PROVIDER", "")
            # The active provider (gemini/openai/grok) is the headline config
            # change — not a secret. The API keys in `new` are never logged.
            logger.info(
                "event=voice.save provider=%s client=%s",
                active, self.address_string(),
            )
            send_see_other(
                self, "./",
                flash=f"Saved. Voice daemon restarting on {_provider_label(active)}.",
            )

        def _handle_save_test(self, form: dict[str, str]) -> None:
            new, err = self._save_provider_state(form)
            if err is not None or new is None:
                send_see_other(self, "./", flash=err or "Could not save.")
                return
            active = new.get("JASPER_VOICE_PROVIDER", "")
            label = _provider_label(active)
            profile = None
            seed_error = ""
            try:
                profile = cfg["loudness_seed_fn"](
                    _seed_config_from_state(new),
                    path=cfg["assistant_loudness_profile_path"],
                    force=True,
                    max_attempts=1,
                    retry_backoff_sec=0.0,
                )
            except Exception as e:  # noqa: BLE001
                seed_error = _redact_provider_error(e, new)
                logger.warning(
                    "event=voice_loudness_seed provider=%s result=error error=%s",
                    active, e.__class__.__name__,
                )
            else:
                if profile is not None:
                    logger.info(
                        "event=voice_loudness_seed provider=%s result=ok "
                        "source_lufs=%.1f confidence=%.2f",
                        active, profile.source_lufs, profile.confidence,
                    )
                else:
                    seed_error = "provider key, model, or voice is incomplete."
                    logger.warning(
                        "event=voice_loudness_seed provider=%s result=skipped",
                        active,
                    )
            restart_voice_daemon()
            # Same save audit as _handle_save — the "Save & Test" button is the
            # other save path, so "voice provider saved" is logged either way.
            logger.info(
                "event=voice.save provider=%s client=%s",
                active, self.address_string(),
            )
            if seed_error:
                send_see_other(
                    self,
                    "./",
                    flash=(
                        f"Saved, but {label} voice test failed: "
                        f"{seed_error} Voice daemon restarting."
                    ),
                )
                return
            assert profile is not None
            send_see_other(
                self,
                "./",
                flash=(
                    f"Saved and tested {label}. "
                    f"Measured voice at {profile.source_lufs:.1f} LUFS; "
                    "voice daemon restarting."
                ),
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
            logger.info(
                "event=voice.clear provider=%s client=%s",
                pid, self.address_string(),
            )
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

        def _handle_spend_cap(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_spend_cap(form, current)
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            try:
                write_env_file(cfg["state_path"], new)
            except OSError as e:
                logger.exception("could not write spend-cap env settings")
                send_see_other(self, "./", flash=f"Could not save spend cap: {e}")
                return
            restart_voice_daemon()
            logger.info("event=voice.spend_cap client=%s", self.address_string())
            send_see_other(
                self,
                "./",
                flash="Saved spend cap. Voice daemon restarting.",
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
            logger.info(
                "event=pricing.edit provider=%s models=%d",
                provider.id, len(new_models),
            )
            restart_voice_daemon()
            send_see_other(
                self, "./",
                flash=(
                    f"Saved {provider.label} pricing. "
                    "Voice daemon restarting."
                ),
            )

        def _handle_pricing_import(self, form: dict[str, str]) -> None:
            models, as_of, err = _apply_pricing_paste(form.get("payload") or "")
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            # MERGE into existing overrides (like the per-provider editor):
            # pasted models overlay, models the paste omitted are preserved.
            # Sparsify so the file stays minimal. A full-replace here would
            # silently drop a hand-priced model the chatbot didn't return.
            existing = load_pricing_overrides(cfg["pricing_path"])
            merged = _sparsify_overrides({**existing, **models})
            try:
                if merged:
                    write_json_file(cfg["pricing_path"], {
                        "as_of": as_of or _today_iso(),
                        "source": "imported via /voice",
                        "models": merged,
                    })
                else:
                    try:
                        os.remove(cfg["pricing_path"])
                    except FileNotFoundError:
                        pass
            except OSError as e:
                logger.exception("could not write imported pricing")
                send_see_other(
                    self, "./", flash=f"Could not save pricing: {e}",
                )
                return
            logger.info(
                "event=pricing.import imported=%d total=%d",
                len(models), len(merged),
            )
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
    assistant_loudness_profile_path: str | None = None,
    loudness_seed_fn: Any | None = None,
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
        "assistant_loudness_profile_path": (
            assistant_loudness_profile_path
            or os.environ.get(
                "JASPER_ASSISTANT_LOUDNESS_PROFILE_PATH",
                DEFAULT_LOUDNESS_PROFILE_PATH,
            )
        ),
        "loudness_seed_fn": loudness_seed_fn or ensure_seed_profile,
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
