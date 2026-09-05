# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bridge + leg orchestration for the wake-corpus recorder.

Pure-function / systemctl layer extracted verbatim from
``jasper/web/wake_corpus_setup.py``. No asyncio; the only heavy import is
NumPy (used for ``np.ndarray`` typing in ``build_capture_health`` and the
buffer shapes the recorder passes in).

This is the lower layer of the recorder: :mod:`recording_backend` imports
the constants + functions it needs from here, and the thin
``jasper.web.wake_corpus_setup`` HTTP adapter re-exports the public names.
``enter_corpus_test_mode`` / ``exit_corpus_test_mode`` couple to
``RecordingBackend`` only through the HTTP layer (enter/exit handlers) and
through ``RecordingBackend._maybe_recover_stale_test_mode`` calling
``exit_corpus_test_mode``.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import numpy as np

from jasper.control import restart_broker
from jasper.log_event import log_event
from jasper.audio_profile_state import (
    build_audio_profile_status,
    runtime_env_from_mapping,
)
from jasper.aec_sweep import (
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
)
from jasper.mics.xvf3800 import CORPUS_CHIP_AEC_ENABLED_ENV
# Reuse audio I/O + systemctl helpers from the CLI. Single source of
# truth for the WAV format + the "stop jasper-voice to free UDP" dance.
from jasper.cli.wake_enroll import (
    SAMPLE_RATE_HZ,
    VOICE_UNIT,
)
from jasper.wake_ports import build_ports
from jasper.web._common import (
    delete_env_file,
    read_env_file,
    write_env_file,
)
from . import runtime_probe
from .capture_plan import (
    CAPTURE_PLAN_STATE_SESSION,
    WakeCorpusCapturePlan,
    build_capture_plan,
)
from .runtime_probe import (
    AEC3_SWEEP_LEGS,
    BASE_LEGS,
    BRIDGE_CORPUS_OUTPUT_VARS,
    BRIDGE_UNIT,
    CHIP_AEC_LEGS,
    CORPUS_PROFILES,
    DEFAULT_CHIP_REF_BUFFER_FRAMES,
    DEFAULT_CHIP_REF_PERIOD_FRAMES,
    DEFAULT_CHIP_REF_SAMPLE_RATE,
    DEFAULT_USB_MIC_DEVICE,
    DTLN_LEG,
    LEGACY_AEC3_SWEEP_LEGS,
    LEGS,
    OUTPUTD_REF_UDP_TARGET,
    PROFILE_CHIP_AEC_COMPARISON,
    PROFILE_STANDARD,
    RAW0_LEG,
    UNIT_STATE_TIMEOUT_SEC,
    USB_CORPUS_LEGS,
    USB_DTLN_LEG,
    leg_detail,
    legacy_aec3_sweep_source as _legacy_aec3_sweep_source,
    missing_bridge_outputs_from_required,
    required_bridge_outputs_for_request,
    session_aec3_sweep_source as _session_aec3_sweep_source,
    session_legs,
)
# Re-exports only: ``jasper.web.wake_corpus_setup`` imports the recorder's
# whole public surface through this module.
from .capture_plan import (  # noqa: F401
    CAPTURE_PLAN_SCHEMA_VERSION,
    validate_active_capture_plan,
)
from .runtime_probe import (  # noqa: F401
    AEC_MODE_PATH,
    AUDIO_VALIDATION_ARTIFACT_PATH,
    BRIDGE_CORPUS_ENV_PATH,
    BRIDGE_OUTPUT_LABELS,
    BRIDGE_STATS_PATH,
    CHIP_AEC_PROFILE_BASE_LEGS,
    DEFAULT_CHIP_REF_PCM,
    DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE,
    LEG_LABELS,
    OUTPUTD_REF_UDP_PORT,
    SYSTEM_ENV_PATH,
    XVF_RAW0_DTLN_LEG,
    aec_bridge_active,
    bridge_output_status,
    read_bridge_stats_snapshot,
    validation_artifact_summary as _validation_artifact_summary,
)

logger = logging.getLogger("jasper-wake-corpus-web")


# ---------------------------------------------------------------------------
# Bridge-side corpus-output config + systemctl units
# ---------------------------------------------------------------------------
AUDIO_CONTEXT_SCHEMA_VERSION = 1
OUTPUTD_UNIT = "jasper-outputd.service"
AEC_INIT_UNIT = "jasper-aec-init.service"
# Owner of the chip-AEC arming decision and single writer of the daemon-facing
# mic env. The recorder owns its corpus overrides and nothing else, so a corpus
# exit that lands the box in a state only this reconciler can resolve hands off
# here rather than deciding locally — same shape as
# jasper/accessories/reconcile.py's VOICE_INPUT_GATE_UNIT.
AEC_RECONCILE_UNIT = "jasper-aec-reconcile.service"
BRIDGE_RESTART_TIMEOUT_SEC = 30.0
DEFAULT_USB_MIXER_CARD = "Device"
USB_AGC_CONTROL = "Auto Gain Control"


