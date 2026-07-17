# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AEC bridge — `jasper-aec-bridge` (Python).

REPLACES the CamillaDSP-based aec-bridge. In software fallback mode this
process runs WebRTC AEC3. In chip-AEC production mode
(`JASPER_AEC_CHIP_AEC_ENABLED=1`) it does not instantiate the WebRTC AEC3
engine; it routes outputd's final speaker buffer into the XVF3800 USB-IN
reference, captures the chip's 150°/210° ASR beams, forwards the selected
primary beam on :9876, and emits optional extra beams on :9887/:9888 only
when the reconciler publishes those runtime device env vars. The wake-corpus
recorder uses the same chip profile under its corpus-only flag so the labeled
comparison data and production mode stay aligned.

In default mode this bridge does the AEC in software, with the
recommended XVF capture channel as near-end and the host-side music
chain as far-end. With `SHF_BYPASS=1` in jasper-aec-init, channels
0/1 are raw-ish chip feeds rather than beamformed/NS/AGC outputs.
The engine is WebRTC AEC3 via the `jasper_aec3` pybind11 binding around
Trixie's `libwebrtc-audio-processing-dev` (v1.3-3 — which IS
AEC3; the 1.x is package-API stability versioning, not algorithm
version). AEC3 includes a frequency-domain residual echo
suppressor + drift-tolerant delay estimator and runs at ~3-8% of
one Pi 5 core. See docs/HANDOFF-aec.md for the full investigation.

JTS previously read raw mic 0 (channel 2) but switched to channel
1 on 2026-05-15 after confirming via XMOS primary docs that
channels 2-5 bypass every chip DSP stage (no BF, NS, AGC, HPF,
not even MIC_GAIN). The canonical XVF3800 voice-assistant capture
is channel 0/1 — see HANDOFF-xvf3800.md §3.

Topology:

    outputd UDP final-speaker monitor (48k stereo speaker reference)
       or explicit fallback pcm.jasper_capture (48k stereo, host clock)
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
       │  MicCapture frame size; optional USB host mic             │  same packet shape
       │  consumer uses one 320-sample / 20 ms frame)             │
       ▼                                                          ▼
    jasper-voice's UdpMicCapture (binds 9876)             jasper-voice's second
                                                          UdpMicCapture (binds 9877)
                                                          for dual-stream wake-word
                                                          detection (PR 2 of the
                                                          wake-telemetry series —
                                                          see docs/HANDOFF-wake-
                                                          telemetry.md).

Why UDP instead of the previous snd-aloop `LoopbackAEC` card: see
the `UdpMicCapture` docstring in jasper/audio_io.py. Short version:
snd-aloop's `loopback_cable` kernel struct wedges when a consumer
is SIGKILL'd, requiring a reboot to clear. UDP has no kernel-side
state and `sendto()` is non-blocking, which orthogonally fixes the
PortAudio-write SIGTERM-observability bug from the 2026-05-11
incident. Validated end-to-end in PR 2 of the resilience-ladder
series.

