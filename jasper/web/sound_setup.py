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
    output_trim_db as _output_trim,  # aliased so local `output_trim_db` vars don't shadow it
    save_sound_settings,
)

from ._common import (
    begin_request,
    canonical_page,
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
        # profile is disabled (bypass) OR flat (no active filters). The page
        # opens on Off vs Saved based on this.
        "filter_count": len(build_sound_filters(profile)),
        # Global output settings + the trim they imply for THIS profile, so
        # the page can render the controls and show the effective trim.
        "sound_settings": settings.to_dict(),
        "output_trim_db": _output_trim(profile, settings),
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
        output_trim_db=_output_trim(profile, settings),
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
    a setting the backend already kept -- no silent failure either way.
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
            output_trim_db=_output_trim(profile, settings),
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
    output_trim_db = _output_trim(profile, settings)
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


async def audition_profile(
    profile: SoundProfile,
    *,
    audition_mode: str = "draft",
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    """Public backend seam for reversible preference-EQ auditions.

    The web route and the calibration-advisor action runner both use the
    same implementation so model-suggested auditions inherit the existing
    CamillaDSP config validation, room-PEQ preservation, and no-persist
    semantics from ``/sound/audition``.
    """

    return await _audition_profile(
        profile,
        audition_mode=audition_mode,
        profile_path=profile_path,
        library_path=library_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
    )


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
    output_trim_db = _output_trim(profile, settings)
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
<script type="module" src="/assets/sound-profile/js/main.js"></script>
"""
    )
    return canonical_page(
        "Sound profile", body, csrf_token=csrf_token,
        page_css_href="/assets/sound-profile/sound.css",
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
