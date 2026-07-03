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
  POST /active-speaker/summed-test run combined-driver test artifact/playback
  POST /active-speaker/summed-test/level update active combined-test level
  POST /active-speaker/summed-test/stop stop active combined-driver playback
  POST /active-speaker/summed-validation record summed crossover validation
  POST /active-speaker/baseline-profile compile active baseline YAML
  POST /active-speaker/baseline-profile/apply explicitly apply active baseline
  POST /active-speaker/baseline-profile/save-and-apply finish commissioning
  POST /active-speaker/channel-identity mark/clear physical identity evidence
  POST /active-speaker/channel-protection mark/clear tweeter protection evidence
  POST /output-topology save a complete speaker/DAC topology draft
  POST /output-topology/reset reset output topology + active setup evidence
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
import hashlib
import html
import json
import logging
import math
import os
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
    topology_path,
)
from jasper.output_hardware import load_state as load_output_hardware_state
from jasper.active_speaker.commission_wiring import (
    commission_seams,
    read_current_config_path,
    resolve_commission_inputs,
    write_commission_path_safety,
)

# Commission-tone orchestration helpers are owned by the active-speaker domain
# (jasper.active_speaker.web_commissioning) so the /sound/ and /correction/
# operator surfaces share one implementation of the mux/fan-in/WAV/signal-plan
# plumbing on this hardware-safe path. /sound/ imports them here rather than
# keeping a hand-copied fork; the only commission-tone piece that stays local is
# _stop_commission_tone_locked, which is bound to this module's own
# _COMMISSION_TONE_SESSION/_COMMISSION_TONE_LOCK that the /sound/ play
# orchestration owns. See L4-1 in the Codex-week review.
from jasper.active_speaker.web_commissioning import (
    _combined_speech_stimulus_wav_path,
    _commission_summed_stimulus_issue,
    _commission_tone_issue,
    _commission_tone_mux_command,
    _commission_tone_payload,
    _commission_tone_release_fanin_lane,
    _commission_tone_select_fanin_lane,
    _commission_tone_signal_plan,
    _commission_tone_target_key,
    _commission_tone_wav_path,
    _config_paths_match,
    _summed_playback_with_issue,
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
    simple_bands_payload,
)
from jasper.sound.settings import (
    DEFAULT_VOLUME_FLOOR_DB,
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
LIVE_DRAFT_UNAVAILABLE_LOG_INTERVAL_SEC = 30.0
VOLUME_FLOOR_TONE_ALSA_DEVICE = "correction_substream"
VOLUME_FLOOR_TONE_FREQS_HZ = (125.0, 500.0, 2000.0)
VOLUME_FLOOR_TONE_SOURCE_DBFS = -12.0
VOLUME_FLOOR_TONE_CHUNK_DURATION_S = 8.0
VOLUME_FLOOR_TONE_SEGMENT_DURATION_S = 0.75
VOLUME_FLOOR_TONE_MAX_DURATION_S = 10 * 60.0
VOLUME_FLOOR_TONE_SAMPLE_RATE = 48000
VOLUME_FLOOR_TONE_STARTUP_CHECK_S = 0.08

_live_draft_unavailable_log_at: dict[str, float] = {}


class OutputTopologyRevisionConflict(ValueError):
    """Raised when a browser posts a topology based on stale saved state."""


# Serializes the optimistic-concurrency revision-compare and the topology write
# in ``_save_output_topology_payload``. The wizard runs on ThreadingHTTPServer
# (one thread per request), so without this the compare and the write are a
# TOCTOU: two concurrent POSTs can both read the same revision, both pass the
# stale-check, and both write — the second silently clobbering the first (the
# lost update the revision guard exists to prevent). The held section is kept
# minimal — the revision compare, the in-memory topology parse + software-guard
# request (which may itself persist), the write, and the post-write revision
# recapture — and nothing else: the response payload (to_dict, channel-identity
# / clock-domain reports, playback-route) is built AFTER release because some of
# those readers do their own filesystem I/O (clock_domain_report reads the
# observed-hardware tmpfs file on the dual-Apple composite-DAC path), which must
# not be held under the lock. ``save_output_topology`` is a self-contained
# atomic tempfile+os.replace write (no subprocess, no re-entry into this module,
# no nested acquisition of this lock), so the held section cannot deadlock or
# stall the wizard on a blocking call.
_output_topology_write_lock = threading.Lock()


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
            # The reset/default volume floor. One owner (volume_curve.
            # DEFAULT_VOLUME_FLOOR_DB, re-exported via sound.settings) → this
            # payload → the page, so the editor stops hardcoding -50 in five
            # places that silently drift if the Python default ever changes.
            "volume_floor_default_db": DEFAULT_VOLUME_FLOOR_DB,
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


def _output_topology_revision() -> str:
    """Content revision for optimistic concurrency on /sound topology writes."""

    target = topology_path()
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return "missing"
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _output_topology_payload() -> dict[str, Any]:
    topology = load_output_topology()

    return {
        "output_topology": topology.to_dict(include_evaluation=True),
        "topology_revision": _output_topology_revision(),
        "output_hardware": _output_hardware_dict(),
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
        "active_playback_route": _active_speaker_playback_route_payload(topology),
    }


def _save_output_topology_payload(
    raw: dict[str, Any],
    *,
    require_revision: bool = False,
) -> dict[str, Any]:
    # Hold the write lock across ONLY the revision-compare, the write, and the
    # post-write revision recapture so they are one atomic critical section (see
    # _output_topology_write_lock). Under ThreadingHTTPServer two concurrent
    # saves would otherwise both read the same revision, both pass the
    # stale-check, and both write — a lost update. The response payload below is
    # built after release (no I/O held under the lock); ``saved_revision`` is
    # captured inside the lock so the returned revision is this writer's own
    # published value, not a racing winner's.
    with _output_topology_write_lock:
        if require_revision:
            expected_revision = str(raw.get("topology_revision") or "")
            current_revision = _output_topology_revision()
            if not expected_revision or expected_revision != current_revision:
                raise OutputTopologyRevisionConflict(
                    "speaker layout changed in another session; refresh "
                    "hardware before saving"
                )
        raw_topology = raw.get("output_topology", raw)
        topology = OutputTopology.from_mapping(raw_topology)
        topology, guards_changed = _active_speaker_request_missing_software_guards(
            topology
        )
        save_output_topology(topology)
        saved_revision = _output_topology_revision()
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
        "topology_revision": saved_revision,
        "output_hardware": _output_hardware_dict(),
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
        "active_playback_route": _active_speaker_playback_route_payload(topology),
    }


