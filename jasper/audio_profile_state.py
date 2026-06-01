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

from . import wake_legs


@dataclass(frozen=True)
class ProfileEnvVar:
    """One env var in a profile's static reconciler-applied env shape."""

    key: str
    value: str


@dataclass(frozen=True)
class ProfileLeg:
    """One wake/corpus leg declared by an audio profile.

    `token` is the frozen wake_legs wire/carrier token. Most profile legs
    carry the same signal kind as their registry entry; `source_kind`
    makes the exceptional case explicit when a profile repoints a carrier,
    such as chip-AEC forwarding its primary beam through the historical
    `on`/:9876 carrier. `env_key`, when present, names the runtime env var
    that points a consumer at that leg's UDP device. The value is still
    written by the reconciler; this object is only the read-only contract.
    """

    token: str
    label: str
    optional: bool = False
    source_kind: wake_legs.LegKind | None = None
    env_key: str = ""
    enabled_env_key: str = ""

    @property
    def effective_source_kind(self) -> wake_legs.LegKind:
        """Signal kind carried by this profile leg."""

        return self.source_kind or wake_legs.by_token(self.token).kind


@dataclass(frozen=True)
class AudioProfileDeclaration:
    """Read-only declaration for the shared audio-profile vocabulary."""

    profile_id: str
    label: str
    purpose: str
    mic_family: str
    has_static_runtime_env: bool
    test_only: bool = False
    requires_bridge: bool = False
    requires_xvf_6ch: bool = False
    requires_chip_reference: bool = False
    wake_legs: tuple[ProfileLeg, ...] = ()
    corpus_legs: tuple[ProfileLeg, ...] = ()
    static_env: tuple[ProfileEnvVar, ...] = ()
    cleared_env_keys: tuple[str, ...] = ()
    mutually_exclusive_leg_tokens: tuple[str, ...] = ()


_BRIDGE_REF_HOST = "127.0.0.1"
_BRIDGE_REF_PORT = "9891"
_CHIP_REF_PCM = "plughw:CARD=Array,DEV=0"
_CHIP_REF_SAMPLE_RATE = "16000"
_CHIP_REF_PERIOD_FRAMES = "320"
_CHIP_REF_BUFFER_FRAMES = "1280"


