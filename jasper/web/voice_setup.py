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
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import re
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ._common import (
    PAGE_STYLE,
    delete_env_file,
    mask_secret,
    read_env_file,
    read_form,
    restart_voice_daemon,
    wrap_page,
    write_env_file,
)

logger = logging.getLogger(__name__)


# Persisted at /var/lib/jasper/voice_provider.env. Operator-managed
# defaults still live in /etc/jasper/jasper.env; the systemd unit
# layers this file ON TOP so wizard-written values win.
PROVIDER_FILE = "/var/lib/jasper/voice_provider.env"


# ----------------------------------------------------------------------
# Provider catalogue.
# ----------------------------------------------------------------------
#
# Each entry's `models` and `voices` are CURATED suggestions surfaced
# in the wizard's dropdowns. The voice daemon doesn't enforce these
# lists at runtime — it just passes whatever string is configured to
# the SDK — so an advanced user editing /etc/jasper/jasper.env by hand
# can still pick a model the wizard hasn't heard of. Refresh these on
# each major model release. See docs/HANDOFF-voice-providers.md for
# the per-provider trade-offs.


PROVIDERS = [
    {
        "id": "gemini",
        "label": "Gemini Live",
        "vendor": "Google",
        "key_env": "GEMINI_API_KEY",
        "key_prefix_hint": "AIzaSy…",
        "key_url": "https://aistudio.google.com/apikey",
        "model_env": "JASPER_GEMINI_MODEL",
        "voice_env": "JASPER_GEMINI_VOICE",
        # Pricing: ~$3 / $12 per 1M audio tokens (rough). The
        # cheapest of the three. 15-min audio cap with a 2-h
        # resumption handle.
        "cost_hint": "~$0.025 / minute",
        "models": [
            {"id": "gemini-3.1-flash-live-preview", "label": "3.1 Flash Live (preview, recommended)"},
            {"id": "gemini-2.5-flash-native-audio-preview-12-2025", "label": "2.5 Flash native-audio (fallback)"},
        ],
        # Gender/style hints sourced from Google's prebuilt-voices
        # catalogue (ai.google.dev/gemini-api/docs/speech-generation).
        "voices": [
            {"id": "Aoede", "label": "Aoede — feminine, breezy"},
            {"id": "Charon", "label": "Charon — masculine, informative"},
            {"id": "Fenrir", "label": "Fenrir — masculine, excitable"},
            {"id": "Kore", "label": "Kore — feminine, firm"},
            {"id": "Puck", "label": "Puck — masculine, upbeat"},
            {"id": "Leda", "label": "Leda — feminine, youthful"},
            {"id": "Orus", "label": "Orus — masculine, firm"},
            {"id": "Zephyr", "label": "Zephyr — feminine, bright"},
        ],
    },
    {
        "id": "openai",
        "label": "OpenAI Realtime",
        "vendor": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "key_prefix_hint": "sk-…",
        "key_url": "https://platform.openai.com/api-keys",
        "model_env": "JASPER_OPENAI_MODEL",
        "voice_env": "JASPER_OPENAI_VOICE",
        # gpt-realtime-2 audio: $32 / $64 / $0.40 per 1M tokens
        # (in / out / cached) → ~$0.30/minute of conversation.
        "cost_hint": "~$0.30 / minute (gpt-realtime-2)",
        "models": [
            {"id": "gpt-realtime-2", "label": "gpt-realtime-2 (released 2026-05-07, recommended)"},
            {"id": "gpt-realtime-mini", "label": "gpt-realtime-mini (cheaper, no reasoning)"},
            {"id": "gpt-realtime-1.5", "label": "gpt-realtime-1.5 (older GA)"},
        ],
        # Gender/style hints sourced from OpenAI's voice catalogue
        # (platform.openai.com/docs/guides/realtime). The user picked
        # `ash` once expecting feminine and got masculine — these
        # hints exist to head that off.
        "voices": [
            {"id": "marin", "label": "marin — feminine, warm"},
            {"id": "cedar", "label": "cedar — masculine, calm"},
            {"id": "alloy", "label": "alloy — neutral, balanced"},
            {"id": "ash", "label": "ash — masculine, soft"},
            {"id": "ballad", "label": "ballad — masculine, expressive"},
            {"id": "coral", "label": "coral — feminine, bright"},
            {"id": "echo", "label": "echo — masculine, smooth"},
            {"id": "sage", "label": "sage — feminine, even"},
            {"id": "shimmer", "label": "shimmer — feminine, light"},
            {"id": "verse", "label": "verse — masculine, melodic"},
        ],
        # gpt-realtime-2 specific: reasoning effort. Field is silently
        # dropped on non-`-2` models by the adapter.
        "extras": {
            "reasoning_effort": {
                "env": "JASPER_OPENAI_REASONING_EFFORT",
                "label": "Reasoning effort (gpt-realtime-2)",
                "default": "low",
                "options": [
                    ("minimal", "minimal — ~1.1 s TTFA, less coherent multi-step"),
                    ("low", "low (default) — best for short voice queries"),
                    ("medium", "medium"),
                    ("high", "high"),
                    ("xhigh", "xhigh — slowest, most thorough"),
                ],
                "hint": "Only meaningful on gpt-realtime-2. Silently ignored on older models.",
            },
        },
    },
    {
        "id": "grok",
        "label": "Grok Voice Agent",
        "vendor": "xAI",
        "key_env": "XAI_API_KEY",
        "key_prefix_hint": "xai-…",
        "key_url": "https://console.x.ai/",
        "model_env": "JASPER_GROK_MODEL",
        "voice_env": "JASPER_GROK_VOICE",
        # Flat $3.00/hour. Token-based spend cap under-counts under
        # Grok — daemon logs a warning at startup. ~$0.05/minute at
        # the listed rate.
        "cost_hint": "$3 / hour flat (~$0.05 / minute)",
        "models": [
            {"id": "grok-voice-think-fast-1.0", "label": "grok-voice-think-fast-1.0 (recommended)"},
        ],
        # Gender/style hints sourced from xAI's voice catalogue
        # (docs.x.ai/docs/guides/voice/agent).
        "voices": [
            {"id": "eve", "label": "eve — feminine, warm"},
            {"id": "ara", "label": "ara — feminine, casual"},
            {"id": "rex", "label": "rex — masculine, confident"},
            {"id": "sal", "label": "sal — masculine, casual"},
            {"id": "leo", "label": "leo — masculine, smooth"},
        ],
    },
]


