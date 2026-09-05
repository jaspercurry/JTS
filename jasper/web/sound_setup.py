# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared backend for preference EQ at /eq/ and hardware setup at /sound/setup/.

Both public prefixes are stripped by nginx, so the routes this server answers
are the bare paths listed in ``do_GET``/``do_POST`` below.

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
import html
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

if TYPE_CHECKING:
    from jasper.active_speaker.crossover_declaration import CrossoverGeometry

# correction_play_device is the lane's one transport reader: payloads resolve
# the device fresh so they report the transport the spawn actually used.
from jasper.audio_measurement.correction_lane import (
    correction_play_device,
    popen_correction_play,
)
from jasper.audio_hardware.hat_eeprom import DEFAULT_HAT_DIR
from jasper.audio_hardware.usb_port_role import (
    DEFAULT_BOOT_CONFIG_PATH,
    DEFAULT_I2S_HAT_INTENT_PATH,
    detected_i2s_hat_profile,
    read_i2s_hat_intent,
    render_i2s_hat_boot_config,
    selectable_i2s_hat_profiles,
    write_i2s_hat_intent,
)
from jasper.dsp_apply import same_config_file
from jasper.json_fields import finite_float as _finite
from jasper.log_event import log_event
from jasper.output_topology import (
    OutputHardware,
    OutputTopology,
    channel_identity_report,
    clock_domain_report,
    composite_serial_repin_plan,
    declared_hardware_mismatch,
    load_output_topology,
    load_output_topology_snapshot,
    new_topology_draft,
    output_topology_mutation,
    repin_composite_child_serials,
    set_channel_identity_verified,
    set_channel_protection_status,
)
from jasper.output_hardware import (
    OutputHardwareState,
    detected_hardware_adoption_precondition,
    load_state as load_output_hardware_state,
    topology_hardware_from_state,
)
from jasper.active_speaker.commission_wiring import (
    commission_seams,
    read_current_config_path,
    resolve_commission_inputs,
    write_commission_path_safety,
)

