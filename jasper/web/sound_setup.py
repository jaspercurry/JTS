"""Sound curve and preference-EQ page at /sound/.

URL surface (after nginx strips /sound/):
  GET  /         page render
  GET  /state    persisted profile + preview + stock curve metadata
  GET  /output-topology              speaker/DAC topology draft + safety evidence
  GET  /active-speaker/channel-identity physical-output identity evidence
  GET  /active-speaker/environment     read-only active-speaker readiness
  GET  /active-speaker/safe-playback   no-audio safety session state
  GET  /active-speaker/staged-config   latest protected startup config evidence
  GET  /active-speaker/calibration-level backend-owned level guard
  GET  /active-speaker/bringup-preflight guided/manual bring-up readiness
  GET  /active-speaker/startup-load guarded startup-load/rollback state
  GET  /active-speaker/tone-targets    preset-derived channel-test targets
  POST /preview  preview a draft profile's response without touching live audio
  POST /live-draft apply a draft to live audio without persisting
  POST /audition validate and load a draft/bypass config without persisting
  POST /active-speaker/arm       arm a no-audio active-speaker safety session
  POST /active-speaker/stop      stop the no-audio active-speaker session
  POST /active-speaker/calibration-level update backend-owned level guard
  POST /active-speaker/playback-readiness no-audio target readiness checklist
  POST /active-speaker/stage-config stage protected startup config
  POST /active-speaker/check-path-safety inspect and persist no-audio path evidence
  POST /active-speaker/load-startup-config load protected startup config, no sound
  POST /active-speaker/rollback-startup-config restore pre-load config, no sound
  POST /active-speaker/tone-plan prepare a bounded no-audio channel-test plan
  POST /active-speaker/play-tone run a bounded artifact/audio-gated tone test
  POST /active-speaker/floor-audio-result record operator result for floor tone
  POST /active-speaker/channel-identity mark/clear physical identity evidence
  POST /active-speaker/channel-protection mark/clear tweeter protection evidence
  POST /output-topology save a complete speaker/DAC topology draft
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

from jasper.output_topology import (
    OutputTopology,
    channel_identity_report,
    clock_domain_report,
    load_output_topology,
    save_output_topology,
    set_channel_identity_verified,
    set_channel_protection_status,
)
from jasper.sound.profile import (
    ADVANCED_GAIN_LIMIT_DB,
    CUT_MAX_Q,
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
    guard_mutating_request,
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
            "cut_max_q": CUT_MAX_Q,
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


def _output_topology_payload() -> dict[str, Any]:
    topology = load_output_topology()
    return {
        "output_topology": topology.to_dict(include_evaluation=True),
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
    }


def _save_output_topology_payload(raw: dict[str, Any]) -> dict[str, Any]:
    raw_topology = raw.get("output_topology", raw)
    topology = OutputTopology.from_mapping(raw_topology)
    save_output_topology(topology)
    evaluation = topology.evaluation()
    logger.info(
        "event=sound.output_topology_save topology_id=%s status=%s "
        "device_id=%s groups=%d assigned_outputs=%d blockers=%d warnings=%d",
        topology.topology_id,
        evaluation["status"],
        topology.hardware.device_id,
        len(topology.speaker_groups),
        evaluation["assigned_output_count"],
        len(evaluation["blockers"]),
        len(evaluation["warnings"]),
    )
    return {
        "output_topology": topology.to_dict(include_evaluation=True),
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
    }


def _active_speaker_channel_identity_payload() -> dict[str, Any]:
    """Return physical-channel identity evidence for the saved topology."""

    topology = load_output_topology()
    return {
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
    }


def _active_speaker_channel_identity_save_payload(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Mark or clear a saved topology channel's physical identity evidence."""

    if not isinstance(raw, dict):
        raise ValueError("channel identity request must be an object")
    topology = load_output_topology()
    speaker_group_id = str(raw.get("speaker_group_id") or raw.get("group_id") or "")
    role = str(raw.get("role") or "")
    verified = raw.get("identity_verified")
    if not isinstance(verified, bool):
        raise ValueError("identity_verified must be a boolean")
    updated = set_channel_identity_verified(
        topology,
        speaker_group_id=speaker_group_id,
        role=role,
        identity_verified=verified,
    )
    save_output_topology(updated)
    report = channel_identity_report(updated)
    evaluation = updated.evaluation()
    logger.info(
        "event=sound.active_speaker_channel_identity action=%s "
        "topology_id=%s group_id=%s role=%s status=%s verified=%d/%d "
        "blockers=%d",
        "mark_verified" if verified else "clear_verified",
        updated.topology_id,
        speaker_group_id,
        role,
        report.get("status"),
        report.get("verified_channel_count"),
        report.get("assigned_channel_count"),
        len(evaluation.get("blockers") or []),
    )
    return {
        "output_topology": updated.to_dict(include_evaluation=True),
        "channel_identity": report,
        "clock_domain": clock_domain_report(updated),
    }


