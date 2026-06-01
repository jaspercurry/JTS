"""jasper-wake-corpus-web — Browser-based corpus recording UI.

Open-ended recording (click to start, click to stop) for building the
gold corpus described in `docs/HANDOFF-wake-training-experiment.md`
Phase 0b. Much better operator UX than running `jasper-wake-enroll`
30 times across 6 conditions with terminal countdowns.

Mechanics:
  - Single-file HTML+JS frontend (no external assets)
  - stdlib `http.server` backend on a configurable port (default 8782)
  - Recording happens on the server via UdpMicCapture — same UDP
    streams (`:9876` AEC ON + `:9877` raw + `:9878` DTLN if present)
    that `jasper-wake-enroll` uses
  - Sync HTTP handlers bridge to an asyncio loop running in a
    background daemon thread via `run_coroutine_threadsafe`
  - Click-start opens captures, streams frames into per-leg buffers;
    click-stop cancels the streaming, writes WAVs to disk in the
    same layout `jasper-wake-enroll` uses (so downstream tools work
    without modification), records metadata in a per-session JSON
    sidecar

What this preserves vs `jasper-wake-enroll`:
  - File naming convention (`enroll_<member>_<session>_<seq>.aec-<leg>.wav`)
  - Quadrant directory layout (`aec_{on,off,dtln}_{nomusic,music}/`)
  - The need for jasper-voice to be stopped (UDP ports must be free)

What this adds:
  - Per-clip start/stop timestamps + duration in JSON sidecar
  - Per-clip distance tag (near/mid/far) — stored in JSON only, NOT
    in filenames, to keep the directory layout compatible with the
    extract/score/review pipeline. Training tools that want distance-
    aware splits can JOIN via the JSON.
  - In-browser playback (HTML5 audio) for instant verification
  - One-click delete (hard-removes WAVs + marks deleted in metadata)
  - Corpus test-mode control that stops jasper-voice, applies selected
    optional bridge outputs, and restores the production-light state on
    exit

Usage:
  sudo /opt/jasper/.venv/bin/jasper-wake-corpus-web
  # then open http://jts.local:8782/ in any browser on the LAN

  # or override the bind for security:
  sudo jasper-wake-corpus-web --host 127.0.0.1 --port 8782
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import subprocess
import threading
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import numpy as np

from jasper import audio_validation, wake_legs
from jasper.audio_profile_state import (
    AecIntent,
    MicProbe,
    build_audio_profile_status,
    env_value,
    parse_env_bool,
    runtime_env_from_mapping,
)
from jasper.wake_conditions import CONDITIONS, DISTANCES
from jasper.aec_sweep import (
    AEC3_SWEEP_ENV_FLAG,
    AEC3_SWEEP_SOURCE_ENV,
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    AEC3_SWEEP_VARIANTS,
    Aec3SweepConfigError,
    USB_AEC3_CORPUS_LABEL,
    USB_AEC3_SWEEP_BASELINE_LABEL,
    config_metadata,
    normalize_aec3_sweep_source,
    variant_metadata,
)
# Reuse audio I/O + systemctl helpers from the CLI. Single source of
# truth for the WAV format + the "stop jasper-voice to free UDP" dance.
from jasper.cli.wake_enroll import (
    CHANNELS,  # noqa: F401 - re-exported for recorder tests/consumers
    SAMPLE_RATE_HZ,  # noqa: F401 - re-exported for recorder tests/consumers
    SAMPLE_WIDTH_BYTES,  # noqa: F401 - re-exported for recorder tests/consumers
    VOICE_UNIT,
    require_root,
    write_wav,
)
from jasper.wake_ports import (
    DEFAULT_AEC_CHIP_AEC_150_PORT,
    DEFAULT_AEC_CHIP_AEC_210_PORT,
    DEFAULT_AEC_DTLN_PORT,
    DEFAULT_AEC_OFF_PORT,
    DEFAULT_AEC_ON_PORT,
    DEFAULT_AEC_REF_PORT,
    DEFAULT_AEC_RAW0_PORT,
    DEFAULT_AEC_XVF_RAW0_DTLN_PORT,
    DEFAULT_AEC_XVF_RAW0_WEBRTC_AEC3_PORT,
    DEFAULT_AEC3_SWEEP_PORTS,
    DEFAULT_AEC_USB_DTLN_PORT,
    DEFAULT_AEC_USB_RAW_PORT,
    DEFAULT_AEC_USB_WEBRTC_PORT,
    build_ports,
)

# Canonical design system. The recorder renders its document shell through
# `canonical_page()` (the shared /assets/app.css look) with `canonical_header()`
# for the top bar — the same seam every migrated wizard reuses. Page behaviour
# lives in the static ES module at /assets/wake-corpus/js/main.js (loaded as
# `type="module"`), and the bespoke recorder visuals live in the page
# stylesheet at /assets/wake-corpus/wake-corpus.css; the "← Home" link and the
# confirm dialog are provided by the canonical header and the shared
# dialog.js module respectively, so this page no longer embeds the legacy
# NAV_BACK_* / DIALOG_CSS / dialog_helpers_js inline twins.
from jasper.web._common import (
    canonical_header,
    canonical_page,
    delete_env_file,
    read_env_file,
    write_env_file,
)

logger = logging.getLogger("jasper-wake-corpus-web")


# Default bind. Loopback by default for safety; CLI flag opens to LAN.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8782

DEFAULT_OUTPUT_DIR = Path("data/enrollment_positives")
DEFAULT_METADATA_SUBDIR = "metadata"
ACTIVE_SESSION_MARKER = ".active_session.json"

# CONDITIONS / DISTANCES are the operator-labelled input domains, imported
# above from the shared single source of truth (jasper.wake_conditions) so
# the corpus, the runtime fuser, and the wake telemetry agree on one
# taxonomy. The wizard validates strictly against them to reject typos;
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
    "usb_raw",
    "usb_webrtc",
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

# Hard cap so a forgotten "stop" doesn't fill memory with a 1-hour
# buffer. The server auto-stops at this duration with a flag in the
# metadata so the operator notices.
MAX_RECORDING_DURATION_SEC = 30.0

# How long after the last clip's metadata-file mtime we'll still
# resume a session on backend startup. Set to 1 hour so a quick crash-
# and-restart picks up cleanly, but a session abandoned overnight
# doesn't surprise the operator the next day with "wait, why does the
# UI show clips from yesterday?"
RESUME_WINDOW_SEC = 3600.0

# CSRF header name. Matches common JS framework conventions; the
# embedded JS reads `<meta name="csrf-token">` and sends this header
# on every mutating request.
CSRF_HEADER = "X-CSRF-Token"

# Bridge-side corpus-output config. The web service is intentionally
# sandboxed away from /etc/jasper/jasper.env, so operator-driven corpus
# experiment flags live in /var/lib/jasper like the other wizard-owned
# env files.
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
METADATA_SCHEMA_VERSION = 2
AUDIO_CONTEXT_SCHEMA_VERSION = 1
BRIDGE_UNIT = "jasper-aec-bridge.service"
OUTPUTD_UNIT = "jasper-outputd.service"
AEC_INIT_UNIT = "jasper-aec-init.service"
BRIDGE_RESTART_TIMEOUT_SEC = 30.0
BRIDGE_CORPUS_OUTPUT_VARS = (
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
    return {
        "schema_version": 1,
        "reference_topology": "outputd_direct_fanout",
        "outputd_reference_udp_target": OUTPUTD_REF_UDP_TARGET,
        "chip_ref_pcm": DEFAULT_CHIP_REF_PCM,
        "chip_ref_sample_rate": int(DEFAULT_CHIP_REF_SAMPLE_RATE),
        "chip_ref_period_frames": int(DEFAULT_CHIP_REF_PERIOD_FRAMES),
        "chip_ref_buffer_frames": int(DEFAULT_CHIP_REF_BUFFER_FRAMES),
        "SHF_BYPASS": 0,
        "AEC_ASROUTONOFF": 1,
        "AEC_ASROUTGAIN": 1.0,
        "AEC_FIXEDBEAMSONOFF": 1,
        "AEC_FIXEDBEAMSGATING": 1,
        "AEC_FIXEDBEAMSAZIMUTH_VALUES": [2.61799, 3.66519],
        "AEC_FIXEDBEAMSELEVATION_VALUES": [0.0, 0.0],
        "AEC_AECEMPHASISONOFF": 2,
        "AEC_FAR_EXTGAIN": 0.0,
        "AUDIO_MGR_OP_L": [7, 0],
        "AUDIO_MGR_OP_R": [7, 1],
        "beams": [
            {"leg": "chip_aec_150", "angle_deg": 150},
            {"leg": "chip_aec_210", "angle_deg": 210},
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
    status["active"] = any(key in corpus_env for key in BRIDGE_CORPUS_OUTPUT_VARS)
    return status


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
    status = bridge_output_status()
    sweep_source = (
        _session_aec3_sweep_source(aec3_sweep_source)
        if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
    )
    sweep_needs_usb = (
        include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
    )
    missing: list[str] = []
    if include_dtln and not status["dtln"]:
        missing.append("dtln")
    if include_usb_mic or include_usb_dtln or sweep_needs_usb:
        if not status["ref"]:
            missing.append("ref")
        if not status["usb"]:
            missing.append("usb")
    if include_usb_dtln and not status["usb_dtln"]:
        missing.append("usb_dtln")
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        if not status["chip_aec"]:
            missing.append("chip_aec")
        if not status["xvf_raw0_webrtc_aec3"]:
            missing.append("xvf_raw0_webrtc_aec3")
        if not status["outputd_ref"]:
            missing.append("outputd_ref")
    if include_xvf_raw0_dtln and not status["xvf_raw0_dtln"]:
        missing.append("xvf_raw0_dtln")
    if include_aec3_sweep and (
        not status["aec3_sweep"]
        or status.get("aec3_sweep_source") != sweep_source
    ):
        missing.append("aec3_sweep")
    return missing


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
    )


def _mic_probe_and_identity() -> tuple[MicProbe, dict[str, Any]]:
    """Cheap mic identity snapshot for corpus metadata.

    This mirrors the `/wake/` status probe: no streaming audio, no chip
    writes, just the XVF USB/card facts already used for profile truth.
    """
    try:
        from jasper.mics import xvf3800

        xvf_present = xvf3800.is_present()
        capture_channels = xvf3800.capture_channels()
        recommended_channels = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
        probe_error = None
        identity = {
            "family": (
                "xvf3800"
                if xvf_present or capture_channels is not None else "unknown"
            ),
            "display_name": xvf3800.DISPLAY_NAME,
            "usb_vid_pid": xvf3800.USB_VID_PID,
            "alsa_card": xvf3800.ALSA_CARD_NAME,
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
        probe_error=probe_error,
    )
    return probe, identity


def _validation_artifact_summary(
    profile_status: Mapping[str, Any],
    *,
    system_env: Mapping[str, str],
    path: Path | None = None,
) -> dict[str, Any]:
    """Read optional profile-validation output, if present.

    The validation stream does not exist yet everywhere, and readiness
    snapshots are advisory. Corpus metadata therefore records a stable
    unknown/missing shape instead of making session creation depend on it.
    """
    path = path or AUDIO_VALIDATION_ARTIFACT_PATH
    return audio_validation.validation_summary_for_profile_status(
        profile_status,
        path=path,
        system_env=system_env,
    )


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
    profile_status: Mapping[str, Any],
    process_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    validation = _validation_artifact_summary(
        profile_status,
        system_env=env,
    )
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
) -> dict[str, Any]:
    """Snapshot production profile truth beside the corpus leg choice.

    This is metadata only. It does not open capture devices, change env
    files, or alter production wake detection.
    """
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fallback = {
        "schema_version": AUDIO_CONTEXT_SCHEMA_VERSION,
        "captured_at": captured_at,
        "status": "unknown",
        "corpus": {
            "profile": corpus_profile,
            "selected_legs": list(enabled_legs),
            "leg_details": [
                _leg_detail(
                    leg, ports, aec3_sweep_source=aec3_sweep_source,
                )
                for leg in enabled_legs
            ],
        },
    }
    try:
        intent = _read_aec_intent()
        system_env = read_env_file(str(SYSTEM_ENV_PATH))
        bridge_env = _read_bridge_env()
        runtime = runtime_env_from_mapping(system_env)
        mic_probe, mic_identity = _mic_probe_and_identity()
        bridge_outputs = bridge_output_status()
        profile_status = build_audio_profile_status(
            intent,
            runtime,
            mic_probe,
            bridge_active=aec_bridge_active(),
            chip_available=(
                mic_probe.xvf_present
                and mic_probe.capture_channels == mic_probe.recommended_channels
            ),
        )
    except Exception as e:  # noqa: BLE001 - metadata must not block recording
        logger.warning("event=wake_corpus.audio_context_snapshot_failed error=%s", e)
        return {**fallback, "error": str(e)}

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
            "leg_details": [
                _leg_detail(
                    leg, ports, aec3_sweep_source=aec3_sweep_source,
                )
                for leg in enabled_legs
            ],
            "chip_aec_config": chip_aec_config,
        },
        "dac_reference": _dac_reference_context(
            {**system_env, **bridge_env},
            bridge_outputs,
            profile_status=profile_status,
            process_env=os.environ,
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


def restart_aec_bridge() -> None:
    """Restart the bridge and wait for systemd to report the outcome.

    This path is only used for the explicit corpus-output enable flow,
    where the operator is waiting to record immediately. A blocking
    restart is better here than a queued `--no-block` restart because
    a missing USB mic or failed DTLN load should stop the session
    before it records silently-missing legs.
    """
    try:
        subprocess.run(
            ["systemctl", "reset-failed", BRIDGE_UNIT],
            check=False,
            timeout=5.0,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("could not reset %s start-limit state: %s", BRIDGE_UNIT, e)
    subprocess.run(
        ["systemctl", "restart", BRIDGE_UNIT],
        check=True,
        timeout=BRIDGE_RESTART_TIMEOUT_SEC,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def restart_unit(unit: str, timeout_sec: float = BRIDGE_RESTART_TIMEOUT_SEC) -> None:
    subprocess.run(
        ["systemctl", "restart", unit],
        check=True,
        timeout=timeout_sec,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def enable_bridge_outputs_for_session(
    *,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_aec3_sweep: bool = False,
    aec3_sweep_source: str | None = None,
) -> None:
    """Persist requested bridge corpus outputs and restart the bridge.

    This function only enables outputs. It deliberately does not turn
    anything off when a later session leaves a box unchecked: disabling
    a live bridge output is a separate operator decision, and the
    recorder can simply ignore legs it is not subscribing to.
    """
    system_env = read_env_file(str(SYSTEM_ENV_PATH))
    env_path = str(BRIDGE_CORPUS_ENV_PATH)
    existed = BRIDGE_CORPUS_ENV_PATH.exists()
    old_values = read_env_file(env_path)
    values = dict(old_values)
    sweep_source = (
        _session_aec3_sweep_source(aec3_sweep_source)
        if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
    )
    sweep_needs_usb = (
        include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
    )

    if include_dtln:
        values["JASPER_AEC_DTLN_ENABLED"] = "1"
    if include_usb_mic or include_usb_dtln or sweep_needs_usb:
        values["JASPER_AEC_CORPUS_REF_ENABLED"] = "1"
        values["JASPER_AEC_CORPUS_USB_ENABLED"] = "1"
        if (
            "JASPER_AEC_USB_MIC_DEVICE" not in values
            and "JASPER_AEC_USB_MIC_DEVICE" not in system_env
        ):
            values["JASPER_AEC_USB_MIC_DEVICE"] = DEFAULT_USB_MIC_DEVICE
    if include_usb_dtln:
        values["JASPER_AEC_CORPUS_USB_DTLN_ENABLED"] = "1"
    if include_aec3_sweep:
        values[AEC3_SWEEP_ENV_FLAG] = "1"
        values[AEC3_SWEEP_SOURCE_ENV] = sweep_source

    write_env_file(env_path, values, mode=0o644)
    try:
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
            restart_aec_bridge()
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as rollback_error:
            logger.warning(
                "bridge env rollback restart failed after corpus-output "
                "enable failure: %s",
                rollback_error,
            )
        raise


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

    Unlike the legacy enable helper, this treats the checkbox selection
    as the desired test-mode bridge state. Production-owned settings in
    /etc or the reconciler env are left alone; the recorder file only
    carries the additional outputs needed for the selected corpus legs.
    Returns True when the bridge was restarted.
    """
    system_env = read_env_file(str(SYSTEM_ENV_PATH))
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
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        include_usb_mic = True
        include_aec3_sweep = False
    sweep_source = (
        _session_aec3_sweep_source(aec3_sweep_source)
        if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
    )
    sweep_needs_usb = (
        include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
    )

    if include_dtln and not _env_truthy(system_env.get("JASPER_AEC_DTLN_ENABLED")):
        values["JASPER_AEC_DTLN_ENABLED"] = "1"
    elif (
        (include_aec3_sweep or corpus_profile == PROFILE_CHIP_AEC_COMPARISON)
        and not include_dtln
    ):
        # AEC3 sweep and chip-AEC comparison are controlled corpus test
        # modes. Temporarily park production DTLN unless the operator
        # explicitly selected the legacy dtln leg; exit removes this
        # override and restores the production intent.
        values["JASPER_AEC_DTLN_ENABLED"] = "0"
    if include_usb_mic or include_usb_dtln or sweep_needs_usb:
        values["JASPER_AEC_CORPUS_REF_ENABLED"] = "1"
        values["JASPER_AEC_CORPUS_USB_ENABLED"] = "1"
        if (
            "JASPER_AEC_USB_MIC_DEVICE" not in values
            and "JASPER_AEC_USB_MIC_DEVICE" not in system_env
        ):
            values["JASPER_AEC_USB_MIC_DEVICE"] = DEFAULT_USB_MIC_DEVICE
    if include_usb_dtln:
        values["JASPER_AEC_CORPUS_USB_DTLN_ENABLED"] = "1"
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        values["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] = "1"
        values["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] = "1"
        values["JASPER_AEC_REF_SOURCE"] = "outputd_udp"
        values["JASPER_AEC_OUTPUTD_REF_UDP_HOST"] = "127.0.0.1"
        values["JASPER_AEC_OUTPUTD_REF_UDP_PORT"] = OUTPUTD_REF_UDP_PORT
        values["JASPER_OUTPUTD_CHIP_REF_PCM"] = DEFAULT_CHIP_REF_PCM
        values["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] = OUTPUTD_REF_UDP_TARGET
        values["JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE"] = DEFAULT_CHIP_REF_SAMPLE_RATE
        values["JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES"] = DEFAULT_CHIP_REF_PERIOD_FRAMES
        values["JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES"] = DEFAULT_CHIP_REF_BUFFER_FRAMES
    if include_xvf_raw0_dtln:
        values["JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"] = "1"
    if include_aec3_sweep:
        values[AEC3_SWEEP_ENV_FLAG] = "1"
        values[AEC3_SWEEP_SOURCE_ENV] = sweep_source

    if values == old_values:
        return False

    if values:
        write_env_file(env_path, values, mode=0o644)
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
        if include_xvf_raw0_dtln and XVF_RAW0_DTLN_LEG in ports:
            legs.append(XVF_RAW0_DTLN_LEG)
        if include_usb_dtln and USB_DTLN_LEG in ports:
            legs.append(USB_DTLN_LEG)
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
        # the caller didn't tick the broader USB/WebRTC checkbox.
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
    """Return a saved checkbox flag, capped to legs this process can record."""
    requested = bool(data.get(key, leg in enabled_legs))
    return requested and leg in enabled_legs


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class ClipMetadata:
    """One recorded clip's complete metadata, written to the per-session
    JSON sidecar. All fields are JSON-serializable.
    """

    clip_id: str
    member: str
    condition: str
    distance: str
    session_id: str
    seq: int
    start_ts: str  # ISO8601 UTC
    stop_ts: str
    duration_sec: float
    files: dict[str, str]  # leg → absolute WAV path
    deleted: bool = False
    auto_stopped: bool = False
    notes: str = ""
    selected_legs: list[str] = field(default_factory=list)
    audio_context: dict[str, Any] = field(default_factory=dict)
    capture_health: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Recording — the actual audio I/O
# ---------------------------------------------------------------------------


def compute_rms_dbfs(frame: np.ndarray) -> float:
    """Return the RMS of an int16 PCM frame in dBFS.

    -100.0 dBFS for near-silent or empty frames (avoids -inf from
    log(0)). 0.0 dBFS = full-scale int16. Used by the SSE level-meter
    endpoint so the UI can show a live "is your voice reaching the
    mic?" bar while recording.
    """
    if len(frame) == 0:
        return -100.0
    mean_sq = float(np.mean(frame.astype(np.float64) ** 2))
    if mean_sq < 1.0:
        return -100.0
    rms = mean_sq ** 0.5
    return 20.0 * float(np.log10(rms / 32768.0))


class RecordingTask:
    """Open-ended audio recording from multiple UDP captures.

    Constructed on each Start click; cancelled on Stop click. Background
    asyncio task streams frames into per-leg buffers. `stop()` cancels
    cleanly + returns the captured PCM bytes per leg.

    Side effect: while recording, updates `current_rms_dbfs` on every
    AEC-ON frame so the SSE level meter can read it. Only the AEC ON
    leg is metered (it's the canonical wake-detection signal); cost
    is one numpy reduction per ~80 ms.

    Memory bound: at 16 kHz mono int16 ≈ 32 KB/s per leg × 3 legs ≈
    96 KB/s. Capped to MAX_RECORDING_DURATION_SEC by the backend, so
    worst-case footprint is bounded.
    """

    def __init__(
        self,
        ports: dict[str, int],
        *,
        aec3_sweep_source: str = AEC3_SWEEP_SOURCE_XVF,
    ) -> None:
        self._ports = ports
        self._aec3_sweep_source = aec3_sweep_source
        self._buffers: dict[str, list[np.ndarray]] = {leg: [] for leg in ports}
        self._captures: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._stack: AsyncExitStack | None = None
        self._start_monotonic: float = 0.0
        self._bridge_stats_start: dict[str, Any] | None = None
        self._bridge_stats_stop: dict[str, Any] | None = None
        # Live RMS of the most recent AEC ON frame, read by the SSE
        # level-meter handler. Written from the asyncio loop thread,
        # read from HTTP handler threads — single-float reads/writes
        # are atomic in CPython so no lock needed.
        self.current_rms_dbfs: float = -100.0

    async def start(self) -> None:
        # Lazy import — keeps this module importable on dev machines
        # that don't have sounddevice / portaudio (UdpMicCapture is
        # pure-asyncio but lives in audio_io which imports sounddevice
        # at the top).
        from jasper.audio_io import UdpMicCapture

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for leg, port in self._ports.items():
                cap = await self._stack.enter_async_context(
                    UdpMicCapture(port=port),
                )
                self._captures[leg] = cap
        except Exception:
            # If any leg fails to bind, clean up the ones that succeeded
            # so the user can retry without a "port already in use"
            # cascade on the next start.
            await self._stack.__aexit__(None, None, None)
            raise

        self._start_monotonic = time.monotonic()
        self._bridge_stats_start = read_bridge_stats_snapshot()
        self._task = asyncio.create_task(self._collect_all())

    async def _collect_all(self) -> None:
        async def _per_leg(leg: str, cap: Any) -> None:
            is_aec_on = (leg == "on")
            async for frame in cap.frames():
                self._buffers[leg].append(frame)
                # Live-meter the AEC ON leg only — it's the canonical
                # wake-detection signal. Single-float atomic write; no
                # lock needed (CPython guarantee).
                if is_aec_on:
                    self.current_rms_dbfs = compute_rms_dbfs(frame)

        await asyncio.gather(*[
            _per_leg(leg, cap) for leg, cap in self._captures.items()
        ])

    def elapsed_sec(self) -> float:
        if self._start_monotonic == 0:
            return 0.0
        return time.monotonic() - self._start_monotonic

    async def stop(self) -> dict[str, bytes]:
        """Cancel the collection task, return PCM bytes per leg.

        Idempotent: calling twice is a no-op on the second call (the
        task + stack sentinels are cleared after first cleanup, so we
        skip both double-await and double-exit which AsyncExitStack
        would error on).
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("recording task raised on cancel: %s", e)
        self._task = None

        result: dict[str, bytes] = {}
        for leg, frames in self._buffers.items():
            if frames:
                pcm = np.concatenate(frames).astype(np.int16).tobytes()
            else:
                pcm = b""
            result[leg] = pcm

        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("cleanup raised: %s", e)
            self._stack = None
        self._bridge_stats_stop = read_bridge_stats_snapshot()
        return result

    def capture_health(self, wall_duration_sec: float) -> dict[str, Any]:
        return build_capture_health(
            wall_duration_sec=wall_duration_sec,
            buffers=self._buffers,
            bridge_start=self._bridge_stats_start,
            bridge_stop=self._bridge_stats_stop,
            aec3_sweep_source=self._aec3_sweep_source,
        )


# ---------------------------------------------------------------------------
# Backend — single-recording state + persistence, thread-safe
# ---------------------------------------------------------------------------


class StateError(RuntimeError):
    """Raised when an operation isn't valid in the current state
    (e.g. starting a recording while one is in progress)."""


class RecordingBackend:
    """Single-recording-at-a-time backend, controllable from sync HTTP
    handlers via a background asyncio event loop.

    Lifecycle:
        backend = RecordingBackend(...)
        backend.start()                     # spins up the loop thread
        backend.begin_session("jasper")
        clip_id = backend.start_recording("quiet", "near")
        ...
        clip_meta = backend.stop_recording()
        backend.delete_clip(clip_id)
        backend.shutdown()                  # joins the loop thread
    """

    def __init__(
        self,
        output_dir: Path,
        ports: dict[str, int] | None = None,
        max_duration_sec: float = MAX_RECORDING_DURATION_SEC,
    ) -> None:
        self._output_dir = output_dir
        self._metadata_dir = output_dir / DEFAULT_METADATA_SUBDIR
        # All known ports. The recorder subscribes to a per-session
        # subset: base production legs by default, raw0 / USB / ref
        # only when the session opted in.
        self._ports = ports or build_ports()
        self._max_duration_sec = max_duration_sec

        # State guarded by _lock. Touched from HTTP handler threads
        # AND from the loop thread (auto-stop timer); the lock makes
        # all observers see consistent state.
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._member: str | None = None
        # Whether THIS session includes the truly-raw mic 0 leg. Set
        # by begin_session(include_raw_mic_0=…); read by
        # start_recording to decide which UDP ports to subscribe to.
        # Per-session (not per-clip) so a session's clips all share
        # the same leg set and downstream training tools can rely on
        # "session contains raw0 → every clip has it."
        self._include_raw_mic_0: bool = False
        self._include_dtln: bool = False
        self._include_usb_mic: bool = False
        self._include_usb_dtln: bool = False
        self._include_xvf_raw0_dtln: bool = False
        self._include_aec3_sweep: bool = False
        self._corpus_profile: str = PROFILE_STANDARD
        self._chip_aec_config: dict[str, object] | None = None
        self._aec3_sweep_source: str = AEC3_SWEEP_SOURCE_XVF
        self._aec3_sweep_variants: list[dict[str, object]] = []
        self._aec3_sweep_config: dict[str, object] | None = None
        self._enabled_legs: tuple[str, ...] = _default_enabled_legs(self._ports)
        self._audio_context: dict[str, Any] | None = None
        self._clips: list[ClipMetadata] = []
        self._current: RecordingTask | None = None
        self._current_clip_id: str | None = None
        self._current_meta: dict[str, str] | None = None  # condition, distance, start_ts
        # Sentinel: set inside _lock when a start_recording call has
        # passed validation but the (slow) RecordingTask.start() hasn't
        # finished yet. Concurrent start attempts see this and refuse
        # with the correct "already in progress" error rather than
        # racing into a UDP-bind-failed error.
        self._starting_clip_id: str | None = None
        self._auto_stop_handle: Any | None = None  # asyncio.TimerHandle

        # Background asyncio loop running in a daemon thread. Lazily
        # created in start() so tests can construct a backend without
        # immediately spawning the thread.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()

    # ----- lifecycle -------------------------------------------------

    def start(self) -> None:
        if self._loop_thread is not None:
            return  # idempotent
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="wake-corpus-loop", daemon=True,
        )
        self._loop_thread.start()
        self._loop_ready.wait()
        # Recover from a previous run only when the prior process left
        # an active-session marker behind. A plain recent metadata file
        # is not enough: after a graceful test-mode exit, reopening the
        # page should feel like a fresh start.
        self._maybe_load_recent_session()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def shutdown(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)

    def _submit(self, coro: Any) -> Any:
        """Run a coroutine on the backend loop, block for the result."""
        if self._loop is None:
            raise RuntimeError("backend not started; call .start() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # ----- session + clip state -------------------------------------

    def session_id(self) -> str | None:
        with self._lock:
            return self._session_id

    def member(self) -> str | None:
        with self._lock:
            return self._member

    def is_recording(self) -> bool:
        with self._lock:
            return self._current is not None

    def get_current_rms_dbfs(self) -> float | None:
        """Latest AEC-ON RMS in dBFS, or None if not recording.

        Read by the /api/recording/level SSE endpoint, called ~12 Hz
        (matches the frame rate). Returns None when no recording is
        in flight; the UI grays out the level bar in that state.
        """
        with self._lock:
            if self._current is None:
                return None
            return self._current.current_rms_dbfs

    # ----- crash recovery -------------------------------------------

    def _active_session_marker_path(self) -> Path:
        return self._metadata_dir / ACTIVE_SESSION_MARKER

    def _write_active_session_marker(self) -> None:
        """Persist the session currently open for appending.

        Metadata files are historical artifacts. This marker is the
        narrow crash-recovery signal: if the web process dies while a
        session is open, startup can reattach; if the operator unloads
        or exits test mode cleanly, the marker is removed.
        """
        with self._lock:
            session_id = self._session_id
            member = self._member
        if session_id is None:
            return
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        path = self._active_session_marker_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = {
            "session_id": session_id,
            "member": member,
            "updated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
        }
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    def _clear_active_session_marker(self) -> None:
        try:
            self._active_session_marker_path().unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning("failed to clear active session marker: %s", e)

    def _clear_session_state_locked(self) -> None:
        self._session_id = None
        self._member = None
        self._clips = []
        self._include_raw_mic_0 = False
        self._include_dtln = False
        self._include_usb_mic = False
        self._include_usb_dtln = False
        self._include_xvf_raw0_dtln = False
        self._include_aec3_sweep = False
        self._corpus_profile = PROFILE_STANDARD
        self._chip_aec_config = None
        self._aec3_sweep_source = AEC3_SWEEP_SOURCE_XVF
        self._aec3_sweep_variants = []
        self._aec3_sweep_config = None
        self._enabled_legs = _default_enabled_legs(self._ports)
        self._audio_context = None

    def _find_session_metadata(self, session_id: str) -> Path | None:
        for p in self._metadata_dir.glob("enroll_*.json"):
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("session_id") == session_id:
                return p
        return None

    def _load_session_data(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            session_id = data["session_id"]
            member = data["member"]
            clips = [
                ClipMetadata(**c) for c in data.get("clips", [])
            ]
        except (KeyError, TypeError) as e:
            raise ValueError(f"session schema mismatch: {e}") from e
        enabled_legs = _enabled_legs_from_metadata(data, self._ports)
        corpus_profile = str(data.get("corpus_profile") or PROFILE_STANDARD)
        if corpus_profile not in CORPUS_PROFILES:
            corpus_profile = PROFILE_STANDARD
        saved_config = data.get("aec3_sweep_config")
        saved_source = (
            saved_config.get("input_source")
            if isinstance(saved_config, dict) else None
        )
        aec3_sweep_source = _legacy_aec3_sweep_source(
            str(data.get("aec3_sweep_source") or saved_source or ""),
        )
        include_raw_mic_0 = RAW0_LEG in enabled_legs
        include_usb_mic = bool(
            data.get(
                "include_usb_mic",
                any(leg in USB_CORPUS_LEGS for leg in enabled_legs),
            )
            or (
                aec3_sweep_source == AEC3_SWEEP_SOURCE_USB
                and any(leg in AEC3_SWEEP_LEGS for leg in enabled_legs)
            )
        )
        include_aec3_sweep = (
            bool(data.get("include_aec3_sweep", False))
            or any(leg in enabled_legs for leg in AEC3_SWEEP_LEGS)
        )
        saved_variants = data.get("aec3_sweep_variants")
        if not isinstance(saved_variants, list):
            saved_variants = []
        if not isinstance(saved_config, dict):
            saved_config = None
        if include_aec3_sweep and not saved_variants:
            saved_variants = variant_metadata(input_source=aec3_sweep_source)
            saved_config = config_metadata(input_source=aec3_sweep_source)
        elif include_aec3_sweep and saved_config is not None:
            saved_config = dict(saved_config)
            saved_config.setdefault("input_source", aec3_sweep_source)
        include_dtln = _metadata_flag(data, "include_dtln", DTLN_LEG, enabled_legs)
        include_usb_dtln = _metadata_flag(
            data, "include_usb_dtln", USB_DTLN_LEG, enabled_legs,
        )
        include_xvf_raw0_dtln = _metadata_flag(
            data, "include_xvf_raw0_dtln", XVF_RAW0_DTLN_LEG, enabled_legs,
        )
        chip_config = data.get("chip_aec_config")
        if not isinstance(chip_config, dict):
            chip_config = (
                chip_aec_config_metadata()
                if corpus_profile == PROFILE_CHIP_AEC_COMPARISON else None
            )
        audio_context = data.get("audio_context")
        if not isinstance(audio_context, dict):
            audio_context = None
        with self._lock:
            self._session_id = session_id
            self._member = member
            self._clips = clips
            self._include_raw_mic_0 = RAW0_LEG in enabled_legs
            self._include_dtln = include_dtln
            self._include_usb_mic = include_usb_mic
            self._include_usb_dtln = include_usb_dtln
            self._include_xvf_raw0_dtln = include_xvf_raw0_dtln
            self._include_aec3_sweep = include_aec3_sweep
            self._corpus_profile = corpus_profile
            self._chip_aec_config = chip_config
            self._aec3_sweep_source = aec3_sweep_source
            self._aec3_sweep_variants = saved_variants
            self._aec3_sweep_config = saved_config
            self._enabled_legs = enabled_legs
            self._audio_context = audio_context
        return {
            "session_id": session_id,
            "member": member,
            "clip_count": sum(1 for c in clips if not c.deleted),
            "include_raw_mic_0": include_raw_mic_0,
            "include_dtln": include_dtln,
            "include_usb_mic": include_usb_mic,
            "include_usb_dtln": include_usb_dtln,
            "include_xvf_raw0_dtln": include_xvf_raw0_dtln,
            "include_aec3_sweep": include_aec3_sweep,
            "corpus_profile": corpus_profile,
            "aec3_sweep_source": aec3_sweep_source,
            "enabled_legs": list(enabled_legs),
            "has_audio_context": audio_context is not None,
        }

    def _maybe_load_recent_session(
        self, now: float | None = None,
    ) -> None:
        """Recover the marked active session after a server crash.

        Called automatically from `start()`. Safe to call multiple
        times (only triggers if no session is currently set).
        """
        with self._lock:
            if self._session_id is not None:
                return  # already have a session, nothing to recover
        if not self._metadata_dir.is_dir():
            return

        now = now if now is not None else time.time()
        marker = self._active_session_marker_path()
        if not marker.is_file():
            return
        age = now - marker.stat().st_mtime
        if age > RESUME_WINDOW_SEC:
            logger.info(
                "skipping recovery: active session marker is %.0fs old "
                "(window=%.0fs)", age, RESUME_WINDOW_SEC,
            )
            self._clear_active_session_marker()
            return

        try:
            marker_data = json.loads(marker.read_text())
            session_id = str(marker_data["session_id"])
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "recovery skipped: failed to read %s: %s", marker, e,
            )
            return
        except KeyError:
            logger.warning(
                "recovery skipped: %s lacks session_id", marker,
            )
            return

        target = self._find_session_metadata(session_id)
        if target is None:
            logger.warning(
                "recovery skipped: active session metadata missing for %s",
                session_id,
            )
            self._clear_active_session_marker()
            return

        try:
            result = self._load_session_data(json.loads(target.read_text()))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "recovery skipped: failed to restore %s: %s", target, e,
            )
            return
        logger.info(
            "recovered active session %s for %s clips=%d legs=%s",
            result["session_id"], result["member"], result["clip_count"],
            ",".join(result["enabled_legs"]),
        )

    def begin_session(
        self,
        member: str,
        corpus_profile: str = PROFILE_STANDARD,
        include_raw_mic_0: bool = False,
        include_dtln: bool = True,
        include_usb_mic: bool = False,
        include_usb_dtln: bool = False,
        include_xvf_raw0_dtln: bool = False,
        include_aec3_sweep: bool = False,
        aec3_sweep_source: str | None = None,
    ) -> str:
        """Open a fresh recording session. Resets the in-memory clip
        list (existing on-disk WAVs are untouched).

        `include_raw_mic_0` (default False) — when True, clips in this
        session also capture the truly-raw mic 0 leg (chip channel 2)
        into `aec_raw0_<condition>/`. Per-session, not per-clip, so
        downstream tools can rely on session-wide consistency.

        `include_dtln` (default True) — when True and the recorder has
        a DTLN port configured, clips capture the XVF raw-through-DTLN
        comparison leg.

        `include_usb_mic` (default False) — when True, clips also
        capture the corpus-only reference + cheap USB mic legs. These
        require matching bridge env flags to be enabled, otherwise the
        UDP captures will simply have no audio to write.

        `include_usb_dtln` (default False) — when True, clips capture
        the cheap USB raw-through-DTLN leg. The bridge must be started
        with JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1 for packets to arrive.

        `include_aec3_sweep` (default False) — when True, clips also
        capture the bounded same-utterance AEC3 tuning variants emitted
        by jasper-aec-bridge. These are pilot/tuning legs, not
        production wake inputs.

        `aec3_sweep_source` selects which raw mic feeds those variants.
        New sessions default to the cheap USB mic so one utterance yields
        USB baseline + three USB AEC3 variants while retaining the XVF
        baseline leg for comparison.

        Returns the new session_id (UTC timestamp).
        """
        safe_member = "".join(c for c in member.lower() if c.isalnum() or c == "_")
        if not safe_member:
            raise ValueError(f"member name has no usable chars: {member!r}")
        if corpus_profile not in CORPUS_PROFILES:
            raise ValueError(f"unknown corpus profile: {corpus_profile!r}")
        sweep_source = (
            _session_aec3_sweep_source(aec3_sweep_source)
            if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
        )
        effective_include_usb_mic = include_usb_mic or (
            include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
        )
        if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
            include_raw_mic_0 = True
            effective_include_usb_mic = True
            include_aec3_sweep = False
        with self._lock:
            if self._current is not None:
                raise StateError(
                    "can't begin session: recording in progress",
                )
            # session_id = UTC second-resolution timestamp + a 4-hex
            # suffix. The suffix avoids a collision when an operator
            # (or a test) calls begin_session() twice within the same
            # second — without it, two sessions would share both the
            # in-memory id AND the on-disk metadata filename, and the
            # second would silently overwrite the first.
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            enabled_legs = _session_legs(
                self._ports,
                corpus_profile=corpus_profile,
                include_dtln=include_dtln,
                include_raw_mic_0=include_raw_mic_0,
                include_usb_mic=effective_include_usb_mic,
                include_usb_dtln=include_usb_dtln,
                include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                include_aec3_sweep=include_aec3_sweep,
                aec3_sweep_source=sweep_source,
            )
            sweep_variants = (
                variant_metadata(input_source=sweep_source)
                if include_aec3_sweep else []
            )
            sweep_config = (
                config_metadata(input_source=sweep_source)
                if include_aec3_sweep else None
            )
            session_id = f"{ts}-{secrets.token_hex(2)}"
            chip_config = (
                chip_aec_config_metadata()
                if corpus_profile == PROFILE_CHIP_AEC_COMPARISON else None
            )
            self._session_id = session_id
            self._member = safe_member
            self._clips = []
            self._include_raw_mic_0 = RAW0_LEG in enabled_legs
            self._include_dtln = DTLN_LEG in enabled_legs
            self._include_usb_mic = effective_include_usb_mic
            self._include_usb_dtln = USB_DTLN_LEG in enabled_legs
            self._include_xvf_raw0_dtln = XVF_RAW0_DTLN_LEG in enabled_legs
            self._include_aec3_sweep = include_aec3_sweep
            self._corpus_profile = corpus_profile
            self._chip_aec_config = chip_config
            self._aec3_sweep_source = sweep_source
            self._aec3_sweep_variants = sweep_variants
            self._aec3_sweep_config = sweep_config
            self._enabled_legs = enabled_legs
            self._audio_context = None
        audio_context = build_session_audio_context(
            corpus_profile=corpus_profile,
            enabled_legs=enabled_legs,
            ports=self._ports,
            include_raw_mic_0=RAW0_LEG in enabled_legs,
            include_dtln=DTLN_LEG in enabled_legs,
            include_usb_mic=effective_include_usb_mic,
            include_usb_dtln=USB_DTLN_LEG in enabled_legs,
            include_xvf_raw0_dtln=XVF_RAW0_DTLN_LEG in enabled_legs,
            include_aec3_sweep=include_aec3_sweep,
            aec3_sweep_source=sweep_source,
            chip_aec_config=chip_config,
        )
        with self._lock:
            if self._session_id == session_id:
                self._audio_context = audio_context
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        self._save_metadata()  # write the per-session flag before clips arrive
        self._write_active_session_marker()
        return session_id

    def include_raw_mic_0(self) -> bool:
        """Whether the active session captures the raw-mic-0 leg."""
        with self._lock:
            return self._include_raw_mic_0

    def include_dtln(self) -> bool:
        """Whether the active session captures the XVF DTLN leg."""
        with self._lock:
            return self._include_dtln

    def include_usb_mic(self) -> bool:
        """Whether the active session captures corpus USB/ref legs."""
        with self._lock:
            return self._include_usb_mic

    def include_usb_dtln(self) -> bool:
        """Whether the active session captures the USB DTLN leg."""
        with self._lock:
            return self._include_usb_dtln

    def include_xvf_raw0_dtln(self) -> bool:
        """Whether the active session captures the XVF raw0 DTLN leg."""
        with self._lock:
            return self._include_xvf_raw0_dtln

    def include_aec3_sweep(self) -> bool:
        """Whether the active session captures same-utterance AEC3 variants."""
        with self._lock:
            return self._include_aec3_sweep

    def corpus_profile(self) -> str:
        with self._lock:
            return self._corpus_profile

    def chip_aec_config(self) -> dict[str, object] | None:
        with self._lock:
            return dict(self._chip_aec_config) if self._chip_aec_config else None

    def aec3_sweep_source(self) -> str:
        """Mic source that feeds the active session's AEC3 sweep variants."""
        with self._lock:
            return self._aec3_sweep_source

    def aec3_sweep_variants(self) -> list[dict[str, object]]:
        """Effective AEC3 sweep variants for the active session or UI status."""
        with self._lock:
            if self._include_aec3_sweep and self._aec3_sweep_variants:
                return list(self._aec3_sweep_variants)
        return variant_metadata(input_source=DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE)

    def aec3_sweep_config(self) -> dict[str, object]:
        """Effective AEC3 sweep config provenance for the active session/status."""
        with self._lock:
            if self._include_aec3_sweep and self._aec3_sweep_config:
                return dict(self._aec3_sweep_config)
        return config_metadata(input_source=DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE)

    def enabled_legs(self) -> tuple[str, ...]:
        """The active session's leg set, in recording/playback order."""
        with self._lock:
            return self._enabled_legs

    def audio_context(self) -> dict[str, Any] | None:
        """Production-profile/corpus-context snapshot for the active session."""
        with self._lock:
            return dict(self._audio_context) if self._audio_context else None

    def start_recording(self, condition: str, distance: str) -> dict[str, str]:
        """Begin recording on the backend loop. Returns {clip_id, start_ts}.

        Reserves the recording slot under the lock via
        `_starting_clip_id` before releasing for the slow async start;
        concurrent calls see the sentinel and refuse with the correct
        "already in progress" error instead of racing into a UDP-bind
        failure.
        """
        if condition not in CONDITIONS:
            raise ValueError(
                f"unknown condition {condition!r}; expected {CONDITIONS}",
            )
        if distance not in DISTANCES:
            raise ValueError(
                f"unknown distance {distance!r}; expected {DISTANCES}",
            )

        clip_id = str(uuid.uuid4())
        with self._lock:
            if self._session_id is None or self._member is None:
                raise StateError("call begin_session() first")
            if self._current is not None or self._starting_clip_id is not None:
                raise StateError("recording already in progress")
            # Reserve the slot — concurrent calls now see this and
            # refuse cleanly.
            self._starting_clip_id = clip_id
            # Per-session leg selection. Built under the lock so the
            # session's clips all share one leg set.
            active_legs = list(self._enabled_legs)
            aec3_sweep_source = self._aec3_sweep_source

        ports_for_task = {
            leg: self._ports[leg]
            for leg in active_legs if leg in self._ports
        }
        task = RecordingTask(
            ports_for_task,
            aec3_sweep_source=aec3_sweep_source,
        )
        # Start on the backend loop. If the UDP bind fails (jasper-voice
        # is still up, port already in use), this raises and we never
        # transition into the recording state.
        try:
            self._submit(task.start())
        except Exception as e:
            with self._lock:
                self._starting_clip_id = None
            raise StateError(
                f"failed to start recording (is jasper-voice down?): {e}",
            ) from e

        start_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock:
            self._current = task
            self._current_clip_id = clip_id
            self._current_meta = {
                "condition": condition,
                "distance": distance,
                "start_ts": start_ts,
            }
            self._starting_clip_id = None  # transitioned: starting → current
            # Auto-stop timer — guards against a forgotten Stop click.
            self._auto_stop_handle = self._loop.call_later(
                self._max_duration_sec, self._auto_stop_threadsafe,
            )
        return {"clip_id": clip_id, "start_ts": start_ts}

    def _auto_stop_threadsafe(self) -> None:
        """Fires on the backend loop when MAX_RECORDING_DURATION_SEC
        elapses. Triggers stop_recording on a worker thread so the
        loop thread doesn't block on its own sync method."""
        thread = threading.Thread(
            target=self._auto_stop_safe, daemon=True,
        )
        thread.start()

    def _auto_stop_safe(self) -> None:
        try:
            self.stop_recording(auto=True)
        except Exception as e:
            logger.warning("auto-stop failed: %s", e)

    def stop_recording(self, auto: bool = False) -> ClipMetadata:
        """Stop the current recording, save WAVs, return metadata."""
        with self._lock:
            if self._current is None:
                raise StateError("no recording in progress")
            task = self._current
            clip_id = self._current_clip_id
            meta = self._current_meta
            session_id = self._session_id
            member = self._member
            selected_legs = list(self._enabled_legs)
            audio_context = dict(self._audio_context or {})
            # Cancel the auto-stop timer if it hasn't fired yet.
            if self._auto_stop_handle is not None and not auto:
                self._auto_stop_handle.cancel()
            self._auto_stop_handle = None
            # Clear state up-front so a second Stop click during the
            # save isn't a confusing no-op.
            self._current = None
            self._current_clip_id = None
            self._current_meta = None

        # Long operations (await stop, write WAVs) happen OUTSIDE the
        # lock — other API calls can read state concurrently.
        pcm_per_leg = self._submit(task.stop())
        stop_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        duration_sec = task.elapsed_sec()
        capture_health = task.capture_health(duration_sec)

        # Pick the next sequence number. Sequence is per-session, not
        # per-condition, so filenames stay unique across the whole
        # session. Include deleted clips in the max() so a later clip
        # never reuses a previous filename after the operator deletes
        # one bad take.
        with self._lock:
            seq = max((c.seq for c in self._clips), default=0) + 1

        files: dict[str, str] = {}
        # Condition → directory mapping. "nomusic" preserved for
        # backward compat with existing recordings + downstream tools
        # (extract-wake-corpus.py emits the same name). "ambient" gets
        # its own dir so training can slice on it explicitly.
        condition_dir = {
            "quiet": "nomusic",
            "ambient": "ambient",
            "music": "music",
        }[meta["condition"]]
        for leg, pcm in pcm_per_leg.items():
            if not pcm:
                continue
            filename = f"enroll_{member}_{session_id}_{seq:03d}.aec-{leg}.wav"
            full_path = self._output_dir / f"aec_{leg}_{condition_dir}" / filename
            full_path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(full_path, pcm)
            files[leg] = str(full_path)

        clip = ClipMetadata(
            clip_id=clip_id,
            member=member,
            condition=meta["condition"],
            distance=meta["distance"],
            session_id=session_id,
            seq=seq,
            start_ts=meta["start_ts"],
            stop_ts=stop_ts,
            duration_sec=duration_sec,
            files=files,
            deleted=False,
            auto_stopped=auto,
            selected_legs=selected_legs,
            audio_context=audio_context,
            capture_health=capture_health,
        )
        with self._lock:
            self._clips.append(clip)
        self._save_metadata()
        logger.info(
            "clip saved: %s seq=%d condition=%s distance=%s dur=%.2fs%s",
            clip_id, seq, meta["condition"], meta["distance"],
            duration_sec, " (auto-stopped)" if auto else "",
        )
        return clip

    def delete_clip(self, clip_id: str) -> bool:
        """Hard-delete a clip's WAVs + mark it deleted in metadata.

        Returns True if the clip existed and was deleted, False if
        not found (or already deleted)."""
        with self._lock:
            clip = next(
                (c for c in self._clips
                 if c.clip_id == clip_id and not c.deleted),
                None,
            )
            if clip is None:
                return False
            for path_str in clip.files.values():
                p = Path(path_str)
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.warning("failed to delete %s: %s", p, e)
            clip.deleted = True
        self._save_metadata()
        logger.info("clip deleted: %s", clip_id)
        return True

    def list_clips(self, include_deleted: bool = False) -> list[ClipMetadata]:
        with self._lock:
            return [
                c for c in self._clips
                if include_deleted or not c.deleted
            ]

    def clip(self, clip_id: str) -> ClipMetadata | None:
        with self._lock:
            return next(
                (c for c in self._clips if c.clip_id == clip_id),
                None,
            )

    def elapsed_recording_sec(self) -> float:
        with self._lock:
            if self._current is None:
                return 0.0
            return self._current.elapsed_sec()

    # ----- metadata persistence -------------------------------------

    def _metadata_path(self) -> Path:
        return self._metadata_dir / f"enroll_{self._member}_{self._session_id}.json"

    def _save_metadata(self) -> None:
        """Atomic-rewrite the session JSON sidecar. Called after every
        clip write + delete so the file on disk always reflects the
        current state (resilient to a server crash mid-session)."""
        with self._lock:
            if self._session_id is None:
                return
            path = self._metadata_path()
            data = {
                "metadata_schema_version": METADATA_SCHEMA_VERSION,
                "session_id": self._session_id,
                "member": self._member,
                "ports": self._ports,
                "include_raw_mic_0": self._include_raw_mic_0,
                "include_dtln": self._include_dtln,
                "include_usb_mic": self._include_usb_mic,
                "include_usb_dtln": self._include_usb_dtln,
                "include_xvf_raw0_dtln": self._include_xvf_raw0_dtln,
                "include_aec3_sweep": self._include_aec3_sweep,
                "corpus_profile": self._corpus_profile,
                "chip_aec_config": self._chip_aec_config,
                "aec3_sweep_source": self._aec3_sweep_source,
                "aec3_sweep_variants": list(self._aec3_sweep_variants),
                "aec3_sweep_config": self._aec3_sweep_config,
                "enabled_legs": list(self._enabled_legs),
                "audio_context": self._audio_context,
                "clips": [c.to_json() for c in self._clips],
            }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    # ----- sessions management --------------------------------------

    def list_sessions(self) -> list[dict[str, Any]]:
        """Scan the metadata dir, return one summary per session.

        Each summary: {session_id, member, mtime, clip_count,
        deleted_count, enabled_legs, conditions: {<cond>: n, ...}}.
        Sorted newest-first by mtime.

        Failure-soft: corrupt JSON files are skipped + logged, not
        raised — one bad file shouldn't black out the whole list.
        """
        if not self._metadata_dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(
            self._metadata_dir.glob("enroll_*.json"),
            key=lambda f: f.stat().st_mtime, reverse=True,
        ):
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("skip corrupt session %s: %s", p.name, e)
                continue
            clips = data.get("clips", [])
            alive = [c for c in clips if not c.get("deleted")]
            conds: dict[str, int] = {}
            for c in alive:
                k = c.get("condition", "?")
                conds[k] = conds.get(k, 0) + 1
            enabled_legs = _enabled_legs_from_metadata(data, self._ports)
            saved_config = data.get("aec3_sweep_config")
            saved_source = (
                saved_config.get("input_source")
                if isinstance(saved_config, dict) else None
            )
            aec3_sweep_source = _legacy_aec3_sweep_source(
                str(data.get("aec3_sweep_source") or saved_source or ""),
            )
            audio_context = data.get("audio_context")
            if not isinstance(audio_context, dict):
                audio_context = {}
            audio_profile = audio_context.get("production_audio_profile")
            if not isinstance(audio_profile, dict):
                audio_profile = {}
            dac_reference = audio_context.get("dac_reference")
            if not isinstance(dac_reference, dict):
                dac_reference = {}
            validation = dac_reference.get("validation")
            if not isinstance(validation, dict):
                validation = {}
            out.append({
                "session_id": data.get("session_id", "?"),
                "member": data.get("member", "?"),
                "metadata_schema_version": data.get("metadata_schema_version"),
                "mtime": p.stat().st_mtime,
                "clip_count": len(alive),
                "deleted_count": len(clips) - len(alive),
                "include_raw_mic_0": bool(data.get("include_raw_mic_0", False)),
                "include_dtln": _metadata_flag(
                    data, "include_dtln", DTLN_LEG, enabled_legs,
                ),
                "include_usb_mic": bool(data.get("include_usb_mic", False)),
                "include_usb_dtln": _metadata_flag(
                    data, "include_usb_dtln", USB_DTLN_LEG, enabled_legs,
                ),
                "include_xvf_raw0_dtln": _metadata_flag(
                    data, "include_xvf_raw0_dtln", XVF_RAW0_DTLN_LEG, enabled_legs,
                ),
                "include_aec3_sweep": (
                    bool(data.get("include_aec3_sweep", False))
                    or any(leg in enabled_legs for leg in AEC3_SWEEP_LEGS)
                ),
                "corpus_profile": data.get("corpus_profile", PROFILE_STANDARD),
                "aec3_sweep_source": aec3_sweep_source,
                "enabled_legs": list(enabled_legs),
                "has_audio_context": bool(audio_context),
                "audio_profile_requested": audio_profile.get("requested"),
                "audio_profile_active": audio_profile.get("active"),
                "audio_profile_state": audio_profile.get("state"),
                "audio_validation_status": validation.get("status"),
                "conditions": conds,
                "is_active": (
                    self._session_id is not None
                    and data.get("session_id") == self._session_id
                ),
            })
        return out

    def load_session(self, session_id: str) -> dict[str, Any]:
        """Switch the in-memory active session to an existing one on
        disk. Returns the loaded session's metadata.

        Refuses if a recording is in progress (would orphan the clip).
        Refuses if the target session doesn't exist.
        """
        with self._lock:
            if self._current is not None:
                raise StateError(
                    "can't load session: recording in progress",
                )
        target = self._find_session_metadata(session_id)
        if target is None:
            raise ValueError(f"session not found: {session_id}")

        data = json.loads(target.read_text())
        try:
            result = self._load_session_data(data)
        except ValueError as e:
            raise ValueError(
                f"session {session_id} schema mismatch: {e}",
            ) from e
        self._write_active_session_marker()
        logger.info(
            "loaded session %s for %s with %d clip(s) include_raw_mic_0=%s "
            "include_dtln=%s include_usb_mic=%s include_usb_dtln=%s "
            "include_aec3_sweep=%s aec3_sweep_source=%s legs=%s",
            session_id, result["member"], result["clip_count"],
            result["include_raw_mic_0"], result["include_dtln"],
            result["include_usb_mic"], result["include_usb_dtln"],
            result["include_aec3_sweep"], result["aec3_sweep_source"],
            ",".join(result["enabled_legs"]),
        )
        return result

    def unload_session(self) -> str | None:
        """Clear the in-memory append target without deleting WAVs.

        This is the graceful end-of-session path for the web UI. The
        session remains in the Sessions list and can be explicitly
        loaded later, but a page refresh or server restart starts from
        a blank new-session form.
        """
        with self._lock:
            if self._current is not None or self._starting_clip_id is not None:
                raise StateError(
                    "can't unload session: recording in progress",
                )
            session_id = self._session_id
            self._clear_session_state_locked()
        self._clear_active_session_marker()
        if session_id is not None:
            logger.info("unloaded session %s", session_id)
        return session_id

    def delete_session(self, session_id: str) -> dict[str, int]:
        """Hard-delete every WAV referenced by a session + remove the
        JSON sidecar. Returns {wavs_deleted, wavs_missing}.

        Refuses if a recording is in progress (covers the case where
        the operator tries to delete the session they're recording
        into).

        If the deleted session was the active in-memory one, clears
        the in-memory state (operator now needs to begin a new
        session or load another).
        """
        with self._lock:
            if self._current is not None:
                raise StateError(
                    "can't delete session: recording in progress",
                )
        target = self._find_session_metadata(session_id)
        if target is None:
            raise ValueError(f"session not found: {session_id}")

        data = json.loads(target.read_text())
        wavs_deleted = 0
        wavs_missing = 0
        for c in data.get("clips", []):
            if c.get("deleted"):
                # Already-deleted clips have already had their WAVs
                # removed by delete_clip(); skip + don't count.
                continue
            for path_str in (c.get("files") or {}).values():
                p_wav = Path(path_str)
                try:
                    p_wav.unlink()
                    wavs_deleted += 1
                except FileNotFoundError:
                    wavs_missing += 1
                except OSError as e:
                    logger.warning("failed to delete %s: %s", p_wav, e)
                    wavs_missing += 1
        target.unlink()

        # If we just deleted the in-memory active session, clear state.
        with self._lock:
            if self._session_id == session_id:
                self._clear_session_state_locked()
                self._clear_active_session_marker()
        logger.info(
            "deleted session %s: %d wavs removed, %d missing",
            session_id, wavs_deleted, wavs_missing,
        )
        return {"wavs_deleted": wavs_deleted, "wavs_missing": wavs_missing}


# ---------------------------------------------------------------------------
# Voice-daemon control — same systemctl helpers as wake-enroll
# ---------------------------------------------------------------------------


def voice_daemon_active() -> bool:
    """True if jasper-voice is currently running (systemd active)."""
    import subprocess
    rc = subprocess.run(
        ["systemctl", "is-active", VOICE_UNIT],
        capture_output=True, text=True,
    )
    return rc.returncode == 0 and rc.stdout.strip() == "active"


def aec_bridge_active() -> bool:
    """True if jasper-aec-bridge is active, for metadata snapshots only."""
    try:
        rc = subprocess.run(
            ["systemctl", "is-active", BRIDGE_UNIT],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return rc.returncode == 0 and rc.stdout.strip() == "active"


def set_voice_daemon_state(action: str) -> None:
    """Start or stop jasper-voice through systemd."""
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    subprocess.run(["systemctl", action, VOICE_UNIT], check=True)


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
    voice_was_active = voice_daemon_active()
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


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    backend: RecordingBackend
    csrf_token: str

    # ----- helpers --------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, body: Any, status: int = 200) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON body: {e}") from e

    def _check_csrf(self) -> bool:
        """Verify the X-CSRF-Token header matches the server token.

        Returns True if valid, False (and sends 403) otherwise. The
        token is embedded in the served HTML page via a meta tag; the
        page's JS reads it and sends it on every mutating request.
        Defense against a malicious cross-origin site triggering
        recordings or daemon toggles from the operator's browser.

        Uses `secrets.compare_digest` for timing-safe comparison
        (defense-in-depth — the attacker probably can't observe
        latency in practice, but it's a one-line free win).
        """
        header_token = self.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(header_token, self.csrf_token):
            self._send_error_json(
                403,
                f"missing or invalid {CSRF_HEADER} header — reload "
                "the page to refresh the token",
            )
            return False
        return True

    # ----- GET --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        if path == "/":
            html_text = _render_index_html(self.csrf_token)
            data = html_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/status":
            self._send_json({
                "voice_daemon_active": voice_daemon_active(),
                "session_id": self.backend.session_id(),
                "member": self.backend.member(),
                "include_raw_mic_0": self.backend.include_raw_mic_0(),
                "include_dtln": self.backend.include_dtln(),
                "include_usb_mic": self.backend.include_usb_mic(),
                "include_usb_dtln": self.backend.include_usb_dtln(),
                "include_xvf_raw0_dtln": self.backend.include_xvf_raw0_dtln(),
                "include_aec3_sweep": self.backend.include_aec3_sweep(),
                "corpus_profile": self.backend.corpus_profile(),
                "chip_aec_config": self.backend.chip_aec_config(),
                "aec3_sweep_source": self.backend.aec3_sweep_source(),
                "aec3_sweep_variants": self.backend.aec3_sweep_variants(),
                "aec3_sweep_config": self.backend.aec3_sweep_config(),
                "enabled_legs": list(self.backend.enabled_legs()),
                "audio_context": self.backend.audio_context(),
                "bridge_outputs": bridge_output_status(),
                "is_recording": self.backend.is_recording(),
                "elapsed_sec": self.backend.elapsed_recording_sec(),
                "clip_count": len(self.backend.list_clips()),
            })
            return

        if path == "/api/clips":
            self._send_json({
                "clips": [c.to_json() for c in self.backend.list_clips()],
            })
            return

        if path == "/api/sessions":
            self._send_json({"sessions": self.backend.list_sessions()})
            return

        if path == "/api/usb-mic/status":
            self._send_json(usb_mic_status())
            return

        if path.startswith("/api/clip/") and path.endswith("/wav"):
            self._serve_wav(path, url)
            return

        if path == "/api/recording/level":
            self._serve_level_sse()
            return

        self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")

    def _serve_level_sse(self) -> None:
        """Server-Sent Events stream of the live AEC-ON RMS in dBFS.

        Connects from the JS on page load, stays open for the lifetime
        of the tab. When recording is active, pushes {"recording": true,
        "rms_dbfs": <float>} every ~80 ms. When idle, pushes {"recording":
        false, "rms_dbfs": null} less frequently so the connection
        stays warm without burning CPU.

        Exit paths:
          - Client closes tab → wfile.write raises BrokenPipeError /
            ConnectionResetError → we exit cleanly.
          - Server shuts down → same.

        NOT CSRF-protected: this is a read-only GET with no side
        effects, like /api/status. The token requirement applies to
        mutating endpoints only.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # disable nginx buffering
        self.end_headers()

        idle_period_sec = 0.5  # slow when not recording
        active_period_sec = 0.08  # ~12.5 Hz, matches frame rate
        try:
            while True:
                rms = self.backend.get_current_rms_dbfs()
                if rms is None:
                    payload = json.dumps({
                        "recording": False, "rms_dbfs": None,
                    })
                    sleep = idle_period_sec
                else:
                    payload = json.dumps({
                        "recording": True, "rms_dbfs": rms,
                    })
                    sleep = active_period_sec
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(sleep)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client gone

    def _serve_wav(self, path: str, url: Any) -> None:
        # /api/clip/<id>/wav?leg=<on|off|dtln>
        parts = path.split("/")
        if len(parts) != 5 or parts[1] != "api" or parts[2] != "clip" or parts[4] != "wav":
            self.send_error(HTTPStatus.NOT_FOUND, "bad clip URL")
            return
        clip_id = parts[3]
        qs = parse_qs(url.query)
        leg = qs.get("leg", ["on"])[0]
        if leg not in LEGS:
            self._send_error_json(400, f"bad leg: {leg}")
            return
        clip = self.backend.clip(clip_id)
        if clip is None or clip.deleted:
            self.send_error(HTTPStatus.NOT_FOUND, "clip not found")
            return
        wav_path = clip.files.get(leg)
        if wav_path is None:
            self.send_error(
                HTTPStatus.NOT_FOUND, f"no {leg} leg for this clip",
            )
            return
        p = Path(wav_path)
        if not p.is_file():
            self.send_error(
                HTTPStatus.NOT_FOUND, f"WAV missing on disk: {wav_path}",
            )
            return
        size = p.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(p, "rb") as f:
            self.wfile.write(f.read())

    # ----- POST -------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"

        # All POSTs are mutating — require CSRF token. _check_csrf
        # sends the 403 itself; we just return on failure.
        if not self._check_csrf():
            return

        try:
            body = self._read_json()
        except ValueError as e:
            self._send_error_json(400, str(e))
            return

        if path == "/api/session":
            member = (body.get("member") or "").strip()
            corpus_profile = str(body.get("corpus_profile") or PROFILE_STANDARD)
            if corpus_profile not in CORPUS_PROFILES:
                self._send_error_json(400, f"unknown corpus_profile: {corpus_profile}")
                return
            include_raw_mic_0 = bool(body.get("include_raw_mic_0", False))
            include_dtln = bool(body.get("include_dtln", True))
            include_usb_mic = bool(body.get("include_usb_mic", False))
            include_usb_dtln = bool(body.get("include_usb_dtln", False))
            include_xvf_raw0_dtln = bool(body.get("include_xvf_raw0_dtln", False))
            include_aec3_sweep = bool(body.get("include_aec3_sweep", False))
            try:
                aec3_sweep_source = (
                    _session_aec3_sweep_source(body.get("aec3_sweep_source"))
                    if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
                )
            except Aec3SweepConfigError as e:
                self._send_error_json(400, str(e))
                return
            if include_aec3_sweep and aec3_sweep_source == AEC3_SWEEP_SOURCE_USB:
                include_usb_mic = True
            if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
                include_raw_mic_0 = True
                include_usb_mic = True
                include_aec3_sweep = False
            enable_bridge_outputs = bool(
                body.get("enable_bridge_outputs", False),
            )
            if not member:
                self._send_error_json(400, "member is required")
                return
            if self.backend.is_recording():
                self._send_error_json(
                    409,
                    "can't begin session: recording in progress",
                )
                return
            missing_outputs = missing_bridge_outputs_for_session(
                corpus_profile=corpus_profile,
                include_dtln=include_dtln,
                include_usb_mic=include_usb_mic,
                include_usb_dtln=include_usb_dtln,
                include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                include_aec3_sweep=include_aec3_sweep,
                aec3_sweep_source=aec3_sweep_source,
            )
            if missing_outputs and not enable_bridge_outputs:
                labels = [
                    BRIDGE_OUTPUT_LABELS.get(key, key)
                    for key in missing_outputs
                ]
                self._send_json({
                    "error": (
                        "bridge outputs are disabled for requested "
                        f"legs: {', '.join(labels)}"
                    ),
                    "can_enable_bridge_outputs": True,
                    "missing_bridge_outputs": missing_outputs,
                    "missing_bridge_output_labels": labels,
                }, status=409)
                return
            if missing_outputs:
                try:
                    set_bridge_outputs_for_session(
                        corpus_profile=corpus_profile,
                        include_dtln=include_dtln,
                        include_usb_mic=include_usb_mic,
                        include_usb_dtln=include_usb_dtln,
                        include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                        include_aec3_sweep=include_aec3_sweep,
                        aec3_sweep_source=aec3_sweep_source,
                    )
                except subprocess.CalledProcessError as e:
                    detail = (e.stderr or e.stdout or str(e)).strip()
                    msg = (
                        f"could not enable bridge outputs; {BRIDGE_UNIT} "
                        "restart failed and the env was rolled back"
                    )
                    if detail:
                        msg = f"{msg}: {detail[-500:]}"
                    self._send_error_json(500, msg)
                    return
                except subprocess.TimeoutExpired:
                    self._send_error_json(
                        500,
                        f"could not enable bridge outputs; {BRIDGE_UNIT} "
                        "restart timed out and the env was rolled back",
                    )
                    return
                except OSError as e:
                    self._send_error_json(
                        500,
                        f"failed to enable bridge outputs: {e}",
                    )
                    return
            try:
                session_id = self.backend.begin_session(
                    member,
                    corpus_profile=corpus_profile,
                    include_raw_mic_0=include_raw_mic_0,
                    include_dtln=include_dtln,
                    include_usb_mic=include_usb_mic,
                    include_usb_dtln=include_usb_dtln,
                    include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                    include_aec3_sweep=include_aec3_sweep,
                    aec3_sweep_source=aec3_sweep_source,
                )
            except (ValueError, StateError) as e:
                self._send_error_json(400, str(e))
                return
            self._send_json({
                "session_id": session_id, "member": member,
                "include_raw_mic_0": include_raw_mic_0,
                "include_dtln": include_dtln,
                "include_usb_mic": include_usb_mic,
                "include_usb_dtln": include_usb_dtln,
                "include_xvf_raw0_dtln": include_xvf_raw0_dtln,
                "include_aec3_sweep": include_aec3_sweep,
                "corpus_profile": corpus_profile,
                "chip_aec_config": self.backend.chip_aec_config(),
                "aec3_sweep_source": aec3_sweep_source,
                "aec3_sweep_variants": self.backend.aec3_sweep_variants(),
                "aec3_sweep_config": self.backend.aec3_sweep_config(),
                "enabled_legs": list(self.backend.enabled_legs()),
                "audio_context": self.backend.audio_context(),
                "bridge_outputs": bridge_output_status(),
            })
            return

        if path == "/api/session/load":
            sid = (body.get("session_id") or "").strip()
            if not sid:
                self._send_error_json(400, "session_id is required")
                return
            try:
                result = self.backend.load_session(sid)
            except ValueError as e:
                self._send_error_json(404, str(e))
                return
            except StateError as e:
                self._send_error_json(409, str(e))
                return
            self._send_json(result)
            return

        if path == "/api/session/unload":
            try:
                unloaded = self.backend.unload_session()
            except StateError as e:
                self._send_error_json(409, str(e))
                return
            self._send_json({"unloaded_session": unloaded})
            return

        if path == "/api/clip/start":
            condition = (body.get("condition") or "").strip()
            distance = (body.get("distance") or "").strip()
            try:
                result = self.backend.start_recording(condition, distance)
            except (ValueError, StateError) as e:
                self._send_error_json(409, str(e))
                return
            self._send_json(result)
            return

        if path == "/api/clip/stop":
            try:
                clip = self.backend.stop_recording()
            except StateError as e:
                self._send_error_json(409, str(e))
                return
            self._send_json(clip.to_json())
            return

        if path == "/api/bridge-outputs":
            action = (body.get("action") or "").strip()
            if action != "disable":
                self._send_error_json(400, "action must be disable")
                return
            if self.backend.is_recording():
                self._send_error_json(
                    409,
                    "stop the current recording before disabling bridge outputs",
                )
                return
            try:
                disable_bridge_corpus_outputs()
            except subprocess.CalledProcessError as e:
                detail = (e.stderr or e.stdout or str(e)).strip()
                msg = (
                    f"could not disable bridge outputs; {BRIDGE_UNIT} "
                    "restart failed and the env was rolled back"
                )
                if detail:
                    msg = f"{msg}: {detail[-500:]}"
                self._send_error_json(500, msg)
                return
            except subprocess.TimeoutExpired:
                self._send_error_json(
                    500,
                    f"could not disable bridge outputs; {BRIDGE_UNIT} "
                    "restart timed out and the env was rolled back",
                )
                return
            except OSError as e:
                self._send_error_json(
                    500,
                    f"failed to disable bridge outputs: {e}",
                )
                return
            self._send_json({"bridge_outputs": bridge_output_status()})
            return

        if path == "/api/corpus-test-mode":
            action = (body.get("action") or "").strip()
            if action not in ("enter", "exit"):
                self._send_error_json(400, "action must be enter or exit")
                return
            if self.backend.is_recording():
                self._send_error_json(
                    409,
                    "stop the current recording before changing corpus test mode",
                )
                return
            try:
                if action == "enter":
                    enter_corpus_test_mode(
                        corpus_profile=str(
                            body.get("corpus_profile") or PROFILE_STANDARD,
                        ),
                        include_dtln=bool(body.get("include_dtln", True)),
                        include_usb_mic=bool(body.get("include_usb_mic", False)),
                        include_usb_dtln=bool(body.get("include_usb_dtln", False)),
                        include_xvf_raw0_dtln=bool(
                            body.get("include_xvf_raw0_dtln", False),
                        ),
                        include_aec3_sweep=bool(
                            body.get("include_aec3_sweep", False),
                        ),
                        aec3_sweep_source=body.get("aec3_sweep_source"),
                    )
                else:
                    exit_corpus_test_mode()
                    self.backend.unload_session()
            except subprocess.CalledProcessError as e:
                detail = (e.stderr or e.stdout or str(e)).strip()
                msg = f"corpus test mode {action} failed"
                if detail:
                    msg = f"{msg}: {detail[-500:]}"
                self._send_error_json(500, msg)
                return
            except subprocess.TimeoutExpired:
                self._send_error_json(
                    500,
                    f"corpus test mode {action} timed out while restarting "
                    f"{BRIDGE_UNIT}",
                )
                return
            except StateError as e:
                self._send_error_json(409, str(e))
                return
            except ValueError as e:
                self._send_error_json(400, str(e))
                return
            except OSError as e:
                self._send_error_json(
                    500,
                    f"corpus test mode {action} failed: {e}",
                )
                return
            self._send_json({
                "action": action,
                "voice_daemon_active": voice_daemon_active(),
                "bridge_outputs": bridge_output_status(),
            })
            return

        if path == "/api/voice-daemon":
            action = (body.get("action") or "").strip()
            if action not in ("start", "stop"):
                self._send_error_json(400, "action must be start or stop")
                return
            disable_outputs = bool(body.get("disable_bridge_outputs", False))
            # Refuse to start jasper-voice while a recording is in
            # progress: starting it would try to bind UDP ports the
            # recording owns, sending the daemon into a restart loop
            # while the operator wonders why their speaker is dead.
            # Caller sees a clear error and knows to stop the
            # recording first.
            if action == "start" and self.backend.is_recording():
                self._send_error_json(
                    409,
                    "stop the current recording first; jasper-voice "
                    "can't bind UDP ports the recording is using",
                )
                return
            if action == "start" and disable_outputs:
                try:
                    disable_bridge_corpus_outputs()
                except subprocess.CalledProcessError as e:
                    detail = (e.stderr or e.stdout or str(e)).strip()
                    msg = (
                        f"could not disable bridge outputs; {BRIDGE_UNIT} "
                        "restart failed and the env was rolled back"
                    )
                    if detail:
                        msg = f"{msg}: {detail[-500:]}"
                    self._send_error_json(500, msg)
                    return
                except subprocess.TimeoutExpired:
                    self._send_error_json(
                        500,
                        f"could not disable bridge outputs; {BRIDGE_UNIT} "
                        "restart timed out and the env was rolled back",
                    )
                    return
                except OSError as e:
                    self._send_error_json(
                        500,
                        f"failed to disable bridge outputs: {e}",
                    )
                    return
            try:
                subprocess.run(
                    ["systemctl", action, VOICE_UNIT], check=True,
                )
            except subprocess.CalledProcessError as e:
                self._send_error_json(500, f"systemctl {action} failed: {e}")
                return
            self._send_json({
                "action": action,
                "voice_daemon_active": voice_daemon_active(),
                "bridge_outputs": bridge_output_status(),
            })
            return

        self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")

    # ----- DELETE -----------------------------------------------------

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._check_csrf():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = path.split("/")
        # /api/clip/<id>
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "clip":
            clip_id = parts[3]
            ok = self.backend.delete_clip(clip_id)
            if not ok:
                self._send_error_json(404, "clip not found")
                return
            self._send_json({"deleted": clip_id})
            return
        # /api/session/<id> — hard-delete a whole session (WAVs + JSON)
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "session":
            session_id = parts[3]
            try:
                result = self.backend.delete_session(session_id)
            except ValueError as e:
                self._send_error_json(404, str(e))
                return
            except StateError as e:
                self._send_error_json(409, str(e))
                return
            self._send_json({"deleted_session": session_id, **result})
            return
        self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")


def _make_handler_class(
    backend: RecordingBackend, csrf_token: str,
) -> type[_Handler]:
    class _BoundHandler(_Handler):
        pass
    _BoundHandler.backend = backend
    _BoundHandler.csrf_token = csrf_token
    return _BoundHandler


def make_server(
    target,
    *,
    csrf_token: str,
    backend: RecordingBackend,
) -> ThreadingHTTPServer:
    """Construct the recorder's HTTP server bound to `target`.

    `target` is either:
      - an `(host, port)` tuple for direct binding (CLI use)
      - a `socket.socket` already bound by systemd (socket-activation
        path via `jasper.web.__main__`)
      - an `int` port (legacy direct-bind shortcut)

    Pairs with `jasper.web._systemd.make_http_server` to handle the
    socket-vs-bind branching. The backend must already be `start()`ed
    by the caller (the asyncio loop thread + crash-recovery state both
    depend on it).
    """
    from . import _systemd
    handler_cls = _make_handler_class(backend, csrf_token)
    return _systemd.make_http_server(target, handler_cls)


# ---------------------------------------------------------------------------
# Frontend — body fragment for canonical_page(); CSS + JS are static assets
# ---------------------------------------------------------------------------
#
# The page renders through `canonical_page()` (shared /assets/app.css look).
# This template is just the <body> fragment: the canonical sticky header, the
# <main class="page"> with the recorder's cards (identical element IDs so the
# behaviour module binds the same way), the JSON config island carrying the
# Python-built leg labels/order, and the ES module entry point. Page-specific
# CSS lives in /assets/wake-corpus/wake-corpus.css (linked via page_css_href);
# the behaviour lives in /assets/wake-corpus/js/main.js.


_INDEX_BODY_TEMPLATE = """{header}
<main class="page">
  <div class="card" id="status-card">
    <div class="row">
      <label>Mode:</label>
      <span id="corpus-mode-status" class="pill gray">checking…</span>
      <button id="corpus-mode-exit" style="margin-left:auto">Exit corpus test mode</button>
    </div>
    <div class="row">
      <label>jasper-voice:</label>
      <span id="voice-status" class="pill gray">checking…</span>
    </div>
    <div class="row">
      <label>Extra corpus outputs:</label>
      <span id="bridge-output-status" class="pill gray">checking…</span>
    </div>
    <div class="row">
      <label>Session:</label>
      <span id="session-id">(no session)</span>
    </div>
  </div>

  <div class="card" id="session-card">
    <h2 id="session-card-title" style="margin-top:0">Begin a new session</h2>
    <div class="row">
      <label for="member">Name:</label>
      <input type="text" id="member" value="jasper" maxlength="20">
    </div>
    <div class="row checkbox">
      <input type="checkbox" id="include-chip-aec-profile" checked>
      <label for="include-chip-aec-profile">
        Use <strong>chip AEC comparison profile</strong>
        <span class="hint">— captures chip AEC ASR 150/210, XVF raw0,
        XVF raw0 WebRTC AEC3, USB raw/AEC3, and the final speaker reference.</span>
      </label>
    </div>
    <div class="row checkbox">
      <input type="checkbox" id="include-raw-mic-0">
      <label for="include-raw-mic-0">
        Also capture <strong>raw mic 0</strong>
        <span class="hint">— chip channel 2, no DSP. Useful for
        future-proofing against cheaper mics; adds one WAV per clip.</span>
      </label>
    </div>
    <div class="row checkbox">
      <input type="checkbox" id="include-xvf-raw0-dtln">
      <label for="include-xvf-raw0-dtln">
        Also capture <strong>XVF raw0 DTLN</strong>
        <span class="hint">— neural AEC on the same raw XVF mic element;
        useful but heavier than the WebRTC AEC3 path.</span>
      </label>
    </div>
    <div class="row checkbox">
      <input type="checkbox" id="include-dtln" checked>
      <label for="include-dtln">
        Capture <strong>XVF DTLN</strong>
        <span class="hint">— chip ASR-beam raw through the neural AEC
        comparison path; requires the bridge DTLN env to be enabled.</span>
      </label>
    </div>
    <div class="row checkbox">
      <input type="checkbox" id="include-aec3-sweep">
      <label for="include-aec3-sweep">
        Capture <strong>USB AEC3 sweep</strong>
        <span class="hint">— USB edge-combo WebRTC AEC3 at 40/80/120/160 ms
        delay hints, with the XVF AEC3 leg kept for comparison. Use this
        as a pilot tuning mode; leave DTLN off while comparing these legs.</span>
      </label>
    </div>
    <div class="row checkbox">
      <input type="checkbox" id="include-usb-mic">
      <label for="include-usb-mic">
        Also capture <strong>USB mic + reference</strong>
        <span class="hint">— corpus-only cheap mic raw, cheap mic WebRTC AEC3,
        and the 16 kHz reference the bridge feeds into AEC.</span>
      </label>
    </div>
    <div class="row checkbox">
      <input type="checkbox" id="include-usb-dtln">
      <label for="include-usb-dtln">
        Also capture <strong>USB DTLN</strong>
        <span class="hint">— cheap USB raw through neural AEC. This also
        records the USB raw and reference companion legs.</span>
      </label>
    </div>
    <div class="session-primary-actions">
      <button id="session-begin" class="primary">
        Enter corpus test mode &amp; begin session
      </button>
      <button id="session-unload" style="display:none">Unload session</button>
    </div>
  </div>

  <details class="card" id="sessions-card">
    <summary style="cursor:pointer">
      <strong>Sessions</strong>
      <span style="color:#888; font-size:0.86em; margin-left:0.4em">
        load or delete previous recordings
      </span>
    </summary>
    <div id="sessions-list" style="margin-top:0.8em">(loading…)</div>
    <p style="margin:0.6em 0 0; color:#888; font-size:0.86em">
      Tap <strong>Load</strong> to resume an existing session, or
      <strong>Delete</strong> to remove its WAVs + metadata permanently.
    </p>
  </details>

  <div class="card" id="record-card" style="display:none">
    <h2 style="margin-top:0">Record a clip</h2>
    <div class="row">
      <label>Condition:</label>
      <div class="conditions">
        <label><input type="radio" name="condition" value="quiet" checked><span>quiet</span></label>
        <label><input type="radio" name="condition" value="ambient"><span>ambient (AC/fridge)</span></label>
        <label><input type="radio" name="condition" value="music"><span>music</span></label>
      </div>
    </div>
    <div class="row">
      <label>Distance:</label>
      <div class="distances">
        <label><input type="radio" name="distance" value="near" checked><span>near ~1m</span></label>
        <label><input type="radio" name="distance" value="mid"><span>mid ~2m</span></label>
        <label><input type="radio" name="distance" value="far"><span>far ~3-4m</span></label>
      </div>
    </div>
    <p style="margin:0.8em 0; color:#666; font-size:0.92em">
      Click the button (or press <kbd>Space</kbd>) to start. Say
      <strong>"Jarvis"</strong>. Click again to stop.
    </p>
    <div class="mic-level" id="mic-level">
      <span class="mic-level-label">Mic level:</span>
      <div class="mic-level-track"><div id="mic-level-fill" class="mic-level-fill"></div></div>
      <span id="mic-level-readout" class="mic-level-readout">—</span>
    </div>
    <button id="record-btn" class="primary recordBtn" disabled>● RECORD</button>
    <div id="recording-info" style="display:none; margin-top:0.6em">
      <span class="pill red">RECORDING</span>
      <span id="elapsed" style="margin-left:0.6em">0.0s</span>
    </div>
    <div id="err" class="err"></div>
  </div>

  <div class="card" id="counts-card" style="display:none">
    <h2 style="margin-top:0">Per-cell counts</h2>
    <div id="counts-matrix" class="matrix"></div>
    <p style="margin:0.6em 0 0; color:#888; font-size:0.86em">
      Session A: ~7-9 per cell. Session B: ~2-3 per cell.
    </p>
  </div>

  <div class="card" id="clips-card" style="display:none">
    <h2 style="margin-top:0">Recorded clips (this session)</h2>
    <div class="clip" style="font-weight:600; border-bottom:2px solid #333">
      <span>#</span><span>condition</span><span>distance</span>
      <span>duration</span><span>audio</span><span></span>
    </div>
    <div id="clips-list"></div>
  </div>
</main>
<script type="application/json" id="wake-corpus-config">{config_json}</script>
<script type="module" src="/assets/wake-corpus/js/main.js"></script>
"""


def _render_index_html(csrf_token: str = "") -> str:
    """Render the recorder page on the canonical design system.

    Returns the full HTML document (str). The document shell comes from
    ``canonical_page()`` (shared /assets/app.css); the body is the
    ``_INDEX_BODY_TEMPLATE`` fragment with the canonical header injected.

    The Python-built leg labels + playback order (which depend on the
    AEC3 sweep registry and so can't live in the cached ES module) are
    serialized into a ``<script type="application/json">`` island the
    behaviour module reads at load time. ``json.dumps`` escapes the
    values; we additionally guard the ``</`` sequence so a label can
    never close the inline ``<script>`` element early.
    """
    aec3_playback_legs = AEC3_SWEEP_LEGS + LEGACY_AEC3_SWEEP_LEGS
    config = {
        "aec3_sweep_labels": {
            leg: LEG_LABELS[leg] for leg in aec3_playback_legs
        },
        "aec3_sweep_order": list(aec3_playback_legs),
        "usb_aec3_corpus_label": USB_AEC3_CORPUS_LABEL,
        "usb_aec3_sweep_baseline_label": USB_AEC3_SWEEP_BASELINE_LABEL,
    }
    # `</script>` can't appear literally inside an inline <script>; escape the
    # `<` of any `</` so the JSON island can't be closed early. json.dumps has
    # already escaped quotes/backslashes.
    config_json = json.dumps(config).replace("</", "<\\/")
    header = canonical_header("Wake-word corpus")
    body = _INDEX_BODY_TEMPLATE.replace("{header}", header).replace(
        "{config_json}", config_json,
    )
    return canonical_page(
        "Wake-word corpus",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/wake-corpus/wake-corpus.css",
    ).decode("utf-8")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-wake-corpus-web",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Bind host (default {DEFAULT_HOST}; use 0.0.0.0 for LAN access).",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Bind port (default {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(os.environ.get("JASPER_WAKE_TRAIN_DATA", "data"))
        / "enrollment_positives",
        help="Output root for WAVs + metadata. Layout matches "
             "jasper-wake-enroll (default ./data/enrollment_positives).",
    )
    parser.add_argument(
        "--aec-on-port", type=int, default=DEFAULT_AEC_ON_PORT,
        help=f"UDP port for AEC ON leg (default {DEFAULT_AEC_ON_PORT}).",
    )
    parser.add_argument(
        "--aec-off-port", type=int, default=DEFAULT_AEC_OFF_PORT,
        help=f"UDP port for AEC OFF (raw chip-direct) leg (default {DEFAULT_AEC_OFF_PORT}).",
    )
    parser.add_argument(
        "--aec-dtln-port", type=int, default=DEFAULT_AEC_DTLN_PORT,
        help=f"UDP port for DTLN leg (default {DEFAULT_AEC_DTLN_PORT}).",
    )
    parser.add_argument(
        "--aec-raw0-port", type=int, default=DEFAULT_AEC_RAW0_PORT,
        help=f"UDP port for raw mic 0 leg (default {DEFAULT_AEC_RAW0_PORT}).",
    )
    parser.add_argument(
        "--aec-ref-port", type=int, default=DEFAULT_AEC_REF_PORT,
        help=f"UDP port for corpus reference leg (default {DEFAULT_AEC_REF_PORT}).",
    )
    parser.add_argument(
        "--aec-usb-raw-port", type=int, default=DEFAULT_AEC_USB_RAW_PORT,
        help=f"UDP port for cheap USB raw leg (default {DEFAULT_AEC_USB_RAW_PORT}).",
    )
    parser.add_argument(
        "--aec-usb-webrtc-port", type=int, default=DEFAULT_AEC_USB_WEBRTC_PORT,
        help="UDP port for cheap USB WebRTC leg "
             f"(default {DEFAULT_AEC_USB_WEBRTC_PORT}).",
    )
    parser.add_argument(
        "--aec-usb-dtln-port", type=int, default=DEFAULT_AEC_USB_DTLN_PORT,
        help="UDP port for cheap USB DTLN leg "
             f"(default {DEFAULT_AEC_USB_DTLN_PORT}).",
    )
    parser.add_argument(
        "--aec-chip-aec-150-port", type=int,
        default=DEFAULT_AEC_CHIP_AEC_150_PORT,
        help="UDP port for chip AEC fixed-beam 150-degree leg "
             f"(default {DEFAULT_AEC_CHIP_AEC_150_PORT}).",
    )
    parser.add_argument(
        "--aec-chip-aec-210-port", type=int,
        default=DEFAULT_AEC_CHIP_AEC_210_PORT,
        help="UDP port for chip AEC fixed-beam 210-degree leg "
             f"(default {DEFAULT_AEC_CHIP_AEC_210_PORT}).",
    )
    parser.add_argument(
        "--aec-xvf-raw0-webrtc-aec3-port", type=int,
        default=DEFAULT_AEC_XVF_RAW0_WEBRTC_AEC3_PORT,
        help="UDP port for XVF raw0 WebRTC AEC3 comparison leg "
             f"(default {DEFAULT_AEC_XVF_RAW0_WEBRTC_AEC3_PORT}).",
    )
    parser.add_argument(
        "--aec-xvf-raw0-dtln-port", type=int,
        default=DEFAULT_AEC_XVF_RAW0_DTLN_PORT,
        help="UDP port for XVF raw0 DTLN comparison leg "
             f"(default {DEFAULT_AEC_XVF_RAW0_DTLN_PORT}).",
    )
    for variant in AEC3_SWEEP_VARIANTS:
        parser.add_argument(
            f"--{variant.leg.replace('_', '-')}-port",
            type=int,
            default=DEFAULT_AEC3_SWEEP_PORTS[variant.leg],
            dest=f"aec3_sweep_port_{variant.leg}",
            help=(
                f"UDP port for {variant.label} sweep leg "
                f"(default {DEFAULT_AEC3_SWEEP_PORTS[variant.leg]})."
            ),
        )
    parser.add_argument(
        "--no-dtln", action="store_true",
        help="Skip the DTLN leg entirely (for 2-stream Pis or "
             "JASPER_WAKE_LEG_DTLN=0).",
    )
    parser.add_argument(
        "--no-usb-corpus", action="store_true",
        help="Hide corpus-only ref/USB leg ports from this recorder process.",
    )
    parser.add_argument(
        "--no-require-root", action="store_true",
        help="Skip the root check. Useful for dev — but voice-daemon "
             "start/stop won't work without sudo, and UDP bind may "
             "fail if other processes hold the ports.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.no_require_root:
        require_root()

    ports = build_ports(
        aec_on_port=args.aec_on_port,
        aec_off_port=args.aec_off_port,
        aec_dtln_port=args.aec_dtln_port,
        aec_raw0_port=args.aec_raw0_port,
        aec_ref_port=args.aec_ref_port,
        aec_usb_raw_port=args.aec_usb_raw_port,
        aec_usb_webrtc_port=args.aec_usb_webrtc_port,
        aec_usb_dtln_port=args.aec_usb_dtln_port,
        aec_chip_aec_150_port=args.aec_chip_aec_150_port,
        aec_chip_aec_210_port=args.aec_chip_aec_210_port,
        aec_xvf_raw0_webrtc_aec3_port=args.aec_xvf_raw0_webrtc_aec3_port,
        aec_xvf_raw0_dtln_port=args.aec_xvf_raw0_dtln_port,
        aec3_sweep_ports={
            variant.leg: getattr(args, f"aec3_sweep_port_{variant.leg}")
            for variant in AEC3_SWEEP_VARIANTS
        },
        include_dtln=not args.no_dtln,
        include_usb=not args.no_usb_corpus,
    )

    backend = RecordingBackend(args.output, ports=ports)
    backend.start()
    # CSRF token regenerated each process startup. If you reload the
    # tab the page picks up the current token; old tabs keep their
    # stale token and get 403s until reload — acceptable UX for an
    # operator tool that runs for a single session.
    csrf_token = secrets.token_hex(16)
    try:
        server = make_server(
            (args.host, args.port),
            csrf_token=csrf_token,
            backend=backend,
        )
        logger.info(
            "jasper-wake-corpus-web on http://%s:%d  output=%s  legs=%s",
            args.host, args.port, args.output,
            ",".join(ports.keys()),
        )
        logger.info(
            "Open http://<this-host>:%d in a browser. Ctrl-C to stop.",
            args.port,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("shutting down on Ctrl-C")
    finally:
        backend.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
