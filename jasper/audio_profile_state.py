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
from pathlib import Path
from typing import Any, Mapping

from .chip_aec.health import (
    ACTION_RECOMMISSION, ENV_KEYS, STATUS_DISCLOSED_STALE, STATUS_READY,
    AlignmentHealth,
)
from .chip_aec.policy import (
    ACTION_USE_SOFTWARE_OR_TEST, STATUS_TESTING, permits_selection,
)


# The operator's audio-input selection, written by the /aec wizard and read
# back by every consumer of the vocabulary below.
DEFAULT_AEC_MODE_PATH = Path("/var/lib/jasper/aec_mode.env")

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
    chip_aec_alignment: AlignmentHealth = AlignmentHealth("")


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
    chip_aec_supported: bool = False
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


def resolve_profile_wake_legs(
    profile: str,
    *,
    chip_available: bool,
) -> dict[str, str]:
    """Runtime AEC mode and wake legs the reconciler applies for a profile.

    `profile_env_updates` owns the per-profile vectors; runtime capability
    only decides which of them a chip-seeking profile lands on. `custom`
    yields no keys — the operator's own mode and legs stand.
    """

    normalized = normalize_audio_input_profile(profile, default=PROFILE_CUSTOM)
    if normalized in (
        PROFILE_AUTO,
        PROFILE_XVF_CHIP_AEC,
        PROFILE_XVF_CHIP_AEC_TESTING,
    ):
        normalized = (
            PROFILE_XVF_CHIP_AEC if chip_available else PROFILE_XVF_SOFTWARE_AEC3
        )
    updates = profile_env_updates(normalized)
    updates.pop("JASPER_AUDIO_INPUT_PROFILE")
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
        chip_aec_alignment=AlignmentHealth.from_env(
            {
                key: env_value(env, key, "", process_env=process_env)
                for key in ENV_KEYS
            }
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


def _chip_session_source(runtime: RuntimeAecEnv) -> str:
    return (
        "Chip AEC 210 beam via :9876"
        if runtime.chip_primary_leg == "chip_aec_210"
        else "Chip AEC 150 beam via :9876"
    )


def _chip_wake_legs(runtime: RuntimeAecEnv) -> list[str]:
    legs = ["Primary chip beam"]
    if runtime.chip_aec_150_device:
        legs.append("Chip AEC 150")
    if runtime.chip_aec_210_device:
        legs.append("Chip AEC 210")
    return legs


# processing_mode, session_source, wake_legs, active profile.
_Engine = tuple[str, str, list[str], str]


def _software_aec3_engine(runtime: RuntimeAecEnv) -> _Engine:
    legs = ["AEC3"]
    if runtime.raw_device:
        legs.append("Chip-direct raw")
    if runtime.dtln_enabled or runtime.dtln_device:
        legs.append("DTLN")
    return "Software AEC3", "WebRTC AEC3 via :9876", legs, PROFILE_XVF_SOFTWARE_AEC3


def _chip_engine(runtime: RuntimeAecEnv, profile: str) -> _Engine:
    testing = profile == PROFILE_XVF_CHIP_AEC_TESTING
    return (
        "Chip-AEC testing" if testing else "Chip-AEC",
        _chip_session_source(runtime),
        _chip_wake_legs(runtime),
        PROFILE_XVF_CHIP_AEC_TESTING if testing else PROFILE_XVF_CHIP_AEC,
    )


def _direct_mic_engine(runtime: RuntimeAecEnv) -> _Engine:
    return (
        "Direct mic",
        mic_source_label(runtime.primary_device),
        ["Direct mic"],
        PROFILE_DIRECT_MIC,
    )


def _wake_engine(
    runtime: RuntimeAecEnv,
    mic: MicProbe,
    *,
    aec_auto: bool,
    bridge_active: bool,
    chip_claimable: bool,
    chip_profile: str,
) -> _Engine | None:
    """The engine carrying the wake path right now, or None if none is.

    The single answer for every arm that names one — ready, software, direct,
    disclosed — so one box cannot report chip-AEC on one surface and AEC3 on
    another from the same facts. ADR-0101 makes `disclosed_stale` a RUNNING
    state, so it may only be claimed on the same live evidence the other arms
    use: a chip beam on the bridge's carrier, the bridge's own AEC3, or a real
    card.
    """

    if not aec_auto:
        return _direct_mic_engine(runtime)
    carrier = runtime.primary_device.startswith("udp:")
    if chip_claimable and runtime.chip_enabled and bridge_active and carrier:
        return _chip_engine(runtime, chip_profile)
    if bridge_active and (carrier or runtime.raw_device):
        return _software_aec3_engine(runtime)
    if (
        runtime.primary_device
        and not carrier
        # `Array` in both device keys is the never-configured default that
        # `_direct_mic_configured` rejects; a probed chip behind it is the
        # deliberate handover the reconciler makes with the bridge down.
        and (_direct_mic_configured(runtime) or mic.xvf_present)
    ):
        return _direct_mic_engine(runtime)
    return None


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
    # The two selections that arm chip-AEC by hand rather than by policy: the
    # testing profile, and a custom profile whose chip leg the operator set and
    # the reconciler honours. `permits_selection` owns what that buys them.
    custom_chip_leg = selection == PROFILE_CUSTOM and intent.chip_aec_enabled
    explicitly_armed = (
        selection == PROFILE_XVF_CHIP_AEC_TESTING or custom_chip_leg
    )
    gate_permitted = permits_selection(
        auto_allowed=gate_auto_allowed, testing_requested=explicitly_armed
    )
    gate_detail = str(gate.get("detail") or "")
    gate_status = str(gate.get("status") or "")
    chip_allowed_for_selection = chip_available and gate_permitted
    requested_intent = resolve_audio_input_intent(
        intent,
        chip_available=chip_allowed_for_selection,
    )
    managed_xvf = mic.xvf_present and selection != PROFILE_CUSTOM
    if managed_xvf:
        # Chip-AEC is what a managed XVF REQUESTS, whatever it ends up running:
        # preserve the stored selection for diagnosis and keep the request
        # honest, so a disclosed fallback reads as chip-AEC-not-armed rather
        # than as a different profile having been asked for.
        requested_intent = AecIntent(
            mode="auto",
            chip_aec_enabled=True,
            profile_selection=selection,
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
    if managed_xvf:
        requested_profile = PROFILE_XVF_CHIP_AEC
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

    hardware_requested = requested_profile in {
        PROFILE_XVF_CHIP_AEC,
        PROFILE_XVF_CHIP_AEC_TESTING,
    }
    # Serving rule for both records: jasper.chip_aec.policy's module docstring.
    alignment = runtime.chip_aec_alignment
    alignment_owned = alignment.applies_to(selection, custom_profile=PROFILE_CUSTOM)
    alignment_status = alignment.status if alignment_owned else ""
    # Absent is not "blocked": with no verdict published for this selection the
    # chip arms on the live evidence _wake_engine checks — the operator's leg
    # and a beam actually on the carrier — rather than on a record about
    # someone else's path.
    alignment_permits_chip = alignment_status == STATUS_READY or not alignment_owned
    # A device mismatch means the bridge is not capturing the detected XVF, so
    # what arrives cannot be this mic's chip beam. The beam plan and the DAC
    # gate answer whether chip-AEC may arm, not what the bridge is carrying, so
    # they gate the ready arm alone: a disclosed box keeps the armed chip legs
    # the reconciler left running under its disclosure.
    chip_claimable = bool(
        not aec_device_mismatch
        and (
            (chip_available and gate_permitted and alignment_permits_chip)
            or alignment_status == STATUS_DISCLOSED_STALE
        )
    )
    running_engine = _wake_engine(
        runtime,
        mic,
        aec_auto=requested_intent.mode == "auto",
        bridge_active=bridge_active,
        chip_claimable=chip_claimable,
        chip_profile=requested_profile,
    )
    running_profile = running_engine[3] if running_engine is not None else None
    disclosed_engine = (
        running_engine
        if requested_intent.mode == "auto"
        and alignment_status == STATUS_DISCLOSED_STALE
        else None
    )
    direct_engine = running_engine if requested_intent.mode != "auto" else None
    chip_engine = (
        running_engine
        if alignment_permits_chip
        and running_profile in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}
        else None
    )
    software_engine = (
        running_engine
        if running_profile == PROFILE_XVF_SOFTWARE_AEC3 and not managed_xvf
        else None
    )

    profile_action = ""
    if (
        managed_xvf
        and alignment_status
        and alignment_status not in {STATUS_READY, STATUS_DISCLOSED_STALE}
    ):
        processing_mode = "Chip-AEC parked"
        session_source = "parked pending chip-AEC alignment"
        wake_legs: list[str] = []
        active_profile: str | None = None
        profile_state = alignment_status
        profile_reason = (
            alignment.reason or "Managed XVF chip-AEC alignment is not ready."
        )
        profile_action = alignment.action
    elif disclosed_engine is not None:
        # ADR-0101: disclosed_stale is a RUNNING state — name the engine the
        # wake path actually has, and let the reconciler's own reason/action
        # say what chip-AEC lost. A disclosed box with no live engine keeps
        # the pending/waiting arms below: it is not running anything to claim.
        processing_mode, session_source, wake_legs, active_profile = disclosed_engine
        profile_state = STATUS_DISCLOSED_STALE
        profile_reason = alignment.reason or "Chip-AEC is not fully armed."
        profile_action = alignment.action
    elif direct_engine is not None:
        processing_mode, session_source, wake_legs, active_profile = direct_engine
        profile_state = "disabled"
        profile_reason = "AEC mode is disabled."
    elif chip_engine is not None:
        processing_mode, session_source, wake_legs, active_profile = chip_engine
        profile_state = "active"
        profile_reason = (
            "Chip-AEC testing runtime env is applied."
            if active_profile == PROFILE_XVF_CHIP_AEC_TESTING
            else "Chip-AEC runtime env is applied."
        )
    else:
        if software_engine is not None:
            (
                processing_mode,
                session_source,
                wake_legs,
                active_profile,
            ) = software_engine
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
        elif hardware_requested and not chip_allowed_for_selection:
            # ADR-0101: unproven chip-AEC is a disclosure on a box that is
            # hearing and a refusal only on one that is not, so the state
            # follows the engine the wake path actually has.
            profile_reason = (
                "Chip-AEC needs a validated XVF3800 chip beam plan for "
                "the detected mic geometry."
                if not chip_available
                else gate_detail or "Chip-AEC is not permitted for this output DAC."
            )
            if active_profile is None:
                profile_state = "unavailable"
            else:
                # The GATE's own disclosure, for a box whose blocked chip-AEC no
                # alignment status describes; `disclosed_engine` above serves the
                # ones the reconciler already published an alignment reason for.
                profile_state = STATUS_DISCLOSED_STALE
                # `action` is operator text, while the gate answers in action
                # CODES. Only the uncodified-DAC code has a command behind it —
                # the one the reconciler pairs with its own disclosure of that
                # same condition; every other code is said by `reason` instead.
                profile_action = (
                    ACTION_RECOMMISSION
                    if str(gate.get("recommended_action") or "")
                    == ACTION_USE_SOFTWARE_OR_TEST
                    else ""
                )
        elif hardware_requested:
            profile_state = "pending"
            profile_reason = (
                "Hardware echo cancellation is selected; waiting for the "
                "reconciler to apply and verify commissioned chip-AEC."
            )
        elif software_engine is not None:
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
        and chip_engine is None
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
        "active": active_profile,
        "state": profile_state,
        "reason": profile_reason,
        "validation_profile": validation_profile(requested_profile),
        "action": profile_action,
        # Structured decode of `action` against the writers' shared constant,
        # so UI surfaces gate on a boolean rather than matching prose.
        "commission_recommended": profile_action == ACTION_RECOMMISSION,
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