def _active_speaker_channel_protection_save_payload(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Mark or clear a saved topology channel's protection evidence."""

    if not isinstance(raw, dict):
        raise ValueError("channel protection request must be an object")
    topology = load_output_topology()
    speaker_group_id = str(raw.get("speaker_group_id") or raw.get("group_id") or "")
    role = str(raw.get("role") or "")
    requested_status = raw.get("protection_status")
    if requested_status is not None:
        if not isinstance(requested_status, str):
            raise ValueError("protection_status must be a string")
        protection_status = requested_status
    else:
        protection_present = raw.get("protection_present")
        if not isinstance(protection_present, bool):
            raise ValueError("protection_present must be a boolean")
        protection_status = "present" if protection_present else "required_missing"
    updated = set_channel_protection_status(
        topology,
        speaker_group_id=speaker_group_id,
        role=role,
        protection_status=protection_status,
    )
    save_output_topology(updated)
    report = channel_identity_report(updated)
    evaluation = updated.evaluation()
    logger.info(
        "event=sound.active_speaker_channel_protection "
        "topology_id=%s group_id=%s role=%s protection_status=%s "
        "status=%s blockers=%d",
        updated.topology_id,
        speaker_group_id,
        role,
        protection_status,
        report.get("status"),
        len(evaluation.get("blockers") or []),
    )
    return {
        "output_topology": updated.to_dict(include_evaluation=True),
        "channel_identity": report,
        "clock_domain": clock_domain_report(updated),
    }


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


def _active_speaker_environment_payload() -> dict[str, Any]:
    """Return read-only active-speaker readiness for the /sound/ advanced card."""

    from jasper.active_speaker.environment import probe_active_speaker_environment

    evidence_path = _active_speaker_path_safety_evidence_path()
    report = probe_active_speaker_environment(
        path_safety_evidence_path=evidence_path or None,
    )
    logger.info(
        "event=sound.active_speaker_environment status=%s load_gate=%s "
        "blockers=%d safe_playback=%s",
        report.get("status"),
        report.get("load_gate"),
        int(report.get("blocker_count") or 0),
        bool(report.get("safe_playback", {}).get("playback_allowed")),
    )
    return report


def _active_speaker_path_safety_evidence_path() -> str | None:
    from jasper.active_speaker.path_safety import path_safety_evidence_path

    evidence_path = os.environ.get("JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE")
    if evidence_path and evidence_path.strip():
        return evidence_path.strip()
    default_path = path_safety_evidence_path()
    return str(default_path) if default_path.exists() else None


def _active_speaker_staged_config_payload() -> dict[str, Any]:
    """Return the latest protected startup config staging evidence."""

    from jasper.active_speaker.staging import load_staged_startup_config

    return load_staged_startup_config()


def _active_speaker_stage_config_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Stage a protected startup config from the saved topology."""

    from jasper.active_speaker.staging import stage_protected_startup_config

    if not isinstance(raw, dict):
        raise ValueError("stage config request must be an object")
    playback_device = raw.get("playback_device")
    if playback_device is not None and not isinstance(playback_device, str):
        raise ValueError("playback_device must be a string")
    topology = load_output_topology()
    payload = stage_protected_startup_config(
        topology,
        playback_device=playback_device,
    )
    logger.info(
        "event=sound.active_speaker_stage_config status=%s topology_id=%s "
        "preset_id=%s config=%s blockers=%d",
        payload.get("status"),
        payload.get("topology", {}).get("topology_id"),
        payload.get("preset", {}).get("preset_id"),
        payload.get("config", {}).get("basename"),
        len(payload.get("issues") or []),
    )
    return payload


def _active_speaker_safe_playback_payload() -> dict[str, Any]:
    """Return the current no-audio active-speaker safety session."""

    from jasper.active_speaker.safe_playback import load_safe_playback_state

    return load_safe_playback_state()


def _active_speaker_requested_target(raw: dict[str, Any]) -> tuple[str, str]:
    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    speaker_group_id = str(
        raw.get("speaker_group_id") or target.get("speaker_group_id") or ""
    ).strip()
    role = str(
        raw.get("role")
        or raw.get("driver_role")
        or target.get("role")
        or target.get("driver_role")
        or ""
    ).strip().lower()
    if not speaker_group_id or not role:
        raise ValueError("target requires speaker_group_id and role")
    return speaker_group_id, role


def _active_speaker_saved_target(raw: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
    """Resolve a request target against the saved output topology."""

    speaker_group_id, role = _active_speaker_requested_target(raw)
    topology = load_output_topology()
    for group in topology.speaker_groups:
        if group.id != speaker_group_id:
            continue
        for channel in group.channels:
            if channel.role != role:
                continue
            if channel.physical_output_index is None:
                raise ValueError("target channel has no physical output")
            target = {
                "speaker_group_id": group.id,
                "speaker_label": group.label,
                "role": channel.role,
                "driver_role": channel.role,
                "driver_style": channel.driver_style,
                "protection_status": channel.protection_status,
                "output_index": channel.physical_output_index,
                "physical_output_index": channel.physical_output_index,
                "label": (
                    f"{group.label} {channel.role} "
                    f"(Output {channel.physical_output_index + 1})"
                ),
            }
            return group, channel, target
    raise ValueError("target channel not found in saved output topology")


def _active_speaker_mic_inputs(
    raw: dict[str, Any],
    current_level: dict[str, Any],
) -> tuple[Any, bool]:
    """Return explicit mic input, falling back to the persisted observation."""

    meter = (
        current_level.get("mic_meter")
        if isinstance(current_level.get("mic_meter"), dict)
        else {}
    )
    observed = (
        raw.get("observed_mic_dbfs")
        if "observed_mic_dbfs" in raw
        else meter.get("observed_dbfs")
    )
    clipping = (
        bool(raw.get("mic_clipping"))
        if "mic_clipping" in raw
        else meter.get("status") == "clipping"
    )
    return observed, clipping


def _active_speaker_auto_level_step(raw: dict[str, Any]) -> dict[str, Any]:
    from jasper.active_speaker.driver_protection import (
        auto_level_decision,
        driver_protection_profile,
    )
    from jasper.active_speaker.safe_playback import (
        floor_audio_confirmed_for_target,
        load_safe_playback_state,
    )
    from jasper.active_speaker.calibration_level import (
        load_calibration_level_state,
        update_calibration_level_state,
    )

    _, channel, target = _active_speaker_saved_target(raw)
    current_level = load_calibration_level_state()
    observed, clipping = _active_speaker_mic_inputs(raw, current_level)
    profile = driver_protection_profile(
        channel.role,
        driver_style=channel.driver_style,
    )
    band_limit = (
        {"type": "highpass", "highpass_hz": profile.min_highpass_hz}
        if profile.min_highpass_hz is not None
        else None
    )
    safe_session = load_safe_playback_state()
    decision = auto_level_decision(
        current_level,
        role=channel.role,
        driver_style=channel.driver_style,
        protection_status=channel.protection_status,
        band_limit=band_limit,
        observed_mic_dbfs=observed,
        mic_clipping=clipping,
        floor_audio_confirmed=floor_audio_confirmed_for_target(safe_session, target),
        stop_control_available=True,
    )
    decision_action = str(decision.get("action") or "hold")
    if decision_action == "raise":
        update_action = "set"
        requested = decision.get("next_level_dbfs")
    elif decision_action == "lower":
        update_action = "lower"
        requested = decision.get("next_level_dbfs")
    elif decision_action == "reset_to_floor":
        update_action = "reset"
        requested = None
    else:
        update_action = "observe"
        requested = None
    payload = update_calibration_level_state(
        action=update_action,
        requested_level_dbfs=requested,
        observed_mic_dbfs=observed,
        mic_clipping=clipping,
    )
    payload.update({
        "auto_level": decision,
        "auto_level_applied_action": update_action,
        "target": target,
    })
    issue_codes = ",".join(
        str(issue.get("code") or "issue")[:80]
        for issue in decision.get("issues", [])
        if isinstance(issue, dict)
    )[:240]
    logger.info(
        "event=sound.active_speaker_auto_level status=%s action=%s "
        "applied_action=%s group_id=%s role=%s output_index=%s "
        "current_level_dbfs=%s next_level_dbfs=%s delta_db=%s "
        "mic_status=%s floor_confirmed=%s issue_codes=%s",
        decision.get("status"),
        decision.get("action"),
        update_action,
        target.get("speaker_group_id"),
        target.get("role"),
        target.get("output_index"),
        decision.get("current_level_dbfs"),
        decision.get("next_level_dbfs"),
        payload.get("applied_delta_db"),
        decision.get("mic_meter", {}).get("status"),
        bool(decision.get("floor_audio_confirmed")),
        issue_codes or "-",
    )
    return payload


def _active_speaker_calibration_level_payload(
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return or update the backend-owned active-speaker level guard."""

    from jasper.active_speaker.calibration_level import (
        load_calibration_level_state,
        update_calibration_level_state,
    )

    if raw is None:
        return load_calibration_level_state()
    if not isinstance(raw, dict):
        raise ValueError("calibration level request must be an object")
    action = str(raw.get("action") or "set")
    if action.strip().lower() == "auto_step":
        return _active_speaker_auto_level_step(raw)
    level = raw.get("level_dbfs", raw.get("requested_level_dbfs"))
    payload = update_calibration_level_state(
        action=action,
        requested_level_dbfs=level,
        observed_mic_dbfs=raw.get("observed_mic_dbfs"),
        mic_clipping=bool(raw.get("mic_clipping")),
    )
    logger.info(
        "event=sound.active_speaker_calibration_level action=%s "
        "level_dbfs=%s prior_level_dbfs=%s delta_db=%s mic_status=%s "
        "mic_recommendation=%s issues=%d",
        payload.get("last_action"),
        payload.get("test_signal", {}).get("requested_level_dbfs"),
        payload.get("prior_level_dbfs"),
        payload.get("applied_delta_db"),
        payload.get("mic_meter", {}).get("status"),
        payload.get("mic_meter", {}).get("recommendation"),
        len(payload.get("issues") or []),
    )
    return payload


def _active_speaker_arm_payload() -> dict[str, Any]:
    """Arm a no-audio active-speaker safety session if gates pass."""

    from jasper.active_speaker.safe_playback import arm_safe_playback_session

    environment_report = _active_speaker_environment_payload()
    state = arm_safe_playback_session(environment_report)
    logger.info(
        "event=sound.active_speaker_safe_playback action=arm status=%s "
        "session_id=%s load_gate=%s blockers=%d",
        state.get("status"),
        state.get("session_id"),
        state.get("environment", {}).get("load_gate"),
        len(state.get("issues") or []),
    )
    return state


def _active_speaker_stop_payload() -> dict[str, Any]:
    """Stop any no-audio active-speaker safety session."""

    from jasper.active_speaker.calibration_level import update_calibration_level_state
    from jasper.active_speaker.playback import stop_tone_playback
    from jasper.active_speaker.safe_playback import stop_safe_playback_session

    playback = stop_tone_playback(reason="operator_stop")
    state = dict(stop_safe_playback_session())
    try:
        state["calibration_level"] = update_calibration_level_state(action="stop")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event=sound.active_speaker_calibration_level action=stop_reset "
            "result=error error=%s",
            type(e).__name__,
        )
        state["calibration_level"] = {
            "status": "reset_failed",
            "error": str(e),
        }
    logger.info(
        "event=sound.active_speaker_safe_playback action=stop status=%s "
        "session_id=%s playback_status=%s audio_emitted=%s level_status=%s",
        state.get("status"),
        state.get("session_id"),
        playback.get("status"),
        bool(playback.get("audio_emitted")),
        state.get("calibration_level", {}).get("status"),
    )
    return state


