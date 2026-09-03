# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AEC bridge — `jasper-aec-bridge` (Python).

Feeds jasper-voice's UDP mic legs from the XVF3800 capture stream, with
outputd's final speaker monitor as the far-end reference. Two modes:

- Default (software AEC): WebRTC AEC3 via the `jasper_aec3` pybind11
  binding, near-end = the XVF capture channel `MIC_CHANNEL_INDEX` names,
  far-end = the downsampled speaker reference. Costs ~3-8% of one Pi 5
  core.
- Chip AEC (`JASPER_AEC_CHIP_AEC_ENABLED=1`): no WebRTC engine is
  instantiated. outputd routes the final speaker buffer into the XVF3800
  USB-IN reference; the bridge captures the chip's fixed 150°/210° ASR
  beams and forwards the selected primary beam on the `on` leg, emitting
  the extra beams only when the reconciler publishes their runtime device
  env vars. The wake-corpus recorder uses the same chip profile under its
  corpus-only flag.

XVF3800 channel map (6-ch firmware): channels 0/1 are the chip's processed
lanes with `SHF_BYPASS=0` — in chip-AEC mode the fixed 150°/210° ASR beams —
and raw-ish lanes with `SHF_BYPASS=1`, the default production state set by
jasper-aec-init. Channels 2-5 bypass every chip DSP stage: no BF, NS, AGC,
HPF, not even MIC_GAIN. The canonical voice-assistant capture is channel 0/1.

Topology:

    outputd UDP final-speaker monitor (48k stereo speaker reference)
       │  reference signal (what the speaker is being asked to play)
       ▼
    [downsample 48→16k, L+R summed to mono, HPF at 125 Hz]         16k mono ref
       │
       │      hw:<XVF card>,0 ch 1 (16k mono, chip clock)
       │  default production mic: raw-ish channel 1, chip AEC disabled
       │       │
       ▼       ├──────────────────────────────────────────────────┐
    WebRTC AEC3 (default) OR chip beam passthrough (chip mode)     │
       │  AEC'd mono mic                                          │  chip-direct mic (pre-AEC3)
       ▼                                                          ▼
    UDP 127.0.0.1:JASPER_AEC_UDP_PORT (default 9876)      UDP 127.0.0.1:JASPER_AEC_UDP_PORT_RAW
       │  one packet per 1280 samples (80 ms, matches             │  (default 9877)
       │  MicCapture frame size; the USB host-mic leg              │  same packet shape
       │  emits one 320-sample / 20 ms frame)                     │
       ▼                                                          ▼
    jasper-voice's UdpMicCapture (binds 9876)             jasper-voice's second
                                                          UdpMicCapture (binds 9877)
                                                          for dual-stream wake-word
                                                          detection.

Every leg's token and UDP port is owned by `jasper.wake_legs`. Why UDP
rather than an snd-aloop card: see `UdpMicCapture` in jasper/audio_io.py.

Reference and mic run on independent clock domains — outputd's DAC-paced
sender against the XVF chip's USB UAC2 clock — and will drift. AEC3's delay
estimator tolerates bounded drift; nothing here resamples to compensate, and
`JASPER_AEC_STREAM_DELAY_MS` is only a starting hint for that estimator.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, replace
import logging
import math
import os
import socket
import signal
import sys
import threading
import time
from queue import Queue, Empty, Full
from pathlib import Path
from typing import Any, Optional

import numpy as np
import sounddevice as sd

from jasper.aec_sweep import (
    AEC3_SWEEP_ENV_FLAG,
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    DEFAULT_AEC3_SWEEP_VARIANTS,
    Aec3SweepConfig,
    Aec3SweepConfigError,
    Aec3SweepVariant,
    current_aec3_sweep_source,
    load_aec3_sweep_config,
)
from jasper.dsp_numpy import butter2_highpass_sos, resample_poly, sosfilt
from jasper.watchdog import Heartbeat
from jasper import wake_legs
from jasper.wake_corpus.capture_plan import (
    DAC_FINGERPRINT_ENV,
    EXPECTED_LEGS_ENV,
    MIC_FINGERPRINT_ENV,
    PLAN_ID_ENV,
)
from jasper.log_event import log_event
from jasper.cli.aec_bridge_engines import (
    Aec3Engine,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    # `_aec_loop` and `main` resolve the engine selector through this alias,
    # so it — not `aec_bridge_engines.select_engine` — is what a test patches
    # to reach the loop.
    select_engine as _select_engine,
)
from jasper.cli.aec_bridge_telemetry import (
    BRIDGE_STATS_PATH,
    LegEmitter,
    OUT_FRAME_BYTES,
    OUT_FRAME_SAMPLES,
    StatsIdentity,
    TimestampedLegEmitter,
    _BridgeStats,
)
from jasper.usb_mic import (
    INTENT_PATH as USB_MIC_INTENT_PATH,
    USB_HOST_MIC_UDP_PORT,
    USB_MIC_LEG_KEY,
    USB_MIC_PRIMARY_LEG,
    USB_MIC_RAW_XVF_LEG,
    usb_mic_enabled,
)
from ..mics import xvf3800 as _mic_profile

logger = logging.getLogger("jasper.aec_bridge")
AEC3_SWEEP_VARIANTS = DEFAULT_AEC3_SWEEP_VARIANTS

# Above this, `jasper.dsp_numpy.resample_poly` runs one Python-level polyphase
# branch per unit of `max(up, down)` and stops fitting a capture callback: a
# 44.1 kHz card reduces to 160/441 and costs ~2 ms per block on a laptop
# against a 20 ms budget, where scipy's C `upfirdn` costs 0.7 ms. Every
# integer-ratio card (32/48/96 kHz -> 1/2, 1/3, 1/6) stays well under 8 and is
# 3-5x *faster* in numpy, so only the 44.1 kHz family reaches for scipy.
MAX_NUMPY_POLYPHASE_BRANCHES = 8

# Wire geometry of the far-end reference. outputd sends its final speaker
# monitor at this rate/channel count; `_ReferenceFrameConverter` folds it to
# the 16 kHz mono frames AEC3 consumes. The bridge has no other reference
# transport — see REF_SOURCE below.
REF_RATE = 48000
REF_CHANNELS = 2

# Capture geometry for the mic; the profile pins the near-end lane for the
# default WebRTC AEC3 path (channels 2-5 are raw mics 0-3, no chip DSP).
# The device name is a PortAudio substring match — NOT an ALSA pcm string:
# PortAudio enumerates ALSA cards by card description, not `hw:CARD=` syntax.
# The default matches "Array: USB Audio (hw:N,0)" on the legacy square
# firmware and "L16K6Ch: USB Audio (hw:N,0)" on the Flex linear firmware.
MIC_CHANNELS = _mic_profile.RECOMMENDED_FIRMWARE.capture_channels
MIC_CHANNEL_INDEX = _mic_profile.MIC_CHANNEL_INDEX

# Output transport: UDP localhost. The bridge sends AEC'd mono int16 frames
# to `127.0.0.1:JASPER_AEC_UDP_PORT`; jasper-voice's `UdpMicCapture` binds
# the same port and receives.
OUT_HOST = "127.0.0.1"


def _leg_default_port(token: str) -> int:
    return wake_legs.by_token(token).udp_port


OUT_PORT = _leg_default_port("on")
OUT_RATE = 16000

# Chip-direct mic stream, pre-AEC3 — exactly the near-end input AEC3
# consumes in default production (chip ch 1, raw-ish when SHF_BYPASS=1), on
# its own port and in the same 1280-sample / 16 kHz mono int16 packet shape
# as the primary leg. jasper-voice's wake loop ORs detections across the
# post-AEC (OUT_PORT) and chip-direct legs, which catch mostly-disjoint sets
# of utterances. It consumes this leg only when the reconciler configures
# `JASPER_MIC_DEVICE_RAW`; otherwise the extra packets are ignored.
OUT_PORT_RAW = _leg_default_port("off")
# 4th UDP stream: truly-raw mic 0 (chip channel 2). Unlike the chip-direct
# stream on OUT_PORT_RAW (chip channel 1 = ASR beam, with chip BF+NS+AGC+HPF
# applied), channel 2 is the raw mic 0 ADC output with NO chip DSP whatsoever
# — not even MIC_GAIN, i.e. what a mic without an XMOS chip would deliver.
# Read by the wake-corpus recorder as the mic-agnostic baseline. Same packet
# shape as the other legs; always emitted, at ~0.25% of one core.
OUT_PORT_RAW0 = _leg_default_port("raw0")
# Corpus-only experiment streams, off by default so production bridge cost
# does not move. When enabled for wake-corpus recording, the bridge emits:
#   - ref: the 16 kHz mono reference frame AEC3 actually consumed
#   - usb_raw: a cheap USB mic's raw mono capture
#   - usb_webrtc: that same USB mic through a second WebRTC AEC3 chain
#   - usb_dtln: the cheap USB mic through a second DTLN-aec chain
#
# jasper-voice never consumes these.
OUT_PORT_REF = _leg_default_port("ref")
OUT_PORT_USB_RAW = _leg_default_port("usb_raw")
OUT_PORT_USB_WEBRTC = _leg_default_port("usb_webrtc")
OUT_PORT_USB_DTLN = _leg_default_port("usb_dtln")
OUT_PORT_CHIP_AEC_150 = _leg_default_port("chip_aec_150")
OUT_PORT_CHIP_AEC_210 = _leg_default_port("chip_aec_210")
OUTPUTD_REF_UDP_HOST = "127.0.0.1"
OUTPUTD_REF_UDP_PORT = 9891
# The bridge's only reference source. Software AEC3, chip-AEC, corpus,
# and diagnostics all consume outputd's final speaker monitor, so they
# all see the same reference contract.
REF_SOURCE = "outputd_udp"
# Retired reference source: the summed snd-aloop tap, whose path and tap are
# both deleted. A box whose /etc/jasper/jasper.env still carries this value
# converges on the next `jasper-aec-reconcile` run, so the bridge warns and
# uses REF_SOURCE rather than refusing to start: a hard failure here would
# leave jasper-voice with an unfed UDP mic and no wake detection.
RETIRED_REF_SOURCE_ALSA = "alsa"
OUT_PORT_AEC3_SWEEP = {
    variant.leg: variant.default_port
    for variant in AEC3_SWEEP_VARIANTS
}
USB_MIC_DEVICE = "USB PnP Sound Device"
USB_MIC_RATE = 0
CAPTURE_LATENCY_MAX_SECONDS = 0.25