_AUDIO_PROFILES: tuple[AudioProfileDeclaration, ...] = (
    AudioProfileDeclaration(
        profile_id="direct_mic",
        label="Direct mic",
        purpose="Fallback/direct capture when the AEC bridge is disabled.",
        mic_family="any",
        has_static_runtime_env=False,
    ),
    AudioProfileDeclaration(
        profile_id="xvf_software_aec3",
        label="XVF software AEC3",
        purpose=(
            "Default safe XVF path: WebRTC AEC3 with optional raw/DTLN "
            "wake legs."
        ),
        mic_family="xvf3800",
        has_static_runtime_env=True,
        requires_bridge=True,
        requires_xvf_6ch=True,
        wake_legs=(
            ProfileLeg("on", "AEC3", env_key="JASPER_MIC_DEVICE"),
            ProfileLeg(
                "off",
                "Chip-direct raw",
                optional=True,
                env_key="JASPER_MIC_DEVICE_RAW",
            ),
            ProfileLeg(
                "dtln",
                "DTLN",
                optional=True,
                env_key="JASPER_MIC_DEVICE_DTLN",
                enabled_env_key="JASPER_AEC_DTLN_ENABLED",
            ),
        ),
        static_env=(
            ProfileEnvVar("JASPER_AEC_CHIP_AEC_ENABLED", "0"),
            ProfileEnvVar("JASPER_AEC_REF_SOURCE", "alsa"),
            ProfileEnvVar("JASPER_AEC_OUTPUTD_REF_UDP_HOST", _BRIDGE_REF_HOST),
            ProfileEnvVar("JASPER_AEC_OUTPUTD_REF_UDP_PORT", _BRIDGE_REF_PORT),
            ProfileEnvVar(
                "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE",
                _CHIP_REF_SAMPLE_RATE,
            ),
            ProfileEnvVar(
                "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES",
                _CHIP_REF_PERIOD_FRAMES,
            ),
            ProfileEnvVar(
                "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES",
                _CHIP_REF_BUFFER_FRAMES,
            ),
        ),
        cleared_env_keys=(
            "JASPER_MIC_DEVICE_CHIP_AEC_150",
            "JASPER_MIC_DEVICE_CHIP_AEC_210",
            "JASPER_OUTPUTD_CHIP_REF_PCM",
            "JASPER_OUTPUTD_REFERENCE_UDP_TARGET",
        ),
    ),
    AudioProfileDeclaration(
        profile_id="xvf_chip_aec",
        label="XVF chip-AEC",
        purpose=(
            "Opt-in XVF hardware-AEC path: chip beam carrier on :9876 plus "
            "fixed 150/210 scoring beams."
        ),
        mic_family="xvf3800",
        has_static_runtime_env=True,
        requires_bridge=True,
        requires_xvf_6ch=True,
        requires_chip_reference=True,
        wake_legs=(
            ProfileLeg(
                "on",
                "Primary chip beam",
                source_kind=wake_legs.LegKind.HARDWARE_AEC,
                env_key="JASPER_MIC_DEVICE",
            ),
            ProfileLeg(
                "chip_aec_150",
                "Chip AEC 150",
                env_key="JASPER_MIC_DEVICE_CHIP_AEC_150",
            ),
            ProfileLeg(
                "chip_aec_210",
                "Chip AEC 210",
                env_key="JASPER_MIC_DEVICE_CHIP_AEC_210",
            ),
        ),
        static_env=(
            ProfileEnvVar("JASPER_AEC_CHIP_AEC_ENABLED", "1"),
            ProfileEnvVar("JASPER_AEC_DTLN_ENABLED", "0"),
            ProfileEnvVar("JASPER_AEC_REF_SOURCE", "outputd_udp"),
            ProfileEnvVar("JASPER_AEC_OUTPUTD_REF_UDP_HOST", _BRIDGE_REF_HOST),
            ProfileEnvVar("JASPER_AEC_OUTPUTD_REF_UDP_PORT", _BRIDGE_REF_PORT),
            ProfileEnvVar("JASPER_OUTPUTD_CHIP_REF_PCM", _CHIP_REF_PCM),
            ProfileEnvVar(
                "JASPER_OUTPUTD_REFERENCE_UDP_TARGET",
                f"{_BRIDGE_REF_HOST}:{_BRIDGE_REF_PORT}",
            ),
            ProfileEnvVar(
                "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE",
                _CHIP_REF_SAMPLE_RATE,
            ),
            ProfileEnvVar(
                "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES",
                _CHIP_REF_PERIOD_FRAMES,
            ),
            ProfileEnvVar(
                "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES",
                _CHIP_REF_BUFFER_FRAMES,
            ),
        ),
        cleared_env_keys=(
            "JASPER_MIC_DEVICE_RAW",
            "JASPER_MIC_DEVICE_DTLN",
        ),
        mutually_exclusive_leg_tokens=("off", "dtln"),
    ),
    AudioProfileDeclaration(
        profile_id="generic_usb_software_aec3",
        label="Generic USB software AEC3",
        purpose=(
            "Future generic mic path: mono USB mic plus playback reference "
            "into WebRTC AEC3."
        ),
        mic_family="generic_usb",
        has_static_runtime_env=False,
        requires_bridge=True,
        wake_legs=(
            ProfileLeg("on", "AEC3", env_key="JASPER_MIC_DEVICE"),
            ProfileLeg(
                "off",
                "Direct mic",
                optional=True,
                env_key="JASPER_MIC_DEVICE_RAW",
            ),
            ProfileLeg(
                "dtln",
                "DTLN",
                optional=True,
                env_key="JASPER_MIC_DEVICE_DTLN",
                enabled_env_key="JASPER_AEC_DTLN_ENABLED",
            ),
        ),
    ),
    AudioProfileDeclaration(
        profile_id="corpus_comparison",
        label="Corpus comparison",
        purpose="Test-only same-utterance capture of production and corpus-only legs.",
        mic_family="mixed",
        has_static_runtime_env=False,
        test_only=True,
        requires_bridge=True,
        wake_legs=(
            ProfileLeg("on", "AEC3"),
            ProfileLeg("off", "Chip-direct raw"),
            ProfileLeg("dtln", "DTLN", optional=True),
            ProfileLeg("chip_aec_150", "Chip AEC 150", optional=True),
            ProfileLeg("chip_aec_210", "Chip AEC 210", optional=True),
        ),
        corpus_legs=(
            ProfileLeg("raw0", "Raw mic 0"),
            ProfileLeg("ref", "AEC reference"),
            ProfileLeg("usb_raw", "USB raw"),
            ProfileLeg("usb_webrtc", "USB WebRTC AEC3"),
            ProfileLeg("usb_dtln", "USB DTLN", optional=True),
            ProfileLeg(
                "xvf_raw0_webrtc_aec3",
                "XVF raw0 WebRTC AEC3",
                optional=True,
            ),
            ProfileLeg("xvf_raw0_dtln", "XVF raw0 DTLN", optional=True),
        ),
    ),
    AudioProfileDeclaration(
        profile_id="dac_validation",
        label="DAC validation",
        purpose="Test-only drift/delay/reference-health measurement for chip-AEC viability.",
        mic_family="xvf3800",
        has_static_runtime_env=False,
        test_only=True,
        requires_xvf_6ch=True,
        requires_chip_reference=True,
    ),
)