def _active_speaker_preset():
    from jasper.active_speaker.tone_plan import load_active_speaker_preset

    return load_active_speaker_preset(
        os.environ.get("JASPER_ACTIVE_SPEAKER_PRESET") or None
    )


def _active_speaker_tone_targets_payload() -> dict[str, Any]:
    """Return preset-derived no-audio tone targets for /sound/."""

    from jasper.active_speaker.tone_plan import tone_targets_payload

    return tone_targets_payload(
        _active_speaker_preset(),
        calibration_level=_active_speaker_calibration_level_payload(),
    )


def _active_speaker_bringup_preflight_payload() -> dict[str, Any]:
    """Return guided-vs-manual active-speaker bring-up readiness."""

    from jasper.active_speaker.bringup import build_bringup_preflight
    from jasper.active_speaker.playback import tone_backend_status

    topology = load_output_topology()
    environment_report = _active_speaker_environment_payload()
    safe_session = _active_speaker_safe_playback_payload()
    staged_config = _active_speaker_staged_config_payload()
    calibration_level = _active_speaker_calibration_level_payload()
    payload = build_bringup_preflight(
        topology,
        environment_report=environment_report,
        safe_session=safe_session,
        staged_config=staged_config,
        calibration_level=calibration_level,
        tone_backend=tone_backend_status(),
        stop_control_available=True,
    )
    logger.info(
        "event=sound.active_speaker_bringup_preflight status=%s "
        "manual_available=%s guided_available=%s microphone=%s guard=%s",
        payload.get("status"),
        bool(payload.get("manual_bringup_available")),
        bool(payload.get("guided_calibration_available")),
        payload.get("microphone", {}).get("status"),
        payload.get("software_guard", {}).get("status"),
    )
    return payload