Caveats this implementation does NOT yet address:
  - Reference and mic are on independent clock domains (kernel
    timer vs XVF chip's USB UAC2 SYNC). They WILL drift over
    time — AEC3's delay estimator tolerates some drift but not
    unbounded. Future fix: drift-compensate via resampling, or
    expose a USB-side reference channel from the chip (would
    require custom firmware).
  - Frame alignment between two PortAudio streams isn't perfectly
    synchronized — the ref-mic offset can vary by a few ms each
    restart. AEC3 auto-adapts (the `stream_delay_ms` hint is just
    a starting point for its delay estimator) but convergence
    takes longer when the offset drifts.
  - The engine is the linear AEC3 + residual suppressor only; no
    neural residual stage. See docs/HANDOFF-aec.md "Deep tuning
    landscape" for the staged options if AEC3 + REF_GAIN + MIC_GAIN
    isn't enough.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
import logging
import os
import socket
import signal
import struct
import sys
import threading
import time
from queue import Queue, Empty, Full
from pathlib import Path
from typing import Optional
import json

import numpy as np
import sounddevice as sd
from scipy.signal import butter, resample_poly, sosfilt

from jasper.aec_sweep import (
    AEC3_SWEEP_ENV_FLAG,
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    DEFAULT_AEC3_SWEEP_VARIANTS,
    Aec3SweepConfig,
    Aec3SweepConfigError,
    Aec3SweepVariant,
    USB_AEC3_CORPUS_LABEL,
    USB_AEC3_CORPUS_OVERRIDES,
    USB_AEC3_SWEEP_BASELINE_LABEL,
    USB_AEC3_SWEEP_BASELINE_OVERRIDES,
    current_aec3_sweep_source,
    load_aec3_sweep_config,
)
from jasper.watchdog import Heartbeat
from jasper import wake_legs
from jasper.wake_corpus.capture_plan import (
    DAC_FINGERPRINT_ENV,
    EXPECTED_LEGS_ENV,
    MIC_FINGERPRINT_ENV,
    PLAN_ID_ENV,
)
from jasper.log_event import log_event
from jasper.usb_mic import (
    INTENT_PATH as USB_MIC_INTENT_PATH,
    USB_HOST_MIC_UDP_PORT,
    USB_MIC_HEADER_STRUCT,
    USB_MIC_PACKET_MAGIC,
    USB_MIC_PACKET_VERSION,
    usb_mic_enabled,
)
from ..mics import xvf3800 as _mic_profile

logger = logging.getLogger("jasper.aec_bridge")
AEC3_SWEEP_VARIANTS = DEFAULT_AEC3_SWEEP_VARIANTS
AEC3_SWEEP_INPUT_SOURCE = AEC3_SWEEP_SOURCE_XVF

# Frame size: 320 samples @ 16 kHz = 20 ms, a multiple of WebRTC
# AEC3's 10 ms frame requirement (160 samples). The binding splits
# 320 → 2×160 internally per the AEC3 API contract. AEC3 manages
# its own filter length internally.
FRAME_SAMPLES = 320
SAMPLE_RATE = 16000

# Fallback capture device for the reference (host-clocked dsnoop on
# the fan-in loopback). Production reconcile sets JASPER_AEC_REF_SOURCE
# to outputd_udp so software AEC consumes outputd's final speaker
# monitor. `jasper_ref` remains available for explicit diagnostics and
# rollback because it is a plug-wrapped alias of `jasper_capture`
# defined in /etc/asound.conf.
REF_DEVICE = "jasper_ref"
REF_RATE = 48000  # what we ask plug for; plug resamples slave to this
REF_CHANNELS = 2

# Capture device for the mic. Chip's 6-ch firmware exposes
# channels 0/1 are the chip's processed-output lanes when SHF_BYPASS=0
# and raw-ish lanes when SHF_BYPASS=1 (the default production state);
# channels 2-5 are raw mics 0-3 (no chip processing of any kind). The
# mic profile pins MIC_CHANNEL_INDEX=1 for the default WebRTC AEC3 path.
# Device names are PortAudio substring matches (sounddevice's
# backend) — NOT ALSA pcm strings. PortAudio enumerates ALSA
# cards by their card description, not by hw:CARD= syntax.
# Default matches "Array: USB Audio (hw:N,0)" on the legacy square
# firmware and "L16K6Ch: USB Audio (hw:N,0)" on the Flex linear firmware.
MIC_DEVICE = _mic_profile.alsa_card_name()
MIC_CHANNELS = _mic_profile.RECOMMENDED_FIRMWARE.capture_channels
MIC_CHANNEL_INDEX = _mic_profile.MIC_CHANNEL_INDEX

# Output transport: UDP localhost. Bridge sends AEC'd mono int16
# frames to `127.0.0.1:JASPER_AEC_UDP_PORT`; jasper-voice's
# `UdpMicCapture` binds the same port and receives.
#
# Why UDP instead of the old snd-aloop `LoopbackAEC` card: see the
# `UdpMicCapture` docstring in jasper/audio_io.py. Short version:
# snd-aloop's `loopback_cable` kernel struct wedges if a consumer
# is SIGKILL'd, requiring `rmmod && modprobe` (with every consumer
# stopped first) or a reboot to recover. UDP has no kernel-side
# state to corrupt and `sendto()` is non-blocking, which
# orthogonally fixes the daemon's SIGTERM-observability bug from
# the 2026-05-11 incident.
OUT_HOST = "127.0.0.1"


def _leg_default_port(token: str) -> int:
    return wake_legs.by_token(token).udp_port


OUT_PORT = _leg_default_port("on")
OUT_RATE = 16000

# Secondary UDP output: chip-direct mic stream, pre-AEC3 — exactly
# the same near-end input AEC3 consumes in default production
# (chip ch 1, raw-ish when SHF_BYPASS=1). Emitted on a separate port
# so jasper-voice's wake
# loop (PR 2 of the wake-telemetry series) can score wake-word
# detection on BOTH the post-AEC stream (OUT_PORT) and the chip-
# direct stream (OUT_PORT_RAW). Same 1280-sample / 16 kHz mono
# int16 packet shape as the primary stream so the consumer sees
# identical chunk sizes.
#
# Why expose this: the 2026-05-20 wake-rate sweep showed the AEC ON
# and AEC OFF legs catch mostly-disjoint sets of utterances —
# test-1 yielded a 40 % union vs 25 % best single leg (see
# HANDOFF-aec.md "Open work streams — option C"). Emitting both
# lets the wake loop OR the detections without changing the AEC
# pipeline. See docs/HANDOFF-wake-telemetry.md for the end-to-end
# design.
#
# Jasper-voice consumes this leg when the reconciler configures
# `JASPER_MIC_DEVICE_RAW`; otherwise the extra UDP packets are ignored.
OUT_PORT_RAW = _leg_default_port("off")
# Optional 3rd UDP stream: DTLN-aec output. The bridge constructs a
# DTLNEngine when JASPER_AEC_DTLN_ENABLED=1 and shares the same mic +
# ref capture with the AEC3 engine. Each input chunk is fed to BOTH
# engines; AEC3 output goes to OUT_PORT, DTLN output to OUT_PORT_DTLN.
# Adds ~95 MB RAM + ~12% of one Pi 5 core. Disabled by default during
# the triple-stream rollout; flip via env var per
# docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture plan".
OUT_PORT_DTLN = _leg_default_port("dtln")
# 4th UDP stream: truly-raw mic 0 (chip channel 2). Unlike the
# chip-direct stream on OUT_PORT_RAW (which is chip channel 1 = ASR
# beam, with chip BF+NS+AGC+HPF applied), channel 2 is the raw mic 0
# ADC output with NO chip DSP whatsoever — not even MIC_GAIN. It's
# what a cheap USB mic without an XMOS chip would deliver.
#
# Used by the wake-corpus recorder so we can build training data that's
# mic-agnostic — useful if we ever swap in cheaper mic hardware, and a
# useful baseline for understanding how much of wake performance comes
# from the chip's DSP vs the wake model itself. Same 1280-sample /
# 16 kHz mono int16 packet shape as the other legs.
#
# Always emitted. Cost is ~0.25% of one core for the extra slice +
# sendto — same noise-floor cost as the existing :9877 raw leg.
OUT_PORT_RAW0 = _leg_default_port("raw0")
# Corpus-only experiment streams. These are disabled by default so
# normal production bridge cost stays exactly where it is. When enabled
# for wake-corpus recording, the bridge emits:
#   - ref: the 16 kHz mono reference frame AEC3 actually consumed
#   - usb_raw: a cheap USB mic's raw mono capture
#   - usb_webrtc: that same USB mic through a second WebRTC AEC3 chain
#   - usb_dtln: the cheap USB mic through a second DTLN-aec chain
#
# They are intentionally not consumed by jasper-voice. They exist to
# make the gold corpus useful for cheap-mic portability experiments.
OUT_PORT_REF = _leg_default_port("ref")
OUT_PORT_USB_RAW = _leg_default_port("usb_raw")
OUT_PORT_USB_WEBRTC = _leg_default_port("usb_webrtc")
OUT_PORT_USB_DTLN = _leg_default_port("usb_dtln")
OUT_PORT_CHIP_AEC_150 = _leg_default_port("chip_aec_150")
OUT_PORT_CHIP_AEC_210 = _leg_default_port("chip_aec_210")
OUT_PORT_XVF_RAW0_WEBRTC_AEC3 = _leg_default_port("xvf_raw0_webrtc_aec3")
OUT_PORT_XVF_RAW0_DTLN = _leg_default_port("xvf_raw0_dtln")
OUTPUTD_REF_UDP_HOST = "127.0.0.1"
OUTPUTD_REF_UDP_PORT = 9891
REF_SOURCE = "outputd_udp"
OUT_PORT_AEC3_SWEEP = {
    variant.leg: variant.default_port
    for variant in AEC3_SWEEP_VARIANTS
}
USB_MIC_DEVICE = "USB PnP Sound Device"
USB_MIC_RATE = 0
# Voice consumes 1280-sample (80 ms) chunks. Aggregating four
# 320-sample AEC frames into one UDP packet keeps the
# bridge↔voice contract symmetric with the existing MicCapture
# frame size and halves packet rate to ~12.5 pps. The AEC engine
# still works on 320-sample windows internally.
OUT_FRAME_SAMPLES = 1280
OUT_FRAME_BYTES = OUT_FRAME_SAMPLES * 2  # int16
BRIDGE_STATS_PATH = Path("/run/jasper/aec_bridge_stats.json")
BRIDGE_STATS_SCHEMA_VERSION = 2

# Drop-frame threshold. If queues fill faster than they drain,
# something's wrong (CPU starvation, clock drift exceeded our
# margin). We log and drop rather than block.
QUEUE_MAXSIZE = 32

_shutdown = threading.Event()


@dataclass(frozen=True)
class BridgeConfig:
    mic_device: str
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

        return cls(
            mic_device=os.environ.get(
                "JASPER_AEC_MIC_DEVICE",
                _mic_profile.alsa_card_name(),
            ),
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
                variant.leg: int(
                    os.environ.get(variant.port_env, str(variant.default_port))
                )
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
        self.drops_in_window += 1
        if self.last_log and now - self.last_log < self.interval_sec:
            return None
        window_sec = now - self.last_log if self.last_log else self.interval_sec
        drops = self.drops_in_window
        self.drops_in_window = 0
        self.last_log = now
        return drops, window_sec


def _zero_leg_counters(
    aec3_sweep_variants: tuple[Aec3SweepVariant, ...] = AEC3_SWEEP_VARIANTS,
) -> dict[str, int]:
    """A fresh per-leg counter dict zeroed for every emit leg: each
    jasper.wake_legs token plus the dynamic AEC3-sweep variant legs.
    Keyed off the registry so the bridge's UDP emit tokens and the
    wake-event corpus columns stay in lockstep — adding a leg to
    jasper.wake_legs surfaces it here automatically.
    """
    counters = {spec.token: 0 for spec in wake_legs.REGISTRY}
    counters.update({variant.leg: 0 for variant in aec3_sweep_variants})
    return counters


class _BridgeStats:
    """Low-cost monotonic counters for capture provenance.

    The wake-corpus recorder snapshots this JSON file at clip
    start/stop and stores counter deltas in clip metadata. Counters are
    intentionally monotonic for the lifetime of one bridge process;
    the PID + start epoch let consumers reject deltas across restarts.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_epoch_sec = time.time()
        self._counters: dict[str, object] = {}
        self.reset()

    def reset(
        self,
        aec3_sweep_variants: tuple[Aec3SweepVariant, ...] = AEC3_SWEEP_VARIANTS,
    ) -> None:
        with self._lock:
            self._started_epoch_sec = time.time()
            self._leg_engines = {}
            self._active_capture_plan: dict[str, object] = {}
            self._counters = {
                "frames_processed": 0,
                "ref_starved_frames": 0,
                "queue_drops": {
                    "mic": 0,
                    "chip": 0,
                    "raw0": 0,
                    "usb": 0,
                    "ref": 0,
                },
                "udp_send_drops_by_leg": _zero_leg_counters(aec3_sweep_variants),
                "packets_sent_by_leg": _zero_leg_counters(aec3_sweep_variants),
            }

    def set_leg_engine(
        self,
        leg: str,
        *,
        enabled: bool,
        loaded: bool,
        error: str | None = None,
    ) -> None:
        """Record an optional engine leg's current runtime availability.

        Gives /run/jasper/aec_bridge_stats.json an authoritative,
        journal-independent answer to "is the DTLN leg actually running?"
        across both initialization and later inference failures.
        jasper-doctor's check_aec_bridge_dtln_engine reads this first and
        only falls back to journal parsing on older bridges."""
        with self._lock:
            self._leg_engines[leg] = {
                "enabled": enabled,
                "loaded": loaded,
                "error": error,
            }

    def set_active_capture_plan(
        self,
        *,
        wake_corpus_plan_id: str,
        expected_legs: tuple[str, ...],
        emitted_legs: list[str],
        corpus_flags: dict[str, object],
        beam_plan: dict[str, object],
        ports: dict[str, int],
        mic_reference_identity: dict[str, object],
        mic_fingerprint: str = "",
        dac_reference_fingerprint: str = "",
    ) -> None:
        with self._lock:
            self._active_capture_plan = {
                "wake_corpus_plan_id": wake_corpus_plan_id,
                "expected_legs": list(expected_legs),
                "emitted_legs": list(emitted_legs),
                "enabled_corpus_flags": dict(corpus_flags),
                "beam_plan": dict(beam_plan),
                "ports": dict(ports),
                "mic_reference_identity": dict(mic_reference_identity),
                "mic_fingerprint": mic_fingerprint,
                "dac_reference_fingerprint": dac_reference_fingerprint,
            }

    def mark_leg_unavailable(self, leg: str, *, error: str) -> None:
        """Atomically withdraw a failed runtime leg from live bridge truth.

        Keep ``expected_legs`` intact so capture-plan validation reports the
        promised leg as missing, while ``emitted_legs`` and ``ports`` describe
        only outputs the bridge can still feed.
        """
        with self._lock:
            self._leg_engines[leg] = {
                "enabled": True,
                "loaded": False,
                "error": error,
            }
            emitted = self._active_capture_plan.get("emitted_legs")
            if isinstance(emitted, list):
                self._active_capture_plan["emitted_legs"] = [
                    emitted_leg for emitted_leg in emitted
                    if emitted_leg != leg
                ]
            ports = self._active_capture_plan.get("ports")
            if isinstance(ports, dict):
                ports.pop(leg, None)

    def inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] = int(self._counters.get(key, 0)) + amount

    def inc_nested(self, group: str, key: str, amount: int = 1) -> None:
        with self._lock:
            values = self._counters.get(group)
            if not isinstance(values, dict):
                return
            values[key] = int(values.get(key, 0)) + amount

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = json.loads(json.dumps(self._counters))
            leg_engines = json.loads(json.dumps(self._leg_engines))
            active_capture_plan = json.loads(json.dumps(self._active_capture_plan))
            started = self._started_epoch_sec
        return {
            "schema_version": BRIDGE_STATS_SCHEMA_VERSION,
            "pid": os.getpid(),
            "started_epoch_sec": started,
            "updated_epoch_sec": time.time(),
            "sample_rate_hz": SAMPLE_RATE,
            "frame_samples": FRAME_SAMPLES,
            "out_frame_samples": OUT_FRAME_SAMPLES,
            "counters": counters,
            "leg_engines": leg_engines,
            "active_capture_plan": active_capture_plan,
            "wake_corpus_plan_id": active_capture_plan.get(
                "wake_corpus_plan_id", "",
            ),
            "emitted_legs": active_capture_plan.get("emitted_legs", []),
        }

    def write_snapshot(self, path: Path = BRIDGE_STATS_PATH) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.snapshot(), sort_keys=True))
            tmp.replace(path)
        except OSError as e:
            logger.debug("bridge stats snapshot write failed: %s", e)


_bridge_stats = _BridgeStats()


def _bridge_stats_writer(path: Path = BRIDGE_STATS_PATH) -> None:
    while not _shutdown.wait(0.5):
        _bridge_stats.write_snapshot(path)
    _bridge_stats.write_snapshot(path)


def _send_packet(
    *,
    sock: socket.socket,
    dest: tuple[str, int],
    packet: bytes,
    leg: str,
) -> None:
    """Send one non-blocking leg packet and preserve drop-newest stats."""

    try:
        sock.sendto(packet, dest)
        _bridge_stats.inc_nested("packets_sent_by_leg", leg)
    except BlockingIOError:
        _bridge_stats.inc_nested("udp_send_drops_by_leg", leg)
        logger.warning("udp %s sendto would block, dropping frame", leg)


def emit_packet(
    *,
    sock: socket.socket,
    dest: tuple[str, int],
    batch: bytearray,
    pcm: bytes,
    leg: str,
    frame_bytes: int = OUT_FRAME_BYTES,
) -> None:
    batch.extend(pcm)
    if len(batch) < frame_bytes:
        return
    _send_packet(
        sock=sock,
        dest=dest,
        packet=bytes(batch[:frame_bytes]),
        leg=leg,
    )
    del batch[:frame_bytes]


@dataclass
class LegEmitter:
    sock: socket.socket
    dest: tuple[str, int]
    batch: bytearray
    stats_key: str
    frame_samples: int = OUT_FRAME_SAMPLES
    engine_token: str | None = None

    def emit(self, pcm: bytes) -> None:
        emit_packet(
            sock=self.sock,
            dest=self.dest,
            batch=self.batch,
            pcm=pcm,
            leg=self.stats_key,
            frame_bytes=self.frame_samples * 2,
        )

    def close(self) -> None:
        self.sock.close()


@dataclass
class TimestampedLegEmitter(LegEmitter):
    """Packetize the isolated USB-host mic leg with emit-time metadata.

    ``t_capture_mono_ns`` in the wire header is deliberately a bridge-emit
    timestamp: it measures bridge emit -> relay sink, while PortAudio's input
    latency is observed separately when the capture stream opens.
    """

    _seq: int = field(default=0, init=False, repr=False)

    def emit(self, pcm: bytes) -> None:
        frame_bytes = self.frame_samples * 2
        self.batch.extend(pcm)
        if len(self.batch) < frame_bytes:
            return
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        header = struct.pack(
            USB_MIC_HEADER_STRUCT,
            USB_MIC_PACKET_MAGIC,
            USB_MIC_PACKET_VERSION,
            0,
            seq,
            time.clock_gettime_ns(time.CLOCK_MONOTONIC),
        )
        _send_packet(
            sock=self.sock,
            dest=self.dest,
            packet=header + bytes(self.batch[:frame_bytes]),
            leg=self.stats_key,
        )
        del self.batch[:frame_bytes]


class BridgeStalled(RuntimeError):
    """Mic capture has stalled — either no frames for the configured
    continuous threshold (JASPER_AEC_STALL_RESTART_SEC, default 5s) or a
    sustained sub-usable frame *rate* (the slow-drip case caught by
    `_MicStarvationWatchdog`; JASPER_AEC_STALL_DRIP_MAX_WINDOWS).

    Raised by `_aec_loop` to bail with a non-zero exit code so
    systemd's `Restart=on-failure` revives us with a fresh
    `sd.InputStream`. PortAudio's InputStream is one-shot — once
    its ALSA capture PCM enters an unrecoverable state (typically
    after a USB underrun on the XVF chip's UAC2 endpoint), the
    callback simply stops being invoked. There's no in-process
    recovery path; only a new process gets a working stream.
    Hit in production 2026-05-11: bridge silently stopped feeding
    the voice mic path for ~10 minutes, wake-word detection got no audio,
    Hey Jarvis was unresponsive with no audible cue.
    """


class MicDeviceUnavailable(RuntimeError):
    """The configured PortAudio mic device is not currently present."""


class UsbMicUnavailable(RuntimeError):
    """The configured corpus USB mic device is not currently present."""


# Clipping counters (module-level for cheap cross-thread access; small
# race conditions in increment+reset are benign — worst case a single
# log window's percentage is off by a frame). Tracked separately for
# the ref pre-clip stage (after JASPER_AEC_REF_GAIN_DB applied) and
# the post-AEC mic stage (after JASPER_AEC_MIC_GAIN_DB applied).
_ref_clipped_samples = 0
_ref_total_samples = 0
_out_clipped_samples = 0
_out_total_samples = 0

# Counter for `ref_q empty when main loop polled` events. See
# `_aec_loop` for why this happens (ALSA delivers ref in 2-period
# bursts every 40 ms, mic at smooth 20 ms cadence — half of main-loop
# polls land between bursts). Logged in the periodic RMS line so the
# rate is observable; an unusually high rate (say > 10 per 5 s window
# = > 2 Hz) indicates the timing balance has drifted and Fix A's
# stale-ref-reuse is doing heavier lifting than expected.
_ref_starved_frames = 0


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


def _cfg_value(
    name: str,
    default: str,
    overrides: dict[str, str] | None = None,
) -> str:
    if overrides is not None and name in overrides:
        return overrides[name]
    return os.environ.get(name, default)


def _cfg_bool(
    name: str,
    default: str,
    overrides: dict[str, str] | None = None,
) -> bool:
    return _cfg_value(name, default, overrides).strip().lower() in (
        "1", "true", "yes", "on",
    )


class _Aec3Engine:
    """WebRTC AEC3 via the jasper_aec3 v1.3-3 (legacy) pybind11 binding.

    Splits each FRAME_SAMPLES (20 ms) buffer into 2× 10 ms windows
    internally, calls ProcessReverseStream + ProcessStream per
    window, returns the joined AEC'd capture. Top-level
    AudioProcessing::Config knobs only (no deep EchoCanceller3Config
    access). Fallback engine when the v2 binding isn't built.
    """

    def __init__(
        self,
        overrides: dict[str, str] | None = None,
        label: str = "aec3_v1",
    ) -> None:
        from jasper_aec3 import Aec3

        ns_enabled = _cfg_bool("JASPER_AEC_NS_ENABLED", "1", overrides)
        ns_level = _cfg_value("JASPER_AEC_NS_LEVEL", "low", overrides).strip().lower()
        agc1_enabled = _cfg_bool("JASPER_AEC_AGC1_ENABLED", "0", overrides)
        agc1_target_dbfs = int(_cfg_value(
            "JASPER_AEC_AGC1_TARGET_DBFS", "9", overrides,
        ))
        agc1_max_gain_db = int(_cfg_value(
            "JASPER_AEC_AGC1_MAX_GAIN_DB", "18", overrides,
        ))
        enable_agc2 = _cfg_bool("JASPER_AEC_AGC2", "0", overrides)
        stream_delay_ms = int(_cfg_value(
            "JASPER_AEC_STREAM_DELAY_MS", "40", overrides,
        ))
        self._aec = Aec3(
            stream_delay_ms=stream_delay_ms,
            enable_agc2=enable_agc2,
            ns_enabled=ns_enabled,
            ns_level=ns_level,
            agc1_enabled=agc1_enabled,
            agc1_target_dbfs=agc1_target_dbfs,
            agc1_max_gain_db=agc1_max_gain_db,
        )
        logger.info(
            "engine=%s ns=%s/%s agc1=%s(target=%d,max=%ddB) "
            "agc2=%s stream_delay_ms=%d frame=%d rate=%d",
            label,
            "on" if ns_enabled else "off", ns_level,
            "on" if agc1_enabled else "off",
            agc1_target_dbfs, agc1_max_gain_db,
            "on" if enable_agc2 else "off", stream_delay_ms,
            FRAME_SAMPLES, SAMPLE_RATE,
        )

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return self._aec.process(mic, ref)

    def close(self) -> None:
        # The pybind11 wrapper's std::unique_ptr<AudioProcessing>
        # is freed when the Python Aec3 instance is GC'd. No
        # explicit teardown needed.
        pass


class _Aec3V2Engine:
    """WebRTC AEC3 via the jasper_aec3 v2.1 vendored-static binding.

    Exposes the deep EchoCanceller3Config knobs the v1 binding can't
    reach — required for the BEST_A canonical config from the
    2026-05-22 tuning campaign. Defaults to BEST_A; env vars override
    each knob individually.

    BEST_A config (the Aec3V2 constructor's pybind11 defaults already
    match these — no override needed for default behavior):
        filter_refined_length_blocks=30
        ep_strength_bounded_erl=False
        ep_strength_default_gain=0.3
        erle_max_l=1.5, erle_max_h=1.0
        erle_onset_detection=False
        use_stationarity_properties=True
        conservative_hf_suppression=True
        normal_mask_hf_enr_transparent=0.3
        normal_mask_hf_enr_suppress=0.4
        normal_mask_hf_emr_transparent=0.3
        normal_max_dec_factor_lf=0.05
        nearend_average_blocks=4
        nearend_mask_hf_enr_transparent=0.1
        nearend_mask_hf_enr_suppress=0.3
        nearend_mask_hf_emr_transparent=0.3
        nearend_max_dec_factor_lf=0.25
        nearend_max_inc_factor=2.0
        dominant_nearend_snr_threshold=30
        dominant_nearend_hold_duration=50
        dominant_nearend_enr_threshold=0.25
        dominant_nearend_trigger_threshold=12

    Env-var overrides (all optional; default to BEST_A):
        JASPER_AEC_FILTER_LENGTH        (int, default 30)
        JASPER_AEC_BOUNDED_ERL          (bool, default false)
        JASPER_AEC_DEFAULT_GAIN         (float, default 0.3)
        JASPER_AEC_ERLE_MAX_L           (float, default 1.5)
        JASPER_AEC_ERLE_MAX_H           (float, default 1.0)
        JASPER_AEC_ERLE_ONSET           (bool, default false)
        JASPER_AEC_USE_STATIONARITY     (bool, default true)
        JASPER_AEC_CONSERVATIVE_HF      (bool, default true)
        JASPER_AEC_MASK_HF_ENR_T        (float, default 0.3)
        JASPER_AEC_MASK_HF_ENR_S        (float, default 0.4)
        JASPER_AEC_MASK_HF_EMR_T        (float, default 0.3)
        JASPER_AEC_MAX_DEC_LF           (float, default 0.05)
        JASPER_AEC_NEAREND_AVERAGE_BLOCKS      (int, default 4)
        JASPER_AEC_NEAREND_MASK_HF_ENR_T       (float, default 0.1)
        JASPER_AEC_NEAREND_MASK_HF_ENR_S       (float, default 0.3)
        JASPER_AEC_NEAREND_MASK_HF_EMR_T       (float, default 0.3)
        JASPER_AEC_NEAREND_MAX_DEC_LF          (float, default 0.25)
        JASPER_AEC_NEAREND_MAX_INC             (float, default 2.0)
        JASPER_AEC_DND_SNR_THRESHOLD           (float, default 30)
        JASPER_AEC_DND_HOLD_DURATION           (int, default 50)
        JASPER_AEC_DND_ENR_THRESHOLD           (float, default 0.25)
        JASPER_AEC_DND_TRIGGER_THRESHOLD       (int, default 12)
    """

    def __init__(
        self,
        overrides: dict[str, str] | None = None,
        label: str = "aec3_v2(BEST_A)",
    ) -> None:
        from jasper_aec3 import Aec3V2

        # Top-level (shared with v1)
        ns_enabled = _cfg_bool("JASPER_AEC_NS_ENABLED", "1", overrides)
        ns_level = _cfg_value("JASPER_AEC_NS_LEVEL", "low", overrides).strip().lower()
        agc1_enabled = _cfg_bool("JASPER_AEC_AGC1_ENABLED", "1", overrides)
        agc1_target_dbfs = int(_cfg_value(
            "JASPER_AEC_AGC1_TARGET_DBFS", "9", overrides,
        ))
        agc1_max_gain_db = int(_cfg_value(
            "JASPER_AEC_AGC1_MAX_GAIN_DB", "18", overrides,
        ))
        enable_agc2 = _cfg_bool("JASPER_AEC_AGC2", "0", overrides)

        # Deep EchoCanceller3Config — defaults from BEST_A
        filter_length = int(_cfg_value("JASPER_AEC_FILTER_LENGTH", "30", overrides))
        bounded_erl = _cfg_bool("JASPER_AEC_BOUNDED_ERL", "0", overrides)
        default_gain = float(_cfg_value("JASPER_AEC_DEFAULT_GAIN", "0.3", overrides))
        erle_max_l = float(_cfg_value("JASPER_AEC_ERLE_MAX_L", "1.5", overrides))
        erle_max_h = float(_cfg_value("JASPER_AEC_ERLE_MAX_H", "1.0", overrides))
        erle_onset = _cfg_bool("JASPER_AEC_ERLE_ONSET", "0", overrides)
        use_stationarity = _cfg_bool("JASPER_AEC_USE_STATIONARITY", "1", overrides)
        conservative_hf = _cfg_bool("JASPER_AEC_CONSERVATIVE_HF", "1", overrides)
        mask_hf_enr_t = float(_cfg_value(
            "JASPER_AEC_MASK_HF_ENR_T", "0.3", overrides,
        ))
        mask_hf_enr_s = float(_cfg_value(
            "JASPER_AEC_MASK_HF_ENR_S", "0.4", overrides,
        ))
        mask_hf_emr_t = float(_cfg_value(
            "JASPER_AEC_MASK_HF_EMR_T", "0.3", overrides,
        ))
        max_dec_lf = float(_cfg_value("JASPER_AEC_MAX_DEC_LF", "0.05", overrides))
        nearend_avg_blocks = int(_cfg_value(
            "JASPER_AEC_NEAREND_AVERAGE_BLOCKS", "4", overrides,
        ))
        nearend_mask_hf_enr_t = float(_cfg_value(
            "JASPER_AEC_NEAREND_MASK_HF_ENR_T", "0.1", overrides,
        ))
        nearend_mask_hf_enr_s = float(_cfg_value(
            "JASPER_AEC_NEAREND_MASK_HF_ENR_S", "0.3", overrides,
        ))
        nearend_mask_hf_emr_t = float(_cfg_value(
            "JASPER_AEC_NEAREND_MASK_HF_EMR_T", "0.3", overrides,
        ))
        nearend_max_dec_lf = float(_cfg_value(
            "JASPER_AEC_NEAREND_MAX_DEC_LF", "0.25", overrides,
        ))
        nearend_max_inc = float(_cfg_value(
            "JASPER_AEC_NEAREND_MAX_INC", "2.0", overrides,
        ))
        dnd_snr_threshold = float(_cfg_value(
            "JASPER_AEC_DND_SNR_THRESHOLD", "30", overrides,
        ))
        dnd_hold_duration = int(_cfg_value(
            "JASPER_AEC_DND_HOLD_DURATION", "50", overrides,
        ))
        dnd_enr_threshold = float(_cfg_value(
            "JASPER_AEC_DND_ENR_THRESHOLD", "0.25", overrides,
        ))
        dnd_trigger_threshold = int(_cfg_value(
            "JASPER_AEC_DND_TRIGGER_THRESHOLD", "12", overrides,
        ))
        stream_delay_ms = int(_cfg_value(
            "JASPER_AEC_STREAM_DELAY_MS", "40", overrides,
        ))

        self._aec = Aec3V2(
            stream_delay_ms=stream_delay_ms,
            enable_agc2=enable_agc2,
            ns_enabled=ns_enabled,
            ns_level=ns_level,
            agc1_enabled=agc1_enabled,
            agc1_target_dbfs=agc1_target_dbfs,
            agc1_max_gain_db=agc1_max_gain_db,
            filter_refined_length_blocks=filter_length,
            ep_strength_bounded_erl=bounded_erl,
            ep_strength_default_gain=default_gain,
            erle_max_l=erle_max_l,
            erle_max_h=erle_max_h,
            erle_onset_detection=erle_onset,
            use_stationarity_properties=use_stationarity,
            conservative_hf_suppression=conservative_hf,
            normal_mask_hf_enr_transparent=mask_hf_enr_t,
            normal_mask_hf_enr_suppress=mask_hf_enr_s,
            normal_mask_hf_emr_transparent=mask_hf_emr_t,
            normal_max_dec_factor_lf=max_dec_lf,
            nearend_average_blocks=nearend_avg_blocks,
            nearend_mask_hf_enr_transparent=nearend_mask_hf_enr_t,
            nearend_mask_hf_enr_suppress=nearend_mask_hf_enr_s,
            nearend_mask_hf_emr_transparent=nearend_mask_hf_emr_t,
            nearend_max_dec_factor_lf=nearend_max_dec_lf,
            nearend_max_inc_factor=nearend_max_inc,
            dominant_nearend_snr_threshold=dnd_snr_threshold,
            dominant_nearend_hold_duration=dnd_hold_duration,
            dominant_nearend_enr_threshold=dnd_enr_threshold,
            dominant_nearend_trigger_threshold=dnd_trigger_threshold,
        )
        logger.info(
            "engine=%s ns=%s/%s agc1=%s(target=%d,max=%ddB) "
            "agc2=%s stream_delay_ms=%d filter_len=%d bounded_erl=%s "
            "default_gain=%.2f "
            "erle=%.2f/%.2f onset=%s stationarity=%s conservative_hf=%s "
            "mask_hf=%.2f/%.2f/%.2f max_dec_lf=%.3f "
            "nearend_avg=%d nearend_mask_hf=%.2f/%.2f/%.2f "
            "nearend_max_dec_lf=%.3f nearend_max_inc=%.2f "
            "dnd=snr%.1f/enr%.2f/hold%d/trigger%d",
            label,
            "on" if ns_enabled else "off", ns_level,
            "on" if agc1_enabled else "off",
            agc1_target_dbfs, agc1_max_gain_db,
            "on" if enable_agc2 else "off", stream_delay_ms,
            filter_length, bounded_erl, default_gain,
            erle_max_l, erle_max_h,
            "on" if erle_onset else "off",
            "on" if use_stationarity else "off",
            "on" if conservative_hf else "off",
            mask_hf_enr_t, mask_hf_enr_s, mask_hf_emr_t,
            max_dec_lf,
            nearend_avg_blocks,
            nearend_mask_hf_enr_t, nearend_mask_hf_enr_s,
            nearend_mask_hf_emr_t,
            nearend_max_dec_lf, nearend_max_inc,
            dnd_snr_threshold, dnd_enr_threshold,
            dnd_hold_duration, dnd_trigger_threshold,
        )

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return self._aec.process(mic, ref)

    def close(self) -> None:
        pass


def _select_engine(
    overrides: dict[str, str] | None = None,
    label: str | None = None,
):
    """Pick the AEC engine to use.

    JASPER_AEC_BINDING=v2 forces v2; =v1 forces v1; default (=auto)
    tries v2 first, falls back to v1 if the v2 module isn't built.
    Returns an engine instance ready to call .process().
    """
    pref = os.environ.get("JASPER_AEC_BINDING", "auto").strip().lower()
    if pref == "v1":
        return _Aec3Engine(overrides=overrides, label=label or "aec3_v1")
    if pref == "v2":
        return _Aec3V2Engine(
            overrides=overrides,
            label=label or "aec3_v2(BEST_A)",
        )
    # auto
    try:
        import jasper_aec3
        if jasper_aec3.HAS_V2:
            return _Aec3V2Engine(
                overrides=overrides,
                label=label or "aec3_v2(BEST_A)",
            )
    except ImportError:
        pass
    logger.info(
        "jasper_aec3._aec3_v2 not available — falling back to v1 binding"
    )
    return _Aec3Engine(overrides=overrides, label=label or "aec3_v1")


class _SimpleAGC:
    """Frame-rate peak-tracking AGC for the raw mic UDP leg.
    EXPERIMENTAL — gated off by default. See docs/HANDOFF-vad-experiments.md.

    Tracks per-frame peak with asymmetric attack/release smoothing,
    computes the gain to bring the envelope toward `target_dbfs`,
    capped at `max_gain_db`. Mirrors WebRTC AGC1 (kAdaptiveDigital)
    behaviour without a second AudioProcessing instance — vectorised
    numpy, ~tens of microseconds per 80 ms frame on a Pi 5.

    Defaults match the AGC1 settings the AEC pipeline already uses
    (JASPER_AEC_AGC1_TARGET_DBFS=9, _MAX_GAIN_DB=18) so the raw leg's
    output level lands in the same ballpark as the AEC leg's.

    KNOWN BUG (2026-05-24): hard-clips at full scale on transients
    after the gain has ramped up across attempts. The release
    coefficient slowly raises gain during quiet periods, and a sudden
    loud syllable pushes the output past int16 max, where np.clip
    hard-clips it. Cell 3 of the test matrix saw peaks at exactly
    0.0 dB on attempts 3-7 of a 7-attempt run, and OpenAI's STT
    returned empty transcripts on the distorted audio. Two fixes
    before this is production-ready: (1) replace np.clip with tanh
    soft-limit or a one-frame look-ahead peak limiter that reduces
    gain pre-multiply; (2) optionally pair with a noise-suppression
    stage so the AGC isn't amplifying background noise during pauses.
    The cleaner long-term answer may be a second WebRTC AudioProcessing
    instance with AEC disabled and AGC1+NS+HPF enabled — Option B in
    the handoff doc. Until then, keep `JASPER_AEC_RAW_AGC_ENABLED`
    OFF in production.
    """

    def __init__(
        self,
        target_dbfs: float,
        max_gain_db: float,
        attack_sec: float = 0.010,
        release_sec: float = 0.500,
        frame_sec: float = 0.080,
    ) -> None:
        import math
        self._target = 10.0 ** (-abs(target_dbfs) / 20.0)
        self._max_gain = 10.0 ** (max_gain_db / 20.0)
        self._attack_c = math.exp(-frame_sec / max(attack_sec, 1e-4))
        self._release_c = math.exp(-frame_sec / max(release_sec, 1e-4))
        self._envelope = 1e-6
        self._gain = 1.0
        # Gain glide: smooth gain transitions over ~3 frames (~240 ms)
        # so adapt steps don't introduce audible pumping.
        self._gain_smooth_c = 0.5

    def process(self, pcm_int16: bytes) -> bytes:
        arr = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float32) / 32768.0
        frame_peak = float(np.abs(arr).max())
        if frame_peak > self._envelope:
            self._envelope = (
                self._attack_c * self._envelope
                + (1.0 - self._attack_c) * frame_peak
            )
        else:
            self._envelope = (
                self._release_c * self._envelope
                + (1.0 - self._release_c) * frame_peak
            )
        desired = self._target / max(self._envelope, 1e-6)
        target_gain = min(desired, self._max_gain)
        self._gain = (
            self._gain_smooth_c * self._gain
            + (1.0 - self._gain_smooth_c) * target_gain
        )
        out = arr * self._gain
        np.clip(out, -1.0, 1.0, out=out)
        return (out * 32767.0).astype(np.int16).tobytes()


def _validate_mic_device(config: BridgeConfig | None = None) -> None:
    """Fail before opening the shared reference tap if the mic is absent.

    Validate the mic first so missing hardware fails before we start
    the far-end reference thread. In the normal outputd UDP path this
    avoids useless socket work; in explicit ALSA fallback mode it also
    avoids opening an unnecessary `jasper_ref` reader.
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


def _ref_thread(ref_q: Queue) -> None:
    """Capture 48k stereo ref via alsaaudio (PortAudio doesn't see
    custom asoundrc PCMs like `jasper_capture`), sum L+R to mono,
    downsample to 16k. Push frames of exactly FRAME_SAMPLES samples
    (= 2*FRAME_SAMPLES bytes mono int16) onto the queue.

    Why L+R sum (not left-only): the speakers radiate the sum of
    L and R into a single mic. AEC3 is mono-reference, so we get
    one shot at modeling the echo path. Feeding it L-only would
    blind it to whatever is panned to R — bass, vocals, lead
    instruments — which for typical stereo music is a substantial
    portion of the energy. Summing matches what the room actually
    contains. (The XMOS chip's USB-IN AEC requires left-only per
    datasheet §3.3, but we are not using that path.)

    alsaaudio.PCM.read() can return partial reads (especially the
    first one as the stream warms up), so we accumulate at the 48k
    rate and only emit complete capture_block-sized chunks. This
    guarantees every queued frame matches the mic frame size — the
    WebRTC AEC3 engine enforces equal lengths strictly.

    Optional pre-AEC reference gain (`JASPER_AEC_REF_GAIN_DB`):
    boosts the digital ref before it enters the AEC engine. AEC3
    was tuned for conferencing setups where ref RMS ≈ mic RMS or
    ref is louder; in our smart-speaker setup the digital ref is
    typically 25-30 dB *quieter* than what the mic captures (amp +
    speakers + room amplify the chain). Boosting ref closes that
    gap so the adaptive filter operates near its design point. See
    docs/HANDOFF-aec.md "Tuning findings" for measured impact."""
    global _ref_clipped_samples, _ref_total_samples
    import alsaaudio
    import time as _time
    capture_block = FRAME_SAMPLES * (REF_RATE // SAMPLE_RATE)
    ref_gain_db = float(os.environ.get("JASPER_AEC_REF_GAIN_DB", "0"))
    ref_gain_lin = 10.0 ** (ref_gain_db / 20.0)

    # Reference HPF — matches the effective mic-side cutoff so AEC3
    # sees symmetric inputs. Default 125 Hz to match the chip's
    # AEC_HPFONOFF=on125 (4th-order Butter at mic ingress, applied
    # to channels 0/1 in the chip pipeline). Without symmetric
    # reference filtering, AEC3's adaptive filter wastes coefficients
    # trying to model an LF relationship the mic doesn't have.
    # 125 Hz is above openWakeWord's 60 Hz mel floor for the
    # reference; this filter applies to the reference, not the mic,
    # so wake-word accuracy is unaffected regardless. See
    # docs/HANDOFF-aec.md for the analysis.
    REF_HPF_HZ = float(os.environ.get("JASPER_AEC_REF_HPF_HZ", "125"))
    hpf_sos = butter(2, REF_HPF_HZ, btype="highpass", fs=SAMPLE_RATE,
                     output="sos")
    # Per-section state, shape (n_sections, 2) for order-2 SOS sections.
    # All zeros = starting from silence (correct for thread startup).
    hpf_zi = np.zeros((hpf_sos.shape[0], 2), dtype=np.float64)

    pcm = alsaaudio.PCM(
        type=alsaaudio.PCM_CAPTURE,
        mode=alsaaudio.PCM_NORMAL,  # blocking
        device=REF_DEVICE,
        rate=REF_RATE,
        channels=REF_CHANNELS,
        format=alsaaudio.PCM_FORMAT_S16_LE,
        periodsize=capture_block,
    )
    logger.info(
        "ref capture opened: %s @ %d Hz, %d ch "
        "(pre-AEC gain=%+.1f dB, HPF=%.0f Hz 2nd Butter)",
        REF_DEVICE, REF_RATE, REF_CHANNELS, ref_gain_db, REF_HPF_HZ,
    )
    accum_48 = np.empty(0, dtype=np.float32)
    # Drop-rate debouncing: during a mic stall the ref keeps producing
    # at ~50 Hz, so a naive per-frame WARNING floods the journal with
    # hundreds of entries that all say the same thing. Aggregate the
    # count and log one summary per second instead.
    drops_in_window = 0
    last_drop_log = 0.0
    try:
        while not _shutdown.is_set():
            length, data = pcm.read()
            if length <= 0:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            # interleaved stereo → sum L+R to mono (×0.5 to keep
            # peak-level the same, so REF_GAIN_DB tuning remains valid)
            left48 = arr[0::REF_CHANNELS].astype(np.float32)
            right48 = arr[1::REF_CHANNELS].astype(np.float32)
            mono48 = (left48 + right48) * 0.5
            accum_48 = np.concatenate([accum_48, mono48])
            # Emit exact-sized chunks at the 48k rate so each
            # downsample yields exactly FRAME_SAMPLES at 16k.
            while accum_48.size >= capture_block:
                chunk = accum_48[:capture_block]
                accum_48 = accum_48[capture_block:]
                mono16 = resample_poly(chunk, up=1, down=3)
                # HPF before gain — matches AEC3's internal HPF on the
                # capture side. Stateful across chunks (zi carried over)
                # so there's no per-chunk transient.
                mono16, hpf_zi = sosfilt(hpf_sos, mono16, zi=hpf_zi)
                if ref_gain_lin != 1.0:
                    mono16 = mono16 * ref_gain_lin
                # Track samples that the hard-clip below will saturate.
                # Reported in the periodic RMS log so we can see if the
                # gain stage is destroying peak information.
                _ref_clipped_samples += int(np.sum(np.abs(mono16) > 32767))
                _ref_total_samples += len(mono16)
                mono16 = np.clip(mono16, -32768, 32767).astype(np.int16)
                try:
                    ref_q.put_nowait(mono16.tobytes())
                except Full:
                    _bridge_stats.inc_nested("queue_drops", "ref")
                    drops_in_window += 1
            now = _time.monotonic()
            if drops_in_window > 0 and now - last_drop_log >= 1.0:
                logger.warning(
                    "ref queue full, dropped %d frames in last %.1fs "
                    "(mic queue likely empty — see next stall log)",
                    drops_in_window, now - last_drop_log if last_drop_log else 1.0,
                )
                drops_in_window = 0
                last_drop_log = now
    finally:
        pcm.close()


def _outputd_ref_udp_thread(
    ref_q: Queue,
    config: BridgeConfig | None = None,
) -> None:
    """Receive outputd's final speaker-reference UDP tap and convert it
    to the 16 kHz mono frames AEC3 consumes.

    Unlike `jasper_ref`, this is not a clocked ALSA capture loop: outputd
    sends the exact post-mix buffer it writes to the DAC. Production
    software AEC, chip-AEC, corpus, and diagnostics use this path so they
    all see the same final speaker reference.
    """
    import time as _time

    global _ref_clipped_samples, _ref_total_samples
    config = config or BridgeConfig.from_env()
    ref_gain_db = float(os.environ.get("JASPER_AEC_REF_GAIN_DB", "0"))
    ref_gain_lin = 10.0 ** (ref_gain_db / 20.0)
    ref_hpf_hz = float(os.environ.get("JASPER_AEC_REF_HPF_HZ", "125"))
    hpf_sos = butter(2, ref_hpf_hz, btype="highpass", fs=SAMPLE_RATE,
                     output="sos")
    hpf_zi = np.zeros((hpf_sos.shape[0], 2), dtype=np.float64)
    capture_block = FRAME_SAMPLES * (REF_RATE // SAMPLE_RATE)
    accum_48 = np.empty(0, dtype=np.float32)
    drops_in_window = 0
    last_drop_log = 0.0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((config.outputd_ref_udp_host, config.outputd_ref_udp_port))
    sock.settimeout(0.5)
    logger.info(
        "outputd ref UDP opened: %s:%d @ %d Hz stereo -> %d Hz mono "
        "(pre-AEC gain=%+.1f dB, HPF=%.0f Hz 2nd Butter)",
        config.outputd_ref_udp_host, config.outputd_ref_udp_port,
        REF_RATE, SAMPLE_RATE, ref_gain_db, ref_hpf_hz,
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
            if arr.size < REF_CHANNELS:
                continue
            usable = arr.size - (arr.size % REF_CHANNELS)
            arr = arr[:usable]
            left48 = arr[0::REF_CHANNELS].astype(np.float32)
            right48 = arr[1::REF_CHANNELS].astype(np.float32)
            mono48 = (left48 + right48) * 0.5
            accum_48 = np.concatenate([accum_48, mono48])
            while accum_48.size >= capture_block:
                chunk = accum_48[:capture_block]
                accum_48 = accum_48[capture_block:]
                mono16 = resample_poly(chunk, up=1, down=3)
                mono16, hpf_zi = sosfilt(hpf_sos, mono16, zi=hpf_zi)
                if ref_gain_lin != 1.0:
                    mono16 = mono16 * ref_gain_lin
                _ref_clipped_samples += int(np.sum(np.abs(mono16) > 32767))
                _ref_total_samples += len(mono16)
                mono16 = np.clip(mono16, -32768, 32767).astype(np.int16)
                try:
                    ref_q.put_nowait(mono16.tobytes())
                except Full:
                    _bridge_stats.inc_nested("queue_drops", "ref")
                    drops_in_window += 1
            now = _time.monotonic()
            if drops_in_window > 0 and now - last_drop_log >= 1.0:
                logger.warning(
                    "outputd ref queue full, dropped %d frames in last %.1fs",
                    drops_in_window, now - last_drop_log if last_drop_log else 1.0,
                )
                drops_in_window = 0
                last_drop_log = now
    finally:
        sock.close()


def _mic_thread(
    mic_q: Queue,
    raw0_q: Optional[Queue] = None,
    chip_aec_qs: Optional[dict[str, Queue]] = None,
    chip_beam_plan: _mic_profile.ChipBeamPlan | None = None,
    config: BridgeConfig | None = None,
) -> None:
    """Capture 16k 6ch from XVF chip (6-ch firmware), pluck
    channel MIC_CHANNEL_INDEX (default 1 = the normal WebRTC-AEC3
    near-end feed) and push mono int16 frames into mic_q. In default
    production SHF_BYPASS=1 makes channels 0/1 raw-ish. In chip-AEC
    mode SHF_BYPASS=0 and OP_L/R=[7,0]/[7,1] make channels 0/1 the
    fixed 150°/210° ASR beams.

    If `raw0_q` is provided, ALSO extract channel 2 (raw mic 0, no
    chip DSP) and push it onto that queue. Used by the truly-raw
    UDP leg on OUT_PORT_RAW0. Independent queue + extraction so a
    backlog on one doesn't stall the other.
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
            # Channel 2 = raw mic 0 ADC output, bypasses chip's
            # BF/NS/AGC/HPF. ".copy=True" so the slice doesn't share
            # backing storage with `indata` (which sounddevice reuses).
            raw0 = indata[:, 2].astype(np.int16, copy=True)
            try:
                raw0_q.put_nowait(raw0.tobytes())
            except Full:
                _bridge_stats.inc_nested("queue_drops", "raw0")
                # The raw0 leg is observational; if it can't keep up,
                # drop quietly so we don't spam the journal during a
                # bridge stall that's already noisy via the mic_q path.
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

    with sd.InputStream(
        device=config.mic_device, samplerate=SAMPLE_RATE, channels=MIC_CHANNELS,
        dtype="int16", blocksize=FRAME_SAMPLES, callback=cb,
    ) as stream:
        log_event(
            logger,
            "aec.mic_stream_latency",
            latency_s=stream.latency,
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
        )
        _shutdown.wait()


def _usb_mic_thread(
    usb_q: Queue,
    config: BridgeConfig | None = None,
) -> None:
    """Capture optional cheap-USB-mic audio for corpus-only legs.

    This stream is deliberately independent of the XVF mic stream so
    unplugging or starving the cheap mic can't stall production AEC.
    The bridge only starts this thread when
    JASPER_AEC_CORPUS_USB_ENABLED=1.
    """

    import math

    config = config or BridgeConfig.from_env()
    usb_rate = _usb_capture_rate(config)
    capture_block = max(1, round(FRAME_SAMPLES * usb_rate / SAMPLE_RATE))
    gcd = math.gcd(usb_rate, SAMPLE_RATE)
    up = SAMPLE_RATE // gcd
    down = usb_rate // gcd
    accum_16 = np.empty(0, dtype=np.float32)

    def cb(indata, frames, time_info, status):
        nonlocal accum_16
        if status:
            logger.debug("usb mic status: %s", status)
        if _shutdown.is_set():
            return
        mono = indata[:, 0].astype(np.float32, copy=True)
        if usb_rate != SAMPLE_RATE:
            mono = resample_poly(mono, up=up, down=down)
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
    threshold forever, even though the mic is effectively dead (well under
    1 usable frame/s vs ~12.5/s healthy). Observed 2026-05-31: the bridge ran
    ~13 h in that state with NRestarts=0 until a manual restart.

    This watchdog measures the mic frame *rate* over rolling windows and
    flags a restart only after `max_starved_windows` *consecutive* low-rate
    windows. Conservative by design: a brief blip (one low window) clears
    when the next window recovers, a healthy or merely-degraded mic never
    trips, and `max_starved_windows <= 0` disables it entirely.

    No threads and no blocking I/O — it emits one diagnostic warning per
    starved window (the buildup log) — so it still unit-tests directly: feed
    it `record_frame()` on each consumed frame and `stalled(now)` once per
    loop iteration with a monotonic clock.
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
        """Call every loop iteration with a monotonic timestamp. Returns
        True once the mic frame rate has stayed below the floor for
        `max_starved_windows` consecutive windows — i.e. time to exit
        non-zero for a systemd restart."""
        if self._max_starved <= 0:
            return False
        if self._window_start is None:
            self._window_start = now
            return False
        if now - self._window_start < self._window_sec:
            return False
        # A full window elapsed — score it, then roll over.
        if self._frames < self._min_frames:
            self._starved_windows += 1
            # Buildup logging — mirrors the continuous detector's "stall
            # growing" warnings so a slow-drip restart is never a surprise in
            # the journal: the operator watches the rate collapse first.
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


def _aec_loop(  # noqa: PLR0915
    ref_q: Queue, mic_q: Queue, engine: Optional[_Aec3Engine],
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

    Each iteration consumes one mic frame and one reference frame in
    arrival order. If the reference queue is empty, the loop carries
    forward the last real reference frame instead of injecting silence;
    that keeps AEC3's adaptive filter fed while tolerating bursty
    reference delivery. Primary, raw, corpus, chip-AEC, and optional
    engine outputs are packetized into per-leg UDP streams.

    Debug-record mode: if `JASPER_AEC_DEBUG_RECORD_DIR` is set, the
    bridge writes the AEC engine's input mic stream and pre-gain output
    to WAV files in that directory for offline ERLE analysis.
    """
    config = config or BridgeConfig.from_env()
    # Post-AEC static gain applied to the engine output before it
    # reaches jasper-voice over UDP. Restores level into openWakeWord's training
    # distribution — the HA Voice PE pattern (`gain_factor: 4`) — when
    # the chip's mic preamp delivers a quiet AEC output. Default 0 dB
    # (off). Soft-clipped via tanh on the way out so high gain doesn't
    # injecting hard-clip distortion into the wake-word input. See
    # docs/HANDOFF-aec.md tuning findings for tested values.
    global _ref_clipped_samples, _ref_total_samples
    global _out_clipped_samples, _out_total_samples
    global _ref_starved_frames
    mic_gain_db = float(os.environ.get("JASPER_AEC_MIC_GAIN_DB", "0"))
    mic_gain_lin = 10.0 ** (mic_gain_db / 20.0)
    # Raw-leg AGC: optional adaptive level normalization for the
    # chip-direct UDP stream (udp:9877). Off by default (legacy
    # behaviour). When on, mirrors the AEC pipeline's AGC1 settings
    # so the raw stream's output level lands in the same ballpark.
    # Use this when sending the raw leg to the LLM — without it the
    # chip's mic output is below OpenAI's server-VAD threshold for
    # most of a typical utterance, so the model only sees ~400 ms of
    # a 1.2 s phrase.
    raw_agc_enabled = (
        _env_bool("JASPER_AEC_RAW_AGC_ENABLED", "0")
        and not production_chip_aec_enabled
    )
    raw_agc_target_dbfs = int(os.environ.get(
        "JASPER_AEC_RAW_AGC_TARGET_DBFS",
        os.environ.get("JASPER_AEC_AGC1_TARGET_DBFS", "9"),
    ))
    raw_agc_max_gain_db = int(os.environ.get(
        "JASPER_AEC_RAW_AGC_MAX_GAIN_DB",
        os.environ.get("JASPER_AEC_AGC1_MAX_GAIN_DB", "18"),
    ))
    raw_agc = (
        _SimpleAGC(raw_agc_target_dbfs, raw_agc_max_gain_db)
        if raw_agc_enabled else None
    )
    logger.info(
        "raw_agc=%s (target=%d max=%ddB)",
        "on" if raw_agc_enabled else "off",
        raw_agc_target_dbfs, raw_agc_max_gain_db,
    )
    # Stall-recovery threshold: consecutive seconds of empty mic_q
    # before we bail for a systemd-driven restart. 0 = disabled
    # (legacy "log forever" behaviour). See BridgeStalled docstring.
    stall_restart_sec = int(
        float(os.environ.get("JASPER_AEC_STALL_RESTART_SEC", "5"))
    )
    consecutive_empty_sec = 0
    # Additive slow-drip stall watchdog (see _MicStarvationWatchdog). The
    # consecutive-empty check above resets on a single frame, so an
    # intermittent trickle never trips it; this escalates a sustained low
    # frame rate. JASPER_AEC_STALL_DRIP_MAX_WINDOWS=0 disables it.
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
    # UDP output: localhost, non-blocking sendto. Replaces the old
    # PortAudio RawOutputStream writing to hw:LoopbackAEC,0. `sendto`
    # never blocks on `lo` at our rate (~256 kbps), so the main
    # thread can always observe SIGTERM and exit cleanly inside
    # `TimeoutStopSec=5s` — no more SIGKILL, no more snd-aloop
    # kernel-state corruption.
    emitters: dict[str, LegEmitter] = {}

    def add_emitter(
        leg: str,
        port: int,
        *,
        engine_token: str | None = None,
        frame_samples: int = OUT_FRAME_SAMPLES,
        emitter_cls: type[LegEmitter] = LegEmitter,
    ) -> LegEmitter:
        emitter = emitter_cls(
            sock=socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
            dest=(config.out_host, port),
            batch=bytearray(),
            stats_key=leg,
            frame_samples=frame_samples,
            engine_token=engine_token,
        )
        emitter.sock.setblocking(False)
        emitters[leg] = emitter
        return emitter

    on_emitter = add_emitter("on", config.out_port)
    # Dedicated non-wake consumer for the optional USB host microphone.  This
    # duplicate keeps jasper-voice's frozen :9876 ownership intact; the
    # jasper-usbmic service may bind/unbind independently with no effect on the
    # primary wake/session carrier.
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
    # Secondary socket carries the chip-direct mic (pre-AEC3),
    # batched and packetized identically to the primary AEC ON
    # stream. See OUT_PORT_RAW comment above for the rationale.
    # Independent socket so a sendto failure on one stream doesn't
    # affect the other.
    raw_emitter = add_emitter("off", config.out_port_raw)
    # 4th-leg socket for truly-raw mic 0 (chip channel 2). Same
    # 1280-sample / 16 kHz mono int16 packet shape as the other
    # legs. Independent socket so a sendto failure here doesn't
    # affect the AEC ON or chip-direct paths.
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

    xvf_raw0_engine = None
    xvf_raw0_webrtc_emitter = None
    if xvf_raw0_webrtc_enabled:
        xvf_raw0_engine = _select_engine(label="xvf_raw0_webrtc_aec3")
        xvf_raw0_webrtc_emitter = add_emitter(
            "xvf_raw0_webrtc_aec3",
            config.out_port_xvf_raw0_webrtc_aec3,
        )

    xvf_raw0_dtln_engine = None
    xvf_raw0_dtln_emitter = None
    if xvf_raw0_dtln_enabled:
        try:
            from jasper.aec_engines import dtln_models
            from jasper.aec_engines.dtln import DTLNEngine, default_model_dir
            xvf_raw0_dtln_size = int(os.environ.get(
                "JASPER_AEC_XVF_RAW0_DTLN_SIZE",
                os.environ.get(
                    "JASPER_AEC_DTLN_SIZE", str(dtln_models.DEFAULT_SIZE)
                ),
            ))
            xvf_raw0_dtln_engine = DTLNEngine(
                model_dir=default_model_dir(), model_size=xvf_raw0_dtln_size,
            )
            xvf_raw0_dtln_emitter = add_emitter(
                "xvf_raw0_dtln",
                config.out_port_xvf_raw0_dtln,
            )
            logger.info(
                "XVF raw0 DTLN-aec corpus output enabled: size=%d, udp out=%s:%d",
                xvf_raw0_dtln_size,
                config.out_host,
                config.out_port_xvf_raw0_dtln,
            )
        except (FileNotFoundError, ImportError) as e:
            logger.warning(
                "JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED set but XVF raw0 "
                "DTLN couldn't load: %s. Continuing without xvf_raw0_dtln.",
                e,
            )
    ref_emitter = None
    if emit_ref:
        ref_emitter = add_emitter("ref", config.out_port_ref)

    usb_raw_emitter = None
    usb_webrtc_emitter = None
    usb_engine = None
    usb_dtln_engine = None
    usb_dtln_emitter = None
    if usb_raw_q is not None:
        usb_raw_emitter = add_emitter("usb_raw", config.out_port_usb_raw)
        usb_webrtc_emitter = add_emitter(
            "usb_webrtc",
            config.out_port_usb_webrtc,
        )
        usb_webrtc_overrides = USB_AEC3_CORPUS_OVERRIDES
        usb_webrtc_label = "usb_webrtc/aec3_edge_combo_80"
        usb_webrtc_display_label = USB_AEC3_CORPUS_LABEL
        if (
            _env_bool(AEC3_SWEEP_ENV_FLAG, "0")
            and config.aec3_sweep_input_source == AEC3_SWEEP_SOURCE_USB
        ):
            # In USB AEC3 sweep mode, the normal usb_webrtc leg becomes
            # the 40 ms member of the delay sweep. The three stable
            # variant slots carry the same edge-combo tuning at longer
            # stream-delay hints, giving four same-utterance USB AEC3
            # candidates without adding more sockets.
            usb_webrtc_overrides = USB_AEC3_SWEEP_BASELINE_OVERRIDES
            usb_webrtc_label = "usb_webrtc/aec3_sweep_delay_40"
            usb_webrtc_display_label = USB_AEC3_SWEEP_BASELINE_LABEL
        usb_engine = _select_engine(
            overrides=usb_webrtc_overrides,
            label=usb_webrtc_label,
        )
        logger.info(
            "USB corpus outputs enabled: raw=%s:%d webrtc=%s:%d label=%s",
            config.out_host,
            config.out_port_usb_raw,
            config.out_host,
            config.out_port_usb_webrtc,
            usb_webrtc_display_label,
        )
        if _env_bool("JASPER_AEC_CORPUS_USB_DTLN_ENABLED", "0"):
            try:
                from jasper.aec_engines import dtln_models
                from jasper.aec_engines.dtln import DTLNEngine, default_model_dir
                usb_dtln_size = int(os.environ.get(
                    "JASPER_AEC_USB_DTLN_SIZE",
                    os.environ.get(
                        "JASPER_AEC_DTLN_SIZE", str(dtln_models.DEFAULT_SIZE)
                    ),
                ))
                usb_dtln_engine = DTLNEngine(
                    model_dir=default_model_dir(), model_size=usb_dtln_size,
                )
                usb_dtln_emitter = add_emitter(
                    "usb_dtln",
                    config.out_port_usb_dtln,
                )
                logger.info(
                    "USB DTLN-aec corpus output enabled: size=%d, udp out=%s:%d",
                    usb_dtln_size, config.out_host, config.out_port_usb_dtln,
                )
            except (FileNotFoundError, ImportError) as e:
                logger.warning(
                    "JASPER_AEC_CORPUS_USB_DTLN_ENABLED set but USB DTLN "
                    "couldn't load: %s. Continuing without usb_dtln.",
                    e,
                )

    aec3_sweep_paths: list[dict[str, object]] = []
    if (not production_chip_aec_enabled) and _env_bool(AEC3_SWEEP_ENV_FLAG, "0"):
        if (
            config.aec3_sweep_input_source == AEC3_SWEEP_SOURCE_USB
            and usb_raw_q is None
        ):
            logger.warning(
                "AEC3 sweep requested with input_source=usb but USB corpus "
                "capture is disabled; continuing without sweep variants",
            )
        else:
            for variant in config.aec3_sweep_variants:
                try:
                    variant_engine = _select_engine(
                        overrides=variant.env_overrides,
                        label=(
                            f"aec3_sweep/{config.aec3_sweep_input_source}/"
                            f"{variant.leg}"
                        ),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "AEC3 sweep variant %s couldn't load: %s. "
                        "Continuing without this variant.",
                        variant.leg, e,
                    )
                    continue
                variant_port = config.out_port_aec3_sweep[variant.leg]
                variant_emitter = add_emitter(variant.leg, variant_port)
                aec3_sweep_paths.append({
                    "variant": variant,
                    "engine": variant_engine,
                    "emitter": variant_emitter,
                    "input_source": config.aec3_sweep_input_source,
                })
                logger.info(
                    "AEC3 corpus sweep variant enabled: leg=%s label=%s "
                    "input_source=%s udp out=%s:%d overrides=%s",
                    variant.leg,
                    variant.label,
                    config.aec3_sweep_input_source,
                    config.out_host,
                    variant_port,
                    variant.env_overrides,
                )

    def emit_aec3_sweep(input_bytes: bytes, ref_bytes: bytes) -> None:
        for path in list(aec3_sweep_paths):
            variant = path["variant"]
            engine_variant = path["engine"]
            try:
                variant_clean = engine_variant.process(input_bytes, ref_bytes)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "AEC3 sweep variant %s process() crashed; "
                    "disabling this path: %s",
                    variant.leg, e,
                )
                try:
                    engine_variant.close()
                except Exception:  # noqa: BLE001
                    pass
                emitter = path["emitter"]
                emitter.close()
                emitters.pop(variant.leg, None)
                aec3_sweep_paths.remove(path)
                continue
            path["emitter"].emit(variant_clean)

    # Optional DTLN-aec parallel engine. Constructed once, mutated
    # per-call via maintained LSTM state. See jasper/aec_engines/dtln.py
    # for the streaming algorithm.
    dtln_engine = None
    dtln_emitter = None
    dtln_wanted = (
        not production_chip_aec_enabled
    ) and _env_bool("JASPER_AEC_DTLN_ENABLED", "0")
    _bridge_stats.set_leg_engine("dtln", enabled=dtln_wanted, loaded=False)
    if dtln_wanted:
        try:
            from jasper.aec_engines import dtln_models
            from jasper.aec_engines.dtln import DTLNEngine, default_model_dir
            dtln_size = int(os.environ.get(
                "JASPER_AEC_DTLN_SIZE", str(dtln_models.DEFAULT_SIZE),
            ))
            dtln_engine = DTLNEngine(
                model_dir=default_model_dir(), model_size=dtln_size,
            )
            dtln_emitter = add_emitter("dtln", config.out_port_dtln)
            _bridge_stats.set_leg_engine("dtln", enabled=True, loaded=True)
            logger.info(
                "DTLN-aec engine enabled: size=%d, udp out=%s:%d",
                dtln_size, config.out_host, config.out_port_dtln,
            )
        except Exception as e:  # noqa: BLE001
            # DTLN is an optional tertiary leg. Bad config, malformed ONNX,
            # or another ordinary initialization failure must not crash-loop
            # the healthy primary AEC3 bridge into systemd's reboot ladder.
            if dtln_emitter is not None:
                with suppress(Exception):
                    dtln_emitter.close()
                emitters.pop("dtln", None)
                dtln_emitter = None
            if dtln_engine is not None:
                with suppress(Exception):
                    dtln_engine.close()
                dtln_engine = None
            # Degraded state lands in the stats snapshot so the doctor
            # can flag it long after this line ages out of the journal
            # window — voice keeps listening on the permanently-unfed
            # :9878 leg otherwise with zero surface.
            _bridge_stats.set_leg_engine(
                "dtln", enabled=True, loaded=False, error=str(e),
            )
            log_event(
                logger,
                "aec_bridge.leg_degraded",
                leg="dtln",
                phase="initialize",
                action="continue_aec3",
                error_type=type(e).__name__,
                error=str(e),
                note=(
                    f"JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: {e}. "
                    "Continuing with AEC3 only."
                ),
                level=logging.WARNING,
            )

    output_parts = [f"aec={config.out_host}:{config.out_port}"]
    if usb_host_mic_emitter is not None:
        output_parts.append(
            f"usb_host_mic={config.out_host}:{config.out_port_usb_host_mic}"
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
        variant = path["variant"]
        output_parts.append(
            f"{variant.leg}="
            f"{config.out_host}:{config.out_port_aec3_sweep[variant.leg]}"
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
        mic_fingerprint=config.wake_corpus_mic_fingerprint,
        dac_reference_fingerprint=config.wake_corpus_dac_fingerprint,
    )
    logger.info(
        "udp outputs: %s frame=%d samples (%d bytes)",
        " ".join(output_parts), OUT_FRAME_SAMPLES, OUT_FRAME_BYTES,
    )
    # Voice/wake LegEmitters aggregate four 320-sample frames into one
    # 1280-sample UDP packet so UdpMicCapture keeps its established wire
    # contract. The dedicated USB host-mic consumer emits each 320-sample
    # frame immediately; it is latency-sensitive and has no voice consumer.
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    # Cold-start value for ref carry-forward. Used only until the first
    # real ref frame arrives — after that, last_ref_bytes always holds
    # a previously-real ref. See the drain block in the main loop for
    # why we carry forward instead of falling back to silence.
    last_ref_bytes = silence
    frames_processed = 0
    chip_primary_missing_log = _DropLogDebouncer()

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
    # Running sums for RMS computation across the log window.
    rms_window_frames = 0
    sum_mic_sq = 0.0
    sum_ref_sq = 0.0
    sum_aec_sq = 0.0

    try:
        while not _shutdown.is_set():
            # Slow-drip stall watchdog (additive to the consecutive-empty
            # check below): catches a mic delivering frames too slowly to be
            # usable but often enough to keep consecutive_empty_sec below
            # threshold. See _MicStarvationWatchdog.
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

            # Consume ONE ref frame per main-loop iteration, in order.
            # If the queue is empty, carry forward the previous ref.
            #
            # Background: ALSA's pcm.read() on jasper_ref returns
            # 1024-frame periods (negotiated up from the bridge's
            # request of 960 to match dsnoop's underlying period).
            # With buffer_size = 4 × period_size, ALSA delivers two
            # periods back-to-back every ~40 ms — the bursting is at
            # the ALSA layer, not the bridge. Meanwhile the mic
            # delivers smoothly at the bridge's 20 ms cadence.
            #
            # The original code's "drain to newest" pattern reacted to
            # the burst by discarding the older of the two frames and
            # using only the newest, then on the next iteration finding
            # an empty queue. Two failure modes followed:
            #   1. Fall back to `silence` → every other AEC frame got
            #      zeroed ref → 25 Hz envelope artefact, AEC's
            #      adaptive filter could not converge.
            #   2. Carry forward newest → every other AEC frame was a
            #      LITERAL byte-duplicate of its predecessor → 50 Hz
            #      envelope artefact (audible as buzzing) AND half the
            #      real ref data was being thrown away by the drain.
            #
            # Both prior approaches lost half the real reference. The
            # correct shape is to take one frame per iteration, in
            # arrival order. Burst → consume A this iteration, B next
            # iteration, then 1 replay-from-carry while waiting for
            # the next burst, repeat. Worst case: 1 in 3 frames is a
            # 20 ms-stale carry-forward; that staleness is well within
            # AEC3's delay-estimator tolerance and immediate replays
            # of the same bytes are eliminated entirely.
            #
            # See `docs/HANDOFF-aec.md` "Ref starvation bug (2026-05-19)"
            # for the full diagnosis trail.
            try:
                last_ref_bytes = ref_q.get_nowait()
            except Empty:
                _ref_starved_frames += 1
                _bridge_stats.inc("ref_starved_frames")
            ref_bytes = last_ref_bytes
            if emit_ref:
                ref_emitter.emit(ref_bytes)

            # Emit chip-direct mic on OUT_PORT_RAW BEFORE running
            # the AEC engine. This is the "AEC OFF" leg the
            # wake-telemetry dual-stream consumer wants — same
            # bytes AEC3 is about to receive as near-end input.
            # Batched and packetized identically to the primary
            # stream. sendto failures here never block the AEC
            # pipeline (independent socket, non-blocking, swallowed
            # on EWOULDBLOCK).
            # Optional AGC on the raw leg (does not touch the AEC3
            # input below — engine.process still receives ungained
            # mic_bytes so AEC3's adaptive filter doesn't see a
            # moving level target).
            if not production_chip_aec_enabled:
                raw_emit_bytes = (
                    raw_agc.process(mic_bytes) if raw_agc is not None
                    else mic_bytes
                )
                raw_emitter.emit(raw_emit_bytes)

            # Truly-raw mic 0 (chip channel 2 — no chip DSP) UDP
            # leg. Drained independently of mic_q so a backlog on
            # one doesn't stall the other. The raw0_q is fed from
            # the same PortAudio callback that feeds mic_q, so
            # there's nominally one new raw0 frame per loop
            # iteration; we drain at most one and carry on
            # (silence-fill is fine — nobody time-aligns this
            # stream to the AEC engine).
            raw0_bytes = b""
            if raw0_q is not None:
                try:
                    raw0_bytes = raw0_q.get_nowait()
                except Empty:
                    pass
                if raw0_bytes:
                    raw0_emitter.emit(raw0_bytes)
                    if xvf_raw0_engine is not None:
                        try:
                            xvf_raw0_clean = xvf_raw0_engine.process(
                                raw0_bytes, ref_bytes,
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.exception(
                                "XVF raw0 WebRTC process() crashed; disabling "
                                "xvf_raw0_webrtc_aec3 path: %s",
                                e,
                            )
                            xvf_raw0_engine = None
                            xvf_raw0_clean = b""
                        if xvf_raw0_clean:
                            xvf_raw0_webrtc_emitter.emit(xvf_raw0_clean)
                    if xvf_raw0_dtln_engine is not None:
                        try:
                            xvf_raw0_dtln_clean = xvf_raw0_dtln_engine.process(
                                raw0_bytes, ref_bytes,
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.exception(
                                "XVF raw0 DTLN process() crashed; disabling "
                                "xvf_raw0_dtln path: %s",
                                e,
                            )
                            xvf_raw0_dtln_engine = None
                            xvf_raw0_dtln_clean = b""
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
            # Save pre-gain output for the RMS metric — we want
            # "attenuation" to reflect what AEC actually accomplished,
            # not how much the post-gain stage amplified the residual.
            clean_aec_only = clean

            # DTLN-aec parallel processing path (optional 3rd UDP leg).
            # Runs AFTER the AEC3 engine.process so the wake loop's
            # primary mic stream is on its normal critical path; the
            # extra ~1.5 ms of DTLN inference per frame is on the slack
            # side of the 20 ms frame budget. State is carried by the
            # DTLNEngine instance across calls.
            if dtln_engine is not None:
                try:
                    dtln_clean = dtln_engine.process(mic_bytes, ref_bytes)
                except Exception as e:  # noqa: BLE001
                    # DTLN is observational; preserve the primary AEC3 path
                    # and make this runtime transition authoritative for the
                    # stats writer and doctor. Nulling the engine guarantees
                    # one event rather than one warning per audio frame.
                    failed_dtln_engine = dtln_engine
                    dtln_engine = None
                    with suppress(Exception):
                        failed_dtln_engine.close()
                    failed_dtln_emitter = emitters.pop("dtln", None)
                    if failed_dtln_emitter is not None:
                        with suppress(Exception):
                            failed_dtln_emitter.close()
                    dtln_emitter = None
                    _bridge_stats.mark_leg_unavailable("dtln", error=str(e))
                    log_event(
                        logger,
                        "aec_bridge.leg_degraded",
                        leg="dtln",
                        phase="process",
                        action="disable",
                        error_type=type(e).__name__,
                        error=str(e),
                        level=logging.WARNING,
                        exc_info=True,
                    )
                    dtln_clean = b""
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
                        try:
                            usb_clean = usb_engine.process(usb_bytes, ref_bytes)
                        except Exception as e:  # noqa: BLE001
                            logger.exception(
                                "USB WebRTC process() crashed; disabling "
                                "usb_webrtc path: %s",
                                e,
                            )
                            usb_engine = None
                            usb_clean = b""
                        if usb_clean:
                            usb_webrtc_emitter.emit(usb_clean)

                    if usb_dtln_engine is not None:
                        try:
                            usb_dtln_clean = usb_dtln_engine.process(
                                usb_bytes, ref_bytes,
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.exception(
                                "USB DTLN process() crashed; disabling "
                                "usb_dtln path: %s",
                                e,
                            )
                            usb_dtln_engine = None
                            usb_dtln_clean = b""
                        if usb_dtln_clean:
                            usb_dtln_emitter.emit(usb_dtln_clean)
                    if config.aec3_sweep_input_source == AEC3_SWEEP_SOURCE_USB:
                        emit_aec3_sweep(usb_bytes, ref_bytes)

            # Debug WAV record: writes happen here so the captured
            # frames are exactly what the bridge measured for its
            # internal "attenuation" log + what the AEC emitted before
            # the post-gain stage. Time-aligned to the sample.
            if mic_wav is not None:
                try:
                    mic_wav.writeframes(mic_bytes)
                    aec_wav.writeframes(clean_aec_only)
                    ref_wav.writeframes(ref_bytes)
                except OSError as e:
                    logger.error("debug wav write failed: %s", e)
                    mic_wav = aec_wav = ref_wav = None
            if mic_gain_lin != 1.0:
                arr = np.frombuffer(clean, dtype=np.int16).astype(np.float32) * mic_gain_lin
                _out_clipped_samples += int(np.sum(np.abs(arr) > 32767))
                _out_total_samples += len(arr)
                # tanh soft-clip: smoothly asymptotic to ±32767 instead
                # of hard-clipping. Below ±~26000 it's near-linear.
                arr = 32767.0 * np.tanh(arr / 32767.0)
                clean = arr.astype(np.int16).tobytes()
            on_emitter.emit(clean)
            if usb_host_mic_emitter is not None:
                usb_host_mic_emitter.emit(clean)
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

            now = time.monotonic()
            if now - last_log > 5.0:
                if rms_window_frames > 0:
                    mic_rms = math.sqrt(sum_mic_sq / rms_window_frames)
                    ref_rms = math.sqrt(sum_ref_sq / rms_window_frames)
                    aec_rms = math.sqrt(sum_aec_sq / rms_window_frames)
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
                            "primary=%s:%.0f level_delta=%.1f dB "
                            "(frames=%d ref_q=%d mic_q=%d ref_starve=%d "
                            "ref_clip=%.2f%% out_clip=%.2f%%)",
                            rms_window_frames * FRAME_SAMPLES / SAMPLE_RATE,
                            ref_rms, "chip_aec_210", mic_rms,
                            chip_aec_primary_leg, aec_rms, attn_db,
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
                path["engine"].close()
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
    # Log flight recorder + runtime debug toggle (/system Debug card).
    # See jasper/flight_recorder.py / docs/HANDOFF-observability.md.
    from .. import flight_recorder
    flight_recorder.install("aec")
    config = BridgeConfig.from_env(log_sweep=True, logger_=logger)
    _bridge_stats.reset(config.aec3_sweep_variants)
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
        (
            REF_DEVICE
            if config.ref_source == "alsa"
            else f"udp:{config.outputd_ref_udp_port}"
        ),
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
    if config.ref_source not in {"alsa", "outputd_udp"}:
        logger.error(
            "unsupported JASPER_AEC_REF_SOURCE=%r "
            "(expected 'alsa' or 'outputd_udp')",
            config.ref_source,
        )
        return 1
    if production_chip_aec_enabled:
        if config.ref_source != "outputd_udp":
            logger.error(
                "JASPER_AEC_CHIP_AEC_ENABLED=1 requires "
                "JASPER_AEC_REF_SOURCE=outputd_udp; got %r",
                config.ref_source,
            )
            return 1
        if not os.environ.get("JASPER_OUTPUTD_CHIP_REF_PCM", "").strip():
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

    # Signal handlers for clean shutdown
    def on_signal(signum, _frame):
        logger.info("received signal %d, shutting down", signum)
        _shutdown.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    ref_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)
    mic_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)
    # 4th-leg queue for truly-raw mic 0 (chip channel 2 — no chip
    # DSP). The mic thread fills it from the same callback that
    # fills mic_q; the AEC loop drains it independently to emit on
    # OUT_PORT_RAW0. See OUT_PORT_RAW0 comment for rationale.
    raw0_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)
    chip_aec_qs: dict[str, Queue[bytes]] | None = (
        {beam.token: Queue(maxsize=QUEUE_MAXSIZE) for beam in chip_beam_plan.legs}
        if chip_aec_enabled else None
    )
    usb_q: Queue[bytes] | None = (
        Queue(maxsize=QUEUE_MAXSIZE) if corpus_usb_enabled else None
    )

    if config.ref_source == "outputd_udp":
        ref_t = threading.Thread(
            target=_outputd_ref_udp_thread,
            args=(ref_q, config),
            daemon=True,
        )
    else:
        ref_t = threading.Thread(target=_ref_thread, args=(ref_q,), daemon=True)
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

    # Tier 1 of the resilience ladder. Bumped after each successful
    # frame in `_aec_loop`; if the loop wedges (e.g. mic InputStream
    # stops invoking its callback after a USB underrun on the XVF
    # UAC2 capture), systemd's `WatchdogSec=` expires and revives
    # us via `Restart=on-watchdog`. The original PortAudio
    # output-stream wedge that motivated this rung is now gone
    # under PR 2's UDP transport — `socket.sendto` is non-blocking
    # on `lo` at our rate — but the heartbeat still protects against
    # any future in-process hang. See jasper/watchdog.py header.
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
