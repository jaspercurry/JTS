# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Live audio readiness and evidence."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from jasper.audio_resources import audio_validation_artifacts as artifacts
from jasper.runtime_config.audio_profile_state import (
    AEC_MODE_ENV,
    PROFILE_XVF_CHIP_AEC as CHIP_AEC_PROFILE,
    MicProbe,
    build_audio_profile_status,
    intent_from_env,
    normalize_aec_mode,
    probe_xvf_mic as _probe_xvf_mic,
    runtime_env_from_mapping,
)
from jasper.aec.bridge_telemetry import read_bridge_stats
from jasper.audio_hardware.dac import HIFIBERRY_DAC8X_ID
from jasper.chip_aec.policy import resolve_chip_aec_dac_gate
from jasper.platform.service_units import (
    AEC_BRIDGE_SERVICE,
    CAMILLA_SERVICE,
    FANIN_SERVICE,
    OUTPUTD_SERVICE,
    JASPER_VOICE_SERVICE,
)
from jasper.audio_control.audio_validation_probes import (
    read_mode_env,
    read_system_env,
    _mic_details,
    outputd_socket_path,
    query_outputd_status,
    service_state,
    read_voice_wake_legs,
    _dac_details,
)
from jasper.audio_control.audio_validation_readiness import (
    _check,
    _rollup_status,
    _readiness_recommendation,
    _runtime_identity_check,
    _chip_aec_dac_support_check,
    _runtime_profile_check,
    _runtime_env_check,
    _service_state_check,
    _dac_reference_check,
    _wake_legs_check,
    _bridge_stats_check,
    profile_runtime_ready,
)
from jasper.audio_control.audio_validation_hardware_checks import (
    _dac_identity_check,
    _outputd_pipeline_service_state_check,
    _outputd_dac_status_check,
    _outputd_reference_health_check,
    _bridge_counter_window_check,
    _chip_profile_readback_check,
    _chip_convergence_check,
    _hardware_recommendation,
    _outputd_stability_recommendation,
)


DAC8X_OUTPUTD_STABILITY_PROFILE = "hifiberry_dac8x_outputd_stability"
READINESS_SNAPSHOT_KIND = "readiness_snapshot"
HARDWARE_VALIDATION_KIND = "hardware_validation_passive"
DEFAULT_HARDWARE_OBSERVE_SECONDS = 10.0