def _active_speaker_startup_load_payload() -> dict[str, Any]:
    """Return startup load state plus current guarded preflight."""

    from jasper.active_speaker.startup_load import (
        build_startup_load_preflight,
        load_startup_load_state,
    )

    topology = load_output_topology()
    payload = {
        "state": load_startup_load_state(),
        "preflight": build_startup_load_preflight(
            topology,
            path_safety_evidence_path=_active_speaker_path_safety_evidence_path(),
        ),
    }
    logger.info(
        "event=sound.active_speaker_startup_load status=%s preflight=%s "
        "rollback_available=%s",
        payload["state"].get("status"),
        payload["preflight"].get("status"),
        bool(payload["state"].get("rollback_available")),
    )
    return payload


def _active_speaker_commissioning_rehearsal_payload() -> dict[str, Any]:
    """Return the read-only durable active-speaker commissioning rehearsal."""

    from jasper.active_speaker.commissioning import build_commissioning_rehearsal

    topology = load_output_topology()
    safe_session = _active_speaker_safe_playback_payload()
    calibration_level = _active_speaker_calibration_level_payload()
    payload = build_commissioning_rehearsal(
        topology,
        bringup_preflight=_active_speaker_bringup_preflight_payload(),
        startup_load=_active_speaker_startup_load_payload(),
        safe_session=safe_session,
        calibration_level=calibration_level,
    )
    logger.info(
        "event=sound.active_speaker_commissioning_rehearsal status=%s "
        "durable_ready=%s completed=%s total=%s blockers=%d",
        payload.get("status"),
        bool(payload.get("durable_steps_ready")),
        payload.get("completed_step_count"),
        payload.get("total_step_count"),
        sum(
            1
            for step in payload.get("steps", [])
            if isinstance(step, dict) and step.get("status") == "blocked"
        ),
    )
    return payload