# The commission-tone helpers, timing constants and blocker vocabulary below
# are the active-speaker domain's objects, shared with /correction/; the only
# local piece is _stop_commission_tone_locked, bound to this module's
# _COMMISSION_TONE_SESSION/_COMMISSION_TONE_LOCK.
#
# COMMISSION_TONE_DURATION_S in particular MUST stay the owner's object: mux
# leases the test fan-in gate for FANIN_TEST_LEASE_SEC, and a /sound/ copy that
# drifted above that lease would let the gate expire mid-tone and readmit
# household music into a live sweep. tests/test_commission_tone_single_owner.py
# pins both the import and the lease headroom.
from jasper.active_speaker._common import blocker_issue as _issue
from jasper.active_speaker.web_commissioning import (
    COMMISSION_TONE_DURATION_S,
    COMMISSION_TONE_RESTART_MARGIN_S,
    COMMISSION_TONE_STARTUP_CHECK_S,
    SUMMED_COMMISSION_SPEECH_BACKEND,
    _blocked_startup_anchor,
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
    _summed_playback_with_issue,
    commission_startup_anchor_load_failed_issue,
    commission_startup_anchor_not_staged_issue,
    commission_startup_anchor_path_safety_blocked_issue,
    ensure_missing_software_guards,
    request_missing_software_guards as _request_missing_software_guards,
    rollback_summed_commission_teardown,
    summed_commission_load_failed_issue,
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

from ._common import (
    JsonBodyError,
    begin_request,
    bonded_follower_active,
    bonded_follower_leader_web_url,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    json_island,
    read_json_object,
    reject_csrf,
    send_html_response,
    send_json_response,
    send_route_failure,
    terminate_process,
)
from .volume_floor_tone import VOLUME_FLOOR_TONE_SESSION

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
I2S_HAT_REBOOT_REQUIRED_PATH = "/run/jasper-output-hardware/i2s-hat-reboot-required"
I2S_HAT_RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"
MAX_JSON_BYTES = 64 * 1024
LIVE_DRAFT_UNAVAILABLE_LOG_INTERVAL_SEC = 30.0

_live_draft_unavailable_log_at: dict[str, float] = {}


class OutputTopologyRevisionConflict(ValueError):
    """Raised when a browser posts a topology based on stale saved state."""


class OutputHardwareRequestConflict(ValueError):
    """A detected-hardware action no longer names the state it was offered for.

    Raised by both hardware-mismatch actions — the full reset and the
    same-shape re-pin — when the saved topology or the reconciler-owned
    hardware observation moved between rendering the offer and clicking it.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(
            "Speaker setup or detected hardware changed. Review it and try again."
        )


class OutputTopologyCapabilityBlocked(ValueError):
    """Raised when a posted layout needs hardware this DAC does not have.

    A ``ValueError`` so the POST dispatcher's existing validation branch returns
    it as ``{"error": ...}`` with 400 — the shape the page already renders as a
    layout error while keeping the operator's unsaved draft on screen.
    """


# Profile Apply and the split EQ/setup settings pages both replace the live DSP
# graph from persisted Sound state. Serialize their fresh reads, durable writes,
# and live side effects so the final profile, settings, and graph describe one
# ordered result instead of interleaving browser saves.
_sound_state_write_lock = threading.Lock()
_LAST_DSP_APPLY_SNAPSHOT_UNSET = object()
_SOUND_SETTINGS_FIELDS = frozenset({
    "headroom_trim_db",
    "match_loudness",
    "volume_floor_db",
})


def _camilla():
    from jasper.camilla import primary_controller

    return primary_controller()


def _state_payload(
    profile: SoundProfile,
    *,
    library_path: str | Path | None = None,
    include_library: bool = False,
    settings_snapshot: SoundSettings | None = None,
    last_dsp_apply_snapshot: Mapping[str, Any] | None | object = (
        _LAST_DSP_APPLY_SNAPSHOT_UNSET
    ),
) -> dict[str, Any]:
    from jasper.dsp_apply import dsp_write_epoch_from_state, last_dsp_apply_state

    if last_dsp_apply_snapshot is _LAST_DSP_APPLY_SNAPSHOT_UNSET:
        last_dsp_apply = last_dsp_apply_state()
    elif last_dsp_apply_snapshot is None:
        last_dsp_apply = None
    else:
        last_dsp_apply = dict(last_dsp_apply_snapshot)
    settings = (
        settings_snapshot
        if settings_snapshot is not None
        else load_sound_settings()
    )

    payload = {
        "profile": profile.to_dict(),
        "curves": curve_payload(),
        "preview": response_preview(profile),
        "headroom_db": estimate_headroom_db(profile),
        # 0 when the profile is disabled (bypass) OR flat (no active filters).
        # The page opens on Off vs Saved based on this.
        "filter_count": len(build_sound_filters(profile)),
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
            # One owner (volume_curve.DEFAULT_VOLUME_FLOOR_DB, re-exported via
            # sound.settings) → this payload → the page's reset control.
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
    """Serializable form of the live output-hardware state, or ``None``.

    These payloads are emitted with plain ``json.dumps``, which cannot encode
    the frozen ``OutputHardwareState``; ``to_dict`` is the single conversion
    boundary. The page keeps this envelope key separate from the topology's own
    ``hardware`` block: topology hardware is the saved speaker contract, this is
    the currently observed attachment state (also mirrored by ``/state`` as
    ``audio.output_hardware``).
    """
    hardware = load_output_hardware_state()
    return hardware.to_dict() if hardware is not None else None


def _i2s_hat_collision_warnings(
    profile_id: str | None, boot_config_path: str | Path, *, detected: bool
) -> list[str]:
    """Re-derive (read-only) whether applying ``profile_id`` would collide.

    ``jasper-audio-hardware-reconcile`` owns the actual write and is the
    one place a collision gets refused; this recomputes the same pure
    check against the live config.txt purely to surface it in the wizard.
    """
    if profile_id is None:
        return []
    try:
        content = Path(boot_config_path).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        _, _, collision = render_i2s_hat_boot_config(content, profile_id)
    except ValueError:
        return []
    if collision is None:
        return []
    remedy = (
        "Remove the hand-written line, then deploy or reboot."
        if detected
        else "Remove the existing line, then try again."
    )
    return [
        f"A hand-written dtoverlay={overlay} line is already in config.txt; "
        f"the {collision.managed_overlay} boot line was not written. {remedy}"
        for overlay in collision.colliding_overlays
    ]


def _i2s_hat_payload(
    *,
    intent_path: str | Path = DEFAULT_I2S_HAT_INTENT_PATH,
    boot_config_path: str | Path = DEFAULT_BOOT_CONFIG_PATH,
    hat_dir: str | Path = DEFAULT_HAT_DIR,
) -> dict[str, Any]:
    profiles = selectable_i2s_hat_profiles()
    hardware = _output_hardware_dict() or {}
    topology = str(
        (hardware.get("usb_data_role") or {}).get("board_topology") or "unknown"
    )
    available = topology in {"shared_otg_port", "separate_host_ports"}
    reason = ""
    if not available:
        reason = "I²S HAT setup requires a recognized Raspberry Pi."
    intent_error = ""
    try:
        desired_profile_id = read_i2s_hat_intent(intent_path)
    except (OSError, UnicodeError, ValueError) as exc:
        desired_profile_id = None
        intent_error = str(exc)
    # A HAT that names itself in its EEPROM is reconciled without the operator
    # choosing anything, so the wizard reports it instead of offering it. On a
    # board the reconciler will not manage, it reports nothing.
    detected = detected_i2s_hat_profile(hat_dir) if available else None
    resolved_id = detected.id if detected is not None else desired_profile_id
    return {
        "visibility": "visible",
        "available": available,
        "shared_usb_data_port": topology == "shared_otg_port",
        "reason": reason,
        "intent_error": intent_error,
        "profiles": [{"id": p.id, "label": p.label} for p in profiles],
        "desired_profile_id": desired_profile_id,
        "detected_profile_id": detected.id if detected is not None else None,
        "detected_label": detected.label if detected is not None else "",
        "warnings": _i2s_hat_collision_warnings(
            resolved_id, boot_config_path, detected=detected is not None
        ),
        "restart_required": Path(I2S_HAT_REBOOT_REQUIRED_PATH).is_file(),
    }


def _save_i2s_hat_payload(
    profile_id: str | None,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    from jasper.control.restart_broker import manage_units

    with _sound_state_write_lock:
        status = _i2s_hat_payload()
        if not status["available"]:
            raise ValueError(status["reason"] or "I²S HAT setup is unavailable")
        write_i2s_hat_intent(profile_id)
        try:
            result = manage_units(
                I2S_HAT_RECONCILE_UNIT,
                verb="start",
                reason="sound i2s hat setting",
                no_block=False,
                timeout=55.0,
            )
        except (OSError, RuntimeError) as exc:
            result = {"ok": False, "error": str(exc)}
        payload = _i2s_hat_payload()
    outcome = "applied" if result.get("ok") else "error"
    log_event(logger, "sound.i2s_hat", result=outcome, desired=profile_id or "auto")
    return payload, result


def _output_topology_payload() -> dict[str, Any]:
    snapshot = load_output_topology_snapshot()
    topology = snapshot.topology
    observed_hardware = load_output_hardware_state()
    repin = composite_serial_repin_plan(topology, observed_hardware)

    return {
        "output_topology": topology.to_dict(include_evaluation=True),
        "topology_revision": snapshot.revision,
        "output_hardware": (
            observed_hardware.to_dict() if observed_hardware is not None else None
        ),
        "hardware_adoption": detected_hardware_adoption_precondition(
            observed_hardware
        ),
        "hardware_mismatch": declared_hardware_mismatch(topology, observed_hardware),
        "hardware_repin": repin.to_dict() if repin is not None else None,
        "i2s_hat": _i2s_hat_payload(),
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
        "active_playback_route": _active_speaker_playback_route_payload(topology),
    }


def _refuse_undrivable_layout(topology: OutputTopology) -> None:
    """Refuse a layout this box's DAC can never drive, before anything is saved.

    A roleful (crossover / protected / subwoofer) layout on a DAC that declares
    no active outputd lane leaves CamillaDSP playing into the active loopback
    lane while outputd captures the passive one: structurally silent with every
    daemon reporting healthy, and unrepairable downstream.
    """

    from jasper.active_speaker.playback_route import (
        ActiveLaneCapabilityGap,
        active_lane_capability_gap,
    )

    gap = active_lane_capability_gap(topology)
    # An unrecognized DAC profile is not proof the layout is undrivable — see
    # active_lane_capability_gap's docstring — so it must not block the save.
    if not isinstance(gap, ActiveLaneCapabilityGap):
        return
    log_event(
        logger,
        "sound.output_topology_save",
        level=logging.WARNING,
        result="blocked",
        reason="dac_no_active_lane",
        device_id=gap.device_id,
        topology_id=topology.topology_id,
    )
    raise OutputTopologyCapabilityBlocked(
        f"{gap.device_label} does not support the active speaker lane. Active "
        "crossover and subwoofer layouts need an active-capable DAC; choose a "
        "passive speaker layout for this hardware (passive sends full-range "
        "audio to every output — only safe when the speaker has its own "
        "built-in passive crossover), or attach an active-capable DAC."
    )


def _save_output_topology_payload(
    raw: dict[str, Any],
    *,
    require_revision: bool = False,
) -> dict[str, Any]:
    """Replace saved speaker intent only after audio is proven parked."""

    from jasper.active_speaker.runtime_convergence import park_and_commit_topology
    from jasper.output_topology_runtime import RECONCILE_UNIT, trigger_reconcile

    def verify_revision(revision: str) -> None:
        if not require_revision:
            return
        expected_revision = str(raw.get("topology_revision") or "")
        if not expected_revision or expected_revision != revision:
            raise OutputTopologyRevisionConflict(
                "speaker layout changed in another session; refresh hardware before saving"
            )

    # One domain-owned transaction covers stale validation, park, durable
    # commit, and the synchronous reconcile request. A competing writer cannot
    # pass validation on the same revision or resurrect pre-reset state.
    with output_topology_mutation() as mutation:
        snapshot = mutation.snapshot()
        verify_revision(snapshot.revision)
        raw_topology = raw.get("output_topology", raw)
        topology = OutputTopology.from_mapping(raw_topology)
        _refuse_undrivable_layout(topology)
        topology, guards_changed = _request_missing_software_guards(topology)
        summed_stop = _active_speaker_stop_summed_test_tone(
            reason="output_topology_save"
        )
        tone_stop = _active_speaker_stop_commission_tone(
            reason="output_topology_save"
        )
        safe_stop = _active_speaker_stop_payload()
        def commit_topology() -> OutputTopology:
            mutation.save(topology)
            return topology

        runtime = park_and_commit_topology(snapshot.topology, commit_topology)
        reconcile = trigger_reconcile(reason="output_topology_save")
        if not reconcile.get("ok"):
            log_event(
                logger,
                "sound.output_topology_save_reconcile",
                level=logging.WARNING,
                unit=RECONCILE_UNIT,
                error=reconcile.get("error"),
                converging=reconcile.get("converging"),
            )
    evaluation = topology.evaluation()
    log_event(
        logger,
        "sound.output_topology_save",
        topology_id=topology.topology_id,
        status=evaluation["status"],
        device_id=topology.hardware.device_id,
        groups=len(topology.speaker_groups),
        assigned_outputs=evaluation["assigned_output_count"],
        blockers=len(evaluation["blockers"]),
        warnings=len(evaluation["warnings"]),
        software_guards_requested=str(guards_changed),
        runtime_convergence_ok=runtime.convergence.ok,
        reconcile_ok=reconcile.get("ok"),
        reconcile_converging=reconcile.get("converging"),
        summed_stop=str(summed_stop.get("status")),
        tone_stop=str(tone_stop.get("status")),
        safe_stop=str(safe_stop.get("status")),
    )
    needs_attention_save = {
        "status": "needs_attention",
        "message": (
            "Speaker layout was saved, but audio remains off. "
            "Open Status before continuing."
        ),
    }
    if not runtime.convergence.ok:
        save = needs_attention_save
    elif reconcile.get("converging"):
        # The reconciler is still running past trigger_reconcile's own wait
        # budget (#3094) -- not a failure, so it must not read as one.
        save = {
            "status": "converging",
            "message": (
                "Speaker layout was saved and is still applying. "
                "Check Status in a moment."
            ),
        }
    elif not reconcile.get("ok"):
        save = needs_attention_save
    else:
        save = {"status": "saved", "message": "Saved speaker layout."}
    return {
        **_output_topology_payload(),
        "runtime_convergence": runtime.convergence.to_dict(),
        "reconcile": reconcile,
        "save": save,
    }


def _verified_detected_hardware(
    raw: Mapping[str, Any], *, revision: str
) -> OutputHardwareState | None:
    """Validate the browser's topology and detected-hardware snapshot.

    The reconciler owns the observed hardware file, so callers re-run this
    check after parking, before they commit. Returns the reconciler's
    observation whatever its adoption verdict; each action decides what it can
    do with it.
    """

    expected_revision = raw.get("topology_revision")
    expected_identity = raw.get("detected_hardware_identity")
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ValueError("topology_revision is required")
    if not isinstance(expected_identity, str) or not expected_identity:
        raise ValueError("detected_hardware_identity is required")
    if expected_revision != revision:
        raise OutputHardwareRequestConflict("topology_changed")
    observed = load_output_hardware_state()
    adoption = detected_hardware_adoption_precondition(observed)
    if expected_identity != adoption["identity"]:
        raise OutputHardwareRequestConflict("detected_hardware_changed")
    return observed


def _reset_request_hardware(
    raw: Mapping[str, Any], *, revision: str
) -> OutputHardware | None:
    """Return the detected hardware a reset may adopt, or ``None``."""

    observed = _verified_detected_hardware(raw, revision=revision)
    if observed is None:
        return None
    if not detected_hardware_adoption_precondition(observed)["allowed"]:
        return None
    return OutputHardware.from_mapping(topology_hardware_from_state(observed))


def _reset_output_topology_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Clear speaker setup to a silent unconfigured topology.

    One operation serves both the contextual detected-hardware action (hidden
    until the reconciler can name usable hardware) and the lower recovery
    control (may still clear stale setup with nothing usable attached).
    Neither path loads a flat graph.
    """

    from jasper.active_speaker.reset import clear_active_speaker_setup_state
    from jasper.active_speaker.runtime_convergence import park_and_commit_topology
    from jasper.output_topology_runtime import trigger_reconcile

    if not isinstance(raw, Mapping):
        raise ValueError("reset request must be an object")

    with output_topology_mutation() as mutation:
        snapshot = mutation.snapshot()
        _reset_request_hardware(raw, revision=snapshot.revision)
        summed_stop = _active_speaker_stop_summed_test_tone(
            reason="output_topology_reset"
        )
        tone_stop = _active_speaker_stop_commission_tone(
            reason="output_topology_reset"
        )
        safe_stop = _active_speaker_stop_payload()
        setup_reset: dict[str, Any]
        saved_revision: str

        def commit_unconfigured() -> OutputTopology:
            nonlocal saved_revision, setup_reset
            detected_hardware = _reset_request_hardware(
                raw, revision=snapshot.revision
            )
            if detected_hardware is not None:
                after = new_topology_draft(hardware=detected_hardware)
            else:
                # Recovery remains possible without attached hardware. Preserve
                # the last known DAC description only as topology metadata;
                # zero groups means unconfigured and runtime stays parked.
                after = new_topology_draft(hardware=snapshot.topology.hardware)
            saved_revision = mutation.save(after)
            try:
                setup_reset = clear_active_speaker_setup_state()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                # Cleanup follows the durable intent write. It may require
                # attention, but it must never roll back that write or restore
                # the prior audible graph.
                setup_reset = {
                    "status": "partial",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            return after

        runtime = park_and_commit_topology(
            snapshot.topology,
            commit_unconfigured,
        )
        reconcile = trigger_reconcile(reason="output_topology_reset")
    adoption = detected_hardware_adoption_precondition(load_output_hardware_state())
    needs_attention_reset = {
        "status": "needs_attention",
        "message": (
            "Speaker setup was reset and audio is off. JTS could not finish "
            "setup cleanup; open Status before continuing."
        ),
    }
    if setup_reset.get("status") == "partial" or not runtime.convergence.ok:
        reset_result = needs_attention_reset
    elif reconcile.get("converging"):
        # The reconciler is still running past trigger_reconcile's own wait
        # budget (#3094) -- not a failure, so it must not read as one.
        reset_result = {
            "status": "converging",
            "message": (
                "Speaker setup was reset and is still applying. "
                "Check Status in a moment."
            ),
        }
    elif not reconcile.get("ok"):
        reset_result = needs_attention_reset
    else:
        reset_result = {
            "status": "reset",
            "message": "Speaker setup was reset. Audio is off until you choose a speaker layout.",
        }
    log_event(
        logger,
        "sound.output_topology_reset",
        result=reset_result["status"],
        topology_revision=saved_revision,
        hardware_ready=str(bool(adoption["allowed"])),
        cleanup_status=str(setup_reset.get("status")),
        cleanup_error=setup_reset.get("error"),
        runtime_convergence_ok=runtime.convergence.ok,
        reconcile_ok=str(bool(reconcile.get("ok"))),
        reconcile_converging=str(bool(reconcile.get("converging"))),
        summed_stop=str(summed_stop.get("status")),
        tone_stop=str(tone_stop.get("status")),
        safe_stop=str(safe_stop.get("status")),
    )
    payload = _output_topology_payload()
    payload["reset"] = reset_result
    payload["saved"] = True
    payload["runtime_convergence"] = runtime.convergence.to_dict()
    payload["reconcile"] = reconcile
    return payload


def _repin_output_topology_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Re-pin a same-shape composite onto the DAC units now attached.

    The narrow counterpart to the reset above. A speaker group, its roles, its
    physical-output assignment, and the crossover/commissioning design are all
    keyed to physical output INDEX, never to a DAC serial, so a replacement
    unit of the same kind in the same USB port invalidates none of them — while
    the reset's wipe would throw all of it away. The two things a swap does
    invalidate (per-lane identity for the replaced unit, and a drift
    measurement of two crystals that never ran together) are cleared by
    ``repin_composite_child_serials``.
    """

    from jasper.active_speaker.runtime_convergence import park_and_commit_topology
    from jasper.output_topology_runtime import trigger_reconcile

    if not isinstance(raw, Mapping):
        raise ValueError("re-pin request must be an object")

    with output_topology_mutation() as mutation:
        snapshot = mutation.snapshot()
        observed = _verified_detected_hardware(raw, revision=snapshot.revision)
        plan = composite_serial_repin_plan(snapshot.topology, observed)
        if plan is None:
            raise OutputHardwareRequestConflict("repin_unavailable")
        summed_stop = _active_speaker_stop_summed_test_tone(
            reason="output_topology_repin"
        )
        tone_stop = _active_speaker_stop_commission_tone(
            reason="output_topology_repin"
        )
        safe_stop = _active_speaker_stop_payload()
        saved_revision = ""

        def commit_repin() -> OutputTopology:
            # Re-read the reconciler's observation after parking: a dongle can
            # leave between the offer and the commit, and its identity token is
            # what proves this re-pin still names attached hardware.
            nonlocal saved_revision
            current = _verified_detected_hardware(raw, revision=snapshot.revision)
            after = repin_composite_child_serials(snapshot.topology, current)
            saved_revision = mutation.save(after)
            return after

        runtime = park_and_commit_topology(
            snapshot.topology,
            commit_repin,
            # The graph selector proves a graph legal for the saved SHAPE, which
            # a re-pin does not change — so it would happily resume the approved
            # active runtime through DACs nobody has confirmed by ear yet. Stay
            # parked instead; the arm ladder's identity gates own the way back.
            stay_parked=True,
            parked_reason=(
                "parked after a DAC re-pin; confirm the re-pinned outputs and "
                "re-arm before audio resumes"
            ),
        )
        reconcile = trigger_reconcile(reason="output_topology_repin")
    needs_attention_repin = {
        "status": "needs_attention",
        "message": (
            "The new DAC was pinned and your speaker setup was kept, but audio "
            "remains off. Open Status before continuing."
        ),
    }
    if not runtime.convergence.ok:
        repin_result = needs_attention_repin
    elif reconcile.get("converging"):
        # The reconciler is still running past trigger_reconcile's own wait
        # budget (#3094) -- not a failure, so it must not read as one.
        repin_result = {
            "status": "converging",
            "message": (
                "Pinned the new DAC and kept your speaker setup. Audio is "
                "still applying. Check Status in a moment."
            ),
        }
    elif not reconcile.get("ok"):
        repin_result = needs_attention_repin
    else:
        repin_result = {
            "status": "repinned",
            "message": (
                "Pinned the new DAC and kept your speaker setup. Confirm these "
                "outputs again: "
                + ", ".join(plan.reverify_output_labels)
                + ". Then re-run the drift measurement."
            ),
        }
    log_event(
        logger,
        "sound.output_topology_repin",
        result=repin_result["status"],
        topology_revision=saved_revision,
        device_id=snapshot.topology.hardware.device_id,
        replaced_children=plan.replaced_child_count,
        child_count=plan.child_count,
        reverify_outputs=len(plan.reverify_output_indexes),
        runtime_convergence_ok=runtime.convergence.ok,
        reconcile_ok=str(bool(reconcile.get("ok"))),
        reconcile_converging=str(bool(reconcile.get("converging"))),
        summed_stop=str(summed_stop.get("status")),
        tone_stop=str(tone_stop.get("status")),
        safe_stop=str(safe_stop.get("status")),
    )
    payload = _output_topology_payload()
    payload["repin"] = repin_result
    payload["saved"] = True
    payload["runtime_convergence"] = runtime.convergence.to_dict()
    payload["reconcile"] = reconcile
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
    speaker_group_id = str(raw.get("speaker_group_id") or raw.get("group_id") or "")
    role = str(raw.get("role") or "")
    verified = raw.get("identity_verified")
    if not isinstance(verified, bool):
        raise ValueError("identity_verified must be a boolean")
    from jasper.active_speaker.runtime_contract import roleful_identity_confirmed
    from jasper.active_speaker.runtime_convergence import park_and_commit_topology

    with output_topology_mutation() as mutation:
        topology = mutation.snapshot().topology
        updated = set_channel_identity_verified(
            topology,
            speaker_group_id=speaker_group_id,
            role=role,
            identity_verified=verified,
        )

        # Un-confirming an ASSIGNED lane of a ROLEFUL topology declares doubt
        # about which driver hangs where — the hazard a DAC swap creates, self
        # declared. Gated on the confirmed -> unconfirmed EDGE: an already
        # unconfirmed box is already parked, and confirming never parks.
        park_needed = (
            roleful_identity_confirmed(topology)
            and not roleful_identity_confirmed(updated)
        )
        # ORDER IS THE SAFETY PROPERTY HERE. The DURABLE half — the cleared flag
        # that makes `roleful_identity_confirmed` refuse an approved graph on
        # every later pass — lands FIRST and unconditionally; the silence is
        # best-effort after it. `park_and_commit_topology` would invert that: it
        # parks BEFORE it commits, so a park failure would discard the declared
        # doubt and leave the lane verified on its approved graph across every
        # reboot — and a park most plausibly fails when the graph is already
        # unhealthy, exactly when the doubt matters most.
        #
        # (The re-pin endpoint does NOT share this shape and keeps its
        # commit-inside-park: a failed park there leaves the old serials pinned,
        # and the hardware mismatch keeps flagging.)
        mutation.save(updated)
        parked = False
        park_error: str | None = None
        if park_needed:
            try:
                park_and_commit_topology(
                    updated,
                    lambda: updated,
                    stay_parked=True,
                    parked_reason=(
                        "parked after an output was marked not confirmed; "
                        "confirm it again and re-arm before audio resumes"
                    ),
                )
                parked = True
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                park_error = f"{type(exc).__name__}: {exc}"
    report = channel_identity_report(updated)
    evaluation = updated.evaluation()
    log_event(
        logger,
        "sound.active_speaker_channel_identity",
        action="mark_verified" if verified else "clear_verified",
        topology_id=updated.topology_id,
        group_id=speaker_group_id,
        role=role,
        status=str(report.get("status")),
        verified="%d/%d"
        % (report.get("verified_channel_count"), report.get("assigned_channel_count")),
        blockers=len(evaluation.get("blockers") or []),
        park_needed=str(park_needed),
        parked=str(parked),
        park_error=park_error,
    )
    payload = _output_topology_payload()
    if park_needed:
        # Say which half actually landed: the doubt is recorded either way, but
        # only the immediate silence can fail, and the household must not be
        # left believing the speaker went quiet when it did not.
        payload["identity_park"] = {
            "parked": parked,
            "message": (
                "Marked not confirmed. The speaker is silent until you confirm "
                "it again and it re-arms."
                if parked
                else "Marked not confirmed, but JTS could not silence the "
                "speaker right now. It stays silent from the next restart. "
                "Open Status before playing anything loud."
            ),
        }
    return payload


def _active_speaker_channel_protection_save_payload(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Mark or clear a saved topology channel's protection evidence."""

    if not isinstance(raw, dict):
        raise ValueError("channel protection request must be an object")
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
    with output_topology_mutation() as mutation:
        topology = mutation.snapshot().topology
        updated = set_channel_protection_status(
            topology,
            speaker_group_id=speaker_group_id,
            role=role,
            protection_status=protection_status,
        )
        mutation.save(updated)
    report = channel_identity_report(updated)
    evaluation = updated.evaluation()
    log_event(
        logger,
        "sound.active_speaker_channel_protection",
        topology_id=updated.topology_id,
        group_id=speaker_group_id,
        role=role,
        protection_status=protection_status,
        status=str(report.get("status")),
        blockers=len(evaluation.get("blockers") or []),
    )
    return _output_topology_payload()


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
    log_event(
        logger,
        "sound.live_draft",
        level=logging.WARNING,
        result="unavailable",
        reason=reason,
        output_trim=f"{output_trim_db:.1f}",
        room_peqs=room_peq_count,
        sound_filters=sound_filter_count,
        err=repr(error),
    )


async def _apply_profile(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    # Settings Apply uses this same ordering boundary. Read settings only after
    # entering it, then keep the durable profile write and live graph/trim emit
    # together so neither operation can finish with the other's stale half.
    with _sound_state_write_lock:
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
    log_event(
        logger,
        "sound.apply",
        enabled=str(stamped.enabled),
        curve=stamped.curve_id,
        simple=(
            f"{stamped.simple_eq.sub_bass_db:.1f}/"
            f"{stamped.simple_eq.bass_db:.1f}/"
            f"{stamped.simple_eq.mid_db:.1f}/"
            f"{stamped.simple_eq.presence_db:.1f}/"
            f"{stamped.simple_eq.treble_db:.1f}"
        ),
        bands=len(stamped.parametric_bands),
        room_peqs=apply_state.room_peq_count or 0,
        config=out_path,
        op_id=apply_state.op_id,
    )
    # Previews and the optional library are assembled outside the ordering
    # boundary; the captured settings keep the response coherent with the DSP
    # transaction without holding the lock across unrelated file I/O.
    payload = _state_payload(
        stamped,
        library_path=library_path,
        include_library=library_path is not None,
        settings_snapshot=settings,
        last_dsp_apply_snapshot=apply_state.to_dict(),
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
    pre-lock fast-check, and wrapped as ``DspApplyError`` (its ``__cause__``)
    from the durable path's in-lock re-check in a concurrent-swap race. Both
    map to a typed 200 body instead of a 502.
    """
    from jasper.sound.graph_carrier import CarrierCannotHostEq

    if isinstance(exc, CarrierCannotHostEq):
        return exc
    cause = exc.__cause__
    if isinstance(cause, CarrierCannotHostEq):
        return cause
    return None


async def _apply_settings(
    changes: Mapping[str, Any] | SoundSettings,
    *,
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    """Merge global sound settings, then make the merged state live.

    EQ and Setup share the one aggregate ``SoundSettings`` file and each page
    posts only the fields it owns, so this fresh-reads and merges recognized
    fields while holding one process-local lock through the durable save, DSP
    re-emit, volume reconciliation, and response snapshots. The profile content
    is not re-stamped or re-persisted. A full ``SoundSettings`` is also accepted
    for internal callers and tests. A failed live re-apply returns the saved
    state with a ``warning`` rather than reverting a setting already kept.
    """
    raw_changes = changes.to_dict() if isinstance(changes, SoundSettings) else changes
    recognized = {
        key: raw_changes[key]
        for key in _SOUND_SETTINGS_FIELDS
        if key in raw_changes
    }
    with _sound_state_write_lock:
        merged_raw = load_sound_settings().to_dict()
        merged_raw.update(recognized)
        settings = SoundSettings.from_mapping(merged_raw)
        save_sound_settings(settings)
        log_event(
            logger,
            "sound.settings",
            headroom_trim=f"{settings.headroom_trim_db:.1f}",
            match_loudness=str(settings.match_loudness),
            volume_floor_db=f"{settings.volume_floor_db:.1f}",
        )
        profile = load_profile(profile_path)
        warning: str | None = None
        volume_warning: str | None = None
        apply_result: tuple[Any, Path, SoundProfile] | None = None
        reconciled = False
        try:
            apply_result = await _load_profile_config(
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
            warning = f"Saved, but applying to the speaker failed: {e}"

        try:
            reconciled = await _reconcile_volume_curve_after_settings(
                camilla_factory=camilla_factory,
            )
        except (AttributeError, OSError, RuntimeError) as e:
            logger.warning("volume floor saved but volume reconcile failed: %s", e)
            volume_warning = (
                "Saved, but the current volume will use the new floor on the next "
                f"volume change: {e}"
            )

        # Capture the response's live-state anchor after every side effect,
        # still serialized; rendering happens outside the boundary.
        if apply_result is not None:
            apply_state, out_path, _ = apply_result
            last_dsp_apply_snapshot = apply_state.to_dict()
        else:
            from jasper.dsp_apply import last_dsp_apply_state

            last_dsp_apply_snapshot = last_dsp_apply_state()

    payload = _state_payload(
        profile,
        library_path=library_path,
        include_library=library_path is not None,
        settings_snapshot=settings,
        last_dsp_apply_snapshot=last_dsp_apply_snapshot,
    )
    if warning is not None:
        payload["warning"] = warning
    if volume_warning is not None:
        payload["volume_warning"] = volume_warning
    if apply_result is not None:
        payload["active_config_path"] = str(out_path)
        payload["preserved_room_peqs"] = apply_state.room_peq_count or 0
        payload["last_dsp_apply"] = apply_state.to_dict()
        payload["dsp_write_epoch"] = apply_state.op_id
    if reconciled:
        payload["volume_reconciled"] = True
    return payload


async def _reconcile_volume_curve_after_settings(
    *,
    camilla_factory: Callable[[], Any] = _camilla,
) -> bool:
    """Apply the newly saved floor to the current listening level when safe.

    ``maybe_reconcile_camilla`` only writes for camilla-master sources
    (idle/AirPlay/USB), so changing the floor cannot unguard a
    Spotify/Bluetooth push-mode handoff.
    """
    from jasper import librespot_state
    from jasper.renderer import RendererClient
    from jasper.volume_coordinator import VolumeCoordinator
    from jasper.volume_persistence import VolumePersistence
    from jasper.volume_persistence import configured_path as volume_state_path

    coord = VolumeCoordinator(
        camilla=camilla_factory(),
        persistence=VolumePersistence(volume_state_path()),
        backend=RendererClient(
            librespot_state_path=librespot_state.configured_path(),
        ),
    )
    try:
        coord.load_persisted_level()
        await coord.maybe_reconcile_camilla()
        return True
    finally:
        await coord.aclose()


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
    log_event(
        logger,
        "sound.audition",
        mode=audition_mode,
        enabled=str(loaded.enabled),
        curve=loaded.curve_id,
        bands=len(loaded.parametric_bands),
        output_trim=f"{output_trim_db:.1f}",
        room_peqs=apply_state.room_peq_count or 0,
        config=out_path,
        op_id=apply_state.op_id,
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

    The web route and the calibration-advisor action runner share this
    implementation, so model-suggested auditions inherit ``/sound/audition``'s
    config validation, room-PEQ preservation and no-persist semantics.
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
    config_dir: str | Path,
    profile_path: str | Path | None = None,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    """Load a bounded preference-EQ draft into the active Camilla config.

    The low-latency editing path: no profile persistence, no config-file
    pointer change, no shared apply-state mutation. The durable Save/Apply path
    is `_apply_profile`, which writes a validated YAML file and records
    rollback state.

    Returns only `live_status` and `dsp_write_epoch` — the browser reads
    nothing else from this response (`runLiveDraft` in
    `deploy/assets/sound-profile/js/main.js`).
    """
    from jasper.dsp_apply import dsp_write_epoch, dsp_writer_lock
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.live_edit import does_live_edits, plan_live_edit_for

    cam = camilla_factory()
    config_path = Path(config_dir)
    settings = load_sound_settings()
    # The trim is derived from the SAVED profile, never from the draft, so an
    # edit cannot move it. Match-loudness makes the trim a function of the
    # profile's own EQ, so a draft-derived trim would fold into
    # `active_baseline_headroom`'s VALUE and be written in place — an instant,
    # un-ducked, full-spectrum level step mid-drag. The durable save realises
    # the change instead, once and on the same rule (ADR-0219). `settings` is
    # still re-read above, so a settings-page change to headroom_trim_db or
    # match_loudness does move the trim, at one swap; the property this buys
    # is only "not draft-derived". Safe because the trim is comfort accounting,
    # not a clip guard — `devices.volume_limit` stays the hard ceiling regardless
    # (`jasper.camilla_stereo_prefix`). The cost is that match-loudness stops
    # tracking the draft until save.
    output_trim_db = _output_trim(load_profile(profile_path), settings)
    sound_filter_count = len(build_sound_filters(profile))

    def _live_payload(*, status: str, current_epoch: str) -> dict[str, Any]:
        return {"live_status": status, "dsp_write_epoch": current_epoch}

    if not does_live_edits(cam):
        current_epoch = dsp_write_epoch()
        _log_live_draft_unavailable(
            reason="active_config_raw_unavailable",
            output_trim_db=output_trim_db,
            room_peq_count=0,
            sound_filter_count=sound_filter_count,
            error=None,
        )
        return _live_payload(status="unavailable", current_epoch=current_epoch)

    async with dsp_writer_lock(config_path, source="sound_live_draft"):
        current_epoch = dsp_write_epoch()
        if expected_dsp_write_epoch != current_epoch:
            log_event(
                logger,
                "sound.live_draft",
                result="stale",
                expected_epoch=str(expected_dsp_write_epoch),
                current_epoch=str(current_epoch),
            )
            return _live_payload(status="stale", current_epoch=current_epoch)

        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            raise RuntimeError("CamillaDSP did not report a loaded config path")

        carrier = carrier_for_loaded_config(current_path, config_dir=config_path)
        result = carrier.reemit(
            profile,
            profile_id=f"live-{time.time_ns()}",
            output_trim_db=output_trim_db,
            fanin_coupling_capture_kwargs=coupling_capture_kwargs_from_env(),
        )
        yaml = result.yaml
        plan = await plan_live_edit_for(cam, yaml)
        method = plan.method

        try:
            # Duck-or-not is decided in jasper.sound.live_edit; an unchanged
            # graph is not written at all.
            if method != "unchanged":
                await cam.set_active_config_raw(
                    yaml, best_effort=False, duck=plan.duck,
                )
        except Exception as e:  # noqa: BLE001
            _log_live_draft_unavailable(
                reason=f"{method}_failed",
                output_trim_db=output_trim_db,
                room_peq_count=result.room_peq_count,
                sound_filter_count=sound_filter_count,
                error=e,
            )
            return _live_payload(status="unavailable", current_epoch=current_epoch)

        log_event(
            logger,
            "sound.live_draft",
            result="live",
            method=method,
            # Empty unless the edit fell back to a ducked pipeline replace, and
            # then it names the section that moved — the field that explains an
            # audible fade to whoever reads this line.
            swap_reason=plan.reason,
            output_trim=f"{output_trim_db:.1f}",
            room_peqs=result.room_peq_count,
            sound_filters=sound_filter_count,
            active_anchor=str(current_path),
            epoch=str(current_epoch),
        )
        return _live_payload(status="live", current_epoch=current_epoch)


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


def _sound_page_island(*, page_mode: str, follower: bool) -> str:
    """The one ``sound-page-data`` island both /sound/ shells render.

    The editor's filter and slope pickers are built from the crossover
    vocabulary carried here, read from the compiler rather than restated, so a
    value the compiler cannot build is never presented. The defaults ride along
    because the picker must pre-select the same member ``crossover_preview``
    would fill in.
    """

    from jasper.active_speaker.crossover_preview import (
        DEFAULT_FILTER_TYPE,
        DEFAULT_SLOPE_DB_PER_OCTAVE,
    )
    from jasper.active_speaker.declaration_vocabulary import (
        supported_declaration_filter_types,
        supported_declaration_slopes_db_per_octave,
    )

    return json_island(
        "sound-page-data",
        {
            "mode": page_mode,
            "follower": follower,
            "crossover_vocabulary": {
                "filter_types": list(supported_declaration_filter_types()),
                "slopes_db_per_octave": list(
                    supported_declaration_slopes_db_per_octave()
                ),
                "default_filter_type": DEFAULT_FILTER_TYPE,
                "default_slope_db_per_octave": DEFAULT_SLOPE_DB_PER_OCTAVE,
            },
        },
    )


def _follower_sound_html(csrf_token: str = "", *, page_mode: str) -> bytes:
    """Render one split Sound page for a bonded active follower.

    A bonded follower delegates the PROGRAM domain (content EQ, room
    correction, volume shaping) to the pair leader but still owns its LOCAL
    driver domain (the per-driver crossover / limiter / tweeter high-pass that
    protects the DAC it drives). Setup keeps the delegation card and mounts the
    same active-speaker UI as a solo box; EQ is a delegation-only page with a
    path back to local Setup.

    The page island tells main.js to boot in follower Setup mode: only the
    active-speaker section, no Off/Saved/Draft editor or now-playing plot.
    Content-DSP POSTs still 409 (``_FOLLOWER_BLOCKED_CONTENT_DSP_POSTS``); the
    active-speaker commissioning/crossover endpoints are allowed.
    """
    page_mode = page_mode if page_mode in {"eq", "setup"} else "eq"
    leader_path = "/eq/" if page_mode == "eq" else "/sound/setup/"
    leader_sound_url = bonded_follower_leader_web_url(leader_path)
    leader_link = (
        '<a class="btn btn--primary" href="'
        + html.escape(leader_sound_url)
        + '">Open leader sound</a>'
        if leader_sound_url
        else ""
    )
    page_island = _sound_page_island(page_mode=page_mode, follower=True)
    title = "EQ" if page_mode == "eq" else "Sound setup"
    local_setup = (
        '<div id="view-body"></div>'
        '<div class="status-line" id="status" role="status" aria-live="polite"></div>'
        '<script type="module" src="/assets/sound-profile/js/main.js"></script>'
        if page_mode == "setup"
        else ""
    )
    local_setup_link = (
        '<a class="btn" href="/sound/setup/">Open local sound setup</a>'
        if page_mode == "eq"
        else ""
    )
    header = canonical_header(title, back_id="back")
    body = f"""
{header}
<main class="page">
  <section class="info-card info-card--accent" role="note">
    <h2 class="section__title">Sound is controlled by the pair leader</h2>
    <p class="form-hint">This speaker is an active follower, so content EQ,
    room correction, and volume shaping are rendered by the leader while the
    pair is active. Local crossover and driver-protection work stays with the
    speaker that owns the DAC path.</p>
    <div class="actions">
      {leader_link}
      {local_setup_link}
      <a class="btn" href="/rooms/">Manage pair</a>
    </div>
  </section>
  {local_setup}
</main>
{page_island}
"""
    return canonical_page(
        title,
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/sound-profile/sound.css",
    )


def _index_html(csrf_token: str = "", *, page_mode: str = "eq") -> bytes:
    page_mode = page_mode if page_mode in {"eq", "setup"} else "eq"
    if bonded_follower_active():
        return _follower_sound_html(csrf_token, page_mode=page_mode)
    title = "EQ" if page_mode == "eq" else "Sound setup"
    eq_tabs_html = (
        '<div><div class="segmented" role="tablist" aria-label="Sound source">'
        '<button class="segmented__btn" id="tab-off" data-view="off" aria-pressed="true">Off</button>'
        '<button class="segmented__btn" id="tab-saved" data-view="saved" aria-pressed="false">Saved</button>'
        '<button class="segmented__btn" id="tab-draft" data-view="draft" aria-pressed="false">Draft</button>'
        '</div></div>'
    )
    editor_chrome = (
        canonical_header(title, back_id="back", tabs_html=eq_tabs_html)
        + """
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
"""
        if page_mode == "eq"
        else canonical_header(title, back_id="back")
        + """
<main class="page">
  <div id="view-body"></div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</main>
"""
    )
    page_island = _sound_page_island(page_mode=page_mode, follower=False)
    body = editor_chrome + page_island + (
        '<script type="module" src="/assets/sound-profile/js/main.js"></script>'
    )
    return canonical_page(
        title,
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/sound-profile/sound.css",
    )


def _active_speaker_environment_payload() -> dict[str, Any]:
    """Return read-only active-speaker readiness for the /sound/ advanced card."""

    from jasper.active_speaker.environment import probe_active_speaker_environment

    evidence_path = _active_speaker_path_safety_evidence_path()
    report = probe_active_speaker_environment(
        path_safety_evidence_path=evidence_path or None,
    )
    log_event(
        logger,
        "sound.active_speaker_environment",
        status=str(report.get("status")),
        load_gate=str(report.get("load_gate")),
        blockers=int(report.get("blocker_count") or 0),
        safe_playback=str(bool(report.get("safe_playback", {}).get("playback_allowed"))),
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
        1
        for issue in payload.get("issues") or []
        if isinstance(issue, dict) and issue.get("severity") == "blocker"
    )
    log_event(
        logger,
        "sound.active_speaker_stage_config",
        status=str(payload.get("status")),
        topology_id=str(payload.get("topology", {}).get("topology_id")),
        preset_id=str(payload.get("preset", {}).get("preset_id")),
        preview_status=str(crossover_preview.get("status")),
        config=str(payload.get("config", {}).get("basename")),
        blockers=blocker_count,
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
    from jasper.active_speaker.safe_playback import load_safe_playback_state

    if raw is None:
        return load_calibration_level_state()
    if not isinstance(raw, dict):
        raise ValueError("calibration level request must be an object")
    action = str(raw.get("action") or "set")
    level = raw.get("level_dbfs", raw.get("requested_level_dbfs"))
    # Bind the persisted level to the current commissioning run (the active
    # safe_playback session) so a previous session's test level cannot seed it.
    run_id = load_safe_playback_state().get("session_id")
    payload = update_calibration_level_state(
        action=action,
        requested_level_dbfs=level,
        observed_mic_dbfs=raw.get("observed_mic_dbfs"),
        mic_clipping=bool(raw.get("mic_clipping")),
        run_id=run_id,
    )
    log_event(
        logger,
        "sound.active_speaker_calibration_level",
        action=str(payload.get("last_action")),
        level_dbfs=str(payload.get("test_signal", {}).get("requested_level_dbfs")),
        prior_level_dbfs=str(payload.get("prior_level_dbfs")),
        delta_db=str(payload.get("applied_delta_db")),
        mic_status=str(payload.get("mic_meter", {}).get("status")),
        mic_recommendation=str(payload.get("mic_meter", {}).get("recommendation")),
        issues=len(payload.get("issues") or []),
    )
    return payload


def _active_speaker_stop_payload() -> dict[str, Any]:
    """Stop any no-audio active-speaker safety session."""

    from jasper.active_speaker.calibration_level import update_calibration_level_state
    from jasper.active_speaker.playback import stop_tone_playback
    from jasper.active_speaker.safe_playback import stop_safe_playback_session

    playback = stop_tone_playback(reason="operator_stop")
    state = dict(stop_safe_playback_session())
    try:
        state["calibration_level"] = update_calibration_level_state(
            action="stop", run_id=state.get("session_id")
        )
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "sound.active_speaker_calibration_level",
            level=logging.WARNING,
            action="stop_reset",
            result="error",
            error=type(e).__name__,
        )
        state["calibration_level"] = {
            "status": "reset_failed",
            "error": str(e),
        }
    log_event(
        logger,
        "sound.active_speaker_safe_playback",
        action="stop",
        status=str(state.get("status")),
        session_id=str(state.get("session_id")),
        playback_status=str(playback.get("status")),
        audio_emitted=str(bool(playback.get("audio_emitted"))),
        level_status=str(state.get("calibration_level", {}).get("status")),
    )
    return state


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
    )
    log_event(
        logger,
        "sound.active_speaker_bringup_preflight",
        status=str(payload.get("status")),
        manual_available=str(bool(payload.get("manual_bringup_available"))),
        guided_available=str(bool(payload.get("guided_calibration_available"))),
        microphone=str(payload.get("microphone", {}).get("status")),
        guard=str(payload.get("software_guard", {}).get("status")),
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
    log_event(
        logger,
        "sound.active_speaker_startup_load",
        status=str(payload["state"].get("status")),
        preflight=str(payload["preflight"].get("status")),
        rollback_available=str(bool(payload["state"].get("rollback_available"))),
    )
    return payload


def _active_speaker_tuning_handoff_payload() -> dict[str, Any]:
    """Mint the AI-operator handoff prompt and the binding it was minted for.

    Read-only and audio-free. Readiness comes from the same baseline-profile
    payload the page renders its active-profile card from, so the route cannot
    offer a handoff the card beside it hides.
    """

    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.tuning_handoff import build_tuning_handoff

    design_draft = load_design_draft()
    payload = build_tuning_handoff(
        # Recompiled on the click rather than read off the page: a tab left
        # open since before a declaration edit must not mint a handoff for a
        # baseline that no longer stands.
        baseline_profile=_active_speaker_baseline_profile_payload(
            design_draft=design_draft
        ),
        design_draft=design_draft,
    )
    log_event(
        logger,
        "sound.active_speaker_tuning_handoff",
        status=str(payload["status"]),
        reason=str(payload["reason"]),
        design_draft_revision=str(payload["binding"]["design_draft_revision"]),
    )
    return payload


def _active_speaker_design_draft_payload() -> dict[str, Any]:
    """Return the saved active-speaker design draft, if any."""

    from jasper.active_speaker.design_draft import load_design_draft

    payload = load_design_draft(topology=load_output_topology())
    log_event(
        logger,
        "sound.active_speaker_design_draft",
        status=str(payload.get("status")),
        driver_count=str((payload.get("summary") or {}).get("driver_count")),
        candidate_count=str(
            (payload.get("summary") or {}).get("crossover_candidate_count")
        ),
    )
    return payload


def _active_speaker_driver_research_request_payload(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Build the silent, target-bound research request and copyable prompt."""

    from jasper.active_speaker.driver_safety import build_driver_research_request
    from jasper.active_speaker.driver_safety_prompt import build_driver_research_prompt
    from jasper.active_speaker.design_draft import (
        normalise_manual_settings,
        normalise_operator_inputs,
    )

    if not isinstance(raw, dict):
        raise ValueError("driver research request must be an object")
    allowed = {"operator_inputs", "manual_settings"}
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "driver research request has unknown fields: " + ", ".join(unknown)
        )
    topology = load_output_topology()
    operator_inputs = normalise_operator_inputs(raw.get("operator_inputs"))
    manual_settings = normalise_manual_settings(raw.get("manual_settings"))
    request = build_driver_research_request(
        topology,
        operator_inputs,
        manual_settings,
    )
    payload = {
        "request": request,
        "prompt": build_driver_research_prompt(request),
        "safety": {
            "no_audio": True,
            "loads_camilla": False,
            "applies_filters": False,
            "authorizes_playback": False,
            "research_is_advisory": True,
        },
    }
    log_event(
        logger,
        "sound.active_speaker_driver_research_request",
        topology_id=topology.topology_id,
        target_count=len(request.get("targets") or []),
        request_fingerprint=str(request.get("request_fingerprint")),
    )
    return payload


def _active_speaker_design_draft_save_payload(
    raw: dict[str, Any], *, durable: bool = False
) -> dict[str, Any]:
    """Persist a design draft from current topology plus bounded research JSON.

    ``durable`` is a caller-only knob (never read from ``raw``, so an HTTP
    body can't set it): the crossover-accept seam
    (:func:`apply_measured_crossover_geometry`) opts in, ordinary wizard
    edits keep the cheaper default.
    """

    from jasper.active_speaker.design_draft import save_design_draft

    if not isinstance(raw, dict):
        raise ValueError("design draft request must be an object")
    allowed = {
        "driver_research_request",
        "driver_research",
        "manual_settings",
        "operator_inputs",
        "expected_revision",
    }
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "design draft request has unknown fields: " + ", ".join(unknown)
        )
    if "expected_revision" not in raw:
        raise ValueError("design draft request requires expected_revision")
    expected_revision = raw.get("expected_revision")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")
    topology, _guards_changed = ensure_missing_software_guards()
    payload = save_design_draft(
        topology,
        driver_research_request=raw.get("driver_research_request"),
        driver_research=raw.get("driver_research"),
        manual_settings=raw.get("manual_settings"),
        operator_inputs=raw.get("operator_inputs"),
        expected_revision=expected_revision,
        durable=durable,
    )
    log_event(
        logger,
        "sound.active_speaker_design_draft_save",
        status=str(payload.get("status")),
        topology_id=topology.topology_id,
        driver_count=str((payload.get("summary") or {}).get("driver_count")),
        candidate_count=str(
            (payload.get("summary") or {}).get("crossover_candidate_count")
        ),
        manual_driver_count=str(
            (payload.get("summary") or {}).get("manual_driver_count")
        ),
        manual_candidate_count=str(
            (payload.get("summary") or {}).get("manual_crossover_candidate_count")
        ),
        safety_profile_status=str(
            (payload.get("driver_safety_profile") or {}).get("status")
        ),
        # #2603: the EVALUATION, not just the stored status, so a box whose
        # profile went un-confirmed is findable in the journal. Carried on this
        # save event rather than a new one — no second grep contract.
        safety_profile_evaluation=str(
            (payload.get("driver_safety_profile_evaluation") or {}).get("status")
        ),
        safety_profile_reasons=",".join(
            str(reason)
            for reason in (
                (payload.get("driver_safety_profile_evaluation") or {}).get("reasons")
                or ()
            )
        ),
        issues=len(payload.get("issues") or []),
    )
    return payload


def apply_measured_crossover_geometry(
    *, expected_revision: int, between_roles: tuple[str, str],
    configured: "CrossoverGeometry", selected: "CrossoverGeometry",
) -> dict[str, Any]:
    """Write a measured crossover onto the Sound declaration. Durable: every
    write through this function is fsynced before it is visible.

    The declaration states a crossover as three fields (corner, filter type,
    slope) and all three go through this one writer, in one write, one fsync
    and one Undo leg: ``baseline_profile``'s
    ``measured_candidate_preset_mismatch`` guard is a whole-preset equality and
    slope compiles into ``CrossoverRegion.order``, so a candidate measured at a
    different slope is as unreconcilable with the saved declaration as one
    measured at a different corner. The compare-and-swap covers all three for
    the same reason: it defends "Sound still says what this review measured".
    """
    from jasper.active_speaker.crossover_declaration import (
        declared_crossover_geometry,
        matching_declared_candidate_index,
    )
    from jasper.active_speaker.design_draft import load_design_draft

    draft = load_design_draft(topology=load_output_topology())
    manual = draft.get("manual_settings")
    if not isinstance(manual, Mapping):
        raise ValueError("Sound has no manual crossover setting to update")
    raw = manual.get("crossover_candidates")
    candidates = list(raw) if isinstance(raw, list) else []
    index = matching_declared_candidate_index(candidates, between_roles)
    if index is None:
        raise ValueError("Sound's matching crossover setting is missing or ambiguous")
    current = declared_crossover_geometry(draft, between_roles)
    if current is None or not current.matches(configured):
        raise ValueError("Sound changed since this measurement; review afresh")
    updated_candidates = [dict(item) for item in candidates]
    updated_candidates[index]["frequency_hz"] = float(selected.fc_hz)
    updated_candidates[index]["filter_type"] = selected.filter_type
    updated_candidates[index]["slope_db_per_octave"] = float(
        selected.slope_db_per_octave
    )
    return _active_speaker_design_draft_save_payload({
        "expected_revision": expected_revision,
        "driver_research_request": draft.get("driver_research_request"),
        "driver_research": draft.get("driver_research"),
        "manual_settings": {**manual, "crossover_candidates": updated_candidates},
        "operator_inputs": draft.get("operator_inputs"),
    }, durable=True)


def _active_speaker_crossover_preview_payload() -> dict[str, Any]:
    """Return the saved no-audio crossover preview, if any."""

    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    payload = load_crossover_preview(current_design_draft=load_design_draft())
    log_event(
        logger,
        "sound.active_speaker_crossover_preview",
        status=str(payload.get("status")),
        active_crossover_count=str(
            (payload.get("summary") or {}).get("active_crossover_count")
        ),
        blocker_count=str((payload.get("summary") or {}).get("blocker_count")),
    )
    return payload


def _active_speaker_crossover_preview_save_payload() -> dict[str, Any]:
    """Persist a no-audio crossover preview from the saved design draft."""

    from jasper.active_speaker.crossover_preview import save_crossover_preview
    from jasper.active_speaker.design_draft import build_design_draft, load_design_draft

    draft = load_design_draft()
    if draft.get("status") not in {"not_saved", "unreadable"}:
        saved_revision = draft.get("revision", 0)
        topology, _guards_changed = ensure_missing_software_guards()
        draft = build_design_draft(
            topology,
            driver_research_request=draft.get("driver_research_request"),
            driver_research=draft.get("driver_research"),
            manual_settings=draft.get("manual_settings"),
            operator_inputs=draft.get("operator_inputs"),
            created_at=draft.get("created_at"),
            updated_at=draft.get("updated_at"),
        )
        draft["revision"] = saved_revision
    payload = save_crossover_preview(draft)
    log_event(
        logger,
        "sound.active_speaker_crossover_preview_save",
        status=str(payload.get("status")),
        topology_id=str((payload.get("source") or {}).get("topology_id")),
        active_crossover_count=str(
            (payload.get("summary") or {}).get("active_crossover_count")
        ),
        blocker_count=str((payload.get("summary") or {}).get("blocker_count")),
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
        log_event(
            logger,
            "sound.active_speaker_path_safety",
            level=logging.WARNING,
            action="current_config",
            result="error",
            error=current_config_error,
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
    log_event(
        logger,
        "sound.active_speaker_path_safety",
        action="check",
        status=str(report.get("status")),
        load_gate=str(report.get("load_gate")),
        path=target,
        blockers=int(report.get("blocker_count") or 0),
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
    log_event(
        logger,
        "sound.active_speaker_startup_load",
        action="load",
        status=str(payload.get("load", {}).get("status")),
        preflight=str(payload.get("preflight", {}).get("status")),
        rollback_available=str(bool(payload.get("load", {}).get("rollback_available"))),
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
    log_event(
        logger,
        "sound.active_speaker_startup_load",
        action="rollback",
        status=str(payload.get("rollback", {}).get("status")),
        active=str(payload.get("rollback", {}).get("active_config_path")),
    )
    return payload


# --- single-audio-path per-driver commissioning + Stage-5 ramp ----------------
#
# The browser surface over the guarded machinery the `jasper-active-speaker` CLI
# drives, shared with it through `jasper.active_speaker.commission_wiring`.
# Every loader uses the INLINE CamillaController seams (set_active_config_raw)
# so the persisted boot statefile is never repointed (crash-recovery-MUTED stays
# structural). A commission load arms a driver at the protected floor (silent);
# the Stage-5 ramp raises it one gated, operator-ACK'd step at a time. The GET
# state endpoint is read-only on purpose — the preflight emits the candidate
# YAML, so the load/step that run it are POST-only.


#: Operator stop reasons that mean "I heard it" — the only client-supplied
#: strings that complete a combined test. The loop's own budget end is NOT in
#: here: it passes ``completed=True`` directly, so a client cannot borrow the
#: machine's reason string to claim a completion it did not earn.
SUMMED_TEST_CONFIRM_STOP_REASONS = {"operator_confirmed"}
#: End reason for a play that ran the caller's whole ``duration_ms`` budget.
SUMMED_TEST_DURATION_ELAPSED_REASON = "duration_elapsed"
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
        log_event(
            logger,
            "sound.active_speaker_source_auto",
            level=logging.WARNING,
            action="restore",
            reason=reason,
            status="failed",
            error=exc,
        )
        return {
            "status": "failed",
            "reason": reason,
            "error": str(exc),
        }
    log_event(
        logger,
        "sound.active_speaker_source_auto",
        action="restore",
        reason=reason,
        status="ok",
        mode=str(payload.get("mode")),
        active_source=str(payload.get("active_source")),
        test_source=str(payload.get("test_source")),
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
        terminate_process(proc)
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
    log_event(
        logger,
        "sound.active_speaker_commission_tone",
        action="stop",
        reason=reason,
        status=str(payload.get("status")),
    )
    return payload


def _summed_test_session_stop_reason(session: dict[str, Any]) -> str | None:
    with _SUMMED_TEST_TONE_LOCK:
        if _SUMMED_TEST_TONE_SESSION is session:
            reason = _SUMMED_TEST_TONE_SESSION.get("stop_reason")
            return str(reason) if reason else None
    return None


# A session sits at ``process=None`` both between the looped ``aplay`` spawns
# and when it leaked before its owning request reached the try/finally teardown.
# A leaked session stays ``process=None`` forever with no owner to clear it, and
# would wedge every retry with ``summed_test_already_active`` until jasper-web
# restarted; a running loop refreshes ``progress_monotonic`` each iteration, so
# a stale heartbeat distinguishes the two.
#
# The window MUST exceed the longest a genuinely-live session can sit at
# process=None — the prepare phase before the first spawn: a ~15 s
# jasper-audio-hardware-reconcile wait (startup_load, manage_units timeout=15.0)
# plus the summed-config camilla WS ops. Shorter than a slow-but-live prepare, a
# concurrent start would misjudge it leaked and preempt it, racing on the fan-in
# lane, a second aplay and the config rollback. A hung (not merely slow) camilla
# is out of scope: the test cannot run then, and both starts block on the same
# dead WS.
SUMMED_TEST_SESSION_STALE_SECONDS = 90.0


def _summed_test_session_active(
    session: dict[str, Any] | None,
    *,
    now: float | None = None,
) -> bool:
    """Whether a combined-test session is genuinely live. Caller holds the lock.

    Live means: a session exists, no stop has been requested, and either the
    ``aplay`` child is alive *or* the loop refreshed its heartbeat within
    ``SUMMED_TEST_SESSION_STALE_SECONDS``. Start serialization uses
    ``_summed_test_session_occupies_resources`` instead, because a
    stopped-but-tearing-down session is no longer UI-active while it still owns
    the fan-in lane and transient Camilla graph.
    """

    if not session or session.get("stop_reason"):
        return False
    proc = session.get("process")
    if proc is not None:
        return proc.poll() is None
    if now is None:
        now = time.monotonic()
    heartbeat = session.get("progress_monotonic", session.get("started_monotonic"))
    try:
        heartbeat = float(heartbeat)
    except (TypeError, ValueError):
        return False
    return (now - heartbeat) < SUMMED_TEST_SESSION_STALE_SECONDS


def _summed_test_session_occupies_resources(
    session: dict[str, Any] | None,
    *,
    now: float | None = None,
) -> bool:
    """Whether a session must still serialize starts around owned resources.

    The UI-visible active state goes false as soon as Stop is requested, but the
    owning request still has to terminate ``aplay``, release the fan-in lane and
    roll back the transient Camilla graph. A new start must not reclaim the
    global until that teardown finishes.
    """

    if not session:
        return False
    proc = session.get("process")
    if proc is not None and proc.poll() is None:
        return True
    if now is None:
        now = time.monotonic()
    heartbeat = session.get("progress_monotonic", session.get("started_monotonic"))
    try:
        heartbeat = float(heartbeat)
    except (TypeError, ValueError):
        return False
    return (now - heartbeat) < SUMMED_TEST_SESSION_STALE_SECONDS


def _active_summed_test_snapshot() -> dict[str, Any]:
    """Live snapshot of the in-progress combined (summed) test, if any.

    The commissioning view is otherwise composed from *persisted* state and
    cannot see this in-memory playback session, so a reloaded ``/sound/`` page
    would offer "Play combined test" with no Stop while the test audio is still
    looping. Surfacing the live session lets any page load render a
    reload-safe Stop.
    """

    with _SUMMED_TEST_TONE_LOCK:
        session = _SUMMED_TEST_TONE_SESSION
        if session is None or not _summed_test_session_active(session):
            return {"active": False}
        return {
            "active": True,
            "playback_id": session.get("playback_id"),
            "speaker_group_id": session.get("speaker_group_id"),
            "level_dbfs": session.get("level_dbfs"),
        }


def _attach_active_summed_test(view: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Fold the live summed-test snapshot into the commissioning view.

    Attaches a top-level ``active_summed_test`` block and, when active, marks
    the matching ``combined_groups`` entry with ``summed_test_active`` so the
    client can render a reload-safe Stop per group.
    """

    if not isinstance(view, dict):
        return
    view["active_summed_test"] = snapshot
    if not snapshot.get("active"):
        return
    group_id = str(snapshot.get("speaker_group_id") or "")
    groups = view.get("combined_groups")
    if not isinstance(groups, list):
        return
    for group in groups:
        if not isinstance(group, dict):
            continue
        if not group_id or str(group.get("group_id") or "") == group_id:
            group["summed_test_active"] = True


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


def _summed_test_play_budget_seconds(value: Any) -> float | None:
    """Seconds of audible combined test the request asked for, or ``None``.

    ``None`` — an absent, unparseable, or non-positive ``duration_ms`` — is the
    open-ended loop: play until the operator stops it or the watchdog expires
    it. A budget is clamped to :data:`SUMMED_TEST_MAX_LOOP_SECONDS`, which
    stays the hard bound on how long this route can hold the single global
    combined-test lane.
    """

    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0.0:
        return None
    return min(seconds, SUMMED_TEST_MAX_LOOP_SECONDS)


def _summed_test_stopped_playback(
    playback: dict[str, Any],
    *,
    commissioning_load: dict[str, Any] | None = None,
    fanin_gate: dict[str, Any] | None = None,
    reason: str = "operator_stop",
    completed: bool = False,
) -> dict[str, Any]:
    """Shape one ended combined-test play.

    A play is ``completed`` — and therefore ``captured``-eligible in
    :func:`jasper.active_speaker.measurement.record_summed_test_artifact` — in
    exactly two cases: the operator confirmed hearing it, or the caller passes
    ``completed=True`` because the play ran its whole requested budget with at
    least one whole stimulus repeat played cleanly. Every other end (a plain
    stop, a stop before audio, the watchdog) leaves the record incomplete.
    """

    completed = completed or reason in SUMMED_TEST_CONFIRM_STOP_REASONS
    out = dict(playback)
    out.update(
        {
            "status": "completed" if completed else "stopped",
            "backend": SUMMED_COMMISSION_SPEECH_BACKEND,
            "audio_emitted": bool(completed),
            "confirmable": bool(completed),
            "stop_reason": reason,
            "issues": [
                issue for issue in playback.get("issues", []) if isinstance(issue, dict)
            ],
        }
    )
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
    session["progress_monotonic"] = time.monotonic()
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
        terminate_process(proc)
    return {
        "status": "stopped" if was_running else "expired",
        "reason": reason,
        "playback_id": session.get("playback_id"),
        "phase": "playing",
    }


def _active_speaker_stop_summed_test_tone(*, reason: str) -> dict[str, Any]:
    """End the combined test now; ``reason`` is a semantic flag, not a label.

    ``reason: "operator_confirmed"`` is the only client-supplied value that
    completes the test — it is what makes the recorded result ``captured`` and
    unlocks ``/active-speaker/summed-validation``. The default
    ``"operator_stop"``, any other string, and a stop that arrives before audio
    started all leave the test incomplete. A client that wants no rendezvous at
    all passes ``duration_ms`` to ``/active-speaker/summed-test`` instead.
    """

    with _SUMMED_TEST_TONE_LOCK:
        payload = _stop_summed_test_tone_locked(reason=reason)
    log_event(
        logger,
        "sound.active_speaker_summed_test",
        action="stop",
        reason=reason,
        status=str(payload.get("status")),
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
        load_payload.get("load") if isinstance(load_payload.get("load"), dict) else {}
    )
    loaded = load_state.get("status") == "loaded"
    status = "loaded" if loaded else "failed"
    if loaded:
        with _SUMMED_TEST_TONE_LOCK:
            if _SUMMED_TEST_TONE_SESSION is session:
                session["level_dbfs"] = level_dbfs
                session["load_payload"] = load_payload
    log_event(
        logger,
        "sound.active_speaker_summed_test",
        action="level",
        status=status,
        group_id=speaker_group_id,
        playback_id=playback_id,
        level_dbfs=str(level_dbfs),
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
        log_event(
            logger,
            "sound.active_speaker_commission_tone",
            level=logging.WARNING,
            action="plan",
            status="blocked",
            group=group_id,
            role=role,
            issues=",".join(
                (
                    str(issue.get("code"))
                    for issue in signal_plan.get("issues", [])
                    if isinstance(issue, dict)
                )
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
                issue
                for issue in signal_plan.get("issues", [])
                if isinstance(issue, dict)
            ],
            signal_plan=signal_plan,
        )
    target_key = _commission_tone_target_key(
        role=role, group_id=group_id, target=target
    )
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
                elapsed = time.monotonic() - float(
                    session.get("started_monotonic", 0.0)
                )
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

            proc = popen_correction_play(
                wav_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc.poll() is not None:
                raise RuntimeError(
                    f"aplay exited immediately with rc={proc.returncode}"
                )
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

    log_event(
        logger,
        "sound.active_speaker_commission_tone",
        action="start",
        group=group_id,
        role=role,
        frequency_hz=f"{frequency_hz:.1f}",
        duration_s=f"{COMMISSION_TONE_DURATION_S:.1f}",
        highpass_hz=str((signal_plan.get("allowed_band") or {}).get("highpass_hz")),
        lowpass_hz=str((signal_plan.get("allowed_band") or {}).get("lowpass_hz")),
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
    from jasper.active_speaker.commission_load import load_summed_commissioning_config

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
    from jasper.active_speaker.commission_load import rollback_driver_commissioning_config

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
    play_budget_s: float | None = None,
) -> dict[str, Any]:
    """Play one bounded combined-driver tone through the real active graph.

    ``play_budget_s`` is the caller's requested audible-play budget (the
    request's ``duration_ms``). With one, the looped speech stimulus stops
    itself once the budget has elapsed and this returns a *completed* play;
    without one, the loop plays until the operator stops it or the watchdog
    expires it. Whole repeats either way: the budget is checked between
    repeats, never mid-word, so a completed play is always at least one clean
    stimulus repeat and can overrun the budget by up to one.
    """

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
    reclaimed: tuple[str, Any] | None = None
    with _SUMMED_TEST_TONE_LOCK:
        prior = _SUMMED_TEST_TONE_SESSION
        if _summed_test_session_occupies_resources(prior):
            return _summed_playback_with_issue(
                artifact_playback,
                issue=_issue(
                    "summed_test_already_active",
                    "a combined speaker test is already running",
                ),
            )
        if prior is not None:
            # Overwriting a non-active prior session: either a leaked prepare or
            # a stopped owner whose teardown heartbeat aged past the stale
            # budget. Surfaced because a leaked prior flags a bug.
            reclaimed = (
                "stopped" if prior.get("stop_reason") else "stale",
                prior.get("playback_id"),
            )
        now_monotonic = time.monotonic()
        session: dict[str, Any] = {
            "playback_id": playback_id,
            "process": None,
            "speaker_group_id": speaker_group_id,
            "level_dbfs": None,
            "started_monotonic": now_monotonic,
            "progress_monotonic": now_monotonic,
            "stop_reason": None,
        }
        _SUMMED_TEST_TONE_SESSION = session
    if reclaimed is not None:
        log_event(
            logger,
            "sound.active_speaker_summed_test",
            action="reclaim_prior_session",
            reason=reclaimed[0],
            prior_playback_id=reclaimed[1],
        )

    tone = (
        artifact_playback.get("tone")
        if isinstance(artifact_playback.get("tone"), dict)
        else {}
    )
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
        load_payload.get("load") if isinstance(load_payload.get("load"), dict) else {}
    )
    if load_state.get("status") != "loaded":
        with _SUMMED_TEST_TONE_LOCK:
            if _SUMMED_TEST_TONE_SESSION is session:
                _SUMMED_TEST_TONE_SESSION = None
        load_issues = [
            issue for issue in load_state.get("issues", []) if isinstance(issue, dict)
        ]
        issue = (
            load_issues[0]
            if load_issues
            else summed_commission_load_failed_issue()
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
            def _ended(reason: str, *, completed: bool = False) -> dict[str, Any]:
                """End the loop at the session's live level, with the reason."""

                current_playback = _summed_test_playback_at_session_level(
                    artifact_playback,
                    session,
                )
                current_playback.update(
                    {
                        "audio_device": {"pcm": correction_play_device()},
                        "stimulus": stimulus,
                    }
                )
                return _summed_test_stopped_playback(
                    current_playback,
                    commissioning_load=current_playback.get(
                        "commissioning_load", load_payload
                    ),
                    fanin_gate=fanin_gate,
                    reason=reason,
                    completed=completed,
                )

            heard_audio = False
            loop_count = 0
            watchdog_deadline = time.monotonic() + SUMMED_TEST_MAX_LOOP_SECONDS
            play_deadline = (
                None
                if play_budget_s is None
                else time.monotonic() + play_budget_s
            )
            while True:
                stop_reason = _summed_test_session_stop_reason(session)
                if stop_reason:
                    playback_result = _ended(
                        stop_reason
                        if heard_audio
                        else (
                            "operator_stop_before_audio"
                            if stop_reason in SUMMED_TEST_CONFIRM_STOP_REASONS
                            else "operator_stop"
                        )
                    )
                    break
                if time.monotonic() >= watchdog_deadline:
                    playback_result = _ended("watchdog_timeout")
                    break
                if (
                    play_deadline is not None
                    and loop_count >= 1
                    and time.monotonic() >= play_deadline
                ):
                    # `loop_count` counts stimulus repeats that reached aplay
                    # exit 0, so requiring >= 1 means a budget shorter than one
                    # repeat still buys real audio before this claims completion.
                    playback_result = _ended(
                        SUMMED_TEST_DURATION_ELAPSED_REASON,
                        completed=True,
                    )
                    break
                started_proc = popen_correction_play(
                    wav_path,
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
                        terminate_process(started_proc)
                        break
                    if now >= deadline:
                        terminate_process(started_proc)
                        raise TimeoutError(
                            "aplay timed out during combined speaker test"
                        )
                    await asyncio.sleep(0.03)
                if watchdog_expired:
                    playback_result = _ended("watchdog_timeout")
                    break
                stop_reason = _summed_test_session_stop_reason(session)
                if stop_reason:
                    playback_result = _ended(stop_reason)
                    break
                if started_proc.returncode != 0:
                    raise RuntimeError(f"aplay exited {started_proc.returncode}")
                loop_count += 1
                with _SUMMED_TEST_TONE_LOCK:
                    if _SUMMED_TEST_TONE_SESSION is session:
                        session["process"] = None
                        session["progress_monotonic"] = time.monotonic()
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
                now_monotonic = time.monotonic()
                session["teardown_started_monotonic"] = now_monotonic
                session["progress_monotonic"] = now_monotonic
        try:
            if started_proc is not None and started_proc.poll() is None:
                terminate_process(started_proc)
            if fanin_gate is not None:
                _commission_tone_release_fanin_lane(reason="summed_test")
            rollback, rollback_issue = await rollback_summed_commission_teardown(
                lambda: _active_speaker_rollback_summed_commissioning_config(
                    camilla_factory=camilla_factory,
                ),
                log_event_name="sound.active_speaker_summed_test",
            )
        finally:
            with _SUMMED_TEST_TONE_LOCK:
                if _SUMMED_TEST_TONE_SESSION is session:
                    session["teardown_completed_monotonic"] = time.monotonic()
                    _SUMMED_TEST_TONE_SESSION = None
    if rollback is not None:
        playback_result["rollback"] = rollback
    if rollback_issue is not None:
        playback_result["status"] = "failed"
        playback_result["confirmable"] = False
        playback_result["issues"] = [
            *(
                playback_result.get("issues")
                if isinstance(playback_result.get("issues"), list)
                else []
            ),
            rollback_issue,
        ]
    return playback_result


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
    if same_config_file(current_config_path, staged_path) and staged_matches:
        return {"status": "already_loaded", "staged_config_path": staged_path}
    if same_config_file(current_config_path, staged_path):
        log_event(
            logger,
            "sound.active_speaker_commission",
            action="startup_anchor",
            group=group,
            role=role,
            status="refresh_required",
            reason="staged_topology_mismatch",
        )

    preview = _active_speaker_crossover_preview_save_payload()
    stage = _active_speaker_stage_config_payload({})
    if stage.get("status") != "staged":
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=commission_startup_anchor_not_staged_issue(),
            startup_setup={"status": "blocked", "preview": preview, "stage": stage},
        )

    path_payload = await _active_speaker_check_path_safety_payload(
        camilla_factory=camilla_factory,
        require_physical_identity=require_physical_identity,
    )
    path_report = path_payload.get("report") if isinstance(path_payload, dict) else {}
    if not isinstance(path_report, dict) or path_report.get("load_gate") != "ready":
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=commission_startup_anchor_path_safety_blocked_issue(),
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
        startup_load.get("load") if isinstance(startup_load.get("load"), dict) else {}
    )
    if load_state.get("status") != "loaded" or not load_state.get("rollback_available"):
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=commission_startup_anchor_load_failed_issue(),
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


def _active_speaker_identity_audition_granted(
    topology: OutputTopology,
    *,
    group: str,
    role: str,
    requested: bool,
) -> bool:
    """Decide server-side whether the weaker identity-audition mode applies.

    A client may only REQUEST it; the saved topology decides. The audition
    plays one assigned lane while the household is still working out which
    physical driver is which, so it is granted while that confirmation is
    outstanding anywhere in the topology (a replay of an already-confirmed lane
    mid-sequence included). Once every assigned lane is confirmed the strict
    gate stands on its own and no client can lower it. See #2821.
    """

    if not requested:
        return False
    group_id = str(group or "").strip()
    role_name = str(role or "").strip().lower()
    target = next(
        (
            channel
            for speaker_group in topology.speaker_groups
            if speaker_group.id == group_id
            for channel in speaker_group.channels
            if channel.role == role_name
        ),
        None,
    )
    if target is None:
        reason = "unknown_target"
    elif target.physical_output_index is None:
        reason = "unassigned_lane"
    elif not channel_identity_report(topology)["unverified_channel_count"]:
        reason = "already_confirmed"
    else:
        return True
    log_event(
        logger,
        "sound.active_speaker_commission",
        action="identity_audition",
        result="refused",
        reason=reason,
        group=group_id,
        role=role_name,
    )
    return False


async def _active_speaker_commission_load_payload(
    raw: dict[str, Any],
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Arm a driver: load its per-driver commissioning config into the RUNNING
    graph at the protected floor (silent). Operator-only, single-flight."""

    from jasper.active_speaker.staging import load_staged_startup_config
    from jasper.active_speaker.commission_load import (
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
        log_event(
            logger,
            "sound.active_speaker_commission",
            action="load",
            result="refused",
            reason="measurement_in_progress",
            group=group,
            role=role,
            blocking=blocking,
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
    # (e.g. a compression-driver tweeter) to carry its software-guard request,
    # and a stale topology can drift to required_missing and block forever. The
    # high-pass itself is still enforced by the protection-while-audible gate.
    topology, guards_changed = ensure_missing_software_guards()
    if guards_changed:
        log_event(
            logger,
            "sound.active_speaker_commission",
            action="request_software_guards",
            group=group,
            role=role,
        )
    require_physical_identity = not _active_speaker_identity_audition_granted(
        topology,
        group=group,
        role=role,
        requested=bool(raw.get("identity_audition")),
    )
    staged = load_staged_startup_config()
    current_config_path, current_config_error = await read_current_config_path(cam)
    startup_setup = await _active_speaker_ensure_commission_startup_anchor(
        group=group,
        role=role,
        staged_config=staged,
        current_config_path=current_config_path,
        camilla_factory=camilla_factory,
        require_physical_identity=require_physical_identity,
    )
    if startup_setup.get("status") == "blocked":
        log_event(
            logger,
            "sound.active_speaker_commission",
            action="startup_anchor",
            group=group,
            role=role,
            status="blocked",
        )
        return startup_setup

    staged = load_staged_startup_config()
    preset, crossover_preview = resolve_commission_inputs()
    current_config_path, current_config_error = await read_current_config_path(cam)
    evidence_path = write_commission_path_safety(
        topology,
        staged,
        current_config_path,
        current_config_error,
        require_physical_identity=require_physical_identity,
    )
    load_config, read_running_config, get_current_config_path = commission_seams(cam)
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
    log_event(
        logger,
        "sound.active_speaker_commission",
        action="load",
        group=group,
        role=role,
        status=str((payload.get("load") or {}).get("status")),
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

    from jasper.active_speaker.commission_ramp import clear_pending_ramp_step
    from jasper.active_speaker.safe_playback import stop_safe_playback_session
    from jasper.active_speaker.commission_load import rollback_driver_commissioning_config

    tone_stop = _active_speaker_stop_commission_tone(reason="commission_rollback")
    cam = camilla_factory()
    load_config, _, _ = commission_seams(cam)
    payload = await rollback_driver_commissioning_config(load_config=load_config)
    if (payload.get("rollback") or {}).get("status") == "rolled_back":
        # The graph is proven back on the all-muted anchor, so the step the ramp
        # was waiting on is gone with it. Only a proven rollback clears it: a
        # blocked / failed one may still be audible.
        payload["ramp"] = clear_pending_ramp_step()
    payload["safe_playback"] = stop_safe_playback_session(reason="commission_rollback")
    payload["tone_stop"] = tone_stop
    log_event(
        logger,
        "sound.active_speaker_commission",
        action="rollback",
        status=str((payload.get("rollback") or {}).get("status")),
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
    topology = load_output_topology()
    require_physical_identity = not _active_speaker_identity_audition_granted(
        topology,
        group=group,
        role=role,
        requested=identity_audition,
    )
    staged = load_staged_startup_config()
    preset, crossover_preview = resolve_commission_inputs()
    cam = camilla_factory()
    current_config_path, current_config_error = await read_current_config_path(cam)
    evidence_path = write_commission_path_safety(
        topology,
        staged,
        current_config_path,
        current_config_error,
        require_physical_identity=require_physical_identity,
    )
    load_config, read_running_config, get_current_config_path = commission_seams(cam)

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
    log_event(
        logger,
        "sound.active_speaker_commission",
        action="ramp_step",
        group=group,
        role=role,
        status=str(payload.get("status")),
        next_db=str(payload.get("next_gain_db")),
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
    confirm_output_identity = bool(raw.get("confirm_output_identity"))
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
    topology_for_measurement = topology
    identity_promoted = False
    if (
        confirm_output_identity
        and outcome == "heard_correct_driver"
        and payload.get("status") == "confirmed"
        and not payload.get("issues")
        and isinstance(acknowledged_step, dict)
    ):
        group_id = str(ramp_state.get("speaker_group_id") or "").strip()
        role = str(acknowledged_step.get("role") or "").strip().lower()
        try:
            with output_topology_mutation() as mutation:
                topology_for_measurement = set_channel_identity_verified(
                    mutation.snapshot().topology,
                    speaker_group_id=group_id,
                    role=role,
                    identity_verified=True,
                )
                mutation.save(topology_for_measurement)
            identity_promoted = True
            payload.update(_output_topology_payload())
        except (OSError, ValueError) as exc:
            payload["status"] = "failed"
            payload["reason"] = "driver_target_identity_save_failed"
            payload["issues"] = [
                *(
                    payload.get("issues")
                    if isinstance(payload.get("issues"), list)
                    else []
                ),
                _issue(
                    "driver_target_identity_save_failed",
                    (
                        "the driver was heard, but JTS could not save the "
                        "output confirmation"
                    ),
                ),
            ]
            log_event(
                logger,
                "sound.active_speaker_commission",
                level=logging.WARNING,
                action="promote_identity",
                status="failed",
                group=group_id,
                role=role,
                error=exc,
            )
    if should_record_driver_evidence and isinstance(acknowledged_step, dict):
        if not confirm_output_identity or identity_promoted:
            measurements = record_driver_measurement(
                topology_for_measurement,
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
    log_event(
        logger,
        "sound.active_speaker_commission",
        action="ramp_ack",
        outcome=outcome,
        status=str(payload.get("status")),
        identity_promoted=str(identity_promoted),
        measurement_status=str((payload.get("measurements") or {}).get("status")),
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
    log_event(
        logger,
        "sound.active_speaker_commission",
        action="ramp_abort",
        status=str(payload.get("status")),
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
    from jasper.active_speaker.commission_load import (
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

    State-loading and composition live in the shared
    ``commissioning_coordinator.load_commissioning_view``, which the crossover
    envelope consumes too. Only the ``commission`` runtime relay is built here,
    because it needs the async CamillaDSP runtime probe this caller owns.
    """

    from jasper.active_speaker.commissioning_coordinator import (
        load_commissioning_view,
    )

    commission = await _active_speaker_commission_state_payload(
        camilla_factory=camilla_factory,
    )
    view = load_commissioning_view(commission=commission)
    active_summed_test = _active_summed_test_snapshot()
    _attach_active_summed_test(view, active_summed_test)
    log_event(
        logger,
        "sound.active_speaker_commissioning_view",
        status=str(view.get("status")),
        next_action=str((view.get("next_action") or {}).get("id")),
        summed_test_active=str(active_summed_test.get("active")),
    )
    return view


def _active_speaker_measurements_payload() -> dict[str, Any]:
    """Return active-speaker measurement evidence for the saved topology."""

    from jasper.active_speaker.measurement import load_measurement_state

    topology = load_output_topology()
    payload = load_measurement_state(topology)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    log_event(
        logger,
        "sound.active_speaker_measurements",
        status=str(payload.get("status")),
        drivers="%s/%s"
        % (summary.get("captured_driver_count"), summary.get("required_driver_count")),
        summed="%s/%s"
        % (
            summary.get("validated_summed_group_count"),
            summary.get("required_summed_group_count"),
        ),
    )
    return payload


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
    log_event(
        logger,
        "sound.active_speaker_driver_measurement",
        status=str(payload.get("status")),
        group_id=str(raw.get("speaker_group_id")),
        role=str(raw.get("role")),
        outcome=str(raw.get("outcome")),
        captured=str(bool(
                (summary.get("latest_driver_measurements") or {})
                .get(f"{raw.get('speaker_group_id')}:{raw.get('role')}", {})
                .get("captured")
            )
            if isinstance(summary.get("latest_driver_measurements"), dict)
        else False),
        drivers="%s/%s"
        % (summary.get("captured_driver_count"), summary.get("required_driver_count")),
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
            group.get("crossovers") if isinstance(group.get("crossovers"), list) else []
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
    """Run and record one bounded combined-driver test for validation.

    This request blocks for as long as the test plays. ``duration_ms`` is how
    long to play: with it the audible loop stops itself, the response is a
    ``completed`` play and the recorded test is ``captured``, so one client can
    run the test and then POST ``/active-speaker/summed-validation`` in
    sequence with no second connection and no race against
    ``active_summed_test_running``. Without ``duration_ms`` the loop runs until
    ``/active-speaker/summed-test/stop`` arrives on another connection
    (``reason: "operator_confirmed"`` to complete it) or the watchdog expires
    it; a stop that is not a confirmation leaves the test incomplete.
    """

    from jasper.active_speaker.calibration_level import (
        calibration_level_payload,
        load_calibration_level_state,
    )
    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.commissioning_coordinator import (
        build_commissioning_view,
        read_applied_profile_verdict,
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
    play_budget_s = _summed_test_play_budget_seconds(raw.get("duration_ms"))
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
        playback_allowed=(
            wants_audio and safe_session.get("status") == "armed" and protected_loaded
        ),
        safe_session_id=safe_session.get("session_id"),
        protected_startup_loaded=protected_loaded,
    )
    baseline_profile = build_baseline_profile_candidate(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
    )
    commissioning_view = build_commissioning_view(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        baseline_profile=baseline_profile,
        calibration_level=calibration_level,
        applied_profile_verdict=read_applied_profile_verdict(baseline_profile),
    )
    driver_target_proof = commissioning_view.get("driver_target_proof")
    driver_target_proof_complete = (
        isinstance(driver_target_proof, dict)
        and driver_target_proof.get("complete") is True
    )
    if not driver_target_proof_complete:
        plan = _active_speaker_plan_with_issues(
            plan,
            [
                {
                    "severity": "blocker",
                    "code": "summed_test_driver_target_proof_missing",
                    "message": (
                        "confirm each output and driver before running the "
                        "combined test"
                    ),
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
            play_budget_s=play_budget_s,
        )
    else:
        playback = start_tone_playback(
            plan,
            safe_session=safe_session,
            backend=None,
            allow_audio=False,
        )
    playback_tone = (
        playback.get("tone") if isinstance(playback.get("tone"), dict) else {}
    )
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
    log_event(
        logger,
        "sound.active_speaker_summed_test",
        status=str(playback.get("status")),
        group_id=speaker_group_id,
        level_dbfs=str(playback.get("tone", {}).get("level_dbfs")),
        requested_level_dbfs=str(requested_level),
        audio_requested=str(wants_audio),
        audio_emitted=str(bool(playback.get("audio_emitted"))),
        play_budget_s=str(play_budget_s),
        stop_reason=str(playback.get("stop_reason")),
        blockers=len(playback.get("issues") or []),
        artifact=str((playback.get("artifact") or {}).get("wav_basename")),
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
    from jasper.active_speaker.commissioning_coordinator import load_commissioning_view
    from jasper.active_speaker.measurement import record_summed_validation

    if not isinstance(raw, dict):
        raise ValueError("summed validation request must be an object")
    topology = load_output_topology()
    commissioning_view = load_commissioning_view(topology)
    driver_target_proof = (
        commissioning_view.get("driver_target_proof")
        if isinstance(commissioning_view.get("driver_target_proof"), dict)
        else {}
    )
    payload = record_summed_validation(
        topology,
        raw,
        calibration_level=load_calibration_level_state(),
        driver_target_proof_complete=driver_target_proof.get("complete") is True,
    )
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    log_event(
        logger,
        "sound.active_speaker_summed_validation",
        status=str(payload.get("status")),
        group_id=str(raw.get("speaker_group_id")),
        outcome=str(raw.get("outcome")),
        validated=str(bool(
                (summary.get("latest_summed_validations") or {})
                .get(str(raw.get("speaker_group_id") or ""), {})
                .get("validated")
            )
            if isinstance(summary.get("latest_summed_validations"), dict)
        else False),
        summed="%s/%s"
        % (
            summary.get("validated_summed_group_count"),
            summary.get("required_summed_group_count"),
        ),
    )
    return payload


def _active_speaker_summed_validation_active_conflict(
    raw: dict[str, Any],
) -> dict[str, Any] | None:
    """Reject validation writes while a combined test is still in progress."""

    active = _active_summed_test_snapshot()
    if active.get("active") is not True:
        return None
    return {
        "status": "active_summed_test_running",
        "reason": "active_summed_test_running",
        "error": "stop the combined speaker test before recording the check",
        "speaker_group_id": str(raw.get("speaker_group_id") or "").strip(),
        "active_summed_test": active,
    }


def _active_speaker_baseline_profile_payload(
    *,
    write: bool = False,
    design_draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return or compile the active-speaker baseline profile candidate.

    ``design_draft`` lets a caller that has already read the draft hand it in
    rather than pay for a second read of the same file.
    """

    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state

    topology = load_output_topology()
    if design_draft is None:
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
    log_event(
        logger,
        "sound.active_speaker_baseline_profile",
        action="compile" if write else "status",
        status=str(payload.get("status")),
        may_apply=str(bool((payload.get("permissions") or {}).get("may_apply"))),
        issue_count=len(payload.get("issues") or []),
        config=str((payload.get("config") or {}).get("basename")),
    )
    return payload


async def _active_speaker_baseline_profile_apply_payload(
    *,
    expected_candidate_fingerprint: str,
    on_candidate_verified: Callable[[], Awaitable[None]] | None = None,
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

    def refresh_inputs():
        current_topology = load_output_topology()
        current_draft = load_design_draft()
        current_preview = load_crossover_preview(current_design_draft=current_draft)
        return (
            current_topology,
            current_draft,
            current_preview,
            load_measurement_state(current_topology),
        )

    cam = camilla_factory()
    payload = await apply_baseline_profile(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
        expected_candidate_fingerprint=expected_candidate_fingerprint,
        on_candidate_verified=on_candidate_verified,
        refresh_inputs=refresh_inputs,
    )
    if payload.get("status") == "applied":
        payload["source_selection_restore"] = _active_speaker_restore_auto_source(
            reason="baseline_apply",
        )
    log_event(
        logger,
        "sound.active_speaker_baseline_profile",
        action="apply",
        status=str(payload.get("status")),
        apply_result=str((payload.get("apply") or {}).get("result")),
        issue_count=len(payload.get("issues") or []),
        source_restore=str((payload.get("source_selection_restore") or {}).get("status")),
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
    expected_candidate_fingerprint: str,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Backend-owned final handoff from commissioning to the active profile.

    The browser expresses one intent — make the checked crossover the normal
    active speaker profile — and the backend owns the whole
    compile/validate/load/confirm sequence, so the UI cannot wedge itself
    between "saved" and "applied".
    """

    reviewed = _active_speaker_baseline_profile_payload(write=False)
    if (
        not expected_candidate_fingerprint
        or str(reviewed.get("candidate_fingerprint") or "")
        != expected_candidate_fingerprint
    ):
        issue = {
            "severity": "blocker",
            "code": "baseline_candidate_fingerprint_mismatch",
            "message": (
                "the crossover candidate changed after review; refresh and "
                "review the current candidate before applying"
            ),
        }
        reviewed = dict(reviewed)
        reviewed["permissions"] = dict(reviewed.get("permissions") or {})
        reviewed["permissions"]["may_apply"] = False
        reviewed["issues"] = [*reviewed.get("issues", []), issue]
        return {
            "status": "blocked",
            "profile": reviewed,
            "apply": None,
            "issues": reviewed["issues"],
            "commissioning_cleanup": {"status": "not_attempted"},
        }

    commissioning_cleanup: dict[str, Any] = {"status": "not_attempted"}

    async def cleanup_after_locked_proof() -> None:
        nonlocal commissioning_cleanup
        summed_stop = _active_speaker_stop_summed_test_tone(
            reason="finish_commissioning"
        )
        try:
            from jasper.active_speaker.commission_ramp import load_ramp_state
            from jasper.active_speaker.commission_load import load_commission_load_state

            ramp_state = load_ramp_state()
            commission_load = load_commission_load_state()
            cleanup_needed = isinstance(ramp_state.get("pending"), dict) or (
                commission_load.get("status") == "loaded"
            )
            if cleanup_needed:
                ramp_cleanup = await _active_speaker_commission_ramp_abort_payload(
                    camilla_factory=camilla_factory,
                )
            else:
                ramp_cleanup = {
                    "status": "idle",
                    "ramp": ramp_state,
                    "commission_load": commission_load,
                }
        except (OSError, RuntimeError, ValueError) as exc:
            ramp_cleanup = {"status": "error", "error": str(exc)}
        commissioning_cleanup = {
            "summed_test": summed_stop,
            "ramp": ramp_cleanup,
        }

    payload = await _active_speaker_baseline_profile_apply_payload(
        expected_candidate_fingerprint=expected_candidate_fingerprint,
        on_candidate_verified=cleanup_after_locked_proof,
        camilla_factory=camilla_factory,
    )
    payload["commissioning_cleanup"] = commissioning_cleanup
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    apply_state = payload.get("apply") if isinstance(payload.get("apply"), dict) else {}
    config = profile.get("config") if isinstance(profile.get("config"), dict) else {}
    active_config_path = apply_state.get("active_config_path") or config.get("path")
    payload["output_safety"] = _active_speaker_output_safety_from_config_path(
        active_config_path
        if isinstance(active_config_path, (str, os.PathLike))
        else None
    )
    log_event(
        logger,
        "sound.active_speaker_finish_commissioning",
        status=str(payload.get("status")),
        apply_result=str((payload.get("apply") or {}).get("result")
            if isinstance(payload.get("apply"), dict)
        else None),
        safety_muted=str((payload.get("output_safety") or {}).get("safety_muted")),
        issue_count=len(payload.get("issues") or []),
    )
    return payload


#: Read-only GET routes whose entire handler is "send this payload, or
#: answer 502 under this event name", dispatched once in :func:`_make_handler`;
#: a route that needs the handler's own state (the two commissioning views
#: close over ``camilla_factory``) stays spelled out there. The log-event drift
#: pin reads the event strings here. The builder is NAMED, not captured: a
#: table holding the objects it had at import time would answer with a builder
#: the module no longer has.
_GET_JSON_ROUTES: dict[str, tuple[str, str]] = {
    "/output-topology": ("_output_topology_payload", "sound.output_topology"),
    "/active-speaker/design-draft": (
        "_active_speaker_design_draft_payload",
        "sound.active_speaker_design_draft",
    ),
    "/active-speaker/crossover-preview": (
        "_active_speaker_crossover_preview_payload",
        "sound.active_speaker_crossover_preview",
    ),
    "/active-speaker/measurements": (
        "_active_speaker_measurements_payload",
        "sound.active_speaker_measurements",
    ),
    "/active-speaker/baseline-profile": (
        "_active_speaker_baseline_profile_payload",
        "sound.active_speaker_baseline_profile",
    ),
    "/active-speaker/tuning-handoff": (
        "_active_speaker_tuning_handoff_payload",
        "sound.active_speaker_tuning_handoff",
    ),
    "/active-speaker/environment": (
        "_active_speaker_environment_payload",
        "sound.active_speaker_environment",
    ),
    "/active-speaker/safe-playback": (
        "_active_speaker_safe_playback_payload",
        "sound.active_speaker_safe_playback",
    ),
    "/active-speaker/calibration-level": (
        "_active_speaker_calibration_level_payload",
        "sound.active_speaker_calibration_level",
    ),
    "/active-speaker/bringup-preflight": (
        "_active_speaker_bringup_preflight_payload",
        "sound.active_speaker_bringup_preflight",
    ),
    "/active-speaker/startup-load": (
        "_active_speaker_startup_load_payload",
        "sound.active_speaker_startup_load",
    ),
    "/active-speaker/staged-config": (
        "_active_speaker_staged_config_payload",
        "sound.active_speaker_staged_config",
    ),
    "/active-speaker/channel-identity": (
        "_active_speaker_channel_identity_payload",
        "sound.active_speaker_channel_identity",
    ),
}


def _json_route_payload(builder: str) -> dict[str, Any]:
    """Call one :data:`_GET_JSON_ROUTES` builder, resolved at call time."""
    fn: Callable[[], dict[str, Any]] = getattr(sys.modules[__name__], builder)
    return fn()


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
            self._json_response_started = True
            send_json_response(self, payload, status=status)

        def _read_json(self, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
            return read_json_object(self, max_bytes=max_bytes)

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/",
                "/state",
                "/output-topology",
                "/active-speaker/design-draft",
                "/active-speaker/crossover-preview",
                "/active-speaker/measurements",
                "/active-speaker/baseline-profile",
                "/active-speaker/tuning-handoff",
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
                self._send_html(
                    _index_html(
                        ctx["csrf_token"],
                        page_mode=self.headers.get("X-JTS-Sound-Page", "eq"),
                    )
                )
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
            json_route = _GET_JSON_ROUTES.get(path)
            if json_route is not None:
                builder, event = json_route
                try:
                    self._send_json(_json_route_payload(builder))
                except Exception as e:  # noqa: BLE001
                    send_route_failure(
                        self._send_json, e, logger=logger, event=event,
                    )
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
                    send_route_failure(
                        self._send_json, e, logger=logger,
                        event="sound.active_speaker_commission",
                    )
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
                    send_route_failure(
                        self._send_json, e, logger=logger,
                        event="sound.active_speaker_commissioning_view",
                    )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            self._json_response_started = False
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
                "/active-speaker/driver-research-request",
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
                "/output-topology/repin",
                "/profiles/save",
                "/profiles/rename",
                "/profiles/delete",
                "/i2s-hat",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            if path in _FOLLOWER_BLOCKED_CONTENT_DSP_POSTS and bonded_follower_active():
                log_event(
                    logger,
                    "sound.follower_content_dsp_blocked",
                    path=path,
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
                if path == "/i2s-hat":
                    profile_id = raw.get("profile_id")
                    if profile_id is not None and not isinstance(profile_id, str):
                        self._send_json(
                            {"error": "profile_id must be a string or null"},
                            status=400,
                        )
                        return
                    try:
                        payload, result = _save_i2s_hat_payload(profile_id)
                    except ValueError as e:
                        self._send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
                        return
                    except (OSError, RuntimeError) as e:
                        self._send_json({"error": str(e)}, status=502)
                        return
                    if not result.get("ok"):
                        error = result.get("error") or result.get("stderr")
                        payload["error"] = str(error or "hardware apply failed")
                    self._send_json(payload, status=200 if result.get("ok") else 502)
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
                    except (OSError, RuntimeError) as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_channel_identity",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/channel-protection":
                    try:
                        self._send_json(
                            _active_speaker_channel_protection_save_payload(raw)
                        )
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_channel_protection",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/stage-config":
                    self._send_json(_active_speaker_stage_config_payload(raw))
                    return
                if path == "/active-speaker/design-draft":
                    from jasper.active_speaker.design_draft import (
                        ActiveSpeakerDesignDraftRevisionConflict,
                    )

                    try:
                        self._send_json(_active_speaker_design_draft_save_payload(raw))
                    except ActiveSpeakerDesignDraftRevisionConflict as e:
                        payload = _active_speaker_design_draft_payload()
                        payload["error"] = str(e)
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_design_draft_save",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/driver-research-request":
                    self._send_json(
                        _active_speaker_driver_research_request_payload(raw)
                    )
                    return
                if path == "/active-speaker/crossover-preview":
                    try:
                        self._send_json(
                            _active_speaker_crossover_preview_save_payload()
                        )
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_crossover_preview_save",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/driver-measurement":
                    try:
                        self._send_json(_active_speaker_driver_measurement_payload(raw))
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_driver_measurement",
                            error=type(e).__name__,
                        )
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
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_summed_test",
                            error=type(e).__name__,
                        )
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
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_summed_test_level",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/summed-test/stop":
                    reason = str(raw.get("reason") or "operator_stop")
                    self._send_json(
                        _active_speaker_stop_summed_test_tone(reason=reason)
                    )
                    return
                if path == "/active-speaker/summed-validation":
                    try:
                        conflict = _active_speaker_summed_validation_active_conflict(
                            raw
                        )
                        if conflict is not None:
                            log_event(
                                logger,
                                "sound.active_speaker_summed_validation",
                                status="blocked",
                                reason="active_summed_test_running",
                                group_id=str(conflict.get("speaker_group_id")),
                                active_playback_id=str((
                                        conflict.get("active_summed_test", {})
                                        if isinstance(
                                            conflict.get("active_summed_test"), dict
                                        )
                                        else {}
                                ).get("playback_id")),
                            )
                            self._send_json(conflict, status=HTTPStatus.CONFLICT)
                            return
                        self._send_json(_active_speaker_summed_validation_payload(raw))
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_summed_validation",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/baseline-profile":
                    try:
                        self._send_json(
                            _active_speaker_baseline_profile_payload(write=True)
                        )
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_baseline_profile",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/baseline-profile/apply":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_baseline_profile_apply_payload(
                                expected_candidate_fingerprint=str(
                                    raw.get("expected_candidate_fingerprint") or ""
                                ),
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/baseline-profile/save-and-apply":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_finish_commissioning_payload(
                                expected_candidate_fingerprint=str(
                                    raw.get("expected_candidate_fingerprint") or ""
                                ),
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
                        log_event(
                            logger,
                            "sound.output_topology_save",
                            level=logging.WARNING,
                            result="conflict",
                            error=type(e).__name__,
                        )
                        payload = _output_topology_payload()
                        payload["error"] = str(e)
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except (OSError, RuntimeError) as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.output_topology_save",
                            error=type(e).__name__,
                        )
                    return
                if path == "/output-topology/reset":
                    try:
                        self._send_json(_reset_output_topology_payload(raw))
                    except OutputHardwareRequestConflict as e:
                        payload = _output_topology_payload()
                        payload["error"] = str(e)
                        payload["conflict"] = e.code
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except ValueError as e:
                        self._send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
                    except (OSError, RuntimeError) as e:
                        log_event(
                            logger,
                            "sound.output_topology_reset",
                            level=logging.ERROR,
                            exc_info=True,
                            result="error",
                            error=type(e).__name__,
                        )
                        message = (
                            "JTS could not confirm whether speaker setup was reset. "
                            "Review the current setup and try again."
                        )
                        try:
                            payload = _output_topology_payload()
                        except (OSError, RuntimeError, ValueError):
                            payload = {}
                        payload["error"] = message
                        payload["reset"] = {
                            "status": "needs_attention",
                            "message": message,
                        }
                        self._send_json(payload, status=502)
                    return
                if path == "/output-topology/repin":
                    try:
                        self._send_json(_repin_output_topology_payload(raw))
                    except OutputHardwareRequestConflict as e:
                        payload = _output_topology_payload()
                        payload["error"] = str(e)
                        payload["conflict"] = e.code
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except ValueError as e:
                        self._send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
                    except (OSError, RuntimeError) as e:
                        log_event(
                            logger,
                            "sound.output_topology_repin",
                            level=logging.ERROR,
                            exc_info=True,
                            result="error",
                            error=type(e).__name__,
                        )
                        message = (
                            "JTS could not confirm whether the new DAC was pinned. "
                            "Review the current setup and try again."
                        )
                        try:
                            payload = _output_topology_payload()
                        except (OSError, RuntimeError, ValueError):
                            payload = {}
                        payload["error"] = message
                        payload["repin"] = {
                            "status": "needs_attention",
                            "message": message,
                        }
                        self._send_json(payload, status=502)
                    return
                if path == "/settings":
                    try:
                        payload = asyncio.run(
                            _apply_settings(
                                raw,
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
                                VOLUME_FLOOR_TONE_SESSION.start_or_update(
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
                                VOLUME_FLOOR_TONE_SESSION.stop(
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
                            log_event(
                                logger,
                                "sound.profile_library",
                                action=action,
                                profile_id=entry.id,
                                curve=entry.profile.curve_id,
                                bands=len(entry.profile.parametric_bands),
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
                            log_event(
                                logger,
                                "sound.profile_library",
                                action="rename",
                                profile_id=entry.id,
                                curve=entry.profile.curve_id,
                                bands=len(entry.profile.parametric_bands),
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
                            log_event(
                                logger,
                                "sound.profile_library",
                                action="delete",
                                profile_id=deleted_id,
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
            except (JsonBodyError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
                self._send_json({"error": str(e)}, status=400)
                return
            except (OSError, RuntimeError) as e:
                if self._json_response_started:
                    raise
                log_event(
                    logger,
                    "sound.post_dispatch_failed",
                    path=path,
                    error_type=type(e).__name__,
                    level=logging.ERROR,
                    exc_info=True,
                )
                self._send_json({"error": str(e)}, status=502)
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
                                config_dir=config_dir,
                                profile_path=profile_path,
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
                    # The loaded graph cannot host EQ: a known, handled state,
                    # not a server error. 200 with a typed body, NOT the 409
                    # used for the follower-block — the page reads
                    # reason_code/message from the body, and a 4xx would be
                    # swallowed by its `if (!resp.ok) throw` into a generic
                    # error, losing the honest reason.
                    log_event(
                        logger,
                        "sound.eq_blocked",
                        path=path,
                        reason=refusal.reason_code,
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
