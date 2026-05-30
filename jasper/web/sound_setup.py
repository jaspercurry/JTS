"""Sound curve and preference-EQ page at /sound/.

URL surface (after nginx strips /sound/):
  GET  /         page render
  GET  /state    persisted profile + preview + stock curve metadata
  POST /preview  preview a draft profile without touching live audio
  POST /live-draft apply a draft to live audio without persisting
  POST /audition validate and load a draft/bypass config without persisting
  POST /profiles/save save or update a named custom profile
  POST /profiles/rename rename a named custom profile
  POST /profiles/delete delete a named custom profile
  POST /apply    validate, persist, emit CamillaDSP config, load it

The page is built on the canonical design system (jasper.web._common.
canonical_page + /assets/app.css). The view's Off / Saved / Draft tabs
ARE the live source: Off auditions bypass, Saved applies a chosen
profile, Draft hot-loads the working bands via /live-draft while editing
and commits via the Save footer. All durable writes go through /apply;
the safety floor (volume_limit, headroom preamp, room-PEQ preservation)
lives in the backend and is untouched here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from jasper.sound.profile import (
    ADVANCED_GAIN_LIMIT_DB,
    MAX_FREQ_HZ,
    MAX_PARAMETRIC_BANDS,
    MAX_Q,
    MIN_FREQ_HZ,
    MIN_Q,
    PROFILE_LIBRARY_PATH,
    PROFILE_PATH,
    SIMPLE_EQ_LIMIT_DB,
    SoundProfile,
    build_sound_filters,
    curve_payload,
    delete_named_profile,
    estimate_headroom_db,
    load_profile_library,
    load_profile,
    loudness_compensation_db,
    profile_library_payload,
    rename_named_profile,
    response_component_payload,
    response_preview,
    save_named_profile,
    save_profile,
    simple_bands_payload,
)
from jasper.sound.settings import (
    HEADROOM_TRIM_MAX_DB,
    SoundSettings,
    load_sound_settings,
    save_sound_settings,
)

from ._common import (
    begin_request,
    canonical_page,
    csrf_fetch_helpers_js,
    reject_csrf,
    send_html_response,
    verify_csrf,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = "/var/lib/camilladsp/configs"
MAX_JSON_BYTES = 64 * 1024
LIVE_DRAFT_UNAVAILABLE_LOG_INTERVAL_SEC = 30.0

_live_draft_unavailable_log_at: dict[str, float] = {}


def _camilla():
    from jasper.camilla import CamillaController

    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


def _output_trim_db(profile: SoundProfile, settings: SoundSettings) -> float:
    """Total post-EQ attenuation for this profile under the current global
    settings: the manual headroom trim, plus the profile's loudness
    compensation when match-loudness is on. Both default to 0, so the
    default is no trim at all -- boosts boost. (The emitter additionally
    ignores any trim on a flat profile, which can't clip from EQ.)"""
    trim = settings.headroom_trim_db
    if settings.match_loudness:
        trim += loudness_compensation_db(profile)
    return round(trim, 3)


def _state_payload(
    profile: SoundProfile,
    *,
    library_path: str | Path | None = None,
    include_library: bool = False,
) -> dict[str, Any]:
    from jasper.dsp_apply import dsp_write_epoch_from_state, last_dsp_apply_state

    last_dsp_apply = last_dsp_apply_state()
    settings = load_sound_settings()

    payload = {
        "profile": profile.to_dict(),
        "curves": curve_payload(),
        "preview": response_preview(profile),
        "components": response_component_payload(profile),
        "headroom_db": estimate_headroom_db(profile),
        # Authoritative "is an EQ effectively applied?" signal: 0 when the
        # profile is disabled (bypass) OR flat (no active filters). The
        # page opens on Off vs Saved based on this.
        "filter_count": len(build_sound_filters(profile)),
        # Global output settings + the trim they imply for THIS profile, so
        # the page can render the controls and show the effective trim.
        "sound_settings": settings.to_dict(),
        "output_trim_db": _output_trim_db(profile, settings),
        "limits": {
            "simple_gain_db": SIMPLE_EQ_LIMIT_DB,
            "advanced_gain_db": ADVANCED_GAIN_LIMIT_DB,
            "max_parametric_bands": MAX_PARAMETRIC_BANDS,
            "min_freq_hz": MIN_FREQ_HZ,
            "max_freq_hz": MAX_FREQ_HZ,
            "min_q": MIN_Q,
            "max_q": MAX_Q,
            "simple_bands": simple_bands_payload(),
            "headroom_trim_max_db": HEADROOM_TRIM_MAX_DB,
        },
        "last_dsp_apply": last_dsp_apply,
        "dsp_write_epoch": dsp_write_epoch_from_state(last_dsp_apply),
    }
    if include_library:
        payload["profile_library"] = profile_library_payload(
            load_profile_library(library_path)
        )
    return payload


def _log_live_draft_unavailable(
    *,
    reason: str,
    output_trim_db: float,
    room_peq_count: int,
    sound_filter_count: int,
    error: Exception | None = None,
) -> None:
    now = time.monotonic()
    last = _live_draft_unavailable_log_at.get(reason, 0.0)
    if now - last < LIVE_DRAFT_UNAVAILABLE_LOG_INTERVAL_SEC:
        return
    _live_draft_unavailable_log_at[reason] = now
    logger.warning(
        "event=sound.live_draft result=unavailable reason=%s "
        "output_trim=%.1f room_peqs=%d sound_filters=%d err=%r",
        reason,
        output_trim_db,
        room_peq_count,
        sound_filter_count,
        error,
    )


async def _apply_profile(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    settings = load_sound_settings()
    apply_state, out_path, stamped = await _load_profile_config(
        profile.with_timestamp(),
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
        source="sound",
        persist_profile=True,
        output_trim_db=_output_trim_db(profile, settings),
    )
    logger.info(
        "event=sound.apply enabled=%s curve=%s "
        "simple=%.1f/%.1f/%.1f/%.1f/%.1f bands=%d room_peqs=%d config=%s op_id=%s",
        stamped.enabled,
        stamped.curve_id,
        stamped.simple_eq.sub_bass_db,
        stamped.simple_eq.bass_db,
        stamped.simple_eq.mid_db,
        stamped.simple_eq.presence_db,
        stamped.simple_eq.treble_db,
        len(stamped.parametric_bands),
        apply_state.room_peq_count or 0,
        out_path,
        apply_state.op_id,
    )
    payload = _state_payload(
        stamped,
        library_path=library_path,
        include_library=library_path is not None,
    )
    payload["active_config_path"] = str(out_path)
    payload["preserved_room_peqs"] = apply_state.room_peq_count or 0
    payload["last_dsp_apply"] = apply_state.to_dict()
    payload["dsp_write_epoch"] = apply_state.op_id
    return payload


async def _apply_settings(
    settings: SoundSettings,
    *,
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    """Persist global sound settings, then re-emit the active profile's config
    with the new output trim so the change is audible immediately.

    The profile content is unchanged, so this re-applies it **without**
    re-stamping or re-persisting the profile JSON (unlike `_apply_profile`).
    Settings are saved first; a write error propagates as `OSError`. A failed
    re-apply returns the saved state with a ``warning`` rather than reverting
    a setting the backend already kept — no silent failure either way.
    """
    save_sound_settings(settings)
    logger.info(
        "event=sound.settings headroom_trim=%.1f match_loudness=%s",
        settings.headroom_trim_db,
        settings.match_loudness,
    )
    profile = load_profile(profile_path)
    payload = _state_payload(
        profile,
        library_path=library_path,
        include_library=library_path is not None,
    )
    try:
        apply_state, out_path, _ = await _load_profile_config(
            profile,
            profile_path=profile_path,
            config_dir=config_dir,
            camilla_factory=camilla_factory,
            source="sound_settings",
            persist_profile=False,
            output_trim_db=_output_trim_db(profile, settings),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("sound settings re-apply failed")
        payload["warning"] = f"Saved, but applying to the speaker failed: {e}"
        return payload
    payload["active_config_path"] = str(out_path)
    payload["preserved_room_peqs"] = apply_state.room_peq_count or 0
    payload["last_dsp_apply"] = apply_state.to_dict()
    payload["dsp_write_epoch"] = apply_state.op_id
    return payload


async def _audition_profile(
    profile: SoundProfile,
    *,
    audition_mode: str = "draft",
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    settings = load_sound_settings()
    output_trim_db = _output_trim_db(profile, settings)
    apply_state, out_path, loaded = await _load_profile_config(
        profile,
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
        source="sound_audition",
        persist_profile=False,
        audition=True,
        output_trim_db=output_trim_db,
    )
    logger.info(
        "event=sound.audition mode=%s enabled=%s curve=%s bands=%d "
        "output_trim=%.1f room_peqs=%d config=%s op_id=%s",
        audition_mode,
        loaded.enabled,
        loaded.curve_id,
        len(loaded.parametric_bands),
        output_trim_db,
        apply_state.room_peq_count or 0,
        out_path,
        apply_state.op_id,
    )
    saved = load_profile(profile_path)
    payload = _state_payload(
        saved,
        library_path=library_path,
        include_library=library_path is not None,
    )
    payload.update(
        {
            "audition_profile": loaded.to_dict(),
            "audition_mode": audition_mode,
            "output_trim_db": output_trim_db,
            "active_config_path": str(out_path),
            "preserved_room_peqs": apply_state.room_peq_count or 0,
            "last_dsp_apply": apply_state.to_dict(),
            "dsp_write_epoch": apply_state.op_id,
        }
    )
    return payload


async def _live_draft_profile(
    profile: SoundProfile,
    *,
    expected_dsp_write_epoch: str,
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    """Load a bounded preference-EQ draft into the active Camilla config.

    This is the low-latency editing path: no profile persistence, no
    config-file pointer change, and no shared apply-state mutation. The
    durable Save/Apply path remains `_apply_profile`, which writes a
    validated YAML file and records rollback state.
    """
    from jasper.dsp_apply import dsp_write_epoch, dsp_writer_lock
    from jasper.sound.camilla_yaml import (
        BASE_CONFIG_PATH,
        emit_sound_config,
        extract_room_peqs_from_config,
        is_base_config,
        is_jts_generated_config,
    )

    cam = camilla_factory()
    config_path = Path(config_dir)
    settings = load_sound_settings()
    output_trim_db = _output_trim_db(profile, settings)
    sound_filter_count = len(build_sound_filters(profile))
    try:
        loader = getattr(cam, "set_active_config_raw")
    except AttributeError:
        loader = None

    def _live_payload(
        *,
        status: str,
        method: str,
        current_epoch: str,
        room_peq_count: int = 0,
        active_config_path: str | None = None,
    ) -> dict[str, Any]:
        payload = _state_payload(
            profile,
            library_path=library_path,
            include_library=library_path is not None,
        )
        payload.update(
            {
                "live_status": status,
                "live_method": method,
                "dsp_write_epoch": current_epoch,
                "active_config_path": active_config_path,
                "preserved_room_peqs": room_peq_count,
                "sound_filter_count": sound_filter_count,
            }
        )
        if status == "live":
            payload.update(
                {
                    "audition_mode": "draft",
                    "audition_profile": profile.to_dict(),
                }
            )
        return payload

    def _unavailable(
        reason: str,
        *,
        current_epoch: str,
        error: Exception | None = None,
    ) -> dict[str, Any]:
        _log_live_draft_unavailable(
            reason=reason,
            output_trim_db=output_trim_db,
            room_peq_count=0,
            sound_filter_count=sound_filter_count,
            error=error,
        )
        return _live_payload(
            status="unavailable",
            method=reason,
            current_epoch=current_epoch,
        )

    if loader is None:
        return _unavailable(
            "active_config_raw_unavailable",
            current_epoch=dsp_write_epoch(),
        )

    async with dsp_writer_lock(config_path):
        current_epoch = dsp_write_epoch()
        if expected_dsp_write_epoch != current_epoch:
            logger.info(
                "event=sound.live_draft result=stale expected_epoch=%s "
                "current_epoch=%s",
                expected_dsp_write_epoch,
                current_epoch,
            )
            return _live_payload(
                status="stale",
                method="skipped_stale_epoch",
                current_epoch=current_epoch,
            )

        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            raise RuntimeError("CamillaDSP did not report a loaded config path")

        if is_base_config(current_path):
            room_peqs = []
        elif is_jts_generated_config(current_path, config_dir=config_path):
            room_peqs = extract_room_peqs_from_config(current_path)
        else:
            raise RuntimeError(
                "CamillaDSP is running a custom config that JTS cannot safely "
                f"preserve ({current_path}). Reset to {BASE_CONFIG_PATH} or apply "
                "room correction before changing sound EQ."
            )

        yaml = emit_sound_config(
            profile,
            room_peqs=room_peqs,
            profile_id=f"live-{time.time_ns()}",
            output_trim_db=output_trim_db,
        )

        try:
            await loader(yaml, best_effort=False)
        except Exception as e:  # noqa: BLE001
            _log_live_draft_unavailable(
                reason="active_config_raw_failed",
                output_trim_db=output_trim_db,
                room_peq_count=len(room_peqs),
                sound_filter_count=sound_filter_count,
                error=e,
            )
            return _live_payload(
                status="unavailable",
                method="active_config_raw_failed",
                current_epoch=current_epoch,
                room_peq_count=len(room_peqs),
                active_config_path=current_path,
            )

        logger.info(
            "event=sound.live_draft result=live output_trim=%.1f "
            "room_peqs=%d sound_filters=%d active_anchor=%s epoch=%s",
            output_trim_db,
            len(room_peqs),
            sound_filter_count,
            current_path,
            current_epoch,
        )
        return _live_payload(
            status="live",
            method="active_config_raw",
            current_epoch=current_epoch,
            room_peq_count=len(room_peqs),
            active_config_path=current_path,
        )


async def _load_profile_config(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any],
    source: str,
    persist_profile: bool,
    audition: bool = False,
    output_trim_db: float = 0.0,
) -> tuple[Any, Path, SoundProfile]:
    from jasper.sound.camilla_yaml import (
        BASE_CONFIG_PATH,
        emit_sound_config,
        extract_room_peqs_from_config,
        is_base_config,
        is_jts_generated_config,
        sound_audition_config_path,
        sound_config_path,
    )
    from jasper.dsp_apply import apply_dsp_config

    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)
    profile_id = str(time.time_ns())
    out_path = (
        sound_audition_config_path(config_path)
        if audition
        else sound_config_path(config_path)
    )
    cam = camilla_factory()

    async def _prepare_config() -> dict[str, Any]:
        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            raise RuntimeError("CamillaDSP did not report a loaded config path")

        if is_base_config(current_path):
            room_peqs = []
        elif is_jts_generated_config(current_path, config_dir=config_path):
            room_peqs = extract_room_peqs_from_config(current_path)
        else:
            raise RuntimeError(
                "CamillaDSP is running a custom config that JTS cannot safely "
                f"preserve ({current_path}). Reset to {BASE_CONFIG_PATH} or apply "
                "room correction before changing sound EQ."
            )

        emit_sound_config(
            profile,
            room_peqs=room_peqs,
            out_path=out_path,
            profile_id=profile_id,
            output_trim_db=output_trim_db,
        )
        return {
            "prior_config_path": current_path,
            "room_peq_count": len(room_peqs),
            "sound_filter_count": len(build_sound_filters(profile)),
        }

    apply_state = await apply_dsp_config(
        source=source,
        candidate_path=out_path,
        prepare=_prepare_config,
        load_config=lambda path: cam.set_config_file_path(
            path,
            best_effort=False,
        ),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
        persist=(lambda: save_profile(profile, profile_path))
        if persist_profile
        else None,
        sound_filter_count=len(build_sound_filters(profile)),
    )
    return apply_state, out_path, profile


# Page-specific component CSS. Shared primitives (.page, .eyebrow,
# .segmented, .btn, .sr-only, tokens, fonts) live in /assets/app.css;
# only the sound-editor components live here. canonical_page() wraps
# this in a <style> tag.
_PAGE_CSS = """
  .app-header {
    position: sticky; top: 0; z-index: 30;
    background: color-mix(in oklab, var(--background) 92%, transparent);
    backdrop-filter: blur(8px);
  }
  .app-header__row {
    display: grid; grid-template-columns: 40px 1fr 40px; align-items: center;
    height: 56px; max-width: 48rem; margin: 0 auto; padding: 0 1.5rem;
  }
  .app-header__title {
    margin: 0; text-align: center;
    font-family: var(--font-display); font-size: 16px; font-weight: 600;
    letter-spacing: -0.01em;
  }
  .app-header__tabs { border-bottom: 1px solid var(--border); }
  .app-header__tabs > div { max-width: 48rem; margin: 0 auto; padding: 12px 1.5rem; }
  .icon-button {
    display: inline-flex; align-items: center; justify-content: center;
    width: 32px; height: 32px; border-radius: 9999px;
    color: var(--muted-foreground);
    box-shadow: inset 0 0 0 1px var(--border);
    background: color-mix(in oklab, var(--secondary) 60%, transparent);
    transition: background 150ms ease, color 150ms ease;
  }
  .icon-button:hover { color: var(--foreground); background: var(--surface-hover); }
  @media (min-width: 768px) {
    .app-header__row, .app-header__tabs > div { padding-left: 2.5rem; padding-right: 2.5rem; }
  }

  .row-between {
    display: flex; align-items: flex-end; justify-content: space-between;
    gap: 8px; padding: 0 4px; margin-bottom: 12px;
  }
  .now-playing { padding-bottom: 28px; }
  .now-playing__label {
    font-family: var(--font-display); font-size: 12px; font-weight: 500;
    color: var(--foreground);
    max-width: 62%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .graph-card {
    border-radius: var(--radius-md); padding: 12px;
    background: var(--foreground-005);
    box-shadow: inset 0 0 0 1px var(--border);
  }
  .eq-graph { display: block; width: 100%; height: auto; }
  .eq-graph text { fill: var(--muted-foreground); font-family: var(--font-display); font-size: 9px; }
  .eq-graph .grid { stroke: var(--foreground-010); stroke-width: 1; stroke-dasharray: 2 3; }
  .eq-graph .zero { stroke: var(--foreground-020); stroke-width: 1; }
  .eq-graph .component {
    fill: none; stroke: color-mix(in oklab, var(--muted-foreground) 55%, transparent);
    stroke-width: 1.5; stroke-dasharray: 4 4; opacity: 0.7;
  }
  .eq-graph .component.selected {
    stroke: var(--accent-strong); stroke-width: 2; stroke-dasharray: none; opacity: 0.95;
  }
  .eq-graph .area { fill: color-mix(in oklab, var(--accent) 14%, transparent); stroke: none; }
  .eq-graph .curve { fill: none; stroke: var(--primary); stroke-width: 2.5; stroke-linejoin: round; }
  .eq-graph.off .curve { stroke: var(--foreground-020); stroke-dasharray: 5 4; }
  .eq-graph .band-width { fill: var(--primary); opacity: 0.08; }
  .eq-graph .band-width.selected { opacity: 0.14; }
  .eq-graph .band-marker { stroke: var(--primary-040); stroke-width: 1.2; stroke-dasharray: 3 4; }
  .eq-graph .band-dot { fill: var(--background); stroke: var(--primary); stroke-width: 2; }
  .eq-graph .band-dot.selected { fill: var(--primary); }

  .btn-row { display: flex; flex-wrap: wrap; gap: 8px; }

  /* Off empty state */
  .off-card {
    border-radius: var(--radius-lg); padding: 24px; text-align: center;
    background: color-mix(in oklab, var(--secondary) 40%, transparent);
    box-shadow: inset 0 0 0 1px var(--border);
  }
  .off-card__icon {
    width: 48px; height: 48px; margin: 0 auto 16px;
    display: inline-flex; align-items: center; justify-content: center;
    border-radius: 9999px; color: var(--primary);
    background: var(--accent-faint);
  }
  .off-card__icon svg { width: 22px; height: 22px; }
  .off-card__text { max-width: 360px; margin: 0 auto; color: var(--muted-foreground); }
  .off-card .btn-row { justify-content: center; margin-top: 20px; }
  /* The off-card pair is a centered choice, not a primary/secondary
     footer row — opt out of the .btn-row first-child flex:1 below. */
  .off-card .btn-row .btn:first-child { flex: 0 1 auto; }

  /* Saved tab */
  .saved-stack { display: flex; flex-direction: column; gap: 24px; }
  /* Sound settings: match-loudness switch + advanced headroom */
  .sound-settings { display: flex; flex-direction: column; gap: 6px; }
  .setting-row {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 14px 16px; border-radius: 14px; background: var(--foreground-005);
    box-shadow: inset 0 0 0 1px var(--border);
  }
  .setting-row--stack { align-items: stretch; flex-direction: column; gap: 12px; }
  .setting-row__title { font-weight: 600; color: var(--foreground); }
  .setting-row__hint { font-size: 0.8rem; color: var(--foreground); opacity: 0.6; margin-top: 2px; }
  .advanced > summary {
    cursor: pointer; padding: 10px 16px; color: var(--primary);
    font-weight: 600; font-size: 0.9rem; list-style: none;
  }
  .advanced > summary::-webkit-details-marker { display: none; }
  .headroom-control { display: flex; align-items: center; gap: 12px; }
  .headroom-range { flex: 1; accent-color: var(--primary); }
  .headroom-readout {
    min-width: 4.5rem; text-align: right;
    font-variant-numeric: tabular-nums; color: var(--foreground);
  }
  /* Canonical checkbox toggle (sage). Checkbox-based per web conventions. */
  .toggle {
    position: relative; display: inline-block; flex-shrink: 0;
    width: 48px; height: 28px;
  }
  .toggle input { position: absolute; opacity: 0; width: 0; height: 0; }
  .toggle .track {
    position: absolute; inset: 0; background: var(--foreground-020);
    border-radius: 28px; cursor: pointer; transition: background-color 0.18s ease;
  }
  .toggle .track::before {
    position: absolute; content: ""; width: 22px; height: 22px; top: 3px; left: 3px;
    background: var(--background); border-radius: 50%;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25); transition: transform 0.18s ease;
  }
  .toggle input:checked + .track { background: var(--primary); }
  .toggle input:checked + .track::before { transform: translateX(20px); }
  .toggle input:focus-visible + .track { outline: 2px solid var(--primary); outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) {
    .toggle .track, .toggle .track::before { transition: none; }
  }
  .section-header {
    display: flex; align-items: flex-end; justify-content: space-between;
    padding: 0 4px; margin-bottom: 12px;
  }
  .text-button {
    font-family: var(--font-display); font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0;
    color: var(--primary); cursor: pointer;
    display: inline-flex; align-items: center; gap: 4px;
  }
  .text-button:hover { color: color-mix(in oklab, var(--primary) 80%, black); }
  .text-button--muted { color: var(--muted-foreground); }
  .text-button--muted:hover { color: var(--foreground); }
  .text-button svg { width: 12px; height: 12px; }
  .list-card {
    border-radius: var(--radius-lg); overflow: hidden;
    background: color-mix(in oklab, var(--secondary) 40%, transparent);
    box-shadow: inset 0 0 0 1px var(--border);
  }
  .list-card__rows > * + * { border-top: 1px solid var(--border); }
  .empty-card {
    border-radius: var(--radius-lg); padding: 24px 16px; text-align: center;
    background: color-mix(in oklab, var(--secondary) 40%, transparent);
    box-shadow: inset 0 0 0 1px var(--border);
  }
  .empty-card p { color: var(--muted-foreground); margin: 0; }
  .empty-card .btn { margin-top: 12px; }
  .profile-row { display: flex; align-items: center; gap: 12px; padding: 12px 16px; }
  .profile-row__select {
    flex: 1; display: flex; align-items: center; gap: 12px; text-align: left;
    background: none; cursor: pointer; min-width: 0;
  }
  .profile-row__dot {
    width: 8px; height: 8px; border-radius: 9999px; flex-shrink: 0;
    background: var(--foreground-020);
  }
  .profile-row__dot--on {
    background: var(--primary);
    box-shadow: 0 0 0 3px color-mix(in oklab, var(--primary) 30%, transparent);
  }
  .profile-row__name { margin: 0; font-size: 14px; font-weight: 500; }
  .profile-row__meta { margin: 2px 0 0; font-size: 12px; color: var(--muted-foreground); }
  .profile-row__actions { display: flex; gap: 4px; flex-shrink: 0; }
  .profile-row__action {
    display: inline-flex; align-items: center; justify-content: center;
    width: 36px; height: 36px; border-radius: var(--radius-md);
    color: var(--muted-foreground); cursor: pointer;
    transition: background 150ms ease, color 150ms ease;
  }
  .profile-row__action:hover { background: var(--foreground-005); color: var(--foreground); }
  .profile-row__action--danger:hover {
    background: color-mix(in oklab, var(--danger) 10%, transparent); color: var(--danger);
  }
  .profile-row__action svg { width: 16px; height: 16px; }

  /* Draft editor */
  .mode-toggle { padding-bottom: 16px; }
  .bands-section { padding-bottom: 28px; }
  .bands-section .row-between span {
    font-family: var(--font-display); font-size: 12px; font-weight: 500;
    color: var(--muted-foreground);
  }
  .bands-meta { display: flex; gap: 12px; align-items: center; }
  .bands-card {
    border-radius: var(--radius-lg);
    background: color-mix(in oklab, var(--secondary) 40%, transparent);
    box-shadow: inset 0 0 0 1px var(--border);
  }
  .bands-card--simple { padding: 20px 8px; }
  .bands-card__rows > * + * { border-top: 1px solid var(--border); }
  .add-band {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    width: 100%; padding: 14px 16px; cursor: pointer;
    font-family: var(--font-display); font-size: 12px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0; color: var(--primary);
  }
  .add-band:hover { background: var(--foreground-005); }
  .add-band:disabled { color: var(--muted-foreground); cursor: not-allowed; }
  .add-band svg { width: 14px; height: 14px; }

  .band-row { padding: 14px 16px; }
  .band-row__header {
    display: flex; width: 100%; align-items: center; justify-content: space-between;
    text-align: left; background: none; cursor: pointer;
  }
  .band-row__title { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .band-row__name { margin: 0; font-size: 14px; font-weight: 500; }
  .band-row__meta {
    margin: 2px 0 0; font-size: 12px; color: var(--muted-foreground);
  }
  .band-row__chev {
    width: 16px; height: 16px; flex-shrink: 0;
    color: color-mix(in oklab, var(--muted-foreground) 60%, transparent);
    transition: transform 150ms ease;
  }
  .band-row[data-open="true"] .band-row__chev { transform: rotate(180deg); }
  .band-row__body { margin-top: 16px; padding-left: 36px; display: flex; flex-direction: column; gap: 16px; }
  .band-row__delete {
    display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
    font-family: var(--font-display); font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0; color: var(--muted-foreground);
  }
  .band-row__delete:hover { color: var(--danger); }
  .band-row__delete svg { width: 12px; height: 12px; }

  .band-dot {
    display: inline-flex; align-items: center; justify-content: center;
    width: 24px; height: 24px; border-radius: 9999px; flex-shrink: 0;
    font-family: var(--font-display); font-size: 11px; font-weight: 600;
    background: var(--accent-faint); color: var(--primary);
    transition: all 150ms ease;
  }
  .band-dot--active {
    width: 28px; height: 28px; background: var(--primary); color: var(--primary-foreground);
    box-shadow: 0 0 0 3px color-mix(in oklab, var(--primary) 40%, transparent);
  }

  /* Range rows (PEQ) + vertical ranges (Simple) */
  .range-row { display: flex; align-items: center; gap: 12px; }
  .range-row__label {
    width: 52px; flex-shrink: 0;
    font-family: var(--font-display); font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0; color: var(--muted-foreground);
  }
  .range { position: relative; height: 36px; flex: 1; }
  .range__track {
    position: absolute; inset-inline: 0; top: 50%; height: 4px;
    transform: translateY(-50%); border-radius: 9999px; background: var(--foreground-010);
  }
  .range__thumb {
    position: absolute; top: 50%; width: 12px; height: 28px; border-radius: 9999px;
    background: var(--primary); transform: translateY(-50%); pointer-events: none;
    box-shadow: 0 1px 2px rgb(0 0 0 / 0.08), inset 0 0 0 1px var(--border);
  }
  .range__fill-track {
    position: absolute; inset: 0; border-radius: var(--radius-sm); overflow: hidden;
    background: var(--foreground-005); box-shadow: inset 0 0 0 1px var(--border);
  }
  .range__fill { position: absolute; inset-block: 0; left: 0; background: var(--accent); }
  .range__input {
    position: absolute; inset: 0; width: 100%; height: 100%; margin: 0;
    appearance: none; -webkit-appearance: none; background: transparent;
    cursor: pointer; opacity: 0;
  }
  .range__readout { width: 78px; flex-shrink: 0; text-align: right; }
  .range__readout-btn, .range__readout-input {
    width: 100%; padding: 2px 4px; border-radius: 4px; text-align: right;
    font-family: var(--font-display); font-size: 12px; font-weight: 500;
  }
  .range__readout-btn { cursor: text; color: var(--foreground); }
  .range__readout-btn:hover { background: var(--foreground-005); }
  .range__readout-input {
    background: var(--foreground-005); box-shadow: inset 0 0 0 1px var(--primary);
    outline: none; appearance: textfield; -moz-appearance: textfield;
  }
  .range__readout-input::-webkit-outer-spin-button,
  .range__readout-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }

  .simple-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
  .simple-col { display: flex; flex-direction: column; align-items: center; gap: 8px; }
  .simple-col__readout { height: 24px; }
  .simple-col__readout-btn, .simple-col__readout-input {
    width: 56px; padding: 2px 4px; border-radius: 4px; text-align: center;
    font-family: var(--font-display); font-size: 12px; font-weight: 500;
  }
  .simple-col__readout-btn { cursor: text; color: var(--foreground); }
  .simple-col__readout-btn:hover { background: var(--foreground-005); }
  .simple-col__readout-input {
    background: var(--foreground-005); box-shadow: inset 0 0 0 1px var(--primary);
    outline: none; appearance: textfield; -moz-appearance: textfield;
  }
  .simple-col__readout-input::-webkit-outer-spin-button,
  .simple-col__readout-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
  .vrange { position: relative; width: 36px; height: 168px; }
  .vrange__track {
    position: absolute; inset-block: 0; left: 50%; width: 4px;
    transform: translateX(-50%); border-radius: 9999px; background: var(--foreground-010);
  }
  .vrange__zero {
    position: absolute; left: 50%; top: 50%; width: 16px; height: 1px;
    transform: translate(-50%, -50%); background: var(--foreground-020);
  }
  .vrange__thumb {
    position: absolute; left: 50%; width: 28px; height: 12px; border-radius: 9999px;
    background: var(--primary); transform: translateX(-50%); pointer-events: none;
    box-shadow: 0 1px 2px rgb(0 0 0 / 0.08), inset 0 0 0 1px var(--border);
  }
  .vrange__input {
    position: absolute; inset: 0; width: 100%; height: 100%; margin: 0;
    appearance: none; -webkit-appearance: none; background: transparent;
    cursor: pointer; opacity: 0; writing-mode: vertical-lr; direction: rtl;
  }
  .simple-col__caption { text-align: center; }
  .simple-col__caption p { margin: 0; }
  .simple-col__caption p:first-child { font-size: 11px; font-weight: 500; }
  .simple-col__caption p:last-child { margin-top: 2px; font-size: 10px; color: var(--muted-foreground); }

  /* Footer + naming */
  .draft-footer { display: flex; flex-direction: column; gap: 12px; }
  .btn-row .btn:first-child { flex: 1; }
  .naming-card {
    display: flex; flex-direction: column; gap: 8px; padding: 12px;
    border-radius: var(--radius-md); background: var(--foreground-005);
    box-shadow: inset 0 0 0 1px var(--border);
  }
  .naming-card input {
    padding: 10px 12px; border-radius: var(--radius-md); background: var(--background);
    box-shadow: inset 0 0 0 1px var(--border); font-size: 14px; font-weight: 500; outline: none;
  }
  .naming-card input:focus { box-shadow: inset 0 0 0 1px var(--primary); }

  .status-line { min-height: 1.3em; margin-top: 16px; padding: 0 4px; font-size: 12px; color: var(--muted-foreground); }
  .status-line.err { color: var(--danger); }
  .meta-row { display: flex; gap: 16px; margin-top: 8px; padding: 0 4px; font-size: 11px; color: var(--muted-foreground); }
"""


_SOUND_JS = r"""
(function() {
  var LIMIT_DEFAULTS = {
    simple_gain_db: 12, advanced_gain_db: 12, max_parametric_bands: 8,
    min_freq_hz: 20, max_freq_hz: 20000, min_q: 0.2, max_q: 10,
    simple_bands: [], headroom_trim_max_db: 12
  };
  var FLAT = function() {
    return {enabled: true, curve_id: 'flat',
            simple_eq: zeroSimple(), parametric_bands: [],
            profile_id: '', profile_name: ''};
  };

  // Declared before FLAT() is first called below — zeroSimple() reads them.
  var simpleBands = [];        // [{key,field,label,freq_hz,type}] from /state
  var limits = Object.assign({}, LIMIT_DEFAULTS);

  var view = 'off';            // off | saved | draft
  var mode = 'simple';         // simple | peq
  var selectedId = null;       // selected library id on the Saved tab
  var draft = FLAT();          // working profile in the Draft tab
  var editing = {kind: 'new'}; // new | {kind:'user',id,name} | {kind:'preset',id,name}
  var activeBand = 0;
  var allCollapsed = false;
  var naming = false;
  var nameMode = 'save';       // 'save' (new/copy) | 'rename'
  var nameDraft = '';

  var applied = FLAT();        // persisted profile
  var library = [];            // [{id,name,kind,editable,description,profile,...}]
  var soundSettings = {headroom_trim_db: 0, match_loudness: false};  // global output settings
  var curvesById = {};
  var dspWriteEpoch = 'none';
  var applying = false;
  var previewTimer = null, previewSeq = 0;
  var liveTimer = null, liveSeq = 0, liveInFlight = false, livePending = false;
  var statusText = '', statusErr = false;

  function el(id) { return document.getElementById(id); }
  __CSRF_HELPERS__
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, Number(v) || 0)); }
  function clone(o) { return JSON.parse(JSON.stringify(o || {})); }
  function fmtDb(v) { v = Number(v) || 0; return (v > 0 ? '+' : '') + v.toFixed(1); }
  function fmtFreq(v) {
    v = Number(v) || 0;
    return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + ' kHz' : Math.round(v) + ' Hz';
  }
  function fmtFreqShort(v) {
    v = Number(v) || 0;
    return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'k' : String(Math.round(v));
  }
  function fmtQ(v) { return 'Q ' + (Number(v) || 0).toFixed(1); }
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function(ch) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch];
    });
  }
  function zeroSimple() {
    var out = {};
    (simpleBands.length ? simpleBands : LIMIT_DEFAULTS.simple_bands).forEach(function(b) {
      out[b.field] = 0;
    });
    if (!simpleBands.length) {
      ['sub_bass_db', 'bass_db', 'mid_db', 'presence_db', 'treble_db'].forEach(function(f) {
        if (!(f in out)) out[f] = 0;
      });
    }
    return out;
  }
  function ico(name, cls) {
    return '<svg class="' + (cls || 'ico') + '" aria-hidden="true"><use href="#icon-' + name + '"></use></svg>';
  }
  function status(msg, isErr) {
    statusText = msg || '';
    statusErr = !!isErr;
    var node = el('status');
    if (node) {
      node.textContent = statusText;
      node.className = 'status-line' + (statusErr ? ' err' : '');
    }
  }

  // ---- profile helpers ------------------------------------------------
  function normalizeProfile(raw) {
    raw = raw || {};
    var simple = raw.simple_eq || {};
    var normSimple = {};
    var bands = simpleBands.length ? simpleBands : [
      {field: 'sub_bass_db'}, {field: 'bass_db'}, {field: 'mid_db'},
      {field: 'presence_db'}, {field: 'treble_db'}
    ];
    bands.forEach(function(b) { normSimple[b.field] = Number(simple[b.field] || 0); });
    return {
      enabled: raw.enabled !== false,
      curve_id: raw.curve_id || 'flat',
      simple_eq: normSimple,
      parametric_bands: (raw.parametric_bands || []).map(function(b) {
        return {
          enabled: b.enabled !== false,
          type: b.type || b.biquad_type || 'Peaking',
          freq_hz: Number(b.freq_hz || b.freq || 1000),
          gain_db: Number(b.gain_db || b.gain || 0),
          q: Number(b.q || 1)
        };
      }),
      profile_id: raw.profile_id || '',
      profile_name: raw.profile_name || ''
    };
  }
  function profileKey(profile) {
    profile = normalizeProfile(profile);
    return JSON.stringify({
      enabled: profile.enabled, curve_id: profile.curve_id,
      simple_eq: profile.simple_eq, parametric_bands: profile.parametric_bands
    });
  }
  function entryById(id) {
    return library.find(function(e) { return e.id === id; }) || null;
  }
  function userEntries() { return library.filter(function(e) { return e.kind === 'custom'; }); }
  function presetEntries() { return library.filter(function(e) { return e.kind === 'stock'; }); }
  function findIdFor(profile) {
    profile = normalizeProfile(profile);
    if (profile.profile_id && entryById(profile.profile_id)) return profile.profile_id;
    var key = profileKey(profile);
    var stock = library.find(function(e) { return e.kind === 'stock' && profileKey(e.profile) === key; });
    if (stock) return stock.id;
    var custom = library.find(function(e) { return e.kind === 'custom' && profileKey(e.profile) === key; });
    if (custom) return custom.id;
    return 'stock:' + (profile.curve_id || 'flat');
  }
  // The profile the editor sources from (for the modified/dirty check).
  function sourceProfile() {
    if (editing.kind === 'new') return FLAT();
    var entry = entryById(editing.id);
    return entry ? normalizeProfile(entry.profile) : FLAT();
  }
  function draftModified() {
    return profileKey(draft) !== profileKey(sourceProfile());
  }
  function withIdentity(profile, id, name) {
    profile = clone(profile);
    profile.profile_id = id || '';
    profile.profile_name = name || '';
    return profile;
  }
  // The profile currently driving the speaker per the active tab.
  function liveProfile() {
    if (view === 'off') return null;
    if (view === 'saved') {
      var entry = entryById(selectedId);
      return entry ? normalizeProfile(entry.profile) : null;
    }
    return draft;
  }
  function liveLabel() {
    if (view === 'off') return 'Bypass';
    if (view === 'saved') {
      var entry = entryById(selectedId);
      return entry ? entry.name : 'No profile selected';
    }
    if (editing.kind === 'new') return 'New profile' + (draftModified() ? ' · edited' : '');
    var lead = editing.kind === 'preset' ? 'From preset: ' : 'Editing: ';
    return lead + editing.name + (draftModified() ? ' · edited' : '');
  }

  // ---- preview math ---------------------------------------------------
  // Optimistic client mirror of jasper/sound/profile.py's response math,
  // for instant graph feedback before /preview returns (and for graphing a
  // saved profile without a round-trip). Both sides are deliberately
  // illustrative approximations; CamillaDSP owns the real biquads, and the
  // authoritative /preview payload overwrites this within ~90 ms. Keep the
  // two shelf/peak formulas in sync.
  function previewFreqs() {
    var out = [];
    for (var i = 0; i <= 120; i += 1) {
      out.push(limits.min_freq_hz * Math.pow(limits.max_freq_hz / limits.min_freq_hz, i / 120));
    }
    return out;
  }
  function specActive(s) { return Math.abs(Number(s.gain_db || 0)) >= 0.05; }
  function responseDb(spec, freq) {
    var f = Math.max(Number(freq) || 0, 1e-6);
    var c = Math.max(Number(spec.freq_hz || spec.freq || 1000), 1e-6);
    var gain = Number(spec.gain_db || 0);
    var type = spec.type || spec.biquad_type || 'Peaking';
    var x = Math.log(f / c) / Math.log(2);
    if (type === 'Lowshelf') return gain / (1 + Math.exp(3 * x));
    if (type === 'Highshelf') return gain / (1 + Math.exp(-3 * x));
    var q = Math.max(Number(spec.q || 1), 1e-3);
    return gain / (1 + Math.pow(x / (1 / q), 2));
  }
  function curveSpecs(profile) { return (curvesById[profile.curve_id] || {}).filters || []; }
  function simpleSpecs(profile) {
    var simple = profile.simple_eq || {};
    return (simpleBands.length ? simpleBands : []).map(function(b) {
      return {type: b.type, freq_hz: b.freq_hz, gain_db: simple[b.field] || 0,
              q: b.type === 'Peaking' ? 1.0 : undefined};
    });
  }
  function advancedSpecs(profile) {
    return (profile.parametric_bands || []).filter(function(b) { return b && b.enabled !== false; })
      .map(function(b) { return {type: b.type, freq_hz: b.freq_hz, gain_db: b.gain_db, q: b.q}; });
  }
  function pointsFor(specs, freqs, emptyWhenFlat) {
    specs = specs || [];
    if (emptyWhenFlat && !specs.some(specActive)) return [];
    return freqs.map(function(f) {
      var db = specs.reduce(function(sum, s) { return specActive(s) ? sum + responseDb(s, f) : sum; }, 0);
      return {freq_hz: f, db: db};
    });
  }
  function previewPayload(profile) {
    profile = normalizeProfile(profile);
    var freqs = previewFreqs();
    if (profile.enabled === false) {
      return {preview: [], components: {curve: [], simple: [], advanced: []}, off: true};
    }
    var all = curveSpecs(profile).concat(simpleSpecs(profile), advancedSpecs(profile));
    var preview = pointsFor(all, freqs, false);
    return {
      preview: preview,
      components: {
        curve: pointsFor(curveSpecs(profile), freqs, true),
        simple: pointsFor(simpleSpecs(profile), freqs, true),
        advanced: (profile.parametric_bands || []).map(function(b, i) {
          return {index: i, enabled: b.enabled !== false,
                  preview: pointsFor(advancedSpecs({parametric_bands: [b]}), freqs, true)};
        })
      }
    };
  }

  // ---- graph rendering ------------------------------------------------
  var W = 620, H = 200, padL = 38, padR = 12, padT = 12, padB = 26;
  var MINDB = -12, MAXDB = 12, MINF = Math.log10(20), MAXF = Math.log10(20000);
  function gx(f) { return padL + (Math.log10(f) - MINF) / (MAXF - MINF) * (W - padL - padR); }
  function gy(db) { return padT + (MAXDB - db) / (MAXDB - MINDB) * (H - padT - padB); }
  function pathD(points) {
    var c = points.map(function(p) { return [gx(p.freq_hz), gy(clamp(p.db, MINDB, MAXDB))]; });
    var d = 'M' + c[0][0].toFixed(1) + ' ' + c[0][1].toFixed(1);
    for (var i = 1; i < c.length; i += 1) d += ' L' + c[i][0].toFixed(1) + ' ' + c[i][1].toFixed(1);
    return d;
  }
  function drawPath(points, cls) {
    if (!points || !points.length) return '';
    return '<path class="' + cls + '" d="' + pathD(points) + '"></path>';
  }
  function drawArea(points) {
    if (!points || !points.length) return '';
    return '<path class="area" d="' + pathD(points) +
      ' L' + gx(20000).toFixed(1) + ' ' + gy(MINDB).toFixed(1) +
      ' L' + gx(20).toFixed(1) + ' ' + gy(MINDB).toFixed(1) + ' Z"></path>';
  }
  function drawBandMarkers() {
    if (view !== 'draft' || mode !== 'peq') return '';
    var html = '';
    (draft.parametric_bands || []).forEach(function(b, i) {
      if (!b || b.enabled === false) return;
      var sel = i === activeBand ? ' selected' : '';
      var cx = gx(clamp(b.freq_hz, 20, 20000)), cy = gy(clamp(b.gain_db, MINDB, MAXDB));
      if ((b.type || 'Peaking') === 'Peaking') {
        var q = Math.max(Number(b.q || 1), 0.2);
        var lo = gx(clamp(b.freq_hz / Math.pow(2, 1 / q), 20, 20000));
        var hi = gx(clamp(b.freq_hz * Math.pow(2, 1 / q), 20, 20000));
        html += '<rect class="band-width' + sel + '" x="' + Math.min(lo, hi).toFixed(1) +
                '" y="' + padT + '" width="' + Math.abs(hi - lo).toFixed(1) +
                '" height="' + (H - padB - padT) + '"></rect>';
      }
      html += '<line class="band-marker" x1="' + cx.toFixed(1) + '" x2="' + cx.toFixed(1) +
              '" y1="' + padT + '" y2="' + (H - padB) + '"></line>';
      html += '<circle class="band-dot' + sel + '" cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
              '" r="' + (sel ? 4.5 : 3.5) + '"></circle>';
    });
    return html;
  }
  function renderGraph(payload, enabled) {
    var svg = el('plot');
    if (!svg) return;
    svg.classList.toggle('off', !enabled);
    var html = '';
    [-6, 0, 6].forEach(function(db) {
      html += '<line class="' + (db === 0 ? 'zero' : 'grid') + '" x1="' + padL + '" x2="' + (W - padR) +
              '" y1="' + gy(db).toFixed(1) + '" y2="' + gy(db).toFixed(1) + '"></line>';
      html += '<text x="6" y="' + (gy(db) + 3).toFixed(1) + '">' + fmtDb(db) + '</text>';
    });
    [20, 100, 1000, 10000, 20000].forEach(function(f) {
      html += '<line class="grid" y1="' + padT + '" y2="' + (H - padB) + '" x1="' + gx(f).toFixed(1) +
              '" x2="' + gx(f).toFixed(1) + '"></line>';
      html += '<text text-anchor="middle" x="' + gx(f).toFixed(1) + '" y="' + (H - 8) + '">' +
              (f >= 1000 ? (f / 1000) + 'k' : f) + '</text>';
    });
    var comp = payload.components || {};
    if (enabled) {
      html += drawArea(payload.preview || []);
      html += drawPath(comp.curve || [], 'component');
      html += drawPath(comp.simple || [], 'component');
      (comp.advanced || []).forEach(function(item) {
        html += drawPath(item.preview || [],
          (view === 'draft' && mode === 'peq' && item.index === activeBand) ? 'component selected' : 'component');
      });
    }
    var curvePts = enabled
      ? (payload.preview || [])
      : [{freq_hz: 20, db: 0}, {freq_hz: 20000, db: 0}];
    html += drawPath(curvePts, 'curve');
    html += drawBandMarkers();
    svg.innerHTML = html;
    var peak = (payload.preview || []).reduce(function(m, p) { return Math.max(m, p.db); }, 0);
    var summary = el('plot-summary');
    if (summary) {
      summary.textContent = enabled
        ? 'EQ response preview. Peak boost ' + fmtDb(peak) + ' dB.'
        : 'EQ bypassed. Flat response.';
    }
  }
  // Render the graph for whatever is the live source right now.
  function renderLiveGraph() {
    var profile = liveProfile();
    el('live-label').textContent = liveLabel();
    if (!profile) { renderGraph({preview: [], components: {}}, false); return; }
    renderGraph(previewPayload(profile), profile.enabled !== false);
  }

  // ---- view rendering -------------------------------------------------
  function renderTabs() {
    ['off', 'saved', 'draft'].forEach(function(v) {
      var btn = el('tab-' + v);
      btn.setAttribute('aria-pressed', v === view ? 'true' : 'false');
    });
  }
  function render() {
    renderTabs();
    renderLiveGraph();
    if (view === 'off') renderOff();
    else if (view === 'saved') renderSaved();
    else renderDraft();
    status(statusText, statusErr);
  }

  function renderOff() {
    el('view-body').innerHTML =
      '<section class="off-card">' +
        '<div class="off-card__icon">' + ico('spark') + '</div>' +
        '<p class="off-card__text">Create a sound profile that changes how your speaker sounds.</p>' +
        '<div class="btn-row">' +
          '<button type="button" class="btn btn--ghost" data-act="browse-presets">Try a stock profile</button>' +
          '<button type="button" class="btn btn--primary" data-act="new-draft">Create custom profile</button>' +
        '</div>' +
      '</section>';
  }

  function profileRow(entry, live, deletable) {
    return '<div class="profile-row">' +
      '<button type="button" class="profile-row__select" data-act="select" data-id="' + escapeHtml(entry.id) + '">' +
        '<span class="profile-row__dot' + (live ? ' profile-row__dot--on' : '') + '"></span>' +
        '<span style="min-width:0">' +
          '<p class="profile-row__name">' + escapeHtml(entry.name) + '</p>' +
          '<p class="profile-row__meta">' + (live ? 'Now playing · ' : '') +
            bandCountLabel(entry.profile) + '</p>' +
        '</span>' +
      '</button>' +
      '<span class="profile-row__actions">' +
        '<button type="button" class="profile-row__action" data-act="edit" data-id="' + escapeHtml(entry.id) +
          '" aria-label="Edit ' + escapeHtml(entry.name) + '">' + ico('pencil') + '</button>' +
        (deletable ? '<button type="button" class="profile-row__action profile-row__action--danger" data-act="delete" data-id="' +
          escapeHtml(entry.id) + '" aria-label="Delete ' + escapeHtml(entry.name) + '">' + ico('trash') + '</button>' : '') +
      '</span>' +
    '</div>';
  }
  function bandCountLabel(profile) {
    profile = normalizeProfile(profile);
    var n = 0;
    Object.keys(profile.simple_eq).forEach(function(k) { if (Math.abs(profile.simple_eq[k]) >= 0.05) n += 1; });
    n += profile.parametric_bands.filter(function(b) { return b.enabled !== false && Math.abs(b.gain_db) >= 0.05; }).length;
    if (profile.curve_id && profile.curve_id !== 'flat') n += 1;
    return n === 0 ? 'Flat' : n + ' band' + (n === 1 ? '' : 's');
  }
  function renderSaved() {
    var users = userEntries(), presets = presetEntries();
    var userSection = '<section><div class="section-header">' +
      '<h2 class="eyebrow">Your profiles</h2>' +
      '<button type="button" class="text-button" data-act="new-draft">' + ico('plus') + 'New</button></div>' +
      (users.length
        ? '<div class="list-card"><div class="list-card__rows">' +
            users.map(function(e) { return profileRow(e, e.id === selectedId, true); }).join('') + '</div></div>'
        : '<div class="empty-card"><p>No profiles yet.</p>' +
            '<button type="button" class="btn btn--primary" data-act="new-draft">Create your first</button></div>') +
      '</section>';
    var presetSection = '<section><div class="section-header"><h2 class="eyebrow">Presets</h2></div>' +
      '<div class="list-card"><div class="list-card__rows">' +
        presets.map(function(e) { return profileRow(e, e.id === selectedId, false); }).join('') + '</div></div></section>';
    el('view-body').innerHTML = '<div class="saved-stack">' + userSection + presetSection +
      renderSoundSettings() + '</div>';
  }
  function fmtTrim(v) { v = Number(v) || 0; return v > 0 ? '−' + v.toFixed(1) + ' dB' : 'Off'; }
  function renderSoundSettings() {
    var ml = soundSettings.match_loudness ? ' checked' : '';
    var trim = Number(soundSettings.headroom_trim_db) || 0;
    var trimMax = Number(limits.headroom_trim_max_db) || 12;  // backend clamps authoritatively
    return '<section class="sound-settings">' +
      '<div class="setting-row">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Match loudness</p>' +
          '<p class="setting-row__hint">Level-match profiles so switching compares tone, not volume.</p>' +
        '</div>' +
        '<label class="toggle"><input type="checkbox" id="set-match-loudness"' + ml +
          ' aria-label="Match loudness"><span class="track"></span></label>' +
      '</div>' +
      '<details class="advanced"' + (trim > 0 ? ' open' : '') + '>' +
        '<summary>Advanced</summary>' +
        '<div class="setting-row setting-row--stack">' +
          '<div class="setting-row__text">' +
            '<p class="setting-row__title">Extra headroom</p>' +
            '<p class="setting-row__hint">Digital attenuation for full-volume setups into your own amp. ' +
              'Leave at Off unless you hear clipping.</p>' +
          '</div>' +
          '<div class="headroom-control">' +
            '<input type="range" class="headroom-range" id="set-headroom" min="0" max="' + trimMax +
              '" step="0.5" value="' + trim + '" aria-label="Extra headroom in dB">' +
            '<span class="headroom-readout" id="set-headroom-readout">' + fmtTrim(trim) + '</span>' +
          '</div>' +
        '</div>' +
      '</details>' +
    '</section>';
  }

  function rangeRow(label, value, min, max, opts) {
    opts = opts || {};
    var pct, thumb;
    if (opts.log) {
      var lmin = Math.log(min), lmax = Math.log(max);
      pct = (Math.log(clamp(value, min, max)) - lmin) / (lmax - lmin) * 100;
    } else {
      pct = (clamp(value, min, max) - min) / (max - min) * 100;
    }
    if (opts.variant === 'thumb') {
      thumb = '<div class="range__track"></div><div class="range__thumb" style="left:calc(' + pct + '% - 6px)"></div>';
    } else {
      thumb = '<div class="range__fill-track"><div class="range__fill" style="width:' + pct + '%"></div></div>';
    }
    return '<div class="range-row">' +
      '<span class="range-row__label">' + escapeHtml(label) + '</span>' +
      '<div class="range">' + thumb +
        '<input type="range" class="range__input" min="' + (opts.log ? 0 : min) + '" max="' + (opts.log ? 1000 : max) +
          '" step="' + (opts.step || 0.1) + '" value="' + (opts.log ? freqToSlider(value, min, max) : value) +
          '" data-range="' + opts.kind + '" aria-label="' + escapeHtml(label) + '"></div>' +
      '<div class="range__readout"><button type="button" class="range__readout-btn" data-readout="' + opts.kind + '">' +
        escapeHtml(opts.format(value)) + '</button></div>' +
    '</div>';
  }
  function freqToSlider(freq, min, max) {
    var lmin = Math.log(min), lmax = Math.log(max);
    return Math.round((Math.log(clamp(freq, min, max)) - lmin) / (lmax - lmin) * 1000);
  }
  function sliderToFreq(pos, min, max) {
    var lmin = Math.log(min), lmax = Math.log(max);
    return Math.exp(lmin + clamp(pos, 0, 1000) / 1000 * (lmax - lmin));
  }
  function bandRow(band, index) {
    var open = !allCollapsed && index === activeBand;
    var body = '';
    if (open) {
      body = '<div class="band-row__body">' +
        '<div class="range-row"><span class="range-row__label">Type</span>' +
          '<div class="segmented" data-band="' + index + '">' +
            typeBtn('Lowshelf', 'Low', band.type) + typeBtn('Peaking', 'Peak', band.type) +
            typeBtn('Highshelf', 'High', band.type) + '</div></div>' +
        rangeRow('Freq', band.freq_hz, limits.min_freq_hz, limits.max_freq_hz,
          {kind: 'freq', log: true, variant: 'thumb', step: 1, format: function(v) { return fmtFreq(v); }}) +
        rangeRow('Gain', band.gain_db, -limits.advanced_gain_db, limits.advanced_gain_db,
          {kind: 'gain', step: 0.1, format: function(v) { return fmtDb(v) + ' dB'; }}) +
        rangeRow('Width', band.q, limits.min_q, limits.max_q,
          {kind: 'q', step: 0.1, format: function(v) { return fmtQ(v); }}) +
        '<button type="button" class="band-row__delete" data-act="del-band" data-index="' + index + '">' +
          ico('trash') + 'Delete band</button>' +
      '</div>';
    }
    return '<div class="band-row" data-index="' + index + '" data-open="' + (open ? 'true' : 'false') + '">' +
      '<button type="button" class="band-row__header" data-act="toggle-band" data-index="' + index + '">' +
        '<span class="band-row__title">' +
          '<span class="band-dot' + (open ? ' band-dot--active' : '') + '">' + (index + 1) + '</span>' +
          '<span><p class="band-row__name">Band ' + (index + 1) + '</p>' +
            '<p class="band-row__meta">' + escapeHtml(band.type) + ' · ' + Math.round(band.freq_hz) +
            ' Hz · ' + band.gain_db.toFixed(1) + ' dB · Q ' + band.q.toFixed(1) + '</p></span>' +
        '</span>' + ico('chevron', 'band-row__chev') +
      '</button>' + body + '</div>';
  }
  function typeBtn(value, label, current) {
    return '<button type="button" class="segmented__btn" data-band-type="' + value + '" aria-pressed="' +
      (current === value ? 'true' : 'false') + '">' + label + '</button>';
  }
  function simpleColumn(slot, value) {
    var min = -limits.simple_gain_db, max = limits.simple_gain_db;
    var pct = (clamp(value, min, max) - min) / (max - min) * 100;
    return '<div class="simple-col" data-field="' + escapeHtml(slot.field) + '">' +
      '<div class="simple-col__readout"><button type="button" class="simple-col__readout-btn" data-readout-field="' +
        escapeHtml(slot.field) + '">' + fmtDb(value) + '</button></div>' +
      '<div class="vrange"><div class="vrange__track"></div><div class="vrange__zero"></div>' +
        '<div class="vrange__thumb" style="bottom:calc(' + pct + '% - 6px)"></div>' +
        '<input type="range" class="vrange__input" min="' + min + '" max="' + max + '" step="0.1" value="' + value +
          '" data-field="' + escapeHtml(slot.field) + '" aria-label="' + escapeHtml(slot.label) + ' gain"></div>' +
      '<div class="band-dot">' + (slot.idx + 1) + '</div>' +
      '<div class="simple-col__caption"><p>' + escapeHtml(slot.label) + '</p><p>' + fmtFreqShort(slot.freq_hz) + ' Hz</p></div>' +
    '</div>';
  }
  function renderDraft() {
    var modeSection = '<section class="mode-toggle"><div class="section-header"><h2 class="eyebrow">Mode</h2></div>' +
      '<div class="segmented" id="mode-tabs">' +
        '<button type="button" class="segmented__btn" data-mode="simple" aria-pressed="' + (mode === 'simple' ? 'true' : 'false') + '">Simple</button>' +
        '<button type="button" class="segmented__btn" data-mode="peq" aria-pressed="' + (mode === 'peq' ? 'true' : 'false') + '">PEQ</button>' +
      '</div></section>';

    var bandsContent;
    if (mode === 'simple') {
      var cols = (simpleBands.length ? simpleBands : []).map(function(slot, i) {
        return simpleColumn(Object.assign({idx: i}, slot), draft.simple_eq[slot.field] || 0);
      }).join('');
      bandsContent = '<div class="bands-card bands-card--simple"><div class="simple-grid">' + cols + '</div></div>';
    } else {
      var rows = (draft.parametric_bands || []).map(bandRow).join('');
      bandsContent = '<div class="bands-card"><div class="bands-card__rows">' + rows +
        '<button type="button" class="add-band" data-act="add-band"' +
        (draft.parametric_bands.length >= limits.max_parametric_bands ? ' disabled' : '') + '>' +
        ico('plus') + 'Add band</button></div></div>';
    }
    var activeCount = mode === 'simple'
      ? Object.keys(draft.simple_eq).filter(function(k) { return Math.abs(draft.simple_eq[k]) >= 0.05; }).length
      : draft.parametric_bands.filter(function(b) { return b.enabled !== false; }).length;
    var bandsSection = '<section class="bands-section"><div class="row-between">' +
      '<h2 class="eyebrow">Bands</h2>' +
      '<div class="bands-meta"><span id="active-count">' + activeCount + ' active</span>' +
      (mode === 'peq' ? '<button type="button" class="text-button text-button--muted" data-act="toggle-collapse">' +
        (allCollapsed ? 'Expand all' : 'Collapse all') + '</button>' : '') +
      '</div></div>' + bandsContent + '</section>';

    el('view-body').innerHTML = '<div>' + modeSection + bandsSection +
      '<section class="draft-footer">' + footerHtml() + '</section></div>';
  }
  function footerHtml() {
    if (naming) {
      var isRename = nameMode === 'rename';
      return '<div class="naming-card">' +
        '<label class="eyebrow">' + (isRename ? 'Rename profile' : 'Name your profile') + '</label>' +
        '<input type="text" id="name-input" maxlength="48" autocomplete="off" value="' + escapeHtml(nameDraft) + '">' +
        '<div class="btn-row">' +
          '<button type="button" class="btn btn--primary" data-act="finalize-name">' +
            (isRename ? 'Rename' : 'Save profile') + '</button>' +
          '<button type="button" class="btn btn--ghost" data-act="cancel-name">Cancel</button>' +
        '</div></div>';
    }
    var dirty = draftModified();
    if (editing.kind === 'user') {
      return '<div class="btn-row">' +
          '<button type="button" class="btn btn--primary" data-act="overwrite"' + (dirty ? '' : ' disabled') + '>Overwrite</button>' +
          '<button type="button" class="btn btn--ghost" data-act="begin-name">Save as new</button></div>' +
        '<div class="btn-row">' +
          '<button type="button" class="btn btn--ghost" data-act="begin-rename">Rename</button>' +
          '<button type="button" class="btn btn--ghost" data-act="discard"' +
            (dirty ? '' : ' disabled') + '>Discard edits</button></div>';
    }
    if (editing.kind === 'preset') {
      return '<div class="btn-row">' +
          '<button type="button" class="btn btn--primary" data-act="begin-name">Save as new</button>' +
          '<button type="button" class="btn btn--ghost" data-act="discard"' + (dirty ? '' : ' disabled') + '>Discard edits</button></div>';
    }
    return '<div class="btn-row">' +
        '<button type="button" class="btn btn--primary" data-act="begin-name">Save profile</button>' +
        '<button type="button" class="btn btn--ghost" data-act="discard"' + (dirty ? '' : ' disabled') + '>Discard</button></div>';
  }

  // ---- backend integration -------------------------------------------
  function schedulePreview() {
    renderLiveGraph();          // optimistic local graph
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(preview, 90);
  }
  async function preview() {
    var seq = ++previewSeq;
    try {
      var resp = await fetch('./preview', {method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify(liveProfile() || Object.assign(FLAT(), {enabled: false}))});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'preview failed');
      if (seq !== previewSeq) return;
      var profile = liveProfile();
      renderGraph(payload, profile ? profile.enabled !== false : false);
    } catch (e) {
      if (seq === previewSeq) status('Could not preview EQ: ' + e.message, true);
    }
  }
  function scheduleLiveDraft(immediate) {
    if (applying) return;
    liveSeq += 1; livePending = true;
    window.clearTimeout(liveTimer);
    liveTimer = window.setTimeout(runLiveDraft, immediate ? 0 : 180);
  }
  function cancelLiveDrafts() { liveSeq += 1; livePending = false; window.clearTimeout(liveTimer); }
  async function runLiveDraft() {
    if (!livePending || applying || liveInFlight) return;
    livePending = false; liveInFlight = true;
    var seq = liveSeq;
    try {
      var resp = await fetch('./live-draft', {method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify({profile: draft, dsp_write_epoch: dspWriteEpoch})});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'live draft failed');
      if (seq === liveSeq) {
        if (payload.dsp_write_epoch) dspWriteEpoch = payload.dsp_write_epoch;
        if (payload.live_status === 'live') status('Listening to this draft live.');
        else if (payload.live_status === 'stale') status('Speaker DSP changed — move a control again to hear this draft.');
        else status('Live preview unavailable on this CamillaDSP connection.', true);
      }
    } catch (e) {
      if (seq === liveSeq) status('Could not update live draft: ' + e.message, true);
    } finally {
      liveInFlight = false;
      if (livePending && !applying) { window.clearTimeout(liveTimer); liveTimer = window.setTimeout(runLiveDraft, 0); }
    }
  }
  // okMsg is shown only for explicit actions (save/overwrite). Tab-driven
  // applies (Off, Saved-select) pass none and stay silent on success —
  // the active tab + "Now playing" label already convey the state. Errors
  // always surface (no silent failure).
  async function applyProfile(profile, okMsg) {
    applying = true; cancelLiveDrafts();
    if (okMsg) status('Applying…');
    try {
      var resp = await fetch('./apply', {method: 'POST', headers: jsonHeaders(), body: JSON.stringify(profile)});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'apply failed');
      ingestState(payload);
      status(okMsg || '');
    } catch (e) {
      status('Could not apply: ' + e.message, true);
    } finally { applying = false; render(); }
  }
  async function profileMutate(path, body) {
    applying = true;
    try {
      var resp = await fetch(path, {method: 'POST', headers: jsonHeaders(), body: JSON.stringify(body || {})});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'profile update failed');
      if (payload.profile_library) library = payload.profile_library;
      return payload;
    } catch (e) {
      status('Could not update profiles: ' + e.message, true);
      return null;
    } finally { applying = false; }
  }

  // Global sound settings (match-loudness, headroom). Optimistic: the
  // controls already show the user's input, so on success we just ingest
  // (audio is re-applied server-side); on failure we revert and re-render.
  async function saveSettings(patch) {
    var prev = soundSettings;
    soundSettings = Object.assign({}, soundSettings, patch);
    try {
      var resp = await fetch('./settings', {method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify(soundSettings)});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'settings failed');
      ingestState(payload);
      if (payload.warning) status(payload.warning, true);
    } catch (e) {
      soundSettings = prev;
      status('Could not save sound settings: ' + e.message, true);
      render();
    }
  }

  function ingestState(payload) {
    limits = Object.assign({}, LIMIT_DEFAULTS, payload.limits || {});
    simpleBands = limits.simple_bands || [];
    if (payload.curves) { curvesById = {}; payload.curves.forEach(function(c) { curvesById[c.id] = c; }); }
    if (payload.profile_library) library = payload.profile_library;
    if (payload.dsp_write_epoch) dspWriteEpoch = payload.dsp_write_epoch;
    if (payload.sound_settings) soundSettings = payload.sound_settings;
    applied = normalizeProfile(payload.profile || {});
  }

  // ---- tab + edit transitions ----------------------------------------
  function setView(v) {
    view = v;
    render();
    // Off and Saved are durable: clicking Off applies a bypass; tapping a
    // saved profile applies it (see selectSaved). Draft is a live, non-
    // persistent preview until the footer Save commits it.
    if (v === 'off') {
      applyProfile(Object.assign(normalizeProfile(applied), {enabled: false}));
    } else if (v === 'draft') {
      scheduleLiveDraft(true);
    }
  }
  function selectSaved(id) {
    selectedId = id;
    var entry = entryById(id);
    render();
    if (entry) applyProfile(withIdentity(normalizeProfile(entry.profile), entry.id, entry.name));
  }
  function newDraft() {
    draft = FLAT(); editing = {kind: 'new'}; mode = 'simple'; activeBand = 0; naming = false;
    view = 'draft'; status(''); render(); scheduleLiveDraft(true);
  }
  function editEntry(id) {
    var entry = entryById(id);
    if (!entry) return;
    draft = normalizeProfile(entry.profile);
    editing = {kind: entry.kind === 'custom' ? 'user' : 'preset', id: entry.id, name: entry.name};
    mode = draft.parametric_bands.length ? 'peq' : 'simple';
    activeBand = 0; naming = false; view = 'draft';
    status('Editing ' + entry.name + '.'); render(); scheduleLiveDraft(true);
  }
  // Body re-render + optimistic graph (via schedulePreview) + live audio.
  function onDraftChanged(immediate) { renderDraft(); schedulePreview(); scheduleLiveDraft(immediate); }
  function refreshActiveCount() {
    var e = el('active-count');
    if (!e) return;
    var n = mode === 'simple'
      ? Object.keys(draft.simple_eq).filter(function(k) { return Math.abs(draft.simple_eq[k]) >= 0.05; }).length
      : draft.parametric_bands.filter(function(b) { return b.enabled !== false; }).length;
    e.textContent = n + ' active';
  }
  // During a drag we patch the DOM in place (no full re-render, so the
  // <input> keeps focus). The visible thumb/fill is a separate element
  // positioned by inline style at render time, so move it here too —
  // otherwise the handle stays put while only the readout changes.
  function positionThumb(input) {
    var min = parseFloat(input.min), max = parseFloat(input.max);
    if (!(max > min)) return;
    var pct = (clamp(parseFloat(input.value), min, max) - min) / (max - min) * 100;
    var wrap = input.parentNode, hit;
    if ((hit = wrap.querySelector('.vrange__thumb'))) hit.style.bottom = 'calc(' + pct + '% - 6px)';
    else if ((hit = wrap.querySelector('.range__thumb'))) hit.style.left = 'calc(' + pct + '% - 6px)';
    else if ((hit = wrap.querySelector('.range__fill'))) hit.style.width = pct + '%';
  }

  // ---- events ---------------------------------------------------------
  ['off', 'saved', 'draft'].forEach(function(v) {
    el('tab-' + v).addEventListener('click', function() { if (view !== v) setView(v); });
  });
  el('back').addEventListener('click', function(e) { e.preventDefault(); window.location.href = '/'; });

  el('view-body').addEventListener('click', function(ev) {
    var t = ev.target.closest('[data-act]');
    if (!t) return;
    var act = t.getAttribute('data-act');
    var id = t.getAttribute('data-id');
    var index = Number(t.getAttribute('data-index'));
    if (act === 'browse-presets') { view = 'saved'; render(); }
    else if (act === 'new-draft') { newDraft(); }
    else if (act === 'select') { selectSaved(id); }
    else if (act === 'edit') { editEntry(id); }
    else if (act === 'delete') { deleteEntry(id); }
    else if (act === 'add-band') { addBand(); }
    else if (act === 'del-band') { delBand(index); }
    else if (act === 'toggle-band') { activeBand = (activeBand === index && !allCollapsed) ? -1 : index; allCollapsed = false; renderDraft(); renderLiveGraph(); }
    else if (act === 'toggle-collapse') { allCollapsed = !allCollapsed; renderDraft(); }
    else if (act === 'begin-name') { naming = true; nameMode = 'save'; nameDraft = defaultName(); renderDraft(); focusNameInput(); }
    else if (act === 'begin-rename') { naming = true; nameMode = 'rename'; nameDraft = editing.name || ''; renderDraft(); focusNameInput(); }
    else if (act === 'cancel-name') { naming = false; renderDraft(); }
    else if (act === 'finalize-name') { finalizeName(); }
    else if (act === 'overwrite') { overwrite(); }
    else if (act === 'discard') { discardEdits(); }
  });
  // Mode + band-type segmented buttons (delegated).
  el('view-body').addEventListener('click', function(ev) {
    var modeBtn = ev.target.closest('[data-mode]');
    if (modeBtn) { switchMode(modeBtn.getAttribute('data-mode')); return; }
    var typeBtn = ev.target.closest('[data-band-type]');
    if (typeBtn) {
      var wrap = typeBtn.closest('[data-band]');
      var bi = Number(wrap.getAttribute('data-band'));
      if (draft.parametric_bands[bi]) { draft.parametric_bands[bi].type = typeBtn.getAttribute('data-band-type'); activeBand = bi; onDraftChanged(true); }
    }
  });
  el('view-body').addEventListener('input', function(ev) {
    var field = ev.target.getAttribute('data-field');
    var range = ev.target.getAttribute('data-range');
    if (field) {
      draft.simple_eq[field] = clamp(ev.target.value, -limits.simple_gain_db, limits.simple_gain_db);
      var btn = el('view-body').querySelector('[data-readout-field="' + field + '"]');
      if (btn) btn.textContent = fmtDb(draft.simple_eq[field]);
      positionThumb(ev.target);
      refreshActiveCount();
      schedulePreview(); scheduleLiveDraft(false);
    } else if (range) {
      var row = ev.target.closest('.band-row');
      var bi = Number(row.getAttribute('data-index'));
      var band = draft.parametric_bands[bi];
      if (!band) return;
      activeBand = bi;
      if (range === 'freq') band.freq_hz = sliderToFreq(ev.target.value, limits.min_freq_hz, limits.max_freq_hz);
      if (range === 'gain') band.gain_db = clamp(ev.target.value, -limits.advanced_gain_db, limits.advanced_gain_db);
      if (range === 'q') band.q = clamp(ev.target.value, limits.min_q, limits.max_q);
      var ro = row.querySelector('[data-readout="' + range + '"]');
      if (ro) ro.textContent = range === 'freq' ? fmtFreq(band.freq_hz) : (range === 'gain' ? fmtDb(band.gain_db) + ' dB' : fmtQ(band.q));
      positionThumb(ev.target);
      schedulePreview(); scheduleLiveDraft(false);
    }
  });
  el('view-body').addEventListener('input', function(ev) {
    if (ev.target.id === 'name-input') { nameDraft = ev.target.value; return; }
    if (ev.target.id === 'set-headroom') {
      var ro = el('set-headroom-readout');           // live readout; commit on 'change'
      if (ro) ro.textContent = fmtTrim(ev.target.value);
    }
  });
  el('view-body').addEventListener('change', function(ev) {
    if (ev.target.id === 'set-match-loudness') saveSettings({match_loudness: ev.target.checked});
    else if (ev.target.id === 'set-headroom') saveSettings({headroom_trim_db: Number(ev.target.value)});
  });
  el('view-body').addEventListener('keydown', function(ev) {
    if (ev.target.id !== 'name-input') return;
    if (ev.key === 'Enter') { ev.preventDefault(); finalizeName(); }
    else if (ev.key === 'Escape') { ev.preventDefault(); naming = false; renderDraft(); }
  });

  function switchMode(next) {
    if (next === mode) return;
    if (next === 'simple') {
      // Snap to the simple template, copying nearest gain by log-frequency.
      var newSimple = zeroSimple();
      (simpleBands || []).forEach(function(slot) {
        var nearest = null, best = 1.2;
        draft.parametric_bands.filter(function(b) { return b.enabled !== false; }).forEach(function(b) {
          var dist = Math.abs(Math.log(b.freq_hz / slot.freq_hz) / Math.log(2));
          if (dist < best) { best = dist; nearest = b; }
        });
        if (nearest) newSimple[slot.field] = clamp(nearest.gain_db, -limits.simple_gain_db, limits.simple_gain_db);
      });
      draft.simple_eq = newSimple;
      draft.parametric_bands = [];
    } else {
      // Simple -> PEQ keeps the simple bands as gains; PEQ owns the bands going forward.
      draft.parametric_bands = (simpleBands || []).filter(function(s) {
        return Math.abs(draft.simple_eq[s.field] || 0) >= 0.05;
      }).map(function(s) {
        return {enabled: true, type: s.type, freq_hz: s.freq_hz,
                gain_db: draft.simple_eq[s.field], q: s.type === 'Peaking' ? 1.0 : 1.0};
      });
      draft.simple_eq = zeroSimple();
      activeBand = 0;
    }
    mode = next;
    onDraftChanged(true);
  }
  function addBand() {
    if (draft.parametric_bands.length >= limits.max_parametric_bands) {
      status('Advanced EQ is limited to ' + limits.max_parametric_bands + ' bands.', true);
      return;
    }
    draft.parametric_bands.push({enabled: true, type: 'Peaking', freq_hz: 1000, gain_db: 0, q: 1});
    activeBand = draft.parametric_bands.length - 1;
    onDraftChanged(true);
  }
  function delBand(index) {
    draft.parametric_bands.splice(index, 1);
    activeBand = Math.max(0, Math.min(activeBand, draft.parametric_bands.length - 1));
    onDraftChanged(true);
  }
  function discardEdits() {
    draft = sourceProfile();
    if (editing.kind !== 'new') draft = withIdentity(draft, editing.id, editing.name);
    mode = draft.parametric_bands.length ? 'peq' : 'simple';
    activeBand = 0; naming = false;
    onDraftChanged(true);
  }
  function defaultName() {
    var d = new Date();
    var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return 'Profile · ' + months[d.getMonth()] + ' ' + d.getDate();
  }
  function focusNameInput() { var n = el('name-input'); if (n) { n.focus(); n.select(); } }
  async function finalizeName() {
    naming = false;
    if (nameMode === 'rename' && editing.kind === 'user') {
      var newName = (nameDraft || '').trim() || editing.name || defaultName();
      var rp = await profileMutate('./profiles/rename', {id: editing.id, name: newName});
      if (rp && rp.profile_entry) {
        if (selectedId === editing.id) selectedId = rp.profile_entry.id;
        editing = {kind: 'user', id: rp.profile_entry.id, name: rp.profile_entry.name};
        status('Renamed to ' + rp.profile_entry.name + '.');
      }
      render();
      return;
    }
    var name = (nameDraft || '').trim() || defaultName();
    var payload = await profileMutate('./profiles/save', {id: null, name: name, profile: draft});
    if (payload && payload.profile_entry) {
      var entry = payload.profile_entry;
      library = payload.profile_library || library;
      await applyProfile(withIdentity(normalizeProfile(entry.profile), entry.id, entry.name), 'Saved ' + entry.name + '.');
      selectedId = entry.id; view = 'saved'; render();
    } else { render(); }
  }
  async function overwrite() {
    if (editing.kind !== 'user') return;
    var payload = await profileMutate('./profiles/save', {id: editing.id, name: editing.name, profile: draft});
    if (payload && payload.profile_entry) {
      var entry = payload.profile_entry;
      await applyProfile(withIdentity(normalizeProfile(entry.profile), entry.id, entry.name), 'Updated ' + entry.name + '.');
      selectedId = entry.id; view = 'saved'; render();
    }
  }
  async function deleteEntry(id) {
    var entry = entryById(id);
    if (!entry || entry.kind !== 'custom') return;
    if (!window.confirm('Delete profile "' + entry.name + '"?')) return;
    var payload = await profileMutate('./profiles/delete', {id: id});
    if (payload) {
      if (selectedId === id) selectedId = null;
      status('Deleted ' + entry.name + '.');
      render();
    }
  }

  async function loadState() {
    try {
      var resp = await fetch('./state', {cache: 'no-store'});
      if (!resp.ok) throw new Error('state failed');
      var payload = await resp.json();
      ingestState(payload);
      // Open on Off when no EQ is effectively applied — bypassed (enabled
      // false) OR flat (no active filters). Open on Saved otherwise, with
      // the currently-applied profile marked active ("Now playing").
      // filter_count is the backend's authoritative signal
      // (len(build_sound_filters); 0 when disabled or flat).
      if (payload.filter_count > 0) {
        view = 'saved';
        selectedId = findIdFor(applied);
      } else {
        view = 'off';
        selectedId = null;
      }
      render();
    } catch (e) {
      status('Could not load sound profile: ' + e.message, true);
    }
  }
  loadState();
})();
"""


def _index_html(csrf_token: str = "") -> bytes:
    body = (
        """
<header class="app-header">
  <div class="app-header__row">
    <a class="icon-button" id="back" href="/" aria-label="Home">"""
        + '<svg class="ico" aria-hidden="true"><use href="#icon-back"></use></svg>'
        + """</a>
    <h1 class="app-header__title">Sound profile</h1>
    <span></span>
  </div>
  <div class="app-header__tabs">
    <div>
      <div class="segmented" role="tablist" aria-label="Sound source">
        <button class="segmented__btn" id="tab-off" data-view="off" aria-pressed="true">Off</button>
        <button class="segmented__btn" id="tab-saved" data-view="saved" aria-pressed="false">Saved</button>
        <button class="segmented__btn" id="tab-draft" data-view="draft" aria-pressed="false">Draft</button>
      </div>
    </div>
  </div>
</header>
<main class="page">
  <section class="now-playing">
    <div class="row-between">
      <h2 class="eyebrow">Now playing</h2>
      <span class="now-playing__label" id="live-label">Bypass</span>
    </div>
    <div class="graph-card">
      <svg class="eq-graph" id="plot" viewBox="0 0 620 200" preserveAspectRatio="none"
           role="img" aria-label="EQ response preview"></svg>
    </div>
    <div class="sr-only" id="plot-summary" aria-live="polite"></div>
  </section>
  <div id="view-body"></div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</main>
<script>
"""
        + _SOUND_JS.replace("__CSRF_HELPERS__", csrf_fetch_helpers_js())
        + """
</script>
"""
    )
    return canonical_page(
        "Sound profile", body, csrf_token=csrf_token, page_css=_PAGE_CSS,
    )


def _make_handler(
    *,
    profile_path: str | Path,
    library_path: str | Path,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length > MAX_JSON_BYTES:
                raise ValueError("request body too large")
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                ctx = begin_request(self)
                self._send_html(_index_html(ctx["csrf_token"]))
                return
            if path == "/state":
                self._send_json(
                    _state_payload(
                        load_profile(profile_path),
                        library_path=library_path,
                        include_library=True,
                    )
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/apply",
                "/audition",
                "/live-draft",
                "/preview",
                "/settings",
                "/profiles/save",
                "/profiles/rename",
                "/profiles/delete",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not verify_csrf(self):
                reject_csrf(self)
                return
            try:
                raw = self._read_json()
                if path == "/settings":
                    settings = SoundSettings.from_mapping(raw)
                    try:
                        payload = asyncio.run(
                            _apply_settings(
                                settings,
                                profile_path=profile_path,
                                library_path=library_path,
                                config_dir=config_dir,
                                camilla_factory=camilla_factory,
                            )
                        )
                    except OSError as e:
                        logger.exception("sound settings save failed")
                        self._send_json({"error": str(e)}, status=502)
                        return
                    self._send_json(payload)
                    return
                if path.startswith("/profiles/"):
                    try:
                        if path == "/profiles/save":
                            requested_id = str(raw.get("id") or "")
                            entry = save_named_profile(
                                SoundProfile.from_mapping(raw.get("profile")),
                                name=raw.get("name"),
                                path=library_path,
                                profile_id=requested_id,
                            )
                            action = "update" if requested_id == entry.id else "create"
                            logger.info(
                                "event=sound.profile_library action=%s "
                                "profile_id=%s curve=%s bands=%d",
                                action,
                                entry.id,
                                entry.profile.curve_id,
                                len(entry.profile.parametric_bands),
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["profile_entry"] = entry.to_payload()
                        elif path == "/profiles/rename":
                            entry = rename_named_profile(
                                str(raw.get("id") or ""),
                                name=str(raw.get("name") or ""),
                                path=library_path,
                            )
                            logger.info(
                                "event=sound.profile_library action=rename "
                                "profile_id=%s curve=%s bands=%d",
                                entry.id,
                                entry.profile.curve_id,
                                len(entry.profile.parametric_bands),
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["profile_entry"] = entry.to_payload()
                        else:
                            deleted_id = str(raw.get("id") or "")
                            delete_named_profile(deleted_id, path=library_path)
                            logger.info(
                                "event=sound.profile_library action=delete profile_id=%s",
                                deleted_id,
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["deleted_profile_id"] = deleted_id
                    except OSError as e:
                        logger.exception("sound profile library update failed")
                        self._send_json({"error": str(e)}, status=502)
                        return
                    self._send_json(payload)
                    return
                if path in {"/audition", "/live-draft"}:
                    raw_profile = raw.get("profile", raw)
                else:
                    raw_profile = raw
                profile = SoundProfile.from_mapping(raw_profile)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
                self._send_json({"error": str(e)}, status=400)
                return
            if path == "/preview":
                self._send_json(_state_payload(profile))
                return
            try:
                if path in {"/audition", "/live-draft"}:
                    if path == "/live-draft":
                        expected_epoch = raw.get("dsp_write_epoch")
                        if not isinstance(expected_epoch, str) or not expected_epoch:
                            self._send_json(
                                {"error": "missing dsp_write_epoch"},
                                status=400,
                            )
                            return
                        payload = asyncio.run(
                            _live_draft_profile(
                                profile,
                                expected_dsp_write_epoch=expected_epoch,
                                profile_path=profile_path,
                                library_path=library_path,
                                config_dir=config_dir,
                                camilla_factory=camilla_factory,
                            )
                        )
                    else:
                        audition_mode = str(raw.get("mode") or "draft")
                        if audition_mode not in {"bypass", "applied", "draft"}:
                            audition_mode = "draft"
                        payload = asyncio.run(
                            _audition_profile(
                                profile,
                                audition_mode=audition_mode,
                                profile_path=profile_path,
                                library_path=library_path,
                                config_dir=config_dir,
                                camilla_factory=camilla_factory,
                            )
                        )
                else:
                    payload = asyncio.run(
                        _apply_profile(
                            profile,
                            profile_path=profile_path,
                            library_path=library_path,
                            config_dir=config_dir,
                            camilla_factory=camilla_factory,
                        )
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("sound profile apply failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(payload)

    return Handler


def make_server(
    target,
    *,
    profile_path: str | Path | None = None,
    library_path: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> ThreadingHTTPServer:
    from . import _systemd

    return _systemd.make_http_server(
        target,
        _make_handler(
            profile_path=profile_path
            or os.environ.get(
                "JASPER_SOUND_PROFILE_PATH",
                PROFILE_PATH,
            ),
            library_path=library_path
            or os.environ.get(
                "JASPER_SOUND_PROFILE_LIBRARY_PATH",
                PROFILE_LIBRARY_PATH,
            ),
            config_dir=config_dir
            or os.environ.get(
                "JASPER_SOUND_CONFIG_DIR",
                DEFAULT_CONFIG_DIR,
            ),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-sound-web",
        description="Sound curve and preference-EQ wizard",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_SOUND_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("JASPER_SOUND_WEB_PORT", "8784")),
    )
    parser.add_argument(
        "--profile-path",
        default=os.environ.get("JASPER_SOUND_PROFILE_PATH", PROFILE_PATH),
    )
    parser.add_argument(
        "--library-path",
        default=os.environ.get(
            "JASPER_SOUND_PROFILE_LIBRARY_PATH",
            PROFILE_LIBRARY_PATH,
        ),
    )
    parser.add_argument(
        "--config-dir",
        default=os.environ.get("JASPER_SOUND_CONFIG_DIR", DEFAULT_CONFIG_DIR),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(
        (args.host, args.port),
        profile_path=args.profile_path,
        library_path=args.library_path,
        config_dir=args.config_dir,
    )
    logger.info("jasper-sound-web listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
