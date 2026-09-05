# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator-controlled audio hardware validation — `jasper-audio-hw-validate`.

Shells out to `jasper.xvf.xvf_host` for chip parameter readback and
convergence polling, and sleeps for a bounded observation window before
building a hardware-validation artifact. Artifact schema, parsing,
freshness checks, and atomic writes stay owned by `jasper.audio_validation`;
this module only produces the evidence a hardware run collects.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..audio_profile_state import probe_xvf_mic
from ..audio_validation import (
    CHIP_AEC_CONVERGENCE_COMMAND,
    CHIP_AEC_PROFILE,
    CHIP_AEC_PROFILE_READBACK_COMMANDS,
    DAC8X_OUTPUTD_STABILITY_PROFILE,
    DEFAULT_HARDWARE_OBSERVE_SECONDS,
    ValidationArtifact,
    artifact_directory,
    build_chip_aec_hardware_validation_artifact,
    build_chip_aec_readiness_artifact,
    build_outputd_stability_hardware_validation_artifact,
    outputd_socket_path,
    profile_runtime_ready,
    query_outputd_status,
    read_bridge_stats,
    read_mode_env,
    read_system_env,
    read_voice_wake_legs,
    service_state,
    write_artifact,
    write_latest_pointer,
)
from ..log_event import log_event

logger = logging.getLogger("jasper.cli.audio_hw_validate")

HARDWARE_VALIDATION_PROFILES = (
    CHIP_AEC_PROFILE,
    DAC8X_OUTPUTD_STABILITY_PROFILE,
)
MAX_SHORT_HARDWARE_OBSERVE_SECONDS = 120.0
LONG_HARDWARE_OBSERVE_SECONDS = 1800.0
MAX_LONG_HARDWARE_OBSERVE_SECONDS = 1800.0
DEFAULT_CHIP_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class HardwareValidationRun:
    """Result from the operator-controlled hardware validation runner."""

    artifact: ValidationArtifact | None
    refused: bool = False
    refusal_reason: str = ""
    path: Path | None = None


def _collect_service_states() -> dict[str, str]:
    return {
        unit: service_state(unit)
        for unit in (
            "jasper-outputd.service",
            "jasper-camilla.service",
            "jasper-fanin.service",
            "jasper-aec-bridge.service",
            "jasper-aec-init.service",
            "jasper-voice.service",
        )
    }


def _parse_xvf_cli_value(stdout: str, command: str) -> list[int | float | str] | None:
    pattern = re.compile(rf"^{re.escape(command)}:\s*\[(?P<body>.*)\]\s*$", re.MULTILINE)
    match = pattern.search(stdout)
    if not match:
        return None
    body = match.group("body").strip()
    if not body:
        return []
    values: list[int | float | str] = []
    for part in body.split(","):
        raw = part.strip().strip("'\"")
        if not raw:
            continue
        try:
            values.append(int(raw, 0))
            continue
        except ValueError:
            pass
        try:
            values.append(float(raw))
            continue
        except ValueError:
            values.append(raw)
    return values


def _read_xvf_parameter(command: str, *, timeout: float = 5.0) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "jasper.xvf.xvf_host", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log_event(
            logger,
            "audio_hw_validation.xvf_read_failed",
            command=command,
            error=str(e),
            level=logging.DEBUG,
        )
        return {"error": str(e)}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {"error": detail or f"xvf_host exited {result.returncode}"}
    values = _parse_xvf_cli_value(result.stdout, command)
    if values is None:
        return {"error": "xvf_host output did not include a parseable value"}
    return {command: values}


def _read_chip_profile_parameters(*, timeout: float = 5.0) -> dict[str, Any]:
    readback: dict[str, Any] = {}
    for command in CHIP_AEC_PROFILE_READBACK_COMMANDS:
        result = _read_xvf_parameter(command, timeout=timeout)
        if "error" in result:
            readback[command] = {"error": result["error"]}
        else:
            readback[command] = result.get(command, [])
    return readback


