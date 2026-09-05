# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Corpus leg/profile vocabulary and the env/hardware probes over it.

The leaf of this package: :mod:`capture_plan` and :mod:`bridge_session` both
import this module, and it imports neither.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from jasper import audio_validation, wake_legs
from jasper.aec_sweep import (
    AEC3_SWEEP_ENV_FLAG,
    AEC3_SWEEP_SOURCE_ENV,
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    AEC3_SWEEP_VARIANTS,
    Aec3SweepConfigError,
    USB_AEC3_CORPUS_LABEL,
    normalize_aec3_sweep_source,
)
from jasper.audio_profile_state import (
    AEC_MODE_ENV,
    AEC_MODE_FILE_ENV,
    AecIntent,
    MicProbe,
    PROFILE_XVF_CHIP_AEC_TESTING,
    env_value,
    infer_audio_input_profile,
    normalize_audio_input_profile,
    parse_env_bool,
)
from jasper.chip_aec.policy import effective_chip_aec_dac_gate
from jasper.cli.aec_bridge_config import (
    OUTPUTD_REF_UDP_HOST_ENV,
    OUTPUTD_REF_UDP_PORT_ENV,
    PLAN_ENV_VARS,
    REF_SOURCE_ENV,
)
from jasper.cli.aec_bridge_engines import (
    CORPUS_USB_DTLN_ENABLED_ENV,
    DTLN_ENABLED_ENV,
)
from jasper.cli.aec_bridge_telemetry import BRIDGE_STATS_PATH_ENV
from jasper.log_event import log_event
from jasper.mics.xvf3800 import (
    AEC_MIC_DEVICE_ENV,
    CORPUS_CHIP_AEC_ENABLED_ENV,
)
from jasper.route_latency.status_socket import OUTPUTD_STATUS_SOCKET
from jasper.web._common import read_env_file

logger = logging.getLogger("jasper-wake-corpus-web")

PROFILE_STANDARD = "standard"
PROFILE_CHIP_AEC_COMPARISON = "chip_aec_comparison_v1"
CORPUS_PROFILES = (PROFILE_STANDARD, PROFILE_CHIP_AEC_COMPARISON)


# ---------------------------------------------------------------------------
# Leg / profile vocabulary
# ---------------------------------------------------------------------------

# CONDITIONS / DISTANCES (defined in the sibling recording_backend.py,
# which imports them from jasper.wake_conditions) are the operator-labelled
# input domains — the shared single source of truth so the corpus, the
# runtime fuser, and the wake telemetry agree on one taxonomy. The wizard
# validates strictly against them to reject typos;
# captured files land in aec_<leg>_<condition>/ for the upstream
# extract/score/review pipeline, so do NOT rename a condition without an
# alias (see wake_conditions' stability contract). "ambient" is the
# realistic-home floor (AC, fridge; no music we control).
# Legs the recorder knows about. "raw0" is the truly-raw mic 0 leg
# (chip channel 2 — no chip DSP), opt-in per session via the
# include_raw_mic_0 flag. The USB/reference legs are corpus-only
# experiment streams emitted by jasper-aec-bridge when explicitly
# enabled; they are never production wake-detection inputs.
AEC3_SWEEP_LEGS = tuple(variant.leg for variant in AEC3_SWEEP_VARIANTS)
DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE = AEC3_SWEEP_SOURCE_USB
# Keep old pilot legs playable when loading earlier same-day sessions.
LEGACY_AEC3_SWEEP_LEGS = (
    "aec3_hf_slow_only",
    "aec3_edge_combo",
    "aec3_gentle_dnd",
    "aec3_ns_off",
    "aec3_default_gain_08",
    "aec3_hf_relaxed",
    "aec3_hf_mask_upstream",
    "aec3_hf_wide_open",
    "aec3_nearend_fast",
    "aec3_slow_attack",
)
LEGS = (
    "on", *AEC3_SWEEP_LEGS, *LEGACY_AEC3_SWEEP_LEGS,
    "off", "dtln", "raw0", "ref",
    "usb_raw", "usb_webrtc", "usb_dtln",
    "chip_aec_150", "chip_aec_210",
    "xvf_raw0_webrtc_aec3", "xvf_raw0_dtln",
)
BASE_LEGS = ("on", "off")
DTLN_LEG = "dtln"
RAW0_LEG = "raw0"
USB_CORPUS_LEGS = ("ref", "usb_raw", "usb_webrtc")
USB_DTLN_LEG = "usb_dtln"
CHIP_AEC_LEGS = ("chip_aec_150", "chip_aec_210")
XVF_RAW0_DTLN_LEG = "xvf_raw0_dtln"
CHIP_AEC_PROFILE_BASE_LEGS = (
    "chip_aec_150",
    "chip_aec_210",
    "raw0",
    "xvf_raw0_webrtc_aec3",
    "ref",
)
LEG_LABELS = {
    "on": "XVF WebRTC AEC3",
    **{variant.leg: variant.label for variant in AEC3_SWEEP_VARIANTS},
    "aec3_hf_slow_only": "AEC3 HF + slow only (legacy)",
    "aec3_edge_combo": "AEC3 edge combo (legacy)",
    "aec3_gentle_dnd": "AEC3 gentle DND (legacy)",
    "aec3_ns_off": "AEC3 NS off (legacy)",
    "aec3_default_gain_08": "AEC3 default gain 0.8 (legacy)",
    "aec3_hf_relaxed": "AEC3 HF relaxed (legacy)",
    "aec3_hf_mask_upstream": "AEC3 HF mask upstream (legacy)",
    "aec3_hf_wide_open": "AEC3 HF wide open (legacy)",
    "aec3_nearend_fast": "AEC3 near-end fast (legacy)",
    "aec3_slow_attack": "AEC3 slow attack (legacy)",
    "off": "XVF raw",
    "dtln": "XVF DTLN",
    "raw0": "XVF raw0",
    "ref": "Reference",
    "usb_raw": "USB raw",
    "usb_webrtc": USB_AEC3_CORPUS_LABEL,
    "usb_dtln": "USB DTLN",
    "chip_aec_150": "Chip AEC ASR 150",
    "chip_aec_210": "Chip AEC ASR 210",
    "xvf_raw0_webrtc_aec3": "XVF raw0 WebRTC AEC3",
    "xvf_raw0_dtln": "XVF raw0 DTLN",
}


