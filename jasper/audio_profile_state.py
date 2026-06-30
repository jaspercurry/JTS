# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

from .chip_aec_policy import STATUS_TESTING


PROFILE_AUTO = "auto"
PROFILE_XVF_CHIP_AEC = "xvf_chip_aec"
PROFILE_XVF_CHIP_AEC_TESTING = "xvf_chip_aec_testing"
PROFILE_XVF_SOFTWARE_AEC3 = "xvf_software_aec3"
PROFILE_DIRECT_MIC = "direct_mic"
PROFILE_CUSTOM = "custom"

CONCRETE_PROFILES = (
    PROFILE_XVF_CHIP_AEC,
    PROFILE_XVF_CHIP_AEC_TESTING,
    PROFILE_XVF_SOFTWARE_AEC3,
    PROFILE_DIRECT_MIC,
)
SELECTABLE_PROFILES = (
    PROFILE_AUTO,
    PROFILE_XVF_CHIP_AEC,
    PROFILE_XVF_CHIP_AEC_TESTING,
    PROFILE_XVF_SOFTWARE_AEC3,
    PROFILE_DIRECT_MIC,
)
ALL_PROFILES = SELECTABLE_PROFILES + (PROFILE_CUSTOM,)


@dataclass(frozen=True)
class AecIntent:
    """Operator-requested AEC state from `/var/lib/jasper/aec_mode.env`."""

    mode: str = "auto"
    raw_enabled: bool = True
    dtln_enabled: bool = False
    chip_aec_enabled: bool = False
    chip_aec_150_enabled: bool = False
    chip_aec_210_enabled: bool = False
    profile_selection: str = ""


@dataclass(frozen=True)
class RuntimeAecEnv:
    """Reconciler-applied runtime env read from `/etc/jasper/jasper.env`."""

    primary_device: str = "Array"
    aec_device: str = "Array"
    mic_variant: str = ""
    mic_geometry: str = ""
    mic_display_name: str = ""
    chip_beam_plan: str = ""
    chip_primary_leg: str = "chip_aec_150"
    chip_enabled: bool = False
    raw_device: str = ""
    dtln_device: str = ""
    dtln_enabled: bool = False
    chip_aec_150_device: str = ""
    chip_aec_210_device: str = ""
    chip_aec_gate_dac_id: str = ""
    chip_aec_gate_status: str = ""
    chip_aec_gate_source: str = ""
    chip_aec_gate_detail: str = ""


@dataclass(frozen=True)
class MicProbe:
    """Cheap, non-streaming mic probe result."""

    xvf_present: bool
    capture_channels: int | None
    recommended_channels: int = 6
    display_name: str = "Seeed ReSpeaker XVF3800 (USB UA)"
    alsa_card_name: str = ""
    variant_id: str = ""
    geometry: str = ""
    chip_beam_plan: str = ""
    probe_error: str | None = None


def parse_env_bool(raw: str, default: bool = False) -> bool:
    """Normalize the boolean vocabulary used by systemd env files."""

    value = raw.strip().strip("'\"").lower()
    if value in ("1", "true", "on", "yes", "y", "enabled", "enable"):
        return True
    if value in ("0", "false", "off", "no", "n", "disabled", "disable", ""):
        return False
    return default


def normalize_audio_input_profile(raw: str, default: str = PROFILE_CUSTOM) -> str:
    """Normalize the operator-facing audio input profile id."""

    value = raw.strip().strip("'\"").lower().replace("-", "_")
    aliases = {
        "": default,
        "automatic": PROFILE_AUTO,
        "xvf_chip": PROFILE_XVF_CHIP_AEC,
        "chip_aec": PROFILE_XVF_CHIP_AEC,
        "chip": PROFILE_XVF_CHIP_AEC,
        "hardware_aec": PROFILE_XVF_CHIP_AEC,
        "xvf_chip_aec_trial": PROFILE_XVF_CHIP_AEC_TESTING,
        "xvf_chip_aec_test": PROFILE_XVF_CHIP_AEC_TESTING,
        "chip_aec_testing": PROFILE_XVF_CHIP_AEC_TESTING,
        "chip_aec_trial": PROFILE_XVF_CHIP_AEC_TESTING,
        "hardware_aec_testing": PROFILE_XVF_CHIP_AEC_TESTING,
        "xvf_software": PROFILE_XVF_SOFTWARE_AEC3,
        "software_aec3": PROFILE_XVF_SOFTWARE_AEC3,
        "aec3": PROFILE_XVF_SOFTWARE_AEC3,
        "software": PROFILE_XVF_SOFTWARE_AEC3,
        "raw": PROFILE_DIRECT_MIC,
        "direct": PROFILE_DIRECT_MIC,
        "off": PROFILE_DIRECT_MIC,
        "disabled": PROFILE_DIRECT_MIC,
        "manual": PROFILE_CUSTOM,
    }
    value = aliases.get(value, value)
    return value if value in ALL_PROFILES else default


