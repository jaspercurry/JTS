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
import logging
import math
import os
import socket
import signal
import sys
import threading
import time
from queue import Queue, Empty
from pathlib import Path
from typing import Any, Optional

import numpy as np

from jasper.aec_sweep import (
    AEC3_SWEEP_ENV_FLAG,
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    DEFAULT_AEC3_SWEEP_VARIANTS,
)
from jasper.watchdog import Heartbeat
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
from jasper.cli.aec_bridge_capture import (
    MIC_CHANNEL_INDEX,
    MIC_CHANNELS,
    mic_thread,
    usb_mic_thread,
)
from jasper.cli.aec_bridge_config import (
    BridgeConfig,
    MicDeviceUnavailable,
    OUTPUTD_REF_UDP_HOST,
    OUTPUTD_REF_UDP_PORT,
    REF_SOURCE,
    UnsupportedReferenceSource,
    UsbMicUnavailable,
    _chip_aec_primary_leg,
    _chip_beam_plan,
    env_bool,
    leg_default_port,
    resolve_usb_mic_source,
    resolved_reference_source,
    validate_mic_device,
    validate_usb_mic_device,
)
from jasper.cli.aec_bridge_reference import (
    REF_RATE,
    outputd_ref_udp_thread,
    ref_clip_percent,
    reset_ref_clip_counters,
)
from jasper.cli.aec_bridge_telemetry import (
    BRIDGE_STATS_PATH,
    DropLogDebouncer,
    LegEmitter,
    OUT_FRAME_BYTES,
    OUT_FRAME_SAMPLES,
    StatsIdentity,
    TimestampedLegEmitter,
    _BridgeStats,
    logger,
)
from jasper.usb_mic import USB_MIC_RAW_XVF_LEG
from ..mics import xvf3800 as _mic_profile

AEC3_SWEEP_VARIANTS = DEFAULT_AEC3_SWEEP_VARIANTS

OUT_PORT = leg_default_port("on")
OUT_RATE = 16000

# Chip-direct mic stream, pre-AEC3 — exactly the near-end input AEC3
# consumes in default production (chip ch 1, raw-ish when SHF_BYPASS=1), on
# its own port and in the same 1280-sample / 16 kHz mono int16 packet shape
# as the primary leg. jasper-voice's wake loop ORs detections across the
# post-AEC (OUT_PORT) and chip-direct legs, which catch mostly-disjoint sets
# of utterances. It consumes this leg only when the reconciler configures
# `JASPER_MIC_DEVICE_RAW`; otherwise the extra packets are ignored.
OUT_PORT_RAW = leg_default_port("off")
# 4th UDP stream: truly-raw mic 0 (chip channel 2). Unlike the chip-direct
# stream on OUT_PORT_RAW (chip channel 1 = ASR beam, with chip BF+NS+AGC+HPF
# applied), channel 2 is the raw mic 0 ADC output with NO chip DSP whatsoever
# — not even MIC_GAIN, i.e. what a mic without an XMOS chip would deliver.
# Read by the wake-corpus recorder as the mic-agnostic baseline. Same packet
# shape as the other legs; always emitted, at ~0.25% of one core.
OUT_PORT_RAW0 = leg_default_port("raw0")
# Corpus-only experiment streams, off by default so production bridge cost
# does not move. When enabled for wake-corpus recording, the bridge emits:
#   - ref: the 16 kHz mono reference frame AEC3 actually consumed
#   - usb_raw: a cheap USB mic's raw mono capture
#   - usb_webrtc: that same USB mic through a second WebRTC AEC3 chain
#   - usb_dtln: the cheap USB mic through a second DTLN-aec chain
#
# jasper-voice never consumes these.
OUT_PORT_REF = leg_default_port("ref")
OUT_PORT_USB_RAW = leg_default_port("usb_raw")
OUT_PORT_USB_WEBRTC = leg_default_port("usb_webrtc")
OUT_PORT_USB_DTLN = leg_default_port("usb_dtln")
OUT_PORT_CHIP_AEC_150 = leg_default_port("chip_aec_150")
OUT_PORT_CHIP_AEC_210 = leg_default_port("chip_aec_210")
OUT_PORT_AEC3_SWEEP = {
    variant.leg: variant.default_port
    for variant in AEC3_SWEEP_VARIANTS
}

# Drop-frame threshold: if queues fill faster than they drain (CPU
# starvation, clock drift past the margin), log and drop rather than block.
QUEUE_MAXSIZE = 32

_shutdown = threading.Event()


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


# Clipping counters for the post-AEC mic stage (after JASPER_AEC_MIC_GAIN_DB),
# module-level for cheap cross-thread access: a race between increment and
# reset costs at most one frame in one log window's percentage. The reference
# pre-clip stage keeps its own pair in `aec_bridge_reference`.
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
    import wave
    usb_mic_choice_plan = chip_beam_plan or _mic_profile.chip_beam_plan_from_env(
        os.environ,
    )
    usb_mic_source = resolve_usb_mic_source(
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
            port = chip_aec_ports.get(beam.token, leg_default_port(beam.token))
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
                port = leg_default_port(beam.token)
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
    chip_primary_missing_log = DropLogDebouncer()
    usb_mic_leg_missing_log = DropLogDebouncer()
    usb_mic_raw0_missing_log = DropLogDebouncer()
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
                assert engine is not None  # main() sets it unless chip-AEC
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
                    ref_clip_pct = ref_clip_percent()
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
                reset_ref_clip_counters()
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
        config = resolved_reference_source(config)
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
    corpus_ref_enabled = env_bool("JASPER_AEC_CORPUS_REF_ENABLED", "0")
    corpus_usb_enabled = env_bool("JASPER_AEC_CORPUS_USB_ENABLED", "0")
    corpus_usb_dtln_enabled = env_bool(
        "JASPER_AEC_CORPUS_USB_DTLN_ENABLED", "0",
    )
    corpus_aec3_sweep_enabled = env_bool(AEC3_SWEEP_ENV_FLAG, "0")
    corpus_chip_aec_enabled = env_bool(
        "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "0",
    )
    production_chip_aec_enabled = env_bool(_mic_profile.CHIP_AEC_ENABLED_ENV, "0")
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
    corpus_xvf_raw0_webrtc_enabled = env_bool(
        "JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED", "0",
    )
    corpus_xvf_raw0_dtln_enabled = env_bool(
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
        validate_mic_device(config)
    except MicDeviceUnavailable as e:
        logger.error("%s", e)
        return 1
    if corpus_usb_enabled:
        try:
            validate_usb_mic_device(config)
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
        target=outputd_ref_udp_thread,
        args=(ref_q,),
        kwargs={
            "host": config.outputd_ref_udp_host,
            "port": config.outputd_ref_udp_port,
            "stats": _bridge_stats,
            "shutdown": _shutdown,
        },
        daemon=True,
    )
    mic_t = threading.Thread(
        target=mic_thread,
        args=(mic_q, raw0_q, chip_aec_qs, chip_beam_plan),
        kwargs={
            "mic_device": config.mic_device,
            "capture_latency": config.capture_latency,
            "stats": _bridge_stats,
            "shutdown": _shutdown,
        },
        daemon=True,
    )
    usb_t = (
        threading.Thread(
            target=usb_mic_thread,
            args=(usb_q,),
            kwargs={
                "usb_mic_device": config.usb_mic_device,
                "usb_mic_rate": config.usb_mic_rate,
                "stats": _bridge_stats,
                "shutdown": _shutdown,
            },
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