_PROFILE_BY_ID = {profile.profile_id: profile for profile in _AUDIO_PROFILES}


def audio_profile_declarations() -> tuple[AudioProfileDeclaration, ...]:
    """Return every declared profile in stable display/order."""

    return _AUDIO_PROFILES


def profile_by_id(profile_id: str) -> AudioProfileDeclaration:
    """Look up a profile declaration by id. Raises KeyError on miss."""

    return _PROFILE_BY_ID[profile_id]


def _default_udp_device(token: str) -> str:
    return f"udp:{wake_legs.by_token(token).udp_port}"


def profile_wake_leg_labels(
    profile_id: str,
    *,
    enabled_optional_tokens: tuple[str, ...] = (),
) -> list[str]:
    """Human labels for the wake legs active under a profile declaration."""

    enabled = set(enabled_optional_tokens)
    labels: list[str] = []
    for leg in profile_by_id(profile_id).wake_legs:
        if leg.optional and leg.token not in enabled:
            continue
        labels.append(leg.label)
    return labels


def expected_runtime_env_for_profile(
    profile_id: str,
    *,
    enabled_optional_tokens: tuple[str, ...] = (),
) -> dict[str, str]:
    """Static default runtime env shape for a profile that declares one.

    This is deliberately read-only test/support data. It mirrors the
    existing reconciler-owned env shape for the default UDP ports; it
    does not write env files, resolve operator port overrides, or prove
    that a profile is runnable on the current hardware.
    """

    profile = profile_by_id(profile_id)
    if not profile.has_static_runtime_env:
        raise ValueError(f"profile {profile_id!r} has no static runtime env shape")

    enabled = set(enabled_optional_tokens)
    env = {item.key: item.value for item in profile.static_env}
    for leg in profile.wake_legs:
        if not leg.env_key:
            continue
        if leg.optional and leg.token not in enabled:
            env[leg.env_key] = ""
            if leg.enabled_env_key:
                env[leg.enabled_env_key] = "0"
            continue
        env[leg.env_key] = _default_udp_device(leg.token)
        if leg.enabled_env_key:
            env[leg.enabled_env_key] = "1"
    for key in profile.cleared_env_keys:
        env.setdefault(key, "")
    return env


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
    elif direct_mic_configured:
        mic_name = f"Direct mic ({runtime.primary_device})"
    elif mic.probe_error:
        mic_name = "Microphone status unavailable"
    else:
        mic_name = "No supported mic detected"

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
        active_profile = "direct_mic"
        profile_state = "disabled"
        profile_reason = "AEC mode is disabled."
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
        wake_legs = profile_wake_leg_labels("xvf_chip_aec")
        requested_profile = "xvf_chip_aec"
    else:
        processing_mode = "Software AEC3"
        session_source = "WebRTC AEC3 via :9876" if bridge_active else "waiting for AEC bridge"
        optional_tokens: list[str] = []
        if intent.raw_enabled:
            optional_tokens.append("off")
        if intent.dtln_enabled:
            optional_tokens.append("dtln")
        wake_legs = profile_wake_leg_labels(
            "xvf_software_aec3",
            enabled_optional_tokens=tuple(optional_tokens),
        )
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
            "primary_device": runtime.primary_device,
            "aec_device": runtime.aec_device,
            "firmware": _firmware_status(mic),
            "processing_mode": processing_mode,
            "session_source": session_source,
            "wake_legs": wake_legs,
            "warnings": warnings,
        },
    }