def infer_audio_input_profile(intent: AecIntent) -> str:
    """Infer the closest profile for pre-profile aec_mode.env files."""

    mode = (intent.mode or "auto").strip().strip("'\"").lower()
    if mode != "auto":
        return PROFILE_DIRECT_MIC
    if intent.chip_aec_150_enabled or intent.chip_aec_210_enabled:
        return PROFILE_CUSTOM
    if intent.chip_aec_enabled:
        return PROFILE_XVF_CHIP_AEC
    if intent.raw_enabled and not intent.dtln_enabled:
        return PROFILE_XVF_SOFTWARE_AEC3
    return PROFILE_CUSTOM


def validation_profile(profile: str | None) -> str | None:
    """Map UI/testing profiles to their physical validation artifact key."""

    normalized = normalize_audio_input_profile(profile or "", default=PROFILE_CUSTOM)
    if normalized == PROFILE_XVF_CHIP_AEC_TESTING:
        return PROFILE_XVF_CHIP_AEC
    if normalized == PROFILE_CUSTOM:
        return None
    return normalized


def profile_env_updates(profile: str) -> dict[str, str]:
    """Legacy-compatible env updates for an explicit profile write.

    The reconciler understands `JASPER_AUDIO_INPUT_PROFILE`, but these
    updates keep rollback behavior unsurprising: an older daemon that
    ignores the profile key still lands on the nearest safe legacy
    AEC/leg configuration.
    """

    normalized = normalize_audio_input_profile(profile, default=PROFILE_CUSTOM)
    updates = {"JASPER_AUDIO_INPUT_PROFILE": normalized}
    if normalized == PROFILE_AUTO:
        updates.update({
            "JASPER_AEC_MODE": "auto",
            "JASPER_WAKE_LEG_RAW": "1",
            "JASPER_WAKE_LEG_DTLN": "0",
            "JASPER_WAKE_LEG_CHIP_AEC": "0",
            "JASPER_WAKE_LEG_CHIP_AEC_150": "0",
            "JASPER_WAKE_LEG_CHIP_AEC_210": "0",
        })
    elif normalized in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}:
        updates.update({
            "JASPER_AEC_MODE": "auto",
            "JASPER_WAKE_LEG_RAW": "0",
            "JASPER_WAKE_LEG_DTLN": "0",
            "JASPER_WAKE_LEG_CHIP_AEC": "1",
            "JASPER_WAKE_LEG_CHIP_AEC_150": "0",
            "JASPER_WAKE_LEG_CHIP_AEC_210": "0",
        })
    elif normalized == PROFILE_XVF_SOFTWARE_AEC3:
        updates.update({
            "JASPER_AEC_MODE": "auto",
            "JASPER_WAKE_LEG_RAW": "1",
            "JASPER_WAKE_LEG_DTLN": "0",
            "JASPER_WAKE_LEG_CHIP_AEC": "0",
            "JASPER_WAKE_LEG_CHIP_AEC_150": "0",
            "JASPER_WAKE_LEG_CHIP_AEC_210": "0",
        })
    elif normalized == PROFILE_DIRECT_MIC:
        updates.update({
            "JASPER_AEC_MODE": "disabled",
            "JASPER_WAKE_LEG_RAW": "0",
            "JASPER_WAKE_LEG_DTLN": "0",
            "JASPER_WAKE_LEG_CHIP_AEC": "0",
            "JASPER_WAKE_LEG_CHIP_AEC_150": "0",
            "JASPER_WAKE_LEG_CHIP_AEC_210": "0",
        })
    return updates