_VALID_PROVIDER_IDS = {p["id"] for p in PROVIDERS}


# All env keys this wizard owns. We re-read the file on every page
# render and rewrite it whole on every save — the wizard is the source
# of truth for these, so keys outside this set in the existing file
# get LEFT ALONE (carried forward) but never produced. That keeps
# operators' /etc/jasper/jasper.env hand-edits compatible (the daemon
# sees them; the wizard doesn't trample them).
_OWNED_ENV_KEYS = {"JASPER_VOICE_PROVIDER"}
for _p in PROVIDERS:
    _OWNED_ENV_KEYS.add(_p["key_env"])
    _OWNED_ENV_KEYS.add(_p["model_env"])
    _OWNED_ENV_KEYS.add(_p["voice_env"])
    for _x in _p.get("extras", {}).values():
        _OWNED_ENV_KEYS.add(_x["env"])


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


def _provider_is_configured(state: dict[str, str], provider: dict) -> bool:
    return bool(_value_for(state, provider["key_env"]))


def _active_provider_id(state: dict[str, str]) -> str:
    """Active provider per the wizard's state (or the env if the wizard
    file hasn't been written yet). Falls back to ``gemini`` so a fresh
    install lands on something coherent — the page itself will then
    flag that the active provider has no key configured yet."""
    active = _value_for(state, "JASPER_VOICE_PROVIDER", "gemini")
    return active if active in _VALID_PROVIDER_IDS else "gemini"


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
        is_active = active == p["id"]
        radio_attrs = ["type=\"radio\"", f"name=\"active\"", f"value=\"{p['id']}\""]
        if is_active:
            radio_attrs.append("checked")
        if not configured:
            radio_attrs.append("disabled")
        radio_input = f"<input {' '.join(radio_attrs)}>"
        cls = "radio disabled" if not configured else "radio"
        status = (
            "configured" if configured
            else f"no {p['key_env']} yet — paste below first"
        )
        rows.append(f"""
          <label class="{cls}">
            {radio_input}
            <span class="name">{html.escape(p['label'])}</span>
            <span class="pricing">{html.escape(p['cost_hint'])}</span>
            <span class="meta" style="margin: 0">{html.escape(status)}</span>
          </label>""")
    return f"""
<div class="active-group">
  <h2>Use this provider for voice</h2>
  <p class="hint">Pick which real-time backend the wake-word loop talks to. Only providers with a saved API key can be selected. Press <strong>Save and restart</strong> at the bottom of the page to apply.</p>
  {''.join(rows)}
</div>"""