def saved_aec3_sweep_source(data: Mapping[str, Any]) -> str:
    """Recover a session's AEC3 sweep source from new or legacy metadata."""
    saved_config = data.get("aec3_sweep_config")
    saved_source = (
        saved_config.get("input_source")
        if isinstance(saved_config, dict) else None
    )
    return _legacy_aec3_sweep_source(
        str(data.get("aec3_sweep_source") or saved_source or ""),
    )


def chip_aec_config_metadata() -> dict[str, object]:
    """Effective chip-AEC corpus profile recorded with each session."""
    from jasper.mics import xvf3800

    runtime_profile = xvf3800.detect_runtime_profile()
    plan = runtime_profile.chip_beam_plan
    if plan is None:
        return {
            "schema_version": 1,
            "available": False,
            "variant_id": runtime_profile.variant_id,
            "geometry": runtime_profile.geometry,
            "reason": runtime_profile.reason,
        }
    return {
        "schema_version": 1,
        "available": True,
        "variant_id": runtime_profile.variant_id,
        "geometry": runtime_profile.geometry,
        "beam_plan": plan.plan_id,
        "reference_topology": "outputd_direct_fanout",
        "outputd_reference_udp_target": OUTPUTD_REF_UDP_TARGET,
        "chip_ref_pcm": runtime_probe.chip_ref_pcm_for_env(
            {"JASPER_XVF_ALSA_CARD": runtime_profile.alsa_card_name}
        ),
        "chip_ref_sample_rate": int(DEFAULT_CHIP_REF_SAMPLE_RATE),
        "chip_ref_period_frames": int(DEFAULT_CHIP_REF_PERIOD_FRAMES),
        "chip_ref_buffer_frames": int(DEFAULT_CHIP_REF_BUFFER_FRAMES),
        "SHF_BYPASS": 0,
        "AEC_ASROUTONOFF": 1,
        "AEC_ASROUTGAIN": 1.0,
        "AEC_FIXEDBEAMSONOFF": 1,
        "AEC_FIXEDBEAMSGATING": 1,
        "AEC_FIXEDBEAMSAZIMUTH_VALUES": [leg.azimuth_rad for leg in plan.legs],
        "AEC_FIXEDBEAMSELEVATION_VALUES": [leg.elevation_rad for leg in plan.legs],
        "AEC_AECEMPHASISONOFF": 2,
        "AEC_FAR_EXTGAIN": 0.0,
        "AUDIO_MGR_OP_L": [7, 0],
        "AUDIO_MGR_OP_R": [7, 1],
        "beams": [
            {
                "leg": leg.token,
                "channel_index": leg.channel_index,
                "angle_deg": leg.azimuth_deg,
                "label": leg.label,
            }
            for leg in plan.legs
        ],
    }


def missing_bridge_outputs_for_session(
    *,
    corpus_profile: str = PROFILE_STANDARD,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool = False,
    include_aec3_sweep: bool = False,
    aec3_sweep_source: str | None = None,
) -> list[str]:
    """Return bridge outputs that must be enabled before a requested
    session can actually produce the WAV legs the operator checked.

    raw0 is always emitted by the bridge, so it does not participate
    in this check.
    """
    sweep_source = (
        _session_aec3_sweep_source(aec3_sweep_source)
        if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
    )
    required = required_bridge_outputs_for_request(
        corpus_profile=corpus_profile,
        include_dtln=include_dtln,
        include_usb_mic=include_usb_mic,
        include_usb_dtln=include_usb_dtln,
        include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
    )
    return missing_bridge_outputs_from_required(
        required,
        runtime_probe.bridge_output_status(),
        aec3_sweep_source=sweep_source,
    )


def _parse_amixer_bool(output: str) -> bool | None:
    """Parse common amixer boolean forms such as `[on]` or `values=off`."""
    text = output.lower()
    if "[on]" in text or "values=on" in text or ": values=on" in text:
        return True
    if "[off]" in text or "values=off" in text or ": values=off" in text:
        return False
    return None