def resolve_audio_input_intent(
    intent: AecIntent,
    *,
    chip_available: bool,
) -> AecIntent:
    """Resolve selected profile into the concrete AEC/leg intent."""

    selection = normalize_audio_input_profile(
        intent.profile_selection,
        default=infer_audio_input_profile(intent),
    )
    if selection == PROFILE_AUTO:
        if chip_available:
            return AecIntent(
                mode="auto",
                raw_enabled=False,
                dtln_enabled=False,
                chip_aec_enabled=True,
                chip_aec_150_enabled=False,
                chip_aec_210_enabled=False,
                profile_selection=selection,
            )
        return AecIntent(
            mode="auto",
            raw_enabled=True,
            dtln_enabled=False,
            chip_aec_enabled=False,
            chip_aec_150_enabled=False,
            chip_aec_210_enabled=False,
            profile_selection=selection,
        )
    if selection in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}:
        return AecIntent(
            mode="auto",
            raw_enabled=False,
            dtln_enabled=False,
            chip_aec_enabled=True,
            chip_aec_150_enabled=False,
            chip_aec_210_enabled=False,
            profile_selection=selection,
        )
    if selection == PROFILE_XVF_SOFTWARE_AEC3:
        return AecIntent(
            mode="auto",
            raw_enabled=True,
            dtln_enabled=False,
            chip_aec_enabled=False,
            chip_aec_150_enabled=False,
            chip_aec_210_enabled=False,
            profile_selection=selection,
        )
    if selection == PROFILE_DIRECT_MIC:
        return AecIntent(
            mode="disabled",
            raw_enabled=False,
            dtln_enabled=False,
            chip_aec_enabled=False,
            chip_aec_150_enabled=False,
            chip_aec_210_enabled=False,
            profile_selection=selection,
        )
    return AecIntent(
        mode=intent.mode,
        raw_enabled=intent.raw_enabled,
        dtln_enabled=intent.dtln_enabled,
        chip_aec_enabled=intent.chip_aec_enabled,
        chip_aec_150_enabled=intent.chip_aec_150_enabled,
        chip_aec_210_enabled=intent.chip_aec_210_enabled,
        profile_selection=PROFILE_CUSTOM,
    )


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


