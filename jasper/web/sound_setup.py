# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
  GET  /active-speaker/commission-state per-driver commission + Stage-5 ramp state
  GET  /active-speaker/commissioning-view backend-owned setup view/actions/copy
  GET  /active-speaker/design-draft saved speaker design/research evidence
  GET  /active-speaker/crossover-preview saved no-audio crossover preview
  GET  /active-speaker/measurements saved driver and summed validation evidence
  GET  /active-speaker/baseline-profile active baseline compile/apply state
  POST /preview  preview a draft profile's response without touching live audio
  POST /live-draft apply a draft to live audio without persisting
  POST /audition validate and load a draft/bypass config without persisting
  POST /active-speaker/stop      stop the no-audio active-speaker session
  POST /active-speaker/calibration-level update backend-owned level guard
  POST /active-speaker/stage-config stage protected startup config
  POST /active-speaker/check-path-safety inspect and persist no-audio path evidence
  POST /active-speaker/load-startup-config load protected startup config, no sound
  POST /active-speaker/rollback-startup-config restore pre-load config, no sound
  POST /active-speaker/commission-load arm a driver at the protected floor (silent)
  POST /active-speaker/commission-rollback re-mute: reload the all-muted staged config
  POST /active-speaker/commission-ramp-step one gated audible Stage-5 gain step
  POST /active-speaker/commission-ramp-ack record the operator's verdict for the step
  POST /active-speaker/commission-ramp-abort hard Stop: re-mute + reset the ramp
  POST /active-speaker/design-draft persist speaker design/research evidence
  POST /active-speaker/crossover-preview persist no-audio crossover preview
  POST /active-speaker/driver-measurement record one measured driver result
  POST /active-speaker/driver-capture analyze + record one phone-mic driver WAV
  POST /active-speaker/summed-test run combined-driver test artifact/playback
  POST /active-speaker/summed-validation record summed crossover validation
  POST /active-speaker/summed-capture analyze + record one phone-mic summed WAV
  POST /active-speaker/baseline-profile compile active baseline YAML
  POST /active-speaker/baseline-profile/apply explicitly apply active baseline
  POST /active-speaker/channel-identity mark/clear physical identity evidence
  POST /active-speaker/channel-protection mark/clear tweeter protection evidence
  POST /output-topology save a complete speaker/DAC topology draft
  POST /settings persist global sound settings
  POST /volume-floor/audition start/update a non-persistent 1% floor tone
  POST /volume-floor/stop stop the non-persistent 1% floor tone
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
import base64
import binascii
import html
import json
import logging
import math
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
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
from jasper.output_hardware import load_state as load_output_hardware_state
from jasper.active_speaker.commission_wiring import (
    commission_seams,
    read_current_config_path,
    resolve_commission_inputs,
    write_commission_path_safety,
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
    VOLUME_FLOOR_MAX_DB,
    VOLUME_FLOOR_MIN_DB,
    load_sound_settings,
    output_trim_db as _output_trim,  # aliased so local `output_trim_db` vars don't shadow it
    save_sound_settings,
)
from jasper.volume_curve import percent_to_db

from ._common import (
    begin_request,
    bonded_follower_active,
    bonded_follower_leader_web_url,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    json_island,
    reject_csrf,
    send_html_response,
)

logger = logging.getLogger(__name__)

_FOLLOWER_BLOCKED_CONTENT_DSP_POSTS = frozenset({
    "/apply",
    "/audition",
    "/live-draft",
    "/settings",
    "/volume-floor/audition",
    "/volume-floor/stop",
    "/profiles/save",
    "/profiles/rename",
    "/profiles/delete",
})

DEFAULT_CONFIG_DIR = "/var/lib/camilladsp/configs"
MAX_JSON_BYTES = 64 * 1024
MAX_CAPTURE_JSON_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_WAV_BYTES = 3 * 1024 * 1024
MAX_CAPTURE_STORED_FILES = 24
MAX_CAPTURE_STORAGE_BYTES = 32 * 1024 * 1024
CAPTURE_FILE_MODE = 0o640
DEFAULT_ACTIVE_SPEAKER_CAPTURE_DIR = Path("/var/lib/jasper/active_speaker_captures")
ACTIVE_SPEAKER_CAPTURE_DIR_ENV = "JASPER_ACTIVE_SPEAKER_CAPTURE_DIR"
LIVE_DRAFT_UNAVAILABLE_LOG_INTERVAL_SEC = 30.0
VOLUME_FLOOR_TONE_ALSA_DEVICE = "correction_substream"
VOLUME_FLOOR_TONE_FREQ_HZ = 1000.0
VOLUME_FLOOR_TONE_SOURCE_DBFS = -12.0
VOLUME_FLOOR_TONE_CHUNK_DURATION_S = 8.0
VOLUME_FLOOR_TONE_MAX_DURATION_S = 10 * 60.0
VOLUME_FLOOR_TONE_SAMPLE_RATE = 48000
VOLUME_FLOOR_TONE_STARTUP_CHECK_S = 0.08

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
            "volume_floor_min_db": VOLUME_FLOOR_MIN_DB,
            "volume_floor_max_db": VOLUME_FLOOR_MAX_DB,
        },
        "last_dsp_apply": last_dsp_apply,
        "dsp_write_epoch": dsp_write_epoch_from_state(last_dsp_apply),
    }
    if include_library:
        payload["profile_library"] = profile_library_payload(
            load_profile_library(library_path)
        )
    return payload


def _output_hardware_dict() -> dict[str, Any] | None:
    """Serializable form of the live output-hardware state.

    ``load_state`` returns a frozen ``OutputHardwareState`` — or ``None`` when
    no state file exists yet. These payloads are emitted with plain
    ``json.dumps`` (``_send_json``), which can't encode the dataclass, so
    embedding it raw 502s ``/sound/output-topology`` on any Pi that has a
    populated state file. ``to_dict`` is the single conversion boundary.

    The page keeps this envelope key separate from the topology's own
    ``hardware`` block: topology hardware is the saved speaker contract,
    while this object is the currently observed attachment state. It is also
    mirrored by ``/state`` as ``audio.output_hardware``.
    """
    hardware = load_output_hardware_state()
    return hardware.to_dict() if hardware is not None else None


def _output_topology_payload() -> dict[str, Any]:
    topology = load_output_topology()

    return {
        "output_topology": topology.to_dict(include_evaluation=True),
        "output_hardware": _output_hardware_dict(),
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
        "active_playback_route": _active_speaker_playback_route_payload(topology),
    }


def _save_output_topology_payload(raw: dict[str, Any]) -> dict[str, Any]:
    raw_topology = raw.get("output_topology", raw)
    topology = OutputTopology.from_mapping(raw_topology)
    topology, guards_changed = _active_speaker_request_missing_software_guards(topology)
    save_output_topology(topology)
    evaluation = topology.evaluation()
    logger.info(
        "event=sound.output_topology_save topology_id=%s status=%s "
        "device_id=%s groups=%d assigned_outputs=%d blockers=%d warnings=%d "
        "software_guards_requested=%s",
        topology.topology_id,
        evaluation["status"],
        topology.hardware.device_id,
        len(topology.speaker_groups),
        evaluation["assigned_output_count"],
        len(evaluation["blockers"]),
        len(evaluation["warnings"]),
        guards_changed,
    )
    return {
        "output_topology": topology.to_dict(include_evaluation=True),
        "output_hardware": _output_hardware_dict(),
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
        "active_playback_route": _active_speaker_playback_route_payload(topology),
    }


def _active_speaker_playback_route_payload(
    topology: OutputTopology | None = None,
) -> dict[str, Any]:
    """Return the active-speaker runtime route capability for the saved topology."""

    from jasper.active_speaker.playback_route import active_playback_route_capability

    return active_playback_route_capability(
        topology or load_output_topology()
    ).to_dict()


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
        "output_hardware": _output_hardware_dict(),
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
        "output_hardware": _output_hardware_dict(),
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


def _carrier_refusal(exc: BaseException):
    """Return the ``CarrierCannotHostEq`` behind ``exc`` if the loaded
    CamillaDSP graph refused to host preference EQ, else ``None``.

    A refusal arrives RAW from the live-draft path and the durable path's
    pre-lock fast-check; it arrives wrapped as ``DspApplyError`` (its
    ``__cause__``) only from the durable path's in-lock re-check in the rare
    concurrent-swap race. Handling both maps either to a typed 200 body instead
    of a 502 — ``jasper/dsp_apply.py`` stays untouched.
    """
    from jasper.sound.graph_carrier import CarrierCannotHostEq

    if isinstance(exc, CarrierCannotHostEq):
        return exc
    cause = exc.__cause__
    if isinstance(cause, CarrierCannotHostEq):
        return cause
    return None


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
        "event=sound.settings headroom_trim=%.1f match_loudness=%s "
        "volume_floor_db=%.1f",
        settings.headroom_trim_db,
        settings.match_loudness,
        settings.volume_floor_db,
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
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        logger.exception("sound settings re-apply failed")
        payload["warning"] = f"Saved, but applying to the speaker failed: {e}"
        return payload
    payload["active_config_path"] = str(out_path)
    payload["preserved_room_peqs"] = apply_state.room_peq_count or 0
    payload["last_dsp_apply"] = apply_state.to_dict()
    payload["dsp_write_epoch"] = apply_state.op_id
    try:
        reconciled = await _reconcile_volume_curve_after_settings(
            camilla_factory=camilla_factory,
        )
        if reconciled:
            payload["volume_reconciled"] = True
    except (AttributeError, OSError, RuntimeError) as e:
        logger.warning("volume floor saved but volume reconcile failed: %s", e)
        payload["volume_warning"] = (
            "Saved, but the current volume will use the new floor on the next "
            f"volume change: {e}"
        )
    return payload


async def _reconcile_volume_curve_after_settings(
    *,
    camilla_factory: Callable[[], Any] = _camilla,
) -> bool:
    """Apply the newly saved floor to the current listening level when safe.

    The source-aware coordinator owns the same guardrails as the normal volume
    path. ``maybe_reconcile_camilla`` only writes for camilla-master sources
    (idle/AirPlay/USB), so changing the floor cannot accidentally unguard a
    Spotify/Bluetooth push-mode handoff.
    """
    from jasper.renderer import RendererClient
    from jasper.volume_coordinator import VolumeCoordinator
    from jasper.volume_persistence import VolumePersistence

    coord = VolumeCoordinator(
        camilla=camilla_factory(),
        persistence=VolumePersistence(
            os.environ.get(
                "JASPER_VOLUME_STATE_PATH",
                "/var/lib/jasper/speaker_volume.json",
            )
        ),
        backend=RendererClient(
            librespot_state_path=os.environ.get(
                "JASPER_LIBRESPOT_STATE",
                "/run/librespot/state.json",
            ),
        ),
    )
    try:
        coord.load_persisted_level()
        await coord.maybe_reconcile_camilla()
        return True
    finally:
        await coord.aclose()


def _volume_floor_tone_wav_path() -> Path:
    from jasper.correction.playback import _ensure_tone_wav

    return _ensure_tone_wav(
        freq_hz=VOLUME_FLOOR_TONE_FREQ_HZ,
        duration_s=VOLUME_FLOOR_TONE_CHUNK_DURATION_S,
        dbfs=VOLUME_FLOOR_TONE_SOURCE_DBFS,
        sample_rate=VOLUME_FLOOR_TONE_SAMPLE_RATE,
        cache_dir=Path(
            os.environ.get(
                "JASPER_VOLUME_FLOOR_TONE_DIR",
                "/var/lib/jasper/correction/tones",
            )
        ),
    )