def usb_mic_status() -> dict[str, Any]:
    """Return operator-facing cheap-USB-mic capture status.

    The raw USB corpus leg is intentionally JTS-unprocessed; this check
    only surfaces whether the mic's own ALSA hardware AGC is enabled.
    """
    env = runtime_probe.read_bridge_env()
    device = env.get("JASPER_AEC_USB_MIC_DEVICE", DEFAULT_USB_MIC_DEVICE)
    mixer_card = env.get("JASPER_AEC_USB_MIXER_CARD", DEFAULT_USB_MIXER_CARD)
    status: dict[str, Any] = {
        "device": device,
        "hardware_agc": {
            "control": USB_AGC_CONTROL,
            "mixer_card": mixer_card,
            "available": False,
            "enabled": None,
        },
    }
    try:
        result = subprocess.run(
            ["amixer", "-c", mixer_card, "get", USB_AGC_CONTROL],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        status["hardware_agc"]["error"] = str(e)
        return status
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            status["hardware_agc"]["error"] = detail[-300:]
        return status
    enabled = _parse_amixer_bool(result.stdout)
    status["hardware_agc"]["available"] = enabled is not None
    status["hardware_agc"]["enabled"] = enabled
    return status


def build_session_audio_context(
    *,
    corpus_profile: str,
    enabled_legs: tuple[str, ...],
    ports: dict[str, int],
    include_raw_mic_0: bool,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool,
    include_aec3_sweep: bool,
    aec3_sweep_source: str,
    chip_aec_config: dict[str, object] | None,
    capture_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Snapshot production profile truth beside the corpus leg choice.

    This is metadata only. It does not open capture devices, change env
    files, or alter production wake detection.
    """
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    capture_plan_legs = (
        list(capture_plan.get("legs", []))
        if isinstance(capture_plan, dict)
        and isinstance(capture_plan.get("legs"), list)
        else None
    )
    fallback = {
        "schema_version": AUDIO_CONTEXT_SCHEMA_VERSION,
        "captured_at": captured_at,
        "status": "unknown",
        "corpus": {
            "profile": corpus_profile,
            "selected_legs": list(enabled_legs),
            "leg_details": capture_plan_legs or [
                leg_detail(
                    leg, ports, aec3_sweep_source=aec3_sweep_source,
                )
                for leg in enabled_legs
            ],
            "capture_plan": capture_plan,
        },
    }
    try:
        intent = runtime_probe.read_aec_intent()
        system_env = runtime_probe.read_system_env()
        bridge_env = runtime_probe.read_bridge_env()
        runtime = runtime_env_from_mapping(system_env, process_env=os.environ)
        mic_probe, mic_identity = runtime_probe.mic_probe_and_identity()
        bridge_outputs = runtime_probe.bridge_output_status()
        chip_gate = runtime_probe.chip_aec_gate_for_status(system_env, intent)
        profile_status = build_audio_profile_status(
            intent,
            runtime,
            mic_probe,
            bridge_active=runtime_probe.aec_bridge_active(),
            chip_available=runtime_probe.mic_chip_aec_available(mic_probe),
            chip_gate=chip_gate,
        )
    except Exception as e:  # noqa: BLE001 - metadata must not block recording
        log_event(
            logger,
            "wake_corpus.audio_context_snapshot_failed",
            error=e,
            level=logging.WARNING,
        )
        return {**fallback, "error": str(e)}

    merged_env = {**system_env, **bridge_env}
    validation = runtime_probe.validation_artifact_summary(
        requested_profile=profile_status["audio_profile"].get("requested"),
        mic_probe=mic_probe,
        system_env=merged_env,
    )
    return {
        "schema_version": AUDIO_CONTEXT_SCHEMA_VERSION,
        "captured_at": captured_at,
        "status": "ok",
        "production_audio_profile": profile_status["audio_profile"],
        "production_intent": asdict(intent),
        "runtime_audio_env": asdict(runtime),
        "microphone": {
            **profile_status["microphone"],
            "identity": mic_identity,
        },
        "corpus": {
            "profile": corpus_profile,
            "profile_kind": (
                "chip_aec_comparison"
                if corpus_profile == PROFILE_CHIP_AEC_COMPARISON
                else "standard"
            ),
            "include_raw_mic_0": include_raw_mic_0,
            "include_dtln": include_dtln,
            "include_usb_mic": include_usb_mic,
            "include_usb_dtln": include_usb_dtln,
            "include_xvf_raw0_dtln": include_xvf_raw0_dtln,
            "include_aec3_sweep": include_aec3_sweep,
            "aec3_sweep_source": aec3_sweep_source,
            "selected_legs": list(enabled_legs),
            "leg_details": capture_plan_legs or [
                leg_detail(
                    leg, ports, aec3_sweep_source=aec3_sweep_source,
                )
                for leg in enabled_legs
            ],
            "chip_aec_config": chip_aec_config,
            "capture_plan": capture_plan,
        },
        "dac_reference": runtime_probe.dac_reference_context(
            merged_env,
            bridge_outputs,
            process_env=os.environ,
            validation=validation,
        ),
        "bridge_outputs": bridge_outputs,
    }


def _nested_int(data: dict[str, Any], *path: str) -> int:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return 0
        current = current.get(key, 0)
    try:
        return int(current)
    except (TypeError, ValueError):
        return 0


def _bridge_counter_delta(
    start: dict[str, Any] | None,
    stop: dict[str, Any] | None,
) -> dict[str, Any]:
    if start is None or stop is None:
        return {
            "available": False,
            "same_process": False,
            "reason": "bridge stats unavailable",
        }
    same_process = (
        start.get("pid") == stop.get("pid")
        and start.get("started_epoch_sec") == stop.get("started_epoch_sec")
    )
    if not same_process:
        return {
            "available": True,
            "same_process": False,
            "reason": "bridge restarted during recording",
            "start": _bridge_identity(start),
            "stop": _bridge_identity(stop),
        }
    start_counters = start.get("counters") if isinstance(start.get("counters"), dict) else {}
    stop_counters = stop.get("counters") if isinstance(stop.get("counters"), dict) else {}

    def diff(*path: str) -> int:
        return max(0, _nested_int(stop_counters, *path) - _nested_int(start_counters, *path))

    queue_drops = {
        key: diff("queue_drops", key)
        for key in ("mic", "chip", "raw0", "usb", "ref")
    }
    udp_drops = {
        leg: diff("udp_send_drops_by_leg", leg)
        for leg in LEGS
    }
    packets_sent = {
        leg: diff("packets_sent_by_leg", leg)
        for leg in LEGS
    }
    return {
        "available": True,
        "same_process": True,
        "start": _bridge_identity(start),
        "stop": _bridge_identity(stop),
        "frames_processed": diff("frames_processed"),
        "ref_starved_frames": diff("ref_starved_frames"),
        "queue_drops": queue_drops,
        "udp_send_drops_by_leg": udp_drops,
        "packets_sent_by_leg": packets_sent,
    }


def _bridge_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "pid": snapshot.get("pid"),
        "started_epoch_sec": snapshot.get("started_epoch_sec"),
        "updated_epoch_sec": snapshot.get("updated_epoch_sec"),
    }


def _leg_bridge_drop_counts(
    leg: str,
    bridge_delta: dict[str, Any],
    *,
    aec3_sweep_source: str = AEC3_SWEEP_SOURCE_XVF,
) -> dict[str, int]:
    queue_drops = bridge_delta.get("queue_drops")
    udp_drops = bridge_delta.get("udp_send_drops_by_leg")
    if not isinstance(queue_drops, dict):
        queue_drops = {}
    if not isinstance(udp_drops, dict):
        udp_drops = {}
    counts: dict[str, int] = {}
    if leg in ("on", "off", "dtln") or (
        leg in AEC3_SWEEP_LEGS
        and aec3_sweep_source == AEC3_SWEEP_SOURCE_XVF
    ):
        counts["mic_queue_full"] = int(queue_drops.get("mic", 0))
    if leg in (
        "on", "dtln", "ref", "usb_webrtc", "usb_dtln",
        "xvf_raw0_webrtc_aec3", "xvf_raw0_dtln",
        *AEC3_SWEEP_LEGS,
    ):
        counts["ref_queue_full"] = int(queue_drops.get("ref", 0))
    if leg in ("raw0", "xvf_raw0_webrtc_aec3", "xvf_raw0_dtln"):
        counts["raw0_queue_full"] = int(queue_drops.get("raw0", 0))
    if leg in CHIP_AEC_LEGS:
        counts["chip_queue_full"] = int(queue_drops.get("chip", 0))
    if leg in ("usb_raw", "usb_webrtc", "usb_dtln") or (
        leg in AEC3_SWEEP_LEGS
        and aec3_sweep_source == AEC3_SWEEP_SOURCE_USB
    ):
        counts["usb_queue_full"] = int(queue_drops.get("usb", 0))
    counts["udp_send_drops"] = int(udp_drops.get(leg, 0))
    if leg in (
        "on", "dtln", "usb_webrtc", "usb_dtln",
        "xvf_raw0_webrtc_aec3", "xvf_raw0_dtln",
        *AEC3_SWEEP_LEGS,
    ):
        counts["ref_starved_frames"] = int(bridge_delta.get("ref_starved_frames", 0))
    return counts


def build_capture_health(
    *,
    wall_duration_sec: float,
    buffers: dict[str, list[np.ndarray]],
    bridge_start: dict[str, Any] | None,
    bridge_stop: dict[str, Any] | None,
    aec3_sweep_source: str = AEC3_SWEEP_SOURCE_XVF,
) -> dict[str, Any]:
    """Build per-clip capture provenance for metadata sidecars."""
    bridge_delta = _bridge_counter_delta(bridge_start, bridge_stop)
    overall_status = "clean"
    notes: list[str] = []
    if not bridge_delta.get("available"):
        overall_status = "unknown"
        notes.append("bridge stats unavailable")
    elif not bridge_delta.get("same_process"):
        overall_status = "compromised"
        notes.append("bridge restarted during recording")

    legs: dict[str, Any] = {}
    max_reasonable_delta = max(0.25, wall_duration_sec * 0.20)
    for leg, frames in buffers.items():
        samples = int(sum(len(frame) for frame in frames))
        packets = len(frames)
        audio_duration_sec = samples / SAMPLE_RATE_HZ if SAMPLE_RATE_HZ else 0.0
        delta_sec = audio_duration_sec - wall_duration_sec
        leg_status = "clean"
        leg_notes: list[str] = []
        if packets == 0:
            leg_status = "compromised"
            leg_notes.append("no packets received")
        elif abs(delta_sec) > max_reasonable_delta:
            leg_status = "warning"
            leg_notes.append("audio duration differs from wall duration")

        drop_counts = _leg_bridge_drop_counts(
            leg,
            bridge_delta,
            aec3_sweep_source=aec3_sweep_source,
        )
        hard_drop_total = sum(
            count for key, count in drop_counts.items()
            if key != "ref_starved_frames"
        )
        if hard_drop_total > 0:
            leg_status = "compromised"
            leg_notes.append("bridge reported upstream drop(s)")
        elif drop_counts.get("ref_starved_frames", 0) > 0 and leg_status == "clean":
            leg_status = "warning"
            leg_notes.append("bridge reused stale reference frame(s)")

        if leg_status == "compromised":
            overall_status = "compromised"
        elif leg_status == "warning" and overall_status == "clean":
            overall_status = "warning"

        legs[leg] = {
            "status": leg_status,
            "packets": packets,
            "samples": samples,
            "audio_duration_sec": audio_duration_sec,
            "duration_delta_sec": delta_sec,
            "bridge_drop_counts": drop_counts,
            "notes": leg_notes,
        }

    return {
        "schema_version": 1,
        "status": overall_status,
        "wall_duration_sec": wall_duration_sec,
        "aec3_sweep_source": aec3_sweep_source,
        "legs": legs,
        "bridge_delta": bridge_delta,
        "notes": notes,
    }


def _broker_restart_or_raise(unit: str, *, timeout_sec: float) -> None:
    """Blocking restart of one unit via jasper-control's restart broker.

    WS1 Phase 3: the wake-corpus bridge-output flow runs inside the
    jasper-web process, which the user-drop PR moves to a non-root service
    user — so it asks the broker rather than shelling out to systemctl. The
    broker returns a result dict (it never raises); we re-raise on failure as
    ``subprocess.CalledProcessError`` to preserve this module's existing
    raise-on-failure contract (callers catch CalledProcessError / OSError and
    surface a 500). While jasper-web is still root the broker client falls
    back to a direct systemctl if the broker is unreachable.
    """
    resp = restart_broker.manage_units(
        unit, verb="restart", reason="wake-corpus bridge outputs",
        no_block=False, timeout=timeout_sec,
    )
    if not resp.get("ok"):
        rc = resp.get("rc")
        raise subprocess.CalledProcessError(
            int(rc) if isinstance(rc, int) else 1,
            ["systemctl", "restart", unit],
            stderr=str(resp.get("stderr") or resp.get("error") or ""),
        )


def restart_aec_bridge() -> None:
    """Restart the bridge and wait for systemd to report the outcome.

    This path is only used for the explicit corpus-output enable flow,
    where the operator is waiting to record immediately. A blocking
    restart is better here than a queued `--no-block` restart because
    a missing USB mic or failed DTLN load should stop the session
    before it records silently-missing legs.
    """
    # reset-failed is best-effort (clears any start-limit lockout before the
    # restart); a non-zero result here must not abort the restart that follows.
    reset = restart_broker.manage_units(
        BRIDGE_UNIT, verb="reset-failed",
        reason="wake-corpus bridge outputs", no_block=False, timeout=5.0,
    )
    if not reset.get("ok"):
        log_event(
            logger,
            "wake_corpus.bridge_reset_failed",
            unit=BRIDGE_UNIT,
            error=reset.get("error") or f"rc={reset.get('rc')}",
            level=logging.WARNING,
        )
    _broker_restart_or_raise(BRIDGE_UNIT, timeout_sec=BRIDGE_RESTART_TIMEOUT_SEC)


def restart_unit(unit: str, timeout_sec: float = BRIDGE_RESTART_TIMEOUT_SEC) -> None:
    _broker_restart_or_raise(unit, timeout_sec=timeout_sec)


_BRIDGE_RESTART_ERRORS = (
    subprocess.CalledProcessError,
    subprocess.TimeoutExpired,
    OSError,
)


def _aec_init_exec_main_status() -> int | None:
    """jasper-aec-init's last ExecStart exit code, or None if unreadable.

    The same signal jasper-aec-reconcile's ``activate_managed_chip_aec``
    branches on to tell a designed park from a fault: `systemctl restart`
    collapses every unit failure to rc=1, so the unit's own exit code is the
    only place that distinction survives. A read-only `systemctl show` — no
    privilege, no broker.
    """
    try:
        result = subprocess.run(
            [
                "systemctl", "show",
                "-p", "ExecMainStatus", "--value", AEC_INIT_UNIT,
            ],
            capture_output=True,
            text=True,
            timeout=UNIT_STATE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip())
    except ValueError:
        return None


def _aec_init_parked_for_commissioning() -> bool:
    """True when aec-init's failure is its designed commissioning park.

    Unreadable status answers False, which keeps the caller on the existing
    rollback path: we only skip a rollback on positive evidence that the box
    parked by design.
    """
    # aec-init owns this exit-code contract; import it rather than restate the
    # integer here. Lazily, because jasper.cli.aec_init pulls the XVF / output-
    # hardware stack into the socket-activated jasper-web process, and this
    # runs only after an aec-init restart has already failed.
    from jasper.cli.aec_init import COMMISSION_REQUIRED_EXIT

    return _aec_init_exec_main_status() == COMMISSION_REQUIRED_EXIT


def _hand_chip_stack_to_reconciler(*, reason: str) -> None:
    """Ask the AEC reconciler to converge the chip stack. Best-effort.

    Non-blocking on purpose: the reconciler restarts aec-init inside its own
    120 s start budget, which does not fit this module's 30 s restart timeout,
    and nothing in the caller's response depends on the converged state — the
    park it lands on is published by its owner (alignment status/reason/action,
    the voice-input-absent marker, doctor, /state).

    A failed kick is logged, not raised: the operator's env change has already
    landed, and jasper-aec-init.service's ``OnFailure=`` names this same unit as
    the backstop (pinned by tests/test_aec_init.py).

    ``manage_units`` documents that it never raises, and the guard below is not
    idle defensiveness against that: this runs inside the ``restart`` closure,
    so anything escaping here would be caught as a failed restart and roll the
    corpus env back — resurrecting the very trap this path exists to close.
    """
    try:
        resp = restart_broker.manage_units(
            AEC_RECONCILE_UNIT, verb="start", reason=reason, no_block=True,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log_event(
            logger,
            "wake_corpus.aec_reconcile_kick_failed",
            unit=AEC_RECONCILE_UNIT,
            error=exc,
            level=logging.WARNING,
        )
        return
    if not resp.get("ok"):
        log_event(
            logger,
            "wake_corpus.aec_reconcile_kick_failed",
            unit=AEC_RECONCILE_UNIT,
            error=resp.get("error") or f"rc={resp.get('rc')}",
            level=logging.WARNING,
        )


def _write_env_and_restart_with_rollback(
    *,
    env_path: str,
    existed: bool,
    old_values: dict[str, str],
    values: dict[str, str],
    restart: Callable[[], None],
    failure_context: str,
) -> None:
    """Apply recorder env and restore it if the matching restart fails."""
    if values:
        write_env_file(env_path, values, mode=0o644)
    else:
        delete_env_file(env_path)
    try:
        restart()
    except _BRIDGE_RESTART_ERRORS:
        if existed:
            write_env_file(env_path, old_values, mode=0o644)
        else:
            delete_env_file(env_path)
        try:
            restart()
        except _BRIDGE_RESTART_ERRORS as rollback_error:
            log_event(
                logger,
                "wake_corpus.bridge_rollback_restart_failed",
                failure_context=failure_context,
                error=rollback_error,
                level=logging.WARNING,
            )
        raise


def set_bridge_outputs_for_session(
    *,
    corpus_profile: str = PROFILE_STANDARD,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool = False,
    include_aec3_sweep: bool = False,
    aec3_sweep_source: str | None = None,
) -> bool:
    """Make recorder-owned bridge output overrides match a session.

    This treats the session toggle selection as the desired test-mode bridge
    state. Production-owned settings in
    /etc or the reconciler env are left alone; the recorder file only
    carries the additional outputs needed for the selected corpus legs.
    Returns True when the bridge was restarted.
    """
    plan = WakeCorpusCapturePlan.from_mapping(build_capture_plan(
        build_ports(),
        corpus_profile=corpus_profile,
        include_dtln=include_dtln,
        include_usb_mic=include_usb_mic,
        include_usb_dtln=include_usb_dtln,
        include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=aec3_sweep_source,
        include_bridge_readiness=True,
        include_runtime_profile=True,
        plan_state=CAPTURE_PLAN_STATE_SESSION,
    ))
    return set_bridge_outputs_for_plan(plan)


def set_bridge_outputs_for_plan(
    plan: WakeCorpusCapturePlan | Mapping[str, Any],
) -> bool:
    """Apply one resolved capture plan to the recorder-owned bridge env."""

    capture_plan = (
        plan if isinstance(plan, WakeCorpusCapturePlan)
        else WakeCorpusCapturePlan.from_mapping(plan)
    )
    env_path = str(runtime_probe.BRIDGE_CORPUS_ENV_PATH)
    existed = runtime_probe.BRIDGE_CORPUS_ENV_PATH.exists()
    old_values = read_env_file(env_path)
    had_chip_profile = any(
        old_values.get(key)
        for key in (
            CORPUS_CHIP_AEC_ENABLED_ENV,
            "JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED",
            "JASPER_OUTPUTD_CHIP_REF_PCM",
            "JASPER_OUTPUTD_REFERENCE_UDP_TARGET",
        )
    )
    values = dict(old_values)
    for key in BRIDGE_CORPUS_OUTPUT_VARS:
        values.pop(key, None)
    values.update(capture_plan.env_overrides())

    if values == old_values:
        return False

    corpus_profile = str(capture_plan.data.get("corpus_profile") or PROFILE_STANDARD)

    def restart() -> None:
        if had_chip_profile or corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
            restart_unit(OUTPUTD_UNIT)
            restart_unit(AEC_INIT_UNIT)
        restart_aec_bridge()

    _write_env_and_restart_with_rollback(
        env_path=env_path,
        existed=existed,
        old_values=old_values,
        values=values,
        restart=restart,
        failure_context="configure",
    )
    return True


def disable_bridge_corpus_outputs() -> bool:
    """Return the bridge to production-light corpus output mode.

    We remove only recorder-owned output overrides so the bridge falls
    back to the reconciler's production intent. This matters for DTLN:
    `JASPER_AEC_DTLN_ENABLED` is also the underlying production wake-leg
    flag written by `jasper-aec-reconcile`, so cleanup must not force it
    off when the /system Wake detection card intentionally enabled it.
    Unrelated settings such as the selected USB mic device are preserved.

    Leaving a chip corpus profile puts jasper-aec-init back on the production
    path, where an uncommissioned box parks by design. That park is the
    *correct* post-corpus state, not a broken restart, so it does not roll the
    exit back — see the aec-init branch in ``restart`` below.
    """
    env_path = str(runtime_probe.BRIDGE_CORPUS_ENV_PATH)
    existed = runtime_probe.BRIDGE_CORPUS_ENV_PATH.exists()
    old_values = read_env_file(env_path)
    had_chip_profile = any(
        old_values.get(key)
        for key in (
            CORPUS_CHIP_AEC_ENABLED_ENV,
            "JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED",
            "JASPER_OUTPUTD_CHIP_REF_PCM",
            "JASPER_OUTPUTD_REFERENCE_UDP_TARGET",
        )
    )
    values = dict(old_values)
    for key in BRIDGE_CORPUS_OUTPUT_VARS:
        values.pop(key, None)
    if values == old_values:
        return False

    def restart() -> None:
        if had_chip_profile:
            restart_unit(OUTPUTD_UNIT)
            try:
                restart_unit(AEC_INIT_UNIT)
            except _BRIDGE_RESTART_ERRORS:
                if not _aec_init_parked_for_commissioning():
                    raise
                # The exit landed and the box is now on the production path,
                # where it correctly requires commissioning. Rolling back here
                # would restore the corpus env and re-enter corpus mode — an
                # inescapable trap, because the artifact an uncommissioned box
                # lacks is exactly what it cannot obtain from inside corpus
                # mode (issue #2254). Returning instead of raising leaves the
                # env written: an exit intent is never reverted.
                #
                # The bridge is deliberately NOT restarted: park_managed_xvf
                # stops it, so starting it here would race the owner's stop and
                # can leave a bridge running against a parked mic.
                log_event(
                    logger,
                    "wake_corpus.corpus_exit_parked",
                    unit=AEC_INIT_UNIT,
                    reason="chip-AEC alignment is not commissioned",
                    action="run sudo jasper-aec-commission",
                    level=logging.WARNING,
                )
                _hand_chip_stack_to_reconciler(
                    reason="wake-corpus exit: chip-AEC needs commissioning",
                )
                return
        restart_aec_bridge()

    _write_env_and_restart_with_rollback(
        env_path=env_path,
        existed=existed,
        old_values=old_values,
        values=values,
        restart=restart,
        failure_context="disable",
    )
    return True


def _default_enabled_legs(ports: dict[str, int]) -> tuple[str, ...]:
    """Session default: base production legs that exist in this process."""
    return tuple(leg for leg in BASE_LEGS if leg in ports)


def _enabled_legs_from_metadata(
    data: dict[str, Any], ports: dict[str, int],
) -> tuple[str, ...]:
    """Recover the session leg set from new or legacy metadata."""
    aec3_sweep_source = saved_aec3_sweep_source(data)
    raw = data.get("enabled_legs")
    if isinstance(raw, list):
        raw_legs = tuple(
            str(leg) for leg in raw
            if str(leg) in LEGS
        )
        include_aec3_sweep = (
            bool(data.get("include_aec3_sweep", False))
            or any(
                leg in AEC3_SWEEP_LEGS or leg in LEGACY_AEC3_SWEEP_LEGS
                for leg in raw_legs
            )
        )
        if include_aec3_sweep:
            return session_legs(
                ports,
                include_dtln=bool(data.get("include_dtln", DTLN_LEG in raw_legs)),
                include_raw_mic_0=bool(
                    data.get("include_raw_mic_0", RAW0_LEG in raw_legs),
                ),
                include_usb_mic=bool(
                    data.get(
                        "include_usb_mic",
                        any(leg in USB_CORPUS_LEGS for leg in raw_legs),
                    ),
                ),
                include_usb_dtln=bool(
                    data.get("include_usb_dtln", USB_DTLN_LEG in raw_legs),
                ),
                include_aec3_sweep=True,
                aec3_sweep_source=aec3_sweep_source,
            )
        legs: list[str] = []
        for leg in raw_legs:
            if leg in AEC3_SWEEP_LEGS or leg in LEGACY_AEC3_SWEEP_LEGS:
                continue
            if leg not in ports:
                continue
            legs.append(leg)
        legs = tuple(dict.fromkeys(legs))
        if legs:
            return legs
    return session_legs(
        ports,
        corpus_profile=str(data.get("corpus_profile") or PROFILE_STANDARD),
        include_dtln=bool(data.get("include_dtln", True)),
        include_raw_mic_0=bool(data.get("include_raw_mic_0", False)),
        include_usb_mic=bool(data.get("include_usb_mic", False)),
        include_usb_dtln=bool(data.get("include_usb_dtln", False)),
        include_xvf_raw0_dtln=bool(data.get("include_xvf_raw0_dtln", False)),
        include_aec3_sweep=bool(data.get("include_aec3_sweep", False)),
        aec3_sweep_source=aec3_sweep_source,
    )


def _metadata_flag(
    data: dict[str, Any],
    key: str,
    leg: str,
    enabled_legs: tuple[str, ...],
) -> bool:
    """Return a saved capture flag, capped to legs this process can record."""
    requested = bool(data.get(key, leg in enabled_legs))
    return requested and leg in enabled_legs


# ---------------------------------------------------------------------------
# Voice-daemon control — same systemctl helpers as wake-enroll
# ---------------------------------------------------------------------------


def voice_daemon_active() -> bool:
    """True if jasper-voice is active; fail soft for status snapshots."""
    try:
        return runtime_probe.systemd_unit_active(VOICE_UNIT)
    except (OSError, subprocess.TimeoutExpired):
        return False


def set_voice_daemon_state(action: str) -> None:
    """Start or stop jasper-voice through jasper-control's restart broker
    (WS1 Phase 3). Blocking + raise-on-failure (CalledProcessError) to
    preserve the prior `systemctl ... check=True` contract — callers surface
    the failure to the operator."""
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    resp = restart_broker.manage_units(
        VOICE_UNIT, verb=action, reason="wake-corpus voice control",
        no_block=False, timeout=BRIDGE_RESTART_TIMEOUT_SEC,
    )
    if not resp.get("ok"):
        rc = resp.get("rc")
        raise subprocess.CalledProcessError(
            int(rc) if isinstance(rc, int) else 1,
            ["systemctl", action, VOICE_UNIT],
            stderr=str(resp.get("stderr") or resp.get("error") or ""),
        )


def enter_corpus_test_mode(
    *,
    corpus_profile: str = PROFILE_STANDARD,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool = False,
    include_aec3_sweep: bool = False,
    aec3_sweep_source: str | None = None,
) -> None:
    """Stop jasper-voice and apply the selected optional bridge legs."""
    if corpus_profile not in CORPUS_PROFILES:
        raise ValueError(f"unknown corpus_profile: {corpus_profile}")
    sweep_source = (
        _session_aec3_sweep_source(aec3_sweep_source)
        if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
    )
    if include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB:
        include_usb_mic = True
    try:
        voice_was_active = runtime_probe.systemd_unit_active(VOICE_UNIT)
    except subprocess.TimeoutExpired as e:
        raise OSError(
            f"could not determine whether {VOICE_UNIT} is active: "
            f"systemctl probe timed out after {UNIT_STATE_TIMEOUT_SEC:g}s",
        ) from e
    except OSError as e:
        raise OSError(
            f"could not determine whether {VOICE_UNIT} is active: {e}",
        ) from e
    set_voice_daemon_state("stop")
    try:
        set_bridge_outputs_for_session(
            corpus_profile=corpus_profile,
            include_dtln=include_dtln,
            include_usb_mic=include_usb_mic,
            include_usb_dtln=include_usb_dtln,
            include_xvf_raw0_dtln=include_xvf_raw0_dtln,
            include_aec3_sweep=include_aec3_sweep,
            aec3_sweep_source=sweep_source,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        if voice_was_active:
            try:
                set_voice_daemon_state("start")
            except (subprocess.CalledProcessError, OSError) as start_error:
                log_event(
                    logger,
                    "wake_corpus.voice_restore_failed",
                    unit=VOICE_UNIT,
                    error=start_error,
                    level=logging.WARNING,
                )
        raise


def exit_corpus_test_mode() -> None:
    """Disable recorder-owned bridge outputs and restart jasper-voice."""
    disable_bridge_corpus_outputs()
    set_voice_daemon_state("start")
