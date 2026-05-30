"""Software AEC bridge — `jasper-aec-bridge` (Python).

REPLACES the CamillaDSP-based aec-bridge. The XVF3800's on-chip
AEC turned out to be architecturally incompatible with our
"external USB DAC for the speaker" topology — the chip's AEC
pipeline assumes the chip's own audio output drives the speaker
(see XMOS XVF3800 user guide §3.5; all "Far end" categories are
defined as I²S sources). Even with USB-IN reference + correct
volume mirroring, the chip's adaptive filter doesn't reliably
attenuate echo in our config.

This bridge does the AEC in software, with chip-processed mic
(channel 1 of the chip's 6-channel USB capture — the ASR beam
with chip BF + NS + AGC + HPF applied, but with the chip's own
AEC stage disabled via SHF_BYPASS=1 in jasper-aec-init) as
near-end, and the host-side music chain as far-end. The engine
is WebRTC AEC3 via the `jasper_aec3` pybind11 binding around
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

    pcm.jasper_capture (48k stereo, host clock)
       │  reference signal (what the speaker is being asked to play)
       ▼
    [downsample 48→16k, L+R summed to mono, HPF at 125 Hz]         16k mono ref
       │
       │      hw:Array,0 ch 1 (16k mono, chip clock)
       │  chip ASR beam: BF + NS + AGC + HPF, chip AEC disabled
       │       │
       ▼       ├──────────────────────────────────────────────────┐
    WebRTC AEC3 (jasper_aec3 binding)                             │
       │  AEC'd mono mic                                          │  chip-direct mic (pre-AEC3)
       ▼                                                          ▼
    UDP 127.0.0.1:JASPER_AEC_UDP_PORT (default 9876)      UDP 127.0.0.1:JASPER_AEC_UDP_PORT_RAW
       │  one packet per 1280 samples (80 ms, matches             │  (default 9877)
       │  MicCapture frame size)                                  │  same packet shape
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

import logging
import os
import signal
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
    Aec3SweepConfigError,
    USB_AEC3_CORPUS_LABEL,
    USB_AEC3_CORPUS_OVERRIDES,
    USB_AEC3_SWEEP_BASELINE_LABEL,
    USB_AEC3_SWEEP_BASELINE_OVERRIDES,
    current_aec3_sweep_source,
    load_aec3_sweep_config,
)
from jasper.watchdog import Heartbeat
from ..mics import xvf3800 as _mic_profile

logger = logging.getLogger("jasper.aec_bridge")
AEC3_SWEEP_CONFIG = load_aec3_sweep_config(logger=logger)
AEC3_SWEEP_VARIANTS = AEC3_SWEEP_CONFIG.variants
try:
    AEC3_SWEEP_INPUT_SOURCE = current_aec3_sweep_source()
except Aec3SweepConfigError as e:
    logger.warning(
        "event=aec3_sweep_source_invalid error=%s fallback=%s",
        e, AEC3_SWEEP_SOURCE_XVF,
    )
    AEC3_SWEEP_INPUT_SOURCE = AEC3_SWEEP_SOURCE_XVF
logger.info(
    "event=aec3_sweep_config_loaded source=%s path=%s hash=%s "
    "input_source=%s variants=%s",
    AEC3_SWEEP_CONFIG.source,
    AEC3_SWEEP_CONFIG.path,
    AEC3_SWEEP_CONFIG.config_hash,
    AEC3_SWEEP_INPUT_SOURCE,
    ",".join(variant.leg for variant in AEC3_SWEEP_VARIANTS),
)

# Frame size: 320 samples @ 16 kHz = 20 ms, a multiple of WebRTC
# AEC3's 10 ms frame requirement (160 samples). The binding splits
# 320 → 2×160 internally per the AEC3 API contract. AEC3 manages
# its own filter length internally.
FRAME_SAMPLES = 320
SAMPLE_RATE = 16000

# Capture device for the reference (host-clocked dsnoop on the
# renderer→camilla loopback). `jasper_ref` is a plug-wrapped alias
# of `jasper_capture` defined in /etc/asound.conf. The fan-in topology
# pins the summed loopback to 48 kHz S16_LE; the plug wrapper remains
# a defensive conversion layer if an operator changes the reference
# tap shape.
# CamillaDSP uses the same plug pattern via `plug:jasper_capture`
# in v1.yml — this just extends it to the bridge.
REF_DEVICE = "jasper_ref"
REF_RATE = 48000  # what we ask plug for; plug resamples slave to this
REF_CHANNELS = 2

# Capture device for the mic. Chip's 6-ch firmware exposes
# channels 0=Conference, 1=ASR (both go through BF + NS + AGC +
# HPF; the chip's own AEC stage is disabled via SHF_BYPASS=1 in
# jasper-aec-init), 2-5=raw mics 0-3 (no chip processing of any
# kind). The mic profile pins MIC_CHANNEL_INDEX=1 (ASR beam) —
# canonical XVF3800 voice-assistant choice per Seeed wiki and
# every public reference design.
# Device names are PortAudio substring matches (sounddevice's
# backend) — NOT ALSA pcm strings. PortAudio enumerates ALSA
# cards by their card description, not by hw:CARD= syntax.
# Default matches "Array: USB Audio (hw:N,0)".
MIC_DEVICE = os.environ.get("JASPER_AEC_MIC_DEVICE", _mic_profile.ALSA_CARD_NAME)
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
OUT_HOST = os.environ.get("JASPER_AEC_UDP_HOST", "127.0.0.1")
OUT_PORT = int(os.environ.get("JASPER_AEC_UDP_PORT", "9876"))
OUT_RATE = 16000

# Secondary UDP output: chip-direct mic stream, pre-AEC3 — exactly
# the same near-end input AEC3 consumes (chip ch 1 = ASR beam with
# chip BF + NS + AGC + HPF applied, chip AEC disabled via
# SHF_BYPASS=1). Emitted on a separate port so jasper-voice's wake
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
# Nothing consumes 9877 today (PR 1 of the series is pure
# plumbing). Safe to deploy alone; jasper-voice ignores it until
# PR 2 ships.
OUT_PORT_RAW = int(os.environ.get("JASPER_AEC_UDP_PORT_RAW", "9877"))
# Optional 3rd UDP stream: DTLN-aec output. The bridge constructs a
# DTLNEngine when JASPER_AEC_DTLN_ENABLED=1 and shares the same mic +
# ref capture with the AEC3 engine. Each input chunk is fed to BOTH
# engines; AEC3 output goes to OUT_PORT, DTLN output to OUT_PORT_DTLN.
# Adds ~95 MB RAM + ~12% of one Pi 5 core. Disabled by default during
# the triple-stream rollout; flip via env var per
# docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture plan".
OUT_PORT_DTLN = int(os.environ.get("JASPER_AEC_UDP_PORT_DTLN", "9878"))
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
OUT_PORT_RAW0 = int(os.environ.get("JASPER_AEC_UDP_PORT_RAW0", "9879"))
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
OUT_PORT_REF = int(os.environ.get("JASPER_AEC_UDP_PORT_REF", "9880"))
OUT_PORT_USB_RAW = int(os.environ.get("JASPER_AEC_UDP_PORT_USB_RAW", "9881"))
OUT_PORT_USB_WEBRTC = int(os.environ.get("JASPER_AEC_UDP_PORT_USB_WEBRTC", "9882"))
OUT_PORT_USB_DTLN = int(os.environ.get("JASPER_AEC_UDP_PORT_USB_DTLN", "9883"))
OUT_PORT_AEC3_SWEEP = {
    variant.leg: int(os.environ.get(variant.port_env, str(variant.default_port)))
    for variant in AEC3_SWEEP_VARIANTS
}
USB_MIC_DEVICE = os.environ.get("JASPER_AEC_USB_MIC_DEVICE", "USB PnP Sound Device")
USB_MIC_RATE = int(float(os.environ.get("JASPER_AEC_USB_MIC_RATE", "0")))
# Voice consumes 1280-sample (80 ms) chunks. Aggregating four
# 320-sample AEC frames into one UDP packet keeps the
# bridge↔voice contract symmetric with the existing MicCapture
# frame size and halves packet rate to ~12.5 pps. The AEC engine
# still works on 320-sample windows internally.
OUT_FRAME_SAMPLES = 1280
OUT_FRAME_BYTES = OUT_FRAME_SAMPLES * 2  # int16
BRIDGE_STATS_PATH = Path(
    os.environ.get("JASPER_AEC_BRIDGE_STATS_PATH", "/run/jasper/aec_bridge_stats.json")
)
BRIDGE_STATS_SCHEMA_VERSION = 1

# Drop-frame threshold. If queues fill faster than they drain,
# something's wrong (CPU starvation, clock drift exceeded our
# margin). We log and drop rather than block.
QUEUE_MAXSIZE = 32

_shutdown = threading.Event()


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

    def reset(self) -> None:
        with self._lock:
            self._started_epoch_sec = time.time()
            self._counters = {
                "frames_processed": 0,
                "ref_starved_frames": 0,
                "queue_drops": {
                    "mic": 0,
                    "raw0": 0,
                    "usb": 0,
                    "ref": 0,
                },
                "udp_send_drops_by_leg": {
                    "on": 0,
                    "off": 0,
                    "dtln": 0,
                    "raw0": 0,
                    "ref": 0,
                    "usb_raw": 0,
                    "usb_webrtc": 0,
                    "usb_dtln": 0,
                    **{variant.leg: 0 for variant in AEC3_SWEEP_VARIANTS},
                },
                "packets_sent_by_leg": {
                    "on": 0,
                    "off": 0,
                    "dtln": 0,
                    "raw0": 0,
                    "ref": 0,
                    "usb_raw": 0,
                    "usb_webrtc": 0,
                    "usb_dtln": 0,
                    **{variant.leg: 0 for variant in AEC3_SWEEP_VARIANTS},
                },
            }

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


class BridgeStalled(RuntimeError):
    """Mic capture has produced no frames for the configured
    threshold (JASPER_AEC_STALL_RESTART_SEC, default 5s).

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