# ---------------------------------------------------------------------------
# Bridge-side corpus-output config + systemctl units
# ---------------------------------------------------------------------------
# The web service is intentionally sandboxed away from
# /etc/jasper/jasper.env, so operator-driven corpus experiment flags live
# in /var/lib/jasper like the other wizard-owned env files.
SYSTEM_ENV_PATH = Path(os.environ.get(
    "JASPER_SYSTEM_ENV_FILE", "/etc/jasper/jasper.env",
))
AEC_MODE_PATH = Path(os.environ.get(
    AEC_MODE_FILE_ENV, "/var/lib/jasper/aec_mode.env",
))
BRIDGE_CORPUS_ENV_PATH = Path(os.environ.get(
    "JASPER_WAKE_CORPUS_BRIDGE_ENV",
    "/var/lib/jasper/wake_corpus_bridge.env",
))
BRIDGE_STATS_PATH = Path(os.environ.get(
    BRIDGE_STATS_PATH_ENV,
    "/run/jasper/aec_bridge_stats.json",
))
AUDIO_VALIDATION_ARTIFACT_PATH = Path(os.environ.get(
    "JASPER_AUDIO_VALIDATION_ARTIFACT",
    str(audio_validation.DEFAULT_ARTIFACT_DIR),
))
BRIDGE_UNIT = "jasper-aec-bridge.service"
UNIT_STATE_TIMEOUT_SEC = 1.5
BRIDGE_CORPUS_OUTPUT_VARS = (
    *PLAN_ENV_VARS,
    DTLN_ENABLED_ENV,
    "JASPER_AEC_CORPUS_REF_ENABLED",
    "JASPER_AEC_CORPUS_USB_ENABLED",
    CORPUS_USB_DTLN_ENABLED_ENV,
    CORPUS_CHIP_AEC_ENABLED_ENV,
    "JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED",
    "JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED",
    REF_SOURCE_ENV,
    OUTPUTD_REF_UDP_HOST_ENV,
    OUTPUTD_REF_UDP_PORT_ENV,
    "JASPER_OUTPUTD_CHIP_REF_PCM",
    "JASPER_OUTPUTD_REFERENCE_UDP_TARGET",
    "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE",
    "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES",
    "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES",
    AEC3_SWEEP_ENV_FLAG,
    AEC3_SWEEP_SOURCE_ENV,
)
OUTPUTD_REF_UDP_TARGET = "127.0.0.1:9891"
OUTPUTD_REF_UDP_PORT = "9891"