async def _active_speaker_check_path_safety_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Build and persist no-audio startup-load path-safety evidence."""

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.path_safety import (
        build_startup_load_path_safety_evidence,
        evaluate_path_safety_evidence,
        write_path_safety_evidence,
    )
    from jasper.active_speaker.staging import load_staged_startup_config
    from jasper.active_speaker.startup_load import (
        build_startup_load_preflight,
        load_startup_load_state,
    )

    topology = load_output_topology()
    staged_config = load_staged_startup_config()
    calibration_level = load_calibration_level_state()
    current_config_path: str | None = None
    current_config_error: str | None = None
    try:
        current_config_path = await camilla_factory().get_config_file_path(
            best_effort=False
        )
    except Exception as exc:  # noqa: BLE001
        current_config_error = type(exc).__name__
        logger.warning(
            "event=sound.active_speaker_path_safety action=current_config "
            "result=error error=%s",
            current_config_error,
        )
    evidence = build_startup_load_path_safety_evidence(
        topology,
        staged_config=staged_config,
        calibration_level=calibration_level,
        current_config_path=current_config_path,
        current_config_error=current_config_error,
    )
    report = evaluate_path_safety_evidence(evidence)
    target = write_path_safety_evidence(evidence)
    preflight = build_startup_load_preflight(
        topology,
        staged_config=staged_config,
        calibration_level=calibration_level,
        path_safety_evidence_path=target,
        current_config_path=current_config_path,
    )
    logger.info(
        "event=sound.active_speaker_path_safety action=check status=%s "
        "load_gate=%s path=%s blockers=%d",
        report.get("status"),
        report.get("load_gate"),
        target,
        int(report.get("blocker_count") or 0),
    )
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_path_safety_check",
        "evidence_path": str(target),
        "evidence": evidence,
        "report": report,
        "startup_load": {
            "state": load_startup_load_state(),
            "preflight": preflight,
        },
    }


async def _active_speaker_load_startup_config_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Load the protected startup config through the guarded backend."""

    from jasper.active_speaker.startup_load import load_protected_startup_config

    topology = load_output_topology()
    cam = camilla_factory()
    payload = await load_protected_startup_config(
        topology,
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
        path_safety_evidence_path=_active_speaker_path_safety_evidence_path(),
    )
    logger.info(
        "event=sound.active_speaker_startup_load action=load status=%s "
        "preflight=%s rollback_available=%s",
        payload.get("load", {}).get("status"),
        payload.get("preflight", {}).get("status"),
        bool(payload.get("load", {}).get("rollback_available")),
    )
    return payload


