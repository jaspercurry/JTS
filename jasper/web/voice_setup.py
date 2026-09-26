# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes and persistence for the voice setup wizard.

Keys and provider settings retain their separate files. Provider selection on
GET only chooses the form; POST /save activates it through
:func:`jasper.voice.provider_state.select_voice` (ADR-0350).
"""
from __future__ import annotations

import logging
import os
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jasper.control.service_restart import restart_voice_daemon
from jasper.assistant_loudness import (
    DEFAULT_PROFILE_PATH as DEFAULT_LOUDNESS_PROFILE_PATH,
    ensure_seed_profile,
)
from jasper.env_load import VOICE_PROVIDER_ENV_PATH
from jasper.voice.catalog import (
    PROVIDERS,
    default_model_id,
    default_voice_id,
    provider_by_id,
)
from jasper.voice.provider_state import (
    KEYS_FILE,
    PROVIDER_FILE_MODE,
    VOICE_PROVIDER_ENV_OWNER,
    VoiceSelectionRefused,
    select_voice,
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
)
from jasper.log_event import log_event
from jasper.secret_redaction import redact_secrets

from ..atomic_io import atomic_write_json, locked_transform_env_file
from ..env_file import read_env_file
from ..platform import systemd
from ._common import (
    RESTART_CLAUSE,
    api_key_token_is_valid,
    begin_request,
    dispatch_get,
    dispatch_post,
    form_guarded,
    send_html_response,
    send_rejected_form,
    send_see_other,
    SECRET_ENV_MODE,
    value_for_env as _value_for,
)
from .voice_page import index_html
from .voice_cost_page import costs_html
from .voice_settings import (
    active_provider_id as _active_provider_id,
    provider_model_ids as _provider_model_ids,
    submitted_settings,
)
from .voice_costs import (
    apply_spend_cap, apply_pricing_save, apply_pricing_paste, sparsify_overrides, today_iso,
)

logger = logging.getLogger(__name__)

# The keys file has no second front end (ADR-0350), so its header names this wizard.
VOICE_ENV_OWNER = "JTS /assistant/voice wizard"


_SECRET_KEY_ENVS = frozenset(p.key_env for p in PROVIDERS)


def _load_merged(cfg: dict[str, Any]) -> dict[str, str]:
    """Wizard's full view: the non-secret selectors in ``state_path``
    UNIONED with the API keys in ``keys_path``. The
    two files own disjoint keys, so order does not matter. Reads are
    fail-soft (missing/unreadable → {})."""
    merged = read_env_file(cfg["state_path"])
    merged.update(read_env_file(cfg["keys_path"]))
    return merged


def _write_half(
    cfg: dict[str, Any], before: dict[str, str], after: dict[str, str], *, secret: bool,
) -> None:
    """Apply the edit ``before`` → ``after`` (merged views) under one file's
    lock: API keys to ``keys_path`` (``secret``), the rest to ``state_path``.
    Another writer's key survives, a key in the other file moves to its own,
    and an emptied file is deleted. Raises OSError; the callers flash it."""
    ours = lambda key: (key in _SECRET_KEY_ENVS) is secret
    changed = {k for k, v in after.items() if ours(k) and before.get(k) != v}
    dropped = {k for k in before if ours(k) and k not in after}

    def transform(fresh: dict[str, str]) -> dict[str, str] | None:
        kept = {k: v for k, v in fresh.items() if ours(k) and k not in dropped}
        added = {
            k: v for k, v in after.items()
            if ours(k) and (k in changed or k not in kept)
        }
        return {**kept, **added} or None

    path, mode, owner = (
        (cfg["keys_path"], SECRET_ENV_MODE, VOICE_ENV_OWNER) if secret
        else (cfg["state_path"], PROVIDER_FILE_MODE, VOICE_PROVIDER_ENV_OWNER)
    )
    locked_transform_env_file(path, transform, mode=mode, owner=owner)


def _write_split(cfg: dict[str, Any], before: dict[str, str], after: dict[str, str]) -> None:
    _write_half(cfg, before, after, secret=True)
    _write_half(cfg, before, after, secret=False)


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
    """Apply posted fields; blank keys and omitted settings keep their saved values."""
    new = dict(current)
    for p in PROVIDERS:
        pid = p.id
        key = (form.get(f"{pid}_key") or "").strip()
        if key:
            err = _validate_key(key)
            if err:
                return current, f"{p.label}: {err}"
            new[p.key_env] = key
        new.update(submitted_settings(p, form))

    active = (form.get("active") or "").strip()
    if provider_by_id(active) is None:
        return current, f"Unknown provider {active!r}."
    new["JASPER_VOICE_PROVIDER"] = active

    # Drop any blank values we accidentally produced (e.g. user picks
    # "(custom)" placeholder — defensive against future UI changes).
    new = {k: v for k, v in new.items() if v}
    return new, None


def _apply_clear(form: dict[str, str], current: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """Remove only the selected provider's saved key."""
    pid = (form.get("provider") or "").strip()
    p = provider_by_id(pid)
    if p is None:
        return current, f"Unknown provider {pid!r}."
    new = dict(current)
    new.pop(p.key_env, None)
    return new, None


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Returns a request handler class closed over the config dict.
    `cfg` carries the persisted-state file path so tests can swap a
    tempdir. The route tables live in this closure so the bodies can
    read `cfg`."""

    def _page(state: dict[str, str], selected: str | None = None):
        return partial(
            index_html, state, selected=selected,
            discovery=load_cache(cfg["discovery_cache_path"]),
        )

    def _get_index(handler: BaseHTTPRequestHandler) -> None:
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
        ctx = begin_request(handler)
        send_html_response(handler, _page(
            _load_merged(cfg), query.get("provider", [None])[0],
        )(
            csrf_token=ctx["csrf_token"],
            status_msg=ctx["flash"],
        ))

    def _get_costs(handler: BaseHTTPRequestHandler) -> None:
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
        ctx = begin_request(handler)
        send_html_response(handler, costs_html(
            _load_merged(cfg), ctx["csrf_token"], status_msg=ctx["flash"],
            selected=query.get("provider", [None])[0],
            discovery=load_cache(cfg["discovery_cache_path"]),
            overrides=load_pricing_overrides(cfg["pricing_path"]),
            default_as_of=default_pricing_as_of(),
        ))

    def _costs_location(form: dict[str, str]) -> str:
        provider = provider_by_id(form.get("provider", ""))
        return f"costs?provider={provider.id}" if provider else "costs"

    def _reject_save(
        handler, form: dict[str, str], error: str, *, key_saved: bool = False,
    ) -> None:
        state = _load_merged(cfg)
        provider = provider_by_id(form.get("active", ""))
        if provider:
            state.update(submitted_settings(provider, form))
            if form.get(f"{provider.id}_key") and not key_saved:
                error += " Enter the new key again."
        send_rejected_form(
            handler, _page(state, provider.id if provider else ""), flash=error,
        )

    def _save_provider_state(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> dict[str, str] | None:
        """The merged state saved, or None once the rejection is answered."""
        current = _load_merged(cfg)
        new, err = _apply_save(form, current)
        if err is not None:
            _reject_save(handler, form, err)
            return None
        provider = provider_by_id(new["JASPER_VOICE_PROVIDER"])
        assert provider is not None  # _apply_save refused an unknown one
        selection = ("JASPER_VOICE_PROVIDER", provider.model_env, provider.voice_env)
        settings = {k: v for k, v in new.items() if k not in selection}
        settings.update((k, current[k]) for k in selection if k in current)
        keys_saved = False
        try:
            # Keys first, so select_voice finds a key this form brought; the
            # rest after, so a refused selection saves no other setting.
            _write_half(cfg, current, settings, secret=True)
            keys_saved = True
            select_voice(
                provider.id, new.get(provider.model_env), voice=new.get(provider.voice_env),
                via="wizard", client=handler.address_string(),
                path=cfg["state_path"], keys_path=cfg["keys_path"],
                discovery_path=cfg["discovery_cache_path"],
            )
            _write_half(cfg, current, settings, secret=False)
        except VoiceSelectionRefused as e:
            # The keys half is saved by now: the page must not ask for the key again.
            _reject_save(handler, form, str(e), key_saved=True)
            return None
        except OSError as e:
            logger.exception("could not write voice provider env file")
            _reject_save(handler, form, f"Could not save: {e}", key_saved=keys_saved)
            return None
        return new

    @form_guarded
    def _post_save(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        new = _save_provider_state(handler, form)
        if new is None:
            return
        outcome = restart_voice_daemon()
        active = new.get("JASPER_VOICE_PROVIDER", "")
        send_see_other(
            handler, "./",
            flash=f"Saved {_provider_label(active)}.{RESTART_CLAUSE[outcome]}",
        )

    @form_guarded
    def _post_save_test(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        new = _save_provider_state(handler, form)
        if new is None:
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
        pid = (form.get("provider") or "").strip()
        current = _load_merged(cfg)
        new, err = _apply_clear(form, current)
        if err is not None:
            send_see_other(handler, "./", flash=err)
            return
        try:
            # _write_split deletes whichever file's slice is now empty —
            # clearing the last provider removes both state_path AND the
            # keys_path, so no stale key file lingers.
            _write_split(cfg, current, new)
        except OSError as e:
            logger.exception("could not write voice provider env file")
            send_see_other(handler, f"./?provider={pid}", flash=f"Could not save: {e}")
            return
        clause = RESTART_CLAUSE[restart_voice_daemon()]
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
            handler, f"./?provider={pid}", flash=f"Cleared {label} credentials.{clause}",
        )

    @form_guarded
    def _post_refresh_models(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        current = _load_merged(cfg)
        pid = (form.get("provider") or "").strip()
        provider = provider_by_id(pid)
        if provider is None:
            send_see_other(handler, "./", flash="Choose a provider.")
            return
        api_key = _value_for(current, provider.key_env).strip()
        if not api_key:
            send_see_other(
                handler,
                f"./?provider={pid}",
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
                f"./?provider={pid}",
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
            f"./?provider={pid}",
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
        new, err = apply_spend_cap(form, current)
        if err is not None:
            send_see_other(handler, _costs_location(form), flash=err)
            return
        try:
            # current came from _load_merged, so `new` still carries the API
            # keys; _write_split keeps them in keys_path rather than writing
            # them back into the broad state_path.
            _write_split(cfg, current, new)
        except OSError as e:
            logger.exception("could not write spend-cap env settings")
            send_see_other(handler, _costs_location(form), flash=f"Could not save spend cap: {e}")
            return
        clause = RESTART_CLAUSE[restart_voice_daemon()]
        log_event(logger, "voice.spend_cap", client=handler.address_string())
        send_see_other(
            handler,
            _costs_location(form),
            flash=f"Saved spend cap.{clause}",
        )

    @form_guarded
    def _post_pricing(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        pid = (form.get("provider") or "").strip()
        provider = provider_by_id(pid)
        if provider is None:
            send_see_other(handler, _costs_location(form), flash=f"Unknown provider {pid!r}.")
            return
        discovery = load_cache(cfg["discovery_cache_path"])
        model_ids = _provider_model_ids(provider, discovery.get(provider.id))
        existing = load_pricing_overrides(cfg["pricing_path"])
        new_models = apply_pricing_save(form, provider, model_ids, existing)
        try:
            if new_models:
                atomic_write_json(cfg["pricing_path"], {
                    "as_of": today_iso(),
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
                handler, _costs_location(form), flash=f"Could not save pricing: {e}",
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
            handler, _costs_location(form),
            flash=f"Saved {provider.label} pricing.{clause}",
        )

    @form_guarded
    def _post_pricing_import(
        handler: BaseHTTPRequestHandler, form: dict[str, str],
    ) -> None:
        models, as_of, err = apply_pricing_paste(form.get("payload") or "")
        if err is not None:
            send_see_other(handler, _costs_location(form), flash=err)
            return
        # MERGE into existing overrides (like the per-provider editor):
        # pasted models overlay, models the paste omitted are preserved.
        # Sparsify so the file stays minimal. A full-replace here would
        # silently drop a hand-priced model the chatbot didn't return.
        existing = load_pricing_overrides(cfg["pricing_path"])
        merged = sparsify_overrides({**existing, **models})
        try:
            if merged:
                atomic_write_json(cfg["pricing_path"], {
                    "as_of": as_of or today_iso(),
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
                handler, _costs_location(form), flash=f"Could not save pricing: {e}",
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
            handler, _costs_location(form),
            flash=f"Imported rates for {len(models)} model(s).{clause}",
        )

    _GET_ROUTES = {"/": _get_index, "/costs": _get_costs}
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
            dispatch_get(self, _GET_ROUTES)

        def do_POST(self) -> None:  # noqa: N802
            dispatch_post(self, _POST_ROUTES, guard="per-body")

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(
    target,
    *,
    state_path: str = VOICE_PROVIDER_ENV_PATH,
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
    The shared signature lets jasper.web.__main__ construct the wizard.
    `pricing_path` defaults to the same
    JASPER_PRICING_FILE the daemon reads, so edits land where it looks."""
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