def _validate_mic_device() -> None:
    """Fail before opening the shared reference tap if the mic is absent.

    The ref capture reads from `jasper_capture`, the same dsnoop PCM
    CamillaDSP uses for music. If the mic device is missing, starting
    the ref reader anyway creates a pointless second reader until the
    stall watchdog exits. Validate the mic first so missing hardware
    cannot perturb the music path.
    """
    try:
        sd.query_devices(MIC_DEVICE, "input")
    except Exception as e:  # noqa: BLE001
        raise MicDeviceUnavailable(
            f"mic device {MIC_DEVICE!r} unavailable: {e}"
        ) from e


def _validate_usb_mic_device() -> None:
    """Fail fast when corpus USB capture is explicitly enabled but absent."""
    try:
        sd.query_devices(USB_MIC_DEVICE, "input")
    except Exception as e:  # noqa: BLE001
        raise UsbMicUnavailable(
            f"USB corpus mic device {USB_MIC_DEVICE!r} unavailable: {e}"
        ) from e


def _usb_capture_rate() -> int:
    """Return the USB mic capture rate PortAudio can actually open."""
    if USB_MIC_RATE > 0:
        return USB_MIC_RATE
    info = sd.query_devices(USB_MIC_DEVICE, "input")
    rate = int(round(float(info.get("default_samplerate") or SAMPLE_RATE)))
    return rate if rate > 0 else SAMPLE_RATE


