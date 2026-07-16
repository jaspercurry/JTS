# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared web/operator orchestration for active-speaker measurement tests.

The active-speaker domain already owns the safety state machines:
``startup_load`` loads guarded graphs, ``commission_ramp`` gates audible driver
steps, and ``safe_playback`` records floor confirmation. This module wires those
pieces into one reusable operator service so HTTPS correction can run the same
measurement prerequisites without importing the `/sound/` page module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import socket
import subprocess
import threading
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jasper.active_speaker import capture_entry_anchor
from jasper.active_speaker.calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    calibration_level_payload,
    clamp_test_level_dbfs,
    load_calibration_level_state,
)
from jasper.active_speaker.commission_ramp import (
    abort_ramp,
    clear_pending_ramp_step,
    load_ramp_state,
    ramp_audible_step,
    record_ramp_operator_ack,
)
from jasper.active_speaker.commission_wiring import (
    commission_load_config,
    commission_seams,
    read_current_config_path,
    resolve_commission_inputs,
    write_commission_path_safety,
)
from jasper.active_speaker.camilla_yaml import APPLIED_RESPONSE_FILTER_MODE
from jasper.active_speaker.measurement import (
    confirmed_driver_roles,
    current_driver_floor_evidence,
    load_measurement_state,
    record_driver_measurement,
    record_summed_test_artifact,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.safe_playback import (
    arm_safe_playback_session,
    load_safe_playback_state,
    record_safe_playback_result,
)
from jasper.active_speaker.staging import (
    DEFAULT_CAMILLA_CONFIG_DIR,
    load_staged_startup_config,
    stage_protected_startup_config,
)
from jasper.active_speaker.startup_load import (
    commission_load_runtime_status,
    commission_load_state_with_runtime_status,
    load_commission_load_state,
    load_driver_commissioning_config,
    load_protected_startup_config,
    load_startup_load_state,
    load_summed_commissioning_config,
    mark_commission_load_state_stale,
    rollback_driver_commissioning_config,
)
from jasper.active_speaker.topology_tone import build_summed_topology_tone_plan
from jasper.camilla import CamillaUnavailable
from jasper.camilla_config_contract import DEFAULT_VOLUME_LIMIT_DB
from jasper.log_event import log_event
from jasper.output_topology import (
    OutputTopology,
    load_output_topology,
    save_output_topology,
    set_channel_protection_status,
)

logger = logging.getLogger(__name__)

CamillaFactory = Callable[[], Any]

COMMISSION_TONE_ALSA_DEVICE = "correction_substream"
COMMISSION_TONE_DURATION_S = 35.0
COMMISSION_TONE_RESTART_MARGIN_S = 3.0
COMMISSION_TONE_STARTUP_CHECK_S = 0.08
COMMISSION_TONE_SAMPLE_RATE = 48000
COMMISSION_TONE_SOURCE_DBFS = 0.0
COMMISSION_TONE_BACKEND = "correction_substream_continuous_tone"
SUMMED_COMMISSION_TONE_BACKEND = "correction_substream_summed_tone"
SUMMED_COMMISSION_SPEECH_BACKEND = "correction_substream_summed_speech"
DRIVER_CAPTURE_SWEEP_BACKEND = "correction_substream_driver_sweep"
SUMMED_CAPTURE_SWEEP_BACKEND = "correction_substream_summed_sweep"
AUTOMATIC_EXCITATION_GAIN_SOURCE = "applied_baseline_recomposition_snapshot"
DEFAULT_MEASUREMENT_SWEEP_DIR = Path("/var/lib/jasper/active_speaker_sweeps")
MEASUREMENT_SWEEP_DIR_ENV = "JASPER_ACTIVE_SPEAKER_SWEEP_DIR"
DEFAULT_AUTOMATIC_SUMMED_CONFIG_PATH = Path(
    "/var/lib/camilladsp/configs/active_speaker_automatic_summed_measurement.yml"
)
AUTOMATIC_SUMMED_CONFIG_PATH_ENV = "JASPER_ACTIVE_SPEAKER_SUMMED_MEASUREMENT_CONFIG"
COMMISSION_TONE_MUX_SOCKET = os.environ.get(
    "JASPER_MUX_CONTROL_SOCKET", "/run/jasper-mux/control.sock",
)
COMMISSION_TONE_FANIN_LABEL = "correction"
_COMMISSION_TONE_LOCK = threading.Lock()
_COMMISSION_TONE_SESSION: dict[str, Any] | None = None


@dataclass(frozen=True)
class FaninGateContext:
    """Nesting context for a tone/sweep played inside another feature's hold.

    A correction measurement window (``jasper.correction.coordinator``) holds
    the mux's single test fan-in gate for its whole duration under its own
    owner. When commission-tone playback runs *inside* that window (the
    crossover-driver-sweep relay flow), it must not claim the gate under its
    own standalone owner (``active-speaker-commissioning``) — the mux refuses
    a second owner outright. Passing a ``FaninGateContext`` makes the tone
    path select/restore under the OUTER owner instead: the mux already allows
    same-owner re-select (``select_test_fanin_label`` treats a matching owner
    as a lease refresh, not a conflict), so the gate stays continuously held
    by one owner across the window. ``restore_label`` is the label the outer
    owner had selected before the tone started; end-of-tone always relabels
    back to it rather than releasing — the outer caller's own end-of-window
    release remains the only release. ``None`` (the default everywhere) means
    the standalone ``/sound/`` commissioning path: today's unchanged
    behavior, owning and releasing its own gate.
    """

    owner: str
    restore_label: str


_EVIDENCE_READ_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    json.JSONDecodeError,
)
_MUX_COMMAND_ERRORS = (
    OSError,
    RuntimeError,
    json.JSONDecodeError,
    UnicodeError,
)
_COMMISSION_OPERATION_ERRORS = (
    CamillaUnavailable,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
)
_TASK_SETTLE_ERRORS = (
    Exception,
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
)
_PLAYBACK_OPERATION_ERRORS = (
    OSError,
    RuntimeError,
    subprocess.TimeoutExpired,
)
_COMMISSION_START_ERRORS = _COMMISSION_OPERATION_ERRORS + _MUX_COMMAND_ERRORS


class AutomaticDriverConfigRestoreError(RuntimeError):
    """Automatic driver capture could not restore its entry production config."""


class AutomaticSummedConfigRestoreError(RuntimeError):
    """Automatic summed capture could not restore its prior production config."""


_SUMMED_TEST_ARM_REPORT: dict[str, Any] = {
    "status": "ready",
    "load_gate": "ready",
    "ok_to_load_active_config": True,
    "camilla_config": {},
    "safe_playback": {},
    "issues": [],
}


def _issue(code: str, message: str) -> dict[str, str]:
    return {"severity": "blocker", "code": code, "message": message}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _config_paths_match(a: str | Path | None, b: str | Path | None) -> bool:
    if not a or not b:
        return False
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except (OSError, RuntimeError):
        return Path(str(a)) == Path(str(b))


def request_missing_software_guards(
    topology: OutputTopology,
) -> tuple[OutputTopology, bool]:
    """Persist software-guard requests needed before active commissioning."""

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