DEFAULT_CHIP_REF_PCM = "plughw:CARD=Array,DEV=0"
DEFAULT_CHIP_REF_SAMPLE_RATE = "16000"
DEFAULT_CHIP_REF_PERIOD_FRAMES = "320"
DEFAULT_CHIP_REF_BUFFER_FRAMES = "1280"
DEFAULT_USB_MIC_DEVICE = "USB PnP Sound Device"


def _plain_alsa_card_id(value: str) -> bool:
    return bool(value) and not any(ch.isspace() or ch in ":,/" for ch in value)


def chip_ref_pcm_for_env(env: Mapping[str, Any] | None = None) -> str:
    """Return the current XVF USB-IN PCM for corpus chip-ref output."""
    card = ""
    if env:
        card = str(env.get("JASPER_XVF_ALSA_CARD") or "").strip()
        if not card:
            aec_mic = str(env.get(AEC_MIC_DEVICE_ENV) or "").strip()
            if _plain_alsa_card_id(aec_mic):
                card = aec_mic
    if card:
        return f"plughw:CARD={card},DEV=0"
    try:
        from jasper.mics import xvf3800
    except Exception:  # noqa: BLE001 - constants should remain import-safe
        return DEFAULT_CHIP_REF_PCM
    return f"plughw:CARD={xvf3800.detect_runtime_profile().alsa_card_name},DEV=0"

BRIDGE_OUTPUT_LABELS = {
    "dtln": "XVF DTLN",
    "ref": "reference",
    "usb": "USB raw/WebRTC AEC3",
    "usb_dtln": "USB DTLN",
    "aec3_sweep": "AEC3 sweep",
    "chip_aec": "chip AEC 150/210",
    "xvf_raw0_webrtc_aec3": "XVF raw0 WebRTC AEC3",
    "xvf_raw0_dtln": "XVF raw0 DTLN",
    "outputd_ref": "outputd direct reference",
}


def session_aec3_sweep_source(value: str | None = None) -> str:
    """New corpus sessions default the AEC3 sweep to the cheap USB mic."""
    return normalize_aec3_sweep_source(
        value,
        default=DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE,
    )


def legacy_aec3_sweep_source(value: str | None = None) -> str:
    """Older metadata/env without an explicit source meant XVF."""
    return normalize_aec3_sweep_source(value, default=AEC3_SWEEP_SOURCE_XVF)


def env_truthy(value: str | None, *, default: bool = False) -> bool:
    """Parse the bool vocabulary used by jasper-aec-bridge."""
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def read_system_env() -> dict[str, str]:
    """Read the production env file the daemons are started with."""
    return read_env_file(str(SYSTEM_ENV_PATH))


def read_bridge_env() -> dict[str, str]:
    """Read bridge env as systemd will see it: /etc first, corpus
    wizard file second. Later EnvironmentFile entries win in systemd,
    so the same overlay order is used here for status/prompt logic.
    """
    env: dict[str, str] = {}
    env.update(read_system_env())
    env.update(read_env_file(str(BRIDGE_CORPUS_ENV_PATH)))
    return env