class _LoopingVolumeFloorTone:
    """Small `aplay` loop independent of per-request asyncio loops."""

    def __init__(
        self,
        wav_path: str | Path,
        *,
        on_finish: Callable[[Any, str], None] | None = None,
        alsa_device: str = VOLUME_FLOOR_TONE_ALSA_DEVICE,
        max_duration_s: float = VOLUME_FLOOR_TONE_MAX_DURATION_S,
    ) -> None:
        self._wav_path = Path(wav_path)
        self._alsa_device = alsa_device
        self._max_duration_s = max_duration_s
        self._on_finish = on_finish
        self._stop = threading.Event()
        self._proc_lock = threading.Lock()
        self._error_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._error: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="jts-volume-floor-tone",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate_current()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    @property
    def error(self) -> str | None:
        with self._error_lock:
            return self._error

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set() and not self.error

    def _set_error(self, message: str) -> None:
        with self._error_lock:
            self._error = message

    def _terminate_current(self) -> None:
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.75)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=0.75)
            except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
                pass
        except ProcessLookupError:
            pass

    def _run(self) -> None:
        deadline = time.monotonic() + self._max_duration_s
        finish_reason = ""
        try:
            while not self._stop.is_set():
                if time.monotonic() >= deadline:
                    finish_reason = "timeout"
                    self._set_error("volume floor tone safety timeout")
                    break
                try:
                    proc = subprocess.Popen(
                        [
                            "aplay",
                            "-D",
                            self._alsa_device,
                            "-q",
                            str(self._wav_path),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError as exc:
                    finish_reason = "error"
                    self._set_error(str(exc))
                    logger.exception(
                        "event=sound.volume_floor_tone action=play result=error"
                    )
                    break

                with self._proc_lock:
                    self._proc = proc

                rc: int | None = None
                while True:
                    rc = proc.poll()
                    if rc is not None:
                        break
                    if self._stop.is_set():
                        self._terminate_current()
                        break
                    if time.monotonic() >= deadline:
                        finish_reason = "timeout"
                        self._set_error("volume floor tone safety timeout")
                        self._terminate_current()
                        break
                    time.sleep(0.05)

                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None

                if self._stop.is_set():
                    finish_reason = "stopped"
                    break
                if finish_reason:
                    break
                if rc not in (0, None):
                    finish_reason = "error"
                    self._set_error(f"aplay exited with rc={rc}")
                    logger.warning(
                        "event=sound.volume_floor_tone action=play result=error rc=%s",
                        rc,
                    )
                    break
                # Natural EOF of the short cached WAV: immediately loop it.
        finally:
            if finish_reason in {"error", "timeout"} and self._on_finish:
                self._on_finish(self, finish_reason)


class _VolumeFloorToneSession:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._camilla_op_lock = threading.Lock()
        self._runner: Any | None = None
        self._original_db: float | None = None
        self._original_mute: bool | None = None
        self._floor_db: float | None = None
        self._camilla_factory: Callable[[], Any] | None = None
        self._starting = False
        self._cancel_start = False
        self._generation = 0

    async def _acquire_camilla_op_lock(self) -> None:
        await asyncio.to_thread(self._camilla_op_lock.acquire)

    async def start_or_update(
        self,
        raw: dict[str, Any],
        *,
        camilla_factory: Callable[[], Any],
        runner_factory: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        settings = SoundSettings.from_mapping({
            **load_sound_settings().to_dict(),
            "volume_floor_db": raw.get("volume_floor_db"),
        })
        floor_db = settings.volume_floor_db
        await self._stop_if_finished(camilla_factory=camilla_factory)

        runner_factory = runner_factory or _LoopingVolumeFloorTone
        started_runner: Any | None = None
        runner: Any | None
        generation: int
        action: str
        while True:
            with self._lock:
                if self._runner is not None:
                    runner = self._runner
                    generation = self._generation
                    action = "update"
                    break
                if not self._starting:
                    self._starting = True
                    self._cancel_start = False
                    runner = None
                    generation = self._generation
                    action = "start"
                    break
            await asyncio.sleep(0.02)

        if action == "start":
            original: tuple[float, bool] | None = None
            acquired_camilla_op = False
            try:
                runner = runner_factory(
                    _volume_floor_tone_wav_path(),
                    on_finish=self._runner_finished,
                )
                await self._acquire_camilla_op_lock()
                acquired_camilla_op = True
                camilla = camilla_factory()
                original = await camilla.get_volume_and_mute(best_effort=True)
                if original is None:
                    raise RuntimeError("CamillaDSP volume state is unavailable")
                await camilla.set_volume_db(percent_to_db(1, floor_db=floor_db))
                await camilla.set_main_mute(False)
                with self._lock:
                    cancelled = self._cancel_start
                    self._starting = False
                    self._cancel_start = False
                    if not cancelled:
                        self._original_db, self._original_mute = original
                        self._camilla_factory = camilla_factory
                        self._runner = runner
                        self._floor_db = floor_db
                        self._generation += 1
                if cancelled:
                    await self._restore_snapshot(
                        camilla_factory=camilla_factory,
                        original_db=original[0],
                        original_mute=original[1],
                    )
                    return self._inactive_payload(
                        floor_db=floor_db,
                        status="stopped",
                    )
                try:
                    runner.start()
                except (OSError, RuntimeError):
                    with self._lock:
                        if self._runner is runner:
                            self._clear_active_locked()
                            self._generation += 1
                    await self._restore_snapshot(
                        camilla_factory=camilla_factory,
                        original_db=original[0],
                        original_mute=original[1],
                    )
                    original = None
                    raise
                started_runner = runner
            except (OSError, RuntimeError):
                with self._lock:
                    self._starting = False
                    self._cancel_start = False
                if original is not None:
                    await self._restore_snapshot(
                        camilla_factory=camilla_factory,
                        original_db=original[0],
                        original_mute=original[1],
                    )
                raise
            finally:
                if acquired_camilla_op:
                    self._camilla_op_lock.release()
        else:
            await self._acquire_camilla_op_lock()
            try:
                with self._lock:
                    active = self._runner is runner and self._generation == generation
                if not active:
                    return self._inactive_payload(
                        floor_db=floor_db,
                        status="stale",
                    )
                camilla = camilla_factory()
                await camilla.set_volume_db(percent_to_db(1, floor_db=floor_db))
                await camilla.set_main_mute(False)
                with self._lock:
                    if self._runner is runner and self._generation == generation:
                        self._floor_db = floor_db
                    else:
                        return self._inactive_payload(
                            floor_db=floor_db,
                            status="stale",
                        )
            finally:
                self._camilla_op_lock.release()

        if started_runner is not None:
            await asyncio.sleep(VOLUME_FLOOR_TONE_STARTUP_CHECK_S)
            error = getattr(started_runner, "error", None)
            if error:
                await self.stop(
                    camilla_factory=camilla_factory,
                    reason="startup_failed",
                )
                raise RuntimeError(str(error))

        logger.info(
            "event=sound.volume_floor_tone action=%s floor_db=%.1f result=ok",
            action,
            floor_db,
        )
        return {
            "ok": True,
            "active": True,
            "continuous": True,
            "status": "started" if action == "start" else "updated",
            "volume_floor_db": floor_db,
            "percent": 1,
            "db": round(percent_to_db(1, floor_db=floor_db), 3),
        }

    def _inactive_payload(self, *, floor_db: float, status: str) -> dict[str, Any]:
        return {
            "ok": True,
            "active": False,
            "continuous": False,
            "status": status,
            "volume_floor_db": floor_db,
            "percent": 1,
            "db": round(percent_to_db(1, floor_db=floor_db), 3),
        }

    async def stop(
        self,
        *,
        camilla_factory: Callable[[], Any],
        reason: str,
    ) -> dict[str, Any]:
        original_db: float | None
        original_mute: bool | None
        with self._lock:
            starting = self._starting
            if starting:
                self._cancel_start = True
            runner = self._runner
            floor_db = self._floor_db
            original_db = self._original_db
            original_mute = self._original_mute
            if runner is not None:
                self._clear_active_locked()
                self._generation += 1
        if runner is not None:
            runner.stop()
        if original_db is not None and original_mute is not None:
            await self._acquire_camilla_op_lock()
            try:
                await self._restore_snapshot(
                    camilla_factory=camilla_factory,
                    original_db=original_db,
                    original_mute=original_mute,
                )
            finally:
                self._camilla_op_lock.release()
        status = "stopped" if runner is not None or starting else "idle"
        logger.info(
            "event=sound.volume_floor_tone action=stop reason=%s status=%s",
            reason,
            status,
        )
        payload = {"ok": True, "active": False, "status": status, "reason": reason}
        if floor_db is not None:
            payload["volume_floor_db"] = floor_db
        return payload

    async def _stop_if_finished(
        self,
        *,
        camilla_factory: Callable[[], Any],
    ) -> None:
        with self._lock:
            runner = self._runner
            finished = runner is not None and not getattr(runner, "running", False)
        if finished:
            await self.stop(camilla_factory=camilla_factory, reason="expired")

    def _runner_finished(self, runner: Any, reason: str) -> None:
        original_db: float | None
        original_mute: bool | None
        with self._lock:
            if self._runner is not runner:
                return
            camilla_factory = self._camilla_factory
            floor_db = self._floor_db
            original_db = self._original_db
            original_mute = self._original_mute
            self._clear_active_locked()
            self._generation += 1
        if camilla_factory is None:
            return
        try:
            asyncio.run(
                self._restore_after_runner_finish(
                    camilla_factory=camilla_factory,
                    reason=reason,
                    floor_db=floor_db,
                    original_db=original_db,
                    original_mute=original_mute,
                )
            )
        except (OSError, RuntimeError):
            logger.exception(
                "event=sound.volume_floor_tone action=restore result=error "
                "reason=%s",
                reason,
            )

    async def _restore_after_runner_finish(
        self,
        *,
        camilla_factory: Callable[[], Any],
        reason: str,
        floor_db: float | None,
        original_db: float | None,
        original_mute: bool | None,
    ) -> None:
        if original_db is not None and original_mute is not None:
            await self._acquire_camilla_op_lock()
            try:
                await self._restore_snapshot(
                    camilla_factory=camilla_factory,
                    original_db=original_db,
                    original_mute=original_mute,
                )
            finally:
                self._camilla_op_lock.release()
        logger.warning(
            "event=sound.volume_floor_tone action=restore reason=%s floor_db=%s",
            reason,
            "" if floor_db is None else f"{floor_db:.1f}",
        )

    async def _restore_snapshot(
        self,
        *,
        camilla_factory: Callable[[], Any],
        original_db: float,
        original_mute: bool,
    ) -> None:
        camilla = camilla_factory()
        if original_mute:
            await camilla.set_main_mute(True, best_effort=True)
            await camilla.set_volume_db(original_db, best_effort=True)
        else:
            await camilla.set_volume_db(original_db, best_effort=True)
            await camilla.set_main_mute(False, best_effort=True)

    def _clear_active_locked(self) -> None:
        self._runner = None
        self._original_db = None
        self._original_mute = None
        self._floor_db = None
        self._camilla_factory = None


_VOLUME_FLOOR_TONE_SESSION = _VolumeFloorToneSession()


async def _audition_volume_floor(
    raw: dict[str, Any],
    *,
    camilla_factory: Callable[[], Any] = _camilla,
    session: _VolumeFloorToneSession | None = None,
    runner_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Start or update a held tone at the proposed 1% volume floor.

    This is intentionally non-persistent. The user can drag the floor slider,
    hear the 1% reference continuously, and only the existing /settings save
    path commits the chosen floor.
    """
    return await (session or _VOLUME_FLOOR_TONE_SESSION).start_or_update(
        raw,
        camilla_factory=camilla_factory,
        runner_factory=runner_factory,
    )


async def _stop_volume_floor_tone(
    *,
    camilla_factory: Callable[[], Any] = _camilla,
    reason: str = "stop",
    session: _VolumeFloorToneSession | None = None,
) -> dict[str, Any]:
    return await (session or _VOLUME_FLOOR_TONE_SESSION).stop(
        camilla_factory=camilla_factory,
        reason=reason,
    )


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
    from jasper.sound.graph_carrier import carrier_for_loaded_config

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

        carrier = carrier_for_loaded_config(current_path, config_dir=config_path)
        result = carrier.reemit(
            profile,
            profile_id=f"live-{time.time_ns()}",
            output_trim_db=output_trim_db,
        )
        yaml = result.yaml

        try:
            await loader(yaml, best_effort=False)
        except Exception as e:  # noqa: BLE001
            _log_live_draft_unavailable(
                reason="active_config_raw_failed",
                output_trim_db=output_trim_db,
                room_peq_count=result.room_peq_count,
                sound_filter_count=sound_filter_count,
                error=e,
            )
            return _live_payload(
                status="unavailable",
                method="active_config_raw_failed",
                current_epoch=current_epoch,
                room_peq_count=result.room_peq_count,
                active_config_path=current_path,
            )

        logger.info(
            "event=sound.live_draft result=live output_trim=%.1f "
            "room_peqs=%d sound_filters=%d active_anchor=%s epoch=%s",
            output_trim_db,
            result.room_peq_count,
            sound_filter_count,
            current_path,
            current_epoch,
        )
        return _live_payload(
            status="live",
            method="active_config_raw",
            current_epoch=current_epoch,
            room_peq_count=result.room_peq_count,
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
        sound_audition_config_path,
        sound_config_path,
    )
    from jasper.sound.graph_carrier import carrier_for_loaded_config
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

    # Fast pre-check: refuse a non-hostable graph BEFORE the apply transaction
    # so a STEADY-STATE active speaker's EQ apply records no prepare_failed
    # state (a refusal is a handled "blocked" outcome, not a DSP failure, and
    # we don't want a persistent jasper-doctor / /state WARN). This is an
    # optimization only — the AUTHORITATIVE hostability gate runs under the
    # dsp-apply lock in _prepare_config, because the loaded config can change
    # between this read and lock acquisition.
    pre_path = await cam.get_config_file_path(best_effort=False)
    if not pre_path:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    pre_carrier = carrier_for_loaded_config(pre_path, config_dir=config_path)
    # Dry-run (out_path=None -> writes nothing) any carrier that could refuse, so
    # a blocked EQ apply records NO prepare_failed state (SF-2). Structurally
    # unhostable carriers (startup/commissioning/bonded/unknown) refuse cheaply;
    # the SOLO active baseline can host EQ but still refuses if its saved
    # crossover/measurement evidence has gone missing — both must surface BEFORE
    # the apply transaction. A stereo host always succeeds, so it is skipped (no
    # double emit). The authoritative re-emit to out_path runs under the
    # dsp-apply lock in _prepare_config.
    if not pre_carrier.can_host_eq or pre_carrier.kind == "active":
        pre_carrier.reemit(profile)  # raises CarrierCannotHostEq, writes nothing

    async def _prepare_config() -> dict[str, Any]:
        # Re-resolve the carrier UNDER the dsp-apply lock against the config
        # that is actually loaded now. An active-startup load shares this lock
        # and could have swapped a roleful graph in after the pre-check;
        # re-asserting here is what guarantees we never re-emit a stereo config
        # over an active crossover (a TOCTOU crossover-drop). In the rare
        # genuine race this raises through apply_dsp_config, which records a
        # real prepare failure and the route still maps it to the typed
        # "blocked" 200 via the refusal's __cause__.
        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            raise RuntimeError("CamillaDSP did not report a loaded config path")
        carrier = carrier_for_loaded_config(current_path, config_dir=config_path)
        result = carrier.reemit(
            profile,
            out_path=out_path,
            profile_id=profile_id,
            output_trim_db=output_trim_db,
        )
        return {
            "prior_config_path": current_path,
            "room_peq_count": result.room_peq_count,
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


def _follower_sound_html(csrf_token: str = "") -> bytes:
    """Render a bonded active follower's /sound/ page.

    Distributed-active Slice 4: a bonded follower delegates the PROGRAM domain
    (content EQ, room correction, volume shaping) to the pair leader, but it
    still owns its LOCAL driver domain (Layer A — the per-driver crossover /
    limiter / tweeter high-pass that protects the DAC it drives). So the page
    keeps the delegation card AND mounts the same active-speaker setup UI that
    main.js renders on a solo box, making the card's "local crossover ... stays
    with the speaker that owns the DAC path" promise literally true.

    The follower island tells main.js to boot in follower mode: it renders only
    the active-speaker section (expanded as the primary content) and omits the
    Off/Saved/Draft content-EQ editor and now-playing plot, which live only on
    the leader. Content-DSP POSTs still 409 (``_FOLLOWER_BLOCKED_CONTENT_DSP_POSTS``);
    the active-speaker commissioning/crossover endpoints are allowed (invariant 6).
    """
    leader_sound_url = bonded_follower_leader_web_url("/sound/")
    leader_link = (
        '<a class="btn btn--primary" href="'
        + html.escape(leader_sound_url)
        + '">Open leader sound</a>'
        if leader_sound_url else ""
    )
    follower_island = json_island("sound-follower-data", {"follower": True})
    body = f"""
<header class="app-header">
  <div class="app-header__row">
    <a class="icon-button" id="back" href="/" aria-label="Home">
      <svg class="ico" aria-hidden="true"><use href="#icon-back"></use></svg>
    </a>
    <h1 class="app-header__title">Sound profile</h1>
    <span></span>
  </div>
</header>
<main class="page">
  <section class="info-card info-card--accent" role="note">
    <h2 class="section__title">Sound is controlled by the pair leader</h2>
    <p class="form-hint">This speaker is an active follower, so content EQ,
    room correction, and volume shaping are rendered by the leader while the
    pair is active. Local crossover and driver-protection work stays with the
    speaker that owns the DAC path.</p>
    <div class="actions">
      {leader_link}
      <a class="btn" href="/rooms/">Manage pair</a>
    </div>
  </section>
  <div id="view-body"></div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</main>
{follower_island}
<script type="module" src="/assets/sound-profile/js/main.js"></script>
"""
    return canonical_page(
        "Sound profile", body, csrf_token=csrf_token,
        page_css_href="/assets/sound-profile/sound.css",
    )


def _index_html(csrf_token: str = "") -> bytes:
    if bonded_follower_active():
        return _follower_sound_html(csrf_token)
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

    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.staging import stage_protected_startup_config

    if not isinstance(raw, dict):
        raise ValueError("stage config request must be an object")
    playback_device = raw.get("playback_device")
    if playback_device is not None and not isinstance(playback_device, str):
        raise ValueError("playback_device must be a string")
    topology = load_output_topology()
    design_draft = load_design_draft()
    crossover_preview = load_crossover_preview(current_design_draft=design_draft)
    payload = stage_protected_startup_config(
        topology,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
    )
    blocker_count = sum(
        1 for issue in payload.get("issues") or []
        if isinstance(issue, dict) and issue.get("severity") == "blocker"
    )
    logger.info(
        "event=sound.active_speaker_stage_config status=%s topology_id=%s "
        "preset_id=%s preview_status=%s config=%s blockers=%d",
        payload.get("status"),
        payload.get("topology", {}).get("topology_id"),
        payload.get("preset", {}).get("preset_id"),
        crossover_preview.get("status"),
        payload.get("config", {}).get("basename"),
        blocker_count,
    )
    return payload


def _active_speaker_tone_backend_status(
    topology: Any | None = None,
) -> dict[str, Any]:
    """Return the explicit lab tone backend status."""

    from jasper.active_speaker.playback import tone_backend_status

    resolved_topology = topology or load_output_topology()
    status = tone_backend_status()
    return {
        **status,
        "default_pcm_source": "explicit_lab_pcm",
        "playback_device": status.get("test_pcm"),
        "channel_count": int(resolved_topology.hardware.physical_output_count or 0),
        "requires_protected_startup": True,
    }


def _active_speaker_request_missing_software_guards(
    topology: OutputTopology,
) -> tuple[OutputTopology, bool]:
    """Persist the no-hardware path for protected active-speaker channels."""

    updated = topology
    changed = False
    for group in topology.speaker_groups:
        if not str(group.mode or "").startswith("active_"):
            continue
        for channel in group.channels:
            if not channel.protection_required:
                continue
            if channel.protection_status in {"present", "software_guard_requested"}:
                continue
            updated = set_channel_protection_status(
                updated,
                speaker_group_id=group.id,
                role=channel.role,
                protection_status="software_guard_requested",
            )
            changed = True
    if changed:
        save_output_topology(updated)
    return updated, changed


def _config_paths_match(a: str | Path | None, b: str | Path | None) -> bool:
    if not a or not b:
        return False
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except (OSError, RuntimeError):
        return Path(str(a)) == Path(str(b))


def _active_speaker_safe_playback_payload() -> dict[str, Any]:
    """Return the current no-audio active-speaker safety session."""

    from jasper.active_speaker.safe_playback import load_safe_playback_state

    return load_safe_playback_state()


def _active_speaker_calibration_level_payload(
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return or update the backend-owned active-speaker test-volume state."""

    from jasper.active_speaker.calibration_level import (
        load_calibration_level_state,
        update_calibration_level_state,
    )

    if raw is None:
        return load_calibration_level_state()
    if not isinstance(raw, dict):
        raise ValueError("calibration level request must be an object")
    action = str(raw.get("action") or "set")
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


def _active_speaker_bringup_preflight_payload() -> dict[str, Any]:
    """Return guided-vs-manual active-speaker bring-up readiness."""

    from jasper.active_speaker.bringup import build_bringup_preflight

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
        tone_backend=_active_speaker_tone_backend_status(topology),
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


def _active_speaker_design_draft_payload() -> dict[str, Any]:
    """Return the saved active-speaker design draft, if any."""

    from jasper.active_speaker.design_draft import load_design_draft

    payload = load_design_draft()
    logger.info(
        "event=sound.active_speaker_design_draft status=%s driver_count=%s "
        "candidate_count=%s",
        payload.get("status"),
        (payload.get("summary") or {}).get("driver_count"),
        (payload.get("summary") or {}).get("crossover_candidate_count"),
    )
    return payload


def _active_speaker_design_draft_save_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Persist a design draft from current topology plus bounded research JSON."""

    from jasper.active_speaker.design_draft import save_design_draft

    if not isinstance(raw, dict):
        raise ValueError("design draft request must be an object")
    topology, _guards_changed = _active_speaker_request_missing_software_guards(
        load_output_topology()
    )
    payload = save_design_draft(
        topology,
        driver_research=raw.get("driver_research"),
        manual_settings=raw.get("manual_settings"),
        operator_inputs=raw.get("operator_inputs"),
    )
    logger.info(
        "event=sound.active_speaker_design_draft_save status=%s "
        "topology_id=%s driver_count=%s candidate_count=%s "
        "manual_driver_count=%s manual_candidate_count=%s issues=%d",
        payload.get("status"),
        topology.topology_id,
        (payload.get("summary") or {}).get("driver_count"),
        (payload.get("summary") or {}).get("crossover_candidate_count"),
        (payload.get("summary") or {}).get("manual_driver_count"),
        (payload.get("summary") or {}).get("manual_crossover_candidate_count"),
        len(payload.get("issues") or []),
    )
    return payload


def _active_speaker_crossover_preview_payload() -> dict[str, Any]:
    """Return the saved no-audio crossover preview, if any."""

    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    payload = load_crossover_preview(current_design_draft=load_design_draft())
    logger.info(
        "event=sound.active_speaker_crossover_preview status=%s "
        "active_crossover_count=%s blocker_count=%s",
        payload.get("status"),
        (payload.get("summary") or {}).get("active_crossover_count"),
        (payload.get("summary") or {}).get("blocker_count"),
    )
    return payload


def _active_speaker_crossover_preview_save_payload() -> dict[str, Any]:
    """Persist a no-audio crossover preview from the saved design draft."""

    from jasper.active_speaker.crossover_preview import save_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft, save_design_draft

    draft = load_design_draft()
    if draft.get("status") not in {"not_saved", "unreadable"}:
        topology, _guards_changed = _active_speaker_request_missing_software_guards(
            load_output_topology()
        )
        draft = save_design_draft(
            topology,
            driver_research=draft.get("driver_research"),
            manual_settings=draft.get("manual_settings"),
            operator_inputs=draft.get("operator_inputs"),
        )
    payload = save_crossover_preview(draft)
    logger.info(
        "event=sound.active_speaker_crossover_preview_save status=%s "
        "topology_id=%s active_crossover_count=%s blocker_count=%s",
        payload.get("status"),
        (payload.get("source") or {}).get("topology_id"),
        (payload.get("summary") or {}).get("active_crossover_count"),
        (payload.get("summary") or {}).get("blocker_count"),
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


# --- single-audio-path per-driver commissioning + Stage-5 ramp ----------------
#
# The browser surface over the same guarded machinery the `jasper-active-speaker`
# CLI drives. Every loader uses the INLINE CamillaController seams
# (set_active_config_raw) so the persisted boot statefile is never repointed
# (crash-recovery-MUTED stays structural). A commission load arms a driver at the
# protected floor (silent); the Stage-5 ramp raises it one gated, operator-ACK'd
# step at a time. The GET state endpoint is read-only on purpose — the preflight
# emits the candidate YAML, so the load/step that run it are POST-only.


# The inline seams, saved-preview resolution, current-config read, and fresh
# path-safety evidence are shared with the `jasper-active-speaker` CLI via
# `jasper.active_speaker.commission_wiring` (commission_seams /
# read_current_config_path / resolve_commission_inputs /
# write_commission_path_safety) — imported lazily in each payload below so the
# socket-activated wizard process stays light.

COMMISSION_TONE_ALSA_DEVICE = "correction_substream"
COMMISSION_TONE_DURATION_S = 35.0
COMMISSION_TONE_RESTART_MARGIN_S = 3.0
COMMISSION_TONE_STARTUP_CHECK_S = 0.08
COMMISSION_TONE_SAMPLE_RATE = 48000
COMMISSION_TONE_SOURCE_DBFS = 0.0
COMMISSION_TONE_BACKEND = "correction_substream_continuous_tone"
SUMMED_COMMISSION_TONE_BACKEND = "correction_substream_summed_tone"
COMMISSION_TONE_MUX_SOCKET = os.environ.get(
    "JASPER_MUX_CONTROL_SOCKET", "/run/jasper-mux/control.sock",
)
COMMISSION_TONE_FANIN_LABEL = "correction"
_COMMISSION_TONE_LOCK = threading.Lock()
_COMMISSION_TONE_SESSION: dict[str, Any] | None = None
_SUMMED_TEST_ARM_REPORT: dict[str, Any] = {
    "status": "ready",
    "load_gate": "ready",
    "ok_to_load_active_config": True,
    "camilla_config": {},
    "safe_playback": {},
    "issues": [],
}


def _commission_tone_target_key(
    *,
    role: str,
    group_id: str | None,
    target: dict[str, Any] | None,
) -> str:
    target = target or {}
    output_index = target.get("output_index")
    if output_index is None:
        output_index = target.get("physical_output_index")
    return ":".join(
        [
            str(target.get("speaker_group_id") or group_id or ""),
            str(target.get("role") or target.get("driver_role") or role or ""),
            "" if output_index is None else str(output_index),
        ]
    )


def _commission_tone_wav_path(
    *,
    frequency_hz: float,
    duration_s: float = COMMISSION_TONE_DURATION_S,
) -> Path:
    from jasper.correction.playback import _ensure_tone_wav

    return _ensure_tone_wav(
        freq_hz=frequency_hz,
        duration_s=duration_s,
        dbfs=COMMISSION_TONE_SOURCE_DBFS,
        sample_rate=COMMISSION_TONE_SAMPLE_RATE,
    )


def _commission_tone_mux_command(cmd: str) -> dict[str, Any]:
    data = b""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(COMMISSION_TONE_MUX_SOCKET)
        sock.sendall((cmd + "\n").encode("ascii"))
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    if not data:
        raise RuntimeError("jasper-mux returned no response")
    payload = json.loads(data.decode("utf-8", "replace"))
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload, dict):
        raise RuntimeError("jasper-mux returned a non-object response")
    return payload


def _commission_tone_select_fanin_lane() -> dict[str, Any]:
    return _commission_tone_mux_command(
        f"TEST_SELECT {COMMISSION_TONE_FANIN_LABEL}",
    )


def _commission_tone_release_fanin_lane(*, reason: str) -> dict[str, Any]:
    try:
        payload = _commission_tone_mux_command("TEST_RELEASE")
    except Exception as exc:  # noqa: BLE001 - stop still needs to continue.
        payload = {
            "status": "failed",
            "reason": reason,
            "error": str(exc),
        }
        logger.warning(
            "event=sound.active_speaker_commission_tone action=fanin_release "
            "reason=%s status=failed error=%s",
            reason,
            exc,
        )
        return payload
    logger.info(
        "event=sound.active_speaker_commission_tone action=fanin_release "
        "reason=%s status=ok active_source=%s",
        reason,
        payload.get("active_source"),
    )
    return payload


def _active_speaker_restore_auto_source(*, reason: str) -> dict[str, Any]:
    """Best-effort return from setup-only routing to normal latest-source-wins."""

    try:
        payload = _commission_tone_mux_command("AUTO")
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "event=sound.active_speaker_source_auto action=restore reason=%s "
            "status=failed error=%s",
            reason,
            exc,
        )
        return {
            "status": "failed",
            "reason": reason,
            "error": str(exc),
        }
    logger.info(
        "event=sound.active_speaker_source_auto action=restore reason=%s "
        "status=ok mode=%s active_source=%s test_source=%s",
        reason,
        payload.get("mode"),
        payload.get("active_source"),
        payload.get("test_source"),
    )
    return {
        "status": "ok",
        "reason": reason,
        "state": payload,
    }


def _commission_tone_issue(exc: BaseException) -> dict[str, str]:
    return {
        "severity": "blocker",
        "code": "commission_tone_backend_failed",
        "message": f"could not play commissioning tone: {exc}",
    }


def _commission_tone_driver_style(
    *,
    topology: Any,
    group_id: str | None,
    role: str,
) -> str | None:
    for group in getattr(topology, "speaker_groups", ()):
        if group_id and getattr(group, "id", None) != group_id:
            continue
        for channel in getattr(group, "channels", ()):
            if getattr(channel, "role", None) == role:
                return getattr(channel, "driver_style", None)
    return None


def _commission_tone_signal_plan(
    *,
    role: str,
    group_id: str | None,
    topology: Any = None,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from jasper.active_speaker import (
        DRIVER_TEST_SIGNAL_PLAN_KIND,
        driver_test_signal_plan,
        load_active_speaker_preset,
    )

    role_id = str(role or "").strip().lower()
    source = "explicit_preset" if preset is not None else "preset_fallback"
    bound_preset = preset
    if bound_preset is None and crossover_preview is not None:
        from jasper.active_speaker.staging import compile_preset_from_crossover_preview

        source = "crossover_preview"
        topology = topology or load_output_topology()
        bound_preset, preview_issues, _ = compile_preset_from_crossover_preview(
            topology,
            crossover_preview,
        )
        if bound_preset is None:
            issues = [
                issue for issue in preview_issues if isinstance(issue, dict)
            ] or [
                {
                    "severity": "blocker",
                    "code": "commission_tone_preset_unresolved",
                    "message": (
                        "could not compile the saved crossover preview into a "
                        "driver test preset"
                    ),
                }
            ]
            return {
                "artifact_schema_version": 1,
                "kind": DRIVER_TEST_SIGNAL_PLAN_KIND,
                "status": "blocked",
                "role": role_id,
                "frequency_hz": None,
                "preset_source": source,
                "issues": issues,
            }
    if bound_preset is None:
        try:
            bound_preset = load_active_speaker_preset()
        except (OSError, ValueError, TypeError) as exc:
            return {
                "artifact_schema_version": 1,
                "kind": DRIVER_TEST_SIGNAL_PLAN_KIND,
                "status": "blocked",
                "role": role_id,
                "frequency_hz": None,
                "preset_source": source,
                "issues": [{
                    "severity": "blocker",
                    "code": "commission_tone_preset_unreadable",
                    "message": f"could not load active-speaker preset: {exc}",
                }],
            }

    driver_style = (
        _commission_tone_driver_style(
            topology=topology,
            group_id=group_id,
            role=role_id,
        )
        if topology is not None
        else None
    )
    plan = driver_test_signal_plan(
        bound_preset,
        role_id,
        driver_style=driver_style,
    )
    plan["preset_source"] = source
    plan["preset_id"] = getattr(bound_preset, "preset_id", None)
    plan["preset_name"] = getattr(bound_preset, "name", None)
    return plan


def _commission_tone_payload(
    *,
    status: str,
    playback_id: str,
    role: str,
    level_dbfs: float,
    frequency_hz: float | None,
    target: dict[str, Any] | None,
    group_id: str | None,
    audio_emitted: bool,
    issues: list[dict[str, str]],
    session_reused: bool = False,
    fanin_gate: dict[str, Any] | None = None,
    signal_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "backend": COMMISSION_TONE_BACKEND,
        "playback_id": playback_id,
        "audio_emitted": audio_emitted,
        "confirmable": audio_emitted and not issues,
        "continuous": True,
        "session_reused": session_reused,
        "target": target or {"speaker_group_id": group_id, "driver_role": role},
        "tone": {
            "frequency_hz": frequency_hz,
            "source_level_dbfs": COMMISSION_TONE_SOURCE_DBFS,
            "commission_gain_db": level_dbfs,
            "duration_ms": int(round(COMMISSION_TONE_DURATION_S * 1000)),
        },
        "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
        "issues": issues,
    }
    if fanin_gate is not None:
        payload["fanin_gate"] = fanin_gate
    if signal_plan is not None:
        payload["signal_plan"] = signal_plan
    return payload


def _stop_commission_tone_locked(*, reason: str) -> dict[str, Any]:
    global _COMMISSION_TONE_SESSION

    session = _COMMISSION_TONE_SESSION
    _COMMISSION_TONE_SESSION = None
    if not session:
        return {"status": "idle", "reason": reason}
    proc = session.get("process")
    was_running = bool(proc is not None and proc.poll() is None)
    if was_running:
        try:
            proc.terminate()
            proc.wait(timeout=0.75)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=0.75)
            except Exception:  # noqa: BLE001 - best-effort cleanup only.
                pass
        except ProcessLookupError:
            pass
    return {
        "status": "stopped" if was_running else "expired",
        "reason": reason,
        "playback_id": session.get("playback_id"),
        "target_key": session.get("target_key"),
    }


def _active_speaker_stop_commission_tone(*, reason: str) -> dict[str, Any]:
    with _COMMISSION_TONE_LOCK:
        payload = _stop_commission_tone_locked(reason=reason)
    payload["fanin_gate"] = _commission_tone_release_fanin_lane(reason=reason)
    logger.info(
        "event=sound.active_speaker_commission_tone action=stop reason=%s status=%s",
        reason,
        payload.get("status"),
    )
    return payload


async def _active_speaker_play_commission_tone(
    *,
    role: str,
    level_dbfs: float,
    playback_id: str,
    group_id: str | None = None,
    target: dict[str, Any] | None = None,
    topology: Any = None,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ensure one bounded continuous commissioning tone is playing."""

    global _COMMISSION_TONE_SESSION

    role = str(role or "").strip().lower()
    signal_plan = _commission_tone_signal_plan(
        role=role,
        group_id=group_id,
        topology=topology,
        preset=preset,
        crossover_preview=crossover_preview,
    )
    frequency_hz = signal_plan.get("frequency_hz")
    if signal_plan.get("status") != "ready" or frequency_hz is None:
        logger.warning(
            "event=sound.active_speaker_commission_tone action=plan status=blocked "
            "group=%s role=%s issues=%s",
            group_id,
            role,
            ",".join(
                str(issue.get("code"))
                for issue in signal_plan.get("issues", [])
                if isinstance(issue, dict)
            ),
        )
        return _commission_tone_payload(
            status="blocked",
            playback_id=playback_id,
            role=role,
            level_dbfs=level_dbfs,
            frequency_hz=None,
            target=target,
            group_id=group_id,
            audio_emitted=False,
            issues=[
                issue for issue in signal_plan.get("issues", [])
                if isinstance(issue, dict)
            ],
            signal_plan=signal_plan,
        )
    target_key = _commission_tone_target_key(role=role, group_id=group_id, target=target)
    try:
        wav_path = _commission_tone_wav_path(frequency_hz=frequency_hz)
    except Exception as exc:  # noqa: BLE001 - fail closed; the ramp will re-mute.
        return _commission_tone_payload(
            status="failed",
            playback_id=playback_id,
            role=role,
            level_dbfs=level_dbfs,
            frequency_hz=frequency_hz,
            target=target,
            group_id=group_id,
            audio_emitted=False,
            issues=[_commission_tone_issue(exc)],
            signal_plan=signal_plan,
        )

    try:
        fanin_gate = _commission_tone_select_fanin_lane()
    except Exception as exc:  # noqa: BLE001 - fail closed; the ramp will re-mute.
        return _commission_tone_payload(
            status="failed",
            playback_id=playback_id,
            role=role,
            level_dbfs=level_dbfs,
            frequency_hz=frequency_hz,
            target=target,
            group_id=group_id,
            audio_emitted=False,
            issues=[_commission_tone_issue(exc)],
            signal_plan=signal_plan,
        )

    started_proc = None
    try:
        with _COMMISSION_TONE_LOCK:
            session = _COMMISSION_TONE_SESSION
            if session and session.get("process") is not None:
                proc = session["process"]
                elapsed = time.monotonic() - float(session.get("started_monotonic", 0.0))
                remaining = COMMISSION_TONE_DURATION_S - elapsed
                if (
                    session.get("target_key") == target_key
                    and (
                        abs(float(session.get("frequency_hz", 0.0)) - frequency_hz)
                        < 0.01
                    )
                    and proc.poll() is None
                    and remaining > COMMISSION_TONE_RESTART_MARGIN_S
                ):
                    session["playback_id"] = playback_id
                    return _commission_tone_payload(
                        status="completed",
                        playback_id=playback_id,
                        role=role,
                        level_dbfs=level_dbfs,
                        frequency_hz=frequency_hz,
                        target=target,
                        group_id=group_id,
                        audio_emitted=True,
                        issues=[],
                        session_reused=True,
                        fanin_gate=fanin_gate,
                        signal_plan=signal_plan,
                    )
                _stop_commission_tone_locked(reason="replace")

            proc = subprocess.Popen(
                ["aplay", "-D", COMMISSION_TONE_ALSA_DEVICE, "-q", str(wav_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc.poll() is not None:
                raise RuntimeError(f"aplay exited immediately with rc={proc.returncode}")
            started_proc = proc
            _COMMISSION_TONE_SESSION = {
                "process": proc,
                "playback_id": playback_id,
                "target_key": target_key,
                "frequency_hz": frequency_hz,
                "started_monotonic": time.monotonic(),
            }
    except (OSError, RuntimeError) as exc:
        _commission_tone_release_fanin_lane(reason="start_failed")
        return _commission_tone_payload(
            status="failed",
            playback_id=playback_id,
            role=role,
            level_dbfs=level_dbfs,
            frequency_hz=frequency_hz,
            target=target,
            group_id=group_id,
            audio_emitted=False,
            issues=[_commission_tone_issue(exc)],
            fanin_gate=fanin_gate,
            signal_plan=signal_plan,
        )
    if started_proc is not None:
        await asyncio.sleep(COMMISSION_TONE_STARTUP_CHECK_S)
        if started_proc.poll() is not None:
            with _COMMISSION_TONE_LOCK:
                if (
                    _COMMISSION_TONE_SESSION
                    and _COMMISSION_TONE_SESSION.get("process") is started_proc
                ):
                    _COMMISSION_TONE_SESSION = None
            _commission_tone_release_fanin_lane(reason="startup_exit")
            return _commission_tone_payload(
                status="failed",
                playback_id=playback_id,
                role=role,
                level_dbfs=level_dbfs,
                frequency_hz=frequency_hz,
                target=target,
                group_id=group_id,
                audio_emitted=False,
                issues=[
                    _commission_tone_issue(
                        RuntimeError(
                            f"aplay exited during startup with rc={started_proc.returncode}"
                        )
                    )
                ],
                fanin_gate=fanin_gate,
                signal_plan=signal_plan,
            )

    logger.info(
        "event=sound.active_speaker_commission_tone action=start group=%s role=%s "
        "frequency_hz=%.1f duration_s=%.1f highpass_hz=%s lowpass_hz=%s",
        group_id,
        role,
        frequency_hz,
        COMMISSION_TONE_DURATION_S,
        (signal_plan.get("allowed_band") or {}).get("highpass_hz"),
        (signal_plan.get("allowed_band") or {}).get("lowpass_hz"),
    )
    return _commission_tone_payload(
        status="completed",
        playback_id=playback_id,
        role=role,
        level_dbfs=level_dbfs,
        frequency_hz=frequency_hz,
        target=target,
        group_id=group_id,
        audio_emitted=True,
        issues=[],
        fanin_gate=fanin_gate,
        signal_plan=signal_plan,
    )


def _active_speaker_plan_with_issues(
    plan: dict[str, Any],
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    if not issues:
        return plan
    return {
        **plan,
        "status": "blocked",
        "playback_allowed": False,
        "would_play": False,
        "issues": [
            *(plan.get("issues") if isinstance(plan.get("issues"), list) else []),
            *issues,
        ],
    }


async def _active_speaker_load_summed_commissioning_config(
    *,
    topology: OutputTopology,
    speaker_group_id: str,
    level_dbfs: float,
    startup_gate_calibration_level: dict[str, Any] | None,
    preset: Any,
    crossover_preview: dict[str, Any] | None,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Load the transient all-drivers-live commissioning graph for one check."""

    from jasper.active_speaker.staging import load_staged_startup_config
    from jasper.active_speaker.startup_load import load_summed_commissioning_config

    cam = camilla_factory()
    staged = load_staged_startup_config()
    current_config_path, _ = await read_current_config_path(cam)
    startup_setup = await _active_speaker_ensure_commission_startup_anchor(
        group=speaker_group_id,
        role="summed",
        staged_config=staged,
        current_config_path=current_config_path,
        camilla_factory=camilla_factory,
    )
    if startup_setup.get("status") == "blocked":
        return startup_setup

    staged = load_staged_startup_config()
    current_config_path, current_config_error = await read_current_config_path(cam)
    evidence_path = write_commission_path_safety(
        topology,
        staged,
        current_config_path,
        current_config_error,
    )
    load_config, read_running_config, get_current_config_path = commission_seams(cam)
    payload = await load_summed_commissioning_config(
        topology,
        speaker_group_id=speaker_group_id,
        calibration_level=startup_gate_calibration_level,
        load_config=load_config,
        read_running_config=read_running_config,
        get_current_config_path=get_current_config_path,
        preset=preset,
        crossover_preview=crossover_preview,
        staged_config=staged,
        audible_gain_db=level_dbfs,
        path_safety_evidence_path=evidence_path,
    )
    payload["startup_setup"] = startup_setup
    return payload


async def _active_speaker_rollback_summed_commissioning_config(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    from jasper.active_speaker.startup_load import rollback_driver_commissioning_config

    cam = camilla_factory()
    load_config, _, _ = commission_seams(cam)
    return await rollback_driver_commissioning_config(load_config=load_config)


def _summed_playback_with_issue(
    playback: dict[str, Any],
    *,
    issue: dict[str, str],
    status: str = "failed",
    commissioning_load: dict[str, Any] | None = None,
    rollback: dict[str, Any] | None = None,
    fanin_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = dict(playback)
    out.update({
        "status": status,
        "backend": SUMMED_COMMISSION_TONE_BACKEND,
        "audio_emitted": False,
        "confirmable": False,
        "issues": [
            *(playback.get("issues") if isinstance(playback.get("issues"), list) else []),
            issue,
        ],
    })
    if commissioning_load is not None:
        out["commissioning_load"] = commissioning_load
    if rollback is not None:
        out["rollback"] = rollback
    if fanin_gate is not None:
        out["fanin_gate"] = fanin_gate
    return out


async def _active_speaker_play_summed_commission_tone(
    plan: dict[str, Any],
    *,
    safe_session: dict[str, Any],
    topology: OutputTopology,
    speaker_group_id: str,
    startup_gate_calibration_level: dict[str, Any] | None,
    preset: Any,
    crossover_preview: dict[str, Any] | None,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Play one bounded combined-driver tone through the real active graph."""

    from jasper.active_speaker.playback import start_tone_playback

    artifact_playback = start_tone_playback(
        plan,
        safe_session=safe_session,
        backend=None,
        allow_audio=True,
    )
    if artifact_playback.get("status") != "completed":
        return artifact_playback

    tone = artifact_playback.get("tone") if isinstance(artifact_playback.get("tone"), dict) else {}
    try:
        level_dbfs = float(tone.get("level_dbfs"))
    except (TypeError, ValueError):
        level_dbfs = -80.0
    try:
        frequency_hz = float(tone.get("frequency_hz"))
        duration_s = max(0.05, float(tone.get("duration_ms")) / 1000.0)
        wav_path = _commission_tone_wav_path(
            frequency_hz=frequency_hz,
            duration_s=duration_s,
        )
    except (OSError, TypeError, ValueError) as exc:
        return _summed_playback_with_issue(
            artifact_playback,
            issue=_commission_tone_issue(exc),
        )

    load_payload = await _active_speaker_load_summed_commissioning_config(
        topology=topology,
        speaker_group_id=speaker_group_id,
        level_dbfs=level_dbfs,
        startup_gate_calibration_level=startup_gate_calibration_level,
        preset=preset,
        crossover_preview=crossover_preview,
        camilla_factory=camilla_factory,
    )
    load_state = (
        load_payload.get("load")
        if isinstance(load_payload.get("load"), dict)
        else {}
    )
    if load_state.get("status") != "loaded":
        load_issues = [
            issue for issue in load_state.get("issues", [])
            if isinstance(issue, dict)
        ]
        issue = load_issues[0] if load_issues else _commission_setup_issue(
            "summed_commission_load_failed",
            "could not open the combined active-speaker test path",
        )
        return _summed_playback_with_issue(
            artifact_playback,
            issue=issue,
            commissioning_load=load_payload,
        )

    fanin_gate: dict[str, Any] | None = None
    rollback: dict[str, Any] | None = None
    rollback_issue: dict[str, str] | None = None
    playback_result: dict[str, Any]
    try:
        fanin_gate = _commission_tone_select_fanin_lane()
        completed = subprocess.run(
            ["aplay", "-D", COMMISSION_TONE_ALSA_DEVICE, "-q", str(wav_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=duration_s + 1.0,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            raise RuntimeError(
                detail[0][:160] if detail else f"aplay exited {completed.returncode}"
            )
        playback_result = dict(artifact_playback)
        playback_result.update({
            "status": "completed",
            "backend": SUMMED_COMMISSION_TONE_BACKEND,
            "audio_emitted": True,
            "confirmable": True,
            "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
            "commissioning_load": load_payload,
            "fanin_gate": fanin_gate,
            "issues": [],
        })
    except Exception as exc:  # noqa: BLE001 - always re-mute below.
        playback_result = _summed_playback_with_issue(
            artifact_playback,
            issue=_commission_tone_issue(exc),
            commissioning_load=load_payload,
            rollback=rollback,
            fanin_gate=fanin_gate,
        )
    finally:
        if fanin_gate is not None:
            _commission_tone_release_fanin_lane(reason="summed_test")
        try:
            rollback = await _active_speaker_rollback_summed_commissioning_config(
                camilla_factory=camilla_factory,
            )
        except Exception as exc:  # noqa: BLE001 - surface but do not mask playback.
            logger.warning(
                "event=sound.active_speaker_summed_test action=rollback "
                "status=failed error=%s",
                exc,
            )
            rollback_issue = _commission_setup_issue(
                "summed_commission_rollback_failed",
                "combined test played, but JTS could not re-mute the active-speaker test path",
            )
    if rollback is not None:
        playback_result["rollback"] = rollback
    if rollback_issue is not None:
        playback_result["status"] = "failed"
        playback_result["confirmable"] = False
        playback_result["issues"] = [
            *(playback_result.get("issues") if isinstance(playback_result.get("issues"), list) else []),
            rollback_issue,
        ]
    return playback_result


def _commission_setup_issue(code: str, message: str) -> dict[str, str]:
    return {"severity": "blocker", "code": code, "message": message}


def _commission_setup_blocked_payload(
    *,
    group: str,
    role: str,
    issue: dict[str, str],
    startup_setup: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "startup_setup": startup_setup,
        "preflight": None,
        "load": {
            "status": "blocked",
            "last_action": "startup_anchor_blocked",
            "target": {"speaker_group_id": group, "role": role},
            "issues": [issue],
        },
    }


async def _active_speaker_ensure_commission_startup_anchor(
    *,
    group: str,
    role: str,
    staged_config: dict[str, Any],
    current_config_path: str | None,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Ensure commissioning has the silent startup graph as rollback anchor."""

    staged_path = (staged_config.get("config") or {}).get("path")
    if _config_paths_match(current_config_path, staged_path):
        return {"status": "already_loaded", "staged_config_path": staged_path}

    preview = _active_speaker_crossover_preview_save_payload()
    stage = _active_speaker_stage_config_payload({})
    if stage.get("status") != "staged":
        issue = _commission_setup_issue(
            "commission_startup_anchor_not_staged",
            "could not stage the silent active-speaker setup before driver testing",
        )
        return _commission_setup_blocked_payload(
            group=group,
            role=role,
            issue=issue,
            startup_setup={"status": "blocked", "preview": preview, "stage": stage},
        )

    path_payload = await _active_speaker_check_path_safety_payload(
        camilla_factory=camilla_factory,
    )
    path_report = path_payload.get("report") if isinstance(path_payload, dict) else {}
    if not isinstance(path_report, dict) or path_report.get("load_gate") != "ready":
        issue = _commission_setup_issue(
            "commission_startup_anchor_path_safety_blocked",
            "could not verify the silent active-speaker setup path before driver testing",
        )
        return _commission_setup_blocked_payload(
            group=group,
            role=role,
            issue=issue,
            startup_setup={
                "status": "blocked",
                "preview": preview,
                "stage": stage,
                "path_safety": path_payload,
            },
        )

    startup_load = await _active_speaker_load_startup_config_payload(
        camilla_factory=camilla_factory,
    )
    load_state = (
        startup_load.get("load")
        if isinstance(startup_load.get("load"), dict)
        else {}
    )
    if load_state.get("status") != "loaded" or not load_state.get(
        "rollback_available"
    ):
        issue = _commission_setup_issue(
            "commission_startup_anchor_load_failed",
            "could not load the silent active-speaker setup before driver testing",
        )
        return _commission_setup_blocked_payload(
            group=group,
            role=role,
            issue=issue,
            startup_setup={
                "status": "blocked",
                "preview": preview,
                "stage": stage,
                "path_safety": path_payload,
                "startup_load": startup_load,
            },
        )

    return {
        "status": "loaded",
        "preview_status": preview.get("status"),
        "staged_config_path": (stage.get("config") or {}).get("path"),
        "path_safety_load_gate": path_report.get("load_gate"),
        "startup_load_status": load_state.get("status"),
        "rollback_available": bool(load_state.get("rollback_available")),
    }


async def _active_speaker_commission_load_payload(
    raw: dict[str, Any],
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Arm a driver: load its per-driver commissioning config into the RUNNING
    graph at the protected floor (silent). Operator-only, single-flight."""

    from jasper.active_speaker.staging import load_staged_startup_config
    from jasper.active_speaker.startup_load import (
        commission_load_runtime_status,
        commission_load_state_with_runtime_status,
        load_commission_load_state,
        load_driver_commissioning_config,
        mark_commission_load_state_stale,
    )

    from .active_speaker_flow import blocking_measurement_phase

    group = str(raw.get("group") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    force = bool(raw.get("force"))
    # Serialize against the other measurement flows (room correction / pair
    # balance / pair sync) — all play sweeps through the production graph, and
    # commissioning does not hold the measurement window, so this cooperative
    # check is the exclusion (see jasper.web.active_speaker_flow).
    blocking = blocking_measurement_phase()
    if blocking is not None:
        logger.info(
            "event=sound.active_speaker_commission action=load result=refused "
            "reason=measurement_in_progress group=%s role=%s blocking=%s",
            group,
            role,
            blocking,
        )
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking,
            "next_step": (
                "Another measurement (room correction, balance, or sync) is "
                "running. Finish or stop it before commissioning a driver."
            ),
        }
    if force:
        _active_speaker_stop_commission_tone(reason="commission_load_force")
    existing = load_commission_load_state()
    cam = camilla_factory()
    if existing.get("status") == "loaded" and not force:
        try:
            running_raw = await cam.get_active_config_raw(best_effort=False)
        except Exception:  # noqa: BLE001 - fail closed; the new load will re-check.
            running_raw = None
        runtime = commission_load_runtime_status(existing, running_raw)
        live_existing = commission_load_state_with_runtime_status(existing, runtime)
        active_target = live_existing.get("target") or {}
        same_target = (
            live_existing.get("status") == "loaded"
            and (active_target.get("speaker_group_id") or "") == group
            and (active_target.get("role") or "") == role
        )
        if same_target:
            return {
                "status": "loaded",
                "reason": "commission_load_already_active",
                "load": live_existing,
                "next_step": "The driver is already armed; start the audible tone.",
            }
        if live_existing.get("status") == "loaded":
            return {
                "status": "refused",
                "reason": "commission_load_already_active",
                "active_target": active_target,
                "next_step": (
                    "A different driver is already armed. Stop it first, or pass force=true."
                ),
            }
        mark_commission_load_state_stale(existing, runtime)

    # Re-sync the live topology's protection state before staging the per-driver
    # candidate. An active commission requires every protection-required channel
    # (e.g. a compression-driver tweeter) to carry its software-guard request; a
    # stale topology can drift to required_missing and then block forever. Arming
    # repairs that drift. The actual high-pass is still enforced by the
    # protection-while-audible gate.
    topology, guards_changed = _active_speaker_request_missing_software_guards(
        load_output_topology()
    )
    if guards_changed:
        logger.info(
            "event=sound.active_speaker_commission action=request_software_guards "
            "group=%s role=%s",
            group,
            role,
        )
    staged = load_staged_startup_config()
    current_config_path, current_config_error = (
        await read_current_config_path(cam)
    )
    startup_setup = await _active_speaker_ensure_commission_startup_anchor(
        group=group,
        role=role,
        staged_config=staged,
        current_config_path=current_config_path,
        camilla_factory=camilla_factory,
    )
    if startup_setup.get("status") == "blocked":
        logger.info(
            "event=sound.active_speaker_commission action=startup_anchor "
            "group=%s role=%s status=blocked",
            group,
            role,
        )
        return startup_setup

    staged = load_staged_startup_config()
    preset, crossover_preview = resolve_commission_inputs()
    current_config_path, current_config_error = (
        await read_current_config_path(cam)
    )
    evidence_path = write_commission_path_safety(
        topology, staged, current_config_path, current_config_error
    )
    load_config, read_running_config, get_current_config_path = (
        commission_seams(cam)
    )
    payload = await load_driver_commissioning_config(
        topology,
        speaker_group_id=group,
        role=role,
        load_config=load_config,
        read_running_config=read_running_config,
        get_current_config_path=get_current_config_path,
        preset=preset,
        crossover_preview=crossover_preview,
        staged_config=staged,
        path_safety_evidence_path=evidence_path,
    )
    logger.info(
        "event=sound.active_speaker_commission action=load group=%s role=%s status=%s",
        group,
        role,
        (payload.get("load") or {}).get("status"),
    )
    payload["startup_setup"] = startup_setup
    if (payload.get("load") or {}).get("status") == "loaded":
        from jasper.active_speaker.commission_ramp import clear_pending_ramp_step

        payload["ramp"] = clear_pending_ramp_step(speaker_group_id=group)
    return payload


async def _active_speaker_commission_rollback_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Roll the running graph back to the all-muted staged config (re-mute)."""

    from jasper.active_speaker.safe_playback import stop_safe_playback_session
    from jasper.active_speaker.startup_load import rollback_driver_commissioning_config

    tone_stop = _active_speaker_stop_commission_tone(reason="commission_rollback")
    cam = camilla_factory()
    load_config, _, _ = commission_seams(cam)
    payload = await rollback_driver_commissioning_config(load_config=load_config)
    payload["safe_playback"] = stop_safe_playback_session(reason="commission_rollback")
    payload["tone_stop"] = tone_stop
    logger.info(
        "event=sound.active_speaker_commission action=rollback status=%s",
        (payload.get("rollback") or {}).get("status"),
    )
    return payload


async def _active_speaker_commission_ramp_step_payload(
    raw: dict[str, Any],
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Take one gated audible gain step on the armed driver (Stage 5)."""

    from jasper.active_speaker.commission_ramp import ramp_audible_step
    from jasper.active_speaker.staging import load_staged_startup_config

    group = str(raw.get("group") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    topology = load_output_topology()
    staged = load_staged_startup_config()
    preset, crossover_preview = resolve_commission_inputs()
    cam = camilla_factory()
    current_config_path, current_config_error = (
        await read_current_config_path(cam)
    )
    evidence_path = write_commission_path_safety(
        topology, staged, current_config_path, current_config_error
    )
    load_config, read_running_config, get_current_config_path = (
        commission_seams(cam)
    )

    async def _play_commission_tone(**kwargs: Any) -> dict[str, Any]:
        return await _active_speaker_play_commission_tone(
            **kwargs,
            topology=topology,
            preset=preset,
            crossover_preview=crossover_preview,
        )

    payload = await ramp_audible_step(
        topology,
        speaker_group_id=group,
        role=role,
        auto_retry_pending=bool(raw.get("auto_retry_pending")),
        load_config=load_config,
        read_running_config=read_running_config,
        get_current_config_path=get_current_config_path,
        preset=preset,
        crossover_preview=crossover_preview,
        staged_config=staged,
        path_safety_evidence_path=evidence_path,
        play_tone=_play_commission_tone,
    )
    logger.info(
        "event=sound.active_speaker_commission action=ramp_step group=%s role=%s "
        "status=%s next_db=%s",
        group,
        role,
        payload.get("status"),
        payload.get("next_gain_db"),
    )
    return payload


async def _active_speaker_commission_ramp_ack_payload(
    raw: dict[str, Any],
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Record the operator's verdict for the pending audible step."""

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.commission_ramp import (
        load_ramp_state,
        record_ramp_operator_ack,
    )
    from jasper.active_speaker.measurement import record_driver_measurement
    from jasper.active_speaker.safe_playback import load_safe_playback_state

    outcome = str(raw.get("outcome") or "").strip().lower()
    topology = load_output_topology()
    ramp_state = load_ramp_state()
    pending = ramp_state.get("pending")
    tone_stop = None
    if outcome != "silent":
        tone_stop = _active_speaker_stop_commission_tone(reason=f"ack_{outcome}")
    cam = camilla_factory()
    # load_config lets any terminal by-ear outcome re-mute the transient graph.
    load_config, _, _ = commission_seams(cam)
    payload = await record_ramp_operator_ack(outcome=outcome, load_config=load_config)
    if (
        outcome == "heard_correct_driver"
        and payload.get("status") == "confirmed"
        and not payload.get("issues")
        and isinstance(pending, dict)
    ):
        measurements = record_driver_measurement(
            topology,
            {
                "speaker_group_id": ramp_state.get("speaker_group_id"),
                "role": pending.get("role"),
                "outcome": outcome,
                "playback_id": pending.get("playback_id"),
                "test_level_dbfs": pending.get("gain_db"),
                "notes": "Recorded from active-speaker guarded ramp confirmation.",
            },
            calibration_level=load_calibration_level_state(),
            safe_session=load_safe_playback_state(),
        )
        payload["measurements"] = measurements
    if tone_stop is not None:
        payload["tone_stop"] = tone_stop
    logger.info(
        "event=sound.active_speaker_commission action=ramp_ack outcome=%s status=%s "
        "measurement_status=%s",
        outcome,
        payload.get("status"),
        (payload.get("measurements") or {}).get("status"),
    )
    return payload


async def _active_speaker_commission_ramp_abort_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Hard Stop: roll back to the all-muted staged config and reset the ramp."""

    from jasper.active_speaker.commission_ramp import abort_ramp

    tone_stop = _active_speaker_stop_commission_tone(reason="commission_abort")
    cam = camilla_factory()
    load_config, _, _ = commission_seams(cam)
    payload = await abort_ramp(load_config=load_config)
    payload["tone_stop"] = tone_stop
    logger.info(
        "event=sound.active_speaker_commission action=ramp_abort status=%s",
        payload.get("status"),
    )
    return payload


async def _active_speaker_commission_state_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Read-only commission-load + ramp + per-driver floor state for the card.

    Deliberately calls NO preflight (which would emit the candidate YAML) — a
    pure read. The arm/step that run the preflight are POST-only.
    """

    from jasper.active_speaker.commission_ramp import load_ramp_state
    from jasper.active_speaker.safe_playback import load_safe_playback_state
    from jasper.active_speaker.startup_load import (
        commission_load_runtime_status,
        commission_load_state_with_runtime_status,
        load_commission_load_state,
    )

    commission = load_commission_load_state()
    if commission.get("status") == "loaded":
        try:
            running_raw = await camilla_factory().get_active_config_raw(
                best_effort=False
            )
        except Exception:  # noqa: BLE001 - status must fail closed, not crash the page.
            running_raw = None
        commission = commission_load_state_with_runtime_status(
            commission,
            commission_load_runtime_status(commission, running_raw),
        )
    ramp = load_ramp_state()
    quiet = load_safe_playback_state().get("quiet_start") or {}
    stale = commission.get("status") == "stale"
    pending = None if stale else ramp.get("pending")
    floor_status = quiet.get("status")
    if stale and floor_status == "floor_pending_operator":
        floor_status = "floor_required"
    return {
        "kind": "jts_active_speaker_commission_state",
        "commission_load": {
            "status": commission.get("status"),
            "target": commission.get("target") or {},
            "rollback_available": bool(commission.get("rollback_available")),
            "runtime_status": commission.get("runtime_status") or {},
            "issues": commission.get("issues") or [],
        },
        "ramp": {
            "confirmed_roles": ramp.get("confirmed_roles") or [],
            "pending": pending,
        },
        "floor": {
            "status": floor_status,
            "floor_audio_confirmed": bool(
                quiet.get("floor_audio_confirmed") and not stale
            ),
            "last_level_dbfs": None if stale else quiet.get("last_level_dbfs"),
            "last_operator_result": (
                {}
                if stale or not isinstance(quiet.get("last_operator_result"), dict)
                else quiet.get("last_operator_result")
            ),
        },
    }


async def _active_speaker_commissioning_view_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Return the backend-owned active-speaker setup view model."""

    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.commissioning_coordinator import (
        build_commissioning_view,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.active_speaker.startup_load import load_startup_load_state

    topology = load_output_topology()
    design_draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=design_draft)
    measurements = load_measurement_state(topology)
    calibration_level = load_calibration_level_state()
    commission = await _active_speaker_commission_state_payload(
        camilla_factory=camilla_factory,
    )
    baseline = build_baseline_profile_candidate(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
    )
    view = build_commissioning_view(
        topology,
        measurements=measurements,
        commission=commission,
        startup_load={"state": load_startup_load_state()},
        baseline_profile=baseline,
        calibration_level=calibration_level,
    )
    logger.info(
        "event=sound.active_speaker_commissioning_view status=%s next_action=%s",
        view.get("status"),
        (view.get("next_action") or {}).get("id"),
    )
    return view


def _active_speaker_measurements_payload() -> dict[str, Any]:
    """Return active-speaker measurement evidence for the saved topology."""

    from jasper.active_speaker.measurement import load_measurement_state

    topology = load_output_topology()
    payload = load_measurement_state(topology)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    logger.info(
        "event=sound.active_speaker_measurements status=%s drivers=%s/%s "
        "summed=%s/%s",
        payload.get("status"),
        summary.get("captured_driver_count"),
        summary.get("required_driver_count"),
        summary.get("validated_summed_group_count"),
        summary.get("required_summed_group_count"),
    )
    return payload


def _safe_capture_slug(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    out = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
    out = "_".join(part for part in out.split("_") if part)
    return (out[:64] or fallback)


def _active_speaker_capture_dir() -> Path:
    return Path(
        os.environ.get(ACTIVE_SPEAKER_CAPTURE_DIR_ENV)
        or DEFAULT_ACTIVE_SPEAKER_CAPTURE_DIR
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _capture_mapping(raw: dict[str, Any]) -> dict[str, Any]:
    capture = raw.get("capture")
    return capture if isinstance(capture, dict) else {}


def _capture_store_files(root: Path) -> list[Path]:
    try:
        children = list(root.iterdir())
    except FileNotFoundError:
        return []
    return [
        child
        for child in children
        if child.is_file() and child.suffix.lower() == ".wav"
    ]


def _capture_sort_key(path: Path) -> tuple[float, str]:
    try:
        stat = path.stat()
    except OSError:
        return (0.0, path.name)
    return (stat.st_mtime, path.name)


def _enforce_capture_retention(root: Path, *, keep: Path | None = None) -> None:
    protected = keep.resolve() if keep is not None else None
    ordered: list[Path] = []
    protected_path: Path | None = None
    for path in sorted(
        _capture_store_files(root),
        key=_capture_sort_key,
        reverse=True,
    ):
        try:
            resolved = path.resolve()
        except OSError:
            ordered.append(path)
            continue
        if protected is not None and resolved == protected:
            protected_path = path
        else:
            ordered.append(path)
    if protected_path is not None:
        ordered.insert(0, protected_path)

    kept_count = 0
    kept_bytes = 0
    for path in ordered:
        try:
            resolved = path.resolve()
            size = path.stat().st_size
        except OSError:
            continue
        if protected is not None and resolved == protected:
            kept_count += 1
            kept_bytes += size
            continue
        if (
            kept_count < MAX_CAPTURE_STORED_FILES
            and kept_bytes + size <= MAX_CAPTURE_STORAGE_BYTES
        ):
            kept_count += 1
            kept_bytes += size
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _active_speaker_capture_wav_path(raw: dict[str, Any], *, kind: str) -> Path:
    """Return a bounded local WAV path from a JSON capture request.

    Browser callers send base64 WAV bytes. Tests and future server-side capture
    machinery may pass a path, but only from the active-speaker capture
    directory so the setup endpoint cannot become an arbitrary local file
    reader.
    """

    capture = _capture_mapping(raw)
    path_value = (
        raw.get("captured_wav_path")
        or raw.get("capture_wav_path")
        or capture.get("captured_wav_path")
        or capture.get("wav_path")
        or capture.get("path")
    )
    capture_root = _active_speaker_capture_dir().resolve()
    if path_value:
        candidate = Path(str(path_value)).expanduser().resolve()
        if not _is_relative_to(candidate, capture_root):
            raise ValueError(
                "capture WAV path must be inside active-speaker capture storage"
            )
        if not candidate.is_file():
            raise ValueError("capture WAV file does not exist")
        if candidate.stat().st_size > MAX_CAPTURE_WAV_BYTES:
            raise ValueError("capture WAV file is too large")
        _enforce_capture_retention(capture_root, keep=candidate)
        return candidate

    encoded = (
        raw.get("captured_wav_base64")
        or raw.get("capture_wav_base64")
        or capture.get("wav_base64")
        or capture.get("data")
    )
    if not encoded:
        raise ValueError("capture WAV evidence is missing")
    encoded_text = str(encoded)
    if encoded_text.startswith("data:"):
        _prefix, _sep, encoded_text = encoded_text.partition(",")
    try:
        wav_bytes = base64.b64decode(encoded_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("capture WAV base64 is invalid") from exc
    if not wav_bytes:
        raise ValueError("capture WAV evidence is empty")
    if len(wav_bytes) > MAX_CAPTURE_WAV_BYTES:
        raise ValueError("capture WAV upload is too large")
    capture_root.mkdir(parents=True, exist_ok=True)
    group = _safe_capture_slug(raw.get("speaker_group_id"), fallback="group")
    role = _safe_capture_slug(raw.get("role"), fallback="target")
    target = capture_root / f"{kind}_{group}_{role}_{uuid.uuid4().hex}.wav"
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_bytes(wav_bytes)
        os.chmod(tmp, CAPTURE_FILE_MODE)
        os.replace(tmp, target)
        os.chmod(target, CAPTURE_FILE_MODE)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    _enforce_capture_retention(capture_root, keep=target)
    return target


def _active_speaker_capture_sweep_meta(raw: dict[str, Any]) -> dict[str, Any]:
    capture = _capture_mapping(raw)
    sweep_meta = raw.get("sweep_meta") or capture.get("sweep_meta")
    if not isinstance(sweep_meta, dict):
        from jasper.active_speaker import driver_acoustics as acoustic
        from jasper.correction import sweep as sweep_mod

        _signal, meta = sweep_mod.synchronized_swept_sine(
            f1=acoustic.DEFAULT_F1_HZ,
            f2=acoustic.DEFAULT_F2_HZ,
            duration_approx_s=acoustic.DEFAULT_DURATION_S,
            sample_rate=acoustic.DEFAULT_SAMPLE_RATE,
            amplitude_dbfs=acoustic.DEFAULT_AMPLITUDE_DBFS,
        )
        return meta.to_dict()
    required = {"sample_rate", "n_samples", "f1", "f2", "duration_s", "amplitude_dbfs"}
    missing = sorted(key for key in required if key not in sweep_meta)
    if missing:
        raise ValueError("capture sweep metadata is incomplete")
    return dict(sweep_meta)


def _active_speaker_capture_preset(topology: OutputTopology) -> Any:
    preset, crossover_preview = resolve_commission_inputs()
    if preset is not None:
        return preset
    if crossover_preview is not None:
        from jasper.active_speaker.staging import compile_preset_from_crossover_preview

        compiled, issues, _gates = compile_preset_from_crossover_preview(
            topology,
            crossover_preview,
        )
        if compiled is not None:
            return compiled
        messages = [
            str(issue.get("message") or issue.get("code"))
            for issue in issues
            if isinstance(issue, dict)
        ]
        raise ValueError(
            "active speaker preset is not ready for capture analysis"
            + (": " + "; ".join(messages[:2]) if messages else "")
        )
    return _active_speaker_preset()


def _playback_id_from_capture(raw: dict[str, Any]) -> str | None:
    playback = raw.get("playback") if isinstance(raw.get("playback"), dict) else {}
    value = raw.get("playback_id") or playback.get("playback_id")
    return str(value).strip() if value else None


def _active_speaker_driver_measurement_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Record one operator-confirmed driver test result."""

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.measurement import record_driver_measurement
    from jasper.active_speaker.safe_playback import load_safe_playback_state

    if not isinstance(raw, dict):
        raise ValueError("driver measurement request must be an object")
    topology = load_output_topology()
    payload = record_driver_measurement(
        topology,
        raw,
        calibration_level=load_calibration_level_state(),
        safe_session=load_safe_playback_state(),
    )
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    logger.info(
        "event=sound.active_speaker_driver_measurement status=%s "
        "group_id=%s role=%s outcome=%s captured=%s drivers=%s/%s",
        payload.get("status"),
        raw.get("speaker_group_id"),
        raw.get("role"),
        raw.get("outcome"),
        bool(
            (summary.get("latest_driver_measurements") or {})
            .get(f"{raw.get('speaker_group_id')}:{raw.get('role')}", {})
            .get("captured")
        )
        if isinstance(summary.get("latest_driver_measurements"), dict)
        else False,
        summary.get("captured_driver_count"),
        summary.get("required_driver_count"),
    )
    return payload


def _active_speaker_capture_calibration(
    raw: dict[str, Any],
) -> tuple[Any, str | None, dict[str, Any]]:
    """Resolve (cal curve, calibration_id, resolved-mode dict) for an L2 capture.

    A calibrated measurement mic is named by ``calibration_id`` — the SAME
    correction calibration store the ``/correction/`` wizard fills (Dayton iMM-6 /
    UMM-6, miniDSP UMIK, or an uploaded REW curve). ``phase_aware`` is gated on the
    curve actually resolving: a phone (no / unknown calibration_id) is downgraded
    to ``magnitude_only`` so it can never authorize a phase/delay/polarity decision.
    """
    from jasper.active_speaker.crossover_alignment import resolve_measurement_mode

    calibration_id = str(raw.get("calibration_id") or "").strip()
    curve = None
    resolved_id: str | None = None
    if calibration_id:
        from jasper.correction.calibration import load_calibration_record

        try:
            record = load_calibration_record(calibration_id)
        except (FileNotFoundError, ValueError, OSError):
            record = None
        if record is not None:
            curve = record.curve
            resolved_id = record.calibration_id
    mode = resolve_measurement_mode(
        raw.get("measurement_mode"), has_calibrated_mic=curve is not None
    )
    return curve, resolved_id, mode.to_dict()


def _active_speaker_driver_capture_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Analyze one phone-mic driver WAV and record acoustic measurement evidence."""

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.commissioning_capture import (
        record_driver_acoustic_capture,
    )
    from jasper.active_speaker.safe_playback import load_safe_playback_state

    if not isinstance(raw, dict):
        raise ValueError("driver capture request must be an object")
    topology = load_output_topology()
    preset = _active_speaker_capture_preset(topology)
    wav_path = _active_speaker_capture_wav_path(raw, kind="driver")
    sweep_meta = _active_speaker_capture_sweep_meta(raw)
    group_id = str(raw.get("speaker_group_id") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    calibration_curve, calibration_id, measurement_mode = (
        _active_speaker_capture_calibration(raw)
    )
    payload = record_driver_acoustic_capture(
        topology,
        preset,
        speaker_group_id=group_id,
        role=role,
        captured_wav=wav_path,
        sweep_meta=sweep_meta,
        playback_id=_playback_id_from_capture(raw),
        test_level_dbfs=raw.get("test_level_dbfs"),
        has_mic_calibration=(
            bool(raw.get("has_mic_calibration")) or calibration_curve is not None
        ),
        calibration=calibration_curve,
        notes=raw.get("notes"),
        calibration_level=load_calibration_level_state(),
        safe_session=load_safe_playback_state(),
    )
    payload["measurement_mode"] = measurement_mode
    payload["calibration_id"] = calibration_id
    measurement = (
        payload.get("measurement")
        if isinstance(payload.get("measurement"), dict)
        else None
    )
    summary = (
        measurement.get("summary")
        if measurement and isinstance(measurement.get("summary"), dict)
        else {}
    )
    latest = (
        (summary.get("latest_driver_measurements") or {}).get(f"{group_id}:{role}")
        if isinstance(summary.get("latest_driver_measurements"), dict)
        else None
    )
    logger.info(
        "event=sound.active_speaker_driver_capture status=%s group_id=%s "
        "role=%s verdict=%s recorded=%s captured=%s drivers=%s/%s",
        "recorded" if payload.get("recorded") else "not_recorded",
        group_id,
        role,
        payload.get("verdict"),
        bool(payload.get("recorded")),
        bool(latest and latest.get("captured")),
        summary.get("captured_driver_count"),
        summary.get("required_driver_count"),
    )
    return payload


def _active_speaker_crossover_frequency_for_group(
    preview: dict[str, Any],
    speaker_group_id: str,
) -> float | None:
    groups = preview.get("groups") if isinstance(preview.get("groups"), list) else []
    for group in groups:
        if not isinstance(group, dict) or group.get("group_id") != speaker_group_id:
            continue
        crossovers = (
            group.get("crossovers")
            if isinstance(group.get("crossovers"), list)
            else []
        )
        for crossover in crossovers:
            if not isinstance(crossover, dict):
                continue
            try:
                frequency = float(crossover.get("proposed_frequency_hz"))
            except (TypeError, ValueError):
                continue
            if frequency > 0:
                return frequency
    return None


def _active_speaker_transient_summed_level(
    *,
    calibration_level: dict[str, Any],
    measurements: dict[str, Any],
    speaker_group_id: str,
    requested_level: Any,
) -> dict[str, Any]:
    """Return the bounded summed-test level without mutating startup state."""

    from jasper.active_speaker.calibration_level import (
        AUDIBLE_RAMP_STEP_DB,
        calibration_level_payload,
        clamp_test_level_dbfs,
    )

    def _finite(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    current = _finite(
        (calibration_level.get("test_signal") or {}).get("requested_level_dbfs")
    )
    summary = (
        measurements.get("summary")
        if isinstance(measurements.get("summary"), dict)
        else {}
    )
    latest_tests = (
        summary.get("latest_summed_tests")
        if isinstance(summary.get("latest_summed_tests"), dict)
        else {}
    )
    latest = latest_tests.get(speaker_group_id)
    latest_issues = (
        latest.get("issues")
        if isinstance(latest, dict) and isinstance(latest.get("issues"), list)
        else []
    )
    latest_ok = (
        isinstance(latest, dict)
        and latest.get("captured") is True
        and latest.get("audio_emitted") is True
        and not any(
            isinstance(issue, dict) and issue.get("severity") == "blocker"
            for issue in latest_issues
        )
    )
    if latest_ok:
        latest_tone = latest.get("tone") if isinstance(latest.get("tone"), dict) else {}
        current = _finite(latest_tone.get("level_dbfs")) or current
    if current is None:
        current = clamp_test_level_dbfs(None)
    requested = _finite(requested_level)
    issues: list[dict[str, str]] = []
    if requested is None:
        level = clamp_test_level_dbfs(current)
    elif requested > current + AUDIBLE_RAMP_STEP_DB:
        level = clamp_test_level_dbfs(current + AUDIBLE_RAMP_STEP_DB)
        issues.append({
            "severity": "warning",
            "code": "audible_ramp_step_limited",
            "message": "requested combined-test level exceeded the bounded step",
        })
    else:
        level = clamp_test_level_dbfs(requested)
    payload = calibration_level_payload(requested_level_dbfs=level)
    payload["last_action"] = "summed_transient_level"
    payload["prior_level_dbfs"] = current
    payload["requested_level_dbfs"] = requested
    payload["applied_delta_db"] = round(level - current, 3)
    payload["issues"] = issues
    return payload


async def _active_speaker_summed_test_payload(
    raw: dict[str, Any],
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Run and record one bounded combined-driver test for validation."""

    from jasper.active_speaker.calibration_level import (
        calibration_level_payload,
        load_calibration_level_state,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import (
        load_measurement_state,
        record_summed_test_artifact,
    )
    from jasper.active_speaker.playback import start_tone_playback
    from jasper.active_speaker.safe_playback import (
        arm_safe_playback_session,
        load_safe_playback_state,
        record_safe_playback_result,
    )
    from jasper.active_speaker.startup_load import load_startup_load_state
    from jasper.active_speaker.topology_tone import build_summed_topology_tone_plan

    if not isinstance(raw, dict):
        raise ValueError("summed test request must be an object")
    topology = load_output_topology()
    speaker_group_id = str(raw.get("speaker_group_id") or "").strip()
    design_draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=design_draft)
    requested_level = raw.get("level_dbfs", raw.get("requested_level_dbfs"))
    measurements = load_measurement_state(topology)
    persisted_calibration_level = load_calibration_level_state()
    calibration_level = (
        _active_speaker_transient_summed_level(
            calibration_level=persisted_calibration_level,
            measurements=measurements,
            speaker_group_id=speaker_group_id,
            requested_level=requested_level,
        )
        if requested_level is not None
        else persisted_calibration_level
    )
    startup_gate_level = calibration_level_payload()
    safe_session = load_safe_playback_state()
    wants_audio = bool(raw.get("audio"))
    if wants_audio and safe_session.get("status") != "armed":
        safe_session = arm_safe_playback_session(_SUMMED_TEST_ARM_REPORT)
    startup_load = load_startup_load_state()
    protected_loaded = bool(
        startup_load.get("loaded")
        and startup_load.get("rollback_available")
        and startup_load.get("current_config_matches_loaded") is not False
    )
    plan = build_summed_topology_tone_plan(
        topology,
        speaker_group_id=speaker_group_id,
        requested_frequency_hz=(
            raw.get("frequency_hz")
            or _active_speaker_crossover_frequency_for_group(preview, speaker_group_id)
        ),
        requested_level_dbfs=calibration_level.get("test_signal", {}).get(
            "requested_level_dbfs"
        ),
        requested_duration_ms=raw.get("duration_ms"),
        playback_allowed=(
            wants_audio
            and safe_session.get("status") == "armed"
            and protected_loaded
        ),
        safe_session_id=safe_session.get("session_id"),
        protected_startup_loaded=protected_loaded,
    )
    summary = (
        measurements.get("summary")
        if isinstance(measurements.get("summary"), dict)
        else {}
    )
    if not summary.get("driver_measurements_complete"):
        plan = _active_speaker_plan_with_issues(
            plan,
            [
                {
                    "severity": "blocker",
                    "code": "summed_test_driver_measurements_missing",
                    "message": "test each driver before running the combined test",
                },
            ],
        )
    preset, resolved_preview = resolve_commission_inputs()
    if wants_audio:
        playback = await _active_speaker_play_summed_commission_tone(
            plan,
            safe_session=safe_session,
            topology=topology,
            speaker_group_id=speaker_group_id,
            startup_gate_calibration_level=startup_gate_level,
            preset=preset,
            crossover_preview=resolved_preview,
            camilla_factory=camilla_factory,
        )
    else:
        playback = start_tone_playback(
            plan,
            safe_session=safe_session,
            backend=None,
            allow_audio=False,
        )
    session = record_safe_playback_result(playback)
    measurement_payload = record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": speaker_group_id,
            "playback": playback,
            "plan": plan,
        },
    )
    logger.info(
        "event=sound.active_speaker_summed_test status=%s group_id=%s "
        "level_dbfs=%s requested_level_dbfs=%s audio_requested=%s audio_emitted=%s blockers=%d "
        "artifact=%s",
        playback.get("status"),
        speaker_group_id,
        playback.get("tone", {}).get("level_dbfs"),
        requested_level,
        wants_audio,
        bool(playback.get("audio_emitted")),
        len(playback.get("issues") or []),
        (playback.get("artifact") or {}).get("wav_basename"),
    )
    return {
        "plan": plan,
        "playback": playback,
        "session": session,
        "calibration_level": calibration_level,
        "measurements": measurement_payload,
    }


def _active_speaker_summed_validation_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Record one summed crossover blend validation result."""

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.measurement import record_summed_validation

    if not isinstance(raw, dict):
        raise ValueError("summed validation request must be an object")
    topology = load_output_topology()
    payload = record_summed_validation(
        topology,
        raw,
        calibration_level=load_calibration_level_state(),
    )
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    logger.info(
        "event=sound.active_speaker_summed_validation status=%s "
        "group_id=%s outcome=%s validated=%s summed=%s/%s",
        payload.get("status"),
        raw.get("speaker_group_id"),
        raw.get("outcome"),
        bool(
            (summary.get("latest_summed_validations") or {})
            .get(str(raw.get("speaker_group_id") or ""), {})
            .get("validated")
        )
        if isinstance(summary.get("latest_summed_validations"), dict)
        else False,
        summary.get("validated_summed_group_count"),
        summary.get("required_summed_group_count"),
    )
    return payload


def _summed_test_id_from_capture(raw: dict[str, Any]) -> str | None:
    playback = raw.get("playback") if isinstance(raw.get("playback"), dict) else {}
    value = (
        raw.get("summed_test_id")
        or raw.get("playback_id")
        or playback.get("summed_test_id")
        or playback.get("playback_id")
    )
    return str(value).strip() if value else None


def _active_speaker_summed_capture_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Analyze one phone-mic summed WAV and record crossover validation evidence."""

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.commissioning_capture import (
        record_summed_acoustic_capture,
    )

    if not isinstance(raw, dict):
        raise ValueError("summed capture request must be an object")
    topology = load_output_topology()
    preset = _active_speaker_capture_preset(topology)
    wav_path = _active_speaker_capture_wav_path(raw, kind="summed")
    sweep_meta = _active_speaker_capture_sweep_meta(raw)
    group_id = str(raw.get("speaker_group_id") or "").strip()
    summed_test_id = _summed_test_id_from_capture(raw)
    calibration_curve, calibration_id, measurement_mode = (
        _active_speaker_capture_calibration(raw)
    )
    payload = record_summed_acoustic_capture(
        topology,
        preset,
        speaker_group_id=group_id,
        captured_wav=wav_path,
        sweep_meta=sweep_meta,
        crossover_fc_hz=raw.get("crossover_fc_hz"),
        summed_test_id=summed_test_id,
        playback_id=_playback_id_from_capture(raw),
        polarity=raw.get("polarity"),
        delay_ms=raw.get("delay_ms"),
        delay_target_role=raw.get("delay_target_role"),
        expect_null=bool(raw.get("expect_null")),
        has_mic_calibration=(
            bool(raw.get("has_mic_calibration")) or calibration_curve is not None
        ),
        calibration=calibration_curve,
        notes=raw.get("notes"),
        calibration_level=load_calibration_level_state(),
    )
    payload["measurement_mode"] = measurement_mode
    payload["calibration_id"] = calibration_id
    measurement = (
        payload.get("measurement")
        if isinstance(payload.get("measurement"), dict)
        else None
    )
    summary = (
        measurement.get("summary")
        if measurement and isinstance(measurement.get("summary"), dict)
        else {}
    )
    latest = (
        (summary.get("latest_summed_validations") or {}).get(group_id)
        if isinstance(summary.get("latest_summed_validations"), dict)
        else None
    )
    logger.info(
        "event=sound.active_speaker_summed_capture status=%s group_id=%s "
        "verdict=%s recorded=%s validated=%s summed=%s/%s",
        "recorded" if payload.get("recorded") else "not_recorded",
        group_id,
        payload.get("verdict"),
        bool(payload.get("recorded")),
        bool(latest and latest.get("validated")),
        summary.get("validated_summed_group_count"),
        summary.get("required_summed_group_count"),
    )
    return payload


def _active_speaker_alignment_curves(
    measurements: dict[str, Any],
    group: str | None,
) -> dict[str, Any]:
    """Collect the surfaced per-driver + summed FR curves for the alignment preview.

    These are the (calibrated) magnitude shapes the maintainer eyeballs to tweak
    Fc/slope by hand — this feature never auto-rewrites Fc/slope.
    """
    curves: dict[str, Any] = {"drivers": {}, "summed": None}
    latest = measurements.get("latest_by_target")
    if isinstance(latest, dict):
        for rec in latest.values():
            if not isinstance(rec, dict):
                continue
            if group and rec.get("speaker_group_id") != group:
                continue
            role = rec.get("role")
            acoustic = rec.get("acoustic")
            if (
                isinstance(role, str)
                and isinstance(acoustic, dict)
                and acoustic.get("fr_curve")
            ):
                curves["drivers"][role] = acoustic["fr_curve"]
    summed = measurements.get("latest_summed_by_group")
    if isinstance(summed, dict) and group:
        rec = summed.get(group)
        if isinstance(rec, dict):
            acoustic = rec.get("acoustic")
            if isinstance(acoustic, dict) and acoustic.get("fr_curve"):
                curves["summed"] = acoustic["fr_curve"]
    return curves


def _active_speaker_crossover_alignment_payload(
    requested_mode: str | None = None,
    speaker_group_id: str | None = None,
) -> dict[str, Any]:
    """Preview the L2 crossover-alignment proposal from current measurement state.

    Reads the recorded per-driver arrivals + summed null depth and proposes a SAFE
    delay/polarity refinement (``phase_aware`` granted only when the contributing
    captures were calibrated), plus the per-driver + summed FR curves for the
    maintainer. Read-only — the operator applies via the confirm POST.
    """
    from jasper.active_speaker.commissioning_capture import (
        build_crossover_alignment_proposal,
    )
    from jasper.active_speaker.crossover_alignment import PHASE_AWARE
    from jasper.active_speaker.measurement import load_measurement_state

    topology = load_output_topology()
    preset = _active_speaker_capture_preset(topology)
    measurements = load_measurement_state(topology)
    result = build_crossover_alignment_proposal(
        preset,
        measurements,
        requested_mode=requested_mode or PHASE_AWARE,
        speaker_group_id=speaker_group_id,
    )
    result["curves"] = _active_speaker_alignment_curves(
        measurements, result.get("speaker_group_id")
    )
    proposal = result.get("proposal") if isinstance(result.get("proposal"), dict) else {}
    logger.info(
        "event=sound.active_speaker_crossover_alignment status=%s mode=%s "
        "authorized=%s polarity_action=%s delay_status=%s",
        result.get("status"),
        (result.get("mode") or {}).get("mode"),
        proposal.get("authorized"),
        proposal.get("polarity_action"),
        proposal.get("delay_status"),
    )
    return result


def _active_speaker_baseline_profile_payload(
    *,
    write: bool = False,
) -> dict[str, Any]:
    """Return or compile the active-speaker baseline profile candidate."""

    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state

    topology = load_output_topology()
    design_draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=design_draft)
    measurements = load_measurement_state(topology)
    payload = build_baseline_profile_candidate(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        write=write,
    )
    logger.info(
        "event=sound.active_speaker_baseline_profile action=%s status=%s "
        "may_apply=%s issue_count=%d config=%s",
        "compile" if write else "status",
        payload.get("status"),
        bool((payload.get("permissions") or {}).get("may_apply")),
        len(payload.get("issues") or []),
        (payload.get("config") or {}).get("basename"),
    )
    return payload


async def _active_speaker_baseline_profile_apply_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Apply the active-speaker baseline profile through DSP apply."""

    from jasper.active_speaker.baseline_profile import apply_baseline_profile
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state

    topology = load_output_topology()
    design_draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=design_draft)
    measurements = load_measurement_state(topology)
    cam = camilla_factory()
    payload = await apply_baseline_profile(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
    )
    if payload.get("status") == "applied":
        payload["source_selection_restore"] = _active_speaker_restore_auto_source(
            reason="baseline_apply",
        )
    logger.info(
        "event=sound.active_speaker_baseline_profile action=apply status=%s "
        "apply_result=%s issue_count=%d source_restore=%s",
        payload.get("status"),
        (payload.get("apply") or {}).get("result"),
        len(payload.get("issues") or []),
        (payload.get("source_selection_restore") or {}).get("status"),
    )
    return payload


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

        def _read_json(self, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length > max_bytes:
                raise ValueError("request body too large")
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/",
                "/state",
                "/output-topology",
                "/active-speaker/design-draft",
                "/active-speaker/crossover-preview",
                "/active-speaker/measurements",
                "/active-speaker/crossover-alignment",
                "/active-speaker/baseline-profile",
                "/active-speaker/environment",
                "/active-speaker/safe-playback",
                "/active-speaker/calibration-level",
                "/active-speaker/bringup-preflight",
                "/active-speaker/startup-load",
                "/active-speaker/commission-state",
                "/active-speaker/commissioning-view",
                "/active-speaker/staged-config",
                "/active-speaker/channel-identity",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_read_request(self):
                return
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
            if path == "/active-speaker/design-draft":
                try:
                    self._send_json(_active_speaker_design_draft_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_design_draft result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/crossover-preview":
                try:
                    self._send_json(_active_speaker_crossover_preview_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_crossover_preview result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/measurements":
                try:
                    self._send_json(_active_speaker_measurements_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_measurements result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/crossover-alignment":
                try:
                    query = urllib.parse.parse_qs(
                        urllib.parse.urlparse(self.path).query
                    )
                    self._send_json(
                        _active_speaker_crossover_alignment_payload(
                            requested_mode=(query.get("measurement_mode") or [None])[0],
                            speaker_group_id=(
                                query.get("speaker_group_id") or [None]
                            )[0],
                        )
                    )
                except (ValueError, OSError, KeyError) as e:
                    logger.exception(
                        "event=sound.active_speaker_crossover_alignment result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/baseline-profile":
                try:
                    self._send_json(_active_speaker_baseline_profile_payload())
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_baseline_profile result=error"
                    )
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
            if path == "/active-speaker/commission-state":
                try:
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_state_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_commission result=error"
                    )
                    self._send_json({"error": str(e)}, status=502)
                return
            if path == "/active-speaker/commissioning-view":
                try:
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commissioning_view_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "event=sound.active_speaker_commissioning_view result=error"
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
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/apply",
                "/audition",
                "/live-draft",
                "/preview",
                "/settings",
                "/volume-floor/audition",
                "/volume-floor/stop",
                "/active-speaker/design-draft",
                "/active-speaker/crossover-preview",
                "/active-speaker/stop",
                "/active-speaker/calibration-level",
                "/active-speaker/channel-identity",
                "/active-speaker/channel-protection",
                "/active-speaker/stage-config",
                "/active-speaker/check-path-safety",
                "/active-speaker/load-startup-config",
                "/active-speaker/rollback-startup-config",
                "/active-speaker/commission-load",
                "/active-speaker/commission-rollback",
                "/active-speaker/commission-ramp-step",
                "/active-speaker/commission-ramp-ack",
                "/active-speaker/commission-ramp-abort",
                "/active-speaker/driver-measurement",
                "/active-speaker/driver-capture",
                "/active-speaker/summed-test",
                "/active-speaker/summed-validation",
                "/active-speaker/summed-capture",
                "/active-speaker/baseline-profile",
                "/active-speaker/baseline-profile/apply",
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
            if path in _FOLLOWER_BLOCKED_CONTENT_DSP_POSTS and bonded_follower_active():
                logger.info(
                    "event=sound.follower_content_dsp_blocked path=%s",
                    path,
                )
                self._send_json(
                    {
                        "error": (
                            "sound profile is controlled on the pair leader "
                            "while this speaker is a follower"
                        ),
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return
            try:
                raw = self._read_json(
                    max_bytes=(
                        MAX_CAPTURE_JSON_BYTES
                        if path in {
                            "/active-speaker/driver-capture",
                            "/active-speaker/summed-capture",
                        }
                        else MAX_JSON_BYTES
                    )
                )
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
                if path == "/active-speaker/stage-config":
                    self._send_json(_active_speaker_stage_config_payload(raw))
                    return
                if path == "/active-speaker/design-draft":
                    try:
                        self._send_json(_active_speaker_design_draft_save_payload(raw))
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_design_draft_save "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/crossover-preview":
                    try:
                        self._send_json(_active_speaker_crossover_preview_save_payload())
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_crossover_preview_save "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/driver-measurement":
                    try:
                        self._send_json(_active_speaker_driver_measurement_payload(raw))
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_driver_measurement "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/driver-capture":
                    try:
                        self._send_json(_active_speaker_driver_capture_payload(raw))
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_driver_capture "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/summed-test":
                    try:
                        self._send_json(
                            asyncio.run(
                                _active_speaker_summed_test_payload(
                                    raw,
                                    camilla_factory=camilla_factory,
                                )
                            )
                        )
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_summed_test "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/summed-validation":
                    try:
                        self._send_json(_active_speaker_summed_validation_payload(raw))
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_summed_validation "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/summed-capture":
                    try:
                        self._send_json(_active_speaker_summed_capture_payload(raw))
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_summed_capture "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/baseline-profile":
                    try:
                        self._send_json(
                            _active_speaker_baseline_profile_payload(write=True)
                        )
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_baseline_profile "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/baseline-profile/apply":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_baseline_profile_apply_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
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
                if path == "/active-speaker/commission-load":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_load_payload(
                                raw, camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-rollback":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_rollback_payload(
                                camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-ramp-step":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_ramp_step_payload(
                                raw, camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-ramp-ack":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_ramp_ack_payload(
                                raw, camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-ramp-abort":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_ramp_abort_payload(
                                camilla_factory=camilla_factory
                            )
                        )
                    )
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
                if path == "/volume-floor/audition":
                    try:
                        self._send_json(
                            asyncio.run(
                                _audition_volume_floor(
                                    raw,
                                    camilla_factory=camilla_factory,
                                )
                            )
                        )
                    except (OSError, RuntimeError, ValueError, TypeError) as e:
                        logger.exception("volume floor audition failed")
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/volume-floor/stop":
                    try:
                        self._send_json(
                            asyncio.run(
                                _stop_volume_floor_tone(
                                    camilla_factory=camilla_factory,
                                    reason=str(raw.get("reason") or "stop"),
                                )
                            )
                        )
                    except (OSError, RuntimeError, ValueError, TypeError) as e:
                        logger.exception("volume floor tone stop failed")
                        self._send_json({"error": str(e)}, status=502)
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
                refusal = _carrier_refusal(e)
                if refusal is not None:
                    # The loaded graph cannot host EQ — this is a known,
                    # handled state, not a server error. Return a typed 200
                    # body so the UI renders an honest hint instead of a 502
                    # toast or a silent no-op (no silent failure). 200, not the
                    # 409 used for the follower-block: the page reads
                    # reason_code/message from the body, and a 4xx would be
                    # swallowed by its `if (!resp.ok) throw` into a generic
                    # error — losing the honest reason.
                    logger.info(
                        "event=sound.eq_blocked path=%s reason=%s",
                        path,
                        refusal.reason_code,
                    )
                    self._send_json(refusal.to_payload())
                    return
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