def env_value_any(
    env: Mapping[str, str],
    keys: tuple[str, ...],
    default: str = "",
    *,
    process_env: Mapping[str, str] | None = None,
) -> str:
    """Read the first present key from env-file data, then process env."""

    for key in keys:
        if key in env:
            return env[key]
    if process_env is not None:
        for key in keys:
            if key in process_env:
                return process_env[key]
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
        mic_variant=env_value(env, "JASPER_XVF_VARIANT", "", process_env=process_env),
        mic_geometry=env_value(env, "JASPER_XVF_GEOMETRY", "", process_env=process_env),
        mic_display_name=env_value(
            env,
            "JASPER_XVF_DISPLAY_NAME",
            "",
            process_env=process_env,
        ),
        chip_beam_plan=env_value(
            env,
            "JASPER_XVF_CHIP_BEAM_PLAN",
            "",
            process_env=process_env,
        ),
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
        raw_device=env_value(
            env,
            "JASPER_MIC_DEVICE_RAW",
            "",
            process_env=process_env,
        ),
        dtln_device=env_value(
            env,
            "JASPER_MIC_DEVICE_DTLN",
            "",
            process_env=process_env,
        ),
        dtln_enabled=parse_env_bool(
            env_value(env, "JASPER_AEC_DTLN_ENABLED", "0", process_env=process_env),
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
        chip_aec_gate_dac_id=env_value_any(
            env,
            (
                "JASPER_AEC_CHIP_AEC_DAC_ID",
                "JASPER_AUDIO_DAC_ID",
            ),
            "",
            process_env=process_env,
        ),
        chip_aec_gate_status=env_value_any(
            env,
            ("JASPER_AEC_CHIP_AEC_DAC_STATUS",),
            "",
            process_env=process_env,
        ),
        chip_aec_gate_source=env_value_any(
            env,
            ("JASPER_AEC_CHIP_AEC_DAC_SOURCE",),
            "",
            process_env=process_env,
        ),
        chip_aec_gate_detail=env_value_any(
            env,
            ("JASPER_AEC_CHIP_AEC_DAC_DETAIL",),
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
    chip_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify intent + observed runtime facts into status payloads.

    The returned `microphone` shape intentionally matches the historical
    `/aec.microphone` JSON object. `audio_profile` is additive and is the
    shared vocabulary future status surfaces should consume.
    """

    selection = normalize_audio_input_profile(
        intent.profile_selection,
        default=infer_audio_input_profile(intent),
    )
    gate = dict(chip_gate or {})
    gate_auto_allowed = bool(gate.get("auto_allowed", chip_available))
    gate_arm_allowed = bool(gate.get("arm_allowed", chip_available))
    gate_permitted = gate_arm_allowed
    gate_detail = str(gate.get("detail") or "")
    gate_status = str(gate.get("status") or "")
    chip_allowed_for_selection = chip_available and (
        gate_arm_allowed
        if selection == PROFILE_XVF_CHIP_AEC_TESTING
        else gate_auto_allowed
    )
    requested_intent = resolve_audio_input_intent(
        intent,
        chip_available=chip_allowed_for_selection,
    )
    if selection == PROFILE_AUTO:
        requested_profile = (
            PROFILE_XVF_CHIP_AEC
            if requested_intent.chip_aec_enabled
            else PROFILE_XVF_SOFTWARE_AEC3
        )
    elif selection in {
        PROFILE_XVF_CHIP_AEC,
        PROFILE_XVF_CHIP_AEC_TESTING,
        PROFILE_XVF_SOFTWARE_AEC3,
        PROFILE_DIRECT_MIC,
    }:
        requested_profile = selection
    elif requested_intent.mode != "auto":
        requested_profile = PROFILE_DIRECT_MIC
    elif requested_intent.chip_aec_enabled:
        requested_profile = PROFILE_XVF_CHIP_AEC
    else:
        requested_profile = PROFILE_XVF_SOFTWARE_AEC3
    direct_mic_configured = _direct_mic_configured(runtime)
    mic_variant = runtime.mic_variant or mic.variant_id
    mic_geometry = runtime.mic_geometry or mic.geometry
    chip_beam_plan = runtime.chip_beam_plan or mic.chip_beam_plan
    if mic.xvf_present:
        mic_name = runtime.mic_display_name or mic.display_name
    elif direct_mic_configured:
        mic_name = f"Direct mic ({runtime.primary_device})"
    elif mic.probe_error:
        mic_name = "Microphone status unavailable"
    else:
        mic_name = "No supported mic detected"

    warnings: list[str] = []
    managed_selection = selection != PROFILE_CUSTOM
    configured_aec_device = (runtime.aec_device or "").strip()
    detected_aec_device = (mic.alsa_card_name or "").strip()
    aec_device_mismatch = bool(
        managed_selection
        and mic.xvf_present
        and configured_aec_device
        and detected_aec_device
        and configured_aec_device != detected_aec_device
    )
    aec_device_mismatch_reason = (
        f"configured AEC mic {configured_aec_device} does not match "
        f"detected XVF card {detected_aec_device}"
        if aec_device_mismatch else ""
    )

    chip_runtime_active = bool(
        requested_intent.mode == "auto"
        and bridge_active
        and chip_available
        and gate_permitted
        and not aec_device_mismatch
        and runtime.chip_enabled
        and runtime.primary_device.startswith("udp:")
    )
    applied_raw_enabled = bool(runtime.raw_device)
    applied_dtln_enabled = bool(runtime.dtln_enabled or runtime.dtln_device)
    software_runtime_active = bool(
        requested_intent.mode == "auto"
        and bridge_active
        and not chip_runtime_active
        and not aec_device_mismatch
        and (runtime.primary_device.startswith("udp:") or runtime.raw_device)
    )
    hardware_requested = requested_profile in {
        PROFILE_XVF_CHIP_AEC,
        PROFILE_XVF_CHIP_AEC_TESTING,
    }

    if requested_intent.mode != "auto":
        processing_mode = "Direct mic"
        session_source = mic_source_label(runtime.primary_device)
        wake_legs = ["Direct mic"]
        active_profile = PROFILE_DIRECT_MIC
        profile_state = "disabled"
        profile_reason = "AEC mode is disabled."
    elif chip_runtime_active:
        processing_label = (
            "Chip-AEC testing"
            if requested_profile == PROFILE_XVF_CHIP_AEC_TESTING
            else "Chip-AEC"
        )
        processing_mode = processing_label
        session_source = (
            "Chip AEC 210 beam via :9876"
            if runtime.chip_primary_leg == "chip_aec_210"
            else "Chip AEC 150 beam via :9876"
        )
        profile_state = "active"
        active_profile = requested_profile
        profile_reason = (
            "Chip-AEC testing runtime env is applied."
            if requested_profile == PROFILE_XVF_CHIP_AEC_TESTING
            else "Chip-AEC runtime env is applied."
        )
        wake_legs = ["Primary chip beam"]
        if runtime.chip_aec_150_device:
            wake_legs.append("Chip AEC 150")
        if runtime.chip_aec_210_device:
            wake_legs.append("Chip AEC 210")
    else:
        if software_runtime_active:
            processing_mode = "Software AEC3"
            session_source = "WebRTC AEC3 via :9876"
            wake_legs = ["AEC3"]
            if applied_raw_enabled:
                wake_legs.append("Chip-direct raw")
            if applied_dtln_enabled:
                wake_legs.append("DTLN")
            active_profile = PROFILE_XVF_SOFTWARE_AEC3
        else:
            processing_mode = (
                "Chip-AEC pending" if hardware_requested else "Software AEC3 pending"
            )
            session_source = "waiting for AEC runtime"
            wake_legs = []
            active_profile = None
        if aec_device_mismatch:
            profile_state = "pending" if bridge_active else "waiting_bridge"
            profile_reason = (
                "AEC bridge is not using the detected XVF card because "
                f"{aec_device_mismatch_reason}."
            )
        elif not bridge_active:
            profile_state = "waiting_bridge"
            profile_reason = "AEC bridge is not active yet."
        elif hardware_requested and not chip_available:
            profile_state = "fallback" if software_runtime_active else "unavailable"
            profile_reason = (
                "Chip-AEC needs a validated XVF3800 chip beam plan for "
                "the detected mic geometry; using software AEC3."
            )
        elif hardware_requested and not gate_permitted:
            profile_state = "fallback" if software_runtime_active else "unavailable"
            profile_reason = (
                f"{gate_detail}; using software AEC3."
                if gate_detail
                else "Chip-AEC is not permitted for this output DAC."
            )
        elif hardware_requested:
            profile_state = "pending"
            profile_reason = (
                "Hardware echo cancellation is selected; software AEC3 is "
                "still active until the reconciler applies the chip-AEC path."
            )
        elif software_runtime_active:
            profile_state = "active"
            profile_reason = "Software AEC3 bridge is active."
        else:
            profile_state = "pending"
            profile_reason = "Software AEC3 is selected; waiting for runtime state."

    if requested_intent.mode == "auto" and not bridge_active:
        warnings.append("AEC bridge is not active yet.")
    if aec_device_mismatch:
        warnings.append(
            f"Configured AEC mic {configured_aec_device} does not match "
            f"detected XVF card {detected_aec_device}; run the reconciler "
            "to update derived mic state."
        )
    if hardware_requested and not chip_available:
        warnings.append(
            "Chip-AEC needs a validated XVF3800 chip beam plan for the "
            "detected mic geometry."
        )
    if hardware_requested and chip_available and not gate_permitted:
        if gate_status:
            warnings.append(f"Chip-AEC DAC gate is {gate_status}: {gate_detail}")
        else:
            warnings.append("Chip-AEC is not permitted for this output DAC.")
    if (
        hardware_requested
        and chip_available
        and gate_permitted
        and bridge_active
        and not chip_runtime_active
    ):
        warnings.append(
            "Chip-AEC is selected but the reconciler has not applied it yet."
        )
    if not mic.xvf_present and (
        requested_intent.mode == "auto" or hardware_requested
    ):
        warnings.append("XVF3800 mic is not detected.")
    if mic.probe_error:
        warnings.append(f"Microphone probe failed: {mic.probe_error}")
    if hardware_requested and gate_status == STATUS_TESTING:
        warnings.append(
            "Chip-AEC testing profile is active; this output DAC path is not "
            "approved for automatic production chip-AEC."
        )

    audio_profile: dict[str, Any] = {
        "selection": selection,
        "requested": requested_profile,
        "resolved": requested_profile,
        "active": active_profile,
        "state": profile_state,
        "reason": profile_reason,
        "validation_profile": validation_profile(requested_profile),
    }
    if gate:
        audio_profile["chip_aec_gate"] = gate

    return {
        "audio_profile": audio_profile,
        "microphone": {
            "detected": mic.xvf_present or direct_mic_configured,
            "name": mic_name,
            "primary_device": runtime.primary_device,
            "aec_device": runtime.aec_device,
            "firmware": _firmware_status(mic),
            "processing_mode": processing_mode,
            "session_source": session_source,
            "wake_legs": wake_legs,
            "variant_id": mic_variant,
            "geometry": mic_geometry,
            "chip_beam_plan": chip_beam_plan,
            "warnings": warnings,
        },
    }