def bridge_output_status() -> dict[str, Any]:
    """Current bridge corpus-output flags, as the UI should present
    them before beginning a session.
    """
    system_env = read_system_env()
    corpus_env = read_env_file(str(BRIDGE_CORPUS_ENV_PATH))
    env: dict[str, str] = {}
    env.update(system_env)
    env.update(corpus_env)
    try:
        aec3_sweep_source = legacy_aec3_sweep_source(
            env.get(AEC3_SWEEP_SOURCE_ENV),
        )
    except Aec3SweepConfigError as e:
        log_event(
            logger,
            "wake_corpus.aec3_sweep_source_invalid",
            error=e,
            fallback=AEC3_SWEEP_SOURCE_XVF,
            level=logging.WARNING,
        )
        aec3_sweep_source = AEC3_SWEEP_SOURCE_XVF
    recorder_outputs = {
        "dtln": env_truthy(corpus_env.get(DTLN_ENABLED_ENV)),
        "ref": env_truthy(corpus_env.get("JASPER_AEC_CORPUS_REF_ENABLED")),
        "usb": env_truthy(corpus_env.get("JASPER_AEC_CORPUS_USB_ENABLED")),
        "usb_dtln": env_truthy(corpus_env.get(CORPUS_USB_DTLN_ENABLED_ENV)),
        "chip_aec": env_truthy(corpus_env.get(CORPUS_CHIP_AEC_ENABLED_ENV)),
        "xvf_raw0_webrtc_aec3": env_truthy(
            corpus_env.get("JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"),
        ),
        "xvf_raw0_dtln": env_truthy(
            corpus_env.get("JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"),
        ),
        "outputd_ref": bool(corpus_env.get("JASPER_OUTPUTD_CHIP_REF_PCM"))
        and corpus_env.get("JASPER_OUTPUTD_REFERENCE_UDP_TARGET") == OUTPUTD_REF_UDP_TARGET,
        "aec3_sweep": env_truthy(corpus_env.get(AEC3_SWEEP_ENV_FLAG)),
        "aec3_sweep_source": aec3_sweep_source,
    }
    status = {
        "dtln": env_truthy(env.get(DTLN_ENABLED_ENV)),
        "ref": env_truthy(env.get("JASPER_AEC_CORPUS_REF_ENABLED")),
        "usb": env_truthy(env.get("JASPER_AEC_CORPUS_USB_ENABLED")),
        "usb_dtln": env_truthy(env.get(CORPUS_USB_DTLN_ENABLED_ENV)),
        "chip_aec": env_truthy(env.get(CORPUS_CHIP_AEC_ENABLED_ENV)),
        "xvf_raw0_webrtc_aec3": env_truthy(
            env.get("JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"),
        ),
        "xvf_raw0_dtln": env_truthy(
            env.get("JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"),
        ),
        "outputd_ref": bool(env.get("JASPER_OUTPUTD_CHIP_REF_PCM"))
        and env.get("JASPER_OUTPUTD_REFERENCE_UDP_TARGET") == OUTPUTD_REF_UDP_TARGET,
        "aec3_sweep": env_truthy(env.get(AEC3_SWEEP_ENV_FLAG)),
        "aec3_sweep_source": aec3_sweep_source,
        "env_path": str(BRIDGE_CORPUS_ENV_PATH),
        "recorder_outputs": recorder_outputs,
    }
    status["active"] = any(
        key in corpus_env
        for key in BRIDGE_CORPUS_OUTPUT_VARS
        if key not in PLAN_ENV_VARS
    )
    return status


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def required_bridge_outputs_for_request(
    *,
    corpus_profile: str = PROFILE_STANDARD,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool = False,
    include_aec3_sweep: bool = False,
    aec3_sweep_source: str | None = None,
) -> list[str]:
    sweep_source = (
        session_aec3_sweep_source(aec3_sweep_source)
        if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
    )
    sweep_needs_usb = (
        include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
    )
    required: list[str] = []
    if include_dtln:
        required.append("dtln")
    if include_usb_mic or include_usb_dtln or sweep_needs_usb:
        required.extend(["ref", "usb"])
    if include_usb_dtln:
        required.append("usb_dtln")
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        required.extend(["ref", "chip_aec", "xvf_raw0_webrtc_aec3", "outputd_ref"])
    if include_xvf_raw0_dtln:
        required.append("xvf_raw0_dtln")
    if include_aec3_sweep:
        required.append("aec3_sweep")
    return _dedupe(required)


def missing_bridge_outputs_from_required(
    required: list[str],
    status: Mapping[str, Any],
    *,
    aec3_sweep_source: str = AEC3_SWEEP_SOURCE_XVF,
) -> list[str]:
    missing: list[str] = []
    for output in required:
        if output == "aec3_sweep":
            if (
                not status.get("aec3_sweep")
                or status.get("aec3_sweep_source") != aec3_sweep_source
            ):
                missing.append(output)
            continue
        if not status.get(output):
            missing.append(output)
    return missing


def read_aec_intent() -> AecIntent:
    """Read production wake/audio intent from the wizard-owned state file."""
    env = read_env_file(str(AEC_MODE_PATH))
    mode = (env.get(AEC_MODE_ENV) or "auto").strip().strip("'\"") or "auto"
    return AecIntent(
        mode=mode,
        raw_enabled=parse_env_bool(
            env.get("JASPER_WAKE_LEG_RAW", "1"), default=True,
        ),
        dtln_enabled=parse_env_bool(
            env.get("JASPER_WAKE_LEG_DTLN", "0"), default=False,
        ),
        chip_aec_enabled=parse_env_bool(
            env.get("JASPER_WAKE_LEG_CHIP_AEC", "0"), default=False,
        ),
        profile_selection=env.get("JASPER_AUDIO_INPUT_PROFILE", ""),
    )