def _poll_chip_convergence(
    *,
    duration_seconds: float,
    interval_seconds: float,
    timeout: float = 5.0,
) -> list[Mapping[str, Any]]:
    polls: list[Mapping[str, Any]] = []
    deadline = time.monotonic() + max(0.0, duration_seconds)
    while True:
        result = _read_xvf_parameter(CHIP_AEC_CONVERGENCE_COMMAND, timeout=timeout)
        polls.append(result)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.1, interval_seconds), remaining))
    return polls


def _duration_limit(duration_seconds: float, *, allow_long: bool) -> float:
    limit = (
        MAX_LONG_HARDWARE_OBSERVE_SECONDS
        if allow_long else MAX_SHORT_HARDWARE_OBSERVE_SECONDS
    )
    if duration_seconds < 0:
        raise ValueError("duration must be non-negative")
    if duration_seconds > limit:
        mode = "--allow-long or --long-window" if not allow_long else "a shorter duration"
        raise ValueError(
            f"duration {duration_seconds:g}s exceeds the {limit:g}s bound; use {mode}"
        )
    return duration_seconds


def _chip_runtime_refusal_reason(artifact: ValidationArtifact) -> str:
    checks = artifact.checks
    runtime_profile = checks.get("runtime_profile")
    if isinstance(runtime_profile, Mapping) and runtime_profile.get("status") != "pass":
        return str(runtime_profile.get("summary") or "Chip-AEC profile is not active.")
    runtime_env = checks.get("runtime_env")
    if isinstance(runtime_env, Mapping) and runtime_env.get("status") != "pass":
        return str(runtime_env.get("summary") or "Chip-AEC runtime env is incomplete.")
    return ""