def _ref_thread(ref_q: Queue) -> None:
    global _ref_clipped_samples, _ref_total_samples
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


def _mic_thread(mic_q: Queue, raw0_q: Optional[Queue] = None) -> None:
    """Capture 16k 6ch from XVF chip (6-ch firmware), pluck
    channel MIC_CHANNEL_INDEX (default 1 = ASR beam, chip
    BF+NS+AGC+HPF applied, chip AEC disabled via SHF_BYPASS) and
    push mono int16 frames into mic_q.

    If `raw0_q` is provided, ALSO extract channel 2 (raw mic 0, no
    chip DSP) and push it onto that queue. Used by the truly-raw
    UDP leg on OUT_PORT_RAW0. Independent queue + extraction so a
    backlog on one doesn't stall the other.
    """
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
            logger.warning("mic queue full, dropping frame")
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

    with sd.InputStream(
        device=MIC_DEVICE, samplerate=SAMPLE_RATE, channels=MIC_CHANNELS,
        dtype="int16", blocksize=FRAME_SAMPLES, callback=cb,
    ):
        _shutdown.wait()


def _usb_mic_thread(usb_q: Queue) -> None:
    """Capture optional cheap-USB-mic audio for corpus-only legs.

    This stream is deliberately independent of the XVF mic stream so
    unplugging or starving the cheap mic can't stall production AEC.
    The bridge only starts this thread when
    JASPER_AEC_CORPUS_USB_ENABLED=1.
    """

    import math

    usb_rate = _usb_capture_rate()
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
        device=USB_MIC_DEVICE,
        samplerate=usb_rate,
        channels=1,
        dtype="int16",
        blocksize=capture_block,
        callback=cb,
    ):
        logger.info(
            "USB corpus mic capture opened: %s @ %d Hz mono -> %d Hz "
            "(block=%d)",
            USB_MIC_DEVICE, usb_rate, SAMPLE_RATE, capture_block,
        )
        _shutdown.wait()