async def _active_speaker_rollback_startup_config_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Rollback the protected startup config through the guarded backend."""

    from jasper.active_speaker.startup_load import rollback_protected_startup_config

    cam = camilla_factory()
    payload = await rollback_protected_startup_config(
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
    )
    logger.info(
        "event=sound.active_speaker_startup_load action=rollback status=%s "
        "active=%s",
        payload.get("rollback", {}).get("status"),
        payload.get("rollback", {}).get("active_config_path"),
    )
    return payload


def _active_speaker_tone_plan_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded no-audio tone plan for the requested target."""

    from jasper.active_speaker.safe_playback import load_safe_playback_state
    from jasper.active_speaker.tone_plan import build_safe_tone_plan

    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    preset = _active_speaker_preset()
    environment_report = _active_speaker_environment_payload()
    safe_session = load_safe_playback_state()
    level = _active_speaker_calibration_level_payload()
    plan = build_safe_tone_plan(
        preset,
        safe_session=safe_session,
        environment_report=environment_report,
        side=raw.get("side") or target.get("side"),
        driver_role=raw.get("driver_role") or target.get("driver_role"),
        requested_level_dbfs=level.get("test_signal", {}).get("requested_level_dbfs"),
        requested_duration_ms=raw.get("duration_ms"),
    )
    logger.info(
        "event=sound.active_speaker_tone_plan status=%s preset_id=%s "
        "side=%s driver_role=%s output_index=%s level_dbfs=%s "
        "duration_ms=%s blockers=%d would_play=%s",
        plan.get("status"),
        plan.get("preset_id"),
        plan.get("target", {}).get("side"),
        plan.get("target", {}).get("driver_role"),
        plan.get("target", {}).get("output_index"),
        plan.get("tone", {}).get("level_dbfs"),
        plan.get("tone", {}).get("duration_ms"),
        len(plan.get("issues") or []),
        bool(plan.get("would_play")),
    )
    return plan