def _model_select_html(provider: dict, current: str) -> str:
    rows = []
    seen = set()
    for m in provider["models"]:
        sel = " selected" if m["id"] == current else ""
        rows.append(
            f'<option value="{html.escape(m["id"])}"{sel}>'
            f'{html.escape(m["label"])}</option>'
        )
        seen.add(m["id"])
    # If the daemon's configured model is something the wizard doesn't
    # know about, surface it as a custom row so the user doesn't get
    # silently switched to something else when they hit Save.
    if current and current not in seen:
        rows.insert(
            0,
            f'<option value="{html.escape(current)}" selected>'
            f'{html.escape(current)} (custom)</option>',
        )
    # `form="save-form"` associates this input with the outer
    # save-form by ID — necessary because the cards visually live
    # OUTSIDE the form's <form>...</form> tags so a per-card "Clear
    # key" form can sit beside them without nesting (HTML forbids
    # nested forms).
    return f'<select name="{provider["id"]}_model" form="save-form">{"".join(rows)}</select>'


def _voice_select_html(provider: dict, current: str) -> str:
    rows = []
    seen = set()
    for v in provider["voices"]:
        # `voices` entries are {"id": ..., "label": ...} dicts. Plain-
        # string entries are accepted as a back-compat path so an
        # operator hand-editing this file with a new voice doesn't
        # have to remember the schema.
        if isinstance(v, str):
            vid, vlabel = v, v
        else:
            vid, vlabel = v["id"], v["label"]
        sel = " selected" if vid == current else ""
        rows.append(f'<option value="{html.escape(vid)}"{sel}>{html.escape(vlabel)}</option>')
        seen.add(vid)
    if current and current not in seen:
        rows.insert(
            0,
            f'<option value="{html.escape(current)}" selected>'
            f'{html.escape(current)} (custom)</option>',
        )
    return f'<select name="{provider["id"]}_voice" form="save-form">{"".join(rows)}</select>'


def _provider_extras_html(provider: dict, state: dict[str, str]) -> str:
    """Render any provider-specific extra controls (today: OpenAI's
    reasoning_effort dropdown). Empty string when the provider has no
    extras."""
    extras = provider.get("extras") or {}
    if not extras:
        return ""
    out = []
    for field_name, spec in extras.items():
        current = _value_for(state, spec["env"], spec["default"])
        rows = []
        seen = set()
        for opt_id, opt_label in spec["options"]:
            sel = " selected" if opt_id == current else ""
            rows.append(
                f'<option value="{html.escape(opt_id)}"{sel}>'
                f'{html.escape(opt_label)}</option>'
            )
            seen.add(opt_id)
        if current and current not in seen:
            rows.insert(
                0,
                f'<option value="{html.escape(current)}" selected>'
                f'{html.escape(current)} (custom)</option>',
            )
        out.append(f"""
          <label for="{provider['id']}_{field_name}">{html.escape(spec['label'])}</label>
          <select id="{provider['id']}_{field_name}" name="{provider['id']}_{field_name}" form="save-form">
            {''.join(rows)}
          </select>
          <small>{html.escape(spec.get('hint', ''))}</small>""")
    return "\n".join(out)


