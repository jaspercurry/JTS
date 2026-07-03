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
from pathlib import Path
from typing import Any, Callable

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
from jasper.active_speaker.measurement import (
    confirmed_driver_roles,
    load_measurement_state,
    record_driver_measurement,
    record_summed_test_artifact,
)
from jasper.active_speaker.safe_playback import (
    arm_safe_playback_session,
    load_safe_playback_state,
    record_safe_playback_result,
)
from jasper.active_speaker.staging import (
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
DEFAULT_MEASUREMENT_SWEEP_DIR = Path("/var/lib/jasper/active_speaker_sweeps")
MEASUREMENT_SWEEP_DIR_ENV = "JASPER_ACTIVE_SPEAKER_SWEEP_DIR"
COMMISSION_TONE_MUX_SOCKET = os.environ.get(
    "JASPER_MUX_CONTROL_SOCKET", "/run/jasper-mux/control.sock",
)
COMMISSION_TONE_FANIN_LABEL = "correction"
_COMMISSION_TONE_LOCK = threading.Lock()
_COMMISSION_TONE_SESSION: dict[str, Any] | None = None

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
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
)
_PLAYBACK_OPERATION_ERRORS = (
    OSError,
    RuntimeError,
    subprocess.TimeoutExpired,
)
_COMMISSION_START_ERRORS = _COMMISSION_OPERATION_ERRORS + _MUX_COMMAND_ERRORS

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


def _stage_startup_config(topology: OutputTopology) -> dict[str, Any]:
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    design_draft = load_design_draft()
    crossover_preview = load_crossover_preview(current_design_draft=design_draft)
    return stage_protected_startup_config(
        topology,
        crossover_preview=crossover_preview,
    )


async def _load_startup_config(
    camilla_factory: CamillaFactory,
    *,
    path_safety_evidence_path: str | Path | None = None,
) -> dict[str, Any]:
    topology = load_output_topology()
    cam = camilla_factory()
    return await load_protected_startup_config(
        topology,
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
        path_safety_evidence_path=path_safety_evidence_path
        or _path_safety_evidence_path(),
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
) -> dict[str, Any]:
    """Ensure commissioning has the silent startup graph as rollback anchor."""

    staged_path = (staged_config.get("config") or {}).get("path")
    if _config_paths_match(current_config_path, staged_path):
        return {"status": "already_loaded", "staged_config_path": staged_path}

    topology, _guards_changed = request_missing_software_guards(load_output_topology())
    stage = _stage_startup_config(topology)
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