# Drop-frame threshold: if queues fill faster than they drain (CPU
# starvation, clock drift past the margin), log and drop rather than block.
QUEUE_MAXSIZE = 32

_shutdown = threading.Event()


@dataclass(frozen=True)
class BridgeConfig:
    mic_device: str
    capture_latency: str
    out_host: str
    out_port: int
    out_port_raw: int
    out_port_dtln: int
    out_port_raw0: int
    out_port_ref: int
    out_port_usb_raw: int
    out_port_usb_webrtc: int
    out_port_usb_dtln: int
    out_port_chip_aec_150: int
    out_port_chip_aec_210: int
    emit_chip_aec_150: bool
    emit_chip_aec_210: bool
    out_port_xvf_raw0_webrtc_aec3: int
    out_port_xvf_raw0_dtln: int
    out_port_usb_host_mic: int
    emit_usb_host_mic: bool
    usb_mic_leg: str
    outputd_ref_udp_host: str
    outputd_ref_udp_port: int
    ref_source: str
    out_port_aec3_sweep: dict[str, int]
    usb_mic_device: str
    usb_mic_rate: int
    bridge_stats_path: Path
    aec3_sweep_config: Aec3SweepConfig
    aec3_sweep_variants: tuple[Aec3SweepVariant, ...]
    aec3_sweep_input_source: str
    wake_corpus_plan_id: str
    wake_corpus_expected_legs: tuple[str, ...]
    wake_corpus_mic_fingerprint: str
    wake_corpus_dac_fingerprint: str

    @classmethod
    def from_env(
        cls,
        *,
        log_sweep: bool = False,
        logger_: logging.Logger | None = None,
    ) -> "BridgeConfig":
        log = logger_ or logger
        sweep_config = load_aec3_sweep_config(logger=log if log_sweep else None)
        try:
            sweep_input_source = current_aec3_sweep_source()
        except Aec3SweepConfigError as e:
            if log_sweep:
                log_event(
                    log,
                    "aec3_sweep_source_invalid",
                    error=str(e),
                    fallback=AEC3_SWEEP_SOURCE_XVF,
                    level=logging.WARNING,
                )
            sweep_input_source = AEC3_SWEEP_SOURCE_XVF

        if log_sweep:
            log_event(
                log,
                "aec3_sweep_config_loaded",
                source=sweep_config.source,
                path=sweep_config.path,
                hash=sweep_config.config_hash,
                input_source=sweep_input_source,
                variants=",".join(variant.leg for variant in sweep_config.variants),
            )

        def _env_leg_port(env_var: str, token: str) -> int:
            return int(os.environ.get(env_var, str(_leg_default_port(token))))

        corpus_chip_aec_enabled = _env_bool(
            "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "0",
        )
        capture_latency = os.environ.get("JASPER_AEC_CAPTURE_LATENCY", "").strip()
        if capture_latency and capture_latency.lower() != "low":
            try:
                capture_latency_seconds = float(capture_latency)
            except ValueError:
                capture_latency_seconds = 0.0
            if (
                not math.isfinite(capture_latency_seconds)
                or capture_latency_seconds <= 0
                or capture_latency_seconds > CAPTURE_LATENCY_MAX_SECONDS
            ):
                log_event(
                    log,
                    "aec.capture_latency_invalid",
                    value=capture_latency,
                    fallback="default",
                    level=logging.WARNING,
                )
                capture_latency = ""

        return cls(
            mic_device=os.environ.get(
                "JASPER_AEC_MIC_DEVICE",
                _mic_profile.alsa_card_name(),
            ),
            capture_latency=capture_latency.lower(),
            out_host=os.environ.get("JASPER_AEC_UDP_HOST", OUT_HOST),
            out_port=_env_leg_port("JASPER_AEC_UDP_PORT", "on"),
            out_port_raw=_env_leg_port("JASPER_AEC_UDP_PORT_RAW", "off"),
            out_port_dtln=_env_leg_port("JASPER_AEC_UDP_PORT_DTLN", "dtln"),
            out_port_raw0=_env_leg_port("JASPER_AEC_UDP_PORT_RAW0", "raw0"),
            out_port_ref=_env_leg_port("JASPER_AEC_UDP_PORT_REF", "ref"),
            out_port_usb_raw=_env_leg_port("JASPER_AEC_UDP_PORT_USB_RAW", "usb_raw"),
            out_port_usb_webrtc=_env_leg_port(
                "JASPER_AEC_UDP_PORT_USB_WEBRTC",
                "usb_webrtc",
            ),
            out_port_usb_dtln=_env_leg_port(
                "JASPER_AEC_UDP_PORT_USB_DTLN",
                "usb_dtln",
            ),
            out_port_chip_aec_150=_env_leg_port(
                "JASPER_AEC_UDP_PORT_CHIP_AEC_150",
                "chip_aec_150",
            ),
            out_port_chip_aec_210=_env_leg_port(
                "JASPER_AEC_UDP_PORT_CHIP_AEC_210",
                "chip_aec_210",
            ),
            emit_chip_aec_150=(
                corpus_chip_aec_enabled
                or bool(
                    os.environ.get(
                        "JASPER_MIC_DEVICE_CHIP_AEC_150", "",
                    ).strip()
                )
            ),
            emit_chip_aec_210=(
                corpus_chip_aec_enabled
                or bool(
                    os.environ.get(
                        "JASPER_MIC_DEVICE_CHIP_AEC_210", "",
                    ).strip()
                )
            ),
            out_port_xvf_raw0_webrtc_aec3=_env_leg_port(
                "JASPER_AEC_UDP_PORT_XVF_RAW0_WEBRTC_AEC3",
                "xvf_raw0_webrtc_aec3",
            ),
            out_port_xvf_raw0_dtln=_env_leg_port(
                "JASPER_AEC_UDP_PORT_XVF_RAW0_DTLN",
                "xvf_raw0_dtln",
            ),
            # Product wiring, not an operator knob: the relay owns the paired
            # listener constant and accessories are regression-guarded from it.
            out_port_usb_host_mic=USB_HOST_MIC_UDP_PORT,
            emit_usb_host_mic=usb_mic_enabled(
                os.environ.get("JASPER_USB_MIC_INTENT_PATH", USB_MIC_INTENT_PATH)
            ),
            usb_mic_leg=(
                os.environ.get(USB_MIC_LEG_KEY, USB_MIC_PRIMARY_LEG).strip()
                or USB_MIC_PRIMARY_LEG
            ),
            outputd_ref_udp_host=os.environ.get(
                "JASPER_AEC_OUTPUTD_REF_UDP_HOST",
                OUTPUTD_REF_UDP_HOST,
            ),
            outputd_ref_udp_port=int(
                os.environ.get(
                    "JASPER_AEC_OUTPUTD_REF_UDP_PORT",
                    str(OUTPUTD_REF_UDP_PORT),
                )
            ),
            ref_source=os.environ.get(
                "JASPER_AEC_REF_SOURCE",
                REF_SOURCE,
            ).strip().lower(),
            out_port_aec3_sweep={
                variant.leg: variant.default_port
                for variant in sweep_config.variants
            },
            usb_mic_device=os.environ.get(
                "JASPER_AEC_USB_MIC_DEVICE",
                USB_MIC_DEVICE,
            ),
            usb_mic_rate=int(float(os.environ.get(
                "JASPER_AEC_USB_MIC_RATE",
                str(USB_MIC_RATE),
            ))),
            bridge_stats_path=Path(os.environ.get(
                "JASPER_AEC_BRIDGE_STATS_PATH",
                str(BRIDGE_STATS_PATH),
            )),
            aec3_sweep_config=sweep_config,
            aec3_sweep_variants=sweep_config.variants,
            aec3_sweep_input_source=sweep_input_source,
            wake_corpus_plan_id=os.environ.get(PLAN_ID_ENV, "").strip(),
            wake_corpus_expected_legs=tuple(
                leg.strip()
                for leg in os.environ.get(EXPECTED_LEGS_ENV, "").split(",")
                if leg.strip()
            ),
            wake_corpus_mic_fingerprint=os.environ.get(
                MIC_FINGERPRINT_ENV, "",
            ).strip(),
            wake_corpus_dac_fingerprint=os.environ.get(
                DAC_FINGERPRINT_ENV, "",
            ).strip(),
        )


@dataclass
class _DropLogDebouncer:
    interval_sec: float = 1.0
    drops_in_window: int = 0
    last_log: float = 0.0

    def record(self, now: float) -> tuple[int, float] | None:
        return self.record_many(now, 1)

    def record_many(self, now: float, drops: int) -> tuple[int, float] | None:
        self.drops_in_window += max(0, drops)
        return self.flush(now)

    def flush(self, now: float) -> tuple[int, float] | None:
        if self.drops_in_window <= 0:
            return None
        if self.last_log and now - self.last_log < self.interval_sec:
            return None
        window_sec = now - self.last_log if self.last_log else self.interval_sec
        drops = self.drops_in_window
        self.drops_in_window = 0
        self.last_log = now
        return drops, window_sec


_STATS_IDENTITY = StatsIdentity(
    sample_rate_hz=SAMPLE_RATE,
    frame_samples=FRAME_SAMPLES,
    reference_source=REF_SOURCE,
    reference_endpoint=f"{OUTPUTD_REF_UDP_HOST}:{OUTPUTD_REF_UDP_PORT}",
)
_bridge_stats = _BridgeStats(_STATS_IDENTITY)


def _bridge_stats_writer(path: Path = BRIDGE_STATS_PATH) -> None:
    while not _shutdown.wait(0.5):
        _bridge_stats.write_snapshot(path)
    _bridge_stats.write_snapshot(path)


class BridgeStalled(RuntimeError):
    """Mic capture has stalled — either no frames for the configured
    continuous threshold (JASPER_AEC_STALL_RESTART_SEC, default 5s) or a
    sustained sub-usable frame *rate* (the slow-drip case caught by
    `_MicStarvationWatchdog`; JASPER_AEC_STALL_DRIP_MAX_WINDOWS).

    Raised by `_aec_loop` to bail with a non-zero exit code so systemd's
    `Restart=on-failure` revives the bridge with a fresh `sd.InputStream`.
    PortAudio's InputStream is one-shot: once its ALSA capture PCM enters an
    unrecoverable state (typically a USB underrun on the XVF chip's UAC2
    endpoint) the callback simply stops being invoked, and no in-process
    recovery path exists — only a new process gets a working stream.
    """