def _provider_card_html(
    provider: dict, state: dict[str, str], *, is_active: bool,
) -> str:
    """One <details> card per provider. Open by default for the active
    provider so the user lands on what's currently in flight."""
    configured = _provider_is_configured(state, provider)
    key_value = _value_for(state, provider["key_env"])
    masked = mask_secret(key_value) if key_value else ""
    model_value = _value_for(state, provider["model_env"])
    voice_value = _value_for(state, provider["voice_env"])
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
    if configured and not state.get(provider["key_env"]):
        # Key came from /etc/jasper/jasper.env (set by the operator,
        # not the wizard). Saving here writes a wizard-owned override.
        key_source = (
            '<p class="key-source">Currently sourced from '
            '/etc/jasper/jasper.env — saving here will override it '
            'in /var/lib/jasper/voice_provider.env.</p>'
        )
    extras = _provider_extras_html(provider, state)
    return f"""
<details class="account"{open_attr}>
  <summary>
    <span class="name">{html.escape(provider['label'])}</span>
    <span class="meta" style="margin:0; font-size:0.85em">{html.escape(provider['vendor'])}</span>
    {badge_html}{active_badge}
  </summary>
  <div class="account-body provider-body">
    <p class="meta">
      Cost: <strong>{html.escape(provider['cost_hint'])}</strong>.
      Get a key:
      <a href="{html.escape(provider['key_url'])}" target="_blank" rel="noopener">{html.escape(provider['vendor'])} console ↗</a>
    </p>

    <label for="{provider['id']}_key">{html.escape(provider['key_env'])}</label>
    <input id="{provider['id']}_key" name="{provider['id']}_key" form="save-form"
           type="password" autocomplete="off" autocapitalize="off"
           autocorrect="off" spellcheck="false"
           placeholder="{html.escape('paste new key — leave blank to keep' if configured else f'paste your key ({provider["key_prefix_hint"]})')}">
    {f'<p class="meta">Currently saved: <code>{html.escape(masked)}</code></p>' if masked else ''}
    {key_source}

    <label for="{provider['id']}_model">Model</label>
    {_model_select_html(provider, model_value)}

    <label for="{provider['id']}_voice">TTS voice</label>
    {_voice_select_html(provider, voice_value)}

    {extras}

    {f'''
    <div class="actions">
      <form method="post" action="clear-credentials"
            onsubmit="return confirm('Clear the saved {html.escape(provider["label"])} key and model/voice override? The daemon will fall back to /etc/jasper/jasper.env defaults.');">
        <input type="hidden" name="provider" value="{provider['id']}">
        <button class="danger" type="submit">Clear key</button>
      </form>
    </div>
    ''' if configured else ''}
  </div>
</details>"""