def _complete_hardware_validation_result(
    artifact: ValidationArtifact,
    *,
    directory: Path | None,
    report_only: bool,
    stdout: bool,
) -> HardwareValidationRun:
    if stdout:
        json.dump(artifact.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    if report_only:
        log_event(
            logger,
            "audio_hw_validation.report",
            profile=artifact.profile,
            status=artifact.status,
            recommendation=artifact.recommendation,
        )
        return HardwareValidationRun(artifact=artifact)

    target_dir = directory or artifact_directory()
    try:
        path = write_artifact(artifact, directory=target_dir)
        latest_path = write_latest_pointer(artifact, directory=target_dir)
    except OSError as e:
        log_event(
            logger,
            "audio_hw_validation.write_failed",
            profile=artifact.profile,
            status=artifact.status,
            error=str(e),
            level=logging.ERROR,
        )
        return HardwareValidationRun(artifact=artifact, refused=True, refusal_reason=str(e))
    log_event(
        logger,
        "audio_hw_validation.write",
        profile=artifact.profile,
        status=artifact.status,
        recommendation=artifact.recommendation,
        path=path,
        latest=latest_path,
    )
    return HardwareValidationRun(artifact=artifact, path=path)


def run_audio_hardware_validation(
    *,
    profile: str = CHIP_AEC_PROFILE,
    directory: Path | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    poll_interval_seconds: float = DEFAULT_CHIP_POLL_INTERVAL_SECONDS,
    report_only: bool = False,
    force: bool = False,
    allow_long: bool = False,
    stdout: bool = False,
    now: datetime | None = None,
) -> HardwareValidationRun:
    """Run a bounded operator-controlled audio hardware validator."""

    now = datetime.now(timezone.utc) if now is None else now
    duration_seconds = _duration_limit(
        0.0 if report_only else duration_seconds,
        allow_long=allow_long,
    )
    if poll_interval_seconds <= 0:
        raise ValueError("poll interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log_event(
        logger,
        "audio_hw_validation.start",
        profile=profile,
        duration_seconds=f"{duration_seconds:.3f}",
        report_only=int(report_only),
        force=int(force),
    )

    system_env = read_system_env()
    if profile == DAC8X_OUTPUTD_STABILITY_PROFILE:
        service_states = _collect_service_states()
        outputd_socket = outputd_socket_path(system_env)
        first_outputd = query_outputd_status(outputd_socket)
        outputd_samples: list[Mapping[str, Any]] = []
        if isinstance(first_outputd, Mapping):
            outputd_samples.append(first_outputd)
        if not report_only and duration_seconds > 0:
            time.sleep(duration_seconds)
            final_outputd = query_outputd_status(outputd_socket)
            if isinstance(final_outputd, Mapping):
                outputd_samples.append(final_outputd)
        artifact = build_outputd_stability_hardware_validation_artifact(
            now=now,
            profile=profile,
            system_env=system_env,
            service_states=service_states,
            outputd_status=first_outputd,
            outputd_status_samples=outputd_samples,
            duration_seconds=duration_seconds,
            report_only=report_only,
            forced=force,
        )
        return _complete_hardware_validation_result(
            artifact,
            directory=directory,
            report_only=report_only,
            stdout=stdout,
        )

    mode_env = read_mode_env()
    mic_probe = probe_xvf_mic()
    service_states = _collect_service_states()
    outputd_socket = outputd_socket_path(system_env)
    first_outputd = query_outputd_status(outputd_socket)
    first_bridge = read_bridge_stats()
    voice_wake_legs = read_voice_wake_legs()

    readiness = build_chip_aec_readiness_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=first_outputd,
        bridge_stats=first_bridge,
        voice_wake_legs=voice_wake_legs,
    )
    refusal_reason = _chip_runtime_refusal_reason(readiness)
    if refusal_reason and not force:
        log_event(
            logger,
            "audio_hw_validation.refused",
            profile=profile,
            reason=refusal_reason,
            level=logging.WARNING,
        )
        return HardwareValidationRun(
            artifact=None,
            refused=True,
            refusal_reason=refusal_reason,
        )

    outputd_samples = []
    bridge_samples: list[Mapping[str, Any]] = []
    if isinstance(first_outputd, Mapping):
        outputd_samples.append(first_outputd)
    if isinstance(first_bridge, Mapping):
        bridge_samples.append(first_bridge)
    preflight_seconds = 0.0
    remaining_seconds = 0.0
    if not report_only and duration_seconds > 0:
        preflight_seconds = min(1.0, duration_seconds)
        remaining_seconds = max(0.0, duration_seconds - preflight_seconds)
        time.sleep(preflight_seconds)
        preflight_outputd = query_outputd_status(outputd_socket)
        if isinstance(preflight_outputd, Mapping):
            outputd_samples.append(preflight_outputd)
        preflight_bridge = read_bridge_stats()
        if isinstance(preflight_bridge, Mapping):
            bridge_samples.append(preflight_bridge)
    probe_gate = build_chip_aec_hardware_validation_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=first_outputd,
        bridge_stats=first_bridge,
        voice_wake_legs=voice_wake_legs,
        outputd_status_samples=outputd_samples,
        bridge_stats_samples=bridge_samples,
        duration_seconds=duration_seconds,
        report_only=report_only,
        forced=force,
        chip_probe_skipped=True,
        chip_probe_skip_reason="probe gate evaluation",
    )
    outputd_health = probe_gate.checks.get("outputd_reference_health")
    chip_probe_allowed = (
        not report_only
        and profile_runtime_ready(probe_gate.checks)
        and isinstance(outputd_health, Mapping)
        and outputd_health.get("status") == "pass"
    )
    chip_readback: Mapping[str, Any] | None = None
    chip_polls: list[Mapping[str, Any]] = []
    skip_reason = ""
    if chip_probe_allowed:
        log_event(logger, "audio_hw_validation.chip_probe_start", profile=profile)
        chip_readback = _read_chip_profile_parameters()
        chip_polls = _poll_chip_convergence(
            duration_seconds=remaining_seconds,
            interval_seconds=poll_interval_seconds,
        )
        log_event(
            logger,
            "audio_hw_validation.chip_probe_complete",
            profile=profile,
            polls=len(chip_polls),
        )
    else:
        skip_reason = (
            "Chip readback/convergence polling waits for report_only=0, "
            "passing runtime checks, and passing outputd reference health."
        )
        if remaining_seconds > 0:
            time.sleep(remaining_seconds)
    if not report_only and duration_seconds > preflight_seconds:
        final_outputd = query_outputd_status(outputd_socket)
        if isinstance(final_outputd, Mapping):
            outputd_samples.append(final_outputd)
        final_bridge = read_bridge_stats()
        if isinstance(final_bridge, Mapping):
            bridge_samples.append(final_bridge)

    artifact = build_chip_aec_hardware_validation_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=first_outputd,
        bridge_stats=first_bridge,
        voice_wake_legs=voice_wake_legs,
        outputd_status_samples=outputd_samples,
        bridge_stats_samples=bridge_samples,
        chip_readback=chip_readback,
        chip_convergence_polls=chip_polls,
        duration_seconds=duration_seconds,
        report_only=report_only,
        forced=force,
        chip_probe_skipped=not chip_probe_allowed,
        chip_probe_skip_reason=skip_reason,
    )
    return _complete_hardware_validation_result(
        artifact,
        directory=directory,
        report_only=report_only,
        stdout=stdout,
    )