def build_chip_aec_readiness_artifact(
    *,
    now: datetime | None = None,
    profile: str = CHIP_AEC_PROFILE,
    system_env: Mapping[str, str] | None = None,
    mode_env: Mapping[str, str] | None = None,
    mic_probe: MicProbe | None = None,
    service_states: Mapping[str, str] | None = None,
    outputd_status: Mapping[str, Any] | None = None,
    bridge_stats: Mapping[str, Any] | None = None,
    voice_wake_legs: set[str] | None = None,
) -> artifacts.ValidationArtifact:
    """Build a bounded schema-v1 chip-AEC readiness snapshot.

    This producer reads runtime state that is already exposed by JTS. It does
    not play audio, capture audio, mutate audio daemons, or persist XVF chip
    settings, so it can only produce readiness evidence. Full validation stays
    a separate operator-controlled hardware run.
    """

    now = datetime.now(timezone.utc) if now is None else now
    mode_env = dict(mode_env) if mode_env is not None else read_mode_env()
    system_env = dict(system_env) if system_env is not None else read_system_env()
    mic_probe = mic_probe or _probe_xvf_mic()
    service_states = (
        dict(service_states)
        if service_states is not None
        else {
            unit: service_state(unit)
            for unit in (
                OUTPUTD_SERVICE,
                AEC_BRIDGE_SERVICE,
                "jasper-aec-init.service",
                JASPER_VOICE_SERVICE,
            )
        }
    )
    if outputd_status is None:
        outputd_status = query_outputd_status(outputd_socket_path(system_env))
    if bridge_stats is None:
        bridge_stats = read_bridge_stats()
    if voice_wake_legs is None:
        voice_wake_legs = read_voice_wake_legs()

    intent = replace(
        intent_from_env(mode_env),
        mode=normalize_aec_mode(mode_env.get(AEC_MODE_ENV, "")),
        profile_selection=mode_env.get("JASPER_AUDIO_INPUT_PROFILE", ""),
    )
    runtime = runtime_env_from_mapping(system_env, process_env=os.environ)
    chip_available = mic_probe.chip_aec_supported
    dac = _dac_details(system_env, outputd_status)
    chip_gate = resolve_chip_aec_dac_gate(dac.get("id"), outputd_status=outputd_status)
    profile_status = build_audio_profile_status(
        intent,
        runtime,
        mic_probe,
        bridge_active=service_states.get(AEC_BRIDGE_SERVICE) == "active",
        chip_available=chip_available,
        chip_gate=chip_gate.to_dict(),
    )
    mic = _mic_details(mic_probe)
    checks = {
        "runtime_identity": _runtime_identity_check(system_env),
        "runtime_profile": _runtime_profile_check(profile_status, profile),
        "mic_detected": _check(
            "pass" if chip_available else "fail",
            summary=(
                "XVF3800 mic profile has a validated chip beam plan."
                if chip_available
                else (
                    "Chip-AEC requires a validated XVF3800 chip beam plan "
                    "for the detected mic geometry."
                )
            ),
            observed=mic,
            expected={
                "family": "xvf3800",
                "chip_beam_plan": "validated",
            },
        ),
        "dac_support": _chip_aec_dac_support_check(dac),
        "runtime_env": _runtime_env_check(runtime),
        "service_state": _service_state_check(service_states),
        "dac_reference": _dac_reference_check(outputd_status),
        "wake_legs": _wake_legs_check(runtime, voice_wake_legs),
        "bridge_counters": _bridge_stats_check(bridge_stats, now),
    }
    status = _rollup_status(checks)
    return artifacts.make_artifact(
        validated_at=now,
        mic_id=str(mic["id"] or "unknown"),
        dac_id=str(dac["id"] or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_readiness_recommendation(checks),
        notes=(
            f"{READINESS_SNAPSHOT_KIND}: runtime readiness only",
            "No playback stimulus was generated.",
            "No capture loop was opened.",
            "No XVF chip settings were written or persisted.",
            "Long-window drift and fixed-delay stability require hardware validation.",
        ),
    )


def build_outputd_stability_hardware_validation_artifact(
    *,
    now: datetime | None = None,
    profile: str = DAC8X_OUTPUTD_STABILITY_PROFILE,
    system_env: Mapping[str, str] | None = None,
    service_states: Mapping[str, str] | None = None,
    outputd_status: Mapping[str, Any] | None = None,
    outputd_status_samples: list[Mapping[str, Any]] | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    report_only: bool = False,
    forced: bool = False,
) -> artifacts.ValidationArtifact:
    """Build a measured outputd/DAC stability artifact.

    This profile intentionally excludes chip-AEC and voice prerequisites so
    DAC/content-loop stability can be validated while chip-AEC is disabled or
    the voice daemon is parked for first-time provider setup.
    """

    now = datetime.now(timezone.utc) if now is None else now
    system_env = dict(system_env) if system_env is not None else read_system_env()
    service_states = (
        dict(service_states)
        if service_states is not None
        else {
            unit: service_state(unit)
            for unit in (
                OUTPUTD_SERVICE,
                CAMILLA_SERVICE,
                FANIN_SERVICE,
            )
        }
    )
    outputd_status_samples = list(outputd_status_samples or [])
    if outputd_status is None and outputd_status_samples:
        outputd_status = outputd_status_samples[0]
    if outputd_status is None:
        outputd_status = query_outputd_status(outputd_socket_path(system_env))

    dac = _dac_details(system_env, outputd_status)
    checks: dict[str, Mapping[str, Any]] = {
        "runtime_identity": _runtime_identity_check(system_env),
        "service_state": _outputd_pipeline_service_state_check(service_states),
        "dac_identity": _dac_identity_check(dac, expected_id=HIFIBERRY_DAC8X_ID),
        "dac_output": _outputd_dac_status_check(outputd_status),
        "outputd_reference_health": _outputd_reference_health_check(
            outputd_status_samples,
            duration_seconds=duration_seconds,
            report_only=report_only,
        ),
        "operator_control": _check(
            "pass",
            required=False,
            summary="Validation was explicitly operator-invoked and bounded.",
            observed={
                "duration_seconds": round(duration_seconds, 3),
                "report_only": report_only,
                "forced": forced,
                "playback_generated": False,
                "capture_loop_opened": False,
                "xvf_reads": False,
                "xvf_persistent_writes": False,
            },
        ),
    }
    status = _rollup_status(checks)
    errors: list[str] = []
    for name, check in checks.items():
        if check.get("status") == "fail":
            errors.append(f"{name}: {check.get('summary', 'failed')}")
    notes = [
        f"{HARDWARE_VALIDATION_KIND}: passive outputd/DAC stability evidence",
        "No playback stimulus was generated.",
        "No capture loop was opened.",
        (
            "Chip-AEC, AEC bridge, XVF readback, and jasper-voice state are "
            "not prerequisites for this profile."
        ),
    ]
    return artifacts.make_artifact(
        validated_at=now,
        mic_id="not_applicable",
        dac_id=str(dac["id"] or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_outputd_stability_recommendation(status, checks),
        notes=tuple(notes),
        errors=tuple(errors),
    )


def build_chip_aec_hardware_validation_artifact(
    *,
    now: datetime | None = None,
    profile: str = CHIP_AEC_PROFILE,
    system_env: Mapping[str, str] | None = None,
    mode_env: Mapping[str, str] | None = None,
    mic_probe: MicProbe | None = None,
    service_states: Mapping[str, str] | None = None,
    outputd_status: Mapping[str, Any] | None = None,
    bridge_stats: Mapping[str, Any] | None = None,
    voice_wake_legs: set[str] | None = None,
    outputd_status_samples: list[Mapping[str, Any]] | None = None,
    bridge_stats_samples: list[Mapping[str, Any]] | None = None,
    chip_readback: Mapping[str, Any] | None = None,
    chip_convergence_polls: list[Mapping[str, Any]] | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    report_only: bool = False,
    forced: bool = False,
    chip_probe_skipped: bool = False,
    chip_probe_skip_reason: str = "",
) -> artifacts.ValidationArtifact:
    """Build a schema-v1 measured chip-AEC validation artifact.

    The default hardware runner is passive: it samples already-running
    outputd/bridge state and read-only XVF parameters. It never generates
    speaker output, opens capture streams, or writes/persists chip settings.
    """

    if profile == DAC8X_OUTPUTD_STABILITY_PROFILE:
        return build_outputd_stability_hardware_validation_artifact(
            now=now,
            profile=profile,
            system_env=system_env,
            service_states=service_states,
            outputd_status=outputd_status,
            outputd_status_samples=outputd_status_samples,
            duration_seconds=duration_seconds,
            report_only=report_only,
            forced=forced,
        )

    now = datetime.now(timezone.utc) if now is None else now
    mode_env = dict(mode_env) if mode_env is not None else read_mode_env()
    system_env = dict(system_env) if system_env is not None else read_system_env()
    outputd_status_samples = list(outputd_status_samples or [])
    bridge_stats_samples = list(bridge_stats_samples or [])
    if outputd_status is None and outputd_status_samples:
        outputd_status = outputd_status_samples[0]
    if bridge_stats is None and bridge_stats_samples:
        bridge_stats = bridge_stats_samples[0]

    readiness = build_chip_aec_readiness_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=outputd_status,
        bridge_stats=bridge_stats,
        voice_wake_legs=voice_wake_legs,
    )
    checks: dict[str, Mapping[str, Any]] = {
        key: value
        for key, value in readiness.checks.items()
        if isinstance(value, Mapping)
    }
    outputd_health = _outputd_reference_health_check(
        outputd_status_samples,
        duration_seconds=duration_seconds,
        report_only=report_only,
    )
    bridge_window = _bridge_counter_window_check(
        bridge_stats_samples,
        duration_seconds=duration_seconds,
        report_only=report_only,
    )
    checks["outputd_reference_health"] = outputd_health
    checks["bridge_counter_window"] = bridge_window
    skip_chip = chip_probe_skipped or not (
        profile_runtime_ready(checks) and outputd_health.get("status") == "pass"
    )
    skip_reason = chip_probe_skip_reason
    if skip_chip and not skip_reason:
        skip_reason = (
            "Chip readback/convergence polling waits for passing runtime "
            "and outputd reference health."
        )
    checks["chip_profile_readback"] = _chip_profile_readback_check(
        chip_readback,
        system_env=system_env,
        skipped=skip_chip,
        skip_reason=skip_reason,
    )
    checks["chip_convergence"] = _chip_convergence_check(
        list(chip_convergence_polls or []),
        skipped=skip_chip,
        skip_reason=skip_reason,
    )
    checks["operator_control"] = _check(
        "pass",
        required=False,
        summary="Validation was explicitly operator-invoked and bounded.",
        observed={
            "duration_seconds": round(duration_seconds, 3),
            "report_only": report_only,
            "forced": forced,
            "playback_generated": False,
            "capture_loop_opened": False,
            "xvf_persistent_writes": False,
        },
    )
    status = _rollup_status(checks)
    dac = _dac_details(system_env, outputd_status)
    notes = (
        f"{HARDWARE_VALIDATION_KIND}: passive operator-controlled hardware evidence",
        "No playback stimulus was generated.",
        "No capture loop was opened.",
        "Only read-only XVF parameters were polled.",
        "No XVF chip settings were written or persisted.",
        (
            "Fixed delay and long-window drift still require an explicit "
            "playback/capture validation mode."
        ),
    )
    errors: list[str] = []
    for name, check in checks.items():
        if check.get("status") == "fail":
            errors.append(f"{name}: {check.get('summary', 'failed')}")
    return artifacts.make_artifact(
        validated_at=now,
        mic_id=readiness.mic_id,
        dac_id=str(dac["id"] or readiness.dac_id or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_hardware_recommendation(checks),
        notes=notes,
        errors=tuple(errors),
    )
