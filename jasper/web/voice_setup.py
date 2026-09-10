# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice configuration wizard at /assistant/voice/.

UX: a single page with distinct sections for the voice decisions:
enter provider API keys, choose the active configured provider, select
model/voice settings, set the spend cap, then adjust advanced pricing.
The active-provider radios are disabled for any provider that doesn't
have a key yet, so the user can't accidentally activate a broken backend.

Persistence: non-secret provider selectors stay in
/var/lib/jasper/voice_provider.env; provider API keys live in
/var/lib/jasper-secrets/voice_keys.env. Both are written at mode 0640 for the
daemon groups that need them. The systemd unit for jasper-voice sources these
files AFTER /etc/jasper/jasper.env, so wizard-written values win over
operator-managed defaults — same pattern as /spotify and its
spotify_credentials.env.

Restart: every successful save kicks `systemctl restart jasper-voice`.
The voice loop comes back ~3-5 s later on the new provider; the cue
manager's `cant_connect` plays if the new key is rejected upstream.

Page behaviour (clipboard copy + clear-key confirm) ships as the ES
module /assets/voice/js/main.js — no inline <script>. The forms stay
server-rendered request/response POSTs; only the presentation changed.

URL surface (after nginx strips the /assistant/voice/ prefix):
  GET  /                          page render
  POST /save                      save credentials + active provider, restart
  POST /save-test                 save, run one silent voice-level test, restart
  POST /clear-credentials         clear one provider's key/model/voice
  POST /refresh-models            refresh one provider's cached model list
  POST /spend-cap                 save daily spend cap settings
  POST /pricing                   save one provider's pricing overrides
  POST /pricing-import            import pricing overrides from pasted JSON