def hardware_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run bounded operator-controlled audio hardware validation.",
    )
    parser.add_argument(
        "--profile",
        default=CHIP_AEC_PROFILE,
        choices=HARDWARE_VALIDATION_PROFILES,
        help="Audio profile to validate.",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Artifact directory (default: /var/lib/jasper/audio-validation).",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=DEFAULT_HARDWARE_OBSERVE_SECONDS,
        help=(
            "Passive outputd observation window (plus bridge counters for "
            "chip-AEC), not a hard total wall-clock cap because bounded "
            "XVF readback/poll subprocesses may add time for chip-AEC "
            f"(default: {DEFAULT_HARDWARE_OBSERVE_SECONDS:g}s; "
            f"max without --allow-long: {MAX_SHORT_HARDWARE_OBSERVE_SECONDS:g}s)."
        ),
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_CHIP_POLL_INTERVAL_SECONDS,
        help=(
            "Read-only chip convergence poll interval "
            f"(default: {DEFAULT_CHIP_POLL_INTERVAL_SECONDS:g}s)."
        ),
    )
    parser.add_argument(
        "--long-window",
        action="store_true",
        help=(
            "Use the explicit 30-minute passive observation window. "
            "This does not generate playback."
        ),
    )
    parser.add_argument(
        "--allow-long",
        action="store_true",
        help=(
            "Allow the passive observation window above the default short "
            "bound, up to 30 minutes."
        ),
    )
    parser.add_argument(
        "--dry-run",
        "--report-only",
        dest="report_only",
        action="store_true",
        help="Collect a report without sleeping for the validation window or writing artifacts.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="For chip-AEC, write/report an artifact even when chip-AEC is not requested and active.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the full artifact JSON to stdout.",
    )
    args = parser.parse_args(argv)
    duration = (
        LONG_HARDWARE_OBSERVE_SECONDS
        if args.long_window else args.duration_seconds
    )
    allow_long = args.allow_long or args.long_window
    try:
        result = run_audio_hardware_validation(
            profile=args.profile,
            directory=args.directory,
            duration_seconds=duration,
            poll_interval_seconds=args.poll_interval_seconds,
            report_only=args.report_only,
            force=args.force,
            allow_long=allow_long,
            stdout=args.stdout or args.report_only,
        )
    except ValueError as e:
        parser.error(str(e))
    if result.refused:
        return 2
    if result.artifact is None:
        return 2
    return 0 if result.artifact.status != "fail" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(hardware_main())