def _measurement_sweep_wav_path() -> tuple[Path, dict[str, Any]]:
    """Return the cached swept-sine WAV + metadata used by acoustic capture."""

    from jasper.active_speaker import driver_acoustics as acoustic
    from jasper.audio_measurement import sweep as sweep_mod

    cache_dir = Path(
        os.environ.get(MEASUREMENT_SWEEP_DIR_ENV) or DEFAULT_MEASUREMENT_SWEEP_DIR
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    signal, meta = sweep_mod.synchronized_swept_sine(
        f1=acoustic.DEFAULT_F1_HZ,
        f2=acoustic.DEFAULT_F2_HZ,
        duration_approx_s=acoustic.DEFAULT_DURATION_S,
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


def _commission_tone_select_fanin_lane() -> dict[str, Any]:
    return _commission_tone_mux_command(
        f"TEST_SELECT {COMMISSION_TONE_FANIN_LABEL}",
    )


def _commission_tone_release_fanin_lane(*, reason: str) -> dict[str, Any]:
    try:
        payload = _commission_tone_mux_command("TEST_RELEASE")
    except _MUX_COMMAND_ERRORS as exc:
        log_event(
            logger,
            "active_speaker.web_commission_tone",
            level=logging.WARNING,
            action="fanin_release",
            reason=reason,
            status="failed",
            error=str(exc),
        )
        return {"status": "failed", "reason": reason, "error": str(exc)}
    log_event(
        logger,
        "active_speaker.web_commission_tone",
        action="fanin_release",
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
    staged = load_staged_startup_config()
    current_config_path, current_config_error = await read_current_config_path(cam)
    startup_setup = await _ensure_commission_startup_anchor(
        group=group,
        role=role,
        staged_config=staged,
        current_config_path=current_config_path,
        camilla_factory=camilla_factory,
    )
    if startup_setup.get("status") == "blocked":
        return startup_setup

    staged = load_staged_startup_config()
    preset, crossover_preview = resolve_commission_inputs()
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
) -> dict[str, Any]:
    cam = camilla_factory()
    return await rollback_driver_commissioning_config(
        load_config=commission_load_config(cam)
    )


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


def _latest_driver_measurement(
    measurements: dict[str, Any],
    *,
    speaker_group_id: str,
    role: str,
) -> dict[str, Any] | None:
    summary = _dict_value(measurements.get("summary"))
    latest = _dict_value(summary.get("latest_driver_measurements"))
    record = latest.get(f"{speaker_group_id}:{role}")
    return record if isinstance(record, dict) else None


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


async def _load_driver_commissioning_config_for_level(
    *,
    topology: OutputTopology,
    speaker_group_id: str,
    role: str,
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
        role=role,
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
        path_safety_evidence_path=evidence_path,
    )
    payload["startup_setup"] = startup_setup
    return payload


async def _play_capture_sweep(
    *,
    backend: str,
    target: dict[str, Any],
    playback_id: str,
    level_dbfs: float,
    load_payload: dict[str, Any],
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    from jasper.correction.playback import play_sweep

    sweep_meta: dict[str, Any] = {}
    fanin_gate: dict[str, Any] | None = None
    rollback: dict[str, Any] | None = None
    rollback_issue: dict[str, str] | None = None
    try:
        wav_path, sweep_meta = _measurement_sweep_wav_path()
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
            rollback = await _rollback_summed_commissioning_config(
                camilla_factory=camilla_factory,
            )
        except _COMMISSION_OPERATION_ERRORS as exc:
            log_event(
                logger,
                "active_speaker.web_capture_sweep",
                level=logging.WARNING,
                action="rollback",
                status="failed",
                error=str(exc),
            )
            rollback_issue = _issue(
                "capture_sweep_rollback_failed",
                "measurement sweep played, but JTS could not re-mute the active-speaker test path",
            )
    if rollback is not None:
        payload["rollback"] = rollback
    if rollback_issue is not None:
        payload["status"] = "failed"
        payload["confirmable"] = False
        payload["issues"] = [*_dict_items(payload.get("issues")), rollback_issue]
    return payload


async def play_driver_capture_sweep(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Play the analyzer sweep through one already-confirmed driver path."""

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
    latest = _latest_driver_measurement(
        measurements,
        speaker_group_id=speaker_group_id,
        role=role,
    )
    if (
        latest is None
        or latest.get("captured") is not True
        or not latest.get("playback_id")
        or _has_blocker(latest)
    ):
        return _refused_capture_sweep(
            "driver_floor_confirmation_required",
            "confirm this driver by ear before recording mic evidence",
        )
    safe_session = load_safe_playback_state()
    if safe_session.get("status") != "armed":
        return _refused_capture_sweep(
            "driver_floor_confirmation_expired",
            "the driver confirmation expired; play and confirm the driver again",
        )

    level = _finite(latest.get("test_level_dbfs"))
    if level is None:
        level = clamp_test_level_dbfs(None)
    startup_gate_level = calibration_level_payload(requested_level_dbfs=level)
    preset, crossover_preview = resolve_commission_inputs()
    load_payload = await _load_driver_commissioning_config_for_level(
        topology=topology,
        speaker_group_id=speaker_group_id,
        role=role,
        level_dbfs=level,
        startup_gate_calibration_level=startup_gate_level,
        preset=preset,
        crossover_preview=crossover_preview,
        camilla_factory=camilla_factory,
    )
    load_state = _dict_value(load_payload.get("load"))
    if load_state.get("status") != "loaded":
        load_issues = _dict_items(load_state.get("issues"))
        return {
            "status": "blocked",
            "reason": "driver_capture_sweep_load_failed",
            "audio_emitted": False,
            "commissioning_load": load_payload,
            "issues": load_issues or [
                _issue(
                    "driver_capture_sweep_load_failed",
                    "could not open the confirmed driver path for mic capture",
                )
            ],
            "commission": commission_status_payload(),
        }

    playback = await _play_capture_sweep(
        backend=DRIVER_CAPTURE_SWEEP_BACKEND,
        target={"speaker_group_id": speaker_group_id, "role": role},
        playback_id=str(latest.get("playback_id")),
        level_dbfs=level,
        load_payload=load_payload,
        camilla_factory=camilla_factory,
    )
    playback["floor_confirmation"] = latest.get("floor_confirmation")
    log_event(
        logger,
        "active_speaker.web_driver_capture_sweep",
        status=playback.get("status"),
        group_id=speaker_group_id,
        role=role,
        audio_emitted=bool(playback.get("audio_emitted")),
    )
    return {
        "status": playback.get("status"),
        "playback": playback,
        "playback_id": playback.get("playback_id"),
        "test_level_dbfs": level,
        "sweep_meta": playback.get("sweep_meta"),
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

    topology = load_output_topology()
    measurements = load_measurement_state(topology)
    latest = _latest_summed_test(measurements, speaker_group_id=speaker_group_id)
    summed_test_id = (
        latest.get("summed_test_id") or latest.get("playback_id")
        if isinstance(latest, dict)
        else None
    )
    if (
        latest is None
        or latest.get("captured") is not True
        or latest.get("audio_emitted") is not True
        or not summed_test_id
        or _has_blocker(latest)
    ):
        return _refused_capture_sweep(
            "summed_playback_required",
            "play the combined crossover test before recording mic evidence",
        )

    safe_session = load_safe_playback_state()
    if safe_session.get("status") != "armed":
        arm_safe_playback_session(_SUMMED_TEST_ARM_REPORT)

    tone = _dict_value(latest.get("tone"))
    level = _finite(tone.get("level_dbfs"))
    if level is None:
        level = clamp_test_level_dbfs(None)
    startup_gate_level = calibration_level_payload(requested_level_dbfs=level)
    preset, crossover_preview = resolve_commission_inputs()
    load_payload = await _load_summed_commissioning_config(
        topology=topology,
        speaker_group_id=speaker_group_id,
        level_dbfs=level,
        startup_gate_calibration_level=startup_gate_level,
        preset=preset,
        crossover_preview=crossover_preview,
        camilla_factory=camilla_factory,
    )
    load_state = _dict_value(load_payload.get("load"))
    if load_state.get("status") != "loaded":
        load_issues = _dict_items(load_state.get("issues"))
        return {
            "status": "blocked",
            "reason": "summed_capture_sweep_load_failed",
            "audio_emitted": False,
            "commissioning_load": load_payload,
            "issues": load_issues or [
                _issue(
                    "summed_capture_sweep_load_failed",
                    "could not open the combined crossover path for mic capture",
                )
            ],
            "commission": commission_status_payload(),
        }

    playback = await _play_capture_sweep(
        backend=SUMMED_CAPTURE_SWEEP_BACKEND,
        target={"speaker_group_id": speaker_group_id, "role": "summed"},
        playback_id=str(summed_test_id),
        level_dbfs=level,
        load_payload=load_payload,
        camilla_factory=camilla_factory,
    )
    playback["summed_test_id"] = str(summed_test_id)
    log_event(
        logger,
        "active_speaker.web_summed_capture_sweep",
        status=playback.get("status"),
        group_id=speaker_group_id,
        audio_emitted=bool(playback.get("audio_emitted")),
    )
    return {
        "status": playback.get("status"),
        "playback": playback,
        "playback_id": playback.get("playback_id"),
        "summed_test_id": str(summed_test_id),
        "test_level_dbfs": level,
        "sweep_meta": playback.get("sweep_meta"),
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
        # blocked by a synchronous ``subprocess.run``. Matches the capture-sweep
        # path above. Same command/device/WAV and the same ``duration_s + 1.0``
        # deadman bound; ``play_sweep`` raises ``SweepPlaybackError``
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