def _index_html(state: dict[str, str], *, status_msg: str = "") -> bytes:
    active_id = _active_provider_id(state)
    cards = "".join(
        _provider_card_html(p, state, is_active=(p["id"] == active_id))
        for p in PROVIDERS
    )
    # Page structure note: HTML forbids nested forms, so the outer
    # "save" form CANNOT enclose the per-card "Clear key" forms. Layout:
    #   <form id="save-form">  ← active radios
    #     ...
    #   </form>                ← form closes BEFORE the cards
    #   <h2>Provider keys</h2>
    #   {cards}                ← card inputs use form="save-form"
    #                            attribute to associate with the outer
    #                            form by ID. Clear-key forms inside
    #                            cards stand alone with no nesting.
    #   <button form="save-form">  ← submit explicitly attaches
    body = f"""
<p class="sub">Configure the real-time voice backend for this speaker.
Paste an API key into any provider you want to enable, pick which one is
active, and save — the voice daemon picks up the change on its next
restart (about 5 seconds).</p>

<form method="post" action="save" id="save-form">
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


def _provider_by_id(provider_id: str) -> dict | None:
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p
    return None


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
        pid = p["id"]
        key = (form.get(f"{pid}_key") or "").strip()
        if key:
            err = _validate_key(key)
            if err:
                return current, f"{p['label']}: {err}"
            new[p["key_env"]] = key
        model = (form.get(f"{pid}_model") or "").strip()
        if model:
            new[p["model_env"]] = model
        voice = (form.get(f"{pid}_voice") or "").strip()
        if voice:
            new[p["voice_env"]] = voice
        for field_name, spec in (p.get("extras") or {}).items():
            val = (form.get(f"{pid}_{field_name}") or "").strip()
            if val:
                new[spec["env"]] = val

    active = (form.get("active") or "").strip()
    if active not in _VALID_PROVIDER_IDS:
        return current, f"Unknown provider {active!r}."
    active_provider = _provider_by_id(active)
    has_key = bool(
        new.get(active_provider["key_env"])
        or os.environ.get(active_provider["key_env"])
    )
    if not has_key:
        return current, (
            f"{active_provider['label']} has no API key configured "
            f"yet. Paste a {active_provider['key_env']} value before "
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
    p = _provider_by_id(pid)
    if p is None:
        return current, f"Unknown provider {pid!r}."
    new = dict(current)
    for env in (p["key_env"], p["model_env"], p["voice_env"]):
        new.pop(env, None)
    for spec in (p.get("extras") or {}).values():
        new.pop(spec["env"], None)
    return new, None


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

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)
            if path == "/":
                state = _load_state(cfg["state_path"])
                self._send_html(_index_html(
                    state, status_msg=qs.get("msg", [""])[0],
                ))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            form = read_form(self)
            if path == "/save":
                self._handle_save(form)
                return
            if path == "/clear-credentials":
                self._handle_clear(form)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        # --- route bodies ---

        def _handle_save(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_save(form, current)
            if err is not None:
                self._redirect(f"./?msg={urllib.parse.quote(err)}")
                return
            try:
                if new:
                    write_env_file(cfg["state_path"], new)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write voice provider env file")
                self._redirect(
                    f"./?msg={urllib.parse.quote(f'Could not save: {e}')}"
                )
                return
            restart_voice_daemon()
            active = new.get("JASPER_VOICE_PROVIDER", "")
            label = next(
                (p["label"] for p in PROVIDERS if p["id"] == active),
                active,
            )
            self._redirect(
                f"./?msg={urllib.parse.quote(f'Saved. Voice daemon restarting on {label}.')}"
            )

        def _handle_clear(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_clear(form, current)
            if err is not None:
                self._redirect(f"./?msg={urllib.parse.quote(err)}")
                return
            try:
                if new:
                    write_env_file(cfg["state_path"], new)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write voice provider env file")
                self._redirect(
                    f"./?msg={urllib.parse.quote(f'Could not save: {e}')}"
                )
                return
            restart_voice_daemon()
            pid = (form.get("provider") or "").strip()
            label = next(
                (p["label"] for p in PROVIDERS if p["id"] == pid),
                pid,
            )
            self._redirect(
                f"./?msg={urllib.parse.quote(f'Cleared {label} credentials.')}"
            )

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(target, *, state_path: str = PROVIDER_FILE) -> ThreadingHTTPServer:
    """Build a configured server. `target` is one of:
      - `socket.socket` — pre-bound listener handed off by systemd
      - `(host, port)` tuple — explicit bind
      - `int` — port, binds 127.0.0.1
    Mirrors the other wizard `make_server` signatures so jasper.web.__main__
    can drive all four uniformly."""
    from . import _systemd
    cfg = {"state_path": state_path}
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
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port), state_path=args.state)
    logger.info(
        "jasper-voice-web listening on http://%s:%d (state=%s)",
        args.host, args.port, args.state,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
