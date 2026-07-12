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

import json
import logging
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper import audio_validation, wake_legs
from jasper.control import restart_broker
from jasper.log_event import log_event
from jasper.audio_profile_state import (
    AecIntent,
    MicProbe,
    PROFILE_XVF_CHIP_AEC,
    PROFILE_XVF_CHIP_AEC_TESTING,
    build_audio_profile_status,
    env_value,
    infer_audio_input_profile,
    normalize_audio_input_profile,
    parse_env_bool,
    runtime_env_from_mapping,
)
from jasper.chip_aec_policy import (
    gate_from_runtime_env,
    resolve_chip_aec_dac_gate,
)
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
from .capture_plan import (
    PLAN_ENV_VARS,
    PlanConformance,
    WakeCorpusCapturePlan,
    fingerprint_mapping,
)

logger = logging.getLogger("jasper-wake-corpus-web")


def _mic_chip_aec_available(mic_probe: MicProbe) -> bool:
    """Whether the detected mic profile has a chip-AEC beam plan."""

    return bool(mic_probe.xvf_present and mic_probe.chip_beam_plan)


def _chip_aec_gate_for_status(
    system_env: Mapping[str, str],
    intent: AecIntent,
) -> dict[str, object]:
    """Resolve the same DAC gate used by /aec for metadata-only snapshots."""

    selection = normalize_audio_input_profile(
        intent.profile_selection,
        default=infer_audio_input_profile(intent),
    )
    testing_requested = selection == PROFILE_XVF_CHIP_AEC_TESTING
    runtime_gate = gate_from_runtime_env(system_env)
    if runtime_gate is not None and (
        not testing_requested or runtime_gate.arm_allowed
    ):
        return runtime_gate.to_dict()
    return resolve_chip_aec_dac_gate(
        system_env.get("JASPER_AUDIO_DAC_ID", "unknown"),
        testing_requested=testing_requested,
    ).to_dict()


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
PROFILE_STANDARD = "standard"
PROFILE_CHIP_AEC_COMPARISON = "chip_aec_comparison_v1"
CORPUS_PROFILES = (PROFILE_STANDARD, PROFILE_CHIP_AEC_COMPARISON)
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
XVF_RAW0_WEBRTC_AEC3_LEG = "xvf_raw0_webrtc_aec3"
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
    "JASPER_AEC_MODE_FILE", "/var/lib/jasper/aec_mode.env",
))
BRIDGE_CORPUS_ENV_PATH = Path(os.environ.get(
    "JASPER_WAKE_CORPUS_BRIDGE_ENV",
    "/var/lib/jasper/wake_corpus_bridge.env",
))
BRIDGE_STATS_PATH = Path(os.environ.get(
    "JASPER_AEC_BRIDGE_STATS_PATH",
    "/run/jasper/aec_bridge_stats.json",
))
AUDIO_VALIDATION_ARTIFACT_PATH = Path(os.environ.get(
    "JASPER_AUDIO_VALIDATION_ARTIFACT",
    str(audio_validation.DEFAULT_ARTIFACT_DIR),
))
AUDIO_CONTEXT_SCHEMA_VERSION = 1
CAPTURE_PLAN_SCHEMA_VERSION = 1
CAPTURE_PLAN_STATE_PREVIEW = "preview"
CAPTURE_PLAN_STATE_SESSION = "session"
_CAPTURE_PLAN_PROBE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
    subprocess.SubprocessError,
)
BRIDGE_UNIT = "jasper-aec-bridge.service"
OUTPUTD_UNIT = "jasper-outputd.service"
AEC_INIT_UNIT = "jasper-aec-init.service"
BRIDGE_RESTART_TIMEOUT_SEC = 30.0
_UNIT_STATE_TIMEOUT_SEC = 1.5
BRIDGE_CORPUS_OUTPUT_VARS = (
    *PLAN_ENV_VARS,
    "JASPER_AEC_DTLN_ENABLED",
    "JASPER_AEC_CORPUS_REF_ENABLED",
    "JASPER_AEC_CORPUS_USB_ENABLED",
    "JASPER_AEC_CORPUS_USB_DTLN_ENABLED",
    "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED",
    "JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED",
    "JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED",
    "JASPER_AEC_REF_SOURCE",
    "JASPER_AEC_OUTPUTD_REF_UDP_HOST",
    "JASPER_AEC_OUTPUTD_REF_UDP_PORT",
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
DEFAULT_USB_MIXER_CARD = "Device"
USB_AGC_CONTROL = "Auto Gain Control"


def _plain_alsa_card_id(value: str) -> bool:
    return bool(value) and not any(ch.isspace() or ch in ":,/" for ch in value)


def chip_ref_pcm_for_env(env: Mapping[str, Any] | None = None) -> str:
    """Return the current XVF USB-IN PCM for corpus chip-ref output."""
    card = ""
    if env:
        card = str(env.get("JASPER_XVF_ALSA_CARD") or "").strip()
        if not card:
            aec_mic = str(env.get("JASPER_AEC_MIC_DEVICE") or "").strip()
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


def _session_aec3_sweep_source(value: str | None = None) -> str:
    """New corpus sessions default the AEC3 sweep to the cheap USB mic."""
    return normalize_aec3_sweep_source(
        value,
        default=DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE,
    )


def _legacy_aec3_sweep_source(value: str | None = None) -> str:
    """Older metadata/env without an explicit source meant XVF."""
    return normalize_aec3_sweep_source(value, default=AEC3_SWEEP_SOURCE_XVF)


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
        "chip_ref_pcm": chip_ref_pcm_for_env(
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


def _env_truthy(value: str | None, *, default: bool = False) -> bool:
    """Parse the bool vocabulary used by jasper-aec-bridge."""
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _read_bridge_env() -> dict[str, str]:
    """Read bridge env as systemd will see it: /etc first, corpus
    wizard file second. Later EnvironmentFile entries win in systemd,
    so the same overlay order is used here for status/prompt logic.
    """
    env: dict[str, str] = {}
    env.update(read_env_file(str(SYSTEM_ENV_PATH)))
    env.update(read_env_file(str(BRIDGE_CORPUS_ENV_PATH)))
    return env


def bridge_output_status() -> dict[str, Any]:
    """Current bridge corpus-output flags, as the UI should present
    them before beginning a session.
    """
    system_env = read_env_file(str(SYSTEM_ENV_PATH))
    corpus_env = read_env_file(str(BRIDGE_CORPUS_ENV_PATH))
    env: dict[str, str] = {}
    env.update(system_env)
    env.update(corpus_env)
    try:
        aec3_sweep_source = _legacy_aec3_sweep_source(
            env.get(AEC3_SWEEP_SOURCE_ENV),
        )
    except Aec3SweepConfigError as e:
        logger.warning(
            "invalid AEC3 sweep source in bridge env: %s; treating as XVF", e,
        )
        aec3_sweep_source = AEC3_SWEEP_SOURCE_XVF
    recorder_outputs = {
        "dtln": _env_truthy(corpus_env.get("JASPER_AEC_DTLN_ENABLED")),
        "ref": _env_truthy(corpus_env.get("JASPER_AEC_CORPUS_REF_ENABLED")),
        "usb": _env_truthy(corpus_env.get("JASPER_AEC_CORPUS_USB_ENABLED")),
        "usb_dtln": _env_truthy(corpus_env.get("JASPER_AEC_CORPUS_USB_DTLN_ENABLED")),
        "chip_aec": _env_truthy(corpus_env.get("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED")),
        "xvf_raw0_webrtc_aec3": _env_truthy(
            corpus_env.get("JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"),
        ),
        "xvf_raw0_dtln": _env_truthy(
            corpus_env.get("JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"),
        ),
        "outputd_ref": bool(corpus_env.get("JASPER_OUTPUTD_CHIP_REF_PCM"))
        and corpus_env.get("JASPER_OUTPUTD_REFERENCE_UDP_TARGET") == OUTPUTD_REF_UDP_TARGET,
        "aec3_sweep": _env_truthy(corpus_env.get(AEC3_SWEEP_ENV_FLAG)),
        "aec3_sweep_source": aec3_sweep_source,
    }
    status = {
        "dtln": _env_truthy(env.get("JASPER_AEC_DTLN_ENABLED")),
        "ref": _env_truthy(env.get("JASPER_AEC_CORPUS_REF_ENABLED")),
        "usb": _env_truthy(env.get("JASPER_AEC_CORPUS_USB_ENABLED")),
        "usb_dtln": _env_truthy(env.get("JASPER_AEC_CORPUS_USB_DTLN_ENABLED")),
        "chip_aec": _env_truthy(env.get("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED")),
        "xvf_raw0_webrtc_aec3": _env_truthy(
            env.get("JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"),
        ),
        "xvf_raw0_dtln": _env_truthy(
            env.get("JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"),
        ),
        "outputd_ref": bool(env.get("JASPER_OUTPUTD_CHIP_REF_PCM"))
        and env.get("JASPER_OUTPUTD_REFERENCE_UDP_TARGET") == OUTPUTD_REF_UDP_TARGET,
        "aec3_sweep": _env_truthy(env.get(AEC3_SWEEP_ENV_FLAG)),
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


def _required_bridge_outputs_for_request(
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
        _session_aec3_sweep_source(aec3_sweep_source)
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


def _missing_bridge_outputs_from_required(
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
    required = _required_bridge_outputs_for_request(
        corpus_profile=corpus_profile,
        include_dtln=include_dtln,
        include_usb_mic=include_usb_mic,
        include_usb_dtln=include_usb_dtln,
        include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
    )
    return _missing_bridge_outputs_from_required(
        required,
        bridge_output_status(),
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
    env = _read_bridge_env()
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


def _read_aec_intent() -> AecIntent:
    """Read production wake/audio intent from the wizard-owned state file."""
    env = read_env_file(str(AEC_MODE_PATH))
    mode = (env.get("JASPER_AEC_MODE") or "auto").strip().strip("'\"") or "auto"
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


def _mic_probe_and_identity() -> tuple[MicProbe, dict[str, Any]]:
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
        identity = {
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
        probe_error=probe_error,
    )
    return probe, identity


def _validation_artifact_summary(
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
    filters = audio_validation.current_artifact_filter_kwargs(
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


def _leg_detail(
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


def _dac_reference_context(
    env: Mapping[str, str],
    bridge_outputs: dict[str, Any],
    *,
    process_env: Mapping[str, str] | None = None,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = validation or _validation_artifact_summary(system_env=env)
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
                "/run/jasper-outputd/control.sock",
                process_env=process_env,
            ),
        },
        "reference": {
            "source": env_value(
                env,
                "JASPER_AEC_REF_SOURCE",
                "alsa",
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
                _leg_detail(
                    leg, ports, aec3_sweep_source=aec3_sweep_source,
                )
                for leg in enabled_legs
            ],
            "capture_plan": capture_plan,
        },
    }
    try:
        intent = _read_aec_intent()
        system_env = read_env_file(str(SYSTEM_ENV_PATH))
        bridge_env = _read_bridge_env()
        runtime = runtime_env_from_mapping(system_env, process_env=os.environ)
        mic_probe, mic_identity = _mic_probe_and_identity()
        bridge_outputs = bridge_output_status()
        chip_gate = _chip_aec_gate_for_status(system_env, intent)
        profile_status = build_audio_profile_status(
            intent,
            runtime,
            mic_probe,
            bridge_active=aec_bridge_active(),
            chip_available=_mic_chip_aec_available(mic_probe),
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
    validation = _validation_artifact_summary(
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
                _leg_detail(
                    leg, ports, aec3_sweep_source=aec3_sweep_source,
                )
                for leg in enabled_legs
            ],
            "chip_aec_config": chip_aec_config,
            "capture_plan": capture_plan,
        },
        "dac_reference": _dac_reference_context(
            merged_env,
            bridge_outputs,
            process_env=os.environ,
            validation=validation,
        ),
        "bridge_outputs": bridge_outputs,
    }


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
        logger.warning(
            "could not reset %s start-limit state: %s",
            BRIDGE_UNIT, reset.get("error") or f"rc={reset.get('rc')}",
        )
    _broker_restart_or_raise(BRIDGE_UNIT, timeout_sec=BRIDGE_RESTART_TIMEOUT_SEC)


def restart_unit(unit: str, timeout_sec: float = BRIDGE_RESTART_TIMEOUT_SEC) -> None:
    _broker_restart_or_raise(unit, timeout_sec=timeout_sec)


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
    env_path = str(BRIDGE_CORPUS_ENV_PATH)
    existed = BRIDGE_CORPUS_ENV_PATH.exists()
    old_values = read_env_file(env_path)
    had_chip_profile = any(
        old_values.get(key)
        for key in (
            "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED",
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

    if values:
        write_env_file(env_path, values, mode=0o644)
    else:
        delete_env_file(env_path)
    corpus_profile = str(capture_plan.data.get("corpus_profile") or PROFILE_STANDARD)
    try:
        if had_chip_profile or corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
            restart_unit(OUTPUTD_UNIT)
            restart_unit(AEC_INIT_UNIT)
        restart_aec_bridge()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        if existed:
            write_env_file(env_path, old_values, mode=0o644)
        else:
            delete_env_file(env_path)
        try:
            if had_chip_profile or corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
                restart_unit(OUTPUTD_UNIT)
                restart_unit(AEC_INIT_UNIT)
            restart_aec_bridge()
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as rollback_error:
            logger.warning(
                "bridge env rollback restart failed after corpus-output "
                "configure failure: %s",
                rollback_error,
            )
        raise
    return True


def disable_bridge_corpus_outputs() -> bool:
    """Return the bridge to production-light corpus output mode.

    We remove only recorder-owned output overrides so the bridge falls
    back to the reconciler's production intent. This matters for DTLN:
    `JASPER_AEC_DTLN_ENABLED` is also the underlying production wake-leg
    flag written by `jasper-aec-reconcile`, so cleanup must not force it
    off when the /system Wake detection card intentionally enabled it.
    Unrelated settings such as the selected USB mic device are preserved.
    """
    env_path = str(BRIDGE_CORPUS_ENV_PATH)
    existed = BRIDGE_CORPUS_ENV_PATH.exists()
    old_values = read_env_file(env_path)
    had_chip_profile = any(
        old_values.get(key)
        for key in (
            "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED",
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
    if values:
        write_env_file(env_path, values, mode=0o644)
    else:
        delete_env_file(env_path)
    try:
        if had_chip_profile:
            restart_unit(OUTPUTD_UNIT)
            restart_unit(AEC_INIT_UNIT)
        restart_aec_bridge()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        if existed:
            write_env_file(env_path, old_values, mode=0o644)
        else:
            delete_env_file(env_path)
        try:
            if had_chip_profile:
                restart_unit(OUTPUTD_UNIT)
                restart_unit(AEC_INIT_UNIT)
            restart_aec_bridge()
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as rollback_error:
            logger.warning(
                "bridge env rollback restart failed after corpus-output "
                "disable failure: %s",
                rollback_error,
            )
        raise
    return True


def _default_enabled_legs(ports: dict[str, int]) -> tuple[str, ...]:
    """Session default: base production legs that exist in this process."""
    return tuple(leg for leg in BASE_LEGS if leg in ports)


def _session_legs(
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
        _session_aec3_sweep_source(aec3_sweep_source)
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


def _enabled_legs_from_metadata(
    data: dict[str, Any], ports: dict[str, int],
) -> tuple[str, ...]:
    """Recover the session leg set from new or legacy metadata."""
    saved_config = data.get("aec3_sweep_config")
    saved_source = (
        saved_config.get("input_source")
        if isinstance(saved_config, dict) else None
    )
    aec3_sweep_source = _legacy_aec3_sweep_source(
        str(data.get("aec3_sweep_source") or saved_source or ""),
    )
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
            return _session_legs(
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
        inserted_aec3 = False
        for leg in raw_legs:
            if leg in AEC3_SWEEP_LEGS or leg in LEGACY_AEC3_SWEEP_LEGS:
                continue
            if leg not in ports:
                continue
            legs.append(leg)
            if leg == "on" and include_aec3_sweep:
                legs.extend(
                    sweep_leg for sweep_leg in AEC3_SWEEP_LEGS
                    if sweep_leg in ports
                )
                inserted_aec3 = True
        if include_aec3_sweep and not inserted_aec3:
            legs = [
                sweep_leg for sweep_leg in AEC3_SWEEP_LEGS
                if sweep_leg in ports
            ] + legs
        legs = tuple(dict.fromkeys(legs))
        if legs:
            return legs
    return _session_legs(
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


_LEG_PLAN_INFO: dict[str, dict[str, Any]] = {
    "on": {
        "device_id": "xvf3800",
        "device_label": "ReSpeaker XVF3800",
        "native_stream": "chip_asr_beam",
        "source_channel": "asr",
        "processing": "webrtc_aec3",
        "processing_label": "WebRTC AEC3",
        "cost": 2,
        "requires": ("reference",),
    },
    "off": {
        "device_id": "xvf3800",
        "device_label": "ReSpeaker XVF3800",
        "native_stream": "chip_direct_asr",
        "source_channel": "asr",
        "processing": "chip_dsp",
        "processing_label": "chip DSP, no software AEC",
        "cost": 1,
        "requires": (),
    },
    "dtln": {
        "device_id": "xvf3800",
        "device_label": "ReSpeaker XVF3800",
        "native_stream": "chip_direct_asr",
        "source_channel": "asr",
        "processing": "dtln",
        "processing_label": "DTLN neural AEC",
        "cost": 4,
        "requires": ("reference",),
    },
    "raw0": {
        "device_id": "xvf3800",
        "device_label": "ReSpeaker XVF3800",
        "native_stream": "raw_mic_0",
        "source_channel": "chip_channel_2",
        "processing": "none",
        "processing_label": "raw",
        "cost": 1,
        "requires": (),
    },
    "ref": {
        "device_id": "speaker_reference",
        "device_label": "Speaker reference",
        "native_stream": "aec_reference",
        "source_channel": "mono_16khz",
        "processing": "reference",
        "processing_label": "final speaker reference",
        "cost": 1,
        "requires": (),
    },
    "usb_raw": {
        "device_id": "usb_mic",
        "device_label": "USB microphone",
        "native_stream": "usb_raw",
        "source_channel": "mono_capture",
        "processing": "none",
        "processing_label": "raw",
        "cost": 1,
        "requires": ("usb_mic",),
    },
    "usb_webrtc": {
        "device_id": "usb_mic",
        "device_label": "USB microphone",
        "native_stream": "usb_raw",
        "source_channel": "mono_capture",
        "processing": "webrtc_aec3",
        "processing_label": "WebRTC AEC3",
        "cost": 2,
        "requires": ("usb_mic", "reference"),
    },
    "usb_dtln": {
        "device_id": "usb_mic",
        "device_label": "USB microphone",
        "native_stream": "usb_raw",
        "source_channel": "mono_capture",
        "processing": "dtln",
        "processing_label": "DTLN neural AEC",
        "cost": 4,
        "requires": ("usb_mic", "reference"),
    },
    "chip_aec_150": {
        "device_id": "xvf3800",
        "device_label": "ReSpeaker XVF3800",
        "native_stream": "chip_aec_asr_150",
        "source_channel": "fixed_beam_150",
        "processing": "hardware_aec",
        "processing_label": "chip AEC beam 150",
        "cost": 1,
        "requires": ("outputd_reference",),
    },
    "chip_aec_210": {
        "device_id": "xvf3800",
        "device_label": "ReSpeaker XVF3800",
        "native_stream": "chip_aec_asr_210",
        "source_channel": "fixed_beam_210",
        "processing": "hardware_aec",
        "processing_label": "chip AEC beam 210",
        "cost": 1,
        "requires": ("outputd_reference",),
    },
    "xvf_raw0_webrtc_aec3": {
        "device_id": "xvf3800",
        "device_label": "ReSpeaker XVF3800",
        "native_stream": "raw_mic_0",
        "source_channel": "chip_channel_2",
        "processing": "webrtc_aec3",
        "processing_label": "WebRTC AEC3",
        "cost": 2,
        "requires": ("reference",),
    },
    "xvf_raw0_dtln": {
        "device_id": "xvf3800",
        "device_label": "ReSpeaker XVF3800",
        "native_stream": "raw_mic_0",
        "source_channel": "chip_channel_2",
        "processing": "dtln",
        "processing_label": "DTLN neural AEC",
        "cost": 4,
        "requires": ("reference",),
    },
}


def _normalize_chip_primary_leg(value: object) -> str:
    leg = str(value or "").strip()
    return leg if leg in CHIP_AEC_LEGS else "chip_aec_150"


def _metadata_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return parse_env_bool(str(value), default=False)


def _primary_on_leg_overlay(
    *,
    active_audio_profile: Mapping[str, Any] | None,
    runtime_audio_env: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Describe what the stable `on`/`:9876` stream carries today.

    The `on` token is frozen for historical corpus/wake-event compatibility.
    In the default profile it is WebRTC AEC3; in production chip-AEC mode the
    bridge forwards the selected chip beam into the same UDP carrier.
    """
    profile_reports_chip = bool(
        isinstance(active_audio_profile, Mapping)
        and active_audio_profile.get("active")
        in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}
        and active_audio_profile.get("state") == "active"
    )
    runtime_reports_chip = False
    if isinstance(runtime_audio_env, Mapping):
        runtime_reports_chip = bool(
            _metadata_bool(runtime_audio_env.get("chip_enabled"))
            and _metadata_bool(runtime_audio_env.get("bridge_active"))
        )
    if not (profile_reports_chip or runtime_reports_chip):
        return None
    primary_leg = _normalize_chip_primary_leg(
        runtime_audio_env.get("chip_primary_leg")
        if isinstance(runtime_audio_env, Mapping) else None,
    )
    angle = "210" if primary_leg == "chip_aec_210" else "150"
    return {
        "label": f"Chip AEC ASR {angle} primary",
        "kind": wake_legs.LegKind.HARDWARE_AEC.value,
        "native_stream": f"chip_aec_asr_{angle}",
        "source_channel": f"fixed_beam_{angle}",
        "processing": "hardware_aec",
        "processing_label": f"chip AEC beam {angle}",
        "requires": ["outputd_reference"],
        "resource_weight": 1,
        "runtime_role": "production_primary",
        "runtime_primary_leg": primary_leg,
    }


def _capture_plan_runtime_context() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Best-effort runtime overlay for capture-plan labels.

    This is metadata-only. It mirrors the audio-context probe path and must not
    block recording when local env/hardware probes are unavailable.
    """
    try:
        intent = _read_aec_intent()
        system_env = read_env_file(str(SYSTEM_ENV_PATH))
        runtime = runtime_env_from_mapping(system_env, process_env=os.environ)
        mic_probe, _ = _mic_probe_and_identity()
        chip_gate = _chip_aec_gate_for_status(system_env, intent)
        bridge_active = aec_bridge_active()
        profile_status = build_audio_profile_status(
            intent,
            runtime,
            mic_probe,
            bridge_active=bridge_active,
            chip_available=_mic_chip_aec_available(mic_probe),
            chip_gate=chip_gate,
        )
    except Exception as e:  # noqa: BLE001 - advisory metadata only
        log_event(
            logger,
            "wake_corpus.capture_plan_runtime_snapshot_failed",
            error=e,
            level=logging.DEBUG,
        )
        return None, None
    runtime_dict = asdict(runtime)
    runtime_dict["bridge_active"] = bridge_active
    return profile_status["audio_profile"], runtime_dict


def _capture_plan_runtime_snapshot() -> dict[str, Any]:
    """Snapshot hardware/runtime identity used by the capture-plan hash."""

    system_env = read_env_file(str(SYSTEM_ENV_PATH))
    bridge_env = _read_bridge_env()
    merged_env = {**system_env, **bridge_env}
    intent = _read_aec_intent()
    runtime = runtime_env_from_mapping(system_env, process_env=os.environ)
    mic_probe, mic_identity = _mic_probe_and_identity()
    bridge_outputs = bridge_output_status()
    chip_gate = _chip_aec_gate_for_status(system_env, intent)
    bridge_active = aec_bridge_active()
    profile_status = build_audio_profile_status(
        intent,
        runtime,
        mic_probe,
        bridge_active=bridge_active,
        chip_available=_mic_chip_aec_available(mic_probe),
        chip_gate=chip_gate,
    )
    runtime_dict = asdict(runtime)
    runtime_dict["bridge_active"] = bridge_active
    validation = _validation_artifact_summary(
        requested_profile=profile_status["audio_profile"].get("requested"),
        mic_probe=mic_probe,
        system_env=merged_env,
    )
    dac_reference = _dac_reference_context(
        merged_env,
        bridge_outputs,
        process_env=os.environ,
        validation=validation,
    )
    selected_usb_mic = merged_env.get(
        "JASPER_AEC_USB_MIC_DEVICE",
        DEFAULT_USB_MIC_DEVICE,
    )
    mic_fingerprint_source = {
        "family": mic_identity.get("family"),
        "variant_id": mic_identity.get("variant_id"),
        "geometry": mic_identity.get("geometry"),
        "chip_beam_plan": mic_identity.get("chip_beam_plan"),
        "chip_aec_supported": mic_identity.get("chip_aec_supported"),
        "usb_vid_pid": mic_identity.get("usb_vid_pid"),
        "alsa_card": mic_identity.get("alsa_card"),
        "capture_channels": (
            mic_identity.get("observed", {})
            if isinstance(mic_identity.get("observed"), dict) else {}
        ).get("capture_channels"),
        "selected_xvf_mic_device": merged_env.get("JASPER_AEC_MIC_DEVICE", ""),
        "selected_usb_mic_device": selected_usb_mic,
        "chip_primary_leg": merged_env.get("JASPER_AEC_CHIP_AEC_PRIMARY_LEG", ""),
    }
    dac_reference_fingerprint_source = {
        "audio_dac_id": system_env.get("JASPER_AUDIO_DAC_ID", "unknown"),
        "dac": dac_reference.get("dac"),
        "reference": dac_reference.get("reference"),
        "chip_gate": chip_gate,
    }
    return {
        # The builder may overlay its desired recorder-owned bridge env and
        # recompute the identity from these sources before hashing the plan.
        # This keeps "plan to apply" and "plan observed after apply" identical
        # without mutating the live env merely to discover its future hash.
        "identity_recomputable": True,
        "system_env": system_env,
        "bridge_env": bridge_env,
        "merged_env": merged_env,
        "active_audio_profile": profile_status["audio_profile"],
        "runtime_audio_env": runtime_dict,
        "mic_identity": mic_identity,
        "dac_reference": dac_reference,
        "bridge_outputs": bridge_outputs,
        "fingerprint_sources": {
            "mic": mic_fingerprint_source,
            "dac_reference": dac_reference_fingerprint_source,
        },
        "fingerprints": {
            "mic": fingerprint_mapping(mic_fingerprint_source),
            "dac_reference": fingerprint_mapping(
                dac_reference_fingerprint_source,
            ),
        },
    }


def _capture_plan_snapshot_for_desired_env(
    runtime_snapshot: Mapping[str, Any],
    *,
    required_env: Mapping[str, str],
    required_outputs: list[str],
) -> dict[str, Any]:
    """Return the plan identity as it will look after its env is applied.

    Recorder-owned reference and optional-mic env is part of the plan itself.
    Hashing the *currently active* env creates a circular identity: applying the
    plan changes the fingerprint and therefore changes the plan id.  Production
    snapshots carry the raw fingerprint sources, so resolve those sources
    against the desired env once, before the id is assigned.  Synthetic/legacy
    snapshots without that marker retain their supplied fingerprints.
    """

    snapshot = json.loads(json.dumps(dict(runtime_snapshot), default=str))
    if snapshot.get("identity_recomputable") is not True:
        return snapshot
    sources = snapshot.get("fingerprint_sources")
    if not isinstance(sources, Mapping):
        return snapshot
    mic_source_raw = sources.get("mic")
    dac_source_raw = sources.get("dac_reference")
    if not isinstance(mic_source_raw, Mapping) or not isinstance(
        dac_source_raw, Mapping
    ):
        return snapshot

    merged_raw = snapshot.get("merged_env")
    desired_env = dict(merged_raw) if isinstance(merged_raw, Mapping) else {}
    desired_env.update({str(k): str(v) for k, v in required_env.items()})

    desired_outputs_raw = snapshot.get("bridge_outputs")
    desired_outputs = (
        dict(desired_outputs_raw)
        if isinstance(desired_outputs_raw, Mapping)
        else {}
    )
    for output in required_outputs:
        desired_outputs[str(output)] = True

    mic_source = dict(mic_source_raw)
    mic_source.update({
        "selected_xvf_mic_device": desired_env.get("JASPER_AEC_MIC_DEVICE", ""),
        "selected_usb_mic_device": desired_env.get(
            "JASPER_AEC_USB_MIC_DEVICE", DEFAULT_USB_MIC_DEVICE
        ),
        "chip_primary_leg": desired_env.get(
            "JASPER_AEC_CHIP_AEC_PRIMARY_LEG", ""
        ),
    })

    prior_context = snapshot.get("dac_reference")
    prior_validation = (
        prior_context.get("validation")
        if isinstance(prior_context, Mapping)
        and isinstance(prior_context.get("validation"), Mapping)
        else {"status": "unknown"}
    )
    desired_dac_context = _dac_reference_context(
        desired_env,
        desired_outputs,
        process_env={},
        validation=dict(prior_validation),
    )
    dac_source = dict(dac_source_raw)
    dac_source.update({
        "dac": desired_dac_context.get("dac"),
        "reference": desired_dac_context.get("reference"),
    })

    snapshot["merged_env"] = desired_env
    snapshot["bridge_outputs"] = desired_outputs
    snapshot["dac_reference"] = desired_dac_context
    snapshot["fingerprint_sources"] = {
        **dict(sources),
        "mic": mic_source,
        "dac_reference": dac_source,
    }
    snapshot["fingerprints"] = {
        "mic": fingerprint_mapping(mic_source),
        "dac_reference": fingerprint_mapping(dac_source),
    }
    return snapshot


def _bridge_env_overrides_for_request(
    *,
    system_env: Mapping[str, str],
    merged_env: Mapping[str, str],
    corpus_profile: str,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool,
    include_aec3_sweep: bool,
    aec3_sweep_source: str,
) -> dict[str, str]:
    values: dict[str, str] = {}
    if include_dtln and not _env_truthy(system_env.get("JASPER_AEC_DTLN_ENABLED")):
        values["JASPER_AEC_DTLN_ENABLED"] = "1"
    elif (
        (include_aec3_sweep or corpus_profile == PROFILE_CHIP_AEC_COMPARISON)
        and not include_dtln
    ):
        values["JASPER_AEC_DTLN_ENABLED"] = "0"

    sweep_needs_usb = (
        include_aec3_sweep and aec3_sweep_source == AEC3_SWEEP_SOURCE_USB
    )
    needs_ref = corpus_profile == PROFILE_CHIP_AEC_COMPARISON
    needs_usb = include_usb_mic or include_usb_dtln or sweep_needs_usb
    if needs_ref or needs_usb:
        values["JASPER_AEC_CORPUS_REF_ENABLED"] = "1"
    if needs_usb:
        values["JASPER_AEC_CORPUS_USB_ENABLED"] = "1"
        if "JASPER_AEC_USB_MIC_DEVICE" not in system_env:
            values["JASPER_AEC_USB_MIC_DEVICE"] = merged_env.get(
                "JASPER_AEC_USB_MIC_DEVICE",
                DEFAULT_USB_MIC_DEVICE,
            )
    if include_usb_dtln:
        values["JASPER_AEC_CORPUS_USB_DTLN_ENABLED"] = "1"
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        values["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] = "1"
        values["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] = "1"
        values["JASPER_AEC_REF_SOURCE"] = "outputd_udp"
        values["JASPER_AEC_OUTPUTD_REF_UDP_HOST"] = "127.0.0.1"
        values["JASPER_AEC_OUTPUTD_REF_UDP_PORT"] = OUTPUTD_REF_UDP_PORT
        values["JASPER_OUTPUTD_CHIP_REF_PCM"] = chip_ref_pcm_for_env(system_env)
        values["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] = OUTPUTD_REF_UDP_TARGET
        values["JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE"] = DEFAULT_CHIP_REF_SAMPLE_RATE
        values["JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES"] = DEFAULT_CHIP_REF_PERIOD_FRAMES
        values["JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES"] = DEFAULT_CHIP_REF_BUFFER_FRAMES
    if include_xvf_raw0_dtln:
        values["JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"] = "1"
    if include_aec3_sweep:
        values[AEC3_SWEEP_ENV_FLAG] = "1"
        values[AEC3_SWEEP_SOURCE_ENV] = aec3_sweep_source
    return values


def _capture_plan_leg_detail(
    leg: str,
    ports: dict[str, int],
    *,
    aec3_sweep_source: str,
    active_audio_profile: Mapping[str, Any] | None = None,
    runtime_audio_env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if leg in AEC3_SWEEP_LEGS or leg in LEGACY_AEC3_SWEEP_LEGS:
        source_device = (
            "usb_mic" if aec3_sweep_source == AEC3_SWEEP_SOURCE_USB else "xvf3800"
        )
        source_label = (
            "USB microphone" if source_device == "usb_mic" else "ReSpeaker XVF3800"
        )
        source_channel = (
            "mono_capture" if source_device == "usb_mic" else "chip_asr_beam"
        )
        return {
            **_leg_detail(leg, ports, aec3_sweep_source=aec3_sweep_source),
            "device_id": source_device,
            "device_label": source_label,
            "native_stream": f"{source_device}_aec3_sweep_source",
            "source_channel": source_channel,
            "processing": "webrtc_aec3_sweep",
            "processing_label": "WebRTC AEC3 sweep variant",
            "requires": ["reference"] + (
                ["usb_mic"] if source_device == "usb_mic" else []
            ),
            "resource_weight": 2,
        }

    info = _LEG_PLAN_INFO.get(leg, {})
    detail = _leg_detail(leg, ports, aec3_sweep_source=aec3_sweep_source)
    if leg == "on":
        overlay = _primary_on_leg_overlay(
            active_audio_profile=active_audio_profile,
            runtime_audio_env=runtime_audio_env,
        )
        if overlay is not None:
            return {
                **detail,
                "device_id": info.get("device_id", "unknown"),
                "device_label": info.get("device_label", "Unknown source"),
                **overlay,
            }
    return {
        **detail,
        "device_id": info.get("device_id", "unknown"),
        "device_label": info.get("device_label", "Unknown source"),
        "native_stream": info.get("native_stream", "unknown"),
        "source_channel": info.get("source_channel", "unknown"),
        "processing": info.get("processing", detail["kind"]),
        "processing_label": info.get("processing_label", detail["kind"]),
        "requires": list(info.get("requires", ())),
        "resource_weight": int(info.get("cost", 1)),
    }


def _resource_level(total_weight: int) -> str:
    if total_weight <= 6:
        return "low"
    if total_weight <= 10:
        return "medium"
    if total_weight <= 15:
        return "high"
    return "unsafe"


def _capture_plan_recipe(
    *,
    corpus_profile: str,
    include_aec3_sweep: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool,
) -> str:
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        if include_usb_mic or include_usb_dtln or include_xvf_raw0_dtln:
            return "chip_aec_comparison_extended"
        return "chip_aec_comparison"
    if include_aec3_sweep:
        return "software_aec3_sweep"
    if include_usb_mic or include_usb_dtln:
        return "two_mic_comparison"
    return "single_mic_comparison"


def _capture_plan_from_legs(
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
    missing_bridge_outputs: list[str] | None = None,
    required_bridge_outputs: list[str] | None = None,
    required_bridge_env: Mapping[str, str] | None = None,
    runtime_snapshot: Mapping[str, Any] | None = None,
    plan_state: str = CAPTURE_PLAN_STATE_PREVIEW,
    active_audio_profile: Mapping[str, Any] | None = None,
    runtime_audio_env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    leg_details = [
        _capture_plan_leg_detail(
            leg,
            ports,
            aec3_sweep_source=aec3_sweep_source,
            active_audio_profile=active_audio_profile,
            runtime_audio_env=runtime_audio_env,
        )
        for leg in enabled_legs
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for detail in leg_details:
        device_id = str(detail["device_id"])
        device = grouped.setdefault(
            device_id,
            {
                "device_id": device_id,
                "label": detail["device_label"],
                "kind": (
                    "reference" if device_id == "speaker_reference" else "microphone"
                ),
                "legs": [],
            },
        )
        device["legs"].append(detail["token"])

    mic_ids = [
        device_id for device_id, device in grouped.items()
        if device.get("kind") == "microphone"
    ]
    total_weight = sum(int(detail["resource_weight"]) for detail in leg_details)
    resource_level = _resource_level(total_weight)
    dtln_legs = [
        detail["token"] for detail in leg_details
        if detail.get("processing") == "dtln"
    ]
    software_aec_legs = [
        detail["token"] for detail in leg_details
        if str(detail.get("processing", "")).startswith("webrtc_aec3")
    ]
    warnings: list[str] = []
    if len(mic_ids) > 1:
        warnings.append(
            "Recording multiple microphones is useful for comparison, but "
            "it increases bridge fan-out and file count.",
        )
    if len(dtln_legs) > 1:
        warnings.append(
            "Multiple DTLN legs are CPU/RAM heavy on small Pis; review "
            "capture_health before using these clips for training.",
        )
    if include_aec3_sweep:
        warnings.append(
            "AEC3 sweep records several software-AEC variants from one "
            "source; leave DTLN off unless you are intentionally stress-testing.",
        )
    if resource_level == "high":
        warnings.append(
            "This is a high-load capture plan. Watch for warnings or "
            "compromised capture_health before trusting the session.",
        )
    elif resource_level == "unsafe":
        warnings.append(
            "This capture plan is likely too heavy for a 1 GB Pi. Prefer "
            "a smaller comparison set or record in separate sessions.",
        )
    if missing_bridge_outputs:
        labels = [BRIDGE_OUTPUT_LABELS.get(key, key) for key in missing_bridge_outputs]
        warnings.append(
            "The bridge is not currently emitting required output(s): "
            + ", ".join(labels),
        )

    fingerprints = {}
    fingerprint_sources = {}
    if isinstance(runtime_snapshot, Mapping):
        raw_fingerprints = runtime_snapshot.get("fingerprints")
        if isinstance(raw_fingerprints, Mapping):
            fingerprints = dict(raw_fingerprints)
        raw_sources = runtime_snapshot.get("fingerprint_sources")
        if isinstance(raw_sources, Mapping):
            fingerprint_sources = dict(raw_sources)

    plan = {
        "schema_version": CAPTURE_PLAN_SCHEMA_VERSION,
        "state": plan_state,
        "recipe": _capture_plan_recipe(
            corpus_profile=corpus_profile,
            include_aec3_sweep=include_aec3_sweep,
            include_usb_mic=include_usb_mic,
            include_usb_dtln=include_usb_dtln,
            include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        ),
        "corpus_profile": corpus_profile,
        "selected_legs": list(enabled_legs),
        "expected_emitted_legs": list(enabled_legs),
        "selected_physical_mics": mic_ids,
        "devices": list(grouped.values()),
        "legs": leg_details,
        "software_transforms": {
            "webrtc_aec3": software_aec_legs,
            "dtln": dtln_legs,
        },
        "resource": {
            "weight": total_weight,
            "level": resource_level,
            "warning_count": len(warnings),
        },
        "bridge": {
            "required_outputs": list(required_bridge_outputs or []),
            "required_env": dict(required_bridge_env or {}),
            "missing_outputs": list(missing_bridge_outputs or []),
        },
        "required_bridge_outputs": list(required_bridge_outputs or []),
        "required_bridge_env": dict(required_bridge_env or {}),
        "fingerprints": fingerprints,
        "fingerprint_sources": fingerprint_sources,
        "flags": {
            "include_raw_mic_0": include_raw_mic_0,
            "include_dtln": include_dtln,
            "include_usb_mic": include_usb_mic,
            "include_usb_dtln": include_usb_dtln,
            "include_xvf_raw0_dtln": include_xvf_raw0_dtln,
            "include_aec3_sweep": include_aec3_sweep,
            "aec3_sweep_source": aec3_sweep_source,
        },
        "warnings": warnings,
    }
    return WakeCorpusCapturePlan.from_mapping(plan, assign_plan_id=True).to_json()


def build_capture_plan(
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
    include_bridge_readiness: bool = True,
    include_runtime_profile: bool = False,
    active_audio_profile: Mapping[str, Any] | None = None,
    runtime_audio_env: Mapping[str, Any] | None = None,
    runtime_snapshot: Mapping[str, Any] | None = None,
    plan_state: str = CAPTURE_PLAN_STATE_PREVIEW,
) -> dict[str, Any]:
    """Return the layered mic/channel/transform plan for a session request.

    This is the authoritative interpretation layer for the web UI and
    metadata: physical microphones expose native streams, JTS optionally
    derives software-AEC/DTLN legs, and the plan records resource cost
    plus bridge-output readiness without starting any capture.
    """
    if corpus_profile not in CORPUS_PROFILES:
        raise ValueError(f"unknown corpus_profile: {corpus_profile!r}")
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        include_raw_mic_0 = True
        include_dtln = False
        include_aec3_sweep = False
        sweep_source = AEC3_SWEEP_SOURCE_XVF
    else:
        sweep_source = (
            _session_aec3_sweep_source(aec3_sweep_source)
            if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
        )
    effective_include_usb_mic = include_usb_mic or (
        include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
    )
    if runtime_snapshot is None:
        try:
            runtime_snapshot = _capture_plan_runtime_snapshot()
        except _CAPTURE_PLAN_PROBE_ERRORS as e:
            log_event(
                logger,
                "wake_corpus.capture_plan_identity_snapshot_failed",
                error=e,
                level=logging.DEBUG,
            )
            runtime_snapshot = {}
    system_env = (
        runtime_snapshot.get("system_env", {})
        if isinstance(runtime_snapshot, Mapping) else {}
    )
    merged_env = (
        runtime_snapshot.get("merged_env", {})
        if isinstance(runtime_snapshot, Mapping) else {}
    )
    if not isinstance(system_env, Mapping):
        system_env = {}
    if not isinstance(merged_env, Mapping):
        merged_env = {}
    enabled_legs = _session_legs(
        ports,
        corpus_profile=corpus_profile,
        include_dtln=include_dtln,
        include_raw_mic_0=include_raw_mic_0,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=include_usb_dtln,
        include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
    )
    required_outputs = _required_bridge_outputs_for_request(
        corpus_profile=corpus_profile,
        include_dtln=include_dtln,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=include_usb_dtln,
        include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
    )
    bridge_outputs = (
        runtime_snapshot.get("bridge_outputs", {})
        if isinstance(runtime_snapshot, Mapping) else {}
    )
    if not isinstance(bridge_outputs, Mapping):
        bridge_outputs = {}
    missing = (
        _missing_bridge_outputs_from_required(
            required_outputs,
            bridge_outputs or bridge_output_status(),
            aec3_sweep_source=sweep_source,
        )
        if include_bridge_readiness else []
    )
    if include_runtime_profile and (
        active_audio_profile is None or runtime_audio_env is None
    ):
        active_audio_profile = (
            runtime_snapshot.get("active_audio_profile")
            if isinstance(runtime_snapshot, Mapping) else None
        )
        runtime_audio_env = (
            runtime_snapshot.get("runtime_audio_env")
            if isinstance(runtime_snapshot, Mapping) else None
        )
        if active_audio_profile is None or runtime_audio_env is None:
            active_audio_profile, runtime_audio_env = _capture_plan_runtime_context()
    required_env = _bridge_env_overrides_for_request(
        system_env=system_env,
        merged_env=merged_env,
        corpus_profile=corpus_profile,
        include_dtln=DTLN_LEG in enabled_legs,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=USB_DTLN_LEG in enabled_legs,
        include_xvf_raw0_dtln=XVF_RAW0_DTLN_LEG in enabled_legs,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
    )
    runtime_snapshot = _capture_plan_snapshot_for_desired_env(
        runtime_snapshot,
        required_env=required_env,
        required_outputs=required_outputs,
    )
    return _capture_plan_from_legs(
        corpus_profile=corpus_profile,
        enabled_legs=enabled_legs,
        ports=ports,
        include_raw_mic_0=RAW0_LEG in enabled_legs,
        include_dtln=DTLN_LEG in enabled_legs,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=USB_DTLN_LEG in enabled_legs,
        include_xvf_raw0_dtln=XVF_RAW0_DTLN_LEG in enabled_legs,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
        missing_bridge_outputs=missing,
        required_bridge_outputs=required_outputs,
        required_bridge_env=required_env,
        runtime_snapshot=runtime_snapshot,
        plan_state=plan_state,
        active_audio_profile=active_audio_profile,
        runtime_audio_env=runtime_audio_env,
    )


def validate_active_capture_plan(
    plan: WakeCorpusCapturePlan | Mapping[str, Any],
    bridge_stats: Mapping[str, Any] | None = None,
    runtime_snapshot: Mapping[str, Any] | None = None,
) -> PlanConformance:
    """Validate that the running bridge conforms to a stored capture plan."""

    capture_plan = (
        plan if isinstance(plan, WakeCorpusCapturePlan)
        else WakeCorpusCapturePlan.from_mapping(plan)
    )
    if not capture_plan.plan_id:
        return PlanConformance(
            ok=False,
            status="legacy_plan",
            errors=[
                "session metadata predates the capture-plan contract; "
                "start a fresh wake-corpus session before appending clips",
            ],
        )
    if bridge_stats is None:
        bridge_stats = read_bridge_stats_snapshot()
    if not isinstance(bridge_stats, Mapping):
        return PlanConformance(
            ok=False,
            status="bridge_stats_unavailable",
            expected_plan_id=capture_plan.plan_id,
            errors=["aec bridge stats are unavailable"],
        )
    active = bridge_stats.get("active_capture_plan")
    if not isinstance(active, Mapping):
        active = {}
    active_plan_id = str(
        active.get("wake_corpus_plan_id")
        or bridge_stats.get("wake_corpus_plan_id")
        or "",
    )
    raw_emitted = active.get("emitted_legs") or bridge_stats.get("emitted_legs") or []
    emitted_legs = (
        [str(leg) for leg in raw_emitted]
        if isinstance(raw_emitted, list) else []
    )
    expected_legs = list(capture_plan.expected_emitted_legs)
    missing_legs = [leg for leg in expected_legs if leg not in emitted_legs]
    errors: list[str] = []
    warnings: list[str] = []
    if active_plan_id != capture_plan.plan_id:
        errors.append(
            "aec bridge is running a different wake-corpus plan "
            f"(active={active_plan_id or 'none'}, expected={capture_plan.plan_id})",
        )
    if missing_legs:
        errors.append(
            "aec bridge is not emitting promised leg(s): "
            + ", ".join(missing_legs),
        )

    if runtime_snapshot is None:
        try:
            runtime_snapshot = _capture_plan_runtime_snapshot()
        except _CAPTURE_PLAN_PROBE_ERRORS as e:
            runtime_snapshot = {}
            errors.append(f"could not fingerprint current mic/DAC runtime: {e}")
    fingerprints = (
        runtime_snapshot.get("fingerprints", {})
        if isinstance(runtime_snapshot, Mapping) else {}
    )
    if not isinstance(fingerprints, Mapping):
        fingerprints = {}
    mismatches: list[str] = []
    current_mic = str(fingerprints.get("mic") or "")
    if capture_plan.mic_fingerprint and current_mic:
        if current_mic != capture_plan.mic_fingerprint:
            mismatches.append("mic")
    current_dac = str(fingerprints.get("dac_reference") or "")
    if capture_plan.dac_reference_fingerprint and current_dac:
        if current_dac != capture_plan.dac_reference_fingerprint:
            mismatches.append("dac_reference")
    if mismatches:
        errors.append(
            "mic/DAC runtime changed after the session plan was built: "
            + ", ".join(mismatches),
        )
    if not capture_plan.mic_fingerprint or not capture_plan.dac_reference_fingerprint:
        warnings.append("stored plan has incomplete runtime fingerprints")

    ok = not errors
    return PlanConformance(
        ok=ok,
        status="ok" if ok else "blocked",
        active_plan_id=active_plan_id,
        expected_plan_id=capture_plan.plan_id,
        emitted_legs=emitted_legs,
        missing_emitted_legs=missing_legs,
        fingerprint_mismatches=mismatches,
        errors=errors,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Voice-daemon control — same systemctl helpers as wake-enroll
# ---------------------------------------------------------------------------


def _systemd_unit_active(unit: str) -> bool:
    """Return whether *unit* is active, with bounded systemd I/O.

    Only stable, recognized states authorize a state-changing caller to
    continue.  Spawn, timeout, manager, permission, transitional, and
    otherwise unrecognized responses raise so those callers can fail closed.
    Observational callers use the public wrappers below.
    """
    rc = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=_UNIT_STATE_TIMEOUT_SEC,
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


def voice_daemon_active() -> bool:
    """True if jasper-voice is active; fail soft for status snapshots."""
    try:
        return _systemd_unit_active(VOICE_UNIT)
    except (OSError, subprocess.TimeoutExpired):
        return False


def aec_bridge_active() -> bool:
    """True if jasper-aec-bridge is active, for metadata snapshots only."""
    try:
        return _systemd_unit_active(BRIDGE_UNIT)
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
        voice_was_active = _systemd_unit_active(VOICE_UNIT)
    except subprocess.TimeoutExpired as e:
        raise OSError(
            f"could not determine whether {VOICE_UNIT} is active: "
            f"systemctl probe timed out after {_UNIT_STATE_TIMEOUT_SEC:g}s",
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
                logger.warning(
                    "failed to restart jasper-voice after corpus test-mode "
                    "entry failed: %s",
                    start_error,
                )
        raise


def exit_corpus_test_mode() -> None:
    """Disable recorder-owned bridge outputs and restart jasper-voice."""
    disable_bridge_corpus_outputs()
    set_voice_daemon_state("start")