def mic_probe_and_identity() -> tuple[MicProbe, dict[str, Any]]:
    """Cheap mic identity snapshot for corpus metadata.

    This mirrors the `/wake/` status probe: no streaming audio, no chip
    writes, just the XVF USB/card facts already used for profile truth.
    """
    try:
        from jasper.mics import xvf3800

        runtime_profile = xvf3800.detect_runtime_profile()
        xvf_present = runtime_profile.present
        capture_channels = runtime_profile.capture_channels
        recommended_channels = xvf3800.RECOMMENDED_CAPTURE_CHANNELS
        probe_error = None
        identity: dict[str, Any] = {
            "family": (
                "xvf3800"
                if xvf_present or capture_channels is not None else "unknown"
            ),
            "display_name": runtime_profile.display_name,
            "variant_id": runtime_profile.variant_id,
            "geometry": runtime_profile.geometry,
            "chip_beam_plan": runtime_profile.chip_beam_plan_id,
            "chip_aec_supported": runtime_profile.chip_aec_supported,
            "profile_reason": runtime_profile.reason,
            "usb_vid_pid": (
                runtime_profile.variant.usb_vid_pid
                if runtime_profile.variant else ""
            ),
            "usb_vid_pids": list(xvf3800.USB_VID_PIDS),
            "alsa_card": runtime_profile.alsa_card_name,
            "alsa_card_candidates": list(xvf3800.ALSA_CARD_NAMES),
            "observed": {
                "present": xvf_present,
                "capture_channels": capture_channels,
            },
            "recommended_firmware": {
                "capture_channels": recommended_channels,
                "raw_mic_indices": list(
                    xvf3800.RECOMMENDED_FIRMWARE.raw_mic_indices,
                ),
                "known_good_as_of": xvf3800.FIRMWARE_KNOWN_GOOD_AS_OF,
                "blob": xvf3800.FIRMWARE_BLOB_6CH,
                "build_repo_hash": xvf3800.FIRMWARE_KNOWN_GOOD_BLD_REPO_HASH,
                "supported_6ch_variants": [
                    {
                        "variant_id": variant.variant_id,
                        "bld_msg": variant.bld_msg,
                        "geometry": variant.geometry,
                        "usb_vid_pid": variant.usb_vid_pid,
                        "alsa_card": variant.alsa_card_name,
                        "chip_beam_plan": variant.chip_beam_plan_id,
                    }
                    for variant in xvf3800.SUPPORTED_6CH_FIRMWARE
                ],
            },
        }
    except Exception as e:  # noqa: BLE001 - metadata must not block recording
        xvf_present = False
        capture_channels = None
        recommended_channels = 6
        probe_error = str(e)
        identity = {
            "family": "unknown",
            "observed": {
                "present": False,
                "capture_channels": None,
            },
            "probe_error": probe_error,
        }
    probe = MicProbe(
        xvf_present=xvf_present,
        capture_channels=capture_channels,
        recommended_channels=recommended_channels,
        display_name=identity.get(
            "display_name", "Seeed ReSpeaker XVF3800 (USB UA)",
        ),
        alsa_card_name=str(identity.get("alsa_card", "")),
        variant_id=str(identity.get("variant_id", "")),
        geometry=str(identity.get("geometry", "")),
        chip_beam_plan=str(identity.get("chip_beam_plan", "")),
        chip_aec_supported=bool(identity.get("chip_aec_supported", False)),
        probe_error=probe_error,
    )
    return probe, identity


def mic_chip_aec_available(mic_probe: MicProbe) -> bool:
    """Whether the detected mic profile has a production-validated
    chip-AEC beam plan."""

    return mic_probe.chip_aec_supported


def chip_aec_gate_for_status(
    system_env: Mapping[str, str],
    intent: AecIntent,
) -> dict[str, object]:
    """Resolve the same DAC gate used by /aec for metadata-only snapshots."""

    selection = normalize_audio_input_profile(
        intent.profile_selection,
        default=infer_audio_input_profile(intent),
    )
    testing_requested = selection == PROFILE_XVF_CHIP_AEC_TESTING
    return effective_chip_aec_dac_gate(
        system_env, testing_requested=testing_requested,
    ).to_dict()