Page rendering — the canonical page shell, provider cards, spend-cap
section, and pricing editor — lives in jasper/web/voice_page.py (also
home to the /assets/voice/voice.css stylesheet's target markup); this
module owns the handler, routes, and save logic.
"""
from __future__ import annotations

import json
import logging
import math
import os
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
from jasper.voice.provider_state import (
    KEYS_FILE,
    PROVIDER_FILE,
)
from jasper.voice.model_discovery import (
    DEFAULT_CACHE_PATH,
    ModelDiscoveryError,
    load_cache,
    refresh_provider_cache,
)
from jasper.usage import (
    DEFAULT_PRICING_FILE,
    default_pricing_as_of,
    load_pricing_overrides,
    pricing_for_model,
    sanitize_pricing_models,
)
from jasper.log_event import log_event
from jasper.secret_redaction import redact_secrets

from ..env_file import delete_env_file, read_env_file, write_env_file
from ._common import (
    RESTART_CLAUSE,
    api_key_token_is_valid,
    begin_request,
    form_guarded,
    restart_voice_daemon,
    route_path,
    send_html_response,
    send_see_other,
    guard_read_request,
    write_json_file,
    SECRET_ENV_MODE,
    value_for_env as _value_for,
)
# Rendering helpers resolve names in voice_page's globals: patch voice_page.<name>, not these aliases.
from .voice_page import (
    _active_provider_id,
    _fmt_env_float,
    _fmt_env_money,
    _index_html,
    _provider_model_ids,
    _today_iso,
)

logger = logging.getLogger(__name__)

VOICE_ENV_OWNER = "JTS /assistant/voice wizard"



# Provider metadata lives in jasper.voice.catalog so the wizard's provider,
# model, voice, and extra-control metadata has one code-owned catalog to
# audit. The catalog is curated, not an allow-list: unknown configured
# models are preserved by the select rendering in voice_page.py instead of
# silently replaced.


# Loose validation — block obvious paste mistakes (whitespace, quotes,
# crlf) without rejecting any real-world key. The provider's API will
# reject anything actually malformed when the daemon connects, and
# the cue manager will play `cant_connect`.
# ----------------------------------------------------------------------
# State helpers — pure functions, no IO except inside read_*/write_*.
# ----------------------------------------------------------------------


# WS1 Phase 4a — the provider API keys are the only secrets the /voice
# wizard owns; they live in a separate file (KEYS_FILE) in the
# group-`jasper-secrets` dir, while the non-secret selectors
# (JASPER_VOICE_PROVIDER + per-provider model/voice/extras) stay in the
# broad PROVIDER_FILE so jasper-control can keep reading the active
# provider for /system/. This set is catalog-derived so a new provider's
# key env joins it automatically.
_SECRET_KEY_ENVS = frozenset(p.key_env for p in PROVIDERS)


def _load_merged(cfg: dict[str, Any]) -> dict[str, str]:
    """Wizard's full view: the non-secret selectors in ``state_path``
    UNIONED with the API keys in ``keys_path`` (the Phase-4a split). The
    two files own disjoint keys, so order does not matter. Reads are
    fail-soft (missing/unreadable → {})."""
    merged = read_env_file(cfg["state_path"])
    merged.update(read_env_file(cfg["keys_path"]))
    return merged


def _write_split(cfg: dict[str, Any], new: dict[str, str]) -> None:
    """Persist ``new`` across the two files: provider API keys go to the
    group-`jasper-secrets` ``keys_path``; everything else to the broad
    ``state_path``. Each file is deleted when its slice is empty (so a
    fully-cleared provider leaves no stale file). Atomic per file via
    ``write_env_file``; the setgid jasper-secrets dir gives keys_path its
    narrowed group automatically. Raises OSError on write failure — the
    callers wrap this to surface a flash + keep the daemon's last-good
    config."""
    secrets = {k: v for k, v in new.items() if k in _SECRET_KEY_ENVS}
    rest = {k: v for k, v in new.items() if k not in _SECRET_KEY_ENVS}
    if rest:
        write_env_file(
            cfg["state_path"], rest, mode=SECRET_ENV_MODE, owner=VOICE_ENV_OWNER,
        )
    else:
        delete_env_file(cfg["state_path"])
    if secrets:
        write_env_file(
            cfg["keys_path"], secrets, mode=SECRET_ENV_MODE, owner=VOICE_ENV_OWNER,
        )
    else:
        delete_env_file(cfg["keys_path"])


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
    """Return a flash-safe error string without raw provider secrets.

    The literal pass is not redundant with the pattern redactor: the wizards
    accept any `api_key_token_is_valid` token, so a pasted key carrying no
    recognised prefix is removable only by the literal the caller holds.
    """
    msg = str(exc) or exc.__class__.__name__
    literals = [_value_for(state, key_env) for key_env in _SECRET_KEY_ENVS]
    msg = " ".join(redact_secrets(msg, literals).split())
    if len(msg) > 220:
        msg = msg[:217] + "..."
    return msg


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
    if not api_key_token_is_valid(key):
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
    tempdir. The route tables live in this closure so the bodies can
    read `cfg`."""

    def _get_index(handler: BaseHTTPRequestHandler) -> None:
        state = _load_merged(cfg)
        discovery = load_cache(cfg["discovery_cache_path"])
        overrides = load_pricing_overrides(cfg["pricing_path"])
        default_as_of = default_pricing_as_of()
        ctx = begin_request(handler)
        send_html_response(handler, _index_html(
            state,
            ctx["csrf_token"],
            status_msg=ctx["flash"],
            discovery=discovery,
            overrides=overrides,
            default_as_of=default_as_of,
        ))

    def _save_provider_state(
        form: dict[str, str],
    ) -> tuple[dict[str, str] | None, str | None]:
        current = _load_merged(cfg)
        new, err = _apply_save(form, current)
        if err is not None:
            return None, err
        try:
            # _apply_save always sets JASPER_VOICE_PROVIDER + the active
            # provider's API key (the has_key guard), so both slices of the
            # split are non-empty: provider/model → state_path, keys →
            # keys_path (group-jasper-secrets). Never deletes on this path.
            _write_split(cfg, new)
        except OSError as e:
            logger.exception("could not write voice provider env file")
            return None, f"Could not save: {e}"
        return new, None

    @form_guarded
    def _post_save(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        new, err = _save_provider_state(form)
        if err is not None or new is None:
            send_see_other(handler, "./", flash=err or "Could not save.")
            return
        outcome = restart_voice_daemon()
        active = new.get("JASPER_VOICE_PROVIDER", "")
        # The active provider (gemini/openai/grok) is the headline config
        # change — not a secret. The API keys in `new` are never logged.
        log_event(
            logger,
            "voice.save",
            provider=active,
            client=handler.address_string(),
        )
        send_see_other(
            handler, "./",
            flash=f"Saved {_provider_label(active)}.{RESTART_CLAUSE[outcome]}",
        )

    @form_guarded
    def _post_save_test(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        new, err = _save_provider_state(form)
        if err is not None or new is None:
            send_see_other(handler, "./", flash=err or "Could not save.")
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
            log_event(
                logger,
                "voice.loudness_seed",
                provider=active,
                result="error",
                error=e.__class__.__name__,
                level=logging.WARNING,
            )
        else:
            if profile is not None:
                log_event(
                    logger,
                    "voice.loudness_seed",
                    provider=active,
                    result="ok",
                    source_lufs=f"{profile.source_lufs:.1f}",
                    confidence=f"{profile.confidence:.2f}",
                )
            else:
                seed_error = "provider key, model, or voice is incomplete."
                log_event(
                    logger,
                    "voice.loudness_seed",
                    provider=active,
                    result="skipped",
                    level=logging.WARNING,
                )
        clause = RESTART_CLAUSE[restart_voice_daemon()]
        # Same save audit as _post_save — the "Save & Test" button is the
        # other save path, so "voice provider saved" is logged either way.
        log_event(
            logger,
            "voice.save",
            provider=active,
            client=handler.address_string(),
        )
        if seed_error:
            send_see_other(
                handler,
                "./",
                flash=(
                    f"Saved, but {label} voice test failed: "
                    f"{seed_error}{clause}"
                ),
            )
            return
        assert profile is not None
        send_see_other(
            handler,
            "./",
            flash=(
                f"Saved and tested {label}. "
                f"Measured voice at {profile.source_lufs:.1f} LUFS."
                f"{clause}"
            ),
        )

    @form_guarded
    def _post_clear_credentials(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        current = _load_merged(cfg)
        new, err = _apply_clear(form, current)
        if err is not None:
            send_see_other(handler, "./", flash=err)
            return
        try:
            # _write_split deletes whichever file's slice is now empty —
            # clearing the last provider removes both state_path AND the
            # keys_path, so no stale key file lingers.
            _write_split(cfg, new)
        except OSError as e:
            logger.exception("could not write voice provider env file")
            send_see_other(handler, "./", flash=f"Could not save: {e}")
            return
        clause = RESTART_CLAUSE[restart_voice_daemon()]
        pid = (form.get("provider") or "").strip()
        log_event(
            logger,
            "voice.clear",
            provider=pid,
            client=handler.address_string(),
        )
        label = next(
            (p.label for p in PROVIDERS if p.id == pid),
            pid,
        )
        send_see_other(
            handler, "./", flash=f"Cleared {label} credentials.{clause}",
        )

    @form_guarded
    def _post_refresh_models(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        current = _load_merged(cfg)
        pid = (form.get("provider") or "").strip()
        provider = provider_by_id(pid)
        if provider is None:
            send_see_other(handler, "./", flash=f"Unknown provider {pid!r}.")
            return
        api_key = _provider_key_for_discovery(provider, current)
        if not api_key:
            send_see_other(
                handler,
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
            log_event(
                logger,
                "voice.model_discovery",
                provider=provider.id,
                result="error",
                error=repr(str(e)),
                level=logging.WARNING,
            )
            send_see_other(
                handler,
                "./",
                flash=f"Could not refresh {provider.label} models: {e}",
            )
            return
        log_event(
            logger,
            "voice.model_discovery",
            provider=provider.id,
            result="ok",
            count=len(snapshot.models),
        )
        send_see_other(
            handler,
            "./",
            flash=(
                f"Refreshed {provider.label} models. "
                "Newly discovered models are experimental until tested."
            ),
        )

    @form_guarded
    def _post_spend_cap(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        current = _load_merged(cfg)
        new, err = _apply_spend_cap(form, current)
        if err is not None:
            send_see_other(handler, "./", flash=err)
            return
        try:
            # current came from _load_merged, so `new` still carries the API
            # keys; _write_split keeps them in keys_path rather than writing
            # them back into the broad state_path.
            _write_split(cfg, new)
        except OSError as e:
            logger.exception("could not write spend-cap env settings")
            send_see_other(handler, "./", flash=f"Could not save spend cap: {e}")
            return
        clause = RESTART_CLAUSE[restart_voice_daemon()]
        log_event(logger, "voice.spend_cap", client=handler.address_string())
        send_see_other(
            handler,
            "./",
            flash=f"Saved spend cap.{clause}",
        )

    @form_guarded
    def _post_pricing(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        pid = (form.get("provider") or "").strip()
        provider = provider_by_id(pid)
        if provider is None:
            send_see_other(handler, "./", flash=f"Unknown provider {pid!r}.")
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
                handler, "./", flash=f"Could not save pricing: {e}",
            )
            return
        log_event(
            logger,
            "pricing.edit",
            provider=provider.id,
            models=len(new_models),
        )
        clause = RESTART_CLAUSE[restart_voice_daemon()]
        send_see_other(
            handler, "./",
            flash=f"Saved {provider.label} pricing.{clause}",
        )

    @form_guarded
    def _post_pricing_import(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        models, as_of, err = _apply_pricing_paste(form.get("payload") or "")
        if err is not None:
            send_see_other(handler, "./", flash=err)
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
                handler, "./", flash=f"Could not save pricing: {e}",
            )
            return
        log_event(
            logger,
            "pricing.import",
            imported=len(models),
            total=len(merged),
        )
        clause = RESTART_CLAUSE[restart_voice_daemon()]
        send_see_other(
            handler, "./",
            flash=f"Imported rates for {len(models)} model(s).{clause}",
        )

    _GET_ROUTES = {"/": _get_index}
    _POST_ROUTES = {
        "/save": _post_save,
        "/save-test": _post_save_test,
        "/clear-credentials": _post_clear_credentials,
        "/refresh-models": _post_refresh_models,
        "/spend-cap": _post_spend_cap,
        "/pricing": _post_pricing,
        "/pricing-import": _post_pricing_import,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            handler_fn = _GET_ROUTES.get(route_path(self.path))
            if handler_fn is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_read_request(self):
                return
            handler_fn(self)

        def do_POST(self) -> None:  # noqa: N802
            handler_fn = _POST_ROUTES.get(route_path(self.path))
            if handler_fn is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            handler_fn(self)

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(
    target,
    *,
    state_path: str = PROVIDER_FILE,
    keys_path: str = KEYS_FILE,
    discovery_cache_path: str = DEFAULT_CACHE_PATH,
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
    from ..platform import systemd
    cfg = {
        "state_path": state_path,
        "keys_path": keys_path,
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
    return systemd.make_http_server(target, _make_handler(cfg))
