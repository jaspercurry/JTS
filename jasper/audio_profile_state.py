"""Read-only audio profile classification.

This module is intentionally small and side-effect-free. It does not
open audio devices, call systemd, write env files, or touch the XVF
chip. Callers pass in the observed facts they already have, and this
module turns them into the shared vocabulary that status surfaces can
show consistently.

Why this exists: `/aec`, `/wake/`, `jasper-doctor`, corpus mode, and
future onboarding all need to distinguish operator intent from runtime
truth. That classification should not live in one HTTP handler.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class AecIntent:
    """Operator-requested AEC state from `/var/lib/jasper/aec_mode.env`."""

    mode: str = "auto"
    raw_enabled: bool = True
    dtln_enabled: bool = False
    chip_aec_enabled: bool = False


@dataclass(frozen=True)
class RuntimeAecEnv:
    """Reconciler-applied runtime env read from `/etc/jasper/jasper.env`."""

    primary_device: str = "Array"
    aec_device: str = "Array"
    chip_primary_leg: str = "chip_aec_150"
    chip_enabled: bool = False
    chip_aec_150_device: str = ""
    chip_aec_210_device: str = ""


@dataclass(frozen=True)
class MicProbe:
    """Cheap, non-streaming mic probe result."""

    xvf_present: bool
    capture_channels: int | None
    recommended_channels: int = 6
    display_name: str = "Seeed ReSpeaker XVF3800 (USB UA)"
    probe_error: str | None = None


def parse_env_bool(raw: str, default: bool = False) -> bool:
    """Normalize the boolean vocabulary used by systemd env files."""

    value = raw.strip().strip("'\"").lower()
    if value in ("1", "true", "on", "yes", "y", "enabled", "enable"):
        return True
    if value in ("0", "false", "off", "no", "n", "disabled", "disable", ""):
        return False
    return default


def env_value(
    env: Mapping[str, str],
    key: str,
    default: str = "",
    *,
    process_env: Mapping[str, str] | None = None,
) -> str:
    """Read a key from a fresh env-file mapping with optional fallback.

    Long-lived daemons like jasper-control should pass a freshly parsed
    `/etc/jasper/jasper.env` mapping first because `os.environ` can be
    stale after the reconciler mutates the env file.
    """

    if key in env:
        return env[key]
    if process_env is not None:
        return process_env.get(key, default)
    return default


def runtime_env_from_mapping(
    env: Mapping[str, str],
    *,
    process_env: Mapping[str, str] | None = None,
) -> RuntimeAecEnv:
    """Build the runtime AEC env snapshot from env-file key/value data."""

    return RuntimeAecEnv(
        primary_device=env_value(env, "JASPER_MIC_DEVICE", "Array", process_env=process_env),
        aec_device=env_value(env, "JASPER_AEC_MIC_DEVICE", "Array", process_env=process_env),
        chip_primary_leg=env_value(
            env,
            "JASPER_AEC_CHIP_AEC_PRIMARY_LEG",
            "chip_aec_150",
            process_env=process_env,
        ),
        chip_enabled=parse_env_bool(
            env_value(env, "JASPER_AEC_CHIP_AEC_ENABLED", "0", process_env=process_env),
            default=False,
        ),
        chip_aec_150_device=env_value(
            env,
            "JASPER_MIC_DEVICE_CHIP_AEC_150",
            "",
            process_env=process_env,
        ),
        chip_aec_210_device=env_value(
            env,
            "JASPER_MIC_DEVICE_CHIP_AEC_210",
            "",
            process_env=process_env,
        ),
    )


def mic_source_label(device: str) -> str:
    if not device:
        return "not configured"
    if device.startswith("udp:"):
        return f"UDP {device[4:]}"
    return device


def _direct_mic_configured(runtime: RuntimeAecEnv) -> bool:
    primary = runtime.primary_device
    return bool(
        primary
        and not primary.startswith("udp:")
        and not (primary == runtime.aec_device == "Array")
    )


def _firmware_status(mic: MicProbe) -> dict[str, Any]:
    if mic.capture_channels is None:
        return {
            "state": "absent",
            "label": "not detected",
            "capture_channels": None,
            "recommended_channels": mic.recommended_channels,
        }
    if mic.capture_channels == mic.recommended_channels:
        return {
            "state": "ok",
            "label": f"{mic.capture_channels}-channel firmware",
            "capture_channels": mic.capture_channels,
            "recommended_channels": mic.recommended_channels,
        }
    return {
        "state": "warn",
        "label": f"{mic.capture_channels}-channel firmware",
        "capture_channels": mic.capture_channels,
        "recommended_channels": mic.recommended_channels,
    }


def build_audio_profile_status(
    intent: AecIntent,
    runtime: RuntimeAecEnv,
    mic: MicProbe,
    *,
    bridge_active: bool,
    chip_available: bool,
) -> dict[str, Any]:
    """Classify intent + observed runtime facts into status payloads.

    The returned `microphone` shape intentionally matches the historical
    `/aec.microphone` JSON object. `audio_profile` is additive and is the
    shared vocabulary future status surfaces should consume.
    """

    direct_mic_configured = _direct_mic_configured(runtime)
    if mic.xvf_present:
        mic_name = mic.display_name
        mic_validation_id = "xvf3800"
    elif direct_mic_configured:
        mic_name = f"Direct mic ({runtime.primary_device})"
        mic_validation_id = f"direct:{runtime.primary_device}"
    elif mic.probe_error:
        mic_name = "Microphone status unavailable"
        mic_validation_id = ""
    else:
        mic_name = "No supported mic detected"
        mic_validation_id = ""

    chip_runtime_active = bool(
        intent.mode == "auto"
        and bridge_active
        and chip_available
        and runtime.chip_enabled
        and runtime.chip_aec_150_device
        and runtime.chip_aec_210_device
    )
    warnings: list[str] = []

    if intent.mode != "auto":
        processing_mode = "Direct mic"
        session_source = mic_source_label(runtime.primary_device)
        wake_legs = ["Direct mic"]
        requested_profile = "direct_mic"
        if mic.xvf_present or direct_mic_configured:
            active_profile = "direct_mic"
            profile_state = "disabled"
            profile_reason = "AEC mode is disabled."
        else:
            active_profile = None
            profile_state = "unavailable"
            profile_reason = "AEC is disabled, but no supported mic is detected."
    elif intent.chip_aec_enabled:
        processing_mode = "Chip-AEC" if chip_runtime_active else "Chip-AEC pending"
        if chip_runtime_active:
            session_source = (
                "Chip AEC 210 beam via :9876"
                if runtime.chip_primary_leg == "chip_aec_210"
                else "Chip AEC 150 beam via :9876"
            )
            profile_state = "active"
            active_profile = "xvf_chip_aec"
            profile_reason = "Chip-AEC runtime env is applied."
        elif not chip_available:
            session_source = "waiting for AEC bridge"
            profile_state = "unavailable"
            active_profile = None
            profile_reason = "Chip-AEC needs the XVF3800 6-channel firmware."
        elif not bridge_active:
            session_source = "waiting for AEC bridge"
            profile_state = "waiting_bridge"
            active_profile = None
            profile_reason = "AEC bridge is not active yet."
        else:
            session_source = "waiting for AEC bridge"
            profile_state = "pending"
            active_profile = None
            profile_reason = "Chip-AEC selected but runtime env is not applied."
        wake_legs = ["Primary chip beam", "Chip AEC 150", "Chip AEC 210"]
        requested_profile = "xvf_chip_aec"
    else:
        processing_mode = "Software AEC3"
        session_source = "WebRTC AEC3 via :9876" if bridge_active else "waiting for AEC bridge"
        wake_legs = ["AEC3"]
        if intent.raw_enabled:
            wake_legs.append("Chip-direct raw")
        if intent.dtln_enabled:
            wake_legs.append("DTLN")
        requested_profile = "xvf_software_aec3"
        active_profile = "xvf_software_aec3" if bridge_active else None
        profile_state = "active" if bridge_active else "waiting_bridge"
        profile_reason = (
            "Software AEC3 bridge is active."
            if bridge_active else "AEC bridge is not active yet."
        )

    if intent.mode == "auto" and not bridge_active:
        warnings.append("AEC bridge is not active yet.")
    if intent.chip_aec_enabled and not chip_available:
        warnings.append("Chip-AEC needs the XVF3800 6-channel firmware.")
    if (
        intent.chip_aec_enabled
        and chip_available
        and bridge_active
        and not chip_runtime_active
    ):
        warnings.append("Chip-AEC is selected but the reconciler has not applied it yet.")
    if not mic.xvf_present and (intent.mode == "auto" or intent.chip_aec_enabled):
        warnings.append("XVF3800 mic is not detected.")
    if intent.mode != "auto" and not (mic.xvf_present or direct_mic_configured):
        warnings.append("No supported mic detected.")
    if mic.probe_error:
        warnings.append(f"Microphone probe failed: {mic.probe_error}")

    return {
        "audio_profile": {
            "requested": requested_profile,
            "active": active_profile,
            "state": profile_state,
            "reason": profile_reason,
        },
        "microphone": {
            "detected": mic.xvf_present or direct_mic_configured,
            "name": mic_name,
            "validation_id": mic_validation_id,
            "primary_device": runtime.primary_device,
            "aec_device": runtime.aec_device,
            "firmware": _firmware_status(mic),
            "processing_mode": processing_mode,
            "session_source": session_source,
            "wake_legs": wake_legs,
            "warnings": warnings,
        },
    }