class MicDeviceUnavailable(RuntimeError):
    """The configured PortAudio mic device is not currently present."""


class UsbMicUnavailable(RuntimeError):
    """The configured corpus USB mic device is not currently present."""


class UnsupportedReferenceSource(RuntimeError):
    """JASPER_AEC_REF_SOURCE names a source this bridge cannot read."""


# Clipping counters, module-level for cheap cross-thread access: a race
# between increment and reset costs at most one frame in one log window's
# percentage. Tracked separately for the ref pre-clip stage (after
# JASPER_AEC_REF_GAIN_DB) and the post-AEC mic stage (after
# JASPER_AEC_MIC_GAIN_DB).
_ref_clipped_samples = 0
_ref_total_samples = 0
_out_clipped_samples = 0
_out_total_samples = 0

# Counter for `ref_q empty when the main loop polled` events: the reference
# arrives in bursts while the mic delivers at a smooth 20 ms cadence, so some
# polls land between bursts (see `_aec_loop`). Logged in the periodic RMS
# line; above roughly 2 Hz, carry-forward is doing more work than expected.
_ref_starved_frames = 0


def _apply_mic_output_gain(
    pcm: bytes,
    gain_lin: float,
) -> tuple[bytes, int, int]:
    """Apply the shared post-AEC gain/soft-limit and return clip counters."""

    if gain_lin == 1.0:
        return pcm, 0, 0
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * gain_lin
    clipped = int(np.sum(np.abs(arr) > 32767))
    # tanh soft-clip: smoothly asymptotic to ±32767 instead of hard-clipping.
    arr = 32767.0 * np.tanh(arr / 32767.0)
    return arr.astype(np.int16).tobytes(), clipped, len(arr)


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _chip_beam_plan() -> _mic_profile.ChipBeamPlan | None:
    return _mic_profile.chip_beam_plan_from_env(os.environ)


def _chip_aec_primary_leg(
    plan: _mic_profile.ChipBeamPlan | None,
) -> str:
    allowed = set(plan.leg_tokens if plan else ("chip_aec_150", "chip_aec_210"))
    fallback = next(iter(plan.leg_tokens), "chip_aec_150") if plan else "chip_aec_150"
    value = os.environ.get(
        "JASPER_AEC_CHIP_AEC_PRIMARY_LEG", fallback,
    ).strip()
    if value in allowed:
        return value
    log_event(
        logger,
        "chip_aec_primary_invalid",
        value=repr(value),
        fallback=fallback,
        level=logging.WARNING,
    )
    return fallback


def _resolve_usb_mic_source(
    requested: str,
    *,
    plan: _mic_profile.ChipBeamPlan | None,
    production_chip_aec_enabled: bool,
    chip_aec_primary_leg: str,
) -> dict[str, object]:
    """Resolve the configured selector to the physical stream being emitted."""

    allowed = {USB_MIC_PRIMARY_LEG, *(plan.leg_tokens if plan else ())}
    if plan is not None:
        allowed.add(USB_MIC_RAW_XVF_LEG)
    selection = requested if requested in allowed else USB_MIC_PRIMARY_LEG
    if selection != requested:
        log_event(
            logger,
            "usb_mic.leg_invalid",
            value=repr(requested),
            fallback=USB_MIC_PRIMARY_LEG,
            beam_plan=plan.plan_id if plan else "none",
            level=logging.WARNING,
        )
    if selection == USB_MIC_RAW_XVF_LEG:
        return {
            "selection": selection,
            "mode": "raw",
            "leg": USB_MIC_RAW_XVF_LEG,
            "fallback_active": False,
        }
    if not production_chip_aec_enabled:
        fallback_active = selection != USB_MIC_PRIMARY_LEG
        if fallback_active:
            log_event(
                logger,
                "usb_mic.leg_unavailable",
                leg=selection,
                fallback="clean",
                mode="software_aec3",
                level=logging.WARNING,
            )
        return {
            "selection": selection,
            "mode": "software_aec3",
            "leg": "clean",
            "fallback_active": fallback_active,
        }
    return {
        "selection": selection,
        "mode": "chip_aec",
        "leg": (
            chip_aec_primary_leg
            if selection == USB_MIC_PRIMARY_LEG
            else selection
        ),
        "fallback_active": False,
    }


def _resolved_reference_source(config: BridgeConfig) -> BridgeConfig:
    """Return `config` with a supported `ref_source`, or reject it.

    `RETIRED_REF_SOURCE_ALSA` is converged, not rejected: a parked box can
    still carry it on disk, and refusing to start would leave jasper-voice
    with an unfed UDP mic. Anything else is a typo or a source this bridge
    genuinely cannot read, and stays a hard failure.

    Call this before anything reads `config.ref_source` — the bridge-stats
    snapshot publishes it as runtime provenance that `jasper-doctor` trusts,
    so the retired spelling must never reach it.
    """
    if config.ref_source == REF_SOURCE:
        return config
    if config.ref_source == RETIRED_REF_SOURCE_ALSA:
        log_event(
            logger,
            "aec_ref_source_retired",
            level=logging.WARNING,
            retired=config.ref_source,
            using=REF_SOURCE,
            detail=(
                "the ALSA reference fallback is gone; run "
                "`sudo systemctl start jasper-aec-reconcile` to converge "
                "/etc/jasper/jasper.env"
            ),
        )
        return replace(config, ref_source=REF_SOURCE)
    raise UnsupportedReferenceSource(
        f"unsupported JASPER_AEC_REF_SOURCE={config.ref_source!r} "
        f"(expected {REF_SOURCE!r})"
    )


def _validate_mic_device(config: BridgeConfig | None = None) -> None:
    """Fail before opening the far-end reference if the mic is absent.

    Ordering matters: missing hardware must fail before the reference thread
    and its UDP socket start.
    """
    config = config or BridgeConfig.from_env()
    try:
        sd.query_devices(config.mic_device, "input")
    except Exception as e:  # noqa: BLE001
        raise MicDeviceUnavailable(
            f"mic device {config.mic_device!r} unavailable: {e}"
        ) from e


def _validate_usb_mic_device(config: BridgeConfig | None = None) -> None:
    """Fail fast when corpus USB capture is explicitly enabled but absent."""
    config = config or BridgeConfig.from_env()
    try:
        sd.query_devices(config.usb_mic_device, "input")
    except Exception as e:  # noqa: BLE001
        raise UsbMicUnavailable(
            f"USB corpus mic device {config.usb_mic_device!r} unavailable: {e}"
        ) from e


def _usb_capture_rate(config: BridgeConfig | None = None) -> int:
    """Return the USB mic capture rate PortAudio can actually open."""
    config = config or BridgeConfig.from_env()
    if config.usb_mic_rate > 0:
        return config.usb_mic_rate
    info = sd.query_devices(config.usb_mic_device, "input")
    rate = int(round(float(info.get("default_samplerate") or SAMPLE_RATE)))
    return rate if rate > 0 else SAMPLE_RATE


@dataclass(frozen=True)
class _ReferenceFrameBatch:
    frames: tuple[bytes, ...]
    clipped_samples: int
    total_samples: int