def _stage_startup_config(
    topology: OutputTopology,
    *,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if preset is None and crossover_preview is None:
        from jasper.active_speaker.crossover_preview import load_crossover_preview
        from jasper.active_speaker.design_draft import load_design_draft

        design_draft = load_design_draft()
        crossover_preview = load_crossover_preview(
            current_design_draft=design_draft
        )
    return stage_protected_startup_config(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
    )


async def _load_startup_config(
    camilla_factory: CamillaFactory,
    *,
    path_safety_evidence_path: str | Path | None = None,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    topology = load_output_topology()
    cam = camilla_factory()
    return await load_protected_startup_config(
        topology,
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
        path_safety_evidence_path=path_safety_evidence_path
        or _path_safety_evidence_path(),
        acquire_lock=acquire_lock,
    )


def _path_safety_evidence_path() -> str | None:
    from jasper.active_speaker.path_safety import path_safety_evidence_path

    evidence_path = os.environ.get("JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE")
    if evidence_path and evidence_path.strip():
        return evidence_path.strip()
    default_path = path_safety_evidence_path()
    return str(default_path) if default_path.exists() else None


def _blocked_startup_anchor(
    *,
    group: str,
    role: str,
    code: str,
    message: str,
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
            "issues": [_issue(code, message)],
        },
    }


async def _ensure_commission_startup_anchor(
    *,
    group: str,
    role: str,
    staged_config: dict[str, Any],
    current_config_path: str | None,
    camilla_factory: CamillaFactory,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Ensure commissioning has the silent startup graph as rollback anchor."""

    if preset is not None and crossover_preview is not None:
        raise ValueError(
            "commissioning startup anchor requires one resolved graph source"
        )
    staged_path = (staged_config.get("config") or {}).get("path")
    if _config_paths_match(current_config_path, staged_path):
        return {"status": "already_loaded", "staged_config_path": staged_path}

    topology, _guards_changed = request_missing_software_guards(load_output_topology())
    stage = _stage_startup_config(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
    )
    if stage.get("status") != "staged":
        return _blocked_startup_anchor(
            group=group,
            role=role,
            code="commission_startup_anchor_not_staged",
            message="could not stage the silent active-speaker setup before driver testing",
            startup_setup={"status": "blocked", "stage": stage},
        )

    staged = load_staged_startup_config()
    cam = camilla_factory()
    path, error = await read_current_config_path(cam)
    evidence_path = write_commission_path_safety(topology, staged, path, error)

    from jasper.active_speaker.path_safety import evaluate_path_safety_evidence

    try:
        import json as _json

        report = evaluate_path_safety_evidence(
            _json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        )
    except _EVIDENCE_READ_ERRORS as exc:
        report = {"status": "blocked", "load_gate": "blocked", "error": str(exc)}
    if report.get("load_gate") != "ready":
        return _blocked_startup_anchor(
            group=group,
            role=role,
            code="commission_startup_anchor_path_safety_blocked",
            message="could not verify the silent active-speaker setup path before driver testing",
            startup_setup={"status": "blocked", "stage": stage, "path_safety": report},
        )

    startup_load = await _load_startup_config(
        camilla_factory,
        path_safety_evidence_path=evidence_path,
        acquire_lock=acquire_lock,
    )
    load_state = _dict_value(startup_load.get("load"))
    if load_state.get("status") != "loaded" or not load_state.get(
        "rollback_available"
    ):
        return _blocked_startup_anchor(
            group=group,
            role=role,
            code="commission_startup_anchor_load_failed",
            message="could not load the silent active-speaker setup before driver testing",
            startup_setup={
                "status": "blocked",
                "stage": stage,
                "path_safety": report,
                "startup_load": startup_load,
            },
        )

    return {
        "status": "loaded",
        "staged_config_path": _dict_value(stage.get("config")).get("path"),
        "path_safety_load_gate": report.get("load_gate"),
        "startup_load_status": load_state.get("status"),
        "rollback_available": bool(load_state.get("rollback_available")),
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


def _combined_speech_stimulus_wav_path() -> tuple[Path, dict[str, Any]]:
    from jasper.active_speaker.speech_stimulus import ensure_combined_speech_stimulus

    return ensure_combined_speech_stimulus()


def _measurement_sweep_wav_path(
    duration_s: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Return the cached swept-sine WAV + metadata used by acoustic capture.

    ``duration_s`` is supplied by the protected driver signal plan.  Leaving it
    unset preserves the historical shared default for compatibility callers.
    The cache filename already includes the realized duration, so role-specific
    sweeps cannot collide.
    """

    from jasper.active_speaker import driver_acoustics as acoustic
    from jasper.audio_measurement import sweep as sweep_mod

    cache_dir = Path(
        os.environ.get(MEASUREMENT_SWEEP_DIR_ENV) or DEFAULT_MEASUREMENT_SWEEP_DIR
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    signal, meta = sweep_mod.synchronized_swept_sine(
        f1=acoustic.DEFAULT_F1_HZ,
        f2=acoustic.DEFAULT_F2_HZ,
        duration_approx_s=(
            duration_s if duration_s is not None else acoustic.DEFAULT_DURATION_S
        ),
        sample_rate=acoustic.DEFAULT_SAMPLE_RATE,
        amplitude_dbfs=acoustic.DEFAULT_AMPLITUDE_DBFS,
    )
    wav_path = cache_dir / (
        "active_speaker_sweep_"
        f"{int(meta.f1)}_{int(meta.f2)}Hz_"
        f"{int(round(meta.duration_s * 1000))}ms_"
        f"{int(abs(meta.amplitude_dbfs) * 10)}dbm_"
        f"{meta.sample_rate}Hz.wav"
    )
    if not wav_path.exists():
        sweep_mod.write_sweep_wav(wav_path, signal, meta.sample_rate)
    return wav_path, meta.to_dict()


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


def _commission_tone_select_fanin_lane(
    fanin_gate_context: FaninGateContext | None = None,
) -> dict[str, Any]:
    owner = (
        fanin_gate_context.owner
        if fanin_gate_context is not None
        else "active-speaker-commissioning"
    )
    try:
        return _commission_tone_mux_command(
            f"TEST_SELECT {COMMISSION_TONE_FANIN_LABEL} {owner}",
        )
    except _MUX_COMMAND_ERRORS:
        # SELECT may have landed even when its response was lost. Standalone
        # mode's owner-scoped release cannot disturb another feature's gate.
        # Nested mode must not release the outer owner's gate either — it
        # recovers by restoring the outer owner's prior label instead.
        _commission_tone_release_fanin_lane(
            reason="select_indeterminate", fanin_gate_context=fanin_gate_context,
        )
        raise


def _commission_tone_release_fanin_lane(
    *, reason: str, fanin_gate_context: FaninGateContext | None = None,
) -> dict[str, Any]:
    if fanin_gate_context is not None:
        # Nested under another feature's hold: never release that owner's
        # gate. Relabel back to what the outer owner had selected before the
        # tone started — same-owner re-select is a lease refresh, not a
        # conflict, so the gate stays continuously held by the outer owner.
        command = (
            f"TEST_SELECT {fanin_gate_context.restore_label} "
            f"{fanin_gate_context.owner}"
        )
        action = "fanin_restore"
    else:
        command = "TEST_RELEASE active-speaker-commissioning"
        action = "fanin_release"
    try:
        payload = _commission_tone_mux_command(command)
    except _MUX_COMMAND_ERRORS as exc:
        log_event(
            logger,
            "active_speaker.web_commission_tone",
            level=logging.WARNING,
            action=action,
            reason=reason,
            status="failed",
            error=str(exc),
        )
        return {"status": "failed", "reason": reason, "error": str(exc)}
    log_event(
        logger,
        "active_speaker.web_commission_tone",
        action=action,
        reason=reason,
        status="ok",
        active_source=payload.get("active_source"),
    )
    return payload


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
    payload: dict[str, Any] = {
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
    if was_running and proc is not None:
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
    return {
        "status": "stopped" if was_running else "expired",
        "reason": reason,
        "playback_id": session.get("playback_id"),
        "target_key": session.get("target_key"),
    }


def stop_commission_tone(*, reason: str) -> dict[str, Any]:
    """Stop the continuous per-driver tone and release the test fan-in lane."""

    with _COMMISSION_TONE_LOCK:
        payload = _stop_commission_tone_locked(reason=reason)
    payload["fanin_gate"] = _commission_tone_release_fanin_lane(reason=reason)
    log_event(
        logger,
        "active_speaker.web_commission_tone",
        action="stop",
        reason=reason,
        status=payload.get("status"),
    )
    return payload


async def play_commission_tone(
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
            "active_speaker.web_commission_tone",
            level=logging.WARNING,
            action="plan",
            status="blocked",
            group=group_id,
            role=role,
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
        fanin_gate = _commission_tone_select_fanin_lane()
    except _COMMISSION_START_ERRORS as exc:
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
                    and abs(float(session.get("frequency_hz", 0.0)) - frequency_hz)
                    < 0.01
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
        proc = started_proc
        await asyncio.sleep(COMMISSION_TONE_STARTUP_CHECK_S)
        if proc.poll() is not None:
            with _COMMISSION_TONE_LOCK:
                if (
                    _COMMISSION_TONE_SESSION
                    and _COMMISSION_TONE_SESSION.get("process") is proc
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
                            f"aplay exited during startup with rc={proc.returncode}"
                        )
                    )
                ],
                fanin_gate=fanin_gate,
                signal_plan=signal_plan,
            )

    log_event(
        logger,
        "active_speaker.web_commission_tone",
        action="start",
        group=group_id,
        role=role,
        frequency_hz=frequency_hz,
        duration_s=COMMISSION_TONE_DURATION_S,
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


def _plan_with_issues(
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
        "issues": [*_dict_items(plan.get("issues")), *issues],
    }


def commission_status_payload() -> dict[str, Any]:
    """Return the active-speaker operator measurement state."""

    return {
        "commission_load": load_commission_load_state(),
        "ramp": load_ramp_state(),
        "safe_playback": load_safe_playback_state(),
    }


async def start_driver_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Arm and play one bounded per-driver test tone."""

    if not isinstance(raw, dict):
        raise ValueError("driver test request must be an object")
    group = str(raw.get("speaker_group_id") or raw.get("group") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    force = bool(raw.get("force"))
    if blocking_phase is not None:
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking_phase,
            "next_step": "Finish the other measurement before testing a driver.",
        }
    if not group or not role:
        raise ValueError("speaker_group_id and role are required")

    if force:
        stop_commission_tone(reason="driver_test_force")

    cam = camilla_factory()
    existing = load_commission_load_state()
    if existing.get("status") == "loaded":
        try:
            running_raw = await cam.get_active_config_raw(best_effort=False)
        except _COMMISSION_OPERATION_ERRORS:
            running_raw = None
        live_existing = commission_load_state_with_runtime_status(
            existing,
            commission_load_runtime_status(existing, running_raw),
        )
        active_target = _dict_value(live_existing.get("target"))
        same_target = (
            live_existing.get("status") == "loaded"
            and (active_target.get("speaker_group_id") or "") == group
            and (active_target.get("role") or "") == role
        )
        if live_existing.get("status") == "loaded" and not same_target:
            if not force:
                return {
                    "status": "refused",
                    "reason": "commission_load_already_active",
                    "active_target": active_target,
                    "next_step": "Stop the active driver test before starting another.",
                }
            await rollback_driver_commissioning_config(
                load_config=commission_load_config(cam),
            )
        elif live_existing.get("status") != "loaded":
            runtime_status = _dict_value(live_existing.get("runtime_status"))
            mark_commission_load_state_stale(existing, runtime_status)

    topology, guards_changed = request_missing_software_guards(load_output_topology())
    if guards_changed:
        log_event(
            logger,
            "active_speaker.web_driver_test",
            action="request_software_guards",
            group=group,
            role=role,
        )
    preset, crossover_preview = resolve_commission_inputs()
    staged = load_staged_startup_config()
    current_config_path, current_config_error = await read_current_config_path(cam)
    startup_setup = await _ensure_commission_startup_anchor(
        group=group,
        role=role,
        staged_config=staged,
        current_config_path=current_config_path,
        camilla_factory=camilla_factory,
        preset=preset,
        crossover_preview=crossover_preview,
    )
    if startup_setup.get("status") == "blocked":
        return startup_setup

    staged = load_staged_startup_config()
    current_config_path, current_config_error = await read_current_config_path(cam)
    evidence_path = write_commission_path_safety(
        topology, staged, current_config_path, current_config_error
    )
    load_config, read_running_config, get_current_config_path = commission_seams(cam)
    commission = load_commission_load_state()
    target = _dict_value(commission.get("target"))
    if (
        commission.get("status") != "loaded"
        or (target.get("speaker_group_id") or "") != group
        or (target.get("role") or "") != role
    ):
        load_payload = await load_driver_commissioning_config(
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
        if (load_payload.get("load") or {}).get("status") == "loaded":
            clear_pending_ramp_step(
                speaker_group_id=group,
                confirmed_roles=confirmed_driver_roles(
                    topology,
                    speaker_group_id=group,
                ),
            )
    else:
        load_payload = {"status": "loaded", "load": commission}

    async def _play_commission_tone(**kwargs: Any) -> dict[str, Any]:
        return await play_commission_tone(
            **kwargs,
            topology=topology,
            preset=preset,
            crossover_preview=crossover_preview,
        )

    step = await ramp_audible_step(
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
        confirmed_roles=confirmed_driver_roles(
            topology,
            speaker_group_id=group,
        ),
    )
    log_event(
        logger,
        "active_speaker.web_driver_test",
        action="start",
        status=step.get("status"),
        group=group,
        role=role,
    )
    return {
        "status": step.get("status"),
        "startup_setup": startup_setup,
        "load": load_payload,
        "step": step,
        "commission": commission_status_payload(),
    }


async def confirm_driver_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Record operator floor confirmation for the active driver test."""

    if not isinstance(raw, dict):
        raise ValueError("driver confirmation request must be an object")
    outcome = str(raw.get("outcome") or "heard_correct_driver").strip().lower()
    prior_ramp = load_ramp_state()
    pending = prior_ramp.get("pending")
    if outcome != "silent":
        tone_stop = stop_commission_tone(reason=f"ack_{outcome}")
    else:
        tone_stop = {"status": "not_stopped", "reason": "silent_retry"}
    cam = camilla_factory()
    payload = await record_ramp_operator_ack(
        outcome=outcome,
        load_config=commission_load_config(cam),
    )
    should_record_driver_evidence = (
        outcome == "heard_correct_driver"
        and payload.get("status") == "confirmed"
        and not payload.get("issues")
    ) or (outcome == "heard_wrong_driver" and payload.get("status") == "aborted")
    if should_record_driver_evidence and isinstance(pending, dict):
        topology = load_output_topology()
        measurements = record_driver_measurement(
            topology,
            {
                "speaker_group_id": prior_ramp.get("speaker_group_id"),
                "role": pending.get("role"),
                "outcome": outcome,
                "playback_id": pending.get("playback_id"),
                "test_level_dbfs": pending.get("gain_db"),
                "notes": "Recorded from secure correction crossover driver confirmation.",
            },
            calibration_level=load_calibration_level_state(),
            safe_session=load_safe_playback_state(),
        )
        payload["measurements"] = measurements
    payload["tone_stop"] = tone_stop
    payload["commission"] = commission_status_payload()
    log_event(
        logger,
        "active_speaker.web_driver_test",
        action="confirm",
        outcome=outcome,
        status=payload.get("status"),
    )
    return payload


async def abort_driver_test(*, camilla_factory: CamillaFactory) -> dict[str, Any]:
    """Hard stop: stop any tone and re-mute the transient driver graph."""

    tone_stop = stop_commission_tone(reason="driver_test_abort")
    payload = await abort_ramp(load_config=commission_load_config(camilla_factory()))
    payload["tone_stop"] = tone_stop
    payload["commission"] = commission_status_payload()
    log_event(
        logger,
        "active_speaker.web_driver_test",
        action="abort",
        status=payload.get("status"),
    )
    return payload


def _crossover_frequency_for_group(
    preview: dict[str, Any],
    speaker_group_id: str,
) -> float | None:
    groups = _dict_items(preview.get("groups"))
    for group in groups:
        if group.get("group_id") != speaker_group_id:
            continue
        crossovers = _dict_items(group.get("crossovers"))
        for crossover in crossovers:
            frequency = _finite(crossover.get("proposed_frequency_hz"))
            if frequency is None:
                continue
            if frequency > 0:
                return frequency
    return None


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _transient_summed_level(
    *,
    calibration_level: dict[str, Any],
    measurements: dict[str, Any],
    speaker_group_id: str,
    requested_level: Any,
) -> dict[str, Any]:
    current = _finite(
        _dict_value(calibration_level.get("test_signal")).get("requested_level_dbfs")
    )
    summary = _dict_value(measurements.get("summary"))
    latest_tests = _dict_value(summary.get("latest_summed_tests"))
    latest = latest_tests.get(speaker_group_id)
    latest_record = _dict_value(latest)
    latest_issues = _dict_items(latest_record.get("issues"))
    latest_ok = (
        bool(latest_record)
        and latest_record.get("captured") is True
        and latest_record.get("audio_emitted") is True
        and not any(
            issue.get("severity") == "blocker"
            for issue in latest_issues
        )
    )
    if latest_ok:
        latest_tone = _dict_value(latest_record.get("tone"))
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


async def _load_summed_commissioning_config(
    *,
    topology: OutputTopology,
    speaker_group_id: str,
    level_dbfs: float,
    startup_gate_calibration_level: dict[str, Any] | None,
    preset: Any,
    crossover_preview: dict[str, Any] | None,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    cam = camilla_factory()
    staged = load_staged_startup_config()
    current_config_path, _ = await read_current_config_path(cam)
    startup_setup = await _ensure_commission_startup_anchor(
        group=speaker_group_id,
        role="summed",
        staged_config=staged,
        current_config_path=current_config_path,
        camilla_factory=camilla_factory,
        preset=preset,
        crossover_preview=crossover_preview,
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


async def _rollback_summed_commissioning_config(
    *,
    camilla_factory: CamillaFactory,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    cam = camilla_factory()
    return await rollback_driver_commissioning_config(
        load_config=commission_load_config(cam),
        acquire_lock=acquire_lock,
    )


async def _load_applied_summed_measurement_config(
    *,
    topology: OutputTopology,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Load a transient full Layer-A graph strictly from its applied snapshot."""
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
        recompose_applied_baseline_yaml,
    )
    from jasper.dsp_apply import validate_camilla_config

    applied = _dict_value(load_applied_baseline_profile_state())
    excitation = automatic_summed_excitation(topology, applied)
    if excitation.get("status") != "ready":
        return {
            "status": "blocked",
            "load": {
                "status": "blocked",
                "issues": [_issue(
                    str(excitation.get("reason")),
                    str(excitation.get("detail")),
                )],
            },
            "excitation": excitation,
        }
    target = Path(
        os.environ.get(AUTOMATIC_SUMMED_CONFIG_PATH_ENV)
        or DEFAULT_AUTOMATIC_SUMMED_CONFIG_PATH
    )
    _yaml, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        out_path=target,
    )
    if issues:
        return {
            "status": "blocked",
            "load": {"status": "blocked", "issues": issues},
            "excitation": excitation,
        }
    validation = validate_camilla_config(target)
    if not validation.ok_to_apply:
        return {
            "status": "blocked",
            "load": {
                "status": "blocked",
                "issues": [_issue(
                    "automatic_summed_config_validation_failed",
                    "the applied crossover measurement graph failed validation",
                )],
            },
            "validation": validation.to_dict(),
            "excitation": excitation,
        }
    cam = camilla_factory()
    previous = await cam.get_config_file_path(best_effort=False)
    if not previous:
        return {
            "status": "blocked",
            "load": {
                "status": "blocked",
                "issues": [_issue(
                    "automatic_summed_rollback_anchor_missing",
                    "the current DSP graph could not be saved for rollback",
                )],
            },
            "validation": validation.to_dict(),
            "excitation": excitation,
        }

    async def _restore_previous_after_failed_load() -> dict[str, Any]:
        try:
            restored = await cam.set_config_file_path(
                str(previous), best_effort=False
            )
        except _COMMISSION_OPERATION_ERRORS as exc:
            return {"status": "failed", "error": str(exc)}
        return {
            "status": "rolled_back" if restored is True else "failed",
            "config_path": str(previous),
        }

    async def _restore_previous_after_failed_load_resilient() -> dict[str, Any]:
        async def _restore_or_raise() -> dict[str, Any]:
            rollback = await _restore_previous_after_failed_load()
            if rollback.get("status") != "rolled_back":
                raise AutomaticSummedConfigRestoreError(
                    str(rollback.get("error") or "CamillaDSP rejected the prior graph")
                )
            return rollback

        restore_task = asyncio.create_task(_restore_or_raise())
        return await _await_restore_task_resilient(restore_task)

    async def _restore_after_failed_load_payload() -> dict[str, Any]:
        try:
            return await _restore_previous_after_failed_load_resilient()
        except AutomaticSummedConfigRestoreError as exc:
            return {"status": "failed", "error": str(exc)}

    def _failed_load_payload(
        *,
        message: str,
        rollback: dict[str, Any],
        failure_mode: str,
    ) -> dict[str, Any]:
        issues = [_issue(
            "automatic_summed_config_load_failed",
            message,
        )]
        if rollback.get("status") != "rolled_back":
            rollback_issue = _issue(
                "automatic_summed_config_rollback_failed",
                (
                    "JTS could not restore the prior DSP graph after the "
                    "measurement graph failed to load. Stop measuring and "
                    "reapply the speaker profile before playing audio."
                ),
            )
            # Lead with the hardware-safety action: playback_issue_text and the
            # wizard render the first blocker as the operator-facing refusal.
            issues.insert(0, rollback_issue)
            log_event(
                logger,
                "active_speaker.automatic_summed_config_rollback",
                level=logging.WARNING,
                status="failed",
                failure_mode=failure_mode,
                previous_config_path=str(previous),
                error=rollback.get("error"),
            )
        return {
            "status": "blocked",
            "load": {"status": "blocked", "issues": issues},
            "validation": validation.to_dict(),
            "excitation": excitation,
            "rollback": rollback,
        }

    load_task = asyncio.create_task(
        cam.set_config_file_path(str(target), best_effort=False)
    )
    try:
        # The real controller offloads this operation to a thread. Shield the
        # task so caller cancellation cannot detach us from a worker that may
        # still set and reload the transient graph.
        loaded = await asyncio.shield(load_task)
    except asyncio.CancelledError as operation_error:
        async def _settle_load_then_restore() -> dict[str, Any]:
            try:
                await load_task
            except _TASK_SETTLE_ERRORS:
                # Cancellation already owns the outward result. Whatever the
                # lost response says, the prior graph must be restored after
                # the worker has definitively stopped mutating CamillaDSP.
                pass
            return await _restore_applied_summed_previous_config(
                str(previous),
                camilla_factory=camilla_factory,
            )

        cleanup_task = asyncio.create_task(_settle_load_then_restore())
        try:
            await _await_restore_task_resilient(cleanup_task)
        except AutomaticSummedConfigRestoreError as restore_error:
            raise restore_error from operation_error
        raise
    except _COMMISSION_OPERATION_ERRORS as exc:
        rollback = await _restore_after_failed_load_payload()
        return _failed_load_payload(
            message=f"the applied crossover measurement graph did not load: {exc}",
            rollback=rollback,
            failure_mode="load_exception",
        )
    except _TASK_SETTLE_ERRORS as operation_error:
        # The caller has not received ``previous_config_path`` yet, so it
        # cannot own rollback if an unexpected base exception escapes the load.
        try:
            await _restore_applied_summed_previous_config_resilient(
                str(previous),
                camilla_factory=camilla_factory,
            )
        except AutomaticSummedConfigRestoreError as restore_error:
            raise restore_error from operation_error
        raise
    if loaded is not True:
        rollback = await _restore_after_failed_load_payload()
        return _failed_load_payload(
            message="the applied crossover measurement graph did not load",
            rollback=rollback,
            failure_mode="load_returned_false",
        )
    return {
        "status": "loaded",
        "load": {
            "status": "loaded",
            "config_path": str(target),
            "previous_config_path": str(previous),
            "rollback_available": True,
            "graph_source": AUTOMATIC_EXCITATION_GAIN_SOURCE,
        },
        "validation": validation.to_dict(),
        "excitation": excitation,
    }


async def _restore_applied_summed_previous_config(
    previous_config_path: str,
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Restore summed capture's production graph or fail loudly."""
    restore_error: str | None
    try:
        cam = camilla_factory()
        restored = await cam.set_config_file_path(
            previous_config_path,
            best_effort=False,
        )
    except _COMMISSION_OPERATION_ERRORS as exc:
        restore_error = str(exc)
    else:
        restore_error = (
            None if restored is True else "CamillaDSP rejected the prior graph"
        )
    if restore_error is not None:
        log_event(
            logger,
            "active_speaker.automatic_summed_config_rollback",
            level=logging.WARNING,
            status="failed",
            failure_mode="load_interrupted",
            previous_config_path=previous_config_path,
            error=restore_error,
        )
        raise AutomaticSummedConfigRestoreError(restore_error)
    return {"status": "rolled_back", "config_path": previous_config_path}


async def _restore_applied_summed_previous_config_resilient(
    previous_config_path: str,
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Finish summed production restoration while the caller is cancelled."""
    restore_task = asyncio.create_task(
        _restore_applied_summed_previous_config(
            previous_config_path,
            camilla_factory=camilla_factory,
        )
    )
    return await _await_restore_task_resilient(restore_task)


async def _await_restore_task_resilient(
    restore_task: asyncio.Task[dict[str, Any]],
) -> dict[str, Any]:
    """Await one graph restoration before propagating caller cancellation."""
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(restore_task)
            break
        except asyncio.CancelledError as exc:
            if restore_task.cancelled():
                raise
            cancellation = exc
    if cancellation is not None:
        raise cancellation
    return result


async def _rollback_applied_summed_measurement_config(
    load_payload: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    previous = _dict_value(load_payload.get("load")).get("previous_config_path")
    if not previous:
        raise RuntimeError("automatic summed measurement has no rollback anchor")
    cam = camilla_factory()
    restored = await cam.set_config_file_path(str(previous), best_effort=False)
    if restored is not True:
        raise RuntimeError("automatic summed measurement rollback was rejected")
    return {"status": "rolled_back", "config_path": str(previous)}


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
        "backend": SUMMED_COMMISSION_SPEECH_BACKEND,
        "audio_emitted": False,
        "confirmable": False,
        "issues": [*_dict_items(playback.get("issues")), issue],
    })
    if commissioning_load is not None:
        out["commissioning_load"] = commissioning_load
    if rollback is not None:
        out["rollback"] = rollback
    if fanin_gate is not None:
        out["fanin_gate"] = fanin_gate
    return out


def _commission_summed_stimulus_issue(exc: BaseException) -> dict[str, str]:
    return _issue(
        "tone_backend_failed",
        f"could not prepare the combined test speech: {exc}",
    )


def _capture_sweep_issue(exc: BaseException) -> dict[str, str]:
    return {
        "severity": "blocker",
        "code": "capture_sweep_playback_failed",
        "message": f"could not play the active-speaker measurement sweep: {exc}",
    }


def _latest_summed_test(
    measurements: dict[str, Any],
    *,
    speaker_group_id: str,
) -> dict[str, Any] | None:
    summary = _dict_value(measurements.get("summary"))
    latest = _dict_value(summary.get("latest_summed_tests"))
    record = latest.get(speaker_group_id)
    return record if isinstance(record, dict) else None


def _has_blocker(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(
        issue.get("severity") == "blocker"
        for issue in _dict_items(payload.get("issues"))
    )


def _refused_capture_sweep(reason: str, message: str) -> dict[str, Any]:
    return {
        "status": "refused",
        "reason": reason,
        "audio_emitted": False,
        "issues": [_issue(reason, message)],
    }


def automatic_driver_excitation(
    topology: OutputTopology,
    role: str,
    *,
    applied_profile: dict[str, Any] | None = None,
    locked_main_volume_db: float | None = None,
) -> dict[str, Any]:
    """Resolve automatic driver excitation from the immutable applied Layer A.

    Manual floor-confirmation gains are intentionally not inputs. They prove
    that the operator heard the intended driver, but their quiet discovery
    floor (-20/-60 dB in legacy records) is not an acoustic measurement level.
    The automatic level tone calibrated the protected applied graph, so an
    isolated ESS must use that same graph's role gain.
    """
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
    from jasper.audio_measurement.excitation import (
        AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
    )

    loaded_profile = (
        applied_profile
        if applied_profile is not None
        else load_applied_baseline_profile_state()
    )
    profile = _dict_value(loaded_profile)
    validated = validated_applied_measurement_snapshot(topology, profile)
    if validated.get("status") != "ready":
        return validated
    snapshot = _dict_value(validated.get("snapshot"))
    corrections = snapshot.get("corrections")
    role_values = (
        corrections.get(role)
        if isinstance(corrections, dict)
        else None
    )
    role_gain_db = (
        _finite(role_values.get("gain_db"))
        if isinstance(role_values, dict)
        else None
    )
    if role_gain_db is None:
        return {
            "status": "blocked",
            "reason": "automatic_crossover_applied_excitation_unavailable",
            "detail": (
                f"the protected applied speaker profile has no safe gain for {role}; "
                "reapply the crossover before measuring"
            ),
        }
    payload: dict[str, Any] = {
        "status": "ready",
        "schema_version": 1,
        "scope": (
            "sweep_plus_role_gain_and_driver_level_lock"
            if locked_main_volume_db is not None
            else "sweep_plus_role_varying_commission_gain"
        ),
        "sweep_peak_dbfs": AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
        "commissioning_gain_db": role_gain_db,
        "effective_peak_dbfs": (
            AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS + role_gain_db
        ),
        "gain_source": AUTOMATIC_EXCITATION_GAIN_SOURCE,
        "baseline_id": str(profile.get("baseline_id") or ""),
        "topology_id": topology.topology_id,
        "role": role,
    }
    if locked_main_volume_db is not None:
        payload["locked_main_volume_db"] = float(locked_main_volume_db)
        payload["effective_peak_dbfs"] += float(locked_main_volume_db)
    return payload


def automatic_summed_excitation(
    topology: OutputTopology,
    applied_profile: dict[str, Any],
) -> dict[str, Any]:
    """Describe the immutable full Layer-A graph used by a summed ESS."""
    from jasper.audio_measurement.excitation import (
        AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
    )

    validated = validated_applied_measurement_snapshot(topology, applied_profile)
    if validated.get("status") != "ready":
        return validated
    snapshot = _dict_value(validated.get("snapshot"))
    corrections = _dict_value(snapshot.get("corrections"))
    normalized: dict[str, dict[str, Any]] = {}
    for role, raw in corrections.items():
        gain = _finite(raw.get("gain_db")) if isinstance(raw, dict) else None
        delay = _finite(raw.get("delay_ms")) if isinstance(raw, dict) else None
        inverted = raw.get("inverted") if isinstance(raw, dict) else None
        if gain is None or delay is None or not isinstance(inverted, bool):
            return {
                "status": "blocked",
                "reason": "automatic_crossover_applied_excitation_unavailable",
                "detail": f"the applied crossover has no safe correction for {role}",
            }
        normalized[str(role)] = {
            "gain_db": gain,
            "delay_ms": delay,
            "inverted": inverted,
            "effective_peak_dbfs": (
                AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS + gain
            ),
        }
    return {
        "status": "ready",
        "schema_version": 1,
        "scope": "sweep_plus_applied_full_layer_a_graph",
        "sweep_peak_dbfs": AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
        "gain_source": AUTOMATIC_EXCITATION_GAIN_SOURCE,
        "baseline_id": str(applied_profile.get("baseline_id") or ""),
        "topology_id": topology.topology_id,
        "corrections": normalized,
    }


def validated_applied_measurement_snapshot(
    topology: OutputTopology,
    applied_profile: dict[str, Any],
) -> dict[str, Any]:
    """Return the canonical full applied snapshot or its stable refusal."""
    from jasper.active_speaker.baseline_profile import topology_config_fingerprint
    from jasper.active_speaker.crossover_contract import crossover_snapshot_state

    state = crossover_snapshot_state(
        applied_profile,
        expected_topology_id=topology.topology_id,
        expected_topology_fingerprint=topology_config_fingerprint(topology),
        expected_domain="full",
    )
    if state.get("valid") is not True:
        return {
            "status": "blocked",
            "reason": str(
                state.get("reason")
                or "automatic_crossover_applied_excitation_unavailable"
            ),
            "detail": str(
                state.get("detail")
                or "reapply the protected crossover before measuring"
            ),
        }
    return {
        "status": "ready",
        "snapshot": applied_profile.get("recomposition_snapshot"),
        "snapshot_state": state,
    }


def _played_excitation_ledger(
    planned: dict[str, Any],
    sweep_meta: dict[str, Any],
) -> dict[str, Any]:
    """Bind planned applied gain to the actual generated sweep metadata."""
    actual_peak = _finite(sweep_meta.get("amplitude_dbfs"))
    planned_peak = _finite(planned.get("sweep_peak_dbfs"))
    scope = planned.get("scope")
    gain = _finite(planned.get("commissioning_gain_db"))
    main_gain = _finite(planned.get("locked_main_volume_db"))
    if (
        planned.get("status") != "ready"
        or actual_peak is None
        or planned_peak is None
        or abs(actual_peak - planned_peak) > 1e-6
        or (
            scope in {
                "sweep_plus_role_varying_commission_gain",
                "sweep_plus_role_gain_and_driver_level_lock",
            }
            and gain is None
        )
        or (scope == "sweep_plus_role_gain_and_driver_level_lock" and main_gain is None)
        or scope
        not in {
            "sweep_plus_role_varying_commission_gain",
            "sweep_plus_role_gain_and_driver_level_lock",
            "sweep_plus_applied_full_layer_a_graph",
        }
    ):
        raise RuntimeError(
            "automatic crossover sweep excitation does not match the level tone"
        )
    ledger = {
        key: value
        for key, value in {
            **planned,
            "status": None,
            "sweep_peak_dbfs": actual_peak,
        }.items()
        if value is not None
    }
    if gain is not None:
        ledger["effective_peak_dbfs"] = actual_peak + gain + (main_gain or 0.0)
    return ledger


async def _load_driver_commissioning_config_for_level(
    *,
    topology: OutputTopology,
    speaker_group_id: str,
    role: str,
    level_dbfs: float,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    startup_gate_calibration_level: dict[str, Any] | None,
    preset: Any,
    crossover_preview: dict[str, Any] | None,
    camilla_factory: CamillaFactory,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    cam = camilla_factory()
    entry_config_path, entry_config_error = await read_current_config_path(cam)
    transaction = {
        "kind": "automatic_driver_capture",
        "entry_config_path": entry_config_path,
        "entry_config_error": entry_config_error,
        "restored": False,
    }
    if not entry_config_path:
        return {
            "status": "blocked",
            "load": {
                "status": "blocked",
                "issues": [_issue(
                    "automatic_driver_entry_config_missing",
                    (
                        "JTS could not save the current production DSP config "
                        "for restoration"
                    ),
                )],
            },
            "measurement_transaction": transaction,
        }
    transaction_payload = {"measurement_transaction": transaction}
    try:
        staged = load_staged_startup_config()
        # De-anchoring live production? Stash its path durably FIRST (before
        # the anchor reload can replace it), so the sequence-level restore
        # (restore_pending_capture_entry_config) has a crash-safe target. On
        # later loads in the same sequence the entry path IS the anchor, this
        # writer is skipped, and the stash keeps the original production path.
        staged_anchor_path = (staged.get("config") or {}).get("path")
        if not staged_anchor_path or not _config_paths_match(
            entry_config_path, staged_anchor_path
        ):
            capture_entry_anchor.record_entry(entry_config_path)
        startup_setup = await _ensure_commission_startup_anchor(
            group=speaker_group_id,
            role=role,
            staged_config=staged,
            current_config_path=entry_config_path,
            camilla_factory=camilla_factory,
            preset=preset,
            crossover_preview=crossover_preview,
            acquire_lock=acquire_lock,
        )
        if startup_setup.get("status") == "blocked":
            startup_setup["measurement_transaction"] = transaction
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
        # ``startup_setup["status"] == "loaded"`` means _ensure_commission_startup_anchor
        # just reloaded the all-muted anchor a moment ago and already triggered
        # jasper-audio-hardware-reconcile for this exact DAC/topology (the
        # "already_loaded" fast path, taken when nothing needed reloading, does
        # not). The automatic capture-sweep flow's own cleanup
        # (_restore_automatic_driver_entry_config) reverts CamillaDSP's
        # persisted config path to the pre-commissioning production config
        # after every single attempt, so an immediate retry of the same
        # speaker_group_id/role (jasper.active_speaker.repeat_admission) always
        # takes the reload branch here — hardware-reproduced on JTS3
        # 2026-07-16: every audio_hardware_reconcile run in that window
        # reported env_changed=0 render_changed=0 (a verified no-op), yet
        # load_driver_commissioning_config's default reconcile_output_hardware
        # asked for a SECOND reconcile run milliseconds after the first,
        # doubling the reconcile+CamillaDSP-graph-churn paid immediately before
        # the mic-capture aplay call on every retry. The output hardware
        # cannot have changed in that window, so skip the second reconcile the
        # same way commission_ramp.py's same-target ramp steps already do.
        just_reconciled_hardware = startup_setup.get("status") == "loaded"
        payload = await load_driver_commissioning_config(
            topology,
            speaker_group_id=speaker_group_id,
            role=role,
            calibration_level=startup_gate_calibration_level,
            load_config=load_config,
            read_running_config=read_running_config,
            get_current_config_path=get_current_config_path,
            preset=preset,
            crossover_preview=crossover_preview,
            staged_config=staged,
            audible_gain_db=level_dbfs,
            volume_limit_db=volume_limit_db,
            filter_mode=APPLIED_RESPONSE_FILTER_MODE,
            path_safety_evidence_path=evidence_path,
            acquire_lock=acquire_lock,
            reconcile_output_hardware=not just_reconciled_hardware,
        )
        payload["startup_setup"] = startup_setup
        payload["measurement_transaction"] = transaction
        return payload
    except BaseException as operation_error:  # noqa: BLE001
        # The startup-anchor call may already have replaced production with the
        # all-muted graph. No exception, including task cancellation, may escape
        # this automatic path until the persisted production config from entry
        # is restored. An inline audition is intentionally not resurrected.
        try:
            await _restore_automatic_driver_entry_config_resilient(
                transaction_payload,
                camilla_factory=camilla_factory,
                acquire_lock=acquire_lock,
            )
        except AutomaticDriverConfigRestoreError as restore_error:
            raise restore_error from operation_error
        raise


def _automatic_driver_restore_issue() -> dict[str, str]:
    return _issue(
        "automatic_driver_config_restore_failed",
        (
            "JTS could not restore the production DSP config from before the "
            "measurement. Stop measuring and reapply the speaker profile before "
            "playing audio."
        ),
    )


async def _restore_automatic_driver_entry_config(
    load_payload: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Idempotently restore automatic capture's entry production config path.

    The file-backed production config is the durable owner. Deliberately do not
    resurrect a transient ``set_active_config_raw`` audition that happened to be
    live on entry.
    """
    transaction = _dict_value(load_payload.get("measurement_transaction"))
    entry_path = str(transaction.get("entry_config_path") or "")
    if transaction.get("restored") is True:
        return {"status": "already_restored", "config_path": entry_path}
    if not entry_path:
        error = "automatic driver capture has no entry production DSP config"
        log_event(
            logger,
            "active_speaker.automatic_driver_config_restore",
            level=logging.WARNING,
            status="failed",
            error=error,
        )
        raise AutomaticDriverConfigRestoreError(error)

    inner_rollback: dict[str, Any] | None = None
    try:
        inner_rollback = await _rollback_summed_commissioning_config(
            camilla_factory=camilla_factory,
            acquire_lock=acquire_lock,
        )
    except _COMMISSION_OPERATION_ERRORS as exc:
        inner_rollback = {"status": "failed", "error": str(exc)}

    restore_error: str | None
    try:
        cam = camilla_factory()
        restored = await cam.set_config_file_path(entry_path, best_effort=False)
    except _COMMISSION_OPERATION_ERRORS as exc:
        restore_error = str(exc)
    else:
        restore_error = (
            None if restored is True else "CamillaDSP rejected the entry graph"
        )
    if restore_error is not None:
        inner_status = (inner_rollback or {}).get("status") or _dict_value(
            (inner_rollback or {}).get("rollback")
        ).get("status")
        log_event(
            logger,
            "active_speaker.automatic_driver_config_restore",
            level=logging.WARNING,
            status="failed",
            entry_config_path=entry_path,
            inner_rollback_status=inner_status,
            error=restore_error,
        )
        raise AutomaticDriverConfigRestoreError(restore_error)
    transaction["restored"] = True
    return {
        "status": "rolled_back",
        "config_path": entry_path,
        "inner_rollback": inner_rollback,
    }


async def _restore_automatic_driver_entry_config_resilient(
    load_payload: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Finish production restoration even while the caller is being cancelled."""
    restore_task = asyncio.create_task(
        _restore_automatic_driver_entry_config(
            load_payload,
            camilla_factory=camilla_factory,
            acquire_lock=acquire_lock,
        )
    )
    return await _await_restore_task_resilient(restore_task)


async def _rollback_capture_attempt_to_anchor(
    load_payload: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Re-mute one automatic capture attempt WITHOUT restoring production.

    Reloads the all-muted staged anchor into the RUNNING graph and leaves the
    persisted config path anchored. The production entry path stays stashed in
    ``capture_entry_anchor`` for the sequence-level restore
    (:func:`restore_pending_capture_entry_config`); restoring production after
    every attempt is exactly the rapid double-config-swap churn that starved
    the fan-in -> loopback -> CamillaDSP measurement transport (JTS3
    2026-07-16 deterministic ``aplay`` timeouts). Staying anchored between
    attempts also matches the crash posture ``startup_load``'s S3 guard
    enforces during loads — the durable config points at the all-muted staged
    anchor, so a crash/reboot anywhere in the sequence comes back muted.
    """

    transaction = _dict_value(load_payload.get("measurement_transaction"))
    entry_path = str(transaction.get("entry_config_path") or "")
    if transaction.get("restored") is True:
        return {"status": "already_restored", "config_path": entry_path}
    inner_rollback = await _rollback_summed_commissioning_config(
        camilla_factory=camilla_factory,
        acquire_lock=acquire_lock,
    )
    rollback_state = _dict_value(inner_rollback.get("rollback"))
    status = str(rollback_state.get("status") or inner_rollback.get("status") or "")
    # "blocked" = no loaded per-driver commissioning state, i.e. the running
    # graph is already the anchor — nothing audible to re-mute. Anything but
    # rolled_back/blocked may leave the driver audible: fail loudly so the
    # caller flips the attempt to failed (same contract as the entry restore).
    if status not in {"rolled_back", "blocked"}:
        log_event(
            logger,
            "active_speaker.automatic_driver_config_restore",
            level=logging.WARNING,
            status="failed",
            action="anchor_rollback",
            inner_rollback_status=status or "unknown",
        )
        raise AutomaticDriverConfigRestoreError(
            "could not re-mute the automatic capture path back to the staged anchor"
        )
    transaction["restored"] = True
    return {
        "status": "anchored",
        "inner_rollback": inner_rollback,
        "pending_entry_config_path": entry_path or None,
    }


async def _rollback_capture_attempt_to_anchor_resilient(
    load_payload: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Finish the anchor re-mute even while the caller is being cancelled."""
    restore_task = asyncio.create_task(
        _rollback_capture_attempt_to_anchor(
            load_payload,
            camilla_factory=camilla_factory,
            acquire_lock=acquire_lock,
        )
    )
    return await _await_restore_task_resilient(restore_task)


async def restore_pending_capture_entry_config(
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Restore the stashed production entry config once, at sequence exit.

    The counterpart of ``capture_entry_anchor.record_entry``: automatic
    capture attempts leave the persisted CamillaDSP path on the all-muted
    staged anchor between attempts, and this converges it back to the
    production config from sequence entry. Called from recovery surfaces
    (jasper-correction-web's service-start claim boundary). Outcomes:

    - ``idle``: no stash — nothing pending.
    - ``deferred``: CamillaDSP unreachable; stash retained (muted-safe) so a
      later surface can converge.
    - ``superseded``: the persisted path is no longer the staged anchor —
      another owner (a crossover apply, an operator) repointed production;
      the stash is obsolete and cleared without touching CamillaDSP.
    - ``entry_missing``: the stashed config file no longer exists; stash
      cleared, speaker stays on the anchor (muted, never loud).
    - ``restored``: production reloaded, stash cleared.
    """

    entry = capture_entry_anchor.pending_entry()
    if not entry:
        return {"status": "idle"}
    cam = camilla_factory()
    current, current_error = await read_current_config_path(cam)
    if current_error is not None or not current:
        log_event(
            logger,
            "active_speaker.capture_entry_restore",
            level=logging.WARNING,
            status="deferred",
            reason=current_error or "current_config_unknown",
        )
        return {
            "status": "deferred",
            "reason": current_error or "current_config_unknown",
        }
    staged = load_staged_startup_config()
    staged_anchor_path = (staged.get("config") or {}).get("path")
    if not staged_anchor_path or not _config_paths_match(current, staged_anchor_path):
        capture_entry_anchor.clear()
        log_event(
            logger,
            "active_speaker.capture_entry_restore",
            status="superseded",
            current_config_path=current,
        )
        return {"status": "superseded", "current_config_path": current}
    if not Path(entry).exists():
        capture_entry_anchor.clear()
        log_event(
            logger,
            "active_speaker.capture_entry_restore",
            level=logging.WARNING,
            status="entry_missing",
            entry_config_path=entry,
        )
        return {"status": "entry_missing", "entry_config_path": entry}
    restored = await cam.set_config_file_path(entry, best_effort=False)
    if restored is not True:
        log_event(
            logger,
            "active_speaker.capture_entry_restore",
            level=logging.WARNING,
            status="failed",
            entry_config_path=entry,
        )
        return {"status": "failed", "entry_config_path": entry}
    capture_entry_anchor.clear()
    log_event(
        logger,
        "active_speaker.capture_entry_restore",
        status="restored",
        entry_config_path=entry,
    )
    return {"status": "restored", "config_path": entry}


async def prepare_automatic_driver_level_match(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
    preset: ActiveSpeakerPreset,
    applied_profile: dict[str, Any],
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Load one protected isolated driver path for its level-match tone."""

    excitation = automatic_driver_excitation(
        topology,
        role,
        applied_profile=applied_profile,
    )
    if excitation.get("status") != "ready":
        raise RuntimeError(str(excitation.get("detail") or excitation.get("reason")))
    load_payload = await _load_driver_commissioning_config_for_level(
        topology=topology,
        speaker_group_id=speaker_group_id,
        role=role,
        level_dbfs=float(excitation["commissioning_gain_db"]),
        startup_gate_calibration_level=calibration_level_payload(),
        preset=preset,
        crossover_preview=None,
        camilla_factory=camilla_factory,
    )
    load_state = _dict_value(load_payload.get("load"))
    if load_state.get("status") != "loaded":
        transaction = _dict_value(load_payload.get("measurement_transaction"))
        if transaction.get("entry_config_path"):
            await _restore_automatic_driver_entry_config_resilient(
                load_payload, camilla_factory=camilla_factory
            )
        issues = _dict_items(load_state.get("issues"))
        primary_issue = next(
            (
                issue
                for issue in issues
                if str(issue.get("severity") or "").lower() == "blocker"
            ),
            issues[0] if issues else {},
        )
        issue_code = str(
            primary_issue.get("code") or "automatic_driver_level_path_not_loaded"
        )
        issue_message = str(
            primary_issue.get("message")
            or "could not load the protected isolated driver path"
        )
        log_event(
            logger,
            "correction.crossover_driver_level_match",
            status="blocked",
            group=speaker_group_id,
            role=role,
            issue_code=issue_code,
        )
        raise RuntimeError(issue_message)
    return {"load": load_payload, "excitation": excitation}


async def restore_automatic_driver_level_match(
    prepared: dict[str, Any], *, camilla_factory: CamillaFactory
) -> dict[str, Any]:
    """Re-mute the level-tone path back to the all-muted staged anchor.

    Deliberately does NOT restore the production entry graph: the level match
    is the first step of the automatic measurement sequence, and the sweep
    attempts that follow reuse the same staged anchor (the anchor fast path in
    ``_ensure_commission_startup_anchor``). The production path from sequence
    entry stays stashed in ``capture_entry_anchor`` until
    :func:`restore_pending_capture_entry_config` runs at sequence exit.
    """

    return await _rollback_capture_attempt_to_anchor_resilient(
        _dict_value(prepared.get("load")), camilla_factory=camilla_factory
    )


async def _play_capture_sweep(
    *,
    backend: str,
    target: dict[str, Any],
    playback_id: str,
    level_dbfs: float,
    load_payload: dict[str, Any],
    camilla_factory: CamillaFactory,
    planned_excitation: dict[str, Any] | None = None,
    rollback_capture_config: (
        Callable[[], Coroutine[Any, Any, dict[str, Any]]] | None
    ) = None,
    sweep_duration_s: float | None = None,
) -> dict[str, Any]:
    from jasper.correction.playback import play_sweep

    sweep_meta: dict[str, Any] = {}
    fanin_gate: dict[str, Any] | None = None
    rollback: dict[str, Any] | None = None
    rollback_issue: dict[str, str] | None = None
    try:
        if sweep_duration_s is None:
            wav_path, sweep_meta = _measurement_sweep_wav_path()
        else:
            wav_path, sweep_meta = _measurement_sweep_wav_path(sweep_duration_s)
        excitation = (
            _played_excitation_ledger(planned_excitation, sweep_meta)
            if isinstance(planned_excitation, dict)
            else None
        )
        duration_s = float(sweep_meta.get("duration_s") or 6.0)
        fanin_gate = _commission_tone_select_fanin_lane()
        await play_sweep(
            wav_path,
            alsa_device=COMMISSION_TONE_ALSA_DEVICE,
            timeout_s=duration_s + 5.0,
        )
        payload = {
            "status": "completed",
            "backend": backend,
            "playback_id": playback_id,
            "audio_emitted": True,
            "confirmable": True,
            "target": target,
            "sweep_meta": sweep_meta,
            "excitation": excitation,
            "tone": {"level_dbfs": level_dbfs},
            "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
            "commissioning_load": load_payload,
            "fanin_gate": fanin_gate,
            "issues": [],
        }
    except _PLAYBACK_OPERATION_ERRORS as exc:
        payload = {
            "status": "failed",
            "backend": backend,
            "playback_id": playback_id,
            "audio_emitted": False,
            "confirmable": False,
            "target": target,
            "sweep_meta": sweep_meta,
            "excitation": None,
            "tone": {"level_dbfs": level_dbfs},
            "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
            "commissioning_load": load_payload,
            "fanin_gate": fanin_gate,
            "issues": [_capture_sweep_issue(exc)],
        }
    finally:
        if fanin_gate is not None:
            _commission_tone_release_fanin_lane(reason="capture_sweep")
        try:
            rollback_operation = (
                rollback_capture_config()
                if rollback_capture_config is not None
                else _rollback_summed_commissioning_config(
                    camilla_factory=camilla_factory,
                )
            )
            rollback_task = asyncio.create_task(rollback_operation)
            rollback = await _await_restore_task_resilient(rollback_task)
        except _COMMISSION_OPERATION_ERRORS as exc:
            if not isinstance(exc, AutomaticDriverConfigRestoreError):
                log_event(
                    logger,
                    "active_speaker.web_capture_sweep",
                    level=logging.WARNING,
                    action="rollback",
                    status="failed",
                    error=str(exc),
                )
            rollback_issue = (
                _automatic_driver_restore_issue()
                if isinstance(exc, AutomaticDriverConfigRestoreError)
                else _issue(
                    "capture_sweep_rollback_failed",
                    "measurement sweep played, but JTS could not re-mute the active-speaker test path",
                )
            )
    if rollback is not None:
        payload["rollback"] = rollback
    if rollback_issue is not None:
        payload["status"] = "failed"
        payload["confirmable"] = False
        payload["issues"] = (
            [rollback_issue, *_dict_items(payload.get("issues"))]
            if rollback_issue["code"] == "automatic_driver_config_restore_failed"
            else [*_dict_items(payload.get("issues")), rollback_issue]
        )
    return payload


async def play_driver_capture_sweep(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
    applied_profile: dict[str, Any] | None = None,
    locked_main_volume_db: float | None = None,
    fanin_gate_context: FaninGateContext | None = None,
) -> dict[str, Any]:
    """Play the analyzer sweep through one already-confirmed driver path.

    ``fanin_gate_context`` is set only when this sweep runs inside a
    correction measurement window (the crossover-driver-sweep relay flow) —
    see ``FaninGateContext``. ``None`` (the default) is the standalone
    ``/sound/`` commissioning path with today's unchanged behavior.
    """

    if not isinstance(raw, dict):
        raise ValueError("driver capture sweep request must be an object")
    if blocking_phase is not None:
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking_phase,
            "next_step": "Finish the other measurement before capturing a driver.",
        }
    speaker_group_id = str(raw.get("speaker_group_id") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    if not speaker_group_id or not role:
        raise ValueError("speaker_group_id and role are required")

    topology = load_output_topology()
    measurements = load_measurement_state(topology)
    floor_evidence = current_driver_floor_evidence(
        topology,
        measurements,
        speaker_group_id=speaker_group_id,
        role=role,
    )
    if floor_evidence.get("valid") is not True:
        return _refused_capture_sweep(
            str(floor_evidence.get("reason") or "driver_floor_confirmation_invalid"),
            str(
                floor_evidence.get("detail")
                or "confirm this driver again before recording mic evidence"
            ),
        )
    latest = _dict_value(floor_evidence.get("record"))

    from .capture_geometry import driver_level_lock

    comparison_set = _dict_value(measurements.get("active_comparison_set"))
    level_lock = driver_level_lock(comparison_set, speaker_group_id, role)
    if level_lock is None:
        return _refused_capture_sweep(
            "automatic_crossover_driver_level_missing",
            "run the protected level check for this driver before recording it",
        )
    if applied_profile is None:
        from jasper.active_speaker.baseline_profile import (
            load_applied_baseline_profile_state,
        )

        applied_profile = load_applied_baseline_profile_state()
    applied_profile = _dict_value(applied_profile)
    validated_snapshot = validated_applied_measurement_snapshot(
        topology,
        applied_profile,
    )
    resolved_locked_volume = locked_main_volume_db
    if (
        isinstance(resolved_locked_volume, bool)
        or not isinstance(resolved_locked_volume, (int, float))
        or not math.isfinite(float(resolved_locked_volume))
        or float(resolved_locked_volume) > 0.0
    ):
        return _refused_capture_sweep(
            "automatic_crossover_driver_level_invalid",
            "run the protected level check for this driver before recording it",
        )
    planned_excitation = automatic_driver_excitation(
        topology,
        role,
        applied_profile=applied_profile,
        locked_main_volume_db=float(resolved_locked_volume),
    )
    if planned_excitation.get("status") != "ready":
        return _refused_capture_sweep(
            str(
                planned_excitation.get("reason")
                or "automatic_crossover_applied_excitation_unavailable"
            ),
            str(
                planned_excitation.get("detail")
                or "reapply the crossover before measuring"
            ),
        )
    # Four gains have separate owners: the durable by-ear level proves identity
    # only; the level lease owns Camilla main volume; the applied Layer-A
    # snapshot owns this isolated role gain; and excitation.py owns the -12 dBFS
    # ESS source peak. Startup-load authorization is neither an acoustic level
    # nor a role gain: it must stay at calibration_level.py's quiet floor.
    commissioning_gain_db = float(planned_excitation["commissioning_gain_db"])
    startup_gate_level = calibration_level_payload()
    snapshot = _dict_value(validated_snapshot.get("snapshot"))
    preset_raw = snapshot.get("preset")
    if validated_snapshot.get("status") != "ready" or not isinstance(
        preset_raw, dict
    ):
        return _refused_capture_sweep(
            str(
                validated_snapshot.get("reason")
                or "automatic_crossover_applied_excitation_unavailable"
            ),
            str(
                validated_snapshot.get("detail")
                or "reapply the crossover before measuring"
            ),
        )
    preset = ActiveSpeakerPreset.from_mapping(preset_raw)
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.commissioning_admission import (
        ActiveCommissioningAdmissionError,
        ActiveCommissioningPlaybackDrift,
        play_admitted_driver_capture,
    )
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.audio_measurement.admitted_playback import (
        PlaybackAdmissionFailed,
        PlaybackAdmissionRefused,
    )
    from jasper.dsp_apply import DspWriterLockTimeout, dsp_writer_lock

    design_draft = load_design_draft()
    safety_profile = _dict_value(design_draft.get("driver_safety_profile"))
    if not safety_profile:
        return _refused_capture_sweep(
            "active_excitation_profile_not_confirmed",
            "confirm the driver safety profile before recording it",
        )

    load_payload: dict[str, Any] = {}
    rollback: dict[str, Any] | None = None
    rollback_issue: dict[str, str] | None = None
    fanin_gate: dict[str, Any] | None = None
    playback: dict[str, Any]
    try:
        async with dsp_writer_lock(
            DEFAULT_CAMILLA_CONFIG_DIR,
            source="active_speaker_driver_capture",
            timeout_s=3.0,
        ):
            try:
                load_payload = await _load_driver_commissioning_config_for_level(
                    topology=topology,
                    speaker_group_id=speaker_group_id,
                    role=role,
                    level_dbfs=commissioning_gain_db,
                    volume_limit_db=float(resolved_locked_volume),
                    startup_gate_calibration_level=startup_gate_level,
                    preset=preset,
                    crossover_preview=None,
                    camilla_factory=camilla_factory,
                    acquire_lock=False,
                )
                load_state = _dict_value(load_payload.get("load"))
                if load_state.get("status") != "loaded":
                    issues = _dict_items(load_state.get("issues")) or [
                        _issue(
                            "driver_capture_sweep_load_failed",
                            "could not open the confirmed driver path for mic capture",
                        )
                    ]
                    playback = {
                        "status": "blocked",
                        "backend": DRIVER_CAPTURE_SWEEP_BACKEND,
                        "audio_emitted": False,
                        "confirmable": False,
                        "issues": issues,
                        "commissioning_load": load_payload,
                    }
                else:
                    cam = camilla_factory()
                    _load_config, read_running_config, _get_path = commission_seams(cam)

                    async def read_main_volume_db() -> float | None:
                        return await cam.get_volume_db(best_effort=False)

                    def load_current_context():
                        current_topology = load_output_topology()
                        current_draft = load_design_draft()
                        current_measurements = load_measurement_state(current_topology)
                        return (
                            current_topology,
                            _dict_value(
                                current_draft.get("driver_safety_profile")
                            ),
                            _dict_value(
                                current_measurements.get("active_comparison_set")
                            ),
                            _dict_value(load_applied_baseline_profile_state()),
                        )

                    fanin_gate = _commission_tone_select_fanin_lane(
                        fanin_gate_context,
                    )
                    admitted = await play_admitted_driver_capture(
                        topology=topology,
                        safety_profile=safety_profile,
                        comparison_set=comparison_set,
                        applied_profile=applied_profile,
                        speaker_group_id=speaker_group_id,
                        role=role,
                        commissioning_gain_db=commissioning_gain_db,
                        expected_main_volume_db=float(resolved_locked_volume),
                        load_payload=load_payload,
                        read_running_config=read_running_config,
                        read_main_volume_db=read_main_volume_db,
                        load_current_context=load_current_context,
                        alsa_device=COMMISSION_TONE_ALSA_DEVICE,
                        timeout_s=12.0,
                    )
                    sweep_meta = admitted.sweep_meta.to_dict()
                    excitation = _played_excitation_ledger(
                        planned_excitation, sweep_meta
                    )
                    playback = {
                        "status": "completed",
                        "backend": DRIVER_CAPTURE_SWEEP_BACKEND,
                        "playback_id": admitted.handoff.admission_id,
                        "prerequisite_playback_id": str(latest.get("playback_id")),
                        "audio_emitted": True,
                        "confirmable": True,
                        "target": {
                            "speaker_group_id": speaker_group_id,
                            "role": role,
                        },
                        "sweep_meta": sweep_meta,
                        "excitation": excitation,
                        "tone": {"level_dbfs": commissioning_gain_db},
                        "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
                        "commissioning_load": load_payload,
                        "fanin_gate": fanin_gate,
                        "capture_admission": admitted.handoff.to_dict(),
                        "issues": [],
                    }
            except PlaybackAdmissionRefused as exc:
                playback = {
                    "status": "refused",
                    "backend": DRIVER_CAPTURE_SWEEP_BACKEND,
                    "audio_emitted": False,
                    "confirmable": False,
                    "issues": [_issue(
                        "active_driver_playback_readmission_refused",
                        "the live speaker graph changed; start this capture again",
                    )],
                    "refusal_codes": [
                        reason.value for reason in exc.decision.refusal_reasons
                    ],
                }
            except PlaybackAdmissionFailed as exc:
                playback = {
                    "status": "failed",
                    "backend": DRIVER_CAPTURE_SWEEP_BACKEND,
                    "audio_emitted": exc.audio_may_have_started,
                    "audio_may_have_started": exc.audio_may_have_started,
                    "confirmable": False,
                    "issues": [_capture_sweep_issue(exc.failure)],
                    "capture_admission": {
                        "admission_id": exc.admission.generation.admission_id,
                        "playback_artifact": exc.admission.artifact.to_dict(),
                        "requires_new_generation": True,
                    },
                }
            except ActiveCommissioningPlaybackDrift as exc:
                if exc.reason == "main_volume_drift":
                    issue = _issue(
                        "active_driver_capture_volume_drift",
                        "the listening volume changed during the sweep; start it again",
                    )
                else:
                    issue = _issue(
                        "active_driver_capture_post_play_volume_unverified",
                        "the listening volume could not be verified after the sweep; "
                        "start it again",
                    )
                playback = {
                    "status": "failed",
                    "backend": DRIVER_CAPTURE_SWEEP_BACKEND,
                    "audio_emitted": True,
                    "audio_may_have_started": True,
                    "confirmable": False,
                    "post_play_failure_reason": exc.reason,
                    "issues": [issue],
                    "capture_admission": {
                        "admission_id": exc.admission_id,
                        "playback_artifact": exc.playback_artifact.to_dict(),
                        "requires_new_generation": True,
                    },
                }
            except ActiveCommissioningAdmissionError as exc:
                playback = {
                    "status": "refused",
                    "backend": DRIVER_CAPTURE_SWEEP_BACKEND,
                    "audio_emitted": False,
                    "confirmable": False,
                    "issues": [_issue(
                        "active_driver_capture_admission_refused", str(exc)
                    )],
                }
            finally:
                if fanin_gate is not None:
                    _commission_tone_release_fanin_lane(
                        reason="capture_sweep",
                        fanin_gate_context=fanin_gate_context,
                    )
                transaction = _dict_value(
                    load_payload.get("measurement_transaction")
                )
                if transaction.get("entry_config_path"):
                    # Per-attempt teardown re-mutes to the staged anchor ONLY.
                    # Production stays stashed in capture_entry_anchor so an
                    # immediate retry hits the anchor fast path instead of
                    # paying the double config swap that starved the sweep
                    # transport (JTS3 2026-07-16).
                    try:
                        rollback = (
                            await _rollback_capture_attempt_to_anchor_resilient(
                                load_payload,
                                camilla_factory=camilla_factory,
                                acquire_lock=False,
                            )
                        )
                    except AutomaticDriverConfigRestoreError:
                        rollback_issue = _automatic_driver_restore_issue()
    except DspWriterLockTimeout:
        return _refused_capture_sweep(
            "active_driver_capture_writer_busy",
            "another speaker update is in progress; start this capture again",
        )
    if rollback is not None:
        playback["rollback"] = rollback
    if rollback_issue is not None:
        playback["status"] = "failed"
        playback["confirmable"] = False
        playback["issues"] = [rollback_issue, *_dict_items(playback.get("issues"))]
    playback["floor_confirmation"] = latest.get("floor_confirmation")
    first_issue = next(iter(_dict_items(playback.get("issues"))), {})
    result_reason = None
    if playback.get("status") == "blocked":
        result_reason = "driver_capture_sweep_load_failed"
    elif playback.get("status") in ("failed", "refused"):
        result_reason = first_issue.get("code")
    log_event(
        logger,
        "active_speaker.web_driver_capture_sweep",
        status=playback.get("status"),
        reason=result_reason,
        group_id=speaker_group_id,
        role=role,
        audio_emitted=bool(playback.get("audio_emitted")),
        excitation_source=(playback.get("excitation") or {}).get("gain_source"),
        effective_peak_dbfs=(playback.get("excitation") or {}).get(
            "effective_peak_dbfs"
        ),
        floor_evidence_source=floor_evidence.get("source"),
        floor_evidence_playback_id=floor_evidence.get("playback_id"),
    )
    return {
        "status": playback.get("status"),
        "reason": result_reason,
        "audio_emitted": bool(playback.get("audio_emitted")),
        "playback": playback,
        "playback_id": playback.get("playback_id"),
        "test_level_dbfs": commissioning_gain_db,
        "sweep_meta": playback.get("sweep_meta"),
        "excitation": playback.get("excitation"),
        "capture_admission": playback.get("capture_admission"),
        "commissioning_load": playback.get("commissioning_load"),
        "rollback": playback.get("rollback"),
        "issues": _dict_items(playback.get("issues")),
        "commission": commission_status_payload(),
    }


async def play_summed_capture_sweep(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Play the analyzer sweep through one already-tested summed path."""

    if not isinstance(raw, dict):
        raise ValueError("summed capture sweep request must be an object")
    if blocking_phase is not None:
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking_phase,
            "next_step": "Finish the other measurement before capturing the crossover.",
        }
    speaker_group_id = str(raw.get("speaker_group_id") or "").strip()
    if not speaker_group_id:
        raise ValueError("speaker_group_id is required")
    log_event(
        logger,
        "active_speaker.web_summed_capture_sweep",
        status="refused",
        reason="active_summed_persisted_admission_unavailable",
        group_id=speaker_group_id,
        audio_emitted=False,
    )
    return {
        "status": "refused",
        "reason": "active_summed_persisted_admission_unavailable",
        "audio_emitted": False,
        "issues": [_issue(
            "active_summed_persisted_admission_unavailable",
            (
                "combined crossover capture is paused until its multi-driver "
                "protection authority is available"
            ),
        )],
        "commission": commission_status_payload(),
    }

async def _play_summed_commission_tone(
    plan: dict[str, Any],
    *,
    safe_session: dict[str, Any],
    topology: OutputTopology,
    speaker_group_id: str,
    startup_gate_calibration_level: dict[str, Any] | None,
    preset: Any,
    crossover_preview: dict[str, Any] | None,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    from jasper.active_speaker.playback import start_tone_playback

    artifact_playback = start_tone_playback(
        plan,
        safe_session=safe_session,
        backend=None,
        allow_audio=True,
    )
    if artifact_playback.get("status") != "completed":
        return artifact_playback

    tone = _dict_value(artifact_playback.get("tone"))
    level_dbfs = _finite(tone.get("level_dbfs"))
    if level_dbfs is None:
        level_dbfs = -80.0
    try:
        wav_path, stimulus = _combined_speech_stimulus_wav_path()
        duration_s = max(0.05, float(stimulus.get("duration_s") or 0.0))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _summed_playback_with_issue(
            artifact_playback,
            issue=_commission_summed_stimulus_issue(exc),
        )

    load_payload = await _load_summed_commissioning_config(
        topology=topology,
        speaker_group_id=speaker_group_id,
        level_dbfs=level_dbfs,
        startup_gate_calibration_level=startup_gate_calibration_level,
        preset=preset,
        crossover_preview=crossover_preview,
        camilla_factory=camilla_factory,
    )
    load_state = _dict_value(load_payload.get("load"))
    if load_state.get("status") != "loaded":
        load_issues = _dict_items(load_state.get("issues"))
        issue = load_issues[0] if load_issues else _issue(
            "summed_commission_load_failed",
            "could not open the combined active-speaker test path",
        )
        return _summed_playback_with_issue(
            artifact_playback,
            issue=issue,
            commissioning_load=load_payload,
        )

    from jasper.correction.playback import play_sweep

    fanin_gate: dict[str, Any] | None = None
    rollback: dict[str, Any] | None = None
    rollback_issue: dict[str, str] | None = None
    try:
        fanin_gate = _commission_tone_select_fanin_lane()
        # Off-loop playback: ``play_sweep`` runs ``aplay`` via
        # ``asyncio.create_subprocess_exec`` and awaits it, so the shared
        # correction loop stays responsive (status polls, SSE progress, the
        # safe-playback TTL deadman) for the whole stimulus instead of being
        # blocked by a synchronous ``subprocess.run``. Same command/device/
        # WAV pattern as the capture-sweep path above, but a tighter
        # ``duration_s + 1.0`` deadman bound (capture-sweep above uses
        # ``duration_s + 5.0``); ``play_sweep`` raises ``SweepPlaybackError``
        # (a ``RuntimeError``) on non-zero exit or timeout, caught below.
        await play_sweep(
            wav_path,
            alsa_device=COMMISSION_TONE_ALSA_DEVICE,
            timeout_s=duration_s + 1.0,
        )
        playback_result = dict(artifact_playback)
        playback_result.update({
            "status": "completed",
            "backend": SUMMED_COMMISSION_SPEECH_BACKEND,
            "audio_emitted": True,
            "confirmable": True,
            "audio_device": {"pcm": COMMISSION_TONE_ALSA_DEVICE},
            "stimulus": stimulus,
            "commissioning_load": load_payload,
            "fanin_gate": fanin_gate,
            "issues": [],
        })
    except _PLAYBACK_OPERATION_ERRORS as exc:
        playback_result = _summed_playback_with_issue(
            artifact_playback,
            issue=_commission_summed_stimulus_issue(exc),
            commissioning_load=load_payload,
            rollback=rollback,
            fanin_gate=fanin_gate,
        )
    finally:
        if fanin_gate is not None:
            _commission_tone_release_fanin_lane(reason="summed_test")
        try:
            rollback = await _rollback_summed_commissioning_config(
                camilla_factory=camilla_factory,
            )
        except _COMMISSION_OPERATION_ERRORS as exc:
            log_event(
                logger,
                "active_speaker.web_summed_test",
                level=logging.WARNING,
                action="rollback",
                status="failed",
                error=str(exc),
            )
            rollback_issue = _issue(
                "summed_commission_rollback_failed",
                "combined test played, but JTS could not re-mute the active-speaker test path",
            )
    if rollback is not None:
        playback_result["rollback"] = rollback
    if rollback_issue is not None:
        playback_result["status"] = "failed"
        playback_result["confirmable"] = False
        playback_result["issues"] = [
            *_dict_items(playback_result.get("issues")),
            rollback_issue,
        ]
    return playback_result


async def start_summed_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Run and record one bounded combined-driver test."""

    if not isinstance(raw, dict):
        raise ValueError("summed test request must be an object")
    if blocking_phase is not None:
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking_phase,
            "next_step": "Finish the other measurement before testing the crossover.",
        }
    topology = load_output_topology()
    speaker_group_id = str(raw.get("speaker_group_id") or "").strip()
    if not speaker_group_id:
        raise ValueError("speaker_group_id is required")

    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.playback import start_tone_playback

    design_draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=design_draft)
    requested_level = raw.get("level_dbfs", raw.get("requested_level_dbfs"))
    measurements = load_measurement_state(topology)
    persisted_calibration_level = load_calibration_level_state()
    calibration_level = (
        _transient_summed_level(
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
    wants_audio = bool(raw.get("audio", True))
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
            or _crossover_frequency_for_group(preview, speaker_group_id)
        ),
        requested_level_dbfs=calibration_level.get("test_signal", {}).get(
            "requested_level_dbfs"
        ),
        requested_duration_ms=raw.get("duration_ms", 500),
        playback_allowed=(
            wants_audio
            and safe_session.get("status") == "armed"
            and protected_loaded
        ),
        safe_session_id=safe_session.get("session_id"),
        protected_startup_loaded=protected_loaded,
    )
    summary = _dict_value(measurements.get("summary"))
    if not summary.get("driver_measurements_complete"):
        plan = _plan_with_issues(
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
        playback = await _play_summed_commission_tone(
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
    log_event(
        logger,
        "active_speaker.web_summed_test",
        status=playback.get("status"),
        group_id=speaker_group_id,
        audio_requested=wants_audio,
        audio_emitted=bool(playback.get("audio_emitted")),
        blockers=len(playback.get("issues") or []),
    )
    return {
        "status": playback.get("status"),
        "plan": plan,
        "playback": playback,
        "session": session,
        "calibration_level": calibration_level,
        "measurements": measurement_payload,
        "commission": commission_status_payload(),
    }