def validation_artifact_summary(
    path: Path | None = None,
    *,
    requested_profile: str | None = None,
    mic_probe: MicProbe | None = None,
    system_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read optional profile-validation output, if present.

    The validation stream does not exist yet everywhere, and readiness
    snapshots are advisory. Corpus metadata therefore records a stable
    unknown/missing shape instead of making session creation depend on it.
    """
    path = path or AUDIO_VALIDATION_ARTIFACT_PATH
    filters: dict[str, Any] = audio_validation.current_artifact_filter_kwargs(
        requested_profile=requested_profile,
        system_env=system_env,
        mic_probe=mic_probe,
    )
    return audio_validation.latest_artifact_summary(path=path, **filters)


def _int_env(
    env: Mapping[str, str],
    key: str,
    default: int,
    *,
    process_env: Mapping[str, str] | None = None,
) -> int:
    try:
        return int(env_value(env, key, str(default), process_env=process_env))
    except (TypeError, ValueError):
        return default


def leg_detail(
    leg: str,
    ports: dict[str, int],
    *,
    aec3_sweep_source: str,
) -> dict[str, Any]:
    label = LEG_LABELS.get(leg, leg)
    if leg in AEC3_SWEEP_LEGS or leg in LEGACY_AEC3_SWEEP_LEGS:
        source = (
            aec3_sweep_source
            if leg in AEC3_SWEEP_LEGS else "legacy_xvf"
        )
        return {
            "token": leg,
            "name": leg,
            "label": label,
            "kind": wake_legs.LegKind.SOFTWARE_AEC.value,
            "wake_input": False,
            "udp_port": ports.get(leg),
            "source": source,
            "profile_role": "corpus_only",
            "health_metadata_key": f"capture_health.legs.{leg}",
        }
    try:
        spec = wake_legs.by_token(leg)
        return {
            "token": spec.token,
            "name": spec.name,
            "label": label,
            "kind": spec.kind.value,
            "wake_input": spec.wake_input,
            "udp_port": ports.get(leg, spec.udp_port),
            "profile_role": (
                "production_wake" if spec.wake_input else "corpus_only"
            ),
            "health_metadata_key": f"capture_health.legs.{leg}",
        }
    except KeyError:
        return {
            "token": leg,
            "name": leg,
            "label": label,
            "kind": "unknown",
            "wake_input": False,
            "udp_port": ports.get(leg),
            "profile_role": "unknown",
            "health_metadata_key": f"capture_health.legs.{leg}",
        }


def dac_reference_context(
    env: Mapping[str, str],
    bridge_outputs: dict[str, Any],
    *,
    process_env: Mapping[str, str] | None = None,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = validation or validation_artifact_summary(system_env=env)
    return {
        "dac": {
            "pcm": env_value(
                env,
                "JASPER_OUTPUTD_DAC_PCM",
                "outputd_dac",
                process_env=process_env,
            ),
            "backend": env_value(
                env,
                "JASPER_OUTPUTD_BACKEND",
                "alsa",
                process_env=process_env,
            ),
            "control_socket": env_value(
                env,
                "JASPER_OUTPUTD_CONTROL_SOCKET",
                OUTPUTD_STATUS_SOCKET,
                process_env=process_env,
            ),
        },
        "reference": {
            "source": env_value(
                env,
                REF_SOURCE_ENV,
                # Matches the bridge's own default (aec_bridge.REF_SOURCE).
                # A corpus session must not record a source nothing can read.
                "outputd_udp",
                process_env=process_env,
            ),
            "outputd_chip_ref_pcm": env_value(
                env,
                "JASPER_OUTPUTD_CHIP_REF_PCM",
                "",
                process_env=process_env,
            ),
            "outputd_reference_udp_target": env_value(
                env,
                "JASPER_OUTPUTD_REFERENCE_UDP_TARGET",
                "",
                process_env=process_env,
            ),
            "outputd_chip_ref_sample_rate": _int_env(
                env,
                "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE",
                int(DEFAULT_CHIP_REF_SAMPLE_RATE),
                process_env=process_env,
            ),
            "outputd_chip_ref_period_frames": _int_env(
                env,
                "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES",
                int(DEFAULT_CHIP_REF_PERIOD_FRAMES),
                process_env=process_env,
            ),
            "outputd_chip_ref_buffer_frames": _int_env(
                env,
                "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES",
                int(DEFAULT_CHIP_REF_BUFFER_FRAMES),
                process_env=process_env,
            ),
            "bridge_output_enabled": bool(bridge_outputs.get("outputd_ref")),
        },
        "validation": validation,
    }


def session_legs(
    ports: dict[str, int],
    *,
    corpus_profile: str = PROFILE_STANDARD,
    include_dtln: bool = True,
    include_raw_mic_0: bool = False,
    include_usb_mic: bool = False,
    include_usb_dtln: bool = False,
    include_xvf_raw0_dtln: bool = False,
    include_aec3_sweep: bool = False,
    aec3_sweep_source: str | None = None,
) -> tuple[str, ...]:
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        legs = [leg for leg in CHIP_AEC_PROFILE_BASE_LEGS if leg in ports]
        if include_usb_mic:
            legs.extend(
                leg for leg in ("usb_raw", "usb_webrtc") if leg in ports
            )
        if include_xvf_raw0_dtln and XVF_RAW0_DTLN_LEG in ports:
            legs.append(XVF_RAW0_DTLN_LEG)
        if include_usb_dtln and USB_DTLN_LEG in ports:
            legs.extend(
                leg for leg in ("usb_raw", USB_DTLN_LEG) if leg in ports
            )
        return tuple(dict.fromkeys(legs))

    sweep_source = (
        session_aec3_sweep_source(aec3_sweep_source)
        if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
    )
    sweep_needs_usb = (
        include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
    )
    legs = []
    if "on" in ports:
        legs.append("on")
    if include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_XVF:
        legs.extend(leg for leg in AEC3_SWEEP_LEGS if leg in ports)
    if "off" in ports:
        legs.append("off")
    if include_dtln and DTLN_LEG in ports:
        legs.append(DTLN_LEG)
    if include_raw_mic_0 and RAW0_LEG in ports:
        legs.append(RAW0_LEG)
    if include_xvf_raw0_dtln and XVF_RAW0_DTLN_LEG in ports:
        # DTLN on raw0 is interpretable only beside the unprocessed raw0
        # clip from the same utterance.
        legs.extend(leg for leg in (RAW0_LEG, XVF_RAW0_DTLN_LEG) if leg in ports)
    if include_usb_mic or sweep_needs_usb:
        legs.extend(leg for leg in USB_CORPUS_LEGS if leg in ports)
    if include_usb_dtln and USB_DTLN_LEG in ports:
        # DTLN only makes sense when compared to the same raw USB mic
        # and reference signal, so include those companion legs even if
        # the caller didn't turn on the broader USB/WebRTC toggle.
        legs.extend(leg for leg in ("ref", "usb_raw", USB_DTLN_LEG) if leg in ports)
    if include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB:
        legs.extend(leg for leg in AEC3_SWEEP_LEGS if leg in ports)
    # Preserve order while de-duping.
    return tuple(dict.fromkeys(legs))


def systemd_unit_active(unit: str) -> bool:
    """Return whether *unit* is active, with bounded systemd I/O.

    Only stable, recognized states authorize a state-changing caller to
    continue.  Spawn, timeout, manager, permission, transitional, and
    otherwise unrecognized responses raise so those callers can fail closed.
    Observational callers use the fail-soft wrappers instead.
    """
    rc = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=UNIT_STATE_TIMEOUT_SEC,
    )
    state = (rc.stdout or "").strip().lower()
    detail = (rc.stderr or "").strip()
    if detail:
        raise OSError(
            f"systemctl is-active {unit} returned rc={rc.returncode}: "
            f"{detail[-300:]}",
        )
    if rc.returncode == 0 and state == "active":
        return True
    if state in {"inactive", "failed"}:
        return False
    raise OSError(
        f"systemctl is-active {unit} returned rc={rc.returncode}, "
        f"state={state or '<empty>'}",
    )


def aec_bridge_active() -> bool:
    """True if jasper-aec-bridge is active, for metadata snapshots only."""
    try:
        return systemd_unit_active(BRIDGE_UNIT)
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_bridge_stats_snapshot() -> dict[str, Any] | None:
    """Read the bridge's monotonic capture counters from tmpfs.

    Returns None when the deployed bridge predates stats support, is not
    running, or the file is mid-write/corrupt. The recorder stores that
    as `capture_health.status=unknown` instead of pretending the clip is
    clean.
    """
    try:
        data = json.loads(BRIDGE_STATS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    counters = data.get("counters")
    if not isinstance(counters, dict):
        return None
    return data