def _active_speaker_playback_readiness_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a no-audio readiness checklist for one saved topology target."""

    if not isinstance(raw, dict):
        raise ValueError("playback readiness request must be an object")
    from jasper.active_speaker.playback import tone_backend_status
    from jasper.active_speaker.readiness import build_playback_readiness
    from jasper.active_speaker.safe_playback import load_safe_playback_state
    from jasper.active_speaker.startup_load import load_startup_load_state

    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    topology = load_output_topology()
    environment_report = _active_speaker_environment_payload()
    safe_session = load_safe_playback_state()
    calibration_level = _active_speaker_calibration_level_payload()
    startup_load_state = load_startup_load_state()
    report = build_playback_readiness(
        topology,
        speaker_group_id=raw.get("speaker_group_id") or target.get("speaker_group_id"),
        role=(
            raw.get("role")
            or raw.get("driver_role")
            or target.get("role")
            or target.get("driver_role")
        ),
        environment_report=environment_report,
        safe_session=safe_session,
        calibration_level=calibration_level,
        startup_load_state=startup_load_state,
        tone_backend=tone_backend_status(),
        stop_control_available=True,
    )
    logger.info(
        "event=sound.active_speaker_playback_readiness status=%s "
        "group_id=%s role=%s preconditions=%s blockers=%d",
        report.get("status"),
        report.get("target", {}).get("speaker_group_id"),
        report.get("target", {}).get("role"),
        bool(report.get("preconditions_passed")),
        len(report.get("issues") or []),
    )
    return report


def _active_speaker_tone_playback_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Run a bounded tone test for a preset or saved-topology target."""

    from jasper.active_speaker.playback import (
        enabled_audio_backend,
        start_tone_playback,
    )
    from jasper.active_speaker.safe_playback import (
        load_safe_playback_state,
        record_safe_playback_result,
    )
    from jasper.active_speaker.topology_tone import build_topology_tone_plan

    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    topology_target = bool(
        raw.get("speaker_group_id")
        or raw.get("role")
        or target.get("speaker_group_id")
        or target.get("role")
    )
    if topology_target:
        topology = load_output_topology()
        readiness = _active_speaker_playback_readiness_payload(raw)
        level = _active_speaker_calibration_level_payload()
        plan = build_topology_tone_plan(
            topology,
            readiness_report=readiness,
            speaker_group_id=raw.get("speaker_group_id")
            or target.get("speaker_group_id"),
            role=(
                raw.get("role")
                or raw.get("driver_role")
                or target.get("role")
                or target.get("driver_role")
            ),
            requested_level_dbfs=level.get("test_signal", {}).get(
                "requested_level_dbfs"
            ),
            requested_duration_ms=raw.get("duration_ms"),
        )
    else:
        plan = _active_speaker_tone_plan_payload(raw)
    safe_session = load_safe_playback_state()
    wants_audio = bool(raw.get("audio"))
    backend = enabled_audio_backend() if wants_audio else None
    if wants_audio and backend is None:
        plan = {
            **plan,
            "status": "blocked",
            "playback_allowed": False,
            "would_play": False,
            "issues": [
                *(plan.get("issues") if isinstance(plan.get("issues"), list) else []),
                {
                    "severity": "blocker",
                    "code": "audio_backend_not_enabled",
                    "message": (
                        "audible channel tests require explicit lab backend "
                        "enablement"
                    ),
                },
            ],
        }
    playback = start_tone_playback(
        plan,
        safe_session=safe_session,
        backend=backend,
        allow_audio=wants_audio,
    )
    session = record_safe_playback_result(playback)
    issue_codes = ",".join(
        str(issue.get("code") or "issue")[:80]
        for issue in playback.get("issues", [])
        if isinstance(issue, dict)
    )[:240]
    logger.info(
        "event=sound.active_speaker_tone_playback status=%s backend=%s "
        "source=%s side=%s group_id=%s driver_role=%s output_index=%s "
        "level_dbfs=%s duration_ms=%s audio_requested=%s audio_emitted=%s "
        "blockers=%d issue_codes=%s artifact=%s quiet_start=%s",
        playback.get("status"),
        playback.get("backend"),
        plan.get("source") or "preset",
        playback.get("target", {}).get("side"),
        playback.get("target", {}).get("speaker_group_id"),
        playback.get("target", {}).get("driver_role"),
        playback.get("target", {}).get("output_index"),
        playback.get("tone", {}).get("level_dbfs"),
        playback.get("tone", {}).get("duration_ms"),
        wants_audio,
        bool(playback.get("audio_emitted")),
        len(playback.get("issues") or []),
        issue_codes or "-",
        (playback.get("artifact") or {}).get("wav_basename"),
        (session.get("quiet_start") or {}).get("status"),
    )
    return {
        "plan": plan,
        "playback": playback,
        "session": session,
    }