def _aec_loop(  # noqa: PLR0915
    ref_q: Queue, mic_q: Queue, engine: _Aec3Engine,
    heartbeat: Optional[Heartbeat] = None,
    raw0_q: Optional[Queue] = None,
    emit_ref: bool = False,
    usb_raw_q: Optional[Queue] = None,
) -> None:
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
    raw_agc_enabled = _env_bool("JASPER_AEC_RAW_AGC_ENABLED", "0")
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
    """Drain both queues frame-by-frame, run the selected AEC
    engine, write to Loopback. The two queues drift independently;
    we loosely sync by always pulling one mic frame and the
    freshest ref frame we can grab without blocking — falling back
    to silence if no ref is available (shouldn't happen if camilla
    is running).

    Periodically logs the per-frame RMS of mic, ref, and AEC out
    so we can observe whether the engine is actually attenuating
    the echo. Comparing mic_rms vs aec_rms gives the running
    attenuation in dB.

    Debug-record mode: if `JASPER_AEC_DEBUG_RECORD_DIR` is set, the
    bridge writes the AEC engine's input mic stream and pre-gain
    output to two WAV files in that directory. Used by
    `scripts/aec-erle-record.sh` to capture both sides of the
    engine for offline ERLE analysis — couldn't otherwise be done
    with a second `arecord` because the bridge already holds the
    Array card exclusively via PortAudio."""
    import math
    import socket
    import time
    import wave
    # UDP output: localhost, non-blocking sendto. Replaces the old
    # PortAudio RawOutputStream writing to hw:LoopbackAEC,0. `sendto`
    # never blocks on `lo` at our rate (~256 kbps), so the main
    # thread can always observe SIGTERM and exit cleanly inside
    # `TimeoutStopSec=5s` — no more SIGKILL, no more snd-aloop
    # kernel-state corruption.
    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out_sock.setblocking(False)
    out_dest = (OUT_HOST, OUT_PORT)
    # Secondary socket carries the chip-direct mic (pre-AEC3),
    # batched and packetized identically to the primary AEC ON
    # stream. See OUT_PORT_RAW comment above for the rationale.
    # Independent socket so a sendto failure on one stream doesn't
    # affect the other.
    out_sock_raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out_sock_raw.setblocking(False)
    out_dest_raw = (OUT_HOST, OUT_PORT_RAW)
    # 4th-leg socket for truly-raw mic 0 (chip channel 2). Same
    # 1280-sample / 16 kHz mono int16 packet shape as the other
    # legs. Independent socket so a sendto failure here doesn't
    # affect the AEC ON or chip-direct paths.
    out_sock_raw0 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out_sock_raw0.setblocking(False)
    out_dest_raw0 = (OUT_HOST, OUT_PORT_RAW0)
    out_batch_raw0 = bytearray()
    out_sock_ref = None
    out_dest_ref = None
    out_batch_ref = bytearray()
    if emit_ref:
        out_sock_ref = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out_sock_ref.setblocking(False)
        out_dest_ref = (OUT_HOST, OUT_PORT_REF)

    out_sock_usb_raw = None
    out_dest_usb_raw = None
    out_batch_usb_raw = bytearray()
    out_sock_usb_webrtc = None
    out_dest_usb_webrtc = None
    out_batch_usb_webrtc = bytearray()
    usb_engine = None
    usb_dtln_engine = None
    out_sock_usb_dtln = None
    out_dest_usb_dtln = None
    out_batch_usb_dtln = bytearray()
    if usb_raw_q is not None:
        out_sock_usb_raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out_sock_usb_raw.setblocking(False)
        out_dest_usb_raw = (OUT_HOST, OUT_PORT_USB_RAW)
        out_sock_usb_webrtc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out_sock_usb_webrtc.setblocking(False)
        out_dest_usb_webrtc = (OUT_HOST, OUT_PORT_USB_WEBRTC)
        usb_webrtc_overrides = USB_AEC3_CORPUS_OVERRIDES
        usb_webrtc_label = "usb_webrtc/aec3_edge_combo_80"
        usb_webrtc_display_label = USB_AEC3_CORPUS_LABEL
        if (
            _env_bool(AEC3_SWEEP_ENV_FLAG, "0")
            and AEC3_SWEEP_INPUT_SOURCE == AEC3_SWEEP_SOURCE_USB
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
            OUT_HOST, OUT_PORT_USB_RAW, OUT_HOST, OUT_PORT_USB_WEBRTC,
            usb_webrtc_display_label,
        )
        if _env_bool("JASPER_AEC_CORPUS_USB_DTLN_ENABLED", "0"):
            try:
                from jasper.aec_engines.dtln import DTLNEngine, default_model_dir
                usb_dtln_size = int(os.environ.get(
                    "JASPER_AEC_USB_DTLN_SIZE",
                    os.environ.get("JASPER_AEC_DTLN_SIZE", "256"),
                ))
                usb_dtln_engine = DTLNEngine(
                    model_dir=default_model_dir(), model_size=usb_dtln_size,
                )
                out_sock_usb_dtln = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                out_sock_usb_dtln.setblocking(False)
                out_dest_usb_dtln = (OUT_HOST, OUT_PORT_USB_DTLN)
                logger.info(
                    "USB DTLN-aec corpus output enabled: size=%d, udp out=%s:%d",
                    usb_dtln_size, OUT_HOST, OUT_PORT_USB_DTLN,
                )
            except (FileNotFoundError, ImportError) as e:
                logger.warning(
                    "JASPER_AEC_CORPUS_USB_DTLN_ENABLED set but USB DTLN "
                    "couldn't load: %s. Continuing without usb_dtln.",
                    e,
                )

    aec3_sweep_paths: list[dict[str, object]] = []
    if _env_bool(AEC3_SWEEP_ENV_FLAG, "0"):
        if (
            AEC3_SWEEP_INPUT_SOURCE == AEC3_SWEEP_SOURCE_USB
            and usb_raw_q is None
        ):
            logger.warning(
                "AEC3 sweep requested with input_source=usb but USB corpus "
                "capture is disabled; continuing without sweep variants",
            )
        else:
            for variant in AEC3_SWEEP_VARIANTS:
                try:
                    variant_engine = _select_engine(
                        overrides=variant.env_overrides,
                        label=(
                            f"aec3_sweep/{AEC3_SWEEP_INPUT_SOURCE}/"
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
                variant_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                variant_sock.setblocking(False)
                variant_port = OUT_PORT_AEC3_SWEEP[variant.leg]
                aec3_sweep_paths.append({
                    "variant": variant,
                    "engine": variant_engine,
                    "sock": variant_sock,
                    "dest": (OUT_HOST, variant_port),
                    "batch": bytearray(),
                    "input_source": AEC3_SWEEP_INPUT_SOURCE,
                })
                logger.info(
                    "AEC3 corpus sweep variant enabled: leg=%s label=%s "
                    "input_source=%s udp out=%s:%d overrides=%s",
                    variant.leg, variant.label, AEC3_SWEEP_INPUT_SOURCE,
                    OUT_HOST, variant_port, variant.env_overrides,
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
                try:
                    path["sock"].close()
                except OSError:
                    pass
                aec3_sweep_paths.remove(path)
                continue
            batch = path["batch"]
            batch.extend(variant_clean)
            if len(batch) >= OUT_FRAME_BYTES:
                try:
                    path["sock"].sendto(
                        bytes(batch[:OUT_FRAME_BYTES]),
                        path["dest"],
                    )
                    _bridge_stats.inc_nested(
                        "packets_sent_by_leg", variant.leg,
                    )
                except BlockingIOError:
                    _bridge_stats.inc_nested(
                        "udp_send_drops_by_leg", variant.leg,
                    )
                    logger.warning(
                        "udp %s sendto would block, dropping frame",
                        variant.leg,
                    )
                del batch[:OUT_FRAME_BYTES]

    # Optional DTLN-aec parallel engine. Constructed once, mutated
    # per-call via maintained LSTM state. See jasper/aec_engines/dtln.py
    # for the streaming algorithm.
    dtln_engine = None
    out_sock_dtln = None
    out_dest_dtln = None
    out_batch_dtln = bytearray()
    if _env_bool("JASPER_AEC_DTLN_ENABLED", "0"):
        try:
            from jasper.aec_engines.dtln import DTLNEngine, default_model_dir
            dtln_size = int(os.environ.get("JASPER_AEC_DTLN_SIZE", "256"))
            dtln_engine = DTLNEngine(
                model_dir=default_model_dir(), model_size=dtln_size,
            )
            out_sock_dtln = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            out_sock_dtln.setblocking(False)
            out_dest_dtln = (OUT_HOST, OUT_PORT_DTLN)
            logger.info(
                "DTLN-aec engine enabled: size=%d, udp out=%s:%d",
                dtln_size, OUT_HOST, OUT_PORT_DTLN,
            )
        except (FileNotFoundError, ImportError) as e:
            logger.warning(
                "JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: %s. "
                "Continuing with AEC3 only.", e,
            )

    output_parts = [
        f"aec={OUT_HOST}:{OUT_PORT}",
        f"raw={OUT_HOST}:{OUT_PORT_RAW}",
        f"raw0={OUT_HOST}:{OUT_PORT_RAW0}",
    ]
    if dtln_engine is not None:
        output_parts.append(f"dtln={OUT_HOST}:{OUT_PORT_DTLN}")
    if emit_ref:
        output_parts.append(f"ref={OUT_HOST}:{OUT_PORT_REF}")
    if usb_raw_q is not None:
        output_parts.append(f"usb_raw={OUT_HOST}:{OUT_PORT_USB_RAW}")
        output_parts.append(f"usb_webrtc={OUT_HOST}:{OUT_PORT_USB_WEBRTC}")
    if usb_dtln_engine is not None:
        output_parts.append(f"usb_dtln={OUT_HOST}:{OUT_PORT_USB_DTLN}")
    for path in aec3_sweep_paths:
        variant = path["variant"]
        output_parts.append(
            f"{variant.leg}={OUT_HOST}:{OUT_PORT_AEC3_SWEEP[variant.leg]}"
        )
    logger.info(
        "udp outputs: %s frame=%d samples (%d bytes)",
        " ".join(output_parts), OUT_FRAME_SAMPLES, OUT_FRAME_BYTES,
    )
    # Aggregate four AEC frames (320 samples each) into one UDP
    # packet (1280 samples = MicCapture.OUTPUT_FRAME_SAMPLES) so
    # voice's UdpMicCapture sees the same chunk size it gets from
    # the PortAudio path. Bytearray rather than list-of-bytes to
    # avoid per-frame allocation churn. The _raw batch tracks the
    # chip-direct mic stream on the same cadence.
    out_batch = bytearray()
    out_batch_raw = bytearray()
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    # Cold-start value for ref carry-forward. Used only until the first
    # real ref frame arrives — after that, last_ref_bytes always holds
    # a previously-real ref. See the drain block in the main loop for
    # why we carry forward instead of falling back to silence.
    last_ref_bytes = silence
    frames_processed = 0

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
            try:
                mic_bytes = mic_q.get(timeout=1.0)
                consecutive_empty_sec = 0
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
                out_batch_ref.extend(ref_bytes)
                if len(out_batch_ref) >= OUT_FRAME_BYTES:
                    try:
                        out_sock_ref.sendto(
                            bytes(out_batch_ref[:OUT_FRAME_BYTES]),
                            out_dest_ref,
                        )
                        _bridge_stats.inc_nested("packets_sent_by_leg", "ref")
                    except BlockingIOError:
                        _bridge_stats.inc_nested("udp_send_drops_by_leg", "ref")
                        logger.warning("udp ref sendto would block, dropping frame")
                    del out_batch_ref[:OUT_FRAME_BYTES]

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
            raw_emit_bytes = (
                raw_agc.process(mic_bytes) if raw_agc is not None
                else mic_bytes
            )
            out_batch_raw.extend(raw_emit_bytes)
            if len(out_batch_raw) >= OUT_FRAME_BYTES:
                try:
                    out_sock_raw.sendto(
                        bytes(out_batch_raw[:OUT_FRAME_BYTES]),
                        out_dest_raw,
                    )
                    _bridge_stats.inc_nested("packets_sent_by_leg", "off")
                except BlockingIOError:
                    _bridge_stats.inc_nested("udp_send_drops_by_leg", "off")
                    logger.warning(
                        "udp raw sendto would block, dropping frame"
                    )
                del out_batch_raw[:OUT_FRAME_BYTES]

            # Truly-raw mic 0 (chip channel 2 — no chip DSP) UDP
            # leg. Drained independently of mic_q so a backlog on
            # one doesn't stall the other. The raw0_q is fed from
            # the same PortAudio callback that feeds mic_q, so
            # there's nominally one new raw0 frame per loop
            # iteration; we drain at most one and carry on
            # (silence-fill is fine — nobody time-aligns this
            # stream to the AEC engine).
            if raw0_q is not None:
                try:
                    raw0_bytes = raw0_q.get_nowait()
                    out_batch_raw0.extend(raw0_bytes)
                except Empty:
                    pass
                if len(out_batch_raw0) >= OUT_FRAME_BYTES:
                    try:
                        out_sock_raw0.sendto(
                            bytes(out_batch_raw0[:OUT_FRAME_BYTES]),
                            out_dest_raw0,
                        )
                        _bridge_stats.inc_nested("packets_sent_by_leg", "raw0")
                    except BlockingIOError:
                        _bridge_stats.inc_nested("udp_send_drops_by_leg", "raw0")
                        logger.warning(
                            "udp raw0 sendto would block, dropping frame"
                        )
                    del out_batch_raw0[:OUT_FRAME_BYTES]

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
                    # Don't let a DTLN crash take the bridge down — log
                    # and disable the parallel path. The AEC3 engine is
                    # the production critical path; DTLN is observational.
                    logger.exception(
                        "DTLN-aec process() crashed; disabling DTLN path: %s", e,
                    )
                    dtln_engine = None
                    dtln_clean = b""
                if dtln_clean:
                    out_batch_dtln.extend(dtln_clean)
                    if len(out_batch_dtln) >= OUT_FRAME_BYTES:
                        try:
                            out_sock_dtln.sendto(
                                bytes(out_batch_dtln[:OUT_FRAME_BYTES]),
                                out_dest_dtln,
                            )
                            _bridge_stats.inc_nested("packets_sent_by_leg", "dtln")
                        except BlockingIOError:
                            _bridge_stats.inc_nested(
                                "udp_send_drops_by_leg", "dtln",
                            )
                            logger.warning(
                                "udp dtln sendto would block, dropping frame"
                            )
                        del out_batch_dtln[:OUT_FRAME_BYTES]

            if AEC3_SWEEP_INPUT_SOURCE == AEC3_SWEEP_SOURCE_XVF:
                emit_aec3_sweep(mic_bytes, ref_bytes)

            if usb_raw_q is not None:
                try:
                    usb_bytes = usb_raw_q.get_nowait()
                except Empty:
                    usb_bytes = b""
                if usb_bytes:
                    out_batch_usb_raw.extend(usb_bytes)
                    if len(out_batch_usb_raw) >= OUT_FRAME_BYTES:
                        try:
                            out_sock_usb_raw.sendto(
                                bytes(out_batch_usb_raw[:OUT_FRAME_BYTES]),
                                out_dest_usb_raw,
                            )
                            _bridge_stats.inc_nested(
                                "packets_sent_by_leg", "usb_raw",
                            )
                        except BlockingIOError:
                            _bridge_stats.inc_nested(
                                "udp_send_drops_by_leg", "usb_raw",
                            )
                            logger.warning(
                                "udp usb_raw sendto would block, dropping frame"
                            )
                        del out_batch_usb_raw[:OUT_FRAME_BYTES]

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
                            out_batch_usb_webrtc.extend(usb_clean)
                            if len(out_batch_usb_webrtc) >= OUT_FRAME_BYTES:
                                try:
                                    out_sock_usb_webrtc.sendto(
                                        bytes(out_batch_usb_webrtc[:OUT_FRAME_BYTES]),
                                        out_dest_usb_webrtc,
                                    )
                                    _bridge_stats.inc_nested(
                                        "packets_sent_by_leg", "usb_webrtc",
                                    )
                                except BlockingIOError:
                                    _bridge_stats.inc_nested(
                                        "udp_send_drops_by_leg", "usb_webrtc",
                                    )
                                    logger.warning(
                                        "udp usb_webrtc sendto would block, "
                                        "dropping frame"
                                    )
                                del out_batch_usb_webrtc[:OUT_FRAME_BYTES]

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
                            out_batch_usb_dtln.extend(usb_dtln_clean)
                            if len(out_batch_usb_dtln) >= OUT_FRAME_BYTES:
                                try:
                                    out_sock_usb_dtln.sendto(
                                        bytes(out_batch_usb_dtln[:OUT_FRAME_BYTES]),
                                        out_dest_usb_dtln,
                                    )
                                    _bridge_stats.inc_nested(
                                        "packets_sent_by_leg", "usb_dtln",
                                    )
                                except BlockingIOError:
                                    _bridge_stats.inc_nested(
                                        "udp_send_drops_by_leg", "usb_dtln",
                                    )
                                    logger.warning(
                                        "udp usb_dtln sendto would block, "
                                        "dropping frame"
                                    )
                                del out_batch_usb_dtln[:OUT_FRAME_BYTES]
                    if AEC3_SWEEP_INPUT_SOURCE == AEC3_SWEEP_SOURCE_USB:
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
            out_batch.extend(clean)
            if len(out_batch) >= OUT_FRAME_BYTES:
                # `sendto` is non-blocking (setblocking(False) above).
                # On `lo` at ~256 kbps, the kernel UDP send buffer
                # never fills, so BlockingIOError essentially never
                # fires; if it ever does, dropping the packet is the
                # right call (voice sees an 80 ms gap, recovers next
                # frame).
                try:
                    out_sock.sendto(bytes(out_batch[:OUT_FRAME_BYTES]), out_dest)
                    _bridge_stats.inc_nested("packets_sent_by_leg", "on")
                except BlockingIOError:
                    _bridge_stats.inc_nested("udp_send_drops_by_leg", "on")
                    logger.warning("udp out sendto would block, dropping frame")
                del out_batch[:OUT_FRAME_BYTES]
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
        out_sock.close()
        out_sock_raw.close()
        out_sock_raw0.close()
        if out_sock_ref is not None:
            out_sock_ref.close()
        if out_sock_usb_raw is not None:
            out_sock_usb_raw.close()
        if out_sock_usb_webrtc is not None:
            out_sock_usb_webrtc.close()
        if out_sock_usb_dtln is not None:
            out_sock_usb_dtln.close()
        if usb_engine is not None:
            usb_engine.close()
        if usb_dtln_engine is not None:
            usb_dtln_engine.close()
        if out_sock_dtln is not None:
            out_sock_dtln.close()
        for path in aec3_sweep_paths:
            try:
                path["sock"].close()
            except OSError:
                pass
            try:
                path["engine"].close()
            except Exception:  # noqa: BLE001
                pass
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
    _bridge_stats.reset()
    _bridge_stats.write_snapshot()
    corpus_ref_enabled = _env_bool("JASPER_AEC_CORPUS_REF_ENABLED", "0")
    corpus_usb_enabled = _env_bool("JASPER_AEC_CORPUS_USB_ENABLED", "0")
    corpus_usb_dtln_enabled = _env_bool(
        "JASPER_AEC_CORPUS_USB_DTLN_ENABLED", "0",
    )
    corpus_aec3_sweep_enabled = _env_bool(AEC3_SWEEP_ENV_FLAG, "0")
    logger.info(
        "starting: ref=%s@%d mic=%s@%d ch=%d->ch%d "
        "aec_out=udp://%s:%d raw_out=udp://%s:%d @%d "
        "corpus_ref=%s corpus_usb=%s corpus_usb_dtln=%s "
        "corpus_aec3_sweep=%s corpus_aec3_sweep_source=%s",
        REF_DEVICE, REF_RATE, MIC_DEVICE, SAMPLE_RATE,
        MIC_CHANNELS, MIC_CHANNEL_INDEX,
        OUT_HOST, OUT_PORT, OUT_HOST, OUT_PORT_RAW, OUT_RATE,
        "on" if corpus_ref_enabled else "off",
        "on" if corpus_usb_enabled else "off",
        "on" if corpus_usb_dtln_enabled else "off",
        "on" if corpus_aec3_sweep_enabled else "off",
        AEC3_SWEEP_INPUT_SOURCE,
    )
    if corpus_usb_dtln_enabled and not corpus_usb_enabled:
        logger.warning(
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1 is ignored unless "
            "JASPER_AEC_CORPUS_USB_ENABLED=1 also starts the USB mic capture",
        )
    if (
        corpus_aec3_sweep_enabled
        and AEC3_SWEEP_INPUT_SOURCE == AEC3_SWEEP_SOURCE_USB
        and not corpus_usb_enabled
    ):
        logger.warning(
            "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb is ignored unless "
            "JASPER_AEC_CORPUS_USB_ENABLED=1 also starts the USB mic capture",
        )

    try:
        _validate_mic_device()
    except MicDeviceUnavailable as e:
        logger.error("%s", e)
        return 1
    if corpus_usb_enabled:
        try:
            _validate_usb_mic_device()
        except UsbMicUnavailable as e:
            logger.error("%s", e)
            return 1

    engine = _select_engine()

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
    usb_q: Queue[bytes] | None = (
        Queue(maxsize=QUEUE_MAXSIZE) if corpus_usb_enabled else None
    )

    ref_t = threading.Thread(target=_ref_thread, args=(ref_q,), daemon=True)
    mic_t = threading.Thread(
        target=_mic_thread, args=(mic_q, raw0_q), daemon=True,
    )
    usb_t = (
        threading.Thread(target=_usb_mic_thread, args=(usb_q,), daemon=True)
        if usb_q is not None else None
    )
    ref_t.start()
    mic_t.start()
    if usb_t is not None:
        usb_t.start()
    stats_t = threading.Thread(
        target=_bridge_stats_writer, name="aec-bridge-stats", daemon=True,
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
            emit_ref=corpus_ref_enabled,
            usb_raw_q=usb_q,
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
        engine.close()
        _bridge_stats.write_snapshot()
        ref_t.join(timeout=2)
        mic_t.join(timeout=2)
        if usb_t is not None:
            usb_t.join(timeout=2)
        stats_t.join(timeout=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