class _ReferenceFrameConverter:
    """Stateful 48 kHz stereo -> 16 kHz mono reference conversion.

    Transport adapters own capture and lifecycle. This class owns only the
    shared DSP contract: stereo folding, exact-size accumulation, resampling,
    stateful HPF, gain, clipping telemetry, and int16 framing.

    L+R are summed, not left-only: the speakers radiate the sum into a single
    mic and AEC3 is mono-reference, so an L-only reference would be blind to
    whatever is panned right. (The XMOS chip's USB-IN AEC requires left-only
    per datasheet §3.3, but that is the chip's own reference, not this one.)

    Accumulate rather than convert per delivery: a transport can hand over
    partial or oversized buffers, so frames accumulate at 48 kHz and only
    complete `capture_block`-sized chunks are emitted. WebRTC AEC3 strictly
    enforces equal mic and reference lengths.

    `JASPER_AEC_REF_GAIN_DB` boosts the digital reference before the engine
    sees it. AEC3 is tuned for conferencing, where ref RMS ≈ mic RMS or
    louder; here the digital reference is typically 25-30 dB *quieter* than
    what the mic captures, because amp + speakers + room amplify the acoustic
    path. The boost puts the adaptive filter back near its design point.
    """

    def __init__(self, *, ref_gain_db: float, ref_hpf_hz: float) -> None:
        self.ref_gain_db = float(ref_gain_db)
        self.ref_hpf_hz = float(ref_hpf_hz)
        self.capture_block = FRAME_SAMPLES * (REF_RATE // SAMPLE_RATE)
        self._ref_gain_lin = 10.0 ** (self.ref_gain_db / 20.0)
        self._hpf_sos = butter2_highpass_sos(self.ref_hpf_hz, SAMPLE_RATE)
        self._hpf_zi = np.zeros((self._hpf_sos.shape[0], 2), dtype=np.float64)
        self._accum_48 = np.empty(0, dtype=np.float32)

    @classmethod
    def from_env(cls) -> _ReferenceFrameConverter:
        return cls(
            ref_gain_db=float(os.environ.get("JASPER_AEC_REF_GAIN_DB", "0")),
            ref_hpf_hz=float(os.environ.get("JASPER_AEC_REF_HPF_HZ", "125")),
        )

    def feed(self, interleaved: np.ndarray) -> _ReferenceFrameBatch:
        arr = np.asarray(interleaved, dtype=np.int16).reshape(-1)
        usable = arr.size - (arr.size % REF_CHANNELS)
        if usable < REF_CHANNELS:
            return _ReferenceFrameBatch((), 0, 0)
        arr = arr[:usable]

        left48 = arr[0::REF_CHANNELS].astype(np.float32)
        right48 = arr[1::REF_CHANNELS].astype(np.float32)
        mono48 = (left48 + right48) * 0.5
        self._accum_48 = np.concatenate((self._accum_48, mono48))

        frames: list[bytes] = []
        clipped_samples = 0
        total_samples = 0
        while self._accum_48.size >= self.capture_block:
            chunk = self._accum_48[:self.capture_block]
            self._accum_48 = self._accum_48[self.capture_block:]
            mono16 = resample_poly(chunk, up=1, down=3)
            mono16, self._hpf_zi = sosfilt(
                self._hpf_sos,
                mono16,
                zi=self._hpf_zi,
            )
            if self._ref_gain_lin != 1.0:
                mono16 = mono16 * self._ref_gain_lin
            clipped_samples += int(np.sum(np.abs(mono16) > 32767))
            total_samples += int(mono16.size)
            frames.append(
                np.clip(mono16, -32768, 32767).astype(np.int16).tobytes()
            )
        return _ReferenceFrameBatch(
            frames=tuple(frames),
            clipped_samples=clipped_samples,
            total_samples=total_samples,
        )


def _enqueue_reference_frames(
    ref_q: Queue,
    batch: _ReferenceFrameBatch,
    *,
    drop_log: _DropLogDebouncer,
    drop_message: str,
) -> None:
    """Publish converted frames without letting reference capture block."""
    global _ref_clipped_samples, _ref_total_samples

    _ref_clipped_samples += batch.clipped_samples
    _ref_total_samples += batch.total_samples
    dropped = 0
    enqueued = 0
    for frame in batch.frames:
        try:
            ref_q.put_nowait(frame)
            enqueued += 1
        except Full:
            dropped += 1
    _bridge_stats.record_reference_frames(enqueued)
    if dropped:
        _bridge_stats.inc_nested("queue_drops", "ref", dropped)

    now = time.monotonic()
    report = (
        drop_log.record_many(now, dropped)
        if dropped
        else drop_log.flush(now)
    )
    if report is not None:
        logger.warning(drop_message, *report)


def _outputd_ref_udp_thread(
    ref_q: Queue,
    config: BridgeConfig | None = None,
) -> None:
    """Receive outputd's final speaker-reference UDP tap and convert it
    to the 16 kHz mono frames AEC3 consumes.

    The bridge's only reference transport, and not a clocked ALSA capture
    loop: outputd sends the exact post-mix buffer it writes to the DAC.
    Software AEC, chip-AEC, corpus, and diagnostics all read it, so they all
    see the same final speaker reference.
    """
    config = config or BridgeConfig.from_env()
    converter = _ReferenceFrameConverter.from_env()
    drop_log = _DropLogDebouncer()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((config.outputd_ref_udp_host, config.outputd_ref_udp_port))
    sock.settimeout(0.5)
    logger.info(
        "outputd ref UDP opened: %s:%d @ %d Hz stereo -> %d Hz mono "
        "(pre-AEC gain=%+.1f dB, HPF=%.0f Hz 2nd Butter)",
        config.outputd_ref_udp_host, config.outputd_ref_udp_port,
        REF_RATE,
        SAMPLE_RATE,
        converter.ref_gain_db,
        converter.ref_hpf_hz,
    )
    try:
        while not _shutdown.is_set():
            try:
                data, _addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            if not data:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            _enqueue_reference_frames(
                ref_q,
                converter.feed(arr),
                drop_log=drop_log,
                drop_message=(
                    "outputd ref queue full, dropped %d frames in last %.1fs"
                ),
            )
    finally:
        sock.close()


def _mic_thread(
    mic_q: Queue,
    raw0_q: Optional[Queue] = None,
    chip_aec_qs: Optional[dict[str, Queue]] = None,
    chip_beam_plan: _mic_profile.ChipBeamPlan | None = None,
    config: BridgeConfig | None = None,
) -> None:
    """Capture 16 kHz 6-ch from the XVF chip, pluck channel
    MIC_CHANNEL_INDEX and push mono int16 frames into `mic_q`.

    In chip-AEC mode `SHF_BYPASS=0` with `OP_L/R=[7,0]/[7,1]` makes channels
    0/1 the fixed 150°/210° ASR beams; in default production `SHF_BYPASS=1`
    makes them raw-ish.

    When `raw0_q` is provided, channel 2 (raw mic 0, no chip DSP) is ALSO
    extracted onto that queue for the OUT_PORT_RAW0 leg. Independent queue
    and extraction so a backlog on one cannot stall the other.
    """
    config = config or BridgeConfig.from_env()
    mic_drop_log = _DropLogDebouncer()

    def cb(indata, frames, time_info, status):
        if status:
            logger.debug("mic status: %s", status)
        if _shutdown.is_set():
            return
        mono = indata[:, MIC_CHANNEL_INDEX].astype(np.int16, copy=True)
        try:
            mic_q.put_nowait(mono.tobytes())
        except Full:
            _bridge_stats.inc_nested("queue_drops", "mic")
            if outcome := mic_drop_log.record(time.monotonic()):
                drops, window_sec = outcome
                logger.warning(
                    "mic queue full, dropped %d frames in last %.1fs",
                    drops, window_sec,
                )
        if raw0_q is not None:
            # Channel 2 = raw mic 0 ADC output, bypassing the chip's
            # BF/NS/AGC/HPF. `copy=True` so the slice does not share backing
            # storage with `indata`, which sounddevice reuses.
            raw0 = indata[:, 2].astype(np.int16, copy=True)
            try:
                raw0_q.put_nowait(raw0.tobytes())
            except Full:
                # Observational leg: drop quietly rather than flood the
                # journal during a stall the mic_q path already reports.
                _bridge_stats.inc_nested("queue_drops", "raw0")
                pass
        if chip_aec_qs and chip_beam_plan:
            for beam in chip_beam_plan.legs:
                q = chip_aec_qs.get(beam.token)
                if q is None:
                    continue
                pcm = indata[:, beam.channel_index].astype(np.int16, copy=True)
                try:
                    q.put_nowait(pcm.tobytes())
                except Full:
                    _bridge_stats.inc_nested("queue_drops", "chip")
                    pass

    input_stream_kwargs = dict(
        device=config.mic_device, samplerate=SAMPLE_RATE, channels=MIC_CHANNELS,
        dtype="int16", blocksize=FRAME_SAMPLES, callback=cb,
    )
    if config.capture_latency:
        input_stream_kwargs["latency"] = (
            "low"
            if config.capture_latency == "low"
            else float(config.capture_latency)
        )
    with sd.InputStream(**input_stream_kwargs) as stream:
        log_event(
            logger,
            "aec.mic_stream_latency",
            latency_s=stream.latency,
            requested_latency=config.capture_latency or "default",
            samplerate=int(stream.samplerate),
            blocksize=int(stream.blocksize),
        )
        _bridge_stats.set_capture_stream(
            sample_rate_hz=int(stream.samplerate),
            block_frames=int(stream.blocksize),
            input_latency_seconds=float(stream.latency),
        )
        _shutdown.wait()


def _usb_resampler(usb_rate: int) -> tuple[Any, int, int]:
    """Pick the corpus USB mic's resampler and its rational ratio.

    Returns `(None, 1, 1)` when the card already runs at `SAMPLE_RATE`.
    """
    gcd = math.gcd(usb_rate, SAMPLE_RATE)
    up = SAMPLE_RATE // gcd
    down = usb_rate // gcd
    if up == down == 1:
        return None, 1, 1
    if max(up, down) <= MAX_NUMPY_POLYPHASE_BRANCHES:
        return resample_poly, up, down
    try:
        # Imported here, never at module scope, so the resident daemon's
        # steady-state import graph stays scipy-free.
        from scipy.signal import resample_poly as scipy_resample_poly
    except ImportError:
        logger.error(
            "scipy missing; resampling the corpus USB mic %d->%d Hz with %d "
            "numpy polyphase branches, which may overrun the %d ms capture "
            "callback and drop corpus frames",
            usb_rate, SAMPLE_RATE, up,
            round(1000 * FRAME_SAMPLES / SAMPLE_RATE),
        )
        return resample_poly, up, down
    return scipy_resample_poly, up, down


def _usb_mic_thread(
    usb_q: Queue,
    config: BridgeConfig | None = None,
) -> None:
    """Capture optional cheap-USB-mic audio for corpus-only legs.

    Deliberately independent of the XVF mic stream so unplugging or starving
    the cheap mic cannot stall production AEC. Started only when
    JASPER_AEC_CORPUS_USB_ENABLED=1.
    """

    config = config or BridgeConfig.from_env()
    usb_rate = _usb_capture_rate(config)
    capture_block = max(1, round(FRAME_SAMPLES * usb_rate / SAMPLE_RATE))
    resample, up, down = _usb_resampler(usb_rate)
    accum_16 = np.empty(0, dtype=np.float32)

    def cb(indata, frames, time_info, status):
        nonlocal accum_16
        if status:
            logger.debug("usb mic status: %s", status)
        if _shutdown.is_set():
            return
        mono = indata[:, 0].astype(np.float32, copy=True)
        if resample is not None:
            # float32 back: dsp_numpy returns float64 and would silently
            # promote the accumulator, scipy returns the input dtype.
            mono = resample(mono, up=up, down=down).astype(
                np.float32, copy=False,
            )
        accum_16 = np.concatenate([accum_16, mono])
        while accum_16.size >= FRAME_SAMPLES:
            chunk = accum_16[:FRAME_SAMPLES]
            accum_16 = accum_16[FRAME_SAMPLES:]
            chunk = np.clip(chunk, -32768, 32767).astype(np.int16)
            try:
                usb_q.put_nowait(chunk.tobytes())
            except Full:
                _bridge_stats.inc_nested("queue_drops", "usb")
                logger.warning("usb corpus mic queue full, dropping frame")

    with sd.InputStream(
        device=config.usb_mic_device,
        samplerate=usb_rate,
        channels=1,
        dtype="int16",
        blocksize=capture_block,
        callback=cb,
    ):
        logger.info(
            "USB corpus mic capture opened: %s @ %d Hz mono -> %d Hz "
            "(block=%d)",
            config.usb_mic_device, usb_rate, SAMPLE_RATE, capture_block,
        )
        _shutdown.wait()


class _MicStarvationWatchdog:
    """Catches a *slow-drip* mic stall that the continuous-empty detector
    (`consecutive_empty_sec >= stall_restart_sec`) structurally misses.

    That detector resets to zero on a single mic frame, so an intermittent
    trickle — a frame every few seconds — keeps it oscillating below the
    threshold indefinitely even though the mic is effectively dead (well
    under 1 usable frame/s against ~12.5/s healthy).

    This watchdog measures the mic frame *rate* over rolling windows and
    flags a restart only after `max_starved_windows` *consecutive* low-rate
    windows. Conservative by design: one low window clears when the next
    recovers, a healthy or merely degraded mic never trips, and
    `max_starved_windows <= 0` disables it entirely.

    No threads and no blocking I/O: feed it `record_frame()` per consumed
    frame and `stalled(now)` once per loop iteration with a monotonic clock.
    """

    def __init__(
        self,
        *,
        window_sec: float = 10.0,
        min_frames_per_window: int = 10,   # < ~1 frame/s averaged over window
        max_starved_windows: int = 3,      # ~30 s sustained before restart
    ) -> None:
        self._window_sec = window_sec
        self._min_frames = min_frames_per_window
        self._max_starved = max_starved_windows
        self._window_start: float | None = None
        self._frames = 0
        self._starved_windows = 0

    def record_frame(self) -> None:
        """Call once per mic frame actually consumed from the queue."""
        self._frames += 1

    def stalled(self, now: float) -> bool:
        """Call every loop iteration with a monotonic timestamp. True once
        the mic frame rate has stayed below the floor for
        `max_starved_windows` consecutive windows — time to exit non-zero
        for a systemd restart."""
        if self._max_starved <= 0:
            return False
        if self._window_start is None:
            self._window_start = now
            return False
        if now - self._window_start < self._window_sec:
            return False
        if self._frames < self._min_frames:
            self._starved_windows += 1
            # Mirrors the continuous detector's "stall growing" warnings so a
            # slow-drip restart is never a surprise in the journal.
            logger.warning(
                "mic starvation: %d frames in last ~%.0fs window (floor %d) "
                "— %d/%d low-rate windows before bridge restart",
                self._frames, self._window_sec, self._min_frames,
                self._starved_windows, self._max_starved,
            )
        else:
            self._starved_windows = 0
        self._frames = 0
        self._window_start = now
        return self._starved_windows >= self._max_starved


def _add_loop_emitter(
    emitters: dict[str, LegEmitter],
    config: BridgeConfig,
    leg: str,
    port: int,
    *,
    frame_samples: int = OUT_FRAME_SAMPLES,
    emitter_cls: type[LegEmitter] = LegEmitter,
) -> LegEmitter:
    emitter = emitter_cls(
        sock=socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
        dest=(config.out_host, port),
        batch=bytearray(),
        stats_key=leg,
        stats=_bridge_stats,
        frame_samples=frame_samples,
    )
    emitter.sock.setblocking(False)
    emitters[leg] = emitter
    return emitter


def _process_optional_engine(
    engine: Any,
    input_bytes: bytes,
    ref_bytes: bytes,
    *,
    failure_message: str | None,
) -> tuple[Any | None, bytes, Exception | None]:
    """Process one optional leg and disable it after its first failure.

    The primary AEC engine deliberately does not use this helper: a primary
    failure must still escape and trigger the bridge's systemd recovery path.
    """
    try:
        return engine, engine.process(input_bytes, ref_bytes), None
    except Exception as exc:  # noqa: BLE001
        if failure_message is not None:
            logger.exception(failure_message, exc)
        return None, b"", exc


def _aec_loop(  # noqa: PLR0915
    ref_q: Queue, mic_q: Queue, engine: Optional[Aec3Engine],
    heartbeat: Optional[Heartbeat] = None,
    raw0_q: Optional[Queue] = None,
    chip_aec_qs: Optional[dict[str, Queue]] = None,
    chip_beam_plan: _mic_profile.ChipBeamPlan | None = None,
    production_chip_aec_enabled: bool = False,
    chip_aec_primary_leg: str = "chip_aec_150",
    emit_ref: bool = False,
    usb_raw_q: Optional[Queue] = None,
    xvf_raw0_webrtc_enabled: bool = False,
    xvf_raw0_dtln_enabled: bool = False,
    config: BridgeConfig | None = None,
) -> None:
    """Drain mic/ref queues, run the selected AEC path, and emit UDP legs.

    Each iteration consumes one mic frame and one reference frame in arrival
    order. An empty reference queue carries the last real reference frame
    forward rather than injecting silence, which keeps AEC3's adaptive filter
    fed through bursty reference delivery. Primary, raw, corpus, chip-AEC and
    optional-engine outputs are packetized into per-leg UDP streams.

    With `JASPER_AEC_DEBUG_RECORD_DIR` set, the engine's input mic stream,
    its pre-gain output and the reference are also written to WAV files there
    for offline ERLE analysis.
    """
    config = config or BridgeConfig.from_env()
    # Post-AEC static gain, applied to the engine output before it reaches
    # jasper-voice over UDP. Restores level into openWakeWord's training
    # distribution when the chip's mic preamp delivers a quiet AEC output;
    # 0 dB (off) by default, and soft-clipped via tanh on the way out so a
    # high gain cannot push hard-clip distortion into the wake-word input.
    global _ref_clipped_samples, _ref_total_samples
    global _out_clipped_samples, _out_total_samples
    global _ref_starved_frames
    mic_gain_db = float(os.environ.get("JASPER_AEC_MIC_GAIN_DB", "0"))
    mic_gain_lin = 10.0 ** (mic_gain_db / 20.0)
    # Stall-recovery threshold: consecutive seconds of empty mic_q before
    # bailing for a systemd-driven restart; 0 disables it. See BridgeStalled.
    stall_restart_sec = int(
        float(os.environ.get("JASPER_AEC_STALL_RESTART_SEC", "5"))
    )
    consecutive_empty_sec = 0
    # Additive slow-drip stall watchdog: the consecutive-empty check above
    # resets on a single frame, so an intermittent trickle never trips it.
    # JASPER_AEC_STALL_DRIP_MAX_WINDOWS=0 disables it.
    drip_watchdog = _MicStarvationWatchdog(
        max_starved_windows=int(
            os.environ.get("JASPER_AEC_STALL_DRIP_MAX_WINDOWS", "3")
        ),
    )
    import math
    import time
    import wave
    if production_chip_aec_enabled and (not chip_aec_qs or not chip_beam_plan):
        raise RuntimeError("chip-AEC mode requires a validated chip beam plan")
    usb_mic_choice_plan = chip_beam_plan or _mic_profile.chip_beam_plan_from_env(
        os.environ,
    )
    usb_mic_source = _resolve_usb_mic_source(
        config.usb_mic_leg,
        plan=usb_mic_choice_plan,
        production_chip_aec_enabled=production_chip_aec_enabled,
        chip_aec_primary_leg=chip_aec_primary_leg,
    )
    # UDP output: localhost, non-blocking sendto. `sendto` never blocks on
    # `lo` at this rate (~256 kbps), so the main thread can always observe
    # SIGTERM and exit inside the unit's `TimeoutStopSec=5s`.
    emitters: dict[str, LegEmitter] = {}

    def add_emitter(
        leg: str,
        port: int,
        *,
        frame_samples: int = OUT_FRAME_SAMPLES,
        emitter_cls: type[LegEmitter] = LegEmitter,
    ) -> LegEmitter:
        return _add_loop_emitter(
            emitters,
            config,
            leg,
            port,
            frame_samples=frame_samples,
            emitter_cls=emitter_cls,
        )

    on_emitter = add_emitter("on", config.out_port)
    # Dedicated non-wake consumer for the optional USB host microphone. This
    # duplicate keeps jasper-voice's frozen :9876 ownership intact: the
    # jasper-usbmic service may bind and unbind independently with no effect
    # on the primary wake/session carrier.
    usb_host_mic_emitter = (
        add_emitter(
            "usb_host_mic",
            config.out_port_usb_host_mic,
            frame_samples=FRAME_SAMPLES,
            emitter_cls=TimestampedLegEmitter,
        )
        if config.emit_usb_host_mic
        else None
    )
    # Chip-direct mic (pre-AEC3) and truly-raw mic 0, batched and packetized
    # identically to the primary AEC ON stream (see OUT_PORT_RAW /
    # OUT_PORT_RAW0). Each leg gets its own socket so a sendto failure on one
    # cannot affect the others.
    raw_emitter = add_emitter("off", config.out_port_raw)
    raw0_emitter = add_emitter("raw0", config.out_port_raw0)
    chip_aec_emitters: dict[str, LegEmitter] = {}
    if chip_aec_qs and chip_beam_plan:
        chip_aec_ports = {
            "chip_aec_150": config.out_port_chip_aec_150,
            "chip_aec_210": config.out_port_chip_aec_210,
        }
        chip_aec_enabled = {
            "chip_aec_150": config.emit_chip_aec_150,
            "chip_aec_210": config.emit_chip_aec_210,
        }
        for beam in chip_beam_plan.legs:
            if not chip_aec_enabled.get(beam.token, False):
                continue
            port = chip_aec_ports.get(beam.token, _leg_default_port(beam.token))
            chip_aec_emitters[beam.token] = add_emitter(beam.token, port)

    # Deferred: aec_bridge_corpus_lanes reads this module at import time.
    from jasper.cli.aec_bridge_corpus_lanes import build_corpus_lanes
    lanes = build_corpus_lanes(
        emitters,
        config,
        select_engine=_select_engine,
        xvf_raw0_webrtc_enabled=xvf_raw0_webrtc_enabled,
        xvf_raw0_dtln_enabled=xvf_raw0_dtln_enabled,
        emit_ref=emit_ref,
        production_chip_aec_enabled=production_chip_aec_enabled,
        usb_raw_q=usb_raw_q,
    )
    xvf_raw0_engine = lanes.xvf_raw0_engine
    xvf_raw0_webrtc_emitter = lanes.xvf_raw0_webrtc_emitter
    xvf_raw0_dtln_engine = lanes.xvf_raw0_dtln_engine
    xvf_raw0_dtln_emitter = lanes.xvf_raw0_dtln_emitter
    ref_emitter = lanes.ref_emitter
    usb_raw_emitter = lanes.usb_raw_emitter
    usb_webrtc_emitter = lanes.usb_webrtc_emitter
    usb_engine = lanes.usb_engine
    usb_dtln_engine = lanes.usb_dtln_engine
    usb_dtln_emitter = lanes.usb_dtln_emitter
    aec3_sweep_paths = lanes.aec3_sweep_paths
    emit_aec3_sweep = lanes.emit_aec3_sweep
    dtln_engine = lanes.dtln_engine
    dtln_emitter = lanes.dtln_emitter
    output_parts = [f"aec={config.out_host}:{config.out_port}"]
    if usb_host_mic_emitter is not None:
        output_parts.append(
            f"usb_host_mic={config.out_host}:{config.out_port_usb_host_mic}"
        )
        output_parts.append(
            "usb_host_mic_source="
            f"{usb_mic_source['mode']}:{usb_mic_source['leg']}"
        )
    if production_chip_aec_enabled:
        output_parts.append(f"aec_source={chip_aec_primary_leg}")
    else:
        output_parts.append(f"raw={config.out_host}:{config.out_port_raw}")
    output_parts.append(f"raw0={config.out_host}:{config.out_port_raw0}")
    if dtln_engine is not None:
        output_parts.append(f"dtln={config.out_host}:{config.out_port_dtln}")
    if chip_beam_plan:
        for beam in chip_beam_plan.legs:
            if beam.token in chip_aec_emitters:
                port = _leg_default_port(beam.token)
                output_parts.append(f"{beam.token}={config.out_host}:{port}")
    if xvf_raw0_engine is not None:
        output_parts.append(
            "xvf_raw0_webrtc_aec3="
            f"{config.out_host}:{config.out_port_xvf_raw0_webrtc_aec3}"
        )
    if xvf_raw0_dtln_engine is not None:
        output_parts.append(
            f"xvf_raw0_dtln={config.out_host}:{config.out_port_xvf_raw0_dtln}"
        )
    if emit_ref:
        output_parts.append(f"ref={config.out_host}:{config.out_port_ref}")
    if usb_raw_q is not None:
        output_parts.append(
            f"usb_raw={config.out_host}:{config.out_port_usb_raw}"
        )
        output_parts.append(
            f"usb_webrtc={config.out_host}:{config.out_port_usb_webrtc}"
        )
    if usb_dtln_engine is not None:
        output_parts.append(
            f"usb_dtln={config.out_host}:{config.out_port_usb_dtln}"
        )
    for path in aec3_sweep_paths:
        output_parts.append(
            f"{path.variant.leg}="
            f"{config.out_host}:{config.out_port_aec3_sweep[path.variant.leg]}"
        )
    _bridge_stats.set_active_capture_plan(
        wake_corpus_plan_id=config.wake_corpus_plan_id,
        expected_legs=config.wake_corpus_expected_legs,
        emitted_legs=sorted(emitters.keys()),
        corpus_flags={
            "ref": emit_ref,
            "usb": usb_raw_q is not None,
            "usb_dtln": usb_dtln_engine is not None,
            "chip_aec": bool(chip_aec_emitters),
            "aec3_sweep": bool(aec3_sweep_paths),
            "xvf_raw0_webrtc_aec3": xvf_raw0_webrtc_emitter is not None,
            "xvf_raw0_dtln": xvf_raw0_dtln_emitter is not None,
            "production_chip_aec": production_chip_aec_enabled,
        },
        beam_plan={
            "plan_id": chip_beam_plan.plan_id if chip_beam_plan else "",
            "primary_leg": chip_aec_primary_leg,
            "emitted_chip_legs": sorted(chip_aec_emitters.keys()),
        },
        ports={leg: int(emitter.dest[1]) for leg, emitter in emitters.items()},
        mic_reference_identity={
            "mic_device": config.mic_device,
            "mic_channels": MIC_CHANNELS,
            "mic_channel_index": MIC_CHANNEL_INDEX,
            "ref_source": config.ref_source,
            "outputd_ref_udp": (
                f"{config.outputd_ref_udp_host}:{config.outputd_ref_udp_port}"
            ),
            "usb_mic_device": config.usb_mic_device,
            "aec3_sweep_input_source": config.aec3_sweep_input_source,
        },
        usb_mic_source=usb_mic_source,
        mic_fingerprint=config.wake_corpus_mic_fingerprint,
        dac_reference_fingerprint=config.wake_corpus_dac_fingerprint,
    )
    logger.info(
        "udp outputs: %s frame=%d samples (%d bytes)",
        " ".join(output_parts), OUT_FRAME_SAMPLES, OUT_FRAME_BYTES,
    )
    # Voice/wake LegEmitters aggregate four 320-sample frames into one
    # 1280-sample UDP packet, holding UdpMicCapture's wire contract. The
    # dedicated USB host-mic consumer emits each 320-sample frame
    # immediately: it is latency-sensitive and has no voice consumer.
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    # Cold-start value for ref carry-forward, used only until the first real
    # ref frame arrives; after that `last_ref_bytes` always holds a
    # previously-real reference.
    last_ref_bytes = silence
    frames_processed = 0
    chip_primary_missing_log = _DropLogDebouncer()
    usb_mic_leg_missing_log = _DropLogDebouncer()
    usb_mic_raw0_missing_log = _DropLogDebouncer()
    usb_mic_effective_leg = str(usb_mic_source["leg"])
    usb_mic_fallback_active = bool(usb_mic_source["fallback_active"])

    # Optional debug WAV writers — see `_aec_loop` docstring.
    debug_dir = os.environ.get("JASPER_AEC_DEBUG_RECORD_DIR", "").strip()
    mic_wav: Optional[wave.Wave_write] = None
    aec_wav: Optional[wave.Wave_write] = None
    ref_wav: Optional[wave.Wave_write] = None
    if debug_dir:
        try:
            os.makedirs(debug_dir, exist_ok=True)
            mic_wav = wave.open(f"{debug_dir}/mic_ch1.wav", "wb")
            mic_wav.setnchannels(1)
            mic_wav.setsampwidth(2)
            mic_wav.setframerate(SAMPLE_RATE)
            aec_wav = wave.open(f"{debug_dir}/aec_output.wav", "wb")
            aec_wav.setnchannels(1)
            aec_wav.setsampwidth(2)
            aec_wav.setframerate(SAMPLE_RATE)
            ref_wav = wave.open(f"{debug_dir}/ref.wav", "wb")
            ref_wav.setnchannels(1)
            ref_wav.setsampwidth(2)
            ref_wav.setframerate(SAMPLE_RATE)
            logger.warning(
                "DEBUG RECORD MODE: writing mic/aec/ref WAVs to %s "
                "until shutdown",
                debug_dir,
            )
        except OSError as e:
            logger.error(
                "failed to open debug record dir %s: %s; skipping",
                debug_dir, e,
            )
            mic_wav = aec_wav = ref_wav = None
    last_log = 0.0
    rms_window_frames = 0
    sum_mic_sq = 0.0
    sum_ref_sq = 0.0
    sum_aec_sq = 0.0
    # Counted separately: raw0 is drained opportunistically, so a
    # window can hold fewer raw0 frames than mic frames.
    raw0_window_frames = 0
    sum_raw0_sq = 0.0

    try:
        while not _shutdown.is_set():
            if drip_watchdog.stalled(time.monotonic()):
                raise BridgeStalled(
                    "mic frame rate collapsed to a slow drip (sustained "
                    "starvation across windows while occasional frames kept "
                    "the consecutive-empty counter below threshold) — exiting "
                    "non-zero so systemd Restart=on-failure revives a fresh "
                    "InputStream"
                )
            try:
                mic_bytes = mic_q.get(timeout=1.0)
                consecutive_empty_sec = 0
                drip_watchdog.record_frame()
            except Empty:
                consecutive_empty_sec += 1
                # Log once at stall onset, then every 2 s so the journal
                # shows the stall growing without flooding 1 line/sec.
                if consecutive_empty_sec == 1 or consecutive_empty_sec % 2 == 0:
                    logger.warning(
                        "mic queue empty for %ds — bridge stalled (will exit "
                        "non-zero at %ds for systemd restart)",
                        consecutive_empty_sec, stall_restart_sec,
                    )
                if (
                    stall_restart_sec > 0
                    and consecutive_empty_sec >= stall_restart_sec
                ):
                    raise BridgeStalled(
                        f"mic queue empty for {consecutive_empty_sec}s — "
                        "InputStream is dead (typically ALSA underrun on "
                        "XVF UAC2 capture), exiting non-zero so systemd "
                        "Restart=on-failure can spin up a fresh process"
                    )
                continue

            # Consume exactly ONE ref frame per iteration, in arrival order,
            # carrying the previous frame forward when the queue is empty.
            # The reference arrives in bursts while the mic delivers smoothly
            # at the 20 ms cadence, so a burst is consumed one frame per
            # iteration and at worst 1 frame in 3 is a 20 ms-stale
            # carry-forward — within AEC3's delay-estimator tolerance.
            #
            # Do NOT drain to the newest frame: that discards half the real
            # reference and leaves every other frame either zeroed (25 Hz
            # envelope artefact, no filter convergence) or a byte-duplicate
            # of its predecessor (50 Hz artefact, audible as buzzing).
            try:
                last_ref_bytes = ref_q.get_nowait()
            except Empty:
                _ref_starved_frames += 1
                _bridge_stats.inc("ref_starved_frames")
            ref_bytes = last_ref_bytes
            if emit_ref:
                ref_emitter.emit(ref_bytes)

            # Emit the chip-direct mic BEFORE running the AEC engine, so the
            # "AEC OFF" leg carries the same bytes AEC3 is about to receive
            # as near-end input.
            if not production_chip_aec_enabled:
                raw_emitter.emit(mic_bytes)

            # Truly-raw mic 0 (chip channel 2, no chip DSP), drained
            # independently of mic_q so a backlog on one cannot stall the
            # other. The same PortAudio callback feeds both queues, so there
            # is nominally one new raw0 frame per iteration; at most one is
            # drained and a gap is simply skipped — nothing time-aligns this
            # stream to the AEC engine.
            raw0_bytes = b""
            if raw0_q is not None:
                try:
                    raw0_bytes = raw0_q.get_nowait()
                except Empty:
                    pass
                if raw0_bytes:
                    raw0_emitter.emit(raw0_bytes)
                    if xvf_raw0_engine is not None:
                        (
                            xvf_raw0_engine,
                            xvf_raw0_clean,
                            _error,
                        ) = _process_optional_engine(
                            xvf_raw0_engine,
                            raw0_bytes,
                            ref_bytes,
                            failure_message=(
                                "XVF raw0 WebRTC process() crashed; disabling "
                                "xvf_raw0_webrtc_aec3 path: %s"
                            ),
                        )
                        if xvf_raw0_clean:
                            xvf_raw0_webrtc_emitter.emit(xvf_raw0_clean)
                    if xvf_raw0_dtln_engine is not None:
                        (
                            xvf_raw0_dtln_engine,
                            xvf_raw0_dtln_clean,
                            _error,
                        ) = _process_optional_engine(
                            xvf_raw0_dtln_engine,
                            raw0_bytes,
                            ref_bytes,
                            failure_message=(
                                "XVF raw0 DTLN process() crashed; disabling "
                                "xvf_raw0_dtln path: %s"
                            ),
                        )
                        if xvf_raw0_dtln_clean:
                            xvf_raw0_dtln_emitter.emit(xvf_raw0_dtln_clean)

            chip_frames: dict[str, bytes] = {}
            if chip_aec_qs:
                for leg, q in chip_aec_qs.items():
                    try:
                        chip_bytes = q.get_nowait()
                    except Empty:
                        continue
                    chip_frames[leg] = chip_bytes
                    if emitter := chip_aec_emitters.get(leg):
                        emitter.emit(chip_bytes)

            if production_chip_aec_enabled:
                clean = chip_frames.get(chip_aec_primary_leg, b"")
                if not clean:
                    if outcome := chip_primary_missing_log.record(time.monotonic()):
                        drops, window_sec = outcome
                        log_event(
                            logger,
                            "chip_aec_primary_missing",
                            leg=chip_aec_primary_leg,
                            action="skip_frame",
                            frames=drops,
                            window_sec=f"{window_sec:.1f}",
                            level=logging.WARNING,
                        )
                    continue
            else:
                if engine is None:
                    raise RuntimeError("AEC3 engine missing outside chip-AEC mode")
                clean = engine.process(mic_bytes, ref_bytes)
            # Pre-gain output for the RMS metric: "attenuation" must reflect
            # what the AEC accomplished, not how much the post-gain stage
            # amplified the residual.
            clean_aec_only = clean
            usb_mic_aec_only = clean_aec_only
            usb_mic_uses_clean = True
            selected_usb_leg = str(usb_mic_source["leg"])
            effective_usb_leg = selected_usb_leg
            fallback_active = bool(usb_mic_source["fallback_active"])
            if selected_usb_leg == USB_MIC_RAW_XVF_LEG:
                # raw0_bytes is the physical XVF channel-2 frame this bridge
                # already captured: reuse it directly — no parallel capture
                # stack, no voice gain, and no clean/chip fallback.
                usb_mic_aec_only = raw0_bytes
                usb_mic_uses_clean = False
                if not raw0_bytes:
                    if outcome := usb_mic_raw0_missing_log.record(
                        time.monotonic()
                    ):
                        drops, window_sec = outcome
                        log_event(
                            logger,
                            "usb_mic.raw0_missing",
                            action="skip_frame",
                            frames=drops,
                            window_sec=f"{window_sec:.1f}",
                            level=logging.WARNING,
                        )
            if (
                production_chip_aec_enabled
                and selected_usb_leg != chip_aec_primary_leg
                and selected_usb_leg != USB_MIC_RAW_XVF_LEG
            ):
                selected_usb_frame = chip_frames.get(selected_usb_leg, b"")
                if selected_usb_frame:
                    usb_mic_aec_only = selected_usb_frame
                    usb_mic_uses_clean = False
                else:
                    effective_usb_leg = chip_aec_primary_leg
                    fallback_active = True
                    if outcome := usb_mic_leg_missing_log.record(time.monotonic()):
                        drops, window_sec = outcome
                        log_event(
                            logger,
                            "usb_mic.leg_missing",
                            leg=selected_usb_leg,
                            fallback=chip_aec_primary_leg,
                            frames=drops,
                            window_sec=f"{window_sec:.1f}",
                            level=logging.WARNING,
                        )
            if fallback_active:
                _bridge_stats.inc("usb_mic_source_fallback_frames")
            if (
                effective_usb_leg != usb_mic_effective_leg
                or fallback_active != usb_mic_fallback_active
            ):
                _bridge_stats.set_usb_mic_effective_source(
                    mode=str(usb_mic_source["mode"]),
                    leg=effective_usb_leg,
                    fallback_active=fallback_active,
                )
                usb_mic_effective_leg = effective_usb_leg
                usb_mic_fallback_active = fallback_active

            # Optional DTLN-aec leg, run AFTER engine.process so the wake
            # loop's primary mic stream keeps its normal critical path: the
            # extra ~1.5 ms of DTLN inference per frame spends the slack in
            # the 20 ms frame budget.
            if dtln_engine is not None:
                failed_dtln_engine = dtln_engine
                dtln_engine, dtln_clean, dtln_error = _process_optional_engine(
                    dtln_engine,
                    mic_bytes,
                    ref_bytes,
                    failure_message=None,
                )
                if dtln_error is not None:
                    # DTLN is observational: preserve the primary AEC3 path
                    # and make this transition authoritative for the stats
                    # writer and doctor. Nulling the engine keeps it to one
                    # event rather than one warning per audio frame.
                    with suppress(Exception):
                        failed_dtln_engine.close()
                    failed_dtln_emitter = emitters.pop("dtln", None)
                    if failed_dtln_emitter is not None:
                        with suppress(Exception):
                            failed_dtln_emitter.close()
                    dtln_emitter = None
                    _bridge_stats.mark_leg_unavailable(
                        "dtln", error=str(dtln_error)
                    )
                    log_event(
                        logger,
                        "aec_bridge.leg_degraded",
                        leg="dtln",
                        phase="process",
                        action="disable",
                        error_type=type(dtln_error).__name__,
                        error=str(dtln_error),
                        level=logging.WARNING,
                        exc_info=(
                            type(dtln_error),
                            dtln_error,
                            dtln_error.__traceback__,
                        ),
                    )
                if dtln_clean:
                    dtln_emitter.emit(dtln_clean)

            if config.aec3_sweep_input_source == AEC3_SWEEP_SOURCE_XVF:
                emit_aec3_sweep(mic_bytes, ref_bytes)

            if usb_raw_q is not None:
                try:
                    usb_bytes = usb_raw_q.get_nowait()
                except Empty:
                    usb_bytes = b""
                if usb_bytes:
                    usb_raw_emitter.emit(usb_bytes)

                    if usb_engine is not None:
                        usb_engine, usb_clean, _error = _process_optional_engine(
                            usb_engine,
                            usb_bytes,
                            ref_bytes,
                            failure_message=(
                                "USB WebRTC process() crashed; disabling "
                                "usb_webrtc path: %s"
                            ),
                        )
                        if usb_clean:
                            usb_webrtc_emitter.emit(usb_clean)

                    if usb_dtln_engine is not None:
                        (
                            usb_dtln_engine,
                            usb_dtln_clean,
                            _error,
                        ) = _process_optional_engine(
                            usb_dtln_engine,
                            usb_bytes,
                            ref_bytes,
                            failure_message=(
                                "USB DTLN process() crashed; disabling "
                                "usb_dtln path: %s"
                            ),
                        )
                        if usb_dtln_clean:
                            usb_dtln_emitter.emit(usb_dtln_clean)
                    if config.aec3_sweep_input_source == AEC3_SWEEP_SOURCE_USB:
                        emit_aec3_sweep(usb_bytes, ref_bytes)

            # Written here, sample-aligned, so the WAVs hold exactly what the
            # bridge measured for its "attenuation" log and what the AEC
            # emitted before the post-gain stage.
            if mic_wav is not None:
                try:
                    mic_wav.writeframes(mic_bytes)
                    aec_wav.writeframes(clean_aec_only)
                    ref_wav.writeframes(ref_bytes)
                except OSError as e:
                    logger.error("debug wav write failed: %s", e)
                    mic_wav = aec_wav = ref_wav = None
            clean, clipped_samples, total_samples = _apply_mic_output_gain(
                clean_aec_only,
                mic_gain_lin,
            )
            _out_clipped_samples += clipped_samples
            _out_total_samples += total_samples
            on_emitter.emit(clean)
            if usb_host_mic_emitter is not None:
                usb_mic_clean = clean
                if selected_usb_leg == USB_MIC_RAW_XVF_LEG:
                    # Missing raw frames remain missing: an explicit lab
                    # source must never silently become production-clean
                    # audio, not even for one USB-export frame.
                    usb_mic_clean = usb_mic_aec_only
                elif not usb_mic_uses_clean:
                    # USB beam selection is downstream-only but keeps the same
                    # output-level contract as the primary clean leg. Its
                    # clipping does not belong in voice's out_clip metric.
                    usb_mic_clean, _clipped, _total = _apply_mic_output_gain(
                        usb_mic_aec_only,
                        mic_gain_lin,
                    )
                if usb_mic_clean:
                    usb_host_mic_emitter.emit(usb_mic_clean)
            frames_processed += 1
            _bridge_stats.inc("frames_processed")
            if heartbeat is not None:
                heartbeat.bump()

            mic_arr = np.frombuffer(mic_bytes, dtype=np.int16).astype(np.float32)
            ref_arr = np.frombuffer(ref_bytes, dtype=np.int16).astype(np.float32)
            aec_arr = np.frombuffer(clean_aec_only, dtype=np.int16).astype(np.float32)
            sum_mic_sq += float(np.mean(mic_arr * mic_arr))
            sum_ref_sq += float(np.mean(ref_arr * ref_arr))
            sum_aec_sq += float(np.mean(aec_arr * aec_arr))
            rms_window_frames += 1
            if raw0_bytes:
                raw0_arr = np.frombuffer(
                    raw0_bytes, dtype=np.int16,
                ).astype(np.float32)
                sum_raw0_sq += float(np.mean(raw0_arr * raw0_arr))
                raw0_window_frames += 1

            now = time.monotonic()
            if now - last_log > 5.0:
                if rms_window_frames > 0:
                    mic_rms = math.sqrt(sum_mic_sq / rms_window_frames)
                    ref_rms = math.sqrt(sum_ref_sq / rms_window_frames)
                    aec_rms = math.sqrt(sum_aec_sq / rms_window_frames)
                    # Omitted, not zeroed, when the window drained no raw0
                    # frames: doctor reads a present `raw0` as the near-end
                    # level, and `raw0=0` would pin its music gate off.
                    raw0_token = (
                        " raw0=%.0f"
                        % math.sqrt(sum_raw0_sq / raw0_window_frames)
                        if raw0_window_frames else ""
                    )
                    if mic_rms > 1.0:
                        attn_db = 20.0 * math.log10(max(aec_rms, 1.0) / mic_rms)
                    else:
                        attn_db = 0.0
                    ref_clip_pct = (
                        100.0 * _ref_clipped_samples / _ref_total_samples
                        if _ref_total_samples else 0.0
                    )
                    out_clip_pct = (
                        100.0 * _out_clipped_samples / _out_total_samples
                        if _out_total_samples else 0.0
                    )
                    if production_chip_aec_enabled:
                        logger.info(
                            "chip_aec rms over %.1fs: ref=%.0f near=%s:%.0f "
                            "primary=%s:%.0f level_delta=%.1f dB%s "
                            "(frames=%d ref_q=%d mic_q=%d ref_starve=%d "
                            "ref_clip=%.2f%% out_clip=%.2f%%)",
                            rms_window_frames * FRAME_SAMPLES / SAMPLE_RATE,
                            ref_rms, "chip_aec_210", mic_rms,
                            chip_aec_primary_leg, aec_rms, attn_db, raw0_token,
                            frames_processed, ref_q.qsize(), mic_q.qsize(),
                            _ref_starved_frames,
                            ref_clip_pct, out_clip_pct,
                        )
                    else:
                        logger.info(
                            "rms over %.1fs: ref=%.0f mic=%.0f aec=%.0f → "
                            "attenuation=%.1f dB (frames=%d ref_q=%d mic_q=%d "
                            "ref_starve=%d ref_clip=%.2f%% out_clip=%.2f%%)",
                            rms_window_frames * FRAME_SAMPLES / SAMPLE_RATE,
                            ref_rms, mic_rms, aec_rms, attn_db,
                            frames_processed, ref_q.qsize(), mic_q.qsize(),
                            _ref_starved_frames,
                            ref_clip_pct, out_clip_pct,
                        )
                last_log = now
                rms_window_frames = 0
                sum_mic_sq = sum_ref_sq = sum_aec_sq = 0.0
                raw0_window_frames = 0
                sum_raw0_sq = 0.0
                _ref_clipped_samples = _ref_total_samples = 0
                _out_clipped_samples = _out_total_samples = 0
                _ref_starved_frames = 0
    finally:
        for emitter in emitters.values():
            emitter.close()
        if xvf_raw0_engine is not None:
            xvf_raw0_engine.close()
        if xvf_raw0_dtln_engine is not None:
            xvf_raw0_dtln_engine.close()
        if usb_engine is not None:
            usb_engine.close()
        if usb_dtln_engine is not None:
            usb_dtln_engine.close()
        for path in aec3_sweep_paths:
            with suppress(Exception):
                path.engine.close()
        for w in (mic_wav, aec_wav, ref_wav):
            if w is not None:
                try:
                    w.close()
                except OSError:
                    pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s aec-bridge %(levelname)s %(message)s",
    )
    # Log flight recorder + runtime debug toggle. See
    # jasper/flight_recorder.py.
    from .. import flight_recorder
    flight_recorder.install("aec")
    config = BridgeConfig.from_env(log_sweep=True, logger_=logger)
    # Resolve the reference source before anything reads it: the stats
    # snapshot below publishes it as the runtime provenance doctor trusts.
    try:
        config = _resolved_reference_source(config)
    except UnsupportedReferenceSource as e:
        logger.error("%s", e)
        return 1
    reference_endpoint = (
        f"{config.outputd_ref_udp_host}:{config.outputd_ref_udp_port}"
    )
    _bridge_stats.reset(
        config.aec3_sweep_variants,
        reference_source=config.ref_source,
        reference_endpoint=reference_endpoint,
    )
    _bridge_stats.write_snapshot(config.bridge_stats_path)
    corpus_ref_enabled = _env_bool("JASPER_AEC_CORPUS_REF_ENABLED", "0")
    corpus_usb_enabled = _env_bool("JASPER_AEC_CORPUS_USB_ENABLED", "0")
    corpus_usb_dtln_enabled = _env_bool(
        "JASPER_AEC_CORPUS_USB_DTLN_ENABLED", "0",
    )
    corpus_aec3_sweep_enabled = _env_bool(AEC3_SWEEP_ENV_FLAG, "0")
    corpus_chip_aec_enabled = _env_bool(
        "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "0",
    )
    production_chip_aec_enabled = _env_bool("JASPER_AEC_CHIP_AEC_ENABLED", "0")
    chip_aec_enabled = corpus_chip_aec_enabled or production_chip_aec_enabled
    chip_beam_plan = _chip_beam_plan() if chip_aec_enabled else None
    if chip_aec_enabled and chip_beam_plan is None:
        logger.error(
            "chip-AEC requested but no validated chip beam plan is active "
            "(variant=%s geometry=%s)",
            os.environ.get("JASPER_XVF_VARIANT", "unknown"),
            os.environ.get("JASPER_XVF_GEOMETRY", "unknown"),
        )
        return 1
    chip_aec_primary_leg = _chip_aec_primary_leg(chip_beam_plan)
    corpus_xvf_raw0_webrtc_enabled = _env_bool(
        "JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED", "0",
    )
    corpus_xvf_raw0_dtln_enabled = _env_bool(
        "JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED", "0",
    )
    raw_out_detail = (
        "disabled-chip-aec-mode"
        if production_chip_aec_enabled
        else f"udp://{config.out_host}:{config.out_port_raw}"
    )
    logger.info(
        "starting: ref=%s@%d mic=%s@%d ch=%d->ch%d "
        "aec_out=udp://%s:%d raw_out=%s @%d "
        "corpus_ref=%s corpus_usb=%s corpus_usb_dtln=%s "
        "corpus_aec3_sweep=%s corpus_aec3_sweep_source=%s "
        "corpus_chip_aec=%s production_chip_aec=%s "
        "chip_beam_plan=%s chip_aec_primary=%s corpus_xvf_raw0_webrtc=%s "
        "corpus_xvf_raw0_dtln=%s",
        f"udp:{config.outputd_ref_udp_port}",
        REF_RATE, config.mic_device, SAMPLE_RATE,
        MIC_CHANNELS, MIC_CHANNEL_INDEX,
        config.out_host, config.out_port, raw_out_detail, OUT_RATE,
        "on" if corpus_ref_enabled else "off",
        "on" if corpus_usb_enabled else "off",
        "on" if corpus_usb_dtln_enabled else "off",
        "on" if corpus_aec3_sweep_enabled else "off",
        config.aec3_sweep_input_source,
        "on" if corpus_chip_aec_enabled else "off",
        "on" if production_chip_aec_enabled else "off",
        chip_beam_plan.plan_id if chip_beam_plan else "none",
        chip_aec_primary_leg,
        "on" if corpus_xvf_raw0_webrtc_enabled else "off",
        "on" if corpus_xvf_raw0_dtln_enabled else "off",
    )
    if production_chip_aec_enabled and not os.environ.get(
        "JASPER_OUTPUTD_CHIP_REF_PCM", ""
    ).strip():
        logger.error(
            "JASPER_AEC_CHIP_AEC_ENABLED=1 requires "
            "JASPER_OUTPUTD_CHIP_REF_PCM so outputd feeds XVF USB-IN",
        )
        return 1
    if corpus_usb_dtln_enabled and not corpus_usb_enabled:
        logger.warning(
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1 is ignored unless "
            "JASPER_AEC_CORPUS_USB_ENABLED=1 also starts the USB mic capture",
        )
    if (
        corpus_aec3_sweep_enabled
        and config.aec3_sweep_input_source == AEC3_SWEEP_SOURCE_USB
        and not corpus_usb_enabled
    ):
        logger.warning(
            "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb is ignored unless "
            "JASPER_AEC_CORPUS_USB_ENABLED=1 also starts the USB mic capture",
        )

    try:
        _validate_mic_device(config)
    except MicDeviceUnavailable as e:
        logger.error("%s", e)
        return 1
    if corpus_usb_enabled:
        try:
            _validate_usb_mic_device(config)
        except UsbMicUnavailable as e:
            logger.error("%s", e)
            return 1

    engine = None if production_chip_aec_enabled else _select_engine()

    def on_signal(signum, _frame):
        logger.info("received signal %d, shutting down", signum)
        _shutdown.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    ref_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)
    mic_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)
    # Filled by the same mic callback that fills mic_q, drained
    # independently by the AEC loop for the OUT_PORT_RAW0 leg.
    raw0_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)
    chip_aec_qs: dict[str, Queue[bytes]] | None = (
        {beam.token: Queue(maxsize=QUEUE_MAXSIZE) for beam in chip_beam_plan.legs}
        if chip_aec_enabled else None
    )
    usb_q: Queue[bytes] | None = (
        Queue(maxsize=QUEUE_MAXSIZE) if corpus_usb_enabled else None
    )

    ref_t = threading.Thread(
        target=_outputd_ref_udp_thread,
        args=(ref_q, config),
        daemon=True,
    )
    mic_t = threading.Thread(
        target=_mic_thread,
        args=(mic_q, raw0_q, chip_aec_qs, chip_beam_plan, config),
        daemon=True,
    )
    usb_t = (
        threading.Thread(
            target=_usb_mic_thread,
            args=(usb_q, config),
            daemon=True,
        )
        if usb_q is not None else None
    )
    ref_t.start()
    mic_t.start()
    if usb_t is not None:
        usb_t.start()
    stats_t = threading.Thread(
        target=_bridge_stats_writer,
        args=(config.bridge_stats_path,),
        name="aec-bridge-stats",
        daemon=True,
    )
    stats_t.start()

    # Tier 1 of the resilience ladder, bumped after each successful frame in
    # `_aec_loop`: if the loop wedges (e.g. the mic InputStream stops
    # invoking its callback after a USB underrun on the XVF UAC2 capture),
    # the daemon stops patting, the unit's `WatchdogSec=` expires, and
    # systemd revives it. See jasper/watchdog.py.
    heartbeat = Heartbeat(stale_threshold_sec=5.0, interval_sec=10.0)
    heartbeat.start()

    try:
        _aec_loop(
            ref_q,
            mic_q,
            engine,
            heartbeat=heartbeat,
            raw0_q=raw0_q,
            chip_aec_qs=chip_aec_qs,
            chip_beam_plan=chip_beam_plan,
            production_chip_aec_enabled=production_chip_aec_enabled,
            chip_aec_primary_leg=chip_aec_primary_leg,
            emit_ref=corpus_ref_enabled,
            usb_raw_q=usb_q,
            xvf_raw0_webrtc_enabled=corpus_xvf_raw0_webrtc_enabled,
            xvf_raw0_dtln_enabled=corpus_xvf_raw0_dtln_enabled,
            config=config,
        )
    except BridgeStalled as e:
        logger.error("%s", e)
        _shutdown.set()
        return 1
    except Exception as e:  # noqa: BLE001
        logger.exception("aec loop crashed: %s", e)
        _shutdown.set()
        return 1
    finally:
        heartbeat.stop()
        if engine is not None:
            engine.close()
        _bridge_stats.write_snapshot(config.bridge_stats_path)
        ref_t.join(timeout=2)
        mic_t.join(timeout=2)
        if usb_t is not None:
            usb_t.join(timeout=2)
        stats_t.join(timeout=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