def _reset_output_topology_payload() -> dict[str, Any]:
    from jasper.active_speaker.reset import clear_active_speaker_setup_state
    from jasper.cli.output_topology_reset import reset_to_detected_passive

    summed_stop = _active_speaker_stop_summed_test_tone(reason="output_topology_reset")
    tone_stop = _active_speaker_stop_commission_tone(reason="output_topology_reset")
    safe_stop = _active_speaker_stop_payload()
    reset = reset_to_detected_passive()
    setup_reset = clear_active_speaker_setup_state()
    payload = _output_topology_payload()
    payload["reset"] = reset
    payload["active_speaker_reset"] = setup_reset
    payload["summed_test_stop"] = summed_stop
    payload["tone_stop"] = tone_stop
    payload["safe_playback"] = safe_stop
    payload["saved"] = True
    return payload


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
    """Generate and cache the volume-floor reference WAV.

    This is a short repeating low/mid/high sequence, not a single steady sine.
    The goal is to let a household judge whether 1% is useful across the speaker
    instead of accidentally tuning the floor around one narrow frequency.
    """

    cache_dir = Path(
        os.environ.get(
            "JASPER_VOLUME_FLOOR_TONE_DIR",
            "/var/lib/jasper/correction/tones",
        )
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    freq_key = "-".join(str(int(freq)) for freq in VOLUME_FLOOR_TONE_FREQS_HZ)
    wav_path = cache_dir / (
        f"volume_floor_reference_{freq_key}Hz_"
        f"{int(VOLUME_FLOOR_TONE_CHUNK_DURATION_S * 1000)}ms_"
        f"{int(abs(VOLUME_FLOOR_TONE_SOURCE_DBFS) * 10)}dbm_"
        f"{VOLUME_FLOOR_TONE_SAMPLE_RATE}Hz.wav"
    )
    if wav_path.exists():
        return wav_path

    import numpy as np
    from scipy.io import wavfile

    sample_rate = VOLUME_FLOOR_TONE_SAMPLE_RATE
    total_n = int(round(VOLUME_FLOOR_TONE_CHUNK_DURATION_S * sample_rate))
    segment_n = max(1, int(round(VOLUME_FLOOR_TONE_SEGMENT_DURATION_S * sample_rate)))
    amp = 10 ** (VOLUME_FLOOR_TONE_SOURCE_DBFS / 20.0)
    fade = max(8, int(0.005 * sample_rate))
    parts: list[Any] = []
    samples_written = 0
    while samples_written < total_n:
        for freq_hz in VOLUME_FLOOR_TONE_FREQS_HZ:
            t = np.arange(segment_n, dtype=np.float64) / sample_rate
            sig = amp * np.sin(2 * math.pi * freq_hz * t)
            if fade * 2 < segment_n:
                sig[:fade] *= np.linspace(0.0, 1.0, fade) ** 2
                sig[-fade:] *= np.linspace(1.0, 0.0, fade) ** 2
            parts.append(sig)
            samples_written += len(sig)
            if samples_written >= total_n:
                break
    out = np.concatenate(parts)[:total_n]
    int16 = (np.clip(out, -1.0, 1.0) * 32767.0).astype(np.int16)
    tmp_path = wav_path.with_name(f".{wav_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        wavfile.write(str(tmp_path), sample_rate, int16)
        os.replace(tmp_path, wav_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    logger.info(
        "volume floor reference tone cached: %s (%s Hz, %.1f s, %.1f dBFS)",
        wav_path,
        ",".join(str(int(freq)) for freq in VOLUME_FLOOR_TONE_FREQS_HZ),
        VOLUME_FLOOR_TONE_CHUNK_DURATION_S,
        VOLUME_FLOOR_TONE_SOURCE_DBFS,
    )
    return wav_path


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
    from jasper.audio_runtime_plan import lean_capture_kwargs
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.runtime import is_lean_live_config_path

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
        active_lean_capture_kwargs = (
            lean_capture_kwargs()
            if is_lean_live_config_path(current_path, config_path)
            else None
        )
        result = carrier.reemit(
            profile,
            profile_id=f"live-{time.time_ns()}",
            output_trim_db=output_trim_db,
            capture_kwargs=active_lean_capture_kwargs,
            fanin_coupling_capture_kwargs=coupling_capture_kwargs_from_env(),
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
    from jasper.sound.runtime import load_profile_config

    return await load_profile_config(
        profile,
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
        source=source,
        persist_profile=persist_profile,
        audition=audition,
        output_trim_db=output_trim_db,
    )


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
    require_physical_identity: bool = True,
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
        require_physical_identity=require_physical_identity,
    )
    report = evaluate_path_safety_evidence(evidence)
    target = write_path_safety_evidence(evidence)
    preflight = build_startup_load_preflight(
        topology,
        staged_config=staged_config,
        calibration_level=calibration_level,
        path_safety_evidence_path=target,
        current_config_path=current_config_path,
        require_physical_identity=require_physical_identity,
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
    require_physical_identity: bool = True,
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
        require_physical_identity=require_physical_identity,
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
SUMMED_COMMISSION_SPEECH_BACKEND = "correction_substream_summed_speech"
SUMMED_TEST_CONFIRM_STOP_REASONS = {"operator_confirmed"}
SUMMED_TEST_MAX_LOOP_SECONDS = 10 * 60.0
_COMMISSION_TONE_LOCK = threading.Lock()
_COMMISSION_TONE_SESSION: dict[str, Any] | None = None
_SUMMED_TEST_TONE_LOCK = threading.Lock()
_SUMMED_TEST_TONE_SESSION: dict[str, Any] | None = None
_SUMMED_TEST_ARM_REPORT: dict[str, Any] = {
    "status": "ready",
    "load_gate": "ready",
    "ok_to_load_active_config": True,
    "camilla_config": {},
    "safe_playback": {},
    "issues": [],
}


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
            except (OSError, subprocess.TimeoutExpired):
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


def _summed_test_session_stop_reason(session: dict[str, Any]) -> str | None:
    with _SUMMED_TEST_TONE_LOCK:
        if _SUMMED_TEST_TONE_SESSION is session:
            reason = _SUMMED_TEST_TONE_SESSION.get("stop_reason")
            return str(reason) if reason else None
    return None


def _summed_test_playback_at_session_level(
    playback: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    out = dict(playback)
    with _SUMMED_TEST_TONE_LOCK:
        level = session.get("level_dbfs")
        load_payload = session.get("load_payload")
    try:
        level_dbfs = float(level)
    except (TypeError, ValueError):
        level_dbfs = None
    if level_dbfs is not None and math.isfinite(level_dbfs):
        tone = dict(out.get("tone") if isinstance(out.get("tone"), dict) else {})
        tone["level_dbfs"] = level_dbfs
        out["tone"] = tone
    if isinstance(load_payload, dict):
        out["commissioning_load"] = load_payload
    return out


def _summed_test_stopped_playback(
    playback: dict[str, Any],
    *,
    commissioning_load: dict[str, Any] | None = None,
    fanin_gate: dict[str, Any] | None = None,
    reason: str = "operator_stop",
) -> dict[str, Any]:
    confirmed = reason in SUMMED_TEST_CONFIRM_STOP_REASONS
    out = dict(playback)
    out.update({
        "status": "completed" if confirmed else "stopped",
        "backend": SUMMED_COMMISSION_SPEECH_BACKEND,
        "audio_emitted": bool(confirmed),
        "confirmable": bool(confirmed),
        "stop_reason": reason,
        "issues": [
            issue for issue in playback.get("issues", [])
            if isinstance(issue, dict)
        ],
    })
    if commissioning_load is not None:
        out["commissioning_load"] = commissioning_load
    if fanin_gate is not None:
        out["fanin_gate"] = fanin_gate
    return out


def _stop_summed_test_tone_locked(*, reason: str) -> dict[str, Any]:
    session = _SUMMED_TEST_TONE_SESSION
    if not session:
        return {"status": "idle", "reason": reason}
    session["stop_reason"] = reason
    proc = session.get("process")
    if proc is None:
        return {
            "status": "stopping",
            "reason": reason,
            "playback_id": session.get("playback_id"),
            "phase": "preparing",
        }
    was_running = bool(proc.poll() is None)
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
        "phase": "playing",
    }


def _active_speaker_stop_summed_test_tone(*, reason: str) -> dict[str, Any]:
    with _SUMMED_TEST_TONE_LOCK:
        payload = _stop_summed_test_tone_locked(reason=reason)
    logger.info(
        "event=sound.active_speaker_summed_test action=stop reason=%s status=%s",
        reason,
        payload.get("status"),
    )
    return payload


async def _active_speaker_summed_test_level_payload(
    raw: dict[str, Any],
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Apply a level change to the currently playing summed commissioning loop."""

    from jasper.active_speaker.calibration_level import calibration_level_payload

    if not isinstance(raw, dict):
        raise ValueError("summed test level request must be an object")
    requested_group_id = str(raw.get("speaker_group_id") or "").strip()
    requested_level = raw.get("level_dbfs", raw.get("requested_level_dbfs"))
    calibration_level = calibration_level_payload(
        requested_level_dbfs=requested_level,
    )
    level_dbfs = float(
        calibration_level.get("test_signal", {}).get("requested_level_dbfs", -80.0)
    )
    with _SUMMED_TEST_TONE_LOCK:
        session = _SUMMED_TEST_TONE_SESSION
        if not session:
            return {
                "status": "idle",
                "reason": "no_active_summed_test",
                "calibration_level": calibration_level,
            }
        session_group_id = str(session.get("speaker_group_id") or "").strip()
        if requested_group_id and requested_group_id != session_group_id:
            return {
                "status": "blocked",
                "reason": "different_active_summed_test",
                "speaker_group_id": session_group_id,
                "requested_speaker_group_id": requested_group_id,
                "playback_id": session.get("playback_id"),
                "calibration_level": calibration_level,
            }
        speaker_group_id = session_group_id or requested_group_id
        playback_id = session.get("playback_id")

    topology = load_output_topology()
    preset, resolved_preview = resolve_commission_inputs()
    load_payload = await _active_speaker_load_summed_commissioning_config(
        topology=topology,
        speaker_group_id=speaker_group_id,
        level_dbfs=level_dbfs,
        startup_gate_calibration_level=calibration_level_payload(),
        preset=preset,
        crossover_preview=resolved_preview,
        camilla_factory=camilla_factory,
        reconcile_output_hardware=False,
    )
    load_state = (
        load_payload.get("load")
        if isinstance(load_payload.get("load"), dict)
        else {}
    )
    loaded = load_state.get("status") == "loaded"
    status = "loaded" if loaded else "failed"
    if loaded:
        with _SUMMED_TEST_TONE_LOCK:
            if _SUMMED_TEST_TONE_SESSION is session:
                session["level_dbfs"] = level_dbfs
                session["load_payload"] = load_payload
    logger.info(
        "event=sound.active_speaker_summed_test action=level status=%s "
        "group_id=%s playback_id=%s level_dbfs=%s",
        status,
        speaker_group_id,
        playback_id,
        level_dbfs,
    )
    return {
        "status": status,
        "speaker_group_id": speaker_group_id,
        "playback_id": playback_id,
        "calibration_level": calibration_level,
        "commissioning_load": load_payload,
    }


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
    reconcile_output_hardware: bool = True,
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
        reconcile_output_hardware=reconcile_output_hardware,
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

    global _SUMMED_TEST_TONE_SESSION

    from jasper.active_speaker.playback import start_tone_playback

    artifact_playback = start_tone_playback(
        plan,
        safe_session=safe_session,
        backend=None,
        allow_audio=True,
    )
    if artifact_playback.get("status") != "completed":
        return artifact_playback

    playback_id = str(artifact_playback.get("playback_id") or uuid.uuid4().hex)
    with _SUMMED_TEST_TONE_LOCK:
        active_session = _SUMMED_TEST_TONE_SESSION
        active_proc = active_session.get("process") if active_session else None
        if active_session and (active_proc is None or active_proc.poll() is None):
            return _summed_playback_with_issue(
                artifact_playback,
                issue=_commission_setup_issue(
                    "summed_test_already_active",
                    "a combined speaker test is already running",
                ),
            )
        session: dict[str, Any] = {
            "playback_id": playback_id,
            "process": None,
            "speaker_group_id": speaker_group_id,
            "level_dbfs": None,
            "started_monotonic": time.monotonic(),
            "stop_reason": None,
        }
        _SUMMED_TEST_TONE_SESSION = session

    tone = artifact_playback.get("tone") if isinstance(artifact_playback.get("tone"), dict) else {}
    try:
        level_dbfs = float(tone.get("level_dbfs"))
    except (TypeError, ValueError):
        level_dbfs = -80.0
    try:
        wav_path, stimulus = _combined_speech_stimulus_wav_path()
        duration_s = max(0.05, float(stimulus.get("duration_s") or 0.0))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        with _SUMMED_TEST_TONE_LOCK:
            if _SUMMED_TEST_TONE_SESSION is session:
                _SUMMED_TEST_TONE_SESSION = None
        return _summed_playback_with_issue(
            artifact_playback,
            issue=_commission_summed_stimulus_issue(exc),
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
        with _SUMMED_TEST_TONE_LOCK:
            if _SUMMED_TEST_TONE_SESSION is session:
                _SUMMED_TEST_TONE_SESSION = None
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
    started_proc: subprocess.Popen[Any] | None = None
    try:
        stop_reason = _summed_test_session_stop_reason(session)
        if stop_reason:
            playback_result = _summed_test_stopped_playback(
                artifact_playback,
                commissioning_load=load_payload,
                reason=(
                    "operator_stop_before_audio"
                    if stop_reason in SUMMED_TEST_CONFIRM_STOP_REASONS
                    else stop_reason
                ),
            )
        else:
            fanin_gate = _commission_tone_select_fanin_lane()
            with _SUMMED_TEST_TONE_LOCK:
                if _SUMMED_TEST_TONE_SESSION is session:
                    session["level_dbfs"] = level_dbfs
                    session["load_payload"] = load_payload
            heard_audio = False
            loop_count = 0
            watchdog_deadline = time.monotonic() + SUMMED_TEST_MAX_LOOP_SECONDS
            while True:
                stop_reason = _summed_test_session_stop_reason(session)
                if stop_reason:
                    current_playback = _summed_test_playback_at_session_level(
                        artifact_playback,
                        session,
                    )
                    current_playback.update({
                        "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
                        "stimulus": stimulus,
                    })
                    playback_result = _summed_test_stopped_playback(
                        current_playback,
                        commissioning_load=current_playback.get(
                            "commissioning_load", load_payload
                        ),
                        fanin_gate=fanin_gate,
                        reason=(
                            stop_reason
                            if heard_audio
                            else (
                                "operator_stop_before_audio"
                                if stop_reason in SUMMED_TEST_CONFIRM_STOP_REASONS
                                else "operator_stop"
                            )
                        ),
                    )
                    break
                if time.monotonic() >= watchdog_deadline:
                    current_playback = _summed_test_playback_at_session_level(
                        artifact_playback,
                        session,
                    )
                    current_playback.update({
                        "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
                        "stimulus": stimulus,
                    })
                    playback_result = _summed_test_stopped_playback(
                        current_playback,
                        commissioning_load=current_playback.get(
                            "commissioning_load", load_payload
                        ),
                        fanin_gate=fanin_gate,
                        reason="watchdog_timeout",
                    )
                    break
                started_proc = subprocess.Popen(
                    ["aplay", "-D", COMMISSION_TONE_ALSA_DEVICE, "-q", str(wav_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                heard_audio = True
                with _SUMMED_TEST_TONE_LOCK:
                    if _SUMMED_TEST_TONE_SESSION is session:
                        session["process"] = started_proc
                        session["loop_count"] = loop_count + 1
                deadline = time.monotonic() + duration_s + 1.0
                watchdog_expired = False
                while started_proc.poll() is None:
                    now = time.monotonic()
                    if now >= watchdog_deadline:
                        watchdog_expired = True
                        try:
                            started_proc.terminate()
                            started_proc.wait(timeout=0.75)
                        except subprocess.TimeoutExpired:
                            try:
                                started_proc.kill()
                                started_proc.wait(timeout=0.75)
                            except (OSError, ProcessLookupError):
                                pass
                        except (OSError, ProcessLookupError):
                            pass
                        break
                    if now >= deadline:
                        try:
                            started_proc.terminate()
                            started_proc.wait(timeout=0.75)
                        except subprocess.TimeoutExpired:
                            try:
                                started_proc.kill()
                                started_proc.wait(timeout=0.75)
                            except (OSError, ProcessLookupError):
                                pass
                        except (OSError, ProcessLookupError):
                            pass
                        raise TimeoutError("aplay timed out during combined speaker test")
                    await asyncio.sleep(0.03)
                if watchdog_expired:
                    current_playback = _summed_test_playback_at_session_level(
                        artifact_playback,
                        session,
                    )
                    current_playback.update({
                        "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
                        "stimulus": stimulus,
                    })
                    playback_result = _summed_test_stopped_playback(
                        current_playback,
                        commissioning_load=current_playback.get(
                            "commissioning_load", load_payload
                        ),
                        fanin_gate=fanin_gate,
                        reason="watchdog_timeout",
                    )
                    break
                stop_reason = _summed_test_session_stop_reason(session)
                if stop_reason:
                    current_playback = _summed_test_playback_at_session_level(
                        artifact_playback,
                        session,
                    )
                    current_playback.update({
                        "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
                        "stimulus": stimulus,
                    })
                    playback_result = _summed_test_stopped_playback(
                        current_playback,
                        commissioning_load=current_playback.get(
                            "commissioning_load", load_payload
                        ),
                        fanin_gate=fanin_gate,
                        reason=stop_reason,
                    )
                    break
                if started_proc.returncode != 0:
                    raise RuntimeError(f"aplay exited {started_proc.returncode}")
                loop_count += 1
                with _SUMMED_TEST_TONE_LOCK:
                    if _SUMMED_TEST_TONE_SESSION is session:
                        session["process"] = None
    except Exception as exc:  # noqa: BLE001 - always re-mute below.
        playback_result = _summed_playback_with_issue(
            artifact_playback,
            issue=_commission_summed_stimulus_issue(exc),
            commissioning_load=load_payload,
            rollback=rollback,
            fanin_gate=fanin_gate,
        )
    finally:
        with _SUMMED_TEST_TONE_LOCK:
            if _SUMMED_TEST_TONE_SESSION is session:
                _SUMMED_TEST_TONE_SESSION = None
        if started_proc is not None and started_proc.poll() is None:
            try:
                started_proc.terminate()
                started_proc.wait(timeout=0.75)
            except subprocess.TimeoutExpired:
                try:
                    started_proc.kill()
                    started_proc.wait(timeout=0.75)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except (OSError, ProcessLookupError):
                pass
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
    require_physical_identity: bool = True,
) -> dict[str, Any]:
    """Ensure commissioning has the silent startup graph as rollback anchor."""

    staged_path = (staged_config.get("config") or {}).get("path")
    topology = load_output_topology()
    from jasper.active_speaker.startup_load import staged_topology_match_status

    staged_topology = staged_topology_match_status(
        topology,
        staged_config,
        require_physical_identity=require_physical_identity,
    )
    staged_matches = bool(staged_topology.get("matched"))
    if _config_paths_match(current_config_path, staged_path) and staged_matches:
        return {"status": "already_loaded", "staged_config_path": staged_path}
    if _config_paths_match(current_config_path, staged_path):
        logger.info(
            "event=sound.active_speaker_commission action=startup_anchor "
            "group=%s role=%s status=refresh_required reason=staged_topology_mismatch",
            group,
            role,
        )

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
        require_physical_identity=require_physical_identity,
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
        require_physical_identity=require_physical_identity,
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


def _active_speaker_confirmed_driver_roles(
    topology: OutputTopology,
    *,
    group: str,
) -> list[str]:
    from jasper.active_speaker.measurement import confirmed_driver_roles

    if not group:
        return []
    return confirmed_driver_roles(topology, speaker_group_id=group)


def _active_speaker_identity_audition_role_order_roles(
    topology: OutputTopology,
    *,
    group: str,
    role: str,
    confirmed_roles: list[str],
) -> list[str]:
    """Gate-only lower-role evidence for confirm-output channel auditions."""

    from jasper.active_speaker.commission_ramp import RAMP_ROLE_ORDER

    group_id = str(group or "").strip()
    role = str(role or "").strip().lower()
    if not group_id or role not in RAMP_ROLE_ORDER:
        return list(confirmed_roles)

    lower_roles = set(RAMP_ROLE_ORDER[: RAMP_ROLE_ORDER.index(role)])
    present_lower_roles: set[str] = set()
    for speaker_group in topology.speaker_groups:
        if speaker_group.id != group_id:
            continue
        present_lower_roles = {
            channel.role
            for channel in speaker_group.channels
            if channel.role in lower_roles
        }
        break

    roles = set(confirmed_roles) | present_lower_roles
    ordered_roles = [candidate for candidate in RAMP_ROLE_ORDER if candidate in roles]
    ordered_roles.extend(sorted(roles - set(RAMP_ROLE_ORDER)))
    return ordered_roles


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
    identity_audition = bool(raw.get("identity_audition"))
    require_physical_identity = not identity_audition
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
        require_physical_identity=require_physical_identity,
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
        topology,
        staged,
        current_config_path,
        current_config_error,
        require_physical_identity=require_physical_identity,
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
        require_physical_identity=require_physical_identity,
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

        payload["ramp"] = clear_pending_ramp_step(
            speaker_group_id=group,
            confirmed_roles=_active_speaker_confirmed_driver_roles(
                topology,
                group=group,
            ),
        )
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
    identity_audition = bool(raw.get("identity_audition"))
    require_physical_identity = not identity_audition
    topology = load_output_topology()
    staged = load_staged_startup_config()
    preset, crossover_preview = resolve_commission_inputs()
    cam = camilla_factory()
    current_config_path, current_config_error = (
        await read_current_config_path(cam)
    )
    evidence_path = write_commission_path_safety(
        topology,
        staged,
        current_config_path,
        current_config_error,
        require_physical_identity=require_physical_identity,
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

    confirmed_roles = _active_speaker_confirmed_driver_roles(
        topology,
        group=group,
    )
    role_order_confirmed_roles = (
        _active_speaker_identity_audition_role_order_roles(
            topology,
            group=group,
            role=role,
            confirmed_roles=confirmed_roles,
        )
        if identity_audition
        else confirmed_roles
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
        require_physical_identity=require_physical_identity,
        confirmed_roles=confirmed_roles,
        role_order_confirmed_roles=role_order_confirmed_roles,
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
    acknowledged_step = (
        payload.get("acknowledged_step")
        if isinstance(payload.get("acknowledged_step"), dict)
        else pending
    )
    should_record_driver_evidence = (
        outcome == "heard_correct_driver"
        and payload.get("status") == "confirmed"
        and not payload.get("issues")
    ) or (outcome == "heard_wrong_driver" and payload.get("status") == "aborted")
    if should_record_driver_evidence and isinstance(acknowledged_step, dict):
        measurements = record_driver_measurement(
            topology,
            {
                "speaker_group_id": ramp_state.get("speaker_group_id"),
                "role": acknowledged_step.get("role"),
                "outcome": outcome,
                "playback_id": acknowledged_step.get("playback_id"),
                "test_level_dbfs": acknowledged_step.get("gain_db"),
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

    from jasper.active_speaker.commission_ramp import (
        effective_confirmed_roles,
        load_ramp_state,
    )
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
    target = commission.get("target") or {}
    group = str(
        target.get("speaker_group_id") or ramp.get("speaker_group_id") or ""
    ).strip()
    durable_confirmed: list[str] = []
    if group:
        topology = load_output_topology()
        durable_confirmed = _active_speaker_confirmed_driver_roles(
            topology,
            group=group,
        )
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
            "confirmed_roles": effective_confirmed_roles(
                ramp,
                speaker_group_id=group,
                confirmed_roles=durable_confirmed,
            ),
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
    """Return the backend-owned active-speaker setup view model.

    The state-loading + composition lives in the shared
    ``commissioning_coordinator.load_commissioning_view`` (the single source of
    truth for "the commissioning view of this speaker" — the crossover envelope
    consumes the same loader). Only the ``commission`` runtime relay is built
    here, because its full payload needs the async CamillaDSP runtime probe
    this caller owns.
    """

    from jasper.active_speaker.commissioning_coordinator import (
        load_commissioning_view,
    )

    commission = await _active_speaker_commission_state_payload(
        camilla_factory=camilla_factory,
    )
    view = load_commissioning_view(commission=commission)
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
    if requested is None:
        level = clamp_test_level_dbfs(current)
    else:
        level = clamp_test_level_dbfs(requested)
    payload = calibration_level_payload(requested_level_dbfs=level)
    payload["last_action"] = "summed_transient_level"
    payload["prior_level_dbfs"] = current
    payload["requested_level_dbfs"] = requested
    payload["applied_delta_db"] = round(level - current, 3)
    payload["issues"] = []
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
    playback_tone = playback.get("tone") if isinstance(playback.get("tone"), dict) else {}
    playback_level = playback_tone.get("level_dbfs")
    if playback_level is not None:
        calibration_level = calibration_level_payload(
            requested_level_dbfs=playback_level,
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


def _active_speaker_output_safety_from_config_path(
    config_path: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """Classify whether an applied config is still the safety-muted startup graph."""

    from jasper.active_speaker.staging import DEFAULT_STAGED_CONFIG_NAME

    path = str(config_path or "")
    safety_muted = os.path.basename(path) == DEFAULT_STAGED_CONFIG_NAME
    return {
        "safety_muted": safety_muted,
        "reason": "active_speaker_staged_startup" if safety_muted else None,
        "active_config_path": path or None,
    }


async def _active_speaker_finish_commissioning_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Backend-owned final handoff from commissioning to the active profile.

    The browser expresses one user intent: make the checked crossover the normal
    active speaker profile. The backend owns the compile/validate/load/confirm
    sequence, so the UI cannot wedge itself between "saved" and "applied".
    """

    summed_stop = _active_speaker_stop_summed_test_tone(reason="finish_commissioning")
    try:
        from jasper.active_speaker.commission_ramp import load_ramp_state
        from jasper.active_speaker.startup_load import load_commission_load_state

        ramp_state = load_ramp_state()
        commission_load = load_commission_load_state()
        cleanup_needed = isinstance(ramp_state.get("pending"), dict) or (
            commission_load.get("status") == "loaded"
        )
        if cleanup_needed:
            commissioning_cleanup = await _active_speaker_commission_ramp_abort_payload(
                camilla_factory=camilla_factory,
            )
        else:
            commissioning_cleanup = {
                "status": "idle",
                "ramp": ramp_state,
                "commission_load": commission_load,
            }
    except (OSError, RuntimeError, ValueError) as exc:
        commissioning_cleanup = {"status": "error", "error": str(exc)}

    payload = await _active_speaker_baseline_profile_apply_payload(
        camilla_factory=camilla_factory,
    )
    payload["commissioning_cleanup"] = {
        "summed_test": summed_stop,
        "ramp": commissioning_cleanup,
    }
    profile = (
        payload.get("profile")
        if isinstance(payload.get("profile"), dict)
        else {}
    )
    apply_state = (
        payload.get("apply")
        if isinstance(payload.get("apply"), dict)
        else {}
    )
    config = (
        profile.get("config")
        if isinstance(profile.get("config"), dict)
        else {}
    )
    active_config_path = apply_state.get("active_config_path") or config.get("path")
    payload["output_safety"] = _active_speaker_output_safety_from_config_path(
        active_config_path
        if isinstance(active_config_path, (str, os.PathLike))
        else None
    )
    logger.info(
        "event=sound.active_speaker_finish_commissioning status=%s "
        "apply_result=%s safety_muted=%s issue_count=%d",
        payload.get("status"),
        (
            (payload.get("apply") or {}).get("result")
            if isinstance(payload.get("apply"), dict)
            else None
        ),
        (payload.get("output_safety") or {}).get("safety_muted"),
        len(payload.get("issues") or []),
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
                "/active-speaker/summed-test",
                "/active-speaker/summed-test/level",
                "/active-speaker/summed-test/stop",
                "/active-speaker/summed-validation",
                "/active-speaker/baseline-profile",
                "/active-speaker/baseline-profile/apply",
                "/active-speaker/baseline-profile/save-and-apply",
                "/output-topology",
                "/output-topology/reset",
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
                raw = self._read_json(max_bytes=MAX_JSON_BYTES)
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
                if path == "/active-speaker/summed-test/level":
                    try:
                        self._send_json(
                            asyncio.run(
                                _active_speaker_summed_test_level_payload(
                                    raw,
                                    camilla_factory=camilla_factory,
                                )
                            )
                        )
                    except OSError as e:
                        logger.exception(
                            "event=sound.active_speaker_summed_test_level "
                            "result=error error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/active-speaker/summed-test/stop":
                    reason = str(raw.get("reason") or "operator_stop")
                    self._send_json(_active_speaker_stop_summed_test_tone(reason=reason))
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
                if path == "/active-speaker/baseline-profile/save-and-apply":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_finish_commissioning_payload(
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
                        self._send_json(
                            _save_output_topology_payload(raw, require_revision=True)
                        )
                    except OutputTopologyRevisionConflict as e:
                        logger.warning(
                            "event=sound.output_topology_save result=conflict "
                            "error=%s",
                            type(e).__name__,
                        )
                        payload = _output_topology_payload()
                        payload["error"] = str(e)
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except OSError as e:
                        logger.exception(
                            "event=sound.output_topology_save result=error "
                            "error=%s",
                            type(e).__name__,
                        )
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/output-topology/reset":
                    try:
                        self._send_json(_reset_output_topology_payload())
                    except (OSError, RuntimeError, ValueError) as e:
                        logger.exception(
                            "event=sound.output_topology_reset result=error "
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