def _active_speaker_floor_audio_result_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Record the operator's result after an audible floor-level test."""

    from jasper.active_speaker.safe_playback import record_floor_audio_operator_result

    if not isinstance(raw, dict):
        raise ValueError("floor audio result request must be an object")
    outcome = str(raw.get("outcome") or "").strip().lower()
    playback_id = str(raw.get("playback_id") or "").strip() or None
    state = record_floor_audio_operator_result(
        outcome=outcome,
        playback_id=playback_id,
    )
    quiet = state.get("quiet_start") if isinstance(state.get("quiet_start"), dict) else {}
    logger.info(
        "event=sound.active_speaker_floor_audio_result status=%s outcome=%s "
        "playback_id=%s floor_confirmed=%s issue_codes=%s",
        quiet.get("status"),
        outcome,
        playback_id or quiet.get("pending_playback_id") or "-",
        bool(quiet.get("floor_audio_confirmed")),
        ",".join(
            str(issue.get("code") or "issue")[:80]
            for issue in state.get("issues", [])
            if isinstance(issue, dict)
        )[:240] or "-",
    )
    return state


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
            if path == "/output-topology":
                try:
                    self._send_json(_output_topology_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception("event=sound.output_topology result=error")
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/environment":
                try:
                    self._send_json(_active_speaker_environment_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_environment result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/safe-playback":
                try:
                    self._send_json(_active_speaker_safe_playback_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_safe_playback result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/calibration-level":
                try:
                    self._send_json(_active_speaker_calibration_level_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_calibration_level result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/bringup-preflight":
                try:
                    self._send_json(_active_speaker_bringup_preflight_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_bringup_preflight result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/startup-load":
                try:
                    self._send_json(_active_speaker_startup_load_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_startup_load result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/commissioning-rehearsal":
                try:
                    self._send_json(_active_speaker_commissioning_rehearsal_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_commissioning_rehearsal "
                        "result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/staged-config":
                try:
                    self._send_json(_active_speaker_staged_config_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_staged_config result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/channel-identity":
                try:
                    self._send_json(_active_speaker_channel_identity_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_channel_identity result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/tone-targets":
                try:
                    self._send_json(_active_speaker_tone_targets_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_tone_targets result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
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
                "/active-speaker/arm",
                "/active-speaker/stop",
                "/active-speaker/calibration-level",
                "/active-speaker/channel-identity",
                "/active-speaker/channel-protection",
                "/active-speaker/playback-readiness",
                "/active-speaker/stage-config",
                "/active-speaker/check-path-safety",
                "/active-speaker/load-startup-config",
                "/active-speaker/rollback-startup-config",
                "/active-speaker/tone-plan",
                "/active-speaker/play-tone",
                "/active-speaker/floor-audio-result",
                "/output-topology",
                "/profiles/save",
                "/profiles/rename",
                "/profiles/delete",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            try:
                raw = self._read_json()
                if path == "/active-speaker/arm":
                    self._send_json(_active_speaker_arm_payload())
                    return
                if path == "/active-speaker/stop":
                    self._send_json(_active_speaker_stop_payload())
                    return
                if path == "/active-speaker/calibration-level":
                    self._send_json(_active_speaker_calibration_level_payload(raw))
                    return
                if path == "/active-speaker/channel-identity":
                    try:
                        self._send_json(
                            _active_speaker_channel_identity_save_payload(raw)
                        )
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_channel_identity "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/channel-protection":
                    try:
                        self._send_json(
                            _active_speaker_channel_protection_save_payload(raw)
                        )
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_channel_protection "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/playback-readiness":
                    self._send_json(_active_speaker_playback_readiness_payload(raw))
                    return
                if path == "/active-speaker/stage-config":
                    self._send_json(_active_speaker_stage_config_payload(raw))
                    return
                if path == "/active-speaker/check-path-safety":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_check_path_safety_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/load-startup-config":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_load_startup_config_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/rollback-startup-config":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_rollback_startup_config_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/tone-plan":
                    self._send_json(_active_speaker_tone_plan_payload(raw))
                    return
                if path == "/active-speaker/play-tone":
                    self._send_json(_active_speaker_tone_playback_payload(raw))
                    return
                if path == "/active-speaker/floor-audio-result":
                    self._send_json(_active_speaker_floor_audio_result_payload(raw))
                    return
                if path == "/output-topology":
                    try:
                        self._send_json(_save_output_topology_payload(raw))
                    except OSError as e:
                        logger.exception(
                            "event=sound.output_topology_save result=error "
                            "error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
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
